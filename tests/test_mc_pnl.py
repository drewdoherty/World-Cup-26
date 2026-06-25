"""Tests for wca.mc.pnl — open-book P&L distribution.

Offline & deterministic (numpy Generator seed=42). No network, no wall-clock
inside the library under test.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from wca.mc.pnl import (
    DEFAULT_FX_RATE,
    OpenPosition,
    build_risk_pnl,
    by_currency,
    distribution_stats,
    hard_floor,
    histogram,
    settle_vectorised,
    simulate_book,
    wilson,
)


# --------------------------------------------------------------------------- #
# Fixtures: a tiny hand-settleable book
# --------------------------------------------------------------------------- #
def _back(bet_id, odds, stake, p, cur="GBP", **kw):
    return OpenPosition(
        bet_id=bet_id,
        match_desc=kw.get("match_desc", f"A{bet_id} vs B{bet_id}"),
        selection=kw.get("selection", "X"),
        platform=kw.get("platform", "betfair_sportsbook"),
        currency=cur,
        decimal_odds=odds,
        stake=stake,
        p_win=p,
        p_source=kw.get("p_source", "model"),
        is_free=kw.get("is_free", False),
        is_lay=kw.get("is_lay", False),
        teams=kw.get("teams", []),
    )


# --------------------------------------------------------------------------- #
# Wilson edges
# --------------------------------------------------------------------------- #
def test_wilson_edges():
    assert wilson(0, 0) == (0.0, 1.0)  # total ignorance
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0.0 < hi < 1.0
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and 0.0 < lo < 1.0
    lo, hi = wilson(1, 1)
    assert 0.0 <= lo <= hi <= 1.0
    lo, hi = wilson(5, 10)
    assert lo < 0.5 < hi


# --------------------------------------------------------------------------- #
# Vectorised settle matches a hand-settled tiny book
# --------------------------------------------------------------------------- #
def test_settle_matches_hand_calc():
    pos = [
        _back(1, 2.0, 10.0, 0.5),  # back: win +10, lose -10
        _back(2, 3.0, 5.0, 0.4, is_free=True),  # free: win +10, lose 0
        _back(3, 5.0, 4.0, 0.2, is_lay=True),  # lay: win(sel lose) +4, lose -16
    ]
    # 3 sims with explicit win masks (win == bet graded a win)
    wins = np.array(
        [
            [True, True, True],
            [False, False, False],
            [True, False, True],
        ]
    )
    pnl = settle_vectorised(pos, wins)
    expected = np.array(
        [
            [10.0, 10.0, 4.0],
            [-10.0, 0.0, -16.0],
            [10.0, 0.0, 4.0],
        ]
    )
    assert np.allclose(pnl, expected), pnl

    # worst-case loss per position (hard floor input)
    assert pos[0].worst_case() == -10.0
    assert pos[1].worst_case() == 0.0
    assert pos[2].worst_case() == -16.0


def test_payoff_back_win_lose_formula():
    p = _back(1, 4.0, 25.0, 0.3)
    won = np.array([True, False])
    out = p.payoff(won)
    # win -> stake*(odds-1) = 25*3 = 75 ; lose -> -stake = -25
    assert out[0] == pytest.approx(75.0)
    assert out[1] == pytest.approx(-25.0)


# --------------------------------------------------------------------------- #
# CVaR95 never better than the worst tail / never better than VaR95
# --------------------------------------------------------------------------- #
def test_cvar_not_better_than_tail():
    rng = np.random.default_rng(7)
    book = rng.normal(0.0, 100.0, size=50_000)
    stats = distribution_stats(book)
    p5 = stats["p5"]
    var95 = stats["var95"]
    cvar95 = stats["cvar95"]
    # CVaR (mean of worst 5%) is a deeper loss than VaR (the 5th pct loss).
    assert cvar95 >= var95 - 1e-9
    # CVaR magnitude is not smaller than the magnitude of the very worst draw? No —
    # it's a mean of the tail, so it sits between VaR and the min loss.
    worst_loss_mag = -float(book.min())
    assert var95 <= cvar95 <= worst_loss_mag + 1e-6
    # And consistency with p5: the tail used is book <= p5.
    tail = book[book <= p5]
    assert cvar95 == pytest.approx(-tail.mean(), abs=1e-6)


def test_cvar_all_profit_floored_at_zero():
    book = np.full(1000, 50.0)  # always +50
    stats = distribution_stats(book)
    assert stats["var95"] == 0.0
    assert stats["cvar95"] == 0.0
    assert stats["p_book_down"] == 0.0


# --------------------------------------------------------------------------- #
# Currencies are NEVER summed except the disclosed FX view
# --------------------------------------------------------------------------- #
def test_currencies_faceted_never_summed():
    pos = [
        _back(1, 2.0, 100.0, 1.0, cur="GBP"),  # always win +100 GBP
        _back(2, 2.0, 100.0, 1.0, cur="USD"),  # always win +100 USD
    ]
    sims = simulate_book(pos, n_sims=200, seed=42, fx_rate=0.5)
    bc = by_currency(pos, sims)
    # faceted EV is native currency, untouched by FX
    assert bc["GBP"]["ev"] == pytest.approx(100.0)
    assert bc["USD"]["ev"] == pytest.approx(100.0)  # NOT 50
    assert bc["GBP"]["n"] == 1 and bc["USD"]["n"] == 1
    assert bc["GBP"]["open_stake"] == pytest.approx(100.0)
    assert bc["USD"]["open_stake"] == pytest.approx(100.0)
    # The ONLY place currencies combine: the GBP distribution view, FX-applied.
    # book = 100 GBP + 100 USD * 0.5 = 150
    assert sims["book_gbp"].mean() == pytest.approx(150.0)


def test_fx_view_uses_rate():
    pos = [_back(1, 2.0, 100.0, 1.0, cur="USD")]
    sims = simulate_book(pos, n_sims=100, seed=42, fx_rate=DEFAULT_FX_RATE)
    # always win +100 USD -> 100 * 0.79 = 79 GBP
    assert sims["book_gbp"].mean() == pytest.approx(100.0 * DEFAULT_FX_RATE)


# --------------------------------------------------------------------------- #
# Determinism under seed
# --------------------------------------------------------------------------- #
def test_deterministic_under_seed():
    pos = [
        _back(1, 2.5, 10.0, 0.5, cur="GBP"),
        _back(2, 1.8, 60.0, 0.6, cur="USD"),
        _back(3, 7.5, 10.0, 0.13, cur="GBP", is_free=True),
    ]
    a = simulate_book(pos, n_sims=5000, seed=42)["book_gbp"]
    b = simulate_book(pos, n_sims=5000, seed=42)["book_gbp"]
    assert np.array_equal(a, b)
    c = simulate_book(pos, n_sims=5000, seed=43)["book_gbp"]
    assert not np.array_equal(a, c)


# --------------------------------------------------------------------------- #
# Hard floor = deterministic sum of worst cases (in GBP view)
# --------------------------------------------------------------------------- #
def test_hard_floor():
    pos = [
        _back(1, 2.0, 10.0, 0.5, cur="GBP"),  # worst -10
        _back(2, 2.0, 20.0, 0.5, cur="USD"),  # worst -20 USD -> -10 GBP @0.5
        _back(3, 3.0, 5.0, 0.4, cur="GBP", is_free=True),  # worst 0
        _back(4, 5.0, 4.0, 0.2, cur="GBP", is_lay=True),  # worst -16
    ]
    hf = hard_floor(pos, fx_rate=0.5)
    assert hf == pytest.approx(-10.0 + (-20.0 * 0.5) + 0.0 + (-16.0))
    assert hf == pytest.approx(-36.0)
    # And the floor is never breached by any simulated draw.
    sims = simulate_book(pos, n_sims=2000, seed=42, fx_rate=0.5)
    assert sims["book_gbp"].min() >= hf - 1e-6


# --------------------------------------------------------------------------- #
# Histogram shape & coverage
# --------------------------------------------------------------------------- #
def test_histogram_covers_all_sims():
    pos = [_back(1, 2.0, 10.0, 0.5), _back(2, 3.0, 10.0, 0.4)]
    n = 4000
    sims = simulate_book(pos, n_sims=n, seed=42)
    hist = histogram(sims["book_gbp"])
    assert sum(h["count"] for h in hist) == n
    for h in hist:
        assert h["bin_hi"] >= h["bin_lo"]


# --------------------------------------------------------------------------- #
# Full feed schema (exact keys)
# --------------------------------------------------------------------------- #
def test_build_feed_schema_exact():
    pos = [
        _back(1, 2.5, 10.0, 0.5, cur="GBP", teams=["England", "Ghana"]),
        _back(2, 1.8, 60.0, 0.6, cur="USD"),
    ]
    res = build_risk_pnl(
        pos,
        generated="2026-06-25T00:00:00Z",
        fx_ts="2026-06-25T00:00:00Z",
        n_sims=3000,
    )
    feed = res.feed
    assert set(feed) == {
        "meta",
        "distribution_gbp",
        "by_currency",
        "histogram",
        "per_team",
        "note",
    }
    assert set(feed["meta"]) == {
        "generated",
        "n_sims",
        "n_open_positions",
        "fx_rate",
        "fx_ts",
        "fx_note",
    }
    assert set(feed["distribution_gbp"]) == {
        "mean",
        "median",
        "p5",
        "p25",
        "p75",
        "p95",
        "var95",
        "cvar95",
        "p_book_down",
        "hard_floor",
    }
    assert set(feed["by_currency"]) == {"GBP", "USD"}
    for cur in ("GBP", "USD"):
        assert set(feed["by_currency"][cur]) == {"n", "open_stake", "ev"}
    assert feed["meta"]["n_open_positions"] == 2
    assert feed["meta"]["n_sims"] == 3000
    assert feed["meta"]["fx_rate"] == DEFAULT_FX_RATE
    # n:0 currency segment is emitted, not dropped (only GBP+USD exist here)
    assert feed["by_currency"]["GBP"]["n"] == 1
    assert feed["by_currency"]["USD"]["n"] == 1
    # per_team emits the mapped team(s); unteamed bucket falls back to desc.
    teams = {r["team"] for r in feed["per_team"]}
    assert "England" in teams and "Ghana" in teams
    # histogram ~40 bins
    assert 1 <= len(feed["histogram"]) <= 40
    # JSON-serialisable
    import json

    json.loads(json.dumps(feed))


def test_empty_book_is_safe():
    res = build_risk_pnl([], generated="t", fx_ts="t", n_sims=100)
    feed = res.feed
    assert feed["meta"]["n_open_positions"] == 0
    d = feed["distribution_gbp"]
    assert d["mean"] == 0.0 and d["hard_floor"] == 0.0
    assert feed["by_currency"]["GBP"]["n"] == 0
    assert feed["by_currency"]["USD"]["n"] == 0
    # No positions => degenerate all-zero P&L; histogram still covers all sims.
    assert sum(h["count"] for h in feed["histogram"]) == 100


def test_insufficient_sample_flagged():
    pos = [_back(i, 2.0, 10.0, 0.5) for i in range(5)]
    res = build_risk_pnl(pos, generated="t", fx_ts="t", n_sims=1000)
    assert "INSUFFICIENT SAMPLE" in res.feed["note"]


def test_law_of_large_numbers_mean():
    # A single +EV back bet: analytic EV = p*stake*(o-1) - (1-p)*stake
    p, o, s = 0.5, 2.2, 10.0
    pos = [_back(1, o, s, p)]
    sims = simulate_book(pos, n_sims=200_000, seed=42)
    analytic = p * s * (o - 1) - (1 - p) * s
    assert sims["book_gbp"].mean() == pytest.approx(analytic, abs=0.2)
