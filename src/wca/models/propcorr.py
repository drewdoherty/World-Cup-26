"""Correlation between a player prop and their team's match result.

``exposure_corr`` deliberately scopes itself to same-fixture 1X2-vs-1X2; it
has no machinery for the bet-builder leg this desk actually prices now:
*team result (or advancement) x player prop* (e.g. "Egypt to advance" +
"Salah over 0.5 SoT"). Multiplying marginals there is WRONG — a winning team
takes more shots, so its players' shot/SoT/goal props are positively
correlated with the win leg and negatively with the loss leg.

Model (deliberately simple, every knob explicit)
------------------------------------------------
The DC scoreline matrix already gives ``P(h, a)``. Conditional on the team
scoring ``g`` goals, the player's event rate over the match scales as::

    lam(g) = rate_p90 * minutes/90 * (1 + beta * (g / lam_team - 1))

``beta`` is the elasticity of the player's volume to team output. beta=0
recovers independence (joint == product of marginals — tested); beta=1 is
full proportional scaling. Default ``BETA = 0.7``: attacking volume tracks
team goals sub-linearly (a team can dominate shots and win 1-0), and the
exact value should be re-fit from ``player_events.db`` once enough current-
tournament rows exist — it is a parameter, not a claim.

The joint over a result leg R is then::

    P(prop >= k AND R) = sum_{(h,a) in R} P(h,a) * PoissonSF(k; lam(g_team))

Settlement bases (do NOT mix silently):
* result legs here are 90-minute results (the DC matrix is 90');
* an ADVANCEMENT leg (PM: includes ET+pens) is approximated as
  ``win + p_win_given_level * draw`` with ``p_win_given_level`` explicit
  (default 0.5). The PLAYER PROP still settles at 90' at sportsbooks; a
  PM-side prop would accrue ET minutes — flagged, not modelled.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np

from wca.models.playerprops import poisson_at_least

#: Elasticity of player attacking volume to team goals. Re-fit from
#: player_events.db when current-tournament sample allows; see module doc.
BETA = 0.7

RESULTS = ("win", "draw", "loss")


def _team_axis_matrix(score_matrix: np.ndarray, team_is_home: bool) -> np.ndarray:
    """Orient the matrix so axis 0 = the PLAYER'S team goals."""
    m = np.asarray(score_matrix, dtype=float)
    return m if team_is_home else m.T


def _result_mask(n_team: int, n_opp: int, result: str) -> np.ndarray:
    t = np.arange(n_team)[:, None]
    o = np.arange(n_opp)[None, :]
    if result == "win":
        return t > o
    if result == "draw":
        return t == o
    if result == "loss":
        return t < o
    raise ValueError("result must be one of %s" % (RESULTS,))


def joint_result_prop_prob(
    score_matrix: np.ndarray,
    team_is_home: bool,
    result: str,
    rate_p90: float,
    expected_minutes: float,
    threshold: int,
    *,
    team_lambda: Optional[float] = None,
    beta: float = BETA,
) -> float:
    """``P(player prop >= threshold AND team result)`` — 90-minute basis.

    ``score_matrix[h, a] = P(home h, away a)`` (any square-ish truncation);
    ``team_lambda`` defaults to the matrix-implied mean of the team's goals.
    """
    m = _team_axis_matrix(score_matrix, team_is_home)
    m = m / m.sum() if m.sum() > 0 else m
    n_t, n_o = m.shape
    goals = np.arange(n_t, dtype=float)
    lam_team = team_lambda if team_lambda and team_lambda > 0 else float(
        (m.sum(axis=1) * goals).sum())
    if lam_team <= 0:
        lam_team = 1e-9

    base = max(0.0, float(rate_p90)) * float(expected_minutes) / 90.0
    mask = _result_mask(n_t, n_o, result)

    total = 0.0
    for g in range(n_t):
        p_g_and_r = float(m[g][mask[g]].sum())
        if p_g_and_r <= 0.0:
            continue
        lam_g = base * max(0.0, 1.0 + beta * (g / lam_team - 1.0))
        total += p_g_and_r * poisson_at_least(int(threshold), lam_g)
    return total


def prop_marginal_prob(
    score_matrix: np.ndarray,
    team_is_home: bool,
    rate_p90: float,
    expected_minutes: float,
    threshold: int,
    *,
    team_lambda: Optional[float] = None,
    beta: float = BETA,
) -> float:
    """Marginal ``P(prop >= threshold)`` under the SAME goal-coupled model
    (so joint/marginal comparisons are internally consistent)."""
    return sum(
        joint_result_prop_prob(
            score_matrix, team_is_home, r, rate_p90, expected_minutes,
            threshold, team_lambda=team_lambda, beta=beta)
        for r in RESULTS
    )


def result_prop_combo(
    score_matrix: np.ndarray,
    team_is_home: bool,
    result: str,
    rate_p90: float,
    expected_minutes: float,
    threshold: int,
    *,
    team_lambda: Optional[float] = None,
    beta: float = BETA,
) -> Dict[str, float]:
    """Price a (team result x player prop) builder leg pair, with the
    correlation made explicit.

    Returns joint prob, both marginals, the naive independent product, the
    correlation uplift (joint / product), and fair odds for the joint.
    """
    joint = joint_result_prop_prob(
        score_matrix, team_is_home, result, rate_p90, expected_minutes,
        threshold, team_lambda=team_lambda, beta=beta)
    m = _team_axis_matrix(score_matrix, team_is_home)
    m = m / m.sum() if m.sum() > 0 else m
    p_result = float(m[_result_mask(*m.shape, result)].sum())
    p_prop = prop_marginal_prob(
        score_matrix, team_is_home, rate_p90, expected_minutes, threshold,
        team_lambda=team_lambda, beta=beta)
    naive = p_result * p_prop
    return {
        "joint_prob": joint,
        "p_result": p_result,
        "p_prop": p_prop,
        "naive_product": naive,
        "corr_uplift": (joint / naive) if naive > 0 else float("nan"),
        "fair_odds": (1.0 / joint) if joint > 0 else float("inf"),
        "beta": beta,
        "settlement": "90min_result x 90min_prop",
    }


def advancement_prop_combo(
    score_matrix: np.ndarray,
    team_is_home: bool,
    rate_p90: float,
    expected_minutes: float,
    threshold: int,
    *,
    p_win_given_level: float = 0.5,
    team_lambda: Optional[float] = None,
    beta: float = BETA,
) -> Dict[str, float]:
    """(Team ADVANCES x player prop) for a knockout tie.

    Advance = 90' win + (90' draw x ``p_win_given_level``), the ET/pens
    coin-flip made explicit (pass the sim's estimate when available). The
    prop is still 90-minute (sportsbook basis); a PM-settled prop accruing ET
    minutes is NOT modelled — the caller must not mix that in silently.
    """
    win = result_prop_combo(
        score_matrix, team_is_home, "win", rate_p90, expected_minutes,
        threshold, team_lambda=team_lambda, beta=beta)
    draw = result_prop_combo(
        score_matrix, team_is_home, "draw", rate_p90, expected_minutes,
        threshold, team_lambda=team_lambda, beta=beta)
    q = min(1.0, max(0.0, float(p_win_given_level)))
    joint = win["joint_prob"] + q * draw["joint_prob"]
    p_adv = win["p_result"] + q * draw["p_result"]
    p_prop = win["p_prop"]  # marginal is result-independent by construction
    naive = p_adv * p_prop
    return {
        "joint_prob": joint,
        "p_result": p_adv,
        "p_prop": p_prop,
        "naive_product": naive,
        "corr_uplift": (joint / naive) if naive > 0 else float("nan"),
        "fair_odds": (1.0 / joint) if joint > 0 else float("inf"),
        "beta": beta,
        "p_win_given_level": q,
        "settlement": "advance_incl_ET_pens x 90min_prop (prop ET minutes NOT modelled)",
    }
