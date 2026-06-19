"""Free-bet (purple gift) detection in betslip scanning.

The vision layer now extracts an ``is_free_bet`` flag; the photo handler must
auto-tag such a slip as ``source='offer'`` (stake-not-returned) unless the
caption explicitly overrides the source.
"""
from __future__ import annotations

from wca.bot import app
from wca.bot.vision import ExtractedBet


def _free_bet():
    return ExtractedBet(
        match_desc="England vs Croatia", market="Bet Builder",
        selection="England 2UP + Kane SOT", decimal_odds=10.0, stake=1.0,
        potential_returns=9.0, status="won", is_free_bet=True, currency="GBP",
    )


def _normal_bet():
    return ExtractedBet(
        match_desc="Ghana vs Panama", market="Match Odds", selection="Ghana",
        decimal_odds=2.46, stake=10.0, is_free_bet=False, currency="GBP",
    )


def test_free_bet_autotags_offer(monkeypatch):
    monkeypatch.setattr(app, "extract_bets_from_image", lambda *a, **k: [_free_bet()],
                        raising=False)
    # Patch the lazily-imported symbol inside handle_photo.
    import wca.bot.vision as vision
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [_free_bet()])
    app._PENDING_PHOTO_BETS.clear(); app._PENDING_PHOTO_TAGS.clear()
    reply = app.handle_photo(b"img", chat_id="c1", caption=None, api_key="x")
    assert "Free bet detected" in reply
    assert app._PENDING_PHOTO_TAGS["c1"]["source"] == "offer"


def test_explicit_caption_source_wins_over_freebet(monkeypatch):
    import wca.bot.vision as vision
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [_free_bet()])
    app._PENDING_PHOTO_BETS.clear(); app._PENDING_PHOTO_TAGS.clear()
    # User explicitly tags 'punt' -> that wins, no auto-offer override.
    reply = app.handle_photo(b"img", chat_id="c2", caption="punt", api_key="x")
    assert app._PENDING_PHOTO_TAGS["c2"]["source"] == "punt"


def test_normal_bet_defaults_punt(monkeypatch):
    import wca.bot.vision as vision
    monkeypatch.setattr(vision, "extract_bets_from_image", lambda *a, **k: [_normal_bet()])
    app._PENDING_PHOTO_BETS.clear(); app._PENDING_PHOTO_TAGS.clear()
    reply = app.handle_photo(b"img", chat_id="c3", caption=None, api_key="x")
    assert "Free bet detected" not in reply
    assert app._PENDING_PHOTO_TAGS["c3"]["source"] == "punt"
