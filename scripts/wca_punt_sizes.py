#!/usr/bin/env python
"""Add one-off indicative sizes to the event-market forest.

This is deliberately an overlay, not a replacement for governed trade recs.
Model rows use quarter-Kelly on a live Polymarket marked-equity balance, with conservative
per-position and per-fixture caps. Rows without a model probability receive a
small discretionary-punt marker only; they are never described as +EV.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _pm_fee(price: float) -> float:
    return 0.03 * price * (1.0 - price)


def _kelly_stake(model: float, market: float, bankroll: float, fraction: float) -> Dict[str, Any]:
    # Buy the displayed outcome when model >= market; otherwise buy its NO
    # complement. Both sides use the same binary PM fee formula.
    side = "YES" if model >= market else "NO"
    p = model if side == "YES" else 1.0 - model
    price = market if side == "YES" else 1.0 - market
    cost = price + _pm_fee(price)
    f = max(0.0, (p - cost) / (1.0 - cost))
    raw = bankroll * fraction * f
    return {"side": side, "raw": raw, "price": price, "kelly_fraction": f}


def build_sizes(
    forest: Dict[str, Any],
    bankroll: float = 3227.0,
    kelly_fraction: float = 0.25,
    per_position_cap: float = 40.0,
    per_fixture_cap: float = 160.0,
    market_only_punt: float = 5.0,
) -> Dict[str, Any]:
    """Return a copy of the forest with ``one_off_size_usd`` row fields."""
    out = json.loads(json.dumps(forest))
    for fixture in out.get("fixtures", []):
        used = 0.0
        rows = [r for r in fixture.get("rows", []) if "section" not in r]

        # Reserve the fixture budget for rows with an actual model edge before
        # assigning discretionary punt markers to market-only rows.
        model_rows = [r for r in rows if r.get("model") is not None and r.get("market") is not None]
        model_rows.sort(key=lambda r: (
            1 if r.get("family") == "scorer_prop" else 0,
            -abs(float(r.get("edge_pp") or 0.0)),
        ))
        for row in model_rows:
            model, market = row.get("model"), row.get("market")
            row["one_off_bankroll_usd"] = bankroll
            row["one_off_quarter_kelly_usd"] = bankroll * kelly_fraction
            row["one_off_size_usd"] = 0.0
            row["one_off_punt_size_usd"] = 0.0
            row["one_off_sizing"] = "no Kelly: no model"
            row["one_off_side"] = None
            if model is not None and market is not None:
                k = _kelly_stake(float(model), float(market), bankroll, kelly_fraction)
                size = min(per_position_cap, max(0.0, per_fixture_cap - used), k["raw"])
                row["one_off_size_usd"] = round(size, 2)
                row["one_off_side"] = k["side"]
                row["one_off_kelly_raw_usd"] = round(k["raw"], 2)
                row["one_off_sizing"] = (
                    "quarter-Kelly" if size > 0 else "quarter-Kelly: no edge"
                )
                used += size
        for row in rows:
            model, market = row.get("model"), row.get("market")
            if model is None and market is not None:
                # This is an explicit discretionary marker, not a model claim.
                size = market_only_punt
                row["one_off_punt_size_usd"] = round(size, 2)
                row["one_off_side"] = "YES"
                row["one_off_sizing"] = "optional punt: no model"
        fixture["one_off_fixture_cap_usd"] = per_fixture_cap
        fixture["one_off_used_usd"] = round(used, 2)
    out.setdefault("meta", {})["one_off_sizing"] = {
        "bankroll_usd": bankroll,
        "quarter_kelly_bankroll_usd": bankroll * kelly_fraction,
        "per_position_cap_usd": per_position_cap,
        "per_fixture_cap_usd": per_fixture_cap,
        "market_only_punt_usd": market_only_punt,
        "warning": "Indicative only; market-only rows have no defensible Kelly size.",
    }
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", default="site/forest_data.json")
    parser.add_argument("--out", default="site/forest_data.json")
    parser.add_argument("--bankroll", type=float, default=None,
                        help="explicit offline balance; default reads the live developer proxy")
    args = parser.parse_args(argv)
    bankroll = args.bankroll
    if bankroll is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from wca.pm.account import DEVELOPER_ADDRESS, read_account
        snap = read_account(DEVELOPER_ADDRESS)
        if not snap.get("available"):
            raise SystemExit("live Polymarket balance unavailable; refusing to size")
        bankroll = float(snap["balance_usd"])
        print("live Polymarket marked equity: $%.2f (%s)" % (bankroll, snap.get("method")))
    source = json.loads(Path(args.source).read_text(encoding="utf-8"))
    result = build_sizes(source, bankroll=bankroll)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
