"""Cross-venue derived metrics.

Given the latest quote per (selection, venue) for one market — i.e. the output of
:func:`wca.intel.store.latest_per_selection` — compute the cross-venue analytics
the dashboard and ``/arb`` consume: best / worst / average / median price, implied
range and spread, the % a backer gains at the best vs the worst book, dispersion,
a vig-adjusted consensus probability, the largest pairwise disagreement, and
(when a model and bankroll are supplied) EV and ¼-Kelly stake against the best
available price.

Pure and generic over market type / number of selections. Vig removal reuses
:func:`wca.markets.devig.shin` over the COMPLETE market; consensus is left
``None`` for a partial book (we never fabricate a fair price). EV/Kelly reuse
:mod:`wca.markets.kelly`. No new math.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Sequence

from wca.markets import devig, kelly
from wca.markets.bankroll import PM_KELLY_FRACTION
from wca.selection import longshot_no_cash
from wca.intel.registry import commission_for, venue_colour, venue_for


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs: Sequence[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def selection_metrics(selection: str, quotes: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """Cross-venue metrics for ONE selection.

    ``quotes`` is a list of dicts (one per venue) with at least ``venue`` and
    ``decimal_odds`` (and optionally ``implied_raw``, ``liquidity``, ``ts_utc``).
    Best = highest decimal odds (most value to a backer); worst = lowest.
    """
    qs = [q for q in quotes if q.get("decimal_odds") not in (None, "")]
    if not qs:
        return {"selection": selection, "n_venues": 0}
    odds = {str(q["venue"]): float(q["decimal_odds"]) for q in qs}
    implied = {v: 1.0 / o for v, o in odds.items() if o > 1.0}

    best_v = max(odds, key=odds.get)            # highest decimal odds
    worst_v = min(odds, key=odds.get)
    best_o, worst_o = odds[best_v], odds[worst_v]

    imp_vals = list(implied.values())
    # largest pairwise disagreement in implied prob, with the venue pair.
    pair = None
    gap = 0.0
    if len(implied) >= 2:
        hi_v = max(implied, key=implied.get)
        lo_v = min(implied, key=implied.get)
        gap = implied[hi_v] - implied[lo_v]
        pair = (hi_v, lo_v)

    return {
        "selection": selection,
        "n_venues": len(odds),
        "best_odds": best_o,
        "worst_odds": worst_o,
        "best_venue": best_v,
        "worst_venue": worst_v,
        "avg_odds": _mean(list(odds.values())),
        "median_odds": _median(list(odds.values())),
        "implied_range": (max(imp_vals) - min(imp_vals)) if imp_vals else None,
        "spread": round(best_o - worst_o, 6),
        "pct_improvement": round(best_o / worst_o - 1.0, 6) if worst_o > 0 else None,
        "stdev": statistics.pstdev(imp_vals) if len(imp_vals) >= 2 else 0.0,
        "largest_disagreement": gap,
        "disagreement_pair": pair,
        "venues": {v: {"odds": o, "implied": implied.get(v), "colour": venue_colour(v)}
                   for v, o in odds.items()},
    }


def consensus_probs(latest: Dict[str, List[Dict[str, object]]],
                    method: str = "shin") -> Dict[str, Optional[float]]:
    """Vig-adjusted consensus probability per selection.

    For each venue that quotes the *complete* market we devig its book (Shin over
    all selections), then average each selection's fair prob across those venues
    and renormalise to sum to 1. Returns ``{selection: None}`` when no venue
    offers a complete book (never fabricates from a partial market).
    """
    selections = list(latest.keys())
    # odds per venue per selection
    by_venue: Dict[str, Dict[str, float]] = {}
    for sel, quotes in latest.items():
        for q in quotes:
            o = q.get("decimal_odds")
            if o:
                by_venue.setdefault(str(q["venue"]), {})[sel] = float(o)

    fair_per_sel: Dict[str, List[float]] = {s: [] for s in selections}
    for venue, sel_odds in by_venue.items():
        if len(sel_odds) < 2 or any(s not in sel_odds for s in selections):
            continue  # incomplete book for this market → skip this venue
        sels = list(sel_odds.keys())
        try:
            probs = devig.devig([sel_odds[s] for s in sels], method=method)
        except Exception:
            continue
        for s, p in zip(sels, probs):
            fair_per_sel[s].append(float(p))

    means = {s: (_mean(v) if v else None) for s, v in fair_per_sel.items()}
    total = sum(p for p in means.values() if p is not None)
    if total and total > 0:
        return {s: (p / total if p is not None else None) for s, p in means.items()}
    return {s: None for s in selections}


def build_market_metrics(latest: Dict[str, List[Dict[str, object]]], *,
                         model: Optional[Dict[str, float]] = None,
                         bankroll: Optional[float] = None,
                         fraction: Optional[float] = None, cap: float = 0.05,
                         method: str = "shin") -> List[Dict[str, object]]:
    """Per-selection metrics for a complete market.

    Adds the vig-adjusted ``consensus_prob`` and ``median_prob``; when ``model``
    (selection -> prob) is supplied, also ``model_prob``, ``ev_vs_model`` (edge at
    the best available, commission-adjusted for the best venue), and — with a
    ``bankroll`` — the ¼-Kelly ``kelly_stake`` at that price.

    ``fraction`` defaults to :data:`wca.markets.bankroll.PM_KELLY_FRACTION` (the
    ONE Kelly fraction) rather than a hard-coded 0.25 — that literal collided
    numerically with the 0.25 model-prob longshot floor. A selection the model
    rates ``< 0.25`` is a longshot (:func:`wca.selection.longshot_no_cash`) —
    free-bet / lottery only — so its ``kelly_stake`` is 0 (``no_cash`` flagged).
    """
    if fraction is None:
        fraction = PM_KELLY_FRACTION
    consensus = consensus_probs(latest, method=method)
    out: List[Dict[str, object]] = []
    for sel, quotes in latest.items():
        m = selection_metrics(sel, quotes)
        m["consensus_prob"] = consensus.get(sel)
        idv = [q.get("implied_devig") for q in quotes if q.get("implied_devig") is not None]
        m["median_prob"] = _median(idv) if idv else None
        if model is not None and sel in model and m.get("best_odds"):
            p = float(model[sel])
            best_o = float(m["best_odds"])
            # commission on the best venue eats into net odds for exchanges/PM.
            comm = commission_for(m.get("best_venue", ""))
            net_o = 1.0 + (best_o - 1.0) * (1.0 - comm) if comm else best_o
            m["model_prob"] = p
            m["ev_vs_model"] = kelly.edge(p, net_o)
            if bankroll:
                if longshot_no_cash(p):
                    # <25c model prob -> no cash (free-bet/lottery only).
                    m["kelly_stake"] = 0.0
                    m["no_cash"] = True
                else:
                    m["kelly_stake"] = kelly.stake(p, net_o, float(bankroll),
                                                   fraction=fraction, cap=cap)
        out.append(m)
    return out
