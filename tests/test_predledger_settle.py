"""Tests for the prediction-ledger settle pass (P0-T3).

Coverage:
- 1x2: home/draw/away win and loss
- scoreline: exact match and miss
- ou_<L>: over/under wins and integer-line push
- btts: both-score (yes/no) both directions
- advancement: won, lost, stays-open-until-decidable
- settle is idempotent (re-run doesn't double-settle)
- only open rows are touched
- both result-file paths resolved correctly
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from wca.predledger.settle import (
    _build_results_index,
    _compute_advancement_state,
    _decide_advancement,
    _fixture_key,
    _parse_score,
    _settle_1x2,
    _settle_btts,
    _settle_ou,
    _settle_scoreline,
    settle_open,
)
from wca.predledger.store import ensure_schema


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _make_db() -> str:
    """Create a temp DB with predledger schema; return its path."""
    tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tf.close()
    ensure_schema(tf.name)
    return tf.name


def _pred_id(build_id: str, market: str, selection: str, line: float = -1.0, stage: str = "", match_id: str = "") -> str:
    raw = f"{build_id}|{match_id}|{stage}|{market}|{selection}|{line}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _insert_prediction(
    db: str,
    *,
    market: str,
    selection: str,
    fixture: str = "Home vs Away",
    line: float = -1.0,
    stage: str = "",
    match_id: str = "",
    n_outcomes: int = 2,
    model_prob: float = 0.5,
    build_id: str = "build1",
    status: str = "open",
) -> str:
    pid = _pred_id(build_id, market, selection, line, stage, match_id)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """INSERT OR IGNORE INTO predictions
           (prediction_id, build_id, ts_utc, match_id, fixture, market, selection,
            line, stage, n_outcomes, model_prob, model_fair_odds, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, build_id, "2026-06-11T00:00:00Z", match_id, fixture, market, selection,
         line, stage, n_outcomes, model_prob, 1.0 / model_prob, status),
    )
    conn.commit()
    conn.close()
    return pid


def _get_status(db: str, pid: str) -> Optional[str]:
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT status FROM predictions WHERE prediction_id=?", (pid,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _get_row(db: str, pid: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM predictions WHERE prediction_id=?", (pid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

RESULTS = [
    {
        "fixture": "Mexico vs South Africa",
        "kickoff_utc": "2026-06-11T19:00:00Z",
        "score": "2-0",
        "outcome": "home",
    },
    {
        "fixture": "Brazil vs Morocco",
        "kickoff_utc": "2026-06-13T22:00:00Z",
        "score": "1-1",
        "outcome": "draw",
    },
    {
        "fixture": "Haiti vs Scotland",
        "kickoff_utc": "2026-06-14T01:00:00Z",
        "score": "0-1",
        "outcome": "away",
    },
]

EMPTY_ADV: List[Dict] = []

# Full group C played (Brazil, Morocco, Haiti, Scotland) - 6 games
# Group C: Brazil 2–0 Haiti, Brazil 1–1 Morocco, Morocco 1–0 Haiti, Haiti 0–1 Scotland,
#          Brazil 2–1 Scotland, Morocco 0–0 Scotland
_GROUP_C_FULL = [
    {"home": "Brazil", "away": "Morocco", "hg": 1, "ag": 1},
    {"home": "Haiti", "away": "Scotland", "hg": 0, "ag": 1},
    {"home": "Brazil", "away": "Haiti", "hg": 2, "ag": 0},
    {"home": "Morocco", "away": "Scotland", "hg": 1, "ag": 0},
    {"home": "Brazil", "away": "Scotland", "hg": 2, "ag": 1},
    {"home": "Morocco", "away": "Haiti", "hg": 2, "ag": 1},
]
# Standings after 3 games each: Brazil 7pts GD+4; Morocco 6pts GD+1; Scotland 3pts GD-1; Haiti 0pts GD-4
# → top-2: Brazil (1st), Morocco (2nd); 3rd: Scotland; 4th (eliminated): Haiti


# ---------------------------------------------------------------------------
# Unit tests: 1x2
# ---------------------------------------------------------------------------


class TestSettle1x2:
    def test_home_win_won(self):
        r = {"outcome": "home", "home_goals": 2, "away_goals": 0}
        assert _settle_1x2("home", r) == "won"

    def test_home_win_away_sel_lost(self):
        r = {"outcome": "home", "home_goals": 2, "away_goals": 0}
        assert _settle_1x2("away", r) == "lost"

    def test_draw_won(self):
        r = {"outcome": "draw", "home_goals": 1, "away_goals": 1}
        assert _settle_1x2("draw", r) == "won"

    def test_draw_home_sel_lost(self):
        r = {"outcome": "draw", "home_goals": 1, "away_goals": 1}
        assert _settle_1x2("home", r) == "lost"

    def test_away_win_won(self):
        r = {"outcome": "away", "home_goals": 0, "away_goals": 1}
        assert _settle_1x2("away", r) == "won"

    def test_empty_outcome_returns_none(self):
        r = {"outcome": "", "home_goals": 0, "away_goals": 0}
        assert _settle_1x2("home", r) is None


# ---------------------------------------------------------------------------
# Unit tests: scoreline
# ---------------------------------------------------------------------------


class TestSettleScoreline:
    def test_exact_match_won(self):
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_scoreline("2-1", r) == "won"

    def test_wrong_score_lost(self):
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_scoreline("1-0", r) == "lost"

    def test_reversed_score_lost(self):
        r = {"home_goals": 2, "away_goals": 0}
        assert _settle_scoreline("0-2", r) == "lost"

    def test_zero_zero_won(self):
        r = {"home_goals": 0, "away_goals": 0}
        assert _settle_scoreline("0-0", r) == "won"


# ---------------------------------------------------------------------------
# Unit tests: O/U
# ---------------------------------------------------------------------------


class TestSettleOU:
    def test_over_25_won_on_3_goals(self):
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_ou("over", 2.5, r) == "won"

    def test_under_25_won_on_1_goal(self):
        r = {"home_goals": 1, "away_goals": 0}
        assert _settle_ou("under", 2.5, r) == "won"

    def test_over_25_lost_on_2_goals(self):
        r = {"home_goals": 1, "away_goals": 1}
        assert _settle_ou("over", 2.5, r) == "lost"

    def test_under_25_lost_on_3_goals(self):
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_ou("under", 2.5, r) == "lost"

    def test_integer_line_push_over(self):
        """O/U 2.0 with 2 total goals → push."""
        r = {"home_goals": 1, "away_goals": 1}
        assert _settle_ou("over", 2.0, r) == "push"

    def test_integer_line_push_under(self):
        """Under 3.0 with 3 goals → push (not won)."""
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_ou("under", 3.0, r) == "push"

    def test_integer_line_no_push_on_4_goals(self):
        """Over 3.0 with 4 total → won."""
        r = {"home_goals": 2, "away_goals": 2}
        assert _settle_ou("over", 3.0, r) == "won"

    def test_missing_line_sentinel_returns_none(self):
        r = {"home_goals": 2, "away_goals": 1}
        assert _settle_ou("over", -1.0, r) is None


# ---------------------------------------------------------------------------
# Unit tests: BTTS
# ---------------------------------------------------------------------------


class TestSettleBTTS:
    def test_btts_yes_both_score_won(self):
        r = {"home_goals": 1, "away_goals": 1}
        assert _settle_btts("yes", r) == "won"

    def test_btts_yes_one_nil_lost(self):
        r = {"home_goals": 1, "away_goals": 0}
        assert _settle_btts("yes", r) == "lost"

    def test_btts_no_one_nil_won(self):
        r = {"home_goals": 2, "away_goals": 0}
        assert _settle_btts("no", r) == "won"

    def test_btts_no_both_score_lost(self):
        r = {"home_goals": 1, "away_goals": 2}
        assert _settle_btts("no", r) == "lost"

    def test_btts_yes_nil_nil_lost(self):
        """Neither team scored → BTTS yes = lost."""
        r = {"home_goals": 0, "away_goals": 0}
        assert _settle_btts("yes", r) == "lost"

    def test_btts_no_nil_nil_won(self):
        r = {"home_goals": 0, "away_goals": 0}
        assert _settle_btts("no", r) == "won"


# ---------------------------------------------------------------------------
# Unit tests: advancement state
# ---------------------------------------------------------------------------


class TestAdvancementState:
    def test_incomplete_group_stays_none(self):
        """Partial group games → group_result stays None."""
        partial = [{"home": "Brazil", "away": "Morocco", "hg": 1, "ag": 1}]
        state = _compute_advancement_state(partial)
        assert state["Brazil"]["group_result"] is None
        assert state["Morocco"]["group_result"] is None

    def test_complete_group_top2_advanced(self):
        """After all 6 group-C games, Brazil and Morocco advance."""
        state = _compute_advancement_state(_GROUP_C_FULL)
        assert state["Brazil"]["group_result"] == "advanced"
        assert state["Morocco"]["group_result"] == "advanced"

    def test_complete_group_4th_eliminated(self):
        state = _compute_advancement_state(_GROUP_C_FULL)
        assert state["Haiti"]["group_result"] == "eliminated"

    def test_complete_group_3rd_stays_open_until_all_groups_done(self):
        """Third-placer (Scotland) is undecided while other groups are incomplete."""
        state = _compute_advancement_state(_GROUP_C_FULL)
        assert state["Scotland"]["group_result"] is None

    def test_knockout_win_increments_ko_wins(self):
        """A non-group match win increments ko_wins."""
        matches = _GROUP_C_FULL + [
            # Brazil plays a knockout match vs a team from another group
            {"home": "Brazil", "away": "Mexico", "hg": 2, "ag": 1}
        ]
        state = _compute_advancement_state(matches)
        assert state["Brazil"]["ko_wins"] == 1
        assert state["Mexico"]["ko_eliminated"] is True

    def test_knockout_loss_marks_eliminated(self):
        matches = _GROUP_C_FULL + [
            {"home": "Morocco", "away": "England", "hg": 0, "ag": 1}
        ]
        state = _compute_advancement_state(matches)
        assert state["Morocco"]["ko_eliminated"] is True
        assert state["England"]["ko_wins"] == 1


# ---------------------------------------------------------------------------
# Unit tests: _decide_advancement
# ---------------------------------------------------------------------------


class TestDecideAdvancement:
    """Covers the decide-advancement logic without a real DB."""

    def _state(self, group_result, ko_wins=0, ko_eliminated=False, group_pos=None):
        return {
            "Brazil": {
                "group_result": group_result,
                "group_pos": group_pos,
                "ko_wins": ko_wins,
                "ko_eliminated": ko_eliminated,
            }
        }

    def test_group_not_decided_r32_open(self):
        s = self._state(None)
        assert _decide_advancement("Brazil R32", "R32", s) is None

    def test_eliminated_groups_r32_lost(self):
        s = self._state("eliminated")
        assert _decide_advancement("Brazil R32", "R32", s) == "lost"

    def test_eliminated_groups_r16_lost(self):
        s = self._state("eliminated")
        assert _decide_advancement("Brazil R16", "R16", s) == "lost"

    def test_advanced_groups_r32_won(self):
        s = self._state("advanced", ko_wins=0)
        assert _decide_advancement("Brazil R32", "R32", s) == "won"

    def test_advanced_groups_r16_still_open(self):
        """Advanced from groups, haven't played R32 match yet → R16 open."""
        s = self._state("advanced", ko_wins=0, ko_eliminated=False)
        assert _decide_advancement("Brazil R16", "R16", s) is None

    def test_won_r32_r16_won(self):
        """Won 1 knockout match → reached R16."""
        s = self._state("advanced", ko_wins=1)
        assert _decide_advancement("Brazil R16", "R16", s) == "won"

    def test_lost_r32_r16_lost(self):
        """Won 0 knockouts but eliminated → lost R16."""
        s = self._state("advanced", ko_wins=0, ko_eliminated=True)
        assert _decide_advancement("Brazil R16", "R16", s) == "lost"

    def test_lost_r16_qf_lost(self):
        """Won R32 (ko_wins=1) but lost R16 → QF prediction = lost."""
        s = self._state("advanced", ko_wins=1, ko_eliminated=True)
        assert _decide_advancement("Brazil QF", "QF", s) == "lost"

    def test_won_r16_qf_won(self):
        s = self._state("advanced", ko_wins=2)
        assert _decide_advancement("Brazil QF", "QF", s) == "won"

    def test_sf_open_when_still_alive_at_qf(self):
        """Won QF (ko_wins=2), not eliminated yet → SF still open."""
        s = self._state("advanced", ko_wins=2, ko_eliminated=False)
        assert _decide_advancement("Brazil SF", "SF", s) is None

    def test_group_winner_pos1_won(self):
        s = {"Brazil": {"group_result": "advanced", "group_pos": 1, "ko_wins": 0, "ko_eliminated": False}}
        assert _decide_advancement("Brazil group_winner", "group_winner", s) == "won"

    def test_group_winner_pos2_lost(self):
        s = {"Brazil": {"group_result": "advanced", "group_pos": 2, "ko_wins": 0, "ko_eliminated": False}}
        assert _decide_advancement("Brazil group_winner", "group_winner", s) == "lost"

    def test_unknown_stage_returns_none(self):
        s = self._state("advanced")
        assert _decide_advancement("Brazil UNKNOWN", "UNKNOWN", s) is None


# ---------------------------------------------------------------------------
# Integration tests: settle_open against a real (temp) DB
# ---------------------------------------------------------------------------


class TestSettleOpen:
    def test_1x2_home_win_settled(self):
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="home",
                                  fixture="Mexico vs South Africa", n_outcomes=3)
        n = settle_open(RESULTS, EMPTY_ADV, db)
        assert n == 1
        assert _get_status(db, pid) == "won"

    def test_1x2_draw_settled(self):
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="draw",
                                  fixture="Brazil vs Morocco", n_outcomes=3)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_1x2_away_win_settled(self):
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="away",
                                  fixture="Haiti vs Scotland", n_outcomes=3)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_1x2_wrong_sel_lost(self):
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="away",
                                  fixture="Mexico vs South Africa", n_outcomes=3)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "lost"

    def test_scoreline_exact_match(self):
        db = _make_db()
        pid = _insert_prediction(db, market="scoreline", selection="2-0",
                                  fixture="Mexico vs South Africa")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_scoreline_miss(self):
        db = _make_db()
        pid = _insert_prediction(db, market="scoreline", selection="1-0",
                                  fixture="Mexico vs South Africa")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "lost"

    def test_ou_over_won(self):
        db = _make_db()
        # Mexico 2-0 South Africa = 2 goals, line=1.5 → over wins
        pid = _insert_prediction(db, market="ou_1.5", selection="over",
                                  fixture="Mexico vs South Africa", line=1.5)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_ou_under_won(self):
        db = _make_db()
        # Brazil 1-1 Morocco = 2 goals, line=2.5 → under wins
        pid = _insert_prediction(db, market="ou_2.5", selection="under",
                                  fixture="Brazil vs Morocco", line=2.5)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_ou_integer_line_push(self):
        db = _make_db()
        # Mexico 2-0 South Africa = 2 goals, line=2.0 → push
        pid = _insert_prediction(db, market="ou_2.0", selection="over",
                                  fixture="Mexico vs South Africa", line=2.0)
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "push"

    def test_btts_yes_won_both_score(self):
        db = _make_db()
        # Brazil 1-1 Morocco → both scored
        pid = _insert_prediction(db, market="btts", selection="yes",
                                  fixture="Brazil vs Morocco")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_btts_no_won_one_nil(self):
        db = _make_db()
        # Mexico 2-0 South Africa → only home scored
        pid = _insert_prediction(db, market="btts", selection="no",
                                  fixture="Mexico vs South Africa")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "won"

    def test_btts_yes_lost_one_nil(self):
        db = _make_db()
        pid = _insert_prediction(db, market="btts", selection="yes",
                                  fixture="Mexico vs South Africa")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "lost"

    def test_btts_no_lost_both_score(self):
        db = _make_db()
        pid = _insert_prediction(db, market="btts", selection="no",
                                  fixture="Brazil vs Morocco")
        settle_open(RESULTS, EMPTY_ADV, db)
        assert _get_status(db, pid) == "lost"

    # --- Advancement ---

    def test_advancement_stays_open_incomplete_group(self):
        """With no adv_results, advancement stays open."""
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R32",
                                  stage="R32", match_id="")
        settle_open([], EMPTY_ADV, db)
        assert _get_status(db, pid) == "open"

    def test_advancement_r32_won_after_group_complete(self):
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R32",
                                  stage="R32", match_id="")
        settle_open([], _GROUP_C_FULL, db)
        # Brazil is top of group C → advanced → R32 won
        assert _get_status(db, pid) == "won"

    def test_advancement_r32_lost_4th_place(self):
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Haiti R32",
                                  stage="R32", match_id="")
        settle_open([], _GROUP_C_FULL, db)
        assert _get_status(db, pid) == "lost"

    def test_advancement_r16_stays_open_when_r32_undecided(self):
        """Group incomplete → R16 prediction stays open."""
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R16",
                                  stage="R16", match_id="")
        partial = [{"home": "Brazil", "away": "Morocco", "hg": 1, "ag": 1}]
        settle_open([], partial, db)
        assert _get_status(db, pid) == "open"

    def test_advancement_r16_stays_open_advanced_no_ko_match(self):
        """Advanced from groups but R32 match not yet played → R16 stays open."""
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R16",
                                  stage="R16", match_id="")
        settle_open([], _GROUP_C_FULL, db)
        # Brazil advanced from groups but no knockout matches played yet
        assert _get_status(db, pid) == "open"

    def test_advancement_r16_won_after_ko_win(self):
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R16",
                                  stage="R16", match_id="")
        ko_match = {"home": "Brazil", "away": "Mexico", "hg": 2, "ag": 1}
        settle_open([], _GROUP_C_FULL + [ko_match], db)
        assert _get_status(db, pid) == "won"

    def test_advancement_r16_lost_after_ko_loss(self):
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R16",
                                  stage="R16", match_id="")
        ko_loss = {"home": "Brazil", "away": "Mexico", "hg": 0, "ag": 1}
        settle_open([], _GROUP_C_FULL + [ko_loss], db)
        assert _get_status(db, pid) == "lost"

    def test_advancement_third_place_stays_open_while_other_groups_incomplete(self):
        """Scotland is 3rd in group C; until all 12 groups finish, stays open."""
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Scotland R32",
                                  stage="R32", match_id="")
        settle_open([], _GROUP_C_FULL, db)
        # Only group C is done — Scotland's R32 fate as a third-placer is undecided
        assert _get_status(db, pid) == "open"

    # --- Idempotency ---

    def test_settle_is_idempotent(self):
        """Running settle twice doesn't change already-settled rows."""
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="home",
                                  fixture="Mexico vs South Africa", n_outcomes=3)
        n1 = settle_open(RESULTS, EMPTY_ADV, db)
        n2 = settle_open(RESULTS, EMPTY_ADV, db)
        assert n1 == 1
        assert n2 == 0  # no open rows left to settle
        assert _get_status(db, pid) == "won"

    def test_already_settled_row_untouched(self):
        """A row with status='won' is never re-settled."""
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="away",
                                  fixture="Mexico vs South Africa", n_outcomes=3,
                                  status="won")  # pre-settled (wrong selection but shouldn't matter)
        n = settle_open(RESULTS, EMPTY_ADV, db)
        assert n == 0
        assert _get_status(db, pid) == "won"  # unchanged

    def test_settle_source_stamped(self):
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="home",
                                  fixture="Mexico vs South Africa", n_outcomes=3)
        settle_open(RESULTS, EMPTY_ADV, db)
        row = _get_row(db, pid)
        assert row["settle_source"] == "results_json"
        assert row["settled_ts"] is not None

    def test_settle_source_advancement_json(self):
        db = _make_db()
        pid = _insert_prediction(db, market="advancement", selection="Brazil R32",
                                  stage="R32")
        settle_open([], _GROUP_C_FULL, db)
        row = _get_row(db, pid)
        assert row["settle_source"] == "advancement_json"

    def test_no_result_leaves_open(self):
        """Prediction for a fixture not in results stays open."""
        db = _make_db()
        pid = _insert_prediction(db, market="1x2", selection="home",
                                  fixture="Germany vs Curaçao", n_outcomes=3)
        settle_open(RESULTS, EMPTY_ADV, db)  # RESULTS doesn't include this fixture
        assert _get_status(db, pid) == "open"

    def test_mixed_markets_settled_correctly(self):
        """Multiple markets for the same fixture are all settled in one pass."""
        db = _make_db()
        pids = {
            "1x2": _insert_prediction(db, market="1x2", selection="home",
                                       fixture="Mexico vs South Africa", n_outcomes=3),
            "score": _insert_prediction(db, market="scoreline", selection="2-0",
                                         fixture="Mexico vs South Africa"),
            "ou": _insert_prediction(db, market="ou_2.5", selection="under",
                                      fixture="Mexico vs South Africa", line=2.5),
            "btts": _insert_prediction(db, market="btts", selection="no",
                                        fixture="Mexico vs South Africa"),
        }
        n = settle_open(RESULTS, EMPTY_ADV, db)
        assert n == 4
        assert _get_status(db, pids["1x2"]) == "won"
        assert _get_status(db, pids["score"]) == "won"
        assert _get_status(db, pids["ou"]) == "won"  # 2 goals < 2.5
        assert _get_status(db, pids["btts"]) == "won"  # Mexico 2-0: no BTTS


# ---------------------------------------------------------------------------
# Path-resolution smoke test
# ---------------------------------------------------------------------------


class TestResultFilePaths:
    """Verify that settle_open reads from the corrected result file paths."""

    def test_results_from_wc2026_results_json(self, tmp_path: Path):
        """Results file at data/processed/wc2026_results.json loads correctly."""
        results_data = {
            "results": [
                {
                    "fixture": "TeamA vs TeamB",
                    "score": "3-0",
                    "outcome": "home",
                    "kickoff_utc": "2026-06-20T19:00:00Z",
                }
            ]
        }
        results_file = tmp_path / "wc2026_results.json"
        results_file.write_text(__import__("json").dumps(results_data))

        index = _build_results_index(results_data["results"])
        assert len(index) == 1
        key = _fixture_key("TeamA vs TeamB")
        assert key in index
        assert index[key]["home_goals"] == 3
        assert index[key]["away_goals"] == 0

    def test_adv_results_from_advancement_played_results_json(self):
        """Advancement results as a bare list of match dicts is parsed correctly."""
        adv_data = [
            {"home": "Brazil", "away": "Morocco", "hg": 2, "ag": 0},
        ]
        state = _compute_advancement_state(adv_data)
        # With 1 game played out of 6 required, groups are incomplete
        assert state["Brazil"]["group_result"] is None
