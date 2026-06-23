"""CLI: build and cache tonight's matchday card.

Usage::

    python scripts/wca_build_card.py [--db PATH] [--hours-ahead N]
        [--regions STR] [--out PATH] [--bankroll FLOAT] [--now ISO]

Requires ODDS_API_KEY in the environment (or a .env file at the repo root).
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Tiny .env loader (same pattern as wca_bot.py — no python-dotenv dep)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and cache tonight's World Cup Alpha matchday card."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument(
        "--hours-ahead",
        type=float,
        default=30.0,
        help="Include fixtures starting within this many hours (default 30)",
    )
    parser.add_argument(
        "--regions",
        default="uk",
        help="Comma-separated Odds API regions, e.g. 'uk' or 'uk,eu' (default: uk)",
    )
    parser.add_argument(
        "--out",
        default="data/card_latest.md",
        help="Output path for the cached card (default: data/card_latest.md)",
    )
    parser.add_argument(
        "--next-out",
        default="data/next_latest.md",
        help=(
            "Output path for the cached next-match preview card "
            "(default: data/next_latest.md; pass '' to skip)"
        ),
    )
    parser.add_argument(
        "--goalscorers-out",
        default="data/goalscorers_latest.md",
        help=(
            "Output path for the cached /goalscorers card "
            "(default: data/goalscorers_latest.md; pass '' to skip)"
        ),
    )
    parser.add_argument(
        "--goalscorers-n",
        type=int,
        default=5,
        help="Number of upcoming fixtures in the /goalscorers card (default: 5)",
    )
    parser.add_argument(
        "--skip-scorers",
        action="store_true",
        help="Skip the per-event anytime-scorer odds pull (saves API quota)",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=None,
        help=(
            "Override the sportsbook-pool bankroll in GBP. By default the "
            "bankroll is resolved from the ledger's settled-with-close CLV via "
            "the pre-registered Kelly ladder (rungs £1000/£2500/£5000); pass "
            "this to force a flat figure instead."
        ),
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "Reference ISO-8601 datetime for fixture filtering; "
            "defaults to the actual current UTC time if omitted"
        ),
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    # Determine the reference time.
    if args.now:
        now_str = args.now
    else:
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    # Parse now_str into a datetime for fixture filtering.
    try:
        now_dt = datetime.datetime.fromisoformat(now_str.replace("Z", "+00:00"))
        # Strip tz for naive comparison with the odds feed timestamps.
        if now_dt.tzinfo is not None:
            import datetime as _dt
            now_dt = now_dt.replace(tzinfo=None) - _dt.timedelta(
                seconds=now_dt.utcoffset().total_seconds()  # type: ignore[union-attr]
            )
    except ValueError as exc:
        print("ERROR: could not parse --now value %r: %s" % (args.now, exc), file=sys.stderr)
        sys.exit(1)

    cutoff_dt = now_dt + datetime.timedelta(hours=args.hours_ahead)

    # ------------------------------------------------------------------
    # Import heavy pipeline only after argument parsing so --help is fast.
    # ------------------------------------------------------------------
    try:
        from wca.data.results import load_results  # type: ignore[attr-defined]
        from wca.data.cleaning import resolve_results_path
        from wca.card import (
            fit_models,
            build_card,
            build_score_cards,
            apply_daily_exposure_caps,
            format_card,
            format_scores,
            resolve_pool_bankroll,
            PoolConfig,
        )
        from wca.data import theoddsapi
    except ImportError as exc:
        print("ERROR: could not import wca pipeline modules: %s" % exc, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Load and fit models.
    # ------------------------------------------------------------------
    results_path = resolve_results_path()
    try:
        results = load_results(results_path)
        models = fit_models(results)
    except Exception as exc:
        print("ERROR: model fitting failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Pull live odds.
    # ------------------------------------------------------------------
    try:
        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup",
            regions=args.regions,
            markets="h2h",
        )
    except Exception as exc:
        print("ERROR: odds pull failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Filter fixtures to the requested look-ahead window.
    # ------------------------------------------------------------------
    import pandas as pd

    if not odds_df.empty and "commence_time" in odds_df.columns:
        ct = pd.to_datetime(odds_df["commence_time"], errors="coerce", utc=True)
        # Convert to naive UTC for comparison.
        ct_naive = ct.dt.tz_localize(None) if ct.dt.tz is None else ct.dt.tz_convert(None)
        mask = (ct_naive >= now_dt) & (ct_naive <= cutoff_dt)
        odds_df = odds_df[mask].copy()

    # Neutral/host resolution comes from the results dataframe (scheduled
    # fixture rows carry neutral/country), NOT from the odds feed.
    fixtures_meta = results

    # ------------------------------------------------------------------
    # Resolve the sportsbook-pool bankroll from the ledger via the Kelly
    # ladder (rungs £1000/£2500/£5000, earned by settled-with-close CLV).
    # --bankroll, when supplied, overrides the figure but the rung the
    # evidence would have earned is still reported.
    # ------------------------------------------------------------------
    try:
        pool_bank = resolve_pool_bankroll(args.db, override=args.bankroll)
    except Exception as exc:
        print("ERROR: bankroll resolution failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    print("Pool bankroll: %s" % pool_bank.reason)

    # ------------------------------------------------------------------
    # Build card. The pool uses the rung's authorised Kelly fraction so
    # sizing tracks the same ladder that set the bankroll.
    # ------------------------------------------------------------------
    pool = PoolConfig(
        name="main",
        bankroll=pool_bank.bankroll,
        kelly_fraction=pool_bank.kelly_fraction,
    )
    pools = [pool]

    try:
        recs = build_card(models, odds_df, pools, fixtures_meta=fixtures_meta)
        recs = apply_daily_exposure_caps(recs, pools)
        score_cards = build_score_cards(models, odds_df, fixtures_meta)
    except Exception as exc:
        print("ERROR: card generation failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    card_text = (
        format_card(recs, pools)
        + "\n\n_Pool: %s_\n\n" % pool_bank.reason
        + format_scores(
            score_cards,
            bankroll=pool.bankroll,
            kelly_fraction=pool.kelly_fraction,
            per_bet_cap=pool.per_bet_cap,
        )
    )

    # ------------------------------------------------------------------
    # Write to cache.
    # ------------------------------------------------------------------
    from wca.cardcache import write_card

    write_card(card_text, path=args.out, ts_utc=now_str)

    # ------------------------------------------------------------------
    # Persist the exact blended 1X2 per fixture (latest + append-only log)
    # so the site and prediction tracking read real model output rather
    # than the top-k scoreline approximation.
    # ------------------------------------------------------------------
    try:
        from wca.card import fixture_blends
        from wca.modelpreds import build_predictions, write_predictions

        blends = fixture_blends(models, odds_df, fixtures_meta)
        write_predictions(build_predictions(blends, now_str))
        print("Model predictions persisted: %d fixtures" % len(blends))
    except Exception as exc:
        print("WARNING: model prediction dump failed: %s" % exc, file=sys.stderr)

    # ------------------------------------------------------------------
    # Next-match preview card (/next): winner blend + corners + anytime
    # scorers + scoreline distribution for the earliest kickoff. Failures
    # here must never break the main card build.
    # ------------------------------------------------------------------
    if args.next_out:
        try:
            from wca.nextmatch import (
                SCORER_MARKETS,
                build_next_match,
                format_next_match,
                select_next_blend,
            )
            from wca.card import _iter_fixture_blends, BlendWeights

            scorer_df = None
            if not args.skip_scorers and not odds_df.empty:
                # Per-event endpoint needs the event id of the next fixture.
                blends = _iter_fixture_blends(
                    models, odds_df, fixtures_meta, BlendWeights(),
                    ("United States", "Mexico", "Canada", "USA"),
                )
                nxt = select_next_blend(blends)
                if nxt is not None:
                    try:
                        # Both anytime + first-goalscorer player-prop markets
                        # (each costs extra Odds API credits per region/market).
                        scorer_df, quota = theoddsapi.get_event_odds(
                            "soccer_fifa_world_cup",
                            str(nxt.fx["event_id"]),
                            regions=args.regions,
                            markets=SCORER_MARKETS,
                        )
                    except Exception as exc:
                        print("WARN: scorer odds pull failed: %s" % exc, file=sys.stderr)

            next_card = build_next_match(
                models, odds_df, fixtures_meta, scorer_df=scorer_df,
                pm_lookup=not args.skip_scorers,
                bankroll=pool_bank.bankroll,
                kelly_fraction=pool_bank.kelly_fraction,
            )
            write_card(format_next_match(next_card), path=args.next_out, ts_utc=now_str)
            print("Next-match card written: out=%s" % args.next_out)
        except Exception as exc:
            print("WARN: next-match card failed: %s" % exc, file=sys.stderr)

    # ------------------------------------------------------------------
    # Goalscorers card (/goalscorers): anytime + first-goalscorer recs for the
    # next N fixtures. One per-event scorer pull per fixture (Odds API credits),
    # priced player-level (StatsBomb npxg-share x DC lambda) with Kelly stakes.
    # ------------------------------------------------------------------
    if args.goalscorers_out and args.goalscorers_n and args.goalscorers_n > 0:
        try:
            from wca.card import _iter_fixture_blends, BlendWeights
            from wca.nextmatch import (
                SCORER_MARKETS,
                build_goalscorer_card,
                format_goalscorer_card,
            )

            scorer_by_event = {}
            if not args.skip_scorers and not odds_df.empty:
                _host = ("United States", "Mexico", "Canada", "USA")
                _blends = sorted(
                    _iter_fixture_blends(
                        models, odds_df, fixtures_meta, BlendWeights(), _host
                    ),
                    key=lambda fb: str(fb.fx["commence_time"]),
                )[: args.goalscorers_n]
                for fb in _blends:
                    eid = str(fb.fx.get("event_id"))
                    if not eid or eid == "None" or eid in scorer_by_event:
                        continue
                    try:
                        df, _q = theoddsapi.get_event_odds(
                            "soccer_fifa_world_cup", eid,
                            regions=args.regions, markets=SCORER_MARKETS,
                        )
                        scorer_by_event[eid] = df
                    except Exception as exc:
                        print("WARN: scorer pull failed for %s: %s" % (eid, exc),
                              file=sys.stderr)

            gcards = build_goalscorer_card(
                models, odds_df, fixtures_meta, scorer_by_event,
                top_k_fixtures=args.goalscorers_n,
                bankroll=pool_bank.bankroll,
                kelly_fraction=pool_bank.kelly_fraction,
                pm_lookup=not args.skip_scorers,
            )
            write_card(
                format_goalscorer_card(gcards),
                path=args.goalscorers_out, ts_utc=now_str,
            )
            print("Goalscorers card written: out=%s (%d fixtures)"
                  % (args.goalscorers_out, len(gcards)))
        except Exception as exc:
            print("WARN: goalscorers card failed: %s" % exc, file=sys.stderr)

    quota_str = (
        "quota remaining=%s" % quota.remaining
        if quota is not None and quota.remaining is not None
        else "quota=unknown"
    )
    print(
        "Card written: %d picks, %s, out=%s" % (len(recs), quota_str, args.out)
    )


if __name__ == "__main__":
    main()
