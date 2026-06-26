"""Hourly venue-position reconciliation daemon (SHADOW by default).

Pulls OPEN POSITIONS from Betfair Exchange, Smarkets and Polymarket (read-only),
reconciles them against the SQLite ledger, and — in SHADOW (default) — only LOGS
the proposed changes + refreshes the read-only site positions projection. LIVE
ledger writes are gated behind ``--live`` / ``WCA_POSITIONS_LIVE=1``.

Hourly cadence is set by launchd (com.wca.positions, 3600s). Run once manually:

    python scripts/wca_positions_sync.py --once --json /tmp/positions.json
    # promote to LIVE writes (conservative insert/close, never settles P&L):
    WCA_POSITIONS_LIVE=1 python scripts/wca_positions_sync.py --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))


def _load_dotenv(path: str) -> None:
    p = Path(ROOT) / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hourly venue-position reconciliation")
    ap.add_argument("--db", default=os.environ.get("WCA_DB_PATH", "data/wca.db"),
                    help="ledger DB path (default: $WCA_DB_PATH or data/wca.db)")
    ap.add_argument("--env", default=".env", help="dotenv file to load (default .env)")
    ap.add_argument("--live", action="store_true",
                    help="apply conservative ledger writes (also via WCA_POSITIONS_LIVE=1)")
    ap.add_argument("--once", action="store_true", help="run a single pass (default)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the reconciliation report to this path")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)
    if args.live:
        os.environ["WCA_POSITIONS_LIVE"] = "1"

    from wca import positions_sync

    live = positions_sync.live_env()
    report = positions_sync.run_sync(args.db, live=live)

    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
