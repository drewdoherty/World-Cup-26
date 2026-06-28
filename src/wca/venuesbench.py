"""Model-vs-Venue benchmark — probability-distance engine and rank inference.

The question this answers: *for the model's fair 1X2 probabilities, which betting
venue (an OddsAPI bookmaker, or Polymarket) sits statistically closest — and is
"closest" stable, and does it mean the venue is accurate or merely shares the
model's bias?*

This is the analysis layer ON TOP OF the existing stack — it does NOT re-implement
de-vig (uses :mod:`wca.markets.devig`), small-sample stats (uses
:mod:`wca.rigor.clv`), the model history (the ``predictions`` ledger) or the venue
data (``odds_snapshots``). It is pure, deterministic (seeded), and offline:
numpy + stdlib only, no wall-clock, no network.

Design commitments that keep the benchmark honest:

* **Distance, not agreement, is primary.** "Closest" is defined by probability
  distance (per-leg MAE/RMSE and absolute logit gap; per-fixture total-variation
  and Jensen-Shannon distance over the 1X2 triple). Correlation is secondary;
  fair-odds gaps are display-only (the tails explode). Agreement with the model
  is NOT accuracy — accuracy is scored separately against realised outcomes via
  paired Brier / log-loss.

* **Circularity is structural, not assumed away.** The deployed blend contains
  market consensus, so comparing a venue against it is not independent evidence.
  Callers therefore compare each venue against several model variants — the
  ex-market blend (Elo/DC only), Elo-only, DC-only — and, when comparing against
  the consensus, against a **leave-one-book-out** consensus that excludes the very
  venue under test (:func:`lobo_consensus`).

* **Honest ranking.** Primary ranking is on *common support* (the same fixtures
  with a fresh quote from every venue), uses within-observation ranks,
  fixture-block bootstrap CIs, a Friedman omnibus and paired permutation tests,
  and reports each venue's bootstrap probability of ranking first. When the
  evidence overlaps it says "no distinguishable winner" rather than inventing one.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from wca.markets import devig
from wca.rigor import clv as rigor_clv

#: 1X2 leg order used for every triple in this module.
LEGS: Tuple[str, str, str] = ("Home", "Draw", "Away")

#: Probability floor for log/logit/KL math (avoids -inf on a 0 leg).
EPS = 1e-6

_DEFAULT_SEED = rigor_clv._DEFAULT_SEED  # 20260625 — one seed for the whole stack


Triple = Tuple[float, float, float]


# ---------------------------------------------------------------------------
# Triple helpers.
# ---------------------------------------------------------------------------


def as_triple(obj) -> Triple:
    """Coerce a dict ``{Home,Draw,Away}`` or a 3-sequence to a normalised triple.

    Raises ``ValueError`` on a missing leg or a non-positive sum. The result is
    renormalised to sum to 1 (defends against minor upstream rounding).
    """
    if isinstance(obj, dict):
        try:
            vals = [float(obj[k]) for k in LEGS]
        except (KeyError, TypeError) as exc:
            raise ValueError("triple dict needs Home/Draw/Away") from exc
    else:
        seq = list(obj)
        if len(seq) != 3:
            raise ValueError("triple needs exactly 3 legs")
        vals = [float(x) for x in seq]
    s = sum(vals)
    if not math.isfinite(s) or s <= 0:
        raise ValueError("triple must have a positive, finite sum")
    return (vals[0] / s, vals[1] / s, vals[2] / s)


def _clip(t: Triple) -> Triple:
    """Clip a normalised triple away from 0/1 and renormalise (for log math)."""
    v = [min(1.0 - EPS, max(EPS, x)) for x in t]
    s = sum(v)
    return (v[0] / s, v[1] / s, v[2] / s)


def _logit(p: float) -> float:
    p = min(1.0 - EPS, max(EPS, p))
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Probability-distance metrics (primary definition of "closest").
# ---------------------------------------------------------------------------


def leg_abs_errors(p_model: Triple, p_venue: Triple) -> Triple:
    """Per-leg absolute probability error ``|p_model - p_venue|``."""
    a, b = as_triple(p_model), as_triple(p_venue)
    return (abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def mae(p_model: Triple, p_venue: Triple) -> float:
    """Mean absolute error across the three 1X2 legs."""
    return float(np.mean(leg_abs_errors(p_model, p_venue)))


def rmse(p_model: Triple, p_venue: Triple) -> float:
    """Root-mean-square error across the three 1X2 legs."""
    e = leg_abs_errors(p_model, p_venue)
    return float(math.sqrt(np.mean(np.square(e))))


def abs_logit_gap(p_model: Triple, p_venue: Triple) -> float:
    """Mean absolute log-odds (logit) gap across legs — scale-stable in the tails."""
    a, b = as_triple(p_model), as_triple(p_venue)
    return float(np.mean([abs(_logit(a[i]) - _logit(b[i])) for i in range(3)]))


def tv_distance(p_model: Triple, p_venue: Triple) -> float:
    """Total-variation distance over the 1X2 triple: ``0.5 * sum|p-q|`` in [0,1]."""
    a, b = as_triple(p_model), as_triple(p_venue)
    return float(0.5 * np.sum(np.abs(np.asarray(a) - np.asarray(b))))


def js_distance(p_model: Triple, p_venue: Triple) -> float:
    """Jensen-Shannon distance (sqrt of base-2 JS divergence) over 1X2, in [0,1]."""
    a, b = _clip(as_triple(p_model)), _clip(as_triple(p_venue))
    a_ = np.asarray(a)
    b_ = np.asarray(b)
    m = 0.5 * (a_ + b_)

    def _kl(x, y):
        return float(np.sum(x * (np.log2(x) - np.log2(y))))

    jsd = 0.5 * _kl(a_, m) + 0.5 * _kl(b_, m)
    return float(math.sqrt(max(0.0, jsd)))


#: The distance metrics exposed in the feed, keyed by name.
DISTANCE_METRICS = {
    "mae": mae,
    "rmse": rmse,
    "abs_logit": abs_logit_gap,
    "tv": tv_distance,
    "js": js_distance,
}


# ---------------------------------------------------------------------------
# Accuracy metrics (vs realised outcome) — agreement is NOT accuracy.
# ---------------------------------------------------------------------------


def brier(p: Triple, outcome: str) -> float:
    """Multi-class Brier score of a 1X2 triple against the realised outcome.

    ``sum_i (p_i - y_i)^2`` with ``y`` the one-hot of ``outcome`` (in :data:`LEGS`).
    Lower is better; range [0, 2].
    """
    if outcome not in LEGS:
        raise ValueError("outcome must be one of %s" % (LEGS,))
    t = as_triple(p)
    y = [1.0 if leg == outcome else 0.0 for leg in LEGS]
    return float(np.sum([(t[i] - y[i]) ** 2 for i in range(3)]))


def log_loss(p: Triple, outcome: str) -> float:
    """Negative log-likelihood of the realised outcome under a 1X2 triple."""
    if outcome not in LEGS:
        raise ValueError("outcome must be one of %s" % (LEGS,))
    t = _clip(as_triple(p))
    return float(-math.log(t[LEGS.index(outcome)]))


# ---------------------------------------------------------------------------
# De-vig a bookmaker triple; model comparators.
# ---------------------------------------------------------------------------


def book_fair_triple(odds: Dict[str, float], method: str = "shin") -> Optional[Triple]:
    """Fair 1X2 probabilities for ONE book from its decimal odds.

    ``odds`` maps each of :data:`LEGS` to a decimal price. Returns ``None`` when
    the book's 1X2 is incomplete or invalid (a missing/<=1.0 leg) — incomplete
    books are OMITTED, never imputed.
    """
    try:
        triple = [float(odds[k]) for k in LEGS]
    except (KeyError, TypeError, ValueError):
        return None
    if any((not math.isfinite(o)) or o <= 1.0 for o in triple):
        return None
    try:
        probs = devig.devig(triple, method=method)
    except Exception:
        return None
    return as_triple(tuple(float(x) for x in probs))


def ex_market_triple(elo: Triple, dc: Triple, w_elo: float = 0.30, w_dc: float = 0.70) -> Triple:
    """Market-free model blend: ``renorm(w_elo*elo + w_dc*dc)``.

    This is the circularity-safe model side — it contains NO market consensus, so
    a venue's closeness to it IS independent evidence. Weights default to the
    deployed 0.30/0.70 Elo/DC split; the market component is dropped (weight 0).
    """
    a, b = as_triple(elo), as_triple(dc)
    s = w_elo + w_dc
    if s <= 0:
        raise ValueError("blend weights must be positive")
    mix = [(w_elo * a[i] + w_dc * b[i]) / s for i in range(3)]
    return as_triple(tuple(mix))


def consensus_triple(book_triples: Sequence[Triple]) -> Optional[Triple]:
    """Equal-weight consensus of de-vigged book triples (mean per leg, renorm)."""
    triples = [as_triple(t) for t in book_triples if t is not None]
    if not triples:
        return None
    arr = np.asarray(triples, dtype=float)
    mean = arr.mean(axis=0)
    return as_triple(tuple(mean))


def lobo_consensus(triples_by_book: Dict[str, Triple], exclude: str) -> Optional[Triple]:
    """Leave-one-book-out consensus: mean of every book EXCEPT ``exclude``.

    This is the independent comparator for a venue under test — the model side
    never contains the book it is being scored against. Returns ``None`` when no
    other book is available (cannot form an independent consensus).
    """
    others = [t for b, t in triples_by_book.items() if b != exclude and t is not None]
    if not others:
        return None
    return consensus_triple(others)


# ---------------------------------------------------------------------------
# Executable price (exchange commission); display-only fair-odds gap.
# ---------------------------------------------------------------------------


def exchange_executable_prob(fair_mid_odds: float, commission: float = 0.02) -> float:
    """Commission-adjusted *executable* implied probability for an exchange back.

    On an exchange you keep ``(odds-1)*(1-commission)`` of net winnings, so the
    effective decimal odds are ``1 + (odds-1)*(1-commission)`` and the executable
    implied probability is its reciprocal. Used for Betfair/Smarkets/Matchbook.
    """
    if fair_mid_odds <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    eff = 1.0 + (fair_mid_odds - 1.0) * (1.0 - commission)
    return 1.0 / eff


def fair_odds_gap(p_model: Triple, p_venue: Triple) -> Triple:
    """Per-leg fair-odds difference ``1/p_venue - 1/p_model`` (DISPLAY ONLY).

    Fair-odds gaps explode on longshot legs, so they are never used for ranking;
    they exist only to render a human-readable price view.
    """
    a, b = _clip(as_triple(p_model)), _clip(as_triple(p_venue))
    return tuple(float(1.0 / b[i] - 1.0 / a[i]) for i in range(3))


# ---------------------------------------------------------------------------
# Ranking & inference on common support.
# ---------------------------------------------------------------------------


def common_support(panel: Dict[str, Dict[str, float]], venues: Sequence[str]) -> List[str]:
    """Observation ids for which EVERY venue in ``venues`` has a finite distance.

    ``panel`` maps ``obs_id -> {venue -> distance}``. Primary ranking must use the
    same observations across all venues, otherwise venue rankings are not
    comparable (a venue that only quotes easy fixtures would look spuriously good).
    """
    out = []
    for obs_id, row in panel.items():
        if all(v in row and row[v] is not None and math.isfinite(row[v]) for v in venues):
            out.append(obs_id)
    return sorted(out)


def within_obs_ranks(panel: Dict[str, Dict[str, float]], venues: Sequence[str],
                     obs_ids: Sequence[str]) -> Dict[str, List[float]]:
    """Per-observation ranks (1 = closest) for each venue, over ``obs_ids``.

    Ties share the average rank. Lower distance -> better (lower) rank.
    """
    ranks: Dict[str, List[float]] = {v: [] for v in venues}
    for obs_id in obs_ids:
        row = panel[obs_id]
        dists = np.asarray([row[v] for v in venues], dtype=float)
        order = dists.argsort(kind="stable")
        rk = np.empty(len(venues), dtype=float)
        # Average-rank tie handling.
        sorted_d = dists[order]
        i = 0
        while i < len(sorted_d):
            j = i
            while j + 1 < len(sorted_d) and sorted_d[j + 1] == sorted_d[i]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie block
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        for vi, v in enumerate(venues):
            ranks[v].append(float(rk[vi]))
    return ranks


def _fixture_of(obs_id: str) -> str:
    """Cluster key for an observation: everything before the first ``|``.

    Observation ids are ``"<match_id>|<build_id>"`` so the fixture is the natural
    bootstrap cluster (legs/builds of one match are correlated).
    """
    return obs_id.split("|", 1)[0]


def fixture_block_bootstrap(values_by_obs: Dict[str, float], obs_ids: Sequence[str],
                            *, n_boot: int = 2000, seed: int = _DEFAULT_SEED
                            ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Point estimate and 95% CI for the mean of ``values`` over ``obs_ids``,
    resampling **fixtures** (clusters) with replacement.

    Returns ``(mean, lo, hi)``; ``(None, None, None)`` for an empty sample.
    """
    vals = np.asarray([values_by_obs[o] for o in obs_ids], dtype=float)
    if vals.size == 0:
        return (None, None, None)
    point = float(np.mean(vals))
    # Group observation indices by fixture cluster.
    clusters: Dict[str, List[int]] = {}
    for i, o in enumerate(obs_ids):
        clusters.setdefault(_fixture_of(o), []).append(i)
    keys = list(clusters.keys())
    if len(keys) <= 1:
        return (point, None, None)  # one cluster -> no honest CI
    arrays = [vals[np.asarray(clusters[k])] for k in keys]
    rng = np.random.default_rng(seed)
    idx = np.arange(len(keys))
    boot = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        pick = rng.choice(idx, size=len(keys), replace=True)
        boot[b] = float(np.mean(np.concatenate([arrays[j] for j in pick])))
    lo = float(np.percentile(boot, 2.5))
    hi = float(np.percentile(boot, 97.5))
    return (point, lo, hi)


def p_rank_first(panel: Dict[str, Dict[str, float]], venues: Sequence[str],
                 obs_ids: Sequence[str], *, n_boot: int = 2000, seed: int = _DEFAULT_SEED
                 ) -> Dict[str, float]:
    """Bootstrap probability that each venue has the lowest mean distance.

    Resamples fixtures with replacement; per resample, the venue with the
    smallest mean distance scores a win (ties split evenly). Returns
    ``{venue -> P(rank 1)}`` summing to 1.
    """
    if not obs_ids:
        return {v: 0.0 for v in venues}
    clusters: Dict[str, List[int]] = {}
    for i, o in enumerate(obs_ids):
        clusters.setdefault(_fixture_of(o), []).append(i)
    keys = list(clusters.keys())
    mat = np.asarray([[panel[o][v] for v in venues] for o in obs_ids], dtype=float)
    rng = np.random.default_rng(seed + 1)
    wins = np.zeros(len(venues), dtype=float)
    idx = np.arange(len(keys))
    cluster_rows = [np.asarray(clusters[k]) for k in keys]
    for _ in range(n_boot):
        pick = rng.choice(idx, size=len(keys), replace=True)
        rows = np.concatenate([cluster_rows[j] for j in pick])
        means = mat[rows].mean(axis=0)
        m = means.min()
        winners = np.where(means == m)[0]
        wins[winners] += 1.0 / len(winners)
    return {venues[i]: float(wins[i] / n_boot) for i in range(len(venues))}


def friedman_test(panel: Dict[str, Dict[str, float]], venues: Sequence[str],
                  obs_ids: Sequence[str]) -> Tuple[Optional[float], Optional[float]]:
    """Friedman omnibus over venues (blocks = observations). Returns (stat, p).

    Tests whether at least one venue's distance distribution differs. ``(None,
    None)`` when there are too few blocks/venues.
    """
    k = len(venues)
    n = len(obs_ids)
    if k < 3 or n < 2:
        return (None, None)
    ranks = within_obs_ranks(panel, venues, obs_ids)
    rank_sums = np.asarray([np.sum(ranks[v]) for v in venues], dtype=float)
    stat = (12.0 / (n * k * (k + 1))) * float(np.sum(rank_sums ** 2)) - 3.0 * n * (k + 1)
    try:
        from scipy.stats import chi2
        p = float(chi2.sf(stat, k - 1))
    except Exception:  # pragma: no cover - scipy is a dependency
        p = None
    return (float(stat), p)


def paired_permutation_test(a: Sequence[float], b: Sequence[float], *,
                            n_perm: int = 5000, seed: int = _DEFAULT_SEED) -> Optional[float]:
    """Two-sided paired permutation (sign-flip) test on ``mean(a - b)``.

    Returns the permutation p-value, or ``None`` for an empty/degenerate sample.
    """
    da = np.asarray(a, dtype=float)
    db = np.asarray(b, dtype=float)
    if da.size == 0 or da.shape != db.shape:
        return None
    diff = da - db
    obs = abs(float(np.mean(diff)))
    if np.allclose(diff, 0.0):
        return 1.0
    rng = np.random.default_rng(seed + 2)
    count = 0
    for _ in range(n_perm):
        signs = rng.choice([-1.0, 1.0], size=diff.size)
        if abs(float(np.mean(diff * signs))) >= obs - 1e-15:
            count += 1
    return float((count + 1) / (n_perm + 1))


def bh_fdr(pvals: Sequence[Optional[float]], alpha: float = 0.05) -> List[Optional[float]]:
    """Benjamini-Hochberg adjusted q-values, preserving input order.

    ``None`` p-values pass through as ``None`` and are excluded from the count.
    """
    indexed = [(i, p) for i, p in enumerate(pvals) if p is not None]
    m = len(indexed)
    q: List[Optional[float]] = [None] * len(pvals)
    if m == 0:
        return q
    indexed.sort(key=lambda t: t[1])
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i, p = indexed[rank]
        val = min(prev, p * m / (rank + 1))
        q[i] = float(val)
        prev = val
    return q


# ---------------------------------------------------------------------------
# Leaderboard assembly (the headline ranking + honest verdict).
# ---------------------------------------------------------------------------

#: Below this many common-support fixture-blocks, no winner is declarable.
MIN_COMMON_FIXTURES = 10


def rank_venues(panel: Dict[str, Dict[str, float]], venues: Sequence[str], *,
                metric: str = "mae", n_boot: int = 2000, seed: int = _DEFAULT_SEED
                ) -> Dict[str, object]:
    """Rank venues by closeness on common support, with CIs, P(rank1) and a verdict.

    ``panel`` maps ``obs_id -> {venue -> distance}`` for ONE metric/comparator.
    Returns a JSON-ready dict: the common-support N, per-venue mean distance +
    fixture-block CI + mean within-obs rank + P(rank 1), the Friedman omnibus,
    and a headline ``verdict`` that is "no distinguishable winner" unless the
    evidence actually separates the field.
    """
    venues = list(venues)
    support = common_support(panel, venues)
    n_fix = len({_fixture_of(o) for o in support})
    out: Dict[str, object] = {
        "metric": metric,
        "n_obs": len(support),
        "n_fixtures": n_fix,
        "venues": [],
        "friedman": {"stat": None, "p": None},
        "verdict": None,
    }
    if not support:
        out["verdict"] = "insufficient — no common support across venues"
        return out

    ranks = within_obs_ranks(panel, venues, support)
    rows = []
    p1 = p_rank_first(panel, venues, support, n_boot=n_boot, seed=seed)
    for v in venues:
        vals_by_obs = {o: panel[o][v] for o in support}
        mean_d, lo, hi = fixture_block_bootstrap(vals_by_obs, support, n_boot=n_boot, seed=seed)
        rows.append({
            "venue": v,
            "mean_distance": None if mean_d is None else round(mean_d, 6),
            "ci_lo": None if lo is None else round(lo, 6),
            "ci_hi": None if hi is None else round(hi, 6),
            "mean_rank": round(float(np.mean(ranks[v])), 4),
            "p_rank1": round(p1[v], 4),
            "n": len(support),
        })
    rows.sort(key=lambda r: (r["mean_rank"], r["mean_distance"] if r["mean_distance"] is not None else 1e9))
    out["venues"] = rows

    stat, p = friedman_test(panel, venues, support)
    out["friedman"] = {"stat": None if stat is None else round(stat, 4),
                       "p": None if p is None else round(p, 6)}

    # Verdict: need enough common-support fixtures AND a separated leader.
    if n_fix < MIN_COMMON_FIXTURES:
        out["verdict"] = ("no distinguishable winner (insufficient common support, "
                          "n_fixtures=%d < %d)" % (n_fix, MIN_COMMON_FIXTURES))
        return out
    leader = rows[0]
    runner = rows[1] if len(rows) > 1 else None
    separated = (
        p is not None and p < 0.05
        and leader["ci_hi"] is not None and runner is not None and runner["ci_lo"] is not None
        and leader["ci_hi"] < runner["ci_lo"]
        and leader["p_rank1"] >= 0.5
    )
    if separated:
        out["verdict"] = ("closest venue: %s (mean rank %.2f, P(rank1)=%.0f%%)"
                          % (leader["venue"], leader["mean_rank"], 100 * leader["p_rank1"]))
    else:
        out["verdict"] = "no distinguishable winner (top venues' evidence overlaps)"
    return out
