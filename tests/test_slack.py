"""Unit tests for the shared Slack message splitter in common.py (no live HTTP)."""
from typing import Any, Dict, List

import pytest

import common
from common import (
    SLACK_MAX_BLOCKS,
    SLACK_TEXT_MAX_CHARS,
    build_error_payload,
    context_blocks,
    fit_text,
    post_payloads,
    split_into_messages,
)


def _card(i: int, chars: int = 20) -> List[Dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"card {i} " + "x" * chars}},
        {"type": "divider"},
    ]


FOOTER = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "_Model: m_"}]}]


def _sections(payload: Dict[str, Any]) -> List[str]:
    return [b["text"]["text"] for b in payload["blocks"] if b["type"] == "section"]


def test_fit_text() -> None:
    assert fit_text("short", 10) == "short"
    assert fit_text("x" * 20, 10) == "x" * 9 + "…"


def test_single_message_has_no_page_suffix() -> None:
    payloads = split_into_messages("Digest", [_card(1)], FOOTER)
    assert len(payloads) == 1
    assert payloads[0]["blocks"][0]["text"]["text"] == "Digest"
    assert payloads[0]["blocks"][-1] == FOOTER[0]


def test_splits_on_block_limit_and_keeps_groups_whole() -> None:
    payloads = split_into_messages("Digest", [_card(i) for i in range(45)], FOOTER)
    assert len(payloads) == 2
    assert all(len(p["blocks"]) <= SLACK_MAX_BLOCKS for p in payloads)
    assert payloads[0]["text"] == "Digest (1/2)" and payloads[1]["text"] == "Digest (2/2)"
    for p in payloads:
        body = p["blocks"][2:]
        sections = [i for i, b in enumerate(body) if b["type"] == "section"]
        assert all(body[i + 1]["type"] == "divider" for i in sections)
    assert sum(len(_sections(p)) for p in payloads) == 45
    assert FOOTER[0] not in payloads[0]["blocks"] and payloads[1]["blocks"][-1] == FOOTER[0]


def test_splits_on_char_budget() -> None:
    payloads = split_into_messages(
        "Digest", [_card(i, chars=2900) for i in range(12)], FOOTER, max_chars=10000
    )
    assert len(payloads) == 4
    assert all(sum(len(t) for t in _sections(p)) <= 10000 for p in payloads)


def test_tail_gets_own_message_when_last_is_full() -> None:
    groups = [_card(i) for i in range(24)]  # 2 + 48 blocks = exactly full
    payloads = split_into_messages("Digest", groups, FOOTER)
    assert len(payloads) == 2
    assert payloads[1]["blocks"][2:] == FOOTER


def test_no_groups_still_posts_tail() -> None:
    payloads = split_into_messages("Digest", [], FOOTER)
    assert len(payloads) == 1 and payloads[0]["blocks"][-1] == FOOTER[0]


def test_oversized_group_raises() -> None:
    with pytest.raises(ValueError, match="cannot fit"):
        split_into_messages("Digest", [[{"type": "divider"}] * 60], FOOTER)


def test_header_truncated_but_keeps_page_suffix() -> None:
    payloads = split_into_messages("T" * 200, [_card(i) for i in range(30)], FOOTER)
    header = payloads[0]["blocks"][0]["text"]["text"]
    assert len(header) <= 150 and header.endswith("(1/2)")


def test_context_blocks_respect_element_and_block_limits() -> None:
    lines = [f"line {i} " + "y" * 900 for i in range(40)]
    blocks = context_blocks(lines)
    elements = [e["text"] for b in blocks for e in b["elements"]]
    assert all(len(e) <= SLACK_TEXT_MAX_CHARS for e in elements)
    assert all(len(b["elements"]) <= 10 for b in blocks)
    assert "\n".join(elements).count("line ") == 40
    assert context_blocks([]) == []


def test_error_payload_fits_section_limit() -> None:
    payload = build_error_payload("Traceback " * 1000, "_footer_")
    assert len(payload["blocks"][0]["text"]["text"]) <= SLACK_TEXT_MAX_CHARS
    assert payload["blocks"][0]["text"]["text"].endswith("…```")


def test_post_payloads_in_order_and_stops_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "SLACK_POST_PAUSE_SECONDS", 0)
    sent: List[str] = []

    def post(url: str, payload: Dict[str, Any]) -> None:
        if payload["text"] == "b":
            raise RuntimeError("Slack webhook returned 500")
        sent.append(payload["text"])

    with pytest.raises(RuntimeError, match="500"):
        post_payloads("https://hook", [{"text": "a"}, {"text": "b"}, {"text": "c"}], post=post)
    assert sent == ["a"]


# ---------------------------------------------------------------------------
# What profile uses the same splitter
# ---------------------------------------------------------------------------

def test_what_payloads_split_and_keep_funnel_on_last_message() -> None:
    from main import build_slack_payloads

    signals = [
        {
            "macro_event": f"Event {i}", "core_fact": "f" * 600, "eli5": "e" * 600,
            "investment_impact": "i" * 600, "source_urls": [f"https://x/{i}"],
        }
        for i in range(30)
    ]
    source_counts = {"techmeme": {"fetched": 40, "in_window": 30, "relevant": 20, "sent_to_llm": 20}}
    payloads = build_slack_payloads(signals, source_counts, total_tokens=1, budget_status="OK")

    assert len(payloads) > 1
    for p in payloads:
        assert len(p["blocks"]) <= SLACK_MAX_BLOCKS
        assert p["blocks"][0]["type"] == "header"
    cards = sum(1 for p in payloads for b in p["blocks"] if b["type"] == "section")
    assert cards == 30
    last = str(payloads[-1])
    assert "📡 *<https://www.techmeme.com/|Techmeme>*" in last and "Model:" in last
    assert all("Model:" not in str(p) for p in payloads[:-1])


def test_what_payloads_empty_is_single_message() -> None:
    from main import build_slack_payloads

    payloads = build_slack_payloads([], {}, total_tokens=0, budget_status="OK")
    assert len(payloads) == 1 and "No structurally significant" in str(payloads)


def test_what_payloads_empty_keeps_warnings_and_funnel() -> None:
    from main import build_slack_payloads

    source_counts = {
        "techmeme": {"fetch_failed": 1},
        "techcrunch": {"fetched": 10, "in_window": 0},
    }
    payloads = build_slack_payloads(
        [],
        source_counts,
        total_tokens=0,
        budget_status="OK",
        source_errors={"techmeme": "feed returned no entries"},
    )
    assert len(payloads) == 1
    text = str(payloads[0])
    assert "No structurally significant" in text
    assert "⚠️ *techmeme* fetch failed: feed returned no entries" in text
    assert "📡 *<https://www.techmeme.com/|Techmeme>*   fetch failed" in text
    assert "📡 quiet: *<https://techcrunch.com/|Techcrunch>*" in text
    assert "Model:" in text
    assert "Sheets" not in text


def test_what_payloads_footer_links_spreadsheet() -> None:
    from main import build_slack_payloads

    payloads = build_slack_payloads(
        [],
        {},
        total_tokens=0,
        budget_status="OK",
        spreadsheet_id="1abcTEST",
    )
    text = str(payloads[0])
    assert "<https://docs.google.com/spreadsheets/d/1abcTEST|Sheets>" in text
