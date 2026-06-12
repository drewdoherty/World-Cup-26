# ERC-7739 / sig-type-3 (POLY_1271 DepositWallet) order-signing: findings

Date: 2026-06-12
Investigator: read-only debug pass (no `src/` changes, no orders placed).

## Verdict

**(a) Our wrapper diverges.** The 400 `Invalid order payload` on POST /order for
signature_type=3 is caused by a bug in **our** code, not the server or an upstream
SDK bug. The divergence is a single root cause with a one-line fix.

The `contentsHash` (the EIP-712 struct hash of the order "contents") that we feed
into the ERC-7739 TypedDataSign wrapper is **double-keccak'd**. Because that wrong
value propagates into (1) the TypedDataSign struct hash, (2) the signed digest,
(3) the inner ECDSA signature, and (4) the `contentsHash` written into the wire
trailer, **every byte of the signature downstream of the order struct hash is
wrong**, so the deposit wallet's on-chain `isValidSignature` (and the CLOB's
pre-check) reject it.

The app domain separator and the TypedDataSign typestring are **byte-identical**
to the official SDK (parity tests for those pass).

## Root cause (exact byte X)

File: `src/wca/pm/signing.py`, function `order_struct_hash` (lines ~510-522):

```python
def order_struct_hash(order, *, exchange_version=EXCHANGE_V2) -> bytes:
    ...
    signable = encode_typed_data(full_message=typed)
    # signable.body == domainSeparator(32) || hashStruct(32); take the tail.
    return signable.body[32:64] if len(signable.body) >= 64 else keccak(signable.body)
```

The comment's assumption is **false for the installed eth-account (0.13.7)**. The
`SignableMessage` returned by `encode_typed_data` is split across three fields:

| field    | value                         | length |
|----------|-------------------------------|--------|
| `.version`| `b"\x01"`                    | 1      |
| `.header`| EIP-712 domain separator      | 32     |
| `.body`  | EIP-712 struct hash (hashStruct) | 32  |

So `len(signable.body) == 32`, the `>= 64` test fails, and we fall into the
`else` branch which returns `keccak(signable.body)` — i.e. `keccak(structHash)`,
a **double hash**. The intended `body[32:64]` slice was written for a layout where
`body == domainSep || structHash` (an older/other eth-account behaviour); that
concatenated layout does not hold here.

Empirical proof (test vector in `tests/test_erc7739_parity.py`):

```
official contents hash (abi_encode):  3ec2e678c42201f936acf7109970781a48d03cb31e478f462703fb41d6ee59b7
ours order_struct_hash:               70542ff8344d4005f5ea5ea48f753833b752bd4ac80e678c581d495a753b0409
encode_typed_data(...).body:          3ec2e678...d6ee59b7   <-- == official, and == what we SHOULD return
```

`70542f...` is exactly `keccak(3ec2e6...)`.

## Exact fix (do NOT apply per task scope)

In `src/wca/pm/signing.py::order_struct_hash`, return the struct hash directly:

```python
    signable = encode_typed_data(full_message=typed)
    return signable.body            # 32-byte EIP-712 hashStruct (contentsHash)
```

(Equivalently, recompute via the official `abi_encode([ORDER_TYPE_HASH, ...11 fields])`
path — they produce the identical 32 bytes.) Be aware other callers of
`order_struct_hash` would be affected, but in practice it is only used by the
sig-3 path, so the fix is self-contained. Do **not** touch `build_order_hash` /
`_eip712_digest` — those use the full `keccak(0x19||version||header||body)` form
and are correct (which is why sig-0/1/2 EIP-712 orders already work).

## Field-by-field diff vs official SDK

Official source read:
- `Polymarket/py-clob-client-v2` @ branch `main`
  - `py_clob_client_v2/order_utils/exchange_order_builder_v2.py`
    (`ExchangeOrderBuilderV2._build_poly_1271_order_signature`,
    `.app_domain_separator`, `ORDER_TYPE_STRING`, `SOLADY_TYPE_STRING`)
    raw: https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/main/py_clob_client_v2/order_utils/exchange_order_builder_v2.py
  - `py_clob_client_v2/order_utils/model/ctf_exchange_v2_typed_data.py`
  - `py_clob_client_v2/order_utils/model/order_data_v2.py` (`order_to_json_v2` wire shape)
  - `py_clob_client_v2/order_utils/model/signature_type_v2.py` (POLY_1271 = 3)
  - `py_clob_client_v2/signer.py`
  - `examples/orders/gtc_limit_buy_deposit_wallet.py`,
    `examples/keys/signature_types.py`

| concern | official SDK | ours | match? |
|---|---|---|---|
| ORDER typestring | `Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)` | identical (`ORDER_TYPE_STRING_V2`) | YES |
| TypedDataSign typestring | `TypedDataSign(Order contents,string name,string version,uint256 chainId,address verifyingContract,bytes32 salt)` + ORDER typestring | identical (`_typed_data_sign_type_string`) | YES |
| app domain separator | `keccak(abi_encode([DOMAIN_TYPE_HASH, name_hash, version_hash, chainId, exchange])` over CTF Exchange V2 domain | identical (`_eip712_domain_separator` over name "Polymarket CTF Exchange", version "2", chainId 137, V2 exchange) | YES |
| **contentsHash** | `keccak(abi_encode([ORDER_TYPE_HASH, 11 fields]))` = the EIP-712 struct hash | `order_struct_hash` returns **keccak(structHash)** (double hash) | **NO — bug** |
| TypedDataSign struct hash | `keccak(abi_encode([SOLADY_TYPE_HASH, contentsHash, DepositWallet name_hash, version_hash, chainId, signer, salt32]))` | same shape (manual keccak of concatenated 32-byte words; address left-padded — equivalent to abi_encode) but fed the wrong contentsHash | shape YES, value NO (inherits bug) |
| account-domain `verifyingContract` in wrapper | `message["signer"]` (= deposit wallet) | `deposit_wallet` arg (= maker = signer) | YES |
| account-domain name/version | "DepositWallet" / "1" | identical | YES |
| account-domain salt | bytes32(0) | bytes32(0) | YES |
| signed digest | `keccak(0x1901 || appDomainSep || typedDataSignStructHash)` | identical formula | formula YES, value NO (inherits bug) |
| inner sig encoding | `Account._sign_hash(digest).signature.hex()` (r||s||v, v=27/28 from eth-account) | `r||s||(v>=27?v:v+27)` re-packed | equivalent (both yield canonical 65-byte r||s||v) |
| wire trailer | `appDomainSep(32) || contentsHash(32) || contentsType(bytes) || uint16_be(len(contentsType))` | identical layout | layout YES; contentsHash bytes wrong (inherits bug) |
| `len(contentsType)` | `len(ORDER_TYPE_STRING)` (char count of the ASCII string) | `len(contents_type_bytes)` | equivalent (ASCII) |
| maker / signer for sig-3 | both = deposit wallet (funder) | both = deposit wallet (funder) | YES |
| extra wire fields for sig-3 | none beyond the standard order JSON (`order_to_json_v2`); same `owner`/`orderType`/`deferExec`/`postOnly` envelope as every order | n/a (our envelope unchanged) | YES |

No sig-3-specific extra wire fields exist in the SDK; the deposit-wallet path
differs ONLY in how the `signature` string is built. So the envelope you verified
is fine.

## Upstream GitHub issues (item 4)

py-clob-client-v2 issues #70/#51 and clob-client-v2 #65 could not be retrieved in
this environment (no authenticated GitHub access; `gh` not logged in and the
issues API/HTML was not fetchable here). However, the question they would answer
is moot for our case: the parity test proves our 400 is explained entirely by a
local double-hash bug in `order_struct_hash`. Any upstream L1-key-derivation bug
discussed in those issues would manifest as an **auth** failure
(GET /balance-allowance, key creation), not a per-order `Invalid order payload` —
and our L1/L2 auth is already verified working (correct $297 balance + MAX
allowances). Recommend a quick manual read of those three issues to confirm they
are L1-derivation-only before relying on this, but they do not change the verdict.

## Test result

`tests/test_erc7739_parity.py` (re-implements the official algorithm inline and
asserts byte equality):

```
.venv/bin/python -m pytest -q tests/test_erc7739_parity.py
..FF
FAILED tests/test_erc7739_parity.py::test_contents_hash_matches   <-- the bug
FAILED tests/test_erc7739_parity.py::test_full_signature_matches  <-- inherits it
2 failed, 2 passed
```

Passing: `test_typehash_strings_match` (typestrings byte-identical),
`test_app_domain_separator_matches` (app domain separator byte-identical).
Failing: contentsHash (ours = keccak of the correct value) and, consequently,
the full wire signature. The failure diff shows the appDomainSep segment of the
two wire signatures is identical and the contentsHash segment is where they part —
exactly localising the bug to the contents struct hash.

## Files written
- `tests/test_erc7739_parity.py`
- `docs/research/erc7739_findings.md` (this file)
