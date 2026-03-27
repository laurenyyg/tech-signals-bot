import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import feedparser

from config import HOW_NEGATIVE_KEYWORDS, HOW_POSITIVE_KEYWORDS, SSPAI_WHITELIST_AUTHORS
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

FEED_URL = "https://sspai.com/feed"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class SSPAIFetcher(BaseFetcher):
    def __init__(self) -> None:
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
            logger.error(f"[SSPAIFetcher] fetch error: {e}")
            return []

        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=1)

        items: List[Dict[str, Any]] = []
        for entry in feed.entries:
            try:
                title: str = getattr(entry, "title", "") or ""
                summary: str = getattr(entry, "summary", "") or ""
                author: str = getattr(entry, "author", "") or ""
                text = title + " " + summary

                if author in SSPAI_WHITELIST_AUTHORS:
                    keep = True
                elif any(kw in text for kw in HOW_NEGATIVE_KEYWORDS):
                    keep = False
                elif any(kw in text for kw in HOW_POSITIVE_KEYWORDS):
                    keep = True
                else:
                    keep = False

                if not keep:
                    continue

                published = self._parse_date(entry)
                if published is None or published < cutoff:
                    continue

                items.append({
                    "title": title,
                    "link": getattr(entry, "link", "") or "",
                    "summary": summary,
                    "author": author,
                    "published": published.isoformat() if published else "",
                    "source": "SSPAI",
                })
            except Exception as e:
                logger.warning(f"[SSPAIFetcher] skipping entry due to error: {e}")

        logger.info(f"SSPAIFetcher: {len(items)} items passed filter")
        return items

    def _parse_date(self, entry: Any) -> Optional[datetime]:
        for attr in ("published_parsed", "updated_parsed"):
            parsed = getattr(entry, attr, None)
            if parsed:
                try:
                    return datetime(*parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
        return None
