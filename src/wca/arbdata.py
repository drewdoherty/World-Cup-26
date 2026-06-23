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


def _index_exchange(rows, home_away):
    """{fixture: {slot: {back, lay}}} of GBP exchange odds, canonicalised."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows or []:
        if (r.get("market") or "") != "h2h":
            continue
        home, away = canonical(r.get("home_team") or ""), canonical(r.get("away_team") or "")
        fk = _fixture_key(home, away)
        team = canonical(r.get("outcome_name") or "")
        slot = "home" if team == home else ("away" if team == away else None)
        if slot is None:
            continue
        try:
            back = float(r.get("decimal_odds"))
        except (TypeError, ValueError):
            continue
        try:
            lay = float(r["lay_odds"]) if r.get("lay_odds") is not None else None
        except (TypeError, ValueError):
            lay = None
        out.setdefault(fk, {})[slot] = {"team": team, "back": back, "lay": lay}
        home_away[fk] = (home, away)
    return out


def build_arb_data(
    *,
    betfair_rows: List[Dict[str, Any]],
    pm_quotes: Dict[str, Dict[str, float]],
    fx_usd_per_gbp: float,
    fx_source: str,
    now_utc: str,
    smarkets_rows: Optional[List[Dict[str, Any]]] = None,
    smarkets_grade: str = "monitoring-grade",
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Pure builder (no I/O). Best risk-free lock per (fixture, outcome) across
    PM (USD), Betfair (GBP) and Smarkets (GBP) — all venue pairs considered.

    pm_quotes: ``{fixture: {"home": yes, "away": yes, "settlement": "1x2_90min"}}``
    where ``yes`` is the PM YES price that the team wins. Exchange rows carry
    ``decimal_odds`` (back) and optional ``lay_odds`` (native depth only).
    """
    home_away: Dict[str, tuple] = {}
    bf = _index_exchange(betfair_rows, home_away)
    sm = _index_exchange(smarkets_rows or [], home_away)

    opps: List[Dict[str, Any]] = []
    for fk in set(bf) | set(sm):
        q = pm_quotes.get(fk)
        if not q or q.get("settlement") != "1x2_90min":
            continue  # settlement guard: 90-min only
        home, away = home_away[fk]
        for slot, team in (("home", home), ("away", away)):
            win_legs: List[Dict[str, Any]] = []   # back the outcome
            lose_legs: List[Dict[str, Any]] = []  # oppose it
            # Polymarket: YES = win, NO = lose.
            yes = q.get(slot)
            if yes is not None:
                wn = arbfx.pm_yes_to_decimal(float(yes))
                ln = arbfx.pm_no_net(float(yes))
                if wn > 1:
                    win_legs.append({"venue": "polymarket", "currency": "USD", "net": wn,
                                     "desc": "PM YES @ %.3f" % yes, "confidence": "monitoring-grade"})
                if ln > 1:
                    lose_legs.append({"venue": "polymarket", "currency": "USD", "net": ln,
                                      "desc": "PM NO @ %.3f" % (1 - float(yes)), "confidence": "monitoring-grade"})
            # Exchanges: back = win; lay (native depth) = lose, execution-grade.
            for venue, idx, grade in (("betfair", bf, "monitoring-grade"), ("smarkets", sm, smarkets_grade)):
                e = idx.get(fk, {}).get(slot)
                if not e:
                    continue
                bnet = arbfx.exchange_back_net(e["back"], venue)
                if bnet > 1:
                    win_legs.append({"venue": venue, "currency": "GBP", "net": bnet,
                                     "desc": "%s back @ %.2f" % (venue, e["back"]),
                                     "confidence": grade if venue == "smarkets" else "monitoring-grade"})
                if e.get("lay"):
                    lnet = arbfx.exchange_lay_net(e["lay"], venue)
                    if lnet > 1:
                        lose_legs.append({"venue": venue, "currency": "GBP", "net": lnet,
                                          "desc": "%s lay @ %.2f" % (venue, e["lay"]),
                                          "confidence": "execution-grade"})
            # Tag every candidate with its event so best_lock's hard guard can
            # refuse any accidental cross-team/cross-fixture pairing.
            for leg in win_legs + lose_legs:
                leg["fixture"] = fk
                leg["outcome"] = team
                leg["market"] = "h2h"
            res = arbfx.best_lock(fixture=fk, market="h2h", outcome=team,
                                  win_legs=win_legs, lose_legs=lose_legs,
                                  fx_usd_per_gbp=fx_usd_per_gbp)
            if res is not None:
                opps.append(_lock_to_row(res))

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


def _lock_to_row(res: "arbfx.LockResult") -> Dict[str, Any]:
    legs = [{"venue": l.venue, "currency": l.currency, "side": l.side,
             "net": l.net, "desc": l.desc, "stake": l.stake} for l in res.legs]
    return {
        "fixture": res.fixture,
        "market": res.market,
        "selection": res.outcome,
        "venue_pair": res.venue_pair,
        "fee_adj_edge": res.fee_adj_edge,
        "guaranteed_pct": res.guaranteed_pct,
        "stake_split": {("%s_%s" % (l["venue"], l["currency"].lower())): l["stake"] for l in legs},
        "legs": legs,
        "confidence": res.confidence,
        "notes": res.notes,
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
