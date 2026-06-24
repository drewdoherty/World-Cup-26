"""Build ``data/players.db`` — a SQLite store of player- and team-level rates.

Every numeric row traces to a **real fetched source**:

* per-player and team rates come from the StatsBomb open-data World Cup events
  (2018 + 2022), aggregated by :mod:`wca.data.statsbomb`;
* squad membership comes from ``data/squads.json`` (published 2026 squads) and
  the analyst override store ``data/players.json``.

There is **no fabrication**: a 2026 squad member with no StatsBomb event history
is recorded in ``squad_members`` with ``event_history = 0`` and carries no
invented per-90 numbers. Players whose StatsBomb sample is below
:data:`THIN_MINUTES` are flagged ``thin = 1`` so downstream models can shrink
them toward priors.

Schema
------
``meta(key, value)``
    Provenance: generated timestamp, source URLs, seasons, row counts.
``players(player, team, minutes, shots, sot, goals, xg, npxg, yellows, reds,
          matches, *_p90, source, thin)``
    One row per (player, StatsBomb team). Per-90 rates are NULL when minutes
    are unavailable. ``source`` is the provenance tag; ``thin`` flags small
    samples.
``team_rates(team, matches, shots_pm, sot_pm, corners_pm, fouls_pm,
             yellows_pm, reds_pm, cards_pm, source)``
    One row per team, per-match rates averaged over that team's StatsBomb
    appearances (home and away pooled).
``squad_members(team, player, source, event_history)``
    One row per published-2026-squad player. ``event_history = 1`` only when a
    confident name match exists in ``players`` for the same canonical team.
"""
from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd

from wca.data import statsbomb
from wca.data.teamnames import canonical

# StatsBomb minutes below which a player's per-90 rates are statistically thin
# and should be shrunk toward team/positional priors by downstream models.
THIN_MINUTES = 180.0

SOURCE_PLAYERS = "statsbomb_open_wc2018_2022"
SOURCE_SQUADS = "squads.json (published 2026 squads)"
SOURCE_OVERRIDES = "players.json (analyst override store)"

DEFAULT_DB_PATH = "data/players.db"


def _per90(count, minutes) -> Optional[float]:
    """Per-90 rate, or None when minutes are missing/zero."""
    if minutes is None or not (minutes > 0):
        return None
    try:
        if pd.isna(minutes):
            return None
    except TypeError:
        pass
    return float(count) * 90.0 / float(minutes)


def _norm_name(name: str) -> str:
    """Accent-folded, lowercased, whitespace-collapsed name for matching."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


def team_rates_from_matches(matches_df: pd.DataFrame) -> pd.DataFrame:
    """Per-team, per-match rates pooled over home and away appearances.

    Each StatsBomb match contributes one home-side row and one away-side row to
    the corresponding teams. Rates are simple means over a team's appearances.
    """
    cols = ("shots", "sot", "corners", "fouls", "yellows", "reds")
    rows = []
    for _, m in matches_df.iterrows():
        for side, team_key in (("_home", "home"), ("_away", "away")):
            team = m.get(team_key)
            if not team:
                continue
            rec = {"team": team}
            for c in cols:
                rec[c] = m.get(c + side, 0)
            rows.append(rec)
    if not rows:
        return pd.DataFrame(
            columns=["team", "matches", "shots_pm", "sot_pm", "corners_pm",
                     "fouls_pm", "yellows_pm", "reds_pm", "cards_pm"])
    long = pd.DataFrame(rows)
    agg = long.groupby("team").agg(
        matches=("shots", "size"),
        shots_pm=("shots", "mean"),
        sot_pm=("sot", "mean"),
        corners_pm=("corners", "mean"),
        fouls_pm=("fouls", "mean"),
        yellows_pm=("yellows", "mean"),
        reds_pm=("reds", "mean"),
    ).reset_index()
    agg["cards_pm"] = agg["yellows_pm"] + agg["reds_pm"]
    return agg


def _load_squads(path: str) -> Dict[str, list]:
    """Load published-squad lists keyed by canonical team; skip ``_`` keys."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return {canonical(k): list(v) for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, list)}


def _load_override_names(path: str) -> Dict[str, list]:
    """Load analyst-override player names keyed by canonical team."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: Dict[str, list] = {}
    for team, players in raw.items():
        if team.startswith("_") or not isinstance(players, list):
            continue
        names = [p.get("name") for p in players if isinstance(p, dict) and p.get("name")]
        out[canonical(team)] = names
    return out


def _player_name_index(players_df: pd.DataFrame) -> Dict[str, set]:
    """Map canonical team -> set of normalized full + surname tokens present."""
    idx: Dict[str, set] = {}
    for _, r in players_df.iterrows():
        team = canonical(str(r["team"]))
        full = _norm_name(str(r["player"]))
        if not full:
            continue
        bucket = idx.setdefault(team, set())
        bucket.add(full)
        # surname token (last word) helps match "Neymar" <-> full SB name only
        # when unambiguous; we record it but require a stronger test below.
    return idx


def _has_event_history(team_canon: str, squad_name: str,
                       name_index: Dict[str, set]) -> int:
    """1 iff a confident name match exists for this squad member.

    Confident = the normalized squad name is a full-string match, or every
    token of the (>=2-token) squad name appears as a substring of a single
    StatsBomb full name for the same team. Conservative by design: when in
    doubt we return 0 (treated as prior/thin, never fabricated).
    """
    full_names = name_index.get(team_canon)
    if not full_names:
        return 0
    target = _norm_name(squad_name)
    if not target:
        return 0
    if target in full_names:
        return 1
    tokens = [t for t in target.split() if len(t) > 1]
    if len(tokens) >= 2:
        for sb in full_names:
            if all(t in sb for t in tokens):
                return 1
    return 0


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS players;
        DROP TABLE IF EXISTS team_rates;
        DROP TABLE IF EXISTS squad_members;

        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

        CREATE TABLE players (
            player TEXT NOT NULL,
            team TEXT NOT NULL,
            minutes REAL,
            shots INTEGER,
            sot INTEGER,
            goals INTEGER,
            xg REAL,
            npxg REAL,
            yellows INTEGER,
            reds INTEGER,
            matches INTEGER,
            goals_p90 REAL,
            shots_p90 REAL,
            sot_p90 REAL,
            xg_p90 REAL,
            npxg_p90 REAL,
            yellows_p90 REAL,
            reds_p90 REAL,
            source TEXT NOT NULL,
            thin INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (player, team)
        );

        CREATE TABLE team_rates (
            team TEXT PRIMARY KEY,
            matches INTEGER,
            shots_pm REAL,
            sot_pm REAL,
            corners_pm REAL,
            fouls_pm REAL,
            yellows_pm REAL,
            reds_pm REAL,
            cards_pm REAL,
            source TEXT NOT NULL
        );

        CREATE TABLE squad_members (
            team TEXT NOT NULL,
            player TEXT NOT NULL,
            source TEXT NOT NULL,
            event_history INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (team, player)
        );

        CREATE INDEX idx_players_team ON players(team);
        CREATE INDEX idx_squad_team ON squad_members(team);
        """
    )


def build_players_db(
    cache_dir: str = statsbomb.DEFAULT_CACHE_DIR,
    out_dir: str = "data/processed",
    squads_path: str = "data/squads.json",
    overrides_path: str = "data/players.json",
    db_path: str = DEFAULT_DB_PATH,
    generated_utc: Optional[str] = None,
    matches_df: Optional[pd.DataFrame] = None,
    players_df: Optional[pd.DataFrame] = None,
) -> Dict[str, int]:
    """Build ``players.db`` and return a summary of row counts.

    Parameters
    ----------
    cache_dir, out_dir :
        Passed to :func:`wca.data.statsbomb.build_props_dataset` (local cache;
        no network when the cache is warm).
    squads_path, overrides_path :
        Published-squad and analyst-override JSON.
    db_path :
        Destination SQLite file (written atomically via a temp file).
    generated_utc :
        Provenance timestamp string. ``Date.now()`` is intentionally not called
        here so the build is deterministic; pass the caller's timestamp.
    matches_df, players_df :
        Pre-aggregated frames (used by tests to avoid IO). When omitted they are
        built from the StatsBomb cache.
    """
    if matches_df is None or players_df is None:
        matches_df, players_df = statsbomb.build_props_dataset(
            cache_dir=cache_dir, out_dir=out_dir)

    team_rates = team_rates_from_matches(matches_df)
    squads = _load_squads(squads_path)
    overrides = _load_override_names(overrides_path)
    name_index = _player_name_index(players_df)

    db_path = str(db_path)
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(tmp_path)
    try:
        _create_schema(conn)

        # players ----------------------------------------------------------
        n_players = 0
        n_thin = 0
        for _, r in players_df.iterrows():
            minutes = r.get("minutes")
            has_min = minutes is not None and not pd.isna(minutes) and minutes > 0
            thin = 1 if (not has_min or minutes < THIN_MINUTES) else 0
            n_thin += thin
            conn.execute(
                """INSERT OR REPLACE INTO players
                   (player, team, minutes, shots, sot, goals, xg, npxg,
                    yellows, reds, matches, goals_p90, shots_p90, sot_p90,
                    xg_p90, npxg_p90, yellows_p90, reds_p90, source, thin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(r["player"]), str(r["team"]),
                    None if not has_min else float(minutes),
                    int(r.get("shots", 0)), int(r.get("sot", 0)),
                    int(r.get("goals", 0)), float(r.get("xg_sum", 0.0)),
                    float(r.get("npxg_sum", 0.0)),
                    int(r.get("yellows", 0)), int(r.get("reds", 0)),
                    int(r.get("matches", 0)),
                    _per90(r.get("goals", 0), minutes),
                    _per90(r.get("shots", 0), minutes),
                    _per90(r.get("sot", 0), minutes),
                    _per90(r.get("xg_sum", 0.0), minutes),
                    _per90(r.get("npxg_sum", 0.0), minutes),
                    _per90(r.get("yellows", 0), minutes),
                    _per90(r.get("reds", 0), minutes),
                    SOURCE_PLAYERS, thin,
                ),
            )
            n_players += 1

        # team_rates -------------------------------------------------------
        for _, r in team_rates.iterrows():
            conn.execute(
                """INSERT OR REPLACE INTO team_rates
                   (team, matches, shots_pm, sot_pm, corners_pm, fouls_pm,
                    yellows_pm, reds_pm, cards_pm, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(r["team"]), int(r["matches"]),
                    float(r["shots_pm"]), float(r["sot_pm"]),
                    float(r["corners_pm"]), float(r["fouls_pm"]),
                    float(r["yellows_pm"]), float(r["reds_pm"]),
                    float(r["cards_pm"]), SOURCE_PLAYERS,
                ),
            )

        # squad_members ----------------------------------------------------
        n_squad = 0
        n_history = 0
        for team_canon, names in squads.items():
            for name in names:
                if not name:
                    continue
                eh = _has_event_history(team_canon, name, name_index)
                n_history += eh
                conn.execute(
                    """INSERT OR REPLACE INTO squad_members
                       (team, player, source, event_history)
                       VALUES (?,?,?,?)""",
                    (team_canon, str(name), SOURCE_SQUADS, eh),
                )
                n_squad += 1
        # analyst-override players are also squad members (flagged separately)
        for team_canon, names in overrides.items():
            for name in names:
                if not name:
                    continue
                eh = _has_event_history(team_canon, name, name_index)
                conn.execute(
                    """INSERT OR IGNORE INTO squad_members
                       (team, player, source, event_history)
                       VALUES (?,?,?,?)""",
                    (team_canon, str(name), SOURCE_OVERRIDES, eh),
                )

        # meta -------------------------------------------------------------
        meta = {
            "generated_utc": generated_utc or "",
            "source_players": SOURCE_PLAYERS,
            "source_team_rates": SOURCE_PLAYERS,
            "source_squads": SOURCE_SQUADS,
            "source_overrides": SOURCE_OVERRIDES,
            "statsbomb_raw_base": statsbomb.RAW_BASE,
            "statsbomb_seasons": ",".join(sorted(statsbomb.WC_SEASONS.values())),
            "thin_minutes": str(THIN_MINUTES),
            "n_players": str(n_players),
            "n_players_thin": str(n_thin),
            "n_teams": str(len(team_rates)),
            "n_squad_members": str(n_squad),
            "n_squad_with_history": str(n_history),
        }
        for k, v in meta.items():
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                         (k, v))
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    return {
        "players": n_players,
        "players_thin": n_thin,
        "teams": len(team_rates),
        "squad_members": n_squad,
        "squad_with_history": n_history,
    }
