"""CLI: tournament-advancement edges (Monte-Carlo sim vs Polymarket).

Fits Elo + Dixon-Coles on the results history (or reuses a cached fit), runs the
2026 World Cup Monte-Carlo simulator to get per-team stage probabilities, pulls
the live Polymarket advancement / group-winner markets, computes fee-adjusted
edges and quarter-Kelly stakes on the $1,310 Polymarket pool, writes the full
report to ``docs/research/advancement_edges.md`` and prints the top-10 edges.

Usage::

    python scripts/wca_advancement.py [--n-sims N] [--seed S]
        [--results PATH] [--out PATH] [--cache PATH] [--refit] [--top N]
        [--venue-aware] [--structural-prior]

``--venue-aware`` and ``--structural-prior`` enable the opt-in Klement-borrowed
features (both default off) for A/B comparison on the Polymarket markets; see
docs/research/backtests/structural_prior.md for the evidence behind keeping them
off by default.

No API key is required (the Polymarket Gamma API is public).
"""
from __future__ import annotations

import argparse
import datetime
import pickle
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd


def _fmt_pct(x: float) -> str:
    return "%.1f%%" % (100.0 * x)


def _load_or_fit_models(
    results_path: str, cache_path: str, refit: bool, structural_prior: bool = False
):
    """Fit Elo+DC, caching the fitted object to ``cache_path`` (pickle).

    The fit takes ~2 minutes; the cache is keyed only by existence (the caller
    passes ``--refit`` to force a fresh fit when the results file changes). When
    ``structural_prior`` is set the Dixon-Coles ridge shrinks low-data teams
    toward the socio-economic prior; that is a *different* fit, so the caller
    routes it to a distinct cache file (see ``main``) to keep A/B runs separate.
    """
    from wca.card import fit_models
    from wca.data.results import load_results

    cache = Path(cache_path)
    if cache.exists() and not refit:
        try:
            with cache.open("rb") as fh:
                models = pickle.load(fh)
            print("Reusing cached model fit: %s" % cache)
            return models
        except Exception as exc:  # noqa: BLE001 - cache is best-effort
            print("Cache load failed (%s); refitting." % exc, file=sys.stderr)

    print(
        "Fitting Elo + Dixon-Coles%s (this takes ~2 minutes)…"
        % (" [structural prior]" if structural_prior else "")
    )
    results = load_results(results_path)
    models = fit_models(results, structural_prior=structural_prior)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open("wb") as fh:
            pickle.dump(models, fh)
        print("Cached model fit to %s" % cache)
    except Exception as exc:  # noqa: BLE001
        print("Could not cache model fit (%s)." % exc, file=sys.stderr)
    return models


def _filter_advancement_events(pm_events: Sequence[Dict[str, Any]]):
    """Keep only the advancement / group-winner events we score."""
    from wca.advancement import PM_STAGE_EVENTS, _group_winner_event_letter

    keep: List[Dict[str, Any]] = []
    for e in pm_events:
        title = str(e.get("title") or "").strip()
        if title in PM_STAGE_EVENTS or _group_winner_event_letter(title) is not None:
            keep.append(e)
    return keep


def _write_report(
    out_path: str,
    sim_df: pd.DataFrame,
    edges_df: pd.DataFrame,
    n_sims: int,
    seed: int,
    n_events_total: int,
    n_events_scored: int,
    matched_markets: int,
    generated_utc: str,
) -> None:
    from wca.advancement import (
        PM_KELLY_FRACTION,
        PM_PER_BET_CAP,
        PM_POOL_BANKROLL,
        STAGE_LABEL,
        WC2026_GROUPS,
    )

    lines: List[str] = []
    a = lines.append

    a("# Tournament-advancement edges — sim vs Polymarket")
    a("")
    a("_Generated %s UTC._" % generated_utc)
    a("")
    a(
        "Monte-Carlo simulation of the 2026 FIFA World Cup (`%d` sims, seed `%d`) "
        "compared to live Polymarket advancement and group-winner markets. "
        "Edges are **fee-adjusted** and sized **quarter-Kelly** on the "
        "$%.0f Polymarket pool (%.0f%% per-bet cap)."
        % (n_sims, seed, PM_POOL_BANKROLL, PM_PER_BET_CAP * 100)
    )
    a("")

    # -- Methodology --------------------------------------------------------
    a("## Methodology")
    a("")
    a(
        "1. **Models.** Elo (rating + ordered-logit outcome model) and a "
        "time-decayed Dixon-Coles model are fit on the full international "
        "results history (`wca.card.fit_models`)."
    )
    a(
        "2. **prob_fn (honest caveat).** Every simulated match is driven by a "
        "**straight 50/50 average of the Elo and Dixon-Coles 1X2 "
        "probabilities** — there is **no market term**. The group-stage card "
        "anchors ~50% on the de-vigged market, but there are *no* odds for the "
        "later rounds, so a market-anchored blend is impossible here. These "
        "edges are therefore an independent, noisier model view, not ground "
        "truth."
    )
    a(
        "3. **Venue.** The three hosts (United States, Mexico, Canada) get the "
        "home-advantage bonus on their own group fixtures (derived from the "
        "scheduled-fixture `neutral` flag, as `wca.card` does). Every other "
        "group match and **all** knockout matches are neutral."
    )
    a(
        "4. **Knockout draws / ET / penalties.** A 90-minute knockout draw is "
        "resolved by the simulator's extra-time / penalty model. \"Advancing\" "
        "therefore **includes** winning on penalties — matching Polymarket "
        "resolution (\"reach stage X\" = the team is in stage X, however it got "
        "there)."
    )
    a(
        "5. **Stage mapping.** `advance to Knockout Stages` = reach the Round of "
        "32 (top-2 or one of the eight best third-placed teams); `Reach Round "
        "of 16/QF/SF/Final` = win the preceding knockout tie; `World Cup "
        "Winner` = win the final; `Group X Winner` = finish 1st in the group. "
        "These match each market's resolution exactly."
    )
    a(
        "6. **Edge.** For each team-stage market we price BOTH sides. YES buy "
        "price = best ask (mid of bid/ask when ask missing); NO buy price = "
        "1 − YES bid. The Polymarket sports **taker fee** `0.03·p·(1−p)` per "
        "share is subtracted. Fee-adjusted edge = `sim_prob − buy_price − fee`. "
        "We report whichever side the simulation favours."
    )
    a(
        "7. **Sizing.** A binary at buy price `c` (incl. fee) is a fixed-odds "
        "bet at decimal odds `1/c`; stake = quarter-Kelly at the simulated "
        "win probability, capped at %.0f%% of the $%.0f pool "
        "(`fraction=%.2f`)."
        % (PM_PER_BET_CAP * 100, PM_POOL_BANKROLL, PM_KELLY_FRACTION)
    )
    a("")
    a(
        "**Coverage.** %d Polymarket World-Cup events pulled; %d scored "
        "(advancement + group-winner); %d team-stage markets matched to the "
        "simulation."
        % (n_events_total, n_events_scored, matched_markets)
    )
    a("")

    # -- Top edges ----------------------------------------------------------
    a("## Top edges")
    a("")
    if edges_df.empty:
        a("_No matched markets._")
    else:
        a(
            "| # | Team | Market | Side | Sim P | PM price | Fee | Fee-adj edge | Stake ($) |"
        )
        a("|---|------|--------|------|-------|----------|-----|--------------|-----------|")
        top = edges_df.head(20)
        for i, (_, r) in enumerate(top.iterrows(), 1):
            a(
                "| %d | %s | %s | %s | %s | %.3f | %.3f | **%+.1f%%** | %.2f |"
                % (
                    i, r["team"], r["stage_label"], r["side"],
                    _fmt_pct(r["sim_prob"]), r["pm_price"], r["fee"],
                    r["fee_adj_edge"] * 100, r["stake"],
                )
            )
    a("")

    # -- Full edge table ----------------------------------------------------
    a("## All matched markets (fee-adjusted edge, descending)")
    a("")
    if edges_df.empty:
        a("_No matched markets._")
    else:
        a(
            "| Team | Grp | Market | Side | Sim P | YES mid | Buy price | Fee | Raw edge | Fee-adj edge | Stake ($) |"
        )
        a(
            "|------|-----|--------|------|-------|---------|-----------|-----|----------|--------------|-----------|"
        )
        for _, r in edges_df.iterrows():
            a(
                "| %s | %s | %s | %s | %s | %.3f | %.3f | %.3f | %+.1f%% | %+.1f%% | %.2f |"
                % (
                    r["team"], r["group"], r["stage_label"], r["side"],
                    _fmt_pct(r["sim_prob"]), r["pm_yes_mid"], r["pm_price"],
                    r["fee"], r["raw_edge"] * 100, r["fee_adj_edge"] * 100,
                    r["stake"],
                )
            )
    a("")

    # -- Full simulated probabilities --------------------------------------
    a("## Simulated stage probabilities (all 48 teams)")
    a("")
    a("Sorted by P(win). Group letter in parentheses.")
    a("")
    a("| Team | Grp | Win Grp | Reach R32 | R16 | QF | SF | Final | Win |")
    a("|------|-----|---------|-----------|-----|----|----|-------|-----|")
    sim_sorted = sim_df.sort_values("P(win)", ascending=False)
    for team, r in sim_sorted.iterrows():
        a(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                team, r["group"], _fmt_pct(r["P(group_winner)"]),
                _fmt_pct(r["P(R32)"]), _fmt_pct(r["P(R16)"]), _fmt_pct(r["P(QF)"]),
                _fmt_pct(r["P(SF)"]), _fmt_pct(r["P(Final)"]), _fmt_pct(r["P(win)"]),
            )
        )
    a("")

    # -- Caveats ------------------------------------------------------------
    a("## Honest caveats")
    a("")
    a(
        "- **No market anchor in the sim.** Unlike the group-stage card, the "
        "simulated probabilities use only the 50/50 Elo+DC blend. They embed "
        "all of that blend's known limitations (the blend does not beat the "
        "de-vigged market with confidence on the backtest) and add Monte-Carlo "
        "noise on top. Large edges most likely reflect model error, not free "
        "money — size conservatively."
    )
    a(
        "- **Monte-Carlo noise.** With `%d` sims the standard error on a 50%% "
        "probability is ~%.2f pp; deep-run probabilities (SF/Final/Win) are "
        "smaller and proportionally noisier. Re-run with more sims before "
        "acting on a marginal edge."
        % (n_sims, 100.0 * (0.25 / n_sims) ** 0.5)
    )
    a(
        "- **NO-side price approximation.** When a YES bid is missing we "
        "approximate the NO ask as `1 − YES_mid`, which is slightly optimistic; "
        "verify the live NO order book before sizing a NO position."
    )
    a(
        "- **Host venue in Dixon-Coles.** The host home bonus is applied via "
        "Elo only; Dixon-Coles is queried neutral (it has no per-host venue "
        "term). This very slightly understates host strength in their group."
    )
    a(
        "- **Group table.** The 12 groups are the FIFA final-draw result "
        "(5 Dec 2025), verified against the Wikipedia draw page and "
        "cross-checked to be consistent with the 72 scheduled fixtures in "
        "`results.csv` (every fixture intra-group, 6 per group)."
    )
    a("")
    a("### Group table used")
    a("")
    for g in sorted(WC2026_GROUPS):
        a("- **Group %s:** %s" % (g, ", ".join(WC2026_GROUPS[g])))
    a("")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tournament-advancement edges: sim vs Polymarket."
    )
    parser.add_argument("--n-sims", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--results", default=None,
                        help="results CSV (default: cleaned dataset if present, else raw)")
    parser.add_argument("--out", default="docs/research/advancement_edges.md")
    parser.add_argument(
        "--cache",
        default="data/advancement_models.pkl",
        help="Pickle cache for the fitted models (speeds up re-runs).",
    )
    parser.add_argument(
        "--refit", action="store_true", help="Force a fresh model fit."
    )
    parser.add_argument(
        "--venue-aware",
        action="store_true",
        help="Opt-in venue/geography-aware host advantage: dilute the host bonus "
        "across the three co-hosts and add an altitude tax (Estadio Azteca). "
        "Default off (legacy full single-host bonus).",
    )
    parser.add_argument(
        "--structural-prior",
        action="store_true",
        help="Opt-in socio-economic shrinkage prior for low-data teams in "
        "Dixon-Coles (Klement / Hoffmann-Ging-Ramasamy). Refits to a separate "
        "model cache. Default off. See docs/research/backtests/structural_prior.md.",
    )
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    try:
        from wca.advancement import compare_to_polymarket, run_advancement
        from wca.data.polymarket import find_world_cup_markets
        from wca.data.cleaning import resolve_results_path
    except ImportError as exc:
        print("ERROR: could not import wca modules: %s" % exc, file=sys.stderr)
        sys.exit(1)

    if args.results is None:
        args.results = resolve_results_path()

    # 1. Models. Structural-prior fits are a different model, so route them to a
    #    distinct cache file to avoid clobbering the baseline A/B cache.
    cache_path = args.cache
    if args.structural_prior:
        p = Path(args.cache)
        cache_path = str(p.with_name(p.stem + "_structural" + p.suffix))
    models = _load_or_fit_models(
        args.results, cache_path, args.refit, structural_prior=args.structural_prior
    )

    # 2. Simulate.
    flags = []
    if args.venue_aware:
        flags.append("venue-aware host")
    if args.structural_prior:
        flags.append("structural prior")
    print(
        "Running %d-sim tournament (seed %d)%s…"
        % (args.n_sims, args.seed, (" [%s]" % ", ".join(flags)) if flags else "")
    )
    sim_df = run_advancement(
        models, n_sims=args.n_sims, seed=args.seed, venue_aware=args.venue_aware
    )

    # 3. Pull Polymarket.
    print("Pulling live Polymarket World-Cup markets…")
    pm_events = find_world_cup_markets()
    scored_events = _filter_advancement_events(pm_events)
    print(
        "  %d WC events; %d advancement/group-winner events scored."
        % (len(pm_events), len(scored_events))
    )

    # 4. Compare.
    edges_df = compare_to_polymarket(sim_df, scored_events)
    print("  %d team-stage markets matched." % len(edges_df))

    # 5. Report.
    generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    _write_report(
        args.out, sim_df, edges_df, args.n_sims, args.seed,
        len(pm_events), len(scored_events), len(edges_df), generated,
    )
    print("Report written: %s" % args.out)

    # 6. Print top-N.
    print()
    print("Top %d fee-adjusted edges:" % args.top)
    if edges_df.empty:
        print("  (no matched markets)")
    else:
        print(
            "  %-22s %-22s %-4s %7s %8s %9s %8s"
            % ("TEAM", "MARKET", "SIDE", "SIM", "PRICE", "EDGE", "STAKE")
        )
        for _, r in edges_df.head(args.top).iterrows():
            print(
                "  %-22s %-22s %-4s %6.1f%% %8.3f %+8.1f%% %7.2f"
                % (
                    r["team"][:22], r["stage_label"][:22], r["side"],
                    r["sim_prob"] * 100, r["pm_price"],
                    r["fee_adj_edge"] * 100, r["stake"],
                )
            )


if __name__ == "__main__":
    main()
