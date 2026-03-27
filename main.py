"""
Tech Signals Bot — main entry point.

Usage:
    venv/bin/python main.py --profile what

Note: CLAUDE.md specified claude-3-7-sonnet-20250219, which was retired
Feb 19 2026. Using claude-sonnet-4-6 with adaptive thinking instead.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import anthropic
import requests
from dotenv import load_dotenv

from config import SUMMARY_TRUNCATE_CHARS
from fetchers.rss_fetcher import DAYS_FILTER, RssFetcher
from fetchers.why_fetcher import WhyFetcher
from models import NewsSignal
from prompts import WHAT_PROFILE_PROMPT, WHY_PROFILE_PROMPT
from sheets_writer import append_what_run, compute_relevant_per_source

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Update CLAUDE.md when changing this; the Slack footer is derived from it.
MODEL_ID = "claude-sonnet-4-6"
MAX_TOKENS = 16000  # Output token budget; used in API call and Slack footer.

PROFILES: Dict[str, Dict[str, Any]] = {
    "what": {
        "fetcher_cls": RssFetcher,
        "system_prompt": WHAT_PROFILE_PROMPT,
        "webhook_env": "WHAT_WEBHOOK_URL",
    },
    "why": {
        "fetcher_cls": WhyFetcher,
        "system_prompt": WHY_PROFILE_PROMPT,
        "webhook_env": "WHY_WEBHOOK_URL",
        "include_metadata": True,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _format_items_for_prompt(
    items: List[Dict[str, Any]], include_metadata: bool = False
) -> str:
    lines: List[str] = []
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. [{item['source'].upper()}] {item['title']}")
        if include_metadata:
            if item.get("author"):
                lines.append(f"   Author: {item['author']}")
            if item.get("content_type"):
                lines.append(f"   Type: {item['content_type']}")
        if item.get("summary"):
            clean_summary = _strip_html(item["summary"])[:SUMMARY_TRUNCATE_CHARS]
            lines.append(f"   Summary: {clean_summary}")
        if include_metadata and item.get("link"):
            lines.append(f"   URL: {item['link']}")
        lines.append("")
    return "\n".join(lines)


def call_anthropic(
    items: List[Dict[str, Any]],
    system_prompt: str,
    include_metadata: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Call the Anthropic API and return (signals, usage_meta).

    usage_meta keys: input_tokens (int), output_tokens (int), budget_status (str).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in the environment")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"Analyse the following {len(items)} pre-scored news items and extract "
        f"paradigm-shifting signals per your mandate:\n\n"
        + _format_items_for_prompt(items, include_metadata=include_metadata)
    )

    try:
        response = client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        raise RuntimeError(f"Anthropic API error: {e}") from e

    input_tokens: int = response.usage.input_tokens
    output_tokens: int = response.usage.output_tokens
    total_tokens: int = input_tokens + output_tokens
    budget_status: str = "Hit" if response.stop_reason == "max_tokens" else "OK"

    logger.info(
        f"Token usage — input: {input_tokens}, output: {output_tokens}, "
        f"total: {total_tokens}/{MAX_TOKENS}, budget: {budget_status}"
    )

    text_content = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    if not text_content:
        raise ValueError("Anthropic response contained no text block")

    try:
        signals = json.loads(text_content)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM returned non-JSON output: {e}\n"
            f"Raw response (first 500 chars):\n{text_content[:500]}"
        ) from e

    if not isinstance(signals, list):
        raise ValueError(f"Expected a JSON array from LLM, got {type(signals)}")

    usage_meta: Dict[str, Any] = {
        "total_tokens": total_tokens,
        "budget_status": budget_status,
    }
    return signals, usage_meta


def _date_range_str() -> str:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_FILTER)
    fmt = lambda d: f"{d.strftime('%b')} {d.day}"
    return f"{fmt(start)} – {fmt(now)}"


def _build_footer(
    total_tokens: Optional[int],
    budget_status: Optional[str],
) -> str:
    tokens = f"{total_tokens}/{MAX_TOKENS}" if total_tokens is not None else "N/A"
    budget = budget_status if budget_status is not None else "N/A"
    return f"_Model: {MODEL_ID} | Tokens: {tokens} | Budget: {budget}_"


def build_slack_payload(
    signals: List[Dict[str, Any]],
    source_counts: Optional[Dict[str, int]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
) -> Dict[str, Any]:
    footer = _build_footer(total_tokens, budget_status)

    if not signals:
        return {
            "text": "Tech Signals Digest — no significant signals detected.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "_No structurally significant signals detected in today's feed._",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}],
                },
            ],
        }

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Tech Signals Digest — {len(signals)} signal(s) | {_date_range_str()}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for signal in signals:
        lines = [
            f"📰 *{signal.get('macro_event', 'Unknown Event')}*: "
            f"{signal.get('core_fact', '')}",
            f"🧠 *ELI5*: {signal.get('eli5', '')}",
            f"📈 *Second-Order Impact*: {signal.get('investment_impact', '')}",
        ]

        source_urls: List[str] = signal.get("source_urls", [])
        if source_urls:
            links = " | ".join(
                f"<{url}|[source]>" for url in source_urls[:3]
            )
            lines.append(f"_Sources: {links}_")

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )
        blocks.append({"type": "divider"})

    if source_counts:
        funnel_lines = []
        for source, counts in source_counts.items():
            funnel_lines.append(
                f"📡 *{source.capitalize()}*   "
                f"{counts.get('fetched', 0)} fetched  ›  "
                f"{counts.get('in_window', 0)} in last 7d  ›  "
                f"{counts.get('relevant', 0)} relevant  ›  "
                f"{counts.get('sent_to_llm', 0)} sent to LLM"
            )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": line} for line in funnel_lines
                ],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}],
        }
    )

    return {
        "text": f"Tech Signals Digest — {len(signals)} signal(s)",
        "blocks": blocks,
    }


def build_why_slack_payload(
    signals: List[Dict[str, Any]],
    source_counts: Optional[Dict[str, int]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
) -> Dict[str, Any]:
    footer = _build_footer(total_tokens, budget_status)

    if not signals:
        return {
            "text": "Why Signals Digest — no items detected.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "_No essays or presentations published in the last 7 days._",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}],
                },
            ],
        }

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Why Signals Digest — {len(signals)} item(s) | {_date_range_str()}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for signal in signals:
        content_type: str = signal.get("content_type", "essay")
        is_deep_read: bool = signal.get("is_deep_read", False)

        if content_type == "podcast":
            prefix = "🎙"
        elif content_type == "presentation":
            prefix = "🗂"
        elif is_deep_read:
            prefix = "❗️"
        else:
            prefix = "🧠"

        title: str = signal.get("title", "Untitled")
        author: str = signal.get("author", "")
        raw_summary = signal.get("summary", [])
        summary_bullets: List[str] = (
            raw_summary if isinstance(raw_summary, list) else [raw_summary]
        )
        source_url: str = signal.get("source_url", "")

        date_str: str = ""
        raw_published: str = signal.get("published", "")
        if raw_published:
            try:
                dt = datetime.fromisoformat(raw_published)
                date_str = dt.strftime("%b %-d")
            except ValueError:
                pass

        header_line = f"{prefix} *{title}*"
        if author:
            header_line += f" — _{author}_"
        if date_str:
            header_line += f" · {date_str}"
        if content_type == "presentation":
            header_line += "  `[Presentation]`"

        if content_type == "presentation":
            read_label = "Find out more"
        elif content_type == "podcast":
            read_label = "Read transcript"
        else:
            read_label = "Read essay"

        lines = [header_line]
        lines += [f"• {b}" for b in summary_bullets if b]
        if source_url:
            lines.append(f"• _<{source_url}|{read_label}>_")

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        )
        blocks.append({"type": "divider"})

    if source_counts:
        counts_str = "  ·  ".join(
            f"{source}: {count}" for source, count in source_counts.items()
        )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"📡 {counts_str}"}],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}],
        }
    )

    return {
        "text": f"Why Signals Digest — {len(signals)} item(s)",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def post_to_slack(webhook_url: str, payload: Dict[str, Any]) -> None:
    response = requests.post(webhook_url, json=payload, timeout=10)
    if response.status_code != 200:
        raise RuntimeError(
            f"Slack webhook returned {response.status_code}: {response.text}"
        )
    logger.info("Slack post successful")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Tech Signals Bot")
    parser.add_argument(
        "--profile",
        required=True,
        choices=list(PROFILES.keys()),
        help="Signal profile to run",
    )
    args = parser.parse_args()

    profile = PROFILES[args.profile]

    usage_meta: Dict[str, Any] = {}

    try:
        # 1. Fetch
        fetcher = profile["fetcher_cls"]()
        items: List[Dict[str, Any]] = fetcher.fetch()
        logger.info(f"Profile '{args.profile}': {len(items)} scored items ready for LLM")

        if not items:
            logger.warning("No items fetched — nothing to analyse.")
            sys.exit(0)

        source_counts: Dict[str, int] = getattr(fetcher, "source_counts", {})

        # 2. Analyse
        signals, usage_meta = call_anthropic(
            items,
            profile["system_prompt"],
            include_metadata=profile.get("include_metadata", False),
        )
        logger.info(f"LLM returned {len(signals)} signal(s)")

        # 2a. Resolve source_ids → source_urls for What profile
        if args.profile == "what":
            id_to_url: Dict[int, str] = {
                i: item.get("link", "") for i, item in enumerate(items, 1)
            }
            for signal in signals:
                signal["source_urls"] = [
                    id_to_url[sid]
                    for sid in signal.pop("source_ids", [])
                    if sid in id_to_url and id_to_url[sid]
                ]

        # 2b. Track source performance in Google Sheets (What profile only)
        if args.profile == "what":
            relevant_per_source = compute_relevant_per_source(signals, items)
            append_what_run(source_counts, relevant_per_source)

        # 3. Format
        if args.profile == "why":
            payload = build_why_slack_payload(signals, source_counts=source_counts, **usage_meta)
        else:
            payload = build_slack_payload(signals, source_counts=source_counts, **usage_meta)

        # 4. Post to Slack
        webhook_url = os.getenv(profile["webhook_env"])
        if not webhook_url:
            raise EnvironmentError(
                f"{profile['webhook_env']} is not set in the environment"
            )
        post_to_slack(webhook_url, payload)

    except Exception as exc:
        error_msg = str(exc)
        logger.error(f"Pipeline failed: {error_msg}", exc_info=True)

        error_footer = _build_footer(
            usage_meta.get("total_tokens"),
            usage_meta.get("budget_status"),
        )
        error_payload: Dict[str, Any] = {
            "text": "🚨 Tech Signals Bot — Pipeline Error",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🚨 *Alert: Pipeline Failed*\n```{error_msg}```",
                    },
                },
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"\n{error_footer}"}],
                },
            ],
        }

        webhook_url = os.getenv(profile["webhook_env"])
        if webhook_url:
            try:
                post_to_slack(webhook_url, error_payload)
            except Exception as slack_exc:
                logger.error(f"Failed to post error payload to Slack: {slack_exc}")
        else:
            logger.warning(
                f"{profile['webhook_env']} not set — error payload not sent to Slack"
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
