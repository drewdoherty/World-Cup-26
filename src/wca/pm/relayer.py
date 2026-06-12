"""Polymarket relayer client for gasless Safe wallet-actions.

The user account's trading proxy is a Gnosis Safe (1.3.0).  To self-sign CLOB
orders we must set ERC-20 / ERC-1155 approvals *from the proxy* to the V2
exchanges.  The proxy has no native gas, so we route the approval transactions
through the Polymarket relayer: the EOA signs a Gnosis Safe ``SafeTx`` EIP-712
digest, and the relayer executes ``execTransaction`` on the proxy with
``gasPrice = 0`` (no gas cost to us).

This module is *inert by default*.  ``RelayerClient.submit_action`` performs a
network POST, but the only caller (``scripts/wca_pm_approve.py``) is dry-run
unless explicitly armed with ``PM_APPROVE_LIVE=1`` and ``--yes``.

Pure helpers (calldata builders, the SafeTx digest) require no network and no
private key.  Signatures and keys are never logged.

Dependencies: stdlib + requests + eth-account / eth-utils.  NO web3.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Constants (chain 137 / Polygon).  All lowercase hex unless checksummed.
# ---------------------------------------------------------------------------

RELAYER_BASE_URL = "https://relayer-v2.polymarket.com"

CHAIN_ID = 137

# Token + exchange addresses from the relayer spec.
PUSD_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
EXCHANGE_STD = "0xE111180000d2663C0091e4f400237545B87B996B"
EXCHANGE_NEG_RISK = "0xe2222d279d744050d28e00520010520000310F59"
# Conditional Tokens Framework (ERC-1155) on Polygon.
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

OWNER_EOA = "0x721A9E426267502d20bcB8afBe9db25a86dCEB76"
PROXY_SAFE = "0x86b4c55a4df1fbea0f325e842434e0a537caa549"

# ERC-20 approve(address,uint256) selector and ERC-1155
# setApprovalForAll(address,bool) selector.
_APPROVE_SELECTOR = "095ea7b3"
_SET_APPROVAL_FOR_ALL_SELECTOR = "a22cb465"

# uint256 max (unlimited allowance).
MAX_UINT256 = (1 << 256) - 1

# Gnosis Safe 1.3.0 SafeTx EIP-712 type hash (verified on-chain).
SAFE_TX_TYPEHASH = (
    "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
)

# ---------------------------------------------------------------------------
# Deposit-wallet ("DepositWallet") constants — the WALLET branch.
#
# Our proxy (PROXY_SAFE / DEPOSIT_WALLET) is NOT a Gnosis Safe: it is an
# ERC-1967 "DepositWallet" proxy whose EIP-712 domain is
#   {name:"DepositWallet", version:"1", chainId:137, verifyingContract:<DW>}
# and whose primary signed type is a Batch of Calls.  Wallet-actions are
# EOA-signed by the deposit wallet's owner and executed gaslessly by the
# relayer's WALLET endpoints.  Typehashes below are verified against the spec
# (keccak of the EIP-712 type strings).
# ---------------------------------------------------------------------------

DEPOSIT_WALLET = PROXY_SAFE
IMPLEMENTATION = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"
FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

DEPOSIT_WALLET_DOMAIN_NAME = "DepositWallet"
DEPOSIT_WALLET_DOMAIN_VERSION = "1"

# EIP-712 type strings.  The Batch primary type references Call, so per EIP-712
# the encodeType is Batch(...)Call(...) (referenced structs appended, sorted).
CALL_TYPE_STRING = "Call(address target,uint256 value,bytes data)"
BATCH_TYPE_STRING = (
    "Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)"
    + CALL_TYPE_STRING
)
# Verified: keccak(BATCH_TYPE_STRING) / keccak(CALL_TYPE_STRING).
BATCH_TYPEHASH = (
    "712ef66e8362c387e862cabf0923c209db0fa24cfc97d25eccba7c86f3ee1dd3"
)
CALL_TYPEHASH = (
    "84fa2cf05cd88e992eae77e851af68a4ee278dcff6ef504e487a55b3baadfbe5"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# A very large default deadline (effectively "no expiry") used when a caller
# does not pass one.  uint256-safe.
DEFAULT_DEADLINE = MAX_UINT256


# ---------------------------------------------------------------------------
# Lazy crypto imports (never log keys/signatures).
# ---------------------------------------------------------------------------


def _keccak(data: bytes) -> bytes:
    try:
        from eth_utils import keccak
    except ImportError as exc:  # pragma: no cover - dependency installed
        raise RuntimeError(
            "eth-utils is required for the relayer client (pip install eth-utils)"
        ) from exc
    return keccak(data)


def _account_from_key(private_key: str):
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - dependency installed
        raise RuntimeError(
            "eth-account is required for the relayer client (pip install eth-account)"
        ) from exc
    return Account.from_key(private_key)


# ---------------------------------------------------------------------------
# ABI encoding helpers (pure, no web3).
# ---------------------------------------------------------------------------


def _strip0x(s: str) -> str:
    return s[2:] if s.lower().startswith("0x") else s


def _leftpad32_addr(address: str) -> str:
    """ABI-encode an address as a 32-byte left-padded lowercase hex word."""
    a = _strip0x(address).lower()
    if len(a) != 40:
        raise ValueError(f"invalid address: {address!r}")
    int(a, 16)  # validate hex
    return a.rjust(64, "0")


def _encode_uint256(value: int) -> str:
    if value < 0 or value > MAX_UINT256:
        raise ValueError("uint256 out of range")
    return format(value, "064x")


def _encode_bool(value: bool) -> str:
    return _encode_uint256(1 if value else 0)


def build_approve_calldata(spender: str, amount: int = MAX_UINT256) -> str:
    """Return ``approve(spender, amount)`` calldata as a ``0x``-prefixed hex string.

    Pure: ``0x095ea7b3 || leftpad32(spender) || uint256(amount)``.  No web3.
    """
    return "0x" + _APPROVE_SELECTOR + _leftpad32_addr(spender) + _encode_uint256(amount)


def build_set_approval_for_all_calldata(operator: str, approved: bool = True) -> str:
    """Return ``setApprovalForAll(operator, approved)`` calldata (ERC-1155)."""
    return (
        "0x"
        + _SET_APPROVAL_FOR_ALL_SELECTOR
        + _leftpad32_addr(operator)
        + _encode_bool(approved)
    )


# ---------------------------------------------------------------------------
# Gnosis Safe SafeTx EIP-712 digest (pure given inputs).
# ---------------------------------------------------------------------------


def safe_domain_separator(proxy: str, chain_id: int = CHAIN_ID) -> bytes:
    """EIP-712 domain separator for a Safe 1.3.0.

    Safe 1.3.0 uses ``EIP712Domain(uint256 chainId,address verifyingContract)``
    — NO name/version field.  TYPEHASH is keccak of that string.
    """
    domain_typehash = _keccak(
        b"EIP712Domain(uint256 chainId,address verifyingContract)"
    )
    encoded = (
        domain_typehash
        + bytes.fromhex(_encode_uint256(chain_id))
        + bytes.fromhex(_leftpad32_addr(proxy))
    )
    return _keccak(encoded)


def safe_tx_struct_hash(
    to: str,
    value: int,
    data: str,
    operation: int,
    safe_tx_gas: int,
    base_gas: int,
    gas_price: int,
    gas_token: str,
    refund_receiver: str,
    nonce: int,
) -> bytes:
    """keccak of the SafeTx struct (bytes ``data`` is hashed per EIP-712)."""
    data_hash = _keccak(bytes.fromhex(_strip0x(data)))
    encoded = (
        bytes.fromhex(SAFE_TX_TYPEHASH)
        + bytes.fromhex(_leftpad32_addr(to))
        + bytes.fromhex(_encode_uint256(value))
        + data_hash
        + bytes.fromhex(_encode_uint256(operation))
        + bytes.fromhex(_encode_uint256(safe_tx_gas))
        + bytes.fromhex(_encode_uint256(base_gas))
        + bytes.fromhex(_encode_uint256(gas_price))
        + bytes.fromhex(_leftpad32_addr(gas_token))
        + bytes.fromhex(_leftpad32_addr(refund_receiver))
        + bytes.fromhex(_encode_uint256(nonce))
    )
    return _keccak(encoded)


def safe_tx_digest(
    proxy: str,
    to: str,
    data: str,
    nonce: int,
    *,
    value: int = 0,
    operation: int = 0,
    safe_tx_gas: int = 0,
    base_gas: int = 0,
    gas_price: int = 0,
    gas_token: str = ZERO_ADDRESS,
    refund_receiver: str = ZERO_ADDRESS,
    chain_id: int = CHAIN_ID,
) -> bytes:
    """Full EIP-712 digest: keccak(0x19 0x01 || domainSep || structHash)."""
    domain_sep = safe_domain_separator(proxy, chain_id=chain_id)
    struct_hash = safe_tx_struct_hash(
        to,
        value,
        data,
        operation,
        safe_tx_gas,
        base_gas,
        gas_price,
        gas_token,
        refund_receiver,
        nonce,
    )
    return _keccak(b"\x19\x01" + domain_sep + struct_hash)


def sign_safe_tx_digest(private_key: str, digest: bytes) -> str:
    """Sign a 32-byte digest with secp256k1; return packed ``r||s||v`` hex (v=27/28)."""
    acct = _account_from_key(private_key)
    # _sign_hash is deprecated in newer eth-account; unsafe_sign_hash is the
    # raw-digest signer.  Try the modern name first.
    if hasattr(acct, "unsafe_sign_hash"):
        signed = acct.unsafe_sign_hash(digest)
    else:  # pragma: no cover - older eth-account
        signed = acct._sign_hash(digest)
    r = signed.r.to_bytes(32, "big")
    s = signed.s.to_bytes(32, "big")
    v = signed.v
    if v < 27:
        v += 27
    return "0x" + (r + s + bytes([v])).hex()


# A digest signer is identical for the deposit-wallet branch (raw secp256k1 over
# the EIP-712 digest, packed r||s||v with v normalised to 27/28).
sign_digest = sign_safe_tx_digest


# ---------------------------------------------------------------------------
# Deposit-wallet ("DepositWallet") EIP-712 Batch digest (pure given inputs).
# ---------------------------------------------------------------------------


def deposit_wallet_domain_separator(
    deposit_wallet: str, chain_id: int = CHAIN_ID
) -> bytes:
    """EIP-712 domain separator for a DepositWallet.

    Domain = ``EIP712Domain(string name,string version,uint256 chainId,
    address verifyingContract)`` with name "DepositWallet", version "1".
    """
    domain_typehash = _keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,"
        b"address verifyingContract)"
    )
    encoded = (
        domain_typehash
        + _keccak(DEPOSIT_WALLET_DOMAIN_NAME.encode("utf-8"))
        + _keccak(DEPOSIT_WALLET_DOMAIN_VERSION.encode("utf-8"))
        + bytes.fromhex(_encode_uint256(chain_id))
        + bytes.fromhex(_leftpad32_addr(deposit_wallet))
    )
    return _keccak(encoded)


def call_struct_hash(target: str, value: int, data: str) -> bytes:
    """keccak of one ``Call(address target,uint256 value,bytes data)`` struct."""
    data_hash = _keccak(bytes.fromhex(_strip0x(data)))
    encoded = (
        bytes.fromhex(CALL_TYPEHASH)
        + bytes.fromhex(_leftpad32_addr(target))
        + bytes.fromhex(_encode_uint256(value))
        + data_hash
    )
    return _keccak(encoded)


def batch_struct_hash(
    wallet: str,
    nonce: int,
    deadline: int,
    calls: "list",
) -> bytes:
    """keccak of the ``Batch`` struct.

    ``calls`` is a list of ``{"target","value","data"}`` dicts.  The dynamic
    ``Call[]`` array is encoded per EIP-712 as keccak of the concatenated
    per-element struct hashes.
    """
    elems = b"".join(
        call_struct_hash(c["target"], int(c.get("value", 0)), c["data"])
        for c in calls
    )
    calls_hash = _keccak(elems)
    encoded = (
        bytes.fromhex(BATCH_TYPEHASH)
        + bytes.fromhex(_leftpad32_addr(wallet))
        + bytes.fromhex(_encode_uint256(nonce))
        + bytes.fromhex(_encode_uint256(deadline))
        + calls_hash
    )
    return _keccak(encoded)


def deposit_wallet_digest(
    deposit_wallet: str,
    nonce: int,
    calls: "list",
    *,
    deadline: int = DEFAULT_DEADLINE,
    chain_id: int = CHAIN_ID,
) -> bytes:
    """Full EIP-712 digest for a deposit-wallet Batch wallet-action.

    ``keccak(0x19 0x01 || domainSep || batchStructHash)``.  ``wallet`` inside
    the struct equals the deposit wallet itself (the verifying contract).
    """
    domain_sep = deposit_wallet_domain_separator(deposit_wallet, chain_id=chain_id)
    struct_hash = batch_struct_hash(deposit_wallet, nonce, deadline, calls)
    return _keccak(b"\x19\x01" + domain_sep + struct_hash)


# ---------------------------------------------------------------------------
# Relayer client.
# ---------------------------------------------------------------------------


class RelayerError(RuntimeError):
    """Raised on relayer transport or protocol errors."""


class RelayerClient:
    """Thin client for the Polymarket Safe relayer (gasless wallet-actions)."""

    # Recognised wallet-action branches.
    WALLET_TYPE_SAFE = "SAFE"
    WALLET_TYPE_WALLET = "WALLET"

    def __init__(
        self,
        private_key: Optional[str] = None,
        funder: Optional[str] = None,
        *,
        base_url: str = RELAYER_BASE_URL,
        owner: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        session: Any = None,
        wallet_type: Optional[str] = None,
    ) -> None:
        src = os.environ if env is None else env
        self._private_key = (
            private_key
            if private_key is not None
            else src.get("POLYMARKET_PRIVATE_KEY")
        )
        self.funder = (
            funder
            if funder is not None
            else (src.get("POLYMARKET_FUNDER") or PROXY_SAFE)
        )
        self.base_url = base_url.rstrip("/")
        self._owner = owner
        self._session = session
        self._env = src
        # Branch selection: explicit arg > POLYMARKET_WALLET_TYPE env > None
        # (None means "not yet resolved"; resolve_wallet_type() probes/defaults).
        wt = wallet_type or src.get("POLYMARKET_WALLET_TYPE")
        self.wallet_type = wt.strip().upper() if wt else None

    # -- properties -------------------------------------------------------

    @property
    def owner(self) -> str:
        """The signing EOA address (derived from the key if not given)."""
        if self._owner:
            return self._owner
        if not self._private_key:
            raise RelayerError(
                "no private key available to derive the owner address; "
                "set POLYMARKET_PRIVATE_KEY or pass owner="
            )
        self._owner = _account_from_key(self._private_key).address
        return self._owner

    def _http(self):
        if self._session is not None:
            return self._session
        import requests  # lazy

        return requests

    def _auth_headers(self) -> Dict[str, str]:
        """Relayer API-key headers (separate from CLOB L1/L2)."""
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        key = self._env.get("RELAYER_API_KEY")
        key_addr = self._env.get("RELAYER_API_KEY_ADDRESS")
        if key:
            headers["RELAYER_API_KEY"] = key
        if key_addr:
            headers["RELAYER_API_KEY_ADDRESS"] = key_addr
        return headers

    # -- reads ------------------------------------------------------------

    def get_nonce(self, address: Optional[str] = None, kind: str = "SAFE") -> int:
        """GET /nonce?address=&type= -> next nonce as int.

        For the SAFE branch ``address`` is the proxy/owner per the Safe spec;
        for the WALLET (deposit-wallet) branch pass ``kind="WALLET"`` and the
        owner EOA address (the relayer keys the deposit-wallet nonce off the
        owner).
        """
        addr = address or self.owner
        url = f"{self.base_url}/nonce"
        resp = self._http().get(
            url,
            params={"address": addr, "type": kind},
            headers=self._auth_headers(),
            timeout=30,
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise RelayerError(f"nonce request failed: {resp.status_code}")
        body = resp.json()
        nonce = body.get("nonce")
        if nonce is None:
            raise RelayerError(f"nonce missing from relayer response: {body!r}")
        return int(nonce)

    def wallet_deployed(self, address: Optional[str] = None) -> bool:
        """Return True if the proxy Safe is already deployed on-chain."""
        addr = address or self.funder
        url = f"{self.base_url}/wallet"
        resp = self._http().get(
            url,
            params={"address": addr},
            headers=self._auth_headers(),
            timeout=30,
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise RelayerError(f"wallet request failed: {resp.status_code}")
        body = resp.json()
        return bool(body.get("deployed", body.get("isDeployed", False)))

    def deployed(
        self, address: Optional[str] = None, kind: Optional[str] = None
    ) -> bool:
        """GET /deployed?address=[&type=] -> bool (deposit-wallet branch).

        ``address`` defaults to the owner EOA (the deposit-wallet factory keys
        deployment off the owner).  ``kind`` is the optional wallet type.
        """
        addr = address or self.owner
        params: Dict[str, Any] = {"address": addr}
        if kind:
            params["type"] = kind
        url = f"{self.base_url}/deployed"
        resp = self._http().get(
            url, params=params, headers=self._auth_headers(), timeout=30
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise RelayerError(f"deployed request failed: {resp.status_code}")
        body = resp.json()
        return bool(body.get("deployed", body.get("isDeployed", False)))

    def resolve_wallet_type(self) -> str:
        """Return the active branch, defaulting to WALLET for our deposit wallet.

        Resolution order: an explicit ``wallet_type`` (constructor or
        ``POLYMARKET_WALLET_TYPE`` env) wins.  Otherwise we default to the
        deposit-wallet (``WALLET``) branch, which is correct for our proxy
        (``DEPOSIT_WALLET`` is an ERC-1967 DepositWallet, NOT a Gnosis Safe).
        Pure: never touches the network so dry-run stays inert.
        """
        if self.wallet_type in (self.WALLET_TYPE_SAFE, self.WALLET_TYPE_WALLET):
            return self.wallet_type
        self.wallet_type = self.WALLET_TYPE_WALLET
        return self.wallet_type

    # -- sign + submit ----------------------------------------------------

    def sign_wallet_action(
        self,
        to: str,
        data: str,
        nonce: int,
        *,
        proxy: Optional[str] = None,
        value: int = 0,
        operation: int = 0,
    ) -> str:
        """Sign a SafeTx wallet-action and return packed ``r||s||v`` hex."""
        if not self._private_key:
            raise RelayerError(
                "no private key available; set POLYMARKET_PRIVATE_KEY or pass "
                "private_key= to RelayerClient"
            )
        digest = safe_tx_digest(
            proxy or self.funder,
            to,
            data,
            nonce,
            value=value,
            operation=operation,
        )
        return sign_safe_tx_digest(self._private_key, digest)

    def build_action_payload(
        self,
        to: str,
        data: str,
        nonce: int,
        signature: str,
        *,
        proxy: Optional[str] = None,
        operation: int = 0,
    ) -> Dict[str, Any]:
        """Assemble the /submit POST body (no network)."""
        return {
            "from": self.owner,
            "to": to,
            "proxyWallet": proxy or self.funder,
            "data": data,
            "nonce": str(nonce),
            "signature": signature,
            # All signatureParams subfields are JSON strings per the official
            # /submit OpenAPI spec (gasPrice/operation/safeTxnGas/baseGas are
            # "0"-strings, not numbers); integers are rejected by the relayer.
            "signatureParams": {
                "gasPrice": "0",
                "operation": str(operation),
                "safeTxnGas": "0",
                "baseGas": "0",
                "gasToken": ZERO_ADDRESS,
                "refundReceiver": ZERO_ADDRESS,
            },
            "type": "SAFE",
        }

    def submit_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /submit -> relayer response ({transactionID, state}).

        Network side-effect.  Callers must gate this behind explicit live flags.
        """
        url = f"{self.base_url}/submit"
        resp = self._http().post(
            url,
            json=payload,
            headers=self._auth_headers(),
            timeout=30,
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise RelayerError(f"submit failed: {resp.status_code}")
        return resp.json()

    def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /transaction?id= -> transaction status."""
        url = f"{self.base_url}/transaction"
        resp = self._http().get(
            url,
            params={"id": transaction_id},
            headers=self._auth_headers(),
            timeout=30,
        )
        if getattr(resp, "status_code", 200) >= 400:
            raise RelayerError(f"transaction request failed: {resp.status_code}")
        return resp.json()

    # -- WALLET (deposit-wallet) branch -----------------------------------

    def sign_wallet_batch(
        self,
        calls: "list",
        nonce: int,
        *,
        deposit_wallet: Optional[str] = None,
        deadline: int = DEFAULT_DEADLINE,
    ) -> str:
        """Sign a deposit-wallet Batch wallet-action; return packed r||s||v hex.

        ``calls`` is a list of ``{"target","value","data"}`` dicts.  The signed
        EIP-712 digest binds the deposit wallet, nonce, deadline and the calls.
        The owner EOA's key produces the secp256k1 signature.
        """
        if not self._private_key:
            raise RelayerError(
                "no private key available; set POLYMARKET_PRIVATE_KEY or pass "
                "private_key= to RelayerClient"
            )
        digest = deposit_wallet_digest(
            deposit_wallet or self.funder,
            nonce,
            calls,
            deadline=deadline,
        )
        return sign_digest(self._private_key, digest)

    def build_wallet_payload(
        self,
        calls: "list",
        nonce: int,
        signature: str,
        *,
        deposit_wallet: Optional[str] = None,
        deadline: int = DEFAULT_DEADLINE,
        factory: str = FACTORY,
    ) -> Dict[str, Any]:
        """Assemble the /submit POST body for a deposit-wallet action (no network).

        Shape per the deposit-wallet spec::

            {type:"WALLET", from:<OWNER_EOA>, to:<FACTORY>, nonce:str,
             signature:"0x"+65B,
             depositWalletParams:{depositWallet:<DW>, deadline:str,
                                  calls:[{target,value,data}]}}
        """
        dw = deposit_wallet or self.funder
        return {
            "type": self.WALLET_TYPE_WALLET,
            "from": self.owner,
            "to": factory,
            "nonce": str(nonce),
            "signature": signature,
            "depositWalletParams": {
                "depositWallet": dw,
                "deadline": str(deadline),
                "calls": [
                    {
                        "target": c["target"],
                        "value": str(int(c.get("value", 0))),
                        "data": c["data"],
                    }
                    for c in calls
                ],
            },
        }

    def build_wallet_create_payload(
        self, *, factory: str = FACTORY
    ) -> Dict[str, Any]:
        """Assemble the /submit body to deploy the deposit wallet (no network)."""
        return {
            "type": "WALLET-CREATE",
            "from": self.owner,
            "to": factory,
        }
