"""Unit tests for What Sheets row shaping (no live Google API)."""
from sheets_writer import rows_for_run, spreadsheet_link


def test_rows_for_run_uses_keyword_relevant_over_fetched() -> None:
    counts = {
        "techmeme": {"fetched": 40, "in_window": 12, "relevant": 8, "sent_to_llm": 5},
        "techcrunch": {"fetched": 20, "in_window": 0, "relevant": 0},
        "openai_news": {"fetched": 0, "fetch_failed": 1, "relevant": 0},
    }
    rows = rows_for_run(counts, "#002", "2026-11-08")
    assert rows == [
        ["#002", "2026-11-08", "Techmeme", 8, 40, "20%"],
        ["#002", "2026-11-08", "Techcrunch", 0, 20, "0%"],
        ["#002", "2026-11-08", "Openai_news", 0, 0, "0%"],
    ]


def test_rows_for_run_ignores_llm_citation_counts() -> None:
    counts = {"techmeme": {"fetched": 10, "relevant": 4, "sent_to_llm": 4}}
    row = rows_for_run(counts, "#001", "2026-11-08")[0]
    assert row[3] == 4
    assert row[4] == 10
    assert row[5] == "40%"


def test_spreadsheet_link_omits_when_missing() -> None:
    assert spreadsheet_link(None) == ""
    assert spreadsheet_link("") == ""
    assert spreadsheet_link("1abc") == (
        "<https://docs.google.com/spreadsheets/d/1abc|Sheets>"
    )
