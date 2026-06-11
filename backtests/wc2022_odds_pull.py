"""Pull WC2022 closing 1X2 odds from The Odds API historical endpoints.

Budgeted, one-shot data collection for the blend backtest (Step 2). Output is
saved to ``data/raw/wc2022_closing_odds.json`` and consumed by
``blend_fit.py step3``.

Cost model (verified live on 2026-06-11 against ``x-requests-last``):

* ``/v4/historical/sports/{sport}/events?date=ISO``  -> **1 credit** per call.
* ``/v4/historical/sports/{sport}/events/{id}/odds`` with ``regions=eu`` and
  ``markets=h2h`` -> **10 credits** per call (1 region x 1 market x 10).

Plan: discover the 64 WC2022 event ids by snapshotting the events listing once
per match-day (~23 credits), then pull ONE odds snapshot ~5 minutes before each
event's commence time (64 x 10 = 640 credits). Total ~= 665 credits, far under
the 7,000 hard budget. The script checks ``x-requests-remaining`` before
starting and after every 20 odds calls, and ABORTS if remaining would drop
below 11,000.

The 2022 World Cup sport key on The Odds API is ``soccer_fifa_world_cup``.

Run: ``python backtests/wc2022_odds_pull.py``  (idempotent: re-running reuses
the saved event list and only fetches odds for events still missing).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
OUT_PATH = os.path.join(_REPO, "data", "raw", "wc2022_closing_odds.json")
EVENTS_CACHE = os.path.join(_HERE, "_cache", "wc2022_events.json")

BASE = "https://api.the-odds-api.com/v4"
SPORT = "soccer_fifa_world_cup"
REGIONS = "eu"
MARKETS = "h2h"
TIMEOUT = 30
_HEADERS = {"User-Agent": "WorldCupAlpha/0.1 (research)", "Accept": "application/json"}

# Hard safety floor: abort if remaining credits would drop below this.
ABORT_BELOW = 11000

# WC2022 match dates (from results.csv). For each we snapshot the events listing
# late in the day (UTC) to capture every fixture that kicked off that day.
MATCH_DATES: Tuple[str, ...] = (
    "2022-11-20", "2022-11-21", "2022-11-22", "2022-11-23", "2022-11-24",
    "2022-11-25", "2022-11-26", "2022-11-27", "2022-11-28", "2022-11-29",
    "2022-11-30", "2022-12-01", "2022-12-02", "2022-12-03", "2022-12-04",
    "2022-12-05", "2022-12-06", "2022-12-09", "2022-12-10", "2022-12-13",
    "2022-12-14", "2022-12-17", "2022-12-18",
)


def _api_key() -> str:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        # Fall back to .env in the repo root.
        env = os.path.join(_REPO, ".env")
        if os.path.exists(env):
            with open(env) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("ODDS_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    if not key:
        raise EnvironmentError("ODDS_API_KEY not set and not found in .env")
    return key


def _remaining(resp: requests.Response) -> Optional[int]:
    v = resp.headers.get("x-requests-remaining")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _cost(resp: requests.Response) -> Optional[int]:
    v = resp.headers.get("x-requests-last")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def list_events_for_date(key: str, date_iso: str) -> Tuple[List[Dict], int, Optional[int]]:
    """Historical events active at the given snapshot timestamp (1 credit)."""
    url = "%s/historical/sports/%s/events" % (BASE, SPORT)
    r = requests.get(
        url, params={"apiKey": key, "date": date_iso}, headers=_HEADERS, timeout=TIMEOUT
    )
    r.raise_for_status()
    body = r.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    return data, _cost(r) or 0, _remaining(r)


def discover_events(key: str) -> Dict[str, Dict]:
    """Return {event_id: {home,away,commence_time}} for all WC2022 fixtures."""
    if os.path.exists(EVENTS_CACHE):
        with open(EVENTS_CACHE) as fh:
            return json.load(fh)

    events: Dict[str, Dict] = {}
    spent = 0
    for date in MATCH_DATES:
        # Snapshot at 08:00Z: before the day's first kickoff (earliest WC2022
        # kickoff was 10:00Z), so the upcoming-events listing carries every
        # fixture that commences this UTC day.
        snap = "%sT08:00:00Z" % date
        data, cost, remaining = list_events_for_date(key, snap)
        spent += cost
        for e in data:
            eid = e.get("id")
            ct = e.get("commence_time", "")
            # Keep only fixtures whose commence date is this match-day.
            if eid and ct[:10] == date:
                events[eid] = {
                    "id": eid,
                    "home_team": e.get("home_team"),
                    "away_team": e.get("away_team"),
                    "commence_time": ct,
                }
        print("  listing %s: +%d ev (cost %d, remaining %s)"
              % (date, len(data), cost, remaining))
        time.sleep(0.2)

    os.makedirs(os.path.dirname(EVENTS_CACHE), exist_ok=True)
    with open(EVENTS_CACHE, "w") as fh:
        json.dump(events, fh, indent=2)
    print("discovered %d unique WC2022 events (listing spend %d credits)"
          % (len(events), spent))
    return events


def _minus_minutes(iso_z: str, minutes: int) -> str:
    """Subtract minutes from an ISO Zulu timestamp."""
    from datetime import datetime, timedelta, timezone

    ct = iso_z.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ct).astimezone(timezone.utc)
    dt = dt - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_event_odds(key: str, eid: str, commence: str) -> Tuple[Optional[Dict], int, Optional[int]]:
    """One historical odds snapshot ~5 min before kickoff (10 credits)."""
    snap = _minus_minutes(commence, 5)
    url = "%s/historical/sports/%s/events/%s/odds" % (BASE, SPORT, eid)
    r = requests.get(
        url,
        params={
            "apiKey": key,
            "date": snap,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "decimal",
        },
        headers=_HEADERS,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    data = body.get("data") if isinstance(body, dict) else None
    snapshot_ts = body.get("timestamp") if isinstance(body, dict) else None
    if data:
        data["_snapshot_timestamp"] = snapshot_ts
        data["_snapshot_request"] = snap
    return data, _cost(r) or 0, _remaining(r)


def main() -> int:
    key = _api_key()

    # Budget preflight.
    r = requests.get(
        "%s/sports" % BASE, params={"apiKey": key, "all": "true"},
        headers=_HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    remaining = _remaining(r)
    print("preflight remaining credits:", remaining)
    if remaining is not None and remaining < ABORT_BELOW + 700:
        print("ABORT: remaining %s below safety floor for ~700 credit pull"
              % remaining)
        return 1

    events = discover_events(key)
    if not (60 <= len(events) <= 70):
        print("WARNING: discovered %d events (expected ~64)" % len(events))

    # Resume support: keep any already-saved snapshots.
    saved: Dict[str, Dict] = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH) as fh:
            blob = json.load(fh)
        for ev in blob.get("events", []):
            saved[ev.get("id")] = ev

    spent = 0
    n_calls = 0
    for eid, meta in events.items():
        if eid in saved and saved[eid].get("bookmakers"):
            continue
        data, cost, remaining = pull_event_odds(key, eid, meta["commence_time"])
        spent += cost
        n_calls += 1
        if data:
            saved[eid] = data
            n_books = len(data.get("bookmakers", []))
        else:
            saved[eid] = {**meta, "bookmakers": [], "_empty": True}
            n_books = 0
        print("  %s %s vs %s: %d books (cost %d, remaining %s)"
              % (eid[:8], meta["home_team"], meta["away_team"], n_books, cost, remaining))

        # Persist incrementally so an abort never loses fetched data.
        _save(saved)

        if remaining is not None and remaining < ABORT_BELOW:
            print("ABORT: remaining %s dropped below floor %d" % (remaining, ABORT_BELOW))
            return 1
        if n_calls % 20 == 0:
            print("  --- %d calls, %d credits spent so far, remaining %s ---"
                  % (n_calls, spent, remaining))
        time.sleep(0.2)

    print("done: %d events saved, odds-pull spend %d credits" % (len(saved), spent))
    return 0


def _save(saved: Dict[str, Dict]) -> None:
    out = {
        "sport": SPORT,
        "regions": REGIONS,
        "markets": MARKETS,
        "note": "WC2022 closing 1X2 odds, ~5min pre-kickoff historical snapshots",
        "events": list(saved.values()),
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
