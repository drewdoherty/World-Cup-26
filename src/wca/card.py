"""Matchday card generator.

Ties the whole pipeline together for a slate of upcoming fixtures:

1.  Fit international Elo (rating + ordered-logit outcome model) and a
    time-decayed Dixon-Coles model on the full results history.
2.  For each fixture in an odds frame, de-vig every book's 1X2 prices with
    Shin and aggregate to a market-consensus fair probability (the baseline).
3.  Blend Elo, Dixon-Coles and the market baseline into a single probability
    per outcome.
4.  Take the *best* (max) decimal price available across books per outcome and
    compute the edge/EV of backing it at the blended probability.
5.  Size a quarter-Kelly stake per pool, capped per-bet, and emit a card.

Design notes
------------
The blend deliberately anchors a large weight on the de-vigged market because
the market is hard to beat; the model's job is to flag where the *best
available price* (after line-shopping across books) diverges enough from the
consensus to clear the vig. Blend weights are pre-backtest priors and are
documented as such — they are not yet fitted (that needs the calibration
backtest, deferred). Until then they are conservative on purpose.

This module produces *recommendations only*; nothing here places a bet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from wca.data.teamnames import canonical
from wca.markets import devig as devig_mod
from wca.markets import kelly as kelly_mod
from wca.models import venues as venues_mod
from wca.models.dixon_coles import DixonColesModel
from wca.models.elo import EloOutcomeModel, EloRater
from wca.models.props import CardsModel, CornersModel
from wca.models.scores import ScorelineCard, scoreline_card

# 1X2 outcome order used throughout: home, draw, away.
OUTCOMES = ("home", "draw", "away")

# 2026 is a neutral-venue, three-co-host tournament, not a single-country home
# tournament. Use half of the classic 100 Elo-point home edge for a host's own
# neutral group fixtures: it is large enough to price crowd/travel familiarity,
# but avoids treating every co-host match as a full domestic home international.
DEFAULT_NEUTRAL_HOST_FACTOR = 0.5

# Map a one-unit Dixon-Coles log-goal strength prior to Elo points. The default
# structural DC prior is 0.15 at one standard deviation, so this yields a modest
# 60-point Elo seed at 1 sd: informative for cold-start teams, quickly swamped by
# match history for established teams.
DEFAULT_ELO_POINTS_PER_DC_PRIOR = 400.0

# The 2026 logged-results grid currently chooses a very conservative 0.05x
# multiplier on the World Football Elo K ladder. Keep it reported as an
# opt-in calibration knob; the default remains the established ladder because
# the logged sample is still only 31 matches and tiny samples can invert the
# ordered-logit slope.
LOGGED_RESULTS_ELO_K_SCALE = 0.05
DEFAULT_ELO_K_SCALE = 1.0


def _elo_initial_ratings_from_dc_prior(
    *,
    prior_scale: float,
    points_per_dc_prior: float = DEFAULT_ELO_POINTS_PER_DC_PRIOR,
    base_rating: float = 1500.0,
) -> Dict[str, float]:
    """Convert the Dixon-Coles structural prior into per-team Elo seeds."""
    from wca.models.structural import dc_priors_from_factors

    seed_atk, seed_dfc = dc_priors_from_factors(scale=prior_scale)
    teams = set(seed_atk) | set(seed_dfc)
    out: Dict[str, float] = {}
    for team in teams:
        prior = 0.5 * (
            float(seed_atk.get(team, 0.0)) + float(seed_dfc.get(team, 0.0))
        )
        out[team] = float(base_rating) + float(points_per_dc_prior) * prior
    return out


@dataclass
class BlendWeights:
    """Convex weights over (Elo, Dixon-Coles, market). Must sum to 1.

    Defaults are **0.25 / 0.25 / 0.50** and were *kept* after the blend backtest
    (see ``backtests/`` and ``docs/research/backtests/``). The fitted blend does
    NOT beat the de-vigged market with confidence: delta mean -0.0031 nats,
    95% CI [-0.0224, +0.0155], P(fitted beats market)=60.2% on n=64. The
    deployed weights are statistically indistinguishable from both the fitted
    blend and market-only (all within ~0.003 nats, heavily overlapping bootstrap
    CIs), so there is no decision-grade reason to change them.

    **2026-06-18 update — the pre-registered conservative DC>Elo move was taken.**
    With the WC2026 group stage now providing live evidence (24 played matches),
    the deployed weights shifted to **0.10 / 0.30 / 0.60**. Drivers: Step 1 LOTO
    and Step 3 both favour DC (fitted w_elo=0.00); a non-leaky pre-tournament
    diagnostic on the 24 matches showed Elo as the worst component, and
    re-blending the 16 logged matches improved Brier from 0.534 (0.25/0.25/0.50)
    to 0.527 here. This is the *conservative* move the prior analysis
    pre-registered (shift Elo→DC, nudge market up). We deliberately did NOT zero
    Elo or adopt the raw single-tournament fit (0.00/0.32/0.68) — that over-fits
    one World Cup. NOTE: this does NOT fix the model's draw under-prediction
    (~14pp on the 24 matches) — that is a Dixon-Coles draw-mass issue, not a
    blend-weight one, and inflating draws on 24 group-stage games risks
    over-fitting (knockouts draw far less). Revisit with more data; do not force.
    """

    elo: float = 0.10
    dc: float = 0.30
    market: float = 0.60

    def normalised(self) -> "BlendWeights":
        s = self.elo + self.dc + self.market
        if s <= 0:
            raise ValueError("blend weights must sum to a positive number")
        return BlendWeights(self.elo / s, self.dc / s, self.market / s)


@dataclass
class PoolConfig:
    """A bankroll pool with its own Kelly sizing parameters."""

    name: str
    bankroll: float
    currency: str = "GBP"
    kelly_fraction: float = 0.25
    per_bet_cap: float = 0.05
    daily_exposure_cap: float = 0.05

    @property
    def symbol(self) -> str:
        """Currency symbol for display ($ for the USD/Polymarket pool, else £)."""
        return "$" if str(self.currency).upper() == "USD" else "£"


# ---------------------------------------------------------------------------
# Dual-pool sizing (user decision, 2026-06-28).
# ---------------------------------------------------------------------------
#
# The desk's bankroll is split equally across its two books and sized at
# HALF-Kelly (the user chose the aggressive 1/2 over 1/4 after re-exploring
# both; per-bet + whole-book caps stay on as the guardrails):
#
#   gbp pool  ->  £1,500   sportsbooks + exchanges (Betfair / Smarkets / bet365…)
#   pm  pool  ->  $1,995   Polymarket balance  (£1,500 at the fixed £1 = $1.33)
#
# £1,500 + $1,995 ≈ £3,000 total, equally shared. Each book is sized in its OWN
# currency off its OWN bankroll — Polymarket stakes come out in $ natively off
# the $1,995 pool (the 1.33 is baked into that figure), every other venue in £.
# This REPLACES the CLV-earned-rung ladder as the stake-sizing base; the ladder
# constants below are retained only for the informational bankroll footer.
GBP_POOL_BANKROLL: float = 1500.0
PM_POOL_BANKROLL: float = 1995.0
DUAL_POOL_KELLY_FRACTION: float = 0.50
GBP_POOL_NAME = "gbp"
PM_POOL_NAME = "pm"


def default_pools() -> List["PoolConfig"]:
    """The canonical dual pool: £1,500 GBP venues + $1,995 Polymarket, 1/2-Kelly."""
    return [
        PoolConfig(
            name=GBP_POOL_NAME, bankroll=GBP_POOL_BANKROLL, currency="GBP",
            kelly_fraction=DUAL_POOL_KELLY_FRACTION,
        ),
        PoolConfig(
            name=PM_POOL_NAME, bankroll=PM_POOL_BANKROLL, currency="USD",
            kelly_fraction=DUAL_POOL_KELLY_FRACTION,
        ),
    ]


def pool_for_venue(
    venue: str, pools: Sequence["PoolConfig"]
) -> "PoolConfig":
    """Route a recommendation to its currency pool.

    Polymarket -> the USD (``pm``) pool; every sportsbook / exchange -> the GBP
    (``gbp``) pool. Falls back gracefully to the first pool if a named pool is
    absent (e.g. a single-pool legacy caller), so callers never KeyError.
    """
    pm = next((p for p in pools if p.name == PM_POOL_NAME), None)
    gbp = next((p for p in pools if p.name == GBP_POOL_NAME), None)
    if str(venue) == VENUE_POLYMARKET:
        return pm or gbp or pools[0]
    return gbp or pools[0]


# ---------------------------------------------------------------------------
# CLV-gated bankroll ladder (governance wiring for the sportsbook pool).
# ---------------------------------------------------------------------------

# Deployable sportsbook-pool bankroll for each rung of the pre-registered Kelly
# ladder (``wca.markets.kelly.KellyPolicy``). The ladder's rung *index* —
# earned by settled-with-close bet count AND positive to-date CLV — selects
# which pool the desk is cleared to deploy. Bankroll governance (user, 2026-06-26):
#
#   rung 0  ->  £2,000   (base; raised from £1,500 by user instruction
#                         2026-06-26 — deploy more now while the edge is being
#                         proven; PROMOTION beyond it stays CLV-gated)
#   rung 1  ->  £3,000   (50+ settled-with-close AND to-date CLV > 0 — once the
#                         CLV evidence backs it, deploy the FULL £3,000 capital)
#   rung 2  ->  £3,000   (100+ settled AND CLV > 0; capped at actual capital —
#                         we never size off more cash than the desk holds)
#
# Kelly is a flat 1/4 at EVERY rung (no rung-0 shrink): the rung scales the
# bankroll, not the Kelly fraction. Demotion (rolling-50 CLV < 0) steps the index
# back down and the bankroll with it. The *kill rule* (pause real money if avg
# CLV < 0 after ~50 bets) is the desk's jurisdiction, not encoded here.
LADDER_BANKROLLS: Tuple[float, ...] = (2000.0, 3000.0, 3000.0)

# Flat fractional-Kelly multiplier applied at EVERY rung (user, 2026-06-26). The
# rung scales the deployable bankroll, NOT the Kelly fraction, so we deliberately
# override the KellyPolicy ladder's escalating 0.25/0.35/0.50 with a constant
# quarter-Kelly. (The ladder is still used to pick the rung -> bankroll.)
FLAT_KELLY_FRACTION: float = 0.25

# ---------------------------------------------------------------------------
# Operating-rules constants (encoded card governance — 2026-06-26).
# ---------------------------------------------------------------------------

# Total actual capital, UNPARTITIONED, held as £/$ across Smarkets (£),
# Betfair (£) and Polymarket ($/USDC). This is the real cash the desk holds; the
# *sizing base* is the CLV-earned ladder RUNG (above), NOT cash-on-hand. The two
# are reported side-by-side in the card footer so the gap between "what we hold"
# and "what the evidence clears us to deploy" is always explicit. Passed as an
# INPUT to the resolver (default here is the documented figure, never sized off).
#
# BANKROLL-FUNGIBILITY RULE (user, 2026-06-30) — applies to ALL sizing, here and
# anywhere else stakes are computed (see also wca.exposure_corr.DEFAULT_BANKROLL
# and docs/policy/bankroll.md):
#   Capital moves FREELY across accounts, venues and currencies (£<->$). The
#   bankroll is ALWAYS this combined effective pool (~£3,000), NEVER the balance
#   sitting in any one account, venue, app or wallet. A balance shown in a single
#   book — e.g. a "£276.57" sportsbook-app wallet on a bet slip — is a liquidity/
#   routing detail (top-ups and transfers are assumed frictionless), NOT a sizing
#   cap. Size off the combined pool, then route the stake to whichever venue holds
#   the bet and move funds to cover it. Do NOT shrink a stake because one wallet
#   looks light.
DEFAULT_ACTUAL_CAPITAL_GBP: float = 3000.0

# Staking is a flat QUARTER-KELLY at every rung (user, 2026-06-26): there is no
# rung-0 shrink. The rung scales the deployable bankroll (£2,000 -> £3,000), not
# the Kelly fraction — the CLV ladder governs HOW MUCH capital is in play, while
# the 1/4 multiplier stays constant. (The kill rule — pause real money if avg CLV
# < 0 after ~50 bets — remains a separate desk call, not encoded here.)

# Selection-rule thresholds (memory: feedback-likely-pnl-no-minnows). HIT
# PROBABILITY is the primary sort; EV stays a gate, not the ranker.
SELECTION_MIN_PROB: float = 0.20          # hard floor: below this we never STAKE
LONGSHOT_PROB: float = 0.25               # below this an outright-underdog is a
#                                           "mispriced minnow" longshot — cut even
#                                           when +EV (they lose ~90% of the time).
STRUCTURAL_DRAW_BAND: Tuple[float, float] = (0.25, 0.32)  # draws we *want*.

# Coherence guard: a complete 1X2 book's implied probabilities (sum of 1/odds)
# always overround to >= 1.0; anything materially below is an impossible book
# (cross-market contamination) and is dropped in _index_odds.
MIN_COHERENT_BOOK_IMPLIED_SUM: float = 0.98

# Single-source policy (user, 2026-06-29): when an outcome is priced by FEWER
# than this many distinct books, there is no cross-venue confirmation, so the
# pick is flagged "indicative" and NOT auto-staked. Set WCA_STAKE_SINGLE_SOURCE
# truthy to override and size single-source picks anyway.
MIN_BOOKS_FOR_STAKING: int = 2

# Further-out tilt. Markets are softest furthest from kickoff; large edges on
# imminent fixtures are more likely model error than true mispricing.
IMMINENT_HOURS: float = 6.0               # < this to kickoff = "imminent".
IMMINENT_EDGE_DISCOUNT: float = 0.5       # halve the modelled edge on imminent
#                                           fixtures before sizing (flag, don't
#                                           size the full gap).
FURTHER_OUT_HOURS: float = 24.0           # >= this = "further out" → prioritised.

# Reference-only match-event lines surfaced on the card (DISPLAY ONLY, never
# staked). Half-integer so no continuity correction is needed in the NB models.
# Corners 8.5 matches the next-match preview default (DEFAULT_CORNERS_LINE);
# cards 3.5 straddles the calibrated WC base rate (~3.41 total cards/match).
DEFAULT_EVENT_CORNERS_LINE: float = 8.5
DEFAULT_EVENT_CARDS_LINE: float = 3.5

# Cross-venue. The odds-source orchestrator tags every price with a
# ``bookmaker_key``; these are the three venues the desk actually deploys into.
VENUE_SMARKETS = "smarkets"
VENUE_BETFAIR = "betfair"
VENUE_POLYMARKET = "polymarket"


@dataclass
class PoolBankroll:
    """Resolved sizing base and the evidence that earned it.

    ``bankroll`` is the *sizing base* the desk is cleared to deploy off — the
    pool the CLV ladder clears (£2,000 at rung 0, £3,000 once CLV is proven; see
    :func:`resolve_pool_bankroll`). ``kelly_fraction`` is a flat 1/4 at every
    rung. ``reason`` is a one-line human-readable explanation for the card footer.

    The new fields make the bankroll model explicit and unpartitioned:

    * ``actual_capital`` — the real cash the desk holds (£3,000), held as £/$
      across venues; an INPUT, never the sizing base.
    * ``venue_balances`` — per-venue available £/$ (Smarkets £, Betfair £,
      Polymarket $), also an INPUT — used to split the recommended deployment,
      not to size it.
    * ``constrained`` — retained for the footer/feed schema; always ``False``
      now that staking is a flat 1/4-Kelly with no rung-0 shrink.
    * ``constraint_note`` — empty under the flat-Kelly policy.
    """

    bankroll: float
    rung: int
    kelly_fraction: float
    reason: str
    n_settled: int
    clv_to_date: Optional[float]
    actual_capital: float = DEFAULT_ACTUAL_CAPITAL_GBP
    venue_balances: Dict[str, float] = field(default_factory=dict)
    constrained: bool = False
    constraint_note: str = ""


def resolve_pool_bankroll(
    db_path: str,
    policy: Optional["kelly_mod.KellyPolicy"] = None,
    bankrolls: Sequence[float] = LADDER_BANKROLLS,
    override: Optional[float] = None,
    currency_symbol: str = "£",
    actual_capital: float = DEFAULT_ACTUAL_CAPITAL_GBP,
    venue_balances: Optional[Dict[str, float]] = None,
) -> PoolBankroll:
    """Resolve the *sizing base* from ledger CLV via the Kelly ladder.

    Reads the settled-with-close CLV statistics from the ledger
    (``wca.ledger.reports.staking_stats``), runs the pre-registered
    :class:`~wca.markets.kelly.KellyPolicy` ladder to find the earned rung, and
    maps that rung index onto the governance bankroll ladder
    (:data:`LADDER_BANKROLLS`: £2000 / £3000 / £3000).

    The rung is *earned* by evidence, never by time or a hot streak: rung 1
    needs 50+ settled-with-close bets and positive to-date CLV; rung 2 needs
    100+ and positive CLV; a negative rolling-50 CLV demotes one rung. See the
    policy's docstring for the full rules.

    Bankroll model (operating rule 1)
    ---------------------------------
    The sizing base is the **CLV-earned rung, NOT cash-on-hand**. ``actual_capital``
    (£3,000, unpartitioned, held as £/$ across Smarkets/Betfair/Polymarket) and
    ``venue_balances`` are *inputs* reported in the footer, never the sizing base.

    **Flat quarter-Kelly (user, 2026-06-26).** Staking is 1/4-Kelly at EVERY
    rung — there is no rung-0 shrink. The CLV ladder governs the deployable
    bankroll (rung 0 = £2,000 now; rung 1 = £3,000 once 50+ settled with
    positive CLV — the full capital), while the Kelly fraction stays 1/4. The
    kill rule (pause real money if avg CLV < 0 after ~50 bets) is a separate
    desk call, not a silent fractional shrink. ``actual_capital`` (£3,000) and
    ``venue_balances`` remain footer *inputs*, and a manual ``override`` still
    sets the base verbatim while the ladder rung is reported alongside.

    Parameters
    ----------
    db_path:
        SQLite ledger path.
    policy:
        The Kelly ladder to apply. Defaults to a fresh
        :class:`~wca.markets.kelly.KellyPolicy` (the pre-registered ladder).
    bankrolls:
        Notional pool per rung, index-aligned with ``policy.rungs``. Defaults
        to the governance ladder £2000 / £3000 / £3000.
    override:
        If not ``None``, use this bankroll verbatim (the ``--bankroll`` CLI
        override). The ledger is still read so the card can report the rung the
        evidence *would* have earned alongside the manual figure.
    currency_symbol:
        Symbol used in the ``reason`` string (display only).
    actual_capital:
        Real unpartitioned capital the desk holds (INPUT, default £3,000).
    venue_balances:
        Per-venue available £/$ (INPUT) used to split deployment, not to size.

    Returns
    -------
    PoolBankroll
        The resolved sizing base plus the rung, Kelly fraction, constraint flag
        and one-line reasons for the card footer.
    """
    from wca.ledger.reports import staking_stats

    if policy is None:
        policy = kelly_mod.KellyPolicy()

    if len(bankrolls) != len(policy.rungs):
        raise ValueError(
            "bankrolls (%d) must align one-to-one with policy.rungs (%d)"
            % (len(bankrolls), len(policy.rungs))
        )

    venue_balances = dict(venue_balances or {})

    stats = staking_stats(db_path)
    n_settled = int(stats["n_settled"])
    clv_to_date = stats["clv_to_date"]
    rolling50 = stats["rolling50_clv"]

    fraction, rung, _policy_reason = policy.evaluate(
        n_settled=n_settled,
        clv_to_date=clv_to_date,
        rolling50_clv=rolling50,
    )

    ladder_bankroll = float(bankrolls[rung])

    # Flat quarter-Kelly at every rung (user, 2026-06-26): NO rung-0 shrink. The
    # CLV ladder scales the deployable bankroll (£2,000 -> £3,000); the Kelly
    # fraction stays 1/4 throughout. The kill rule (pause if avg CLV < 0 after
    # ~50 bets) is a separate desk call, not a silent fractional shrink here.
    constrained = False
    constraint_note = ""
    constrained_base = ladder_bankroll
    constrained_fraction = FLAT_KELLY_FRACTION  # flat 1/4 at every rung

    # Threshold of the *next* rung, for the "X/Y settled" progress hint.
    if rung + 1 < len(policy.rungs):
        next_threshold = policy.rungs[rung + 1].min_settled
    else:
        next_threshold = policy.rungs[rung].min_settled

    clv_str = ("%+.4f" % clv_to_date) if clv_to_date is not None else "n/a"

    if override is not None:
        bankroll = float(override)
        out_fraction = FLAT_KELLY_FRACTION
        reason = (
            "%s%.0f (manual override) — ladder would set rung %d "
            "(%s%.0f) from %d/%d settled-with-close bets, CLV %s%s"
            % (
                currency_symbol, bankroll, rung, currency_symbol,
                ladder_bankroll, n_settled, next_threshold, clv_str,
                ("; %s" % constraint_note) if constrained else "",
            )
        )
    else:
        bankroll = constrained_base
        out_fraction = constrained_fraction
        reason = (
            "rung %d sizing-base %s%.0f (notional pool %s%.0f) — %d/%d "
            "settled-with-close bets, CLV %s, Kelly fraction %.2f"
            % (
                rung, currency_symbol, bankroll, currency_symbol,
                ladder_bankroll, n_settled, next_threshold, clv_str,
                out_fraction,
            )
        )

    return PoolBankroll(
        bankroll=bankroll,
        rung=rung,
        kelly_fraction=out_fraction,
        reason=reason,
        actual_capital=float(actual_capital),
        venue_balances=venue_balances,
        constrained=constrained,
        constraint_note=constraint_note,
        n_settled=n_settled,
        clv_to_date=clv_to_date,
    )


@dataclass
class Recommendation:
    match_id: str
    match_desc: str
    commence_time: str
    selection: str  # home/draw/away
    selection_team: str
    best_book: str
    best_odds: float
    model_prob: float
    market_prob: float
    elo_prob: float
    dc_prob: float
    edge: float
    ev_per_unit: float
    stakes: Dict[str, float] = field(default_factory=dict)  # pool name -> stake
    # --- operating-rules fields (rules 2/3/4) -----------------------------
    venue: str = ""                       # cross-venue tag (best price's source)
    raw_edge: Optional[float] = None      # pre-time-tilt edge (rule 3 audit)
    hours_to_kickoff: Optional[float] = None
    imminent: bool = False                # < IMMINENT_HOURS to kickoff (rule 3)
    category: str = ""                    # favourite / second_favourite /
    #                                       structural_draw / longshot (rule 2)
    cut: bool = False                     # excluded from STAKED picks (rule 2)
    cut_reason: str = ""
    indicative: bool = False              # single-source price (no cross-venue
    #                                       confirmation) — shown, not auto-staked


@dataclass
class FittedModels:
    rater: EloRater
    elo_outcome: EloOutcomeModel
    dc: DixonColesModel
    n_matches: int


# ---------------------------------------------------------------------------
# Model fitting.
# ---------------------------------------------------------------------------


def _played(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with real (non-NA) integer scores, sorted by date."""
    d = df.copy()
    for col in ("home_score", "away_score"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["home_score", "away_score"])
    d["home_score"] = d["home_score"].astype(int)
    d["away_score"] = d["away_score"].astype(int)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date", kind="mergesort")
    if "neutral" in d.columns:
        d["neutral"] = d["neutral"].astype(bool)
    return d.reset_index(drop=True)


def fit_models(
    results: pd.DataFrame,
    half_life_years: float = 8.0,
    reference_date: Optional[str] = None,
    structural_prior: bool = False,
    structural_prior_scale: Optional[float] = None,
    elo_seed_from_dc_prior: bool = True,
    elo_prior_scale: Optional[float] = None,
    elo_points_per_dc_prior: float = DEFAULT_ELO_POINTS_PER_DC_PRIOR,
    elo_k_scale: float = DEFAULT_ELO_K_SCALE,
) -> FittedModels:
    """Fit Elo (rating + outcome) and Dixon-Coles on the results history.

    Dixon-Coles half-life
    ---------------------
    ``half_life_years`` defaults to **8.0**, deliberately *kept* after the
    half-life backtest (see ``backtests/`` and ``docs/research/backtests/``).
    The evidence does not support moving it:

    * DC-only: the pooled best is hl=4 (log-loss 0.9773) but it beats the
      deployed hl=8 (0.9789) by only **+0.0016 log-loss** — not decision-grade
      on ~211 holdout matches. Only 2 of 3 holdouts favour 4 over 8
      (deployed-minus-best per block: WC2018 +0.0028, WC2022 -0.0143,
      Euro2024+Copa2024 +0.0130); WC2022 strongly prefers *longer* memory (16).
    * Blend (the 50/50 Elo+DC mix the card actually deploys): the pooled best
      *is* hl=8 (0.9817); hl=4 only ties (0.9824 vs 0.9817).

    8.0 is a sensible compromise between the divergent per-tournament optima
    (2-4 for European-summer tournaments vs 16 for the anomalous Qatar winter
    WC), not a value that should move. Revisit only with a larger holdout
    (more tournaments / club-data augmentation).
    """
    played = _played(results)

    # -- Shared Dixon-Coles priors -----------------------------------------
    from wca.models.dixon_coles import xi_from_half_life

    # Structural shrinkage prior (opt-in, default off). When enabled, low-data
    # teams shrink toward a socio-economic estimate instead of the global mean.
    atk_prior = dfc_prior = None
    from wca.models.structural import DEFAULT_PRIOR_SCALE, dc_priors_from_factors

    scale = DEFAULT_PRIOR_SCALE if structural_prior_scale is None else structural_prior_scale
    if structural_prior:
        atk_prior, dfc_prior = dc_priors_from_factors(scale=scale)

    # Elo uses the same DC prior family as an initial-rating seed by default,
    # without forcing the Dixon-Coles likelihood itself to use structural
    # shrinkage. Missing teams retain the flat Elo default.
    elo_initial_ratings: Dict[str, float] = {}
    if elo_seed_from_dc_prior:
        seed_scale = scale if elo_prior_scale is None else elo_prior_scale
        elo_initial_ratings = _elo_initial_ratings_from_dc_prior(
            prior_scale=seed_scale,
            points_per_dc_prior=elo_points_per_dc_prior,
        )

    # -- Elo ratings --------------------------------------------------------
    k_factors = None
    if elo_k_scale != 1.0:
        from wca.models.elo import DEFAULT_K_FACTORS

        k_factors = {
            k: float(v) * float(elo_k_scale)
            for k, v in DEFAULT_K_FACTORS.items()
        }
    rater = EloRater(initial_ratings=elo_initial_ratings, k_factors=k_factors)
    out = rater.rate_matches(played, return_history=True)
    history = out["history"]

    # -- Elo ordered-logit outcome model -----------------------------------
    # Reconstruct the pre-match rating diff (with home advantage on non-neutral
    # venues) and the realised ordinal outcome for every historical match.
    diffs: List[float] = []
    outcomes: List[int] = []
    hist_scores = played[["home_score", "away_score"]].to_numpy()
    for rec, (hs, as_) in zip(history, hist_scores):
        adv = 0.0 if rec["neutral"] else rater.home_advantage
        diff = (rec["home_rating_pre"] + adv) - rec["away_rating_pre"]
        diffs.append(diff)
        outcomes.append(2 if hs > as_ else (1 if hs == as_ else 0))
    elo_outcome = EloOutcomeModel().fit(diffs, outcomes)

    # -- Dixon-Coles --------------------------------------------------------
    dc = DixonColesModel(
        xi=xi_from_half_life(half_life_years),
        attack_prior=atk_prior,
        defence_prior=dfc_prior,
    )
    dc.fit_dataframe(played, reference_date=reference_date)

    return FittedModels(rater=rater, elo_outcome=elo_outcome, dc=dc, n_matches=len(played))


# ---------------------------------------------------------------------------
# Per-fixture probabilities.
# ---------------------------------------------------------------------------


def elo_probs(
    models: FittedModels,
    home: str,
    away: str,
    neutral: bool,
    host: Optional[str] = None,
    host_points: Optional[float] = None,
) -> Tuple[float, float, float]:
    """Elo (home, draw, away) via the ordered-logit outcome model.

    ``host_points`` optionally overrides the host-bonus magnitude (venue-aware
    path); ``None`` keeps the legacy full ``home_advantage``.
    """
    diff = models.rater._rating_diff(
        home, away, neutral=neutral, host=host, host_points=host_points
    )
    return models.elo_outcome.predict_proba(diff)


def dc_probs(
    models: FittedModels, home: str, away: str, neutral: bool
) -> Tuple[float, float, float]:
    """Dixon-Coles (home, draw, away) from the scoreline matrix."""
    pred = models.dc.predict(home, away, neutral=neutral, warn=False)
    return pred.one_x_two()


def market_consensus(book_prices: Dict[str, Dict[str, float]]) -> Optional[np.ndarray]:
    """De-vig each complete book with Shin, return median fair (home,draw,away).

    ``book_prices`` maps book -> {home,draw,away: decimal_odds}. Books missing
    any of the three outcomes are skipped. Returns ``None`` if no book has a
    complete 1X2.
    """
    fair_rows: List[np.ndarray] = []
    for prices in book_prices.values():
        if not all(o in prices and prices[o] > 1.0 for o in OUTCOMES):
            continue
        odds = [prices[o] for o in OUTCOMES]
        try:
            fair_rows.append(devig_mod.shin(odds))
        except Exception:
            continue
    if not fair_rows:
        return None
    arr = np.vstack(fair_rows)
    med = np.median(arr, axis=0)
    return med / med.sum()  # renormalise after the per-column median


# ---------------------------------------------------------------------------
# Card construction.
# ---------------------------------------------------------------------------


def _index_odds(odds_df: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    """Group the flat odds frame into per-fixture h2h price books.

    Returns fixture_key -> {meta, books: {book: {home/draw/away: odds}}}.
    The Odds API h2h outcome names are the team names plus 'Draw'.

    Fixtures are keyed by the **canonical, order-independent team pair**, not the
    source ``event_id``: in best-price (union) mode the same real fixture arrives
    from several venues (Betfair, TheOddsAPI books, Polymarket) each with its own
    event_id and possibly opposite home/away orientation. Grouping by canonical
    pair merges every venue's book into ONE fixture so :func:`best_price` can
    line-shop across venues per outcome (instead of emitting a duplicate pick per
    venue). Each book's outcomes are re-slotted into the fixture's chosen
    home/away orientation by matching the canonical team name.
    """
    fixtures: Dict[str, Dict[str, object]] = {}
    h2h = odds_df[odds_df["market"] == "h2h"]
    # Stable per-pair grouping: the first time we see a pair fixes its display
    # orientation (home/away) and a representative event_id/commence.
    def _pair_key(home: object, away: object) -> str:
        a, b = sorted((canonical(str(home or "")), canonical(str(away or ""))))
        return "%s|%s" % (a, b)

    for (eid, home, away, commence), grp in h2h.groupby(
        ["event_id", "home_team", "away_team", "commence_time"], sort=False
    ):
        key = _pair_key(home, away)
        fx = fixtures.get(key)
        if fx is None:
            fx = {
                "event_id": str(eid),
                "home": str(home),
                "away": str(away),
                "commence_time": str(commence),
                "books": {},
            }
            fixtures[key] = fx
        # Re-slot this source's outcomes into the fixture's display orientation.
        canon_home = canonical(str(fx["home"]))
        canon_away = canonical(str(fx["away"]))
        books: Dict[str, Dict[str, float]] = fx["books"]  # type: ignore[assignment]
        for book, bgrp in grp.groupby("bookmaker_key"):
            prices = books.setdefault(str(book), {})
            for _, r in bgrp.iterrows():
                name = str(r["outcome_name"])
                try:
                    odd = float(r["decimal_odds"])
                except (TypeError, ValueError):
                    continue
                if name.lower() == "draw":
                    slot = "draw"
                elif canonical(name) == canon_home:
                    slot = "home"
                elif canonical(name) == canon_away:
                    slot = "away"
                else:
                    continue
                # Same book seen twice for an outcome (shouldn't happen across
                # one source) — keep the better price.
                if odd > prices.get(slot, 0.0):
                    prices[slot] = odd
            if not prices:
                books.pop(str(book), None)

    # Coherence guard (2026-06-29 defense-in-depth): drop any COMPLETE 1X2 book
    # whose implied probabilities sum to materially below 1.0 — an impossible
    # "sub-fair" book that only arises from merging prices across DIFFERENT
    # markets under one bookmaker_key (e.g. Polymarket's halftime / second-half
    # events collapsed together, each outcome taking the longest leg). A real
    # single market always overrounds to >= 1.0, so this never drops a genuine
    # book; it backstops the Polymarket parser filter so contamination can't
    # reach best_price / market_consensus even if a new ancillary market leaks.
    for fx in fixtures.values():
        fx_books: Dict[str, Dict[str, float]] = fx["books"]  # type: ignore[assignment]
        for book in list(fx_books.keys()):
            prices = fx_books[book]
            if all(o in prices and prices[o] > 1.0 for o in OUTCOMES):
                implied = sum(1.0 / prices[o] for o in OUTCOMES)
                if implied < MIN_COHERENT_BOOK_IMPLIED_SUM:
                    fx_books.pop(book, None)
    return fixtures


# Human venue labels for the bookmaker_key the odds feed carries. Unknown keys
# (e.g. synthetic test books) fall through unchanged so existing tests are
# unaffected and any new venue still renders *something* sensible.
VENUE_LABELS: Dict[str, str] = {
    "betfair_ex": "Betfair",
    "polymarket": "Polymarket",
    "smarkets": "Smarkets",
}

# Effective commission charged on net winnings per venue, used to fee-adjust the
# price before BOTH best-venue selection and edge/stake. Betfair Exchange takes
# a market-base commission on winnings (default 2%, overridable); Polymarket has
# no per-trade fee. Unknown books default to 0.0 so synthetic-book tests (and any
# already-net source) keep their raw odds and edges.
_DEFAULT_COMMISSION: Dict[str, float] = {
    "betfair_ex": 0.02,
    "polymarket": 0.0,
}


def venue_label(book: Optional[str]) -> str:
    """Map a bookmaker_key to a clean venue label for display."""
    if not book:
        return "—"
    return VENUE_LABELS.get(book, book)


def _commission(book: Optional[str]) -> float:
    """Resolve the net-winnings commission for a venue (env override wins).

    ``WCA_BETFAIR_COMMISSION`` / ``WCA_PM_FEE`` let the operator tune the real
    figure (Betfair's base rate varies by market and discount); everything else
    is fee-free by default.
    """
    if book == "betfair_ex":
        raw = os.environ.get("WCA_BETFAIR_COMMISSION", "").strip()
    elif book == "polymarket":
        raw = os.environ.get("WCA_PM_FEE", "").strip()
    else:
        raw = ""
    if raw:
        try:
            return max(0.0, min(0.5, float(raw)))
        except ValueError:
            pass
    return _DEFAULT_COMMISSION.get(book or "", 0.0)


def net_odds(book: Optional[str], gross: float) -> float:
    """Fee-adjusted decimal odds: payout net of the venue's winnings commission.

    A back at decimal ``gross`` returns ``gross-1`` profit per unit; commission
    ``c`` is taken on that profit, so the effective decimal is
    ``1 + (gross-1)*(1-c)``. Used for edge/stake so a nominally bigger price that
    is worse after fees does not win the best-price comparison.
    """
    if gross is None or gross <= 1.0:
        return gross
    return 1.0 + (gross - 1.0) * (1.0 - _commission(book))


def _stake_single_source() -> bool:
    """Whether to size picks whose only price is a single, unconfirmed book.

    Default OFF (user, 2026-06-29): a single-source price (e.g. Polymarket alone,
    no Betfair/exchange to confirm) is shown as 'indicative' and NOT staked.
    ``WCA_STAKE_SINGLE_SOURCE`` truthy overrides for when the lone book is deep.
    """
    return os.environ.get("WCA_STAKE_SINGLE_SOURCE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def books_pricing(books: Dict[str, Dict[str, float]], outcome: str) -> int:
    """Number of distinct books offering a usable price for ``outcome``."""
    return sum(1 for prices in books.values() if prices.get(outcome, 0.0) > 1.0)


def best_price(books: Dict[str, Dict[str, float]], outcome: str) -> Tuple[Optional[str], float]:
    """Best decimal odds for an outcome across books, with the book name.

    Selection is by **fee-adjusted** odds (so a soft venue with a higher gross
    price but worse net payout cannot win), but the returned price is the
    **gross** decimal — the number actually shown/backed at that venue.
    """
    best_book, best_net, best_gross = None, 0.0, 0.0
    for book, prices in books.items():
        o = prices.get(outcome)
        if o is None or o <= 1.0:
            continue
        net = net_odds(book, o)
        if net > best_net:
            best_net, best_gross, best_book = net, o, book
    return best_book, best_gross


@dataclass
class _FixtureBlend:
    """Per-fixture blend state shared by the recommendation and score pipelines."""

    fx: Dict[str, object]
    home: str  # canonical
    away: str  # canonical
    neutral: bool
    host: Optional[str]
    books: Dict[str, Dict[str, float]]
    blended: Dict[str, float]  # home/draw/away
    elo_map: Dict[str, float]
    dc_map: Dict[str, float]
    mkt_map: Dict[str, float]


def _meta_lookup(
    fixtures_meta: Optional[pd.DataFrame],
) -> Dict[Tuple[str, str], Dict[str, object]]:
    """Build the neutral/host lookup keyed by canonical team pair.

    Prefer *unplayed* (scheduled) rows so a historical friendly between the same
    two teams can't overwrite the World Cup fixture's neutral/host flags.
    """
    meta_lookup: Dict[Tuple[str, str], Dict[str, object]] = {}
    if fixtures_meta is not None and not fixtures_meta.empty:
        fm = fixtures_meta.copy()
        if "home_score" in fm.columns:
            scores = pd.to_numeric(fm["home_score"], errors="coerce")
            fm = pd.concat([fm[scores.notna()], fm[scores.isna()]])  # played first, scheduled last (wins)
        for _, r in fm.iterrows():
            meta_lookup[(str(r["home_team"]), str(r["away_team"]))] = {
                "neutral": bool(r["neutral"]) if "neutral" in r else True,
                "country": str(r.get("country", "")),
            }
    return meta_lookup


def _iter_fixture_blends(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights,
    host_nations: Sequence[str],
    neutral_host_factor: float = DEFAULT_NEUTRAL_HOST_FACTOR,
) -> List[_FixtureBlend]:
    """Compute the blended 1X2 for every fixture with a usable market.

    Shared by :func:`build_card` and :func:`build_score_cards` so both pipelines
    bet against the *same* blended probabilities. Fixtures without a complete
    market consensus are skipped (no blend is well-defined).
    """
    w = weights.normalised()
    fixtures = _index_odds(odds_df)
    meta_lookup = _meta_lookup(fixtures_meta)

    out: List[_FixtureBlend] = []
    for fx in fixtures.values():
        # Display names come from the odds feed; model/meta lookups MUST use the
        # canonical results-dataset spelling or they fall back to default
        # ratings and emit garbage edges.
        home_disp, away_disp = fx["home"], fx["away"]
        home, away = canonical(home_disp), canonical(away_disp)
        books = fx["books"]  # type: ignore[assignment]

        meta = meta_lookup.get((home, away), {"neutral": True, "country": ""})
        neutral = bool(meta["neutral"])
        host = None
        country = str(meta.get("country", ""))
        if neutral and country in host_nations:
            if home in host_nations:
                host = home
            elif away in host_nations:
                host = away
        elif not neutral:
            host = home  # genuine home team

        host_points = None
        if neutral and host is not None:
            host_points = venues_mod.host_advantage_points(
                models.rater.home_advantage,
                factor=neutral_host_factor,
            )

        e_h, e_d, e_a = elo_probs(
            models, home, away, neutral=neutral, host=host, host_points=host_points
        )
        d_h, d_d, d_a = dc_probs(models, home, away, neutral=neutral)
        mkt = market_consensus(books)  # type: ignore[arg-type]
        if mkt is None:
            continue
        m_h, m_d, m_a = float(mkt[0]), float(mkt[1]), float(mkt[2])

        blended = {
            "home": w.elo * e_h + w.dc * d_h + w.market * m_h,
            "draw": w.elo * e_d + w.dc * d_d + w.market * m_d,
            "away": w.elo * e_a + w.dc * d_a + w.market * m_a,
        }
        out.append(
            _FixtureBlend(
                fx=fx,
                home=home,
                away=away,
                neutral=neutral,
                host=host,
                books=books,  # type: ignore[arg-type]
                blended=blended,
                elo_map={"home": e_h, "draw": e_d, "away": e_a},
                dc_map={"home": d_h, "draw": d_d, "away": d_a},
                mkt_map={"home": m_h, "draw": m_d, "away": m_a},
            )
        )
    return out


def fixture_blends(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    neutral_host_factor: float = DEFAULT_NEUTRAL_HOST_FACTOR,
) -> List[_FixtureBlend]:
    """Public wrapper over :func:`_iter_fixture_blends` for persistence.

    Lets callers (e.g. the card CLI dumping ``data/model_predictions.json``)
    reuse the exact blended 1X2 the card bets against without reaching into a
    private helper.
    """
    return _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations, neutral_host_factor
    )


# ---------------------------------------------------------------------------
# Operating-rules helpers (rules 2/3/4) — selection floor, time tilt, venue.
# ---------------------------------------------------------------------------


def _parse_kickoff(value: object) -> Optional["pd.Timestamp"]:
    """Parse an ISO-ish kickoff string to an aware-UTC pandas Timestamp."""
    if value is None:
        return None
    ts = pd.to_datetime(str(value), errors="coerce", utc=True)
    return None if pd.isna(ts) else ts


def hours_to_kickoff(
    commence_time: object, now: Optional[object] = None
) -> Optional[float]:
    """Hours from ``now`` (default: real UTC) until ``commence_time``.

    Returns ``None`` when the kickoff is unparseable. Negative values mean the
    fixture has already started (treated as imminent by the tilt logic).
    """
    ko = _parse_kickoff(commence_time)
    if ko is None:
        return None
    if now is None:
        now_ts = pd.Timestamp.utcnow()
    else:
        now_ts = pd.to_datetime(str(now), errors="coerce", utc=True)
        if pd.isna(now_ts):
            now_ts = pd.Timestamp.utcnow()
    return float((ko - now_ts).total_seconds()) / 3600.0


def venue_of(book: str) -> str:
    """Normalise a ``bookmaker_key`` to one of the three deployment venues.

    The odds-source orchestrator tags Betfair/Polymarket rows with those source
    names and TheOddsAPI rows with the individual book key (e.g. ``smarkets``,
    ``betfair_ex_uk``). Anything that isn't recognisably Betfair or Polymarket
    is routed to Smarkets — the desk's default exchange — so every staked pick
    carries a concrete venue tag.
    """
    b = (book or "").strip().lower()
    if "polymarket" in b or b == "pm":
        return VENUE_POLYMARKET
    if "betfair" in b:
        return VENUE_BETFAIR
    return VENUE_SMARKETS


def classify_outcome(
    selection: str, model_prob: float, market_map: Dict[str, float]
) -> str:
    """Bucket an outcome for the selection rule (rule 2).

    * ``structural_draw`` — a draw whose model probability sits in the
      :data:`STRUCTURAL_DRAW_BAND` (~25-32%): the kind of draw the desk *wants*.
    * ``favourite`` — the market favourite of the three 1X2 outcomes.
    * ``second_favourite`` — the second-shortest of the three.
    * ``longshot`` — the outright underdog (market outsider). These are the
      "mispriced minnows" the feedback rule de-prioritises even when +EV.
    """
    if selection == "draw" and STRUCTURAL_DRAW_BAND[0] <= model_prob <= STRUCTURAL_DRAW_BAND[1]:
        return "structural_draw"
    # Rank outcomes by *market* probability (highest = favourite).
    ranked = sorted(OUTCOMES, key=lambda o: market_map.get(o, 0.0), reverse=True)
    if selection == ranked[0]:
        return "favourite"
    if selection == ranked[1]:
        return "second_favourite"
    return "longshot"


# Category sort priority: HIT PROBABILITY first, EV only as a gate (rule 2).
# Lower number = ranked higher.
_CATEGORY_PRIORITY = {
    "favourite": 0,
    "structural_draw": 1,
    "second_favourite": 2,
    "longshot": 3,
}


def build_card(
    models: FittedModels,
    odds_df: pd.DataFrame,
    pools: Sequence[PoolConfig],
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    neutral_host_factor: float = DEFAULT_NEUTRAL_HOST_FACTOR,
    now: Optional[object] = None,
) -> List[Recommendation]:
    """Generate +EV recommendations for every outcome in the slate.

    This is the **gating** layer: it emits one :class:`Recommendation` per
    outcome whose time-tilted edge clears ``min_edge``, with the operating-rules
    metadata populated (venue tag, raw vs tilted edge, hours-to-kickoff,
    selection category). It is edge-sorted for backward compatibility; the
    *operating-rules ranking and longshot cut* are applied separately by
    :func:`rank_card` (rule 2/3), which the card CLI calls.

    Parameters
    ----------
    models, odds_df, pools, fixtures_meta, weights, min_edge, host_nations,
    neutral_host_factor:
        As before.
    now:
        Reference time (ISO-8601 or Timestamp) for the further-out tilt (rule 3).
        ``None`` uses real UTC. When kickoff is unparseable the tilt is skipped
        (edge unchanged) so the function never crashes on a bad timestamp.
    """
    blends = _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations, neutral_host_factor
    )

    recs: List[Recommendation] = []
    for fb in blends:
        home, away = fb.home, fb.away
        team_map = {"home": home, "draw": "Draw", "away": away}
        commence = str(fb.fx["commence_time"])
        h2k = hours_to_kickoff(commence, now=now)
        imminent = h2k is not None and h2k < IMMINENT_HOURS

        for outcome in OUTCOMES:
            book, odds = best_price(fb.books, outcome)
            if book is None or odds <= 1.0:
                continue
            p = fb.blended[outcome]
            # Edge and sizing use the fee-adjusted (net) price of the chosen
            # venue; the displayed best_odds stays gross (the screen price).
            net = net_odds(book, odds)
            raw_e = kelly_mod.edge(p, net)
            # Further-out tilt (rule 3): markets are softest furthest from
            # kickoff. A large model-vs-market edge on an IMMINENT fixture is
            # more likely model error than true mispricing, so DISCOUNT the edge
            # before it gates / sizes (flag, don't size the full gap).
            e = raw_e * IMMINENT_EDGE_DISCOUNT if (imminent and raw_e > 0) else raw_e
            if e < min_edge:
                continue
            # Single-source guard (rule, 2026-06-29): if this outcome is priced
            # by fewer than MIN_BOOKS_FOR_STAKING distinct books there is no
            # cross-venue confirmation (e.g. Polymarket alone), so flag it
            # indicative and DON'T auto-stake it unless explicitly overridden.
            indicative = (
                books_pricing(fb.books, outcome) < MIN_BOOKS_FOR_STAKING
                and not _stake_single_source()
            )
            # Route each pick to the single currency pool that backs its venue
            # (Polymarket -> $ pool, every other book -> £ pool) and size it ONLY
            # there. Every other pool gets 0 so the per-pool deployment, exposure
            # and daily-cap maths downstream never mix £ and $.
            target = pool_for_venue(venue_of(book), pools)
            stakes: Dict[str, float] = {}
            for pool in pools:
                stakes[pool.name] = (
                    kelly_mod.stake(
                        p, net, pool.bankroll,
                        fraction=pool.kelly_fraction, cap=pool.per_bet_cap,
                    )
                    if (pool.name == target.name and not indicative)
                    else 0.0
                )
            recs.append(
                Recommendation(
                    match_id=str(fb.fx["event_id"]),
                    match_desc="%s vs %s" % (home, away),
                    commence_time=commence,
                    selection=outcome,
                    selection_team=team_map[outcome],
                    best_book=venue_label(book),
                    best_odds=odds,
                    model_prob=p,
                    market_prob=fb.mkt_map[outcome],
                    elo_prob=fb.elo_map[outcome],
                    dc_prob=fb.dc_map[outcome],
                    edge=e,
                    ev_per_unit=e,
                    stakes=stakes,
                    venue=venue_of(book),
                    raw_edge=raw_e,
                    hours_to_kickoff=h2k,
                    imminent=imminent,
                    category=classify_outcome(outcome, p, fb.mkt_map),
                    indicative=indicative,
                )
            )

    recs.sort(key=lambda r: r.edge, reverse=True)
    return recs


def build_score_cards(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    neutral_host_factor: float = DEFAULT_NEUTRAL_HOST_FACTOR,
    top_k: int = 6,
) -> List[ScorelineCard]:
    """Full-time scoreline cards reconciled to the *same* blended 1X2 as the bets.

    For every fixture with a usable market this builds the Dixon-Coles score
    matrix and reconciles it (via :func:`wca.models.scores.reconcile_scoreline_matrix`)
    to the blended ``(home, draw, away)`` probability the card pipeline bets
    against, then derives the top scorelines, over/under and BTTS from the
    reconciled matrix. The returned list is aligned one-to-one with the fixtures
    that survive market filtering, in odds-feed order.

    Parameters
    ----------
    models, odds_df, fixtures_meta, weights, host_nations:
        Same as :func:`build_card`; ``weights`` MUST match those used for the
        recommendations so the scorelines are consistent with the picks.
    min_edge:
        Edge threshold stored on each card for its ``min_price`` helper.
    top_k:
        Number of top scorelines per fixture (default 6).
    """
    blends = _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations, neutral_host_factor
    )

    cards: List[ScorelineCard] = []
    for fb in blends:
        pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
        target = (
            fb.blended["home"],
            fb.blended["draw"],
            fb.blended["away"],
        )
        cards.append(
            scoreline_card(
                pred,
                target,
                home=fb.home,
                away=fb.away,
                top_k=top_k,
                min_edge=min_edge,
            )
        )
    return cards


# ---------------------------------------------------------------------------
# Match-event references (DISPLAY ONLY — never staked, never sized).
# ---------------------------------------------------------------------------


@dataclass
class MatchEventsReference:
    """Reference-only match-event view for one fixture (NOT a stakeable pick).

    Surfaces corners O/U, cards O/U and BTTS alongside the main bet card purely
    as *reference*. None of these fields feed sizing, Kelly or the +EV gate —
    they reuse already-fitted models (the same Dixon-Coles expected goals the
    1X2 bets use) so the desk can eyeball event markets without a second
    pipeline. There is deliberately no ``edge``, ``stake`` or best-price field:
    these are never staked.

    Fields
    ------
    home, away:
        Canonical team labels (same spelling as the 1X2 recommendations).
    commence_time:
        ISO-8601 kickoff string, copied from the fixture's odds feed.
    corners_line / corners_p_over / corners_mu:
        The corners O/U line, P(total corners > line) from
        :class:`~wca.models.props.CornersModel`, and the model's expected total
        corners. Reuses the exact CornersModel path :mod:`wca.nextmatch` uses.
    cards_line / cards_p_over / cards_mu:
        The cards O/U line, P(total cards > line) from
        :class:`~wca.models.props.CardsModel` (previously orphaned — wired here
        for the first time), and the expected total cards. With no team
        aggression priors available at card-build time the multipliers stay at
        their 1.0 defaults, so this is the tournament base rate — flagged as a
        baseline in the formatter.
    btts:
        P(both teams score), taken from the reconciled scoreline matrix so it is
        consistent with the blended 1X2 the card bets against (identical source
        to the scoreline reference already shown in the footer).
    """

    home: str
    away: str
    commence_time: str
    corners_line: float
    corners_p_over: float
    corners_mu: float
    cards_line: float
    cards_p_over: float
    cards_mu: float
    btts: float


def build_event_references(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    neutral_host_factor: float = DEFAULT_NEUTRAL_HOST_FACTOR,
    corners_line: float = DEFAULT_EVENT_CORNERS_LINE,
    cards_line: float = DEFAULT_EVENT_CARDS_LINE,
    corners_model: Optional[CornersModel] = None,
    cards_model: Optional[CardsModel] = None,
) -> List[MatchEventsReference]:
    """Reference-only match-event view per fixture already on the card.

    DISPLAY ONLY. For every fixture with a usable market this reuses the
    already-fitted models — the Dixon-Coles expected goals behind the 1X2 bets —
    to surface three event markets as *reference, never staked*:

    * **corners O/U** via :class:`~wca.models.props.CornersModel`, driven by the
      DC ``lambda_home`` / ``lambda_away`` exactly like :mod:`wca.nextmatch`;
    * **cards O/U** via :class:`~wca.models.props.CardsModel` (calibrated but
      previously never called — wired in here at its tournament base rate, with
      aggression multipliers left at 1.0 as no team foul priors are available at
      card-build time);
    * **BTTS** from the reconciled scoreline matrix (the same source as the
      scoreline reference in the footer), so it agrees with the blended 1X2.

    No probability or Kelly math from :func:`build_card` is touched — this is a
    parallel, non-staked surface aligned one-to-one with the fixtures that
    survive market filtering, in odds-feed order.

    Parameters
    ----------
    models, odds_df, fixtures_meta, weights, host_nations, neutral_host_factor:
        As in :func:`build_card` / :func:`build_score_cards`; ``weights`` should
        match the recommendations so BTTS reconciles to the same 1X2.
    corners_line, cards_line:
        Half-integer O/U lines to evaluate (defaults 8.5 / 3.5).
    corners_model, cards_model:
        Optional pre-built models (defaults to fresh calibrated instances).
    """
    blends = _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations, neutral_host_factor
    )

    cm = corners_model or CornersModel()
    km = cards_model or CardsModel()

    refs: List[MatchEventsReference] = []
    for fb in blends:
        pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
        lam_h = float(getattr(pred, "lambda_home", 0.0) or 0.0)
        lam_a = float(getattr(pred, "lambda_away", 0.0) or 0.0)

        # BTTS from the reconciled matrix so it matches the blended 1X2 bets use.
        scores = scoreline_card(
            pred,
            (fb.blended["home"], fb.blended["draw"], fb.blended["away"]),
            home=fb.home,
            away=fb.away,
        )

        refs.append(
            MatchEventsReference(
                home=fb.home,
                away=fb.away,
                commence_time=str(fb.fx["commence_time"]),
                corners_line=float(corners_line),
                corners_p_over=cm.prob_over(corners_line, lam_h, lam_a),
                corners_mu=cm.mean_total(lam_h, lam_a),
                cards_line=float(cards_line),
                cards_p_over=km.prob_over(cards_line),
                cards_mu=km.mean_total(),
                btts=float(scores.btts),
            )
        )
    return refs


def format_event_references(refs: Sequence[MatchEventsReference]) -> str:
    """Human-readable match-event reference block (Markdown), clearly non-staked.

    Renders the corners O/U, cards O/U and BTTS surfaced by
    :func:`build_event_references`. Every line is explicitly flagged
    REFERENCE / NOT STAKED — these markets are never sized and carry no edge.
    """
    if not refs:
        return "*No match-event references* for the current slate."
    lines: List[str] = [
        "*World Cup Alpha — match events (REFERENCE, NOT STAKED)* (%d fixtures)"
        % len(refs),
        "_Reference only — reused models, never sized, no edge/stake._",
    ]
    for r in refs:
        lines.append("")
        lines.append("*%s vs %s*" % (r.home, r.away))
        lines.append(
            "    corners O/U %.1f: over %.1f%% / under %.1f%%  (xCorners %.1f)"
            % (
                r.corners_line, r.corners_p_over * 100,
                (1.0 - r.corners_p_over) * 100, r.corners_mu,
            )
        )
        lines.append(
            "    cards O/U %.1f: over %.1f%% / under %.1f%%  (xCards %.1f, base rate)"
            % (
                r.cards_line, r.cards_p_over * 100,
                (1.0 - r.cards_p_over) * 100, r.cards_mu,
            )
        )
        lines.append("    BTTS: yes %.1f%% / no %.1f%%" % (r.btts * 100, (1.0 - r.btts) * 100))
    return "\n".join(lines)


def apply_daily_exposure_caps(
    recs: List[Recommendation], pools: Sequence[PoolConfig]
) -> List[Recommendation]:
    """Scale each pool's stakes down so same-day total respects its cap."""
    pool_by_name = {p.name: p for p in pools}
    for name, pool in pool_by_name.items():
        stakes = np.array([r.stakes.get(name, 0.0) for r in recs], dtype=float)
        scaled = kelly_mod.simultaneous_exposure_scale(
            stakes, pool.daily_exposure_cap, pool.bankroll
        )
        for r, s in zip(recs, scaled):
            r.stakes[name] = float(s)
    return recs


# ---------------------------------------------------------------------------
# Operating-rules ranking + cut (rule 2/3) and cross-venue split (rule 4).
# ---------------------------------------------------------------------------


@dataclass
class RankedCard:
    """The operating-rules card: ranked STAKED picks + the CUT longshot list.

    ``picks`` are sorted by HIT PROBABILITY first (favourites, structural draws,
    second-favourites, then any surviving short longshots), with EV as a gate
    only — never the ranker. ``cut`` holds the excluded outright-underdog
    longshots with their EV and the reason they were cut, so nothing is hidden.
    """

    picks: List[Recommendation]
    cut: List[Recommendation]


def _cut_reason(rec: Recommendation) -> Optional[str]:
    """Why a +EV rec should be CUT from the STAKED picks (rule 2), or ``None``.

    Selection rule (memory: feedback-likely-pnl-no-minnows): keep EV as a gate
    but make hit-probability primary. Two cuts:

    * below the hard probability floor — too unlikely to return PnL; and
    * an outright-underdog "mispriced-minnow" longshot below
      :data:`LONGSHOT_PROB` — these lose ~90% of the time even when +EV.
    """
    if rec.model_prob < SELECTION_MIN_PROB:
        return (
            "below %.0f%% hit-probability floor (model %.1f%%) — too unlikely "
            "to return PnL even at +%.1f%% EV"
            % (SELECTION_MIN_PROB * 100, rec.model_prob * 100, rec.edge * 100)
        )
    if rec.category == "longshot":
        # Category-based cut (user, 2026-06-29): NO cash on outright-underdog
        # longshots regardless of exact probability — they lose far more often
        # than not even when +EV. The likely-PnL rule routes these to the
        # free-bet / lottery pool only, never the cash card.
        return (
            "outright-underdog longshot (model %.1f%%) — no cash on minnows "
            "(likely-PnL rule); free-bet/lottery pool only, +%.1f%% EV"
            % (rec.model_prob * 100, rec.edge * 100)
        )
    return None


def rank_card(recs: Sequence[Recommendation]) -> RankedCard:
    """Apply the selection rule (rule 2) to gated recs: rank and cut.

    HIT PROBABILITY is the primary sort key, then model probability, then edge:
    favourites and structural draws rank first, second-favourites next, and
    only short, high-probability longshots survive at all — outright-underdog
    mispriced minnows are CUT (kept visible with their EV + reason). Stakes on
    cut recs are zeroed so a downstream sizer can't accidentally deploy them.
    """
    picks: List[Recommendation] = []
    cut: List[Recommendation] = []
    for r in recs:
        reason = _cut_reason(r)
        if reason is None:
            picks.append(r)
        else:
            r.cut = True
            r.cut_reason = reason
            r.stakes = {k: 0.0 for k in r.stakes}
            cut.append(r)

    picks.sort(
        key=lambda r: (
            _CATEGORY_PRIORITY.get(r.category, 9),  # hit-probability bucket
            -r.model_prob,                          # then higher prob first
            -r.edge,                                # EV breaks ties only
        )
    )
    cut.sort(key=lambda r: -r.edge)  # show the most-tempting (highest EV) first
    return RankedCard(picks=picks, cut=cut)


def venue_deployment(
    picks: Sequence[Recommendation], pool_name: str
) -> Dict[str, float]:
    """Per-venue £/$ deployment split across the staked picks (rule 4).

    Sums each pick's stake (for ``pool_name``) into its best-price venue, so the
    card can show how much capital lands on Smarkets / Betfair / Polymarket.
    """
    split: Dict[str, float] = {}
    for r in picks:
        if r.cut:
            continue
        stake = float(r.stakes.get(pool_name, 0.0))
        if stake <= 0.0:
            continue
        split[r.venue] = split.get(r.venue, 0.0) + stake
    return {v: round(s, 2) for v, s in sorted(split.items())}


def whole_book_exposure(
    picks: Sequence[Recommendation],
    bankroll: float,
    cap_fraction: float = 0.05,
) -> List[Dict[str, object]]:
    """Whole-book exposure ACROSS venues, combined per match (rule 4).

    For every fixture carrying staked picks, sum the real-money stake at risk
    across ALL its outcomes and venues (the hard cash floor: if every outcome
    on the match lost we'd be down their combined stake) and flag it against a
    ``cap_fraction``-of-bankroll cap. Each outcome is sized individually
    upstream; this is the cross-venue whole-book check on top.

    This intentionally uses the conservative independent-stake floor; the
    correlation-aware joint distribution (when scoreline lambdas are persisted)
    lives in :mod:`wca.exposure_corr` and is wired into the exposure feed —
    reused here only when the caller passes lambdas in via that module.
    """
    by_match: Dict[str, Dict[str, object]] = {}
    for r in picks:
        if r.cut:
            continue
        stake = sum(float(s) for s in r.stakes.values())
        if stake <= 0.0:
            continue
        m = by_match.setdefault(
            r.match_desc,
            {"match": r.match_desc, "stake_at_risk": 0.0, "venues": set(), "n_legs": 0},
        )
        m["stake_at_risk"] = float(m["stake_at_risk"]) + stake  # type: ignore[arg-type]
        m["venues"].add(r.venue)  # type: ignore[union-attr]
        m["n_legs"] = int(m["n_legs"]) + 1  # type: ignore[arg-type]

    cap = cap_fraction * float(bankroll)
    out: List[Dict[str, object]] = []
    for m in by_match.values():
        risk = round(float(m["stake_at_risk"]), 2)  # type: ignore[arg-type]
        out.append({
            "match": m["match"],
            "stake_at_risk": risk,
            "venues": sorted(m["venues"]),  # type: ignore[arg-type]
            "n_legs": m["n_legs"],
            "cap": round(cap, 2),
            "over_cap": risk > cap,
        })
    out.sort(key=lambda d: -float(d["stake_at_risk"]))  # type: ignore[arg-type]
    return out


def format_card(recs: Sequence[Recommendation], pools: Sequence[PoolConfig]) -> str:
    """Human-readable card for the terminal or Telegram (Markdown)."""
    if not recs:
        return "*No +EV bets* on the current slate at the configured threshold."
    lines = ["*World Cup Alpha — bet card* (%d picks)" % len(recs), ""]
    for i, r in enumerate(recs, 1):
        stake_str = "  ".join(
            "%s £%.2f" % (p.name, r.stakes.get(p.name, 0.0)) for p in pools
        )
        lines.append(
            "*%d. %s* — %s @ *%.2f* (%s)\n"
            "    model %.1f%% / mkt %.1f%%  edge *%+.1f%%*  [elo %.0f%% dc %.0f%%]\n"
            "    stake: %s"
            % (
                i, r.match_desc, r.selection_team, r.best_odds, r.best_book,
                r.model_prob * 100, r.market_prob * 100, r.edge * 100,
                r.elo_prob * 100, r.dc_prob * 100, stake_str,
            )
        )
    return "\n".join(lines)


_CATEGORY_LABEL = {
    "favourite": "FAV",
    "second_favourite": "2ND-FAV",
    "structural_draw": "DRAW",
    "longshot": "LONGSHOT",
}


def format_ranked_card(
    ranked: RankedCard,
    pools: Union[PoolConfig, Sequence[PoolConfig]],
    bank: Optional["PoolBankroll"] = None,
) -> str:
    """Operating-rules card (Markdown): ranked picks, CUT list, footer.

    Renders rule 2 (hit-probability ranking + CUT longshots), rule 3 (the
    further-out tilt — imminent fixtures flagged and edge-discounted), rule 4
    (venue tag + per-venue deployment split + cross-venue whole-book exposure)
    and rule 1 (the dual-pool 1/2-Kelly bankroll footer). Each pick is sized
    and shown in its venue's own currency — Polymarket in $ off the $-pool,
    every other book in £ off the £-pool. Reference-only markets (scorelines
    etc.) are appended by the caller, clearly flagged "REFERENCE, NOT SIZED".

    ``pools`` accepts a single :class:`PoolConfig` (legacy single-pool callers)
    or a sequence; a lone pool is wrapped so per-pick routing still works.
    """
    pool_list: List[PoolConfig] = (
        [pools] if isinstance(pools, PoolConfig) else list(pools)
    )
    lines: List[str] = []
    n_indicative = sum(1 for r in ranked.picks if r.indicative)
    n_staked = len(ranked.picks) - n_indicative
    header = "*World Cup Alpha — bet card* (%d staked" % n_staked
    if n_indicative:
        header += ", %d indicative" % n_indicative
    header += " picks, hit-prob ranked)"
    lines.append(header)
    lines.append("")
    if not ranked.picks:
        lines.append("_No +EV bets clear the selection rule on the current slate._")
    if n_indicative and n_staked == 0:
        lines.append(
            "_⚠ Every pick is single-source (Polymarket only) — INDICATIVE, "
            "not staked. Wire a 2nd book (Betfair creds) or set "
            "WCA_STAKE_SINGLE_SOURCE=1 to size them._"
        )
        lines.append("")
    for i, r in enumerate(ranked.picks, 1):
        rp = pool_for_venue(r.venue, pool_list)
        stake = r.stakes.get(rp.name, 0.0)
        tilt = ""
        if r.indicative:
            tilt = "  INDICATIVE — single-source, no cross-venue confirmation (not staked)"
        elif r.imminent and r.raw_edge is not None:
            tilt = "  IMMINENT: edge discounted %+.1f%%->%+.1f%% (likely model error)" % (
                r.raw_edge * 100, r.edge * 100
            )
        elif r.hours_to_kickoff is not None and r.hours_to_kickoff >= FURTHER_OUT_HOURS:
            tilt = "  further-out (%.0fh) — thin/soft market" % r.hours_to_kickoff
        lines.append(
            "*%d. [%s] %s* — %s @ *%.2f* via *%s*\n"
            "    model %.1f%% / mkt %.1f%%  edge *%+.1f%%*  [elo %.0f%% dc %.0f%%]\n"
            "    stake: %s %s%.2f%s"
            % (
                i, _CATEGORY_LABEL.get(r.category, r.category.upper()),
                r.match_desc, r.selection_team, r.best_odds, r.venue,
                r.model_prob * 100, r.market_prob * 100, r.edge * 100,
                r.elo_prob * 100, r.dc_prob * 100, rp.name, rp.symbol, stake, tilt,
            )
        )

    # CUT list (rule 2): excluded longshots, kept visible with EV + reason.
    if ranked.cut:
        lines.append("")
        lines.append("*— CUT (excluded from staking, %d) —*" % len(ranked.cut))
        for r in ranked.cut:
            lines.append(
                "  x %s — %s @ %.2f (model %.1f%%, +%.1f%% EV): %s"
                % (
                    r.match_desc, r.selection_team, r.best_odds,
                    r.model_prob * 100, r.edge * 100, r.cut_reason,
                )
            )

    # Cross-venue deployment split + whole-book exposure (rule 4), PER POOL so
    # the £ and $ books are summed and capped in their own currency (never mixed).
    if ranked.picks:
        for pool in pool_list:
            pool_picks = [
                r for r in ranked.picks
                if float(r.stakes.get(pool.name, 0.0)) > 0.0
            ]
            if not pool_picks:
                continue
            split = venue_deployment(pool_picks, pool.name)
            if split:
                lines.append("")
                lines.append(
                    "*Venue split (%s):* " % pool.name
                    + "  ".join(
                        "%s %s%.2f" % (v, pool.symbol, s)
                        for v, s in split.items()
                    )
                )
            book = whole_book_exposure(pool_picks, pool.bankroll)
            flagged = [b for b in book if b["over_cap"]]
            if flagged:
                lines.append(
                    "*Whole-book exposure (%s, cross-venue):* %d match(es) over "
                    "the 5%%-of-base cap:" % (pool.name, len(flagged))
                )
                for b in flagged:
                    lines.append(
                        "  ! %s — %s%.2f at risk across %s (cap %s%.2f)"
                        % (
                            b["match"], pool.symbol, b["stake_at_risk"],
                            ", ".join(b["venues"]), pool.symbol, b["cap"],
                        )
                    )

    # Bankroll footer (rule 1): dual-pool 1/2-Kelly, each book in its own currency.
    lines.append("")
    lines.append("*Bankroll model (1/2-Kelly, equally split across books):*")
    for pool in pool_list:
        lines.append(
            "  - %s pool: %s%.0f  (1/2-Kelly, per-bet cap %.0f%%, daily cap %.0f%%)"
            % (
                pool.name, pool.symbol, pool.bankroll,
                pool.per_bet_cap * 100, pool.daily_exposure_cap * 100,
            )
        )
    lines.append(
        "  - £1 = $1.33 fixed; Polymarket sized in $ off the $%.0f pool, every "
        "other venue in £ off the £%.0f pool." % (PM_POOL_BANKROLL, GBP_POOL_BANKROLL)
    )
    if bank is not None:
        lines.append(
            "  - actual capital (unpartitioned, ref): £%.0f" % bank.actual_capital
        )
        if bank.constrained:
            lines.append("  - CONSTRAINED: %s" % bank.constraint_note)
    return "\n".join(lines)


def format_scores(
    cards: Sequence[ScorelineCard], min_edge: float = 0.02
) -> str:
    """Human-readable scoreline card for the terminal or Telegram (Markdown).

    Per fixture: expected goals from the model, the top-6 scorelines
    (``"2-1  12.3%  fair 8.13  back >= 8.46"``) followed by one line with
    over/under 2.5 and BTTS probabilities.  The ``back >=`` price is the
    minimum decimal odds at which backing that scoreline clears ``min_edge``
    (each card's own ``min_edge`` is used; the argument is a display-only
    fallback for cards that predate it).
    """
    if not cards:
        return "*No scoreline cards* for the current slate."
    lines: List[str] = ["*World Cup Alpha — scorelines* (%d fixtures)" % len(cards)]
    for c in cards:
        me = getattr(c, "min_edge", min_edge)
        lines.append("")
        lines.append("*%s vs %s*" % (c.home, c.away))
        # Expected goals from the reconciled score-probability matrix.
        rows = np.arange(c.matrix.shape[0])
        cols = np.arange(c.matrix.shape[1])
        eh = float((rows * c.matrix.sum(axis=1)).sum())
        ea = float((cols * c.matrix.sum(axis=0)).sum())
        lines.append("    xG: %.2f-%.2f" % (eh, ea))
        for h, a, p in c.top_scorelines:
            fair = c.fair_odds(p)
            backp = c.min_price(p, me)
            # Show the implied probability after each decimal price: the fair
            # leg restates the model prob; the back leg is the break-even prob at
            # the minimum price (the gap is the edge buffer you must clear).
            lines.append(
                "    %d-%d  %.1f%%  fair %.2f (%.1f%%)  back >= %.2f (%.1f%%)"
                % (h, a, p * 100, fair, (100.0 / fair) if fair else 0.0,
                   backp, (100.0 / backp) if backp else 0.0)
            )
        ou25 = c.over_under.get(2.5)
        p_over = ou25[0] if ou25 is not None else float("nan")
        lines.append(
            "    O/U 2.5: over %.1f%% / under %.1f%%   BTTS %.1f%%"
            % (p_over * 100, (1.0 - p_over) * 100, c.btts * 100)
        )
    return "\n".join(lines)
