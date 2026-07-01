"""Tests for the test-book equity series + P&L chart rendering."""

from __future__ import annotations

import pytest

from wca.testbook import chart, store


def _book():
    con = store.connect(":memory:")
    store.seed_bankroll(con, 2000.0, ts_utc="2026-06-30T00:00:00Z")
    return con


def test_equity_series_starts_at_seed_and_tracks_mark():
    con = _book()
    bid = store.log_paper_bet(
        con, ts_utc="2026-06-30T01:00:00Z", fixture="Belgium", market_type="advance",
        selection="Belgium to reach QF", resolution_basis="advance",
        entry_price=0.40, stake_usd=40.0, model_prob=0.48, edge=0.08)
    # Mark it up: 100 shares now worth 0.60 -> +$20 unrealised.
    store.record_mark(con, bid, 0.60, "2026-06-30T02:00:00Z")

    series = store.equity_series(con)
    assert series[0]["equity"] == pytest.approx(2000.0)          # seed anchor first
    assert series[-1]["equity"] == pytest.approx(2020.0)         # seed + $20 MTM
    assert series[-1]["unrealized_pl"] == pytest.approx(20.0)


def test_equity_series_realized_after_settle():
    con = _book()
    bid = store.log_paper_bet(
        con, ts_utc="2026-06-30T01:00:00Z", fixture="X vs Y", market_type="match_result",
        selection="X win (FT 90')", resolution_basis="FT",
        entry_price=0.50, stake_usd=100.0, model_prob=0.60, edge=0.10)
    store.settle(con, bid, outcome="won", ts_utc="2026-06-30T03:00:00Z")  # +$100
    series = store.equity_series(con)
    assert series[0]["equity"] == pytest.approx(2000.0)
    assert series[-1]["equity"] == pytest.approx(2100.0)
    assert series[-1]["realized_pl"] == pytest.approx(100.0)


def test_equity_series_empty_without_seed():
    con = store.connect(":memory:")
    assert store.equity_series(con) == []


def test_render_equity_png_returns_png_bytes():
    pytest.importorskip("matplotlib")
    series = [
        {"ts": "2026-06-30T00:00:00Z", "equity": 2000.0},
        {"ts": "2026-06-30T02:00:00Z", "equity": 1850.0},
        {"ts": "2026-06-30T04:00:00Z", "equity": 1950.0},
    ]
    png = chart.render_equity_png(series, seed=2000.0)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_equity_png_single_point_does_not_crash():
    pytest.importorskip("matplotlib")
    png = chart.render_equity_png([{"ts": "2026-06-30T00:00:00Z", "equity": 2000.0}], seed=2000.0)
    assert png is None or png[:4] == b"\x89PNG"


def test_render_equity_png_empty_is_none():
    assert chart.render_equity_png([], seed=2000.0) is None
