import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import feedparser

from fetchers.base_fetcher import BaseFetcher

logger = logging.getLogger(__name__)

FEEDS: Dict[str, str] = {
    "techmeme": "https://www.techmeme.com/feed.xml",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
}

# Keyword → score weight. Higher = more relevant to the Crypto/AI/Tech focus.
KEYWORD_WHITELIST: Dict[str, int] = {
    # Crypto fundamentals
    "bitcoin": 3, "btc": 3, "ethereum": 3, "eth": 3, "crypto": 2,
    "blockchain": 2, "defi": 3, "nft": 1, "web3": 2, "layer2": 3,
    "layer 2": 3, "l2": 3, "solana": 2, "stablecoin": 3, "cbdc": 3,
    "dao": 2, "protocol": 2, "consensus": 3, "proof of stake": 3,
    "proof of work": 3, "validator": 2, "smart contract": 2,
    "dex": 2, "liquidity": 2, "yield": 1, "airdrop": 1,
    # AI / LLM
    "artificial intelligence": 3, "machine learning": 2,
    "llm": 3, "large language model": 3, "foundation model": 3,
    "gpt": 2, "claude": 2, "gemini": 2, "llama": 2,
    "neural": 2, "inference": 2, "fine-tuning": 2, "fine tuning": 2,
    "agent": 2, "rag": 2, "multimodal": 2, "reasoning": 2,
    "transformer": 2, "diffusion": 2, "open source model": 3,
    # Infrastructure / Compute
    "semiconductor": 3, "chip": 2, "nvidia": 2, "amd": 2, "tsmc": 3,
    "quantum": 3, "gpu": 3, "tpu": 3, "datacenter": 2, "data center": 2,
    "compute": 2, "energy": 2, "power grid": 3, "nuclear": 2,
    "cloud": 1, "api": 1, "open source": 2,
    # Macro / Regulation
    "regulation": 2, "sec": 2, "cftc": 2, "fed": 1, "federal reserve": 2,
    "interest rate": 2, "etf": 2, "institutional": 2, "treasury": 2,
    "congress": 2, "legislation": 2,
}

TOP_N = 40
DAYS_FILTER = 7

# Mimic a real browser so sites like CoinDesk don't silently return empty feeds.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


class RssFetcher(BaseFetcher):
    def __init__(self) -> None:
        # Populated during fetch(). Each source maps to a dict of stage → count:
        # fetched, in_window, relevant (score > 0), sent_to_llm (after top-N cut).
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
            for source in FEEDS
        }

        for source, url in FEEDS.items():
            try:
                feed = feedparser.parse(
                    url,
                    agent=USER_AGENT,
                    handlers=[self._proxy_handler] if self._proxy_handler else [],
                )
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

                        if score > 0:
                            stage_counts[source]["relevant"] += 1

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

        all_items.sort(key=lambda x: x["score"], reverse=True)
        top_items = all_items[:TOP_N]

        for item in top_items:
            stage_counts[item["source"]]["sent_to_llm"] += 1

        logger.info(
            f"Scored {len(all_items)} total items across all feeds; "
            f"keeping top {len(top_items)} (score threshold: "
            f"{top_items[-1]['score'] if top_items else 'n/a'})"
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

    def _score_item(self, title: str, summary: str) -> int:
        text = (title + " " + summary).lower()
        return sum(weight for kw, weight in KEYWORD_WHITELIST.items() if kw in text)
