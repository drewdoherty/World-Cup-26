"""Unit tests for wca.data.statsbomb on synthetic event fixtures.

No network: tests build small synthetic StatsBomb-shaped event lists.

Card convention under test: 'Second Yellow' counts as ONE red card (the
second caution converts to a sending-off per FIFA rules; red-card props
settle on the dismissal), NOT as two yellows.
"""
import math
import os

import pytest

from wca.data.statsbomb import match_props, player_shares


def _xi(team, players=()):
    return {
        "type": {"name": "Starting XI"},
        "team": {"name": team},
        "minute": 0,
        "tactics": {"lineup": [{"player": {"name": p}} for p in players]},
    }


def _corner(team):
    return {
        "type": {"name": "Pass"},
        "team": {"name": team},
        "pass": {"type": {"name": "Corner"}},
    }


def _shot(team, player, xg, goal=False, penalty=False):
    shot = {"statsbomb_xg": xg}
    if goal:
        shot["outcome"] = {"name": "Goal"}
    if penalty:
        shot["type"] = {"name": "Penalty"}
    return {
        "type": {"name": "Shot"},
        "team": {"name": team},
        "player": {"name": player},
        "shot": shot,
        "minute": 50,
    }


def _foul(team, card=None):
    ev = {"type": {"name": "Foul Committed"}, "team": {"name": team},
          "foul_committed": {}}
    if card:
        ev["foul_committed"]["card"] = {"name": card}
    return ev


def _bad_behaviour(team, card):
    return {"type": {"name": "Bad Behaviour"}, "team": {"name": team},
            "bad_behaviour": {"card": {"name": card}}}


BASE = [_xi("HomeFC"), _xi("AwayFC")]


def test_corner_attribution():
    events = BASE + [_corner("HomeFC"), _corner("HomeFC"), _corner("AwayFC")]
    props = match_props(events)
    assert props["corners_home"] == 2
    assert props["corners_away"] == 1


def test_second_yellow_counts_as_one_red():
    # Player gets Yellow, then Second Yellow: 1 yellow + 1 red total.
    events = BASE + [
        _foul("AwayFC", card="Yellow Card"),
        _foul("AwayFC", card="Second Yellow"),
        _bad_behaviour("HomeFC", card="Red Card"),
    ]
    props = match_props(events)
    assert props["yellows_away"] == 1
    assert props["reds_away"] == 1
    assert props["reds_home"] == 1
    assert props["yellows_home"] == 0
    # Fouls counted regardless of card.
    assert props["fouls_away"] == 2


def test_xg_and_goal_summing():
    events = BASE + [
        _shot("HomeFC", "A", 0.3, goal=True),
        _shot("HomeFC", "A", 0.2),
        _shot("AwayFC", "B", 0.05),
    ]
    props = match_props(events)
    assert props["shots_home"] == 2
    assert props["shots_away"] == 1
    assert props["goals_home"] == 1
    assert props["goals_away"] == 0
    assert math.isclose(props["xg_home"], 0.5)
    assert math.isclose(props["xg_away"], 0.05)


def test_player_shares_excludes_penalty_xg():
    m1 = BASE + [
        _shot("HomeFC", "Striker", 0.4, goal=True),
        _shot("HomeFC", "Striker", 0.76, goal=True, penalty=True),
    ]
    m2 = BASE + [_shot("HomeFC", "Striker", 0.1)]
    df = player_shares({1: m1, 2: m2})
    row = df[df["player"] == "Striker"].iloc[0]
    assert row["shots"] == 3
    assert row["goals"] == 2
    assert math.isclose(row["xg_sum"], 1.26)
    assert math.isclose(row["npxg_sum"], 0.5)  # penalty excluded
    assert row["matches"] == 2


def test_player_minutes_from_lineups_and_subs():
    events = [
        _xi("HomeFC", players=["Starter"]),
        _xi("AwayFC", players=["Opp"]),
        {"type": {"name": "Substitution"}, "team": {"name": "HomeFC"},
         "minute": 60, "player": {"name": "Starter"},
         "substitution": {"replacement": {"name": "Sub"}}},
        {"type": {"name": "Half End"}, "team": {"name": "HomeFC"},
         "minute": 93},
    ]
    df = player_shares({1: events})
    by = {r["player"]: r for _, r in df.iterrows()}
    assert by["Starter"]["minutes"] == 60
    assert by["Sub"]["minutes"] == 33
    assert by["Opp"]["minutes"] == 93


@pytest.mark.skipif(os.environ.get("WCA_NET") != "1",
                    reason="network test; set WCA_NET=1 to enable")
def test_fetch_matches_live(tmp_path):
    from wca.data.statsbomb import fetch_matches
    matches = fetch_matches(43, 3, cache_dir=str(tmp_path))
    assert len(matches) == 64
