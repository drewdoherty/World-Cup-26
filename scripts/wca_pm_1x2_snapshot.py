#!/usr/bin/env python
"""Capture a Polymarket 1X2 (match-winner) snapshot into ``odds_snapshots`` so
Polymarket becomes a ranked venue in the Model-vs-Venue benchmark.

Live fetch via :func:`wca.data.polymarket_odds.get_odds`; resolution + insert via
:mod:`wca.pm1x2snapshot` (network-free, unit-tested). Run on a schedule (e.g.
hourly on the mini) to accrue the matched-time H/D/A series the benchmark needs
to move Polymarket off ``COLLECTING``.

    PYTHONPATH=src python3 scripts/wca_pm_1x2_snapshot.py [--db data/wca.db] [--dry-run]
        [--stale-hours 4] [--notify]

Degrades gracefully: if Polymarket is unreachable the fetch returns an empty
frame and nothing is written (never raises).

Freshness gate (2026-07-02, closes a silent-stall postmortem): this CLI
existed and degraded gracefully for a full day with NOTHING scheduling it —
"run on a schedule" was a docstring instruction nobody automated, so
``odds_snapshots`` accrued zero Polymarket rows while the pipeline looked
fine. ``--notify`` makes a persistently-stale (or never-populated) snapshot
loud: a debounced Telegram DM to the admin, reusing the same client/env the
rest of the bot uses. Best-effort — a notify failure never fails the job.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import pm1x2snapshot as pms  # noqa: E402
from wca.data import polymarket_odds  # noqa: E402

DEFAULT_ALERT_STATE_PATH = os.path.join(_ROOT, "data", "pm1x2snapshot_alert_state.json")


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_last_alert_age(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh).get("age_secs"))
    except Exception:
        return None


def _save_last_alert_age(path: str, age_secs) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"age_secs": age_secs, "at": _now_iso_z()}, fh)
    except Exception as exc:  # noqa: BLE001 — best-effort state, never fatal
        print("alert-state write failed: %s" % exc, file=sys.stderr)


def _notify_stale(age_secs, threshold_hours: float) -> None:
    """Best-effort Telegram DM to the admin. Never raises."""
    try:
        from wca.bot.telegram import TelegramClient

        admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
        if not admin:
            print("stale PM 1X2 snapshot — no TELEGRAM_ADMIN_USER_ID, alert not sent",
                  file=sys.stderr)
            return
        if age_secs is None:
            body = ("⚠️ *PM 1X2 snapshot: NEVER captured a row* — "
                    "odds_snapshots has zero source='polymarket' rows. Check "
                    "the job is actually scheduled (deploy/macmini/services.env "
                    "+ install.sh) and that Polymarket is reachable.")
        else:
            body = ("⚠️ *PM 1X2 snapshot stale* — last capture %.1fh ago "
                    "(threshold %.0fh). Polymarket may be unreachable, or the "
                    "live h2h markets have gone quiet." % (age_secs / 3600.0, threshold_hours))
        TelegramClient().send_message(admin, body)
    except Exception as exc:  # noqa: BLE001 — alerting is best-effort
        print("notify failed: %s" % exc, file=sys.stderr)


def check_freshness(
    con: sqlite3.Connection,
    *,
    stale_hours: float,
    alert_state_path: str,
    notify: bool,
    now_iso=None,
) -> dict:
    """Compute snapshot age, decide + (optionally) send the debounced alert.

    Pure decision logic lives in :mod:`wca.pm1x2snapshot`
    (``seconds_since_last_snapshot`` / ``should_alert_stale``); this wrapper
    only adds the I/O (DB read, alert-state file, Telegram send).
    """
    age_secs = pms.seconds_since_last_snapshot(con, now_iso)
    last_alert_age = _load_last_alert_age(alert_state_path)
    threshold_secs = stale_hours * 3600.0
    fire = pms.should_alert_stale(age_secs, last_alert_age, threshold_secs)
    if fire and notify:
        _notify_stale(age_secs, stale_hours)
        _save_last_alert_age(alert_state_path, age_secs)
    return {"age_secs": age_secs, "alert_fired": bool(fire and notify)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "wca.db"))
    ap.add_argument("--ts", default=None, help="capture timestamp (default: now)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + resolve but do not write to the DB (skips the freshness check)")
    ap.add_argument("--stale-hours", type=float, default=4.0,
                    help="alert if the last captured row is older than this (hours)")
    ap.add_argument("--notify", action="store_true",
                    help="send a debounced Telegram alert to the admin on a stale/never-captured snapshot")
    ap.add_argument("--alert-state", default=DEFAULT_ALERT_STATE_PATH,
                    help="path used to debounce repeat alerts")
    args = ap.parse_args(argv)
    ts = args.ts or _now_iso_z()

    frame, _ = polymarket_odds.get_odds(markets="h2h")
    pm_rows = [] if frame is None or frame.empty else frame.to_dict("records")

    if args.dry_run:
        if not pm_rows:
            print("DRY RUN: no Polymarket h2h rows fetched (unreachable or no live markets)")
            return 0
        con = sqlite3.connect(args.db)
        try:
            index = pms.build_match_index(con)
            insert_rows, unmatched = pms.pm_rows_to_snapshot_rows(pm_rows, index, ts)
        finally:
            con.close()
        print("DRY RUN: would insert %d rows | %d unmatched legs | %d fixtures indexed"
              % (len(insert_rows), len(unmatched), len(index)))
        return 0

    con = sqlite3.connect(args.db)
    try:
        if not pm_rows:
            print("no Polymarket h2h rows fetched (unreachable or no live markets) — nothing written")
        else:
            summary = pms.snapshot(con, pm_rows, ts)
            print("PM 1X2 snapshot @ %s: inserted %d rows | unmatched legs %d | indexed %d fixtures"
                  % (ts, summary["inserted"], summary["n_unmatched_legs"], summary["n_fixtures_indexed"]))
            if summary["unmatched_fixtures"]:
                print("  unmatched (no book/model coverage): "
                      + ", ".join(summary["unmatched_fixtures"][:10]))

        # Freshness gate runs on every real (non-dry-run) invocation — even
        # (especially) when this run itself fetched/matched nothing, since
        # that is exactly the failure mode that went unnoticed for a day.
        fresh = check_freshness(
            con, stale_hours=args.stale_hours, alert_state_path=args.alert_state,
            notify=args.notify, now_iso=ts,
        )
        if fresh["age_secs"] is None:
            print("freshness: NEVER captured a row (source='polymarket' is empty)")
        else:
            print("freshness: last capture %.1fh ago (stale threshold %.0fh)%s"
                  % (fresh["age_secs"] / 3600.0, args.stale_hours,
                     " — ALERT" + (" sent" if fresh["alert_fired"] else " suppressed (debounced or --notify off)")
                     if fresh["age_secs"] >= args.stale_hours * 3600.0 else ""))
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
