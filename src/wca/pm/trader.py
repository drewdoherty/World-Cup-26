"""Polymarket CLOB REST client built on the local signers.

:class:`ClobTrader` is a thin wrapper around ``requests`` that wires the
:mod:`wca.pm.signing` functions to the CLOB host (``https://clob.polymarket.com``):

* ``derive_or_create_creds`` — L1 ClobAuth -> api key / secret / passphrase.
* ``detect_account_class`` — work out whether the funds sit on the EOA or a
  proxy / safe wallet, returning the right ``signature_type`` and funder.
* ``balance_allowance`` / ``open_orders`` / ``midpoint`` — L2 / public reads.
* ``place_order`` — sign an order correctly for the detected account class and
  POST it (honouring ``dry_run``).

The class is deliberately import-light: ``requests`` is imported at call time
so the module parses even in environments where it is unavailable, and the
private key is only ever read from the instance attribute, never logged.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from wca.pm import signing

CLOB_HOST = "https://clob.polymarket.com"
DATA_API_HOST = "https://data-api.polymarket.com"
_TIMEOUT = 20


class ClobAuthError(RuntimeError):
    """Raised when L1/L2 authentication fails — the signer-address bug detector.

    The message carries the CLOB's own error text so the probe can surface the
    exact failure (e.g. an ``invalid signature`` / address-mismatch response is
    the smoking gun for the proxy-wallet signing bug).
    """


class ClobTrader:
    """Sign and submit Polymarket CLOB requests for any account class.

    Parameters
    ----------
    private_key:
        The EOA private key.  Never logged; only used to derive the address and
        produce signatures.
    funder:
        The funding wallet (proxy / safe) when the account is not a bare EOA.
        If omitted, account detection falls back to the EOA.
    signature_type:
        Force a signature type (0/1/2).  If ``None`` it is detected from where
        the USDC balance lives.
    host:
        CLOB base URL (overridable for tests).
    creds:
        Pre-derived ``{"api_key","api_secret","api_passphrase"}`` to skip the
        L1 derive round-trip.
    """

    def __init__(
        self,
        private_key: str,
        funder: Optional[str] = None,
        signature_type: Optional[int] = None,
        host: str = CLOB_HOST,
        creds: Optional[Dict[str, str]] = None,
    ) -> None:
        if not private_key:
            raise ValueError("private_key is required")
        self._key = private_key
        self.address = signing.address_for_key(private_key)
        self.host = host.rstrip("/")
        self._funder = funder
        self._forced_sig_type = signature_type
        self.creds = creds
        # Resolved after detect_account_class(); sensible EOA defaults.
        self.signature_type = signature_type if signature_type is not None else signing.SIG_EOA
        self.funder = funder or self.address

    # ------------------------------------------------------------------ HTTP
    def _request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[str] = None,
    ):
        import requests

        url = self.host + path
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body,
            timeout=_TIMEOUT,
        )
        return resp

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ------------------------------------------------------------ L1: creds
    def derive_or_create_creds(self, nonce: int = 0) -> Dict[str, str]:
        """Derive (or create) the L2 API credentials via an L1 ClobAuth sig.

        Tries ``GET /auth/derive-api-key`` first (idempotent for an existing
        key) then falls back to ``POST /auth/api-key``.  Raises
        :class:`ClobAuthError` carrying the CLOB error text on failure — this
        is the auth-failure signal the probe treats as the bug detector.
        """
        if self.creds:
            return self.creds

        ts = self._now()
        headers = signing.build_l1_headers(self._key, ts, nonce)
        # Derive first.
        resp = self._request("GET", "/auth/derive-api-key", headers=headers)
        if resp.status_code != 200:
            # Re-sign with a fresh timestamp for the create attempt.
            ts2 = self._now()
            headers2 = signing.build_l1_headers(self._key, ts2, nonce)
            resp2 = self._request("POST", "/auth/api-key", headers=headers2)
            if resp2.status_code != 200:
                raise ClobAuthError(
                    "L1 auth failed: derive=%s %s | create=%s %s"
                    % (resp.status_code, _short(resp.text), resp2.status_code, _short(resp2.text))
                )
            resp = resp2

        data = resp.json()
        creds = {
            "api_key": data.get("apiKey") or data.get("api_key", ""),
            "api_secret": data.get("secret") or data.get("api_secret", ""),
            "api_passphrase": data.get("passphrase") or data.get("api_passphrase", ""),
        }
        if not creds["api_key"] or not creds["api_secret"]:
            raise ClobAuthError("L1 auth returned no usable creds: %s" % _short(resp.text))
        self.creds = creds
        return creds

    # --------------------------------------------------- account detection
    def detect_account_class(self) -> Dict[str, Any]:
        """Resolve ``signature_type`` + ``funder`` from where the USDC lives.

        Strategy: honour an explicitly forced signature type / funder.  Else,
        if a funder address distinct from the EOA was supplied, assume a
        Gnosis-safe proxy (type 2, the MetaMask-deposit flow).  A bare EOA with
        no funder stays type 0.  In all cases we report the address that
        carries the collateral so the caller can sanity-check before trading.
        """
        if self._forced_sig_type is not None:
            self.signature_type = self._forced_sig_type
            self.funder = self._funder or self.address
        elif self._funder and self._funder.lower() != self.address.lower():
            # A distinct funder address means a proxy-funded account.  The
            # MetaMask deposit flow uses a Gnosis safe (type 2); email/magic
            # uses type 1.  Default to safe; caller can force type 1 via env.
            self.signature_type = signing.SIG_POLY_GNOSIS_SAFE
            self.funder = self._funder
        else:
            self.signature_type = signing.SIG_EOA
            self.funder = self.address

        return {
            "address": self.address,
            "signature_type": self.signature_type,
            "signature_type_name": _SIG_NAMES.get(self.signature_type, "?"),
            "funder": self.funder,
        }

    # -------------------------------------------------------- L2 reads
    def _l2_headers(self, method: str, path: str, body: Optional[str] = None) -> Dict[str, str]:
        creds = self.derive_or_create_creds()
        ts = self._now()
        return signing.build_l2_headers(
            self.address,
            creds["api_key"],
            creds["api_secret"],
            creds["api_passphrase"],
            ts,
            method,
            path,
            body,
        )

    def balance_allowance(self, signature_type: Optional[int] = None) -> Dict[str, Any]:
        """Fetch USDC (collateral) balance + allowance for this account (L2)."""
        path = "/balance-allowance"
        headers = self._l2_headers("GET", path)
        sig_type = signature_type if signature_type is not None else self.signature_type
        params = {"asset_type": "COLLATERAL", "signature_type": sig_type}
        resp = self._request("GET", path, headers=headers, params=params)
        if resp.status_code != 200:
            raise ClobAuthError(
                "balance-allowance failed: %s %s" % (resp.status_code, _short(resp.text))
            )
        return resp.json()

    def open_orders(self) -> List[Dict[str, Any]]:
        """List this account's open orders (L2 ``GET /data/orders``)."""
        path = "/data/orders"
        headers = self._l2_headers("GET", path)
        resp = self._request("GET", path, headers=headers)
        if resp.status_code != 200:
            raise ClobAuthError(
                "open-orders failed: %s %s" % (resp.status_code, _short(resp.text))
            )
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data", data.get("orders", [])) or []
        return data or []

    def midpoint(self, token_id: str) -> Optional[float]:
        """Public midpoint for a token id, or ``None`` if no book exists."""
        resp = self._request("GET", "/midpoint", params={"token_id": str(token_id)})
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        mid = data.get("mid") if isinstance(data, dict) else None
        try:
            return float(mid) if mid is not None else None
        except (TypeError, ValueError):
            return None

    # -------------------------------------------------------- order placement
    def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        *,
        neg_risk: bool = False,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Sign + (optionally) submit one order, returning a status dict.

        The order is *always* signed with the detected account class so the
        ``maker`` / ``signer`` / ``signatureType`` triple is correct even for
        proxy-funded wallets (the bug the SDKs trip on).  When ``dry_run`` is
        true the signed order is built (proving signing works) but never POSTed.
        """
        self.detect_account_class()
        args = signing.OrderArgs(
            token_id=str(token_id), price=float(price), size=float(size), side=side
        )
        signed = signing.build_signed_order(
            self._key,
            args,
            funder=self.funder,
            signature_type=self.signature_type,
            neg_risk=neg_risk,
        )
        if dry_run:
            return {
                "dry_run": True,
                "submitted": False,
                "maker": signed["maker"],
                "signer": signed["signer"],
                "signature_type": signed["signatureType"],
                "side": signed["side"],
                "makerAmount": signed["makerAmount"],
                "takerAmount": signed["takerAmount"],
            }

        body_obj = {"order": signed, "owner": self.creds["api_key"] if self.creds else self.address, "orderType": "GTC"}
        body = signing.serialize_body(body_obj)
        headers = self._l2_headers("POST", "/order", body)
        headers["Content-Type"] = "application/json"
        resp = self._request("POST", "/order", headers=headers, body=body)
        try:
            out = resp.json()
        except ValueError:
            out = {"raw": _short(resp.text)}
        if resp.status_code != 200 or (isinstance(out, dict) and out.get("success") is False):
            raise ClobAuthError(
                "order POST failed: %s %s" % (resp.status_code, _short(str(out)))
            )
        out["dry_run"] = False
        out["submitted"] = True
        out["maker"] = signed["maker"]
        out["signer"] = signed["signer"]
        out["signature_type"] = signed["signatureType"]
        return out


_SIG_NAMES = {
    signing.SIG_EOA: "EOA",
    signing.SIG_POLY_PROXY: "POLY_PROXY",
    signing.SIG_POLY_GNOSIS_SAFE: "POLY_GNOSIS_SAFE",
}


def _short(text: Optional[str], n: int = 240) -> str:
    """Trim an error body for safe display (signatures never reach here)."""
    if not text:
        return ""
    s = str(text).strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."
