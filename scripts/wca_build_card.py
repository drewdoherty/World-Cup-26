"""CLI: build and cache tonight's matchday card.

Usage::

    python scripts/wca_build_card.py [--db PATH] [--hours-ahead N]
        [--regions STR] [--out PATH] [--bankroll FLOAT] [--now ISO]

Requires ODDS_API_KEY in the environment (or a .env file at the repo root).
"""
from __future__ import annotations

import argparse
import datetime
import json
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

def want_goalscorers_card(goalscorers_out, goalscorers_n, skip_scorers,
                          goalscorers_only, model_available=False) -> bool:
    """Whether this run should (re)build the /goalscorers card.

    Historically the fast ``--skip-scorers`` job must NOT — without scorer
    markets it overwrote the populated card with all-"no scorer market" rows
    (the empty-result bug). But when a built ``players.db`` is available the card
    can be rebuilt **from the model** (free, no Odds API quota), so the fast job
    keeps /goalscorers fresh and aligned with /next instead of letting the two
    drift. The dedicated ``--goalscorers-only`` refresh always rebuilds; a normal
    full build does too.
    """
    return bool(
        goalscorers_out
        and goalscorers_n
        and goalscorers_n > 0
        and (not skip_scorers or goalscorers_only or model_available)
    )


def has_scorer_markets(scorer_by_event) -> bool:
    """True iff at least one fixture actually returned a (non-empty) scorer market.

    Gate the card WRITE on this so a quiet window (no markets posted yet) or a
    transient API miss preserves the last good card instead of clobbering it.
    """
    return any(
        getattr(df, "empty", True) is False for df in (scorer_by_event or {}).values()
    )


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
        "--players-db",
        default="data/players.db",
        help=(
            "Path to the player/team rate store (Phase-2 players.db). When "
            "present, /next and /goalscorers are model-priced from it on every "
            "run — one source of truth — even with no bookmaker market "
            "(labelled 'model price, no market'). Pass '' to disable."
        ),
    )
    parser.add_argument(
        "--goalscorers-only",
        action="store_true",
        help=(
            "Refresh ONLY the /goalscorers card (pull scorer markets for the next "
            "N fixtures and write it); skip the main card, /next and predictions. "
            "Cheap dedicated refresh so the fast --skip-scorers job never has to "
            "clobber the scorer card."
        ),
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

    # write_card is used by every card section below (main, /next, /goalscorers),
    # so import it once here — the --goalscorers-only path skips the main block but
    # still needs it.
    from wca.cardcache import write_card

    # ------------------------------------------------------------------
    # Build the main card (/card, /scores) + persist predictions — UNLESS this
    # is a dedicated --goalscorers-only refresh, which touches only the scorer
    # card and must not re-pull/rewrite the main card, /next or predictions.
    # ------------------------------------------------------------------
    if not args.goalscorers_only:
        # Build card. The pool uses the rung's authorised Kelly fraction so
        # sizing tracks the same ladder that set the bankroll.
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
            + format_scores(score_cards)
        )

        write_card(card_text, path=args.out, ts_utc=now_str)

        # Persist the exact blended 1X2 per fixture (latest + append-only log) so
        # the site and prediction tracking read real model output rather than the
        # top-k scoreline approximation.
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
    if args.next_out and not args.goalscorers_only:
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

            # Model scorer pricing is local + free, so attach it on EVERY run
            # (incl. the fast --skip-scorers job) from the one source of truth
            # (players.db). The bookmaker market is an optional overlay above.
            _db = args.players_db if (args.players_db and os.path.exists(args.players_db)) else None
            next_card = build_next_match(
                models, odds_df, fixtures_meta, scorer_df=scorer_df,
                pm_lookup=not args.skip_scorers,
                bankroll=pool_bank.bankroll,
                kelly_fraction=pool_bank.kelly_fraction,
                db_path=_db,
                model_only_fallback=_db is not None,
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
    # Only build the scorer card when we are actually pulling scorers. The fast
    # --skip-scorers job must NOT run this — otherwise it overwrites a good card
    # with an all-"no scorer market" one (the /goalscorers empty-result bug). The
    # dedicated --goalscorers-only refresh job keeps the card current.
    _gs_db = args.players_db if (args.players_db and os.path.exists(args.players_db)) else None
    want_goalscorers = want_goalscorers_card(
        args.goalscorers_out, args.goalscorers_n, args.skip_scorers,
        args.goalscorers_only, model_available=_gs_db is not None,
    )
    if want_goalscorers:
        try:
            from wca.card import _iter_fixture_blends, BlendWeights
            from wca.nextmatch import (
                SCORER_MARKETS,
                build_goalscorer_card,
                format_goalscorer_card,
            )

            scorer_by_event = {}
            if not odds_df.empty:
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

            # Write when a real scorer market exists OR the model can price the
            # card from players.db. The old preserve-last-good guard only applied
            # because a market-only card went empty in quiet windows; a
            # model-priced card is never empty, so it is safe (and aligned with
            # /next) to refresh it every run. Only when BOTH are unavailable do
            # we preserve the last good card.
            if has_scorer_markets(scorer_by_event) or _gs_db is not None:
                gcards = build_goalscorer_card(
                    models, odds_df, fixtures_meta, scorer_by_event,
                    top_k_fixtures=args.goalscorers_n,
                    bankroll=pool_bank.bankroll,
                    kelly_fraction=pool_bank.kelly_fraction,
                    pm_lookup=not args.skip_scorers,
                    db_path=_gs_db,
                    model_only_fallback=_gs_db is not None,
                )
                basis = ("market+model" if has_scorer_markets(scorer_by_event)
                         else "model-only (no bookmaker market)")
                write_card(
                    format_goalscorer_card(gcards),
                    path=args.goalscorers_out, ts_utc=now_str,
                )
                print("Goalscorers card written: out=%s (%d fixtures, %s)"
                      % (args.goalscorers_out, len(gcards), basis))
            else:
                print("Goalscorers card: no scorer markets and no players.db — "
                      "preserving the existing card (not overwriting with empties)",
                      file=sys.stderr)
        except Exception as exc:
            print("WARN: goalscorers card failed: %s" % exc, file=sys.stderr)

    # ------------------------------------------------------------------
    # Persist the unified model-scorer source (data/model_scorers.json) for the
    # next N fixtures. This is the ONE on-disk source /accas (and the site) read,
    # priced from the SAME model as /next + /goalscorers — so the commands cannot
    # drift. Pure model, no Odds API quota; written on every run when players.db
    # exists.
    if _gs_db is not None and not odds_df.empty:
        try:
            from wca.card import _iter_fixture_blends, BlendWeights
            from wca.models.scorer_props import fixture_scorers_payload

            _host = ("United States", "Mexico", "Canada", "USA")
            _msb = sorted(
                _iter_fixture_blends(models, odds_df, fixtures_meta, BlendWeights(), _host),
                key=lambda fb: str(fb.fx["commence_time"]),
            )[: args.goalscorers_n]
            payloads = []
            for fb in _msb:
                try:
                    pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
                    lh = float(getattr(pred, "lambda_home", 0.0) or 0.0)
                    la = float(getattr(pred, "lambda_away", 0.0) or 0.0)
                    if lh <= 0 or la <= 0:
                        continue
                    p = fixture_scorers_payload(fb.home, fb.away, lh, la, db_path=_gs_db)
                    p["commence_time"] = str(fb.fx.get("commence_time"))
                    payloads.append(p)
                except Exception:
                    continue
            out_obj = {"meta": {"generated": now_str, "source": _gs_db,
                                "basis": "model price (players.db npxg-share x DC lambda)"},
                       "fixtures": payloads}
            with open("data/model_scorers.json", "w", encoding="utf-8") as fh:
                json.dump(out_obj, fh, indent=2)
            print("Model-scorers source written: data/model_scorers.json (%d fixtures)"
                  % len(payloads))
        except Exception as exc:
            print("WARN: model_scorers.json write failed: %s" % exc, file=sys.stderr)

    quota_str = (
        "quota remaining=%s" % quota.remaining
        if quota is not None and quota.remaining is not None
        else "quota=unknown"
    )
    if args.goalscorers_only:
        # No main card in this mode; the goalscorers section logged its own result.
        print("Goalscorers-only refresh complete (%s)" % quota_str)
    else:
        print(
            "Card written: %d picks, %s, out=%s" % (len(recs), quota_str, args.out)
        )


if __name__ == "__main__":
    main()
