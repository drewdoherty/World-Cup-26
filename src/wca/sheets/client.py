"""Google Sheets client: auth, worksheet access, and low-level I/O.

Auth strategy: Service Account JSON key (works headlessly on mini, no
token expiry, no user interaction). Credentials live in the file path
given by SHEETS_CREDS_PATH env var (defaults to data/sheets_creds.json).
The sheet ID comes from SHEETS_BET_LEDGER_ID env var.

Never import this directly in tests — use a patched client.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_DEFAULT_CREDS = "data/sheets_creds.json"
_ENV_CREDS = "SHEETS_CREDS_PATH"
_ENV_SHEET_ID = "SHEETS_BET_LEDGER_ID"

# Tab names
TAB_OPEN = "Open Bets"
TAB_CLOSED = "Closed Bets"

# Column headers — order matters (A=1 … )
OPEN_HEADERS = [
    "bet_id",           # A  wca.db primary key
    "placed_utc",       # B
    "match_desc",       # C
    "market",           # D
    "selection",        # E
    "platform",         # F
    "account",          # G
    "source",           # H
    "odds",             # I
    "stake",            # J
    "model_prob",       # K
    "mkt_prob_devig",   # L
    "ev",               # M
    "kelly_frac",       # N
    "notes",            # O  editable — sheet wins
    "token_id",         # P  Polymarket token
    "status",           # Q  always 'open' while here
]

CLOSED_HEADERS = OPEN_HEADERS + [
    "result",           # R  won/lost/void/cashed
    "settled_pl",       # S
    "closing_odds",     # T  editable — sheet wins
    "clv_pct",          # U  decimal_odds/closing_odds - 1
    "settled_utc",      # V
]

# Columns where a manual sheet edit should be pulled back to wca.db
# (everything else is pushed FROM wca.db and should not be overwritten by sheet)
EDITABLE_OPEN_COLS = {"notes", "closing_odds"}
EDITABLE_CLOSED_COLS = {"notes", "closing_odds", "clv_pct"}


def _creds_path() -> str:
    return os.environ.get(_ENV_CREDS, _DEFAULT_CREDS)


def _sheet_id() -> str:
    sid = os.environ.get(_ENV_SHEET_ID, "")
    if not sid:
        raise RuntimeError(
            "SHEETS_BET_LEDGER_ID env var not set. "
            "Run wca_sheets_setup.py first to create the sheet."
        )
    return sid


def connect() -> gspread.Spreadsheet:
    """Authenticate and return the spreadsheet object."""
    creds_file = _creds_path()
    if not os.path.exists(creds_file):
        raise FileNotFoundError(
            f"Sheets credentials not found at {creds_file}. "
            "See docs/sheets_setup.md for instructions."
        )
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(_sheet_id())


def get_or_create_worksheet(
    sh: gspread.Spreadsheet, title: str, headers: List[str]
) -> gspread.Worksheet:
    """Get existing worksheet or create with headers if missing."""
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        _format_header_row(ws, len(headers))
    return ws


def _format_header_row(ws: gspread.Worksheet, n_cols: int) -> None:
    """Bold + freeze the header row."""
    try:
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    except Exception:
        pass  # formatting is cosmetic, never fail on it


def all_rows_as_dicts(ws: gspread.Worksheet) -> List[Dict[str, Any]]:
    """Return all data rows (skip header) as list of dicts keyed by header."""
    return ws.get_all_records(default_blank=None)


def upsert_row(
    ws: gspread.Worksheet,
    headers: List[str],
    row_data: Dict[str, Any],
    key_col: str = "bet_id",
) -> None:
    """Update the row matching key_col value, or append if not found.

    Retries once on quota errors.
    """
    key_val = str(row_data.get(key_col, ""))
    all_rows = ws.get_all_values()
    if not all_rows:
        ws.append_row(headers, value_input_option="RAW")
        _format_header_row(ws, len(headers))
        all_rows = ws.get_all_values()

    header_row = all_rows[0]
    try:
        key_idx = header_row.index(key_col)
    except ValueError:
        # Sheet has wrong headers; rebuild
        ws.clear()
        ws.append_row(headers, value_input_option="RAW")
        _format_header_row(ws, len(headers))
        all_rows = ws.get_all_values()
        key_idx = headers.index(key_col)

    # Find the row index (1-based, sheet row = list index + 1 because we skip header)
    target_row = None
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) > key_idx and str(row[key_idx]) == key_val:
            target_row = i
            break

    values = [str(row_data.get(h, "") or "") for h in headers]
    _retry_write(lambda: (
        ws.delete_rows(target_row) or ws.insert_row(values, index=target_row, value_input_option="RAW")
    ) if target_row else ws.append_row(values, value_input_option="RAW"))


def delete_row_by_key(
    ws: gspread.Worksheet,
    key_col: str,
    key_val: str,
) -> bool:
    """Delete the row where key_col == key_val. Returns True if found."""
    all_rows = ws.get_all_values()
    if not all_rows:
        return False
    header_row = all_rows[0]
    try:
        key_idx = header_row.index(key_col)
    except ValueError:
        return False
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) > key_idx and str(row[key_idx]) == str(key_val):
            _retry_write(lambda idx=i: ws.delete_rows(idx))
            return True
    return False


def _retry_write(fn, retries: int = 3, delay: float = 2.0) -> Any:
    """Retry a Sheets write call on quota/transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
    raise last_err
