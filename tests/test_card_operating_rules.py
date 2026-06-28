"""Tests for the operating-rules card refactor (rules 2/3/4).

Covers the selection rule (hit-probability ranking + mispriced-minnow longshot
cut), the further-out tilt (imminent-fixture edge discount), and the cross-venue
sizing (venue tag, per-venue deployment split, whole-book exposure across
venues). The bankroll/rung-0 rule (rule 1) is exercised in
``tests/test_pool_bankroll.py``.

These are pure-logic tests: they construct ``Recommendation`` objects directly
(no model fitting / IO), so they pin the ranking and cut semantics exactly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wca.card import (
    FURTHER_OUT_HOURS,
    IMMINENT_EDGE_DISCOUNT,
    IMMINENT_HOURS,
    LONGSHOT_PROB,
    SELECTION_MIN_PROB,
    Recommendation,
    build_card,
    classify_outcome,
    fit_models,
    hours_to_kickoff,
    rank_card,
    venue_deployment,
    venue_of,
    whole_book_exposure,
    PoolConfig,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _rec(
    *,
    match="A vs B",
    selection="home",
    team="A",
    odds=2.0,
    model_prob=0.55,
    edge=0.05,
    category="favourite",
    venue="smarkets",
    stakes=None,
    commence="2026-07-01T18:00:00Z",
    h2k=None,
    imminent=False,
    raw_edge=None,
):
    return Recommendation(
        match_id="evt",
        match_desc=match,
        commence_time=commence,
        selection=selection,
        selection_team=team,
        best_book=venue,
        best_odds=odds,
        model_prob=model_prob,
        market_prob=model_prob,
        elo_prob=model_prob,
        dc_prob=model_prob,
        edge=edge,
        ev_per_unit=edge,
        stakes=stakes if stakes is not None else {"main": 10.0},
        venue=venue,
        raw_edge=raw_edge if raw_edge is not None else edge,
        hours_to_kickoff=h2k,
        imminent=imminent,
        category=category,
    )


# ---------------------------------------------------------------------------
# Rule 2 — selection rule: hit-probability primary, longshot cut.
# ---------------------------------------------------------------------------


class TestSelectionRule:
    def test_longshot_deprioritised_vs_favourite_even_with_higher_ev(self):
        """The headline rule: a higher-EV longshot ranks BELOW a lower-EV
        favourite — and the mispriced-minnow longshot is CUT entirely."""
        fav = _rec(
            match="Fav match", selection="home", team="Fav", odds=1.8,
            model_prob=0.60, edge=0.05, category="favourite",
        )
        longshot = _rec(
            match="Dog match", selection="away", team="Dog", odds=9.0,
            model_prob=0.15, edge=0.20, category="longshot",  # HIGHER EV
        )
        ranked = rank_card([longshot, fav])  # deliberately longshot first
        # The favourite is the only STAKED pick; the longshot is cut despite EV.
        assert [r.selection_team for r in ranked.picks] == ["Fav"]
        assert [r.selection_team for r in ranked.cut] == ["Dog"]
        cut = ranked.cut[0]
        assert cut.cut is True
        assert cut.edge == pytest.approx(0.20)  # EV preserved + visible
        assert "longshot" in cut.cut_reason.lower() or "floor" in cut.cut_reason.lower()
        # The cut bet's stake is zeroed so a sizer can't deploy it.
        assert all(v == 0.0 for v in cut.stakes.values())

    def test_below_probability_floor_is_cut(self):
        below = _rec(
            selection="away", team="Tiny", odds=12.0,
            model_prob=SELECTION_MIN_PROB - 0.01, edge=0.30, category="second_favourite",
        )
        ranked = rank_card([below])
        assert ranked.picks == []
        assert len(ranked.cut) == 1
        assert "floor" in ranked.cut[0].cut_reason.lower()

    def test_short_high_prob_longshot_survives(self):
        """A longshot whose model prob clears LONGSHOT_PROB is NOT a minnow."""
        short = _rec(
            selection="away", team="Live", odds=3.2,
            model_prob=LONGSHOT_PROB + 0.05, edge=0.04, category="longshot",
        )
        ranked = rank_card([short])
        assert [r.selection_team for r in ranked.picks] == ["Live"]
        assert ranked.cut == []

    def test_ranking_order_fav_draw_secondfav(self):
        fav = _rec(team="Fav", model_prob=0.55, edge=0.03, category="favourite")
        draw = _rec(team="Draw", selection="draw", model_prob=0.28, edge=0.03,
                    category="structural_draw")
        second = _rec(team="2nd", model_prob=0.35, edge=0.10, category="second_favourite")
        ranked = rank_card([second, draw, fav])
        assert [r.selection_team for r in ranked.picks] == ["Fav", "Draw", "2nd"]

    def test_classify_outcome_buckets(self):
        mkt = {"home": 0.55, "draw": 0.27, "away": 0.18}
        assert classify_outcome("home", 0.55, mkt) == "favourite"
        assert classify_outcome("draw", 0.27, mkt) == "structural_draw"
        assert classify_outcome("away", 0.18, mkt) == "longshot"
        # A draw outside the structural band falls back to market-rank logic;
        # here the draw is the 2nd-shortest market price -> second_favourite.
        assert classify_outcome("draw", 0.15, mkt) == "second_favourite"


# ---------------------------------------------------------------------------
# Rule 3 — further-out tilt.
# ---------------------------------------------------------------------------


class TestFurtherOutTilt:
    def test_hours_to_kickoff_basic(self):
        h = hours_to_kickoff(
            "2026-07-01T18:00:00Z", now="2026-07-01T12:00:00Z"
        )
        assert h == pytest.approx(6.0)

    def test_hours_to_kickoff_unparseable_returns_none(self):
        assert hours_to_kickoff("not-a-date", now="2026-07-01T12:00:00Z") is None

    def test_imminent_edge_discounted_via_build_card(self):
        """A large model edge on an IMMINENT (<6h) fixture is discounted before
        it sizes — flagged as likely model error, not true mispricing."""
        rng = np.random.default_rng(3)
        results = _synthetic_results(rng)
        meta = _synthetic_fixtures_meta()
        models = fit_models(results, half_life_years=8.0)
        pools = [PoolConfig(name="main", bankroll=1000.0)]

        # Same fixture/odds, two reference times: imminent vs far-out.
        odds = _synthetic_odds(commence="2026-07-01T18:00:00Z")
        recs_imminent = build_card(
            models, odds, pools, meta, min_edge=-1.0, now="2026-07-01T15:00:00Z",
        )
        recs_far = build_card(
            models, odds, pools, meta, min_edge=-1.0, now="2026-06-20T15:00:00Z",
        )
        by_far = {r.selection: r for r in recs_far}
        for r in recs_imminent:
            assert r.imminent is True
            far = by_far[r.selection]
            assert far.imminent is False
            if r.raw_edge is not None and r.raw_edge > 0:
                # Imminent edge is the raw edge times the discount factor.
                assert r.edge == pytest.approx(r.raw_edge * IMMINENT_EDGE_DISCOUNT)
                # ... and strictly smaller than the un-discounted far-out edge.
                assert r.edge < far.edge or far.edge <= 0

    def test_further_out_flag_threshold(self):
        far = _rec(h2k=FURTHER_OUT_HOURS + 1, imminent=False)
        assert far.hours_to_kickoff >= FURTHER_OUT_HOURS


# ---------------------------------------------------------------------------
# Rule 4 — cross-venue: tag, deployment split, whole-book exposure.
# ---------------------------------------------------------------------------


class TestCrossVenue:
    def test_venue_of_normalises_keys(self):
        assert venue_of("smarkets") == "smarkets"
        assert venue_of("betfair_ex_uk") == "betfair"
        assert venue_of("polymarket") == "polymarket"
        assert venue_of("williamhill") == "smarkets"  # default exchange

    def test_venue_deployment_split(self):
        recs = [
            _rec(match="m1", venue="smarkets", stakes={"main": 10.0}),
            _rec(match="m2", venue="betfair", stakes={"main": 5.0}),
            _rec(match="m3", venue="smarkets", stakes={"main": 4.0}),
            _rec(match="m4", venue="polymarket", stakes={"main": 8.0}),
        ]
        split = venue_deployment(recs, "main")
        assert split == {"betfair": 5.0, "polymarket": 8.0, "smarkets": 14.0}

    def test_whole_book_exposure_combines_outcomes_per_match(self):
        """Two outcomes on the SAME match, on DIFFERENT venues, combine into one
        cross-venue exposure measured against the cap."""
        recs = [
            _rec(match="X vs Y", selection="home", team="X", venue="smarkets",
                 stakes={"main": 30.0}),
            _rec(match="X vs Y", selection="draw", team="Draw", venue="betfair",
                 stakes={"main": 25.0}),
            _rec(match="P vs Q", selection="home", team="P", venue="polymarket",
                 stakes={"main": 5.0}),
        ]
        book = whole_book_exposure(recs, bankroll=1000.0, cap_fraction=0.05)
        by_match = {b["match"]: b for b in book}
        xy = by_match["X vs Y"]
        assert xy["stake_at_risk"] == pytest.approx(55.0)  # 30 + 25 combined
        assert set(xy["venues"]) == {"smarkets", "betfair"}
        assert xy["n_legs"] == 2
        assert xy["cap"] == pytest.approx(50.0)  # 5% of 1000
        assert xy["over_cap"] is True            # 55 > 50
        assert by_match["P vs Q"]["over_cap"] is False

    def test_cut_recs_excluded_from_split_and_exposure(self):
        good = _rec(match="g", venue="smarkets", stakes={"main": 10.0})
        bad = _rec(match="b", venue="betfair", stakes={"main": 10.0})
        bad.cut = True
        bad.stakes = {"main": 0.0}
        assert venue_deployment([good, bad], "main") == {"smarkets": 10.0}
        book = whole_book_exposure([good, bad], bankroll=1000.0)
        assert [b["match"] for b in book] == ["g"]


# ---------------------------------------------------------------------------
# Synthetic fixtures (mirrors tests/test_scores.py conventions).
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=200):
    teams = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    base = pd.Timestamp("2022-01-01")
    rows = []
    for k in range(n):
        i, j = rng.choice(len(teams), size=2, replace=False)
        rows.append({
            "date": base + pd.Timedelta(days=int(k)),
            "home_team": teams[i], "away_team": teams[j],
            "home_score": int(rng.poisson(1.5)),
            "away_score": int(rng.poisson(1.1)),
            "tournament": "Friendly", "neutral": False,
        })
    return pd.DataFrame(rows)


def _synthetic_odds(commence="2026-07-01T18:00:00Z"):
    rows = []
    fixture = dict(
        event_id="evt1", home_team="Alpha", away_team="Bravo",
        commence_time=commence, market="h2h",
    )
    book_prices = {
        "smarkets": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.60},
        "betfair_ex_uk": {"Alpha": 2.05, "Draw": 3.30, "Bravo": 3.80},
    }
    for book, prices in book_prices.items():
        for name, odd in prices.items():
            rows.append(dict(fixture, bookmaker_key=book, outcome_name=name,
                             decimal_odds=odd))
    return pd.DataFrame(rows)


def _synthetic_fixtures_meta():
    return pd.DataFrame([{
        "home_team": "Alpha", "away_team": "Bravo", "neutral": False,
        "country": "", "home_score": np.nan, "away_score": np.nan,
    }])


class TestFairKellyDisplay:
    """Display overhaul: model fair % + fair decimal odds (+ ¼-Kelly) shown
    next to every bet-related number in the card renderers."""

    def test_format_card_shows_fair_odds(self):
        from wca.card import format_card
        from wca.models.scores import fair_odds

        rec = _rec(model_prob=0.55, odds=2.0, edge=0.05)
        out = format_card([rec], [PoolConfig(name="main", bankroll=1000.0)])
        # model % already shown; the fair decimal odds (1/p) must now appear too.
        assert "55.0%" in out
        assert ("fair %.2f" % fair_odds(0.55)) in out

    def test_format_ranked_card_shows_fair_odds_and_kelly(self):
        from wca.card import format_ranked_card
        from wca.models.scores import fair_odds

        rec = _rec(model_prob=0.55, odds=2.0, edge=0.05, stakes={"main": 12.34})
        ranked = rank_card([rec])
        assert ranked.picks, "favourite should be staked, not cut"
        pool = PoolConfig(name="main", bankroll=1000.0)
        out = format_ranked_card(ranked, pool)
        assert "55.0%" in out                              # model fair %
        assert ("fair %.2f" % fair_odds(0.55)) in out      # fair decimal odds
        assert "£12.34" in out                             # ¼-Kelly stake (preserved)

    def test_format_ranked_card_cut_list_shows_fair_odds(self):
        from wca.card import format_ranked_card
        from wca.models.scores import fair_odds

        # A mispriced-minnow longshot is CUT (no stake) but still shows fair odds.
        longshot = _rec(
            match="Dog match", selection="away", team="Dog", odds=9.0,
            model_prob=0.15, edge=0.20, category="longshot",
        )
        ranked = rank_card([longshot])
        assert ranked.cut, "longshot should be cut"
        out = format_ranked_card(ranked, PoolConfig(name="main", bankroll=1000.0))
        assert ("fair %.2f" % fair_odds(0.15)) in out
        # No edge -> no bet honesty: cut reason still present.
        assert "EV)" in out

    def test_format_scores_kelly_only_when_bankroll_given(self):
        from wca.card import format_scores
        from wca.markets import kelly as kelly_mod
        from wca.models.scores import ScorelineCard
        import numpy as _np

        # Minimal hand-built card: one scoreline at p=0.2, min_edge 0.02.
        mat = _np.zeros((2, 2))
        mat[1, 0] = 0.2
        mat[0, 0] = 0.8
        card = ScorelineCard(
            home="A", away="B", matrix=mat,
            top_scorelines=[(1, 0, 0.2)],
            over_under={2.5: (0.0, 1.0)}, btts=0.0,
            one_x_two=(0.2, 0.8, 0.0), min_edge=0.02,
        )
        # Without a bankroll: no ¼-K token (unchanged REFERENCE behaviour).
        plain = format_scores([card])
        assert "¼-K" not in plain
        assert "fair" in plain  # fair % + fair odds still shown
        # With a bankroll: ¼-Kelly stake at the min back price, same kernel.
        sized = format_scores([card], bankroll=1500.0)
        back = card.min_price(0.2, 0.02)
        expected = kelly_mod.stake(0.2, back, 1500.0)
        assert expected > 0
        assert ("¼-K £%.2f" % expected) in sized
        assert "REFERENCE" in sized


def test_build_card_tags_venue_from_best_price():
    """build_card carries the best-price venue tag onto each recommendation."""
    rng = np.random.default_rng(11)
    results = _synthetic_results(rng)
    odds = _synthetic_odds()
    meta = _synthetic_fixtures_meta()
    models = fit_models(results, half_life_years=8.0)
    pools = [PoolConfig(name="main", bankroll=1000.0)]
    recs = build_card(models, odds, pools, meta, min_edge=-1.0,
                      now="2026-06-20T12:00:00Z")
    assert recs
    for r in recs:
        assert r.venue in {"smarkets", "betfair", "polymarket"}
        assert r.category in {
            "favourite", "second_favourite", "structural_draw", "longshot",
        }
