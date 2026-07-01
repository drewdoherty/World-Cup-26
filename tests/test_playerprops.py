"""Tests for the Polymarket player-prop pricing model (wca.models.playerprops)."""
import math

import pytest

from wca.models import playerprops as pp
from wca.models.scorers import PlayerParams


# --------------------------------------------------------------------------- Poisson math


def test_poisson_at_least_basic_values():
    # P(0+) is always 1; P(1+) = 1 - e^-lam (anytime).
    assert pp.poisson_at_least(0, 1.3) == 1.0
    assert pp.poisson_at_least(1, 1.3) == pytest.approx(1 - math.exp(-1.3), rel=1e-12)
    # P(2+) = 1 - e^-lam(1 + lam)
    lam = 0.8
    assert pp.poisson_at_least(2, lam) == pytest.approx(
        1 - math.exp(-lam) * (1 + lam), rel=1e-12)
    # P(3+) = 1 - e^-lam(1 + lam + lam^2/2)
    assert pp.poisson_at_least(3, lam) == pytest.approx(
        1 - math.exp(-lam) * (1 + lam + lam * lam / 2.0), rel=1e-12)


def test_poisson_edge_cases():
    assert pp.poisson_at_least(1, 0.0) == 0.0
    assert pp.poisson_at_least(0, 0.0) == 1.0
    assert pp.poisson_at_least(1, -1.0) == 0.0


def test_poisson_at_least_monotonic_in_k_and_lambda():
    lam = 1.5
    p1 = pp.poisson_at_least(1, lam)
    p2 = pp.poisson_at_least(2, lam)
    p3 = pp.poisson_at_least(3, lam)
    assert p1 > p2 > p3                       # fewer at higher threshold
    assert pp.poisson_at_least(1, 2.0) > p1   # more at higher lambda


def test_poisson_matches_scipy_if_available():
    scipy = pytest.importorskip("scipy.stats")
    for lam in (0.3, 1.1, 2.7):
        for k in (1, 2, 3):
            assert pp.poisson_at_least(k, lam) == pytest.approx(
                float(scipy.poisson.sf(k - 1, lam)), rel=1e-10)


# --------------------------------------------------------------------------- lambda / shrink


def test_prop_lambda_minutes_proration():
    assert pp.prop_lambda(0.9, 90) == pytest.approx(0.9)
    assert pp.prop_lambda(0.9, 45) == pytest.approx(0.45)
    assert pp.prop_lambda(0.9, 0) == 0.0
    assert pp.prop_lambda(-1.0, 90) == 0.0


def test_shrink_rate_no_evidence_returns_prior():
    # sample_minutes=0 => weight 0 => prior, ignoring the empirical rate.
    assert pp.shrink_rate(5.0, 0.6, sample_minutes=0.0) == pytest.approx(0.6)


def test_shrink_rate_lots_of_evidence_approaches_empirical():
    # weight = n_eff/(n_eff+k); with 1000 full matches it is ~0.994 -> ~empirical.
    r = pp.shrink_rate(2.0, 0.6, sample_minutes=90 * 1000, shrink_k=6.0)
    assert r == pytest.approx(2.0, abs=0.02)
    # monotone: more evidence -> closer to empirical than less evidence.
    near = pp.shrink_rate(2.0, 0.6, sample_minutes=90 * 1000, shrink_k=6.0)
    far = pp.shrink_rate(2.0, 0.6, sample_minutes=90 * 3, shrink_k=6.0)
    assert abs(near - 2.0) < abs(far - 2.0)


def test_shrink_rate_none_empirical_is_prior():
    assert pp.shrink_rate(None, 0.7, sample_minutes=900) == 0.7


# --------------------------------------------------------------------------- PM label parsing


@pytest.mark.parametrize("label,expected", [
    ("Lionel Messi: 1+ goals", ("Lionel Messi", pp.MK_GOALS, 1)),
    ("Lautaro Martinez: 2+ goals", ("Lautaro Martinez", pp.MK_GOALS, 2)),
    ("Erling Haaland: 3+ goals", ("Erling Haaland", pp.MK_GOALS, 3)),
    ("Ousmane Dembélé: 2+ shots", ("Ousmane Dembélé", pp.MK_SHOTS, 2)),
    ("Kylian Mbappé: 2+ shots on target", ("Kylian Mbappé", pp.MK_SOT, 2)),
    ("Jude Bellingham: 1+ assists", ("Jude Bellingham", pp.MK_ASSISTS, 1)),
    ("Bukayo Saka: 1+ shot on target", ("Bukayo Saka", pp.MK_SOT, 1)),
])
def test_parse_pm_prop_label_recognised(label, expected):
    assert pp.parse_pm_prop_label(label) == expected


def test_parse_pm_prop_label_sot_before_shots():
    # The substring "shots" must NOT win over "shots on target".
    name, mt, thr = pp.parse_pm_prop_label("Vinicius Junior: 1+ shots on target")
    assert mt == pp.MK_SOT and thr == 1


@pytest.mark.parametrize("label", [
    "",
    "no colon here 1+ goals",
    "Player Name: anytime goalscorer",   # no "n+" form
    "Lionel Messi: 1+ goals + assists",  # combined market, must be rejected
    "Team: Over 2.5",
    "Just A Name:",
])
def test_parse_pm_prop_label_rejects(label):
    assert pp.parse_pm_prop_label(label) is None


# --------------------------------------------------------------------------- per-player pricing


def test_price_player_goals_uses_share_cascade():
    # No direct rate; goals priced off npxg_share x (team_lambda - pen_xg).
    rates = pp.PlayerPropRates(player="Lionel Messi", team="Argentina",
                               expected_minutes=90)
    priced = pp.price_player_props(rates, team_lambda=1.8, npxg_share=0.28,
                                   pen_xg=0.18, markets=(pp.MK_GOALS,))
    by = {(p.market_type, p.threshold): p for p in priced}
    g1 = by[(pp.MK_GOALS, 1)]
    expected_lam = max(1.8 - 0.18, 0.0) * 0.28
    assert g1.lam == pytest.approx(expected_lam, rel=1e-9)
    assert g1.prob == pytest.approx(1 - math.exp(-expected_lam), rel=1e-9)
    assert g1.rate_source == "share"
    # 1+ > 2+ > 3+
    assert by[(pp.MK_GOALS, 1)].prob > by[(pp.MK_GOALS, 2)].prob > by[(pp.MK_GOALS, 3)].prob


def test_penalty_taker_increases_goal_intensity():
    rates = pp.PlayerPropRates(player="X", team="T")
    no_pen = pp.price_player_props(rates, team_lambda=1.5, npxg_share=0.2,
                                   penalty_taker=False, markets=(pp.MK_GOALS,))[0]
    pen = pp.price_player_props(rates, team_lambda=1.5, npxg_share=0.2,
                                penalty_taker=True, markets=(pp.MK_GOALS,))[0]
    assert pen.lam > no_pen.lam


def test_direct_sot_rate_prorates_by_minutes():
    full = pp.PlayerPropRates(player="X", team="T", sot_p90=1.2,
                              expected_minutes=90, sample_minutes=900)
    sub = pp.PlayerPropRates(player="X", team="T", sot_p90=1.2,
                             expected_minutes=30, sample_minutes=900)
    p_full = pp.price_player_props(full, markets=(pp.MK_SOT,))[0]
    p_sub = pp.price_player_props(sub, markets=(pp.MK_SOT,))[0]
    assert p_sub.lam < p_full.lam
    assert p_sub.prob < p_full.prob


def test_thin_sample_sot_shrinks_toward_prior():
    prior = pp.PLAYER_P90_PRIORS[pp.MK_SOT]
    # A wild 5.0 SoT/90 from a tiny sample should be pulled toward the prior.
    thin = pp.PlayerPropRates(player="X", team="T", sot_p90=5.0,
                              expected_minutes=90, sample_minutes=45)
    rich = pp.PlayerPropRates(player="Y", team="T", sot_p90=5.0,
                              expected_minutes=90, sample_minutes=90 * 50)
    lam_thin = pp.price_player_props(thin, markets=(pp.MK_SOT,))[0].lam
    lam_rich = pp.price_player_props(rich, markets=(pp.MK_SOT,))[0].lam
    assert lam_thin < lam_rich          # thin pulled down toward prior
    assert lam_thin > prior * 90 / 90   # but still above the bare prior


def test_fallback_priors_when_no_data():
    rates = pp.PlayerPropRates(player="Nobody", team="T")
    priced = pp.price_player_props(rates, markets=(pp.MK_SHOTS, pp.MK_SOT, pp.MK_ASSISTS))
    by = {p.market_type: p for p in priced}
    # Shots prior used directly (no goals to derive from).
    assert by[pp.MK_SHOTS].lam == pytest.approx(pp.PLAYER_P90_PRIORS[pp.MK_SHOTS])
    assert by[pp.MK_SHOTS].rate_source == "prior"


# --------------------------------------------------------------------------- fixture pricing


def _scorers():
    return {
        "Argentina": [
            PlayerParams(name="Lionel Messi", team="Argentina", npxg_share=0.28,
                         penalty_taker=False, expected_minutes=80),
            PlayerParams(name="Lautaro Martinez", team="Argentina", npxg_share=0.22,
                         penalty_taker=True, expected_minutes=85),
        ],
        "France": [
            PlayerParams(name="Kylian Mbappé", team="France", npxg_share=0.30,
                         penalty_taker=True, expected_minutes=90),
        ],
    }


def test_price_fixture_props_returns_probs_for_all_players():
    out = pp.price_fixture_props(
        "Argentina", "France", lambda_home=1.7, lambda_away=1.5,
        scorers_by_team=_scorers())
    # Messi 1+ goals present and in (0,1).
    key = ("Lionel Messi", pp.MK_GOALS, 1)
    assert key in out
    assert 0.0 < out[key] < 1.0
    # Each player should have goals/shots/sot/assists thresholds.
    players = {k[0] for k in out}
    assert {"Lionel Messi", "Lautaro Martinez", "Kylian Mbappé"} <= players


def test_price_fixture_props_empty_without_sources():
    out = pp.price_fixture_props("A", "B", lambda_home=1.4, lambda_away=1.2)
    assert out == {}


def test_fixture_lambda_routing_by_team():
    # Same share, but the away team has a much higher lambda -> higher goal prob.
    scorers = {
        "A": [PlayerParams(name="Pa", team="A", npxg_share=0.25, expected_minutes=90)],
        "B": [PlayerParams(name="Pb", team="B", npxg_share=0.25, expected_minutes=90)],
    }
    out = pp.price_fixture_props("A", "B", lambda_home=0.8, lambda_away=2.4,
                                 scorers_by_team=scorers)
    assert out[("Pb", pp.MK_GOALS, 1)] > out[("Pa", pp.MK_GOALS, 1)]


# --------------------------------------------------------------------------- PM join


def _pm_market(label, yes_token, yes_price):
    """Build a minimal PM market dict shaped like the decoded Gamma response."""
    import json as _json
    return {
        "groupItemTitle": label,
        "clobTokenIds": _json.dumps([yes_token, "NO_TOKEN"]),
        "outcomes": _json.dumps(["Yes", "No"]),
        "outcomePrices": _json.dumps([str(yes_price), str(round(1 - yes_price, 4))]),
        "volumeNum": 1000.0,
        "bestBid": yes_price - 0.01,
        "bestAsk": yes_price,
    }


def test_join_matches_player_market_and_computes_edge():
    priced = pp.price_fixture_props_detailed(
        "Argentina", "France", lambda_home=1.7, lambda_away=1.5,
        scorers_by_team=_scorers())
    event = {"title": "Argentina vs. France - Player Props", "markets": [
        _pm_market("Lionel Messi: 1+ goals", "TOK_MESSI_1G", 0.30),
        _pm_market("Kylian Mbappé: 2+ shots on target", "TOK_MBAPPE_2SOT", 0.20),
        _pm_market("Lionel Messi: 1+ goals + assists", "TOK_COMBINED", 0.5),  # rejected
        _pm_market("Some Market: Over 2.5", "TOK_X", 0.4),                    # rejected
    ]}
    rows = pp.join_fixture_to_pm(priced, event)
    by = {(r.player, r.market_type, r.threshold): r for r in rows}
    assert ("Lionel Messi", pp.MK_GOALS, 1) in by
    assert ("Kylian Mbappé", pp.MK_SOT, 2) in by
    # combined + non-prop markets are dropped
    assert len(rows) == 2
    r = by[("Lionel Messi", pp.MK_GOALS, 1)]
    assert r.token_id == "TOK_MESSI_1G"
    assert r.pm_price == pytest.approx(0.30)
    assert r.edge == pytest.approx(r.model_prob - 0.30)
    assert r.match_kind == "exact"


def test_join_name_matching_across_spellings():
    # Model has "Sergino Dest"; PM lists "Sergiño Dest" -> exact via accent fold.
    # Model has "Brendan Aaronson"; PM lists "Brenden Aaronson" -> key fallback.
    priced = pp.price_fixture_props_detailed(
        "USA", "Wales", lambda_home=1.6, lambda_away=1.0,
        scorers_by_team={"USA": [
            PlayerParams(name="Sergino Dest", team="USA", npxg_share=0.08,
                         expected_minutes=90),
            PlayerParams(name="Brendan Aaronson", team="USA", npxg_share=0.10,
                         expected_minutes=90),
        ]})
    event = {"title": "USA vs. Wales - Player Props", "markets": [
        _pm_market("Sergiño Dest: 1+ shots on target", "TOK_DEST", 0.35),
        _pm_market("Brenden Aaronson: 1+ shots", "TOK_AARONSON", 0.55),
    ]}
    rows = pp.join_fixture_to_pm(priced, event)
    by = {(r.player, r.market_type, r.threshold): r for r in rows}
    dest = by[("Sergino Dest", pp.MK_SOT, 1)]
    assert dest.match_kind == "exact"           # accent fold makes it exact
    assert dest.token_id == "TOK_DEST"
    aaronson = by[("Brendan Aaronson", pp.MK_SHOTS, 1)]
    assert aaronson.match_kind == "key"          # first-name variant -> key match
    assert aaronson.token_id == "TOK_AARONSON"


def test_join_skips_markets_without_a_yes_price():
    priced = pp.price_fixture_props_detailed(
        "A", "B", lambda_home=1.5, lambda_away=1.5,
        scorers_by_team={"A": [PlayerParams(name="P", team="A", npxg_share=0.2)]})
    bad = _pm_market("P: 1+ goals", "TOK", 0.3)
    # Corrupt the price so no usable YES quote can be parsed.
    import json as _json
    bad["bestAsk"] = None
    bad["bestBid"] = None
    bad["outcomePrices"] = _json.dumps(["", ""])
    rows = pp.join_fixture_to_pm(priced, {"markets": [bad]})
    assert rows == []
