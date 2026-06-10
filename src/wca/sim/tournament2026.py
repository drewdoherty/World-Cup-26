"""Monte Carlo simulator for the 2026 FIFA World Cup (48 teams, 104 matches).

Format and bracket
-------------------
The 2026 tournament is the first 48-team World Cup. The 48 teams are drawn into
**12 groups of four** (groups ``A`` through ``L``). Each group plays a single
round-robin (6 matches per group, 72 group matches in total). The **top two of
each group** (24 teams) plus the **eight best third-placed teams** advance to a
**Round of 32** (16 ties), followed by a Round of 16, quarter-finals,
semi-finals, a third-place play-off and the final -- 104 matches in all.

Group-stage tie-breakers (FIFA 2026 regulations, in order)
----------------------------------------------------------
For teams level on points the criteria are applied in this order:

1. greatest number of points in head-to-head matches among the tied teams;
2. superior goal difference in those head-to-head matches;
3. greatest number of goals scored in those head-to-head matches;
4. (if a subset is still level, criteria 1-3 are re-applied to that subset);
5. superior goal difference in **all** group matches;
6. greatest number of goals scored in **all** group matches;
7. fewest disciplinary points ("fair play" / team-conduct score);
8. position in the FIFA World Ranking.

The 2026 edition moved head-to-head *ahead of* overall goal difference and
replaced the historical "drawing of lots" final tie-breaker with the FIFA World
Ranking. This module implements points -> H2H(points, GD, goals) -> overall GD
-> overall goals scored, and then breaks any residual tie with an optional
per-team ``fair_play`` / ``fifa_rank`` ordering supplied by the caller, falling
back to a deterministic RNG draw (which stands in for the disciplinary and FIFA
ranking criteria that are not modelled at goal-only resolution). See
``_APPROXIMATIONS`` in the source and the caveats in the project notes.

Third-placed-team ranking (FIFA 2026)
-------------------------------------
The 12 third-placed teams are ranked by: points, goal difference, goals scored,
team-conduct score, FIFA World Ranking. The top eight qualify. This module
ranks by points -> GD -> goals scored, then the same optional
``fair_play``/``fifa_rank``/RNG fallback as above.

Round-of-32 bracket and the eight thirds
----------------------------------------
The 16 R32 ties are fixed by *group letter* (not by seeding); the only thing
decided after the group stage is *which* group's third-placed team fills each of
the eight "winner-vs-third" slots. FIFA publishes an allocation table with one
row for every C(12, 8) = 495 combination of groups whose third-placed team
qualifies. That official table is embedded below in ``THIRDS_ALLOCATION`` and
was transcribed from the Wikipedia template (which mirrors the FIFA
regulations, Annex C).

The eight "winner-vs-third" winner slots, *in the column order used by the
allocation table*, are::

    (1A, 1B, 1D, 1E, 1G, 1I, 1K, 1L)

The 16 R32 ties (match numbers 73-88) are::

    73: 2A v 2B    74: 1E v 3rd   75: 1F v 2C    76: 1C v 2F
    77: 1I v 3rd   78: 2E v 2I    79: 1A v 3rd   80: 1L v 3rd
    81: 1D v 3rd   82: 1G v 3rd   83: 2K v 2L    84: 1H v 2J
    85: 1B v 3rd   86: 1J v 2H    87: 1K v 3rd   88: 2D v 2G

The knockout feed (winner of match X meets winner of match Y)::

    R16:  89:W74-W77  90:W73-W75  91:W76-W78  92:W79-W80
          93:W83-W84  94:W81-W82  95:W86-W88  96:W85-W87
    QF:   97:W89-W90  98:W93-W94  99:W91-W92 100:W95-W96
    SF:  101:W97-W98 102:W99-W100
    3rd: 103:L101-L102            Final: 104:W101-W102

Sources (accessed 2026-06-10)
-----------------------------
* https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_knockout_stage
* https://en.wikipedia.org/wiki/Template:2026_FIFA_World_Cup_third-place_table
* https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/knockout-stage-match-schedule-bracket
* https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/groups-how-teams-qualify-tie-breakers

Knockout draw resolution
------------------------
``prob_fn(team_a, team_b, knockout)`` returns 90-minute ``(p_a, p_draw, p_b)``.
In the knockout rounds a 90-minute draw is taken to extra time / penalties; the
winner is drawn with probability biased toward the stronger side. With the
default ``et_skill_weight = 0.5`` the conditional win probability for ``team_a``
given a 90-minute draw is

    p_a_et = 0.5 + et_skill_weight * (q_a - 0.5)

where ``q_a = p_a / (p_a + p_b)`` is ``team_a``'s share of the 90-minute decisive
probability mass. ``et_skill_weight = 0`` makes every shoot-out a coin flip;
``et_skill_weight = 1`` reuses the full 90-minute strength ratio.

Notes on approximations are collected in ``_APPROXIMATIONS``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import numpy as np

_APPROXIMATIONS = (
    "Group tie-breaks implement points -> head-to-head(points, GD, goals) -> "
    "overall GD -> overall goals scored. Disciplinary/fair-play points and FIFA "
    "World Ranking are not simulated from match events; residual ties are broken "
    "by an optional per-team 'fair_play'/'fifa_rank' ordering if supplied, else "
    "by a deterministic RNG draw. Match goals are sampled from independent "
    "Poisson scorelines whose total expected goals and win/draw split are "
    "calibrated to prob_fn, so head-to-head GD/goals are realistic but not "
    "drawn from prob_fn's exact scoreline distribution.",
)

# Probability-function signature:
#   prob_fn(team_a, team_b, knockout) -> (p_a_win, p_draw, p_b_win)
ProbFn = Callable[[str, str, bool], Tuple[float, float, float]]

GROUP_LETTERS: Tuple[str, ...] = tuple("ABCDEFGHIJKL")

# The six round-robin orderings of a 4-team group, as (home_idx, away_idx).
GROUP_FIXTURE_PAIRS: Tuple[Tuple[int, int], ...] = (
    (0, 1),
    (2, 3),
    (0, 2),
    (1, 3),
    (0, 3),
    (1, 2),
)

# Winner slots (group letters) that face a third-placed team in the R32, in the
# exact column order used by the official allocation table.
THIRDS_SLOT_WINNERS: Tuple[str, ...] = ("A", "B", "D", "E", "G", "I", "K", "L")


# ---------------------------------------------------------------------------
# Round-of-32 bracket definition.
# ---------------------------------------------------------------------------
# Each entry is (match_no, side_a, side_b) where each side is one of:
#   ("W", "A")   -> winner of group A
#   ("R", "B")   -> runner-up of group B
#   ("T", "A")   -> the third-placed team allocated to the slot whose winner is
#                   group A (resolved via THIRDS_ALLOCATION)
R32_TIES: Tuple[Tuple[int, Tuple[str, str], Tuple[str, str]], ...] = (
    (73, ("R", "A"), ("R", "B")),
    (74, ("W", "E"), ("T", "E")),
    (75, ("W", "F"), ("R", "C")),
    (76, ("W", "C"), ("R", "F")),
    (77, ("W", "I"), ("T", "I")),
    (78, ("R", "E"), ("R", "I")),
    (79, ("W", "A"), ("T", "A")),
    (80, ("W", "L"), ("T", "L")),
    (81, ("W", "D"), ("T", "D")),
    (82, ("W", "G"), ("T", "G")),
    (83, ("R", "K"), ("R", "L")),
    (84, ("W", "H"), ("R", "J")),
    (85, ("W", "B"), ("T", "B")),
    (86, ("W", "J"), ("R", "H")),
    (87, ("W", "K"), ("T", "K")),
    (88, ("R", "D"), ("R", "G")),
)

# Knockout feed: match_no -> (source_match_a, source_match_b) using the WINNERS.
KNOCKOUT_FEED: Tuple[Tuple[int, int, int], ...] = (
    # Round of 16 (89-96)
    (89, 74, 77),
    (90, 73, 75),
    (91, 76, 78),
    (92, 79, 80),
    (93, 83, 84),
    (94, 81, 82),
    (95, 86, 88),
    (96, 85, 87),
    # Quarter-finals (97-100)
    (97, 89, 90),
    (98, 93, 94),
    (99, 91, 92),
    (100, 95, 96),
    # Semi-finals (101-102)
    (101, 97, 98),
    (102, 99, 100),
    # Final (104). The third-place play-off (103) is omitted from progression.
    (104, 101, 102),
)

# Match numbers grouped by round, for reporting reached-stage probabilities.
ROUND_MATCHES: Dict[str, Tuple[int, ...]] = {
    "R32": tuple(t[0] for t in R32_TIES),
    "R16": tuple(range(89, 97)),
    "QF": tuple(range(97, 101)),
    "SF": (101, 102),
    "F": (104,),
}


def standard_groups(team_names: Optional[Mapping[str, Sequence[str]]] = None) -> Dict[str, List[str]]:
    """Return a placeholder ``group -> [4 teams]`` mapping.

    With ``team_names`` ``None`` the teams are named ``"A1".."L4"``. This is a
    convenience for tests / smoke runs; real usage passes the drawn groups.
    """

    if team_names is not None:
        return {g: list(team_names[g]) for g in GROUP_LETTERS}
    return {g: [f"{g}{i + 1}" for i in range(4)] for g in GROUP_LETTERS}


@dataclass(frozen=True)
class Fixture:
    """A single scheduled match between two named teams.

    ``group`` is the group letter for group-stage matches, or ``None`` for
    knockout matches (which this simulator generates internally and so does not
    usually receive as input).
    """

    home: str
    away: str
    group: Optional[str] = None


@dataclass(frozen=True)
class Result:
    """A played group-stage match with a final 90-minute scoreline."""

    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass
class SimulationResult:
    """Output of :meth:`TournamentSimulator.simulate`.

    All probability arrays are indexed by team name. ``group_position`` maps a
    team to a length-4 array ``[P(1st), P(2nd), P(3rd), P(4th)]``. ``reach`` maps
    a stage label to a scalar probability that the team reaches that stage, and
    ``win`` is the probability of winning the tournament.
    """

    teams: List[str]
    n_sims: int
    group_position: Dict[str, np.ndarray]
    reach: Dict[str, Dict[str, float]] = field(default_factory=dict)
    win: Dict[str, float] = field(default_factory=dict)

    def as_dataframe(self):  # pragma: no cover - convenience only
        """Return a tidy ``pandas.DataFrame`` summary (one row per team)."""

        import pandas as pd

        rows = []
        for team in self.teams:
            gp = self.group_position[team]
            row = {
                "team": team,
                "P_1st": gp[0],
                "P_2nd": gp[1],
                "P_3rd": gp[2],
                "P_4th": gp[3],
            }
            for stage in ("R32", "R16", "QF", "SF", "F"):
                row[f"P_{stage}"] = self.reach.get(stage, {}).get(team, 0.0)
            row["P_win"] = self.win.get(team, 0.0)
            rows.append(row)
        return pd.DataFrame(rows).set_index("team")


def thirds_assignment(qualified_groups: Sequence[str]) -> Dict[str, str]:
    """Map each winner slot to the group whose third-placed team it faces.

    ``qualified_groups`` is the set of 8 group letters whose third-placed teams
    advanced. Returns ``{winner_group_letter: third_place_group_letter}`` using
    the official FIFA allocation table (495 combinations).

    Raises ``KeyError`` if the combination is not exactly 8 distinct valid
    groups present in the table.
    """

    key = "".join(sorted(set(qualified_groups)))
    if len(key) != 8:
        raise KeyError(
            f"expected 8 distinct qualifying groups, got {sorted(set(qualified_groups))!r}"
        )
    assign = THIRDS_ALLOCATION[key]
    return {slot: third for slot, third in zip(THIRDS_SLOT_WINNERS, assign)}


class TournamentSimulator:
    """Vectorised Monte Carlo simulator for the full 2026 World Cup.

    Parameters
    ----------
    groups
        ``{group_letter: [team0, team1, team2, team3]}`` for all 12 groups.
    prob_fn
        ``prob_fn(team_a, team_b, knockout) -> (p_a, p_draw, p_b)`` giving the
        90-minute outcome probabilities. Must be deterministic for a given pair
        (it is queried once per ordered pair and cached).
    results
        Already-played group-stage matches, used to constrain re-simulation
        mid-tournament. Each :class:`Result` fixes one group match's scoreline;
        unplayed group matches are simulated.
    et_skill_weight
        Knockout extra-time / penalty skill bias in ``[0, 1]`` (see module
        docstring). Default ``0.5``.
    mean_goals
        Baseline expected total goals per match used to convert ``prob_fn`` into
        Poisson scoring rates for the two sides (affects only GD/goals
        tie-break realism, never who wins/draws). Default ``2.7``.
    fair_play / fifa_rank
        Optional ``{team: value}`` orderings used as deterministic tie-breakers
        after goals-scored (lower ``fair_play`` disciplinary points rank higher;
        lower ``fifa_rank`` numeric position ranks higher). Any team missing is
        treated as worst. Residual ties fall back to the RNG.
    allocation
        Override for the third-placed allocation table (defaults to the official
        ``THIRDS_ALLOCATION``); injectable for what-if analysis.
    """

    def __init__(
        self,
        groups: Mapping[str, Sequence[str]],
        prob_fn: ProbFn,
        results: Optional[Sequence[Result]] = None,
        et_skill_weight: float = 0.5,
        mean_goals: float = 2.7,
        fair_play: Optional[Mapping[str, float]] = None,
        fifa_rank: Optional[Mapping[str, float]] = None,
        allocation: Optional[Mapping[str, str]] = None,
    ) -> None:
        if set(groups) != set(GROUP_LETTERS):
            raise ValueError(
                "groups must contain exactly the 12 letters A-L; got "
                f"{sorted(groups)!r}"
            )
        self.groups: Dict[str, List[str]] = {g: list(groups[g]) for g in GROUP_LETTERS}
        for g, teams in self.groups.items():
            if len(teams) != 4:
                raise ValueError(f"group {g} must have 4 teams, got {len(teams)}")

        self.teams: List[str] = [t for g in GROUP_LETTERS for t in self.groups[g]]
        if len(set(self.teams)) != 48:
            raise ValueError("team names must be unique across all groups")
        self._team_index: Dict[str, int] = {t: i for i, t in enumerate(self.teams)}
        # Per-group team -> local index 0..3
        self._group_local: Dict[str, Dict[str, int]] = {
            g: {t: i for i, t in enumerate(self.groups[g])} for g in GROUP_LETTERS
        }

        self.prob_fn = prob_fn
        self.et_skill_weight = float(et_skill_weight)
        if not 0.0 <= self.et_skill_weight <= 1.0:
            raise ValueError("et_skill_weight must be in [0, 1]")
        self.mean_goals = float(mean_goals)
        self.fair_play = dict(fair_play) if fair_play is not None else None
        self.fifa_rank = dict(fifa_rank) if fifa_rank is not None else None
        self.allocation: Mapping[str, str] = allocation if allocation is not None else THIRDS_ALLOCATION

        # Fixed results, keyed by (group, frozenset-of-teams). Goals are stored
        # in the group's *listed* (lower-local-index = home) orientation so that
        # _simulate_group can apply them directly to the (i, j) fixture.
        self._fixed: Dict[Tuple[str, frozenset], Tuple[int, int]] = {}
        if results:
            for r in _normalise_results(self.groups, results):
                g = self._group_of_pair(r.home, r.away)
                self._fixed[(g, frozenset((r.home, r.away)))] = (r.home_goals, r.away_goals)

        self._prob_cache: Dict[Tuple[str, str, bool], Tuple[float, float, float]] = {}

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------
    def _group_of_pair(self, a: str, b: str) -> str:
        ga = self._group_of_team(a)
        gb = self._group_of_team(b)
        if ga != gb:
            raise ValueError(f"result {a} vs {b} crosses groups {ga}/{gb}")
        return ga

    def _group_of_team(self, team: str) -> str:
        for g, teams in self.groups.items():
            if team in teams:
                return g
        raise KeyError(f"unknown team {team!r}")

    def _probs(self, a: str, b: str, knockout: bool) -> Tuple[float, float, float]:
        key = (a, b, knockout)
        cached = self._prob_cache.get(key)
        if cached is None:
            pa, pd, pb = self.prob_fn(a, b, knockout)
            s = pa + pd + pb
            if s <= 0:
                raise ValueError(f"prob_fn returned non-positive total for {a} vs {b}")
            cached = (pa / s, pd / s, pb / s)
            self._prob_cache[key] = cached
        return cached

    def _tiebreak_keys(self) -> Dict[str, Tuple[float, float]]:
        """Deterministic (fair_play, fifa_rank) sort priors for every team.

        Returned as ``team -> (fair_play_pts, fifa_rank)`` where *smaller is
        better*. Missing values default to ``+inf`` (worst).
        """

        out: Dict[str, Tuple[float, float]] = {}
        for t in self.teams:
            fp = float(self.fair_play[t]) if self.fair_play and t in self.fair_play else float("inf")
            fr = float(self.fifa_rank[t]) if self.fifa_rank and t in self.fifa_rank else float("inf")
            out[t] = (fp, fr)
        return out

    # ------------------------------------------------------------------
    # Group-stage simulation (vectorised over sims).
    # ------------------------------------------------------------------
    def _simulate_group(
        self,
        group: str,
        n_sims: int,
        rng: np.random.Generator,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Simulate one group across all sims.

        Returns ``(order, points, gd, gf)`` where ``order`` has shape
        ``(n_sims, 4)`` giving local team indices sorted best-to-worst, and the
        other three arrays have shape ``(n_sims, 4)`` indexed by *local team
        index* (0..3) holding final points, goal difference and goals-for.
        """

        teams = self.groups[group]
        points = np.zeros((n_sims, 4), dtype=np.int64)
        gf = np.zeros((n_sims, 4), dtype=np.int64)
        ga = np.zeros((n_sims, 4), dtype=np.int64)
        # Head-to-head accumulators: h2h_pts[s, i, j] = points i earned vs j.
        h2h_pts = np.zeros((n_sims, 4, 4), dtype=np.int64)
        h2h_gf = np.zeros((n_sims, 4, 4), dtype=np.int64)

        for (i, j) in GROUP_FIXTURE_PAIRS:
            home, away = teams[i], teams[j]
            fixed = self._fixed.get((group, frozenset((home, away))))
            if fixed is not None:
                # The stored scoreline is in (home_listed, away_listed) order of
                # the original Result; re-map to (i, j).
                hg, ag = self._resolve_fixed(group, home, away, fixed)
                hgoals = np.full(n_sims, hg, dtype=np.int64)
                agoals = np.full(n_sims, ag, dtype=np.int64)
            else:
                hgoals, agoals = self._sample_goals(home, away, n_sims, rng, knockout=False)

            self._apply_match(points, gf, ga, h2h_pts, h2h_gf, i, j, hgoals, agoals)

        gd = gf - ga
        order = self._rank_group(points, gd, gf, h2h_pts, h2h_gf, teams, rng)
        return order, points, gd, gf

    def _resolve_fixed(
        self, group: str, home: str, away: str, fixed: Tuple[int, int]
    ) -> Tuple[int, int]:
        """Return the stored scoreline for the fixture's (home, away) orientation.

        Results are normalised at construction time to the group's listed
        orientation (lower local index = home), which is exactly the ``(i, j)``
        orientation used by :meth:`_simulate_group`, so the stored tuple already
        matches and is returned unchanged.
        """

        return fixed

    def _sample_goals(
        self,
        home: str,
        away: str,
        n_sims: int,
        rng: np.random.Generator,
        knockout: bool,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Sample a scoreline whose W/D/L split matches ``prob_fn``.

        Strategy: draw the *outcome* (home win / draw / away win) directly from
        ``prob_fn`` so the win/draw probabilities are exact, then draw a
        plausible scoreline consistent with that outcome from independent
        Poisson goal counts. This decouples "who wins" (exact) from "by how
        much" (calibrated, for GD/goals tie-breaks) without distorting the
        outcome distribution.
        """

        pa, pd, pb = self._probs(home, away, knockout)
        u = rng.random(n_sims)
        outcome = np.where(u < pa, 0, np.where(u < pa + pd, 1, 2))  # 0 H, 1 D, 2 A

        # Expected goals for each side from a simple proportional model.
        lam_h, lam_a = self._goal_rates(pa, pd, pb)
        hg = rng.poisson(lam_h, size=n_sims).astype(np.int64)
        ag = rng.poisson(lam_a, size=n_sims).astype(np.int64)

        # Reconcile sampled scoreline with the drawn outcome.
        hg, ag = self._coerce_outcome(hg, ag, outcome, rng)
        return hg, ag

    def _goal_rates(self, pa: float, pd: float, pb: float) -> Tuple[float, float]:
        """Map outcome probabilities to two Poisson rates summing to mean_goals.

        Uses the decisive win-share to split the mean total between the sides,
        with a floor so neither rate collapses to zero.
        """

        decisive = pa + pb
        share_a = 0.5 if decisive <= 0 else pa / decisive
        share_a = min(max(share_a, 0.15), 0.85)
        lam_h = self.mean_goals * share_a
        lam_a = self.mean_goals * (1.0 - share_a)
        return max(lam_h, 0.05), max(lam_a, 0.05)

    @staticmethod
    def _coerce_outcome(
        hg: np.ndarray, ag: np.ndarray, outcome: np.ndarray, rng: np.random.Generator
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Adjust scorelines so their sign matches the drawn ``outcome``.

        Minimal nudge: if the sampled margin contradicts the required outcome we
        add a single goal to the side that must come out ahead (or level the
        score for a required draw).
        """

        hg = hg.copy()
        ag = ag.copy()
        # Required home win but not ahead -> bump home above away.
        mask = (outcome == 0) & (hg <= ag)
        hg[mask] = ag[mask] + 1
        # Required away win but not ahead -> bump away above home.
        mask = (outcome == 2) & (ag <= hg)
        ag[mask] = hg[mask] + 1
        # Required draw but not level -> set the lower to the higher.
        mask = (outcome == 1) & (hg != ag)
        hi = np.maximum(hg, ag)
        hg[mask] = hi[mask]
        ag[mask] = hi[mask]
        return hg, ag

    @staticmethod
    def _apply_match(
        points: np.ndarray,
        gf: np.ndarray,
        ga: np.ndarray,
        h2h_pts: np.ndarray,
        h2h_gf: np.ndarray,
        i: int,
        j: int,
        hg: np.ndarray,
        ag: np.ndarray,
    ) -> None:
        """Accumulate one fixture's contribution into the standings arrays."""

        gf[:, i] += hg
        ga[:, i] += ag
        gf[:, j] += ag
        ga[:, j] += hg
        h2h_gf[:, i, j] += hg
        h2h_gf[:, j, i] += ag

        home_win = hg > ag
        away_win = ag > hg
        draw = ~home_win & ~away_win

        points[home_win, i] += 3
        points[away_win, j] += 3
        points[draw, i] += 1
        points[draw, j] += 1

        h2h_pts[home_win, i, j] += 3
        h2h_pts[away_win, j, i] += 3
        h2h_pts[draw, i, j] += 1
        h2h_pts[draw, j, i] += 1

    def _rank_group(
        self,
        points: np.ndarray,
        gd: np.ndarray,
        gf: np.ndarray,
        h2h_pts: np.ndarray,
        h2h_gf: np.ndarray,
        teams: Sequence[str],
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Rank the four teams per sim applying the FIFA 2026 tie-break order.

        Returns an ``(n_sims, 4)`` array of local team indices, best first.

        The ranking key per team is a lexicographic tuple
        ``(points, h2h_points, h2h_gd, h2h_gf, overall_gd, overall_gf,
        -fair_play, -fifa_rank, random)`` with larger = better. Head-to-head
        sub-criteria are computed *among the teams currently level on points*,
        re-evaluated as required by re-applying them to the remaining tied
        subset (achieved here by including them all in the lexicographic key,
        which matches FIFA's mini-table among equal-points teams for the common
        cases; pathological partial-subset cycles fall through to overall GD).
        """

        n_sims = points.shape[0]
        # Tie-break priors (fair play, fifa rank): smaller = better, so negate.
        priors = self._tiebreak_keys()
        fp = np.array([priors[t][0] for t in teams])
        fr = np.array([priors[t][1] for t in teams])
        # Replace inf with a large finite sentinel for arithmetic stability.
        big = 1e9
        fp = np.where(np.isfinite(fp), fp, big)
        fr = np.where(np.isfinite(fr), fr, big)

        # Head-to-head among equal-points teams: build per-team H2H aggregates
        # restricted to opponents sharing the same point total.
        h2h_p, h2h_d, h2h_g = self._h2h_among_equal(points, h2h_pts, h2h_gf)

        rand = rng.random((n_sims, 4))

        # Compose a single sortable float key per (sim, team). We pack the
        # ordered criteria into descending significance with safe magnitudes.
        # Larger key = better rank.
        key = (
            points.astype(np.float64) * 1e30
            + h2h_p.astype(np.float64) * 1e26
            + (h2h_d.astype(np.float64) + 100) * 1e22
            + h2h_g.astype(np.float64) * 1e18
            + (gd.astype(np.float64) + 100) * 1e14
            + gf.astype(np.float64) * 1e10
            - fp[None, :] * 1e6
            - fr[None, :] * 1e2
            + rand
        )
        # argsort descending: best first.
        order = np.argsort(-key, axis=1, kind="stable").astype(np.int64)
        return order

    @staticmethod
    def _h2h_among_equal(
        points: np.ndarray, h2h_pts: np.ndarray, h2h_gf: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Head-to-head aggregates restricted to equal-points opponents.

        For each team, sum H2H points / GD / GF only over opponents that finished
        on the *same* total points. Teams uniquely on their point total get
        zeroes (no mini-table), which leaves them ordered by the overall
        criteria -- the correct behaviour.
        """

        n_sims = points.shape[0]
        # equal[s, i, j] = points i == points j (and i != j).
        eq = (points[:, :, None] == points[:, None, :])
        idx = np.arange(4)
        eq[:, idx, idx] = False

        eq_pts = np.where(eq, h2h_pts, 0)
        h2h_p = eq_pts.sum(axis=2)

        gf_for = np.where(eq, h2h_gf, 0)
        gf_against = np.where(eq, np.transpose(h2h_gf, (0, 2, 1)), 0)
        h2h_g = gf_for.sum(axis=2)
        h2h_d = (gf_for - gf_against).sum(axis=2)
        return h2h_p, h2h_d, h2h_g

    # ------------------------------------------------------------------
    # Third-place ranking and allocation.
    # ------------------------------------------------------------------
    def _rank_thirds(
        self,
        third_team_global: np.ndarray,
        third_points: np.ndarray,
        third_gd: np.ndarray,
        third_gf: np.ndarray,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Return, per sim, the 8 group letters whose thirds qualify (sorted).

        Inputs are ``(n_sims, 12)`` arrays giving each group's third-placed
        team's global index, points, GD and GF (column order = GROUP_LETTERS).
        Returns an ``(n_sims, 8)`` array of *group column indices* (0..11) of
        the eight qualifying groups, and the caller maps them to letters.
        """

        n_sims = third_points.shape[0]
        priors = self._tiebreak_keys()
        big = 1e9
        # Per-team finite (fair_play, fifa_rank) priors, indexed by global idx.
        fp_team = np.array(
            [priors[t][0] if np.isfinite(priors[t][0]) else big for t in self.teams]
        )
        fr_team = np.array(
            [priors[t][1] if np.isfinite(priors[t][1]) else big for t in self.teams]
        )
        # Map each group's third-placed team's prior via its global index.
        fp_third = np.take(fp_team, third_team_global)
        fr_third = np.take(fr_team, third_team_global)
        rand = rng.random((n_sims, 12))

        key = (
            third_points.astype(np.float64) * 1e24
            + (third_gd.astype(np.float64) + 100) * 1e18
            + third_gf.astype(np.float64) * 1e12
            - fp_third * 1e6
            - fr_third * 1e2
            + rand
        )
        # Top 8 columns per sim (descending key), then sort those columns so the
        # combination key is canonical.
        ranked = np.argsort(-key, axis=1, kind="stable")[:, :8]
        ranked.sort(axis=1)
        return ranked.astype(np.int64)

    # ------------------------------------------------------------------
    # Knockout simulation.
    # ------------------------------------------------------------------
    def _play_ko(
        self, a: np.ndarray, b: np.ndarray, rng: np.random.Generator
    ) -> np.ndarray:
        """Resolve a knockout tie between team-global-index arrays ``a`` vs ``b``.

        Returns the winners' global indices. A 90-minute draw is decided by the
        extra-time / penalty model.
        """

        n = a.shape[0]
        winners = np.empty(n, dtype=np.int64)
        # Group sims by the (a, b) pair to reuse cached probabilities. In a real
        # tournament many distinct pairs appear, but vectorising per unique pair
        # keeps it fast.
        pairs = (a.astype(np.int64) << 16) | b.astype(np.int64)
        uniq = np.unique(pairs)
        u_all = rng.random(n)
        for code in uniq:
            mask = pairs == code
            ai = int(code >> 16)
            bi = int(code & 0xFFFF)
            ta, tb = self.teams[ai], self.teams[bi]
            pa, pd, pb = self._probs(ta, tb, knockout=True)
            decisive = pa + pb
            qa = 0.5 if decisive <= 0 else pa / decisive
            # Probability a wins outright in 90' or via ET/pens after a draw.
            p_et_a = 0.5 + self.et_skill_weight * (qa - 0.5)
            p_a_total = pa + pd * p_et_a
            u = u_all[mask]
            win_a = u < p_a_total
            sub = np.where(win_a, ai, bi)
            winners[mask] = sub
        return winners

    # ------------------------------------------------------------------
    # Top-level driver.
    # ------------------------------------------------------------------
    def simulate(self, n_sims: int = 10000, rng_seed: Optional[int] = None) -> SimulationResult:
        """Run ``n_sims`` Monte Carlo tournaments and aggregate probabilities.

        Returns a :class:`SimulationResult` with per-team exact group-position
        distributions and reached-stage / win probabilities. Deterministic for a
        fixed ``rng_seed``.
        """

        if n_sims <= 0:
            raise ValueError("n_sims must be positive")
        rng = np.random.default_rng(rng_seed)

        # --- Group stage ------------------------------------------------
        # winners_global[s, g], runners_global[s, g], thirds_global[s, g]
        winners_g = np.empty((n_sims, 12), dtype=np.int64)
        runners_g = np.empty((n_sims, 12), dtype=np.int64)
        thirds_g = np.empty((n_sims, 12), dtype=np.int64)
        thirds_pts = np.empty((n_sims, 12), dtype=np.int64)
        thirds_gd = np.empty((n_sims, 12), dtype=np.int64)
        thirds_gf = np.empty((n_sims, 12), dtype=np.int64)

        # Exact group-position counts per team (global index) -> 4 positions.
        pos_counts = np.zeros((len(self.teams), 4), dtype=np.int64)

        for gi, g in enumerate(GROUP_LETTERS):
            order, points, gd, gf = self._simulate_group(g, n_sims, rng)
            # local index -> global index for this group
            locals_to_global = np.array(
                [self._team_index[t] for t in self.groups[g]], dtype=np.int64
            )
            # order holds local indices; convert to global.
            global_order = locals_to_global[order]  # (n_sims, 4)
            winners_g[:, gi] = global_order[:, 0]
            runners_g[:, gi] = global_order[:, 1]
            thirds_g[:, gi] = global_order[:, 2]

            # Accumulate exact-position counts.
            for pos in range(4):
                teams_at_pos = global_order[:, pos]
                np.add.at(pos_counts, (teams_at_pos, pos), 1)

            # Stats of the third-placed team per sim (by local idx = order[:,2]).
            third_local = order[:, 2]
            rows = np.arange(n_sims)
            thirds_pts[:, gi] = points[rows, third_local]
            thirds_gd[:, gi] = gd[rows, third_local]
            thirds_gf[:, gi] = gf[rows, third_local]

        # --- Pick the 8 best thirds ------------------------------------
        qual_cols = self._rank_thirds(thirds_g, thirds_pts, thirds_gd, thirds_gf, rng)

        # --- Build R32 and run knockout --------------------------------
        win_counts = np.zeros(len(self.teams), dtype=np.int64)
        reach_counts: Dict[str, np.ndarray] = {
            r: np.zeros(len(self.teams), dtype=np.int64) for r in ("R32", "R16", "QF", "SF", "F")
        }

        # Resolve the R32 participants for every sim, vectorised by combination.
        match_winner = self._run_knockout(
            winners_g, runners_g, thirds_g, qual_cols, n_sims, rng, reach_counts, win_counts
        )

        # --- Assemble result -------------------------------------------
        group_position = {
            t: pos_counts[i].astype(np.float64) / n_sims for i, t in enumerate(self.teams)
        }
        reach = {
            stage: {t: reach_counts[stage][i] / n_sims for i, t in enumerate(self.teams)}
            for stage in ("R32", "R16", "QF", "SF", "F")
        }
        win = {t: win_counts[i] / n_sims for i, t in enumerate(self.teams)}

        return SimulationResult(
            teams=list(self.teams),
            n_sims=n_sims,
            group_position=group_position,
            reach=reach,
            win=win,
        )

    def _run_knockout(
        self,
        winners_g: np.ndarray,
        runners_g: np.ndarray,
        thirds_g: np.ndarray,
        qual_cols: np.ndarray,
        n_sims: int,
        rng: np.random.Generator,
        reach_counts: Dict[str, np.ndarray],
        win_counts: np.ndarray,
    ) -> Dict[int, np.ndarray]:
        """Populate R32 and play through the bracket. Returns match winners.

        The eight thirds-allocation depends on which 8 groups qualified; we group
        sims by that combination so each gets its official mapping, then play the
        identical bracket structure for all.
        """

        letter_to_col = {g: i for i, g in enumerate(GROUP_LETTERS)}

        # Participant arrays per R32 match, by match_no -> (side_a, side_b) global idx.
        side_a: Dict[int, np.ndarray] = {}
        side_b: Dict[int, np.ndarray] = {}
        for (mno, sa, sb) in R32_TIES:
            side_a[mno] = self._resolve_side(sa, winners_g, runners_g, thirds_g, qual_cols, letter_to_col)
            side_b[mno] = self._resolve_side(sb, winners_g, runners_g, thirds_g, qual_cols, letter_to_col)

        # Everyone in an R32 tie has "reached R32".
        for mno in (t[0] for t in R32_TIES):
            np.add.at(reach_counts["R32"], side_a[mno], 1)
            np.add.at(reach_counts["R32"], side_b[mno], 1)

        # Play R32.
        match_winner: Dict[int, np.ndarray] = {}
        for (mno, _, _) in R32_TIES:
            match_winner[mno] = self._play_ko(side_a[mno], side_b[mno], rng)

        # Walk the feed for the later rounds.
        round_of = {89: "R16", 97: "QF", 101: "SF", 104: "F"}
        # Map each match number to its round label for crediting reach.
        match_round = {}
        for m in range(89, 97):
            match_round[m] = "R16"
        for m in range(97, 101):
            match_round[m] = "QF"
        for m in (101, 102):
            match_round[m] = "SF"
        match_round[104] = "F"

        for (mno, src_a, src_b) in KNOCKOUT_FEED:
            a = match_winner[src_a]
            b = match_winner[src_b]
            # Both participants reached this round.
            label = match_round[mno]
            np.add.at(reach_counts[label], a, 1)
            np.add.at(reach_counts[label], b, 1)
            match_winner[mno] = self._play_ko(a, b, rng)

        # Champions = winners of the final (match 104).
        np.add.at(win_counts, match_winner[104], 1)
        return match_winner

    def _resolve_side(
        self,
        side: Tuple[str, str],
        winners_g: np.ndarray,
        runners_g: np.ndarray,
        thirds_g: np.ndarray,
        qual_cols: np.ndarray,
        letter_to_col: Mapping[str, int],
    ) -> np.ndarray:
        """Resolve a R32 side spec to an ``(n_sims,)`` array of global indices."""

        kind, letter = side
        col = letter_to_col[letter]
        if kind == "W":
            return winners_g[:, col]
        if kind == "R":
            return runners_g[:, col]
        if kind == "T":
            return self._resolve_third_for_slot(letter, thirds_g, qual_cols, letter_to_col)
        raise ValueError(f"unknown side kind {kind!r}")

    def _resolve_third_for_slot(
        self,
        winner_letter: str,
        thirds_g: np.ndarray,
        qual_cols: np.ndarray,
        letter_to_col: Mapping[str, int],
    ) -> np.ndarray:
        """For winner slot ``winner_letter`` find which group's third it faces.

        Groups sims by the qualifying-combination, looks up the official
        allocation, and returns the global index of the relevant third-placed
        team per sim.
        """

        n_sims = thirds_g.shape[0]
        out = np.empty(n_sims, dtype=np.int64)
        slot_pos = THIRDS_SLOT_WINNERS.index(winner_letter)

        # Encode each sim's qualifying combination as a sorted 8-letter string.
        # qual_cols is (n_sims, 8) sorted column indices.
        # Build the combination key per sim and group identical keys together.
        col_to_letter = {i: g for g, i in letter_to_col.items()}
        # Vectorised key: pack 8 column indices (0..11) into one integer.
        packed = np.zeros(n_sims, dtype=np.int64)
        for k in range(8):
            packed = packed * 12 + qual_cols[:, k]
        uniq = np.unique(packed)
        for code in uniq:
            mask = packed == code
            cols = qual_cols[np.argmax(mask)]  # the 8 columns for this combo
            key = "".join(sorted(col_to_letter[int(c)] for c in cols))
            assign = self.allocation[key]
            third_letter = assign[slot_pos]
            third_col = letter_to_col[third_letter]
            out[mask] = thirds_g[mask, third_col]
        return out


# Normalise stored results to the listed (i, j) orientation at construction.
def _normalise_results(
    groups: Mapping[str, Sequence[str]], results: Sequence[Result]
) -> List[Result]:
    """Return results re-oriented so home==listed-first, away==listed-second.

    The simulator keys fixed games by unordered pair but applies them in the
    group's listed orientation; this helper flips any Result recorded in the
    opposite orientation so goals stay attached to the right side.
    """

    local: Dict[str, Dict[str, int]] = {
        g: {t: i for i, t in enumerate(groups[g])} for g in groups
    }
    out: List[Result] = []
    for r in results:
        # find the group
        g = None
        for gg, teams in groups.items():
            if r.home in teams and r.away in teams:
                g = gg
                break
        if g is None:
            raise ValueError(f"result {r.home} vs {r.away} not in any single group")
        if local[g][r.home] < local[g][r.away]:
            out.append(r)
        else:
            out.append(Result(r.away, r.home, r.away_goals, r.home_goals))
    return out


# ---------------------------------------------------------------------------
# Official third-placed-team allocation table (FIFA 2026, Annex C).
#
# Key:   sorted 8-letter string of the groups whose third-placed teams qualify.
# Value: 8-letter string giving the group whose third-placed team is assigned to
#        each winner slot, in the column order THIRDS_SLOT_WINNERS =
#        (A, B, D, E, G, I, K, L).
#
# Transcribed and validated (all 495 = C(12, 8) combinations, every value a
# permutation of its key) from
# https://en.wikipedia.org/wiki/Template:2026_FIFA_World_Cup_third-place_table
# ---------------------------------------------------------------------------
THIRDS_ALLOCATION = {
    "ABCDEFGH": "HGBCAFDE",
    "ABCDEFGI": "CGBDAFEI",
    "ABCDEFGJ": "CGBDAFEJ",
    "ABCDEFGK": "CGBDAFEK",
    "ABCDEFGL": "CGBDAFLE",
    "ABCDEFHI": "HEBCAFDI",
    "ABCDEFHJ": "HJBCAFDE",
    "ABCDEFHK": "HEBCAFDK",
    "ABCDEFHL": "HFBCADLE",
    "ABCDEFIJ": "CJBDAFEI",
    "ABCDEFIK": "CEBDAFIK",
    "ABCDEFIL": "CEBDAFLI",
    "ABCDEFJK": "CJBDAFEK",
    "ABCDEFJL": "CJBDAFLE",
    "ABCDEFKL": "CEBDAFLK",
    "ABCDEGHI": "HGBCADEI",
    "ABCDEGHJ": "HGBCADEJ",
    "ABCDEGHK": "HGBCADEK",
    "ABCDEGHL": "HGBCADLE",
    "ABCDEGIJ": "EGBCADIJ",
    "ABCDEGIK": "EGBCADIK",
    "ABCDEGIL": "EGBCADLI",
    "ABCDEGJK": "EGBCADJK",
    "ABCDEGJL": "EGBCADLJ",
    "ABCDEGKL": "EGBCADLK",
    "ABCDEHIJ": "HJBCADEI",
    "ABCDEHIK": "HEBCADIK",
    "ABCDEHIL": "HEBCADLI",
    "ABCDEHJK": "HJBCADEK",
    "ABCDEHJL": "HJBCADLE",
    "ABCDEHKL": "HEBCADLK",
    "ABCDEIJK": "EJBCADIK",
    "ABCDEIJL": "EJBCADLI",
    "ABCDEIKL": "EIBCADLK",
    "ABCDEJKL": "EJBCADLK",
    "ABCDFGHI": "HGBCAFDI",
    "ABCDFGHJ": "HGBCAFDJ",
    "ABCDFGHK": "HGBCAFDK",
    "ABCDFGHL": "CGBDAFLH",
    "ABCDFGIJ": "CGBDAFIJ",
    "ABCDFGIK": "CGBDAFIK",
    "ABCDFGIL": "CGBDAFLI",
    "ABCDFGJK": "CGBDAFJK",
    "ABCDFGJL": "CGBDAFLJ",
    "ABCDFGKL": "CGBDAFLK",
    "ABCDFHIJ": "HJBCAFDI",
    "ABCDFHIK": "HFBCADIK",
    "ABCDFHIL": "HFBCADLI",
    "ABCDFHJK": "HJBCAFDK",
    "ABCDFHJL": "CJBDAFLH",
    "ABCDFHKL": "HFBCADLK",
    "ABCDFIJK": "CJBDAFIK",
    "ABCDFIJL": "CJBDAFLI",
    "ABCDFIKL": "CIBDAFLK",
    "ABCDFJKL": "CJBDAFLK",
    "ABCDGHIJ": "HGBCADIJ",
    "ABCDGHIK": "HGBCADIK",
    "ABCDGHIL": "HGBCADLI",
    "ABCDGHJK": "HGBCADJK",
    "ABCDGHJL": "HGBCADLJ",
    "ABCDGHKL": "HGBCADLK",
    "ABCDGIJK": "CJBDAGIK",
    "ABCDGIJL": "CJBDAGLI",
    "ABCDGIKL": "IGBCADLK",
    "ABCDGJKL": "CJBDAGLK",
    "ABCDHIJK": "HJBCADIK",
    "ABCDHIJL": "HJBCADLI",
    "ABCDHIKL": "HIBCADLK",
    "ABCDHJKL": "HJBCADLK",
    "ABCDIJKL": "IJBCADLK",
    "ABCEFGHI": "HGBCAFEI",
    "ABCEFGHJ": "HGBCAFEJ",
    "ABCEFGHK": "HGBCAFEK",
    "ABCEFGHL": "HGBCAFLE",
    "ABCEFGIJ": "EGBCAFIJ",
    "ABCEFGIK": "EGBCAFIK",
    "ABCEFGIL": "EGBCAFLI",
    "ABCEFGJK": "EGBCAFJK",
    "ABCEFGJL": "EGBCAFLJ",
    "ABCEFGKL": "EGBCAFLK",
    "ABCEFHIJ": "HJBCAFEI",
    "ABCEFHIK": "HEBCAFIK",
    "ABCEFHIL": "HEBCAFLI",
    "ABCEFHJK": "HJBCAFEK",
    "ABCEFHJL": "HJBCAFLE",
    "ABCEFHKL": "HEBCAFLK",
    "ABCEFIJK": "EJBCAFIK",
    "ABCEFIJL": "EJBCAFLI",
    "ABCEFIKL": "EIBCAFLK",
    "ABCEFJKL": "EJBCAFLK",
    "ABCEGHIJ": "HJBCAGEI",
    "ABCEGHIK": "EGBCAHIK",
    "ABCEGHIL": "EGBCAHLI",
    "ABCEGHJK": "HJBCAGEK",
    "ABCEGHJL": "HJBCAGLE",
    "ABCEGHKL": "EGBCAHLK",
    "ABCEGIJK": "EJBCAGIK",
    "ABCEGIJL": "EJBCAGLI",
    "ABCEGIKL": "EGBAICLK",
    "ABCEGJKL": "EJBCAGLK",
    "ABCEHIJK": "EJBCAHIK",
    "ABCEHIJL": "EJBCAHLI",
    "ABCEHIKL": "EIBCAHLK",
    "ABCEHJKL": "EJBCAHLK",
    "ABCEIJKL": "EJBAICLK",
    "ABCFGHIJ": "HGBCAFIJ",
    "ABCFGHIK": "HGBCAFIK",
    "ABCFGHIL": "HGBCAFLI",
    "ABCFGHJK": "HGBCAFJK",
    "ABCFGHJL": "HGBCAFLJ",
    "ABCFGHKL": "HGBCAFLK",
    "ABCFGIJK": "CJBFAGIK",
    "ABCFGIJL": "CJBFAGLI",
    "ABCFGIKL": "IGBCAFLK",
    "ABCFGJKL": "CJBFAGLK",
    "ABCFHIJK": "HJBCAFIK",
    "ABCFHIJL": "HJBCAFLI",
    "ABCFHIKL": "HIBCAFLK",
    "ABCFHJKL": "HJBCAFLK",
    "ABCFIJKL": "IJBCAFLK",
    "ABCGHIJK": "HJBCAGIK",
    "ABCGHIJL": "HJBCAGLI",
    "ABCGHIKL": "IGBCAHLK",
    "ABCGHJKL": "HJBCAGLK",
    "ABCGIJKL": "IJBCAGLK",
    "ABCHIJKL": "IJBCAHLK",
    "ABDEFGHI": "HGBDAFEI",
    "ABDEFGHJ": "HGBDAFEJ",
    "ABDEFGHK": "HGBDAFEK",
    "ABDEFGHL": "HGBDAFLE",
    "ABDEFGIJ": "EGBDAFIJ",
    "ABDEFGIK": "EGBDAFIK",
    "ABDEFGIL": "EGBDAFLI",
    "ABDEFGJK": "EGBDAFJK",
    "ABDEFGJL": "EGBDAFLJ",
    "ABDEFGKL": "EGBDAFLK",
    "ABDEFHIJ": "HJBDAFEI",
    "ABDEFHIK": "HEBDAFIK",
    "ABDEFHIL": "HEBDAFLI",
    "ABDEFHJK": "HJBDAFEK",
    "ABDEFHJL": "HJBDAFLE",
    "ABDEFHKL": "HEBDAFLK",
    "ABDEFIJK": "EJBDAFIK",
    "ABDEFIJL": "EJBDAFLI",
    "ABDEFIKL": "EIBDAFLK",
    "ABDEFJKL": "EJBDAFLK",
    "ABDEGHIJ": "HJBDAGEI",
    "ABDEGHIK": "EGBDAHIK",
    "ABDEGHIL": "EGBDAHLI",
    "ABDEGHJK": "HJBDAGEK",
    "ABDEGHJL": "HJBDAGLE",
    "ABDEGHKL": "EGBDAHLK",
    "ABDEGIJK": "EJBDAGIK",
    "ABDEGIJL": "EJBDAGLI",
    "ABDEGIKL": "EGBAIDLK",
    "ABDEGJKL": "EJBDAGLK",
    "ABDEHIJK": "EJBDAHIK",
    "ABDEHIJL": "EJBDAHLI",
    "ABDEHIKL": "EIBDAHLK",
    "ABDEHJKL": "EJBDAHLK",
    "ABDEIJKL": "EJBAIDLK",
    "ABDFGHIJ": "HGBDAFIJ",
    "ABDFGHIK": "HGBDAFIK",
    "ABDFGHIL": "HGBDAFLI",
    "ABDFGHJK": "HGBDAFJK",
    "ABDFGHJL": "HGBDAFLJ",
    "ABDFGHKL": "HGBDAFLK",
    "ABDFGIJK": "FJBDAGIK",
    "ABDFGIJL": "FJBDAGLI",
    "ABDFGIKL": "IGBDAFLK",
    "ABDFGJKL": "FJBDAGLK",
    "ABDFHIJK": "HJBDAFIK",
    "ABDFHIJL": "HJBDAFLI",
    "ABDFHIKL": "HIBDAFLK",
    "ABDFHJKL": "HJBDAFLK",
    "ABDFIJKL": "IJBDAFLK",
    "ABDGHIJK": "HJBDAGIK",
    "ABDGHIJL": "HJBDAGLI",
    "ABDGHIKL": "IGBDAHLK",
    "ABDGHJKL": "HJBDAGLK",
    "ABDGIJKL": "IJBDAGLK",
    "ABDHIJKL": "IJBDAHLK",
    "ABEFGHIJ": "HJBFAGEI",
    "ABEFGHIK": "EGBFAHIK",
    "ABEFGHIL": "EGBFAHLI",
    "ABEFGHJK": "HJBFAGEK",
    "ABEFGHJL": "HJBFAGLE",
    "ABEFGHKL": "EGBFAHLK",
    "ABEFGIJK": "EJBFAGIK",
    "ABEFGIJL": "EJBFAGLI",
    "ABEFGIKL": "EGBAIFLK",
    "ABEFGJKL": "EJBFAGLK",
    "ABEFHIJK": "EJBFAHIK",
    "ABEFHIJL": "EJBFAHLI",
    "ABEFHIKL": "EIBFAHLK",
    "ABEFHJKL": "EJBFAHLK",
    "ABEFIJKL": "EJBAIFLK",
    "ABEGHIJK": "EJBAHGIK",
    "ABEGHIJL": "EJBAHGLI",
    "ABEGHIKL": "EGBAIHLK",
    "ABEGHJKL": "EJBAHGLK",
    "ABEGIJKL": "EJBAIGLK",
    "ABEHIJKL": "EJBAIHLK",
    "ABFGHIJK": "HJBFAGIK",
    "ABFGHIJL": "HJBFAGLI",
    "ABFGHIKL": "HGBAIFLK",
    "ABFGHJKL": "HJBFAGLK",
    "ABFGIJKL": "IJBFAGLK",
    "ABFHIJKL": "HJBAIFLK",
    "ABGHIJKL": "HJBAIGLK",
    "ACDEFGHI": "HGECAFDI",
    "ACDEFGHJ": "HGJCAFDE",
    "ACDEFGHK": "HGECAFDK",
    "ACDEFGHL": "HGFCADLE",
    "ACDEFGIJ": "CGJDAFEI",
    "ACDEFGIK": "CGEDAFIK",
    "ACDEFGIL": "CGEDAFLI",
    "ACDEFGJK": "CGJDAFEK",
    "ACDEFGJL": "CGJDAFLE",
    "ACDEFGKL": "CGEDAFLK",
    "ACDEFHIJ": "HJECAFDI",
    "ACDEFHIK": "HEFCADIK",
    "ACDEFHIL": "HEFCADLI",
    "ACDEFHJK": "HJECAFDK",
    "ACDEFHJL": "HJFCADLE",
    "ACDEFHKL": "HEFCADLK",
    "ACDEFIJK": "CJEDAFIK",
    "ACDEFIJL": "CJEDAFLI",
    "ACDEFIKL": "CEIDAFLK",
    "ACDEFJKL": "CJEDAFLK",
    "ACDEGHIJ": "HGJCADEI",
    "ACDEGHIK": "HGECADIK",
    "ACDEGHIL": "HGECADLI",
    "ACDEGHJK": "HGJCADEK",
    "ACDEGHJL": "HGJCADLE",
    "ACDEGHKL": "HGECADLK",
    "ACDEGIJK": "EGJCADIK",
    "ACDEGIJL": "EGJCADLI",
    "ACDEGIKL": "EGICADLK",
    "ACDEGJKL": "EGJCADLK",
    "ACDEHIJK": "HJECADIK",
    "ACDEHIJL": "HJECADLI",
    "ACDEHIKL": "HEICADLK",
    "ACDEHJKL": "HJECADLK",
    "ACDEIJKL": "EJICADLK",
    "ACDFGHIJ": "HGJCAFDI",
    "ACDFGHIK": "HGFCADIK",
    "ACDFGHIL": "HGFCADLI",
    "ACDFGHJK": "HGJCAFDK",
    "ACDFGHJL": "CGJDAFLH",
    "ACDFGHKL": "HGFCADLK",
    "ACDFGIJK": "CGJDAFIK",
    "ACDFGIJL": "CGJDAFLI",
    "ACDFGIKL": "CGIDAFLK",
    "ACDFGJKL": "CGJDAFLK",
    "ACDFHIJK": "HJFCADIK",
    "ACDFHIJL": "HJFCADLI",
    "ACDFHIKL": "HFICADLK",
    "ACDFHJKL": "HJFCADLK",
    "ACDFIJKL": "CJIDAFLK",
    "ACDGHIJK": "HGJCADIK",
    "ACDGHIJL": "HGJCADLI",
    "ACDGHIKL": "HGICADLK",
    "ACDGHJKL": "HGJCADLK",
    "ACDGIJKL": "IGJCADLK",
    "ACDHIJKL": "HJICADLK",
    "ACEFGHIJ": "HGJCAFEI",
    "ACEFGHIK": "HGECAFIK",
    "ACEFGHIL": "HGECAFLI",
    "ACEFGHJK": "HGJCAFEK",
    "ACEFGHJL": "HGJCAFLE",
    "ACEFGHKL": "HGECAFLK",
    "ACEFGIJK": "EGJCAFIK",
    "ACEFGIJL": "EGJCAFLI",
    "ACEFGIKL": "EGICAFLK",
    "ACEFGJKL": "EGJCAFLK",
    "ACEFHIJK": "HJECAFIK",
    "ACEFHIJL": "HJECAFLI",
    "ACEFHIKL": "HEICAFLK",
    "ACEFHJKL": "HJECAFLK",
    "ACEFIJKL": "EJICAFLK",
    "ACEGHIJK": "EGJCAHIK",
    "ACEGHIJL": "EGJCAHLI",
    "ACEGHIKL": "EGICAHLK",
    "ACEGHJKL": "EGJCAHLK",
    "ACEGIJKL": "EJICAGLK",
    "ACEHIJKL": "EJICAHLK",
    "ACFGHIJK": "HGJCAFIK",
    "ACFGHIJL": "HGJCAFLI",
    "ACFGHIKL": "HGICAFLK",
    "ACFGHJKL": "HGJCAFLK",
    "ACFGIJKL": "IGJCAFLK",
    "ACFHIJKL": "HJICAFLK",
    "ACGHIJKL": "HJICAGLK",
    "ADEFGHIJ": "HGJDAFEI",
    "ADEFGHIK": "HGEDAFIK",
    "ADEFGHIL": "HGEDAFLI",
    "ADEFGHJK": "HGJDAFEK",
    "ADEFGHJL": "HGJDAFLE",
    "ADEFGHKL": "HGEDAFLK",
    "ADEFGIJK": "EGJDAFIK",
    "ADEFGIJL": "EGJDAFLI",
    "ADEFGIKL": "EGIDAFLK",
    "ADEFGJKL": "EGJDAFLK",
    "ADEFHIJK": "HJEDAFIK",
    "ADEFHIJL": "HJEDAFLI",
    "ADEFHIKL": "HEIDAFLK",
    "ADEFHJKL": "HJEDAFLK",
    "ADEFIJKL": "EJIDAFLK",
    "ADEGHIJK": "EGJDAHIK",
    "ADEGHIJL": "EGJDAHLI",
    "ADEGHIKL": "EGIDAHLK",
    "ADEGHJKL": "EGJDAHLK",
    "ADEGIJKL": "EJIDAGLK",
    "ADEHIJKL": "EJIDAHLK",
    "ADFGHIJK": "HGJDAFIK",
    "ADFGHIJL": "HGJDAFLI",
    "ADFGHIKL": "HGIDAFLK",
    "ADFGHJKL": "HGJDAFLK",
    "ADFGIJKL": "IGJDAFLK",
    "ADFHIJKL": "HJIDAFLK",
    "ADGHIJKL": "HJIDAGLK",
    "AEFGHIJK": "EGJFAHIK",
    "AEFGHIJL": "EGJFAHLI",
    "AEFGHIKL": "EGIFAHLK",
    "AEFGHJKL": "EGJFAHLK",
    "AEFGIJKL": "EJIFAGLK",
    "AEFHIJKL": "EJIFAHLK",
    "AEGHIJKL": "EJIAHGLK",
    "AFGHIJKL": "HJIFAGLK",
    "BCDEFGHI": "CGBDHFEI",
    "BCDEFGHJ": "HGBCJFDE",
    "BCDEFGHK": "CGBDHFEK",
    "BCDEFGHL": "CGBDHFLE",
    "BCDEFGIJ": "CGBDJFEI",
    "BCDEFGIK": "CGBDEFIK",
    "BCDEFGIL": "CGBDEFLI",
    "BCDEFGJK": "CGBDJFEK",
    "BCDEFGJL": "CGBDJFLE",
    "BCDEFGKL": "CGBDEFLK",
    "BCDEFHIJ": "CJBDHFEI",
    "BCDEFHIK": "CEBDHFIK",
    "BCDEFHIL": "CEBDHFLI",
    "BCDEFHJK": "CJBDHFEK",
    "BCDEFHJL": "CJBDHFLE",
    "BCDEFHKL": "CEBDHFLK",
    "BCDEFIJK": "CJBDEFIK",
    "BCDEFIJL": "CJBDEFLI",
    "BCDEFIKL": "CEBDIFLK",
    "BCDEFJKL": "CJBDEFLK",
    "BCDEGHIJ": "HGBCJDEI",
    "BCDEGHIK": "EGBCHDIK",
    "BCDEGHIL": "EGBCHDLI",
    "BCDEGHJK": "HGBCJDEK",
    "BCDEGHJL": "HGBCJDLE",
    "BCDEGHKL": "EGBCHDLK",
    "BCDEGIJK": "EGBCJDIK",
    "BCDEGIJL": "EGBCJDLI",
    "BCDEGIKL": "EGBCIDLK",
    "BCDEGJKL": "EGBCJDLK",
    "BCDEHIJK": "EJBCHDIK",
    "BCDEHIJL": "EJBCHDLI",
    "BCDEHIKL": "EIBCHDLK",
    "BCDEHJKL": "EJBCHDLK",
    "BCDEIJKL": "EJBCIDLK",
    "BCDFGHIJ": "HGBCJFDI",
    "BCDFGHIK": "CGBDHFIK",
    "BCDFGHIL": "CGBDHFLI",
    "BCDFGHJK": "HGBCJFDK",
    "BCDFGHJL": "CGBDHFLJ",
    "BCDFGHKL": "CGBDHFLK",
    "BCDFGIJK": "CGBDJFIK",
    "BCDFGIJL": "CGBDJFLI",
    "BCDFGIKL": "CGBDIFLK",
    "BCDFGJKL": "CGBDJFLK",
    "BCDFHIJK": "CJBDHFIK",
    "BCDFHIJL": "CJBDHFLI",
    "BCDFHIKL": "CIBDHFLK",
    "BCDFHJKL": "CJBDHFLK",
    "BCDFIJKL": "CJBDIFLK",
    "BCDGHIJK": "HGBCJDIK",
    "BCDGHIJL": "HGBCJDLI",
    "BCDGHIKL": "HGBCIDLK",
    "BCDGHJKL": "HGBCJDLK",
    "BCDGIJKL": "IGBCJDLK",
    "BCDHIJKL": "HJBCIDLK",
    "BCEFGHIJ": "HGBCJFEI",
    "BCEFGHIK": "EGBCHFIK",
    "BCEFGHIL": "EGBCHFLI",
    "BCEFGHJK": "HGBCJFEK",
    "BCEFGHJL": "HGBCJFLE",
    "BCEFGHKL": "EGBCHFLK",
    "BCEFGIJK": "EGBCJFIK",
    "BCEFGIJL": "EGBCJFLI",
    "BCEFGIKL": "EGBCIFLK",
    "BCEFGJKL": "EGBCJFLK",
    "BCEFHIJK": "EJBCHFIK",
    "BCEFHIJL": "EJBCHFLI",
    "BCEFHIKL": "EIBCHFLK",
    "BCEFHJKL": "EJBCHFLK",
    "BCEFIJKL": "EJBCIFLK",
    "BCEGHIJK": "EJBCHGIK",
    "BCEGHIJL": "EJBCHGLI",
    "BCEGHIKL": "EGBCIHLK",
    "BCEGHJKL": "EJBCHGLK",
    "BCEGIJKL": "EJBCIGLK",
    "BCEHIJKL": "EJBCIHLK",
    "BCFGHIJK": "HGBCJFIK",
    "BCFGHIJL": "HGBCJFLI",
    "BCFGHIKL": "HGBCIFLK",
    "BCFGHJKL": "HGBCJFLK",
    "BCFGIJKL": "IGBCJFLK",
    "BCFHIJKL": "HJBCIFLK",
    "BCGHIJKL": "HJBCIGLK",
    "BDEFGHIJ": "HGBDJFEI",
    "BDEFGHIK": "EGBDHFIK",
    "BDEFGHIL": "EGBDHFLI",
    "BDEFGHJK": "HGBDJFEK",
    "BDEFGHJL": "HGBDJFLE",
    "BDEFGHKL": "EGBDHFLK",
    "BDEFGIJK": "EGBDJFIK",
    "BDEFGIJL": "EGBDJFLI",
    "BDEFGIKL": "EGBDIFLK",
    "BDEFGJKL": "EGBDJFLK",
    "BDEFHIJK": "EJBDHFIK",
    "BDEFHIJL": "EJBDHFLI",
    "BDEFHIKL": "EIBDHFLK",
    "BDEFHJKL": "EJBDHFLK",
    "BDEFIJKL": "EJBDIFLK",
    "BDEGHIJK": "EJBDHGIK",
    "BDEGHIJL": "EJBDHGLI",
    "BDEGHIKL": "EGBDIHLK",
    "BDEGHJKL": "EJBDHGLK",
    "BDEGIJKL": "EJBDIGLK",
    "BDEHIJKL": "EJBDIHLK",
    "BDFGHIJK": "HGBDJFIK",
    "BDFGHIJL": "HGBDJFLI",
    "BDFGHIKL": "HGBDIFLK",
    "BDFGHJKL": "HGBDJFLK",
    "BDFGIJKL": "IGBDJFLK",
    "BDFHIJKL": "HJBDIFLK",
    "BDGHIJKL": "HJBDIGLK",
    "BEFGHIJK": "EJBFHGIK",
    "BEFGHIJL": "EJBFHGLI",
    "BEFGHIKL": "EGBFIHLK",
    "BEFGHJKL": "EJBFHGLK",
    "BEFGIJKL": "EJBFIGLK",
    "BEFHIJKL": "EJBFIHLK",
    "BEGHIJKL": "EJIBHGLK",
    "BFGHIJKL": "HJBFIGLK",
    "CDEFGHIJ": "CGJDHFEI",
    "CDEFGHIK": "CGEDHFIK",
    "CDEFGHIL": "CGEDHFLI",
    "CDEFGHJK": "CGJDHFEK",
    "CDEFGHJL": "CGJDHFLE",
    "CDEFGHKL": "CGEDHFLK",
    "CDEFGIJK": "CGEDJFIK",
    "CDEFGIJL": "CGEDJFLI",
    "CDEFGIKL": "CGEDIFLK",
    "CDEFGJKL": "CGEDJFLK",
    "CDEFHIJK": "CJEDHFIK",
    "CDEFHIJL": "CJEDHFLI",
    "CDEFHIKL": "CEIDHFLK",
    "CDEFHJKL": "CJEDHFLK",
    "CDEFIJKL": "CJEDIFLK",
    "CDEGHIJK": "EGJCHDIK",
    "CDEGHIJL": "EGJCHDLI",
    "CDEGHIKL": "EGICHDLK",
    "CDEGHJKL": "EGJCHDLK",
    "CDEGIJKL": "EGICJDLK",
    "CDEHIJKL": "EJICHDLK",
    "CDFGHIJK": "CGJDHFIK",
    "CDFGHIJL": "CGJDHFLI",
    "CDFGHIKL": "CGIDHFLK",
    "CDFGHJKL": "CGJDHFLK",
    "CDFGIJKL": "CGIDJFLK",
    "CDFHIJKL": "CJIDHFLK",
    "CDGHIJKL": "HGICJDLK",
    "CEFGHIJK": "EGJCHFIK",
    "CEFGHIJL": "EGJCHFLI",
    "CEFGHIKL": "EGICHFLK",
    "CEFGHJKL": "EGJCHFLK",
    "CEFGIJKL": "EGICJFLK",
    "CEFHIJKL": "EJICHFLK",
    "CEGHIJKL": "EJICHGLK",
    "CFGHIJKL": "HGICJFLK",
    "DEFGHIJK": "EGJDHFIK",
    "DEFGHIJL": "EGJDHFLI",
    "DEFGHIKL": "EGIDHFLK",
    "DEFGHJKL": "EGJDHFLK",
    "DEFGIJKL": "EGIDJFLK",
    "DEFHIJKL": "EJIDHFLK",
    "DEGHIJKL": "EJIDHGLK",
    "DFGHIJKL": "HGIDJFLK",
    "EFGHIJKL": "EJIFHGLK",
}
