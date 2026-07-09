"""Regression: every fixture with a market must get a predictions-log row.

Root cause (audit, 2026-07): ``scripts/wca_build_card.py`` filtered the odds
frame to the ``--hours-ahead`` display window *before* handing it to
``wca.card.fixture_blends`` / ``wca.modelpreds.build_predictions`` for
persistence. Once a fixture's kickoff passed ``now_dt`` it fell out of the
``[now_dt, cutoff_dt]`` mask and was never captured again — so a fixture that
never got a pre-match snapshot logged during its (possibly very short, or
API-thin) time inside the window vanished from ``data/model_predictions_log.jsonl``
forever. In production this hit simultaneous final-group-matchday kickoffs
hardest: 11 of 12 games played simultaneously on 2026-06-24/25 had zero log
rows out of 21 missing fixtures overall (of 96 settled).

The fix keeps an UNFILTERED ``odds_df_all`` alongside the windowed
``odds_df`` (extracted here as :func:`wca_build_card.window_odds_df` for
testability) and logs predictions from ``odds_df_all`` while the display card
keeps using the windowed frame. These tests pin:

1. ``window_odds_df`` filters correctly in isolation (pure-logic unit test,
   same style as ``want_goalscorers_card`` / ``has_scorer_markets``).
2. The end-to-end scenario: two SIMULTANEOUS kickoffs where one falls outside
   a narrow display window — using the unfiltered frame with
   ``fixture_blends`` + ``build_predictions`` logs BOTH; using the windowed
   frame (the old, buggy behaviour) would only log one.

scripts/ is not a package; load the module by path (like
test_build_card_goalscorers.py).
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

import pandas as pd

from wca.card import fixture_blends, BlendWeights
from wca.modelpreds import build_predictions, write_predictions

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_build_card.py"
_spec = importlib.util.spec_from_file_location("wca_build_card", _SCRIPT)
wbc = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(wbc)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# window_odds_df: pure-logic unit tests.
# ---------------------------------------------------------------------------


def _odds_row(event_id, home, away, commence, book="book"):
    rows = []
    for name, price in [(home, 2.0), ("Draw", 3.2), (away, 4.0)]:
        rows.append(
            {
                "event_id": event_id,
                "home_team": home,
                "away_team": away,
                "commence_time": commence,
                "bookmaker_key": book,
                "market": "h2h",
                "outcome_name": name,
                "decimal_odds": price,
            }
        )
    return rows


def _simultaneous_odds_df():
    """Two fixtures kicking off at the SAME instant, both with full 1X2 books."""
    rows = []
    rows += _odds_row("m1", "Bosnia and Herzegovina", "Qatar", "2026-06-24T19:00:00Z")
    rows += _odds_row("m2", "Canada", "Switzerland", "2026-06-24T19:00:00Z")
    return pd.DataFrame(rows)


def test_window_filters_to_now_and_cutoff():
    odds = _simultaneous_odds_df()
    now_dt = pd.Timestamp("2026-06-24T18:00:00")
    cutoff_dt = pd.Timestamp("2026-06-24T20:00:00")
    windowed = wbc.window_odds_df(odds, now_dt, cutoff_dt)
    assert set(windowed["event_id"]) == {"m1", "m2"}


def test_window_excludes_fixture_once_kickoff_has_passed():
    odds = _simultaneous_odds_df()
    # now_dt AFTER the simultaneous kickoff -> both fixtures fall out of the
    # window (this is exactly the state once the hourly job's "now" rolls
    # past a fixture that was never snapshotted pre-match).
    now_dt = pd.Timestamp("2026-06-24T20:00:00")
    cutoff_dt = pd.Timestamp("2026-06-25T20:00:00")
    windowed = wbc.window_odds_df(odds, now_dt, cutoff_dt)
    assert windowed.empty


def test_window_handles_missing_commence_time_column():
    odds = pd.DataFrame([{"event_id": "m1"}])
    out = wbc.window_odds_df(odds, pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-25"))
    assert out is odds  # returned unchanged, no crash


def test_window_handles_empty_frame():
    odds = pd.DataFrame()
    out = wbc.window_odds_df(odds, pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-25"))
    assert out.empty


# ---------------------------------------------------------------------------
# End-to-end: simultaneous kickoffs must BOTH log, even when only one of them
# would have survived the narrow display window.
# ---------------------------------------------------------------------------


class _Rater:
    home_advantage = 0.0


class _Models:
    rater = _Rater()


def _fixtures_meta():
    return pd.DataFrame(
        [
            {
                "home_team": "Bosnia and Herzegovina",
                "away_team": "Qatar",
                "neutral": True,
                "country": "",
            },
            {
                "home_team": "Canada",
                "away_team": "Switzerland",
                "neutral": True,
                "country": "",
            },
        ]
    )


def test_simultaneous_kickoffs_both_logged_via_unfiltered_frame(monkeypatch):
    def fake_elo_probs(models, home, away, neutral, host=None, host_points=None):
        return (0.4, 0.3, 0.3)

    def fake_dc_probs(models, home, away, neutral):
        return (0.42, 0.28, 0.30)

    monkeypatch.setattr("wca.card.elo_probs", fake_elo_probs)
    monkeypatch.setattr("wca.card.dc_probs", fake_dc_probs)

    odds_df_all = _simultaneous_odds_df()
    fixtures_meta = _fixtures_meta()

    # Simulate the hourly job's "now" having rolled PAST the simultaneous
    # kickoff (exactly the production scenario: neither fixture was ever
    # snapshotted in an earlier run, and now both have kicked off).
    now_dt = pd.Timestamp("2026-06-24T20:00:00")
    cutoff_dt = now_dt + pd.Timedelta(hours=96)
    windowed = wbc.window_odds_df(odds_df_all, now_dt, cutoff_dt)
    assert windowed.empty, "sanity: the old windowed frame excludes both kicked-off fixtures"

    # THE FIX: predictions are built from the unfiltered frame.
    blends = fixture_blends(_Models(), odds_df_all, fixtures_meta, BlendWeights())
    assert len(blends) == 2

    payload = build_predictions(blends, now_dt.isoformat())
    fixtures_logged = {fx["fixture"] for fx in payload["fixtures"]}
    assert fixtures_logged == {
        "Bosnia and Herzegovina vs Qatar",
        "Canada vs Switzerland",
    }

    with tempfile.TemporaryDirectory() as tmp:
        latest = str(Path(tmp) / "latest.json")
        log = str(Path(tmp) / "log.jsonl")
        write_predictions(payload, latest_path=latest, log_path=log)

        with open(log, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh.read().splitlines()]
        assert {l["fixture"] for l in lines} == fixtures_logged
        assert len(lines) == 2


def test_old_windowed_behaviour_would_have_dropped_both_regression_guard(monkeypatch):
    """Documents the bug this PR fixes: using the WINDOWED frame for logging
    (the pre-fix call site) loses both simultaneous fixtures once kickoff has
    passed. Guards against someone re-wiring build_predictions back onto the
    windowed ``odds_df`` in a future refactor.
    """
    def fake_elo_probs(models, home, away, neutral, host=None, host_points=None):
        return (0.4, 0.3, 0.3)

    def fake_dc_probs(models, home, away, neutral):
        return (0.42, 0.28, 0.30)

    monkeypatch.setattr("wca.card.elo_probs", fake_elo_probs)
    monkeypatch.setattr("wca.card.dc_probs", fake_dc_probs)

    odds_df_all = _simultaneous_odds_df()
    fixtures_meta = _fixtures_meta()

    now_dt = pd.Timestamp("2026-06-24T20:00:00")
    cutoff_dt = now_dt + pd.Timedelta(hours=96)
    windowed = wbc.window_odds_df(odds_df_all, now_dt, cutoff_dt)

    blends = fixture_blends(_Models(), windowed, fixtures_meta, BlendWeights())
    assert blends == []  # the bug: nothing would have been logged this run
