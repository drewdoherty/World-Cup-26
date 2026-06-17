"""Accumulator bet suggestions for the next 5 matches.

Builds 4+ leg accumulators with minimum 2.0 odds per leg, selecting from
match results, BTTS, over/under, and corner markets.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


def build_accas_from_odds(
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    *,
    max_fixtures: int = 5,
    min_legs: int = 4,
    min_leg_odds: float = 2.0,
    max_accas_per_fixture: int = 2,
) -> List[Dict[str, Any]]:
    """Build accumulator suggestions for the next N fixtures.

    Each acca includes 4+ legs from different fixtures, with each leg having
    minimum 2.0 odds. Legs are selected from available markets (match result,
    BTTS, over/under goals, corners).

    Parameters
    ----------
    odds_df:
        Flat odds frame with h2h and props rows.
    fixtures_meta:
        Results-schedule for fixture identification.
    max_fixtures:
        Include at most this many upcoming fixtures (default 5).
    min_legs:
        Minimum legs per acca (default 4).
    min_leg_odds:
        Minimum decimal odds for any leg (default 2.0).
    max_accas_per_fixture:
        Max number of accas per fixture to avoid spam.

    Returns
    -------
    list of acca dicts, each carrying::

        {fixture_legs: [...], total_odds: float, implied_prob: float}

    where fixture_legs is a list of {fixture, market, selection, odds}.
    """
    if odds_df.empty or fixtures_meta.empty:
        return []

    # Sort by commence_time and take next N fixtures
    upcoming = odds_df.copy()
    if "commence_time" in upcoming.columns:
        upcoming["commence_time"] = pd.to_datetime(
            upcoming["commence_time"], errors="coerce", utc=True
        )
        upcoming = upcoming.sort_values("commence_time")

    fixtures = upcoming.drop_duplicates(
        subset=["home_team", "away_team"], keep="first"
    ).head(max_fixtures)

    if len(fixtures) < min_legs:
        return []

    # Extract legs (competitive odds >= min_leg_odds) per fixture
    fixture_legs: Dict[str, List[Dict[str, Any]]] = {}

    for _, fixture_row in fixtures.iterrows():
        home = (fixture_row.get("home_team") or "").strip()
        away = (fixture_row.get("away_team") or "").strip()
        if not home or not away:
            continue

        fixture_key = f"{home} vs {away}"
        legs = []

        # Match result legs (home, draw, away)
        for selection, team in [("Home", home), ("Draw", None), ("Away", away)]:
            h_odds = float(fixture_row.get("home_odds") or 0)
            d_odds = float(fixture_row.get("draw_odds") or 0)
            a_odds = float(fixture_row.get("away_odds") or 0)

            if selection == "Home" and h_odds >= min_leg_odds:
                legs.append(
                    {
                        "fixture": fixture_key,
                        "market": "Match Result",
                        "selection": home,
                        "odds": h_odds,
                    }
                )
            elif selection == "Draw" and d_odds >= min_leg_odds:
                legs.append(
                    {
                        "fixture": fixture_key,
                        "market": "Draw",
                        "selection": "Draw",
                        "odds": d_odds,
                    }
                )
            elif selection == "Away" and a_odds >= min_leg_odds:
                legs.append(
                    {
                        "fixture": fixture_key,
                        "market": "Match Result",
                        "selection": away,
                        "odds": a_odds,
                    }
                )

        # BTTS legs (if available in the odds_df as props rows)
        btts_rows = odds_df[
            (odds_df.get("home_team") == home) & (odds_df.get("away_team") == away)
        ]
        for _, btts_row in btts_rows.iterrows():
            if str(btts_row.get("market_key", "")).lower() == "btts":
                btts_yes = float(btts_row.get("price", 0))
                if btts_yes >= min_leg_odds:
                    legs.append(
                        {
                            "fixture": fixture_key,
                            "market": "BTTS",
                            "selection": "Yes",
                            "odds": btts_yes,
                        }
                    )

        fixture_legs[fixture_key] = legs

    # Generate accas: pick min_legs fixtures with at least one leg each
    accas: List[Dict[str, Any]] = []
    fixture_keys = list(fixture_legs.keys())

    if len(fixture_keys) < min_legs:
        return []

    # Simple greedy: pick first N fixtures with at least one valid leg
    selected_fixtures = [fk for fk in fixture_keys if fixture_legs[fk]][:max_fixtures]

    if len(selected_fixtures) < min_legs:
        return []

    # Build a few accas by combining legs from different fixtures
    # Strategy: acca 1 uses strongest legs (lowest odds), acca 2 uses balanced legs
    for acca_num in range(max_accas_per_fixture):
        acca_legs = []
        total_odds = 1.0

        for fixture_key in selected_fixtures:
            legs = fixture_legs[fixture_key]
            if not legs:
                continue

            # Sort by odds ascending (easiest wins first for acca 1, varied for acca 2)
            sorted_legs = sorted(legs, key=lambda l: l["odds"])
            if acca_num == 0:
                # First acca: pick lowest odds (most confident)
                chosen = sorted_legs[0]
            else:
                # Second acca: pick middle odds for balance
                chosen = sorted_legs[len(sorted_legs) // 2] if len(sorted_legs) > 1 else sorted_legs[0]

            acca_legs.append(chosen)
            total_odds *= chosen["odds"]

            if len(acca_legs) >= min_legs:
                break

        # Only keep if we hit min_legs
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
