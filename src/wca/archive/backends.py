"""Storage backends: local-first, with a pluggable cloud mirror.

The archive ALWAYS writes part files to the local directory tree (the source
of truth the mini and backtests read directly). A backend's only job is the
*optional* cloud mirror: :meth:`StorageBackend.upload` copies an
already-written local file up to object storage.

``make_backend`` is the load-bearing degrade path: with no cloud creds (or no
``boto3``), it returns a :class:`LocalBackend` whose ``upload`` is a no-op, so
everything keeps working against the local archive alone.
"""

from __future__ import annotations

import logging
from typing import Optional

from wca.archive.config import ArchiveConfig, CloudConfig

logger = logging.getLogger(__name__)


class StorageBackend:
    """Base backend: local-only, no cloud mirror."""

    has_cloud = False

    def upload(self, rel_path: str, abs_path: str) -> None:
        """Mirror a local file to cloud storage. No-op for local-only."""
        return None

    def describe(self) -> str:
        return "local"


class LocalBackend(StorageBackend):
    """Local filesystem only — the default, works with zero configuration."""


class S3Backend(StorageBackend):
    """Mirror part files to S3-compatible object storage (R2 / B2 / S3).

    boto3 is imported lazily and the client built on first use so importing
    this module never requires boto3. Upload failures are swallowed (logged at
    debug): the local archive remains authoritative, a flaky network must never
    break ingestion.
    """

    has_cloud = True

    def __init__(self, cloud: CloudConfig) -> None:
        self._cloud = cloud
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy: only needed when a cloud target is configured

            self._client = boto3.client(
                "s3",
                endpoint_url=self._cloud.endpoint_url,
                region_name=self._cloud.region,
                aws_access_key_id=self._cloud.access_key_id,
                aws_secret_access_key=self._cloud.secret_access_key,
            )
        return self._client

    def _key(self, rel_path: str) -> str:
        rel = rel_path.lstrip("/")
        return f"{self._cloud.prefix}/{rel}" if self._cloud.prefix else rel

    def upload(self, rel_path: str, abs_path: str) -> None:
        try:
            self._get_client().upload_file(abs_path, self._cloud.bucket, self._key(rel_path))
        except Exception as exc:  # noqa: BLE001 — cloud mirror is best-effort.
            logger.debug("archive cloud upload failed for %s: %s", rel_path, exc)

    def describe(self) -> str:
        target = self._cloud.endpoint_url or "s3"
        return "cloud[%s/%s/%s]" % (target, self._cloud.bucket, self._cloud.prefix or "")


def make_backend(config: ArchiveConfig) -> StorageBackend:
    """Pick a backend from config, degrading to local on any gap.

    Returns an :class:`S3Backend` only when cloud creds are complete AND boto3
    is importable; otherwise a :class:`LocalBackend`.
    """
    cloud = config.cloud
    if cloud is None or not cloud.is_complete():
        return LocalBackend()
    try:
        import boto3  # noqa: F401 — presence check only.
    except Exception:  # noqa: BLE001 — no boto3 => degrade to local.
        logger.info("archive: cloud creds present but boto3 missing; using local-only")
        return LocalBackend()
    return S3Backend(cloud)
