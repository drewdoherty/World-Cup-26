"""Tests for paper-book Telegram pings (formatters; send is best-effort no-op)."""

from __future__ import annotations

import pytest

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


def test_format_activity_is_actionable_buy_with_size_and_fixture():
    res = {"n_placed": 1, "candidates": 4, "placed": [
        {"basis": "advance", "fixture": "Belgium", "selection": "Belgium to reach QF",
         "price": 0.32, "model": 0.48, "edge": 0.16, "stake": 40.0}]}
    msg = notify.format_activity(res)
    assert "BUY on A1 Polymarket" in msg          # manual-action framing
    assert "BUY" in msg and "@ 32¢ ask" in msg     # transactable price
    assert "fair 48¢" in msg and "+16%" in msg
    # $40 at 32¢ -> 125 shares, surfaced so the manual fill size is unambiguous.
    assert "125 sh" in msg


def test_live_sizing_quarter_kelly_and_cap():
    # q=0.48, p=0.32 -> f*=(0.16/0.68)=0.2353; ¼-Kelly=0.0588 -> 5.88% of bankroll.
    s = notify.live_sizing(0.48, 0.32, 3000.0)
    assert s["f_star"] == pytest.approx(0.16 / 0.68, rel=1e-3)
    assert s["frac"] == pytest.approx(0.25 * 0.16 / 0.68, rel=1e-3)
    assert s["stake"] == pytest.approx(3000.0 * 0.25 * 0.16 / 0.68, rel=1e-3)
    assert s["capped"] is False
    # A 2% cap binds on this edge.
    c = notify.live_sizing(0.48, 0.32, 3000.0, max_frac=0.02)
    assert c["frac"] == pytest.approx(0.02) and c["capped"] is True
    assert c["stake"] == pytest.approx(60.0)
    # No edge -> zero stake.
    assert notify.live_sizing(0.30, 0.40, 3000.0)["stake"] == 0.0


def test_format_activity_live_bankroll_shows_kelly_stake_in_usd():
    # Bankroll is USD (£3,000 → $3,990 at $1.33=£1).
    res = {"n_placed": 1, "candidates": 4, "placed": [
        {"basis": "advance", "fixture": "Belgium", "selection": "Belgium to reach QF",
         "price": 0.32, "model": 0.48, "edge": 0.16, "stake": 40.0}]}
    msg = notify.format_activity(res, live_bankroll=3990.0)
    assert "¼-Kelly on $3,990 bankroll" in msg        # USD header
    assert "@ 32¢ ask · fair 48¢ · edge +16%" in msg
    assert "stake $235" in msg                          # 3990 × 0.25 × 0.2353 ≈ 235
    assert "5.9% of bankroll" in msg
    assert "$1.33=£1" in msg                            # FX rule stated in footer


def test_format_activity_live_bankroll_flags_hot_and_caps():
    # 12pp edge at 82¢ -> f*=0.667, ¼-Kelly=16.7% -> hot flag.
    res = {"n_placed": 1, "candidates": 1, "placed": [
        {"basis": "prop", "selection": "Pedri 1+ shots", "price": 0.82,
         "model": 0.94, "edge": 0.12, "stake": 31.0}]}
    hot = notify.format_activity(res, live_bankroll=3990.0)
    assert "⚠ hot" in hot
    capped = notify.format_activity(res, live_bankroll=3990.0, max_frac=0.02)
    assert "capped" in capped and "stake $80" in capped


def test_format_activity_book_scale_shrinks_and_flags():
    res = {"n_placed": 1, "candidates": 4, "placed": [
        {"basis": "advance", "fixture": "Belgium", "selection": "Belgium to reach QF",
         "price": 0.32, "model": 0.48, "edge": 0.16, "stake": 40.0}]}
    # 5.88% ¼-Kelly of $3,990 = $235; ×0.5 book scale -> ~$117 and a scaled note.
    msg = notify.format_activity(res, live_bankroll=3990.0, book_scale=0.5)
    assert "book-scaled ×0.50" in msg
    assert "stake $117" in msg and "book-scaled" in msg   # 2% of $3,990


def test_format_exits_explains_what_and_why():
    actions = [
        {"id": 33, "action": "close", "rule": "liquidity_exit", "fixture": "France vs Sweden",
         "selection": "France win (FT 90')", "basis": "FT", "entry_price": 0.53,
         "exit_price": 0.58, "shares_sold": 43.1, "realized_pl": 2.16, "stake_after": 0.0,
         "q": 0.55, "spread": 0.12},
        {"id": 40, "action": "trim", "rule": "over_kelly_trim", "fixture": "Brazil",
         "selection": "Brazil to reach QF", "basis": "advance", "entry_price": 0.40,
         "exit_price": 0.66, "shares_sold": 38.8, "realized_pl": 10.1, "stake_after": 25.0,
         "q": 0.80, "spread": 0.02},
    ]
    msg = notify.format_exits(actions, {"equity": 1501, "roi_pct": -24.9,
                                        "n_open": 32, "realized_balance": 368})
    assert "Mirror on A1 Polymarket" in msg
    assert "EXIT (full)" in msg and "France win (FT 90')" in msg
    assert "SELL 43 sh @ 58¢ (entry 53¢)" in msg and "realised $+2.16" in msg
    assert "liquidity/spread blowout" in msg
    assert "TRIM" in msg and "kept $25 open" in msg   # partial trim keeps size
    assert "equity $1501" in msg


def test_format_exits_empty_is_none():
    assert notify.format_exits([]) is None


def test_chart_caption_has_equity_and_roi():
    cap = notify.chart_caption({"equity": 1501, "roi_pct": -24.9, "realized_pl": -120,
                                "unrealized_pl": 5, "n_open": 32})
    assert "$1501" in cap and "-24.9%" in cap


def test_send_photo_noop_without_credentials(monkeypatch):
    for k in ("WCA_TESTBOOK_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
              "WCA_TESTBOOK_CHAT_ID", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert notify.send_photo(b"\x89PNG...") is False
    assert notify.send_photo(None) is False


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
