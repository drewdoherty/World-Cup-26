"""Tests for wca.models.totals_prior — SHADOW totals-market-implied lambda.

Covers (per the P6 quant-ladder task, docs/HANDOFF_2026-07-03.md §4):

* de-vig math: a known synthetic O/U quote devigs to the right implied
  probability, and the implied-lambda inversion round-trips exactly against a
  Poisson survival function built from a KNOWN lambda (no fabricated numbers —
  every "known" value here is computed with ``scipy.stats.poisson`` inline);
* the credibility-weight blend function (bounds, monotonicity, no-evidence
  fallback to the model-only lambda, home/away apportionment);
* the ``odds_snapshots``-row grouping helper (complete pairs only, incomplete
  quotes dropped, multiple bookmakers/lines all captured);
* shadow-logging plumbing in ``wca.modelpreds.build_predictions`` /
  ``load_totals_prior``: additive-only, schema-unchanged when no totals-quotes
  lookup is supplied (mirrors the existing ``gb_lambda_*`` shadow tests in
  ``tests/test_modelpreds.py``).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from typing import Dict

import pytest
from scipy.stats import poisson

from wca import modelpreds
from wca.models.totals_prior import (
    DEFAULT_CREDIBILITY_K,
    TotalsQuote,
    blend_lambda_home_away,
    blend_lambda_total,
    compute_totals_prior,
    credibility_weight,
    devig_over_prob,
    implied_lambda_from_over_prob,
    load_totals_quotes_by_match,
    market_implied_lambda,
    quotes_from_odds_rows,
)


# ---------------------------------------------------------------------------
# De-vig math.
# ---------------------------------------------------------------------------


def test_devig_over_prob_fair_book_unchanged():
    # A FAIR two-way book (no margin): decimal odds are exact reciprocals of a
    # probability pair summing to 1. De-vigging a fair book must return the
    # inputs essentially unchanged (booksum already 1).
    p_true = 0.4
    over_odds = 1.0 / p_true
    under_odds = 1.0 / (1.0 - p_true)
    p_over = devig_over_prob(over_odds, under_odds)
    assert abs(p_over - p_true) < 1e-9


def test_devig_over_prob_removes_margin():
    # A margined book: raw implied probabilities sum to > 1. Construct a book
    # with a KNOWN 5% overround split evenly, verify devig recovers the
    # underlying fair probability (multiplicative method: divide by booksum).
    p_over_true, p_under_true = 0.55, 0.45
    overround = 0.05
    # Inflate both raw implied probabilities proportionally by (1+overround),
    # which is exactly what the multiplicative method inverts.
    raw_over = p_over_true * (1.0 + overround)
    raw_under = p_under_true * (1.0 + overround)
    over_odds = 1.0 / raw_over
    under_odds = 1.0 / raw_under

    p_over = devig_over_prob(over_odds, under_odds, method="multiplicative")
    assert abs(p_over - p_over_true) < 1e-9


def test_implied_lambda_round_trips_against_known_poisson_sf():
    # Ground truth computed directly from scipy.stats.poisson (n=1 synthetic
    # check, exact by construction): pick a KNOWN lambda, compute the true
    # P(Over 2.5) = sf(2, lambda), then invert and recover the same lambda.
    known_lambda = 2.7
    line = 2.5
    p_over_known = float(poisson.sf(2, known_lambda))
    recovered = implied_lambda_from_over_prob(p_over_known, line, tol=1e-9)
    assert abs(recovered - known_lambda) < 1e-6


@pytest.mark.parametrize("known_lambda,line", [
    (1.0, 0.5),
    (1.8, 1.5),
    (2.5, 2.5),
    (3.9, 3.5),
    (5.2, 4.5),
])
def test_implied_lambda_round_trips_multiple_lines(known_lambda, line):
    k = int(math.floor(line))
    p_over_known = float(poisson.sf(k, known_lambda))
    recovered = implied_lambda_from_over_prob(p_over_known, line, tol=1e-9)
    assert abs(recovered - known_lambda) < 1e-6


def test_implied_lambda_rejects_degenerate_probabilities():
    with pytest.raises(ValueError):
        implied_lambda_from_over_prob(0.0, 2.5)
    with pytest.raises(ValueError):
        implied_lambda_from_over_prob(1.0, 2.5)
    with pytest.raises(ValueError):
        implied_lambda_from_over_prob(0.5, -1.0)


def test_market_implied_lambda_single_quote_matches_known_lambda():
    known_lambda = 3.1
    p_over_known = float(poisson.sf(2, known_lambda))
    over_odds = 1.0 / p_over_known
    under_odds = 1.0 / (1.0 - p_over_known)
    quote = TotalsQuote(line=2.5, over_odds=over_odds, under_odds=under_odds)

    result = market_implied_lambda([quote])
    assert result is not None
    lam, n = result
    assert n == 1
    assert abs(lam - known_lambda) < 1e-6


def test_market_implied_lambda_averages_multiple_quotes():
    # Two quotes implying different lambdas (e.g. two bookmakers disagreeing
    # slightly) should average, not just take the first.
    lam_a, lam_b = 2.4, 2.8
    p_a = float(poisson.sf(2, lam_a))
    p_b = float(poisson.sf(2, lam_b))
    q_a = TotalsQuote(line=2.5, over_odds=1.0 / p_a, under_odds=1.0 / (1.0 - p_a), venue="a")
    q_b = TotalsQuote(line=2.5, over_odds=1.0 / p_b, under_odds=1.0 / (1.0 - p_b), venue="b")

    result = market_implied_lambda([q_a, q_b])
    assert result is not None
    lam, n = result
    assert n == 2
    assert abs(lam - (lam_a + lam_b) / 2.0) < 1e-6


def test_market_implied_lambda_empty_returns_none():
    assert market_implied_lambda([]) is None


def test_market_implied_lambda_skips_bad_quotes_no_crash():
    # A quote with non-finite/invalid odds should be skipped, not raise.
    bad = TotalsQuote(line=2.5, over_odds=float("nan"), under_odds=1.5)
    good_lambda = 2.6
    p_good = float(poisson.sf(2, good_lambda))
    good = TotalsQuote(line=2.5, over_odds=1.0 / p_good, under_odds=1.0 / (1.0 - p_good))
    result = market_implied_lambda([bad, good])
    assert result is not None
    lam, n = result
    assert n == 1
    assert abs(lam - good_lambda) < 1e-6


# ---------------------------------------------------------------------------
# Credibility-weight blend.
# ---------------------------------------------------------------------------


def test_credibility_weight_bounds_and_monotone():
    assert credibility_weight(0) == 0.0
    w1 = credibility_weight(1)
    w5 = credibility_weight(5)
    w50 = credibility_weight(50)
    assert 0.0 < w1 < w5 < w50 < 1.0
    # As n -> inf, w -> 1.
    assert credibility_weight(1_000_000) > 0.999


def test_credibility_weight_matches_formula():
    n, k = 4.0, DEFAULT_CREDIBILITY_K
    expected = n / (n + k)
    assert abs(credibility_weight(n) - expected) < 1e-12


def test_credibility_weight_rejects_bad_inputs():
    with pytest.raises(ValueError):
        credibility_weight(1.0, k=0.0)
    with pytest.raises(ValueError):
        credibility_weight(-1.0)


def test_blend_lambda_total_no_market_evidence_returns_model_only():
    assert blend_lambda_total(2.5, None, 0) == 2.5
    assert blend_lambda_total(2.5, 3.0, 0) == 2.5


def test_blend_lambda_total_convex_combination():
    model, market, n = 2.0, 3.0, 9.0  # n == k -> w == 0.5
    blended = blend_lambda_total(model, market, int(n), k=9.0)
    assert abs(blended - 2.5) < 1e-9  # exact midpoint at w=0.5


def test_blend_lambda_total_more_quotes_more_market_weight():
    model, market = 2.0, 4.0
    b_few = blend_lambda_total(model, market, 1)
    b_many = blend_lambda_total(model, market, 100)
    # More market evidence should pull the blend further toward the market.
    assert model < b_few < b_many < market + 1e-9
    assert b_many > b_few


def test_blend_lambda_home_away_preserves_model_share_and_sums_to_total():
    lam_h_model, lam_a_model = 1.8, 1.0
    lambda_blend_total = 3.5
    lam_h, lam_a = blend_lambda_home_away(lam_h_model, lam_a_model, lambda_blend_total)
    assert abs((lam_h + lam_a) - lambda_blend_total) < 1e-9
    # Home share preserved: 1.8 / 2.8 of the model total.
    expected_share = 1.8 / 2.8
    assert abs(lam_h / lambda_blend_total - expected_share) < 1e-9


def test_blend_lambda_home_away_degenerate_zero_total_splits_evenly():
    lam_h, lam_a = blend_lambda_home_away(0.0, 0.0, 3.0)
    assert lam_h == pytest.approx(1.5)
    assert lam_a == pytest.approx(1.5)


def test_compute_totals_prior_no_quotes_falls_back_to_model():
    result = compute_totals_prior(1.6, 1.1, [])
    assert result.lambda_market_total is None
    assert result.n_market_quotes == 0
    assert result.weight_market == 0.0
    assert abs(result.lambda_blend_home - 1.6) < 1e-9
    assert abs(result.lambda_blend_away - 1.1) < 1e-9


def test_compute_totals_prior_with_quotes_shrinks_toward_market():
    lam_h_model, lam_a_model = 1.5, 1.0  # model total = 2.5
    market_total_known = 3.5
    p_over_known = float(poisson.sf(2, market_total_known))
    quote = TotalsQuote(
        line=2.5, over_odds=1.0 / p_over_known, under_odds=1.0 / (1.0 - p_over_known)
    )
    result = compute_totals_prior(lam_h_model, lam_a_model, [quote])
    assert result.lambda_market_total is not None
    assert abs(result.lambda_market_total - market_total_known) < 1e-6
    model_total = lam_h_model + lam_a_model
    blend_total = result.lambda_blend_home + result.lambda_blend_away
    # Blend must sit strictly between model and market (shrinkage, not
    # replacement) whenever there is at least one quote.
    assert min(model_total, market_total_known) < blend_total < max(
        model_total, market_total_known
    )


# ---------------------------------------------------------------------------
# odds_snapshots-row grouping helper.
# ---------------------------------------------------------------------------


def test_quotes_from_odds_rows_pairs_complete_over_under():
    rows = [
        {"market": "totals", "outcome_name": "Over", "outcome_point": 2.5,
         "decimal_odds": 2.0, "bookmaker_key": "bookieA"},
        {"market": "totals", "outcome_name": "Under", "outcome_point": 2.5,
         "decimal_odds": 1.9, "bookmaker_key": "bookieA"},
    ]
    quotes = quotes_from_odds_rows(rows)
    assert len(quotes) == 1
    assert quotes[0].line == 2.5
    assert quotes[0].over_odds == 2.0
    assert quotes[0].under_odds == 1.9
    assert quotes[0].venue == "bookieA"


def test_quotes_from_odds_rows_drops_incomplete_pairs():
    rows = [
        {"market": "totals", "outcome_name": "Over", "outcome_point": 2.5,
         "decimal_odds": 2.0, "bookmaker_key": "bookieA"},
        # No matching Under for bookieA -> must be dropped, not fabricated.
        {"market": "totals", "outcome_name": "Over", "outcome_point": 3.5,
         "decimal_odds": 3.0, "bookmaker_key": "bookieB"},
        {"market": "totals", "outcome_name": "Under", "outcome_point": 3.5,
         "decimal_odds": 1.3, "bookmaker_key": "bookieB"},
    ]
    quotes = quotes_from_odds_rows(rows)
    assert len(quotes) == 1
    assert quotes[0].line == 3.5
    assert quotes[0].venue == "bookieB"


def test_quotes_from_odds_rows_ignores_non_totals_markets():
    rows = [
        {"market": "h2h", "outcome_name": "Over", "outcome_point": 2.5,
         "decimal_odds": 2.0, "bookmaker_key": "bookieA"},
        {"market": "btts", "outcome_name": "Yes", "decimal_odds": 1.8,
         "bookmaker_key": "bookieA"},
    ]
    assert quotes_from_odds_rows(rows) == []


def test_quotes_from_odds_rows_multiple_bookmakers_and_lines():
    rows = [
        {"market": "totals", "outcome_name": "Over", "outcome_point": 2.5,
         "decimal_odds": 2.1, "bookmaker_key": "bookieA"},
        {"market": "totals", "outcome_name": "Under", "outcome_point": 2.5,
         "decimal_odds": 1.75, "bookmaker_key": "bookieA"},
        {"market": "totals", "outcome_name": "Over", "outcome_point": 2.5,
         "decimal_odds": 2.05, "bookmaker_key": "bookieB"},
        {"market": "totals", "outcome_name": "Under", "outcome_point": 2.5,
         "decimal_odds": 1.8, "bookmaker_key": "bookieB"},
    ]
    quotes = quotes_from_odds_rows(rows)
    assert len(quotes) == 2
    venues = {q.venue for q in quotes}
    assert venues == {"bookieA", "bookieB"}


# ---------------------------------------------------------------------------
# Shadow-logging plumbing (wca.modelpreds), mirroring the gb_lambda_* tests.
# ---------------------------------------------------------------------------

NOW = "2026-06-13T00:00:00"


@dataclass
class _Blend:
    home: str
    away: str
    blended: Dict[str, float]
    elo_map: Dict[str, float]
    dc_map: Dict[str, float]
    mkt_map: Dict[str, float]
    fx: Dict[str, object] = field(default_factory=dict)


class _FakeDC:
    """Stub with the drop-in DC interface _lambdas_for relies on."""

    def __init__(self, lam_h, lam_a):
        self._lam_h = lam_h
        self._lam_a = lam_a

    def expected_lambdas(self, home, away, neutral=True, warn=False):
        return self._lam_h, self._lam_a


def _blend(match_id="ev1"):
    triple = {"home": 0.4, "draw": 0.25, "away": 0.35}
    return _Blend(
        home="TeamA",
        away="TeamB",
        blended=triple,
        elo_map=triple,
        dc_map=triple,
        mkt_map=triple,
        fx={"event_id": match_id, "commence_time": "2026-06-13T16:00:00Z"},
    )


def test_no_totals_quotes_lookup_keeps_schema_unchanged():
    dc = _FakeDC(1.5, 1.2)
    payload = modelpreds.build_predictions([_blend()], NOW, dc_model=dc)
    (fx,) = payload["fixtures"]
    assert "tl_lambda_market_total" not in fx
    assert "tl_lambda_blend_home" not in fx


def test_totals_quotes_lookup_with_no_match_for_fixture_keeps_schema_unchanged():
    dc = _FakeDC(1.5, 1.2)
    payload = modelpreds.build_predictions(
        [_blend(match_id="ev1")], NOW, dc_model=dc,
        totals_quotes_by_match={"some-other-match": [
            TotalsQuote(line=2.5, over_odds=2.0, under_odds=1.9)
        ]},
    )
    (fx,) = payload["fixtures"]
    assert "tl_lambda_blend_home" not in fx


def test_totals_shadow_fields_persisted_additively():
    lam_h_model, lam_a_model = 1.5, 1.2
    dc = _FakeDC(lam_h_model, lam_a_model)
    known_market_total = 3.2
    p_over_known = float(poisson.sf(2, known_market_total))
    quote = TotalsQuote(
        line=2.5, over_odds=1.0 / p_over_known, under_odds=1.0 / (1.0 - p_over_known)
    )
    payload = modelpreds.build_predictions(
        [_blend(match_id="ev1")], NOW, dc_model=dc,
        totals_quotes_by_match={"ev1": [quote]},
    )
    (fx,) = payload["fixtures"]
    assert fx["tl_lambda_market_total"] == pytest.approx(known_market_total, abs=1e-4)
    assert fx["tl_n_market_quotes"] == 1
    assert 0.0 < fx["tl_weight_market"] < 1.0
    blend_total = fx["tl_lambda_blend_home"] + fx["tl_lambda_blend_away"]
    model_total = lam_h_model + lam_a_model
    assert min(model_total, known_market_total) < blend_total < max(
        model_total, known_market_total
    )
    # Original schema (model/elo/dc/market/lambda_home/lambda_away) untouched.
    assert fx["lambda_home"] == lam_h_model
    assert fx["lambda_away"] == lam_a_model


def test_totals_shadow_requires_dc_lambda_present():
    # No dc_model -> no lambda_home/lambda_away -> totals prior cannot be
    # computed even if quotes are supplied (never fabricate a blend without a
    # model anchor to shrink toward).
    payload = modelpreds.build_predictions(
        [_blend(match_id="ev1")], NOW,
        totals_quotes_by_match={"ev1": [
            TotalsQuote(line=2.5, over_odds=2.0, under_odds=1.9)
        ]},
    )
    (fx,) = payload["fixtures"]
    assert "tl_lambda_blend_home" not in fx


def test_load_totals_prior_round_trip():
    lam_h_model, lam_a_model = 1.4, 1.3
    dc = _FakeDC(lam_h_model, lam_a_model)
    known_market_total = 2.9
    p_over_known = float(poisson.sf(2, known_market_total))
    quote = TotalsQuote(
        line=2.5, over_odds=1.0 / p_over_known, under_odds=1.0 / (1.0 - p_over_known)
    )
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        payload = modelpreds.build_predictions(
            [_blend(match_id="ev1")], NOW, dc_model=dc,
            totals_quotes_by_match={"ev1": [quote]},
        )
        modelpreds.write_predictions(payload, latest_path=latest, log_path=log)

        loaded = modelpreds.load_totals_prior(latest)
        assert "TeamA vs TeamB" in loaded
        row = loaded["TeamA vs TeamB"]
        assert row["tl_lambda_market_total"] == pytest.approx(known_market_total, abs=1e-4)
        assert row["tl_n_market_quotes"] == 1


def test_load_totals_prior_empty_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        dc = _FakeDC(1.5, 1.2)
        payload = modelpreds.build_predictions([_blend()], NOW, dc_model=dc)
        modelpreds.write_predictions(payload, latest_path=latest, log_path=log)
        assert modelpreds.load_totals_prior(latest) == {}


def test_load_totals_prior_missing_file():
    assert modelpreds.load_totals_prior("/nonexistent/path.json") == {}


# ---------------------------------------------------------------------------
# load_totals_quotes_by_match (odds_snapshots DB reader).
# ---------------------------------------------------------------------------


def _make_odds_db(path: str, rows) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE odds_snapshots ("
        "ts_utc TEXT, source TEXT, match_id TEXT, market TEXT, "
        "selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    con.executemany(
        "INSERT INTO odds_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    con.commit()
    con.close()


def test_load_totals_quotes_by_match_reads_latest_complete_pairs():
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    raw_over = json.dumps({
        "bookmaker_key": "bookieA", "outcome_name": "Over", "outcome_point": 2.5,
    })
    raw_under = json.dumps({
        "bookmaker_key": "bookieA", "outcome_name": "Under", "outcome_point": 2.5,
    })
    rows = [
        (ts, "theoddsapi", "matchX", "totals", "Over 2.5", 2.1, raw_over),
        (ts, "theoddsapi", "matchX", "totals", "Under 2.5", 1.8, raw_under),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        _make_odds_db(db_path, rows)
        by_match = load_totals_quotes_by_match(db_path)
        assert "matchX" in by_match
        quotes = by_match["matchX"]
        assert len(quotes) == 1
        assert quotes[0].over_odds == 2.1
        assert quotes[0].under_odds == 1.8


def test_load_totals_quotes_by_match_ignores_stale_rows_outside_lookback():
    from datetime import datetime, timedelta, timezone

    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=999)).isoformat()
    raw_over = json.dumps({
        "bookmaker_key": "bookieA", "outcome_name": "Over", "outcome_point": 2.5,
    })
    raw_under = json.dumps({
        "bookmaker_key": "bookieA", "outcome_name": "Under", "outcome_point": 2.5,
    })
    rows = [
        (stale_ts, "theoddsapi", "matchStale", "totals", "Over 2.5", 2.1, raw_over),
        (stale_ts, "theoddsapi", "matchStale", "totals", "Under 2.5", 1.8, raw_under),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        _make_odds_db(db_path, rows)
        by_match = load_totals_quotes_by_match(db_path, lookback_hours=72.0)
        assert "matchStale" not in by_match


def test_load_totals_quotes_by_match_empty_db_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        _make_odds_db(db_path, [])
        assert load_totals_quotes_by_match(db_path) == {}
