"""Tests for the dual-pool 1/2-Kelly sizing (user decision, 2026-06-28).

The desk's bankroll is split equally across its two books — £1,500 in GBP
sportsbooks/exchanges + the $1,995 Polymarket balance (£1 = $1.33) — and sized
at half-Kelly. Each pick is routed to exactly one pool by venue and sized/shown
in that pool's OWN currency, so the per-pool deployment, whole-book exposure and
daily caps never mix £ and $.

These are pure-logic tests: routing + rendering, no model fitting / IO.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from wca.card import (
    DUAL_POOL_KELLY_FRACTION,
    GBP_POOL_BANKROLL,
    GBP_POOL_NAME,
    PM_POOL_BANKROLL,
    PM_POOL_NAME,
    PoolConfig,
    RankedCard,
    Recommendation,
    build_card,
    default_pools,
    fit_models,
    format_ranked_card,
    pool_for_venue,
)


def _rec(*, match="A vs B", venue="smarkets", stakes, team="A", odds=2.0,
         model_prob=0.55, edge=0.05, category="favourite"):
    return Recommendation(
        match_id="evt", match_desc=match, commence_time="2026-07-01T18:00:00Z",
        selection="home", selection_team=team, best_book=venue, best_odds=odds,
        model_prob=model_prob, market_prob=model_prob, elo_prob=model_prob,
        dc_prob=model_prob, edge=edge, ev_per_unit=edge, stakes=stakes,
        venue=venue, raw_edge=edge, hours_to_kickoff=None, imminent=False,
        category=category,
    )


# ---------------------------------------------------------------------------
# default_pools / PoolConfig.symbol
# ---------------------------------------------------------------------------


def test_default_pools_shape():
    pools = default_pools()
    by_name = {p.name: p for p in pools}
    assert by_name[GBP_POOL_NAME].bankroll == GBP_POOL_BANKROLL == 1500.0
    assert by_name[GBP_POOL_NAME].currency == "GBP"
    assert by_name[PM_POOL_NAME].bankroll == PM_POOL_BANKROLL == 1995.0
    assert by_name[PM_POOL_NAME].currency == "USD"
    # Both books at the chosen aggressive half-Kelly.
    assert all(p.kelly_fraction == DUAL_POOL_KELLY_FRACTION == 0.50 for p in pools)


def test_pool_symbol():
    assert PoolConfig(name="pm", bankroll=1.0, currency="USD").symbol == "$"
    assert PoolConfig(name="gbp", bankroll=1.0, currency="GBP").symbol == "£"


# ---------------------------------------------------------------------------
# pool_for_venue routing
# ---------------------------------------------------------------------------


def test_pool_for_venue_routes_polymarket_to_usd():
    pools = default_pools()
    assert pool_for_venue("polymarket", pools).name == PM_POOL_NAME
    for v in ("betfair", "smarkets", "bet365", "exchange"):
        assert pool_for_venue(v, pools).name == GBP_POOL_NAME


def test_pool_for_venue_falls_back_to_first_pool():
    # Single-pool legacy caller: any venue resolves without KeyError.
    only = [PoolConfig(name="main", bankroll=100.0)]
    assert pool_for_venue("polymarket", only).name == "main"
    assert pool_for_venue("betfair", only).name == "main"


# ---------------------------------------------------------------------------
# format_ranked_card: per-pick currency + per-pool footer/exposure
# ---------------------------------------------------------------------------


def test_card_shows_each_pick_in_its_own_currency():
    pools = default_pools()
    picks = [
        _rec(match="GBP match", venue="betfair",
             stakes={GBP_POOL_NAME: 30.0, PM_POOL_NAME: 0.0}),
        _rec(match="PM match", venue="polymarket",
             stakes={GBP_POOL_NAME: 0.0, PM_POOL_NAME: 40.0}),
    ]
    out = format_ranked_card(RankedCard(picks=picks, cut=[]), pools)
    # Display convention (2026-07-02): everything is SHOWN in $ (GBP stakes at
    # the fixed $1.33/£ rate) but each pick is still SIZED in its own pool's
    # currency — £30 × 1.33 = $39.90, PM already $40.
    assert "stake *$39.90*" in out
    assert "stake *$40.00*" in out
    # Footer carries both pools with their currencies.
    assert "gbp pool: £1500" in out
    assert "pm pool: $1995" in out
    assert "£1 = $1.33" in out


def test_exposure_is_currency_isolated_per_pool():
    # Same fixture carries a £ leg (betfair) AND a $ leg (polymarket). The two
    # legs must be summed/capped in their OWN currency — never combined into a
    # single mixed-currency number.
    pools = default_pools()
    picks = [
        _rec(match="Mixed FX", venue="betfair",
             stakes={GBP_POOL_NAME: 90.0, PM_POOL_NAME: 0.0}),
        _rec(match="Mixed FX", venue="polymarket",
             stakes={GBP_POOL_NAME: 0.0, PM_POOL_NAME: 120.0}),
    ]
    out = format_ranked_card(RankedCard(picks=picks, cut=[]), pools)
    # Per-pool venue split, each in its own symbol — never a £210/$210 mix.
    assert "Venue split (gbp):" in out
    assert "betfair £90.00" in out
    assert "Venue split (pm):" in out
    assert "polymarket $120.00" in out
    assert "£210" not in out and "$210" not in out


def test_legacy_single_pool_still_renders():
    # A lone PoolConfig (not a list) is accepted and wrapped.
    pool = PoolConfig(name="main", bankroll=2000.0)
    picks = [_rec(venue="smarkets", stakes={"main": 12.0})]
    out = format_ranked_card(RankedCard(picks=picks, cut=[]), pool)
    # £12 pool stake shown in $ at the fixed rate: 12 × 1.33 = $15.96.
    assert "stake *$15.96*" in out


# ---------------------------------------------------------------------------
# build_card: each pick is sized in exactly ONE pool (routed by venue)
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=240):
    teams = ["Alpha", "Bravo", "Cienega", "Delta", "Echo", "Foxtrot"]
    base = pd.Timestamp("2024-01-01")
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


def _odds_with_polymarket_best():
    # Polymarket posts the best (highest) price on Bravo; the GBP exchanges win
    # the other two outcomes — so the card should route Bravo to the $ pool and
    # Alpha/Draw to the £ pool.
    fixture = dict(event_id="evt1", home_team="Alpha", away_team="Bravo",
                   commence_time="2026-07-01T18:00:00Z", market="h2h")
    book_prices = {
        "smarkets": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.55},
        "betfair_ex_uk": {"Alpha": 2.12, "Draw": 3.45, "Bravo": 3.60},
        "polymarket": {"Alpha": 2.00, "Draw": 3.20, "Bravo": 4.10},
    }
    rows = []
    for book, prices in book_prices.items():
        for name, odd in prices.items():
            rows.append(dict(fixture, bookmaker_key=book, outcome_name=name,
                             decimal_odds=odd))
    return pd.DataFrame(rows)


def _fixtures_meta():
    return pd.DataFrame([{
        "home_team": "Alpha", "away_team": "Bravo", "neutral": False,
        "country": "", "home_score": np.nan, "away_score": np.nan,
    }])


def test_build_card_sizes_each_pick_in_exactly_one_pool():
    rng = np.random.default_rng(7)
    models = fit_models(_synthetic_results(rng), half_life_years=8.0)
    recs = build_card(models, _odds_with_polymarket_best(), default_pools(),
                      _fixtures_meta(), min_edge=-1.0, now="2026-06-20T12:00:00Z")
    assert recs
    saw_pm_staked = saw_gbp_routed = False
    for r in recs:
        gbp = float(r.stakes.get(GBP_POOL_NAME, 0.0))
        pm = float(r.stakes.get(PM_POOL_NAME, 0.0))
        # Currency isolation invariant: the NON-routed pool is always 0, so the
        # £ and $ books can never bleed into each other (a -EV pick is simply
        # left unstaked by Kelly — 0 in both — which still satisfies isolation).
        if r.venue == "polymarket":
            assert gbp == 0.0, r.stakes
            if pm > 0.0:
                saw_pm_staked = True
        else:
            assert pm == 0.0, r.stakes
            saw_gbp_routed = True
    # The juicy Polymarket price on Bravo is +EV -> staked in the $ pool; and the
    # GBP exchanges carry picks (routed to the £ pool, staked iff +EV).
    assert saw_pm_staked
    assert saw_gbp_routed
