from __future__ import annotations

import json

from wca.bot import app


def _scores_feed():
    return {
        "meta": {"generated": "2026-06-20 10:00:00 UTC"},
        "fixtures": [
            {
                "fixture": "Alpha vs Bravo",
                "kickoff": "2026-06-20T18:00:00+00:00",
                "scores": [
                    {"score": "1-0", "prob": 16.0, "fair": 6.25},
                    {"score": "1-1", "prob": 12.0, "fair": 8.33},
                    {"score": "2-0", "prob": 10.0, "fair": 10.0},
                ],
                "over_under": {"line": 2.5, "over": 42.0, "under": 58.0},
                "btts": 44.0,
            }
        ],
    }


def test_goalscorers_falls_back_to_score_feed_and_player_overrides(tmp_path):
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps(_scores_feed()), encoding="utf-8")
    players_path = tmp_path / "players.json"
    players_path.write_text(
        json.dumps(
            {
                "Alpha": [
                    {
                        "name": "Alice Striker",
                        "npxg_share": 0.35,
                        "penalty_taker": True,
                        "expected_minutes": 85,
                    }
                ],
                "Bravo": [
                    {
                        "name": "Bob Forward",
                        "npxg_share": 0.30,
                        "penalty_taker": False,
                        "expected_minutes": 80,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    out = app.handle_goalscorers(
        next_path=str(tmp_path / "missing_next.md"),
        now_utc="2026-06-20T11:00:00",
        scores_path=str(scores_path),
        players_path=str(players_path),
        statsbomb_players_path=str(tmp_path / "missing_stats.csv"),
    )

    assert "Goalscorers — Alpha vs Bravo" in out
    assert "Team xG approx" in out
    assert "Alice Striker" in out
    assert "Bob Forward" in out
    assert "anytime" in out
    assert "fair" in out
    assert "No active next-match cache" not in out


def test_statsbomb_players_can_feed_goalscorer_fallback(tmp_path):
    stats = tmp_path / "props_players.csv"
    stats.write_text(
        "\n".join(
            [
                "player,team,minutes,shots,goals,xg_sum,npxg_sum,matches",
                "Alice Striker,Alpha,180,8,2,1.4,1.0,2",
                "Alice Winger,Alpha,90,5,1,0.5,0.5,1",
                "Other Player,Other,90,3,0,0.3,0.3,1",
            ]
        ),
        encoding="utf-8",
    )

    players = app._statsbomb_players_for_team("Alpha", str(stats), limit=2)

    assert [p.name for p in players] == ["Alice Striker", "Alice Winger"]
    assert players[0].npxg_share > players[1].npxg_share
    assert players[0].expected_minutes == 90.0
