"""Adaptive, budget-aware odds-polling decision logic.

This module is *pure*: every function takes the wall clock as an injected
ISO-8601 string so behaviour is fully deterministic and unit-testable with
no network and no real clock.  The daemon (``scripts/wca_snapshotd.py``) is
the only thing that touches time, env and the network; it leans on the
functions here to decide *when* to poll next.

Cadence tiers (fastest to slowest):

* **in-game**   -- a match is currently live: poll often to track the line.
* **pre-close** -- a kickoff is imminent (within 10 minutes): poll to
  capture the closing line, which is the single most valuable price.
* **idle**      -- nothing is happening: poll slowly to conserve credits.

Budget guards layer on top of the cadence so we never run the monthly API
quota dry, while treating closing-line polls as (nearly) sacred.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# A kickoff is considered "imminent" (pre-close window) when it is at most
# this many seconds in the future.
_PRE_CLOSE_WINDOW_SECONDS = 10 * 60

# Below this quota we slow down idle polling but still allow pre-close polls.
_LOW_QUOTA_THRESHOLD = 200


@dataclass
class PollPolicy:
    """Tunable cadence + budget parameters for the polling daemon."""

    in_game_seconds: int = 180
    pre_close_seconds: int = 300
    idle_seconds: int = 3600
    low_quota_idle_seconds: int = 10800
    # Credits we refuse to spend; once quota dips below this we stop polling
    # entirely (including closing lines) to preserve a hard reserve.
    min_reserve: int = 60
    # How long after kickoff a match is still considered "live".
    match_duration_minutes: int = 130


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    A naive timestamp (no offset) is interpreted as UTC.  Anything that
    fails to parse returns ``None`` so callers can skip it.
    """
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    # ``datetime.fromisoformat`` (3.9) does not accept a trailing "Z".
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_kickoffs(kickoffs: List[str]) -> List[datetime]:
    """Parse a list of ISO kickoff strings, silently dropping malformed ones."""
    out: List[datetime] = []
    for ko in kickoffs or []:
        dt = _parse_iso(ko)
        if dt is not None:
            out.append(dt)
    return out


def next_poll_delay(
    now_utc: str,
    kickoffs: List[str],
    quota_remaining: Optional[int],
    policy: PollPolicy,
) -> Tuple[int, str]:
    """Decide how long to wait before the next odds poll.

    Parameters
    ----------
    now_utc:
        Current wall-clock time as an ISO-8601 string (naive == UTC).
    kickoffs:
        Kickoff times (ISO-8601 strings) for fixtures of interest.  Malformed
        entries are skipped.
    quota_remaining:
        Credits left on the odds-API plan, or ``None`` if unknown.
    policy:
        :class:`PollPolicy` with the cadence + budget knobs.

    Returns
    -------
    ``(delay_seconds, reason)`` -- how long to sleep and a short label
    explaining which rule fired.
    """
    now = _parse_iso(now_utc)
    if now is None:
        # Without a usable clock we cannot reason about cadence; fall back to
        # idle so the daemon keeps ticking slowly rather than busy-looping.
        return policy.idle_seconds, "idle"

    parsed = _parse_kickoffs(kickoffs)

    match_duration = timedelta(minutes=policy.match_duration_minutes)
    pre_close_window = timedelta(seconds=_PRE_CLOSE_WINDOW_SECONDS)

    any_live = False
    any_pre_close = False
    for ko in parsed:
        if ko <= now < ko + match_duration:
            any_live = True
        # A kickoff that is in the future but within the pre-close window.
        if now < ko <= now + pre_close_window:
            any_pre_close = True

    # --- Determine the base cadence tier ----------------------------------
    if any_live:
        base_delay, reason = policy.in_game_seconds, "in_game"
    elif any_pre_close:
        base_delay, reason = policy.pre_close_seconds, "pre_close"
    else:
        base_delay, reason = policy.idle_seconds, "idle"

    # --- Budget guards (layered on top of cadence) ------------------------
    if quota_remaining is not None:
        # Hard reserve: below this we stop spending entirely, even on the
        # otherwise-sacred closing line.
        if quota_remaining < policy.min_reserve:
            return policy.low_quota_idle_seconds, "quota-reserve"

        # Low quota: throttle everything *except* the pre-close window.
        # Closing lines are the highest-value polls, so we keep capturing
        # them as long as we are above the hard reserve.
        if quota_remaining < _LOW_QUOTA_THRESHOLD and reason != "pre_close":
            if policy.low_quota_idle_seconds > base_delay:
                return policy.low_quota_idle_seconds, "low_quota_idle"

    return base_delay, reason


def estimate_monthly_calls(
    n_matches_per_day: float,
    policy: PollPolicy,
) -> Dict[str, float]:
    """Rough planning estimate of API calls over a 30-day window.

    This is intentionally approximate -- it exists for documentation and
    log lines, not for precise budgeting.  It assumes:

    * Idle polling runs continuously in the background (``idle_seconds``
      cadence over the whole 30 days), and
    * Each match adds a burst of in-game polling for the duration of the
      match (``match_duration_minutes`` at ``in_game_seconds`` cadence).

    Returns
    -------
    ``{"idle_calls", "in_game_calls", "total"}`` for the 30-day horizon.
    """
    days = 30.0
    seconds_per_day = 86400.0

    idle_calls = (days * seconds_per_day) / float(policy.idle_seconds)

    match_seconds = policy.match_duration_minutes * 60.0
    polls_per_match = match_seconds / float(policy.in_game_seconds)
    in_game_calls = polls_per_match * float(n_matches_per_day) * days

    return {
        "idle_calls": idle_calls,
        "in_game_calls": in_game_calls,
        "total": idle_calls + in_game_calls,
    }
