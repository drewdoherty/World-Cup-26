#!/usr/bin/env python3
"""Exchange vs Sportsbook lead-lag microstructure analysis (READ-ONLY).

Question
--------
On 1X2 (h2h) deep intraday price series, when the de-vigged consensus mid
moves, do the betting *exchanges* (smarkets / betfair_ex_uk / matchbook) move
BEFORE the *sportsbooks* (williamhill, skybet, paddypower, ...) or after?
And which venue's price is closest to the eventual pre-kickoff close (i.e. most
"informative")?

Method (all derived from data/wca.db, never mutated)
----------------------------------------------------
1. odds_snapshots are written on a *shared* timestamp grid: a single API poll
   stores every bookmaker + every outcome at the same ts_utc. So exchange and
   sportsbook prices are sampled at identical times -> we can cross-correlate
   change series at integer snapshot lags directly.

2. For each deep-series match (>= MIN_SNAPS distinct snapshots, all pre-kickoff)
   we build, at each snapshot ts:
     - per-bookmaker Shin-devigged fair probability for the HOME outcome
       (3-way 1X2; Shin chosen for its favourite/longshot handling, consistent
       with src/wca/markets/devig.py);
     - EXCHANGE consensus = median home-prob across available exchange books;
     - SPORTSBOOK consensus = median home-prob across available sportsbooks.

3. Snapshot spacing is irregular (sparse hourly polling early, dense ~3-min
   polling in active windows). A fixed *snapshot* lag would conflate a 3-min
   move with a 1-hour move, so we keep only "dense runs": maximal stretches of
   consecutive snapshots spaced <= MAX_GAP_MIN apart, of length >= MIN_RUN.
   Within a dense run, lag = k snapshots ~ k * (median spacing) wall-clock.

4. Lead-lag: first-difference each consensus series within a run, then compute
   the Pearson cross-correlation of dEXCH(t) vs dBOOK(t + lag) for lag in
   [-LAGS .. +LAGS]. Pool the per-lag (cov, var) contributions across ALL runs
   and matches to form one pooled cross-correlation function. The lag with peak
   |correlation| and its sign identify the leader:
       peak lag > 0  => book change at t+lag tracks exchange change at t
                        => EXCHANGE LEADS (book follows by `lag` snapshots).
       peak lag < 0  => exchange follows book.
       peak lag = 0  => contemporaneous (no measurable lead at this cadence).

5. Informativeness: for each match define the CLOSE as the last pre-kickoff
   snapshot's *blended* consensus home-prob (median of all books). Then at a
   fixed pre-close horizon (the last snapshot at least HORIZON_MIN minutes
   before that close) compare |exchange_consensus - close| vs
   |sportsbook_consensus - close|. The venue with the smaller mean absolute
   gap-to-close is the more informative (its early price better anticipates the
   close). We also report which venue "wins" per match.

Outputs site/microstructure/exchange_vs_book.json with headline numbers,
the pooled cross-correlation curve (for charting), informativeness ranking,
sample sizes, window, and a data_caveat.

Run:
    PYTHONPATH=src .venv/bin/python scripts/microstructure/exchange_vs_book.py
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from wca.markets.devig import shin

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DB_PATH = os.path.join(REPO, "data", "wca.db")
OUT_PATH = os.path.join(REPO, "site", "microstructure", "exchange_vs_book.json")

EXCHANGES = ("smarkets", "betfair_ex_uk", "matchbook")
# Sportsbook set (everything that is not an exchange / lay book). Restrict to
# books that actually carry h2h volume in this dataset.
SPORTSBOOKS = (
    "williamhill", "skybet", "paddypower", "ladbrokes_uk", "coral",
    "betfred_uk", "unibet_uk", "betvictor", "boylesports", "virginbet",
    "leovegas", "casumo", "grosvenor", "sport888", "betway",
    "livescorebet", "betfair_sb_uk",
)

MIN_SNAPS = 250        # deep-series threshold (distinct snapshots per match)
MAX_GAP_MIN = 10.0     # snapshots farther apart than this break a "dense run"
MIN_RUN = 12           # a dense run needs at least this many snapshots
LAGS = 6               # cross-correlate over lag in [-LAGS, +LAGS] snapshots
HORIZON_MIN = 30.0     # informativeness measured at >= this many min before close
MIN_BOOKS_EACH = 2     # need >= this many books per side to form a consensus


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def home_prob_for_book(quotes: Dict[str, float], home: str, away: str) -> Optional[float]:
    """Shin-devigged HOME fair probability from a single book's 1X2 quotes."""
    if home not in quotes or away not in quotes or "Draw" not in quotes:
        return None
    odds = [quotes[home], quotes["Draw"], quotes[away]]
    if any((o is None) or (o <= 1.0) for o in odds):
        return None
    try:
        p = shin(odds)
    except Exception:
        return None
    v = float(p[0])
    if not math.isfinite(v) or v <= 0.0 or v >= 1.0:
        return None
    return v


def consensus_series(con: sqlite3.Connection, match_id: str):
    """Return (sorted_ts, exch_prob, book_prob, blend_prob) arrays for a match.

    Each *_prob is a list aligned with sorted_ts; entries are float or None
    when that side lacks >= MIN_BOOKS_EACH books at that ts.
    """
    cur = con.cursor()
    cur.execute(
        """SELECT ts_utc,
                  json_extract(raw,'$.bookmaker_key'),
                  json_extract(raw,'$.outcome_name'),
                  decimal_odds,
                  json_extract(raw,'$.home_team'),
                  json_extract(raw,'$.away_team')
           FROM odds_snapshots
           WHERE market='h2h' AND match_id=?
           ORDER BY ts_utc""",
        (match_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return [], [], [], [], None, None
    home = rows[0][4]
    away = rows[0][5]
    # ts -> bk -> {outcome: odds}
    grid: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for ts, bk, outc, od, h, a in rows:
        if bk is None or outc is None or od is None:
            continue
        grid[ts][bk][outc] = od

    sorted_ts = sorted(grid.keys())
    exch_p: List[Optional[float]] = []
    book_p: List[Optional[float]] = []
    blend_p: List[Optional[float]] = []
    for ts in sorted_ts:
        books = grid[ts]
        ex_vals, bk_vals, all_vals = [], [], []
        for bkkey, quotes in books.items():
            p = home_prob_for_book(quotes, home, away)
            if p is None:
                continue
            all_vals.append(p)
            if bkkey in EXCHANGES:
                ex_vals.append(p)
            elif bkkey in SPORTSBOOKS:
                bk_vals.append(p)
        exch_p.append(float(np.median(ex_vals)) if len(ex_vals) >= 1 else None)
        book_p.append(float(np.median(bk_vals)) if len(bk_vals) >= MIN_BOOKS_EACH else None)
        blend_p.append(float(np.median(all_vals)) if len(all_vals) >= MIN_BOOKS_EACH else None)
    return sorted_ts, exch_p, book_p, blend_p, home, away


def dense_runs(sorted_ts: List[str]) -> List[Tuple[int, int]]:
    """Index ranges [start, end) of maximal runs with consecutive gaps <= MAX_GAP_MIN."""
    if len(sorted_ts) < 2:
        return []
    times = [_parse(t) for t in sorted_ts]
    runs = []
    start = 0
    for i in range(1, len(times)):
        gap = (times[i] - times[i - 1]).total_seconds() / 60.0
        if gap > MAX_GAP_MIN:
            if i - start >= MIN_RUN:
                runs.append((start, i))
            start = i
    if len(times) - start >= MIN_RUN:
        runs.append((start, len(times)))
    return runs


# --------------------------------------------------------------------------
# Main analysis
# --------------------------------------------------------------------------
def main() -> None:
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = con.cursor()

    # Deep-series matches.
    cur.execute(
        """SELECT match_id, COUNT(DISTINCT ts_utc) n
           FROM odds_snapshots WHERE market='h2h'
           GROUP BY match_id HAVING n>=? ORDER BY n DESC""",
        (MIN_SNAPS,),
    )
    deep = [r[0] for r in cur.fetchall()]

    # Window bounds.
    cur.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc) FROM odds_snapshots WHERE market='h2h'"
    )
    win_min, win_max = cur.fetchone()

    # Pooled cross-correlation accumulators, per lag.
    lag_vals = range(-LAGS, LAGS + 1)
    # For each lag we accumulate paired (dExch[t], dBook[t+lag]) deltas across
    # all runs/matches, then compute one Pearson corr per lag at the end.
    paired: Dict[int, Tuple[List[float], List[float]]] = {L: ([], []) for L in lag_vals}

    n_runs = 0
    n_delta_pairs = 0
    matches_used = 0

    # Informativeness accumulators.
    info_records = []  # per match: (exch_abs_gap, book_abs_gap)
    # Also accumulate per-venue (individual book) closeness to close for ranking.
    venue_gaps: Dict[str, List[float]] = defaultdict(list)

    for mid in deep:
        sorted_ts, exch_p, book_p, blend_p, home, away = consensus_series(con, mid)
        if not sorted_ts:
            continue
        used_this_match = False

        # --- Lead-lag over dense runs ---
        for (s, e) in dense_runs(sorted_ts):
            ex = exch_p[s:e]
            bk = book_p[s:e]
            # need both sides present; compute deltas only where consecutive
            # pair is fully present on the respective series.
            dex = []
            dbk = []
            idx = []
            for i in range(1, e - s):
                if ex[i] is not None and ex[i - 1] is not None:
                    dex.append(ex[i] - ex[i - 1])
                else:
                    dex.append(None)
                if bk[i] is not None and bk[i - 1] is not None:
                    dbk.append(bk[i] - bk[i - 1])
                else:
                    dbk.append(None)
            # dex/dbk are length (e-s-1), index t corresponds to delta at run pos i=t+1
            m = len(dex)
            if m < MIN_RUN:
                continue
            run_has_pairs = False
            for L in lag_vals:
                xs, ys = paired[L]
                for t in range(m):
                    tt = t + L
                    if 0 <= tt < m:
                        a = dex[t]
                        b = dbk[tt]
                        if a is not None and b is not None:
                            xs.append(a)
                            ys.append(b)
                            if L == 0:
                                n_delta_pairs += 1
                            run_has_pairs = True
            if run_has_pairs:
                n_runs += 1
                used_this_match = True

        # --- Informativeness: closeness to pre-kickoff close ---
        # Close = last snapshot with a blended consensus.
        close_idx = None
        for i in range(len(sorted_ts) - 1, -1, -1):
            if blend_p[i] is not None:
                close_idx = i
                break
        if close_idx is not None:
            close_val = blend_p[close_idx]
            close_t = _parse(sorted_ts[close_idx])
            # horizon snapshot: last snapshot >= HORIZON_MIN before close that
            # has BOTH exchange and book consensus present.
            h_idx = None
            for i in range(close_idx - 1, -1, -1):
                dt = (close_t - _parse(sorted_ts[i])).total_seconds() / 60.0
                if dt >= HORIZON_MIN and exch_p[i] is not None and book_p[i] is not None:
                    h_idx = i
                    break
            if h_idx is not None:
                eg = abs(exch_p[h_idx] - close_val)
                bg = abs(book_p[h_idx] - close_val)
                info_records.append((eg, bg))
                # per-venue closeness at same horizon ts (individual books)
                _accumulate_venue_gaps(con, mid, sorted_ts[h_idx], close_val, venue_gaps, home, away)

        if used_this_match:
            matches_used += 1

    con.close()

    # --- Build pooled cross-correlation curve ---
    xcorr = []
    best = None
    for L in lag_vals:
        xs, ys = paired[L]
        n = len(xs)
        if n >= 30:
            r = float(np.corrcoef(xs, ys)[0, 1]) if np.std(xs) > 0 and np.std(ys) > 0 else 0.0
        else:
            r = None
        entry = {"lag_snapshots": L, "corr": (round(r, 4) if r is not None else None), "n": n}
        xcorr.append(entry)
        if r is not None and (best is None or abs(r) > abs(best["corr"])):
            best = {"lag_snapshots": L, "corr": round(r, 4), "n": n}

    # Lag-0 baseline corr (contemporaneous co-movement strength).
    lag0 = next((x for x in xcorr if x["lag_snapshots"] == 0), None)

    # Approx wall-clock per snapshot in dense runs (for lead-time estimate).
    med_spacing_min = _median_dense_spacing(DB_PATH, deep)

    # Direction interpretation.
    if best is None:
        leader = "indeterminate"
        lead_minutes = None
    else:
        bl = best["lag_snapshots"]
        if bl > 0:
            leader = "exchange"
        elif bl < 0:
            leader = "sportsbook"
        else:
            leader = "contemporaneous"
        lead_minutes = (
            round(abs(bl) * med_spacing_min, 1) if (med_spacing_min and bl != 0) else 0.0
        )

    # --- Informativeness summary ---
    info_n = len(info_records)
    if info_n > 0:
        exch_gaps = np.array([r[0] for r in info_records])
        book_gaps = np.array([r[1] for r in info_records])
        exch_mean = float(np.mean(exch_gaps))
        book_mean = float(np.mean(book_gaps))
        exch_wins = int(np.sum(exch_gaps < book_gaps))
        book_wins = int(np.sum(book_gaps < exch_gaps))
        # paired diff test (book_gap - exch_gap); >0 means exchange closer
        diff = book_gaps - exch_gaps
        diff_mean = float(np.mean(diff))
        diff_sd = float(np.std(diff, ddof=1)) if info_n > 1 else 0.0
        tstat = (diff_mean / (diff_sd / math.sqrt(info_n))) if diff_sd > 0 else None
    else:
        exch_mean = book_mean = diff_mean = None
        exch_wins = book_wins = 0
        tstat = None

    # Per-venue informativeness ranking (mean abs gap-to-close).
    venue_rank = []
    for v, gaps in venue_gaps.items():
        if len(gaps) >= 10:
            venue_rank.append({
                "venue": v,
                "kind": "exchange" if v in EXCHANGES else "sportsbook",
                "mean_abs_gap_to_close": round(float(np.mean(gaps)), 5),
                "n": len(gaps),
            })
    venue_rank.sort(key=lambda d: d["mean_abs_gap_to_close"])

    out = {
        "key": "exchange_vs_book",
        "title": "Exchange vs Sportsbook Lead-Lag",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": win_min, "end": win_max, "source": "theoddsapi"},
        "config": {
            "exchanges": list(EXCHANGES),
            "sportsbooks_considered": len(SPORTSBOOKS),
            "devig_method": "shin",
            "outcome_modelled": "home_win_fair_prob_1x2",
            "min_snaps_deep": MIN_SNAPS,
            "max_gap_min_dense_run": MAX_GAP_MIN,
            "min_run_len": MIN_RUN,
            "lag_window_snapshots": LAGS,
            "informativeness_horizon_min": HORIZON_MIN,
        },
        "sample": {
            "deep_matches": len(deep),
            "matches_used_leadlag": matches_used,
            "dense_runs": n_runs,
            "lag0_delta_pairs": n_delta_pairs,
            "informativeness_matches": info_n,
            "median_dense_snapshot_spacing_min": round(med_spacing_min, 2) if med_spacing_min else None,
        },
        "leadlag": {
            "peak": best,
            "lag0": lag0,
            "leader": leader,
            "approx_lead_minutes": lead_minutes,
            "xcorr_curve": xcorr,
            "interpretation": (
                "lag>0 => sportsbook delta at t+lag correlates with exchange delta at t "
                "(exchange leads); lag<0 => exchange follows sportsbook; lag=0 => contemporaneous."
            ),
        },
        "informativeness": {
            "horizon_min_before_close": HORIZON_MIN,
            "exchange_mean_abs_gap_to_close": round(exch_mean, 5) if exch_mean is not None else None,
            "sportsbook_mean_abs_gap_to_close": round(book_mean, 5) if book_mean is not None else None,
            "exchange_closer_matches": exch_wins,
            "sportsbook_closer_matches": book_wins,
            "paired_diff_book_minus_exch_mean": round(diff_mean, 5) if diff_mean is not None else None,
            "paired_t_stat": round(tstat, 2) if tstat is not None else None,
            "more_informative": (
                None if exch_mean is None else
                ("exchange" if exch_mean < book_mean else "sportsbook")
            ),
            "venue_ranking": venue_rank,
        },
        "data_caveat": (
            "Single source (theoddsapi) over a ~12-day window; all prices are "
            "PRE-KICKOFF. Snapshots share one capture-grid across books, so "
            "lead-lag is measured at the API poll cadence (~"
            f"{round(med_spacing_min,1) if med_spacing_min else '?'} min median in dense runs), "
            "NOT true tick latency: a lag of 0-1 snapshots cannot resolve sub-poll "
            "leadership. Cross-correlation pools first-differenced consensus "
            "moves across matches. Informativeness uses the last pre-kickoff "
            "snapshot as 'close'. Treat as indicative microstructure, not "
            "execution-grade latency."
        ),
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Wrote {OUT_PATH}")
    print(json.dumps({
        "peak_lag": best,
        "leader": leader,
        "approx_lead_minutes": lead_minutes,
        "lag0": lag0,
        "more_informative": out["informativeness"]["more_informative"],
        "exch_gap": exch_mean, "book_gap": book_mean,
        "exch_wins": exch_wins, "book_wins": book_wins,
        "paired_t": out["informativeness"]["paired_t_stat"],
        "matches_used": matches_used, "dense_runs": n_runs,
        "lag0_pairs": n_delta_pairs, "info_n": info_n,
    }, indent=2))


def _accumulate_venue_gaps(con, match_id, ts, close_val, venue_gaps, home, away):
    """At one horizon ts, record each individual book's |home_prob - close|."""
    cur = con.cursor()
    cur.execute(
        """SELECT json_extract(raw,'$.bookmaker_key'),
                  json_extract(raw,'$.outcome_name'), decimal_odds
           FROM odds_snapshots
           WHERE market='h2h' AND match_id=? AND ts_utc=?""",
        (match_id, ts),
    )
    bk_quotes = defaultdict(dict)
    for bk, outc, od in cur.fetchall():
        if bk and outc and od:
            bk_quotes[bk][outc] = od
    for bk, quotes in bk_quotes.items():
        if bk not in EXCHANGES and bk not in SPORTSBOOKS:
            continue
        p = home_prob_for_book(quotes, home, away)
        if p is not None:
            venue_gaps[bk].append(abs(p - close_val))


def _median_dense_spacing(db_path, deep):
    """Median consecutive-snapshot spacing (minutes) within dense runs, pooled."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    spacings = []
    for mid in deep:
        cur.execute(
            "SELECT DISTINCT ts_utc FROM odds_snapshots WHERE market='h2h' AND match_id=? ORDER BY ts_utc",
            (mid,),
        )
        ts = [_parse(r[0]) for r in cur.fetchall()]
        for i in range(1, len(ts)):
            g = (ts[i] - ts[i - 1]).total_seconds() / 60.0
            if 0 < g <= MAX_GAP_MIN:
                spacings.append(g)
    con.close()
    return float(np.median(spacings)) if spacings else None


if __name__ == "__main__":
    main()
