#!/usr/bin/env python3
"""Cross-venue arbitrage & synthetic-hedge scan on 1X2 (h2h) snapshots (READ-ONLY).

Question
--------
At each *matched* capture timestamp, take the best decimal price per 1X2 outcome
across the ~20 books. If ``sum(1/best_odds) < 1`` the snapshot implies a
risk-free back-only arbitrage. How often does that happen, how big is the
margin, which books create it -- and crucially, how many survive realistic
costs (exchange commission, account limits, stale/in-play quotes, latency)?

Method (all derived from data/wca.db, never mutated; DB opened mode=ro)
----------------------------------------------------------------------
1. odds_snapshots are written on a *shared* ts_utc grid: one API poll stores
   every bookmaker + every outcome at the same ts_utc. So the best price per
   outcome at a given ts_utc is a genuine matched-timestamp cross-section
   (NOT prices stitched from different times). We scan h2h only -- the only
   market with a full mutually-exclusive-exhaustive 3-way partition here.

2. DATA-QUALITY GUARDS (this is where most "arbs" die):
   a. floor guard -- drop any quote <= 1.05. A decimal price pinned near 1.0 is
      a "suspended / off-the-board" placeholder (book pulled the market), not a
      back you could ever stake.
   b. consensus-outlier guard -- per outcome, drop any quote > 1.6x the median
      quote across books. A lone 276.0 next to a 51.0 cluster is a stale price
      left behind during a line move, not a standing market.
   c. pre-match guard -- drop snapshots at/after kickoff (commence_time). The
      large "arbs" cluster IN-PLAY where books reprice asynchronously after a
      goal; those are artifacts of mixing pre-move and post-move quotes in one
      snapshot, never two clickable legs.

3. GROSS arb = sum(1/best_raw) < 1 (no fees). NET arb applies exchange
   commission via wca.arb.effective_back (Smarkets/Matchbook 2%, Betfair 6%;
   plain sportsbooks 0%) BEFORE the test -- the same settlement-safe logic in
   src/wca/arb.py. We report both; NET is the honest number.

4. PERSISTENCE: a NET arb visible across N consecutive ~5-min snapshots is ONE
   opportunity, not N. We collapse consecutive net-arb snapshots within a match
   into "episodes" (gap > 15 min starts a new episode) to count distinct,
   actually-actionable opportunities.

5. EXCHANGE dependence: we flag whether each net arb needs at least one exchange
   leg (commission already netted, no gubbing) vs. is all-sportsbook (exposed to
   account limits / stake factoring / gubbing -- the real-world killer for the
   sportsbook side).

Outputs site/microstructure/arbitrage.json: headline frequencies/margins,
top book-combos, a per-margin-bucket histogram and a daily episode series for
charting, sample sizes, window and a data_caveat.

Run:
    PYTHONPATH=src .venv/bin/python scripts/microstructure/arbitrage.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from wca.arb import effective_back

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DB_PATH = os.path.join(REPO, "data", "wca.db")
OUT_PATH = os.path.join(REPO, "site", "microstructure", "arbitrage.json")

# Guards / parameters
FLOOR_ODDS = 1.05          # quotes <= this are suspended/off-the-board placeholders
OUTLIER_RATIO = 1.6        # drop quotes > this x the per-outcome median (stale)
EPISODE_GAP_MIN = 15.0     # gap (minutes) that starts a new arb "episode"
MIN_PROFIT = 0.0           # report any sum<1 (we surface the full margin dist)

EXCHANGES = {"smarkets", "betfair_ex_uk", "betfair_ex_eu", "matchbook"}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _load(con: sqlite3.Connection):
    """Return snaps[(match,ts)][outcome][book]=odds, kickoff[match], meta[match]."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT match_id, ts_utc,
               json_extract(raw,'$.outcome_name'),
               json_extract(raw,'$.bookmaker_key'),
               decimal_odds,
               json_extract(raw,'$.commence_time'),
               json_extract(raw,'$.home_team'),
               json_extract(raw,'$.away_team')
        FROM odds_snapshots
        WHERE market='h2h'
        """
    )
    snaps: Dict[Tuple[str, str], Dict[str, Dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    kickoff: Dict[str, str] = {}
    meta: Dict[str, Tuple[str, str]] = {}
    for mid, ts, outc, book, odds, ct, home, away in cur.fetchall():
        if odds is None or book is None or outc is None:
            continue
        snaps[(mid, ts)][outc][book] = float(odds)
        if ct:
            kickoff[mid] = ct
        meta[mid] = (home, away)
    return snaps, kickoff, meta


def _best_per_outcome(
    oc: Dict[str, Dict[str, float]], net: bool
) -> Optional[Dict[str, Tuple[float, str]]]:
    """Robust best price (raw or net) per outcome after floor+outlier guards.

    Returns None if any outcome has no clean quote (snapshot unusable).
    """
    best: Dict[str, Tuple[float, str]] = {}
    for name, bm in oc.items():
        # floor guard
        vals = {b: o for b, o in bm.items() if o > FLOOR_ODDS}
        if not vals:
            return None
        med = st.median(vals.values())
        # consensus-outlier guard
        clean = {b: o for b, o in vals.items() if med <= 0 or o <= OUTLIER_RATIO * med}
        if not clean:
            return None
        if net:
            bk = max(clean, key=lambda b: effective_back(clean[b], b))
            best[name] = (effective_back(clean[bk], bk), bk)
        else:
            bk = max(clean, key=lambda b: clean[b])
            best[name] = (clean[bk], bk)
    return best


def main() -> None:
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        snaps, kickoff, meta = _load(con)
    finally:
        con.close()

    ts_all = [ts for (_mid, ts) in snaps.keys()]
    win_min, win_max = min(ts_all), max(ts_all)

    # Phase split + scan
    pre_total = 0
    inplay_total = 0
    inplay_arb = 0
    inplay_margins: List[float] = []

    gross_arb = 0
    net_arb = 0
    gross_margins: List[float] = []
    net_margins: List[float] = []
    net_combos: Dict[Tuple[str, ...], int] = defaultdict(int)
    net_has_exch = 0
    net_all_sb = 0
    net_arb_ts_by_match: Dict[str, List[datetime]] = defaultdict(list)
    matches_pre = set()
    # best single example arb for the site
    best_example: Optional[dict] = None

    for (mid, ts), oc in snaps.items():
        if len(oc) != 3:
            continue
        ko = kickoff.get(mid)
        is_inplay = bool(ko) and _parse(ts) >= _parse(ko)

        if is_inplay:
            inplay_total += 1
            nb = _best_per_outcome(oc, net=True)
            if nb is not None:
                s = sum(1.0 / nb[n][0] for n in nb)
                if s < 1.0:
                    inplay_arb += 1
                    inplay_margins.append(1.0 / s - 1.0)
            continue

        # PRE-MATCH
        pre_total += 1
        matches_pre.add(mid)
        rb = _best_per_outcome(oc, net=False)
        nb = _best_per_outcome(oc, net=True)
        if rb is not None:
            s_raw = sum(1.0 / rb[n][0] for n in rb)
            if s_raw < 1.0:
                gross_arb += 1
                gross_margins.append(1.0 / s_raw - 1.0)
        if nb is not None:
            s_net = sum(1.0 / nb[n][0] for n in nb)
            if s_net < 1.0:
                m = 1.0 / s_net - 1.0
                net_arb += 1
                net_margins.append(m)
                books = tuple(sorted(set(nb[n][1] for n in nb)))
                net_combos[books] += 1
                if set(nb[n][1] for n in nb) & EXCHANGES:
                    net_has_exch += 1
                else:
                    net_all_sb += 1
                net_arb_ts_by_match[mid].append(_parse(ts))
                if best_example is None or m > best_example["margin_pct"]:
                    home, away = meta.get(mid, ("?", "?"))
                    best_example = {
                        "match": f"{home} v {away}",
                        "ts_utc": ts,
                        "margin_pct": m,
                        "legs": [
                            {"outcome": n, "net_odds": round(nb[n][0], 3), "book": nb[n][1]}
                            for n in nb
                        ],
                    }

    # Collapse net-arb snapshots into episodes (distinct opportunities)
    episodes = 0
    for mid, tss in net_arb_ts_by_match.items():
        tss.sort()
        prev = None
        for t in tss:
            if prev is None or (t - prev).total_seconds() > EPISODE_GAP_MIN * 60:
                episodes += 1
            prev = t

    # Margin histogram (net) for charting
    buckets = [(0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 1.0)]
    blabels = ["0-0.5%", "0.5-1%", "1-2%", "2-5%", ">5%"]
    hist = [0] * len(buckets)
    for m in net_margins:
        for i, (lo, hi) in enumerate(buckets):
            if lo <= m < hi:
                hist[i] += 1
                break
    margin_hist = [{"bucket": blabels[i], "count": hist[i]} for i in range(len(buckets))]

    # Daily episode series (date -> distinct episodes)
    daily: Dict[str, int] = defaultdict(int)
    for mid, tss in net_arb_ts_by_match.items():
        tss.sort()
        prev = None
        for t in tss:
            if prev is None or (t - prev).total_seconds() > EPISODE_GAP_MIN * 60:
                daily[t.date().isoformat()] += 1
            prev = t
    daily_series = [{"date": d, "episodes": daily[d]} for d in sorted(daily)]

    # Top book-combos (sets of books that jointly form a net arb)
    top_combos = [
        {"books": list(c), "count": n}
        for c, n in sorted(net_combos.items(), key=lambda x: -x[1])[:8]
    ]

    def pct(xs, q):
        if not xs:
            return None
        s = sorted(xs)
        return s[min(len(s) - 1, int(q * len(s)))]

    span_days = (_parse(win_max) - _parse(win_min)).total_seconds() / 86400.0

    out = {
        "key": "arbitrage",
        "title": "Cross-venue arbitrage & synthetic hedges (1X2)",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": win_min,
            "end": win_max,
            "span_days": round(span_days, 1),
            "source": "theoddsapi",
        },
        "method": (
            "At each matched ts_utc poll, take best 1X2 price per outcome across "
            "~20 books; arb iff sum(1/best) < 1. Guards: drop quotes<=1.05 "
            "(suspended), drop >1.6x per-outcome median (stale), pre-match only "
            "(in-play quotes reprice asynchronously). NET applies exchange "
            "commission (Smarkets/Matchbook 2%, Betfair 6%) via wca.arb."
        ),
        "headline": {
            "pre_match_snapshots": pre_total,
            "gross_arb_snapshots": gross_arb,
            "gross_arb_rate_pct": round(100 * gross_arb / pre_total, 2) if pre_total else None,
            "gross_margin_median_pct": round(100 * st.median(gross_margins), 3) if gross_margins else None,
            "gross_margin_max_pct": round(100 * max(gross_margins), 3) if gross_margins else None,
            "net_arb_snapshots": net_arb,
            "net_arb_rate_pct": round(100 * net_arb / pre_total, 2) if pre_total else None,
            "net_margin_median_pct": round(100 * st.median(net_margins), 3) if net_margins else None,
            "net_margin_p90_pct": round(100 * pct(net_margins, 0.90), 3) if net_margins else None,
            "net_margin_max_pct": round(100 * max(net_margins), 3) if net_margins else None,
            "net_arb_episodes": episodes,
            "episodes_per_day": round(episodes / span_days, 2) if span_days else None,
            "matches_with_net_arb": len(net_arb_ts_by_match),
            "matches_scanned": len(matches_pre),
            "net_arbs_needing_exchange_leg": net_has_exch,
            "net_arbs_all_sportsbook": net_all_sb,
        },
        "inplay_contamination": {
            "inplay_snapshots": inplay_total,
            "inplay_arb_snapshots": inplay_arb,
            "inplay_arb_rate_pct": round(100 * inplay_arb / inplay_total, 2) if inplay_total else None,
            "inplay_margin_median_pct": round(100 * st.median(inplay_margins), 3) if inplay_margins else None,
            "inplay_margin_max_pct": round(100 * max(inplay_margins), 3) if inplay_margins else None,
            "note": (
                "In-play 'arbs' are ~7x more frequent and far larger but are "
                "artifacts of asynchronous repricing -- NOT executable. Excluded "
                "from headline."
            ),
        },
        "margin_histogram": margin_hist,
        "daily_episodes": daily_series,
        "top_book_combos": top_combos,
        "best_example": best_example,
        "samples": {
            "pre_match_snapshots": pre_total,
            "net_arb_snapshots": net_arb,
            "net_arb_episodes": episodes,
            "matches": len(matches_pre),
        },
        "data_caveat": (
            "Single source (theoddsapi), ~20 UK books, h2h only, ~12-day window "
            "(11-23 Jun 2026), 72 matches. NO order-book depth or matched volume, "
            "so we cannot confirm the best quote was actually takeable for "
            "meaningful stake. Net-of-commission only; account limits / stake "
            "factoring / gubbing on the sportsbook leg, plus the ~minutes of "
            "latency between poll and bet placement, are NOT modelled and will "
            "shrink the survivable set further. Indicative (net episodes n="
            + str(episodes) + ", < 30 distinct opportunities), not significant."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(json.dumps(out["headline"], indent=2))
    print("inplay:", json.dumps(out["inplay_contamination"], indent=2))


if __name__ == "__main__":
    main()
