"""Agent 3 — Market Intelligence.

De-vigs bookmaker odds, detects steam moves, and measures Polymarket vs
bookmaker dislocation for a single fixture.

Input:  DataPackage
Output: MarketIntelligence
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from wca.agents.contracts import DataPackage, MarketIntelligence, SteamSignal

logger = logging.getLogger(__name__)

_OUTCOMES = ("home", "draw", "away")

# Steam threshold: a book's implied prob moving > this between snapshots is a signal.
_STEAM_THRESHOLD = 0.03   # 3-point implied-prob shift
# Min liquidity cut-off for Polymarket markets.
_PM_MIN_LIQUIDITY = 500.0


def run(pkg: DataPackage) -> MarketIntelligence:
    """Analyse market data in *pkg* and return :class:`MarketIntelligence`."""
    book_prices = _build_book_prices(pkg.bookmaker_odds, pkg.fixture.home, pkg.fixture.away)

    # --- Consensus (Shin de-vig) ------------------------------------------
    fair_shin = _shin_consensus(book_prices)

    # --- Multiplicative consensus -----------------------------------------
    fair_mult = _multiplicative_consensus(book_prices)

    # --- Best available price per outcome ----------------------------------
    best = _best_available(book_prices)

    # --- Steam detection --------------------------------------------------
    steam = _detect_steam(pkg.bookmaker_odds, pkg.fixture.home, pkg.fixture.away)

    # --- PM vs BM dislocation --------------------------------------------
    disloc = _dislocation_score(fair_mult, pkg.prediction_market_odds)

    return MarketIntelligence(
        fair_odds_estimate=fair_shin or fair_mult or {},
        bookmaker_consensus=fair_mult or {},
        market_dislocation_score=disloc,
        steam_signals=steam,
        best_available=best,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_book_prices(
    bookmaker_odds: List[Dict[str, Any]],
    home: str,
    away: str,
) -> Dict[str, Dict[str, float]]:
    """Reshape flat bookmaker rows into {book: {home, draw, away: odds}} dict."""
    books: Dict[str, Dict[str, float]] = defaultdict(dict)
    for row in bookmaker_odds:
        book = str(
            row.get("bookmaker_title") or row.get("bookmaker") or row.get("book") or "unknown"
        )
        outcome = str(row.get("outcome_name", row.get("selection", ""))).lower().strip()
        price = _to_float(
            row.get("decimal_odds") or row.get("price") or row.get("odds")
        )
        if price is None or price <= 1.0:
            continue
        # Map outcome strings to home/draw/away.
        if outcome in (home.lower(), "home", "1"):
            books[book]["home"] = price
        elif outcome in (away.lower(), "away", "2"):
            books[book]["away"] = price
        elif outcome in ("draw", "tie", "x", "the draw"):
            books[book]["draw"] = price
    # Keep only complete 1X2 lines.
    return {b: p for b, p in books.items() if all(k in p for k in _OUTCOMES)}


def _shin_consensus(
    book_prices: Dict[str, Dict[str, float]]
) -> Optional[Dict[str, float]]:
    """Return Shin-de-vigged median consensus, or None if no books are complete."""
    try:
        from wca.markets.devig import shin

        per_book: List[Dict[str, float]] = []
        for prices in book_prices.values():
            odds_list = [prices[o] for o in _OUTCOMES]
            fair = list(shin(odds_list))    # shin() takes decimal odds directly
            per_book.append(dict(zip(_OUTCOMES, fair)))

        if not per_book:
            return None

        result: Dict[str, float] = {}
        for outcome in _OUTCOMES:
            vals = sorted(b[outcome] for b in per_book)
            n = len(vals)
            if n == 0:
                continue
            mid = n // 2
            result[outcome] = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2

        total = sum(result.values())
        if total > 0:
            result = {k: v / total for k, v in result.items()}
        return result
    except Exception as exc:
        logger.warning("Shin consensus failed: %s", exc)
        return None


def _multiplicative_consensus(
    book_prices: Dict[str, Dict[str, float]]
) -> Optional[Dict[str, float]]:
    """Return multiplicative (basic normalisation) median across books."""
    if not book_prices:
        return None

    per_book: List[Dict[str, float]] = []
    for prices in book_prices.values():
        raw = {o: 1.0 / prices[o] for o in _OUTCOMES}
        total = sum(raw.values())
        if total > 0:
            per_book.append({o: raw[o] / total for o in _OUTCOMES})

    if not per_book:
        return None

    result: Dict[str, float] = {}
    for outcome in _OUTCOMES:
        vals = sorted(b[outcome] for b in per_book)
        n = len(vals)
        mid = n // 2
        result[outcome] = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2

    total = sum(result.values())
    return {k: v / total for k, v in result.items()} if total else None


def _best_available(
    book_prices: Dict[str, Dict[str, float]]
) -> Dict[str, Dict[str, float]]:
    """Return {outcome: {book, odds}} for the best (highest) price per outcome."""
    best: Dict[str, Dict[str, float]] = {}
    for outcome in _OUTCOMES:
        top_book, top_odds = None, 0.0
        for book, prices in book_prices.items():
            if prices.get(outcome, 0) > top_odds:
                top_odds = prices[outcome]
                top_book = book
        if top_book:
            best[outcome] = {"bookmaker": top_book, "odds": top_odds}
    return best


def _detect_steam(
    bookmaker_odds: List[Dict[str, Any]],
    home: str,
    away: str,
) -> List[SteamSignal]:
    """Simple intra-session steam detector: flag outcomes where all books
    moved in the same direction by more than *_STEAM_THRESHOLD*."""
    signals: List[SteamSignal] = []
    # Without time-series snapshots in the raw rows there is nothing to diff.
    # The snapshot daemon writes to `odds_snapshots` (SQLite); the linemove
    # module handles the full time-series.  Here we flag any row that carries
    # an explicit ``prev_odds`` field (set by some data sources).
    moves: Dict[str, List[float]] = defaultdict(list)
    for row in bookmaker_odds:
        prev = _to_float(row.get("prev_odds"))
        curr = _to_float(row.get("decimal_odds") or row.get("price") or row.get("odds"))
        outcome = str(row.get("outcome_name", "")).lower()
        if prev and curr and prev > 1.0 and curr > 1.0:
            delta_prob = (1.0 / curr) - (1.0 / prev)
            moves[outcome].append(delta_prob)

    for outcome, deltas in moves.items():
        if not deltas:
            continue
        avg = sum(deltas) / len(deltas)
        if abs(avg) >= _STEAM_THRESHOLD and all(
            (d > 0) == (avg > 0) for d in deltas
        ):
            direction = outcome if avg > 0 else ("away" if "home" in outcome else "home")
            signals.append(
                SteamSignal(
                    market="1X2",
                    direction=direction,
                    magnitude_pct=round(abs(avg) * 100, 2),
                    source="bookmaker_consensus",
                )
            )
    return signals


def _dislocation_score(
    bm_consensus: Optional[Dict[str, float]],
    pm_odds: List[Dict[str, Any]],
) -> float:
    """0–1 score measuring how far the best PM price diverges from BM consensus.

    Looks for a "match result" Polymarket market and computes the average
    absolute difference between BM and PM implied probabilities.
    """
    if not bm_consensus or not pm_odds:
        return 0.0

    # Find PM home/away probabilities (ignore markets that are not 1X2-like).
    pm_probs: Dict[str, float] = {}
    for row in pm_odds:
        market_title = (row.get("market", "") or "").lower()
        if "winner" in market_title or "match result" in market_title or "result" in market_title:
            sel = (row.get("selection", "") or "").lower()
            prob = _to_float(row.get("probability"))
            if prob and 0 < prob < 1:
                pm_probs[sel] = prob

    if not pm_probs:
        return 0.0

    diffs: List[float] = []
    for outcome in ("home", "away"):
        bm_p = bm_consensus.get(outcome)
        # Polymarket markets often use "Yes"/"No" — try both key forms.
        pm_p = pm_probs.get(outcome) or pm_probs.get("yes")
        if bm_p and pm_p:
            diffs.append(abs(bm_p - pm_p))

    return round(sum(diffs) / len(diffs), 4) if diffs else 0.0


def _to_float(val: Any) -> Optional[float]:
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None
