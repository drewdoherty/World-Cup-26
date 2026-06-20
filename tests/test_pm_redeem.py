"""Tests for the Polymarket unfilled-order redemption core (emulated 24h GTD).

Covers the pure logic in :mod:`wca.pm.redeem`:
* age computation (CLOB ``created_at`` seconds/ms, local-log fallback, unknown),
* selection precedence (single id / all / age threshold; unknown age skipped),
* top-of-book + %-off-market for the relevant fill side (BUY→ask, SELL→bid),
* the Telegram "Unfilled orders" section rendering + the redeem hints.
"""
from __future__ import annotations

from wca.pm import redeem


_NOW = 1_750_000_000.0  # fixed "now" in Unix seconds (deterministic)
_HOUR = 3600.0


def _order(oid, side="BUY", price=0.18, created=None, token="tok1",
           original_size=10.0, size_matched=0.0):
    o = {"id": oid, "side": side, "price": price, "asset_id": token,
         "original_size": original_size, "size_matched": size_matched}
    if created is not None:
        o["created_at"] = created
    return o


# -- age --------------------------------------------------------------------

def test_age_from_created_at_seconds_and_ms():
    o_s = _order("a", created=_NOW - 25 * _HOUR)            # 25h old, seconds
    o_ms = _order("b", created=(_NOW - 2 * _HOUR) * 1000.0)  # 2h old, milliseconds
    assert abs(redeem.order_age_hours(o_s, _NOW) - 25.0) < 1e-6
    assert abs(redeem.order_age_hours(o_ms, _NOW) - 2.0) < 1e-6


def test_age_falls_back_to_local_log_then_unknown():
    o = _order("c")  # no created_at on the order
    assert redeem.order_age_hours(o, _NOW) is None
    log = {"c": _NOW - 30 * _HOUR}
    assert abs(redeem.order_age_hours(o, _NOW, log_epoch_by_id=log) - 30.0) < 1e-6


# -- selection --------------------------------------------------------------

def test_select_by_age_threshold_skips_unknown_age():
    orders = [
        _order("old", created=_NOW - 25 * _HOUR),   # redeem
        _order("fresh", created=_NOW - 1 * _HOUR),  # keep
        _order("nodate"),                            # unknown age -> skip
    ]
    sel = redeem.select_orders_to_redeem(orders, _NOW, max_age_hours=24.0)
    ids = [redeem.order_id_of(o) for o, _ in sel]
    assert ids == ["old"]


def test_select_single_order_id_override():
    orders = [_order("x", created=_NOW - 1 * _HOUR), _order("y", created=_NOW - 1 * _HOUR)]
    sel = redeem.select_orders_to_redeem(orders, _NOW, order_id="y")
    assert [redeem.order_id_of(o) for o, _ in sel] == ["y"]  # fresh, but explicitly targeted


def test_select_redeem_all_takes_everything():
    orders = [_order("x", created=_NOW - 1 * _HOUR), _order("y")]  # one fresh, one undated
    sel = redeem.select_orders_to_redeem(orders, _NOW, redeem_all=True)
    assert {redeem.order_id_of(o) for o, _ in sel} == {"x", "y"}


# -- book + %-off-market ----------------------------------------------------

_BOOK = {"bids": [{"price": "0.15", "size": "100"}, {"price": "0.14", "size": "50"}],
         "asks": [{"price": "0.20", "size": "80"}, {"price": "0.22", "size": "40"}]}


def test_best_bid_ask_picks_inside_market():
    bid, ask = redeem.best_bid_ask(_BOOK)
    assert bid == 0.15 and ask == 0.20


def test_pct_off_market_buy_uses_ask():
    # BUY @ 0.18, best ask 0.20 -> 10% below ask (away from filling).
    off = redeem.pct_off_market("BUY", 0.18, _BOOK)
    assert abs(off - 10.0) < 1e-6


def test_pct_off_market_sell_uses_bid_and_marketable_is_nonpositive():
    # SELL @ 0.15, best bid 0.15 -> 0% (marketable).
    assert abs(redeem.pct_off_market("SELL", 0.15, _BOOK)) < 1e-6
    # BUY @ 0.21 above the 0.20 ask -> negative (would cross / marketable).
    assert redeem.pct_off_market("BUY", 0.21, _BOOK) < 0


def test_pct_off_market_none_when_side_empty():
    assert redeem.pct_off_market("BUY", 0.18, {"bids": [{"price": "0.15"}], "asks": []}) is None


# -- rendering --------------------------------------------------------------

def test_format_unfilled_orders_section():
    orders = [_order("ord1", side="BUY", price=0.18, created=_NOW - 25 * _HOUR,
                     token="tok1", original_size=10, size_matched=3)]
    section = redeem.format_unfilled_orders(
        orders, {"tok1": _BOOK}, _NOW,
        label_by_id={"ord1": "USA vs Australia — Draw"}, max_age_hours=24.0,
    )
    assert "Unfilled PM orders" in section
    assert "USA vs Australia — Draw" in section
    assert "10.0% off ask" in section      # 0.18 vs 0.20 ask
    assert "7.0 sh left" in section        # 10 - 3
    assert "DUE" in section                # 25h ≥ 24h
    assert "`REDEEM ord1`" in section
    assert "REDEEM ALL" in section


def test_format_empty_when_no_orders():
    assert redeem.format_unfilled_orders([], {}, _NOW) == ""


# -- shared IO helpers ------------------------------------------------------

def test_iso_to_epoch_roundtrip_and_bad():
    ep = redeem.iso_to_epoch("2026-06-19T10:00:00")
    assert ep is not None
    # Same instant via a different accepted format parses identically.
    assert redeem.iso_to_epoch("2026-06-19 10:00:00") == ep
    assert redeem.iso_to_epoch("not-a-date") is None
    assert redeem.iso_to_epoch("") is None


def test_log_epoch_by_id_reads_live_rows(tmp_path):
    import sqlite3
    db = str(tmp_path / "wca.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE pm_order_log (id INTEGER, ts_utc TEXT, day_utc TEXT, "
        "token_id TEXT, side TEXT, price REAL, size REAL, notional REAL, "
        "order_id TEXT, dry_run INTEGER)"
    )
    con.execute("INSERT INTO pm_order_log VALUES (1,'2026-06-19T10:00:00','2026-06-19',"
                "'tok','BUY',0.18,10,1.8,'ord-live',0)")
    con.execute("INSERT INTO pm_order_log VALUES (2,'2026-06-19T11:00:00','2026-06-19',"
                "'tok','BUY',0.18,10,1.8,'ord-dry',1)")  # dry_run -> excluded
    con.commit(); con.close()
    m = redeem.log_epoch_by_id(db)
    assert "ord-live" in m and "ord-dry" not in m
    assert m["ord-live"] == redeem.iso_to_epoch("2026-06-19T10:00:00")


def test_log_epoch_by_id_missing_db_is_empty(tmp_path):
    assert redeem.log_epoch_by_id(str(tmp_path / "nope.db")) == {}


# -- bot command wiring -----------------------------------------------------

def test_redeem_is_admin_gated_money_action():
    from wca.bot import app
    assert app._is_money_action("REDEEM ALL") is True
    assert app._is_money_action("redeem 0xdeadbeef") is True
    assert app._is_money_action("redeem") is False   # bare verb is not a money action
    assert app._is_money_action("/card") is False


def test_handle_redeem_usage_without_target():
    from wca.bot import app
    assert "Usage:" in app.handle_redeem("REDEEM")
