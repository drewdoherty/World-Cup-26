"""Live cross-venue arbitrage sweep for upcoming World Cup fixtures.

Pulls h2h across all UK books, per-event derivative markets (btts, totals,
draw_no_bet, alternate_totals, h2h_lay) and Polymarket match-winner quotes,
runs every deterministic detector in ``wca.arb`` and prints a ranked table of
guaranteed-profit opportunities with the stake split for a configurable
bankroll.  Settlement keys are enforced so only same-settlement legs are
paired.  Writes the methodology doc on each run.

Usage: ./.venv/bin/python scripts/wca_arb.py [--hours-ahead 48]
                                             [--bankroll 1000]
                                             [--min-profit 0.005]
Credit cost: 1 (h2h all books) + 5 per event (event-odds endpoint).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

EVENT_MARKETS = "h2h_lay,btts,totals,draw_no_bet,alternate_totals"

METHODOLOGY = """# Arbitrage methodology

## What an arb is
A back-only arbitrage exists across a set of mutually-exclusive and exhaustive
outcomes when the sum of the inverse *net* decimal prices is below 1:

    sum_i (1 / net_i) < 1   =>   guaranteed return = (1 / sum) - 1

Stakes are split in proportion to ``1/net_i`` so every outcome pays out the
same amount; the stake fractions sum to the bankroll.

## Net prices (commission & fees)
- Plain bookmakers: net = raw decimal odds.
- Exchanges (back side): net = 1 + (odds - 1) * (1 - commission). Betfair is
  currently 6% (2% from July), Smarkets/Matchbook 2%.
- Polymarket YES at price p: one share costs ``p + 0.03*p*(1-p)`` and pays 1,
  so net decimal = 1 / cost. (Maker fee is 0.)

## The settlement-key guard (the fake-arb trap)
Every market carries an explicit *settlement key* describing what it resolves
on. Two prices may be paired ONLY if their settlement keys are identical.

- UK 1X2, Betfair Match Odds, h2h_lay, and Polymarket match-winner all settle
  on 90 minutes + stoppage in the group stage -> key ``1x2_90min``. Backing /
  laying across these is valid arb.
- BTTS settles 90-min -> ``btts_90min``.
- Totals settle 90-min, keyed per line -> ``totals_2.5_90min`` etc. Only the
  same line is paired (Over 2.5 vs Under 2.5, never vs Under 3.5).
- Draw-no-bet -> ``dnb_90min``.
- "To qualify" / tournament outright markets include extra time and penalties.
  Their settlement key is ``None`` and they are REFUSED for pairing against any
  90-minute market.

## Detectors
1. ``find_cross_book_arbs`` - best net back per outcome across books within a
   single (event, market, line); flags 3-way (1X2) and complementary 2-way
   (BTTS / DNB / totals) arbs.
2. ``find_pm_book_arbs`` -
   - PM-internal: YES + NO priced so both shares cost < 1 after fee.
   - Book-vs-PM 3-way: back two 1X2 outcomes at the book + the third via a PM
     YES share, only when the PM market settles ``1x2_90min``.

Results are filtered by ``min_profit`` (default 0.5%) and ranked by guaranteed
return.

## Liquidity caveat (an arb you cannot match is not an arb)
Exchange (Betfair/Smarkets/Matchbook) and Polymarket prices are only real if
there is size available at the quoted price. A reported back/lay or PM leg that
cannot actually be matched for the required stake is NOT a realisable arb. The
detector works from top-of-book prices and does NOT model available size, so
every leg sourced from an exchange or Polymarket must be liquidity-checked
manually before staking. Soft books also void/limit obvious arbs.
"""


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


def _write_doc() -> None:
    out = Path("docs/research/arb_methodology.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(METHODOLOGY)


def _build_pm_quotes(home: str, away: str, event_id: str) -> List[Dict[str, Any]]:
    """Fetch Polymarket match-winner quotes for one fixture (90-min settlement)."""
    from wca.data import polymarket as pm

    quotes: List[Dict[str, Any]] = []
    try:
        pm_evs = pm.find_world_cup_markets()
    except Exception:
        return quotes
    hl, al = home.lower(), away.lower()
    for pev in pm_evs or []:
        t = (pev.get("title") or "").lower()
        if " vs" not in t:
            continue
        if not ((hl.split()[0] in t or hl in t) and (al.split()[-1] in t or al in t)):
            continue
        try:
            full = pm.get_event(pev.get("id"))
        except Exception:
            continue
        for m in full.get("markets", []):
            q = (m.get("question") or "").lower()
            try:
                outs = json.loads(m.get("outcomes") or "[]")
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                pmap = dict(zip(outs, prices))
            except Exception:
                continue
            yes = pmap.get("Yes")
            no = pmap.get("No")
            if yes is None:
                continue
            # Map the question to a 1X2 outcome name (book convention).
            outcome: Optional[str] = None
            if "draw" in q:
                outcome = "Draw"
            elif hl in q or hl.split()[0] in q:
                outcome = home
            elif al in q or al.split()[-1] in q:
                outcome = away
            if outcome is None:
                continue
            quotes.append({
                "event_id": event_id,
                "settlement_key": "1x2_90min",
                "outcome": outcome,
                "yes_price": yes,
                "no_price": no,
                "question": m.get("question"),
            })
    return quotes


def _fmt_legs(legs: List[Dict[str, Any]], bankroll: float) -> str:
    parts = []
    for leg in legs:
        stake = leg.get("stake_fraction", 0.0) * bankroll
        parts.append(
            "%s @%s [%s] net=%.3f stake=%.2f" % (
                leg.get("outcome", "?"),
                ("%.2f" % leg["raw_odds"]) if "raw_odds" in leg else "-",
                leg.get("book", "?"),
                leg.get("net_odds", 0.0),
                stake,
            )
        )
    return " | ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-ahead", type=float, default=48.0)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--min-profit", type=float, default=0.005)
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()
    _load_dotenv(args.env)
    _write_doc()

    import pandas as pd
    import requests

    from wca import arb
    from wca.data import theoddsapi

    cutoff = (dt.datetime.utcnow() + dt.timedelta(hours=args.hours_ahead)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    odds, quota = theoddsapi.get_odds("soccer_fifa_world_cup", regions="uk", markets="h2h")
    if not odds.empty:
        odds = odds[odds["commence_time"].astype(str) < cutoff]
    events = odds[["event_id", "home_team", "away_team", "commence_time"]].drop_duplicates()
    print("fixtures in window: %d" % len(events))

    key = os.environ.get("ODDS_API_KEY", "")
    all_rows = odds.to_dict("records")

    # Per-event derivative markets.
    for _, ev in events.iterrows():
        eid, home, away = ev["event_id"], ev["home_team"], ev["away_team"]
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/events/%s/odds"
            "?apiKey=%s&regions=uk&markets=%s&oddsFormat=decimal" % (eid, key, EVENT_MARKETS)
        )
        try:
            data = requests.get(url, timeout=20).json()
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for bk in data.get("bookmakers", []):
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    all_rows.append({
                        "event_id": eid, "home_team": home, "away_team": away,
                        "market": m.get("key"), "outcome_name": o.get("name"),
                        "outcome_point": o.get("point"),
                        "decimal_odds": o.get("price"),
                        "bookmaker_key": bk.get("key"),
                    })

    full_df = pd.DataFrame(all_rows)

    pm_quotes: List[Dict[str, Any]] = []
    for _, ev in events.iterrows():
        pm_quotes.extend(_build_pm_quotes(ev["home_team"], ev["away_team"], ev["event_id"]))

    arbs = arb.find_cross_book_arbs(full_df, min_profit=args.min_profit)
    arbs += arb.find_pm_book_arbs(full_df, pm_quotes, min_profit=args.min_profit)
    ranked = arb.rank_arbs(arbs, min_profit=args.min_profit)

    print("\n=== ARBS (>= %.2f%%) ===" % (args.min_profit * 100))
    print("NB: exchange/Polymarket legs assume top-of-book size is available; "
          "liquidity-check every such leg before staking - an unmatched leg is "
          "not an arb.")
    if not ranked:
        print("none found")
    for a in ranked:
        fixture = "%s vs %s" % (a.get("home_team", "?"), a.get("away_team", "?"))
        print("\n[%s] %.3f%%  %s  %s  (%s)" % (
            a["kind"], a["profit_pct"] * 100, fixture,
            a.get("market") or a.get("question") or "", a.get("settlement_key"),
        ))
        print("  " + _fmt_legs(a["legs"], args.bankroll))

    if quota is not None:
        print("\nquota remaining: %s (used %s)" % (quota.remaining, quota.used))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
