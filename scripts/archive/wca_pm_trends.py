#!/usr/bin/env python
"""Polymarket price-trajectory line charts (multi-team, faceted) — generator.

For each market of interest (stage) and each look-back period, renders one
two-facet line figure (raw ¢ on top, % change below) with one line per team,
ordered by soonest kickoff. Also emits an "exposure" set restricted to teams we
hold open bets on.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_trends.py --out reports/trends
    PYTHONPATH=src python3 scripts/wca_pm_trends.py --stages R16,win --periods 24,all
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmhistory, pmmovers, pmtrends  # noqa: E402

_DEF_JSONL = os.path.join(_ROOT, "data", "pm_price_history.jsonl")
_DEF_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
_DEF_DB = os.path.join(_ROOT, "data", "wca.db")


def _records(jsonl, db, extra=None):
    records = list(pmhistory.load_records(jsonl))
    for ex in (extra or []):
        if ex and os.path.exists(ex):
            records += list(pmhistory.load_records(ex))
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            cur = con.execute(
                "SELECT ts_utc, kind, team, stage, market_slug, token_id, pm_mid, model_prob FROM pm_snapshots")
            cols = [c[0] for c in cur.description]
            records += [dict(zip(cols, r)) for r in cur.fetchall()]
            con.close()
        except Exception:
            pass
    return records


def _parse_periods(spec):
    """Parse 'hours@binmin' tokens, e.g. '24@30,168@60,all'."""
    if not spec:
        return list(pmtrends.DEFAULT_PERIODS)
    out = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower() in ("all", "full"):
            out.append(("Full history · native", None, None))
            continue
        h, _, b = tok.partition("@")
        hours = float(h)
        binm = float(b) if b else None
        lbl = "Last %gh" % hours + (" · %g-min" % binm if binm else "")
        out.append((lbl, hours, binm))
    return out or list(pmtrends.DEFAULT_PERIODS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jsonl", default=_DEF_JSONL)
    ap.add_argument("--extra", default="", help="comma list of extra JSONL sources (e.g. archive backfill)")
    ap.add_argument("--db", default=_DEF_DB)
    ap.add_argument("--scores", default=_DEF_SCORES)
    ap.add_argument("--out", default=os.path.join(_ROOT, "reports", "pm_trends"))
    ap.add_argument("--stages", default="win_match,win", help="comma list of stages/markets")
    ap.add_argument("--periods", default="", help="'hours@binmin' list, e.g. 24@30,168@60,all")
    ap.add_argument("--top", type=int, default=7)
    args = ap.parse_args(argv)

    extra = [e.strip() for e in args.extra.split(",") if e.strip()]
    records = _records(args.jsonl, args.db, extra=extra)
    if not records:
        print("No PM snapshots found.")
        return 1
    recs = pmmovers.clean_records(records)
    kickoffs = pmtrends.load_kickoffs(args.scores)
    # Only feature fixtures still upcoming relative to the latest snapshot.
    anchor0 = pmmovers.anchor_time(recs)
    if anchor0 is not None:
        kickoffs = {t: k for t, k in kickoffs.items() if k >= anchor0}
    known = sorted({str(r.get("team")) for r in recs if r.get("team")})
    expo = pmtrends.exposure_teams(args.db, known)
    periods = _parse_periods(args.periods)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]

    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    anchor = pmmovers.anchor_time(recs)
    print("Snapshots: %d valid · anchor %s · kickoffs for %d teams · exposure: %s"
          % (len(recs), anchor.strftime("%Y-%m-%d %H:%M UTC") if anchor else "n/a",
             len(kickoffs), ", ".join(sorted(expo)) or "none"))

    written = 0
    # General set: soonest-kickoff teams per market.
    for stage in stages:
        figs = pmtrends.build_market_figures(
            recs, stage=stage, periods=periods, kickoffs=kickoffs, top_n=args.top)
        for f in figs:
            if not f["png"]:
                continue
            path = "%s_%s_%s.png" % (args.out, stage, f["period"].replace(" ", "").lower())
            with open(path, "wb") as fh:
                fh.write(f["png"])
            written += 1
            print("  %-22s %-13s -> %s  [%s]" % (f["market"], f["period"], path, ", ".join(f["teams"])))

    # Exposure set: only teams we hold, full history per market.
    if expo:
        for stage in stages:
            figs = pmtrends.build_market_figures(
                recs, stage=stage, periods=[("Full history", None)], kickoffs=kickoffs,
                teams=sorted(expo), top_n=args.top,
                require_live=False, scope_label="OUR EXPOSURE")
            for f in figs:
                if not f["png"]:
                    continue
                path = "%s_exposure_%s.png" % (args.out, stage)
                with open(path, "wb") as fh:
                    fh.write(f["png"])
                written += 1
                print("  [exposure] %-13s %-13s -> %s  [%s]"
                      % (f["market"], f["period"], path, ", ".join(f["teams"])))

    print("Wrote %d figures." % written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
