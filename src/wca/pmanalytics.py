"""Polymarket analytics suite — calibration, term-structure, mark-to-market.

Three families of analytics over our model vs live Polymarket prices and our open
positions. The compute is **pure and network-free** so it is fully unit-testable;
the live fetches (Gamma events, CLOB price history, the two ledgers) live in
``scripts/wca_pm_analytics_suite.py``.

CRITICAL DOMAIN RULE enforced throughout: the **FT 1X2** market ("Will X win on
<date>?", 90'+stoppage, a draw is possible) is a DIFFERENT market from **advance**
("reach Round of N", after extra-time + penalties). FT probabilities come from
``site/scores_data.json`` ``model_1x2``; advance probabilities come from
``site/advancement_data.json`` ``model`` (reach-probs incl. ET/pens). They are
never conflated. The one place the two MEET is a deliberate cross-check
(:func:`advance_vs_ft_flags`): P(advance past the next match) must be >= P(win
that next match in 90'), because winning in 90' is one of several ways to advance.

Priceable calibration categories
---------------------------------
* ``advance``       — reach-Round-of-N YES probs (advancement model vs PM).
* ``match_result``  — FT 1X2 YES probs (scores model ``model_1x2`` vs PM).
* ``btts``          — both-teams-to-score YES (scores model ``btts`` vs PM).
* ``exact_score``   — exact-scoreline YES (scores model ``scores`` vs PM).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# The advancement ladder, strongest -> weakest. P at each stage is the
# probability of *reaching* that stage (so it must be non-increasing).
LADDER_STAGES: Tuple[str, ...] = ("R16", "QF", "SF", "Final", "win")

# Pretty labels for the calibration categories.
CATEGORIES: Tuple[str, ...] = ("advance", "match_result", "btts", "exact_score")


# =========================================================================== #
# 1. Model-vs-market calibration & edge                                       #
# =========================================================================== #


@dataclass
class EdgeRow:
    """A single model<->PM comparison for one priceable outcome."""

    category: str
    subject: str            # team / fixture / scoreline that names the outcome
    label: str              # human-readable outcome label
    model_prob: float       # model probability in [0,1]
    pm_price: float         # live PM YES price (implied prob) in [0,1]
    token_id: Optional[str] = None

    @property
    def edge(self) -> float:
        """Signed edge = model - PM (positive => model thinks PM is too cheap)."""
        return self.model_prob - self.pm_price

    @property
    def abs_edge(self) -> float:
        return abs(self.edge)

    def as_dict(self) -> Dict[str, object]:
        return {
            "category": self.category,
            "subject": self.subject,
            "label": self.label,
            "model_prob": self.model_prob,
            "pm_price": self.pm_price,
            "edge": self.edge,
            "token_id": self.token_id,
        }


def _is_prob(x: object) -> bool:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and 0.0 <= v <= 1.0


def build_edge_rows(observations: Sequence[Dict[str, object]]) -> List[EdgeRow]:
    """Turn raw ``{category, subject, label, model_prob, pm_price, token_id}``
    observations into :class:`EdgeRow`s, skipping any with an out-of-range or
    missing probability/price. Pure: callers pass already-joined model<->PM pairs.
    """
    rows: List[EdgeRow] = []
    for o in observations:
        mp = o.get("model_prob")
        pm = o.get("pm_price")
        if not (_is_prob(mp) and _is_prob(pm)):
            continue
        rows.append(
            EdgeRow(
                category=str(o.get("category") or "other"),
                subject=str(o.get("subject") or ""),
                label=str(o.get("label") or ""),
                model_prob=float(mp),  # type: ignore[arg-type]
                pm_price=float(pm),     # type: ignore[arg-type]
                token_id=(str(o["token_id"]) if o.get("token_id") else None),
            )
        )
    return rows


def aggregate_category_bias(rows: Sequence[EdgeRow]) -> Dict[str, Dict[str, float]]:
    """Per-category systematic-bias summary.

    For each category returns ``n``, ``mean_edge``, ``median_edge``,
    ``mean_abs_edge``, ``rmse`` (root-mean-square of edges) and ``frac_model_high``
    (share of rows where model > PM). A non-zero ``mean_edge`` is the systematic
    miscalibration signal: e.g. a consistently positive BTTS ``mean_edge`` means
    our BTTS model sits above PM across the board.
    """
    by_cat: Dict[str, List[EdgeRow]] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)

    out: Dict[str, Dict[str, float]] = {}
    for cat, rs in by_cat.items():
        edges = [r.edge for r in rs]
        n = len(edges)
        out[cat] = {
            "n": float(n),
            "mean_edge": statistics.fmean(edges),
            "median_edge": statistics.median(edges),
            "mean_abs_edge": statistics.fmean([abs(e) for e in edges]),
            "rmse": math.sqrt(statistics.fmean([e * e for e in edges])),
            "frac_model_high": statistics.fmean([1.0 if e > 0 else 0.0 for e in edges]),
        }
    return out


def is_degenerate(row: EdgeRow, *, lo: float = 0.02, hi: float = 0.98) -> bool:
    """True when the PM price is pinned near 0 or 1 — a dead / already-resolved
    market that carries no live calibration signal (e.g. a "reach R16" market
    quoting 0.001 for a team that is already through). Such rows produce huge
    spurious "edges" and should be excluded from the aggregate bias.
    """
    return row.pm_price <= lo or row.pm_price >= hi


def filter_live(rows: Sequence[EdgeRow], *, lo: float = 0.02, hi: float = 0.98) -> List[EdgeRow]:
    """Drop degenerate (pinned-price) rows; keep genuinely live markets."""
    return [r for r in rows if not is_degenerate(r, lo=lo, hi=hi)]


def calibration_summary(rows: Sequence[EdgeRow], *, lo: float = 0.02,
                        hi: float = 0.98) -> Dict[str, object]:
    """Full calibration report: per-category bias + the top absolute edges.

    ``by_category`` is computed over ALL rows; ``by_category_live`` excludes
    degenerate pinned-price markets (the trustworthy aggregate). ``top_edges``
    lists the largest absolute edges among the LIVE rows so dead-market noise
    does not dominate.
    """
    live = filter_live(rows, lo=lo, hi=hi)
    by_cat = aggregate_category_bias(rows)
    by_cat_live = aggregate_category_bias(live)
    top = sorted(live, key=lambda r: r.abs_edge, reverse=True)
    return {
        "n_rows": len(rows),
        "n_rows_live": len(live),
        "by_category": by_cat,
        "by_category_live": by_cat_live,
        "top_edges": [r.as_dict() for r in top],
    }


# =========================================================================== #
# 2. Term-structure consistency flags                                         #
# =========================================================================== #


@dataclass
class LadderViolation:
    team: str
    source: str             # "model" or "pm"
    stage_hi: str           # earlier/stronger stage (should be >= stage_lo)
    stage_lo: str
    prob_hi: float
    prob_lo: float

    @property
    def gap(self) -> float:
        """How much the ladder is inverted (positive = a real violation)."""
        return self.prob_lo - self.prob_hi

    def as_dict(self) -> Dict[str, object]:
        return {
            "team": self.team,
            "source": self.source,
            "stage_hi": self.stage_hi,
            "stage_lo": self.stage_lo,
            "prob_hi": self.prob_hi,
            "prob_lo": self.prob_lo,
            "gap": self.gap,
        }


def check_ladder_monotonic(
    team: str,
    probs: Dict[str, float],
    *,
    source: str,
    tol: float = 1e-9,
    stages: Sequence[str] = LADDER_STAGES,
) -> List[LadderViolation]:
    """Flag where an advancement ladder is NOT non-increasing.

    ``probs`` maps stage -> P(reach that stage). The required ordering is
    ``P(R16) >= P(QF) >= P(SF) >= P(Final) >= P(win)``. Adjacent pairs where the
    later stage has a *higher* reach-prob than the earlier one (beyond ``tol``)
    are returned as violations. Missing stages are skipped (we only compare
    consecutive *present* stages). Pure.
    """
    present = [(s, float(probs[s])) for s in stages if _is_prob(probs.get(s))]
    out: List[LadderViolation] = []
    for (s_hi, p_hi), (s_lo, p_lo) in zip(present, present[1:]):
        if p_lo > p_hi + tol:
            out.append(LadderViolation(team, source, s_hi, s_lo, p_hi, p_lo))
    return out


def _pm_stage_prices(team_pm: Dict[str, object]) -> Dict[str, float]:
    """Extract stage -> PM price from an advancement ``pm`` block.

    ``advancement_data.json`` stores ``pm`` as ``{stage: {"pm": price, ...}}``;
    we flatten to ``{stage: price}`` keeping only valid probabilities.
    """
    out: Dict[str, float] = {}
    for stage, blob in (team_pm or {}).items():
        price = None
        if isinstance(blob, dict):
            price = blob.get("pm")
        else:
            price = blob
        if _is_prob(price):
            out[stage] = float(price)  # type: ignore[arg-type]
    return out


def ladder_violations(
    teams: Sequence[Dict[str, object]],
    *,
    tol: float = 1e-6,
) -> List[LadderViolation]:
    """Scan advancement teams for ladder (term-structure) violations.

    Each team dict is an entry from ``advancement_data.json``'s ``teams`` list:
    ``{"team": ..., "model": {stage: reach_prob}, "pm": {stage: {"pm": price}}}``.
    Checks BOTH the model ladder and the PM-price ladder. PM violations are arb /
    mispricing signals (you could buy the cheaper later stage and sell the dearer
    earlier one). Pure.
    """
    out: List[LadderViolation] = []
    for t in teams:
        name = str(t.get("team") or "")
        model = t.get("model") or {}
        if isinstance(model, dict):
            out.extend(check_ladder_monotonic(name, model, source="model", tol=tol))  # type: ignore[arg-type]
        pm = _pm_stage_prices(t.get("pm") or {})  # type: ignore[arg-type]
        if pm:
            out.extend(check_ladder_monotonic(name, pm, source="pm", tol=tol))
    return out


@dataclass
class AdvanceVsFtFlag:
    """Advance-prob must dominate FT-win-prob for the same upcoming match."""

    team: str
    source: str             # "model" or "pm"
    advance_prob: float     # P(advance past the next match)
    ft_win_prob: float      # P(win that next match in 90')

    @property
    def gap(self) -> float:
        """Positive = violation: FT-win exceeds advance (impossible)."""
        return self.ft_win_prob - self.advance_prob

    def as_dict(self) -> Dict[str, object]:
        return {
            "team": self.team,
            "source": self.source,
            "advance_prob": self.advance_prob,
            "ft_win_prob": self.ft_win_prob,
            "gap": self.gap,
        }


def check_advance_vs_ft(
    team: str,
    advance_prob: float,
    ft_win_prob: float,
    *,
    source: str,
    tol: float = 1e-6,
) -> Optional[AdvanceVsFtFlag]:
    """Flag a single advance-vs-FT inconsistency.

    Winning the next match in 90' is one of several disjoint ways to advance
    (the others: draw-then-win-on-pens, win-in-ET, etc.), so we must have
    ``P(advance) >= P(win in 90')``. Returns a flag when ``ft_win_prob`` exceeds
    ``advance_prob`` by more than ``tol``, else ``None``. Pure.

    Note: this is the *only* sanctioned place FT and advance probabilities are
    compared — and even here they are bounded, never equated.
    """
    if not (_is_prob(advance_prob) and _is_prob(ft_win_prob)):
        return None
    if ft_win_prob > advance_prob + tol:
        return AdvanceVsFtFlag(team, source, float(advance_prob), float(ft_win_prob))
    return None


def term_structure_report(
    teams: Sequence[Dict[str, object]],
    advance_vs_ft: Sequence[Dict[str, object]] = (),
    *,
    tol: float = 1e-6,
) -> Dict[str, object]:
    """Bundle ladder violations + advance-vs-FT flags into one report.

    ``advance_vs_ft`` is an optional list of pre-joined cross-checks, each
    ``{team, source, advance_prob, ft_win_prob}`` (the join — which next match,
    which side — happens in the live script where fixtures are known).
    """
    ladder = ladder_violations(teams, tol=tol)
    cross: List[AdvanceVsFtFlag] = []
    for c in advance_vs_ft:
        flag = check_advance_vs_ft(
            str(c.get("team") or ""),
            c.get("advance_prob"),  # type: ignore[arg-type]
            c.get("ft_win_prob"),   # type: ignore[arg-type]
            source=str(c.get("source") or "model"),
            tol=tol,
        )
        if flag is not None:
            cross.append(flag)
    return {
        "n_ladder_violations": len(ladder),
        "ladder_violations": [v.as_dict() for v in ladder],
        "n_advance_vs_ft_flags": len(cross),
        "advance_vs_ft_flags": [f.as_dict() for f in cross],
    }


# =========================================================================== #
# 3. Open-position mark-to-market                                             #
# =========================================================================== #


@dataclass
class MarkedPosition:
    book: str               # "real" or "paper"
    bet_id: object
    fixture: str
    market: str
    selection: str
    resolution_basis: str   # FT / advance / prop / exact / outright / ...
    token_id: Optional[str]
    stake: float            # cash at risk (GBP for real-book non-PM, USD for paper)
    currency: str           # "GBP" / "USD"
    entry_price: Optional[float]   # YES share price [0,1] if known
    decimal_odds: Optional[float]  # decimal odds if priced that way
    mark_price: Optional[float]    # current PM YES price [0,1], None if no mark
    unrealized_pl: Optional[float] = None
    shares: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        d = dict(self.__dict__)
        return d


def shares_from_stake(stake: float, entry_price: Optional[float],
                      decimal_odds: Optional[float]) -> Optional[float]:
    """Number of unit-payout (=1) YES shares a ``stake`` buys.

    A "share" pays 1 on win, 0 on loss. With an explicit YES ``entry_price`` (the
    cost of one share) the count is ``stake / entry_price``. With ``decimal_odds``
    instead, a winning bet returns ``stake * decimal_odds``, i.e. it owns
    ``stake * decimal_odds`` units of unit-payout claim (implied entry price is
    ``1/decimal_odds``, and ``stake / (1/decimal_odds) = stake * decimal_odds``).
    ``entry_price`` wins when both are given; returns ``None`` when neither is
    usable.
    """
    if entry_price is not None and entry_price > 0:
        return stake / entry_price
    if decimal_odds is not None and decimal_odds > 0:
        return stake * decimal_odds
    return None


def mark_to_market(stake: float, mark_price: float, *,
                   entry_price: Optional[float] = None,
                   decimal_odds: Optional[float] = None) -> Optional[float]:
    """Unrealised P&L of an open binary-YES position marked at ``mark_price``.

    A position holds ``shares`` of a $1-payout YES claim; its current value is
    ``shares * mark_price`` and unrealised P&L is ``value - stake``. ``mark_price``
    is the live PM YES price in [0,1]. Returns ``None`` if shares can't be derived
    or the mark is out of range. Pure.

    Examples
    --------
    Bought 100 shares at 0.20 ($20 stake) now trading 0.30:
    value = 100*0.30 = $30, P&L = +$10.
    """
    if not _is_prob(mark_price):
        return None
    shares = shares_from_stake(stake, entry_price, decimal_odds)
    if shares is None:
        return None
    return shares * float(mark_price) - float(stake)


def mark_position(pos: Dict[str, object], mark_price: Optional[float]) -> MarkedPosition:
    """Build a :class:`MarkedPosition` from a normalised position dict + a mark.

    ``pos`` is the normalised position (book/bet_id/fixture/market/selection/
    resolution_basis/token_id/stake/currency/entry_price/decimal_odds). ``mark_price``
    is the live PM YES price (or ``None`` when no mark is available). Pure.
    """
    stake = float(pos.get("stake") or 0.0)
    entry = pos.get("entry_price")
    entry_f = float(entry) if _is_prob(entry) else None
    dec = pos.get("decimal_odds")
    dec_f = float(dec) if (dec is not None and float(dec) > 0) else None  # type: ignore[arg-type]

    shares = shares_from_stake(stake, entry_f, dec_f)
    unreal = None
    if mark_price is not None:
        unreal = mark_to_market(stake, float(mark_price), entry_price=entry_f,
                                decimal_odds=dec_f)
    return MarkedPosition(
        book=str(pos.get("book") or ""),
        bet_id=pos.get("bet_id"),
        fixture=str(pos.get("fixture") or ""),
        market=str(pos.get("market") or ""),
        selection=str(pos.get("selection") or ""),
        resolution_basis=str(pos.get("resolution_basis") or "other"),
        token_id=(str(pos["token_id"]) if pos.get("token_id") else None),
        stake=stake,
        currency=str(pos.get("currency") or ""),
        entry_price=entry_f,
        decimal_odds=dec_f,
        mark_price=(float(mark_price) if mark_price is not None else None),
        unrealized_pl=unreal,
        shares=shares,
    )


def mtm_totals(marked: Sequence[MarkedPosition]) -> Dict[str, object]:
    """Aggregate marked positions: totals overall, by book, by resolution basis.

    Stakes/P&L are summed *within* a currency only (real GBP and paper USD are not
    cross-converted). Positions with no mark contribute to ``n_unmarked`` and are
    excluded from P&L sums. Pure.
    """
    def _empty() -> Dict[str, float]:
        return {"n": 0, "n_marked": 0, "n_unmarked": 0,
                "stake_marked": 0.0, "unrealized_pl": 0.0}

    overall: Dict[str, Dict[str, float]] = {}      # by currency
    by_book: Dict[str, Dict[str, Dict[str, float]]] = {}   # book -> currency -> agg
    by_basis: Dict[str, Dict[str, Dict[str, float]]] = {}  # basis -> currency -> agg

    def _bucket(d: Dict[str, Dict[str, float]], key: str) -> Dict[str, float]:
        return d.setdefault(key, _empty())

    for m in marked:
        cur = m.currency or "?"
        ov = _bucket(overall, cur)
        bk = _bucket(by_book.setdefault(m.book, {}), cur)
        ba = _bucket(by_basis.setdefault(m.resolution_basis, {}), cur)
        for agg in (ov, bk, ba):
            agg["n"] += 1
        if m.unrealized_pl is None or m.mark_price is None:
            for agg in (ov, bk, ba):
                agg["n_unmarked"] += 1
        else:
            for agg in (ov, bk, ba):
                agg["n_marked"] += 1
                agg["stake_marked"] += m.stake
                agg["unrealized_pl"] += float(m.unrealized_pl)

    def _roi(buckets: Dict[str, Dict[str, float]]) -> None:
        for agg in buckets.values():
            s = agg["stake_marked"]
            agg["roi_pct"] = (100.0 * agg["unrealized_pl"] / s) if s else 0.0

    _roi(overall)
    for cmap in by_book.values():
        _roi(cmap)
    for cmap in by_basis.values():
        _roi(cmap)

    return {
        "overall": overall,
        "by_book": by_book,
        "by_basis": by_basis,
        "n_positions": len(marked),
    }
