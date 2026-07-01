"""Settlement for the paper test book — FT-result vs advance aware.

The grading basis is whatever the bet recorded in ``resolution_basis``:

* ``FT``       — graded on the 90'+stoppage match result. A knockout level at 90'
  is a DRAW here, so an "X win (FT)" paper bet LOSES even if X later advances.
* ``advance``  — graded on actual tournament progression (reaching the stage),
  which INCLUDES winning in extra-time / on penalties. Needs a ``reached``
  mapping (team -> deepest stage reached); without it, advance bets stay open.
* ``exact`` / ``btts`` / ``totals`` — graded on the final score.

This is the concrete place the win-vs-advance distinction must not be fudged.

Caveat: the martj42 results feed stores one score per fixture; for knockouts that
went to extra-time it may be the post-ET score, not strictly the 90' line. FT
grading is therefore best-effort until a 90'-specific source is wired.
"""

from __future__ import annotations

import csv
import re
from typing import Dict, Optional, Tuple

from wca.data.teamnames import canonical
from wca.testbook import store

_STAGE_ORDER = {"R32": 0, "R16": 1, "QF": 2, "SF": 3, "Final": 4, "win": 5}


def load_wc_results(path: str, *, season_prefix: str = "2026") -> Dict[frozenset, Tuple[str, str, int, int]]:
    """``{frozenset({home,away}): (date, canon_home, home_score, away_score)}`` for WC rows."""
    out: Dict[frozenset, Tuple[str, str, int, int]] = {}
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if "world cup" not in (r.get("tournament") or "").lower():
                    continue
                d = r.get("date") or ""
                if not d.startswith(season_prefix):
                    continue
                try:
                    hs, as_ = int(r["home_score"]), int(r["away_score"])
                except (KeyError, ValueError, TypeError):
                    continue
                h, a = canonical(r.get("home_team") or ""), canonical(r.get("away_team") or "")
                if h and a:
                    out[frozenset({h, a})] = (d, h, hs, as_)
    except FileNotFoundError:
        pass
    return out


def _stage_ge(reached: str, target: str) -> bool:
    return _STAGE_ORDER.get(reached, -1) >= _STAGE_ORDER.get(target, 99)


def grade(bet: Dict[str, object], results: Dict[frozenset, Tuple[str, str, int, int]],
          reached: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Return 'won'/'lost'/'void' for a bet, or None if not yet resolvable."""
    basis = str(bet.get("resolution_basis") or "")
    sel = str(bet.get("selection") or "")
    fixture = str(bet.get("fixture") or "")

    # ADVANCE — progression (incl. ET/pens); needs the reached mapping.
    if basis == "advance":
        if reached is None:
            return None
        m = re.search(r"(.+?)\s+to reach\s+(\w+)", sel)
        if not m:
            return None
        team, stage = canonical(m.group(1)), m.group(2)
        got = reached.get(team)
        if got is None:
            return None
        return "won" if _stage_ge(got, stage) else "lost"

    # Score-based families need the fixture result.
    parts = [p.strip() for p in fixture.split(" vs ")]
    if len(parts) != 2:
        return None
    res = results.get(frozenset({canonical(parts[0]), canonical(parts[1])}))
    if res is None:
        return None
    _date, res_home, hs, as_ = res
    bet_home = canonical(parts[0])
    # Orient the score to the bet's "home vs away" convention.
    h, a = (hs, as_) if res_home == bet_home else (as_, hs)

    if basis == "FT":
        if "draw" in sel.lower():
            return "won" if h == a else "lost"
        m = re.search(r"(.+?)\s+win", sel)
        team = canonical(m.group(1)) if m else None
        if team == bet_home:
            return "won" if h > a else "lost"
        return "won" if a > h else "lost"
    if basis == "btts":
        return "won" if (h > 0 and a > 0) else "lost"
    if basis == "totals":
        total = h + a
        return "won" if ((total > 2.5) == ("over" in sel.lower())) else "lost"
    if basis == "exact":
        m = re.search(r"(\d+)\s*[-–]\s*(\d+)", sel)
        if not m:
            return None
        return "won" if (int(m.group(1)), int(m.group(2))) == (h, a) else "lost"
    if basis == "handicap":
        # "<Team> -1.5": team wins by 2+.
        m = re.search(r"(.+?)\s*-1\.5", sel)
        team = canonical(m.group(1)) if m else None
        margin = (h - a) if team == bet_home else (a - h)
        return "won" if margin >= 2 else "lost"
    return None


def settle_open(con, results: Dict[frozenset, Tuple[str, str, int, int]],
                reached: Optional[Dict[str, str]] = None, *, ts_utc: str) -> Dict[str, object]:
    """Grade & settle every resolvable open paper bet. Returns a summary."""
    settled = {"won": 0, "lost": 0, "void": 0}
    pl = 0.0
    unresolved = 0
    for b in store.open_bets(con):
        outcome = grade(b, results, reached)
        if outcome is None:
            unresolved += 1
            continue
        pl += store.settle(con, b["id"], outcome=outcome, ts_utc=ts_utc)
        settled[outcome] += 1
    backfilled = backfill_decision_outcomes(con, results, reached, ts_utc=ts_utc)
    return {"settled": settled, "pl": round(pl, 2), "unresolved": unresolved,
            "decisions_backfilled": backfilled}


# --------------------------------------------------------------------------- decision evaluation


def backfill_decision_outcomes(con, results: Dict[frozenset, Tuple[str, str, int, int]],
                               reached: Optional[Dict[str, str]] = None, *, ts_utc: str) -> int:
    """Fill the quarantined outcome columns on decision rows once the market resolved.

    Per decision, the realised regret and model-EV regret are the MARGINAL effect
    of that decision's shares vs settlement (v ∈ {0,1}):
      add  → realised = shares·(v − p_t) ; model_ev = shares·(q_t − p_t)
      exit → realised = shares·(p_t − v) ; model_ev = shares·(p_t − q_t)
    so (realised − model_ev) = ±shares·(v − q_t) — the model calibration error.
    Outcomes touch ONLY these aggregate-bound columns, never the process score (INV-5).
    """
    rows = con.execute(
        "SELECT d.id, d.action, d.shares_delta, d.p_t, d.q_t, d.resolution_basis,"
        " b.selection, b.fixture FROM decision_events d JOIN paper_bets b ON b.id=d.paper_bet_id"
        " WHERE d.settled_outcome IS NULL").fetchall()
    n = 0
    for r in rows:
        bet = {"resolution_basis": r["resolution_basis"], "selection": r["selection"], "fixture": r["fixture"]}
        outcome = grade(bet, results, reached)
        if outcome is None:
            continue
        if outcome == "void":
            con.execute("UPDATE decision_events SET settled_outcome='void', settled_ts=? WHERE id=?",
                        (ts_utc, r["id"]))
            n += 1
            continue
        v = 1.0 if outcome == "won" else 0.0
        sh, p, q = float(r["shares_delta"]), float(r["p_t"]), float(r["q_t"])
        if r["action"] == "add":
            realised, model_ev = sh * (v - p), sh * (q - p)
        else:
            realised, model_ev = sh * (p - v), sh * (p - q)
        con.execute(
            "UPDATE decision_events SET settled_outcome=?, settled_ts=?, settled_pl=?,"
            " realized_regret=?, delta_ev=? WHERE id=?",
            (outcome, ts_utc, realised, realised, model_ev, r["id"]))
        n += 1
    con.commit()
    return n


def process_rollup(con) -> Dict[str, Dict[str, Dict[str, object]]]:
    """LEADING dashboard: per (resolution_basis × q_source), the decision-time-only
    process scores (mean GOG, mean Δg, mean exit-spread cost, cap-binding rate)."""
    out: Dict[str, Dict[str, Dict[str, object]]] = {}
    for r in con.execute(
        "SELECT resolution_basis, q_source, COUNT(*) n, AVG(gog) gog, AVG(delta_g) dg,"
        " AVG(exit_spread_cost) esc, AVG(cap_binding) capb FROM decision_events"
        " GROUP BY resolution_basis, q_source"):
        out.setdefault(r["resolution_basis"], {})[r["q_source"] or "?"] = {
            "n": r["n"], "mean_gog": r["gog"], "mean_delta_g": r["dg"],
            "mean_exit_spread_cost": r["esc"], "cap_binding_rate": r["capb"]}
    return out


def calibration_rollup(con, *, min_n: int = 20) -> Dict[str, object]:
    """LAGGING validator (quarantined): per-basis ev_calibration_gap from settled ADD
    decisions = stake-weighted (v − q) (>0 ⇒ model under-predicts), plus the policy
    check that exits beat holding (Σ realised regret over trims/closes)."""
    by_basis: Dict[str, Dict[str, object]] = {}
    for r in con.execute(
        "SELECT resolution_basis, SUM(realized_regret - delta_ev) g, SUM(shares_delta) sh, COUNT(*) n"
        " FROM decision_events WHERE action='add' AND settled_outcome IN ('won','lost')"
        " GROUP BY resolution_basis"):
        sh = float(r["sh"] or 0.0)
        by_basis[r["resolution_basis"]] = {
            "ev_calibration_gap": (float(r["g"]) / sh) if sh else None,
            "n": int(r["n"]), "collecting": (int(r["n"] or 0) < min_n)}
    ev = con.execute(
        "SELECT COALESCE(SUM(realized_regret),0), COUNT(*) FROM decision_events"
        " WHERE action IN ('trim','close') AND settled_outcome IN ('won','lost')").fetchone()
    return {"by_basis": by_basis, "exit_value_vs_hold": float(ev[0]), "n_exits": int(ev[1]),
            "min_n": min_n}
