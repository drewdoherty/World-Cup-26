#!/usr/bin/env python
"""Historical arb analysis from ALREADY-LOGGED odds snapshots (no paid pulls).

Reuses data/raw/snapshots/oddsapi_h2h_uk_*.json (Jun-11 tournament start on).
Sparse, credit-efficient cadence: at most N snapshots per UTC match-day (odds
drift slowly pre-match, and these are already on disk — the sparseness is for
signal clarity + a sane go-forward cadence, not cost). Runs the existing
cross-book arb detector, categorises each detected arb by venue-pair TYPE, and
tests the hypothesis: are arbs mostly sportsbook↔exchange rather than
exchange-vs-exchange?

Usage: python scripts/wca_arb_history.py [--per-day 3] [--min-profit 0.0]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import arb  # noqa: E402
from wca.data import theoddsapi  # noqa: E402

_EXCHANGES = {"betfair_ex_uk", "betfair_ex_eu", "smarkets", "matchbook"}


def _venue_class(book: str) -> str:
    return "exchange" if book in _EXCHANGES else "sportsbook"


def _category(legs) -> str:
    classes = {_venue_class(l.get("book", "")) for l in legs}
    if classes == {"exchange"}:
        return "exchange-only"
    if classes == {"sportsbook"}:
        return "sportsbook-only"
    return "mixed (sportsbook+exchange)"


def _sparse(paths, per_day):
    by_day = {}
    for p in sorted(paths):
        base = os.path.basename(p)
        day = base.split("_")[-1][:8]  # YYYYMMDD
        by_day.setdefault(day, []).append(p)
    picked = []
    for day, ps in sorted(by_day.items()):
        if len(ps) <= per_day:
            picked.extend(ps)
        else:
            step = len(ps) / per_day
            picked.extend(ps[int(i * step)] for i in range(per_day))
    return picked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-day", type=int, default=3)
    ap.add_argument("--min-profit", type=float, default=0.0)
    ap.add_argument("--glob", default="data/raw/snapshots/oddsapi_h2h_uk_*.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    import pandas as pd
    paths = _sparse(glob.glob(args.glob), args.per_day)
    cats = {"mixed (sportsbook+exchange)": 0, "exchange-only": 0, "sportsbook-only": 0}
    true_arbs = 0
    best_leg_provider = {"sportsbook": 0, "exchange": 0}
    overrounds = []
    near = []  # tightest near-arbs with leg composition
    for p in paths:
        try:
            events = json.load(open(p))
        except (OSError, json.JSONDecodeError):
            continue
        df = pd.DataFrame(theoddsapi._parse_events(events))
        if df.empty:
            continue
        # True back-only arbs (rare): categorise by venue-pair type.
        for a in arb.find_cross_book_arbs(df, min_profit=args.min_profit):
            true_arbs += 1
            cats[_category(a.get("legs", []))] += 1
        # Best NET leg per outcome per event → who supplies the arb-creating price.
        h = df[df["market"] == "h2h"]
        for (eid, ), g in h.groupby(["event_id"]):
            best = {}
            for _, r in g.iterrows():
                n = r["outcome_name"]
                book = r.get("bookmaker_key") or ""
                try:
                    net = arb.effective_back(float(r["decimal_odds"]), book)
                except (TypeError, ValueError):
                    continue
                if n not in best or net > best[n][0]:
                    best[n] = (net, book)
            if len(best) < 2:
                continue
            for net, book in best.values():
                best_leg_provider[_venue_class(book)] += 1
            orr = sum(1.0 / v[0] for v in best.values())
            overrounds.append(orr)
            if orr < 1.02:  # tightest near-arbs
                near.append({"event": "%s vs %s" % (g.iloc[0]["home_team"], g.iloc[0]["away_team"]),
                             "overround": round(orr, 4),
                             "best_legs": {n: {"book": b, "class": _venue_class(b)} for n, (x, b) in best.items()}})
    near.sort(key=lambda x: x["overround"])
    tot_prov = best_leg_provider["sportsbook"] + best_leg_provider["exchange"] or 1
    result = {
        "snapshots_scanned": len(paths),
        "true_back_only_arbs": true_arbs,
        "true_arbs_by_category": cats,
        "best_leg_provider": best_leg_provider,
        "best_leg_sportsbook_pct": round(100.0 * best_leg_provider["sportsbook"] / tot_prov, 1),
        "min_overround_seen": round(min(overrounds), 4) if overrounds else None,
        "median_overround": round(sorted(overrounds)[len(overrounds) // 2], 4) if overrounds else None,
        "tightest_near_arbs": near[:5],
        "hypothesis": _verdict_provider(true_arbs, best_leg_provider),
    }
    print(json.dumps(result, indent=2))
    if args.out:
        json.dump(result, open(args.out, "w"), indent=2)
    return 0


def _verdict_provider(true_arbs, prov) -> str:
    tot = prov["sportsbook"] + prov["exchange"] or 1
    sb_pct = 100.0 * prov["sportsbook"] / tot
    base = ("%d true back-only arbs in sample (markets efficient). " % true_arbs)
    verb = "CONFIRMED" if sb_pct >= 60 else ("MIXED" if sb_pct >= 40 else "REJECTED")
    return base + ("%s: sportsbooks supply %.0f%% of the best (arb-creating) legs "
                   "vs exchanges %.0f%% — so any lock would be sportsbook<->exchange, "
                   "not exchange-vs-exchange." % (verb, sb_pct, 100.0 - sb_pct))


if __name__ == "__main__":
    raise SystemExit(main())
