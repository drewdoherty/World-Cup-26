"""Tests for scripts/wca_prop_calibration.py (P4: prop_calibration.json generator).

Covers: output schema well-formedness (the exact shape
scripts/wca_betrecs.py::build_event_props reads — fixture/corners.mean/
corners.o{line}_fair_over/cards.mean), meta freshness stamp, graceful
handling of missing lambdas/malformed fixtures, and that build_event_props
actually consumes a real generated file end-to-end.

All tests run offline — no network, no live data, no ledger writes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_betrecs as br  # noqa: E402
import wca_prop_calibration as pc  # noqa: E402


def _predictions(n: int = 2) -> List[Dict[str, Any]]:
    base = [
        {
            "fixture": "Brazil vs Norway",
            "kickoff": "2099-12-31 20:00:00+00:00",
            "lambda_home": 2.25,
            "lambda_away": 0.84,
        },
        {
            "fixture": "Mexico vs England",
            "kickoff": "2099-12-31 23:00:00+00:00",
            "lambda_home": 1.10,
            "lambda_away": 1.68,
        },
    ]
    return base[:n]


# ---------------------------------------------------------------------------
# Schema well-formedness
# ---------------------------------------------------------------------------

class TestBuildCalibrationSchema:
    def test_top_level_shape(self):
        data = pc.build_calibration(_predictions(), priors=None)
        assert set(data.keys()) == {"meta", "fixtures"}
        assert isinstance(data["fixtures"], list)
        assert isinstance(data["meta"], dict)

    def test_meta_freshness_stamp(self):
        data = pc.build_calibration(_predictions(), priors=None)
        meta = data["meta"]
        assert "generated" in meta and meta["generated"]
        assert meta["n_fixtures"] == len(data["fixtures"])
        assert meta["n_team_priors"] == 0
        assert "cash_status" in meta and "FREE-BET-ONLY" in meta["cash_status"]

    def test_fixture_shape_matches_betrecs_expectations(self):
        """Schema that scripts/wca_betrecs.py::build_event_props reads.

        build_event_props does: fix_cal.get("fixture"), fix_cal.get("corners"),
        fix_cal.get("cards") (each a dict, possibly empty) — see wca_betrecs.py.
        """
        data = pc.build_calibration(_predictions(), priors=None)
        assert len(data["fixtures"]) == 2
        for fx in data["fixtures"]:
            assert set(fx.keys()) >= {
                "fixture", "kickoff", "lambda_home", "lambda_away",
                "corners", "cards",
            }
            assert isinstance(fx["fixture"], str) and " vs " in fx["fixture"]
            corners = fx["corners"]
            cards = fx["cards"]
            assert "mean" in corners and corners["mean"] > 0
            assert "mean" in cards and cards["mean"] > 0
            # Fair O/U keys use the on-disk convention "o<line>_fair_over".
            for line in pc.CORNERS_LINES:
                assert ("o%s_fair_over" % line) in corners
            for line in pc.CARDS_LINES:
                assert ("o%s_fair_over" % line) in cards

    def test_corners_mean_increases_with_combined_xg(self):
        """Sanity: higher combined lambda -> higher corners mean (damped elasticity)."""
        low = pc.build_calibration(
            [{"fixture": "A vs B", "kickoff": None, "lambda_home": 0.5, "lambda_away": 0.5}],
            priors=None,
        )["fixtures"][0]
        high = pc.build_calibration(
            [{"fixture": "A vs B", "kickoff": None, "lambda_home": 2.5, "lambda_away": 2.5}],
            priors=None,
        )["fixtures"][0]
        assert high["corners"]["mean"] > low["corners"]["mean"]

    def test_skips_fixtures_missing_lambdas(self):
        """A row with no lambda_home/lambda_away is skipped, not fabricated."""
        rows = _predictions(1) + [{"fixture": "No Lambda vs Team", "kickoff": None}]
        data = pc.build_calibration(rows, priors=None)
        fixtures = [f["fixture"] for f in data["fixtures"]]
        assert "No Lambda vs Team" not in fixtures
        assert len(data["fixtures"]) == 1

    def test_max_fixtures_cap(self):
        data = pc.build_calibration(_predictions(2), priors=None, max_fixtures=1)
        assert len(data["fixtures"]) == 1

    def test_empty_predictions_yields_empty_fixtures(self):
        data = pc.build_calibration([], priors=None)
        assert data["fixtures"] == []
        assert data["meta"]["n_fixtures"] == 0

    def test_deterministic_given_same_inputs(self):
        d1 = pc.build_calibration(_predictions(), priors=None)
        d2 = pc.build_calibration(_predictions(), priors=None)
        assert d1["fixtures"] == d2["fixtures"]


# ---------------------------------------------------------------------------
# CLI end-to-end (writes a real file)
# ---------------------------------------------------------------------------

class TestMainCLI:
    def test_main_writes_valid_json(self, tmp_path):
        preds_path = tmp_path / "model_predictions.json"
        preds_path.write_text(json.dumps({"fixtures": _predictions()}), encoding="utf-8")
        out_path = tmp_path / "prop_calibration.json"
        missing_priors = tmp_path / "no_such_priors.csv"

        rc = pc.main([
            "--predictions", str(preds_path),
            "--priors", str(missing_priors),
            "--out", str(out_path),
        ])
        assert rc == 0
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(data["fixtures"]) == 2

    def test_main_tolerates_missing_predictions_file(self, tmp_path):
        out_path = tmp_path / "prop_calibration.json"
        rc = pc.main([
            "--predictions", str(tmp_path / "does_not_exist.json"),
            "--priors", str(tmp_path / "does_not_exist.csv"),
            "--out", str(out_path),
        ])
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["fixtures"] == []


# ---------------------------------------------------------------------------
# Integration: build_event_props actually picks up a real generated file
# ---------------------------------------------------------------------------

class TestBetrecsIntegration:
    def test_event_props_withheld_rows_populated_from_real_calibration(self, tmp_path):
        """A real generator-produced prop_calibration.json feeds withheld rows.

        build_event_props still correctly withholds corners/cards from cash
        (no live sportsbook price snapshot — see CLAUDE.md), but with this
        generator wired the withheld rows now trace back to a real computed
        calibration file instead of the field being permanently empty because
        prop_calibration.json never existed.
        """
        data = pc.build_calibration(_predictions(2), priors=None)
        cal_path = tmp_path / "prop_calibration.json"
        cal_path.write_text(json.dumps(data), encoding="utf-8")

        loaded = json.loads(cal_path.read_text(encoding="utf-8"))
        recs, withheld = br.build_event_props(
            loaded, [], {"bankroll": 2000.0, "kelly_fraction": 0.25, "max_stake": 100.0},
            price_age_secs=None, model_age_secs=60,
        )
        # No live book price -> still nothing actionable (correct governance).
        assert recs == []
        # But withheld now carries one corners + one cards row PER fixture,
        # keyed off the real fixtures in the generated file, plus the
        # standing scorer-props withheld row.
        fixtures_seen = {w["fixture"] for w in withheld if w.get("market") in ("corners", "cards")}
        assert fixtures_seen == {"Brazil vs Norway", "Mexico vs England"}
        corners_rows = [w for w in withheld if w.get("market") == "corners"]
        cards_rows = [w for w in withheld if w.get("market") == "cards"]
        assert len(corners_rows) == 2
        assert len(cards_rows) == 2
        for w in corners_rows + cards_rows:
            assert w["withheld_reason"]
            assert w["stale"] is True

    def test_event_props_empty_fixtures_yields_only_scorer_withheld(self):
        recs, withheld = br.build_event_props(
            {"fixtures": []}, [], {"bankroll": 2000.0, "kelly_fraction": 0.25, "max_stake": 100.0},
            price_age_secs=None, model_age_secs=60,
        )
        assert recs == []
        assert len(withheld) == 1
        assert withheld[0]["market"] == "anytime_scorer"
