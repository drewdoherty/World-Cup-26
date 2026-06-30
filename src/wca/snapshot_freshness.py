"""Staleness guard for ``odds_snapshots`` reads.

Several call sites take the newest ``odds_snapshots`` row (``SELECT MAX(ts_utc)``)
and price EV / CLV off it *without any age check* — so a feed that silently
stopped updating would keep producing confidently-wrong edges off prices that are
hours or days old.  This module centralises a tiny, dependency-free freshness
check those sites can share.

The check is deliberately conservative and side-effect-light: it parses the
snapshot timestamp, compares it to ``now`` against a max-age threshold, logs a
single WARNING when stale, and returns a small :class:`SnapshotFreshness` result
the caller uses to flag or skip the snapshot.  It never raises — an unparseable
or missing timestamp is treated as *stale* (fail-safe: better to skip than to
price off an unknown-age snapshot).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

#: Default freshness window for odds snapshots.  Live odds move fast; anything
#: older than this is treated as stale for EV/CLV purposes.
DEFAULT_MAX_AGE_HOURS: float = 6.0


def parse_snapshot_ts(ts_utc: Optional[str]) -> Optional[datetime]:
    """Parse an ``odds_snapshots`` timestamp to an aware UTC datetime, or None.

    Handles the daemon's microsecond+offset form
    (``2026-06-23T06:52:27.484258+00:00``), a trailing ``Z``, and the bare
    second-resolution form.  Returns ``None`` (never raises) when unparseable.
    """
    if not ts_utc:
        return None
    text = str(ts_utc).strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        # Fall back to the bare second-resolution prefix.
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class SnapshotFreshness:
    """Outcome of a snapshot freshness check."""

    is_stale: bool
    age_hours: Optional[float]      # None when the timestamp was unparseable
    max_age_hours: float
    ts_utc: Optional[str]


def check_snapshot_freshness(
    ts_utc: Optional[str],
    now: Optional[datetime] = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    *,
    context: str = "odds_snapshots",
    log: bool = True,
) -> SnapshotFreshness:
    """Return whether the newest snapshot ``ts_utc`` is too old to price off.

    Parameters
    ----------
    ts_utc:
        The newest snapshot timestamp (e.g. ``SELECT MAX(ts_utc)``).
    now:
        Reference time (aware UTC).  Defaults to the wall clock; callers that
        must stay deterministic should pass it explicitly.
    max_age_hours:
        Staleness threshold in hours (default :data:`DEFAULT_MAX_AGE_HOURS`).
    context:
        A short label for the WARNING log line (which call site / market).
    log:
        Emit a WARNING when stale (default True).

    A missing/unparseable timestamp is treated as **stale** (fail-safe).
    """
    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    dt = parse_snapshot_ts(ts_utc)
    if dt is None:
        if log:
            logger.warning(
                "STALE %s: no parseable snapshot timestamp (%r); "
                "treating as stale and skipping for EV/CLV.",
                context,
                ts_utc,
            )
        return SnapshotFreshness(True, None, max_age_hours, ts_utc)

    age_hours = (ref - dt).total_seconds() / 3600.0
    is_stale = age_hours > max_age_hours
    if is_stale and log:
        logger.warning(
            "STALE %s: newest snapshot is %.1fh old (>%.1fh threshold) "
            "[ts=%s]; skipping these prices for EV/CLV.",
            context,
            age_hours,
            max_age_hours,
            ts_utc,
        )
    return SnapshotFreshness(is_stale, age_hours, max_age_hours, ts_utc)
