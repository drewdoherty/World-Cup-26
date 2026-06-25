"""Rolling win-rate builder (Module B).

Two books, one outcome stream:

* **MODEL book** — model predictions taken from ``data/dev.db`` (or, when that is
  empty, reconstructed from ``data/model_predictions_log.jsonl`` x
  ``data/processed/wc2026_results.json``).  A row "wins" when the model argmax of
  the 1X2 triple equals the realised outcome.
* **REALIZED book** — settled bets in ``data/wca.db`` (read-only).  A row "wins"
  when ``status == 'won'``;  ``void``/``push`` are excluded from BOTH numerator
  and denominator of every rate.

Statistical honesty is the core principle:

* Wilson 95% intervals everywhere — never a bare point estimate.
* ``n`` is reported beside every aggregate;  below power thresholds the feed is
  flagged ``low_n``.
* CLV / market comparisons are fair-vs-fair only;  pushes/voids never count.
* No market/segment is invented:  an empty segment is emitted with ``n: 0``
  rather than dropped.

The library is deterministic: no wall-clock and no network.  ``wca.db`` is opened
strictly read-only (immutable URI).  Predledger reads come from ``dev.db``.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Power thresholds (below these we degrade to "band only" / low_n).
# --------------------------------------------------------------------------- #
LOW_N_WINRATE = 30          # win-rate series below this is "band only"
ACCA_CORR_MIN = 30          # leg-correlation not estimable below this many accas
ROLL_WINDOW = 10            # rolling window W
EWMA_H = 8                  # EWMA half-ish horizon -> lambda = 1 - 2^(-1/H)

_OUTCOMES = ("home", "draw", "away")
_SEL_TO_LEG = {"home": "home", "draw": "draw", "away": "away"}


# --------------------------------------------------------------------------- #
# Wilson score interval.
# --------------------------------------------------------------------------- #
def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(p, lo, hi)`` for the Wilson score interval of ``k`` of ``n``.

    Handles the degenerate cases the design calls out explicitly:

    * ``n == 0`` -> ``(None, None, None)`` (nothing observed).
    * ``n == 1`` -> a valid (wide) band around the single observation.
    * ``k == 0`` -> ``lo`` is pinned at 0.0 (never negative).
    * ``k == n`` -> ``hi`` is pinned at 1.0 (never above 1).
    """
    if n <= 0:
        return None, None, None
    p = k / n
    z2 = z * z
    d = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return p, lo, hi


def _band(k: int, n: int) -> Dict[str, Any]:
    p, lo, hi = wilson(k, n)
    return {"p": p, "lo": lo, "hi": hi, "n": n}


# --------------------------------------------------------------------------- #
# Team-name + outcome helpers.
# --------------------------------------------------------------------------- #
def _fixture_key(fixture: str):
    """``"A vs B"`` -> ``frozenset({canonical(A), canonical(B)})`` or ``None``."""
    from wca.data.teamnames import canonical

    parts = [p.strip() for p in fixture.split(" vs ")]
    if len(parts) != 2 or not all(parts):
        return None
    return frozenset(canonical(p) for p in parts)


def _argmax_outcome(triple: Dict[str, float]) -> Optional[str]:
    vals = {o: triple.get(o) for o in _OUTCOMES}
    if any(v is None for v in vals.values()):
        return None
    return max(_OUTCOMES, key=lambda o: vals[o])


# --------------------------------------------------------------------------- #
# MODEL book assembly.
# --------------------------------------------------------------------------- #
@dataclass
class ModelRow:
    """One settled match in the MODEL book, in matchday order."""

    match_id: str
    fixture: str
    kickoff: str
    model: Dict[str, float]
    market: Optional[Dict[str, float]]
    outcome: str          # realised 'home'|'draw'|'away'
    win: bool             # model argmax == outcome


def _results_map(results_path: str) -> Dict[Any, str]:
    """``fixture_key -> realised outcome`` from the processed results file."""
    with open(results_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("results", data) if isinstance(data, dict) else data
    out: Dict[Any, str] = {}
    for r in rows:
        k = _fixture_key(r.get("fixture", ""))
        oc = r.get("outcome")
        if k is not None and oc in _OUTCOMES:
            out[k] = oc
    return out


def _jsonl_market_by_match(jsonl_path: str) -> Dict[str, Dict[str, float]]:
    """``match_id -> market triple`` from the predictions log (latest build)."""
    out: Dict[str, Tuple[str, Dict[str, float]]] = {}
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            mid = rec.get("match_id")
            mkt = rec.get("market")
            gen = rec.get("generated", "")
            if not mid or not isinstance(mkt, dict):
                continue
            if mid not in out or gen > out[mid][0]:
                out[mid] = (gen, {o: mkt.get(o) for o in _OUTCOMES})
    return {mid: triple for mid, (_, triple) in out.items()}


def model_book_from_devdb(dev_db_path: str, market_by_match: Dict[str, Dict[str, float]]) -> List[ModelRow]:
    """Build the MODEL book from ``dev.db`` predictions.

    Predictions accumulate across many builds;  we keep the **latest build per
    match** so each settled match contributes exactly one row.  The model triple
    is read from the three 1X2 legs;  the realised outcome is the leg whose
    ``status == 'won'``.  Matches without a complete (Home/Draw/Away) settled
    triple are skipped (cannot form an argmax).
    """
    con = sqlite3.connect(dev_db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT match_id, fixture, kickoff_utc, build_id, selection, "
            "       model_prob, market_devig_prob, status "
            "FROM predictions WHERE market = '1X2' AND status IN ('won','lost')"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    by_match: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_match[r["match_id"]].append(r)

    book: List[ModelRow] = []
    for mid, rs in by_match.items():
        latest = max(r["build_id"] for r in rs)
        legs = [r for r in rs if r["build_id"] == latest]
        sel = {(_SEL_TO_LEG.get((r["selection"] or "").lower())): r for r in legs}
        if any(o not in sel for o in _OUTCOMES):
            continue
        model = {o: sel[o]["model_prob"] for o in _OUTCOMES}
        # market triple: prefer the jsonl (clean full triple), else dev.db legs.
        mkt = market_by_match.get(mid)
        if mkt is None or any(mkt.get(o) is None for o in _OUTCOMES):
            dev_mkt = {o: sel[o]["market_devig_prob"] for o in _OUTCOMES}
            mkt = dev_mkt if all(v is not None for v in dev_mkt.values()) else None
        won = [o for o in _OUTCOMES if sel[o]["status"] == "won"]
        if len(won) != 1:
            continue
        outcome = won[0]
        arg = _argmax_outcome(model)
        if arg is None:
            continue
        book.append(ModelRow(
            match_id=mid,
            fixture=legs[0]["fixture"],
            kickoff=legs[0]["kickoff_utc"] or "",
            model=model,
            market=mkt,
            outcome=outcome,
            win=(arg == outcome),
        ))
    book.sort(key=lambda r: (r.kickoff, r.match_id))
    return book


def model_book_from_jsonl(jsonl_path: str, results_path: str) -> List[ModelRow]:
    """Reconstruct the MODEL book from the predictions log x results.

    Used when ``dev.db`` has no predictions.  One row per settled fixture (the
    latest build's model triple) joined to the realised outcome.
    """
    resmap = _results_map(results_path)
    by_fx: Dict[Any, Dict[str, Any]] = {}
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            k = _fixture_key(rec.get("fixture", ""))
            if k is None:
                continue
            gen = rec.get("generated", "")
            if k not in by_fx or gen > by_fx[k].get("generated", ""):
                by_fx[k] = rec

    book: List[ModelRow] = []
    for k, rec in by_fx.items():
        if k not in resmap:
            continue
        model = {o: rec.get("model", {}).get(o) for o in _OUTCOMES}
        if any(v is None for v in model.values()):
            continue
        mkt_raw = rec.get("market") or {}
        mkt = {o: mkt_raw.get(o) for o in _OUTCOMES}
        if any(v is None for v in mkt.values()):
            mkt = None
        outcome = resmap[k]
        arg = _argmax_outcome(model)
        if arg is None:
            continue
        book.append(ModelRow(
            match_id=rec.get("match_id", ""),
            fixture=rec.get("fixture", ""),
            kickoff=str(rec.get("kickoff", "")),
            model=model,
            market=mkt,
            outcome=outcome,
            win=(arg == outcome),
        ))
    book.sort(key=lambda r: (r.kickoff, r.match_id))
    return book


def build_model_book(dev_db_path: str, jsonl_path: str, results_path: str) -> List[ModelRow]:
    """MODEL book: ``dev.db`` first, then jsonl x results fallback."""
    market_by_match = _jsonl_market_by_match(jsonl_path)
    book = model_book_from_devdb(dev_db_path, market_by_match)
    if book:
        return book
    return model_book_from_jsonl(jsonl_path, results_path)


# --------------------------------------------------------------------------- #
# REALIZED book assembly.
# --------------------------------------------------------------------------- #
@dataclass
class RealizedRow:
    bet_id: int
    ts_utc: str
    match_desc: str
    market: str
    selection: str
    decimal_odds: Optional[float]
    status: str           # 'won' | 'lost'  (void/push already excluded)
    win: bool


_ACCA_MARKET_KW = ("acca", "accumulator", "treble", "builder", "bet_builder", "fold")


def _is_acca(market: str, selection: str, match_desc: str) -> bool:
    m = (market or "").lower()
    if any(kw in m for kw in _ACCA_MARKET_KW):
        return True
    s = (selection or "").lower()
    if " + " in (selection or "") or "|" in (match_desc or ""):
        return True
    # "X all win" / "A/B/C" treble shorthands.
    if "all win" in s or s.count("/") >= 2:
        return True
    return False


def realized_book(wca_db_ro_uri: str) -> List[RealizedRow]:
    """Settled (won/lost only) bets from ``wca.db``, matchday order (ts_utc).

    ``void``/``push`` are excluded here, so they never enter any numerator or
    denominator downstream.
    """
    con = sqlite3.connect(wca_db_ro_uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, ts_utc, match_desc, market, selection, decimal_odds, status "
            "FROM bets WHERE status IN ('won','lost') ORDER BY ts_utc, id"
        ).fetchall()
    finally:
        con.close()
    out: List[RealizedRow] = []
    for r in rows:
        out.append(RealizedRow(
            bet_id=r["id"],
            ts_utc=r["ts_utc"] or "",
            match_desc=r["match_desc"] or "",
            market=r["market"] or "",
            selection=r["selection"] or "",
            decimal_odds=r["decimal_odds"],
            status=r["status"],
            win=(r["status"] == "won"),
        ))
    return out


# --------------------------------------------------------------------------- #
# Rolling / expanding / EWMA series.
# --------------------------------------------------------------------------- #
def ewma_lambda(h: int = EWMA_H) -> float:
    return 1.0 - 2.0 ** (-1.0 / h)


def ewma_series(wins: Sequence[bool], h: int = EWMA_H) -> List[Optional[float]]:
    """EWMA of a 0/1 win series with smoothing ``lambda = 1 - 2^(-1/H)``.

    The estimate at step ``t`` is ``sum(w_i x_i) / sum(w_i)`` with geometric
    weights ``w_i = lambda^(t-i)`` (most-recent weight 1).  Effective sample
    size ``n_eff = (sum w)^2 / sum w^2`` reaches a steady state of
    ``(2 - lambda) / lambda`` as ``t -> inf``.
    """
    lam = ewma_lambda(h)
    out: List[Optional[float]] = []
    s_wx = 0.0
    s_w = 0.0
    for x in wins:
        s_wx = (1.0 - lam) * s_wx + (1.0 if x else 0.0)
        s_w = (1.0 - lam) * s_w + 1.0
        out.append(s_wx / s_w if s_w > 0 else None)
    return out


def ewma_neff_steady_state(h: int = EWMA_H) -> float:
    lam = ewma_lambda(h)
    return (2.0 - lam) / lam


def rolling_series(wins: Sequence[bool], window: int = ROLL_WINDOW):
    """``(p_roll, lo, hi)`` Wilson band over the trailing ``window`` outcomes."""
    out = []
    for t in range(len(wins)):
        lo_i = max(0, t - window + 1)
        seg = wins[lo_i: t + 1]
        k = sum(1 for x in seg if x)
        out.append(wilson(k, len(seg)))
    return out


def expanding_series(wins: Sequence[bool]):
    """``(p_cum, lo, hi)`` Wilson band over all outcomes up to ``t`` (inclusive)."""
    out = []
    k = 0
    for t, x in enumerate(wins):
        k += 1 if x else 0
        out.append(wilson(k, t + 1))
    return out


# --------------------------------------------------------------------------- #
# Brier / BSS (model vs market on the 1X2 triple).
# --------------------------------------------------------------------------- #
def _brier_one(triple: Dict[str, float], outcome: str) -> float:
    """Multiclass Brier score for a single match (sum of squared errors)."""
    s = 0.0
    for o in _OUTCOMES:
        y = 1.0 if o == outcome else 0.0
        s += (triple[o] - y) ** 2
    return s


def brier_bss(book: Sequence[ModelRow]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Mean model Brier, mean market Brier, and BSS = 1 - model/market.

    Only matches with BOTH a complete model and a complete market triple count
    (fair-vs-fair).  Returns ``(None, None, None)`` when none qualify.
    """
    mb: List[float] = []
    kb: List[float] = []
    for r in book:
        if r.market is None or any(r.market.get(o) is None for o in _OUTCOMES):
            continue
        if any(r.model.get(o) is None for o in _OUTCOMES):
            continue
        mb.append(_brier_one(r.model, r.outcome))
        kb.append(_brier_one(r.market, r.outcome))
    if not mb:
        return None, None, None
    model_brier = sum(mb) / len(mb)
    market_brier = sum(kb) / len(kb)
    bss = (1.0 - model_brier / market_brier) if market_brier > 0 else None
    return model_brier, market_brier, bss


# --------------------------------------------------------------------------- #
# Segments.
# --------------------------------------------------------------------------- #
def _odds_bucket(odds: Optional[float]) -> Optional[str]:
    if odds is None:
        return None
    if odds < 1.5:
        return "odds_lt_1.5"
    if odds < 2.0:
        return "odds_1.5_2.0"
    if odds < 3.0:
        return "odds_2.0_3.0"
    return "odds_gte_3.0"


_ODDS_BUCKETS = ["odds_lt_1.5", "odds_1.5_2.0", "odds_2.0_3.0", "odds_gte_3.0"]
_ODDS_LABELS = {
    "odds_lt_1.5": "Odds < 1.5",
    "odds_1.5_2.0": "Odds 1.5-2.0",
    "odds_2.0_3.0": "Odds 2.0-3.0",
    "odds_gte_3.0": "Odds >= 3.0",
}
_LEG_LABELS = {"home": "Home", "draw": "Draw", "away": "Away"}


def model_leg_segments(book: Sequence[ModelRow]) -> List[Dict[str, Any]]:
    """MODEL win-rate split by the model's *picked* leg (home/draw/away)."""
    agg: Dict[str, List[int]] = {o: [0, 0] for o in _OUTCOMES}
    for r in book:
        pick = _argmax_outcome(r.model)
        if pick is None:
            continue
        agg[pick][1] += 1
        if r.win:
            agg[pick][0] += 1
    out = []
    for o in _OUTCOMES:
        k, n = agg[o]
        b = _band(k, n)
        out.append({"key": f"model_leg_{o}", "label": f"Model picks {_LEG_LABELS[o]}",
                    "book": "model", **b})
    return out


def realized_segments(book: Sequence[RealizedRow]) -> List[Dict[str, Any]]:
    """REALIZED win-rate split by odds bucket (always emit all buckets, n:0 ok)."""
    agg: Dict[str, List[int]] = {b: [0, 0] for b in _ODDS_BUCKETS}
    for r in book:
        bk = _odds_bucket(r.decimal_odds)
        if bk is None:
            continue
        agg[bk][1] += 1
        if r.win:
            agg[bk][0] += 1
    out = []
    for bk in _ODDS_BUCKETS:
        k, n = agg[bk]
        b = _band(k, n)
        out.append({"key": f"realized_{bk}", "label": _ODDS_LABELS[bk],
                    "book": "realized", **b})
    return out


# --------------------------------------------------------------------------- #
# Acca autopsy.
# --------------------------------------------------------------------------- #
def acca_autopsy(realized: Sequence[RealizedRow], wca_db_ro_uri: str) -> Dict[str, Any]:
    """Realised-acca strike rate + per-leg-type breakdown + near-misses.

    Leg decomposition uses ``reports._decompose_legs`` where it applies
    (multi-fixture or ``" + "``-joined builders);  leg-correlation is declared
    "not estimable" below :data:`ACCA_CORR_MIN` accas.
    """
    from wca.ledger.reports import _decompose_legs, _leg_is_result

    accas = [r for r in realized if _is_acca(r.market, r.selection, r.match_desc)]
    n_acca = len(accas)
    won = sum(1 for r in accas if r.win)

    # Per leg-type: result (1X2) vs prop/other, from decomposable accas.
    leg_agg: Dict[str, List[int]] = {"result": [0, 0], "prop": [0, 0]}
    for r in accas:
        legs = _decompose_legs(r.match_desc, r.selection)
        for (_key, leg_sel, _team, is_result) in legs:
            kind = "result" if is_result else "prop"
            leg_agg[kind][1] += 1
            # A lost acca means at least one leg failed; we cannot attribute the
            # failing leg per-leg without leg-level settlement, so leg "wins"
            # are only credited on a won acca (every leg won).  This is honest:
            # it under-counts, never over-counts, leg strike.
            if r.win:
                leg_agg[kind][0] += 1

    legs_out = []
    for kind in ("result", "prop"):
        k, n = leg_agg[kind]
        b = _band(k, n)
        legs_out.append({"type": kind, **b})

    # Near-miss: lost accas (we record the acca + flag missing-leg unknown).
    near_miss = []
    for r in accas:
        if not r.win:
            near_miss.append({"acca": r.selection or r.match_desc, "missing_leg": "unknown"})

    if n_acca < ACCA_CORR_MIN:
        note = (f"INSUFFICIENT SAMPLE: {n_acca} settled accas "
                f"(< {ACCA_CORR_MIN}); leg-correlation not estimable.")
    else:
        note = f"{n_acca} settled accas; leg strike credited only on full wins."

    strike = {"p": (won / n_acca) if n_acca else None, "n": n_acca}
    return {"legs": legs_out, "near_miss": near_miss, "note": note, "strike": strike}


# --------------------------------------------------------------------------- #
# Rolling feed rows (realized win series + model expected curves).
# --------------------------------------------------------------------------- #
def build_rolling(realized: Sequence[RealizedRow], model: Sequence[ModelRow]) -> List[Dict[str, Any]]:
    """One row per realised settled bet in matchday order.

    * ``p_roll`` / ``lo`` / ``hi`` — trailing-``W`` Wilson band of realised wins.
    * ``p_ewma`` — EWMA of realised wins.
    * ``p_cum`` — expanding realised win-rate.
    * ``exp_model`` / ``exp_market`` — expanding mean of the model's / market's
      probability on the *picked* leg over the model book, indexed positionally
      (a model "expected win-rate" reference curve), or ``None`` past its length.
    """
    wins = [r.win for r in realized]
    roll = rolling_series(wins, ROLL_WINDOW)
    ew = ewma_series(wins, EWMA_H)
    cum = expanding_series(wins)

    # Expanding mean of model / market probability on the model's picked leg.
    exp_model: List[Optional[float]] = []
    exp_market: List[Optional[float]] = []
    sm = 0.0
    sk = 0.0
    nk = 0
    nm = 0
    for r in model:
        pick = _argmax_outcome(r.model)
        if pick is not None:
            sm += r.model[pick]
            nm += 1
            exp_model.append(sm / nm)
            if r.market is not None and r.market.get(pick) is not None:
                sk += r.market[pick]
                nk += 1
            exp_market.append((sk / nk) if nk else None)
        else:
            exp_model.append(exp_model[-1] if exp_model else None)
            exp_market.append(exp_market[-1] if exp_market else None)

    rows = []
    for t, r in enumerate(realized):
        p_roll, lo, hi = roll[t]
        p_cum, _, _ = cum[t]
        rows.append({
            "t": t,
            "label": r.ts_utc[:10] if r.ts_utc else f"bet {r.bet_id}",
            "p_roll": p_roll,
            "lo": lo,
            "hi": hi,
            "p_ewma": ew[t],
            "p_cum": p_cum,
            "exp_model": exp_model[t] if t < len(exp_model) else None,
            "exp_market": exp_market[t] if t < len(exp_market) else None,
        })
    return rows


# --------------------------------------------------------------------------- #
# Top-level feed assembly.
# --------------------------------------------------------------------------- #
def build_feed(
    *,
    dev_db_path: str,
    wca_db_ro_uri: str,
    jsonl_path: str,
    results_path: str,
    generated: str,
) -> Dict[str, Any]:
    """Assemble the full ``winrate.json`` payload (pure; caller writes it)."""
    model = build_model_book(dev_db_path, jsonl_path, results_path)
    realized = realized_book(wca_db_ro_uri)

    # Headline win-rates.
    m_k = sum(1 for r in model if r.win)
    m_n = len(model)
    r_k = sum(1 for r in realized if r.win)
    r_n = len(realized)
    model_band = _band(m_k, m_n)
    realized_band = _band(r_k, r_n)

    model_brier, market_brier, bss = brier_bss(model)
    autopsy = acca_autopsy(realized, wca_db_ro_uri)

    # CLV-style coverage: share of realized settled bets with a captured close.
    coverage = _close_coverage(wca_db_ro_uri, r_n)

    headline = {
        "model_win_rate": model_band,
        "realized_win_rate": realized_band,
        "model_brier": model_brier,
        "market_brier": market_brier,
        "bss": bss,
        "acca_strike": autopsy["strike"],
        "coverage": coverage,
    }

    segments = model_leg_segments(model) + realized_segments(realized)
    rolling = build_rolling(realized, model)

    low_n = (r_n < LOW_N_WINRATE) or (m_n < LOW_N_WINRATE)

    return {
        "meta": {"generated": generated, "n_model": m_n, "n_realized": r_n},
        "headline": headline,
        "rolling": rolling,
        "segments": segments,
        "acca_autopsy": {
            "legs": autopsy["legs"],
            "near_miss": autopsy["near_miss"],
            "note": autopsy["note"],
        },
        "low_n": low_n,
    }


def _close_coverage(wca_db_ro_uri: str, n_settled: int) -> float:
    """Share of settled (won/lost) bets carrying a captured ``closing_odds``."""
    if n_settled <= 0:
        return 0.0
    con = sqlite3.connect(wca_db_ro_uri, uri=True)
    try:
        (k,) = con.execute(
            "SELECT count(*) FROM bets "
            "WHERE status IN ('won','lost') AND closing_odds IS NOT NULL"
        ).fetchone()
    finally:
        con.close()
    return k / n_settled
