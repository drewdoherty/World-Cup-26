"""Stable parquet schemas + partitioning for each archived dataset.

Field lists are plain Python (no pyarrow import at module load) so that
importing :mod:`wca.archive` never drags in pyarrow — the heavy dependency is
imported lazily by the store, and the TEE hooks degrade to a no-op if it is
absent. :func:`build_schema` materialises a ``pyarrow.Schema`` on demand.

Every dataset is partitioned hive-style by ``date`` / ``venue`` / ``market``
(see :data:`PARTITION_COLS`). Partition columns are ALSO kept in-file so each
part file is self-describing: read the archive with
``pyarrow.dataset.dataset(path)`` (partitioning=None, the default) and the
directory names are ignored — the in-file columns drive every filter.

Schema stability is load-bearing for backtests: a fixed, explicitly-typed
schema means appends made months apart concatenate without column drift, and
``from_pylist(rows, schema=...)`` coerces/fills so a varying payload can never
change the on-disk shape.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# Logical type tokens -> resolved to pyarrow types lazily in build_schema.
_STR = "str"
_F64 = "f64"
_I64 = "i64"

# Uniform partition columns across every dataset.
PARTITION_COLS: Tuple[str, str, str] = ("date", "venue", "market")

# Raw, lossless API payloads — one row per fetched payload, JSON kept verbatim.
RAW_FIELDS: List[Tuple[str, str]] = [
    ("ts_utc", _STR),       # capture time (ISO-8601 UTC)
    ("date", _STR),         # partition: UTC date YYYY-MM-DD
    ("venue", _STR),        # partition: source (oddsapi|polymarket|betfair|model)
    ("market", _STR),       # partition: endpoint / market / method
    ("kind", _STR),         # finer sub-label within the venue
    ("sha256", _STR),       # content hash of payload_json (idempotency key)
    ("n_bytes", _I64),      # size of payload_json
    ("payload_json", _STR), # canonical JSON of the raw payload
]

# Normalized 1X2 / totals odds rows flattened from a get_odds DataFrame.
ODDS_FIELDS: List[Tuple[str, str]] = [
    ("ts_utc", _STR),
    ("date", _STR),
    ("venue", _STR),         # source label
    ("market", _STR),        # h2h / totals / btts / ...
    ("event_id", _STR),
    ("commence_time", _STR),
    ("home_team", _STR),
    ("away_team", _STR),
    ("bookmaker_key", _STR),
    ("selection", _STR),
    ("point", _F64),
    ("decimal_odds", _F64),
]

# Normalized model 1X2 predictions, one row per fixture per build.
MODEL_FIELDS: List[Tuple[str, str]] = [
    ("ts_utc", _STR),
    ("date", _STR),
    ("venue", _STR),         # always "model"
    ("market", _STR),        # "predictions"
    ("match_id", _STR),
    ("fixture", _STR),
    ("kickoff", _STR),
    ("p_home", _F64),
    ("p_draw", _F64),
    ("p_away", _F64),
    ("lambda_home", _F64),
    ("lambda_away", _F64),
    ("payload_json", _STR),  # the full fixture row, verbatim
]

# Point-in-time export of the ledger `bets` table for backtests.
BETS_FIELDS: List[Tuple[str, str]] = [
    ("snapshot_ts", _STR),   # when this export was taken
    ("date", _STR),          # partition: export UTC date
    ("venue", _STR),         # "ledger"
    ("market", _STR),        # "bets"
    ("id", _I64),
    ("ts_utc", _STR),
    ("match_id", _STR),
    ("match_desc", _STR),
    ("bet_market", _STR),    # the bet's own market (renamed to avoid the partition col)
    ("selection", _STR),
    ("platform", _STR),
    ("decimal_odds", _F64),
    ("stake", _F64),
    ("model_prob", _F64),
    ("market_prob_devig", _F64),
    ("ev", _F64),
    ("kelly_fraction", _F64),
    ("status", _STR),
    ("settled_pl", _F64),
    ("closing_odds", _F64),
    ("clv", _F64),
    ("notes", _STR),
    ("manual_override", _STR),
]

DATASETS: Dict[str, List[Tuple[str, str]]] = {
    "raw": RAW_FIELDS,
    "odds": ODDS_FIELDS,
    "model_predictions": MODEL_FIELDS,
    "ledger_bets": BETS_FIELDS,
}


def build_schema(fields: List[Tuple[str, str]]) -> Any:
    """Materialise a ``pyarrow.Schema`` from a field list (lazy pyarrow import)."""
    import pyarrow as pa

    types = {_STR: pa.string(), _F64: pa.float64(), _I64: pa.int64()}
    return pa.schema([(name, types[tok]) for name, tok in fields])
