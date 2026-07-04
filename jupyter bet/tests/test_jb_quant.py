"""Quant tests: implied prob / de-vig, fair value, look-ahead prevention,
time bucketing, matching gates, arb math, promo EV, stress gates, and
decision parity between lib wrappers and production functions.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import polars as pl
import pytest

import lib.arbpromo as ap
import lib.convergence as cv
import lib.fairvalue as fv
import lib.ids as ids
import lib.matching as mt
import lib.pmdata as pm
import lib.stress as sx
from lib.config import Params

UTC = dt.timezone.utc


# ------------------------------------------------- implied prob / devig ----

def test_implied_probs_and_overround():
    probs = fv.implied_probs([2.0, 4.0, 4.0])
    assert np.allclose(probs, [0.5, 0.25, 0.25])
    assert fv.overround([1.8, 3.6, 4.5]) == pytest.approx(
        1 / 1.8 + 1 / 3.6 + 1 / 4.5 - 1)


@pytest.mark.parametrize("method", ["shin", "multiplicative", "power"])
def test_devig_sums_to_one_and_orders_preserved(method):
    p = fv.devig([1.8, 3.6, 4.5], method)
    assert p.sum() == pytest.approx(1.0, abs=1e-9)
    assert p[0] > p[1] > p[2]


def test_devig_unknown_method_raises():
    with pytest.raises(ValueError):
        fv.devig([2.0, 2.0], "vibes")


def test_devig_matches_production_directly():
    """lib.fairvalue.devig IS wca.markets.devig — spot-check dispatch."""
    from wca.markets import devig as prod
    odds = [2.1, 3.3, 3.9]
    assert np.allclose(fv.devig(odds, "shin"), prod.shin(odds))
    assert np.allclose(fv.devig(odds, "multiplicative"), prod.multiplicative(odds))


# ------------------------------------------------------------ fair value ----

def _quote():
    return {"best_bid": 0.40, "best_ask": 0.44, "bid_sz_top": 300.0,
            "ask_sz_top": 100.0}


def test_fv_mid_and_microprice():
    assert fv.fair_value("pm_mid", quote=_quote()) == pytest.approx(0.42)
    # microprice leans toward the heavier side's opposite quote:
    # (bid*ask_sz + ask*bid_sz)/(bid_sz+ask_sz) = (.40*100+.44*300)/400 = .43
    assert fv.fair_value("microprice", quote=_quote()) == pytest.approx(0.43)


def test_fv_last_trade_respects_asof_cutoff():
    trades = pl.DataFrame({"ts": [100, 200, 300],
                           "price": [0.40, 0.45, 0.60],
                           "size": [10.0, 10.0, 10.0]})
    assert fv.fair_value("last_trade", trades=trades, asof_ts=250) == 0.45
    assert fv.fair_value("last_trade", trades=trades) == 0.60


def test_fv_vwap_window_and_lookahead():
    trades = pl.DataFrame({"ts": [0, 1800, 3500, 7200],
                           "price": [0.30, 0.40, 0.50, 0.90],
                           "size": [10.0, 10.0, 10.0, 10.0]})
    v = fv.fair_value("vwap_1h", trades=trades, asof_ts=3600)
    assert v == pytest.approx((0.40 + 0.50) / 2)  # only trades in (0, 3600]
    assert 0.90 not in (v,), "post-asof trade must never contaminate VWAP"


def test_fv_book_devig_and_model():
    v = fv.fair_value("book_devig", book_odds=[1.8, 3.6, 4.5], book_index=0,
                      devig_method="multiplicative")
    assert v == pytest.approx(fv.devig([1.8, 3.6, 4.5], "multiplicative")[0])
    assert fv.fair_value("model", model_prob=0.61) == 0.61


def test_fv_missing_inputs_return_none_never_guess():
    assert fv.fair_value("pm_mid", quote=None) is None
    assert fv.fair_value("vwap_1h", trades=pl.DataFrame(
        {"ts": [], "price": [], "size": []})) is None


def test_closing_is_not_a_fair_value_method():
    with pytest.raises(ValueError):
        fv.fair_value("closing")


# ---------------------------------------------------- costs, EV, sizing ----

def test_pm_fee_shape_and_ev():
    assert fv.pm_fee(0.5, 0.03) == pytest.approx(0.0075)
    assert fv.pm_fee(0.5, 0.0) == 0.0
    # EV: fair 0.55 buying at 0.50 no fee → 0.55/0.5 − 1 = 10%
    assert fv.ev_per_dollar(0.55, 0.50) == pytest.approx(0.10, abs=1e-6)


def test_kelly_stake_matches_production():
    from wca_betrecs import _kelly_stake
    stake = fv.kelly_stake(0.55, 0.50, 1000.0, fraction=0.25, cap_frac=0.10)
    assert stake == _kelly_stake(0.55, 2.0, 1000.0, fraction=0.25, cap=0.10)
    assert stake > 0


def test_kelly_zero_when_no_edge():
    assert fv.kelly_stake(0.45, 0.50, 1000.0) == 0.0


def test_walk_book_depth_limits():
    levels = [{"price": 0.50, "size": 100}, {"price": 0.55, "size": 100}]
    fill = fv.walk_book(levels, 50.0)
    assert fill["avg_px"] == pytest.approx(0.50)
    big = fv.walk_book(levels, 100.0)   # crosses into the 0.55 level
    assert 0.50 < big["avg_px"] < 0.55 and big["worst_px"] == 0.55
    assert fv.walk_book(levels, 1000.0) is None, "insufficient depth must be None"


# ------------------------------------------------ time buckets/look-ahead ----

def test_mark_times_exact():
    ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    m = cv.mark_times(ko, (48, 24, 0))
    assert m[0] == ko
    assert m[48] == ko - dt.timedelta(hours=48)


def test_value_at_mark_observed_vs_reconstructed_vs_unavailable():
    ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    ts = [ko - dt.timedelta(hours=49), ko - dt.timedelta(hours=47)]
    s = pl.DataFrame({"ts_utc": ts, "price": [0.40, 0.50]})
    mark = ko - dt.timedelta(hours=48)
    r = cv.value_at_mark(s, mark, tolerance_min=30)
    assert r["basis"] == "reconstructed"          # 1h-away obs, bracketing pair
    assert r["value"] == pytest.approx(0.45)      # linear midpoint
    r2 = cv.value_at_mark(s, mark, tolerance_min=90)
    assert r2["basis"] == "observed" and r2["value"] == 0.40
    empty = pl.DataFrame({"ts_utc": [], "price": []},
                         schema={"ts_utc": pl.Datetime("us", "UTC"),
                                 "price": pl.Float64})
    assert cv.value_at_mark(empty, mark)["basis"] == "unavailable"


def test_value_at_mark_never_uses_future_only():
    ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    s = pl.DataFrame({"ts_utc": [ko + dt.timedelta(hours=1)], "price": [0.99]})
    r = cv.value_at_mark(s, ko, tolerance_min=600)
    assert r["basis"] == "unavailable", "future-only data must not backfill a mark"


def test_guard_lookahead_filters():
    ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    df = pl.DataFrame({"ts_utc": [ko - dt.timedelta(1), ko + dt.timedelta(1)],
                       "price": [0.4, 0.9]}).lazy()
    out = cv.guard_lookahead(df, ko).collect()
    assert out.height == 1 and out["price"][0] == 0.4


def test_convergence_expost_column_gated():
    ko = dt.datetime(2026, 7, 6, 19, 0, tzinfo=UTC)
    s = pl.DataFrame({"ts_utc": [ko - dt.timedelta(hours=1)], "price": [0.5]})
    t = cv.convergence_table(cv.mark_times(ko, (24, 0)), s, closing_expost=0.55,
                             allow_expost=False)
    assert "closing_price_expost" not in t.columns
    t2 = cv.convergence_table(cv.mark_times(ko, (0,)), s, closing_expost=0.55,
                              allow_expost=True, tolerance_min=90)
    assert "closing_price_expost" in t2.columns


# --------------------------------------------------------------- matching ----

def _mk(home, away, ko_h, mtype="1x2", settlement=ids.S_90MIN, line=None,
        src="a", mid="m1"):
    return {"home": home, "away": away,
            "kickoff_utc": dt.datetime(2026, 7, 6, ko_h, 0, tzinfo=UTC),
            "market_type": mtype, "line": line, "period": "FT",
            "settlement": settlement, "source": src, "source_market_id": mid}


def test_match_accepts_full_agreement_with_alias_teams():
    s = mt.score_match(_mk("Korea Republic", "Czechia", 17),
                       _mk("South Korea", "Czech Republic", 18))
    assert s["verdict"] == "accepted" and s["confidence"] == 1.0


def test_match_rejects_settlement_mismatch_pm_advancement_vs_1x2():
    a = _mk("Spain", "Austria", 17, mtype="1x2", settlement=ids.S_90MIN)
    b = _mk("Spain", "Austria", 17, mtype="1x2", settlement=ids.S_ETPENS)
    s = mt.score_match(a, b)
    assert s["verdict"] == "rejected"
    assert any("NEVER" in r for r in s["reasons"])


def test_match_rejects_kickoff_beyond_tolerance():
    s = mt.score_match(_mk("Spain", "Austria", 10), _mk("Spain", "Austria", 20),
                       kickoff_tolerance_h=3.0)
    assert s["verdict"] == "rejected"
    assert any("kickoff" in r for r in s["reasons"])


def test_match_rejects_different_lines():
    a = _mk("Spain", "Austria", 17, mtype="totals", line=2.5)
    b = _mk("Spain", "Austria", 17, mtype="totals", line=3.5)
    assert mt.score_match(a, b)["verdict"] == "rejected"


def test_match_never_matches_on_names_alone():
    """Same names, everything else wrong → rejected (fuzzy-name ban)."""
    a = _mk("Spain", "Austria", 10, mtype="1x2")
    b = _mk("Spain", "Austria", 23, mtype="totals", line=2.5,
            settlement=ids.S_ETPENS)
    assert mt.score_match(a, b)["verdict"] == "rejected"


def test_match_frames_emits_verdicts(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "OVERRIDES_PATH", tmp_path / "none.yaml")
    fa = pl.DataFrame([_mk("Spain", "Austria", 17, src="polymarket", mid="pm1")])
    fb = pl.DataFrame([_mk("Spain", "Austria", 17, src="theoddsapi", mid="oa1"),
                       _mk("Spain", "Austria", 17, mtype="totals", line=2.5,
                           src="theoddsapi", mid="oa2")])
    out = mt.match_frames(fa, fb)
    v = dict(zip(out["b_market_id"], out["verdict"]))
    assert v == {"oa1": "accepted", "oa2": "rejected"}


def test_manual_override_wins(tmp_path, monkeypatch):
    ov = tmp_path / "overrides.yaml"
    ov.write_text("pairs:\n  - a: pm1\n    b: oa1\n    verdict: reject\n")
    monkeypatch.setattr(mt, "OVERRIDES_PATH", ov)
    fa = pl.DataFrame([_mk("Spain", "Austria", 17, src="polymarket", mid="pm1")])
    fb = pl.DataFrame([_mk("Spain", "Austria", 17, src="theoddsapi", mid="oa1")])
    out = mt.match_frames(fa, fb)
    assert out["verdict"].to_list() == ["rejected"]
    assert "manual_override" in out["reasons"][0]


def test_pm_match_event_assembly_real_shapes():
    rows = [
        {"condition_id": "0xH", "event_slug": "fifwc-esp-aut-2026-07-02",
         "market_slug": "fifwc-esp-aut-2026-07-02-esp",
         "question": "Will Spain win on 2026-07-02?", "category": "match_1x2",
         "game_start_time": "2026-07-02 19:00:00+00"},
        {"condition_id": "0xA", "event_slug": "fifwc-esp-aut-2026-07-02",
         "market_slug": "fifwc-esp-aut-2026-07-02-aut",
         "question": "Will Austria win on 2026-07-02?", "category": "match_1x2",
         "game_start_time": "2026-07-02 19:00:00+00"},
        {"condition_id": "0xD", "event_slug": "fifwc-esp-aut-2026-07-02",
         "market_slug": "fifwc-esp-aut-2026-07-02-draw",
         "question": "Will Spain vs Austria end in a draw?",
         "category": "match_1x2",
         "game_start_time": "2026-07-02 19:00:00+00"},
    ]
    ev = mt.pm_match_events(pl.DataFrame(rows))
    assert ev.height == 1
    r = ev.to_dicts()[0]
    assert (r["home"], r["away"]) == ("Spain", "Austria")
    assert (r["cid_home"], r["cid_away"], r["cid_draw"]) == ("0xH", "0xA", "0xD")
    assert r["kickoff_utc"] == dt.datetime(2026, 7, 2, 19, 0, tzinfo=UTC)
    canon = mt.pm_canonical_matches(pl.DataFrame(rows))
    assert canon["settlement"].to_list() == [ids.S_90MIN]


def test_classify_pm_market_prefers_production_category():
    assert mt.classify_pm_market({"category": "advancement_qf"}) == "advance"
    assert mt.classify_pm_market({"category": "winner"}) == "outright"
    assert mt.classify_pm_market(
        {"category": "", "event_slug": "fifwc-arg-alg-2026-06-16",
         "question": "Will Argentina win on 2026-06-16?"}) == "match_1x2"


# --------------------------------------------------------------- book math ----

def test_book_metrics_from_clob_shape():
    book = {"bids": [{"price": "0.40", "size": "300"},
                     {"price": "0.38", "size": "500"}],
            "asks": [{"price": "0.44", "size": "100"},
                     {"price": "0.47", "size": "200"}]}
    m = pm.book_metrics(book)
    assert m["best_bid"] == 0.40 and m["best_ask"] == 0.44
    assert m["mid"] == pytest.approx(0.42)
    assert m["spread"] == pytest.approx(0.04)
    assert m["microprice"] == pytest.approx((0.40 * 100 + 0.44 * 300) / 400)
    assert m["depth_bid_5c_usd"] == pytest.approx(0.40 * 300 + 0.38 * 500)
    assert m["depth_ask_5c_usd"] == pytest.approx(0.44 * 100 + 0.47 * 200)


# -------------------------------------------------------------------- arb ----

def test_arb_exchange_pm_detects_real_arb_and_rounding_erosion():
    # exchange back 2.40 at 6% commission → net 2.316 (1/2.316 = .4318);
    # PM YES on the complement at 0.42 → net decimal 2.381 (1/2.381 = .42);
    # inverse sum .852 < 1 ⇒ genuine lock per production evaluate_pair.
    r = ap.arb_exchange_pm(exchange_odds=2.40, pm_yes_price=0.42,
                           fx_usd_per_gbp=1.33)
    assert r["is_arb"], "production evaluate_pair should confirm this lock"
    assert r["locked_profit_gbp_after_rounding"] > 0
    # rounding + FX haircut must never IMPROVE on the pre-rounding lock
    assert r["roi_after_rounding"] <= r["guaranteed_pct_pre_rounding"] + 1e-6
    assert r["verdict"] == "EXECUTABLE arb"


def test_arb_exchange_pm_rejects_no_arb():
    # 1.90/0.55: inverse sum ≈ 1.09 → production returns None
    r = ap.arb_exchange_pm(exchange_odds=1.90, pm_yes_price=0.55,
                           fx_usd_per_gbp=1.33)
    assert not r["is_arb"]
    assert "no arb" in r["verdict"]


def test_stake_rounding_conservative():
    assert ap.stake_rounding(10.07, "smarkets") == pytest.approx(10.05)
    assert ap.stake_rounding(10.07, "polymarket") == pytest.approx(10.07)


# ------------------------------------------------------------------ promo ----

def test_promo_incomplete_terms_not_executable():
    t = ap.PromoTerms("mystery boost", "bet365", "profit_boost", max_stake=10.0)
    r = ap.promo_ev(t, back_odds=2.0, lay_odds=2.05)
    assert r["executable"] is False and "incomplete" in r["reason"]


def test_promo_qualifying_floor_enforced():
    t = ap.PromoTerms("acca boost", "bet365", "free_bet", max_stake=10.0,
                      qualifying_min_odds=2.0, stake_returned=False,
                      freebet_amount=10.0)
    r = ap.promo_ev(t, back_odds=1.8, lay_odds=1.85)
    assert r["executable"] is False and "qualifying" in r["reason"]


def test_promo_free_bet_ev_positive_when_terms_good():
    t = ap.PromoTerms("bet10 get10", "bet365", "free_bet", max_stake=10.0,
                      qualifying_min_odds=1.5, stake_returned=False,
                      freebet_amount=10.0, jurisdiction_ok=True)
    r = ap.promo_ev(t, back_odds=2.0, lay_odds=2.04, freebet_conversion=0.7)
    assert r["executable"] and r["ev"] > 4.0, \
        "tight matched free-bet extraction should clear +£4 on £10"


def test_promo_outside_jurisdiction_blocked():
    t = ap.PromoTerms("us only", "fanduel", "free_bet", max_stake=10.0,
                      qualifying_min_odds=1.5, stake_returned=False,
                      freebet_amount=10.0, jurisdiction_ok=False)
    assert ap.promo_ev(t, back_odds=2.0, lay_odds=2.04)["executable"] is False


# ----------------------------------------------------------------- stress ----

def _cand(**kw):
    base = {"candidate_id": "c1", "event_id": "e", "market_type": "1x2",
            "outcome": "home", "fair_p": 0.55, "mid": 0.50, "spread": 0.02,
            "depth_usd": 500.0, "staleness_s": 60.0, "match_confidence": 1.0,
            "settled": False, "won": None}
    base.update(kw)
    return base


def test_stress_gates_fire_individually():
    p = Params()
    assert sx.decide_one(_cand(), p, 1000.0)["accepted"]
    assert "spread" in sx.decide_one(_cand(spread=0.5), p, 1000.0)["fails"]
    assert "staleness" in sx.decide_one(_cand(staleness_s=1e6), p, 1000.0)["fails"]
    assert "edge_raw" in sx.decide_one(_cand(fair_p=0.505), p, 1000.0)["fails"]
    assert "depth" in sx.decide_one(_cand(depth_usd=1.0), p, 1000.0)["fails"]
    assert "confidence" in sx.decide_one(
        _cand(match_confidence=0.5), p, 1000.0)["fails"]


def test_stress_stake_respects_hard_cap():
    p = Params(stake_cap_usd=25.0)
    d = sx.decide_one(_cand(fair_p=0.80, mid=0.50), p, 100000.0)
    assert d["accepted"] and d["stake"] <= 25.0


def test_stress_evaluate_reports_only_settled_metrics():
    df = pl.DataFrame([_cand(candidate_id="a", settled=True, won=True),
                       _cand(candidate_id="b", settled=False, won=None)])
    r = sx.evaluate(df, Params(), 1000.0)
    assert r["n_candidates"] == 2 and r["settled_n"] == 1
    assert r["brier"] == pytest.approx((0.55 - 1.0) ** 2, abs=1e-6)
    assert r["realized_pnl"] is not None


def test_stress_sweep_monotone_edge_gate():
    cands = pl.DataFrame([_cand(candidate_id=str(i), fair_p=0.50 + i / 100)
                          for i in range(1, 9)])
    out = sx.sweep_one(cands, Params(), "min_edge_raw", [0.01, 0.03, 0.05])
    acc = out["n_accepted"].to_list()
    assert acc[0] >= acc[1] >= acc[2], "tightening a gate can only reduce accepts"


def test_stress_grid_covers_cartesian():
    cands = pl.DataFrame([_cand()])
    out = sx.sweep_grid(cands, Params(), {"min_edge_raw": [0.01, 0.05],
                                          "max_spread": [0.01, 0.10]})
    assert out.height == 4
