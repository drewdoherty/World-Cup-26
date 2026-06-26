#!/usr/bin/env python3
"""Liquidity & Execution Cost microstructure analysis (READ-ONLY).

What this measures, and what it CANNOT
--------------------------------------
TheOddsAPI (the ONLY source in data/wca.db) gives top-of-book DISPLAYED prices.
It carries NO order-book depth, NO matched volume, NO queue position. So true
liquidity (how much you can get matched, market impact, time-to-fill) is NOT
measurable here and is left as a FRAMEWORK (see `framework` block in the JSON
and the SCHEMA constants below) describing exactly what new capture would be
needed (Betfair streaming API / Smarkets depth).

What IS measurable from this data:

1) EXCHANGE BID-ASK SPREAD.  The DB stores both sides of the three exchanges:
   market='h2h'      = best BACK price (the odds you can bet/back at), and
   market='h2h_lay'  = best LAY price (the odds offered to lay you).
   Crucially these are written on the SAME capture grid, so for a given
   (ts, match, selection, exchange) we have a matched back/lay pair that
   brackets the fair price. The displayed top-of-book spread is then:
       odds spread  = lay_odds - back_odds
       pct  spread  = (lay_odds - back_odds) / back_odds
       prob spread  = 1/back_odds - 1/lay_odds      (implied-prob bid-ask, bps)
   The *round-trip* cost a taker pays to cross this spread (back then lay out,
   or vice-versa) is ~ the full spread; a one-way taker crossing to mid pays
   ~ half. We report the displayed spread (full) and flag the half-spread as
   the one-way crossing cost. We break this down by venue, by odds bucket
   (favourite vs longshot), and by time-to-kickoff.

2) SPORTSBOOK EFFECTIVE COST via OVERROUND.  Sportsbooks have no published lay
   side, so their execution cost is inferred from the 1X2 book margin
   (sum of 1/odds across the 3 outcomes - 1). Per-outcome cost ~ margin/3.
   This is the bookmaker's vig, the sportsbook analogue of crossing the spread.

All figures are PRE-KICKOFF, single source (theoddsapi), ~12-day window. The
back/lay (h2h_lay) capture is much sparser than h2h: it exists only at 7
distinct timestamps spanning 2026-06-13..2026-06-17 across 69 matches, so the
spread series is INDICATIVE, not a continuous tick record. Sample sizes are
reported on every metric and the JSON carries a data_caveat.

Run:
    PYTHONPATH=src .venv/bin/python scripts/microstructure/liquidity.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

# --------------------------------------------------------------------------
# Paths / config
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DB_PATH = os.path.join(REPO, "data", "wca.db")
OUT_PATH = os.path.join(REPO, "site", "microstructure", "liquidity.json")

EXCHANGES = ("smarkets", "betfair_ex_uk", "matchbook")
SPORTSBOOKS = (
    "williamhill", "skybet", "paddypower", "ladbrokes_uk", "coral",
    "betfred_uk", "unibet_uk", "betvictor", "boylesports", "virginbet",
    "leovegas", "casumo", "grosvenor", "sport888", "betway",
    "livescorebet", "betfair_sb_uk",
)

# Approx exchange commission on net winnings (UK retail defaults). Used only to
# annotate total round-trip cost; NOT applied to the raw spread numbers.
COMMISSION = {"smarkets": 0.0, "betfair_ex_uk": 0.05, "matchbook": 0.0}

# ------------------------------------------------------------------------
# FRAMEWORK SCHEMA: what new capture would make depth/impact measurable.
# These are emitted into the JSON so the site can show the gap explicitly.
# No numbers are invented here.
# ------------------------------------------------------------------------
DEPTH_CAPTURE_SCHEMA = {
    "table": "exchange_depth",
    "purpose": "Order-book depth + matched volume, which TheOddsAPI lacks.",
    "source_options": [
        "Betfair Exchange Streaming API (MarketSubscription, EX_ALL_OFFERS + EX_TRADED)",
        "Smarkets streaming/depth endpoint",
    ],
    "columns": {
        "ts_utc": "TEXT capture time",
        "venue": "TEXT smarkets|betfair_ex_uk|matchbook",
        "match_id": "TEXT",
        "selection": "TEXT outcome name",
        "side": "TEXT back|lay",
        "level": "INTEGER 0=top of book, 1.. deeper rungs",
        "price": "REAL decimal odds at this rung",
        "size_gbp": "REAL available stake at this rung (the depth number we cannot get today)",
        "traded_volume_gbp": "REAL cumulative matched £ on this selection (Betfair EX_TRADED)",
        "last_traded_price": "REAL",
    },
    "derivable_once_captured": [
        "fillable_stake_at_top",  "stake-weighted effective price for a £X order",
        "market_impact_curve (slippage vs order size)",
        "realised vs displayed spread", "queue / time-to-fill proxy",
    ],
}


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def load_pairs(con: sqlite3.Connection) -> List[dict]:
    """Matched back/lay pairs for the three exchanges.

    For every h2h_lay (LAY) row, find the h2h (BACK) row at the identical
    (ts_utc, match_id, selection, bookmaker_key). Both sides are written on the
    same capture grid so this is an exact join, not an approximation.
    """
    cur = con.cursor()
    cur.execute(
        """
        SELECT
            l.ts_utc,
            l.match_id,
            json_extract(l.raw,'$.bookmaker_key')   AS venue,
            l.selection                             AS selection,
            json_extract(l.raw,'$.commence_time')   AS ko,
            l.decimal_odds                          AS lay_odds,
            (SELECT b.decimal_odds
               FROM odds_snapshots b
              WHERE b.market='h2h'
                AND b.match_id = l.match_id
                AND b.ts_utc   = l.ts_utc
                AND b.selection = l.selection
                AND json_extract(b.raw,'$.bookmaker_key')
                    = json_extract(l.raw,'$.bookmaker_key')
              LIMIT 1)                              AS back_odds
        FROM odds_snapshots l
        WHERE l.market = 'h2h_lay'
        """
    )
    out = []
    for ts, mid, venue, sel, ko, lay, back in cur.fetchall():
        if back is None or lay is None or back <= 1.0 or lay <= 1.0:
            continue
        if lay < back:
            # crossed/locked quote (4 rows on smarkets) -> drop from spread calc
            continue
        try:
            hrs_to_ko = (
                (datetime.fromisoformat(ko) - datetime.fromisoformat(ts)).total_seconds()
                / 3600.0
            ) if ko else None
        except Exception:
            hrs_to_ko = None
        out.append({
            "ts": ts, "match_id": mid, "venue": venue, "selection": sel,
            "back": float(back), "lay": float(lay),
            "odds_spread": float(lay - back),
            "pct_spread": float((lay - back) / back * 100.0),
            "prob_spread_bps": float((1.0 / back - 1.0 / lay) * 10000.0),
            "hrs_to_ko": hrs_to_ko,
        })
    return out


def stats(vals: List[float]) -> dict:
    a = np.array(vals, dtype=float)
    return {
        "n": int(a.size),
        "mean": round(float(np.mean(a)), 4) if a.size else None,
        "median": round(float(np.median(a)), 4) if a.size else None,
        "p25": round(float(np.percentile(a, 25)), 4) if a.size else None,
        "p75": round(float(np.percentile(a, 75)), 4) if a.size else None,
    }


# --------------------------------------------------------------------------
# Sportsbook overround (effective cost) at the lay-capture timestamps so the
# comparison with the exchange spread is time-matched.
# --------------------------------------------------------------------------
def overround_by_book(con: sqlite3.Connection) -> Tuple[List[dict], List[str]]:
    cur = con.cursor()
    cur.execute("SELECT DISTINCT ts_utc FROM odds_snapshots WHERE market='h2h_lay'")
    lay_ts = [r[0] for r in cur.fetchall()]
    if not lay_ts:
        return [], []
    placeholders = ",".join("?" for _ in lay_ts)
    cur.execute(
        f"""SELECT ts_utc, match_id, json_extract(raw,'$.bookmaker_key') AS bk,
                   selection, decimal_odds
            FROM odds_snapshots
            WHERE market='h2h' AND ts_utc IN ({placeholders})""",
        lay_ts,
    )
    grid: Dict[Tuple[str, str, str], Dict[str, float]] = defaultdict(dict)
    for ts, mid, bk, sel, od in cur.fetchall():
        if bk and sel and od and od > 1.0:
            grid[(ts, mid, bk)][sel] = od

    per_book: Dict[str, List[float]] = defaultdict(list)
    for (ts, mid, bk), quotes in grid.items():
        if len(quotes) != 3:  # require full 3-way book
            continue
        overround = sum(1.0 / o for o in quotes.values())
        per_book[bk].append(overround)

    rows = []
    for bk, ovs in per_book.items():
        kind = "exchange" if bk in EXCHANGES else ("sportsbook" if bk in SPORTSBOOKS else "other")
        a = np.array(ovs)
        rows.append({
            "venue": bk,
            "kind": kind,
            "n_book_snapshots": int(a.size),
            "mean_overround": round(float(np.mean(a)), 5),
            "margin_pct": round(float(np.mean(a) - 1.0) * 100.0, 3),
            "per_outcome_cost_pct": round(float((np.mean(a) - 1.0) / 3.0) * 100.0, 3),
        })
    rows.sort(key=lambda d: d["mean_overround"])
    return rows, lay_ts


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> None:
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    pairs = load_pairs(con)
    n_pairs = len(pairs)

    # Window of the lay (spread) data specifically.
    cur.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc), COUNT(DISTINCT ts_utc), COUNT(DISTINCT match_id) "
        "FROM odds_snapshots WHERE market='h2h_lay'"
    )
    lay_min, lay_max, lay_n_ts, lay_n_matches = cur.fetchone()

    # ---- Per-venue spread ----
    by_venue = {}
    for v in EXCHANGES:
        sub = [p for p in pairs if p["venue"] == v]
        if not sub:
            continue
        by_venue[v] = {
            "n_pairs": len(sub),
            "pct_spread": stats([p["pct_spread"] for p in sub]),
            "prob_spread_bps": stats([p["prob_spread_bps"] for p in sub]),
            "commission_on_winnings_pct": COMMISSION.get(v, 0.0) * 100.0,
        }
    venue_ranking = sorted(
        ({"venue": v, **d} for v, d in by_venue.items()),
        key=lambda d: d["pct_spread"]["median"],
    )

    # ---- Spread by odds bucket (favourite vs longshot) ----
    def bucket(o: float) -> str:
        return "fav_<1.5" if o < 1.5 else "1.5-2.5" if o < 2.5 else "2.5-5" if o < 5 else "longshot_>=5"
    by_bucket_map: Dict[str, List[float]] = defaultdict(list)
    by_bucket_bps: Dict[str, List[float]] = defaultdict(list)
    for p in pairs:
        by_bucket_map[bucket(p["back"])].append(p["pct_spread"])
        by_bucket_bps[bucket(p["back"])].append(p["prob_spread_bps"])
    bucket_order = ["fav_<1.5", "1.5-2.5", "2.5-5", "longshot_>=5"]
    by_odds_bucket = [
        {
            "bucket": b,
            "pct_spread": stats(by_bucket_map[b]),
            "prob_spread_bps": stats(by_bucket_bps[b]),
        }
        for b in bucket_order if by_bucket_map.get(b)
    ]

    # ---- Spread by time-to-kickoff ----
    def ttk_bucket(h: Optional[float]) -> Optional[str]:
        if h is None:
            return None
        if h < 0:
            return "0_post_ko"
        if h < 6:
            return "1_<6h"
        if h < 24:
            return "2_6-24h"
        if h < 72:
            return "3_1-3d"
        return "4_>3d"
    ttk_map: Dict[str, List[float]] = defaultdict(list)
    ttk_bps: Dict[str, List[float]] = defaultdict(list)
    for p in pairs:
        tb = ttk_bucket(p["hrs_to_ko"])
        if tb is None:
            continue
        ttk_map[tb].append(p["pct_spread"])
        ttk_bps[tb].append(p["prob_spread_bps"])
    ttk_order = ["0_post_ko", "1_<6h", "2_6-24h", "3_1-3d", "4_>3d"]
    by_time_to_kickoff = [
        {
            "bucket": t,
            "pct_spread": stats(ttk_map[t]),
            "prob_spread_bps": stats(ttk_bps[t]),
        }
        for t in ttk_order if ttk_map.get(t)
    ]

    # ---- Sportsbook (and exchange) overround, time-matched ----
    overround, _ = overround_by_book(con)
    exch_overround = [r for r in overround if r["kind"] == "exchange"]
    book_overround = [r for r in overround if r["kind"] == "sportsbook"]
    mean_book_margin = (
        round(float(np.mean([r["margin_pct"] for r in book_overround])), 3)
        if book_overround else None
    )
    mean_book_per_outcome = (
        round(float(np.mean([r["per_outcome_cost_pct"] for r in book_overround])), 3)
        if book_overround else None
    )

    con.close()

    # ---- Headline numbers ----
    all_pct = [p["pct_spread"] for p in pairs]
    all_bps = [p["prob_spread_bps"] for p in pairs]
    overall_median_pct = round(float(np.median(all_pct)), 3) if all_pct else None
    overall_median_bps = round(float(np.median(all_bps)), 2) if all_bps else None
    tightest = venue_ranking[0]["venue"] if venue_ranking else None
    widest = venue_ranking[-1]["venue"] if venue_ranking else None

    # one-way taker cost = half the displayed spread (cross to mid)
    overall_oneway_pct = round(overall_median_pct / 2.0, 3) if overall_median_pct else None

    out = {
        "key": "liquidity",
        "title": "Liquidity & Execution Cost",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "spread_data_start": lay_min,
            "spread_data_end": lay_max,
            "spread_distinct_capture_times": lay_n_ts,
            "spread_matches": lay_n_matches,
            "source": "theoddsapi",
        },
        "method": {
            "spread_definition": "lay_odds - back_odds at identical (ts,match,selection,venue); top-of-book displayed only",
            "one_way_taker_cost": "half the displayed spread (crossing to mid)",
            "round_trip_taker_cost": "the full displayed spread",
            "sportsbook_cost": "inferred from 1X2 overround (sum 1/odds - 1); per-outcome ~ margin/3",
            "devig_note": "spread is computed on raw displayed odds; no de-vig needed for a within-venue back/lay bracket",
        },
        "headline": {
            "matched_back_lay_pairs": n_pairs,
            "overall_median_pct_spread": overall_median_pct,
            "overall_median_prob_spread_bps": overall_median_bps,
            "overall_one_way_taker_cost_pct": overall_oneway_pct,
            "tightest_exchange": tightest,
            "widest_exchange": widest,
            "mean_sportsbook_margin_pct": mean_book_margin,
            "mean_sportsbook_per_outcome_cost_pct": mean_book_per_outcome,
        },
        "by_venue": by_venue,
        "venue_ranking_by_median_pct_spread": [
            {"venue": v["venue"],
             "median_pct_spread": v["pct_spread"]["median"],
             "median_prob_spread_bps": v["prob_spread_bps"]["median"],
             "n_pairs": v["n_pairs"]}
            for v in venue_ranking
        ],
        "by_odds_bucket": by_odds_bucket,
        "by_time_to_kickoff": by_time_to_kickoff,
        "overround_by_venue": overround,
        "framework_not_measurable": {
            "note": (
                "TheOddsAPI carries NO depth, NO matched volume, NO queue. "
                "The following are FRAMEWORK ONLY and require new capture; "
                "no values are estimated from current data."
            ),
            "unmeasurable_today": [
                "fillable stake / available £ at top of book",
                "market impact (slippage as a function of order size)",
                "matched/traded volume per selection",
                "time-to-fill / queue position",
                "realised (vs displayed) spread once you take liquidity",
            ],
            "required_capture_schema": DEPTH_CAPTURE_SCHEMA,
        },
        "data_caveat": (
            "Single source (theoddsapi), pre-kickoff only, ~12-day window. The "
            "back/lay spread uses market='h2h' (best back) vs market='h2h_lay' "
            "(best lay) on the SAME capture grid; this is the DISPLAYED "
            "top-of-book spread, NOT depth-aware. The lay side is sparse: only "
            f"{lay_n_ts} distinct capture times spanning {lay_min[:10]}..{lay_max[:10]} "
            f"across {lay_n_matches} matches (n={n_pairs} matched pairs), so "
            "treat venue/time-to-kickoff spread differences as INDICATIVE. "
            "Mean spreads are inflated by longshots; medians are the robust "
            "figure. No queue depth, matched volume or market impact is "
            "observable in this dataset (see framework_not_measurable)."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(json.dumps({
        "matched_pairs": n_pairs,
        "overall_median_pct_spread": overall_median_pct,
        "overall_median_prob_spread_bps": overall_median_bps,
        "tightest": tightest, "widest": widest,
        "venue_ranking": [(v["venue"], v["pct_spread"]["median"]) for v in venue_ranking],
        "ttk": [(t["bucket"], t["pct_spread"]["median"], t["pct_spread"]["n"]) for t in by_time_to_kickoff],
        "mean_book_margin_pct": mean_book_margin,
    }, indent=2))


if __name__ == "__main__":
    main()
