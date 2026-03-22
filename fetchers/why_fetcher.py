import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup, SoupStrainer

from config import DEEP_READ_KEYWORDS
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

STRATECHERY_URL = "https://stratechery.com/category/articles/"

DAYS_FILTER = 7

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

DEEPLEARNING_AI_URL = "https://www.deeplearning.ai/the-batch/tag/letters/"
DEEPLEARNING_AI_BASE = "https://www.deeplearning.ai"

BEN_EVANS_URL = "https://www.ben-evans.com/"
BEN_EVANS_BASE = "https://www.ben-evans.com"


class WhyFetcher(BaseFetcher):
    def __init__(self) -> None:
        self.source_counts: Dict[str, int] = {
            "stratechery": 0,
            "deeplearning-ai": 0,
            "ben-evans-essays": 0,
            "ben-evans-presentations": 0,
        }
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def fetch(self) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_FILTER)
        items: List[Dict[str, Any]] = []

        strat_items = self._scrape_stratechery(cutoff)
        self.source_counts["stratechery"] = len(strat_items)
        items.extend(strat_items)

        dl_items = self._scrape_deeplearning_ai(cutoff)
        self.source_counts["deeplearning-ai"] = len(dl_items)
        items.extend(dl_items)

        essay_items, presentation_items = self._scrape_ben_evans(cutoff)
        self.source_counts["ben-evans-essays"] = len(essay_items)
        self.source_counts["ben-evans-presentations"] = len(presentation_items)
        items.extend(essay_items)
        items.extend(presentation_items)

        logger.info(f"WhyFetcher: {len(items)} items in the last {DAYS_FILTER} days")
        return items

    def _scrape_stratechery(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            r = requests.get(
                STRATECHERY_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=self._proxies,
                timeout=15,
            )
            r.raise_for_status()
            soup = BeautifulSoup(
                r.text, "html.parser",
                parse_only=SoupStrainer("li", class_="wp-block-post"),
            )
            posts = soup.find_all("li", class_="wp-block-post")
            logger.info(f"Scraped {len(posts)} article cards from stratechery")

            for post in posts:
                try:
                    title_a = post.find("h2", class_="wp-block-post-title")
                    if not title_a:
                        continue
                    a_tag = title_a.find("a", href=True)
                    title: str = title_a.get_text(strip=True)
                    link: str = a_tag["href"] if a_tag else ""

                    time_tag = post.find("time")
                    published: Optional[datetime] = None
                    if time_tag and time_tag.get("datetime"):
                        try:
                            published = datetime.fromisoformat(time_tag["datetime"])
                            if published.tzinfo is None:
                                published = published.replace(tzinfo=timezone.utc)
                            else:
                                published = published.astimezone(timezone.utc)
                        except ValueError:
                            pass

                    if published and published < cutoff:
                        continue

                    excerpt_tag = post.find("p", class_="wp-block-post-excerpt__excerpt")
                    summary: str = excerpt_tag.get_text(strip=True) if excerpt_tag else ""

                    items.append({
                        "title": title,
                        "author": "Ben Thompson",
                        "summary": summary,
                        "link": link,
                        "source": "stratechery",
                        "content_type": "essay",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing stratechery post card: {e}")

        except Exception as e:
            logger.error(f"Error scraping stratechery ({STRATECHERY_URL}): {e}")

        logger.info(f"stratechery: {len(items)} article(s) in window")
        return items

    def _scrape_deeplearning_ai(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            r = requests.get(
                DEEPLEARNING_AI_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=self._proxies,
                timeout=15,
            )
            r.raise_for_status()
            soup = BeautifulSoup(
                r.text, "html.parser",
                parse_only=SoupStrainer("article"),
            )
            articles = soup.find_all("article")
            logger.info(f"Scraped {len(articles)} article cards from deeplearning-ai")

            for article in articles:
                try:
                    h2 = article.find("h2")
                    title: str = h2.get_text(strip=True) if h2 else ""
                    if not title:
                        continue

                    excerpt_div = article.find("div", class_=lambda c: c and "line-clamp" in c)
                    summary: str = excerpt_div.get_text(strip=True) if excerpt_div else ""

                    time_tag = article.find("time")
                    published: Optional[datetime] = None
                    if time_tag and time_tag.get("datetime"):
                        try:
                            published = datetime.fromisoformat(time_tag["datetime"])
                            if published.tzinfo is None:
                                published = published.replace(tzinfo=timezone.utc)
                            else:
                                published = published.astimezone(timezone.utc)
                        except ValueError:
                            pass

                    if published and published < cutoff:
                        continue

                    a_tag = article.find("a", href=True)
                    href: str = a_tag["href"] if a_tag else ""
                    link: str = (DEEPLEARNING_AI_BASE + href) if href.startswith("/") else href

                    needs_deep_read: bool = self._check_deep_read(title, summary)

                    items.append({
                        "title": title,
                        "author": "Andrew Ng",
                        "summary": summary,
                        "link": link,
                        "source": "deeplearning-ai",
                        "content_type": "essay",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": needs_deep_read,
                    })
                except Exception as e:
                    logger.error(f"Error parsing deeplearning-ai article card: {e}")

        except Exception as e:
            logger.error(f"Error scraping deeplearning-ai ({DEEPLEARNING_AI_URL}): {e}")

        return items

    def _scrape_ben_evans(
        self, cutoff: datetime
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        essays: List[Dict[str, Any]] = []
        presentations: List[Dict[str, Any]] = []
        try:
            r = requests.get(
                BEN_EVANS_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=self._proxies,
                timeout=15,
            )
            r.raise_for_status()
            soup = BeautifulSoup(
                r.text, "html.parser",
                parse_only=SoupStrainer(
                    class_=lambda c: c and (
                        "summary-item" in c or "PageRight" in c
                    )
                ),
            )

            # --- Essays (summary-item cards) ---
            cards = soup.find_all("div", class_="summary-item")
            logger.info(f"Scraped {len(cards)} essay cards from ben-evans")

            for card in cards:
                try:
                    title_a = card.find("a", class_="summary-title-link")
                    if not title_a:
                        continue
                    title: str = title_a.get_text(strip=True)
                    href: str = title_a.get("href", "")
                    link: str = (BEN_EVANS_BASE + href) if href.startswith("/") else href

                    date_tag = card.find(class_="summary-metadata-item--date")
                    date_str: str = date_tag.get_text(strip=True) if date_tag else ""
                    published: Optional[datetime] = self._parse_human_date(date_str)
                    if published and published < cutoff:
                        continue

                    excerpt = card.find(class_="summary-excerpt")
                    summary: str = excerpt.get_text(strip=True) if excerpt else ""

                    essays.append({
                        "title": title,
                        "author": "Benedict Evans",
                        "summary": summary,
                        "link": link,
                        "source": "ben-evans",
                        "content_type": "essay",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing ben-evans essay card: {e}")

            # --- Presentations (PageRight blocks) ---
            for block in soup.find_all("div", class_="PageRight"):
                try:
                    # Skip newsletter and podcast blocks (no plain h2 title)
                    h2 = block.find("h2")
                    if not h2:
                        continue
                    # Newsletter h2 wraps an <a>; presentation h2 is plain text
                    if h2.find("a"):
                        continue

                    title = h2.get_text(strip=True)
                    if not title:
                        continue

                    eyebrow = block.find(class_="PageRightEyebrow")
                    date_str = eyebrow.get_text(strip=True) if eyebrow else ""
                    published = self._parse_human_date(date_str)
                    if published and published < cutoff:
                        continue

                    body = block.find(class_="pagerightbody")
                    summary = body.get_text(strip=True) if body else ""

                    link_tag = block.find("a", href=True)
                    href = link_tag["href"] if link_tag else ""
                    link = (BEN_EVANS_BASE + href) if href.startswith("/") else href

                    presentations.append({
                        "title": title,
                        "author": "Benedict Evans",
                        "summary": summary,
                        "link": link,
                        "source": "ben-evans",
                        "content_type": "presentation",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing ben-evans presentation block: {e}")

            logger.info(
                f"ben-evans: {len(essays)} essay(s), {len(presentations)} presentation(s) in window"
            )
        except Exception as e:
            logger.error(f"Error scraping ben-evans ({BEN_EVANS_URL}): {e}")

        return essays, presentations

    def _parse_human_date(self, date_str: str) -> Optional[datetime]:
        """Parse dates like 'Feb 19, 2026' or 'Nov 2025' (treated as Nov 1)."""
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
            try:
                return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        return None

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

    def _check_deep_read(self, title: str, summary: str) -> bool:
        text = (title + " " + summary).lower()
        return any(kw in text for kw in DEEP_READ_KEYWORDS)
