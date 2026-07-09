"""Score the SHADOW model variants against realised 1X2 outcomes.

Nothing in this module or its CLI (``scripts/wca_shadow_score.py``) touches live
pricing, sizing, or selection. It reads the append-only prediction log written
by :mod:`wca.modelpreds` plus the realised-results file and, for every shadow
family present, computes paired Brier + log-loss against the *live* deployed
blend (the ``model`` triple), with bootstrap CIs on the paired diff.

Design (mirrors the "computation in :mod:`wca`, CLI gathers inputs" split used
by :mod:`wca.tracking`):

* the log is deduped to the **last pre-kickoff** row per fixture (reusing
  :func:`wca.tracking.exact_model_before` on the ``model`` triple, so a fixture
  only counts once and never with a post-hoc build);
* the 1X2 shadow families (``mw90`` / ``shrink``) are **counterfactually
  recomputed** from the ``elo`` / ``dc`` / ``market`` / ``model`` triples that
  every historical row already carries — so they are scored over ALL settled
  fixtures, not only the future dual-writes;
* the goal-lambda shadow families are DISCOVERED dynamically from whichever
  ``<prefix>_lambda_home`` / ``<prefix>_lambda_away`` (or ``..._blend_home/away``)
  keys are actually present in the log (see :func:`discover_lambda_prefixes`;
  currently ``gb`` and ``tl``, but a new dual-write family needs no code change
  to be judged) and settle a *totals* market (total goals over/under a line,
  plus a BTTS Brier and mean signed goal-lambda bias as extra diagnostics) —
  scored only where the row carries the lambdas AND the realised score is
  known, and kept in a SEPARATE market family from the 1X2 variants (never
  mixed);
* every family is scored **paired** against the live baseline on the exact same
  fixture set, and a bootstrap 90% CI on the mean paired diff drives a
  ``decision`` (PROMOTE / KILL / COLLECTING) at an ``n >= 30`` gate.

The convention throughout: **lower is better** (Brier and log-loss are losses),
so ``diff = shadow - live``. A NEGATIVE diff means the shadow beat live; the
promote gate fires when the CI is entirely below zero.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from wca import tracking

_LEGS = ("home", "draw", "away")

# Group stage 2026-06-11..2026-06-27; knockout (Round of 32) from 2026-06-28.
# The 2026 results file carries no stage field and there is no fixtures->stage
# lookup in the repo, so the split is by kickoff date against this documented
# boundary. Heuristic, flagged in the scoreboard meta; it only affects the
# split rows, never the overall paired scores.
KNOCKOUT_START_DATE = "2026-06-28"

# Decision gate: a paired 90% CI must exclude 0 at this many settled fixtures
# before the scorer will call PROMOTE / KILL; below it, COLLECTING.
DECISION_MIN_N = 30
DEFAULT_BOOTSTRAP = 2000
DEFAULT_CI = 0.90

# Recompute knobs for the counterfactual 1X2 shadows must match the live writer
# (imported so they can never silently drift out of sync with modelpreds).
from wca.modelpreds import (  # noqa: E402
    _mw90_triple,
    _shrink_triple,
    _valid_triple,
)


# ---------------------------------------------------------------------------
# Metrics (lower is better).
# ---------------------------------------------------------------------------


def brier_1x2(triple: Optional[Mapping[str, float]], outcome: str) -> Optional[float]:
    """Multiclass Brier of a 1X2 triple vs a ``home``/``draw``/``away`` outcome."""
    return tracking.brier_1x2(dict(triple) if triple else None, outcome)


def log_loss_1x2(triple: Optional[Mapping[str, float]], outcome: str) -> Optional[float]:
    """Negative log-likelihood of *outcome* under the triple."""
    return tracking.log_loss_1x2(dict(triple) if triple else None, outcome)


def brier_binary(p: Optional[float], hit: int) -> Optional[float]:
    """Binary Brier ``(p - hit)^2`` for a single-probability (totals) market."""
    if p is None:
        return None
    return (float(p) - float(hit)) ** 2


def log_loss_binary(p: Optional[float], hit: int, eps: float = 1e-12) -> Optional[float]:
    """Binary log-loss for a single-probability market (``p`` = P(hit=1))."""
    if p is None:
        return None
    q = min(max(float(p), eps), 1.0 - eps)
    return -math.log(q if hit else 1.0 - q)


# ---------------------------------------------------------------------------
# Totals-market probability from a goal-lambda pair (for gb / tl shadows).
# ---------------------------------------------------------------------------


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def prob_over_line(lambda_home: float, lambda_away: float, line: float,
                   max_goals: int = 15) -> Optional[float]:
    """P(total goals > *line*) under two independent Poissons.

    Used to settle the goal-lambda shadows (gb / tl) against a totals line. The
    two teams' goals are assumed independent (the DC correlation only nudges the
    low-score cells and is immaterial to a total-goals over/under at these
    lines). ``line`` is a half-integer (e.g. 2.5) so there is never a push.
    ``None`` for a non-finite lambda.
    """
    if lambda_home is None or lambda_away is None:
        return None
    if not (math.isfinite(lambda_home) and math.isfinite(lambda_away)):
        return None
    lam_total = float(lambda_home) + float(lambda_away)
    # Total goals of two independent Poissons is Poisson(lam_home + lam_away).
    threshold = math.floor(line) + 1  # goals strictly above the line
    p_at_least = 0.0
    for k in range(threshold, max_goals + 1):
        p_at_least += _poisson_pmf(k, lam_total)
    return p_at_least


def prob_btts_yes(lambda_home: float, lambda_away: float) -> Optional[float]:
    """P(both teams score) under two independent Poissons.

    Same independence convention as :func:`prob_over_line` (a goal-lambda pair
    carries no correlation term, so BTTS is scored as ``P(home>0)*P(away>0)``
    rather than the DC-matrix inclusion-exclusion used elsewhere in the repo,
    e.g. :meth:`wca.models.dixon_coles.DixonColesModel.both_teams_to_score`).
    ``None`` for a non-finite lambda.
    """
    if lambda_home is None or lambda_away is None:
        return None
    if not (math.isfinite(lambda_home) and math.isfinite(lambda_away)):
        return None
    p_home_scores = 1.0 - math.exp(-float(lambda_home))
    p_away_scores = 1.0 - math.exp(-float(lambda_away))
    return p_home_scores * p_away_scores


def signed_goal_bias(lambda_home: float, lambda_away: float,
                     actual_total_goals: int) -> Optional[float]:
    """``(lambda_home + lambda_away) - actual_total_goals`` for one fixture.

    Positive => the lambda pair over-predicted goals; negative => it
    under-predicted. Averaged across fixtures in the scoreboard to surface a
    systematic (signed, not squared-error) bias per shadow family — this is a
    diagnostic column, not part of the PROMOTE/KILL decision. ``None`` for a
    non-finite lambda.
    """
    if lambda_home is None or lambda_away is None:
        return None
    if not (math.isfinite(lambda_home) and math.isfinite(lambda_away)):
        return None
    return (float(lambda_home) + float(lambda_away)) - float(actual_total_goals)


# ---------------------------------------------------------------------------
# Bootstrap CI on a paired difference.
# ---------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def bootstrap_ci(diffs: Sequence[float], ci: float = DEFAULT_CI,
                 n_boot: int = DEFAULT_BOOTSTRAP, seed: int = 12345
                 ) -> Tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI on the MEAN of a paired-difference sample.

    Deterministic (fixed *seed*) so the scorer is idempotent — the same log +
    results always yield the same scoreboard. Returns ``(lo, hi)``; ``(None,
    None)`` for an empty sample.
    """
    vals = [float(x) for x in diffs if x is not None]
    n = len(vals)
    if n == 0:
        return (None, None)
    if n == 1:
        return (vals[0], vals[0])
    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += vals[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_q = (1.0 - ci) / 2.0
    hi_q = 1.0 - lo_q
    lo = means[max(0, int(math.floor(lo_q * (n_boot - 1))))]
    hi = means[min(n_boot - 1, int(math.ceil(hi_q * (n_boot - 1))))]
    return (lo, hi)


# ---------------------------------------------------------------------------
# Log dedup + shadow-triple extraction.
# ---------------------------------------------------------------------------


def _stage_for(date: str) -> str:
    return "knockout" if (date or "") >= KNOCKOUT_START_DATE else "group"


def dedup_pre_kickoff(rows: Sequence[Mapping[str, Any]],
                      result: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Last pre-kickoff log row for a result's fixture, or ``None``.

    Reuses :func:`wca.tracking.exact_model_before` (matches on canonical fixture
    key + guards the ``model`` triple + requires ``generated < kickoff``), so a
    fixture is scored exactly once and never with a post-hoc build.
    """
    key = tracking.fixture_key(str(result.get("fixture") or ""))
    if key is None:
        return None
    kickoff = result.get("kickoff_utc") or result.get("date")
    row = tracking.exact_model_before(list(rows), key, kickoff)
    return dict(row) if row is not None else None


def _live_1x2(row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    model = row.get("model")
    return dict(model) if _valid_triple(model) else None


def _market_1x2(row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    mkt = row.get("market")
    return dict(mkt) if _valid_triple(mkt) else None


def _raw_model_for_shrink(row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """The RAW model blend to feed the ``shrink`` recompute (lenient fallback).

    Since the 2026-07-09 shrink promotion the live writer persists ``model_raw``
    (the raw blend) alongside ``model`` (the shrunk live line). Historical rows
    predate ``model_raw``; there ``model`` IS the raw blend (no live shrink was
    applied), so we fall back to it. This keeps the ``shrink`` recompute honest
    across the promotion boundary — it always shrinks the RAW model, never an
    already-shrunk value.
    """
    raw = row.get("model_raw")
    if _valid_triple(raw):
        return dict(raw)
    model = row.get("model")
    return dict(model) if _valid_triple(model) else None


def _raw_1x2(row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """The RAW pre-shrink blend for the ``raw`` family (STRICT — no fallback).

    Only returns a triple when the row genuinely carries ``model_raw`` (i.e. a
    post-promotion build). Pre-promotion rows — where ``model`` already IS the
    raw blend — return ``None`` so the ``raw`` family does not accumulate a pile
    of trivially-zero (raw == live) paired diffs; it measures the promotion only
    over builds where a distinct pre-shrink blend was actually recorded.
    """
    raw = row.get("model_raw")
    return dict(raw) if _valid_triple(raw) else None


def shadow_1x2(family: str, row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """1X2 triple for a shadow *family*, recomputed from the row's components.

    ``mw90`` / ``shrink`` are recomputed (so historical rows predating the
    dual-write are still scored); ``market`` / ``live`` / ``raw`` are read from
    the row. Returns ``None`` when the required component triples are absent.

    Post-promotion the ``live`` baseline (``model``) is the shrunk line. The
    ``shrink`` recompute reads the RAW model via :func:`_raw_model_for_shrink`
    (``model_raw`` when present, else ``model`` for pre-promotion rows) so it
    shrinks the raw blend, not an already-shrunk one. The ``raw`` family reads
    the RAW blend STRICTLY (:func:`_raw_1x2`, only where ``model_raw`` is
    present) so it scores the ex-live blend against the now-live shrunk one only
    over post-promotion builds.
    """
    if family == "live":
        return _live_1x2(row)
    if family == "market":
        return _market_1x2(row)
    if family == "raw":
        return _raw_1x2(row)
    elo, dc, mkt = row.get("elo"), row.get("dc"), row.get("market")
    if family == "mw90":
        return _mw90_triple(elo, dc, mkt) if _valid_triple(mkt) else None
    if family == "shrink":
        raw_model = _raw_model_for_shrink(row)
        return (
            _shrink_triple(raw_model, mkt)
            if (raw_model is not None and _valid_triple(mkt))
            else None
        )
    return None


_LIVE_LAMBDA_KEYS = ("lambda_home", "lambda_away")


def discover_lambda_prefixes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """Scan log rows for ``<prefix>_lambda_home`` / ``<prefix>_lambda_away`` pairs.

    Returns ``{prefix: (home_key, away_key)}`` for every goal-lambda shadow
    family PRESENT IN THE DATA — so a brand-new shadow (any future
    ``<prefix>_lambda_*`` dual-write) is picked up automatically without a code
    change, and a family with zero rows in the log never appears at all (no
    fabricated empty families). Recognises both the plain pattern
    (``gb_lambda_home`` / ``gb_lambda_away`` -> prefix ``gb``) and the
    qualified pattern used by the totals-prior blend (``tl_lambda_blend_home``
    / ``tl_lambda_blend_away`` -> prefix ``tl``, stem ``tl_lambda_blend``). The
    bare ``lambda_home`` / ``lambda_away`` (the live DC baseline) is never
    treated as a discovered family — it is the fixed comparison baseline for
    every totals shadow, handled by :func:`totals_lambdas` under the
    ``"live"`` family name.
    """
    stems: set = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in row.keys():
            if key.endswith("_home") and "lambda" in key:
                stem = key[: -len("_home")]
                if stem and stem != "lambda" and (stem + "_away") in row:
                    stems.add(stem)
    prefixes: Dict[str, Tuple[str, str]] = {}
    for stem in stems:
        idx = stem.find("_lambda")
        if idx <= 0:
            continue  # no non-empty prefix before "_lambda" -> not a shadow family
        prefix = stem[:idx]
        # First stem wins for a given prefix (stable given one naming scheme
        # per prefix in practice); avoids silently merging two conventions.
        prefixes.setdefault(prefix, (stem + "_home", stem + "_away"))
    return prefixes


def totals_lambdas(family: str, row: Mapping[str, Any],
                   prefix_keys: Optional[Mapping[str, Tuple[str, str]]] = None
                   ) -> Optional[Tuple[float, float]]:
    """``(lambda_home, lambda_away)`` for a goal-lambda shadow family, or ``None``.

    ``live`` uses the deployed DC lambdas (``lambda_home`` / ``lambda_away``).
    Any other *family* is looked up in *prefix_keys* (as produced by
    :func:`discover_lambda_prefixes`) — e.g. ``gb`` -> ``gb_lambda_*``, ``tl``
    -> ``tl_lambda_blend_*``. Missing lambdas, or a family absent from
    *prefix_keys*, -> ``None`` (row skipped, never fabricated).
    """
    if family == "live":
        keys = _LIVE_LAMBDA_KEYS
    else:
        keys = (prefix_keys or {}).get(family)
    if keys is None:
        return None
    lam_h, lam_a = row.get(keys[0]), row.get(keys[1])
    if isinstance(lam_h, (int, float)) and isinstance(lam_a, (int, float)):
        return (float(lam_h), float(lam_a))
    return None


# ---------------------------------------------------------------------------
# Family scoring.
# ---------------------------------------------------------------------------

# Which 1X2 shadow families we score (against the ``live`` baseline). ``market``
# is included as a reference column (the n=73 evidence's in-sample winner).
# ``raw`` is the PRE-shrink blend: after the 2026-07-09 shrink promotion the
# ``live`` baseline (``model``) is the shrunk line, so ``raw`` measures the
# blend that USED to be live against the one that is now live (a positive
# ``raw`` brier_diff = the promotion helped). ``shrink`` still recomputes the
# shrunk line from the RAW model, so it should track ``live`` closely once every
# scored row carries ``model_raw``.
ONEX2_SHADOWS = ("raw", "mw90", "shrink", "market")
# Goal-lambda shadow families are DISCOVERED dynamically per-run from the log
# rows (see discover_lambda_prefixes) rather than hardcoded, so a new
# "<prefix>_lambda_home/away" dual-write is judged automatically.
TOTALS_LINE = 2.5


def _paired_scores(
    matched: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
    shadow_family: str,
    market: str,
    prefix_keys: Optional[Mapping[str, Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Paired Brier/log-loss of *shadow_family* vs the live baseline.

    *matched* is a list of ``(row, result)`` pairs (already deduped to one per
    fixture). *market* is ``"1x2"`` or ``"totals"`` and selects both the
    baseline and the metric. Only fixtures where BOTH the shadow and the live
    baseline produce a probability are counted (a true paired comparison).

    For ``market == "totals"``, two extra diagnostics are computed from the
    SAME goal-lambda pair (never gating the PROMOTE/KILL decision, which stays
    driven by the primary total-goals O/U metric): a paired BTTS Brier (own n
    + bootstrap CI, since a fixture can be missing a lambda for one metric but
    not the other — e.g. a non-finite lambda still parses a score) and the
    mean SIGNED goal bias (shadow lambda-sum minus actual total goals, and the
    same for live) — a diagnostic of systematic over/under-prediction, not a
    loss, so no CI is computed for it.
    """
    brier_shadow: List[float] = []
    brier_live: List[float] = []
    ll_shadow: List[float] = []
    ll_live: List[float] = []
    brier_diffs: List[float] = []
    ll_diffs: List[float] = []
    split: Dict[str, Dict[str, List[float]]] = {
        "group": {"brier": [], "logloss": []},
        "knockout": {"brier": [], "logloss": []},
    }
    btts_brier_shadow: List[float] = []
    btts_brier_live: List[float] = []
    btts_diffs: List[float] = []
    bias_shadow: List[float] = []
    bias_live: List[float] = []

    for row, result in matched:
        if market == "1x2":
            outcome = result.get("outcome")
            if outcome not in _LEGS:
                continue
            s_triple = shadow_1x2(shadow_family, row)
            l_triple = shadow_1x2("live", row)
            if s_triple is None or l_triple is None:
                continue
            b_s = brier_1x2(s_triple, outcome)
            b_l = brier_1x2(l_triple, outcome)
            ll_s = log_loss_1x2(s_triple, outcome)
            ll_l = log_loss_1x2(l_triple, outcome)
        else:  # totals
            parsed = tracking.parse_score(result.get("score"))
            if parsed is None:
                continue
            total_goals = parsed[0] + parsed[1]
            hit = 1 if total_goals > TOTALS_LINE else 0
            s_lam = totals_lambdas(shadow_family, row, prefix_keys)
            l_lam = totals_lambdas("live", row, prefix_keys)
            if s_lam is None or l_lam is None:
                continue
            p_s = prob_over_line(s_lam[0], s_lam[1], TOTALS_LINE)
            p_l = prob_over_line(l_lam[0], l_lam[1], TOTALS_LINE)
            b_s = brier_binary(p_s, hit)
            b_l = brier_binary(p_l, hit)
            ll_s = log_loss_binary(p_s, hit)
            ll_l = log_loss_binary(p_l, hit)

            # BTTS + signed goal bias: same lambda pair, independent diagnostic.
            btts_hit = 1 if (parsed[0] > 0 and parsed[1] > 0) else 0
            p_btts_s = prob_btts_yes(s_lam[0], s_lam[1])
            p_btts_l = prob_btts_yes(l_lam[0], l_lam[1])
            btts_b_s = brier_binary(p_btts_s, btts_hit)
            btts_b_l = brier_binary(p_btts_l, btts_hit)
            if btts_b_s is not None and btts_b_l is not None:
                btts_brier_shadow.append(btts_b_s)
                btts_brier_live.append(btts_b_l)
                btts_diffs.append(btts_b_s - btts_b_l)
            bias_s = signed_goal_bias(s_lam[0], s_lam[1], total_goals)
            bias_l = signed_goal_bias(l_lam[0], l_lam[1], total_goals)
            if bias_s is not None:
                bias_shadow.append(bias_s)
            if bias_l is not None:
                bias_live.append(bias_l)

        if None in (b_s, b_l, ll_s, ll_l):
            continue
        brier_shadow.append(b_s)
        brier_live.append(b_l)
        ll_shadow.append(ll_s)
        ll_live.append(ll_l)
        brier_diffs.append(b_s - b_l)
        ll_diffs.append(ll_s - ll_l)
        stage = _stage_for(str(result.get("date") or ""))
        split[stage]["brier"].append(b_s - b_l)
        split[stage]["logloss"].append(ll_s - ll_l)

    n = len(brier_diffs)
    b_lo, b_hi = bootstrap_ci(brier_diffs)
    ll_lo, ll_hi = bootstrap_ci(ll_diffs)
    out: Dict[str, Any] = {
        "family": shadow_family,
        "market": market,
        "n": n,
        "brier_shadow": _mean(brier_shadow),
        "brier_live": _mean(brier_live),
        "brier_diff": _mean(brier_diffs),
        "brier_ci_lo": b_lo,
        "brier_ci_hi": b_hi,
        "logloss_shadow": _mean(ll_shadow),
        "logloss_live": _mean(ll_live),
        "logloss_diff": _mean(ll_diffs),
        "logloss_ci_lo": ll_lo,
        "logloss_ci_hi": ll_hi,
        "split": {
            stage: {
                "n": len(split[stage]["brier"]),
                "brier_diff": _mean(split[stage]["brier"]),
                "logloss_diff": _mean(split[stage]["logloss"]),
            }
            for stage in ("group", "knockout")
        },
        "decision": decide(n, b_lo, b_hi, ll_lo, ll_hi),
    }
    if market == "totals":
        btts_lo, btts_hi = bootstrap_ci(btts_diffs)
        out["btts"] = {
            "n": len(btts_diffs),
            "brier_shadow": _mean(btts_brier_shadow),
            "brier_live": _mean(btts_brier_live),
            "brier_diff": _mean(btts_diffs),
            "brier_ci_lo": btts_lo,
            "brier_ci_hi": btts_hi,
        }
        out["goal_bias"] = {
            "n_shadow": len(bias_shadow),
            "mean_shadow": _mean(bias_shadow),
            "n_live": len(bias_live),
            "mean_live": _mean(bias_live),
        }
    return out


def decide(n: int,
           brier_lo: Optional[float], brier_hi: Optional[float],
           ll_lo: Optional[float], ll_hi: Optional[float]) -> str:
    """Decision string from the paired CIs.

    Lower is better -> a shadow that beats live has a NEGATIVE diff. We require
    BOTH Brier and log-loss CIs to agree and to exclude 0 (a strict gate: a
    shadow only promotes if it wins on both losses with the CIs clear of zero),
    at ``n >= DECISION_MIN_N``.

    * ``PROMOTE-candidate`` — both CIs entirely below 0 (shadow beats live).
    * ``KILL-candidate``    — both CIs entirely above 0 (shadow loses to live).
    * ``COLLECTING n=<x>/<gate>`` — otherwise (under the n gate, or CIs cross 0,
      or the two metrics disagree).
    """
    if n < DECISION_MIN_N or None in (brier_lo, brier_hi, ll_lo, ll_hi):
        return "COLLECTING n=%d/%d" % (n, DECISION_MIN_N)
    both_below = brier_hi < 0.0 and ll_hi < 0.0
    both_above = brier_lo > 0.0 and ll_lo > 0.0
    if both_below:
        return "PROMOTE-candidate"
    if both_above:
        return "KILL-candidate"
    return "COLLECTING n=%d/%d" % (n, DECISION_MIN_N)


# ---------------------------------------------------------------------------
# Top-level scoreboard.
# ---------------------------------------------------------------------------


def build_scoreboard(
    log_rows: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    now_utc: str,
) -> Dict[str, Any]:
    """Full scoreboard payload: one row per shadow family + a market reference.

    *log_rows* are the raw model-prediction log lines; *results* the realised
    fixtures (``{fixture, score, outcome, date, kickoff_utc}``). Every family is
    scored on the SAME deduped fixture set for its market so the pairing is
    honest. ``now_utc`` is caller-supplied (no clock reads here) so the output
    is reproducible.

    Goal-lambda ("totals") shadow families are DISCOVERED dynamically from
    *log_rows* (see :func:`discover_lambda_prefixes`) rather than a hardcoded
    list — any current or future ``<prefix>_lambda_home/away`` dual-write
    (``gb``, ``tl``, or a family not yet invented) is judged automatically, and
    a family absent from the log entirely never appears in the output (no
    empty fabricated rows, per the "no-shadow-rows -> clean empty scoreboard"
    contract).
    """
    matched: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for result in results:
        row = dedup_pre_kickoff(log_rows, result)
        if row is not None:
            matched.append((row, dict(result)))

    prefix_keys = discover_lambda_prefixes(log_rows)
    totals_families = sorted(prefix_keys.keys())

    rows_out: List[Dict[str, Any]] = []
    for family in ONEX2_SHADOWS:
        rows_out.append(_paired_scores(matched, family, "1x2"))
    for family in totals_families:
        rows_out.append(_paired_scores(matched, family, "totals", prefix_keys))

    return {
        "meta": {
            "generated": now_utc,
            "matched_fixtures": len(matched),
            "total_results": len(results),
            "decision_min_n": DECISION_MIN_N,
            "bootstrap": DEFAULT_BOOTSTRAP,
            "ci": DEFAULT_CI,
            "totals_line": TOTALS_LINE,
            "knockout_start_date": KNOCKOUT_START_DATE,
            "totals_shadow_families": totals_families,
            "note": (
                "Lower is better; diff = family - live, negative means the "
                "family beat the deployed (live ``model``) blend. Since the "
                "2026-07-09 shrink promotion the live line IS the shrunk blend, "
                "so ``raw`` scores the ex-live (pre-shrink) blend against it "
                "(positive raw brier_diff => the promotion helped) and ``shrink`` "
                "recomputes the shrunk line from the RAW model (``model_raw``, "
                "or ``model`` on pre-promotion rows). 1X2 families "
                "(raw/mw90/shrink) are recomputed over ALL settled fixtures; "
                "totals shadow families (%s, discovered from "
                "<prefix>_lambda_home/away keys present in the log) score a "
                "total-goals O/%.1f market plus a BTTS Brier and mean signed "
                "goal-lambda bias, only where the lambdas were logged. "
                "Group/knockout split is by kickoff date vs %s (heuristic; no "
                "stage field in results). This scorer is read-only reporting — "
                "it never itself feeds live pricing/sizing/selection."
            ) % (", ".join(totals_families) or "none", TOTALS_LINE, KNOCKOUT_START_DATE),
        },
        "shadows": rows_out,
    }
