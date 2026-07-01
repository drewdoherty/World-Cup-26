"""Polymarket player-prop pricing — anytime/2+/3+ goals, shots, SoT, assists.

This unlocks the largest untapped Polymarket surface: the per-match
``"<Home> vs. <Away> - Player Props"`` events expose thousands of graded
player questions ("Lionel Messi: 1+ goals", "Ousmane Dembélé: 2+ shots",
"Kylian Mbappé: 2+ shots on target", "1+ assists") that the rest of the
codebase never prices. This module is the pure, testable pricing core; the
live Polymarket fetch + join lives in ``scripts/wca_player_props.py``.

Model
-----
Each player-prop is a count threshold ``k+``. We model the player's count of
the relevant event over their expected minutes as Poisson with rate
``lambda = rate_per90 * expected_minutes / 90`` and price::

    P(k+) = 1 - CDF(k - 1; lambda) = P(N >= k)

so anytime goals = ``P(1+) = 1 - exp(-lambda)``. Poisson (not Negative
Binomial) is the deliberate, conservative choice here because the player-prop
universe is dominated by *low* thresholds (1+/2+/3+) where the upper-tail
shape of NB matters little and we have no per-player dispersion estimate to
justify it — :mod:`wca.models.betbuilder` keeps NB for higher team-total
lines where dispersion is fit. Using Poisson everywhere here keeps the math
transparent and avoids manufacturing a dispersion parameter we can't source.

Why a separate module from ``betbuilder``/``scorers``
-----------------------------------------------------
* :mod:`wca.models.scorers` prices *goals* off a non-penalty xG **share** of
  the team lambda. That is the right input for goals; this module reuses it
  exactly for the goals family (anytime/2+/3+) so the two never disagree.
* :mod:`wca.models.betbuilder` already prices player SoT (NB) off a
  ``sot_p90`` rate; we reuse its rate sourcing (``RateStore`` → ``players.db``
  → priors) but re-express SoT as Poisson ``k+`` to match the PM market grid,
  and add **shots** and **assists** families that betbuilder does not cover.
* The novel piece is the *Polymarket join*: matching the model's per-player
  probabilities to live PM ``groupItemTitle`` strings ("Player: 2+ shots on
  target") using the same name-matching the scorer-token resolver uses.

Data sourcing & HONEST limitations
----------------------------------
Rates come, in order of preference, from:

1. ``players.db`` per-90 rates (StatsBomb WC2018+2022), via
   :class:`wca.models.betbuilder.RateStore` — present only after
   ``scripts/wca_build_players_db.py`` has run (it is NOT on every box; the
   StatsBomb cache must be warm). Goals/SoT have real per-90 columns there;
   **assists are not in players.db** and fall back to a derived prior.
2. ``data/players.json`` analyst ``npxg_share`` overrides for the *goals*
   family (the same store :mod:`wca.models.scorers` uses) — this is what makes
   marquee names (Messi, Mbappé) sharp even with no StatsBomb history.
3. Tournament/positional priors otherwise (deliberately modest, see
   :data:`PLAYER_P90_PRIORS`), so the fallback never invents a confident edge.

The dominant uncertainty is **NOT** the count distribution — it is
**lineup/minutes**: whether the player starts, and for how long. We expose
``expected_minutes`` per player and SHRINK rate-derived rates toward priors for
thin-sample players (``shrink_rate``). But we have **no live lineup feed**, so
expected_minutes is an estimate. A 60'→90' minutes error moves a 1+ goals
probability by tens of percent; treat any single-prop "edge" as dominated by
this, and prefer markets/players where the start is near-certain. The join
returns ``minutes_source``/``rate_source`` on every row so callers can gate on
provenance rather than trusting a number blindly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from wca.data.polymarket import _norm_player, _player_key
from wca.models.scorers import PlayerParams

# ---------------------------------------------------------------------------
# Market taxonomy
# ---------------------------------------------------------------------------

# Canonical market types this module prices. Each is a Poisson k+ threshold on
# a per-player count.
MK_GOALS = "goals"
MK_SHOTS = "shots"
MK_SOT = "shots_on_target"
MK_ASSISTS = "assists"

ALL_MARKET_TYPES = (MK_GOALS, MK_SHOTS, MK_SOT, MK_ASSISTS)

# Thresholds Polymarket actually lists per family (anytime == 1+).
DEFAULT_THRESHOLDS: Dict[str, Tuple[int, ...]] = {
    MK_GOALS: (1, 2, 3),
    MK_SHOTS: (1, 2, 3),
    MK_SOT: (1, 2),
    MK_ASSISTS: (1,),
}

# ---------------------------------------------------------------------------
# Priors (per-90), used only when no rate/share is available. Deliberately
# modest — a generic rotation forward — so the prior never manufactures edge.
# Order of magnitude WC values; replace as players.db coverage grows.
# ---------------------------------------------------------------------------
PLAYER_P90_PRIORS: Dict[str, float] = {
    MK_SHOTS: 1.6,      # total shots per 90 for a generic attacker
    MK_SOT: 0.6,        # shots on target per 90
    MK_ASSISTS: 0.12,   # assists per 90
    # goals is priced off npxg_share -> team lambda, not a flat p90 prior.
}

# When deriving SoT/shots from a goals rate (no direct rate available) these
# ratios convert goals/90 -> shots/90, SoT/90. WC aggregate: ~0.10 goals/shot,
# ~0.35 SoT/shot. Used only as a last-resort coupling, flagged source="derived".
SHOTS_PER_GOAL = 10.0
SOT_PER_SHOT = 0.35
# Assists per goal (team-level WC assist:goal ~ 0.75; per-player attackers
# create roughly as often as they finish at the low end). Used to derive an
# assists rate from the goals rate when no assist data exists.
ASSISTS_PER_GOAL = 0.6

# Shrinkage: a thin-sample per-90 rate is shrunk toward the prior with weight
# n_eff / (n_eff + SHRINK_K) where n_eff ~ minutes/90.
SHRINK_K = 6.0  # ~6 full matches of evidence to half-trust an empirical rate


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

@dataclass
class PlayerPropRates:
    """Per-90 rates + expected minutes for one player in one fixture.

    Any ``None`` rate falls back to a prior (or a goals-derived value). Rates
    are *per 90 minutes*; ``expected_minutes`` prorates them to a match λ.
    """

    player: str
    team: str
    goals_p90: Optional[float] = None
    shots_p90: Optional[float] = None
    sot_p90: Optional[float] = None
    assists_p90: Optional[float] = None
    expected_minutes: float = 90.0
    # n_eff ~ minutes of evidence behind the rates (for shrinkage). 0 => prior.
    sample_minutes: float = 0.0
    rate_source: str = "prior"
    minutes_source: str = "assumed_90"


@dataclass
class PropPrice:
    """A priced player-prop threshold."""

    player: str
    team: str
    market_type: str       # one of ALL_MARKET_TYPES
    threshold: int         # k in "k+"
    lam: float             # Poisson rate over expected minutes
    prob: float            # P(N >= k)
    fair_odds: float       # 1 / prob
    rate_source: str
    minutes_source: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "player": self.player,
            "team": self.team,
            "market_type": self.market_type,
            "threshold": self.threshold,
            "lam": round(self.lam, 5),
            "prob": round(self.prob, 6),
            "fair_odds": _round_odds(self.fair_odds),
            "rate_source": self.rate_source,
            "minutes_source": self.minutes_source,
        }


@dataclass
class PropEdgeRow:
    """One matched model-vs-PM row from :func:`join_fixture_to_pm`."""

    player: str
    team: str
    market_type: str
    threshold: int
    model_prob: float
    pm_price: float
    edge: float            # model_prob - pm_price
    token_id: str
    fair_odds: float
    rate_source: str
    minutes_source: str
    pm_label: str          # the raw PM groupItemTitle matched
    match_kind: str        # "exact" | "key" (name-match confidence)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "player": self.player,
            "team": self.team,
            "market_type": self.market_type,
            "threshold": self.threshold,
            "model_prob": round(self.model_prob, 6),
            "pm_price": round(self.pm_price, 6),
            "edge": round(self.edge, 6),
            "token_id": self.token_id,
            "fair_odds": _round_odds(self.fair_odds),
            "rate_source": self.rate_source,
            "minutes_source": self.minutes_source,
            "pm_label": self.pm_label,
            "match_kind": self.match_kind,
        }


def _round_odds(o: float) -> Optional[float]:
    if o is None or math.isinf(o) or math.isnan(o):
        return None
    return round(o, 3)


# ---------------------------------------------------------------------------
# Poisson core
# ---------------------------------------------------------------------------

def poisson_at_least(k: int, lam: float) -> float:
    """P(N >= k) for N ~ Poisson(lam), k an integer >= 0.

    Pure (no SciPy dependency) so the math is transparent and the module is
    import-light. P(N >= 0) == 1; lam <= 0 => 0 for k >= 1.
    """
    if k <= 0:
        return 1.0
    if lam <= 0.0:
        return 0.0
    # P(N >= k) = 1 - sum_{i=0}^{k-1} e^-lam lam^i / i!  (k is tiny: 1..3)
    term = math.exp(-lam)   # i = 0
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def prop_lambda(rate_p90: float, expected_minutes: float) -> float:
    """Match-level Poisson rate = rate_per90 * minutes / 90 (clamped >= 0)."""
    return max(rate_p90, 0.0) * (max(expected_minutes, 0.0) / 90.0)


def shrink_rate(empirical_p90: Optional[float], prior_p90: float,
                sample_minutes: float, shrink_k: float = SHRINK_K) -> float:
    """Shrink a thin-sample per-90 rate toward a prior.

    Weight ``w = n_eff / (n_eff + shrink_k)`` with ``n_eff = sample_minutes/90``
    full-match equivalents; ``w=0`` (no evidence) returns the prior, ``w→1``
    (lots of evidence) returns the empirical rate. This is the lever that keeps
    a player with 45 StatsBomb minutes from getting a confident edge.
    """
    if empirical_p90 is None:
        return prior_p90
    n_eff = max(sample_minutes, 0.0) / 90.0
    w = n_eff / (n_eff + max(shrink_k, 1e-9))
    return w * empirical_p90 + (1.0 - w) * prior_p90


# ---------------------------------------------------------------------------
# Resolve effective per-90 rates for each family (rate -> share -> prior cascade)
# ---------------------------------------------------------------------------

def _goals_rate_p90(rates: PlayerPropRates, team_lambda: Optional[float],
                    npxg_share: Optional[float], pen_xg: float,
                    penalty_taker: bool) -> Tuple[float, str]:
    """Effective goals/90 for a player.

    Preference: a direct ``goals_p90`` rate (shrunk) if present; else derive it
    from the analyst ``npxg_share`` x the team's non-penalty lambda (the
    :mod:`wca.models.scorers` convention, the sharp path for marquee names);
    else a flat share of a positional prior. Returns (rate_p90, source).
    """
    if rates.goals_p90 is not None:
        # We still have no goals-specific prior p90; shrink toward the
        # share-derived value when a share exists, else toward the raw rate.
        if npxg_share is not None and team_lambda is not None:
            prior = max(team_lambda - pen_xg, 0.0) * npxg_share + (
                pen_xg if penalty_taker else 0.0)
            r = shrink_rate(rates.goals_p90, prior, rates.sample_minutes)
            return r, ("players.db+share" if rates.sample_minutes > 0 else "share")
        return rates.goals_p90, rates.rate_source
    if npxg_share is not None and team_lambda is not None:
        # scorers convention: non-pen xG share of team lambda over 90, + pen.
        r = max(team_lambda - pen_xg, 0.0) * npxg_share + (
            pen_xg if penalty_taker else 0.0)
        return r, "share"
    return 0.0, "none"


def _shots_rate_p90(rates: PlayerPropRates, goals_p90: float) -> Tuple[float, str]:
    if rates.shots_p90 is not None:
        return shrink_rate(rates.shots_p90, PLAYER_P90_PRIORS[MK_SHOTS],
                           rates.sample_minutes), rates.rate_source
    if goals_p90 > 0:
        return goals_p90 * SHOTS_PER_GOAL, "derived_from_goals"
    return PLAYER_P90_PRIORS[MK_SHOTS], "prior"


def _sot_rate_p90(rates: PlayerPropRates, shots_p90: float,
                  shots_src: str) -> Tuple[float, str]:
    if rates.sot_p90 is not None:
        return shrink_rate(rates.sot_p90, PLAYER_P90_PRIORS[MK_SOT],
                           rates.sample_minutes), rates.rate_source
    if shots_src in ("derived_from_goals", "players.db") and shots_p90 > 0:
        return shots_p90 * SOT_PER_SHOT, "derived_from_shots"
    return PLAYER_P90_PRIORS[MK_SOT], "prior"


def _assists_rate_p90(rates: PlayerPropRates, goals_p90: float) -> Tuple[float, str]:
    if rates.assists_p90 is not None:
        return shrink_rate(rates.assists_p90, PLAYER_P90_PRIORS[MK_ASSISTS],
                           rates.sample_minutes), rates.rate_source
    if goals_p90 > 0:
        return goals_p90 * ASSISTS_PER_GOAL, "derived_from_goals"
    return PLAYER_P90_PRIORS[MK_ASSISTS], "prior"


# ---------------------------------------------------------------------------
# Per-player pricing
# ---------------------------------------------------------------------------

def price_player_props(
    rates: PlayerPropRates,
    *,
    team_lambda: Optional[float] = None,
    npxg_share: Optional[float] = None,
    penalty_taker: bool = False,
    pen_xg: float = 0.18,
    thresholds: Optional[Dict[str, Sequence[int]]] = None,
    markets: Sequence[str] = ALL_MARKET_TYPES,
) -> List[PropPrice]:
    """Price every (market_type, threshold) for one player.

    ``team_lambda`` + ``npxg_share`` feed the goals family (scorers convention)
    and, when no direct count rate exists, cascade into shots/SoT/assists. Pass
    them from :func:`wca.models.scorers.PlayerParams`/the fixture model.
    """
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update({k: tuple(v) for k, v in thresholds.items()})

    goals_p90, goals_src = _goals_rate_p90(
        rates, team_lambda, npxg_share, pen_xg, penalty_taker)
    shots_p90, shots_src = _shots_rate_p90(rates, goals_p90)
    sot_p90, sot_src = _sot_rate_p90(rates, shots_p90, shots_src)
    assists_p90, assists_src = _assists_rate_p90(rates, goals_p90)

    fam = {
        MK_GOALS: (goals_p90, goals_src),
        MK_SHOTS: (shots_p90, shots_src),
        MK_SOT: (sot_p90, sot_src),
        MK_ASSISTS: (assists_p90, assists_src),
    }

    out: List[PropPrice] = []
    for mt in markets:
        if mt not in fam:
            continue
        rate_p90, src = fam[mt]
        lam = prop_lambda(rate_p90, rates.expected_minutes)
        for k in th.get(mt, ()):  # type: ignore[arg-type]
            p = poisson_at_least(int(k), lam)
            fair = float("inf") if p <= 0 else 1.0 / p
            out.append(PropPrice(
                player=rates.player, team=rates.team, market_type=mt,
                threshold=int(k), lam=lam, prob=p, fair_odds=fair,
                rate_source=src, minutes_source=rates.minutes_source))
    return out


# ---------------------------------------------------------------------------
# Fixture pricing
# ---------------------------------------------------------------------------

def price_fixture_props(
    home: str,
    away: str,
    *,
    lambda_home: float,
    lambda_away: float,
    rates_by_player: Optional[Dict[Tuple[str, str], PlayerPropRates]] = None,
    scorers_by_team: Optional[Dict[str, Sequence[PlayerParams]]] = None,
    pen_xg: float = 0.18,
    thresholds: Optional[Dict[str, Sequence[int]]] = None,
    markets: Sequence[str] = ALL_MARKET_TYPES,
) -> Dict[Tuple[str, str, int], float]:
    """Price all player props for a fixture.

    Returns ``{(player, market_type, threshold): prob}``.

    Inputs (in order of how they drive each player):

    * ``scorers_by_team`` — ``{team -> [PlayerParams]}`` from
      :func:`wca.models.scorers.load_player_overrides`. This supplies
      ``npxg_share`` / ``penalty_taker`` / ``expected_minutes`` and is the
      sharp source for the GOALS family (and the derived shots/SoT/assists when
      no count rate is given). This is the primary, always-available source.
    * ``rates_by_player`` — ``{(team, player) -> PlayerPropRates}`` from
      ``players.db`` (via :class:`wca.models.betbuilder.RateStore`) for direct
      per-90 count rates (goals/shots/SoT). Optional; merged on top of the
      scorer params so a player with both gets data-driven counts AND a
      share-driven goals prior to shrink toward.

    A player named in EITHER source is priced. If only ``rates_by_player`` has
    a player (e.g. a defender with SoT history but no analyst share) they are
    priced off rates+priors; if only ``scorers_by_team`` has them they are
    priced off the share cascade.

    DATA DEPENDENCY: with neither source populated this returns ``{}``. The
    caller (the script) is responsible for loading at least ``players.json``.
    """
    rates_by_player = rates_by_player or {}
    scorers_by_team = scorers_by_team or {}

    # team -> lambda (which side is this team on this fixture)
    team_lambda: Dict[str, float] = {home: lambda_home, away: lambda_away}

    # Collect the union of players from both sources, keyed by (team, player).
    params_by_key: Dict[Tuple[str, str], PlayerParams] = {}
    for team, plist in scorers_by_team.items():
        for pp in plist:
            params_by_key[(team, pp.name)] = pp

    keys = set(rates_by_player.keys()) | set(params_by_key.keys())

    out: Dict[Tuple[str, str, int], float] = {}
    for (team, player) in sorted(keys):
        lam_team = team_lambda.get(team)
        pp = params_by_key.get((team, player))
        rates = rates_by_player.get((team, player))
        if rates is None:
            # Build a bare rates record carrying just minutes from the params.
            mins = pp.expected_minutes if pp else 90.0
            rates = PlayerPropRates(
                player=player, team=team, expected_minutes=mins,
                minutes_source=("scorers.json" if pp else "assumed_90"))
        else:
            # Prefer scorer-params minutes when present (analyst lineup view).
            if pp is not None:
                rates.expected_minutes = pp.expected_minutes
                rates.minutes_source = "scorers.json"
        npxg_share = pp.npxg_share if pp else None
        pen_taker = pp.penalty_taker if pp else False

        priced = price_player_props(
            rates, team_lambda=lam_team, npxg_share=npxg_share,
            penalty_taker=pen_taker, pen_xg=pen_xg,
            thresholds=thresholds, markets=markets)
        for pr in priced:
            out[(player, pr.market_type, pr.threshold)] = pr.prob
    return out


def price_fixture_props_detailed(
    home: str,
    away: str,
    *,
    lambda_home: float,
    lambda_away: float,
    rates_by_player: Optional[Dict[Tuple[str, str], PlayerPropRates]] = None,
    scorers_by_team: Optional[Dict[str, Sequence[PlayerParams]]] = None,
    pen_xg: float = 0.18,
    thresholds: Optional[Dict[str, Sequence[int]]] = None,
    markets: Sequence[str] = ALL_MARKET_TYPES,
) -> List[PropPrice]:
    """Like :func:`price_fixture_props` but returns full :class:`PropPrice` rows.

    Used by the join (it needs the team, fair odds, and provenance per prop,
    not just the probability). The two share their player-collection logic.
    """
    rates_by_player = rates_by_player or {}
    scorers_by_team = scorers_by_team or {}
    team_lambda: Dict[str, float] = {home: lambda_home, away: lambda_away}

    params_by_key: Dict[Tuple[str, str], PlayerParams] = {}
    for team, plist in scorers_by_team.items():
        for pp in plist:
            params_by_key[(team, pp.name)] = pp
    keys = set(rates_by_player.keys()) | set(params_by_key.keys())

    out: List[PropPrice] = []
    for (team, player) in sorted(keys):
        lam_team = team_lambda.get(team)
        pp = params_by_key.get((team, player))
        rates = rates_by_player.get((team, player))
        if rates is None:
            mins = pp.expected_minutes if pp else 90.0
            rates = PlayerPropRates(
                player=player, team=team, expected_minutes=mins,
                minutes_source=("scorers.json" if pp else "assumed_90"))
        elif pp is not None:
            rates.expected_minutes = pp.expected_minutes
            rates.minutes_source = "scorers.json"
        out.extend(price_player_props(
            rates, team_lambda=lam_team,
            npxg_share=(pp.npxg_share if pp else None),
            penalty_taker=(pp.penalty_taker if pp else False),
            pen_xg=pen_xg, thresholds=thresholds, markets=markets))
    return out


# ---------------------------------------------------------------------------
# Polymarket market-string parsing
# ---------------------------------------------------------------------------

# Maps the suffix after "<Player>:" to (market_type, threshold). Polymarket
# wording observed: "1+ goals", "2+ goals", "3+ goals", "1+ shots",
# "2+ shots", "1+ shots on target", "2+ shots on target", "1+ assists".
# NOTE: "1+ goals + assists" is a COMBINED market and is intentionally NOT
# matched here (it is a different bet from either goals or assists).
def parse_pm_prop_label(label: str) -> Optional[Tuple[str, str, int]]:
    """Parse a PM ``groupItemTitle`` into ``(player, market_type, threshold)``.

    ``label`` looks like ``"Kylian Mbappé: 2+ shots on target"``. Returns
    ``None`` for labels that are not a recognised single-stat player prop
    (e.g. combined "goals + assists", or a non-prop market). Order of the
    suffix checks matters: "shots on target" must be tested before "shots".
    """
    if not label or ":" not in label:
        return None
    name, _, suffix = label.partition(":")
    name = name.strip()
    suffix = suffix.strip().lower()
    if not name or not suffix:
        return None

    # Threshold: leading "<n>+".
    import re
    m = re.match(r"^(\d+)\s*\+\s*(.*)$", suffix)
    if not m:
        return None
    threshold = int(m.group(1))
    stat = m.group(2).strip()

    # Reject combined markets explicitly (e.g. "goals + assists").
    if "+" in stat or " and " in stat:
        return None

    # Order matters: longest/most-specific first.
    if "shots on target" in stat or stat in ("sot", "shot on target"):
        mt = MK_SOT
    elif stat.startswith("assist"):
        mt = MK_ASSISTS
    elif stat.startswith("goal"):
        mt = MK_GOALS
    elif stat.startswith("shot"):
        mt = MK_SHOTS
    else:
        return None
    return name, mt, threshold


# ---------------------------------------------------------------------------
# Join: model probs <-> live PM "Player Props" event
# ---------------------------------------------------------------------------

def join_fixture_to_pm(
    priced: Sequence[PropPrice],
    pm_event: Dict[str, Any],
    *,
    yes_quote_fn=None,
) -> List[PropEdgeRow]:
    """Join model :class:`PropPrice` rows to a live PM Player-Props event.

    ``pm_event`` is one event dict from
    :func:`wca.data.polymarket.find_world_cup_markets` /
    :func:`wca.data.polymarket._player_props_event` (its ``markets`` carry
    ``groupItemTitle`` + decoded ``clobTokenIds``/``outcomes``/``outcomePrices``).

    Player names are matched with the SAME normaliser the scorer-token resolver
    uses (:func:`wca.data.polymarket._norm_player`), falling back to the
    first-initial+surname key (:func:`wca.data.polymarket._player_key`) so the
    odds-feed spelling lines up with Polymarket's. Market type + threshold are
    parsed from the PM label by :func:`parse_pm_prop_label`.

    ``yes_quote_fn(market) -> {"token","ask",...} | None`` extracts the YES
    price+token; defaults to :func:`wca.testbook.trader.yes_quote`. Returns one
    :class:`PropEdgeRow` per (player, market, threshold) present in BOTH sides.
    """
    if yes_quote_fn is None:
        from wca.testbook.trader import yes_quote as yes_quote_fn  # noqa

    # Index model rows two ways: exact normalised name, and loose key.
    by_exact: Dict[Tuple[str, str, int], PropPrice] = {}
    by_key: Dict[Tuple[str, str, int], PropPrice] = {}
    for pr in priced:
        ek = (_norm_player(pr.player), pr.market_type, pr.threshold)
        by_exact[ek] = pr
        kk = _player_key(pr.player)
        if kk:
            by_key[(kk, pr.market_type, pr.threshold)] = pr

    rows: List[PropEdgeRow] = []
    for m in pm_event.get("markets") or []:
        label = (m.get("groupItemTitle") or m.get("question") or "")
        parsed = parse_pm_prop_label(label)
        if not parsed:
            continue
        pm_name, mt, thr = parsed

        pr = by_exact.get((_norm_player(pm_name), mt, thr))
        match_kind = "exact"
        if pr is None:
            kk = _player_key(pm_name)
            if kk:
                pr = by_key.get((kk, mt, thr))
                match_kind = "key"
        if pr is None:
            continue

        quote = yes_quote_fn(m)
        if not quote:
            continue
        ask = quote.get("ask")
        token = quote.get("token")
        if ask is None or token is None or not (0.0 < float(ask) < 1.0):
            continue
        pm_price = float(ask)
        rows.append(PropEdgeRow(
            player=pr.player, team=pr.team, market_type=mt, threshold=thr,
            model_prob=pr.prob, pm_price=pm_price,
            edge=pr.prob - pm_price, token_id=str(token),
            fair_odds=pr.fair_odds, rate_source=pr.rate_source,
            minutes_source=pr.minutes_source, pm_label=label,
            match_kind=match_kind))
    return rows


# ---------------------------------------------------------------------------
# Rate sourcing helper (players.db via betbuilder.RateStore -> PlayerPropRates)
# ---------------------------------------------------------------------------

def rates_from_players_db(
    team: str,
    player: str,
    *,
    db_path: Optional[str] = None,
    store: Any = None,
    expected_minutes: float = 90.0,
) -> Optional[PlayerPropRates]:
    """Build :class:`PlayerPropRates` from ``players.db`` for one player.

    Reuses :class:`wca.models.betbuilder.RateStore` (the existing loader that
    degrades to priors when the DB is absent). Returns ``None`` when the DB
    has no real per-90 numbers for this (team, player) so the caller can fall
    back to the share path. ``store`` may be a pre-built RateStore to avoid
    re-opening the DB per player.
    """
    from wca.models.betbuilder import RateStore

    rs = store if store is not None else RateStore(db_path)
    pr = rs.player(team, player)
    # betbuilder.PlayerRate exposes sot_p90 (+ goals via npxg_share). It does
    # NOT carry shots_p90/goals_p90/assists_p90 directly, so we only lift what
    # the schema provides today (sot_p90); the rest cascade from the share.
    sot = getattr(pr, "sot_p90", None)
    if sot is None:
        return None
    return PlayerPropRates(
        player=player, team=team, sot_p90=sot,
        expected_minutes=expected_minutes,
        # We don't know the underlying minutes here; treat as moderate evidence
        # unless the caller knows better. Conservative default = light shrink.
        sample_minutes=getattr(pr, "sample_minutes", 90.0) or 90.0,
        rate_source=getattr(pr, "source", "players.db") or "players.db",
        minutes_source="assumed_90")
