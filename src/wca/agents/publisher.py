"""Agent 8 — Telegram Publisher.

Formats the full pipeline output into a concise, actionable Telegram alert
and (optionally) sends it.  Always returns the formatted string so callers
can log or display it independently of Telegram availability.

Input:  PipelineResult
Output: str (formatted Telegram message)
"""

from __future__ import annotations

import logging
from typing import Optional

from wca.agents.contracts import PipelineResult

logger = logging.getLogger(__name__)

# Confidence-score thresholds for letter grades.
_GRADE_THRESHOLDS = [
    (90, "A"),
    (80, "A-"),
    (70, "B+"),
    (60, "B"),
    (50, "B-"),
    (40, "C+"),
    (30, "C"),
    (0, "C-"),
]


def format_alert(result: PipelineResult, currency: str = "£") -> str:
    """Return the full Telegram-formatted alert string for *result*."""
    top = result.edges.top_pick
    sizing = result.sizing
    review = result.review
    model = result.model
    fx = result.fixture

    if not review.approved or top is None:
        return _format_no_pick(result)

    # Confidence grade.
    grade = _grade(review.confidence_score)

    # Fair odds (model implied).
    fair = round(1.0 / top.model_probability, 2) if top.model_probability > 0 else "—"

    # Stake display.
    if sizing:
        stake_display = "%s%.2f (%.2f%% bankroll)" % (
            currency, sizing.stake_amount, sizing.stake_pct * 100
        )
    else:
        stake_display = "size manually"

    # Key drivers from team intel + review.
    drivers = _key_drivers(result)

    # Score-distribution snippet (top 3 scorelines).
    score_lines = _score_snippet(result)

    lines = [
        "🚨 *EDGE DETECTED*",
        "",
        "*Match:*",
        "  %s vs %s" % (fx.home, fx.away),
        "  %s" % _stage_label(fx.stage),
        "  Kickoff: %s" % _fmt_kickoff(fx.kickoff),
        "",
        "*Market:*",
        "  %s — %s" % (top.market.replace("_", " ").title(), top.selection.title()),
        "",
        "*Best Price:*",
        "  %s @ *%.2f*" % (top.bookmaker, top.odds),
        "",
        "*Model:*",
        "  %.1f%% (fair odds: %.2f)" % (top.model_probability * 100, fair),
        "  Elo: %.1f%%  DC: %.1f%%  Market: %.1f%%" % (
            model.model_sources.get("elo", {}).get(_outcome_key(top.selection), 0) * 100,
            model.model_sources.get("dc", {}).get(_outcome_key(top.selection), 0) * 100,
            model.model_sources.get("market", {}).get(_outcome_key(top.selection), 0) * 100,
        ),
        "",
        "*Edge:*",
        "  +%.1f%%   EV: +%.1f%% per unit" % (top.edge * 100, top.expected_value * 100),
        "",
        "*Confidence:* %s (score %d/100)" % (grade, int(review.confidence_score)),
        "",
        "*Stake:*",
        "  %s" % stake_display,
    ]

    if score_lines:
        lines += ["", "*Top scorelines:*", score_lines]

    if drivers:
        lines += ["", "*Key drivers:*"] + ["  • %s" % d for d in drivers]

    if review.failure_modes:
        lines += ["", "*Risks flagged:*"] + ["  ⚠️ %s" % f for f in review.failure_modes[:3]]

    if review.recommendation_adjustments:
        lines += [""] + ["  💡 %s" % a for a in review.recommendation_adjustments[:2]]

    return "\n".join(lines)


def send(
    result: PipelineResult,
    chat_id: Optional[str] = None,
    currency: str = "£",
) -> str:
    """Format and send the alert to Telegram.

    Returns the formatted string regardless of send success.
    """
    text = format_alert(result, currency=currency)

    if chat_id:
        try:
            from wca.bot.telegram import TelegramClient

            client = TelegramClient()
            client.send_message(chat_id, text)
            logger.info("Alert sent to chat %s", chat_id)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_no_pick(result: PipelineResult) -> str:
    """Compact message when no pick was approved."""
    fx = result.fixture
    review = result.review
    edges = result.edges

    lines = [
        "ℹ️ *NO PICK — %s vs %s*" % (fx.home, fx.away),
        "",
    ]
    if not edges.opportunities:
        lines.append("No edge opportunities cleared the threshold.")
    elif not review.approved:
        lines.append("Pick blocked by adversarial review (confidence %d/100)." % int(review.confidence_score))
        if review.failure_modes:
            lines += [""] + ["  ⚠️ %s" % f for f in review.failure_modes[:3]]
    else:
        lines.append("Pipeline completed — no actionable edge found.")

    return "\n".join(lines)


def _grade(score: float) -> str:
    for threshold, letter in _GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return "F"


def _stage_label(stage: str) -> str:
    return {
        "group": "Group Stage",
        "r32": "Round of 32",
        "r16": "Round of 16",
        "qf": "Quarter-Final",
        "sf": "Semi-Final",
        "final": "Final",
    }.get(stage, stage.title())


def _fmt_kickoff(kickoff: str) -> str:
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y  %H:%M UTC")
    except Exception:
        return kickoff


def _outcome_key(selection: str) -> str:
    s = selection.lower()
    if s in ("home", "1", "home win"):
        return "home"
    if s in ("away", "2", "away win"):
        return "away"
    return "draw"


def _score_snippet(result: PipelineResult) -> str:
    top3 = sorted(
        result.model.score_distribution, key=lambda s: s.probability, reverse=True
    )[:3]
    if not top3:
        return ""
    return "  " + "  ".join(
        "%d-%d (%.0f%%)" % (s.home_goals, s.away_goals, s.probability * 100)
        for s in top3
    )


def _key_drivers(result: PipelineResult) -> list:
    drivers = []
    ti = result.team_intel
    mi = result.market_intel
    model = result.model

    # xG balance.
    if model.expected_goals_home and model.expected_goals_away:
        drivers.append(
            "xG: %s %.2f — %s %.2f" % (
                result.fixture.home, model.expected_goals_home,
                result.fixture.away, model.expected_goals_away,
            )
        )

    # Strength adjustments (if material).
    for side, adj in ti.strength_adjustments.items():
        if abs(adj - 1.0) >= 0.02:
            direction = "weakened" if adj < 1.0 else "boosted"
            team = result.fixture.home if side == "home" else result.fixture.away
            drivers.append("%s squad %s (×%.2f)" % (team, direction, adj))

    # Steam signals.
    for s in mi.steam_signals[:2]:
        drivers.append("Steam: %s %s +%.1f%%" % (s.market, s.direction, s.magnitude_pct))

    # Polymarket vs BM dislocation.
    if mi.market_dislocation_score >= 0.05:
        drivers.append("PM/BM gap: %.0f%%" % (mi.market_dislocation_score * 100))

    # Tactical notes.
    for note in ti.tactical_notes[:2]:
        if "absence" in note.lower() or "doubt" in note.lower() or "news" in note.lower():
            drivers.append(note)

    return drivers[:5]
