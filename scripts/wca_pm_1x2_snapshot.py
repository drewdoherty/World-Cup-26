#!/usr/bin/env python
"""Capture a Polymarket 1X2 (match-winner) snapshot into ``odds_snapshots`` so
Polymarket becomes a ranked venue in the Model-vs-Venue benchmark.

Live fetch via :func:`wca.data.polymarket_odds.get_odds`; resolution + insert via
:mod:`wca.pm1x2snapshot` (network-free, unit-tested). Run on a schedule (e.g.
hourly on the mini) to accrue the matched-time H/D/A series the benchmark needs
to move Polymarket off ``COLLECTING``.

    PYTHONPATH=src python3 scripts/wca_pm_1x2_snapshot.py [--db data/wca.db] [--dry-run]

Degrades gracefully: if Polymarket is unreachable the fetch returns an empty
frame and nothing is written (never raises).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pm1x2snapshot as pms  # noqa: E402
from wca.data import polymarket_odds  # noqa: E402


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "wca.db"))
    ap.add_argument("--ts", default=None, help="capture timestamp (default: now)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + resolve but do not write to the DB")
    args = ap.parse_args(argv)
    ts = args.ts or _now_iso_z()

    frame, _ = polymarket_odds.get_odds(markets="h2h")
    pm_rows = [] if frame is None or frame.empty else frame.to_dict("records")
    if not pm_rows:
        print("no Polymarket h2h rows fetched (unreachable or no live markets) — nothing written")
        return 0

    con = sqlite3.connect(args.db)
    try:
        if args.dry_run:
            index = pms.build_match_index(con)
            insert_rows, unmatched = pms.pm_rows_to_snapshot_rows(pm_rows, index, ts)
            print("DRY RUN: would insert %d rows | %d unmatched legs | %d fixtures indexed"
                  % (len(insert_rows), len(unmatched), len(index)))
            return 0
        summary = pms.snapshot(con, pm_rows, ts)
    finally:
        con.close()

    print("PM 1X2 snapshot @ %s: inserted %d rows | unmatched legs %d | indexed %d fixtures"
          % (ts, summary["inserted"], summary["n_unmatched_legs"], summary["n_fixtures_indexed"]))
    if summary["unmatched_fixtures"]:
        print("  unmatched (no book/model coverage): " + ", ".join(summary["unmatched_fixtures"][:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
