"""Settle open predictions against real results.

Match markets (1X2 / scoreline / O-U / BTTS) settle from
``wc2026_results.json`` — each result carries a final ``score`` and 1X2
``outcome``, from which every market's won/lost/push is *derived* (the results
file holds no per-market settlement, so we compute it).  Advancement markets
settle from ``advancement_played_results.json`` (currently only group-stage
results, so reach-stage / winner predictions stay open until the bracket plays
out).

Correctness rules
-----------------
* **1X2** — won iff the picked leg equals the result outcome.
* **scoreline** — won iff the picked exact score equals the final score.
* **O-U** — Over <line> won iff total goals > line; Under won iff < line; a
  total exactly equal to an integer line is a **push** (excluded from rates).
  Standard half-line totals (2.5) never push.
* **BTTS** — Yes won iff both teams scored; No won iff at least one did not.
* **advancement** — settled only where a definitive result exists; group-stage
  results alone cannot confirm a reach-R16/winner prediction, so those stay
  open (no fabricated settlement).

Idempotent: only ``status='open'`` rows are touched, and ``settle_source`` is
stamped so a re-run is a no-op.  Pushes/voids set ``status`` accordingly and
are excluded from both numerator and denominator of any downstream rate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wca import tracking
from wca.data.teamnames import canonical
from wca.predledger import store

_LEG_FROM_LABEL = {"home": "home", "draw": "draw", "away": "away"}


def _fixture_key(fixture: str) -> Optional[Tuple[str, str]]:
    pair = tracking.split_fixture(fixture or "")
    if pair is None:
        return None
    return (canonical(pair[0]), canonical(pair[1]))


def _load_results(results_path: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Index ``wc2026_results.json`` by canonical fixture key."""
    try:
        data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in data.get("results", []):
        key = _fixture_key(r.get("fixture", ""))
        if key is None:
            continue
        parsed = tracking.parse_score(r.get("score"))
        if parsed is None:
            continue
        out[key] = {
            "outcome": str(r.get("outcome", "")).strip().lower(),
            "home_goals": parsed[0],
            "away_goals": parsed[1],
        }
    return out


def _load_adv_results(adv_path: str) -> Dict[Tuple[str, str], Dict[str, int]]:
    """Index ``advancement_played_results.json`` by canonical fixture key."""
    try:
        data = json.loads(Path(adv_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[Tuple[str, str], Dict[str, int]] = {}
    for r in data:
        home = r.get("home")
        away = r.get("away")
        if not home or not away:
            continue
        key = (canonical(home), canonical(away))
        out[key] = {"hg": int(r.get("hg", 0)), "ag": int(r.get("ag", 0))}
    return out


def _settle_1x2(selection: str, res: Dict[str, Any]) -> Optional[str]:
    leg = (selection or "").strip().lower()
    if leg not in _LEG_FROM_LABEL:
        return None
    return "won" if leg == res["outcome"] else "lost"


def _settle_scoreline(selection: str, res: Dict[str, Any]) -> Optional[str]:
    parsed = tracking.parse_score(selection)
    if parsed is None:
        return None
    return "won" if parsed == (res["home_goals"], res["away_goals"]) else "lost"


def _settle_ou(selection: str, line: Optional[float], res: Dict[str, Any]) -> Optional[str]:
    """Over/Under <line>.  Integer-line exact total -> push."""
    total = res["home_goals"] + res["away_goals"]
    sel = (selection or "").strip().lower()
    if line is None or line < 0:
        # line embedded in the selection, e.g. "Over 2.5"
        parts = sel.split()
        if len(parts) >= 2:
            try:
                line = float(parts[-1])
            except ValueError:
                return None
        else:
            return None
    if total == line:
        return "push"
    is_over = sel.startswith("over")
    is_under = sel.startswith("under")
    if not (is_over or is_under):
        return None
    if is_over:
        return "won" if total > line else "lost"
    return "won" if total < line else "lost"


def _settle_btts(selection: str, res: Dict[str, Any]) -> Optional[str]:
    both = res["home_goals"] > 0 and res["away_goals"] > 0
    sel = (selection or "").strip().lower()
    if sel in ("yes", "btts yes", "both teams to score"):
        return "won" if both else "lost"
    if sel in ("no", "btts no"):
        return "won" if not both else "lost"
    return None


def settle_open(
    results_path: str,
    adv_results_path: str,
    db: str = store._DEFAULT_DB,
    source: str = "results",
) -> Dict[str, int]:
    """Settle every open prediction it can; return a per-status tally.

    Returns ``{"won":n,"lost":n,"push":n,"void":n,"unsettled":n}`` counting the
    rows touched this run (``unsettled`` = open rows left open for lack of a
    result).  Idempotent — only ``status='open'`` rows are considered.
    """
    results = _load_results(results_path)
    adv = _load_adv_results(adv_results_path)
    tally = {"won": 0, "lost": 0, "push": 0, "void": 0, "unsettled": 0}

    rows = store.open_predictions(db)
    settled: List[Tuple[str, str]] = []  # (pred_id, status)
    for row in rows:
        market = (row["market"] or "").strip().lower()
        selection = row["selection"]
        line = row["line"]
        outcome: Optional[str] = None

        if market == "advancement":
            # Only settle where a definitive bracket result exists; group-stage
            # results cannot confirm a reach-stage / winner prediction.
            outcome = None  # advancement settlement is not derivable yet
        else:
            key = _fixture_key(row["fixture"])
            res = results.get(key) if key else None
            if res is None:
                tally["unsettled"] += 1
                continue
            if market == "1x2":
                outcome = _settle_1x2(selection, res)
            elif market == "scoreline":
                outcome = _settle_scoreline(selection, res)
            elif market == "ou":
                outcome = _settle_ou(selection, line, res)
            elif market == "btts":
                outcome = _settle_btts(selection, res)

        if outcome is None:
            tally["unsettled"] += 1
            continue
        settled.append((row["prediction_id"], outcome))
        tally[outcome] = tally.get(outcome, 0) + 1

    for pred_id, status in settled:
        store.settle_prediction(pred_id, status, source, db)
    return tally
