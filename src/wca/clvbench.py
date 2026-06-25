"""Module C — full-book CLV benchmark.

Scores the *entire* model recommendation book (every home/draw/away leg of
every fixture-build in ``model_predictions_log.jsonl``) against the de-vigged
consensus closing line, fair-vs-fair.  Where Module A/B grade only the bets
that were actually placed, this grades the model's whole opinion set — the
honest denominator for "does the model beat the close".

For one model leg::

    o_model = 1 / p_model              (fair model decimal odds)
    o_close = 1 / p_close              (fair de-vigged consensus close)
    clv_odds(leg) = o_model / o_close - 1 = p_close / p_model - 1

``p_model`` is the model 1X2 triple from the build; ``p_close`` is the
de-vigged consensus close from :func:`wca.closecapture.consensus_close`
(both sides fair — no vig on either, so the comparison is fair-vs-fair).  A
positive ``clv_odds`` means the model's fair price was longer than where the
market closed: the model "beat the close".

``edge_build(leg) = p_model - p_market_build`` uses the market triple stored
*in the same build record* (the prices the model saw when it spoke), so the
edge-bucket analysis relates the edge the model claimed at build time to the
CLV it subsequently realised against the close.

Statistical honesty
--------------------
* Headline ``beat_rate`` is ``P(clv_odds > 0)`` over legs **with** a close;
  pushes (``clv_odds == 0``) and legs with **no** close are excluded from
  both numerator and denominator.  Reported with a Wilson 95% interval.
* A **label-shuffle placebo** (permute the model triples across fixtures
  *within each build*, ~200 shuffles) gives ``placebo_null`` — the beat-rate
  a model with no fixture-specific skill would post.  Because ``clv_odds`` is
  taken vs the *real* close, the null is **not** 0.50; it absorbs the residual
  consensus overround/skew, so it is the honest baseline to beat.
* ``clv_odds`` is right-skewed (capped below at -1, unbounded above), so the
  headline central tendency is the **median** and a **10% trimmed mean**, not
  the raw mean.
* Every aggregate carries its ``n``; market legs are **never pooled** (home /
  draw / away have structurally different price dynamics).

Deterministic & offline: no wall clock, no network.  The caller supplies the
``generated`` timestamp and the (read-only) ledger connection.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from wca import closecapture, tracking
from wca.data import teamnames

LEGS: Tuple[str, str, str] = ("home", "draw", "away")

# Edge buckets for clv-by-edge (edge = p_model - p_market_build).  Half-open
# [lo, hi); the last bucket's hi is +inf so the most-positive edge lands.
_EDGE_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("<=-5%", -1.0, -0.05),
    ("-5..-2%", -0.05, -0.02),
    ("-2..0%", -0.02, 0.0),
    ("0..2%", 0.0, 0.02),
    ("2..5%", 0.02, 0.05),
    (">5%", 0.05, 1.0),
)

# Odds buckets on the *model fair* decimal odds (1/p_model).
_ODDS_BUCKETS: Tuple[Tuple[str, float, float], ...] = (
    ("1.0-1.5", 1.0, 1.5),
    ("1.5-2.0", 1.5, 2.0),
    ("2.0-3.0", 2.0, 3.0),
    ("3.0-5.0", 3.0, 5.0),
    ("5.0+", 5.0, float("inf")),
)

_PLACEBO_SHUFFLES = 200
_PLACEBO_SEED = 42
_TRIM_FRAC = 0.10  # 10% trimmed mean


# --------------------------------------------------------------------------- #
# small stats helpers
# --------------------------------------------------------------------------- #
def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Wilson score interval for a binomial rate.

    Returns ``(p_hat, lo, hi)``; ``(None, None, None)`` when ``n == 0``.
    Handles ``k in {0, n}`` and ``n == 1`` without error.
    """
    if n <= 0:
        return (None, None, None)
    p = k / n
    z2 = z * z
    d = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return (p, lo, hi)


def _median(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    return float(np.median(np.asarray(xs, dtype=float)))


def _mean(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    return float(np.mean(np.asarray(xs, dtype=float)))


def trimmed_mean(xs: Sequence[float], frac: float = _TRIM_FRAC) -> Optional[float]:
    """Symmetric trimmed mean: drop ``frac`` from each tail, then average.

    Falls back to the plain mean when trimming would empty the sample (tiny
    n).  ``frac`` is clamped to ``[0, 0.5)``.
    """
    if not xs:
        return None
    arr = np.sort(np.asarray(xs, dtype=float))
    n = arr.size
    frac = min(max(frac, 0.0), 0.49)
    cut = int(math.floor(n * frac))
    core = arr[cut:n - cut] if n - 2 * cut > 0 else arr
    return float(np.mean(core))


def _beat_rate(clvs: Sequence[float]) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
    """Wilson beat-rate over CLVs, excluding pushes (clv == 0) from num+denom."""
    nz = [c for c in clvs if c != 0.0]
    k = sum(1 for c in nz if c > 0.0)
    p, lo, hi = wilson(k, len(nz))
    return (p, lo, hi, len(nz))


# --------------------------------------------------------------------------- #
# canonicalisation / loading
# --------------------------------------------------------------------------- #
def _canon(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return (teamnames.canonical(name) or "").strip().casefold()


def _pair_key(home: str, away: str) -> frozenset:
    return frozenset((_canon(home), _canon(away)))


def load_builds(jsonl_path: str) -> List[Dict[str, Any]]:
    """Load ``model_predictions_log.jsonl`` build records (skips blank lines)."""
    out: List[Dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return out


def _kickoff_iso(raw: Any) -> str:
    """Normalise a jsonl kickoff (``'2026-06-13 01:00:00+00:00'``) for SQL."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().replace(" ", "T")


def placed_legs(con: sqlite3.Connection) -> set:
    """Set of ``(pair_key, leg)`` model legs that were actually backed.

    Resolves every 1X2-style ledger bet to its fixture pair + leg (reusing the
    close-capture mappers).  A ``No`` share names the complement leg, so the
    leg the bet *expresses a positive view on* is the opposite of the named
    leg — but for "did we trade this leg's market" purposes we record the
    named leg the price refers to.  Used only to split the full book into
    ``placed`` vs ``passed``.
    """
    out: set = set()
    rows = con.execute(
        "SELECT match_desc, market, selection FROM bets"
    ).fetchall()
    for match_desc, market, selection in rows:
        if not closecapture.is_1x2_market(market):
            continue
        pair = tracking.split_fixture(match_desc or "")
        if pair is None:
            continue
        home, away = pair
        mapped = closecapture.selection_leg(selection, home, away)
        if mapped is None:
            continue
        leg, _is_no = mapped
        out.add((_pair_key(home, away), leg))
    return out


# --------------------------------------------------------------------------- #
# leg construction (the offline join)
# --------------------------------------------------------------------------- #
class Leg:
    """One scored model leg of one build.

    Attributes
    ----------
    clv:
        ``p_close / p_model - 1`` (fair-vs-fair), or ``None`` when the fixture
        has no captured close (counted in ``n_legs`` but excluded from every
        CLV aggregate).
    edge:
        ``p_model - p_market_build`` (always present — both from the build).
    """

    __slots__ = (
        "build_idx", "fixture", "pair", "leg", "p_model", "p_market",
        "p_close", "clv", "edge", "model_odds", "placed",
    )

    def __init__(
        self,
        build_idx: int,
        fixture: str,
        pair: frozenset,
        leg: str,
        p_model: float,
        p_market: Optional[float],
        p_close: Optional[float],
        placed: bool,
    ) -> None:
        self.build_idx = build_idx
        self.fixture = fixture
        self.pair = pair
        self.leg = leg
        self.p_model = p_model
        self.p_market = p_market
        self.p_close = p_close
        self.placed = placed
        self.clv = (p_close / p_model - 1.0) if (p_close is not None and p_model > 0.0) else None
        self.edge = (p_model - p_market) if p_market is not None else None
        self.model_odds = (1.0 / p_model) if p_model > 0.0 else None


def build_legs(
    builds: Sequence[Dict[str, Any]],
    con: sqlite3.Connection,
    placed: Optional[set] = None,
) -> List[Leg]:
    """Join builds x odds-snapshot closes into a flat list of scored legs.

    For each build record, the de-vigged consensus close is computed *once*
    per ``match_id`` (cached), then each of the three legs is emitted with its
    ``clv`` (``None`` if no close), ``edge`` and placed flag.
    """
    if placed is None:
        placed = placed_legs(con)
    # A "build" is one timestamped scan over several fixtures: group records by
    # their ``generated`` stamp so the placebo can permute model triples across
    # the fixtures that were scored together.  The jsonl is one row per
    # fixture-per-build, so the row index alone is NOT the build.
    build_ids: Dict[str, int] = {}
    close_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    legs: List[Leg] = []
    for rec in builds:
        model = rec.get("model") or {}
        market = rec.get("market") or {}
        fixture = str(rec.get("fixture") or "")
        match_id = str(rec.get("match_id") or "")
        gen = str(rec.get("generated") or "")
        bi = build_ids.setdefault(gen, len(build_ids))
        split = tracking.split_fixture(fixture)
        if split is None:
            continue
        home, away = split
        pair = _pair_key(home, away)
        if match_id not in close_cache:
            ko = _kickoff_iso(rec.get("kickoff"))
            close_cache[match_id] = (
                closecapture.consensus_close(con, match_id, home, away, ko)
                if match_id and ko else None
            )
        close = close_cache[match_id]
        triple = close["triple"] if close else None
        for leg in LEGS:
            try:
                p_model = float(model[leg])
            except (KeyError, TypeError, ValueError):
                continue
            if not 0.0 < p_model < 1.0:
                continue
            p_market = None
            try:
                pm = float(market[leg])
                if 0.0 < pm < 1.0:
                    p_market = pm
            except (KeyError, TypeError, ValueError):
                pass
            p_close = None
            if triple is not None:
                try:
                    pc = float(triple[leg])
                    if 0.0 < pc < 1.0:
                        p_close = pc
                except (KeyError, TypeError, ValueError):
                    p_close = None
            legs.append(
                Leg(
                    build_idx=bi,
                    fixture=fixture,
                    pair=pair,
                    leg=leg,
                    p_model=p_model,
                    p_market=p_market,
                    p_close=p_close,
                    placed=(pair, leg) in placed,
                )
            )
    return legs


# --------------------------------------------------------------------------- #
# placebo (label-shuffle within build)
# --------------------------------------------------------------------------- #
def placebo_beat_rate(
    legs: Sequence[Leg],
    builds: Sequence[Dict[str, Any]],
    con: sqlite3.Connection,
    n_shuffles: int = _PLACEBO_SHUFFLES,
    seed: int = _PLACEBO_SEED,
) -> Optional[float]:
    """Mean beat-rate when model triples are permuted across fixtures per build.

    For each shuffle the model 1X2 triples are randomly re-assigned among the
    fixtures *within the same build* (a build is one timestamped scan over
    several fixtures), the closes are held fixed, and the beat-rate
    recomputed.  Averaged over ``n_shuffles`` this is ``placebo_null`` — the
    beat-rate a skill-free model posts against the real close.  ``None`` when
    no build has ≥2 fixtures with a close (nothing to permute).
    """
    # Group legs by build, keeping only legs with a close (drives beat-rate).
    by_build: Dict[int, Dict[str, List[Leg]]] = {}
    for lg in legs:
        if lg.clv is None:
            continue
        by_build.setdefault(lg.build_idx, {}).setdefault(lg.leg, []).append(lg)

    # A build contributes only if it has ≥2 distinct fixtures to permute.
    permutable: List[Tuple[List[float], List[float]]] = []
    # For each (build, leg) collect aligned (p_model, p_close) over fixtures.
    units: List[Tuple[np.ndarray, np.ndarray]] = []
    for bi, by_leg in by_build.items():
        for leg, items in by_leg.items():
            if len(items) < 2:
                continue
            pm = np.array([it.p_model for it in items], dtype=float)
            pc = np.array([it.p_close for it in items], dtype=float)
            units.append((pm, pc))
    if not units:
        return None

    rng = np.random.Generator(np.random.PCG64(seed))
    rates: List[float] = []
    for _ in range(n_shuffles):
        wins = 0
        total = 0
        for pm, pc in units:
            perm = rng.permutation(pm.size)
            clv = pc / pm[perm] - 1.0
            nz = clv[clv != 0.0]
            wins += int(np.count_nonzero(nz > 0.0))
            total += int(nz.size)
        if total:
            rates.append(wins / total)
    return float(np.mean(rates)) if rates else None


def _placebo_edge_slope(
    legs: Sequence[Leg],
    n_shuffles: int = _PLACEBO_SHUFFLES,
    seed: int = _PLACEBO_SEED,
) -> Tuple[Dict[str, Optional[float]], Optional[float]]:
    """Placebo per-edge-bucket mean CLV + placebo drift slope.

    Permutes the (p_model, edge) pairing against the closes within each build
    (shuffling which model speaks for which fixture), so any apparent
    edge->CLV gradient under the null is exposed.  Returns
    ``({bucket_label: placebo_clv|None}, placebo_beta)``.
    """
    by_build: Dict[int, List[Leg]] = {}
    for lg in legs:
        if lg.clv is None or lg.edge is None:
            continue
        by_build.setdefault(lg.build_idx, []).append(lg)
    groups = [v for v in by_build.values() if len(v) >= 2]
    if not groups:
        return ({lbl: None for lbl, _, _ in _EDGE_BUCKETS}, None)

    rng = np.random.Generator(np.random.PCG64(seed))
    bucket_acc: Dict[str, List[float]] = {lbl: [] for lbl, _, _ in _EDGE_BUCKETS}
    slopes: List[float] = []
    for _ in range(n_shuffles):
        bucket_vals: Dict[str, List[float]] = {lbl: [] for lbl, _, _ in _EDGE_BUCKETS}
        all_edge: List[float] = []
        all_clv: List[float] = []
        for grp in groups:
            pm = np.array([g.p_model for g in grp], dtype=float)
            pc = np.array([g.p_close for g in grp], dtype=float)
            edge = np.array([g.edge for g in grp], dtype=float)
            perm = rng.permutation(pm.size)
            # model (and its edge) reassigned to fixtures; close stays put
            clv = pc / pm[perm] - 1.0
            e = edge[perm]
            for ed, cv in zip(e, clv):
                lbl = _edge_bucket_label(ed)
                bucket_vals[lbl].append(cv)
                all_edge.append(ed)
                all_clv.append(cv)
        for lbl, vals in bucket_vals.items():
            if vals:
                bucket_acc[lbl].append(float(np.mean(vals)))
        if len(all_edge) >= 2 and len(set(all_edge)) >= 2:
            slope = _ols_slope(all_edge, all_clv)
            if slope is not None:
                slopes.append(slope)
    placebo_bucket = {
        lbl: (float(np.mean(v)) if v else None) for lbl, v in bucket_acc.items()
    }
    placebo_beta = float(np.mean(slopes)) if slopes else None
    return (placebo_bucket, placebo_beta)


# --------------------------------------------------------------------------- #
# bucketers & regression
# --------------------------------------------------------------------------- #
def _edge_bucket_label(edge: float) -> str:
    for lbl, lo, hi in _EDGE_BUCKETS:
        if lo <= edge < hi:
            return lbl
    return _EDGE_BUCKETS[-1][0]


def _odds_bucket_label(odds: float) -> str:
    for lbl, lo, hi in _ODDS_BUCKETS:
        if lo <= odds < hi:
            return lbl
    return _ODDS_BUCKETS[-1][0]


def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Least-squares slope of ys on xs; ``None`` if degenerate."""
    if len(xs) < 2:
        return None
    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    vx = x - x.mean()
    denom = float(np.dot(vx, vx))
    if denom <= 0.0:
        return None
    return float(np.dot(vx, y - y.mean()) / denom)


# --------------------------------------------------------------------------- #
# section builders
# --------------------------------------------------------------------------- #
def _by_edge_bucket(
    legs: Sequence[Leg], placebo_bucket: Dict[str, Optional[float]]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for lbl, lo, hi in _EDGE_BUCKETS:
        clvs = [
            lg.clv
            for lg in legs
            if lg.clv is not None and lg.edge is not None
            and _edge_bucket_label(lg.edge) == lbl
        ]
        if clvs:
            p, c_lo, c_hi, _ = (None, None, None, 0)
            mean = _mean(clvs)
            # mean-CLV CI via normal approx on the mean (n small → wide).
            arr = np.asarray(clvs, dtype=float)
            n = arr.size
            if n >= 2:
                se = float(arr.std(ddof=1) / math.sqrt(n))
                c_lo, c_hi = mean - 1.96 * se, mean + 1.96 * se
            else:
                c_lo = c_hi = None
        else:
            mean = c_lo = c_hi = None
        out.append(
            {
                "bucket": lbl,
                "lo_edge": (None if math.isinf(lo) else round(lo, 6)),
                "hi_edge": (None if math.isinf(hi) else round(hi, 6)),
                "clv_mean": (None if mean is None else round(mean, 6)),
                "clv_lo": (None if c_lo is None else round(c_lo, 6)),
                "clv_hi": (None if c_hi is None else round(c_hi, 6)),
                "n": len(clvs),
                "placebo_clv": (
                    None if placebo_bucket.get(lbl) is None
                    else round(placebo_bucket[lbl], 6)
                ),
            }
        )
    return out


def _hist(clvs: Sequence[float], bins: int = 8) -> List[Dict[str, Any]]:
    if not clvs:
        return []
    arr = np.asarray(clvs, dtype=float)
    lo = float(arr.min())
    hi = float(arr.max())
    if hi <= lo:
        return [{"lo": round(lo, 6), "hi": round(hi, 6), "count": int(arr.size)}]
    counts, edges = np.histogram(arr, bins=bins, range=(lo, hi))
    return [
        {"lo": round(float(edges[i]), 6), "hi": round(float(edges[i + 1]), 6),
         "count": int(counts[i])}
        for i in range(len(counts))
    ]


def _by_market(legs: Sequence[Leg]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for leg in LEGS:
        clvs = [lg.clv for lg in legs if lg.leg == leg and lg.clv is not None]
        med = _median(clvs)
        out.append(
            {
                "leg": leg,
                "clv_median": (None if med is None else round(med, 6)),
                "n": len(clvs),
                "hist": _hist(clvs),
            }
        )
    return out


def _by_odds_bucket(legs: Sequence[Leg]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for lbl, _lo, _hi in _ODDS_BUCKETS:
        clvs = [
            lg.clv
            for lg in legs
            if lg.clv is not None and lg.model_odds is not None
            and _odds_bucket_label(lg.model_odds) == lbl
        ]
        med = _median(clvs)
        out.append(
            {
                "bucket": lbl,
                "clv_median": (None if med is None else round(med, 6)),
                "n": len(clvs),
            }
        )
    return out


def _placed_vs_passed(legs: Sequence[Leg]) -> Dict[str, Any]:
    placed = [lg.clv for lg in legs if lg.placed and lg.clv is not None]
    passed = [lg.clv for lg in legs if not lg.placed and lg.clv is not None]
    return {
        "placed": {
            "clv_median": (None if not placed else round(_median(placed), 6)),
            "n": len(placed),
        },
        "passed": {
            "clv_median": (None if not passed else round(_median(passed), 6)),
            "n": len(passed),
        },
    }


def _coverage_by_market(legs: Sequence[Leg]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for leg in LEGS:
        sub = [lg for lg in legs if lg.leg == leg]
        n = len(sub)
        clv_n = sum(1 for lg in sub if lg.clv is not None)
        out[leg] = {
            "n": n,
            "clv_n": clv_n,
            "coverage_pct": round(100.0 * clv_n / n, 2) if n else 0.0,
        }
    return out


def _brier_skill(legs: Sequence[Leg]) -> Optional[float]:
    """Brier skill of model vs build-market, scored against the close outcome.

    The "close" is treated as the ground-truth probability the market settled
    on; per build/fixture we one-hot the close's argmax leg and score the
    model's and the market's Brier against it.  ``skill = 1 -
    BS_model/BS_market`` — positive means the model is closer to the close than
    the build market was.  ``None`` when no closed fixture has all three legs.
    """
    # Group close-bearing legs by (build, fixture).
    groups: Dict[Tuple[int, frozenset], Dict[str, Leg]] = {}
    for lg in legs:
        if lg.p_close is None:
            continue
        groups.setdefault((lg.build_idx, lg.pair), {})[lg.leg] = lg
    bs_model: List[float] = []
    bs_market: List[float] = []
    for legmap in groups.values():
        if not all(k in legmap for k in LEGS):
            continue
        if any(legmap[k].p_market is None for k in LEGS):
            continue
        # outcome = leg the close most favours (argmax p_close)
        target = max(LEGS, key=lambda k: legmap[k].p_close)
        bm = sum((legmap[k].p_model - (1.0 if k == target else 0.0)) ** 2 for k in LEGS)
        bk = sum((legmap[k].p_market - (1.0 if k == target else 0.0)) ** 2 for k in LEGS)
        bs_model.append(bm)
        bs_market.append(bk)
    if not bs_model:
        return None
    mean_m = float(np.mean(bs_model))
    mean_k = float(np.mean(bs_market))
    if mean_k <= 0.0:
        return None
    return round(1.0 - mean_m / mean_k, 6)


# --------------------------------------------------------------------------- #
# top-level
# --------------------------------------------------------------------------- #
_NOTE = (
    "Full model book (every home/draw/away leg of every build) scored "
    "fair-vs-fair against the de-vigged consensus close: "
    "clv_odds = p_close/p_model - 1. beat_rate excludes pushes and "
    "no-close legs from numerator AND denominator (Wilson 95%). "
    "placebo_null = beat-rate under within-build label-shuffle (skill-free "
    "baseline vs the real close, absorbs consensus overround; beat THIS, not "
    "0.50). clv_odds is right-skewed so headline uses median + 10% trimmed "
    "mean. Market legs never pooled. Small sample: read n and coverage."
)


def build_benchmark(
    builds: Sequence[Dict[str, Any]],
    con: sqlite3.Connection,
    generated: str,
    placed: Optional[set] = None,
) -> Dict[str, Any]:
    """Compute the full ``tracking_clv_benchmark.json`` payload.

    Parameters
    ----------
    builds:
        Parsed ``model_predictions_log.jsonl`` records.
    con:
        Read-only ledger connection (``odds_snapshots`` + ``bets``).
    generated:
        ISO-8601 ``Z`` timestamp (caller-supplied — never the wall clock).
    placed:
        Optional pre-computed placed-leg set (else derived from ``bets``).
    """
    legs = build_legs(builds, con, placed=placed)
    with_close = [lg for lg in legs if lg.clv is not None]
    clvs = [lg.clv for lg in with_close]

    p, lo, hi, n_nz = _beat_rate(clvs)
    placebo = placebo_beat_rate(legs, builds, con)
    placebo_bucket, placebo_beta = _placebo_edge_slope(legs)

    # drift_beta: realised CLV regressed on the build edge (skill gradient).
    edge_xs = [lg.edge for lg in with_close if lg.edge is not None]
    edge_ys = [lg.clv for lg in with_close if lg.edge is not None]
    drift_beta = _ols_slope(edge_xs, edge_ys)

    # lead_rate: share of legs where the model's fair price LED the close,
    # i.e. model assigned higher prob than the close did (p_model > p_close).
    lead_n = sum(1 for lg in with_close if lg.p_model > lg.p_close)
    lead_rate = (lead_n / len(with_close)) if with_close else None

    coverage_pct = (100.0 * len(with_close) / len(legs)) if legs else 0.0

    payload: Dict[str, Any] = {
        "meta": {
            "generated": generated,
            "n_legs": len(legs),
            "n_with_close": len(with_close),
        },
        "headline": {
            "beat_rate": {
                "p": (None if p is None else round(p, 6)),
                "lo": (None if lo is None else round(lo, 6)),
                "hi": (None if hi is None else round(hi, 6)),
                "n": n_nz,
            },
            "placebo_null": (None if placebo is None else round(placebo, 6)),
            "clv_median": (None if not clvs else round(_median(clvs), 6)),
            "clv_trimmed_mean": (
                None if not clvs else round(trimmed_mean(clvs), 6)
            ),
            "lead_rate": (None if lead_rate is None else round(lead_rate, 6)),
            "drift_beta": (None if drift_beta is None else round(drift_beta, 6)),
            "brier_skill": _brier_skill(legs),
            "coverage_pct": round(coverage_pct, 2),
        },
        "by_edge_bucket": _by_edge_bucket(with_close, placebo_bucket),
        "by_market": _by_market(legs),
        "by_odds_bucket": _by_odds_bucket(legs),
        "placed_vs_passed": _placed_vs_passed(legs),
        "coverage_by_market": _coverage_by_market(legs),
        "note": _NOTE,
    }
    # placebo edge slope rides alongside drift_beta in the headline note path;
    # expose it on the edge-bucket section header implicitly via placebo_clv.
    payload["headline"]["placebo_drift_beta"] = (
        None if placebo_beta is None else round(placebo_beta, 6)
    )
    return payload
