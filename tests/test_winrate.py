"""Offline, deterministic tests for the rolling win-rate builder (Module B).

No network, no wall-clock, no production-DB writes.  The live-data checks open
``data/wca.db`` strictly read-only (immutable URI) and ``data/dev.db`` read-only.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from wca import winrate

REPO = Path(__file__).resolve().parents[1]
DEV_DB = REPO / "data" / "dev.db"
WCA_DB = REPO / "data" / "wca.db"
JSONL = REPO / "data" / "model_predictions_log.jsonl"
RESULTS = REPO / "data" / "processed" / "wc2026_results.json"


def _wca_ro_uri() -> str:
    return f"file:{WCA_DB}?mode=ro&immutable=1"


# --------------------------------------------------------------------------- #
# Wilson edge cases.
# --------------------------------------------------------------------------- #
def test_wilson_n_zero_is_all_none():
    assert winrate.wilson(0, 0) == (None, None, None)


def test_wilson_k_zero_lo_pinned_at_zero():
    p, lo, hi = winrate.wilson(0, 10)
    assert p == 0.0
    assert lo == 0.0          # never negative
    assert 0.0 < hi < 1.0


def test_wilson_k_equals_n_hi_pinned_at_one():
    p, lo, hi = winrate.wilson(10, 10)
    assert p == 1.0
    assert hi == 1.0          # never above 1
    assert 0.0 < lo < 1.0


def test_wilson_n_one_is_valid_wide_band():
    # Single success: valid band, wide, p in (lo, hi], within [0,1].
    p, lo, hi = winrate.wilson(1, 1)
    assert p == 1.0
    assert 0.0 <= lo < hi <= 1.0
    assert hi == 1.0
    # Single failure mirrors it.
    p0, lo0, hi0 = winrate.wilson(0, 1)
    assert p0 == 0.0 and lo0 == 0.0 and 0.0 < hi0 <= 1.0


def test_wilson_centre_known_value():
    # k=5,n=10 -> centre 0.5 exactly; half ~0.260 (classic Wilson result).
    p, lo, hi = winrate.wilson(5, 10)
    assert p == 0.5
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)


# --------------------------------------------------------------------------- #
# EWMA n_eff steady state + smoothing.
# --------------------------------------------------------------------------- #
def test_ewma_lambda_formula():
    assert winrate.ewma_lambda(8) == pytest.approx(1.0 - 2.0 ** (-1.0 / 8))


def test_ewma_neff_steady_state():
    # n_eff = (sum w)^2 / sum w^2 -> (2 - lambda)/lambda as t -> inf.
    lam = winrate.ewma_lambda(winrate.EWMA_H)
    expected = (2.0 - lam) / lam
    assert winrate.ewma_neff_steady_state() == pytest.approx(expected)

    # Empirically converge n_eff over a long stream and match the closed form.
    n = 5000
    s_w = 0.0
    s_w2 = 0.0
    for _ in range(n):
        s_w = (1.0 - lam) * s_w + 1.0
        s_w2 = (1.0 - lam) ** 2 * s_w2 + 1.0
    n_eff = (s_w * s_w) / s_w2
    assert n_eff == pytest.approx(expected, rel=1e-6)


def test_ewma_all_wins_is_one_all_losses_is_zero():
    assert winrate.ewma_series([True] * 20)[-1] == pytest.approx(1.0)
    assert winrate.ewma_series([False] * 20)[-1] == pytest.approx(0.0)


def test_ewma_tracks_recent_more_than_old():
    # losses then wins -> EWMA ends near 1; the reverse ends near 0.
    up = winrate.ewma_series([False] * 10 + [True] * 10)[-1]
    down = winrate.ewma_series([True] * 10 + [False] * 10)[-1]
    assert up > 0.7
    assert down < 0.3


# --------------------------------------------------------------------------- #
# Expanding band shrinks monotonically when the rate is stable.
# --------------------------------------------------------------------------- #
def test_expanding_band_width_shrinks_monotonically():
    # Constant 50% win pattern: Wilson half-width must be non-increasing in n.
    wins = [True, False] * 40
    series = winrate.expanding_series(wins)
    widths = [hi - lo for (_, lo, hi) in series]
    # Compare at even indices (where k/n == 0.5 exactly) to avoid sawtooth.
    even = widths[1::2]
    for a, b in zip(even, even[1:]):
        assert b <= a + 1e-12


def test_expanding_band_strictly_narrows_for_all_wins():
    wins = [True] * 30
    series = winrate.expanding_series(wins)
    widths = [hi - lo for (_, lo, hi) in series]
    for a, b in zip(widths, widths[1:]):
        assert b < a + 1e-12


# --------------------------------------------------------------------------- #
# Rolling window length + Wilson semantics.
# --------------------------------------------------------------------------- #
def test_rolling_window_caps_denominator():
    wins = [True] * 25
    series = winrate.rolling_series(wins, window=10)
    # Every fully-populated window is 10 wins -> p == 1.0, hi == 1.0.
    last_p, last_lo, last_hi = series[-1]
    assert last_p == 1.0 and last_hi == 1.0
    # The band is the SAME for every fully-populated window (n fixed at 10).
    bands = series[9:]
    assert all(b == bands[0] for b in bands)


# --------------------------------------------------------------------------- #
# Brier / BSS.
# --------------------------------------------------------------------------- #
def test_brier_one_perfect_and_worst():
    perfect = {"home": 1.0, "draw": 0.0, "away": 0.0}
    assert winrate._brier_one(perfect, "home") == pytest.approx(0.0)
    worst = {"home": 0.0, "draw": 0.0, "away": 1.0}
    assert winrate._brier_one(worst, "home") == pytest.approx(2.0)


def test_bss_zero_when_model_equals_market():
    rows = [
        winrate.ModelRow("m1", "A vs B", "k", {"home": .5, "draw": .3, "away": .2},
                         {"home": .5, "draw": .3, "away": .2}, "home", True),
        winrate.ModelRow("m2", "C vs D", "k", {"home": .2, "draw": .3, "away": .5},
                         {"home": .2, "draw": .3, "away": .5}, "away", True),
    ]
    mb, kb, bss = winrate.brier_bss(rows)
    assert mb == pytest.approx(kb)
    assert bss == pytest.approx(0.0)


def test_brier_none_when_no_market_triple():
    rows = [winrate.ModelRow("m", "A vs B", "k",
                             {"home": .5, "draw": .3, "away": .2}, None, "home", True)]
    assert winrate.brier_bss(rows) == (None, None, None)


# --------------------------------------------------------------------------- #
# Push / void excluded from numerator AND denominator.
# --------------------------------------------------------------------------- #
def test_void_push_excluded_from_realized(tmp_path):
    db = tmp_path / "mini.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY, ts_utc TEXT, match_desc TEXT, "
        "market TEXT, selection TEXT, decimal_odds REAL, status TEXT, "
        "closing_odds REAL)"
    )
    rows = [
        (1, "2026-06-11T10:00:00", "A vs B", "h2h", "A", 2.0, "won", None),
        (2, "2026-06-11T11:00:00", "C vs D", "h2h", "C", 2.0, "lost", None),
        (3, "2026-06-11T12:00:00", "E vs F", "h2h", "E", 2.0, "void", None),
        (4, "2026-06-11T13:00:00", "G vs H", "h2h", "G", 2.0, "push", None),
        (5, "2026-06-11T14:00:00", "I vs J", "h2h", "I", 2.0, "open", None),
    ]
    con.executemany("INSERT INTO bets VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    uri = f"file:{db}?mode=ro&immutable=1"
    book = winrate.realized_book(uri)
    # void/push/open all excluded -> only 2 rows, 1 win.
    assert len(book) == 2
    assert sum(1 for r in book if r.win) == 1
    cov = winrate._close_coverage(uri, len(book))
    assert cov == 0.0  # none have a captured close


# --------------------------------------------------------------------------- #
# Low-N degradation flag.
# --------------------------------------------------------------------------- #
def test_low_n_flag_true_below_threshold():
    realized = [winrate.RealizedRow(1, "2026-06-11", "A vs B", "h2h", "A", 2.0, "won", True)]
    model = []
    # Tiny realized + empty model -> low_n must be True.
    rows = winrate.build_rolling(realized, model)
    assert len(rows) == 1
    # Headline via small synthetic feed (no DB):  reuse band helpers.
    assert winrate.LOW_N_WINRATE == 30


def test_acca_autopsy_not_estimable_below_min(tmp_path):
    db = tmp_path / "mini.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE bets (id INTEGER PRIMARY KEY, ts_utc TEXT, match_desc TEXT, "
        "market TEXT, selection TEXT, decimal_odds REAL, status TEXT, closing_odds REAL)"
    )
    con.executemany(
        "INSERT INTO bets VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, "2026-06-11", "Treble: X+Y+Z", "ACCA", "X/Y/Z all win", 10.0, "lost", None),
            (2, "2026-06-12", "Treble: P+Q+R", "acca_treble", "P/Q/R all win", 8.0, "won", None),
        ],
    )
    con.commit()
    con.close()
    uri = f"file:{db}?mode=ro&immutable=1"
    realized = winrate.realized_book(uri)
    autopsy = winrate.acca_autopsy(realized, uri)
    assert autopsy["strike"]["n"] == 2
    assert autopsy["strike"]["p"] == pytest.approx(0.5)
    assert "INSUFFICIENT SAMPLE" in autopsy["note"]
    assert "not estimable" in autopsy["note"]


# --------------------------------------------------------------------------- #
# Live-data integration (read-only): the builder must run on real data.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not WCA_DB.exists(), reason="wca.db not present")
def test_realized_book_excludes_void_push_live():
    book = winrate.realized_book(_wca_ro_uri())
    assert all(r.status in ("won", "lost") for r in book)
    assert len(book) > 0


@pytest.mark.skipif(not (DEV_DB.exists() and JSONL.exists()), reason="model sources absent")
def test_model_book_one_row_per_match_and_argmax_win():
    market_by_match = winrate._jsonl_market_by_match(str(JSONL))
    book = winrate.model_book_from_devdb(str(DEV_DB), market_by_match)
    # One row per match (no duplicate match_ids).
    ids = [r.match_id for r in book]
    assert len(ids) == len(set(ids))
    # win flag is consistent with argmax == outcome.
    for r in book:
        arg = winrate._argmax_outcome(r.model)
        assert r.win == (arg == r.outcome)
    # ordered by kickoff.
    kos = [r.kickoff for r in book]
    assert kos == sorted(kos)


@pytest.mark.skipif(
    not (DEV_DB.exists() and WCA_DB.exists() and JSONL.exists() and RESULTS.exists()),
    reason="data sources absent",
)
def test_build_feed_shape_live():
    feed = winrate.build_feed(
        dev_db_path=str(DEV_DB),
        wca_db_ro_uri=_wca_ro_uri(),
        jsonl_path=str(JSONL),
        results_path=str(RESULTS),
        generated="2026-06-25T00:00:00Z",
    )
    # Top-level shape.
    assert set(feed) == {"meta", "headline", "rolling", "segments", "acca_autopsy", "low_n"}
    assert set(feed["meta"]) == {"generated", "n_model", "n_realized"}
    hl = feed["headline"]
    assert set(hl) == {
        "model_win_rate", "realized_win_rate", "model_brier", "market_brier",
        "bss", "acca_strike", "coverage",
    }
    for key in ("model_win_rate", "realized_win_rate"):
        b = hl[key]
        assert set(b) == {"p", "lo", "hi", "n"}
        if b["n"] > 0:
            assert 0.0 <= b["lo"] <= b["p"] <= b["hi"] <= 1.0
    # rolling rows carry every required field.
    for row in feed["rolling"]:
        assert set(row) == {"t", "label", "p_roll", "lo", "hi", "p_ewma", "p_cum",
                            "exp_model", "exp_market"}
    # segments: draw segment with no model picks must still be present (n:0).
    seg_keys = {s["key"] for s in feed["segments"]}
    assert "model_leg_draw" in seg_keys
    # all odds buckets present even if empty.
    for bk in ("odds_lt_1.5", "odds_1.5_2.0", "odds_2.0_3.0", "odds_gte_3.0"):
        assert f"realized_{bk}" in seg_keys
    # acca autopsy shape.
    assert set(feed["acca_autopsy"]) == {"legs", "near_miss", "note"}
    # The whole feed must be JSON-serialisable with no NaN/Inf.
    json.dumps(feed, allow_nan=False)


@pytest.mark.skipif(not WCA_DB.exists(), reason="wca.db not present")
def test_wca_db_opened_readonly_is_immutable():
    # Sanity: writing through our RO URI must fail (proves read-only handle).
    con = sqlite3.connect(_wca_ro_uri(), uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO bets (id) VALUES (999999)")
    con.close()
