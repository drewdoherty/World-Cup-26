"""Convert bookmaker decimal odds into fair (de-vigged) probabilities.

A bookmaker's quoted decimal odds embed a profit margin (the *overround*,
*vigorish* or *vig*): the raw implied probabilities ``1 / odds`` sum to more
than one. To recover a fair probability estimate for each outcome the margin
must be removed. This module implements three standard de-vigging schemes,
each defined for any ``n``-way market (1X2 three-way, two-way money lines,
correct-score multi-way books, ...):

* **Multiplicative / basic normalization.** Divide each raw implied
  probability by the booksum. This is the maximum-likelihood estimator under
  the assumption that the bookmaker applies a constant *proportional* margin to
  every outcome. It is the de-facto industry default. See e.g. Wikipedia
  "Mathematics of bookmaking", and Clarke, Kovalchik & Ingram (2017),
  "Adjusting bookmaker's odds to allow for overround", *American Journal of
  Sports Science* 5(6):45-49.

* **Power / odds-ratio method.** Find an exponent ``k`` such that the
  normalized probabilities ``p_i = pi_i ** k`` sum to one, where ``pi_i`` are
  the raw implied probabilities. ``k`` is found by a one-dimensional root
  solve (``scipy.optimize.brentq``). The power method shifts margin away from
  short-priced favourites and onto longshots relative to the multiplicative
  method. See Clarke, Kovalchik & Ingram (2017).

* **Shin (1993) method.** Models the overround as arising from a proportion
  ``z`` of insider (informed) money. The fair probabilities solve

  .. math::

      p_i = \\frac{\\sqrt{z^2 + 4 (1 - z)\\, \\pi_i^2 / \\Pi} - z}{2 (1 - z)}

  where ``pi_i`` are the raw implied probabilities and ``Pi = sum_j pi_j`` is
  the booksum. ``z`` is the unique value in ``[0, 1)`` for which the ``p_i``
  sum to one. This is the closed-form-per-outcome formulation given by
  Štrumbelj (2014), "On determining probability forecasts from betting odds",
  *International Journal of Forecasting* 30(4):934-943, which itself follows
  Shin (1993), "Measuring the incidence of insider trading in a market for
  state-contingent claims", *The Economic Journal* 103(420):1141-1153.
  Because Shin attributes part of the margin to informed trading, it removes
  *more* probability from longshots than the multiplicative method, i.e. it
  assigns lower fair probabilities to long-priced outcomes. This favourite /
  longshot correction is the method's defining empirical property.

All three estimators are exact (return the input probabilities unchanged) on a
fair book whose raw implied probabilities already sum to one.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

import numpy as np

try:  # pandas is a hard dependency of the package; import defensively for typing
    import pandas as pd
except Exception:  # pragma: no cover - pandas is always present at runtime
    pd = None  # type: ignore


_EPS = 1e-12

#: Method names recognised by :func:`devig` / :func:`compare_methods`.
METHODS = ("multiplicative", "power", "shin")


def _as_odds_array(decimal_odds: Sequence[float]) -> np.ndarray:
    """Validate and coerce a sequence of decimal odds to a float array."""
    odds = np.asarray(decimal_odds, dtype=float).ravel()
    if odds.size < 2:
        raise ValueError("a market needs at least two outcomes")
    if not np.all(np.isfinite(odds)):
        raise ValueError("decimal odds must be finite")
    if np.any(odds <= 1.0):
        # Decimal odds of 1.0 imply certainty (zero payout above stake); odds
        # at or below 1.0 are not valid quotes.
        raise ValueError("decimal odds must be strictly greater than 1.0")
    return odds


def implied_probs(decimal_odds: Sequence[float]) -> np.ndarray:
    """Raw implied probabilities ``1 / odds`` (these sum to ``1 + overround``).

    Parameters
    ----------
    decimal_odds:
        Decimal (European) odds, one per outcome. Each must exceed 1.0.

    Returns
    -------
    numpy.ndarray
        Array ``pi_i = 1 / odds_i``. The sum is the booksum, which equals
        ``1 + overround`` for a margined book and exactly ``1`` for a fair one.
    """
    odds = _as_odds_array(decimal_odds)
    return 1.0 / odds


def booksum(decimal_odds: Sequence[float]) -> float:
    """Sum of raw implied probabilities (a.k.a. the *book*, ``1 + overround``)."""
    return float(np.sum(implied_probs(decimal_odds)))


def overround(decimal_odds: Sequence[float]) -> float:
    """Bookmaker overround / margin ``sum(1 / odds) - 1``.

    A fair book has overround ``0``; a typical 1X2 football book is around
    ``0.05``-``0.08`` (5-8%).
    """
    return booksum(decimal_odds) - 1.0


def margin(decimal_odds: Sequence[float]) -> float:
    """Alias for :func:`overround` (the bookmaker's margin)."""
    return overround(decimal_odds)


# ---------------------------------------------------------------------------
# De-vig methods.
# ---------------------------------------------------------------------------


def multiplicative(decimal_odds: Sequence[float]) -> np.ndarray:
    """Basic-normalization (multiplicative) de-vig.

    Divides each raw implied probability by the booksum so the result sums to
    one. Assumes a constant proportional margin across outcomes.
    """
    pi = implied_probs(decimal_odds)
    return pi / pi.sum()


def power(decimal_odds: Sequence[float]) -> np.ndarray:
    """Power (odds-ratio) de-vig: solve ``sum(pi_i ** k) == 1`` for ``k``.

    The exponent ``k`` is found with ``scipy.optimize.brentq``. For a margined
    book ``k > 1`` (it deflates every raw probability, deflating longshots
    proportionally more than favourites); for a fair book ``k == 1`` and the
    raw probabilities are returned unchanged.
    """
    from scipy.optimize import brentq

    pi = implied_probs(decimal_odds)
    total = pi.sum()
    # Already fair: the unique exponent is 1, return the inputs unchanged.
    if abs(total - 1.0) <= _EPS:
        return pi.copy()

    log_pi = np.log(pi)

    def f(k: float) -> float:
        return float(np.sum(np.exp(k * log_pi)) - 1.0)

    # f is continuous and strictly decreasing in k (each pi_i < 1 so pi_i**k
    # decreases with k). At k=1 the sum is the booksum (> 1 for a margined
    # book). We bracket a root k > 1 by growing the upper bound until f < 0.
    lo, hi = 1.0, 2.0
    f_lo = f(lo)
    if f_lo < 0.0:
        # Booksum below one (a fair/under-round book with all odds < their
        # fair value): search downward for k < 1 instead.
        hi = lo
        lo = 0.5
        while f(lo) < 0.0:
            lo *= 0.5
            if lo < 1e-6:
                raise ValueError("failed to bracket power-method exponent")
    else:
        while f(hi) > 0.0:
            hi *= 2.0
            if hi > 1e6:
                raise ValueError("failed to bracket power-method exponent")

    k = brentq(f, lo, hi, xtol=1e-12, rtol=1e-14, maxiter=200)
    p = np.exp(k * log_pi)
    # Renormalize to wash out any residual root-finding error.
    return p / p.sum()


def shin(decimal_odds: Sequence[float], max_iter: int = 100, tol: float = 1e-12) -> np.ndarray:
    """Shin (1993) de-vig, in the Štrumbelj (2014) per-outcome closed form.

    Solves for the insider-trading proportion ``z`` in ``[0, 1)`` such that the
    fair probabilities

    ``p_i = (sqrt(z**2 + 4 (1 - z) pi_i**2 / Pi) - z) / (2 (1 - z))``

    sum to one, where ``pi_i`` are the raw implied probabilities and ``Pi`` is
    their sum (the booksum). The function ``S(z) = sum_i p_i(z)`` is monotone in
    ``z`` over ``[0, 1)``, so ``z`` is found by bisection. On a fair book the
    margin is zero, ``z = 0`` and the raw probabilities are returned.

    Parameters
    ----------
    decimal_odds:
        Decimal odds per outcome.
    max_iter:
        Maximum bisection iterations.
    tol:
        Absolute tolerance on ``sum(p_i) - 1`` and on the ``z`` bracket width.

    Returns
    -------
    numpy.ndarray
        Fair probabilities summing to one. Longshots receive *lower*
        probability than under the multiplicative method (the favourite /
        longshot bias correction).
    """
    pi = implied_probs(decimal_odds)
    total = float(pi.sum())

    # Fair book: no margin, z == 0 and p_i == pi_i exactly.
    if abs(total - 1.0) <= _EPS:
        return pi.copy()

    pi2_over_pi = (pi * pi) / total

    def probs_for_z(z: float) -> np.ndarray:
        # Guard the (1 - z) denominator as z -> 1.
        denom = 2.0 * (1.0 - z)
        root = np.sqrt(z * z + 4.0 * (1.0 - z) * pi2_over_pi)
        return (root - z) / denom

    def sum_for_z(z: float) -> float:
        return float(np.sum(probs_for_z(z)))

    # S(0) == booksum (> 1 for a margined book). As z -> 1, S(z) -> 1 from
    # above for an over-round book, so the root z lies in [0, 1). Bisection.
    lo, hi = 0.0, 1.0 - 1e-12
    s_lo = sum_for_z(lo)
    if s_lo <= 1.0 + tol:
        # S(z) is monotone *decreasing* over [0, 1), so when even S(0) is at or
        # below one (a fair or under-round book) no z > 0 can lift the sum to
        # one. Fall back to z = 0 and normalize. For a genuinely fair book
        # (booksum == 1) this returns the raw probabilities unchanged.
        p0 = probs_for_z(0.0)
        return p0 / p0.sum()

    z = 0.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        s_mid = sum_for_z(mid)
        if abs(s_mid - 1.0) <= tol or (hi - lo) <= tol:
            z = mid
            break
        # S(z) is decreasing in z towards 1, so if the sum is still above one
        # we must increase z.
        if s_mid > 1.0:
            lo = mid
        else:
            hi = mid
        z = mid

    p = probs_for_z(z)
    # Final renormalization to absorb residual bisection error.
    return p / p.sum()


def shin_z(decimal_odds: Sequence[float], max_iter: int = 100, tol: float = 1e-12) -> float:
    """Return just the fitted Shin insider-trading proportion ``z`` in ``[0, 1)``.

    A convenience for diagnostics: larger ``z`` means the book attributes more
    of its margin to informed money (a stronger longshot correction).
    """
    pi = implied_probs(decimal_odds)
    total = float(pi.sum())
    if abs(total - 1.0) <= _EPS:
        return 0.0
    pi2_over_pi = (pi * pi) / total

    def sum_for_z(z: float) -> float:
        denom = 2.0 * (1.0 - z)
        root = np.sqrt(z * z + 4.0 * (1.0 - z) * pi2_over_pi)
        return float(np.sum((root - z) / denom))

    lo, hi = 0.0, 1.0 - 1e-12
    if sum_for_z(lo) <= 1.0 + tol:
        return 0.0
    z = 0.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        s_mid = sum_for_z(mid)
        if abs(s_mid - 1.0) <= tol or (hi - lo) <= tol:
            return mid
        if s_mid > 1.0:
            lo = mid
        else:
            hi = mid
        z = mid
    return z


_METHOD_FUNCS = {
    "multiplicative": multiplicative,
    "power": power,
    "shin": shin,
}


def devig(decimal_odds: Sequence[float], method: str = "multiplicative") -> np.ndarray:
    """Dispatch to a named de-vig method.

    Parameters
    ----------
    decimal_odds:
        Decimal odds per outcome.
    method:
        One of :data:`METHODS` (``"multiplicative"``, ``"power"``, ``"shin"``).
    """
    key = str(method).strip().lower()
    if key not in _METHOD_FUNCS:
        raise ValueError(
            "unknown de-vig method %r; choose from %s" % (method, ", ".join(METHODS))
        )
    return _METHOD_FUNCS[key](decimal_odds)


# ---------------------------------------------------------------------------
# Helpers around fair odds and tabular comparison.
# ---------------------------------------------------------------------------


def fair_odds(probs: Sequence[float]) -> np.ndarray:
    """Decimal odds implied by a probability vector: ``1 / p_i``.

    The inverse of :func:`implied_probs` for a normalized (fair) probability
    vector. Probabilities must be strictly positive.
    """
    p = np.asarray(probs, dtype=float).ravel()
    if np.any(p <= 0.0):
        raise ValueError("probabilities must be strictly positive to invert to odds")
    if not np.all(np.isfinite(p)):
        raise ValueError("probabilities must be finite")
    return 1.0 / p


def compare_methods(
    decimal_odds: Sequence[float],
    labels: Sequence[str] = None,
) -> "pd.DataFrame":
    """Tabulate de-vigged probabilities from every method for one market.

    Parameters
    ----------
    decimal_odds:
        Decimal odds per outcome.
    labels:
        Optional outcome labels (e.g. ``["Home", "Draw", "Away"]``) used as the
        DataFrame columns. Defaults to ``outcome_0``, ``outcome_1``, ...

    Returns
    -------
    pandas.DataFrame
        Index is the method name plus an ``implied`` row of the raw (margined)
        probabilities; columns are the outcomes. A trailing ``sum`` column is
        included for inspection (``1`` for de-vigged rows, the booksum for the
        ``implied`` row).
    """
    if pd is None:  # pragma: no cover
        raise RuntimeError("pandas is required for compare_methods")

    odds = _as_odds_array(decimal_odds)
    n = odds.size
    if labels is None:
        cols = ["outcome_%d" % i for i in range(n)]
    else:
        cols = list(labels)
        if len(cols) != n:
            raise ValueError("labels length must match number of outcomes")

    rows: Dict[str, np.ndarray] = {"implied": implied_probs(odds)}
    for name in METHODS:
        rows[name] = _METHOD_FUNCS[name](odds)

    data: List[List[float]] = []
    index: List[str] = []
    for name, vec in rows.items():
        index.append(name)
        data.append(list(vec))

    frame = pd.DataFrame(data, index=index, columns=cols)
    frame["sum"] = frame[cols].sum(axis=1)
    return frame
