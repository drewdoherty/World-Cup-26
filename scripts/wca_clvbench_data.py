#!/usr/bin/env python
"""Generate the full-book CLV benchmark feed.

Joins, offline:

* ``data/model_predictions_log.jsonl`` — the model 1X2 triple (and the market
  triple it saw) per fixture per timestamped build;
* ``data/wca.db`` ``odds_snapshots`` (READ-ONLY) — the de-vigged consensus
  *close* per fixture, via :func:`wca.closecapture.consensus_close`;
* ``data/wca.db`` ``bets`` (READ-ONLY) — which legs were actually placed, to
  split the full book into placed vs passed.

and scores EVERY model leg (home/draw/away of every build) fair-vs-fair:
``clv_odds = p_close / p_model - 1``.  All maths live in the deterministic
:mod:`wca.clvbench`; this CLI only gathers inputs (filesystem, SQLite) and the
``generated`` timestamp, then writes the feed atomically.

The ledger DB is opened strictly read-only (``mode=ro&immutable=1``): this
script never writes to it.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_clvbench_data.py \
        [--db data/wca.db] \
        [--log data/model_predictions_log.jsonl] \
        [--out site-analytics/data/tracking_clv_benchmark.json] \
        [--generated 2026-06-25T00:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import clvbench  # noqa: E402

_DEF_DB = os.path.join(_ROOT, "data", "wca.db")
_DEF_LOG = os.path.join(_ROOT, "data", "model_predictions_log.jsonl")
_DEF_OUT = os.path.join(_ROOT, "site-analytics", "data", "tracking_clv_benchmark.json")


def _now_iso_z() -> str:
    """UTC ISO-8601 with a trailing ``Z`` (the feed ``generated`` convention)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open *db_path* strictly read-only (immutable) — never writes."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    """Write *payload* as JSON to *path* atomically (tmp + os.replace)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".clvbench_", suffix=".tmp")
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


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=_DEF_DB, help="ledger DB (read-only)")
    parser.add_argument("--log", default=_DEF_LOG, help="model predictions jsonl")
    parser.add_argument("--out", default=_DEF_OUT, help="output feed path")
    parser.add_argument(
        "--generated", default=None,
        help="ISO-8601 Z timestamp for meta.generated (default: now)",
    )
    args = parser.parse_args(argv)

    generated = args.generated or _now_iso_z()
    builds = clvbench.load_builds(args.log)

    con = _connect_ro(args.db)
    try:
        payload = clvbench.build_benchmark(builds, con, generated)
    finally:
        con.close()

    _write_atomic(args.out, payload)

    meta = payload["meta"]
    head = payload["headline"]
    br = head["beat_rate"]
    print(
        f"wrote {args.out}: n_legs={meta['n_legs']} "
        f"n_with_close={meta['n_with_close']} "
        f"beat_rate={br['p']} (n={br['n']}, "
        f"95% [{br['lo']},{br['hi']}]) "
        f"placebo_null={head['placebo_null']} "
        f"clv_median={head['clv_median']} "
        f"clv_trimmed_mean={head['clv_trimmed_mean']} "
        f"coverage={head['coverage_pct']}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
