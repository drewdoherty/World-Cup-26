#!/usr/bin/env python3
"""Polymarket orderflow ingest CLI — sweep WC26 taker fills into sqlite.

Run with::

    PYTHONPATH=src .venv/bin/python scripts/pm_orderflow_ingest.py

Thin wrapper around :mod:`wca.pm.orderflow`: discovers every in-scope 2026
World Cup team-level market on Polymarket (advancement rungs, winner, group
winners, match 1X2, other team futures), upserts them into
``data/pm_orderflow.db`` and pages the data-api ``/trades`` history for each.
Idempotent — reruns only fetch until they hit already-stored fills. The
data-api history window is capped (offset 3000), so run this regularly or the
older flow is gone for good; markets that hit the cap are logged truncated and
additionally swept with the large-trade CASH filters.

``--open-only`` is the recurring-refresh mode (hourly via
``scripts/wca_orderflow_refresh.sh``): discovery still runs in full, but a
market's trade sweep is skipped only when it is closed AND its latest
``pm_ingest_log`` row was written while closed (``market_closed=1`` — the
frozen tape already got its final sweep). A market that closed since its last
sweep gets exactly one final sweep, so no fills are ever lost to the skip; a
final sweep that FAILS mid-outage is stamped ``market_closed=0``/``failed=1``
and retried on the next run.

``--check-freshness`` is the silent-stall gate (run by the refresh script
after the ingest step): read-only, reports how long ago the last SUCCESSFUL
market sweep ran and — with ``--notify`` — sends a debounced Telegram DM to
the admin when that exceeds ``--stale-hours`` (or when no sweep has ever
succeeded). launchd ignores interval-job exit codes and the watchdog only
covers daemons, so without this a permanently failing ingest keeps advancing
orderflow.json's ``generated_utc`` while the capture is frozen and the
data-api offset window scrolls match-day fills away for good. Mirrors the
pm1x2snapshot ``--notify`` convention; alerting is best-effort and never
fails the job.

``--backfill-leaderboards site/microstructure/orderflow.json`` instead sweeps
the complete per-user fill history of every rendered leaderboard wallet (the
per-user filter gets its own offset window), so featured PnL is not computed
on truncated positions.

Never touches ``data/wca.db``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys

DEFAULT_ALERT_STATE_PATH = os.path.join("data", "orderflow_alert_state.json")


def _load_last_alert_age(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh).get("age_secs"))
    except Exception:
        return None


def _save_last_alert_age(path: str, age_secs) -> None:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"age_secs": age_secs}, fh)
    except Exception as exc:  # noqa: BLE001 — best-effort state, never fatal
        print("alert-state write failed: %s" % exc, file=sys.stderr)


def _notify_stale(age_secs, threshold_hours: float) -> None:
    """Best-effort Telegram DM to the admin. Never raises."""
    try:
        from wca.bot.telegram import TelegramClient

        admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
        if not admin:
            print("stale orderflow capture — no TELEGRAM_ADMIN_USER_ID, alert not sent",
                  file=sys.stderr)
            return
        if age_secs is None:
            body = ("⚠️ *PM orderflow capture: NEVER succeeded* — "
                    "pm_ingest_log has zero successful market-sweep rows. "
                    "Check com.wca.orderflow is scheduled "
                    "(deploy/macmini/services.env + install.sh) and that "
                    "data-api.polymarket.com is reachable.")
        else:
            body = ("⚠️ *PM orderflow capture stale* — last successful market "
                    "sweep %.1fh ago (threshold %.0fh). The data-api offset "
                    "window is scrolling; missed match-day fills are "
                    "permanently lost. Check logs/orderflow.log on the mini."
                    % (age_secs / 3600.0, threshold_hours))
        TelegramClient().send_message(admin, body)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        print("notify failed: %s" % exc, file=sys.stderr)


def check_freshness(db_path: str, *, stale_hours: float,
                    alert_state_path: str, notify: bool) -> int:
    """Read-only staleness gate over pm_ingest_log. Always returns 0.

    Stale is signalled via the (debounced) Telegram alert, NOT the exit code
    — launchd ignores interval-job exit codes anyway, and the refresh script
    already reports its own step failures.
    """
    from wca.pm import orderflow as of

    age = None
    if os.path.exists(db_path):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            try:
                age = of.seconds_since_last_successful_sweep(con)
            finally:
                con.close()
        except sqlite3.OperationalError as exc:
            print("freshness: cannot read %s (%s) — treating as never-succeeded"
                  % (db_path, exc), file=sys.stderr)
    threshold_secs = stale_hours * 3600.0
    fire = of.should_alert_stale(age, _load_last_alert_age(alert_state_path),
                                 threshold_secs)
    if age is None:
        print("freshness: NO successful market sweep recorded in %s" % db_path)
    else:
        print("freshness: last successful market sweep %.1fh ago (stale threshold %.0fh)%s"
              % (age / 3600.0, stale_hours, " — STALE" if age >= threshold_secs else ""))
    if fire and notify:
        _notify_stale(age, stale_hours)
        _save_last_alert_age(alert_state_path, age)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default="data/pm_orderflow.db",
        help="sqlite path (default: data/pm_orderflow.db)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="only discover/upsert markets; skip trade ingestion",
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=None,
        metavar="N",
        help="ingest at most N markets (testing)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="parallel fetch workers (default 8)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip markets already in pm_ingest_log (continue a killed backfill)",
    )
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="hourly-refresh mode: sweep open markets only, plus one "
        "guaranteed final sweep for any market that closed since its last "
        "sweep (closed + last log row market_closed=1 -> skipped, tape is "
        "frozen and captured); discovery still runs in full",
    )
    parser.add_argument(
        "--backfill-leaderboards",
        metavar="ORDERFLOW_JSON",
        default=None,
        help="skip the market sweep; instead backfill the full per-user fill "
        "history of every wallet on the leaderboards of the given "
        "orderflow.json (fixes partial PnL on truncated markets)",
    )
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="read-only staleness gate: report age of the last SUCCESSFUL "
        "market sweep; with --notify, send a debounced Telegram alert to the "
        "admin when it exceeds --stale-hours (or never succeeded)",
    )
    parser.add_argument(
        "--stale-hours",
        type=float,
        default=3.0,
        help="freshness-gate threshold in hours (default 3; the job is hourly)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="with --check-freshness: send the debounced Telegram alert "
        "(best-effort; needs TELEGRAM_BOT_TOKEN + TELEGRAM_ADMIN_USER_ID)",
    )
    parser.add_argument(
        "--alert-state",
        default=DEFAULT_ALERT_STATE_PATH,
        help="path used to debounce repeat alerts (default %s)" % DEFAULT_ALERT_STATE_PATH,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check_freshness:
        return check_freshness(
            args.db,
            stale_hours=args.stale_hours,
            alert_state_path=args.alert_state,
            notify=args.notify,
        )

    if args.backfill_leaderboards:
        from wca.pm.orderflow import backfill_wallets

        with open(args.backfill_leaderboards, "r", encoding="utf-8") as fh:
            feed = json.load(fh)
        wallets: list = []
        for board in (feed.get("leaderboards") or {}).values():
            for row in board or []:
                w = row.get("wallet")
                if w and w not in wallets:
                    wallets.append(w)
        print("backfilling %d leaderboard wallets ..." % len(wallets))
        summary = backfill_wallets(args.db, wallets)
        print("wallets backfilled:  %s" % summary["wallets"])
        print("trades fetched:      %s" % summary["fetched"])
        print("trades new:          %s" % summary["new"])
        if summary["still_truncated"]:
            print("still capped (>3500 fills in one market):")
            for w in summary["still_truncated"]:
                print("  - %s" % w)
        return 0

    from wca.pm.orderflow import run

    summary = run(
        args.db,
        discover_only=args.discover_only,
        max_markets=args.max_markets,
        workers=args.workers,
        resume=args.resume,
        open_only=args.open_only,
    )
    print("markets discovered: %s" % summary.get("markets_discovered"))
    if not args.discover_only:
        if args.open_only:
            print("skipped (closed, final sweep done): %s"
                  % summary.get("skipped_closed_final"))
        print("markets ingested:   %s" % summary.get("markets_ingested"))
        print("trades fetched:     %s" % summary.get("trades_fetched"))
        print("trades new:         %s" % summary.get("trades_new"))
        truncated = summary.get("truncated") or []
        print("truncated markets:  %d" % len(truncated))
        for slug in truncated:
            print("  - %s" % slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
