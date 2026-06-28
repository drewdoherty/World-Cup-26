"""Informational-edge metrics for outright / advancement / knockout markets.

CLV is unusable here (no fixed close; a single tournament's correlated outcomes
collapse the effective sample to ~1). This module provides the replacements the
benchmark needs, all pure / deterministic / dependency-light (numpy + stdlib,
scipy only for the rank test) and reusing :mod:`wca.rigor.clv` for the shared
small-sample stats:

* **convergence** — the *leading* signal: over the holding period, does the
  Polymarket price drift toward the model's number, and how much of the model's
  predicted move does the market capture? Needs only a price trajectory (from
  :mod:`wca.pmhistory`), no resolved outcomes.
* **calibration** — *lagging* ground truth: are the model's outright
  probabilities honest (reliability) and do they discriminate (AUC / Brier
  skill)? Pools many markets; needs resolved 0/1 outcomes.
* **paired_skill** — *lagging*: did the model beat the market's forecast on the
  realised outcome (Brier skill + log-loss differential)?
* **information_coefficient** — rank-correlation between model edge and outcome.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np

from wca.rigor import clv as rigor_clv

EPS = 1e-6
#: Minimum effective sample before a lagging (outcome-based) metric is "live".
MIN_RESOLVED = 30


def _clipp(p: float) -> float:
    return min(1.0 - EPS, max(EPS, float(p)))


# ---------------------------------------------------------------------------
# Leading: mark-to-market convergence (generalised CLV)
# ---------------------------------------------------------------------------


def convergence(rows: Sequence[Dict[str, float]], *, min_signal: float = 0.01) -> Dict[str, object]:
    """Did the market drift toward the model over the holding period?

    Each row needs ``entry_pm`` (PM price at entry), ``later_pm`` (a later mark)
    and ``model`` (the model probability at entry). Only markets where the model
    actually disagreed with the entry price by at least ``min_signal`` count
    (otherwise there is no edge to converge to).

    Returns ``convergence_rate`` (fraction where the market moved in the model's
    direction), ``capture_fraction`` (mean share of the model's predicted move
    the market realised, clipped to [-1, 2]), and the signal count. No outcomes
    required — this is the leading signal.
    """
    dirs, caps = [], []
    for r in rows:
        try:
            e, l, m = float(r["entry_pm"]), float(r["later_pm"]), float(r["model"])
        except (KeyError, TypeError, ValueError):
            continue
        gap = m - e
        if abs(gap) < min_signal:
            continue
        moved = l - e
        dirs.append(1.0 if (moved > 0) == (gap > 0) and moved != 0 else 0.0)
        caps.append(max(-1.0, min(2.0, moved / gap)))
    n = len(dirs)
    return {
        "n_markets": len(rows),
        "n_signal": n,
        "convergence_rate": (round(float(np.mean(dirs)), 4) if n else None),
        "capture_fraction": (round(float(np.mean(caps)), 4) if n else None),
        "sufficient": n >= MIN_RESOLVED,
        "leading": True,
    }


# ---------------------------------------------------------------------------
# Lagging: calibration + discrimination
# ---------------------------------------------------------------------------


def _auc(probs: Sequence[float], outcomes: Sequence[int]) -> Optional[float]:
    """Rank-based ROC AUC (Mann-Whitney). None if only one class present."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = p.argsort(kind="stable")
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    # average ranks for ties
    _, inv, counts = np.unique(p, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    start = csum - counts
    avg = (start + csum + 1) / 2.0
    ranks = avg[inv]
    r_pos = ranks[y == 1].sum()
    auc = (r_pos - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))
    return float(auc)


def calibration(probs: Sequence[float], outcomes: Sequence[int], *,
                cluster_ids: Optional[Sequence] = None, n_bins: int = 5) -> Dict[str, object]:
    """Reliability + discrimination of model probabilities against 0/1 outcomes.

    Returns Brier, Brier *skill* vs the base-rate forecast, AUC, a reliability
    MAE (binned |mean_p − mean_y|), and the cluster-aware effective N (so a
    handful of correlated outcomes can't masquerade as many).
    """
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    n = len(p)
    if n == 0:
        return {"n": 0, "n_eff": 0.0, "brier": None, "brier_skill": None,
                "auc": None, "reliability_mae": None, "sufficient": False, "leading": False}
    brier = float(np.mean((p - y) ** 2))
    base = float(np.mean(y))
    brier_base = float(np.mean((base - y) ** 2))
    brier_skill = (1.0 - brier / brier_base) if brier_base > 0 else None
    auc = _auc(p, y)
    # binned reliability
    bins = np.clip((p * n_bins).astype(int), 0, n_bins - 1)
    rel = []
    for b in range(n_bins):
        m = bins == b
        if m.any():
            rel.append(abs(float(p[m].mean()) - float(y[m].mean())))
    reliability_mae = float(np.mean(rel)) if rel else None
    if cluster_ids is not None:
        n_eff = rigor_clv.n_eff_clusters(list(y), list(cluster_ids))
    else:
        n_eff = float(n)
    return {
        "n": n, "n_eff": round(float(n_eff), 2),
        "brier": round(brier, 6), "brier_skill": (round(brier_skill, 6) if brier_skill is not None else None),
        "auc": (round(auc, 4) if auc is not None else None),
        "reliability_mae": (round(reliability_mae, 6) if reliability_mae is not None else None),
        "sufficient": n_eff >= MIN_RESOLVED, "leading": False,
    }


# ---------------------------------------------------------------------------
# Lagging: paired skill vs the market
# ---------------------------------------------------------------------------


def _logloss(p: float, y: float) -> float:
    pc = _clipp(p)
    return -(y * math.log(pc) + (1 - y) * math.log(1 - pc))


def paired_skill(model_probs: Sequence[float], market_probs: Sequence[float],
                 outcomes: Sequence[int], *, cluster_ids: Optional[Sequence] = None) -> Dict[str, object]:
    """Did the model beat the market's forecast on the realised outcomes?

    Brier skill = 1 − Brier(model)/Brier(market) (>0 = model better); log-loss
    differential = mean(logloss(market) − logloss(model)) (>0 = model better).
    """
    mp = np.asarray(model_probs, dtype=float)
    kp = np.asarray(market_probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    n = len(y)
    if n == 0 or not (len(mp) == len(kp) == n):
        return {"n": 0, "n_eff": 0.0, "brier_skill": None, "logloss_diff": None,
                "model_brier": None, "market_brier": None, "sufficient": False, "leading": False}
    model_brier = float(np.mean((mp - y) ** 2))
    market_brier = float(np.mean((kp - y) ** 2))
    brier_skill = (1.0 - model_brier / market_brier) if market_brier > 0 else None
    ll = [(_logloss(kp[i], y[i]) - _logloss(mp[i], y[i])) for i in range(n)]
    logloss_diff = float(np.mean(ll))
    n_eff = rigor_clv.n_eff_clusters(list(y), list(cluster_ids)) if cluster_ids is not None else float(n)
    return {
        "n": n, "n_eff": round(float(n_eff), 2),
        "model_brier": round(model_brier, 6), "market_brier": round(market_brier, 6),
        "brier_skill": (round(brier_skill, 6) if brier_skill is not None else None),
        "logloss_diff": round(logloss_diff, 6),
        "sufficient": n_eff >= MIN_RESOLVED, "leading": False,
    }


# ---------------------------------------------------------------------------
# Information Coefficient (edge vs outcome)
# ---------------------------------------------------------------------------


def information_coefficient(edges: Sequence[float], outcomes: Sequence[int]) -> Dict[str, object]:
    """Spearman rank-correlation between model edge (model − market) and outcome.

    A positive IC means larger model edges systematically precede the event
    happening — i.e. the edge carries real information.
    """
    e = np.asarray(edges, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    n = len(e)
    if n < 3 or len(np.unique(y)) < 2:
        return {"n": n, "ic": None, "p": None, "sufficient": False, "leading": False}
    try:
        from scipy.stats import spearmanr
        rho, p = spearmanr(e, y)
        return {"n": n, "ic": round(float(rho), 4), "p": round(float(p), 6),
                "sufficient": n >= MIN_RESOLVED, "leading": False}
    except Exception:  # pragma: no cover - scipy is a dependency
        return {"n": n, "ic": None, "p": None, "sufficient": False, "leading": False}
