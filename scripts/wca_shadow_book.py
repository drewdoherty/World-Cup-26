#!/usr/bin/env python
"""Run, settle, and report the isolated multi-venue shadow paper book."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from wca import shadowbook  # noqa: E402


def _load(path: str):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_report(con, out_path: str):
    payload = shadowbook.report(con)
    payload["generated"] = shadowbook.utc_now()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/shadow_book.db")
    parser.add_argument("--out", default="site/shadow_book.json")
    sub = parser.add_subparsers(dest="command", required=True)

    cycle = sub.add_parser("cycle")
    cycle.add_argument("--forest", default="site/forest_data.json")
    cycle.add_argument("--hyperliquid", default="site/hl_xvenue.json")
    cycle.add_argument("--bankroll", type=float, default=3227.0)
    cycle.add_argument("--ts")

    settle = sub.add_parser("settle")
    settle.add_argument("--settlements", required=True,
                        help="JSON list of {market_key,outcome}; outcome is 0/0.5/1")
    settle.add_argument("--ts")

    settle_fixtures = sub.add_parser("settle-fixtures")
    settle_fixtures.add_argument("--results", required=True,
                                 help="JSON list of structured fixture-event results")
    settle_fixtures.add_argument("--ts")

    sub.add_parser("report")
    args = parser.parse_args(argv)
    con = shadowbook.connect(args.db)

    if args.command == "cycle":
        policy = shadowbook.ShadowPolicy(bankroll_usd=args.bankroll)
        result = shadowbook.run_cycle(
            con, forest=_load(args.forest), hl_feed=_load(args.hyperliquid),
            ts_utc=args.ts, policy=policy)
        payload = _write_report(con, args.out)
        print("shadow cycle %d: PM observed=%d entered=%d explored=%d; HL pairs=%d cross=%d"
              % (result["run_id"], result["polymarket"]["observed"],
                 result["polymarket"]["entered"], result["polymarket"]["explored"],
                 result["hyperliquid"]["pairs"], result["hyperliquid"]["cross_entered"]))
        print("positions=%d open_stake=$%.2f -> %s"
              % (payload["summary"]["positions"], payload["summary"]["open_stake_usd"], args.out))
        return 0

    if args.command == "settle":
        rows = _load(args.settlements)
        n = 0
        for row in rows:
            n += shadowbook.settle_market(
                con, str(row["market_key"]), float(row["outcome"]), ts_utc=args.ts)
        payload = _write_report(con, args.out)
        print("settled %d observation(s); P&L $%+.2f"
              % (n, payload["summary"]["settled_pl_usd"]))
        return 0

    if args.command == "settle-fixtures":
        totals = {"settled_observations": 0, "unresolved_observations": 0,
                  "settled_markets": 0}
        for result in _load(args.results):
            got = shadowbook.settle_fixture(con, result, ts_utc=args.ts)
            for key in totals:
                totals[key] += got[key]
        payload = _write_report(con, args.out)
        print("fixture settlement: %d observations across %d markets; %d unresolved; P&L $%+.2f"
              % (totals["settled_observations"], totals["settled_markets"],
                 totals["unresolved_observations"], payload["summary"]["settled_pl_usd"]))
        return 0

    payload = _write_report(con, args.out)
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
