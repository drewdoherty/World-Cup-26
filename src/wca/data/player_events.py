"""Append-only per-player, per-match event store — ``data/player_events.db``.

The missing empirical layer under :mod:`wca.models.playerprops`: every prop
probability used to be a structural cascade off team lambda through two magic
constants (``SHOTS_PER_GOAL=10.0``, ``SOT_PER_SHOT=0.35``) because no
per-player sample existed anywhere. This module persists real per-match rows
so ``shrink_rate()`` finally has an empirical rate to shrink.

Why a SEPARATE file from ``players.db``
---------------------------------------
:func:`wca.data.players_db.build_players_db` rebuilds ``players.db``
atomically (writes a temp file, then ``os.replace``) — any table we added
there would be silently destroyed on every rebuild. The per-match history is
append-only and must survive rebuilds, so it lives in its own
``data/player_events.db``. Readers join both: per-match empirics first,
aggregate ``players.db`` second, structural derivation last.

Sources (provenance is a column, never implicit)
------------------------------------------------
* ``statsbomb_open_wc2018_2022`` — real per-match rows backfilled from the
  StatsBomb open-data events already cached for the props dataset. Only prior
  World Cups exist there (no 2026 feed).
* ``analyst_csv`` — current-tournament rows ingested from a CSV drop
  (``scripts/wca_player_events_ingest.py --csv``): the honest 2026 path until
  a live per-player event feed exists. Column contract is documented on the
  CLI.

``corners_taken`` note: StatsBomb attributes corner KICKS to the taker; a
"corner won" has no clean attribution in the data, so the store records
corners *taken* and says so — nothing is fabricated.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_DB_PATH = "data/player_events.db"

SOURCE_STATSBOMB = "statsbomb_open_wc2018_2022"
SOURCE_ANALYST = "analyst_csv"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS player_matches (
    player        TEXT NOT NULL,
    team          TEXT NOT NULL,
    match_id      TEXT NOT NULL,
    date          TEXT,
    competition   TEXT,
    minutes       REAL,
    shots         INTEGER NOT NULL DEFAULT 0,
    sot           INTEGER NOT NULL DEFAULT 0,
    goals         INTEGER NOT NULL DEFAULT 0,
    assists       INTEGER NOT NULL DEFAULT 0,
    yellows       INTEGER NOT NULL DEFAULT 0,
    reds          INTEGER NOT NULL DEFAULT 0,
    corners_taken INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,
    ingested_utc  TEXT NOT NULL,
    PRIMARY KEY (player, team, match_id)
);
CREATE INDEX IF NOT EXISTS idx_pm_team_player ON player_matches(team, player);
CREATE INDEX IF NOT EXISTS idx_pm_date ON player_matches(date);
"""


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating schema if needed). Append-only by convention: the only
    writer is :func:`upsert_rows`; nothing here deletes or rebuilds."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    return con


@dataclass
class PlayerMatchRow:
    player: str
    team: str
    match_id: str
    date: Optional[str] = None
    competition: Optional[str] = None
    minutes: Optional[float] = None
    shots: int = 0
    sot: int = 0
    goals: int = 0
    assists: int = 0
    yellows: int = 0
    reds: int = 0
    corners_taken: int = 0
    source: str = SOURCE_ANALYST


def upsert_rows(con: sqlite3.Connection, rows: Iterable[PlayerMatchRow],
                ingested_utc: str) -> int:
    """Idempotent insert: re-ingesting the same (player, team, match_id)
    REPLACES the row (a corrected stat line wins; history is per-match, so
    replace ≠ lost information). Returns the number of rows written."""
    n = 0
    for r in rows:
        con.execute(
            """INSERT OR REPLACE INTO player_matches
               (player, team, match_id, date, competition, minutes, shots,
                sot, goals, assists, yellows, reds, corners_taken, source,
                ingested_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.player, r.team, str(r.match_id), r.date, r.competition,
             None if r.minutes is None else float(r.minutes),
             int(r.shots), int(r.sot), int(r.goals), int(r.assists),
             int(r.yellows), int(r.reds), int(r.corners_taken),
             r.source, ingested_utc),
        )
        n += 1
    con.commit()
    return n


# ---------------------------------------------------------------------------
# StatsBomb backfill: per-match rows (prior WCs — the only event feed we have)
# ---------------------------------------------------------------------------

def statsbomb_match_rows(events: Sequence[dict], match_id, date: Optional[str],
                         competition: str) -> List[PlayerMatchRow]:
    """Per-player rows for ONE match, reusing the tested statsbomb parsers.

    ``player_shares`` fed a single match aggregates exactly one match — its
    minutes / shots / SoT / goals / second-yellow card logic is reused
    verbatim rather than re-implemented. Corners taken are extracted here
    (pass.type == Corner), which ``player_shares`` does not carry.
    """
    from wca.data import statsbomb

    df = statsbomb.player_shares({match_id: list(events)})

    corners: Dict[tuple, int] = {}
    for ev in events:
        if (ev.get("type") or {}).get("name") != "Pass":
            continue
        if ((ev.get("pass") or {}).get("type") or {}).get("name") != "Corner":
            continue
        player = (ev.get("player") or {}).get("name")
        team = (ev.get("team") or {}).get("name")
        if player:
            corners[(player, team)] = corners.get((player, team), 0) + 1

    rows: List[PlayerMatchRow] = []
    for _, r in df.iterrows():
        minutes = r.get("minutes")
        try:
            import math
            if minutes is not None and isinstance(minutes, float) and math.isnan(minutes):
                minutes = None
        except Exception:
            pass
        rows.append(PlayerMatchRow(
            player=str(r["player"]), team=str(r["team"]), match_id=str(match_id),
            date=date, competition=competition,
            minutes=minutes,
            shots=int(r.get("shots", 0)), sot=int(r.get("sot", 0)),
            goals=int(r.get("goals", 0)),
            yellows=int(r.get("yellows", 0)), reds=int(r.get("reds", 0)),
            corners_taken=int(corners.get((str(r["player"]), str(r["team"])), 0)),
            source=SOURCE_STATSBOMB,
        ))
    return rows


def backfill_statsbomb(con: sqlite3.Connection, ingested_utc: str,
                       cache_dir: Optional[str] = None) -> int:
    """Backfill every WC2018+2022 match into ``player_matches`` (idempotent).

    Uses the same cached fetches as the props dataset — no network when the
    cache is warm.
    """
    from wca.data import statsbomb

    cache = cache_dir or statsbomb.DEFAULT_CACHE_DIR
    total = 0
    for season_id, label in sorted(statsbomb.WC_SEASONS.items()):
        matches = statsbomb.fetch_matches(
            statsbomb.WC_COMPETITION_ID, season_id, cache_dir=cache)
        for m in matches:
            match_id = m["match_id"]
            events = statsbomb.fetch_events(match_id, cache_dir=cache)
            rows = statsbomb_match_rows(
                events, match_id, m.get("match_date"), label)
            total += upsert_rows(con, rows, ingested_utc)
    return total


# ---------------------------------------------------------------------------
# Empirical per-90 rates (the thing shrink_rate() has been waiting for)
# ---------------------------------------------------------------------------

def empirical_rates(
    team: str,
    player: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
    con: Optional[sqlite3.Connection] = None,
    expected_minutes: float = 90.0,
):
    """``PlayerPropRates`` built from real per-match rows, or ``None``.

    Sums count columns over every stored match with recorded minutes and
    converts to per-90. ``sample_minutes`` is the TRUE evidence behind the
    rates, so :func:`wca.models.playerprops.shrink_rate` (n_eff = minutes/90
    against ``SHRINK_K``) weighs it honestly — a 2026-only player with two
    matches gets ~25% empirical weight; prior-WC veterans carry more.
    Name matching is exact on (team, player) after accent folding, same
    convention as players.db; no fuzzy guessing here.
    """
    from wca.data.players_db import _norm_name  # accent-fold convention
    from wca.models.playerprops import PlayerPropRates

    own = False
    if con is None:
        if not os.path.exists(db_path):
            return None
        con = connect(db_path)
        own = True
    try:
        cur = con.execute(
            "SELECT player, team, minutes, shots, sot, goals, assists "
            "FROM player_matches WHERE minutes IS NOT NULL AND minutes > 0"
        )
        t_norm, p_norm = _norm_name(team), _norm_name(player)
        minutes = shots = sot = goals = assists = 0.0
        n_matches = 0
        for row in cur:
            if _norm_name(row[1]) != t_norm or _norm_name(row[0]) != p_norm:
                continue
            minutes += float(row[2])
            shots += row[3]
            sot += row[4]
            goals += row[5]
            assists += row[6]
            n_matches += 1
    finally:
        if own:
            con.close()

    if minutes <= 0 or n_matches == 0:
        return None
    per90 = 90.0 / minutes
    return PlayerPropRates(
        player=player, team=team,
        goals_p90=goals * per90,
        shots_p90=shots * per90,
        sot_p90=sot * per90,
        assists_p90=assists * per90,
        expected_minutes=expected_minutes,
        sample_minutes=minutes,
        rate_source="player_events.db (%d matches)" % n_matches,
        minutes_source="assumed_90",
    )
