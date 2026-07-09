"""Event-markets pricing + trade-rec engine (Polymarket single-match markets).

This module is the pure (network-free) core behind ``scripts/wca_event_markets.py``:

1. **Grid pricing** — fair model probabilities for every 90-minute market family
   derivable from the production Dixon-Coles scoreline grid (the SAME reconciled
   matrix the card bets against, built via :func:`wca.models.scores.scoreline_card`
   / :func:`reconcile_scoreline_matrix` from a ``wca.card.fit_models`` fit with the
   production ``DEFAULT_DC_LEVEL_TARGET`` anchor): totals at ANY line, BTTS,
   exact score, spreads / winning margins, team totals, clean sheets, and the
   knockout "goes to extra time" question (= 90' draw).

2. **PM market classification** — map a live Gamma market (event kind +
   ``groupItemTitle`` / ``question`` / ``outcomes``) to a family descriptor that
   says HOW to model-price it, or records honestly that the production model
   cannot price it fairly yet (``model: null`` + reason — never an invented
   number).

3. **Market-blend calibration constraint (2026-07-08 study, n=70 totals / 24
   BTTS group-stage fixtures)** — the anchored DC grid ties the de-vigged market
   on Brier for O/U 2.5 (0.2384 vs 0.2383) and BTTS (0.2364 vs 0.2389): a
   legitimate fair-value ANCHOR with NO measured edge over market consensus.
   Where a de-vigged market reference exists, the emitted ``model_prob`` for the
   totals / BTTS / exact-score families is therefore a market-blended value
   (:data:`MARKET_BLEND_WEIGHT` market weight), with the blend weight and both
   components recorded on the row for audit. Raw DC-alone is only emitted when
   no market reference exists, and is labelled as such.

   The same study found a residual OVER-bias (+0.149 goals/match realized vs
   the anchored expectation; DC "under" calls went 33% win rate, -34.9% ROI,
   the only CI excluding zero), so every totals-family UNDER/lay signal carries
   :data:`UNDER_SIGNAL_WARNING` and is NEVER staked by
   :func:`build_event_market_recs` (displayed dimmed, stake 0, reason recorded).

4. **Trade recs governance** — :func:`build_event_market_recs` applies the SAME
   canonical desk rules as every other real-money surface: ``wca.selection``
   ordering (moneyline > mid > longshot by MODEL prob, further-out first, EV
   tiebreak), ``longshot_no_cash`` (< 0.25 model -> stake forced 0, displayed
   dimmed), the PM fee shape ``0.03 * p * (1 - p)``, a 2pp NET edge floor,
   quarter-Kelly on the PM pool via :mod:`wca.markets.bankroll`, the $160
   per-order execution cap, a same-fixture correlation cap (1X2 / totals /
   BTTS / exact on one match are correlated — the whole fixture is treated as
   ONE bet for cap purposes), and the standing kill-list (correct score and
   scorer props NEVER get cash).

Settlement bases are stamped on every row: 90-minute markets must never be
visually confusable with advancement (ET+pens) markets.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from wca.selection import (
    bucket_rank,
    longshot_no_cash,
    preference_sort_key,
    prob_bucket,
)

# ---------------------------------------------------------------------------
# Constants (rule-level; do not retune casually).
# ---------------------------------------------------------------------------

#: PM taker fee shape — same as scripts/wca_betrecs.py PM_FEE_RATE.
PM_FEE_RATE: float = 0.03

#: Net-edge floor (2pp) below which no trade signal / rec is emitted.
MIN_EDGE: float = 0.02

#: Forest back/lay signal threshold: |model - market| >= 2pp.
SIGNAL_THRESHOLD_PP: float = 2.0

#: Per-order execution cap ($). Mirrors the fail-closed constant in
#: ``wca.pm.trader.TraderConfig.max_order_usd`` (static, human-approved).
PM_MAX_ORDER_USD: float = 160.0

#: Market weight for the totals / BTTS / exact-score fair-value blend
#: (2026-07-08 calibration study: DC grid ties the de-vigged market on Brier,
#: so fair value leans on the market reference; 1X2 keeps the card's own
#: Elo/DC/market blend untouched).
MARKET_BLEND_WEIGHT: float = 0.60

#: Warning stamped on every totals-family UNDER/lay signal (2026-07-08 study:
#: DC under-side calls went 33% win rate, -34.9% ROI, n=21 — the only CI
#: excluding zero). Kept verbatim on rows + recs so the caveat travels with
#: the number.
UNDER_SIGNAL_WARNING: str = (
    "under-signal: DC under-calls historically unreliable "
    "(33% win, -34.9% ROI, n=21) — display only, never staked"
)

#: Market families the desk KILLED for cash (CLAUDE.md: do not resurrect).
KILLED_FAMILIES: Tuple[str, ...] = ("exact_score", "scorer_prop")

#: Settlement bases.
SETTLE_90MIN = "90min"
SETTLE_ADVANCE = "ET+pens"

# Families whose UNDER/lay side is display-only (see UNDER_SIGNAL_WARNING).
_TOTALS_FAMILIES = ("total_goals", "team_total")


# ---------------------------------------------------------------------------
# Grid pricing — every function takes the RECONCILED score matrix P[h, a]
# (rows = home goals, cols = away goals, sums to 1) from wca.models.scores.
# ---------------------------------------------------------------------------


def _mat(matrix: Any) -> np.ndarray:
    m = np.asarray(matrix, dtype=float)
    if m.ndim != 2:
        raise ValueError("score matrix must be 2-D (home goals x away goals)")
    return m


def prob_over(matrix: Any, line: float) -> float:
    """P(total goals > line) from the scoreline grid (half-integer lines)."""
    m = _mat(matrix)
    totals = np.add.outer(np.arange(m.shape[0]), np.arange(m.shape[1]))
    return float(m[totals > float(line)].sum())


def prob_btts(matrix: Any) -> float:
    """P(both teams score) from the scoreline grid."""
    m = _mat(matrix)
    yes = 1.0 - float(m[0, :].sum()) - float(m[:, 0].sum()) + float(m[0, 0])
    return float(min(max(yes, 0.0), 1.0))


def prob_exact(matrix: Any, home_goals: int, away_goals: int) -> float:
    """P(exact score home_goals-away_goals); 0.0 when outside the grid."""
    m = _mat(matrix)
    h, a = int(home_goals), int(away_goals)
    if h < 0 or a < 0 or h >= m.shape[0] or a >= m.shape[1]:
        return 0.0
    return float(m[h, a])


def prob_any_other_score(matrix: Any, listed: Sequence[Tuple[int, int]]) -> float:
    """P(none of the ``listed`` exact scores) — PM's "Any Other Score" leg."""
    m = _mat(matrix)
    p = 1.0
    for h, a in listed:
        p -= prob_exact(m, h, a)
    return float(min(max(p, 0.0), 1.0))


def prob_margin_at_least(matrix: Any, margin: int, side: str = "home") -> float:
    """P(side wins by >= ``margin`` goals). ``side`` is "home" or "away".

    A PM spread "France (-1.5)" (France home) is ``margin=2, side="home"``.
    """
    m = _mat(matrix)
    rows = np.arange(m.shape[0])[:, None]
    cols = np.arange(m.shape[1])[None, :]
    diff = rows - cols if side == "home" else cols - rows
    return float(m[diff >= int(margin)].sum())


def prob_team_over(matrix: Any, line: float, side: str = "home") -> float:
    """P(team goals > line) — marginal team total from the grid."""
    m = _mat(matrix)
    marginal = m.sum(axis=1) if side == "home" else m.sum(axis=0)
    goals = np.arange(marginal.shape[0])
    return float(marginal[goals > float(line)].sum())


def prob_clean_sheet(matrix: Any, side: str = "home") -> float:
    """P(``side`` keeps a clean sheet) = P(opponent scores 0)."""
    m = _mat(matrix)
    return float(m[:, 0].sum()) if side == "home" else float(m[0, :].sum())


def prob_draw(matrix: Any) -> float:
    """P(90-minute draw) — in a knockout tie this IS P(extra time)."""
    m = _mat(matrix)
    n = min(m.shape)
    return float(np.trace(m[:n, :n]))


# ---------------------------------------------------------------------------
# Market-blend fair value (totals / BTTS / exact-score families).
# ---------------------------------------------------------------------------


def blend_with_market(
    grid_prob: Optional[float],
    market_ref: Optional[float],
    market_weight: float = MARKET_BLEND_WEIGHT,
) -> Dict[str, Any]:
    """Blend the DC-grid probability with a de-vigged market reference.

    Returns ``{prob, source, components}`` where ``source`` says exactly what
    was blended. When no market reference exists, the raw grid value is
    returned and labelled ``dc_grid_raw (no market reference)`` — honest, not
    presented as a calibrated fair value.
    """
    if grid_prob is None:
        return {"prob": None, "source": "unpriced", "components": None}
    g = float(grid_prob)
    if market_ref is None or not (0.0 < float(market_ref) < 1.0):
        return {
            "prob": g,
            "source": "dc_grid_raw (no market reference)",
            "components": {"dc_grid": round(g, 6)},
        }
    w = float(market_weight)
    mref = float(market_ref)
    blended = (1.0 - w) * g + w * mref
    return {
        "prob": float(blended),
        "source": "blend(dc_grid %.0f%% + market %.0f%%)" % ((1 - w) * 100, w * 100),
        "components": {
            "dc_grid": round(g, 6),
            "market_ref": round(mref, 6),
            "market_weight": w,
        },
    }


# ---------------------------------------------------------------------------
# PM market classification.
# ---------------------------------------------------------------------------

#: Event-slug suffix -> event kind. The bare match slug (no suffix) is "main".
EVENT_KIND_SUFFIXES: Tuple[Tuple[str, str], ...] = (
    ("-halftime-result", "halftime_result"),
    ("-second-half-result", "second_half_result"),
    ("-exact-score", "exact_score"),
    ("-first-to-score", "first_to_score"),
    ("-more-markets", "more_markets"),
    ("-total-corners", "total_corners"),
    ("-player-props", "player_props"),
)

_OU_RE = re.compile(r"^O/U\s+(\d+(?:\.\d+)?)$")
_TEAM_OU_RE = re.compile(r"^(.*?)\s+O/U\s+(\d+(?:\.\d+)?)$")
_SPREAD_RE = re.compile(r"^(.*?)\s*\(([+-]\d+(?:\.\d+)?)\)$")
_EXACT_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")


def event_kind_from_slug(slug: str) -> str:
    """Classify a ``fifwc-...`` event slug into its market-family event kind."""
    s = (slug or "").strip().lower()
    for suffix, kind in EVENT_KIND_SUFFIXES:
        if s.endswith(suffix):
            return kind
    return "main"


def classify_pm_market(
    event_kind: str,
    group_item_title: str,
    question: str,
    home: str,
    away: str,
) -> Dict[str, Any]:
    """Classify one PM market into a pricing descriptor.

    Returns a dict with:

    * ``family`` — market family key (e.g. ``total_goals``, ``spread``);
    * ``label`` — display label for the forest row;
    * ``settlement`` — :data:`SETTLE_90MIN` or :data:`SETTLE_ADVANCE`;
    * ``priceable`` — True when the production grid can price it fairly;
    * ``model_null_reason`` — honest reason when ``priceable`` is False;
    * family-specific params (``line``, ``margin``, ``side``, ``score`` ...).

    The classifier NEVER invents a model number: anything it cannot map to a
    production pricing path comes back ``priceable=False`` with a reason.
    """
    git = (group_item_title or "").strip()
    q = (question or "").strip()
    text = git or q

    def _side_of(name: str) -> Optional[str]:
        n = name.strip().casefold()
        if n == (home or "").strip().casefold():
            return "home"
        if n == (away or "").strip().casefold():
            return "away"
        return None

    # --- main 1X2 event ----------------------------------------------------
    if event_kind == "main":
        if git.lower().startswith("draw") or "end in a draw" in q.lower():
            return {
                "family": "1x2", "label": "Draw", "leg": "draw",
                "settlement": SETTLE_90MIN, "priceable": True,
            }
        side = _side_of(git)
        if side is not None:
            return {
                "family": "1x2", "label": git, "leg": side,
                "settlement": SETTLE_90MIN, "priceable": True,
            }
        return _unpriceable("1x2", text, SETTLE_90MIN,
                            "unrecognised main-event leg")

    # --- exact score ---------------------------------------------------------
    if event_kind == "exact_score":
        if "any other" in text.lower():
            return {
                "family": "exact_score", "label": "Any Other Score",
                "any_other": True, "settlement": SETTLE_90MIN, "priceable": True,
            }
        mm = _EXACT_RE.search(text)
        if mm:
            return {
                "family": "exact_score",
                "label": text, "score": (int(mm.group(1)), int(mm.group(2))),
                "settlement": SETTLE_90MIN, "priceable": True,
            }
        return _unpriceable("exact_score", text, SETTLE_90MIN,
                            "unrecognised exact-score leg")

    # --- player props --------------------------------------------------------
    if event_kind == "player_props":
        return {
            "family": "scorer_prop", "label": text,
            "settlement": SETTLE_90MIN, "priceable": True,
        }

    # --- halves / timing / corners: no production model ----------------------
    if event_kind == "halftime_result":
        return _unpriceable(
            "halftime_result", text or "Halftime result", SETTLE_90MIN,
            "no half-split model in production (90-min DC grid only)")
    if event_kind == "second_half_result":
        return _unpriceable(
            "second_half_result", text or "Second-half result", SETTLE_90MIN,
            "no half-split model in production (90-min DC grid only)")
    if event_kind == "first_to_score":
        return _unpriceable(
            "first_to_score", text or "First team to score", SETTLE_90MIN,
            "no within-match timing model in production")
    if event_kind == "total_corners":
        return _unpriceable(
            "corners", text or "Corners", SETTLE_90MIN,
            "corners model is tournament base-rate reference only — "
            "not fair per-fixture pricing")

    # --- more-markets bucket -------------------------------------------------
    if event_kind == "more_markets":
        low = text.lower()

        # Anything half-scoped first (1st/2nd half O/U, half BTTS, half spreads).
        if "1st half" in low or "2nd half" in low or "first half" in low \
                or "second half" in low or "halftime" in low:
            return _unpriceable(
                "half_market", text, SETTLE_90MIN,
                "no half-split model in production (90-min DC grid only)")

        if low == "team to advance":
            return {
                "family": "advance", "label": "Team to Advance",
                "settlement": SETTLE_ADVANCE, "priceable": True,
            }
        if "extra time" in low:
            return {
                "family": "extra_time",
                "label": "Goes to Extra Time (= 90' draw)",
                "settlement": SETTLE_90MIN, "priceable": True,
            }
        if "penalty shootout" in low:
            return _unpriceable(
                "penalty_shootout", text, SETTLE_ADVANCE,
                "needs an extra-time goals model (not in production)")
        if "odd or even" in low:
            return _unpriceable(
                "odd_even", text, SETTLE_90MIN,
                "parity market — no production pricing path")

        if low == "both teams to score":
            return {
                "family": "btts", "label": "BTTS — Yes",
                "settlement": SETTLE_90MIN, "priceable": True,
            }

        mm = _OU_RE.match(text)
        if mm:
            line = float(mm.group(1))
            return {
                "family": "total_goals", "label": "Over %s Goals" % mm.group(1),
                "line": line, "settlement": SETTLE_90MIN, "priceable": True,
            }

        mm = _SPREAD_RE.match(text)
        if mm:
            side = _side_of(mm.group(1))
            handicap = float(mm.group(2))
            if side is not None and handicap < 0:
                margin = int(math.ceil(-handicap))
                return {
                    "family": "spread",
                    "label": "%s %s (win by %d+)" % (mm.group(1).strip(),
                                                     mm.group(2), margin),
                    "side": side, "margin": margin,
                    "settlement": SETTLE_90MIN, "priceable": True,
                }
            return _unpriceable("spread", text, SETTLE_90MIN,
                                "unrecognised spread side/handicap")

        mm = _TEAM_OU_RE.match(text)
        if mm:
            side = _side_of(mm.group(1))
            if side is not None:
                line = float(mm.group(2))
                return {
                    "family": "team_total",
                    "label": "%s Over %s Goals" % (mm.group(1).strip(),
                                                   mm.group(2)),
                    "side": side, "line": line,
                    "settlement": SETTLE_90MIN, "priceable": True,
                }
            return _unpriceable("team_total", text, SETTLE_90MIN,
                                "unrecognised team-total side")

        return _unpriceable("other", text, SETTLE_90MIN,
                            "unmapped PM market — no production pricing path")

    return _unpriceable("other", text, SETTLE_90MIN,
                        "unknown PM event kind %r" % event_kind)


def _unpriceable(family: str, label: str, settlement: str, reason: str) -> Dict[str, Any]:
    return {
        "family": family, "label": label, "settlement": settlement,
        "priceable": False, "model_null_reason": reason,
    }


def grid_prob_for(desc: Dict[str, Any], matrix: Any,
                  model_1x2: Optional[Dict[str, float]] = None) -> Optional[float]:
    """Model probability for a classified PM market's PRIMARY outcome.

    ``matrix`` is the reconciled scoreline grid; ``model_1x2`` (the exact card
    blend) overrides the grid's implied triple for the 1X2 family so the forest
    1X2 stays bit-identical to the persisted card predictions. Returns ``None``
    when the descriptor is not grid-priceable here (advance, scorer props —
    those are priced by their own dedicated sources).
    """
    if not desc.get("priceable"):
        return None
    fam = desc.get("family")
    if fam == "1x2":
        leg = desc.get("leg")
        if model_1x2 and leg in model_1x2 and model_1x2[leg] is not None:
            return float(model_1x2[leg])
        if matrix is None:
            return None
        from wca.models.scores import implied_1x2

        trip = implied_1x2(_mat(matrix))
        return {"home": trip[0], "draw": trip[1], "away": trip[2]}.get(leg)
    if matrix is None:
        return None
    if fam == "total_goals":
        return prob_over(matrix, desc["line"])
    if fam == "btts":
        return prob_btts(matrix)
    if fam == "exact_score":
        if desc.get("any_other"):
            listed = desc.get("listed_scores") or []
            return prob_any_other_score(matrix, listed)
        h, a = desc["score"]
        return prob_exact(matrix, h, a)
    if fam == "spread":
        return prob_margin_at_least(matrix, desc["margin"], desc["side"])
    if fam == "team_total":
        return prob_team_over(matrix, desc["line"], desc["side"])
    if fam == "extra_time":
        return prob_draw(matrix)
    return None


# ---------------------------------------------------------------------------
# Signals + fees.
# ---------------------------------------------------------------------------


def pm_fee(p: float) -> float:
    """PM taker fee at price ``p``: ``0.03 * p * (1 - p)``."""
    return PM_FEE_RATE * float(p) * (1.0 - float(p))


def edge_pp(model_prob: Optional[float], market_prob: Optional[float]) -> Optional[float]:
    """Signed edge in percentage points (model - market), or ``None``."""
    if model_prob is None or market_prob is None:
        return None
    return (float(model_prob) - float(market_prob)) * 100.0


def signal_for(model_prob: Optional[float], market_prob: Optional[float],
               threshold_pp: float = SIGNAL_THRESHOLD_PP) -> Optional[str]:
    """Trade signal at the +/-2pp threshold.

    ``"back"`` when model >= market + 2pp (back the outcome), ``"lay"`` when
    market >= model + 2pp (lay / back the complement), else ``None``. A ``None``
    input on either side yields no signal.
    """
    e = edge_pp(model_prob, market_prob)
    if e is None:
        return None
    if e >= threshold_pp:
        return "back"
    if e <= -threshold_pp:
        return "lay"
    return None


def totals_under_warning(family: str, side_is_under_or_lay: bool) -> Optional[str]:
    """The UNDER-bias caveat for totals-family under/lay signals (else None)."""
    if family in _TOTALS_FAMILIES and side_is_under_or_lay:
        return UNDER_SIGNAL_WARNING
    return None


# ---------------------------------------------------------------------------
# Trade recs (site/event_market_recs.json rows).
# ---------------------------------------------------------------------------


def _no_cash_reason(family: str, model_prob: float, side: str) -> Optional[str]:
    """Why a candidate is display-only (stake forced to 0), or None if cashable.

    Order matters: the kill-list and the totals-under ban are structural rules;
    the longshot floor is the generic selection rule.
    """
    if family in KILLED_FAMILIES:
        return ("killed market family (%s) — display only, never cash "
                "(desk rule, CLAUDE.md)" % family)
    if family in _TOTALS_FAMILIES and side == "lay":
        return UNDER_SIGNAL_WARNING
    if longshot_no_cash(model_prob):
        return ("longshot (<25% model) — free-bet/lottery only, "
                "stake forced to 0 (wca.selection.longshot_no_cash)")
    return None


def build_event_market_recs(
    candidates: List[Dict[str, Any]],
    *,
    bankroll_usd: float,
    now_dt: Any = None,
    min_edge: float = MIN_EDGE,
    max_order_usd: float = PM_MAX_ORDER_USD,
) -> Dict[str, Any]:
    """Rank + size event-market trade candidates under full desk governance.

    Each candidate dict needs::

        fixture, kickoff (ISO str or ""), family, label,
        side ("back"|"lay" — which way the model points),
        selection (display name of the outcome being BACKED),
        model_prob (0..1 of the outcome being BACKED),
        price (0..1 PM cost of that outcome), settlement,
        token_id, price_source, captured_utc, model_source

    Pipeline (mirrors the production surfaces):

    1. NET edge = model - price - fee(price); keep >= ``min_edge`` (2pp).
    2. Kill-list + totals-under ban + longshot cash floor -> stake 0, kept
       (dimmed) for display honesty.
    3. Quarter-Kelly stake on the PM pool via
       :func:`wca.markets.bankroll.size_placement`, hard-capped at
       min($160, 4% of pool) per order.
    4. Same-fixture correlation cap: all cash stakes on ONE fixture are
       proportionally scaled so their sum <= the single-order cap (1X2 /
       totals / BTTS / exact on one match are correlated — the fixture is
       treated as ONE bet).
    5. Canonical desk ordering via :func:`wca.selection.preference_sort_key`.

    Returns the full feed dict (``meta`` + ``recs``).
    """
    from wca.markets import bankroll as pm_rule

    hard_cap = min(float(max_order_usd),
                   pm_rule.PM_MAX_STAKE_FRAC * float(bankroll_usd))

    recs: List[Dict[str, Any]] = []
    for c in candidates:
        q = c.get("model_prob")
        p = c.get("price")
        if q is None or p is None or not (0.0 < float(p) < 1.0):
            continue
        q, p = float(q), float(p)
        fee = pm_fee(p)
        edge_net = q - p - fee
        # Inclusive floor with an epsilon so a mathematically-exact 2pp edge
        # is never dropped by float rounding.
        if edge_net + 1e-12 < float(min_edge):
            continue

        no_cash = _no_cash_reason(c.get("family") or "", q, c.get("side") or "back")
        sized = pm_rule.size_placement(q, p, float(bankroll_usd))
        stake = min(float(sized["stake"]), hard_cap)
        caps: List[str] = []
        if sized.get("capped"):
            caps.append("per-trade %.0f%% pool cap" % (pm_rule.PM_MAX_STAKE_FRAC * 100))
        if float(sized["stake"]) > hard_cap:
            caps.append("$%.0f per-order cap" % float(max_order_usd))
        if no_cash is not None:
            stake = 0.0

        rec = dict(c)
        rec.update({
            "model_prob": round(q, 6),
            "price": round(p, 6),
            "price_c": round(p * 100, 1),
            "model_c": round(q * 100, 1),
            "fee": round(fee, 6),
            "edge_net": round(edge_net, 6),
            "ev": round(edge_net / p, 6),          # net EV per $1 staked
            "ev_pct": round(edge_net / p * 100, 1),
            "bucket": prob_bucket(q),
            "dimmed": no_cash is not None,
            "no_cash_reason": no_cash,
            "stake_usd": round(stake, 2),
            "kelly_stake_usd": round(float(sized["stake"]), 2),
            "caps_applied": caps,
        })
        warn = totals_under_warning(c.get("family") or "",
                                    (c.get("side") or "back") == "lay")
        if warn:
            rec["warning"] = warn
        recs.append(rec)

    # --- Same-fixture correlation cap (cash rows only) -----------------------
    by_fixture: Dict[str, float] = {}
    for r in recs:
        by_fixture[r["fixture"]] = by_fixture.get(r["fixture"], 0.0) + r["stake_usd"]
    for fixture, total in by_fixture.items():
        if total <= hard_cap or total <= 0.0:
            continue
        scale = hard_cap / total
        for r in recs:
            if r["fixture"] == fixture and r["stake_usd"] > 0.0:
                r["stake_usd"] = round(r["stake_usd"] * scale, 2)
                r["caps_applied"] = list(r.get("caps_applied") or []) + [
                    "same-fixture correlation cap ($%.0f/fixture, scaled x%.2f)"
                    % (hard_cap, scale)
                ]

    # --- Canonical desk ordering (wca.selection) ------------------------------
    kick_by_match = {r["fixture"]: r.get("kickoff") or "" for r in recs}
    for r in recs:
        r["match_desc"] = r["fixture"]  # key expected by selection.hours_out
    recs.sort(key=lambda r: preference_sort_key(r, kick_by_match, now_dt))
    from wca.selection import hours_out as _hours_out

    for r in recs:
        r["hours_out"] = round(_hours_out(r, kick_by_match, now_dt), 1)
        r["bucket_rank"] = bucket_rank(r["model_prob"])
        r.pop("match_desc", None)

    return {
        "meta": {
            "fee_rate": PM_FEE_RATE,
            "min_edge": float(min_edge),
            "bankroll_usd": round(float(bankroll_usd), 2),
            "per_order_cap_usd": round(hard_cap, 2),
            "correlation_cap": (
                "same-fixture cash stakes are correlated (1X2/totals/BTTS/exact "
                "on one match) and are jointly capped at $%.0f per fixture"
                % hard_cap
            ),
            "ranking": ("bucket by MODEL prob (moneyline>mid>longshot), "
                        "further-out fixtures first, net-EV tiebreak "
                        "(wca.selection.preference_sort_key)"),
            "settlement_note": ("90-min markets settle on the 90'+stoppage "
                                "score; ET+pens rows are advancement-basis and "
                                "flagged per row — never mix the two"),
            "sizing": "quarter-Kelly on the PM pool (wca.markets.bankroll)",
        },
        "recs": recs,
    }
