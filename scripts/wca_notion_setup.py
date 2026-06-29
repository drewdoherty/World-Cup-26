"""One-time setup: create the WCA Bet Ledger Notion database and populate it.

Run ONCE from the mini (or any machine with wca.db access) after you have:
  1. Created a Notion Internal Integration at https://www.notion.so/my-integrations
  2. Copied the integration token → set NOTION_TOKEN=<token>
  3. Created a blank Notion page to host the database (or let this script create one)
  4. Shared that page with your integration (Share → Invite → select integration)

The script will:
  - Create a "WCA Bet Ledger" database with all property columns
  - Load all current bets from wca.db
  - Print the database URL and the env-var line to add to .env

Usage::

    NOTION_TOKEN=secret_xxx python scripts/wca_notion_setup.py
    # or with an existing database ID to repopulate:
    NOTION_TOKEN=secret_xxx NOTION_BET_DB_ID=<id> python scripts/wca_notion_setup.py --repopulate
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
sys.path.insert(0, "src")


def _connect_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _create_database(client, parent_page_id: str) -> str:
    """Create the WCA Bet Ledger database under parent_page_id. Returns db_id."""
    db = client.databases.create(
        parent={"type": "page_id", "page_id": parent_page_id},
        title=[{"type": "text", "text": {"content": "WCA Bet Ledger"}}],
        properties={
            # Title (required by Notion — used for match_desc)
            "match_desc":    {"title": {}},
            # Numbers
            "bet_id":        {"number": {"format": "number"}},
            "odds":          {"number": {"format": "number"}},
            "stake":         {"number": {"format": "number"}},
            "model_prob":    {"number": {"format": "percent"}},
            "mkt_prob_devig":{"number": {"format": "percent"}},
            "ev":            {"number": {"format": "percent"}},
            "kelly_frac":    {"number": {"format": "percent"}},
            "settled_pl":    {"number": {"format": "number"}},
            "closing_odds":  {"number": {"format": "number"}},
            "clv_pct":       {"number": {"format": "percent"}},
            # Dates
            "placed_utc":    {"date": {}},
            "settled_utc":   {"date": {}},
            # Selects
            "status":        {"select": {"options": [
                {"name": "open",   "color": "green"},
                {"name": "won",    "color": "blue"},
                {"name": "lost",   "color": "red"},
                {"name": "void",   "color": "gray"},
                {"name": "cashed", "color": "yellow"},
            ]}},
            "platform":      {"select": {"options": [
                {"name": "smarkets"},
                {"name": "betfair"},
                {"name": "polymarket"},
                {"name": "bet365"},
                {"name": "betdaq"},
            ]}},
            "account":       {"select": {"options": [
                {"name": "1"},
                {"name": "2"},
            ]}},
            "source":        {"select": {"options": [
                {"name": "model"},
                {"name": "manual"},
                {"name": "promo"},
            ]}},
            # Text
            "market":        {"rich_text": {}},
            "selection":     {"rich_text": {}},
            "notes":         {"rich_text": {}},
            "token_id":      {"rich_text": {}},
        },
    )
    return db["id"]


def _load_bets(client, db_id: str, db_path: str) -> None:
    from wca.notion.client import bet_to_props, create_page

    con = _connect_db(db_path)
    try:
        bets = [dict(r) for r in con.execute("SELECT * FROM bets ORDER BY id").fetchall()]
    finally:
        con.close()

    logger.info("Loading %d bets into Notion...", len(bets))
    for i, b in enumerate(bets):
        props = bet_to_props(b)
        create_page(client, props, db_id=db_id)
        if (i + 1) % 10 == 0:
            logger.info("  %d/%d loaded", i + 1, len(bets))
            time.sleep(0.4)  # stay under Notion rate limit (3 req/s avg)
    logger.info("Done: %d bets loaded.", len(bets))


def _wipe_database(client, db_id: str) -> None:
    """Archive all pages in the database."""
    from wca.notion.client import query_all, archive_page

    pages = query_all(client, db_id=db_id)
    logger.info("Archiving %d existing pages...", len(pages))
    for p in pages:
        archive_page(client, p["id"])
        time.sleep(0.15)
    logger.info("Done wiping database.")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Create and populate the WCA Notion bet ledger.")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to wca.db")
    parser.add_argument("--parent-page-id", default="", help="Notion page ID to create the database under")
    parser.add_argument("--repopulate", action="store_true", help="Archive existing pages and reload from wca.db")
    args = parser.parse_args(argv)

    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        print("\nERROR: NOTION_TOKEN not set.")
        print("\nSetup steps:")
        print("  1. Go to https://www.notion.so/my-integrations")
        print("  2. New integration → name 'wca-ledger' → Submit")
        print("  3. Copy the Internal Integration Token")
        print("  4. In Notion, open the page that will host the database")
        print("     Share → Invite → find 'wca-ledger' integration")
        print("  5. Re-run: NOTION_TOKEN=secret_xxx python scripts/wca_notion_setup.py --parent-page-id <page_id>")
        print("\n  The page ID is the last part of the Notion URL:")
        print("  https://www.notion.so/My-Page-<PAGE_ID_HERE>\n")
        sys.exit(1)

    from notion_client import Client  # type: ignore
    client = Client(auth=token)

    existing_id = os.environ.get("NOTION_BET_DB_ID", "")

    if existing_id and args.repopulate:
        logger.info("Repopulating existing database %s", existing_id)
        _wipe_database(client, existing_id)
        _load_bets(client, existing_id, args.db)
        db_id = existing_id
    elif existing_id:
        logger.info("Database already exists. Use --repopulate to reload.")
        db_id = existing_id
    else:
        if not args.parent_page_id:
            print("\nERROR: --parent-page-id required when creating a new database.")
            print("  Find it in your Notion page URL after the last /")
            print("  e.g. https://notion.so/Betting-abcdef1234567890 → page ID = abcdef1234567890\n")
            sys.exit(1)

        logger.info("Creating new database under page %s", args.parent_page_id)
        db_id = _create_database(client, args.parent_page_id)
        logger.info("Database created: %s", db_id)
        _load_bets(client, db_id, args.db)

    db_url = f"https://www.notion.so/{db_id.replace('-', '')}"
    print(f"\n✓ Database ready: {db_url}")
    print(f"\nAdd to .env and .env.conductor on the mini:")
    print(f"  NOTION_TOKEN={token[:12]}...  (already set)")
    print(f"  NOTION_BET_DB_ID={db_id}\n")


if __name__ == "__main__":
    main()
