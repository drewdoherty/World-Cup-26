"""Smoke tests for the Dixon-Coles half-life walk-forward backtest.

These are intentionally cheap: they exercise the scoring helpers and the block
selection / aggregation plumbing on tiny synthetic inputs, plus one very small
end-to-end fit on a short training window so a single full model fit is not
required. The heavy real-data run lives in ``backtests/halflife_backtest.py`` and
is not invoked by the test suite.
"""

from __future__ import annotations

import math
import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKTESTS = os.path.join(_REPO_ROOT, "backtests")
if _BACKTESTS not in sys.path:
    sys.path.insert(0, _BACKTESTS)

import halflife_backtest as hb  # noqa: E402


def test_outcome_index():
    assert hb.outcome_index(2, 0) == 0  # home
    assert hb.outcome_index(1, 1) == 1  # draw
    assert hb.outcome_index(0, 3) == 2  # away


def test_log_loss_one_perfect_and_certain_wrong():
    # Perfect confident correct call -> ~0.
    assert hb.log_loss_one([1.0, 0.0, 0.0], 0) == pytest.approx(0.0, abs=1e-9)
    # Confident-but-wrong is heavily penalised but finite (clipped).
    assert hb.log_loss_one([0.0, 0.0, 1.0], 0) > 20.0
    # Uniform forecast == ln(3).
    assert hb.log_loss_one([1 / 3, 1 / 3, 1 / 3], 1) == pytest.approx(math.log(3.0))


def test_brier_one():
    # Perfect.
    assert hb.brier_one([1.0, 0.0, 0.0], 0) == pytest.approx(0.0)
    # Uniform vs a one-hot: 3 * (1/3 - {1,0,0})^2 summed.
    expected = (1 / 3 - 1) ** 2 + (1 / 3) ** 2 + (1 / 3) ** 2
    assert hb.brier_one([1 / 3, 1 / 3, 1 / 3], 0) == pytest.approx(expected)


def test_holdout_select_filters_window_and_tournament():
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2018-06-14", "2018-06-20", "2017-06-14", "2018-06-15"]
            ),
            "home_team": ["A", "B", "C", "D"],
            "away_team": ["X", "Y", "Z", "W"],
            "home_score": [1, 2, 0, None],  # last row unplayed -> dropped
            "away_score": [0, 2, 1, None],
            "tournament": [
                "FIFA World Cup",
                "Friendly",  # wrong tournament -> excluded
                "FIFA World Cup",  # out of window -> excluded
                "FIFA World Cup",
            ],
            "neutral": [False, True, True, True],
        }
    )
    ho = hb.Holdout("T", "2018-06-01", "2018-07-31", [("fifa world cup",)])
    sel = ho.select(df)
    # Only row 0 survives (row 3 unplayed, row 1 wrong tourn, row 2 out of window).
    assert list(sel["home_team"]) == ["A"]


def test_played_before_strict_cutoff():
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2018-05-31", "2018-06-01", "2018-06-02"]),
            "home_team": ["A", "B", "C"],
            "away_team": ["X", "Y", "Z"],
            "home_score": [1.0, 2.0, 3.0],
            "away_score": [0.0, 1.0, 2.0],
            "tournament": ["Friendly"] * 3,
            "neutral": [False, False, False],
        }
    )
    train = hb.played_before(df, pd.Timestamp("2018-06-01"))
    # Strictly before the cutoff -> only the 2018-05-31 row.
    assert list(train["home_team"]) == ["A"]


def test_aggregate_weights_by_match_count():
    b1 = hb.BlockResult("b1", 10)
    b1.dc = {2.0: {"log_loss": 1.0, "brier": 0.5}}
    b1.blend = {2.0: {"log_loss": 1.0, "brier": 0.5}}
    b2 = hb.BlockResult("b2", 30)
    b2.dc = {2.0: {"log_loss": 2.0, "brier": 0.7}}
    b2.blend = {2.0: {"log_loss": 2.0, "brier": 0.7}}
    agg = hb.aggregate([b1, b2], [2.0])
    # (10*1 + 30*2)/40 = 1.75.
    assert agg["dc"][2.0]["log_loss"] == pytest.approx(1.75)
    assert agg["dc"][2.0]["brier"] == pytest.approx((10 * 0.5 + 30 * 0.7) / 40)


def test_end_to_end_tiny_block():
    """Tiny synthetic end-to-end run with a short training window (fast fit)."""
    rows = []
    teams = ["A", "B", "C", "D"]
    # Build a small round-robin-ish history strictly before the holdout.
    import itertools

    base = pd.Timestamp("2020-01-01")
    day = 0
    for rep in range(6):
        for h, a in itertools.permutations(teams, 2):
            rows.append(
                {
                    "date": base + pd.Timedelta(days=day),
                    "home_team": h,
                    "away_team": a,
                    "home_score": (hash((h, a, rep)) % 4),
                    "away_score": (hash((a, h, rep)) % 3),
                    "tournament": "Friendly",
                    "neutral": False,
                    "country": h,
                }
            )
            day += 1
    # Holdout matches inside the window.
    for h, a in [("A", "B"), ("C", "D")]:
        rows.append(
            {
                "date": pd.Timestamp("2021-06-15"),
                "home_team": h,
                "away_team": a,
                "home_score": 2,
                "away_score": 1,
                "tournament": "FIFA World Cup",
                "neutral": False,
                "country": h,
            }
        )
    df = pd.DataFrame(rows)
    ho = hb.Holdout("tiny", "2021-06-01", "2021-07-31", [("fifa world cup",)])
    res = hb.evaluate_block(df, ho, [2.0, 8.0], verbose=False)
    assert res.n == 2
    for hl in (2.0, 8.0):
        assert math.isfinite(res.dc[hl]["log_loss"])
        assert math.isfinite(res.blend[hl]["log_loss"])
        assert res.dc[hl]["log_loss"] > 0
        # Brier in [0, 2] for a 3-class one-hot target.
        assert 0.0 <= res.dc[hl]["brier"] <= 2.0
