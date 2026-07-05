"""Medallion storage: immutable raw payloads → bronze/silver/gold Parquet.

RAW    ``data/raw/<source>/<endpoint>/<utc-ts>__<params-hash>.json.gz`` plus a
       ``.meta.json`` sidecar (endpoint, params with secrets redacted, subset
       of response headers, HTTP status, retrieval time, payload sha256).
       Raw files are never modified — a re-pull writes a new snapshot. The
       raw layer doubles as the offline cache: ``latest_raw()`` returns the
       newest snapshot for an endpoint+params signature.

BRONZE/SILVER/GOLD  Parquet (zstd) written by Polars, one dataset per file
       (or hive partition dir); every write is recorded in
       ``data/catalog.parquet`` — the lineage table (dataset, layer, rows,
       schema hash, built_utc, inputs, notebook).

All notebooks read/write ONLY through these helpers so the lineage table is
complete by construction.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import polars as pl

import lib.bootstrap as bt

CATALOG = bt.DATA_DIR / "catalog.parquet"
_REDACT = ("apiKey", "api_key", "key", "token", "authorization", "signature")


def _params_hash(params: Optional[Dict[str, Any]]) -> str:
    clean = {k: v for k, v in (params or {}).items() if k not in _REDACT}
    return hashlib.sha256(
        json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:10]


def redact(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Secrets replaced with '***' — safe to persist and display."""
    return {k: ("***" if k in _REDACT else v) for k, v in (params or {}).items()}


# ---------------------------------------------------------------------------
# RAW layer
# ---------------------------------------------------------------------------

def write_raw(source: str, endpoint: str, payload: Any, *,
              params: Optional[Dict[str, Any]] = None,
              status: Optional[int] = None,
              headers: Optional[Dict[str, str]] = None,
              url: Optional[str] = None) -> str:
    """Persist one immutable API payload; returns the snapshot_id."""
    ts = bt.utcnow_iso().replace(":", "").replace("-", "")
    ep_dir = bt.RAW_DIR / source / endpoint.strip("/").replace("/", "_")
    ep_dir.mkdir(parents=True, exist_ok=True)
    snap_id = f"{ts}__{_params_hash(params)}"
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    (ep_dir / f"{snap_id}.json.gz").write_bytes(gzip.compress(body))
    keep = {k.lower(): v for k, v in (headers or {}).items()
            if k.lower() in ("x-requests-remaining", "x-requests-used",
                             "x-requests-last", "content-type", "date")}
    meta = {
        "source": source, "endpoint": endpoint, "url": url,
        "params": redact(params), "params_hash": _params_hash(params),
        "status": status, "headers": keep,
        "retrieved_utc": bt.utcnow_iso(),
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "payload_bytes": len(body),
    }
    (ep_dir / f"{snap_id}.meta.json").write_text(json.dumps(meta, indent=2))
    return f"{source}/{endpoint.strip('/').replace('/', '_')}/{snap_id}"


def read_raw(snapshot_id: str) -> Any:
    p = bt.RAW_DIR / f"{snapshot_id}.json.gz"
    return json.loads(gzip.decompress(p.read_bytes()))


def raw_meta(snapshot_id: str) -> Dict[str, Any]:
    return json.loads((bt.RAW_DIR / f"{snapshot_id}.meta.json").read_text())


def latest_raw(source: str, endpoint: str,
               params: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Newest snapshot_id for endpoint(+params sig); None if never pulled."""
    ep_dir = bt.RAW_DIR / source / endpoint.strip("/").replace("/", "_")
    if not ep_dir.exists():
        return None
    sig = _params_hash(params) if params is not None else None
    cands = sorted(f.stem[:-5] if f.stem.endswith(".meta") else f.stem
                   for f in ep_dir.glob("*.json.gz"))
    cands = [c.replace(".json", "") for c in cands]
    if sig is not None:
        cands = [c for c in cands if c.endswith(f"__{sig}")]
    if not cands:
        return None
    return f"{source}/{endpoint.strip('/').replace('/', '_')}/{cands[-1]}"


def list_raw() -> pl.DataFrame:
    """Inventory of every raw snapshot (from the .meta.json sidecars)."""
    rows = []
    for meta_file in sorted(bt.RAW_DIR.rglob("*.meta.json")):
        m = json.loads(meta_file.read_text())
        rows.append({
            "source": m.get("source"), "endpoint": m.get("endpoint"),
            "snapshot": meta_file.stem.replace(".meta", ""),
            "retrieved_utc": m.get("retrieved_utc"),
            "status": m.get("status"),
            "payload_kb": round((m.get("payload_bytes") or 0) / 1024, 1),
        })
    schema = {"source": pl.Utf8, "endpoint": pl.Utf8, "snapshot": pl.Utf8,
              "retrieved_utc": pl.Utf8, "status": pl.Int64, "payload_kb": pl.Float64}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


# ---------------------------------------------------------------------------
# Parquet layers + lineage catalog
# ---------------------------------------------------------------------------
_LAYER_DIRS = {"bronze": bt.BRONZE_DIR, "silver": bt.SILVER_DIR,
               "gold": bt.GOLD_DIR}


def dataset_path(layer: str, name: str) -> Path:
    return _LAYER_DIRS[layer] / f"{name}.parquet"


def save_dataset(df: pl.DataFrame, layer: str, name: str, *,
                 inputs: Sequence[str] = (), notebook: str = "",
                 note: str = "") -> Path:
    """Write Parquet (zstd) + append a lineage row to the catalog."""
    if layer not in _LAYER_DIRS:
        raise ValueError(f"layer must be one of {sorted(_LAYER_DIRS)}")
    path = dataset_path(layer, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path, compression="zstd")
    schema_hash = hashlib.sha256(
        json.dumps({c: str(t) for c, t in df.schema.items()},
                   sort_keys=True).encode()).hexdigest()[:12]
    try:
        rel = str(path.relative_to(bt.JB_ROOT))
    except ValueError:          # redirected dirs (tests) live outside JB_ROOT
        rel = str(path)
    entry = pl.DataFrame([{
        "dataset": name, "layer": layer, "path": rel,
        "rows": df.height, "cols": df.width, "schema_hash": schema_hash,
        "built_utc": bt.utcnow_iso(), "inputs": json.dumps(list(inputs)),
        "notebook": notebook, "note": note,
        "file_sha256": bt.sha256_file(path),
    }])
    if CATALOG.exists():
        cat = pl.read_parquet(CATALOG)
        cat = pl.concat([cat, entry], how="vertical_relaxed")
    else:
        cat = entry
    cat.write_parquet(CATALOG)
    return path


def load_dataset(layer: str, name: str, *, lazy: bool = False):
    """Read a dataset back (LazyFrame when lazy=True for big scans)."""
    path = dataset_path(layer, name)
    if not path.exists():
        raise FileNotFoundError(
            f"{layer}/{name} not built yet — run the earlier notebook that "
            f"produces it (see the data catalog in notebook 00).")
    return pl.scan_parquet(path) if lazy else pl.read_parquet(path)


def catalog(latest_only: bool = True) -> pl.DataFrame:
    """The lineage table. latest_only keeps the newest build per dataset."""
    if not CATALOG.exists():
        return pl.DataFrame(schema={"dataset": pl.Utf8, "layer": pl.Utf8,
                                    "path": pl.Utf8, "rows": pl.Int64,
                                    "cols": pl.Int64, "schema_hash": pl.Utf8,
                                    "built_utc": pl.Utf8, "inputs": pl.Utf8,
                                    "notebook": pl.Utf8, "note": pl.Utf8,
                                    "file_sha256": pl.Utf8})
    cat = pl.read_parquet(CATALOG)
    if latest_only:
        cat = (cat.sort("built_utc")
                  .group_by(["layer", "dataset"], maintain_order=True).last())
    return cat.sort(["layer", "dataset"])


# ---------------------------------------------------------------------------
# Frame inspection — the standard "show your work" block for notebooks
# ---------------------------------------------------------------------------

def profile_frame(df: pl.DataFrame, name: str = "", *,
                  unique_keys: Optional[List[str]] = None):
    """Schema, row count, null rates and (optionally) key-uniqueness — as a
    pandas DataFrame for display. This is the transparency contract: call it
    on every intermediate frame that matters."""
    import pandas as pd
    rows = []
    n = df.height
    for col, dtype in df.schema.items():
        nulls = int(df[col].null_count()) if n else 0
        rows.append({"column": col, "dtype": str(dtype),
                     "null_rate": round(nulls / n, 4) if n else 0.0})
    prof = pd.DataFrame(rows)
    prof.attrs["name"] = name
    prof.attrs["rows"] = n
    if unique_keys:
        dup = n - df.select(unique_keys).unique().height
        prof.attrs["duplicate_keys"] = dup
    return prof


def to_pandas(df: pl.DataFrame, reason: str):
    """Explicit Polars→Pandas conversion. `reason` is required by design —
    every conversion in a notebook states why (display, matplotlib, .style,
    scikit interop...). Polars stays the engine for heavy transforms."""
    assert reason, "state why this conversion is needed"
    return df.to_pandas()
