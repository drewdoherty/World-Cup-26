"""Hourly venue-position reconciliation daemon (SHADOW by default).

Pulls OPEN + SETTLED-24h positions from Betfair Exchange, Smarkets and
Polymarket (read-only), reconciles them against the SQLite ledger, and — in
SHADOW (default) — only LOGS the proposed changes (inserts, settles, closes) +
refreshes the read-only site positions projection. LIVE ledger writes are gated
behind ``--live`` / ``WCA_POSITIONS_LIVE=1``.

THREE MODES
-----------
1. All-in-one (tests/dev) — fetch + apply against the local ledger:

     python scripts/wca_positions_sync.py --once --json /tmp/positions.json
     WCA_POSITIONS_LIVE=1 python scripts/wca_positions_sync.py --live

2. FETCH-ONLY (runs on the MacBook, Betfair VPN on) — no DB access, writes a
   self-describing snapshot:

     python scripts/wca_positions_sync.py --fetch-only --out /tmp/snapshot.json

3. APPLY-FROM-SNAPSHOT (runs on the mini, canonical ledger) — reconcile + apply:

     python scripts/wca_positions_sync.py --apply-from-snapshot /tmp/snapshot.json \
         --db data/wca.db [--live]

The 24h lookback is for SETTLED positions only (open positions are all-current
regardless); tune it with ``--settled-lookback-hours`` (default 24) so the first
run captures a full day of settles.
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
    ap = argparse.ArgumentParser(description="Venue-position reconciliation (open + settled-24h)")
    ap.add_argument("--db", default=os.environ.get("WCA_DB_PATH", "data/wca.db"),
                    help="ledger DB path (default: $WCA_DB_PATH or data/wca.db)")
    ap.add_argument("--env", default=".env", help="dotenv file to load (default .env)")
    ap.add_argument("--live", action="store_true",
                    help="apply conservative ledger writes (also via WCA_POSITIONS_LIVE=1)")
    ap.add_argument("--once", action="store_true", help="run a single pass (default)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the reconciliation report to this path")
    ap.add_argument("--settled-lookback-hours", type=int, default=24,
                    help="settled-position lookback window in hours (default 24)")
    # Cross-machine FETCH/APPLY split.
    ap.add_argument("--fetch-only", action="store_true",
                    help="FETCH every venue (open + settled-24h) and write a "
                         "snapshot; NO DB access. Runs on the MacBook (VPN on).")
    ap.add_argument("--out", default=None,
                    help="snapshot output path for --fetch-only")
    ap.add_argument("--apply-from-snapshot", dest="snapshot_in", default=None,
                    help="reconcile + apply a snapshot file against --db. "
                         "Runs on the mini (canonical ledger).")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)
    if args.live:
        os.environ["WCA_POSITIONS_LIVE"] = "1"

    from wca import positions_sync

    # --- FETCH-ONLY: no DB access, emit a snapshot. ---
    if args.fetch_only:
        snap = positions_sync.fetch_snapshot(
            settled_lookback_hours=args.settled_lookback_hours
        )
        text = json.dumps(snap, indent=2, default=str)
        if args.out:
            Path(args.out).write_text(text)
        else:
            print(text)
        return 0

    # --- APPLY-FROM-SNAPSHOT: reconcile + apply against the canonical ledger. ---
    if args.snapshot_in:
        snapshot = json.loads(Path(args.snapshot_in).read_text())
        live = positions_sync.live_env()
        report = positions_sync.apply_snapshot(snapshot, args.db, live=live)
        text = json.dumps(report, indent=2, default=str)
        if args.json_out:
            Path(args.json_out).write_text(text)
        print(text)
        return 0

    # --- All-in-one (fetch + apply locally). ---
    live = positions_sync.live_env()
    report = positions_sync.run_sync(
        args.db, live=live, settled_lookback_hours=args.settled_lookback_hours
    )

    text = json.dumps(report, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
