"""Cryptographic core for Polymarket CLOB auth + order signing.

Everything here is a pure function of its inputs (plus a private key handed in
explicitly): no network, no environment reads, no global mutable state.  That
makes the whole module unit-testable with a throwaway key and lets the trader
layer stay a thin HTTP shell.

Three signing schemes, all verified against the official Polymarket clients
(py-clob-client / python-order-utils) on 2026-06-11:

L1 — ClobAuth (EIP-712 typed data)
    domain  = {name:"ClobAuthDomain", version:"1", chainId:137}
    struct  = ClobAuth(address:address, timestamp:string, nonce:uint256,
                       message:string)
    message = "This message attests that I control the given wallet"
    Used to create/derive the API credentials.  Headers: POLY_ADDRESS,
    POLY_SIGNATURE, POLY_TIMESTAMP, POLY_NONCE.

L2 — per-request HMAC-SHA256
    secret    = base64url-decode(api_secret)
    msg       = str(timestamp) + method + request_path + body
                (body has single quotes -> double quotes, matching the
                 official client's str(dict) JSON quirk; we pass already-JSON
                 bodies so this is a no-op for us)
    signature = base64url(HMAC-SHA256(secret, msg))
    Headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY,
    POLY_PASSPHRASE.

Order — CTF Exchange order (EIP-712 typed data)
    domain = {name:"Polymarket CTF Exchange", version:"1", chainId:137,
              verifyingContract:<exchange>}
    struct field order (load-bearing — EIP-712 hashes are order-sensitive):
        salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
        expiration, nonce, feeRateBps, side, signatureType
    THE BUG FIX: maker = funder (proxy) for sig types 1/2, signer = EOA always.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Constants (Polygon mainnet, chainId 137).  Sourced from
# py_clob_client/config.py::get_contract_config and signing modules.
# ---------------------------------------------------------------------------

POLYGON_CHAIN_ID = 137

CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_COLLATERAL = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CLOB_DOMAIN_NAME = "ClobAuthDomain"
CLOB_DOMAIN_VERSION = "1"
CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

ORDER_DOMAIN_NAME = "Polymarket CTF Exchange"
ORDER_DOMAIN_VERSION = "1"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Signature types (py_order_utils/model/signatures.py).
SIG_EOA = 0
SIG_POLY_PROXY = 1
SIG_POLY_GNOSIS_SAFE = 2

# BUY/SELL encoded as the on-chain uint8 side.
SIDE_BUY = 0
SIDE_SELL = 1

# USDC and CTF shares both use 6 decimals on Polymarket.
_TOKEN_DECIMALS = 6


# ---------------------------------------------------------------------------
# eth-account is imported lazily so the module imports cleanly even if the
# dependency is somehow missing; only the signing functions need it.
# ---------------------------------------------------------------------------


def _account_from_key(private_key: str):
    """Return an ``eth_account`` ``LocalAccount`` for *private_key*.

    Imported lazily and never logged.  Raises a clear error if eth-account is
    not installed.
    """
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise RuntimeError(
            "eth-account is required for Polymarket signing (pip install eth-account)"
        ) from exc
    return Account.from_key(private_key)


def address_for_key(private_key: str) -> str:
    """Return the checksummed EOA address for *private_key* (no logging)."""
    return _account_from_key(private_key).address


# ---------------------------------------------------------------------------
# L1: ClobAuth EIP-712 signature + headers.
# ---------------------------------------------------------------------------


def _clob_auth_typed_data(address: str, timestamp: int, nonce: int) -> Dict[str, Any]:
    """Build the full EIP-712 typed-data document for a ClobAuth message."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "primaryType": "ClobAuth",
        "domain": {
            "name": CLOB_DOMAIN_NAME,
            "version": CLOB_DOMAIN_VERSION,
            "chainId": POLYGON_CHAIN_ID,
        },
        "message": {
            "address": address,
            "timestamp": str(timestamp),
            "nonce": int(nonce),
            "message": CLOB_AUTH_MESSAGE,
        },
    }


def sign_clob_auth(private_key: str, timestamp: int, nonce: int = 0) -> str:
    """Return the 0x-prefixed EIP-712 ClobAuth signature.

    The official client signs ``timestamp`` as a *string* inside the struct
    (type ``string``) even though it is a unix epoch — we replicate that
    exactly so the recovered signer matches what the CLOB expects.
    """
    from eth_account.messages import encode_typed_data

    acct = _account_from_key(private_key)
    typed = _clob_auth_typed_data(acct.address, timestamp, nonce)
    signable = encode_typed_data(full_message=typed)
    signed = acct.sign_message(signable)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def build_l1_headers(private_key: str, timestamp: int, nonce: int = 0) -> Dict[str, str]:
    """Return the POLY_* headers for an L1 (create/derive api-key) request."""
    acct = _account_from_key(private_key)
    return {
        "POLY_ADDRESS": acct.address,
        "POLY_SIGNATURE": sign_clob_auth(private_key, timestamp, nonce),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }


# ---------------------------------------------------------------------------
# L2: per-request HMAC-SHA256 signature + headers.
# ---------------------------------------------------------------------------


def build_hmac_signature(
    secret: str, timestamp: int, method: str, request_path: str, body: Optional[str]
) -> str:
    """base64url(HMAC-SHA256(base64url_decode(secret), msg)).

    ``msg = str(timestamp) + method + request_path + body``.  ``body`` is the
    already-serialized request body string (or ``None``/empty for GETs); we
    apply the same ``'`` -> ``"`` normalisation the official client does so a
    Python-``str(dict)`` body and a real JSON body sign identically.
    """
    base64_secret = base64.urlsafe_b64decode(secret)
    message = str(timestamp) + method + request_path
    if body:
        message += str(body).replace("'", '"')
    digest = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def build_l2_headers(
    address: str,
    api_key: str,
    secret: str,
    passphrase: str,
    timestamp: int,
    method: str,
    request_path: str,
    body: Optional[str] = None,
) -> Dict[str, str]:
    """Return the POLY_* headers for an L2 (credentialed) request.

    ``request_path`` must be the path *without* query string (the official
    client signs the bare endpoint path, e.g. ``/balance-allowance``).
    """
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": build_hmac_signature(
            secret, timestamp, method, request_path, body
        ),
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
    }


# ---------------------------------------------------------------------------
# Order: amount maths + EIP-712 order struct signing.
# ---------------------------------------------------------------------------


def _round_down(x: float, sig_digits: int) -> float:
    f = 10 ** sig_digits
    return math.floor(x * f) / f


def _round_normal(x: float, sig_digits: int) -> float:
    f = 10 ** sig_digits
    return round(x * f) / f


def to_token_units(x: float) -> int:
    """Convert a human amount (USDC or shares) to 6-decimal integer base units."""
    f = (10 ** _TOKEN_DECIMALS) * x
    if abs(f - round(f)) < 1e-3:
        f = _round_normal(f, 0)
    return int(f)


@dataclass
class OrderArgs:
    """Human-friendly inputs for one limit order.

    ``price`` is the per-share price in [0, 1]; ``size`` is the number of CTF
    shares.  For a BUY the maker spends ``price * size`` USDC to receive
    ``size`` shares; for a SELL the maker gives ``size`` shares to receive
    ``price * size`` USDC.
    """

    token_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    fee_rate_bps: int = 0
    nonce: int = 0
    expiration: int = 0  # 0 == GTC (no expiry)


def order_amounts(side: str, price: float, size: float) -> Dict[str, int]:
    """Return ``maker_amount`` / ``taker_amount`` (base units) + side code.

    Mirrors py-clob-client's ``get_order_amounts`` rounding: size rounded down
    to 2 dp, price to 4 dp.
    """
    s = side.strip().upper()
    raw_price = _round_normal(price, 4)
    raw_size = _round_down(size, 2)
    if s == "BUY":
        side_code = SIDE_BUY
        maker = to_token_units(_round_down(raw_size * raw_price, 2))
        taker = to_token_units(raw_size)
    elif s == "SELL":
        side_code = SIDE_SELL
        maker = to_token_units(raw_size)
        taker = to_token_units(_round_down(raw_size * raw_price, 2))
    else:
        raise ValueError("side must be BUY or SELL, got %r" % side)
    return {"side": side_code, "maker_amount": maker, "taker_amount": taker}


def generate_salt() -> int:
    """Cryptographically-random 256-bit-safe salt for order entropy."""
    return secrets.randbelow(2 ** 64)


def _order_typed_data(order: Dict[str, Any], verifying_contract: str) -> Dict[str, Any]:
    """Full EIP-712 typed-data document for a CTF Exchange Order."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "taker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "expiration", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "feeRateBps", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": ORDER_DOMAIN_NAME,
            "version": ORDER_DOMAIN_VERSION,
            "chainId": POLYGON_CHAIN_ID,
            "verifyingContract": verifying_contract,
        },
        "message": order,
    }


def build_signed_order(
    private_key: str,
    args: OrderArgs,
    *,
    funder: Optional[str] = None,
    signature_type: int = SIG_EOA,
    neg_risk: bool = False,
    taker: str = ZERO_ADDRESS,
    salt: Optional[int] = None,
) -> Dict[str, Any]:
    """Build and EIP-712-sign one CTF Exchange order.

    This is where the proxy-wallet bug is fixed.  The on-chain ``maker`` (the
    address whose USDC / shares move) is the *funder*: for an EOA account that
    is the EOA itself, but for a Polymarket proxy (sig type 1) or Gnosis safe
    (sig type 2) it is the proxy/safe wallet.  The ``signer`` — the key that
    actually produces the ECDSA signature — is *always* the EOA.

    Parameters
    ----------
    private_key:
        The EOA private key (never logged).
    args:
        :class:`OrderArgs` describing the order.
    funder:
        The funding wallet (maker).  Defaults to the EOA address when omitted
        (correct for sig type 0; required for types 1/2).
    signature_type:
        0 EOA, 1 POLY_PROXY, 2 POLY_GNOSIS_SAFE.
    neg_risk:
        If *True* sign against the Neg-Risk CTF Exchange verifying contract.
    taker:
        Counterparty; zero address means a public order.
    salt:
        Override the random salt (tests pass a fixed value for determinism).

    Returns
    -------
    dict
        The order payload ready to POST to ``/order`` under ``"order"`` plus
        the ``"signature"`` and the resolved ``"owner"`` semantics.  All
        amount fields are decimal strings as the CLOB expects.
    """
    if signature_type not in (SIG_EOA, SIG_POLY_PROXY, SIG_POLY_GNOSIS_SAFE):
        raise ValueError("invalid signature_type %r" % signature_type)

    acct = _account_from_key(private_key)
    signer = acct.address
    maker = funder if funder else signer

    amounts = order_amounts(args.side, args.price, args.size)
    use_salt = generate_salt() if salt is None else int(salt)

    order_msg: Dict[str, Any] = {
        "salt": int(use_salt),
        "maker": maker,
        "signer": signer,
        "taker": taker,
        "tokenId": int(args.token_id),
        "makerAmount": int(amounts["maker_amount"]),
        "takerAmount": int(amounts["taker_amount"]),
        "expiration": int(args.expiration),
        "nonce": int(args.nonce),
        "feeRateBps": int(args.fee_rate_bps),
        "side": int(amounts["side"]),
        "signatureType": int(signature_type),
    }

    verifying = NEG_RISK_EXCHANGE if neg_risk else CTF_EXCHANGE

    from eth_account.messages import encode_typed_data

    typed = _order_typed_data(order_msg, verifying)
    signable = encode_typed_data(full_message=typed)
    signed = acct.sign_message(signable)
    sig = signed.signature.hex()
    sig = sig if sig.startswith("0x") else "0x" + sig

    # The CLOB POST /order body wants string amounts and the literal side.
    payload = {
        "salt": str(order_msg["salt"]),
        "maker": maker,
        "signer": signer,
        "taker": taker,
        "tokenId": str(order_msg["tokenId"]),
        "makerAmount": str(order_msg["makerAmount"]),
        "takerAmount": str(order_msg["takerAmount"]),
        "expiration": str(order_msg["expiration"]),
        "nonce": str(order_msg["nonce"]),
        "feeRateBps": str(order_msg["feeRateBps"]),
        "side": "BUY" if order_msg["side"] == SIDE_BUY else "SELL",
        "signatureType": int(signature_type),
        "signature": sig,
    }
    return payload


def serialize_body(payload: Dict[str, Any]) -> str:
    """JSON-serialize a request body deterministically for HMAC signing."""
    return json.dumps(payload, separators=(",", ":"))
