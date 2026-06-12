"""Matched-betting calculator and promo-offer tracker CLI.

This is the command-line front end for :mod:`wca.matched` (pure-math lay
calculators) and :mod:`wca.offers` (the SEPARATE promo-extraction ledger).

The values handled here are RISK-FREE promo extraction and are tracked apart
from the model-bet ledger / CLV experiment — see the module docstrings.

Usage examples
--------------
Qualifying bet (back 5.0 / lay 5.2 / 10 stake on Smarkets)::

    python scripts/wca_matched.py calc qualifying \\
        --back 5.0 --lay 5.2 --stake 10 --venue smarkets

Stake-not-returned free bet (30 free @ back 6.0 / lay 6.2 at 2%)::

    python scripts/wca_matched.py calc freebet \\
        --back 6.0 --lay 6.2 --stake 30 --commission 0.02

Track an offer::

    python scripts/wca_matched.py offer add \\
        --holder me --bookmaker Bet365 \\
        --desc "Bet 10 get 30" --type free_snr \\
        --qual-stake 10 --qual-loss 0.54 \\
        --free-bet 30 --lay-venue smarkets --status qualified

    python scripts/wca_matched.py offer update --id 1 \\
        --status extracted --extracted 23.79

    python scripts/wca_matched.py offer list
    python scripts/wca_matched.py offer summary
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

# Allow running directly from the repo without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wca import matched, offers  # noqa: E402


# ---------------------------------------------------------------------------
# Commission resolution shared by the calc subcommands.
# ---------------------------------------------------------------------------


def _resolve_commission(args: argparse.Namespace) -> float:
    """Resolve the lay commission from --commission or --venue.

    --commission takes precedence; otherwise --venue maps to a known rate;
    otherwise default to 0.0 (commission-free).
    """
    if args.commission is not None:
        return float(args.commission)
    if args.venue is not None:
        return matched.best_lay_commission(args.venue)
    return 0.0


def _print_kv(rows: List[tuple], title: str) -> None:
    width = max(len(k) for k, _ in rows) + 2
    print("\n=== %s ===" % title)
    for k, v in rows:
        print(("%-" + str(width) + "s %s") % (k + ":", v))


# ---------------------------------------------------------------------------
# calc handlers.
# ---------------------------------------------------------------------------


def cmd_calc_qualifying(args: argparse.Namespace) -> None:
    comm = _resolve_commission(args)
    r = matched.qualifying_bet(
        back_odds=args.back,
        lay_odds=args.lay,
        back_stake=args.stake,
        commission=comm,
    )
    d = r.as_dict()
    rows = [
        ("Back odds", "%.3f" % args.back),
        ("Lay odds", "%.3f" % args.lay),
        ("Back stake", "%.2f" % args.stake),
        ("Commission", "%.2f%%" % (comm * 100)),
        ("Lay stake", "%.2f" % d["lay_stake"]),
        ("Liability", "%.2f" % d["liability"]),
        ("Profit if back wins", "%.2f" % d["profit_if_back_wins"]),
        ("Profit if lay wins", "%.2f" % d["profit_if_lay_wins"]),
        ("Qualifying loss (worst)", "%.2f" % d["worst_case"]),
        ("Rating (loss/stake)", "%.2f%%" % (d["rating"] * 100)),
    ]
    _print_kv(rows, "Qualifying Bet")


def cmd_calc_freebet(args: argparse.Namespace) -> None:
    comm = _resolve_commission(args)
    if args.stake_returned:
        r = matched.free_bet_sr(
            back_odds=args.back, lay_odds=args.lay,
            free_stake=args.stake, commission=comm,
        )
        kind = "Free Bet (stake returned)"
    else:
        r = matched.free_bet_snr(
            back_odds=args.back, lay_odds=args.lay,
            free_stake=args.stake, commission=comm,
        )
        kind = "Free Bet (stake NOT returned)"
    d = r.as_dict()
    rows = [
        ("Back odds", "%.3f" % args.back),
        ("Lay odds", "%.3f" % args.lay),
        ("Free-bet value", "%.2f" % args.stake),
        ("Commission", "%.2f%%" % (comm * 100)),
        ("Lay stake", "%.2f" % d["lay_stake"]),
        ("Liability", "%.2f" % d["liability"]),
        ("Profit if back wins", "%.2f" % d["profit_if_back_wins"]),
        ("Profit if lay wins", "%.2f" % d["profit_if_lay_wins"]),
        ("Locked profit", "%.2f" % d["locked_profit"]),
        ("Retention", "%.2f%%" % (d["retention_pct"] * 100)),
    ]
    _print_kv(rows, kind)


# ---------------------------------------------------------------------------
# offer handlers.
# ---------------------------------------------------------------------------


def cmd_offer_add(args: argparse.Namespace) -> None:
    oid = offers.record_offer(
        account_holder=args.holder,
        bookmaker=args.bookmaker,
        offer_desc=args.desc,
        offer_type=args.type,
        qualifying_stake=args.qual_stake,
        qualifying_loss=args.qual_loss,
        free_bet_value=args.free_bet,
        lay_venue=args.lay_venue,
        extracted_value=args.extracted,
        status=args.status,
        notes=args.notes,
        db_path=args.db,
    )
    print("Recorded offer id=%d (%s @ %s, status=%s)"
          % (oid, args.holder, args.bookmaker, args.status))


def cmd_offer_update(args: argparse.Namespace) -> None:
    fields = {}
    if args.status is not None:
        fields["status"] = args.status
    if args.extracted is not None:
        fields["extracted_value"] = args.extracted
    if args.qual_loss is not None:
        fields["qualifying_loss"] = args.qual_loss
    if args.free_bet is not None:
        fields["free_bet_value"] = args.free_bet
    if args.lay_venue is not None:
        fields["lay_venue"] = args.lay_venue
    if args.notes is not None:
        fields["notes"] = args.notes
    if not fields:
        print("Nothing to update (pass at least one field).")
        return
    offers.update_offer(args.id, db_path=args.db, **fields)
    print("Updated offer id=%d: %s"
          % (args.id, ", ".join("%s=%s" % kv for kv in fields.items())))


def cmd_offer_list(args: argparse.Namespace) -> None:
    rows = offers.list_offers(db_path=args.db)
    if not rows:
        print("(no offers tracked)")
        return
    header = ("id", "holder", "bookmaker", "type", "free_bet",
              "qual_loss", "extracted", "venue", "status")
    print("\n=== Tracked Offers ===")
    fmt = "%-3s %-8s %-14s %-9s %9s %9s %9s %-10s %-9s"
    print(fmt % header)
    for r in rows:
        print(fmt % (
            r["id"],
            (r["account_holder"] or "")[:8],
            (r["bookmaker"] or "")[:14],
            (r["offer_type"] or "")[:9],
            _money(r["free_bet_value"]),
            _money(r["qualifying_loss"]),
            _money(r["extracted_value"]),
            (r["lay_venue"] or "")[:10],
            (r["status"] or "")[:9],
        ))


def _money(v: object) -> str:
    if v is None:
        return "-"
    return "%.2f" % float(v)  # type: ignore[arg-type]


def cmd_offer_summary(args: argparse.Namespace) -> None:
    s = offers.offers_summary(db_path=args.db)
    by_status = ", ".join("%s=%d" % kv for kv in sorted(s["by_status"].items())) or "-"
    rows = [
        ("Offers tracked", str(s["n_offers"])),
        ("By status", by_status),
        ("Total free-bet value", "%.2f" % s["total_free_bet_value"]),
        ("Total qualifying loss", "%.2f" % s["total_qualifying_loss"]),
        ("Total extracted", "%.2f" % s["total_extracted"]),
        ("NET locked (risk-free)", "%.2f" % s["net_locked"]),
    ]
    _print_kv(rows, "Promo Extraction Summary")
    print("\n(Note: promo extraction is tracked SEPARATELY from the model-bet")
    print(" ledger / CLV experiment. These figures are NOT model edge.)")


# ---------------------------------------------------------------------------
# Parser.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wca-matched",
        description="Matched-betting calculator and promo-offer tracker",
    )
    parser.add_argument(
        "--db", default="data/wca.db",
        help="Path to SQLite database file (default: data/wca.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- calc ---------------------------------------------------------------
    calc_p = sub.add_parser("calc", help="Matched-bet lay calculators")
    calc_sub = calc_p.add_subparsers(dest="calc_command", required=True)

    q = calc_sub.add_parser("qualifying", help="Qualifying (stake-returned) bet")
    q.add_argument("--back", required=True, type=float, help="Back (bookie) decimal odds")
    q.add_argument("--lay", required=True, type=float, help="Lay (exchange) decimal odds")
    q.add_argument("--stake", required=True, type=float, help="Back stake")
    q.add_argument("--commission", type=float, default=None,
                   help="Lay commission fraction (e.g. 0.02); overrides --venue")
    q.add_argument("--venue", default=None,
                   help="Exchange venue (smarkets, smarkets_2pc, betfair, "
                        "betfair_basic, matchbook)")

    f = calc_sub.add_parser("freebet", help="Free bet (default: stake NOT returned)")
    f.add_argument("--back", required=True, type=float, help="Back (bookie) decimal odds")
    f.add_argument("--lay", required=True, type=float, help="Lay (exchange) decimal odds")
    f.add_argument("--stake", required=True, type=float, help="Free-bet face value")
    f.add_argument("--commission", type=float, default=None,
                   help="Lay commission fraction (e.g. 0.02); overrides --venue")
    f.add_argument("--venue", default=None,
                   help="Exchange venue (smarkets, smarkets_2pc, betfair, "
                        "betfair_basic, matchbook)")
    f.add_argument("--stake-returned", dest="stake_returned", action="store_true",
                   help="Treat as a stake-RETURNED free bet (rare)")

    # ---- offer --------------------------------------------------------------
    off_p = sub.add_parser("offer", help="Promo-offer tracker (separate ledger)")
    off_sub = off_p.add_subparsers(dest="offer_command", required=True)

    a = off_sub.add_parser("add", help="Record a new offer")
    a.add_argument("--holder", required=True, help="Account holder (e.g. me, mum)")
    a.add_argument("--bookmaker", required=True)
    a.add_argument("--desc", default=None, dest="desc", help="Offer description")
    a.add_argument("--type", default=None, dest="type",
                   help="Offer type (qualifying, free_snr, free_sr, ...)")
    a.add_argument("--qual-stake", dest="qual_stake", type=float, default=None)
    a.add_argument("--qual-loss", dest="qual_loss", type=float, default=None,
                   help="Qualifying loss (store as a positive number)")
    a.add_argument("--free-bet", dest="free_bet", type=float, default=None,
                   help="Free-bet face value")
    a.add_argument("--lay-venue", dest="lay_venue", default=None)
    a.add_argument("--extracted", type=float, default=None,
                   help="Extracted (locked) value")
    a.add_argument("--status", default="claimed",
                   choices=["claimed", "qualified", "extracted", "expired"])
    a.add_argument("--notes", default=None)

    u = off_sub.add_parser("update", help="Update fields on an existing offer")
    u.add_argument("--id", required=True, type=int)
    u.add_argument("--status", default=None,
                   choices=["claimed", "qualified", "extracted", "expired"])
    u.add_argument("--extracted", type=float, default=None)
    u.add_argument("--qual-loss", dest="qual_loss", type=float, default=None)
    u.add_argument("--free-bet", dest="free_bet", type=float, default=None)
    u.add_argument("--lay-venue", dest="lay_venue", default=None)
    u.add_argument("--notes", default=None)

    off_sub.add_parser("list", help="List all tracked offers")
    off_sub.add_parser("summary", help="Aggregate promo-extraction summary")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "calc":
        if args.calc_command == "qualifying":
            cmd_calc_qualifying(args)
        elif args.calc_command == "freebet":
            cmd_calc_freebet(args)
    elif args.command == "offer":
        if args.offer_command == "add":
            cmd_offer_add(args)
        elif args.offer_command == "update":
            cmd_offer_update(args)
        elif args.offer_command == "list":
            cmd_offer_list(args)
        elif args.offer_command == "summary":
            cmd_offer_summary(args)


if __name__ == "__main__":
    main()
