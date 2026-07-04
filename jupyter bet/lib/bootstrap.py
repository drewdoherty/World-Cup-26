"""Path/config/manifest bootstrap — run (import) this first in every notebook.

Responsibilities:
* locate the repo root robustly (works from notebooks/, tests/, or the CLI);
* put ``<repo>/src`` and ``<repo>/scripts`` on ``sys.path`` so production
  modules import directly;
* load the repo ``.env`` the same way production scripts do (names only are
  ever displayed — values never leave the environment);
* expose canonical directories for the raw/bronze/silver/gold layers;
* produce a run manifest (package versions, git commit, UTC timestamp) that
  notebooks print and store next to their outputs.

No network access happens here.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

# --------------------------------------------------------------------------
# Root discovery: this file lives at <repo>/jupyter bet/lib/bootstrap.py
# --------------------------------------------------------------------------
JB_ROOT = Path(__file__).resolve().parent.parent          # .../jupyter bet
REPO_ROOT = JB_ROOT.parent                                 # repo root

for _p in (str(REPO_ROOT / "src"), str(REPO_ROOT / "scripts"), str(JB_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA_DIR = JB_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
BRONZE_DIR = DATA_DIR / "bronze"
SILVER_DIR = DATA_DIR / "silver"
GOLD_DIR = DATA_DIR / "gold"
OUT_DIR = JB_ROOT / "outputs"
CHART_DIR = OUT_DIR / "charts"
TABLE_DIR = OUT_DIR / "tables"
RUNLOG_DIR = OUT_DIR / "run_logs"

for _d in (RAW_DIR, BRONZE_DIR, SILVER_DIR, GOLD_DIR, CHART_DIR, TABLE_DIR,
           RUNLOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Display timezone (user jurisdiction). Internal timestamps stay UTC.
DISPLAY_TZ = "Asia/Bahrain"

# --------------------------------------------------------------------------
# .env loading — same tiny-loader pattern production scripts use (no dep).
# --------------------------------------------------------------------------

def load_dotenv(path: Optional[Path] = None) -> int:
    """Load ``<repo>/.env`` into os.environ (setdefault — never overrides an
    already-exported value). Returns the number of NEW names set. Values are
    never returned or printed."""
    p = path or (REPO_ROOT / ".env")
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if os.environ.setdefault(key.strip(), value.strip()) == value.strip():
            n += 1
    return n


def secret_names_present() -> Dict[str, bool]:
    """Which credential NAMES are configured (booleans only, never values)."""
    names = ["ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "POLYMARKET_PRIVATE_KEY"]
    return {n: bool(os.environ.get(n)) for n in names}


# --------------------------------------------------------------------------
# Run manifest
# --------------------------------------------------------------------------

def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_manifest(notebook: str) -> Dict[str, object]:
    """Versions/commit/timestamp block every notebook prints and saves."""
    import pandas as pd  # noqa: PLC0415
    import polars as plr  # noqa: PLC0415
    import pyarrow  # noqa: PLC0415
    import matplotlib  # noqa: PLC0415

    m = {
        "notebook": notebook,
        "run_utc": utcnow_iso(),
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "polars": plr.__version__,
        "pyarrow": pyarrow.__version__,
        "matplotlib": matplotlib.__version__,
        "secrets_present": secret_names_present(),
    }
    (RUNLOG_DIR / f"{notebook}.manifest.json").write_text(
        json.dumps(m, indent=2))
    return m


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Convenience: repo-level real data sources the notebooks read (read-only).
ORDERFLOW_DB = REPO_ROOT / "data" / "pm_orderflow.db"
DEV_WCA_DB = REPO_ROOT / "data" / "wca.db"           # STALE ledger copy (dev box)
MODEL_PRED_LOG = REPO_ROOT / "data" / "model_predictions_log.jsonl"
RESULTS_JSON = REPO_ROOT / "data" / "processed" / "wc2026_results.json"
ADVANCEMENT_JSON = REPO_ROOT / "site" / "advancement_data.json"
BET_RECS_JSON = REPO_ROOT / "site" / "bet_recs.json"
PROMOS_JSON = REPO_ROOT / "site" / "promos_data.json"
SCORES_MARKETS_JSON = REPO_ROOT / "site" / "scores_markets.json"

load_dotenv()
