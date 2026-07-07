"""Totals-market-implied total-goals prior, blended with the DC lambda (SHADOW).

Status: SHADOW-ONLY, per ``CLAUDE.md``'s "model changes ship SHADOW-FIRST"
standing rule. This module computes a market-implied total-goals expectation
from the Over/Under totals ladder and a credibility-shrinkage blend against the
deployed Dixon-Coles ``lambda_total`` (``lambda_home + lambda_away``). It is
**not wired into pricing or sizing** in this change; it only feeds the
prediction-log shadow columns (mirrors the F7 ``gb_lambda_*`` pattern in
:mod:`wca.models.goalblend` / :mod:`wca.modelpreds`) so a later, separate PR can
graduate it once there is an out-of-sample CLV comparison.

Motivation / prior art
-----------------------
``docs/research/xg_ag_ko_calibration.md`` §4 ranks exactly this idea — "(d)
Market-totals-implied level per fixture (devig O/U ladder -> lambda)" — as
RANK 3, "deploy as DIAGNOSTIC column only", with the explicit circularity
warning: if the model's level were *set from* the totals market, model-vs-market
totals edges would be zero by construction, cannibalising the very market this
system trades (``wca-oddsapi-utilization`` memory: totals O/U devig is the
biggest unused edge — TheOddsAPI already ships totals+BTTS odds but only h2h is
read anywhere). This module is the first implementation of that recommendation:
it logs the market-implied lambda and a blended lambda **side by side** with
the model-only lambda, and explicitly does NOT feed back into any totals price
or size.

Method
------
1. **De-vig the O/U quote.** Given decimal odds for Over/Under at a line
   ``k + 0.5`` (football totals lines are always half-integers, so no push
   case), remove the bookmaker's margin with :func:`wca.markets.devig.devig`
   (default ``method="multiplicative"`` — the same default used across
   :mod:`wca.intel.normalise`) to get a fair ``P(Over k+0.5)``.

2. **Invert to an implied total-goals lambda.** Assume total goals ``T = H + A``
   is Poisson-distributed with mean ``lambda_total`` (the standard total-goals
   convention used by the totals market itself, and by ``docs/research`` §5's
   own P(Over 2.5) tables) — i.e. we deliberately ignore the small Dixon-Coles
   low-score ``tau`` correlation correction here, since totals books do not
   price it either and this keeps the inversion well-defined and invertible in
   closed form via a 1-D monotone root-solve:
   ``P(Over k+0.5) = 1 - PoissonCDF(k; lambda) = sf(k, lambda)``.
   ``sf(k, lambda)`` is strictly increasing in ``lambda`` for fixed ``k`` (a
   higher goal expectation shifts mass above the line), so the inverse exists
   and is unique; found here by bisection over ``lambda in [0, 30]``. When
   multiple lines are available (e.g. 1.5, 2.5, 3.5) each yields an independent
   lambda estimate; :func:`market_implied_lambda` averages them (equal weight —
   no line has a documented liquidity edge over another in this feed) after
   devigging each independently.

3. **Blend with the model lambda.** :func:`blend_lambda_total` is a
   James-Stein / credibility-style convex blend,
   ``lambda_blend = (1 - w) * lambda_model + w * lambda_market``, with
   ``w = n_market / (n_market + k)`` where ``n_market`` is the number of
   *distinct bookmaker quotes* backing the market-implied estimate (more quotes
   -> more trust in the market side) and ``k`` is a shrinkage constant
   (default :data:`DEFAULT_CREDIBILITY_K`). This reuses the exact credibility
   form already established and documented in
   :func:`wca.models.goalblend.credibility_weight` (``w = n / (n + k)``,
   James-Stein / credibility shrinkage) rather than inventing a new blend
   rule — same shrinkage family, different evidence source (market quotes vs
   recent-match count). The per-fixture home/away split of the blended total is
   apportioned using the model's own home/away *share* (``lambda_home_model /
   lambda_total_model``), since the totals market only prices the sum, not the
   split; see :func:`blend_lambda_home_away`.

No fabrication: when the totals ladder for a fixture has no complete
(Over, Under) pair at any line, :func:`market_implied_lambda` returns ``None``
rather than guessing, and the blend degenerates to the model-only lambda
(``w`` effectively 0 because ``n_market = 0``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy.stats import poisson

from wca.markets import devig

#: Credibility shrinkage constant `k` in `w = n_market / (n_market + k)`.
#: Chosen so that ~3 independent bookmaker quotes on the same fixture carry
#: about a quarter of the blend weight, and it takes ~9 quotes to reach half
#: weight — deliberately conservative given this is a first, unvalidated
#: shadow pass (mirrors the "deliberately large" rationale documented for
#: ``DEFAULT_CREDIBILITY_K`` in wca.models.goalblend).
DEFAULT_CREDIBILITY_K: float = 9.0

#: Numerical bracket ceiling for the lambda root-solve (a football match total
#: of 30 goals is already many multiples beyond any observed fixture).
_MAX_LAMBDA_BRACKET: float = 30.0


def devig_over_prob(
    over_odds: float, under_odds: float, *, method: str = "multiplicative"
) -> float:
    """Fair ``P(Over line)`` from a two-way Over/Under decimal-odds quote.

    Thin, explicit wrapper over :func:`wca.markets.devig.devig` (same default
    method used across :mod:`wca.intel.normalise`) so callers of this module
    don't need to know the outcome ordering convention.
    """
    probs = devig.devig([float(over_odds), float(under_odds)], method=method)
    return float(probs[0])


def implied_lambda_from_over_prob(
    p_over: float, line: float, *, tol: float = 1e-10, max_iter: int = 200
) -> float:
    """Invert a fair ``P(Over line)`` into a Poisson total-goals ``lambda``.

    ``line`` is a half-integer (e.g. ``2.5``); ``k = floor(line)`` goals or
    fewer is Under, ``k + 1`` or more is Over, so
    ``P(Over line) = 1 - PoissonCDF(k; lambda) = sf(k, lambda)``.

    ``sf(k, lambda)`` is strictly increasing in ``lambda`` for fixed ``k`` (a
    higher goal expectation shifts mass above the line), so the inverse exists
    and is unique; found here by bisection over ``lambda in [0, 30]``. Raises
    ``ValueError`` for a ``p_over`` outside ``(0, 1)`` — an exact 0 or 1 has no
    finite-lambda solution and should never occur from a de-vigged market
    quote.
    """
    p = float(p_over)
    if not (0.0 < p < 1.0):
        raise ValueError("p_over must be strictly between 0 and 1, got %r" % p_over)
    k = int(np.floor(float(line)))
    if k < 0:
        raise ValueError("line must be non-negative, got %r" % line)

    def sf(lam: float) -> float:
        return float(poisson.sf(k, lam))

    lo, hi = 0.0, _MAX_LAMBDA_BRACKET
    # sf(0) == 0 for k >= 0 (Poisson(0) puts all mass at 0 <= k), sf(hi) -> 1.
    if sf(hi) < p:
        # Pathological (line implausibly low relative to p); grow the bracket.
        while sf(hi) < p and hi < 1e6:
            hi *= 2.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if sf(mid) < p:
            lo = mid
        else:
            hi = mid
        if (hi - lo) <= tol:
            break
    return 0.5 * (lo + hi)


@dataclass
class TotalsQuote:
    """One venue's Over/Under decimal-odds quote at one line for one fixture."""

    line: float
    over_odds: float
    under_odds: float
    venue: Optional[str] = None


def market_implied_lambda(
    quotes: Sequence[TotalsQuote], *, method: str = "multiplicative"
) -> Optional[Tuple[float, int]]:
    """Market-implied total-goals ``lambda`` from one or more O/U quotes.

    Each quote is de-vigged independently, inverted to a per-quote implied
    lambda via :func:`implied_lambda_from_over_prob`, then equal-weight
    averaged across quotes (no documented liquidity ranking across lines or
    bookmakers in this feed, so equal weight is the simplest defensible
    choice — flagged here rather than silently assumed).

    Returns ``(lambda_market, n_quotes)``, or ``None`` if ``quotes`` is empty
    or every quote fails to devig (e.g. non-finite odds) — never fabricates a
    value from zero evidence.
    """
    lambdas: List[float] = []
    for q in quotes:
        try:
            p_over = devig_over_prob(q.over_odds, q.under_odds, method=method)
            lam = implied_lambda_from_over_prob(p_over, q.line)
        except (ValueError, ZeroDivisionError):
            continue
        if np.isfinite(lam) and lam >= 0:
            lambdas.append(lam)
    if not lambdas:
        return None
    return float(np.mean(lambdas)), len(lambdas)


def credibility_weight(n_market: float, k: float = DEFAULT_CREDIBILITY_K) -> float:
    """Credibility weight ``w = n / (n + k)`` on the market-implied lambda.

    Identical shrinkage form to :func:`wca.models.goalblend.credibility_weight`
    (James-Stein / credibility blending) — reused deliberately rather than
    inventing a second blend convention in the same codebase. ``n_market`` here
    is the count of independent bookmaker/line quotes backing the market
    estimate (see :func:`market_implied_lambda`), not a recent-match count.
    """
    n = float(n_market)
    kk = float(k)
    if kk <= 0:
        raise ValueError("credibility_k must be positive")
    if n < 0:
        raise ValueError("n_market must be non-negative")
    if n == 0:
        return 0.0
    return n / (n + kk)


def blend_lambda_total(
    lambda_model_total: float,
    lambda_market_total: Optional[float],
    n_market: int,
    *,
    k: float = DEFAULT_CREDIBILITY_K,
) -> float:
    """Convex-blend the model's total lambda with the market-implied total.

    ``lambda_blend = (1 - w) * lambda_model_total + w * lambda_market_total``,
    ``w = credibility_weight(n_market, k)``. With no market evidence
    (``lambda_market_total is None`` or ``n_market == 0``) returns the model
    lambda unchanged (``w = 0``).
    """
    if lambda_market_total is None or n_market <= 0:
        return float(lambda_model_total)
    w = credibility_weight(n_market, k)
    return (1.0 - w) * float(lambda_model_total) + w * float(lambda_market_total)


def blend_lambda_home_away(
    lambda_home_model: float,
    lambda_away_model: float,
    lambda_blend_total: float,
) -> Tuple[float, float]:
    """Apportion a blended total lambda into home/away using the model's split.

    The totals market prices only the *sum*; it carries no information about
    the home/away split. We preserve the model's own implied share
    ``lambda_home_model / (lambda_home_model + lambda_away_model)`` and scale
    both legs so they sum to ``lambda_blend_total`` exactly. Falls back to an
    even 50/50 split if the model total is zero (degenerate edge case, should
    not occur for a fitted DC model).
    """
    total_model = float(lambda_home_model) + float(lambda_away_model)
    if total_model <= 0:
        return lambda_blend_total / 2.0, lambda_blend_total / 2.0
    share_home = float(lambda_home_model) / total_model
    return lambda_blend_total * share_home, lambda_blend_total * (1.0 - share_home)


@dataclass
class TotalsPriorResult:
    """Full shadow result for one fixture: market lambda + blended lambda."""

    lambda_market_total: Optional[float]
    n_market_quotes: int
    lambda_blend_total: float
    lambda_blend_home: float
    lambda_blend_away: float
    weight_market: float


def compute_totals_prior(
    lambda_home_model: float,
    lambda_away_model: float,
    quotes: Sequence[TotalsQuote],
    *,
    method: str = "multiplicative",
    k: float = DEFAULT_CREDIBILITY_K,
) -> TotalsPriorResult:
    """End-to-end shadow computation for one fixture: quotes -> blended lambda.

    Convenience wrapper chaining :func:`market_implied_lambda`,
    :func:`blend_lambda_total` and :func:`blend_lambda_home_away`. Always
    returns a result (falls back to the model-only lambda, ``weight_market=0``,
    when there is no usable totals quote) so callers never need a special-case
    branch.
    """
    lambda_model_total = float(lambda_home_model) + float(lambda_away_model)
    market = market_implied_lambda(quotes, method=method)
    if market is None:
        lambda_market_total: Optional[float] = None
        n_quotes = 0
    else:
        lambda_market_total, n_quotes = market

    lambda_blend_total = blend_lambda_total(
        lambda_model_total, lambda_market_total, n_quotes, k=k
    )
    lam_h, lam_a = blend_lambda_home_away(
        lambda_home_model, lambda_away_model, lambda_blend_total
    )
    w = credibility_weight(n_quotes, k) if n_quotes > 0 else 0.0
    return TotalsPriorResult(
        lambda_market_total=lambda_market_total,
        n_market_quotes=n_quotes,
        lambda_blend_total=lambda_blend_total,
        lambda_blend_home=lam_h,
        lambda_blend_away=lam_a,
        weight_market=w,
    )


def load_totals_quotes_by_match(
    db_path: str, *, lookback_hours: float = 72.0
) -> Dict[str, List[TotalsQuote]]:
    """Latest totals quote per (match, venue, line) from ``odds_snapshots``.

    Read-only (``PRAGMA query_only=ON``, per ``CLAUDE.md``'s data-discipline
    rule — this module never writes to the ledger DB). Looks back
    ``lookback_hours`` from "now" so a stale/quiet DB doesn't silently return
    ancient quotes as if they were current; for each ``(match_id, venue,
    line)`` only the most recent row per outcome (Over/Under) is kept, then
    paired via :func:`quotes_from_odds_rows`.

    Returns ``{match_id: [TotalsQuote, ...]}``; a match with no complete O/U
    pair in the lookback window is simply absent (never fabricated).
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    uri = "file:%s?mode=ro" % db_path
    con = sqlite3.connect(uri, uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        cur = con.execute(
            "SELECT ts_utc, match_id, selection, decimal_odds, raw "
            "FROM odds_snapshots WHERE market='totals' AND ts_utc >= ? "
            "ORDER BY ts_utc",
            (since,),
        )
        rows = [
            {"ts_utc": ts, "match_id": mid, "market": "totals",
             "selection": sel, "decimal_odds": dec, "raw": raw}
            for ts, mid, sel, dec, raw in cur
        ]
    finally:
        con.close()

    # Flatten raw JSON (bookmaker_key / outcome_name / outcome_point) and keep
    # only the LATEST row per (match_id, bookmaker, line, outcome) so a stale
    # earlier quote never outvotes a fresher one for the same cell.
    import json as _json

    latest: Dict[Tuple[str, object, object, str], dict] = {}
    for r in rows:
        raw = r.get("raw")
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except ValueError:
                raw = {}
        raw = raw or {}
        outcome = str(raw.get("outcome_name", r.get("selection", ""))).strip().lower()
        key = (
            r["match_id"],
            raw.get("bookmaker_key"),
            raw.get("outcome_point"),
            "over" if outcome.startswith("over") else "under",
        )
        latest[key] = {
            "market": "totals",
            "match_id": r["match_id"],
            "bookmaker_key": raw.get("bookmaker_key"),
            "outcome_name": raw.get("outcome_name", r.get("selection")),
            "outcome_point": raw.get("outcome_point"),
            "decimal_odds": r["decimal_odds"],
        }

    by_match: Dict[str, List[dict]] = {}
    for (match_id, _venue, _line, _outcome), row in latest.items():
        by_match.setdefault(match_id, []).append(row)

    return {
        match_id: quotes_from_odds_rows(flat_rows)
        for match_id, flat_rows in by_match.items()
    }


def quotes_from_odds_rows(rows: Sequence[Mapping[str, object]]) -> List[TotalsQuote]:
    """Group flat ``totals``-market rows (the ``odds_snapshots`` row shape, or
    the parsed ``raw`` dict within them) into :class:`TotalsQuote` objects.

    Expects rows shaped like the flattened output of
    :func:`wca.data.theoddsapi._parse_events` / the ``raw`` JSON column of
    ``odds_snapshots``: each row has ``market`` (must be ``"totals"``),
    ``outcome_name`` (``"Over"`` / ``"Under"``), ``outcome_point`` (the line),
    ``decimal_odds``, and optionally ``bookmaker_key`` (grouping key so
    Over/Under from the SAME bookmaker+line are paired; rows without a
    bookmaker key fall back to grouping by line alone). Incomplete pairs
    (an Over with no matching Under, or vice versa) are silently dropped — no
    one-sided quote is turned into a probability.
    """
    groups: Dict[Tuple[object, object], Dict[str, float]] = {}
    for r in rows:
        if str(r.get("market", "")).lower() != "totals":
            continue
        line = r.get("outcome_point")
        if line is None:
            continue
        venue = r.get("bookmaker_key", r.get("venue"))
        key = (venue, line)
        name = str(r.get("outcome_name", "")).strip().lower()
        odds = r.get("decimal_odds")
        if odds is None:
            continue
        if name.startswith("over"):
            groups.setdefault(key, {})["over"] = float(odds)  # type: ignore[arg-type]
        elif name.startswith("under"):
            groups.setdefault(key, {})["under"] = float(odds)  # type: ignore[arg-type]

    out: List[TotalsQuote] = []
    for (venue, line), pair in groups.items():
        if "over" in pair and "under" in pair:
            out.append(
                TotalsQuote(
                    line=float(line),  # type: ignore[arg-type]
                    over_odds=pair["over"],
                    under_odds=pair["under"],
                    venue=str(venue) if venue is not None else None,
                )
            )
    return out
