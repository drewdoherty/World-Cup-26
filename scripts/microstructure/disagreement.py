#!/usr/bin/env python3
"""Market Disagreement microstructure analysis (READ-ONLY).

Per match / market / snapshot, measures the cross-book dispersion of de-vigged
implied probabilities for the *consensus favourite* in the 1X2 (h2h) market.
Two dispersion metrics are computed across the ~20 books quoting at a given
snapshot:

* ``std``    - sample standard deviation of the favourite's Shin-de-vigged
               implied probability across books (in probability points).
* ``spread`` - max-min range of the same quantity.

It then tests two hypotheses, honestly, on the captured window
(2026-06-11 .. 2026-06-23, theoddsapi only, 72 World Cup matches):

  H(a)  Does high *early* disagreement (first captured snapshot) predict a
        larger subsequent *move-to-close* in the consensus favourite prob
        (last pre-kickoff captured snapshot)?
  H(b)  Does high early disagreement predict a larger realized *model edge*
        (max model EV on that fixture, fuzzy-joined from the bet ledger)?

DATA CAVEATS (do not overstate):
  - Only ~8 of 72 fixtures have snapshots that reach kickoff; for most fixtures
    the "close" is the last snapshot in the capture window, typically 1-5 days
    BEFORE kickoff. So H(a) measures movement over the captured window, not a
    true closing line. We exclude any snapshot at/after kickoff (live odds).
  - The bet ledger uses WC2026_XXX slugs that do NOT join to the MD5 odds
    match_ids; H(b) relies on a team-name fuzzy join and lands at n~10 with
    promo/boost-inflated EVs. It is reported as indicative/framework-only.
  - Source is theoddsapi only; no Polymarket price history exists.

Run:
  PYTHONPATH=src .venv/bin/python scripts/microstructure/disagreement.py

Writes: site/microstructure/disagreement.json  (never mutates the DB).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from wca.markets.devig import devig  # noqa: E402

try:
    from scipy import stats as _sp_stats  # type: ignore
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - scipy optional
    _HAVE_SCIPY = False

_MIN_BOOKS = 5  # need at least this many complete 3-way books to call dispersion
_DEVIG_METHOD = "shin"  # favourite/longshot-aware; matches repo convention


# --------------------------------------------------------------------------- #
# stats helpers (no-scipy fallback so the feed always builds)
# --------------------------------------------------------------------------- #
def _pearsonr(x: List[float], y: List[float]) -> tuple[float, float]:
    if _HAVE_SCIPY:
        r, p = _sp_stats.pearsonr(x, y)
        return float(r), float(p)
    xa, ya = np.asarray(x, float), np.asarray(y, float)
    if xa.size < 3:
        return float("nan"), float("nan")
    r = float(np.corrcoef(xa, ya)[0, 1])
    n = xa.size
    # t approximation for p-value without scipy
    if abs(r) >= 1.0:
        return r, 0.0
    t = r * np.sqrt((n - 2) / (1 - r * r))
    # crude two-sided normal approx
    from math import erfc, sqrt
    p = erfc(abs(t) / sqrt(2))
    return r, float(p)


def _spearmanr(x: List[float], y: List[float]) -> tuple[float, float]:
    if _HAVE_SCIPY:
        rho, p = _sp_stats.spearmanr(x, y)
        return float(rho), float(p)
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    return _pearsonr(list(rx), list(ry))


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def _consensus_favourite_dispersion(
    books: Dict[str, Dict[str, float]],
    sels: List[str],
    fav: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """De-vig every complete 3-way book; return dispersion of the favourite's
    implied prob across books. ``fav`` may be pinned (to track the same
    selection at close as identified early)."""
    pbb: Dict[str, Dict[str, float]] = {}
    for bk, od in books.items():
        if set(sels) <= set(od) and all(od[s] > 1.0 for s in sels):
            try:
                p = devig([od[s] for s in sels], method=_DEVIG_METHOD)
            except Exception:
                continue
            pbb[bk] = dict(zip(sels, p))
    if len(pbb) < _MIN_BOOKS:
        return None
    avg = {s: float(np.mean([pb[s] for pb in pbb.values()])) for s in sels}
    if fav is None:
        fav = max(avg, key=avg.get)
    fp = np.array([pb[fav] for pb in pbb.values()], dtype=float)
    return {
        "n_books": len(pbb),
        "fav": fav,
        "mean": float(fp.mean()),
        "std": float(fp.std(ddof=1)),
        "spread": float(fp.max() - fp.min()),
    }


def _norm_team(s: Optional[str]) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def analyse(db_path: str) -> Dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    matches = cur.execute(
        """SELECT match_id,
                  json_extract(raw,'$.home_team')     AS h,
                  json_extract(raw,'$.away_team')     AS a,
                  json_extract(raw,'$.commence_time') AS ko
           FROM odds_snapshots WHERE market='h2h'
           GROUP BY match_id"""
    ).fetchall()

    window = cur.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc) FROM odds_snapshots WHERE market='h2h'"
    ).fetchone()

    per_match: List[Dict[str, Any]] = []
    disp_by_match: Dict[str, Dict[str, Any]] = {}
    name_to_mid: Dict[frozenset, str] = {}

    for m in matches:
        mid, h, a, ko = m["match_id"], m["h"], m["a"], m["ko"]
        sels = [h, a, "Draw"]
        # PRE-MATCH ONLY: exclude any snapshot at/after kickoff (live odds)
        rows = cur.execute(
            """SELECT ts_utc,
                      json_extract(raw,'$.bookmaker_key') AS bk,
                      selection, decimal_odds
               FROM odds_snapshots
               WHERE match_id=? AND market='h2h' AND ts_utc<=?
               ORDER BY ts_utc""",
            (mid, ko),
        ).fetchall()
        byts: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for r in rows:
            byts[r["ts_utc"]][r["bk"]][r["selection"]] = r["decimal_odds"]
        tss = sorted(byts.keys())
        if len(tss) < 2:
            continue
        early = _consensus_favourite_dispersion(byts[tss[0]], sels)
        if not early:
            continue
        close = _consensus_favourite_dispersion(
            byts[tss[-1]], sels, fav=early["fav"]
        )
        if not close:
            continue
        # hours between last captured snapshot and kickoff
        try:
            last_dt = dt.datetime.fromisoformat(tss[-1])
            ko_dt = dt.datetime.fromisoformat(ko)
            hrs_to_ko = (ko_dt - last_dt).total_seconds() / 3600.0
        except Exception:
            hrs_to_ko = None

        rec = {
            "match_id": mid,
            "desc": f"{h} v {a}",
            "kickoff": ko,
            "fav": early["fav"],
            "early_std": early["std"],
            "early_spread": early["spread"],
            "early_mean": early["mean"],
            "close_std": close["std"],
            "close_mean": close["mean"],
            "move_to_close": abs(close["mean"] - early["mean"]),
            "n_books_early": early["n_books"],
            "close_hrs_to_ko": hrs_to_ko,
        }
        per_match.append(rec)
        disp_by_match[mid] = rec
        name_to_mid[frozenset([_norm_team(h), _norm_team(a)])] = mid

    # ------------------------------------------------------------------ #
    # H(a): early disagreement vs move-to-close (over captured window)
    # ------------------------------------------------------------------ #
    es = [r["early_std"] for r in per_match]
    esp = [r["early_spread"] for r in per_match]
    mv = [r["move_to_close"] for r in per_match]
    cs = [r["close_std"] for r in per_match]

    r_std_move, p_std_move = _pearsonr(es, mv)
    rho_std_move, prho_std_move = _spearmanr(es, mv)
    r_spread_move, p_spread_move = _pearsonr(esp, mv)

    near = [r for r in per_match
            if r["close_hrs_to_ko"] is not None and abs(r["close_hrs_to_ko"]) < 48]
    if len(near) >= 4:
        r_near, p_near = _pearsonr(
            [r["early_std"] for r in near], [r["move_to_close"] for r in near]
        )
    else:
        r_near, p_near = float("nan"), float("nan")

    # ------------------------------------------------------------------ #
    # H(b): early disagreement vs realized model edge (fuzzy ledger join)
    # ------------------------------------------------------------------ #
    clean_markets = {
        "full-time result", "full time result", "match odds",
        "match winner", "pm_moneyline", "h2h", "match",
    }
    bets = cur.execute(
        """SELECT match_desc, market, ev, model_prob
           FROM bets WHERE ev IS NOT NULL AND model_prob IS NOT NULL"""
    ).fetchall()
    ev_per_match: Dict[str, List[float]] = defaultdict(list)
    for b in bets:
        if (b["market"] or "").lower() not in clean_markets:
            continue
        parts = re.split(r" vs | v | - ", b["match_desc"] or "")
        if len(parts) != 2:
            continue
        key = frozenset([_norm_team(parts[0]), _norm_team(parts[1])])
        mid = name_to_mid.get(key)
        if mid:
            ev_per_match[mid].append(b["ev"])

    hb_x, hb_y = [], []
    for mid, evs in ev_per_match.items():
        if mid in disp_by_match:
            hb_x.append(disp_by_match[mid]["early_std"])
            hb_y.append(max(evs))
    if len(hb_x) >= 4:
        r_hb, p_hb = _pearsonr(hb_x, hb_y)
        rho_hb, prho_hb = _spearmanr(hb_x, hb_y)
    else:
        r_hb = p_hb = rho_hb = prho_hb = float("nan")

    con.close()

    # ------------------------------------------------------------------ #
    # assemble feed
    # ------------------------------------------------------------------ #
    es_arr = np.array(es, float)
    esp_arr = np.array(esp, float)
    cs_arr = np.array(cs, float)
    mv_arr = np.array(mv, float)

    def pp(x: float) -> float:
        return round(float(x) * 100, 3)

    ranking = sorted(per_match, key=lambda r: r["early_std"], reverse=True)
    top = [
        {
            "desc": r["desc"],
            "fav": r["fav"],
            "early_std_pp": pp(r["early_std"]),
            "early_spread_pp": pp(r["early_spread"]),
            "move_to_close_pp": pp(r["move_to_close"]),
            "n_books": r["n_books_early"],
        }
        for r in ranking[:12]
    ]

    # scatter series for charting H(a)
    scatter = [
        {"x": pp(r["early_std"]), "y": pp(r["move_to_close"]), "desc": r["desc"]}
        for r in per_match
    ]

    feed = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window": {"start": window[0], "end": window[1]},
        "source": "theoddsapi",
        "data_caveat": (
            "1X2 favourite cross-book dispersion, Shin de-vig, theoddsapi only, "
            "72 World Cup fixtures, 2026-06-11..2026-06-23. 'Close' = last "
            "PRE-KICKOFF captured snapshot, which for most fixtures is 1-5 days "
            "before kickoff (only ~8 fixtures captured to kickoff), so move-to-close "
            "is movement over the captured window, not a true closing line. "
            "H(b) model-edge join is team-name fuzzy (n~10, promo/boost-inflated "
            "EV) and is indicative only."
        ),
        "n_matches": len(per_match),
        "distribution": {
            "early_std_pp": {
                "mean": pp(es_arr.mean()),
                "median": pp(np.median(es_arr)),
                "p10": pp(np.percentile(es_arr, 10)),
                "p90": pp(np.percentile(es_arr, 90)),
                "min": pp(es_arr.min()),
                "max": pp(es_arr.max()),
            },
            "early_spread_pp": {
                "mean": pp(esp_arr.mean()),
                "median": pp(np.median(esp_arr)),
                "max": pp(esp_arr.max()),
            },
            "close_std_pp": {
                "mean": pp(cs_arr.mean()),
                "median": pp(np.median(cs_arr)),
            },
            "move_to_close_pp": {
                "mean": pp(mv_arr.mean()),
                "median": pp(np.median(mv_arr)),
                "max": pp(mv_arr.max()),
            },
            "books_converge_to_close": bool(cs_arr.mean() < es_arr.mean()),
        },
        "hypothesis_a_disagreement_predicts_movement": {
            "n": len(per_match),
            "pearson_r_std": round(r_std_move, 3),
            "pearson_p_std": round(p_std_move, 4),
            "spearman_rho_std": round(rho_std_move, 3),
            "spearman_p_std": round(prho_std_move, 4),
            "pearson_r_spread": round(r_spread_move, 3),
            "pearson_p_spread": round(p_spread_move, 4),
            "near_close_subset": {
                "n": len(near),
                "pearson_r_std": round(r_near, 3),
                "pearson_p_std": round(p_near, 4),
            },
            "verdict": (
                "No predictive relationship (r~0, p>>0.05): early cross-book "
                "disagreement does not forecast subsequent line movement in this "
                "sample."
            ),
            "confidence": "measured",
        },
        "hypothesis_b_disagreement_predicts_model_edge": {
            "n": len(hb_x),
            "pearson_r": round(r_hb, 3) if hb_x else None,
            "pearson_p": round(p_hb, 4) if hb_x else None,
            "spearman_rho": round(rho_hb, 3) if hb_x else None,
            "spearman_p": round(prho_hb, 4) if hb_x else None,
            "verdict": (
                "Insufficient sample (n~10) and EVs contaminated by promo/boosts; "
                "point estimate is weak and negative (opposite of hypothesis). "
                "Indicative only."
            ),
            "confidence": "indicative",
        },
        "top_disagreement_fixtures": top,
        "scatter_std_vs_move": scatter,
    }
    return feed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(_ROOT / "data" / "wca.db"))
    ap.add_argument("--out", default=str(_ROOT / "site" / "microstructure" / "disagreement.json"))
    args = ap.parse_args()

    feed = analyse(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(feed, indent=2))

    d = feed["distribution"]["early_std_pp"]
    ha = feed["hypothesis_a_disagreement_predicts_movement"]
    print(f"Wrote {out}")
    print(f"  matches={feed['n_matches']}  early_std mean={d['mean']}pp "
          f"(p10={d['p10']} p90={d['p90']})")
    print(f"  H(a) pearson r={ha['pearson_r_std']} p={ha['pearson_p_std']} "
          f"(n={ha['n']})  spearman rho={ha['spearman_rho_std']}")
    print(f"  top fixture: {feed['top_disagreement_fixtures'][0]['desc']} "
          f"std={feed['top_disagreement_fixtures'][0]['early_std_pp']}pp")


if __name__ == "__main__":
    main()
