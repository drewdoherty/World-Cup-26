"""Unified pre-match EV sweep: model vs books (h2h + derivatives) + Polymarket.

Pulls the freshest odds for upcoming fixtures, prices every market the models
can price natively (1X2, BTTS, O/U incl. alternates, DNB), compares against
best available book prices and Polymarket, and prints an edge-sorted report.

Derivative model probabilities come from the scoreline cards, which are
reconciled to the blended 1X2 — so every number here is consistent with the
headline card.

Usage: ./.venv/bin/python scripts/wca_event_ev.py [--hours-ahead 30]
Credit cost: 1 (h2h all books) + 4 per event (btts,draw_no_bet,totals,
alternate_totals via the event-odds endpoint).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


EVENT_MARKETS = "btts,draw_no_bet,totals,alternate_totals"
MIN_EDGE = 0.02
PM_TAKER_FEE_RATE = 0.03  # fee = rate * p * (1-p) per share


def fee_adj_pm_edge(model_p: float, price: float) -> float:
    """Edge buying YES at `price` (per $ spent), net of taker fee."""
    if price <= 0 or price >= 1:
        return float("-inf")
    fee_per_dollar = PM_TAKER_FEE_RATE * price * (1 - price) / price
    return model_p / price - 1 - fee_per_dollar


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-ahead", type=float, default=30.0)
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()
    _load_dotenv(args.env)

    import datetime as dt

    import requests

    from wca.card import build_card, build_score_cards, fit_models, PoolConfig
    from wca.data import polymarket as pm
    from wca.data import theoddsapi
    from wca.data.results import load_results

    now = dt.datetime.utcnow()
    cutoff = (now + dt.timedelta(hours=args.hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("fitting models...", flush=True)
    results = load_results("data/raw/results.csv")
    models = fit_models(results)

    odds, quota = theoddsapi.get_odds("soccer_fifa_world_cup", regions="uk", markets="h2h")
    odds = odds[odds["commence_time"] < cutoff]
    events = odds[["event_id", "home_team", "away_team", "commence_time"]].drop_duplicates()

    pools = [PoolConfig(name="books", bankroll=1000.0)]
    recs = build_card(models, odds, pools, fixtures_meta=results, min_edge=-1.0)
    cards = build_score_cards(models, odds, fixtures_meta=results)
    by_fixture = {(c.home, c.away): c for c in cards}

    key = os.environ["ODDS_API_KEY"]
    rows: List[Dict[str, Any]] = []

    # ---- 1X2 vs best book (no-vig venues need commission adj; flag book) ----
    best_1x2: Dict[Any, Dict[str, Any]] = {}
    for r in recs:
        k = (r.match_desc, r.selection)
        if k not in best_1x2 or r.best_odds > best_1x2[k]["odds"]:
            best_1x2[k] = {"odds": r.best_odds, "book": r.best_book, "model": r.model_prob}
    for (match, sel), d in best_1x2.items():
        eff = d["odds"]
        if "betfair_ex" in (d["book"] or "") or "smarkets" in (d["book"] or "") or "matchbook" in (d["book"] or ""):
            eff = 1 + (d["odds"] - 1) * 0.94  # 6% commission until July
        edge = d["model"] * eff - 1
        rows.append({"market": "1X2", "fixture": match, "selection": sel,
                     "model_p": d["model"], "price": d["odds"], "book": d["book"],
                     "edge": edge})

    # ---- derivatives per event ----
    for _, ev in events.iterrows():
        eid, home, away = ev["event_id"], ev["home_team"], ev["away_team"]
        card = None
        for (h, a), c in by_fixture.items():
            if h in home or home in h or h == home:
                if a in away or away in a or a == away:
                    card = c
                    break
        if card is None:
            continue
        url = ("https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/events/%s/odds"
               "?apiKey=%s&regions=uk&markets=%s&oddsFormat=decimal" % (eid, key, EVENT_MARKETS))
        try:
            data = requests.get(url, timeout=20).json()
        except Exception:
            continue
        if not isinstance(data, dict) or "bookmakers" not in data:
            continue

        ou = card.over_under or {}
        p_btts = card.btts
        p_h, p_d, p_a = card.one_x_two  # tuple (home, draw, away), probs 0..1
        fixture = "%s vs %s" % (home, away)

        model_for: Dict[Any, Optional[float]] = {}
        if p_btts is not None:
            model_for[("btts", "Yes")] = p_btts
            model_for[("btts", "No")] = 1 - p_btts
        for line, pv in ou.items():
            try:
                lf = float(line)
                over_p = float(pv[0])  # (over, under) tuple
            except Exception:
                continue
            model_for[("totals", "Over", lf)] = over_p
            model_for[("totals", "Under", lf)] = 1 - over_p
            model_for[("alternate_totals", "Over", lf)] = over_p
            model_for[("alternate_totals", "Under", lf)] = 1 - over_p
        if p_h is not None and p_a is not None and (p_h + p_a) > 0:
            model_for[("draw_no_bet", home)] = p_h / (p_h + p_a)
            model_for[("draw_no_bet", away)] = p_a / (p_h + p_a)

        best: Dict[Any, Dict[str, Any]] = {}
        for bk in data.get("bookmakers", []):
            is_exch = bk["key"] in ("betfair_ex_uk", "smarkets", "matchbook")
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    mk = m["key"]
                    name = o.get("name")
                    point = o.get("point")
                    k = (mk, name, float(point)) if point is not None else (mk, name)
                    price = float(o.get("price") or 0)
                    eff = 1 + (price - 1) * 0.94 if is_exch else price
                    if k in model_for and (k not in best or eff > best[k]["eff"]):
                        best[k] = {"price": price, "eff": eff, "book": bk["key"]}
        for k, mp in model_for.items():
            if mp is None or k not in best:
                continue
            b = best[k]
            edge = mp * b["eff"] - 1
            sel = " ".join(str(x) for x in k[1:])
            rows.append({"market": k[0], "fixture": fixture, "selection": sel,
                         "model_p": mp, "price": b["price"], "book": b["book"],
                         "edge": edge})

        # ---- Polymarket 3-way for this fixture ----
        try:
            pm_evs = pm.find_world_cup_markets()
        except Exception:
            pm_evs = []
        hl, al = home.lower(), away.lower()
        for pev in pm_evs or []:
            t = (pev.get("title") or "").lower()
            if (hl.split()[0] in t or hl in t) and (al.split()[-1] in t or al in t) and " vs" in t:
                full = pm.get_event(pev.get("id"))
                for m in full.get("markets", []):
                    q = (m.get("question") or "").lower()
                    import json as _j
                    try:
                        outs = _j.loads(m.get("outcomes") or "[]")
                        prices = [float(x) for x in _j.loads(m.get("outcomePrices") or "[]")]
                        yes = dict(zip(outs, prices)).get("Yes")
                    except Exception:
                        yes = None
                    if not yes or yes <= 0.01 or yes >= 0.99:
                        continue
                    mp = None
                    if "draw" in q:
                        mp = p_d
                    elif hl in q or hl.split()[0] in q:
                        mp = p_h
                    elif al in q or al.split()[-1] in q:
                        mp = p_a
                    if mp is None:
                        continue
                    edge = fee_adj_pm_edge(mp, yes)
                    rows.append({"market": "pm_moneyline", "fixture": fixture,
                                 "selection": q[:44], "model_p": mp, "price": yes,
                                 "book": "polymarket", "edge": edge})
                break

    rows.sort(key=lambda r: r["edge"], reverse=True)
    print("\n%-16s %-34s %-26s %7s %7s %7s  %s" %
          ("MARKET", "FIXTURE", "SELECTION", "MODEL", "PRICE", "EDGE", "BOOK"))
    for r in rows:
        if r["edge"] < -0.10:
            continue
        star = " *" if r["edge"] >= MIN_EDGE else ""
        price = ("%.3f" % r["price"]) if r["price"] < 1 else ("%.2f" % r["price"])
        print("%-16s %-34s %-26s %6.1f%% %7s %+6.1f%%  %s%s" %
              (r["market"], r["fixture"][:34], r["selection"][:26],
               r["model_p"] * 100, price, r["edge"] * 100, r["book"], star))
    print("\n* = clears %.0f%% edge floor   quota remaining: %s" % (MIN_EDGE * 100, quota.remaining))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
