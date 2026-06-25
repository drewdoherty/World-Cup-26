"""Outcome-anchored skill gates G4 (skill vs market) and G5 (calibration).

These are the gates that distinguish a *real* edge from a best-price-only
artifact.  A bettor who only ever takes the best available price will post a
positive CLV with **zero predictive skill** — their model probabilities carry
no information beyond the market's.  G4 and G5 are computed from realized
outcomes, so they cannot be gamed by price selection.

G4 — paired per-fixture log-loss differential
----------------------------------------------
For every settled fixture we score the model's probability and the market's
de-vigged probability on the realized outcome with the log-loss (Good, 1952).
The *paired* differential ``d_i = logloss_market_i - logloss_model_i`` is
positive when the model beat the market on that fixture.  A one-sided test of
``mean(d) > 0`` (model strictly better) over the per-fixture means is the gate.
Pairing removes fixture difficulty as a confounder.

G5 — calibration (slope CI ∋ 1, intercept CI ∋ 0)
-------------------------------------------------
We fit a logistic calibration line ``logit(o) ~ a + b * logit(p_model)`` over
settled binary outcomes.  A perfectly calibrated forecaster has slope ``b = 1``
and intercept ``a = 0``.  The gate passes only when the 95% CI for the slope
contains 1 *and* the CI for the intercept contains 0 — and only with at least
100 settled outcomes (below that the fit is too noisy to trust, so the gate is
``None`` = insufficient, never a spurious pass).

Brier skill score (reported, not gated)
---------------------------------------
``BSS = 1 - Brier_model / Brier_market`` summarises model vs market in one
number: positive means the model's probabilities are sharper-and-calibrated
than the market's de-vigged ones.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

_EPS = 1e-12
N_CALIB_MIN = 100  # G5 needs >= 100 settled outcomes.


def _clip(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, float(p)))


def log_loss(p: float, outcome: float) -> float:
    """Binary log-loss for probability ``p`` and outcome in {0, 1}."""
    p = _clip(p)
    o = float(outcome)
    return -(o * math.log(p) + (1.0 - o) * math.log(1.0 - p))


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Gate G4: paired log-loss differential, model vs market.
# ---------------------------------------------------------------------------


def skill_vs_market(
    model_probs: Sequence[float],
    market_probs: Sequence[float],
    outcomes: Sequence[float],
    fixture_ids: Sequence,
) -> Dict[str, object]:
    """Gate G4: is the model's log-loss strictly lower than the market's?

    Each (model_prob, market_prob, outcome) triple is scored with log-loss; we
    average the per-leg differential ``market - model`` *within each fixture*
    (so one fixture contributes one paired observation), then run a one-sided
    test of ``mean(d) > 0`` across fixtures.  Returns the mean differential, a
    one-sided p-value (Student-t on the per-fixture means), the number of
    paired fixtures, and the pass flag.  ``pass`` is ``None`` when there are
    fewer than 3 paired fixtures (no power).
    """
    mp = np.asarray(list(model_probs), dtype=float)
    kp = np.asarray(list(market_probs), dtype=float)
    oo = np.asarray(list(outcomes), dtype=float)
    fids = list(fixture_ids)
    n = len(mp)
    if n == 0 or not (len(kp) == len(oo) == n == len(fids)):
        return {"logloss_diff": None, "p_value": None, "n_fixtures": 0, "pass": None}

    # Per-leg differential, then average within fixture.
    per_fix: Dict[object, List[float]] = {}
    for i in range(n):
        if math.isnan(mp[i]) or math.isnan(kp[i]):
            continue
        d = log_loss(kp[i], oo[i]) - log_loss(mp[i], oo[i])
        per_fix.setdefault(fids[i], []).append(d)
    fix_means = np.array([float(np.mean(v)) for v in per_fix.values()], dtype=float)
    m = len(fix_means)
    if m == 0:
        return {"logloss_diff": None, "p_value": None, "n_fixtures": 0, "pass": None}

    mean_d = float(np.mean(fix_means))
    if m < 3:
        # Report the differential but withhold a verdict: no power to test.
        return {
            "logloss_diff": mean_d,
            "p_value": None,
            "n_fixtures": m,
            "pass": None,
        }

    sd = float(np.std(fix_means, ddof=1))
    if sd <= 0:
        # All fixtures identical differential: significant iff strictly positive.
        passed = bool(mean_d > 0)
        return {
            "logloss_diff": mean_d,
            "p_value": 0.0 if passed else 1.0,
            "n_fixtures": m,
            "pass": passed,
        }
    t = mean_d / (sd / math.sqrt(m))
    p_one = _t_sf(t, m - 1)  # P(T > t): small when model clearly better.
    passed = bool(mean_d > 0 and p_one < 0.05)
    return {
        "logloss_diff": mean_d,
        "p_value": p_one,
        "n_fixtures": m,
        "pass": passed,
    }


# ---------------------------------------------------------------------------
# Gate G5: logistic calibration slope / intercept.
# ---------------------------------------------------------------------------


def calibration(
    model_probs: Sequence[float],
    outcomes: Sequence[float],
) -> Dict[str, object]:
    """Gate G5: logistic calibration ``logit(o) ~ a + b * logit(p)``.

    Fits ``a`` (intercept) and ``b`` (slope) by Newton-Raphson IRLS (no SciPy
    dependency).  Returns the point estimates, their 95% CIs, and the gate flag
    (``slope CI ∋ 1`` *and* ``intercept CI ∋ 0``).  The gate is ``None`` when
    there are fewer than ``N_CALIB_MIN`` settled outcomes, or the fit is
    degenerate (perfect separation / no outcome variation).
    """
    p = np.asarray(list(model_probs), dtype=float)
    y = np.asarray(list(outcomes), dtype=float)
    n = len(p)
    out: Dict[str, object] = {
        "slope": None, "slope_ci": None,
        "intercept": None, "intercept_ci": None,
        "n": n, "pass": None,
    }
    if n == 0:
        return out
    x = np.array([_logit(v) for v in p], dtype=float)
    # Degenerate: no outcome variation -> calibration undefined.
    if len(set(y.tolist())) < 2:
        return out

    # IRLS for logistic regression with design [1, x].
    X = np.column_stack([np.ones(n), x])
    beta = np.zeros(2)
    for _ in range(100):
        eta = X @ beta
        eta = np.clip(eta, -30, 30)
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = np.clip(mu * (1.0 - mu), 1e-9, None)
        # Weighted least squares update.
        WX = X * w[:, None]
        XtWX = X.T @ WX
        try:
            XtWX_inv = np.linalg.inv(XtWX)
        except np.linalg.LinAlgError:
            return out
        grad = X.T @ (y - mu)
        step = XtWX_inv @ grad
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break

    eta = np.clip(X @ beta, -30, 30)
    mu = 1.0 / (1.0 + np.exp(-eta))
    w = np.clip(mu * (1.0 - mu), 1e-9, None)
    try:
        cov = np.linalg.inv(X.T @ (X * w[:, None]))
    except np.linalg.LinAlgError:
        return out
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    a, b = float(beta[0]), float(beta[1])
    a_ci = (a - 1.96 * float(se[0]), a + 1.96 * float(se[0]))
    b_ci = (b - 1.96 * float(se[1]), b + 1.96 * float(se[1]))

    out.update(slope=b, slope_ci=b_ci, intercept=a, intercept_ci=a_ci)
    if n < N_CALIB_MIN:
        out["pass"] = None  # insufficient: report the fit, withhold the gate.
        return out
    slope_ok = b_ci[0] <= 1.0 <= b_ci[1]
    intc_ok = a_ci[0] <= 0.0 <= a_ci[1]
    out["pass"] = bool(slope_ok and intc_ok)
    return out


# ---------------------------------------------------------------------------
# Brier skill score (reported in skill_block, not a gate).
# ---------------------------------------------------------------------------


def brier_skill(
    model_probs: Sequence[float],
    market_probs: Sequence[float],
    outcomes: Sequence[float],
) -> Optional[float]:
    """``BSS = 1 - Brier_model / Brier_market`` over paired settled outcomes.

    Positive means the model's probabilities are sharper-and-calibrated than
    the market's de-vigged ones.  ``None`` when there is no paired sample or
    the market Brier is zero.
    """
    mp = np.asarray(list(model_probs), dtype=float)
    kp = np.asarray(list(market_probs), dtype=float)
    oo = np.asarray(list(outcomes), dtype=float)
    mask = ~(np.isnan(mp) | np.isnan(kp))
    if not mask.any():
        return None
    bm = float(np.mean((mp[mask] - oo[mask]) ** 2))
    bk = float(np.mean((kp[mask] - oo[mask]) ** 2))
    if bk <= 0:
        return None
    return 1.0 - bm / bk


# ---------------------------------------------------------------------------
# Student-t survival function (stdlib-only, no SciPy).
# ---------------------------------------------------------------------------


def _t_sf(t: float, df: int) -> float:
    """One-sided upper-tail P(T > t) for Student-t with ``df`` dof.

    Uses the regularised incomplete beta via a continued fraction.  Accurate
    enough for gate decisions; deterministic and dependency-free.
    """
    if df <= 0:
        return float("nan")
    if t == 0:
        return 0.5
    x = df / (df + t * t)
    ib = _betai(df / 2.0, 0.5, x)  # = I_x(df/2, 1/2)
    tail = 0.5 * ib
    return tail if t > 0 else 1.0 - tail


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta) / a
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x)
    return 1.0 - front * _betacf(b, a, 1.0 - x) * (a / b) if False else (
        1.0 - _betai(b, a, 1.0 - x)
    )


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT = 300
    EPS = 3.0e-12
    FPMIN = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h
