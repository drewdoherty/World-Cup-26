#!/usr/bin/env python
"""Generate the Model-vs-Venue benchmark feed for the localhost:8001 dashboard.

Joins, offline and read-only:

* ``data/dev.db`` ``predictions`` — the model 1X2 triple + Elo/DC/market
  components per fixture per timestamped build (the model history / ledger);
* ``data/wca.db`` ``odds_snapshots`` (READ-ONLY) — per-bookmaker venue quotes,
  matched at-or-before each build time within a freshness limit, de-vigged (Shin);
* ``data/wca.db`` ``bets`` (READ-ONLY) — ``source='model'`` bets linked to the
  exact preceding build/leg (Arm B).

All statistics live in the deterministic :mod:`wca.venuesbench` /
:mod:`wca.venuesdata`; this CLI only gathers inputs and the ``generated``
timestamp, appends a compact history row, and writes the feed atomically.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_venues_benchmark.py \
        [--pred-db data/dev.db] [--odds-db data/wca.db] \
        [--out site-analytics/data/venues_benchmark.json] \
        [--freshness-hours 6] [--generated 2026-06-27T00:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import venuesdata as vd  # noqa: E402

_DEF_PRED_DB = os.path.join(_ROOT, "data", "dev.db")
_DEF_ODDS_DB = os.path.join(_ROOT, "data", "wca.db")
_DEF_OUT = os.path.join(_ROOT, "site-analytics", "data", "venues_benchmark.json")
_HISTORY_CAP = 60


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open *db_path* strictly read-only (immutable) — never writes."""
    uri = "file:%s?mode=ro&immutable=1" % os.path.abspath(db_path)
    return sqlite3.connect(uri, uri=True)


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".venuesbench_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prior_history(out_path: str) -> List[Dict[str, Any]]:
    """Read the history list from a prior feed (append-only versioned history)."""
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            prev = json.load(fh)
        hist = prev.get("history") or []
        return [h for h in hist if isinstance(h, dict)]
    except (OSError, ValueError):
        return []


def _odds_window(con: sqlite3.Connection) -> str:
    row = con.execute(
        "SELECT min(ts_utc), max(ts_utc) FROM odds_snapshots WHERE market='h2h'"
    ).fetchone()
    if not row or not row[0]:
        return "no odds_snapshots"
    return "%s .. %s" % (str(row[0])[:10], str(row[1])[:10])


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pred-db", default=_DEF_PRED_DB, help="predictions ledger (read-only)")
    parser.add_argument("--odds-db", default=_DEF_ODDS_DB, help="odds_snapshots + bets DB (read-only)")
    parser.add_argument("--out", default=_DEF_OUT, help="output feed path")
    parser.add_argument("--freshness-hours", type=float, default=6.0,
                        help="max age of a venue quote relative to the build time")
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--generated", default=None,
                        help="ISO-8601 Z timestamp for meta.generated (default: now)")
    parser.add_argument("--model-variant", default="blend(elo0.30/dc0.70 + market) — ex-market for ranking")
    args = parser.parse_args(argv)

    generated = args.generated or _now_iso_z()
    freshness_s = args.freshness_hours * 3600.0

    con_pred = _connect_ro(args.pred_db)
    con_odds = _connect_ro(args.odds_db)
    try:
        records = vd.load_model_records(con_pred)
        arm_a = vd.build_arm_a(records, con_odds, freshness_s=freshness_s)
        placed = vd.link_model_bets(con_pred, con_odds)  # bets table lives in odds-db (wca.db)
        window = _odds_window(con_odds)
    finally:
        con_pred.close()
        con_odds.close()

    history = _prior_history(args.out)
    lb = None
    feed = vd.assemble_feed(
        arm_a, placed, generated=generated, window=window,
        model_variant=args.model_variant, freshness_s=freshness_s,
        n_boot=args.n_boot, history=history,
    )
    lb = feed["leaderboard"]
    # Append this run's compact summary to the versioned history (then re-embed).
    summary = {
        "generated": generated,
        "n_obs": feed["coverage"]["n_obs"],
        "n_fixtures": feed["coverage"]["n_fixtures"],
        "n_venues": feed["coverage"]["n_venues"],
        "closest": (lb["venues"][0]["venue"] if lb.get("venues") else None),
        "verdict": lb.get("verdict"),
    }
    feed["history"] = (history + [summary])[-_HISTORY_CAP:]

    _write_atomic(args.out, feed)

    cov = feed["coverage"]
    print(
        "wrote %s: n_obs=%d n_fixtures=%d n_venues=%d | %s | placed linked=%d/%d (%s)"
        % (args.out, cov["n_obs"], cov["n_fixtures"], cov["n_venues"],
           lb.get("verdict"), placed["n_linked"], placed["n_model_bets"],
           "insufficient" if placed["insufficient"] else "ok")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
