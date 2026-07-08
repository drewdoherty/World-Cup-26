#!/usr/bin/env python
"""Score the SHADOW model variants and emit ``site/shadow_scoreboard.json``.

SHADOW-ONLY. This CLI only gathers inputs (the model-predictions log + the
realised-results file + a clock) and hands them to the deterministic
:mod:`wca.shadowscore`; it never touches live pricing, sizing, or selection.

For every shadow family present it computes paired Brier + log-loss against the
deployed blend, with n, bootstrap 90% CIs on the paired diff, a group/knockout
split, and a PROMOTE / KILL / COLLECTING decision (n>=30 gate). The 1X2 shadows
(``mw90`` / ``shrink``) are recomputed over ALL historical settled fixtures; the
goal-lambda shadow families are DISCOVERED dynamically from whatever
``<prefix>_lambda_home`` / ``<prefix>_lambda_away`` (or ``..._blend_home/away``)
keys are actually present in the log (currently ``gb`` and ``tl`` — a future
dual-write is picked up automatically, no code change needed) and each settles
a total-goals over/under market plus a BTTS Brier and mean signed goal-lambda
bias, only where their lambdas were logged.

Idempotent and fast (<30s) — trivially cronable. NOTE: registering it as a
launchd job on the Mac mini is a human step (``bash deploy/macmini/install.sh``
per CLAUDE.md).

Usage
-----
    python scripts/wca_shadow_score.py \
        [--log data/model_predictions_log.jsonl] \
        [--results data/processed/wc2026_results.json] \
        [--out site/shadow_scoreboard.json] \
        [--now "YYYY-MM-DDTHH:MM:SS"] [--print]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Mapping

# Make ``src`` importable when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import shadowscore  # noqa: E402

DEFAULT_LOG = "data/model_predictions_log.jsonl"
DEFAULT_RESULTS = "data/processed/wc2026_results.json"
DEFAULT_OUT = "site/shadow_scoreboard.json"


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def load_log(path: str) -> List[Dict[str, Any]]:
    """Read the append-only prediction log (one JSON object per line).

    Malformed lines are skipped rather than aborting the run, so a single bad
    append can never blind the scorer to the rest of the history.
    """
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def load_results(path: str) -> List[Dict[str, Any]]:
    """Read the realised-results file (``{"results": [...]}``)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    results = data.get("results") if isinstance(data, dict) else None
    return [r for r in results if isinstance(r, dict)] if results else []


def _fmt(x: Any, digits: int = 4) -> str:
    if x is None:
        return "   -   "
    return ("%+.*f" % (digits, x)) if isinstance(x, float) else str(x)


def render_print(scoreboard: Mapping[str, Any]) -> str:
    """Compact terminal table for ``--print``."""
    meta = scoreboard.get("meta", {})
    lines: List[str] = []
    lines.append("SHADOW SCOREBOARD  (generated %s)" % meta.get("generated", "?"))
    lines.append(
        "matched fixtures: %s / %s   |   lower is better, diff = shadow - live "
        "(negative = shadow wins)"
        % (meta.get("matched_fixtures", "?"), meta.get("total_results", "?"))
    )
    header = "%-8s %-7s %4s  %-9s %-9s  %-9s %-9s   %s" % (
        "family", "market", "n",
        "brierΔ", "brierCI", "loglossΔ", "loglossCI", "decision",
    )
    lines.append(header)
    lines.append("-" * len(header))
    for r in scoreboard.get("shadows", []):
        b_ci = "[%s,%s]" % (_fmt(r["brier_ci_lo"], 3), _fmt(r["brier_ci_hi"], 3))
        ll_ci = "[%s,%s]" % (_fmt(r["logloss_ci_lo"], 3), _fmt(r["logloss_ci_hi"], 3))
        lines.append(
            "%-8s %-7s %4d  %-9s %-19s %-9s %-19s %s"
            % (
                r["family"], r["market"], r["n"],
                _fmt(r["brier_diff"], 4), b_ci,
                _fmt(r["logloss_diff"], 4), ll_ci,
                r["decision"],
            )
        )
        btts = r.get("btts")
        bias = r.get("goal_bias")
        if btts is not None or bias is not None:
            btts_ci = (
                "[%s,%s]" % (_fmt(btts["brier_ci_lo"], 3), _fmt(btts["brier_ci_hi"], 3))
                if btts else "   -   "
            )
            lines.append(
                "         %-7s %4s  btts_brierΔ=%s ci=%-19s  bias(shadow/live)=%s/%s"
                % (
                    "btts/bias", (btts["n"] if btts else "-"),
                    _fmt(btts["brier_diff"], 4) if btts else "   -   ",
                    btts_ci,
                    _fmt(bias["mean_shadow"], 3) if bias else "   -   ",
                    _fmt(bias["mean_live"], 3) if bias else "   -   ",
                )
            )
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=os.path.join(_ROOT, DEFAULT_LOG))
    parser.add_argument("--results", default=os.path.join(_ROOT, DEFAULT_RESULTS))
    parser.add_argument("--out", default=os.path.join(_ROOT, DEFAULT_OUT))
    parser.add_argument("--now", default=None,
                        help="Override the generated timestamp (default: now UTC).")
    parser.add_argument("--print", action="store_true", dest="do_print",
                        help="Print a compact table to stdout.")
    parser.add_argument("--no-write", action="store_true",
                        help="Skip writing the JSON (useful with --print).")
    args = parser.parse_args(argv)

    now = args.now or _now_utc_iso()
    log_rows = load_log(args.log)
    results = load_results(args.results)
    scoreboard = shadowscore.build_scoreboard(log_rows, results, now)

    if not args.no_write:
        out_dir = os.path.dirname(args.out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(scoreboard, indent=2, sort_keys=True) + "\n")

    if args.do_print:
        print(render_print(scoreboard))
    elif not args.no_write:
        print("wrote %s (matched %d fixtures)"
              % (args.out, scoreboard["meta"]["matched_fixtures"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
