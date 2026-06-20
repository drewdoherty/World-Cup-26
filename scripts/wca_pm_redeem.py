"""CLI/cron: redeem (cancel) Polymarket orders unfilled past a deadline.

The V2 exchange has no signed expiration, so we emulate GTD: orders rest as GTC
and this job cancels any still unfilled past ``--max-age-hours`` (default 24),
returning the reserved pUSD.  It also powers the bot's instant-override
(``REDEEM PM-<n>`` / ``REDEEM ALL``) via ``--order-id`` / ``--all``.

    python scripts/wca_pm_redeem.py                 # cancel orders unfilled >24h
    python scripts/wca_pm_redeem.py --max-age-hours 6
    python scripts/wca_pm_redeem.py --all           # cancel EVERY open order now
    python scripts/wca_pm_redeem.py --order-id 0x..  # cancel one order now
    python scripts/wca_pm_redeem.py --dry-run        # show, cancel nothing
    python scripts/wca_pm_redeem.py --notify         # also DM the admin a summary

Cancelling only removes unfilled orders (frees reserved collateral); it never
risks money, so it is NOT gated by ``PM_DRY_RUN`` — use ``--dry-run`` to preview.
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Redeem (cancel) Polymarket orders unfilled past a deadline."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="Cancel orders unfilled at least this long (default 24)")
    parser.add_argument("--all", action="store_true", dest="redeem_all",
                        help="Cancel EVERY open order now (instant override)")
    parser.add_argument("--order-id", default=None,
                        help="Cancel exactly this order id now (instant override)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be cancelled; cancel nothing")
    parser.add_argument("--notify", action="store_true",
                        help="DM the admin a summary via Telegram")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args(argv)

    _load_dotenv(args.env)

    from wca.pm import redeem as redeem_core

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set — cannot reach the CLOB.",
              file=sys.stderr)
        return 1

    try:
        from wca.pm.trader import ClobTrader, resolve_funder_from_env
    except Exception as exc:  # noqa: BLE001
        print("ERROR: trader import failed: %s" % exc, file=sys.stderr)
        return 1

    funder, sig_type, _ = resolve_funder_from_env()
    try:
        trader = ClobTrader(key, funder=funder, signature_type=sig_type)
    except Exception as exc:  # noqa: BLE001
        print("ERROR: could not init trader: %s" % exc, file=sys.stderr)
        return 1

    try:
        orders = trader.open_orders()
    except Exception as exc:  # noqa: BLE001
        print("ERROR: could not fetch open orders: %s" % exc, file=sys.stderr)
        return 1

    now_epoch = datetime.datetime.now(datetime.timezone.utc).timestamp()
    log_map = redeem_core.log_epoch_by_id(args.db)

    selected = redeem_core.select_orders_to_redeem(
        orders, now_epoch,
        max_age_hours=args.max_age_hours,
        order_id=args.order_id,
        redeem_all=args.redeem_all,
        log_epoch_by_id=log_map,
    )

    print("Open orders: %d | selected to redeem: %d (max-age %.0fh%s)" % (
        len(orders), len(selected), args.max_age_hours,
        ", DRY-RUN" if args.dry_run else "",
    ))

    cancelled, freed, failures = [], 0.0, []
    for order, reason in selected:
        oid = redeem_core.order_id_of(order) or "?"
        rem = redeem_core.unfilled_size(order) or 0.0
        price = order.get("price")
        notional = (float(price) * rem) if price not in (None, "") else 0.0
        line = "%s (%s) — %.1f sh, ~$%.2f freed" % (oid, reason, rem, notional)
        if args.dry_run:
            print("  WOULD CANCEL: " + line)
            cancelled.append(line)
            freed += notional
            continue
        try:
            trader.cancel_order(oid)
            print("  CANCELLED: " + line)
            cancelled.append(line)
            freed += notional
        except Exception as exc:  # noqa: BLE001 — keep going, report at end
            print("  FAILED %s: %s" % (oid, exc), file=sys.stderr)
            failures.append("%s: %s" % (oid, exc))

    verb = "Would free" if args.dry_run else "Freed"
    summary = "%s ~$%.2f pUSD across %d order(s)" % (verb, freed, len(cancelled))
    if failures:
        summary += " | %d FAILED" % len(failures)
    print(summary)

    if args.notify and (cancelled or failures):
        try:
            from wca.bot.telegram import TelegramClient
            admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
            if admin:
                head = "🧹 *PM redeem* — %s\n" % summary
                body = head + "\n".join("• " + c for c in cancelled[:20])
                if failures:
                    body += "\n⚠️ " + "\n⚠️ ".join(failures[:10])
                TelegramClient().send_message(admin, body)
        except Exception as exc:  # noqa: BLE001 — notification is best-effort
            print("notify failed: %s" % exc, file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
