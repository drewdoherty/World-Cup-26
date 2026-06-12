"""CLI: produce Polymarket parked-order proposals from tonight's card.

Pipeline
--------
1. Load ``.env`` (ODDS_API_KEY, TELEGRAM_*, POLYMARKET_FUNDER, ...).
2. Fit the Elo + Dixon-Coles models on the results history.
3. Pull live World Cup h2h odds and keep the next ``--hours-ahead`` window.
4. Build Polymarket-pool proposals (:func:`wca.pm.propose.build_pm_proposals`),
   resolving each card pick to a live Polymarket YES token + price.
5. For each proposal: park it via :func:`wca.bot.app.push_parked_order` (which
   returns the ``PM-<n>`` confirmation text) and send that text to
   ``TELEGRAM_ADMIN_USER_ID`` via :class:`wca.bot.telegram.TelegramClient`.

This script NEVER places an order. It only parks proposals and notifies the
admin; execution stays behind the bot's ``Y PM-<n>`` confirmation gate and the
``PM_DRY_RUN`` flag. Use ``--dry-print`` to inspect the proposals (and the
resolved token ids / prices) without touching Telegram.

Usage::

    python scripts/wca_pm_propose.py --dry-print
    python scripts/wca_pm_propose.py            # parks + notifies the admin
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# Polymarket pool bankroll in USDC (project charter: $2,500 quarter-Kelly).
# The funder fallback (known proxy, never the empty EOA) lives in
# wca.pm.trader.resolve_funder_from_env so the producer and the bot agree.
_DEFAULT_POOL_USD = 2500.0


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (same pattern as the other scripts); never echoes values."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _resolve_funder() -> str:
    """Return POLYMARKET_FUNDER, warning + falling back to the known proxy.

    Delegates to :func:`wca.pm.trader.resolve_funder_from_env` so the producer
    and the bot share one fallback (the proxy, never the empty EOA).
    """
    from wca.pm.trader import resolve_funder_from_env

    funder, _sig_type, used_fallback = resolve_funder_from_env()
    if used_fallback:
        print(
            "WARNING: POLYMARKET_FUNDER not set — falling back to the known "
            "Polymarket proxy %s. USDC sits in the proxy, never the EOA; set "
            "POLYMARKET_FUNDER in .env to silence this." % funder,
            file=sys.stderr,
        )
    return funder


def _augment_for_gate(proposal: dict) -> dict:
    """Add the bot-gate keys (``size`` = shares, ``label``) to a proposal.

    The bot's park/execute gate sizes the order in *shares* (it computes the USD
    notional as ``price * size`` and passes ``size`` straight to
    ``place_order``), and renders a human label from ``proposal['label']``. The
    producer emits ``shares`` / ``size_usd``; mirror ``shares`` onto ``size``
    and derive a label so the parked order both executes and reads correctly.
    """
    p = dict(proposal)
    p["size"] = float(proposal["shares"])  # gate sizes in shares
    home, _, _ = str(proposal.get("match_desc", "")).partition(" vs ")
    # A compact label: the question is the most informative human string.
    p["label"] = proposal.get("market_question") or proposal.get("match_desc") or "market"
    return p


def _format_proposal_line(i: int, p: dict) -> str:
    return (
        "%d. %s | %s @ %.3f | $%.2f (%.1f shares) | model %.1f%% ev %+.1f%% | "
        "token %s%s"
        % (
            i,
            p.get("market_question") or p.get("match_desc"),
            p.get("outcome", "Yes"),
            p["price"],
            p["size_usd"],
            p["shares"],
            p["model_prob"] * 100.0,
            p["ev"] * 100.0,
            p["token_id"],
            " [neg_risk]" if p.get("neg_risk") else "",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Produce Polymarket parked-order proposals from the card."
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
        help="Comma-separated Odds API regions (default: uk)",
    )
    parser.add_argument(
        "--pool-usd",
        type=float,
        default=_DEFAULT_POOL_USD,
        help="Polymarket pool bankroll in USDC (default 2500)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.02,
        help="Minimum sportsbook edge to surface a selection (default 0.02)",
    )
    parser.add_argument(
        "--max-order-usd",
        type=float,
        default=30.0,
        help="Absolute per-order USD ceiling (default 30)",
    )
    parser.add_argument(
        "--dry-print",
        action="store_true",
        help="Print proposals (with resolved token ids) without pushing to Telegram",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    funder = _resolve_funder()

    now_dt = datetime.datetime.utcnow()
    cutoff_dt = now_dt + datetime.timedelta(hours=args.hours_ahead)

    # Heavy imports after arg parsing so --help stays fast.
    try:
        from wca.data.results import load_results  # type: ignore[attr-defined]
        from wca.card import fit_models
        from wca.data import theoddsapi
        from wca.pm.propose import build_pm_proposals
    except ImportError as exc:
        print("ERROR: could not import wca pipeline modules: %s" % exc, file=sys.stderr)
        return 1

    # -- models -----------------------------------------------------------
    try:
        results = load_results("data/raw/results.csv")
        models = fit_models(results)
    except Exception as exc:
        print("ERROR: model fitting failed: %s" % exc, file=sys.stderr)
        return 1

    # -- odds -------------------------------------------------------------
    try:
        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup", regions=args.regions, markets="h2h"
        )
    except Exception as exc:
        print("ERROR: odds pull failed: %s" % exc, file=sys.stderr)
        return 1

    import pandas as pd

    if not odds_df.empty and "commence_time" in odds_df.columns:
        ct = pd.to_datetime(odds_df["commence_time"], errors="coerce", utc=True)
        ct_naive = ct.dt.tz_localize(None) if ct.dt.tz is None else ct.dt.tz_convert(None)
        mask = (ct_naive >= now_dt) & (ct_naive <= cutoff_dt)
        odds_df = odds_df[mask].copy()

    # -- proposals --------------------------------------------------------
    try:
        proposals = build_pm_proposals(
            models,
            odds_df,
            fixtures_meta=results,
            pool_usd=args.pool_usd,
            min_edge=args.min_edge,
            max_order_usd=args.max_order_usd,
        )
    except Exception as exc:
        print("ERROR: proposal build failed: %s" % exc, file=sys.stderr)
        return 1

    total_size = sum(p["size_usd"] for p in proposals)
    print(
        "Funder (maker): %s | pool $%.0f | %d proposal(s), total $%.2f"
        % (funder, args.pool_usd, len(proposals), total_size)
    )
    quota_str = (
        "quota remaining=%s" % quota.remaining
        if quota is not None and getattr(quota, "remaining", None) is not None
        else "quota=unknown"
    )
    print("Odds %s" % quota_str)

    if not proposals:
        print("No proposals to park (no +EV pick resolved to a live token).")
        return 0

    if args.dry_print:
        print("\n-- proposals (dry-print; nothing parked or sent) --")
        for i, p in enumerate(proposals, 1):
            print(_format_proposal_line(i, p))
        return 0

    # -- park + notify ----------------------------------------------------
    from wca.bot.app import push_parked_order
    from wca.bot.telegram import TelegramClient, TelegramError

    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if not admin:
        print(
            "ERROR: TELEGRAM_ADMIN_USER_ID not set — cannot notify. "
            "Proposals not parked.",
            file=sys.stderr,
        )
        return 1

    try:
        client = TelegramClient()
    except TelegramError as exc:
        print("ERROR: Telegram client init failed: %s" % exc, file=sys.stderr)
        return 1

    pushed = 0
    for p in proposals:
        text = push_parked_order(_augment_for_gate(p))
        try:
            client.send_message(admin, text)
            pushed += 1
        except TelegramError as exc:
            print("send error: %s" % exc, file=sys.stderr)

    print(
        "Parked + notified %d/%d proposal(s) to admin %s. "
        "Confirm each with `Y PM-<n>` in Telegram (PM_DRY_RUN gates execution)."
        % (pushed, len(proposals), admin)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
