#!/usr/bin/env python
"""Append a Polymarket price snapshot to the historical dataset.

Outright / advancement / knockout markets have no fixed close, so the only
*leading* edge signal is whether the PM price drifts toward the model over time —
which needs a captured price trajectory. This snapshotter builds that trajectory.

It reuses the model-vs-PM pairing the advancement feed already computes every
build (``site/advancement_data.json``: per team x stage, ``model`` prob and PM
``pm`` mid), so it costs NO extra Polymarket calls and runs wherever that feed
is fresh (CI cloud build or the mini). Each run appends one timestamped record
per market to ``data/pm_price_history.jsonl`` (a versioned dataset) and, when a
DB is given, to the ``pm_snapshots`` table.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_snapshot.py \
        [--adv site/advancement_data.json] \
        [--jsonl data/pm_price_history.jsonl] \
        [--db data/wca.db] [--ts 2026-06-28T06:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmhistory  # noqa: E402

_DEF_ADV = os.path.join(_ROOT, "site", "advancement_data.json")
_DEF_JSONL = os.path.join(_ROOT, "data", "pm_price_history.jsonl")
_ADV_STAGES = ("R32", "R16", "QF", "SF", "Final", "win", "group_winner")


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_from_advancement(adv: Dict[str, object]) -> List[Dict[str, object]]:
    """Snapshot rows from an advancement feed: one per (team, stage) priced market."""
    rows: List[Dict[str, object]] = []
    for t in adv.get("teams", []):
        team = t.get("team")
        model = t.get("model", {}) or {}
        pm = t.get("pm", {}) or {}
        for stage in _ADV_STAGES:
            cell = pm.get(stage)
            if not isinstance(cell, dict) or cell.get("pm") is None:
                continue
            rows.append({
                "kind": "advancement", "team": team, "stage": stage,
                "market_slug": "%s:%s" % (team, stage),
                "token_id": cell.get("token_id"),
                "pm_mid": cell.get("pm"),
                "model_prob": (model.get(stage) if model.get(stage) is not None else None),
            })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adv", default=_DEF_ADV, help="advancement feed (model+pm per team/stage)")
    ap.add_argument("--jsonl", default=_DEF_JSONL, help="append-only JSONL history dataset")
    ap.add_argument("--db", default=None, help="optional sqlite DB for the pm_snapshots table")
    ap.add_argument("--ts", default=None, help="capture timestamp (default: advancement model_generated, else now)")
    args = ap.parse_args(argv)

    with open(args.adv, "r", encoding="utf-8") as fh:
        adv = json.load(fh)
    rows = rows_from_advancement(adv)
    # Prefer the model's own generation time so re-running on the same feed is idempotent-ish.
    meta = adv.get("meta", {}) or {}
    ts = args.ts or meta.get("model_generated") or meta.get("generated") or _now_iso_z()

    n_jsonl = pmhistory.append_jsonl(args.jsonl, rows, ts)
    n_db = 0
    if args.db:
        con = sqlite3.connect(args.db)
        try:
            n_db = pmhistory.append_snapshots(con, rows, ts)
        finally:
            con.close()
    print("pm snapshot @ %s: %d markets -> jsonl(+%d) db(+%d) [%s]"
          % (ts, len(rows), n_jsonl, n_db, args.jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
