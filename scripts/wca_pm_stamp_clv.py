#!/usr/bin/env python
"""Stamp Polymarket advancement closes onto the ledger (mini-side).

WHY this script exists (network topology)
------------------------------------------
Polymarket's CLOB is reachable only from the MacBook (VPN); the Mac mini
(production, canonical ``data/wca.db``) is PM-blind. So the close price is
captured on the MacBook (``scripts/wca_pm_close_capture.py``) into a small
committed JSON artifact, ``data/pm_closes.json``, delivered here by the
normal git autopull/merge — this script never makes a network call, it only
reads that artifact and the local ledger.

    MacBook: scripts/wca_pm_close_capture.py  -> git ->  data/pm_closes.json
    Mini:    scripts/wca_pm_stamp_clv.py reads data/pm_closes.json, stamps
             closing_odds + clv onto matching platform='polymarket' bets.

Join key (documented fuzziness)
--------------------------------
No historical PM bet row has ``token_id`` populated (the column exists on
``bets`` but nothing has written it yet), so the join is on **team + stage**
parsed from each side's free text:

* the close row's ``question`` (the market's own wording, e.g. "Will Ghana
  be eliminated in the Round of 32 at the 2026 FIFA World Cup?");
* the bet's ``match_desc`` / ``selection`` / ``notes`` (e.g. ``selection``
  ``"Japan reach R16 - No"`` or ``match_desc`` ``"Ghana eliminated R32 of
  the World Cup"``).

When the bet's stage can't be parsed but its team has exactly one captured
close, that close is used (unambiguous fallback); a team with several
captured stage-closes and no parseable stage on the bet is left unstamped
rather than guessed at — see :func:`wca.pmclose.match_bet_to_close`. Once
bets start recording ``token_id`` at placement time, this should be switched
to an exact token join (kept as a follow-up, not implemented here to avoid
touching the placement/recording path in a CLV-only change).

A bet can back either share of the same market — e.g. the real ledger has
``"No — Ghana not eliminated in Round of 32"`` (backing the NO/eliminated
share). The captured close row always carries the YES mid, so
:func:`wca.pmclose.fair_close_mid_for_bet` complements it to ``1 - mid``
before the CLV/closing-odds arithmetic when the selection is a "No" bet.

Idempotent: only bets with ``platform='polymarket' AND closing_odds IS
NULL`` are touched, and a bet without a resolvable close row is left alone
for a future run to pick up once the MacBook side captures it — a rerun with
nothing new stamps nothing (a true no-op).

Usage
-----
    # After `git pull` has delivered a fresh data/pm_closes.json:
    PYTHONPATH=src python scripts/wca_pm_stamp_clv.py

    # Dry run (report what would be stamped, write nothing):
    PYTHONPATH=src python scripts/wca_pm_stamp_clv.py --dry-run

    # Explicit paths (e.g. testing against a copy):
    PYTHONPATH=src python scripts/wca_pm_stamp_clv.py \\
        --db data/wca.db --artifact data/pm_closes.json
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmclose  # noqa: E402

_DEFAULT_DB = "data/wca.db"
_DEFAULT_ARTIFACT = "data/pm_closes.json"

# ``bets.market`` values (casefolded) that price a single-match moneyline
# rather than an advancement/futures outcome — mirrors
# ``wca.closecapture._X12_MARKETS``. These are EXCLUDED here: they already
# have their own dedicated close (the sportsbook consensus at the fixture's
# own kickoff, via ``wca.closecapture.rebackfill_pm_closes``), and — unlike
# every advancement rung, where a team has exactly one market per stage — a
# team plays several match_1x2 markets with no stage text to disambiguate
# them, so admitting them here risks the team-only fallback in
# :func:`wca.pmclose.match_bet_to_close` mis-joining a moneyline bet to an
# unrelated advancement-rung close captured for the same team.
_MONEYLINE_MARKETS = frozenset(
    {"h2h", "full-time result", "full time result", "match odds",
     "match winner", "match", "pm_moneyline"}
)


def _is_moneyline_market(market: Any) -> bool:
    return isinstance(market, str) and market.strip().casefold() in _MONEYLINE_MARKETS


def stamp_clv(
    con: sqlite3.Connection,
    closes: List[Dict[str, Any]],
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Stamp ``closing_odds``/``clv`` on open, unstamped PM advancement bets.

    Candidates: ``platform='polymarket' AND closing_odds IS NULL``, EXCLUDING
    single-match moneyline bets (see :data:`_MONEYLINE_MARKETS` — those are
    ``wca.closecapture.rebackfill_pm_closes``'s job), any ``status`` (an
    advancement bet is often already settled by the time its close is
    captured, and a settled bet's CLV is just as real a KPI input as an open
    one; unlike the 1X2 closecapture path this does not require
    ``status='open'``). Returns one record per bet actually stamped:
    ``{"bet_id", "match", "selection", "decimal_odds", "closing_odds", "clv",
    "close_ts", "question"}``.
    """
    by_team_stage, by_team = pmclose.index_closes(closes)

    try:
        bets = con.execute(
            "SELECT id, match_desc, selection, decimal_odds, notes, market "
            "FROM bets WHERE platform='polymarket' AND closing_odds IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    stamped: List[Dict[str, Any]] = []
    for bet_id, match_desc, selection, decimal_odds, notes, market in bets:
        if _is_moneyline_market(market):
            continue
        row = pmclose.match_bet_to_close(
            match_desc, selection, notes, by_team_stage, by_team
        )
        if row is None:
            continue
        # Complement the captured YES mid to 1-mid for a "No" bet (e.g. "No —
        # Ghana not eliminated in Round of 32" backs Ghana's NO share) — see
        # wca.pmclose.fair_close_mid_for_bet.
        mid = pmclose.fair_close_mid_for_bet(selection, row)
        closing_odds = pmclose.closing_odds_from_mid(mid)
        clv = pmclose.clv_from_mid(decimal_odds, mid)
        if closing_odds is None or clv is None:
            continue
        if not dry_run:
            con.execute(
                "UPDATE bets SET closing_odds=?, clv=? "
                "WHERE id=? AND platform='polymarket' AND closing_odds IS NULL",
                (closing_odds, clv, bet_id),
            )
        stamped.append(
            {
                "bet_id": bet_id,
                "match": match_desc,
                "selection": selection,
                "decimal_odds": float(decimal_odds),
                "closing_odds": closing_odds,
                "clv": clv,
                "close_ts": row.get("close_ts_utc"),
                "question": row.get("question"),
            }
        )
    if stamped and not dry_run:
        con.commit()
    return stamped


def stamp_clv_db(
    db_path: str,
    artifact_path: str,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Open *db_path* + *artifact_path* and run :func:`stamp_clv`."""
    closes = pmclose.load_closes(artifact_path)
    if not closes:
        return []
    con = sqlite3.connect(db_path)
    try:
        return stamp_clv(con, closes, dry_run=dry_run)
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stamp closing_odds + CLV onto Polymarket ledger bets from "
        "data/pm_closes.json (mini-side; no network access)."
    )
    parser.add_argument(
        "--db", default=_DEFAULT_DB,
        help="SQLite ledger path (default: data/wca.db).",
    )
    parser.add_argument(
        "--artifact", default=_DEFAULT_ARTIFACT,
        help="Close-capture artifact path (default: data/pm_closes.json).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be stamped without writing.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("ERROR: ledger DB not found at %s" % args.db, file=sys.stderr)
        return 1
    if not os.path.exists(args.artifact):
        print(
            "no-op: artifact not found at %s (nothing captured yet)" % args.artifact
        )
        return 0

    try:
        records = stamp_clv_db(args.db, args.artifact, dry_run=args.dry_run)
    except sqlite3.Error as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    verb = "would stamp" if args.dry_run else "stamped"
    if not records:
        print("no Polymarket bets matched a captured close (%s 0)" % verb)
    for rec in records:
        print(
            "%s bet %d: %s — %s @ %.3f | close %.4f mid (%s) | CLV %+.2f%%"
            % (
                verb,
                rec["bet_id"],
                rec["match"],
                rec["selection"],
                rec["decimal_odds"],
                rec["closing_odds"],
                rec["close_ts"],
                rec["clv"] * 100.0,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
