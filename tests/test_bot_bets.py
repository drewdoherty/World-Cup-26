"""Tests for /bets and the per-pool /summary."""
from __future__ import annotations

import wca.bot.app as app
from wca.ledger.store import add_bankroll_event, record_bet


def _seed(db):
    record_bet("2026-06-11T10:00:00", "M1", "USA vs Paraguay", "h2h", "Paraguay",
               "virginbet", 4.2, 5.68, db_path=db)
    record_bet("2026-06-11T10:00:00", "M2", "Treble", "acca", "3 legs",
               "betfair_sportsbook", 20.41, 2.0, notes="FREE bet SNR", db_path=db)
    record_bet("2026-06-11T10:00:00", "M3", "Mexico vs South Africa", "pm_moneyline",
               "Mexico Yes", "polymarket", 1.449, 22.0, notes="currency=USD", db_path=db)
    add_bankroll_event("2026-06-11T09:00:00", 1310.0, "deposit pool=polymarket currency=USD", db_path=db)
    add_bankroll_event("2026-06-11T09:00:00", 1000.0, "notional pool=sportsbook currency=GBP", db_path=db)


def test_bets_groups_by_venue_with_max_win_loss(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    out = app.handle_bets(db)
    assert "SPORTSBOOK" in out and "POLYMARKET" in out
    # Paraguay: win 5.68*3.2=18.18; free acca: win 2*19.41=38.82, loss 0
    assert "£18.18" in out
    assert "(free)" in out
    # sportsbook max loss excludes the free stake: only 5.68
    assert "max win £57.00 / max loss £5.68" in out
    # polymarket: win 22*0.449=9.88, loss 22
    assert "max win $9.88 / max loss $22.00" in out
    assert "TOTAL" in out


def test_bets_empty(tmp_path):
    db = str(tmp_path / "e.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "x", 2.0, 1.0, db_path=db)
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE bets SET status='won', settled_pl=1.0")
    con.commit(); con.close()
    assert "flat" in app.handle_bets(db)


def test_summary_shows_pools_and_open_risk(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    out = app.handle_summary(db)
    assert "At risk (open):" in out
    assert "sportsbook: £1000.00" in out
    assert "polymarket: $1310.00" in out
    assert "at risk $22.00" in out


def test_venue_mapping():
    assert app._venue_of("polymarket") == "polymarket"


# ---------------------------------------------------------------------------
# handle_settle: P&L pays at the backed price, CLV is the execution ratio,
# and an auto-captured close is honoured (and never wiped on void).
# ---------------------------------------------------------------------------


def _one_open_bet(db, odds=4.2, stake=5.0, model_prob=0.27, closing_odds=None):
    bet_id = record_bet("2026-06-11T10:00:00", "M", "USA vs Paraguay", "h2h",
                        "Paraguay", "virginbet", odds, stake,
                        model_prob=model_prob, db_path=db)
    if closing_odds is not None:
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("UPDATE bets SET closing_odds=? WHERE id=?",
                    (closing_odds, bet_id))
        con.commit(); con.close()
    return bet_id


def _bet_row(db, bet_id):
    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT status, settled_pl, closing_odds, clv FROM bets WHERE id=?",
        (bet_id,),
    ).fetchone()
    con.close()
    return row


def test_settle_pays_at_backed_price_not_close(tmp_path):
    db = str(tmp_path / "s.db")
    bet_id = _one_open_bet(db, odds=4.2, stake=5.0)
    app.handle_settle(f"/settle {bet_id} won 4.0", db)
    row = _bet_row(db, bet_id)
    assert row["status"] == "won"
    # Pays at the BACKED 4.2 (= 5*3.2 = 16.0), NOT the close 4.0 (= 16.0... )
    assert row["settled_pl"] == 5.0 * (4.2 - 1)
    # CLV is the execution ratio: 4.2 / 4.0 - 1 = +5%.
    assert abs(row["clv"] - (4.2 / 4.0 - 1)) < 1e-9


def test_settle_clv_uses_close_not_model_fair(tmp_path):
    db = str(tmp_path / "s.db")
    # model_prob would give fair 1/0.5 = 2.0; the old log(close/fair) CLV would
    # be log(4.0/2.0) ≈ +0.69. The correct execution CLV is 4.2/4.0-1 = +0.05.
    bet_id = _one_open_bet(db, odds=4.2, model_prob=0.5)
    app.handle_settle(f"/settle {bet_id} lost 4.0", db)
    row = _bet_row(db, bet_id)
    assert row["status"] == "lost"
    assert row["settled_pl"] == -5.0
    assert abs(row["clv"] - (4.2 / 4.0 - 1)) < 1e-9


def test_settle_falls_back_to_auto_captured_close(tmp_path):
    db = str(tmp_path / "s.db")
    bet_id = _one_open_bet(db, odds=4.2, closing_odds=4.143)
    out = app.handle_settle(f"/settle {bet_id} lost", db)  # no close given
    assert "needs closing odds" not in out
    row = _bet_row(db, bet_id)
    assert row["closing_odds"] == 4.143
    assert abs(row["clv"] - (4.2 / 4.143 - 1)) < 1e-9


def test_settle_requires_close_when_none_available(tmp_path):
    db = str(tmp_path / "s.db")
    bet_id = _one_open_bet(db, odds=4.2)  # no auto close
    out = app.handle_settle(f"/settle {bet_id} won", db)
    assert "needs closing odds" in out
    assert _bet_row(db, bet_id)["status"] == "open"  # not settled


def test_settle_void_preserves_auto_captured_close(tmp_path):
    db = str(tmp_path / "s.db")
    bet_id = _one_open_bet(db, odds=4.2, closing_odds=4.143)
    app.handle_settle(f"/settle {bet_id} void", db)
    row = _bet_row(db, bet_id)
    assert row["status"] == "void"
    assert row["settled_pl"] == 0.0
    assert row["closing_odds"] == 4.143  # NOT wiped to NULL
    assert app._venue_of("Kalshi") == "kalshi"
    assert app._venue_of("virginbet") == "sportsbook"
    assert app._venue_of(None) == "sportsbook"
