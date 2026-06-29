"""Tests for the tiered, budget-aware Market-Intelligence collector.

Covers: the planner's cadence/market set per time-to-kickoff window, the budget
governor's graceful degradation (props dropped first, moneyline always kept),
the config loader's defaults, the OddsAPI/Polymarket adapters mapping a raw row
-> MarketSnapshot, and an end-to-end change-gated append round-trip on an
in-memory SQLite store.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wca.intel import poller, store
from wca.intel.poller import Fixture, PINNED_MARKET
from wca.intel.sources import OddsApiSource, PolymarketSource


NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _fx_at(mins_to_ko: float, fid: str = "fx") -> Fixture:
    """Fixture kicking off ``mins_to_ko`` minutes from NOW (via ko_utc, so the
    planner's own mins-to-ko math is exercised)."""
    ko = NOW + timedelta(minutes=mins_to_ko)
    return Fixture(fixture_id=fid, ko_utc=ko.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _plan_one(fx: Fixture, **kw):
    return poller.plan_polls([fx], now=NOW, **kw)[0]


# --------------------------------------------------------------------------- #
# Planner: cadence + market set per window
# --------------------------------------------------------------------------- #

def test_window_far_out_moneyline_plus_totals_6h():
    p = _plan_one(_fx_at(48 * 60))           # >24h
    assert p.due is True                      # never polled -> due
    assert set(p.markets) == {"moneyline", "ou"}
    assert p.cadence_s == 21600.0             # 6h

def test_window_24h_to_3h_hourly_adds_ah_btts():
    p = _plan_one(_fx_at(12 * 60))           # 12h -> 24h-3h bucket
    assert p.cadence_s == 3600.0              # 1h
    assert set(p.markets) == {"moneyline", "ou", "ah", "btts"}

def test_window_3h_to_1h_30min_adds_props():
    p = _plan_one(_fx_at(120))               # 2h -> 3h-1h bucket
    assert p.cadence_s == 1800.0             # 30m
    assert "player_prop" in p.markets
    assert {"moneyline", "ou", "ah", "btts"}.issubset(set(p.markets))

def test_window_under_1h_tight_cadence_full_set():
    p = _plan_one(_fx_at(30))                # 30m -> 1h-KO bucket
    assert p.cadence_s == 720.0             # 12m
    assert {"moneyline", "ou", "ah", "btts", "player_prop", "team_total"} <= set(p.markets)

def test_past_kickoff_not_due():
    p = _plan_one(_fx_at(-5))
    assert p.due is False
    assert p.markets == []
    assert "past kickoff" in p.reason

def test_no_kickoff_time_not_due():
    p = _plan_one(Fixture(fixture_id="x"))
    assert p.due is False
    assert "no kickoff" in p.reason


# --------------------------------------------------------------------------- #
# Planner: cadence gating via last_polled_at
# --------------------------------------------------------------------------- #

def test_not_due_when_polled_recently():
    fx = _fx_at(12 * 60)                      # 1h cadence
    recent = {fx.fixture_id: NOW - timedelta(minutes=10)}
    p = _plan_one(fx, last_polled_at=recent)
    assert p.due is False
    assert p.markets == []
    assert "not due" in p.reason

def test_due_when_cadence_elapsed():
    fx = _fx_at(12 * 60)                      # 1h cadence
    old = {fx.fixture_id: NOW - timedelta(minutes=90)}
    p = _plan_one(fx, last_polled_at=old)
    assert p.due is True
    assert set(p.markets) == {"moneyline", "ou", "ah", "btts"}


# --------------------------------------------------------------------------- #
# Budget governor
# --------------------------------------------------------------------------- #

def test_budget_above_floor_keeps_everything():
    p = _plan_one(_fx_at(30), remaining_credits=5000)
    assert p.degraded is False
    assert "player_prop" in p.markets and "team_total" in p.markets

def test_budget_at_floor_drops_props_keeps_moneyline():
    # default floor=500, hard_floor=100
    p = _plan_one(_fx_at(30), remaining_credits=300)
    assert p.degraded is True
    assert PINNED_MARKET in p.markets
    assert "player_prop" not in p.markets    # priority 1 -> shed first
    assert "team_total" not in p.markets
    # mid tiers survive at the soft floor
    assert "ah" in p.markets and "btts" in p.markets and "ou" in p.markets
    assert "shed low-priority" in p.reason

def test_budget_hard_floor_sheds_more_and_halves_cadence():
    p = _plan_one(_fx_at(30), remaining_credits=50)
    assert p.degraded is True
    assert PINNED_MARKET in p.markets
    # only the top non-pinned tier (ou, priority 4) survives min_priority=4
    assert "ah" not in p.markets and "btts" not in p.markets
    assert "player_prop" not in p.markets
    assert "ou" in p.markets
    assert p.cadence_s == 720.0 * 2          # halved cadence -> doubled seconds
    assert "halve cadence" in p.reason

def test_budget_unknown_no_degradation():
    p = _plan_one(_fx_at(30), remaining_credits=None)
    assert p.degraded is False
    assert "player_prop" in p.markets

def test_moneyline_never_dropped_even_at_zero_credits():
    p = _plan_one(_fx_at(30), remaining_credits=0)
    assert PINNED_MARKET in p.markets


# --------------------------------------------------------------------------- #
# available_markets intersection
# --------------------------------------------------------------------------- #

def test_available_markets_intersection_drops_unoffered():
    # OddsAPI only offers moneyline/ou/btts -> ah & props should drop out even
    # without budget pressure, but moneyline stays pinned.
    p = _plan_one(_fx_at(30), available_markets=("moneyline", "ou", "btts"))
    assert "ah" not in p.markets
    assert "player_prop" not in p.markets
    assert {"moneyline", "ou", "btts"} <= set(p.markets)


# --------------------------------------------------------------------------- #
# Config loader
# --------------------------------------------------------------------------- #

def test_config_defaults_when_missing():
    cfg = poller.load_polling_config("/nonexistent/path/intel_polling.yml")
    assert len(cfg.windows) == 4
    assert cfg.budget.floor_credits == 500.0
    assert cfg.market_priority("moneyline") > cfg.market_priority("player_prop")

def test_config_default_when_path_none():
    cfg = poller.load_polling_config(None)
    assert cfg.window_for(48 * 60).cadence_s == 21600.0

def test_config_loads_real_file_if_present():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "data", "intel_polling.yml")
    if not os.path.exists(path):
        pytest.skip("intel_polling.yml not present")
    cfg = poller.load_polling_config(path)
    # whichever parser ran, the documented shape must come through
    assert cfg.window_for(48 * 60).cadence_s == 21600.0
    assert set(cfg.window_for(48 * 60).markets) == {"moneyline", "ou"}
    assert cfg.window_for(30).cadence_s == 720.0
    assert cfg.budget.floor_credits == 500.0
    assert cfg.budget.hard_floor_credits == 100.0


# --------------------------------------------------------------------------- #
# Source adapters
# --------------------------------------------------------------------------- #

def test_oddsapi_adapter_maps_raw_row_to_snapshot():
    src = OddsApiSource()
    assert src.name == "theoddsapi"
    assert set(src.supported_markets) == {"moneyline", "moneyline_lay", "ou", "btts"}
    assert "Betfair" in src.venues          # exchange relay venue present
    rows = [
        {"ts_utc": "2026-06-28T10:00:00Z", "source": "theoddsapi", "match_id": "m1",
         "market": "h2h", "selection": "Home", "decimal_odds": 2.0,
         "raw": {"bookmaker_key": "bet365", "outcome_name": "France",
                 "home_team": "France", "away_team": "Spain",
                 "commence_time": "2026-06-28T20:00:00Z"}},
        {"ts_utc": "2026-06-28T10:00:00Z", "source": "theoddsapi", "match_id": "m1",
         "market": "h2h", "selection": "Away", "decimal_odds": 4.0,
         "raw": {"bookmaker_key": "bet365", "outcome_name": "Spain",
                 "home_team": "France", "away_team": "Spain",
                 "commence_time": "2026-06-28T20:00:00Z"}},
        {"ts_utc": "2026-06-28T10:00:00Z", "source": "theoddsapi", "match_id": "m1",
         "market": "h2h", "selection": "Draw", "decimal_odds": 3.5,
         "raw": {"bookmaker_key": "bet365", "outcome_name": "Draw",
                 "home_team": "France", "away_team": "Spain",
                 "commence_time": "2026-06-28T20:00:00Z"}},
    ]
    snaps = src.to_snapshots(rows)
    assert len(snaps) == 3
    s = next(x for x in snaps if x.selection == "Home")
    assert s.market_type == "moneyline"
    assert s.venue == "bet365"
    assert s.fixture_id == "m1"
    assert abs(s.implied_raw - 0.5) < 1e-9
    # complete 3-way book -> devig filled
    assert s.implied_devig is not None and 0 < s.implied_devig < 1

def test_oddsapi_cost_estimate():
    assert OddsApiSource.cost_estimate(4, 1) == 4
    assert OddsApiSource.cost_estimate(4, 2) == 8
    assert OddsApiSource.cost_estimate(0, 3) == 0
    assert OddsApiSource.cost_estimate(-1, 2) == 0

def test_polymarket_adapter_maps_mid_to_snapshot():
    src = PolymarketSource()
    assert src.name == "polymarket"
    rows = [
        {"ts_utc": "2026-06-28T10:00:00Z", "team": "Argentina", "pm_mid": 0.25,
         "fixture_id": "out-winner", "market_type": "moneyline"},
        {"ts_utc": "2026-06-28T10:00:00Z", "team": "Brazil", "pm_mid": 0.0},  # invalid mid
    ]
    snaps = src.to_snapshots(rows)
    assert len(snaps) == 1
    s = snaps[0]
    assert s.venue == "polymarket"
    assert s.source == "polymarket"
    assert s.selection == "Argentina"
    assert abs(s.decimal_odds - 4.0) < 1e-9
    assert abs(s.implied_raw - 0.25) < 1e-9
    # single binary leg -> no fabricated devig
    assert s.implied_devig is None

def test_polymarket_adapter_accepts_explicit_decimal():
    src = PolymarketSource()
    rows = [{"ts_utc": "2026-06-28T10:00:00Z", "selection": "Yes",
             "decimal_odds": 2.5}]
    snaps = src.to_snapshots(rows)
    assert len(snaps) == 1
    assert abs(snaps[0].implied_raw - 0.4) < 1e-9


# --------------------------------------------------------------------------- #
# End-to-end: change-gated append round-trip
# --------------------------------------------------------------------------- #

def _oddsapi_rows(ts: str, home_odds: float):
    return [
        {"ts_utc": ts, "source": "theoddsapi", "match_id": "m1", "market": "h2h",
         "selection": "Home", "decimal_odds": home_odds,
         "raw": {"bookmaker_key": "bet365", "outcome_name": "France",
                 "home_team": "France", "away_team": "Spain",
                 "commence_time": "2026-06-28T20:00:00Z"}},
        {"ts_utc": ts, "source": "theoddsapi", "match_id": "m1", "market": "h2h",
         "selection": "Away", "decimal_odds": 4.0,
         "raw": {"bookmaker_key": "bet365", "outcome_name": "Spain",
                 "home_team": "France", "away_team": "Spain",
                 "commence_time": "2026-06-28T20:00:00Z"}},
    ]


def test_append_snapshots_change_gating_round_trip():
    con = sqlite3.connect(":memory:")
    store.ensure_schema(con)
    src = OddsApiSource()

    # First poll -> both selections written.
    n1 = store.append_snapshots(con, src.to_snapshots(_oddsapi_rows("2026-06-28T10:00:00Z", 2.0)))
    assert n1 == 2

    # Re-poll a few minutes later, identical odds -> change-gate skips both
    # (no move, max_staleness not elapsed).
    n2 = store.append_snapshots(con, src.to_snapshots(_oddsapi_rows("2026-06-28T10:05:00Z", 2.0)))
    assert n2 == 0

    # Re-poll with a material move on Home -> that one writes.
    n3 = store.append_snapshots(con, src.to_snapshots(_oddsapi_rows("2026-06-28T10:10:00Z", 2.5)))
    assert n3 >= 1

    total = con.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    assert total == n1 + n2 + n3
    con.close()
