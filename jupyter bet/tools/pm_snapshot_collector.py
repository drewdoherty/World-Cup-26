#!/usr/bin/env python3
"""Continuous PM quote/book snapshot collector (run when you want a dense
pre-match record of spread/depth that the trade tape can't give you).

    ../.venv/bin/python tools/pm_snapshot_collector.py --minutes 120 --every 60

Snapshots top-of-book for every OPEN PM match-market token into the silver
dataset ``pm_quote_snapshots`` (appending, idempotent per timestamp) and the
raw layer. Needs the PM network route (VPN on this MacBook). Ctrl-C safe.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

JB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JB))

import polars as pl  # noqa: E402

import lib.bootstrap as bt  # noqa: E402
import lib.pmdata as pm  # noqa: E402
import lib.storage as st  # noqa: E402


def snapshot_once() -> pl.DataFrame:
    mk = pm.orderflow_markets().filter(pl.col("closed") == 0)
    rows = []
    for r in mk.to_dicts():
        import json
        tokens = json.loads(r["token_ids"]) if r["token_ids"] else []
        outcomes = json.loads(r["outcomes"]) if r["outcomes"] else []
        for i, tok in enumerate(tokens):
            try:
                book = pm.clob_book(tok, capture=False)
            except pm.PMUnavailable:
                return pl.DataFrame()
            if not book:
                continue
            m = pm.book_metrics(book)
            rows.append({"snapshot_utc": bt.utcnow_iso(),
                         "condition_id": r["condition_id"],
                         "question": r["question"],
                         "outcome": outcomes[i] if i < len(outcomes) else str(i),
                         "token_id": tok, **m})
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--every", type=int, default=60, help="seconds between snaps")
    a = ap.parse_args()
    t_end = time.time() + a.minutes * 60
    name = "pm_quote_snapshots"
    while time.time() < t_end:
        snap = snapshot_once()
        if snap.height:
            path = st.dataset_path("silver", name)
            if path.exists():
                snap = pl.concat([pl.read_parquet(path), snap],
                                 how="vertical_relaxed").unique(
                    subset=["snapshot_utc", "token_id"])
            st.save_dataset(snap, "silver", name, notebook="collector",
                            note="live top-of-book time series")
            print(f"{bt.utcnow_iso()} captured {snap.height} total quote rows")
        else:
            print(f"{bt.utcnow_iso()} PM unreachable or no open markets")
        time.sleep(a.every)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
