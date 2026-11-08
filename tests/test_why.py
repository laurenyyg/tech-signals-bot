"""Unit tests for the Why profile (mocked HTML/feeds/LLM, no live HTTP or API calls)."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import feedparser
import pytest

import why_pipeline
from fetchers.why_fetcher import (
    a16z_skip_reason,
    dedupe_podcasts,
    parse_a16z_archive,
    parse_ben_evans_page,
    parse_deeplearning_ai_page,
    parse_human_date,
    parse_human_date_range,
    parse_rss_feed,
)
from seen_ledger import canonical_url, load_json_list
from why_pipeline import (
    build_why_slack_payload,
    drop_seen,
    ledger_key,
    parse_why_response,
    summarize_items,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=8)


@pytest.fixture(autouse=True)
def _isolated_seen_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "data" / "why_seen.json")
    monkeypatch.setattr(why_pipeline, "SEEN_FILE", path)
    return path


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("3 September 2026", datetime(2026, 9, 3, tzinfo=timezone.utc)),
    ("3 Sept 2026", datetime(2026, 9, 3, tzinfo=timezone.utc)),
    ("Feb 19, 2026", datetime(2026, 2, 19, tzinfo=timezone.utc)),
    ("Sept 2026", datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ("Nov 2025", datetime(2025, 11, 1, tzinfo=timezone.utc)),
    ("2026", None),
    ("", None),
])
def test_parse_human_date(raw: str, expected: Any) -> None:
    assert parse_human_date(raw) == expected


def test_parse_human_date_range_spans_day_or_month() -> None:
    utc = timezone.utc
    assert parse_human_date_range("5 November 2026") == (
        datetime(2026, 11, 5, tzinfo=utc), datetime(2026, 11, 6, tzinfo=utc), False,
    )
    assert parse_human_date_range("Dec 2026") == (
        datetime(2026, 12, 1, tzinfo=utc), datetime(2027, 1, 1, tzinfo=utc), True,
    )
    assert parse_human_date_range("Feb 2028") == (
        datetime(2028, 2, 1, tzinfo=utc), datetime(2028, 3, 1, tzinfo=utc), True,
    )


# ---------------------------------------------------------------------------
# a16z
# ---------------------------------------------------------------------------

def _a16z_post(title: str, date: str, **extra: Any) -> Dict[str, Any]:
    return {
        "title": title,
        "post_date": date,
        "canonical_url": f"https://www.a16z.news/p/{title.lower().replace(' ', '-')}",
        "description": "Short description.",
        "publishedBylines": [{"name": "Jane Doe"}],
        **extra,
    }


def test_a16z_parses_current_dict_shape() -> None:
    payload = {
        "posts": [
            _a16z_post("AI Margins", "2026-11-06T15:00:00.000Z", truncated_body_text="Full-ish body."),
            _a16z_post("Investing in Acme", "2026-11-06T15:00:00.000Z"),
            _a16z_post("Old Post", "2026-10-01T15:00:00.000Z"),
        ],
        "nextCursor": "abc",
    }
    items = parse_a16z_archive(payload, CUTOFF)
    assert [i["title"] for i in items] == ["AI Margins"]
    assert items[0]["author"] == "Jane Doe"
    assert items[0]["summary"] == "Full-ish body."


def test_a16z_accepts_legacy_bare_list() -> None:
    items = parse_a16z_archive([_a16z_post("AI Margins", "2026-11-06T15:00:00Z")], CUTOFF)
    assert items[0]["summary"] == "Short description."


@pytest.mark.parametrize("title, tags, skipped", [
    ("Investing in Phaze", ["Fundraise"], True),
    ("The Machine Age Fund", ["Daily Newsletter", "Fundraise"], True),
    ("Welcome Andy Baker", [], True),
    ("Meet the a16z Ops Engineering Fellows ", [], True),
    ("Announcing the Third a16z New Media Fellowship", ["Culture"], True),
    ("Introducing Covenant", ["Daily Newsletter", "America"], True),
    ("Expanding the a16z Growth Fund and Platform", ["Daily Newsletter", "Growth"], True),
    ("Product Management is Still All About Telling Stories", [], False),
    ("How AI Applications Earn Their Premium", ["Daily Newsletter", "Technology"], False),
    ("Welcoming Chaos Is a Strategy", ["Opinion"], False),
])
def test_a16z_skip_reason(title: str, tags: List[str], skipped: bool) -> None:
    post = {"title": title, "postTags": [{"name": t} for t in tags]}
    assert (a16z_skip_reason(post) is not None) is skipped


def test_a16z_archive_applies_skip_filter() -> None:
    payload = {"posts": [
        _a16z_post("Welcome Andy Baker", "2026-11-06T15:00:00Z"),
        _a16z_post("The Machine Age Fund", "2026-11-06T15:00:00Z", postTags=[{"name": "Fundraise"}]),
        _a16z_post("Real Essay", "2026-11-06T15:00:00Z"),
    ]}
    assert [i["title"] for i in parse_a16z_archive(payload, CUTOFF)] == ["Real Essay"]


def test_a16z_unexpected_shape_raises() -> None:
    with pytest.raises(ValueError, match="unexpected archive response shape"):
        parse_a16z_archive({"items": []}, CUTOFF)


def test_a16z_all_posts_malformed_raises() -> None:
    with pytest.raises(ValueError, match="all 2 entries failed"):
        parse_a16z_archive(["posts", "nextCursor"], CUTOFF)


# ---------------------------------------------------------------------------
# Ben Evans
# ---------------------------------------------------------------------------

def _essay_card(title: str, date: str) -> str:
    return (
        '<div class="summary-item">'
        f'<a class="summary-title-link" href="/essays/{title[:4]}">{title}</a>'
        f'<span class="summary-metadata-item--date">{date}</span>'
        '<div class="summary-excerpt">Excerpt.</div>'
        "</div>"
    )


BEN_EVANS_PAGE = (
    "<html><body>"
    + _essay_card("New essay", "5 November 2026")
    + _essay_card("Old essay", "3 September 2026")
    + _essay_card("Undated essay", "")
    + '<div class="PageRight"><h2><a href="/newsletter">What mattered this week?</a></h2>'
    '<div class="PageRightEyebrow">Nov 2026</div></div>'
    '<div class="PageRight"><h2>AI eats the world</h2>'
    '<div class="PageRightEyebrow">May 2026</div>'
    '<div class="pagerightbody">Deck.</div><a href="/presentations">More</a></div>'
    "</body></html>"
)


def test_ben_evans_day_first_dates_filter_correctly() -> None:
    essays, presentations = parse_ben_evans_page(BEN_EVANS_PAGE, CUTOFF)
    assert [e["title"] for e in essays] == ["New essay"]
    assert essays[0]["link"] == "https://www.ben-evans.com/essays/New "
    assert presentations == []


def test_ben_evans_month_only_presentation_stays_in_window_all_month() -> None:
    page = (
        _essay_card("Old essay", "3 September 2026")
        + '<div class="PageRight"><h2>Tokens are the new oil</h2>'
        '<div class="PageRightEyebrow">Nov 2026</div><a href="/presentations">More</a></div>'
    )
    _, presentations = parse_ben_evans_page(page, CUTOFF)
    assert [p["title"] for p in presentations] == ["Tokens are the new oil"]
    assert presentations[0]["date_precision"] == "month"

    three_weeks_later = datetime(2026, 11, 28, tzinfo=timezone.utc) - timedelta(days=8)
    _, presentations = parse_ben_evans_page(page, three_weeks_later)
    assert len(presentations) == 1

    next_month = datetime(2026, 12, 9, tzinfo=timezone.utc) - timedelta(days=8)
    _, presentations = parse_ben_evans_page(page, next_month)
    assert presentations == []


def test_ben_evans_all_undated_raises() -> None:
    page = _essay_card("A", "Someday") + _essay_card("B", "")
    with pytest.raises(ValueError, match="unparsable date"):
        parse_ben_evans_page(page, CUTOFF)


def test_ben_evans_no_cards_raises() -> None:
    with pytest.raises(ValueError, match="no essay cards"):
        parse_ben_evans_page("<html><body></body></html>", CUTOFF)


# ---------------------------------------------------------------------------
# DeepLearning.AI
# ---------------------------------------------------------------------------

def test_deeplearning_ai_cutoff_day_buffer() -> None:
    cutoff_day = CUTOFF.date().isoformat()
    page = (
        f'<article><a href="/the-batch/a"><h2>On the cutoff day</h2></a><time datetime="{cutoff_day}"></time></article>'
        '<article><a href="/the-batch/b"><h2>Too old</h2></a><time datetime="2026-10-01"></time></article>'
        '<article><h2></h2></article>'
    )
    items = parse_deeplearning_ai_page(page, CUTOFF)
    assert [i["title"] for i in items] == ["On the cutoff day"]
    assert items[0]["link"] == "https://www.deeplearning.ai/the-batch/a"


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
  <item><title>Aggregators Again</title><link>https://stratechery.com/a</link>
    <guid isPermaLink="false">https://stratechery.com/?p=20955</guid>
    <pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate>
    <description>Teaser.</description>
    <content:encoded><![CDATA[<p>Full body.</p>]]></content:encoded></item>
  <item><title>Old</title><link>https://stratechery.com/old</link>
    <pubDate>Thu, 01 Oct 2026 10:00:00 GMT</pubDate></item>
  <item><title>No date</title><link>https://stratechery.com/nodate</link></item>
</channel></rss>"""


def test_rss_prefers_full_content_and_drops_undated() -> None:
    items = parse_rss_feed(feedparser.parse(RSS_XML), CUTOFF, "stratechery", "Ben Thompson", "essay")
    assert [i["title"] for i in items] == ["Aggregators Again"]
    assert items[0]["summary"] == "<p>Full body.</p>"
    assert items[0]["guid"] == "https://stratechery.com/?p=20955"


def test_rss_empty_feed_raises() -> None:
    with pytest.raises(ValueError, match="no entries"):
        parse_rss_feed(feedparser.parse("<rss><channel></channel></rss>"), CUTOFF, "x", "y", "essay")


def test_dedupe_podcasts_articles_win() -> None:
    articles = [{"title": "Aggregators Again"}]
    podcasts = [{"title": "aggregators again"}, {"title": "Interview"}]
    assert dedupe_podcasts(articles, podcasts) == [{"title": "Interview"}]


# ---------------------------------------------------------------------------
# Seen ledger
# ---------------------------------------------------------------------------

def test_canonical_url_strips_access_token() -> None:
    assert canonical_url(
        "https://Stratechery.com/2026/falling-leaves/?access_token=eyJ.abc#top"
    ) == "https://stratechery.com/2026/falling-leaves"
    assert canonical_url("") == ""


def test_canonical_url_keeps_identifying_params() -> None:
    assert canonical_url(
        "https://stratechery.com/?access_token=eyJ.abc&p=20955&utm_source=rss"
    ) == "https://stratechery.com?p=20955"
    assert canonical_url("https://stratechery.com/?p=20955&access_token=other") == canonical_url(
        "https://stratechery.com/?access_token=eyJ.abc&p=20955"
    )


def test_ledger_key_prefers_guid_then_link_then_title() -> None:
    assert ledger_key({
        "guid": "https://stratechery.com/?p=20955",
        "link": "https://stratechery.com/2026/ai-on-the-ballot/?access_token=x",
    }) == "https://stratechery.com?p=20955"
    assert ledger_key({"link": "https://x.com/a/"}) == "https://x.com/a"
    assert ledger_key({"link": "", "source": "ben-evans", "title": " AI Eats "}) == "ben-evans:ai eats"


def test_drop_seen() -> None:
    items = [
        {"link": "https://stratechery.com/a/?access_token=new"},
        {"link": "https://stratechery.com/b"},
    ]
    fresh, dropped = drop_seen(items, {"https://stratechery.com/a"})
    assert fresh == [items[1]] and dropped == 1


# ---------------------------------------------------------------------------
# LLM parsing and per-item summarisation
# ---------------------------------------------------------------------------

def test_parse_why_response_variants() -> None:
    assert parse_why_response('```json\n{"summary": ["a", " "], "is_deep_read": true}\n```') == (["a"], True)
    assert parse_why_response('{"summary": "single", "is_deep_read": "yes"}') == (["single"], False)
    with pytest.raises(ValueError, match="no summary"):
        parse_why_response('{"summary": [], "is_deep_read": true}')
    with pytest.raises(ValueError, match="non-JSON"):
        parse_why_response('{"summary": ["unterminated')
    with pytest.raises(ValueError, match="JSON object"):
        parse_why_response("[]")


def _item(title: str) -> Dict[str, Any]:
    return {
        "title": title, "author": "A", "summary": "<p>Body</p>", "link": f"https://x/{title}",
        "source": "a16z", "content_type": "essay", "published": NOW.isoformat(),
    }


def _fake_llm(responses: Dict[str, Tuple[str, str]]) -> Any:
    def fake(model_id: str, system: str, user_message: str, max_tokens: int, use_thinking: bool = False):
        title = user_message.splitlines()[0].removeprefix("Title: ")
        text, stop = responses[title]
        if text == "RAISE":
            raise RuntimeError("Anthropic API error: overloaded")
        return text, 100, 500, stop
    return fake


def test_summarize_items_isolates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(why_pipeline, "call_claude_raw", _fake_llm({
        "good": ('{"summary": ["*Core thesis:* x"], "is_deep_read": true}', "end_turn"),
        "truncated": ('{"summary": ["cut', "max_tokens"),
        "api-error": ("RAISE", ""),
    }))
    usage: Dict[str, Any] = {}
    signals, failures = summarize_items([_item("good"), _item("truncated"), _item("api-error")], usage)

    assert [s["title"] for s in signals] == ["good"]
    assert signals[0]["link"] == "https://x/good"
    assert signals[0]["is_deep_read"] is True
    assert [(f[0]["title"], "budget" in f[1] or "overloaded" in f[1]) for f in failures] == [
        ("truncated", True), ("api-error", True),
    ]
    assert usage == {"total_tokens": 1200, "budget_status": "Hit", "max_tokens": 16000}


def test_run_why_pipeline_raises_when_every_item_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFetcher:
        source_counts: Dict[str, int] = {"a16z": 1}
        source_errors: Dict[str, str] = {}

        def fetch(self) -> List[Dict[str, Any]]:
            return [_item("bad")]

    monkeypatch.setattr(why_pipeline, "WhyFetcher", FakeFetcher)
    monkeypatch.setattr(why_pipeline, "call_claude_raw", _fake_llm({"bad": ("RAISE", "")}))
    posted: List[Any] = []
    monkeypatch.setattr(why_pipeline, "post_to_slack", lambda url, payload: posted.append(payload))

    with pytest.raises(RuntimeError, match="All 1 Why summaries failed"):
        why_pipeline.run_why_pipeline("https://hooks.example", {})
    assert posted == []


def test_run_why_pipeline_posts_empty_week_with_source_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFetcher:
        source_counts: Dict[str, int] = {"a16z": 0}
        source_errors: Dict[str, str] = {"a16z": "unexpected archive response shape"}

        def fetch(self) -> List[Dict[str, Any]]:
            return []

    monkeypatch.setattr(why_pipeline, "WhyFetcher", FakeFetcher)
    posted: List[Any] = []
    monkeypatch.setattr(why_pipeline, "post_to_slack", lambda url, payload: posted.append(payload))

    why_pipeline.run_why_pipeline("https://hooks.example", {})
    text = str(posted[0])
    assert "No new essays or presentations" in text
    assert "a16z* fetch failed: unexpected archive response shape" in text


def _fetcher_returning(items: List[Dict[str, Any]]) -> Any:
    class FakeFetcher:
        source_counts: Dict[str, int] = {"a16z": len(items)}
        source_errors: Dict[str, str] = {}

        def fetch(self) -> List[Dict[str, Any]]:
            return [dict(i) for i in items]

    return FakeFetcher


def test_run_why_pipeline_ledger_skips_posted_and_retries_failed(
    monkeypatch: pytest.MonkeyPatch, _isolated_seen_file: str
) -> None:
    monkeypatch.setattr(why_pipeline, "WhyFetcher", _fetcher_returning([_item("good"), _item("broken")]))
    monkeypatch.setattr(why_pipeline, "call_claude_raw", _fake_llm({
        "good": ('{"summary": ["x"], "is_deep_read": false}', "end_turn"),
        "broken": ("not json", "end_turn"),
        "fresh": ('{"summary": ["y"], "is_deep_read": false}', "end_turn"),
    }))
    posted: List[Any] = []
    monkeypatch.setattr(why_pipeline, "post_to_slack", lambda url, payload: posted.append(payload))

    why_pipeline.run_why_pipeline("https://hooks.example", {})
    assert [e["url"] for e in load_json_list(_isolated_seen_file)] == ["https://x/good"]

    # Next week: "good" is skipped as already posted; "broken" is retried.
    monkeypatch.setattr(
        why_pipeline, "WhyFetcher",
        _fetcher_returning([_item("good"), _item("broken"), _item("fresh")]),
    )
    why_pipeline.run_why_pipeline("https://hooks.example", {})
    second = str(posted[1])
    assert "*good*" not in second and "*fresh*" in second
    assert "Not summarised" in second and "broken" in second
    assert "(1 already posted, skipped)" in second
    assert [e["url"] for e in load_json_list(_isolated_seen_file)] == [
        "https://x/good", "https://x/fresh",
    ]


def test_run_why_pipeline_slack_failure_leaves_ledger_untouched(
    monkeypatch: pytest.MonkeyPatch, _isolated_seen_file: str
) -> None:
    monkeypatch.setattr(why_pipeline, "WhyFetcher", _fetcher_returning([_item("good")]))
    monkeypatch.setattr(why_pipeline, "call_claude_raw", _fake_llm({
        "good": ('{"summary": ["x"], "is_deep_read": false}', "end_turn"),
    }))

    def failing_post(url: str, payload: Any) -> None:
        raise RuntimeError("Slack webhook returned 500")

    monkeypatch.setattr(why_pipeline, "post_to_slack", failing_post)
    with pytest.raises(RuntimeError, match="500"):
        why_pipeline.run_why_pipeline("https://hooks.example", {})
    assert load_json_list(_isolated_seen_file) == []


# ---------------------------------------------------------------------------
# Slack payload
# ---------------------------------------------------------------------------

def test_build_why_slack_payload_layout() -> None:
    signals = [
        {**_item("Deep"), "summary": ["*Core thesis:* x"], "is_deep_read": True},
        {**_item("Pod"), "content_type": "podcast", "summary": ["y"], "is_deep_read": True},
        {**_item("Deck"), "content_type": "presentation", "summary": ["z"], "is_deep_read": False},
    ]
    payload = build_why_slack_payload(
        signals,
        source_counts={"a16z": 3},
        source_errors={"ben-evans": "no essay cards found"},
        failures=[(_item("Broken"), "non-JSON output")],
        total_tokens=1234,
        budget_status="OK",
        max_tokens=24000,
    )
    sections = [b["text"]["text"] for b in payload["blocks"] if b["type"] == "section"]
    assert sections[0].startswith("❗️ *Deep* — _A_ · Nov 7")
    assert sections[0].endswith("• _<https://x/Deep|Read essay>_")
    assert sections[1].startswith("🎙 *Pod*") and "Read transcript" in sections[1]
    assert "`[Presentation]`" in sections[2] and "Find out more" in sections[2]

    contexts = [b["elements"][0]["text"] for b in payload["blocks"] if b["type"] == "context"]
    assert "⚠️ *ben-evans* fetch failed: no essay cards found" in contexts[0]
    assert "⚠️ Not summarised: <https://x/Broken|Broken> — non-JSON output" in contexts[0]
    assert contexts[1] == "📡 a16z: 3"
    assert "Tokens: 1234/24000 | Budget: OK" in contexts[2]


def test_build_why_slack_payload_month_only_date() -> None:
    signal = {
        **_item("Deck"), "content_type": "presentation", "date_precision": "month",
        "published": "2026-11-01T00:00:00+00:00", "summary": ["z"],
    }
    section = build_why_slack_payload([signal])["blocks"][2]["text"]["text"]
    assert "· Nov 2026" in section
