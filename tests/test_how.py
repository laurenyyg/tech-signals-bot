"""Unit tests for the How profile (mocked HTML/feeds, no live HTTP or LLM calls)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from fetchers.chatprd_fetcher import (
    extract_workflow_links,
    parse_chatprd_page,
    parse_howto_page,
)
from fetchers.sspai_fetcher import parse_sspai_feed, passes_how_filter
import how_pipeline
from how_pipeline import (
    build_how_slack_payloads,
    dedupe_tools,
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


EPISODE_PAGE = """
<html><body>
<a href="/how-i-ai/workflows">All workflows</a>
<a href="/how-i-ai/workflows/automate-bug-bashing">Automate Bug Bashing</a>
<a href="https://www.chatprd.ai/how-i-ai/workflows/growth-loop/">Growth Loop</a>
<a href="/how-i-ai/workflows/automate-bug-bashing">duplicate link</a>
<a href="/how-i-ai/some-other-episode">Another episode</a>
</body></html>
"""


def test_extract_workflow_links_dedupes_and_skips_index() -> None:
    assert extract_workflow_links(EPISODE_PAGE) == [
        "https://www.chatprd.ai/how-i-ai/workflows/automate-bug-bashing",
        "https://www.chatprd.ai/how-i-ai/workflows/growth-loop",
    ]


HOWTO_PAGE = _ld_page(
    {"@type": "BreadcrumbList"},
    {
        "@type": "HowTo",
        "name": "Automate Software Bug Bashing with a Browser Agent",
        "description": "Use a browser agent to follow a test plan.",
        "datePublished": "2026-10-29T21:24:56.155Z",
        "tool": [{"@type": "HowToTool", "name": "Codex"}, {"@type": "HowToTool", "name": "FigJam"}],
        "supply": [{"@type": "HowToSupply", "name": "A detailed test plan."}],
        "yield": "A FigJam board of bug comments.",
        "step": [
            {"@type": "HowToStep", "name": "Create a Test Plan", "text": "Document exact steps."},
            {"@type": "HowToStep", "text": "Run the agent."},
        ],
    },
)


def test_parse_howto_page_renders_body() -> None:
    parsed = parse_howto_page(HOWTO_PAGE)
    assert parsed is not None
    assert parsed["title"] == "Automate Software Bug Bashing with a Browser Agent"
    assert parsed["published"] == "2026-10-29T21:24:56.155Z"
    body = parsed["body"]
    assert "Tools: Codex, FigJam" in body
    assert "- A detailed test plan." in body
    assert "1. Create a Test Plan: Document exact steps." in body
    assert "2. Run the agent." in body
    assert "Outcome: A FigJam board of bug comments." in body


def test_parse_howto_page_missing_returns_none() -> None:
    assert parse_howto_page(_ld_page({"@type": "BreadcrumbList"})) is None


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

def _all_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _signal(title: str = "A") -> Dict[str, Any]:
    return {
        "title": title, "source": "ChatPRD", "url": f"https://{title}",
        "tech_stack": ["Cursor"], "workflow_steps": ["Step 1: x"], "roi_impact": "saved time",
    }


def test_payload_with_signals_has_funnel_and_footer() -> None:
    funnel = {"ChatPRD": {"caught": 4, "passed": 3, "structured": 2, "workflows": 3, "errored": 1}}
    payloads = build_how_slack_payloads([_signal()], funnel, total_tokens=100, budget_status="OK", max_tokens=5000)
    assert len(payloads) == 1
    text = _all_text(payloads[0])
    assert "4 caught" in text and "3 passed bouncer" in text and "(1 errored)" in text
    assert "(2 pre-structured, not bounced)" in text
    assert "Tokens: 100/5000" in text
    assert "claude-haiku-4-5-20251001 + claude-sonnet-4-6" in text
    assert "Read more" in text


def test_payload_truncates_long_sections() -> None:
    signal = {**_signal(), "workflow_steps": ["x" * 5000]}
    payloads = build_how_slack_payloads([signal], {}, total_tokens=1, budget_status="OK")
    sections = [b for b in payloads[0]["blocks"] if b["type"] == "section"]
    assert all(len(s["text"]["text"]) <= 3000 for s in sections)
    assert "Read more" in sections[0]["text"]["text"]


def test_payload_splits_across_messages_under_block_limit() -> None:
    signals = [_signal(f"s{i}") for i in range(45)]
    funnel = {"ChatPRD": {"caught": 45, "passed": 45, "workflows": 45}}
    payloads = build_how_slack_payloads(signals, funnel, total_tokens=1, budget_status="OK")
    assert len(payloads) == 3
    assert all(len(p["blocks"]) <= 50 for p in payloads)
    assert "(1/3)" in payloads[0]["text"] and "(3/3)" in payloads[2]["text"]
    cards = sum(1 for p in payloads for b in p["blocks"] if b["type"] == "section")
    assert cards == 45
    assert "Model:" not in _all_text(payloads[0]) and "45 caught" not in _all_text(payloads[0])
    assert "Model:" in _all_text(payloads[2]) and "45 caught" in _all_text(payloads[2])


def test_payload_empty_backlog_message() -> None:
    payloads = build_how_slack_payloads([], {}, total_tokens=0, budget_status="OK")
    assert "No new items were caught" in _all_text(payloads)


def test_payload_no_workflows_message() -> None:
    funnel = {"SSPAI": {"caught": 3, "passed": 0}}
    text = _all_text(build_how_slack_payloads([], funnel, total_tokens=10, budget_status="OK"))
    assert "No replicable AI tool workflows" in text
    assert "3 caught" in text


# ---------------------------------------------------------------------------
# Tool cleanup
# ---------------------------------------------------------------------------

def test_dedupe_tools_folds_mcp_and_duplicates() -> None:
    assert dedupe_tools(["ElevenLabs", "ElevenLabs MCP", "claude", "Claude", " ", "Figma MCP"]) == [
        "ElevenLabs", "claude", "Figma MCP",
    ]


# ---------------------------------------------------------------------------
# Pipeline (LLM + Slack mocked)
# ---------------------------------------------------------------------------

def test_pipeline_skips_bouncer_for_howto_items(tmp_path: Any, monkeypatch: Any) -> None:
    backlog_file = tmp_path / "how_backlog.json"
    seen_file = tmp_path / "how_seen.json"
    backlog_file.write_text(json.dumps([
        {"title": "HowTo", "link": "https://howto", "source": "ChatPRD",
         "kind": "howto", "body": "Tools: Codex\nSteps:\n1. Do it"},
        {"title": "Post", "link": "https://post", "source": "SSPAI", "body": "some article"},
    ]))
    monkeypatch.setattr(how_pipeline, "BACKLOG_FILE", str(backlog_file))
    monkeypatch.setattr(how_pipeline, "SEEN_FILE", str(seen_file))

    calls: List[str] = []

    def fake_claude(model_id: str, system: str, user_message: str, max_tokens: int,
                    use_thinking: bool = False) -> Any:
        calls.append(model_id)
        if model_id == how_pipeline.HAIKU_MODEL_ID:
            assert "some article" in user_message
            return "NULL", 10, 1, "end_turn"
        assert "[1] Title: HowTo" in user_message and "Post" not in user_message
        return json.dumps([{"source_id": 1, "tech_stack": ["Codex"],
                            "workflow_steps": ["Step 1: x"], "roi_impact": "y"}]), 20, 5, "end_turn"

    posted: List[Dict[str, Any]] = []
    monkeypatch.setattr(how_pipeline, "call_claude_raw", fake_claude)
    monkeypatch.setattr(how_pipeline, "post_to_slack", lambda url, p: posted.append(p))

    meta: Dict[str, Any] = {}
    how_pipeline.run_how_pipeline("https://hook", meta)

    assert calls == [how_pipeline.HAIKU_MODEL_ID, how_pipeline.MODEL_ID]
    assert len(posted) == 1 and "HowTo" in _all_text(posted)
    assert meta["total_tokens"] == 36
    assert json.loads(backlog_file.read_text()) == []
    assert {e["url"] for e in json.loads(seen_file.read_text())} == {"https://howto", "https://post"}
