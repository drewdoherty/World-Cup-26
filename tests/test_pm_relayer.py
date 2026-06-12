"""Tests for src/wca/pm/relayer.py and scripts/wca_pm_approve.py.

Coverage:
- approve / setApprovalForAll calldata are byte-exact vs. known-good vectors.
- the SafeTx EIP-712 digest matches an independent reconstruction, and a
  signature over it recovers to the signing EOA.
- the relayer client posts the spec-shaped payload (with a fake session) and
  never POSTs on read paths.
- the approve script is dry-run by default, never POSTs without both flags,
  and refuses the live path when only one flag is present.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from wca.pm import relayer, signing


# ---------------------------------------------------------------------------
# Test key (well-known throwaway; address derived below).  NEVER a real key.
# ---------------------------------------------------------------------------

TEST_KEY = "0x4646464646464646464646464646464646464646464646464646464646464646"


def _addr() -> str:
    from eth_account import Account

    return Account.from_key(TEST_KEY).address


# ---------------------------------------------------------------------------
# Calldata vectors.
# ---------------------------------------------------------------------------


def test_approve_calldata_max_byte_exact():
    # approve(0xE111180000d2663C0091e4f400237545B87B996B, MAX_UINT256)
    cd = relayer.build_approve_calldata(relayer.EXCHANGE_STD)
    expected = (
        "0x095ea7b3"
        "000000000000000000000000e111180000d2663c0091e4f400237545b87b996b"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    assert cd == expected


def test_approve_calldata_specific_amount():
    cd = relayer.build_approve_calldata(
        "0x0000000000000000000000000000000000000001", amount=1
    )
    expected = (
        "0x095ea7b3"
        "0000000000000000000000000000000000000000000000000000000000000001"
        "0000000000000000000000000000000000000000000000000000000000000001"
    )
    assert cd == expected
    # selector is exactly the keccak4 of the ABI signature.
    assert cd[2:10] == "095ea7b3"


def test_set_approval_for_all_calldata_byte_exact():
    cd = relayer.build_set_approval_for_all_calldata(relayer.EXCHANGE_NEG_RISK, True)
    expected = (
        "0xa22cb465"
        "000000000000000000000000e2222d279d744050d28e00520010520000310f59"
        "0000000000000000000000000000000000000000000000000000000000000001"
    )
    assert cd == expected


def test_approve_calldata_rejects_bad_address():
    with pytest.raises(ValueError):
        relayer.build_approve_calldata("0x1234")


# ---------------------------------------------------------------------------
# SafeTx digest + signing.
# ---------------------------------------------------------------------------


def test_domain_separator_matches_independent_keccak():
    from eth_utils import keccak

    proxy = relayer.PROXY_SAFE
    dom_th = keccak(b"EIP712Domain(uint256 chainId,address verifyingContract)")
    encoded = (
        dom_th
        + (137).to_bytes(32, "big")
        + bytes.fromhex(relayer._strip0x(proxy).lower().rjust(64, "0"))
    )
    assert relayer.safe_domain_separator(proxy) == keccak(encoded)


def test_safe_tx_digest_recovers_to_eoa():
    from eth_account import Account
    from eth_account.messages import encode_defunct  # noqa: F401  (sanity import)

    data = relayer.build_approve_calldata(relayer.EXCHANGE_STD)
    digest = relayer.safe_tx_digest(
        relayer.PROXY_SAFE,
        relayer.PUSD_TOKEN,
        data,
        nonce=7,
    )
    assert isinstance(digest, bytes) and len(digest) == 32

    sig = relayer.sign_safe_tx_digest(TEST_KEY, digest)
    raw = bytes.fromhex(relayer._strip0x(sig))
    assert len(raw) == 65
    v = raw[64]
    assert v in (27, 28)

    # Recover the signer from the raw digest + packed signature.
    rec = Account._recover_hash(digest, signature=raw)
    assert rec.lower() == _addr().lower()


def test_safe_tx_typehash_constant():
    from eth_utils import keccak

    th = keccak(
        b"SafeTx(address to,uint256 value,bytes data,uint8 operation,"
        b"uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,"
        b"address refundReceiver,uint256 nonce)"
    )
    assert th.hex() == relayer.SAFE_TX_TYPEHASH


# ---------------------------------------------------------------------------
# Deposit-wallet (WALLET) branch: typehashes, digest, payload, sign+recover.
# ---------------------------------------------------------------------------


def test_deposit_wallet_typehashes_match_spec():
    from eth_utils import keccak

    call_th = keccak(relayer.CALL_TYPE_STRING.encode())
    batch_th = keccak(relayer.BATCH_TYPE_STRING.encode())
    assert call_th.hex() == relayer.CALL_TYPEHASH
    assert batch_th.hex() == relayer.BATCH_TYPEHASH
    # Exact spec vectors.
    assert relayer.BATCH_TYPEHASH == (
        "712ef66e8362c387e862cabf0923c209db0fa24cfc97d25eccba7c86f3ee1dd3"
    )
    assert relayer.CALL_TYPEHASH == (
        "84fa2cf05cd88e992eae77e851af68a4ee278dcff6ef504e487a55b3baadfbe5"
    )


def test_deposit_wallet_domain_separator_independent():
    from eth_utils import keccak

    dw = relayer.DEPOSIT_WALLET
    th = keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,"
        b"address verifyingContract)"
    )
    encoded = (
        th
        + keccak(b"DepositWallet")
        + keccak(b"1")
        + (137).to_bytes(32, "big")
        + bytes.fromhex(relayer._strip0x(dw).lower().rjust(64, "0"))
    )
    assert relayer.deposit_wallet_domain_separator(dw) == keccak(encoded)


def test_deposit_wallet_digest_recovers_to_eoa():
    from eth_account import Account

    calls = [
        {
            "target": relayer.PUSD_TOKEN,
            "value": 0,
            "data": relayer.build_approve_calldata(relayer.EXCHANGE_STD),
        }
    ]
    digest = relayer.deposit_wallet_digest(
        relayer.DEPOSIT_WALLET, 4, calls, deadline=relayer.MAX_UINT256
    )
    assert isinstance(digest, bytes) and len(digest) == 32

    sig = relayer.sign_digest(TEST_KEY, digest)
    raw = bytes.fromhex(relayer._strip0x(sig))
    assert len(raw) == 65 and raw[64] in (27, 28)
    rec = Account._recover_hash(digest, signature=raw)
    assert rec.lower() == _addr().lower()


def test_wallet_payload_shape():
    sess = _FakeSession()
    c = relayer.RelayerClient(
        private_key=TEST_KEY,
        funder=relayer.DEPOSIT_WALLET,
        session=sess,
        env={},
        wallet_type="WALLET",
    )
    calls = [
        {
            "target": relayer.PUSD_TOKEN,
            "value": 0,
            "data": relayer.build_approve_calldata(relayer.EXCHANGE_STD),
        },
        {
            "target": relayer.CTF_ADDRESS,
            "value": 0,
            "data": relayer.build_set_approval_for_all_calldata(
                relayer.EXCHANGE_STD, True
            ),
        },
    ]
    nonce = 4
    sig = c.sign_wallet_batch(calls, nonce)
    payload = c.build_wallet_payload(calls, nonce, sig)

    assert payload["type"] == "WALLET"
    assert payload["from"].lower() == _addr().lower()
    assert payload["to"] == relayer.FACTORY
    assert payload["nonce"] == "4"
    assert payload["signature"] == sig
    dwp = payload["depositWalletParams"]
    assert dwp["depositWallet"] == relayer.DEPOSIT_WALLET
    assert dwp["deadline"] == str(relayer.MAX_UINT256)
    assert len(dwp["calls"]) == 2
    assert dwp["calls"][0]["target"] == relayer.PUSD_TOKEN
    assert dwp["calls"][0]["value"] == "0"
    # No network on the build path.
    assert sess.posts == []


def test_wallet_create_payload_shape():
    c = relayer.RelayerClient(
        private_key=TEST_KEY, funder=relayer.DEPOSIT_WALLET, env={}, owner=_addr()
    )
    p = c.build_wallet_create_payload()
    assert p["type"] == "WALLET-CREATE"
    assert p["from"].lower() == _addr().lower()
    assert p["to"] == relayer.FACTORY


def test_resolve_wallet_type_defaults_to_wallet():
    c = relayer.RelayerClient(private_key=TEST_KEY, env={})
    assert c.resolve_wallet_type() == "WALLET"
    # Explicit env override is honoured.
    c2 = relayer.RelayerClient(
        private_key=TEST_KEY, env={"POLYMARKET_WALLET_TYPE": "SAFE"}
    )
    assert c2.resolve_wallet_type() == "SAFE"


def test_sign_wallet_batch_without_key_raises():
    c = relayer.RelayerClient(private_key=None, funder=relayer.DEPOSIT_WALLET, env={})
    with pytest.raises(relayer.RelayerError):
        c.sign_wallet_batch(
            [{"target": relayer.PUSD_TOKEN, "value": 0, "data": "0x095ea7b3"}], 0
        )


# ---------------------------------------------------------------------------
# Deposit-wallet CLOB sig type 3 (POLY_1271) — ERC-7739 wrapped signature.
# ---------------------------------------------------------------------------


def _dw_order(sig_type: int = signing.SIG_POLY_1271):
    return {
        "salt": 1,
        "maker": relayer.DEPOSIT_WALLET,
        "signer": relayer.DEPOSIT_WALLET,
        "tokenId": 12345,
        "makerAmount": 1000,
        "takerAmount": 2000,
        "side": signing.SIDE_BUY,
        "signatureType": sig_type,
        "timestamp": 1700000000000,
        "metadata": signing.ZERO_BYTES32,
        "builder": signing.ZERO_BYTES32,
    }


def test_erc7739_1271_signature_layout():
    sig = signing.build_erc7739_1271_signature(
        TEST_KEY, _dw_order(), deposit_wallet=relayer.DEPOSIT_WALLET
    )
    raw = bytes.fromhex(relayer._strip0x(sig))
    # 65 inner + 32 appDomainSep + 32 contentsHash + N typeString + 2 len.
    assert len(raw) == 317
    type_len = int.from_bytes(raw[-2:], "big")
    assert type_len == 186
    assert type_len == len(signing.ORDER_TYPE_STRING_V2.encode())
    # The appended type string equals the V2 Order type string.
    type_str = raw[-(2 + type_len):-2].decode()
    assert type_str == signing.ORDER_TYPE_STRING_V2


def test_build_signed_order_sig_type_3_maker_is_deposit_wallet():
    payload = signing.build_signed_order(
        TEST_KEY,
        signing.OrderArgs(token_id="12345", price=0.5, size=10.0, side="BUY"),
        funder=relayer.DEPOSIT_WALLET,
        signature_type=signing.SIG_POLY_1271,
        salt=1,
        timestamp_ms=1700000000000,
    )
    # maker == signer == deposit wallet for the 1271 path.
    assert payload["maker"] == relayer.DEPOSIT_WALLET
    assert payload["signer"] == relayer.DEPOSIT_WALLET
    assert payload["signatureType"] == signing.SIG_POLY_1271
    raw = bytes.fromhex(relayer._strip0x(payload["signature"]))
    assert len(raw) == 317


def test_sig_type_3_requires_funder():
    with pytest.raises(ValueError):
        signing.build_signed_order(
            TEST_KEY,
            signing.OrderArgs(token_id="1", price=0.5, size=10.0, side="BUY"),
            signature_type=signing.SIG_POLY_1271,
        )


def test_default_sig_type_unchanged():
    # Sanity: the default signing path is still plain EIP-712 (65-byte sig),
    # i.e. we did NOT silently switch the default to 1271.
    payload = signing.build_signed_order(
        TEST_KEY,
        signing.OrderArgs(token_id="1", price=0.5, size=10.0, side="BUY"),
        salt=1,
        timestamp_ms=1700000000000,
    )
    raw = bytes.fromhex(relayer._strip0x(payload["signature"]))
    assert len(raw) == 65


# ---------------------------------------------------------------------------
# Relayer client transport with a fake session.
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload: Dict[str, Any], status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeSession:
    def __init__(self) -> None:
        self.gets: List[Dict[str, Any]] = []
        self.posts: List[Dict[str, Any]] = []
        self.nonce_resp = {"nonce": 42}
        self.wallet_resp = {"deployed": True}
        self.submit_resp = {"transactionID": "tx-1", "state": "STATE_NEW"}
        self.tx_resp = {"transactionID": "tx-1", "state": "STATE_CONFIRMED"}

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": params})
        if url.endswith("/nonce"):
            return _Resp(self.nonce_resp)
        if url.endswith("/wallet"):
            return _Resp(self.wallet_resp)
        if url.endswith("/transaction"):
            return _Resp(self.tx_resp)
        return _Resp({}, status=404)

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return _Resp(self.submit_resp)


def _client(session: _FakeSession) -> relayer.RelayerClient:
    return relayer.RelayerClient(
        private_key=TEST_KEY,
        funder=relayer.PROXY_SAFE,
        session=session,
        env={},
    )


def test_get_nonce_does_not_post():
    sess = _FakeSession()
    c = _client(sess)
    assert c.get_nonce() == 42
    assert sess.posts == []
    assert sess.gets and sess.gets[0]["url"].endswith("/nonce")
    assert sess.gets[0]["params"]["type"] == "SAFE"


def test_wallet_deployed_read_only():
    sess = _FakeSession()
    c = _client(sess)
    assert c.wallet_deployed() is True
    assert sess.posts == []


def test_submit_action_payload_shape():
    sess = _FakeSession()
    c = _client(sess)
    nonce = 7
    data = relayer.build_approve_calldata(relayer.EXCHANGE_STD)
    sig = c.sign_wallet_action(relayer.PUSD_TOKEN, data, nonce)
    payload = c.build_action_payload(relayer.PUSD_TOKEN, data, nonce, sig)

    assert payload["from"].lower() == _addr().lower()
    assert payload["to"] == relayer.PUSD_TOKEN
    assert payload["proxyWallet"] == relayer.PROXY_SAFE
    assert payload["data"] == data
    assert payload["nonce"] == "7"
    assert payload["type"] == "SAFE"
    sp = payload["signatureParams"]
    # Per the official /submit spec these are JSON strings, not numbers.
    assert sp["gasPrice"] == "0"
    assert sp["operation"] == "0"
    assert sp["safeTxnGas"] == "0"
    assert sp["baseGas"] == "0"
    assert sp["gasToken"] == relayer.ZERO_ADDRESS
    assert sp["refundReceiver"] == relayer.ZERO_ADDRESS

    resp = c.submit_action(payload)
    assert resp["transactionID"] == "tx-1"
    assert sess.posts and sess.posts[0]["url"].endswith("/submit")


def test_signing_without_key_raises():
    c = relayer.RelayerClient(private_key=None, funder=relayer.PROXY_SAFE, env={})
    with pytest.raises(relayer.RelayerError):
        c.sign_wallet_action(relayer.PUSD_TOKEN, "0x095ea7b3", 0)


# ---------------------------------------------------------------------------
# Script: dry-run by default, refuses live without both flags.
# ---------------------------------------------------------------------------


def _load_script():
    path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "wca_pm_approve.py"
    )
    spec = importlib.util.spec_from_file_location("wca_pm_approve", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _NoNetSession:
    """Session that fails the test if any network call is attempted."""

    def get(self, *a, **k):  # pragma: no cover - must not be called in dry-run
        raise AssertionError("dry-run must not perform GET")

    def post(self, *a, **k):  # pragma: no cover - must not be called in dry-run
        raise AssertionError("dry-run must not perform POST")


def test_script_dry_run_default_no_network(capsys):
    mod = _load_script()
    rc = mod.main(
        argv=[],
        env={"POLYMARKET_PRIVATE_KEY": TEST_KEY},
        session=_NoNetSession(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    # The four planned actions are previewed.
    assert "approve" in out.lower()
    assert "setApprovalForAll" in out or "setapprovalforall" in out.lower()


def test_script_live_refused_without_yes(capsys):
    mod = _load_script()
    rc = mod.main(
        argv=[],
        env={"POLYMARKET_PRIVATE_KEY": TEST_KEY, "PM_APPROVE_LIVE": "1"},
        session=_NoNetSession(),
    )
    assert rc != 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out or "refus" in out.lower()


def test_script_live_refused_without_env(capsys):
    mod = _load_script()
    rc = mod.main(
        argv=["--yes"],
        env={"POLYMARKET_PRIVATE_KEY": TEST_KEY},
        session=_NoNetSession(),
    )
    assert rc != 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out or "refus" in out.lower()


def test_script_never_logs_key(capsys):
    mod = _load_script()
    mod.main(argv=[], env={"POLYMARKET_PRIVATE_KEY": TEST_KEY}, session=_NoNetSession())
    out = capsys.readouterr().out
    assert TEST_KEY not in out
    assert relayer._strip0x(TEST_KEY) not in out
