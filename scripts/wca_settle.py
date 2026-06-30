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

Realized P&L follows the shared ``wca.ledger.store.settled_pl`` schedule, so this
CLI agrees with the Telegram ``/settle`` command and ``store.settle_bet`` on every
row, including free bets and lays:

* back bet:  won -> ``stake*(decimal_odds-1)`` ; lost -> ``-stake``.
* free bet (``source='offer'``, stake NOT returned): won -> profit only ; lost ->
  ``£0`` (no stake at risk). Detected from the bet's stored ``source`` unless
  overridden with ``--free`` / ``--no-free``.
* lay (``Lay (Bet Against)``): won -> ``+stake`` ; lost -> ``-liability``
  (``stake*(odds-1)``).

    clv = decimal_odds / closing_odds - 1

where decimal_odds is the price the bet was BACKED at. CLV > 0 means the
backed price beat the closing line. This matches the convention used by
every other CLV producer in the ledger.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Make ``wca`` importable when this script is run standalone (CI/tests already
# have the editable install on sys.path; this is a no-op there).
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"),
)

from wca.ledger import store  # noqa: E402


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
    free_group = parser.add_mutually_exclusive_group()
    free_group.add_argument(
        "--free",
        dest="free",
        action="store_true",
        default=None,
        help="Settle as a free bet (stake NOT returned: a loss costs £0, a win "
        "pays profit only). Defaults to the bet's stored source ('offer' => "
        "free).",
    )
    free_group.add_argument(
        "--no-free",
        dest="free",
        action="store_false",
        help="Force normal-stake settlement even if the bet's source is 'offer'.",
    )
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        # source/account are added lazily for pre-existing DBs; ensure present
        # so the free-bet/lay-aware settlement below can read source.
        store._ensure_account_source_columns(con)
        # Fetch the open bet
        row = con.execute(
            "SELECT id, stake, decimal_odds, model_prob, closing_odds, source, "
            "market, selection FROM bets WHERE id = ? AND status = 'open'",
            (args.bet_id,),
        ).fetchone()

        if not row:
            print(f"ERROR: No open bet with ID {args.bet_id}", file=sys.stderr)
            sys.exit(1)

        stake = float(row["stake"] or 0.0)
        odds_backed = float(row["decimal_odds"] or 0.0)
        model_prob = float(row["model_prob"] or 0.0)

        # Free-bet handling: default to the stored source ('offer' => free), but
        # let --free / --no-free override for the odd row that was tagged wrong.
        source = str(row["source"] or "model")
        is_free = (source == "offer") if args.free is None else bool(args.free)
        is_lay = store.is_lay_bet(row["market"], row["selection"])

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

        # Realized P&L — a win pays at the odds the bet was BACKED at, not the
        # closing price (the close only matters for CLV). The void -> free -> lay
        # -> back schedule is shared with store.settle_bet and the bot's /settle
        # so all three paths agree on free bets and lays.
        settled_pl = store.settled_pl(
            args.outcome, stake, odds_backed, is_free=is_free, is_lay=is_lay
        )

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
        kind = "free bet" if is_free else ("lay" if is_lay else "back")
        print(f"✅ Bet {args.bet_id} settled as '{args.outcome}' ({kind})")
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
