"""Accumulator bet suggestions for the next 5 matches.

Builds 4+ leg accumulators using model fair odds (1 / model_1x2 probability)
derived from site/scores_data.json.  Each leg must clear min_leg_odds (2.0).
Legs priced outside [min_leg_odds, _LONGSHOT_MAX_ODDS] are excluded; the
rung-0 longshot guard (odds > 10x / price < 0.10) is applied per Rule 4.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Rung-0 longshot guard: exclude any leg priced < 0.10 (odds > 10x).
_LONGSHOT_MAX_ODDS = 10.0


def load_odds_df(path: str = "site/scores_data.json") -> pd.DataFrame:
    """Read the scores feed and return a flat odds DataFrame.

    Reads ``path`` (scores_data.json), splits each fixture string into
    ``home_team`` / ``away_team``, and converts ``model_1x2`` probabilities
    to fair decimal odds (1/p) as ``home_odds``, ``draw_odds``, ``away_odds``.
    Returns an empty DataFrame on any failure or missing file.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            feed = json.load(fh)
    except (OSError, ValueError):
        return pd.DataFrame()

    if not isinstance(feed, dict):
        return pd.DataFrame()

    fixtures = feed.get("fixtures") or []
    rows = []
    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        fixture_str = (fx.get("fixture") or "").strip()
        if not fixture_str:
            continue
        # Split "Home vs Away" or "Home v Away".
        parts = re.split(r"\s+vs?\s+", fixture_str, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            continue
        home, away = parts[0].strip(), parts[1].strip()
        m1x2 = fx.get("model_1x2") or {}
        home_p = float(m1x2.get("home") or 0)
        draw_p = float(m1x2.get("draw") or 0)
        away_p = float(m1x2.get("away") or 0)
        rows.append({
            "home_team": home,
            "away_team": away,
            "commence_time": (fx.get("commence_time") or fx.get("kickoff") or ""),
            "home_odds": round(1.0 / home_p, 4) if home_p > 0 else 0.0,
            "draw_odds": round(1.0 / draw_p, 4) if draw_p > 0 else 0.0,
            "away_odds": round(1.0 / away_p, 4) if away_p > 0 else 0.0,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def load_model_scorer_legs(
    path: str = "data/model_scorers.json",
    min_leg_odds: float = 2.0,
    max_leg_odds: float = _LONGSHOT_MAX_ODDS,
) -> List[Dict[str, Any]]:
    """Model-priced anytime-scorer legs from the unified players-model source.

    Reads ``data/model_scorers.json`` (written by the build from the SAME model
    that powers /next and /goalscorers — one source of truth) and returns
    acca-ready legs whose fair odds fall in ``[min_leg_odds, max_leg_odds]``.
    Each leg is labelled "model price, no market" so the acca card can show that
    these legs are model-derived. Returns ``[]`` on any failure/missing file so
    /accas degrades to 1X2-only.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            feed = json.load(fh)
    except (OSError, ValueError):
        return []
    fixtures = feed.get("fixtures") if isinstance(feed, dict) else None
    if not fixtures:
        return []
    legs: List[Dict[str, Any]] = []
    for fx in fixtures:
        if not isinstance(fx, dict):
            continue
        fixture = fx.get("fixture", "")
        for side in ("home_scorers", "away_scorers"):
            for s in fx.get(side) or []:
                fair = float(s.get("fair_anytime") or 0.0)
                if not (min_leg_odds <= fair <= max_leg_odds):
                    continue
                legs.append({
                    "fixture": fixture,
                    "selection": "%s to score anytime" % s.get("player", ""),
                    "team": s.get("team", ""),
                    "market": "anytime_scorer",
                    "prob": float(s.get("p_anytime") or 0.0),
                    "fair_odds": round(fair, 4),
                    "label": s.get("label", "model price, no market"),
                    "source": s.get("share_source", ""),
                })
    legs.sort(key=lambda l: l["prob"], reverse=True)
    return legs


def build_accas_from_odds(
    odds_df: pd.DataFrame,
    *,
    max_fixtures: int = 5,
    min_legs: int = 4,
    min_leg_odds: float = 2.0,
    max_accas_per_fixture: int = 2,
) -> List[Dict[str, Any]]:
    """Build accumulator suggestions for the next N fixtures.

    Each acca includes min_legs legs from different fixtures, one leg per
    fixture.  Legs with odds < min_leg_odds or > _LONGSHOT_MAX_ODDS (rung-0
    longshot guard) are excluded; excluded legs are recorded in the returned
    dict so format_accas can surface them.

    Parameters
    ----------
    odds_df:
        Flat odds frame produced by :func:`load_odds_df` (or compatible shape
        with home_team, away_team, home_odds, draw_odds, away_odds columns).
    max_fixtures:
        Include at most this many upcoming fixtures (default 5).
    min_legs:
        Minimum legs per acca (default 4).
    min_leg_odds:
        Minimum decimal odds for any leg (default 2.0).
    max_accas_per_fixture:
        Max number of accas to build.

    Returns
    -------
    list of acca dicts, each carrying::

        {
          "legs": [{fixture, market, selection, odds}, ...],
          "total_odds": float,
          "implied_prob": float,
          "excluded_longshot": [{fixture, selection, odds, reason}, ...],
        }
    """
    if odds_df.empty:
        return []

    # Sort by commence_time and take next N fixtures.
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

    # Extract qualifying legs per fixture; track longshot exclusions globally.
    fixture_legs: Dict[str, List[Dict[str, Any]]] = {}
    excluded_longshot: List[Dict[str, Any]] = []

    for _, fixture_row in fixtures.iterrows():
        home = (fixture_row.get("home_team") or "").strip()
        away = (fixture_row.get("away_team") or "").strip()
        if not home or not away:
            continue

        fixture_key = "%s vs %s" % (home, away)
        legs: List[Dict[str, Any]] = []

        for selection, odds_key in [
            ("Home", "home_odds"),
            ("Draw", "draw_odds"),
            ("Away", "away_odds"),
        ]:
            odds = float(fixture_row.get(odds_key) or 0)
            label = home if selection == "Home" else (away if selection == "Away" else "Draw")
            market = "Match Result" if selection != "Draw" else "Draw"

            if odds <= 0:
                continue
            if odds > _LONGSHOT_MAX_ODDS:
                excluded_longshot.append({
                    "fixture": fixture_key,
                    "selection": label,
                    "odds": odds,
                    "reason": "odds %.2f > %.1f longshot cap (price < 0.10)" % (
                        odds, _LONGSHOT_MAX_ODDS
                    ),
                })
                continue
            if odds >= min_leg_odds:
                legs.append({
                    "fixture": fixture_key,
                    "market": market,
                    "selection": label,
                    "odds": odds,
                })

        # BTTS props rows (only present when odds_df has market_key + price cols).
        if "market_key" in odds_df.columns:
            btts_rows = odds_df[
                (odds_df["home_team"] == home) & (odds_df["away_team"] == away)
            ]
            for _, btts_row in btts_rows.iterrows():
                if str(btts_row.get("market_key", "")).lower() == "btts":
                    btts_yes = float(btts_row.get("price", 0))
                    if btts_yes > _LONGSHOT_MAX_ODDS:
                        excluded_longshot.append({
                            "fixture": fixture_key,
                            "selection": "BTTS Yes",
                            "odds": btts_yes,
                            "reason": "odds %.2f > %.1f longshot cap (price < 0.10)" % (
                                btts_yes, _LONGSHOT_MAX_ODDS
                            ),
                        })
                    elif btts_yes >= min_leg_odds:
                        legs.append({
                            "fixture": fixture_key,
                            "market": "BTTS",
                            "selection": "Yes",
                            "odds": btts_yes,
                        })

        fixture_legs[fixture_key] = legs

    # Generate accas: pick fixtures with at least one qualifying leg.
    accas: List[Dict[str, Any]] = []
    fixture_keys = list(fixture_legs.keys())

    if len(fixture_keys) < min_legs:
        return []

    selected_fixtures = [fk for fk in fixture_keys if fixture_legs[fk]][:max_fixtures]

    if len(selected_fixtures) < min_legs:
        return []

    for acca_num in range(max_accas_per_fixture):
        acca_legs: List[Dict[str, Any]] = []
        total_odds = 1.0

        for fixture_key in selected_fixtures:
            legs = fixture_legs[fixture_key]
            if not legs:
                continue

            sorted_legs = sorted(legs, key=lambda l: l["odds"])
            if acca_num == 0:
                # Acca 1: most confident (lowest-odds) qualifying leg per fixture.
                chosen = sorted_legs[0]
            else:
                # Acca 2: middle-odds leg for balance.
                chosen = sorted_legs[len(sorted_legs) // 2] if len(sorted_legs) > 1 else sorted_legs[0]

            acca_legs.append(chosen)
            total_odds *= chosen["odds"]

            if len(acca_legs) >= min_legs:
                break

        if len(acca_legs) >= min_legs:
            accas.append({
                "legs": acca_legs,
                "total_odds": total_odds,
                "implied_prob": 1.0 / total_odds if total_odds > 0 else 0.0,
                "excluded_longshot": excluded_longshot,
            })

    return accas


def format_accas(accas: List[Dict[str, Any]]) -> str:
    """Format accas as a human-readable Telegram message."""
    if not accas:
        return "*Accumulators*\nNo +EV accas found for the next 5 matches."

    lines = ["🎯 *Accumulators (next 5 matches):*", ""]
    lines.append("_Odds are model fair values (1/model\\_prob), not market prices._")
    lines.append("_Back only if market ask ≥ fair; verify live before placing._")
    lines.append("")

    for i, acca in enumerate(accas, 1):
        legs = acca.get("legs", [])
        total_odds = float(acca.get("total_odds", 0))
        implied_prob = float(acca.get("implied_prob", 0))

        lines.append("*Acca %d:* `%.2f` @ %.1f%% implied" % (i, total_odds, implied_prob * 100))

        for j, leg in enumerate(legs, 1):
            fixture = leg.get("fixture", "?")
            market = leg.get("market", "?")
            selection = leg.get("selection", "?")
            odds = float(leg.get("odds", 0))
            lines.append("  %d. %s — %s (%s) @ `%.2f`" % (j, fixture, selection, market, odds))

        lines.append("")

    # Adversarial line: rejected legs + bear case.
    excluded = accas[0].get("excluded_longshot") or []
    if excluded:
        lines.append("*Excluded — rung-0 longshot guard (odds > 10x):*")
        for ex in excluded[:6]:
            lines.append("  – %s — %s @ %.2f: %s" % (
                ex.get("fixture", "?"), ex.get("selection", "?"),
                float(ex.get("odds", 0)), ex.get("reason", ""),
            ))
        lines.append("")

    lines.append(
        "⚡ _Bear case: same-tournament fixtures are correlated — acca edge is "
        "overstated without a correlation model. No live spread checked; check "
        "market ask vs fair before placing. Model inputs from last cron build._"
    )

    return "\n".join(lines)
