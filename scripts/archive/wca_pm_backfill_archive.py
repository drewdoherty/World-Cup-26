#!/usr/bin/env python
"""Backfill the Polymarket price trajectory from the raw archive.

Systematic snapshotting only began 2026-06-28, but the additive archive tee
(``data/archive/raw/.../venue=polymarket/market=events``) captured the raw Gamma
``/events`` payloads from 2026-06-27 onward — densely for liquid markets. This
script replays those payloads into the same trajectory JSONL shape the live
snapshotter writes, so charts/analytics get a denser, *current* history without
new API calls.

Reliably dense in the archive: **match-winner (1X2)** markets (present in ~94% of
captures). **Champion** ("World Cup Winner") is present intermittently (~hourly).
Other tournament markets (stage-of-elimination, group winner) are captured when
their page repriced. Nothing exists before 2026-06-27 — that history was never
recorded and cannot be reconstructed.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_backfill_archive.py --out data/pm_price_history_archive.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import polymarket as P  # noqa: E402

_DEF_GLOB = os.path.join(_ROOT, "data", "archive", "raw", "date=*", "venue=polymarket",
                         "market=events", "*.parquet")


def _rows_from_events(evs, ts):
    """Trajectory rows from one archived /events payload at capture ``ts``."""
    rows = []
    for e in evs or []:
        title = (e.get("title") or "").strip()
        tl = title.lower()
        is_match = (" vs. " in title or " vs " in title) and " - " not in title
        is_champ = title == "World Cup Winner"
        is_group = ("group" in tl and "winner" in tl)
        is_elim = "stage of elimination" in tl
        if not (is_match or is_champ or is_group or is_elim):
            continue
        slug = e.get("slug") or title
        for m in e.get("markets") or []:
            git = (m.get("groupItemTitle") or "").strip()
            q = (m.get("question") or "")
            if is_match and (git.lower().startswith("draw") or "end in a draw" in q.lower()):
                continue  # per-team win lines only
            res = P._yes_token_and_price(m, e)
            if not res or not git:
                continue
            if is_match:
                kind, stage, team = "match", "win_match", git
            elif is_champ:
                kind, stage, team = "futures", "win", git
            elif is_group:
                kind, stage, team = "futures", "group_winner", git
            else:  # stage of elimination
                subj = title.split(":", 1)[1] if ":" in title else title
                subj = subj.lower().replace("stage of elimination", "").strip().title()
                kind, stage, team = "advancement", "elim:%s" % git, subj
            rows.append({
                "kind": kind, "team": team, "stage": stage,
                "market_slug": "%s::%s" % (slug, git),
                "token_id": res.get("token_id"), "pm_mid": res.get("price"),
                "model_prob": None,
            })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--glob", default=_DEF_GLOB, help="archive parquet glob")
    ap.add_argument("--out", default=os.path.join(_ROOT, "data", "pm_price_history_archive.jsonl"))
    ap.add_argument("--only", default=None, help="comma kinds to keep (match,futures,advancement)")
    args = ap.parse_args(argv)

    import pyarrow.parquet as pq

    keep = set(args.only.split(",")) if args.only else None
    files = sorted(glob.glob(args.glob))
    if not files:
        print("No archive parquet files matched %s" % args.glob)
        return 1

    seen = set()  # (ts, market_slug) dedup
    n_rows = 0
    kinds: Dict[str, int] = {}
    tss = set()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as out:
        for f in files:
            try:
                row = pq.ParquetFile(f).read().to_pylist()[0]
                evs = json.loads(row["payload_json"])
            except Exception:
                continue
            ts = row.get("ts_utc")
            if not isinstance(evs, list):
                continue
            for r in _rows_from_events(evs, ts):
                if keep and r["kind"] not in keep:
                    continue
                key = (ts, r["market_slug"])
                if key in seen:
                    continue
                seen.add(key)
                mid = r.get("pm_mid")
                if mid is None or not (0.0 <= float(mid) <= 1.0):
                    continue
                rec = dict(r)
                rec["ts_utc"] = ts
                out.write(json.dumps(rec, sort_keys=True) + "\n")
                n_rows += 1
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                tss.add(ts)
    print("Backfilled %d rows from %d files -> %s" % (n_rows, len(files), args.out))
    print("  kinds: %s" % kinds)
    print("  distinct capture timestamps: %d (%s -> %s)"
          % (len(tss), min(tss) if tss else "-", max(tss) if tss else "-"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
