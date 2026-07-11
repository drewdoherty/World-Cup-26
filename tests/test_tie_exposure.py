"""Tests for ``wca.tie_exposure`` — cross-feed same-bet dedup.

Live incident (2026-07-11): ``event_market_recs.json`` recommended $96.61 on
"England to advance" (this QF's ET+pens market) while ``bet_recs.json``
independently recommended $136.39 on "england_sf_pm" / reach_SF (the
advancement-futures market) — both keyed off the SAME model_prob (0.7123),
both pricing the exact same real-world event (England beats Norway), with
zero cross-awareness. Only an unrelated staleness guard kept the second one
withheld. These tests pin the fix.
"""
from __future__ import annotations

from wca.tie_exposure import (
    find_cross_feed_duplicates,
    resolve_duplicate,
    same_bet_key,
    tie_key,
)


def test_tie_key_is_order_independent():
    assert tie_key("England", "Norway") == tie_key("Norway", "England")


def test_tie_key_canonicalises_names():
    # canonical() resolves aliases ("USA" -> "United States"); exact mapping
    # lives in wca.data.teamnames — this just pins that tie_key uses it.
    assert tie_key("USA", "Norway") == tie_key("United States", "Norway")


def test_same_bet_key_none_without_stage():
    assert same_bet_key("England", None) is None
    assert same_bet_key(None, "SF") is None


def test_same_bet_key_canonical_and_stage_scoped():
    assert same_bet_key("USA", "SF") == same_bet_key("United States", "SF")
    assert same_bet_key("England", "SF") != same_bet_key("England", "Final")


def _em_leg(team, stage, stake_usd=96.61, ev=0.102169, fixture="Norway vs England"):
    return {"fixture": fixture, "label": "%s to advance" % team, "team": team,
            "tie_stage": stage, "stake_usd": stake_usd, "ev": ev}


def _af_leg(team, stage, stake=136.39, ev_net=0.0505, id_="x"):
    return {"id": id_, "team": team, "stage": stage, "stake": stake, "ev_net": ev_net}


def test_finds_the_live_incident_duplicate():
    em = [_em_leg("England", "SF")]
    af = [_af_leg("England", "SF", id_="england_sf_pm")]
    dupes = find_cross_feed_duplicates(em, af)
    assert len(dupes) == 1
    assert dupes[0]["key"] == ("England", "SF")


def test_no_duplicate_when_stages_differ():
    em = [_em_leg("England", "SF")]
    af = [_af_leg("England", "Final", id_="england_final_pm")]
    assert find_cross_feed_duplicates(em, af) == []


def test_no_duplicate_when_opposite_teams_of_same_tie():
    # Norway backed in event_markets, England backed in advancement_futures —
    # mutually exclusive outcomes of the same tie, NOT the same bet.
    em = [_em_leg("Norway", "SF", fixture="Norway vs England")]
    af = [_af_leg("England", "SF", id_="england_sf_pm")]
    assert find_cross_feed_duplicates(em, af) == []


def test_zero_stake_legs_never_flagged():
    em = [_em_leg("England", "SF", stake_usd=0.0)]
    af = [_af_leg("England", "SF", stake=0.0, id_="england_sf_pm")]
    assert find_cross_feed_duplicates(em, af) == []


def test_missing_stage_never_falsely_matches():
    em = [_em_leg("England", None)]
    af = [_af_leg("England", None, id_="england_sf_pm")]
    assert find_cross_feed_duplicates(em, af) == []


def test_resolve_duplicate_keeps_better_edge():
    dupe = {"key": ("England", "SF"),
            "event_market": _em_leg("England", "SF", ev=0.10),
            "advancement_futures": _af_leg("England", "SF", ev_net=0.05)}
    # advancement_futures has the WORSE edge (0.05 < 0.10) -> it loses.
    assert resolve_duplicate(dupe) == "advancement_futures"

    dupe2 = {"key": ("England", "SF"),
             "event_market": _em_leg("England", "SF", ev=0.02),
             "advancement_futures": _af_leg("England", "SF", ev_net=0.05)}
    assert resolve_duplicate(dupe2) == "event_market"


def test_resolve_duplicate_ties_favour_event_market():
    dupe = {"key": ("England", "SF"),
            "event_market": _em_leg("England", "SF", ev=0.05),
            "advancement_futures": _af_leg("England", "SF", ev_net=0.05)}
    assert resolve_duplicate(dupe) == "advancement_futures"
