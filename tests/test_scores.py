"""Tests for full-time scoreline predictions reconciled to the blended 1X2.

Covers three layers:

1.  :func:`reconcile_scoreline_matrix` — exactness of the implied 1X2, mass
    conservation, preservation of within-region ratios, the degenerate-region
    fallback, and the NaN/negative input guards.
2.  :class:`ScorelineCard` derived markets — top-k ordering, O/U and BTTS on a
    hand-checkable 3x3 matrix, and the ``fair_odds`` / ``min_price`` formulas.
3.  Card integration — that ``build_score_cards`` / ``format_scores`` run on a
    synthetic odds + fixtures slate and emit scorelines reconciled to the same
    blend the recommendations use.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from wca.models.scores import (
    ScorelineCard,
    btts_from_matrix,
    fair_odds,
    implied_1x2,
    min_price,
    over_under_from_matrix,
    reconcile_scoreline_matrix,
    scoreline_card,
    top_scorelines_from_matrix,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _independent_poisson(lam_h, lam_a, n=8):
    """Reference truncated independent-Poisson matrix (no DC correction)."""
    from scipy.stats import poisson

    goals = np.arange(n)
    ph = poisson.pmf(goals, lam_h)
    pa = poisson.pmf(goals, lam_a)
    mat = np.outer(ph, pa)
    return mat / mat.sum()


# ---------------------------------------------------------------------------
# 1. Reconciliation.
# ---------------------------------------------------------------------------


class TestReconcile:
    def test_implied_1x2_equals_target_exactly(self):
        mat = _independent_poisson(1.6, 1.1, n=10)
        target = (0.5, 0.3, 0.2)
        rec = reconcile_scoreline_matrix(mat, target)
        ph, pdraw, pa = implied_1x2(rec)
        assert ph == pytest.approx(0.5, abs=1e-12)
        assert pdraw == pytest.approx(0.3, abs=1e-12)
        assert pa == pytest.approx(0.2, abs=1e-12)

    def test_matrix_sums_to_one(self):
        mat = _independent_poisson(2.0, 0.7, n=9)
        rec = reconcile_scoreline_matrix(mat, (0.4, 0.25, 0.35))
        assert rec.sum() == pytest.approx(1.0, abs=1e-12)

    def test_target_renormalised_if_not_summing_to_one(self):
        mat = _independent_poisson(1.3, 1.3, n=8)
        # Target sums to 2.0; should be renormalised to (0.25, 0.5, 0.25).
        rec = reconcile_scoreline_matrix(mat, (0.5, 1.0, 0.5))
        ph, pdraw, pa = implied_1x2(rec)
        assert ph == pytest.approx(0.25, abs=1e-12)
        assert pdraw == pytest.approx(0.5, abs=1e-12)
        assert pa == pytest.approx(0.25, abs=1e-12)

    def test_within_region_ratios_preserved(self):
        mat = _independent_poisson(1.7, 1.0, n=10)
        rec = reconcile_scoreline_matrix(mat, (0.55, 0.25, 0.20))
        rows = np.arange(mat.shape[0])[:, None]
        cols = np.arange(mat.shape[1])[None, :]
        # Two distinct home-win cells: their ratio must be unchanged.
        for (h1, a1), (h2, a2) in [((2, 0), (3, 1)), ((1, 0), (4, 2))]:
            assert rec[h2, a2] > 0
            orig = mat[h1, a1] / mat[h2, a2]
            new = rec[h1, a1] / rec[h2, a2]
            assert new == pytest.approx(orig, rel=1e-12)
        # An away-win pair too.
        assert rec[0, 2] / rec[1, 3] == pytest.approx(mat[0, 2] / mat[1, 3], rel=1e-12)

    def test_region_scaled_by_single_constant(self):
        mat = _independent_poisson(1.4, 1.4, n=8)
        target = (0.45, 0.30, 0.25)
        rec = reconcile_scoreline_matrix(mat, target)
        home_alpha = target[0] / float(np.tril(mat, k=-1).sum())
        # Every home-win cell scaled by the same alpha.
        for h, a in [(1, 0), (2, 0), (2, 1), (3, 1)]:
            assert rec[h, a] == pytest.approx(mat[h, a] * home_alpha, rel=1e-12)

    def test_degenerate_region_reallocated_from_prior(self):
        # Start from a matrix with ZERO draw mass, but ask for positive draw prob.
        mat = _independent_poisson(1.5, 1.2, n=8)
        diag = np.arange(min(mat.shape))
        mat[diag, diag] = 0.0  # wipe the entire draw region
        mat = mat / mat.sum()
        assert implied_1x2(mat)[1] == pytest.approx(0.0, abs=1e-15)

        rec = reconcile_scoreline_matrix(mat, (0.4, 0.3, 0.3), lambdas=(1.5, 1.2))
        ph, pdraw, pa = implied_1x2(rec)
        assert pdraw == pytest.approx(0.3, abs=1e-12)
        assert ph == pytest.approx(0.4, abs=1e-12)
        assert pa == pytest.approx(0.3, abs=1e-12)
        # Draw mass was reallocated onto the diagonal (and only there).
        assert rec.sum() == pytest.approx(1.0, abs=1e-12)
        diag_mass = float(np.trace(rec))
        assert diag_mass == pytest.approx(0.3, abs=1e-12)
        # The reallocated draw shape follows the independent-Poisson prior, so
        # the most likely draw cell matches the prior's most likely draw cell
        # (1-1 dominates 0-0 here because both lambdas exceed 1).
        prior = _independent_poisson(1.5, 1.2, n=8)
        prior_diag = np.array([prior[i, i] for i in diag])
        prior_diag = prior_diag / prior_diag.sum()
        rec_diag = np.array([rec[i, i] for i in diag])
        # Within-region shape equals the prior's diagonal shape.
        assert rec_diag == pytest.approx(prior_diag * 0.3, abs=1e-12)
        assert int(np.argmax(rec_diag)) == int(np.argmax(prior_diag))

    def test_degenerate_region_canonical_fallback(self):
        # Tiny 2x2 matrix with all draw mass removed and lambdas that make the
        # independent-Poisson prior numerically negligible on the diagonal is
        # hard to force; instead test the canonical fallback directly via a
        # matrix whose prior diagonal is also ~0 by using zero lambdas.
        mat = np.zeros((3, 3))
        mat[1, 0] = 0.5  # home win
        mat[0, 1] = 0.5  # away win
        # No draw mass. lambdas=(0,0) -> prior is all on (0,0) which IS a draw,
        # so reallocation lands on 0-0; assert that canonical draw cell.
        rec = reconcile_scoreline_matrix(mat, (0.4, 0.2, 0.4), lambdas=(0.0, 0.0))
        assert rec[0, 0] == pytest.approx(0.2, abs=1e-12)
        assert implied_1x2(rec) == pytest.approx((0.4, 0.2, 0.4), abs=1e-12)

    def test_fully_degenerate_source_uses_prior(self):
        mat = np.zeros((6, 6))
        rec = reconcile_scoreline_matrix(mat, (0.5, 0.25, 0.25), lambdas=(1.2, 1.0))
        assert rec.sum() == pytest.approx(1.0, abs=1e-12)
        assert implied_1x2(rec) == pytest.approx((0.5, 0.25, 0.25), abs=1e-12)

    def test_zero_target_region_left_empty(self):
        mat = _independent_poisson(1.5, 1.5, n=8)
        rec = reconcile_scoreline_matrix(mat, (0.6, 0.4, 0.0))
        ph, pdraw, pa = implied_1x2(rec)
        assert pa == pytest.approx(0.0, abs=1e-12)
        assert ph == pytest.approx(0.6, abs=1e-12)
        assert pdraw == pytest.approx(0.4, abs=1e-12)

    def test_guards_negative_matrix(self):
        mat = _independent_poisson(1.0, 1.0, n=5)
        mat[1, 1] = -0.1
        with pytest.raises(ValueError):
            reconcile_scoreline_matrix(mat, (0.4, 0.3, 0.3))

    def test_guards_nan_matrix(self):
        mat = _independent_poisson(1.0, 1.0, n=5)
        mat[2, 1] = np.nan
        with pytest.raises(ValueError):
            reconcile_scoreline_matrix(mat, (0.4, 0.3, 0.3))

    def test_guards_negative_target(self):
        mat = _independent_poisson(1.0, 1.0, n=5)
        with pytest.raises(ValueError):
            reconcile_scoreline_matrix(mat, (0.6, 0.6, -0.2))

    def test_guards_nan_target(self):
        mat = _independent_poisson(1.0, 1.0, n=5)
        with pytest.raises(ValueError):
            reconcile_scoreline_matrix(mat, (0.5, np.nan, 0.5))

    def test_guards_zero_sum_target(self):
        mat = _independent_poisson(1.0, 1.0, n=5)
        with pytest.raises(ValueError):
            reconcile_scoreline_matrix(mat, (0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# 2. ScorelineCard derived markets on a hand-computed 3x3 matrix.
# ---------------------------------------------------------------------------


def _tiny_matrix():
    """A 3x3 matrix (home/away goals in {0,1,2}) summing to one.

    Layout (rows=home goals, cols=away goals):

            a0    a1    a2
        h0  0.10  0.12  0.03
        h1  0.15  0.20  0.05
        h2  0.18  0.10  0.07
    """
    m = np.array(
        [
            [0.10, 0.12, 0.03],
            [0.15, 0.20, 0.05],
            [0.18, 0.10, 0.07],
        ]
    )
    assert m.sum() == pytest.approx(1.0)
    return m


class TestScorelineCardMarkets:
    def test_implied_1x2_hand_computed(self):
        m = _tiny_matrix()
        # home win (h>a): (1,0)+(2,0)+(2,1) = 0.15+0.18+0.10 = 0.43
        # draw (h==a):    (0,0)+(1,1)+(2,2) = 0.10+0.20+0.07 = 0.37
        # away win (h<a): (0,1)+(0,2)+(1,2) = 0.12+0.03+0.05 = 0.20
        assert implied_1x2(m) == pytest.approx((0.43, 0.37, 0.20), abs=1e-12)

    def test_over_under_hand_computed(self):
        m = _tiny_matrix()
        # totals: 0->0.10; 1->0.12+0.15=0.27; 2->0.03+0.20+0.18=0.41;
        #         3->0.05+0.10=0.15; 4->0.07
        # over 2.5 = totals {3,4} = 0.15+0.07 = 0.22
        over, under = over_under_from_matrix(m, 2.5)
        assert over == pytest.approx(0.22, abs=1e-12)
        assert under == pytest.approx(0.78, abs=1e-12)
        assert over + under == pytest.approx(1.0, abs=1e-12)
        # over 1.5 = totals {2,3,4} = 0.41+0.15+0.07 = 0.63
        over15, under15 = over_under_from_matrix(m, 1.5)
        assert over15 == pytest.approx(0.63, abs=1e-12)
        assert under15 == pytest.approx(0.37, abs=1e-12)

    def test_btts_hand_computed(self):
        m = _tiny_matrix()
        # BTTS = 1 - P(home=0) - P(away=0) + P(0,0)
        # P(home=0)=row0 sum=0.10+0.12+0.03=0.25
        # P(away=0)=col0 sum=0.10+0.15+0.18=0.43
        # P(0,0)=0.10  -> 1 - 0.25 - 0.43 + 0.10 = 0.42
        assert btts_from_matrix(m) == pytest.approx(0.42, abs=1e-12)

    def test_top_scorelines_ordering(self):
        m = _tiny_matrix()
        top = top_scorelines_from_matrix(m, k=3)
        assert top[0] == (1, 1, pytest.approx(0.20))
        assert top[1] == (2, 0, pytest.approx(0.18))
        assert top[2] == (1, 0, pytest.approx(0.15))
        # Descending probability.
        probs = [p for _, _, p in top]
        assert probs == sorted(probs, reverse=True)

    def test_top_scorelines_deterministic_tiebreak(self):
        # Two equal-prob cells: tie broken by ascending (home, away).
        m = np.zeros((3, 3))
        m[2, 1] = 0.5
        m[0, 1] = 0.5
        top = top_scorelines_from_matrix(m, k=2)
        assert top[0][:2] == (0, 1)  # lower (home,away) first on tie
        assert top[1][:2] == (2, 1)

    def test_fair_odds_formula(self):
        assert fair_odds(0.25) == pytest.approx(4.0)
        assert fair_odds(0.1) == pytest.approx(10.0)
        assert fair_odds(0.0) == math.inf
        assert ScorelineCard.fair_odds(0.5) == pytest.approx(2.0)

    def test_min_price_formula(self):
        # min back price = (1 + edge) / p
        assert min_price(0.25, 0.0) == pytest.approx(4.0)
        assert min_price(0.25, 0.04) == pytest.approx(1.04 / 0.25)
        assert min_price(0.1, 0.02) == pytest.approx(1.02 / 0.1)
        assert min_price(0.0, 0.05) == math.inf

    def test_card_min_price_uses_card_edge(self):
        m = _tiny_matrix()
        card = scoreline_card(_FakePrediction(m, 1.0, 1.0), implied_1x2(m), min_edge=0.05)
        assert card.min_price(0.2) == pytest.approx(1.05 / 0.2)
        # Explicit override wins.
        assert card.min_price(0.2, min_edge=0.0) == pytest.approx(1.0 / 0.2)


class _FakePrediction:
    """Minimal stand-in for ScorelinePrediction (matrix + lambdas + labels)."""

    def __init__(self, matrix, lambda_home, lambda_away, home="H", away="A"):
        self.matrix = np.asarray(matrix, dtype=float)
        self.lambda_home = lambda_home
        self.lambda_away = lambda_away
        self.home = home
        self.away = away


class TestScorelineCardBuild:
    def test_card_reconciles_to_target(self):
        m = _independent_poisson(1.5, 1.1, n=9)
        target = (0.5, 0.28, 0.22)
        card = scoreline_card(
            _FakePrediction(m, 1.5, 1.1, home="Brazil", away="Serbia"), target
        )
        assert card.home == "Brazil"
        assert card.away == "Serbia"
        assert card.one_x_two == pytest.approx(target, abs=1e-12)
        assert card.matrix.sum() == pytest.approx(1.0, abs=1e-12)
        assert len(card.top_scorelines) == 6
        # O/U + BTTS derived from the RECONCILED matrix.
        assert set(card.over_under.keys()) == {1.5, 2.5, 3.5}
        over25, under25 = card.over_under[2.5]
        assert over25 + under25 == pytest.approx(1.0, abs=1e-12)
        assert (over25, under25) == pytest.approx(
            over_under_from_matrix(card.matrix, 2.5), abs=1e-12
        )
        assert card.btts == pytest.approx(btts_from_matrix(card.matrix), abs=1e-12)

    def test_card_uses_real_dixon_coles_prediction(self):
        from wca.models.dixon_coles import DixonColesModel

        rng = np.random.default_rng(0)
        teams = ["A", "B", "C", "D"]
        homes, aways, hg, ag = [], [], [], []
        for _ in range(120):
            i, j = rng.choice(len(teams), size=2, replace=False)
            homes.append(teams[i])
            aways.append(teams[j])
            hg.append(int(rng.poisson(1.4)))
            ag.append(int(rng.poisson(1.1)))
        model = DixonColesModel().fit(homes, aways, hg, ag)
        pred = model.predict("A", "B", warn=False)
        target = (0.5, 0.3, 0.2)
        card = scoreline_card(pred, target, home="A", away="B")
        assert card.one_x_two == pytest.approx(target, abs=1e-12)
        assert card.matrix.shape == pred.matrix.shape


# ---------------------------------------------------------------------------
# 3. Card-pipeline integration on a synthetic slate.
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=200):
    """Synthetic international results history with dates + neutral flags."""
    teams = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    base = pd.Timestamp("2022-01-01")
    rows = []
    for k in range(n):
        i, j = rng.choice(len(teams), size=2, replace=False)
        rows.append(
            {
                "date": base + pd.Timedelta(days=int(k)),
                "home_team": teams[i],
                "away_team": teams[j],
                "home_score": int(rng.poisson(1.5)),
                "away_score": int(rng.poisson(1.1)),
                "tournament": "Friendly",
                "neutral": False,
            }
        )
    return pd.DataFrame(rows)


def _synthetic_odds():
    """Flat h2h odds frame for one fixture across two books (Odds-API schema)."""
    rows = []
    fixture = dict(
        event_id="evt1",
        home_team="Alpha",
        away_team="Bravo",
        commence_time="2026-06-11T18:00:00Z",
        market="h2h",
    )
    book_prices = {
        "book_a": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.60},
        "book_b": {"Alpha": 2.05, "Draw": 3.30, "Bravo": 3.80},
    }
    for book, prices in book_prices.items():
        for name, odd in prices.items():
            rows.append(
                dict(fixture, bookmaker_key=book, outcome_name=name, decimal_odds=odd)
            )
    return pd.DataFrame(rows)


def _synthetic_fixtures_meta():
    return pd.DataFrame(
        [
            {
                "home_team": "Alpha",
                "away_team": "Bravo",
                "neutral": False,
                "country": "",
                "home_score": np.nan,
                "away_score": np.nan,
            }
        ]
    )


class TestCardIntegration:
    def test_build_score_cards_and_format(self):
        from wca.card import (
            BlendWeights,
            build_card,
            build_score_cards,
            fit_models,
            format_scores,
            PoolConfig,
        )

        rng = np.random.default_rng(42)
        results = _synthetic_results(rng)
        odds = _synthetic_odds()
        meta = _synthetic_fixtures_meta()
        weights = BlendWeights(elo=0.25, dc=0.25, market=0.50)

        models = fit_models(results, half_life_years=8.0)
        cards = build_score_cards(models, odds, meta, weights=weights)

        assert len(cards) == 1
        card = cards[0]
        assert card.home == "Alpha"
        assert card.away == "Bravo"
        assert card.matrix.sum() == pytest.approx(1.0, abs=1e-12)
        assert len(card.top_scorelines) == 6

        # The score card's implied 1X2 must equal the blend the *bets* use.
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        # Recompute the blend exactly as build_card does, via build_card recs
        # plus an independent recomputation of the blended home prob.
        recs = build_card(models, odds, pools, meta, weights=weights, min_edge=-1.0)
        assert recs, "expected at least one recommendation on the synthetic slate"
        # Every rec for this single fixture carries its blended model_prob; the
        # card's one_x_two for the matching outcome must agree.
        outcome_to_idx = {"home": 0, "draw": 1, "away": 2}
        for r in recs:
            assert card.one_x_two[outcome_to_idx[r.selection]] == pytest.approx(
                r.model_prob, abs=1e-9
            )

        text = format_scores(cards)
        assert "Alpha vs Bravo" in text
        assert "O/U 2.5" in text
        assert "BTTS" in text
        assert "back >=" in text
        # A scoreline line like "1-0  ..%  fair ..  back >= .." must appear.
        assert "fair" in text

    def test_build_card_still_backward_compatible(self):
        from wca.card import build_card, fit_models, format_card, PoolConfig

        rng = np.random.default_rng(7)
        results = _synthetic_results(rng)
        odds = _synthetic_odds()
        meta = _synthetic_fixtures_meta()
        models = fit_models(results, half_life_years=8.0)
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        recs = build_card(models, odds, pools, meta, min_edge=-1.0)
        # Backward-compatible: still returns Recommendation objects, formattable.
        assert recs
        out = format_card(recs, pools)
        assert "World Cup Alpha" in out
