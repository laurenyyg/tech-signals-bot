import logging
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Pattern

import feedparser

from common import USER_AGENT
from config import (
    HOW_DAYS_FILTER,
    HOW_NEGATIVE_KEYWORDS,
    HOW_POSITIVE_KEYWORDS,
    SSPAI_WHITELIST_AUTHORS,
)
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

FEED_URL = "https://sspai.com/feed"


def _keyword_pattern(keyword: str) -> Pattern[str]:
    """Latin keywords match as whole words (so "AI" doesn't hit "PAID"); CJK as substrings."""
    escaped = re.escape(keyword)
    if keyword.isascii():
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped)


_POSITIVE_PATTERNS: List[Pattern[str]] = [_keyword_pattern(k) for k in HOW_POSITIVE_KEYWORDS]
_NEGATIVE_PATTERNS: List[Pattern[str]] = [_keyword_pattern(k) for k in HOW_NEGATIVE_KEYWORDS]


def passes_how_filter(title: str, summary: str, author: str) -> bool:
    """Author whitelist wins; otherwise no negative keyword and at least one positive."""
    if author in SSPAI_WHITELIST_AUTHORS:
        return True
    text = f"{title} {summary}"
    if any(p.search(text) for p in _NEGATIVE_PATTERNS):
        return False
    return any(p.search(text) for p in _POSITIVE_PATTERNS)


def _parse_date(entry: Any) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def parse_sspai_feed(feed: Any, cutoff: datetime) -> List[Dict[str, Any]]:
    """Filter a parsed SSPAI feed into backlog items published on or after cutoff."""
    items: List[Dict[str, Any]] = []
    for entry in feed.entries:
        try:
            title: str = getattr(entry, "title", "") or ""
            summary: str = getattr(entry, "summary", "") or ""
            author: str = getattr(entry, "author", "") or ""
            link: str = getattr(entry, "link", "") or ""

            if not link or not passes_how_filter(title, summary, author):
                continue

            published = _parse_date(entry)
            if published is None or published < cutoff:
                continue

            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "author": author,
                "published": published.isoformat(),
                "source": "SSPAI",
            })
        except Exception as e:
            logger.warning(f"[SSPAIFetcher] skipping entry due to error: {e}")
    return items


class SSPAIFetcher(BaseFetcher):
    """Fetches recent SSPAI posts that pass the How filter. Raises on feed failure."""

    def __init__(self, days: int = HOW_DAYS_FILTER) -> None:
        self._days = days
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        self._proxy_handler: Optional[urllib.request.ProxyHandler] = (
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            if proxy_url else None
        )

    def fetch(self) -> List[Dict[str, Any]]:
        try:
            feed = feedparser.parse(
                FEED_URL,
                agent=USER_AGENT,
                handlers=[self._proxy_handler] if self._proxy_handler else [],
            )
        except Exception as e:
            raise RuntimeError(f"[SSPAIFetcher] fetch error: {e}") from e

        if not feed.entries:
            reason = getattr(feed, "bozo_exception", None) or "feed returned no entries"
            raise RuntimeError(f"[SSPAIFetcher] feed unusable: {reason}")

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)
        items = parse_sspai_feed(feed, cutoff)
        logger.info(
            f"SSPAIFetcher: {len(items)}/{len(feed.entries)} entries passed filter "
            f"within {self._days}-day window"
        )
        return items
