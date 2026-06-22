"""Agent 6 — Adversarial Reviewer.

An LLM critic that attacks every recommended pick before it reaches Telegram.
Searches for missing injuries, lineup uncertainty, model weaknesses and
market-price traps.  A pick is BLOCKED unless this agent approves it.

Uses the Anthropic Messages API via ``requests`` (no SDK dependency, matching
the ``wca.bot.vision`` pattern).

Input:  DataPackage + TeamIntelligence + ModelOutput + EdgeReport
Output: AdversarialReview
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

from wca.agents.contracts import (
    AdversarialReview,
    DataPackage,
    EdgeReport,
    ModelOutput,
    TeamIntelligence,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1024

_SESSION = requests.Session()

_SYSTEM_PROMPT = """You are an adversarial sports-betting analyst. Your job is to
ATTACK a recommended bet — find every reason it might be wrong before it is
placed. Be a rigorous critic, not an advocate.

You will receive a JSON summary of:
- The fixture (home vs away, stage, kickoff)
- Team intelligence (injuries, absences, strength adjustments, tactical notes)
- Model output (Elo, Dixon-Coles, market blend probabilities)
- The top edge opportunity (market, selection, odds, model probability, EV)
- Recent news items

Your output must be a JSON object with these exact keys:
{
  "confidence_score": <integer 0-100>,
  "failure_modes": ["<string>", ...],
  "recommendation_adjustments": ["<string>", ...],
  "approved": <boolean>,
  "reviewer_reasoning": "<string>"
}

Rules:
- confidence_score: how confident you are that the pick has genuine edge (100 = certain).
  It combines model reliability, data quality, market liquidity and absence of red flags.
- failure_modes: concrete reasons the pick could be wrong (max 5 bullet strings).
  Always find at least 2 even for a strong pick — that is your job.
- recommendation_adjustments: actionable changes (e.g. "reduce stake 50% given lineup uncertainty").
- approved: true only if the pick survives scrutiny (confidence_score >= 55 AND no fatal failure modes).
  Fatal failure modes include: confirmed key player out that the model didn't know about,
  suspicious line move against the pick, very thin market liquidity, model input error.
- reviewer_reasoning: one short paragraph summarising your critique.

Return ONLY the JSON object — no markdown, no extra text."""


def run(
    pkg: DataPackage,
    team_intel: TeamIntelligence,
    model: ModelOutput,
    edges: EdgeReport,
    api_key: Optional[str] = None,
    model_id: str = DEFAULT_MODEL,
) -> AdversarialReview:
    """Run the adversarial review for the top pick in *edges*.

    Parameters
    ----------
    pkg, team_intel, model, edges:
        Pipeline outputs from Agents 1–5.
    api_key:
        Anthropic API key; falls back to ``ANTHROPIC_API_KEY`` env var.
    model_id:
        Claude model to use for the critique.
    """
    top = edges.top_pick
    if top is None:
        # Nothing to review — no pick reached this stage.
        return AdversarialReview(
            confidence_score=0.0,
            failure_modes=["No edge opportunity passed the threshold gate."],
            recommendation_adjustments=["Do not bet."],
            approved=False,
            reviewer_reasoning="No pick survived the edge threshold (Agent 5).",
        )

    payload = _build_payload(pkg, team_intel, model, edges)
    try:
        result = _call_claude(payload, api_key=api_key, model_id=model_id)
        return _parse_response(result)
    except Exception as exc:
        logger.warning("Adversarial review failed: %s — blocking pick", exc)
        return AdversarialReview(
            confidence_score=0.0,
            failure_modes=["Adversarial review unavailable: %s" % exc],
            recommendation_adjustments=["Treat as unreviewed — increase caution."],
            approved=False,
            reviewer_reasoning="Review call failed; pick blocked as a safety measure.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload(
    pkg: DataPackage,
    team_intel: TeamIntelligence,
    model: ModelOutput,
    edges: EdgeReport,
) -> Dict[str, Any]:
    top = edges.top_pick
    return {
        "fixture": {
            "home": pkg.fixture.home,
            "away": pkg.fixture.away,
            "kickoff": pkg.fixture.kickoff,
            "stage": pkg.fixture.stage,
        },
        "team_intelligence": {
            "strength_adjustments": team_intel.strength_adjustments,
            "player_availability": team_intel.player_availability,
            "tactical_notes": team_intel.tactical_notes,
        },
        "model_output": {
            "blend": {
                "home": round(model.win_prob, 4),
                "draw": round(model.draw_prob, 4),
                "away": round(model.loss_prob, 4),
            },
            "components": model.model_sources,
            "xg": {
                "home": model.expected_goals_home,
                "away": model.expected_goals_away,
            },
        },
        "top_pick": {
            "market": top.market,
            "selection": top.selection,
            "bookmaker": top.bookmaker,
            "odds": top.odds,
            "model_probability": top.model_probability,
            "implied_probability": top.implied_probability,
            "edge": round(top.edge, 4),
            "expected_value": round(top.expected_value, 4),
        } if top else None,
        "recent_news": [
            {"title": n.get("title", ""), "pub_date": n.get("pub_date", "")}
            for n in pkg.news_items[:8]
        ],
        "injuries_and_suspensions": [
            {"name": i.name, "team": i.team, "status": i.status, "reason": i.reason}
            for i in (pkg.injuries + pkg.suspensions)[:10]
        ],
    }


def _call_claude(
    payload: Dict[str, Any],
    api_key: Optional[str],
    model_id: str,
) -> str:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": model_id,
        "max_tokens": MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": "Review this pick:\n\n%s" % json.dumps(payload, indent=2),
            }
        ],
    }
    resp = _SESSION.post(API_URL, headers=headers, json=body, timeout=30)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError("Anthropic returned non-JSON: %s" % resp.text[:200]) from exc

    if not resp.ok:
        raise RuntimeError(
            "Anthropic API error %d: %s" % (resp.status_code, data.get("error", {}).get("message", "unknown"))
        )

    content = data.get("content", [])
    for block in content:
        if block.get("type") == "text":
            return block["text"]
    raise RuntimeError("No text content in Anthropic response")


def _parse_response(text: str) -> AdversarialReview:
    """Parse the Claude JSON response into :class:`AdversarialReview`."""
    # Strip markdown code fences if present.
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Could not parse adversarial JSON: %s — raw: %r" % (exc, text[:300])) from exc

    return AdversarialReview(
        confidence_score=float(obj.get("confidence_score", 0)),
        failure_modes=list(obj.get("failure_modes") or []),
        recommendation_adjustments=list(obj.get("recommendation_adjustments") or []),
        approved=bool(obj.get("approved", False)),
        reviewer_reasoning=str(obj.get("reviewer_reasoning", "")),
    )


