"""Configuration for the data-archival pipeline.

Everything is driven from environment variables so the archive can be turned
on, redirected, or pointed at cloud object storage without code changes. The
defaults make the LOCAL parquet path work out of the box — no creds required.

Env vars
--------
WCA_ARCHIVE_DIR
    Root directory for the local parquet archive. Default ``data/archive``.
WCA_ARCHIVE_ENABLED
    ``"0"``/``"false"`` disables every TEE hook (pure no-op). Default on.
WCA_ARCHIVE_S3_BUCKET / _ENDPOINT / _ACCESS_KEY_ID / _SECRET_ACCESS_KEY
WCA_ARCHIVE_S3_REGION / _PREFIX
    Optional S3-compatible object storage (Cloudflare R2, Backblaze B2, AWS
    S3). When the bucket + both keys are present AND ``boto3`` is importable,
    each written part file is mirrored to the bucket. Missing any of these =>
    the archive silently degrades to local-only. Secrets stay in the env /
    ``.env`` file; they are never written into the archive itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Local part files are snappy-compressed: fast, splittable, the de-facto
# default for analytic parquet. Kept as a constant so every writer agrees.
COMPRESSION = "snappy"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass(frozen=True)
class CloudConfig:
    """S3-compatible object-storage target (R2 / B2 / S3). All-or-nothing."""

    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint_url: Optional[str] = None  # None => real AWS S3
    region: Optional[str] = None
    prefix: str = ""  # key prefix inside the bucket, e.g. "wca/archive"

    def is_complete(self) -> bool:
        return bool(self.bucket and self.access_key_id and self.secret_access_key)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Optional[CloudConfig]":
        e = os.environ if env is None else env
        bucket = (e.get("WCA_ARCHIVE_S3_BUCKET") or "").strip()
        akid = (e.get("WCA_ARCHIVE_S3_ACCESS_KEY_ID") or "").strip()
        secret = (e.get("WCA_ARCHIVE_S3_SECRET_ACCESS_KEY") or "").strip()
        if not (bucket and akid and secret):
            return None
        return cls(
            bucket=bucket,
            access_key_id=akid,
            secret_access_key=secret,
            endpoint_url=(e.get("WCA_ARCHIVE_S3_ENDPOINT") or "").strip() or None,
            region=(e.get("WCA_ARCHIVE_S3_REGION") or "").strip() or None,
            prefix=(e.get("WCA_ARCHIVE_S3_PREFIX") or "").strip().strip("/"),
        )


@dataclass(frozen=True)
class ArchiveConfig:
    """Resolved archive configuration."""

    root: str = "data/archive"
    enabled: bool = True
    compression: str = COMPRESSION
    cloud: Optional[CloudConfig] = None

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "ArchiveConfig":
        e = os.environ if env is None else env
        root = (e.get("WCA_ARCHIVE_DIR") or "").strip() or "data/archive"
        enabled = (e.get("WCA_ARCHIVE_ENABLED") or "1").strip().lower() not in _FALSE
        return cls(root=root, enabled=enabled, cloud=CloudConfig.from_env(e))
