#!/usr/bin/env python
"""Daily player-data refresh (the ``playersdb`` launchd job).

1. Idempotent StatsBomb backfill of per-match rows into
   ``data/player_events.db`` (no network once the open-data cache is warm;
   prior WCs only — 2026 rows arrive via the analyst CSV ingest).
2. Rebuild the aggregate ``data/players.db`` (squads + per-player aggregates
   + team rates) — atomic replace, per-match history unaffected (separate
   file, see wca.data.player_events docstring).

    PYTHONPATH=src python scripts/wca_players_refresh.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import player_events as pe  # noqa: E402
from wca.data.players_db import build_players_db  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    con = pe.connect(pe.DEFAULT_DB_PATH)
    try:
        n = pe.backfill_statsbomb(con, now)
        total = con.execute("SELECT COUNT(*) FROM player_matches").fetchone()[0]
        print("player_events: %d rows upserted (store total %d)" % (n, total))
    finally:
        con.close()

    summary = build_players_db(generated_utc=now)
    print("players.db rebuilt: %r" % (summary,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
