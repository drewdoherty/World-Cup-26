#!/usr/bin/env python
"""Build the outright/advancement edge feed for the analytics dashboard.

Reads the PM price-history dataset (``data/pm_price_history.jsonl``) and computes
the edge metrics chosen to replace CLV on markets that have no fixed close:

* **convergence** (leading) — does PM drift toward the model over the holding
  period? Computable as soon as >=2 snapshots exist per market.
* **calibration / paired_skill / information_coefficient** (lagging) — need
  resolved 0/1 outcomes; rendered as honest ``insufficient`` until knockouts
  resolve and an outcomes source is wired.

Deterministic and offline. Honest empty/COLLECTING states; never fabricates.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_outright_edge_data.py \
        [--jsonl data/pm_price_history.jsonl] \
        [--out site-analytics/data/outright_edge.json] [--generated <iso>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmhistory, outrightedge  # noqa: E402

_DEF_JSONL = os.path.join(_ROOT, "data", "pm_price_history.jsonl")
_DEF_OUT = os.path.join(_ROOT, "site-analytics", "data", "outright_edge.json")


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".oe_", suffix=".tmp")
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


def build_feed(records, *, generated):
    """Assemble the outright-edge payload from PM-history records (pure)."""
    n_records = len(records)
    n_markets = len({(r.get("kind"), r.get("team"), r.get("stage"), r.get("market_slug")) for r in records})
    timestamps = sorted({r.get("ts_utc") for r in records if r.get("ts_utc")})
    conv_rows = pmhistory.convergence_inputs_from_records(records, kind="advancement")
    convergence = outrightedge.convergence(conv_rows)
    convergence["state"] = "live" if convergence["sufficient"] else "COLLECTING"
    if not conv_rows:
        convergence["note"] = ("need >=2 snapshots per market; history currently spans %d capture(s) "
                               "across %d markets" % (len(timestamps), n_markets))

    insufficient = lambda why: {"state": "insufficient", "n": 0, "note": why}
    no_outcomes = ("needs resolved 0/1 outcomes (knockouts unplayed / outcomes source not wired); "
                   "accrues as the bracket resolves")
    return {
        "meta": {
            "generated": generated,
            "n_history_records": n_records,
            "n_markets": n_markets,
            "n_captures": len(timestamps),
            "first_capture": (timestamps[0] if timestamps else None),
            "last_capture": (timestamps[-1] if timestamps else None),
            "primary": "convergence (leading)",
            "note": ("CLV is undefined for outrights (no fixed close; single-tournament n_eff~1). "
                     "Convergence is the leading replacement; skill/calibration/IC are lagging confirmation."),
        },
        "convergence": convergence,
        "calibration": insufficient(no_outcomes),
        "paired_skill": insufficient(no_outcomes),
        "information_coefficient": insufficient(no_outcomes),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=_DEF_JSONL)
    ap.add_argument("--out", default=_DEF_OUT)
    ap.add_argument("--generated", default=None)
    args = ap.parse_args(argv)

    records = pmhistory.load_records(args.jsonl)
    feed = build_feed(records, generated=(args.generated or _now_iso_z()))
    _write_atomic(args.out, feed)
    c = feed["convergence"]
    print("wrote %s: %d records / %d markets / %d captures | convergence=%s (n_signal=%s, state=%s)"
          % (args.out, feed["meta"]["n_history_records"], feed["meta"]["n_markets"],
             feed["meta"]["n_captures"], c.get("convergence_rate"), c.get("n_signal"), c.get("state")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
