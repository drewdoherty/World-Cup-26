"""CLI: backfill EV for Polymarket exact score bets using model scoreline probs.

Usage::

    python scripts/wca_backfill_pm_ev.py [--card PATH] [--db PATH]

Parses the card scorelines, matches them to Polymarket exact score bets by
(match, scoreline), and calculates EV = (model_prob × decimal_odds) - 1.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional


def parse_card_scorelines(card_text: str) -> Dict[str, Dict[str, float]]:
    """Extract scoreline probabilities from card.

    Returns: {match: {scoreline: probability, ...}, ...}
    Example: {"Canada vs Bosnia-Herzegovina": {"1-0": 0.145, "1-1": 0.115, ...}}
    """
    scorelines: Dict[str, Dict[str, float]] = {}
    current_match = None

    for line in card_text.split("\n"):
        line_stripped = line.strip()

        # Match header: "*Match Name*"
        if line_stripped.startswith("*") and line_stripped.endswith("*") and "-" not in line_stripped:
            current_match = line_stripped.strip("*").strip()
            scorelines[current_match] = {}
            continue

        # Scoreline row: "1-0  14.5%  fair 6.91  back >= 7.04"
        if current_match and re.match(r"^\d+-\d+\s+", line_stripped):
            m = re.match(r"^(\d+-\d+)\s+([0-9.]+)%", line_stripped)
            if m:
                scoreline = m.group(1)
                prob = float(m.group(2)) / 100.0
                scorelines[current_match][scoreline] = prob

    return scorelines


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill EV for Polymarket exact score bets")
    parser.add_argument("--card", default="data/card_latest.md", help="Card path")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    args = parser.parse_args()

    # Parse card
    card_text = Path(args.card).read_text(encoding="utf-8")
    scorelines = parse_card_scorelines(card_text)

    # Connect to ledger
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # Get all Polymarket exact score bets
    bets = con.execute(
        "SELECT id, match_desc, selection, decimal_odds FROM bets "
        "WHERE platform='polymarket' AND market='Exact Score' AND ev IS NULL"
    ).fetchall()

    updated = 0
    for bet in bets:
        match_desc = bet["match_desc"]
        selection = bet["selection"]  # e.g., "United States 0-1 Paraguay"
        decimal_odds = float(bet["decimal_odds"])

        # Extract scoreline from selection
        # "United States 0-1 Paraguay" → extract "0-1"
        scoreline_match = re.search(r"(\d+-\d+)", selection)
        if not scoreline_match:
            print(f"⚠️  Could not parse scoreline from: {selection}")
            continue

        scoreline = scoreline_match.group(1)

        # Find matching card entry
        model_prob = None
        for card_match, card_scores in scorelines.items():
            # Fuzzy match match_desc to card entry
            match_words = set(match_desc.lower().split())
            card_words = set(card_match.lower().split())

            if match_words & card_words:  # At least some word overlap
                if scoreline in card_scores:
                    model_prob = card_scores[scoreline]
                    break

        if model_prob is None:
            print(f"⚠️  No model prob found: {match_desc} {scoreline}")
            continue

        # Calculate EV
        ev = (model_prob * decimal_odds) - 1

        # Update ledger
        con.execute(
            "UPDATE bets SET ev = ? WHERE id = ?",
            (ev, bet["id"]),
        )
        updated += 1
        print(f"✓ {match_desc} {scoreline} @ {decimal_odds:.2f} "
              f"(prob {model_prob*100:.1f}%) → EV {ev:+.1%}")

    con.commit()
    con.close()

    print(f"\n✅ Updated {updated} bets")


if __name__ == "__main__":
    main()
