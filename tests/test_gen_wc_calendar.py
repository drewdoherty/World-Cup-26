"""gen_wc_calendar: shootout-aware knockout resolution.

Regression guard for 2026-07-04: a drawn knockout match (90-min score tied)
was left as an unresolved "Winner M##" placeholder forever, even after the
real penalty-shootout winner was known — because load_results() explicitly
skipped draws and never consulted a shootout source. Three of the 16 R32
ties this tournament were drawn (Germany-Paraguay, Netherlands-Morocco,
Australia-Egypt); all three, plus their downstream R16 fixtures, must now
resolve to the real pens winner.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gen_wc_calendar as cal  # noqa: E402


def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def test_draw_stays_unresolved_without_shootouts(tmp_path):
    results = tmp_path / "results.csv"
    _write_csv(results, [
        {"date": "2026-06-29", "home_team": "Germany", "away_team": "Paraguay",
         "home_score": "1", "away_score": "1"},
    ], ["date", "home_team", "away_team", "home_score", "away_score"])
    out = cal.load_results(str(results))
    assert out == {}  # no shootout source -> genuinely unresolved, not guessed


def test_draw_resolves_via_shootouts(tmp_path):
    results = tmp_path / "results.csv"
    shootouts = tmp_path / "shootouts.csv"
    _write_csv(results, [
        {"date": "2026-06-29", "home_team": "Germany", "away_team": "Paraguay",
         "home_score": "1", "away_score": "1"},
    ], ["date", "home_team", "away_team", "home_score", "away_score"])
    _write_csv(shootouts, [
        {"date": "2026-06-29", "home_team": "Germany", "away_team": "Paraguay",
         "winner": "Paraguay", "first_shooter": "Germany"},
    ], ["date", "home_team", "away_team", "winner", "first_shooter"])
    out = cal.load_results(str(results), shootouts_path=str(shootouts))
    assert out[frozenset({"germany", "paraguay"})] == "Paraguay"


def test_decisive_90min_result_never_overridden_by_shootouts(tmp_path):
    """A shootouts.csv row for an already-decisive match must never win —
    90-min score takes precedence (setdefault, not overwrite)."""
    results = tmp_path / "results.csv"
    shootouts = tmp_path / "shootouts.csv"
    _write_csv(results, [
        {"date": "2026-06-30", "home_team": "France", "away_team": "Sweden",
         "home_score": "3", "away_score": "0"},
    ], ["date", "home_team", "away_team", "home_score", "away_score"])
    _write_csv(shootouts, [
        {"date": "2026-06-30", "home_team": "France", "away_team": "Sweden",
         "winner": "Sweden", "first_shooter": "France"},
    ], ["date", "home_team", "away_team", "winner", "first_shooter"])
    out = cal.load_results(str(results), shootouts_path=str(shootouts))
    assert out[frozenset({"france", "sweden"})] == "France"


def test_r89_resolves_both_sides_end_to_end(tmp_path):
    """France (M77 winner) + the M74 draw resolved via shootouts -> M89 =
    'Paraguay vs France' fully, not just one side."""
    results = tmp_path / "results.csv"
    shootouts = tmp_path / "shootouts.csv"
    _write_csv(results, [
        {"date": "2026-06-29", "home_team": "Germany", "away_team": "Paraguay",
         "home_score": "1", "away_score": "1"},
        {"date": "2026-06-30", "home_team": "France", "away_team": "Sweden",
         "home_score": "3", "away_score": "0"},
    ], ["date", "home_team", "away_team", "home_score", "away_score"])
    _write_csv(shootouts, [
        {"date": "2026-06-29", "home_team": "Germany", "away_team": "Paraguay",
         "winner": "Paraguay", "first_shooter": "Germany"},
    ], ["date", "home_team", "away_team", "winner", "first_shooter"])
    results_map = cal.load_results(str(results), shootouts_path=str(shootouts))
    a, b = cal.resolve_teams(89, results_map)
    assert {a, b} == {"Paraguay", "France"}


def test_missing_shootouts_path_is_graceful(tmp_path):
    results = tmp_path / "results.csv"
    _write_csv(results, [
        {"date": "2026-06-30", "home_team": "France", "away_team": "Sweden",
         "home_score": "3", "away_score": "0"},
    ], ["date", "home_team", "away_team", "home_score", "away_score"])
    out = cal.load_results(str(results), shootouts_path=str(tmp_path / "nope.csv"))
    assert out[frozenset({"france", "sweden"})] == "France"
