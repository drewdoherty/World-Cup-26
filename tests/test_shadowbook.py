from __future__ import annotations

from wca import shadowbook as S


def _forest():
    return {
        "meta": {"generated": "2026-07-16T12:00:00Z"},
        "fixtures": [{
            "fixture": "France vs England",
            "rows": [
                {"section": "1X2"},
                {"label": "France", "family": "1x2", "settlement": "90min",
                 "market": 0.40, "model": 0.55, "token_id": "pm1"},
                {"label": "First corner France", "family": "corners", "settlement": "90min",
                 "market": 0.51, "model": None, "token_id": "pm2"},
            ],
        }],
    }


def _hl(stamp="2026-07-16T12:00:00Z"):
    return {
        "generated_at": stamp,
        "pairs": [{
            "pair_id": "champion:France", "kind": "champion",
            "hl": {"outcome_id": 1, "yes_bid": 0.39, "yes_ask": 0.40},
            "pm": {"token_yes": "py", "yes_bid": 0.44, "yes_ask": 0.45},
            "directions": {
                "safe": {
                    "edge_per_share_at_best": 0.02,
                    "executable": {"shares": 10, "cost_usd": 9.8, "profit_usd": 0.2},
                    "settlement_tail": {"gated": False, "tail": "none"},
                },
                "gated": {
                    "edge_per_share_at_best": 0.10,
                    "executable": {"shares": 10, "cost_usd": 9, "profit_usd": 1},
                    "settlement_tail": {"gated": True, "tail": "toxic"},
                },
            },
        }],
    }


def test_cycle_records_forecasts_abstentions_exploration_and_cross():
    con = S.connect(":memory:")
    result = S.run_cycle(
        con, forest=_forest(), hl_feed=_hl(), ts_utc="2026-07-16T12:05:00Z",
        policy=S.ShadowPolicy(min_edge=0.01))
    assert result["polymarket"] == {"observed": 2, "entered": 2, "explored": 1, "abstained": 0}
    assert result["hyperliquid"]["cross_entered"] == 1
    assert result["hyperliquid"]["abstained"] >= 1
    report = S.report(con)
    assert report["summary"]["observations"] == 4  # 2 forest + 2 independent venue observations
    assert report["summary"]["positions"] >= 2
    assert len(report["cross_venue"]) == 2


def test_stale_hl_feed_fails_cross_positions_closed():
    con = S.connect(":memory:")
    result = S.run_cycle(
        con, forest={"fixtures": []}, hl_feed=_hl("2026-07-15T00:00:00Z"),
        ts_utc="2026-07-16T12:05:00Z")
    assert result["hyperliquid"]["cross_entered"] == 0
    rows = con.execute("SELECT action,reason FROM shadow_cross_decisions").fetchall()
    assert rows and all(r["action"] == "abstain" for r in rows)
    assert all(r["reason"] == "stale_cross_venue_snapshot" for r in rows)


def test_settlement_updates_forecast_learning_and_position_pl():
    con = S.connect(":memory:")
    S.run_cycle(con, forest=_forest(), ts_utc="2026-07-16T12:05:00Z")
    key = "France vs England|1x2|France|90min"
    n = S.settle_market(con, key, 1.0, ts_utc="2026-07-18T23:00:00Z")
    assert n == 1
    pos = con.execute("SELECT status,settled_pl FROM shadow_positions WHERE market_key=?", (key,)).fetchone()
    assert pos["status"] == "settled" and pos["settled_pl"] > 0
    report = S.report(con)
    assert report["calibration"][0]["n"] == 1


def test_calibration_is_prequential_and_shrunk_to_raw_forecast():
    con = S.connect(":memory:")
    con.execute(
        """INSERT INTO shadow_runs(ts_utc,policy_version,policy_json,source_hash)
           VALUES('t','v','{}','h')""")
    for outcome in (1.0, 1.0, 0.0):
        con.execute(
            """INSERT INTO shadow_observations(
                   run_id,ts_utc,venue,market_key,family,selection,settlement_basis,
                   yes_price,raw_forecast,calibrated_forecast,forecast_source,outcome)
               VALUES(1,'t','polymarket','k','corners','x','90min',.5,.55,.55,'production_model',?)""",
            (outcome,))
    p, n = S.calibrated_probability(
        con, venue="polymarket", family="corners", source="production_model",
        raw=0.55, prior=20)
    assert n == 3
    assert 0.55 < p < 2.0 / 3.0


def test_fixture_resolver_covers_core_and_event_families():
    base = {"fixture": "France vs England"}
    result = {
        "fixture": "France vs England", "home_goals_90": 2, "away_goals_90": 1,
        "first_half_score": [1, 0], "second_half_score": [1, 1],
        "total_corners": 11, "first_half_corners": 4, "second_half_corners": 7,
        "first_team_to_score": "France", "scorers": {"Kylian Mbappe": 1},
        "went_extra_time": False, "penalty_shootout": False, "advanced": "France",
    }
    cases = [
        ({**base, "family": "1x2", "selection": "France"}, 1),
        ({**base, "family": "total_goals", "selection": "Over 2.5"}, 1),
        ({**base, "family": "btts", "selection": "BTTS — Yes"}, 1),
        ({**base, "family": "exact_score", "selection": "2-1"}, 1),
        ({**base, "family": "scorer_prop", "selection": "Kylian Mbappe anytime"}, 1),
        ({**base, "family": "extra_time", "selection": "Goes to Extra Time — Yes"}, 0),
        ({**base, "family": "halftime_result", "selection": "France"}, 1),
        ({**base, "family": "corners", "selection": "Total Corners: O/U 10.5"}, 1),
        ({**base, "family": "penalty_shootout", "selection": "Will the Match Go to a Penalty Shootout?"}, 0),
    ]
    for row, expected in cases:
        assert S.resolve_fixture_observation(row, result) == expected
