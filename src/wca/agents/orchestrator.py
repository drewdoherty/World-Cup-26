"""Agent 0 — Orchestrator (Planner).

Receives a fixture specification (from a Telegram command or CLI), decomposes
the work into a sequential task graph, routes it through Agents 1–8 and
aggregates the outputs.

Responsibilities
----------------
* Parse and validate the fixture spec.
* Run agents 1–8 in order, passing typed contracts between them.
* Maintain pipeline state (bankroll, daily exposure) for sizing.
* Return the final :class:`~wca.agents.contracts.PipelineResult`.

Never
-----
* Calculate probabilities.
* Analyse matches directly.
* Generate betting picks.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from wca.agents.contracts import Fixture, PipelineResult

logger = logging.getLogger(__name__)


def run_pipeline(
    home: str,
    away: str,
    event_id: str = "",
    kickoff: str = "",
    stage: str = "group",
    neutral: bool = True,
    db_path: str = "data/wca.db",
    regions: str = "uk",
    bankroll: Optional[float] = None,
    daily_exposure_used: float = 0.0,
    chat_id: Optional[str] = None,
    send_telegram: bool = False,
    api_key: Optional[str] = None,
    currency: str = "£",
) -> PipelineResult:
    """Run the full 8-agent analysis pipeline for one fixture.

    Parameters
    ----------
    home, away:
        Team names as they appear in TheOddsAPI / martj42 results.
    event_id:
        TheOddsAPI event_id.  Can be blank — the Data Collector will fuzzy-match
        on team names.
    kickoff:
        ISO-8601 kickoff time string (used for display only if blank).
    stage:
        Tournament stage: ``"group"`` | ``"r32"`` | ``"r16"`` | ``"qf"`` |
        ``"sf"`` | ``"final"``.
    neutral:
        True for a neutral-venue World Cup match (all WC matches are neutral).
    db_path:
        SQLite ledger path for news store and bankroll resolution.
    regions:
        Comma-separated TheOddsAPI region string.
    bankroll:
        Override bankroll in GBP.  If None, resolved from the ledger CLV ladder.
    daily_exposure_used:
        Fraction of bankroll already staked today (0–1).
    chat_id:
        Telegram chat ID to push the alert to (only if *send_telegram* is True).
    send_telegram:
        Whether to send the formatted alert to Telegram on completion.
    api_key:
        Anthropic API key for Agent 6.  Falls back to ``ANTHROPIC_API_KEY`` env.
    currency:
        Currency symbol for display (``"£"`` or ``"$"``).

    Returns
    -------
    PipelineResult
        Full typed output from all agents.
    """
    fixture = Fixture(
        home=home,
        away=away,
        kickoff=kickoff or "",
        event_id=event_id,
        stage=stage,
        neutral=neutral,
    )
    logger.info("Pipeline start: %s vs %s [%s]", home, away, stage)

    # --- Agent 1: Data Collector -----------------------------------------
    from wca.agents import data_collector

    logger.info("Agent 1: collecting data...")
    pkg = data_collector.run(fixture, db_path=db_path, regions=regions)
    logger.info(
        "Agent 1 done: %d bm rows, %d pm rows, %d news items",
        len(pkg.bookmaker_odds), len(pkg.prediction_market_odds), len(pkg.news_items),
    )

    # --- Agent 2: Team Intelligence --------------------------------------
    from wca.agents import team_intel

    logger.info("Agent 2: team intelligence...")
    ti = team_intel.run(pkg)
    logger.info("Agent 2 done: strength adj %s", ti.strength_adjustments)

    # --- Agent 3: Market Intelligence ------------------------------------
    from wca.agents import market_intel

    logger.info("Agent 3: market intelligence...")
    mi = market_intel.run(pkg)
    logger.info(
        "Agent 3 done: Shin consensus %s, dislocation %.2f",
        {k: round(v, 3) for k, v in mi.fair_odds_estimate.items()},
        mi.market_dislocation_score,
    )

    # --- Agent 4: Match Model -------------------------------------------
    from wca.agents import match_model

    logger.info("Agent 4: running models...")
    model_out = match_model.run(pkg, ti, mi)
    logger.info(
        "Agent 4 done: blend H/D/A = %.1f/%.1f/%.1f%%",
        model_out.win_prob * 100, model_out.draw_prob * 100, model_out.loss_prob * 100,
    )

    # --- Agent 5: Edge Detector -----------------------------------------
    from wca.agents import edge_detector

    logger.info("Agent 5: scanning for edge...")
    edge_rpt = edge_detector.run(pkg, model_out, mi)
    logger.info(
        "Agent 5 done: %d opps, %d rejected, top=%s",
        len(edge_rpt.opportunities), edge_rpt.rejected_count,
        "%s @ %.2f" % (edge_rpt.top_pick.selection, edge_rpt.top_pick.odds)
        if edge_rpt.top_pick else "None",
    )

    # --- Agent 6: Adversarial Reviewer ----------------------------------
    from wca.agents import adversarial

    logger.info("Agent 6: adversarial review...")
    review = adversarial.run(pkg, ti, model_out, edge_rpt, api_key=api_key)
    logger.info(
        "Agent 6 done: approved=%s, confidence=%d",
        review.approved, int(review.confidence_score),
    )

    # --- Agent 7: Bet Sizing -------------------------------------------
    from wca.agents import bet_sizing

    resolved_bankroll = bankroll or _resolve_bankroll(db_path)
    logger.info("Agent 7: sizing (bankroll %s%.0f)...", currency, resolved_bankroll)
    sizing = bet_sizing.run(
        edge_rpt,
        review,
        bankroll=resolved_bankroll,
        daily_exposure_used=daily_exposure_used,
        currency=currency,
    )
    if sizing:
        logger.info(
            "Agent 7 done: stake %s%.2f (%.2f%% bankroll)",
            currency, sizing.stake_amount, sizing.stake_pct * 100,
        )
    else:
        logger.info("Agent 7 done: no stake (pick blocked or no edge)")

    # --- Assemble result ------------------------------------------------
    result = PipelineResult(
        fixture=fixture,
        data=pkg,
        team_intel=ti,
        market_intel=mi,
        model=model_out,
        edges=edge_rpt,
        review=review,
        sizing=sizing,
    )

    # --- Agent 8: Publisher --------------------------------------------
    from wca.agents import publisher

    logger.info("Agent 8: publishing...")
    if send_telegram and chat_id:
        publisher.send(result, chat_id=chat_id, currency=currency)
    else:
        alert_text = publisher.format_alert(result, currency=currency)
        logger.info("Agent 8 formatted alert (%d chars)", len(alert_text))

    logger.info("Pipeline complete: %s vs %s", home, away)
    return result


def parse_fixture_spec(text: str) -> tuple[str, str]:
    """Parse ``"<Home> vs <Away>"`` or ``"<Home> v <Away>"`` from user text.

    Returns ``(home, away)`` with stripped whitespace.

    Raises ``ValueError`` if the spec cannot be parsed.
    """
    for sep in (" vs ", " v ", " VS ", " V "):
        if sep in text:
            parts = text.split(sep, 1)
            home = parts[0].strip()
            away = parts[1].strip()
            if home and away:
                return home, away
    raise ValueError(
        "Could not parse fixture from %r — use 'Home vs Away' format." % text
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_bankroll(db_path: str) -> float:
    """Attempt to read the CLV-ladder bankroll from the ledger."""
    try:
        from wca.card import resolve_pool_bankroll

        pb = resolve_pool_bankroll(db_path)
        return float(pb.bankroll)
    except Exception as exc:
        logger.warning("Bankroll resolution failed (%s) — using default", exc)
        return 1500.0
