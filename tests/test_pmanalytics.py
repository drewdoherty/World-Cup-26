"""Unit tests for the pure logic of :mod:`wca.pmanalytics`.

Network-free: all inputs are small hand-built fixtures so the calibration-edge
maths, the term-structure (ladder + advance-vs-FT) violation detection, and the
mark-to-market P&L are verified deterministically.
"""

from __future__ import annotations

import math

import pytest

from wca import pmanalytics as pa


# --------------------------------------------------------------------------- #
# 1. Calibration & edge                                                       #
# --------------------------------------------------------------------------- #


def test_edge_row_signed_edge():
    r = pa.EdgeRow("btts", "X vs Y", "BTTS Yes", model_prob=0.55, pm_price=0.40)
    assert r.edge == pytest.approx(0.15)
    assert r.abs_edge == pytest.approx(0.15)

    r2 = pa.EdgeRow("match_result", "A vs B", "A win", model_prob=0.30, pm_price=0.45)
    assert r2.edge == pytest.approx(-0.15)
    assert r2.abs_edge == pytest.approx(0.15)


def test_build_edge_rows_skips_bad_probs():
    obs = [
        {"category": "btts", "subject": "f1", "label": "Yes", "model_prob": 0.5, "pm_price": 0.4},
        {"category": "btts", "subject": "f2", "label": "Yes", "model_prob": 1.4, "pm_price": 0.4},  # out of range
        {"category": "btts", "subject": "f3", "label": "Yes", "model_prob": None, "pm_price": 0.4}, # missing
        {"category": "btts", "subject": "f4", "label": "Yes", "model_prob": 0.5, "pm_price": "x"},  # bad price
    ]
    rows = pa.build_edge_rows(obs)
    assert len(rows) == 1
    assert rows[0].subject == "f1"


def test_aggregate_category_bias_systematic_high():
    # Every BTTS row has model above PM by +0.10 -> mean_edge +0.10, all model-high.
    rows = [
        pa.EdgeRow("btts", str(i), "Yes", 0.5 + 0.1, 0.5) for i in range(4)
    ]
    # match_result rows are unbiased (mean ~0): +0.1 and -0.1 cancel.
    rows += [
        pa.EdgeRow("match_result", "a", "w", 0.6, 0.5),
        pa.EdgeRow("match_result", "b", "w", 0.4, 0.5),
    ]
    agg = pa.aggregate_category_bias(rows)

    btts = agg["btts"]
    assert btts["n"] == 4
    assert btts["mean_edge"] == pytest.approx(0.10)
    assert btts["median_edge"] == pytest.approx(0.10)
    assert btts["frac_model_high"] == pytest.approx(1.0)
    assert btts["rmse"] == pytest.approx(0.10)

    mr = agg["match_result"]
    assert mr["mean_edge"] == pytest.approx(0.0)
    assert mr["mean_abs_edge"] == pytest.approx(0.10)
    assert mr["frac_model_high"] == pytest.approx(0.5)


def test_calibration_summary_top_edges_sorted():
    rows = [
        pa.EdgeRow("btts", "small", "Yes", 0.50, 0.48),    # |edge| 0.02
        pa.EdgeRow("advance", "big", "R16", 0.90, 0.40),   # |edge| 0.50
        pa.EdgeRow("exact_score", "mid", "1-0", 0.20, 0.30),  # |edge| 0.10
    ]
    summ = pa.calibration_summary(rows)
    assert summ["n_rows"] == 3
    top = summ["top_edges"]
    assert [t["subject"] for t in top] == ["big", "mid", "small"]
    assert set(summ["by_category"]) == {"btts", "advance", "exact_score"}


def test_degenerate_and_filter_live():
    pinned_lo = pa.EdgeRow("advance", "dead-through", "R16", 0.99, 0.001)
    pinned_hi = pa.EdgeRow("advance", "dead-out", "win", 0.0, 0.995)
    live = pa.EdgeRow("advance", "live", "QF", 0.50, 0.45)
    assert pa.is_degenerate(pinned_lo)
    assert pa.is_degenerate(pinned_hi)
    assert not pa.is_degenerate(live)
    kept = pa.filter_live([pinned_lo, pinned_hi, live])
    assert [r.subject for r in kept] == ["live"]


def test_calibration_summary_live_excludes_pinned():
    rows = [
        # genuine BTTS bias +0.10
        pa.EdgeRow("btts", "a", "Yes", 0.60, 0.50),
        pa.EdgeRow("btts", "b", "Yes", 0.60, 0.50),
        # a dead advance market that would otherwise show a +0.9 edge
        pa.EdgeRow("advance", "dead", "R16", 0.90, 0.001),
    ]
    summ = pa.calibration_summary(rows)
    assert summ["n_rows"] == 3
    assert summ["n_rows_live"] == 2
    # raw includes the dead advance row...
    assert "advance" in summ["by_category"]
    # ...but the live view drops it entirely.
    assert "advance" not in summ["by_category_live"]
    assert summ["by_category_live"]["btts"]["mean_edge"] == pytest.approx(0.10)
    # top edges come from live rows only -> no +0.9 dead-market edge.
    assert all(t["category"] == "btts" for t in summ["top_edges"])


# --------------------------------------------------------------------------- #
# 2. Term-structure consistency                                               #
# --------------------------------------------------------------------------- #


def test_ladder_monotonic_clean():
    probs = {"R16": 0.9, "QF": 0.7, "SF": 0.5, "Final": 0.3, "win": 0.18}
    assert pa.check_ladder_monotonic("Argentina", probs, source="model") == []


def test_ladder_monotonic_violation_detected():
    # SF reach-prob exceeds QF reach-prob -> impossible (must be <=).
    probs = {"R16": 0.9, "QF": 0.5, "SF": 0.6, "Final": 0.3, "win": 0.18}
    viols = pa.check_ladder_monotonic("Foo", probs, source="model")
    assert len(viols) == 1
    v = viols[0]
    assert (v.stage_hi, v.stage_lo) == ("QF", "SF")
    assert v.prob_hi == pytest.approx(0.5)
    assert v.prob_lo == pytest.approx(0.6)
    assert v.gap == pytest.approx(0.1)


def test_ladder_skips_missing_stages():
    # Only R16, SF, win present -> compares R16>=SF and SF>=win (consecutive present).
    probs = {"R16": 0.8, "SF": 0.4, "win": 0.5}  # win > SF is the violation
    viols = pa.check_ladder_monotonic("Bar", probs, source="pm")
    assert len(viols) == 1
    assert (viols[0].stage_hi, viols[0].stage_lo) == ("SF", "win")


def test_ladder_violations_both_sources():
    teams = [
        {
            "team": "CleanModelBadPM",
            "model": {"R16": 0.9, "QF": 0.7, "SF": 0.5, "Final": 0.3, "win": 0.1},
            # PM Final price > SF price -> arb signal
            "pm": {"R16": {"pm": 0.9}, "QF": {"pm": 0.7}, "SF": {"pm": 0.4},
                   "Final": {"pm": 0.5}, "win": {"pm": 0.1}},
        },
    ]
    viols = pa.ladder_violations(teams)
    assert len(viols) == 1
    v = viols[0]
    assert v.source == "pm"
    assert (v.stage_hi, v.stage_lo) == ("SF", "Final")


def test_advance_vs_ft_flag():
    # FT win in 90' (0.55) cannot exceed advance-past-the-match prob (0.50).
    flag = pa.check_advance_vs_ft("Spain", advance_prob=0.50, ft_win_prob=0.55, source="model")
    assert flag is not None
    assert flag.gap == pytest.approx(0.05)
    # Consistent case: advance >= ft win -> no flag.
    assert pa.check_advance_vs_ft("Spain", 0.60, 0.55, source="model") is None
    # Equality (within tol) -> no flag.
    assert pa.check_advance_vs_ft("Spain", 0.55, 0.55, source="model") is None


def test_term_structure_report_bundles():
    teams = [
        {"team": "T", "model": {"R16": 0.5, "QF": 0.6}, "pm": {}},  # QF>R16 violation
    ]
    cross = [{"team": "T", "source": "model", "advance_prob": 0.4, "ft_win_prob": 0.5}]
    rep = pa.term_structure_report(teams, cross)
    assert rep["n_ladder_violations"] == 1
    assert rep["n_advance_vs_ft_flags"] == 1


# --------------------------------------------------------------------------- #
# 3. Mark-to-market                                                           #
# --------------------------------------------------------------------------- #


def test_shares_from_stake_entry_price():
    # $20 stake at 0.20 entry -> 100 shares.
    assert pa.shares_from_stake(20.0, 0.20, None) == pytest.approx(100.0)


def test_shares_from_stake_decimal_odds():
    # 5.00 decimal odds, £10 stake -> winning return £50 = 50 unit-payout shares.
    assert pa.shares_from_stake(10.0, None, 5.0) == pytest.approx(50.0)
    # entry_price preferred when both present.
    assert pa.shares_from_stake(10.0, 0.5, 5.0) == pytest.approx(20.0)
    assert pa.shares_from_stake(10.0, None, None) is None


def test_mark_to_market_gain_and_loss():
    # Bought 100 shares @0.20 ($20), now 0.30 -> value $30, P&L +$10.
    assert pa.mark_to_market(20.0, 0.30, entry_price=0.20) == pytest.approx(10.0)
    # Now 0.10 -> value $10, P&L -$10.
    assert pa.mark_to_market(20.0, 0.10, entry_price=0.20) == pytest.approx(-10.0)
    # Mark equals entry -> flat.
    assert pa.mark_to_market(20.0, 0.20, entry_price=0.20) == pytest.approx(0.0)
    # No usable share count -> None.
    assert pa.mark_to_market(20.0, 0.30) is None
    # Out-of-range mark -> None.
    assert pa.mark_to_market(20.0, 1.5, entry_price=0.2) is None


def test_mark_to_market_decimal_odds_basis():
    # £10 @ 5.0 -> 50 shares (implied entry 0.20). Mark at 0.40 -> value £20, P&L +£10.
    pl = pa.mark_to_market(10.0, 0.40, decimal_odds=5.0)
    assert pl == pytest.approx(10.0)


def test_mark_position_and_unmarked():
    pos = {
        "book": "paper", "bet_id": 1, "fixture": "X vs Y", "market": "btts",
        "selection": "BTTS Yes", "resolution_basis": "btts", "token_id": "abc",
        "stake": 40.0, "currency": "USD", "entry_price": 0.20, "decimal_odds": None,
    }
    m = pa.mark_position(pos, 0.30)
    assert m.shares == pytest.approx(200.0)
    assert m.unrealized_pl == pytest.approx(20.0)  # 200*0.30-40
    # No mark -> unrealized None but shares still derived.
    m2 = pa.mark_position(pos, None)
    assert m2.mark_price is None
    assert m2.unrealized_pl is None
    assert m2.shares == pytest.approx(200.0)


def test_mtm_totals_splits_by_book_basis_currency():
    positions = [
        # paper USD, BTTS, +20
        {"book": "paper", "bet_id": 1, "stake": 40.0, "currency": "USD",
         "entry_price": 0.20, "resolution_basis": "btts"},
        # paper USD, advance, -10
        {"book": "paper", "bet_id": 2, "stake": 20.0, "currency": "USD",
         "entry_price": 0.40, "resolution_basis": "advance"},
        # real GBP, advance, decimal-odds, unmarked (no price)
        {"book": "real", "bet_id": 3, "stake": 60.0, "currency": "GBP",
         "decimal_odds": 1.6667, "resolution_basis": "advance"},
    ]
    marked = [
        pa.mark_position(positions[0], 0.30),   # +20
        pa.mark_position(positions[1], 0.20),   # 50 shares*0.20=10 -20 = -10
        pa.mark_position(positions[2], None),   # unmarked
    ]
    tot = pa.mtm_totals(marked)
    assert tot["n_positions"] == 3

    usd = tot["overall"]["USD"]
    assert usd["n_marked"] == 2
    assert usd["unrealized_pl"] == pytest.approx(10.0)  # +20 -10
    assert usd["stake_marked"] == pytest.approx(60.0)

    gbp = tot["overall"]["GBP"]
    assert gbp["n_unmarked"] == 1
    assert gbp["n_marked"] == 0
    assert gbp["unrealized_pl"] == pytest.approx(0.0)

    # by_book keeps currencies separate
    assert tot["by_book"]["paper"]["USD"]["unrealized_pl"] == pytest.approx(10.0)
    assert tot["by_book"]["real"]["GBP"]["n_unmarked"] == 1

    # by_basis: advance has one marked USD (-10) and one unmarked GBP
    adv = tot["by_basis"]["advance"]
    assert adv["USD"]["unrealized_pl"] == pytest.approx(-10.0)
    assert adv["GBP"]["n_unmarked"] == 1


def test_mtm_totals_roi():
    positions = [
        {"book": "paper", "bet_id": 1, "stake": 20.0, "currency": "USD",
         "entry_price": 0.20, "resolution_basis": "btts"},  # +10 on 20 -> 50% ROI
    ]
    marked = [pa.mark_position(positions[0], 0.30)]
    tot = pa.mtm_totals(marked)
    assert tot["overall"]["USD"]["roi_pct"] == pytest.approx(50.0)


def test_pm_stage_prices_flatten():
    pm = {"R16": {"pm": 0.9, "edge_adj": 0.01}, "QF": {"pm": 0.7},
          "win": {"pm": 1.4}, "SF": {"pm": None}}  # win out of range, SF bad
    flat = pa._pm_stage_prices(pm)
    assert flat == {"R16": 0.9, "QF": 0.7}
