"""Agent 5 — Edge Detector.

Compares blended model probabilities against the best available bookmaker
price and Polymarket price.  Ranks opportunities by expected value and gates
out anything below the configured threshold.

Input:  DataPackage + ModelOutput + MarketIntelligence
Output: EdgeReport
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from wca.agents.contracts import (
    DataPackage,
    EdgeOpportunity,
    EdgeReport,
    MarketIntelligence,
    ModelOutput,
)

logger = logging.getLogger(__name__)

# Minimum positive edge to pass through (model_prob - implied_prob).
_MIN_EDGE = 0.02       # 2-percentage-point edge
# Minimum expected value (profit per unit) to pass.
_MIN_EV = 0.03         # 3 % return per unit
# Markets with fewer than this number of bookmakers quoting them are penalised.
_MIN_BOOKS = 2


def run(
    pkg: DataPackage,
    model: ModelOutput,
    market_intel: MarketIntelligence,
    min_edge: float = _MIN_EDGE,
    min_ev: float = _MIN_EV,
) -> EdgeReport:
    """Identify positive-EV opportunities for the fixture in *pkg*.

    Parameters
    ----------
    pkg:
        Raw data package (used for fixture label).
    model:
        Blended model output from Agent 4.
    market_intel:
        De-vigged market intelligence from Agent 3.
    min_edge:
        Minimum edge (model_prob - implied_prob) to keep an opportunity.
    min_ev:
        Minimum expected value (edge * odds) to keep an opportunity.
    """
    fixture_label = "%s vs %s" % (pkg.fixture.home, pkg.fixture.away)
    all_opps: List[EdgeOpportunity] = []
    rejected = 0

    # --- 1X2 opportunities ------------------------------------------------
    best = market_intel.best_available
    model_probs = {
        "home": model.win_prob,
        "draw": model.draw_prob,
        "away": model.loss_prob,
    }
    for outcome, prob in model_probs.items():
        best_info = best.get(outcome)
        if not best_info:
            continue
        odds = best_info["odds"]
        bookmaker = best_info["bookmaker"]
        opp = _evaluate(
            market="1X2",
            selection=outcome,
            bookmaker=bookmaker,
            odds=odds,
            model_prob=prob,
            book_count=_count_books(pkg.bookmaker_odds, outcome),
            min_edge=min_edge,
            min_ev=min_ev,
        )
        if opp:
            all_opps.append(opp)
        else:
            rejected += 1

    # --- Prop opportunities ----------------------------------------------
    prop_book_map = _build_prop_book_map(pkg.bookmaker_odds)
    for prop in model.prop_estimates:
        key = prop.selection.lower().replace(" ", "_")
        book_info = prop_book_map.get(key)
        if not book_info:
            continue
        opp = _evaluate(
            market=prop.market,
            selection=prop.selection,
            bookmaker=book_info["bookmaker"],
            odds=book_info["odds"],
            model_prob=prop.model_prob,
            book_count=1,
            min_edge=min_edge,
            min_ev=min_ev,
        )
        if opp:
            all_opps.append(opp)
        else:
            rejected += 1

    # --- Rank and pick top -----------------------------------------------
    all_opps.sort(key=lambda o: o.expected_value, reverse=True)
    top = all_opps[0] if all_opps else None

    if top:
        logger.info(
            "Top edge: %s %s @ %.2f (model %.1f%%, edge +%.1f%%, EV +%.1f%%)",
            top.market, top.selection, top.odds,
            top.model_probability * 100, top.edge * 100, top.expected_value * 100,
        )
    else:
        logger.info("No edges above threshold for %s", fixture_label)

    return EdgeReport(
        fixture=fixture_label,
        opportunities=all_opps,
        top_pick=top,
        rejected_count=rejected,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evaluate(
    market: str,
    selection: str,
    bookmaker: str,
    odds: float,
    model_prob: float,
    book_count: int,
    min_edge: float,
    min_ev: float,
) -> Optional[EdgeOpportunity]:
    if odds <= 1.0 or not (0 < model_prob < 1):
        return None

    implied = 1.0 / odds
    edge = model_prob - implied
    ev = edge * odds   # = model_prob * odds - 1

    if edge < min_edge or ev < min_ev:
        return None

    # Liquidity penalty: reduce EV slightly for thin markets.
    liq_penalty = max(0.0, (_MIN_BOOKS - book_count) * 0.01)

    return EdgeOpportunity(
        market=market,
        selection=selection,
        bookmaker=bookmaker,
        odds=round(odds, 3),
        model_probability=round(model_prob, 6),
        implied_probability=round(implied, 6),
        edge=round(edge, 6),
        expected_value=round(ev - liq_penalty, 6),
        liquidity_penalty=round(liq_penalty, 4),
    )


def _count_books(bookmaker_odds: List[Dict[str, Any]], outcome: str) -> int:
    """Count how many distinct books quote this outcome."""
    books = set()
    for row in bookmaker_odds:
        sel = str(row.get("outcome_name", row.get("selection", ""))).lower()
        if outcome in sel or sel in outcome:
            books.add(
                str(row.get("bookmaker_title") or row.get("bookmaker") or row.get("book") or "")
            )
    return len(books)


def _build_prop_book_map(bookmaker_odds: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map normalised prop-market selections to the best available odds."""
    best: Dict[str, Dict[str, Any]] = {}
    for row in bookmaker_odds:
        market = str(row.get("market_key", row.get("market", ""))).lower()
        if "h2h" in market or "moneyline" in market:
            continue   # skip 1X2
        sel = str(row.get("outcome_name", row.get("selection", ""))).lower().replace(" ", "_")
        price = _to_float(
            row.get("decimal_odds") or row.get("price") or row.get("odds")
        )
        if price is None or price <= 1.0:
            continue
        book = str(
            row.get("bookmaker_title") or row.get("bookmaker") or row.get("book") or "unknown"
        )
        existing = best.get(sel)
        if existing is None or price > existing["odds"]:
            best[sel] = {"bookmaker": book, "odds": price}
    return best


def _to_float(val: Any) -> Optional[float]:
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None
