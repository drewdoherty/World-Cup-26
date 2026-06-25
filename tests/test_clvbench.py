"""Offline tests for the full-book CLV benchmark (:mod:`wca.clvbench`).

A tiny synthetic 3-fixture join is built in-memory (an ``odds_snapshots`` +
``bets`` SQLite DB matching the production schema) so the whole pipeline —
``consensus_close`` -> fair-vs-fair CLV -> aggregates -> feed — runs end to
end with hand-computable numbers.  No network, no wall clock, no real DB.
"""

from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
import pytest

from wca import clvbench


# --------------------------------------------------------------------------- #
# synthetic DB
# --------------------------------------------------------------------------- #
def _raw(match_id, home, away, book, outcome, dec, ko="2026-06-13T19:00:00+00:00"):
    return json.dumps(
        {
            "commence_time": ko,
            "home_team": home,
            "away_team": away,
            "bookmaker_key": book,
            "outcome_name": outcome,
            "decimal_odds": dec,
            "market": "h2h",
        }
    )


def _mk_db():
    """In-memory ledger with odds_snapshots + bets, schema = production."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
        "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    con.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY, ts_utc TEXT, match_id TEXT, "
        "match_desc TEXT, market TEXT, selection TEXT, platform TEXT, "
        "decimal_odds REAL, stake REAL, status TEXT)"
    )
    return con


def _add_close(con, match_id, home, away, triple, ko, ts="2026-06-13T18:00:00+00:00"):
    """Insert a single-book h2h capture whose de-vigged triple == *triple*.

    A single book with decimal odds 1/p (p summing to 1) de-vigs to exactly
    *triple* (overround 1.0), so closes are exactly controllable.
    """
    for leg, name in (("home", home), ("draw", "Draw"), ("away", away)):
        # Full precision (no rounding) so a single-book devig reconstructs the
        # triple exactly — the hand-computed CLVs must be exact, not ~exact.
        dec = 1.0 / triple[leg]
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
            (ts, "test", match_id, "h2h", name, dec,
             _raw(match_id, home, away, "bookA", name, dec, ko)),
        )
    con.commit()


# Three fixtures.  CLV(leg) = p_close/p_model - 1.
#  F1 Alpha v Beta:  model home .50 close .60 -> +0.20 ; draw .25/.20 -> -0.20 ;
#                    away .25/.20 -> -0.20
#  F2 Gamma v Delta: model home .40 close .40 -> 0.0 (push) ; draw .30/.35 ->
#                    +0.1667 ; away .30/.25 -> -0.1667
#  F3 Epsilon v Zeta: model home .20 close .10 -> -0.50 ; draw .30/.30 -> 0.0 ;
#                     away .50/.60 -> +0.20
_BUILDS = [
    {
        "generated": "2026-06-12T00:00:00",
        "fixture": "Alpha vs Beta",
        "match_id": "m1",
        "kickoff": "2026-06-13 19:00:00+00:00",
        "model": {"home": 0.50, "draw": 0.25, "away": 0.25},
        "market": {"home": 0.48, "draw": 0.26, "away": 0.26},
    },
    {
        "generated": "2026-06-12T00:00:00",
        "fixture": "Gamma vs Delta",
        "match_id": "m2",
        "kickoff": "2026-06-13 19:00:00+00:00",
        "model": {"home": 0.40, "draw": 0.30, "away": 0.30},
        "market": {"home": 0.42, "draw": 0.29, "away": 0.29},
    },
    {
        "generated": "2026-06-12T00:00:00",
        "fixture": "Epsilon vs Zeta",
        "match_id": "m3",
        "kickoff": "2026-06-13 19:00:00+00:00",
        "model": {"home": 0.20, "draw": 0.30, "away": 0.50},
        "market": {"home": 0.22, "draw": 0.31, "away": 0.47},
    },
]
_KO = "2026-06-13T19:00:00+00:00"


def _db_with_three():
    con = _mk_db()
    _add_close(con, "m1", "Alpha", "Beta",
               {"home": 0.60, "draw": 0.20, "away": 0.20}, _KO)
    _add_close(con, "m2", "Gamma", "Delta",
               {"home": 0.40, "draw": 0.35, "away": 0.25}, _KO)
    _add_close(con, "m3", "Epsilon", "Zeta",
               {"home": 0.10, "draw": 0.30, "away": 0.60}, _KO)
    return con


# --------------------------------------------------------------------------- #
# Wilson / trimmed-mean unit tests
# --------------------------------------------------------------------------- #
def test_wilson_edges():
    assert clvbench.wilson(0, 0) == (None, None, None)
    p, lo, hi = clvbench.wilson(0, 1)
    assert p == 0.0 and lo >= 0.0 and 0.0 < hi <= 1.0
    p, lo, hi = clvbench.wilson(1, 1)
    assert p == 1.0 and 0.0 <= lo < 1.0 and hi <= 1.0
    p, lo, hi = clvbench.wilson(5, 10)
    assert abs(p - 0.5) < 1e-12 and lo < 0.5 < hi


def test_trimmed_mean_drops_tails():
    # 10% of 10 = 1 trimmed each side -> mean of 2..9
    xs = [0, 1, 2, 3, 4, 5, 6, 7, 8, 100]
    tm = clvbench.trimmed_mean(xs, 0.10)
    assert abs(tm - np.mean(xs[1:-1])) < 1e-9
    # tiny sample: trimming would empty -> falls back to plain mean
    assert clvbench.trimmed_mean([2.0, 4.0], 0.10) == 3.0
    assert clvbench.trimmed_mean([]) is None


# --------------------------------------------------------------------------- #
# hand-computed CLV on the synthetic join (fair-vs-fair)
# --------------------------------------------------------------------------- #
def test_clv_hand_computed_fair_vs_fair():
    con = _db_with_three()
    legs = clvbench.build_legs(_BUILDS, con, placed=set())
    by = {(lg.fixture, lg.leg): lg for lg in legs}

    # F1
    assert math.isclose(by[("Alpha vs Beta", "home")].clv, 0.60 / 0.50 - 1, rel_tol=1e-9)
    assert math.isclose(by[("Alpha vs Beta", "draw")].clv, 0.20 / 0.25 - 1, rel_tol=1e-9)
    assert math.isclose(by[("Alpha vs Beta", "away")].clv, 0.20 / 0.25 - 1, rel_tol=1e-9)
    # F2 home is a PUSH (0.40/0.40)
    assert math.isclose(by[("Gamma vs Delta", "home")].clv, 0.0, abs_tol=1e-9)
    assert math.isclose(by[("Gamma vs Delta", "draw")].clv, 0.35 / 0.30 - 1, rel_tol=1e-9)
    # F3
    assert math.isclose(by[("Epsilon vs Zeta", "home")].clv, 0.10 / 0.20 - 1, rel_tol=1e-9)
    assert math.isclose(by[("Epsilon vs Zeta", "away")].clv, 0.60 / 0.50 - 1, rel_tol=1e-9)

    # edge = p_model - p_market_build
    assert math.isclose(by[("Alpha vs Beta", "home")].edge, 0.50 - 0.48, rel_tol=1e-9)


def test_pushes_excluded_from_beat_rate():
    con = _db_with_three()
    legs = clvbench.build_legs(_BUILDS, con, placed=set())
    clvs = [lg.clv for lg in legs if lg.clv is not None]
    # 9 legs, two pushes (F2 home 0.40/0.40 and F3 draw 0.30/0.30) excluded
    # from BOTH numerator and denominator -> denominator 7
    p, lo, hi, n = clvbench._beat_rate(clvs)
    assert n == 7
    # wins (clv>0): F1 home, F2 draw, F3 away = 3
    assert math.isclose(p, 3.0 / 7.0, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# missing-close leg counted but excluded from CLV
# --------------------------------------------------------------------------- #
def test_missing_close_excluded_but_counted():
    con = _mk_db()
    # only F1 and F3 get a close; F2 has none
    _add_close(con, "m1", "Alpha", "Beta",
               {"home": 0.60, "draw": 0.20, "away": 0.20}, _KO)
    _add_close(con, "m3", "Epsilon", "Zeta",
               {"home": 0.10, "draw": 0.30, "away": 0.60}, _KO)
    legs = clvbench.build_legs(_BUILDS, con, placed=set())
    assert len(legs) == 9                       # all legs constructed
    with_close = [lg for lg in legs if lg.clv is not None]
    assert len(with_close) == 6                 # F2's 3 legs have no close
    f2 = [lg for lg in legs if lg.fixture == "Gamma vs Delta"]
    assert all(lg.clv is None for lg in f2)
    # coverage on the feed reflects the gap
    payload = clvbench.build_benchmark(_BUILDS, con, "2026-06-25T00:00:00Z",
                                       placed=set())
    assert payload["meta"]["n_legs"] == 9
    assert payload["meta"]["n_with_close"] == 6
    assert payload["headline"]["coverage_pct"] == round(100.0 * 6 / 9, 2)


# --------------------------------------------------------------------------- #
# empty bucket emits n:0 (never dropped, never invented)
# --------------------------------------------------------------------------- #
def test_empty_buckets_emit_n_zero():
    con = _db_with_three()
    payload = clvbench.build_benchmark(_BUILDS, con, "2026-06-25T00:00:00Z",
                                       placed=set())
    # every declared edge bucket is present
    labels = [b["bucket"] for b in payload["by_edge_bucket"]]
    assert labels == [lbl for lbl, _, _ in clvbench._EDGE_BUCKETS]
    # at least one is empty given only 9 legs -> n:0, clv_mean None
    empty = [b for b in payload["by_edge_bucket"] if b["n"] == 0]
    assert empty
    for b in empty:
        assert b["clv_mean"] is None
    # every odds bucket present too
    obl = [b["bucket"] for b in payload["by_odds_bucket"]]
    assert obl == [lbl for lbl, _, _ in clvbench._ODDS_BUCKETS]
    # all three market legs present and never pooled
    assert [m["leg"] for m in payload["by_market"]] == list(clvbench.LEGS)


# --------------------------------------------------------------------------- #
# placebo ~ noise baseline (NOT exactly 0.5, absorbs overround/skew)
# --------------------------------------------------------------------------- #
def test_placebo_in_plausible_band():
    con = _db_with_three()
    legs = clvbench.build_legs(_BUILDS, con, placed=set())
    placebo = clvbench.placebo_beat_rate(legs, _BUILDS, con, n_shuffles=200)
    assert placebo is not None
    # skill-free baseline lands in a broad band around 0.5 (the closes are
    # real and skewed, so it need not be exactly 0.5, but it can't be extreme).
    assert 0.2 <= placebo <= 0.8


def test_placebo_none_when_nothing_to_permute():
    # one fixture in its own build -> nothing to shuffle within a build
    con = _mk_db()
    _add_close(con, "m1", "Alpha", "Beta",
               {"home": 0.60, "draw": 0.20, "away": 0.20}, _KO)
    single = [_BUILDS[0]]
    legs = clvbench.build_legs(single, con, placed=set())
    assert clvbench.placebo_beat_rate(legs, single, con, n_shuffles=10) is None


# --------------------------------------------------------------------------- #
# placed vs passed split
# --------------------------------------------------------------------------- #
def test_placed_vs_passed_split():
    con = _db_with_three()
    # back Alpha (F1 home) only -> that leg is "placed", rest "passed"
    con.execute(
        "INSERT INTO bets (ts_utc, match_id, match_desc, market, selection, "
        "platform, decimal_odds, stake, status) VALUES "
        "('2026-06-12T00:00:00','m1','Alpha vs Beta','h2h','Alpha','smarkets',"
        "2.0,10,'open')"
    )
    con.commit()
    placed = clvbench.placed_legs(con)
    payload = clvbench.build_benchmark(_BUILDS, con, "2026-06-25T00:00:00Z",
                                       placed=placed)
    pvp = payload["placed_vs_passed"]
    assert pvp["placed"]["n"] == 1            # only the Alpha home leg
    assert pvp["passed"]["n"] == 8            # other 8 close-bearing legs
    # placed median == the F1-home CLV (+0.20)
    assert math.isclose(pvp["placed"]["clv_median"], 0.20, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# full feed shape + JSON-serialisable + no NaN
# --------------------------------------------------------------------------- #
def test_feed_shape_and_json_safe():
    con = _db_with_three()
    payload = clvbench.build_benchmark(_BUILDS, con, "2026-06-25T00:00:00Z",
                                       placed=set())
    # exact top-level keys required by the spec
    assert set(payload) >= {
        "meta", "headline", "by_edge_bucket", "by_market", "by_odds_bucket",
        "placed_vs_passed", "coverage_by_market", "note",
    }
    assert set(payload["meta"]) == {"generated", "n_legs", "n_with_close"}
    head = payload["headline"]
    for k in ("beat_rate", "placebo_null", "clv_median", "clv_trimmed_mean",
              "lead_rate", "drift_beta", "brier_skill", "coverage_pct"):
        assert k in head
    assert set(payload["coverage_by_market"]) == set(clvbench.LEGS)
    for leg, cov in payload["coverage_by_market"].items():
        assert set(cov) == {"n", "clv_n", "coverage_pct"}
    # must serialise with allow_nan=False (no NaN/Inf leaked anywhere)
    s = json.dumps(payload, allow_nan=False)
    assert isinstance(s, str)


def test_empty_input_safe():
    con = _mk_db()
    payload = clvbench.build_benchmark([], con, "2026-06-25T00:00:00Z",
                                       placed=set())
    assert payload["meta"]["n_legs"] == 0
    assert payload["meta"]["n_with_close"] == 0
    assert payload["headline"]["beat_rate"]["p"] is None
    assert payload["headline"]["coverage_pct"] == 0.0
    # buckets still all present with n:0 (never dropped)
    assert len(payload["by_edge_bucket"]) == len(clvbench._EDGE_BUCKETS)
    assert all(b["n"] == 0 for b in payload["by_edge_bucket"])
    json.dumps(payload, allow_nan=False)  # serialises
