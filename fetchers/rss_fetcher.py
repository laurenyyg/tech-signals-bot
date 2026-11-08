import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import anthropic
import feedparser
import requests

from config import SUMMARY_TRUNCATE_CHARS
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

FEEDS: Dict[str, str] = {
    "techmeme": "https://www.techmeme.com/feed.xml",
    "techcrunch": "https://techcrunch.com/feed/",
    "openai_news": "https://openai.com/news/rss.xml",
}

ANTHROPIC_PAGES: Dict[str, str] = {
    "anthropic_news": "https://www.anthropic.com/news",
    "anthropic_research": "https://www.anthropic.com/research",
}
ANTHROPIC_SOURCES: Tuple[str, ...] = ("anthropic_news", "anthropic_research")

_TRACKING_QUERY_EXACT = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "_ga"}
)

# Strip trailing " | TechCrunch" / " - Techmeme" style suffixes for title-level dedupe.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*([|–—\-])\s*(techcrunch|techmeme)\s*$",
    re.IGNORECASE,
)

# Keyword → score weight. Higher = more relevant to the AI/tech focus.
KEYWORD_WHITELIST: Dict[str, int] = {
    # AI / LLM
    "artificial intelligence": 3, "machine learning": 2,
    "llm": 3, "large language model": 3, "foundation model": 3,
    "openai": 5, "anthropic": 5, "chatgpt": 5, "claude": 5,
    "google": 5, "gemini": 4, "meta": 5, "facebook": 5,
    "gpt": 2, "llama": 2,
    "neural": 2, "inference": 2, "fine-tuning": 2, "fine tuning": 2,
    "agent": 2, "rag": 2, "multimodal": 2, "reasoning": 2,
    "transformer": 2, "diffusion": 2, "open source model": 3,
    # Infrastructure / Compute
    "semiconductor": 3, "chip": 2, "nvidia": 2, "amd": 2, "tsmc": 3,
    "quantum": 3, "gpu": 3, "tpu": 3, "datacenter": 2, "data center": 2,
    "compute": 2, "energy": 2, "power grid": 3, "nuclear": 2,
    "cloud": 1, "open source": 2,
    # Macro / Regulation
    "regulation": 2, "sec": 2, "fed": 1, "federal reserve": 2,
    "interest rate": 2, "institutional": 2, "treasury": 2,
    "congress": 2, "legislation": 2,
}

TOP_N = 40
DAYS_FILTER = 7

# Mimic a real browser so some RSS hosts don't silently return empty feeds.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def _normalize_url(link: str) -> str:
    """Stable key for cross-feed dedupe: host/path + query without tracking params."""
    raw = (link or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path or ""
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = ""
        if p.query:
            pairs = parse_qsl(p.query, keep_blank_values=True)
            kept = [
                (k, v)
                for k, v in pairs
                if k.lower() not in _TRACKING_QUERY_EXACT
                and not k.lower().startswith("utm_")
            ]
            query = urlencode(kept) if kept else ""
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return raw.lower()


def _normalize_title(title: str) -> str:
    t = " ".join((title or "").strip().lower().split())
    t = _TITLE_SUFFIX_RE.sub("", t).strip()
    return t


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _better_item(incumbent: Dict[str, Any], challenger: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer higher score, then newer published, then incumbent (feed order wins)."""
    if challenger["score"] > incumbent["score"]:
        return challenger
    if challenger["score"] < incumbent["score"]:
        return incumbent
    da = _parse_iso_datetime(incumbent.get("published", ""))
    db = _parse_iso_datetime(challenger.get("published", ""))
    if da and db:
        return incumbent if da >= db else challenger
    if da and not db:
        return incumbent
    if db and not da:
        return challenger
    return incumbent


def _parse_anthropic_ssr(html: str, cutoff: datetime) -> List[Dict[str, Any]]:
    """Extract post records from the Next.js SSR payload embedded in Anthropic page HTML.

    The page embeds data as escaped JSON inside self.__next_f.push([1,"..."]) script tags.
    Post objects always follow the field ordering: directories → illustration →
    publishedOn → slug → subjects → summary → title.
    """
    # Find the script tag that holds publishedOn data
    raw = ""
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        if "publishedOn" in script:
            raw = script
            break
    if not raw:
        return []

    # Each post in the SSR payload serialises as (after the illustration block):
    #   \"publishedOn\":\"DATE\",\"slug\":{\"_type\":\"slug\",\"current\":\"SLUG\"},
    #   \"subjects\":[...],\"summary\":\"SUMMARY\",\"title\":\"TITLE\"
    post_re = re.compile(
        r'\\"publishedOn\\":\\"([^\\"]+)\\"'               # group 1: publishedOn
        r',\\"slug\\":{\\"_type\\":\\"slug\\"'
        r',\\"current\\":\\"([^\\"]+)\\"}'                 # group 2: slug
        r'.*?'                                             # subjects array (non-greedy)
        r',\\"summary\\":\\"([^\\"]*)\\"'                  # group 3: summary
        r',\\"title\\":\\"([^\\"]+)\\"',                   # group 4: title
        re.DOTALL,
    )

    posts: List[Dict[str, Any]] = []
    seen_slugs: set = set()

    for m in post_re.finditer(raw):
        published_str, slug, summary, title = m.group(1), m.group(2), m.group(3), m.group(4)

        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        try:
            published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published_dt < cutoff:
            continue

        posts.append({"slug": slug, "published": published_str, "title": title, "summary": summary})

    return posts


def _dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two-pass dedupe: normalized URL, then normalized title (same story, different links)."""
    if not items:
        return []

    by_url: Dict[str, Dict[str, Any]] = {}
    no_url: List[Dict[str, Any]] = []
    for item in items:
        uk = _normalize_url(item.get("link", ""))
        if not uk:
            no_url.append(item)
            continue
        if uk not in by_url:
            by_url[uk] = item
        else:
            by_url[uk] = _better_item(by_url[uk], item)

    merged = list(by_url.values()) + no_url

    by_title: Dict[str, Dict[str, Any]] = {}
    no_title: List[Dict[str, Any]] = []
    for item in merged:
        tk = _normalize_title(item.get("title", ""))
        if not tk:
            no_title.append(item)
            continue
        if tk not in by_title:
            by_title[tk] = item
        else:
            by_title[tk] = _better_item(by_title[tk], item)

    return list(by_title.values()) + no_title


def _semantic_dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(items) < 2:
        return items
    titles = [item["title"] for item in items]
    prompt = (
        "You are given a list of article titles. "
        "Group titles that cover the same news event into clusters. "
        "Return ONLY a JSON array of arrays, where each inner array contains the "
        f"0-based indices of titles in the same group. "
        f"Every index 0..{len(items) - 1} must appear in exactly one group.\n\n"
        f"Titles:\n{json.dumps(titles, ensure_ascii=False)}"
    )
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        groups: List[List[int]] = json.loads(response.content[0].text)
    except Exception as e:
        logger.warning(f"Semantic dedupe LLM call failed, skipping: {e}")
        return items

    result: List[Dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        winner = items[group[0]]
        for idx in group[1:]:
            if idx < len(items):
                winner = _better_item(winner, items[idx])
        result.append(winner)
    return result


class RssFetcher(BaseFetcher):
    def __init__(self) -> None:
        # Populated during fetch(). Each source maps to a dict of stage → count:
        # fetched, in_window, relevant (unique score>0 after dedupe), sent_to_llm (after top-N cut).
        self.source_counts: Dict[str, Dict[str, int]] = {}
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        self._proxy_handler = (
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            if proxy_url else None
        )

    def fetch(self) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_FILTER)
        all_items: List[Dict[str, Any]] = []

        stage_counts: Dict[str, Dict[str, int]] = {
            source: {"fetched": 0, "in_window": 0, "relevant": 0, "sent_to_llm": 0}
            for source in list(FEEDS) + list(ANTHROPIC_SOURCES)
        }

        for source, url in FEEDS.items():
            try:
                feed = feedparser.parse(
                    url,
                    agent=USER_AGENT,
                    handlers=[self._proxy_handler] if self._proxy_handler else [],
                )
                if feed.bozo and not feed.entries:
                    logger.error(f"Error fetching feed '{source}': {feed.bozo_exception}")
                stage_counts[source]["fetched"] = len(feed.entries)
                logger.info(f"Fetched {len(feed.entries)} raw entries from {source}")

                for entry in feed.entries:
                    try:
                        published = self._parse_date(entry)
                        if published and published < cutoff:
                            continue

                        stage_counts[source]["in_window"] += 1

                        title: str = getattr(entry, "title", "") or ""
                        summary: str = getattr(entry, "summary", "") or ""
                        link: str = getattr(entry, "link", "") or ""
                        score = self._score_item(title, summary)

                        all_items.append({
                            "title": title,
                            "summary": summary,
                            "link": link,
                            "source": source,
                            "published": published.isoformat() if published else "",
                            "score": score,
                        })
                    except Exception as e:
                        logger.error(f"Error parsing entry from {source}: {e}")

            except Exception as e:
                logger.error(f"Error fetching feed '{source}' ({url}): {e}")

        try:
            anthropic_items = self._fetch_anthropic(cutoff, stage_counts)
            all_items.extend(anthropic_items)
        except Exception as e:
            logger.error(f"Error fetching Anthropic content: {e}")

        before_dedupe = len(all_items)
        all_items = _dedupe_items(all_items)
        logger.info(f"Dedupe: {before_dedupe} -> {len(all_items)} items")

        before_semantic = len(all_items)
        all_items = _semantic_dedupe(all_items)
        logger.info(f"Semantic dedupe: {before_semantic} -> {len(all_items)} items")

        for s in stage_counts:
            stage_counts[s]["relevant"] = 0
        for item in all_items:
            if item["score"] > 0:
                stage_counts[item["source"]]["relevant"] += 1

        all_items = [item for item in all_items if item["score"] > 0]
        all_items.sort(key=lambda x: x["score"], reverse=True)
        top_items = all_items[:TOP_N]

        for item in top_items:
            stage_counts[item["source"]]["sent_to_llm"] += 1

        logger.info(
            f"Scored {len(all_items)} total items across all feeds; "
            f"keeping top {len(top_items)} (score threshold: "
            f"{top_items[-1]['score'] if top_items else 'n/a'})"
        )
        for item in top_items:
            logger.debug(
                f"  [{item['score']:>3}] [{item['source'].upper()}] {item['title']}"
            )

        self.source_counts = stage_counts
        return top_items

    def _parse_date(self, entry: Any) -> Optional[datetime]:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
        if hasattr(entry, "updated_parsed") and entry.updated_parsed:
            try:
                return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
        return None

    def _fetch_anthropic(
        self,
        cutoff: datetime,
        stage_counts: Dict[str, Dict[str, int]],
    ) -> List[Dict[str, Any]]:
        """Fetch recent Anthropic news and research posts by scraping the SSR page HTML.

        The Sanity GROQ public API no longer returns data (dataset is now private).
        Both pages embed post records as escaped JSON in Next.js SSR script tags.
        """
        proxies: Dict[str, str] = {}
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}

        items: List[Dict[str, Any]] = []

        for source, url in ANTHROPIC_PAGES.items():
            directory = source.replace("anthropic_", "")  # "news" or "research"
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    proxies=proxies or None,
                    timeout=15,
                )
                resp.raise_for_status()
                posts = _parse_anthropic_ssr(resp.text, cutoff)

                for post in posts:
                    slug: str = post["slug"]
                    title: str = post["title"]
                    summary: str = post["summary"]
                    published: str = post["published"]
                    link = f"https://www.anthropic.com/{directory}/{slug}"

                    stage_counts[source]["fetched"] += 1
                    stage_counts[source]["in_window"] += 1

                    score = self._score_item(title, summary)
                    items.append({
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "source": source,
                        "published": published,
                        "score": score,
                    })
            except Exception as e:
                logger.error(f"Error fetching Anthropic {directory} page: {e}")

        logger.info(
            f"Fetched {stage_counts['anthropic_news']['fetched']} raw entries from anthropic_news, "
            f"{stage_counts['anthropic_research']['fetched']} from anthropic_research"
        )
        return items

    def _score_item(self, title: str, summary: str) -> int:
        text = (title + " " + summary).lower()
        return sum(weight for kw, weight in KEYWORD_WHITELIST.items() if kw in text)
