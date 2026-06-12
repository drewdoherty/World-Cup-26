"""CLI: back-fill model_prob and ev from the cached card to existing bets.

Usage::

    python scripts/wca_backfill_model.py [--card PATH] [--db PATH]

Parses the cached matchday card (recommendations with model probs + edges) and
matches bets in the ledger by (match, selection, odds). Updates matched bets
with model_prob and ev derived from the card.

Matching is fuzzy on odds (±0.05) to handle rounding in betslip screenshots.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def parse_card_picks(card_text: str) -> List[Dict[str, Any]]:
    """Extract picks from card markdown.

    Looks for lines like:
    *1. Australia vs Turkey* — Australia @ *5.50* (betfair_ex_uk)
        model 24.5% / mkt 18.1%  edge *+35.0%*
    """
    picks: List[Dict[str, Any]] = []
    lines = card_text.split("\n")

    current_pick: Optional[Dict[str, Any]] = None

    for line in lines:
        line_stripped = line.strip()

        # Match pick header: "*1. Team A vs Team B* — Selection @ *odds*"
        match_header = re.match(
            r"^\*\d+\.\s*(.+?)\*\s*—\s*(.+?)\s*@\s*\*([0-9.]+)\*",
            line_stripped
        )
        if match_header:
            current_pick = {
                "match": match_header.group(1).strip(),
                "selection": match_header.group(2).strip(),
                "decimal_odds": float(match_header.group(3)),
                "model_prob": None,
                "ev": None,
            }
            picks.append(current_pick)
            continue

        # Match model/edge line: "model 24.5% / mkt 18.1%  edge *+35.0%*"
        if current_pick and "model" in line_stripped.lower():
            model_match = re.search(r"model\s+([0-9.]+)%", line_stripped)
            edge_match = re.search(r"edge\s*\*?([+-]?[0-9.]+)%?\*?", line_stripped)

            if model_match:
                current_pick["model_prob"] = float(model_match.group(1)) / 100.0
            if edge_match:
                edge_str = edge_match.group(1)
                # Edge can be "+35.0%" or "+35.0", we want it as a decimal
                try:
                    current_pick["ev"] = float(edge_str) / 100.0
                except ValueError:
                    pass

    return [p for p in picks if p.get("model_prob") is not None or p.get("ev") is not None]


def match_bet_to_pick(
    bet: Dict[str, Any],
    picks: List[Dict[str, Any]],
    odds_tolerance: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Find a pick that matches a bet (fuzzy on odds)."""
    bet_match = (bet.get("match_desc") or "").lower()
    bet_sel = (bet.get("selection") or "").lower()
    bet_odds = float(bet.get("decimal_odds") or 0.0)

    for pick in picks:
        pick_match = (pick.get("match") or "").lower()
        pick_sel = (pick.get("selection") or "").lower()
        pick_odds = float(pick.get("decimal_odds") or 0.0)

        # Fuzzy match: both must contain each other's key words
        match_words_bet = set(bet_match.split())
        match_words_pick = set(pick_match.split())
        if not (match_words_bet & match_words_pick):
            continue

        sel_words_bet = set(bet_sel.split())
        sel_words_pick = set(pick_sel.split())
        if not (sel_words_bet & sel_words_pick):
            continue

        # Odds must be within tolerance
        if abs(bet_odds - pick_odds) > odds_tolerance:
            continue

        return pick

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back-fill model_prob and ev from cached card to ledger bets."
    )
    parser.add_argument("--card", default="data/card_latest.md", help="Card path")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    args = parser.parse_args()

    # Read card
    if not Path(args.card).exists():
        print(f"ERROR: card file not found: {args.card}", file=sys.stderr)
        sys.exit(1)

    card_text = Path(args.card).read_text(encoding="utf-8")
    picks = parse_card_picks(card_text)

    print(f"Parsed {len(picks)} picks from card")

    # Connect to ledger
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        bets = con.execute("SELECT id, match_desc, selection, decimal_odds FROM bets WHERE model_prob IS NULL").fetchall()

        matched = 0
        for bet in bets:
            pick = match_bet_to_pick(dict(bet), picks)
            if not pick:
                continue

            con.execute(
                "UPDATE bets SET model_prob = ?, ev = ? WHERE id = ?",
                (pick["model_prob"], pick["ev"], bet["id"]),
            )
            matched += 1
            print(f"✓ Bet {bet['id']}: {bet['selection']} @ {bet['decimal_odds']:.2f} "
                  f"← model {pick['model_prob']*100:.1f}% edge {pick['ev']*100:+.1f}%")

        con.commit()
        print(f"\nBack-filled {matched} bets")

    finally:
        con.close()


if __name__ == "__main__":
    main()
