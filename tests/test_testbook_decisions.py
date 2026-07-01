"""Tests for the paper-book decision-quality layer (store/trader/settle)."""

from __future__ import annotations

import math

from wca.testbook import settle, store, trader


def _con():
    con = store.connect(":memory:")
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    return con


# --------------------------------------------------------------------------- pure math


def test_f_target_caps_kelly():
    # q=0.6,p=0.5 -> kelly 0.2 -> half 0.1 -> capped at 0.02.
    assert round(store.kelly_fraction(0.6, 0.5), 6) == 0.2
    assert round(store.f_target(0.6, 0.5, 0.5, 0.02), 6) == 0.02
    assert round(store.f_target(0.52, 0.5, 0.5, 0.02), 6) == round(0.5 * (0.02 / 0.5), 6)  # uncapped


def test_g_logwealth_maxes_at_full_kelly():
    q, p = 0.6, 0.5
    fk = store.kelly_fraction(q, p)
    g_at = store.g_logwealth(fk, q, p)
    assert g_at >= store.g_logwealth(fk * 0.5, q, p)      # concave, peak at full Kelly
    assert g_at >= store.g_logwealth(min(0.99, fk * 1.5), q, p)


# --------------------------------------------------------------------------- trim / close


def test_close_books_realized_pl_and_status():
    con = _con()
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="m", selection="x",
                              resolution_basis="advance", entry_price=0.50, stake_usd=100.0)
    assert store.realized_balance(con) == 1900.0
    pl = store.close(con, bid, 0.60, "t2")               # 200 sh * .60 = 120 ; pl = +20
    assert round(pl, 6) == 20.0
    assert store.realized_balance(con) == 2020.0
    row = con.execute("SELECT status, settled_pl, exit_price FROM paper_bets WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "closed" and round(row["settled_pl"], 6) == 20.0


def test_trim_reduces_stake_in_place():
    con = _con()
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="m", selection="x",
                              resolution_basis="advance", entry_price=0.50, stake_usd=100.0)
    realized = store.trim(con, bid, 100.0, 0.60, "t2")   # sell 100 of 200 sh @ .60
    assert round(realized, 6) == 10.0                     # 60 - 100*.5
    row = con.execute("SELECT status, stake_usd FROM paper_bets WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "open" and round(row["stake_usd"], 6) == 50.0   # 100 sh * .5
    assert store.realized_balance(con) == 1960.0          # 1900 + 60 proceeds


def test_trim_whole_position_routes_to_close():
    con = _con()
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="m", selection="x",
                              resolution_basis="advance", entry_price=0.50, stake_usd=100.0)
    store.trim(con, bid, 200.0, 0.55, "t2")
    assert con.execute("SELECT status FROM paper_bets WHERE id=?", (bid,)).fetchone()["status"] == "closed"


# --------------------------------------------------------------------------- log_decision scores


def test_log_decision_gog_zero_when_sized_at_target_and_cap_binds():
    con = _con()
    bid = store.log_paper_bet(con, ts_utc="t1", fixture="A vs B", market_type="m", selection="x",
                              resolution_basis="advance", entry_price=0.50, stake_usd=40.0)
    store.log_decision(con, action="add", rule="auto_scan", bet_id=bid, token_id="tok",
                       fixture="A vs B", resolution_basis="advance", q_t=0.60, q_source="entry_static",
                       p_t=0.50, p_mid_t=0.50, equity_t=2000.0, kelly_mult=0.5, max_stake_frac=0.02,
                       entry_price=0.50, stake_before=0.0, stake_after=40.0, shares_delta=80.0, ts_utc="t1")
    r = con.execute("SELECT gog, f_target, cap_binding, delta_g FROM decision_events WHERE paper_bet_id=?",
                    (bid,)).fetchone()
    assert abs(r["gog"]) < 1e-9 and round(r["f_target"], 6) == 0.02   # stake 40/2000 = 0.02 = target
    assert r["cap_binding"] == 1 and abs(r["delta_g"]) < 1e-9


# --------------------------------------------------------------------------- exit rules


def test_eval_exit_rules_edge_flip_closes():
    d = trader.eval_exit_rules(q_t=0.40, p_bid=0.50, p_mid=0.50, spread=0.01, depth=100,
                               shares=200, equity=2000, kelly_mult=0.5, max_stake_frac=0.02)
    assert d[0] == "edge_flip_close" and d[1] == "close"


def test_eval_exit_rules_liquidity_then_over_kelly():
    # wide spread -> liquidity close (R3) wins over R2
    d = trader.eval_exit_rules(q_t=0.70, p_bid=0.50, p_mid=0.55, spread=0.15, depth=100,
                               shares=200, equity=2000, kelly_mult=0.5, max_stake_frac=0.02, spread_cap=0.10)
    assert d[0] == "liquidity_exit"
    # tight spread but over-Kelly -> trim back to target
    d2 = trader.eval_exit_rules(q_t=0.70, p_bid=0.50, p_mid=0.50, spread=0.01, depth=100,
                                shares=200, equity=2000, kelly_mult=0.5, max_stake_frac=0.02)
    assert d2[0] == "over_kelly_trim" and d2[1] == "trim"
    assert abs(d2[2] - (200 - 0.02 * 2000 / 0.50)) < 1e-6   # sell down to f_target shares (80)


def test_eval_exit_rules_none_when_well_sized():
    assert trader.eval_exit_rules(q_t=0.55, p_bid=0.50, p_mid=0.50, spread=0.01, depth=100,
                                  shares=40, equity=2000, kelly_mult=0.5, max_stake_frac=0.02) is None


# --------------------------------------------------------------------------- backfill + rollups


def _seed_add(con, *, q, p, shares, basis="totals", fixture="France vs Sweden", sel="Over 2.5"):
    bid = store.log_paper_bet(con, ts_utc="t1", fixture=fixture, market_type="m", selection=sel,
                              resolution_basis=basis, entry_price=p, stake_usd=round(shares * p, 6))
    store.log_decision(con, action="add", rule="auto_scan", bet_id=bid, token_id="tok",
                       fixture=fixture, resolution_basis=basis, q_t=q, q_source="entry_static",
                       p_t=p, p_mid_t=p, equity_t=2000.0, kelly_mult=0.5, max_stake_frac=0.02,
                       entry_price=p, stake_before=0.0, stake_after=round(shares * p, 6),
                       shares_delta=shares, ts_utc="t1")
    return bid


def test_backfill_and_calibration_gap_equals_v_minus_q():
    con = _con()
    # France 1-1 Sweden -> Over 2.5 LOST (2 goals). model_q said 0.60 -> over-predicted.
    _seed_add(con, q=0.60, p=0.50, shares=200, basis="totals", sel="Over 2.5")
    results = {frozenset({"France", "Sweden"}): ("2026-07-01", "France", 1, 1)}
    n = settle.backfill_decision_outcomes(con, results, ts_utc="t2")
    assert n == 1
    row = con.execute("SELECT settled_outcome, realized_regret, delta_ev FROM decision_events").fetchone()
    assert row["settled_outcome"] == "lost"
    # add: realized = sh*(v-p)=200*(0-.5)=-100 ; model_ev = sh*(q-p)=200*(.1)=20
    assert round(row["realized_regret"], 6) == -100.0 and round(row["delta_ev"], 6) == 20.0
    calib = settle.calibration_rollup(con)
    gap = calib["by_basis"]["totals"]["ev_calibration_gap"]
    assert round(gap, 6) == round((-100.0 - 20.0) / 200.0, 6) == -0.6   # (v - q) = 0 - 0.6


def test_process_rollup_groups_by_basis_and_qsource():
    con = _con()
    _seed_add(con, q=0.60, p=0.50, shares=80, basis="advance")
    proc = settle.process_rollup(con)
    assert "advance" in proc and "entry_static" in proc["advance"]
    assert proc["advance"]["entry_static"]["n"] == 1
