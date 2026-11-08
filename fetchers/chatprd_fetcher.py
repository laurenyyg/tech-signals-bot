import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

import requests
from bs4 import BeautifulSoup

from common import USER_AGENT, proxies_from_env
from config import HOW_DAYS_FILTER
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://www.chatprd.ai"
TARGET_URL = f"{BASE_URL}/how-i-ai"


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


class ChatPRDFetcher(BaseFetcher):
    """Fetches recent How I AI episodes. Raises on network or structural failure."""

    def __init__(self, days: int = HOW_DAYS_FILTER) -> None:
        self._days = days

    def fetch(self) -> List[Dict[str, Any]]:
        try:
            r = requests.get(
                TARGET_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=proxies_from_env(),
                timeout=15,
            )
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"[ChatPRDFetcher] fetch error: {e}") from e

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)
        items = parse_chatprd_page(r.text, cutoff)
        logger.info(f"ChatPRDFetcher: {len(items)} items within {self._days}-day window")
        return items
