"""Scoring metrics for the benchmark harness.

Compact, dependency-light implementations of the calibration / discrimination
metrics. They mirror the canonical helpers elsewhere in the codebase
(``wca.tracking.brier_1x2`` / ``log_loss_1x2``, ``wca.winrate.wilson``,
``wca.clvbench.trimmed_mean``) but are kept local so the benchmark package is
self-contained and stable against concurrent edits to those modules.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

_OUTCOME_KEY = {"H": "home", "D": "draw", "A": "away"}


def brier_1x2(probs: Dict[str, float], outcome: str) -> Optional[float]:
    """Multiclass Brier: sum_k (p_k - 1{outcome=k})^2 over {home,draw,away}."""
    if outcome not in _OUTCOME_KEY:
        return None
    win = _OUTCOME_KEY[outcome]
    total = 0.0
    for k in ("home", "draw", "away"):
        p = probs.get(k)
        if p is None:
            return None
        total += (p - (1.0 if k == win else 0.0)) ** 2
    return total


def log_loss_1x2(probs: Dict[str, float], outcome: str, eps: float = 1e-12) -> Optional[float]:
    if outcome not in _OUTCOME_KEY:
        return None
    p = probs.get(_OUTCOME_KEY[outcome])
    if p is None:
        return None
    return -math.log(min(max(p, eps), 1.0))


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float, float]:
    """Wilson score interval for a binomial rate; returns (p_hat, lo, hi)."""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def trimmed_mean(xs: Sequence[float], frac: float = 0.10) -> Optional[float]:
    vals = sorted(float(x) for x in xs if x is not None and not math.isnan(x))
    if not vals:
        return None
    k = int(len(vals) * frac)
    core = vals[k: len(vals) - k] if len(vals) - 2 * k > 0 else vals
    return sum(core) / len(core)


def mean(xs: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return sum(vals) / len(vals) if vals else None


def median(xs: Sequence[float]) -> Optional[float]:
    vals = sorted(float(x) for x in xs if x is not None and not math.isnan(x))
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def reliability_bins(pairs: Sequence[Tuple[float, int]], n_bins: int = 5
                     ) -> List[Dict[str, float]]:
    """Reliability table for (predicted_prob, hit in {0,1}) pairs.

    Equal-width bins on [0,1]. Each bin reports mean predicted prob, realized
    frequency, count, and a Wilson 95% CI on the realized frequency.
    """
    bins: List[List[Tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, hit in pairs:
        if p is None:
            continue
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((float(p), int(hit)))
    out = []
    for i, b in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if not b:
            out.append({"bin_lo": lo, "bin_hi": hi, "count": 0,
                        "mean_pred": None, "freq_pos": None, "ci_lo": None, "ci_hi": None})
            continue
        ps = [p for p, _ in b]
        k = sum(h for _, h in b)
        _, clo, chi = wilson(k, len(b))
        out.append({"bin_lo": lo, "bin_hi": hi, "count": len(b),
                    "mean_pred": sum(ps) / len(ps), "freq_pos": k / len(b),
                    "ci_lo": clo, "ci_hi": chi})
    return out


def ece(pairs: Sequence[Tuple[float, int]], n_bins: int = 10) -> Optional[float]:
    """Expected Calibration Error: sum_b (n_b/N) * |freq_b - mean_pred_b|."""
    rows = reliability_bins(pairs, n_bins)
    n = sum(r["count"] for r in rows)
    if n == 0:
        return None
    total = 0.0
    for r in rows:
        if r["count"] == 0:
            continue
        total += (r["count"] / n) * abs(r["freq_pos"] - r["mean_pred"])
    return total


def roi(staked: float, pl: float) -> Optional[float]:
    return (pl / staked) if staked > 0 else None
