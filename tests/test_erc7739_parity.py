"""ERC-7739 / Solady TypedDataSign parity test for Polymarket sig-type-3 orders.

Compares our hand-rolled wrapper in ``wca.pm.signing.build_erc7739_1271_signature``
against a faithful inline transcription of the OFFICIAL py-clob-client-v2 algorithm:

    Polymarket/py-clob-client-v2 @ main
    py_clob_client_v2/order_utils/exchange_order_builder_v2.py
      ::ExchangeOrderBuilderV2._build_poly_1271_order_signature
    (raw: https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/main/
     py_clob_client_v2/order_utils/exchange_order_builder_v2.py)

The test asserts byte-equality of (a) the app domain separator, (b) the contents
struct hash, (c) the TypedDataSign struct hash, (d) the final signed digest, and
(e) the full wire signature. It is EXPECTED to surface any divergence as a diff.
"""
import pytest

eth_abi = pytest.importorskip("eth_abi")
from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import keccak as _keccak  # noqa: E402

from wca.pm import signing  # noqa: E402

# --- fixed test vector -----------------------------------------------------
TEST_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
DEPOSIT_WALLET = "0x2222222222222222222222222222222222222222"
CHAIN_ID = 137
CONTRACT = signing.CTF_EXCHANGE_V2  # standard (neg_risk=False) V2 exchange

ORDER_MSG = {
    "salt": 123456789,
    "maker": DEPOSIT_WALLET,
    "signer": DEPOSIT_WALLET,
    "tokenId": 71321045679252212594626385532706912750332728571942532289631379312455583992563,
    "makerAmount": 40_000_000,
    "takerAmount": 100_000_000,
    "side": signing.SIDE_BUY,
    "signatureType": signing.SIG_POLY_1271,
    "timestamp": 1739000000000,
    "metadata": signing.ZERO_BYTES32,
    "builder": signing.ZERO_BYTES32,
}

# --- official SDK algorithm, transcribed inline ----------------------------
ORDER_TYPE_STRING = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
SOLADY_TYPE_STRING = (
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)"
    f"{ORDER_TYPE_STRING}"
)
DOMAIN_TYPE_STRING = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
ORDER_TYPE_HASH = _keccak(text=ORDER_TYPE_STRING)
DOMAIN_TYPE_HASH = _keccak(text=DOMAIN_TYPE_STRING)
SOLADY_TYPE_HASH = _keccak(text=SOLADY_TYPE_STRING)
DEPOSIT_WALLET_NAME_HASH = _keccak(text="DepositWallet")
DEPOSIT_WALLET_VERSION_HASH = _keccak(text="1")
CTF_NAME_HASH = _keccak(text="Polymarket CTF Exchange")
CTF_VERSION_HASH = _keccak(text="2")
SALT32 = bytes(32)


def _b32(hexstr):
    return bytes.fromhex(hexstr.replace("0x", "").zfill(64))


def official_app_domain_separator():
    return _keccak(
        primitive=abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPE_HASH, CTF_NAME_HASH, CTF_VERSION_HASH, CHAIN_ID, CONTRACT],
        )
    )


def official_contents_hash(m):
    return _keccak(
        primitive=abi_encode(
            ["bytes32", "uint256", "address", "address", "uint256", "uint256",
             "uint256", "uint8", "uint8", "uint256", "bytes32", "bytes32"],
            [ORDER_TYPE_HASH, int(m["salt"]), m["maker"], m["signer"],
             int(m["tokenId"]), int(m["makerAmount"]), int(m["takerAmount"]),
             int(m["side"]), int(m["signatureType"]), int(m["timestamp"]),
             _b32(m["metadata"]), _b32(m["builder"])],
        )
    )


def official_typed_data_sign_struct_hash(contents_hash, m):
    return _keccak(
        primitive=abi_encode(
            ["bytes32", "bytes32", "bytes32", "bytes32", "uint256", "address", "bytes32"],
            [SOLADY_TYPE_HASH, contents_hash, DEPOSIT_WALLET_NAME_HASH,
             DEPOSIT_WALLET_VERSION_HASH, CHAIN_ID, m["signer"], SALT32],
        )
    )


def official_signature(pk, m):
    app_sep = official_app_domain_separator()
    ch = official_contents_hash(m)
    tdsh = official_typed_data_sign_struct_hash(ch, m)
    digest = _keccak(primitive=b"\x19\x01" + app_sep + tdsh)
    signed = Account._sign_hash(digest, private_key=pk)
    inner = signed.signature.hex()
    if inner.startswith("0x"):
        inner = inner[2:]
    contents_type = ORDER_TYPE_STRING.encode("utf-8").hex()
    contents_type_len = len(ORDER_TYPE_STRING).to_bytes(2, "big").hex()
    return (
        "0x" + inner + app_sep.hex() + ch.hex() + contents_type + contents_type_len
    ), app_sep, ch, tdsh, digest


# --- our implementation ----------------------------------------------------
def our_signature(pk, m):
    return signing.build_erc7739_1271_signature(
        pk, m, deposit_wallet=DEPOSIT_WALLET, neg_risk=False,
        exchange_version=signing.EXCHANGE_V2,
    )


# --- assertions ------------------------------------------------------------
def test_typehash_strings_match():
    assert signing.ORDER_TYPE_STRING_V2 == ORDER_TYPE_STRING
    # Our TypedDataSign typestring vs official SOLADY_TYPE_STRING.
    ours = signing._typed_data_sign_type_string(ORDER_TYPE_STRING).decode()
    assert ours == SOLADY_TYPE_STRING, (
        "TypedDataSign typestring diverges:\n ours=%r\n offc=%r" % (ours, SOLADY_TYPE_STRING)
    )


def test_app_domain_separator_matches():
    off, app_sep, _, _, _ = official_signature(TEST_PK, ORDER_MSG)
    ours = signing._eip712_domain_separator(
        "Polymarket CTF Exchange", "2", CHAIN_ID, CONTRACT
    )
    assert ours == app_sep


def test_contents_hash_matches():
    ch = official_contents_hash(ORDER_MSG)
    ours = signing.order_struct_hash(ORDER_MSG, exchange_version=signing.EXCHANGE_V2)
    assert ours == ch


def test_full_signature_matches():
    off, app_sep, ch, tdsh, digest = official_signature(TEST_PK, ORDER_MSG)
    ours = our_signature(TEST_PK, ORDER_MSG)
    assert ours == off, "wire signature diverges:\n ours=%s\n offc=%s" % (ours, off)
