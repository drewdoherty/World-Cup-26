"""Tests for the two-timescale opponent-adjusted goal blend (F7).

Covers:
- credibility-weight bounds + monotonicity (the blend weight math),
- per-team convex-blend math + identifiability (mean-zero re-centring),
- squad-adjustment fallback (no fabrication when no feed is supplied),
- the tracking-artifact writer (labelled tracking-only, correct columns),
- flag-OFF bit-identity at the ``fit_models`` level (existing behaviour
  unchanged unless opted in).

Synthetic DC fits keep these tests fast and self-contained — they do not read
the multi-tens-of-thousands-match production corpus.
"""

from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd
import pytest

from wca.models.dixon_coles import DixonColesModel, xi_from_half_life
from wca.models.goalblend import (
    DEFAULT_CREDIBILITY_K,
    DEFAULT_SHORT_HALF_LIFE_YEARS,
    GoalBlendConfig,
    build_goal_blend,
    credibility_weight,
    fixture_contrast_rows,
    squad_log_rate_adjustment,
    write_tracking_artifact,
)


# ---------------------------------------------------------------------------
# Synthetic played history with a clear recent-form vs long-history split.
# ---------------------------------------------------------------------------


def _synthetic_played(seed: int = 0) -> pd.DataFrame:
    """Two-era history: an OLD era and a RECENT era with different strengths.

    Teams T0..T3. In the OLD era T0 is the best attacker; in the RECENT era T3
    surges. A short half-life should pick up T3's surge more than a long one.
    """
    rng = np.random.default_rng(seed)
    teams = ["T0", "T1", "T2", "T3"]
    rows = []

    def play(date, atk_levels):
        for i, h in enumerate(teams):
            for j, a in enumerate(teams):
                if i == j:
                    continue
                lam_h = math.exp(atk_levels[h] - 0.0 + 0.1)
                lam_a = math.exp(atk_levels[a])
                rows.append(
                    dict(
                        date=date,
                        home_team=h,
                        away_team=a,
                        home_score=int(rng.poisson(lam_h)),
                        away_score=int(rng.poisson(lam_a)),
                        neutral=False,
                        tournament="Friendly",
                    )
                )

    old = {"T0": 0.6, "T1": 0.0, "T2": -0.2, "T3": -0.4}
    recent = {"T0": 0.0, "T1": 0.0, "T2": 0.0, "T3": 0.7}
    # Many old matchdays (long history dominated by OLD era).
    for d in range(8):
        play(f"201{d}-06-01", old)
    # A few recent matchdays (RECENT era).
    for d in range(3):
        play(f"2026-0{d+4}-01", recent)
    return pd.DataFrame(rows)


def _fit_long(df: pd.DataFrame) -> DixonColesModel:
    m = DixonColesModel(half_life_years=8.0)
    m.fit_dataframe(df, reference_date="2026-06-10")
    return m


# ---------------------------------------------------------------------------
# Credibility-weight math: bounds + monotonicity.
# ---------------------------------------------------------------------------


def test_credibility_weight_bounds_and_endpoints():
    assert credibility_weight(0, k=10.0) == 0.0
    w = credibility_weight(10.0, k=10.0)
    assert w == pytest.approx(0.5)
    # Always strictly in [0, 1).
    for n in [0, 1, 5, 50, 1000]:
        wv = credibility_weight(n, k=7.0)
        assert 0.0 <= wv < 1.0


def test_credibility_weight_monotonic_in_n_and_k():
    ws = [credibility_weight(n, k=10.0) for n in range(0, 40)]
    assert all(b >= a for a, b in zip(ws, ws[1:]))  # increasing in n
    # decreasing in k for fixed n
    big_k = credibility_weight(10.0, k=100.0)
    small_k = credibility_weight(10.0, k=1.0)
    assert small_k > big_k


def test_credibility_weight_rejects_bad_args():
    with pytest.raises(ValueError):
        credibility_weight(5.0, k=0.0)
    with pytest.raises(ValueError):
        credibility_weight(-1.0, k=10.0)


# ---------------------------------------------------------------------------
# Squad adjustment: HONEST fallback (no fabrication).
# ---------------------------------------------------------------------------


def test_squad_adjustment_defaults_to_no_op():
    teams = ["T0", "T1", "T2"]
    nudge = squad_log_rate_adjustment(teams)
    assert nudge == {t: 0.0 for t in teams}
    # An explicit empty mapping is also treated as "no feed".
    assert squad_log_rate_adjustment(teams, squad_strength={}) == {t: 0.0 for t in teams}


def test_squad_adjustment_recenters_a_real_feed():
    teams = ["T0", "T1", "T2"]
    feed = {"T0": 1.0, "T1": 0.0, "T2": -1.0}
    nudge = squad_log_rate_adjustment(teams, squad_strength=feed)
    # mean-zero (identifiability preserved)
    assert sum(nudge.values()) == pytest.approx(0.0)
    # ordering preserved
    assert nudge["T0"] > nudge["T1"] > nudge["T2"]


def test_build_goal_blend_does_not_adjust_squad_by_default():
    df = _synthetic_played()
    long = _fit_long(df)
    blend = build_goal_blend(long, df, reference_date="2026-06-10")
    assert blend.squad_adjusted is False


# ---------------------------------------------------------------------------
# Blend math: convexity, identifiability, monotonic in weight.
# ---------------------------------------------------------------------------


def test_blend_is_mean_zero_identifiable():
    df = _synthetic_played()
    long = _fit_long(df)
    blend = build_goal_blend(long, df, reference_date="2026-06-10")
    atk_mean = float(np.mean(list(blend.blended.attack.values())))
    dfc_mean = float(np.mean(list(blend.blended.defence.values())))
    assert atk_mean == pytest.approx(0.0, abs=1e-9)
    assert dfc_mean == pytest.approx(0.0, abs=1e-9)


def test_blend_weights_within_bounds():
    df = _synthetic_played()
    long = _fit_long(df)
    blend = build_goal_blend(long, df, reference_date="2026-06-10")
    assert blend.weights
    for w in blend.weights.values():
        assert 0.0 <= w < 1.0


def test_blend_moves_toward_short_with_smaller_k():
    """A smaller k (more credibility on recent form) pulls the blend further
    from the long-only attack — monotone shrinkage."""
    df = _synthetic_played()
    long = _fit_long(df)
    blend_big_k = build_goal_blend(
        long, df, reference_date="2026-06-10",
        config=GoalBlendConfig(credibility_k=1e9),
    )
    blend_small_k = build_goal_blend(
        long, df, reference_date="2026-06-10",
        config=GoalBlendConfig(credibility_k=0.1),
    )
    # Larger k => smaller credibility weight => blended attack closer to long
    # (after re-centring, a no-op shift on an already mean-zero long fit).
    long_atk = np.array([long.attack[t] for t in sorted(long.attack)])
    long_atk = long_atk - long_atk.mean()
    big = np.array([blend_big_k.blended.attack[t] for t in sorted(long.attack)])
    small = np.array([blend_small_k.blended.attack[t] for t in sorted(long.attack)])
    dist_big = np.linalg.norm(big - long_atk)
    dist_small = np.linalg.norm(small - long_atk)
    assert dist_small > dist_big  # smaller k pulls further toward short
    # Huge k collapses the credibility weight toward 0 => blend ~ long.
    assert max(blend_big_k.weights.values()) < 1e-3
    assert dist_big < 1e-3


def test_blend_config_rejects_bad_knobs():
    with pytest.raises(ValueError):
        GoalBlendConfig(short_half_life_years=0.0)
    with pytest.raises(ValueError):
        GoalBlendConfig(credibility_k=-1.0)


# ---------------------------------------------------------------------------
# Tracking artifact.
# ---------------------------------------------------------------------------


def test_fixture_contrast_rows_have_expected_keys():
    df = _synthetic_played()
    long = _fit_long(df)
    blend = build_goal_blend(long, df, reference_date="2026-06-10")
    rows = fixture_contrast_rows(blend, [("T0", "T3", True)], elo_ratings={"T0": 1500.0, "T3": 1600.0})
    assert len(rows) == 1
    r = rows[0]
    for key in (
        "lam_home_longDC", "lam_away_longDC", "lam_home_blend", "lam_away_blend",
        "total_longDC", "total_blend", "w_home", "w_away", "elo_home", "elo_away",
    ):
        assert key in r


def test_write_tracking_artifact_is_labelled_and_parses(tmp_path):
    df = _synthetic_played()
    long = _fit_long(df)
    blend = build_goal_blend(long, df, reference_date="2026-06-10")
    out = tmp_path / "track.csv"
    p = write_tracking_artifact(
        out, blend, [("T0", "T3", True), ("T1", "T2", False)],
        elo_ratings={"T0": 1500, "T1": 1480, "T2": 1470, "T3": 1600},
        reference_date="2026-06-10",
    )
    text = out.read_text()
    # Clearly labelled tracking-only.
    assert "TRACKING-ONLY" in text
    assert text.splitlines()[0].startswith("#")
    # Data parses past the comment lines.
    data = pd.read_csv(out, comment="#")
    assert len(data) == 2
    assert "total_blend" in data.columns
    assert p == str(out)


# ---------------------------------------------------------------------------
# Flag-OFF bit-identity at the fit_models level.
# ---------------------------------------------------------------------------


def test_fit_models_flag_off_is_default_and_blend_is_none():
    """The default ``fit_models`` call leaves ``goal_blend`` None and the long
    DC unperturbed; opting in only ADDS the blend without changing ``dc``."""
    from wca.card import fit_models

    df = _synthetic_played()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m_off = fit_models(
            df, half_life_years=8.0, reference_date="2026-06-10",
            elo_seed_from_dc_prior=False,
        )
        m_on = fit_models(
            df, half_life_years=8.0, reference_date="2026-06-10",
            elo_seed_from_dc_prior=False, goal_blend=True,
        )

    assert m_off.goal_blend is None
    assert m_on.goal_blend is not None
    # The deployed long DC must be bit-identical whether or not the blend is on.
    assert m_off.dc.mu == m_on.dc.mu
    assert m_off.dc.attack == m_on.dc.attack
    assert m_off.dc.defence == m_on.dc.defence
    assert m_off.dc.rho == m_on.dc.rho
    assert m_off.dc.home_advantage == m_on.dc.home_advantage
