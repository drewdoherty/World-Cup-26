"""CLI: record match outcomes and bet settlements to the ledger.

Usage::

    python scripts/wca_settle.py --bet-id 123 --closing-odds 3.45 --outcome won
    python scripts/wca_settle.py --bet-id 124 --closing-odds 2.10 --outcome lost
    python scripts/wca_settle.py --bet-id 125 --outcome void

Logs the result (outcome + closing odds) to the ledger and computes realized P&L
and closing-line-value (CLV). This is the manual settlement path — intended for
daily post-match bookkeeping.

Requires: bet ID (from the ledger) and outcome (won/lost/void).  ``--closing-odds``
is optional when the bet already carries an auto-captured close (the snapshot
daemon stamps 1X2 bets at kickoff — see ``wca_close_capture.py``); passing it
explicitly always overrides the stored value.

The script computes::

    settled_pl = stake * (decimal_odds - 1)   if outcome='won' else -stake
    clv = decimal_odds / closing_odds - 1

where decimal_odds is the price the bet was BACKED at. CLV > 0 means the
backed price beat the closing line. This matches the convention used by
every other CLV producer in the ledger.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Record match settlement and compute realized P&L + CLV."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument("--bet-id", type=int, required=True, help="Bet ID to settle")
    parser.add_argument(
        "--outcome",
        required=True,
        choices=["won", "lost", "void"],
        help="Outcome of the bet",
    )
    parser.add_argument(
        "--closing-odds",
        type=float,
        help="De-vigged FAIR decimal close (optional when an auto-captured "
        "close is already stamped on the bet; overrides it when given). Pass "
        "a vig-removed consensus, not a raw single-book quote — a raw quote "
        "overstates CLV vs auto-captured rows. See wca_close_capture.py.",
    )
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        # Fetch the open bet
        row = con.execute(
            "SELECT id, stake, decimal_odds, model_prob, closing_odds FROM bets WHERE id = ? AND status = 'open'",
            (args.bet_id,),
        ).fetchone()

        if not row:
            print(f"ERROR: No open bet with ID {args.bet_id}", file=sys.stderr)
            sys.exit(1)

        stake = float(row["stake"] or 0.0)
        odds_backed = float(row["decimal_odds"] or 0.0)
        model_prob = float(row["model_prob"] or 0.0)

        # Explicit --closing-odds wins; otherwise fall back to the close the
        # snapshot daemon auto-captured at kickoff.
        closing_odds = args.closing_odds
        closing_source = "manual"
        if closing_odds is None and row["closing_odds"] is not None:
            closing_odds = float(row["closing_odds"])
            closing_source = "auto-captured"
        if args.outcome in ("won", "lost") and closing_odds is None:
            print(
                "ERROR: --closing-odds required for 'won'/'lost' (no auto-captured "
                "close on this bet — run scripts/wca_close_capture.py or pass it "
                "explicitly)",
                file=sys.stderr,
            )
            sys.exit(1)

        # Compute realized P&L — a win pays at the odds the bet was BACKED
        # at, not the closing price (the close only matters for CLV).
        if args.outcome == "won":
            settled_pl = stake * (odds_backed - 1)
        elif args.outcome == "lost":
            settled_pl = -stake
        else:  # void
            settled_pl = 0.0

        # CLV: backed price vs closing line (ledger convention: ratio - 1).
        clv = None
        if odds_backed > 0 and closing_odds and closing_odds > 0:
            clv = odds_backed / closing_odds - 1

        # Update the ledger
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        con.execute(
            "UPDATE bets SET status = ?, settled_pl = ?, closing_odds = ?, clv = ?, settled_ts = ? WHERE id = ?",
            (args.outcome, settled_pl, closing_odds, clv, now_utc, args.bet_id),
        )
        con.commit()

        # Report
        print(f"✅ Bet {args.bet_id} settled as '{args.outcome}'")
        if closing_odds:
            print(f"   Closing odds: {closing_odds:.2f} ({closing_source})")
        print(f"   Realized P&L: {settled_pl:+.2f}")
        if clv is not None:
            print(f"   CLV: {clv:+.4f} ({clv*100:+.2f}%)")

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
