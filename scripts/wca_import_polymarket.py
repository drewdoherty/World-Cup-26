"""CLI: bulk import Polymarket bets from portfolio activity.

Usage::

    python scripts/wca_import_polymarket.py --data bets.json [--db PATH]

Input format (bets.json):
[
  {
    "market": "Exact Score: United States 0 - 1 Paraguay?",
    "selection": "Yes",
    "shares": 11.1,
    "price": 0.09,
    "value": -1.03,
    "ts_utc": "2026-06-12T14:30:00"
  },
  ...
]

Imports settled bets with outcome='lost' (if value < 0) or 'won' (if value > 0).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def parse_exact_score_market(market: str) -> Optional[Dict[str, Any]]:
    """Extract match and scoreline from 'Exact Score: Team A X - Y Team B?' format."""
    # "Exact Score: United States 0 - 1 Paraguay?"
    m = re.match(
        r"Exact Score:\s*(.+?)\s+(\d+)\s*-\s*(\d+)\s+(.+?)\?",
        market
    )
    if not m:
        return None

    home_team = m.group(1).strip()
    home_goals = int(m.group(2))
    away_goals = int(m.group(3))
    away_team = m.group(4).strip()

    return {
        "match_desc": f"{home_team} vs {away_team}",
        "market": "Exact Score",
        "selection": f"{home_goals}-{away_goals}",
        "home_team": home_team,
        "away_team": away_team,
        "home_goals": home_goals,
        "away_goals": away_goals,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk import Polymarket exact score bets."
    )
    parser.add_argument("--data", required=True, help="JSON file with bets")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    args = parser.parse_args()

    # Read input
    if not Path(args.data).exists():
        print(f"ERROR: data file not found: {args.data}", file=sys.stderr)
        sys.exit(1)

    with open(args.data) as f:
        bets_data = json.load(f)

    if not isinstance(bets_data, list):
        print("ERROR: data must be a JSON list", file=sys.stderr)
        sys.exit(1)

    # Import to ledger
    con = sqlite3.connect(args.db)
    imported = 0
    skipped = 0

    for bet_data in bets_data:
        market_str = bet_data.get("market", "")
        parsed = parse_exact_score_market(market_str)

        if not parsed:
            print(f"⚠️  Skipped (unrecognized market): {market_str}")
            skipped += 1
            continue

        selection = bet_data.get("selection", "").strip()
        shares = float(bet_data.get("shares", 0))
        price = float(bet_data.get("price", 0))
        ts_utc = bet_data.get("ts_utc", "")

        # Compute stake (cost to open the position)
        stake = shares * price

        # Generate match_id from home/away team names
        match_id = f"{parsed['home_team'].upper().replace(' ', '')}_{parsed['away_team'].upper().replace(' ', '')}"

        # Convert Polymarket price (probability 0-1) to decimal odds
        # Price 0.09 (9%) → Decimal odds 1/0.09 = 11.11
        decimal_odds = 1.0 / price if price > 0 else None

        try:
            con.execute(
                """INSERT INTO bets
                   (ts_utc, match_id, match_desc, market, selection, platform,
                    decimal_odds, stake, status,
                    account, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts_utc,
                    match_id,
                    parsed["match_desc"],
                    parsed["market"],
                    selection,
                    "polymarket",
                    decimal_odds,  # Converted to decimal odds
                    stake,
                    "open",  # Status is OPEN (not yet settled)
                    "1",  # account 1 (default polymarket account)
                    "punt",  # source = punt (discretionary exact score bets)
                ),
            )
            imported += 1
            print(f"✓ {parsed['match_desc']} {parsed['selection']} @ ${price:.2f} "
                  f"({shares:.1f} shares) — open")
        except Exception as e:
            print(f"✗ Failed to import {market_str}: {e}")
            skipped += 1

    con.commit()
    con.close()

    print(f"\n✅ Imported {imported} bets, skipped {skipped}")


if __name__ == "__main__":
    main()
