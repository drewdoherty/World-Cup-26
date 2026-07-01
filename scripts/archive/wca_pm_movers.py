#!/usr/bin/env python
"""Top Polymarket share-price movers by category + the three insightful charts.

Reads the captured PM price-history trajectory (the versioned JSONL dataset
written by ``scripts/wca_pm_snapshot.py``, and/or the ``pm_snapshots`` table),
buckets markets into prop / tournament-futures / advancement-knockout, ranks the
biggest cent moves over several look-back windows, and renders one chart per
category (the set the ``/movers`` bot command sends).

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_movers.py            # text digest
    PYTHONPATH=src python3 scripts/wca_pm_movers.py --charts reports/movers
    PYTHONPATH=src python3 scripts/wca_pm_movers.py --db data/wca.db --top 8
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmhistory, pmmovers  # noqa: E402

_DEF_JSONL = os.path.join(_ROOT, "data", "pm_price_history.jsonl")


def _records_from_db(db_path: str):
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT ts_utc, kind, team, stage, market_slug, token_id, pm_mid, model_prob"
            " FROM pm_snapshots"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=_DEF_JSONL, help="JSONL price-history dataset")
    ap.add_argument("--db", default=None, help="also pull from a pm_snapshots DB")
    ap.add_argument("--top", type=int, default=8, help="movers per category chart")
    ap.add_argument("--charts", default=None,
                    help="render the 3 charts to this directory prefix (PNG)")
    args = ap.parse_args(argv)

    records = list(pmhistory.load_records(args.jsonl))
    if args.db and os.path.exists(args.db):
        records += _records_from_db(args.db)
    if not records:
        print("No PM snapshots found in %s%s." % (args.jsonl, (" / " + args.db) if args.db else ""))
        return 1

    recs = pmmovers.clean_records(records)
    windows = pmmovers.default_windows(recs)
    now = pmmovers.anchor_time(recs)
    print("Loaded %d valid snapshots across %d markets; anchor=%s; windows=%s"
          % (len(recs), len({pmmovers._market_key(r) for r in recs}),
             now.strftime("%Y-%m-%d %H:%M UTC") if now else "n/a",
             ", ".join(l for l, _ in windows)))
    print()
    print(pmmovers.text_summary(recs, windows=windows, top_n=args.top))

    if args.charts:
        out_dir = os.path.dirname(args.charts) or "."
        os.makedirs(out_dir, exist_ok=True)
        charts = pmmovers.build_charts(recs, windows=windows, top_n=args.top)
        print("\nCharts:")
        for c in charts:
            if c["png"] is None:
                print("  (matplotlib unavailable — %s chart skipped)" % c["category"])
                continue
            path = "%s_%s.png" % (args.charts, c["category"])
            with open(path, "wb") as fh:
                fh.write(c["png"])
            print("  %-12s -> %s  (%d markets)" % (c["category"], path, c["n_markets"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
