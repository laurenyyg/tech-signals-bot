import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import feedparser
import requests
from bs4 import BeautifulSoup, SoupStrainer

from common import USER_AGENT, proxies_from_env
from config import A16Z_SKIP_TAGS, A16Z_SKIP_TITLE_PREFIXES, A16Z_SKIP_TITLE_SUBSTRINGS
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

# One day longer than the weekly cadence: GitHub's scheduled runs start hours late.
DAYS_FILTER = 8

DEEPLEARNING_AI_URL = "https://www.deeplearning.ai/the-batch/tag/letters/"
DEEPLEARNING_AI_BASE = "https://www.deeplearning.ai"

BEN_EVANS_URL = "https://www.ben-evans.com/"
BEN_EVANS_BASE = "https://www.ben-evans.com"

A16Z_ARCHIVE_URL = "https://www.a16z.news/api/v1/archive?sort=new&limit=25"

ONE_USEFUL_THING_URL = "https://www.oneusefulthing.org/feed"

# Funnel rows, in display order. Stratechery and Ben Evans each split into two feeds.
FEEDS: Tuple[str, ...] = (
    "stratechery",
    "stratechery-podcasts",
    "deeplearning-ai",
    "ben-evans-essays",
    "ben-evans-presentations",
    "a16z",
    "one-useful-thing",
)

Item = Dict[str, Any]

# Day-first ("3 September 2026") is Ben Evans's current format; month-first is legacy.
_DAY_DATE_FORMATS = ("%d %B %Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y")
_MONTH_DATE_FORMATS = ("%b %Y", "%B %Y")


# ---------------------------------------------------------------------------
# Parsing helpers (pure; unit-tested on mocked strings)
# ---------------------------------------------------------------------------

def parse_human_date_range(date_str: str) -> Optional[Tuple[datetime, datetime, bool]]:
    """Parse a human date into (start, end, month_only), UTC, end exclusive.

    '3 September 2026' → that day; 'Sept 2026' / 'Nov 2025' → the whole month,
    since the actual publish day is unknown.
    """
    cleaned = re.sub(r"\bSept\b", "Sep", " ".join(date_str.split()))
    for fmt in _DAY_DATE_FORMATS:
        try:
            start = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            return start, start + timedelta(days=1), False
        except ValueError:
            pass
    for fmt in _MONTH_DATE_FORMATS:
        try:
            start = datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            return start, next_month, True
        except ValueError:
            pass
    return None


def parse_human_date(date_str: str) -> Optional[datetime]:
    """Start of the date parsed by parse_human_date_range (month-only → the 1st)."""
    parsed = parse_human_date_range(date_str)
    return parsed[0] if parsed else None


def a16z_skip_reason(post: Dict[str, Any]) -> Optional[str]:
    """Why an a16z post is an announcement rather than an essay (None = keep it)."""
    title = (post.get("title") or "").strip().lower()
    tags = {
        t.get("name") for t in (post.get("postTags") or []) if isinstance(t, dict)
    }
    for tag in A16Z_SKIP_TAGS:
        if tag in tags:
            return f"tagged {tag}"
    for prefix in A16Z_SKIP_TITLE_PREFIXES:
        if title.startswith(prefix):
            return f"title starts with {prefix.strip()!r}"
    for substring in A16Z_SKIP_TITLE_SUBSTRINGS:
        if substring in title:
            return f"title mentions {substring!r}"
    return None


def parse_iso_date(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (trailing 'Z' allowed) as UTC; None if invalid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_date(published: Optional[datetime], raw: str) -> datetime:
    """Undated items can't be windowed, so they're treated as parse errors."""
    if published is None:
        raise ValueError(f"unparsable date {raw!r}")
    return published


def _make_item(
    title: str,
    author: str,
    summary: str,
    link: str,
    source: str,
    content_type: str,
    published: datetime,
    month_only: bool = False,
) -> Item:
    item: Item = {
        "title": title,
        "author": author,
        "summary": summary,
        "link": link,
        "source": source,
        "content_type": content_type,
        "published": published.isoformat(),
    }
    if month_only:
        item["date_precision"] = "month"
    return item


def _require_range(raw: str) -> Tuple[datetime, datetime, bool]:
    parsed = parse_human_date_range(raw)
    if parsed is None:
        raise ValueError(f"unparsable date {raw!r}")
    return parsed


def parse_entries(
    entries: List[Any], parse_one: Callable[[Any], Optional[Item]], label: str
) -> List[Item]:
    """Apply parse_one to each entry; None means skip (e.g. out of window).

    A bad entry is logged and skipped, but if every entry fails the source is
    considered broken and ValueError is raised so the caller can surface it.
    """
    items: List[Item] = []
    errors = 0
    first_error: Optional[Exception] = None
    for entry in entries:
        try:
            item = parse_one(entry)
        except Exception as e:
            errors += 1
            first_error = first_error or e
            logger.warning(f"{label}: skipping entry: {e}")
            continue
        if item is not None:
            items.append(item)
    if entries and errors == len(entries):
        raise ValueError(f"all {errors} entries failed to parse; first error: {first_error}")
    return items


def _entry_date(entry: Any) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def parse_rss_feed(
    feed: Any,
    cutoff: datetime,
    source: str,
    author: str,
    content_type: str,
    stats: Optional[Dict[str, int]] = None,
) -> List[Item]:
    """Convert a feedparser result into items published on/after cutoff."""
    entries = list(getattr(feed, "entries", []) or [])
    if not entries:
        reason = getattr(feed, "bozo_exception", None)
        raise ValueError(f"feed returned no entries{f' ({reason})' if reason else ''}")
    if stats is not None:
        stats["fetched"] = len(entries)

    def parse_one(entry: Any) -> Optional[Item]:
        published = _require_date(_entry_date(entry), getattr(entry, "published", ""))
        if published < cutoff:
            return None
        title: str = getattr(entry, "title", "") or ""
        if not title:
            raise ValueError("entry has no title")
        # Prefer the full body over the one-sentence teaser.
        content_list = getattr(entry, "content", None)
        if content_list:
            summary: str = content_list[0].get("value", "") or ""
        else:
            summary = getattr(entry, "summary", "") or ""
        item = _make_item(
            title, author, summary, getattr(entry, "link", "") or "",
            source, content_type, published,
        )
        # WordPress guids are permanent per post, unlike links (?p=ID drafts later get slugs).
        guid: str = getattr(entry, "id", "") or ""
        if guid:
            item["guid"] = guid
        return item

    return parse_entries(entries, parse_one, source)


def _deeplearning_ai_date(article: Any) -> Tuple[datetime, datetime]:
    """(published, window end) from <time datetime> (legacy) or the footer's 'November 6, 2026'."""
    time_tag = article.find("time")
    if time_tag and time_tag.get("datetime"):
        raw: str = time_tag["datetime"]
        published = _require_date(parse_iso_date(raw), raw)
        # Date-only values parse as midnight; treat them as covering the whole day.
        if (published.hour, published.minute, published.second) == (0, 0, 0):
            return published, published + timedelta(days=1)
        return published, published
    footer = article.find("footer")
    span = footer.find("span") if footer else None
    raw = span.get_text(strip=True) if span else ""
    published, end, _ = _require_range(raw)
    return published, end


def parse_deeplearning_ai_page(
    html: str, cutoff: datetime, stats: Optional[Dict[str, int]] = None
) -> List[Item]:
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer("article"))
    articles = soup.find_all("article")
    if not articles:
        raise ValueError("no <article> cards found (page layout changed?)")
    logger.info(f"Scraped {len(articles)} article cards from deeplearning-ai")
    if stats is not None:
        stats["fetched"] = len(articles)

    def parse_one(article: Any) -> Optional[Item]:
        heading = article.find(["h2", "h3"])
        title: str = heading.get_text(strip=True) if heading else ""
        if not title:
            raise ValueError("card has no h2/h3 title")
        published, end = _deeplearning_ai_date(article)
        if end <= cutoff:
            return None
        excerpt_div = article.find(class_=lambda c: c and "line-clamp" in c)
        # The first link is often the "Letters" tag badge; the card link is the other one.
        a_tag = next(
            (a for a in article.find_all("a", href=True) if "/tag/" not in a["href"]), None
        )
        href: str = a_tag["href"] if a_tag else ""
        return _make_item(
            title,
            "Andrew Ng",
            excerpt_div.get_text(strip=True) if excerpt_div else "",
            (DEEPLEARNING_AI_BASE + href) if href.startswith("/") else href,
            "deeplearning-ai",
            "essay",
            published,
        )

    return parse_entries(articles, parse_one, "deeplearning-ai")


def parse_ben_evans_page(
    html: str,
    cutoff: datetime,
    essay_stats: Optional[Dict[str, int]] = None,
    presentation_stats: Optional[Dict[str, int]] = None,
) -> Tuple[List[Item], List[Item]]:
    """Return (essays, presentations) from the ben-evans.com homepage."""
    soup = BeautifulSoup(
        html, "html.parser",
        parse_only=SoupStrainer(
            class_=lambda c: c and ("summary-item" in c or "PageRight" in c)
        ),
    )
    cards = soup.find_all("div", class_="summary-item")
    if not cards:
        raise ValueError("no essay cards found (page layout changed?)")
    logger.info(f"Scraped {len(cards)} essay cards from ben-evans")
    blocks = soup.find_all("div", class_="PageRight")
    if essay_stats is not None:
        essay_stats["fetched"] = len(cards)
    if presentation_stats is not None:
        presentation_stats["fetched"] = len(blocks)

    def parse_essay(card: Any) -> Optional[Item]:
        title_a = card.find("a", class_="summary-title-link")
        if not title_a or not title_a.get_text(strip=True):
            raise ValueError("essay card has no title link")
        date_tag = card.find(class_="summary-metadata-item--date")
        raw_date: str = date_tag.get_text(strip=True) if date_tag else ""
        published, end, month_only = _require_range(raw_date)
        if end <= cutoff:
            return None
        href: str = title_a.get("href", "")
        excerpt = card.find(class_="summary-excerpt")
        return _make_item(
            title_a.get_text(strip=True),
            "Benedict Evans",
            excerpt.get_text(strip=True) if excerpt else "",
            (BEN_EVANS_BASE + href) if href.startswith("/") else href,
            "ben-evans",
            "essay",
            published,
            month_only,
        )

    def parse_presentation(block: Any) -> Optional[Item]:
        h2 = block.find("h2")
        # Newsletter/podcast blocks have no h2 or an h2 wrapping a link.
        if not h2 or h2.find("a"):
            return None
        title = h2.get_text(strip=True)
        if not title:
            return None
        eyebrow = block.find(class_="PageRightEyebrow")
        raw_date = eyebrow.get_text(strip=True) if eyebrow else ""
        published, end, month_only = _require_range(raw_date)
        if end <= cutoff:
            return None
        body = block.find(class_="pagerightbody")
        link_tag = block.find("a", href=True)
        href = link_tag["href"] if link_tag else ""
        return _make_item(
            title,
            "Benedict Evans",
            body.get_text(strip=True) if body else "",
            (BEN_EVANS_BASE + href) if href.startswith("/") else href,
            "ben-evans",
            "presentation",
            published,
            month_only,
        )

    essays = parse_entries(cards, parse_essay, "ben-evans-essays")
    presentations = parse_entries(blocks, parse_presentation, "ben-evans-presentations")
    return essays, presentations


def parse_a16z_archive(
    payload: Any, cutoff: datetime, stats: Optional[Dict[str, int]] = None
) -> List[Item]:
    """Parse the a16z archive API: {"posts": [...], "nextCursor": ...} (or a bare list)."""
    if isinstance(payload, dict):
        posts = payload.get("posts")
    else:
        posts = payload
    if not isinstance(posts, list):
        raise ValueError(
            f"unexpected archive response shape: {type(payload).__name__} "
            f"with keys {sorted(payload)[:5] if isinstance(payload, dict) else 'n/a'}"
        )
    if not posts:
        raise ValueError("archive returned no posts")
    logger.info(f"Fetched {len(posts)} posts from a16z archive")
    filtered = 0

    def parse_one(post: Any) -> Optional[Item]:
        nonlocal filtered
        if not isinstance(post, dict):
            raise ValueError(f"post is {type(post).__name__}, expected object")
        title: str = post.get("title", "") or ""
        if not title:
            raise ValueError("post has no title")
        raw_date: str = post.get("post_date", "") or ""
        published = _require_date(parse_iso_date(raw_date), raw_date)
        if published < cutoff:
            return None
        skip_reason = a16z_skip_reason(post)
        if skip_reason:
            logger.info(f"a16z: skipping {title!r} ({skip_reason})")
            filtered += 1
            return None
        bylines = post.get("publishedBylines") or []
        first = bylines[0] if bylines and isinstance(bylines[0], dict) else {}
        summary: str = post.get("truncated_body_text") or post.get("description") or ""
        return _make_item(
            title,
            first.get("name") or "a16z",
            summary,
            post.get("canonical_url", "") or "",
            "a16z",
            "essay",
            published,
        )

    items = parse_entries(posts, parse_one, "a16z")
    if stats is not None:
        stats["fetched"] = len(posts)
        stats["filtered"] = filtered
    return items


def dedupe_podcasts(articles: List[Item], podcasts: List[Item]) -> List[Item]:
    """Drop podcast episodes whose title matches an article (articles win)."""
    article_titles = {item["title"].lower() for item in articles}
    return [p for p in podcasts if p["title"].lower() not in article_titles]


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class WhyFetcher(BaseFetcher):
    """Fetches Why-profile items.

    funnel[feed] holds fetched / in_window / filtered counts (plus fetch_failed);
    source_errors[source] holds the error for each source that failed. Every item
    gets a "feed" key matching its funnel entry.
    """

    def __init__(self, cutoff: Optional[datetime] = None) -> None:
        self.funnel: Dict[str, Dict[str, int]] = {feed: {} for feed in FEEDS}
        self.source_errors: Dict[str, str] = {}
        self.cutoff = cutoff if cutoff is not None else (
            datetime.now(timezone.utc) - timedelta(days=DAYS_FILTER)
        )
        self._proxies = proxies_from_env()
        self._proxy_handler = (
            urllib.request.ProxyHandler(self._proxies) if self._proxies else None
        )

    def fetch(self) -> List[Item]:
        cutoff = self.cutoff
        f = self.funnel

        articles = self._guarded("stratechery", ["stratechery"], lambda: self._fetch_rss(
            os.getenv("STRATECHERY_RSS_URL"), "STRATECHERY_RSS_URL",
            cutoff, "stratechery", "Ben Thompson", "essay", f["stratechery"],
        ))
        podcasts = self._guarded("stratechery-podcasts", ["stratechery-podcasts"], lambda: self._fetch_rss(
            os.getenv("STRATECHERY_PODCAST_URL"), "STRATECHERY_PODCAST_URL",
            cutoff, "stratechery", "Ben Thompson", "podcast", f["stratechery-podcasts"],
        ))
        deduped = dedupe_podcasts(articles, podcasts)
        f["stratechery-podcasts"]["filtered"] = len(podcasts) - len(deduped)
        deeplearning = self._guarded("deeplearning-ai", ["deeplearning-ai"], lambda: parse_deeplearning_ai_page(
            self._get(DEEPLEARNING_AI_URL).text, cutoff, f["deeplearning-ai"],
        ))
        essays, presentations = self._guarded(
            "ben-evans",
            ["ben-evans-essays", "ben-evans-presentations"],
            lambda: parse_ben_evans_page(
                self._get(BEN_EVANS_URL).text, cutoff,
                f["ben-evans-essays"], f["ben-evans-presentations"],
            ),
            default=([], []),
        )
        a16z = self._guarded("a16z", ["a16z"], lambda: parse_a16z_archive(
            self._get(A16Z_ARCHIVE_URL).json(), cutoff, f["a16z"],
        ))
        one_useful_thing = self._guarded("one-useful-thing", ["one-useful-thing"], lambda: self._fetch_rss(
            ONE_USEFUL_THING_URL, None, cutoff, "one-useful-thing", "Ethan Mollick", "essay",
            f["one-useful-thing"],
        ))

        by_feed = {
            "stratechery": articles,
            "stratechery-podcasts": deduped,
            "deeplearning-ai": deeplearning,
            "ben-evans-essays": essays,
            "ben-evans-presentations": presentations,
            "a16z": a16z,
            "one-useful-thing": one_useful_thing,
        }
        items: List[Item] = []
        for feed, feed_items in by_feed.items():
            f[feed]["in_window"] = len(feed_items) + f[feed].get("filtered", 0)
            for item in feed_items:
                item["feed"] = feed
            items.extend(feed_items)
            logger.info(f"{feed}: {len(feed_items)} item(s) in window")

        days = max(1, (datetime.now(timezone.utc) - cutoff).days)
        logger.info(
            f"WhyFetcher: {len(items)} items since {cutoff.date()} ({days}d window); "
            f"{len(self.source_errors)} source(s) failed"
        )
        return items

    def _guarded(
        self, source: str, feeds: List[str], fn: Callable[[], Any], default: Any = None
    ) -> Any:
        try:
            return fn()
        except Exception as e:
            logger.error(f"Error fetching {source}: {e}")
            self.source_errors[source] = str(e)
            for feed in feeds:
                self.funnel[feed]["fetch_failed"] = 1
            return [] if default is None else default

    def _get(self, url: str) -> requests.Response:
        r = requests.get(
            url, headers={"User-Agent": USER_AGENT}, proxies=self._proxies, timeout=15,
        )
        r.raise_for_status()
        return r

    def _fetch_rss(
        self,
        url: Optional[str],
        env_name: Optional[str],
        cutoff: datetime,
        source: str,
        author: str,
        content_type: str,
        stats: Dict[str, int],
    ) -> List[Item]:
        if not url:
            raise EnvironmentError(f"{env_name} is not set")
        feed = feedparser.parse(
            url,
            agent=USER_AGENT,
            handlers=[self._proxy_handler] if self._proxy_handler else [],
        )
        logger.info(f"Fetched {len(feed.entries)} entries from {source} ({content_type}) RSS")
        return parse_rss_feed(feed, cutoff, source, author, content_type, stats)
