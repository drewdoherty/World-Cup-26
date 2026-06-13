"""Player-level goalscorer pricing built on :class:`AnytimeScorerModel`.

Given a team's expected goals (the Dixon-Coles ``lambda``) this module prices a
player's **anytime**, **first-goalscorer**, **brace (2+)** and **hat-trick (3+)**
probabilities, and evaluates promotional boosts — notably Betfred's
*Double Delight & Hat-Trick Heaven*: a FIRST-GOALSCORER **single** whose odds
**double** if the player also scores a 2nd and **treble** on a 3rd.

Why a separate layer
--------------------
:class:`wca.models.props.AnytimeScorerModel` is the pure intensity/Poisson
engine and takes a ``player_share`` (the player's share of the team's
*non-penalty* xG) as an injected parameter. The historical StatsBomb dataset
(``data/processed/props_players.csv``) only covers WC2018+2022, so squads that
did not feature there — e.g. **Scotland 2026** — have no empirical share. This
module adds the missing piece: a small ``data/players.json`` override store of
analyst-estimated player params, merged with whatever empirical shares exist,
plus the boost-EV maths the offer desk actually needs.

The shares in the override store are estimates pending a live xG / penalty-taker
feed; ``source`` on each record flags provenance so they can be replaced without
code changes.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from wca.models.props import AnytimeScorerModel


@dataclass
class ScorerLine:
    """Priced goalscorer markets for one player in one fixture."""

    player: str
    team: str
    intensity: float          # Poisson goal intensity lambda_p
    p_anytime: float          # P(scores >= 1)
    p_first: float            # P(scores the match's first goal)
    p_two_plus: float         # P(scores >= 2) — brace
    p_three_plus: float       # P(scores >= 3) — hat-trick
    fair_anytime: float       # 1 / p_anytime
    fair_first: float         # 1 / p_first


@dataclass
class PlayerParams:
    """Inputs for one player (from the override store or an empirical share)."""

    name: str
    team: str
    npxg_share: float            # share of team NON-penalty xG, in [0, 1]
    penalty_taker: bool = False
    expected_minutes: float = 90.0
    source: str = "override"


class ScorerPricer:
    """Price player goalscorer markets and promotional boosts.

    Parameters
    ----------
    pen_xg:
        Team penalty xG awarded to the designated taker (default 0.18), passed
        straight through to :class:`AnytimeScorerModel`.
    """

    def __init__(self, pen_xg: float = 0.18) -> None:
        self.pen_xg = float(pen_xg)
        self._model = AnytimeScorerModel(pen_xg=pen_xg)

    # -- core intensity (mirrors AnytimeScorerModel._intensity, kept explicit) --
    def intensity(
        self,
        team_lambda: float,
        npxg_share: float,
        expected_minutes: float = 90.0,
        penalty_taker: bool = False,
    ) -> float:
        """Poisson scoring intensity ``lambda_p`` for the player."""
        if not 0.0 <= npxg_share <= 1.0:
            raise ValueError("npxg_share must be in [0, 1]")
        if team_lambda < 0:
            raise ValueError("team_lambda must be non-negative")
        if expected_minutes < 0:
            raise ValueError("expected_minutes must be non-negative")
        frac = expected_minutes / 90.0
        lam_np = max(team_lambda - self.pen_xg, 0.0) * npxg_share
        pen = self.pen_xg if penalty_taker else 0.0
        return (lam_np + pen) * frac

    def price(
        self,
        team_lambda: float,
        total_lambda: float,
        npxg_share: float,
        expected_minutes: float = 90.0,
        penalty_taker: bool = False,
        player: str = "",
        team: str = "",
    ) -> ScorerLine:
        """Return a :class:`ScorerLine` of priced markets for the player.

        ``total_lambda`` is the combined match expected goals
        (``lambda_home + lambda_away``) used for the first-scorer split.
        """
        lam = self.intensity(team_lambda, npxg_share, expected_minutes, penalty_taker)
        e = math.exp(-lam)
        p_any = 1.0 - e
        # Poisson tail masses for >=2 and >=3 goals by the same player.
        p_two = 1.0 - e * (1.0 + lam)
        p_three = 1.0 - e * (1.0 + lam + lam * lam / 2.0)
        p_first = self._model.prob_first_scorer(
            team_lambda, npxg_share, total_lambda, expected_minutes, penalty_taker
        )
        inf = float("inf")
        return ScorerLine(
            player=player,
            team=team,
            intensity=lam,
            p_anytime=p_any,
            p_first=p_first,
            p_two_plus=p_two,
            p_three_plus=p_three,
            fair_anytime=(1.0 / p_any if p_any > 0 else inf),
            fair_first=(1.0 / p_first if p_first > 0 else inf),
        )

    def price_player(
        self, params: PlayerParams, team_lambda: float, total_lambda: float
    ) -> ScorerLine:
        """Convenience wrapper: price a :class:`PlayerParams` record."""
        return self.price(
            team_lambda=team_lambda,
            total_lambda=total_lambda,
            npxg_share=params.npxg_share,
            expected_minutes=params.expected_minutes,
            penalty_taker=params.penalty_taker,
            player=params.name,
            team=params.team,
        )

    def double_delight_ev(self, line: ScorerLine, offered_first_odds: float) -> Dict[str, float]:
        """EV of a FIRST-GOALSCORER single under Double Delight / Hat-Trick Heaven.

        The bet pays ``offered_first_odds`` if the player scores the first goal;
        the odds **double** if that player also scores a 2nd, and **treble** on a
        3rd. The goal-count split, conditional on the player having scored, uses
        ``P(>=k) / P(>=1)``.

        Returns a dict with ``ev_per_unit`` (expected return per £1 staked,
        i.e. 1.0 is break-even), ``edge_pct``, the boost's ``effective_mult``
        on the offered odds, and ``ev_no_boost`` for comparison.
        """
        if offered_first_odds <= 1.0:
            raise ValueError("offered_first_odds must be > 1.0 (decimal)")
        pa = line.p_anytime
        if pa <= 0:
            return {"ev_per_unit": 0.0, "edge_pct": -100.0, "effective_mult": 0.0, "ev_no_boost": 0.0}
        p2_given1 = line.p_two_plus / pa
        p3_given1 = line.p_three_plus / pa
        exact1 = 1.0 - p2_given1            # scored, but only once
        exact2 = p2_given1 - p3_given1      # exactly a brace
        three_plus = p3_given1              # hat-trick or more
        odds = offered_first_odds
        ev = line.p_first * (exact1 * odds + exact2 * 2.0 * odds + three_plus * 3.0 * odds)
        effective_mult = exact1 + 2.0 * exact2 + 3.0 * three_plus
        return {
            "ev_per_unit": ev,
            "edge_pct": (ev - 1.0) * 100.0,
            "effective_mult": effective_mult,
            "ev_no_boost": line.p_first * odds,
        }


# ---------------------------------------------------------------------------
# Player override store (data/players.json)
# ---------------------------------------------------------------------------

def load_player_overrides(path: str = "data/players.json") -> Dict[str, List[PlayerParams]]:
    """Load analyst player params keyed by canonical team name.

    Keys beginning with ``_`` (notes/schema) are ignored. Returns an empty dict
    when the file is missing so callers degrade gracefully.
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    out: Dict[str, List[PlayerParams]] = {}
    for team, players in raw.items():
        if team.startswith("_"):
            continue
        recs: List[PlayerParams] = []
        for p in players:
            recs.append(
                PlayerParams(
                    name=p["name"],
                    team=team,
                    npxg_share=float(p["npxg_share"]),
                    penalty_taker=bool(p.get("penalty_taker", False)),
                    expected_minutes=float(p.get("expected_minutes", 90.0)),
                    source=str(p.get("source", "override")),
                )
            )
        out[team] = recs
    return out


def players_for_team(
    team: str, path: str = "data/players.json"
) -> List[PlayerParams]:
    """Return override player params for ``team`` (exact key match), or []."""
    return load_player_overrides(path).get(team, [])
