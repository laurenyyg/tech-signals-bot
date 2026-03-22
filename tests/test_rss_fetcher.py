"""Unit tests for What-profile RSS dedupe helpers (no live HTTP)."""

from fetchers.rss_fetcher import _dedupe_items, _normalize_title, _normalize_url


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
