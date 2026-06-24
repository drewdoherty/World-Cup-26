#!/usr/bin/env python
"""/event scanner — player-aware model prices + fair odds for one fixture.

    python scripts/wca_event_scan.py scorers --match "Brazil vs Morocco"
    python scripts/wca_event_scan.py corners --match "Brazil vs Morocco"
    python scripts/wca_event_scan.py cards   --home Brazil --away Morocco

Model-first and **offline by default**: scorer/corner/card prices come from
``data/players.db`` + the Dixon-Coles team lambdas, so they are always available
even when no bookmaker market exists (each line is labelled accordingly). The
Dixon-Coles lambdas are fit on ``data/raw/results.csv`` unless ``--lam-home`` /
``--lam-away`` are supplied.

This is monitoring/analytics only — it prices and surfaces edge; it never places
or moves anything.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _derive_lambdas(home, away, neutral):
    from wca.card import fit_models
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results
    from wca.data.teamnames import canonical
    results = load_results(resolve_results_path())
    models = fit_models(results)
    pred = models.dc.predict(canonical(home), canonical(away), neutral=neutral, warn=False)
    return float(pred.lambda_home), float(pred.lambda_away)


def _split_match(args):
    if args.match:
        for sep in (" vs ", " v ", " - "):
            if sep in args.match:
                h, a = args.match.split(sep, 1)
                return h.strip(), a.strip()
        raise SystemExit("could not parse --match %r" % args.match)
    if not (args.home and args.away):
        raise SystemExit("provide --match 'Home vs Away' or --home/--away")
    return args.home, args.away


def _fmt(x, nd=2):
    return "--" if x is None else ("%.*f" % (nd, x))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["scorers", "corners", "cards"])
    ap.add_argument("--match")
    ap.add_argument("--home")
    ap.add_argument("--away")
    ap.add_argument("--db", default=str(ROOT / "data" / "players.db"))
    ap.add_argument("--overrides", default=str(ROOT / "data" / "players.json"))
    ap.add_argument("--lam-home", type=float)
    ap.add_argument("--lam-away", type=float)
    ap.add_argument("--no-neutral", action="store_true",
                    help="treat as a home fixture (default: neutral venue)")
    ap.add_argument("--top-n", type=int, default=5)
    args = ap.parse_args(argv)

    home, away = _split_match(args)
    if args.lam_home is not None and args.lam_away is not None:
        lh, la = args.lam_home, args.lam_away
    else:
        lh, la = _derive_lambdas(home, away, neutral=not args.no_neutral)

    print("=== /event %s — %s vs %s ===" % (args.kind, home, away))
    print("DC lambdas: home=%.3f away=%.3f (total=%.3f)" % (lh, la, lh + la))

    from wca.models import scorer_props as sp

    if args.kind == "scorers":
        lines = sp.model_scorer_lines(home, away, lh, la, db_path=args.db,
                                      overrides_path=args.overrides,
                                      top_n_per_team=args.top_n)
        for side, team in (("home", home), ("away", away)):
            print("\n%s — top %d model scorers:" % (team, args.top_n))
            if not lines[side]:
                print("  data-pending (no StatsBomb history / override for squad)")
                continue
            print("  %-26s %7s %8s %8s  %-26s" %
                  ("player", "P(any)", "fair", "share", "basis"))
            for ln in lines[side]:
                print("  %-26s %6.1f%% %8s %7.1f%%  %-26s" % (
                    ln.player[:26], 100 * ln.model_p_anytime,
                    _fmt(ln.model_fair_anytime), 100 * ln.share,
                    ln.share_source))
        print("\nnote: %s — overlay live book/PM odds to compute EV." % sp.MODEL_ONLY_LABEL)
        return 0

    if args.kind == "corners":
        for pl in sp.corners_scan(home, away, lh, la):
            print("  corners O/U %.1f : P(over)=%.1f%%  fair over=%s under=%s  [%s]" % (
                pl.line, 100 * pl.p_over, _fmt(pl.fair_over), _fmt(pl.fair_under), pl.label))
        return 0

    # cards
    rows, p_red, mean = sp.cards_scan(home, away, lh, la, db_path=args.db)
    print("  expected total cards: %.2f   P(>=1 red): %.1f%%" % (mean, 100 * p_red))
    for pl in rows:
        print("  cards O/U %.1f : P(over)=%.1f%%  fair over=%s under=%s  [%s]" % (
            pl.line, 100 * pl.p_over, _fmt(pl.fair_over), _fmt(pl.fair_under), pl.label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
