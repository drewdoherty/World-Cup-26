#!/usr/bin/env python
"""Recompute fair value / EV of currently-open WC2026 bets on the CORRECTED
(Elo + Dixon-Coles) models fitted to the cleaned martj42 dataset.

What "corrected models" means here: the models are refit on
``data/raw/martj42_cleaned.csv`` (raw martj42 + verified corrections overlay),
so they reflect accurate team strength. The fitted ratings/params are exported
to ``data/elo_ratings_corrected.json`` and ``data/dc_params_corrected.json``.

Each open bet is repriced with the RIGHT tool for its market:
  * advancement markets (reach R16 / eliminated R32) -> corrected tournament sim
  * single-match goals / double-chance legs           -> corrected Dixon-Coles
  * outrights (golden boot)                            -> no Elo+DC model (skipped)

The old (pre-correction) fair value is the ``model_prob`` stored in the DB at
placement, so old-vs-new isolates the effect of the data correction.

NOTE on the advancement sim: it is a COLD-START simulation (it does not condition
on group games already played as of the eval date). That is the correct basis
for "how much did fixing the data move our fair value", and matches how these
bets were originally priced — but it is NOT a live in-tournament probability.
Flagged clearly in the report.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.card import fit_models  # noqa: E402
from wca.data.results import load_results  # noqa: E402
from wca.data.cleaning import resolve_results_path  # noqa: E402
from wca.advancement import run_advancement  # noqa: E402

DB = "data/wca.db"
EVAL_DATE = "2026-06-15"
N_SIMS = 20000


def kelly(prob: float, odds: float) -> float:
    edge = prob * odds - 1.0
    return edge / (odds - 1.0) if odds > 1.0 else 0.0


def ev(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def recommend(old_ev: Optional[float], new_ev: float) -> str:
    """Apply the GREEN/YELLOW/RED/BLUE rules from the brief."""
    if old_ev is not None and old_ev < 0 <= new_ev:
        return "BLUE (was -EV, now +EV: candidate for additional stake)"
    if old_ev is not None and (new_ev - old_ev) > 0.01 and new_ev > 0:
        tag = "GREEN (confidence restored, hold)"
    elif new_ev < 0:
        tag = "YELLOW (now -EV: flag for lay/close)"
    else:
        tag = "GREEN (hold)" if new_ev > 0 else "YELLOW"
    if old_ev is not None and abs(new_ev - old_ev) > 0.02:
        tag += " | RED (material >2pp EV swing: review cash-out)"
    return tag


def main() -> int:
    # ------------------------------------------------------------------
    # 1. Fit corrected models on the cleaned dataset + export artifacts.
    # ------------------------------------------------------------------
    path = resolve_results_path()
    print(f"Fitting corrected models on: {path}")
    results = load_results(path)
    models = fit_models(results)
    rater, dc = models.rater, models.dc

    Path("data/elo_ratings_corrected.json").write_text(
        json.dumps(
            {"as_of": EVAL_DATE, "source": path, "n_matches": models.n_matches,
             "ratings": {t: round(r, 2) for t, r in sorted(
                 rater.ratings.items(), key=lambda kv: -kv[1])}},
            indent=2, ensure_ascii=False) + "\n")
    dc_dict = dc.to_dict()
    # The brief names the home-advantage term "gamma"; the model stores it as
    # "home_advantage". Expose both so the file matches the requested schema.
    dc_dict["gamma"] = dc_dict.get("home_advantage")
    Path("data/dc_params_corrected.json").write_text(
        json.dumps({"as_of": EVAL_DATE, "source": path, **dc_dict},
                   indent=2, ensure_ascii=False) + "\n")
    print(f"  exported corrected Elo ({len(rater.ratings)} teams) + DC params "
          f"(gamma={dc_dict.get('gamma')}, rho={dc_dict.get('rho')}, mu={dc_dict.get('mu')})")

    # ------------------------------------------------------------------
    # 2. Corrected tournament sim (cold-start) for advancement markets.
    # ------------------------------------------------------------------
    print(f"Running corrected advancement sim (n={N_SIMS}) ...")
    adv = run_advancement(models, n_sims=N_SIMS, seed=42)

    def reach(team: str, stage: str) -> Optional[float]:
        if team not in adv.index:
            return None
        return float(adv.loc[team, stage])

    # ------------------------------------------------------------------
    # 3. Open bets.
    # ------------------------------------------------------------------
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, match_id, match_desc, market, selection, decimal_odds, "
        "stake, model_prob, ev FROM bets WHERE status='open' ORDER BY id"
    ).fetchall()
    con.close()

    report: List[Dict[str, Any]] = []
    for b in rows:
        rec: Dict[str, Any] = {
            "id": b["id"], "match_id": b["match_id"], "market": b["market"],
            "selection": b["selection"], "decimal_odds": b["decimal_odds"],
            "stake": b["stake"],
            "old_prob": b["model_prob"], "old_ev": b["ev"],
            "new_prob": None, "new_ev": None, "new_kelly": None,
            "prob_delta": None, "ev_delta": None,
            "recommendation": "", "basis": "", "flag": "",
        }
        odds = b["decimal_odds"]
        sel = b["selection"]
        mid = b["match_id"]

        if mid == "WC2026_JPN_R16":
            p_r16 = reach("Japan", "P(R16)")
            new_no = 1.0 - p_r16            # selection is "Japan reach R16 - NO"
            rec.update(new_prob=new_no, basis="corrected tournament sim (cold-start)")
        elif mid == "PM_ghana_r32":
            # "eliminated in R32 - NO" wins unless Ghana is knocked out exactly at
            # the R32 round: P(elim at R32) = P(reach R32) - P(reach R16).
            p_r32, p_r16 = reach("Ghana", "P(R32)"), reach("Ghana", "P(R16)")
            p_elim_r32 = max(0.0, p_r32 - p_r16)
            new_no = 1.0 - p_elim_r32
            rec.update(new_prob=new_no,
                       basis="corrected sim; market-definition caveat (see report)")
        elif mid == "AUSTUR_BB_BOOST":
            # Match already played 13 Jun (Australia 2-0 Turkey) -> RESOLVED.
            pred = dc.predict("Australia", "Turkey", neutral=True)
            ou = pred.over_under(3.5)
            h, d, a = pred.one_x_two()
            dc_not_draw = h + a
            model_combo = ou["under"] * dc_not_draw  # cards leg not modelled
            rec.update(new_prob=model_combo,
                       basis="DC pre-match (Under3.5 x DC-not-draw); cards leg unmodelled",
                       flag="RESOLVED 13 Jun: AUS 2-0 TUR (Under3.5 WON, DC WON; cards leg pending) "
                            "-> SETTLE, do not re-price")
        elif mid == "WC2026_GOLDEN_BOOT":
            rec.update(basis="outright golden boot: no Elo+DC model",
                       recommendation="N/A (no match model)")
            report.append(rec)
            continue
        else:
            rec.update(basis="unrecognised market", recommendation="N/A")
            report.append(rec)
            continue

        new_ev = ev(rec["new_prob"], odds)
        rec["new_ev"] = new_ev
        rec["new_kelly"] = kelly(rec["new_prob"], odds)
        if rec["old_prob"] is not None:
            rec["prob_delta"] = rec["new_prob"] - rec["old_prob"]
        if rec["old_ev"] is not None:
            rec["ev_delta"] = new_ev - rec["old_ev"]
        if mid == "AUSTUR_BB_BOOST":
            rec["recommendation"] = "RED — match already played; settle now (see flag)"
        else:
            rec["recommendation"] = recommend(rec["old_ev"], new_ev)
        report.append(rec)

    _write_outputs(report, adv)
    return 0


def _f(x, pct=False, signed=False):
    if x is None:
        return ""
    if pct:
        s = f"{x*100:+.2f}%" if signed else f"{x*100:.2f}%"
        return s
    return f"{x:+.4f}" if signed else f"{x:.4f}"


def _write_outputs(report: List[Dict[str, Any]], adv) -> None:
    import csv

    # CSV
    cols = ["id", "match_id", "market", "selection", "old_prob", "new_prob",
            "prob_delta", "old_ev", "new_ev", "ev_delta", "new_kelly",
            "recommendation", "basis", "flag"]
    with open("data/recompute_report.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in report:
            w.writerow(r)

    # JSON summary
    scored = [r for r in report if r["new_ev"] is not None]
    summary = {
        "as_of": EVAL_DATE,
        "total_open": len(report),
        "repriced": len(scored),
        "now_positive_ev": sum(1 for r in scored if r["new_ev"] >= 0),
        "now_negative_ev": sum(1 for r in scored if r["new_ev"] < 0),
        "high_impact_changes": [
            {"id": r["id"], "selection": r["selection"],
             "ev_delta": round(r["ev_delta"], 4) if r["ev_delta"] is not None else None,
             "recommendation": r["recommendation"]}
            for r in scored
            if (r["ev_delta"] is not None and abs(r["ev_delta"]) > 0.02)
            or "RED" in r["recommendation"]
        ],
        "needs_settlement": [r["id"] for r in report if "RESOLVED" in r.get("flag", "")],
    }
    Path("data/recompute_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    # TXT report + table
    lines: List[str] = []
    lines.append("WC2026 OPEN-BET FAIR-VALUE RECOMPUTE (corrected Elo + Dixon-Coles)")
    lines.append(f"Evaluation date: {EVAL_DATE}")
    lines.append("Models refit on data/raw/martj42_cleaned.csv (verified corrections overlay).")
    lines.append("Old prob/EV = value stored in DB at placement (pre-correction).")
    lines.append("Advancement bets use a COLD-START corrected sim: it isolates the")
    lines.append("data-correction effect on team strength, NOT a live in-tournament prob.")
    lines.append("")
    hdr = (f"{'ID':>3}  {'SELECTION':40.40}  {'ODDS':>6}  {'OLD_P':>7}  {'NEW_P':>7}  "
           f"{'dP':>7}  {'OLD_EV':>7}  {'NEW_EV':>7}  {'dEV':>7}  {'KELLY':>6}  REC")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in report:
        lines.append(
            f"{r['id']:>3}  {r['selection'][:40]:40.40}  {r['decimal_odds']:>6.3f}  "
            f"{_f(r['old_prob'], pct=True):>7}  {_f(r['new_prob'], pct=True):>7}  "
            f"{_f(r['prob_delta'], pct=True, signed=True):>7}  "
            f"{_f(r['old_ev'], pct=True):>7}  {_f(r['new_ev'], pct=True):>7}  "
            f"{_f(r['ev_delta'], pct=True, signed=True):>7}  "
            f"{_f(r['new_kelly']):>6}  {r['recommendation']}")
    lines.append("")
    lines.append("PER-BET NOTES")
    for r in report:
        lines.append(f"  [{r['id']}] {r['selection']}")
        lines.append(f"        basis: {r['basis']}")
        if r.get("flag"):
            lines.append(f"        FLAG:  {r['flag']}")
    lines.append("")
    lines.append("CORRECTED SIM — key teams (cold-start P(reach stage)):")
    for t in ("Japan", "Ghana", "Australia", "Turkey"):
        if t in adv.index:
            row = adv.loc[t]
            lines.append(f"  {t:10}  R32={row['P(R32)']:.3f}  R16={row['P(R16)']:.3f}  "
                         f"QF={row['P(QF)']:.3f}")
    Path("data/recompute_report.txt").write_text("\n".join(lines) + "\n")

    print("\n".join(lines))
    print("\nWrote: data/recompute_report.csv, data/recompute_summary.json, "
          "data/recompute_report.txt, data/elo_ratings_corrected.json, "
          "data/dc_params_corrected.json")


if __name__ == "__main__":
    raise SystemExit(main())
