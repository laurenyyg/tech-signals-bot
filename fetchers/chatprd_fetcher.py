import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://www.chatprd.ai"
TARGET_URL = f"{BASE_URL}/how-i-ai"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def _cutoff() -> datetime:
    """Return midnight UTC yesterday (i.e. keep today and yesterday)."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).replace(
        day=now.day - 1
    )


class ChatPRDFetcher(BaseFetcher):
    def __init__(self) -> None:
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        self._proxies: Optional[Dict[str, str]] = (
            {"http": proxy_url, "https": proxy_url} if proxy_url else None
        )

    def fetch(self) -> List[Dict[str, Any]]:
        try:
            r = requests.get(
                TARGET_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=self._proxies,
                timeout=15,
            )
            r.raise_for_status()
        except Exception as e:
            logger.error(f"[ChatPRDFetcher] fetch error: {e}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")

        # Prefer the JSON-LD BlogPosting list — richer and more reliable than DOM scraping.
        ld_script = soup.find("script", type="application/ld+json")
        if not ld_script:
            logger.error("[ChatPRDFetcher] JSON-LD not found; page structure may have changed")
            return []

        try:
            ld_data = json.loads(ld_script.string)
            posts = ld_data.get("blogPost", [])
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"[ChatPRDFetcher] failed to parse JSON-LD: {e}")
            return []

        cutoff = _cutoff()
        items: List[Dict[str, Any]] = []
        for post in posts:
            try:
                date_str: str = post.get("datePublished", "")
                if date_str:
                    published = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if published < cutoff:
                        continue
                else:
                    published = None

                url: str = post.get("url", "")
                if url and not url.startswith("http"):
                    url = BASE_URL + url

                title: str = post.get("headline", "").strip()
                if not title or not url:
                    continue

                items.append({
                    "title": title,
                    "link": url,
                    "summary": post.get("description", ""),
                    "published": published.isoformat() if published else "",
                    "source": "ChatPRD",
                })
            except Exception as e:
                logger.warning(f"[ChatPRDFetcher] skipping post due to error: {e}")

        logger.info(f"ChatPRDFetcher: {len(items)} items within date window")
        return items
