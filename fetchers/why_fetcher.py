import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import feedparser
import requests
from bs4 import BeautifulSoup, SoupStrainer

from config import DEEP_READ_KEYWORDS
from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

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

A16Z_ARCHIVE_URL = "https://www.a16z.news/api/v1/archive?sort=new&limit=25"

ONE_USEFUL_THING_URL = "https://www.oneusefulthing.org/feed"


class WhyFetcher(BaseFetcher):
    def __init__(self) -> None:
        self.source_counts: Dict[str, int] = {
            "stratechery": 0,
            "stratechery-podcasts": 0,
            "deeplearning-ai": 0,
            "ben-evans-essays": 0,
            "ben-evans-presentations": 0,
            "a16z": 0,
            "one-useful-thing": 0,
        }
        proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._proxy_handler = (
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            if proxy_url else None
        )

    def fetch(self) -> List[Dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_FILTER)
        items: List[Dict[str, Any]] = []

        article_items = self._fetch_stratechery_rss(cutoff)
        podcast_items = self._fetch_stratechery_podcast_rss(cutoff)

        article_titles = {item["title"].lower() for item in article_items}
        deduped_podcasts = [p for p in podcast_items if p["title"].lower() not in article_titles]

        self.source_counts["stratechery"] = len(article_items)
        self.source_counts["stratechery-podcasts"] = len(deduped_podcasts)
        items.extend(article_items)
        items.extend(deduped_podcasts)

        dl_items = self._scrape_deeplearning_ai(cutoff)
        self.source_counts["deeplearning-ai"] = len(dl_items)
        items.extend(dl_items)

        essay_items, presentation_items = self._scrape_ben_evans(cutoff)
        self.source_counts["ben-evans-essays"] = len(essay_items)
        self.source_counts["ben-evans-presentations"] = len(presentation_items)
        items.extend(essay_items)
        items.extend(presentation_items)

        a16z_items = self._fetch_a16z(cutoff)
        self.source_counts["a16z"] = len(a16z_items)
        items.extend(a16z_items)

        one_useful_thing_items = self._fetch_one_useful_thing_rss(cutoff)
        self.source_counts["one-useful-thing"] = len(one_useful_thing_items)
        items.extend(one_useful_thing_items)

        logger.info(f"WhyFetcher: {len(items)} items in the last {DAYS_FILTER} days")
        return items

    def _fetch_stratechery_rss(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        rss_url = os.getenv("STRATECHERY_RSS_URL")
        if not rss_url:
            logger.warning("STRATECHERY_RSS_URL not set; skipping stratechery")
            return items
        try:
            feed = feedparser.parse(
                rss_url,
                agent=USER_AGENT,
                handlers=[self._proxy_handler] if self._proxy_handler else [],
            )
            logger.info(f"Fetched {len(feed.entries)} entries from stratechery RSS")

            for entry in feed.entries:
                try:
                    published = self._parse_date(entry)
                    if published and published < cutoff:
                        continue

                    title: str = getattr(entry, "title", "") or ""
                    link: str = getattr(entry, "link", "") or ""

                    # Prefer full article body over one-sentence teaser
                    content_list = getattr(entry, "content", None)
                    if content_list:
                        summary: str = content_list[0].get("value", "") or ""
                    else:
                        summary = getattr(entry, "summary", "") or ""

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
                    logger.error(f"Error parsing stratechery RSS entry: {e}")

        except Exception as e:
            logger.error(f"Error fetching stratechery RSS: {e}")

        logger.info(f"stratechery: {len(items)} article(s) in window")
        return items

    def _fetch_stratechery_podcast_rss(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        rss_url = os.getenv("STRATECHERY_PODCAST_URL")
        if not rss_url:
            logger.warning("STRATECHERY_PODCAST_URL not set; skipping stratechery podcast")
            return items
        try:
            feed = feedparser.parse(
                rss_url,
                agent=USER_AGENT,
                handlers=[self._proxy_handler] if self._proxy_handler else [],
            )
            logger.info(f"Fetched {len(feed.entries)} entries from stratechery podcast RSS")

            for entry in feed.entries:
                try:
                    published = self._parse_date(entry)
                    if published and published < cutoff:
                        continue

                    title: str = getattr(entry, "title", "") or ""
                    link: str = getattr(entry, "link", "") or ""

                    content_list = getattr(entry, "content", None)
                    if content_list:
                        summary: str = content_list[0].get("value", "") or ""
                    else:
                        summary = getattr(entry, "summary", "") or ""

                    items.append({
                        "title": title,
                        "author": "Ben Thompson",
                        "summary": summary,
                        "link": link,
                        "source": "stratechery",
                        "content_type": "podcast",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing stratechery podcast RSS entry: {e}")

        except Exception as e:
            logger.error(f"Error fetching stratechery podcast RSS: {e}")

        logger.info(f"stratechery-podcasts: {len(items)} episode(s) in window")
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

    def _fetch_a16z(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            r = requests.get(
                A16Z_ARCHIVE_URL,
                headers={"User-Agent": USER_AGENT},
                proxies=self._proxies,
                timeout=15,
            )
            r.raise_for_status()
            posts = r.json()
            logger.info(f"Fetched {len(posts)} posts from a16z archive")

            for post in posts:
                try:
                    title: str = post.get("title", "") or ""
                    if not title:
                        continue
                    if title.lower().startswith("investing in"):
                        continue

                    post_date_str: str = post.get("post_date", "") or ""
                    published: Optional[datetime] = None
                    if post_date_str:
                        try:
                            published = datetime.fromisoformat(post_date_str.replace("Z", "+00:00"))
                            if published.tzinfo is None:
                                published = published.replace(tzinfo=timezone.utc)
                            else:
                                published = published.astimezone(timezone.utc)
                        except ValueError:
                            pass

                    if published and published < cutoff:
                        continue

                    bylines = post.get("publishedBylines", [])
                    author: str = bylines[0]["name"] if bylines and bylines[0].get("name") else "a16z"

                    summary: str = post.get("description", "") or ""
                    link: str = post.get("canonical_url", "") or ""

                    items.append({
                        "title": title,
                        "author": author,
                        "summary": summary,
                        "link": link,
                        "source": "a16z",
                        "content_type": "essay",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing a16z post: {e}")

        except Exception as e:
            logger.error(f"Error fetching a16z archive ({A16Z_ARCHIVE_URL}): {e}")

        logger.info(f"a16z: {len(items)} post(s) in window")
        return items

    def _fetch_one_useful_thing_rss(self, cutoff: datetime) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            feed = feedparser.parse(
                ONE_USEFUL_THING_URL,
                agent=USER_AGENT,
                handlers=[self._proxy_handler] if self._proxy_handler else [],
            )
            logger.info(f"Fetched {len(feed.entries)} entries from one-useful-thing RSS")

            for entry in feed.entries:
                try:
                    published = self._parse_date(entry)
                    if published and published < cutoff:
                        continue

                    title: str = getattr(entry, "title", "") or ""
                    link: str = getattr(entry, "link", "") or ""

                    content_list = getattr(entry, "content", None)
                    if content_list:
                        summary: str = content_list[0].get("value", "") or ""
                    else:
                        summary = getattr(entry, "summary", "") or ""

                    items.append({
                        "title": title,
                        "author": "Ethan Mollick",
                        "summary": summary,
                        "link": link,
                        "source": "one-useful-thing",
                        "content_type": "essay",
                        "published": published.isoformat() if published else "",
                        "needs_deep_read": self._check_deep_read(title, summary),
                    })
                except Exception as e:
                    logger.error(f"Error parsing one-useful-thing RSS entry: {e}")

        except Exception as e:
            logger.error(f"Error fetching one-useful-thing RSS ({ONE_USEFUL_THING_URL}): {e}")

        logger.info(f"one-useful-thing: {len(items)} post(s) in window")
        return items

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
