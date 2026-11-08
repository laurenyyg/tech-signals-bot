"""How profile: backlog/seen-ledger state and the weekly Haiku → Sonnet synthesizer.

The daily catcher (catch_how.py) fills data/how_backlog.json; run_how_pipeline()
drains it once a week and posts to Slack.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import seen_ledger
from common import (
    MAX_TOKENS,
    MODEL_ID,
    build_footer,
    call_claude_raw,
    fetch_body,
    post_to_slack,
    strip_code_fences,
    strip_html,
)
from config import HOW_BODY_MAX_CHARS, HOW_SEEN_RETENTION_DAYS
from prompts import HOW_BOUNCER_PROMPT, HOW_PROFILE_PROMPT
from seen_ledger import load_json_list, mark_seen, save_json_list

logger = logging.getLogger(__name__)

HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 1024
FOOTER_MODEL_LABEL = f"{HAIKU_MODEL_ID} + {MODEL_ID}"

BACKLOG_FILE = "data/how_backlog.json"
SEEN_FILE = "data/how_seen.json"

# Slack rejects section text over 3000 chars; leave room for the "Read more" link.
SLACK_SECTION_MAX_CHARS = 2700
# Slack allows 50 blocks per message; each card is 2 blocks plus 4 for header/funnel/footer.
SLACK_MAX_CARDS_PER_MESSAGE = 20

# Items whose bouncer call errors stay in the backlog for retry, up to this many runs.
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# State: backlog + seen ledger
# ---------------------------------------------------------------------------

def prune_seen(
    seen: List[Dict[str, Any]],
    now: datetime,
    retention_days: int = HOW_SEEN_RETENTION_DAYS,
) -> List[Dict[str, Any]]:
    return seen_ledger.prune_seen(seen, now, retention_days)


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


def dedupe_tools(tools: List[Any]) -> List[str]:
    """Drop blanks and case-insensitive duplicates; fold "X MCP" into "X" when X is listed."""
    names = [str(t).strip() for t in tools if str(t).strip()]
    lowered = {n.lower() for n in names}
    out: List[str] = []
    seen: Set[str] = set()
    for name in names:
        key = name.lower()
        if key.endswith(" mcp") and key[: -len(" mcp")] in lowered:
            continue
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def parse_sonnet_signals(
    text: str, id_to_item: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Parse Stage 2 JSON and resolve source_id → title/source/url from metadata.

    Raises ValueError on non-JSON or non-array output. Elements with unknown or
    duplicate source_ids are dropped.
    """
    try:
        raw = json.loads(strip_code_fences(text))
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
            "tech_stack": dedupe_tools(tech_stack if isinstance(tech_stack, list) else [tech_stack]),
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
    passed = f"{counts.get('passed', 0)} passed bouncer"
    if counts.get("structured"):
        passed += f" ({counts['structured']} pre-structured, not bounced)"
    line = (
        f"📡 *{source}*   {counts.get('caught', 0)} caught  ›  {passed}  ›  "
        f"{counts.get('workflows', 0)} workflow(s)"
    )
    if counts.get("errored"):
        line += f"  ({counts['errored']} errored)"
    return line


def _signal_section(signal: Dict[str, Any]) -> Dict[str, Any]:
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
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_how_slack_payloads(
    signals: List[Dict[str, Any]],
    funnel: Optional[Dict[str, Dict[str, int]]] = None,
    total_tokens: Optional[int] = None,
    budget_status: Optional[str] = None,
    max_tokens: int = MAX_TOKENS,
    model_label: str = FOOTER_MODEL_LABEL,
) -> List[Dict[str, Any]]:
    """Build the How digest as one or more Slack messages.

    Cards are split across messages of at most SLACK_MAX_CARDS_PER_MESSAGE so no
    message exceeds Slack's 50-block limit. The funnel and FinOps footer go on
    the last message only.
    """
    footer = build_footer(
        total_tokens, budget_status, model_label=model_label, max_tokens=max_tokens
    )
    funnel_lines = [_funnel_line(src, c) for src, c in (funnel or {}).items()]
    footer_block = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}]}

    if not signals:
        message = (
            "_No replicable AI tool workflows found in the backlog._"
            if funnel
            else "_No new items were caught this week._"
        )
        return [{
            "text": "How Signals Digest — no workflow SOPs found.",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join([message] + funnel_lines)},
                },
                footer_block,
            ],
        }]

    chunks = [
        signals[i : i + SLACK_MAX_CARDS_PER_MESSAGE]
        for i in range(0, len(signals), SLACK_MAX_CARDS_PER_MESSAGE)
    ]
    payloads: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        title = f"How Signals Digest — {len(signals)} workflow(s)"
        if len(chunks) > 1:
            title += f" ({index}/{len(chunks)})"

        blocks: List[Dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
            {"type": "divider"},
        ]
        for signal in chunk:
            blocks.append(_signal_section(signal))
            blocks.append({"type": "divider"})

        if index == len(chunks):
            if funnel_lines:
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": line} for line in funnel_lines],
                })
            blocks.append(footer_block)

        payloads.append({"text": title, "blocks": blocks})
    return payloads


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
      Pre-structured items (ChatPRD HowTo pages, kind == "howto") skip it.
    Stage 2 — Sonnet formatter: one batched call over every item that passed.

    usage_meta is updated in place (total_tokens, budget_status, max_tokens) so
    the caller's crash alert can report spend. Raises if every bouncer call
    errors and nothing else passed, or if Stage 2 fails. On a successful Slack post, processed items are
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

    bounced = 0
    for item in backlog:
        url: str = item.get("link", "")
        counts = funnel[item.get("source", "unknown")]
        text = _item_text(item)

        if item.get("kind") == "howto" and item.get("body"):
            logger.info(f"[Cascade Stage 1] {url}: pre-structured HowTo, skipping bouncer.")
            processed_urls.append(url)
            counts["passed"] = counts.get("passed", 0) + 1
            counts["structured"] = counts.get("structured", 0) + 1
            passed.append((len(passed) + 1, item, text))
            continue

        bounced += 1
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

    # A dead bouncer with nothing else to post is a crash; with pre-structured items
    # still flowing, the errors are surfaced in the funnel line instead.
    if bounced and len(errored) == bounced and not passed:
        raise RuntimeError(
            f"All {bounced} Haiku bouncer calls failed. First error: {first_error}"
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

    payloads = build_how_slack_payloads(
        signals,
        funnel=funnel,
        total_tokens=usage_meta.get("total_tokens", 0),
        budget_status=usage_meta.get("budget_status", "OK"),
        max_tokens=budget_cap,
        model_label=" + ".join(
            m for m, used in ((HAIKU_MODEL_ID, bounced > 0), (MODEL_ID, bool(passed))) if used
        ) or FOOTER_MODEL_LABEL,
    )
    for payload in payloads:
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
