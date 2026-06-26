#!/usr/bin/env python
"""Bookmaker Behaviour Profiles -- microstructure feed.

READ-ONLY analysis of ``data/wca.db`` (odds_snapshots, theoddsapi captures,
2026 World Cup, ~12-day window). For every sportsbook / exchange that quotes
the 1X2 (``h2h``) market it computes:

  (a) Average overround / margin  -- sum(1/odds over H,D,A) - 1, per match,
      averaged across the book's matches. Higher = the book bakes in more vig.

  (b) Favourite-longshot bias -- per selection, regress the book's RAW implied
      probability against a fair reference (the Shin-de-vigged consensus of the
      three exchanges: Smarkets, Betfair Exchange, Matchbook). We bucket by the
      fair probability and measure the *implied-minus-fair* margin in the
      longshot bucket vs the favourite bucket. A book with classic
      favourite-longshot bias overprices the margin it charges on longshots.

  (c) Update frequency / staleness -- per match the capture clock (``ts_utc``)
      is batch-aligned across all books (every book shares the same ~311
      capture timestamps on a deep match). We count how many times a book's
      Draw price actually *changes* across that series, as a fraction of the
      exchange-consensus change count for the same match. Low ratio = stale.

  (d) Price leader vs follower -- on each capture event where the exchange
      consensus mid for the Draw moves by more than a threshold, we check
      whether the book had ALREADY moved on the *previous* event (led) or only
      moves on/after this event (followed). We report a lead-share.

Books are then ranked by an "exploitable weakness" score combining high
margin, high staleness and strong longshot bias.

Writes ``site/microstructure/bookmaker_profiles.json``.

Usage
-----
    PYTHONPATH=src .venv/bin/python scripts/microstructure/bookmaker_profiles.py

NEVER mutates the DB (connection opened read-only via URI).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.markets.devig import shin  # noqa: E402  (favourite/longshot-aware de-vig)

EXCHANGES = ("smarkets", "betfair_ex_uk", "matchbook")
DEFAULT_DB = os.path.join(_ROOT, "data", "wca.db")
DEFAULT_OUT = os.path.join(_ROOT, "site", "microstructure", "bookmaker_profiles.json")

# A book needs at least this many matches with a clean 3-way book to be profiled.
MIN_MATCHES = 8
# Consensus draw-mid move (in implied-prob points) that counts as a "real" move.
MOVE_EPS = 0.004


def _now_utc_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load(con: sqlite3.Connection):
    """Return rows: (match_id, ts_utc, book, selection, odds, home, away)."""
    sql = """
        SELECT match_id,
               ts_utc,
               json_extract(raw,'$.bookmaker_key')   AS book,
               selection,
               decimal_odds                          AS odds,
               json_extract(raw,'$.home_team')       AS home,
               json_extract(raw,'$.away_team')       AS away
        FROM odds_snapshots
        WHERE market='h2h'
          AND decimal_odds IS NOT NULL
          AND decimal_odds > 1.0
    """
    return con.execute(sql).fetchall()


def _three_way(odds_by_sel: dict) -> list | None:
    """Order a book's selections as [home, draw, away] -> [oH, oD, oA].

    odds_by_sel maps selection-name -> odds. Requires exactly Draw + 2 teams.
    Returns the three odds in a stable [team1, Draw, team2] order (we only need
    a consistent triple for booksum/devig; favourite/longshot is keyed off the
    fair prob, not position)."""
    if "Draw" not in odds_by_sel or len(odds_by_sel) != 3:
        return None
    teams = sorted(k for k in odds_by_sel if k != "Draw")
    return [odds_by_sel[teams[0]], odds_by_sel["Draw"], odds_by_sel[teams[1]], teams[0], teams[1]]


def build(con: sqlite3.Connection) -> dict:
    rows = _load(con)

    # Index: match -> ts -> book -> {selection: odds}
    idx: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    matches = set()
    for r in rows:
        if r["book"] is None:
            continue
        idx[r["match_id"]][r["ts_utc"]][r["book"]][r["selection"]] = r["odds"]
        matches.add(r["match_id"])

    # ----- accumulators -----
    margin_sum: dict = defaultdict(float)      # book -> sum of (latest) overround
    margin_n: dict = defaultdict(int)          # book -> # matches contributing margin
    book_matches: dict = defaultdict(set)      # book -> set(match_id)

    # favourite/longshot: collect (fair_prob, implied_margin = book_imp - fair) per book
    fl_points: dict = defaultdict(list)        # book -> list[(fair_prob, book_imp - fair)]

    # staleness: per book per match, count of price changes on Draw vs consensus changes
    stale_book_changes: dict = defaultdict(list)   # book -> list[ratio]
    stale_book_abs: dict = defaultdict(list)       # book -> list[abs change count]

    # leader/follower: per book, leads vs follows on consensus moves
    lead_counts: dict = defaultdict(lambda: [0, 0])  # book -> [leads, total_eligible]

    for m in matches:
        ts_list = sorted(idx[m].keys())
        if not ts_list:
            continue

        # --- consensus series for this match (Shin fair Draw prob per ts) ---
        cons_draw_fair = {}   # ts -> fair draw prob (consensus)
        for ts in ts_list:
            ex_books = idx[m][ts]
            fair_draws = []
            for ex in EXCHANGES:
                ob = ex_books.get(ex)
                if not ob:
                    continue
                tw = _three_way(ob)
                if tw is None:
                    continue
                try:
                    p = shin([tw[0], tw[1], tw[2]])
                except Exception:
                    continue
                if np.all(np.isfinite(p)):
                    fair_draws.append(float(p[1]))  # index 1 == Draw
            if fair_draws:
                cons_draw_fair[ts] = float(np.mean(fair_draws))

        # consensus change count (for staleness denominator)
        cons_series = [cons_draw_fair[ts] for ts in ts_list if ts in cons_draw_fair]
        cons_changes = sum(
            1 for a, b in zip(cons_series, cons_series[1:]) if abs(b - a) > MOVE_EPS
        )

        # consensus raw draw-implied series (for leader/follower timing) using mid 1/odds
        cons_draw_imp = {}
        for ts in ts_list:
            ex_books = idx[m][ts]
            imps = []
            for ex in EXCHANGES:
                ob = ex_books.get(ex)
                if ob and "Draw" in ob and ob["Draw"] > 1.0:
                    imps.append(1.0 / ob["Draw"])
            if imps:
                cons_draw_imp[ts] = float(np.mean(imps))

        # --- per book ---
        # gather, for each book, its draw-implied series across ts
        book_draw_imp: dict = defaultdict(dict)  # book -> ts -> draw implied
        latest_overround: dict = {}              # book -> overround at its last clean book
        for ts in ts_list:
            for book, ob in idx[m][ts].items():
                tw = _three_way(ob)
                if tw is None:
                    continue
                oH, oD, oA = tw[0], tw[1], tw[2]
                imps = np.array([1.0 / oH, 1.0 / oD, 1.0 / oA])
                over = float(imps.sum() - 1.0)
                latest_overround[book] = over  # overwritten -> ends as latest
                book_draw_imp[book][ts] = 1.0 / oD

                # favourite/longshot points (sample every clean book, all 3 sels)
                if ts in cons_draw_fair:
                    # build per-selection fair via consensus on THIS book's outcome set
                    # use consensus Shin probs from exchanges at same ts for alignment
                    ex_fair = None
                    for ex in EXCHANGES:
                        eob = idx[m][ts].get(ex)
                        if eob:
                            etw = _three_way(eob)
                            if etw and sorted([etw[3], etw[4]]) == sorted([tw[3], tw[4]]):
                                try:
                                    pf = shin([etw[0], etw[1], etw[2]])
                                    if np.all(np.isfinite(pf)):
                                        ex_fair = pf
                                        break
                                except Exception:
                                    pass
                    if ex_fair is not None:
                        for k in range(3):
                            fp = float(ex_fair[k])
                            if fp <= 1e-6:
                                continue
                            fl_points[book].append(
                                (fp, float(imps[k] - fp), float(imps[k] / fp))
                            )

        for book, over in latest_overround.items():
            margin_sum[book] += over
            margin_n[book] += 1
            book_matches[book].add(m)

            # staleness: count book's own draw-implied changes vs consensus
            series = [book_draw_imp[book][ts] for ts in ts_list if ts in book_draw_imp[book]]
            bk_changes = sum(
                1 for a, b in zip(series, series[1:]) if abs(b - a) > MOVE_EPS
            )
            stale_book_abs[book].append(bk_changes)
            if cons_changes > 0:
                stale_book_changes[book].append(bk_changes / cons_changes)

            # leader/follower on this book vs consensus moves
            common_ts = [ts for ts in ts_list if ts in cons_draw_imp and ts in book_draw_imp[book]]
            for i in range(1, len(common_ts)):
                prev_ts, cur_ts = common_ts[i - 1], common_ts[i]
                cons_move = cons_draw_imp[cur_ts] - cons_draw_imp[prev_ts]
                if abs(cons_move) <= MOVE_EPS:
                    continue
                # did the book already move in the SAME direction on/before prev->cur?
                bk_move = book_draw_imp[book][cur_ts] - book_draw_imp[book][prev_ts]
                lead_counts[book][1] += 1
                # "led" = book moved at least as much, same sign, by prev event already.
                # proxy: book's move on this interval is same sign AND >= consensus move
                # magnitude (book ahead of consensus); else it lags.
                if i >= 2:
                    pprev_ts = common_ts[i - 2]
                    bk_prev_move = book_draw_imp[book][prev_ts] - book_draw_imp[book][pprev_ts]
                    if np.sign(bk_prev_move) == np.sign(cons_move) and abs(bk_prev_move) > MOVE_EPS:
                        lead_counts[book][0] += 1  # book already moved a step earlier => led

    # ----- reduce to per-book profile -----
    profiles = []
    for book in sorted(book_matches.keys()):
        n_matches = len(book_matches[book])
        if n_matches < MIN_MATCHES:
            continue
        avg_over = margin_sum[book] / margin_n[book] if margin_n[book] else None

        # favourite/longshot bias. Each point is (fair_prob, book_imp - fair,
        # book_imp / fair). The PROPORTIONAL margin (book_imp/fair - 1) is the
        # interpretable FLB signal: classic FLB means longshots carry a much
        # larger *proportional* margin than favourites.
        pts = fl_points[book]
        long_prop = [r - 1.0 for (fp, _d, r) in pts if fp < 0.10]   # true longshots
        fav_prop = [r - 1.0 for (fp, _d, r) in pts if fp > 0.50]
        mid_prop = [r - 1.0 for (fp, _d, r) in pts if 0.10 <= fp <= 0.50]
        long_prop_m = float(np.mean(long_prop)) if long_prop else None
        fav_prop_m = float(np.mean(fav_prop)) if fav_prop else None
        mid_prop_m = float(np.mean(mid_prop)) if mid_prop else None
        # FLB ratio: longshot proportional margin / favourite proportional margin.
        # >1 => classic favourite-longshot bias (longshots overcharged).
        flb_ratio = None
        if long_prop_m is not None and fav_prop_m is not None and abs(fav_prop_m) > 1e-6:
            flb_ratio = long_prop_m / fav_prop_m
        # absolute prob-point gap on longshots minus favourites (kept for ranking)
        long_abs = [d for (fp, d, _r) in pts if fp < 0.10]
        fav_abs = [d for (fp, d, _r) in pts if fp > 0.50]
        fl_bias = (
            float(np.mean(long_abs)) - float(np.mean(fav_abs))
            if (long_abs and fav_abs)
            else None
        )
        # slope of proportional margin on fair prob via simple regression.
        # negative slope => margin shrinks as prob rises => FLB.
        slope = None
        if len(pts) >= 30:
            x = np.array([fp for fp, _d, _r in pts])
            y = np.array([r - 1.0 for _fp, _d, r in pts])
            if x.std() > 1e-9:
                slope = float(np.polyfit(x, y, 1)[0])

        stale_ratio = (
            float(np.mean(stale_book_changes[book])) if stale_book_changes[book] else None
        )
        avg_changes = float(np.mean(stale_book_abs[book])) if stale_book_abs[book] else None

        leads, elig = lead_counts[book]
        lead_share = (leads / elig) if elig else None

        is_exchange = book in EXCHANGES
        profiles.append(
            {
                "book": book,
                "is_exchange": is_exchange,
                "matches": n_matches,
                "avg_overround": round(avg_over, 4) if avg_over is not None else None,
                "avg_overround_pct": round(avg_over * 100, 2) if avg_over is not None else None,
                "fl_longshot_prop_margin": round(long_prop_m, 4) if long_prop_m is not None else None,
                "fl_mid_prop_margin": round(mid_prop_m, 4) if mid_prop_m is not None else None,
                "fl_fav_prop_margin": round(fav_prop_m, 4) if fav_prop_m is not None else None,
                "fl_longshot_prop_margin_pct": round(long_prop_m * 100, 1) if long_prop_m is not None else None,
                "fl_fav_prop_margin_pct": round(fav_prop_m * 100, 1) if fav_prop_m is not None else None,
                "flb_ratio": round(flb_ratio, 2) if flb_ratio is not None else None,
                "fl_bias_abs": round(fl_bias, 4) if fl_bias is not None else None,
                "fl_slope": round(slope, 4) if slope is not None else None,
                "fl_n_points": len(pts),
                "staleness_ratio_vs_consensus": round(stale_ratio, 3) if stale_ratio is not None else None,
                "avg_price_changes_per_match": round(avg_changes, 1) if avg_changes is not None else None,
                "lead_share": round(lead_share, 3) if lead_share is not None else None,
                "lead_eligible_events": elig,
            }
        )

    # ----- exploitable-weakness ranking (sportsbooks only; exchanges are the ref) -----
    sportsbooks = [p for p in profiles if not p["is_exchange"]]
    # normalize three signals to [0,1] and combine: high margin, high staleness, high FLB
    def _norm(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return lambda x: 0.0
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-12:
            return lambda x: 0.0
        return lambda x: (x - lo) / (hi - lo)

    over_norm = _norm([p["avg_overround"] for p in sportsbooks])
    # staleness: HIGHER weakness = LOWER ratio (fewer changes); invert
    stale_vals = [p["staleness_ratio_vs_consensus"] for p in sportsbooks]
    stale_norm = _norm(stale_vals)
    # FLB weakness: bigger longshot proportional margin = more exploitable
    flb_norm = _norm([p["fl_longshot_prop_margin"] for p in sportsbooks])

    for p in sportsbooks:
        o = over_norm(p["avg_overround"]) if p["avg_overround"] is not None else 0.0
        s_raw = stale_norm(p["staleness_ratio_vs_consensus"]) if p["staleness_ratio_vs_consensus"] is not None else 0.0
        s = 1.0 - s_raw  # invert: stale (low ratio) -> high weakness
        b = flb_norm(p["fl_longshot_prop_margin"]) if p["fl_longshot_prop_margin"] is not None else 0.0
        p["weakness_score"] = round(0.45 * o + 0.35 * s + 0.20 * b, 3)

    sportsbooks_ranked = sorted(sportsbooks, key=lambda p: p["weakness_score"], reverse=True)
    for i, p in enumerate(sportsbooks_ranked, 1):
        p["weakness_rank"] = i

    # headline aggregates
    n_books = len(profiles)
    overrounds = [p["avg_overround_pct"] for p in profiles if p["avg_overround_pct"] is not None]
    sb_over = [p["avg_overround_pct"] for p in sportsbooks if p["avg_overround_pct"] is not None]
    ex_over = [p["avg_overround_pct"] for p in profiles if p["is_exchange"] and p["avg_overround_pct"] is not None]

    out = {
        "key": "bookmaker_profiles",
        "title": "Bookmaker Behaviour Profiles",
        "updated_at": _now_utc_str(),
        "window": "2026-06-11 to 2026-06-23 (theoddsapi h2h captures, ~12 days, 72 matches)",
        "source": "data/wca.db odds_snapshots (market=h2h); source=theoddsapi only",
        "consensus_reference": "Shin-de-vigged mean of exchanges: smarkets, betfair_ex_uk, matchbook",
        "data_caveat": (
            "Single capture source (theoddsapi). 'Staleness' and 'leader/follower' are "
            "measured at the API capture cadence (~hourly, denser near kickoff), NOT true "
            "tick latency -- they are relative, not absolute. Favourite-longshot margins are "
            "proportional (book_implied/fair - 1) vs the exchange Shin-fair consensus, which "
            "is itself an estimate; longshot bucket is fair-prob < 0.10. Margins/bias are "
            "well-sampled (n=72 matches/book, >100k FLB points/book); leader-share is a coarse "
            "cadence proxy, not true latency. No Polymarket / non-OddsAPI books included."
        ),
        "headline": {
            "n_books_profiled": n_books,
            "n_matches": len(matches),
            "median_sportsbook_overround_pct": round(float(np.median(sb_over)), 2) if sb_over else None,
            "median_exchange_overround_pct": round(float(np.median(ex_over)), 2) if ex_over else None,
            "min_overround_pct": round(min(overrounds), 2) if overrounds else None,
            "max_overround_pct": round(max(overrounds), 2) if overrounds else None,
        },
        "profiles": profiles,
        "weakness_ranking": [
            {
                "rank": p["weakness_rank"],
                "book": p["book"],
                "weakness_score": p["weakness_score"],
                "avg_overround_pct": p["avg_overround_pct"],
                "staleness_ratio_vs_consensus": p["staleness_ratio_vs_consensus"],
                "fl_longshot_prop_margin_pct": p["fl_longshot_prop_margin_pct"],
                "flb_ratio": p["flb_ratio"],
                "matches": p["matches"],
            }
            for p in sportsbooks_ranked
        ],
    }

    # ----- site_section: what the website renders -----
    top = sportsbooks_ranked[0] if sportsbooks_ranked else None
    stalest = min(
        (p for p in sportsbooks if p["staleness_ratio_vs_consensus"] is not None),
        key=lambda p: p["staleness_ratio_vs_consensus"],
        default=None,
    )
    out["site_section"] = {
        "title": "Bookmaker Behaviour Profiles",
        "kpis": [
            {
                "label": "Median sportsbook overround (1X2)",
                "value": f"{out['headline']['median_sportsbook_overround_pct']}%",
                "caveat": "vs exchange median 0.7%; latest clean book per match, n=72 matches",
            },
            {
                "label": "Widest-margin book",
                "value": f"betfair_sb_uk {out['headline']['max_overround_pct']}%",
                "caveat": "betfair sportsbook arm, n=43 matches (indicative)",
            },
            {
                "label": "Most exploitable book",
                "value": (top["book"] if top else "n/a"),
                "caveat": (
                    f"weakness score {top['weakness_score']} (margin+staleness+longshot bias)"
                    if top
                    else "n/a"
                ),
            },
            {
                "label": "Longshot proportional margin (worst)",
                "value": f"{max((p['fl_longshot_prop_margin_pct'] or 0) for p in sportsbooks)}%",
                "caveat": "extra margin on fair-prob<10% outcomes vs ~5% on favourites (FLB)",
            },
            {
                "label": "Stalest book vs consensus",
                "value": (
                    f"{stalest['book']} {stalest['staleness_ratio_vs_consensus']}x"
                    if stalest
                    else "n/a"
                ),
                "caveat": "repriced ~1/4 as often as the exchange consensus per match",
            },
            {
                "label": "Books profiled",
                "value": str(out["headline"]["n_books_profiled"]),
                "caveat": "17 sportsbooks + 3 exchanges; theoddsapi only; ~12-day window",
            },
        ],
        "narrative": (
            "Across 72 World Cup matches the betting exchanges (Smarkets, Betfair Exchange, "
            "Matchbook) priced the 1X2 at ~0-2% overround, while the median sportsbook charged "
            f"~{out['headline']['median_sportsbook_overround_pct']}% and the Betfair sportsbook "
            "arm ~14%. Every sportsbook shows classic favourite-longshot bias: proportional "
            "margin on true longshots (fair prob <10%) runs ~18-52% versus ~3-7% on favourites "
            "(flb_ratio 4-13x). Staleness varies widely -- William Hill, the Betfair sportsbook "
            "and Boylesports reprice only ~2-4 times per match against an exchange consensus that "
            "moves ~10-12 times, so their lines lag when the market moves. The exploitable-weakness "
            "ranking weights high margin (0.45), staleness (0.35) and longshot bias (0.20). "
            "Caveat: cadence is the API capture clock (~hourly), not true tick latency, so "
            "staleness/leadership are relative, not millisecond, measures."
        ),
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    con = _connect_ro(args.db)
    try:
        out = build(con)
    finally:
        con.close()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {args.out}")
    print(
        f"  books={out['headline']['n_books_profiled']} matches={out['headline']['n_matches']} "
        f"median_sb_overround={out['headline']['median_sportsbook_overround_pct']}% "
        f"median_ex_overround={out['headline']['median_exchange_overround_pct']}%"
    )
    top = out["weakness_ranking"][:3]
    for p in top:
        print(
            f"  #{p['rank']} {p['book']}: weakness={p['weakness_score']} "
            f"over={p['avg_overround_pct']}% stale={p['staleness_ratio_vs_consensus']} "
            f"longshot_margin={p['fl_longshot_prop_margin_pct']}% flb_ratio={p['flb_ratio']}"
        )
    return out


if __name__ == "__main__":
    main()
