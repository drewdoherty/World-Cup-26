"""CLV gates G0-G3 plus the shared small-sample statistics used everywhere.

Everything here is dependency-light (``numpy`` + stdlib) and deterministic: no
wall-clock, no network.  Randomness (the cluster bootstrap, the placebo null)
is seeded so the same inputs always yield the same numbers.

Statistics implemented
----------------------
``wilson``
    Wilson (1927) score interval for a binomial proportion.  Correct at the
    boundaries ``k = 0``, ``k = n`` and degenerate ``n in {0, 1}``.

``n_eff_clusters``
    Cluster-bootstrap-implied effective sample size.  When observations
    cluster (legs in a fixture, legs in an acca, re-predictions of one match),
    the naive row count overstates information.  We resample *clusters* with
    replacement, recompute the mean each time, and back out the effective N
    from the inflation of the bootstrap variance relative to the i.i.d.
    variance: ``n_eff = n * Var_iid / Var_cluster`` (design-effect deflation).

``sequential_clv_significant``  (gate G2)
    An always-valid-ish significance check that does *not* use a fixed 1.65 /
    1.96 critical value.  We use a conservative finite-sample boundary
    (z grows with a ``log`` penalty in n_eff, an anytime-valid flavour) so that
    "significant" survives the multiple looks an online ledger implies.

``placebo_beat_rate``  (gate G3)
    The null beat-rate a *price-taker with no edge* would post.  We resample
    the sign of CLV under the null (shuffle which side of the close we landed
    on within clusters) and take the 95th percentile of the placebo beat-rate
    distribution.  A real edge must clear that bar, not merely 0.5.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Cost-adjusted ROPE floor for CLV (gate G1): a region of practical
# equivalence to "no edge".  A fair-vs-fair CLV below this is indistinguishable
# from execution noise / rounding once a realistic cost of acting is netted
# out, so the CLV lower bound must clear it (not merely clear 0).
CLV_ROPE_FLOOR = 0.005  # 0.5 percentage points of fair-odds value.

# Power thresholds for the gate battery.
N_EFF_CLV_MIN = 25.0   # G0 for CLV-style gates.
N_EFF_ROI_MIN = 100.0  # G0 for ROI-style gates.

_DEFAULT_SEED = 20260625


# ---------------------------------------------------------------------------
# Wilson score interval.
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float, float]:
    """Wilson 95% score interval for ``k`` successes in ``n`` trials.

    Returns ``(p_hat, lo, hi)`` where ``p_hat = k / n`` and ``[lo, hi]`` is the
    Wilson interval.  Boundaries are handled explicitly:

    * ``n == 0`` -> ``(nan, 0.0, 1.0)`` (no information; widest honest band).
    * ``n == 1`` -> centre still defined; interval is the full Wilson width.
    * ``k == 0`` / ``k == n`` -> lower / upper bound is pinned but the interval
      is *not* degenerate (the score interval keeps a non-zero width, unlike
      the Wald interval which collapses to a point).
    """
    if n <= 0:
        return (float("nan"), 0.0, 1.0)
    p = k / n
    z2 = z * z
    d = 1.0 + z2 / n
    c = (p + z2 / (2.0 * n)) / d
    h = (z / d) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    lo = max(0.0, c - h)
    hi = min(1.0, c + h)
    return (p, lo, hi)


def wilson_lower(k: int, n: int, z: float = 1.96) -> Optional[float]:
    """Just the Wilson lower bound, or ``None`` when ``n == 0``."""
    if n <= 0:
        return None
    return wilson(k, n, z)[1]


# ---------------------------------------------------------------------------
# Cluster-aware effective sample size.
# ---------------------------------------------------------------------------


def n_eff_clusters(
    values: Sequence[float],
    cluster_ids: Sequence,
    *,
    n_boot: int = 2000,
    seed: int = _DEFAULT_SEED,
) -> float:
    """Cluster-bootstrap-implied effective sample size for a mean.

    ``values`` are per-observation; ``cluster_ids`` assigns each observation to
    a cluster (a fixture, an acca, a re-predicted match).  Observations in the
    same cluster are correlated, so the information content is closer to the
    number of *clusters* than the number of rows.

    We estimate the design effect by bootstrapping over clusters: resample
    clusters with replacement, recompute the grand mean, and compare the
    bootstrap variance of that mean to the variance an i.i.d. sample of the
    same row-count would have.  ``n_eff = n_rows * Var_iid / Var_cluster``,
    clamped to ``[1, n_clusters]`` for a single-cluster / degenerate input.

    A single cluster (e.g. a futures market: one tournament, one resolution)
    therefore yields ``n_eff <= 1`` — permanently insufficient by construction.
    """
    vals = np.asarray(list(values), dtype=float)
    n = len(vals)
    if n == 0:
        return 0.0
    ids = list(cluster_ids)
    # Map cluster id -> indices.
    groups: Dict[object, List[int]] = {}
    for i, cid in enumerate(ids):
        groups.setdefault(cid, []).append(i)
    cluster_keys = list(groups.keys())
    n_clusters = len(cluster_keys)
    if n_clusters <= 1:
        # All correlated: one independent observation at most.
        return 1.0 if n >= 1 else 0.0
    if n == 1:
        return 1.0

    grand_mean = float(np.mean(vals))
    # i.i.d. variance of the mean of n rows.
    var_pop = float(np.var(vals))  # population variance
    if var_pop == 0.0:
        # No spread: every observation identical -> mean is known; the
        # cluster structure cannot inflate variance.  Effective N = n_clusters
        # (the honest count of independent draws of a constant is the cluster
        # count, never the inflated row count).
        return float(n_clusters)
    var_iid_mean = var_pop / n

    rng = np.random.default_rng(seed)
    cluster_arrays = [vals[np.asarray(groups[k])] for k in cluster_keys]
    boot_means = np.empty(n_boot, dtype=float)
    idx_space = np.arange(n_clusters)
    for b in range(n_boot):
        pick = rng.choice(idx_space, size=n_clusters, replace=True)
        chunks = [cluster_arrays[j] for j in pick]
        boot_means[b] = float(np.mean(np.concatenate(chunks)))
    var_cluster_mean = float(np.var(boot_means))
    if var_cluster_mean <= 0.0:
        return float(n_clusters)
    deff = var_cluster_mean / var_iid_mean  # design effect (>= ~1)
    n_eff = n / deff if deff > 0 else float(n_clusters)
    # An honest n_eff can never exceed the number of independent clusters, nor
    # drop below 1 for a non-empty sample.
    n_eff = max(1.0, min(float(n_clusters), float(n_eff)))
    return n_eff


# ---------------------------------------------------------------------------
# Gate G2: always-valid-ish sequential significance for mean CLV.
# ---------------------------------------------------------------------------


def sequential_z_threshold(n_eff: float) -> float:
    """Anytime-valid-flavoured critical z that *grows* with the sample.

    A fixed 1.65 / 1.96 cut-off is invalid for a ledger that is inspected after
    every settled bet (the classic optional-stopping inflation).  We use a
    boundary of the form ``z(n) = sqrt(2 * log(log(e^2 * n) / alpha))`` (a
    finite-LIL / mixture-boundary shape, Robbins-style) which starts well above
    1.96 and creeps upward, so significance declared under it is robust to the
    many looks an online ledger implies.
    """
    alpha = 0.05
    n = max(2.0, float(n_eff))
    inner = math.log(math.exp(2.0) * n) / alpha
    return math.sqrt(2.0 * math.log(max(inner, math.e)))


def sequential_clv_significant(
    mean_clv: Optional[float],
    sd_clv: Optional[float],
    n_eff: float,
) -> Tuple[Optional[bool], Optional[float], float]:
    """Gate G2: is mean CLV positive *and* sequentially significant?

    Returns ``(passed, z_observed, z_threshold)``.  ``passed`` is ``None`` when
    there is no usable CLV sample.  A negative or zero mean never passes.
    """
    z_thr = sequential_z_threshold(n_eff)
    if mean_clv is None or sd_clv is None or n_eff < 2 or sd_clv <= 0:
        return (None, None, z_thr)
    se = sd_clv / math.sqrt(n_eff)
    if se <= 0:
        return (None, None, z_thr)
    z_obs = mean_clv / se
    return (bool(z_obs > z_thr and mean_clv > 0), z_obs, z_thr)


# ---------------------------------------------------------------------------
# Gate G3: placebo beat-rate (95th percentile of the no-edge null).
# ---------------------------------------------------------------------------


def placebo_beat_rate(
    clv_values: Sequence[float],
    cluster_ids: Sequence,
    *,
    n_boot: int = 3000,
    seed: int = _DEFAULT_SEED,
) -> Optional[float]:
    """95th-percentile beat-rate a no-edge price-taker would post.

    Under the null of no edge the sign of each CLV is symmetric noise.  We flip
    the sign of whole clusters at random (preserving within-cluster
    correlation) and record the fraction with positive CLV.  The 95th
    percentile of that distribution is the placebo bar the *observed* beat-rate
    must clear in gate G3.  ``None`` for an empty sample.
    """
    vals = np.asarray(list(clv_values), dtype=float)
    if len(vals) == 0:
        return None
    ids = list(cluster_ids)
    groups: Dict[object, List[int]] = {}
    for i, cid in enumerate(ids):
        groups.setdefault(cid, []).append(i)
    keys = list(groups.keys())
    cluster_idx = [np.asarray(groups[k]) for k in keys]
    rng = np.random.default_rng(seed + 7)
    n = len(vals)
    rates = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        flipped = vals.copy()
        signs = rng.choice([-1.0, 1.0], size=len(keys))
        for s, idx in zip(signs, cluster_idx):
            flipped[idx] = vals[idx] * s
        rates[b] = float(np.mean(flipped > 0))
    return float(np.percentile(rates, 95))


# ---------------------------------------------------------------------------
# CLV block assembly (drives G0-G3).
# ---------------------------------------------------------------------------


def clv_block(
    clv_values: Sequence[float],
    cluster_ids: Sequence,
    *,
    seed: int = _DEFAULT_SEED,
) -> Dict[str, object]:
    """Compute the CLV summary block and the G0-G3 gate inputs.

    ``clv_values`` must already exclude pushes / voids and any bet without a
    captured close (CLV is NULL there, not 0).  Returns a dict with the
    summary numbers plus a ``gates`` sub-dict carrying the raw pass/fail and
    statistic for G0(CLV), G1, G2, G3.
    """
    vals = np.asarray(list(clv_values), dtype=float)
    n = int(len(vals))
    if n == 0:
        return {
            "mean": None,
            "lower": None,
            "beat_rate": None,
            "placebo_null": None,
            "n_eff": 0.0,
            "gates": {
                "G0": {"value": 0.0, "pass": False},
                "G1": {"value": None, "pass": None},
                "G2": {"value": None, "pass": None, "threshold": None},
                "G3": {"value": None, "pass": None, "placebo": None},
            },
        }

    n_eff = n_eff_clusters(vals, cluster_ids, seed=seed)
    mean_clv = float(np.mean(vals))
    sd_clv = float(np.std(vals, ddof=1)) if n > 1 else 0.0

    # CLV lower bound: a normal-approx lower confidence bound on the mean using
    # the effective (de-correlated) sample size, not the inflated row count.
    if n_eff >= 2 and sd_clv > 0:
        se = sd_clv / math.sqrt(n_eff)
        clv_lower = mean_clv - 1.96 * se
    else:
        clv_lower = None

    # Beat rate excludes exact zeros (a CLV of exactly 0 is a tie, not a beat).
    k_beat = int(np.sum(vals > 0))
    n_nonzero = int(np.sum(vals != 0))
    beat_rate = (k_beat / n_nonzero) if n_nonzero > 0 else None
    _, beat_lo, _ = wilson(k_beat, n_nonzero) if n_nonzero > 0 else (None, None, None)
    placebo = placebo_beat_rate(vals, cluster_ids, seed=seed)

    # G0: enough effective CLV sample.
    g0_pass = bool(n_eff >= N_EFF_CLV_MIN)
    # G1: CLV lower bound clears the cost-adjusted ROPE floor (not just 0).
    g1_pass = bool(clv_lower is not None and clv_lower > CLV_ROPE_FLOOR)
    # G2: sequential significance.
    g2_pass, z_obs, z_thr = sequential_clv_significant(mean_clv, sd_clv, n_eff)
    # G3: Wilson lower bound on beat-rate clears the placebo 95th percentile.
    if beat_lo is None or placebo is None:
        g3_pass = None
    else:
        g3_pass = bool(beat_lo > placebo)

    return {
        "mean": mean_clv,
        "lower": clv_lower,
        "beat_rate": beat_rate,
        "beat_lower": beat_lo,
        "placebo_null": placebo,
        "n_eff": n_eff,
        "n": n,
        "sd": sd_clv,
        "gates": {
            "G0": {"value": n_eff, "pass": g0_pass},
            "G1": {"value": clv_lower, "pass": g1_pass, "floor": CLV_ROPE_FLOOR},
            "G2": {"value": z_obs, "pass": g2_pass, "threshold": z_thr},
            "G3": {"value": beat_lo, "pass": g3_pass, "placebo": placebo},
        },
    }
