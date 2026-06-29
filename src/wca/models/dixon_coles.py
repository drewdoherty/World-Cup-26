"""Time-decayed Dixon-Coles (1997) model for international football.

This module implements the bivariate-Poisson-with-correction goals model of

    Dixon, M. J. and Coles, S. G. (1997). "Modelling Association Football
    Scores and Inefficiencies in the Football Betting Market." Journal of the
    Royal Statistical Society: Series C (Applied Statistics), 46(2):265-280.

together with the exponential time-decay weighting proposed in

    Dixon, M. J. and Robinson, M. E. (1998) and popularised by Hvattum &
    Arntzen (2010) / Opisthokonta's blog series on weighted Dixon-Coles. The
    weighting scheme used here is the one analysed in

        Hvattum, L. M. and Arntzen, H. (2010). "Using ELO ratings for match
        result prediction in association football." International Journal of
        Forecasting, 26(3):460-470.

Model
-----
For a match between a home team ``i`` and an away team ``j`` the goal counts
``(X, Y)`` are modelled as (approximately) independent Poisson variables with
means::

    log lambda_home = mu + attack_i - defence_j + gamma   (gamma only if NOT neutral)
    log lambda_away = mu + attack_j - defence_i

The marginal independence assumption is corrected at low scores by the
Dixon-Coles ``tau`` adjustment, which depends on a single dependence parameter
``rho``::

    tau(x, y) =  1 - lambda*mu_*rho   if (x, y) == (0, 0)
                 1 + lambda*rho       if (x, y) == (0, 1)
                 1 + mu_*rho          if (x, y) == (1, 0)
                 1 - rho              if (x, y) == (1, 1)
                 1                    otherwise

where ``lambda`` (= ``lambda_home``) and ``mu_`` (= ``lambda_away``) are the two
Poisson means. The joint probability of the scoreline ``(x, y)`` is::

    P(X=x, Y=y) = tau(x, y) * Pois(x; lambda_home) * Pois(y; lambda_away)

When ``rho == 0`` the model reduces exactly to two independent Poisson margins.

Identifiability
---------------
``mu`` and ``gamma`` are free, while the per-team attack/defence vectors are
constrained to be mean-zero (``mean(attack) == 0`` and ``mean(defence) == 0``),
which removes the additive degeneracy ``attack_i -> attack_i + c``,
``mu -> mu - c``. This is enforced by optimising over free per-team values and
re-centring inside the likelihood.

Regularisation
--------------
A ridge (L2) penalty ``reg_lambda * (||attack||^2 + ||defence||^2)`` shrinks
team strengths toward the global mean. For international football, where many
"minnow" teams have only a handful of matches, this shrinkage is essential to
avoid wildly overfit parameters; teams below ``min_matches`` are additionally
shrunk with a stronger penalty (``low_data_reg_multiplier``).

Time decay
----------
Each match carries a weight::

    w = exp(-xi * days_ago / 365.25)

with ``xi`` expressed *per year*. A half-life of ``H`` years (the age at which a
match's weight has halved) satisfies ``exp(-xi * H) = 1/2``, i.e.::

    xi = ln(2) / H.

The default ``xi`` corresponds to ``H = 2`` years, so
``xi = ln(2) / 2 ~= 0.34657``. Larger ``xi`` forgets the past faster.
"""

from __future__ import annotations

import json
import math
import warnings
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # pandas is a hard dependency of the package; import defensively for typing
    import pandas as pd
except Exception:  # pragma: no cover - pandas is always present at runtime
    pd = None  # type: ignore

from scipy.optimize import minimize
from scipy.stats import poisson


_EPS = 1e-12

#: Default decay half-life in years.
DEFAULT_HALF_LIFE_YEARS = 2.0


def xi_from_half_life(half_life_years: float) -> float:
    """Convert a decay half-life (in years) to the per-year decay rate ``xi``.

    A match of age ``H`` years should carry weight ``1/2``, i.e.
    ``exp(-xi * H) = 1/2`` so ``xi = ln(2) / H``.
    """
    if half_life_years <= 0:
        raise ValueError("half_life_years must be positive")
    return math.log(2.0) / float(half_life_years)


def half_life_from_xi(xi: float) -> float:
    """Inverse of :func:`xi_from_half_life`: half-life in years for a given ``xi``."""
    if xi <= 0:
        return math.inf
    return math.log(2.0) / float(xi)


#: Default per-year decay rate (half-life of two years).
DEFAULT_XI = xi_from_half_life(DEFAULT_HALF_LIFE_YEARS)


def decay_weights(days_ago: np.ndarray, xi: float) -> np.ndarray:
    """Exponential time-decay weights ``exp(-xi * days_ago / 365.25)``.

    Parameters
    ----------
    days_ago:
        Non-negative array of match ages in days, relative to a reference date.
    xi:
        Per-year decay rate. ``xi == 0`` yields uniform (all-ones) weights.
    """
    days_ago = np.asarray(days_ago, dtype=float)
    if xi == 0:
        return np.ones_like(days_ago)
    return np.exp(-xi * days_ago / 365.25)


# ---------------------------------------------------------------------------
# Scoreline prediction container.
# ---------------------------------------------------------------------------


class ScorelinePrediction:
    """Derived quantities from a Dixon-Coles score-probability matrix.

    Parameters
    ----------
    matrix:
        ``(max_goals + 1, max_goals + 1)`` array where ``matrix[x, y]`` is the
        probability of the home team scoring ``x`` and the away team scoring
        ``y``. Rows index home goals, columns away goals.
    home:
        Home-team label (informational).
    away:
        Away-team label (informational).
    lambda_home, lambda_away:
        The fitted Poisson means used to build the matrix.
    """

    def __init__(
        self,
        matrix: np.ndarray,
        home: str,
        away: str,
        lambda_home: float,
        lambda_away: float,
    ) -> None:
        self.matrix = np.asarray(matrix, dtype=float)
        self.home = home
        self.away = away
        self.lambda_home = float(lambda_home)
        self.lambda_away = float(lambda_away)

    @property
    def max_goals(self) -> int:
        """Largest goal count represented on each axis."""
        return self.matrix.shape[0] - 1

    # -- 1X2 ----------------------------------------------------------------

    def outcome_probs(self) -> Dict[str, float]:
        """Return ``{"home": ph, "draw": pd, "away": pa}`` (1X2 market)."""
        p_home = float(np.tril(self.matrix, k=-1).sum())  # home goals > away goals
        p_draw = float(np.trace(self.matrix))
        p_away = float(np.triu(self.matrix, k=1).sum())  # away goals > home goals
        return {"home": p_home, "draw": p_draw, "away": p_away}

    def one_x_two(self) -> Tuple[float, float, float]:
        """Return ``(p_home, p_draw, p_away)`` as a tuple."""
        o = self.outcome_probs()
        return o["home"], o["draw"], o["away"]

    # -- totals -------------------------------------------------------------

    def over_under(self, line: float = 2.5) -> Dict[str, float]:
        """Over/under probabilities for a goals ``line`` (default 2.5).

        ``line`` is typically a half-integer so a push is impossible. For an
        integer line, exact totals equal to the line are reported separately as
        ``push``.
        """
        total = (
            np.add.outer(
                np.arange(self.matrix.shape[0]),
                np.arange(self.matrix.shape[1]),
            )
        )
        over = float(self.matrix[total > line].sum())
        under = float(self.matrix[total < line].sum())
        push = float(self.matrix[total == line].sum())
        return {"over": over, "under": under, "push": push}

    # -- BTTS ---------------------------------------------------------------

    def both_teams_to_score(self) -> Dict[str, float]:
        """Probability both teams score (``yes``) versus not (``no``)."""
        # P(both score) = 1 - P(home=0) - P(away=0) + P(0,0)
        p_home_zero = float(self.matrix[0, :].sum())
        p_away_zero = float(self.matrix[:, 0].sum())
        p_both_zero = float(self.matrix[0, 0])
        yes = 1.0 - p_home_zero - p_away_zero + p_both_zero
        yes = float(min(max(yes, 0.0), 1.0))
        return {"yes": yes, "no": 1.0 - yes}

    # -- correct score ------------------------------------------------------

    def top_correct_scores(self, k: int = 5) -> List[Tuple[Tuple[int, int], float]]:
        """Return the ``k`` most probable exact scorelines.

        Each element is ``((home_goals, away_goals), probability)`` sorted in
        descending probability order.
        """
        flat = self.matrix.ravel()
        n = self.matrix.shape[1]
        k = int(min(k, flat.size))
        idx = np.argpartition(flat, -k)[-k:]
        idx = idx[np.argsort(flat[idx])[::-1]]
        out: List[Tuple[Tuple[int, int], float]] = []
        for fi in idx:
            x, y = divmod(int(fi), n)
            out.append(((x, y), float(flat[fi])))
        return out

    # -- expected goals -----------------------------------------------------

    def expected_goals(self) -> Tuple[float, float]:
        """Expected goals ``(home, away)`` computed from the *matrix*.

        These can differ very slightly from the underlying Poisson means when
        the matrix is truncated at ``max_goals`` and after the tau correction.
        """
        rows = np.arange(self.matrix.shape[0])
        cols = np.arange(self.matrix.shape[1])
        eh = float((rows * self.matrix.sum(axis=1)).sum())
        ea = float((cols * self.matrix.sum(axis=0)).sum())
        return eh, ea

    def total_probability(self) -> float:
        """Sum of the score matrix (should be ~1 after normalisation)."""
        return float(self.matrix.sum())

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matrix": self.matrix.tolist(),
            "home": self.home,
            "away": self.away,
            "lambda_home": self.lambda_home,
            "lambda_away": self.lambda_away,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScorelinePrediction":
        return cls(
            matrix=np.asarray(data["matrix"], dtype=float),
            home=data.get("home", ""),
            away=data.get("away", ""),
            lambda_home=float(data.get("lambda_home", 0.0)),
            lambda_away=float(data.get("lambda_away", 0.0)),
        )


# ---------------------------------------------------------------------------
# tau correction.
# ---------------------------------------------------------------------------


def dc_tau(
    x: np.ndarray,
    y: np.ndarray,
    lambda_home: np.ndarray,
    lambda_away: np.ndarray,
    rho: float,
) -> np.ndarray:
    """Vectorised Dixon-Coles low-score dependence correction ``tau``.

    See Dixon and Coles (1997), eq. (4.4). Only the four low scorelines
    ``(0,0), (0,1), (1,0), (1,1)`` are adjusted; every other scoreline returns
    ``1``.
    """
    x = np.asarray(x)
    y = np.asarray(y)
    tau = np.ones(np.broadcast(x, y, lambda_home, lambda_away).shape, dtype=float)

    m00 = (x == 0) & (y == 0)
    m01 = (x == 0) & (y == 1)
    m10 = (x == 1) & (y == 0)
    m11 = (x == 1) & (y == 1)

    lh = np.broadcast_to(lambda_home, tau.shape)
    la = np.broadcast_to(lambda_away, tau.shape)

    tau[m00] = 1.0 - lh[m00] * la[m00] * rho
    tau[m01] = 1.0 + lh[m01] * rho
    tau[m10] = 1.0 + la[m10] * rho
    tau[m11] = 1.0 - rho
    return tau


# ---------------------------------------------------------------------------
# The model.
# ---------------------------------------------------------------------------


class DixonColesModel:
    """Time-decayed, ridge-regularised Dixon-Coles model.

    Parameters
    ----------
    xi:
        Per-year time-decay rate. Defaults to :data:`DEFAULT_XI` (two-year
        half-life). Pass ``xi=0`` to disable decay. Mutually exclusive with
        ``half_life_years``.
    half_life_years:
        Convenience alternative to ``xi``; if given, ``xi`` is derived via
        :func:`xi_from_half_life`.
    reg_lambda:
        Ridge penalty weight on the attack/defence vectors. Larger values shrink
        team strengths harder toward the league mean.
    min_matches:
        Teams whose (unweighted) match count is below this threshold are treated
        as "low data" and receive an extra ``low_data_reg_multiplier`` times the
        ridge penalty, pulling them toward the baseline prior.
    low_data_reg_multiplier:
        Extra ridge multiplier applied to low-data teams (see ``min_matches``).
    max_goals:
        Default truncation of the score matrix used by :meth:`predict`.
    attack_prior, defence_prior:
        Optional ``{team: value}`` shrinkage *targets* (in log-goal units) for the
        ridge penalty. When given, the penalty pulls each team's attack/defence
        toward its prior instead of toward zero — the structural-prior path (see
        :mod:`wca.models.structural`). The priors are re-centred mean-zero over
        the fitted team set to preserve identifiability. When ``None`` (default)
        the targets are zero and the model is identical to the classic
        shrink-to-global-mean Dixon-Coles. Most valuable for low-data teams,
        whose weak likelihood lets the (stronger) ridge dominate; swamped by the
        likelihood for data-rich teams.
    """

    def __init__(
        self,
        xi: Optional[float] = None,
        half_life_years: Optional[float] = None,
        reg_lambda: float = 0.01,
        min_matches: int = 5,
        low_data_reg_multiplier: float = 5.0,
        max_goals: int = 10,
        attack_prior: Optional[Dict[str, float]] = None,
        defence_prior: Optional[Dict[str, float]] = None,
        goal_supply_boost: float = 1.0,
    ) -> None:
        if xi is not None and half_life_years is not None:
            raise ValueError("pass at most one of xi / half_life_years")
        if half_life_years is not None:
            xi = xi_from_half_life(half_life_years)
        if xi is None:
            xi = DEFAULT_XI
        if xi < 0:
            raise ValueError("xi must be non-negative")

        self.xi = float(xi)
        self.reg_lambda = float(reg_lambda)
        self.min_matches = int(min_matches)
        self.low_data_reg_multiplier = float(low_data_reg_multiplier)
        self.max_goals = int(max_goals)
        # Post-hoc multiplicative correction on BOTH Poisson means, applied at
        # prediction time (not during the fit). 1.0 = no change. Used to correct
        # a calibrated goal-supply level shift the historical fit can't see —
        # e.g. an in-tournament goal glut. Set/justified by
        # ``scripts/wca_dc_goal_calibration.py``. Symmetric, so it shifts
        # totals/scorelines/BTTS without distorting the 1X2 home/away balance.
        if goal_supply_boost <= 0:
            raise ValueError("goal_supply_boost must be positive")
        self.goal_supply_boost = float(goal_supply_boost)
        # Structural shrinkage targets (raw, as supplied). The per-fit, mean-zero
        # re-centred versions are stored as ``_attack_prior_c`` / ``_defence_prior_c``.
        self.attack_prior: Dict[str, float] = dict(attack_prior) if attack_prior else {}
        self.defence_prior: Dict[str, float] = dict(defence_prior) if defence_prior else {}
        self._attack_prior_c: Dict[str, float] = {}
        self._defence_prior_c: Dict[str, float] = {}

        # Fitted state.
        self.teams: List[str] = []
        self._team_index: Dict[str, int] = {}
        self.attack: Dict[str, float] = {}
        self.defence: Dict[str, float] = {}
        self.mu: float = 0.0
        self.home_advantage: float = 0.0
        self.rho: float = 0.0
        self.match_counts: Dict[str, int] = {}
        self.fitted: bool = False

    @property
    def half_life_years(self) -> float:
        """Decay half-life in years implied by ``xi``."""
        return half_life_from_xi(self.xi)

    # -- prior for unseen / low-data teams ----------------------------------

    @property
    def prior_attack(self) -> float:
        """Attack value assigned to an unseen team (the mean-zero baseline)."""
        return 0.0

    @property
    def prior_defence(self) -> float:
        """Defence value assigned to an unseen team (the mean-zero baseline)."""
        return 0.0

    def _attack_of(self, team: str, warn: bool = True) -> float:
        if team in self.attack:
            return self.attack[team]
        # Unseen team: prefer its (mean-zero re-centred) structural prior if one
        # was supplied, else the zero baseline.
        fallback = self._attack_prior_c.get(team, self.prior_attack)
        if warn:
            warnings.warn(
                "Unseen team %r; falling back to baseline prior (attack=%.3f)."
                % (team, fallback),
                RuntimeWarning,
                stacklevel=2,
            )
        return fallback

    def _defence_of(self, team: str, warn: bool = True) -> float:
        if team in self.defence:
            return self.defence[team]
        fallback = self._defence_prior_c.get(team, self.prior_defence)
        if warn:
            warnings.warn(
                "Unseen team %r; falling back to baseline prior (defence=%.3f)."
                % (team, fallback),
                RuntimeWarning,
                stacklevel=2,
            )
        return fallback

    # -- fitting ------------------------------------------------------------

    def fit(
        self,
        home_teams: Sequence[str],
        away_teams: Sequence[str],
        home_goals: Sequence[int],
        away_goals: Sequence[int],
        days_ago: Optional[Sequence[float]] = None,
        neutral: Optional[Sequence[bool]] = None,
        maxiter: int = 500,
    ) -> "DixonColesModel":
        """Penalised weighted maximum-likelihood fit.

        Parameters
        ----------
        home_teams, away_teams:
            Team labels for each match.
        home_goals, away_goals:
            Integer goal counts.
        days_ago:
            Age of each match in days relative to the reference date. If
            ``None``, all matches are treated as same-aged (no decay effect).
        neutral:
            Boolean flag per match; home advantage is applied only where
            ``neutral`` is ``False``. If ``None``, every match is treated as a
            home/away (non-neutral) fixture.
        maxiter:
            Maximum L-BFGS-B iterations.
        """
        home_teams = list(home_teams)
        away_teams = list(away_teams)
        hg = np.asarray(home_goals, dtype=float)
        ag = np.asarray(away_goals, dtype=float)
        n = len(home_teams)
        if not (len(away_teams) == n == hg.shape[0] == ag.shape[0]):
            raise ValueError("all match arrays must share the same length")
        if n == 0:
            raise ValueError("cannot fit on empty data")

        if days_ago is None:
            days = np.zeros(n, dtype=float)
        else:
            days = np.asarray(days_ago, dtype=float)
        if neutral is None:
            is_neutral = np.zeros(n, dtype=bool)
        else:
            is_neutral = np.asarray(neutral, dtype=bool)

        weights = decay_weights(days, self.xi)

        # Build team index over the union of home/away labels.
        teams = sorted(set(home_teams) | set(away_teams))
        self.teams = teams
        self._team_index = {t: i for i, t in enumerate(teams)}
        n_teams = len(teams)

        hi = np.fromiter((self._team_index[t] for t in home_teams), dtype=np.intp, count=n)
        ai = np.fromiter((self._team_index[t] for t in away_teams), dtype=np.intp, count=n)
        not_neutral = (~is_neutral).astype(float)

        # Per-team match counts (unweighted) drive low-data shrinkage.
        counts = np.zeros(n_teams, dtype=float)
        np.add.at(counts, hi, 1)
        np.add.at(counts, ai, 1)
        self.match_counts = {t: int(counts[i]) for i, t in enumerate(teams)}

        # Per-team ridge weight: stronger for low-data teams.
        ridge = np.full(n_teams, self.reg_lambda, dtype=float)
        low_data = counts < self.min_matches
        ridge[low_data] = self.reg_lambda * self.low_data_reg_multiplier

        # Structural shrinkage targets, aligned to the team index and re-centred
        # mean-zero (to match the mean-zero attack/defence parameterisation).
        # Empty/absent priors => zero vectors => classic shrink-to-mean behaviour.
        atk_prior_vec = np.array(
            [self.attack_prior.get(t, 0.0) for t in teams], dtype=float
        )
        dfc_prior_vec = np.array(
            [self.defence_prior.get(t, 0.0) for t in teams], dtype=float
        )
        atk_prior_vec = atk_prior_vec - atk_prior_vec.mean()
        dfc_prior_vec = dfc_prior_vec - dfc_prior_vec.mean()
        self._attack_prior_c = {t: float(atk_prior_vec[i]) for i, t in enumerate(teams)}
        self._defence_prior_c = {t: float(dfc_prior_vec[i]) for i, t in enumerate(teams)}

        # Precompute index masks for the four corrected scorelines so the
        # log-likelihood is fully vectorised.
        x = hg.astype(int)
        y = ag.astype(int)
        m00 = (x == 0) & (y == 0)
        m01 = (x == 0) & (y == 1)
        m10 = (x == 1) & (y == 0)
        m11 = (x == 1) & (y == 1)
        # log Poisson pmf is computed analytically: x*log(lam) - lam - log(x!).
        log_factorial_h = np.array([math.lgamma(v + 1.0) for v in x], dtype=float)
        log_factorial_a = np.array([math.lgamma(v + 1.0) for v in y], dtype=float)

        # Parameter packing:
        #   theta = [attack_free (n_teams), defence_free (n_teams), mu, gamma, rho]
        # attack/defence are re-centred (mean-zero) inside the objective so the
        # optimiser sees an unconstrained problem.
        n_params = 2 * n_teams + 3
        idx_mu = 2 * n_teams
        idx_gamma = 2 * n_teams + 1
        idx_rho = 2 * n_teams + 2

        def unpack(theta: np.ndarray):
            atk = theta[:n_teams]
            atk = atk - atk.mean()
            dfc = theta[n_teams:2 * n_teams]
            dfc = dfc - dfc.mean()
            mu = theta[idx_mu]
            gamma = theta[idx_gamma]
            rho = theta[idx_rho]
            return atk, dfc, mu, gamma, rho

        def neg_log_lik(theta: np.ndarray) -> float:
            atk, dfc, mu, gamma, rho = unpack(theta)
            log_lh = mu + atk[hi] - dfc[ai] + gamma * not_neutral
            log_la = mu + atk[ai] - dfc[hi]
            # Clip to keep exp finite for pathological iterates.
            log_lh = np.clip(log_lh, -30.0, 30.0)
            log_la = np.clip(log_la, -30.0, 30.0)
            lam_h = np.exp(log_lh)
            lam_a = np.exp(log_la)

            # Independent-Poisson log-likelihood (analytic pmf).
            ll = (
                x * log_lh - lam_h - log_factorial_h
                + y * log_la - lam_a - log_factorial_a
            )

            # tau correction (additive in log space).
            tau = np.ones(n, dtype=float)
            tau[m00] = 1.0 - lam_h[m00] * lam_a[m00] * rho
            tau[m01] = 1.0 + lam_h[m01] * rho
            tau[m10] = 1.0 + lam_a[m10] * rho
            tau[m11] = 1.0 - rho
            # tau can go non-positive for extreme rho; clip then add log.
            tau = np.clip(tau, _EPS, None)
            ll = ll + np.log(tau)

            wll = float(np.sum(weights * ll))

            atk_dev = atk - atk_prior_vec
            dfc_dev = dfc - dfc_prior_vec
            penalty = float(
                np.sum(ridge * atk_dev * atk_dev) + np.sum(ridge * dfc_dev * dfc_dev)
            )
            return -wll + penalty

        # Initialisation: log mean goals for mu, small home advantage, rho 0.
        mean_goals = float(np.clip((hg.mean() + ag.mean()) / 2.0, 0.1, None))
        theta0 = np.zeros(n_params, dtype=float)
        theta0[idx_mu] = math.log(mean_goals)
        theta0[idx_gamma] = 0.1
        theta0[idx_rho] = 0.0

        # Bounds: rho is constrained to a region where tau stays positive for
        # the typical low-score range; Dixon-Coles report rho in roughly
        # (-0.2, 0.2). attack/defence/mu/gamma are unbounded.
        bounds: List[Tuple[Optional[float], Optional[float]]] = [
            (None, None)
        ] * (2 * n_teams)
        bounds.append((None, None))  # mu
        bounds.append((None, None))  # gamma
        bounds.append((-1.0, 1.0))   # rho

        res = minimize(
            neg_log_lik,
            theta0,
            method="L-BFGS-B",
            bounds=bounds,
            options={"maxiter": maxiter, "maxfun": 100000, "ftol": 1e-10, "gtol": 1e-7},
        )

        atk, dfc, mu, gamma, rho = unpack(res.x)
        self.attack = {t: float(atk[i]) for i, t in enumerate(teams)}
        self.defence = {t: float(dfc[i]) for i, t in enumerate(teams)}
        self.mu = float(mu)
        self.home_advantage = float(gamma)
        self.rho = float(rho)
        self.fitted = True
        self._opt_result = res  # type: ignore[attr-defined]
        return self

    def fit_dataframe(self, df: "pd.DataFrame", reference_date=None, maxiter: int = 500) -> "DixonColesModel":
        """Fit from a results ``DataFrame``.

        Required columns: ``home_team``, ``away_team``, ``home_score``,
        ``away_score``. Optional: ``date`` (used with ``reference_date`` to
        compute ``days_ago``) and ``neutral``.
        """
        if pd is None:  # pragma: no cover
            raise RuntimeError("pandas is required for fit_dataframe")
        required = {"home_team", "away_team", "home_score", "away_score"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError("DataFrame missing required columns: %s" % sorted(missing))

        days_ago = None
        if "date" in df.columns:
            dates = pd.to_datetime(df["date"])
            ref = pd.to_datetime(reference_date) if reference_date is not None else dates.max()
            days_ago = (ref - dates).dt.total_seconds().to_numpy() / 86400.0
        neutral = df["neutral"].to_numpy() if "neutral" in df.columns else None

        return self.fit(
            df["home_team"].tolist(),
            df["away_team"].tolist(),
            df["home_score"].to_numpy(),
            df["away_score"].to_numpy(),
            days_ago=days_ago,
            neutral=neutral,
            maxiter=maxiter,
        )

    # -- prediction ---------------------------------------------------------

    def expected_lambdas(
        self,
        home: str,
        away: str,
        neutral: bool = False,
        warn: bool = True,
    ) -> Tuple[float, float]:
        """Return the ``(lambda_home, lambda_away)`` Poisson means for a fixture."""
        atk_h = self._attack_of(home, warn=warn)
        atk_a = self._attack_of(away, warn=warn)
        dfc_h = self._defence_of(home, warn=False)
        dfc_a = self._defence_of(away, warn=False)
        gamma = self.home_advantage if not neutral else 0.0
        log_lh = self.mu + atk_h - dfc_a + gamma
        log_la = self.mu + atk_a - dfc_h
        b = self.goal_supply_boost
        return math.exp(log_lh) * b, math.exp(log_la) * b

    def score_matrix(
        self,
        home: str,
        away: str,
        neutral: bool = False,
        max_goals: Optional[int] = None,
        warn: bool = True,
    ) -> Tuple[np.ndarray, float, float]:
        """Build the normalised score-probability matrix for a fixture.

        Returns ``(matrix, lambda_home, lambda_away)``. ``matrix[x, y]`` is the
        probability of the scoreline ``(home=x, away=y)`` after the tau
        correction, renormalised so the (truncated) matrix sums to one.
        """
        mg = self.max_goals if max_goals is None else int(max_goals)
        lam_h, lam_a = self.expected_lambdas(home, away, neutral=neutral, warn=warn)

        goals = np.arange(mg + 1)
        ph = poisson.pmf(goals, lam_h)
        pa = poisson.pmf(goals, lam_a)
        mat = np.outer(ph, pa)  # mat[x, y]

        # Apply tau correction on the four low-score cells.
        xx = goals[:, None]
        yy = goals[None, :]
        tau = dc_tau(
            np.broadcast_to(xx, mat.shape),
            np.broadcast_to(yy, mat.shape),
            np.full(mat.shape, lam_h),
            np.full(mat.shape, lam_a),
            self.rho,
        )
        mat = mat * tau
        mat = np.clip(mat, 0.0, None)
        total = mat.sum()
        if total > 0:
            mat = mat / total
        return mat, lam_h, lam_a

    def predict(
        self,
        home: str,
        away: str,
        neutral: bool = False,
        max_goals: Optional[int] = None,
        warn: bool = True,
    ) -> ScorelinePrediction:
        """Predict a fixture, returning a :class:`ScorelinePrediction`.

        Unseen teams fall back to the baseline (mean-zero) prior with a
        ``RuntimeWarning`` when ``warn`` is ``True``.
        """
        mat, lam_h, lam_a = self.score_matrix(
            home, away, neutral=neutral, max_goals=max_goals, warn=warn
        )
        return ScorelinePrediction(mat, home, away, lam_h, lam_a)

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise configuration and fitted parameters to a plain dict."""
        return {
            "xi": self.xi,
            "reg_lambda": self.reg_lambda,
            "min_matches": self.min_matches,
            "low_data_reg_multiplier": self.low_data_reg_multiplier,
            "max_goals": self.max_goals,
            "teams": list(self.teams),
            "attack": dict(self.attack),
            "defence": dict(self.defence),
            "mu": self.mu,
            "home_advantage": self.home_advantage,
            "rho": self.rho,
            "goal_supply_boost": self.goal_supply_boost,
            "match_counts": dict(self.match_counts),
            "attack_prior": dict(self.attack_prior),
            "defence_prior": dict(self.defence_prior),
            "attack_prior_centered": dict(self._attack_prior_c),
            "defence_prior_centered": dict(self._defence_prior_c),
            "fitted": self.fitted,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DixonColesModel":
        """Reconstruct a model from :meth:`to_dict` output."""
        obj = cls(
            xi=data.get("xi", DEFAULT_XI),
            reg_lambda=data.get("reg_lambda", 0.01),
            min_matches=data.get("min_matches", 5),
            low_data_reg_multiplier=data.get("low_data_reg_multiplier", 5.0),
            max_goals=data.get("max_goals", 10),
            attack_prior=data.get("attack_prior") or None,
            defence_prior=data.get("defence_prior") or None,
            goal_supply_boost=float(data.get("goal_supply_boost", 1.0)),
        )
        obj.teams = list(data.get("teams", []))
        obj._team_index = {t: i for i, t in enumerate(obj.teams)}
        obj.attack = {str(k): float(v) for k, v in data.get("attack", {}).items()}
        obj.defence = {str(k): float(v) for k, v in data.get("defence", {}).items()}
        obj._attack_prior_c = {
            str(k): float(v) for k, v in data.get("attack_prior_centered", {}).items()
        }
        obj._defence_prior_c = {
            str(k): float(v) for k, v in data.get("defence_prior_centered", {}).items()
        }
        obj.mu = float(data.get("mu", 0.0))
        obj.home_advantage = float(data.get("home_advantage", 0.0))
        obj.rho = float(data.get("rho", 0.0))
        obj.match_counts = {str(k): int(v) for k, v in data.get("match_counts", {}).items()}
        obj.fitted = bool(data.get("fitted", False))
        return obj

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "DixonColesModel":
        return cls.from_dict(json.loads(s))
