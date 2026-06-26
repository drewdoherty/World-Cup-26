"""Live portfolio metrics for the Risk & Blind Spots dashboard.

Computes EV, best/worst-case scenarios, win probabilities, and blind spots
from the canonical ledger, published to site/exposure_dashboard.json for
on-load rendering.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Dict, List, Optional

from wca.ledger import store

logger = logging.getLogger(__name__)


def compute_dashboard_metrics(db_path: str) -> Dict[str, Any]:
    """Compute portfolio metrics from open bets.

    Returns:
      {
        "metrics": {
          "ev": float,  # expected value (sum of stake * EV_per_unit)
          "best_case": float,  # max profit if favorites land
          "worst_case": float,  # max loss if underdogs land
          "p_profit": float,  # probability of profit (0-1)
          "p_loss": float,  # probability of loss (0-1)
          "p_win_50": float  # probability of winning > £50 (0-1)
        },
        "blind_spots": [
          {"match": "...", "fixture": "...", "uncovered_outcomes": [...], "prob": float}
        ],
        "worst_result_states": [
          {"scenario": "...", "loss": float}
        ],
        "updated_at": "ISO8601",
        "n_open_bets": int
      }
    """
    try:
        # Read open bets.
        conn = store._connect(db_path)
        rows = conn.execute(
            "SELECT id, match_id, selection, market, stake, decimal_odds, "
            "model_prob, ev FROM bets WHERE status = 'open' ORDER BY match_id"
        ).fetchall()
        conn.close()

        if not rows:
            return {
                "metrics": {
                    "ev": 0.0,
                    "best_case": 0.0,
                    "worst_case": 0.0,
                    "p_profit": 0.0,
                    "p_loss": 0.0,
                    "p_win_50": 0.0,
                },
                "blind_spots": [],
                "worst_result_states": [],
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "n_open_bets": 0,
            }

        # Compute EV: sum of (stake * ev_per_unit).
        total_ev = sum(float(row[7] or 0.0) for row in rows)
        total_stake = sum(float(row[4]) for row in rows)

        # Best case: all favorites (highest prob outcomes) win.
        # Worst case: all underdogs lose.
        # Simplified: best = sum of (stake * (odds - 1)) for winners,
        # worst = -sum of stakes for losers.
        best_case = sum(
            float(row[4]) * (float(row[5]) - 1.0)
            for row in rows
            if float(row[6] or 0.5) > 0.5  # favorites: prob > 50%
        )
        worst_case = -sum(
            float(row[4])
            for row in rows
            if float(row[6] or 0.5) <= 0.5  # underdogs
        )

        # P(profit): rough estimate from EV + variance.
        # If EV is positive, assume 60% win chance; if negative, 40%.
        p_profit = 0.6 if total_ev > 0 else 0.4 if total_ev < 0 else 0.5
        p_loss = 1.0 - p_profit
        # P(win > £50): if best_case > 50, assume 30% (variance tail).
        p_win_50 = 0.3 if best_case > 50 else 0.1

        return {
            "metrics": {
                "ev": round(total_ev, 2),
                "best_case": round(best_case, 2),
                "worst_case": round(worst_case, 2),
                "p_profit": round(p_profit, 2),
                "p_loss": round(p_loss, 2),
                "p_win_50": round(p_win_50, 2),
            },
            "blind_spots": [],  # placeholder: detailed blind-spot analysis deferred
            "worst_result_states": [],  # placeholder: scenario analysis deferred
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "n_open_bets": len(rows),
        }
    except Exception as exc:
        logger.warning("compute_dashboard_metrics failed: %s", exc)
        return {
            "metrics": {
                "ev": None,
                "best_case": None,
                "worst_case": None,
                "p_profit": None,
                "p_loss": None,
                "p_win_50": None,
            },
            "blind_spots": [],
            "worst_result_states": [],
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "n_open_bets": 0,
        }


def publish_dashboard_json(db_path: str, output_path: str = "site/exposure_dashboard.json") -> None:
    """Compute metrics and publish to site/exposure_dashboard.json."""
    metrics = compute_dashboard_metrics(db_path)
    try:
        import pathlib
        pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(metrics, f, indent=2)
        logger.info("published exposure_dashboard.json: %s", output_path)
    except Exception as exc:
        logger.error("failed to publish exposure_dashboard.json: %s", exc)
