"""Construct same-game *bet builders* (same-match accas) for a World Cup group.

For each bettable fixture in a group it builds the most likely correlation-aware
same-game multi whose model-fair price clears a minimum-odds floor (default
2.0 = EVS), pricing every leg off the reconciled Dixon-Coles score matrix. When
real offered prices are supplied (``--odds``) it sizes each builder quarter-Kelly
off the ledger's CLV-gated bankroll, respecting existing open exposure.

The models are the platform's fitted Elo + Dixon-Coles (refit from the results
history, or loaded from ``--models-pkl``). Group rosters and the round-robin
schedule are built in; pass ``--fixtures`` to override.

Usage:
    # Group K same-match accas at model fair value (no sizing):
    ./.venv/bin/python scripts/wca_group_builders.py --group K

    # Size against real bet365 prices off the ledger bankroll:
    ./.venv/bin/python scripts/wca_group_builders.py --group K \\
        --odds "Portugal vs Colombia=2.10" --odds "DR Congo vs Uzbekistan=2.40"

    # Reuse cached fitted models (refit is ~4 min):
    ./.venv/bin/python scripts/wca_group_builders.py --group J --models-pkl /tmp/models.pkl
"""
from __future__ import annotations

import argparse
import pickle
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wca.betbuilder import (  # noqa: E402
    EVS,
    apply_slate_cap,
    build_bet_builder,
    format_bet_builder,
    matrix_from_models,
    size_bet_builder,
)

# 2026 group rosters (J and K) and the bettable round-robin pairings. The first
# two matchdays are played; the remaining same-match-acca-able games are the
# third-round pairings (each team plays the one side it has not yet faced).
GROUPS: Dict[str, List[str]] = {
    "J": ["Argentina", "Algeria", "Austria", "Jordan"],
    "K": ["Portugal", "DR Congo", "Uzbekistan", "Colombia"],
}

# Remaining (matchday-3) fixtures per group — the games still open to bet.
REMAINING: Dict[str, List[Tuple[str, str]]] = {
    "J": [("Argentina", "Jordan"), ("Algeria", "Austria")],
    "K": [("Portugal", "Colombia"), ("DR Congo", "Uzbekistan")],
}


def _load_models(models_pkl: Optional[str]):
    if models_pkl:
        with open(models_pkl, "rb") as fh:
            return pickle.load(fh)
    # Refit from the results history (matches scripts/wca_build_card.py).
    from wca.card import fit_models
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results

    print("Fitting Elo + Dixon-Coles from results history (~4 min)...", file=sys.stderr)
    return fit_models(load_results(resolve_results_path()))


def _parse_odds(pairs: Optional[List[str]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for item in pairs or []:
        key, _, val = item.partition("=")
        if not val:
            raise SystemExit("--odds expects 'Fixture=odds', got %r" % item)
        out[key.strip().lower()] = float(val)
    return out


def _parse_fixtures(specs: Optional[List[str]]) -> Optional[List[Tuple[str, str]]]:
    if not specs:
        return None
    import re

    out: List[Tuple[str, str]] = []
    for s in specs:
        parts = re.split(r"\s+vs?\s+", s.strip(), maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2:
            raise SystemExit("--fixture expects 'Home vs Away', got %r" % s)
        out.append((parts[0].strip(), parts[1].strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group", default="K", choices=sorted(GROUPS), help="Group letter.")
    ap.add_argument(
        "--fixture",
        action="append",
        dest="fixtures",
        metavar="HOME vs AWAY",
        help="Override the fixture list (repeatable).",
    )
    ap.add_argument("--min-odds", type=float, default=EVS, help="Min builder odds (2.0=EVS).")
    ap.add_argument("--min-legs", type=int, default=2)
    ap.add_argument("--max-legs", type=int, default=4)
    ap.add_argument(
        "--odds",
        action="append",
        metavar="FIXTURE=ODDS",
        help="Offered builder price per fixture for Kelly sizing (repeatable).",
    )
    ap.add_argument("--db", default="data/wca.db", help="Ledger SQLite path for sizing.")
    ap.add_argument("--bankroll", type=float, default=None, help="Override bankroll.")
    ap.add_argument("--models-pkl", default=None, help="Load fitted models from a pickle.")
    args = ap.parse_args()

    warnings.filterwarnings("ignore")
    fixtures = _parse_fixtures(args.fixtures) or REMAINING[args.group]
    offered = _parse_odds(args.odds)

    models = _load_models(args.models_pkl)

    # Resolve bankroll + Kelly fraction from the ledger's CLV-gated ladder, and
    # the existing open exposure already at risk.
    from wca.card import resolve_pool_bankroll
    from wca.ledger.reports import open_exposure

    pool = resolve_pool_bankroll(args.db, override=args.bankroll)
    try:
        open_df = open_exposure(args.db)
        existing = float(open_df["stake"].sum()) if len(open_df) else 0.0
    except Exception:
        existing = 0.0

    print("=" * 70)
    print("Group %s same-match accas — model fair value, min odds %.2f (EVS)"
          % (args.group, args.min_odds))
    print("Bankroll: %s" % pool.reason)
    if existing > 0:
        print("Existing open exposure: %.2f (reduces today's slate budget)" % existing)
    print("=" * 70)

    sized_for_slate = []
    for home, away in fixtures:
        fm = matrix_from_models(models, home, away)
        builders = build_bet_builder(
            fm, min_odds=args.min_odds, min_legs=args.min_legs, max_legs=args.max_legs
        )
        print()
        print(format_bet_builder(fm, builders, min_odds=args.min_odds))

        key = ("%s vs %s" % (home, away)).lower()
        if builders and key in offered:
            sb = size_bet_builder(
                builders[0], offered[key], pool.bankroll,
                kelly_fraction=pool.kelly_fraction,
            )
            sized_for_slate.append((home, away, sb))

    # Slate-level sizing (only when offered prices were supplied).
    if sized_for_slate:
        scaled = apply_slate_cap(
            [sb for _, _, sb in sized_for_slate],
            pool.bankroll,
            existing_exposure=existing,
        )
        print()
        print("=" * 70)
        print("SIZING (quarter-Kelly, %s bankroll, slate-capped):" % pool.bankroll)
        print("=" * 70)
        total = 0.0
        for (home, away, _), sb in zip(sized_for_slate, scaled):
            verdict = "BET" if sb.stake > 0 else "no bet (offered <= fair)"
            print(
                "%-26s offered %.2f | fair %.2f | edge %+.1f%% | stake %.2f | EV %+.2f  [%s]"
                % (
                    "%s v %s" % (home, away),
                    sb.offered_odds,
                    sb.builder.fair_odds,
                    sb.edge * 100,
                    sb.stake,
                    sb.ev,
                    verdict,
                )
            )
            total += sb.stake
        print("-" * 70)
        print("Total staked: %.2f  (%.1f%% of bankroll)"
              % (total, 100 * total / pool.bankroll if pool.bankroll else 0))
    elif offered:
        print("\n(No supplied --odds matched a built fixture.)")
    else:
        print("\nSupply real bet365 prices to size, e.g.:")
        for home, away in fixtures:
            print('  --odds "%s vs %s=<price>"' % (home, away))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
