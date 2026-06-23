"""Tests for the FX-adjusted arb pipeline + Betfair(OddsAPI) adapter + FX."""
from __future__ import annotations

import pandas as pd
import pytest

from wca import arbfx, fx
from wca.data import betfair


# -- Part A: Betfair adapter over The Odds API --------------------------------

def _odds_frame():
    return pd.DataFrame([
        {"event_id": "e1", "home_team": "Brazil", "away_team": "Mexico",
         "bookmaker_key": "betfair_ex_uk", "market": "h2h",
         "outcome_name": "Brazil", "decimal_odds": 2.10},
        {"event_id": "e1", "home_team": "Brazil", "away_team": "Mexico",
         "bookmaker_key": "bet365", "market": "h2h",
         "outcome_name": "Brazil", "decimal_odds": 2.05},
    ])


def test_filter_betfair_keeps_only_exchange_and_tags_gbp():
    out = betfair.filter_betfair(_odds_frame())
    assert list(out["bookmaker_key"].unique()) == ["betfair_ex_uk"]
    assert (out["currency"] == "GBP").all()


def test_filter_betfair_empty_is_shaped():
    out = betfair.filter_betfair(pd.DataFrame())
    assert "currency" in out.columns and out.empty


def test_betfair_execution_stub_raises():
    with pytest.raises(NotImplementedError):
        betfair.betfair_execution_stub("place", size=10)


# -- FX: bounded fetch + fallback ---------------------------------------------

def test_fx_live_injected():
    r = fx.get_gbp_usd(fetch=lambda: (1.30, "2026-06-23"))
    assert r.source == "live" and r.usd_per_gbp == 1.30
    assert abs(r.gbp_per_usd - 1 / 1.30) < 1e-9


def test_fx_fallback_on_error():
    def boom():
        raise TimeoutError("slow")
    r = fx.get_gbp_usd(fetch=boom)
    assert r.source == "fallback" and r.usd_per_gbp == fx.FALLBACK_USD_PER_GBP


def test_fx_rejects_insane_rate():
    r = fx.get_gbp_usd(fetch=lambda: (99.0, ""))
    assert r.source == "fallback"


# -- Part B: arb math ---------------------------------------------------------

def test_no_arb_when_inverse_sum_ge_one():
    # Tight prices: no edge after fees.
    opp = arbfx.evaluate_pair(
        fixture="A vs B", market="h2h",
        betfair_outcome="A", betfair_odds=1.90,
        pm_outcome="not A", pm_price=0.55,
        fx_usd_per_gbp=1.27,
    )
    assert opp is None


def test_arb_detected_and_profit_positive():
    # Generous both sides -> risk-free lock.
    opp = arbfx.evaluate_pair(
        fixture="Brazil vs Mexico", market="h2h",
        betfair_outcome="Brazil", betfair_odds=2.30,
        pm_outcome="not Brazil", pm_price=0.40,
        fx_usd_per_gbp=1.27,
    )
    assert opp is not None
    assert opp.fee_adj_edge > 0 and opp.guaranteed_pct > 0
    assert {l.venue for l in opp.legs} == {"betfair", "polymarket"}
    # stake fractions of total outlay are positive
    assert all(l.stake > 0 for l in opp.legs)


def test_fx_haircut_reduces_guaranteed_pct():
    kw = dict(fixture="A vs B", market="h2h", betfair_outcome="A",
              betfair_odds=2.30, pm_outcome="not A", pm_price=0.40,
              fx_usd_per_gbp=1.27)
    hi = arbfx.evaluate_pair(**kw, fx_haircut=0.0)
    lo = arbfx.evaluate_pair(**kw, fx_haircut=0.02)
    assert lo.guaranteed_pct < hi.guaranteed_pct


def test_invalid_inputs_return_none():
    assert arbfx.evaluate_pair(fixture="", market="h2h", betfair_outcome="A",
                               betfair_odds=0.0, pm_outcome="x", pm_price=0.4,
                               fx_usd_per_gbp=1.27) is None
    assert arbfx.evaluate_pair(fixture="", market="h2h", betfair_outcome="A",
                               betfair_odds=2.0, pm_outcome="x", pm_price=1.5,
                               fx_usd_per_gbp=1.27) is None


# -- Part C feed builder ------------------------------------------------------

def test_build_arb_data_pairs_and_guards_settlement():
    from wca import arbdata
    rows = [
        {"event_id": "e1", "home_team": "Brazil", "away_team": "Mexico",
         "market": "h2h", "outcome_name": "Brazil", "decimal_odds": 2.30},
        {"event_id": "e1", "home_team": "Brazil", "away_team": "Mexico",
         "market": "h2h", "outcome_name": "Mexico", "decimal_odds": 3.50},
    ]
    pm = {"Brazil vs Mexico": {"home": 0.40, "away": 0.30, "settlement": "1x2_90min"}}
    out = arbdata.build_arb_data(betfair_rows=rows, pm_quotes=pm,
                                 fx_usd_per_gbp=1.27, fx_source="live", now_utc="t")
    assert out["meta"]["monitoring_only"] is True
    assert "HYPOTHETICAL" in out["hypothetical"]["label"]
    assert all(a["guaranteed_pct"] > 0 for a in out["arbs"])


def test_build_arb_data_drops_non_90min_settlement():
    from wca import arbdata
    rows = [{"event_id": "e1", "home_team": "Brazil", "away_team": "Mexico",
             "market": "h2h", "outcome_name": "Brazil", "decimal_odds": 2.30}]
    pm = {"Brazil vs Mexico": {"home": 0.40, "settlement": "winner_incl_et"}}
    out = arbdata.build_arb_data(betfair_rows=rows, pm_quotes=pm,
                                 fx_usd_per_gbp=1.27, fx_source="live", now_utc="t")
    assert out["arbs"] == []
