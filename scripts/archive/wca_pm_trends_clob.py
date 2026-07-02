#!/usr/bin/env python
"""Polymarket trajectory line charts sourced from the CLOB price-history API.

Discovers current token ids from the live WC market universe, then pulls each
token's full price history from ``clob.polymarket.com/prices-history`` at the
fidelity each period needs (30-min for 24h, hourly for a week / full). Renders
the faceted line charts (raw ¢ + % change) per market and period, ordered by
soonest kickoff, plus an "our exposure" set.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_trends_clob.py --out reports/clob
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmtrends  # noqa: E402
from wca.data import polymarket as P  # noqa: E402
from wca.data import pm_clob_history as CH  # noqa: E402
from wca.data.teamnames import canonical  # noqa: E402

_DEF_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
_DEF_DB = os.path.join(_ROOT, "data", "wca.db")

# (label, clob_interval, fidelity_minutes)
PERIODS = [
    ("Last 24h · 30-min", "1d", 30),
    ("Last week · hourly", "1w", 60),
    ("Full history · hourly", "max", 60),
]


def _token_maps(events):
    """{market_key: {team: token_id}} for champion + match-winner markets."""
    champ, match = {}, {}
    for e in events:
        title = (e.get("title") or "").strip()
        is_champ = title == "World Cup Winner"
        is_match = (" vs. " in title or " vs " in title) and " - " not in title
        if not (is_champ or is_match):
            continue
        for m in e.get("markets") or []:
            git = (m.get("groupItemTitle") or "").strip()
            q = (m.get("question") or "")
            if is_match and (git.lower().startswith("draw") or "end in a draw" in q.lower()):
                continue
            res = P._yes_token_and_price(m, e)
            if not res or not git or not res.get("token_id"):
                continue
            (champ if is_champ else match)[git] = res["token_id"]
    # Bare "X vs Y" = FT 1X2 (90' + stoppage, draw possible) — NOT advancement.
    return {"Win the World Cup": champ, "Win in 90' (FT result)": match}


def _series_for(token_map, teams, interval, fidelity):
    out = {}
    for t in teams:
        tok = token_map.get(t)
        if not tok:
            continue
        pts = CH.price_history(tok, interval=interval, fidelity=fidelity)
        if pts:
            out[t] = pts
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(_ROOT, "reports", "pm_clob"))
    ap.add_argument("--scores", default=_DEF_SCORES)
    ap.add_argument("--db", default=_DEF_DB)
    ap.add_argument("--top", type=int, default=7)
    args = ap.parse_args(argv)

    print("Fetching live WC market universe …")
    events = P.find_world_cup_markets(include_closed=False)
    token_maps = _token_maps(events)
    kickoffs = pmtrends.load_kickoffs(args.scores)
    # upcoming only, relative to wall-clock now
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    upcoming = {t: k for t, k in kickoffs.items() if k >= now}
    known = sorted({canonical(t) for tm in token_maps.values() for t in tm})
    expo = pmtrends.exposure_teams(args.db, list({t for tm in token_maps.values() for t in tm}))
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    print("markets: %s | upcoming kickoffs: %d | exposure: %s"
          % ({k: len(v) for k, v in token_maps.items()}, len(upcoming), ", ".join(sorted(expo)) or "none"))

    written = 0

    def render_set(token_map, market, teams, scope=""):
        nonlocal written
        for plabel, interval, fid in PERIODS:
            series = _series_for(token_map, teams, interval, fid)
            order = pmtrends.select_teams(series, kickoffs=upcoming if not scope else kickoffs,
                                          top_n=args.top, require_live=not scope)
            if not order:
                continue
            sc = (" · %s" % scope) if scope else ""
            sub = "%s%s · pulled %s" % (plabel, sc, now.strftime("%Y-%m-%d %H:%M UTC"))
            png = pmtrends.render_trend_figure(
                series, title="%s — Polymarket (CLOB history)" % market,
                subtitle=sub, kickoffs=kickoffs, order=order)
            if not png:
                continue
            tag = "champion" if "World Cup" in market else "ft"
            fn = "%s_%s%s_%s.png" % (args.out, tag, ("_expo" if scope else ""),
                                     plabel.split()[1].replace("·", "").strip())
            with open(fn, "wb") as fh:
                fh.write(png)
            written += 1
            print("  %-26s %-22s -> %s [%s]" % (market + sc, plabel, fn, ", ".join(order)))

    # General: soonest-kickoff teams per market.
    for market, tmap in token_maps.items():
        teams = pmtrends.select_teams({t: [(now, 0.5)] for t in tmap}, kickoffs=upcoming,
                                      top_n=args.top, require_live=False)
        render_set(tmap, market, teams)

    # Exposure: teams we hold, per market.
    if expo:
        expo_pm = [t for tm in token_maps.values() for t in tm if canonical(t) in {canonical(x) for x in expo}]
        for market, tmap in token_maps.items():
            teams = [t for t in tmap if canonical(t) in {canonical(x) for x in expo}][:args.top]
            render_set(tmap, market, teams, scope="OUR EXPOSURE")

    print("Wrote %d figures." % written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
