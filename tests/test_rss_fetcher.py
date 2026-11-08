"""Unit tests for What-profile RSS helpers (mocked feeds only, no live HTTP)."""
import json
from datetime import datetime, timezone
from typing import Any

import feedparser
import pytest

from fetchers.rss_fetcher import (
    RssFetcher,
    _dedupe_items,
    _normalize_title,
    _normalize_url,
    _parse_anthropic_ssr,
    apply_semantic_groups,
    funnel_line,
    funnel_lines,
    parse_semantic_groups,
    parse_what_feed,
)

_scorer = RssFetcher()
CUTOFF = datetime(2026, 11, 1, tzinfo=timezone.utc)
IN_WINDOW = "Thu, 06 Nov 2026 12:00:00 +0000"
TOO_OLD = "Thu, 01 Oct 2026 12:00:00 +0000"


def _rss(*items: str) -> Any:
    body = "".join(items)
    return feedparser.parse(
        f"<?xml version='1.0'?><rss version='2.0'><channel>{body}</channel></rss>"
    )


def _item(title: str, pub: str = "", link: str = "https://x/a") -> str:
    date = f"<pubDate>{pub}</pubDate>" if pub else ""
    return f"<item><title>{title}</title><link>{link}</link>{date}</item>"


def test_normalize_url_strips_utm() -> None:
    a = "https://techcrunch.com/2026/03/22/foo/?utm_source=feed"
    b = "https://techcrunch.com/2026/03/22/foo/"
    assert _normalize_url(a) == _normalize_url(b)


def test_normalize_url_strips_fbclid() -> None:
    base = "https://example.com/path"
    tracked = "https://example.com/path?fbclid=IwAR123"
    assert _normalize_url(tracked) == _normalize_url(base)


def test_normalize_url_lowercase_host_and_trailing_slash() -> None:
    assert _normalize_url("HTTPS://Example.COM/foo/") == "https://example.com/foo"


def test_normalize_title_strips_suffix() -> None:
    assert _normalize_title("Cursor raises Series Z | TechCrunch") == _normalize_title(
        "Cursor raises Series Z"
    )


def test_dedupe_keeps_higher_score_same_url() -> None:
    url = "https://techcrunch.com/a"
    low = {
        "title": "t",
        "summary": "",
        "link": url,
        "source": "techmeme",
        "published": "2026-03-20T00:00:00+00:00",
        "score": 2,
    }
    high = {
        "title": "t",
        "summary": "",
        "link": url + "?utm_source=x",
        "source": "techcrunch",
        "published": "2026-03-20T00:00:00+00:00",
        "score": 10,
    }
    out = _dedupe_items([low, high])
    assert len(out) == 1
    assert out[0]["score"] == 10
    assert out[0]["source"] == "techcrunch"


def test_dedupe_title_fallback_different_urls() -> None:
    a = {
        "title": "Nvidia announces new chip",
        "summary": "",
        "link": "https://techcrunch.com/nvidia",
        "source": "techcrunch",
        "published": "2026-03-22T12:00:00+00:00",
        "score": 5,
    }
    b = {
        "title": "Nvidia announces new chip | TechCrunch",
        "summary": "",
        "link": "https://www.techmeme.com/250322/p12",
        "source": "techmeme",
        "published": "2026-03-22T10:00:00+00:00",
        "score": 5,
    }
    out = _dedupe_items([a, b])
    assert len(out) == 1
    # Tie on score; newer published wins (techcrunch 12:00 > techmeme 10:00)
    assert out[0]["source"] == "techcrunch"


def test_dedupe_empty_input() -> None:
    assert _dedupe_items([]) == []


def test_score_true_hit_openai_gpt() -> None:
    assert _scorer._score_item("OpenAI releases GPT-5", "") > 0


def test_score_false_hit_security_update() -> None:
    assert _scorer._score_item("Apple storage security update", "") == 0


def test_score_false_hit_chipotle() -> None:
    assert _scorer._score_item("Chipotle earnings", "") == 0


def test_score_false_hit_meta_analysis() -> None:
    assert _scorer._score_item("Meta-analysis", "") == 0


def test_parse_what_feed_keeps_in_window_skips_old() -> None:
    feed = _rss(
        _item("OpenAI releases GPT-5", IN_WINDOW, "https://x/new"),
        _item("Old OpenAI note", TOO_OLD, "https://x/old"),
    )
    items, fetched = parse_what_feed(feed, CUTOFF, "openai_news")
    assert fetched == 2
    assert [i["link"] for i in items] == ["https://x/new"]


def test_parse_what_feed_skips_undated() -> None:
    feed = _rss(
        _item("OpenAI releases GPT-5", IN_WINDOW, "https://x/dated"),
        _item("Undated OpenAI note", "", "https://x/undated"),
    )
    items, fetched = parse_what_feed(feed, CUTOFF, "openai_news")
    assert fetched == 2
    assert [i["link"] for i in items] == ["https://x/dated"]


def test_parse_what_feed_empty_raises() -> None:
    with pytest.raises(ValueError, match="no entries"):
        parse_what_feed(_rss(), CUTOFF, "techmeme")


def test_parse_what_feed_all_undated_raises() -> None:
    feed = _rss(_item("No date A"), _item("No date B"))
    with pytest.raises(ValueError, match="all 2 entries failed"):
        parse_what_feed(feed, CUTOFF, "techcrunch")


def test_parse_anthropic_ssr_empty_html() -> None:
    assert _parse_anthropic_ssr("") == []
    assert _parse_anthropic_ssr("<html><body>no payload</body></html>") == []


def test_funnel_lines_folds_quiet_keeps_failed() -> None:
    counts = {
        "techmeme": {"fetched": 40, "in_window": 12, "relevant": 8, "sent_to_llm": 8},
        "techcrunch": {"fetched": 20, "in_window": 0},
        "openai_news": {"fetch_failed": 1},
        "anthropic_news": {"fetched": 15, "in_window": 0},
        "anthropic_research": {"fetched": 8, "in_window": 0},
    }
    assert funnel_line("openai_news", counts["openai_news"]) == (
        "📡 *Openai_news*   fetch failed"
    )
    lines = funnel_lines(counts)
    assert lines[0].startswith("📡 *Techmeme*")
    assert "*Openai_news*   fetch failed" in lines[1]
    assert lines[-1] == (
        "📡 quiet: *Techcrunch*, *Anthropic_news*, *Anthropic_research*"
        "  (43 fetched, 0 in window)"
    )


def _story(title: str, score: int, source: str = "techmeme") -> dict:
    return {
        "title": title,
        "summary": "",
        "link": f"https://x/{title}",
        "source": source,
        "published": "2026-11-06T12:00:00+00:00",
        "score": score,
    }


def test_parse_semantic_groups_accepts_fences_and_full_cover() -> None:
    assert parse_semantic_groups("```json\n[[0, 2], [1]]\n```", 3) == [[0, 2], [1]]


def test_parse_semantic_groups_rejects_partial_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="exactly once"):
        parse_semantic_groups("[[0, 1]]", 3)
    with pytest.raises(ValueError, match="out of range"):
        parse_semantic_groups("[[0, 5], [1]]", 2)
    with pytest.raises(ValueError, match="exactly once"):
        parse_semantic_groups("[[0, 1], [1]]", 2)
    with pytest.raises(ValueError, match="non-empty"):
        parse_semantic_groups("[[], [0]]", 1)
    with pytest.raises(json.JSONDecodeError):
        parse_semantic_groups("not json", 2)


def test_apply_semantic_groups_keeps_higher_score() -> None:
    items = [
        _story("OpenAI GPT-5", 5, "techmeme"),
        _story("Unrelated TSMC note", 3, "techcrunch"),
        _story("OpenAI GPT-5 launch", 10, "openai_news"),
    ]
    out = apply_semantic_groups(items, [[0, 2], [1]])
    assert len(out) == 2
    assert out[0]["score"] == 10 and out[0]["source"] == "openai_news"
    assert out[1]["title"] == "Unrelated TSMC note"
