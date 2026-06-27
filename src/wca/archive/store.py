"""ArchiveStore: append-only, partitioned parquet with a manifest + idempotency.

Layout (hive-style, self-describing part files)::

    <root>/<dataset>/date=YYYY-MM-DD/venue=<v>/market=<m>/part-<ns>-<key8>.parquet
    <root>/<dataset>/_manifest.jsonl

Each ``write_*`` call carries a ``dedup_key`` (a content hash). The key set per
dataset is read from the manifest once and cached; a repeat key is skipped
whole, so re-archiving an identical payload is a no-op (append idempotency).

pyarrow is imported lazily inside the write methods so that merely importing
this module — or the TEE hooks that call it — never requires pyarrow. If
pyarrow is missing the call raises and the TEE layer swallows it (degrade to
nothing; betting is never affected).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from wca.archive.config import ArchiveConfig
from wca.archive import schemas
from wca.archive.backends import StorageBackend, make_backend

logger = logging.getLogger(__name__)

_PART_COUNTER = itertools.count()
_SAFE = re.compile(r"[^0-9a-zA-Z._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing + verbatim storage."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date_of(ts: Optional[str]) -> str:
    if ts and len(ts) >= 10 and ts[4] == "-" and ts[7] == "-":
        return ts[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _part(value: Any, default: str = "unknown") -> str:
    """Filesystem-safe partition token."""
    s = str(value).strip() if value is not None else ""
    if not s:
        return default
    s = _SAFE.sub("_", s).strip("_.")
    return (s[:60] or default).lower()


def _s(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        import math

        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


class ArchiveStore:
    """Owns the local archive tree + an optional cloud-mirror backend."""

    def __init__(self, config: ArchiveConfig, backend: Optional[StorageBackend] = None) -> None:
        self.config = config
        self.backend = backend if backend is not None else make_backend(config)
        self._seen_cache: Dict[str, set] = {}

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "ArchiveStore":
        return cls(ArchiveConfig.from_env(env))

    # -- paths -------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.config.root)

    def _dataset_dir(self, dataset: str) -> Path:
        return self.root / dataset

    def _manifest_path(self, dataset: str) -> Path:
        return self._dataset_dir(dataset) / "_manifest.jsonl"

    # -- idempotency -------------------------------------------------------
    def _seen(self, dataset: str) -> set:
        cached = self._seen_cache.get(dataset)
        if cached is not None:
            return cached
        seen: set = set()
        mpath = self._manifest_path(dataset)
        if mpath.exists():
            with mpath.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        key = json.loads(line).get("dedup_key")
                    except ValueError:
                        continue
                    if key:
                        seen.add(key)
        self._seen_cache[dataset] = seen
        return seen

    # -- core writer -------------------------------------------------------
    def write_table(
        self,
        dataset: str,
        rows: List[Dict[str, Any]],
        fields: List,
        dedup_key: Optional[str] = None,
    ) -> List[str]:
        """Append ``rows`` to ``dataset`` as partitioned parquet. Idempotent
        on ``dedup_key``. Returns the relative paths of the part files written
        (empty if nothing was written / deduped)."""
        if not rows:
            return []
        if dedup_key is not None and dedup_key in self._seen(dataset):
            return []

        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = schemas.build_schema(fields)
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for row in rows:
            key = (
                _part(row.get("date"), default=_date_of(None)),
                _part(row.get("venue")),
                _part(row.get("market")),
            )
            groups.setdefault(key, []).append(row)

        written: List[str] = []
        manifest_entries: List[Dict[str, Any]] = []
        key8 = (dedup_key or _sha256(_canonical_json(rows)))[:8]
        for (date, venue, market), grp in groups.items():
            part_dir = (
                self._dataset_dir(dataset)
                / f"date={date}"
                / f"venue={venue}"
                / f"market={market}"
            )
            part_dir.mkdir(parents=True, exist_ok=True)
            fname = f"part-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{next(_PART_COUNTER)}-{key8}.parquet"
            abs_path = part_dir / fname
            table = pa.Table.from_pylist(grp, schema=schema)
            pq.write_table(table, str(abs_path), compression=self.config.compression)
            rel = str(abs_path.relative_to(self.root))
            written.append(rel)
            manifest_entries.append(
                {
                    "ts": _now_iso(),
                    "dataset": dataset,
                    "dedup_key": dedup_key,
                    "path": rel,
                    "rows": len(grp),
                    "partition": {"date": date, "venue": venue, "market": market},
                }
            )

        self._append_manifest(dataset, manifest_entries)
        if dedup_key is not None:
            self._seen(dataset).add(dedup_key)
        # Best-effort cloud mirror (part files + the updated manifest).
        if self.backend.has_cloud:
            for rel in written:
                self.backend.upload(f"{dataset}/{rel}", str(self.root / rel))
            mrel = str(self._manifest_path(dataset).relative_to(self.root))
            self.backend.upload(mrel, str(self._manifest_path(dataset)))
        return written

    def _append_manifest(self, dataset: str, entries: List[Dict[str, Any]]) -> None:
        if not entries:
            return
        mpath = self._manifest_path(dataset)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        with mpath.open("a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, sort_keys=True) + "\n")

    # -- dataset-specific writers -----------------------------------------
    def write_raw(
        self,
        venue: str,
        market: str,
        payload: Any,
        kind: Optional[str] = None,
        ts_utc: Optional[str] = None,
    ) -> List[str]:
        """Archive one raw API payload verbatim (lossless, deduped by content)."""
        canonical = _canonical_json(payload)
        sha = _sha256(canonical)
        ts = ts_utc or _now_iso()
        row = {
            "ts_utc": ts,
            "date": _date_of(ts),
            "venue": _s(venue),
            "market": _s(market),
            "kind": _s(kind or market),
            "sha256": sha,
            "n_bytes": len(canonical.encode("utf-8")),
            "payload_json": canonical,
        }
        return self.write_table("raw", [row], schemas.RAW_FIELDS, dedup_key=sha)

    def write_odds(self, df: Any, venue: str, ts_utc: Optional[str] = None) -> List[str]:
        """Archive normalized 1X2/totals odds rows from a get_odds DataFrame."""
        ts = ts_utc or _now_iso()
        rows = _odds_rows(df, venue, ts)
        if not rows:
            return []
        dedup_key = _sha256(venue + "|" + ts + "|" + _canonical_json(rows))
        return self.write_table("odds", rows, schemas.ODDS_FIELDS, dedup_key=dedup_key)

    def write_model(self, payload: Dict[str, Any], ts_utc: Optional[str] = None) -> List[str]:
        """Archive model 1X2 predictions (normalized rows + verbatim raw)."""
        fixtures = (payload or {}).get("fixtures") or []
        if not fixtures:
            return []
        generated = ((payload.get("meta") or {}).get("generated")) or ts_utc or _now_iso()
        # Verbatim copy of the whole build (separate dataset, separate dedup).
        self.write_raw("model", "predictions", payload, kind="model_predictions", ts_utc=generated)
        rows: List[Dict[str, Any]] = []
        for fx in fixtures:
            m = fx.get("model") or {}
            rows.append(
                {
                    "ts_utc": generated,
                    "date": _date_of(generated),
                    "venue": "model",
                    "market": "predictions",
                    "match_id": _s(fx.get("match_id")),
                    "fixture": _s(fx.get("fixture")),
                    "kickoff": _s(fx.get("kickoff")),
                    "p_home": _f(m.get("home")),
                    "p_draw": _f(m.get("draw")),
                    "p_away": _f(m.get("away")),
                    "lambda_home": _f(fx.get("lambda_home")),
                    "lambda_away": _f(fx.get("lambda_away")),
                    "payload_json": _canonical_json(fx),
                }
            )
        dedup_key = _sha256(_canonical_json(payload))
        return self.write_table("model_predictions", rows, schemas.MODEL_FIELDS, dedup_key=dedup_key)

    def write_bets(self, bet_rows: List[Dict[str, Any]], snapshot_ts: str) -> List[str]:
        """Archive a point-in-time export of the ledger `bets` table."""
        if not bet_rows:
            return []
        date = _date_of(snapshot_ts)
        rows = []
        for b in bet_rows:
            rows.append(
                {
                    "snapshot_ts": snapshot_ts,
                    "date": date,
                    "venue": "ledger",
                    "market": "bets",
                    "id": int(b["id"]) if b.get("id") is not None else None,
                    "ts_utc": _s(b.get("ts_utc")),
                    "match_id": _s(b.get("match_id")),
                    "match_desc": _s(b.get("match_desc")),
                    "bet_market": _s(b.get("market")),
                    "selection": _s(b.get("selection")),
                    "platform": _s(b.get("platform")),
                    "decimal_odds": _f(b.get("decimal_odds")),
                    "stake": _f(b.get("stake")),
                    "model_prob": _f(b.get("model_prob")),
                    "market_prob_devig": _f(b.get("market_prob_devig")),
                    "ev": _f(b.get("ev")),
                    "kelly_fraction": _f(b.get("kelly_fraction")),
                    "status": _s(b.get("status")),
                    "settled_pl": _f(b.get("settled_pl")),
                    "closing_odds": _f(b.get("closing_odds")),
                    "clv": _f(b.get("clv")),
                    "notes": _s(b.get("notes")),
                    "manual_override": _s(b.get("manual_override")),
                }
            )
        dedup_key = _sha256("bets|" + snapshot_ts + "|" + str(len(rows)))
        return self.write_table("ledger_bets", rows, schemas.BETS_FIELDS, dedup_key=dedup_key)

    # -- introspection -----------------------------------------------------
    def info(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "root": str(self.root),
            "enabled": self.config.enabled,
            "backend": self.backend.describe(),
            "datasets": {},
        }
        for dataset in schemas.DATASETS:
            mpath = self._manifest_path(dataset)
            n = 0
            if mpath.exists():
                with mpath.open("r", encoding="utf-8") as fh:
                    n = sum(1 for ln in fh if ln.strip())
            out["datasets"][dataset] = {"part_files": n}
        return out


def _odds_rows(df: Any, venue: str, ts: str) -> List[Dict[str, Any]]:
    """Flatten a get_odds DataFrame into ODDS_FIELDS rows."""
    if df is None:
        return []
    try:
        if getattr(df, "empty", False):
            return []
        records = df.to_dict(orient="records")
    except Exception:  # noqa: BLE001 — not a frame we understand; skip silently.
        return []
    date = _date_of(ts)
    rows: List[Dict[str, Any]] = []
    for rec in records:
        event_id = rec.get("event_id")
        if event_id is None:
            continue
        rows.append(
            {
                "ts_utc": ts,
                "date": date,
                "venue": _s(venue),
                "market": _s(rec.get("market")),
                "event_id": _s(event_id),
                "commence_time": _s(rec.get("commence_time")),
                "home_team": _s(rec.get("home_team")),
                "away_team": _s(rec.get("away_team")),
                "bookmaker_key": _s(rec.get("bookmaker_key")),
                "selection": _s(rec.get("outcome_name")),
                "point": _f(rec.get("outcome_point")),
                "decimal_odds": _f(rec.get("decimal_odds")),
            }
        )
    return rows
