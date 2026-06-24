"""Tests for wca.models.scorer_props — the unified model-driven props source."""
import json
import math

import pandas as pd
import pytest

from wca.data import players_db
from wca.models import scorer_props as sp


def _players_frame():
    # minutes=90 -> npxg_p90 == npxg_sum.
    rows = [
        ("Olivier Giroud", "France", 0.6),
        ("Antoine Griezmann", "France", 0.3),
        ("Adrien Rabiot", "France", 0.1),       # low share
        ("Neymar da Silva Santos Junior", "Brazil", 0.5),
    ]
    return pd.DataFrame([
        {"player": p, "team": t, "minutes": 90.0, "shots": 3, "sot": 2,
         "goals": 1, "xg_sum": x, "npxg_sum": x, "yellows": 1, "reds": 0,
         "matches": 1}
        for p, t, x in rows
    ])


def _matches_frame():
    return pd.DataFrame([{
        "match_id": 1, "home": "France", "away": "Brazil",
        "shots_home": 14, "sot_home": 5, "corners_home": 6, "fouls_home": 13,
        "yellows_home": 2, "reds_home": 0,
        "shots_away": 9, "sot_away": 3, "corners_away": 5, "fouls_away": 11,
        "yellows_away": 1, "reds_away": 0,
    }])


@pytest.fixture()
def env(tmp_path):
    squads = {
        "France": ["Olivier Giroud", "Antoine Griezmann", "Adrien Rabiot"],
        "Brazil": ["Neymar Junior"],
        "Scotland": ["Lawrence Shankland"],  # override-only, no SB history
    }
    overrides = {
        "Scotland": [{"name": "Lawrence Shankland", "npxg_share": 0.30,
                      "penalty_taker": True, "expected_minutes": 85,
                      "source": "analyst_estimate"}],
        "France": [{"name": "Antoine Griezmann", "npxg_share": 0.40,
                    "penalty_taker": True, "source": "analyst_estimate"}],
    }
    sp_path = tmp_path / "squads.json"
    op_path = tmp_path / "players.json"
    db = tmp_path / "players.db"
    sp_path.write_text(json.dumps(squads))
    op_path.write_text(json.dumps(overrides))
    players_db.build_players_db(
        squads_path=str(sp_path), overrides_path=str(op_path), db_path=str(db),
        generated_utc="2026-06-24T00:00:00Z",
        matches_df=_matches_frame(), players_df=_players_frame())
    return {"db": str(db), "overrides": str(op_path)}


def test_team_params_shares_and_override_precedence(env):
    params = sp.team_scorer_params("France", db_path=env["db"],
                                   overrides_path=env["overrides"])
    by = {p.name: p for p in params}
    # Griezmann overridden -> share 0.40, penalty taker, override source.
    assert math.isclose(by["Antoine Griezmann"].npxg_share, 0.40)
    assert by["Antoine Griezmann"].penalty_taker is True
    assert "override" in by["Antoine Griezmann"].source
    # Giroud db-derived: 0.6 / (0.6+0.3+0.1) = 0.6.
    assert math.isclose(by["Olivier Giroud"].npxg_share, 0.6)
    assert "players.db" in by["Olivier Giroud"].source


def test_override_only_team_priced_from_analyst_store(env):
    params = sp.team_scorer_params("Scotland", db_path=env["db"],
                                   overrides_path=env["overrides"])
    assert len(params) == 1
    assert params[0].name == "Lawrence Shankland"
    assert "override" in params[0].source


def test_model_scorer_lines_work_without_market(env):
    lines = sp.model_scorer_lines("France", "Brazil", 1.7, 1.0,
                                  db_path=env["db"], overrides_path=env["overrides"],
                                  top_n_per_team=2)
    assert lines["home"] and lines["away"]
    # Sorted by model anytime probability, descending.
    ph = [l.model_p_anytime for l in lines["home"]]
    assert ph == sorted(ph, reverse=True)
    for l in lines["home"]:
        assert l.label == sp.MODEL_ONLY_LABEL
        assert l.model_fair_anytime > 1.0
        assert 0.0 < l.model_p_anytime < 1.0


def test_overlay_market_adds_ev_and_relabels(env):
    lines = sp.model_scorer_lines("France", "Brazil", 1.7, 1.0,
                                  db_path=env["db"], overrides_path=env["overrides"],
                                  top_n_per_team=3)
    top = lines["home"][0].player
    book = pd.DataFrame([{
        "market": "player_goal_scorer_anytime", "outcome_name": "Yes",
        "outcome_description": top, "decimal_odds": 5.0,
        "bookmaker_title": "TestBook",
    }])
    sp.overlay_market(lines, scorer_df=book, pm_lookup=False)
    enriched = lines["home"][0]
    assert enriched.book_anytime_odds == 5.0
    assert enriched.label == "model + market"
    assert enriched.anytime_ev == pytest.approx(enriched.model_p_anytime * 5.0)
    assert enriched.anytime_edge_pct == pytest.approx((enriched.anytime_ev - 1) * 100)


def test_corners_scan_lines(env):
    rows = sp.corners_scan("France", "Brazil", 1.7, 1.0)
    assert len(rows) == 3
    for r in rows:
        assert 0.0 < r.p_over < 1.0
        assert r.label == sp.MODEL_ONLY_LABEL
        assert math.isclose(1 / r.fair_over + 1 / r.fair_under, 1.0, rel_tol=1e-9)


def test_cards_scan_red_shrinkage_nonzero(env):
    rows, p_red, mean = sp.cards_scan("France", "Brazil", 1.7, 1.0, db_path=env["db"])
    # Both teams had zero reds in the sample, but shrinkage toward the base
    # keeps red risk strictly positive (not a fabricated zero).
    assert p_red > 0.0
    assert mean > 0.0
    assert len(rows) == 3
