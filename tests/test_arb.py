"""Tests for the deterministic arbitrage core (no network)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from wca import arb


# --------------------------------------------------------------------------
# Core 3-way / 2-way math
# --------------------------------------------------------------------------

def test_three_way_arb_exists():
    # 1/2.1 + 1/3.6 + 1/4.2 = 0.4762 + 0.2778 + 0.2381 = 0.9920 < 1 -> arb
    res = arb.three_way_arb([(2.1, "a"), (3.6, "b"), (4.2, "c")])
    assert res is not None
    assert res["profit_pct"] > 0
    expected = (1.0 / (1 / 2.1 + 1 / 3.6 + 1 / 4.2)) - 1.0
    assert math.isclose(res["profit_pct"], expected, rel_tol=1e-9)


def test_three_way_non_arb():
    # Tighter odds -> sum of inverses > 1
    res = arb.three_way_arb([(2.0, "a"), (3.2, "b"), (3.5, "c")])
    assert res is None


def test_commission_kills_arb():
    # Raw 3-way is a (thin) arb but exchange commission removes it.
    raw = arb.three_way_arb([(2.05, "betfair_ex_uk"), (3.7, "betfair_ex_uk"),
                             (4.3, "betfair_ex_uk")], net=True)
    assert raw is not None  # treated as net -> arb
    # Now apply commission (raw odds, 6% each leg) -> should vanish.
    netted = arb.three_way_arb(
        [(2.05, "betfair_ex_uk"), (3.7, "betfair_ex_uk"), (4.3, "betfair_ex_uk")],
    )
    assert netted is None


def test_stake_split_sums_and_equalises_payout():
    res = arb.three_way_arb([(2.1, "a"), (3.6, "b"), (4.2, "c")])
    bankroll = 1000.0
    stakes = arb.stake_split(res["stake_fractions"], bankroll)
    assert math.isclose(sum(stakes), bankroll, rel_tol=1e-9)
    # Payout of each leg = stake * net_odds; should be equal across legs.
    payouts = [stakes[i] * res["legs"][i]["net_odds"] for i in range(3)]
    assert math.isclose(payouts[0], payouts[1], rel_tol=1e-9)
    assert math.isclose(payouts[1], payouts[2], rel_tol=1e-9)


# --------------------------------------------------------------------------
# effective_back / pm pricing
# --------------------------------------------------------------------------

def test_effective_back_commission():
    # 3.0 decimal at 6% commission: 1 + 2*0.94 = 2.88
    assert math.isclose(arb.effective_back(3.0, "betfair_ex_uk"), 2.88)
    # Plain book unchanged.
    assert arb.effective_back(3.0, "williamhill") == 3.0


# --------------------------------------------------------------------------
# Settlement-key guard
# --------------------------------------------------------------------------

def test_settlement_key_refuses_outright():
    assert arb.settlement_key("outrights") is None
    assert arb.settlement_key("h2h") == "1x2_90min"
    assert arb.settlement_key("totals", 2.5) == "totals_2.5_90min"
    assert arb.settlement_key("totals", None) is None


def test_cross_book_refuses_mixed_settlement():
    # Build a df where a 90-min draw price and a different-settlement market
    # both sit on the same event. The detector must NOT pair them: the
    # outright row carries settlement_key None and is skipped entirely.
    rows = [
        # A genuine 1X2 3-way arb at one book set.
        dict(event_id="E1", home_team="A", away_team="B", market="h2h",
             outcome_name="A", outcome_point=None, decimal_odds=2.1,
             bookmaker_key="bk1"),
        dict(event_id="E1", home_team="A", away_team="B", market="h2h",
             outcome_name="Draw", outcome_point=None, decimal_odds=3.6,
             bookmaker_key="bk2"),
        dict(event_id="E1", home_team="A", away_team="B", market="h2h",
             outcome_name="B", outcome_point=None, decimal_odds=4.2,
             bookmaker_key="bk3"),
        # An outright/advancement row (different settlement) that must be ignored.
        dict(event_id="E1", home_team="A", away_team="B", market="outrights",
             outcome_name="A", outcome_point=None, decimal_odds=1.01,
             bookmaker_key="bk1"),
    ]
    df = pd.DataFrame(rows)
    arbs = arb.find_cross_book_arbs(df, min_profit=0.001)
    assert len(arbs) == 1
    a = arbs[0]
    assert a["market"] == "h2h"
    assert a["settlement_key"] == "1x2_90min"
    # No leg may reference the outright market's settlement.
    assert all(leg["outcome"] in ("A", "Draw", "B") for leg in a["legs"])


def test_cross_book_totals_pairs_only_same_line():
    rows = [
        dict(event_id="E2", home_team="A", away_team="B", market="totals",
             outcome_name="Over", outcome_point=2.5, decimal_odds=2.1,
             bookmaker_key="bk1"),
        dict(event_id="E2", home_team="A", away_team="B", market="totals",
             outcome_name="Under", outcome_point=2.5, decimal_odds=2.1,
             bookmaker_key="bk2"),
        # Different line - must not be paired with the 2.5 Over.
        dict(event_id="E2", home_team="A", away_team="B", market="totals",
             outcome_name="Under", outcome_point=3.5, decimal_odds=1.2,
             bookmaker_key="bk3"),
    ]
    df = pd.DataFrame(rows)
    arbs = arb.find_cross_book_arbs(df, min_profit=0.001)
    # Over2.5 + Under2.5 at 2.1/2.1 -> 1/2.1*2 = 0.952 < 1 -> arb.
    assert len(arbs) == 1
    assert arbs[0]["settlement_key"] == "totals_2.5_90min"
    assert arbs[0]["point"] == 2.5


# --------------------------------------------------------------------------
# Polymarket detectors
# --------------------------------------------------------------------------

def test_pm_internal_yes_no_arb():
    # YES 0.45 + NO 0.50: cost ~ 0.95 + small fee. Buying both shares pays 1.
    quotes = [dict(event_id="E3", settlement_key="1x2_90min",
                   question="Will A win?", yes_price=0.45, no_price=0.50)]
    arbs = arb.find_pm_book_arbs(pd.DataFrame([]), quotes, min_profit=0.001)
    internal = [a for a in arbs if a["kind"] == "pm_internal"]
    assert len(internal) == 1
    assert internal[0]["profit_pct"] > 0


def test_pm_internal_no_arb_when_priced_fairly():
    quotes = [dict(event_id="E3", settlement_key="1x2_90min",
                   question="Will A win?", yes_price=0.55, no_price=0.47)]
    arbs = arb.find_pm_book_arbs(pd.DataFrame([]), quotes, min_profit=0.001)
    internal = [a for a in arbs if a["kind"] == "pm_internal"]
    assert internal == []


def test_pm_book_refuses_wrong_settlement():
    df = pd.DataFrame([
        dict(event_id="E4", home_team="A", away_team="B", market="h2h",
             outcome_name="A", outcome_point=None, decimal_odds=2.5,
             bookmaker_key="bk1"),
        dict(event_id="E4", home_team="A", away_team="B", market="h2h",
             outcome_name="Draw", outcome_point=None, decimal_odds=3.5,
             bookmaker_key="bk1"),
        dict(event_id="E4", home_team="A", away_team="B", market="h2h",
             outcome_name="B", outcome_point=None, decimal_odds=3.0,
             bookmaker_key="bk1"),
    ])
    # PM market with a NON-1x2 settlement (e.g. to-qualify) must be refused.
    quotes = [dict(event_id="E4", settlement_key="qualify_etpens",
                   outcome="A", yes_price=0.30, question="A to qualify")]
    arbs = arb.find_pm_book_arbs(df, quotes, min_profit=-1.0)
    pm_book = [a for a in arbs if a["kind"] == "pm_book"]
    assert pm_book == []


def test_pm_book_arb_valid_settlement():
    # Cheap book prices for Draw & B, very cheap PM YES for A -> 3-way arb.
    df = pd.DataFrame([
        dict(event_id="E5", home_team="A", away_team="B", market="h2h",
             outcome_name="Draw", outcome_point=None, decimal_odds=4.0,
             bookmaker_key="bk1"),
        dict(event_id="E5", home_team="A", away_team="B", market="h2h",
             outcome_name="B", outcome_point=None, decimal_odds=4.0,
             bookmaker_key="bk1"),
    ])
    quotes = [dict(event_id="E5", settlement_key="1x2_90min",
                   outcome="A", yes_price=0.40, question="A to win (90)")]
    arbs = arb.find_pm_book_arbs(df, quotes, min_profit=0.001)
    pm_book = [a for a in arbs if a["kind"] == "pm_book"]
    assert len(pm_book) == 1
    assert pm_book[0]["profit_pct"] > 0
    fracs = [leg["stake_fraction"] for leg in pm_book[0]["legs"]]
    assert math.isclose(sum(fracs), 1.0, rel_tol=1e-9)


def test_rank_filters_and_sorts():
    arbs = [
        {"profit_pct": 0.001}, {"profit_pct": 0.02}, {"profit_pct": 0.01},
    ]
    ranked = arb.rank_arbs(arbs, min_profit=0.005)
    assert [a["profit_pct"] for a in ranked] == [0.02, 0.01]
