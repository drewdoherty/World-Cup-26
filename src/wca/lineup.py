"""Lineup-strength model — turn ``data/players.db`` into a per-team attacking
rating and an honest absences list for a fixture.

Scope and honesty
-----------------
There is no live expected-XI or injury feed wired into this repo. So this model
degrades exactly as the data allows:

* **Rating** is grounded in *real* StatsBomb per-90 output: a team's attacking
  rating is the summed ``npxg_p90`` of its best **available** squad players that
  have event history (matched to ``players.db`` via ``squad_members``). A team
  with no StatsBomb history (e.g. a 2026 debutant squad) returns ``rating=None``
  and ``source='data-pending'`` — never an invented number.
* **Absences** come only from a caller-supplied injuries source (a dict or
  ``data/injuries.json`` of ``{team: [names]}``). With no feed, the absences
  list is empty and the lineup is labelled ``expected XI (no injury feed)``.

``get_lineup_strength(match_id, team=...)`` returns a :class:`LineupStrength`,
which unpacks as the spec's ``(lineup_name, rating, absences)`` triple.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from wca.data.players_db import DEFAULT_DB_PATH, THIN_MINUTES, _norm_name
from wca.data.teamnames import canonical

# Default size of the attacking core summed into the rating. Goals come from a
# handful of players, so the top slice carries the signal; the full XI's
# defenders contribute ~nothing to an *attacking* npxg rating.
DEFAULT_TOP_N = 11


@dataclass
class PlayerRating:
    """One available player's contribution to the lineup rating."""

    name: str                 # squad-list name
    sb_name: str              # matched StatsBomb full name
    npxg_p90: Optional[float]
    minutes: Optional[float]
    thin: bool


@dataclass
class LineupStrength:
    """A team's lineup strength for a fixture.

    Iterating yields the spec triple ``(lineup_name, rating, absences)``.
    """

    team: str
    lineup_name: str
    rating: Optional[float]
    n_available: int
    n_with_stats: int
    absences: List[str]
    source: str
    contributors: List[PlayerRating] = field(default_factory=list)

    def __iter__(self):
        return iter((self.lineup_name, self.rating, self.absences))


@dataclass
class MatchLineups:
    """Both sides' lineup strengths for a fixture."""

    home: LineupStrength
    away: LineupStrength


# ---------------------------------------------------------------------------
# Match / injuries resolution
# ---------------------------------------------------------------------------

def resolve_match(match_id: Union[str, Tuple[str, str], Dict]) -> Tuple[str, str]:
    """Resolve a fixture id to ``(home_canonical, away_canonical)``.

    Accepts ``"Home vs Away"`` (the repo's fixture key), a ``(home, away)``
    tuple/list, or a dict with ``home``/``away`` (or ``fixture``).
    """
    if isinstance(match_id, dict):
        if match_id.get("home") and match_id.get("away"):
            return canonical(match_id["home"]), canonical(match_id["away"])
        match_id = match_id.get("fixture", "")
    if isinstance(match_id, (tuple, list)):
        if len(match_id) != 2:
            raise ValueError("match tuple must be (home, away)")
        return canonical(match_id[0]), canonical(match_id[1])
    if isinstance(match_id, str):
        for sep in (" vs ", " v ", " - "):
            if sep in match_id:
                home, away = match_id.split(sep, 1)
                return canonical(home.strip()), canonical(away.strip())
    raise ValueError("could not resolve match id: %r" % (match_id,))


def load_injuries(injuries: Union[None, Dict, str],
                  path: str = "data/injuries.json") -> Dict[str, set]:
    """Normalise an injuries source to ``{canonical_team: {norm_name, ...}}``.

    ``injuries`` may be a dict ``{team: [names]}``, a path to such a JSON file,
    or ``None`` (in which case ``path`` is read if it exists, else empty).
    """
    raw: Optional[Dict] = None
    if isinstance(injuries, dict):
        raw = injuries
    elif isinstance(injuries, str):
        if os.path.exists(injuries):
            with open(injuries, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
    elif injuries is None and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    if not raw:
        return {}
    out: Dict[str, set] = {}
    for team, names in raw.items():
        if team.startswith("_") or not isinstance(names, list):
            continue
        out[canonical(team)] = {_norm_name(n) for n in names if n}
    return out


# ---------------------------------------------------------------------------
# players.db access
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            "players.db not found at %s — run scripts/wca_build_players_db.py"
            % db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _players_by_team(conn: sqlite3.Connection) -> Dict[str, List[sqlite3.Row]]:
    """All StatsBomb player rows grouped by canonical team."""
    out: Dict[str, List[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM players"):
        out.setdefault(canonical(r["team"]), []).append(r)
    return out


def _match_player_row(squad_name: str, rows: List[sqlite3.Row]) -> Optional[sqlite3.Row]:
    """Confident name match (mirrors players_db._has_event_history)."""
    target = _norm_name(squad_name)
    if not target:
        return None
    by_full = {_norm_name(r["player"]): r for r in rows}
    if target in by_full:
        return by_full[target]
    tokens = [t for t in target.split() if len(t) > 1]
    if len(tokens) >= 2:
        for full, r in by_full.items():
            if all(t in full for t in tokens):
                return r
    return None


def _squad_names(conn: sqlite3.Connection, team_canon: str) -> List[str]:
    """Distinct squad-member names for a team (published squad + overrides)."""
    rows = conn.execute(
        "SELECT DISTINCT player FROM squad_members WHERE team=?", (team_canon,))
    return [r["player"] for r in rows]


# ---------------------------------------------------------------------------
# Rating
# ---------------------------------------------------------------------------

def team_lineup_strength(
    team: str,
    conn: Optional[sqlite3.Connection] = None,
    db_path: str = DEFAULT_DB_PATH,
    injuries: Union[None, Dict, str] = None,
    top_n: int = DEFAULT_TOP_N,
    players_by_team: Optional[Dict[str, List[sqlite3.Row]]] = None,
) -> LineupStrength:
    """Compute a single team's :class:`LineupStrength`.

    The rating is the summed ``npxg_p90`` of the team's best ``top_n``
    available players that carry StatsBomb history; injured players (per the
    injuries source) are removed first and listed in ``absences``.
    """
    team_canon = canonical(team)
    own_conn = conn is None
    if own_conn:
        conn = _connect(db_path)
    try:
        if players_by_team is None:
            players_by_team = _players_by_team(conn)
        squad = _squad_names(conn, team_canon)
        rows = players_by_team.get(team_canon, [])
        inj = load_injuries(injuries).get(team_canon, set())

        absences: List[str] = []
        contributors: List[PlayerRating] = []
        for name in squad:
            if _norm_name(name) in inj:
                absences.append(name)
                continue
            row = _match_player_row(name, rows)
            if row is None or row["npxg_p90"] is None:
                continue
            contributors.append(PlayerRating(
                name=name,
                sb_name=row["player"],
                npxg_p90=float(row["npxg_p90"]),
                minutes=None if row["minutes"] is None else float(row["minutes"]),
                thin=bool(row["thin"]),
            ))

        contributors.sort(key=lambda p: p.npxg_p90, reverse=True)
        core = contributors[:top_n]
        n_with_stats = len(contributors)
        n_available = max(len(squad) - len(absences), 0)

        if not core:
            label = "%s — data-pending (no StatsBomb history for squad)" % team_canon
            if absences:
                label += " · %d out" % len(absences)
            return LineupStrength(
                team=team_canon, lineup_name=label, rating=None,
                n_available=n_available, n_with_stats=0, absences=absences,
                source="data-pending", contributors=[])

        rating = round(sum(p.npxg_p90 for p in core), 4)
        feed = "no injury feed" if not inj else "%d out" % len(absences)
        label = "%s — expected XI (%s; %d of %d rated)" % (
            team_canon, feed, n_with_stats, len(squad))
        return LineupStrength(
            team=team_canon, lineup_name=label, rating=rating,
            n_available=n_available, n_with_stats=n_with_stats,
            absences=absences, source="statsbomb_npxg_p90", contributors=core)
    finally:
        if own_conn:
            conn.close()


def get_lineup_strength(
    match_id: Union[str, Tuple[str, str], Dict],
    team: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
    injuries: Union[None, Dict, str] = None,
    top_n: int = DEFAULT_TOP_N,
) -> Union[LineupStrength, MatchLineups]:
    """Lineup strength for a fixture.

    With ``team`` given, returns that side's :class:`LineupStrength` (which
    unpacks to ``(lineup_name, rating, absences)``). With ``team=None``, returns
    a :class:`MatchLineups` of both sides.
    """
    home, away = resolve_match(match_id)
    conn = _connect(db_path)
    try:
        pbt = _players_by_team(conn)
        if team is not None:
            tc = canonical(team)
            if tc not in (home, away):
                raise ValueError(
                    "team %r is not in match (%s vs %s)" % (team, home, away))
            return team_lineup_strength(
                tc, conn=conn, injuries=injuries, top_n=top_n,
                players_by_team=pbt)
        return MatchLineups(
            home=team_lineup_strength(home, conn=conn, injuries=injuries,
                                      top_n=top_n, players_by_team=pbt),
            away=team_lineup_strength(away, conn=conn, injuries=injuries,
                                      top_n=top_n, players_by_team=pbt),
        )
    finally:
        conn.close()
