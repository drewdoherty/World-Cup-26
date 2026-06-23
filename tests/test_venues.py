"""Tests for venue/geography-aware host advantage (wca.models.venues) and its
opt-in wiring into the advancement prob_fn."""

from __future__ import annotations

import pandas as pd
import pytest

from wca.advancement import HOST_NATIONS, make_prob_fn
from wca.card import BlendWeights, fit_models, fixture_blends, _elo_initial_ratings_from_dc_prior
from wca.models import venues as V


# ---------------------------------------------------------------------------
# Venue table & point function.
# ---------------------------------------------------------------------------


def test_venue_table_has_host_cities_and_countries():
    ven = V.load_venues()
    assert "Mexico City" in ven
    assert ven["Mexico City"].country == "Mexico"
    assert ven["Mexico City"].altitude_m > 2000
    countries = {v.country for v in ven.values()}
    assert {"Mexico", "United States", "Canada"} <= countries


def test_host_points_default_reproduces_legacy_bonus():
    # factor=1.0, no altitudes -> exactly the base home advantage.
    assert V.host_advantage_points(100.0) == pytest.approx(100.0)


def test_host_points_dilution():
    assert V.host_advantage_points(100.0, factor=0.5) == pytest.approx(50.0)


def test_altitude_taxes_sea_level_visitor_only_above_threshold():
    # Mexico City (2240 m) vs a sea-level side: a real penalty.
    high = V.altitude_penalty_points(2240.0, 10.0)
    assert high > 0
    # A low venue (30 m) gap is below threshold -> no penalty.
    assert V.altitude_penalty_points(30.0, 10.0) == 0.0
    # A visitor already at altitude is not taxed.
    assert V.altitude_penalty_points(2240.0, 2600.0) == 0.0


def test_host_points_combines_dilution_and_altitude():
    pts = V.host_advantage_points(
        100.0, factor=0.5, venue_altitude_m=2240.0, visitor_home_altitude_m=10.0
    )
    assert pts > 50.0  # dilution floor plus altitude term


# ---------------------------------------------------------------------------
# prob_fn opt-in wiring (uses a tiny fitted model).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_models():
    # A minimal results frame covering a few of the host/opponent teams so the
    # Elo + DC models have ratings to query. Two host nations + opponents.
    rows = []
    teams = ["Mexico", "United States", "Brazil", "Japan", "Canada", "Norway"]
    import itertools

    for d, (h, a) in enumerate(itertools.cycle(itertools.permutations(teams, 2))):
        rows.append(
            {
                "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                "home_team": h,
                "away_team": a,
                "home_score": (d % 3),
                "away_score": ((d + 1) % 2),
                "tournament": "Friendly",
                "neutral": False,
            }
        )
        if d > 120:
            break
    return fit_models(pd.DataFrame(rows))


def test_prob_fn_venue_aware_off_matches_legacy(tiny_models):
    legacy = make_prob_fn(tiny_models, venue_aware=False)
    # Mexico (host) vs Brazil group game.
    assert make_prob_fn(tiny_models, venue_aware=False)("Mexico", "Brazil", False) == \
        legacy("Mexico", "Brazil", False)


def test_prob_fn_venue_aware_dilutes_host_edge(tiny_models):
    legacy = make_prob_fn(tiny_models, venue_aware=False)
    aware = make_prob_fn(tiny_models, venue_aware=True, host_factor=0.5)
    # USA hosting Norway at a sea-level venue: dilution should shrink the host's
    # win probability relative to the full-bonus legacy path.
    p_legacy = legacy("United States", "Norway", False)[0]
    p_aware = aware("United States", "Norway", False)[0]
    assert p_aware < p_legacy


def test_card_fixture_blends_pass_diluted_host_points(monkeypatch):
    calls = []

    def fake_elo_probs(models, home, away, neutral, host=None, host_points=None):
        calls.append(
            {
                "home": home,
                "away": away,
                "neutral": neutral,
                "host": host,
                "host_points": host_points,
            }
        )
        return (0.5, 0.25, 0.25)

    def fake_dc_probs(models, home, away, neutral):
        return (0.4, 0.3, 0.3)

    monkeypatch.setattr("wca.card.elo_probs", fake_elo_probs)
    monkeypatch.setattr("wca.card.dc_probs", fake_dc_probs)

    class Rater:
        home_advantage = 100.0

    class Models:
        rater = Rater()

    odds = pd.DataFrame(
        [
            {
                "event_id": "m1",
                "home_team": "United States",
                "away_team": "Norway",
                "commence_time": "2026-06-13T00:00:00Z",
                "bookmaker_key": "book",
                "market": "h2h",
                "outcome_name": name,
                "decimal_odds": odds,
            }
            for name, odds in [
                ("United States", 2.0),
                ("Draw", 3.2),
                ("Norway", 4.0),
            ]
        ]
    )
    fixtures = pd.DataFrame(
        [
            {
                "home_team": "United States",
                "away_team": "Norway",
                "neutral": True,
                "country": "United States",
            }
        ]
    )

    blends = fixture_blends(
        Models(), odds, fixtures, BlendWeights(), neutral_host_factor=0.5
    )

    assert len(blends) == 1
    assert calls[0]["host"] == "United States"
    assert calls[0]["host_points"] == pytest.approx(50.0)


def test_elo_seed_from_dc_prior_is_non_flat_and_scaled():
    seeds = _elo_initial_ratings_from_dc_prior(
        prior_scale=0.15,
        points_per_dc_prior=400.0,
    )
    assert seeds["Brazil"] > 1500.0
    assert seeds["Haiti"] < 1500.0

    stronger = _elo_initial_ratings_from_dc_prior(
        prior_scale=0.30,
        points_per_dc_prior=400.0,
    )
    assert (stronger["Brazil"] - 1500.0) == pytest.approx(
        2.0 * (seeds["Brazil"] - 1500.0)
    )


def test_prob_fn_knockout_is_neutral_regardless_of_flag(tiny_models):
    aware = make_prob_fn(tiny_models, venue_aware=True)
    legacy = make_prob_fn(tiny_models, venue_aware=False)
    # Knockout => host=None => the two paths agree exactly.
    assert aware("Mexico", "Brazil", True) == legacy("Mexico", "Brazil", True)


def test_host_nations_are_the_three_co_hosts():
    assert set(HOST_NATIONS) == {"United States", "Mexico", "Canada"}
