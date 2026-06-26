"""Stamp de-vigged closing-line value onto 1X2 predictions.

Reads the production ledger ``data/wca.db`` strictly read-only (immutable URI)
to find the last pre-kickoff h2h snapshot for each fixture, de-vigs it to a
consensus 1X2 (:func:`wca.closecapture.consensus_close`), and stamps the fair
closing price + fair-vs-fair CLV onto the matching open/settled 1X2 prediction
in ``data/dev.db``.

CLV here is fair-vs-fair: ``clv = model_fair_odds / closing_odds - 1`` where
``model_fair_odds = 1/model_prob`` and ``closing_odds`` is the leg's fair
closing decimal.  CLV is ``NULL`` (never ``0``) when no pre-kickoff snapshot
survives — the dev box has a sparse snapshot set, so honest coverage may be
low.

Idempotent: only predictions with a NULL ``closing_odds`` are stamped, so a
re-run never overwrites a captured close.  Deterministic apart from the
explicit ``now`` the caller supplies (used to gate fixtures that have kicked
off).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from wca import closecapture
from wca.predledger import store

# Read-only, immutable URI for the production ledger (snapshot source).
_PROD_DB = "data/wca.db"

_LEG_FROM_LABEL = {"home": "home", "draw": "draw", "away": "away"}


def _prod_ro_uri(prod_db_path: str) -> str:
    abspath = os.path.abspath(prod_db_path)
    return "file:%s?mode=ro&immutable=1" % abspath


def _bare(ts: Any) -> str:
    return closecapture._bare_ts(ts)


def _lag_seconds(close_ts: Any, kickoff: Any) -> Optional[int]:
    """Seconds between the close capture and kickoff (kickoff - close)."""
    a = closecapture._parse_dt(_bare(close_ts))
    b = closecapture._parse_dt(_bare(kickoff))
    if a is None or b is None:
        return None
    return int((b - a).total_seconds())


def stamp_closes(
    now: str,
    db: str = store._DEFAULT_DB,
    prod_db_path: str = _PROD_DB,
) -> Dict[str, Any]:
    """Stamp CLV on open 1X2 predictions whose fixture has kicked off.

    Returns ``{"stamped":n,"no_close":n,"future":n,"considered":n}``.  Only
    1X2 predictions with NULL ``closing_odds`` are candidates; a fixture's
    close is computed once and shared across its three legs.
    """
    now_bare = _bare(now)
    rows = [
        r
        for r in store.all_predictions(db)
        if (r["market"] or "").strip().lower() == "1x2"
        and r["closing_odds"] is None
    ]
    stats = {"stamped": 0, "no_close": 0, "future": 0, "considered": len(rows)}
    if not rows:
        return stats

    con = sqlite3.connect(_prod_ro_uri(prod_db_path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        # Cache one close per match_id (keyed by the prod snapshot match_id).
        closes: Dict[str, Optional[Dict[str, Any]]] = {}
        index = closecapture.match_index(con)
        to_set: List[Tuple[str, Dict[str, Any]]] = []
        for r in rows:
            mid = str(r["match_id"] or "")
            info = index.get(mid)
            if info is None:
                stats["no_close"] += 1
                continue
            kickoff = info["kickoff"].replace("Z", "+00:00")
            if now_bare and _bare(kickoff) > now_bare:
                stats["future"] += 1
                continue
            if mid not in closes:
                closes[mid] = closecapture.consensus_close(
                    con, mid, info["home"], info["away"], kickoff
                )
            close = closes[mid]
            if close is None:
                stats["no_close"] += 1
                continue
            leg = _LEG_FROM_LABEL.get((r["selection"] or "").strip().lower())
            if leg is None:
                stats["no_close"] += 1
                continue
            closing = closecapture.fair_closing_odds(close["triple"], leg, False)
            if closing is None or closing <= 1.0:
                stats["no_close"] += 1
                continue
            to_set.append(
                (
                    r["prediction_id"],
                    {
                        "closing_odds": closing,
                        "closing_devig_prob": float(close["triple"][leg]),
                        "close_ts": close["ts"],
                        "close_lag_seconds": _lag_seconds(close["ts"], kickoff),
                        "n_books_at_close": int(close["books"]),
                        "close_is_prematch": 1,
                    },
                )
            )
    finally:
        con.close()

    for pred_id, payload in to_set:
        store.set_prediction_close(
            pred_id,
            payload["closing_odds"],
            None,
            db,
            closing_devig_prob=payload["closing_devig_prob"],
            close_ts=payload["close_ts"],
            close_lag_seconds=payload["close_lag_seconds"],
            n_books_at_close=payload["n_books_at_close"],
            close_is_prematch=payload["close_is_prematch"],
        )
        stats["stamped"] += 1
    return stats
