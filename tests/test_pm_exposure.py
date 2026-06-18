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
    _seed_bets(db)  # exposure on England (Home) and Draw in England vs Croatia
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()

    # A PM proposal on Croatia (the un-exposed outcome) should be a HEDGE;
    # a PM proposal on England (already exposed) should be an ADD.
    proposals = [
        {"match_desc": "England vs Croatia", "outcome": "Croatia",
         "price": 0.18, "size_usd": 25.0, "ev": 0.10},
        {"match_desc": "England vs Croatia", "outcome": "England",
         "price": 0.55, "size_usd": 15.0, "ev": 0.03},
    ]
    section = propose_cli._build_exposure_section(proposals, _odds_df(), db, now)
    assert "England vs Croatia" in section
    assert "existing exposure" in section
    assert "🛡 HEDGE Croatia" in section
    assert "➕ ADD England" in section


def test_exposure_section_ev_pick_when_no_exposure(tmp_path):
    db = str(tmp_path / "wca.db")  # empty ledger -> no exposure
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    proposals = [
        {"match_desc": "Ghana vs Panama", "outcome": "Panama",
         "price": 0.29, "size_usd": 30.0, "ev": 0.27},
    ]
    section = propose_cli._build_exposure_section(proposals, _odds_df(), db, now)
    assert "EV pick: Panama" in section
    assert "HEDGE" not in section


def test_next_5_filters_and_orders(tmp_path):
    now = pd.Timestamp("2026-06-17T12:00:00").to_pydatetime()
    matches = propose_cli._next_5_matches(_odds_df(), now)
    assert [m[0] for m in matches] == ["England", "Ghana"]  # sorted by kickoff
    # A fixture already kicked off is excluded.
    now_late = pd.Timestamp("2026-06-17T21:00:00").to_pydatetime()
    matches2 = propose_cli._next_5_matches(_odds_df(), now_late)
    assert [m[0] for m in matches2] == ["Ghana"]
