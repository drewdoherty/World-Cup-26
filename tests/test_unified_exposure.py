"""Tests for the unified card+advancement correlated exposure layer."""

from __future__ import annotations

from wca import unified_exposure as ue
from wca.unified_exposure import Position


def test_team_long_nets_advancement_and_match():
    pos = [
        Position("Brazil", "R16", 60.0, 0.65, "adv"),
        Position("Brazil", "win", 6.0, 0.07, "outright"),
        Position("Brazil", "match", 20.0, 0.55, "match"),
        Position("Ghana", "elim", 1.0, 0.68, "fade"),   # short, not a Brazil long
    ]
    exp = ue.team_long_exposure(pos)
    assert exp["Brazil"] == 86.0      # 60 + 6 + 20 (all correlated long)
    assert "Ghana" not in exp         # fade is not a long


def test_quarter_kelly_zero_when_no_edge():
    assert ue.quarter_kelly_stake(0.40, 0.45, 2000) == 0.0   # model below price
    s = ue.quarter_kelly_stake(0.60, 0.50, 2000)             # edge -> positive, capped
    assert 0 < s <= 0.05 * 2000


def test_build_plan_trims_reversed_and_caps_team():
    open_pos = [
        Position("France", "SF", 67.0, 0.50, "adv"),     # model now below price -> trim
        Position("Brazil", "R16", 63.0, 0.65, "adv"),    # still +EV -> hold
    ]
    cands = [
        {"team": "France", "stage": "SF", "model": 0.33, "pm": 0.51, "kind": "adv"},
        {"team": "Brazil", "stage": "R16", "model": 0.76, "pm": 0.71, "kind": "adv"},
        {"team": "Ivory Coast", "stage": "R16", "model": 0.43, "pm": 0.34, "kind": "adv"},
    ]
    plan = ue.build_plan(open_pos, cands, bankroll=2000, deploy_frac=0.70)
    # France flagged as trim (model<price); Brazil not
    trim_teams = {t["team"] for t in plan.trims}
    assert "France" in trim_teams and "Brazil" not in trim_teams
    # a new order exists and respects the buffer (combined <= 70% bankroll)
    assert plan.combined <= 0.70 * 2000 + 1e-6
    assert plan.buffer >= 0.30 * 2000 - 1e-6
    assert any(o["team"] == "Ivory Coast" for o in plan.new_orders)
    # Brazil R16 is already HELD -> must NOT be a "new" order; it's a top-up.
    assert not any(o["team"] == "Brazil" and o["stage"] == "R16" for o in plan.new_orders)
    assert any(o["team"] == "Brazil" and o["stage"] == "R16" for o in plan.topups)
    # Ivory Coast is a fresh team -> not nested with a held position.
    ivc = [o for o in plan.new_orders if o["team"] == "Ivory Coast"][0]
    assert ivc["nested_with_held"] is False


def test_shrink_pulls_longshots_harder():
    # a longshot is pulled most of the way to market; a favourite barely moves
    qs_long = ue.shrink_q(0.34, 0.17)
    qs_fav = ue.shrink_q(0.72, 0.67)
    assert qs_long < 0.30 and qs_long > 0.17        # shrunk well below the 0.34 model
    assert abs(qs_fav - 0.72) < abs(qs_long - 0.34)  # favourite trusted more


def test_screen_drops_longshot_trap_and_thin_favourite():
    bosnia = ue.screen(0.34, 0.17)      # p<0.20, model 2x line -> hard drop
    assert bosnia["verdict"] == "DROP" and "longshot" in bosnia["reason"]
    thin_fav = ue.screen(0.70, 0.67)    # favourite but edge 0.03 < 0.05 floor
    assert thin_fav["verdict"] == "DROP"
    assert ue.screen(0.60, 0.51)["verdict"] == "DROP"   # mid prob, 6.3pp shrunk < 9pp floor
    good = ue.screen(0.65, 0.55)        # favourite, shrunk edge clears the 5pp floor
    assert good["verdict"] == "PASS"
    nope = ue.screen(0.40, 0.45)        # model below market
    assert nope["verdict"] == "NO_EDGE"


def test_edge_floor_rises_as_prob_falls():
    assert ue.edge_floor(0.60) < ue.edge_floor(0.40) < ue.edge_floor(0.25) <= ue.edge_floor(0.10)


def test_implied_ko_wins():
    assert ue.implied_ko_wins("R16") == 1
    assert ue.implied_ko_wins("win") == 5
