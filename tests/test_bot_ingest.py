"""Tests for betslip-screenshot ingestion + cached /card in the bot loop."""
from __future__ import annotations

import sqlite3

import wca.bot.app as app
import wca.bot.vision as vision
from wca import cardcache


def _bet(match, sel, **kw):
    return vision.ExtractedBet(match_desc=match, market=kw.get("market", "h2h"),
                               selection=sel, bookmaker=kw.get("bookmaker"),
                               decimal_odds=kw.get("decimal_odds"),
                               stake=kw.get("stake"), is_boost=kw.get("is_boost", False),
                               confidence=kw.get("confidence", 0.9))


def test_handle_photo_parks_and_summarizes(monkeypatch):
    parsed = [_bet("Mexico vs South Africa", "Mexico", decimal_odds=1.45, stake=10.0,
                   bookmaker="paddypower", is_boost=True)]
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: parsed)
    pending = {}
    msg = app.handle_photo(b"img", 42, pending)
    assert pending[42] == parsed
    assert "Mexico" in msg and "yes" in msg.lower() and "boost" in msg.lower()


def test_handle_photo_no_bets(monkeypatch):
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [])
    pending = {}
    msg = app.handle_photo(b"img", 1, pending)
    assert "No bets" in msg and 1 not in pending


def test_handle_photo_vision_error(monkeypatch):
    def boom(*a, **k):
        raise vision.VisionError("no api key")
    monkeypatch.setattr(vision, "extract_bets_from_image", boom)
    msg = app.handle_photo(b"img", 1, {})
    assert "Couldn't read" in msg and "no api key" in msg


def test_confirmation_yes_logs_to_ledger(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet("USA vs Paraguay", "Paraguay", decimal_odds=4.2, stake=5.0,
                        bookmaker="virginbet")]}
    out = app.handle_photo_confirmation("yes", 7, db, pending, ts_utc="2026-06-11T18:00:00")
    assert "Logged 1" in out and 7 not in pending
    con = sqlite3.connect(db)
    rows = con.execute("select match_desc, selection, decimal_odds, stake, platform "
                       "from bets").fetchall()
    con.close()
    # Platform is normalised on ingest ("virginbet" -> "Virgin Bet").
    assert rows == [("USA vs Paraguay", "Paraguay", 4.2, 5.0, "Virgin Bet")]


def test_confirmation_no_discards(tmp_path):
    db = str(tmp_path / "t.db")
    pending = {7: [_bet("A vs B", "A", decimal_odds=2.0, stake=1.0)]}
    out = app.handle_photo_confirmation("no", 7, db, pending)
    assert "Discarded 1" in out and 7 not in pending


def test_confirmation_none_when_not_pending():
    assert app.handle_photo_confirmation("yes", 99, "x.db", {}) is None


def test_confirmation_ignores_non_yesno_keeps_parked():
    pending = {7: [_bet("A vs B", "A")]}
    # A normal command while a slip is parked must not consume the slip.
    assert app.handle_photo_confirmation("/summary", 7, "x.db", pending) is None
    assert 7 in pending


def test_handle_card_reads_cache(tmp_path):
    path = str(tmp_path / "card.md")
    cardcache.write_card("PICKS HERE", path, ts_utc="2026-06-11T12:00:00")
    out = app.handle_card("x.db", card_path=path, now_utc="2026-06-11T13:00:00")
    assert "PICKS HERE" in out and "generated 2026-06-11T12:00:00" in out
    assert "STALE" not in out


def test_handle_card_flags_stale(tmp_path):
    path = str(tmp_path / "card.md")
    cardcache.write_card("OLD", path, ts_utc="2026-06-10T00:00:00")
    out = app.handle_card("x.db", card_path=path, now_utc="2026-06-11T13:00:00")
    assert "STALE" in out


def test_handle_card_missing_cache(tmp_path):
    out = app.handle_card("x.db", card_path=str(tmp_path / "nope.md"))
    assert "No card cached" in out
