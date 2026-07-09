"""Tests for the shrink-to-market LIVE promotion (WCA_SHRINK_LIVE).

The ``shrink`` shadow family (``p' = p_mkt + k*(p_model - p_mkt)``, ``k=0.5`` for
legs the model rates >=0.25 and ``k=0.25`` below, renormalised) was graduated
from SHADOW to the LIVE model line on 2026-07-09. These tests pin:

* the exact promoted formula (raw -> shrunk at known probabilities, including
  the stronger longshot-zone pull);
* the ``WCA_SHRINK_LIVE`` kill-switch — ON reproduces the shadow's shrink, OFF
  restores the raw blend byte-for-byte;
* the application point (:func:`wca.card._iter_fixture_blends`): ``blended`` is
  the shrunk live line, ``blended_raw`` is always the raw blend;
* the persisted feed carries BOTH ``model`` (shrunk) and ``model_raw`` (raw),
  and the ``shrink`` shadow field is recomputed from the RAW model so the
  scoreboard is never blinded by the live shrink;
* no market reference -> no shrink (blended unchanged);
* a calibration check that the promoted shrink beats the raw blend on Brier over
  the real settled sample (n stated in the assertion message).
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import pytest

from wca import modelpreds, shadowscore
from wca.card import BlendWeights, _iter_fixture_blends, fit_models

_LEGS = ("home", "draw", "away")


# ---------------------------------------------------------------------------
# 1. Exact promoted formula.
# ---------------------------------------------------------------------------


def _hand_shrink(model, market):
    """Reference implementation of the promoted transform (renormalised)."""
    shrunk = {}
    for leg in _LEGS:
        pm, pk = model[leg], market[leg]
        k = 0.5 if pm >= 0.25 else 0.25
        shrunk[leg] = pk + k * (pm - pk)
    tot = sum(shrunk.values())
    return {leg: shrunk[leg] / tot for leg in _LEGS}


def test_shrink_triple_matches_hand_formula_mid_and_longshot():
    # home 0.70 (>=0.25 -> k=0.5); draw 0.20 and away 0.10 (<0.25 -> k=0.25).
    model = {"home": 0.70, "draw": 0.20, "away": 0.10}
    market = {"home": 0.60, "draw": 0.25, "away": 0.15}
    got = modelpreds.shrink_triple(model, market)
    want = _hand_shrink(model, market)
    for leg in _LEGS:
        assert got[leg] == pytest.approx(want[leg], abs=1e-9)
    assert sum(got.values()) == pytest.approx(1.0, abs=1e-9)


def test_shrink_longshot_pull_is_weaker_toward_own_model():
    # A longshot leg (model < 0.25) is pulled only 25% of the way from market,
    # so it ends up CLOSER to market than the same gap on a >=0.25 leg would.
    model = {"home": 0.50, "draw": 0.35, "away": 0.15}
    market = {"home": 0.40, "draw": 0.30, "away": 0.30}
    got = modelpreds.shrink_triple(model, market)
    # away leg: k=0.25 -> pre-renorm 0.30 + 0.25*(0.15-0.30) = 0.2625.
    # Had it used k=0.5 it would be 0.225 (further from market). Confirm the
    # weaker pull by checking the pre-renorm value is the k=0.25 one (via ratio
    # invariance of renorm is awkward, so assert against the hand formula).
    want = _hand_shrink(model, market)
    assert got["away"] == pytest.approx(want["away"], abs=1e-9)


def test_shrink_triple_none_without_valid_triples():
    assert modelpreds.shrink_triple({"home": 0.6}, {"home": 0.6, "draw": 0.25, "away": 0.15}) is None
    assert modelpreds.shrink_triple({"home": 0.6, "draw": 0.25, "away": 0.15}, {}) is None


# ---------------------------------------------------------------------------
# 2. Kill-switch parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val,expected", [
    (None, True),      # unset -> ON (default)
    ("1", True),
    ("true", True),
    ("on", True),
    ("yes", True),
    ("anything", True),
    ("0", False),
    ("false", False),
    ("FALSE", False),
    ("no", False),
    ("off", False),
    ("", False),       # explicit empty -> OFF (treated as falsey)
])
def test_shrink_live_enabled_parsing(monkeypatch, val, expected):
    if val is None:
        monkeypatch.delenv("WCA_SHRINK_LIVE", raising=False)
    else:
        monkeypatch.setenv("WCA_SHRINK_LIVE", val)
    assert modelpreds.shrink_live_enabled() is expected


# ---------------------------------------------------------------------------
# 3. Application point: _iter_fixture_blends.
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=200):
    teams = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    base = pd.Timestamp("2022-01-01")
    rows = []
    for k in range(n):
        i, j = rng.choice(len(teams), size=2, replace=False)
        rows.append({
            "date": base + pd.Timedelta(days=int(k)),
            "home_team": teams[i], "away_team": teams[j],
            "home_score": int(rng.poisson(1.5)), "away_score": int(rng.poisson(1.1)),
            "tournament": "Friendly", "neutral": False,
        })
    return pd.DataFrame(rows)


def _synthetic_odds():
    rows = []
    fixture = dict(event_id="evt1", home_team="Alpha", away_team="Bravo",
                   commence_time="2026-06-11T18:00:00Z", market="h2h")
    book_prices = {
        "book_a": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.60},
        "book_b": {"Alpha": 2.05, "Draw": 3.30, "Bravo": 3.80},
    }
    for book, prices in book_prices.items():
        for name, odd in prices.items():
            rows.append(dict(fixture, bookmaker_key=book, outcome_name=name, decimal_odds=odd))
    return pd.DataFrame(rows)


def _synthetic_meta():
    return pd.DataFrame([{
        "home_team": "Alpha", "away_team": "Bravo", "neutral": False,
        "country": "", "home_score": np.nan, "away_score": np.nan,
    }])


@pytest.fixture
def slate():
    rng = np.random.default_rng(42)
    return (
        fit_models(_synthetic_results(rng), half_life_years=8.0),
        _synthetic_odds(),
        _synthetic_meta(),
        BlendWeights(elo=0.25, dc=0.25, market=0.50),
    )


def _blends(slate, host=("United States", "Mexico", "Canada", "USA")):
    models, odds, meta, weights = slate
    return _iter_fixture_blends(models, odds, meta, weights, host)


def test_flag_on_blended_is_shrink_of_raw(monkeypatch, slate):
    monkeypatch.setenv("WCA_SHRINK_LIVE", "1")
    (fb,) = _blends(slate)
    want = modelpreds.shrink_triple(fb.blended_raw, fb.mkt_map)
    assert want is not None
    for leg in _LEGS:
        assert fb.blended[leg] == pytest.approx(want[leg], abs=1e-12)
    # The shrink actually moved the blend toward the market (not a no-op here).
    assert any(abs(fb.blended[leg] - fb.blended_raw[leg]) > 1e-6 for leg in _LEGS)


def test_flag_off_blended_equals_raw_exactly(monkeypatch, slate):
    monkeypatch.setenv("WCA_SHRINK_LIVE", "0")
    (fb,) = _blends(slate)
    for leg in _LEGS:
        assert fb.blended[leg] == fb.blended_raw[leg]  # byte-identical, no shrink


def test_raw_blend_is_the_classic_weighted_blend(slate):
    # blended_raw must equal w.elo*elo + w.dc*dc + w.market*mkt regardless of flag.
    (fb,) = _blends(slate)
    w = BlendWeights(elo=0.25, dc=0.25, market=0.50).normalised()
    for leg in _LEGS:
        expected = w.elo * fb.elo_map[leg] + w.dc * fb.dc_map[leg] + w.market * fb.mkt_map[leg]
        assert fb.blended_raw[leg] == pytest.approx(expected, abs=1e-12)


# ---------------------------------------------------------------------------
# 4. Persisted feed: model (shrunk) + model_raw (raw), shadow from raw.
# ---------------------------------------------------------------------------


def test_feed_carries_model_and_model_raw(monkeypatch, slate):
    monkeypatch.setenv("WCA_SHRINK_LIVE", "1")
    (fb,) = _blends(slate)
    payload = modelpreds.build_predictions([fb], "2026-06-11T00:00:00")
    (row,) = payload["fixtures"]
    assert set(row["model"]) == set(_LEGS)
    assert set(row["model_raw"]) == set(_LEGS)
    # model == shrunk live line; model_raw == the raw blend.
    for leg in _LEGS:
        assert row["model"][leg] == pytest.approx(fb.blended[leg], abs=1e-6)
        assert row["model_raw"][leg] == pytest.approx(fb.blended_raw[leg], abs=1e-6)
    # They genuinely differ (the shrink moved the line).
    assert any(abs(row["model"][leg] - row["model_raw"][leg]) > 1e-6 for leg in _LEGS)


def test_shadow_shrink_field_computed_from_raw_not_shrunk_model(monkeypatch, slate):
    monkeypatch.setenv("WCA_SHRINK_LIVE", "1")
    (fb,) = _blends(slate)
    payload = modelpreds.build_predictions([fb], "2026-06-11T00:00:00")
    (row,) = payload["fixtures"]
    # The persisted `shrink` shadow must be shrink(model_raw, market) — i.e. it
    # is derived from the RAW model, NOT from the already-shrunk `model`.
    from_raw = modelpreds.shrink_triple(row["model_raw"], row["market"])
    for leg in _LEGS:
        assert row["shrink"][leg] == pytest.approx(from_raw[leg], abs=1e-6)
    # Since model is already ~shrink(model_raw), shrink(model) would differ:
    shrink_of_shrunk = modelpreds.shrink_triple(row["model"], row["market"])
    assert any(abs(row["shrink"][leg] - shrink_of_shrunk[leg]) > 1e-7 for leg in _LEGS)


def test_no_market_reference_no_shrink():
    # _iter_fixture_blends skips a fixture with no market consensus entirely, so
    # the "no market -> no shrink" contract is exercised at the transform: a
    # missing market triple returns None and the caller keeps the raw blend.
    raw = {"home": 0.70, "draw": 0.20, "away": 0.10}
    assert modelpreds.shrink_triple(raw, {}) is None
    assert modelpreds.shrink_triple(raw, {"home": 0.6}) is None


# ---------------------------------------------------------------------------
# 5. Calibration: promoted shrink beats the raw blend on Brier (real sample).
# ---------------------------------------------------------------------------

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG = os.path.join(_REPO, "data", "model_predictions_log.jsonl")
_RESULTS = os.path.join(_REPO, "data", "processed", "wc2026_results.json")


def _load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


@pytest.mark.skipif(
    not (os.path.exists(_LOG) and os.path.exists(_RESULTS)),
    reason="real prediction log / results not present",
)
def test_shrink_beats_raw_on_brier_over_settled_sample():
    """Over the real settled sample, shrink(raw, mkt) has LOWER mean Brier than
    the raw blend. The scorer's `shrink` family recomputes shrink from the raw
    model and scores it paired against `live` (= the persisted `model`, which is
    the raw blend on these pre-promotion log rows), so a negative `shrink`
    brier_diff IS the raw-vs-shrunk calibration comparison.
    """
    log_rows = _load_jsonl(_LOG)
    with open(_RESULTS, encoding="utf-8") as fh:
        results = json.load(fh).get("results", [])
    sb = shadowscore.build_scoreboard(log_rows, results, "2026-07-09T00:00:00")
    by_family = {r["family"]: r for r in sb["shadows"]}
    shrink = by_family["shrink"]
    n = shrink["n"]
    assert n >= 30, "need a usable settled sample, got n=%d" % n
    # Lower is better; diff = shrink - live(raw). Negative => shrink beats raw.
    assert shrink["brier_diff"] < 0.0, (
        "shrink did NOT beat raw on Brier over the settled sample "
        "(n=%d, brier_diff=%+.5f)" % (n, shrink["brier_diff"])
    )
