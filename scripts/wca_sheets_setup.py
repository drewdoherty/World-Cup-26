"""One-time setup: create the WCA bet-ledger Google Sheet and populate it.

Run ONCE from the mini (or any machine with wca.db access) after you have:
  1. Created a Google Cloud project + enabled Sheets API + Drive API
  2. Created a Service Account and downloaded the JSON key
  3. Set SHEETS_CREDS_PATH=data/sheets_creds.json (or the path you chose)
  4. NOT yet set SHEETS_BET_LEDGER_ID (this script sets it for you)

The script will:
  - Create a new spreadsheet named "WCA Bet Ledger"
  - Create Open Bets + Closed Bets tabs with headers
  - Freeze + bold headers, set column widths
  - Load all current bets from wca.db
  - Print the sheet URL and the env-var line to add to .env / .env.conductor

Usage::

    SHEETS_CREDS_PATH=data/sheets_creds.json python scripts/wca_sheets_setup.py
    # or with an existing sheet ID to just repopulate:
    SHEETS_CREDS_PATH=data/sheets_creds.json SHEETS_BET_LEDGER_ID=<id> python scripts/wca_sheets_setup.py --repopulate
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_DB = "data/wca.db"


def _connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _setup_sheet(sh, repopulate: bool) -> None:
    import gspread
    from wca.sheets import client

    # Create / verify tabs
    ws_open   = client.get_or_create_worksheet(sh, client.TAB_OPEN,   client.OPEN_HEADERS)
    ws_closed = client.get_or_create_worksheet(sh, client.TAB_CLOSED, client.CLOSED_HEADERS)

    if repopulate:
        # Wipe data rows (keep header)
        for ws in (ws_open, ws_closed):
            all_vals = ws.get_all_values()
            if len(all_vals) > 1:
                ws.delete_rows(2, len(all_vals))

    # Apply column widths (cosmetic, tolerates failure)
    _apply_formatting(sh, ws_open, ws_closed)

    return ws_open, ws_closed


def _apply_formatting(sh, ws_open, ws_closed) -> None:
    try:
        import gspread
        # Freeze row 1, bold header
        for ws in (ws_open, ws_closed):
            sh.batch_update({
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": ws.id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    }
                ]
            })
            ws.format("1:1", {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.27, "green": 0.29, "blue": 0.82},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            })
    except Exception as e:
        logger.warning("formatting skipped: %s", e)


def _load_bets(ws_open, ws_closed, db_path: str) -> None:
    from wca.sheets.sync import _bet_to_open_row, _bet_to_closed_row
    from wca.sheets import client

    con = _connect(db_path)
    try:
        bets = [dict(r) for r in con.execute("SELECT * FROM bets ORDER BY id").fetchall()]
    finally:
        con.close()

    open_rows   = [_bet_to_open_row(b)   for b in bets if b["status"] == "open"]
    closed_rows = [_bet_to_closed_row(b) for b in bets if b["status"] != "open"]

    logger.info("Loading %d open bets...", len(open_rows))
    if open_rows:
        ws_open.append_rows(
            [[str(r.get(h, "") or "") for h in client.OPEN_HEADERS] for r in open_rows],
            value_input_option="RAW",
        )

    logger.info("Loading %d closed bets...", len(closed_rows))
    if closed_rows:
        # Batch in chunks of 100 to avoid quota errors
        chunk = 100
        for i in range(0, len(closed_rows), chunk):
            batch = closed_rows[i:i+chunk]
            ws_closed.append_rows(
                [[str(r.get(h, "") or "") for h in client.CLOSED_HEADERS] for r in batch],
                value_input_option="RAW",
            )
            if i + chunk < len(closed_rows):
                time.sleep(1)

    logger.info("Done: %d open + %d closed bets loaded.", len(open_rows), len(closed_rows))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Create and populate the WCA Google Sheets ledger.")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to wca.db")
    parser.add_argument(
        "--repopulate",
        action="store_true",
        help="Wipe existing data rows and reload from wca.db (use when schema changes).",
    )
    args = parser.parse_args(argv)

    # Check creds
    creds_path = os.environ.get("SHEETS_CREDS_PATH", "data/sheets_creds.json")
    if not os.path.exists(creds_path):
        print(f"\nERROR: Credentials file not found at {creds_path}")
        print("\nSetup steps:")
        print("  1. Go to https://console.cloud.google.com")
        print("  2. Create a project (or use existing)")
        print("  3. Enable 'Google Sheets API' and 'Google Drive API'")
        print("  4. IAM & Admin → Service Accounts → Create Service Account")
        print("  5. Keys → Add Key → JSON → save to", creds_path)
        print("  6. Re-run this script\n")
        sys.exit(1)

    import gspread
    from google.oauth2.service_account import Credentials
    from wca.sheets import client

    existing_id = os.environ.get("SHEETS_BET_LEDGER_ID", "")

    # Connect (create sheet if needed)
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    gc = gspread.authorize(creds)

    if existing_id and not args.repopulate:
        sh = gc.open_by_key(existing_id)
        logger.info("Using existing sheet: %s", sh.url)
    else:
        if existing_id and args.repopulate:
            sh = gc.open_by_key(existing_id)
            logger.info("Repopulating existing sheet: %s", sh.url)
        else:
            sh = gc.create("WCA Bet Ledger")
            sh.share(None, perm_type="anyone", role="writer")  # share-by-link; restrict later
            logger.info("Created new sheet: %s", sh.url)
            print(f"\nSHEET CREATED: {sh.url}")
            print(f"\nAdd to .env and .env.conductor on the mini:")
            print(f"  SHEETS_BET_LEDGER_ID={sh.id}")
            print(f"  SHEETS_CREDS_PATH=data/sheets_creds.json\n")

    os.environ["SHEETS_BET_LEDGER_ID"] = sh.id
    ws_open, ws_closed = _setup_sheet(sh, repopulate=args.repopulate)
    _load_bets(ws_open, ws_closed, args.db)

    print(f"\n✓ Sheet ready: {sh.url}")
    print(f"\nSHEETS_BET_LEDGER_ID={sh.id}")


if __name__ == "__main__":
    main()
