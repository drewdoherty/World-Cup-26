#!/usr/bin/env python
"""Dedup economically-identical "advance" bets across ``bet_recs.json`` and
``event_market_recs.json``.

``wca_betrecs.py``'s advancement-futures section and ``wca_event_markets.py``'s
"Team to Advance" family price the SAME real-world event (a team winning its
next knockout tie) via TWO DIFFERENT Polymarket instruments. They run on
independent schedules with zero mutual awareness, so both can recommend cash
on "England reaches SF" at once — double-counting one exposure (CLAUDE.md
"Whole-book: size ALL bets together; worst case respects the hard cash
floor"). All matching/tie-break logic lives in :mod:`wca.tie_exposure`; this
CLI only loads the two feeds, applies it, and rewrites whichever changed.

Read-only when there is nothing to reconcile — files are only rewritten if a
duplicate was found and zeroed.

Usage
-----
    PYTHONPATH=src python3 scripts/wca_exposure_reconcile.py \
        [--bet-recs site/bet_recs.json] [--event-recs site/event_market_recs.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any, Dict, List

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from wca.tie_exposure import (  # noqa: E402
    REASON_DUP_TIE_EXPOSURE,
    find_cross_feed_duplicates,
    resolve_duplicate,
)

_STAGE_RE = re.compile(r"stage=(\w+)")
_TEAM_SUFFIX = " to advance"


def _backfill_legacy_fields(em_legs: List[Dict[str, Any]]) -> None:
    """Derive ``team``/``tie_stage`` for rows built before those fields were
    stamped at generation time (2026-07-11). Both are recoverable from
    ``label`` ("<team> to advance") and ``model_source``
    ("...stage=<stage>)"), so an older feed on disk still gets deduped
    instead of silently skipped until it's next rebuilt.
    """
    for leg in em_legs:
        if not leg.get("team") and leg.get("label", "").endswith(_TEAM_SUFFIX):
            leg["team"] = leg["label"][: -len(_TEAM_SUFFIX)]
        if not leg.get("tie_stage"):
            m = _STAGE_RE.search(leg.get("model_source") or "")
            if m:
                leg["tie_stage"] = m.group(1)


def _write_atomic(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".exposure_reconcile_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def reconcile(bet_recs: Dict[str, Any], event_recs: Dict[str, Any]) -> int:
    """Mutate ``bet_recs``/``event_recs`` in place. Returns count deduped."""
    em_legs = [r for r in (event_recs.get("recs") or []) if r.get("family") == "advance"]
    af_legs = list(bet_recs.get("advancement_futures") or [])
    _backfill_legacy_fields(em_legs)

    dupes = find_cross_feed_duplicates(em_legs, af_legs)
    for dupe in dupes:
        loser = resolve_duplicate(dupe)
        team, stage = dupe["key"]
        if loser == "event_market":
            leg = dupe["event_market"]
            leg["stake_usd"] = 0.0
            leg["dimmed"] = True
            leg["no_cash_reason"] = (
                "%s: same exposure as advancement_futures '%s' (%s reach %s) "
                "— kept there instead" % (REASON_DUP_TIE_EXPOSURE,
                                          dupe["advancement_futures"].get("id"),
                                          team, stage))
        else:
            leg = dupe["advancement_futures"]
            leg["stake"] = 0.0
            leg["withheld_reason"] = (
                "same exposure as event_market_recs '%s' (%s reach %s) — kept "
                "there instead" % (dupe["event_market"].get("label"), team, stage))
            leg["reason_code"] = REASON_DUP_TIE_EXPOSURE
            af_legs.remove(leg)
            bet_recs.setdefault("withheld", []).append(leg)
    if any(loser == "advancement_futures" for loser in
           (resolve_duplicate(d) for d in dupes)):
        bet_recs["advancement_futures"] = af_legs
    return len(dupes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bet-recs", default=os.path.join(_ROOT, "site", "bet_recs.json"))
    ap.add_argument("--event-recs", default=os.path.join(_ROOT, "site", "event_market_recs.json"))
    args = ap.parse_args()

    bet_recs = _load(args.bet_recs)
    event_recs = _load(args.event_recs)
    if not bet_recs or not event_recs:
        print("exposure-reconcile: one or both feeds missing/empty — nothing to do")
        return 0

    n = reconcile(bet_recs, event_recs)
    if n == 0:
        print("exposure-reconcile: no cross-feed duplicates found")
        return 0

    _write_atomic(args.bet_recs, bet_recs)
    _write_atomic(args.event_recs, event_recs)
    print("exposure-reconcile: deduped %d leg(s) — see reason_code=%s / "
          "no_cash_reason in the affected feeds" % (n, REASON_DUP_TIE_EXPOSURE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
