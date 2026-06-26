"""Safe TEE entry points wired into the live ingestion path.

These are the ONLY functions the odds / Polymarket / Betfair clients and the
model-prediction writer call. Every one is a guaranteed-safe no-op on failure:

* gated on ``WCA_ARCHIVE_ENABLED`` (off => instant return);
* the whole body is wrapped so it can NEVER raise into the caller — a missing
  pyarrow, a full disk, a malformed payload, anything is swallowed and logged
  at debug. Archiving is additive and must never change betting behavior.

A single process-wide :class:`~wca.archive.store.ArchiveStore` is built lazily
from the environment and reused.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_STORE = None
_INIT_DONE = False


def _store():
    """Lazily build (once) the default store from the environment.

    Returns ``None`` when archiving is disabled or the store cannot be built —
    callers must treat ``None`` as "do nothing".
    """
    global _STORE, _INIT_DONE
    if _INIT_DONE:
        return _STORE
    _INIT_DONE = True
    try:
        from wca.archive.config import ArchiveConfig
        from wca.archive.store import ArchiveStore

        config = ArchiveConfig.from_env()
        if not config.enabled:
            _STORE = None
        else:
            _STORE = ArchiveStore(config)
    except Exception as exc:  # noqa: BLE001 — never block ingestion.
        logger.debug("archive disabled (init failed): %s", exc)
        _STORE = None
    return _STORE


def reset() -> None:
    """Drop the cached store so the next call re-reads the environment (tests)."""
    global _STORE, _INIT_DONE
    _STORE = None
    _INIT_DONE = False


def raw(venue: str, market: str, payload: Any, kind: Optional[str] = None,
        ts_utc: Optional[str] = None) -> None:
    """TEE a raw API payload to the archive. Never raises."""
    store = _store()
    if store is None:
        return
    try:
        store.write_raw(venue, market, payload, kind=kind, ts_utc=ts_utc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("archive raw tee failed (%s/%s): %s", venue, market, exc)


def odds_frame(df: Any, venue: str, ts_utc: Optional[str] = None) -> None:
    """TEE normalized odds rows from a get_odds DataFrame. Never raises."""
    store = _store()
    if store is None:
        return
    try:
        store.write_odds(df, venue, ts_utc=ts_utc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("archive odds tee failed (%s): %s", venue, exc)


def model_payload(payload: Any, ts_utc: Optional[str] = None) -> None:
    """TEE model 1X2 predictions. Never raises."""
    store = _store()
    if store is None:
        return
    try:
        store.write_model(payload, ts_utc=ts_utc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("archive model tee failed: %s", exc)
