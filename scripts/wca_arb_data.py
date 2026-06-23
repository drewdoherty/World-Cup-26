#!/usr/bin/env python
"""Generate the arbitrage site feed (``site/arb_data.json``) — monitoring-only.

Read-only: pulls Betfair Exchange odds via The Odds API + Polymarket prices,
computes FX-adjusted risk-free opportunities, writes the JSON the Arb tab
renders. NO execution, NO fund movement. The live calls are best-effort and the
script degrades to an empty feed rather than failing.

Usage
-----
    python scripts/wca_arb_data.py [--out site/arb_data.json] [--offline]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import arbdata, fx  # noqa: E402


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_history(path: str):
    try:
        with open(path) as fh:
            return (json.load(fh).get("hypothetical") or {}).get("points") or []
    except (OSError, json.JSONDecodeError):
        return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the WCA arbitrage feed (monitoring-only).")
    ap.add_argument("--out", default="site/arb_data.json")
    ap.add_argument("--offline", action="store_true",
                    help="Skip live odds/PM/FX calls; emit an empty (shaped) feed.")
    args = ap.parse_args(argv)

    betfair_rows: list = []
    smarkets_rows: list = []
    sportsbook_rows: list = []
    smarkets_grade = "monitoring-grade"
    pm_quotes: dict = {}
    fxr = fx.FxRate(fx.FALLBACK_USD_PER_GBP, "fallback")

    if not args.offline:
        fxr = fx.get_gbp_usd()  # bounded; never raises
        try:
            from wca.data import betfair as bf
            df, _ = bf.betfair_odds(markets="h2h")
            betfair_rows = df.to_dict("records") if df is not None and not df.empty else []
        except Exception as exc:  # noqa: BLE001 — never fail the feed
            print("betfair/oddsapi fetch skipped: %s" % exc, file=sys.stderr)
        try:
            from wca.data import smarkets as sm
            sdf, smarkets_grade = sm.smarkets_odds(markets="h2h")
            smarkets_rows = sdf.to_dict("records") if sdf is not None and not sdf.empty else []
        except Exception as exc:  # noqa: BLE001
            print("smarkets fetch skipped: %s" % exc, file=sys.stderr)
        try:
            from wca.data import theoddsapi
            odf, _ = theoddsapi.get_odds("soccer_fifa_world_cup", regions="uk", markets="h2h")
            _EX = {"betfair_ex_uk", "betfair_ex_eu", "smarkets", "matchbook"}
            if odf is not None and not odf.empty:
                sportsbook_rows = odf[~odf["bookmaker_key"].isin(_EX)].to_dict("records")
        except Exception as exc:  # noqa: BLE001
            print("sportsbook fetch skipped: %s" % exc, file=sys.stderr)
        # PM quotes wiring is intentionally left to the live op (needs the PM
        # event fetch + canonical pairing); empty here keeps the feed honest.

    history = _load_history(args.out)
    data = arbdata.build_arb_data(
        betfair_rows=betfair_rows, smarkets_rows=smarkets_rows,
        sportsbook_rows=sportsbook_rows, smarkets_grade=smarkets_grade,
        pm_quotes=pm_quotes, fx_usd_per_gbp=fxr.usd_per_gbp, fx_source=fxr.source,
        now_utc=_now_utc(), history=history,
    )
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print("%s: %d arb(s), fx=%s (%s)" % (args.out, len(data["arbs"]), fxr.usd_per_gbp, fxr.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
