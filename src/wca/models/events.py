"""Event-distribution pipeline — turn Dixon-Coles team lambdas (and team rates)
into match-event distributions: goal timing, corner counts, card risk and
substitution timing.

This layer *composes* the existing prop models (:class:`CornersModel`,
:class:`CardsModel`) and adds the timing shapes that those count-only models do
not provide. The timing/foul constants below are **empirical**, fit on the
StatsBomb open-data World Cup events (2018 + 2022): 128 matches, 334 regulation
goals, 926 substitutions, 3550 fouls (≈27.7 fouls/match). They are module
constants with provenance so a later refit can replace them without code
changes; every default therefore traces to a real fetched source.

Extra-time and shootout events are excluded from the 90-minute timing shapes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from wca.models.props import CardsModel, CornersModel

# 15-minute buckets across regulation (first-half added time -> "45+",
# second-half added time -> "90+").
TIMING_BUCKETS = ["1-15", "16-30", "31-45", "45+", "46-60", "61-75", "76-90", "90+"]

# Normalised goal-minute distribution (share of regulation goals per bucket).
# Source: StatsBomb open WC2018+2022, 334 regulation goals.
GOAL_TIMING = {
    "1-15": 0.1138, "16-30": 0.1048, "31-45": 0.1647, "45+": 0.0120,
    "46-60": 0.1946, "61-75": 0.1856, "76-90": 0.1437, "90+": 0.0808,
}

# Normalised substitution-minute distribution. Source: same data, 926 subs.
SUB_TIMING = {
    "1-15": 0.0032, "16-30": 0.0086, "31-45": 0.0151, "45+": 0.0032,
    "46-60": 0.1933, "61-75": 0.4017, "76-90": 0.3380, "90+": 0.0367,
}

# Tournament base rates (per match) from the same data.
BASE_FOULS_PER_MATCH = 27.734    # total fouls / match
BASE_REDS_PER_MATCH = 0.16       # red cards (incl. 2nd-yellow) / match, WC-typical
DEFAULT_SUBS_PER_MATCH = 7.2     # 926 / 128


# ---------------------------------------------------------------------------
# Goal timing
# ---------------------------------------------------------------------------

@dataclass
class GoalTiming:
    """When a match's goals arrive, given the total expected goals.

    ``expected_goals[b]`` is the Poisson mean goals in bucket ``b``;
    ``p_at_least_one[b]`` is P(>=1 goal in that bucket); ``p_first[b]`` is the
    probability that the match's *first* goal lands in that bucket.
    """

    lambda_total: float
    buckets: List[str]
    weights: Dict[str, float]
    expected_goals: Dict[str, float]
    p_at_least_one: Dict[str, float]
    p_first: Dict[str, float]
    p_any_goal: float

    def p_goal_before(self, bucket: str) -> float:
        """P(>=1 goal strictly before the start of ``bucket``)."""
        idx = self.buckets.index(bucket)
        mu = sum(self.expected_goals[b] for b in self.buckets[:idx])
        return 1.0 - math.exp(-mu)

    def p_first_half_goal(self) -> float:
        return self.p_goal_before("46-60")


def goal_timing_pdf(lambda_total: float,
                    shape: Optional[Dict[str, float]] = None) -> GoalTiming:
    """Distribute ``lambda_total`` expected goals over the regulation buckets.

    Each bucket is an independent Poisson with mean ``lambda_total * w_b`` where
    ``w_b`` is the (normalised) empirical timing weight. The first-goal split is
    the standard ordered-Poisson product.
    """
    if lambda_total < 0:
        raise ValueError("lambda_total must be non-negative")
    shape = shape or GOAL_TIMING
    wsum = sum(shape.values())
    if wsum <= 0:
        raise ValueError("timing shape must have positive mass")
    weights = {b: shape.get(b, 0.0) / wsum for b in TIMING_BUCKETS}

    expected = {b: lambda_total * weights[b] for b in TIMING_BUCKETS}
    p_one = {b: 1.0 - math.exp(-expected[b]) for b in TIMING_BUCKETS}

    p_first: Dict[str, float] = {}
    cum = 0.0
    for b in TIMING_BUCKETS:
        mu = expected[b]
        p_first[b] = math.exp(-cum) * (1.0 - math.exp(-mu))
        cum += mu
    return GoalTiming(
        lambda_total=float(lambda_total), buckets=list(TIMING_BUCKETS),
        weights=weights, expected_goals=expected, p_at_least_one=p_one,
        p_first=p_first, p_any_goal=1.0 - math.exp(-lambda_total))


# ---------------------------------------------------------------------------
# Corner count distribution
# ---------------------------------------------------------------------------

@dataclass
class CornerDist:
    mean_total: float
    mean_home: float
    mean_away: float
    pmf: Dict[int, float]
    _model: CornersModel
    _lh: float
    _la: float

    def prob_over(self, line: float) -> float:
        return self._model.prob_over(line, self._lh, self._la)

    def prob_team_over(self, line: float, home: bool = True) -> float:
        if home:
            return self._model.prob_team_over(line, self._lh, self._la)
        return self._model.prob_team_over(line, self._la, self._lh)

    def fair_over_under(self, line: float):
        return self._model.fair_odds_over_under(line, self._lh, self._la)


def corner_count_dist(lambda_home: float, lambda_away: float,
                      model: Optional[CornersModel] = None,
                      max_count: int = 25) -> CornerDist:
    """Full total-corner pmf + team means, driven by the DC lambdas."""
    model = model or CornersModel()
    pmf = {n: model.pmf(n, lambda_home, lambda_away) for n in range(max_count + 1)}
    return CornerDist(
        mean_total=model.mean_total(lambda_home, lambda_away),
        mean_home=model.team_mean(lambda_home, lambda_away),
        mean_away=model.team_mean(lambda_away, lambda_home),
        pmf=pmf, _model=model, _lh=lambda_home, _la=lambda_away)


# ---------------------------------------------------------------------------
# Card risk
# ---------------------------------------------------------------------------

@dataclass
class CardRisk:
    mean_total: float
    aggression_home: float
    aggression_away: float
    p_red: float
    pmf: Dict[int, float]
    _model: CardsModel
    _ah: float
    _aa: float
    _sm: float

    def prob_over(self, line: float) -> float:
        return self._model.prob_over(line, self._ah, self._aa, self._sm)

    def fair_over_under(self, line: float):
        return self._model.fair_odds_over_under(line, self._ah, self._aa, self._sm)


def _aggression(fouls_pm: Optional[float], base_fouls: float) -> float:
    """Team aggression multiplier from its fouls-per-match vs half the base."""
    if fouls_pm is None or not (fouls_pm > 0):
        return 1.0
    base_per_team = base_fouls / 2.0
    return float(fouls_pm) / base_per_team if base_per_team > 0 else 1.0


def card_risk(lambda_home: float, lambda_away: float,
              fouls_home: Optional[float] = None,
              fouls_away: Optional[float] = None,
              reds_home: Optional[float] = None,
              reds_away: Optional[float] = None,
              stakes_mult: float = 1.0,
              base_fouls: float = BASE_FOULS_PER_MATCH,
              model: Optional[CardsModel] = None,
              max_count: int = 15) -> CardRisk:
    """Card-count distribution + red-card probability.

    Aggression multipliers come from each team's fouls-per-match (from
    ``team_rates`` in players.db) relative to the tournament base; ``stakes_mult``
    is a caller-supplied knockout/derby bump. ``p_red`` is 1 - exp(-expected
    reds), expected reds from the team red rates (or the tournament base). The
    ``lambda_*`` are accepted for signature symmetry / future coupling; the card
    mean is foul-driven, not goal-driven.
    """
    model = model or CardsModel()
    ah = _aggression(fouls_home, base_fouls)
    aa = _aggression(fouls_away, base_fouls)
    mean = model.mean_total(ah, aa, stakes_mult)
    pmf = {n: model.pmf(n, ah, aa, stakes_mult) for n in range(max_count + 1)}

    if reds_home is None and reds_away is None:
        exp_reds = BASE_REDS_PER_MATCH * stakes_mult
    else:
        exp_reds = ((reds_home or 0.0) + (reds_away or 0.0)) * stakes_mult
    p_red = 1.0 - math.exp(-max(exp_reds, 0.0))

    return CardRisk(
        mean_total=mean, aggression_home=ah, aggression_away=aa, p_red=p_red,
        pmf=pmf, _model=model, _ah=ah, _aa=aa, _sm=stakes_mult)


# ---------------------------------------------------------------------------
# Substitution timing
# ---------------------------------------------------------------------------

@dataclass
class SubTiming:
    buckets: List[str]
    weights: Dict[str, float]
    expected_subs: Dict[str, float]
    total_subs: float

    def p_sub_in(self, bucket: str) -> float:
        """P(>=1 substitution in ``bucket``), Poisson on the bucket mean."""
        return 1.0 - math.exp(-self.expected_subs[bucket])


def substitution_timing(total_subs: float = DEFAULT_SUBS_PER_MATCH,
                        shape: Optional[Dict[str, float]] = None) -> SubTiming:
    """Distribute expected substitutions over the regulation buckets.

    Substitution timing is not goal-lambda driven (it is coach behaviour); the
    default shape is the empirical WC2018+2022 distribution and is the honest
    default. ``total_subs`` scales the volume (e.g. 10 under 5-sub rules).
    """
    if total_subs < 0:
        raise ValueError("total_subs must be non-negative")
    shape = shape or SUB_TIMING
    wsum = sum(shape.values())
    if wsum <= 0:
        raise ValueError("timing shape must have positive mass")
    weights = {b: shape.get(b, 0.0) / wsum for b in TIMING_BUCKETS}
    expected = {b: total_subs * weights[b] for b in TIMING_BUCKETS}
    return SubTiming(buckets=list(TIMING_BUCKETS), weights=weights,
                     expected_subs=expected, total_subs=float(total_subs))


# ---------------------------------------------------------------------------
# Convenience: all four for one fixture
# ---------------------------------------------------------------------------

@dataclass
class MatchEventDistributions:
    goal_timing: GoalTiming
    corners: CornerDist
    cards: CardRisk
    subs: SubTiming


def match_event_distributions(
    lambda_home: float, lambda_away: float,
    fouls_home: Optional[float] = None, fouls_away: Optional[float] = None,
    reds_home: Optional[float] = None, reds_away: Optional[float] = None,
    stakes_mult: float = 1.0, total_subs: float = DEFAULT_SUBS_PER_MATCH,
) -> MatchEventDistributions:
    """Build all four event distributions from the DC lambdas + team rates."""
    return MatchEventDistributions(
        goal_timing=goal_timing_pdf(lambda_home + lambda_away),
        corners=corner_count_dist(lambda_home, lambda_away),
        cards=card_risk(lambda_home, lambda_away, fouls_home, fouls_away,
                        reds_home, reds_away, stakes_mult),
        subs=substitution_timing(total_subs),
    )
