"""File-backed cache for the latest formatted matchday card.

The bot can serve the most recent card instantly by reading from this cache
rather than re-running the full model pipeline on every request.

Design constraints
------------------
* ``write_card`` and ``read_card`` are *pure / deterministic* with respect to
  the clock — they never call ``datetime.now()`` internally.  Callers supply
  the current timestamp as a string so the functions are fully testable without
  monkeypatching.
* The on-disk format is a tiny single-line HTML comment header followed by the
  raw card body, making the file human-readable as-is.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_HEADER_PREFIX = "<!-- generated: "
_HEADER_SUFFIX = " -->"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_card(
    text: str,
    path: str = "data/card_latest.md",
    ts_utc: Optional[str] = None,
) -> None:
    """Write *text* to *path* with a generated-timestamp comment header.

    Parameters
    ----------
    text:
        The formatted card body (plain Markdown / Telegram Markdown).
    path:
        Destination file path.  Parent directories are created if missing.
    ts_utc:
        ISO-8601 timestamp string injected into the header comment.  Pass
        ``None`` (default) to leave the timestamp field empty — the header
        will still be written so ``read_card`` can reliably strip it.
    """
    timestamp = ts_utc if ts_utc is not None else ""
    header = _HEADER_PREFIX + timestamp + _HEADER_SUFFIX
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n" + text)


def read_card(
    path: str = "data/card_latest.md",
    now_utc: Optional[str] = None,
    max_age_hours: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Read a card written by :func:`write_card`.

    Parameters
    ----------
    path:
        Path to the cached card file.
    now_utc:
        The *current* time as an ISO-8601 string.  Used only for staleness
        calculation; pass ``None`` to skip staleness checking.
    max_age_hours:
        Maximum age in hours before the card is considered stale.  Staleness
        is only computed when *both* ``now_utc`` and ``max_age_hours`` are
        provided **and** the file contains a parseable generated timestamp.

    Returns
    -------
    ``None``
        If the file does not exist.
    dict
        ``{"text": str, "generated": Optional[str], "stale": bool}``

        * ``text`` — the card body with the header line stripped.
        * ``generated`` — the raw timestamp string from the header, or
          ``None`` if absent / empty.
        * ``stale`` — ``True`` only when staleness can be fully determined
          (both ``now_utc`` / ``max_age_hours`` supplied *and* a parseable
          ``generated`` timestamp exists); ``False`` in all other cases.
    """
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    # Split off the first line (header) from the body.
    parts = raw.split("\n", 1)
    header_line = parts[0] if parts else ""
    body = parts[1] if len(parts) > 1 else ""

    # Parse the generated timestamp from the header comment.
    generated: Optional[str] = None
    if header_line.startswith(_HEADER_PREFIX) and header_line.endswith(_HEADER_SUFFIX):
        ts_raw = header_line[len(_HEADER_PREFIX): -len(_HEADER_SUFFIX)]
        generated = ts_raw if ts_raw else None

    # Compute staleness only when all required pieces are available.
    stale = False
    if generated is not None and now_utc is not None and max_age_hours is not None:
        stale = _is_stale(generated, now_utc, max_age_hours)

    return {"text": body, "generated": generated, "stale": stale}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> Optional[float]:
    """Return a POSIX timestamp (float seconds) for an ISO-8601 string.

    Handles the common ``YYYY-MM-DDTHH:MM:SS`` and
    ``YYYY-MM-DDTHH:MM:SS+HH:MM`` / ``...Z`` forms without external deps.
    Returns ``None`` on parse failure.

    All forms are interpreted on a single, fixed UTC reference frame:

    * Naive timestamps (no offset) are treated as **UTC**, not local time.
    * ``Z`` and explicit ``+HH:MM`` / ``-HH:MM`` offsets are honoured.

    This makes the function fully deterministic — independent of the host's
    ``TZ`` — so a naive header written by ``write_card`` and a ``Z``-suffixed
    ``now_utc`` compare correctly.  (The previous implementation used
    ``time.mktime``/``time.timezone``, which silently read the machine
    timezone and put naive vs. offset strings on different frames.)
    """
    import calendar as _calendar
    import time as _time

    # Normalise trailing 'Z' to '+00:00'.
    ts_norm = ts.strip()
    if ts_norm.endswith("Z"):
        ts_norm = ts_norm[:-1] + "+00:00"

    # Try with an explicit UTC offset (+HH:MM / +HHMM / -HH:MM) first, so a
    # naive date like "2026-06-11" is not mistaken for an offset-bearing one.
    # Python 3.7+ datetime supports %z with a colon, but we parse manually to
    # avoid any platform/version quirks and to keep the UTC frame explicit.
    if len(ts_norm) > 10:
        for sign in ("+", "-"):
            idx = ts_norm.rfind(sign, 11)  # search past the date (YYYY-MM-DD)
            if idx == -1:
                continue
            dt_part = ts_norm[:idx]
            offset_part = ts_norm[idx + 1:]
            # Parse offset as HH:MM or HHMM.
            try:
                if ":" in offset_part:
                    oh, om = offset_part.split(":", 1)
                else:
                    oh, om = offset_part[:2], offset_part[2:4]
                offset_secs = (int(oh) * 3600 + int(om) * 60) * (1 if sign == "+" else -1)
            except (ValueError, IndexError):
                continue
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                try:
                    t = _time.strptime(dt_part, fmt)
                    # timegm() assumes the struct_time is UTC; subtract the
                    # string's own offset to land on the true UTC instant.
                    return float(_calendar.timegm(t) - offset_secs)
                except ValueError:
                    pass

    # Naive ISO (no tz offset) — interpret as UTC via timegm (not mktime).
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            t = _time.strptime(ts_norm, fmt)
            return float(_calendar.timegm(t))
        except ValueError:
            pass

    return None


def _is_stale(generated: str, now_utc: str, max_age_hours: float) -> bool:
    """Return True iff the card is older than *max_age_hours*.

    On any parse failure returns False (treat as not-stale rather than crash).
    """
    t_gen = _parse_iso(generated)
    t_now = _parse_iso(now_utc)
    if t_gen is None or t_now is None:
        return False
    age_hours = (t_now - t_gen) / 3600.0
    return age_hours > max_age_hours
