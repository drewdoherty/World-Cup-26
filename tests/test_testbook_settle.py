"""Tests for paper-book settlement — the FT-vs-advance grading distinction."""

from __future__ import annotations

from wca.testbook import settle, store


def _bet(basis, sel, fixture="France vs Sweden"):
    return {"id": 1, "resolution_basis": basis, "selection": sel, "fixture": fixture}


# Result: France 1 - 1 Sweden at 90' (a draw), France home.
_RES = {frozenset({"France", "Sweden"}): ("2026-07-01", "France", 1, 1)}


def test_ft_win_loses_on_a_90min_draw():
    # The crux: level at 90' -> FT "France win" LOSES even though they might advance.
    assert settle.grade(_bet("FT", "France win (FT 90')"), _RES) == "lost"
    assert settle.grade(_bet("FT", "Draw (FT)"), _RES) == "won"


def test_advance_wins_when_progressed_despite_ft_draw():
    reached = {"France": "QF"}                       # France went through (e.g. on pens)
    assert settle.grade(_bet("advance", "France to reach R16"), _RES, reached) == "won"
    assert settle.grade(_bet("advance", "France to reach SF"), _RES, reached) == "lost"
    # No reached mapping -> advance bets stay open (None).
    assert settle.grade(_bet("advance", "France to reach R16"), _RES, None) is None


def test_score_families_grade_from_final_score():
    assert settle.grade(_bet("btts", "BTTS Yes"), _RES) == "won"        # 1-1 both scored
    assert settle.grade(_bet("totals", "Over 2.5"), _RES) == "lost"     # 2 goals < 2.5
    assert settle.grade(_bet("totals", "Under 2.5"), _RES) == "won"
    assert settle.grade(_bet("exact", "Exact 1-1"), _RES) == "won"
    assert settle.grade(_bet("exact", "Exact 2-0"), _RES) == "lost"


def test_orientation_flips_when_result_home_differs():
    # Bet fixture lists France first, but the actual match had Sweden at home 2-0.
    res = {frozenset({"France", "Sweden"}): ("2026-07-01", "Sweden", 2, 0)}
    assert settle.grade(_bet("FT", "Sweden win (FT 90')"), res) == "won"
    assert settle.grade(_bet("FT", "France win (FT 90')"), res) == "lost"
    assert settle.grade(_bet("exact", "Exact 0-2"), res) == "won"       # France 0 - 2 Sweden


def test_unresolved_when_no_result():
    assert settle.grade(_bet("FT", "France win (FT 90')"), {}) is None


def test_settle_open_updates_book_and_bankroll():
    con = store.connect(":memory:")
    store.seed_bankroll(con, 2000.0, ts_utc="t0")
    store.log_paper_bet(con, ts_utc="t1", fixture="France vs Sweden", market_type="btts",
                        selection="BTTS Yes", resolution_basis="btts", entry_price=0.40, stake_usd=100.0)
    summ = settle.settle_open(con, _RES, ts_utc="t2")
    assert summ["settled"]["won"] == 1
    # BTTS won at 0.40: pl = 100*(0.6/0.4)=150 -> bankroll 1900 +100 +150 = 2150
    assert round(store.realized_balance(con), 2) == 2150.0
