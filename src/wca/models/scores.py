"""Full-time scoreline predictions reconciled to a blended 1X2.

The matchday card pipeline (:mod:`wca.card`) produces a *blended* 1X2
probability per fixture by mixing Elo, Dixon-Coles and a Shin-de-vigged market
consensus. That blend is the object we actually bet against, so any scoreline
("correct score") view we publish alongside it must imply *exactly* the same
home/draw/away probabilities — otherwise the correct-score prices would
contradict the headline 1X2 picks.

A raw Dixon-Coles score matrix ``P[h, a]`` (rows = home goals, cols = away
goals) implies its own 1X2 via the three triangular regions::

    p_home = sum_{h > a} P[h, a]   (lower triangle, excl. diagonal)
    p_draw = sum_{h = a} P[h, a]   (diagonal)
    p_away = sum_{h < a} P[h, a]   (upper triangle, excl. diagonal)

which in general differs from the blended target. :func:`reconcile_scoreline_matrix`
rescales each of the three outcome regions by a single constant so that the
region masses become the target ``(p_home, p_draw, p_away)`` while the *relative*
distribution of scorelines within each region is preserved (every home-win
scoreline keeps its share of the home-win mass, etc.). This is the
maximum-entropy / minimum-KL reconciliation under the constraint that the three
marginal outcome probabilities equal the target: the unique solution is a
piecewise-constant reweighting, one factor per region.

Degenerate regions
-------------------
If a region carries (near) zero mass in the source matrix but the target assigns
it positive probability, there is nothing to rescale. We then *reallocate* the
target mass onto that region using an independent-Poisson prior built from the
matrix's own implied goal means (so the within-region shape is still
model-driven rather than arbitrary). If even that prior is empty for the region
(it never is for a non-trivial matrix, but we guard anyway) we fall back to the
canonical minimal scorelines 1-0 / 0-0 / 0-1 for home / draw / away.

All public entry points guard against negative or NaN inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

_EPS = 1e-12

# Outcome order used throughout the card pipeline: home, draw, away.
OUTCOMES = ("home", "draw", "away")

# Over/under goal lines published on the card.
DEFAULT_OU_LINES = (1.5, 2.5, 3.5)


# ---------------------------------------------------------------------------
# Region helpers.
# ---------------------------------------------------------------------------


def _region_masks(shape: Tuple[int, int]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Boolean masks ``(home_win, draw, away_win)`` for an ``(nh, na)`` matrix.

    ``home_win`` selects cells with home goals > away goals, ``draw`` the
    diagonal, ``away_win`` the cells with away goals > home goals.
    """
    nh, na = shape
    rows = np.arange(nh)[:, None]
    cols = np.arange(na)[None, :]
    home_win = rows > cols
    draw = rows == cols
    away_win = rows < cols
    return home_win, draw, away_win


def _independent_poisson_matrix(
    lambda_home: float, lambda_away: float, shape: Tuple[int, int]
) -> np.ndarray:
    """Truncated independent-Poisson score matrix for the given means.

    Used purely as a *prior shape* when reallocating target mass into a
    degenerate (near-zero) region. Built without scipy to keep this module
    dependency-light; the Poisson pmf is evaluated by the stable recurrence
    ``p(k) = p(k-1) * lam / k`` starting from ``p(0) = exp(-lam)``.
    """
    nh, na = shape
    lam_h = max(float(lambda_home), 0.0)
    lam_a = max(float(lambda_away), 0.0)

    def _pmf(lam: float, n: int) -> np.ndarray:
        out = np.empty(n, dtype=float)
        out[0] = np.exp(-lam)
        for k in range(1, n):
            out[k] = out[k - 1] * lam / k
        return out

    ph = _pmf(lam_h, nh)
    pa = _pmf(lam_a, na)
    mat = np.outer(ph, pa)
    total = mat.sum()
    if total > 0:
        mat = mat / total
    return mat


def _canonical_region_matrix(mask: np.ndarray, which: str) -> np.ndarray:
    """All mass on the canonical minimal scoreline of a region.

    Home -> 1-0, draw -> 0-0, away -> 0-1, falling back to the first available
    cell of the region if the canonical cell is outside the matrix.
    """
    out = np.zeros(mask.shape, dtype=float)
    canonical = {"home": (1, 0), "draw": (0, 0), "away": (0, 1)}[which]
    h, a = canonical
    if h < mask.shape[0] and a < mask.shape[1] and mask[h, a]:
        out[h, a] = 1.0
        return out
    # Fallback: first cell of the region in row-major order.
    idx = np.argwhere(mask)
    if idx.size:
        out[idx[0, 0], idx[0, 1]] = 1.0
    return out


# ---------------------------------------------------------------------------
# Reconciliation.
# ---------------------------------------------------------------------------


def reconcile_scoreline_matrix(
    matrix: np.ndarray,
    target_1x2: Tuple[float, float, float],
    lambdas: Optional[Tuple[float, float]] = None,
) -> np.ndarray:
    """Rescale a score matrix so its implied 1X2 equals ``target_1x2`` exactly.

    Each of the three outcome regions (home win ``h > a``, draw ``h == a``, away
    win ``h < a``) is multiplied by a single constant ``alpha`` chosen so the
    region mass becomes the corresponding target probability::

        alpha_H = target_home / current_home
        alpha_D = target_draw / current_draw
        alpha_A = target_away / current_away

    Because the target sums to one and the rescaled regions sum exactly to the
    target components, the result sums to one by construction and its implied
    1X2 equals the target to machine precision. Within each region every
    scoreline keeps its original relative weight, so the correct-score shape of
    the source model is preserved.

    Parameters
    ----------
    matrix:
        Source score-probability matrix ``P[h, a]`` (rows home goals, cols away
        goals). Need not be exactly normalised; it is renormalised internally.
        Must be non-negative and finite.
    target_1x2:
        Target ``(p_home, p_draw, p_away)``. Must be non-negative, finite and
        sum to a positive number (it is renormalised to sum to one).
    lambdas:
        Optional ``(lambda_home, lambda_away)`` goal means used to build the
        independent-Poisson prior for reallocating mass into a degenerate
        (near-zero) region. If ``None`` the prior means are estimated from the
        matrix itself.

    Returns
    -------
    numpy.ndarray
        The reconciled matrix, same shape as ``matrix``, summing to one with
        implied 1X2 equal to the (normalised) target.

    Raises
    ------
    ValueError
        If ``matrix`` or ``target_1x2`` contains NaN/inf or negative values, or
        the target does not sum to a positive number.
    """
    mat = np.asarray(matrix, dtype=float)
    if mat.ndim != 2:
        raise ValueError("matrix must be 2-D (home goals x away goals)")
    if not np.all(np.isfinite(mat)):
        raise ValueError("matrix must contain only finite values")
    if np.any(mat < 0.0):
        raise ValueError("matrix must be non-negative")

    target = np.asarray(target_1x2, dtype=float)
    if target.shape != (3,):
        raise ValueError("target_1x2 must have exactly three components")
    if not np.all(np.isfinite(target)):
        raise ValueError("target_1x2 must contain only finite values")
    if np.any(target < 0.0):
        raise ValueError("target_1x2 must be non-negative")
    tsum = float(target.sum())
    if tsum <= _EPS:
        raise ValueError("target_1x2 must sum to a positive number")
    target = target / tsum
    t_home, t_draw, t_away = float(target[0]), float(target[1]), float(target[2])

    total = float(mat.sum())
    if total <= _EPS:
        # Wholly degenerate source: build the entire matrix from the prior.
        mat = _independent_poisson_matrix(
            *(lambdas if lambdas is not None else (1.0, 1.0)), shape=mat.shape
        )
        total = float(mat.sum())
    mat = mat / total

    home_mask, draw_mask, away_mask = _region_masks(mat.shape)
    masks = {"home": home_mask, "draw": draw_mask, "away": away_mask}
    targets = {"home": t_home, "draw": t_draw, "away": t_away}

    # Independent-Poisson prior (lazy): only built if a region is degenerate.
    prior: Optional[np.ndarray] = None
    if lambdas is not None:
        lam_h, lam_a = float(lambdas[0]), float(lambdas[1])
    else:
        rows = np.arange(mat.shape[0])
        cols = np.arange(mat.shape[1])
        lam_h = float((rows * mat.sum(axis=1)).sum())
        lam_a = float((cols * mat.sum(axis=0)).sum())

    out = np.zeros_like(mat)
    for which in OUTCOMES:
        mask = masks[which]
        tgt = targets[which]
        current = float(mat[mask].sum())
        if tgt <= _EPS:
            # Target assigns ~no mass to this region: leave it empty.
            continue
        if current > _EPS:
            # Standard case: scale the region to the target mass.
            out[mask] = mat[mask] * (tgt / current)
        else:
            # Degenerate region with positive target mass: reallocate using the
            # independent-Poisson prior shape, falling back to the canonical
            # minimal scoreline if the prior is also empty on this region.
            if prior is None:
                prior = _independent_poisson_matrix(lam_h, lam_a, mat.shape)
            region_prior = np.where(mask, prior, 0.0)
            ps = float(region_prior.sum())
            if ps > _EPS:
                out[mask] = (region_prior[mask] / ps) * tgt
            else:
                canon = _canonical_region_matrix(mask, which)
                out[mask] = canon[mask] * tgt

    # By construction out already sums to one and matches the target, but
    # renormalise defensively against floating-point drift.
    s = float(out.sum())
    if s > 0:
        out = out / s
    return out


# ---------------------------------------------------------------------------
# Derived quantities from a reconciled matrix.
# ---------------------------------------------------------------------------


def implied_1x2(matrix: np.ndarray) -> Tuple[float, float, float]:
    """Return ``(p_home, p_draw, p_away)`` implied by a score matrix."""
    mat = np.asarray(matrix, dtype=float)
    home_mask, draw_mask, away_mask = _region_masks(mat.shape)
    return (
        float(mat[home_mask].sum()),
        float(mat[draw_mask].sum()),
        float(mat[away_mask].sum()),
    )


def over_under_from_matrix(matrix: np.ndarray, line: float) -> Tuple[float, float]:
    """Return ``(p_over, p_under)`` of total goals against a goals ``line``.

    For a half-integer ``line`` (the only kind published) no push is possible,
    so ``p_over + p_under == 1`` up to floating point. Any exact-total push mass
    (integer line) is excluded from both.
    """
    mat = np.asarray(matrix, dtype=float)
    totals = np.add.outer(np.arange(mat.shape[0]), np.arange(mat.shape[1]))
    over = float(mat[totals > line].sum())
    under = float(mat[totals < line].sum())
    return over, under


def btts_from_matrix(matrix: np.ndarray) -> float:
    """Probability both teams score (BTTS = yes) from a score matrix."""
    mat = np.asarray(matrix, dtype=float)
    p_home_zero = float(mat[0, :].sum())
    p_away_zero = float(mat[:, 0].sum())
    p_both_zero = float(mat[0, 0])
    yes = 1.0 - p_home_zero - p_away_zero + p_both_zero
    return float(min(max(yes, 0.0), 1.0))


def top_scorelines_from_matrix(
    matrix: np.ndarray, k: int = 6
) -> List[Tuple[int, int, float]]:
    """Return the ``k`` most probable scorelines as ``(home, away, prob)``.

    Sorted by descending probability; ties broken by ascending ``(home, away)``
    so the ordering is deterministic.
    """
    mat = np.asarray(matrix, dtype=float)
    nh, na = mat.shape
    cells: List[Tuple[int, int, float]] = []
    for h in range(nh):
        for a in range(na):
            cells.append((h, a, float(mat[h, a])))
    cells.sort(key=lambda c: (-c[2], c[0], c[1]))
    return cells[: int(max(k, 0))]


# ---------------------------------------------------------------------------
# ScorelineCard.
# ---------------------------------------------------------------------------


def fair_odds(p: float) -> float:
    """Fair decimal odds for a probability ``p`` (``1 / p``).

    Returns ``inf`` for a non-positive probability.
    """
    p = float(p)
    if p <= 0.0:
        return float("inf")
    return 1.0 / p


def min_price(p: float, min_edge: float) -> float:
    """Minimum decimal price at which backing a ``p``-chance clears ``min_edge``.

    Backing at decimal odds ``o`` has edge ``p * o - 1``; requiring that to be
    at least ``min_edge`` gives ``o >= (1 + min_edge) / p``. Returns ``inf`` for
    a non-positive probability.
    """
    p = float(p)
    if p <= 0.0:
        return float("inf")
    return (1.0 + float(min_edge)) / p


@dataclass
class ScorelineCard:
    """Reconciled full-time correct-score view for a single fixture.

    All derived markets (top scorelines, over/under, BTTS, 1X2) are computed
    from the *reconciled* matrix, so they are mutually consistent and agree with
    the blended 1X2 the card pipeline bets against.

    Parameters
    ----------
    home, away:
        Team labels.
    matrix:
        The reconciled score-probability matrix ``P[h, a]`` (sums to one, implied
        1X2 equals ``one_x_two``).
    top_scorelines:
        ``[(home_goals, away_goals, prob), ...]`` most probable scorelines,
        descending.
    over_under:
        Maps a goals line to ``(p_over, p_under)`` for the published lines.
    btts:
        Probability both teams score.
    one_x_two:
        ``(p_home, p_draw, p_away)`` implied by the reconciled matrix.
    min_edge:
        Edge threshold used by :meth:`min_price` defaults / formatting.
    """

    home: str
    away: str
    matrix: np.ndarray
    top_scorelines: List[Tuple[int, int, float]]
    over_under: Dict[float, Tuple[float, float]]
    btts: float
    one_x_two: Tuple[float, float, float]
    min_edge: float = 0.02

    # -- price helpers ------------------------------------------------------

    @staticmethod
    def fair_odds(p: float) -> float:
        """Fair decimal odds ``1 / p`` (see module-level :func:`fair_odds`)."""
        return fair_odds(p)

    def min_price(self, p: float, min_edge: Optional[float] = None) -> float:
        """Minimum back price clearing the edge threshold (default ``self.min_edge``)."""
        me = self.min_edge if min_edge is None else min_edge
        return min_price(p, me)


def scoreline_card(
    prediction,
    blended_1x2: Tuple[float, float, float],
    home: Optional[str] = None,
    away: Optional[str] = None,
    top_k: int = 6,
    ou_lines: Tuple[float, ...] = DEFAULT_OU_LINES,
    min_edge: float = 0.02,
) -> ScorelineCard:
    """Build a :class:`ScorelineCard` from a DC prediction + blended 1X2.

    Reuses the matrix from a fitted :class:`wca.models.dixon_coles.ScorelinePrediction`
    (its ``matrix`` attribute and ``lambda_home`` / ``lambda_away`` means) and
    reconciles it to the ``blended_1x2`` produced by the card pipeline.

    Parameters
    ----------
    prediction:
        A :class:`~wca.models.dixon_coles.ScorelinePrediction` (or anything with
        a ``matrix`` attribute and optional ``lambda_home`` / ``lambda_away``
        / ``home`` / ``away``).
    blended_1x2:
        ``(p_home, p_draw, p_away)`` from the card blend (the betting target).
    home, away:
        Optional team labels; default to the prediction's own labels.
    top_k:
        Number of top scorelines to keep (default 6).
    ou_lines:
        Goal lines to compute over/under for (default 1.5 / 2.5 / 3.5).
    min_edge:
        Edge threshold stored on the card for :meth:`ScorelineCard.min_price`.
    """
    matrix = np.asarray(getattr(prediction, "matrix"), dtype=float)
    lam_h = getattr(prediction, "lambda_home", None)
    lam_a = getattr(prediction, "lambda_away", None)
    lambdas = None
    if lam_h is not None and lam_a is not None:
        lambdas = (float(lam_h), float(lam_a))

    reconciled = reconcile_scoreline_matrix(matrix, blended_1x2, lambdas=lambdas)

    h = home if home is not None else getattr(prediction, "home", "")
    a = away if away is not None else getattr(prediction, "away", "")

    ou: Dict[float, Tuple[float, float]] = {}
    for line in ou_lines:
        ou[float(line)] = over_under_from_matrix(reconciled, float(line))

    return ScorelineCard(
        home=str(h),
        away=str(a),
        matrix=reconciled,
        top_scorelines=top_scorelines_from_matrix(reconciled, k=top_k),
        over_under=ou,
        btts=btts_from_matrix(reconciled),
        one_x_two=implied_1x2(reconciled),
        min_edge=min_edge,
    )
