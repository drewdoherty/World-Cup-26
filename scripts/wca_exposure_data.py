#!/usr/bin/env python
"""Generate the portfolio-exposure feed (``site/exposure_data.json``).

Reads the open bet ledger (``data/wca.db``), the model's 1X2 predictions
(``data/model_predictions.json``), and the latest h2h odds snapshot (for
gap-plug suggestions), then writes the structured JSON the Scores & Markets
tab renders as the exposure column + Risk & Blind Spots panel.

Usage
-----
    python scripts/wca_exposure_data.py [--db data/wca.db] \
        [--preds data/model_predictions.json] [--out site/exposure_data.json]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import exposure  # noqa: E402


def _now_utc_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _open_bets(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM bets WHERE status='open'").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _odds_index(preds_fixtures):
    """Build {fixture: {outcome: {venue: best_odds}}} from the newest h2h snapshot.

    Outcome keys are the model fixture's home team / "Draw" / away team so the
    exposure engine can look up a plug price for any blind-spot outcome.
    """
    files = sorted(glob.glob("data/raw/snapshots/oddsapi_h2h_uk_*.json"),
                   key=os.path.getmtime)
    if not files:
        return {}
    rows = json.load(open(files[-1]))
    # fixture name -> (home, away)
    pairs = {}
    for f in preds_fixtures:
        if " vs " in f["fixture"]:
            h, a = f["fixture"].split(" vs ", 1)
            pairs[f["fixture"]] = (h, a)

    def canon(s):
        return (s or "").strip().lower()

    idx = {}
    for fixture, (home, away) in pairs.items():
        per_outcome = {}
        for r in rows:
            if r.get("market") != "h2h":
                continue
            if canon(home) not in canon(r.get("home_team")) and \
               canon(r.get("home_team")) not in canon(home):
                continue
            if canon(away) not in canon(r.get("away_team")) and \
               canon(r.get("away_team")) not in canon(away):
                continue
            name = r.get("outcome_name")
            outcome = "Draw" if canon(name) == "draw" else (
                home if canon(name) in canon(home) or canon(home) in canon(name)
                else away if canon(name) in canon(away) or canon(away) in canon(name)
                else None)
            if outcome is None:
                continue
            venue = r.get("bookmaker_key") or "?"
            price = r.get("decimal_odds")
            if price is None:
                continue
            cur = per_outcome.setdefault(outcome, {})
            if venue not in cur or price > cur[venue]:
                cur[venue] = float(price)
        if per_outcome:
            idx[fixture] = per_outcome
    return idx


def _scores_fixtures(scores_path):
    """Raw fixtures list from the fresh scores feed, or ``[]`` on failure."""
    try:
        return json.load(open(scores_path)).get("fixtures", []) or []
    except (OSError, json.JSONDecodeError):
        return []


def _odds_index_from_scores(sd_fixtures):
    """``{fixture: {outcome: {venue: best_odds}}}`` from scores_data per-venue
    1X2 prices — fresh and matched to the exact fixtures, for blind-spot plugs."""
    idx = {}
    for f in sd_fixtures:
        fixture = f.get("fixture") or ""
        if " vs " not in fixture:
            continue
        home, away = fixture.split(" vs ", 1)
        label = {"home": home, "draw": "Draw", "away": away}
        per_outcome = {}
        for v in f.get("venues") or []:
            venue = v.get("venue") or "?"
            sp = v.get("selection_prices") or {}
            for k in ("home", "draw", "away"):
                try:
                    price = float(sp.get(k))
                except (TypeError, ValueError):
                    continue
                if price > 1.0:
                    per_outcome.setdefault(label[k], {})[venue] = price
        if per_outcome:
            idx[fixture] = per_outcome
    return idx


def _results_index(path: str):
    """Map finished fixtures to ``{fixture: {"outcome", "score"}}``.

    Reads the manually-maintained results file; rows with outcome ``"pending"``
    (or missing) are skipped so only genuinely-settled games pin the floor.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        rows = json.load(open(path)).get("results", [])
    except Exception:
        return {}
    out = {}
    for r in rows:
        oc = str(r.get("outcome") or "").strip().lower()
        if oc in ("home", "draw", "away"):
            out[r.get("fixture")] = {"outcome": oc, "score": r.get("score")}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the exposure feed.")
    ap.add_argument("--db", default="data/wca.db")
    ap.add_argument("--scores", default="site/scores_data.json")
    ap.add_argument("--preds", default="data/model_predictions.json")
    ap.add_argument("--results", default="data/processed/wc2026_results.json")
    ap.add_argument("--out", default="site/exposure_data.json")
    args = ap.parse_args(argv)

    # Prefer the FRESH scores feed (live upcoming slate + per-venue prices) over
    # data/model_predictions.json, which the deploy can leave days stale
    # (deploy/sync.sh reverts it to its last commit each cycle).
    sd_fixtures = _scores_fixtures(args.scores)
    if sd_fixtures:
        fixtures = [
            {"fixture": f["fixture"], "kickoff": f.get("kickoff"),
             "model": f.get("model_1x2") or {}}
            for f in sd_fixtures if f.get("fixture") and f.get("model_1x2")
        ]
        odds_index = _odds_index_from_scores(sd_fixtures)
    else:
        try:
            fixtures = json.load(open(args.preds)).get("fixtures", [])
        except (OSError, json.JSONDecodeError):
            fixtures = []
        odds_index = _odds_index(fixtures)

    bets = _open_bets(args.db)
    results = _results_index(args.results)

    data = exposure.build_exposure_data(
        bets=bets, model_fixtures=fixtures,
        odds_index=odds_index, now_utc=_now_utc_str(), results=results,
    )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    p = data["portfolio"]
    print(args.out)
    print("fixtures=%d  blindspots=%d  EV=£%.2f  best=£%.2f  worst=£%.2f  "
          "P(profit)=%.0f%%  unmapped=%d"
          % (len(data["fixtures"]), len(data["blindspots"]), p["ev"],
             p["best"], p["worst"], p["p_profit"] * 100, len(data["unmapped"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
