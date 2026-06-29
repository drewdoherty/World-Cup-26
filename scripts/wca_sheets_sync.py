"""Periodic sync daemon: reconcile wca.db ↔ Google Sheet.

Run by the analytics-live launchd job (every 10 min) or manually.

Usage::

    python scripts/wca_sheets_sync.py              # full sync
    python scripts/wca_sheets_sync.py --dry-run    # show what would change
    python scripts/wca_sheets_sync.py --bet-id 77  # push one specific bet
    python scripts/wca_sheets_sync.py --settle 77  # move bet 77 to Closed tab
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _check_env() -> bool:
    missing = []
    if not os.path.exists(os.environ.get("SHEETS_CREDS_PATH", "data/sheets_creds.json")):
        missing.append("SHEETS_CREDS_PATH (credentials file missing)")
    if not os.environ.get("SHEETS_BET_LEDGER_ID", ""):
        missing.append("SHEETS_BET_LEDGER_ID")
    if missing:
        for m in missing:
            logger.warning("Sheets sync skipped — missing: %s", m)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sync wca.db bets ↔ Google Sheet.")
    parser.add_argument("--db", default="data/wca.db", help="Path to wca.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--bet-id", type=int, help="Push one specific bet to Open Bets tab")
    parser.add_argument("--settle", type=int, help="Move one bet_id to Closed Bets tab")
    args = parser.parse_args(argv)

    if not _check_env():
        return 0  # soft exit — missing config is not a fatal error

    sys.path.insert(0, "src")
    from wca.sheets.sync import full_sync, push_single_bet, settle_in_sheet

    if args.bet_id:
        push_single_bet(args.bet_id, db_path=args.db)
        return 0

    if args.settle:
        settle_in_sheet(args.settle, db_path=args.db)
        return 0

    counts = full_sync(db_path=args.db, dry_run=args.dry_run)
    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(
        f"{prefix}Sync complete: "
        f"{counts['pushed_open']} open pushed, "
        f"{counts['pushed_closed']} closed pushed, "
        f"{counts['pulled_notes']} notes pulled, "
        f"{counts['pulled_closes']} closes pulled"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
