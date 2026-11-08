"""Why profile: fetch essays, summarise each with its own Sonnet call, post one digest.

One call per item keeps a long week from exhausting a single output budget, and
one bad response only drops that item. Title, author, date, type, and URL come
from fetcher metadata; the LLM returns only the summary and the deep-read flag.

data/why_seen.json records posted items so the overlapping fetch window (and
month-only dates, which stay in window all month) never repeat an item.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from common import (
    MODEL_ID,
    build_footer,
    call_claude_raw,
    date_range_str,
    post_to_slack,
    strip_code_fences,
    strip_html,
)
from config import SUMMARY_TRUNCATE_CHARS, WHY_ITEM_MAX_TOKENS, WHY_SEEN_RETENTION_DAYS
from fetchers.why_fetcher import DAYS_FILTER, WhyFetcher
from prompts import WHY_PROFILE_PROMPT
from seen_ledger import canonical_url, load_json_list, mark_seen, prune_seen, save_json_list

logger = logging.getLogger(__name__)

SEEN_FILE = "data/why_seen.json"

# Slack rejects section text over 3000 chars; leave room for the source link.
SLACK_SECTION_MAX_CHARS = 2700
ERROR_DETAIL_MAX_CHARS = 200

Item = Dict[str, Any]


# ---------------------------------------------------------------------------
# Seen ledger
# ---------------------------------------------------------------------------

def ledger_key(item: Item) -> str:
    """Canonical RSS guid, else canonical link, else source+title.

    canonical_url drops Stratechery's access_token, so no credential reaches the ledger.
    """
    url = item.get("guid") or item.get("link") or ""
    if url:
        return canonical_url(url)
    return f"{item.get('source', '')}:{item.get('title', '').strip().lower()}"


def drop_seen(items: List[Item], seen_keys: Set[str]) -> Tuple[List[Item], int]:
    """Return (unseen items, number dropped as already posted)."""
    fresh = [item for item in items if ledger_key(item) not in seen_keys]
    return fresh, len(items) - len(fresh)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def format_item_message(item: Item) -> str:
    lines = [
        f"Title: {item.get('title', '')}",
        f"Author: {item.get('author', '') or 'unknown'}",
        f"Type: {item.get('content_type', 'essay')}",
    ]
    body = strip_html(item.get("summary", "") or "")[:SUMMARY_TRUNCATE_CHARS]
    lines.append(f"Text:\n{body or '(no text available; title only)'}")
    return "\n".join(lines)


def parse_why_response(text: str) -> Tuple[List[str], bool]:
    """Return (summary bullets, is_deep_read). Raises ValueError on invalid output."""
    try:
        raw = json.loads(strip_code_fences(text))
    except json.JSONDecodeError as e:
        raise ValueError(f"non-JSON output: {e}; raw (first 200 chars): {text[:200]!r}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    summary = raw.get("summary")
    bullets = summary if isinstance(summary, list) else [summary]
    bullets = [str(b).strip() for b in bullets if b is not None and str(b).strip()]
    if not bullets:
        raise ValueError("response has no summary bullets")
    return bullets, raw.get("is_deep_read") is True


def summarize_items(
    items: List[Item], usage_meta: Dict[str, Any]
) -> Tuple[List[Item], List[Tuple[Item, str]]]:
    """Summarise each item; return (signals, failures as (item, error)).

    usage_meta is updated in place after every call so a crash footer reports spend.
    """
    signals: List[Item] = []
    failures: List[Tuple[Item, str]] = []
    total_tokens = 0
    budget_cap = 0
    budget_hit = False

    for index, item in enumerate(items, 1):
        label = f"[{index}/{len(items)}] {item.get('title', '')!r}"
        try:
            text, t_in, t_out, stop = call_claude_raw(
                model_id=MODEL_ID,
                system=WHY_PROFILE_PROMPT,
                user_message=format_item_message(item),
                max_tokens=WHY_ITEM_MAX_TOKENS,
                use_thinking=True,
            )
        except Exception as e:
            logger.error(f"{label}: LLM call failed: {e}")
            failures.append((item, str(e)))
            continue

        total_tokens += t_in + t_out
        budget_cap += WHY_ITEM_MAX_TOKENS
        budget_hit = budget_hit or stop == "max_tokens"
        usage_meta.update(
            total_tokens=total_tokens,
            budget_status="Hit" if budget_hit else "OK",
            max_tokens=budget_cap,
        )
        logger.info(f"{label}: {t_in} input / {t_out} output tokens, stop={stop}")

        try:
            if stop == "max_tokens":
                raise ValueError(f"hit the {WHY_ITEM_MAX_TOKENS}-token output budget")
            bullets, is_deep_read = parse_why_response(text)
        except ValueError as e:
            logger.error(f"{label}: {e}")
            failures.append((item, str(e)))
            continue

        signals.append({**item, "summary": bullets, "is_deep_read": is_deep_read})

    return signals, failures


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _prefix(signal: Item) -> str:
    content_type = signal.get("content_type", "essay")
    if content_type == "podcast":
        return "🎙"
    if content_type == "presentation":
        return "🗂"
    return "❗️" if signal.get("is_deep_read") else "🧠"


def _signal_section(signal: Item) -> Dict[str, Any]:
    content_type: str = signal.get("content_type", "essay")
    header_line = f"{_prefix(signal)} *{signal.get('title', 'Untitled')}*"
    if signal.get("author"):
        header_line += f" — _{signal['author']}_"
    date_fmt = "%b %Y" if signal.get("date_precision") == "month" else "%b %-d"
    try:
        header_line += f" · {datetime.fromisoformat(signal.get('published', '')).strftime(date_fmt)}"
    except ValueError:
        pass
    if content_type == "presentation":
        header_line += "  `[Presentation]`"

    text = "\n".join([header_line] + [f"• {b}" for b in signal.get("summary", [])])
    if len(text) > SLACK_SECTION_MAX_CHARS:
        text = text[: SLACK_SECTION_MAX_CHARS - 1] + "…"

    read_label = {"presentation": "Find out more", "podcast": "Read transcript"}.get(
        content_type, "Read essay"
    )
    if signal.get("link"):
        text += f"\n• _<{signal['link']}|{read_label}>_"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _short(error: str) -> str:
    error = " ".join(error.split())
    return error if len(error) <= ERROR_DETAIL_MAX_CHARS else error[: ERROR_DETAIL_MAX_CHARS - 1] + "…"


def build_why_slack_payload(
    signals: List[Item],
    source_counts: Optional[Dict[str, int]] = None,
    source_errors: Optional[Dict[str, str]] = None,
    failures: Optional[List[Tuple[Item, str]]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
    max_tokens: int = 0,
    already_posted: int = 0,
) -> Dict[str, Any]:
    footer = build_footer(total_tokens, budget_status, max_tokens=max_tokens)
    title = f"Why Signals Digest — {len(signals)} item(s) | {date_range_str(DAYS_FILTER)}"

    blocks: List[Dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "divider"},
    ]
    if not signals and not failures:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"_No new essays or presentations in the last {DAYS_FILTER} days._",
            },
        })
    for signal in signals:
        blocks.append(_signal_section(signal))
        blocks.append({"type": "divider"})

    warnings: List[str] = []
    for source, error in (source_errors or {}).items():
        warnings.append(f"⚠️ *{source}* fetch failed: {_short(error)}")
    for item, error in failures or []:
        name = item.get("title", "Untitled")
        if item.get("link"):
            name = f"<{item['link']}|{name}>"
        warnings.append(f"⚠️ Not summarised: {name} — {_short(error)}")
    if warnings:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "\n".join(warnings)}],
        })

    if source_counts:
        counts_str = "  ·  ".join(f"{source}: {count}" for source, count in source_counts.items())
        if already_posted:
            counts_str += f"  ({already_posted} already posted, skipped)"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"📡 {counts_str}"}]})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}]})
    return {"text": title, "blocks": blocks}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_why_pipeline(webhook_url: str, usage_meta: Dict[str, Any]) -> None:
    """Fetch, drop already-posted items, summarise per item, and post the weekly digest.

    Raises if every LLM call fails (the caller posts a 🚨 alert). Source fetch
    failures and individual item failures are listed in the digest instead.
    Only after a successful Slack post are the summarised items added to the
    seen ledger; failed items stay eligible for next week's run.
    """
    now = datetime.now(timezone.utc)
    seen = prune_seen(load_json_list(SEEN_FILE), now, WHY_SEEN_RETENTION_DAYS)

    fetcher = WhyFetcher()
    items, already_posted = drop_seen(fetcher.fetch(), {e["url"] for e in seen})
    if already_posted:
        logger.info(f"Seen ledger: skipped {already_posted} already-posted item(s).")

    signals, failures = summarize_items(items, usage_meta)
    if items and not signals:
        raise RuntimeError(
            f"All {len(items)} Why summaries failed. First error: {failures[0][1]}"
        )

    logger.info(
        f"[FinOps] Why pipeline complete. Total tokens: {usage_meta.get('total_tokens', 0)}/"
        f"{usage_meta.get('max_tokens', 0)}. Budget: {usage_meta.get('budget_status', 'OK')}. "
        f"Signals: {len(signals)}. Failed items: {len(failures)}. "
        f"Failed sources: {len(fetcher.source_errors)}."
    )

    payload = build_why_slack_payload(
        signals,
        source_counts=fetcher.source_counts,
        source_errors=fetcher.source_errors,
        failures=failures,
        total_tokens=usage_meta.get("total_tokens", 0),
        budget_status=usage_meta.get("budget_status", "OK"),
        max_tokens=usage_meta.get("max_tokens", 0),
        already_posted=already_posted,
    )
    post_to_slack(webhook_url, payload)  # raises on failure — ledger left untouched

    seen = mark_seen(seen, [ledger_key(s) for s in signals], now)
    save_json_list(SEEN_FILE, seen)
    logger.info(f"Seen ledger now has {len(seen)} item(s).")
