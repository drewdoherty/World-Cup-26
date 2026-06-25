"""Assemble the full ``rigor.json`` payload from the three data sources.

Sources (all read-only)
-----------------------
* ``wca.db`` money ledger — settled bets drive the CLV gates (G0-G3), the ROI
  / profit block, the stability series (G6) and the per-segment FDR (G7).
* ``model_predictions_log.jsonl`` + ``wc2026_results.json`` — the full model
  book (every 1X2 leg the model priced, not just the ones we bet) drives the
  outcome-anchored skill gates G4 (log-loss vs market) and G5 (calibration).
* ``dev.db`` prediction ledger (optional) — when present, its de-vigged
  closing odds give a second, fair-vs-fair CLV view; its settled outcomes
  reinforce the skill block.

Determinism
-----------
No wall-clock and no network in this module.  The ``generated`` timestamp is
injected by the caller (the script gets it from ``date -u``).  All bootstraps
are seeded.  The whole battery runs offline.

The output dict matches the Module-D spec exactly (see ``_emit``).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from wca.rigor import clv as clvmod
from wca.rigor import skill as skillmod
from wca.rigor import profit as profitmod
from wca.rigor import stability as stabmod
from wca.rigor import verdict as verdictmod

# ---------------------------------------------------------------------------
# Identifying futures / accas in the money ledger.
# ---------------------------------------------------------------------------

_FUTURES_KW = (
    "outright", "golden boot", "winner", "advancement", "to win the",
    "reach the", "eliminated", "group winner", "top goalscorer",
)
_ACCA_KW = ("acca", "treble", "double", "accumulator", "betbuilder",
            "bet builder", "bet-builder", "parlay", "2up", "2 up")


def _is_futures(market: str, selection: str, match_desc: str) -> bool:
    blob = " ".join(str(x or "").lower() for x in (market, selection, match_desc))
    return any(k in blob for k in _FUTURES_KW)


def _is_acca(market: str, selection: str, match_desc: str) -> bool:
    blob = " ".join(str(x or "").lower() for x in (market, selection))
    if any(k in blob for k in _ACCA_KW):
        return True
    # Multi-fixture desc or multi-leg selection.
    return ("|" in str(match_desc or "")) or (" + " in str(selection or ""))


def _platform_currency(platform: str) -> str:
    p = (platform or "").lower()
    return "USD" if ("polymarket" in p or "kalshi" in p) else "GBP"


# ---------------------------------------------------------------------------
# Inputs container.
# ---------------------------------------------------------------------------


@dataclass
class RigorInputs:
    """Loaded, cleaned inputs for the battery (all derived offline)."""

    # Money-ledger CLV rows (singles only; futures excluded from CLV n_eff).
    clv_values: List[float] = field(default_factory=list)
    clv_clusters: List[str] = field(default_factory=list)
    clv_order: List[str] = field(default_factory=list)  # ts for stability

    # Money-ledger profit rows (settled singles + accas; pushes/voids excluded).
    pl_values: List[float] = field(default_factory=list)
    pl_stakes: List[float] = field(default_factory=list)
    pl_clusters: List[str] = field(default_factory=list)

    # Full model book (1X2 legs joined to a realized result).
    model_probs: List[float] = field(default_factory=list)
    market_probs: List[float] = field(default_factory=list)
    outcomes: List[float] = field(default_factory=list)
    skill_fixtures: List[str] = field(default_factory=list)

    # Per-segment money-ledger rows for the FDR gate.
    segment_rows: List[Dict[str, Any]] = field(default_factory=list)

    # Bookkeeping.
    n_bets_total: int = 0
    n_futures: int = 0
    has_nonfutures_singles: bool = False


# ---------------------------------------------------------------------------
# Loaders.
# ---------------------------------------------------------------------------


def _open_ro(db_path: str) -> sqlite3.Connection:
    """Strictly read-only / immutable connection to a ledger DB."""
    uri = "file:%s?mode=ro&immutable=1" % os.path.abspath(db_path)
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_money_ledger(db_path: str, inputs: RigorInputs) -> None:
    """Populate the money-ledger-derived rows of ``inputs`` from ``wca.db``."""
    con = _open_ro(db_path)
    try:
        rows = con.execute("SELECT * FROM bets ORDER BY id").fetchall()
    finally:
        con.close()

    inputs.n_bets_total = len(rows)
    for r in rows:
        market = r["market"]
        selection = r["selection"]
        match_desc = r["match_desc"]
        status = (r["status"] or "").lower()
        is_fut = _is_futures(market, selection, match_desc)
        is_acca = _is_acca(market, selection, match_desc)
        if is_fut:
            inputs.n_futures += 1

        # --- CLV rows: singles only, non-futures, with a captured close -----
        clv = r["clv"]
        if (clv is not None and not is_fut and not is_acca
                and status in ("won", "lost")):
            inputs.clv_values.append(float(clv))
            # Cluster = fixture (match_id) for singles.
            inputs.clv_clusters.append(str(r["match_id"] or r["id"]))
            inputs.clv_order.append(str(r["ts_utc"] or r["id"]))
            inputs.has_nonfutures_singles = True

        # --- profit rows: settled won/lost; pushes/voids excluded ----------
        if status in ("won", "lost") and r["settled_pl"] is not None \
                and r["stake"] is not None:
            inputs.pl_values.append(float(r["settled_pl"]))
            inputs.pl_stakes.append(float(r["stake"]))
            # Cluster = acca id (use match_desc) for accas, fixture for singles.
            cluster = ("acca:" + str(match_desc)) if is_acca \
                else str(r["match_id"] or r["id"])
            inputs.pl_clusters.append(cluster)

            # --- segment membership (for the FDR gate G7) ------------------
            seg_market = "futures" if is_fut else (
                "acca" if is_acca else "single")
            inputs.segment_rows.append({
                "clv": float(clv) if (clv is not None and not is_fut
                                      and not is_acca) else None,
                "market_seg": seg_market,
                "source": str(r["source"] or "model"),
                "currency": _platform_currency(r["platform"]),
                "pl": float(r["settled_pl"]),
                "stake": float(r["stake"]),
            })


def load_full_book_from_jsonl(
    jsonl_path: str, results_path: str, inputs: RigorInputs
) -> None:
    """Populate the full-model-book skill rows from the jsonl + results join.

    For every 1X2 fixture in the model log that has a realized result, emit
    three legs (home / draw / away).  ``model_probs`` are the model's triple,
    ``market_probs`` the de-vigged market triple, ``outcomes`` the realized
    0/1, ``skill_fixtures`` the fixture id used to pair within G4.  Only the
    *latest* model build per fixture is used (deduped on match_id), so the same
    match is not double-counted across re-predictions.
    """
    from wca.data.teamnames import canonical
    from wca.tracking import split_fixture

    def fxkey(fixture: str):
        sp = split_fixture(fixture)
        if not sp:
            return None
        return frozenset((canonical(sp[0]), canonical(sp[1])))

    # results -> outcome by fixture key.
    with open(results_path) as fh:
        results = json.load(fh)["results"]
    res_by_fx: Dict[frozenset, str] = {}
    for r in results:
        if r.get("outcome") in ("home", "draw", "away"):
            k = fxkey(r["fixture"])
            if k is not None:
                res_by_fx[k] = r["outcome"]

    # latest model build per fixture key.
    latest: Dict[frozenset, Dict[str, Any]] = {}
    with open(jsonl_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            k = fxkey(rec.get("fixture", ""))
            if k is None:
                continue
            gen = rec.get("generated", "")
            if k not in latest or gen >= latest[k].get("generated", ""):
                latest[k] = rec

    leg_map = {"home": ("home", 1.0), "draw": ("draw", 1.0), "away": ("away", 1.0)}
    for k, rec in sorted(latest.items(), key=lambda kv: str(kv[1].get("kickoff"))):
        outcome = res_by_fx.get(k)
        if outcome is None:
            continue
        model = rec.get("model") or {}
        market = rec.get("market") or {}
        fid = "|".join(sorted(canonical(x) for x in k))
        for leg in ("home", "draw", "away"):
            mp = model.get(leg)
            kp = market.get(leg)
            if mp is None:
                continue
            o = 1.0 if leg == outcome else 0.0
            inputs.model_probs.append(float(mp))
            inputs.market_probs.append(float(kp) if kp is not None else float("nan"))
            inputs.outcomes.append(o)
            inputs.skill_fixtures.append(fid)


def load_predledger(db_path: str, inputs: RigorInputs) -> None:
    """Optionally augment the skill rows from the dev.db prediction ledger.

    Only used when ``model_predictions_log.jsonl`` yielded nothing (defensive
    fallback).  Uses the latest build per (match_id, selection) settled 1X2
    prediction so build re-runs do not inflate the sample.
    """
    if inputs.model_probs:  # jsonl already supplied the full book.
        return
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return
    con = _open_ro(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM predictions WHERE status IN ('won','lost') "
            "AND market='1X2' ORDER BY ts_utc"
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return
    con.close()

    latest: Dict[Tuple[str, str], sqlite3.Row] = {}
    for r in rows:
        key = (str(r["match_id"]), str(r["selection"]))
        prev = latest.get(key)
        if prev is None or (r["ts_utc"] or "") >= (prev["ts_utc"] or ""):
            latest[key] = r
    for (mid, _sel), r in latest.items():
        mp = r["model_prob"]
        kp = r["market_devig_prob"]
        if mp is None:
            continue
        inputs.model_probs.append(float(mp))
        inputs.market_probs.append(float(kp) if kp is not None else float("nan"))
        inputs.outcomes.append(1.0 if (r["status"] or "").lower() == "won" else 0.0)
        inputs.skill_fixtures.append(str(mid))


# ---------------------------------------------------------------------------
# Segment FDR helper.
# ---------------------------------------------------------------------------


def _segment_pvalues(inputs: RigorInputs) -> List[Dict[str, Any]]:
    """Build per-segment one-sided p-values of a positive CLV edge for G7."""
    rows = inputs.segment_rows
    total = max(1, len(rows))
    segs: Dict[Tuple[str, str], List[float]] = {}
    for r in rows:
        if r["clv"] is None:
            continue
        for dim, val in (("market", r["market_seg"]),
                         ("source", r["source"]),
                         ("currency", r["currency"])):
            segs.setdefault((dim, str(val)), []).append(r["clv"])

    out: List[Dict[str, Any]] = []
    for (dim, val), clvs in sorted(segs.items()):
        arr = np.asarray(clvs, dtype=float)
        n = len(arr)
        coverage = n / total
        if n < 2 or arr.std(ddof=1) == 0:
            p_raw = None
        else:
            mean = float(arr.mean())
            sd = float(arr.std(ddof=1))
            t = mean / (sd / math.sqrt(n))
            p_raw = skillmod._t_sf(t, n - 1)  # one-sided P(positive edge)
        out.append({"key": "%s=%s" % (dim, val), "p_raw": p_raw,
                    "coverage": coverage})
    return out


# ---------------------------------------------------------------------------
# Top-level builder.
# ---------------------------------------------------------------------------


def build_rigor(
    *,
    wca_db: str,
    jsonl_path: str,
    results_path: str,
    dev_db: Optional[str] = None,
    generated: str,
    seed: int = 20260625,
) -> Dict[str, Any]:
    """Run the full battery and return the ``rigor.json`` payload dict.

    Parameters
    ----------
    wca_db:
        Read-only path to the money ledger (``data/wca.db``).
    jsonl_path / results_path:
        The model-prediction log and the realized-results JSON.
    dev_db:
        Optional prediction-ledger DB (fallback skill source).
    generated:
        ISO-8601 Z timestamp injected by the caller (deterministic library).
    """
    inputs = RigorInputs()
    load_money_ledger(wca_db, inputs)
    load_full_book_from_jsonl(jsonl_path, results_path, inputs)
    if dev_db:
        load_predledger(dev_db, inputs)

    # --- CLV block + G0(CLV)/G1/G2/G3 ----------------------------------------
    cblock = clvmod.clv_block(inputs.clv_values, inputs.clv_clusters, seed=seed)
    clv_n_eff = float(cblock["n_eff"])

    # --- Profit block + G0(ROI) ----------------------------------------------
    pblock = profitmod.profit_block(
        inputs.pl_values, inputs.pl_stakes, inputs.pl_clusters, seed=seed)

    # --- Skill block: G4 (vs market) + G5 (calibration) ----------------------
    g4 = skillmod.skill_vs_market(
        inputs.model_probs, inputs.market_probs,
        inputs.outcomes, inputs.skill_fixtures)
    g5 = skillmod.calibration(inputs.model_probs, inputs.outcomes)
    bss = skillmod.brier_skill(
        inputs.model_probs, inputs.market_probs, inputs.outcomes)

    # --- Stability block: G6 (CLV series ordered in time) --------------------
    # Order CLV values by their timestamp for the OOS/IS and break scan.
    if inputs.clv_values:
        order = sorted(range(len(inputs.clv_values)),
                       key=lambda i: inputs.clv_order[i])
        clv_series = [inputs.clv_values[i] for i in order]
    else:
        clv_series = []
    sblock = stabmod.stability_block(clv_series)

    # --- Segment FDR: G7 ------------------------------------------------------
    seg_inputs = _segment_pvalues(inputs)
    segments = verdictmod.segments_block(seg_inputs)
    any_survive = any(s["survives_fdr"] for s in segments
                      if s["survives_fdr"] is not None)
    g7_pass = (None if not any(s["survives_fdr"] is not None for s in segments)
               else bool(any_survive))

    # --- Gate flags ----------------------------------------------------------
    g0_clv = cblock["gates"]["G0"]["pass"]
    g0_roi = pblock["gate"]
    g0_pass = bool(g0_clv) or bool(g0_roi)  # either floor met counts as G0.
    gate_flags: Dict[str, Optional[bool]] = {
        "G0": g0_pass,
        "G1": cblock["gates"]["G1"]["pass"],
        "G2": cblock["gates"]["G2"]["pass"],
        "G3": cblock["gates"]["G3"]["pass"],
        "G4": g4["pass"],
        "G5": g5["pass"],
        "G6": sblock["pass"],
        "G7": g7_pass,
    }

    # --- Verdict --------------------------------------------------------------
    # Futures-only book (no non-futures singles with CLV) -> permanent insuf.
    futures_only = (inputs.n_futures > 0 and not inputs.has_nonfutures_singles
                    and len(inputs.clv_values) == 0)
    vblock = verdictmod.assemble_verdict(
        gate_flags, cblock, clv_n_eff, futures_only=futures_only)

    # --- Stage label ----------------------------------------------------------
    if vblock["level"] == "INSUFFICIENT_SAMPLE":
        stage = "insufficient"
    elif vblock["level"] in ("PROMISING",):
        stage = "clv_only"
    elif vblock["level"] == "EDGE_LIKELY":
        stage = "edge_confirmed"
    else:
        stage = vblock["level"].lower()

    payload = _emit(
        generated=generated,
        inputs=inputs,
        clv_n_eff=clv_n_eff,
        cblock=cblock,
        pblock=pblock,
        g4=g4,
        g5=g5,
        bss=bss,
        sblock=sblock,
        segments=segments,
        gate_flags=gate_flags,
        vblock=vblock,
        stage=stage,
    )
    return payload


def _g(value: Any) -> Any:
    """Coerce numpy / NaN scalars to JSON-safe Python (NaN -> None)."""
    if value is None:
        return None
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _emit(**kw: Any) -> Dict[str, Any]:
    """Render the exact Module-D ``rigor.json`` schema from the blocks."""
    inputs: RigorInputs = kw["inputs"]
    cblock = kw["cblock"]
    pblock = kw["pblock"]
    g4 = kw["g4"]
    g5 = kw["g5"]
    sblock = kw["sblock"]
    gf = kw["gate_flags"]
    clv_n_eff = kw["clv_n_eff"]

    n_total = inputs.n_bets_total
    # n_eff reported at the top is the CLV effective sample (the binding floor).
    n_eff = clv_n_eff

    gates = [
        {
            "id": "G0", "name": "Power floor (n_eff)",
            "stat": "n_eff (CLV>=25 or ROI>=100)",
            "value": _g(clv_n_eff),
            "threshold": "CLV n_eff>=25 or ROI n_eff>=100",
            "pass": gf["G0"],
            "note": "effective sample after cluster deflation (match/acca)",
        },
        {
            "id": "G1", "name": "CLV lower bound > ROPE floor",
            "stat": "CLV 95% lower bound",
            "value": _g(cblock["gates"]["G1"]["value"]),
            "threshold": "> %.3f (cost-adjusted ROPE)" % clvmod.CLV_ROPE_FLOOR,
            "pass": cblock["gates"]["G1"]["pass"],
            "note": "must clear a cost-adjusted floor, not merely 0",
        },
        {
            "id": "G2", "name": "Sequential CLV significance",
            "stat": "observed z vs anytime-valid boundary",
            "value": _g(cblock["gates"]["G2"]["value"]),
            "threshold": ("z > %.2f (grows with n_eff)"
                          % (cblock["gates"]["G2"]["threshold"] or float("nan"))),
            "pass": cblock["gates"]["G2"]["pass"],
            "note": "no fixed 1.65/1.96 cutoff — survives optional stopping",
        },
        {
            "id": "G3", "name": "Beat-rate > placebo 95th pct",
            "stat": "Wilson lower bound of beat-rate",
            "value": _g(cblock["gates"]["G3"]["value"]),
            "threshold": ("> placebo %s"
                          % ("%.3f" % cblock["gates"]["G3"]["placebo"]
                             if cblock["gates"]["G3"]["placebo"] is not None
                             else "n/a")),
            "pass": cblock["gates"]["G3"]["pass"],
            "note": "no-edge price-taker null, not 0.5",
        },
        {
            "id": "G4", "name": "Skill vs market (paired log-loss)",
            "stat": "mean per-fixture log-loss differential",
            "value": _g(g4["logloss_diff"]),
            "threshold": "mean>0 one-sided p<0.05 (model better)",
            "pass": g4["pass"],
            "note": "outcome-anchored: a best-price artifact cannot pass this; "
                    "n_fixtures=%s" % g4.get("n_fixtures"),
        },
        {
            "id": "G5", "name": "Calibration (slope∋1, intercept∋0)",
            "stat": "logistic calibration slope",
            "value": _g(g5["slope"]),
            "threshold": "slope CI∋1 & intercept CI∋0 (N>=100)",
            "pass": g5["pass"],
            "note": "outcome-anchored; N=%s settled (need >=100)" % g5["n"],
        },
        {
            "id": "G6", "name": "Stability (no break & OOS/IS>0.5)",
            "stat": "OOS/IS ratio",
            "value": _g(sblock["oos_is_ratio"]),
            "threshold": "no break AND OOS/IS > 0.5",
            "pass": sblock["pass"],
            "note": "break_detected=%s" % sblock["break_detected"],
        },
        {
            "id": "G7", "name": "Multiple testing (BH FDR)",
            "stat": "any segment survives FDR 5%",
            "value": _g(sum(1 for s in kw["segments"]
                            if s["survives_fdr"] is True)),
            "threshold": "BH-adjusted q<0.05 in >=1 segment",
            "pass": gf["G7"],
            "note": "guards against garden-of-forking-paths on segments",
        },
    ]

    return {
        "meta": {
            "generated": kw["generated"],
            "n": int(n_total),
            "n_eff": _g(round(float(n_eff), 3)),
            "stage": kw["stage"],
        },
        "verdict": {
            "level": kw["vblock"]["level"],
            "label": kw["vblock"]["label"],
            "reason": kw["vblock"]["reason"],
            "color": kw["vblock"]["color"],
        },
        "gates": gates,
        "clv_block": {
            "mean": _g(cblock["mean"]),
            "lower": _g(cblock["lower"]),
            "beat_rate": _g(cblock["beat_rate"]),
            "placebo_null": _g(cblock["placebo_null"]),
            "n_eff": _g(round(float(cblock["n_eff"]), 3)),
        },
        "skill_block": {
            "logloss_diff": _g(g4["logloss_diff"]),
            "brier_skill": _g(kw["bss"]),
            "calibration_slope": _g(g5["slope"]),
        },
        "profit_block": {
            "roi": _g(pblock["roi"]),
            "roi_lo": _g(pblock["roi_lo"]),
            "sharpe": _g(pblock["sharpe"]),
            "n": int(pblock["n"]),
        },
        "stability_block": {
            "break_detected": sblock["break_detected"],
            "oos_is_ratio": _g(sblock["oos_is_ratio"]),
        },
        "segments": [
            {
                "key": s["key"],
                "p_raw": _g(s["p_raw"]),
                "p_adj": _g(s["p_adj"]),
                "survives_fdr": s["survives_fdr"],
                "coverage": _g(round(float(s["coverage"]), 4)),
            }
            for s in kw["segments"]
        ],
        "samples_to_sig": {
            "roi_n": profitmod.ROI_SAMPLE_TO_SIG,
            "clv_n": int(clvmod.N_EFF_CLV_MIN),
            "current_n_eff": _g(round(float(clv_n_eff), 3)),
        },
    }
