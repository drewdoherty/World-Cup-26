"""Stable canonical identifiers across sources.

Every silver/gold table keys on these IDs while ALWAYS carrying the original
source IDs alongside (``source``, ``source_event_id``, ``source_market_id``,
``token_id`` ...) so any row can be traced back to the raw payload.

Scheme (deterministic, human-readable):
    competition_id  ``fifa-wc-2026``
    event_id        ``wc2026:<home>__<away>__<YYYY-MM-DDTHH>Z``   (canonical
                    team names via production ``wca.data.teamnames.canonical``,
                    kickoff floored to the hour → same match from two venues
                    collides to one ID even with small feed-time skews)
    market_id       ``<event_id>|<market_type>|<line>|<period>|<settlement>``
    outcome_id      ``<market_id>|<outcome>``
    snapshot_id     the raw-layer path (see lib.storage.write_raw)

``settlement`` matters: 1X2 settles at 90 minutes; PM advancement includes
extra time + penalties. Two markets with different settlement bases are
DIFFERENT markets here and can never silently match (CLAUDE.md rule).
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

import lib.bootstrap  # noqa: F401  (sys.path side-effect)
from wca.data.teamnames import canonical

COMPETITION_ID = "fifa-wc-2026"

# Settlement bases
S_90MIN = "90min"          # regulation only (sportsbook 1X2, totals, btts…)
S_ETPENS = "et-pens"       # to advance / lift trophy (PM advancement)
S_UNKNOWN = "unknown"


def slug(name: str) -> str:
    """Lower-kebab canonical team slug: 'Korea Republic' -> 'south-korea'."""
    c = canonical(name or "")
    return re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-")


def event_id(home: str, away: str, kickoff_utc: dt.datetime) -> str:
    if kickoff_utc.tzinfo is None:
        raise ValueError("kickoff must be tz-aware UTC")
    ko = kickoff_utc.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H")
    return f"wc2026:{slug(home)}__{slug(away)}__{ko}Z"


def market_id(ev_id: str, market_type: str, *, line: Optional[float] = None,
              period: str = "FT", settlement: str = S_90MIN) -> str:
    ln = "" if line is None else f"{line:g}"
    return f"{ev_id}|{market_type}|{ln}|{period}|{settlement}"


def outcome_id(mk_id: str, outcome: str) -> str:
    return f"{mk_id}|{slug(outcome) or outcome.lower()}"


def parse_event_id(ev_id: str) -> dict:
    m = re.match(r"^wc2026:(?P<home>[^_]+(?:-[^_]+)*)__(?P<away>[^_]+(?:-[^_]+)*)__(?P<ko>.+)Z$", ev_id)
    if not m:
        raise ValueError(f"not an event_id: {ev_id}")
    return {"home_slug": m["home"], "away_slug": m["away"],
            "kickoff_hour_utc": m["ko"] + ":00:00Z"}
