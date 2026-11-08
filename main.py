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
from typing import Any, Dict, List, Optional, Tuple

import anthropic
from dotenv import load_dotenv

from common import (
    MAX_TOKENS,
    MODEL_ID,
    Block,
    build_error_payload,
    build_footer,
    configure_logging,
    context_blocks,
    date_range_str,
    post_payloads,
    post_to_slack,
    section_block,
    split_into_messages,
    strip_html,
)
from config import SUMMARY_TRUNCATE_CHARS
from fetchers.rss_fetcher import DAYS_FILTER as WHAT_DAYS_FILTER, RssFetcher
from how_pipeline import FOOTER_MODEL_LABEL as HOW_FOOTER_MODEL_LABEL, run_how_pipeline
from prompts import WHAT_PROFILE_PROMPT
from sheets_writer import append_what_run, compute_relevant_per_source
from why_pipeline import run_why_pipeline

load_dotenv()

configure_logging()
logger = logging.getLogger(__name__)

PROFILES: Dict[str, Dict[str, Any]] = {
    "what": {
        "fetcher_cls": RssFetcher,
        "system_prompt": WHAT_PROFILE_PROMPT,
        "webhook_env": "WHAT_WEBHOOK_URL",
    },
}

# Why and How run their own pipelines (per-item calls / two-stage cascade).
STANDALONE_PROFILES: Dict[str, Dict[str, Any]] = {
    "why": {"webhook_env": "WHY_WEBHOOK_URL"},
    "how": {"webhook_env": "HOW_WEBHOOK_URL"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_items_for_prompt(items: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. [{item['source'].upper()}] {item['title']}")
        if item.get("summary"):
            clean_summary = strip_html(item["summary"])[:SUMMARY_TRUNCATE_CHARS]
            lines.append(f"   Summary: {clean_summary}")
        lines.append("")
    return "\n".join(lines)


def call_anthropic(
    items: List[Dict[str, Any]],
    system_prompt: str,
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
        + _format_items_for_prompt(items)
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


def build_slack_payloads(
    signals: List[Dict[str, Any]],
    source_counts: Optional[Dict[str, Dict[str, int]]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build the What digest as one or more Slack messages (see split_into_messages)."""
    footer_blocks = context_blocks([f"\n{build_footer(total_tokens, budget_status)}"])

    if not signals:
        return [{
            "text": "Tech Signals Digest — no significant signals detected.",
            "blocks": [
                section_block("_No structurally significant signals detected in today's feed._"),
                *footer_blocks,
            ],
        }]

    groups: List[List[Block]] = []
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

        groups.append([section_block("\n".join(lines)), {"type": "divider"}])

    funnel_lines = [
        f"📡 *{source.capitalize()}*   "
        f"{counts.get('fetched', 0)} fetched  ›  "
        f"{counts.get('in_window', 0)} in last {WHAT_DAYS_FILTER}d  ›  "
        f"{counts.get('relevant', 0)} relevant  ›  "
        f"{counts.get('sent_to_llm', 0)} sent to LLM"
        for source, counts in (source_counts or {}).items()
    ]
    return split_into_messages(
        f"Tech Signals Digest — {len(signals)} signal(s) | {date_range_str(WHAT_DAYS_FILTER)}",
        groups,
        context_blocks(funnel_lines) + footer_blocks,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Tech Signals Bot")
    parser.add_argument(
        "--profile",
        required=True,
        choices=[*PROFILES.keys(), *STANDALONE_PROFILES.keys()],
        help="Signal profile to run",
    )
    args = parser.parse_args()

    profile: Dict[str, Any] = {**PROFILES, **STANDALONE_PROFILES}[args.profile]

    usage_meta: Dict[str, Any] = {}

    try:
        # Standalone pipelines update usage_meta in place so a crash footer shows
        # tokens spent before the failure.
        if args.profile in STANDALONE_PROFILES:
            webhook = os.getenv(profile["webhook_env"])
            if not webhook:
                raise EnvironmentError(
                    f"{profile['webhook_env']} is not set in the environment"
                )
            runner = run_why_pipeline if args.profile == "why" else run_how_pipeline
            runner(webhook, usage_meta)
            return

        # 1. Fetch
        fetcher = profile["fetcher_cls"]()
        items: List[Dict[str, Any]] = fetcher.fetch()
        logger.info(f"Profile '{args.profile}': {len(items)} scored items ready for LLM")

        if not items:
            logger.warning("No items fetched — nothing to analyse.")
            sys.exit(0)

        source_counts: Dict[str, Dict[str, int]] = getattr(fetcher, "source_counts", {})

        # 2. Analyse
        signals, usage_meta = call_anthropic(
            items,
            profile["system_prompt"],
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
        payloads = build_slack_payloads(signals, source_counts=source_counts, **usage_meta)

        # 4. Post to Slack
        webhook_url = os.getenv(profile["webhook_env"])
        if not webhook_url:
            raise EnvironmentError(
                f"{profile['webhook_env']} is not set in the environment"
            )
        post_payloads(webhook_url, payloads, post=post_to_slack)

    except Exception as exc:
        error_msg = str(exc)
        logger.error(f"Pipeline failed: {error_msg}", exc_info=True)

        if args.profile == "how":
            error_footer = build_footer(
                usage_meta.get("total_tokens"),
                usage_meta.get("budget_status"),
                model_label=HOW_FOOTER_MODEL_LABEL,
                max_tokens=usage_meta.get("max_tokens", MAX_TOKENS),
            )
        else:
            error_footer = build_footer(
                usage_meta.get("total_tokens"),
                usage_meta.get("budget_status"),
                max_tokens=usage_meta.get("max_tokens", MAX_TOKENS),
            )
        error_payload = build_error_payload(error_msg, error_footer)

        _env_name = profile["webhook_env"]
        _webhook = os.getenv(_env_name)
        if _webhook:
            try:
                post_to_slack(_webhook, error_payload)
            except Exception as slack_exc:
                logger.error(f"Failed to post error payload to Slack: {slack_exc}")
        else:
            logger.warning(f"{_env_name} not set — error payload not sent to Slack")
        sys.exit(1)


if __name__ == "__main__":
    main()
