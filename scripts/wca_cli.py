"""World Cup Alpha command-line interface.

Usage examples
--------------
Record a bet::

    python scripts/wca_cli.py bet add \\
        --ts 2026-06-11T14:00:00 \\
        --match-id GRP_A_01 \\
        --match-desc "Mexico vs Canada" \\
        --market 1X2 --selection Home \\
        --platform Bet365 \\
        --odds 2.10 --stake 25.00 \\
        --model-prob 0.52 --market-prob-devig 0.49 \\
        --ev 3.00 --kelly 0.06

Settle a bet as won::

    python scripts/wca_cli.py bet settle --id 1 --result won

Record closing odds::

    python scripts/wca_cli.py bet close-odds --id 1 --odds 1.95

Print summary::

    python scripts/wca_cli.py report summary

Print CLV report::

    python scripts/wca_cli.py report clv

Print calibration report::

    python scripts/wca_cli.py report calibration

Add bankroll deposit::

    python scripts/wca_cli.py bankroll add --ts 2026-06-10T12:00:00 --amount 1000 --reason "Initial deposit"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from wca.ledger import store, reports


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _fmt_float(v: object, decimals: int = 4) -> str:
    if v is None or (isinstance(v, float) and (v != v)):  # NaN check
        return "N/A"
    try:
        return ("%." + str(decimals) + "f") % float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(v)


def _print_df(df: pd.DataFrame) -> None:
    if df.empty:
        print("(no data)")
    else:
        print(df.to_string(index=False))


# ---------------------------------------------------------------------------
# Sub-command handlers.
# ---------------------------------------------------------------------------


def cmd_bet_add(args: argparse.Namespace) -> None:
    ts = args.ts or _now_utc()
    bet_id = store.record_bet(
        ts_utc=ts,
        match_id=args.match_id,
        match_desc=args.match_desc,
        market=args.market,
        selection=args.selection,
        platform=args.platform,
        decimal_odds=args.odds,
        stake=args.stake,
        model_prob=args.model_prob,
        market_prob_devig=args.market_prob_devig,
        ev=args.ev,
        kelly_fraction=args.kelly,
        notes=args.notes,
        # Manual single-bet entry: regenerate + publish the site so the bet
        # shows up without a separate sync step.
        sync_site=True,
        db_path=args.db,
    )
    print("Recorded bet id=%d" % bet_id)


def cmd_bet_settle(args: argparse.Namespace) -> None:
    store.settle_bet(bet_id=args.id, result=args.result, db_path=args.db)
    print("Settled bet id=%d as %s" % (args.id, args.result.lower()))


def cmd_bet_close_odds(args: argparse.Namespace) -> None:
    store.set_closing_odds(bet_id=args.id, closing_odds=args.odds, db_path=args.db)
    row = store.get_bet(args.id, db_path=args.db)
    if row is not None:
        print(
            "Updated bet id=%d  closing_odds=%.4f  CLV=%.4f (%.2f%%)"
            % (args.id, float(row["closing_odds"]), float(row["clv"]), float(row["clv"]) * 100)
        )
    else:
        print("Updated bet id=%d" % args.id)


def cmd_report_summary(args: argparse.Namespace) -> None:
    s = reports.summary(db_path=args.db)
    rows = [
        ("Total bets", str(s["total_bets"])),
        ("  Open", str(s["open_bets"])),
        ("  Won", str(s["won_bets"])),
        ("  Lost", str(s["lost_bets"])),
        ("  Void", str(s["void_bets"])),
        ("Total staked", _fmt_float(s["total_staked"], 2)),
        ("Total P&L", _fmt_float(s["total_pl"], 2)),
        ("ROI", _fmt_float(s["roi"] * 100 if s["roi"] == s["roi"] else s["roi"], 2) + "%"),
        ("Avg CLV", _fmt_float(s["avg_clv"] * 100 if s["avg_clv"] == s["avg_clv"] else s["avg_clv"], 2) + "%"),
        ("% bets beat close", _fmt_float(s["pct_beat_close"] * 100 if s["pct_beat_close"] == s["pct_beat_close"] else s["pct_beat_close"], 1) + "%"),
        ("Brier score (model)", _fmt_float(s["brier_model"])),
        ("Brier score (market)", _fmt_float(s["brier_market"])),
        ("Total deposited", _fmt_float(s["total_deposited"], 2)),
        ("Current bankroll", _fmt_float(s["current_bankroll"], 2)),
    ]
    width = max(len(k) for k, _ in rows) + 2
    print("\n=== World Cup Alpha Summary ===")
    for k, v in rows:
        print(("%-" + str(width) + "s %s") % (k + ":", v))


def cmd_report_clv(args: argparse.Namespace) -> None:
    data = reports.clv_report(db_path=args.db)
    print("\n=== CLV Report ===")
    print("Bets with closing odds: %d" % data["n_bets"])
    print("Average CLV: %s%%" % _fmt_float(
        data["avg_clv"] * 100 if data["avg_clv"] == data["avg_clv"] else data["avg_clv"], 2
    ))
    print("Bets that beat close: %s%%" % _fmt_float(
        data["pct_beat_close"] * 100 if data["pct_beat_close"] == data["pct_beat_close"] else data["pct_beat_close"], 1
    ))
    print()
    _print_df(data["per_bet"])


def cmd_report_calibration(args: argparse.Namespace) -> None:
    data = reports.calibration_report(db_path=args.db)
    print("\n=== Calibration Report ===")
    print("Settled bets: %d" % data["n_settled"])
    print("Brier score (model):  %s" % _fmt_float(data["brier_model"]))
    print("Brier score (market): %s" % _fmt_float(data["brier_market"]))
    print()
    _print_df(data["calibration_bins"])


def cmd_bankroll_add(args: argparse.Namespace) -> None:
    ts = args.ts or _now_utc()
    ev_id = store.add_bankroll_event(
        ts_utc=ts,
        amount=args.amount,
        reason=args.reason,
        db_path=args.db,
    )
    verb = "Deposit" if args.amount >= 0 else "Withdrawal"
    print("%s recorded (id=%d): %.2f" % (verb, ev_id, args.amount))


# ---------------------------------------------------------------------------
# Argument parser.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wca",
        description="World Cup Alpha betting ledger CLI",
    )
    parser.add_argument(
        "--db",
        default="data/wca.db",
        help="Path to SQLite database file (default: data/wca.db)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- bet ----------------------------------------------------------------
    bet_parser = sub.add_parser("bet", help="Bet management")
    bet_sub = bet_parser.add_subparsers(dest="bet_command", required=True)

    # bet add
    add_p = bet_sub.add_parser("add", help="Record a new bet")
    add_p.add_argument("--ts", default=None, help="Timestamp UTC (ISO-8601); defaults to now")
    add_p.add_argument("--match-id", required=True, dest="match_id")
    add_p.add_argument("--match-desc", required=True, dest="match_desc")
    add_p.add_argument("--market", required=True)
    add_p.add_argument("--selection", required=True)
    add_p.add_argument("--platform", required=True)
    add_p.add_argument("--odds", required=True, type=float, help="Decimal odds taken")
    add_p.add_argument("--stake", required=True, type=float)
    add_p.add_argument("--model-prob", dest="model_prob", type=float, default=None)
    add_p.add_argument("--market-prob-devig", dest="market_prob_devig", type=float, default=None)
    add_p.add_argument("--ev", type=float, default=None, help="Expected value in currency")
    add_p.add_argument("--kelly", type=float, default=None, dest="kelly", help="Kelly fraction used")
    add_p.add_argument("--notes", default=None)

    # bet settle
    settle_p = bet_sub.add_parser("settle", help="Settle a bet won/lost")
    settle_p.add_argument("--id", required=True, type=int)
    settle_p.add_argument("--result", required=True, choices=["won", "lost"])

    # bet close-odds
    close_p = bet_sub.add_parser("close-odds", help="Record closing odds and compute CLV")
    close_p.add_argument("--id", required=True, type=int)
    close_p.add_argument("--odds", required=True, type=float, help="Closing decimal odds")

    # ---- report -------------------------------------------------------------
    report_parser = sub.add_parser("report", help="Reporting")
    report_sub = report_parser.add_subparsers(dest="report_command", required=True)
    report_sub.add_parser("summary", help="Print portfolio summary")
    report_sub.add_parser("clv", help="Print CLV report")
    report_sub.add_parser("calibration", help="Print calibration report")

    # ---- bankroll -----------------------------------------------------------
    br_parser = sub.add_parser("bankroll", help="Bankroll management")
    br_sub = br_parser.add_subparsers(dest="br_command", required=True)
    br_add_p = br_sub.add_parser("add", help="Record a deposit or withdrawal")
    br_add_p.add_argument("--ts", default=None, help="Timestamp UTC (ISO-8601); defaults to now")
    br_add_p.add_argument("--amount", required=True, type=float,
                          help="Positive for deposit, negative for withdrawal")
    br_add_p.add_argument("--reason", default=None)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "bet":
        if args.bet_command == "add":
            cmd_bet_add(args)
        elif args.bet_command == "settle":
            cmd_bet_settle(args)
        elif args.bet_command == "close-odds":
            cmd_bet_close_odds(args)
    elif args.command == "report":
        if args.report_command == "summary":
            cmd_report_summary(args)
        elif args.report_command == "clv":
            cmd_report_clv(args)
        elif args.report_command == "calibration":
            cmd_report_calibration(args)
    elif args.command == "bankroll":
        if args.br_command == "add":
            cmd_bankroll_add(args)


if __name__ == "__main__":
    main()
