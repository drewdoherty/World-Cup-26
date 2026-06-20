"""Portfolio exposure & blind-spot analytics for the World Cup Alpha book.

Given the open bet ledger, the model's 1X2 probabilities for the upcoming
slate, and the latest market odds, this module computes — deterministically,
with no IO or clock access — three things the front-end visualises:

1. **Per-match exposure** — for every result (and the events the user actually
   holds: scorelines, props, acca legs), the expected P&L if that outcome
   occurs.  Accumulators that span several matches contribute their
   *model-conditional* expected payoff (profit times the probability that the
   acca's *other* legs also land), so a leg only ever adds upside to the
   outcome it needs, never to the outcomes that kill it.

2. **Blind spots** — results/events carrying meaningful probability where the
   net exposure is zero or negative: the places where, now that we're stacking
   accas, we might *think* we're covered but aren't.

3. **Upside/downside correlation + gap plugs** — the joint result-scenario
   distribution of total P&L (how concentrated the upside is, how often the
   book loses), plus, for each blind spot, the best current market price to
   plug it and whether plugging is +EV or better left unhedged.

Free bets / promo stakes (``source == 'offer'``) are stake-not-returned: a loss
costs £0, only the profit is at stake.  Real-money bets lose their stake.
"""
from __future__ import annotations

import datetime
import itertools
import re
from typing import Any, Dict, List, Optional, Tuple

# A fixture stays on the risk slate until ~this long after kickoff (a match plus
# stoppage / a little buffer); after that it is treated as finished and dropped,
# so the panel never shows blind spots / "no live market price" for past games.
IN_PLAY_HOURS = 3.0

# Probability a blind spot must exceed to be "meaningful" (else it's noise).
BLINDSPOT_MIN_PROB = 0.18
# Net-P&L at or below this (£) counts as "not covered" for an outcome.
BLINDSPOT_NET_FLOOR = 0.50
# Result markets whose single bets settle directly on 1X2.
_RESULT_MARKETS = {"Full-time result", "Match Odds", "Match Winner", "h2h"}
# Team-name aliases seen in bet descriptions vs the model fixture spelling.
_ALIAS = {"Türkiye": "Turkey", "Turkiye": "Turkey", "Korea Republic": "South Korea"}


def _team_key(name: str) -> str:
    name = (name or "").strip()
    return _ALIAS.get(name, name)


def _is_free(bet: Dict[str, Any]) -> bool:
    """Promo / free-bet stake: a loss costs nothing (stake not returned)."""
    return str(bet.get("source") or "") == "offer"


def _profit(bet: Dict[str, Any]) -> float:
    return float(bet["stake"]) * (float(bet["decimal_odds"]) - 1.0)


def _acca_legs(match_desc: str) -> List[str]:
    """Winning-team names for an ACCA, parsed from its match_desc.

    Handles "Acca 4-fold: Switzerland+Brazil+Scotland+Turkey",
    "Acca treble (drop AUS): Switzerland+Brazil+Scotland" and
    "Treble: Netherlands + Brazil + Paraguay".
    """
    md = match_desc or ""
    if ":" in md:
        md = md.split(":", 1)[1]
    return [_team_key(p) for p in re.split(r"[+]", md) if p.strip()]


def _parse_dt(value: Any) -> Optional[datetime.datetime]:
    """Parse an ISO-ish timestamp to an aware UTC datetime, or ``None``."""
    if not value:
        return None
    txt = str(value).strip().replace("Z", "+00:00")
    for candidate in (txt, txt[:19]):
        try:
            d = datetime.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    return None


def _is_future_or_inplay(
    kickoff: Any, now: Optional[datetime.datetime]
) -> bool:
    """Upcoming or still in play (not finished). Missing/unparseable kickoff or
    a missing ``now`` -> kept (never silently drop a possibly-live fixture)."""
    if now is None:
        return True
    ko = _parse_dt(kickoff)
    if ko is None:
        return True
    return now < ko + datetime.timedelta(hours=IN_PLAY_HOURS)


def build_slate(
    model_fixtures: List[Dict[str, Any]],
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Index the model fixtures into ``{fixture: {home, away, kickoff, p{}}}``.

    When ``now`` is given, fixtures that have already finished (kickoff more than
    :data:`IN_PLAY_HOURS` in the past) are dropped, so the risk view only ever
    covers future or in-play games.
    """
    slate: Dict[str, Dict[str, Any]] = {}
    for f in model_fixtures:
        fixture = f["fixture"]
        if " vs " not in fixture:
            continue
        if not _is_future_or_inplay(f.get("kickoff"), now):
            continue
        home, away = fixture.split(" vs ", 1)
        m = f.get("model") or {}
        slate[fixture] = {
            "home": home,
            "away": away,
            "kickoff": f.get("kickoff"),
            "p": {home: m.get("home"), "Draw": m.get("draw"), away: m.get("away")},
        }
    return slate


def _team_to_fixture(slate: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """Map a winning-team name to ``(fixture, winning_selection)``."""
    idx: Dict[str, Tuple[str, str]] = {}
    for fx, d in slate.items():
        idx[d["home"]] = (fx, d["home"])
        idx[d["away"]] = (fx, d["away"])
    return idx


def _map_single(bet: Dict[str, Any], slate: Dict[str, Dict[str, Any]]
                ) -> Optional[Tuple[str, str]]:
    """Map a result single to ``(fixture, winning_selection)`` or None."""
    md = (bet.get("match_desc") or "").strip()
    fx = md if md in slate else None
    if fx is None:
        for k in slate:
            if k.lower() == md.lower():
                fx = k
                break
    if fx is None:
        return None
    d = slate[fx]
    sel = (bet.get("selection") or "").strip()
    if sel in ("The Draw", "Draw"):
        return (fx, "Draw")
    for team in (d["home"], d["away"]):
        if sel == team or _team_key(sel) == _team_key(team):
            return (fx, team)
    return None


# ---------------------------------------------------------------------------
# Core build
# ---------------------------------------------------------------------------
def build_exposure_data(
    bets: List[Dict[str, Any]],
    model_fixtures: List[Dict[str, Any]],
    odds_index: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
    now_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the full exposure feed.

    Parameters
    ----------
    bets:
        Open bets (ledger rows as dicts): match_desc, market, selection,
        decimal_odds, stake, source, account, status.
    model_fixtures:
        ``data/model_predictions.json`` ``fixtures`` list.
    odds_index:
        ``{fixture: {outcome: {venue: decimal_odds}}}`` for plug suggestions.
    now_utc:
        Display timestamp for the feed header.
    """
    # Restrict the risk slate to future / in-play games (drop finished ones), so
    # blind spots and per-fixture exposure never cover games already played.
    slate = build_slate(model_fixtures, now=_parse_dt(now_utc))
    team2fx = _team_to_fixture(slate)
    odds_index = odds_index or {}

    result_bets: List[Dict[str, Any]] = []
    acca_bets: List[Dict[str, Any]] = []
    event_bets: Dict[str, List[Dict[str, Any]]] = {}
    unmapped: List[str] = []

    for b in bets:
        if str(b.get("status") or "open") != "open":
            continue
        market = b.get("market") or ""
        if market == "ACCA":
            legs_raw = _acca_legs(b.get("match_desc") or "")
            legs: List[Tuple[str, str]] = []
            off = False
            for t in legs_raw:
                if t in team2fx:
                    legs.append(team2fx[t])
                else:
                    off = True
            acca_bets.append({
                "legs": legs, "profit": _profit(b), "free": _is_free(b),
                "label": b.get("match_desc"), "off": off,
                "stake": float(b["stake"]), "odds": float(b["decimal_odds"]),
            })
        elif market in _RESULT_MARKETS:
            m = _map_single(b, slate)
            if m:
                result_bets.append({
                    "fx": m[0], "sel": m[1], "profit": _profit(b),
                    "free": _is_free(b), "stake": float(b["stake"]),
                    "label": b.get("selection"), "odds": float(b["decimal_odds"]),
                })
            else:
                unmapped.append("%s / %s" % (b.get("match_desc"), b.get("selection")))
        else:
            md = (b.get("match_desc") or "")
            fx = None
            for k in slate:
                if k.lower() in md.lower() or md.lower() in k.lower():
                    fx = k
                    break
            rec = {
                "type": market, "selection": b.get("selection"),
                "stake": float(b["stake"]), "profit": _profit(b),
                "free": _is_free(b), "odds": float(b["decimal_odds"]),
            }
            if fx is None:
                rec["match"] = md
            event_bets.setdefault(fx or "(off-slate)", []).append(rec)

    # Fixtures carrying live exposure (>=1 open bet — a result single, an acca
    # leg, or a prop/event bet). A blind spot is only flagged where we actually
    # hold a position, so "uncovered" never fires for games we simply aren't on.
    fixtures_with_bets = set(rb["fx"] for rb in result_bets)
    for ab in acca_bets:
        for (f2, _s) in ab["legs"]:
            fixtures_with_bets.add(f2)
    for fx_key, evs in event_bets.items():
        if fx_key != "(off-slate)" and evs:
            fixtures_with_bets.add(fx_key)

    fixtures_out = [
        _fixture_exposure(fx, slate, result_bets, acca_bets,
                          event_bets.get(fx, []), odds_index.get(fx, {}),
                          has_bets=fx in fixtures_with_bets)
        for fx in slate
    ]
    portfolio, correlation = _portfolio_scenarios(slate, result_bets, acca_bets)
    blindspots = _collect_blindspots(fixtures_out)

    return {
        "meta": {"generated": now_utc},
        "portfolio": portfolio,
        "correlation": correlation,
        "fixtures": fixtures_out,
        "blindspots": blindspots,
        "unmapped": unmapped,
        "off_slate_accas": [a["label"] for a in acca_bets if a["off"]],
        "off_slate_events": event_bets.get("(off-slate)", []),
    }


def _fixture_exposure(fx, slate, result_bets, acca_bets, events, fx_odds,
                      has_bets=True):
    d = slate[fx]
    outcomes = [d["home"], "Draw", d["away"]]
    rows = []
    for X in outcomes:
        direct = 0.0
        live = []
        for rb in result_bets:
            if rb["fx"] != fx:
                continue
            if rb["sel"] == X:
                direct += rb["profit"]
                live.append({"label": rb["label"], "kind": "single",
                             "stake": rb["stake"], "profit": round(rb["profit"], 2),
                             "free": rb["free"]})
            elif not rb["free"]:
                direct -= rb["stake"]
        acca_ev = 0.0
        acca_live = []
        for ab in acca_bets:
            leg = next(((f, s) for (f, s) in ab["legs"] if f == fx), None)
            if leg is None or leg[1] != X:
                continue
            p_other = 1.0
            for (f2, s2) in ab["legs"]:
                if f2 != fx:
                    p_other *= (slate[f2]["p"].get(s2) or 0.0)
            ev = ab["profit"] * p_other
            acca_ev += ev
            acca_live.append({"label": ab["label"], "kind": "acca",
                              "profit_if_all": round(ab["profit"], 2),
                              "p_other_legs": round(p_other, 4),
                              "ev": round(ev, 2)})
        net = direct + acca_ev
        prob = d["p"].get(X) or 0.0
        rows.append({
            "outcome": X, "prob": round(prob, 4),
            "direct_pnl": round(direct, 2), "acca_ev": round(acca_ev, 2),
            "net_pnl": round(net, 2),
            "blindspot": bool(has_bets and net <= BLINDSPOT_NET_FLOOR
                              and prob >= BLINDSPOT_MIN_PROB),
            "live": live + acca_live,
        })
    # plug suggestions for blind-spot outcomes
    for r in rows:
        if r["blindspot"]:
            r["plug"] = _plug_for(fx, r["outcome"], r["prob"], fx_odds)
    max_win = max((r["net_pnl"] for r in rows), default=0.0)
    stake_at_risk = round(sum(rb["stake"] for rb in result_bets
                              if rb["fx"] == fx and not rb["free"]), 2)
    best = max(rows, key=lambda r: r["net_pnl"])["outcome"] if rows else None
    worst = min(rows, key=lambda r: r["net_pnl"])["outcome"] if rows else None
    return {
        "fixture": fx, "kickoff": d["kickoff"], "results": rows,
        "events": [_event_view(e) for e in events],
        "summary": {"max_win": round(max_win, 2), "stake_at_risk": stake_at_risk,
                    "best_outcome": best, "worst_outcome": worst,
                    "n_blindspots": sum(1 for r in rows if r["blindspot"])},
    }


def _event_view(e):
    return {
        "type": e["type"], "selection": e["selection"],
        "stake": round(e["stake"], 2), "potential_profit": round(e["profit"], 2),
        "free": e["free"], "odds": round(e["odds"], 2),
    }


def _plug_for(fx, outcome, prob, fx_odds):
    """Best current price for ``outcome`` + whether plugging is worthwhile."""
    venue_prices = fx_odds.get(outcome) or {}
    if not venue_prices:
        return {"available": False,
                "note": "no live market price found for this outcome"}
    best_venue = max(venue_prices, key=venue_prices.get)
    best_odds = venue_prices[best_venue]
    ev = prob * best_odds - 1.0
    if ev > 0:
        rec = "PLUG: +EV to back (%.1f%% edge) — fills the gap and adds value" % (ev * 100)
    elif ev > -0.05:
        rec = "marginal: near-fair hedge; plug only if you want lower variance"
    else:
        rec = "LEAVE UNHEDGED: plugging costs %.1f%% EV — take the variance" % (-ev * 100)
    return {"available": True, "outcome": outcome, "best_venue": best_venue,
            "best_odds": round(best_odds, 2), "model_prob": round(prob, 4),
            "ev_pct": round(ev * 100, 1), "recommendation": rec}


def _portfolio_scenarios(slate, result_bets, acca_bets):
    """Joint result-scenario P&L distribution over the slate."""
    fxs = list(slate.keys())
    if not fxs:
        return ({"ev": 0.0, "best": 0.0, "worst": 0.0, "p_profit": 0.0,
                 "p_loss": 0.0, "p_big_win": 0.0, "n_scenarios": 0},
                {"worst_states": [], "narrative": "no upcoming fixtures"})
    scns = []
    for combo in itertools.product(
            *[[slate[f]["home"], "Draw", slate[f]["away"]] for f in fxs]):
        sel = dict(zip(fxs, combo))
        p = 1.0
        for f in fxs:
            p *= (slate[f]["p"].get(sel[f]) or 0.0)
        pnl = 0.0
        for rb in result_bets:
            if sel[rb["fx"]] == rb["sel"]:
                pnl += rb["profit"]
            elif not rb["free"]:
                pnl -= rb["stake"]
        for ab in acca_bets:
            if ab["off"]:
                continue
            if all(sel[f] == s for (f, s) in ab["legs"]):
                pnl += ab["profit"]
        scns.append((p, pnl, sel))
    ev = sum(p * pnl for p, pnl, _ in scns)
    best = max(pnl for _, pnl, _ in scns)
    worst = min(pnl for _, pnl, _ in scns)
    p_profit = sum(p for p, pnl, _ in scns if pnl > 0.5)
    p_loss = sum(p for p, pnl, _ in scns if pnl < -0.5)
    p_big = sum(p for p, pnl, _ in scns if pnl >= 50)
    worst_states = [
        {"pnl": round(pnl, 2), "prob": round(p, 4),
         "results": [sel[f] for f in fxs]}
        for p, pnl, sel in sorted(scns, key=lambda x: x[1])[:5]
    ]
    big_states = [
        {"pnl": round(pnl, 2), "prob": round(p, 4),
         "results": [sel[f] for f in fxs]}
        for p, pnl, sel in sorted(scns, key=lambda x: -x[1])[:3]
    ]
    # upside concentration: how much of the win-probability rides the favourites
    narrative = (
        "Upside is concentrated: the biggest payouts (£%.0f best case) need the "
        "favourites to land together, while %.0f%% of result-states leave the "
        "book down. Downside is driven by the real-money singles, not the free "
        "accas." % (best, p_loss * 100)
    )
    portfolio = {
        "ev": round(ev, 2), "best": round(best, 2), "worst": round(worst, 2),
        "p_profit": round(p_profit, 4), "p_loss": round(p_loss, 4),
        "p_big_win": round(p_big, 4), "n_scenarios": len(scns),
    }
    correlation = {"worst_states": worst_states, "best_states": big_states,
                   "narrative": narrative}
    return portfolio, correlation


def _collect_blindspots(fixtures_out):
    out = []
    for fx in fixtures_out:
        for r in fx["results"]:
            if r["blindspot"]:
                out.append({
                    "fixture": fx["fixture"], "outcome": r["outcome"],
                    "prob": r["prob"], "net_pnl": r["net_pnl"],
                    "plug": r.get("plug"),
                })
    out.sort(key=lambda b: -b["prob"])
    return out
