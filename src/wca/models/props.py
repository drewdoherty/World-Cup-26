"""Prop-market models: corners, cards, anytime/first goalscorer.

All three models are pure functions/classes over their inputs: no file IO and
no network. Calibration constants (tournament base rates, dispersions,
aggression priors, player shares) are constructor or method arguments with
sensible defaults, so a data pipeline can refit and inject them later without
touching this module.

Negative-binomial parameterisation
----------------------------------
Counts are modelled as Negative Binomial with mean ``mu`` and dispersion
``k > 0`` so that::

    Var = mu + mu**2 / k

This maps onto :func:`scipy.stats.nbinom` via::

    r = k
    p = k / (k + mu)

As ``k -> inf`` the NB converges to a Poisson with mean ``mu``.

Over/under lines are half-integers (8.5, 9.5, ...), so no continuity
correction is applied anywhere::

    P(over L) = 1 - CDF(floor(L))
"""

from __future__ import annotations

import math

from scipy.stats import nbinom


def _nb_pmf(n, mu, k):
    """NB pmf at ``n`` for mean ``mu``, dispersion ``k`` (r=k, p=k/(k+mu))."""
    if mu <= 0:
        return 1.0 if n == 0 else 0.0
    p = k / (k + mu)
    return float(nbinom.pmf(n, k, p))


def _nb_sf_over(line, mu, k):
    """P(N > line) for a half-integer ``line``: 1 - CDF(floor(line))."""
    if mu <= 0:
        return 0.0
    p = k / (k + mu)
    return float(nbinom.sf(math.floor(line), k, p))


def _fair_odds(p_over):
    """(over_odds, under_odds) as fair decimal odds 1/p; inf at p=0."""
    p_under = 1.0 - p_over
    over = float("inf") if p_over <= 0 else 1.0 / p_over
    under = float("inf") if p_under <= 0 else 1.0 / p_under
    return over, under


class CornersModel:
    """Total/team corners as Negative Binomial with damped xG scaling.

    Calibrated on StatsBomb WC 2018+2022 (128 matches, see
    ``scripts/wca_props_data.py``): corners/match mean 8.97, var 9.48 —
    var/mean ~ 1.06, i.e. *nearly Poisson* (k_mm ~ 158), and match-level
    corners correlate only weakly with goals (r=0.02) and xG (r=0.15).
    Full proportional scaling in expected goals would therefore badly
    overstate the spread of corner means across fixtures, so the mean uses a
    damped elasticity around the tournament base rate::

        mu = base_corners * (1 + elasticity * ((lambda_h + lambda_a) / base_goals - 1))

    With ``elasticity = 0`` every match gets the base rate; ``1`` recovers
    naive proportional scaling. Default 0.30 reflects the weak observed
    xG-corners coupling.

    Parameters
    ----------
    base_corners : float, optional
        Tournament average total corners per match (default 8.97, WC18+22).
    base_goals : float, optional
        Tournament average total goals per match in the same data, including
        knockout extra time (default 3.07).
    dispersion : float, optional
        NB dispersion ``k`` (default 157.5 — method-of-moments fit; corner
        counts at WC level are nearly Poisson).
    elasticity : float, optional
        Damping of the xG scaling in [0, 1] (default 0.30).
    """

    def __init__(self, base_corners=8.97, base_goals=3.07, dispersion=157.5,
                 elasticity=0.30):
        if base_corners <= 0 or base_goals <= 0 or dispersion <= 0:
            raise ValueError("base_corners, base_goals and dispersion must be > 0")
        if not 0.0 <= elasticity <= 1.0:
            raise ValueError("elasticity must be in [0, 1]")
        self.base_corners = float(base_corners)
        self.base_goals = float(base_goals)
        self.dispersion = float(dispersion)
        self.elasticity = float(elasticity)

    def mean_total(self, lambda_home, lambda_away):
        """Expected total corners for the match (damped xG scaling)."""
        if lambda_home < 0 or lambda_away < 0:
            raise ValueError("expected goals must be non-negative")
        rel = (lambda_home + lambda_away) / self.base_goals - 1.0
        return max(self.base_corners * (1.0 + self.elasticity * rel), 0.0)

    def pmf(self, n, lambda_home, lambda_away):
        """P(total corners == n)."""
        return _nb_pmf(n, self.mean_total(lambda_home, lambda_away), self.dispersion)

    def prob_over(self, line, lambda_home, lambda_away):
        """P(total corners > line) for a half-integer line (e.g. 9.5)."""
        return _nb_sf_over(line, self.mean_total(lambda_home, lambda_away), self.dispersion)

    def fair_odds_over_under(self, line, lambda_home, lambda_away):
        """Fair decimal (over_odds, under_odds) at ``line``."""
        return _fair_odds(self.prob_over(line, lambda_home, lambda_away))

    def team_mean(self, lambda_team, lambda_opponent):
        """Expected corners for one team: total mean times its attack share.

        The split is proportional to the attack share
        ``lambda_team / (lambda_team + lambda_opponent)``.
        """
        total = self.mean_total(lambda_team, lambda_opponent)
        denom = lambda_team + lambda_opponent
        if denom <= 0:
            return 0.0
        return total * lambda_team / denom

    def prob_team_over(self, line, lambda_team, lambda_opponent):
        """P(team corners > line); team mean via attack-share split.

        The team count is modelled NB with the same dispersion ``k`` (a
        simplification; refit later if team-level dispersion differs).
        """
        return _nb_sf_over(line, self.team_mean(lambda_team, lambda_opponent), self.dispersion)

    def fair_odds_team_over_under(self, line, lambda_team, lambda_opponent):
        """Fair decimal (over_odds, under_odds) for team corners."""
        return _fair_odds(self.prob_team_over(line, lambda_team, lambda_opponent))


class CardsModel:
    """Total cards as Negative Binomial with multiplicative aggression.

    Mean::

        mu = base_cards * aggression_home * aggression_away * stakes_mult

    Parameters
    ----------
    base_cards : float, optional
        Tournament average total cards per match (default 3.41 — StatsBomb
        WC 2018+2022, second yellow counted as one red).
    dispersion : float, optional
        NB dispersion ``k`` (default 6.9 — method-of-moments fit on the same
        data; cards are strongly overdispersed, var/mean ~ 1.5).

    Notes
    -----
    ``aggression_home`` / ``aggression_away`` default to 1.0 and will be
    injected from team foul-rate priors by the data pipeline; ``stakes_mult``
    is a caller-supplied knockout/derby bump (default 1.0).
    """

    def __init__(self, base_cards=3.41, dispersion=6.9):
        if base_cards <= 0 or dispersion <= 0:
            raise ValueError("base_cards and dispersion must be > 0")
        self.base_cards = float(base_cards)
        self.dispersion = float(dispersion)

    def mean_total(self, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0):
        """Expected total cards for the match."""
        if aggression_home < 0 or aggression_away < 0 or stakes_mult < 0:
            raise ValueError("aggression and stakes multipliers must be non-negative")
        return self.base_cards * aggression_home * aggression_away * stakes_mult

    def pmf(self, n, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0):
        """P(total cards == n)."""
        mu = self.mean_total(aggression_home, aggression_away, stakes_mult)
        return _nb_pmf(n, mu, self.dispersion)

    def prob_over(self, line, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0):
        """P(total cards > line) for a half-integer line (e.g. 3.5)."""
        mu = self.mean_total(aggression_home, aggression_away, stakes_mult)
        return _nb_sf_over(line, mu, self.dispersion)

    def fair_odds_over_under(self, line, aggression_home=1.0, aggression_away=1.0,
                             stakes_mult=1.0):
        """Fair decimal (over_odds, under_odds) at ``line``."""
        return _fair_odds(self.prob_over(line, aggression_home, aggression_away, stakes_mult))


class AnytimeScorerModel:
    """Poisson-thinning anytime / first goalscorer model.

    A player's scoring intensity is a minutes-prorated share of the team's
    *non-penalty* Dixon-Coles expected goals plus a penalty add-on::

        lam_p = (max(lambda_team - pen_xg, 0) * s + pen) * (m / 90)

    where ``s`` is the player's non-penalty xG share, ``pen = pen_xg`` if the
    player is the designated taker else 0, and ``P(anytime) = 1 - exp(-lam_p)``.
    Splitting penalty xG out keeps takers from being double-counted (their
    npxG share already excludes penalties in the data pipeline).

    Parameters
    ----------
    pen_xg : float, optional
        Team penalty xG awarded to the designated taker, default 0.18
        (≈ league penalty xG per match times the taker's share). To be
        refit by the data pipeline alongside player shares ``s``.
    """

    def __init__(self, pen_xg=0.18):
        if pen_xg < 0:
            raise ValueError("pen_xg must be non-negative")
        self.pen_xg = float(pen_xg)

    def _intensity(self, lambda_team, player_share, expected_minutes, penalty_taker):
        if not 0.0 <= player_share <= 1.0:
            raise ValueError("player_share must be in [0, 1]")
        if lambda_team < 0:
            raise ValueError("lambda_team must be non-negative")
        if expected_minutes < 0:
            raise ValueError("expected_minutes must be non-negative")
        frac = expected_minutes / 90.0
        # player_share is a NON-penalty xG share, so apply it to the team's
        # non-penalty goal expectation only; the penalty top-up is a separate
        # term for the designated taker (no double count).
        lam_np = max(lambda_team - self.pen_xg, 0.0) * player_share
        pen = self.pen_xg if penalty_taker else 0.0
        return (lam_np + pen) * frac

    def prob_anytime(self, lambda_team, player_share, expected_minutes=90.0,
                     penalty_taker=False):
        """P(player scores at least once) = 1 - exp(-lam_p)."""
        lam_p = self._intensity(lambda_team, player_share, expected_minutes, penalty_taker)
        return 1.0 - math.exp(-lam_p)

    def fair_odds_anytime(self, lambda_team, player_share, expected_minutes=90.0,
                          penalty_taker=False):
        """Fair decimal odds 1/P(anytime); inf when the probability is 0."""
        p = self.prob_anytime(lambda_team, player_share, expected_minutes, penalty_taker)
        return float("inf") if p <= 0 else 1.0 / p

    def prob_first_scorer(self, lambda_team, player_share, lambda_total,
                          expected_minutes=90.0, penalty_taker=False):
        """Approximate P(player scores the first goal of the match).

        Approximation: with all scoring processes Poisson and homogeneous in
        time, given that at least one goal is scored the first goal belongs
        to the player with probability ``lam_p / lam_total``, hence::

            P(first) ≈ (lam_p / lam_total) * (1 - exp(-lam_total))

        This ignores the non-homogeneity introduced by ``expected_minutes``
        (a substitute cannot score the first goal before coming on) and
        treats the player's intensity as independent of teammates'. It is a
        standard, slightly generous approximation for sub appearances.

        ``lambda_total`` is the combined match expected goals
        (lambda_home + lambda_away).
        """
        if lambda_total <= 0:
            return 0.0
        lam_p = self._intensity(lambda_team, player_share, expected_minutes, penalty_taker)
        return (lam_p / lambda_total) * (1.0 - math.exp(-lambda_total))
