"""Correlation-aware whole-book exposure (Phase-2 Wave-1).

The legacy :func:`wca.exposure._portfolio_scenarios` enumerates only the
per-fixture **1X2** outcomes (home / draw / away). Two correlated bets on the
*same* fixture — e.g. "Home Win" plus "Over 2.5", which both cash on a 3-0 — are
therefore never settled from a single shared state, so their joint P&L is wrong
(the two legs are implicitly treated as if they could be decided independently).

This module fixes that by settling **every same-fixture bet from one shared
scoreline**. For each fixture we enumerate the scoreline matrix (rows = home
goals, cols = away goals) reconstructed from the persisted goal-expectation
lambdas, and for each cell accumulate ``prob * pnl_of_all_same_fixture_bets``.
That yields the *exact* within-fixture-correlated P&L distribution across 1X2,
Over/Under, BTTS, correct-score and team-total bets at once.

Different fixtures are independent (different games), so the slate distribution
is the convolution of the per-fixture P&L distributions. We then expose, per
fixture, the net worst-case correlated loss and a 5%-of-bankroll cap check
(treating *all* same-fixture outcomes as one exposure).

Design notes / approximation
----------------------------
* The reconstructed matrix is an **independent-Poisson** grid (the same prior
  shape :mod:`wca.models.scores` uses), *not* the full fitted Dixon-Coles matrix
  with its low-score ``tau`` correction. We do not refit a model inside the
  exposure layer (it must stay deterministic and IO-free), and the persisted
  lambdas are the compact sufficient statistic available at runtime. The tau
  correction only reshuffles mass among the four lowest-score cells (0-0 / 1-0 /
  0-1 / 1-1); the 1X2 / O/U / BTTS / correct-score *settlement* of any given
  scoreline is identical either way, and the probability weighting differs only
  at the margin. When the exact reconciled DC matrix is wanted it can be passed
  in directly via ``matrix=``.
* Everything here is deterministic and pure: the matrix is reconstructed from
  the passed-in lambdas, no clock / IO / RNG. Cross-fixture combination is an
  exact convolution (no Monte-Carlo needed for the per-fixture-independent
  structure), so the feed is reproducible for identical inputs.

Free bets / promo stakes (``source == 'offer'``) are stake-not-returned: a
losing scoreline costs £0, only the profit is at stake. Real-money bets lose
their stake on a losing scoreline.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .models.scores import (
    btts_from_matrix,
    over_under_from_matrix,
)

# Effective combined bankroll (£1,500 sportsbook pool + ~$1,995 ≈ £1,500
# on-chain) used for the per-underlying exposure cap.
#
# This is the COMBINED, cross-venue, cross-currency pool — capital is fungible
# (£<->$, freely transferable between accounts/venues), so the bankroll is never
# the balance in any single account/wallet/app. See the bankroll-fungibility rule
# in wca.card (near DEFAULT_ACTUAL_CAPITAL_GBP) and docs/policy/bankroll.md.
DEFAULT_BANKROLL = 3000.0

# Max NET downside per correlated underlying (one fixture = one exposure).
CORRELATED_CAP_FRACTION = 0.05

# Default truncation for the reconstructed scoreline grid. 10 goals per side
# (121 cells) captures effectively all realistic football mass.
DEFAULT_MAX_GOALS = 10

# Probabilities below this are treated as numerical zero when trimming the grid.
_EPS = 1e-12

# --- text parsing (mirrors wca.boosts / wca.exposure conventions) ----------
_OVER_RE = re.compile(r"\bover\b", re.IGNORECASE)
_UNDER_RE = re.compile(r"\bunder\b", re.IGNORECASE)
_YES_RE = re.compile(r"\byes\b", re.IGNORECASE)
_NO_RE = re.compile(r"\bno\b", re.IGNORECASE)
_SCORE_RE = re.compile(r"(\d+)\s*[-:]\s*(\d+)")
_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)")

_RESULT_MARKETS = {
    "full-time result", "match odds", "match winner", "match result",
    "h2h", "1x2", "moneyline", "money line",
}

#: Bare over/under market names that carry no "goals" word in the market label.
#: ``wca.accas`` and the ledger use ``"totals"`` as the canonical match-totals
#: key (selection like "Over 2.5"), so match the market name directly.
_TOTALS_MARKETS = {
    "totals", "total", "match total", "match totals", "goals",
}


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


# ---------------------------------------------------------------------------
# Scoreline matrix.
# ---------------------------------------------------------------------------


def scoreline_matrix(
    lam_h: float, lam_a: float, max_goals: int = DEFAULT_MAX_GOALS
) -> np.ndarray:
    """Independent-Poisson score-probability matrix ``P[h, a]``.

    ``P[h, a]`` is the probability of the scoreline *home=h, away=a* for goal
    means ``lam_h`` / ``lam_a``, truncated at ``max_goals`` per side and
    renormalised to sum to one. Reuses the same truncated-Poisson construction
    as :mod:`wca.models.scores` (no scipy dependency, stable recurrence). This
    omits the Dixon-Coles ``tau`` low-score correction — see the module
    docstring for why that is acceptable for exposure settlement.

    Non-finite / negative means are coerced to ``0.0`` so a bad lambda never
    raises here (the caller falls back to legacy behaviour upstream).
    """
    n = int(max_goals) + 1

    def _pmf(lam: float) -> np.ndarray:
        lam = float(lam)
        if not np.isfinite(lam) or lam < 0.0:
            lam = 0.0
        out = np.empty(n, dtype=float)
        out[0] = np.exp(-lam)
        for k in range(1, n):
            out[k] = out[k - 1] * lam / k
        return out

    mat = np.outer(_pmf(lam_h), _pmf(lam_a))
    total = float(mat.sum())
    if total > 0:
        mat = mat / total
    return mat


# ---------------------------------------------------------------------------
# Per-bet settlement on a concrete scoreline.
# ---------------------------------------------------------------------------


def _loss(bet: Dict[str, Any]) -> float:
    """Cost when a bet loses: £0 for a free bet, the stake for a real bet."""
    if bool(bet.get("free")):
        return 0.0
    return float(bet.get("stake") or 0.0)


def _win(bet: Dict[str, Any]) -> float:
    """Profit when a bet wins (stake * (odds - 1))."""
    profit = bet.get("profit")
    if profit is not None:
        return float(profit)
    return float(bet.get("stake") or 0.0) * (float(bet.get("odds") or 1.0) - 1.0)


def _classify(market: str, selection: str) -> str:
    m, s = _norm(market), _norm(selection)
    blob = "%s %s" % (m, s)
    # Correct score: an explicit "Correct Score" market, or a bare scoreline
    # selection like "2-1" / "2:1" (checked before over/under so the digits
    # aren't misread as a goals line).
    sel_stripped = (selection or "").strip()
    if "correct score" in m or _SCORE_RE.fullmatch(sel_stripped):
        return "correct_score"
    if "btts" in blob or "both teams" in blob:
        return "btts"
    if any(p in blob for p in ("corner", "card", "booking", "shot", "player",
                               "scorer", "assist")):
        return "other"
    # Team total goals (e.g. "Brazil Over 1.5") — a totals line tied to one team.
    if ("team total" in blob or "team goals" in blob) and (
        _OVER_RE.search(blob) or _UNDER_RE.search(blob)
    ):
        return "team_total"
    if "over/under" in blob or "total goals" in blob or (
        "total" in blob and "goal" in blob
    ):
        return "over_under"
    if "goals" in blob and (_OVER_RE.search(blob) or _UNDER_RE.search(blob)):
        return "over_under"
    # Bare totals market (canonical accas/ledger key, e.g. market="totals",
    # selection="Over 2.5") — no "goals" word in the label, so key off the
    # market name when the selection states an over/under side.
    if m in _TOTALS_MARKETS and (_OVER_RE.search(s) or _UNDER_RE.search(s)):
        return "over_under"
    if m in _RESULT_MARKETS:
        return "result"
    return "other"


def _extract_line(market: str, selection: str) -> Optional[float]:
    for text in (selection, market):
        m = _LINE_RE.search(str(text or ""))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _ou_side(market: str, selection: str) -> Optional[bool]:
    """Resolve an over/under bet to ``True`` (over) / ``False`` (under) / None.

    The *selection* is authoritative — a market named "Over/Under 2.5 Goals"
    contains BOTH words, so reading the side off the combined blob would always
    fire "over". Only fall back to the market text when the selection states
    neither side.
    """
    sel = selection or ""
    sel_over = bool(_OVER_RE.search(sel))
    sel_under = bool(_UNDER_RE.search(sel))
    if sel_over and not sel_under:
        return True
    if sel_under and not sel_over:
        return False
    # Selection ambiguous (or empty): consult the market name.
    mkt = market or ""
    mkt_over = bool(_OVER_RE.search(mkt))
    mkt_under = bool(_UNDER_RE.search(mkt))
    if mkt_over and not mkt_under:
        return True
    if mkt_under and not mkt_over:
        return False
    return None


def _team_side(selection: str, home: str, away: str) -> Optional[str]:
    """Which side ('home'/'away') a team-total selection refers to, else None."""
    s = _norm(selection)
    if home and _norm(home) in s:
        return "home"
    if away and _norm(away) in s:
        return "away"
    return None


def settle_on_scoreline(
    bet: Dict[str, Any], home: str, away: str, sh: int, sa: int
) -> float:
    """Profit/loss of ``bet`` GIVEN the fixture finishes ``sh``-``sa``.

    Covers the market types actually carried in the ledger:

    * **1X2** (Full-time result / Match Odds / Match Winner / h2h) — selection
      is the winning team or "Draw"/"The Draw".
    * **Totals / Over-Under** — selection states over/under and a goals line.
      A bet *pushes* (returns £0, no win/loss) only on an integer line equal to
      the total; half-integer lines never push.
    * **BTTS** (yes / no).
    * **Correct Score** — selection like "2-1" (or "2:1").
    * **Team totals** — e.g. "Brazil Over 1.5" settles on that team's goals.

    Anything else (player props, corners, cards) is treated as *not settleable
    from a scoreline*; such a bet contributes ``0.0`` to the within-fixture
    distribution (it is independent of the scoreline) so it never distorts the
    correlated P&L. A winning result returns the bet's profit; a losing one
    returns ``-stake`` (or ``0`` for a free bet, stake-not-returned).
    """
    sh, sa = int(sh), int(sa)
    total = sh + sa
    market = str(bet.get("type") or bet.get("market") or "")
    selection = str(bet.get("selection") or bet.get("label") or "")
    kind = _classify(market, selection)

    def _settle(won: Optional[bool]) -> float:
        if won is None:  # push: stake returned, no P&L
            return 0.0
        return _win(bet) if won else -_loss(bet)

    if kind == "result":
        sel = _norm(selection)
        if sel in ("draw", "the draw"):
            return _settle(sh == sa)
        if _norm(home) and sel == _norm(home):
            return _settle(sh > sa)
        if _norm(away) and sel == _norm(away):
            return _settle(sh < sa)
        # Unmappable result selection -> independent of scoreline.
        return 0.0

    if kind == "over_under":
        line = _extract_line(market, selection)
        side_over = _ou_side(market, selection)
        if line is None or side_over is None:
            return 0.0
        if abs(total - line) < 1e-9:  # exact integer-line push
            return _settle(None)
        over = total > line
        return _settle(over if side_over else (not over))

    if kind == "team_total":
        side = _team_side(selection, home, away)
        line = _extract_line(market, selection)
        side_over = _ou_side(market, selection)
        if side is None or line is None or side_over is None:
            return 0.0
        team_goals = sh if side == "home" else sa
        if abs(team_goals - line) < 1e-9:
            return _settle(None)
        over = team_goals > line
        return _settle(over if side_over else (not over))

    if kind == "btts":
        both = (sh > 0) and (sa > 0)
        # "no" wins when not both scored; default to "yes" if side unstated.
        is_no = bool(_NO_RE.search(selection)) and not bool(_YES_RE.search(selection))
        return _settle((not both) if is_no else both)

    if kind == "correct_score":
        m = _SCORE_RE.search(selection)
        if m is None:
            return 0.0
        return _settle(sh == int(m.group(1)) and sa == int(m.group(2)))

    # Player props / corners / cards: not decided by the scoreline.
    return 0.0


# ---------------------------------------------------------------------------
# Per-fixture correlated P&L distribution.
# ---------------------------------------------------------------------------


def fixture_pnl_distribution(
    bets: List[Dict[str, Any]],
    home: str,
    away: str,
    lam_h: Optional[float] = None,
    lam_a: Optional[float] = None,
    matrix: Optional[np.ndarray] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> Optional[List[Tuple[float, float]]]:
    """``[(prob, total_pnl), ...]`` over the fixture's scorelines.

    For every scoreline cell the P&L is the **sum across all** same-fixture
    bets settled from that one shared scoreline (so 1X2 + O/U + BTTS + CS are
    jointly consistent). Returns one entry per distinct total-P&L value
    (probability-aggregated), sorted by P&L.

    Returns ``None`` when no scoreline matrix can be built (no lambdas and no
    explicit ``matrix``) so the caller falls back to legacy 1X2-only handling.
    """
    if matrix is None:
        if lam_h is None or lam_a is None:
            return None
        if not (np.isfinite(lam_h) and np.isfinite(lam_a)):
            return None
        matrix = scoreline_matrix(float(lam_h), float(lam_a), max_goals=max_goals)
    mat = np.asarray(matrix, dtype=float)
    nh, na = mat.shape

    agg: Dict[float, float] = {}
    for sh in range(nh):
        for sa in range(na):
            p = float(mat[sh, sa])
            if p <= _EPS:
                continue
            pnl = 0.0
            for bet in bets:
                pnl += settle_on_scoreline(bet, home, away, sh, sa)
            key = round(pnl, 6)
            agg[key] = agg.get(key, 0.0) + p
    return sorted(agg.items(), key=lambda kv: kv[0])


def _distribution_stats(
    dist: List[Tuple[float, float]]
) -> Dict[str, float]:
    """EV / best / worst / loss-prob summary of a ``[(pnl, prob), ...]`` dist."""
    if not dist:
        return {"ev": 0.0, "best": 0.0, "worst": 0.0,
                "p_loss": 0.0, "p_profit": 0.0}
    ev = sum(pnl * p for pnl, p in dist)
    best = max(pnl for pnl, _ in dist)
    worst = min(pnl for pnl, _ in dist)
    p_loss = sum(p for pnl, p in dist if pnl < -0.5)
    p_profit = sum(p for pnl, p in dist if pnl > 0.5)
    return {"ev": ev, "best": best, "worst": worst,
            "p_loss": p_loss, "p_profit": p_profit}


def convolve_distributions(
    dists: List[List[Tuple[float, float]]],
    round_to: int = 2,
) -> List[Tuple[float, float]]:
    """Exact convolution of independent per-fixture P&L distributions.

    Fixtures are independent (different games), so the slate P&L is the
    distribution of the *sum* of the per-fixture P&Ls. P&L values are rounded to
    ``round_to`` decimals when combining to keep the support size bounded
    (currency is pennies-resolution anyway). Deterministic — no RNG.
    """
    combined: Dict[float, float] = {0.0: 1.0}
    for dist in dists:
        if not dist:
            continue
        nxt: Dict[float, float] = {}
        for pnl0, p0 in combined.items():
            for pnl1, p1 in dist:
                key = round(pnl0 + pnl1, round_to)
                nxt[key] = nxt.get(key, 0.0) + p0 * p1
        combined = nxt
    return sorted(combined.items(), key=lambda kv: kv[0])


# ---------------------------------------------------------------------------
# Public: correlated-exposure feed section.
# ---------------------------------------------------------------------------


def build_correlated_exposure(
    fixture_bets: Dict[str, List[Dict[str, Any]]],
    lambdas: Dict[str, Tuple[float, float]],
    home_away: Dict[str, Tuple[str, str]],
    bankroll: float = DEFAULT_BANKROLL,
    cap_fraction: float = CORRELATED_CAP_FRACTION,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> Dict[str, Any]:
    """Correlated-exposure section for the exposure feed.

    Parameters
    ----------
    fixture_bets:
        ``{fixture: [bet, ...]}`` — every open bet that maps to that fixture,
        already in the flat dict form the exposure layer uses (keys: ``type`` or
        ``market``, ``selection`` or ``label``, ``stake``, ``profit``, ``free``,
        ``odds``).
    lambdas:
        ``{fixture: (lambda_home, lambda_away)}`` from the persisted predictions.
        Fixtures absent here (old data) get ``None`` cap fields and are flagged
        ``has_lambdas=False`` so the caller keeps legacy 1X2-only behaviour.
    home_away:
        ``{fixture: (home_team, away_team)}``.
    bankroll:
        Effective combined bankroll for the 5% cap (default £3000).
    cap_fraction:
        Net-downside cap as a fraction of bankroll (default 0.05).

    Returns
    -------
    dict
        ``{"bankroll", "cap_fraction", "cap_abs", "fixtures": [...],
        "n_over_exposed", "slate": {...}}`` — per fixture the correlated EV /
        best / worst / loss-prob, the cap check, and the convolved slate
        distribution summary across fixtures that *have* lambdas.
    """
    cap_abs = round(float(bankroll) * float(cap_fraction), 2)
    out_fixtures: List[Dict[str, Any]] = []
    slate_dists: List[List[Tuple[float, float]]] = []
    n_over = 0

    for fx in sorted(fixture_bets):
        bets = fixture_bets[fx]
        if not bets:
            continue
        home, away = home_away.get(fx, (fx, ""))
        lam = lambdas.get(fx)
        if lam is None or lam[0] is None or lam[1] is None:
            out_fixtures.append({
                "fixture": fx, "has_lambdas": False,
                "n_bets": len(bets),
                "note": "no lambdas persisted — legacy 1X2-only exposure used",
            })
            continue

        dist = fixture_pnl_distribution(
            bets, home, away, lam_h=lam[0], lam_a=lam[1], max_goals=max_goals
        )
        if dist is None:
            out_fixtures.append({
                "fixture": fx, "has_lambdas": False, "n_bets": len(bets),
                "note": "scoreline matrix unavailable — legacy exposure used",
            })
            continue

        # Distribution is keyed (pnl, prob); reuse the stats helper.
        stats = _distribution_stats(dist)
        slate_dists.append(dist)

        # Net worst-case correlated downside = the most negative P&L the joint
        # within-fixture distribution can produce (all same-fixture outcomes
        # already settled together, so this IS the one correlated exposure).
        worst = stats["worst"]
        downside = max(0.0, -worst)
        over = downside > cap_abs + 1e-9
        if over:
            n_over += 1
        out_fixtures.append({
            "fixture": fx,
            "has_lambdas": True,
            "n_bets": len(bets),
            "lambda_home": round(float(lam[0]), 4),
            "lambda_away": round(float(lam[1]), 4),
            "ev": round(stats["ev"], 2),
            "best_case": round(stats["best"], 2),
            "worst_case": round(worst, 2),
            "net_downside": round(downside, 2),
            "p_loss": round(stats["p_loss"], 4),
            "p_profit": round(stats["p_profit"], 4),
            "cap_abs": cap_abs,
            "over_exposed": bool(over),
            "headroom": round(cap_abs - downside, 2),
        })

    slate_dist = convolve_distributions(slate_dists)
    slate_stats = _distribution_stats(
        [(pnl, p) for pnl, p in slate_dist]
    )
    return {
        "bankroll": round(float(bankroll), 2),
        "cap_fraction": float(cap_fraction),
        "cap_abs": cap_abs,
        "n_over_exposed": n_over,
        "fixtures": out_fixtures,
        "slate": {
            "ev": round(slate_stats["ev"], 2),
            "best": round(slate_stats["best"], 2),
            "worst": round(slate_stats["worst"], 2),
            "p_loss": round(slate_stats["p_loss"], 4),
            "p_profit": round(slate_stats["p_profit"], 4),
            "n_states": len(slate_dist),
        },
    }
