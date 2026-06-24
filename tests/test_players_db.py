"""Tests for wca.data.players_db — the SQLite player/team rate store.

Built entirely from in-memory synthetic frames (no network, no StatsBomb
cache). The headline guarantee under test:

    *every stat traces to a real source — no fabricated players.*

Concretely: every row in ``players`` comes from the supplied StatsBomb frame
and carries a provenance tag; squad members without a confident name match are
recorded with ``event_history = 0`` and carry **no** invented per-90 numbers.
"""
import json
import math
import sqlite3

import numpy as np
import pandas as pd
import pytest

from wca.data import players_db
from wca.data.players_db import (
    _has_event_history,
    _norm_name,
    _per90,
    build_players_db,
    team_rates_from_matches,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_per90_math_and_missing_minutes():
    assert math.isclose(_per90(4, 360.0), 1.0)
    assert _per90(4, 0) is None
    assert _per90(4, None) is None
    assert _per90(4, float("nan")) is None


def test_norm_name_folds_accents_and_case():
    assert _norm_name("Kylian Mbappé Lottin") == "kylian mbappe lottin"
    assert _norm_name("  Neymar   da  Silva ") == "neymar da silva"
    assert _norm_name(None) == ""


def test_has_event_history_confident_only():
    index = {"France": {"kylian mbappe lottin", "olivier giroud"}}
    # Exact normalized full name.
    assert _has_event_history("France", "Olivier Giroud", index) == 1
    # Two-token subset match (accent-folded).
    assert _has_event_history("France", "Mbappé Lottin", index) == 1
    # Single ambiguous token -> NOT confident.
    assert _has_event_history("France", "Mbappe", index) == 0
    # Unknown team / player -> 0.
    assert _has_event_history("Brazil", "Neymar", index) == 0


# ---------------------------------------------------------------------------
# Team rates
# ---------------------------------------------------------------------------

def _matches_frame():
    # Two matches; France plays both (once home, once away).
    return pd.DataFrame([
        {"match_id": 1, "home": "France", "away": "Brazil",
         "shots_home": 10, "sot_home": 4, "corners_home": 6, "fouls_home": 12,
         "yellows_home": 2, "reds_home": 0,
         "shots_away": 8, "sot_away": 3, "corners_away": 5, "fouls_away": 10,
         "yellows_away": 1, "reds_away": 1},
        {"match_id": 2, "home": "Brazil", "away": "France",
         "shots_home": 12, "sot_home": 5, "corners_home": 7, "fouls_home": 9,
         "yellows_home": 0, "reds_home": 0,
         "shots_away": 6, "sot_away": 2, "corners_away": 3, "fouls_away": 11,
         "yellows_away": 3, "reds_away": 0},
    ])


def test_team_rates_pools_home_and_away():
    rates = team_rates_from_matches(_matches_frame())
    fr = rates[rates["team"] == "France"].iloc[0]
    assert fr["matches"] == 2
    # France shots: 10 (home, m1) and 6 (away, m2) -> mean 8
    assert math.isclose(fr["shots_pm"], 8.0)
    assert math.isclose(fr["sot_pm"], 3.0)  # 4, 2
    # cards = yellows + reds means: (2+3)/2 yellows + (0+0)/2 reds = 2.5
    assert math.isclose(fr["yellows_pm"], 2.5)
    assert math.isclose(fr["cards_pm"], 2.5)


# ---------------------------------------------------------------------------
# Full build
# ---------------------------------------------------------------------------

def _players_frame():
    return pd.DataFrame([
        {"player": "Olivier Giroud", "team": "France", "minutes": 360.0,
         "shots": 8, "sot": 4, "goals": 2, "xg_sum": 1.8, "npxg_sum": 1.8,
         "yellows": 1, "reds": 0, "matches": 4},
        {"player": "Kylian Mbappé Lottin", "team": "France", "minutes": 180.0,
         "shots": 10, "sot": 6, "goals": 3, "xg_sum": 2.4, "npxg_sum": 2.0,
         "yellows": 0, "reds": 0, "matches": 2},
        # Thin sample: under THIN_MINUTES.
        {"player": "Bench Guy", "team": "France", "minutes": 30.0,
         "shots": 1, "sot": 0, "goals": 0, "xg_sum": 0.05, "npxg_sum": 0.05,
         "yellows": 0, "reds": 0, "matches": 1},
        # Missing minutes -> per90 must be NULL, flagged thin.
        {"player": "No Minutes", "team": "Brazil", "minutes": np.nan,
         "shots": 2, "sot": 1, "goals": 1, "xg_sum": 0.5, "npxg_sum": 0.5,
         "yellows": 0, "reds": 0, "matches": 1},
    ])


@pytest.fixture()
def built_db(tmp_path):
    squads = {
        "_note": "ignored",
        "France": ["Olivier Giroud", "Kylian Mbappé", "Phantom Prospect"],
        "Scotland": ["Lawrence Shankland"],  # no event history at all
    }
    overrides = {
        "_note": "ignored",
        "Scotland": [{"name": "Lawrence Shankland", "npxg_share": 0.3}],
    }
    squads_path = tmp_path / "squads.json"
    overrides_path = tmp_path / "players.json"
    squads_path.write_text(json.dumps(squads))
    overrides_path.write_text(json.dumps(overrides))
    db_path = tmp_path / "players.db"

    summary = build_players_db(
        squads_path=str(squads_path),
        overrides_path=str(overrides_path),
        db_path=str(db_path),
        generated_utc="2026-06-24T00:00:00Z",
        matches_df=_matches_frame(),
        players_df=_players_frame(),
    )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn, summary, _players_frame()
    conn.close()


def test_schema_tables_present(built_db):
    conn, _, _ = built_db
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "players", "team_rates", "squad_members"} <= names


def test_per90_columns_correct(built_db):
    conn, _, _ = built_db
    giroud = conn.execute(
        "SELECT * FROM players WHERE player='Olivier Giroud'").fetchone()
    # 2 goals in 360 min -> 0.5 / 90
    assert math.isclose(giroud["goals_p90"], 0.5)
    assert math.isclose(giroud["sot_p90"], 1.0)  # 4 sot / 360 min
    assert giroud["thin"] == 0
    assert giroud["source"] == players_db.SOURCE_PLAYERS


def test_missing_minutes_yields_null_per90_and_thin(built_db):
    conn, _, _ = built_db
    nm = conn.execute(
        "SELECT * FROM players WHERE player='No Minutes'").fetchone()
    assert nm["minutes"] is None
    assert nm["goals_p90"] is None  # NOT fabricated
    assert nm["thin"] == 1


def test_thin_flag_below_threshold(built_db):
    conn, _, _ = built_db
    bench = conn.execute(
        "SELECT thin FROM players WHERE player='Bench Guy'").fetchone()
    assert bench["thin"] == 1  # 30 < THIN_MINUTES


def test_no_fabricated_players(built_db):
    """Every players row traces to the supplied StatsBomb frame."""
    conn, _, players_df = built_db
    source_names = set(players_df["player"])
    rows = conn.execute("SELECT player, source FROM players").fetchall()
    assert rows, "expected player rows"
    for r in rows:
        assert r["player"] in source_names, "player not in source frame"
        assert r["source"] == players_db.SOURCE_PLAYERS


def test_squad_without_history_has_no_stats(built_db):
    """A squad member with no event match must NOT appear in `players`."""
    conn, _, _ = built_db
    # Scotland's Shankland is in squads/overrides but has no StatsBomb history.
    sm = conn.execute(
        "SELECT * FROM squad_members WHERE player='Lawrence Shankland'").fetchone()
    assert sm is not None
    assert sm["event_history"] == 0
    # And he must not have leaked into the stats table.
    leaked = conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE player='Lawrence Shankland'"
    ).fetchone()
    assert leaked["n"] == 0
    # Phantom squad name with no SB match -> event_history 0.
    phantom = conn.execute(
        "SELECT event_history FROM squad_members WHERE player='Phantom Prospect'"
    ).fetchone()
    assert phantom["event_history"] == 0


def test_squad_member_with_history_flagged(built_db):
    conn, _, _ = built_db
    giroud = conn.execute(
        "SELECT event_history FROM squad_members WHERE player='Olivier Giroud'"
    ).fetchone()
    assert giroud["event_history"] == 1


def test_meta_provenance_recorded(built_db):
    conn, summary, _ = built_db
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert meta["generated_utc"] == "2026-06-24T00:00:00Z"
    assert meta["source_players"] == players_db.SOURCE_PLAYERS
    assert "statsbomb" in meta["statsbomb_raw_base"]
    assert int(meta["n_players"]) == summary["players"] == 4
