"""Accumulator bet suggestions for the next 5 matches.

Builds 4+ leg accumulators with minimum 2.0 odds per leg, selecting from
match result markets in the scores_data.json feed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def build_accas_from_odds(
    scores_feed: dict,
    fixtures_meta: Any = None,  # kept for caller compatibility, not used
    *,
    max_fixtures: int = 5,
    min_legs: int = 4,
    min_leg_odds: float = 2.0,
    max_accas_per_fixture: int = 2,
) -> List[Dict[str, Any]]:
    """Build accumulator suggestions from the scores_data.json feed.

    ``scores_feed`` is the dict returned by :func:`wca.boosts.load_scores_feed`
    (``{"meta":..., "fixtures":[...]}``) where each fixture carries a ``venues``
    list with per-book ``selection_prices``.  Best (max) odds across all books
    are used for each outcome; legs with odds < ``min_leg_odds`` are skipped.

    Returns a list of acca dicts ``{legs:[...], total_odds:float,
    implied_prob:float}`` where each leg is
    ``{fixture, market, selection, odds}``.
    """
    fixtures = (scores_feed or {}).get("fixtures") or []
    if not fixtures:
        return []

    # Take the first N fixtures (the feed is already ordered by kickoff time).
    fixtures = fixtures[:max_fixtures]

    fixture_legs: Dict[str, List[Dict[str, Any]]] = {}

    for fx in fixtures:
        fixture_name = (fx.get("fixture") or "").strip()
        if not fixture_name:
            continue

        venues = fx.get("venues") or []

        # Best (highest) odds for each outcome across all books.
        best_home = max(
            (float(v.get("selection_prices", {}).get("home") or 0) for v in venues),
            default=0.0,
        )
        best_draw = max(
            (float(v.get("selection_prices", {}).get("draw") or 0) for v in venues),
            default=0.0,
        )
        best_away = max(
            (float(v.get("selection_prices", {}).get("away") or 0) for v in venues),
            default=0.0,
        )

        # Split "Home vs Away" into team names for the selection label.
        parts = _VS_RE.split(fixture_name, maxsplit=1)
        home_name = parts[0].strip() if len(parts) == 2 else fixture_name
        away_name = parts[1].strip() if len(parts) == 2 else ""

        legs: List[Dict[str, Any]] = []
        if best_home >= min_leg_odds:
            legs.append(
                {"fixture": fixture_name, "market": "Match Result",
                 "selection": home_name, "odds": best_home}
            )
        if best_draw >= min_leg_odds:
            legs.append(
                {"fixture": fixture_name, "market": "Draw",
                 "selection": "Draw", "odds": best_draw}
            )
        if best_away >= min_leg_odds:
            legs.append(
                {"fixture": fixture_name, "market": "Match Result",
                 "selection": away_name, "odds": best_away}
            )
        fixture_legs[fixture_name] = legs

    if len(fixture_legs) < min_legs:
        return []

    # Keep only fixtures that have at least one valid leg.
    selected = [fk for fk in fixture_legs if fixture_legs[fk]][:max_fixtures]
    if len(selected) < min_legs:
        return []

    accas: List[Dict[str, Any]] = []
    for acca_num in range(max_accas_per_fixture):
        acca_legs: List[Dict[str, Any]] = []
        total_odds = 1.0

        for fk in selected:
            legs = fixture_legs[fk]
            sorted_legs = sorted(legs, key=lambda l: l["odds"])
            # acca 0: most likely leg (lowest odds); acca 1: balanced mid-odds leg.
            if acca_num == 0:
                chosen = sorted_legs[0]
            else:
                chosen = sorted_legs[len(sorted_legs) // 2] if len(sorted_legs) > 1 else sorted_legs[0]

            acca_legs.append(chosen)
            total_odds *= chosen["odds"]

            if len(acca_legs) >= min_legs:
                break

        if len(acca_legs) >= min_legs:
            accas.append(
                {
                    "legs": acca_legs,
                    "total_odds": total_odds,
                    "implied_prob": 1.0 / total_odds if total_odds > 0 else 0.0,
                }
            )

    return accas


def format_accas(accas: List[Dict[str, Any]]) -> str:
    """Format accas as a human-readable Telegram message."""
    if not accas:
        return "*Accumulators*\nNo +EV accas found for the next 5 matches."

    lines = ["🎯 *Accumulators (next 5 matches):*", ""]

    for i, acca in enumerate(accas, 1):
        legs = acca.get("legs", [])
        total_odds = float(acca.get("total_odds", 0))
        implied_prob = float(acca.get("implied_prob", 0))

        lines.append(f"*Acca {i}:* {total_odds:.2f} @ {implied_prob*100:.1f}% implied")

        for j, leg in enumerate(legs, 1):
            fixture = leg.get("fixture", "?")
            market = leg.get("market", "?")
            selection = leg.get("selection", "?")
            odds = float(leg.get("odds", 0))
            lines.append(f"  {j}. {fixture} — {selection} ({market}) @ {odds:.2f}")

        lines.append("")

    return "\n".join(lines)
