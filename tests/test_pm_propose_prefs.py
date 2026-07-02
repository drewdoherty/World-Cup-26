"""Proposal ordering: +EV moneylines > longshots; further-out > imminent."""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_pm_propose as prop  # noqa: E402

NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
KICK = {
    "A vs B": "2026-07-02T14:00:00Z",  # 2h out (imminent)
    "C vs D": "2026-07-05T14:00:00Z",  # ~74h out (further away)
}


def _p(match, prob, ev):
    return {"match_desc": match, "model_prob": prob, "ev": ev}


def test_moneylines_rank_before_longshots_even_at_lower_ev():
    ps = [_p("A vs B", 0.15, 0.30), _p("A vs B", 0.60, 0.05)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW))
    assert ps[0]["model_prob"] == 0.60


def test_further_out_preferred_within_a_bucket():
    ps = [_p("A vs B", 0.60, 0.10), _p("C vs D", 0.60, 0.05)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW))
    assert ps[0]["match_desc"] == "C vs D"


def test_ev_breaks_ties_same_fixture_same_bucket():
    ps = [_p("A vs B", 0.60, 0.04), _p("A vs B", 0.60, 0.09)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW))
    assert ps[0]["ev"] == 0.09


def test_missing_kickoff_degrades_gracefully():
    ps = [_p("X vs Y", 0.60, 0.02), _p("C vs D", 0.60, 0.02)]
    ps.sort(key=lambda p: prop.preference_sort_key(p, KICK, NOW))
    assert ps[0]["match_desc"] == "C vs D"  # known-further beats unknown (0h)
