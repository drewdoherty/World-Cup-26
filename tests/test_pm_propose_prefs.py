"""Proposal ordering (reference surface wca_pm_propose).

+EV moneylines > longshots (bucket, unchanged). Proposals here are scorer props
+ game 1X2 = 90-min MATCH markets, so post-2026-07-09 the hours-out term is
NEUTRAL and EV breaks ties within the bucket (the reference surface passes
market_kind=MARKET_MATCH at its real call sites; the default is also match).
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_pm_propose as prop  # noqa: E402
from wca.selection import MARKET_FUTURES, MARKET_MATCH  # noqa: E402

NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
KICK = {
    "A vs B": "2026-07-02T14:00:00Z",  # 2h out (imminent)
    "C vs D": "2026-07-05T14:00:00Z",  # ~74h out (further away)
}


def _p(match, prob, ev):
    return {"match_desc": match, "model_prob": prob, "ev": ev}


def test_moneylines_rank_before_longshots_even_at_lower_ev():
    ps = [_p("A vs B", 0.15, 0.30), _p("A vs B", 0.60, 0.05)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW,
                                                   market_kind=MARKET_MATCH))
    assert ps[0]["model_prob"] == 0.60


def test_match_ev_preferred_over_further_out_within_bucket():
    # 2026-07-09 reversal for match markets: nearer +EV beats further-out low-EV.
    ps = [_p("A vs B", 0.60, 0.10), _p("C vs D", 0.60, 0.05)]  # A vs B is nearer, higher EV
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW,
                                                   market_kind=MARKET_MATCH))
    assert ps[0]["match_desc"] == "A vs B"  # EV wins (hours neutral for match)


def test_futures_further_out_preferred_within_a_bucket():
    # Futures/advancement KEEP further-out-first.
    ps = [_p("A vs B", 0.60, 0.10), _p("C vs D", 0.60, 0.05)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW,
                                                   market_kind=MARKET_FUTURES))
    assert ps[0]["match_desc"] == "C vs D"  # further-out wins for futures


def test_ev_breaks_ties_same_fixture_same_bucket():
    ps = [_p("A vs B", 0.60, 0.04), _p("A vs B", 0.60, 0.09)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW,
                                                   market_kind=MARKET_MATCH))
    assert ps[0]["ev"] == 0.09


def test_match_missing_kickoff_does_not_matter():
    # Hours neutral for match -> EV breaks the tie, not kickoff knowledge.
    ps = [_p("X vs Y", 0.60, 0.09), _p("C vs D", 0.60, 0.02)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW,
                                                   market_kind=MARKET_MATCH))
    assert ps[0]["match_desc"] == "X vs Y"  # higher EV wins (hours neutral)
