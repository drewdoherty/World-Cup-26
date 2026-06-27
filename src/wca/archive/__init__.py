"""World Cup Alpha data-archival pipeline.

Durably stores everything ingested — raw API payloads, normalized odds, model
predictions, and point-in-time ledger snapshots — as partitioned, snappy
parquet so betting speed/accuracy can be benchmarked as volume grows and
strategies can be backtested against exactly what was seen at decision time.

Design + query guide: ``docs/research/data_archival.md``.

Public surface
--------------
* :mod:`wca.archive.tee` — safe TEE hooks called from the live ingestion path
  (``tee.raw`` / ``tee.odds_frame`` / ``tee.model_payload``). Never raise.
* :class:`~wca.archive.store.ArchiveStore` — the parquet writer.
* :func:`~wca.archive.ledger.snapshot_ledger` — DB-copy + bets export job.
* :class:`~wca.archive.config.ArchiveConfig` — env-driven configuration.

Importing this package does NOT import pyarrow; the heavy dependency is loaded
lazily by the store so the TEE hooks degrade to a no-op if it is absent.
"""

from __future__ import annotations

from wca.archive.config import ArchiveConfig, CloudConfig

__all__ = ["ArchiveConfig", "CloudConfig", "tee"]

from wca.archive import tee  # noqa: E402  (re-export; light module, no pyarrow)
