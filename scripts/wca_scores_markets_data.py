#!/usr/bin/env python3
"""Generate the enhanced Scores & Markets feed (``site/scores_markets.json``).

Powers the overhauled "Scorelines · Exposure · Best Price" panel: model markets
for every upcoming game of the current stage (group) plus a projected next stage
(Round of 32), organised so the site can pivot the view by **stage** or by
**team**.

Scorelines (1X2 / top scoreline / O-U 2.5 / BTTS / xG) are computed fresh from
the Elo + Dixon-Coles fit. Advancement probabilities, group standings and the
projected R32 qualifiers are **reused** from ``site/advancement_data.json`` (the
same numbers the advancement panel shows) so the two panels never disagree and
we avoid re-running the Monte-Carlo sim on every refresh.

Run hourly alongside the other site feeds (see .github/workflows/hourly-odds.yml).

    python scripts/wca_scores_markets_data.py [--out site/scores_markets.json]
        [--advancement site/advancement_data.json]
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from wca.card import fit_models, dc_probs, elo_probs  # noqa: E402
from wca.data.cleaning import resolve_results_path  # noqa: E402
from wca.advancement import WC2026_GROUPS  # noqa: E402
from wca.sim.tournament2026 import (  # noqa: E402
    R32_TIES,
    KNOCKOUT_FEED,
    thirds_assignment,
)

GROUP_LETTERS = list("ABCDEFGHIJKL")
STAGES = [
    ("group", "Group Stage"),
    ("r32", "Round of 32"),
    ("r16", "Round of 16"),
    ("qf", "Quarter-finals"),
    ("sf", "Semi-finals"),
    ("final", "Final"),
]


def _market(models, home: str, away: str) -> Dict[str, Any]:
    """Model FT markets for one fixture (neutral WC venue)."""
    mat, lam_h, lam_a = models.dc.score_matrix(home, away, neutral=True, warn=False)
    gx, gy = np.indices(mat.shape)
    dc = [
        float(np.tril(mat, -1).sum()),  # home win
        float(np.trace(mat)),            # draw
        float(np.triu(mat, 1).sum()),    # away win
    ]
    elo = elo_probs(models, home, away, True)
    blend = [round((dc[i] + elo[i]) / 2.0, 4) for i in range(3)]
    flat = [((x, y), float(mat[x, y])) for x in range(mat.shape[0]) for y in range(mat.shape[1])]
    (tx, ty), tp = max(flat, key=lambda z: z[1])
    return {
        "x1x2": blend,
        "top": f"{tx}-{ty}",
        "topp": round(tp, 3),
        "over25": round(float(mat[(gx + gy) >= 3].sum()), 3),
        "btts": round(float(mat[(gx >= 1) & (gy >= 1)].sum()), 3),
        "eg": [round(lam_h, 2), round(lam_a, 2)],
    }


def _load_advancement(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"teams": [], "groups": {}}
    with open(path) as fh:
        return json.load(fh)


# Knockout round windows for the fixed 2026 schedule (venues/dates are set; only
# the teams change as results land). A KO fixture is bucketed to its round purely
# by its calendar date, so the feed stays correct as later rounds appear in the
# results spine — no per-round bracket resolution needed.
_KO_WINDOWS = [
    ("r32", datetime.date(2026, 7, 3)),
    ("r16", datetime.date(2026, 7, 8)),
    ("qf", datetime.date(2026, 7, 12)),
    ("sf", datetime.date(2026, 7, 16)),
    # 2026-07-18 is the 3rd-place play-off — intentionally not a headline stage.
    ("final", datetime.date(2026, 7, 31)),
]
_KO_LABEL = {"r32": "Round of 32", "r16": "Round of 16", "qf": "Quarter-finals",
             "sf": "Semi-finals", "final": "Final"}


def _ko_round(d: datetime.date) -> Optional[str]:
    """Map a knockout fixture's date to its round key, or None (e.g. 3rd-place)."""
    for key, cutoff in _KO_WINDOWS:
        if d <= cutoff:
            if key == "final" and d < datetime.date(2026, 7, 19):
                return None  # 3rd-place play-off window — skip
            return key
    return None


# Which output key each bracket match number lands in.
_R32_MATCH_NOS = tuple(t[0] for t in R32_TIES)
_MATCH_ROUND_KEY: Dict[int, str] = {}
for _mno in _R32_MATCH_NOS:
    _MATCH_ROUND_KEY[_mno] = "r32"
for _mno in range(89, 97):
    _MATCH_ROUND_KEY[_mno] = "r16"
for _mno in range(97, 101):
    _MATCH_ROUND_KEY[_mno] = "qf"
for _mno in (101, 102):
    _MATCH_ROUND_KEY[_mno] = "sf"
_MATCH_ROUND_KEY[104] = "final"


def _best_eight_thirds(standings: Dict[str, Any]) -> Dict[str, str]:
    """Rank the 12 third-placed teams and return {winner_slot_group: third_group}.

    Each group's third-placed team is its ``pos==3`` standings row. The 12 are
    ranked by (pts, gd, gf) descending; the top 8 group letters' thirds advance,
    and :func:`thirds_assignment` maps each of the 8 winner slots to the group
    whose third fills it (official FIFA allocation).
    """
    thirds = []
    for g in GROUP_LETTERS:
        rows = standings.get(g) or []
        row3 = next((r for r in rows if r.get("pos") == 3), None)
        if row3 is None:
            continue
        thirds.append((g, row3.get("pts", 0), row3.get("gd", 0), row3.get("gf", 0)))
    if len(thirds) < 8:
        return {}
    thirds.sort(key=lambda z: (z[1], z[2], z[3]), reverse=True)
    top8 = sorted(t[0] for t in thirds[:8])
    return thirds_assignment(top8)


def _team_at(standings: Dict[str, Any], group: str, pos: int) -> Optional[str]:
    rows = standings.get(group) or []
    row = next((r for r in rows if r.get("pos") == pos), None)
    return row.get("team") if row else None


def _ft_winner(home: str, away: str, ft: Optional[str]) -> Optional[str]:
    """Decisive winner from a knockout FT string (KO ties can't end level)."""
    if not ft:
        return None
    try:
        hg, ag = (int(x) for x in str(ft).split("-", 1))
    except (ValueError, TypeError):
        return None
    if hg > ag:
        return home
    if ag > hg:
        return away
    return None  # a level FT means the tie went to ET/pens we can't read here


def _projected_bracket(
    models,
    standings: Dict[str, Any],
    actual_ties: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Build the full knockout bracket, priced with the same rich markets.

    The bracket is *anchored on real results* wherever the results spine has
    them: R32 matchups are determined directly from the final standings + the
    best-8-thirds allocation; each later round chains forward from the ACTUAL
    winner of a played tie (if the spine has it), else from the model's modal
    winner. So the projection never contradicts a result that already happened.

    Each tie is flagged ``projected`` = True only when its *matchup* is
    model-inferred (an upstream tie is still unplayed). R32 ties are determined
    (``projected`` False); a later tie whose two participants are both already
    decided by real results is likewise not projected.

    ``actual_ties`` maps a round key ("r32".."final") to the spine's ties for
    that round, each ``{"home","away","ft","date"}``; used to seed winners and
    to carry real FT/date onto the matching bracket slot.

    Returns ``{"r32":[...16], "r16":[...8], "qf":[...4], "sf":[...2],
    "final":[...1]}``; an empty dict if standings are incomplete.
    """
    thirds = _best_eight_thirds(standings)
    if not thirds:
        return {}
    actual_ties = actual_ties or {}

    def side_team(side: tuple) -> Optional[str]:
        kind, g = side
        if kind == "W":
            return _team_at(standings, g, 1)
        if kind == "R":
            return _team_at(standings, g, 2)
        if kind == "T":  # third-placed team allocated to the winner-of-g slot
            third_group = thirds.get(g)
            return _team_at(standings, third_group, 3) if third_group else None
        return None

    # Index the spine's actual ties by unordered participant pair, per round.
    actual_index: Dict[str, Dict[frozenset, Dict[str, Any]]] = {}
    for rnd_key, ties in actual_ties.items():
        idx = {}
        for t in ties:
            idx[frozenset((t["home"], t["away"]))] = t
        actual_index[rnd_key] = idx

    out: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("r32", "r16", "qf", "sf", "final")}
    winners: Dict[int, str] = {}        # match_no -> resolved winner (actual or modal)
    winner_known: Dict[int, bool] = {}  # match_no -> winner comes from a real, decided FT

    def add_tie(match_no: int, home: str, away: str, matchup_determined: bool) -> None:
        rnd_key = _MATCH_ROUND_KEY[match_no]
        actual = actual_index.get(rnd_key, {}).get(frozenset((home, away)))
        m = _market(models, home, away)
        # Modal winner from 90-min home vs away win prob (draw ignored); an
        # actual, decisive FT overrides it when the tie has been played.
        modal = home if m["x1x2"][0] >= m["x1x2"][2] else away
        ft = actual.get("ft") if actual else None
        date = actual.get("date") if actual else None
        real_winner = _ft_winner(home, away, ft)
        winners[match_no] = real_winner or modal
        winner_known[match_no] = real_winner is not None
        # 'projected' iff the *matchup* is model-inferred. R32 matchups are fixed
        # by the finished group stage; a later matchup is real only when BOTH its
        # feeder ties have a decided winner.
        m.update({
            "home": home, "away": away, "date": date, "group": None,
            "round": _KO_LABEL[rnd_key], "ft": ft,
            "match_no": match_no, "projected": not matchup_determined,
        })
        out[rnd_key].append(m)

    # R32: matchups determined by the finished group stage (projected=False).
    for match_no, side_a, side_b in R32_TIES:
        home, away = side_team(side_a), side_team(side_b)
        if not home or not away:
            return {}  # incomplete standings — bail rather than emit half a bracket
        add_tie(match_no, home, away, matchup_determined=True)

    # R16..Final: chain forward. A tie's matchup is real only when both feeder
    # ties have a decided winner; otherwise it's a model-projected pairing.
    for match_no, src_a, src_b in KNOCKOUT_FEED:
        home, away = winners.get(src_a), winners.get(src_b)
        if not home or not away:
            return {}
        matchup_determined = winner_known.get(src_a, False) and winner_known.get(src_b, False)
        add_tie(match_no, home, away, matchup_determined=matchup_determined)

    return out


def build(results_path: str, advancement_path: str) -> Dict[str, Any]:
    adv = _load_advancement(advancement_path)
    team_adv = {t["team"]: t.get("model", {}) for t in adv.get("teams", [])}
    standings = adv.get("groups", {})
    team_group = {t: g for g, ts in WC2026_GROUPS.items() for t in ts}

    models = fit_models(pd.read_csv(results_path), half_life_years=8.0)

    df = pd.read_csv(results_path)
    df["_d"] = pd.to_datetime(df["date"], errors="coerce")
    wc = df[(df["tournament"] == "FIFA World Cup") & (df["_d"].dt.year == 2026)]

    # --- current stage: ALL group games (completed + upcoming) with model markets ---
    group_games: List[Dict[str, Any]] = []
    n_upcoming = 0
    for _, r in wc.sort_values("_d").iterrows():
        h, a = r["home_team"], r["away_team"]
        g = team_group.get(h)
        if not g or g != team_group.get(a):
            continue  # not a same-group fixture (knockout placeholder etc.)
        m = _market(models, h, a)
        ft: Optional[str] = None
        if pd.notna(r.get("home_score")) and pd.notna(r.get("away_score")):
            ft = f"{int(r['home_score'])}-{int(r['away_score'])}"
        else:
            n_upcoming += 1
        m.update({"home": h, "away": a, "date": str(r["_d"].date()), "group": g, "ft": ft})
        group_games.append(m)

    # --- knockout stages: concrete ties from the results spine (FT-accurate) ---
    # A cross-group WC2026 fixture is a knockout tie; bucket it to its round by
    # date and price the SAME model markets as the group rows. Rounds populate as
    # results land (R16 teams are only known once R32 completes), so this stays
    # accurate to the latest FT with no timed job.
    ko_upcoming: Dict[str, int] = {k: 0 for k, _ in _KO_WINDOWS}
    actual_ties: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _ in _KO_WINDOWS}
    for _, r in wc.sort_values("_d").iterrows():
        h, a = r["home_team"], r["away_team"]
        gh, ga = team_group.get(h), team_group.get(a)
        if gh and ga and gh == ga:
            continue  # same-group => already handled as a group game
        if pd.isna(r["_d"]):
            continue
        rnd = _ko_round(r["_d"].date())
        if rnd is None:
            continue
        ft = None
        if pd.notna(r.get("home_score")) and pd.notna(r.get("away_score")):
            ft = f"{int(r['home_score'])}-{int(r['away_score'])}"
        else:
            ko_upcoming[rnd] += 1
        actual_ties[rnd].append({"home": h, "away": a, "ft": ft, "date": str(r["_d"].date())})

    # --- full knockout bracket: rich per-tie markets for EVERY round ----------
    # One self-consistent bracket, anchored on real results. R32 matchups are
    # determined by the finished group stage; each later round chains from the
    # ACTUAL winner of a played tie where the spine has it, else from the model's
    # modal winner. Every tie carries the SAME rich markets as the group rows,
    # and real FT/date land on the matching slot. See _projected_bracket.
    ko_games: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _ in _KO_WINDOWS}
    bracket = _projected_bracket(models, standings, actual_ties)
    for rnd in ("r32", "r16", "qf", "sf", "final"):
        if bracket.get(rnd):
            ko_games[rnd] = bracket[rnd]
        elif actual_ties.get(rnd):
            # Fallback (standings incomplete → no bracket): use spine ties as-is.
            ko_games[rnd] = actual_ties[rnd]
    # A round is "projected" (model-inferred matchups) when at least one of its
    # ties is flagged projected. R32 is determined, never projected.
    projected_rounds = [
        rnd for rnd in ("r16", "qf", "sf", "final")
        if ko_games[rnd] and any(g.get("projected") for g in ko_games[rnd])
    ]
    r32_is_projected = bool(ko_games["r32"]) and any(
        g.get("projected") for g in ko_games["r32"]
    )

    # --- next stage: projected R32 qualifiers (top-2 per group standings) ---
    # Retained for backward-compat; the R32 tab now renders the rich 16-tie
    # breakout above, so this sparse grid is a fallback only.
    r32_projected = []
    for g in GROUP_LETTERS:
        rows = standings.get(g) or []
        top2 = [
            {"team": x["team"], "pos": x["pos"], "pts": x.get("pts"),
             "p_adv": round(team_adv.get(x["team"], {}).get("R32", 0.0), 3)}
            for x in rows[:2]
        ]
        if top2:
            r32_projected.append({"group": g, "teams": top2})

    # --- by-team: all of a team's games (group + knockout) + advancement line ---
    all_games = group_games + [g for gs in ko_games.values() for g in gs]
    by_team: Dict[str, Any] = {}
    for team, g in sorted(team_group.items()):
        games = [gg for gg in all_games if gg["home"] == team or gg["away"] == team]
        by_team[team] = {
            "group": g,
            "adv": {k: round(v, 3) for k, v in team_adv.get(team, {}).items()},
            "games": games,
        }

    # stage availability & status. Every round now has games (actual ties from the
    # results spine, else a projection), so no round is 'locked'. Statuses:
    #   done      — all its games have a final score (FT-in)
    #   current   — earliest round still holding an actual unplayed fixture
    #   next      — the determined-but-unplayed round (R32 once groups finish)
    #   projected — model-projected matchups (R16..Final before they're reached)
    # 'projected' rounds are clickable; the frontend labels them honestly.
    stage_upcoming = {"group": n_upcoming, "r32": ko_upcoming["r32"], "r16": ko_upcoming["r16"],
                      "qf": ko_upcoming["qf"], "sf": ko_upcoming["sf"], "final": ko_upcoming["final"]}
    stage_has = {key: bool(ko_games[key]) if key != "group" else bool(group_games)
                 for key, _ in STAGES}
    is_projected = {"group": False, "r32": r32_is_projected,
                    "r16": "r16" in projected_rounds, "qf": "qf" in projected_rounds,
                    "sf": "sf" in projected_rounds, "final": "final" in projected_rounds}
    # 'current' = earliest round with an *actual* (non-projected) unplayed game.
    current_key = next(
        (k for k, _ in STAGES if stage_has[k] and stage_upcoming[k] > 0 and not is_projected[k]),
        None,
    )
    stages = []
    for key, label in STAGES:
        if not stage_has[key]:
            status, count = "locked", 0
        elif is_projected[key]:
            # R16..Final: model-projected matchups. Clickable; UI flags them.
            status, count = "projected", len(ko_games[key])
        elif key == current_key:
            status, count = "current", stage_upcoming[key]  # badge: "● N left"
        elif key == "r32" and stage_upcoming[key] == 0 and not any(g.get("ft") for g in ko_games[key]):
            # R32 matchups determined (groups done) but no tie kicked off yet.
            status, count = "next", len(ko_games[key])
        elif stage_upcoming[key] == 0:
            status, count = "done", 0
        else:
            status, count = "next", stage_upcoming[key]
        stages.append({"key": key, "label": label, "status": status, "count": count})

    return {
        "meta": {
            "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "n_games": len(group_games),
            "n_upcoming": n_upcoming,
            "n_ko_games": sum(len(v) for v in ko_games.values()),
            "source": "Elo+DC scorelines · advancement reused from advancement_data.json",
        },
        "stages": stages,
        "projected_rounds": projected_rounds,
        "group_games": group_games,
        "r32_projected": r32_projected,
        "r32_games": ko_games["r32"],
        "r16_games": ko_games["r16"],
        "qf_games": ko_games["qf"],
        "sf_games": ko_games["sf"],
        "final_games": ko_games["final"],
        "by_team": by_team,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build site/scores_markets.json")
    p.add_argument("--out", default="site/scores_markets.json")
    p.add_argument("--advancement", default="site/advancement_data.json")
    p.add_argument("--results", default=None, help="override results path")
    args = p.parse_args(argv)

    results_path = args.results or resolve_results_path()
    data = build(results_path, args.advancement)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(
        f"[scores_markets] wrote {args.out}: {data['meta']['n_games']} group games, "
        f"{len(data['r32_projected'])} projected R32 groups, {len(data['by_team'])} teams"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
