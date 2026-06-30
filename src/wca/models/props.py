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

# ---------------------------------------------------------------------------
# Documented fallback constants (mirror wca.data.matchevents fallbacks).  When
# no prop_priors table is injected, every model below falls back to exactly
# these numbers, reproducing today's behaviour bit-for-bit.  They are the
# method-of-moments fits on the 128-match StatsBomb WC18+22 sample.
# ---------------------------------------------------------------------------

# Per-TEAM league means (one team's count per match) — EB shrinkage targets and
# bare-prior fallbacks for the new team-aware paths.
FALLBACK_TEAM_MEAN = {
    "corners": 4.484,
    "sot": 4.16,
    "fouls": 14.262,
    "cards": 1.707,
}

# SoT specifics (no SoT in the StatsBomb props pull; on_target_ratio is the one
# external prior — WC literature ~0.345 — flagged for refit when SoT lands).
FALLBACK_SOT_RATIO = 0.345           # shots-on-target / shots, WC literature
FALLBACK_BASE_SHOTS = 12.5           # TEAM shots/match mean (exact, WC18+22)
FALLBACK_SOT_DISPERSION = 6.0        # team SoT NB k (shots k≈7.9; SoT tighter)
FALLBACK_SOT_PLAYER_DISPERSION = 4.0  # matches betbuilder PLAYER_DISPERSION

# Fouls specifics (exact MoM on team fouls).
FALLBACK_BASE_FOULS = 14.262
FALLBACK_FOULS_DISPERSION = 20.4
FALLBACK_FOULS_PLAYER_DISPERSION = 6.0

# Cards aggression coupling (fouls↔cards r=0.508 ⇒ sub-linear β).
FALLBACK_FOUL_BETA = 0.5


def _market_mean_from_priors(priors, market, entity=None):
    """Look up an EB team mean from a ``load_priors``-shaped nested dict.

    ``priors`` is the dict returned by :func:`wca.data.matchevents.load_priors`:
    ``{entity: {market: {"mean": float, ...}}}`` with a ``"GLOBAL"`` entity
    always present.  Returns ``None`` when ``priors`` is falsy (so the caller
    falls back to its hard-coded constant — the DEFAULT, file-absent path).

    Resolution order: the named ``entity`` -> ``GLOBAL`` -> ``None``.  Team
    names are looked up verbatim; callers that want canonicalisation should pass
    an already-canonical name (the matchevents loader stores canonical names).
    """
    if not priors:
        return None
    if entity is not None:
        ent = priors.get(entity)
        if ent and market in ent:
            try:
                return float(ent[market]["mean"])
            except (KeyError, TypeError, ValueError):
                pass
    glob = priors.get("GLOBAL")
    if glob and market in glob:
        try:
            return float(glob[market]["mean"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


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

    Team empirical-Bayes priors (A4, OPT-IN, fallback-guarded)
    ----------------------------------------------------------
    The following parameters are **additive** and default to values that keep
    every existing output bit-identical:

    team_priors : dict or None, optional
        A ``load_priors``-shaped nested dict
        (``{entity: {market: {"mean": ...}}}``) from
        :func:`wca.data.matchevents.load_priors`.  Default ``None`` -> no team
        priors -> the legacy 8.97 base path.  When supplied AND team names are
        passed to :meth:`mean_total`/:meth:`team_mean`, the per-fixture corner
        mean is built from EB-shrunk team corner rates (the ``corners`` market
        is a per-TEAM mean in the prior table) instead of the flat base.
    team_dispersion : float or None, optional
        NB dispersion ``k`` for TEAM corner counts.  Default ``None`` ->
        :attr:`dispersion` (legacy: team O/U uses the total k=157.5).  Pass a
        smaller k (the design recommends ~9.5 — team corners are overdispersed
        with var/mean≈1.47) to widen team-corner tails.  Only affects
        :meth:`prob_team_over` / :meth:`fair_odds_team_over_under`.
    league_team_mean : float, optional
        EB shrinkage target / bare fallback per-team corner mean
        (default 4.484, exact WC18+22).

    With ``team_priors=None`` and names omitted, ALL methods reproduce the
    legacy numbers exactly (two independent fallbacks: no priors, and the
    names-omitted legacy code path).
    """

    def __init__(self, base_corners=8.97, base_goals=3.07, dispersion=157.5,
                 elasticity=0.30, team_priors=None, team_dispersion=None,
                 league_team_mean=None):
        if base_corners <= 0 or base_goals <= 0 or dispersion <= 0:
            raise ValueError("base_corners, base_goals and dispersion must be > 0")
        if not 0.0 <= elasticity <= 1.0:
            raise ValueError("elasticity must be in [0, 1]")
        if team_dispersion is not None and team_dispersion <= 0:
            raise ValueError("team_dispersion must be > 0")
        if league_team_mean is not None and league_team_mean <= 0:
            raise ValueError("league_team_mean must be > 0")
        self.base_corners = float(base_corners)
        self.base_goals = float(base_goals)
        self.dispersion = float(dispersion)
        self.elasticity = float(elasticity)
        # Additive, fallback-guarded team-prior machinery (A4).
        self.team_priors = team_priors or None
        self.team_dispersion = (float(team_dispersion)
                                if team_dispersion is not None else None)
        self.league_team_mean = (float(league_team_mean)
                                 if league_team_mean is not None
                                 else FALLBACK_TEAM_MEAN["corners"])

    def _eb_team_corners(self, team, opponent):
        """EB per-team corner mean for ``team`` from injected priors.

        Returns the team's shrunk corners mean (already EB-shrunk in the prior
        table) when available, else the GLOBAL prior, else
        :attr:`league_team_mean` (the documented fallback).  Returns ``None``
        only when NO priors are injected — the caller then uses the legacy
        flat-base path so behaviour is unchanged.
        """
        if not self.team_priors:
            return None
        m = _market_mean_from_priors(self.team_priors, "corners", team)
        if m is None:
            return self.league_team_mean
        return m

    def mean_total(self, lambda_home, lambda_away, home=None, away=None):
        """Expected total corners for the match.

        Legacy path (``home``/``away`` omitted OR no team_priors injected):
        damped xG scaling around the flat base — byte-for-byte unchanged.

        Team-prior path (both names given AND team_priors injected): the two
        teams' EB corner means are summed, then nudged by the same damped xG
        term as a small second-order correction (design §2: EB prior primary,
        xG nudge secondary).
        """
        if lambda_home < 0 or lambda_away < 0:
            raise ValueError("expected goals must be non-negative")
        rel = (lambda_home + lambda_away) / self.base_goals - 1.0
        if home is None or away is None or not self.team_priors:
            return max(self.base_corners * (1.0 + self.elasticity * rel), 0.0)
        base = self._eb_team_corners(home, away) + self._eb_team_corners(away, home)
        return max(base * (1.0 + self.elasticity * rel), 0.0)

    def pmf(self, n, lambda_home, lambda_away, home=None, away=None):
        """P(total corners == n)."""
        return _nb_pmf(n, self.mean_total(lambda_home, lambda_away, home, away),
                       self.dispersion)

    def prob_over(self, line, lambda_home, lambda_away, home=None, away=None):
        """P(total corners > line) for a half-integer line (e.g. 9.5)."""
        return _nb_sf_over(line, self.mean_total(lambda_home, lambda_away, home, away),
                           self.dispersion)

    def fair_odds_over_under(self, line, lambda_home, lambda_away, home=None, away=None):
        """Fair decimal (over_odds, under_odds) at ``line``."""
        return _fair_odds(self.prob_over(line, lambda_home, lambda_away, home, away))

    def team_mean(self, lambda_team, lambda_opponent, team=None, opponent=None):
        """Expected corners for one team.

        Legacy path (names omitted OR no team_priors): total mean times the
        team's attack share ``lambda_team / (lambda_team + lambda_opponent)`` —
        unchanged.

        Team-prior path: the team's EB corner mean, nudged by the same damped
        xG term.
        """
        if (team is None or opponent is None) or not self.team_priors:
            total = self.mean_total(lambda_team, lambda_opponent)
            denom = lambda_team + lambda_opponent
            if denom <= 0:
                return 0.0
            return total * lambda_team / denom
        if lambda_team < 0 or lambda_opponent < 0:
            raise ValueError("expected goals must be non-negative")
        rel = (lambda_team + lambda_opponent) / self.base_goals - 1.0
        return max(self._eb_team_corners(team, opponent) * (1.0 + self.elasticity * rel), 0.0)

    def _team_k(self):
        return self.team_dispersion if self.team_dispersion is not None else self.dispersion

    def prob_team_over(self, line, lambda_team, lambda_opponent,
                       team=None, opponent=None):
        """P(team corners > line); team mean via attack-share split or EB prior.

        Dispersion: :attr:`team_dispersion` if set, else :attr:`dispersion`
        (legacy default — the total k, so team O/U is unchanged when
        team_dispersion is None).
        """
        return _nb_sf_over(line, self.team_mean(lambda_team, lambda_opponent, team, opponent),
                           self._team_k())

    def fair_odds_team_over_under(self, line, lambda_team, lambda_opponent,
                                  team=None, opponent=None):
        """Fair decimal (over_odds, under_odds) for team corners."""
        return _fair_odds(self.prob_team_over(line, lambda_team, lambda_opponent,
                                              team, opponent))


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

    Referee / foul priors (A5, OPT-IN, fallback-guarded)
    ----------------------------------------------------
    league_foul_mean : float, optional
        Per-team league foul mean (default 14.262, exact WC18+22), the
        denominator in :meth:`aggression_from_fouls`.
    foul_beta : float, optional
        Sub-linear exponent mapping foul rate -> aggression (default 0.5;
        fouls↔cards r=0.508, not 1.0 — flagged for a small grid-search refit).
    ref_factor : float, optional
        Default referee multiplier (default 1.0 -> back-compat).  A per-call
        ``ref_factor`` on :meth:`mean_total` overrides it.

    With everything at its default (aggression=1.0, ref_factor=1.0) every
    method reproduces 3.41 (and today's NB) exactly.
    """

    def __init__(self, base_cards=3.41, dispersion=6.9,
                 league_foul_mean=None, foul_beta=None, ref_factor=1.0):
        if base_cards <= 0 or dispersion <= 0:
            raise ValueError("base_cards and dispersion must be > 0")
        if ref_factor < 0:
            raise ValueError("ref_factor must be non-negative")
        if league_foul_mean is not None and league_foul_mean <= 0:
            raise ValueError("league_foul_mean must be > 0")
        self.base_cards = float(base_cards)
        self.dispersion = float(dispersion)
        self.league_foul_mean = (float(league_foul_mean)
                                 if league_foul_mean is not None
                                 else FALLBACK_BASE_FOULS)
        self.foul_beta = (float(foul_beta) if foul_beta is not None
                          else FALLBACK_FOUL_BETA)
        self.ref_factor = float(ref_factor)

    def aggression_from_fouls(self, foul_rate_team):
        """Map a team foul rate to a multiplicative aggression factor.

        ``agg = (foul_rate_team / league_foul_mean) ** foul_beta``.  Returns
        1.0 (the FALLBACK / no-op) when ``foul_rate_team`` is missing or
        non-positive — so a caller with no foul data leaves cards at base.
        Callers opt in by computing this from a :class:`FoulsModel` team mean
        and passing the result as ``aggression_home``/``aggression_away``.
        """
        if foul_rate_team is None or foul_rate_team <= 0:
            return 1.0
        return (foul_rate_team / self.league_foul_mean) ** self.foul_beta

    def mean_total(self, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0,
                   ref_factor=None):
        """Expected total cards for the match.

        ``ref_factor`` defaults to the instance value (1.0 unless set), so the
        signature stays back-compatible and every existing call reproduces the
        base rate.
        """
        if aggression_home < 0 or aggression_away < 0 or stakes_mult < 0:
            raise ValueError("aggression and stakes multipliers must be non-negative")
        rf = self.ref_factor if ref_factor is None else float(ref_factor)
        if rf < 0:
            raise ValueError("ref_factor must be non-negative")
        return self.base_cards * aggression_home * aggression_away * stakes_mult * rf

    def pmf(self, n, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0,
            ref_factor=None):
        """P(total cards == n)."""
        mu = self.mean_total(aggression_home, aggression_away, stakes_mult, ref_factor)
        return _nb_pmf(n, mu, self.dispersion)

    def prob_over(self, line, aggression_home=1.0, aggression_away=1.0, stakes_mult=1.0,
                  ref_factor=None):
        """P(total cards > line) for a half-integer line (e.g. 3.5)."""
        mu = self.mean_total(aggression_home, aggression_away, stakes_mult, ref_factor)
        return _nb_sf_over(line, mu, self.dispersion)

    def fair_odds_over_under(self, line, aggression_home=1.0, aggression_away=1.0,
                             stakes_mult=1.0, ref_factor=None):
        """Fair decimal (over_odds, under_odds) at ``line``."""
        return _fair_odds(self.prob_over(line, aggression_home, aggression_away,
                                         stakes_mult, ref_factor))


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


class ShotsOnTargetModel:
    """Team & player shots-on-target as Negative Binomial (A3, NEW).

    Team SoT scales off team xG via shots (shots↔xG r=0.696; reuse the
    ``betbuilder`` shot elasticity 0.6)::

        shots    = base_shots * (1 + elasticity * (lambda_team / base_lambda - 1))
        team SoT = shots * on_target_ratio

    Player SoT is a Poisson/NB thinning of the team SoT process by the player's
    shot share, minutes-prorated::

        player SoT = team_sot_mean * player_shot_share * (expected_minutes / 90)

    Data limit (honest)
    -------------------
    Shots-on-target is **absent** from the StatsBomb props pull, so
    ``on_target_ratio`` is an *external* prior (WC literature ~0.345) until a
    SoT pull lands.  When a ``prop_priors`` table that carries a ``sot`` market
    is injected, the team SoT mean is taken from the EB ``sot`` prior directly
    (the on_target_ratio path is then a fallback only).  With NO priors
    injected the model uses the documented constant — nothing is fabricated.

    Parameters
    ----------
    base_shots : float, optional
        Team shots/match mean (default 12.5, exact WC18+22).
    base_lambda : float, optional
        Reference team xG the elasticity is centred on (default 1.35,
        ``betbuilder.BASE_TEAM_LAMBDA``).
    on_target_ratio : float, optional
        SoT/shots ratio (default 0.345, WC literature — flagged for refit).
    elasticity : float, optional
        xG->shots elasticity in [0, 1] (default 0.6, ``SHOT_ELASTICITY``).
    dispersion : float, optional
        Team NB dispersion ``k`` (default 6.0).
    player_dispersion : float, optional
        Player NB dispersion ``k`` (default 4.0).
    team_priors : dict or None, optional
        ``load_priors``-shaped dict; when given and a ``sot`` market is present
        for the team (or GLOBAL), the team SoT mean is read from it instead of
        the shots*ratio construction.  Default ``None`` -> constant path.
    """

    def __init__(self, base_shots=None, base_lambda=1.35, on_target_ratio=None,
                 elasticity=0.6, dispersion=None, player_dispersion=None,
                 team_priors=None):
        bs = FALLBACK_BASE_SHOTS if base_shots is None else float(base_shots)
        otr = FALLBACK_SOT_RATIO if on_target_ratio is None else float(on_target_ratio)
        disp = FALLBACK_SOT_DISPERSION if dispersion is None else float(dispersion)
        pdisp = (FALLBACK_SOT_PLAYER_DISPERSION if player_dispersion is None
                 else float(player_dispersion))
        if bs <= 0 or base_lambda <= 0 or disp <= 0 or pdisp <= 0:
            raise ValueError("base_shots, base_lambda, dispersion, "
                             "player_dispersion must be > 0")
        if not 0.0 < otr <= 1.0:
            raise ValueError("on_target_ratio must be in (0, 1]")
        if not 0.0 <= elasticity <= 1.0:
            raise ValueError("elasticity must be in [0, 1]")
        self.base_shots = bs
        self.base_lambda = float(base_lambda)
        self.on_target_ratio = otr
        self.elasticity = float(elasticity)
        self.dispersion = disp
        self.player_dispersion = pdisp
        self.team_priors = team_priors or None

    def team_mean(self, lambda_team, team=None):
        """Expected team shots-on-target.

        If a ``sot`` prior is injected for ``team`` (or GLOBAL), use it;
        otherwise build it from ``base_shots * scaling * on_target_ratio``.
        """
        if lambda_team < 0:
            raise ValueError("lambda_team must be non-negative")
        prior = (_market_mean_from_priors(self.team_priors, "sot", team)
                 if self.team_priors else None)
        if prior is not None:
            return max(prior, 0.0)
        rel = lambda_team / self.base_lambda - 1.0
        shots = max(self.base_shots * (1.0 + self.elasticity * rel), 0.0)
        return shots * self.on_target_ratio

    def prob_team_over(self, line, lambda_team, team=None):
        """P(team SoT > line) for a half-integer line."""
        return _nb_sf_over(line, self.team_mean(lambda_team, team), self.dispersion)

    def fair_odds_team_over_under(self, line, lambda_team, team=None):
        """Fair decimal (over_odds, under_odds) for team SoT."""
        return _fair_odds(self.prob_team_over(line, lambda_team, team))

    def player_mean(self, lambda_team, player_shot_share, expected_minutes=90.0,
                    team=None):
        """Expected player SoT = team mean * shot share * minutes/90."""
        if not 0.0 <= player_shot_share <= 1.0:
            raise ValueError("player_shot_share must be in [0, 1]")
        if expected_minutes < 0:
            raise ValueError("expected_minutes must be non-negative")
        return (self.team_mean(lambda_team, team) * player_shot_share
                * (expected_minutes / 90.0))

    def prob_player_over(self, line, lambda_team, player_shot_share,
                         expected_minutes=90.0, team=None):
        """P(player SoT > line) for a half-integer line."""
        mu = self.player_mean(lambda_team, player_shot_share, expected_minutes, team)
        return _nb_sf_over(line, mu, self.player_dispersion)

    def fair_odds_player_over_under(self, line, lambda_team, player_shot_share,
                                    expected_minutes=90.0, team=None):
        """Fair decimal (over_odds, under_odds) for player SoT."""
        return _fair_odds(self.prob_player_over(line, lambda_team, player_shot_share,
                                                expected_minutes, team))


class FoulsModel:
    """Team & player fouls committed as Negative Binomial (A4, NEW).

    The team foul mean is an EB-shrunk team prior (read from an injected
    ``prop_priors`` ``fouls`` market) and falls back to the league mean
    (14.262, exact WC18+22) when no prior is available.  The same team-foul
    estimate double-duties as the :class:`CardsModel` aggression driver via
    :meth:`CardsModel.aggression_from_fouls` (fouls↔cards r=0.508).

    Parameters
    ----------
    base_fouls : float, optional
        League per-team foul mean / fallback (default 14.262).
    dispersion : float, optional
        Team NB dispersion ``k`` (default 20.4, exact MoM).
    player_dispersion : float, optional
        Player NB dispersion ``k`` (default 6.0, ``betbuilder``).
    team_priors : dict or None, optional
        ``load_priors``-shaped dict; team foul means read from the ``fouls``
        market when present.  Default ``None`` -> flat league-mean fallback.
    """

    def __init__(self, base_fouls=None, dispersion=None, player_dispersion=None,
                 team_priors=None):
        bf = FALLBACK_BASE_FOULS if base_fouls is None else float(base_fouls)
        disp = FALLBACK_FOULS_DISPERSION if dispersion is None else float(dispersion)
        pdisp = (FALLBACK_FOULS_PLAYER_DISPERSION if player_dispersion is None
                 else float(player_dispersion))
        if bf <= 0 or disp <= 0 or pdisp <= 0:
            raise ValueError("base_fouls, dispersion, player_dispersion must be > 0")
        self.base_fouls = bf
        self.dispersion = disp
        self.player_dispersion = pdisp
        self.team_priors = team_priors or None

    def team_mean(self, team=None, opponent=None):
        """Expected team fouls.

        EB team prior when injected & present; else the league mean
        :attr:`base_fouls` (the documented fallback).  ``opponent`` is accepted
        for API symmetry / future opponent adjustment but unused today (kept
        deliberately conservative — no fabricated coupling).
        """
        prior = (_market_mean_from_priors(self.team_priors, "fouls", team)
                 if self.team_priors else None)
        if prior is None:
            return self.base_fouls
        return max(prior, 0.0)

    def prob_team_over(self, line, team=None, opponent=None):
        """P(team fouls > line) for a half-integer line."""
        return _nb_sf_over(line, self.team_mean(team, opponent), self.dispersion)

    def fair_odds_team_over_under(self, line, team=None, opponent=None):
        """Fair decimal (over_odds, under_odds) for team fouls."""
        return _fair_odds(self.prob_team_over(line, team, opponent))

    def player_mean(self, player_foul_share, team=None, expected_minutes=90.0):
        """Expected player fouls = team mean * foul share * minutes/90."""
        if not 0.0 <= player_foul_share <= 1.0:
            raise ValueError("player_foul_share must be in [0, 1]")
        if expected_minutes < 0:
            raise ValueError("expected_minutes must be non-negative")
        return self.team_mean(team) * player_foul_share * (expected_minutes / 90.0)

    def prob_player_over(self, line, player_foul_share, team=None,
                         expected_minutes=90.0):
        """P(player fouls > line) for a half-integer line."""
        mu = self.player_mean(player_foul_share, team, expected_minutes)
        return _nb_sf_over(line, mu, self.player_dispersion)

    def fair_odds_player_over_under(self, line, player_foul_share, team=None,
                                    expected_minutes=90.0):
        """Fair decimal (over_odds, under_odds) for player fouls."""
        return _fair_odds(self.prob_player_over(line, player_foul_share, team,
                                                expected_minutes))
