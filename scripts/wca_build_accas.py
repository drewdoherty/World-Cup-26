"""CLI: build and cache today's World Cup acca report.

Reads cached model data (``data/model_predictions.json``) and the odds
feed (``site/scores_data.json``) and writes a pre-formatted Telegram
message to ``data/accas_latest.md`` via :func:`~wca.cardcache.write_card`.

The bot handler reads the cache file; this script is the only place that
does live data access or model computation.  Run it from cron after
``wca_build_card.py`` so the model predictions are already fresh.

Usage::

    python scripts/wca_build_accas.py [--db PATH] [--bankroll FLOAT]
        [--predictions PATH] [--scores PATH] [--out PATH] [--window FLOAT]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal .env loader (same pattern as other scripts in this project)
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
        description="Build and cache today's World Cup accumulator report."
    )
    parser.add_argument(
        "--db",
        default="data/wca.db",
        help="SQLite ledger path (used for Kelly-ladder bankroll lookup; default: data/wca.db)",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=None,
        help=(
            "Override the sportsbook-pool bankroll in GBP. By default the "
            "bankroll is resolved from the ledger's CLV-ladder rung."
        ),
    )
    parser.add_argument(
        "--predictions",
        default="data/model_predictions.json",
        help="Path to model predictions JSON (default: data/model_predictions.json)",
    )
    parser.add_argument(
        "--scores",
        default="site/scores_data.json",
        help="Path to scores / odds feed JSON (default: site/scores_data.json)",
    )
    parser.add_argument(
        "--out",
        default="data/accas_latest.md",
        help="Output path for the cached accas card (default: data/accas_latest.md)",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=30.0,
        help="Include fixtures whose kickoff is within this many hours (default: 30)",
    )
    args = parser.parse_args()

    _load_dotenv()

    # ------------------------------------------------------------------ #
    # Resolve bankroll from ledger CLV ladder (mirrors wca_build_card.py) #
    # ------------------------------------------------------------------ #
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

    bankroll = args.bankroll
    if bankroll is None:
        try:
            from wca.card import LADDER_BANKROLLS, resolve_pool_bankroll

            pb = resolve_pool_bankroll(args.db, bankrolls=LADDER_BANKROLLS)
            bankroll = pb.bankroll
            print("[accas] Kelly ladder: rung %d → bankroll £%.0f (%s)"
                  % (pb.rung, bankroll, pb.reason))
        except Exception as exc:
            bankroll = 1500.0
            print("[accas] Could not resolve Kelly ladder (%s); using £%.0f" % (exc, bankroll))

    # ------------------------------------------------------------------ #
    # Build the report                                                     #
    # ------------------------------------------------------------------ #
    from wca import accas as accas_mod
    from wca import cardcache

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    print("[accas] Building report for %s (window %.0fh)…" % (now_str, args.window))

    try:
        report = accas_mod.build_accas_report(
            predictions_path=args.predictions,
            scores_path=args.scores,
            bankroll=float(bankroll),
            now=now,
            window_hours=args.window,
        )
    except Exception as exc:
        print("[accas] ERROR building report: %s" % exc, file=sys.stderr)
        sys.exit(1)

    text = accas_mod.format_acca_report(report)

    # ------------------------------------------------------------------ #
    # Cache                                                                #
    # ------------------------------------------------------------------ #
    try:
        cardcache.write_card(text, path=args.out, ts_utc=now_str)
        print("[accas] Cached to %s" % args.out)
    except Exception as exc:
        print("[accas] ERROR writing cache: %s" % exc, file=sys.stderr)
        sys.exit(1)

    # Brief summary to stdout.
    n_types = sum(1 for x in (report.safe, report.value, report.longshot) if x is not None)
    if n_types:
        print("[accas] Built %d acca type(s) from %d fixture(s)."
              % (n_types, report.fixtures_analysed))
    else:
        print("[accas] NO BET: %s" % report.no_bet_reason)


if __name__ == "__main__":
    main()
