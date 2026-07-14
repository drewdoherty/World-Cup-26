"""Sim-consistency validator + transitive staleness propagation.

Regression guards for the 2026-07-13 live incident: the tournament sim pins
played knockout ties by TEAM-PAIR (``tournament2026._play_ko``), so with the
R16 Morocco-Netherlands tie missing from the pin set (stale
``data/raw/shootouts.csv``) the sim branches where Netherlands advanced
produced a France-Netherlands QF, France's PINNED QF win silently failed to
apply, and France showed P(SF)=0.5691 (Argentina 0.72) despite both having
already won their QFs — while the state-freshness gate flagged NEITHER team,
because their own ties WERE pinned. Two defences are pinned here:

* ``stage_prob_consistency`` — output-side: for every undecided real knockout
  tie the two teams' P(next stage) must sum to ~1.0, and P(win) over the
  alive set must sum to ~1.0;
* ``knockout_state_staleness(propagate=True)`` — input-side: one bracket hop
  of contamination, so a team whose pinned opponent's own path is unresolved
  is flagged with a distinct "state-stale (propagated)" reason;
* the feed guard — an existing consistency-passing feed is never overwritten
  by a consistency-failing rebuild without ``WCA_ALLOW_INCONSISTENT=1``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca.advancement import (  # noqa: E402
    CONSISTENCY_DEFAULT_TOLERANCE,
    knockout_state_staleness,
    stage_prob_consistency,
)

_STAGE_COLS = {"R32": "P(R32)", "R16": "P(R16)", "QF": "P(QF)", "SF": "P(SF)",
               "F": "P(Final)", "win": "P(win)"}


def _recs(teams):
    """Sim records (cached-feed shape): {team: {stage: prob}} -> list of rows."""
    out = []
    for team, probs in teams.items():
        row = {"team": team, "group": "?"}
        for st, col in _STAGE_COLS.items():
            row[col] = probs.get(st, 0.0)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# (a) Per-tie complementarity: pass / fail / tolerance.
# ---------------------------------------------------------------------------


def test_validator_passes_consistent_bracket():
    recs = _recs({
        "France": {"SF": 1.0, "F": 0.4456, "win": 0.20},
        "Spain": {"SF": 1.0, "F": 0.5544, "win": 0.27},
        "England": {"SF": 1.0, "F": 0.48, "win": 0.25},
        "Argentina": {"SF": 1.0, "F": 0.52, "win": 0.28},
    })
    ties = [("France", "Spain", "SF"), ("England", "Argentina", "SF")]
    rep = stage_prob_consistency(recs, ties)
    assert rep["ok"] is True
    assert rep["checked_ties"] == 2
    assert rep["failures"] == []
    assert rep["tolerance"] == CONSISTENCY_DEFAULT_TOLERANCE
    assert rep["ties"][0]["sum"] == pytest.approx(1.0)
    assert rep["ties"][0]["next_stage"] == "F"
    # win-sum over the 4 alive teams (default = tie participants) is clean.
    assert rep["win_sum"]["ok"] is True
    assert rep["win_sum"]["sum"] == pytest.approx(1.0)


def test_validator_fails_contaminated_tie():
    """The France failure shape: pinned-but-unanchored upstream tie leaks
    next-stage mass to teams not actually in the bracket, so the SF pair's
    P(Final) sums well short of 1.0."""
    recs = _recs({
        "France": {"SF": 0.5691, "F": 0.355, "win": 0.18},
        "Spain": {"SF": 1.0, "F": 0.425, "win": 0.22},
    })
    rep = stage_prob_consistency(
        recs, [("France", "Spain", "SF")], check_win_sum=False
    )
    assert rep["ok"] is False
    assert len(rep["failures"]) == 1
    f = rep["failures"][0]
    assert f["teams"] == ["France", "Spain"]
    assert f["stage"] == "SF" and f["next_stage"] == "F"
    assert f["sum"] == pytest.approx(0.355 + 0.425)
    assert f["ok"] is False


def test_validator_respects_tolerance():
    recs = _recs({"France": {"F": 0.4456}, "Spain": {"F": 0.5394}})  # sum .985
    tie = [("France", "Spain", "SF")]
    ok_wide = stage_prob_consistency(recs, tie, tolerance=0.02,
                                     check_win_sum=False)
    ok_tight = stage_prob_consistency(recs, tie, tolerance=0.01,
                                      check_win_sum=False)
    assert ok_wide["ok"] is True
    assert ok_tight["ok"] is False


def test_validator_accepts_dataframe_and_mapping_ties():
    df = pd.DataFrame([
        {"team": "France", "P(Final)": 0.4456, "P(win)": 0.45},
        {"team": "Spain", "P(Final)": 0.5544, "P(win)": 0.55},
    ]).set_index("team")
    rep = stage_prob_consistency(
        df, [{"teams": ("France", "Spain"), "stage": "SF"}]
    )
    assert rep["ok"] is True and rep["checked_ties"] == 1


def test_validator_final_tie_checks_win_column():
    recs = _recs({"Spain": {"win": 0.61}, "Argentina": {"win": 0.39}})
    rep = stage_prob_consistency(recs, [("Spain", "Argentina", "F")])
    assert rep["ok"] is True
    assert rep["ties"][0]["next_stage"] == "win"


def test_validator_unknown_stage_raises():
    with pytest.raises(ValueError):
        stage_prob_consistency(_recs({}), [("A", "B", "R64")])


def test_validator_trivial_with_no_undecided_ties():
    rep = stage_prob_consistency(_recs({"France": {"win": 1.0}}), [])
    assert rep["ok"] is True
    assert rep["checked_ties"] == 0
    assert rep["win_sum"] is None


def test_validator_missing_team_counts_as_zero_and_fails():
    """A real bracket participant the sim does not know about IS an
    inconsistency — its probability contributes 0 and the sum fails."""
    recs = _recs({"France": {"F": 0.55}})
    rep = stage_prob_consistency(
        recs, [("France", "Atlantis", "SF")], check_win_sum=False
    )
    assert rep["ok"] is False
    assert rep["failures"][0]["sum"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# (b) Terminal win-sum over the alive set.
# ---------------------------------------------------------------------------


def test_win_sum_detects_champion_mass_leak():
    """Contamination leaks P(win) mass to teams no longer alive (Netherlands
    branches crowned a champion outside the real SF quartet), driving the
    alive-set win-sum below 1.0 even if per-tie sums were repaired."""
    recs = _recs({
        "France": {"F": 0.45, "win": 0.16},
        "Spain": {"F": 0.55, "win": 0.24},
        "England": {"F": 0.48, "win": 0.22},
        "Argentina": {"F": 0.52, "win": 0.28},
        "Netherlands": {"win": 0.10},  # eliminated in reality; sim disagrees
    })
    ties = [("France", "Spain", "SF"), ("England", "Argentina", "SF")]
    rep = stage_prob_consistency(
        recs, ties, alive_teams=["France", "Spain", "England", "Argentina"]
    )
    assert rep["ok"] is False
    assert rep["win_sum"]["ok"] is False
    assert rep["win_sum"]["sum"] == pytest.approx(0.90)
    assert rep["failures"][-1]["check"] == "win_sum"
    # Per-tie sums were fine — the win-sum is what caught it.
    assert all(t["ok"] for t in rep["ties"])


def test_win_sum_skippable_when_alive_set_indeterminable():
    recs = _recs({"France": {"F": 0.45, "win": 0.10},
                  "Spain": {"F": 0.55, "win": 0.20}})
    rep = stage_prob_consistency(
        recs, [("France", "Spain", "SF")], check_win_sum=False
    )
    assert rep["win_sum"] is None
    assert rep["ok"] is True  # only the tie sum was checked


# ---------------------------------------------------------------------------
# (c) Staleness propagation: the France case.
# ---------------------------------------------------------------------------

_NOW = pd.Timestamp("2026-07-13T12:00:00")


def _ko_csv(tmp_path, rows) -> str:
    """Minimal cleaned-results CSV; rows = (date, home, away, hs, as) tuples."""
    df = pd.DataFrame(
        [
            {
                "date": d, "home_team": h, "away_team": a,
                "home_score": hs, "away_score": as_,
                "tournament": "FIFA World Cup", "city": "X",
                "country": "Y", "neutral": True,
            }
            for d, h, a, hs, as_ in rows
        ]
    )
    path = tmp_path / "results.csv"
    df.to_csv(path, index=False)
    return str(path)


# France (group I) beat Morocco (C) in a pinned QF, but Morocco's own R16 vs
# Netherlands (F) went to pens and is NOT pinned (stale shootouts.csv) — the
# exact 2026-07-13 live shape. All cross-group, so every row is a KO tie.
_FRANCE_CASE_ROWS = [
    ("2026-07-11", "Morocco", "Netherlands", 1, 1),  # R16, pens — unpinned
    ("2026-07-12", "France", "Morocco", 2, 0),       # QF — pinned
]


def test_propagation_flags_pinned_team_behind_unsettled_opponent(tmp_path):
    csv = _ko_csv(tmp_path, _FRANCE_CASE_ROWS)
    rj = str(tmp_path / "missing.json")
    pinned = {frozenset(("France", "Morocco")): "France"}
    flags = knockout_state_staleness(
        pinned, results_path=csv, results_json_path=rj, now=_NOW
    )
    # Direct: both participants of the unpinned R16. Propagated: France, whose
    # pinned QF only binds in branches where Morocco actually arrives.
    assert set(flags) == {"Morocco", "Netherlands", "France"}
    assert flags["Morocco"].startswith("state-stale:")
    assert flags["Netherlands"].startswith("state-stale:")
    assert flags["France"].startswith("state-stale (propagated):")
    assert "pinned tie vs Morocco" in flags["France"]
    assert "Morocco vs Netherlands" in flags["France"]


def test_propagation_silent_when_everything_is_pinned(tmp_path):
    csv = _ko_csv(tmp_path, _FRANCE_CASE_ROWS)
    rj = str(tmp_path / "missing.json")
    pinned = {
        frozenset(("Morocco", "Netherlands")): "Morocco",
        frozenset(("France", "Morocco")): "France",
    }
    flags = knockout_state_staleness(
        pinned, results_path=csv, results_json_path=rj, now=_NOW
    )
    assert flags == {}


def test_propagation_can_be_disabled(tmp_path):
    """propagate=False restores the exact pre-2026-07-13 direct-only set."""
    csv = _ko_csv(tmp_path, _FRANCE_CASE_ROWS)
    rj = str(tmp_path / "missing.json")
    pinned = {frozenset(("France", "Morocco")): "France"}
    flags = knockout_state_staleness(
        pinned, results_path=csv, results_json_path=rj, now=_NOW,
        propagate=False,
    )
    assert set(flags) == {"Morocco", "Netherlands"}


def test_propagation_direct_reason_wins_over_propagated(tmp_path):
    """A team that qualifies for both keeps its DIRECT reason (Morocco here:
    direct via the unpinned R16, propagated-eligible via the pinned QF whose
    opponent France could also be flagged in other shapes)."""
    csv = _ko_csv(tmp_path, _FRANCE_CASE_ROWS)
    rj = str(tmp_path / "missing.json")
    pinned = {frozenset(("France", "Morocco")): "France"}
    flags = knockout_state_staleness(
        pinned, results_path=csv, results_json_path=rj, now=_NOW
    )
    assert flags["Morocco"].startswith("state-stale:")
    assert "(propagated)" not in flags["Morocco"]


# ---------------------------------------------------------------------------
# (d) Feed guard: ok:true -> ok:false overwrite is refused without the env var.
# ---------------------------------------------------------------------------


def _consistency(ok, failures=None):
    return {"checked_ties": 2, "failures": failures or [], "ok": ok,
            "tolerance": 0.02}


def _existing_feed(tmp_path, consistency_ok):
    out = tmp_path / "advancement_data.json"
    meta = {"generated": "2026-07-13 09:00:00 UTC", "n_pm_markets": 83}
    if consistency_ok is not None:
        meta["consistency"] = _consistency(consistency_ok)
    out.write_text(json.dumps({"meta": meta, "teams": [], "groups": {}}),
                   encoding="utf-8")
    return str(out)


def test_guard_refuses_ok_true_to_ok_false(tmp_path, monkeypatch):
    import wca_advancement_data as mod

    monkeypatch.delenv("WCA_ALLOW_INCONSISTENT", raising=False)
    out = _existing_feed(tmp_path, consistency_ok=True)
    bad = _consistency(False, failures=[{"teams": ["France", "Spain"],
                                         "stage": "SF", "sum": 0.78,
                                         "ok": False}])
    assert mod._refuse_inconsistent_overwrite(out, bad) is True


def test_guard_env_var_allows_the_overwrite(tmp_path, monkeypatch):
    import wca_advancement_data as mod

    monkeypatch.setenv("WCA_ALLOW_INCONSISTENT", "1")
    out = _existing_feed(tmp_path, consistency_ok=True)
    assert mod._refuse_inconsistent_overwrite(out, _consistency(False)) is False


def test_guard_never_blocks_clean_or_unknown_builds(tmp_path, monkeypatch):
    import wca_advancement_data as mod

    monkeypatch.delenv("WCA_ALLOW_INCONSISTENT", raising=False)
    out = _existing_feed(tmp_path, consistency_ok=True)
    assert mod._refuse_inconsistent_overwrite(out, _consistency(True)) is False
    assert mod._refuse_inconsistent_overwrite(out, _consistency(None)) is False
    assert mod._refuse_inconsistent_overwrite(out, None) is False


def test_guard_allows_when_existing_feed_never_passed(tmp_path, monkeypatch):
    import wca_advancement_data as mod

    monkeypatch.delenv("WCA_ALLOW_INCONSISTENT", raising=False)
    # Existing feed failed the check itself -> replacing it is fine.
    out_bad = _existing_feed(tmp_path, consistency_ok=False)
    assert mod._refuse_inconsistent_overwrite(out_bad, _consistency(False)) is False
    # Existing feed predates the consistency stamp -> no basis to refuse.
    out_legacy = _existing_feed(tmp_path, consistency_ok=None)
    assert mod._refuse_inconsistent_overwrite(out_legacy, _consistency(False)) is False
    # No existing feed at all -> write the honest (flagged) one.
    missing = str(tmp_path / "nope.json")
    assert mod._refuse_inconsistent_overwrite(missing, _consistency(False)) is False


# ---------------------------------------------------------------------------
# Script-side bracket extraction from site/scores_markets.json.
# ---------------------------------------------------------------------------


def _scores_feed(tmp_path, pens_winner_known=True):
    """Mini bracket in the scores_markets.json shape: R32 decided (one on
    pens), two real undecided R16 ties, a projected QF row to be ignored."""
    scores = {
        "r32_games": [
            {"home": "France", "away": "Norway", "ft": "2-0",
             "projected": False},
            {"home": "Spain", "away": "Uruguay", "ft": "1-1",
             "winner": ("Spain" if pens_winner_known else None),
             "projected": False},
            {"home": "England", "away": "Panama", "ft": "3-0",
             "projected": False},
            {"home": "Argentina", "away": "Austria", "ft": "1-0",
             "projected": False},
        ],
        "r16_games": [
            {"home": "France", "away": "Spain", "ft": None,
             "projected": False},
            {"home": "England", "away": "Argentina", "ft": None,
             "projected": False},
        ],
        "qf_games": [
            {"home": "France", "away": "England", "ft": None,
             "projected": True},  # model-inferred matchup: must be ignored
        ],
    }
    path = tmp_path / "scores_markets.json"
    path.write_text(json.dumps(scores), encoding="utf-8")
    return str(path)


def test_undecided_ties_extracted_from_scores_feed(tmp_path):
    import wca_advancement_data as mod

    ties, alive, can_win = mod._undecided_bracket_ties(
        {}, scores_path=_scores_feed(tmp_path)
    )
    assert ties == [("France", "Spain", "R16"), ("England", "Argentina", "R16")]
    assert can_win is True
    assert alive == ["Argentina", "England", "France", "Spain"]


def test_unknown_pens_winner_disables_win_sum_but_keeps_ties(tmp_path):
    """A drawn decided tie with no winner field falls back to the sim pins;
    with neither, the alive set is indeterminable -> win-sum is skipped
    (conservative), while the per-tie checks still run."""
    import wca_advancement_data as mod

    path = _scores_feed(tmp_path, pens_winner_known=False)
    # Pins know the winner -> alive set still determinable.
    ties, alive, can_win = mod._undecided_bracket_ties(
        {frozenset(("Spain", "Uruguay")): "Spain"}, scores_path=path
    )
    assert can_win is True and "Spain" in alive and "Uruguay" not in alive
    # No pins either -> skip the win-sum, keep the ties.
    ties, alive, can_win = mod._undecided_bracket_ties({}, scores_path=path)
    assert can_win is False and alive is None
    assert len(ties) == 2


def test_consistency_block_shape_and_failure_warning(tmp_path, capsys):
    import wca_advancement_data as mod

    sim_df = pd.DataFrame([
        # France's SF prob short of 1.0 and the R16 Final-sums leaking: the
        # 2026-07-13 contamination shape scaled onto the mini bracket.
        {"team": "France", "P(Final)": 0.30, "P(win)": 0.15},
        {"team": "Spain", "P(Final)": 0.40, "P(win)": 0.20},
        {"team": "England", "P(Final)": 0.48, "P(win)": 0.22},
        {"team": "Argentina", "P(Final)": 0.52, "P(win)": 0.28},
    ]).set_index("team")
    block = mod._consistency_block(sim_df, {}, scores_path=_scores_feed(tmp_path))
    assert set(block) == {"checked_ties", "failures", "ok", "tolerance"}
    assert block["ok"] is False
    assert block["checked_ties"] == 2
    assert any(f.get("check") == "win_sum" or f.get("teams") == ["France", "Spain"]
               for f in block["failures"])
    assert "CONSISTENCY CHECK FAILED" in capsys.readouterr().err


def test_consistency_block_ok_none_when_bracket_source_missing(tmp_path, capsys):
    import wca_advancement_data as mod

    sim_df = pd.DataFrame([{"team": "France", "P(win)": 1.0}]).set_index("team")
    block = mod._consistency_block(
        sim_df, {}, scores_path=str(tmp_path / "missing.json")
    )
    assert block["ok"] is None
    assert block["checked_ties"] == 0
    assert "unavailable" in capsys.readouterr().err
