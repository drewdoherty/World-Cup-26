#!/usr/bin/env python
"""CLI: price player goalscorer markets + promo boosts for a fixture.

Derives the two teams' Dixon-Coles expected goals (lambda), then prices the
anytime / first-goalscorer / brace / hat-trick markets for the override-store
players, and — when an offered First-Goalscorer price is supplied — the EV of a
Betfred *Double Delight & Hat-Trick Heaven* single.

Usage
-----
    python scripts/wca_price_scorers.py --home Haiti --away Scotland \
        --team Scotland [--player "Lawrence Shankland" --fgs-odds 5.5] \
        [--lam-home L --lam-away L]  [--no-neutral]

If ``--lam-home/--lam-away`` are omitted the script fits Dixon-Coles on
``data/raw/results.csv`` (slower) to derive them.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _derive_lambdas(home: str, away: str, neutral: bool):
    """Fit DC on the results history and return (lambda_home, lambda_away)."""
    from wca.data.results import load_results
    from wca.data.teamnames import canonical
    from wca.card import fit_models

    results = load_results("data/raw/results.csv")
    models = fit_models(results)
    pred = models.dc.predict(canonical(home), canonical(away), neutral=neutral, warn=False)
    return float(pred.lambda_home), float(pred.lambda_away)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--team", help="which side's players to price (default: away)")
    ap.add_argument("--player", help="restrict to one player (name as in players.json)")
    ap.add_argument("--fgs-odds", type=float, help="offered First-Goalscorer decimal odds for the Double Delight EV")
    ap.add_argument("--lam-home", type=float)
    ap.add_argument("--lam-away", type=float)
    ap.add_argument("--no-neutral", action="store_true", help="non-neutral venue (default: neutral, as at a World Cup)")
    ap.add_argument("--players", default="data/players.json")
    ap.add_argument("--pen-xg", type=float, default=0.18)
    args = ap.parse_args(argv)

    from wca.models.scorers import ScorerPricer, players_for_team

    neutral = not args.no_neutral
    if args.lam_home is not None and args.lam_away is not None:
        lam_h, lam_a = args.lam_home, args.lam_away
        src = "supplied"
    else:
        lam_h, lam_a = _derive_lambdas(args.home, args.away, neutral)
        src = "Dixon-Coles fit"
    lam_total = lam_h + lam_a

    team = args.team or args.away
    team_lambda = lam_h if team == args.home else lam_a

    print(f"{args.home} vs {args.away}  (neutral={neutral})")
    print(f"  lambdas [{src}]: {args.home}={lam_h:.3f}  {args.away}={lam_a:.3f}  total={lam_total:.3f}")
    print(f"  pricing {team} (team lambda={team_lambda:.3f})\n")

    pricer = ScorerPricer(pen_xg=args.pen_xg)
    players = players_for_team(team, args.players)
    if args.player:
        players = [p for p in players if p.name == args.player]
    if not players:
        print(f"  no override players for {team!r} in {args.players}. "
              f"Add them (Scotland 2026 etc. are not in the StatsBomb dataset).")
        return 1

    hdr = f"{'player':22} {'min':>4} {'pen':>4} | {'anytime':>8} {'first':>7} {'2+':>6} {'3+':>6} | {'fairFGS':>8}"
    print(hdr)
    print("-" * len(hdr))
    for p in players:
        line = pricer.price_player(p, team_lambda, lam_total)
        print(f"{p.name:22} {p.expected_minutes:4.0f} {('Y' if p.penalty_taker else 'n'):>4} | "
              f"{line.p_anytime*100:7.1f}% {line.p_first*100:6.1f}% {line.p_two_plus*100:5.1f}% "
              f"{line.p_three_plus*100:5.1f}% | {line.fair_first:8.2f}  [{p.source}]")
        if args.fgs_odds and (not args.player or p.name == args.player):
            dd = pricer.double_delight_ev(line, args.fgs_odds)
            verdict = "+EV" if dd["edge_pct"] > 0 else "-EV"
            print(f"    Double Delight @ {args.fgs_odds:.2f}: effective x{dd['effective_mult']:.3f}  "
                  f"EV/£1=£{dd['ev_per_unit']:.3f} ({dd['edge_pct']:+.1f}%)  {verdict}  "
                  f"(no-boost EV £{dd['ev_no_boost']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
