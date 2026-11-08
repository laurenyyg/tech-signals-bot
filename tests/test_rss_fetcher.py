"""Unit tests for What-profile RSS helpers (mocked feeds only, no live HTTP)."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import feedparser
import pytest

from fetchers.rss_fetcher import (
    RssFetcher,
    _anthropic_published_dt,
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


def _ssr_script(payload: str) -> str:
    return f'<script>self.__next_f.push([1,"{payload}"])</script>'


def _ssr_listing(post: Dict[str, str], extra_before_slug: str = "") -> str:
    """Escaped listing-card JSON. extra_before_slug is the current CMS gap
    (sensitive / seoDescription); the old fixture leaves it empty and keeps
    a subjects array between slug and summary."""
    gap = extra_before_slug
    after_slug = post.get("after_slug", ',\\"subjects\\":[]')
    return (
        f'\\"publishedOn\\":\\"{post["published"]}\\"'
        f"{gap}"
        f',\\"slug\\":{{\\"_type\\":\\"slug\\",\\"current\\":\\"{post["slug"]}\\"}}'
        f"{after_slug}"
        f',\\"summary\\":\\"{post.get("summary", "")}\\"'
        f',\\"title\\":\\"{post["title"]}\\"'
    )


def _ssr_html(*posts: Dict[str, str], extra_before_slug: str = "") -> str:
    """Minimal Next.js RSC payload wrapping listing cards in one script tag."""
    payload = " ".join(_ssr_listing(p, extra_before_slug) for p in posts)
    return f"<html><body>{_ssr_script(payload)}</body></html>"


def _ssr_related(title: str, slug: str, published: str) -> str:
    """Slimmer related-article card: slug has no _type, no summary."""
    return (
        f'{{\\"_id\\":\\"x\\",\\"_type\\":\\"article\\",\\"title\\":\\"{title}\\"'
        f',\\"slug\\":{{\\"current\\":\\"{slug}\\"}}'
        f',\\"articleType\\":\\"news\\",\\"publishedOn\\":\\"{published}\\"'
        f',\\"category\\":\\"Science\\"}}'
    )


def test_parse_anthropic_ssr_empty_html() -> None:
    assert _parse_anthropic_ssr("") == []
    assert _parse_anthropic_ssr("<html><body>no payload</body></html>") == []


def test_parse_anthropic_ssr_fixture_keeps_old_skips_bad_dedupes_slug() -> None:
    html = _ssr_html(
        {
            "published": "2026-11-06T12:00:00.000Z",
            "slug": "claude-opus",
            "summary": "Opus ships.",
            "title": "Claude Opus",
        },
        {
            "published": "2025-01-01T00:00:00.000Z",
            "slug": "old-post",
            "summary": "Old.",
            "title": "Old Post",
        },
        {
            "published": "not-a-date",
            "slug": "bad-date",
            "summary": "x",
            "title": "Bad Date",
        },
        {
            "published": "2026-11-07T00:00:00Z",
            "slug": "claude-opus",
            "summary": "dup",
            "title": "Dup",
        },
    )
    posts = _parse_anthropic_ssr(html)
    assert [(p["slug"], p["title"]) for p in posts] == [
        ("claude-opus", "Claude Opus"),
        ("old-post", "Old Post"),
    ]
    assert posts[0]["summary"] == "Opus ships."


def test_parse_anthropic_ssr_current_shape_extra_fields_date_only() -> None:
    extra = (
        ',\\"sensitive\\":false'
        ',\\"seoDescription\\":\\"We are expanding the program.\\"'
    )
    html = _ssr_html(
        {
            "published": "2026-10-28",
            "slug": "ai-for-science-expands",
            "summary": "We're highlighting 20 institutions.",
            "title": "AI for Science expands",
            "after_slug": "",
        },
        extra_before_slug=extra,
    )
    posts = _parse_anthropic_ssr(html)
    assert [(p["slug"], p["published"], p["title"]) for p in posts] == [
        ("ai-for-science-expands", "2026-10-28", "AI for Science expands"),
    ]
    assert posts[0]["summary"] == "We're highlighting 20 institutions."


def test_parse_anthropic_ssr_scans_later_script_ignores_related_cards() -> None:
    related = _ssr_related("Related teaser", "related-teaser", "2026-11-06")
    listing = _ssr_listing(
        {
            "published": "2026-11-06",
            "slug": "teaching-ai-to-build-scientific-models",
            "summary": "Models from papers.",
            "title": "Teaching AI to build scientific models",
            "after_slug": "",
        },
        extra_before_slug=',\\"sensitive\\":false,\\"seoDescription\\":null',
    )
    html = (
        "<html><body>"
        + _ssr_script(related)
        + _ssr_script(listing)
        + "</body></html>"
    )
    posts = _parse_anthropic_ssr(html)
    assert [p["slug"] for p in posts] == ["teaching-ai-to-build-scientific-models"]


def test_parse_anthropic_ssr_strips_title_does_not_pair_related_date() -> None:
    related = _ssr_related("Related teaser", "related-teaser", "2026-01-01")
    listing = _ssr_listing(
        {
            "published": "2026-11-06",
            "slug": "vault-particles",
            "summary": "Biology puzzle.",
            "title": " Investigating vault particles",
            "after_slug": "",
        }
    )
    html = f"<html><body>{_ssr_script(related + listing)}</body></html>"
    posts = _parse_anthropic_ssr(html)
    assert [(p["slug"], p["published"], p["title"]) for p in posts] == [
        ("vault-particles", "2026-11-06", "Investigating vault particles"),
    ]


def test_parse_anthropic_ssr_null_summary_kept() -> None:
    payload = (
        '\\"publishedOn\\":\\"2026-09-18\\"'
        ',\\"sensitive\\":null,\\"seoDescription\\":\\"Partnering on evals.\\"'
        ',\\"slug\\":{\\"_type\\":\\"slug\\",\\"current\\":\\"accenture-embedded-evaluation\\"}'
        ',\\"summary\\":null'
        ',\\"title\\":\\" Partnering with Accenture on embedded evaluation\\"'
    )
    html = f"<html><body>{_ssr_script(payload)}</body></html>"
    posts = _parse_anthropic_ssr(html)
    assert [(p["slug"], p["summary"], p["title"]) for p in posts] == [
        (
            "accenture-embedded-evaluation",
            "",
            "Partnering with Accenture on embedded evaluation",
        ),
    ]


def test_anthropic_published_dt_date_only_is_end_of_utc_day() -> None:
    cutoff = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
    date_only = _anthropic_published_dt("2026-10-28")
    assert date_only is not None
    assert date_only >= cutoff
    stamp = _anthropic_published_dt("2026-10-28T00:00:00.000Z")
    assert stamp is not None
    assert stamp < cutoff


def test_funnel_lines_folds_quiet_keeps_failed() -> None:
    counts = {
        "techmeme": {"fetched": 40, "in_window": 12, "relevant": 8, "sent_to_llm": 8},
        "techcrunch": {"fetched": 20, "in_window": 0},
        "openai_news": {"fetch_failed": 1},
        "anthropic_news": {"fetched": 15, "in_window": 0},
        "anthropic_research": {"fetched": 8, "in_window": 0},
    }
    assert funnel_line("openai_news", counts["openai_news"]) == (
        "📡 *<https://openai.com/news|Openai_news>*   fetch failed"
    )
    lines = funnel_lines(counts)
    assert "in last 8d" in lines[0]
    assert lines[0].startswith("📡 *<https://www.techmeme.com/|Techmeme>*")
    assert "*<https://openai.com/news|Openai_news>*   fetch failed" in lines[1]
    assert lines[-1] == (
        "📡 quiet: *<https://techcrunch.com/|Techcrunch>*, "
        "*<https://www.anthropic.com/news|Anthropic_news>*, "
        "*<https://www.anthropic.com/research|Anthropic_research>*"
        "  (43 fetched, 0 in window)"
    )
    stretched = funnel_line(
        "techmeme",
        {"fetched": 40, "in_window": 12, "relevant": 8, "sent_to_llm": 4, "already_posted": 4},
        window_days=14,
    )
    assert "in last 14d" in stretched
    assert "(4 already posted)" in stretched


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


def test_fetch_cutoff_floors_stretches_and_caps() -> None:
    from main import fetch_cutoff

    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    floor = now - timedelta(days=8)
    assert fetch_cutoff([], now) == floor

    recent = [{"url": "https://x/a", "processed_at": (now - timedelta(days=3)).isoformat()}]
    assert fetch_cutoff(recent, now) == floor

    gap = [{"url": "https://x/a", "processed_at": (now - timedelta(days=14)).isoformat()}]
    assert fetch_cutoff(gap, now) == now - timedelta(days=14)

    stale = [{"url": "https://x/a", "processed_at": (now - timedelta(days=40)).isoformat()}]
    assert fetch_cutoff(stale, now) == now - timedelta(days=14)
    assert fetch_cutoff([{"url": "https://x/a", "processed_at": "not-a-date"}], now) == floor


def test_drop_seen_uses_canonical_url() -> None:
    from main import drop_seen, ledger_key

    posted = _story("OpenAI GPT-5", 5)
    posted["link"] = "https://techcrunch.com/gpt5/?utm_source=feed"
    fresh_item = _story("TSMC 2nm", 3, "techcrunch")
    seen = {ledger_key(posted)}
    fresh, dropped = drop_seen(
        [{**posted, "link": "https://techcrunch.com/gpt5/"}, fresh_item],
        seen,
    )
    assert [i["title"] for i in dropped] == ["OpenAI GPT-5"]
    assert [i["title"] for i in fresh] == ["TSMC 2nm"]


def test_resolve_source_urls_drops_unknown_and_empty() -> None:
    from main import resolve_source_urls

    items = [
        {"link": "https://x/one", "source": "techmeme"},
        {"link": "", "source": "techcrunch"},
        {"link": "https://x/three", "source": "openai_news"},
    ]
    signals = [
        {"macro_event": "A", "source_ids": [1, 2, 99, 3]},
        {"macro_event": "B", "source_ids": [1]},
        {"macro_event": "C"},
    ]
    resolve_source_urls(signals, items)
    assert signals[0]["source_urls"] == ["https://x/one", "https://x/three"]
    assert "source_ids" not in signals[0]
    assert signals[1]["source_urls"] == ["https://x/one"]
    assert signals[2]["source_urls"] == []
