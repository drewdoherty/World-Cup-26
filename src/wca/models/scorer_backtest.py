"""Out-of-sample backtest of the player-aware anytime-scorer model vs a
no-player-awareness baseline, on the 2022 World Cup.

Why this comparison
-------------------
The v1 system (Elo + Dixon-Coles + Shin de-vig) is a **1X2 match-outcome**
model — it prices no players at all. So the honest test of the Phase-2
player-level edge is at the *scorer* level: does distributing a team's expected
goals across players by their StatsBomb non-penalty-xG share (the player-aware
model) beat the naive baseline that has no player information and spreads the
goals equally across the players who appeared?

Leakage control
---------------
Player shares are learned on **WC2018** and evaluated on **WC2022** — strictly
out of sample. Both models are handed the *same* per-match inputs (the team goal
expectation and each player's realised minutes), so the only thing that differs
is the share. Using realised minutes is not leakage of the label (who scored):
it is the lineup, shared identically by both models.

Metrics: multiclass-free binary Brier and log-loss on "did the player score in
this match", aggregated over every covered player-match.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from wca.data import statsbomb


def _norm(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def brier_one(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss_one(p: float, y: int, eps: float = 1e-12) -> float:
    p = min(max(p, eps), 1.0 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1.0 - p))


# ---------------------------------------------------------------------------
# Training: player npxg-shares from a tournament's events
# ---------------------------------------------------------------------------

def team_npxg_shares(events_by_match: Dict[int, list]) -> Dict[str, Dict[str, float]]:
    """``{team: {norm_player: share_of_team_npxg}}`` from StatsBomb events.

    Share = player non-penalty xG / team total non-penalty xG over the events.
    """
    df = statsbomb.player_shares(events_by_match)
    out: Dict[str, Dict[str, float]] = {}
    if df.empty:
        return out
    totals = df.groupby("team")["npxg_sum"].sum().to_dict()
    for _, r in df.iterrows():
        team = str(r["team"])
        tot = totals.get(team, 0.0)
        if tot <= 0:
            continue
        out.setdefault(team, {})[_norm(r["player"])] = float(r["npxg_sum"]) / tot
    return out


# ---------------------------------------------------------------------------
# Test: per-match scorer observations
# ---------------------------------------------------------------------------

@dataclass
class PlayerMatch:
    player: str
    team: str
    minutes: float
    scored: bool


def match_scorer_observations(events: list) -> List[PlayerMatch]:
    """Appearing players, minutes and whether they scored, for one match.

    A "score" is a Shot with outcome Goal by the player (own goals are excluded,
    matching anytime-scorer market settlement).
    """
    mins = statsbomb._match_minutes(events)
    scorers = set()
    for ev in events:
        if (ev.get("type") or {}).get("name") != "Shot":
            continue
        if ((ev.get("shot") or {}).get("outcome") or {}).get("name") == "Goal":
            player = (ev.get("player") or {}).get("name")
            team = (ev.get("team") or {}).get("name")
            if player:
                scorers.add((player, team))
    obs = []
    for (player, team), m in mins.items():
        if m <= 0:
            continue
        obs.append(PlayerMatch(player, team, float(m), (player, team) in scorers))
    return obs


def tournament_team_goal_mean(events_by_match: Dict[int, list]) -> float:
    """Mean goals scored by a team in a match (per-side), for the lambda prior."""
    goals = 0
    sides = 0
    for events in events_by_match.values():
        try:
            home, away = statsbomb._home_away_teams(events)
        except ValueError:
            continue
        props = statsbomb.match_props(events, home, away)
        goals += props["goals_home"] + props["goals_away"]
        sides += 2
    return goals / sides if sides else 1.3


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class BacktestResult:
    n_covered: int
    n_uncovered: int
    n_matches: int
    lambda_team: float
    pa_brier: float
    pa_log_loss: float
    base_brier: float
    base_log_loss: float

    @property
    def coverage(self) -> float:
        tot = self.n_covered + self.n_uncovered
        return self.n_covered / tot if tot else 0.0

    @property
    def brier_improvement(self) -> float:
        return self.base_brier - self.pa_brier

    @property
    def log_loss_improvement(self) -> float:
        return self.base_log_loss - self.pa_log_loss

    @property
    def recommend_adopt(self) -> bool:
        """Adopt only if the player-aware model wins on BOTH metrics."""
        return self.brier_improvement > 0 and self.log_loss_improvement > 0


def _p_anytime(lambda_team: float, share: float, minutes: float) -> float:
    lam = max(lambda_team, 0.0) * max(share, 0.0) * (minutes / 90.0)
    return 1.0 - math.exp(-lam)


def evaluate(
    obs_by_match: Dict[int, List[PlayerMatch]],
    shares: Dict[str, Dict[str, float]],
    lambda_team: float,
) -> BacktestResult:
    """Score player-aware vs equal-share baseline over covered player-matches.

    Only player-matches whose player has an out-of-sample share are scored, and
    both models are scored on that same set for a fair head-to-head. The
    baseline spreads ``lambda_team`` equally across the team's appearing players.
    """
    pa_b = pa_l = base_b = base_l = 0.0
    n_cov = n_unc = 0
    for events_obs in obs_by_match.values():
        # team -> count of appearing players (baseline denominator)
        team_counts: Dict[str, int] = {}
        for pm in events_obs:
            team_counts[pm.team] = team_counts.get(pm.team, 0) + 1
        for pm in events_obs:
            share = shares.get(pm.team, {}).get(_norm(pm.player))
            if share is None:
                n_unc += 1
                continue
            n_cov += 1
            y = 1 if pm.scored else 0
            p_pa = _p_anytime(lambda_team, share, pm.minutes)
            n_team = max(team_counts.get(pm.team, 1), 1)
            p_bl = _p_anytime(lambda_team, 1.0 / n_team, pm.minutes)
            pa_b += brier_one(p_pa, y)
            pa_l += log_loss_one(p_pa, y)
            base_b += brier_one(p_bl, y)
            base_l += log_loss_one(p_bl, y)
    n = max(n_cov, 1)
    return BacktestResult(
        n_covered=n_cov, n_uncovered=n_unc, n_matches=len(obs_by_match),
        lambda_team=lambda_team,
        pa_brier=pa_b / n, pa_log_loss=pa_l / n,
        base_brier=base_b / n, base_log_loss=base_l / n)


@dataclass
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    observed: float


def reliability(
    obs_by_match: Dict[int, List[PlayerMatch]],
    shares: Dict[str, Dict[str, float]],
    lambda_team: float,
    n_bins: int = 5,
) -> Tuple[List[ReliabilityBin], float]:
    """Reliability bins for the player-aware model + the global calibration scale.

    Each bin reports mean predicted P(anytime) vs the observed scoring rate; a
    well-calibrated model has ``mean_pred ≈ observed`` in every bin. The returned
    scale = sum(observed) / sum(predicted): <1 means the model is over-confident
    and its probabilities should be multiplied by ~scale (a first-order
    temperature/shrinkage correction).
    """
    preds: List[Tuple[float, int]] = []
    for events_obs in obs_by_match.values():
        for pm in events_obs:
            share = shares.get(pm.team, {}).get(_norm(pm.player))
            if share is None:
                continue
            p = _p_anytime(lambda_team, share, pm.minutes)
            preds.append((p, 1 if pm.scored else 0))
    bins: List[ReliabilityBin] = []
    sum_p = sum(p for p, _ in preds) or 1e-9
    sum_y = sum(y for _, y in preds)
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        sel = [(p, y) for p, y in preds if (lo <= p < hi or (i == n_bins - 1 and p == hi))]
        if not sel:
            bins.append(ReliabilityBin(lo, hi, 0, 0.0, 0.0))
            continue
        mp = sum(p for p, _ in sel) / len(sel)
        ob = sum(y for _, y in sel) / len(sel)
        bins.append(ReliabilityBin(lo, hi, len(sel), mp, ob))
    return bins, sum_y / sum_p


def run_backtest(
    train_events: Dict[int, list],
    test_events: Dict[int, list],
    lambda_team: Optional[float] = None,
) -> BacktestResult:
    """End-to-end: learn shares on ``train_events``, evaluate on ``test_events``."""
    shares = team_npxg_shares(train_events)
    if lambda_team is None:
        lambda_team = tournament_team_goal_mean(train_events)
    obs_by_match = {mid: match_scorer_observations(evs)
                    for mid, evs in test_events.items()}
    return evaluate(obs_by_match, shares, lambda_team)
