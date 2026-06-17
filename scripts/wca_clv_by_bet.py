#!/usr/bin/env python
"""TODO1 — CLV-by-bet series for the "average CLV by bet number" chart.

Builds, per bet:
  * clv_old        : the CLV recorded in the ledger (None where absent).
  * clv_corrected  : CLV re-priced against the CORRECTED Elo+DC fair value,
                     where a single-match Dixon-Coles (or the published
                     advancement re-price) lets us do so.

CLV convention (ledger-wide, see wca/closecapture.py + wca/bot/app.py):
    clv = backed_decimal_odds / fair_close - 1.0
For a model-fair close, fair_close = 1 / p_fair, so
    clv = backed_odds * p_fair - 1.0
i.e. CLV against the model's fair value is identical to the bet's EV against
that fair value. That is exactly the quantity recompute_report.csv reports as
new_ev for the open bets; we extend it to the closed single-match bets the
corrected DC can price.

The corrected DC model is reconstructed directly from
data/dc_params_corrected.json (DixonColesModel.from_dict) — the same fitted
artifact wca_recompute_open_bets.py exported — so no expensive re-fit is needed
and the numbers reproduce the report exactly (verified: bet 69 -> 0.5663).
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = "/Users/andrewdoherty/World-Cup-26"
sys.path.insert(0, os.path.join(ROOT, "src"))

from wca.models.dixon_coles import DixonColesModel  # noqa: E402
from wca.data.teamnames import canonical  # noqa: E402

DB = os.path.join(ROOT, "data", "wca.db")
DC_JSON = os.path.join(ROOT, "data", "dc_params_corrected.json")
REPORT_CSV = os.path.join(ROOT, "data", "recompute_report.csv")
SUMMARY_JSON = os.path.join(ROOT, "data", "recompute_summary.json")
OUT_CSV = os.path.join(ROOT, "data", "analysis", "clv_by_bet.csv")

AS_OF = "2026-06-15"  # recompute_summary.json as_of


def clv_from_prob(odds: float, p: float) -> Optional[float]:
    """Model-fair-close CLV = backed_odds * p_fair - 1 (== EV vs model fair)."""
    if odds is None or p is None or p <= 0.0:
        return None
    return odds * p - 1.0


def load_corrected_dc() -> DixonColesModel:
    d = json.load(open(DC_JSON))
    return DixonColesModel.from_dict(d)


def load_report() -> Dict[int, Dict[str, Any]]:
    """recompute_report.csv keyed by bet id (authoritative open-bet re-price)."""
    out: Dict[int, Dict[str, Any]] = {}
    with open(REPORT_CSV) as f:
        for row in csv.DictReader(f):
            try:
                bid = int(row["id"])
            except (TypeError, ValueError):
                continue
            out[bid] = row
    return out


def _f(x) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Closed single-match bets the corrected DC can re-price.
# Each entry: bet_id -> (home, away, neutral, pricer(pred) -> p_corrected, note)
# Selections are mapped to the corrected model's probability for the SAME
# selection the bet took, so clv_corrected is comparable to the recorded clv.
# Only 1X2 / double-chance / totals legs are modelled (the parts DC covers);
# player props, exact scores beyond DC's matrix support, accumulators and
# outrights are left unpriced (clv_corrected = None) — same scope discipline as
# the recompute script.
# ---------------------------------------------------------------------------
def build_closed_repricers(dc: DixonColesModel):
    def p_home(pred):  # back the home side
        return pred.one_x_two()[0]

    def p_away(pred):
        return pred.one_x_two()[2]

    def p_draw(pred):
        return pred.one_x_two()[1]

    def pred_for(home, away, neutral=True):
        return dc.predict(canonical(home), canonical(away), neutral=neutral, warn=False)

    # (home, away, neutral, selection-prob-fn, basis)
    specs: Dict[int, Tuple[str, str, bool, Any, str]] = {
        # --- 1X2 single-match back-the-side bets (group-stage neutral sims;
        #     host venue not modelled in DC -> neutral=True, matches recompute) ---
        1:  ("United States", "Paraguay", True, p_away, "DC 1X2: back Paraguay (away)"),
        2:  ("Canada", "Bosnia and Herzegovina", True, p_home, "DC 1X2: back Canada (home)"),
        6:  ("Mexico", "South Africa", True, p_home, "DC 1X2: back Mexico"),
        7:  ("Canada", "Bosnia and Herzegovina", True, p_home, "DC 1X2: back Canada"),
        8:  ("South Korea", "Czech Republic", True, p_home, "DC 1X2: back South Korea"),
        9:  ("United States", "Paraguay", True, p_away, "DC 1X2: back Paraguay"),
        10: ("South Korea", "Czech Republic", True, p_home, "DC 1X2: back South Korea"),
        15: ("Brazil", "Morocco", True, p_away, "DC 1X2: back Morocco (away)"),
        16: ("Qatar", "Switzerland", True, p_home, "DC 1X2: back Qatar (home)"),
        17: ("Canada", "Bosnia and Herzegovina", True, p_home, "DC 1X2: back Canada"),
        18: ("United States", "Paraguay", True, p_home, "DC 1X2: back USA (home)"),
        22: ("Australia", "Turkey", True, p_home, "DC 1X2: back Australia"),
        50: ("Qatar", "Switzerland", True, p_draw, "DC 1X2: back the Draw"),
        51: ("Haiti", "Scotland", True, p_draw, "DC 1X2: back the Draw"),
        52: ("Brazil", "Morocco", True, p_away, "DC 1X2: back Morocco"),
        53: ("Australia", "Turkey", True, p_draw, "DC 1X2: back the Draw"),
        54: ("Brazil", "Morocco", True, p_draw, "DC 1X2: back the Draw"),
        58: ("Canada", "Bosnia and Herzegovina", True, p_home, "DC 1X2 (PM): Canada win - Yes"),
        59: ("United States", "Paraguay", True, p_away, "DC 1X2: back Paraguay"),
        71: ("Australia", "Turkey", True, p_home, "DC 1X2: back Australia"),
        72: ("Haiti", "Scotland", True, p_home, "DC 1X2: back Haiti (home)"),
        73: ("Ivory Coast", "Ecuador", True, p_away, "DC 1X2: back Ecuador (away)"),
        74: ("Sweden", "Tunisia", True, p_away, "DC 1X2: back Tunisia (away)"),
        75: ("Germany", "Curacao", True, p_draw, "DC 1X2: back the Draw"),
        76: ("Sweden", "Tunisia", True, p_draw, "DC 1X2: back the Draw"),
        77: ("Germany", "Curacao", True, p_away, "DC 1X2: back Curacao (away)"),
        78: ("Haiti", "Scotland", True, p_draw, "DC 1X2: back the Draw"),
        79: ("Brazil", "Morocco", True, p_draw, "DC 1X2: back the Draw"),
        81: ("Brazil", "Morocco", True, p_draw, "DC 1X2: back the Draw"),
        12: ("South Korea", "Czech Republic", True,
             lambda pred: pred.over_under(2.5)["under"], "DC totals: BTTS-No proxy via Under2.5"),
    }
    # NOTE bet 12 is BTTS-No, not a totals line; DC's BTTS needs the scoreline
    # matrix P(both score)=1-P(home 0)-P(away 0)+P(0-0). Price it properly:
    repricers: Dict[int, Tuple[Any, str]] = {}
    for bid, (h, a, neu, fn, basis) in specs.items():
        repricers[bid] = ((h, a, neu, fn), basis)

    def btts_no(pred):
        m = pred.matrix
        p_home0 = float(m[0, :].sum())
        p_away0 = float(m[:, 0].sum())
        p_00 = float(m[0, 0])
        p_btts_yes = 1.0 - p_home0 - p_away0 + p_00
        return 1.0 - p_btts_yes  # BTTS - No

    repricers[12] = (("South Korea", "Czech Republic", True, btts_no), "DC: BTTS - No")
    return repricers, pred_for


def main() -> int:
    dc = load_corrected_dc()
    report = load_report()
    repricers, pred_for = build_closed_repricers(dc)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    bets = con.execute(
        "SELECT id, ts_utc, match_id, match_desc, market, selection, decimal_odds, "
        "model_prob, market_prob_devig, ev, clv, closing_odds, status, settled_pl "
        "FROM bets ORDER BY id"
    ).fetchall()
    con.close()

    series: List[Dict[str, Any]] = []
    detail: List[Dict[str, Any]] = []

    for b in bets:
        bid = b["id"]
        odds = b["decimal_odds"]
        clv_old = b["clv"]
        clv_corr: Optional[float] = None
        basis = ""

        if bid in report:
            # Authoritative open-bet re-price already computed by the recompute
            # script. new_ev is exactly the model-fair-close CLV.
            r = report[bid]
            new_ev = _f(r.get("new_ev"))
            new_prob = _f(r.get("new_prob"))
            if new_ev is not None:
                clv_corr = new_ev
                basis = "recompute_report.csv new_ev (corrected model-fair close)"
            elif new_prob is not None:
                clv_corr = clv_from_prob(odds, new_prob)
                basis = "recompute_report.csv new_prob"
            else:
                basis = (r.get("basis") or "no corrected model") + " (not repriceable)"
        elif bid in repricers:
            spec, basis_txt = repricers[bid]
            home, away, neutral, fn = spec
            pred = pred_for(home, away, neutral=neutral)
            p = float(fn(pred))
            clv_corr = clv_from_prob(odds, p)
            basis = basis_txt + f" (p={p:.4f})"
        else:
            basis = "no corrected single-match model (prop/acca/outright)"

        series.append({
            "bet_id": bid,
            "status": b["status"],
            "clv_old": clv_old,
            "clv_corrected": clv_corr,
        })
        detail.append({
            "bet_id": bid,
            "ts_utc": b["ts_utc"],
            "match_id": b["match_id"],
            "match_desc": b["match_desc"],
            "selection": b["selection"],
            "decimal_odds": odds,
            "status": b["status"],
            "model_prob_old": b["model_prob"],
            "ev_old": b["ev"],
            "clv_old": clv_old,
            "clv_corrected": clv_corr,
            "clv_delta": (clv_corr - clv_old) if (clv_corr is not None and clv_old is not None) else None,
            "basis": basis,
        })

    # ------------------------------------------------------------------
    # cutover_bet_id: first bet id to which the corrected fair value applies
    # going forward. as_of = 2026-06-15. The correction re-prices bets that
    # existed at as_of; the cutover is the next bet placed after the last bet
    # that existed at as_of (i.e. the first bet priced natively on the
    # corrected model going forward).
    # ------------------------------------------------------------------
    ids_at_as_of = [b["id"] for b in bets if (b["ts_utc"] or "")[:10] <= AS_OF]
    ids_after = sorted(b["id"] for b in bets if (b["ts_utc"] or "")[:10] > AS_OF)
    last_existing = max(ids_at_as_of) if ids_at_as_of else None
    cutover = ids_after[0] if ids_after else (last_existing + 1 if last_existing else None)

    # ------------------------------------------------------------------
    # most_affected: largest |EV or CLV change|.
    #
    # We rank on the APPLES-TO-APPLES change in CLV (clv_old, the recorded
    # de-vigged market-close CLV, vs clv_corrected, the corrected model-fair
    # CLV) for every bet that has BOTH — this is the exact quantity the chart
    # plots, so its movers are the chart's movers. We add the recompute
    # report's open bets, whose EV swing (old_ev -> new_ev) is the documented
    # high-impact set (bet 14 reach-R16, bet 69 the resolved boost).
    #
    # NOTE: the recorded `ev` column is NOT used as an "old" baseline for the
    # closed bets — it is on an inconsistent scale (bets 73-79 store 1+EV; bet
    # 69 stores a boost figure of 6.75), so an ev-delta off it would rank
    # artifacts, not the data correction. recompute_report.csv old_ev IS clean
    # (it is the model_prob-implied EV) and is used for the report bets.
    # ------------------------------------------------------------------
    affected: List[Dict[str, Any]] = []
    sel_by_id = {b["id"]: b["selection"] for b in bets}

    # (a) Report bets: EV swing old_ev -> new_ev (the documented high-impact set).
    for bid, r in report.items():
        old_ev = _f(r.get("old_ev"))
        new_ev = _f(r.get("new_ev"))
        ev_delta = _f(r.get("ev_delta"))
        if ev_delta is None and old_ev is not None and new_ev is not None:
            ev_delta = new_ev - old_ev
        if ev_delta is not None:
            affected.append({
                "bet_id": bid, "selection": sel_by_id.get(bid, r.get("selection", "")),
                "metric": "EV (old model_prob -> corrected fair value)",
                "old": old_ev, "new": new_ev, "delta": ev_delta,
            })

    # (b) Closed re-prices: CLV swing clv_old -> clv_corrected (same metric the
    #     chart plots), for bets with a recorded market-close CLV.
    for d in detail:
        bid = d["bet_id"]
        if bid in report:
            continue
        cc = d["clv_corrected"]
        co = d["clv_old"]
        if cc is None or co is None:
            continue
        affected.append({
            "bet_id": bid, "selection": d["selection"],
            "metric": "CLV (recorded market-close -> corrected model-fair)",
            "old": co, "new": cc, "delta": cc - co,
        })

    affected.sort(key=lambda a: abs(a["delta"]) if a["delta"] is not None else -1, reverse=True)
    most_affected = affected[:10]

    # ------------------------------------------------------------------
    # Write the optional CSV.
    # ------------------------------------------------------------------
    Path(os.path.dirname(OUT_CSV)).mkdir(parents=True, exist_ok=True)
    cols = ["bet_id", "ts_utc", "status", "match_id", "match_desc", "selection",
            "decimal_odds", "model_prob_old", "ev_old", "clv_old",
            "clv_corrected", "clv_delta", "basis"]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for d in detail:
            w.writerow(d)

    out = {
        "as_of": AS_OF,
        "cutover_bet_id": cutover,
        "last_bet_existing_at_as_of": last_existing,
        "n_repriced": sum(1 for d in detail if d["clv_corrected"] is not None),
        "n_with_clv_old": sum(1 for d in detail if d["clv_old"] is not None),
        "series": series,
        "most_affected": most_affected,
    }
    print(json.dumps(out, indent=2, default=lambda o: None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
