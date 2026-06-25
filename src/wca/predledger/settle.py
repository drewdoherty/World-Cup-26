"""Settle pass for the prediction ledger.

Queries all 'open' predictions and resolves status to won/lost/push based on:

  Market         Source
  ─────────────  ───────────────────────────────────────────────────
  1x2            data/processed/wc2026_results.json
  scoreline      data/processed/wc2026_results.json
  ou_<L>         data/processed/wc2026_results.json  (push on integer hit)
  btts           data/processed/wc2026_results.json
  advancement    data/advancement_played_results.json (stays open until decidable)

No P&L is computed here — that lives on the linked bets row.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import re

from wca.advancement import STAGE_ORDER, WC2026_GROUPS
from wca.data.teamnames import canonical
from wca.predledger.store import _connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight fixture / score helpers (avoid heavy wca.tracking import chain)
# ---------------------------------------------------------------------------

_SEP_RE = re.compile(r" vs\.? | v\.? ", re.IGNORECASE)
_SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _fixture_key(fixture: str) -> Optional[Tuple[str, str]]:
    """Return (home_canon, away_canon) tuple or None if unparseable."""
    if not fixture:
        return None
    m = _SEP_RE.search(fixture)
    if m is None:
        return None
    home = fixture[: m.start()].strip()
    away = fixture[m.end() :].strip()
    if not home or not away:
        return None
    return (canonical(home).casefold(), canonical(away).casefold())


def _parse_score(score: str) -> Optional[Tuple[int, int]]:
    """Parse '2-1' into (2, 1); None on failure."""
    m = _SCORE_RE.match(score or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# ---------------------------------------------------------------------------
# Result index from wc2026_results.json
# ---------------------------------------------------------------------------


def _build_results_index(results: List[Dict]) -> Dict[Tuple[str, str], Dict]:
    """Index results by canonical fixture key → {outcome, home_goals, away_goals}."""
    index: Dict[Tuple[str, str], Dict] = {}
    for r in results:
        key = _fixture_key(r.get("fixture") or "")
        if key is None:
            continue
        parsed = _parse_score(r.get("score") or "")
        if parsed is None:
            continue
        h, a = parsed
        index[key] = {
            "outcome": (r.get("outcome") or "").strip().lower(),
            "home_goals": h,
            "away_goals": a,
        }
    return index


# ---------------------------------------------------------------------------
# Advancement state from advancement_played_results.json
# ---------------------------------------------------------------------------


def _team_to_group_map() -> Dict[str, str]:
    return {
        canonical(t): letter
        for letter, teams in WC2026_GROUPS.items()
        for t in teams
    }


def _compute_group_standings(
    matches: List[Dict], team_to_group: Dict[str, str]
) -> Dict[str, Dict]:
    """Return {team: {pts, gd, gf, played}} for group matches only."""
    stats: Dict[str, Dict] = {
        t: {"pts": 0, "gd": 0, "gf": 0, "played": 0} for t in team_to_group
    }
    for m in matches:
        home = canonical(m.get("home") or "")
        away = canonical(m.get("away") or "")
        gh = team_to_group.get(home)
        ga = team_to_group.get(away)
        if gh is None or ga is None or gh != ga:
            continue
        hg = int(m.get("hg", 0))
        ag = int(m.get("ag", 0))
        stats[home]["played"] += 1
        stats[home]["gf"] += hg
        stats[home]["gd"] += hg - ag
        stats[away]["played"] += 1
        stats[away]["gf"] += ag
        stats[away]["gd"] += ag - hg
        if hg > ag:
            stats[home]["pts"] += 3
        elif hg == ag:
            stats[home]["pts"] += 1
            stats[away]["pts"] += 1
        else:
            stats[away]["pts"] += 3
    return stats


def _compute_advancement_state(adv_results: List[Dict]) -> Dict[str, Dict]:
    """Return advancement state per team.

    Each entry: {
        'group_result':  'advanced' | 'eliminated' | None,
        'group_pos':     1..4 | None,
        'ko_wins':       int,
        'ko_eliminated': bool,
    }

    group_result=None means the group is incomplete or the team is a third-placer
    whose R32 fate hasn't been decided yet (not all 12 groups complete).

    ko_wins counts knockout-match victories. Mapping to stages:
        0 wins + advanced from groups → reached R32
        1 win  → reached R16 (won R32 tie)
        2 wins → reached QF
        3 wins → reached SF
        4 wins → reached F
        5 wins → won the tournament
    """
    team_to_group = _team_to_group_map()
    standings = _compute_group_standings(adv_results, team_to_group)

    group_played: Dict[str, int] = defaultdict(int)
    for m in adv_results:
        home = canonical(m.get("home") or "")
        away = canonical(m.get("away") or "")
        gh = team_to_group.get(home)
        ga = team_to_group.get(away)
        if gh is not None and gh == ga:
            group_played[gh] += 1

    state: Dict[str, Dict] = {
        t: {"group_result": None, "group_pos": None, "ko_wins": 0, "ko_eliminated": False}
        for t in team_to_group
    }

    third_place: List[str] = []
    for letter, teams in WC2026_GROUPS.items():
        if group_played.get(letter, 0) < 6:
            continue  # group not complete
        team_list = [canonical(t) for t in teams]
        sorted_t = sorted(
            team_list,
            key=lambda t: (-standings[t]["pts"], -standings[t]["gd"], -standings[t]["gf"]),
        )
        state[sorted_t[0]]["group_result"] = "advanced"
        state[sorted_t[0]]["group_pos"] = 1
        state[sorted_t[1]]["group_result"] = "advanced"
        state[sorted_t[1]]["group_pos"] = 2
        state[sorted_t[2]]["group_pos"] = 3
        third_place.append(sorted_t[2])
        state[sorted_t[3]]["group_result"] = "eliminated"
        state[sorted_t[3]]["group_pos"] = 4

    # With 12 groups, top-2 = 24 teams. Best 8 of 12 third-placers = 32 total.
    n_complete = sum(1 for letter in WC2026_GROUPS if group_played.get(letter, 0) >= 6)
    if n_complete == len(WC2026_GROUPS):
        ranked_thirds = sorted(
            third_place,
            key=lambda t: (-standings[t]["pts"], -standings[t]["gd"], -standings[t]["gf"]),
        )
        n_best_thirds = 8
        for t in ranked_thirds[:n_best_thirds]:
            state[t]["group_result"] = "advanced"
        for t in ranked_thirds[n_best_thirds:]:
            state[t]["group_result"] = "eliminated"
    # else: third-placers stay group_result=None (open) until all groups finish

    # Process knockout matches (non-group match pairs)
    for m in adv_results:
        home = canonical(m.get("home") or "")
        away = canonical(m.get("away") or "")
        gh = team_to_group.get(home)
        ga = team_to_group.get(away)
        if gh is not None and gh == ga:
            continue  # skip group matches
        hg = int(m.get("hg", 0))
        ag = int(m.get("ag", 0))
        if hg > ag:
            if home in state:
                state[home]["ko_wins"] += 1
            if away in state:
                state[away]["ko_eliminated"] = True
        elif ag > hg:
            if away in state:
                state[away]["ko_wins"] += 1
            if home in state:
                state[home]["ko_eliminated"] = True
        # hg == ag: genuine draw impossible in knockout (ET+PK always decides);
        # leave both teams' states unchanged (prediction stays open).

    return state


def _parse_adv_team(selection: str, stage: str) -> str:
    """Strip the stage suffix from a selection like 'Brazil R16' → 'Brazil'."""
    sel = (selection or "").strip()
    stage_s = (stage or "").strip()
    if stage_s and sel.endswith(stage_s):
        team = sel[: -len(stage_s)].strip()
        return team
    return sel


def _decide_advancement(
    selection: str, stage: str, adv_state: Dict[str, Dict]
) -> Optional[str]:
    """Determine settlement for an advancement prediction.

    Returns 'won' | 'lost' | None (None → stays open).

    Stage order (index = number of ko wins needed to reach it after group adv.):
        R32=0, R16=1, QF=2, SF=3, F=4, win=5
    """
    stage_s = (stage or "").strip()
    team = canonical(_parse_adv_team(selection, stage_s))
    if not team:
        return None

    s = adv_state.get(team)
    if s is None:
        return None  # team not in known WC structure

    group_result = s["group_result"]
    ko_wins = s["ko_wins"]
    ko_eliminated = s["ko_eliminated"]

    # group_winner: independent of knockout stages
    if stage_s == "group_winner":
        if group_result is None:
            return None  # group not resolved
        return "won" if s["group_pos"] == 1 else "lost"

    if stage_s not in STAGE_ORDER:
        return None

    stage_idx = STAGE_ORDER.index(stage_s)

    if group_result == "eliminated":
        return "lost"

    if group_result == "advanced":
        # ko_wins=0 → reached R32 (stage_idx 0), ko_wins=1 → reached R16, …
        if ko_wins >= stage_idx:
            return "won"
        if ko_eliminated:
            # knocked out before reaching target stage
            return "lost"
        return None  # still alive; haven't reached target stage yet → open

    # group_result is None: group incomplete or third-place not decided
    return None


# ---------------------------------------------------------------------------
# Per-market settlement rules
# ---------------------------------------------------------------------------


def _settle_1x2(selection: str, result: Dict) -> Optional[str]:
    outcome = result.get("outcome") or ""
    if not outcome:
        return None
    return "won" if selection.strip().lower() == outcome else "lost"


def _settle_scoreline(selection: str, result: Dict) -> Optional[str]:
    expected = f"{result['home_goals']}-{result['away_goals']}"
    return "won" if selection.strip() == expected else "lost"


def _settle_ou(selection: str, line: float, result: Dict) -> Optional[str]:
    if line < 0:
        return None  # missing line sentinel
    total = result["home_goals"] + result["away_goals"]
    sel = selection.strip().lower()
    if total > line:
        return "won" if sel == "over" else "lost"
    if total < line:
        return "won" if sel == "under" else "lost"
    # total == line: integer push
    return "push"


def _settle_btts(selection: str, result: Dict) -> Optional[str]:
    both_scored = result["home_goals"] > 0 and result["away_goals"] > 0
    sel = selection.strip().lower()
    if sel == "yes":
        return "won" if both_scored else "lost"
    if sel == "no":
        return "won" if not both_scored else "lost"
    return None


# ---------------------------------------------------------------------------
# Main settle pass
# ---------------------------------------------------------------------------


def settle_open(
    results: List[Dict],
    adv_results: List[Dict],
    db: str,
) -> int:
    """Settle all open predictions in the prediction ledger.

    Parameters
    ----------
    results:
        Entries from ``wc2026_results.json["results"]`` — each has
        ``fixture``, ``score``, ``outcome``.
    adv_results:
        Entries from ``advancement_played_results.json`` — each has
        ``home``, ``away``, ``hg``, ``ag``.
    db:
        Path to the SQLite database containing the ``predictions`` table.

    Returns
    -------
    int
        Number of prediction rows that transitioned from 'open' to a
        terminal status (won/lost/push).
    """
    results_index = _build_results_index(results)
    adv_state = _compute_advancement_state(adv_results)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    n_settled = 0
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT prediction_id, fixture, market, selection, line, stage "
            "FROM predictions WHERE status='open'"
        ).fetchall()

        updates = []
        for row in rows:
            pid = row["prediction_id"]
            market = (row["market"] or "").strip()
            selection = (row["selection"] or "").strip()
            line = row["line"] if row["line"] is not None else -1.0
            stage = (row["stage"] or "").strip()
            fixture = row["fixture"] or ""

            new_status: Optional[str] = None
            source = ""

            if market == "advancement":
                new_status = _decide_advancement(selection, stage, adv_state)
                source = "advancement_json"
            else:
                fkey = _fixture_key(fixture)
                result = results_index.get(fkey) if fkey else None
                if result is None:
                    continue

                if market == "1x2":
                    new_status = _settle_1x2(selection, result)
                    source = "results_json"
                elif market == "scoreline":
                    new_status = _settle_scoreline(selection, result)
                    source = "results_json"
                elif market.startswith("ou_"):
                    new_status = _settle_ou(selection, float(line), result)
                    source = "results_json"
                elif market == "btts":
                    new_status = _settle_btts(selection, result)
                    source = "results_json"

            if new_status is not None:
                updates.append((new_status, now, source, pid))

        with conn:
            for new_status, ts, source, pid in updates:
                cur = conn.execute(
                    "UPDATE predictions SET status=?, settled_ts=?, settle_source=? "
                    "WHERE prediction_id=? AND status='open'",
                    (new_status, ts, source, pid),
                )
                n_settled += cur.rowcount

    finally:
        conn.close()

    logger.info("settle_open: settled %d predictions", n_settled)
    return n_settled
