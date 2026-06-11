"""Tests for the Polymarket parked-order confirmation gate + signing core.

Two groups:

1. Bot gate (``wca.bot.app``): park / confirm / discard lifecycle with a mocked
   trader, ledger write on ``Y``, dry-run flag respected, ``/pm`` rendering, and
   the existing ``BET-<id>`` acknowledgement left untouched.
2. Signing core (``wca.pm.signing``): the proxy-wallet bug fix — maker = funder,
   signer = EOA, signature recovers the EOA for every account class — plus the
   amount maths, L1 ClobAuth recovery, and the L2 HMAC scheme.  All run with a
   throwaway key, so they pass with or without POLYMARKET_PRIVATE_KEY set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3

import pytest

import wca.bot.app as app
from wca.pm import signing


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------

# Deterministic throwaway key (NOT a real account) — public test vector only.
TEST_KEY = "0x" + "22" * 32
TEST_EOA = signing.address_for_key(TEST_KEY)
PROXY = "0x721A9E426267502d20bcB8afBe9db25a86dCEB76"


class _FakeTrader:
    """Records place_order calls; returns a canned result honouring dry_run."""

    def __init__(self):
        self.calls = []

    def place_order(self, token_id, price, size, side, *, neg_risk=False, dry_run=True):
        self.calls.append(
            dict(token_id=token_id, price=price, size=size, side=side,
                 neg_risk=neg_risk, dry_run=dry_run)
        )
        if dry_run:
            return {"dry_run": True, "submitted": False, "maker": PROXY,
                    "signer": TEST_EOA, "signature_type": 2}
        return {"dry_run": False, "submitted": True, "orderID": "0xLIVEorder",
                "maker": PROXY, "signer": TEST_EOA}


@pytest.fixture(autouse=True)
def _clean_pending():
    """Each test starts with an empty module-level parked-order registry."""
    app._PENDING_ORDERS.clear()
    app._PM_SEQ["n"] = 0
    yield
    app._PENDING_ORDERS.clear()
    app._PM_SEQ["n"] = 0


def _proposal(**kw):
    base = dict(label="Mexico", outcome="Yes", side="BUY", price=0.69,
                size=31.88, token_id="123456789", match_desc="Mexico vs Canada")
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Park / format.
# ---------------------------------------------------------------------------


def test_park_order_returns_incrementing_tokens():
    t1 = app.park_order(_proposal())
    t2 = app.park_order(_proposal(label="Canada"))
    assert t1 == "PM-1" and t2 == "PM-2"
    assert set(app._PENDING_ORDERS) == {1, 2}


def test_format_parked_order_summary():
    tok = app.park_order(_proposal(price=0.69, size=31.88))
    text = app.format_parked_order(tok, app._PENDING_ORDERS[1])
    # $0.69 * 31.88 = 22.0 (ish) notional, both action tokens present.
    assert "$22.00" in text
    assert "Mexico Yes" in text and "BUY" in text
    assert "Y PM-1" in text and "N PM-1" in text


def test_push_parked_order_parks_and_renders():
    msg = app.push_parked_order(_proposal())
    assert "PM-1" in msg and 1 in app._PENDING_ORDERS


# ---------------------------------------------------------------------------
# Confirm (Y) — executes via trader + writes ledger.
# ---------------------------------------------------------------------------


def test_confirm_yes_dry_run_writes_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_DRY_RUN", "1")
    db = str(tmp_path / "t.db")
    app.park_order(_proposal())
    trader = _FakeTrader()

    out = app.handle_confirmation(
        "Y PM-1", db, trader=trader, ts_utc="2026-06-11T18:00:00"
    )
    # Trader was called in dry-run; ledger row written + parked order cleared.
    assert trader.calls and trader.calls[0]["dry_run"] is True
    assert "DRY-RUN" in out and 1 not in app._PENDING_ORDERS

    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT match_desc, selection, platform, stake, notes FROM bets"
    ).fetchall()
    con.close()
    assert len(rows) == 1
    match_desc, selection, platform, stake, notes = rows[0]
    assert platform == "polymarket"
    assert selection == "Yes"
    assert abs(stake - 22.0) < 0.01
    assert "DRY-RUN" in notes and "token=123456789" in notes


def test_confirm_yes_live_passes_dry_run_false(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_DRY_RUN", "0")
    db = str(tmp_path / "t.db")
    app.park_order(_proposal())
    trader = _FakeTrader()

    out = app.handle_confirmation("Y PM-1", db, trader=trader)
    assert trader.calls[0]["dry_run"] is False
    assert "LIVE" in out and "0xLIVEorder" in out

    con = sqlite3.connect(db)
    notes = con.execute("SELECT notes FROM bets").fetchone()[0]
    con.close()
    assert "LIVE" in notes and "order_id=0xLIVEorder" in notes


def test_confirm_yes_forwards_neg_risk_and_side(tmp_path):
    db = str(tmp_path / "t.db")
    app.park_order(_proposal(side="SELL", neg_risk=True, price=0.4, size=10.0))
    trader = _FakeTrader()
    app.handle_confirmation("Y PM-1", db, trader=trader)
    call = trader.calls[0]
    assert call["side"] == "SELL" and call["neg_risk"] is True
    assert call["token_id"] == "123456789"


# ---------------------------------------------------------------------------
# Discard (N) and edge cases.
# ---------------------------------------------------------------------------


def test_confirm_no_discards_without_trader_call(tmp_path):
    db = str(tmp_path / "t.db")
    app.park_order(_proposal())
    trader = _FakeTrader()
    out = app.handle_confirmation("N PM-1", db, trader=trader)
    assert "Discarded" in out and 1 not in app._PENDING_ORDERS
    assert trader.calls == []  # discard never touches the trader
    # No ledger row written.
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM bets").fetchone()[0] if _table_exists(con, "bets") else 0
    con.close()
    assert n == 0


def test_confirm_unknown_pm_token_reports_expired(tmp_path):
    out = app.handle_confirmation("Y PM-99", str(tmp_path / "t.db"), trader=_FakeTrader())
    assert "not a parked order" in out


def test_confirm_pm_bad_number_returns_none():
    assert app.handle_confirmation("Y PM-abc", "x.db", trader=_FakeTrader()) is None


# ---------------------------------------------------------------------------
# Existing BET-<id> path must be unchanged.
# ---------------------------------------------------------------------------


def test_bet_confirmation_path_unchanged():
    assert app.handle_confirmation("Y BET-12", "x.db") == (
        "Bet BET-12 confirmed. (Ledger write pending card-generator wiring.)"
    )
    assert app.handle_confirmation("N BET-7", "x.db") == (
        "Bet BET-7 declined. (Ledger write pending card-generator wiring.)"
    )


def test_non_confirmation_returns_none():
    assert app.handle_confirmation("/summary", "x.db") is None
    assert app.handle_confirmation("hello there", "x.db") is None
    assert app.handle_confirmation("Y", "x.db") is None


# ---------------------------------------------------------------------------
# /pm rendering.
# ---------------------------------------------------------------------------


def test_handle_pm_renders_status_and_parked(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_DRY_RUN", "1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", TEST_KEY)
    app.park_order(_proposal())
    out = app.handle_pm(str(tmp_path / "t.db"))
    assert "Polymarket" in out
    assert "configured" in out and "DRY-RUN" in out
    assert "PM-1" in out and "Mexico Yes" in out


def test_handle_pm_no_orders_and_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    out = app.handle_pm(str(tmp_path / "t.db"))
    assert "NOT configured" in out and "No parked orders" in out


def test_handle_pm_reports_daily_spend(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE pm_order_log (ts_utc TEXT, notional REAL)")
    con.execute("INSERT INTO pm_order_log VALUES (?, ?)", ("2026-06-11T10:00:00", 12.5))
    con.commit()
    con.close()
    out = app._pm_daily_spend(db, day_utc="2026-06-11")
    assert abs(out - 12.5) < 1e-6
    # And /pm surfaces it.
    rendered = app.handle_pm(db)
    assert "Spend today" in rendered


def test_dispatch_routes_pm(tmp_path):
    out = app.dispatch("/pm", str(tmp_path / "t.db"))
    assert "Polymarket" in out


# ---------------------------------------------------------------------------
# Signing core — the proxy-wallet bug fix.
# ---------------------------------------------------------------------------


def _recover_typed(typed, signature):
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    return Account.recover_message(encode_typed_data(full_message=typed), signature=signature)


def test_l1_clob_auth_recovers_eoa():
    sig = signing.sign_clob_auth(TEST_KEY, 1_700_000_000, 0)
    typed = signing._clob_auth_typed_data(TEST_EOA, 1_700_000_000, 0)
    assert _recover_typed(typed, sig).lower() == TEST_EOA.lower()


def test_l1_headers_shape():
    h = signing.build_l1_headers(TEST_KEY, 1_700_000_000, 0)
    assert h["POLY_ADDRESS"] == TEST_EOA
    assert h["POLY_TIMESTAMP"] == "1700000000" and h["POLY_NONCE"] == "0"
    assert h["POLY_SIGNATURE"].startswith("0x")


@pytest.mark.parametrize(
    "sig_type", [signing.SIG_POLY_PROXY, signing.SIG_POLY_GNOSIS_SAFE]
)
def test_proxy_order_maker_is_funder_signer_is_eoa(sig_type):
    """The crux: proxy/safe orders set maker=funder but the EOA signs."""
    args = signing.OrderArgs(token_id="123456789", price=0.69, size=32.0, side="BUY")
    payload = signing.build_signed_order(
        TEST_KEY, args, funder=PROXY, signature_type=sig_type, salt=42
    )
    assert payload["maker"].lower() == PROXY.lower()         # funds move from proxy
    assert payload["signer"].lower() == TEST_EOA.lower()     # EOA holds the key
    assert payload["signatureType"] == sig_type

    # The ECDSA signature must recover the EOA (signer), NOT the proxy (maker).
    order_msg = {
        "salt": 42, "maker": PROXY, "signer": TEST_EOA,
        "taker": signing.ZERO_ADDRESS, "tokenId": 123456789,
        "makerAmount": int(payload["makerAmount"]),
        "takerAmount": int(payload["takerAmount"]),
        "expiration": 0, "nonce": 0, "feeRateBps": 0,
        "side": signing.SIDE_BUY, "signatureType": sig_type,
    }
    typed = signing._order_typed_data(order_msg, signing.CTF_EXCHANGE)
    recovered = _recover_typed(typed, payload["signature"])
    assert recovered.lower() == TEST_EOA.lower()
    assert recovered.lower() != PROXY.lower()


def test_eoa_order_maker_defaults_to_eoa():
    args = signing.OrderArgs(token_id="1", price=0.5, size=10.0, side="BUY")
    payload = signing.build_signed_order(TEST_KEY, args, salt=1)
    assert payload["maker"].lower() == TEST_EOA.lower()
    assert payload["signer"].lower() == TEST_EOA.lower()
    assert payload["signatureType"] == signing.SIG_EOA


def test_order_amount_maths_buy_and_sell():
    # BUY 32 shares @ 0.69 -> spend 22.08 USDC (maker), receive 32 shares (taker)
    buy = signing.order_amounts("BUY", 0.69, 32.0)
    assert buy["side"] == signing.SIDE_BUY
    assert buy["maker_amount"] == 22_080_000  # 22.08 * 1e6
    assert buy["taker_amount"] == 32_000_000  # 32 * 1e6
    # SELL 32 shares @ 0.69 -> give 32 shares (maker), receive 22.08 USDC (taker)
    sell = signing.order_amounts("SELL", 0.69, 32.0)
    assert sell["side"] == signing.SIDE_SELL
    assert sell["maker_amount"] == 32_000_000
    assert sell["taker_amount"] == 22_080_000


def test_order_neg_risk_uses_neg_risk_exchange():
    args = signing.OrderArgs(token_id="1", price=0.5, size=10.0, side="BUY")
    p_std = signing.build_signed_order(TEST_KEY, args, salt=7, neg_risk=False)
    p_neg = signing.build_signed_order(TEST_KEY, args, salt=7, neg_risk=True)
    # Different verifying contract => different signature for identical order.
    assert p_std["signature"] != p_neg["signature"]


def test_invalid_signature_type_rejected():
    args = signing.OrderArgs(token_id="1", price=0.5, size=10.0, side="BUY")
    with pytest.raises(ValueError):
        signing.build_signed_order(TEST_KEY, args, signature_type=9)


def test_to_token_units():
    assert signing.to_token_units(1.0) == 1_000_000
    assert signing.to_token_units(22.08) == 22_080_000
    assert signing.to_token_units(0.0) == 0


# ---------------------------------------------------------------------------
# L2 HMAC scheme.
# ---------------------------------------------------------------------------


def test_l2_hmac_matches_reference_scheme():
    secret = base64.urlsafe_b64encode(b"topsecretkey1234abcd").decode()
    got = signing.build_hmac_signature(secret, 1_700_000_000, "GET", "/balance-allowance", None)
    expected = base64.urlsafe_b64encode(
        hmac.new(
            base64.urlsafe_b64decode(secret),
            b"1700000000GET/balance-allowance",
            hashlib.sha256,
        ).digest()
    ).decode()
    assert got == expected


def test_l2_hmac_includes_body_with_quote_normalisation():
    secret = base64.urlsafe_b64encode(b"k" * 20).decode()
    body = "{'a': 1}"  # python-str(dict) style with single quotes
    got = signing.build_hmac_signature(secret, 1, "POST", "/order", body)
    expected = base64.urlsafe_b64encode(
        hmac.new(
            base64.urlsafe_b64decode(secret),
            b'1POST/order{"a": 1}',  # quotes normalised to double
            hashlib.sha256,
        ).digest()
    ).decode()
    assert got == expected


def test_l2_headers_shape():
    secret = base64.urlsafe_b64encode(b"s" * 20).decode()
    h = signing.build_l2_headers(
        TEST_EOA, "api-key-1", secret, "passphrase-1", 100, "GET", "/data/orders"
    )
    assert h["POLY_ADDRESS"] == TEST_EOA
    assert h["POLY_API_KEY"] == "api-key-1" and h["POLY_PASSPHRASE"] == "passphrase-1"
    assert h["POLY_TIMESTAMP"] == "100"
    assert h["POLY_SIGNATURE"]  # non-empty base64url


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None
