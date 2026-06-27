"""Review the open Polymarket book and propose trims/keeps/adds under the rule.

Loads open Polymarket positions (from the published ``site/data.json`` or the
ledger), attaches the advancement model's fair probability for each market, and
classifies every position with :mod:`wca.pmtrim`:

    +EV but not all longshots; near-moneyline first; biggest mispricing first.

Prints the proposals and, with ``--ping``, sends them to the Telegram bot
(requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID; dry-prints otherwise).

Usage:
    ./.venv/bin/python scripts/wca_pm_trim.py
    ./.venv/bin/python scripts/wca_pm_trim.py --ping

NOTE: priced off the local model snapshot. Verify live Polymarket prices before
executing. Match-event markets (game lines / scorers / corners / handicaps) are
not covered here — this reviews the futures/advancement book only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wca.pmtrim import Position, format_proposals, ping_proposals, propose  # noqa: E402


def _load_dotenv(path: str = ".env") -> None:
    import os

    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _open_pm_positions(site_data: str) -> List[dict]:
    with open(site_data, encoding="utf-8") as fh:
        data = json.load(fh)
    return [
        p for p in (data.get("positions") or [])
        if (p.get("platform") or "").lower() == "polymarket"
    ]


def _advancement_model_probs(path: str) -> Dict[Tuple[str, str, str], float]:
    """Parse the advancement markdown into ``(team, market_key, side) -> prob``."""
    out: Dict[Tuple[str, str, str], float] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(
            r"\|\s*\d*\s*\|?\s*([A-Za-z .]+?)\s*\|\s*([A-L]|[^|]*?)\s*\|"
            r"\s*([^|]+?)\s*\|\s*(YES|NO)\s*\|\s*([\d.]+)%",
            line,
        )
        if not m:
            continue
        team, _grp, mkt, side, prob = (x.strip() for x in m.groups())
        out[(team, _market_key(mkt), side.upper())] = float(prob) / 100.0
    return out


def _market_key(market: str) -> str:
    m = market.lower()
    if "win group" in m:
        return "wingroup"
    if "round of 16" in m:
        return "r16"
    if "r32" in m or "knockout" in m or "advance" in m:
        return "advance"
    if "win the" in m or "tournament" in m or m.strip() == "win":
        return "winwc"
    return "?"


def _lookup(market: str, selection: str, model: Dict) -> Optional[float]:
    t = re.search(r"Will ([A-Za-z .]+?) (?:reach|advance|win)", market or "")
    if not t:
        return None
    return model.get((t.group(1).strip(), _market_key(market or ""), (selection or "").upper()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site-data", default="site/data.json")
    ap.add_argument("--advancement", default="data/advancement_latest.json")
    ap.add_argument("--ping", action="store_true", help="Send proposals to the TG bot.")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()
    _load_dotenv(args.env)

    raw = _open_pm_positions(args.site_data)
    model = _advancement_model_probs(args.advancement)

    positions: List[Position] = []
    for p in raw:
        dec = float(p.get("decimal_odds") or 0)
        if dec <= 0:
            continue
        positions.append(
            Position(
                market=p.get("market") or "?",
                selection=p.get("selection") or "?",
                stake=float(p.get("stake") or 0),
                decimal_odds=dec,
                model_prob=_lookup(p.get("market") or "", p.get("selection") or "", model),
                currency=p.get("currency") or "USD",
            )
        )

    proposals = propose(positions)
    text = format_proposals(proposals)
    sent = ping_proposals(text, dry_run=not args.ping)
    if not args.ping:
        # ping_proposals already printed in dry-run; nothing more to do.
        pass
    elif sent:
        print("Sent %d proposals to the Telegram bot." % len(proposals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
