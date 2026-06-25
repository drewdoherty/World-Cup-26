"""Reconstruct the historical 1X2 paper book from the model-predictions log.

``data/model_predictions_log.jsonl`` is an append-only record of the exact
blended 1X2 triple the model emitted at every card build.  This module replays
it into the prediction ledger as a paper book (``model_source='backfill'``):
one prediction row per fixture-leg per build, with deterministic ids so a
re-run upserts rather than duplicates.

Only 1X2 is reconstructed — the log holds nothing about scoreline / O-U / BTTS
/ futures, and fabricating those would violate the no-fabrication rule.

After loading rows the backfill (optionally) settles them against
``wc2026_results.json`` and stamps fair-vs-fair CLV from ``data/wca.db``
read-only odds snapshots, reusing :mod:`wca.predledger.settle` and
:mod:`wca.predledger.close`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from wca.predledger import close as pl_close
from wca.predledger import settle as pl_settle
from wca.predledger import store

_LOG_PATH = "data/model_predictions_log.jsonl"
_RESULTS_PATH = "data/processed/wc2026_results.json"
_ADV_RESULTS_PATH = "data/advancement_played_results.json"

_LEGS = ("home", "draw", "away")
_LEG_LABEL = {"home": "Home", "draw": "Draw", "away": "Away"}


def _build_id(generated: str) -> str:
    """One build per distinct ``generated`` timestamp in the log."""
    return "bf_" + str(generated).replace(":", "").replace("-", "").replace("T", "")


def _load_log(log_path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    p = Path(log_path)
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _fair_odds(prob: Optional[float]) -> Optional[float]:
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    return 1.0 / p if 0.0 < p < 1.0 else None


def _flatten_log(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One prediction row per (build, fixture, leg)."""
    out: List[Dict[str, Any]] = []
    for rec in records:
        generated = rec.get("generated", "")
        build_id = _build_id(generated)
        model = rec.get("model") or {}
        elo = rec.get("elo") or {}
        dc = rec.get("dc") or {}
        mkt = rec.get("market") or {}
        fixture = rec.get("fixture", "")
        match_id = str(rec.get("match_id", ""))
        kickoff = str(rec.get("kickoff", ""))
        for leg in _LEGS:
            mp = model.get(leg)
            if mp is None:
                continue
            mkt_p = mkt.get(leg)
            edge = (
                float(mp) - float(mkt_p)
                if (mp is not None and mkt_p is not None)
                else None
            )
            row = {
                "build_id": build_id,
                "ts_utc": generated,
                "match_id": match_id,
                "fixture": fixture,
                "kickoff_utc": kickoff,
                "market": "1X2",
                "selection": _LEG_LABEL[leg],
                "line": -1.0,
                "stage": "",
                "n_outcomes": 3,
                "model_prob": float(mp),
                "model_fair_odds": _fair_odds(mp),
                "elo_prob": (float(elo[leg]) if leg in elo else None),
                "dc_prob": (float(dc[leg]) if leg in dc else None),
                "market_devig_prob": (float(mkt_p) if mkt_p is not None else None),
                "market_best_odds": None,
                "market_book": None,
                "devig_method": "log_market_triple",
                "edge": edge,
                "ev_per_unit": None,
                "status": "open",
                "model_source": "backfill",
            }
            out.append(row)
    return out


def run_backfill(
    db: str = store._DEFAULT_DB,
    log_path: str = _LOG_PATH,
    results_path: str = _RESULTS_PATH,
    adv_results_path: str = _ADV_RESULTS_PATH,
    now: Optional[str] = None,
    do_settle: bool = True,
    do_close: bool = True,
    prod_db_path: str = pl_close._PROD_DB,
) -> Dict[str, Any]:
    """Replay the log into the prediction ledger; return a summary dict.

    ``now`` (ISO-8601 UTC) gates which fixtures have kicked off for the close
    stamp; the caller supplies it (no clock read here).  When ``now`` is
    ``None`` the close pass is skipped (no fixtures are considered kicked off),
    keeping the function deterministic and offline-safe in tests.
    """
    store.ensure_schema(db)
    records = _load_log(log_path)
    rows = _flatten_log(records)
    written = store.upsert_predictions(rows, db)

    summary: Dict[str, Any] = {
        "log_records": len(records),
        "rows_written": written,
        "settle": None,
        "close": None,
    }
    if do_settle:
        summary["settle"] = pl_settle.settle_open(
            results_path, adv_results_path, db, source="backfill"
        )
    if do_close and now:
        summary["close"] = pl_close.stamp_closes(now, db, prod_db_path)
    return summary
