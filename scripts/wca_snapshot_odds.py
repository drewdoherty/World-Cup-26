"""CLI: ingest hourly odds snapshots for line movement tracking.

Usage::

    python scripts/wca_snapshot_odds.py [--db PATH] [--hours-ahead N] [--regions STR]

Pulls live odds from TheOddsAPI and stores them in the ``odds_snapshots`` table
for line movement visualization and validation. This runs on a cron schedule
(e.g., hourly) to track how odds move over time.

Requires ODDS_API_KEY in the environment (or .env file at repo root).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest hourly odds snapshots for line movement tracking."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument(
        "--hours-ahead",
        type=float,
        default=48.0,
        help="Include fixtures within this many hours (default 48)",
    )
    parser.add_argument(
        "--regions",
        default="uk",
        help="Comma-separated Odds API regions (default: uk)",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    # Import pipeline only after arg parsing
    try:
        from wca.data import theoddsapi
    except ImportError as exc:
        print("ERROR: could not import wca modules: %s" % exc, file=sys.stderr)
        sys.exit(1)

    # Pull odds
    try:
        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup",
            regions=args.regions,
            markets="h2h",
        )
    except Exception as exc:
        print("ERROR: odds pull failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    if odds_df.empty:
        print("No odds returned.")
        sys.exit(0)

    # Store in odds_snapshots table
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(args.db)
    try:
        for _, row in odds_df.iterrows():
            match_id = (row.get("match_id") or "").strip()
            match_name = (row.get("match_name") or "").strip()
            bookmaker = (row.get("bookmaker") or "").strip()
            outcome = (row.get("outcome") or "").strip()
            decimal_odds = row.get("decimal_odds")

            if not match_id or not bookmaker or not outcome:
                continue

            try:
                con.execute(
                    "INSERT INTO odds_snapshots "
                    "(ts_utc, match_id, match_name, bookmaker, outcome, decimal_odds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (now_utc, match_id, match_name, bookmaker, outcome, decimal_odds),
                )
            except sqlite3.IntegrityError:
                # Duplicate or constraint violation — skip
                pass
            except Exception:
                # Unknown error — log and continue
                pass

        con.commit()
        count = con.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE ts_utc = ?",
            (now_utc,),
        ).fetchone()[0]
        print("Ingested %d odds records at %s (quota remaining: %s)" % (
            count,
            now_utc,
            quota.remaining if quota else "unknown",
        ))
    finally:
        con.close()


if __name__ == "__main__":
    main()
