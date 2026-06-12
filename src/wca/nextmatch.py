"""Next-match preview card: one fixture, every angle.

Builds a single-fixture Telegram card for the *next* upcoming match in the
odds slate:

* winner — the blended 1X2 (same Elo/DC/market blend the bet card uses),
  with fair odds, the best available book price and the edge per outcome;
* corners — the calibrated :class:`wca.models.props.CornersModel` driven by
  the Dixon-Coles expected-goals lambdas, at a configurable line;
* top goalscorers — market anytime-scorer prices (best price per player from
  the per-event Odds API endpoint); shown as raw implied probabilities since
  anytime markets are not a coherent simplex to de-vig;
* scorelines — the reconciled Dixon-Coles score matrix (same reconciliation
  as the main card's scorelines section) plus O/U 2.5 and BTTS.

Like the rest of the card pipeline this module only *recommends*; nothing
here places a bet. The heavy build runs on cron (``scripts/wca_build_card.py``
writes ``data/next_latest.md``) and the bot serves the cache via ``/next``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from wca.card import (
    OUTCOMES,
    BlendWeights,
    FittedModels,
    _FixtureBlend,
    _iter_fixture_blends,
    best_price,
)
from wca.markets import kelly as kelly_mod
from wca.models.props import CornersModel
from wca.models.scores import ScorelineCard, scoreline_card

DEFAULT_CORNERS_LINE = 8.5
ANYTIME_SCORER_MARKET = "player_goal_scorer_anytime"


@dataclass
class ScorerPrice:
    """Best market price for one player in the anytime-scorer market."""

    player: str
    best_odds: float
    best_book: str
    implied: float  # raw 1/odds — vig NOT removed (anytime is not a simplex)


@dataclass
class NextMatchCard:
    """Everything the /next Telegram card renders for one fixture."""

    home: str
    away: str
    commence_time: str
    # outcome -> (blended_prob, best_book or None, best_odds, edge)
    winner: Dict[str, Tuple[float, Optional[str], float, float]]
    corners_line: float
    corners_p_over: float
    corners_mu: float
    scores: ScorelineCard
    scorers: List[ScorerPrice] = field(default_factory=list)
    min_edge: float = 0.02


def select_next_blend(blends: Sequence[_FixtureBlend]) -> Optional[_FixtureBlend]:
    """The fixture kicking off first (min commence_time), or None if empty."""
    if not blends:
        return None
    return min(blends, key=lambda fb: str(fb.fx["commence_time"]))


def top_scorers_from_odds(
    scorer_df: Optional[pd.DataFrame],
    top_n: int = 5,
    market: str = ANYTIME_SCORER_MARKET,
) -> List[ScorerPrice]:
    """Best anytime-scorer price per player, ranked by implied probability.

    ``scorer_df`` is the flat frame from :func:`wca.data.theoddsapi.get_event_odds`
    (may be ``None`` / empty / missing the market — all degrade to ``[]``).
    Shortest price = market favourite, so ranking by raw implied probability
    matches the books' own ordering even though the vig is left in.
    """
    if scorer_df is None or scorer_df.empty or "market" not in scorer_df.columns:
        return []
    rows = scorer_df[scorer_df["market"] == market]
    if rows.empty:
        return []
    out: List[ScorerPrice] = []
    for player, grp in rows.groupby("outcome_name"):
        # Best price for the punter is the MAX odds across books.
        idx = grp["decimal_odds"].astype(float).idxmax()
        odds = float(grp.loc[idx, "decimal_odds"])
        if odds <= 1.0:
            continue
        out.append(
            ScorerPrice(
                player=str(player),
                best_odds=odds,
                best_book=str(grp.loc[idx, "bookmaker_title"]),
                implied=1.0 / odds,
            )
        )
    out.sort(key=lambda s: s.implied, reverse=True)
    return out[:top_n]


def build_next_match(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    scorer_df: Optional[pd.DataFrame] = None,
    corners_line: float = DEFAULT_CORNERS_LINE,
    corners_model: Optional[CornersModel] = None,
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    top_k_scores: int = 6,
) -> Optional[NextMatchCard]:
    """Build the next-match preview, or None when the slate is empty.

    ``odds_df`` should already be filtered to the look-ahead window by the
    caller (same frame the main card build uses); the earliest kickoff among
    fixtures with a usable market wins. ``scorer_df`` is the optional
    per-event anytime-scorer pull for that fixture.
    """
    blends = _iter_fixture_blends(models, odds_df, fixtures_meta, weights, host_nations)
    fb = select_next_blend(blends)
    if fb is None:
        return None

    winner: Dict[str, Tuple[float, Optional[str], float, float]] = {}
    for outcome in OUTCOMES:
        p = fb.blended[outcome]
        book, odds = best_price(fb.books, outcome)
        edge = kelly_mod.edge(p, odds) if book is not None and odds > 1.0 else float("nan")
        winner[outcome] = (p, book, odds, edge)

    pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
    scores = scoreline_card(
        pred,
        (fb.blended["home"], fb.blended["draw"], fb.blended["away"]),
        home=fb.home,
        away=fb.away,
        top_k=top_k_scores,
        min_edge=min_edge,
    )

    cm = corners_model or CornersModel()
    lam_h = float(getattr(pred, "lambda_home", 0.0) or 0.0)
    lam_a = float(getattr(pred, "lambda_away", 0.0) or 0.0)
    p_over = cm.prob_over(corners_line, lam_h, lam_a)
    mu = cm.mean_total(lam_h, lam_a)

    return NextMatchCard(
        home=fb.home,
        away=fb.away,
        commence_time=str(fb.fx["commence_time"]),
        winner=winner,
        corners_line=corners_line,
        corners_p_over=p_over,
        corners_mu=mu,
        scores=scores,
        scorers=top_scorers_from_odds(scorer_df),
        min_edge=min_edge,
    )


def format_next_match(card: Optional[NextMatchCard]) -> str:
    """Telegram Markdown for the next-match card (phone-width friendly)."""
    if card is None:
        return "*Next match*\nNo upcoming fixture with a usable market in the current window."

    lines: List[str] = [
        "⚽ *Next match* — %s vs %s" % (card.home, card.away),
        "Kickoff %s" % card.commence_time,
        "",
        "*Winner* (model blend)",
    ]
    names = {"home": card.home, "draw": "Draw", "away": card.away}
    for outcome in OUTCOMES:
        p, book, odds, edge = card.winner[outcome]
        fair = (1.0 / p) if p > 0 else float("inf")
        line = "  %-14s %5.1f%%  fair %.2f" % (names[outcome][:14], p * 100, fair)
        if book is not None and odds > 1.0:
            flag = " ✅" if edge >= card.min_edge else ""
            line += "  best %.2f (%s) %+.1f%%%s" % (odds, book, edge * 100, flag)
        lines.append(line)

    p_over = card.corners_p_over
    lines.append("")
    lines.append("*Corners* (model, exp %.1f)" % card.corners_mu)
    lines.append(
        "  O/U %.1f: over %.1f%% / under %.1f%%  fair %.2f / %.2f"
        % (
            card.corners_line,
            p_over * 100,
            (1.0 - p_over) * 100,
            (1.0 / p_over) if p_over > 0 else float("inf"),
            (1.0 / (1.0 - p_over)) if p_over < 1 else float("inf"),
        )
    )

    lines.append("")
    if card.scorers:
        lines.append("*Anytime scorer* (best market price, vig in)")
        for s in card.scorers:
            lines.append(
                "  %-18s %5.2f (%s)  imp %.0f%%"
                % (s.player[:18], s.best_odds, s.best_book, s.implied * 100)
            )
    else:
        lines.append("*Anytime scorer* — no market prices available yet.")

    c = card.scores
    lines.append("")
    lines.append("*Scorelines* (top %d)" % len(c.top_scorelines))
    lines.append(
        " | ".join("%d-%d %.1f%%" % (h, a, p * 100) for h, a, p in c.top_scorelines)
    )
    ou25 = c.over_under.get(2.5)
    if ou25 is not None:
        lines.append(
            "  O/U 2.5: over %.1f%% / under %.1f%%   BTTS %.1f%%"
            % (ou25[0] * 100, ou25[1] * 100, c.btts * 100)
        )
    return "\n".join(lines)
