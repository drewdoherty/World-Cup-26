"""Alignment tests for #7: the unified model-driven scorer/props source feeding
/next, /goalscorers and /accas — one source of truth, model-priced even with no
bookmaker market.
"""
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from wca.accas import load_model_scorer_legs
from wca.data import players_db
from wca.models.scorer_props import fixture_scorers_payload
from wca.nextmatch import build_goalscorers

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_build_card.py"
_spec = importlib.util.spec_from_file_location("wca_build_card", _SCRIPT)
wbc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wbc)


def _players_frame():
    rows = [
        ("Olivier Giroud", "France", 0.6),
        ("Antoine Griezmann", "France", 0.3),
        ("Neymar da Silva Santos Junior", "Brazil", 0.5),
        ("Vinicius Junior", "Brazil", 0.4),
    ]
    return pd.DataFrame([
        {"player": p, "team": t, "minutes": 90.0, "shots": 3, "sot": 2,
         "goals": 1, "xg_sum": x, "npxg_sum": x, "yellows": 0, "reds": 0,
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
def db_path(tmp_path):
    squads = {"France": ["Olivier Giroud", "Antoine Griezmann"],
              "Brazil": ["Neymar Junior", "Vinicius Junior"]}
    sp = tmp_path / "squads.json"
    op = tmp_path / "players.json"
    db = tmp_path / "players.db"
    sp.write_text(json.dumps(squads))
    op.write_text(json.dumps({"_note": "x"}))
    players_db.build_players_db(
        squads_path=str(sp), overrides_path=str(op), db_path=str(db),
        generated_utc="2026-06-24T00:00:00Z",
        matches_df=_matches_frame(), players_df=_players_frame())
    return str(db)


# ---------------------------------------------------------------------------
# build_goalscorers model-only fallback (drives /next + /goalscorers)
# ---------------------------------------------------------------------------

def test_build_goalscorers_model_only_when_no_market(db_path):
    gs, note = build_goalscorers(
        "France", "Brazil", None, lambda_home=1.7, lambda_away=1.1,
        db_path=db_path, model_only_fallback=True, pm_lookup=False)
    assert gs["home"] and gs["away"]
    assert "model price" in note
    line = gs["home"][0]
    assert line.model_p_anytime is not None
    assert line.model_fair_anytime > 1.0
    assert line.anytime_book_odds is None  # no market


def test_build_goalscorers_no_fallback_stays_empty(db_path):
    # Default behaviour (opt-out) is unchanged: no market -> empty.
    gs, note = build_goalscorers("France", "Brazil", None, lambda_home=1.7,
                                 lambda_away=1.1, pm_lookup=False)
    assert gs == {"home": [], "away": []}
    assert "no sportsbook scorer market" in note


# ---------------------------------------------------------------------------
# Persisted single-source payload + /accas consumption
# ---------------------------------------------------------------------------

def test_payload_and_accas_legs_share_source(db_path, tmp_path):
    payload = fixture_scorers_payload("France", "Brazil", 1.7, 1.1, db_path=db_path)
    assert payload["home_scorers"] and payload["away_scorers"]
    feed = {"meta": {"generated": "t"}, "fixtures": [payload]}
    p = tmp_path / "model_scorers.json"
    p.write_text(json.dumps(feed))

    legs = load_model_scorer_legs(str(p), min_leg_odds=1.0, max_leg_odds=99.0)
    assert legs
    leg = legs[0]
    assert "to score anytime" in leg["selection"]
    assert leg["market"] == "anytime_scorer"
    assert leg["label"] == "model price, no market"
    # Legs sorted by probability descending.
    probs = [l["prob"] for l in legs]
    assert probs == sorted(probs, reverse=True)


def test_accas_legs_missing_file_degrades():
    assert load_model_scorer_legs("does/not/exist.json") == []


def test_accas_legs_respect_odds_band(db_path, tmp_path):
    payload = fixture_scorers_payload("France", "Brazil", 1.7, 1.1, db_path=db_path)
    feed = {"fixtures": [payload]}
    p = tmp_path / "ms.json"
    p.write_text(json.dumps(feed))
    # Very tight band excludes everything outside [2.0, 2.2].
    legs = load_model_scorer_legs(str(p), min_leg_odds=2.0, max_leg_odds=2.2)
    for l in legs:
        assert 2.0 <= l["fair_odds"] <= 2.2


# ---------------------------------------------------------------------------
# Orchestrator gate: model availability lets the fast job rebuild /goalscorers
# ---------------------------------------------------------------------------

_SCAN = Path(__file__).resolve().parent.parent / "scripts" / "wca_event_scan.py"
_scan_spec = importlib.util.spec_from_file_location("wca_event_scan", _SCAN)
scan = importlib.util.module_from_spec(_scan_spec)
_scan_spec.loader.exec_module(scan)


@pytest.mark.parametrize("kind", ["scorers", "corners", "cards"])
def test_event_scan_cli_runs_offline(db_path, kind, capsys):
    rc = scan.main([kind, "--home", "France", "--away", "Brazil",
                    "--lam-home", "1.7", "--lam-away", "1.1", "--db", db_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "France" in out and "Brazil" in out


def test_want_goalscorers_card_model_available():
    # Fast --skip-scorers job: historically blocked.
    assert wbc.want_goalscorers_card("out.md", 5, True, False) is False
    # ...but with a built players.db it can rebuild from the model.
    assert wbc.want_goalscorers_card("out.md", 5, True, False,
                                     model_available=True) is True
    # Dedicated refresh still always rebuilds.
    assert wbc.want_goalscorers_card("out.md", 5, True, True) is True
