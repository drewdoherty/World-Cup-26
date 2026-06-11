"""Currency handling in vision extraction and bot betslip formatting."""
from __future__ import annotations

import wca.bot.app as app
import wca.bot.vision as vision
from wca.bot.vision import _coerce_currency, currency_symbol


def test_polymarket_always_usd_even_without_hint():
    assert _coerce_currency(None, "Polymarket") == "USD"
    assert _coerce_currency("GBP", "polymarket") == "USD"
    assert _coerce_currency(None, "Kalshi") == "USD"


def test_symbol_and_code_normalization():
    assert _coerce_currency("£", "bet365") == "GBP"
    assert _coerce_currency("$", None) == "USD"
    assert _coerce_currency("USDC", None) == "USD"
    assert _coerce_currency("€", "unibet") == "EUR"
    assert _coerce_currency(None, "virginbet") is None


def test_currency_symbol_display():
    assert currency_symbol("USD") == "$"
    assert currency_symbol("GBP") == "£"
    assert currency_symbol(None) == "£"
    assert currency_symbol("AED") == "AED "


def test_format_extracted_uses_currency():
    usd = vision.ExtractedBet(match_desc="Mexico vs South Africa", market="pm_moneyline",
                              selection="Mexico Yes", bookmaker="Polymarket",
                              decimal_odds=1.449, stake=22.0, currency="USD", confidence=0.9)
    gbp = vision.ExtractedBet(match_desc="A vs B", market="h2h", selection="A",
                              bookmaker="virginbet", decimal_odds=2.0, stake=5.0,
                              currency="GBP", confidence=0.9)
    msg = app._format_extracted([usd, gbp])
    assert "$22.00" in msg and "£5.00" in msg


def test_confirmation_records_currency_in_notes(tmp_path):
    import sqlite3
    db = str(tmp_path / "t.db")
    bet = vision.ExtractedBet(match_desc="Mexico vs South Africa", market="pm_moneyline",
                              selection="Mexico Yes", bookmaker="polymarket",
                              decimal_odds=1.449, stake=22.0, currency="USD", confidence=0.9)
    out = app.handle_photo_confirmation("yes", 1, db, {1: [bet]}, ts_utc="2026-06-11T15:00:00")
    assert "Logged 1" in out
    con = sqlite3.connect(db)
    notes = con.execute("select notes from bets").fetchone()[0]
    con.close()
    assert "currency=USD" in notes
