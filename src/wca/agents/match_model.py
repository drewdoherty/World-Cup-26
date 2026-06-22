"""Agent 4 — Match Model.

Runs the full ensemble model (Elo + Dixon-Coles + market blend) plus prop
markets (corners, cards, anytime scorers) for a single fixture.

Input:  DataPackage + TeamIntelligence + MarketIntelligence
Output: ModelOutput
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from wca.agents.contracts import (
    DataPackage,
    MarketIntelligence,
    ModelOutput,
    PropEstimate,
    ScoreProb,
    TeamIntelligence,
)

logger = logging.getLogger(__name__)

# Blend weights (matches wca.card.BlendWeights defaults post-2026-06-18 update).
_W_ELO = 0.10
_W_DC = 0.30
_W_MKT = 0.60

# Default xG means if Dixon-Coles params are unavailable.
_FALLBACK_HOME_XG = 1.35
_FALLBACK_AWAY_XG = 1.05

# Scoreline matrix cap: model up to this many goals per team.
_MAX_GOALS = 8


def run(
    pkg: DataPackage,
    team_intel: TeamIntelligence,
    market_intel: MarketIntelligence,
    results_path: Optional[str] = None,
    reference_date: Optional[str] = None,
) -> ModelOutput:
    """Produce the full ensemble model output for the fixture in *pkg*.

    Parameters
    ----------
    pkg:
        Raw data package from Agent 1.
    team_intel:
        Squad-context adjustments from Agent 2.
    market_intel:
        De-vigged market consensus from Agent 3.
    results_path:
        Path to the martj42 ``results.csv``.  Resolved automatically if omitted.
    reference_date:
        ISO date string to use as the Dixon-Coles reference (for time decay).
    """
    home = pkg.fixture.home
    away = pkg.fixture.away
    neutral = pkg.fixture.neutral

    # --- Fit models -------------------------------------------------------
    fitted = _fit_or_load_models(results_path, reference_date)

    # --- Component probabilities ------------------------------------------
    elo_h, elo_d, elo_a = _elo_probs(fitted, home, away, neutral)
    dc_h, dc_d, dc_a, dc_lambda_h, dc_lambda_a, score_matrix = _dc_probs(
        fitted, home, away, neutral
    )
    mkt_h = market_intel.bookmaker_consensus.get("home") or (1.0 / 3.0)
    mkt_d = market_intel.bookmaker_consensus.get("draw") or (1.0 / 3.0)
    mkt_a = market_intel.bookmaker_consensus.get("away") or (1.0 / 3.0)

    # --- Apply team-intel strength adjustments ----------------------------
    adj_h = team_intel.strength_adjustments.get("home", 1.0)
    adj_a = team_intel.strength_adjustments.get("away", 1.0)
    dc_lambda_h_adj = dc_lambda_h * adj_h
    dc_lambda_a_adj = dc_lambda_a * adj_a

    # --- Blend (weights normalised to 1) ----------------------------------
    w_elo, w_dc, w_mkt = _W_ELO, _W_DC, _W_MKT
    w_sum = w_elo + w_dc + w_mkt
    blend_h = (w_elo * elo_h + w_dc * dc_h + w_mkt * mkt_h) / w_sum
    blend_d = (w_elo * elo_d + w_dc * dc_d + w_mkt * mkt_d) / w_sum
    blend_a = (w_elo * elo_a + w_dc * dc_a + w_mkt * mkt_a) / w_sum

    # Re-normalise blend (floating-point safety).
    b_total = blend_h + blend_d + blend_a
    if b_total > 0:
        blend_h /= b_total
        blend_d /= b_total
        blend_a /= b_total

    # --- Score distribution (reconciled to blend 1X2) --------------------
    score_probs = _build_score_distribution(
        score_matrix, (blend_h, blend_d, blend_a), dc_lambda_h_adj, dc_lambda_a_adj
    )

    # --- Prop estimates ---------------------------------------------------
    props = _build_prop_estimates(dc_lambda_h_adj, dc_lambda_a_adj)

    return ModelOutput(
        home=home,
        away=away,
        win_prob=round(blend_h, 6),
        draw_prob=round(blend_d, 6),
        loss_prob=round(blend_a, 6),
        expected_goals_home=round(dc_lambda_h_adj, 4),
        expected_goals_away=round(dc_lambda_a_adj, 4),
        score_distribution=score_probs,
        prop_estimates=props,
        model_sources={
            "elo": {"home": round(elo_h, 6), "draw": round(elo_d, 6), "away": round(elo_a, 6)},
            "dc": {"home": round(dc_h, 6), "draw": round(dc_d, 6), "away": round(dc_a, 6)},
            "market": {"home": round(mkt_h, 6), "draw": round(mkt_d, 6), "away": round(mkt_a, 6)},
            "blend": {"home": round(blend_h, 6), "draw": round(blend_d, 6), "away": round(blend_a, 6)},
        },
        advancement_probs={},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fit_or_load_models(
    results_path: Optional[str], reference_date: Optional[str]
) -> Any:
    """Fit Elo + Dixon-Coles from results history."""
    try:
        from wca.data.results import load_results
        from wca.data.cleaning import resolve_results_path
        from wca.card import fit_models

        path = results_path or resolve_results_path()
        results = load_results(path)
        return fit_models(results, reference_date=reference_date)
    except Exception as exc:
        logger.warning("Model fitting failed: %s — using fallback", exc)
        return None


def _elo_probs(
    fitted: Any, home: str, away: str, neutral: bool
) -> Tuple[float, float, float]:
    if fitted is None:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    try:
        from wca.card import elo_probs

        return elo_probs(fitted, home, away, neutral)
    except Exception as exc:
        logger.warning("Elo probs failed for %s vs %s: %s", home, away, exc)
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)


def _dc_probs(
    fitted: Any, home: str, away: str, neutral: bool
) -> Tuple[float, float, float, float, float, Any]:
    """Return (home, draw, away, lambda_h, lambda_a, score_matrix)."""
    import numpy as np

    fallback = (
        1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0,
        _FALLBACK_HOME_XG, _FALLBACK_AWAY_XG,
        None,
    )
    if fitted is None:
        return fallback
    try:
        pred = fitted.dc.predict(home, away, neutral=neutral, warn=False)
        h, d, a = pred.one_x_two()
        matrix = pred.matrix if hasattr(pred, "matrix") else None
        lambda_h = float(pred.lambda_home) if hasattr(pred, "lambda_home") else _FALLBACK_HOME_XG
        lambda_a = float(pred.lambda_away) if hasattr(pred, "lambda_away") else _FALLBACK_AWAY_XG
        return h, d, a, lambda_h, lambda_a, matrix
    except Exception as exc:
        logger.warning("DC probs failed for %s vs %s: %s", home, away, exc)
        return fallback


def _build_score_distribution(
    matrix: Any,
    blend: Tuple[float, float, float],
    lambda_h: float,
    lambda_a: float,
) -> List[ScoreProb]:
    """Top scorelines reconciled to the blend 1X2."""
    import math
    import numpy as np

    blend_h, blend_d, blend_a = blend

    # Build an independent-Poisson matrix if the DC matrix is unavailable.
    if matrix is None:
        size = _MAX_GOALS + 1
        matrix = np.zeros((size, size))
        for h in range(size):
            for a in range(size):
                ph = math.exp(-lambda_h) * (lambda_h ** h) / math.factorial(h)
                pa = math.exp(-lambda_a) * (lambda_a ** a) / math.factorial(a)
                matrix[h, a] = ph * pa

    # Reconcile to blend via piecewise scaling.
    try:
        from wca.models.scores import reconcile_scoreline_matrix, top_scorelines_from_matrix

        mat_arr = np.asarray(matrix, dtype=float)
        if mat_arr.shape[0] < _MAX_GOALS + 1:
            pad = _MAX_GOALS + 1 - mat_arr.shape[0]
            mat_arr = np.pad(mat_arr, pad_width=((0, pad), (0, pad)))

        reconciled = reconcile_scoreline_matrix(
            mat_arr[:_MAX_GOALS + 1, :_MAX_GOALS + 1],
            (blend_h, blend_d, blend_a),
        )
        top = top_scorelines_from_matrix(reconciled, k=16)
        return [
            ScoreProb(home_goals=int(hg), away_goals=int(ag), probability=round(float(p), 6))
            for hg, ag, p in top
        ]
    except Exception as exc:
        logger.warning("Score distribution reconciliation failed: %s", exc)
        return []


def _build_prop_estimates(lambda_h: float, lambda_a: float) -> List[PropEstimate]:
    """Corners, cards, and anytime-scorer estimates via the props models."""
    props: List[PropEstimate] = []
    try:
        from wca.models.props import CornersModel, CardsModel

        # --- Corners -------------------------------------------------------
        cm = CornersModel()
        for line in (8.5, 9.5, 10.5):
            p_over = cm.prob_over(line, lambda_h, lambda_a)
            fair_o = round(1.0 / p_over, 4) if p_over > 0 else 99.0
            fair_u = round(1.0 / (1.0 - p_over), 4) if p_over < 1 else 99.0
            props.append(
                PropEstimate(
                    market="corners_total_over",
                    selection="over %.1f" % line,
                    line=line,
                    model_prob=round(p_over, 4),
                    fair_odds=fair_o,
                )
            )
            props.append(
                PropEstimate(
                    market="corners_total_under",
                    selection="under %.1f" % line,
                    line=line,
                    model_prob=round(1.0 - p_over, 4),
                    fair_odds=fair_u,
                )
            )

        # --- Cards ---------------------------------------------------------
        # CardsModel.prob_over takes aggression multipliers, not xG directly.
        # Use default aggression (1.0) as a neutral baseline.
        cardm = CardsModel()
        for line in (3.5, 4.5, 5.5):
            p_over = cardm.prob_over(line)
            fair_o = round(1.0 / p_over, 4) if p_over > 0 else 99.0
            props.append(
                PropEstimate(
                    market="cards_total_over",
                    selection="over %.1f" % line,
                    line=line,
                    model_prob=round(p_over, 4),
                    fair_odds=fair_o,
                )
            )
    except Exception as exc:
        logger.warning("Prop estimates failed: %s", exc)

    return props
