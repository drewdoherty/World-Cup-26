"""Raw Polymarket CLOB trading client (no official SDK).

This module implements signing and order placement against the Polymarket
Central Limit Order Book (CLOB) *without* depending on ``py-clob-client`` or
``py-order-utils``.  Those clients have an open bug for accounts whose funds
sit on a deposit/proxy wallet (the L1 auth binds the API key to the EOA but
the order ``maker``/signature-type fields are mishandled for proxy wallets,
see Polymarket/clob-client-v2#65 and py-clob-client-v2#70).  Because we
control both the signer and the funder, we implement all three signature
classes correctly here.

All cryptography uses ``eth_account`` only (no ``web3``).

Protocol facts encoded below were verified against the official docs and the
reference client source on 2026-06-11:

L1 (create/derive API creds) -- EIP-712 ``ClobAuth``
    domain  = {name: "ClobAuthDomain", version: "1", chainId: 137}
    struct  = ClobAuth(address, timestamp(string), nonce(uint256), message(string))
    message = "This message attests that I control the given wallet"
    docs:   https://docs.polymarket.com/developers/CLOB/authentication
    source: github.com/Polymarket/py-clob-client .../signing/eip712.py
    headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_NONCE
    endpoints: POST /auth/api-key , GET /auth/derive-api-key

L2 (per-request) -- HMAC-SHA256 over (timestamp + method + path + body)
    secret is base64url-decoded; digest is base64url-encoded.
    single quotes in the JSON body are replaced with double quotes so the
    signed message matches the Go/TS clients.
    source: github.com/Polymarket/py-clob-client .../signing/hmac.py
    headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY,
             POLY_PASSPHRASE

Orders -- EIP-712 ``Order`` for the CTF Exchange
    domain  = {name: "Polymarket CTF Exchange", version: "1",
               chainId: 137, verifyingContract: <exchange>}
    struct fields (in order):
        salt(uint256), maker(address), signer(address), taker(address),
        tokenId(uint256), makerAmount(uint256), takerAmount(uint256),
        expiration(uint256), nonce(uint256), feeRateBps(uint256),
        side(uint8), signatureType(uint8)
    side:           BUY=0, SELL=1
    signatureType:  EOA=0, POLY_PROXY=1, POLY_GNOSIS_SAFE=2
    maker  = the FUNDER address (proxy/safe when type 1/2, else the EOA)
    signer = always the EOA derived from the private key
    source: github.com/Polymarket/python-order-utils .../builders/base_builder.py
            (make_domain name="Polymarket CTF Exchange", version="1",
             chainId=str(chain_id), verifyingContract=<exchange>)
    Polygon (137) contract addresses
        regular  exchange = 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
        neg-risk exchange = 0xC5d563A36AE78145C45a50134d48A1215220f80a
        USDC collateral   = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

POST /order body envelope (verified utilities.order_to_json):
    {"order": <signed order dict>, "owner": <api_key>,
     "orderType": "GTC", "postOnly": false}
    In the order dict: side is the STRING "BUY"/"SELL", signatureType and
    salt are ints, the amount/id fields are strings.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (verified -- see module docstring for sources).
# ---------------------------------------------------------------------------

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# side enum (used inside the EIP-712 hash)
SIDE_BUY = 0
SIDE_SELL = 1

# signatureType enum
SIG_TYPE_EOA = 0
SIG_TYPE_POLY_PROXY = 1
SIG_TYPE_POLY_GNOSIS_SAFE = 2

# Polygon mainnet (chainId 137) contracts.
_EXCHANGE_REGULAR = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
_EXCHANGE_NEG_RISK = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

_CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"
_CLOB_AUTH_DOMAIN = {"name": "ClobAuthDomain", "version": "1", "chainId": 137}
_ORDER_DOMAIN_NAME = "Polymarket CTF Exchange"
_ORDER_DOMAIN_VERSION = "1"

# Data API for resolving proxy wallets / balances by address.
_DATA_API_HOST = "https://data-api.polymarket.com"

# USDC has 6 decimals.
_USDC_DECIMALS = 6

# tick-size -> (price, size, amount) decimal places.
# source: py_clob_client order_builder ROUNDING_CONFIG.
_ROUNDING_CONFIG: Dict[str, Tuple[int, int, int]] = {
    "0.1": (1, 2, 3),
    "0.01": (2, 2, 4),
    "0.001": (3, 2, 5),
    "0.0001": (4, 2, 6),
}

# EIP-712 type definitions (eth_account message_types form).
_CLOB_AUTH_TYPES = {
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ]
}

_ORDER_TYPES = {
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
    ]
}


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class TradeError(Exception):
    """Raised for any trading failure.

    Messages are deliberately safe: they never contain the private key, the
    API secret, or any signature material.
    """


# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------


@dataclass
class TradeConfig:
    """Configuration and guardrails for :class:`ClobTrader`.

    Attributes
    ----------
    dry_run:
        When *True* (default) :meth:`ClobTrader.place_order` returns the
        would-be request payload without contacting the network.
    max_order_usd:
        Hard cap on the notional (price * size) of a single order, in USDC.
    max_daily_usd:
        Hard cap on cumulative *placed* notional within a rolling UTC day,
        tracked in the ``pm_order_log`` table.
    allowed_keywords:
        Case-insensitive substrings; the market question (fetched from Gamma)
        must contain at least one of these or the order is blocked.
    host:
        CLOB REST host.
    chain_id:
        EVM chain id (137 = Polygon mainnet).
    signature_type:
        Force a signature class (0/1/2).  ``None`` (default) means autodetect
        via :meth:`ClobTrader.detect_account_class`.
    db_path:
        SQLite database used for the daily-cap order log.
    """

    dry_run: bool = True
    max_order_usd: float = 30.0
    max_daily_usd: float = 100.0
    allowed_keywords: Tuple[str, ...] = ("world cup", "fifa", "wc")
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    signature_type: Optional[int] = None
    db_path: str = "data/wca.db"


# ---------------------------------------------------------------------------
# API credential container.
# ---------------------------------------------------------------------------


@dataclass
class ApiCreds:
    """L2 API credentials returned from create/derive."""

    api_key: str
    api_secret: str
    api_passphrase: str


# ---------------------------------------------------------------------------
# Order-log table (daily cap).
# ---------------------------------------------------------------------------

_DDL_PM_ORDER_LOG = """
CREATE TABLE IF NOT EXISTS pm_order_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT    NOT NULL,
    day_utc     TEXT    NOT NULL,
    token_id    TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    size        REAL    NOT NULL,
    notional_usd REAL   NOT NULL,
    order_id    TEXT,
    dry_run     INTEGER NOT NULL DEFAULT 0
)
"""


# ---------------------------------------------------------------------------
# Rounding helpers (mirror py_clob_client order_builder/helpers.py).
# ---------------------------------------------------------------------------


def _round(value: float, decimals: int, rounding: str) -> Decimal:
    q = Decimal(1).scaleb(-decimals)
    return Decimal(str(value)).quantize(q, rounding=rounding)


def round_down(value: float, decimals: int) -> Decimal:
    return _round(value, decimals, ROUND_DOWN)


def round_normal(value: float, decimals: int) -> Decimal:
    return _round(value, decimals, ROUND_HALF_UP)


def round_up(value: float, decimals: int) -> Decimal:
    return _round(value, decimals, ROUND_UP)


def _to_token_units(amount: Decimal) -> int:
    """Convert a human USDC/share amount to integer base units (6 decimals)."""
    scaled = (amount * (Decimal(10) ** _USDC_DECIMALS)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return int(scaled)


def compute_order_amounts(
    side: int, price: float, size: float, tick_size: str = "0.01"
) -> Tuple[int, int]:
    """Compute integer ``makerAmount`` / ``takerAmount`` from price and size.

    Mirrors ``py_clob_client`` ``get_order_amounts``.  ``size`` is the number
    of outcome shares; ``price`` is the per-share price in USDC (0..1).

    For BUY: maker gives USDC, taker gives shares
        taker = size; maker = size * price
    For SELL: maker gives shares, taker gives USDC
        maker = size; taker = size * price

    Returns
    -------
    (maker_amount, taker_amount) as integer USDC base units (10**6).
    """
    cfg = _ROUNDING_CONFIG.get(tick_size, _ROUNDING_CONFIG["0.01"])
    price_dp, size_dp, amount_dp = cfg
    raw_price = round_normal(price, price_dp)

    if side == SIDE_BUY:
        raw_taker = round_down(size, size_dp)
        raw_maker = (raw_taker * raw_price)
    elif side == SIDE_SELL:
        raw_maker = round_down(size, size_dp)
        raw_taker = (raw_maker * raw_price)
    else:
        raise TradeError("side must be 0 (BUY) or 1 (SELL)")

    # The reference client re-rounds the product to `amount` decimals.
    raw_maker = round_down(float(raw_maker), amount_dp)
    raw_taker = round_down(float(raw_taker), amount_dp)
    return _to_token_units(raw_maker), _to_token_units(raw_taker)


def _utc_now() -> int:
    return int(time.time())


def _utc_day(ts: Optional[int] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else _utc_now(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# The client.
# ---------------------------------------------------------------------------


class ClobTrader:
    """Minimal, correct Polymarket CLOB client.

    Parameters
    ----------
    private_key:
        Hex private key (with or without ``0x``).  If ``None`` the value is
        read from ``POLYMARKET_PRIVATE_KEY`` in the environment.  If neither
        is present, constructing the client raises :class:`TradeError` with a
        safe message.
    config:
        :class:`TradeConfig` instance (guardrails).
    session:
        A ``requests.Session``-like object (injected for tests).  Defaults to
        a new :class:`requests.Session`.
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        config: Optional[TradeConfig] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.config = config or TradeConfig()
        self._session = session if session is not None else requests.Session()

        key = private_key if private_key is not None else os.environ.get(
            "POLYMARKET_PRIVATE_KEY"
        )
        if not key:
            raise TradeError(
                "POLYMARKET_PRIVATE_KEY is not set; cannot construct a signer. "
                "Set it in the environment (.env) to trade."
            )

        # Import lazily so the rest of the module imports even without
        # eth_account installed (tests that don't sign still work).
        try:
            from eth_account import Account  # noqa: F401
        except Exception as exc:  # pragma: no cover - import guard
            raise TradeError("eth_account is required for signing") from exc

        from eth_account import Account

        try:
            self._account = Account.from_key(key)
        except Exception:
            # Never echo the key or the underlying error (may contain key).
            raise TradeError("invalid POLYMARKET_PRIVATE_KEY")

        self._creds: Optional[ApiCreds] = None
        # Resolved by detect_account_class(); default to EOA self-custody.
        self._funder: str = self._account.address
        self._sig_type: int = (
            self.config.signature_type
            if self.config.signature_type is not None
            else SIG_TYPE_EOA
        )

    # -- identity ----------------------------------------------------------

    @property
    def address(self) -> str:
        """The EOA address derived from the private key (the *signer*)."""
        return self._account.address

    @property
    def funder(self) -> str:
        """The address holding USDC (the order *maker*)."""
        return self._funder

    @property
    def signature_type(self) -> int:
        """Resolved signature type (0/1/2)."""
        return self._sig_type

    # -- L1: API credential derivation -------------------------------------

    def _sign_clob_auth(self, timestamp: int, nonce: int) -> str:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        message = {
            "address": self.address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": _CLOB_AUTH_MESSAGE,
        }
        signable = encode_typed_data(
            domain_data=_CLOB_AUTH_DOMAIN,
            message_types=_CLOB_AUTH_TYPES,
            message_data=message,
        )
        signed = Account.sign_message(signable, self._account.key)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    def l1_headers(self, timestamp: Optional[int] = None, nonce: int = 0) -> Dict[str, str]:
        """Build L1 headers (POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE)."""
        ts = timestamp if timestamp is not None else _utc_now()
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": self._sign_clob_auth(ts, nonce),
            "POLY_TIMESTAMP": str(ts),
            "POLY_NONCE": str(nonce),
        }

    def derive_or_create_api_creds(self, nonce: int = 0) -> ApiCreds:
        """Return L2 API credentials, deriving (or creating) them once.

        Tries ``GET /auth/derive-api-key`` first (idempotent: returns the
        existing creds for a given nonce); falls back to
        ``POST /auth/api-key`` to create them.  Cached in-memory.
        """
        if self._creds is not None:
            return self._creds

        headers = self.l1_headers(nonce=nonce)
        creds = self._try_creds_endpoint("GET", "/auth/derive-api-key", headers)
        if creds is None:
            # rebuild headers (fresh timestamp) for the create call
            headers = self.l1_headers(nonce=nonce)
            creds = self._try_creds_endpoint("POST", "/auth/api-key", headers)
        if creds is None:
            raise TradeError("could not derive or create CLOB API credentials")

        self._creds = creds
        return creds

    def _try_creds_endpoint(
        self, method: str, path: str, headers: Dict[str, str]
    ) -> Optional[ApiCreds]:
        url = self.config.host.rstrip("/") + path
        try:
            resp = self._session.request(method, url, headers=headers, timeout=15)
        except Exception as exc:
            raise TradeError("network error contacting auth endpoint") from exc
        if getattr(resp, "status_code", 200) >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        key = data.get("apiKey") or data.get("api_key")
        secret = data.get("secret") or data.get("api_secret")
        passphrase = data.get("passphrase") or data.get("api_passphrase")
        if not (key and secret and passphrase):
            return None
        return ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase)

    # -- L2: per-request HMAC ---------------------------------------------

    @staticmethod
    def build_hmac_signature(
        secret: str, timestamp: int, method: str, path: str, body: Optional[str]
    ) -> str:
        """Compute the base64url L2 HMAC signature.

        Message = ``str(timestamp) + method + path + body`` where ``body`` is
        the JSON string with single quotes replaced by double quotes (to match
        the Go/TS reference clients), or omitted entirely when ``None``.
        """
        message = str(timestamp) + method + path
        if body is not None:
            message += body.replace("'", '"')
        base = base64.urlsafe_b64decode(secret)
        digest = hmac.new(base, message.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8")

    def l2_headers(
        self,
        method: str,
        path: str,
        body: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> Dict[str, str]:
        """Build L2 headers for an authenticated CLOB request."""
        creds = self.derive_or_create_api_creds()
        ts = timestamp if timestamp is not None else _utc_now()
        sig = self.build_hmac_signature(creds.api_secret, ts, method, path, body)
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": str(ts),
            "POLY_API_KEY": creds.api_key,
            "POLY_PASSPHRASE": creds.api_passphrase,
        }

    # -- authenticated GET helpers ----------------------------------------

    def _l2_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        headers = self.l2_headers("GET", path)
        url = self.config.host.rstrip("/") + path
        try:
            resp = self._session.request(
                "GET", url, headers=headers, params=params, timeout=15
            )
        except Exception as exc:
            raise TradeError("network error on GET %s" % path) from exc
        if getattr(resp, "status_code", 200) >= 400:
            raise TradeError("CLOB GET %s failed (status %s)" % (path, resp.status_code))
        return resp.json()

    def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.config.host.rstrip("/") + path
        try:
            resp = self._session.request("GET", url, params=params, timeout=15)
        except Exception as exc:
            raise TradeError("network error on GET %s" % path) from exc
        if getattr(resp, "status_code", 200) >= 400:
            raise TradeError("CLOB GET %s failed (status %s)" % (path, resp.status_code))
        return resp.json()

    def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> Any:
        """GET /balance-allowance (L2-authenticated)."""
        return self._l2_get(
            "/balance-allowance",
            params={"asset_type": asset_type, "signature_type": self._sig_type},
        )

    def get_open_orders(self, market: Optional[str] = None) -> Any:
        """GET /data/orders (L2-authenticated)."""
        params = {"market": market} if market else None
        return self._l2_get("/data/orders", params=params)

    def get_order_book(self, token_id: str) -> Any:
        """GET /book?token_id=... (public)."""
        return self._public_get("/book", params={"token_id": token_id})

    def midpoint(self, token_id: str) -> float:
        """GET /midpoint?token_id=... -> float (public)."""
        data = self._public_get("/midpoint", params={"token_id": token_id})
        mid = data.get("mid") if isinstance(data, dict) else data
        try:
            return float(mid)
        except (TypeError, ValueError):
            raise TradeError("could not parse midpoint response")

    # -- account-class detection ------------------------------------------

    def detect_account_class(self) -> int:
        """Determine whether funds sit on the EOA or a proxy/safe.

        Strategy
        --------
        1. If ``config.signature_type`` is explicitly set, honour it (and set
           the funder to the EOA for type 0, otherwise leave the EOA as a
           placeholder funder unless a proxy is discovered).
        2. Otherwise query the Data API ``/value?user=<addr>`` for the EOA;
           if it holds value, treat as self-custody EOA (signature type 0,
           maker = EOA).
        3. If the EOA holds nothing, look up the Polymarket proxy address for
           this EOA (``/value?user=<proxy>``) and, if it holds value, use
           POLY_GNOSIS_SAFE (type 2, maker = proxy).  Fall back gracefully to
           EOA self-custody if discovery fails (so dry-run still works
           offline).

        Returns
        -------
        int
            The resolved signature type, also stored on the instance.
        """
        if self.config.signature_type is not None:
            self._sig_type = self.config.signature_type
            if self.config.signature_type == SIG_TYPE_EOA:
                self._funder = self.address
            return self._sig_type

        eoa = self.address
        eoa_value = self._data_api_value(eoa)
        if eoa_value is not None and eoa_value > 0:
            self._sig_type = SIG_TYPE_EOA
            self._funder = eoa
            return self._sig_type

        proxy = self._lookup_proxy_address(eoa)
        if proxy:
            proxy_value = self._data_api_value(proxy)
            if proxy_value is not None and proxy_value > 0:
                self._sig_type = SIG_TYPE_POLY_GNOSIS_SAFE
                self._funder = proxy
                return self._sig_type

        # Graceful fallback: assume EOA self-custody.
        self._sig_type = SIG_TYPE_EOA
        self._funder = eoa
        return self._sig_type

    def _data_api_value(self, address: str) -> Optional[float]:
        """Return total position value (USD) for *address*, or None on failure."""
        url = _DATA_API_HOST + "/value"
        try:
            resp = self._session.request(
                "GET", url, params={"user": address}, timeout=15
            )
        except Exception:
            return None
        if getattr(resp, "status_code", 200) >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        # Data API returns either a number, {"value": N}, or [{"value": N}].
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            v = data.get("value")
            return float(v) if v is not None else None
        if isinstance(data, list) and data:
            v = data[0].get("value") if isinstance(data[0], dict) else None
            return float(v) if v is not None else None
        return None

    def _lookup_proxy_address(self, eoa: str) -> Optional[str]:
        """Resolve the Polymarket proxy/safe address for *eoa* via Data API.

        Uses the public profile lookup; returns None if unavailable so callers
        can fall back to EOA self-custody.
        """
        url = _DATA_API_HOST + "/profile"
        try:
            resp = self._session.request(
                "GET", url, params={"address": eoa}, timeout=15
            )
        except Exception:
            return None
        if getattr(resp, "status_code", 200) >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, dict):
            return data.get("proxyWallet") or data.get("proxy_wallet")
        return None

    # -- order construction & signing -------------------------------------

    def _exchange_address(self, neg_risk: bool = False) -> str:
        return _EXCHANGE_NEG_RISK if neg_risk else _EXCHANGE_REGULAR

    def _sign_order(self, order_msg: Dict[str, Any], neg_risk: bool) -> str:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        domain = {
            "name": _ORDER_DOMAIN_NAME,
            "version": _ORDER_DOMAIN_VERSION,
            "chainId": self.config.chain_id,
            "verifyingContract": self._exchange_address(neg_risk),
        }
        signable = encode_typed_data(
            domain_data=domain, message_types=_ORDER_TYPES, message_data=order_msg
        )
        signed = Account.sign_message(signable, self._account.key)
        sig = signed.signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig

    def build_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        fee_rate_bps: int = 0,
        nonce: int = 0,
        expiration: int = 0,
    ) -> Dict[str, Any]:
        """Build and EIP-712-sign a CTF-Exchange order.

        Parameters
        ----------
        token_id:
            The ERC-1155 outcome token id (decimal string).
        side:
            ``"BUY"`` or ``"SELL"`` (case-insensitive).
        price:
            Per-share price in USDC (0 < price < 1).
        size_usd:
            For BUY: the USDC notional to spend (shares = size_usd / price).
            For SELL: the USDC notional to receive (shares = size_usd / price).
            The number of *shares* is derived so that price*shares == size_usd.
        tick_size:
            Market tick size; selects the rounding precision.
        neg_risk:
            Whether the market trades on the neg-risk exchange.
        expiration:
            Unix expiry; 0 = GTC (good-till-cancelled).

        Returns
        -------
        dict
            The signed order object (server JSON form): ``salt`` (int),
            ``maker``/``signer``/``taker`` (addresses), the amount/id fields as
            strings, ``side`` as ``"BUY"``/``"SELL"``, ``signatureType`` (int)
            and ``signature`` (0x-hex).
        """
        side_up = side.strip().upper()
        if side_up == "BUY":
            side_int = SIDE_BUY
        elif side_up == "SELL":
            side_int = SIDE_SELL
        else:
            raise TradeError("side must be 'BUY' or 'SELL'")

        if not (0.0 < price < 1.0):
            raise TradeError("price must be strictly between 0 and 1")
        if size_usd <= 0:
            raise TradeError("size_usd must be positive")

        # Derive share count so that notional == size_usd at this price.
        shares = size_usd / price
        maker_amount, taker_amount = compute_order_amounts(
            side_int, price, shares, tick_size
        )

        # Resolve funder/signer based on signature class.
        if self._sig_type == SIG_TYPE_EOA:
            maker = self.address
        else:
            maker = self._funder

        salt = secrets.randbits(64)
        order_msg = {
            "salt": salt,
            "maker": maker,
            "signer": self.address,
            "taker": ZERO_ADDRESS,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": int(expiration),
            "nonce": int(nonce),
            "feeRateBps": int(fee_rate_bps),
            "side": side_int,
            "signatureType": self._sig_type,
        }
        signature = self._sign_order(order_msg, neg_risk)

        # Server JSON form (verified SignedOrder.dict()): side as string,
        # salt/signatureType as ints, amount/id fields as strings.
        return {
            "salt": salt,
            "maker": maker,
            "signer": self.address,
            "taker": ZERO_ADDRESS,
            "tokenId": str(token_id),
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(int(expiration)),
            "nonce": str(int(nonce)),
            "feeRateBps": str(int(fee_rate_bps)),
            "side": side_up,
            "signatureType": self._sig_type,
            "signature": signature,
        }

    # -- guardrails: market keyword + daily cap ---------------------------

    def _market_question(self, token_id: str, market_question: Optional[str]) -> str:
        """Return the market question text for keyword checking.

        If ``market_question`` is provided we trust it (caller already fetched
        from Gamma).  Otherwise we attempt a Gamma lookup via the existing
        read client; any failure raises so we never trade an unverified
        market.
        """
        if market_question:
            return market_question
        try:
            from wca.data import polymarket as gamma  # noqa
        except Exception as exc:
            raise TradeError("cannot verify market keyword (gamma unavailable)") from exc
        # We cannot reliably map a CLOB token_id to a Gamma question without a
        # network call; require the caller to pass the question explicitly.
        raise TradeError(
            "market_question is required to enforce the keyword allowlist"
        )

    def _check_keyword_allowed(self, question: str) -> None:
        q = question.lower()
        if not any(kw.lower() in q for kw in self.config.allowed_keywords):
            raise TradeError(
                "market is not in the keyword allowlist; refusing to trade"
            )

    def _daily_notional(self) -> float:
        self._ensure_log_table()
        day = _utc_day()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(notional_usd), 0.0) AS s "
                "FROM pm_order_log WHERE day_utc = ? AND dry_run = 0",
                (day,),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def _log_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        notional: float,
        order_id: Optional[str],
        dry_run: bool,
    ) -> None:
        self._ensure_log_table()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pm_order_log "
                "(ts_utc, day_utc, token_id, side, price, size, notional_usd, "
                " order_id, dry_run) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    _utc_day(),
                    str(token_id),
                    side,
                    float(price),
                    float(size),
                    float(notional),
                    order_id,
                    1 if dry_run else 0,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_log_table(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL_PM_ORDER_LOG)

    # -- place / cancel ----------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        market_question: Optional[str] = None,
        tick_size: str = "0.01",
        neg_risk: bool = False,
        order_type: str = "GTC",
    ) -> Dict[str, Any]:
        """Validate guardrails, sign, and (unless dry-run) POST the order.

        Guardrails enforced *before* any network POST:
          1. per-order cap (notional <= ``config.max_order_usd``);
          2. market keyword allowlist (``market_question`` checked);
          3. daily cap (today's logged notional + this order
             <= ``config.max_daily_usd``).

        In dry-run mode the fully-signed request payload is returned and
        **no** HTTP request is made; the attempt is still logged with
        ``dry_run=1`` (which does not count toward the daily cap).

        Returns
        -------
        dict
            ``{"dry_run": True, "request": {...}}`` in dry-run, otherwise the
            parsed server response under ``{"dry_run": False, "response": ...}``.
        """
        # size_usd is the USDC notional of the order.
        notional = float(size_usd)

        # (1) per-order cap
        if notional > self.config.max_order_usd + 1e-9:
            raise TradeError(
                "order notional %.2f exceeds per-order cap %.2f"
                % (notional, self.config.max_order_usd)
            )

        # (2) keyword allowlist
        question = self._market_question(token_id, market_question)
        self._check_keyword_allowed(question)

        # (3) daily cap (real orders only)
        if not self.config.dry_run:
            spent = self._daily_notional()
            if spent + notional > self.config.max_daily_usd + 1e-9:
                raise TradeError(
                    "daily cap exceeded: %.2f already placed, +%.2f would pass %.2f"
                    % (spent, notional, self.config.max_daily_usd)
                )

        signed = self.build_order(
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )

        creds = self.derive_or_create_api_creds()
        envelope = {
            "order": signed,
            "owner": creds.api_key,
            "orderType": order_type,
            "postOnly": False,
        }

        if self.config.dry_run:
            # Never POST. Log the would-be attempt (does not count to cap).
            self._log_order(
                token_id, side.upper(), price, size_usd, notional, None, dry_run=True
            )
            return {"dry_run": True, "request": envelope}

        path = "/order"
        body = json.dumps(envelope, separators=(",", ":"))
        headers = self.l2_headers("POST", path, body=body)
        url = self.config.host.rstrip("/") + path
        try:
            resp = self._session.request(
                "POST", url, headers=headers, data=body, timeout=15
            )
        except Exception as exc:
            raise TradeError("network error posting order") from exc
        if getattr(resp, "status_code", 200) >= 400:
            raise TradeError("order POST rejected (status %s)" % resp.status_code)
        try:
            data = resp.json()
        except Exception:
            raise TradeError("could not parse order response")

        order_id = data.get("orderID") or data.get("orderId") if isinstance(data, dict) else None
        self._log_order(
            token_id, side.upper(), price, size_usd, notional, order_id, dry_run=False
        )
        return {"dry_run": False, "response": data}

    def cancel_order(self, order_id: str) -> Any:
        """DELETE /order with body {"orderID": id} (L2-authenticated)."""
        path = "/order"
        body = json.dumps({"orderID": order_id}, separators=(",", ":"))
        headers = self.l2_headers("DELETE", path, body=body)
        url = self.config.host.rstrip("/") + path
        try:
            resp = self._session.request(
                "DELETE", url, headers=headers, data=body, timeout=15
            )
        except Exception as exc:
            raise TradeError("network error cancelling order") from exc
        if getattr(resp, "status_code", 200) >= 400:
            raise TradeError("cancel rejected (status %s)" % resp.status_code)
        try:
            return resp.json()
        except Exception:
            raise TradeError("could not parse cancel response")
