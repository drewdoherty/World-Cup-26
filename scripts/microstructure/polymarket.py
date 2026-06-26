#!/usr/bin/env python3
"""Polymarket & combo-market decomposition (FRAMEWORK + live advancement data).

Run with::

    PYTHONPATH=src .venv/bin/python scripts/microstructure/polymarket.py

What this is (read the caveats)
-------------------------------
There are **no historical Polymarket price snapshots** anywhere in
``data/wca.db`` — the only PM rows are ``pm_parked`` (10 parked proposals) and
``pm_order_log`` (6 dry-run order lines), both operational, neither a price
time-series. So this module is deliberately a **method specification** for
decomposing PM sports-combo markets, made concrete with the one genuinely
data-backed live PM artefact the project keeps: ``site/advancement_data.json``.

That file is a single live snapshot (regenerated ~daily by
``scripts/wca_advancement.py`` / :mod:`wca.advancement`) of per-team Polymarket
**stage-advancement** prices (Reach R16 / QF / SF / Final / Win the World Cup)
alongside the project's Monte-Carlo model probabilities for the same events.

The decomposition method (combo markets as nested conditionals)
---------------------------------------------------------------
A tournament-advancement ladder is the cleanest real example of a *combo*
market: "reach the Semifinals" is mechanically the product of single-tie wins
``P(R32) * P(R16|R32) * P(QF|R16) * P(SF|QF)``. Polymarket prices each *rung*
(R16, QF, SF, Final, Win) as an independent binary. That lets us:

  1. **Back out PM's implied per-tie (conditional) probabilities** by dividing
     adjacent rung prices: ``pm_cond[stage] = pm[stage] / pm[prev_stage]``. This
     is the "decompose the bundle into its components" step.
  2. **Compare to the model's JOINT distribution.** The Monte-Carlo simulator
     produces a coherent joint (every rung is the marginal of one simulated
     tournament), so its conditionals are ``model_cond[stage] =
     model[stage] / model[prev_stage]`` and they multiply back exactly.
  3. **Diagnose the two failure modes the brief names:**
       * *independence / coherence* — does PM's own rung ladder multiply out
         consistently (monotone-decreasing marginals, conditionals in (0,1])?
         A coherent ladder means PM is internally arbitrage-free; an incoherent
         one is a structural mispricing you can lock.
       * *over-discounting* — even when coherent, does PM apply a **deep-run
         discount** the model disagrees with? We attribute the total title
         disagreement ``log(model_win / pm_win)`` additively across rungs
         (``log(model_cond/pm_cond)`` per step) to see **which single tie**
         drives the gap. A large negative-then-large total is the classic
         longshot/favourite over-discount.

This is NOT yet a profitability claim. We have one cross-section, no PM price
history, no settled combo P&L. Every number below is a *current-snapshot
disagreement*, explicitly framework/indicative, to be confirmed by the
walk-forward test described in the JSON ``validation`` block.

Read-only: this script never writes to the database.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(REPO, "data", "wca.db")
ADV_PATH = os.path.join(REPO, "site", "advancement_data.json")
OUT_PATH = os.path.join(REPO, "site", "microstructure", "polymarket.json")

# Advancement ladder, easiest (most likely) rung first. R32 is the group-stage
# survival rung; the four knockout ties are R16..Final and the title is "win".
CHAIN: Tuple[str, ...] = ("R32", "R16", "QF", "SF", "Final", "win")
# Human label for the single tie that *advances you INTO* each rung.
TIE_LABEL: Dict[str, str] = {
    "R16": "R32 tie (win to reach R16)",
    "QF": "R16 tie (win to reach QF)",
    "SF": "QF tie (win to reach SF)",
    "Final": "SF tie (win to reach Final)",
    "win": "the Final itself",
}


# ---------------------------------------------------------------------------
# DB facts (read-only) — confirm the data reality the analysis rests on.
# ---------------------------------------------------------------------------


def db_pm_inventory(db_path: str = DB_PATH) -> Dict[str, Any]:
    """Confirm there is NO PM price history; report the operational PM rows.

    Pure SELECTs against a read-only connection — never mutates the DB.
    """
    out: Dict[str, Any] = {
        "pm_price_snapshots": 0,
        "pm_parked_rows": 0,
        "pm_order_log_rows": 0,
        "pm_order_log_live_fills": 0,
        "odds_snapshots_sources": [],
    }
    uri = "file:%s?mode=ro" % db_path
    con = sqlite3.connect(uri, uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT DISTINCT source FROM odds_snapshots")
        out["odds_snapshots_sources"] = sorted(r[0] for r in cur.fetchall())
        # Is Polymarket anywhere in the price table? (it is not, but assert it.)
        cur.execute(
            "SELECT count(*) FROM odds_snapshots "
            "WHERE lower(source) LIKE '%poly%' OR lower(source) LIKE '%pm%'"
        )
        out["pm_price_snapshots"] = int(cur.fetchone()[0])
        for tbl, key in (("pm_parked", "pm_parked_rows"), ("pm_order_log", "pm_order_log_rows")):
            try:
                cur.execute("SELECT count(*) FROM %s" % tbl)
                out[key] = int(cur.fetchone()[0])
            except sqlite3.OperationalError:
                out[key] = 0
        try:
            cur.execute("SELECT count(*) FROM pm_order_log WHERE dry_run=0")
            out["pm_order_log_live_fills"] = int(cur.fetchone()[0])
        except sqlite3.OperationalError:
            out["pm_order_log_live_fills"] = 0
    finally:
        con.close()
    return out


# ---------------------------------------------------------------------------
# Decomposition core.
# ---------------------------------------------------------------------------


def _pm_chain(team: Dict[str, Any]) -> Dict[str, float]:
    """Per-team PM marginal price for each rung that has a usable price."""
    pm = team.get("pm") or {}
    out: Dict[str, float] = {}
    for s in CHAIN:
        node = pm.get(s)
        if isinstance(node, dict) and node.get("pm") is not None:
            try:
                v = float(node["pm"])
            except (TypeError, ValueError):
                continue
            if 0.0 < v <= 1.0001:
                out[s] = min(v, 1.0)
    return out


def _model_chain(team: Dict[str, Any]) -> Dict[str, float]:
    model = team.get("model") or {}
    out: Dict[str, float] = {}
    for s in CHAIN:
        v = model.get(s)
        if isinstance(v, (int, float)):
            out[s] = float(v)
    return out


def conditional_ladder(
    marg: Dict[str, float]
) -> Dict[str, Optional[float]]:
    """Back out per-tie conditional probs from a ladder of marginal probs.

    ``cond[stage] = marg[stage] / marg[prev_stage]`` for the rung you advance
    INTO; the first rung's conditional is its own marginal. Returns ``None``
    where a base price is missing so callers can skip cleanly.
    """
    out: Dict[str, Optional[float]] = {}
    for i, s in enumerate(CHAIN):
        if i == 0:
            out[s] = marg.get(s)
            continue
        prev = marg.get(CHAIN[i - 1])
        cur = marg.get(s)
        if prev is not None and cur is not None and prev > 1e-9:
            out[s] = cur / prev
        else:
            out[s] = None
    return out


def coherence_check(pm_marg: Dict[str, float]) -> Tuple[int, int, int]:
    """Internal arbitrage check on PM's own rung ladder.

    Returns ``(monotonicity_violations, conditional_out_of_range, pairs_checked)``.
    A monotonicity violation is a deeper rung priced ABOVE a shallower one
    (P(reach SF) > P(reach QF)) — a structural, lockable mispricing. A
    conditional out of range is a backed-out single-tie prob >1 (same thing,
    seen in conditional space).
    """
    seq = [(s, pm_marg[s]) for s in CHAIN if s in pm_marg]
    viol = 0
    oor = 0
    pairs = 0
    for (sa, a), (sb, b) in zip(seq, seq[1:]):
        pairs += 1
        if b > a + 1e-9:
            viol += 1
        if a > 1e-9 and (b / a) > 1.0 + 1e-9:
            oor += 1
    return viol, oor, pairs


def log_attribution(
    model_marg: Dict[str, float], pm_marg: Dict[str, float]
) -> Tuple[Optional[float], Dict[str, float], Optional[str]]:
    """Attribute the title disagreement across rungs in log space.

    ``log(model_win / pm_win) ~= sum_step log(model_cond / pm_cond)`` over the
    rungs both ladders price. Returns ``(total_log, per_step_contrib,
    dominant_step)``. The dominant step is the single tie that contributes most
    (in absolute value) to the model-vs-market title gap — the "where is the
    combo mispriced" answer.
    """
    contrib: Dict[str, float] = {}
    for i in range(1, len(CHAIN)):
        s = CHAIN[i]
        p = CHAIN[i - 1]
        mp = model_marg.get(p)
        pp = pm_marg.get(p)
        ms = model_marg.get(s)
        ps = pm_marg.get(s)
        if mp and pp and ms is not None and ps is not None and mp > 1e-9 and pp > 1e-9:
            mc = ms / mp
            pc = ps / pp
            if mc > 1e-9 and pc > 1e-9:
                contrib[s] = math.log(mc / pc)
    mw = model_marg.get("win")
    pw = pm_marg.get("win")
    total = math.log(mw / pw) if (mw and pw and mw > 1e-9 and pw > 1e-9) else None
    dom = max(contrib, key=lambda k: abs(contrib[k])) if contrib else None
    return total, contrib, dom


# ---------------------------------------------------------------------------
# Build the feed.
# ---------------------------------------------------------------------------


def build() -> Dict[str, Any]:
    with open(ADV_PATH, "r", encoding="utf-8") as fh:
        adv = json.load(fh)
    teams: List[Dict[str, Any]] = adv.get("teams") or []
    meta = adv.get("meta") or {}

    db = db_pm_inventory()

    # Per-step conditional disagreement (model_cond - pm_cond), PM-base only.
    step_edges: Dict[str, List[float]] = {s: [] for s in CHAIN[1:]}
    # Title longshot/favourite over-discount buckets.
    win_rows: List[Tuple[str, float, float]] = []  # (team, pm_win, model_win)
    # Per-team log attribution for the chart + dominant-tie tally.
    attributions: List[Dict[str, Any]] = []
    dominant_tally: Dict[str, int] = {s: 0 for s in CHAIN[1:]}

    coh_viol = coh_oor = coh_pairs = 0
    n_full_chain = 0

    for t in teams:
        pm_marg = _pm_chain(t)
        model_marg = _model_chain(t)
        if not pm_marg:
            continue

        v, o, p = coherence_check(pm_marg)
        coh_viol += v
        coh_oor += o
        coh_pairs += p

        pm_cond = conditional_ladder(pm_marg)
        model_cond = conditional_ladder(model_marg)
        for s in CHAIN[1:]:
            mc = model_cond.get(s)
            pc = pm_cond.get(s)
            # only count a step when PM actually priced BOTH base and rung
            if (
                mc is not None
                and pc is not None
                and CHAIN[CHAIN.index(s) - 1] in pm_marg
                and s in pm_marg
                and 0.0 < pc <= 1.0001
            ):
                step_edges[s].append(mc - pc)

        if "win" in pm_marg and "win" in model_marg:
            win_rows.append((t["team"], pm_marg["win"], model_marg["win"]))

        if all(s in pm_marg for s in ("R16", "QF", "SF", "Final", "win")):
            n_full_chain += 1
            total, contrib, dom = log_attribution(model_marg, pm_marg)
            if total is not None and dom is not None:
                dominant_tally[dom] = dominant_tally.get(dom, 0) + 1
                attributions.append(
                    {
                        "team": t["team"],
                        "group": t.get("group"),
                        "model_win": round(model_marg["win"], 4),
                        "pm_win": round(pm_marg["win"], 4),
                        "log_title_gap": round(total, 4),
                        "dominant_tie": dom,
                        "dominant_tie_label": TIE_LABEL.get(dom, dom),
                        "contrib": {k: round(val, 4) for k, val in contrib.items()},
                    }
                )

    # ---- aggregate stats ----
    def _agg(vals: List[float]) -> Dict[str, Any]:
        if not vals:
            return {"n": 0, "mean": None, "median": None}
        return {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 4),
            "median": round(statistics.median(vals), 4),
            "stdev": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
        }

    step_summary = []
    for s in CHAIN[1:]:
        a = _agg(step_edges[s])
        a["tie_into"] = s
        a["tie_label"] = TIE_LABEL.get(s, s)
        step_summary.append(a)

    # Longshot/favourite over-discount: mean (model_win - pm_win) by PM price bucket.
    def _bucket(lo: float, hi: float) -> Dict[str, Any]:
        b = [r for r in win_rows if lo <= r[1] < hi]
        if not b:
            return {"n": 0, "mean_model_minus_pm": None, "lo": lo, "hi": hi}
        return {
            "n": len(b),
            "mean_model_minus_pm": round(statistics.mean(r[2] - r[1] for r in b), 4),
            "lo": lo,
            "hi": hi,
        }

    longshot_buckets = {
        "longshot_lt_0.05": _bucket(0.0, 0.05),
        "mid_0.05_0.12": _bucket(0.05, 0.12),
        "fav_ge_0.12": _bucket(0.12, 1.01),
    }

    # Sort the title-attribution rows by absolute gap for the chart/ranking.
    attributions.sort(key=lambda r: abs(r["log_title_gap"]), reverse=True)
    # Contenders only: log-ratios explode on near-zero minnow priors, so the
    # economically meaningful attribution view keeps teams PM prices >=2% to win.
    contenders = [a for a in attributions if a["pm_win"] >= 0.02]
    contenders.sort(key=lambda r: abs(r["log_title_gap"]), reverse=True)

    # Headline scalars.
    win_step = next((a for a in step_summary if a["tie_into"] == "win"), {})
    final_step = next((a for a in step_summary if a["tie_into"] == "Final"), {})

    now = datetime.now(timezone.utc).isoformat()

    feed: Dict[str, Any] = {
        "key": "polymarket",
        "title": "Polymarket & Combo Markets: decomposing advancement bundles",
        "updated_at": now,
        "status": "framework-plus-live-snapshot",
        "window": {
            "pm_snapshot_generated": meta.get("generated"),
            "model_generated": meta.get("model_generated"),
            "n_pm_markets_in_snapshot": meta.get("n_pm_markets"),
            "n_teams": len(teams),
            "n_teams_with_pm_chain": sum(1 for t in teams if _pm_chain(t)),
            "n_teams_full_chain": n_full_chain,
        },
        "data_caveat": (
            "FRAMEWORK + a single live snapshot. There are NO historical "
            "Polymarket price snapshots in data/wca.db (confirmed: %d PM rows in "
            "the price table; only %d pm_parked + %d pm_order_log operational "
            "rows, of which %d are live fills). All numbers below are "
            "model-vs-market DISAGREEMENTS on one cross-section of "
            "site/advancement_data.json, not realised edges. No combo P&L, no "
            "out-of-sample test yet — treat as indicative/framework only."
        )
        % (
            db["pm_price_snapshots"],
            db["pm_parked_rows"],
            db["pm_order_log_rows"],
            db["pm_order_log_live_fills"],
        ),
        "method": {
            "decomposition": (
                "Advancement ladder = nested conditional combo. Back out PM's "
                "implied per-tie probability by dividing adjacent rung prices "
                "(pm_cond[stage]=pm[stage]/pm[prev]); compare to the model's "
                "coherent joint conditionals (model_cond=model[stage]/model[prev])."
            ),
            "coherence_test": (
                "PM rung ladder must be monotone-decreasing and every backed-out "
                "conditional in (0,1]; violations are internal arbitrage."
            ),
            "attribution": (
                "log(model_win/pm_win) = sum over ties of log(model_cond/pm_cond); "
                "the dominant tie is where the combo is most mispriced vs the model."
            ),
            "fees": "PM sports taker fee = 0.03*p*(1-p) per share (see wca.advancement.pm_taker_fee); not yet folded into these raw disagreements.",
        },
        "db_pm_inventory": db,
        "headline": {
            "pm_chain_coherence": {
                "monotonicity_violations": coh_viol,
                "conditional_out_of_range": coh_oor,
                "adjacent_pairs_checked": coh_pairs,
                "coherent_pct": round(100.0 * (1 - coh_viol / coh_pairs), 1)
                if coh_pairs
                else None,
            },
            "final_tie_conditional_edge": final_step,
            "win_tie_conditional_edge": win_step,
            "title_overdiscount_buckets": longshot_buckets,
            "n_full_chain_teams": n_full_chain,
        },
        "series": {
            "conditional_edge_by_tie": step_summary,
            "title_attribution_ranked": attributions[:20],
            "title_attribution_contenders": contenders[:15],
            "dominant_tie_tally": dominant_tally,
            "title_longshot_scatter": [
                {"team": tm, "pm_win": round(pw, 4), "model_win": round(mw, 4)}
                for (tm, pw, mw) in sorted(win_rows, key=lambda r: r[1])
            ],
        },
        "validation": {
            "walk_forward": (
                "Capture PM advancement rung prices daily (snapshot, not just the "
                "live overwrite). For each closed tie, grade the backed-out PM "
                "conditional and the model conditional with a Brier/log score; the "
                "edge is CONFIRMED only if model conditionals beat PM conditionals "
                "out-of-sample across >=40 graded ties, and the dominant-tie "
                "attribution predicts which combos actually paid."
            ),
            "current_sample": "1 cross-section, %d teams with a PM chain, %d full chains — insufficient for significance."
            % (sum(1 for t in teams if _pm_chain(t)), n_full_chain),
        },
    }
    return feed


def main() -> None:
    feed = build()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(feed, fh, indent=2, ensure_ascii=False)
    h = feed["headline"]
    print("wrote %s" % OUT_PATH)
    print(
        "  pm chain coherence: %d/%d monotonicity violations (%.1f%% coherent)"
        % (
            h["pm_chain_coherence"]["monotonicity_violations"],
            h["pm_chain_coherence"]["adjacent_pairs_checked"],
            h["pm_chain_coherence"]["coherent_pct"],
        )
    )
    print(
        "  Final-tie conditional edge (model-pm): mean=%s n=%s"
        % (h["final_tie_conditional_edge"].get("mean"), h["final_tie_conditional_edge"].get("n"))
    )
    print(
        "  win-tie conditional edge (model-pm): mean=%s n=%s"
        % (h["win_tie_conditional_edge"].get("mean"), h["win_tie_conditional_edge"].get("n"))
    )
    print("  full-chain teams: %d" % h["n_full_chain_teams"])
    print("  db PM price snapshots: %d (expect 0)" % feed["db_pm_inventory"]["pm_price_snapshots"])


if __name__ == "__main__":
    main()
