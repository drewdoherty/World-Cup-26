"""Event-driven Polymarket cash-out watcher (live daemon / one-shot inspector).

Polls our held positions + the live World-Cup score feed and, when a position is
invalidated by a match event (0-0 + a goal, Under 2.5 + the 3rd goal, BTTS-No +
both teams score), sells the dead position into Polymarket's still-lagging book
to capture residual value. The decision/loop logic lives in
:mod:`wca.pm.cashout_watch` (unit-tested); this CLI only wires real I/O.

Safety ladder (you must opt UP each rung explicitly):

  default (no --arm)      SHADOW   — logs what it WOULD sell, never signs.
  --arm  (PM_DRY_RUN=1)   DRY-ARM  — exercises the full path, signs, no submit.
  --arm  PM_DRY_RUN=0     LIVE     — places real cash-out SELLs.

Other rails: a kill switch (``WCA_CASHOUT_OFF=1`` or a --kill-file), a single-
instance flock, a VAR cooldown + dedup claim (see cashout_watch), and a
min-proceeds floor so it never dumps a position for ~nothing.

Usage::

    # one-shot inspection of every held killable position (no orders):
    python scripts/wca_cashout_watch.py --once
    # continuous shadow measurement through a match window:
    python scripts/wca_cashout_watch.py --until 2026-06-13T23:00:00Z --interval 15
    # armed auto-sell (real money once PM_DRY_RUN=0):
    PM_DRY_RUN=0 python scripts/wca_cashout_watch.py --arm --until 2026-06-13T23:00:00Z
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from wca.pm import cashout_state, positions  # noqa: E402
from wca.pm.cashout import classify_market  # noqa: E402
from wca.pm.cashout_watch import CashoutWatcher, WatchConfig  # noqa: E402

_SPORT = "soccer_fifa_world_cup"


def _load_dotenv(path: str = ".env") -> None:
    p = Path(ROOT) / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print("[%s] %s" % (_now_iso(), msg), flush=True)


def _kill_switch_on(kill_file: str | None) -> bool:
    if os.environ.get("WCA_CASHOUT_OFF", "").strip() in ("1", "true", "yes"):
        return True
    return bool(kill_file) and os.path.exists(kill_file)


def _build_trader():
    """Build a ClobTrader for book reads + (armed) execution, or None."""
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        return None
    from wca.pm.trader import ClobTrader, resolve_funder_from_env

    funder, sig_type, _ = resolve_funder_from_env()
    try:
        return ClobTrader(key, funder=funder, signature_type=sig_type)
    except Exception as exc:  # noqa: BLE001
        _log("trader unavailable (%s) — running without book/execution" % exc)
        return None


def _notify(text: str) -> None:
    """Best-effort Telegram alert to the admin (no-op if unconfigured)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not admin:
        return
    try:
        from wca.bot.telegram import TelegramClient

        TelegramClient(token).send_message(admin, text)
    except Exception as exc:  # noqa: BLE001
        _log("telegram notify failed: %s" % exc)


def _make_book_fn(trader):
    def book_fn(asset: str):
        if trader is None:
            return None
        return trader.get_order_book(asset)
    return book_fn


def _make_execute_fn(trader, db_path: str):
    """Armed executor: place + book one cash-out, returning the structured result.

    Records the proposal in the parked-order table for audit (so it shows in
    /pm), then executes via the single source of truth ``bot.execute_cashout``
    (which honours PM_DRY_RUN and books only the actual fill)."""
    from wca.bot import app as bot

    def execute_fn(proposal: dict):
        bot.park_order(proposal)  # audit trail; not awaited for a "Y"
        return bot.execute_cashout(
            proposal, db_path, trader=trader, dry_run=bot._pm_dry_run()
        )

    return execute_fn


def _describe(action: dict) -> str:
    a = action.get("action")
    title = (action.get("title") or "")[:48]
    if a in ("shadow_sell", "sold"):
        p = action.get("proposal") or {}
        money = "$%.2f @ %.3f x%.0f" % (
            p.get("est_proceeds", p.get("price", 0) * p.get("size", 0)),
            p.get("price", 0), p.get("size", 0))
        return "%s %s | %s | %s" % (a.upper(), title, money, action.get("reason", ""))
    if a == "cooldown":
        return "cooldown %s | %ss left | %s" % (
            title, action.get("remaining_s"), action.get("reason", ""))
    return "%s %s | %s" % (a, title, action.get("reason") or action.get("error") or "")


def run_once(watcher: CashoutWatcher, trader, db_path: str, *, armed: bool) -> list:
    # Watch the SAME wallet the trader signs sells from (its funder/proxy), so we
    # never try to sell a position the signing account doesn't actually hold. With
    # no trader (inspection-only / no key) fall back to the manual account-1 wallet.
    if trader is not None and getattr(trader, "funder", None):
        wallet = trader.funder
    else:
        wallet = positions.ACCOUNT1_WALLET
        _log("no trader funder — inspecting account-1 wallet %s (cannot sell)" % wallet)
    pos = positions.fetch_positions(wallet, open_only=True)
    killable = [p for p in pos
                if classify_market(p.title, p.outcome) in watcher.cfg.kinds]
    if not killable:
        _log("no killable positions in wallet %s" % wallet)
        return []
    from wca.data import theoddsapi

    scores, quota = theoddsapi.get_scores(_SPORT, days_from=1)
    _log("%d killable position(s) in %s; scores feed: %d live match(es), quota left %s"
         % (len(killable), wallet[:10] + "…", len(scores), quota.remaining))

    execute_fn = _make_execute_fn(trader, db_path) if armed else (lambda p: None)
    acts = watcher.tick(
        killable, scores, now=time.monotonic(),
        book_fn=_make_book_fn(trader), execute_fn=execute_fn,
    )
    for a in acts:
        _log("  " + _describe(a))
        if a["action"] in ("sold", "error", "settle_failed"):
            _notify(_describe(a))
    return acts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--until", default=None, help="ISO deadline, e.g. 2026-06-13T23:00:00Z")
    ap.add_argument("--interval", type=int, default=15, help="poll seconds (default 15)")
    ap.add_argument("--once", action="store_true", help="single inspection tick, then exit")
    ap.add_argument("--arm", action="store_true",
                    help="ENABLE execution (still honours PM_DRY_RUN; default is shadow)")
    ap.add_argument("--min-proceeds", type=float, default=1.0)
    ap.add_argument("--price-floor", type=float, default=0.0,
                    help="don't sell into bids below this price (0 = any)")
    ap.add_argument("--var-cooldown", type=float, default=45.0,
                    help="seconds a kill must persist before selling (VAR safety)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "wca.db"))
    ap.add_argument("--kill-file", default=os.path.join(ROOT, "data", "CASHOUT_OFF"))
    args = ap.parse_args(argv)

    _load_dotenv()
    cashout_state.init(args.db)

    # Single-instance flock so two daemons can't double-sell.
    import fcntl

    lock_path = os.path.join(ROOT, "data", ".cashout_watch.lock")
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("another cash-out watcher holds the lock; exiting")
        return 1

    trader = _build_trader()
    armed = bool(args.arm)
    dry = os.environ.get("PM_DRY_RUN", "1").strip().lower() not in ("0", "false", "no")
    mode = "SHADOW" if not armed else ("DRY-ARM" if dry else "LIVE")
    _log("cash-out watcher starting | mode=%s | cooldown=%ss | min_proceeds=$%.2f | "
         "floor=%.3f | db=%s" % (mode, args.var_cooldown, args.min_proceeds,
                                 args.price_floor, args.db))
    if armed and not dry:
        _notify("⚠️ Cash-out watcher armed LIVE — will auto-sell invalidated positions.")

    watcher = CashoutWatcher(
        WatchConfig(min_proceeds=args.min_proceeds, price_floor=args.price_floor,
                    var_cooldown_s=args.var_cooldown, arm=armed),
        args.db,
    )

    try:
        if args.once:
            run_once(watcher, trader, args.db, armed=armed)
            return 0

        until = None
        if args.until:
            until = dt.datetime.strptime(args.until, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc)

        while True:
            now = dt.datetime.now(dt.timezone.utc)
            if until and now >= until:
                _log("deadline reached, exiting")
                return 0
            if _kill_switch_on(args.kill_file):
                _log("kill switch ON (WCA_CASHOUT_OFF / kill-file) — paused")
            else:
                try:
                    run_once(watcher, trader, args.db, armed=armed)
                except Exception as exc:  # noqa: BLE001 — one bad poll must not crash the daemon
                    _log("tick error: %s" % exc)
                    _notify("Cash-out watcher tick error: %s" % exc)
            time.sleep(args.interval)
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
