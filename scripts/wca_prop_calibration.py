#!/usr/bin/env python
"""Build ``data/prop_calibration.json`` — per-fixture corners/cards calibration.

Why this exists
----------------
``scripts/wca_betrecs.py`` has read ``data/prop_calibration.json`` (via
``--prop-cal``, default ``data/prop_calibration.json``) since the Action Desk
feed was written, but nothing ever generated that file — ``event_props`` in
``site/bet_recs.json`` has therefore been permanently empty (every fixture
falls straight into the honest "no live book price snapshot" withheld rows in
``build_event_props``). This script closes that gap: it feeds each upcoming
fixture's blended Elo+DC goal expectations (``lambda_home``/``lambda_away``
from ``data/model_predictions.json``) through :class:`wca.models.props.CornersModel`
and :class:`wca.models.props.CardsModel` — refit 2026-07-03 on STRICTLY
90-minute StatsBomb data (see ``docs/research/handicap_corners_cards_verdicts.md``)
— and, when available, per-team empirical-Bayes priors from
``wca.data.matchevents.load_priors()`` (falls back to hard-coded league
constants when ``data/processed/prop_priors.csv`` is absent, so this script
never fabricates a number it can't source).

This ONLY populates the model-calibration side. ``build_event_props`` still
correctly withholds every prop from real cash (corners/cards remain
FREE-BET-ONLY per CLAUDE.md — no live sportsbook price snapshot exists to
compare against), but the withheld rows now carry a real calibrated mean +
fair O/U odds instead of standing for a scanner that never ran.

Usage::

    .venv/bin/python scripts/wca_prop_calibration.py \\
        [--predictions data/model_predictions.json] \\
        [--priors data/processed/prop_priors.csv] \\
        [--out data/prop_calibration.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import matchevents  # noqa: E402
from wca.data.teamnames import canonical  # noqa: E402
from wca.models.props import CardsModel, CornersModel, FoulsModel  # noqa: E402

# Over/under lines shown in the calibration payload (half-integers per house
# convention — see props.py module docstring).
CORNERS_LINES = [8.5, 9.5, 10.5]
CARDS_LINES = [2.5, 3.5, 4.5]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fixture_teams(fixture: str) -> Optional[List[str]]:
    parts = fixture.split(" vs ")
    if len(parts) != 2:
        return None
    return parts


def _corners_block(model: CornersModel, lam_h: float, lam_a: float,
                   home: Optional[str], away: Optional[str]) -> Dict[str, Any]:
    mean = model.mean_total(lam_h, lam_a, home=home, away=away)
    block: Dict[str, Any] = {"mean": round(mean, 3)}
    for line in CORNERS_LINES:
        over, _under = model.fair_odds_over_under(line, lam_h, lam_a, home=home, away=away)
        key = "o%s_fair_over" % line
        block[key] = round(over, 3) if over != float("inf") else None
    return block


def _cards_block(model: CardsModel, agg_home: float, agg_away: float) -> Dict[str, Any]:
    mean = model.mean_total(aggression_home=agg_home, aggression_away=agg_away)
    block: Dict[str, Any] = {"mean": round(mean, 3)}
    for line in CARDS_LINES:
        over, _under = model.fair_odds_over_under(
            line, aggression_home=agg_home, aggression_away=agg_away)
        key = "o%s_fair_over" % line
        block[key] = round(over, 3) if over != float("inf") else None
    return block


def build_calibration(predictions: List[Dict[str, Any]],
                      priors: Optional[Dict[str, dict]] = None,
                      max_fixtures: Optional[int] = None) -> Dict[str, Any]:
    """Build the ``fixtures`` list for prop_calibration.json.

    ``predictions`` is the ``fixtures`` array from ``data/model_predictions.json``
    (each row: ``fixture``, ``kickoff``, ``lambda_home``, ``lambda_away``).
    ``priors`` is a ``matchevents.load_priors()``-shaped dict; when ``None``
    the models fall back to their hard-coded 90-min-refit constants (never
    fabricated — see ``props.py`` FALLBACK_* constants).
    """
    corners_model = CornersModel(team_priors=priors)
    cards_model = CardsModel()
    fouls_model = FoulsModel(team_priors=priors)

    fixtures_out: List[Dict[str, Any]] = []
    rows = predictions or []
    if max_fixtures is not None:
        rows = rows[:max_fixtures]

    for fix in rows:
        fixture = fix.get("fixture") or ""
        lam_h = fix.get("lambda_home")
        lam_a = fix.get("lambda_away")
        if lam_h is None or lam_a is None or not fixture:
            continue
        lam_h = float(lam_h)
        lam_a = float(lam_a)

        teams = _fixture_teams(fixture)
        home = canonical(teams[0]) if teams else None
        away = canonical(teams[1]) if teams else None

        # Cards aggression coupling: team foul-rate priors -> aggression
        # multiplier (fouls<->cards r=0.508, CardsModel.aggression_from_fouls).
        # Falls back to aggression=1.0 (base rate, no-op) when no team is
        # resolvable or no fouls prior is injected — never fabricated.
        agg_home = cards_model.aggression_from_fouls(
            fouls_model.team_mean(home, away) if home else None)
        agg_away = cards_model.aggression_from_fouls(
            fouls_model.team_mean(away, home) if away else None)

        fixtures_out.append({
            "fixture": fixture,
            "kickoff": fix.get("kickoff"),
            "lambda_home": round(lam_h, 4),
            "lambda_away": round(lam_a, 4),
            "corners": _corners_block(corners_model, lam_h, lam_a, home, away),
            "cards": _cards_block(cards_model, agg_home, agg_away),
        })

    return {
        "meta": {
            "generated": _now().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "method": (
                "CornersModel/CardsModel (src/wca/models/props.py), refit "
                "2026-07-03 on 90-minute-only StatsBomb WC18+22 data; team "
                "priors from wca.data.matchevents.load_priors() when "
                "data/processed/prop_priors.csv is present, else hard-coded "
                "league fallbacks."
            ),
            "n_fixtures": len(fixtures_out),
            "n_team_priors": len(priors) if priors else 0,
            "corners_lines": CORNERS_LINES,
            "cards_lines": CARDS_LINES,
            "cash_status": (
                "FREE-BET-ONLY / no real money — no live sportsbook price "
                "snapshot exists for corners/cards (see CLAUDE.md); "
                "build_event_props in wca_betrecs.py withholds these from "
                "the actionable feed until a book-price feed is wired."
            ),
        },
        "fixtures": fixtures_out,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build data/prop_calibration.json (corners/cards calibration).")
    ap.add_argument("--predictions", default="data/model_predictions.json")
    ap.add_argument("--priors", default=matchevents.DEFAULT_PRIORS_PATH)
    ap.add_argument("--out", default="data/prop_calibration.json")
    ap.add_argument("--max-fixtures", type=int, default=None,
                    help="Cap the number of fixtures processed (testing).")
    args = ap.parse_args(argv)

    try:
        with open(args.predictions, encoding="utf-8") as fh:
            pred_raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print("predictions unreadable (%s); writing empty calibration" % exc,
              file=sys.stderr)
        pred_raw = {"fixtures": []}

    predictions = pred_raw.get("fixtures") or []
    priors = matchevents.load_priors(args.priors)

    data = build_calibration(predictions, priors=priors, max_fixtures=args.max_fixtures)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print("%s: %d fixtures, %d team priors"
          % (args.out, len(data["fixtures"]), data["meta"]["n_team_priors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
