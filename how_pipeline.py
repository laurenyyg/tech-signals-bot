"""How profile: backlog/seen-ledger state and the weekly Haiku → Sonnet synthesizer.

The daily catcher (catch_how.py) fills data/how_backlog.json; run_how_pipeline()
drains it once a week and posts to Slack.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from common import (
    MAX_TOKENS,
    MODEL_ID,
    build_footer,
    call_claude_raw,
    fetch_body,
    post_to_slack,
    strip_html,
)
from config import HOW_BODY_MAX_CHARS, HOW_SEEN_RETENTION_DAYS
from prompts import HOW_BOUNCER_PROMPT, HOW_PROFILE_PROMPT

logger = logging.getLogger(__name__)

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 1024
FOOTER_MODEL_LABEL = f"{HAIKU_MODEL_ID} + {MODEL_ID}"

BACKLOG_FILE = "data/how_backlog.json"
SEEN_FILE = "data/how_seen.json"

# Slack rejects section text over 3000 chars; leave room for the "Read more" link.
SLACK_SECTION_MAX_CHARS = 2700

# Items whose bouncer call errors stay in the backlog for retry, up to this many runs.
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# State: backlog + seen ledger
# ---------------------------------------------------------------------------

def load_json_list(path: str) -> List[Dict[str, Any]]:
    """Load a JSON array file. Missing file → []. Malformed file raises."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is malformed JSON: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a JSON array, got {type(data).__name__}")
    return data


def save_json_list(path: str, data: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def prune_seen(
    seen: List[Dict[str, Any]],
    now: datetime,
    retention_days: int = HOW_SEEN_RETENTION_DAYS,
) -> List[Dict[str, Any]]:
    """Drop ledger entries older than retention_days (and any unparsable ones)."""
    cutoff = now - timedelta(days=retention_days)
    kept: List[Dict[str, Any]] = []
    for entry in seen:
        try:
            if entry.get("url") and datetime.fromisoformat(entry["processed_at"]) >= cutoff:
                kept.append(entry)
        except (KeyError, TypeError, ValueError):
            continue
    return kept


def mark_seen(
    seen: List[Dict[str, Any]], urls: List[str], now: datetime
) -> List[Dict[str, Any]]:
    """Return the ledger with urls appended (existing URLs are not duplicated)."""
    known = {e.get("url") for e in seen}
    stamp = now.isoformat()
    return seen + [
        {"url": u, "processed_at": stamp} for u in dict.fromkeys(urls) if u and u not in known
    ]


def known_urls(backlog: List[Dict[str, Any]], seen: List[Dict[str, Any]]) -> Set[str]:
    return {i["link"] for i in backlog if i.get("link")} | {
        e["url"] for e in seen if e.get("url")
    }


# ---------------------------------------------------------------------------
# LLM parsing helpers
# ---------------------------------------------------------------------------

def parse_bouncer_verdict(text: str) -> Optional[str]:
    """Return extracted workflow text if the bouncer passed the item, else None.

    Tolerates "NULL.", "NULL — no steps", lowercase, and leading whitespace. A
    response with no verdict line is treated as a pass so Sonnet can decide.
    """
    stripped = text.strip()
    first_line, _, rest = stripped.partition("\n")
    head = first_line.strip().upper()
    if head.startswith("NULL"):
        return None
    if head.startswith("PASS"):
        return rest.strip() or None
    logger.warning("[Cascade Stage 1 - Haiku] No PASS/NULL verdict line; treating as PASS.")
    return stripped or None


def _strip_code_fences(text: str) -> str:
    match = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    return match.group(1) if match else text


def parse_sonnet_signals(
    text: str, id_to_item: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Parse Stage 2 JSON and resolve source_id → title/source/url from metadata.

    Raises ValueError on non-JSON or non-array output. Elements with unknown or
    duplicate source_ids are dropped.
    """
    try:
        raw = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Sonnet returned non-JSON output: {e}\nRaw (first 500 chars):\n{text[:500]}"
        ) from e
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array from Sonnet, got {type(raw).__name__}")

    signals: List[Dict[str, Any]] = []
    used: Set[int] = set()
    for element in raw:
        if not isinstance(element, dict):
            continue
        try:
            sid = int(element.get("source_id"))
        except (TypeError, ValueError):
            logger.warning(f"[Cascade Stage 2 - Sonnet] dropping element without valid source_id: {element}")
            continue
        if sid not in id_to_item or sid in used:
            logger.warning(f"[Cascade Stage 2 - Sonnet] dropping unknown/duplicate source_id {sid}")
            continue
        used.add(sid)
        item = id_to_item[sid]
        tech_stack = element.get("tech_stack") or []
        steps = element.get("workflow_steps") or []
        signals.append({
            "title": item.get("title", "Untitled"),
            "source": item.get("source", ""),
            "url": item.get("link", ""),
            "tech_stack": [str(t) for t in tech_stack] if isinstance(tech_stack, list) else [str(tech_stack)],
            "workflow_steps": [str(s) for s in steps] if isinstance(steps, list) else [str(steps)],
            "roi_impact": str(element.get("roi_impact", "") or ""),
        })
    return signals


def format_sonnet_batch(passed: List[Tuple[int, Dict[str, Any], str]]) -> str:
    lines: List[str] = [
        f"Structure the following {len(passed)} pre-extracted workflow(s) per your mandate:\n"
    ]
    for sid, item, extracted in passed:
        lines.append(f"[{sid}] Title: {item.get('title', '')}")
        lines.append(f"Source: {item.get('source', '')}")
        lines.append(f"Extracted workflow:\n{extracted}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _funnel_line(source: str, counts: Dict[str, int]) -> str:
    line = (
        f"📡 *{source}*   {counts.get('caught', 0)} caught  ›  "
        f"{counts.get('passed', 0)} passed bouncer  ›  "
        f"{counts.get('workflows', 0)} workflow(s)"
    )
    if counts.get("errored"):
        line += f"  ({counts['errored']} errored)"
    return line


def build_how_slack_payload(
    signals: List[Dict[str, Any]],
    funnel: Optional[Dict[str, Dict[str, int]]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
) -> Dict[str, Any]:
    footer = build_footer(
        total_tokens, budget_status, model_label=FOOTER_MODEL_LABEL, max_tokens=max_tokens
    )
    funnel_lines = [_funnel_line(src, c) for src, c in (funnel or {}).items()]

    if not signals:
        message = (
            "_No replicable AI tool workflows found in the backlog._"
            if funnel
            else "_No new items were caught this week._"
        )
        return {
            "text": "How Signals Digest — no workflow SOPs found.",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join([message] + funnel_lines)},
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
        header_line = f"*{signal.get('title', 'Untitled')}*"
        if signal.get("source"):
            header_line += f" — _{signal['source']}_"

        lines = [header_line]
        if signal.get("tech_stack"):
            lines.append(f"🛠️ *Tech Stack*: {', '.join(signal['tech_stack'])}")
        if signal.get("workflow_steps"):
            bullets = "\n".join(f"• {step}" for step in signal["workflow_steps"])
            lines.append(f"📝 *Workflow SOP*:\n{bullets}")
        if signal.get("roi_impact"):
            lines.append(f"💰 *ROI/Impact*: {signal['roi_impact']}")
        text = "\n".join(lines)
        if len(text) > SLACK_SECTION_MAX_CHARS:
            text = text[: SLACK_SECTION_MAX_CHARS - 1] + "…"
        if signal.get("url"):
            text += f"\n_<{signal['url']}|Read more>_"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})

    if funnel_lines:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": line} for line in funnel_lines],
        })

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}]})

    return {
        "text": f"How Signals Digest — {len(signals)} workflow(s)",
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

def _item_text(item: Dict[str, Any]) -> str:
    """Full body if the catcher stored one; else re-fetch; else the feed summary."""
    body: str = item.get("body") or ""
    if not body:
        body = fetch_body(item.get("link", ""), HOW_BODY_MAX_CHARS)
    if not body:
        body = strip_html(item.get("summary", "") or "")
    return body


def run_how_pipeline(webhook_url: str, usage_meta: Dict[str, Any]) -> None:
    """Two-stage LLM cascade for the How profile.

    Stage 1 — Haiku bouncer: one call per backlog item on title + full body.
    Stage 2 — Sonnet formatter: one batched call over every item that passed.

    usage_meta is updated in place (total_tokens, budget_status, max_tokens) so
    the caller's crash alert can report spend. Raises if every bouncer call
    errors or Stage 2 fails. On a successful Slack post, processed items are
    removed from the backlog and recorded in the seen ledger.
    """
    now = datetime.now(timezone.utc)
    backlog = load_json_list(BACKLOG_FILE)
    logger.info(f"How pipeline: {len(backlog)} items in backlog.")

    total_tokens = 0
    budget_cap = 0
    budget_hit = False

    def _record(tokens_in: int, tokens_out: int, cap: int, stop_reason: str) -> None:
        nonlocal total_tokens, budget_cap, budget_hit
        total_tokens += tokens_in + tokens_out
        budget_cap += cap
        budget_hit = budget_hit or stop_reason == "max_tokens" or tokens_out >= cap
        usage_meta.update(
            total_tokens=total_tokens,
            budget_status="Hit" if budget_hit else "OK",
            max_tokens=budget_cap,
        )

    funnel: Dict[str, Dict[str, int]] = {}
    for item in backlog:
        counts = funnel.setdefault(item.get("source", "unknown"), {})
        counts["caught"] = counts.get("caught", 0) + 1

    # Stage 1 — Haiku bouncer
    passed: List[Tuple[int, Dict[str, Any], str]] = []
    errored: List[Dict[str, Any]] = []
    processed_urls: List[str] = []
    first_error: Optional[str] = None

    for item in backlog:
        url: str = item.get("link", "")
        counts = funnel[item.get("source", "unknown")]
        text = _item_text(item)
        try:
            haiku_text, h_in, h_out, h_stop = call_claude_raw(
                model_id=HAIKU_MODEL_ID,
                system=HOW_BOUNCER_PROMPT,
                user_message=f"Title: {item.get('title', '')}\n\n{text}",
                max_tokens=HAIKU_MAX_TOKENS,
            )
        except Exception as e:
            logger.error(f"[Cascade Stage 1 - Haiku] Error for {url}: {e}")
            first_error = first_error or str(e)
            counts["errored"] = counts.get("errored", 0) + 1
            errored.append(item)
            continue

        _record(h_in, h_out, HAIKU_MAX_TOKENS, h_stop)
        processed_urls.append(url)
        extracted = parse_bouncer_verdict(haiku_text)
        if extracted is None:
            logger.info(f"[Cascade Stage 1 - Haiku] {url}: NULL, discarded ({h_in} input tokens).")
            continue
        logger.info(f"[Cascade Stage 1 - Haiku] {url}: PASS ({h_in} input tokens).")
        counts["passed"] = counts.get("passed", 0) + 1
        passed.append((len(passed) + 1, item, extracted))

    if backlog and len(errored) == len(backlog):
        raise RuntimeError(
            f"All {len(backlog)} Haiku bouncer calls failed. First error: {first_error}"
        )

    # Stage 2 — one batched Sonnet call
    signals: List[Dict[str, Any]] = []
    if passed:
        id_to_item = {sid: item for sid, item, _ in passed}
        sonnet_text, s_in, s_out, s_stop = call_claude_raw(
            model_id=MODEL_ID,
            system=HOW_PROFILE_PROMPT,
            user_message=format_sonnet_batch(passed),
            max_tokens=MAX_TOKENS,
            use_thinking=True,
        )
        _record(s_in, s_out, MAX_TOKENS, s_stop)
        logger.info(f"[Cascade Stage 2 - Sonnet] {len(passed)} item(s), {s_in} input tokens.")
        signals = parse_sonnet_signals(sonnet_text, id_to_item)
        for signal in signals:
            counts = funnel.setdefault(signal["source"] or "unknown", {})
            counts["workflows"] = counts.get("workflows", 0) + 1

    logger.info(
        f"[FinOps] How pipeline complete. Total tokens: {total_tokens}/{budget_cap}. "
        f"Budget: {usage_meta.get('budget_status', 'OK')}. Signals: {len(signals)}. "
        f"Errored: {len(errored)}."
    )

    payload = build_how_slack_payload(
        signals,
        funnel=funnel,
        total_tokens=usage_meta.get("total_tokens", 0),
        budget_status=usage_meta.get("budget_status", "OK"),
        max_tokens=budget_cap,
    )
    post_to_slack(webhook_url, payload)  # raises on failure — state left intact

    # State update — only reached on successful post
    retry: List[Dict[str, Any]] = []
    for item in errored:
        attempts = int(item.get("attempts", 0)) + 1
        if attempts < MAX_ATTEMPTS:
            retry.append({**item, "attempts": attempts})
        else:
            logger.warning(f"Dropping {item.get('link')} after {attempts} failed attempts.")
            processed_urls.append(item.get("link", ""))

    seen = mark_seen(prune_seen(load_json_list(SEEN_FILE), now), processed_urls, now)
    save_json_list(SEEN_FILE, seen)
    save_json_list(BACKLOG_FILE, retry)
    logger.info(
        f"Backlog reset ({len(retry)} item(s) kept for retry); "
        f"seen ledger now has {len(seen)} URL(s)."
    )
