#!/usr/bin/env python3
"""Build ``site-analytics/data/winrate.json`` (Module B feed).

Run::

    cd "/Users/andrewdoherty/Desktop/Coding/World Cup Alpha"
    PYTHONPATH=src python3 scripts/wca_winrate_data.py

Reads (read-only):
  * ``data/dev.db``                          — MODEL book (predledger predictions)
  * ``data/wca.db``  (immutable URI)         — REALIZED book (settled bets)
  * ``data/model_predictions_log.jsonl``     — market triple + MODEL fallback
  * ``data/processed/wc2026_results.json``   — realised outcomes (fallback join)

Writes (atomically):
  * ``site-analytics/data/winrate.json``

The ``generated`` timestamp comes from the environment variable
``WCA_GENERATED`` when set (so callers / CI can pin it);  otherwise the builder
shells out to ``date -u`` once — never imported into library code, keeping the
library deterministic and offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wca import winrate  # noqa: E402

DEV_DB = REPO / "data" / "dev.db"
WCA_DB = REPO / "data" / "wca.db"
JSONL = REPO / "data" / "model_predictions_log.jsonl"
RESULTS = REPO / "data" / "processed" / "wc2026_results.json"
OUT = REPO / "site-analytics" / "data" / "winrate.json"


def _wca_ro_uri() -> str:
    return f"file:{WCA_DB}?mode=ro&immutable=1"


def _generated() -> str:
    env = os.environ.get("WCA_GENERATED")
    if env:
        return env
    return subprocess.check_output(
        ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], text=True
    ).strip()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
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


def main() -> int:
    feed = winrate.build_feed(
        dev_db_path=str(DEV_DB),
        wca_db_ro_uri=_wca_ro_uri(),
        jsonl_path=str(JSONL),
        results_path=str(RESULTS),
        generated=_generated(),
    )
    _atomic_write_json(OUT, feed)

    meta = feed["meta"]
    hl = feed["headline"]
    mw = hl["model_win_rate"]
    rw = hl["realized_win_rate"]
    print(f"wrote {OUT}")
    print(f"  n_model={meta['n_model']} n_realized={meta['n_realized']} "
          f"low_n={feed['low_n']}")

    def fmt(b):
        if b["p"] is None:
            return "n/a"
        return f"{b['p']:.3f} [{b['lo']:.3f},{b['hi']:.3f}] (n={b['n']})"

    print(f"  model_win_rate    = {fmt(mw)}")
    print(f"  realized_win_rate = {fmt(rw)}")
    print(f"  model_brier={hl['model_brier']} market_brier={hl['market_brier']} "
          f"bss={hl['bss']}")
    print(f"  acca_strike p={hl['acca_strike']['p']} n={hl['acca_strike']['n']}  "
          f"coverage={hl['coverage']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
