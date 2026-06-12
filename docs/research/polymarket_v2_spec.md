# Polymarket CTF Exchange V2 — EIP-712 Signing Spec

Research phase only. NO code written. Every load-bearing fact below is backed by a
**primary source** (deployed bytecode read over RPC, raw GitHub source, or the official
docs/migration page) with a URL + quote/line. Where a secondary summary disagreed with a
primary source, the primary source wins and the disagreement is logged in §7 Caveats.

Date of capture: 2026-06-12. Chain: Polygon mainnet (chainId 137).

---

## 0. TL;DR for the Port phase

V2 is a breaking change from our current V1 signer (`wca.pm.signing` / `wca.pm.trader`):

- **Domain version bumps `"1"` → `"2"`**; `verifyingContract` changes to the V2 exchange
  addresses; selected by `(negRisk)`.
- **Order struct goes from 12 fields → 11 signed fields.** Removed: `taker`, `expiration`,
  `nonce`, `feeRateBps`. Added: `timestamp` (uint256, ms), `metadata` (bytes32),
  `builder` (bytes32). `salt` + `signer` + `tokenId` + `makerAmount` + `takerAmount` +
  `side` + `signatureType` + `maker` are retained.
- **Collateral changes USDC.e → pUSD** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
  (still 6 dp). USDC.e survives only as the CTF position-id derivation collateral
  (`getCtfCollateral()`), not as what orders are denominated in.
- **Fees leave the signed struct entirely** (`feeRateBps` is gone). On-chain
  `getMaxFeeRate()` is currently `0`. Fee handling is off-chain at order-build time.
- **`signatureType = 2` still = POLY_GNOSIS_SAFE.** Our account (EOA signer
  `0x721A9E…EB76`, funder Safe `0x40231C…E191`, sig_type 2) is unchanged in semantics —
  but the bytes you sign change because the struct/domain changed.
- **Wire envelope** to `POST /order`: `{order:{…}, owner, orderType, deferExec, postOnly}`.
  The nested `order` carries an `expiration` wire field (default `"0"`) that is **NOT** in
  the signed struct, plus the `signature`.

---

## 1. EIP-712 DOMAIN (all four cases)

The domain type is the standard 4-field EIP712Domain (no `salt`):

```
EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
```

Source — SDK typed-data model (raw):
`https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/main/py_clob_client_v2/order_utils/model/ctf_exchange_v2_typed_data.py`
```
CTF_EXCHANGE_V2_DOMAIN_NAME    = "Polymarket CTF Exchange"
CTF_EXCHANGE_V2_DOMAIN_VERSION = "2"
EIP712_DOMAIN = [name:string, version:string, chainId:uint256, verifyingContract:address]
```

Source — on-chain `Hashing.sol` mixin (raw):
`https://raw.githubusercontent.com/Polymarket/ctf-exchange-v2/main/src/exchange/mixins/Hashing.sol`
```
string internal constant DOMAIN_NAME    = "Polymarket CTF Exchange";  // line 11
string internal constant DOMAIN_VERSION = "2";                        // line 12
```

`name` and `version` are identical for standard and neg-risk; only `verifyingContract`
differs. Confirmed by reading `eip712Domain()` (EIP-5267, selector `0x84b0196e`) directly
from each deployed contract over a public Polygon RPC (`polygon-bor-rpc.publicnode.com`),
decoded below.

| Case | name | version | chainId | verifyingContract |
|------|------|---------|---------|-------------------|
| **V2 standard exchange** | `Polymarket CTF Exchange` | `2` | `137` | `0xE111180000d2663C0091e4f400237545B87B996B` |
| **V2 neg-risk exchange** | `Polymarket CTF Exchange` | `2` | `137` | `0xe2222d279d744050d28e00520010520000310F59` |
| V1 standard (ours today) | `Polymarket CTF Exchange` | `1` | `137` | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| V1 neg-risk (ours today) | `Polymarket CTF Exchange` | `1` | `137` | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |

On-chain `eip712Domain()` raw return for **standard** `0xE111…996B` decodes to:
`chainId=0x89 (137)`, `verifyingContract=0xe111180000d2663c0091e4f400237545b87b996b`,
`name="Polymarket CTF Exchange"` (hex `506f6c796d61726b6574204354462045786368616e6765`, len 23),
`version="2"` (hex `32`, len 1).

On-chain `eip712Domain()` raw return for **neg-risk** `0xe2222…0F59` decodes to:
`chainId=0x89 (137)`, `verifyingContract=0xe2222d279d744050d28e00520010520000310f59`,
same name, same version `"2"`.

The V2 addresses are also pinned in the SDK config (raw):
`https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/main/py_clob_client_v2/config.py`
```
137: ContractConfig(
    exchange            = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",   # V1 standard
    neg_risk_exchange   = "0xC5d563A36AE78145C45a50134d48A1215220f80a",   # V1 neg-risk
    collateral          = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",   # pUSD
    conditional_tokens  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    exchange_v2         = "0xE111180000d2663C0091e4f400237545B87B996B",   # V2 standard
    neg_risk_exchange_v2= "0xe2222d279d744050d28e00520010520000310F59",   # V2 neg-risk
)
```

**verifyingContract selection rule (Port phase MUST implement):** pick `exchange_v2` for
standard markets and `neg_risk_exchange_v2` for neg-risk markets (per-market `neg_risk`
flag; CLOB exposes it at `GET /neg-risk`). In the SDK this is the `contract_address` passed
into `ExchangeOrderBuilderV2.__init__`, which becomes `domain.verifyingContract`
(builder lines 69, 136 of `exchange_order_builder_v2.py`).

---

## 2. The V2 Order STRUCT (exact)

### 2a. Signed struct — 11 fields (the EIP-712 message)

Source — on-chain `Structs.sol` (raw), lines 25–57:
`https://raw.githubusercontent.com/Polymarket/ctf-exchange-v2/main/src/exchange/libraries/Structs.sol`

```solidity
bytes32 constant ORDER_TYPEHASH = 0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589;
// keccak256(
//   "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,
//    uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,
//    bytes32 builder)" )

struct Order {
    uint256       salt;          // unique entropy
    address       maker;         // source of funds (our funder Safe)
    address       signer;        // EOA that signs (our EOA)
    uint256       tokenId;       // CTF ERC1155 asset id
    uint256       makerAmount;   // max tokens to sell
    uint256       takerAmount;   // min tokens to receive
    Side          side;          // uint8: BUY=0, SELL=1
    SignatureType signatureType; // uint8: EOA=0, POLY_PROXY=1, POLY_GNOSIS_SAFE=2, POLY_1271=3
    uint256       timestamp;     // unix ms at order creation
    bytes32       metadata;      // hashed order metadata
    bytes32       builder;       // builder code (origin attribution)
    bytes         signature;     // EXCLUDED from hash (see below)
}
```

The EIP-712 type string for hashing (field order + Solidity types), confirmed identical in
the SDK builder `ORDER_TYPE_STRING` and the on-chain typehash comment:

```
Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)
```

**Independently verified:** `keccak256(text=ORDER_TYPE_STRING)` ==
`0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589`, byte-for-byte equal
to the on-chain `ORDER_TYPEHASH` constant. (Computed locally with `eth_utils.keccak`.)

**`signature` is excluded from the hash.** Source — `Hashing.sol` `_createStructHash`
(lines 26–37): doc comment *"This does not include the signature; the signature is
downstream of this hash"*, and the assembly hashes `keccak256(sub(order,0x20), 0x180)`.
`0x180 = 384 bytes = 12 × 32-byte slots = ORDER_TYPEHASH + the 11 fields above`. (`side`
and `signatureType` each occupy a full 32-byte slot, ABI-padded.) So exactly the 11 fields
are hashed; `signature` is not.

SDK message construction (raw `exchange_order_builder_v2.py`, lines 138–151) populates the
same 11 fields and signs via `eth_account.encode_typed_data(full_message=…)` →
`Account.sign_message`. `metadata` / `builder` are passed as 32-byte values
(`_hex_to_bytes32`); default `BYTES32_ZERO`. `timestamp` defaults to
`str(time.time_ns() // 1_000_000)` (ms). `salt` from `generate_order_salt()`.

### 2b. Diff vs our current V1 12-field struct

V1 (current) signed order:
`{salt, maker, signer, taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType}`.

| Field | V1 | V2 | Change |
|-------|----|----|--------|
| salt | uint256 | uint256 | unchanged |
| maker | address | address | unchanged |
| signer | address | address | unchanged |
| **taker** | address | — | **REMOVED** from signed struct |
| tokenId | uint256 | uint256 | unchanged |
| makerAmount | uint256 | uint256 | unchanged |
| takerAmount | uint256 | uint256 | unchanged |
| **expiration** | uint256 | — | **REMOVED** from signed struct (survives as wire-only field, default "0") |
| **nonce** | uint256 | — | **REMOVED** (V2 tracks orders by hash + `OrderStatus`, no nonce cancel) |
| **feeRateBps** | uint256 | — | **REMOVED** (fees no longer in signed order) |
| side | uint8 | uint8 | unchanged (BUY=0, SELL=1) |
| signatureType | uint8 | uint8 | unchanged (2 = POLY_GNOSIS_SAFE) |
| **timestamp** | — | uint256 | **ADDED** (unix ms, order entropy/uniqueness) |
| **metadata** | — | bytes32 | **ADDED** |
| **builder** | — | bytes32 | **ADDED** (builder-code attribution, replaces builder HMAC headers) |

Field **ordering** also changed: V2 places `side`,`signatureType` immediately after
`takerAmount`, then `timestamp`,`metadata`,`builder`. The signer MUST emit fields in the V2
order above; using V1 ordering yields a different (invalid) hash.

### 2c. Other V2 struct facts (informational)

- `OrderStatus { bool filled; uint248 remaining; }` — V2 tracks fills by order hash, not
  nonces. README: *"Nonce-based order cancellation removed. Orders are tracked by hash with
  `OrderStatus`."*
- `enum MatchType { COMPLEMENTARY, MINT, MERGE }` — matching modes (no signer impact).

---

## 3. Collateral, decimals, side & signatureType encoding

### Collateral
- **V2 collateral token = pUSD** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`.
  - Primary proof: `getCollateral()` (selector `0x5c1548fb`) on the **deployed** standard
    V2 exchange `0xE111…996B` returns `0x…c011a7e12a19f7b1f670d46f03b03f3342e82dfb`. Same
    value from the neg-risk V2 exchange `0xe2222…0F59`.
  - On-chain `decimals()` (selector `0x313ce567`) of pUSD = `6`. `symbol()` = `"pUSD"`.
  - SDK `fees`/`config`: `COLLATERAL_TOKEN_DECIMALS = 6`,
    `collateral = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"`.
  - Docs/migration: pUSD = "Polymarket USD … standard ERC-20 on Polygon backed by USDC",
    wrapped via the Collateral Onramp `wrap()`.
- **USDC.e** `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` is **no longer** the order
  denomination. It persists only as the V2 exchange's `getCtfCollateral()`
  (returns `0x2791…84174` on standard; `0x3a3b…02e2` neg-risk-wrapped on neg-risk),
  used for CTF position-id derivation, not for order amounts. USDC.e decimals = 6.
- **Amounts are still 6 dp integers** (both `COLLATERAL_TOKEN_DECIMALS` and
  `CONDITIONAL_TOKEN_DECIMALS` = 6 in SDK `fees.py`). makerAmount/takerAmount are integer
  base units (×10^6), serialized as decimal strings on the wire / ints in the EIP-712
  message.

### Side encoding
`enum Side { BUY=0, SELL=1 }` (Structs.sol). In the signed message `side` is the int
(`0`/`1`); on the wire the `order.side` field is the **string** `"BUY"`/`"SELL"`
(SDK `order_to_json_v2` maps via `SideString`).

### signatureType encoding — UNCHANGED from V1
`enum SignatureType { EOA=0, POLY_PROXY=1, POLY_GNOSIS_SAFE=2, POLY_1271=3 }`
(Structs.sol lines 59–68; SDK `SignatureTypeV2`). **`2` still = POLY_GNOSIS_SAFE** — matches
our account setup. For sig_type 2 the SDK signs with the **standard EIP-712 ECDSA path**
(`Account.sign_message`), NOT the Solady `TypedDataSign` wrapper. The Solady
`TypedDataSign(Order contents,…)` nested-712 path is used **only** for `POLY_1271=3`
(DepositWallet smart-contract wallets). Our sig_type-2 orders use plain
`\x19\x01 || domainSeparator || structHash`.

---

## 4. Fee model & amount rounding

- **`feeRateBps` is no longer a field in the signed order.** It is removed entirely from the
  struct (see §2b). The signer does not commit to any fee rate.
- On-chain the exchange enforces an **admin-settable max fee rate in bps** via
  `getMaxFeeRate()` (selector `0x4a2a11f5`). On both deployed V2 exchanges this currently
  reads **`0`** (no protocol fee active at capture time). Fees are validated lazily on-chain
  only when non-zero (README: *"Admin-settable maximum fee rate in basis points (default
  500 = 5%), enforced per-order … lazy validation only when fees are non-zero"*).
- **`getFeeReceiver()`** on the standard V2 exchange = `0x115F48dc2a731Aa16251C6d6e1bEFc42f92AccC9`.
- **Fees are computed off-chain at order-build time** (SDK `fees.py`): a *platform fee*
  `fee_rate * (price*(1-price))**fee_exponent` plus a *builder taker fee*
  `builder_taker_fee_rate`. The CLOB exposes the live rates at `GET /fee-rate` and
  `GET /fees/builder-fees/{...}`. These adjust the spendable amount; they are NOT signed
  into the order.
- **makerAmount / takerAmount rounding for V2:** amounts are integer 6-dp base units.
  Price/size → maker/taker conversion uses `CalculatorHelper`-style math; the SDK keeps the
  same 6-decimal base-unit convention as V1. (No change to the 6dp scaling; the Port phase
  should reuse our existing integer base-unit rounding, just without a `feeRateBps` term.)

---

## 5. CLOB API submission shape (V2)

Source — SDK `endpoints.py` + `order_to_json_v2` (raw `order_data_v2.py`, lines 50–78) +
official migration doc `https://docs.polymarket.com/v2-migration`.

- **Single order:** `POST /order`  (`POST_ORDER = "/order"`)
- **Batch:** `POST /orders`        (`POST_ORDERS = "/orders"`)
- **Production host:** `https://clob.polymarket.com`

### Order envelope JSON (exact, from `order_to_json_v2`)

```jsonc
{
  "order": {
    "salt":          <int>,            // numeric
    "maker":         "0x…",            // funder Safe address
    "signer":        "0x…",            // EOA signer
    "tokenId":       "<decimal str>",
    "makerAmount":   "<decimal str>",  // 6dp base units
    "takerAmount":   "<decimal str>",  // 6dp base units
    "side":          "BUY" | "SELL",   // STRING on the wire
    "expiration":    "0",              // wire-only; NOT in signed struct; default "0"
    "signatureType": 2,                // int; 2 = POLY_GNOSIS_SAFE
    "timestamp":     "<ms str>",       // unix ms (also in signed struct)
    "metadata":      "0x…32bytes",     // bytes32 hex (default 0x00..00)
    "builder":       "0x…32bytes",     // bytes32 hex builder code (default 0x00..00)
    "signature":     "0x…"             // EIP-712 signature
  },
  "owner":     "<API key id>",
  "orderType": "GTC" | "GTD" | "FOK" | "FAK",
  "deferExec": false,                  // NEW top-level flag in V2 envelope
  "postOnly":  false
}
```

### New / changed fields vs V1 envelope
- **In `order`:** add `timestamp`, `metadata`, `builder`; remove `taker`, `nonce`,
  `feeRateBps`. `expiration` stays on the wire (default "0") but is no longer signed.
  `side` is the string `"BUY"/"SELL"` (unchanged convention).
- **Top-level:** `deferExec` is new alongside `owner`/`orderType`/`postOnly`.

### How the API knows V1 vs V2
There is **no version header and no explicit `version` field** in the envelope. The API
distinguishes by the **EIP-712 domain the order was signed against** — i.e. the
`signatureType`/struct shape plus the `verifyingContract` + domain `version "2"` implied by
the signature. Per the migration doc, V1-signed orders are simply rejected on production now
(see §6). Practically: sign against the V2 domain (version "2", V2 verifyingContract) and
post the V2 envelope; the server validates the signature against the V2 exchange.

### Auth / builder headers
- L1/L2 auth headers are **unchanged**: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`,
  `POLY_API_KEY`, `POLY_PASSPHRASE`.
- **Builder HMAC headers are removed** (`POLY_BUILDER_API_KEY/SECRET/PASSPHRASE/SIGNATURE`).
  Builder attribution now rides in the signed `builder` (bytes32) order field.

---

## 6. V1 deprecation & approval target

### V1 deprecation
Per the official migration page (`https://docs.polymarket.com/v2-migration`):
*"CLOB V2 is live as of April 28, 2026. Legacy V1 SDKs and V1-signed orders are no longer
supported on production."* / *"There is no backward compatibility."* V1-signed orders are
**rejected** for new orders. (A read-only `/data/pre-migration-orders` endpoint exists for
historical V1 orders.) → The Port phase must sign V2 only.

### USDC/collateral approval target for placing V2 orders
To trade on V2 you approve the **pUSD** collateral
`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, and the spender is the **V2 exchange contract
the order is signed against**:
- standard markets → spender `0xE111180000d2663C0091e4f400237545B87B996B`
- neg-risk markets → spender `0xe2222d279d744050d28e00520010520000310F59`

Plus the usual two companion approvals (same pattern as V1):
- pUSD `approve(conditional_tokens, MAX)` so the CTF can pull collateral for mint/merge
  (`conditional_tokens = 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`).
- CTF `setApprovalForAll(exchange, true)` so the exchange can move ERC-1155 outcome tokens.

(The SDK example `examples/account/approve_allowances.py` shows exactly this approve trio:
collateral→exchange, collateral→CTF, CTF.setApprovalForAll→exchange. NOTE: that example
script reads `contract_config.exchange` (the V1 field) and defaults `IS_MAINNET=False`
(Amoy); for V2 mainnet the spender must be `exchange_v2` / `neg_risk_exchange_v2`. Treat the
example's `exchange` reference as stale — see Caveats.)

---

## 7. Caveats / open items for the Port phase

1. **Live-money discipline:** this is research only. Do NOT place a live order. First V2
   action should be a dry-run: build a V2 order, recompute the struct hash, and confirm it
   matches `hashOrder()` read from the deployed exchange before ever broadcasting.

2. **Approval example is stale for V2.** `examples/account/approve_allowances.py` approves
   `contract_config.exchange` (V1 `0x4bFb41…982E`) and defaults to Amoy. The correct V2
   mainnet spender is `exchange_v2` (`0xE111…996B`) or `neg_risk_exchange_v2`
   (`0xe2222…0F59`). Verify the live approval is keyed to the V2 spender + pUSD before
   trading.

3. **Allowance/balance probe at capture disagreed with MEMORY.** Reading on-chain at
   2026-06-12, the funder Safe `0x40231C…E191` had pUSD allowance = 0 AND USDC.e allowance
   = 0 to the V2 standard exchange, and pUSD/USDC.e balances = 0. MEMORY says "USDC
   ALLOWANCE is keyed to V2 exchange." Reconcile before funding/trading: confirm which
   token (pUSD vs USDC.e), which owner (EOA vs funder Safe), and which spender actually
   carries a non-zero allowance. The Port phase should add `GET /balance-allowance` and an
   on-chain allowance check as a pre-trade gate. (The account likely needs pUSD funded via
   the Onramp `wrap()` and the V2 spender approved.)

4. **`metadata` semantics.** Struct comment says "metadata … hashed". For a plain order we
   send `bytes32(0)`. If we ever attach metadata, confirm the exact pre-image hashing the
   server expects before signing (it is committed in the signature).

5. **`builder` code.** Defaults to `bytes32(0)` (no builder attribution). If we register a
   Builder Profile we put its code here; it is signed, so it cannot be changed post-signing.

6. **makerAmount/takerAmount rounding detail** (`CalculatorHelper`) was not byte-verified
   here. Reuse our V1 6-dp base-unit rounding and add a parity test against the deployed
   `hashOrder()` / a known-good SDK order before live use.

7. **Fees are dynamic.** `getMaxFeeRate()` = 0 today but is admin-settable up to a 500-bps
   cap. Since fees aren't signed, pull `GET /fee-rate` at order time so spendable-amount
   math stays correct if Polymarket turns fees on.

---

## 8. Primary sources used

- Deployed standard V2 exchange (RPC reads: `eip712Domain`, `getCollateral`, `getCtf`,
  `getCtfCollateral`, `getMaxFeeRate`, `getFeeReceiver`):
  `https://polygonscan.com/address/0xE111180000d2663C0091e4f400237545B87B996B`
- Deployed neg-risk V2 exchange (RPC reads): `0xe2222d279d744050d28e00520010520000310F59`
- pUSD token (RPC `decimals`/`symbol`): `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
- `Structs.sol` (Order struct, ORDER_TYPEHASH, enums):
  `https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/libraries/Structs.sol`
- `Hashing.sol` (domain name/version, `_createStructHash`, signature exclusion):
  `https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/mixins/Hashing.sol`
- SDK `py-clob-client-v2`: `ctf_exchange_v2_typed_data.py`, `order_data_v2.py`
  (`order_to_json_v2`), `exchange_order_builder_v2.py`, `signature_type_v2.py`,
  `config.py`, `fees.py`, `endpoints.py`, `examples/account/approve_allowances.py`:
  `https://github.com/Polymarket/py-clob-client-v2`
- Contracts page: `https://docs.polymarket.com/resources/contracts`
- Migration page: `https://docs.polymarket.com/v2-migration`
- ORDER_TYPEHASH cross-check computed locally: `keccak256(ORDER_TYPE_STRING)` ==
  `0xbb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589`.
