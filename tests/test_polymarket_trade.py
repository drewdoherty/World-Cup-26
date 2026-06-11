"""Tests for wca.data.polymarket_trade (raw Polymarket CLOB client).

No network access and no real private key are used.  A throwaway Account is
generated locally for signing tests, and a mock session asserts that the
dry-run path never POSTs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from wca.data import polymarket_trade as pmt
from wca.data.polymarket_trade import (
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
# Missing key
# ---------------------------------------------------------------------------


def test_missing_key_raises_clean_error(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    with pytest.raises(TradeError) as ei:
        ClobTrader(private_key=None)
    msg = str(ei.value)
    assert "POLYMARKET_PRIVATE_KEY" in msg
    # never leak anything secret-shaped
    assert "0x" not in msg


def test_env_key_fallback(monkeypatch, throwaway_key):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", throwaway_key)
    t = ClobTrader(private_key=None)
    expected = Account.from_key(throwaway_key).address
    assert t.address == expected


def test_invalid_key_no_leak():
    with pytest.raises(TradeError) as ei:
        ClobTrader(private_key="not-a-valid-hex-key")
    assert "invalid" in str(ei.value).lower()
    # must not echo the bad key
    assert "not-a-valid-hex-key" not in str(ei.value)


# ---------------------------------------------------------------------------
# L1: ClobAuth signature recovers to the signer
# ---------------------------------------------------------------------------


def test_l1_signature_recovers_to_address(throwaway_key):
    t = _trader(throwaway_key)
    ts, nonce = 1700000000, 0
    sig = t._sign_clob_auth(ts, nonce)

    # Reconstruct the same typed-data and recover.
    message = {
        "address": t.address,
        "timestamp": str(ts),
        "nonce": nonce,
        "message": pmt._CLOB_AUTH_MESSAGE,
    }
    signable = encode_typed_data(
        domain_data=pmt._CLOB_AUTH_DOMAIN,
        message_types=pmt._CLOB_AUTH_TYPES,
        message_data=message,
    )
    recovered = Account.recover_message(signable, signature=bytes.fromhex(sig[2:]))
    assert recovered.lower() == t.address.lower()


def test_l1_headers_shape(throwaway_key):
    t = _trader(throwaway_key)
    h = t.l1_headers(timestamp=1700000000, nonce=0)
    assert set(h) == {"POLY_ADDRESS", "POLY_SIGNATURE", "POLY_TIMESTAMP", "POLY_NONCE"}
    assert h["POLY_ADDRESS"] == t.address
    assert h["POLY_SIGNATURE"].startswith("0x")
    assert h["POLY_TIMESTAMP"] == "1700000000"
    assert h["POLY_NONCE"] == "0"


# ---------------------------------------------------------------------------
# L2: HMAC matches a hand-computed vector
# ---------------------------------------------------------------------------


def test_l2_hmac_matches_hand_vector(throwaway_key):
    secret = base64.urlsafe_b64encode(b"my-test-secret-0123456789").decode()
    ts, method, path = 1700000000, "GET", "/order"

    # hand computation
    base = base64.urlsafe_b64decode(secret)
    msg = str(ts) + method + path  # no body
    expected = base64.urlsafe_b64encode(
        hmac.new(base, msg.encode(), hashlib.sha256).digest()
    ).decode()

    got = ClobTrader.build_hmac_signature(secret, ts, method, path, None)
    assert got == expected


def test_l2_hmac_body_quote_replacement(throwaway_key):
    secret = base64.urlsafe_b64encode(b"another-secret-value-here-99").decode()
    ts, method, path = 1700000001, "POST", "/order"
    body_single = "{'order': {'a': 1}}"  # single quotes
    base = base64.urlsafe_b64decode(secret)
    # message uses double-quote-normalised body
    msg = str(ts) + method + path + body_single.replace("'", '"')
    expected = base64.urlsafe_b64encode(
        hmac.new(base, msg.encode(), hashlib.sha256).digest()
    ).decode()
    got = ClobTrader.build_hmac_signature(secret, ts, method, path, body_single)
    assert got == expected


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
    # signature must match the standalone vector
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


# ---------------------------------------------------------------------------
# Order struct: deterministic hash + recover
# ---------------------------------------------------------------------------


def _expected_order_signable(t: ClobTrader, order_msg: Dict[str, Any], neg_risk=False):
    domain = {
        "name": pmt._ORDER_DOMAIN_NAME,
        "version": pmt._ORDER_DOMAIN_VERSION,
        "chainId": t.config.chain_id,
        "verifyingContract": t._exchange_address(neg_risk),
    }
    return encode_typed_data(
        domain_data=domain, message_types=pmt._ORDER_TYPES, message_data=order_msg
    )


def test_order_hash_deterministic_and_recovers(throwaway_key):
    t = _trader(throwaway_key)
    t.detect_account_class()  # offline -> EOA fallback

    # Build a deterministic order_msg (fixed salt) to check the hash.
    order_msg = {
        "salt": 12345,
        "maker": t.address,
        "signer": t.address,
        "taker": pmt.ZERO_ADDRESS,
        "tokenId": 987654321,
        "makerAmount": 30000000,
        "takerAmount": 50000000,
        "expiration": 0,
        "nonce": 0,
        "feeRateBps": 0,
        "side": pmt.SIDE_BUY,
        "signatureType": pmt.SIG_TYPE_EOA,
    }
    signable = _expected_order_signable(t, order_msg)
    # Determinism: same struct -> same hash header/body bytes
    again = _expected_order_signable(t, order_msg)
    assert signable.body == again.body
    assert signable.header == again.header

    sig = t._sign_order(order_msg, neg_risk=False)
    recovered = Account.recover_message(signable, signature=bytes.fromhex(sig[2:]))
    assert recovered.lower() == t.address.lower()


def test_build_order_signature_recovers_and_fields(throwaway_key):
    t = _trader(throwaway_key)
    t.detect_account_class()
    order = t.build_order(
        token_id="987654321", side="BUY", price=0.60, size_usd=30.0
    )
    # JSON serialization expectations (server form)
    assert order["side"] == "BUY"  # string
    assert isinstance(order["salt"], int)
    assert isinstance(order["signatureType"], int)
    assert order["signatureType"] == pmt.SIG_TYPE_EOA
    assert order["signer"] == t.address
    assert order["maker"] == t.address  # EOA self-custody
    assert isinstance(order["tokenId"], str)
    assert isinstance(order["makerAmount"], str)
    assert order["signature"].startswith("0x")

    # Reconstruct EIP-712 with int side/sig to verify the signature.
    order_msg = {
        "salt": order["salt"],
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
    signable = _expected_order_signable(t, order_msg)
    recovered = Account.recover_message(
        signable, signature=bytes.fromhex(order["signature"][2:])
    )
    assert recovered.lower() == t.address.lower()


def test_compute_order_amounts_buy_and_sell():
    # BUY 50 shares @ 0.60 -> taker=50 shares, maker=30 USDC
    maker, taker = compute_order_amounts(pmt.SIDE_BUY, 0.60, 50.0, "0.01")
    assert taker == 50_000000  # 50 shares * 1e6
    assert maker == 30_000000  # 30 USDC * 1e6
    # SELL 50 shares @ 0.60 -> maker=50 shares, taker=30 USDC
    maker2, taker2 = compute_order_amounts(pmt.SIDE_SELL, 0.60, 50.0, "0.01")
    assert maker2 == 50_000000
    assert taker2 == 30_000000


def test_proxy_order_uses_funder_as_maker(throwaway_key):
    # Force POLY_GNOSIS_SAFE; maker must be the funder, signer the EOA.
    t = _trader(throwaway_key, signature_type=pmt.SIG_TYPE_POLY_GNOSIS_SAFE)
    proxy = "0x000000000000000000000000000000000000dEaD"
    t._funder = proxy
    t.detect_account_class()  # explicit sig type honoured
    order = t.build_order(token_id="1", side="BUY", price=0.5, size_usd=10.0)
    assert order["signatureType"] == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert order["maker"] == proxy
    assert order["signer"] == t.address  # EOA still signs


# ---------------------------------------------------------------------------
# Account-class detection
# ---------------------------------------------------------------------------


def test_detect_account_class_eoa_holds_funds(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/value"):
            user = kwargs.get("params", {}).get("user")
            # EOA holds value
            return _resp({"value": 42.0}) if user else _resp({"value": 0.0})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    sig = t.detect_account_class()
    assert sig == pmt.SIG_TYPE_EOA
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

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    sig = t.detect_account_class()
    assert sig == pmt.SIG_TYPE_POLY_GNOSIS_SAFE
    assert t.funder.lower() == proxy.lower()


def test_detect_account_class_graceful_fallback_offline(throwaway_key):
    # Network errors everywhere -> fall back to EOA self-custody.
    def handler(method, url, kwargs):
        raise RuntimeError("offline")

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    sig = t.detect_account_class()
    assert sig == pmt.SIG_TYPE_EOA
    assert t.funder == t.address


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
    t = _trader(
        throwaway_key, session=session, dry_run=True, db_path=db, max_order_usd=30.0
    )
    t.detect_account_class()
    result = t.place_order(
        token_id="1",
        side="BUY",
        price=0.5,
        size_usd=10.0,
        market_question="2026 FIFA World Cup: Brazil to win?",
    )
    assert result["dry_run"] is True
    assert "request" in result
    assert result["request"]["order"]["side"] == "BUY"
    # The mock session must NOT have been used to POST an order.
    posts = [c for c in session.calls if c["method"] == "POST" and c["url"].endswith("/order")]
    assert posts == []


def test_order_over_per_order_cap_raises(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(
        throwaway_key, session=session, dry_run=True, db_path=db, max_order_usd=30.0
    )
    t.detect_account_class()
    with pytest.raises(TradeError) as ei:
        t.place_order(
            token_id="1",
            side="BUY",
            price=0.5,
            size_usd=31.0,  # over cap
            market_question="FIFA World Cup winner",
        )
    assert "per-order cap" in str(ei.value)


def test_non_wc_market_blocked(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db)
    t.detect_account_class()
    with pytest.raises(TradeError) as ei:
        t.place_order(
            token_id="1",
            side="BUY",
            price=0.5,
            size_usd=10.0,
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
        dry_run=False,  # real orders so they count toward the cap
        db_path=db,
        max_order_usd=30.0,
        max_daily_usd=50.0,
    )
    t.detect_account_class()

    q = "2026 FIFA World Cup champion"
    # 1st: 30 placed -> ok
    r1 = t.place_order("1", "BUY", 0.5, 30.0, market_question=q)
    assert r1["dry_run"] is False
    # 2nd: +30 -> would be 60 > 50 cap -> blocked
    with pytest.raises(TradeError) as ei:
        t.place_order("1", "BUY", 0.5, 30.0, market_question=q)
    assert "daily cap" in str(ei.value)

    # only the first order was actually POSTed
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
    t = _trader(throwaway_key, session=session, dry_run=False, db_path=db)
    t.detect_account_class()
    out = t.place_order("42", "BUY", 0.4, 10.0, market_question="World Cup final")
    assert out["dry_run"] is False
    assert out["response"]["orderID"] == "abc"
    # envelope owner is the api key, headers carry L2 auth
    import json as _json

    env = _json.loads(captured["body"])
    assert env["owner"] == "owner-key"
    assert env["orderType"] == "GTC"
    assert env["order"]["signature"].startswith("0x")
    assert "POLY_API_KEY" in captured["headers"]


def test_missing_market_question_blocks(throwaway_key):
    session, _ = _creds_session()
    db = _tmp_db()
    t = _trader(throwaway_key, session=session, dry_run=True, db_path=db)
    t.detect_account_class()
    with pytest.raises(TradeError):
        t.place_order("1", "BUY", 0.5, 10.0)  # no market_question


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def test_midpoint_parses(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/midpoint"):
            return _resp({"mid": "0.534"})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    assert t.midpoint("token-1") == pytest.approx(0.534)


def test_get_order_book_public_no_auth(throwaway_key):
    def handler(method, url, kwargs):
        if url.endswith("/book"):
            return _resp({"bids": [], "asks": []})
        return _resp({}, status=404)

    session = RecordingSession(handler)
    t = _trader(throwaway_key, session=session)
    book = t.get_order_book("tok")
    assert "bids" in book
    # the book call must not have triggered an auth derive
    assert not any("/auth/" in c["url"] for c in session.calls)
