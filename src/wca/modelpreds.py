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


def _lambdas_for(dc_model: Any, fb: Any) -> Optional[Dict[str, float]]:
    """``{lambda_home, lambda_away}`` goal means for a blend, or ``None``.

    Uses the *same* fitted DC model and neutral-venue flag that produced the
    persisted DC 1X2, so the goal expectations are leakage-free (lagged fit, no
    future info) and consistent with the 1X2 already in the row. Any failure
    (unseen team, missing attribute) returns ``None`` so the entry is still
    written without lambdas and older readers never crash.
    """
    if dc_model is None:
        return None
    expected = getattr(dc_model, "expected_lambdas", None)
    if expected is None:
        return None
    try:
        lam_h, lam_a = expected(
            fb.home, fb.away, neutral=bool(getattr(fb, "neutral", True)), warn=False
        )
    except Exception:
        return None
    if lam_h is None or lam_a is None:
        return None
    return {
        "lambda_home": round(float(lam_h), 6),
        "lambda_away": round(float(lam_a), 6),
    }


def build_predictions(
    blends: List[Any], now_utc: str, dc_model: Any = None
) -> Dict[str, Any]:
    """JSON-ready payload from ``card._iter_fixture_blends`` output.

    ``now_utc`` is supplied by the caller (no clock reads here) and stamps both
    the meta block and each fixture row so log lines are self-contained.

    ``dc_model`` (the fitted :class:`~wca.models.dixon_coles.DixonColesModel`)
    is optional. When supplied, each fixture row additionally carries
    ``lambda_home`` / ``lambda_away`` — the per-fixture Poisson goal means from
    the *same* lagged fit that produced the DC 1X2, honouring the neutral-venue
    flag. These are the compact sufficient statistic the correlated-exposure
    model reconstructs the full scoreline matrix from (so the 49/121-cell matrix
    is never persisted). Older entries without lambdas stay valid.
    """
    fixtures: List[Dict[str, Any]] = []
    for fb in blends:
        row: Dict[str, Any] = {
            "generated": now_utc,
            "fixture": "%s vs %s" % (fb.home, fb.away),
            "match_id": str(fb.fx.get("event_id", "")),
            "kickoff": str(fb.fx.get("commence_time", "")),
            "model": _triple(fb.blended),
            "elo": _triple(fb.elo_map),
            "dc": _triple(fb.dc_map),
            "market": _triple(fb.mkt_map),
        }
        lambdas = _lambdas_for(dc_model, fb)
        if lambdas is not None:
            row.update(lambdas)
        fixtures.append(row)
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


def load_lambdas(path: str = LATEST_PATH) -> Dict[str, Dict[str, float]]:
    """Map fixture string -> ``{lambda_home, lambda_away}`` from the snapshot.

    Only fixtures whose row carries both finite lambdas are returned, so a caller
    can cleanly fall back to its legacy behaviour for older entries that predate
    lambda persistence. Missing / malformed file -> empty dict.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for fx in data.get("fixtures", []):
        name = fx.get("fixture")
        lam_h = fx.get("lambda_home")
        lam_a = fx.get("lambda_away")
        if not name:
            continue
        if isinstance(lam_h, (int, float)) and isinstance(lam_a, (int, float)):
            out[str(name)] = {
                "lambda_home": float(lam_h),
                "lambda_away": float(lam_a),
            }
    return out
