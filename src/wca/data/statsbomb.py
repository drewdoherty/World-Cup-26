"""StatsBomb open-data pipeline for World Cup prop markets.

Downloads (and caches) match and event JSON from the StatsBomb open-data
repository, then aggregates match-level prop counts (corners, cards, fouls,
shots, xG, goals) and player-level shot/xG shares.

Competitions of interest
------------------------
FIFA World Cup: competition_id=43, season_id=3 (2018) and season_id=106 (2022).

Card-counting convention
------------------------
A 'Second Yellow' is counted as ONE red card (not two yellows): under FIFA
rules the second caution converts into a sending-off, and prop markets
("total red cards") settle on the dismissal. So:
    'Yellow Card'   -> +1 yellow
    'Second Yellow' -> +1 red
    'Red Card'      -> +1 red
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

RAW_BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"

WC_COMPETITION_ID = 43
WC_SEASONS = {3: "WC2018", 106: "WC2022"}

DEFAULT_CACHE_DIR = "data/raw/statsbomb"

_session = None


def _get_session():
    """Return a module-level requests.Session (lazy)."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "wca-props/1.0"})
    return _session


def _download_json(url, retries=4, backoff=5.0):
    """GET a JSON URL with simple retry/backoff on failures or rate limits."""
    sess = _get_session()
    last_err = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 429, 500, 502, 503):
                logger.warning("HTTP %d for %s; retrying", resp.status_code, url)
                time.sleep(backoff * (attempt + 1))
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_err = exc
            logger.warning("request error for %s: %s; retrying", url, exc)
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError("failed to download %s (last error: %s)" % (url, last_err))


def _cached_fetch(url, cache_path):
    """Return JSON from cache_path if present, else download and cache."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        with open(cache_path, "r") as fh:
            return json.load(fh)
    data = _download_json(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    tmp.rename(cache_path)
    return data


def fetch_matches(competition_id, season_id, cache_dir=DEFAULT_CACHE_DIR):
    """Fetch the match list for a competition/season, with disk caching.

    Parameters
    ----------
    competition_id : int
    season_id : int
    cache_dir : str
        Directory for cached JSON files.

    Returns
    -------
    list of dict
        StatsBomb match dicts.
    """
    url = "%s/matches/%d/%d.json" % (RAW_BASE, competition_id, season_id)
    cache_path = Path(cache_dir) / ("matches_%d_%d.json" % (competition_id, season_id))
    return _cached_fetch(url, cache_path)


def fetch_events(match_id, cache_dir=DEFAULT_CACHE_DIR):
    """Fetch the event stream for a match, with disk caching.

    Parameters
    ----------
    match_id : int
    cache_dir : str

    Returns
    -------
    list of dict
        StatsBomb event dicts (chronological).
    """
    url = "%s/events/%d.json" % (RAW_BASE, match_id)
    cache_path = Path(cache_dir) / "events" / ("%d.json" % match_id)
    return _cached_fetch(url, cache_path)


def _card_counts(event):
    """Return (yellows, reds) implied by a single event's card fields."""
    yellows = 0
    reds = 0
    for key in ("foul_committed", "bad_behaviour"):
        card = (event.get(key) or {}).get("card")
        if not card:
            continue
        name = card.get("name")
        if name == "Yellow Card":
            yellows += 1
        elif name == "Second Yellow":
            # Counted as one red card; see module docstring.
            reds += 1
        elif name == "Red Card":
            reds += 1
    return yellows, reds


def _home_away_teams(events):
    """Infer (home_team, away_team) names from the Starting XI events.

    StatsBomb orders the two Starting XI events home-first. Falls back to
    first-seen team order if Starting XI events are absent.
    """
    teams = []
    for ev in events:
        if (ev.get("type") or {}).get("name") == "Starting XI":
            name = (ev.get("team") or {}).get("name")
            if name and name not in teams:
                teams.append(name)
        if len(teams) == 2:
            return teams[0], teams[1]
    for ev in events:
        name = (ev.get("team") or {}).get("name")
        if name and name not in teams:
            teams.append(name)
        if len(teams) == 2:
            return teams[0], teams[1]
    raise ValueError("could not infer two teams from events")


def match_props(events, home_team=None, away_team=None):
    """Aggregate one match's events into prop-market counts.

    Parameters
    ----------
    events : list of dict
        StatsBomb events for a single match.
    home_team, away_team : str, optional
        Team names; inferred from Starting XI order if omitted.

    Returns
    -------
    dict
        Keys: corners/yellows/reds/fouls/shots/goals (ints) and xg (float),
        each suffixed _home and _away.
    """
    if home_team is None or away_team is None:
        home_team, away_team = _home_away_teams(events)

    out = {}
    for k in ("corners", "yellows", "reds", "fouls", "shots", "goals"):
        out[k + "_home"] = 0
        out[k + "_away"] = 0
    out["xg_home"] = 0.0
    out["xg_away"] = 0.0

    def side(team_name):
        if team_name == home_team:
            return "_home"
        if team_name == away_team:
            return "_away"
        return None

    for ev in events:
        team = (ev.get("team") or {}).get("name")
        sfx = side(team)
        if sfx is None:
            continue
        etype = (ev.get("type") or {}).get("name")

        if etype == "Pass":
            ptype = ((ev.get("pass") or {}).get("type") or {}).get("name")
            if ptype == "Corner":
                out["corners" + sfx] += 1
        elif etype == "Shot":
            shot = ev.get("shot") or {}
            out["shots" + sfx] += 1
            xg = shot.get("statsbomb_xg")
            if xg is not None:
                out["xg" + sfx] += float(xg)
            if (shot.get("outcome") or {}).get("name") == "Goal":
                out["goals" + sfx] += 1
        elif etype == "Own Goal For":
            out["goals" + sfx] += 1
        elif etype == "Foul Committed":
            out["fouls" + sfx] += 1

        y, r = _card_counts(ev)
        out["yellows" + sfx] += y
        out["reds" + sfx] += r

    return out


def _minute_value(event):
    """Absolute minute of an event (period-aware enough for subs)."""
    return event.get("minute")


def _match_minutes(events):
    """Approximate per-player minutes for one match.

    Starters get (match_end - 0); substitutes coming on get
    (match_end - sub_minute); a player substituted off gets capped at the
    sub minute. Red cards are ignored (approximation). Returns dict
    {(player, team): minutes}.
    """
    starters = {}
    sub_on = {}
    sub_off = {}
    max_minute = 0
    for ev in events:
        m = ev.get("minute")
        if isinstance(m, (int, float)) and m > max_minute:
            max_minute = m
        etype = (ev.get("type") or {}).get("name")
        team = (ev.get("team") or {}).get("name")
        if etype == "Starting XI":
            lineup = ((ev.get("tactics") or {}).get("lineup")) or []
            for entry in lineup:
                pname = (entry.get("player") or {}).get("name")
                if pname:
                    starters[(pname, team)] = True
        elif etype == "Substitution":
            off = (ev.get("player") or {}).get("name")
            on = ((ev.get("substitution") or {}).get("replacement") or {}).get("name")
            minute = ev.get("minute") or 0
            if off:
                sub_off[(off, team)] = minute
            if on:
                sub_on[(on, team)] = minute

    end = max(max_minute, 90)
    minutes = {}
    for key in starters:
        minutes[key] = min(sub_off.get(key, end), end)
    for key, on_min in sub_on.items():
        minutes[key] = max(0, min(sub_off.get(key, end), end) - on_min)
    return minutes


def player_shares(events_by_match):
    """Aggregate player-level shot/xG stats across matches.

    Parameters
    ----------
    events_by_match : dict
        Mapping match_id -> list of events.

    Returns
    -------
    pandas.DataFrame
        Columns: player, team, minutes, shots, goals, xg_sum, npxg_sum,
        matches. npxg_sum excludes penalty-shot xG. minutes is approximate
        (from Starting XI / Substitution events) and NaN if unavailable.
    """
    stats = {}  # (player, team) -> dict

    def get(key):
        if key not in stats:
            stats[key] = {
                "minutes": 0.0,
                "has_minutes": False,
                "shots": 0,
                "goals": 0,
                "xg_sum": 0.0,
                "npxg_sum": 0.0,
                "matches": set(),
            }
        return stats[key]

    for match_id, events in events_by_match.items():
        mins = _match_minutes(events)
        for key, mval in mins.items():
            rec = get(key)
            rec["minutes"] += mval
            rec["has_minutes"] = True
            rec["matches"].add(match_id)

        for ev in events:
            if (ev.get("type") or {}).get("name") != "Shot":
                continue
            player = (ev.get("player") or {}).get("name")
            team = (ev.get("team") or {}).get("name")
            if not player:
                continue
            rec = get((player, team))
            rec["matches"].add(match_id)
            shot = ev.get("shot") or {}
            rec["shots"] += 1
            xg = float(shot.get("statsbomb_xg") or 0.0)
            rec["xg_sum"] += xg
            is_pen = ((shot.get("type") or {}).get("name")) == "Penalty"
            if not is_pen:
                rec["npxg_sum"] += xg
            if (shot.get("outcome") or {}).get("name") == "Goal":
                rec["goals"] += 1

    rows = []
    for (player, team), rec in stats.items():
        rows.append({
            "player": player,
            "team": team,
            "minutes": rec["minutes"] if rec["has_minutes"] else float("nan"),
            "shots": rec["shots"],
            "goals": rec["goals"],
            "xg_sum": rec["xg_sum"],
            "npxg_sum": rec["npxg_sum"],
            "matches": len(rec["matches"]),
        })
    df = pd.DataFrame(
        rows,
        columns=["player", "team", "minutes", "shots", "goals",
                 "xg_sum", "npxg_sum", "matches"],
    )
    if len(df):
        df = df.sort_values("npxg_sum", ascending=False).reset_index(drop=True)
    return df


def build_props_dataset(cache_dir=DEFAULT_CACHE_DIR, out_dir="data/processed"):
    """Build match- and player-level prop datasets for WC2018 + WC2022.

    Downloads (with caching) match lists and event files, aggregates them,
    and writes props_matches.csv and props_players.csv to out_dir.

    Returns
    -------
    (matches_df, players_df) : tuple of pandas.DataFrame
    """
    match_rows = []
    events_by_match = {}

    for season_id, label in sorted(WC_SEASONS.items()):
        matches = fetch_matches(WC_COMPETITION_ID, season_id, cache_dir=cache_dir)
        logger.info("%s: %d matches", label, len(matches))
        for m in matches:
            match_id = m["match_id"]
            events = fetch_events(match_id, cache_dir=cache_dir)
            events_by_match[match_id] = events
            home = (m.get("home_team") or {}).get("home_team_name")
            away = (m.get("away_team") or {}).get("away_team_name")
            props = match_props(events, home_team=home, away_team=away)
            row = {
                "match_id": match_id,
                "season": label,
                "date": m.get("match_date"),
                "home": home,
                "away": away,
            }
            row.update(props)
            match_rows.append(row)

    matches_df = pd.DataFrame(match_rows).sort_values(
        ["date", "match_id"]).reset_index(drop=True)
    players_df = player_shares(events_by_match)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    matches_df.to_csv(out / "props_matches.csv", index=False)
    players_df.to_csv(out / "props_players.csv", index=False)
    return matches_df, players_df
