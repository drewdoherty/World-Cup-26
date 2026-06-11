"""Deterministic cross-venue arbitrage detection (no network).

Encodes an explicit *settlement key* per market so that we never pair prices
whose underlying resolution differs (the "fake-arb trap"): e.g. a 90-minute
1X2 draw must never be paired against a "to qualify" market that includes
extra time and penalties.

Two prices are arbable only if (a) they carry the *same* settlement key and
(b) their outcomes form a mutually-exclusive-and-exhaustive partition of the
outcome space (3-way for 1X2; complementary 2-way for BTTS / DNB / a totals
over-under at one specific line).

All odds are *net of commission* before the arb test is applied:
exchange back prices have their winnings reduced by the venue commission, and
Polymarket prices have the taker fee subtracted.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Settlement identity
# ---------------------------------------------------------------------------

# Default commission map (fraction of net winnings) for exchanges / books.
# A plain bookmaker has zero commission. Betfair is 6% (gating; 2% from July),
# Smarkets and Matchbook are 2%.
DEFAULT_COMMISSIONS: Dict[str, float] = {
    "betfair_ex_uk": 0.06,
    "betfair_ex_eu": 0.06,
    "betfair": 0.06,
    "smarkets": 0.02,
    "matchbook": 0.02,
}

# Polymarket taker fee: fee = rate * p * (1 - p) per share at price p.
PM_TAKER_FEE_RATE = 0.03


def settlement_key(market_key: str, point: Optional[float] = None) -> Optional[str]:
    """Return a settlement-identity string for *market_key* (+ line *point*).

    Markets that resolve on the same underlying event share a key.  Returns
    ``None`` for markets we refuse to arb (unknown or settlement-ambiguous,
    e.g. outright / to-qualify which include ET/pens).
    """
    mk = (market_key or "").lower()
    if mk in ("h2h", "1x2"):
        return "1x2_90min"
    if mk == "h2h_lay":
        # A lay on the match-odds market settles identically to the back.
        return "1x2_90min"
    if mk == "btts":
        return "btts_90min"
    if mk == "draw_no_bet":
        return "dnb_90min"
    if mk in ("totals", "alternate_totals"):
        if point is None:
            return None
        return "totals_%s_90min" % _fmt_line(point)
    # Outright / to-qualify / advancement markets resolve on ET/pens -> refuse.
    return None


def _fmt_line(point: float) -> str:
    f = float(point)
    if f == int(f):
        return str(int(f))
    return ("%g" % f)


# Settlement keys that may legitimately be paired against a Polymarket
# match-winner (3-way moneyline) market.
PM_MONEYLINE_SETTLEMENT = "1x2_90min"


# ---------------------------------------------------------------------------
# Net price helpers
# ---------------------------------------------------------------------------

def effective_back(
    decimal_odds: float,
    book: str,
    commissions: Optional[Dict[str, float]] = None,
) -> float:
    """Net decimal return after commission on winnings.

    For a plain bookmaker this is the raw decimal odds.  For an exchange
    charging commission *c* on net winnings, a 1 stake returning ``d`` pays
    ``1 + (d - 1) * (1 - c)``.
    """
    comm_map = DEFAULT_COMMISSIONS if commissions is None else commissions
    c = comm_map.get(book, 0.0)
    if decimal_odds <= 1.0:
        return decimal_odds
    return 1.0 + (decimal_odds - 1.0) * (1.0 - c)


def pm_yes_to_decimal(price: float, taker_fee_rate: float = PM_TAKER_FEE_RATE) -> float:
    """Net decimal odds of buying a Polymarket YES share at *price*.

    Buying one share costs ``price + fee`` (fee = rate * p * (1-p)) and pays
    out 1 if YES resolves.  Net decimal = payout / cost.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    fee = taker_fee_rate * price * (1.0 - price)
    cost = price + fee
    if cost <= 0:
        return 0.0
    return 1.0 / cost


# ---------------------------------------------------------------------------
# Core arb math
# ---------------------------------------------------------------------------

def _arb_from_net(net_prices: Sequence[float]) -> Optional[Dict[str, Any]]:
    """Given net decimal back prices for an exhaustive partition, return arb.

    Arb exists iff ``sum(1/net) < 1``.  Stake fractions equalise payout across
    outcomes; total fractions sum to 1.  ``profit_pct`` is the guaranteed
    return on total stake.
    """
    if any(p <= 1.0 for p in net_prices):
        return None
    inv = [1.0 / p for p in net_prices]
    s = sum(inv)
    if s >= 1.0:
        return None
    fractions = [i / s for i in inv]
    profit_pct = (1.0 / s) - 1.0
    return {"profit_pct": profit_pct, "stake_fractions": fractions}


def two_way_arb(
    price_a: float,
    price_b: float,
    book_a: str = "",
    book_b: str = "",
    commissions: Optional[Dict[str, float]] = None,
    net: bool = False,
) -> Optional[Dict[str, Any]]:
    """Arb across two complementary outcomes.

    If *net* is False, prices are raw decimal odds and commission is applied
    via ``effective_back``.  If *net* is True they are already net.
    """
    if net:
        na, nb = price_a, price_b
    else:
        na = effective_back(price_a, book_a, commissions)
        nb = effective_back(price_b, book_b, commissions)
    res = _arb_from_net([na, nb])
    if res is None:
        return None
    res["legs"] = [
        {"book": book_a, "net_odds": na, "stake_fraction": res["stake_fractions"][0]},
        {"book": book_b, "net_odds": nb, "stake_fraction": res["stake_fractions"][1]},
    ]
    return res


def three_way_arb(
    prices: Sequence[Tuple[float, str]],
    commissions: Optional[Dict[str, float]] = None,
    net: bool = False,
) -> Optional[Dict[str, Any]]:
    """Arb across three mutually-exclusive-exhaustive outcomes (1X2).

    *prices* is a sequence of ``(decimal_odds, book)`` triples.
    """
    if len(prices) != 3:
        return None
    nets = []
    for odds, book in prices:
        nets.append(odds if net else effective_back(odds, book, commissions))
    res = _arb_from_net(nets)
    if res is None:
        return None
    res["legs"] = [
        {"book": prices[i][1], "net_odds": nets[i],
         "stake_fraction": res["stake_fractions"][i]}
        for i in range(3)
    ]
    return res


def stake_split(stake_fractions: Sequence[float], bankroll: float) -> List[float]:
    """Convert stake fractions into absolute stakes summing to *bankroll*."""
    return [f * bankroll for f in stake_fractions]


# ---------------------------------------------------------------------------
# Cross-book detectors
# ---------------------------------------------------------------------------

def _best_net_by_outcome(
    rows: List[Dict[str, Any]],
    commissions: Optional[Dict[str, float]],
) -> Dict[str, Dict[str, Any]]:
    """Best net back price per outcome_name within a single (event,market,line)."""
    best: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        name = r["outcome_name"]
        odds = float(r["decimal_odds"])
        book = r["bookmaker_key"]
        net = effective_back(odds, book, commissions)
        if name not in best or net > best[name]["net"]:
            best[name] = {"net": net, "odds": odds, "book": book}
    return best


def find_cross_book_arbs(
    odds_df: Any,
    commissions: Optional[Dict[str, float]] = None,
    min_profit: float = 0.005,
) -> List[Dict[str, Any]]:
    """Detect back-only arbs across books within each (event, market, line).

    Pairs/triples are only formed within a single settlement key.  Returns a
    list of arb dicts with legs, net prices, profit_pct and a settlement key.
    """
    arbs: List[Dict[str, Any]] = []
    if odds_df is None or len(odds_df) == 0:
        return arbs

    records = odds_df.to_dict("records")
    # Group by (event_id, market, point)
    groups: Dict[Tuple[Any, Any, Any], List[Dict[str, Any]]] = {}
    for r in records:
        mk = r.get("market") or r.get("market_key")
        point = r.get("outcome_point")
        # Only lay markets are excluded from back-arb (handled elsewhere).
        if mk == "h2h_lay":
            continue
        skey = settlement_key(mk, point if point is not None else None)
        if skey is None:
            continue
        groups.setdefault((r["event_id"], mk, point), []).append(r)

    for (eid, mk, point), rows in groups.items():
        skey = settlement_key(mk, point if point is not None else None)
        best = _best_net_by_outcome(rows, commissions)
        names = list(best.keys())
        if len(names) == 3:
            prices = [(best[n]["odds"], best[n]["book"]) for n in names]
            res = three_way_arb(prices, commissions)
        elif len(names) == 2:
            a, b = names
            res = two_way_arb(
                best[a]["odds"], best[b]["odds"], best[a]["book"], best[b]["book"],
                commissions,
            )
        else:
            continue
        if res is None or res["profit_pct"] < min_profit:
            continue
        for i, n in enumerate(names):
            res["legs"][i]["outcome"] = n
            res["legs"][i]["raw_odds"] = best[n]["odds"]
        meta = rows[0]
        arbs.append({
            "kind": "cross_book",
            "event_id": eid,
            "market": mk,
            "settlement_key": skey,
            "home_team": meta.get("home_team"),
            "away_team": meta.get("away_team"),
            "point": point,
            "profit_pct": res["profit_pct"],
            "legs": res["legs"],
        })
    return arbs


# ---------------------------------------------------------------------------
# Polymarket detectors
# ---------------------------------------------------------------------------

def find_pm_book_arbs(
    odds_df: Any,
    pm_quotes: List[Dict[str, Any]],
    commissions: Optional[Dict[str, float]] = None,
    min_profit: float = 0.005,
    taker_fee_rate: float = PM_TAKER_FEE_RATE,
) -> List[Dict[str, Any]]:
    """Detect arbs involving Polymarket match-winner (90-min) markets.

    *pm_quotes*: list of dicts, each describing one PM market with keys:
        ``event_id`` (book event id to match), ``outcome`` (the book outcome
        name the YES side corresponds to, e.g. home team / "Draw"),
        ``yes_price``, ``no_price`` (optional), ``settlement_key``
        (must equal ``1x2_90min`` to pair against the book), and optional
        ``question`` label.

    Two arb families are produced:

    1. **PM-internal**: YES + NO sum < 1 after fee on each leg.
    2. **Book-vs-PM**: back the remaining 1X2 outcomes at the book and back
       the complementary PM YES so the three legs partition the 1X2 space.
       Only allowed when the PM market settles ``1x2_90min``.
    """
    arbs: List[Dict[str, Any]] = []

    # --- PM-internal YES+NO < 1 after fee ---
    for q in pm_quotes:
        yes = q.get("yes_price")
        no = q.get("no_price")
        if yes is None or no is None:
            continue
        ny = pm_yes_to_decimal(yes, taker_fee_rate)
        nn = pm_yes_to_decimal(no, taker_fee_rate)
        res = two_way_arb(ny, nn, "polymarket_yes", "polymarket_no", net=True)
        if res is None or res["profit_pct"] < min_profit:
            continue
        res["legs"][0]["outcome"] = "YES"
        res["legs"][1]["outcome"] = "NO"
        arbs.append({
            "kind": "pm_internal",
            "event_id": q.get("event_id"),
            "settlement_key": q.get("settlement_key"),
            "question": q.get("question"),
            "profit_pct": res["profit_pct"],
            "legs": res["legs"],
        })

    # --- Book 1X2 vs PM YES (cross-venue 3-way) ---
    if odds_df is not None and len(odds_df) > 0:
        records = odds_df.to_dict("records")
        # best book net per (event, outcome) for h2h only
        book_best: Dict[Tuple[Any, str], Dict[str, Any]] = {}
        meta_by_event: Dict[Any, Dict[str, Any]] = {}
        for r in records:
            mk = r.get("market") or r.get("market_key")
            if mk not in ("h2h", "1x2"):
                continue
            eid = r["event_id"]
            name = r["outcome_name"]
            odds = float(r["decimal_odds"])
            book = r["bookmaker_key"]
            net = effective_back(odds, book, commissions)
            k = (eid, name)
            if k not in book_best or net > book_best[k]["net"]:
                book_best[k] = {"net": net, "odds": odds, "book": book}
            meta_by_event.setdefault(eid, r)

        for q in pm_quotes:
            if q.get("settlement_key") != PM_MONEYLINE_SETTLEMENT:
                continue  # settlement guard: refuse to pair
            eid = q.get("event_id")
            pm_outcome = q.get("outcome")
            yes = q.get("yes_price")
            if eid is None or pm_outcome is None or yes is None:
                continue
            # The two remaining 1X2 outcomes must come from the book.
            outcomes = [k[1] for k in book_best if k[0] == eid]
            others = [o for o in outcomes if o != pm_outcome]
            if len(others) != 2:
                continue
            pm_net = pm_yes_to_decimal(yes, taker_fee_rate)
            legs_prices = [
                (book_best[(eid, others[0])]["net"], others[0],
                 book_best[(eid, others[0])]["book"], book_best[(eid, others[0])]["odds"]),
                (book_best[(eid, others[1])]["net"], others[1],
                 book_best[(eid, others[1])]["book"], book_best[(eid, others[1])]["odds"]),
                (pm_net, pm_outcome, "polymarket", yes),
            ]
            res = _arb_from_net([lp[0] for lp in legs_prices])
            if res is None or res["profit_pct"] < min_profit:
                continue
            legs = []
            for i, (net, name, book, raw) in enumerate(legs_prices):
                legs.append({
                    "book": book, "outcome": name, "net_odds": net,
                    "raw_odds": raw, "stake_fraction": res["stake_fractions"][i],
                })
            meta = meta_by_event.get(eid, {})
            arbs.append({
                "kind": "pm_book",
                "event_id": eid,
                "settlement_key": PM_MONEYLINE_SETTLEMENT,
                "home_team": meta.get("home_team"),
                "away_team": meta.get("away_team"),
                "question": q.get("question"),
                "profit_pct": res["profit_pct"],
                "legs": legs,
            })

    return arbs


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rank_arbs(
    arbs: List[Dict[str, Any]],
    min_profit: float = 0.005,
) -> List[Dict[str, Any]]:
    """Filter by *min_profit* and sort by guaranteed return descending."""
    keep = [a for a in arbs if a.get("profit_pct", 0.0) >= min_profit]
    return sorted(keep, key=lambda a: a["profit_pct"], reverse=True)
