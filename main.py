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
import trafilatura
from dotenv import load_dotenv

from config import SUMMARY_TRUNCATE_CHARS
from fetchers.rss_fetcher import DAYS_FILTER as WHAT_DAYS_FILTER, RssFetcher
from fetchers.why_fetcher import DAYS_FILTER as WHY_DAYS_FILTER, WhyFetcher
from models import NewsSignal
from prompts import HOW_PROFILE_PROMPT, WHAT_PROFILE_PROMPT, WHY_PROFILE_PROMPT
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

HAIKU_MODEL_ID = "claude-3-5-haiku-20241022"
HOW_BACKLOG_FILE = "data/how_backlog.json"

HAIKU_BOUNCER_SYSTEM_PROMPT = (
    "You are a ruthless technical filter. This article body is supposed to contain "
    "a step-by-step AI tool workflow SOP. If it does not contain explicit steps, "
    "return 'NULL'. If it does, strictly extract the raw Tool Stack and the core "
    "Step-by-Step workflow. Do NOT use fancy JSON, just plain structured text."
)

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


def _call_claude_raw(
    model_id: str,
    system: str,
    user_message: str,
    max_tokens: int,
    use_thinking: bool = False,
) -> Tuple[str, int, int]:
    """Low-level Anthropic call. Returns (response_text, input_tokens, output_tokens).

    Does not use _format_items_for_prompt. Pass use_thinking=False for models
    that do not support the thinking parameter (e.g. claude-3-5-haiku-20241022).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set in the environment")

    client = anthropic.Anthropic(api_key=api_key)

    create_kwargs: Dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }
    if use_thinking:
        create_kwargs["thinking"] = {"type": "adaptive"}

    try:
        response = client.messages.create(**create_kwargs)
    except anthropic.APIError as e:
        raise RuntimeError(f"Anthropic API error ({model_id}): {e}") from e

    input_tokens: int = response.usage.input_tokens
    output_tokens: int = response.usage.output_tokens

    text_content = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    if not text_content:
        raise ValueError(f"Anthropic response from {model_id} contained no text block")

    return text_content, input_tokens, output_tokens


def preprocess_article(url: str, html_content: str) -> str:
    """Extract clean article text from raw HTML.

    Primary path: trafilatura.extract(). Fallback: _strip_html().
    """
    raw_len = len(html_content)
    cleaned = trafilatura.extract(
        html_content, include_comments=False, include_tables=False
    )
    if not cleaned:
        logger.warning(
            "trafilatura returned no content for %s — falling back to _strip_html()", url
        )
        cleaned = _strip_html(html_content)
    clean_len = len(cleaned)
    logger.info(
        "Pre-processing URL: %s. Reduced raw HTML from %d chars to %d chars of clean text.",
        url, raw_len, clean_len,
    )
    return cleaned


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


def _date_range_str(days: int) -> str:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
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
                "text": f"Tech Signals Digest — {len(signals)} signal(s) | {_date_range_str(WHAT_DAYS_FILTER)}",
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
                f"{counts.get('in_window', 0)} in last {WHAT_DAYS_FILTER}d  ›  "
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
                        "text": f"_No essays or presentations published in the last {WHY_DAYS_FILTER} days._",
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
                "text": f"Why Signals Digest — {len(signals)} item(s) | {_date_range_str(WHY_DAYS_FILTER)}",
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


def build_how_slack_payload(
    signals: List[Dict[str, Any]],
    source_counts: Optional[Dict[str, int]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
) -> Dict[str, Any]:
    footer = _build_footer(total_tokens, budget_status)

    if not signals:
        empty_lines = ["_No replicable AI tool workflows found in the backlog._"]
        if source_counts:
            counts_str = "  ·  ".join(
                f"{source}: {count}" for source, count in source_counts.items()
            )
            empty_lines.append(f"📡 {counts_str}")
        return {
            "text": "How Signals Digest — no workflow SOPs found.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "\n".join(empty_lines),
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
                "text": f"How Signals Digest — {len(signals)} workflow(s)",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for signal in signals:
        tech_stack: List[str] = signal.get("tech_stack", [])
        workflow_steps: List[str] = signal.get("workflow_steps", [])
        roi_impact: str = signal.get("roi_impact", "")
        url: str = signal.get("url", "")
        title: str = signal.get("title", "Untitled")
        source: str = signal.get("source", "")

        header_line = f"*{title}*"
        if source:
            header_line += f" — _{source}_"

        lines = [header_line]
        if tech_stack:
            lines.append(f"🛠️ *Tech Stack*: {', '.join(tech_stack)}")
        if workflow_steps:
            bullets = "\n".join(f"• {step}" for step in workflow_steps)
            lines.append(f"📝 *Workflow SOP*:\n{bullets}")
        if roi_impact:
            lines.append(f"💰 *ROI/Impact*: {roi_impact}")
        if url:
            lines.append(f"_<{url}|Read more>_")

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
        "text": f"How Signals Digest — {len(signals)} workflow(s)",
        "blocks": blocks,
    }


def run_how_pipeline(webhook_url: str) -> None:
    """Two-stage LLM cascade for the How profile.

    Stage 1 — Haiku bouncer: filters each backlog item, extracts raw workflow text.
    Stage 2 — Sonnet formatter: structures passing items into HowSignal JSON.

    Backlog is cleared only on a successful Slack post.
    """
    # 1. Load backlog
    backlog: List[Dict[str, Any]] = []
    try:
        with open(HOW_BACKLOG_FILE, "r", encoding="utf-8") as f:
            backlog = json.load(f)
    except FileNotFoundError:
        logger.info("How backlog file not found — nothing to process.")
        return
    except json.JSONDecodeError as e:
        raise RuntimeError(f"How backlog JSON is malformed: {e}") from e

    if not backlog:
        logger.info("How backlog is empty — nothing to process.")
        return

    logger.info(f"How pipeline: {len(backlog)} items in backlog.")

    source_counts: Dict[str, int] = {}
    for item in backlog:
        src = item.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    signals: List[Dict[str, Any]] = []
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    budget_hit: bool = False

    # 2. Per-item cascade
    for item in backlog:
        url: str = item.get("link", "")
        title: str = item.get("title", "")

        # Stage 1: preprocess + Haiku bouncer
        clean_text = preprocess_article(url, item.get("summary", ""))

        try:
            haiku_text, h_in, h_out = _call_claude_raw(
                model_id=HAIKU_MODEL_ID,
                system=HAIKU_BOUNCER_SYSTEM_PROMPT,
                user_message=clean_text,
                max_tokens=1024,
                use_thinking=False,
            )
        except Exception as e:
            logger.warning(f"[Cascade Stage 1 - Haiku] Error for {url}: {e}. Skipping.")
            continue

        total_input_tokens += h_in
        total_output_tokens += h_out
        if h_out >= 1024:
            budget_hit = True

        logger.info(
            f"[Cascade Stage 1 - Haiku] Bouncing URL {url}. Used {h_in} input tokens."
        )

        if haiku_text.strip().upper() == "NULL":
            logger.info(
                f"[Cascade] Article {url} contains no structured workflow, discarding."
            )
            continue

        # Stage 2: Sonnet formatter
        word_count = len(haiku_text.split())
        logger.info(
            f"[FinOps] Sonnet input size for this item: {word_count} words. "
            f"Cost significantly reduced!"
        )

        sonnet_user_message = (
            f"Title: {title}\n"
            f"Source: {item.get('source', '')}\n"
            f"URL: {url}\n\n"
            f"Extracted workflow text:\n{haiku_text}"
        )

        try:
            sonnet_text, s_in, s_out = _call_claude_raw(
                model_id=MODEL_ID,
                system=HOW_PROFILE_PROMPT,
                user_message=sonnet_user_message,
                max_tokens=MAX_TOKENS,
                use_thinking=True,
            )
        except Exception as e:
            logger.warning(f"[Cascade Stage 2 - Sonnet] Error for {url}: {e}. Skipping.")
            continue

        total_input_tokens += s_in
        total_output_tokens += s_out
        if s_out >= MAX_TOKENS:
            budget_hit = True

        logger.info(
            f"[Cascade Stage 2 - Sonnet] Refining pre-filtered data. Used {s_in} input tokens."
        )

        try:
            item_signals = json.loads(sonnet_text)
            if not isinstance(item_signals, list):
                raise ValueError(f"Expected JSON array, got {type(item_signals)}")
            signals.extend(item_signals)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                f"[Cascade Stage 2 - Sonnet] JSON parse failed for {url}: {e}. "
                f"Raw (first 300): {sonnet_text[:300]}. Skipping."
            )
            continue

    # 3. Aggregate
    total_tokens = total_input_tokens + total_output_tokens
    budget_status = "Hit" if budget_hit else "OK"
    logger.info(
        f"[FinOps] How pipeline complete. Total tokens: {total_tokens} "
        f"(in: {total_input_tokens}, out: {total_output_tokens}). "
        f"Budget: {budget_status}. Signals extracted: {len(signals)}."
    )

    # 4. Build and post
    payload = build_how_slack_payload(signals, source_counts=source_counts, total_tokens=total_tokens, budget_status=budget_status)
    post_to_slack(webhook_url, payload)  # raises on failure — backlog left intact

    # 5. State reset — only reached on successful post
    logger.info("Successfully pushed to Slack. Cleared how_backlog.json for the next cycle.")
    with open(HOW_BACKLOG_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)


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
        choices=[*PROFILES.keys(), "how"],
        help="Signal profile to run",
    )
    args = parser.parse_args()

    # How pipeline is a two-stage cascade handled separately from PROFILES.
    if args.profile == "how":
        how_webhook = os.getenv("HOW_WEBHOOK_URL")
        if not how_webhook:
            raise EnvironmentError("HOW_WEBHOOK_URL is not set in the environment")
        run_how_pipeline(how_webhook)
        return

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

        if args.profile == "how":
            _webhook = os.getenv("HOW_WEBHOOK_URL")
            _env_name = "HOW_WEBHOOK_URL"
        else:
            _webhook = os.getenv(profile["webhook_env"])
            _env_name = profile["webhook_env"]
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
