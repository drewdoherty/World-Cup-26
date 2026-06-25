#!/usr/bin/env python3
"""Build ``site-analytics/data/risk_pnl.json`` — open-book P&L distribution.

Reads the OPEN book from the production ledger STRICTLY read-only
(``mode=ro&immutable=1``) and the model predictions log, runs ~20000
Monte-Carlo sims (numpy Generator seed=42), and writes the feed atomically.

Run:
    cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha" && \
        PYTHONPATH=src python3 scripts/wca_risk_pnl_data.py

This script (unlike the library) is permitted wall-clock for the ``generated``
and ``fx_ts`` fields.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone

# Allow `python3 scripts/wca_risk_pnl_data.py` without PYTHONPATH=src.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
import sys

if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.mc.pnl import (  # noqa: E402
    DEFAULT_FX_RATE,
    DEFAULT_N_SIMS,
    DEFAULT_SEED,
    build_risk_pnl,
    load_open_positions,
)

DEFAULT_DB = os.path.join(_ROOT, "data", "wca.db")
DEFAULT_MODEL_LOG = os.path.join(_ROOT, "data", "model_predictions_log.jsonl")
DEFAULT_OUT = os.path.join(_ROOT, "site-analytics", "data", "risk_pnl.json")


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=".risk_pnl.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help="ledger path (read-only)")
    ap.add_argument("--model-log", default=DEFAULT_MODEL_LOG)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--n-sims", type=int, default=DEFAULT_N_SIMS)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--fx-rate", type=float, default=DEFAULT_FX_RATE)
    ap.add_argument(
        "--generated", default=None, help="override ISO8601Z (default: now)"
    )
    args = ap.parse_args(argv)

    generated = args.generated or _now_z()
    fx_ts = generated  # placeholder FX captured at build time

    positions = load_open_positions(args.db, args.model_log, read_only=True)
    result = build_risk_pnl(
        positions,
        generated=generated,
        fx_ts=fx_ts,
        n_sims=args.n_sims,
        seed=args.seed,
        fx_rate=args.fx_rate,
    )
    _atomic_write(args.out, result.feed)

    d = result.feed["distribution_gbp"]
    print(f"wrote {args.out}")
    print(f"n_open_positions = {result.feed['meta']['n_open_positions']}")
    print(
        f"mean={d['mean']}  median={d['median']}  p5={d['p5']}  "
        f"p95={d['p95']}  VaR95={d['var95']}  CVaR95={d['cvar95']}  "
        f"P(down)={d['p_book_down']}  hard_floor={d['hard_floor']}"
    )
    for cur, row in result.feed["by_currency"].items():
        print(f"  {cur}: n={row['n']} open_stake={row['open_stake']} ev={row['ev']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
