"""World Football Elo Ratings for international football.

This module implements an Elo rating system following the conventions of the
World Football Elo Ratings (https://www.eloratings.net/about), together with an
ordered-logistic outcome model that converts an Elo rating difference into
(home win, draw, away win) probabilities.

Key formulae (eloratings.net):

* Expected score of the home side::

      E = 1 / (1 + 10 ** (-dr / 400))

  where ``dr`` is the home rating minus away rating, *including* any home /
  host advantage expressed in rating points.

* Rating update for one side::

      R' = R + K * G * (W - E)

  with ``W`` the actual result (1 win / 0.5 draw / 0 loss), ``G`` the
  goal-margin multiplier and ``K`` the importance-of-match weight.

* Goal-margin multiplier ``G`` (eloratings.net):

  - goal difference of 0 or 1   -> 1.0
  - goal difference of 2        -> 1.5
  - goal difference of 3        -> 1.75
  - goal difference of N >= 4   -> 1.75 + (N - 3) / 8

The K-factor (importance) defaults mirror the eloratings.net importance ladder,
mapped onto the kinds of competition that appear in the martj42
``results.csv`` ``tournament`` column
(https://github.com/martj42/international_results).

The ordered-logistic outcome model is the standard proportional-odds /
cumulative-logit model (McCullagh, 1980, "Regression Models for Ordinal Data",
JRSS-B 42(2):109-142). With a single covariate ``x`` (the Elo difference scaled
by 1/400) and two ordered cut points ``c_lo <= c_hi`` it defines::

      P(away win)        = sigma(c_lo - beta * x)
      P(draw or better)  = sigma(c_hi - beta * x)
      P(home win)        = 1 - sigma(c_hi - beta * x)

where ``sigma`` is the logistic CDF. ``beta`` controls how sharply the Elo
difference separates outcomes; the two cut points control the baseline draw
band. Parameters are fit by maximum likelihood with ``scipy.optimize``.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

try:  # pandas is a hard dependency of the package; import defensively for typing
    import pandas as pd
except Exception:  # pragma: no cover - pandas is always present at runtime
    pd = None  # type: ignore


# ---------------------------------------------------------------------------
# Match-importance (K-factor) defaults and tournament-name mapping.
# ---------------------------------------------------------------------------

#: Default K-factor per match-importance class.
DEFAULT_K_FACTORS: Dict[str, float] = {
    "friendly": 20.0,
    "nations_league": 30.0,
    "qualifier": 40.0,
    "continental": 50.0,
    "world_cup": 60.0,
}

#: Fallback importance class when a tournament name is unrecognised.
DEFAULT_IMPORTANCE = "friendly"


def classify_tournament(tournament: str) -> str:
    """Map a martj42 ``tournament`` string to an importance class.

    The classification is keyword based and case-insensitive so that it is
    robust to the many variants present in the martj42 dataset
    (https://github.com/martj42/international_results), e.g. "FIFA World Cup",
    "FIFA World Cup qualification", "UEFA Euro qualification", "Copa America",
    "African Cup of Nations", "UEFA Nations League", "Friendly".

    Parameters
    ----------
    tournament:
        The raw tournament name.

    Returns
    -------
    str
        One of the keys of :data:`DEFAULT_K_FACTORS`.
    """
    if tournament is None:
        return DEFAULT_IMPORTANCE
    name = str(tournament).strip().lower()
    if not name:
        return DEFAULT_IMPORTANCE

    # Qualification matches must be detected before the parent competition,
    # since "FIFA World Cup qualification" contains "world cup".
    if "qualif" in name:
        return "qualifier"

    if "nations league" in name:
        return "nations_league"

    if "friendly" in name:
        return "friendly"

    # Premier global tournament.
    if "world cup" in name and "qualif" not in name:
        return "world_cup"

    # Continental finals tournaments. These are the eloratings.net
    # "continental championship" tier.
    continental_keywords = (
        "euro",  # UEFA Euro
        "copa am",  # Copa America / Copa América
        "copa amèrica",
        "african cup",  # African Cup of Nations
        "africa cup",
        "afcon",
        "asian cup",  # AFC Asian Cup
        "gold cup",  # CONCACAF Gold Cup
        "oceania nations",  # OFC Nations Cup
        "ofc nations",
        "confederations cup",
        "nations cup",
        "championship",
    )
    for kw in continental_keywords:
        if kw in name:
            return "continental"

    return DEFAULT_IMPORTANCE


def goal_margin_multiplier(goal_difference: int) -> float:
    """Return the World Football Elo goal-margin multiplier ``G``.

    Following eloratings.net:

    * ``|gd|`` in {0, 1} -> 1.0
    * ``|gd|`` == 2      -> 1.5
    * ``|gd|`` == 3      -> 1.75
    * ``|gd|`` >= 4      -> 1.75 + (|gd| - 3) / 8

    Parameters
    ----------
    goal_difference:
        Absolute or signed goal difference; only the magnitude is used.
    """
    n = abs(int(goal_difference))
    if n <= 1:
        return 1.0
    if n == 2:
        return 1.5
    if n == 3:
        return 1.75
    return 1.75 + (n - 3) / 8.0


def expected_score(rating_diff: float) -> float:
    """Logistic expected score for the higher-rated side.

    ``E = 1 / (1 + 10 ** (-rating_diff / 400))`` where ``rating_diff`` is the
    home rating minus the away rating, including any home advantage.
    """
    return 1.0 / (1.0 + 10.0 ** (-rating_diff / 400.0))


class EloRater:
    """World Football Elo rating engine for international football.

    Parameters
    ----------
    initial_rating:
        Rating assigned to a team the first time it is seen. Default 1500.
    initial_ratings:
        Optional per-team initial ratings. Teams absent from the mapping still
        start at ``initial_rating``. This lets callers seed Elo from an external
        prior while preserving the flat default for standalone use.
    home_advantage:
        Rating points added to the home team's effective rating when the match
        is *not* played at a neutral venue. Default 100 (eloratings.net uses
        roughly 100 points of home-field advantage).
    k_factors:
        Mapping of importance class -> K-factor. Defaults to
        :data:`DEFAULT_K_FACTORS`.
    host_advantage:
        Extra rating points granted to a tournament host playing at home on a
        venue that is flagged neutral for the opponent. When ``True`` is passed
        to :meth:`rate_match` / a ``host`` column is present, the host receives
        ``home_advantage`` even on a neutral venue. The numeric size of the host
        bonus equals ``home_advantage`` by construction.
    """

    def __init__(
        self,
        initial_rating: float = 1500.0,
        initial_ratings: Optional[Mapping[str, float]] = None,
        home_advantage: float = 100.0,
        k_factors: Optional[Dict[str, float]] = None,
        host_advantage: bool = True,
    ) -> None:
        self.initial_rating = float(initial_rating)
        self.initial_ratings: Dict[str, float] = (
            {str(k): float(v) for k, v in initial_ratings.items()}
            if initial_ratings is not None
            else {}
        )
        self.home_advantage = float(home_advantage)
        self.k_factors: Dict[str, float] = dict(
            k_factors if k_factors is not None else DEFAULT_K_FACTORS
        )
        self.host_advantage = bool(host_advantage)
        self.ratings: Dict[str, float] = {}

    # -- rating access ------------------------------------------------------

    def get_rating(self, team: str) -> float:
        """Current rating of ``team`` (creating it at ``initial_rating``)."""
        return self.ratings.get(
            team,
            self.initial_ratings.get(team, self.initial_rating),
        )

    def k_for(self, tournament: str) -> float:
        """K-factor for a tournament name via :func:`classify_tournament`."""
        importance = classify_tournament(tournament)
        return float(self.k_factors.get(importance, self.k_factors.get(DEFAULT_IMPORTANCE, 20.0)))

    # -- core update --------------------------------------------------------

    def expected_home(
        self,
        home_team: str,
        away_team: str,
        neutral: bool = False,
        host: Optional[str] = None,
        host_points: Optional[float] = None,
    ) -> float:
        """Expected score of the home side given the current ratings.

        ``host`` may name a team that receives the home/host advantage even on
        a neutral venue (used for tournament hosts). ``host_points`` optionally
        overrides the magnitude of that host bonus (see :meth:`_rating_diff`).
        """
        diff = self._rating_diff(
            home_team, away_team, neutral=neutral, host=host, host_points=host_points
        )
        return expected_score(diff)

    def _rating_diff(
        self,
        home_team: str,
        away_team: str,
        neutral: bool,
        host: Optional[str],
        host_points: Optional[float] = None,
    ) -> float:
        """Home-minus-away effective rating difference, including advantages.

        ``host_points`` optionally overrides the host-bonus magnitude on a
        neutral venue. When ``None`` (default) the bonus is the full
        ``home_advantage``, reproducing the legacy behaviour; the venue-aware
        path (see :mod:`wca.models.venues`) passes a diluted, altitude-adjusted
        value instead.
        """
        rh = self.get_rating(home_team)
        ra = self.get_rating(away_team)
        adv = 0.0
        if not neutral:
            adv = self.home_advantage
        elif self.host_advantage and host is not None:
            # Neutral venue but a tournament host is playing: grant the host
            # the advantage in whichever direction it applies.
            mag = self.home_advantage if host_points is None else float(host_points)
            if host == home_team:
                adv = mag
            elif host == away_team:
                adv = -mag
        return (rh + adv) - ra

    def rate_match(
        self,
        home_team: str,
        away_team: str,
        home_score: int,
        away_score: int,
        tournament: str = "Friendly",
        neutral: bool = False,
        host: Optional[str] = None,
    ) -> Tuple[float, float]:
        """Apply a single match result and update both ratings in place.

        Returns the ``(new_home_rating, new_away_rating)`` pair. The amount the
        winner gains exactly equals the amount the loser drops because both
        sides share the same ``K * G`` and ``W_home - E_home == -(W_away -
        E_away)``.
        """
        rh = self.get_rating(home_team)
        ra = self.get_rating(away_team)

        diff = self._rating_diff(home_team, away_team, neutral=neutral, host=host)
        e_home = expected_score(diff)

        if home_score > away_score:
            w_home = 1.0
        elif home_score < away_score:
            w_home = 0.0
        else:
            w_home = 0.5

        k = self.k_for(tournament)
        g = goal_margin_multiplier(home_score - away_score)
        delta = k * g * (w_home - e_home)

        new_home = rh + delta
        new_away = ra - delta
        self.ratings[home_team] = new_home
        self.ratings[away_team] = new_away
        return new_home, new_away

    # -- batch --------------------------------------------------------------

    def rate_matches(
        self,
        df: "pd.DataFrame",
        return_history: bool = True,
    ) -> Dict[str, Any]:
        """Chronologically process a results ``DataFrame``.

        The frame must contain the columns ``date``, ``home_team``,
        ``away_team``, ``home_score``, ``away_score``, ``tournament`` and
        ``neutral``. An optional ``host`` column (team name) drives the
        host advantage on neutral venues.

        Parameters
        ----------
        df:
            Match results.
        return_history:
            If ``True`` (default) the returned dict carries a ``history`` list
            with one record per match describing the ratings before/after.

        Returns
        -------
        dict
            ``{"final_ratings": {team: rating}, "history": [...]}``.
        """
        if pd is None:  # pragma: no cover
            raise RuntimeError("pandas is required for rate_matches")

        required = {
            "date",
            "home_team",
            "away_team",
            "home_score",
            "away_score",
            "tournament",
            "neutral",
        }
        missing = required - set(df.columns)
        if missing:
            raise ValueError("DataFrame missing required columns: %s" % sorted(missing))

        ordered = df.sort_values("date", kind="mergesort").reset_index(drop=True)
        has_host = "host" in ordered.columns

        history: List[Dict[str, Any]] = []
        for row in ordered.itertuples(index=False):
            home_team = getattr(row, "home_team")
            away_team = getattr(row, "away_team")
            home_score = int(getattr(row, "home_score"))
            away_score = int(getattr(row, "away_score"))
            tournament = getattr(row, "tournament")
            neutral = bool(getattr(row, "neutral"))
            host = getattr(row, "host") if has_host else None
            if host is not None and (isinstance(host, float) and math.isnan(host)):
                host = None

            pre_home = self.get_rating(home_team)
            pre_away = self.get_rating(away_team)
            new_home, new_away = self.rate_match(
                home_team,
                away_team,
                home_score,
                away_score,
                tournament=tournament,
                neutral=neutral,
                host=host,
            )
            if return_history:
                history.append(
                    {
                        "date": getattr(row, "date"),
                        "home_team": home_team,
                        "away_team": away_team,
                        "home_rating_pre": pre_home,
                        "away_rating_pre": pre_away,
                        "home_rating_post": new_home,
                        "away_rating_post": new_away,
                        "tournament": tournament,
                        "neutral": neutral,
                    }
                )

        return {
            "final_ratings": dict(self.ratings),
            "history": history if return_history else [],
        }

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the rater (config + current ratings) to a plain dict."""
        return {
            "initial_rating": self.initial_rating,
            "initial_ratings": dict(self.initial_ratings),
            "home_advantage": self.home_advantage,
            "k_factors": dict(self.k_factors),
            "host_advantage": self.host_advantage,
            "ratings": dict(self.ratings),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EloRater":
        """Reconstruct an :class:`EloRater` from :meth:`to_dict` output."""
        obj = cls(
            initial_rating=data.get("initial_rating", 1500.0),
            initial_ratings=data.get("initial_ratings"),
            home_advantage=data.get("home_advantage", 100.0),
            k_factors=data.get("k_factors"),
            host_advantage=data.get("host_advantage", True),
        )
        obj.ratings = {str(k): float(v) for k, v in data.get("ratings", {}).items()}
        return obj

    def to_json(self) -> str:
        """JSON string of :meth:`to_dict`."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "EloRater":
        return cls.from_dict(json.loads(s))


# ---------------------------------------------------------------------------
# Ordered-logistic outcome model.
# ---------------------------------------------------------------------------

# Outcome encoding (ordered from the away side's perspective so that larger
# values mean a better home result):
#   0 -> away win
#   1 -> draw
#   2 -> home win
AWAY_WIN = 0
DRAW = 1
HOME_WIN = 2

_EPS = 1e-12
_MIN_BETA = 0.05


def _sigmoid(z: np.ndarray) -> np.ndarray:
    """Numerically stable logistic CDF."""
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    neg = ~pos
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[neg])
    out[neg] = ez / (1.0 + ez)
    return out


class EloOutcomeModel:
    """Ordered-logistic model mapping Elo difference -> outcome probabilities.

    The model uses the proportional-odds parameterisation (McCullagh, 1980).
    For a scaled covariate ``x = diff / scale`` and two ordered cut points
    ``c_lo <= c_hi``::

        eta_lo = c_lo - beta * x
        eta_hi = c_hi - beta * x
        P(away win) = sigma(eta_lo)
        P(draw)     = sigma(eta_hi) - sigma(eta_lo)
        P(home win) = 1 - sigma(eta_hi)

    where ``sigma`` is the logistic CDF. Increasing ``diff`` (home stronger)
    monotonically increases ``P(home win)`` and decreases ``P(away win)``.

    Parameters
    ----------
    scale:
        Divisor applied to the Elo difference before entering the linear
        predictor. The default of 400 keeps ``beta`` on the same order as the
        natural-log base used by the underlying logistic, mirroring the Elo
        400-point convention.
    """

    def __init__(self, scale: float = 400.0) -> None:
        self.scale = float(scale)
        # Sensible defaults so predict_proba works before fitting.
        self.beta: float = 1.0
        self.c_lo: float = -0.5
        self.c_hi: float = 0.5
        self.fitted: bool = False

    # -- internal probability computation -----------------------------------

    def _probs(
        self,
        x: np.ndarray,
        beta: float,
        c_lo: float,
        c_hi: float,
    ) -> np.ndarray:
        """Return an (n, 3) array of [p_away, p_draw, p_home]."""
        eta_lo = c_lo - beta * x
        eta_hi = c_hi - beta * x
        s_lo = _sigmoid(eta_lo)
        s_hi = _sigmoid(eta_hi)
        p_away = s_lo
        p_draw = s_hi - s_lo
        p_home = 1.0 - s_hi
        probs = np.stack([p_away, p_draw, p_home], axis=1)
        # Guard against tiny negative draw probabilities from rounding.
        probs = np.clip(probs, _EPS, 1.0)
        probs = probs / probs.sum(axis=1, keepdims=True)
        return probs

    # -- fitting ------------------------------------------------------------

    def fit(
        self,
        diffs: Sequence[float],
        outcomes: Sequence[int],
        maxiter: int = 1000,
    ) -> "EloOutcomeModel":
        """Maximum-likelihood fit of ``beta`` and the two cut points.

        Parameters
        ----------
        diffs:
            Elo rating differences (home minus away, including home advantage).
        outcomes:
            Ordinal outcomes encoded as 0 = away win, 1 = draw, 2 = home win.
        maxiter:
            Maximum optimiser iterations.
        """
        from scipy.optimize import minimize

        x = np.asarray(diffs, dtype=float) / self.scale
        y = np.asarray(outcomes, dtype=int)
        if x.shape[0] != y.shape[0]:
            raise ValueError("diffs and outcomes must have the same length")
        if x.shape[0] == 0:
            raise ValueError("cannot fit on empty data")

        # Parameterise with (raw_beta, c_lo, gap). ``beta`` and ``gap`` are
        # constrained positive via softplus: Elo advantage must monotonically
        # increase the home-outcome probability, and the cut-point ordering
        # c_lo <= c_hi is guaranteed.
        def unpack(theta: np.ndarray) -> Tuple[float, float, float]:
            beta = (
                _MIN_BETA
                + math.log1p(math.exp(-abs(theta[0])))
                + max(theta[0], 0.0)
            )
            c_lo = theta[1]
            gap = math.log1p(math.exp(-abs(theta[2]))) + max(theta[2], 0.0)  # softplus
            c_hi = c_lo + gap
            return beta, c_lo, c_hi

        def nll(theta: np.ndarray) -> float:
            beta, c_lo, c_hi = unpack(theta)
            probs = self._probs(x, beta, c_lo, c_hi)
            chosen = probs[np.arange(x.shape[0]), y]
            return -float(np.sum(np.log(np.clip(chosen, _EPS, 1.0))))

        theta0 = np.array([1.0, -0.5, 0.0], dtype=float)
        res = minimize(nll, theta0, method="Nelder-Mead",
                       options={"maxiter": maxiter, "xatol": 1e-7, "fatol": 1e-9})
        # A second polish with BFGS sharpens the optimum.
        res = minimize(nll, res.x, method="BFGS", options={"maxiter": maxiter})

        beta, c_lo, c_hi = unpack(res.x)
        self.beta = float(beta)
        self.c_lo = float(c_lo)
        self.c_hi = float(c_hi)
        self.fitted = True
        return self

    # -- prediction ---------------------------------------------------------

    def predict_proba(self, diff: float) -> Tuple[float, float, float]:
        """Return ``(p_home_win, p_draw, p_away_win)`` for a single ``diff``.

        Note the output ordering is home/draw/away to match betting market 1X2
        conventions, whereas the internal encoding is away/draw/home.
        """
        x = np.asarray([float(diff)], dtype=float) / self.scale
        probs = self._probs(x, self.beta, self.c_lo, self.c_hi)[0]
        p_away, p_draw, p_home = float(probs[0]), float(probs[1]), float(probs[2])
        return p_home, p_draw, p_away

    def predict_proba_batch(self, diffs: Sequence[float]) -> np.ndarray:
        """Vectorised prediction: ``(n, 3)`` array of [p_home, p_draw, p_away]."""
        x = np.asarray(diffs, dtype=float) / self.scale
        probs = self._probs(x, self.beta, self.c_lo, self.c_hi)
        # Reorder away/draw/home -> home/draw/away.
        return probs[:, [2, 1, 0]]

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scale": self.scale,
            "beta": self.beta,
            "c_lo": self.c_lo,
            "c_hi": self.c_hi,
            "fitted": self.fitted,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EloOutcomeModel":
        obj = cls(scale=data.get("scale", 400.0))
        obj.beta = float(data.get("beta", 1.0))
        obj.c_lo = float(data.get("c_lo", -0.5))
        obj.c_hi = float(data.get("c_hi", 0.5))
        obj.fitted = bool(data.get("fitted", False))
        return obj

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "EloOutcomeModel":
        return cls.from_dict(json.loads(s))
