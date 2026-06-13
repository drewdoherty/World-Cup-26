"""Persist the blended model 1X2 per fixture at card-build time.

The card markdown only records model probabilities for *picked* selections,
and the scores feed reconstructs an approximate 1X2 from the top-k scoreline
ladder (which clips outcomes that never crack the top six).  This module dumps
the exact blended triple — plus the Elo / DC / market components — so the site
and the prediction-tracking pipeline can read what the model actually said
before kickoff.

Two artefacts, both git-tracked so history is preserved by commits:

* ``data/model_predictions.json`` — latest snapshot, overwritten each build.
* ``data/model_predictions_log.jsonl`` — append-only log, one line per
  fixture per build.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

LATEST_PATH = "data/model_predictions.json"
LOG_PATH = "data/model_predictions_log.jsonl"

_LEGS = ("home", "draw", "away")


def _triple(probs: Dict[str, float]) -> Dict[str, float]:
    return {leg: round(float(probs[leg]), 6) for leg in _LEGS}


def build_predictions(blends: List[Any], now_utc: str) -> Dict[str, Any]:
    """JSON-ready payload from ``card._iter_fixture_blends`` output.

    ``now_utc`` is supplied by the caller (no clock reads here) and stamps both
    the meta block and each fixture row so log lines are self-contained.
    """
    fixtures: List[Dict[str, Any]] = []
    for fb in blends:
        fixtures.append(
            {
                "generated": now_utc,
                "fixture": "%s vs %s" % (fb.home, fb.away),
                "match_id": str(fb.fx.get("event_id", "")),
                "kickoff": str(fb.fx.get("commence_time", "")),
                "model": _triple(fb.blended),
                "elo": _triple(fb.elo_map),
                "dc": _triple(fb.dc_map),
                "market": _triple(fb.mkt_map),
            }
        )
    fixtures.sort(key=lambda f: (f["kickoff"], f["fixture"]))
    return {"meta": {"generated": now_utc}, "fixtures": fixtures}


def write_predictions(
    payload: Dict[str, Any],
    latest_path: str = LATEST_PATH,
    log_path: str = LOG_PATH,
) -> None:
    """Overwrite the latest snapshot and append every fixture to the log.

    An empty fixture list never clobbers a populated latest file (mirrors the
    linemove transient-failure guard).
    """
    latest = Path(latest_path)
    log = Path(log_path)
    latest.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)

    if payload["fixtures"] or not _has_fixtures(latest):
        latest.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if payload["fixtures"]:
        with log.open("a", encoding="utf-8") as fh:
            for row in payload["fixtures"]:
                fh.write(json.dumps(row, sort_keys=True) + "\n")


def _has_fixtures(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("fixtures"))


def load_latest(path: str = LATEST_PATH) -> Dict[str, Dict[str, float]]:
    """Map fixture string -> exact blended 1X2 triple from the latest snapshot.

    Returns an empty dict when the file is missing or malformed so callers can
    fall back to their existing approximations.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for fx in data.get("fixtures", []):
        model = fx.get("model")
        name = fx.get("fixture")
        if not name or not isinstance(model, dict):
            continue
        if all(isinstance(model.get(leg), (int, float)) for leg in _LEGS):
            out[str(name)] = {leg: float(model[leg]) for leg in _LEGS}
    return out
