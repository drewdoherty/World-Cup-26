"""Tests for the Model-vs-Venue data layer (wca.venuesdata).

Covers the correctness-critical joins with synthetic in-memory DBs: no-lookahead
quote matching, freshness capping, incomplete-book omission, the realised-outcome
accuracy block (agreement != accuracy), exact bet linkage with audit, and
byte-deterministic feed output.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from wca import venuesdata as vd
from wca import venuesbench as vb


def _dt(s):
    return vd.parse_ts(s)


# --------------------------------------------------------------------------- #
# parse_ts / outcome mapping
# --------------------------------------------------------------------------- #


def test_parse_ts_handles_naive_and_aware():
    a = vd.parse_ts("2026-06-13T00:09:50")               # naive -> UTC
    b = vd.parse_ts("2026-06-11T13:27:30.716212+00:00")  # aware + micros
    c = vd.parse_ts("2026-06-13 01:00:00+00:00")         # space + tz
    assert a.tzinfo is not None and b.tzinfo is not None and c.tzinfo is not None
    assert a < c


def test_outcome_to_leg_and_aliases():
    assert vd.map_outcome_to_leg("Mexico", "Mexico", "South Africa") == "Home"
    assert vd.map_outcome_to_leg("South Africa", "Mexico", "South Africa") == "Away"
    assert vd.map_outcome_to_leg("Draw", "Mexico", "South Africa") == "Draw"
    # alias bridge: USA == United States
    assert vd.map_outcome_to_leg("USA", "USA", "Paraguay") == "Home"
    assert vd.pair_key("USA", "Paraguay") == vd.pair_key("United States", "Paraguay")


# --------------------------------------------------------------------------- #
# per_book_quotes_from_rows: no-lookahead, freshness, incomplete
# --------------------------------------------------------------------------- #


def _rows(book, home, away, h, d, a, ts):
    """Three QuoteRows (one per leg) for a book at one timestamp."""
    t = _dt(ts)
    return [
        (book, home, home, away, t, h),
        (book, "Draw", home, away, t, d),
        (book, away, home, away, t, a),
    ]


def test_no_lookahead_excludes_future_quotes():
    home, away = "Mexico", "South Africa"
    as_of = _dt("2026-06-11T12:00:00")
    rows = []
    rows += _rows("paddypower", home, away, 1.50, 4.0, 7.0, "2026-06-11T10:00:00")  # before
    rows += _rows("paddypower", home, away, 9.99, 9.9, 9.9, "2026-06-11T13:00:00")  # AFTER as_of
    q = vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600)
    assert "Paddy Power" in q
    # The future (post as_of) quote must NOT be used: odds reflect the 10:00 row.
    assert q["Paddy Power"]["odds"]["Home"] == 1.50


def test_freshness_drops_stale_book():
    home, away = "Mexico", "South Africa"
    as_of = _dt("2026-06-11T20:00:00")
    rows = _rows("coral", home, away, 1.5, 4.0, 7.0, "2026-06-11T10:00:00")  # 10h old
    q = vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600)  # 6h cap
    assert q == {}  # stale -> omitted, never imputed


def test_incomplete_book_omitted():
    home, away = "Mexico", "South Africa"
    as_of = _dt("2026-06-11T12:00:00")
    # Only Home + Draw present (missing Away leg).
    t = _dt("2026-06-11T10:00:00")
    rows = [("betway", home, home, away, t, 1.5), ("betway", "Draw", home, away, t, 4.0)]
    assert vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600) == {}


def test_devig_sums_to_one_per_book():
    home, away = "Mexico", "South Africa"
    as_of = _dt("2026-06-11T12:00:00")
    rows = _rows("skybet", home, away, 1.8, 3.5, 4.5, "2026-06-11T11:30:00")
    q = vd.per_book_quotes_from_rows(rows, as_of, freshness_s=6 * 3600)
    assert sum(q["Sky Bet"]["fair"]) == pytest.approx(1.0, abs=1e-9)
    assert q["Sky Bet"]["age_s"] == pytest.approx(1800.0)


# --------------------------------------------------------------------------- #
# Synthetic DBs for integration
# --------------------------------------------------------------------------- #


def _odds_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
                "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)")
    return con


def _insert_book(con, match_id, home, away, book, h, d, a, ts):
    for outcome, odds in ((home, h), ("Draw", d), (away, a)):
        raw = json.dumps({"bookmaker_key": book, "outcome_name": outcome,
                          "home_team": home, "away_team": away})
        con.execute("INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
                    (ts, "theoddsapi", match_id, "h2h", outcome, odds, raw))


def _pred_db():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE predictions (build_id TEXT, match_id TEXT, fixture TEXT, "
        "kickoff_utc TEXT, ts_utc TEXT, market TEXT, selection TEXT, model_prob REAL, "
        "elo_prob REAL, dc_prob REAL, market_devig_prob REAL, closing_devig_prob REAL, "
        "placed INTEGER, status TEXT, stage TEXT)")
    return con


def _insert_pred(con, build, mid, fixture, kickoff, ts, triple, won_leg):
    elo = triple
    for i, leg in enumerate(vb.LEGS):
        status = "won" if leg == won_leg else "lost"
        con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (build, mid, fixture, kickoff, ts, "1X2", leg, triple[i],
                     elo[i], triple[i], triple[i], triple[i], 0, status, ""))


def test_build_arm_a_integration():
    odds = _odds_db()
    mid = "abc123"
    home, away = "Mexico", "South Africa"
    # Two books quote before the build (ts 12:00); one is closer to the model.
    _insert_book(odds, mid, home, away, "paddypower", 2.0, 3.3, 4.0, "2026-06-11T11:00:00")
    _insert_book(odds, mid, home, away, "skybet", 1.5, 4.5, 7.0, "2026-06-11T11:30:00")
    recs = [{
        "build_id": "b1", "match_id": mid, "fixture": "Mexico vs South Africa",
        "kickoff": _dt("2026-06-11T19:00:00"), "ts": _dt("2026-06-11T12:00:00"),
        "stage": "", "legs": {
            "Home": {"model": 0.5, "elo": 0.5, "dc": 0.5, "market": 0.5, "closing": 0.5},
            "Draw": {"model": 0.3, "elo": 0.3, "dc": 0.3, "market": 0.3, "closing": 0.3},
            "Away": {"model": 0.2, "elo": 0.2, "dc": 0.2, "market": 0.2, "closing": 0.2},
        }, "placed": set(), "outcome": "Home",
    }]
    arm = vd.build_arm_a(recs, odds, freshness_s=24 * 3600)
    assert arm["coverage"]["n_venues"] == 2
    assert set(arm["coverage"]["venues"]) == {"Paddy Power", "Sky Bet"}
    obs = "%s|b1" % mid
    assert obs in arm["panels"]["ex_market"]["mae"]
    # Accuracy computed on the settled fixture.
    assert arm["accuracy"]["n_fixtures"] == 1
    assert arm["accuracy"]["venues"]["Paddy Power"]["n"] == 1


def test_link_model_bets_audit_and_link():
    pred = _pred_db()
    bets = sqlite3.connect(":memory:")
    bets.execute("CREATE TABLE bets (id INTEGER, ts_utc TEXT, match_desc TEXT, "
                 "market TEXT, selection TEXT, source TEXT)")
    # A prediction build at 12:00 for USA vs Paraguay.
    _insert_pred(pred, "b1", "m1", "United States vs Paraguay",
                 "2026-06-13T19:00:00", "2026-06-13T12:00:00",
                 (0.5, 0.3, 0.2), "Home")
    # Bet AFTER the build, 1X2 market, matching fixture+leg (alias USA) -> linked.
    bets.execute("INSERT INTO bets VALUES (1,'2026-06-13T15:00:00','USA vs Paraguay','Match Odds','USA','model')")
    # Bet BEFORE any build -> audited as predates.
    bets.execute("INSERT INTO bets VALUES (2,'2026-06-11T09:00:00','USA vs Paraguay','h2h','USA','model')")
    # Non-1X2 market -> audited.
    bets.execute("INSERT INTO bets VALUES (3,'2026-06-13T15:00:00','USA vs Paraguay','Both Teams To Score','Yes','model')")
    out = vd.link_model_bets(pred, bets)
    assert out["n_model_bets"] == 3
    assert out["n_linked"] == 1
    reasons = {u["reason"] for u in out["unmatched_audit"]}
    assert "bet_predates_first_build" in reasons
    assert "non_1x2_market" in reasons
    assert out["insufficient"] is True  # 1 < 25


def test_assemble_feed_deterministic():
    odds = _odds_db()
    mid = "abc123"
    _insert_book(odds, mid, "Mexico", "South Africa", "paddypower", 2.0, 3.3, 4.0, "2026-06-11T11:00:00")
    _insert_book(odds, mid, "Mexico", "South Africa", "skybet", 1.5, 4.5, 7.0, "2026-06-11T11:30:00")
    recs = [{
        "build_id": "b1", "match_id": mid, "fixture": "Mexico vs South Africa",
        "kickoff": _dt("2026-06-11T19:00:00"), "ts": _dt("2026-06-11T12:00:00"),
        "stage": "", "legs": {
            "Home": {"model": 0.5, "elo": 0.5, "dc": 0.5, "market": 0.5, "closing": 0.5},
            "Draw": {"model": 0.3, "elo": 0.3, "dc": 0.3, "market": 0.3, "closing": 0.3},
            "Away": {"model": 0.2, "elo": 0.2, "dc": 0.2, "market": 0.2, "closing": 0.2},
        }, "placed": set(), "outcome": "Home",
    }]
    placed = {"n_model_bets": 0, "n_linked": 0, "n_unmatched": 0, "linked": [],
              "unmatched_audit": [], "insufficient": True, "note": "x"}
    a1 = vd.build_arm_a(recs, odds, freshness_s=24 * 3600)
    f1 = vd.assemble_feed(a1, placed, generated="2026-06-27T00:00:00Z",
                          window="w", model_variant="v", freshness_s=21600, n_boot=200)
    f2 = vd.assemble_feed(a1, placed, generated="2026-06-27T00:00:00Z",
                          window="w", model_variant="v", freshness_s=21600, n_boot=200)
    assert json.dumps(f1, sort_keys=True) == json.dumps(f2, sort_keys=True)
    assert f1["polymarket"]["state"] == "COLLECTING"
