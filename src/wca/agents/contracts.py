"""Typed JSON contracts for the World Cup Alpha agent pipeline.

Every agent receives and returns instances of these dataclasses.  Serialise
with ``dataclasses.asdict(obj)``; deserialise by splatting a dict into the
constructor.  No agent reads another agent's source code or internal state —
only these contracts cross agent boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


@dataclass
class Fixture:
    home: str
    away: str
    kickoff: str        # ISO-8601, e.g. "2026-06-25T19:00:00Z"
    event_id: str       # TheOddsAPI event_id
    stadium: str = ""
    stage: str = "group"   # group | r32 | r16 | qf | sf | final
    neutral: bool = True


@dataclass
class PlayerAvailability:
    name: str
    team: str
    status: str     # available | doubtful | out
    reason: str = ""
    source: str = ""


# ---------------------------------------------------------------------------
# Agent 1 — Data Collector
# ---------------------------------------------------------------------------


@dataclass
class DataPackage:
    """Raw data gathered for one fixture before any modelling."""

    fixture: Fixture
    injuries: List[PlayerAvailability] = field(default_factory=list)
    suspensions: List[PlayerAvailability] = field(default_factory=list)
    referee: Dict[str, Any] = field(default_factory=dict)
    weather: Dict[str, Any] = field(default_factory=dict)
    # Raw bookmaker rows: [{book, market, selection, odds}, ...]
    bookmaker_odds: List[Dict[str, Any]] = field(default_factory=list)
    # Prediction-market rows: [{source, market, selection, probability}, ...]
    prediction_market_odds: List[Dict[str, Any]] = field(default_factory=list)
    # News items: [{title, summary, pub_date, url, score}, ...]
    news_items: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent 2 — Team Intelligence
# ---------------------------------------------------------------------------


@dataclass
class TeamIntelligence:
    """Squad and tactical context for both teams."""

    expected_lineups: Dict[str, List[str]] = field(default_factory=dict)
    player_availability: Dict[str, str] = field(default_factory=dict)   # name -> status
    tactical_notes: List[str] = field(default_factory=list)
    # Multiplicative adjustments to apply to xG means (1.0 = no change).
    strength_adjustments: Dict[str, float] = field(
        default_factory=lambda: {"home": 1.0, "away": 1.0}
    )


# ---------------------------------------------------------------------------
# Agent 3 — Market Intelligence
# ---------------------------------------------------------------------------


@dataclass
class SteamSignal:
    market: str
    direction: str      # "home" | "away" | "draw" | "over" | "under"
    magnitude_pct: float
    source: str = ""


@dataclass
class MarketIntelligence:
    """De-vigged consensus odds, dislocation score and steam signals."""

    # Shin-de-vigged median 1X2 across all books.
    fair_odds_estimate: Dict[str, float] = field(default_factory=dict)
    # Aggregate de-vigged book consensus (multiplicative).
    bookmaker_consensus: Dict[str, float] = field(default_factory=dict)
    # 0–1: how far Polymarket diverges from the bookmaker consensus.
    market_dislocation_score: float = 0.0
    steam_signals: List[SteamSignal] = field(default_factory=list)
    # Best (max) decimal price available per outcome across all books.
    best_available: Dict[str, Dict[str, float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent 4 — Match Model
# ---------------------------------------------------------------------------


@dataclass
class ScoreProb:
    home_goals: int
    away_goals: int
    probability: float


@dataclass
class PropEstimate:
    market: str          # "corners_total_over", "cards_total_over", "anytime_scorer"
    selection: str       # "over 9.5" | "<player name>"
    line: Optional[float]
    model_prob: float
    fair_odds: float


@dataclass
class ModelOutput:
    """Full model output for one fixture."""

    home: str
    away: str
    # Blended 1X2 (Elo 10% + DC 30% + market 60%)
    win_prob: float
    draw_prob: float
    loss_prob: float
    expected_goals_home: float
    expected_goals_away: float
    score_distribution: List[ScoreProb] = field(default_factory=list)
    prop_estimates: List[PropEstimate] = field(default_factory=list)
    # Per-component breakdown for transparency.
    model_sources: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Advancement probabilities from Monte Carlo (when in knockout stage).
    advancement_probs: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent 5 — Edge Detector
# ---------------------------------------------------------------------------


@dataclass
class EdgeOpportunity:
    market: str
    selection: str
    bookmaker: str
    odds: float
    model_probability: float
    implied_probability: float
    edge: float             # model_prob - implied_prob (positive = model likes it)
    expected_value: float   # edge * odds (profit per unit staked)
    liquidity_penalty: float = 0.0


@dataclass
class EdgeReport:
    fixture: str
    opportunities: List[EdgeOpportunity] = field(default_factory=list)
    top_pick: Optional[EdgeOpportunity] = None
    rejected_count: int = 0


# ---------------------------------------------------------------------------
# Agent 6 — Adversarial Reviewer
# ---------------------------------------------------------------------------


@dataclass
class AdversarialReview:
    """Result of the LLM-based critique pass. Pick is blocked if not approved."""

    confidence_score: float     # 0–100
    failure_modes: List[str] = field(default_factory=list)
    recommendation_adjustments: List[str] = field(default_factory=list)
    approved: bool = False
    reviewer_reasoning: str = ""


# ---------------------------------------------------------------------------
# Agent 7 — Bet Sizing
# ---------------------------------------------------------------------------


@dataclass
class BetSizing:
    opportunity: EdgeOpportunity
    stake_pct: float        # fraction of bankroll (e.g. 0.0125 = 1.25 %)
    stake_amount: float     # in pool currency (e.g. £18.75)
    bankroll_ref: float     # bankroll used for sizing
    portfolio_impact: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Full pipeline result
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Aggregated output of all agents for one fixture analysis."""

    fixture: Fixture
    data: DataPackage
    team_intel: TeamIntelligence
    market_intel: MarketIntelligence
    model: ModelOutput
    edges: EdgeReport
    review: AdversarialReview
    sizing: Optional[BetSizing]
