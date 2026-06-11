"""Consensus line-movement series for the World Cup Alpha site (``linemove.json``).

The snapshot daemon writes one ``odds_snapshots`` row per (timestamp, bookmaker,
selection) every poll cycle.  This module collapses that firehose into a compact,
per-event time series of the **market consensus** implied probability for each
1X2 outcome, so the static front-end can draw a line-movement chart.

Design notes
------------
* **Deterministic.**  :func:`build_linemove` never reads the wall clock or the
  network — the caller passes ``now_utc`` (the CLI is allowed to stamp it) and
  the data comes entirely from the on-disk SQLite ledger.
* **Consensus per timestamp.**  At a given ``ts_utc`` several bookmakers each
  quote the three outcomes.  For one selection we take the *median* of the raw
  implied probabilities ``1/odds`` across those bookmaker rows.  The three
  per-outcome medians are then normalised to sum to 1 (removing the bookmaker
  overround in aggregate) so the series is directly comparable across time.
* **Tolerant.**  A missing database, a missing ``odds_snapshots`` table or an
  event with fewer than two distinct timestamps simply yields no series — never
  an exception.

Output shape::

    {
      "meta": {"generated": now_utc},
      "events": [
        {
          "fixture": "Mexico vs South Africa",
          "kickoff": "2026-06-11T19:00:00Z",
          "series": {
            "home": [[ts, prob], ...],
            "draw": [[ts, prob], ...],
            "away": [[ts, prob], ...]
          }
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from wca.data import teamnames


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _canon(name: Any) -> str:
    """Canonicalise a team / outcome name for matching: alias-resolved and
    casefolded.  Returns ``""`` for missing / non-string inputs."""
    if not isinstance(name, str):
        return ""
    canon = teamnames.canonical(name)
    if not isinstance(canon, str):
        return ""
    return canon.casefold().strip()


def _median(values: List[float]) -> Optional[float]:
    """Median of a non-empty list of floats (no numpy dependency).

    Returns ``None`` for an empty list.  For an even count it averages the two
    middle values, matching ``numpy.median`` / ``statistics.median``.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _leg_for_outcome(
    selection: Any, home_c: str, away_c: str
) -> Optional[str]:
    """Map an h2h outcome label to ``"home"`` / ``"draw"`` / ``"away"``.

    Soccer h2h outcomes are the two team names plus the literal ``"Draw"``.
    Comparison is on canonicalised names so feed / card spelling variants align.
    Returns ``None`` for an unrecognised label.
    """
    sel_c = _canon(selection)
    if not sel_c:
        return None
    if sel_c == "draw":
        return "draw"
    if home_c and sel_c == home_c:
        return "home"
    if away_c and sel_c == away_c:
        return "away"
    return None


def _opt_float(value: Any) -> Optional[float]:
    """Coerce a value to a positive float price, else ``None``.

    Non-positive odds are rejected (an implied probability ``1/odds`` is only
    meaningful for a positive decimal price).
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    if result <= 0.0:
        return None
    return result


def _downsample(points: List[Any], max_points: int) -> List[Any]:
    """Evenly downsample ``points`` to at most ``max_points`` entries.

    The first and last points are always kept; intermediate points are picked
    at evenly spaced indices.  When ``len(points) <= max_points`` the list is
    returned unchanged.
    """
    n = len(points)
    if max_points <= 0:
        return []
    if n <= max_points:
        return list(points)
    if max_points == 1:
        return [points[-1]]
    # Evenly spaced indices from 0 to n-1 inclusive (so first & last are kept).
    out: List[Any] = []
    last_idx = -1
    for k in range(max_points):
        idx = round(k * (n - 1) / (max_points - 1))
        if idx != last_idx:
            out.append(points[idx])
            last_idx = idx
    return out


# ---------------------------------------------------------------------------
# Consensus series construction.
# ---------------------------------------------------------------------------


def _consensus_at_ts(
    rows: List[Tuple[str, Optional[float]]], home_c: str, away_c: str
) -> Optional[Dict[str, float]]:
    """Compute the normalised consensus implied probabilities for one timestamp.

    ``rows`` is a list of ``(selection, decimal_odds)`` for a single event at a
    single ``ts_utc`` (across all bookmakers).  For each of the three legs we
    take the median of ``1/odds`` over the bookmaker rows, then normalise the
    three medians to sum to 1.

    Returns ``{"home": p, "draw": p, "away": p}`` or ``None`` when any leg has no
    usable price (so we never emit a partial / misleading consensus point).
    """
    buckets: Dict[str, List[float]] = {"home": [], "draw": [], "away": []}
    for selection, odds in rows:
        leg = _leg_for_outcome(selection, home_c, away_c)
        if leg is None:
            continue
        price = _opt_float(odds)
        if price is None:
            continue
        buckets[leg].append(1.0 / price)

    medians: Dict[str, float] = {}
    for leg in ("home", "draw", "away"):
        med = _median(buckets[leg])
        if med is None:
            return None
        medians[leg] = med

    total = medians["home"] + medians["draw"] + medians["away"]
    if total <= 0.0:
        return None
    return {leg: medians[leg] / total for leg in ("home", "draw", "away")}


def _series_for_event(
    ts_rows: Dict[str, List[Tuple[str, Optional[float]]]],
    home: str,
    away: str,
    max_points: int,
) -> Optional[Dict[str, List[List[Any]]]]:
    """Build the downsampled per-leg series for one event.

    ``ts_rows`` maps ``ts_utc`` -> list of ``(selection, decimal_odds)``.  We
    require at least two distinct timestamps that each yield a complete
    consensus; otherwise there is nothing to plot and we return ``None``.
    """
    home_c = _canon(home)
    away_c = _canon(away)

    consensus_points: List[Tuple[str, Dict[str, float]]] = []
    for ts in sorted(ts_rows):
        cons = _consensus_at_ts(ts_rows[ts], home_c, away_c)
        if cons is not None:
            consensus_points.append((ts, cons))

    if len(consensus_points) < 2:
        return None

    consensus_points = _downsample(consensus_points, max_points)

    series: Dict[str, List[List[Any]]] = {"home": [], "draw": [], "away": []}
    for ts, cons in consensus_points:
        for leg in ("home", "draw", "away"):
            series[leg].append([ts, cons[leg]])
    return series


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def build_linemove(
    db_path: str,
    event_meta: Dict[str, Dict[str, Any]],
    max_points: int = 120,
    now_utc: str = "",
) -> Dict[str, Any]:
    """Build the per-event consensus line-movement payload.

    Parameters
    ----------
    db_path:
        Path to the SQLite ledger.  A missing file or a missing
        ``odds_snapshots`` table yields ``{"events": []}`` (never raises).
    event_meta:
        Maps ``match_id`` -> ``{"fixture", "home", "away", "kickoff"}``.  Only
        events present in this map are emitted, and the home/away names are used
        to label the three legs.  Events absent from the map are skipped.
    max_points:
        Cap on the number of timestamps per series; the points are downsampled
        evenly (first & last always retained).
    now_utc:
        Pre-formatted generation timestamp (the caller stamps the clock).

    Returns
    -------
    dict
        ``{"meta": {"generated": now_utc}, "events": [...]}`` — see module
        docstring for the per-event shape.  Events are ordered by ``match_id``
        for deterministic output.
    """
    out: Dict[str, Any] = {"meta": {"generated": now_utc}, "events": []}

    if not event_meta:
        return out
    if not db_path or not os.path.exists(db_path):
        return out

    # Group h2h rows by match_id -> ts_utc -> [(selection, decimal_odds), ...].
    grouped: Dict[str, Dict[str, List[Tuple[str, Optional[float]]]]] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT match_id, ts_utc, selection, decimal_odds "
                "FROM odds_snapshots WHERE market = 'h2h'"
            )
            for match_id, ts_utc, selection, decimal_odds in cur.fetchall():
                if match_id is None or ts_utc is None:
                    continue
                by_ts = grouped.setdefault(str(match_id), {})
                by_ts.setdefault(str(ts_utc), []).append((selection, decimal_odds))
        finally:
            conn.close()
    except sqlite3.Error:
        # Missing table / corrupt db / locked -> tolerate with empty output.
        return out

    events: List[Dict[str, Any]] = []
    for match_id in sorted(grouped):
        meta = event_meta.get(match_id)
        if not meta:
            continue
        ts_rows = grouped[match_id]
        if len(ts_rows) < 2:  # need >= 2 distinct timestamps
            continue
        series = _series_for_event(
            ts_rows, meta.get("home", ""), meta.get("away", ""), max_points
        )
        if series is None:
            continue
        events.append({
            "fixture": meta.get("fixture", ""),
            "kickoff": meta.get("kickoff", ""),
            "series": series,
        })

    out["events"] = events
    return out


def write_linemove(
    db_path: str,
    out_path: str = "site/linemove.json",
    event_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    max_points: int = 120,
    now_utc: str = "",
) -> str:
    """Build the line-movement payload and write it to ``out_path`` as JSON.

    Parent directories are created as needed.  Returns ``out_path``.
    """
    data = build_linemove(
        db_path,
        event_meta or {},
        max_points=max_points,
        now_utc=now_utc,
    )

    # Never clobber a populated file with an empty payload: an empty events
    # list here almost always means a transient input problem (e.g. the
    # newest raw snapshot was read mid-write by the daemon and failed to
    # parse), not that line history genuinely vanished.
    if not data.get("events"):
        try:
            with open(out_path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if existing.get("events"):
                return out_path  # keep the good file
        except Exception:
            pass  # no existing file / unreadable -> write the empty payload

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return out_path


def robust_event_meta(snapshots_dir: str, max_files: int = 4) -> Dict[str, Dict[str, Any]]:
    """Event meta from the newest parseable snapshot file in a directory.

    The daemon may be mid-write on the newest file (truncated JSON parses to
    nothing), so walk backwards through up to ``max_files`` recent snapshots
    until one yields a non-empty meta map.
    """
    import glob as _glob

    files = sorted(_glob.glob(os.path.join(snapshots_dir, "oddsapi_h2h_uk_*.json")))
    for path in reversed(files[-max_files:]):
        meta = event_meta_from_snapshot_file(path)
        if meta:
            return meta
    return {}


def event_meta_from_snapshot_file(path: str) -> Dict[str, Dict[str, Any]]:
    """Derive ``event_meta`` from a raw Odds-API h2h snapshot JSON file.

    The file is the raw API response: a JSON list whose entries carry an event
    identifier, ``home_team`` / ``away_team`` and ``commence_time``.  Two on-disk
    shapes are tolerated:

    * the canonical Odds-API event shape, keyed by ``id`` (with nested
      ``bookmakers``); and
    * the flattened per-row dump, keyed by ``event_id`` (one row per
      bookmaker/outcome), where the same event id repeats across rows.

    Returns ``match_id`` -> ``{"fixture", "home", "away", "kickoff"}``.  A
    missing / unreadable / malformed file yields an empty dict (never raises).
    The fixture label is ``"<home> vs <away>"``.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}

    if not isinstance(data, list):
        return {}

    meta: Dict[str, Dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        match_id = entry.get("id")
        if match_id is None:
            match_id = entry.get("event_id")
        if match_id is None:
            continue
        match_id = str(match_id)
        if match_id in meta:
            continue  # first occurrence wins (handles repeated flat rows)
        home = entry.get("home_team")
        away = entry.get("away_team")
        if not isinstance(home, str) or not isinstance(away, str):
            continue
        kickoff = entry.get("commence_time")
        meta[match_id] = {
            "fixture": "{0} vs {1}".format(home, away),
            "home": home,
            "away": away,
            "kickoff": kickoff if isinstance(kickoff, str) else "",
        }
    return meta
