"""Bet-builder market models: team totals and player counts.

This module extends the existing prop engines (:mod:`wca.models.props`,
:mod:`wca.models.scorers`) to the classic *bet-builder* surface that
sportsbooks price as single-fixture combos:

* **Team totals** — goals, shots, shots on target (SoT), fouls, corners, cards.
* **Player counts** — shots on target, fouls committed, to-be-booked (yellow+).
* **Player to score** — delegated to :class:`wca.models.scorers.ScorerPricer`
  (already calibrated), re-exported here so the bet-builder payload is one stop.

Design rules (match the rest of the codebase)
---------------------------------------------
* Pure functions/classes over their inputs — no network, no implicit file IO
  except the optional :class:`RateStore` loader which degrades to bundled
  tournament priors when ``players.db`` is absent (it is not on ``main`` yet —
  it lands with the Phase-2 player branch).
* Counts use the same Negative-Binomial parameterisation as
  :mod:`wca.models.props` (mean ``mu``, dispersion ``k``; ``Var = mu + mu^2/k``;
  ``k -> inf`` is Poisson). Over/under lines are half-integers so
  ``P(over L) = 1 - CDF(floor(L))`` with no continuity correction.
* Fair odds are ``1/p`` (no margin). :func:`price_with_overround` adds a target
  book margin for display, and :func:`ev_vs_offer` computes EV against a real
  offered price **net of venue fee**.

Honesty caveats
---------------
The base rates and dispersions below are **tournament priors** (WC2018+2022
StatsBomb aggregates, see ``scripts/wca_props_data.py`` for the corners/cards
fit; shots/SoT/fouls priors are order-of-magnitude WC values pending a refit).
They are constructor/method arguments so the data pipeline can replace them
without touching this module. Player-count markets only become individually
sharp once ``players.db`` per-90 rates are available; until then they run off
injected rates or the override store.

Venue availability
------------------
SoT, cards/bookings, corners and fouls are **sportsbook-only** — they are not
offered on the Betfair Exchange, and the project has no sportsbook *odds* feed
beyond TheOddsAPI player-prop markets. So for most of these markets we publish
**model fair odds only** (``venues=("sportsbook",)``) and can price EV only
where an offered price is supplied. Team total goals and player-to-score have
exchange/Polymarket coverage in places; the rest are model-only by default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from scipy.stats import nbinom, poisson

from wca.models.props import CardsModel, CornersModel
from wca.models.scorers import PlayerParams, ScorerLine, ScorerPricer

# ---------------------------------------------------------------------------
# Tournament priors (per team, per match). Refit-pending; documented above.
# ---------------------------------------------------------------------------

# Mean team goal expectation used to scale attack-driven counts. WC total ~2.7
# goals/match => ~1.35/team.
BASE_TEAM_LAMBDA = 1.35

# (mean_per_team, NB dispersion k). Means are WC priors; dispersions are
# method-of-moments order-of-magnitude values to be refit from players.db.
TEAM_PRIORS: Dict[str, Tuple[float, float]] = {
    "shots": (12.0, 18.0),     # total shots per team
    "sot": (4.2, 9.0),         # shots on target per team
    "fouls": (11.5, 22.0),     # fouls committed per team
}

# Player per-90 priors when no player rate is available at all (a generic
# rotation outfielder). These are deliberately modest so the fallback never
# manufactures a confident edge.
PLAYER_P90_PRIORS: Dict[str, float] = {
    "sot": 0.7,
    "fouls": 1.2,
    "yellows": 0.18,
}
PLAYER_DISPERSION: Dict[str, float] = {"sot": 4.0, "fouls": 6.0}

# How strongly attack strength scales team shot/SoT means (corners use 0.30 in
# props.py; shots track xG more tightly so 0.6).
SHOT_ELASTICITY = 0.6

# Default venue fees (commission on net winnings). Mirrors card.py conventions.
DEFAULT_FEES: Dict[str, float] = {
    "betfair": 0.02,
    "betfair_ex": 0.02,
    "smarkets": 0.0,
    "polymarket": 0.0,
    "sportsbook": 0.0,
}


# ---------------------------------------------------------------------------
# Output records
# ---------------------------------------------------------------------------

@dataclass
class OverUnderLine:
    """A priced over/under count market."""

    market: str            # canonical key, e.g. "team_total_shots"
    subject: str           # team or player name
    line: float
    mean: float
    p_over: float
    p_under: float
    fair_over: float
    fair_under: float
    venues: Tuple[str, ...] = ("sportsbook",)
    source: str = "prior"

    def as_dict(self) -> Dict[str, object]:
        return {
            "market": self.market,
            "subject": self.subject,
            "line": self.line,
            "mean": round(self.mean, 4),
            "p_over": round(self.p_over, 6),
            "p_under": round(self.p_under, 6),
            "fair_over": _round_odds(self.fair_over),
            "fair_under": _round_odds(self.fair_under),
            "venues": list(self.venues),
            "source": self.source,
        }


@dataclass
class BinaryLine:
    """A priced yes/no market (e.g. player to be booked)."""

    market: str
    subject: str
    prob: float
    fair: float
    venues: Tuple[str, ...] = ("sportsbook",)
    source: str = "prior"

    def as_dict(self) -> Dict[str, object]:
        return {
            "market": self.market,
            "subject": self.subject,
            "prob": round(self.prob, 6),
            "fair": _round_odds(self.fair),
            "venues": list(self.venues),
            "source": self.source,
        }


def _round_odds(o: float) -> Optional[float]:
    if o is None or math.isinf(o) or math.isnan(o):
        return None
    return round(o, 3)


# ---------------------------------------------------------------------------
# Distribution helpers (mirror props.py, kept local so the module stands alone)
# ---------------------------------------------------------------------------

def _nb_sf_over(line: float, mu: float, k: float) -> float:
    """P(N > line) for a half-integer line under NB(mean mu, dispersion k)."""
    if mu <= 0:
        return 0.0
    p = k / (k + mu)
    return float(nbinom.sf(math.floor(line), k, p))


def _pois_sf_over(line: float, mu: float) -> float:
    """P(N > line) for a half-integer line under Poisson(mu)."""
    if mu <= 0:
        return 0.0
    return float(poisson.sf(math.floor(line), mu))


def _fair_pair(p_over: float) -> Tuple[float, float]:
    p_under = 1.0 - p_over
    over = float("inf") if p_over <= 0 else 1.0 / p_over
    under = float("inf") if p_under <= 0 else 1.0 / p_under
    return over, under


# ---------------------------------------------------------------------------
# Rate store (optional players.db; degrades to priors)
# ---------------------------------------------------------------------------

@dataclass
class TeamRates:
    """Per-match team rates (counts per match). NaN/None => use prior."""

    team: str
    shots_pm: Optional[float] = None
    sot_pm: Optional[float] = None
    fouls_pm: Optional[float] = None
    corners_pm: Optional[float] = None
    yellows_pm: Optional[float] = None
    cards_pm: Optional[float] = None
    source: str = "prior"


@dataclass
class PlayerRate:
    """Per-90 player rates (counts per 90 minutes)."""

    player: str
    team: str
    sot_p90: Optional[float] = None
    fouls_p90: Optional[float] = None
    yellows_p90: Optional[float] = None
    npxg_share: Optional[float] = None
    expected_minutes: float = 90.0
    source: str = "prior"


class RateStore:
    """Loads team/player rates from ``players.db`` if present, else priors.

    The schema matches the Phase-2 ``players.db`` (tables ``team_rates`` and
    ``players``). When the DB is missing every lookup returns a prior-filled
    record, so callers never branch on availability.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._team: Dict[str, TeamRates] = {}
        self._player: Dict[Tuple[str, str], PlayerRate] = {}
        self.loaded = False
        if db_path:
            self._try_load(db_path)

    def _try_load(self, db_path: str) -> None:
        import os
        import sqlite3

        if not os.path.exists(db_path):
            return
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return
        try:
            tcols = [r[1] for r in con.execute("PRAGMA table_info(team_rates)")]
            if tcols:
                for row in con.execute("SELECT * FROM team_rates"):
                    d = dict(zip(tcols, row))
                    tr = TeamRates(
                        team=str(d.get("team", "")),
                        shots_pm=_f(d.get("shots_pm")),
                        sot_pm=_f(d.get("sot_pm")),
                        fouls_pm=_f(d.get("fouls_pm")),
                        corners_pm=_f(d.get("corners_pm")),
                        yellows_pm=_f(d.get("yellows_pm")),
                        cards_pm=_f(d.get("cards_pm")),
                        source="players.db",
                    )
                    self._team[tr.team] = tr
            pcols = {r[1] for r in con.execute("PRAGMA table_info(players)")}
            if pcols:
                names = [r[1] for r in con.execute("PRAGMA table_info(players)")]
                for row in con.execute("SELECT * FROM players"):
                    d = dict(zip(names, row))
                    pr = PlayerRate(
                        player=str(d.get("player", "")),
                        team=str(d.get("team", "")),
                        sot_p90=_f(d.get("sot_p90")),
                        fouls_p90=_f(d.get("fouls_p90")),
                        yellows_p90=_f(d.get("yellows_p90")),
                        npxg_share=_f(d.get("npxg_p90")),  # raw rate; share derived elsewhere
                        source="players.db",
                    )
                    self._player[(pr.team, pr.player)] = pr
            self.loaded = True
        except sqlite3.Error:
            self.loaded = False
        finally:
            con.close()

    def team(self, name: str) -> TeamRates:
        return self._team.get(name, TeamRates(team=name, source="prior"))

    def player(self, team: str, name: str) -> PlayerRate:
        return self._player.get((team, name), PlayerRate(player=name, team=team, source="prior"))


def _f(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Team-total pricers
# ---------------------------------------------------------------------------

DEFAULT_GOAL_LINES = (0.5, 1.5, 2.5)
DEFAULT_SHOT_LINES = (8.5, 10.5, 12.5)
DEFAULT_SOT_LINES = (2.5, 3.5, 4.5)
DEFAULT_FOUL_LINES = (9.5, 11.5, 13.5)


def _scaled_team_mean(base: float, lambda_team: float, elasticity: float = SHOT_ELASTICITY,
                      base_lambda: float = BASE_TEAM_LAMBDA) -> float:
    """Damped attack scaling of a team count, like CornersModel."""
    if base_lambda <= 0:
        return base
    rel = lambda_team / base_lambda - 1.0
    return max(base * (1.0 + elasticity * rel), 0.0)


def team_total_goals(team: str, lambda_team: float,
                     lines: Sequence[float] = DEFAULT_GOAL_LINES,
                     venues: Tuple[str, ...] = ("sportsbook", "exchange")) -> List[OverUnderLine]:
    """Team total goals as Poisson(lambda_team) over/under at each line."""
    out: List[OverUnderLine] = []
    for line in lines:
        p_over = _pois_sf_over(line, lambda_team)
        fo, fu = _fair_pair(p_over)
        out.append(OverUnderLine("team_total_goals", team, float(line), lambda_team,
                                 p_over, 1.0 - p_over, fo, fu, venues, "model"))
    return out


def team_total_count(market: str, team: str, mean: float, dispersion: float,
                     lines: Sequence[float], venues: Tuple[str, ...] = ("sportsbook",),
                     source: str = "prior") -> List[OverUnderLine]:
    """Generic NB team-total over/under (shots, SoT, fouls, corners, cards)."""
    out: List[OverUnderLine] = []
    for line in lines:
        p_over = _nb_sf_over(line, mean, dispersion)
        fo, fu = _fair_pair(p_over)
        out.append(OverUnderLine(market, team, float(line), mean, p_over,
                                 1.0 - p_over, fo, fu, venues, source))
    return out


def team_total_shots(team: str, lambda_team: float, rates: Optional[TeamRates] = None,
                     lines: Sequence[float] = DEFAULT_SHOT_LINES) -> List[OverUnderLine]:
    base, k = TEAM_PRIORS["shots"]
    src = "prior"
    if rates and rates.shots_pm:
        base, src = rates.shots_pm, rates.source
    mean = _scaled_team_mean(base, lambda_team)
    return team_total_count("team_total_shots", team, mean, k, lines, ("sportsbook",), src)


def team_total_sot(team: str, lambda_team: float, rates: Optional[TeamRates] = None,
                   lines: Sequence[float] = DEFAULT_SOT_LINES) -> List[OverUnderLine]:
    base, k = TEAM_PRIORS["sot"]
    src = "prior"
    if rates and rates.sot_pm:
        base, src = rates.sot_pm, rates.source
    mean = _scaled_team_mean(base, lambda_team)
    return team_total_count("team_total_sot", team, mean, k, lines, ("sportsbook",), src)


def team_total_fouls(team: str, rates: Optional[TeamRates] = None,
                     lines: Sequence[float] = DEFAULT_FOUL_LINES) -> List[OverUnderLine]:
    base, k = TEAM_PRIORS["fouls"]
    src = "prior"
    if rates and rates.fouls_pm:
        base, src = rates.fouls_pm, rates.source
    return team_total_count("team_total_fouls", team, base, k, lines, ("sportsbook",), src)


def team_total_corners(team: str, lambda_team: float, lambda_opp: float,
                       lines: Sequence[float] = (3.5, 4.5, 5.5),
                       model: Optional[CornersModel] = None) -> List[OverUnderLine]:
    """Team corners via the calibrated :class:`CornersModel` attack-share split."""
    model = model or CornersModel()
    mean = model.team_mean(lambda_team, lambda_opp)
    out: List[OverUnderLine] = []
    for line in lines:
        p_over = model.prob_team_over(line, lambda_team, lambda_opp)
        fo, fu = _fair_pair(p_over)
        out.append(OverUnderLine("team_total_corners", team, float(line), mean, p_over,
                                 1.0 - p_over, fo, fu, ("sportsbook",), "model"))
    return out


# ---------------------------------------------------------------------------
# Player-count pricers
# ---------------------------------------------------------------------------

DEFAULT_PLAYER_SOT_LINES = (0.5, 1.5, 2.5)
DEFAULT_PLAYER_FOUL_LINES = (0.5, 1.5, 2.5)


def _player_mean(rate_p90: float, minutes: float, context_mult: float = 1.0) -> float:
    return max(rate_p90, 0.0) * (max(minutes, 0.0) / 90.0) * max(context_mult, 0.0)


def player_shots_on_target(player: str, team: str, rate: Optional[PlayerRate] = None,
                           lambda_team: Optional[float] = None,
                           lines: Sequence[float] = DEFAULT_PLAYER_SOT_LINES) -> List[OverUnderLine]:
    """Player SoT over/under. Mean = sot_p90 * minutes/90 * attack-context."""
    p90 = (rate.sot_p90 if rate and rate.sot_p90 else PLAYER_P90_PRIORS["sot"])
    minutes = rate.expected_minutes if rate else 90.0
    src = rate.source if rate and rate.sot_p90 else "prior"
    ctx = 1.0 if lambda_team is None else max(lambda_team / BASE_TEAM_LAMBDA, 0.3)
    ctx = 1.0 + SHOT_ELASTICITY * (ctx - 1.0)
    mean = _player_mean(p90, minutes, ctx)
    return team_total_count("player_shots_on_target", player, mean,
                            PLAYER_DISPERSION["sot"], lines, ("sportsbook",), src)


def player_fouls(player: str, team: str, rate: Optional[PlayerRate] = None,
                 aggression: float = 1.0,
                 lines: Sequence[float] = DEFAULT_PLAYER_FOUL_LINES) -> List[OverUnderLine]:
    """Player fouls committed over/under (NB)."""
    p90 = (rate.fouls_p90 if rate and rate.fouls_p90 else PLAYER_P90_PRIORS["fouls"])
    minutes = rate.expected_minutes if rate else 90.0
    src = rate.source if rate and rate.fouls_p90 else "prior"
    mean = _player_mean(p90, minutes, aggression)
    return team_total_count("player_fouls", player, mean,
                            PLAYER_DISPERSION["fouls"], lines, ("sportsbook",), src)


def player_to_be_booked(player: str, team: str, rate: Optional[PlayerRate] = None,
                        aggression: float = 1.0) -> BinaryLine:
    """P(player receives at least one yellow) = 1 - exp(-yellow_intensity).

    ``aggression`` scales the booking intensity for a feisty fixture/referee
    (1.0 = neutral). A red is counted as a card here (the common "to be carded"
    market); refine to yellow-only when reds are modelled separately.
    """
    p90 = (rate.yellows_p90 if rate and rate.yellows_p90 else PLAYER_P90_PRIORS["yellows"])
    minutes = rate.expected_minutes if rate else 90.0
    src = rate.source if rate and rate.yellows_p90 else "prior"
    intensity = _player_mean(p90, minutes, aggression)
    prob = 1.0 - math.exp(-intensity)
    fair = float("inf") if prob <= 0 else 1.0 / prob
    return BinaryLine("player_to_be_booked", player, prob, fair, ("sportsbook",), src)


# ---------------------------------------------------------------------------
# Fee / overround helpers
# ---------------------------------------------------------------------------

def price_with_overround(fair_probs: Sequence[float], margin: float = 0.05) -> List[float]:
    """Apply a multiplicative book margin to fair probs, return display odds.

    ``margin`` is the overround (e.g. 0.05 = 105% book). Useful to show what a
    typical sportsbook *would* charge versus our model-fair price.
    """
    total = sum(fair_probs)
    if total <= 0:
        return [float("inf")] * len(fair_probs)
    scale = (1.0 + margin) / total
    return [float("inf") if p <= 0 else 1.0 / (p * scale) for p in fair_probs]


def ev_vs_offer(model_prob: float, offered_odds: float, venue: str = "sportsbook",
                fees: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """EV per unit staked at ``offered_odds``, net of venue commission.

    Returns ``ev_per_unit`` (1.0 = break-even), ``edge_pct`` and the
    ``net_odds`` after fee on net winnings.
    """
    fees = fees or DEFAULT_FEES
    fee = fees.get(venue, 0.0)
    if offered_odds <= 1.0:
        return {"ev_per_unit": 0.0, "edge_pct": -100.0, "net_odds": offered_odds}
    net_odds = 1.0 + (offered_odds - 1.0) * (1.0 - fee)
    ev = model_prob * net_odds
    return {"ev_per_unit": ev, "edge_pct": (ev - 1.0) * 100.0, "net_odds": net_odds}


# ---------------------------------------------------------------------------
# Full bet-builder payload for one fixture
# ---------------------------------------------------------------------------

def fixture_betbuilder(home: str, away: str, lambda_home: float, lambda_away: float,
                       store: Optional[RateStore] = None,
                       scorers: Optional[Dict[str, List[PlayerParams]]] = None,
                       aggression: float = 1.0,
                       top_n_players: int = 4) -> Dict[str, object]:
    """Price the full bet-builder market set for one fixture.

    ``store`` provides team/player rates (priors if absent). ``scorers`` maps
    team -> player params for the player-to-score and player-count markets
    (typically from :func:`wca.models.scorers.load_player_overrides`).
    """
    store = store or RateStore()
    corners = CornersModel()
    cards = CardsModel()
    total_lambda = lambda_home + lambda_away

    team_lines: List[Dict[str, object]] = []
    for team, lam, opp_lam in ((home, lambda_home, lambda_away),
                               (away, lambda_away, lambda_home)):
        tr = store.team(team)
        team_lines += [l.as_dict() for l in team_total_goals(team, lam)]
        team_lines += [l.as_dict() for l in team_total_shots(team, lam, tr)]
        team_lines += [l.as_dict() for l in team_total_sot(team, lam, tr)]
        team_lines += [l.as_dict() for l in team_total_fouls(team, tr)]
        team_lines += [l.as_dict() for l in team_total_corners(team, lam, opp_lam, model=corners)]

    # Match-total cards via calibrated CardsModel (aggression from priors).
    card_lines = [OverUnderLine("match_total_cards", f"{home} vs {away}", float(line),
                                cards.mean_total(aggression, aggression),
                                cards.prob_over(line, aggression, aggression),
                                1.0 - cards.prob_over(line, aggression, aggression),
                                *_fair_pair(cards.prob_over(line, aggression, aggression)),
                                ("sportsbook",), "model").as_dict()
                  for line in (3.5, 4.5, 5.5)]

    player_lines: List[Dict[str, object]] = []
    scorer_lines: List[Dict[str, object]] = []
    sp = ScorerPricer()
    scorers = scorers or {}
    for team, lam in ((home, lambda_home), (away, lambda_away)):
        params = scorers.get(team, [])[:top_n_players]
        for pp in params:
            sl = sp.price_player(pp, team_lambda=lam, total_lambda=total_lambda)
            scorer_lines.append(_scorer_as_dict(sl))
            pr = store.player(team, pp.name)
            pr.expected_minutes = pp.expected_minutes
            player_lines += [l.as_dict() for l in
                             player_shots_on_target(pp.name, team, pr, lam)]
            player_lines += [l.as_dict() for l in player_fouls(pp.name, team, pr, aggression)]
            player_lines.append(player_to_be_booked(pp.name, team, pr, aggression).as_dict())

    return {
        "fixture": f"{home} vs {away}",
        "lambda_home": round(lambda_home, 4),
        "lambda_away": round(lambda_away, 4),
        "team_totals": team_lines,
        "match_cards": card_lines,
        "player_to_score": scorer_lines,
        "player_props": player_lines,
        "notes": ("SoT/cards/corners/fouls are sportsbook-only; values are model "
                  "fair odds. Player markets use players.db rates when present, "
                  "else tournament priors."),
    }


def _scorer_as_dict(sl: ScorerLine) -> Dict[str, object]:
    return {
        "market": "player_to_score",
        "subject": sl.player,
        "team": sl.team,
        "p_anytime": round(sl.p_anytime, 6),
        "p_first": round(sl.p_first, 6),
        "p_two_plus": round(sl.p_two_plus, 6),
        "fair_anytime": _round_odds(sl.fair_anytime),
        "fair_first": _round_odds(sl.fair_first),
        "venues": ["sportsbook", "polymarket"],
        "source": "model",
    }
