"""Periodic sync: reconcile wca.db ↔ Notion bet database.

Run by the analytics-live launchd job (every 10 min) or manually.

Usage::

    python scripts/wca_notion_sync.py              # full sync
    python scripts/wca_notion_sync.py --dry-run    # show what would change
    python scripts/wca_notion_sync.py --bet-id 77  # push one specific bet
    python scripts/wca_notion_sync.py --settle 77  # update one settled bet
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
    if not os.environ.get("NOTION_TOKEN"):
        missing.append("NOTION_TOKEN")
    if not os.environ.get("NOTION_BET_DB_ID"):
        missing.append("NOTION_BET_DB_ID")
    if missing:
        for m in missing:
            logger.warning("Notion sync skipped — missing: %s", m)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sync wca.db bets ↔ Notion database.")
    parser.add_argument("--db", default="data/wca.db", help="Path to wca.db")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--bet-id", type=int, help="Push one specific bet to Notion")
    parser.add_argument("--settle", type=int, help="Update one settled bet in Notion")
    args = parser.parse_args(argv)

    if not _check_env():
        return 0

    sys.path.insert(0, "src")
    from wca.notion.sync import full_sync, push_single_bet, settle_in_notion

    if args.bet_id:
        push_single_bet(args.bet_id, db_path=args.db)
        return 0

    if args.settle:
        settle_in_notion(args.settle, db_path=args.db)
        return 0

    counts = full_sync(db_path=args.db, dry_run=args.dry_run)
    prefix = "[DRY-RUN] " if args.dry_run else ""
    print(
        f"{prefix}Sync complete: "
        f"{counts['pushed']} pushed, "
        f"{counts['updated']} updated, "
        f"{counts['pulled_notes']} notes pulled, "
        f"{counts['pulled_closes']} closes pulled"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
