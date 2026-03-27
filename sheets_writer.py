"""
sheets_writer.py — Append per-source performance stats to a Google Spreadsheet
after each "What" profile run.
"""
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES: List[str] = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME: str = "Performance"
HEADER_ROW: List[str] = [
    "Run ID", "Timestamp", "Source Name", "Relevant (LLM)", "Total (7d)", "Ratio"
]


def _get_next_run_id(worksheet: gspread.Worksheet) -> str:
    """Read the last data row and return the next sequential run ID (e.g. '#003')."""
    all_values: List[List[str]] = worksheet.get_all_values()
    data_rows = [r for r in all_values if r and r[0].startswith("#")]
    if not data_rows:
        return "#001"
    try:
        n = int(data_rows[-1][0].lstrip("#"))
        return f"#{n + 1:03d}"
    except ValueError:
        return "#001"


def _open_or_create_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Return the Performance worksheet, creating it with a header row if absent."""
    try:
        return spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADER_ROW))
        ws.append_row(HEADER_ROW)
        return ws


def compute_relevant_per_source(
    signals: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Count unique LLM-output URLs per source.

    Builds a reverse map from URL → source using the pre-LLM items list, then
    walks all signal source_urls to tally unique URLs per source.
    """
    url_to_source: Dict[str, str] = {
        item["link"]: item["source"]
        for item in items
        if item.get("link") and item.get("source")
    }
    source_relevant_urls: Dict[str, Set[str]] = defaultdict(set)
    for signal in signals:
        for url in signal.get("source_urls", []):
            src = url_to_source.get(url)
            if src:
                source_relevant_urls[src].add(url)
    return {src: len(urls) for src, urls in source_relevant_urls.items()}


def append_what_run(
    source_counts: Dict[str, Dict[str, Any]],
    relevant_per_source: Dict[str, int],
) -> None:
    """Append one row per source to the Google Sheets Performance tab.

    Non-fatal: any auth, network, or API error is logged and swallowed so that
    a Sheets failure never blocks the Slack post.

    Args:
        source_counts:       Stage-count dict from RssFetcher (keyed by source name).
        relevant_per_source: Count of unique LLM-output URLs per source.
    """
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
    sa_path = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_PATH",
        "information-source-digest-0a187c6dd85d.json",
    )

    if not spreadsheet_id:
        logger.warning("GOOGLE_SPREADSHEET_ID not set — skipping Sheets write")
        return

    try:
        creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(spreadsheet_id)
        ws = _open_or_create_worksheet(spreadsheet)

        run_id = _get_next_run_id(ws)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: List[List[Any]] = []
        for source, counts in source_counts.items():
            in_window = counts.get("in_window", 0)
            relevant = relevant_per_source.get(source, 0)
            ratio = f"{(relevant / in_window * 100):.0f}%" if in_window > 0 else "0%"
            rows.append([run_id, timestamp, source.capitalize(), relevant, in_window, ratio])

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Sheets: wrote {len(rows)} rows for run {run_id}")

    except Exception as exc:
        logger.error(f"Sheets write failed (non-fatal): {exc}", exc_info=True)
