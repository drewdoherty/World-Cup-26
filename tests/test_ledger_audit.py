"""Tests for the one-time ledger audit/repair script (scripts/wca_ledger_audit.py).

Focus on the risky pass: auto-settlement grading. A synthetic ledger is built
with wca.ledger.store; results are passed as an in-memory lookup so no CSV/IO
is needed. The closing-odds pass needs odds_snapshots fixtures and is exercised
by the real dry-run instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from wca.ledger import store

# Load the script module directly (scripts/ is not a package).
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_ledger_audit.py"
_spec = importlib.util.spec_from_file_location("wca_ledger_audit", _SCRIPT)
audit = importlib.util.module_from_spec(_spec)
sys.modules["wca_ledger_audit"] = audit
_spec.loader.exec_module(audit)


def _db(tmp_path):
    db = str(tmp_path / "t.db")
    store.init_db(db)
    return db


def _bet(db, match_desc, market, selection, odds=3.0, stake=10.0):
    return store.record_bet(
        "2026-06-11T10:00:00", "M", match_desc, market, selection,
        "betfair", odds, stake, db_path=db,
    )


# -- pure grading ---------------------------------------------------------


def test_grade_1x2_home_away_draw():
    assert audit.grade_1x2("Scotland", "Scotland", "Morocco", "2-1") == "won"
    assert audit.grade_1x2("Morocco", "Scotland", "Morocco", "2-1") == "lost"
    assert audit.grade_1x2("The Draw", "Scotland", "Morocco", "1-1") == "won"
    assert audit.grade_1x2("Morocco", "Scotland", "Morocco", "0-2") == "won"


def test_grade_1x2_polymarket_no_share():
    # "Scotland No" wins when Scotland does NOT win.
    assert audit.grade_1x2("Scotland No", "Scotland", "Morocco", "0-1") == "won"
    assert audit.grade_1x2("Scotland No", "Scotland", "Morocco", "2-0") == "lost"


def test_grade_1x2_unmappable_selection_returns_none():
    assert audit.grade_1x2("Some Bet Builder leg", "Scotland", "Morocco", "1-0") is None


def test_results_lookup_excludes_pre_tournament_history(tmp_path):
    csv = tmp_path / "r.csv"
    csv.write_text(
        "date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n"
        "2011-03-29,England,Ghana,1,1,Friendly,London,England,FALSE\n"
        "2026-06-14,Scotland,Morocco,0,2,FIFA World Cup,Foxborough,United States,TRUE\n"
    )
    lut = audit.build_results_lookup(str(csv), since="2026-06-01")
    # historical England-Ghana friendly must NOT settle the unplayed WC fixture
    assert audit.result_for("England vs Ghana", lut) is None
    # an in-window result is still found
    assert audit.result_for("Scotland vs Morocco", lut) == ("Scotland", "Morocco", "0-2")


def test_result_for_found_and_ambiguous():
    lut = {("a", "b"): [("2026-06-10", "1-0")]}
    assert audit.result_for("A vs B", lut) == ("A", "B", "1-0")
    # rematch with differing scores -> ambiguous -> None (settle manually)
    lut2 = {("a", "b"): [("2026-06-10", "1-0"), ("2026-06-20", "2-2")]}
    assert audit.result_for("A vs B", lut2) is None
    # unknown fixture
    assert audit.result_for("X vs Y", lut) is None


# -- settle pass ----------------------------------------------------------


def test_settle_pass_dry_run_makes_no_writes(tmp_path):
    db = _db(tmp_path)
    bid = _bet(db, "Scotland vs Morocco", "Match Odds", "Morocco")
    lut = {("scotland", "morocco"): [("2026-06-14", "0-2")]}  # Morocco won
    con = store._connect(db)
    settled, manual = audit.pass_settle(con, db, lut, apply=False, log=lambda m: None)
    con.close()
    assert settled == [(bid, "won")]
    con = store._connect(db)
    assert con.execute("SELECT status FROM bets WHERE id=?", (bid,)).fetchone()[0] == "open"
    con.close()


def test_settle_pass_apply_settles_with_correct_pnl(tmp_path):
    db = _db(tmp_path)
    bid = _bet(db, "Scotland vs Morocco", "Match Odds", "Morocco", odds=3.0, stake=10.0)
    lut = {("scotland", "morocco"): [("2026-06-14", "0-2")]}
    con = store._connect(db)
    audit.pass_settle(con, db, lut, apply=True, log=lambda m: None)
    con.close()
    con = store._connect(db)
    row = con.execute("SELECT status, settled_pl FROM bets WHERE id=?", (bid,)).fetchone()
    con.close()
    assert row["status"] == "won"
    assert abs(row["settled_pl"] - (3.0 - 1.0) * 10.0) == pytest.approx(0.0, abs=1e-6)


def test_settle_pass_non_1x2_goes_to_manual_untouched(tmp_path):
    db = _db(tmp_path)
    bid = _bet(db, "Scotland vs Morocco", "Correct Score", "2-0", odds=8.0, stake=5.0)
    lut = {("scotland", "morocco"): [("2026-06-14", "0-2")]}
    con = store._connect(db)
    settled, manual = audit.pass_settle(con, db, lut, apply=True, log=lambda m: None)
    con.close()
    assert settled == []
    assert [m[0] for m in manual] == [bid]
    con = store._connect(db)
    assert con.execute("SELECT status FROM bets WHERE id=?", (bid,)).fetchone()[0] == "open"
    con.close()


def test_settle_pass_unconcluded_match_left_open(tmp_path):
    db = _db(tmp_path)
    bid = _bet(db, "Belgium vs Iran", "Match Odds", "Belgium")
    con = store._connect(db)
    settled, manual = audit.pass_settle(con, db, {}, apply=True, log=lambda m: None)
    con.close()
    assert settled == [] and manual == []
    con = store._connect(db)
    assert con.execute("SELECT status FROM bets WHERE id=?", (bid,)).fetchone()[0] == "open"
    con.close()
