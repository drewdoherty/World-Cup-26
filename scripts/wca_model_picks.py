#!/usr/bin/env python
"""Player-model +EV picks off CURRENT odds — ready-to-place slips.

Pulls the live World Cup slate (Odds API h2h to discover fixtures/event ids),
then for the next N fixtures prices anytime-goalscorer markets with the unified
player model (#7: players.db npxg-share x DC lambda) and overlays the best book
price (Odds API per-event player props) and the Polymarket "1+ goals" price.
Surfaces every +EV selection with model prob, fair odds, best available price +
venue, edge %, and a quarter-Kelly stake.

RECOMMENDATIONS ONLY — this never places, executes or moves anything. It is a
read-only monitoring tool. Corner/card model lines are shown as context; no live
corner/card market is overlaid (the Odds API WC feed does not carry them).

    ODDS_API_KEY=... python scripts/wca_model_picks.py --max-fixtures 6 \
        --bankroll 1000 --min-edge 0.03 --db data/players.db
"""
import argparse
import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _now_utc():
    return _dt.datetime.now(_dt.timezone.utc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="uk,eu")
    ap.add_argument("--max-fixtures", type=int, default=6)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--kelly-fraction", type=float, default=0.25)
    ap.add_argument("--kelly-cap", type=float, default=0.05)
    ap.add_argument("--min-edge", type=float, default=0.03, help="min edge to list (e.g. 0.03 = 3%)")
    ap.add_argument("--db", default=str(ROOT / "data" / "players.db"))
    ap.add_argument("--players", default=str(ROOT / "data" / "players.json"))
    ap.add_argument("--no-book", action="store_true", help="skip Odds API per-event props (PM + model only)")
    ap.add_argument("--no-pm", action="store_true", help="skip Polymarket overlay")
    ap.add_argument("--out", default=str(ROOT / "data" / "model_picks_latest.md"))
    args = ap.parse_args(argv)

    from wca.card import fit_models
    from wca.data import theoddsapi
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results
    from wca.data.teamnames import canonical
    from wca.markets import kelly as kelly_mod
    from wca.models import scorer_props as sp
    from wca.nextmatch import SCORER_MARKETS

    print("Fitting DC/Elo on results history ...", file=sys.stderr)
    models = fit_models(load_results(resolve_results_path()))

    print("Pulling live h2h slate ...", file=sys.stderr)
    odds_df, quota = theoddsapi.get_odds("soccer_fifa_world_cup",
                                         regions=args.regions, markets="h2h")
    if odds_df is None or odds_df.empty:
        print("No live slate returned (out of season / no markets / quota). "
              "Nothing to price.", file=sys.stderr)
        return 0

    now = _now_utc()
    # Unique upcoming fixtures by event.
    seen = {}
    for _, r in odds_df.iterrows():
        eid = str(r.get("event_id"))
        ct = r.get("commence_time")
        if eid in seen or eid in ("None", ""):
            continue
        try:
            if ct is not None and ct < now:
                continue
        except TypeError:
            pass
        seen[eid] = (str(r.get("home_team")), str(r.get("away_team")), ct)
    fixtures = sorted(seen.items(), key=lambda kv: str(kv[1][2]))[: args.max_fixtures]
    print("Upcoming fixtures: %d (quota remaining=%s)"
          % (len(fixtures), getattr(quota, "remaining", "?")), file=sys.stderr)

    pm_events = None
    if not args.no_pm:
        try:
            from wca.data import polymarket as pm
            pm_events = pm.find_world_cup_markets(include_closed=False)
        except Exception as exc:
            print("WARN: Polymarket fetch failed: %s" % exc, file=sys.stderr)

    picks = []          # actionable +EV anytime-scorer slips
    context = []        # per-fixture model context (corners/cards + top model scorers)

    for eid, (home, away, ct) in fixtures:
        hc, ac = canonical(home), canonical(away)
        try:
            pred = models.dc.predict(hc, ac, neutral=True, warn=False)
            lh = float(getattr(pred, "lambda_home", 0.0) or 0.0)
            la = float(getattr(pred, "lambda_away", 0.0) or 0.0)
        except Exception:
            continue
        if lh <= 0 or la <= 0:
            continue

        lines = sp.model_scorer_lines(home, away, lh, la, db_path=args.db,
                                      overrides_path=args.players, top_n_per_team=6)

        scorer_df = None
        if not args.no_book:
            try:
                scorer_df, _q = theoddsapi.get_event_odds(
                    "soccer_fifa_world_cup", eid, regions=args.regions,
                    markets=SCORER_MARKETS)
            except Exception as exc:
                print("WARN: book props pull failed for %s vs %s: %s"
                      % (home, away, exc), file=sys.stderr)
        sp.overlay_market(lines, scorer_df=scorer_df, pm_events=pm_events,
                          home=home, away=away, pm_lookup=not args.no_pm)

        # corners / cards model context (no live market overlay available).
        corners = sp.corners_scan(home, away, lh, la)
        cards_rows, p_red, cards_mu = sp.cards_scan(home, away, lh, la, db_path=args.db)
        context.append({"fixture": "%s vs %s" % (hc, ac), "lh": lh, "la": la,
                        "corners": corners, "cards": cards_rows, "p_red": p_red,
                        "cards_mu": cards_mu,
                        "scorers": (lines["home"][:3] + lines["away"][:3])})

        for side in ("home", "away"):
            for ln in lines[side]:
                # Best available anytime price across book + Polymarket.
                cands = []
                if ln.book_anytime_odds:
                    cands.append((ln.book_anytime_odds, ln.book_anytime_name or "book"))
                if ln.pm_anytime_price and 0 < ln.pm_anytime_price < 1:
                    cands.append((1.0 / ln.pm_anytime_price, "Polymarket"))
                if not cands:
                    continue
                best_odds, venue = max(cands, key=lambda c: c[0])
                ev = ln.model_p_anytime * best_odds
                edge = ev - 1.0
                if edge < args.min_edge:
                    continue
                stk = kelly_mod.stake(ln.model_p_anytime, best_odds, args.bankroll,
                                      fraction=args.kelly_fraction, cap=args.kelly_cap)
                picks.append({
                    "fixture": "%s vs %s" % (hc, ac),
                    "player": ln.player, "team": ln.team,
                    "market": "anytime scorer",
                    "model_p": ln.model_p_anytime,
                    "fair": ln.model_fair_anytime,
                    "best_odds": best_odds, "venue": venue,
                    "edge": edge, "stake": stk,
                    "share_source": ln.share_source,
                })

    picks.sort(key=lambda p: p["edge"], reverse=True)

    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    out.append("# Player-model +EV picks — ready-to-place slips")
    out.append("")
    out.append("_Generated %s · model: players.db npxg-share x DC lambda · "
               "quarter-Kelly (cap %.0f%%) on a £%.0f bankroll · RECOMMENDATIONS "
               "ONLY, no execution._" % (ts, args.kelly_cap * 100, args.bankroll))
    out.append("")
    out.append("> **PRE-BACKTEST**: these prices come from the player model that "
               "has NOT yet been validated vs the v1 Elo/DC/Shin baseline. The "
               "WC2018->WC2022 backtest shows the model ranks scorers well but is "
               "over-confident on absolute probability — treat edges as "
               "optimistic and size conservatively.")
    out.append("")
    if not picks:
        out.append("**No +EV anytime-scorer selections at the current min edge "
                   "(%.0f%%).** Either no book/PM scorer market is open yet for "
                   "the upcoming fixtures, or the model finds no edge." % (args.min_edge * 100))
    else:
        out.append("## Anytime goalscorer (%d +EV slips)" % len(picks))
        out.append("")
        out.append("| # | Player | Fixture | Model P | Fair | Best price | Venue | Edge | ¼-Kelly |")
        out.append("|---|--------|---------|--------:|-----:|-----------:|-------|-----:|--------:|")
        for i, p in enumerate(picks, 1):
            out.append("| %d | %s | %s | %.1f%% | %.2f | %.2f | %s | **+%.1f%%** | £%.2f |" % (
                i, p["player"], p["fixture"], 100 * p["model_p"], p["fair"],
                p["best_odds"], p["venue"], 100 * p["edge"], p["stake"]))
    out.append("")
    out.append("## Model context — corners / cards / top scorers (no live market overlaid)")
    for c in context:
        out.append("")
        out.append("**%s** — xG %.2f − %.2f" % (c["fixture"], c["lh"], c["la"]))
        cor = c["corners"][0]
        out.append("- corners O/U %.1f: model P(over) %.0f%% (fair %.2f / %.2f)" % (
            cor.line, 100 * cor.p_over, cor.fair_over, cor.fair_under))
        out.append("- cards: model exp %.2f, P(>=1 red) %.0f%%" % (c["cards_mu"], 100 * c["p_red"]))
        top = ", ".join("%s %.0f%%" % (s.player, 100 * s.model_p_anytime)
                        for s in sorted(c["scorers"], key=lambda s: s.model_p_anytime, reverse=True)[:4])
        out.append("- top model scorers: %s" % top)
    out.append("")
    out.append("_Markets not overlaid live (no WC feed via current sources): "
               "shots-on-target lines, score-or-assist (= anytime lower bound), "
               "corner/card books. Anytime markets carry bookmaker overround, so "
               "exchange/Polymarket prices give the truer edge._")

    text = "\n".join(out)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print("\n" + text)
    print("\n(written to %s)" % args.out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
