"""Tests for the blend-weight fitting harness (backtests/blend_fit.py).

These cover the pure-math pieces of the evidence pipeline that do not require
fitting models or hitting The Odds API:

* multiclass log-loss matches the textbook value,
* the convex Elo/DC blend is a proper, renormalised convex combination,
* the simplex softmax parameterisation maps R^2 onto the 3-simplex,
* a perfect-information blend grid finds the all-weight-on-the-better-model end,
* the WC2022 market loader de-vigs a synthetic odds snapshot with Shin and
  takes the per-book median consensus (matching wca.card.market_consensus).

Heavy paths (model fits, the LOTO driver, the live API pull) are exercised by
running ``python backtests/blend_fit.py step1`` / ``step3`` directly, not here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import numpy as np
import pytest

# blend_fit lives under backtests/ (not an installed package); import by path.
# Register in sys.modules before exec so dataclass annotation resolution
# (Python 3.9) can find the module's namespace.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BF_PATH = os.path.join(_REPO, "backtests", "blend_fit.py")
_spec = importlib.util.spec_from_file_location("blend_fit", _BF_PATH)
blend_fit = importlib.util.module_from_spec(_spec)
sys.modules["blend_fit"] = blend_fit
_spec.loader.exec_module(blend_fit)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# log_loss
# ---------------------------------------------------------------------------


def test_log_loss_perfect_is_zero():
    probs = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    y = np.array([0, 2])
    assert blend_fit.log_loss(probs, y) == pytest.approx(0.0, abs=1e-10)


def test_log_loss_uniform_is_ln3():
    probs = np.full((5, 3), 1 / 3)
    y = np.array([0, 1, 2, 1, 0])
    assert blend_fit.log_loss(probs, y) == pytest.approx(np.log(3.0), abs=1e-9)


def test_log_loss_renormalises_subunit_rows():
    # Rows in the right ratios but summing to < 1 (each entry <= 1, so the
    # internal clip(.,1.0) is a no-op) give the same loss as the normalised row.
    norm = np.array([[0.6, 0.2, 0.2]])
    raw = norm * 0.5  # sums to 0.5, all entries <= 1
    y = np.array([0])
    assert blend_fit.log_loss(raw, y) == pytest.approx(blend_fit.log_loss(norm, y))
    assert blend_fit.log_loss(norm, y) == pytest.approx(-np.log(0.6))


# ---------------------------------------------------------------------------
# blend_elo_dc
# ---------------------------------------------------------------------------


def test_blend_endpoints():
    elo = np.array([[0.7, 0.2, 0.1]])
    dc = np.array([[0.3, 0.3, 0.4]])
    assert blend_fit.blend_elo_dc(elo, dc, 1.0) == pytest.approx(elo)
    assert blend_fit.blend_elo_dc(elo, dc, 0.0) == pytest.approx(dc)


def test_blend_midpoint_and_normalised():
    elo = np.array([[0.7, 0.2, 0.1]])
    dc = np.array([[0.3, 0.3, 0.4]])
    mid = blend_fit.blend_elo_dc(elo, dc, 0.5)
    assert mid[0] == pytest.approx([0.5, 0.25, 0.25])
    assert mid.sum(axis=1)[0] == pytest.approx(1.0)


def test_blend_grid_picks_better_model_end():
    # DC is the oracle (always puts mass on the truth); Elo is wrong. The grid
    # optimum should sit at w_elo == 0 (all weight on DC).
    n = 200
    rng = np.random.default_rng(0)
    y = rng.integers(0, 3, size=n)
    dc = np.full((n, 3), 0.02)
    dc[np.arange(n), y] = 0.96
    elo = np.full((n, 3), 1 / 3)
    grid = np.round(np.arange(0.0, 1.0 + 1e-9, 0.05), 4)
    curve = [blend_fit.log_loss(blend_fit.blend_elo_dc(elo, dc, w), y) for w in grid]
    assert grid[int(np.argmin(curve))] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _softmax3 simplex parameterisation
# ---------------------------------------------------------------------------


def test_softmax3_on_simplex():
    for theta in [(0.0, 0.0), (3.0, -2.0), (-5.0, 5.0), (10.0, 10.0)]:
        w = blend_fit._softmax3(np.array(theta))
        assert w.shape == (3,)
        assert np.all(w > 0)
        assert w.sum() == pytest.approx(1.0)


def test_softmax3_equal_thirds_at_origin():
    w = blend_fit._softmax3(np.array([0.0, 0.0]))
    assert w == pytest.approx([1 / 3, 1 / 3, 1 / 3])


# ---------------------------------------------------------------------------
# realised_outcome encoding
# ---------------------------------------------------------------------------


def test_realised_outcome_encoding():
    assert blend_fit.realised_outcome(2, 1) == blend_fit.OUTCOME_HOME
    assert blend_fit.realised_outcome(1, 1) == blend_fit.OUTCOME_DRAW
    assert blend_fit.realised_outcome(0, 3) == blend_fit.OUTCOME_AWAY


# ---------------------------------------------------------------------------
# Holdout selection windows
# ---------------------------------------------------------------------------


def test_copa2024_window_excludes_march_playins():
    import pandas as pd

    copa = next(h for h in blend_fit.HOLDOUTS if h.key == "copa2024")
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-03-23", "2024-06-21", "2024-07-14"]),
            "tournament": ["Copa América", "Copa América", "Copa América"],
            "home_team": ["Canada", "Argentina", "Argentina"],
            "away_team": ["T&T", "Canada", "Colombia"],
            "home_score": [2, 2, 1],
            "away_score": [0, 0, 0],
            "neutral": [True, True, True],
        }
    )
    sel = copa.select(df)
    # The March 23 CONCACAF play-in must be excluded by the finals window.
    assert len(sel) == 2
    assert (sel["date"] >= pd.Timestamp("2024-06-20")).all()


# ---------------------------------------------------------------------------
# WC2022 market loader (Shin de-vig + median consensus)
# ---------------------------------------------------------------------------


def test_load_wc2022_market_devigs_and_consensus(tmp_path):
    from wca.markets import devig as devig_mod

    # Two books on one fixture; outcome names are team names + 'Draw'.
    odds_a = {"Brazil": 1.50, "Draw": 4.20, "Serbia": 7.00}
    odds_b = {"Brazil": 1.55, "Draw": 4.00, "Serbia": 6.50}
    blob = {
        "events": [
            {
                "id": "evt1",
                "home_team": "Brazil",
                "away_team": "Serbia",
                "bookmakers": [
                    {
                        "key": "bookA",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Brazil", "price": odds_a["Brazil"]},
                                    {"name": "Serbia", "price": odds_a["Serbia"]},
                                    {"name": "Draw", "price": odds_a["Draw"]},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "bookB",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Brazil", "price": odds_b["Brazil"]},
                                    {"name": "Serbia", "price": odds_b["Serbia"]},
                                    {"name": "Draw", "price": odds_b["Draw"]},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]
    }
    path = tmp_path / "wc2022_closing_odds.json"
    path.write_text(json.dumps(blob))

    market = blend_fit.load_wc2022_market(str(path))
    key = "Brazil|Serbia"
    assert key in market
    p = market[key]
    assert p.shape == (3,)
    assert p.sum() == pytest.approx(1.0)
    # Home (Brazil) is the heavy favourite -> highest fair probability.
    assert p[0] > p[1] and p[0] > p[2]

    # Reconstruct the expected median-consensus to confirm we match card logic.
    fair_a = devig_mod.shin([odds_a["Brazil"], odds_a["Draw"], odds_a["Serbia"]])
    fair_b = devig_mod.shin([odds_b["Brazil"], odds_b["Draw"], odds_b["Serbia"]])
    med = np.median(np.vstack([fair_a, fair_b]), axis=0)
    med = med / med.sum()
    assert p == pytest.approx(med)


def test_load_wc2022_market_skips_incomplete_books(tmp_path):
    blob = {
        "events": [
            {
                "id": "evt2",
                "home_team": "Wales",
                "away_team": "Iran",
                "bookmakers": [
                    {
                        "key": "bookOnlyTwo",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Wales", "price": 2.5},
                                    {"name": "Iran", "price": 2.9},
                                ],  # no Draw -> incomplete, skipped
                            }
                        ],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "odds.json"
    path.write_text(json.dumps(blob))
    market = blend_fit.load_wc2022_market(str(path))
    # No complete book -> fixture dropped entirely.
    assert market == {}


def test_minus_minutes():
    import importlib.util as iu

    pull_path = os.path.join(_REPO, "backtests", "wc2022_odds_pull.py")
    spec = iu.spec_from_file_location("wc2022_odds_pull", pull_path)
    mod = iu.module_from_spec(spec)
    sys.modules["wc2022_odds_pull"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert mod._minus_minutes("2022-11-24T16:00:00Z", 5) == "2022-11-24T15:55:00Z"
    # Crossing an hour boundary.
    assert mod._minus_minutes("2022-11-24T16:02:00Z", 5) == "2022-11-24T15:57:00Z"
