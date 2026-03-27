"""Catcher script for the How pipeline.

Fetches new items from ChatPRD and SSPAI and appends them to a persistent
JSON backlog file. No LLM calls — pure fetch-and-deduplicate.
"""
import json
import os
from typing import Any, Dict, List

from fetchers.chatprd_fetcher import ChatPRDFetcher
from fetchers.sspai_fetcher import SSPAIFetcher

BACKLOG_FILE = "data/how_backlog.json"


def main() -> None:
    # 1. Load existing backlog
    backlog: List[Dict[str, Any]] = []
    try:
        with open(BACKLOG_FILE, "r", encoding="utf-8") as f:
            backlog = json.load(f)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass

    existing_urls = {item["link"] for item in backlog if item.get("link")}

    # 2. Fetch from all sources
    new_items: List[Dict[str, Any]] = ChatPRDFetcher().fetch() + SSPAIFetcher().fetch()

    # 3. Deduplicate by URL and append
    added = 0
    for item in new_items:
        link = item.get("link", "")
        if link and link not in existing_urls:
            backlog.append(item)
            existing_urls.add(link)
            added += 1

    # 4. Persist
    os.makedirs(os.path.dirname(BACKLOG_FILE), exist_ok=True)
    with open(BACKLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(backlog, f, indent=2, ensure_ascii=False)

    print(f"Found {added} new items. Backlog now has {len(backlog)} items.")


if __name__ == "__main__":
    main()
