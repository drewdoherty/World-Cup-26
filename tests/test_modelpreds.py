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


# ---------------------------------------------------------------------------
# F7 goal-blend SHADOW lambdas (2026-07-02): additive, never schema-breaking.
# ---------------------------------------------------------------------------


class _FakeGB:
    """Stub with the drop-in DC interface _lambdas_for relies on."""

    def expected_lambdas(self, home, away, neutral=True, warn=False):
        return 1.7, 1.2


def test_gb_shadow_lambdas_persisted_additively():
    payload = modelpreds.build_predictions([_blend()], NOW, gb_model=_FakeGB())
    (fx,) = payload["fixtures"]
    assert fx["gb_lambda_home"] == 1.7
    assert fx["gb_lambda_away"] == 1.2


def test_no_gb_model_keeps_schema_unchanged():
    payload = modelpreds.build_predictions([_blend()], NOW)
    assert "gb_lambda_home" not in payload["fixtures"][0]


# ---------------------------------------------------------------------------
# 1X2 SHADOW variants (2026-07-08): mw90 / shrink / disagree3pp — additive,
# recomputed from the elo/dc/market/model triples already in the row.
# ---------------------------------------------------------------------------


def _distinct_blend(home="Brazil", away="Serbia"):
    """A blend with genuinely different elo / dc / market / model triples."""
    return _Blend(
        home=home,
        away=away,
        blended={"home": 0.70, "draw": 0.20, "away": 0.10},   # model
        elo_map={"home": 0.40, "draw": 0.30, "away": 0.30},
        dc_map={"home": 0.50, "draw": 0.25, "away": 0.25},
        mkt_map={"home": 0.60, "draw": 0.25, "away": 0.15},
        fx={"event_id": "ev2", "commence_time": "2026-06-14T18:00:00Z"},
    )


def _approx_triple(got, want, tol=1e-5):
    # Persisted shadow triples are rounded to 6 dp, so the renorm sum is 1.0 to
    # ~1e-6, not exactly 1.0; assert to that persisted precision.
    assert abs(sum(got.values()) - 1.0) < 1e-5, "shadow triple must renormalise"
    for leg in ("home", "draw", "away"):
        assert abs(got[leg] - want[leg]) < tol, (leg, got[leg], want[leg])


def test_mw90_shadow_matches_hand_computation():
    payload = modelpreds.build_predictions([_distinct_blend()], NOW)
    (fx,) = payload["fixtures"]
    # residual = 0.25*elo + 0.75*dc ; blend = 0.9*mkt + 0.1*residual, renorm.
    # home: res=0.475 blend=0.9*0.60+0.1*0.475=0.5875
    # draw: res=0.2625 blend=0.9*0.25+0.1*0.2625=0.25125
    # away: res=0.2625 blend=0.9*0.15+0.1*0.2625=0.16125  (sum already 1.0)
    _approx_triple(fx["mw90"], {"home": 0.5875, "draw": 0.25125, "away": 0.16125})


def test_shrink_shadow_matches_hand_computation():
    payload = modelpreds.build_predictions([_distinct_blend()], NOW)
    (fx,) = payload["fixtures"]
    # model home=0.70 (>=0.25 -> k=0.5): 0.60+0.5*(0.70-0.60)=0.65
    # model draw=0.20 (<0.25  -> k=0.25): 0.25+0.25*(0.20-0.25)=0.2375
    # model away=0.10 (<0.25  -> k=0.25): 0.15+0.25*(0.10-0.15)=0.1375
    raw = {"home": 0.65, "draw": 0.2375, "away": 0.1375}
    tot = sum(raw.values())
    _approx_triple(fx["shrink"], {k: v / tot for k, v in raw.items()})


def test_disagree3pp_flags_per_leg():
    payload = modelpreds.build_predictions([_distinct_blend()], NOW)
    (fx,) = payload["fixtures"]
    # |model-mkt|: home |0.70-0.60|=0.10>=.03 T; draw |0.20-0.25|=0.05 T;
    # away |0.10-0.15|=0.05 T.
    assert fx["disagree3pp"] == {"home": True, "draw": True, "away": True}


def test_disagree3pp_false_when_close():
    b = _distinct_blend()
    b.blended = {"home": 0.61, "draw": 0.26, "away": 0.13}  # within 3pp of mkt
    payload = modelpreds.build_predictions([b], NOW)
    (fx,) = payload["fixtures"]
    # home |0.61-0.60|=0.01 F; draw |0.26-0.25|=0.01 F; away |0.13-0.15|=0.02 F.
    assert fx["disagree3pp"] == {"home": False, "draw": False, "away": False}


def test_shadows_omitted_when_no_market():
    # The hard guard lives in _onex2_shadows: no usable market triple -> no
    # shadow fields at all (never fabricated). build_predictions always receives
    # a full market triple in production, so exercise the guard directly.
    model = {"home": 0.7, "draw": 0.2, "away": 0.1}
    elo = {"home": 0.4, "draw": 0.3, "away": 0.3}
    dc = {"home": 0.5, "draw": 0.25, "away": 0.25}
    assert modelpreds._onex2_shadows(model, elo, dc, {}) == {}
    assert modelpreds._onex2_shadows(model, elo, dc, {"home": 0.6}) == {}
    # A full market triple -> all three shadow families present.
    out = modelpreds._onex2_shadows(model, elo, dc, {"home": 0.6, "draw": 0.25, "away": 0.15})
    assert set(out) == {"mw90", "shrink", "disagree3pp"}


def test_shadows_round_trip_through_log():
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        log = os.path.join(tmp, "log.jsonl")
        payload = modelpreds.build_predictions([_distinct_blend()], NOW)
        modelpreds.write_predictions(payload, latest_path=latest, log_path=log)
        with open(log, encoding="utf-8") as fh:
            (row,) = [json.loads(l) for l in fh.read().splitlines()]
        assert "mw90" in row and "shrink" in row and "disagree3pp" in row
        # Persisted (6-dp-rounded) triples renorm to 1.0 within rounding slack.
        assert abs(sum(row["mw90"].values()) - 1.0) < 1e-5
        assert abs(sum(row["shrink"].values()) - 1.0) < 1e-5


def test_renorm_rejects_degenerate_triple():
    assert modelpreds._renorm({"home": 0.0, "draw": 0.0, "away": 0.0}) is None
    assert modelpreds._renorm({"home": -1.0, "draw": 0.0, "away": 0.0}) is None
    out = modelpreds._renorm({"home": 1.0, "draw": 1.0, "away": 2.0})
    assert abs(out["away"] - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# model_raw persistence (2026-07-09 shrink promotion). A blend stub that carries
# `blended_raw` distinct from `blended` (the shrunk live line) persists both,
# and the shrink shadow is computed from the RAW model.
# ---------------------------------------------------------------------------


@dataclass
class _ShrunkBlend(_Blend):
    blended_raw: Dict[str, float] = None  # type: ignore[assignment]


def test_model_raw_falls_back_to_model_for_legacy_stub():
    # A blend WITHOUT blended_raw (legacy shape) -> model_raw mirrors model.
    payload = modelpreds.build_predictions([_distinct_blend()], NOW)
    (fx,) = payload["fixtures"]
    assert fx["model_raw"] == fx["model"]


def test_model_raw_persisted_when_distinct():
    b = _ShrunkBlend(
        home="Brazil", away="Serbia",
        blended={"home": 0.65, "draw": 0.22, "away": 0.13},        # shrunk live line
        elo_map={"home": 0.40, "draw": 0.30, "away": 0.30},
        dc_map={"home": 0.50, "draw": 0.25, "away": 0.25},
        mkt_map={"home": 0.60, "draw": 0.25, "away": 0.15},
        fx={"event_id": "ev2", "commence_time": "2026-06-14T18:00:00Z"},
        blended_raw={"home": 0.70, "draw": 0.20, "away": 0.10},    # raw blend
    )
    payload = modelpreds.build_predictions([b], NOW)
    (fx,) = payload["fixtures"]
    assert abs(fx["model"]["home"] - 0.65) < 1e-6
    assert abs(fx["model_raw"]["home"] - 0.70) < 1e-6
    # shrink shadow must be shrink(model_raw, market), NOT shrink(model, market).
    from_raw = modelpreds.shrink_triple(fx["model_raw"], fx["market"])
    for leg in ("home", "draw", "away"):
        assert abs(fx["shrink"][leg] - from_raw[leg]) < 1e-6
