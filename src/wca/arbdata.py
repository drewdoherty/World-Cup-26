"""Build the arbitrage site feed (``site/arb_data.json``) — monitoring-only.

Pairs Polymarket (USD) ↔ Betfair (GBP via The Odds API) on EXACT canonical
team-set fixtures with a 1x2_90min settlement guard, computes FX-adjusted
risk-free opportunities, and emits the JSON the Arb tab renders. No execution.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from wca import arbfx
from wca.data.teamnames import canonical

# 1X2 outcome -> the PM "win" market we oppose. Draw is excluded (no clean
# two-way PM complement for the back-only aggregator feed).
_SIDES = ("home", "away")


def _fixture_key(home: str, away: str) -> str:
    return "%s vs %s" % (home, away)


def build_arb_data(
    *,
    betfair_rows: List[Dict[str, Any]],
    pm_quotes: Dict[str, Dict[str, float]],
    fx_usd_per_gbp: float,
    fx_source: str,
    now_utc: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure builder (no I/O).

    Parameters
    ----------
    betfair_rows: flat Betfair h2h rows (event_id, home_team, away_team,
        outcome_name, decimal_odds) from :func:`wca.data.betfair.betfair_odds`.
    pm_quotes: ``{fixture_key: {"home": yes_price, "away": yes_price,
        "settlement": "1x2_90min"}}`` — PM YES price that the team wins.
    fx_usd_per_gbp / fx_source: from :mod:`wca.fx`.
    history: optional prior detections for the HYPOTHETICAL cumulative curve.
    """
    # Index Betfair h2h back odds per fixture+team.
    bf: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in betfair_rows:
        if (r.get("market") or "") != "h2h":
            continue
        home, away = canonical(r.get("home_team") or ""), canonical(r.get("away_team") or "")
        fk = _fixture_key(home, away)
        team = canonical(r.get("outcome_name") or "")
        slot = "home" if team == home else ("away" if team == away else None)
        if slot is None:
            continue
        try:
            odds = float(r.get("decimal_odds"))
        except (TypeError, ValueError):
            continue
        bf.setdefault(fk, {})[slot] = {"team": team, "odds": odds}

    opps: List[Dict[str, Any]] = []
    for fk, sides in bf.items():
        q = pm_quotes.get(fk)
        if not q:
            continue
        # Settlement guard: only pair like-for-like 90-min markets.
        confidence = "high" if q.get("settlement") == "1x2_90min" else "low"
        if q.get("settlement") != "1x2_90min":
            continue
        for slot in _SIDES:
            leg = sides.get(slot)
            pm_price = q.get(slot)
            if not leg or pm_price is None:
                continue
            opp = arbfx.evaluate_pair(
                fixture=fk, market="h2h",
                betfair_outcome=leg["team"], betfair_odds=leg["odds"],
                pm_outcome="%s wins" % leg["team"], pm_price=float(pm_price),
                fx_usd_per_gbp=fx_usd_per_gbp, confidence=confidence,
            )
            if opp is not None:
                opps.append(_opp_to_row(opp))

    opps.sort(key=lambda o: o["guaranteed_pct"], reverse=True)
    return {
        "meta": {
            "generated": now_utc,
            "fx_usd_per_gbp": round(fx_usd_per_gbp, 4),
            "fx_source": fx_source,
            "monitoring_only": True,
            "note": "Read-only monitoring. No execution. Profit illustration is HYPOTHETICAL.",
        },
        "arbs": opps,
        "hypothetical": _cumulative(history or [], opps),
    }


def _opp_to_row(opp: "arbfx.ArbOpp") -> Dict[str, Any]:
    bf_leg = next(l for l in opp.legs if l.venue == "betfair")
    pm_leg = next(l for l in opp.legs if l.venue == "polymarket")
    return {
        "fixture": opp.fixture,
        "market": opp.market,
        "selection": opp.betfair_outcome,
        "pm_price": opp.pm_price,            # USD YES price
        "betfair_odds": opp.betfair_odds,    # GBP decimal
        "fx": opp.fx_usd_per_gbp,
        "fee_adj_edge": opp.fee_adj_edge,
        "guaranteed_pct": opp.guaranteed_pct,
        "stake_split": {"betfair_gbp": bf_leg.stake, "polymarket_usd": pm_leg.stake},
        "confidence": opp.confidence,
        "notes": opp.notes,
    }


def _cumulative(history: List[Dict[str, Any]], opps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """HYPOTHETICAL: cumulative risk-free % if every detection were executed.

    Pure illustration — NOT realised P&L. Sums guaranteed_pct over prior
    detections plus the current batch on a notional unit stake each.
    """
    pts = list(history)
    running = sum(p.get("guaranteed_pct", 0.0) for p in pts)
    for o in opps:
        running += o["guaranteed_pct"]
        pts.append({"guaranteed_pct": o["guaranteed_pct"], "cum_pct": round(running, 5)})
    return {
        "label": "HYPOTHETICAL — modeled, not live",
        "cum_pct": round(running, 5),
        "points": pts,
    }
