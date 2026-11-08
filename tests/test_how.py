"""Unit tests for the How profile (mocked HTML/feeds, no live HTTP or LLM calls)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from fetchers.chatprd_fetcher import parse_chatprd_page
from fetchers.sspai_fetcher import parse_sspai_feed, passes_how_filter
from how_pipeline import (
    build_how_slack_payload,
    known_urls,
    mark_seen,
    parse_bouncer_verdict,
    parse_sonnet_signals,
    prune_seen,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=3)


def _ld_page(*nodes: Dict[str, Any]) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes
    )
    return f"<html><head>{scripts}</head><body></body></html>"


PODCAST_SERIES_PAGE = _ld_page(
    {"@type": "ItemList", "itemListElement": []},
    {
        "@type": "PodcastSeries",
        "hasPart": [
            {
                "@type": "PodcastEpisode",
                "name": "Inside Figma's PM OS",
                "description": "A PM shows her agent setup.",
                "url": "https://www.chatprd.ai/how-i-ai/figma-pm-os",
                "datePublished": "2026-11-06T21:24:57.931Z",
            },
            {
                "@type": "PodcastEpisode",
                "name": "Old episode",
                "url": "/how-i-ai/old",
                "datePublished": "2026-10-01T00:00:00Z",
            },
            {
                "@type": "PodcastEpisode",
                "name": "Relative URL episode",
                "url": "/how-i-ai/relative",
                "datePublished": "2026-11-07T00:00:00Z",
            },
        ],
    },
)

LEGACY_BLOG_PAGE = _ld_page(
    {
        "@type": "Blog",
        "blogPost": [
            {
                "headline": "Legacy post",
                "url": "https://www.chatprd.ai/how-i-ai/legacy",
                "datePublished": "2026-11-06T00:00:00Z",
                "description": "old shape",
            }
        ],
    }
)


# ---------------------------------------------------------------------------
# ChatPRD
# ---------------------------------------------------------------------------

def test_chatprd_parses_podcast_series_and_applies_cutoff() -> None:
    items = parse_chatprd_page(PODCAST_SERIES_PAGE, CUTOFF)
    assert [i["title"] for i in items] == ["Inside Figma's PM OS", "Relative URL episode"]
    assert items[0]["source"] == "ChatPRD"
    assert items[0]["summary"] == "A PM shows her agent setup."
    assert items[1]["link"] == "https://www.chatprd.ai/how-i-ai/relative"


def test_chatprd_legacy_blogpost_fallback() -> None:
    items = parse_chatprd_page(LEGACY_BLOG_PAGE, CUTOFF)
    assert len(items) == 1
    assert items[0]["title"] == "Legacy post"


def test_chatprd_raises_when_structure_missing() -> None:
    with pytest.raises(RuntimeError):
        parse_chatprd_page(_ld_page({"@type": "BreadcrumbList"}), CUTOFF)
    with pytest.raises(RuntimeError):
        parse_chatprd_page("<html><body>no json-ld</body></html>", CUTOFF)


# ---------------------------------------------------------------------------
# SSPAI
# ---------------------------------------------------------------------------

def test_sspai_ai_keyword_is_whole_word() -> None:
    assert passes_how_filter("用 AI 整理笔记", "", "someone")
    assert not passes_how_filter("PAID apps worth buying", "MAIL client", "someone")


def test_sspai_cjk_keyword_substring_match() -> None:
    assert passes_how_filter("我的自动化工作流", "", "someone")


def test_sspai_negative_keyword_wins() -> None:
    assert not passes_how_filter("AI 摄影评测", "", "someone")


def test_sspai_whitelist_overrides_negative() -> None:
    assert passes_how_filter("摄影评测", "", "张立行")


def _entry(title: str, published: datetime, author: str = "someone") -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        summary="",
        author=author,
        link=f"https://sspai.com/post/{abs(hash(title))}",
        published_parsed=published.timetuple(),
    )


def test_sspai_parse_feed_filters_and_windows() -> None:
    feed = SimpleNamespace(entries=[
        _entry("Claude 工作流", NOW - timedelta(days=1)),
        _entry("Claude 工作流 old", NOW - timedelta(days=10)),
        _entry("MacBook 开箱", NOW - timedelta(days=1)),
    ])
    items = parse_sspai_feed(feed, CUTOFF)
    assert [i["title"] for i in items] == ["Claude 工作流"]
    assert items[0]["source"] == "SSPAI"


# ---------------------------------------------------------------------------
# Bouncer verdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["NULL", "NULL.", "  null\n", "NULL — no steps here"])
def test_bouncer_null_variants(text: str) -> None:
    assert parse_bouncer_verdict(text) is None


def test_bouncer_pass_returns_body() -> None:
    text = "PASS\nTool Stack: Cursor, Claude\nSteps:\n1. Do X"
    assert parse_bouncer_verdict(text) == "Tool Stack: Cursor, Claude\nSteps:\n1. Do X"


def test_bouncer_pass_without_body_is_none() -> None:
    assert parse_bouncer_verdict("PASS") is None


def test_bouncer_missing_verdict_treated_as_pass() -> None:
    assert parse_bouncer_verdict("Tool Stack: Cursor") == "Tool Stack: Cursor"


# ---------------------------------------------------------------------------
# Sonnet output
# ---------------------------------------------------------------------------

ID_TO_ITEM: Dict[int, Dict[str, Any]] = {
    1: {"title": "A", "source": "ChatPRD", "link": "https://a"},
    2: {"title": "B", "source": "SSPAI", "link": "https://b"},
}


def test_sonnet_signals_resolve_metadata_and_drop_bad_ids() -> None:
    raw = json.dumps([
        {"source_id": 2, "tech_stack": ["Cursor"], "workflow_steps": ["Step 1: x"], "roi_impact": "y"},
        {"source_id": 2, "tech_stack": ["dup"]},
        {"source_id": 99},
        {"tech_stack": ["no id"]},
    ])
    signals = parse_sonnet_signals(raw, ID_TO_ITEM)
    assert len(signals) == 1
    assert signals[0]["title"] == "B"
    assert signals[0]["url"] == "https://b"
    assert signals[0]["source"] == "SSPAI"
    assert signals[0]["tech_stack"] == ["Cursor"]


def test_sonnet_signals_tolerate_code_fences() -> None:
    raw = '```json\n[{"source_id": 1, "tech_stack": [], "workflow_steps": [], "roi_impact": ""}]\n```'
    assert parse_sonnet_signals(raw, ID_TO_ITEM)[0]["title"] == "A"


def test_sonnet_signals_raise_on_non_json() -> None:
    with pytest.raises(ValueError):
        parse_sonnet_signals("Sorry, I cannot help.", ID_TO_ITEM)
    with pytest.raises(ValueError):
        parse_sonnet_signals('{"source_id": 1}', ID_TO_ITEM)


# ---------------------------------------------------------------------------
# Seen ledger
# ---------------------------------------------------------------------------

def test_prune_seen_drops_old_and_malformed() -> None:
    seen: List[Dict[str, Any]] = [
        {"url": "https://new", "processed_at": (NOW - timedelta(days=5)).isoformat()},
        {"url": "https://old", "processed_at": (NOW - timedelta(days=90)).isoformat()},
        {"url": "https://bad", "processed_at": "not a date"},
        {"processed_at": NOW.isoformat()},
    ]
    assert [e["url"] for e in prune_seen(seen, NOW, retention_days=60)] == ["https://new"]


def test_mark_seen_dedupes() -> None:
    seen = [{"url": "https://a", "processed_at": NOW.isoformat()}]
    out = mark_seen(seen, ["https://a", "https://b", "https://b", ""], NOW)
    assert [e["url"] for e in out] == ["https://a", "https://b"]


def test_known_urls_combines_backlog_and_ledger() -> None:
    backlog = [{"link": "https://x"}, {"link": ""}]
    seen = [{"url": "https://y", "processed_at": NOW.isoformat()}]
    assert known_urls(backlog, seen) == {"https://x", "https://y"}


# ---------------------------------------------------------------------------
# Slack payload
# ---------------------------------------------------------------------------

def _all_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_payload_with_signals_has_funnel_and_footer() -> None:
    signals = [{
        "title": "A", "source": "ChatPRD", "url": "https://a",
        "tech_stack": ["Cursor"], "workflow_steps": ["Step 1: x"], "roi_impact": "saved time",
    }]
    funnel = {"ChatPRD": {"caught": 4, "passed": 1, "workflows": 1, "errored": 1}}
    payload = build_how_slack_payload(signals, funnel, total_tokens=100, budget_status="OK", max_tokens=5000)
    text = _all_text(payload)
    assert "4 caught" in text and "1 passed bouncer" in text and "(1 errored)" in text
    assert "Tokens: 100/5000" in text
    assert "claude-haiku-4-5-20251001 + claude-sonnet-4-6" in text
    assert "Read more" in text


def test_payload_truncates_long_sections() -> None:
    signals = [{
        "title": "A", "source": "ChatPRD", "url": "https://a",
        "tech_stack": [], "workflow_steps": ["x" * 5000], "roi_impact": "",
    }]
    payload = build_how_slack_payload(signals, {}, total_tokens=1, budget_status="OK")
    sections = [b for b in payload["blocks"] if b["type"] == "section"]
    assert all(len(s["text"]["text"]) <= 3000 for s in sections)
    assert "Read more" in sections[0]["text"]["text"]


def test_payload_empty_backlog_message() -> None:
    payload = build_how_slack_payload([], {}, total_tokens=0, budget_status="OK")
    assert "No new items were caught" in _all_text(payload)


def test_payload_no_workflows_message() -> None:
    funnel = {"SSPAI": {"caught": 3, "passed": 0}}
    payload = build_how_slack_payload([], funnel, total_tokens=10, budget_status="OK")
    text = _all_text(payload)
    assert "No replicable AI tool workflows" in text
    assert "3 caught" in text
