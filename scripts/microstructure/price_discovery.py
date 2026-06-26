#!/usr/bin/env python
"""Price Discovery & Convergence analysis (READ-ONLY).

For the 1X2 (``h2h``) market on the deep-series matches in ``data/wca.db``,
this script reconstructs the consensus implied-probability time-series across
the intraday snapshot history and measures:

  (a) DRIFT MAGNITUDE  - how far the de-vigged consensus moves from the first
      pre-kickoff capture to the last pre-kickoff capture (total-variation
      distance + signed favourite move), over all deep matches.

  (b) CONVERGENCE / LATENCY PROFILE - for the subset of matches whose *final*
      pre-kickoff capture is genuinely close to kickoff (so it approximates a
      closing line), what fraction of the total pre-kickoff movement has
      already completed by T-24h / T-6h / T-1h.

  (c) FIRST-MOVER RANKING - when the consensus favourite price steps, which
      bookmaker_key tended to already be ahead of the consensus in the move
      direction (i.e. led the move). Books ranked by lead frequency and by
      lead-per-appearance, against the uniform baseline.

It writes a small JSON feed for the website at
``site/microstructure/price_discovery.json`` and NEVER mutates the database
(connection opened read-only).

Usage
-----
    PYTHONPATH=src .venv/bin/python scripts/microstructure/price_discovery.py
        [--db data/wca.db] [--out site/microstructure/price_discovery.json]

Data caveats (see also the ``data_caveat`` field in the JSON):
  * Source is theoddsapi ONLY; capture window 2026-06-11..2026-06-23 (~12 days).
  * Capture STOPPED on 2026-06-23, but many deep matches kick off later, so for
    most matches the last capture is hours/days before kickoff. The drift
    measure (a) uses the last PRE-kickoff capture in-window as its endpoint and
    is honest about that. The convergence profile (b) is therefore restricted
    to the handful of matches with late, near-kickoff coverage -> small n,
    INDICATIVE not significant.
  * All bookmakers share one capture timestamp per pull (~3.2 min cadence); the
    per-book ``retrieved_at`` only spans the ~1 min API call, so sub-capture
    lead/lag between books is NOT observable. First-mover is detected ACROSS
    consecutive captures.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

# Deep series = matches with this many or more distinct capture-times.
DEEP_MIN_CAPTURES = 300
# A consensus favourite move of at least this (in probability) counts as a "step".
STEP_THRESHOLD = 0.01  # 1 percentage point
# A book is "ahead of consensus" in the move direction by at least this much.
AHEAD_EPS = 0.002  # 0.2 pp
# Convergence profile: final capture must be within this many hours of kickoff
# to be treated as a proxy closing line.
CLOSE_GATE_HOURS = 12.0
CONVERGENCE_THRESHOLDS = (24.0, 6.0, 1.0)  # hours before kickoff
# Minimum appearances for a book to be ranked on lead-per-appearance.
MIN_APPEAR_FOR_RATE = 30


def _pp(ts: str) -> _dt.datetime:
    """Parse an ISO-8601 timestamp (with or without trailing Z)."""
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _now_utc_str() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the DB strictly read-only via a file: URI."""
    uri = "file:%s?mode=ro" % os.path.abspath(db_path)
    return sqlite3.connect(uri, uri=True)


def _deep_matches(cur) -> list:
    rows = cur.execute(
        """
        SELECT match_id,
               json_extract(raw,'$.commence_time') AS ko,
               json_extract(raw,'$.home_team')     AS home,
               json_extract(raw,'$.away_team')     AS away,
               COUNT(DISTINCT ts_utc)              AS ncap
        FROM odds_snapshots
        WHERE market='h2h'
        GROUP BY match_id, ko
        """
    ).fetchall()
    return [r for r in rows if r[4] >= DEEP_MIN_CAPTURES]


def _consensus_series(cur, match_id: str, ko: str):
    """Return list of (hrs_to_ko, normalised_consensus_probs_dict) for
    PRE-kickoff captures only, plus the raw per-book per-ts dict for first-mover
    detection.

    Consensus per selection = MEDIAN of (1/odds) across all books at that ts,
    then proportionally normalised to sum to 1 (proportional de-vig).
    """
    ko_dt = _pp(ko)
    data = cur.execute(
        """
        SELECT ts_utc, selection,
               json_extract(raw,'$.bookmaker_key') AS bk,
               decimal_odds
        FROM odds_snapshots
        WHERE match_id=? AND market='h2h'
        """,
        (match_id,),
    ).fetchall()

    # ts -> selection -> list of implied probs ; ts -> bk -> selection -> implied
    by_ts_sel = defaultdict(lambda: defaultdict(list))
    by_ts_bk = defaultdict(lambda: defaultdict(dict))
    for ts, sel, bk, od in data:
        if od and od > 1 and _pp(ts) <= ko_dt:
            imp = 1.0 / od
            by_ts_sel[ts][sel].append(imp)
            if bk:
                by_ts_bk[ts][bk][sel] = imp

    times = sorted(by_ts_sel)
    series = []
    for ts in times:
        cons = {s: statistics.median(v) for s, v in by_ts_sel[ts].items()}
        tot = sum(cons.values())
        if tot <= 0:
            continue
        norm = {s: v / tot for s, v in cons.items()}
        series.append(((ko_dt - _pp(ts)).total_seconds() / 3600.0, norm, ts))
    return series, by_ts_bk


def _tvd(a: dict, b: dict) -> float:
    """Total-variation distance between two prob dicts over their shared keys."""
    common = set(a) & set(b)
    return sum(abs(a[k] - b[k]) for k in common) / 2.0


def _quantiles(xs: list) -> dict:
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return {}
    return {
        "n": n,
        "min": round(xs[0], 4),
        "p25": round(xs[n // 4], 4),
        "median": round(statistics.median(xs), 4),
        "mean": round(statistics.mean(xs), 4),
        "p75": round(xs[(3 * n) // 4], 4),
        "p90": round(xs[min(n - 1, int(n * 0.9))], 4),
        "max": round(xs[-1], 4),
    }


def analyse(db_path: str) -> dict:
    con = _connect_ro(db_path)
    cur = con.cursor()
    try:
        deep = _deep_matches(cur)
        n_books = cur.execute(
            "SELECT COUNT(DISTINCT json_extract(raw,'$.bookmaker_key')) "
            "FROM odds_snapshots WHERE market='h2h'"
        ).fetchone()[0]

        # ---- (a) drift magnitude + cache series for later passes ----
        tvds = []
        fav_moves = []  # signed favourite probability change
        last_gap_hours = []  # final pre-ko capture's hours-before-kickoff
        cached = []  # (match_id, ko, home, away, series, by_ts_bk)
        for match_id, ko, home, away, _nc in deep:
            series, by_ts_bk = _consensus_series(cur, match_id, ko)
            if len(series) < 3:
                continue
            first = series[0][1]
            final = series[-1][1]
            tvd = _tvd(first, final)
            fav = max(first, key=first.get)
            fav_moves.append(final.get(fav, first[fav]) - first[fav])
            tvds.append(tvd)
            last_gap_hours.append(series[-1][0])
            cached.append((match_id, ko, home, away, series, by_ts_bk))

        drift_stats = _quantiles(tvds)
        fav_stats = _quantiles(fav_moves)

        # ---- (b) convergence / latency profile (gated near-kickoff subset) ----
        prof = {t: [] for t in CONVERGENCE_THRESHOLDS}
        n_qual = 0
        for (_m, _ko, _h, _a, series, _bk) in cached:
            if series[-1][0] > CLOSE_GATE_HOURS:
                continue  # final capture too far from KO to act as closing line
            n_qual += 1
            first = series[0][1]
            final = series[-1][1]
            total = _tvd(first, final)
            if total < 1e-6:
                continue
            for T in CONVERGENCE_THRESHOLDS:
                cands = [s for s in series if s[0] >= T]
                if not cands:
                    continue
                state = cands[-1][1]  # last capture at-or-before threshold T
                prof[T].append(1.0 - _tvd(state, final) / total)

        convergence = {}
        for T in CONVERGENCE_THRESHOLDS:
            v = prof[T]
            convergence["T-%dh" % int(T)] = {
                "n": len(v),
                "median_frac_done": round(statistics.median(v), 3) if v else None,
                "mean_frac_done": round(statistics.mean(v), 3) if v else None,
            }

        # ---- (c) first-mover ranking ----
        lead = Counter()
        appear = Counter()
        total_steps = 0
        for (_m, _ko, _h, _a, series, by_ts_bk) in cached:
            first = series[0][1]
            fav = max(first, key=first.get)
            for i in range(1, len(series)):
                hrs_prev, prev, ts_prev = series[i - 1]
                hrs_cur, cur_state, _ts_cur = series[i]
                if fav not in prev or fav not in cur_state:
                    continue
                d = cur_state[fav] - prev[fav]
                if abs(d) < STEP_THRESHOLD:
                    continue
                total_steps += 1
                direction = 1.0 if d > 0 else -1.0
                vp = prev[fav]
                # which books were already ahead-of-consensus in the move dir at ts_prev?
                movers = []
                for bk, sels in by_ts_bk.get(ts_prev, {}).items():
                    if fav in sels:
                        appear[bk] += 1
                        if direction * (sels[fav] - vp) > AHEAD_EPS:
                            movers.append((bk, direction * (sels[fav] - vp)))
                if movers:
                    movers.sort(key=lambda x: -x[1])
                    lead[movers[0][0]] += 1  # most-ahead book leads this step

        ranking = []
        for bk, c in lead.most_common():
            ap = appear[bk]
            ranking.append(
                {
                    "book": bk,
                    "leads": c,
                    "appearances": ap,
                    "lead_per_appearance": round(c / ap, 3) if ap else None,
                    "rate_reliable": ap >= MIN_APPEAR_FOR_RATE,
                }
            )
        uniform_baseline = round(1.0 / max(1, n_books - 1), 3)

        # ---- window metadata ----
        wrow = cur.execute(
            "SELECT MIN(ts_utc), MAX(ts_utc) FROM odds_snapshots WHERE market='h2h'"
        ).fetchone()
        n_near_ko = sum(1 for g in last_gap_hours if g <= CLOSE_GATE_HOURS)

        out = {
            "meta": {
                "key": "price_discovery",
                "title": "Price Discovery & Convergence (1X2)",
                "updated_at": _now_utc_str(),
                "window": {
                    "first_capture_utc": wrow[0],
                    "last_capture_utc": wrow[1],
                    "approx_days": 12,
                },
                "source": "theoddsapi",
                "n_deep_matches": len(cached),
                "n_books": n_books,
                "data_caveat": (
                    "Single source (theoddsapi). Capture stopped 2026-06-23 while "
                    "many matches kick off later, so for most deep matches the last "
                    "in-window capture is hours/days before kickoff. Drift (a) uses "
                    "the last PRE-kickoff capture as endpoint and labels this. The "
                    "convergence profile (b) is restricted to the %d matches with a "
                    "final capture within %dh of kickoff -> INDICATIVE, n is small. "
                    "Books share one capture timestamp per ~3.2-min pull, so "
                    "first-mover (c) is detected across consecutive captures, not "
                    "sub-second." % (n_near_ko, int(CLOSE_GATE_HOURS))
                ),
            },
            "drift": {
                "metric": "total_variation_distance_of_devigged_consensus",
                "first_to_last_prekickoff": drift_stats,
                "favourite_signed_prob_change": fav_stats,
                "note": "TVD units are probability mass moved (0.01 = 1pp). "
                "Favourite is the highest-prob selection at first capture.",
            },
            "convergence": {
                "definition": "fraction of total first->final pre-kickoff TVD "
                "already completed by each kickoff-relative threshold",
                "close_gate_hours": CLOSE_GATE_HOURS,
                "n_qualifying_matches": n_qual,
                "profile": convergence,
            },
            "first_mover": {
                "definition": "at each >=1pp consensus-favourite step, the book "
                "most ahead-of-consensus in the move direction is credited the lead",
                "total_steps": total_steps,
                "uniform_baseline_lead_rate": uniform_baseline,
                "min_appearances_for_reliable_rate": MIN_APPEAR_FOR_RATE,
                "ranking": ranking,
            },
            "secondary_markets_note": (
                "Totals (70 matches) and BTTS (24 matches) exist but with far "
                "shallower intraday series (~238 capture-times total vs ~315 for "
                "h2h) and were not profiled here; h2h is the only market with a "
                "deep per-match series suitable for convergence work."
            ),
        }
        return out
    finally:
        con.close()


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(repo, "data", "wca.db"))
    ap.add_argument(
        "--out",
        default=os.path.join(repo, "site", "microstructure", "price_discovery.json"),
    )
    args = ap.parse_args(argv)

    result = analyse(args.db)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print("wrote %s" % args.out)
    print(
        "deep matches=%d | drift median TVD=%.4f | fav median move=%+.4f"
        % (
            result["meta"]["n_deep_matches"],
            result["drift"]["first_to_last_prekickoff"]["median"],
            result["drift"]["favourite_signed_prob_change"]["median"],
        )
    )
    top = result["first_mover"]["ranking"][:3]
    print("top first-movers:", ", ".join("%s(%d)" % (r["book"], r["leads"]) for r in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
