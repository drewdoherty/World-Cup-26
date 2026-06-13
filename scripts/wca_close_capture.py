#!/usr/bin/env python
"""Stamp closing odds + CLV onto open bets whose fixtures have kicked off.

The snapshot daemon runs this automatically after every poll; this CLI is the
manual / backfill path — e.g. stamping bets placed before the daemon hook
existed, or checking what *would* be stamped without writing.

For every open 1X2-style bet (h2h / Full-time result / Match Odds /
pm_moneyline, ...) with no ``closing_odds`` yet whose fixture has kicked off,
the last pre-kickoff ``odds_snapshots`` pull is de-vigged into a consensus
1X2 and the bet gets ``closing_odds`` (fair close for its selection) and
``clv`` (``backed / close - 1``).  Manually-stamped closes are never
overwritten; non-1X2 markets keep the manual ``wca_settle.py`` path.

Usage
-----
    python scripts/wca_close_capture.py [--db data/wca.db] \
        [--now 2026-06-13T01:05:00+00:00] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# Make ``src`` importable when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import closecapture  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stamp closing odds + CLV onto open bets after kickoff."
    )
    parser.add_argument(
        "--db", default="data/wca.db", help="SQLite ledger path (default: data/wca.db)."
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Override 'now' (ISO-8601 UTC) — only fixtures kicked off by "
        "this instant are stamped (default: current time).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be stamped without writing.",
    )
    parser.add_argument(
        "--rebackfill",
        action="store_true",
        help="Normalise ALL kicked-off 1X2 bets (any status) onto the fair "
        "de-vigged close, OVERWRITING existing closing_odds — converts legacy "
        "raw-quote closes. Use with --dry-run first; back up the DB.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("ERROR: ledger DB not found at %s" % args.db, file=sys.stderr)
        return 1

    if args.rebackfill:
        return _run_rebackfill(args)

    skipped: list = []
    try:
        records = closecapture.capture_closes_db(
            args.db, now_utc=args.now, dry_run=args.dry_run, skipped_out=skipped
        )
    except sqlite3.Error as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    verb = "would stamp" if args.dry_run else "stamped"
    if not records:
        print("no open 1X2 bets ready for close capture (%s 0)" % verb)
    for rec in records:
        print(
            "%s bet %d: %s — %s @ %.3f | close %.3f (%d books @ %s) | "
            "CLV %+.2f%%"
            % (
                verb,
                rec["bet_id"],
                rec["match"],
                rec["selection"],
                rec["decimal_odds"],
                rec["closing_odds"],
                rec["books"],
                rec["close_ts"],
                rec["clv"] * 100.0,
            )
        )

    # Surface real coverage gaps (unsplittable desc, ambiguous rematch, no
    # snapshot for the fixture) so a missed bet doesn't rot unnoticed.
    actionable = [s for s in skipped if s["reason"] in closecapture.ACTIONABLE_SKIPS]
    for rec in actionable:
        print(
            "  skipped bet %s: %s — %s (%s)"
            % (rec["bet_id"], rec["match"], rec["selection"], rec["reason"]),
            file=sys.stderr,
        )
    return 0


def _run_rebackfill(args) -> int:
    """Normalise every kicked-off 1X2 bet's close onto the fair basis."""
    skipped: list = []
    try:
        records = closecapture.rebackfill_fair_closes_db(
            args.db, now_utc=args.now, dry_run=args.dry_run, skipped_out=skipped
        )
    except sqlite3.Error as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    verb = "would set" if args.dry_run else "set"
    changed = [r for r in records if r["changed"]]
    unchanged = [r for r in records if not r["changed"]]
    if not records:
        print("rebackfill: no kicked-off 1X2 bets with a computable close")
    for rec in changed:
        old = "none" if rec["old_closing"] is None else "%.3f" % rec["old_closing"]
        old_clv = "—" if rec["old_clv"] is None else "%+.2f%%" % (rec["old_clv"] * 100)
        print(
            "%s bet %d [%s]: %s — %s @ %.3f | close %s -> %.3f | "
            "CLV %s -> %+.2f%% (%d books @ %s)"
            % (
                verb, rec["bet_id"], rec["status"], rec["match"], rec["selection"],
                rec["decimal_odds"], old, rec["new_closing"],
                old_clv, rec["new_clv"] * 100.0, rec["books"], rec["close_ts"],
            )
        )
    print(
        "rebackfill summary: %d changed, %d already-fair, %d total"
        % (len(changed), len(unchanged), len(records))
    )
    actionable = [s for s in skipped if s["reason"] in closecapture.ACTIONABLE_SKIPS]
    for rec in actionable:
        print(
            "  skipped bet %s: %s — %s (%s)"
            % (rec["bet_id"], rec["match"], rec["selection"], rec["reason"]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
