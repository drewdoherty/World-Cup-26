#!/usr/bin/env python3
"""Closing Line Value (CLV) microstructure analysis — READ-ONLY.

Two analyses, written to ``site/microstructure/clv.json`` for the website:

PART 1 — Early generosity by book (from ``odds_snapshots``).
    For each match (1X2 / ``h2h`` market) we build a de-vigged consensus
    *closing* fair probability per outcome from the last pre-kickoff capture
    across all books (Shin de-vig per book, then average fair probs across
    books). We then look at every book's *early* price at ~T-24h before
    kickoff and ask: if you had taken that early price, what is the EV implied
    by the eventual fair close?

        edge = early_decimal_odds * fair_close_prob - 1

    A positive average edge means the book systematically posts *generous*
    early prices that the consensus close later shortens past — i.e. an early
    price you can beat the close with. Negative means the book is already
    sharp / stale-tight early. Books are ranked by mean early-vs-close edge.

    We also measure, per book, how its *own* closing price sits vs the
    cross-book consensus close (a "sharp close" diagnostic): a book whose close
    is consistently longer than consensus is leaving value on the table at the
    bell; a book tighter than consensus has the sharpest close.

PART 2 — Realized CLV from the ledger (``bets``), n=25 — INDICATIVE ONLY.
    Distribution of realized CLV (``taken_odds / closing_odds - 1``) and its
    relationship to realized P&L. n=25 is far below significance; reported as
    a directional sanity check, never as proof.

Run:  PYTHONPATH=src .venv/bin/python scripts/microstructure/clv.py
Never mutates the DB (opens read-only).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from wca.markets.devig import shin

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "wca.db")
OUT_PATH = os.path.join(ROOT, "site", "microstructure", "clv.json")

# Early anchor: target lead time before kickoff, and the tolerance window the
# nearest capture must fall inside to count as a valid "T-24h" observation.
EARLY_LEAD_HOURS = 24.0
EARLY_TOL_HOURS = 8.0  # accept the capture closest to T-24h within +/- 8h

# A capture counts as "pre-kickoff close-eligible" only if at/ before KO.
# The consensus close is the latest capture per book at or before kickoff.

# Books with fewer than this many (match, outcome) early observations are
# reported but flagged thin.
MIN_OBS_FOR_RANK = 30


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts)


def _fair_probs(odds_by_sel: Dict[str, float], order: Sequence[str]) -> Optional[Dict[str, float]]:
    """Shin de-vig a single book's 1X2 quote into fair probs keyed by selection.

    Returns ``None`` if the book does not quote all three outcomes or any quote
    is invalid (odds <= 1).
    """
    odds = []
    for sel in order:
        o = odds_by_sel.get(sel)
        if o is None or o <= 1.0:
            return None
        odds.append(o)
    try:
        p = shin(odds)
    except Exception:
        return None
    return {sel: float(p[i]) for i, sel in enumerate(order)}


def load_match_series(con: sqlite3.Connection) -> Dict[str, dict]:
    """Return per-match structure: kickoff + per-book per-selection time series.

    Structure: ``{match_id: {"ko": dt, "order": [sels], "series": {bk: {sel: [(dt,odds)]}}}}``
    Only the ``h2h`` (1X2) market is used.
    """
    cur = con.execute(
        "SELECT match_id, ts_utc, decimal_odds, raw FROM odds_snapshots WHERE market='h2h'"
    )
    matches: Dict[str, dict] = {}
    for match_id, ts_utc, odds, raw in cur:
        j = json.loads(raw)
        ko = j.get("commence_time")
        bk = j.get("bookmaker_key")
        sel = j.get("outcome_name")
        if not (ko and bk and sel) or odds is None:
            continue
        m = matches.get(match_id)
        if m is None:
            m = {"ko": ko, "series": defaultdict(lambda: defaultdict(list)), "sels": set()}
            matches[match_id] = m
        # Keep the earliest commence_time seen (duplicate-KO artifacts a few min apart).
        if ko < m["ko"]:
            m["ko"] = ko
        m["series"][bk][sel].append((ts_utc, float(odds)))
        m["sels"].add(sel)
    # Finalize: parse KO, fix selection order (home/away/draw -> deterministic).
    out: Dict[str, dict] = {}
    for mid, m in matches.items():
        sels = sorted(m["sels"])
        if len(sels) != 3:
            continue  # need a clean three-way book
        out[mid] = {
            "ko": _parse(m["ko"]),
            "order": sels,
            "series": {bk: {s: sorted(v) for s, v in bks.items()} for bk, bks in m["series"].items()},
        }
    return out


def _price_at(series: List[Tuple[str, float]], target: dt.datetime, tol_hours: float) -> Optional[Tuple[dt.datetime, float]]:
    """Nearest (ts, odds) to ``target`` within ``tol_hours``; else None."""
    best = None
    best_gap = None
    for ts, od in series:
        t = _parse(ts)
        gap = abs((t - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = (t, od)
    if best is None or best_gap is None:
        return None
    if best_gap > tol_hours * 3600:
        return None
    return best


def _last_pre_ko(series: List[Tuple[str, float]], ko: dt.datetime) -> Optional[Tuple[dt.datetime, float]]:
    """Latest (ts, odds) at or before kickoff (allow up to 5 min slack)."""
    cutoff = ko + dt.timedelta(minutes=5)
    pre = [(_parse(ts), od) for ts, od in series if _parse(ts) <= cutoff]
    if not pre:
        return None
    return max(pre, key=lambda x: x[0])


def analyze_early_generosity(matches: Dict[str, dict]) -> dict:
    """PART 1: rank books by early-vs-consensus-close edge."""
    # Per book accumulators.
    edge_obs: Dict[str, List[float]] = defaultdict(list)  # early edge vs fair close
    close_vs_consensus: Dict[str, List[float]] = defaultdict(list)  # book close odds / consensus fair odds - 1
    match_coverage = 0
    total_matches = len(matches)

    for mid, m in matches.items():
        ko = m["ko"]
        order = m["order"]
        series = m["series"]
        target_early = ko - dt.timedelta(hours=EARLY_LEAD_HOURS)

        # --- Build consensus close: fair probs per book at last pre-KO capture ---
        book_close_fair: Dict[str, Dict[str, float]] = {}
        for bk, sels in series.items():
            close_odds = {}
            ok = True
            for sel in order:
                pp = _last_pre_ko(sels.get(sel, []), ko)
                if pp is None:
                    ok = False
                    break
                close_odds[sel] = pp[1]
            if not ok:
                continue
            fair = _fair_probs(close_odds, order)
            if fair is not None:
                book_close_fair[bk] = fair

        if len(book_close_fair) < 3:
            continue  # not enough books to form a consensus close
        match_coverage += 1

        # Consensus fair close prob per outcome = mean across books.
        consensus_fair = {}
        for sel in order:
            vals = [f[sel] for f in book_close_fair.values()]
            consensus_fair[sel] = sum(vals) / len(vals)
        # Renormalize (means need not sum to 1).
        s = sum(consensus_fair.values())
        consensus_fair = {k: v / s for k, v in consensus_fair.items()}
        consensus_fair_odds = {k: (1.0 / v if v > 0 else None) for k, v in consensus_fair.items()}

        # --- Per book: early price edge vs consensus fair close ---
        for bk, sels in series.items():
            for sel in order:
                pp = _price_at(sels.get(sel, []), target_early, EARLY_TOL_HOURS)
                if pp is None:
                    continue
                early_odds = pp[1]
                p_close = consensus_fair[sel]
                edge = early_odds * p_close - 1.0  # EV of taking the early price if fair close is true
                edge_obs[bk].append(edge)

        # --- Per book: own close vs consensus close (sharp-close diagnostic) ---
        for bk, fair in book_close_fair.items():
            # Use the book's own raw closing odds vs consensus fair odds.
            for sel in order:
                pp = _last_pre_ko(series[bk].get(sel, []), ko)
                if pp is None:
                    continue
                book_close_odds = pp[1]
                cons_odds = consensus_fair_odds[sel]
                if cons_odds and cons_odds > 0:
                    close_vs_consensus[bk].append(book_close_odds / cons_odds - 1.0)

    # Build ranking.
    ranking = []
    for bk, edges in edge_obs.items():
        n = len(edges)
        mean_edge = statistics.mean(edges)
        med_edge = statistics.median(edges)
        sd = statistics.pstdev(edges) if n > 1 else 0.0
        se = sd / math.sqrt(n) if n > 0 else 0.0
        cvc = close_vs_consensus.get(bk, [])
        ranking.append({
            "book": bk,
            "n_early_obs": n,
            "mean_early_edge_pct": round(mean_edge * 100, 3),
            "median_early_edge_pct": round(med_edge * 100, 3),
            "se_pct": round(se * 100, 3),
            "pct_early_beats_close": round(100.0 * sum(1 for e in edges if e > 0) / n, 1) if n else None,
            "mean_close_vs_consensus_pct": round(statistics.mean(cvc) * 100, 3) if cvc else None,
            "n_close_obs": len(cvc),
            "thin": n < MIN_OBS_FOR_RANK,
        })
    ranking.sort(key=lambda r: r["mean_early_edge_pct"], reverse=True)

    all_edges = [e for v in edge_obs.values() for e in v]
    return {
        "ranking": ranking,
        "total_matches": total_matches,
        "matches_with_consensus_close": match_coverage,
        "total_early_obs": len(all_edges),
        "pooled_mean_early_edge_pct": round(statistics.mean(all_edges) * 100, 3) if all_edges else None,
        "pooled_pct_early_beats_close": round(100.0 * sum(1 for e in all_edges if e > 0) / len(all_edges), 1) if all_edges else None,
        "early_lead_hours": EARLY_LEAD_HOURS,
        "early_tol_hours": EARLY_TOL_HOURS,
    }


def analyze_ledger_clv(con: sqlite3.Connection) -> dict:
    """PART 2: realized CLV distribution + CLV->P&L (n=25, indicative only)."""
    rows = con.execute(
        """
        SELECT clv, settled_pl, stake, status, decimal_odds, closing_odds
        FROM bets
        WHERE clv IS NOT NULL
          AND status IN ('won','lost','void','push','half_won','half_lost')
        """
    ).fetchall()
    clvs = [r[0] for r in rows if r[0] is not None]
    n = len(clvs)
    if n == 0:
        return {"n": 0, "note": "no settled bets carry CLV"}

    pls = [r[1] for r in rows if r[1] is not None]
    stakes = [r[2] for r in rows]
    # ROI per bet (P&L / stake), pair with CLV for correlation.
    paired = [(r[0], r[1] / r[2]) for r in rows if r[0] is not None and r[1] is not None and r[2]]

    mean_clv = statistics.mean(clvs)
    med_clv = statistics.median(clvs)
    beat = sum(1 for x in clvs if x > 0)
    pushed = sum(1 for x in clvs if x == 0)

    # Pearson correlation CLV vs per-bet ROI (n small; report with caveat).
    corr = None
    if len(paired) >= 3:
        xs = [p[0] for p in paired]
        ys = [p[1] for p in paired]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if dx > 0 and dy > 0:
            corr = num / (dx * dy)

    # Split P&L by positive- vs negative-CLV bucket (directional only).
    pos = [(r[1] / r[2]) for r in rows if r[0] is not None and r[0] > 0 and r[1] is not None and r[2]]
    neg = [(r[1] / r[2]) for r in rows if r[0] is not None and r[0] < 0 and r[1] is not None and r[2]]

    # Histogram for the chart.
    bins = [(-1.0, -0.10), (-0.10, -0.05), (-0.05, -0.01), (-0.01, 0.01),
            (0.01, 0.05), (0.05, 0.10), (0.10, 1.0)]
    hist = []
    for lo, hi in bins:
        cnt = sum(1 for x in clvs if (lo <= x < hi) or (hi == 1.0 and x >= lo and x <= hi))
        hist.append({"lo_pct": round(lo * 100, 1), "hi_pct": round(hi * 100, 1), "count": cnt})

    return {
        "n": n,
        "mean_clv_pct": round(mean_clv * 100, 3),
        "median_clv_pct": round(med_clv * 100, 3),
        "pct_beat_close": round(100.0 * beat / n, 1),
        "n_pushes": pushed,
        "mean_roi_pos_clv": round(statistics.mean(pos) * 100, 2) if pos else None,
        "n_pos_clv": len(pos),
        "mean_roi_neg_clv": round(statistics.mean(neg) * 100, 2) if neg else None,
        "n_neg_clv": len(neg),
        "corr_clv_roi": round(corr, 3) if corr is not None else None,
        "histogram": hist,
        "caveat": "n=25 settled bets with CLV — far below significance; directional only.",
    }


def main() -> None:
    # Open strictly read-only.
    uri = f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        matches = load_match_series(con)
        part1 = analyze_early_generosity(matches)
        part2 = analyze_ledger_clv(con)

        win_lo, win_hi = con.execute(
            "SELECT min(ts_utc), max(ts_utc) FROM odds_snapshots"
        ).fetchone()
    finally:
        con.close()

    feed = {
        "key": "clv",
        "title": "Closing Line Value & Early-Price Generosity",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window": {"from": win_lo, "to": win_hi, "matches": part1["total_matches"]},
        "data_caveat": (
            "Single source (theoddsapi), ~12-day window, 72 matches. Early anchor "
            f"= nearest capture to T-{int(EARLY_LEAD_HOURS)}h within +/-{int(EARLY_TOL_HOURS)}h; "
            "consensus close = Shin-devigged mean fair prob across all books at the last "
            "pre-kickoff capture. Ledger CLV is n=25 (indicative, not significant). "
            "No out-of-sample / future-match validation yet."
        ),
        "early_generosity": part1,
        "ledger_clv": part2,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(feed, f, indent=2)
    print(f"wrote {OUT_PATH}")
    # Console summary.
    print(f"PART1: {part1['matches_with_consensus_close']}/{part1['total_matches']} matches with consensus close; "
          f"{part1['total_early_obs']} early obs; pooled mean early edge {part1['pooled_mean_early_edge_pct']}%")
    top = part1["ranking"][:5]
    for r in top:
        print(f"  + {r['book']:16s} mean_early_edge={r['mean_early_edge_pct']:+.2f}% "
              f"(n={r['n_early_obs']}, beats_close={r['pct_early_beats_close']}%)")
    bot = part1["ranking"][-3:]
    for r in bot:
        print(f"  - {r['book']:16s} mean_early_edge={r['mean_early_edge_pct']:+.2f}% (n={r['n_early_obs']})")
    print(f"PART2: n={part2['n']} mean_clv={part2.get('mean_clv_pct')}% beat_close={part2.get('pct_beat_close')}% "
          f"corr_clv_roi={part2.get('corr_clv_roi')}")


if __name__ == "__main__":
    main()
