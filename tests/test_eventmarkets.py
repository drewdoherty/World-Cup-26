"""Tests for the PM event-markets engine (src/wca/eventmarkets.py +
scripts/wca_event_markets.py).

Covers: grid pricing against hand-computed values (explicit matrix AND a known
independent-Poisson lambda pair), the market-blend fair value, PM market
classification, the +/-2pp back/lay signal boundaries, and the trade-rec
governance (longshot cash floor, kill-list, totals-under ban, quarter-Kelly +
caps, same-fixture correlation cap, canonical selection ordering, 2pp net-edge
floor boundaries) plus feed row shape.

All tests run offline — no network, no live data, no ledger access.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca import eventmarkets as EM  # noqa: E402
import wca_event_markets as script  # noqa: E402


# ---------------------------------------------------------------------------
# Grid pricing — explicit 3x3 matrix, every value hand-computed.
# ---------------------------------------------------------------------------

# M[h, a]: rows = home goals 0..2, cols = away goals 0..2; sums to 1.
M = np.array([
    [0.10, 0.05, 0.02],
    [0.20, 0.15, 0.03],
    [0.10, 0.20, 0.15],
])


def test_matrix_sums_to_one():
    assert M.sum() == pytest.approx(1.0)


def test_prob_over_hand_computed():
    # totals > 2.5: (1,2)=0.03 + (2,1)=0.20 + (2,2)=0.15
    assert EM.prob_over(M, 2.5) == pytest.approx(0.38)
    # totals > 0.5: 1 - P(0-0)
    assert EM.prob_over(M, 0.5) == pytest.approx(0.90)


def test_prob_btts_hand_computed():
    # 1 - P(home 0) - P(away 0) + P(0-0) = 1 - 0.17 - 0.40 + 0.10
    assert EM.prob_btts(M) == pytest.approx(0.53)


def test_prob_exact_and_any_other():
    assert EM.prob_exact(M, 1, 0) == pytest.approx(0.20)
    assert EM.prob_exact(M, 5, 0) == 0.0  # outside grid
    assert EM.prob_any_other_score(M, [(0, 0), (1, 1)]) == pytest.approx(0.75)


def test_prob_margin_hand_computed():
    # home by 1+: (1,0)+(2,0)+(2,1) = 0.20+0.10+0.20
    assert EM.prob_margin_at_least(M, 1, "home") == pytest.approx(0.50)
    # home by 2+: (2,0)
    assert EM.prob_margin_at_least(M, 2, "home") == pytest.approx(0.10)
    # away by 1+: (0,1)+(0,2)+(1,2) = 0.05+0.02+0.03
    assert EM.prob_margin_at_least(M, 1, "away") == pytest.approx(0.10)


def test_prob_team_over_hand_computed():
    # home goals > 1.5: row 2 mass = 0.45
    assert EM.prob_team_over(M, 1.5, "home") == pytest.approx(0.45)
    # away goals > 0.5: 1 - col 0 mass (0.40)
    assert EM.prob_team_over(M, 0.5, "away") == pytest.approx(0.60)


def test_prob_clean_sheet_and_draw_hand_computed():
    assert EM.prob_clean_sheet(M, "home") == pytest.approx(0.40)  # away scores 0
    assert EM.prob_clean_sheet(M, "away") == pytest.approx(0.17)  # home scores 0
    assert EM.prob_draw(M) == pytest.approx(0.10 + 0.15 + 0.15)


# ---------------------------------------------------------------------------
# Grid pricing — independent Poisson at a known lambda pair (closed form).
# ---------------------------------------------------------------------------


def _poisson_matrix(lam_h: float, lam_a: float, n: int = 10) -> np.ndarray:
    def pmf(lam, n):
        out = [math.exp(-lam)]
        for k in range(1, n + 1):
            out.append(out[-1] * lam / k)
        return np.array(out)

    return np.outer(pmf(lam_h, n), pmf(lam_a, n))


def test_poisson_grid_over25_closed_form():
    lam_h, lam_a = 1.4, 0.8
    mat = _poisson_matrix(lam_h, lam_a)
    tot = lam_h + lam_a
    p_le2 = math.exp(-tot) * (1 + tot + tot**2 / 2)  # N~Poisson(2.2)
    assert EM.prob_over(mat, 2.5) == pytest.approx(1 - p_le2, abs=1e-4)


def test_poisson_grid_btts_closed_form():
    lam_h, lam_a = 1.4, 0.8
    mat = _poisson_matrix(lam_h, lam_a)
    expected = (1 - math.exp(-lam_h)) * (1 - math.exp(-lam_a))
    assert EM.prob_btts(mat) == pytest.approx(expected, abs=1e-4)


def test_poisson_grid_exact_closed_form():
    lam_h, lam_a = 1.4, 0.8
    mat = _poisson_matrix(lam_h, lam_a)
    expected = (math.exp(-lam_h) * lam_h) * math.exp(-lam_a)  # P(1-0)
    assert EM.prob_exact(mat, 1, 0) == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# Market-blend fair value.
# ---------------------------------------------------------------------------


def test_blend_uses_60pct_market_weight():
    out = EM.blend_with_market(0.40, 0.50)
    assert out["prob"] == pytest.approx(0.4 * 0.40 + 0.6 * 0.50)
    assert "blend" in out["source"]
    assert out["components"]["market_weight"] == pytest.approx(0.60)
    assert out["components"]["dc_grid"] == pytest.approx(0.40)
    assert out["components"]["market_ref"] == pytest.approx(0.50)


def test_blend_without_market_is_labelled_raw():
    out = EM.blend_with_market(0.40, None)
    assert out["prob"] == pytest.approx(0.40)
    assert "raw" in out["source"] and "no market reference" in out["source"]


def test_blend_without_grid_is_none():
    out = EM.blend_with_market(None, 0.5)
    assert out["prob"] is None
    assert out["source"] == "unpriced"


# ---------------------------------------------------------------------------
# PM market classification.
# ---------------------------------------------------------------------------


def test_event_kind_from_slug():
    assert EM.event_kind_from_slug("fifwc-fra-mar-2026-07-09") == "main"
    assert EM.event_kind_from_slug(
        "fifwc-fra-mar-2026-07-09-more-markets") == "more_markets"
    assert EM.event_kind_from_slug(
        "fifwc-esp-bel-2026-07-10-exact-score") == "exact_score"
    assert EM.event_kind_from_slug(
        "fifwc-arg-che-2026-07-11-total-corners") == "total_corners"
    assert EM.event_kind_from_slug(
        "fifwc-arg-alg-2026-06-16-player-props") == "player_props"


def test_classify_total_goals_any_line():
    d = EM.classify_pm_market("more_markets", "O/U 3.5",
                              "France vs. Morocco: O/U 3.5", "France", "Morocco")
    assert d["family"] == "total_goals" and d["priceable"]
    assert d["line"] == pytest.approx(3.5)
    assert d["settlement"] == EM.SETTLE_90MIN


def test_classify_btts():
    d = EM.classify_pm_market("more_markets", "Both Teams to Score",
                              "Spain vs. Belgium: Both Teams to Score?",
                              "Spain", "Belgium")
    assert d["family"] == "btts" and d["priceable"]


def test_classify_spread_maps_handicap_to_margin():
    d = EM.classify_pm_market("more_markets", "France (-1.5)",
                              "Spread: France (-1.5)", "France", "Morocco")
    assert d["family"] == "spread" and d["priceable"]
    assert d["side"] == "home" and d["margin"] == 2
    d = EM.classify_pm_market("more_markets", "Morocco (-2.5)",
                              "Spread: Morocco (-2.5)", "France", "Morocco")
    assert d["side"] == "away" and d["margin"] == 3


def test_classify_team_total():
    d = EM.classify_pm_market("more_markets", "Belgium O/U 1.5",
                              "Spain vs. Belgium: Belgium O/U 1.5",
                              "Spain", "Belgium")
    assert d["family"] == "team_total" and d["priceable"]
    assert d["side"] == "away" and d["line"] == pytest.approx(1.5)


def test_classify_advance_is_et_pens_settlement():
    d = EM.classify_pm_market("more_markets", "Team to Advance",
                              "France vs. Morocco: Team to Advance",
                              "France", "Morocco")
    assert d["family"] == "advance" and d["priceable"]
    assert d["settlement"] == EM.SETTLE_ADVANCE  # never mixed into 90-min rows


def test_classify_halves_and_corners_are_honestly_unpriced():
    for kind, git in (
        ("halftime_result", "France"),
        ("second_half_result", "Draw"),
        ("first_to_score", "Morocco"),
        ("total_corners", "Total Corners: O/U 9.5"),
    ):
        d = EM.classify_pm_market(kind, git, "", "France", "Morocco")
        assert not d["priceable"]
        assert d["model_null_reason"]
    # half-scoped more-markets legs are also unpriced
    d = EM.classify_pm_market("more_markets", "1st Half O/U 1.5", "",
                              "Spain", "Belgium")
    assert not d["priceable"] and "half" in d["model_null_reason"].lower()


def test_classify_penalty_shootout_unpriced():
    d = EM.classify_pm_market("more_markets",
                              "Will the Match Go to a Penalty Shootout?",
                              "", "France", "Morocco")
    assert not d["priceable"]


def test_grid_prob_for_exact_any_other_uses_listed_scores():
    d = {"family": "exact_score", "priceable": True, "any_other": True,
         "listed_scores": [(0, 0), (1, 1)]}
    assert EM.grid_prob_for(d, M) == pytest.approx(0.75)


def test_grid_prob_for_1x2_prefers_card_blend():
    d = {"family": "1x2", "priceable": True, "leg": "home"}
    assert EM.grid_prob_for(d, M, {"home": 0.61, "draw": 0.2, "away": 0.19}) \
        == pytest.approx(0.61)
    # falls back to the grid's implied triple when no persisted blend
    assert EM.grid_prob_for(d, M) == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Signal boundaries (+/-2pp).
# ---------------------------------------------------------------------------


def test_signal_boundary_exactly_2pp_is_back():
    assert EM.signal_for(0.52, 0.50) == "back"


def test_signal_boundary_just_inside_band_is_none():
    assert EM.signal_for(0.5199, 0.50) is None
    assert EM.signal_for(0.50, 0.5199) is None


def test_signal_boundary_exactly_minus_2pp_is_lay():
    assert EM.signal_for(0.50, 0.52) == "lay"


def test_signal_none_when_either_side_missing():
    assert EM.signal_for(None, 0.5) is None
    assert EM.signal_for(0.5, None) is None


def test_edge_pp_signed():
    assert EM.edge_pp(0.316, 0.284) == pytest.approx(3.2)
    assert EM.edge_pp(0.284, 0.316) == pytest.approx(-3.2)
    assert EM.edge_pp(None, 0.5) is None


# ---------------------------------------------------------------------------
# Trade-rec governance.
# ---------------------------------------------------------------------------

_BANKROLL = 3990.0  # base PM pool (£3,000 at $1.33)


def _cand(**kw):
    base = {
        "fixture": "Team A vs Team B",
        "kickoff": "2099-12-31T20:00:00+00:00",
        "family": "total_goals",
        "label": "Over 2.5",
        "side": "back",
        "selection": "Over 2.5",
        "settlement": EM.SETTLE_90MIN,
        "model_prob": 0.60,
        "price": 0.50,
        "token_id": "t1",
        "price_source": "clob_ask",
        "captured_utc": "2026-07-08T15:00:00Z",
        "model_source": "test",
    }
    base.update(kw)
    return base


def test_rec_cash_stake_quarter_kelly_capped():
    out = EM.build_event_market_recs([_cand()], bankroll_usd=_BANKROLL)
    assert len(out["recs"]) == 1
    r = out["recs"][0]
    # f* = (0.6-0.5)/0.5 = 0.2 -> quarter-Kelly 0.05 > 4% cap -> 4% of pool
    assert r["stake_usd"] == pytest.approx(0.04 * _BANKROLL, abs=0.01)
    assert r["dimmed"] is False and r["no_cash_reason"] is None
    assert r["bucket"] == "moneyline"


def test_rec_per_order_160_cap():
    # Huge pool: 4% would be $800 -> the $160 fail-closed order cap binds.
    out = EM.build_event_market_recs([_cand()], bankroll_usd=20000.0)
    r = out["recs"][0]
    assert r["stake_usd"] == pytest.approx(160.0, abs=0.01)


def test_rec_longshot_stake_forced_zero_but_displayed():
    c = _cand(model_prob=0.20, price=0.10, label="Longshot", selection="Longshot")
    out = EM.build_event_market_recs([c], bankroll_usd=_BANKROLL)
    assert len(out["recs"]) == 1  # displayed (dimmed), NOT dropped
    r = out["recs"][0]
    assert r["stake_usd"] == 0.0
    assert r["dimmed"] is True
    assert "longshot" in r["no_cash_reason"].lower()
    assert r["bucket"] == "longshot"


def test_rec_killed_family_never_cash_even_above_25pct():
    c = _cand(family="exact_score", model_prob=0.30, price=0.20)
    out = EM.build_event_market_recs([c], bankroll_usd=_BANKROLL)
    r = out["recs"][0]
    assert r["stake_usd"] == 0.0
    assert "killed" in r["no_cash_reason"].lower()
    c = _cand(family="scorer_prop", model_prob=0.40, price=0.30)
    r = EM.build_event_market_recs([c], bankroll_usd=_BANKROLL)["recs"][0]
    assert r["stake_usd"] == 0.0 and "killed" in r["no_cash_reason"].lower()


def test_rec_totals_under_lay_side_never_staked():
    # A lay-side (Under) totals signal clears the edge floor but must be
    # display-only per the 2026-07-08 calibration study.
    c = _cand(side="lay", selection="Under 2.5", model_prob=0.60, price=0.50)
    out = EM.build_event_market_recs([c], bankroll_usd=_BANKROLL)
    r = out["recs"][0]
    assert r["stake_usd"] == 0.0
    assert "under" in r["no_cash_reason"].lower()
    assert r.get("warning")
    # ... but the same lay side on a NON-totals family is stakeable.
    c2 = _cand(family="1x2", side="lay", selection="Not Team A",
               model_prob=0.60, price=0.50)
    r2 = EM.build_event_market_recs([c2], bankroll_usd=_BANKROLL)["recs"][0]
    assert r2["stake_usd"] > 0.0


def test_rec_2pp_net_edge_floor_boundaries():
    # fee at p=0.5 is 0.03*0.5*0.5 = 0.0075; net edge = q - p - fee.
    q_at_floor = 0.50 + 0.0075 + 0.02
    out = EM.build_event_market_recs(
        [_cand(model_prob=q_at_floor)], bankroll_usd=_BANKROLL)
    assert len(out["recs"]) == 1  # exactly 2pp net -> included
    out = EM.build_event_market_recs(
        [_cand(model_prob=q_at_floor - 1e-4)], bankroll_usd=_BANKROLL)
    assert len(out["recs"]) == 0  # just below -> excluded


def test_rec_same_fixture_correlation_cap():
    a = _cand(label="Over 2.5", token_id="t1")
    b = _cand(label="Team A -1.5", family="spread", token_id="t2",
              selection="Team A -1.5")
    out = EM.build_event_market_recs([a, b], bankroll_usd=_BANKROLL)
    cash = [r for r in out["recs"] if r["stake_usd"] > 0]
    assert len(cash) == 2
    total = sum(r["stake_usd"] for r in cash)
    cap = min(EM.PM_MAX_ORDER_USD, 0.04 * _BANKROLL)
    assert total <= cap + 0.05  # whole fixture treated as ONE bet
    assert any("correlation" in c for r in cash for c in r["caps_applied"])


def test_rec_different_fixtures_not_jointly_capped():
    a = _cand(fixture="A vs B")
    b = _cand(fixture="C vs D")
    out = EM.build_event_market_recs([a, b], bankroll_usd=_BANKROLL)
    for r in out["recs"]:
        assert r["stake_usd"] == pytest.approx(0.04 * _BANKROLL, abs=0.01)


def test_rec_ordering_is_canonical_selection_rule():
    # moneyline ALWAYS above mid above longshot, regardless of EV;
    # within a bucket further-out first; EV breaks ties last.
    import datetime as dt

    now = dt.datetime(2026, 7, 8, 12, 0, 0)
    near_ml = _cand(fixture="ML near", kickoff="2026-07-09T12:00:00+00:00",
                    model_prob=0.60, price=0.50)          # moneyline, 24h out
    far_ml = _cand(fixture="ML far", kickoff="2026-07-11T12:00:00+00:00",
                   model_prob=0.55, price=0.50)           # moneyline, 72h out
    mid = _cand(fixture="MID huge EV", kickoff="2026-07-12T12:00:00+00:00",
                model_prob=0.45, price=0.30)              # mid bucket, giant EV
    lsh = _cand(fixture="LONGSHOT", kickoff="2026-07-12T12:00:00+00:00",
                model_prob=0.20, price=0.10)              # longshot
    out = EM.build_event_market_recs([mid, lsh, near_ml, far_ml],
                                     bankroll_usd=_BANKROLL, now_dt=now)
    order = [r["fixture"] for r in out["recs"]]
    assert order == ["ML far", "ML near", "MID huge EV", "LONGSHOT"]


def test_rec_meta_documents_governance():
    out = EM.build_event_market_recs([_cand()], bankroll_usd=_BANKROLL)
    meta = out["meta"]
    assert meta["fee_rate"] == pytest.approx(0.03)
    assert meta["min_edge"] == pytest.approx(0.02)
    assert "correlated" in meta["correlation_cap"]
    assert "moneyline>mid>longshot" in meta["ranking"].replace(" ", "") or \
        "moneyline" in meta["ranking"]
    assert "ET+pens" in meta["settlement_note"]


def test_rec_rejects_unpriced_candidates():
    out = EM.build_event_market_recs(
        [_cand(model_prob=None), _cand(price=None)], bankroll_usd=_BANKROLL)
    assert out["recs"] == []


# ---------------------------------------------------------------------------
# Feed shape (builder row + fixture assembly, offline).
# ---------------------------------------------------------------------------


def _pm_market(git, question, outcomes, prices, tokens, bid=None, ask=None):
    import json as _json

    m = {
        "groupItemTitle": git,
        "question": question,
        "outcomes": _json.dumps(outcomes),
        "outcomePrices": _json.dumps([str(p) for p in prices]),
        "clobTokenIds": _json.dumps(tokens),
    }
    if bid is not None:
        m["bestBid"] = bid
    if ask is not None:
        m["bestAsk"] = ask
    return m


def _fx():
    return {
        "fixture": "France vs Morocco",
        "home": "France",
        "away": "Morocco",
        "kickoff": "2099-07-09T20:00:00+00:00",
        "model_1x2": {"home": 0.585262, "draw": 0.25471, "away": 0.160028},
        "lambda_home": 1.432456,
        "lambda_away": 0.777631,
    }


def _pm_by_kind():
    main = {
        "slug": "fifwc-fra-mar-2099-07-09",
        "title": "France vs. Morocco",
        "markets": [
            _pm_market("France", "Will France win on 2099-07-09?",
                       ["Yes", "No"], [0.615, 0.385], ["tF", "tFn"],
                       bid=0.61, ask=0.62),
            _pm_market("Draw (France vs. Morocco)",
                       "Will France vs. Morocco end in a draw?",
                       ["Yes", "No"], [0.245, 0.755], ["tD", "tDn"],
                       bid=0.24, ask=0.25),
            _pm_market("Morocco", "Will Morocco win on 2099-07-09?",
                       ["Yes", "No"], [0.135, 0.865], ["tM", "tMn"],
                       bid=0.13, ask=0.14),
        ],
    }
    more = {
        "slug": "fifwc-fra-mar-2099-07-09-more-markets",
        "title": "France vs. Morocco - More Markets",
        "markets": [
            _pm_market("O/U 2.5", "France vs. Morocco: O/U 2.5",
                       ["Over", "Under"], [0.475, 0.525], ["tO25", "tU25"],
                       bid=0.47, ask=0.48),
            _pm_market("France (-1.5)", "Spread: France (-1.5)",
                       ["France", "Morocco"], [0.345, 0.655], ["tS", "tSn"],
                       bid=0.34, ask=0.35),
            _pm_market("Team to Advance", "France vs. Morocco: Team to Advance",
                       ["France", "Morocco"], [0.785, 0.215], ["tA", "tAn"],
                       bid=0.78, ask=0.79),
            _pm_market("Will the Match Go to Extra Time?",
                       "France vs. Morocco: Will the Match Go to Extra Time?",
                       ["Yes", "No"], [0.255, 0.745], ["tET", "tETn"],
                       bid=0.25, ask=0.26),
            _pm_market("1st Half O/U 0.5", "France vs. Morocco: 1st Half O/U 0.5",
                       ["Over", "Under"], [0.71, 0.29], ["tH", "tHn"],
                       bid=0.70, ask=0.72),
        ],
    }
    return {"main": [main], "more_markets": [more]}


def _adv_model():
    return {
        "France": {"R32": 1.0, "R16": 1.0, "QF": 1.0, "SF": 0.6929},
        "Morocco": {"R32": 1.0, "R16": 1.0, "QF": 1.0, "SF": 0.3071},
    }


def _build():
    mat = _poisson_matrix(1.432456, 0.777631)
    entry, cands = script.build_fixture(
        _fx(), mat, {"ok": True}, _pm_by_kind(), _adv_model(),
        "2026-07-08 14:24 UTC", "2026-07-08T15:00:00Z",
        use_clob=False, scorer_pricer=None)
    return entry, cands


def test_feed_rows_have_required_keys():
    entry, _ = _build()
    assert entry["fixture"] == "France vs Morocco"
    assert entry["has_market"] is True
    data_rows = [r for r in entry["rows"] if "section" not in r]
    assert data_rows, "no data rows emitted"
    for r in data_rows:
        assert "label" in r and "model" in r and "market" in r
        assert "family" in r and "settlement" in r
        assert "edge_pp" in r and "signal" in r
        if r["market"] is None:
            assert r["market_null_reason"] == "no PM market" or \
                "no PM market" in r["market_null_reason"]
        else:
            assert r.get("price_source")
            assert r.get("captured_utc")
            assert r.get("token_id")
        if r["model"] is None:
            assert r.get("model_null_reason")


def test_feed_1x2_uses_persisted_card_blend_verbatim():
    entry, _ = _build()
    by_label = {r["label"]: r for r in entry["rows"] if "section" not in r}
    assert by_label["France"]["model"] == pytest.approx(0.585262)
    assert by_label["Draw"]["model"] == pytest.approx(0.25471)
    assert by_label["Morocco"]["model"] == pytest.approx(0.160028)
    # gamma mid of 0.61/0.62 (use_clob=False path) with source recorded
    assert by_label["France"]["market"] == pytest.approx(0.615)
    assert by_label["France"]["price_source"] == "gamma_mid"


def test_feed_totals_row_is_market_blended_and_labelled():
    entry, _ = _build()
    row = next(r for r in entry["rows"]
               if "section" not in r and r["family"] == "total_goals")
    comp = row.get("model_components")
    assert comp and comp["market_weight"] == pytest.approx(0.60)
    expected = 0.4 * comp["dc_grid"] + 0.6 * comp["market_ref"]
    assert row["model"] == pytest.approx(expected, abs=1e-6)
    assert "blend" in row["model_source"]


def test_feed_advance_rows_are_flagged_et_pens_and_separate():
    entry, _ = _build()
    adv_rows = [r for r in entry["rows"]
                if "section" not in r and r["family"] == "advance"]
    assert len(adv_rows) == 2
    for r in adv_rows:
        assert r["settlement"] == EM.SETTLE_ADVANCE
    # model = MC sim complement pair
    assert adv_rows[0]["model"] == pytest.approx(0.6929)
    assert adv_rows[1]["model"] == pytest.approx(0.3071)
    # its section header is the ET+pens one
    sec = next(r for r in entry["rows"]
               if "section" in r and r.get("settlement") == EM.SETTLE_ADVANCE)
    assert "Advance" in sec["section"]


def test_feed_half_market_is_market_only_with_reason():
    entry, _ = _build()
    row = next(r for r in entry["rows"]
               if "section" not in r and r["family"] == "half_market")
    assert row["model"] is None
    assert "half" in row["model_null_reason"].lower()
    assert row["market"] is not None  # market price still shown
    assert row["signal"] is None      # no trade signal without a model


def test_feed_extra_time_model_is_card_draw_prob():
    entry, _ = _build()
    row = next(r for r in entry["rows"]
               if "section" not in r and r["family"] == "extra_time")
    assert row["model"] == pytest.approx(0.25471)


def test_feed_btts_without_pm_market_says_no_pm_market():
    pm = _pm_by_kind()  # no BTTS market listed (mirrors France-Morocco live)
    mat = _poisson_matrix(1.432456, 0.777631)
    entry, _ = script.build_fixture(
        _fx(), mat, {"ok": True}, pm, _adv_model(), "", "2026-07-08T15:00:00Z",
        use_clob=False, scorer_pricer=None)
    row = next(r for r in entry["rows"]
               if "section" not in r and r["family"] == "btts")
    assert row["market"] is None
    assert row["market_null_reason"] == "no PM market"
    assert row["model"] is not None
    assert "raw" in row["model_source"]  # honestly labelled, not "fair blend"


def test_feed_no_pm_events_at_all_never_fabricates():
    mat = _poisson_matrix(1.4, 0.8)
    entry, cands = script.build_fixture(
        _fx(), mat, {"ok": True}, {}, {}, "", "2026-07-08T15:00:00Z",
        use_clob=False, scorer_pricer=None)
    data_rows = [r for r in entry["rows"] if "section" not in r]
    assert all(r["market"] is None for r in data_rows)
    assert entry["has_market"] is False
    assert cands == []  # no market -> no trade candidates


def test_feed_candidates_have_executable_prices():
    _, cands = _build()
    assert cands
    for c in cands:
        assert 0.0 < c["price"] < 1.0
        assert c["side"] in ("back", "lay")
        assert c["model_prob"] is not None
        assert c["settlement"] in (EM.SETTLE_90MIN, EM.SETTLE_ADVANCE)
    # back sides use the ask, lay sides the mirrored 1-bid
    back = next(c for c in cands if c["label"] == "France" and c["side"] == "back")
    assert back["price"] == pytest.approx(0.62)
    lay = next(c for c in cands if c["label"] == "France" and c["side"] == "lay")
    assert lay["price"] == pytest.approx(1.0 - 0.61)
    assert lay["model_prob"] == pytest.approx(1.0 - 0.585262)


def test_load_upcoming_fixtures_filters_by_window(tmp_path):
    import datetime as dt
    import json as _json

    now = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)
    preds = {
        "fixtures": [
            {"fixture": "A vs B", "kickoff": "2026-07-09 20:00:00+00:00",
             "model": {"home": 0.5, "draw": 0.3, "away": 0.2}},
            {"fixture": "C vs D", "kickoff": "2026-07-20 20:00:00+00:00",
             "model": {"home": 0.5, "draw": 0.3, "away": 0.2}},
            {"fixture": "E vs F", "kickoff": "2026-07-01 20:00:00+00:00",
             "model": {"home": 0.5, "draw": 0.3, "away": 0.2}},
        ],
        "meta": {},
    }
    p = tmp_path / "preds.json"
    p.write_text(_json.dumps(preds))
    out = script.load_upcoming_fixtures(str(p), 7.0, now)
    assert [f["fixture"] for f in out] == ["A vs B"]


def test_advance_model_probs_pairing_mismatch_is_null():
    adv = {
        "France": {"R32": 1.0, "R16": 1.0, "QF": 1.0, "SF": 0.6929},
        "Morocco": {"R32": 1.0, "R16": 1.0, "QF": 1.0, "SF": 0.55},  # stale sim
    }
    ph, pa, note = script.advance_model_probs(adv, "France", "Morocco")
    assert ph is None and pa is None
    assert "mismatch" in note
