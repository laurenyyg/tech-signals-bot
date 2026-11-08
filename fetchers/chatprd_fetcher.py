import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests
from bs4 import BeautifulSoup

from common import USER_AGENT, proxies_from_env
from config import HOW_BODY_MAX_CHARS, HOW_DAYS_FILTER
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://www.chatprd.ai"
TARGET_URL = f"{BASE_URL}/how-i-ai"
WORKFLOW_PATH_RE = re.compile(r"^(?:https://www\.chatprd\.ai)?(/how-i-ai/workflows/[A-Za-z0-9\-]+)/?$")


def _iter_ld_nodes(soup: BeautifulSoup) -> Iterator[Dict[str, Any]]:
    """Yield every top-level JSON-LD object on the page (skipping unparsable scripts)."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict):
                yield node


def _extract_posts(html: str) -> List[Dict[str, Any]]:
    """Return raw episode/post dicts from the page's JSON-LD.

    Current shape: PodcastSeries.hasPart[PodcastEpisode]. Legacy shape: Blog.blogPost.
    Raises RuntimeError if neither is present, so a site redesign fails loudly.
    """
    soup = BeautifulSoup(html, "html.parser")
    for node in _iter_ld_nodes(soup):
        if node.get("@type") == "PodcastSeries" and isinstance(node.get("hasPart"), list):
            return node["hasPart"]
        if isinstance(node.get("blogPost"), list):
            return node["blogPost"]
    raise RuntimeError(
        "ChatPRD JSON-LD has no PodcastSeries.hasPart or blogPost list; "
        "page structure may have changed"
    )


def parse_chatprd_page(html: str, cutoff: datetime) -> List[Dict[str, Any]]:
    """Parse the How I AI listing into backlog items published on or after cutoff."""
    items: List[Dict[str, Any]] = []
    for post in _extract_posts(html):
        try:
            date_str: str = post.get("datePublished", "") or ""
            published: Optional[datetime] = (
                datetime.fromisoformat(date_str.replace("Z", "+00:00")) if date_str else None
            )
            if published is None or published < cutoff:
                continue

            url: str = post.get("url", "") or ""
            if url and not url.startswith("http"):
                url = BASE_URL + url

            title: str = (post.get("name") or post.get("headline") or "").strip()
            if not title or not url:
                continue

            items.append({
                "title": title,
                "link": url,
                "summary": post.get("description", "") or "",
                "published": published.isoformat(),
                "source": "ChatPRD",
            })
        except Exception as e:
            logger.warning(f"[ChatPRDFetcher] skipping post due to error: {e}")
    return items


def extract_workflow_links(episode_html: str) -> List[str]:
    """Return absolute URLs of the per-workflow pages linked from an episode page."""
    soup = BeautifulSoup(episode_html, "html.parser")
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        match = WORKFLOW_PATH_RE.match(a["href"])
        if match:
            url = BASE_URL + match.group(1)
            if url not in links:
                links.append(url)
    return links


def _names(values: Any) -> List[str]:
    """Normalise a schema.org tool/supply list (strings or {name: ...} dicts) to names."""
    if not isinstance(values, list):
        values = [values] if values else []
    names: List[str] = []
    for v in values:
        name = v.get("name", "") if isinstance(v, dict) else str(v)
        if name.strip():
            names.append(name.strip())
    return names


def parse_howto_page(html: str) -> Optional[Dict[str, Any]]:
    """Parse a workflow page's schema.org HowTo JSON-LD.

    Returns {title, summary, published, body} with body rendered as plain text
    (tools, inputs, numbered steps, outcome), or None if no HowTo is present.
    """
    soup = BeautifulSoup(html, "html.parser")
    howto = next((n for n in _iter_ld_nodes(soup) if n.get("@type") == "HowTo"), None)
    if howto is None:
        return None

    title: str = (howto.get("name") or "").strip()
    description: str = (howto.get("description") or "").strip()
    lines: List[str] = [title, description, ""]

    tools = _names(howto.get("tool"))
    if tools:
        lines.append(f"Tools: {', '.join(tools)}")
    supplies = _names(howto.get("supply"))
    if supplies:
        lines.append("Inputs:")
        lines += [f"- {s}" for s in supplies]

    steps = howto.get("step") or []
    if isinstance(steps, list) and steps:
        lines.append("Steps:")
        for i, step in enumerate(steps, 1):
            if isinstance(step, dict):
                name = (step.get("name") or "").strip()
                text = (step.get("text") or "").strip()
                lines.append(f"{i}. {name}: {text}" if name else f"{i}. {text}")
            else:
                lines.append(f"{i}. {step}")

    yield_ = howto.get("yield")
    if isinstance(yield_, str) and yield_.strip():
        lines.append(f"Outcome: {yield_.strip()}")

    return {
        "title": title,
        "summary": description,
        "published": howto.get("datePublished", "") or "",
        "body": "\n".join(lines).strip()[:HOW_BODY_MAX_CHARS],
    }


class ChatPRDFetcher(BaseFetcher):
    """Fetches per-workflow HowTo pages linked from recent How I AI episodes.

    Episodes without workflow links are returned as episode items (their body is
    extracted later by the catcher). Raises on listing-page network or structural
    failure; individual episode/workflow page failures are logged and skipped.
    """

    def __init__(self, days: int = HOW_DAYS_FILTER) -> None:
        self._days = days

    def _get(self, url: str) -> str:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            proxies=proxies_from_env(),
            timeout=15,
        )
        r.raise_for_status()
        return r.text

    def _workflow_items(self, episode: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            links = extract_workflow_links(self._get(episode["link"]))
        except Exception as e:
            logger.warning(f"[ChatPRDFetcher] episode page failed for {episode['link']}: {e}")
            return [episode]
        if not links:
            return [episode]

        items: List[Dict[str, Any]] = []
        for url in links:
            try:
                parsed = parse_howto_page(self._get(url))
            except Exception as e:
                logger.warning(f"[ChatPRDFetcher] workflow page failed for {url}: {e}")
                continue
            if parsed is None or not parsed["title"]:
                logger.warning(f"[ChatPRDFetcher] no HowTo JSON-LD on {url}; skipping")
                continue
            items.append({
                "title": parsed["title"],
                "link": url,
                "summary": parsed["summary"],
                "published": parsed["published"] or episode["published"],
                "source": "ChatPRD",
                "episode": episode["link"],
                "body": parsed["body"],
            })
        return items or [episode]

    def fetch(self) -> List[Dict[str, Any]]:
        try:
            listing = self._get(TARGET_URL)
        except Exception as e:
            raise RuntimeError(f"[ChatPRDFetcher] fetch error: {e}") from e

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)
        episodes = parse_chatprd_page(listing, cutoff)

        items: List[Dict[str, Any]] = []
        for episode in episodes:
            items.extend(self._workflow_items(episode))
        logger.info(
            f"ChatPRDFetcher: {len(episodes)} episode(s) within {self._days}-day window "
            f"→ {len(items)} item(s)"
        )
        return items
