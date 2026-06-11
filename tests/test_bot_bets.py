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
    assert app._venue_of("Kalshi") == "kalshi"
    assert app._venue_of("virginbet") == "sportsbook"
    assert app._venue_of(None) == "sportsbook"
