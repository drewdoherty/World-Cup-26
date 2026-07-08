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
* the goal-lambda shadows (``gb`` / ``tl``) settle a *totals* market (total
  goals over/under a line) — they are scored on total-goals log-loss/Brier only
  where the row carries the lambdas AND the realised score is known, and they
  are kept in a SEPARATE market family from the 1X2 variants (never mixed);
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


def shadow_1x2(family: str, row: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """1X2 triple for a shadow *family*, recomputed from the row's components.

    ``mw90`` / ``shrink`` are recomputed (so historical rows predating the
    dual-write are still scored); ``market`` and ``live`` are read straight from
    the row. Returns ``None`` when the required component triples are absent.
    """
    if family == "live":
        return _live_1x2(row)
    if family == "market":
        return _market_1x2(row)
    model, elo, dc, mkt = (
        row.get("model"), row.get("elo"), row.get("dc"), row.get("market")
    )
    if family == "mw90":
        return _mw90_triple(elo, dc, mkt) if _valid_triple(mkt) else None
    if family == "shrink":
        return _shrink_triple(model, mkt) if _valid_triple(mkt) else None
    return None


def totals_lambdas(family: str, row: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """``(lambda_home, lambda_away)`` for a goal-lambda shadow family, or ``None``.

    ``live`` uses the deployed DC lambdas (``lambda_home`` / ``lambda_away``);
    ``gb`` the F7 goal-blend shadow (``gb_lambda_*``); ``tl`` the totals-prior
    blend (``tl_lambda_blend_*``). Missing lambdas -> ``None`` (row skipped).
    """
    keymap = {
        "live": ("lambda_home", "lambda_away"),
        "gb": ("gb_lambda_home", "gb_lambda_away"),
        "tl": ("tl_lambda_blend_home", "tl_lambda_blend_away"),
    }
    keys = keymap.get(family)
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
ONEX2_SHADOWS = ("mw90", "shrink", "market")
# Goal-lambda shadow families, scored on a total-goals over/under market.
TOTALS_SHADOWS = ("gb", "tl")
TOTALS_LINE = 2.5


def _paired_scores(
    matched: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
    shadow_family: str,
    market: str,
) -> Dict[str, Any]:
    """Paired Brier/log-loss of *shadow_family* vs the live baseline.

    *matched* is a list of ``(row, result)`` pairs (already deduped to one per
    fixture). *market* is ``"1x2"`` or ``"totals"`` and selects both the
    baseline and the metric. Only fixtures where BOTH the shadow and the live
    baseline produce a probability are counted (a true paired comparison).
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
            s_lam = totals_lambdas(shadow_family, row)
            l_lam = totals_lambdas("live", row)
            if s_lam is None or l_lam is None:
                continue
            p_s = prob_over_line(s_lam[0], s_lam[1], TOTALS_LINE)
            p_l = prob_over_line(l_lam[0], l_lam[1], TOTALS_LINE)
            b_s = brier_binary(p_s, hit)
            b_l = brier_binary(p_l, hit)
            ll_s = log_loss_binary(p_s, hit)
            ll_l = log_loss_binary(p_l, hit)

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
    return {
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
    """
    matched: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for result in results:
        row = dedup_pre_kickoff(log_rows, result)
        if row is not None:
            matched.append((row, dict(result)))

    rows_out: List[Dict[str, Any]] = []
    for family in ONEX2_SHADOWS:
        rows_out.append(_paired_scores(matched, family, "1x2"))
    for family in TOTALS_SHADOWS:
        rows_out.append(_paired_scores(matched, family, "totals"))

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
            "note": (
                "SHADOW-ONLY. Lower is better; diff = shadow - live, negative "
                "means the shadow beat the deployed blend. 1X2 shadows "
                "(mw90/shrink) are recomputed over ALL settled fixtures; "
                "totals shadows (gb/tl) score a total-goals O/%.1f market only "
                "where the lambdas were logged. Group/knockout split is by "
                "kickoff date vs %s (heuristic; no stage field in results). "
                "Nothing here feeds live pricing/sizing/selection."
            ) % (TOTALS_LINE, KNOCKOUT_START_DATE),
        },
        "shadows": rows_out,
    }
