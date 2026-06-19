"""Tests for the Polymarket exposure + hedge header (overhauled).

Covers:
1. ``wca.ledger.reports.sportsbook_open_exposure_by_match`` — only model/offer
   single-match open bets count; accumulators are skipped; free bets expose
   profit-at-risk (stake*(odds-1)) not stake.
2. ``scripts/wca_pm_propose._build_exposure_section`` — next-5 filtering, the
   HEDGE-vs-ADD labelling when existing exposure is present, and the EV-pick
   fallback when there is none.
"""
from __future__ import annotations

import importlib.util
import os

import pandas as pd

from wca.ledger.reports import sportsbook_open_exposure_by_match
from wca.ledger.store import record_bet


# Load the CLI module (scripts/ is not a package) by path.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROPOSE_PATH = os.path.join(os.path.dirname(_HERE), "scripts", "wca_pm_propose.py")
_spec = importlib.util.spec_from_file_location("wca_pm_propose", _PROPOSE_PATH)
propose_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(propose_cli)


def _seed_bets(db):
    # A model single-match bet on England (Home) in England vs Croatia.
    record_bet(
        "2026-06-17T10:00:00", "M1", "England vs Croatia", "Match Odds",
        "England", "bet365", 1.8, 50.0, source="model", db_path=db,
    )
    # A free bet (offer) on the Draw in the same match: risk = 20*(3.5-1)=50.
    record_bet(
        "2026-06-17T10:05:00", "M1", "England vs Croatia", "Match Odds",
        "Draw", "betfred", 3.5, 20.0, source="offer", db_path=db,
    )
    # An accumulator (multi-match) — must be ignored by the helper.
    record_bet(
        "2026-06-17T10:10:00", "ACC", "England vs Croatia | Ghana vs Panama",
        "Accumulator", "England + Ghana", "Betfair", 4.0, 10.0,
        source="model", db_path=db,
    )
    # A punt single-match bet — wrong source, must be ignored.
    record_bet(
        "2026-06-17T10:15:00", "M2", "Ghana vs Panama", "Match Odds",
        "Ghana", "betfair", 2.4, 30.0, source="punt", db_path=db,
    )


def test_sportsbook_exposure_only_model_offer_single_matches(tmp_path):
    db = str(tmp_path / "wca.db")
    _seed_bets(db)

    exp = sportsbook_open_exposure_by_match(db)
    # Only England vs Croatia (model + offer singles) — acca and punt excluded.
    assert len(exp) == 1
    key = frozenset({"England", "Croatia"})
    assert key in exp
    outcomes = exp[key]["outcomes"]
    assert set(outcomes) == {"England", "Draw"}
    # Real-money stake exposed at stake; free bet exposes profit-at-risk.
    assert outcomes["England"]["risk"] == 50.0
    assert outcomes["Draw"]["risk"] == 50.0  # 20 * (3.5 - 1)
    assert outcomes["England"]["stake"] == 50.0
    assert outcomes["Draw"]["stake"] == 20.0


def _odds_df():
    # Two fixtures; England vs Croatia is the sooner one.
    return pd.DataFrame(
        [
            {"home_team": "England", "away_team": "Croatia",
             "commence_time": "2026-06-17T20:00:00Z"},
            {"home_team": "Ghana", "away_team": "Panama",
             "commence_time": "2026-06-17T23:00:00Z"},
        ]
    )


def test_exposure_section_labels_hedge_vs_add(tmp_path):
    db = str(tmp_path / "wca.db")
    _seed_bets(db)  # result exposure on England (Home) + Draw in England vs Croatia
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()

    # Realistic shape: outcome is the bare PM 'Yes'; the real 1X2 selection
    # lives in market_question. Croatia (un-exposed result) -> HEDGE;
    # England (already exposed) -> ADD.
    proposals = [
        {"match_desc": "England vs Croatia",
         "market_question": "Will Croatia win on 2026-06-17?", "outcome": "Yes",
         "price": 0.18, "size_usd": 25.0, "ev": 0.10},
        {"match_desc": "England vs Croatia",
         "market_question": "Will England win on 2026-06-17?", "outcome": "Yes",
         "price": 0.55, "size_usd": 15.0, "ev": 0.03},
    ]
    section = propose_cli._build_exposure_section(proposals, _odds_df(), db, now)
    assert "England vs Croatia" in section
    assert "result exposure" in section
    # PM-1 = Croatia (higher EV, listed first), PM-2 = England.
    assert "🛡 HEDGE PM-1 Croatia to WIN" in section
    assert "➕ ADD PM-2 England to WIN" in section


def test_exposure_section_ev_pick_when_no_exposure(tmp_path):
    db = str(tmp_path / "wca.db")  # empty ledger -> no exposure
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    proposals = [
        {"match_desc": "Ghana vs Panama",
         "market_question": "Will Panama win on 2026-06-17?", "outcome": "Yes",
         "price": 0.29, "size_usd": 30.0, "ev": 0.27},
    ]
    section = propose_cli._build_exposure_section(proposals, _odds_df(), db, now)
    assert "✅ PM-1 Panama to WIN" in section
    assert "HEDGE" not in section


def test_prop_exposure_is_not_hedged_by_a_result_bet(tmp_path):
    # The McTominay-prop bug: a player-shots prop is exposure on the fixture but
    # a 1X2 result bet does NOT offset it -> must be an EV pick, never a HEDGE.
    db = str(tmp_path / "wca.db")
    record_bet(
        "2026-06-17T09:00:00", "PROP1", "Scotland vs Morocco",
        "Player shots on target",
        "Scott McTominay to have 1 or more shots on target", "paddypower",
        1.8, 5.0, source="offer", db_path=db,
    )
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    odds = pd.DataFrame([
        {"home_team": "Scotland", "away_team": "Morocco",
         "commence_time": "2026-06-17T20:00:00Z"},
    ])
    proposals = [
        {"match_desc": "Scotland vs Morocco",
         "market_question": "Will Scotland win on 2026-06-17?", "outcome": "Yes",
         "price": 0.17, "size_usd": 18.0, "ev": 0.139},
    ]
    section = propose_cli._build_exposure_section(proposals, odds, db, now)
    assert "HEDGE" not in section
    assert "does NOT offset" in section
    assert "✅ PM-1 Scotland to WIN" in section


def test_next_5_filters_and_orders(tmp_path):
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    matches = propose_cli._next_5_matches(_odds_df(), now)
    assert [m[0] for m in matches] == ["England", "Ghana"]  # sorted by kickoff
    # A fixture already kicked off is excluded.
    now_late = pd.Timestamp("2026-06-17T21:00:00").to_pydatetime()
    matches2 = propose_cli._next_5_matches(_odds_df(), now_late)
    assert [m[0] for m in matches2] == ["Ghana"]


def test_exposure_matches_v_and_dash_separators(tmp_path):
    # Result bets are logged with varied separators (" v ", " - ", " vs ");
    # all must be attributed to the fixture so hedge/add logic can fire.
    db = str(tmp_path / "wca.db")
    record_bet(
        "2026-06-17T10:00:00", "MV", "Scotland v Morocco", "Match Odds",
        "Scotland", "betfair", 1.8, 40.0, source="model", db_path=db,
    )
    exp = sportsbook_open_exposure_by_match(db)
    assert frozenset({"Scotland", "Morocco"}) in exp

    # End-to-end: a " v " Scotland-win bet -> the DRAW proposal HEDGEs it and the
    # Scotland-win proposal ADDs to it (the bug that left both as bare EV picks).
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    odds = pd.DataFrame([
        {"home_team": "Scotland", "away_team": "Morocco",
         "commence_time": "2026-06-17T20:00:00Z"},
    ])
    proposals = [
        {"match_desc": "Scotland vs Morocco",
         "market_question": "Will Scotland vs. Morocco end in a draw?", "outcome": "Yes",
         "price": 0.27, "size_usd": 14.0, "ev": 0.06},
        {"match_desc": "Scotland vs Morocco",
         "market_question": "Will Scotland win on 2026-06-17?", "outcome": "Yes",
         "price": 0.17, "size_usd": 18.0, "ev": 0.14},
    ]
    section = propose_cli._build_exposure_section(proposals, odds, db, now)
    assert "result exposure" in section
    assert "🛡 HEDGE PM-1 the DRAW" in section
    assert "➕ ADD PM-2 Scotland to WIN" in section
