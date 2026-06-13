"""Tests for wca.modelpreds — persisted blended 1X2 snapshots.

These exercise:

* :func:`wca.modelpreds.build_predictions` against synthetic fixture blends
  (shape-compatible with ``wca.card._FixtureBlend``);
* the latest-snapshot + append-only-log write behaviour, including the guard
  that an empty build never clobbers a populated latest file;
* :func:`wca.modelpreds.load_latest` round-trip and malformed-file fallback;
* the scores-page preference for exact triples over the top-k scoreline
  reconstruction (``approx_1x2`` flag flips off).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict

from wca import modelpreds, scorespage

NOW = "2026-06-13T00:00:00"


@dataclass
class _Blend:
    home: str
    away: str
    blended: Dict[str, float]
    elo_map: Dict[str, float]
    dc_map: Dict[str, float]
    mkt_map: Dict[str, float]
    fx: Dict[str, object] = field(default_factory=dict)


def _blend(home="Qatar", away="Switzerland", h=0.077, d=0.162, a=0.761):
    triple = {"home": h, "draw": d, "away": a}
    return _Blend(
        home=home,
        away=away,
        blended=triple,
        elo_map=triple,
        dc_map=triple,
        mkt_map=triple,
        fx={"event_id": "ev1", "commence_time": "2026-06-13T16:00:00Z"},
    )


def test_build_predictions_payload():
    payload = modelpreds.build_predictions([_blend()], NOW)
    assert payload["meta"] == {"generated": NOW}
    (fx,) = payload["fixtures"]
    assert fx["fixture"] == "Qatar vs Switzerland"
    assert fx["kickoff"] == "2026-06-13T16:00:00Z"
    assert abs(fx["model"]["home"] - 0.077) < 1e-9
    assert fx["generated"] == NOW


def test_write_and_load_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        payload = modelpreds.build_predictions([_blend()], NOW)
        modelpreds.write_predictions(payload, latest_path=latest, log_path=log)
        modelpreds.write_predictions(payload, latest_path=latest, log_path=log)

        triples = modelpreds.load_latest(latest)
        assert "Qatar vs Switzerland" in triples
        assert abs(triples["Qatar vs Switzerland"]["away"] - 0.761) < 1e-9

        # Two builds -> two log lines (append-only).
        with open(log, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh.read().splitlines()]
        assert len(lines) == 2
        assert lines[0]["fixture"] == "Qatar vs Switzerland"


def test_empty_build_never_clobbers_latest():
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        populated = modelpreds.build_predictions([_blend()], NOW)
        modelpreds.write_predictions(populated, latest_path=latest, log_path=log)

        empty = modelpreds.build_predictions([], "2026-06-14T00:00:00")
        modelpreds.write_predictions(empty, latest_path=latest, log_path=log)

        assert modelpreds.load_latest(latest)  # still populated
        assert not os.path.exists(log) or len(open(log).read().splitlines()) == 1


def test_load_latest_missing_or_malformed():
    assert modelpreds.load_latest("/nonexistent/path.json") == {}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write("not json")
    try:
        assert modelpreds.load_latest(fh.name) == {}
    finally:
        os.unlink(fh.name)


CARD = """<!-- generated: 2026-06-13T00:00:00 -->
*World Cup Alpha — scorelines* (1 fixtures)

*Qatar vs Switzerland*
    0-1  14.0%  fair 7.14  back >= 7.28
    0-2  12.0%  fair 8.33  back >= 8.50
    1-1  10.0%  fair 10.00  back >= 10.20
    O/U 2.5: over 45.0% / under 55.0%   BTTS 40.0%
"""


def test_scores_page_prefers_exact_triple():
    with tempfile.TemporaryDirectory() as tmp:
        card_path = os.path.join(tmp, "card.md")
        with open(card_path, "w", encoding="utf-8") as fh:
            fh.write(CARD)
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        modelpreds.write_predictions(
            modelpreds.build_predictions([_blend()], NOW),
            latest_path=latest,
            log_path=log,
        )

        data = scorespage.build_scores_data(
            card_path, now_utc=NOW, model_preds_path=latest
        )
        (fx,) = data["fixtures"]
        assert fx["approx_1x2"] is False
        assert abs(fx["model_1x2"]["home"] - 0.077) < 1e-9

        # Without the snapshot the reconstruction kicks in (home never in
        # top-k => 0.0) and the approx flag stays on.
        data = scorespage.build_scores_data(
            card_path, now_utc=NOW, model_preds_path="/nonexistent.json"
        )
        (fx,) = data["fixtures"]
        assert fx["approx_1x2"] is True
        assert fx["model_1x2"]["home"] == 0.0
