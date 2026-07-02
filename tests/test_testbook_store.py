"""Tests for the isolated paper-trading test-book store (wca.testbook.store)."""

from __future__ import annotations

from wca.testbook import store


def _con():
    return store.connect(":memory:")


def test_pl_if_win_math():
    # YES at 0.25, stake $100 -> 400 shares -> win pays $400 -> +$300.
    assert round(store.pl_if_win(0.25, 100.0), 6) == 300.0
    assert store.pl_if_win(0.0, 100.0) == 0.0


def test_seed_is_idempotent():
    con = _con()
    assert store.seed_bankroll(con, 2000.0, ts_utc="2026-06-30T00:00:00Z") is True
    assert store.seed_bankroll(con, 2000.0, ts_utc="2026-06-30T00:00:00Z") is False
    assert store.realized_balance(con) == 2000.0


def test_stake_reserved_then_returned_on_win():
    con = _con()
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="match",
                              selection="A win (FT)", resolution_basis="FT",
                              entry_price=0.50, stake_usd=100.0, model_prob=0.6)
    assert store.realized_balance(con) == 1900.0          # stake reserved
    assert store.deployed_capital(con) == 100.0
    pl = store.settle(con, bid, outcome="won", ts_utc="t2")
    assert round(pl, 6) == 100.0                           # 0.5 -> +100
    assert store.realized_balance(con) == 2100.0           # stake + pl back
    assert store.deployed_capital(con) == 0.0


def test_settle_lost_and_void():
    con = _con()
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    b1 = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="m", selection="x",
                             resolution_basis="advance", entry_price=0.4, stake_usd=80.0)
    b2 = store.log_paper_bet(con, ts_utc="t1", fixture="C vs D", market_type="m", selection="y",
                             resolution_basis="prop", entry_price=0.5, stake_usd=50.0)
    assert store.settle(con, b1, outcome="lost", ts_utc="t2") == -80.0
    assert store.settle(con, b2, outcome="void", ts_utc="t2") == 0.0
    # 2000 -80 -50 (reserved) +0 (lost: stake gone) +50 (void: stake back) = 1920
    assert store.realized_balance(con) == 1920.0


def test_mark_to_market_and_report():
    con = _con()
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="match",
                              selection="A win (FT)", resolution_basis="FT",
                              entry_price=0.50, stake_usd=100.0)
    unreal = store.record_mark(con, bid, 0.60, ts_utc="t2")   # 200 shares * .60 - 100 = +20
    assert round(unreal, 6) == 20.0
    rep = store.report(con)
    assert rep["seed"] == 2000.0
    assert rep["n_open"] == 1
    assert round(rep["unrealized_pl"], 6) == 20.0
    # equity = cash(1900) + deployed(100) + unreal(20) = 2020
    assert round(rep["equity"], 6) == 2020.0
    assert round(rep["roi_pct"], 4) == 1.0
    assert "FT" in rep["by_basis"]


def test_resolution_basis_tracks_ft_vs_advance():
    con = _con()
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    store.log_paper_bet(con, ts_utc="t1", fixture="France vs Sweden", market_type="match",
                        selection="France win (90')", resolution_basis="FT",
                        entry_price=0.55, stake_usd=20.0)
    store.log_paper_bet(con, ts_utc="t1", fixture="France vs Sweden", market_type="advance",
                        selection="France to advance", resolution_basis="advance",
                        entry_price=0.78, stake_usd=20.0)
    bases = {r["resolution_basis"] for r in store.open_bets(con)}
    assert bases == {"FT", "advance"}
