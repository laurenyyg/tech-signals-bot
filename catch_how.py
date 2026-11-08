"""Catcher script for the How pipeline.

Fetches new items from ChatPRD and SSPAI, downloads and extracts each new
article's body (unless the fetcher already supplied one, as ChatPRD workflow
items do), and appends them to the persistent JSON backlog. No LLM calls.

Usage:
    venv/bin/python catch_how.py            # default HOW_DAYS_FILTER window
    venv/bin/python catch_how.py --days 14  # one-off backfill
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from dotenv import load_dotenv

from common import (
    build_error_payload,
    build_footer,
    configure_logging,
    fetch_body,
    post_to_slack,
)
from config import HOW_BODY_MAX_CHARS, HOW_DAYS_FILTER
from fetchers.chatprd_fetcher import ChatPRDFetcher
from fetchers.sspai_fetcher import SSPAIFetcher
from how_pipeline import (
    BACKLOG_FILE,
    SEEN_FILE,
    known_urls,
    load_json_list,
    prune_seen,
    save_json_list,
)

load_dotenv()
configure_logging()
logger = logging.getLogger(__name__)


def _alert(source: str, error: Exception) -> None:
    """Post a 🚨 alert for a failed source; never raises."""
    webhook = os.getenv("HOW_WEBHOOK_URL")
    if not webhook:
        logger.warning("HOW_WEBHOOK_URL not set — catcher alert not sent to Slack")
        return
    payload = build_error_payload(
        f"How catcher: {source} failed — {error}",
        build_footer(None, None, model_label="none (catcher)"),
    )
    try:
        post_to_slack(webhook, payload)
    except Exception as slack_exc:
        logger.error(f"Failed to post catcher alert to Slack: {slack_exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="How pipeline daily catcher")
    parser.add_argument(
        "--days", type=int, default=HOW_DAYS_FILTER, help="Fetch window in days"
    )
    args = parser.parse_args()

    backlog = load_json_list(BACKLOG_FILE)
    seen = prune_seen(load_json_list(SEEN_FILE), datetime.now(timezone.utc))
    known = known_urls(backlog, seen)

    sources: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
        "ChatPRD": ChatPRDFetcher(days=args.days).fetch,
        "SSPAI": SSPAIFetcher(days=args.days).fetch,
    }

    failed: List[str] = []
    added = 0
    for name, fetch in sources.items():
        try:
            items = fetch()
        except Exception as e:
            logger.error(f"[Catcher] {name} failed: {e}", exc_info=True)
            failed.append(name)
            _alert(name, e)
            continue

        for item in items:
            link = item.get("link", "")
            if not link or link in known:
                continue
            if not item.get("body"):
                item["body"] = fetch_body(link, HOW_BODY_MAX_CHARS)
            if not item["body"]:
                logger.warning(f"[Catcher] no body extracted for {link}; synthesizer will retry")
            backlog.append(item)
            known.add(link)
            added += 1

    save_json_list(BACKLOG_FILE, backlog)
    save_json_list(SEEN_FILE, seen)
    logger.info(f"Found {added} new item(s). Backlog now has {len(backlog)} item(s).")

    if len(failed) == len(sources):
        sys.exit(1)


if __name__ == "__main__":
    main()
