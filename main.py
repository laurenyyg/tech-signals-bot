"""
Tech Signals Bot — main entry point.

Usage:
    venv/bin/python main.py --profile what

Note: the project conventions (now AGENTS.md) specified claude-3-7-sonnet-20250219, which was retired
Feb 19 2026. Using claude-sonnet-4-6 with adaptive thinking instead.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

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
    fit_text,
    post_payloads,
    post_to_slack,
    section_block,
    split_into_messages,
    strip_html,
)
from config import (
    SUMMARY_TRUNCATE_CHARS,
    WHAT_DAYS_FILTER,
    WHAT_DAYS_MAX,
    WHAT_DAYS_SLACK,
    WHAT_SEEN_RETENTION_DAYS,
)
from fetchers.rss_fetcher import TOP_N, RssFetcher, funnel_lines
from how_pipeline import FOOTER_MODEL_LABEL as HOW_FOOTER_MODEL_LABEL, run_how_pipeline
from prompts import WHAT_PROFILE_PROMPT
from seen_ledger import (
    canonical_url,
    latest_processed_at,
    load_json_list,
    mark_seen,
    prune_seen,
    save_json_list,
)
from sheets_writer import append_what_run, spreadsheet_link
from why_pipeline import run_why_pipeline

load_dotenv()

configure_logging()
logger = logging.getLogger(__name__)

SEEN_FILE = "data/what_seen.json"
Item = Dict[str, Any]

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
# Seen ledger
# ---------------------------------------------------------------------------

def ledger_key(item: Item) -> str:
    """Canonical link, else source+title."""
    url = item.get("link") or ""
    if url:
        return canonical_url(url)
    return f"{item.get('source', '')}:{item.get('title', '').strip().lower()}"


def drop_seen(items: List[Item], seen_keys: Set[str]) -> Tuple[List[Item], List[Item]]:
    """Return (unseen items, items dropped as already posted)."""
    fresh = [item for item in items if ledger_key(item) not in seen_keys]
    dropped = [item for item in items if ledger_key(item) in seen_keys]
    return fresh, dropped


def fetch_cutoff(seen: List[Dict[str, Any]], now: datetime) -> datetime:
    """Oldest publish date to fetch.

    Normal weeks stay at WHAT_DAYS_FILTER (8). After a missed run the window
    stretches back to the last successful post minus WHAT_DAYS_SLACK, capped at
    WHAT_DAYS_MAX (14). An empty ledger uses the 8-day floor.
    """
    floor = now - timedelta(days=WHAT_DAYS_FILTER)
    last = latest_processed_at(seen)
    if last is None:
        return floor
    recovered = last - timedelta(days=WHAT_DAYS_SLACK)
    ceiling = now - timedelta(days=WHAT_DAYS_MAX)
    return max(ceiling, min(floor, recovered))


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


def resolve_source_urls(
    signals: List[Dict[str, Any]], items: List[Dict[str, Any]]
) -> None:
    """Replace each signal's source_ids with URLs from items. Unknown ids are dropped."""
    id_to_url: Dict[int, str] = {
        i: item.get("link", "") for i, item in enumerate(items, 1)
    }
    for signal in signals:
        signal["source_urls"] = [
            id_to_url[sid]
            for sid in signal.pop("source_ids", [])
            if sid in id_to_url and id_to_url[sid]
        ]


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
    source_errors: Optional[Dict[str, str]] = None,
    window_days: int = WHAT_DAYS_FILTER,
    spreadsheet_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build the What digest as one or more Slack messages (see split_into_messages).

    Cards come first; source-error warnings, the per-source funnel, and the
    FinOps footer go on the last message.
    """
    footer_blocks = context_blocks([
        f"\n{build_footer(total_tokens, budget_status, extra=spreadsheet_link(spreadsheet_id))}"
    ])

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
    if not signals:
        groups.append([section_block(
            "_No structurally significant signals detected in today's feed._"
        )])

    warnings = [
        f"⚠️ *{source}* fetch failed: {fit_text(' '.join(error.split()), 200)}"
        for source, error in (source_errors or {}).items()
    ]
    tail = (
        context_blocks(warnings)
        + context_blocks(funnel_lines(source_counts or {}, window_days))
        + footer_blocks
    )
    return split_into_messages(
        f"Tech Signals Digest — {len(signals)} signal(s) | {date_range_str(window_days)}",
        groups,
        tail,
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

        now = datetime.now(timezone.utc)
        seen = prune_seen(load_json_list(SEEN_FILE), now, WHAT_SEEN_RETENTION_DAYS)
        cutoff = fetch_cutoff(seen, now)
        window_days = max(1, (now - cutoff).days)
        logger.info(f"Fetch window: {window_days}d (cutoff {cutoff.date()})")

        fetcher = profile["fetcher_cls"](cutoff=cutoff)
        scored: List[Dict[str, Any]] = fetcher.fetch()
        source_counts: Dict[str, Dict[str, int]] = getattr(fetcher, "source_counts", {})
        source_errors: Dict[str, str] = getattr(fetcher, "source_errors", {})

        items, already_posted = drop_seen(scored, {e["url"] for e in seen})
        for item in already_posted:
            src = item.get("source", "")
            if src in source_counts:
                source_counts[src]["already_posted"] += 1
        if already_posted:
            logger.info(f"Seen ledger: skipped {len(already_posted)} already-posted item(s).")

        items = items[:TOP_N]
        for item in items:
            if item.get("source") in source_counts:
                source_counts[item["source"]]["sent_to_llm"] += 1
        logger.info(f"Profile '{args.profile}': {len(items)} scored items ready for LLM")

        webhook_url = os.getenv(profile["webhook_env"])
        if not webhook_url:
            raise EnvironmentError(
                f"{profile['webhook_env']} is not set in the environment"
            )

        if not items:
            logger.warning("No scored items — posting empty digest.")
            payloads = build_slack_payloads(
                [],
                source_counts=source_counts,
                source_errors=source_errors,
                total_tokens=0,
                budget_status="OK",
                window_days=window_days,
                spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID"),
            )
            post_payloads(webhook_url, payloads, post=post_to_slack)
            if args.profile == "what":
                append_what_run(source_counts)
            return

        # 2. Analyse
        signals, usage_meta = call_anthropic(
            items,
            profile["system_prompt"],
        )
        logger.info(f"LLM returned {len(signals)} signal(s)")

        # 2a. Resolve source_ids → source_urls for What profile
        if args.profile == "what":
            resolve_source_urls(signals, items)

        # 3. Format and post
        payloads = build_slack_payloads(
            signals,
            source_counts=source_counts,
            source_errors=source_errors,
            window_days=window_days,
            spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID"),
            **usage_meta,
        )
        post_payloads(webhook_url, payloads, post=post_to_slack)  # raises — ledger/Sheets left untouched

        if args.profile == "what":
            append_what_run(source_counts)

        seen = mark_seen(seen, [ledger_key(item) for item in items], now)
        save_json_list(SEEN_FILE, seen)
        logger.info(f"Seen ledger now has {len(seen)} item(s).")

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
