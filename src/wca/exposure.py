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
# Result markets whose single bets settle directly on 1X2.  Matched case- and
# punctuation-insensitively so "Full Time Result" / "Full-time result" / "1X2"
# all resolve — a mislabelled result bet must never leak into the event bucket
# (that is exactly how a covered outcome reads as a blind spot).
_RESULT_MARKETS = {
    "full time result", "fulltime result", "match odds", "match winner",
    "match result", "h2h", "1x2", "result", "to win", "winner",
}
# Team-name aliases seen in bet descriptions vs the model fixture spelling.
_ALIAS = {"Türkiye": "Turkey", "Turkiye": "Turkey", "Korea Republic": "South Korea"}


def _canon_market(market: str) -> str:
    """Normalise a market label for result-market matching."""
    return re.sub(r"[^a-z0-9]+", " ", (market or "").lower()).strip()


def _is_result_market(market: str) -> bool:
    return _canon_market(market) in _RESULT_MARKETS


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


# Split a "Home vs Away" / "Home v Away" fixture string. Both spellings occur in
# the wild (model feed uses "vs", some card/bet descriptions use a single "v").
# Missing this is a SILENT floor leak: a real single whose desc reads "A v B"
# fails to map and vanishes from both the worst case and stake-at-risk.
_FIXTURE_VS = re.compile(r"\s+vs?\s+", re.IGNORECASE)


def _split_fixture(s: str) -> Optional[Tuple[str, str]]:
    parts = _FIXTURE_VS.split((s or "").strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


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
        ha = _split_fixture(fixture)
        if not ha:
            continue
        if not _is_future_or_inplay(f.get("kickoff"), now):
            continue
        home, away = ha
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


def _canon_fixture(s: str) -> str:
    """Alias-normalise a 'Home vs Away' string so spelling variants match.

    Handles both " vs " and a single " v ", plus team aliases:
    'Australia v Türkiye' -> 'australia vs turkey'.
    """
    ha = _split_fixture(s)
    if ha:
        return ("%s vs %s" % (_team_key(ha[0]), _team_key(ha[1]))).lower()
    return (s or "").strip().lower()


def _map_single(bet: Dict[str, Any], slate: Dict[str, Dict[str, Any]]
                ) -> Optional[Tuple[str, str]]:
    """Map a result single to ``(fixture, winning_selection)`` or None."""
    md = (bet.get("match_desc") or "").strip()
    fx = md if md in slate else None
    if fx is None:
        md_canon = _canon_fixture(md)
        for k in slate:
            if _canon_fixture(k) == md_canon:
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
    results: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the full exposure feed.

    Parameters
    ----------
    bets:
        Open bets (ledger rows as dicts): match_desc, market, selection,
        decimal_odds, stake, source, account, status, model_prob, ev.
    model_fixtures:
        ``data/model_predictions.json`` ``fixtures`` list.
    odds_index:
        ``{fixture: {outcome: {venue: decimal_odds}}}`` for plug suggestions.
    now_utc:
        Display timestamp for the feed header.
    results:
        Optional ``{fixture: {"outcome": <home|"Draw"|away>, "score": "h-a"}}``
        for fixtures that have already finished.  When supplied the headline
        floor is recomputed *conditional on results so far*: settled fixtures
        are pinned, accas whose settled leg lost are dead, and event punts on
        finished games are settled against the actual score.  This is the number
        that moves the instant a leg breaks — the live floor, not the pre-slate
        one.
    """
    # Restrict the risk slate to future / in-play games (drop finished ones), so
    # blind spots and per-fixture exposure never cover games already played.
    slate = build_slate(model_fixtures, now=_parse_dt(now_utc))
    team2fx = _team_to_fixture(slate)
    odds_index = odds_index or {}
    results = _normalise_results(results, slate)

    result_bets: List[Dict[str, Any]] = []
    acca_bets: List[Dict[str, Any]] = []
    event_bets: Dict[str, List[Dict[str, Any]]] = {}
    event_list: List[Dict[str, Any]] = []
    unmapped: List[str] = []
    unmapped_real_stake = 0.0  # real money on result singles we couldn't map

    for b in bets:
        if str(b.get("status") or "open") != "open":
            continue
        market = b.get("market") or ""
        if _canon_market(market) == "acca":
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
        elif _is_result_market(market):
            m = _map_single(b, slate)
            if m:
                result_bets.append({
                    "fx": m[0], "sel": m[1], "profit": _profit(b),
                    "free": _is_free(b), "stake": float(b["stake"]),
                    "label": b.get("selection"), "odds": float(b["decimal_odds"]),
                })
            else:
                unmapped.append("%s / %s" % (b.get("match_desc"), b.get("selection")))
                if not _is_free(b):
                    # Never let real money disappear: an unmappable real single
                    # is surfaced as off-slate exposure rather than silently
                    # dropped from the risk accounting.
                    unmapped_real_stake += float(b["stake"])
        else:
            md = (b.get("match_desc") or "")
            fx = None
            md_canon = _canon_fixture(md)
            for k in slate:
                kc = _canon_fixture(k)
                if kc in md_canon or md_canon in kc:
                    fx = k
                    break
            rec = {
                "type": market, "selection": b.get("selection"),
                "stake": float(b["stake"]), "profit": _profit(b),
                "free": _is_free(b), "odds": float(b["decimal_odds"]),
                "ev": (float(b["ev"]) if b.get("ev") is not None else None),
                "fx": fx,
            }
            if fx is None:
                rec["match"] = md
            event_bets.setdefault(fx or "(off-slate)", []).append(rec)
            event_list.append(rec)

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
    portfolio, correlation = _portfolio_scenarios(
        slate, result_bets, acca_bets, event_list, results)
    blindspots = _collect_blindspots(fixtures_out)

    # Real-money positions that the slate scenarios cannot see directly:
    # off-slate accas/events still cost real money if they lose.
    offslate_real = round(
        sum(a["stake"] for a in acca_bets if a["off"] and not a["free"])
        + sum(e["stake"] for e in event_bets.get("(off-slate)", []) if not e["free"])
        + unmapped_real_stake,
        2)

    return {
        "meta": {"generated": now_utc},
        "portfolio": portfolio,
        "correlation": correlation,
        "fixtures": fixtures_out,
        "blindspots": blindspots,
        "unmapped": unmapped,
        "off_slate_accas": [a["label"] for a in acca_bets if a["off"]],
        "off_slate_events": event_bets.get("(off-slate)", []),
        "real_money_offslate": offslate_real,
        "settled": results,
    }


def _normalise_results(results, slate):
    """Coerce a results map into ``{fixture: {"outcome", "score"}}``.

    Accepts either ``{fixture: "home"|"draw"|"away"}`` or
    ``{fixture: {"outcome": ..., "score": "h-a"}}`` and maps home/draw/away to
    the slate's actual team names.  Pending / unknown fixtures are dropped.
    """
    out: Dict[str, Any] = {}
    for fx, val in (results or {}).items():
        if fx not in slate:
            continue
        d = slate[fx]
        if isinstance(val, dict):
            outcome = val.get("outcome")
            score = val.get("score")
        else:
            outcome = val
            score = None
        oc = (outcome or "").strip().lower()
        sel = {"home": d["home"], "draw": "Draw", "away": d["away"]}.get(oc)
        if sel is None:  # already a team name / "Draw", or pending
            if outcome in (d["home"], d["away"], "Draw"):
                sel = outcome
            else:
                continue
        out[fx] = {"outcome": sel, "score": score}
    return out


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
        # A blind spot is decided on HARD CASH (direct real-money P&L), never on
        # the model-conditional acca EV: a free acca that *might* pay does not
        # cover an outcome in cash, and it evaporates the moment any leg misses.
        is_blind = bool(direct <= BLINDSPOT_NET_FLOOR and prob >= BLINDSPOT_MIN_PROB)
        # "soft only" = looks covered once you add acca EV, but is hard-cash thin.
        soft_only = bool(is_blind and net > BLINDSPOT_NET_FLOOR)
        rows.append({
            "outcome": X, "prob": round(prob, 4),
            "direct_pnl": round(direct, 2), "acca_ev": round(acca_ev, 2),
            "net_pnl": round(net, 2), "cash_net": round(direct, 2),
            "blindspot": bool(has_bets and is_blind),
            "soft_only": bool(has_bets and soft_only),
            "live": live + acca_live,
        })
    # plug suggestions for blind-spot outcomes
    for r in rows:
        if r["blindspot"]:
            r["plug"] = _plug_for(fx, r["outcome"], r["prob"], fx_odds)
    max_win = max((r["net_pnl"] for r in rows), default=0.0)
    # Stake at risk on this fixture = EVERY real-money position riding it:
    # result singles AND event/scoreline/prop punts (the latter were omitted
    # before, understating the per-fixture downside).
    stake_at_risk = round(
        sum(rb["stake"] for rb in result_bets if rb["fx"] == fx and not rb["free"])
        + sum(e["stake"] for e in events if not e["free"]), 2)
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


# Scores are written "home-away" with a hyphen ("Brazil 1-0 Morocco", "1-1").
# Hyphen-only (a ":" would let "Bet #2: 1-0" misparse as 2-1) and we take the
# LAST match so leading reference numbers in a label can't be read as the score.
_SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def _parse_score(text: str) -> Optional[Tuple[str, str]]:
    ms = _SCORE_RE.findall(text or "")
    return ms[-1] if ms else None


def _event_won(e, res):
    """Did event bet ``e`` win on a settled fixture with result ``res``?

    Exact-score markets are checked against the actual home-away score; anything
    we cannot *positively* confirm as a win on a finished fixture is treated as a
    loss (conservative — it never inflates the floor).
    """
    market = _canon_market(e.get("type"))
    if market in ("exact score", "correct score"):
        sel = _parse_score(e.get("selection") or "")
        act = _parse_score(res.get("score") or "")
        return bool(sel and act and sel == act)
    return False


def _settle_event(e, results):
    """Realised P&L of an event bet on a SETTLED fixture, else None (still live).

    Free bets: +profit on win, £0 on loss.  Real bets: +profit on win, −stake.
    """
    fx = e.get("fx")
    if not fx or fx not in results:
        return None
    if _event_won(e, results[fx]):
        return e["profit"]
    return 0.0 if e["free"] else -e["stake"]


def _acca_alive(ab, results):
    """An acca is dead once any *settled* leg has lost."""
    for (f, s) in ab["legs"]:
        if f in results and results[f]["outcome"] != s:
            return False
    return True


def _portfolio_scenarios(slate, result_bets, acca_bets, event_bets=None,
                         results=None):
    """Joint result-scenario P&L distribution over the slate.

    The headline ``worst`` is a HARD CASH FLOOR: it counts every real-money
    position — result singles, accumulators (free or real), and sub-1X2 event
    punts (exact scores, scorers, props) — and never leans on model-conditional
    acca EV.  Free bets add profit when they win and cost £0 when they lose.
    Live exact-score / prop punts are assumed to MISS in the floor (their stake
    is at risk); ``ev`` credits their model expectation via the ledger EV field.

    ``results`` pins settled fixtures, kills accas whose settled leg lost, and
    settles event punts on finished games — so the floor is conditional on
    results so far, not the stale pre-kickoff figure.
    """
    results = results or {}
    fxs = list(slate.keys())
    # Only ON-SLATE event punts belong in the slate floor.  Off-slate bets
    # (e.g. a tournament-long "reach R16" position) resolve on a different
    # timeframe and are surfaced separately as real_money_offslate — folding
    # their stake into tonight's floor would wildly overstate the downside.
    event_bets = [e for e in (event_bets or []) if e.get("fx") in slate]

    # --- settle / partition the sub-1X2 event punts --------------------------
    banked_events = 0.0
    live_events = []
    for e in event_bets:
        r = _settle_event(e, results)
        if r is None:
            live_events.append(e)
        else:
            banked_events += r
    event_stake_risk = sum(e["stake"] for e in live_events if not e["free"])
    event_ev = sum((e.get("ev") or 0.0) for e in live_events)

    # --- realised losses from accas already killed by a settled leg ----------
    banked_accas = sum(-ab["stake"] for ab in acca_bets
                       if not ab["free"] and not _acca_alive(ab, results))
    # off-slate real-money accas can't be enumerated — their stake is at risk.
    offslate_acca_risk = sum(ab["stake"] for ab in acca_bets
                             if ab["off"] and not ab["free"] and _acca_alive(ab, results))

    # adj shifts every result-state by the cash we already know plus the punt
    # stakes assumed lost in the floor; it is the same constant for all states.
    adj = banked_events + banked_accas - event_stake_risk - offslate_acca_risk

    # --- total real money that can still be lost -----------------------------
    stake_at_risk = round(
        sum(rb["stake"] for rb in result_bets
            if not rb["free"] and rb["fx"] not in results)
        + sum(ab["stake"] for ab in acca_bets
              if not ab["free"] and _acca_alive(ab, results))
        + event_stake_risk, 2)

    if not fxs:
        worst = round(adj, 2)
        portfolio = {"ev": round(adj + event_ev, 2), "best": round(adj, 2),
                     "worst": worst, "p_profit": 0.0, "p_loss": 0.0,
                     "p_big_win": 0.0, "n_scenarios": 0,
                     "stake_at_risk": stake_at_risk}
        return portfolio, {"worst_states": [], "best_states": [],
                           "narrative": "no upcoming fixtures"}

    def outcomes_for(f):
        if f in results:
            return [results[f]["outcome"]]
        return [slate[f]["home"], "Draw", slate[f]["away"]]

    n_alive_accas = sum(1 for ab in acca_bets
                        if not ab["off"] and _acca_alive(ab, results))
    scns = []
    for combo in itertools.product(*[outcomes_for(f) for f in fxs]):
        sel = dict(zip(fxs, combo))
        p = 1.0
        for f in fxs:
            if f not in results:           # settled legs carry probability 1
                p *= (slate[f]["p"].get(sel[f]) or 0.0)
        core = 0.0
        for rb in result_bets:
            if sel[rb["fx"]] == rb["sel"]:
                core += rb["profit"]
            elif not rb["free"]:
                core -= rb["stake"]
        for ab in acca_bets:
            if ab["off"] or not _acca_alive(ab, results):
                continue
            if all(sel[f] == s for (f, s) in ab["legs"]):
                core += ab["profit"]
            elif not ab["free"]:           # real-money acca loses its stake
                core -= ab["stake"]
        scns.append((p, core + adj, sel))

    # EV is an expectation, NOT a floor: it credits the event punts' model EV
    # (which already nets their stake) and the realised/banked cash — it must
    # NOT also subtract the punt stakes the floor pessimistically writes off.
    ev = sum(p * (core_adj - adj) for p, core_adj, _ in scns) \
        + banked_events + banked_accas + event_ev
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
    conditional = bool(results)
    narrative = (
        "Hard cash floor £%.2f across %d result-states%s — this counts EVERY "
        "real-money position (singles, accas, and £%.2f of exact-score/prop "
        "punts assumed to miss), not just the singles, and never the free-acca "
        "EV. %.0f%% of states finish down; £%.2f of real money is still at risk."
        % (worst, len(scns),
           " (conditional on results so far)" if conditional else "",
           event_stake_risk, p_loss * 100, stake_at_risk)
    )
    portfolio = {
        "ev": round(ev, 2), "best": round(best, 2), "worst": round(worst, 2),
        "p_profit": round(p_profit, 4), "p_loss": round(p_loss, 4),
        "p_big_win": round(p_big, 4), "n_scenarios": len(scns),
        "stake_at_risk": stake_at_risk,
        "event_stake_at_risk": round(event_stake_risk, 2),
        "banked": round(banked_events + banked_accas, 2),
        "alive_accas": n_alive_accas,
        "dead_accas": sum(1 for ab in acca_bets
                          if not ab["off"] and not _acca_alive(ab, results)),
        "conditional": conditional,
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
                    "cash_net": r.get("cash_net", r["net_pnl"]),
                    "acca_ev": r.get("acca_ev", 0.0),
                    "soft_only": r.get("soft_only", False),
                    "plug": r.get("plug"),
                })
    out.sort(key=lambda b: -b["prob"])
    return out
