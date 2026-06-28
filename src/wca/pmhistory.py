"""Polymarket price-history store — the trajectory backbone for outright edge.

Match 1X2 markets close at kickoff, so CLV works there. Outright / advancement /
knockout markets have no fixed close and a single tournament's outcomes are
mutually correlated (so outcome-based effective N collapses to ~1). The only
*leading* edge signal available for them is whether the Polymarket price drifts
toward the model's number over the holding period — and that needs a captured
**price trajectory**, which the project did not store (PM was overwritten each
build → "COLLECTING").

This module is the append-only store for that trajectory. It is deliberately
network-free: the CLI (`scripts/wca_pm_snapshot.py`) does the live fetch via the
existing :mod:`wca.data.polymarket` and hands plain rows here, so the storage and
read paths are fully unit-testable without the API.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Dict, List, Optional, Sequence

#: One captured PM quote. ``pm_mid`` is the YES mid (an implied probability in
#: [0,1]); ``model_prob`` is the model's probability for the same event at capture
#: time (stored alongside so convergence needs no later re-join).
SNAPSHOT_FIELDS = ("kind", "team", "stage", "market_slug", "token_id", "pm_mid", "model_prob", "raw")


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the append-only ``pm_snapshots`` table + index if absent."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS pm_snapshots ("
        " ts_utc TEXT NOT NULL,"
        " kind TEXT NOT NULL,"          # 'advancement' | 'outright' | 'match' | ...
        " team TEXT,"
        " stage TEXT,"                  # R32/R16/QF/SF/Final/win (advancement) or NULL
        " market_slug TEXT,"
        " token_id TEXT,"
        " pm_mid REAL,"                 # YES mid = implied probability
        " model_prob REAL,"            # model probability at capture
        " raw TEXT"
        ")"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_pm_snap_market ON pm_snapshots(kind, team, stage, ts_utc)"
    )
    con.commit()


def _market_key(row: Dict[str, object]) -> str:
    """Stable identity for a market across snapshots."""
    return "|".join(str(row.get(k, "")) for k in ("kind", "team", "stage", "market_slug"))


def append_snapshots(con: sqlite3.Connection, rows: Sequence[Dict[str, object]], ts_utc: str) -> int:
    """Append captured PM quotes at capture time ``ts_utc`` (append-only).

    Each row needs at least ``kind`` and ``pm_mid``; missing optional fields are
    stored NULL. Rows whose ``pm_mid`` is not a finite probability are skipped.
    Returns the number of rows inserted.
    """
    ensure_schema(con)
    out = 0
    for r in rows:
        mid = r.get("pm_mid")
        try:
            mid = float(mid)
        except (TypeError, ValueError):
            continue
        if not (0.0 <= mid <= 1.0):
            continue
        raw = r.get("raw")
        if raw is not None and not isinstance(raw, str):
            raw = json.dumps(raw, sort_keys=True)
        con.execute(
            "INSERT INTO pm_snapshots(ts_utc, kind, team, stage, market_slug, token_id, pm_mid, model_prob, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts_utc, str(r.get("kind")), r.get("team"), r.get("stage"), r.get("market_slug"),
             r.get("token_id"), mid,
             (float(r["model_prob"]) if r.get("model_prob") is not None else None), raw),
        )
        out += 1
    con.commit()
    return out


def trajectory(con: sqlite3.Connection, *, kind: Optional[str] = None) -> Dict[str, List[Dict[str, object]]]:
    """All snapshots grouped by market, each list ordered by capture time.

    Returns ``{market_key: [{ts_utc, pm_mid, model_prob, team, stage, kind}, ...]}``.
    """
    q = ("SELECT ts_utc, kind, team, stage, market_slug, pm_mid, model_prob FROM pm_snapshots")
    params: List[object] = []
    if kind is not None:
        q += " WHERE kind = ?"
        params.append(kind)
    q += " ORDER BY ts_utc"
    out: Dict[str, List[Dict[str, object]]] = {}
    for ts, k, team, stage, slug, mid, mdl in con.execute(q, params):
        row = {"ts_utc": ts, "kind": k, "team": team, "stage": stage,
               "market_slug": slug, "pm_mid": mid, "model_prob": mdl}
        out.setdefault(_market_key(row), []).append(row)
    return out


def _parse_ts(ts):
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _convergence_from_groups(groups: Dict[str, List[Dict[str, object]]]) -> List[Dict[str, object]]:
    """Shared entry-vs-latest reducer used by both the DB and JSONL paths."""
    out = []
    for snaps in groups.values():
        snaps = sorted(snaps, key=lambda s: str(s["ts_utc"]))
        if len(snaps) < 2:
            continue
        entry, later = snaps[0], snaps[-1]
        if entry.get("model_prob") is None or entry.get("pm_mid") is None or later.get("pm_mid") is None:
            continue
        te, tl = _parse_ts(entry["ts_utc"]), _parse_ts(later["ts_utc"])
        span = ((tl - te).total_seconds() / 3600.0) if (te and tl) else None
        out.append({
            "team": entry.get("team"), "stage": entry.get("stage"), "kind": entry.get("kind"),
            "entry_pm": float(entry["pm_mid"]), "later_pm": float(later["pm_mid"]),
            "model": float(entry["model_prob"]), "n_snaps": len(snaps),
            "span_hours": (round(span, 2) if span is not None else None),
        })
    return out


def convergence_inputs(con: sqlite3.Connection, *, kind: Optional[str] = None) -> List[Dict[str, object]]:
    """Per market with >=2 snapshots: the entry (earliest) and latest marks.

    Returns rows ``{team, stage, kind, entry_pm, later_pm, model, n_snaps,
    span_hours}`` ready for :func:`wca.outrightedge.convergence`. The model side
    is the model probability captured AT ENTRY (no lookahead). Markets with fewer
    than two snapshots, or no entry model probability, are omitted.
    """
    return _convergence_from_groups(trajectory(con, kind=kind))


# ---------------------------------------------------------------------------
# JSONL store — a portable, versioned historical dataset (CI / mini-independent)
# ---------------------------------------------------------------------------


def append_jsonl(path: str, rows: Sequence[Dict[str, object]], ts_utc: str) -> int:
    """Append captured PM quotes to a JSONL history file (one record per line).

    Mirrors :func:`append_snapshots` but to a versioned text dataset so the
    history accrues in the repo even when the canonical DB (mini) is offline.
    """
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            try:
                mid = float(r.get("pm_mid"))
            except (TypeError, ValueError):
                continue
            if not (0.0 <= mid <= 1.0):
                continue
            rec = {"ts_utc": ts_utc, "kind": str(r.get("kind")), "team": r.get("team"),
                   "stage": r.get("stage"), "market_slug": r.get("market_slug"),
                   "token_id": r.get("token_id"), "pm_mid": mid,
                   "model_prob": (float(r["model_prob"]) if r.get("model_prob") is not None else None)}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            n += 1
    return n


def load_records(path: str) -> List[Dict[str, object]]:
    """Load all snapshot records from a JSONL history file (missing file -> [])."""
    import os
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def convergence_inputs_from_records(records: Sequence[Dict[str, object]], *, kind: Optional[str] = None
                                    ) -> List[Dict[str, object]]:
    """:func:`convergence_inputs` over JSONL records instead of the DB."""
    groups: Dict[str, List[Dict[str, object]]] = {}
    for r in records:
        if kind is not None and r.get("kind") != kind:
            continue
        groups.setdefault(_market_key(r), []).append(r)
    return _convergence_from_groups(groups)
