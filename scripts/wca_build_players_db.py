#!/usr/bin/env python
"""Build ``data/players.db`` from the StatsBomb open-data WC cache + squads.

Reuses the local StatsBomb event cache (``data/raw/statsbomb``) — no network
when the cache is warm, so the build is resumable. Every numeric row traces to
a real fetched source; squad members without event history are flagged, never
fabricated. See :mod:`wca.data.players_db`.

Usage
-----
    .venv/bin/python scripts/wca_build_players_db.py [--db data/players.db]
        [--cache-dir data/raw/statsbomb] [--squads data/squads.json]
"""
import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wca.data import players_db  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "players.db"))
    ap.add_argument("--cache-dir", default=str(ROOT / "data" / "raw" / "statsbomb"))
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "processed"))
    ap.add_argument("--squads", default=str(ROOT / "data" / "squads.json"))
    ap.add_argument("--overrides", default=str(ROOT / "data" / "players.json"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = players_db.build_players_db(
        cache_dir=args.cache_dir,
        out_dir=args.out_dir,
        squads_path=args.squads,
        overrides_path=args.overrides,
        db_path=args.db,
        generated_utc=ts,
    )

    print("")
    print("=== players.db built (%s) ===" % ts)
    print("path: %s" % args.db)
    print("players (StatsBomb):      %d  (thin sample: %d)"
          % (summary["players"], summary["players_thin"]))
    print("teams (team_rates):       %d" % summary["teams"])
    print("squad members (2026):     %d  (with event history: %d)"
          % (summary["squad_members"], summary["squad_with_history"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
