"""Project the prediction ledger to the site-analytics feed JSON.

Writes ``site-analytics/data/predledger.json`` atomically.  Every aggregate
carries its ``n`` and a Wilson 95% interval (never a bare point estimate);
pushes/voids are excluded from both numerator and denominator of any rate; CLV
is counted only where a close exists.

The feed is the honest-uncertainty surface: where a market / segment has no
data it is emitted with ``n:0`` rather than dropped, and below-power segments
are flagged by their interval width rather than by a hidden threshold.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wca import tracking
from wca.predledger import store

_FEED_PATH = "site-analytics/data/predledger.json"

# Markets we always surface in coverage / by_market even when empty (n:0),
# so a zero-data market is visible rather than silently missing.
_MARKETS = ("1X2", "scoreline", "ou", "btts", "advancement")


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Wilson score interval for ``k`` successes in ``n`` trials.

    Returns ``(p, lo, hi)`` with ``p = k/n``.  Handles the edge cases:
    ``n == 0`` -> ``(None, None, None)``; ``k == 0`` and ``k == n`` give the
    correct one-sided-ish Wilson bounds (the centre is pulled off 0/1 and the
    half-width keeps the interval inside ``[0, 1]``); ``n == 1`` is finite.
    """
    if n <= 0:
        return None, None, None
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / d
    half = (z / d) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return p, lo, hi


def _settled_mask(row: Any) -> bool:
    return (row["status"] or "") in ("won", "lost", "push", "void")


def _coverage(rows: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {
        m: {"n": 0, "clv_n": 0, "coverage_pct": 0.0} for m in _MARKETS
    }
    for r in rows:
        m = r["market"] or "?"
        bucket = out.setdefault(m, {"n": 0, "clv_n": 0, "coverage_pct": 0.0})
        bucket["n"] += 1
        if r["clv"] is not None:
            bucket["clv_n"] += 1
    for bucket in out.values():
        n = bucket["n"]
        bucket["coverage_pct"] = (
            round(100.0 * bucket["clv_n"] / n, 2) if n else 0.0
        )
    return out


def _by_market(rows: List[Any]) -> List[Dict[str, Any]]:
    # Group rows by market.
    groups: Dict[str, List[Any]] = {m: [] for m in _MARKETS}
    for r in rows:
        groups.setdefault(r["market"] or "?", []).append(r)

    out: List[Dict[str, Any]] = []
    for market, grp in groups.items():
        n = len(grp)
        settled = [r for r in grp if _settled_mask(r)]
        won = sum(1 for r in settled if r["status"] == "won")
        lost = sum(1 for r in settled if r["status"] == "lost")
        push = sum(1 for r in settled if r["status"] in ("push", "void"))
        # Rate denominator excludes pushes/voids.
        decided = won + lost
        p = lo = hi = None
        if decided > 0:
            p, lo, hi = wilson(won, decided)
        brier = _model_brier(grp)
        out.append(
            {
                "market": market,
                "n": n,
                "settled": len(settled),
                "won": won,
                "lost": lost,
                "push": push,
                "win_rate": _round(p),
                "win_lo": _round(lo),
                "win_hi": _round(hi),
                "model_brier": _round(brier),
            }
        )
    return out


def _model_brier(rows: List[Any]) -> Optional[float]:
    """Mean per-prediction Brier on settled won/lost rows with a model_prob.

    Treats each settled prediction as a binary event (this selection happened
    or not): ``(model_prob - outcome)^2`` averaged.  ``None`` when no settled
    row carries a model_prob.
    """
    vals: List[float] = []
    for r in rows:
        if r["status"] not in ("won", "lost"):
            continue
        mp = r["model_prob"]
        if mp is None:
            continue
        outcome = 1.0 if r["status"] == "won" else 0.0
        vals.append((float(mp) - outcome) ** 2)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _headline(rows: List[Any]) -> Dict[str, Any]:
    paper = [r for r in rows if r["bet_id"] is None]
    decided = [r for r in paper if r["status"] in ("won", "lost")]
    won = sum(1 for r in decided if r["status"] == "won")
    p, lo, hi = wilson(won, len(decided)) if decided else (None, None, None)
    with_clv = sum(1 for r in rows if r["clv"] is not None)
    n_all = len(rows)
    return {
        "paper_win_rate": {
            "p": _round(p),
            "lo": _round(lo),
            "hi": _round(hi),
            "n": len(decided),
        },
        "n_with_clv": with_clv,
        "clv_coverage_pct": round(100.0 * with_clv / n_all, 2) if n_all else 0.0,
    }


def _recent(rows: List[Any], limit: int = 60) -> List[Dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda r: (r["ts_utc"] or "", r["kickoff_utc"] or "", r["fixture"] or ""),
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for r in ordered[:limit]:
        out.append(
            {
                "build_id": r["build_id"],
                "fixture": r["fixture"],
                "market": r["market"],
                "selection": r["selection"],
                "model_prob": _round(r["model_prob"]),
                "status": r["status"],
                "clv": _round(r["clv"]),
            }
        )
    return out


def _round(value: Optional[float], places: int = 4) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), places)


def build_feed(generated: str, db: str = store._DEFAULT_DB) -> Dict[str, Any]:
    """Assemble the feed payload (pure; no file I/O, no clock read)."""
    rows = store.all_predictions(db)
    n_pred = len(rows)
    n_paper = sum(1 for r in rows if r["bet_id"] is None)
    n_realized = n_pred - n_paper
    n_with_clv = sum(1 for r in rows if r["clv"] is not None)
    return {
        "meta": {
            "generated": generated,
            "db": "dev.db",
            "n_predictions": n_pred,
            "n_paper": n_paper,
            "n_realized": n_realized,
            "n_with_clv": n_with_clv,
        },
        "coverage": _coverage(rows),
        "by_market": _by_market(rows),
        "headline": _headline(rows),
        "recent": _recent(rows),
    }


def write_feed(
    generated: str,
    db: str = store._DEFAULT_DB,
    feed_path: str = _FEED_PATH,
) -> str:
    """Build and atomically write the feed; return the path written."""
    payload = build_feed(generated, db)
    path = Path(feed_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return str(path)


# Convenience alias matching the task's "store.publish" naming option.
def publish(generated: str, db: str = store._DEFAULT_DB, feed_path: str = _FEED_PATH) -> str:
    return write_feed(generated, db, feed_path)
