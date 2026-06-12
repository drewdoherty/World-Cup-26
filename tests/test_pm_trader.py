"""Tests for the canonical Polymarket CLOB client ``wca.pm.trader.ClobTrader``.

Migrated from the former ``tests/test_polymarket_trade.py`` (which targeted the
now-deleted ``wca.data.polymarket_trade``) and re-pointed at the single
canonical module.  The pure signing core (L1 ClobAuth recovery, L2 HMAC,
EIP-712 order signing + the proxy-wallet maker/signer fix) is exercised in
``tests/test_pm_gate.py`` against ``wca.pm.signing``; here we test the trader's
HTTP wiring, account-class detection, guardrails, and order placement.

No network access and no real private key are used: a throwaway Account is
generated locally for signing, and a recording session asserts the dry-run path
never POSTs.
"""

from __future__ import annotations

import base64
import os
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from eth_account import Account

from wca.pm import trader as pmt
from wca.pm.trader import (
    ApiCreds,
    ClobAuthError,
    ClobTrader,
    TradeConfig,
    TradeError,
    compute_order_amounts,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def throwaway_key() -> str:
    """A locally-generated throwaway private key (never a real wallet)."""
    return Account.create().key.hex()


def _resp(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.text = ""
    return m


class RecordingSession:
    """Minimal session stub recording every .request() call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


def _trader(key: str, session=None, **cfg_kw) -> ClobTrader:
    cfg = TradeConfig(**cfg_kw)
    return ClobTrader(private_key=key, config=cfg, session=session)


def _creds_session(extra=None):
    """Session that answers derive-api-key with fixed creds."""
    secret = base64.urlsafe_b64encode(b"unit-test-secret-payload-0001").decode()

    def handler(method, url, kwargs):
        if url.endswith("/auth/derive-api-key"):
            return _resp(
                {"apiKey": "key-123", "secret": secret, "passphrase": "pass-xyz"}
            )
        if extra is not None:
            return extra(method, url, kwargs)
        return _resp({}, status=404)

    return RecordingSession(handler), secret


# ---------------------------------------------------------------------------
# Construction: key handling
# ---------------------------------------------------------------------------


def test_missing_key_raises_clean_error(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    with pytest.raises(TradeError) as ei:
        ClobTrader(private_key=None)
    msg = str(ei.value)
    assert "POLYMARKET_PRIVATE_KEY" in msg
    assert "0x" not in msg  # never leak anything secret-shaped


def test_env_key_fallback(monkeypatch, throwaway_key):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", throwaway_key)
    t = ClobTrader(private_key=None)
    expected = Account.from_key(throwaway_key).address
    assert t.address == expected


def test_invalid_key_no_leak():
    with pytest.raises(TradeError) as ei:
        ClobTrader(private_key="not-a-valid-hex-key")
    assert "invalid" in str(ei.value).lower()
    assert "not-a-valid-hex-key" not in str(ei.value)


def test_bot_style_construction(throwaway_key):
    """Bot/probe call style: positional key + funder/signature_type kwargs."""
    proxy = "0x000000000000000000000000000000000000dEaD"
    t = ClobTrader(throwaway_key, funder=proxy, signature_type=2)
    cls = t.detect_account_class()
    assert cls["signature_type"] == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert cls["signature_type_name"] == "POLY_GNOSIS_SAFE"
    assert t.funder == proxy


# ---------------------------------------------------------------------------
# L1 / L2 header wiring
# ---------------------------------------------------------------------------


def test_l1_headers_shape(throwaway_key):
    t = _trader(throwaway_key)
    h = t.l1_headers(timestamp=1700000000, nonce=0)
    assert set(h) == {"POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_NONCE"}
    assert h["POLY_ADDRESS"] == t.address
    assert h["POLY_SIGNATURE"].startswith("0x")
    assert h["POLY_TIMESTAMP"] == "1700000000"
    assert h["POLY_NONCE"] == "0"


def test_l2_headers_complete(throwaway_key):
    session, secret = _creds_session()
    t = _trader(throwaway_key, session=session)
    h = t.l2_headers("GET", "/data/orders", timestamp=1700000000)
    assert set(h) == {
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    }
    assert h["POLY_API_KEY"] == "key-123"
    assert h["POLY_PASSPHRASE"] == "pass-xyz"
    expected = ClobTrader.build_hmac_signature(
        secret, 1700000000, "GET", "/data/orders", None
    )
    assert h["POLY_SIGNATURE"] == expected


# ---------------------------------------------------------------------------
# Credential derivation caching / fallback
# ---------------------------------------------------------------------------


def test_creds_cached_and_derive_then_create(throwaway_key):
    secret = base64.urlsafe_b64encode(b"create-path-secret-000").decode()
    state = {"derive_calls": 0, "create_calls": 0}

    def handler(method, url, kwargs):
        if url.endswith("/auth/derive-api-key"):
            state["derive_calls"] += 1
            return _resp({}, status=404)  # force fallback
        if url.endswith("/auth/api-key"):
            state["create_calls"] += 1
            return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    c1 = t.derive_or_create_api_creds()
    c2 = t.derive_or_create_api_creds()
    assert c1 is c2  # cached
    assert state["derive_calls"] == 1
    assert state["create_calls"] == 1
    assert c1.api_key == "k"
    # dict alias returns the same material
    d = t.derive_or_create_creds()
    assert d == {"api_key": "k", "api_secret": secret, "api_passphrase": "p"}


def test_creds_failure_raises_clob_auth_error(throwaway_key):
    def handler(method, url, kwargs):
        return _resp({"error": "invalid signature"}, status=401)

    t = _trader(throwaway_key, session=RecordingSession(handler))
    with pytest.raises(ClobAuthError) as ei:
        t.derive_or_create_api_creds()
    assert "L1 auth failed" in str(ei.value)


def test_preseeded_creds_skip_derive(throwaway_key):
    creds = {"api_key": "ak", "api_secret": "as", "api_passphrase": "ap"}
    called = {"n": 0}

    def handler(method, url, kwargs):
        called["n"] += 1
        return _resp({}, status=404)

    t = ClobTrader(
        throwaway_key, creds=creds, session=RecordingSession(handler)
    )
    got = t.derive_or_create_api_creds()
    assert isinstance(got, ApiCreds)
    assert got.api_key == "ak"
    assert called["n"] == 0  # never hit the network


# ---------------------------------------------------------------------------
# Order construction
# ---------------------------------------------------------------------------


def test_build_order_signature_recovers_and_fields(throwaway_key):
    from eth_account.messages import encode_typed_data

    t = _trader(throwaway_key)
    t.detect_account_class()  # offline -> EOA fallback
    # 50 shares @ 0.60.
    order = t.build_order(token_id="987654321", side="BUY", price=0.60, size=50.0)
    assert order["side"] == "BUY"  # string in server form
    assert isinstance(order["salt"], str)
    assert isinstance(order["signatureType"], int)
    assert order["signatureType"] == pmt.SIG_TYPE_EOA
    assert order["signer"] == t.address
    assert order["maker"] == t.address  # EOA self-custody
    assert isinstance(order["tokenId"], str)
    assert isinstance(order["makerAmount"], str)
    assert order["signature"].startswith("0x")

    # Reconstruct EIP-712 with int side/sig to verify the signature recovers.
    order_msg = {
        "salt": int(order["salt"]),
        "maker": order["maker"],
        "signer": order["signer"],
        "taker": order["taker"],
        "tokenId": int(order["tokenId"]),
        "makerAmount": int(order["makerAmount"]),
        "takerAmount": int(order["takerAmount"]),
        "expiration": int(order["expiration"]),
        "nonce": int(order["nonce"]),
        "feeRateBps": int(order["feeRateBps"]),
        "side": pmt.SIDE_BUY,
        "signatureType": order["signatureType"],
    }
    typed = pmt.signing._order_typed_data(order_msg, pmt.signing.CTF_EXCHANGE)
    recovered = Account.recover_message(
        encode_typed_data(full_message=typed),
        signature=bytes.fromhex(order["signature"][2:]),
    )
    assert recovered.lower() == t.address.lower()


def test_compute_order_amounts_buy_and_sell():
    # BUY 50 shares @ 0.60 -> taker=50 shares, maker=30 USDC
    maker, taker = compute_order_amounts(pmt.SIDE_BUY, 0.60, 50.0, "0.01")
    assert taker == 50_000000
    assert maker == 30_000000
    # SELL 50 shares @ 0.60 -> maker=50 shares, taker=30 USDC
    maker2, taker2 = compute_order_amounts(pmt.SIDE_SELL, 0.60, 50.0, "0.01")
    assert maker2 == 50_000000
    assert taker2 == 30_000000


def test_proxy_order_uses_funder_as_maker(throwaway_key):
    # Force POLY_GNOSIS_SAFE; maker must be the funder, signer the EOA.
    proxy = "0x000000000000000000000000000000000000dEaD"
    t = ClobTrader(throwaway_key, funder=proxy, signature_type=pmt.SIG_TYPE_POLY_GNOSIS_SAFE)
    t.detect_account_class()
    order = t.build_order(token_id="1", side="BUY", price=0.5, size=20.0)
    assert order["signatureType"] == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert order["maker"].lower() == proxy.lower()
    assert order["signer"] == t.address  # EOA still signs


def test_build_order_rejects_bad_price(throwaway_key):
    t = _trader(throwaway_key)
    t.detect_account_class()
    with pytest.raises(TradeError):
        t.build_order(token_id="1", side="BUY", price=1.5, size=10.0)


# ---------------------------------------------------------------------------
# Account-class detection
# ---------------------------------------------------------------------------


def test_detect_account_class_eoa_holds_funds(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/value"):
            user = kwargs.get("params", {}).get("user")
            return _resp({"value": 42.0}) if user else _resp({"value": 0.0})
        return _resp({}, status=404)

    t = _trader(throwaway_key, session=RecordingSession(handler))
    cls = t.detect_account_class()
    assert cls["signature_type"] == pmt.SIG_TYPE_EOA
    assert t.funder == t.address


def test_detect_account_class_proxy_holds_funds(throwaway_key):
    proxy = "0x1111111111111111111111111111111111111111"

    def handler(method, url, kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/profile"):
            return _resp({"proxyWallet": proxy})
        if url.endswith("/value"):
            user = (params.get("user") or "").lower()
            if user == proxy.lower():
                return _resp({"value": 99.0})  # proxy funded
            return _resp({"value": 0.0})  # EOA empty
        return _resp({}, status=404)

    t = _trader(throwaway_key, session=RecordingSession(handler))
    cls = t.detect_account_class()
    assert cls["signature_type"] == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert t.funder.lower() == proxy.lower()


def test_detect_account_class_graceful_fallback_offline(throwaway_key):
    def handler(method, url, kwargs):
        raise RuntimeError("offline")

    t = _trader(throwaway_key, session=RecordingSession(handler))
    cls = t.detect_account_class()
    assert cls["signature_type"] == pmt.SIG_TYPE_EOA
    assert t.funder == t.address
    # The fallback is *unproven* — a live order must be refused (see below).
    assert t._account_class_proven is False


def test_live_order_refused_when_account_class_unproven(throwaway_key):
    """Funder safety: a LIVE order with the unproven-EOA fallback is refused.

    No funder / signature_type was supplied and discovery turns up nothing, so
    the maker would default to the empty EOA — never the proxy holding USDC.
    The order must be rejected before any signing or POST.
    """
    secret = base64.urlsafe_b64encode(b"unproven-eoa-secret-0001").decode()

    def handler(method, url, kwargs):
        if url.endswith("/auth/derive-api-key"):
            return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
        if url.endswith("/value") or url.endswith("/profile"):
            return _resp({}, status=404)  # discovery proves nothing
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(
        throwaway_key, session=session, dry_run=False, db_path=_tmp_db()
    )
    with pytest.raises(TradeError) as ei:
        t.place_order("1", 0.5, 10.0, "BUY", market_question="2026 FIFA World Cup")
    msg = str(ei.value).lower()
    assert "unproven" in msg or "funder" in msg
    # No order POST ever happened.
    posts = [c for c in session.calls if c["method"] == "POST" and c["url"].endswith("/order")]
    assert posts == []


def test_dry_run_allowed_when_account_class_unproven(throwaway_key):
    """Dry-run is exempt from the funder-safety guard so offline signing works."""
    session, _ = _creds_session()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=_tmp_db())
    out = t.place_order("1", 0.5, 10.0, "BUY", market_question="2026 FIFA World Cup")
    assert out["dry_run"] is True
    assert out["submitted"] is False


def test_forced_funder_marks_account_class_proven(throwaway_key):
    """Supplying a funder/sig_type proves the class and unblocks live orders."""
    proxy = "0x000000000000000000000000000000000000dEaD"
    t = ClobTrader(
        throwaway_key,
        funder=proxy,
        signature_type=pmt.SIG_TYPE_POLY_GNOSIS_SAFE,
        config=TradeConfig(dry_run=False, db_path=_tmp_db()),
        session=_creds_session()[0],
    )
    t.detect_account_class()
    assert t._account_class_proven is True


def test_resolve_funder_from_env_falls_back_to_proxy_not_eoa():
    """No POLYMARKET_FUNDER -> known proxy + sig type 2, never the empty EOA."""
    funder, sig_type, used_fallback = pmt.resolve_funder_from_env(env={})
    assert funder == pmt.KNOWN_PROXY_FUNDER
    assert sig_type == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert used_fallback is True


def test_resolve_funder_from_env_honours_explicit_funder():
    funder, sig_type, used_fallback = pmt.resolve_funder_from_env(
        env={"POLYMARKET_FUNDER": "0xabc", "POLYMARKET_SIG_TYPE": "2"}
    )
    assert funder == "0xabc"
    assert sig_type == 2
    assert used_fallback is False


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_dry_run_never_posts(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db, max_order_usd=30.0)
    # token, price, size, side  (bot positional order)
    result = t.place_order(
        "1", 0.5, 10.0, "BUY",
        market_question="2026 FIFA World Cup: Brazil to win?",
    )
    assert result["dry_run"] is True
    assert result["submitted"] is False
    assert "request" in result
    assert result["request"]["order"]["side"] == "BUY"
    posts = [c for c in session.calls if c["method"] == "POST" and c["url"].endswith("/order")]
    assert posts == []


def test_dry_run_flag_per_call_overrides_config(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    # config says dry_run False, but the per-call flag forces dry-run.
    t = _trader(throwaway_key, session=session, dry_run=False, db_path=db)
    out = t.place_order("1", 0.5, 10.0, "BUY", dry_run=True, market_question="FIFA WC")
    assert out["dry_run"] is True
    posts = [c for c in session.calls if c["method"] == "POST" and c["url"].endswith("/order")]
    assert posts == []


def test_order_over_per_order_cap_raises(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db, max_order_usd=30.0)
    with pytest.raises(TradeError) as ei:
        # 62 shares * 0.5 = 31 USDC notional > 30 cap
        t.place_order("1", 0.5, 62.0, "BUY", market_question="FIFA World Cup winner")
    assert "per-order cap" in str(ei.value)


def test_non_wc_market_blocked(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db)
    with pytest.raises(TradeError) as ei:
        t.place_order(
            "1", 0.5, 10.0, "BUY",
            market_question="US Presidential Election 2028 winner",
        )
    assert "allowlist" in str(ei.value)


def test_daily_cap_accumulates_and_blocks(throwaway_key):
    secret = base64.urlsafe_b64encode(b"daily-cap-secret-aaa").decode()

    def handler(method, url, kwargs):
        if url.endswith("/auth/derive-api-key"):
            return _resp({"apiKey": "k", "secret": secret, "passphrase": "p"})
        if method == "POST" and url.endswith("/order"):
            return _resp({"orderID": "srv-1", "success": True})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    db = _tmp_db()
    t = _trader(
        throwaway_key,
        session=session,
        dry_run=False,
        db_path=db,
        max_order_usd=30.0,
        max_daily_usd=50.0,
        # Force the account class so the live-order funder-safety guard is
        # satisfied (a real deployment sets POLYMARKET_FUNDER/SIG_TYPE).
        signature_type=pmt.SIG_TYPE_EOA,
    )
    q = "2026 FIFA World Cup champion"
    # 1st: 60 shares * 0.5 = 30 -> ok
    r1 = t.place_order("1", 0.5, 60.0, "BUY", market_question=q)
    assert r1["dry_run"] is False
    assert r1["submitted"] is True
    # 2nd: +30 -> 60 > 50 -> blocked
    with pytest.raises(TradeError) as ei:
        t.place_order("1", 0.5, 60.0, "BUY", market_question=q)
    assert "daily cap" in str(ei.value)

    posts = [c for c in session.calls if c["method"] == "POST" and c["url"].endswith("/order")]
    assert len(posts) == 1


def test_place_order_real_posts_with_signed_envelope(throwaway_key):
    secret = base64.urlsafe_b64encode(b"real-post-secret-bbb").decode()
    captured = {}

    def handler(method, url, kwargs):
        if url.endswith("/auth/derive-api-key"):
            return _resp({"apiKey": "owner-key", "secret": secret, "passphrase": "p"})
        if method == "POST" and url.endswith("/order"):
            captured["body"] = kwargs.get("data")
            captured["headers"] = kwargs.get("headers")
            return _resp({"orderID": "abc", "success": True})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    db = _tmp_db()
    t = _trader(
        throwaway_key,
        session=session,
        dry_run=False,
        db_path=db,
        signature_type=pmt.SIG_TYPE_EOA,  # proven account class for live order
    )
    out = t.place_order("42", 0.4, 25.0, "BUY", market_question="World Cup final")
    assert out["dry_run"] is False
    assert out["submitted"] is True
    # bot reads orderID off the top-level result
    assert out["orderID"] == "abc"
    import json as _json

    env = _json.loads(captured["body"])
    assert env["owner"] == "owner-key"
    assert env["orderType"] == "GTC"
    assert env["order"]["signature"].startswith("0x")
    assert "POLY_API_KEY" in captured["headers"]


def test_place_order_without_question_skips_allowlist(throwaway_key):
    """Bot vets markets before parking, so no question -> no allowlist gate."""
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db)
    out = t.place_order("1", 0.5, 10.0, "BUY")  # no market_question
    assert out["dry_run"] is True


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def test_midpoint_parses(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/midpoint"):
            return _resp({"mid": "0.534"})
        return _resp({}, status=404)

    t = _trader(throwaway_key, session=RecordingSession(handler))
    assert t.midpoint("token-1") == pytest.approx(0.534)


def test_midpoint_no_book_returns_none(throwaway_key):
    def handler(method, url, kwargs):
        return _resp({}, status=404)

    t = _trader(throwaway_key, session=RecordingSession(handler))
    assert t.midpoint("token-1") is None


def test_get_order_book_public_no_auth(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/book"):
            return _resp({"bids": [], "asks": []})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    book = t.get_order_book("tok")
    assert "bids" in book
    assert not any("/auth/" in c["url"] for c in session.calls)


def test_balance_allowance_l2(throwaway_key):
    def extra(method, url, kwargs):
        if url.endswith("/balance-allowance"):
            return _resp({"balance": "100.0", "allowance": "100.0"})
        return _resp({}, status=404)

    session, _ = _creds_session(extra=extra)
    t = _trader(throwaway_key, session=session)
    ba = t.balance_allowance()
    assert ba["balance"] == "100.0"


def test_open_orders_unwraps_data(throwaway_key):
    def extra(method, url, kwargs):
        if url.endswith("/data/orders"):
            return _resp({"data": [{"id": "1"}, {"id": "2"}]})
        return _resp({}, status=404)

    session, _ = _creds_session(extra=extra)
    t = _trader(throwaway_key, session=session)
    orders = t.open_orders()
    assert len(orders) == 2
