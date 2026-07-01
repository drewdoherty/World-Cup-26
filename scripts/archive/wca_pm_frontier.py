#!/usr/bin/env python
"""Polymarket 'trading frontier' scan — never-traded + microstructure opportunities.

Classifies the whole live WC market universe by category and liquidity, then ranks
where we could trade next through a MICROSTRUCTURE lens (our dependable edge is
execution / spread / overround, not prediction): wide bid-ask spreads are both the
cost and the opportunity. Cross-references which categories we can already PRICE
(so a paper-trade is actionable now) vs which need a model built first.

    PYTHONPATH=src python3 scripts/wca_pm_frontier.py
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import polymarket as P  # noqa: E402
from wca.testbook import trader  # noqa: E402

# Market categories we can already price from existing models.
PRICEABLE = {"match_result", "advance", "totals", "btts", "exact_score"}


def categorize(event_title: str, git: str, question: str) -> str:
    t, g, q = event_title.lower(), git.lower(), question.lower()
    blob = " ".join((t, g, q))
    if "nation to reach" in t or "to advance" in t or "stage of elimination" in t:
        return "advance"
    if "exact score" in t:
        return "exact_score"
    if "total corners" in t or "corner" in blob:
        return "corners"
    if "player props" in t or any(k in g for k in (": 1+", ": 2+", ": 3+", "shots", "saves", "assist", "goals")):
        return "player_prop"
    if "both teams to score" in blob:
        return "btts"
    if "o/u" in g or "o/u" in q:
        return "totals"
    if "spread:" in g or re.search(r"\(-?\d+\.5\)", g):
        return "handicap"
    if "halftime" in t:
        return "halftime"
    if "second half" in t:
        return "2nd_half"
    if any(k in t for k in ("golden boot", "silver boot", "bronze boot", "golden ball",
                            "golden glove", "fair play", "most assists")):
        return "award"
    if t.strip().endswith("world cup winner") or t.strip() == "world cup winner":
        return "outright"
    if (" vs. " in t or " vs " in t) and " - " not in t:
        return "match_result"
    return "other"


def main(argv=None) -> int:
    print("Fetching live WC market universe …")
    events = P.find_world_cup_markets(include_closed=False)

    cat_stats = defaultdict(lambda: {"n": 0, "traded": 0, "never": 0, "spreads": []})
    opportunities = []  # priceable + tradeable thin markets
    never_by_cat = defaultdict(list)

    for ev in events:
        title = ev.get("title") or ""
        for m in ev.get("markets") or []:
            git = (m.get("groupItemTitle") or "")
            q = (m.get("question") or "")
            cat = categorize(title, git, q)
            vol = trader._f(m.get("volumeNum")) or 0.0
            quote = trader.yes_quote(m)
            st = cat_stats[cat]
            st["n"] += 1
            if vol <= 0:
                st["never"] += 1
                if len(never_by_cat[cat]) < 4:
                    never_by_cat[cat].append((git or q)[:48])
                continue
            st["traded"] += 1
            if quote and quote.get("spread") is not None and quote["mid"]:
                spct = quote["spread"] / quote["mid"]
                st["spreads"].append(spct)
                if cat in PRICEABLE and 0.02 < spct:  # wide enough that a model edge can clear cost
                    opportunities.append((spct, cat, (git or q)[:46], quote["bid"], quote["ask"], vol))

    total = sum(s["n"] for s in cat_stats.values())
    never = sum(s["never"] for s in cat_stats.values())
    print("\n=== UNIVERSE: %d markets across %d events · %d never-traded (%.0f%%) ===\n"
          % (total, len(events), never, 100.0 * never / total if total else 0))

    print("%-14s %6s %7s %7s %8s  %s" % ("category", "n", "traded", "never", "med_sprd", "priceable"))
    for cat in sorted(cat_stats, key=lambda c: -cat_stats[c]["n"]):
        s = cat_stats[cat]
        sp = sorted(s["spreads"])
        med = (sp[len(sp) // 2] if sp else 0.0)
        print("%-14s %6d %7d %7d %7.0f%%  %s"
              % (cat, s["n"], s["traded"], s["never"], 100 * med,
                 "YES" if cat in PRICEABLE else "—"))

    print("\n=== TOP PRICEABLE THIN MARKETS (we can model these now) ===")
    opportunities.sort(reverse=True)
    for spct, cat, label, bid, ask, vol in opportunities[:20]:
        print("  %5.0f%% spread  [%-12s] %-46s  %.2f/%.2f  vol %.0f"
              % (100 * spct, cat, label, bid, ask, vol))

    print("\n=== NEVER-TRADED INVENTORY (where structure-only edge may exist) ===")
    for cat in sorted(never_by_cat, key=lambda c: -cat_stats[c]["never"]):
        if cat_stats[cat]["never"] == 0:
            continue
        print("  %-14s %4d never-traded   e.g. %s"
              % (cat, cat_stats[cat]["never"], "; ".join(never_by_cat[cat][:3])))

    print("\n=== WHERE TO TRADE NEXT (modeling backlog: thin + NOT yet priceable) ===")
    backlog = sorted(((cat_stats[c]["n"], c) for c in cat_stats
                      if c not in PRICEABLE and c not in ("other",)), reverse=True)
    for n, c in backlog:
        s = cat_stats[c]
        sp = sorted(s["spreads"])
        med = (sp[len(sp) // 2] if sp else 0.0)
        print("  %-14s %4d markets, %.0f%% median spread — build a model to harvest" % (c, n, 100 * med))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
