"""Tests for the tournament-advancement edge engine (wca.advancement).

These exercise the deterministic core without any network or ~2-minute model
fit: the simulator is driven by a small synthetic ``prob_fn`` and the Polymarket
matching logic is fed hand-built event fixtures (including name variants).
"""
from __future__ import annotations

import math
import os
from typing import Dict, List

import pandas as pd
import pytest

# Canonical committed results dataset (the un-cleaned ``results.csv`` is a
# download artifact and is not checked into the repo). Anchored to the repo
# root so the test is independent of the working directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RESULTS_CSV = os.path.join(_REPO_ROOT, "data", "raw", "martj42_cleaned.csv")

from wca.advancement import (
    HOST_NATIONS,
    PM_POOL_BANKROLL,
    PM_STAGE_EVENTS,
    WC2026_GROUPS,
    _market_consensus_lookup,
    _no_ask,
    _yes_mid,
    compare_to_polymarket,
    make_prob_fn,
    pm_taker_fee,
)
from wca.card import BlendWeights
from wca.data.results import load_results
from wca.sim.tournament2026 import GROUP_LETTERS, TournamentSimulator


# ---------------------------------------------------------------------------
# Group-table integrity.
# ---------------------------------------------------------------------------


def test_groups_have_12_groups_of_4_unique_teams():
    assert set(WC2026_GROUPS) == set(GROUP_LETTERS)
    all_teams: List[str] = []
    for g in GROUP_LETTERS:
        assert len(WC2026_GROUPS[g]) == 4, "group %s must have 4 teams" % g
        all_teams.extend(WC2026_GROUPS[g])
    assert len(all_teams) == 48
    assert len(set(all_teams)) == 48, "team names must be globally unique"


def test_all_48_teams_appear_in_scheduled_fixtures():
    """Every group team must be a real scheduled WC2026 fixture participant.

    Also verifies the embedded group table is *consistent* with the schedule:
    every scheduled World-Cup fixture is intra-group and each group has exactly
    6 fixtures (a single round robin).
    """
    df = load_results(_RESULTS_CSV)
    # Use the FULL 2026 group-stage schedule (played + unplayed). Once the
    # tournament is under way, completed fixtures have scores, so the old
    # NA-score filter would undercount each group's round-robin (no longer 6
    # *unplayed*). Filter by the 2026 season so historical World Cups are
    # excluded but already-played 2026 group games are still counted.
    _yr = pd.to_datetime(df["date"], errors="coerce").dt.year
    wc = df[(df["tournament"] == "FIFA World Cup") & (_yr == 2026)]
    fixture_teams = set(wc["home_team"]).union(set(wc["away_team"]))

    group_teams = {t for ts in WC2026_GROUPS.values() for t in ts}
    assert group_teams == fixture_teams, (
        "group table teams must match scheduled-fixture teams exactly; "
        "missing=%r extra=%r"
        % (fixture_teams - group_teams, group_teams - fixture_teams)
    )

    team_to_group: Dict[str, str] = {
        t: g for g, ts in WC2026_GROUPS.items() for t in ts
    }
    per_group: Dict[str, int] = {g: 0 for g in GROUP_LETTERS}
    for _, r in wc.iterrows():
        h, a = r["home_team"], r["away_team"]
        assert team_to_group[h] == team_to_group[a], (
            "scheduled fixture %s vs %s crosses groups" % (h, a)
        )
        per_group[team_to_group[h]] += 1
    assert all(per_group[g] == 6 for g in GROUP_LETTERS), per_group


def test_hosts_are_in_the_group_table():
    teams = {t for ts in WC2026_GROUPS.values() for t in ts}
    for host in HOST_NATIONS:
        assert host in teams


# ---------------------------------------------------------------------------
# A synthetic prob_fn / simulator: invariants on the REAL tournament2026 API.
# ---------------------------------------------------------------------------


def _ranked_groups() -> Dict[str, List[str]]:
    """A1..L4 placeholder groups (48 unique synthetic teams)."""
    return {g: ["%s%d" % (g, i + 1) for i in range(4)] for g in GROUP_LETTERS}


def _favour_lower_index_prob_fn(groups: Dict[str, List[str]]):
    """prob_fn favouring the team with the smaller global index (stronger)."""
    order = {t: i for i, t in enumerate(t for g in GROUP_LETTERS for t in groups[g])}

    def prob_fn(a: str, b: str, knockout: bool):
        # Strength decreases with global index; map to a 1X2 split.
        sa = math.exp(-order[a] / 16.0)
        sb = math.exp(-order[b] / 16.0)
        draw = 0.26
        pa = (1 - draw) * sa / (sa + sb)
        pb = (1 - draw) * sb / (sa + sb)
        return (pa, draw, pb)

    return prob_fn


def test_synthetic_sim_stage_probabilities_monotone_and_simplex():
    groups = _ranked_groups()
    sim = TournamentSimulator(groups, _favour_lower_index_prob_fn(groups))
    res = sim.simulate(n_sims=3000, rng_seed=42)

    # Reach sums equal bracket sizes.
    assert math.isclose(sum(res.reach["R32"].values()), 32.0, abs_tol=1e-9)
    assert math.isclose(sum(res.reach["R16"].values()), 16.0, abs_tol=1e-9)
    assert math.isclose(sum(res.reach["QF"].values()), 8.0, abs_tol=1e-9)
    assert math.isclose(sum(res.reach["SF"].values()), 4.0, abs_tol=1e-9)
    assert math.isclose(sum(res.reach["F"].values()), 2.0, abs_tol=1e-9)
    assert math.isclose(sum(res.win.values()), 1.0, abs_tol=1e-9)

    # Per-team monotonicity: P(win) <= P(Final) <= ... <= P(R32).
    for t in res.teams:
        chain = [
            res.reach["R32"][t],
            res.reach["R16"][t],
            res.reach["QF"][t],
            res.reach["SF"][t],
            res.reach["F"][t],
            res.win[t],
        ]
        for hi, lo in zip(chain, chain[1:]):
            assert lo <= hi + 1e-12, (t, chain)
        # group_position is a proper simplex.
        assert math.isclose(res.group_position[t].sum(), 1.0, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# make_prob_fn returns a normalised triple (uses a tiny real model fit).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_models():
    """Fit Elo+DC on a small synthetic history so make_prob_fn has ratings.

    Kept deliberately small (a handful of matches among the real WC teams) so
    the test stays fast; we only assert structural invariants, not numbers.
    """
    from wca.card import fit_models

    teams = [t for ts in WC2026_GROUPS.values() for t in ts]
    rows = []
    # Round-robin-ish synthetic results so every team has a rating.
    import itertools

    date = pd.Timestamp("2024-01-01")
    for i, (h, a) in enumerate(itertools.combinations(teams[:16], 2)):
        rows.append(
            {
                "date": date + pd.Timedelta(days=i),
                "home_team": h,
                "away_team": a,
                "home_score": (i % 3),
                "away_score": ((i + 1) % 2),
                "tournament": "Friendly",
                "city": "X",
                "country": "Neutral",
                "neutral": True,
            }
        )
    df = pd.DataFrame(rows)
    return fit_models(df)


def test_make_prob_fn_returns_simplex(tiny_models):
    prob_fn = make_prob_fn(tiny_models)
    teams = [t for ts in WC2026_GROUPS.values() for t in ts]
    for knockout in (False, True):
        for a, b in ((teams[0], teams[1]), (teams[5], teams[20]), (teams[10], teams[3])):
            pa, pd_, pb = prob_fn(a, b, knockout)
            assert pa >= 0 and pd_ >= 0 and pb >= 0
            assert math.isclose(pa + pd_ + pb, 1.0, abs_tol=1e-9)


def test_make_prob_fn_host_advantage_helps_host_in_group(tiny_models):
    """A host (e.g. Mexico) should not be *hurt* by the home bonus in a group.

    We compare the host's win prob with the host as nominal home (host bonus
    applied) vs the same matchup at knockout (neutral, no host bonus). The host
    bonus must weakly increase the host's group win probability.
    """
    prob_fn = make_prob_fn(tiny_models)
    host = "Mexico"
    opp = "South Africa"  # same group A opponent
    p_group = prob_fn(host, opp, False)[0]
    p_ko = prob_fn(host, opp, True)[0]
    assert p_group >= p_ko - 1e-9


def _odds_df_fixture(home: str = "Mexico", away: str = "South Africa") -> pd.DataFrame:
    rows = []
    for book, odds in {
        "book_a": {home: 2.20, "Draw": 3.20, away: 3.80},
        "book_b": {home: 2.10, "Draw": 3.30, away: 3.90},
    }.items():
        for outcome, price in odds.items():
            rows.append(
                {
                    "event_id": "evt1",
                    "commence_time": "2026-06-11T19:00:00Z",
                    "home_team": home,
                    "away_team": away,
                    "bookmaker_key": book,
                    "market": "h2h",
                    "outcome_name": outcome,
                    "decimal_odds": price,
                }
            )
    return pd.DataFrame(rows)


def test_market_consensus_lookup_is_order_aware():
    odds_df = _odds_df_fixture()
    lookup = _market_consensus_lookup(odds_df)
    fwd = lookup[("Mexico", "South Africa")]
    rev = lookup[("South Africa", "Mexico")]
    assert fwd[0] == pytest.approx(rev[2])
    assert fwd[1] == pytest.approx(rev[1])
    assert fwd[2] == pytest.approx(rev[0])
    assert sum(fwd) == pytest.approx(1.0)


def test_make_prob_fn_uses_market_when_available(tiny_models):
    odds_df = _odds_df_fixture()
    expected = _market_consensus_lookup(odds_df)[("Mexico", "South Africa")]
    prob_fn = make_prob_fn(
        tiny_models,
        odds_df=odds_df,
        weights=BlendWeights(elo=0.0, dc=0.0, market=1.0),
    )
    assert prob_fn("Mexico", "South Africa", False) == pytest.approx(expected)
    rev_expected = (expected[2], expected[1], expected[0])
    assert prob_fn("South Africa", "Mexico", False) == pytest.approx(rev_expected)


def test_make_prob_fn_falls_back_without_market(tiny_models):
    odds_df = _odds_df_fixture()
    market_only = make_prob_fn(
        tiny_models,
        odds_df=odds_df,
        weights=BlendWeights(elo=0.0, dc=0.0, market=1.0),
    )
    model_only = make_prob_fn(
        tiny_models,
        weights=BlendWeights(elo=0.5, dc=0.5, market=0.0),
    )
    # Knockout pairings are generated and have no tradable 1X2 market, so even
    # a market-only requested blend must fall back deterministically to Elo+DC.
    assert market_only("Mexico", "South Africa", True) == pytest.approx(
        model_only("Mexico", "South Africa", True)
    )


# ---------------------------------------------------------------------------
# Polymarket price helpers.
# ---------------------------------------------------------------------------


def test_yes_mid_prefers_bid_ask_midpoint():
    m = {"bestBid": 0.40, "bestAsk": 0.42, "priceMap": {"Yes": 0.55, "No": 0.45}}
    assert _yes_mid(m) == pytest.approx(0.41)


def test_yes_mid_falls_back_to_pricemap():
    m = {"priceMap": {"Yes": 0.62, "No": 0.38}}
    assert _yes_mid(m) == pytest.approx(0.62)


def test_yes_mid_none_when_unusable():
    assert _yes_mid({"bestBid": 0, "bestAsk": 1}) is None  # placeholder/noise


def test_no_ask_is_one_minus_yes_bid():
    m = {"bestBid": 0.40, "bestAsk": 0.42}
    assert _no_ask(m, 0.41) == pytest.approx(0.60)


def test_pm_taker_fee_formula():
    # fee = 0.03 * p * (1 - p); max at p=0.5.
    assert pm_taker_fee(0.5) == pytest.approx(0.03 * 0.25)
    assert pm_taker_fee(0.0) == pytest.approx(0.0)
    assert pm_taker_fee(1.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# compare_to_polymarket: matching logic incl. name variants.
# ---------------------------------------------------------------------------


def _sim_df_fixture() -> pd.DataFrame:
    """A tiny sim DataFrame for a few teams with known probabilities."""
    rows = [
        # team, group, P(R32), P(R16), P(QF), P(SF), P(Final), P(win), P(gw)
        ("United States", "D", 0.80, 0.55, 0.30, 0.15, 0.07, 0.03, 0.50),
        ("Turkey", "D", 0.60, 0.35, 0.18, 0.08, 0.03, 0.01, 0.20),
        ("DR Congo", "K", 0.20, 0.08, 0.03, 0.01, 0.004, 0.001, 0.05),
        ("Curaçao", "E", 0.10, 0.03, 0.01, 0.003, 0.001, 0.0003, 0.02),
        ("Mexico", "A", 0.70, 0.45, 0.25, 0.12, 0.05, 0.02, 0.60),
    ]
    df = pd.DataFrame(
        rows,
        columns=[
            "team", "group", "P(R32)", "P(R16)", "P(QF)", "P(SF)",
            "P(Final)", "P(win)", "P(group_winner)",
        ],
    ).set_index("team")
    return df


def _mkt(git: str, yes_bid: float, yes_ask: float) -> Dict:
    mid = 0.5 * (yes_bid + yes_ask)
    return {
        "groupItemTitle": git,
        "bestBid": yes_bid,
        "bestAsk": yes_ask,
        "priceMap": {"Yes": mid, "No": 1 - mid},
    }


def test_compare_matches_name_variants_and_skips_noise():
    sim_df = _sim_df_fixture()
    # R16 event with name variants: USA->United States, Turkiye->Turkey,
    # "Congo DR"->DR Congo, "Curacao"->Curaçao, plus noise markets.
    pm_events = [
        {
            "title": "World Cup: Nation To Reach Round of 16",
            "markets": [
                _mkt("USA", 0.30, 0.32),       # sim 0.55 -> big YES edge
                _mkt("Turkiye", 0.34, 0.36),   # sim 0.35
                _mkt("Congo DR", 0.10, 0.12),  # sim 0.08
                _mkt("Curacao", 0.05, 0.07),   # sim 0.03
                _mkt("Other", 0.0, 1.0),       # noise: no usable price
                _mkt("Team AM", 0.0, 1.0),     # noise placeholder
            ],
        }
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    matched = set(out["team"])
    assert matched == {"United States", "Turkey", "DR Congo", "Curaçao"}
    # The USA YES position must be the top edge (sim 0.55 vs ~0.31 ask).
    top = out.iloc[0]
    assert top["team"] == "United States"
    assert top["side"] == "YES"
    assert top["stage"] == "R16"
    assert top["fee_adj_edge"] > 0.15


def test_compare_picks_no_side_when_sim_below_price():
    sim_df = _sim_df_fixture()
    # DR Congo sim R16 = 0.08 but priced at ~0.50 -> NO should win.
    pm_events = [
        {
            "title": "World Cup: Nation To Reach Round of 16",
            "markets": [_mkt("DR Congo", 0.49, 0.51)],
        }
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["side"] == "NO"
    # NO pays out with prob 1 - 0.08 = 0.92, bought at ~0.51 -> positive edge.
    assert row["sim_prob"] == pytest.approx(0.92)
    assert row["fee_adj_edge"] > 0.3


def test_compare_group_winner_matches_only_in_group_teams():
    sim_df = _sim_df_fixture()
    pm_events = [
        {
            "title": "World Cup Group A Winner",
            "markets": [
                _mkt("Mexico", 0.55, 0.57),   # in group A
                _mkt("USA", 0.30, 0.32),      # group D -> must be ignored here
                _mkt("Other", 0.0, 1.0),
            ],
        }
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    assert set(out["team"]) == {"Mexico"}
    assert out.iloc[0]["stage"] == "GW"
    assert out.iloc[0]["stage_label"] == "Win Group A"


def test_compare_uses_correct_stage_column():
    sim_df = _sim_df_fixture()
    # Same team across two stage events; the matched sim_prob must differ.
    pm_events = [
        {
            "title": "World Cup: Team to advance to Knockout Stages",
            "markets": [_mkt("Mexico", 0.50, 0.52)],
        },
        {
            "title": "World Cup Winner",
            "markets": [_mkt("Mexico", 0.05, 0.07)],
        },
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    by_stage = {r["stage"]: r for _, r in out.iterrows()}
    assert "R32" in by_stage and "win" in by_stage
    # R32 side: YES sim 0.70; win side: YES sim 0.02.
    assert by_stage["R32"]["sim_prob"] == pytest.approx(0.70)
    assert by_stage["win"]["sim_prob"] == pytest.approx(0.98)  # NO side wins


def test_stakes_respect_pool_cap():
    sim_df = _sim_df_fixture()
    pm_events = [
        {
            "title": "World Cup: Nation To Reach Round of 16",
            "markets": [_mkt("USA", 0.20, 0.22)],  # huge edge -> capped stake
        }
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    assert (out["stake"] <= PM_POOL_BANKROLL * 0.05 + 1e-6).all()


def test_pm_stage_event_titles_cover_all_stages():
    # Guards against typos in the event-title -> stage mapping.
    assert set(PM_STAGE_EVENTS.values()) == {"R32", "R16", "QF", "SF", "F", "win"}


def test_compare_matches_titles_with_surrounding_whitespace():
    """Live Polymarket titles can carry trailing/leading spaces.

    Regression: the live 'World Cup Winner ' event ships with a trailing space;
    an exact-match lookup silently dropped the entire outright-winner book (48
    team markets). The stage lookup must strip the title first.
    """
    sim_df = _sim_df_fixture()
    pm_events = [
        {
            "title": "World Cup Winner ",  # note trailing space, as served live
            "markets": [_mkt("Mexico", 0.04, 0.06)],
        }
    ]
    out = compare_to_polymarket(sim_df, pm_events)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["stage"] == "win"
    # Mexico sim win = 0.02 -> NO side pays with prob 0.98, bought ~0.95.
    assert row["side"] == "NO"
    assert row["sim_prob"] == pytest.approx(0.98)
