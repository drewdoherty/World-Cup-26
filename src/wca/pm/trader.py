"""Polymarket CLOB trading client — the single canonical ``ClobTrader``.

This is the *only* Polymarket order-placement client in the codebase.  It is
built directly on the no-SDK signing core in :mod:`wca.pm.signing` (verified
EIP-712 ClobAuth, HMAC, and CTF-Exchange order signing — including the
proxy-wallet fix where ``maker`` is the funder and ``signer`` is always the
EOA) and adds the network shell plus the trading guardrails:

* ``derive_or_create_creds`` — L1 ClobAuth -> L2 api key / secret / passphrase.
* ``detect_account_class`` — work out whether the funds sit on the EOA or a
  proxy / Gnosis-safe wallet, returning the right ``signature_type`` + funder.
* ``balance_allowance`` / ``open_orders`` / ``midpoint`` / ``get_order_book`` —
  L2 / public reads.
* ``place_order`` — guardrail-checked, correctly-signed order placement that
  honours a per-call (or config) ``dry_run`` flag; in dry-run the order is
  fully signed (proving signing works) but never POSTed.
* ``cancel_order`` — L2 DELETE.

Guardrails (mirroring the former ``wca.data.polymarket_trade``): a per-order
USD cap, a keyword allowlist on the market question, and a rolling-UTC-day
notional cap tracked in the ``pm_order_log`` table.

The private key, api secret, and signature material are NEVER logged.  Heavy
imports (``requests``, ``eth_account``) are deferred so the module parses even
where they are unavailable; ``eth_account`` is only needed once you sign.

Public API (every method the bot / probe / producer call):

    ClobTrader(private_key=None, funder=None, signature_type=None,
               host=CLOB_HOST, creds=None, config=None, session=None)
    .address                       -> str  (EOA, the signer)
    .funder                        -> str  (the order maker)
    .signature_type                -> int  (0 EOA / 1 POLY_PROXY / 2 SAFE)
    .config                        -> TradeConfig
    .derive_or_create_creds(nonce=0)            -> dict  {api_key,api_secret,api_passphrase}
    .derive_or_create_api_creds(nonce=0)        -> ApiCreds  (alias, dataclass form)
    .detect_account_class()        -> dict  {address,signature_type,signature_type_name,funder}
    .balance_allowance(signature_type=None)     -> dict
    .open_orders(market=None)      -> list
    .midpoint(token_id)            -> Optional[float]
    .get_order_book(token_id)      -> dict
    .place_order(token_id, price, size, side, *, neg_risk=False, dry_run=None,
                 market_question=None, tick_size="0.01", order_type="GTC") -> dict
    .cancel_order(order_id)        -> dict
    .build_order(token_id, side, price, size, *, neg_risk=False, salt=None,
                 fee_rate_bps=0, nonce=0, expiration=0) -> dict
    .l1_headers(timestamp=None, nonce=0)        -> dict
    .l2_headers(method, path, body=None, timestamp=None) -> dict
    ClobTrader.build_hmac_signature(secret, ts, method, path, body) -> str
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from wca.pm import signing
from wca.pm import filltelemetry as _filltelemetry

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"
_TIMEOUT = 20

# The Polymarket trading account is the developer DepositWallet, never the
# EOA or the separate deposit address. When POLYMARKET_FUNDER is unset, callers
# must fall back to this known trading proxy rather than the empty EOA.
# This is the single source of truth shared by the bot gate and the probe.
KNOWN_PROXY_FUNDER = "0x86b4C55A4DF1FBea0F325E842434e0a537CAa549"


def resolve_funder_from_env(env: Optional[Dict[str, str]] = None) -> Tuple[str, Optional[int], bool]:
    """Resolve ``(funder, signature_type, used_fallback)`` from the environment.

    Reads ``POLYMARKET_FUNDER`` / ``POLYMARKET_SIG_TYPE``.  When the funder is
    absent it falls back to :data:`KNOWN_PROXY_FUNDER` with signature type 2
    (POLY_1271) — *never* the empty EOA or deposit address — and signals the fallback via
    the third return value so the caller can warn.  Never reads or returns the
    private key.
    """
    src = os.environ if env is None else env
    funder = (src.get("POLYMARKET_FUNDER") or "").strip()
    st = (src.get("POLYMARKET_SIG_TYPE") or "").strip()
    sig_type = int(st) if st else None
    if funder:
        return funder, sig_type, False
    # No explicit funder: fall back to the known trading DepositWallet.
    return KNOWN_PROXY_FUNDER, (sig_type if sig_type is not None else SIG_TYPE_POLY_1271), True

# Re-export signing constants so callers can ``from wca.pm.trader import SIG_*``.
SIG_TYPE_EOA = signing.SIG_EOA
SIG_TYPE_POLY_PROXY = signing.SIG_POLY_PROXY
SIG_TYPE_POLY_GNOSIS_SAFE = signing.SIG_POLY_GNOSIS_SAFE
# Deposit-wallet ERC-1271 (POLY_1271).  Not a default; the user opts in via
# POLYMARKET_SIG_TYPE=3 because the trading proxy is an ERC-1967 DepositWallet.
SIG_TYPE_POLY_1271 = signing.SIG_POLY_1271
SIDE_BUY = signing.SIDE_BUY
SIDE_SELL = signing.SIDE_SELL
ZERO_ADDRESS = signing.ZERO_ADDRESS
EXCHANGE_V1 = signing.EXCHANGE_V1
EXCHANGE_V2 = signing.EXCHANGE_V2


def resolve_exchange_version_from_env(
    env: Optional[Dict[str, str]] = None,
) -> int:
    """Resolve the CTF Exchange version from ``POLYMARKET_EXCHANGE_VERSION``.

    Accepts ``"1"``/``"2"`` (or ``"v1"``/``"v2"``).  Defaults to
    :data:`EXCHANGE_V2` — the current production exchange — when unset or
    unrecognised.  Never raises (a bad value falls back to V2 so a stray env
    string can't break order construction).
    """
    src = os.environ if env is None else env
    raw = (src.get("POLYMARKET_EXCHANGE_VERSION") or "").strip().lower().lstrip("v")
    if raw == "1":
        return EXCHANGE_V1
    return EXCHANGE_V2

_USDC_DECIMALS = 6

# tick-size -> (price, size, amount) decimal places.
# source: py_clob_client order_builder ROUNDING_CONFIG.
_ROUNDING_CONFIG: Dict[str, Tuple[int, int, int]] = {
    "0.1": (1, 2, 3),
    "0.01": (2, 2, 4),
    "0.001": (3, 2, 5),
    "0.0001": (4, 2, 6),
}

_SIG_NAMES = {
    SIG_TYPE_EOA: "EOA",
    SIG_TYPE_POLY_PROXY: "POLY_PROXY",
    SIG_TYPE_POLY_GNOSIS_SAFE: "POLY_GNOSIS_SAFE",
    SIG_TYPE_POLY_1271: "POLY_1271",
}


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class TradeError(Exception):
    """Raised for any trading failure (guardrail / validation / network).

    Messages are deliberately safe: they never contain the private key, the
    API secret, or any signature material.
    """


class ClobAuthError(TradeError):
    """Raised when L1/L2 authentication fails — the signer-address bug detector.

    The message carries the CLOB's own error text so the probe can surface the
    exact failure (an ``invalid signature`` / address-mismatch response is the
    smoking gun for the proxy-wallet signing bug).  Subclasses
    :class:`TradeError` so callers catching either work.
    """


class LiveOrderUnconfirmed(TradeError):
    """A live order was submitted but its outcome could not be confirmed/logged.

    Raised only *after* the order POST is attempted, so the order MAY have
    reached the matching engine (network error / 5xx / unparseable response)
    or — when the server accepted it but the ``pm_order_log`` write failed —
    definitely DID.  Either way the caller must alert an operator to reconcile
    against the wallet on-chain rather than silently treat the order as "not
    placed" and invite a double-spend retry.

    This is the guard for the 2026-06-15 incident, where an Iran-win order
    filled on-chain but left no ``pm_order_log`` and no ledger row: ``place_order``
    raised *after* the POST, skipping both the live ``_log_order`` and the bot's
    ``record_bet``.  Carries the order parameters so the alert can name the
    position.
    """

    def __init__(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        notional: float,
        order_id: Optional[str],
        message: str,
    ) -> None:
        super().__init__(message)
        self.token_id = token_id
        self.side = side
        self.price = price
        self.size = size
        self.notional = notional
        self.order_id = order_id


# ---------------------------------------------------------------------------
# Config + creds containers.
# ---------------------------------------------------------------------------


@dataclass
class TradeConfig:
    """Configuration and guardrails for :class:`ClobTrader`.

    Attributes
    ----------
    dry_run:
        Default dry-run posture.  ``place_order`` returns the would-be signed
        request without contacting the network unless its own ``dry_run``
        argument overrides this.
    max_order_usd:
        Hard cap on the notional (price * size) of a single order, in USDC.
    max_daily_usd:
        Hard cap on cumulative *live* notional within a rolling UTC day,
        tracked in the ``pm_order_log`` table.
    allowed_keywords:
        Case-insensitive substrings; the market question must contain at least
        one of these or the order is blocked.
    host:
        CLOB REST host.
    chain_id:
        EVM chain id (137 = Polygon mainnet).
    signature_type:
        Force a signature class (0/1/2).  ``None`` (default) means autodetect.
    exchange_version:
        Which CTF Exchange to sign against — :data:`EXCHANGE_V2` (default, the
        current production exchange) or :data:`EXCHANGE_V1` (deprecated, kept
        for regression).  Overridable via ``POLYMARKET_EXCHANGE_VERSION``.
    db_path:
        SQLite database used for the daily-cap order log.
    """

    dry_run: bool = True
    # Caps raised 2026-07-02 (user instruction: "remove execution-cap stage
    # raise") to match the full-pool sizing rule: per-order = 4% of the $3,990
    # PM base pool; daily = ~25% of it. Still static fail-closed ceilings —
    # any further change is a human-approved code change.
    max_order_usd: float = 160.0
    max_daily_usd: float = 1000.0
    # Substrings that prove World-Cup provenance.  Single-match Polymarket
    # questions ("Will X win on <date>?") carry no "world cup"/"fifa" keyword,
    # so we also accept the FIFA-World-Cup event-slug prefix ``fifwc`` that the
    # producer folds into the market_question it forwards.
    allowed_keywords: Tuple[str, ...] = ("world cup", "fifa", "wc", "fifwc")
    host: str = CLOB_HOST
    chain_id: int = signing.POLYGON_CHAIN_ID
    signature_type: Optional[int] = None
    exchange_version: int = EXCHANGE_V2
    db_path: str = "data/wca.db"
    # Per-order notional ceiling for a *de-risking* SELL (cash-out). Selling
    # shares we already hold reduces risk, so it is governed by its own (looser)
    # cap rather than the risk-on per-buy cap, and is exempt from the daily BUY
    # budget. Still a hard ceiling so a fat-fingered size can't dump an
    # arbitrarily large position.
    max_cashout_usd_per_order: float = 400.0
    # Observation-only order/fill lifecycle log (wca.pm.filltelemetry). NOT a
    # guardrail: never read to gate, size, or cap anything — purely additive
    # telemetry so an unfilled resting GTC maker order stops being invisible.
    fill_log_path: str = _filltelemetry.DEFAULT_LOG_PATH


@dataclass
class ApiCreds:
    """L2 API credentials returned from create/derive."""

    api_key: str
    api_secret: str
    api_passphrase: str


# ---------------------------------------------------------------------------
# Order-log table (daily cap).  The ``notional`` column is read by the bot's
# ``/pm`` daily-spend reporter (wca.bot.app._pm_daily_spend).
# ---------------------------------------------------------------------------

_DDL_PM_ORDER_LOG = """
CREATE TABLE IF NOT EXISTS pm_order_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT    NOT NULL,
    day_utc     TEXT    NOT NULL,
    token_id    TEXT    NOT NULL,
    market      TEXT,
    side        TEXT    NOT NULL,
    price       REAL    NOT NULL,
    size        REAL    NOT NULL,
    notional    REAL    NOT NULL,
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
        raw_maker = raw_taker * raw_price
    elif side == SIDE_SELL:
        raw_maker = round_down(size, size_dp)
        raw_taker = raw_maker * raw_price
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


def _short(text: Optional[str], n: int = 240) -> str:
    """Trim an error body for safe display (signatures never reach here)."""
    if not text:
        return ""
    s = str(text).strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------------------
# The client.
# ---------------------------------------------------------------------------


class ClobTrader:
    """Sign and submit Polymarket CLOB requests for any account class.

    Construction supports the two call styles in the codebase:

    * Bot / probe::

          ClobTrader(private_key, funder=..., signature_type=..., host=...)

    * Config / injected-session (tests, producer)::

          ClobTrader(private_key, config=TradeConfig(...), session=...)

    Either way the resolved guardrails live on ``self.config``.  ``funder`` /
    ``signature_type`` / ``host`` passed directly override the corresponding
    config fields.

    Parameters
    ----------
    private_key:
        The EOA private key (with or without ``0x``).  If ``None`` it is read
        from ``POLYMARKET_PRIVATE_KEY`` in the environment; if neither is
        present, construction raises :class:`TradeError` with a safe message.
        Never logged.
    funder:
        The funding wallet (proxy / safe) when funds do not sit on the EOA.
        Drives ``maker`` for sig types 1/2.  Omit for a bare EOA.
    signature_type:
        Force a signature type (0/1/2).  ``None`` -> autodetect from where the
        USDC balance lives.
    host:
        CLOB base URL (overridable for tests).
    creds:
        Pre-derived ``{"api_key","api_secret","api_passphrase"}`` to skip the
        L1 derive round-trip.
    config:
        :class:`TradeConfig` instance (guardrails).  A default is created when
        omitted.
    session:
        A ``requests.Session``-like object (injected for tests).  Defaults to a
        lazily-created :class:`requests.Session`.
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        signature_type: Optional[int] = None,
        host: Optional[str] = None,
        creds: Optional[Dict[str, str]] = None,
        config: Optional[TradeConfig] = None,
        session: Optional[Any] = None,
        exchange_version: Optional[int] = None,
    ) -> None:
        self.config = config or TradeConfig()
        if host is not None:
            self.config.host = host
        if signature_type is not None:
            self.config.signature_type = signature_type
        # Exchange-version precedence: explicit arg > explicit config (only when
        # a config was supplied) > POLYMARKET_EXCHANGE_VERSION env > V2 default.
        if exchange_version is not None:
            self.config.exchange_version = exchange_version
        elif config is None:
            self.config.exchange_version = resolve_exchange_version_from_env()

        key = private_key if private_key is not None else os.environ.get(
            "POLYMARKET_PRIVATE_KEY"
        )
        if not key:
            raise TradeError(
                "POLYMARKET_PRIVATE_KEY is not set; cannot construct a signer. "
                "Set it in the environment (.env) to trade."
            )
        try:
            self.address = signing.address_for_key(key)
        except Exception:
            # Never echo the key or the underlying error (may contain key).
            raise TradeError("invalid POLYMARKET_PRIVATE_KEY")
        self._key = key

        self._session = session
        self._forced_funder = funder
        self._forced_sig_type = self.config.signature_type

        # Cached / resolved state.
        self._creds: Optional[ApiCreds] = (
            ApiCreds(
                api_key=creds.get("api_key", ""),
                api_secret=creds.get("api_secret", ""),
                api_passphrase=creds.get("api_passphrase", ""),
            )
            if creds
            else None
        )
        # Resolved after detect_account_class(); sensible EOA defaults.
        self._sig_type: int = (
            self._forced_sig_type if self._forced_sig_type is not None else SIG_TYPE_EOA
        )
        self._funder: str = funder or self.address
        # True once the account class is *proven* — either forced by the caller
        # (funder/sig_type), or because the Data API showed value at the EOA or a
        # discovered proxy.  When it stays False the EOA was used only as a
        # graceful offline fallback; placing a *live* order in that state is
        # refused (the funder is almost certainly the empty EOA, not the proxy
        # that actually holds USDC).  See place_order's live-order guard.
        self._account_class_proven: bool = (
            self._forced_sig_type is not None or self._forced_funder is not None
        )

    # ------------------------------------------------------------------ host
    @property
    def host(self) -> str:
        return self.config.host.rstrip("/")

    @property
    def funder(self) -> str:
        """The address holding USDC (the order *maker*)."""
        return self._funder

    @property
    def signature_type(self) -> int:
        """Resolved signature type (0/1/2)."""
        return self._sig_type

    @property
    def exchange_version(self) -> int:
        """CTF Exchange version orders are signed against (V2 default)."""
        return self.config.exchange_version

    # ------------------------------------------------------------------ HTTP
    def _sess(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[str] = None,
    ):
        url = self.host + path
        try:
            return self._sess().request(
                method,
                url,
                headers=headers,
                params=params,
                data=body,
                timeout=_TIMEOUT,
            )
        except Exception as exc:
            raise TradeError("network error on %s %s" % (method, path)) from exc

    @staticmethod
    def _now() -> int:
        return _utc_now()

    # ------------------------------------------------------------ L1: creds
    def l1_headers(
        self, timestamp: Optional[int] = None, nonce: int = 0
    ) -> Dict[str, str]:
        """Build L1 headers (POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE)."""
        ts = timestamp if timestamp is not None else self._now()
        return signing.build_l1_headers(self._key, ts, nonce)

    def derive_or_create_creds(self, nonce: int = 0) -> Dict[str, str]:
        """Derive (or create) the L2 API credentials via an L1 ClobAuth sig.

        Tries ``GET /auth/derive-api-key`` first (idempotent for an existing
        key) then falls back to ``POST /auth/api-key``.  Raises
        :class:`ClobAuthError` carrying the CLOB error text on failure — this
        is the auth-failure signal the probe treats as the bug detector.

        Returns a plain dict ``{api_key, api_secret, api_passphrase}``; see
        :meth:`derive_or_create_api_creds` for the :class:`ApiCreds` form.
        """
        creds = self.derive_or_create_api_creds(nonce=nonce)
        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
        }

    def derive_or_create_api_creds(self, nonce: int = 0) -> ApiCreds:
        """Return L2 API credentials as an :class:`ApiCreds` (cached)."""
        if self._creds is not None:
            return self._creds

        headers = self.l1_headers(nonce=nonce)
        creds, err = self._try_creds_endpoint("GET", "/auth/derive-api-key", headers)
        if creds is None:
            headers = self.l1_headers(nonce=nonce)  # fresh timestamp
            creds, err2 = self._try_creds_endpoint("POST", "/auth/api-key", headers)
            err = "derive=%s | create=%s" % (err, err2)
        if creds is None:
            raise ClobAuthError("L1 auth failed: %s" % err)

        self._creds = creds
        return creds

    def _try_creds_endpoint(
        self, method: str, path: str, headers: Dict[str, str]
    ) -> Tuple[Optional[ApiCreds], str]:
        resp = self._request(method, path, headers=headers)
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            return None, "%s %s" % (status, _short(getattr(resp, "text", "")))
        try:
            data = resp.json()
        except Exception:
            return None, "%s (unparseable body)" % status
        key = data.get("apiKey") or data.get("api_key")
        secret = data.get("secret") or data.get("api_secret")
        passphrase = data.get("passphrase") or data.get("api_passphrase")
        if not (key and secret and passphrase):
            return None, "%s (no usable creds)" % status
        return ApiCreds(api_key=key, api_secret=secret, api_passphrase=passphrase), ""

    # -- L2: per-request HMAC ---------------------------------------------

    @staticmethod
    def build_hmac_signature(
        secret: str, timestamp: int, method: str, path: str, body: Optional[str]
    ) -> str:
        """Compute the base64url L2 HMAC signature (delegates to signing)."""
        return signing.build_hmac_signature(secret, timestamp, method, path, body)

    def l2_headers(
        self,
        method: str,
        path: str,
        body: Optional[str] = None,
        timestamp: Optional[int] = None,
    ) -> Dict[str, str]:
        """Build L2 headers for an authenticated CLOB request."""
        creds = self.derive_or_create_api_creds()
        ts = timestamp if timestamp is not None else self._now()
        return signing.build_l2_headers(
            self.address,
            creds.api_key,
            creds.api_secret,
            creds.api_passphrase,
            ts,
            method,
            path,
            body,
        )

    # -- authenticated / public GET helpers -------------------------------

    def _l2_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        headers = self.l2_headers("GET", path)
        resp = self._request("GET", path, headers=headers, params=params)
        if getattr(resp, "status_code", 200) >= 400:
            raise ClobAuthError(
                "CLOB GET %s failed: %s %s"
                % (path, resp.status_code, _short(getattr(resp, "text", "")))
            )
        return resp.json()

    def _public_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = self._request("GET", path, params=params)
        if getattr(resp, "status_code", 200) >= 400:
            raise TradeError(
                "CLOB GET %s failed (status %s)" % (path, resp.status_code)
            )
        return resp.json()

    def balance_allowance(self, signature_type: Optional[int] = None) -> Dict[str, Any]:
        """Fetch USDC (collateral) balance + allowance for this account (L2)."""
        sig_type = signature_type if signature_type is not None else self._sig_type
        return self._l2_get(
            "/balance-allowance",
            params={"asset_type": "COLLATERAL", "signature_type": sig_type},
        )

    def open_orders(self, market: Optional[str] = None) -> List[Dict[str, Any]]:
        """List this account's open orders (L2 ``GET /data/orders``)."""
        params = {"market": market} if market else None
        data = self._l2_get("/data/orders", params=params)
        if isinstance(data, dict):
            return data.get("data", data.get("orders", [])) or []
        return data or []

    def get_order_book(self, token_id: str) -> Any:
        """GET /book?token_id=... (public)."""
        return self._public_get("/book", params={"token_id": str(token_id)})

    def get_tick_size(self, token_id: str) -> Optional[str]:
        """GET /tick-size for a token; returns e.g. ``"0.001"`` or None."""
        try:
            data = self._public_get("/tick-size", params={"token_id": str(token_id)})
        except Exception:  # noqa: BLE001 — fall back to caller's tick
            return None
        tick = data.get("minimum_tick_size") if isinstance(data, dict) else data
        if tick is None:
            return None
        # Normalise float/str (0.001 -> "0.001") to the ROUNDING_CONFIG keys.
        s = ("%g" % float(tick))
        return s if s in signing.ROUNDING_CONFIG else None

    def midpoint(self, token_id: str) -> Optional[float]:
        """Public midpoint for a token id, or ``None`` if no book exists."""
        resp = self._request("GET", "/midpoint", params={"token_id": str(token_id)})
        if getattr(resp, "status_code", 200) >= 400:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        mid = data.get("mid") if isinstance(data, dict) else data
        try:
            return float(mid) if mid is not None else None
        except (TypeError, ValueError):
            return None

    # -- account-class detection ------------------------------------------

    def detect_account_class(self) -> Dict[str, Any]:
        """Resolve ``signature_type`` + ``funder`` from where the USDC lives.

        Strategy
        --------
        1. If a signature type was forced (constructor/config), honour it; the
           funder is the explicit funder (or the EOA for type 0).
        2. Else, if an explicit funder distinct from the EOA was supplied,
           assume a Gnosis-safe proxy (type 2, the MetaMask-deposit flow).
        3. Else query the Data API: if the EOA holds value, self-custody EOA
           (type 0).  If not but a discovered proxy holds value, use
           POLY_GNOSIS_SAFE (type 2, maker = proxy).  Fall back gracefully to
           EOA self-custody when discovery fails (so dry-run works offline).

        Returns a dict ``{address, signature_type, signature_type_name,
        funder}`` (the probe reads these keys).
        """
        if self._forced_sig_type is not None:
            self._sig_type = self._forced_sig_type
            self._funder = self._forced_funder or (
                self.address if self._sig_type == SIG_TYPE_EOA else self._funder
            )
            self._account_class_proven = True
        elif self._forced_funder and self._forced_funder.lower() != self.address.lower():
            # A distinct funder address means a proxy-funded account.  MetaMask
            # deposits use a Gnosis safe (type 2); email/magic uses type 1.
            self._sig_type = SIG_TYPE_POLY_GNOSIS_SAFE
            self._funder = self._forced_funder
            self._account_class_proven = True
        else:
            self._sig_type, self._funder, self._account_class_proven = (
                self._discover_account_class()
            )

        return {
            "address": self.address,
            "signature_type": self._sig_type,
            "signature_type_name": _SIG_NAMES.get(self._sig_type, "?"),
            "funder": self._funder,
        }

    def _discover_account_class(self) -> Tuple[int, str, bool]:
        """Return ``(sig_type, funder, proven)``.

        ``proven`` is ``True`` only when the Data API positively showed value at
        the EOA or a discovered proxy.  When discovery turns up nothing it is
        ``False`` and the EOA is returned purely as a graceful offline default —
        good enough for dry-run signing, but :meth:`place_order` refuses to
        submit a *live* order in that state (the funder is almost certainly the
        empty EOA rather than the proxy that holds USDC).
        """
        eoa = self.address
        eoa_value = self._data_api_value(eoa)
        if eoa_value is not None and eoa_value > 0:
            return SIG_TYPE_EOA, eoa, True

        proxy = self._lookup_proxy_address(eoa)
        if proxy:
            proxy_value = self._data_api_value(proxy)
            if proxy_value is not None and proxy_value > 0:
                return SIG_TYPE_POLY_GNOSIS_SAFE, proxy, True

        # Unproven fallback: assume EOA self-custody for dry-run only.
        return SIG_TYPE_EOA, eoa, False

    def _data_api_value(self, address: str) -> Optional[float]:
        """Return total position value (USD) for *address*, or None on failure."""
        try:
            resp = self._sess().request(
                "GET", DATA_API_HOST + "/value", params={"user": address},
                timeout=_TIMEOUT,
            )
        except Exception:
            return None
        if getattr(resp, "status_code", 200) >= 400:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
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
        """Resolve the Polymarket proxy/safe address for *eoa* via Data API."""
        try:
            resp = self._sess().request(
                "GET", DATA_API_HOST + "/profile", params={"address": eoa},
                timeout=_TIMEOUT,
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

    def build_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        *,
        neg_risk: bool = False,
        salt: Optional[int] = None,
        fee_rate_bps: int = 0,
        nonce: int = 0,
        expiration: int = 0,
        tick_size: str = "0.01",
    ) -> Dict[str, Any]:
        """Build and EIP-712-sign a CTF-Exchange order (server JSON form).

        ``size`` is the number of outcome *shares*; ``price`` is the per-share
        price in USDC.  The order is signed for the resolved account class so
        ``maker`` = funder (proxy/safe for types 1/2, EOA for type 0) and
        ``signer`` = EOA — the proxy-wallet fix.  Signing targets
        ``self.exchange_version`` (V2 by default).  Returns the dict ready to
        nest under ``{"order": ...}`` in the POST body.
        """
        if not (0.0 < price < 1.0):
            raise TradeError("price must be strictly between 0 and 1")
        if size <= 0:
            raise TradeError("size must be positive")

        args = signing.OrderArgs(
            token_id=str(token_id),
            price=float(price),
            size=float(size),
            side=str(side),
            fee_rate_bps=int(fee_rate_bps),
            nonce=int(nonce),
            expiration=int(expiration),
            tick_size=str(tick_size),
        )
        return signing.build_signed_order(
            self._key,
            args,
            funder=self._funder,
            signature_type=self._sig_type,
            neg_risk=neg_risk,
            exchange_version=self.config.exchange_version,
            salt=salt,
        )

    # -- guardrails: market keyword + daily cap ---------------------------

    def _check_keyword_allowed(self, market_question: Optional[str]) -> None:
        if not market_question:
            raise TradeError(
                "market_question is required to enforce the keyword allowlist"
            )
        q = market_question.lower()
        if not any(kw.lower() in q for kw in self.config.allowed_keywords):
            raise TradeError(
                "market is not in the keyword allowlist; refusing to trade"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_log_table(self) -> None:
        with self._connect() as conn:
            conn.execute(_DDL_PM_ORDER_LOG)
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(pm_order_log)").fetchall()
            }
            if "market" not in cols:
                conn.execute("ALTER TABLE pm_order_log ADD COLUMN market TEXT")

    def _daily_notional(self) -> float:
        self._ensure_log_table()
        day = _utc_day()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(notional), 0.0) FROM pm_order_log "
                "WHERE day_utc = ? AND dry_run = 0",
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
        market: Optional[str] = None,
    ) -> None:
        self._ensure_log_table()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pm_order_log "
                "(ts_utc, day_utc, token_id, market, side, price, size, notional, "
                " order_id, dry_run) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    _utc_day(),
                    str(token_id),
                    market,
                    side,
                    float(price),
                    float(size),
                    float(notional),
                    order_id,
                    1 if dry_run else 0,
                ),
            )

    # -- place / cancel ----------------------------------------------------

    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        *,
        neg_risk: bool = False,
        dry_run: Optional[bool] = None,
        market_question: Optional[str] = None,
        tick_size: str = "0.01",
        order_type: str = "GTC",
        de_risk: bool = False,
    ) -> Dict[str, Any]:
        """Validate guardrails, sign, and (unless dry-run) POST one order.

        Positional contract matches the bot: ``(token_id, price, size, side)``
        where ``size`` is the number of outcome shares and ``price`` the
        per-share USDC price (notional = price * size).

        ``dry_run`` overrides ``config.dry_run`` when given (the bot passes the
        ``PM_DRY_RUN`` env flag per call).  Guardrails enforced *before* any
        network POST:
          1. per-order cap (notional <= ``config.max_order_usd``, or
             ``config.max_cashout_usd_per_order`` for a ``de_risk`` sell);
          2. keyword allowlist (``market_question`` must match) — skipped when
             no ``market_question`` is supplied AND the allowlist is the only
             gate the caller relies on; the bot supplies pre-vetted markets;
          3. daily cap (today's live notional + this order
             <= ``config.max_daily_usd``), live orders only.

        ``de_risk`` marks a *cash-out SELL* of shares we already hold. Such an
        order REDUCES risk, so it is exempt from the risk-on entry guards that
        would otherwise wrongly block it: the daily BUY budget (check 3) and the
        World-Cup keyword allowlist (check 2 — an exit shouldn't be gated on the
        market title carrying a WC keyword). It is still funder-checked, still
        per-order capped (by the looser cash-out cap), and still logged. It is a
        hard error to pass ``de_risk=True`` for a BUY.

        Returns a status dict.  In dry-run: ``{"dry_run": True, "submitted":
        False, "request": {...}, maker/signer/signature_type/side/makerAmount/
        takerAmount}``.  Live: the parsed server response merged with
        ``{"dry_run": False, "submitted": True, maker/signer/signature_type}``
        and (for the bot) ``orderID``.
        """
        is_dry = self.config.dry_run if dry_run is None else bool(dry_run)
        notional = float(price) * float(size)

        if de_risk and str(side).strip().upper() != "SELL":
            raise TradeError("de_risk is only valid for a SELL (cash-out) order")

        # Resolve account class so maker/signer are correct before signing.
        self.detect_account_class()

        # (0) Funder safety — never submit a LIVE order with an unproven EOA
        # funder.  When no funder / signature_type was supplied and Data-API
        # discovery could not prove where the USDC lives, the maker defaulted to
        # the EOA (which, for this account, holds $0 — the real balance sits in
        # the Polymarket proxy/safe).  Submitting would sign from the wrong
        # wallet; refuse loudly instead of silently using the EOA.  Dry-run is
        # exempt so offline signing/inspection still works.
        if not is_dry and not self._account_class_proven:
            raise TradeError(
                "refusing to submit a live order: account class is unproven and "
                "the funder defaulted to the EOA (%s), which is not where the "
                "USDC lives. Set POLYMARKET_FUNDER (and POLYMARKET_SIG_TYPE=2 "
                "for a Gnosis-safe/deposit wallet) before trading live."
                % self.address
            )

        # (1) per-order cap — looser cash-out cap for a de-risking sell.
        per_order_cap = (
            self.config.max_cashout_usd_per_order if de_risk
            else self.config.max_order_usd
        )
        if notional > per_order_cap + 1e-9:
            raise TradeError(
                "order notional %.2f exceeds per-order cap %.2f"
                % (notional, per_order_cap)
            )

        # (2) keyword allowlist — only enforced when a question is supplied, and
        # never for a de-risk exit (selling something we hold is not gated on the
        # market title carrying a World-Cup keyword).
        if market_question is not None and not de_risk:
            self._check_keyword_allowed(market_question)

        # (3) daily cap (live BUY budget; de-risk sells reduce risk -> exempt,
        # but still logged below for audit / the runaway backstop).
        if not is_dry and not de_risk:
            spent = self._daily_notional()
            if spent + notional > self.config.max_daily_usd + 1e-9:
                raise TradeError(
                    "daily cap exceeded: %.2f already placed, +%.2f would pass %.2f"
                    % (spent, notional, self.config.max_daily_usd)
                )

        # Use the market's LIVE tick size — amount rounding decimals depend on
        # it, and a stale/wrong tick produces maker/taker pairs whose implied
        # price is off-grid (server: 400 "Invalid order payload").
        live_tick = self.get_tick_size(token_id)
        if live_tick:
            tick_size = live_tick

        signed = self.build_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            neg_risk=neg_risk,
            tick_size=tick_size,
        )

        if is_dry:
            self._log_order(
                token_id,
                side.upper(),
                price,
                size,
                notional,
                None,
                dry_run=True,
                market=market_question,
            )
            # Fill-lifecycle telemetry (observation only — never gates,
            # sizes, or retries anything). See wca.pm.filltelemetry module
            # docstring: this is the "GTC-at-mid, no fill-rate logging"
            # instrumentation from the 2026-07-08 review.
            _filltelemetry.log_placed(
                order_id=None,
                token_id=token_id,
                market=market_question,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
                dry_run=True,
                de_risk=de_risk,
                path=self.config.fill_log_path,
            )
            return {
                "dry_run": True,
                "submitted": False,
                "request": {
                    "order": signed,
                    "owner": self._creds.api_key if self._creds else self.address,
                    "orderType": order_type,
                    "deferExec": False,
                    "postOnly": False,
                },
                "maker": signed["maker"],
                "signer": signed["signer"],
                "signature_type": signed["signatureType"],
                "side": signed["side"],
                "makerAmount": signed["makerAmount"],
                "takerAmount": signed["takerAmount"],
            }

        creds = self.derive_or_create_api_creds()
        envelope = {
            "order": signed,
            "owner": creds.api_key,
            "orderType": order_type,
            "deferExec": False,
            "postOnly": False,
        }
        # Neg-risk markets require an explicit top-level negRisk flag in the
        # POST envelope; without it the server rejects with a generic
        # "Invalid order payload" even for a correctly-signed order. Verified
        # live 2026-06-12 (first filled bot order). Standard markets must NOT
        # carry the flag.
        if neg_risk:
            envelope["negRisk"] = True
        body = json.dumps(envelope, separators=(",", ":"))
        headers = self.l2_headers("POST", "/order", body=body)
        headers["Content-Type"] = "application/json"

        # --- LIVE submission boundary ---------------------------------------
        # Past this point the order may reach the matching engine even if we
        # fail to read the response or record it.  Three outcomes:
        #   * definitive client-side rejection (4xx / success:false) -> the
        #     order was NOT placed; raise ClobAuthError as before (safe retry).
        #   * uncertain outcome (network error / 5xx / unparseable) -> the
        #     order MAY be on-chain; raise LiveOrderUnconfirmed so the bot
        #     alerts an operator instead of inviting a blind double-spend retry.
        #   * server accepted it but the pm_order_log write fails -> the order
        #     is definitely LIVE and unlogged; also LiveOrderUnconfirmed.
        # This is the fix for the 2026-06-15 silently-unlogged on-chain fill.
        try:
            resp = self._request("POST", "/order", headers=headers, body=body)
        except TradeError as exc:
            raise LiveOrderUnconfirmed(
                token_id, side.upper(), price, size, notional, None,
                "network error submitting live order (may be on-chain): %s" % exc,
            ) from exc

        try:
            out = resp.json()
        except ValueError:
            out = {"raw": _short(getattr(resp, "text", ""))}

        status_code = getattr(resp, "status_code", 200)
        if status_code >= 500:
            # Server-side error: the order may have been accepted before the
            # failure — treat as possibly-live, not a clean rejection.
            raise LiveOrderUnconfirmed(
                token_id, side.upper(), price, size, notional, None,
                "server error on live order (may be on-chain): %s %s"
                % (status_code, _short(str(out))),
            )
        if status_code >= 400 or (
            isinstance(out, dict) and out.get("success") is False
        ):
            raise ClobAuthError(
                "order POST failed: %s %s" % (status_code, _short(str(out)))
            )

        order_id = (
            out.get("orderID") or out.get("orderId") if isinstance(out, dict) else None
        )
        try:
            self._log_order(
                token_id,
                side.upper(),
                price,
                size,
                notional,
                order_id,
                dry_run=False,
                market=market_question,
            )
        except Exception as exc:
            # The server accepted the order (it is LIVE) but the pm_order_log
            # write failed — surface loudly so the ledger gets reconciled
            # rather than the fill going silently unrecorded.
            raise LiveOrderUnconfirmed(
                token_id, side.upper(), price, size, notional, order_id,
                "live order accepted (id %s) but pm_order_log write failed: %s"
                % (order_id, exc),
            ) from exc

        # Fill-lifecycle telemetry (observation only). A GTC BUY logged here
        # rests at ``price``; whether it ever fills is NOT observed on this
        # path today (see wca.pm.filltelemetry docstring) — that is exactly
        # the invisible-EV-leak this row makes measurable: a "placed" row
        # with no later "fill_observed" row for the same order_id is an
        # unconfirmed/unfilled resting order.
        _filltelemetry.log_placed(
            order_id=order_id,
            token_id=token_id,
            market=market_question,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            dry_run=False,
            de_risk=de_risk,
            path=self.config.fill_log_path,
        )
        result: Dict[str, Any] = dict(out) if isinstance(out, dict) else {"response": out}
        result.update(
            {
                "dry_run": False,
                "submitted": True,
                "response": out,
                "maker": signed["maker"],
                "signer": signed["signer"],
                "signature_type": signed["signatureType"],
            }
        )
        return result

    def cancel_order(self, order_id: str) -> Any:
        """DELETE /order with body {"orderID": id} (L2-authenticated)."""
        body = json.dumps({"orderID": order_id}, separators=(",", ":"))
        headers = self.l2_headers("DELETE", "/order", body=body)
        headers["Content-Type"] = "application/json"
        resp = self._request("DELETE", "/order", headers=headers, body=body)
        if getattr(resp, "status_code", 200) >= 400:
            raise ClobAuthError("cancel rejected (status %s)" % resp.status_code)
        try:
            return resp.json()
        except Exception:
            raise TradeError("could not parse cancel response")
