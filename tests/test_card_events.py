"""Tests for the match-event reference surface on the bet card (CARD-2a).

These cover the DISPLAY-ONLY corners O/U, cards O/U and BTTS rows surfaced by
:func:`wca.card.build_event_references` / :func:`wca.card.format_event_references`.
The contract under test is twofold:

1.  The reference markets reuse the already-fitted models (CornersModel driven
    by the DC expected goals, the previously-orphaned CardsModel, and BTTS from
    the reconciled scoreline matrix) and produce sane, self-consistent numbers.
2.  Surfacing them does NOT change any probability or Kelly math — the staked
    recommendations from :func:`build_card` are byte-for-byte unchanged.

Synthetic-slate helpers mirror ``tests/test_scores.py``; the model-free
assertions on the NB outputs mirror ``tests/test_props.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from wca.card import (
    DEFAULT_EVENT_CARDS_LINE,
    DEFAULT_EVENT_CORNERS_LINE,
    BlendWeights,
    MatchEventsReference,
    PoolConfig,
    build_card,
    build_event_references,
    fit_models,
    format_event_references,
)
from wca.models.props import CardsModel, CornersModel


# ---------------------------------------------------------------------------
# Synthetic slate (mirrors tests/test_scores.py).
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=200):
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


@pytest.fixture
def slate():
    rng = np.random.default_rng(42)
    results = _synthetic_results(rng)
    odds = _synthetic_odds()
    meta = _synthetic_fixtures_meta()
    weights = BlendWeights(elo=0.25, dc=0.25, market=0.50)
    models = fit_models(results, half_life_years=8.0)
    return models, odds, meta, weights


# ---------------------------------------------------------------------------
# build_event_references — shape, alignment, sanity.
# ---------------------------------------------------------------------------


def test_one_reference_per_market_fixture(slate):
    models, odds, meta, weights = slate
    refs = build_event_references(models, odds, meta, weights=weights)
    assert len(refs) == 1
    r = refs[0]
    assert isinstance(r, MatchEventsReference)
    assert r.home == "Alpha"
    assert r.away == "Bravo"
    assert r.commence_time == "2026-06-11T18:00:00Z"


def test_probabilities_are_valid(slate):
    models, odds, meta, weights = slate
    r = build_event_references(models, odds, meta, weights=weights)[0]
    for p in (r.corners_p_over, r.cards_p_over, r.btts):
        assert 0.0 <= p <= 1.0
    # Expected counts are positive and finite.
    assert r.corners_mu > 0.0
    assert r.cards_mu > 0.0
    # Default reference lines are surfaced verbatim.
    assert r.corners_line == DEFAULT_EVENT_CORNERS_LINE
    assert r.cards_line == DEFAULT_EVENT_CARDS_LINE


def test_corners_reference_matches_cornersmodel(slate):
    """Corners reference reuses CornersModel on the DC expected goals (as nextmatch)."""
    models, odds, meta, weights = slate
    r = build_event_references(models, odds, meta, weights=weights)[0]
    pred = models.dc.predict("Alpha", "Bravo", neutral=False, warn=False)
    lam_h = float(pred.lambda_home)
    lam_a = float(pred.lambda_away)
    cm = CornersModel()
    assert r.corners_mu == pytest.approx(cm.mean_total(lam_h, lam_a))
    assert r.corners_p_over == pytest.approx(
        cm.prob_over(DEFAULT_EVENT_CORNERS_LINE, lam_h, lam_a)
    )


def test_cards_reference_matches_cardsmodel_base_rate(slate):
    """Cards reference wires the orphaned CardsModel at its base rate."""
    models, odds, meta, weights = slate
    r = build_event_references(models, odds, meta, weights=weights)[0]
    km = CardsModel()
    assert r.cards_mu == pytest.approx(km.mean_total())
    assert r.cards_p_over == pytest.approx(km.prob_over(DEFAULT_EVENT_CARDS_LINE))


def test_btts_matches_reconciled_scoreline(slate):
    """BTTS reference equals the reconciled-matrix BTTS used by the scoreline card."""
    from wca.card import build_score_cards

    models, odds, meta, weights = slate
    r = build_event_references(models, odds, meta, weights=weights)[0]
    score_card = build_score_cards(models, odds, meta, weights=weights)[0]
    assert r.btts == pytest.approx(score_card.btts)


def test_custom_lines_are_respected(slate):
    models, odds, meta, weights = slate
    r = build_event_references(
        models, odds, meta, weights=weights, corners_line=9.5, cards_line=4.5
    )[0]
    cm, km = CornersModel(), CardsModel()
    pred = models.dc.predict("Alpha", "Bravo", neutral=False, warn=False)
    lam_h, lam_a = float(pred.lambda_home), float(pred.lambda_away)
    assert r.corners_line == 9.5
    assert r.cards_line == 4.5
    assert r.corners_p_over == pytest.approx(cm.prob_over(9.5, lam_h, lam_a))
    assert r.cards_p_over == pytest.approx(km.prob_over(4.5))
    # A higher line is harder to clear: over-prob must drop vs the default line.
    base = build_event_references(models, odds, meta, weights=weights)[0]
    assert r.corners_p_over < base.corners_p_over
    assert r.cards_p_over < base.cards_p_over


def test_empty_slate_returns_empty():
    models = fit_models(_synthetic_results(np.random.default_rng(0)))
    empty_odds = pd.DataFrame(
        columns=[
            "event_id", "home_team", "away_team", "commence_time",
            "market", "bookmaker_key", "outcome_name", "decimal_odds",
        ]
    )
    refs = build_event_references(models, empty_odds, _synthetic_fixtures_meta())
    assert refs == []


# ---------------------------------------------------------------------------
# DISPLAY ONLY: surfacing references does not change the staked card.
# ---------------------------------------------------------------------------


def test_references_do_not_alter_recommendations(slate):
    """The +EV / Kelly card must be identical whether or not refs are built."""
    models, odds, meta, weights = slate
    pools = [PoolConfig(name="sb", bankroll=1000.0)]

    recs_before = build_card(models, odds, pools, meta, weights=weights, min_edge=-1.0)
    snapshot = [
        (r.match_id, r.selection, r.model_prob, r.edge, r.best_odds,
         dict(r.stakes))
        for r in recs_before
    ]

    # Build references in between — must not mutate models or recs.
    build_event_references(models, odds, meta, weights=weights)

    recs_after = build_card(models, odds, pools, meta, weights=weights, min_edge=-1.0)
    after = [
        (r.match_id, r.selection, r.model_prob, r.edge, r.best_odds,
         dict(r.stakes))
        for r in recs_after
    ]
    assert snapshot == after


# ---------------------------------------------------------------------------
# format_event_references — clearly labelled reference / non-staked.
# ---------------------------------------------------------------------------


def test_format_flags_reference_not_staked(slate):
    models, odds, meta, weights = slate
    refs = build_event_references(models, odds, meta, weights=weights)
    text = format_event_references(refs)
    assert "REFERENCE" in text
    assert "NOT STAKED" in text
    assert "Alpha vs Bravo" in text
    assert "corners O/U" in text
    assert "cards O/U" in text
    assert "BTTS" in text
    # No per-fixture stake amount or edge figure should be rendered — these are
    # reference rows, not sized picks. (The header explicitly says NOT STAKED /
    # no edge, so we check the per-fixture body lines carry no £ or edge value.)
    body = "\n".join(
        ln for ln in text.splitlines() if ln.startswith("    ")
    )
    assert "£" not in body
    assert "edge" not in body.lower()
    assert "stake" not in body.lower()


def test_format_empty_is_safe():
    assert "No match-event references" in format_event_references([])
