"""Unit tests for the Model-vs-Venue benchmark engine (wca.venuesbench).

Pure-logic tests: distance metrics, Shin de-vig of a book triple, ex-market and
leave-one-book-out comparators, synthetic rank recovery, common support,
fixture-block bootstrap determinism, Friedman/permutation inference, BH-FDR,
accuracy (agreement != accuracy), and book canonicalisation.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from wca import venuesbench as vb
from wca import venues


# --------------------------------------------------------------------------- #
# Distance & accuracy metrics
# --------------------------------------------------------------------------- #


def test_identical_triples_zero_distance():
    p = (0.5, 0.3, 0.2)
    assert vb.mae(p, p) == 0.0
    assert vb.rmse(p, p) == 0.0
    assert vb.abs_logit_gap(p, p) == pytest.approx(0.0, abs=1e-12)
    assert vb.tv_distance(p, p) == pytest.approx(0.0, abs=1e-12)
    assert vb.js_distance(p, p) == pytest.approx(0.0, abs=1e-9)


def test_tv_and_mae_known_values():
    a = (0.6, 0.2, 0.2)
    b = (0.5, 0.3, 0.2)
    # |0.1| + |0.1| + |0| = 0.2 ; TV = 0.1 ; MAE = 0.2/3
    assert vb.tv_distance(a, b) == pytest.approx(0.1, abs=1e-12)
    assert vb.mae(a, b) == pytest.approx(0.2 / 3, abs=1e-12)


def test_js_distance_bounded():
    a = (0.98, 0.01, 0.01)
    b = (0.01, 0.01, 0.98)
    d = vb.js_distance(a, b)
    assert 0.0 <= d <= 1.0
    assert d > 0.9  # near-orthogonal distributions


def test_as_triple_accepts_dict_and_normalises():
    t = vb.as_triple({"Home": 2.0, "Draw": 1.0, "Away": 1.0})
    assert sum(t) == pytest.approx(1.0)
    assert t[0] == pytest.approx(0.5)


def test_as_triple_rejects_bad():
    with pytest.raises(ValueError):
        vb.as_triple((1.0, 2.0))
    with pytest.raises(ValueError):
        vb.as_triple({"Home": 0.0, "Draw": 0.0, "Away": 0.0})


def test_brier_logloss_and_agreement_is_not_accuracy():
    # A venue can perfectly agree with the model yet both be wrong.
    model = (0.7, 0.2, 0.1)
    venue = (0.7, 0.2, 0.1)  # identical -> zero distance
    assert vb.mae(model, venue) == 0.0
    # Outcome was the Away longshot: both score badly despite agreeing.
    assert vb.brier(model, "Away") == pytest.approx((0.7) ** 2 + (0.2) ** 2 + (0.1 - 1) ** 2)
    assert vb.log_loss(model, "Away") == pytest.approx(-math.log(0.1), abs=1e-6)
    # A different venue closer to the truth scores better on accuracy.
    sharp = (0.3, 0.2, 0.5)
    assert vb.brier(sharp, "Away") < vb.brier(model, "Away")


# --------------------------------------------------------------------------- #
# De-vig a book triple (Shin)
# --------------------------------------------------------------------------- #


def test_book_fair_triple_recovers_fair_book():
    # A fair book whose odds are exactly 1/p returns p (Shin z=0 on a fair book).
    p = (0.5, 0.3, 0.2)
    odds = {leg: 1.0 / p[i] for i, leg in enumerate(vb.LEGS)}
    fair = vb.book_fair_triple(odds)
    assert fair is not None
    assert fair == pytest.approx(p, abs=1e-9)


def test_book_fair_triple_devigs_margin_to_one():
    odds = {"Home": 1.8, "Draw": 3.4, "Away": 4.2}  # margined book
    fair = vb.book_fair_triple(odds)
    assert fair is not None
    assert sum(fair) == pytest.approx(1.0, abs=1e-9)


def test_incomplete_book_is_omitted():
    assert vb.book_fair_triple({"Home": 1.8, "Draw": 3.4}) is None          # missing Away
    assert vb.book_fair_triple({"Home": 1.0, "Draw": 3.4, "Away": 4.2}) is None  # odds <= 1.0
    assert vb.book_fair_triple({"Home": 1.8, "Draw": 3.4, "Away": "x"}) is None  # non-numeric


# --------------------------------------------------------------------------- #
# Comparators: ex-market blend & leave-one-book-out
# --------------------------------------------------------------------------- #


def test_ex_market_blend_drops_market():
    elo = (0.4, 0.3, 0.3)
    dc = (0.5, 0.3, 0.2)
    out = vb.ex_market_triple(elo, dc, w_elo=0.30, w_dc=0.70)
    expect = np.array([0.30 * elo[i] + 0.70 * dc[i] for i in range(3)])
    expect = expect / expect.sum()
    assert out == pytest.approx(tuple(expect), abs=1e-12)
    assert sum(out) == pytest.approx(1.0)


def test_lobo_excludes_the_book():
    triples = {
        "A": (0.9, 0.05, 0.05),
        "B": (0.5, 0.3, 0.2),
        "C": (0.5, 0.3, 0.2),
    }
    # Excluding the outlier A leaves the B/C consensus.
    lobo = vb.lobo_consensus(triples, exclude="A")
    assert lobo == pytest.approx((0.5, 0.3, 0.2), abs=1e-9)
    # The full consensus is pulled toward A.
    full = vb.consensus_triple(list(triples.values()))
    assert full[0] > lobo[0]
    # Excluding the only book -> no independent consensus.
    assert vb.lobo_consensus({"A": (0.5, 0.3, 0.2)}, exclude="A") is None


def test_exchange_executable_raises_breakeven_prob():
    # Commission is a cost: it lowers effective odds, so the executable
    # (break-even) implied prob is HIGHER than the fair-midpoint prob.
    fair_odds = 2.0  # fair prob 0.5
    p_exec = vb.exchange_executable_prob(fair_odds, commission=0.02)
    assert p_exec > 0.5
    assert p_exec == pytest.approx(1.0 / (1.0 + 1.0 * 0.98))


# --------------------------------------------------------------------------- #
# Ranking & inference
# --------------------------------------------------------------------------- #


def _panel_from_distances(dist_by_venue):
    """Build {obs_id: {venue: dist}} from {venue: [per-fixture dists]}."""
    venues_ = list(dist_by_venue.keys())
    n = len(next(iter(dist_by_venue.values())))
    panel = {}
    for i in range(n):
        obs = "fix%02d|b0" % i
        panel[obs] = {v: dist_by_venue[v][i] for v in venues_}
    return panel, venues_


def test_synthetic_rankings_clear_winner():
    rng = np.random.default_rng(0)
    n = 14
    panel, venues_ = _panel_from_distances({
        "A": list(0.010 + 0.001 * rng.random(n)),  # clearly closest
        "B": list(0.050 + 0.001 * rng.random(n)),
        "C": list(0.090 + 0.001 * rng.random(n)),
    })
    res = vb.rank_venues(panel, venues_, metric="mae", n_boot=500)
    assert res["venues"][0]["venue"] == "A"
    assert res["venues"][0]["p_rank1"] == pytest.approx(1.0)
    assert "closest venue: A" in res["verdict"]
    assert res["friedman"]["p"] is not None and res["friedman"]["p"] < 0.05


def test_synthetic_rankings_no_winner():
    rng = np.random.default_rng(1)
    n = 14
    # All venues essentially equal -> ranks shuffle -> no separation.
    panel, venues_ = _panel_from_distances({
        "A": list(0.050 + 0.02 * rng.random(n)),
        "B": list(0.050 + 0.02 * rng.random(n)),
        "C": list(0.050 + 0.02 * rng.random(n)),
    })
    res = vb.rank_venues(panel, venues_, metric="mae", n_boot=500)
    assert "no distinguishable winner" in res["verdict"]


def test_insufficient_common_support_verdict():
    panel, venues_ = _panel_from_distances({
        "A": [0.01, 0.02, 0.03],
        "B": [0.05, 0.06, 0.07],
        "C": [0.09, 0.10, 0.11],
    })  # only 3 fixtures < MIN_COMMON_FIXTURES
    res = vb.rank_venues(panel, venues_, metric="mae", n_boot=200)
    assert "insufficient common support" in res["verdict"]


def test_common_support_restricts_to_all_venues():
    panel = {
        "f0|b": {"A": 0.1, "B": 0.2, "C": 0.3},
        "f1|b": {"A": 0.1, "B": 0.2},            # C missing
        "f2|b": {"A": 0.1, "B": 0.2, "C": 0.3},
    }
    sup = vb.common_support(panel, ["A", "B", "C"])
    assert sup == ["f0|b", "f2|b"]


def test_within_obs_ranks_average_ties():
    panel = {"f0|b": {"A": 0.1, "B": 0.1, "C": 0.3}}
    ranks = vb.within_obs_ranks(panel, ["A", "B", "C"], ["f0|b"])
    assert ranks["A"][0] == pytest.approx(1.5)  # tie of ranks 1,2
    assert ranks["B"][0] == pytest.approx(1.5)
    assert ranks["C"][0] == pytest.approx(3.0)


def test_p_rank_first_sums_to_one():
    panel, venues_ = _panel_from_distances({
        "A": [0.01, 0.02, 0.03, 0.04],
        "B": [0.05, 0.06, 0.07, 0.08],
        "C": [0.02, 0.01, 0.04, 0.03],
    })
    p1 = vb.p_rank_first(panel, venues_, list(panel.keys()), n_boot=300)
    assert sum(p1.values()) == pytest.approx(1.0, abs=1e-9)


def test_bootstrap_deterministic():
    vals = {f"f{i}|b": 0.01 * i for i in range(12)}
    obs = list(vals.keys())
    a = vb.fixture_block_bootstrap(vals, obs, n_boot=500, seed=123)
    b = vb.fixture_block_bootstrap(vals, obs, n_boot=500, seed=123)
    assert a == b
    assert a[0] is not None and a[1] is not None and a[1] <= a[0] <= a[2]


def test_friedman_separates_and_permutation():
    panel, venues_ = _panel_from_distances({
        "A": [0.01, 0.012, 0.011, 0.013, 0.010],
        "B": [0.05, 0.052, 0.051, 0.053, 0.050],
        "C": [0.09, 0.092, 0.091, 0.093, 0.090],
    })
    stat, p = vb.friedman_test(panel, venues_, list(panel.keys()))
    assert p is not None and p < 0.05
    # Identical inputs -> not significant.
    flat, fv = _panel_from_distances({"A": [0.05] * 5, "B": [0.05] * 5, "C": [0.05] * 5})
    a = [flat[o]["A"] for o in flat]
    b = [flat[o]["B"] for o in flat]
    assert vb.paired_permutation_test(a, b) == pytest.approx(1.0)


def test_bh_fdr_monotone_and_none_passthrough():
    q = vb.bh_fdr([0.01, 0.02, 0.03, 0.04, 0.05])
    # q-values are non-decreasing in sorted p order and >= raw p.
    assert all(qi is not None for qi in q)
    assert q[0] <= q[-1]
    q2 = vb.bh_fdr([0.001, None, 0.5])
    assert q2[1] is None
    assert q2[0] is not None and q2[2] is not None


# --------------------------------------------------------------------------- #
# Book canonicalisation
# --------------------------------------------------------------------------- #


def test_canon_book_oddsapi_keys():
    assert venues.canon_book("betfair_ex_uk") == "Betfair"
    assert venues.canon_book("betfair_sb_uk") == "Betfair Sportsbook"
    assert venues.canon_book("paddypower") == "Paddy Power"
    assert venues.canon_book("sport888") == "888sport"
    assert venues.canon_book("") == "Unknown"
    # Exchange vs sportsbook must NOT merge.
    assert venues.canon_book("betfair_ex_uk") != venues.canon_book("betfair_sb_uk")


def test_is_exchange():
    assert venues.is_exchange("Betfair") is True
    assert venues.is_exchange("Smarkets") is True
    assert venues.is_exchange("Paddy Power") is False
