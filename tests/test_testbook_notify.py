"""Tests for paper-book Telegram pings (formatters; send is best-effort no-op)."""

from __future__ import annotations

from wca.testbook import notify


def test_format_activity_includes_placements_and_book_state():
    res = {"n_placed": 2, "candidates": 10, "suspicious": 1, "placed": [
        {"basis": "advance", "selection": "Belgium to reach QF", "price": 0.32,
         "model": 0.48, "edge": 0.16, "stake": 40.0},
        {"basis": "exact", "selection": "Exact 1-0", "price": 0.09,
         "model": 0.18, "edge": 0.09, "stake": 40.0},
    ]}
    report = {"equity": 2000.0, "roi_pct": 0.0, "n_open": 29, "realized_balance": 840.0}
    msg = notify.format_activity(res, report)
    assert "placed *2*" in msg and "suspicious" in msg
    assert "Belgium to reach QF" in msg and "32¢" in msg and "+16%" in msg
    assert "equity $2000" in msg


def test_format_activity_quiet_pass_is_none():
    assert notify.format_activity({"n_placed": 0, "candidates": 5, "placed": []}) is None


def test_format_settlement():
    msg = notify.format_settlement({"settled": {"won": 3, "lost": 1, "void": 0}, "pl": 42.5})
    assert "3W/1L/0V" in msg and "+42.50" in msg
    assert notify.format_settlement({"settled": {"won": 0, "lost": 0, "void": 0}, "pl": 0.0}) is None


def test_send_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("WCA_TESTBOOK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("WCA_TESTBOOK_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send("hello") is False          # unconfigured -> no-op, no raise
    assert notify.send(None) is False
