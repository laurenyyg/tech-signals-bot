"""Shared helpers used by main.py, the How catcher, and the How synthesizer."""
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import anthropic
import requests
import trafilatura

logger = logging.getLogger(__name__)

# Update AGENTS.md when changing this; the Slack footer is derived from it.
MODEL_ID = "claude-sonnet-4-6"
MAX_TOKENS = 16000  # Output token budget; used in API call and Slack footer.

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)


def proxies_from_env() -> Optional[Dict[str, str]]:
    proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_code_fences(text: str) -> str:
    match = re.match(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    return match.group(1) if match else text


def date_range_str(days: int) -> str:
    """'Oct 31 – Nov 8' for the window ending now."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    fmt = lambda d: f"{d.strftime('%b')} {d.day}"
    return f"{fmt(start)} – {fmt(now)}"


def preprocess_article(url: str, html_content: str) -> str:
    """Extract clean article text from raw HTML.

    Primary path: trafilatura.extract(). Fallback: strip_html().
    """
    raw_len = len(html_content)
    cleaned = trafilatura.extract(
        html_content, include_comments=False, include_tables=False
    )
    if not cleaned:
        logger.warning(
            "trafilatura returned no content for %s — falling back to strip_html()", url
        )
        cleaned = strip_html(html_content)
    logger.info(
        "Pre-processing URL: %s. Reduced raw HTML from %d chars to %d chars of clean text.",
        url, raw_len, len(cleaned),
    )
    return cleaned


def fetch_body(url: str, max_chars: int) -> str:
    """Download an article page and return its extracted text ("" on any failure)."""
    if not url:
        return ""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            proxies=proxies_from_env(),
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"[fetch_body] failed to fetch {url}: {e}")
        return ""
    try:
        return preprocess_article(url, r.text)[:max_chars]
    except Exception as e:
        logger.warning(f"[fetch_body] failed to extract {url}: {e}")
        return ""


def call_claude_raw(
    model_id: str,
    system: str,
    user_message: str,
    max_tokens: int,
    use_thinking: bool = False,
) -> Tuple[str, int, int, str]:
    """Low-level Anthropic call.

    Returns (response_text, input_tokens, output_tokens, stop_reason). Pass
    use_thinking=False for models that do not support adaptive thinking (Haiku).
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

    text_content = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    if not text_content:
        raise ValueError(f"Anthropic response from {model_id} contained no text block")

    return (
        text_content,
        response.usage.input_tokens,
        response.usage.output_tokens,
        response.stop_reason or "",
    )


def build_footer(
    total_tokens: Optional[int],
    budget_status: Optional[str],
    model_label: str = MODEL_ID,
    max_tokens: int = MAX_TOKENS,
) -> str:
    tokens = f"{total_tokens}/{max_tokens}" if total_tokens is not None else "N/A"
    budget = budget_status if budget_status is not None else "N/A"
    return f"_Model: {model_label} | Tokens: {tokens} | Budget: {budget}_"


def build_error_payload(error_msg: str, footer: str) -> Dict[str, Any]:
    prefix, suffix = "🚨 *Alert: Pipeline Failed*\n```", "```"
    error_msg = fit_text(error_msg, SLACK_TEXT_MAX_CHARS - len(prefix) - len(suffix))
    return {
        "text": "🚨 Tech Signals Bot — Pipeline Error",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{prefix}{error_msg}{suffix}"},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"\n{footer}"}],
            },
        ],
    }


def post_to_slack(webhook_url: str, payload: Dict[str, Any]) -> None:
    response = requests.post(webhook_url, json=payload, timeout=10)
    if response.status_code != 200:
        raise RuntimeError(
            f"Slack webhook returned {response.status_code}: {response.text}"
        )
    logger.info("Slack post successful")


# ---------------------------------------------------------------------------
# Slack message splitting (shared by every profile)
# ---------------------------------------------------------------------------

Block = Dict[str, Any]

SLACK_MAX_BLOCKS = 50
SLACK_TEXT_MAX_CHARS = 3000  # section text and each context element
SLACK_HEADER_MAX_CHARS = 150
SLACK_CONTEXT_MAX_ELEMENTS = 10
# Sum of block text per message; a safety margin well under Slack's message limits.
SLACK_MESSAGE_MAX_CHARS = 30000
# Incoming webhooks allow about one message per second.
SLACK_POST_PAUSE_SECONDS = 1.0


def fit_text(text: str, limit: int = SLACK_TEXT_MAX_CHARS) -> str:
    """Truncate text to at most limit chars, ending with an ellipsis when cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def section_block(text: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": fit_text(text)}}


def context_blocks(lines: List[str]) -> List[Block]:
    """Pack lines into context blocks without exceeding per-element or per-block limits.

    Lines are joined with newlines into elements of at most SLACK_TEXT_MAX_CHARS;
    a single over-long line is truncated.
    """
    elements: List[str] = []
    for line in (fit_text(l) for l in lines):
        if elements and len(elements[-1]) + 1 + len(line) <= SLACK_TEXT_MAX_CHARS:
            elements[-1] += "\n" + line
        else:
            elements.append(line)
    return [
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": e}
                for e in elements[i : i + SLACK_CONTEXT_MAX_ELEMENTS]
            ],
        }
        for i in range(0, len(elements), SLACK_CONTEXT_MAX_ELEMENTS)
    ]


def _block_chars(block: Block) -> int:
    text = block.get("text")
    total = len(text.get("text", "")) if isinstance(text, dict) else 0
    for element in block.get("elements", []) or []:
        if isinstance(element, dict):
            total += len(str(element.get("text", "")))
    return total


def split_into_messages(
    title: str,
    groups: List[List[Block]],
    tail: List[Block],
    max_blocks: int = SLACK_MAX_BLOCKS,
    max_chars: int = SLACK_MESSAGE_MAX_CHARS,
) -> List[Dict[str, Any]]:
    """Lay out a digest as one or more Slack messages.

    Each message starts with a header (title, plus "(i/N)" when there are several)
    and a divider. A group (e.g. a card and its divider) is never split across
    messages, and no block is ever split. The tail (warnings, funnel, footer) goes
    on the last message, or on a message of its own if it doesn't fit there.
    """
    overhead = 2  # header + divider

    def fits(chunk: List[Block], extra: List[Block]) -> bool:
        blocks = chunk + extra
        return (
            overhead + len(blocks) <= max_blocks
            and sum(_block_chars(b) for b in blocks) <= max_chars
        )

    chunks: List[List[Block]] = [[]]
    for group in groups:
        if not fits([], group):
            raise ValueError(f"a block group of {len(group)} blocks cannot fit in one message")
        if not fits(chunks[-1], group):
            chunks.append([])
        chunks[-1].extend(group)
    if not fits(chunks[-1], tail):
        chunks.append([])
    chunks[-1].extend(tail)

    payloads: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
        header = fit_text(title, SLACK_HEADER_MAX_CHARS - len(suffix)) + suffix
        payloads.append({
            "text": header,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
                {"type": "divider"},
                *chunk,
            ],
        })
    return payloads


def post_payloads(
    webhook_url: str,
    payloads: List[Dict[str, Any]],
    post: Callable[[str, Dict[str, Any]], None] = post_to_slack,
) -> None:
    """Post messages in order; raises on the first failure (later ones are not sent).

    Callers pass their module's post_to_slack so tests can patch it there.
    """
    for index, payload in enumerate(payloads):
        if index:
            time.sleep(SLACK_POST_PAUSE_SECONDS)
        post(webhook_url, payload)
