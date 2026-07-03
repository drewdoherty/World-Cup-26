#!/usr/bin/env python
"""Ingest per-player, per-match event rows into ``data/player_events.db``.

Two modes, both idempotent (re-running never duplicates — same
(player, team, match_id) replaces):

Backfill prior World Cups from the cached StatsBomb open-data (the source
that has always fed props_players.csv, now persisted per-match)::

    PYTHONPATH=src python scripts/wca_player_events_ingest.py --from-statsbomb

Ingest current-tournament rows from an analyst CSV drop (the honest 2026
path — no public per-player event feed exists for this WC)::

    PYTHONPATH=src python scripts/wca_player_events_ingest.py --csv rows.csv

CSV column contract (header required; extra columns ignored)::

    player,team,match_id,date,minutes,shots,sot,goals,assists,yellows,reds,corners_taken

``date`` = YYYY-MM-DD kickoff date; ``match_id`` = any stable id (the
convention used elsewhere is fine: "<home> vs <away> <date>"). Missing
numeric fields default to 0; missing minutes makes the row unusable for
rates (stored, flagged by NULL minutes).
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import player_events as pe  # noqa: E402


def _rows_from_csv(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            if not rec.get("player") or not rec.get("team") or not rec.get("match_id"):
                print("skipping row missing player/team/match_id: %r" % rec,
                      file=sys.stderr)
                continue
            minutes = rec.get("minutes")
            yield pe.PlayerMatchRow(
                player=rec["player"].strip(),
                team=rec["team"].strip(),
                match_id=str(rec["match_id"]).strip(),
                date=(rec.get("date") or "").strip() or None,
                competition=(rec.get("competition") or "WC2026").strip(),
                minutes=float(minutes) if minutes not in (None, "",) else None,
                shots=int(rec.get("shots") or 0),
                sot=int(rec.get("sot") or 0),
                goals=int(rec.get("goals") or 0),
                assists=int(rec.get("assists") or 0),
                yellows=int(rec.get("yellows") or 0),
                reds=int(rec.get("reds") or 0),
                corners_taken=int(rec.get("corners_taken") or 0),
                source=pe.SOURCE_ANALYST,
            )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=pe.DEFAULT_DB_PATH)
    ap.add_argument("--from-statsbomb", action="store_true",
                    help="backfill WC2018+2022 per-match rows (cached fetch)")
    ap.add_argument("--csv", default=None,
                    help="ingest analyst CSV rows (see module docstring)")
    args = ap.parse_args(argv)

    if not args.from_statsbomb and not args.csv:
        ap.error("nothing to do: pass --from-statsbomb and/or --csv PATH")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = pe.connect(args.db)
    try:
        if args.from_statsbomb:
            n = pe.backfill_statsbomb(con, now)
            print("statsbomb backfill: %d player-match rows upserted" % n)
        if args.csv:
            n = pe.upsert_rows(con, _rows_from_csv(args.csv), now)
            print("csv ingest: %d rows upserted from %s" % (n, args.csv))
        total = con.execute("SELECT COUNT(*) FROM player_matches").fetchone()[0]
        by_src = con.execute(
            "SELECT source, COUNT(*) FROM player_matches GROUP BY source"
        ).fetchall()
        print("store now: %d rows total | %s"
              % (total, ", ".join("%s=%d" % s for s in by_src)))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
