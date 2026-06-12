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

Order (V1) — CTF Exchange order (EIP-712 typed data)
    domain = {name:"Polymarket CTF Exchange", version:"1", chainId:137,
              verifyingContract:<exchange>}
    struct field order (load-bearing — EIP-712 hashes are order-sensitive):
        salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
        expiration, nonce, feeRateBps, side, signatureType
    THE BUG FIX: maker = funder (proxy) for sig types 1/2, signer = EOA always.

Order (V2) — CTF Exchange V2 order (EIP-712 typed data); the current default.
    domain = {name:"Polymarket CTF Exchange", version:"2", chainId:137,
              verifyingContract:<v2 exchange, standard or neg-risk>}
    11 signed fields (V1's taker/expiration/nonce/feeRateBps are GONE;
    timestamp/metadata/builder are NEW), field order load-bearing:
        salt, maker, signer, tokenId, makerAmount, takerAmount, side,
        signatureType, timestamp, metadata, builder
    ORDER_TYPEHASH = keccak256(ORDER_TYPE_STRING_V2) =
        0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589
    Verified byte-for-byte against the deployed exchange's on-chain typehash.
    Collateral is pUSD (6 dp); fees are off-chain (no feeRateBps field).  The
    proxy-wallet rule is unchanged: maker = funder for sig types 1/2, signer =
    EOA always.  For sig_type 2 (our account) signing is plain EIP-712 ECDSA.
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

# --- V1 (deprecated for new orders; kept for regression safety) -------------
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_COLLATERAL = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# --- V2 (current default) ---------------------------------------------------
# verifyingContract is chosen by (negRisk); name/version are shared.
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
# pUSD is the V2 order-denomination collateral (6 dp).  USDC.e survives only as
# the CTF position-id derivation collateral, not as what orders are priced in.
PUSD_COLLATERAL = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
FEE_RECEIVER_V2 = "0x115F48dc2a731Aa16251C6d6e1bEFc42f92AccC9"

# Exchange-version selector.  V2 is the default the trader signs against.
EXCHANGE_V1 = 1
EXCHANGE_V2 = 2

CLOB_DOMAIN_NAME = "ClobAuthDomain"
CLOB_DOMAIN_VERSION = "1"
CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

ORDER_DOMAIN_NAME = "Polymarket CTF Exchange"
ORDER_DOMAIN_VERSION = "1"
# V2 shares the name; only the version string and verifyingContract differ.
ORDER_DOMAIN_VERSION_V2 = "2"

# The exact V1 EIP-712 type string (field order + Solidity types).  Kept for
# regression parity with the deprecated V1 path; keccak256 of this is the V1
# ORDER_TYPEHASH (asserted in tests).
ORDER_TYPE_STRING_V1 = (
    "Order(uint256 salt,address maker,address signer,address taker,"
    "uint256 tokenId,uint256 makerAmount,uint256 takerAmount,"
    "uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,"
    "uint8 signatureType)"
)
ORDER_TYPEHASH_V1 = (
    "0xa852566c4e14d00869b6db0220888a9090a13eccdaea03713ff0a3d27bf9767c"
)

# The exact V2 EIP-712 type string (field order + Solidity types).  keccak256 of
# this equals the on-chain ORDER_TYPEHASH below — asserted in tests.
ORDER_TYPE_STRING_V2 = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
ORDER_TYPEHASH_V2 = (
    "0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32

# Signature types (py_order_utils/model/signatures.py).
SIG_EOA = 0
SIG_POLY_PROXY = 1
SIG_POLY_GNOSIS_SAFE = 2
# Deposit-wallet ERC-1271 (Solady ERC-7739 TypedDataSign) — our proxy's class.
# NOT the default; selected explicitly (e.g. via POLYMARKET_SIG_TYPE=3) because
# our trading proxy is an ERC-1967 "DepositWallet", which validates orders via
# ERC-1271 over an ERC-7739-wrapped digest rather than a plain EOA ECDSA sig.
SIG_POLY_1271 = 3

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
    tick_size: str = "0.01"  # market tick; governs amount rounding decimals


# Per-tick rounding decimals, mirroring py-clob-client's ROUNDING_CONFIG:
# tick size -> (price decimals, size decimals, amount decimals). The amount
# decimals MUST be >= price + size decimals so makerAmount/takerAmount stays
# EXACTLY equal to the on-grid price — the CLOB validates that ratio and
# rejects inconsistent pairs with "Invalid order payload" (bug found live
# 2026-06-12: 14.81 sh @ 0.135 floored to $1.99 -> ratio 0.13437 -> 400).
ROUNDING_CONFIG = {
    "0.1": (1, 2, 3),
    "0.01": (2, 2, 4),
    "0.001": (3, 2, 5),
    "0.0001": (4, 2, 6),
}


def order_amounts(
    side: str, price: float, size: float, tick_size: str = "0.01"
) -> Dict[str, int]:
    """Return ``maker_amount`` / ``taker_amount`` (base units) + side code.

    Mirrors py-clob-client's ``get_order_amounts``: price rounded to the
    market's tick decimals, size down to 2 dp, and the USDC amount kept at
    full ``price+size`` precision so the implied price stays on-grid.
    """
    s = side.strip().upper()
    if tick_size not in ROUNDING_CONFIG:
        raise ValueError("unsupported tick size %r" % tick_size)
    price_dp, size_dp, _amount_dp = ROUNDING_CONFIG[tick_size]
    # Exact integer arithmetic: price in tick units x size in hundredth-shares.
    # Float multiply-then-floor loses ticks (0.69 * 32 = 22.079999... -> 22.0799),
    # and any maker/taker pair whose implied price is off-grid is rejected by
    # the CLOB with "Invalid order payload".
    p_int = int(round(price * (10 ** price_dp)))  # price on the tick grid
    s_int = int(math.floor(size * (10 ** size_dp) + 1e-9))  # size floored to 2dp
    scale = 10 ** (_TOKEN_DECIMALS - price_dp - size_dp)
    usdc_units = p_int * s_int * scale
    share_units = s_int * (10 ** (_TOKEN_DECIMALS - size_dp))
    if s == "BUY":
        side_code = SIDE_BUY
        maker, taker = usdc_units, share_units
    elif s == "SELL":
        side_code = SIDE_SELL
        maker, taker = share_units, usdc_units
    else:
        raise ValueError("side must be BUY or SELL, got %r" % side)
    return {"side": side_code, "maker_amount": maker, "taker_amount": taker}


def generate_salt() -> int:
    """Cryptographically-random 256-bit-safe salt for order entropy."""
    return secrets.randbelow(2 ** 64)


def _now_ms() -> int:
    """Current unix time in milliseconds (V2 order ``timestamp`` is ms)."""
    import time

    return int(time.time() * 1000)


def _order_typed_data(order: Dict[str, Any], verifying_contract: str) -> Dict[str, Any]:
    """Full EIP-712 typed-data document for a V1 CTF Exchange Order.

    Kept for regression coverage of the deprecated V1 path; new orders use
    :func:`_order_typed_data_v2`.
    """
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


def _order_typed_data_v2(order: Dict[str, Any], verifying_contract: str) -> Dict[str, Any]:
    """Full EIP-712 typed-data document for a V2 CTF Exchange Order.

    The ``Order`` member list below is the byte-for-byte image of the on-chain
    ``ORDER_TYPEHASH`` (verified: keccak256 of the concatenated type string
    equals :data:`ORDER_TYPEHASH_V2`).  Field order is load-bearing — EIP-712
    struct hashes are order-sensitive — and matches Structs.sol exactly:
    salt, maker, signer, tokenId, makerAmount, takerAmount, side,
    signatureType, timestamp, metadata, builder.  ``signature`` is *not* part
    of the signed struct.  The domain uses version "2" and the V2
    verifyingContract (standard or neg-risk).
    """
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
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": ORDER_DOMAIN_NAME,
            "version": ORDER_DOMAIN_VERSION_V2,
            "chainId": POLYGON_CHAIN_ID,
            "verifyingContract": verifying_contract,
        },
        "message": order,
    }


# ---------------------------------------------------------------------------
# Deposit-wallet ERC-1271 (Solady ERC-7739 TypedDataSign) signing — sig type 3.
#
# A deposit wallet validates an order via ERC-1271.  Per Solady's ERC-7739
# defensive nesting the contract recovers the EOA from a *wrapped* digest:
#
#   digest = keccak(0x1901 || appDomainSeparator || hashStruct(TypedDataSign))
#   TypedDataSign(Order contents,string name,string version,uint256 chainId,
#                 address verifyingContract,bytes salt)... — but Solady's
#   compact form hashes the wrapper as
#   keccak(TYPED_DATA_SIGN_TYPEHASH_for_contents || contentsHash || accountDomainFields)
#
# and the on-chain check re-derives it from the appended trailer.  The wire
# signature the CLOB stores is the ERC-7739 "compact" encoding:
#
#   innerSig(65) || appDomainSeparator(32) || contentsHash(32)
#       || contentsType(string) || uint16(len(contentsType))
#
# where appDomainSeparator is the *order exchange's* EIP-712 domain separator
# (the "contents" / app domain), contentsHash is the order struct hash, and
# contentsType is the EIP-712 type string of the contents ("Order(...)").  The
# trailing uint16 is the byte length of contentsType.
# ---------------------------------------------------------------------------


def _eip712_domain_separator(name: str, version: str, chain_id: int, verifying: str) -> bytes:
    from eth_utils import keccak

    type_hash = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,"
        b"address verifyingContract)"
    )
    addr = verifying[2:] if verifying.lower().startswith("0x") else verifying
    return keccak(
        type_hash
        + keccak(name.encode("utf-8"))
        + keccak(version.encode("utf-8"))
        + chain_id.to_bytes(32, "big")
        + bytes.fromhex(addr.lower().rjust(64, "0"))
    )


def order_struct_hash(
    order: Dict[str, Any],
    *,
    exchange_version: int = EXCHANGE_V2,
) -> bytes:
    """keccak hashStruct of the order ``contents`` (no domain wrapping)."""
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    typed = build_order_typed_data(order, exchange_version=exchange_version)
    signable = encode_typed_data(full_message=typed)
    # signable.body == domainSeparator(32) || hashStruct(32); take the tail.
    return signable.body[32:64] if len(signable.body) >= 64 else keccak(signable.body)


# DepositWallet (account) EIP-712 domain — the wallet whose ERC-1271
# implementation validates the order.  These are the account domain fields the
# ERC-7739 TypedDataSign struct embeds (eip712Domain() on the deposit wallet:
# name "DepositWallet", version "1", salt = 0).
DEPOSIT_WALLET_DOMAIN_NAME = "DepositWallet"
DEPOSIT_WALLET_DOMAIN_VERSION = "1"


def _typed_data_sign_type_string(contents_type: str) -> bytes:
    """ERC-7739 ``TypedDataSign`` type string with the contents type appended.

    Per ERC-7739: ``"TypedDataSign(" + contentsName + " contents," + accountFields
    + ")" + contentsType`` where contentsName is contentsType up to the first
    ``"("`` and the account fields are name/version/chainId/verifyingContract/salt.
    """
    contents_name = contents_type[: contents_type.index("(")]
    return (
        "TypedDataSign(" + contents_name + " contents,"
        "string name,string version,uint256 chainId,"
        "address verifyingContract,bytes32 salt)" + contents_type
    ).encode("utf-8")


def build_erc7739_1271_signature(
    private_key: str,
    order: Dict[str, Any],
    *,
    deposit_wallet: str,
    neg_risk: bool = False,
    exchange_version: int = EXCHANGE_V2,
) -> str:
    """Return the ERC-7739 compact ERC-1271 signature for a deposit-wallet order.

    Layout (hex, 0x-prefixed):
        innerSig(65) || appDomainSeparator(32) || contentsHash(32)
            || contentsType(bytes) || uint16_be(len(contentsType))

    The inner signature is a secp256k1 sig over the ERC-7739 *TypedDataSign*
    nested digest::

        keccak(0x1901 || APP_DOMAIN_SEPARATOR || hashStruct(TypedDataSign))

    where ``APP_DOMAIN_SEPARATOR`` is the CTF Exchange order domain separator and
    the ``TypedDataSign`` struct embeds the order ``contents`` hash plus the
    deposit wallet's *own* EIP-712 domain fields (name "DepositWallet", version
    "1", chainId, verifyingContract = deposit wallet, salt = 0) — this is what
    the deposit wallet's ERC-1271 ``isValidSignature`` re-derives.  The wire
    trailer (appDomainSep || contentsHash || contentsType || uint16 len) lets
    the contract reconstruct that digest.  ``maker == signer == deposit wallet``.
    """
    verifying = verifying_contract_for(exchange_version, neg_risk)
    app_domain_sep = _eip712_domain_separator(
        ORDER_DOMAIN_NAME,
        ORDER_DOMAIN_VERSION_V2 if exchange_version == EXCHANGE_V2 else ORDER_DOMAIN_VERSION,
        POLYGON_CHAIN_ID,
        verifying,
    )
    contents_hash = order_struct_hash(order, exchange_version=exchange_version)
    contents_type = (
        ORDER_TYPE_STRING_V2 if exchange_version == EXCHANGE_V2 else ORDER_TYPE_STRING_V1
    ).encode("utf-8")

    from eth_utils import keccak

    # hashStruct(TypedDataSign) — the account (deposit wallet) domain fields are
    # hashed inline (string name/version -> keccak, salt = bytes32(0)).
    dw = deposit_wallet[2:] if deposit_wallet.lower().startswith("0x") else deposit_wallet
    typed_data_sign_typehash = keccak(_typed_data_sign_type_string(contents_type.decode()))
    typed_data_sign_struct_hash = keccak(
        typed_data_sign_typehash
        + contents_hash
        + keccak(DEPOSIT_WALLET_DOMAIN_NAME.encode("utf-8"))
        + keccak(DEPOSIT_WALLET_DOMAIN_VERSION.encode("utf-8"))
        + POLYGON_CHAIN_ID.to_bytes(32, "big")
        + bytes.fromhex(dw.lower().rjust(64, "0"))
        + (b"\x00" * 32)  # salt
    )

    # The signer signs the ERC-7739 TypedDataSign digest under the APP domain;
    # the wrapper/trailer lets the on-chain ERC-1271 check re-derive it.
    digest = keccak(b"\x19\x01" + app_domain_sep + typed_data_sign_struct_hash)
    acct = _account_from_key(private_key)
    if hasattr(acct, "unsafe_sign_hash"):
        signed = acct.unsafe_sign_hash(digest)
    else:  # pragma: no cover - older eth-account
        signed = acct._sign_hash(digest)
    v = signed.v if signed.v >= 27 else signed.v + 27
    inner = signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([v])

    wrapped = (
        inner
        + app_domain_sep
        + contents_hash
        + contents_type
        + len(contents_type).to_bytes(2, "big")
    )
    return "0x" + wrapped.hex()


def verifying_contract_for(exchange_version: int, neg_risk: bool) -> str:
    """Return the EIP-712 ``verifyingContract`` for ``(version, neg_risk)``.

    The four cases are the exact addresses in the V2 spec / SDK config.  V2 is
    the default the trader signs against; V1 is reachable for regression.
    """
    if exchange_version == EXCHANGE_V2:
        return NEG_RISK_EXCHANGE_V2 if neg_risk else CTF_EXCHANGE_V2
    if exchange_version == EXCHANGE_V1:
        return NEG_RISK_EXCHANGE if neg_risk else CTF_EXCHANGE
    raise ValueError("unknown exchange_version %r (use EXCHANGE_V1/EXCHANGE_V2)" % exchange_version)


def build_order_typed_data(
    order: Dict[str, Any],
    *,
    exchange_version: int = EXCHANGE_V2,
    neg_risk: bool = False,
) -> Dict[str, Any]:
    """Return the full EIP-712 typed-data document for an order.

    Selects the struct (V1's 12-field vs V2's 11-field) *and* the domain
    (version string + verifyingContract) by ``(exchange_version, neg_risk)``.
    ``order`` must already carry the message fields for the chosen version
    (V1: salt/maker/signer/taker/tokenId/makerAmount/takerAmount/expiration/
    nonce/feeRateBps/side/signatureType; V2: salt/maker/signer/tokenId/
    makerAmount/takerAmount/side/signatureType/timestamp/metadata/builder).
    """
    verifying = verifying_contract_for(exchange_version, neg_risk)
    if exchange_version == EXCHANGE_V2:
        return _order_typed_data_v2(order, verifying)
    if exchange_version == EXCHANGE_V1:
        return _order_typed_data(order, verifying)
    raise ValueError("unknown exchange_version %r" % exchange_version)


def build_order_hash(
    order: Dict[str, Any],
    *,
    exchange_version: int = EXCHANGE_V2,
    neg_risk: bool = False,
) -> bytes:
    """Return the EIP-712 digest (``keccak(0x1901 || domainSep || structHash)``).

    This is the exact 32-byte hash the EOA signs and that the deployed exchange
    recovers against (``hashOrder()`` on-chain).  Selects struct + domain by
    ``(exchange_version, neg_risk)`` via :func:`build_order_typed_data`.  Useful
    for parity checks against the contract without producing a signature.
    """
    from eth_account.messages import encode_typed_data

    typed = build_order_typed_data(
        order, exchange_version=exchange_version, neg_risk=neg_risk
    )
    signable = encode_typed_data(full_message=typed)
    # SignableMessage: header (b"\x01") + body (domainSep || structHash).
    return _eip712_digest(signable)


def _eip712_digest(signable: Any) -> bytes:
    """keccak256 of the EIP-712 preimage (0x19 || version || header || body)."""
    from eth_utils import keccak

    return keccak(b"\x19" + signable.version + signable.header + signable.body)


def build_signed_order(
    private_key: str,
    args: OrderArgs,
    *,
    funder: Optional[str] = None,
    signature_type: int = SIG_EOA,
    neg_risk: bool = False,
    exchange_version: int = EXCHANGE_V2,
    taker: str = ZERO_ADDRESS,
    salt: Optional[int] = None,
    timestamp_ms: Optional[int] = None,
    metadata: str = ZERO_BYTES32,
    builder: str = ZERO_BYTES32,
) -> Dict[str, Any]:
    """Build and EIP-712-sign one CTF Exchange order (V2 by default).

    The on-chain ``maker`` (the address whose pUSD / shares move) is the
    *funder*: for an EOA account that is the EOA itself, but for a Polymarket
    proxy (sig type 1) or Gnosis safe (sig type 2) it is the proxy/safe wallet.
    The ``signer`` — the key that actually produces the ECDSA signature — is
    *always* the EOA.  For sig type 2 (our account) signing is plain EIP-712
    ECDSA (the Solady TypedDataSign 1271 wrapper is *only* for sig type 3).

    ``exchange_version`` selects which struct + domain to sign against:

    * :data:`EXCHANGE_V2` (default) — the 11-field struct (salt, maker, signer,
      tokenId, makerAmount, takerAmount, side, signatureType, timestamp,
      metadata, builder) against the version-"2" domain + V2 verifyingContract.
      V1's taker/expiration/nonce/feeRateBps are gone from the signed struct;
      ``taker`` is dropped entirely and ``expiration`` survives only as a
      wire-only default ("0") that is *not* signed.
    * :data:`EXCHANGE_V1` — the deprecated 12-field struct (salt, maker, signer,
      taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps,
      side, signatureType) against the version-"1" domain + V1 verifyingContract.
      Kept reachable for regression safety; not used for new orders.

    Either way the verifyingContract is chosen by ``(exchange_version,
    neg_risk)`` via :func:`verifying_contract_for`.

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
        If *True* sign against the Neg-Risk CTF Exchange verifying contract for
        the chosen ``exchange_version``.
    exchange_version:
        :data:`EXCHANGE_V2` (default) or :data:`EXCHANGE_V1`.
    taker:
        V1 counterparty (signed in V1; wire-only/unused in V2).
    salt:
        Override the random salt (tests pass a fixed value for determinism).
    timestamp_ms:
        Override the V2 order ``timestamp`` (unix **milliseconds**); defaults to
        now.  Ignored by V1.  Tests pass a fixed value for determinism.
    metadata / builder:
        V2 ``bytes32`` attribution fields (default zero); part of the V2 signed
        struct.  Ignored by V1.

    Returns
    -------
    dict
        The order payload ready to nest under ``{"order": ...}`` in the POST
        body, plus the ``"signature"``.  For V2: amount / id fields are decimal
        strings, ``timestamp`` is a millisecond string, ``side`` is the literal
        ``"BUY"``/``"SELL"``, ``expiration`` is the wire-only default ``"0"``,
        and ``metadata``/``builder`` are bytes32 hex.  For V1: the legacy
        12-field shape (taker/expiration/nonce/feeRateBps present).
    """
    if signature_type not in (
        SIG_EOA,
        SIG_POLY_PROXY,
        SIG_POLY_GNOSIS_SAFE,
        SIG_POLY_1271,
    ):
        raise ValueError("invalid signature_type %r" % signature_type)
    if exchange_version not in (EXCHANGE_V1, EXCHANGE_V2):
        raise ValueError(
            "invalid exchange_version %r (use EXCHANGE_V1/EXCHANGE_V2)"
            % exchange_version
        )
    if signature_type == SIG_POLY_1271 and exchange_version != EXCHANGE_V2:
        raise ValueError("sig type 3 (POLY_1271) is only defined for EXCHANGE_V2")

    acct = _account_from_key(private_key)
    signer = acct.address
    maker = funder if funder else signer
    # For the deposit-wallet 1271 path the maker AND signer are the deposit
    # wallet itself (the contract that validates via ERC-1271), not the EOA.
    if signature_type == SIG_POLY_1271:
        if not funder:
            raise ValueError(
                "sig type 3 (POLY_1271) requires funder= (the deposit wallet); "
                "maker == signer == deposit wallet"
            )
        maker = funder
        signer = funder

    amounts = order_amounts(args.side, args.price, args.size, tick_size=args.tick_size)
    use_salt = generate_salt() if salt is None else int(salt)

    if exchange_version == EXCHANGE_V1:
        return _build_signed_order_v1(
            acct,
            args=args,
            maker=maker,
            signer=signer,
            amounts=amounts,
            salt=use_salt,
            signature_type=signature_type,
            neg_risk=neg_risk,
            taker=taker,
        )

    ts_ms = _now_ms() if timestamp_ms is None else int(timestamp_ms)

    # The signed V2 struct — field order load-bearing (matches ORDER_TYPEHASH).
    order_msg: Dict[str, Any] = {
        "salt": int(use_salt),
        "maker": maker,
        "signer": signer,
        "tokenId": int(args.token_id),
        "makerAmount": int(amounts["maker_amount"]),
        "takerAmount": int(amounts["taker_amount"]),
        "side": int(amounts["side"]),
        "signatureType": int(signature_type),
        "timestamp": int(ts_ms),
        "metadata": metadata,
        "builder": builder,
    }

    if signature_type == SIG_POLY_1271:
        # Deposit-wallet: ERC-1271 over an ERC-7739-wrapped digest.
        sig = build_erc7739_1271_signature(
            private_key,
            order_msg,
            deposit_wallet=maker,  # maker == signer == deposit wallet here
            neg_risk=neg_risk,
            exchange_version=EXCHANGE_V2,
        )
    else:
        sig = _sign_typed(
            acct,
            build_order_typed_data(
                order_msg, exchange_version=EXCHANGE_V2, neg_risk=neg_risk
            ),
        )

    # The CLOB POST /order body wants string amounts and the literal side.
    # ``expiration`` is wire-only ("0"), NOT signed; taker/nonce/feeRateBps are
    # gone in V2.
    payload = {
        # NUMERIC on the wire (spec: order_to_json_v2 emits salt as int; the
        # server's parser rejects a string salt with "Invalid order payload").
        "salt": int(order_msg["salt"]),
        "maker": maker,
        "signer": signer,
        "tokenId": str(order_msg["tokenId"]),
        "makerAmount": str(order_msg["makerAmount"]),
        "takerAmount": str(order_msg["takerAmount"]),
        "side": "BUY" if order_msg["side"] == SIDE_BUY else "SELL",
        "signatureType": int(signature_type),
        "timestamp": str(order_msg["timestamp"]),
        "metadata": metadata,
        "builder": builder,
        "expiration": "0",
        "signature": sig,
    }
    return payload


def _sign_typed(acct: Any, typed: Dict[str, Any]) -> str:
    """Sign an EIP-712 typed-data document; return a 0x-prefixed signature."""
    from eth_account.messages import encode_typed_data

    signed = acct.sign_message(encode_typed_data(full_message=typed))
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def _build_signed_order_v1(
    acct: Any,
    *,
    args: OrderArgs,
    maker: str,
    signer: str,
    amounts: Dict[str, int],
    salt: int,
    signature_type: int,
    neg_risk: bool,
    taker: str,
) -> Dict[str, Any]:
    """Build + sign a deprecated **V1** 12-field CTF Exchange order.

    Reachable only via ``exchange_version=EXCHANGE_V1``; kept for regression
    parity with the legacy signer.  Field order is load-bearing and matches the
    V1 ORDER_TYPEHASH.  ``maker`` = funder, ``signer`` = EOA (the proxy fix).
    """
    order_msg: Dict[str, Any] = {
        "salt": int(salt),
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

    sig = _sign_typed(
        acct,
        build_order_typed_data(
            order_msg, exchange_version=EXCHANGE_V1, neg_risk=neg_risk
        ),
    )

    return {
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


def serialize_body(payload: Dict[str, Any]) -> str:
    """JSON-serialize a request body deterministically for HMAC signing."""
    return json.dumps(payload, separators=(",", ":"))
