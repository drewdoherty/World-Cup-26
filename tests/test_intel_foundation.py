"""Tests for the Market Intelligence foundation (wca.intel)."""

from __future__ import annotations

import sqlite3

import pytest

from wca import intel
from wca.intel import registry, store, normalise
from wca.intel.store import MarketSnapshot


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_resolves_variants_and_colours():
    assert registry.venue_for("betfair_ex_uk").canon == "Betfair"        # OddsAPI key
    assert registry.venue_for("Polymarket").kind == registry.PREDICTION_MARKET
    assert registry.venue_for("Smarkets").is_exchange
    assert registry.commission_for("Smarkets") == 0.02
    assert registry.commission_for("bet365") == 0.0
    # stable, distinct colours for the dashboard
    cols = [registry.venue_colour(n) for n in ("Polymarket", "Betfair", "bet365", "Smarkets", "Paddy Power")]
    assert len(set(cols)) == len(cols)
    assert registry.venue_colour("Some New Book") == registry._DEFAULT_COLOUR


# --------------------------------------------------------------------------- #
# Store: change-gated writes
# --------------------------------------------------------------------------- #


def _con():
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    return c


def _snap(ts, venue, odds, **kw):
    return MarketSnapshot(ts_utc=ts, source="theoddsapi", venue=venue, market_type="moneyline",
                          selection="Home", decimal_odds=odds, implied_raw=1.0 / odds,
                          fixture_id="m1", **kw)


def test_append_writes_first_then_gates_unchanged():
    c = _con()
    assert store.append_snapshots(c, [_snap("2026-06-28T10:00:00Z", "bet365", 2.00)]) == 1
    # same price, within staleness window -> skipped
    assert store.append_snapshots(c, [_snap("2026-06-28T10:10:00Z", "bet365", 2.00)],
                                  max_staleness_s=3600) == 0
    n = c.execute("select count(*) from market_snapshots").fetchone()[0]
    assert n == 1


def test_append_writes_on_material_move():
    c = _con()
    store.append_snapshots(c, [_snap("2026-06-28T10:00:00Z", "bet365", 2.00)])
    # implied 0.50 -> 0.526 (odds 1.90), move 2.6pp >= eps 0.3pp -> written
    assert store.append_snapshots(c, [_snap("2026-06-28T10:05:00Z", "bet365", 1.90)]) == 1


def test_append_rewrites_on_staleness_even_if_flat():
    c = _con()
    store.append_snapshots(c, [_snap("2026-06-28T10:00:00Z", "bet365", 2.00)])
    # flat price but > max_staleness later -> re-stamped
    assert store.append_snapshots(c, [_snap("2026-06-28T12:00:00Z", "bet365", 2.00)],
                                  max_staleness_s=3600) == 1


def test_latest_per_selection_groups_by_venue():
    c = _con()
    store.append_snapshots(c, [
        _snap("2026-06-28T10:00:00Z", "bet365", 2.00),
        _snap("2026-06-28T10:00:00Z", "Smarkets", 2.10),
    ])
    out = store.latest_per_selection(c, "m1", "moneyline")
    assert set(r["venue"] for r in out["Home"]) == {"bet365", "Smarkets"}


# --------------------------------------------------------------------------- #
# Normalise
# --------------------------------------------------------------------------- #


def test_implied_and_complete_market_devig_sums_to_one():
    assert normalise.implied_from_decimal(2.0) == 0.5
    assert normalise.implied_from_decimal(1.0) is None
    snaps = normalise.normalise_market(
        source="theoddsapi", venue="bet365", market_type="moneyline",
        selection_odds={"Home": 1.8, "Draw": 3.6, "Away": 4.5},
        ts_utc="2026-06-28T10:00:00Z", fixture_id="m1", ko_utc="2026-06-28T19:00:00Z")
    assert len(snaps) == 3
    assert abs(sum(s.implied_devig for s in snaps) - 1.0) < 1e-9   # vig removed
    assert all(s.implied_raw > s.implied_devig for s in snaps)     # raw carries the vig
    assert snaps[0].mins_to_ko == 540.0


def test_partial_market_leaves_devig_none():
    snaps = normalise.normalise_market(
        source="theoddsapi", venue="bet365", market_type="moneyline",
        selection_odds={"Home": 1.8}, ts_utc="2026-06-28T10:00:00Z")
    assert snaps[0].implied_devig is None  # never fabricate a fair price from a partial book


def test_from_oddsapi_rows_maps_and_devigs():
    rows = [
        {"ts_utc": "2026-06-28T10:00:00Z", "match_id": "m1", "market": "h2h", "decimal_odds": 1.8,
         "source": "theoddsapi", "raw": {"bookmaker_key": "bet365", "outcome_name": "Mexico",
                                          "home_team": "Mexico", "away_team": "Canada", "commence_time": "2026-06-28T19:00:00Z"}},
        {"ts_utc": "2026-06-28T10:00:00Z", "match_id": "m1", "market": "h2h", "decimal_odds": 3.6,
         "source": "theoddsapi", "raw": {"bookmaker_key": "bet365", "outcome_name": "Draw",
                                          "home_team": "Mexico", "away_team": "Canada", "commence_time": "2026-06-28T19:00:00Z"}},
        {"ts_utc": "2026-06-28T10:00:00Z", "match_id": "m1", "market": "h2h", "decimal_odds": 4.5,
         "source": "theoddsapi", "raw": {"bookmaker_key": "bet365", "outcome_name": "Canada",
                                          "home_team": "Mexico", "away_team": "Canada", "commence_time": "2026-06-28T19:00:00Z"}},
    ]
    snaps = normalise.from_oddsapi_rows(rows)
    assert len(snaps) == 3
    assert {s.selection for s in snaps} == {"Home", "Draw", "Away"}     # outcome->leg mapped
    assert all(s.venue == "bet365" and s.market_type == "moneyline" for s in snaps)
    assert abs(sum(s.implied_devig for s in snaps) - 1.0) < 1e-9


def test_package_exports():
    assert intel.MARKET_TYPES and intel.VENUES and intel.MarketSnapshot is MarketSnapshot
