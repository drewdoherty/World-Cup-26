#!/usr/bin/env python
"""Polymarket analytics suite — live runner for :mod:`wca.pmanalytics`.

Runs the three analytics against LIVE data and prints reports + saves charts:

  1. Model-vs-market calibration & edge  (advance / match_result / btts / exact)
  2. Term-structure consistency flags     (advancement ladder + advance-vs-FT)
  3. Open-position mark-to-market         (real ledger + paper test book)

Domain rule held throughout: FT 1X2 ("win in 90'") and advance ("reach Round of
N", incl. ET/pens) are DIFFERENT markets. FT probs come from scores_data.json
``model_1x2``; advance probs from advancement_data.json ``model``. The single
sanctioned cross-check is P(advance) >= P(win-in-90') for the same next match.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_pm_analytics_suite.py
    PYTHONPATH=src python3 scripts/wca_pm_analytics_suite.py --charts reports/pm_analytics
    PYTHONPATH=src python3 scripts/wca_pm_analytics_suite.py --offline   # no network (skips MTM marks)
    PYTHONPATH=src python3 scripts/wca_pm_analytics_suite.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pmanalytics as pa  # noqa: E402
from wca.testbook import store as tb_store  # noqa: E402
from wca.testbook import trader as tb_trader  # noqa: E402

_DEF_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
_DEF_ADV = os.path.join(_ROOT, "site", "advancement_data.json")
_DEF_REAL_DB = os.path.join(_ROOT, "data", "wca.db")
_DEF_PAPER_DB = os.path.join(_ROOT, "data", "test_book.db")

# trader candidate market_type -> calibration category.
_CAND_CAT = {
    "advance": "advance",
    "match_result": "match_result",
    "btts": "btts",
    "exact_score": "exact_score",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str) -> dict:
    with open(path, "r") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Live PM fetch                                                               #
# --------------------------------------------------------------------------- #


def fetch_pm_events(include_closed: bool = False):
    from wca.data.polymarket import find_world_cup_markets
    return find_world_cup_markets(include_closed=include_closed)


def latest_clob_price(token_id: str) -> Optional[float]:
    """Latest CLOB YES mid for a token (best-effort, returns None on failure)."""
    from wca.data.pm_clob_history import price_history
    if not token_id:
        return None
    hist = price_history(token_id, interval="1d", fidelity=60)
    if not hist:
        hist = price_history(token_id, interval="max", fidelity=60)
    return hist[-1][1] if hist else None


# --------------------------------------------------------------------------- #
# 1. Calibration                                                             #
# --------------------------------------------------------------------------- #


def build_calibration_observations(model, pm_events) -> List[Dict[str, object]]:
    """Join model probs to live PM YES prices across the four categories.

    Reuses :func:`trader.build_candidates` for the model<->PM join (it already
    resolves fixtures, advance reach-events, BTTS, exact-score), then maps each
    candidate's PM ask price + model prob into a calibration observation. The
    ``totals_ou25`` family is intentionally dropped (not one of the four
    priceable categories requested).
    """
    cands = tb_trader.build_candidates(model, pm_events, min_volume=0.0)
    obs: List[Dict[str, object]] = []
    for c in cands:
        cat = _CAND_CAT.get(c.market_type)
        if cat is None:
            continue
        # c.price is the PM YES ask; c.model_prob is the model YES prob.
        obs.append({
            "category": cat,
            "subject": c.fixture,
            "label": c.selection,
            "model_prob": c.model_prob,
            "pm_price": c.price,
            "token_id": c.token_id,
        })
    return obs


def print_calibration(summary: Dict[str, object], top_n: int = 15) -> None:
    print("\n" + "=" * 78)
    print("1. MODEL-vs-MARKET CALIBRATION & EDGE  (edge = model - PM YES price)")
    print("=" * 78)
    print("rows joined: %d  (live, non-pinned: %d)\n" %
          (summary["n_rows"], summary["n_rows_live"]))
    by_cat = summary["by_category_live"]
    if not by_cat:
        print("  (no live model<->PM joins — nothing to calibrate)")
        return
    print("  LIVE markets only (PM price not pinned at 0/1):")
    print("  %-14s %4s %9s %9s %9s %8s %10s" %
          ("category", "n", "mean_edge", "median", "mean|e|", "rmse", "model>PM"))
    print("  " + "-" * 70)
    for cat in sorted(by_cat, key=lambda k: -abs(by_cat[k]["mean_edge"])):
        s = by_cat[cat]
        print("  %-14s %4d %+9.3f %+9.3f %9.3f %8.3f %9.0f%%" % (
            cat, int(s["n"]), s["mean_edge"], s["median_edge"],
            s["mean_abs_edge"], s["rmse"], 100.0 * s["frac_model_high"]))
    print("\n  Interpretation: a large |mean_edge| with frac model>PM near 0%% or")
    print("  100%% is a SYSTEMATIC bias (model sits below / above PM for that family).")

    print("\n  Top absolute edges:")
    print("  %-12s %-34s %7s %7s %7s" %
          ("category", "outcome", "model", "PM", "edge"))
    print("  " + "-" * 72)
    for t in summary["top_edges"][:top_n]:
        label = ("%s | %s" % (t["subject"], t["label"]))[:34]
        print("  %-12s %-34s %7.3f %7.3f %+7.3f" %
              (t["category"], label, t["model_prob"], t["pm_price"], t["edge"]))


# --------------------------------------------------------------------------- #
# 2. Term structure                                                          #
# --------------------------------------------------------------------------- #


def _next_match_advance_vs_ft(model) -> List[Dict[str, object]]:
    """Build advance-vs-FT cross-checks for each live fixture (model side).

    For a live fixture (from scores_data) we approximate "advance past the next
    match" by the team's lowest still-meaningful reach probability among the
    advancement stages. We compare that against P(win in 90') from model_1x2.

    This is a *bounding* check, not an equality: winning in 90' is a subset of
    advancing, so P(advance) must be >= P(win in 90'). We use the team's
    nearest-stage reach prob as the advance proxy. If no clean stage maps, the
    pair is skipped (the bound only ever produces true violations, never false
    positives, because we use the MOST generous advance proxy available).
    """
    out: List[Dict[str, object]] = []
    from wca.data.teamnames import canonical
    advance = model["advance"]
    # advance proxy for the next match = the team's largest reach prob strictly
    # below 1.0 (the nearest uncertain hurdle); a 1.0 reach prob means
    # already-through and is not informative for the next game.
    for key, fx in model["fixtures"].items():
        for side, team in (("home", fx["home"]), ("away", fx["away"])):
            ft_win = (fx["model_1x2"] or {}).get(side)
            ladder = advance.get(canonical(team)) or {}
            # advance proxy = the largest reach prob strictly below 1.0 (the next
            # uncertain hurdle); fall back to win prob if all are 1.0.
            cands = [p for s, p in ladder.items()
                     if s in pa.LADDER_STAGES and isinstance(p, (int, float)) and p < 1.0]
            adv_proxy = max(cands) if cands else None
            if ft_win is None or adv_proxy is None:
                continue
            out.append({
                "team": team, "source": "model",
                "advance_prob": float(adv_proxy), "ft_win_prob": float(ft_win),
            })
    return out


def print_term_structure(report: Dict[str, object]) -> None:
    print("\n" + "=" * 78)
    print("2. TERM-STRUCTURE CONSISTENCY FLAGS")
    print("=" * 78)
    print("Rule A (ladder): P(R16) >= P(QF) >= P(SF) >= P(Final) >= P(win), per team.")
    print("Rule B (cross):  P(advance) >= P(win the next match in 90').\n")

    lv = report["ladder_violations"]
    print("Ladder violations: %d" % report["n_ladder_violations"])
    if lv:
        print("  %-22s %-6s %-18s %8s %8s %7s" %
              ("team", "src", "stages", "p_hi", "p_lo", "gap"))
        print("  " + "-" * 70)
        for v in sorted(lv, key=lambda x: -x["gap"]):
            print("  %-22s %-6s %-18s %8.3f %8.3f %+7.3f" % (
                v["team"][:22], v["source"],
                "%s>=%s" % (v["stage_hi"], v["stage_lo"]),
                v["prob_hi"], v["prob_lo"], v["gap"]))
    else:
        print("  (none — every team's reach ladder is monotone in both model & PM)")

    cf = report["advance_vs_ft_flags"]
    print("\nAdvance-vs-FT flags: %d" % report["n_advance_vs_ft_flags"])
    if cf:
        print("  %-22s %-6s %10s %10s %7s" %
              ("team", "src", "advance", "ft_win90", "gap"))
        print("  " + "-" * 60)
        for f in sorted(cf, key=lambda x: -x["gap"]):
            print("  %-22s %-6s %10.3f %10.3f %+7.3f" % (
                f["team"][:22], f["source"], f["advance_prob"],
                f["ft_win_prob"], f["gap"]))
    else:
        print("  (none — P(win in 90') never exceeds the advance proxy)")


# --------------------------------------------------------------------------- #
# 3. Mark-to-market                                                          #
# --------------------------------------------------------------------------- #


def _real_open_positions(db_path: str) -> List[Dict[str, object]]:
    """Open real-ledger bets as normalised position dicts.

    Marks only the Polymarket bets that carry a ``token_id`` (others have no PM
    price to mark against and are surfaced as unmarked). Resolution basis is
    inferred from the market text (advance vs FT vs outright vs prop).
    """
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, match_desc, market, selection, platform, decimal_odds, stake,"
            " token_id FROM bets WHERE status='open'").fetchall()
    finally:
        con.close()
    out: List[Dict[str, object]] = []
    for r in rows:
        market = (r["market"] or "")
        ml = market.lower()
        sel = (r["selection"] or "")
        if "reach" in ml or "eliminat" in ml or "advance" in ml or "round of" in ml:
            basis = "advance"
        elif "outright" in ml or "golden boot" in ml:
            basis = "outright"
        elif re.search(r"\bwin\b", ml) and " vs " in (r["match_desc"] or "").lower():
            basis = "FT"
        elif "builder" in ml or "acca" in ml or "treble" in ml or "sot" in ml:
            basis = "prop"
        else:
            basis = "other"
        out.append({
            "book": "real", "bet_id": r["id"], "fixture": r["match_desc"] or "",
            "market": market, "selection": sel, "resolution_basis": basis,
            "token_id": r["token_id"], "stake": float(r["stake"] or 0.0),
            "currency": "USD" if (r["platform"] or "") == "polymarket" else "GBP",
            "entry_price": None,
            "decimal_odds": float(r["decimal_odds"]) if r["decimal_odds"] else None,
        })
    return out


def _paper_open_positions(db_path: str) -> List[Dict[str, object]]:
    """Open test-book paper bets as normalised position dicts (all USD, YES shares)."""
    if not os.path.exists(db_path):
        return []
    con = tb_store.connect(db_path)
    try:
        bets = tb_store.open_bets(con)
    finally:
        con.close()
    out: List[Dict[str, object]] = []
    for b in bets:
        out.append({
            "book": "paper", "bet_id": b["id"], "fixture": b.get("fixture") or "",
            "market": b.get("market_type") or "", "selection": b.get("selection") or "",
            "resolution_basis": b.get("resolution_basis") or "other",
            "token_id": b.get("token_id"),
            "stake": float(b.get("stake_usd") or 0.0), "currency": "USD",
            "entry_price": float(b["entry_price"]) if b.get("entry_price") is not None else None,
            "decimal_odds": None,
        })
    return out


def _quote_index(pm_events) -> Dict[str, float]:
    """token_id -> live YES mid from the Gamma snapshot (cheap fallback mark)."""
    idx: Dict[str, float] = {}
    for ev in pm_events or []:
        for m in ev.get("markets") or []:
            q = tb_trader.yes_quote(m)
            if q and q.get("mid") and 0.0 < q["mid"] < 1.0:
                idx[q["token"]] = float(q["mid"])
    return idx


def mark_all(positions, pm_events, *, use_clob: bool, paper_record_db: Optional[str] = None):
    """Mark each position to a live PM price; CLOB latest first, Gamma mid fallback."""
    gamma = _quote_index(pm_events) if pm_events else {}
    clob_cache: Dict[str, Optional[float]] = {}
    marked: List[pa.MarkedPosition] = []
    paper_con = None
    if paper_record_db and os.path.exists(paper_record_db):
        paper_con = tb_store.connect(paper_record_db)
    ts = _now()
    try:
        for pos in positions:
            tok = pos.get("token_id")
            mark = None
            if tok:
                if use_clob:
                    if tok not in clob_cache:
                        clob_cache[tok] = latest_clob_price(str(tok))
                    mark = clob_cache[tok]
                if mark is None:
                    mark = gamma.get(str(tok))
            mp = pa.mark_position(pos, mark)
            marked.append(mp)
            # Persist the mark for paper positions so the equity curve stays fresh.
            if (paper_con is not None and pos.get("book") == "paper"
                    and mark is not None and pos.get("entry_price")):
                try:
                    tb_store.record_mark(paper_con, int(pos["bet_id"]), float(mark), ts)
                except Exception:
                    pass
    finally:
        if paper_con is not None:
            paper_con.close()
    return marked


def print_mtm(marked: Sequence[pa.MarkedPosition], totals: Dict[str, object]) -> None:
    print("\n" + "=" * 78)
    print("3. OPEN-POSITION MARK-TO-MARKET  (real ledger + paper test book)")
    print("=" * 78)
    print("  %-5s %-5s %-26s %-9s %8s %7s %7s %9s" %
          ("book", "id", "fixture / selection", "basis", "stake", "entry", "mark", "unreal"))
    print("  " + "-" * 92)
    for m in sorted(marked, key=lambda x: (x.book, -(x.unrealized_pl or -9e9))):
        name = ("%s | %s" % (m.fixture, m.selection))[:26]
        entry = ("%.3f" % m.entry_price) if m.entry_price is not None else (
            "1/%.2f" % m.decimal_odds if m.decimal_odds else "n/a")
        mark = ("%.3f" % m.mark_price) if m.mark_price is not None else " — "
        unreal = ("%+.2f" % m.unrealized_pl) if m.unrealized_pl is not None else "  —  "
        cur = "$" if m.currency == "USD" else ("£" if m.currency == "GBP" else "")
        print("  %-5s %-5s %-26s %-9s %s%6.2f %7s %7s %9s" % (
            m.book, str(m.bet_id), name, m.resolution_basis, cur, m.stake,
            entry, mark, unreal))

    print("\n  Totals by book (within each currency; £ and $ never netted):")
    for book, cmap in sorted(totals["by_book"].items()):
        for cur, agg in sorted(cmap.items()):
            sym = "$" if cur == "USD" else ("£" if cur == "GBP" else cur + " ")
            print("    %-5s %-3s  n=%d (marked %d, unmarked %d)  staked=%s%.2f  unreal=%s%+.2f  ROI=%+.1f%%" % (
                book, cur, int(agg["n"]), int(agg["n_marked"]), int(agg["n_unmarked"]),
                sym, agg["stake_marked"], sym, agg["unrealized_pl"], agg.get("roi_pct", 0.0)))

    print("\n  Totals by resolution basis:")
    for basis, cmap in sorted(totals["by_basis"].items()):
        for cur, agg in sorted(cmap.items()):
            sym = "$" if cur == "USD" else ("£" if cur == "GBP" else cur + " ")
            print("    %-9s %-3s n=%d marked=%d  unreal=%s%+.2f" % (
                basis, cur, int(agg["n"]), int(agg["n_marked"]), sym, agg["unrealized_pl"]))


# --------------------------------------------------------------------------- #
# Charts                                                                     #
# --------------------------------------------------------------------------- #


def save_charts(summary: Dict[str, object], rows, prefix: str) -> List[str]:
    """Scatter (model vs PM by category) + per-category mean-edge bar. Best-effort."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - matplotlib optional
        print("  [charts skipped: matplotlib unavailable: %s]" % exc)
        return []

    os.makedirs(os.path.dirname(os.path.abspath(prefix)) or ".", exist_ok=True)
    saved: List[str] = []
    # Chart the LIVE (non-pinned) markets only so dead-market noise doesn't skew.
    rows = pa.filter_live(rows)
    by_cat = summary["by_category_live"]
    cats = sorted(by_cat)
    cmap = {c: col for c, col in zip(
        cats, ["#6D4AD0", "#E0567A", "#2E9E7B", "#E0A21F", "#3A78C2", "#888"])}

    # Scatter: model vs PM, coloured by category, with y=x.
    if rows:
        fig, ax = plt.subplots(figsize=(7.5, 7))
        for c in cats:
            xs = [r.pm_price for r in rows if r.category == c]
            ys = [r.model_prob for r in rows if r.category == c]
            ax.scatter(xs, ys, s=28, alpha=0.7, label=c, color=cmap.get(c, "#888"),
                       edgecolors="white", linewidths=0.4)
        ax.plot([0, 1], [0, 1], "--", color="#555", lw=1, label="y=x (fair)")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.set_xlabel("Polymarket YES price (implied prob)")
        ax.set_ylabel("Model probability")
        ax.set_title("Model vs Polymarket by category\n(above line = model dearer than PM)")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.25)
        p1 = prefix + "_calibration_scatter.png"
        fig.tight_layout(); fig.savefig(p1, dpi=130); plt.close(fig)
        saved.append(p1)

    # Bar: per-category mean edge.
    if by_cat:
        fig, ax = plt.subplots(figsize=(7.5, 4.5))
        labels = cats
        vals = [by_cat[c]["mean_edge"] for c in labels]
        ns = [int(by_cat[c]["n"]) for c in labels]
        colors = ["#2E9E7B" if v >= 0 else "#E0567A" for v in vals]
        bars = ax.bar(labels, vals, color=colors, edgecolor="#333", linewidth=0.5)
        ax.axhline(0, color="#333", lw=1)
        for b, v, n in zip(bars, vals, ns):
            ax.text(b.get_x() + b.get_width() / 2, v + (0.003 if v >= 0 else -0.003),
                    "%+.3f\n(n=%d)" % (v, n), ha="center",
                    va="bottom" if v >= 0 else "top", fontsize=8)
        ax.set_ylabel("mean edge (model - PM)")
        ax.set_title("Per-category systematic bias  (>0 = model above PM)")
        ax.grid(axis="y", alpha=0.25)
        if vals:
            lo_y, hi_y = min(0.0, min(vals)), max(0.0, max(vals))
            pad = 0.12 * (hi_y - lo_y or 1.0)
            ax.set_ylim(lo_y - pad, hi_y + pad)
        p2 = prefix + "_category_bias.png"
        fig.tight_layout(); fig.savefig(p2, dpi=130); plt.close(fig)
        saved.append(p2)

    return saved


# --------------------------------------------------------------------------- #
# main                                                                       #
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scores", default=_DEF_SCORES)
    ap.add_argument("--advancement", default=_DEF_ADV)
    ap.add_argument("--real-db", default=_DEF_REAL_DB)
    ap.add_argument("--paper-db", default=_DEF_PAPER_DB)
    ap.add_argument("--charts", default=os.path.join(_ROOT, "reports", "pm_analytics"),
                    help="path prefix for the PNG charts (set empty to skip)")
    ap.add_argument("--offline", action="store_true",
                    help="no network: skip PM event fetch + CLOB marks")
    ap.add_argument("--no-clob", action="store_true",
                    help="use only the Gamma snapshot mid for marks (no CLOB latest)")
    ap.add_argument("--record-marks", action="store_true",
                    help="persist paper-position marks into the test book (equity curve)")
    ap.add_argument("--json", default=None, help="also dump the full report as JSON here")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    scores = _load_json(args.scores)
    adv = _load_json(args.advancement)
    model = tb_trader.load_model(scores, adv)

    pm_events = []
    if not args.offline:
        print("Fetching live Polymarket World Cup events ...")
        try:
            pm_events = fetch_pm_events(include_closed=False)
            print("  got %d events" % len(pm_events))
        except Exception as exc:
            print("  [PM fetch failed: %s — continuing offline]" % exc)
            pm_events = []

    # 1) calibration
    obs = build_calibration_observations(model, pm_events) if pm_events else []
    rows = pa.build_edge_rows(obs)
    cal = pa.calibration_summary(rows)
    print_calibration(cal, top_n=args.top)

    # 2) term structure
    cross = _next_match_advance_vs_ft(model)
    ts_report = pa.term_structure_report(adv.get("teams", []), cross)
    print_term_structure(ts_report)

    # 3) mark-to-market
    positions = _real_open_positions(args.real_db) + _paper_open_positions(args.paper_db)
    use_clob = (not args.offline) and (not args.no_clob)
    rec_db = args.paper_db if args.record_marks else None
    marked = mark_all(positions, pm_events, use_clob=use_clob, paper_record_db=rec_db)
    totals = pa.mtm_totals(marked)
    print_mtm(marked, totals)

    # charts
    saved: List[str] = []
    if args.charts:
        saved = save_charts(cal, rows, args.charts)
        if saved:
            print("\nCharts saved:")
            for p in saved:
                print("  " + p)

    if args.json:
        payload = {
            "generated_utc": _now(),
            "calibration": cal,
            "term_structure": ts_report,
            "mark_to_market": {
                "totals": totals,
                "positions": [m.as_dict() for m in marked],
            },
            "charts": saved,
        }
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print("\nJSON written: %s" % args.json)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
