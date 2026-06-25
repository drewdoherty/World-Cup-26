#!/usr/bin/env python3
"""Build ``site-analytics/data/rigor.json`` — the repeatable-edge VERDICT feed.

Module D's runnable entrypoint.  Reads the production ledger strictly
read-only, joins the model-prediction log to realized results for the
outcome-anchored skill gates, optionally reads the dev prediction ledger, runs
the conservative gate battery, and writes the feed atomically.

Usage
-----
    cd "<repo root>" && PYTHONPATH=src python3 scripts/wca_rigor_data.py
    # options:
    #   --wca-db PATH      money ledger (default data/wca.db, opened RO)
    #   --jsonl PATH       model prediction log (default data/model_predictions_log.jsonl)
    #   --results PATH     realized results (default data/processed/wc2026_results.json)
    #   --dev-db PATH      prediction ledger (default data/dev.db if present)
    #   --out PATH         feed path (default site-analytics/data/rigor.json)
    #   --generated ISO    override the generated timestamp (else `date -u`)
    #   --print            also print the payload to stdout

The ``generated`` timestamp is the only non-deterministic input and is taken
from the wall clock here (never inside the library), so the battery itself
stays fully deterministic and offline-testable.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import tempfile
from pathlib import Path

# Make ``src`` importable when run as a plain script.
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wca.rigor.build import build_rigor  # noqa: E402

_DEFAULT_WCA_DB = str(_REPO / "data" / "wca.db")
_DEFAULT_JSONL = str(_REPO / "data" / "model_predictions_log.jsonl")
_DEFAULT_RESULTS = str(_REPO / "data" / "processed" / "wc2026_results.json")
_DEFAULT_DEV_DB = str(_REPO / "data" / "dev.db")
_DEFAULT_OUT = str(_REPO / "site-analytics" / "data" / "rigor.json")


def _utc_now_z() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: str, payload: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wca-db", default=_DEFAULT_WCA_DB)
    ap.add_argument("--jsonl", default=_DEFAULT_JSONL)
    ap.add_argument("--results", default=_DEFAULT_RESULTS)
    ap.add_argument("--dev-db", default=_DEFAULT_DEV_DB)
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--generated", default=None)
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args(argv)

    generated = args.generated or _utc_now_z()
    dev_db = args.dev_db if (args.dev_db and os.path.exists(args.dev_db)
                             and os.path.getsize(args.dev_db) > 0) else None

    payload = build_rigor(
        wca_db=args.wca_db,
        jsonl_path=args.jsonl,
        results_path=args.results,
        dev_db=dev_db,
        generated=generated,
    )
    _atomic_write_json(args.out, payload)

    v = payload["verdict"]
    meta = payload["meta"]
    print(
        "rigor.json written -> %s\n  verdict: %s (%s)  |  n=%d  n_eff=%.2f  stage=%s"
        % (args.out, v["level"], v["color"], meta["n"], meta["n_eff"], meta["stage"])
    )
    if args.do_print:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
