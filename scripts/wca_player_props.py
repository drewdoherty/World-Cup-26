#!/usr/bin/env python
"""Price Polymarket player-prop markets vs our model for a live fixture.

Unlocks the largest untapped Polymarket surface: the per-match
``"<Home> vs. <Away> - Player Props"`` events (anytime/2+/3+ goals, 1+/2+ shots,
1+/2+ shots on target, 1+ assists). This script wires the pure model in
:mod:`wca.models.playerprops` to live Gamma-API prices and prints model
probability vs PM YES price + edge for the top players.

    # list live World-Cup fixtures that have a PM Player-Props event
    PYTHONPATH=src python3 scripts/wca_player_props.py list

    # price one fixture (team names match site/scores_data.json + Polymarket)
    PYTHONPATH=src python3 scripts/wca_player_props.py price --home "Argentina" --away "France"
    PYTHONPATH=src python3 scripts/wca_player_props.py price --home Argentina --away France \
        --min-edge 0.03 --top 15 --json

Model probabilities come from:
  * data/players.json  -> analyst npxg_share/minutes (GOALS family + cascade)
  * data/players.db    -> StatsBomb per-90 SoT (if present; optional)
  * site/scores_data.json -> per-team expected goals (lambda) for the fixture

HONEST LIMITS: the dominant uncertainty is lineup/minutes, not the count math.
Expected minutes are analyst estimates (no live lineup feed); rate_source /
minutes_source are printed on every row so you can gate on provenance. Treat any
single-prop edge as dominated by start/minutes risk.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data import polymarket as P            # noqa: E402
from wca.data.teamnames import canonical        # noqa: E402
from wca.models import playerprops as PPM        # noqa: E402
from wca.models.scorers import load_player_overrides, PlayerParams  # noqa: E402

_SCORES = os.path.join(_ROOT, "site", "scores_data.json")
_PLAYERS_JSON = os.path.join(_ROOT, "data", "players.json")
_PLAYERS_DB = os.path.join(_ROOT, "data", "players.db")


# --------------------------------------------------------------------------- model inputs


def _load_scores() -> List[dict]:
    if not os.path.exists(_SCORES):
        return []
    with open(_SCORES) as fh:
        return (json.load(fh) or {}).get("fixtures", [])


def _fixture_lambdas(home: str, away: str) -> Optional[Tuple[float, float, str]]:
    """Per-team expected goals (lambda_home, lambda_away) for a fixture.

    Derived from site/scores_data.json. Preferred source: the exact-score
    distribution (sum of prob*home_goals / prob*away_goals gives each team's
    expected goals directly). Falls back to splitting the over/under total by
    the 1X2 implied strength when no scores array is present. Returns
    ``(lh, la, source)`` or ``None`` if the fixture is not in scores_data.json.
    """
    hc, ac = canonical(home), canonical(away)
    for f in _load_scores():
        fx = f.get("fixture") or ""
        if " vs " not in fx:
            continue
        h, a = [s.strip() for s in fx.split(" vs ", 1)]
        if {canonical(h), canonical(a)} != {hc, ac}:
            continue
        flip = canonical(h) != hc  # scores_data lists teams the other way round

        scores = f.get("scores") or []
        if scores:
            eh = ea = 0.0
            tot = 0.0
            for s in scores:
                sc = s.get("score") or ""
                if "-" not in sc:
                    continue
                try:
                    g1, g2 = (int(x) for x in sc.split("-", 1))
                except ValueError:
                    continue
                pr = float(s.get("prob") or 0.0) / 100.0
                # scores_data convention is its own home-away; reorient.
                gh, ga = (g2, g1) if flip else (g1, g2)
                eh += pr * gh
                ea += pr * ga
                tot += pr
            if tot > 0:
                # Normalise (exact-score grid may be truncated) and floor.
                return max(eh / tot, 0.05), max(ea / tot, 0.05), "scores_dist"

        ou = f.get("over_under") or {}
        m1x2 = f.get("model_1x2") or {}
        line = float(ou.get("line") or 2.5)
        # crude total from the line + over prob; fall back to the line itself.
        total = line + 0.2  # WC totals run a touch over the 2.5 line on average
        ph = float(m1x2.get("home") or 0.40)
        pa = float(m1x2.get("away") or 0.30)
        flip_p = (ph, pa) if not flip else (pa, ph)
        ph2, pa2 = flip_p
        denom = ph2 + pa2 + 1e-9
        lh = total * (0.5 + 0.5 * (ph2 - pa2) / denom)
        la = total - lh
        return max(lh, 0.05), max(la, 0.05), "ou_split"
    return None


def _scorers_for_fixture(home: str, away: str,
                         overrides: Dict[str, List[PlayerParams]]
                         ) -> Dict[str, List[PlayerParams]]:
    """Pick the override player lists for the two fixture teams (canonical key)."""
    by_canon = {canonical(k): v for k, v in overrides.items()}
    out: Dict[str, List[PlayerParams]] = {}
    for team in (home, away):
        plist = by_canon.get(canonical(team))
        if plist:
            # Re-stamp the team name to the spelling we price the fixture with.
            out[team] = [PlayerParams(name=p.name, team=team,
                                      npxg_share=p.npxg_share,
                                      penalty_taker=p.penalty_taker,
                                      expected_minutes=p.expected_minutes,
                                      source=p.source) for p in plist]
    return out


def _rates_from_db(scorers: Dict[str, List[PlayerParams]]
                   ) -> Dict[Tuple[str, str], PPM.PlayerPropRates]:
    """Build players.db SoT rates for every scorer player (empty if no DB)."""
    if not os.path.exists(_PLAYERS_DB):
        return {}
    from wca.models.betbuilder import RateStore
    store = RateStore(_PLAYERS_DB)
    out: Dict[Tuple[str, str], PPM.PlayerPropRates] = {}
    for team, plist in scorers.items():
        for pp in plist:
            r = PPM.rates_from_players_db(team, pp.name, store=store,
                                          expected_minutes=pp.expected_minutes)
            if r is not None:
                out[(team, pp.name)] = r
    return out


# --------------------------------------------------------------------------- commands


def cmd_list(args) -> int:
    print("Fetching live PM markets …")
    events = P.find_world_cup_markets(include_closed=False)
    pp_events = [e for e in events if "player prop" in (e.get("title") or "").lower()]
    if not pp_events:
        print("No live '… - Player Props' events found.")
        return 0
    print("%d Player-Props events:" % len(pp_events))
    for e in pp_events:
        title = e.get("title") or ""
        n = len(e.get("markets") or [])
        print("  %-55s  (%d markets)" % (title[:55], n))
    return 0


def cmd_price(args) -> int:
    home, away = args.home, args.away

    lam = _fixture_lambdas(home, away)
    if lam is None and (args.lambda_home is None or args.lambda_away is None):
        print("No lambdas: '%s vs %s' not in scores_data.json. Pass "
              "--lambda-home/--lambda-away to price anyway." % (home, away))
        return 2
    if args.lambda_home is not None and args.lambda_away is not None:
        lambda_home, lambda_away, lam_src = args.lambda_home, args.lambda_away, "cli"
    else:
        lambda_home, lambda_away, lam_src = lam  # type: ignore[misc]

    overrides = load_player_overrides(_PLAYERS_JSON)
    scorers = _scorers_for_fixture(home, away, overrides)
    if not scorers:
        print("No players.json entries for either team (%s / %s). "
              "Model can't price props without player params." % (home, away))
        return 2
    rates_by_player = _rates_from_db(scorers)

    priced = PPM.price_fixture_props_detailed(
        home, away, lambda_home=lambda_home, lambda_away=lambda_away,
        scorers_by_team=scorers, rates_by_player=rates_by_player)

    print("Fetching live PM Player-Props event for %s vs %s …" % (home, away))
    events = P.find_world_cup_markets(include_closed=False)
    event = P._player_props_event(home, away, events)
    if event is None:
        print("No live '%s vs. %s - Player Props' PM event. Model-only "
              "fair odds below." % (home, away))
        _print_model_only(priced, args)
        return 0

    rows = PPM.join_fixture_to_pm(priced, event)
    if not rows:
        print("PM event found but no player/market matched the model. "
              "(name spelling or markets not yet listed)")
        _print_model_only(priced, args)
        return 0

    rows = [r for r in rows if r.edge >= args.min_edge]
    rows.sort(key=lambda r: r.edge, reverse=True)
    rows = rows[: args.top]

    if args.json:
        print(json.dumps({
            "fixture": "%s vs %s" % (home, away),
            "lambda_home": round(lambda_home, 4),
            "lambda_away": round(lambda_away, 4),
            "lambda_source": lam_src,
            "rows": [r.as_dict() for r in rows],
        }, indent=2))
        return 0

    print("\n=== PLAYER PROPS: %s vs %s ===" % (home, away))
    print("lambda %s=%.2f %s=%.2f (source=%s)  |  edge>=%.0f%%  top %d"
          % (home, lambda_home, away, lambda_away, lam_src,
             args.min_edge * 100, args.top))
    print("%-22s %-16s %-3s  %6s %6s %6s   %-18s" %
          ("player", "market", "k+", "model", "PM", "edge", "rate/min src"))
    print("-" * 92)
    for r in rows:
        print("%-22s %-16s %-3s  %5.0f%% %5.0f%% %+5.0f%%   %s/%s"
              % (r.player[:22], r.market_type, "%d+" % r.threshold,
                 r.model_prob * 100, r.pm_price * 100, r.edge * 100,
                 r.rate_source, r.minutes_source))
    print("\nNOTE: edges are dominated by lineup/minutes uncertainty (no live "
          "lineup feed). Prefer near-certain starters; treat 'prior'/'derived' "
          "rate_source rows as soft.")
    return 0


def _print_model_only(priced, args) -> int:
    rows = [p for p in priced if p.prob > 0]
    rows.sort(key=lambda p: p.prob, reverse=True)
    print("%-22s %-16s %-3s  %6s  %-10s" %
          ("player", "market", "k+", "model", "fair"))
    print("-" * 64)
    for p in rows[: args.top]:
        fair = p.fair_odds
        print("%-22s %-16s %-3s  %5.0f%%  %s"
              % (p.player[:22], p.market_type, "%d+" % p.threshold,
                 p.prob * 100,
                 ("%.2f" % fair) if fair and not math.isinf(fair) else "—"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list live Player-Props events")
    pl.set_defaults(fn=cmd_list)

    pr = sub.add_parser("price", help="price one fixture's player props vs PM")
    pr.set_defaults(fn=cmd_price)
    pr.add_argument("--home", required=True)
    pr.add_argument("--away", required=True)
    pr.add_argument("--min-edge", type=float, default=0.0, dest="min_edge")
    pr.add_argument("--top", type=int, default=20)
    pr.add_argument("--lambda-home", type=float, default=None, dest="lambda_home")
    pr.add_argument("--lambda-away", type=float, default=None, dest="lambda_away")
    pr.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
