"""
sheets_writer.py — Append per-source sieve stats to a Google Spreadsheet
after each "What" profile run (only once Slack has succeeded).
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES: List[str] = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME: str = "Performance"
HEADER_ROW: List[str] = [
    "Run ID", "Timestamp", "Source Name", "Relevant", "Fetched", "Ratio"
]


def spreadsheet_link(spreadsheet_id: Optional[str]) -> str:
    """Slack mrkdwn link to the spreadsheet, or '' when the id is missing."""
    if not spreadsheet_id:
        return ""
    return f"<https://docs.google.com/spreadsheets/d/{spreadsheet_id}|Sheets>"


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


def _ensure_header(worksheet: gspread.Worksheet) -> None:
    """Create or rewrite the header so column labels match what we write."""
    existing = worksheet.row_values(1)
    if existing[: len(HEADER_ROW)] == HEADER_ROW:
        return
    worksheet.update([HEADER_ROW], range_name="A1:F1")


def _open_or_create_worksheet(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Return the Performance worksheet, creating it with a header row if absent."""
    try:
        ws = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADER_ROW))
        ws.append_row(HEADER_ROW)
        return ws
    _ensure_header(ws)
    return ws


def rows_for_run(
    source_counts: Dict[str, Dict[str, Any]],
    run_id: str,
    timestamp: str,
) -> List[List[Any]]:
    """One row per source: keyword-relevant / fetched. No live Sheets I/O."""
    rows: List[List[Any]] = []
    for source, counts in source_counts.items():
        fetched = counts.get("fetched", 0)
        relevant = counts.get("relevant", 0)
        ratio = f"{(relevant / fetched * 100):.0f}%" if fetched > 0 else "0%"
        rows.append(
            [run_id, timestamp, source.capitalize(), relevant, fetched, ratio]
        )
    return rows


def append_what_run(source_counts: Dict[str, Dict[str, Any]]) -> None:
    """Append one row per source to the Google Sheets Performance tab.

    Non-fatal: any auth, network, or API error is logged and swallowed so that
    a Sheets failure never undoes a successful Slack post.

    Args:
        source_counts: Stage-count dict from RssFetcher (keyed by source name).
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
        rows = rows_for_run(source_counts, run_id, timestamp)

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Sheets: wrote {len(rows)} rows for run {run_id}")

    except Exception as exc:
        logger.error(f"Sheets write failed (non-fatal): {exc}", exc_info=True)
