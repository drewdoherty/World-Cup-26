"""Tests for the processed-results builder (scripts/wca_build_wc2026_results.py).

Guards F3: the committed ``data/processed/wc2026_results.json`` must be a derived
artefact that covers every PLAYED WC2026 match in the cleaned martj42 dataset
and matches the exact schema its consumers (settle / backfill / win-rate /
rigor) read — never a stale hand-frozen subset.

These tests resolve the dataset by REPO-RELATIVE path, so pytest must be run
from the repository root.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_build_wc2026_results as build  # noqa: E402
from wca.data.teamnames import canonical  # noqa: E402
from wca.predledger.settle import _load_results  # noqa: E402

_SRC_CSV = _REPO / "data" / "raw" / "martj42_cleaned.csv"
_OUT_JSON = _REPO / "data" / "processed" / "wc2026_results.json"


def _played_from_csv():
    """The authoritative set of played WC2026 ``(home, away, score, outcome)``."""
    out = {}
    with open(_SRC_CSV, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            date = (row.get("date") or "").strip()
            if date < build.WC2026_START_DATE:
                continue
            if build.TOURNAMENT_SUBSTR not in (row.get("tournament") or ""):
                continue
            if not (build._is_int(row.get("home_score")) and build._is_int(row.get("away_score"))):
                continue
            home = canonical((row.get("home_team") or "").strip())
            away = canonical((row.get("away_team") or "").strip())
            hs, as_ = int(row["home_score"]), int(row["away_score"])
            out[(home, away)] = {
                "score": f"{hs}-{as_}",
                "outcome": build._outcome_from_scores(hs, as_),
            }
    return out


# ---------------------------------------------------------------------------
# Pure builder logic
# ---------------------------------------------------------------------------

def test_outcome_from_scores():
    assert build._outcome_from_scores(2, 0) == "home"
    assert build._outcome_from_scores(0, 1) == "away"
    assert build._outcome_from_scores(1, 1) == "draw"


def test_builder_covers_all_played_matches():
    """The builder emits exactly the played WC2026 matches from the CSV."""
    played = _played_from_csv()
    assert len(played) >= 70, "expected the full played slate (>=70), not a subset"

    payload = build.build_results(str(_SRC_CSV), prev_path=None, log_path=None)
    rows = payload["results"]
    assert len(rows) == len(played)

    by_key = {}
    for r in rows:
        home, away = r["fixture"].split(" vs ", 1)
        by_key[(home, away)] = r
    assert set(by_key) == set(played)
    for key, exp in played.items():
        assert by_key[key]["score"] == exp["score"]
        assert by_key[key]["outcome"] == exp["outcome"]


def test_builder_emits_consumer_schema():
    """Every row carries date/fixture/score/outcome; kickoff_utc optional."""
    payload = build.build_results(str(_SRC_CSV), prev_path=None, log_path=None)
    assert "_comment" in payload and isinstance(payload["_comment"], str)
    for r in payload["results"]:
        assert set(r) <= {"date", "fixture", "score", "outcome", "kickoff_utc"}
        for required in ("date", "fixture", "score", "outcome"):
            assert r[required], f"missing/empty {required} in {r}"
        assert r["outcome"] in ("home", "draw", "away")
        assert " vs " in r["fixture"]
        # score parses as the consumers expect ("H-A")
        h, a = r["score"].split("-")
        assert h.isdigit() and a.isdigit()


def test_kickoff_carried_only_from_real_source():
    """kickoff_utc is carried from the prior file, never invented."""
    prev = {
        "results": [
            {
                "date": "2026-06-11",
                "fixture": "Mexico vs South Africa",
                "kickoff_utc": "2026-06-11T19:00:00Z",
                "score": "2-0",
                "outcome": "home",
            }
        ]
    }
    tmp_prev = _REPO / "tests" / "_tmp_prev_results.json"
    tmp_prev.write_text(json.dumps(prev), encoding="utf-8")
    try:
        payload = build.build_results(str(_SRC_CSV), prev_path=str(tmp_prev), log_path=None)
    finally:
        tmp_prev.unlink(missing_ok=True)
    row = next(r for r in payload["results"] if r["fixture"] == "Mexico vs South Africa")
    assert row["kickoff_utc"] == "2026-06-11T19:00:00Z"
    # A match absent from the prior file (and given no log) gets no kickoff_utc.
    no_prev = build.build_results(str(_SRC_CSV), prev_path=None, log_path=None)
    none_ko = [r for r in no_prev["results"] if "kickoff_utc" not in r]
    assert none_ko, "expected at least one match without a real kickoff source"


# ---------------------------------------------------------------------------
# The committed artefact must be in sync (F3 anti-staleness guard)
# ---------------------------------------------------------------------------

def test_committed_file_matches_consumer_schema_and_indexes():
    """The shipped processed file loads cleanly through the settle consumer."""
    indexed = _load_results(str(_OUT_JSON))
    played = _played_from_csv()
    assert set(indexed) == set(played)
    for key, exp in played.items():
        assert indexed[key]["outcome"] == exp["outcome"]


def test_committed_file_is_not_stale():
    """The shipped file covers the full played slate (regression on the 31-match freeze)."""
    payload = json.loads(_OUT_JSON.read_text(encoding="utf-8"))
    rows = payload.get("results", [])
    played = _played_from_csv()
    assert len(rows) == len(played)
    assert len(rows) >= 70, "shipped processed results regressed to a stale subset"
