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
                 "market": 0.40, "bid": 0.39, "ask": 0.41,
                 "complement_market": 0.60, "complement_bid": 0.58,
                 "complement_ask": 0.61, "model": 0.55,
                 "token_id": "pm1", "complement_token_id": "pm1-no",
                 "complement_quote_basis": "independent_token_book"},
                {"label": "First corner France", "family": "corners", "settlement": "90min",
                 "market": 0.51, "bid": 0.50, "ask": 0.52,
                 "complement_market": 0.49, "complement_bid": 0.47,
                 "complement_ask": 0.50, "model": None,
                 "token_id": "pm2", "complement_token_id": "pm2-no",
                 "complement_quote_basis": "independent_token_book"},
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


def test_report_probabilities_and_tokens_are_for_held_side():
    con = S.connect(":memory:")
    forest = _forest()
    forest["fixtures"][0]["rows"][1].update(
        {"market": 0.60, "bid": 0.59, "ask": 0.61,
         "complement_market": 0.40, "complement_bid": 0.39,
         "complement_ask": 0.41, "model": 0.20,
         "token_id": "YES_TOKEN", "complement_token_id": "NO_TOKEN"})
    S.run_cycle(con, forest=forest, ts_utc="2026-07-16T12:05:00Z")
    row = next(p for p in S.report(con)["open_positions"]
               if p["selection"] == "France")
    assert row["side"] == "NO"
    assert row["entry_yes_reference"] == 0.60
    assert row["outcome_market_prob_at_entry"] == 0.40
    assert row["current_outcome_market_prob"] == 0.40
    assert row["forecast_prob"] == 0.80
    assert row["entry_cost"] > row["outcome_market_prob_at_entry"]
    assert row["instrument_id"] == "NO_TOKEN"
    assert row["execution_style"] == "marketable_limit"


def test_report_exposes_model_vs_market_only_provenance():
    con = S.connect(":memory:")
    S.run_cycle(con, forest=_forest(), ts_utc="2026-07-16T12:05:00Z")
    rows = S.report(con)["open_positions"]
    model = next(p for p in rows if p["selection"] == "France")
    exploration = next(p for p in rows if p["selection"] == "First corner France")
    assert model["exploration"] == 0
    assert model["forecast_source"] == "production_model"
    assert exploration["exploration"] == 1
    assert exploration["forecast_source"] == "market_prior_exploration"


def test_connect_clears_legacy_yes_token_from_no_position(tmp_path):
    path = str(tmp_path / "shadow.db")
    con = S.connect(path)
    S.run_cycle(con, forest={"fixtures": []}, ts_utc="2026-07-16T12:00:00Z")
    run_id = con.execute("SELECT id FROM shadow_runs").fetchone()[0]
    obs = S._insert_observation(
        con, run_id=run_id, ts_utc="t", venue="polymarket", fixture="A vs B",
        market_key="k", family="x", selection="x", settlement="90min",
        instrument_id="YES_TOKEN", yes_price=.7, raw=.2, calibrated=.2,
        source="test", calibration_n=0)
    S._record_decision(
        con, observation_id=obs, ts_utc="t", action="enter", side="NO",
        reason="test", edge=.1, stake=1, exploration=False,
        policy=S.ShadowPolicy(), position={"venue":"polymarket","fixture":"A vs B",
        "market_key":"k","family":"x","selection":"x","settlement":"90min",
        "instrument_id":"YES_TOKEN","price":.3,"forecast":.8})
    con.commit(); con.close()
    con = S.connect(path)
    assert con.execute("SELECT instrument_id FROM shadow_positions").fetchone()[0] is None


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
