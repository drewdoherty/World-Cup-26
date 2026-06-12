# Polymarket DepositWallet — EIP-712 + Relayer Spec

Research phase. Every load-bearing fact is backed by a **primary source**: deployed
bytecode/state read over RPC, the official Polymarket TypeScript client source on GitHub
(`Polymarket/builder-relayer-client`, branch `main`), or docs.polymarket.com. Typehashes
are derived deterministically from the EIP-712 type definitions in that client (canonical
EIP-712 `encodeType` rules) and are reproducible.

Date of capture: 2026-06-12. Chain: Polygon mainnet (chainId 137).

This wallet (`0x86b4c55a4df1fbea0f325e842434e0a537caa549`) is a **DepositWallet**, NOT a
Gnosis Safe and NOT a GSN proxy. The existing `src/wca/pm/relayer.py` (SAFE branch) and
`src/wca/pm/signing.py` / `trader.py` (sig_type 2 POLY_GNOSIS_SAFE) are the WRONG path for
it. Add a WALLET branch as described here.

---

## 0. TL;DR for the Port phase

To authorize an on-chain action from the deposit wallet (e.g. approve pUSD to the V2
exchanges), gaslessly via the relayer:

1. `GET https://relayer-v2.polymarket.com/nonce?address=<OWNER_EOA>&type=WALLET` → `{"nonce":"4"}`.
   - `address` is the **owner EOA** (`0x721A9E426267502d20bcB8afBe9db25a86dCEB76`), the
     L1 signer — NOT the deposit wallet. `type=WALLET`.
2. Sign an EIP-712 `Batch` struct with the owner EOA private key under domain
   `{name:"DepositWallet", version:"1", chainId:137, verifyingContract:<DEPOSIT_WALLET>}`.
   Normal 65-byte signature (r,s,v). `verifyingContract` is the deposit wallet address.
3. `POST https://relayer-v2.polymarket.com/submit` with `type:"WALLET"`, the signature, and
   `depositWalletParams`. Auth via builder API key headers (or relayer API key).
4. Poll `GET /transaction?id=<transactionID>` until `STATE_MINED`/`STATE_CONFIRMED`.

For **CLOB orders** from a deposit wallet: sig_type = **3 (POLY_1271)**; `maker` and
`signer` both = the deposit wallet address; the order signature is an ERC-7739-wrapped
ERC-1271 signature (317 bytes), NOT a raw EIP-712 order sig.

Our wallet is already deployed (impl `0x58ca52eb...`, nonce()=4), so WALLET-CREATE is not
needed — but it is documented in §3 for completeness.

---

## 1. EIP-712 — what the owner signs (ground truth)

Source: `src/builder/deposit-wallet.ts` (`DEPOSIT_WALLET_TYPES` + `signBatch`),
`src/constants/index.ts` (domain name/version), branch `main`.

### Domain

```
EIP712Domain {
  name:              "DepositWallet"   // DEPOSIT_WALLET_DOMAIN_NAME
  version:           "1"               // DEPOSIT_WALLET_DOMAIN_VERSION
  chainId:           137
  verifyingContract: <deposit wallet address>   // the wallet itself, NOT the factory
}
```
Matches the on-chain `eip712Domain()` read earlier:
`{name:"DepositWallet", version:"1", chainId:137, verifyingContract:proxy}`.

### Types (verbatim from `DEPOSIT_WALLET_TYPES`)

```
Call {
  address target
  uint256 value
  bytes   data
}

Batch {
  address  wallet     // = the deposit wallet address (also verifyingContract)
  uint256  nonce      // current WALLET nonce from the relayer
  uint256  deadline   // unix seconds; signature expiry
  Call[]   calls
}
```

Primary type signed is **`Batch`** (`signer.signTypedData(domain, TYPES, message, "Batch")`).

### Derived typehashes (canonical EIP-712 encodeType, reproducible)

`encodeType(Batch)` = `Batch(...)` followed by the one referenced type `Call(...)`
(referenced types sorted alphabetically):

```
BATCH_TYPEHASH = keccak256("Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)Call(address target,uint256 value,bytes data)")
               = 0x712ef66e8362c387e862cabf0923c209db0fa24cfc97d25eccba7c86f3ee1dd3

CALL_TYPEHASH  = keccak256("Call(address target,uint256 value,bytes data)")
               = 0x84fa2cf05cd88e992eae77e851af68a4ee278dcff6ef504e487a55b3baadfbe5
```

`hashStruct(Call)` = keccak256(CALL_TYPEHASH ‖ target ‖ value ‖ keccak256(data)).
`hashStruct(Batch)` = keccak256(BATCH_TYPEHASH ‖ wallet ‖ nonce ‖ deadline ‖
keccak256(concat of each Call's hashStruct)). Standard EIP-712: dynamic `bytes data` is
hashed; the `Call[]` array element is the keccak of the concatenated per-element struct
hashes.

CAVEAT: these typehash hex values are computed locally from the client's type definitions,
not copy-pasted from verified Solidity source (polygonscan source fetch needs an API key —
see §6). They are correct iff the deployed contract uses the standard EIP-712 encoding of
exactly these structs, which the client's `signTypedData` call implies and the on-chain
`eip712Domain()` corroborates. Validate against an on-chain `eth_call` before live use if
the contract exposes a typehash getter, or by submitting a single low-value batch.

### Message construction (from `signBatch`)

```
message = {
  wallet:   <deposit wallet>,
  nonce:    BigInt(nonce),       // from GET /nonce type=WALLET
  deadline: BigInt(deadline),    // e.g. now + 240s (see executeDepositWallet.ts)
  calls:    [{ target, value: BigInt(value), data }, ...]
}
```
Note `value` is per-call uint256 (use "0" for ERC-20 approvals); there is **no
`operation`/`callType` field** — unlike Safe/Proxy, DepositWallet `Call` is plain CALL only.

---

## 2. On-chain wallet facts (RPC, branch-confirmed)

- Deposit wallet: `0x86b4c55a4df1fbea0f325e842434e0a537caa549`, ERC-1967 proxy.
- Implementation (chainId 137): `0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB`
  — exactly the `POL.DepositWalletContracts.DepositWalletImplementation` in
  `src/config/index.ts`. Confirms wallet type and version.
- Owner EOA: `0x721A9E426267502d20bcB8afBe9db25a86dCEB76` (L1 signer / `from`).
- `nonce()` = 4 (on-chain view in `src/abis/depositWallet.ts` `depositWalletV1Abi`).
  Always re-fetch the WALLET nonce from the relayer before each batch (replay protection).
- Factory (chainId 137): `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07`
  (`DepositWalletFactory`) — this is the `to` in relayer payloads.

Address derivation (if ever needed): CREATE2 from factory; salt = keccak256(abi.encode(
factory, bytes32(owner))); UUPS bytecodeHash via Solady `initCodeHashERC1967(impl, args)`,
args = abi.encode(factory, bytes32(owner)). See `src/builder/derive.ts`
(`deriveUupsDepositWallet`). A beacon variant exists (`deriveBeaconDepositWallet`); the
client prefers UUPS unless the factory's beacon selector `0x49493a4d` returns non-zero and
the UUPS address isn't deployed.

---

## 3. Relayer payloads (ground truth)

Base URL: `https://relayer-v2.polymarket.com` (relayer-v2). Endpoints from
`src/endpoints.ts`. The published docs API-reference page only lists SAFE/PROXY, but the
official client uses these WALLET variants — client source is authoritative here.

### 3.1 Nonce — `GET /nonce`
Query params (from `RelayClient.getNonce`): `address=<OWNER_EOA>`, `type=WALLET`.
Response: `{"nonce":"<string>"}`.

### 3.2 Deployed check (optional) — `GET /deployed`
Params: `address`, optional `type`. Response `{"deployed":bool}`. Ours is deployed.

### 3.3 Submit — `POST /submit`

WALLET batch body (from `buildDepositWalletBatchRequest`):
```json
{
  "type": "WALLET",
  "from": "<OWNER_EOA>",
  "to":   "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",   // DepositWalletFactory
  "nonce": "<wallet nonce string>",
  "signature": "0x<65-byte EIP-712 Batch sig>",
  "depositWalletParams": {
    "depositWallet": "<deposit wallet address>",
    "deadline": "<unix seconds string>",
    "calls": [ { "target": "0x...", "value": "0", "data": "0x..." } ]
  }
}
```
Note: the WALLET batch body does NOT use the SAFE/PROXY `data`/`signatureParams`/
`proxyWallet` fields (those belong to `TransactionRequest` for SAFE/PROXY). WALLET uses the
dedicated `DepositWalletBatchRequest` shape above.

WALLET-CREATE body (from `buildDepositWalletCreateRequest`) — deployment only, NOT needed
for our already-deployed wallet:
```json
{ "type": "WALLET-CREATE", "from": "<OWNER_EOA>", "to": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07" }
```

### 3.4 Auth (from `RelayClient.sendAuthedRequest` + docs)
Builder API key headers (preferred): `POLY_BUILDER_API_KEY`, `POLY_BUILDER_TIMESTAMP`,
`POLY_BUILDER_PASSPHRASE`, `POLY_BUILDER_SIGNATURE`. Alternative: relayer API keys
(`RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS`). `/nonce`, `/deployed`, `/transaction` are
unauthed GETs; `/submit` and `/transactions` are authed.

### 3.5 Result
`/submit` returns `{transactionID, state:"STATE_NEW", ...}`. Poll
`GET /transaction?id=<transactionID>`; terminal states: STATE_MINED, STATE_CONFIRMED
(success), STATE_FAILED, STATE_INVALID (failure).

---

## 4. CLOB order signature for a deposit wallet

Sources: docs.polymarket.com authentication + deposit-wallets pages; py-clob-client-v2
issues #53/#70.

- sig_type = **3 = POLY_1271** ("Deposit wallet flow for new API users").
- `maker` and `signer` order fields = the **deposit wallet address** (not the EOA). Using
  the EOA maker errors "maker address not allowed, please use the deposit wallet flow".
- The order signature is NOT the raw order EIP-712 sig. It is an **ERC-7739-wrapped**
  signature the deposit wallet validates via **ERC-1271**. Byte layout (docs):
  `innerSig(65) + appDomainSeparator(32) + contentsHash(32) + orderTypeString(186) +
  typeStringLength(2)` = 317 bytes (0x + 634 hex).
- L1/L2 auth: standard two-tier. KNOWN ISSUE (py-clob-client-v2 #70): some clients' L1
  header signature bound the API key to the EOA, not the deposit wallet — docs say
  `POLY_ADDRESS` / L1 must bind to the deposit wallet address. Verify the L1 header binds
  to the deposit wallet before relying on POLY_1271 order placement.

This differs from our current `signing.py`/`trader.py` sig_type 2 (POLY_GNOSIS_SAFE) — a
separate change from the wallet-action path in §1–3.

---

## 5. public_api (machine-usable)

```
DOMAIN          = {name:"DepositWallet", version:"1", chainId:137, verifyingContract:<DEPOSIT_WALLET>}
PRIMARY_TYPE    = "Batch"
TYPES.Call      = [("target","address"),("value","uint256"),("data","bytes")]
TYPES.Batch     = [("wallet","address"),("nonce","uint256"),("deadline","uint256"),("calls","Call[]")]
BATCH_TYPEHASH  = 0x712ef66e8362c387e862cabf0923c209db0fa24cfc97d25eccba7c86f3ee1dd3
CALL_TYPEHASH   = 0x84fa2cf05cd88e992eae77e851af68a4ee278dcff6ef504e487a55b3baadfbe5

RELAYER_BASE    = "https://relayer-v2.polymarket.com"
NONCE           = GET /nonce?address=<OWNER_EOA>&type=WALLET            -> {"nonce":str}
DEPLOYED        = GET /deployed?address=<addr>[&type=]                  -> {"deployed":bool}
SUBMIT          = POST /submit (authed: POLY_BUILDER_* headers)         -> {transactionID,state,...}
TRANSACTION     = GET /transaction?id=<transactionID>                   -> [RelayerTransaction]

SUBMIT_WALLET   = {type:"WALLET", from:<OWNER_EOA>, to:<FACTORY>, nonce:str,
                   signature:"0x"+65B, depositWalletParams:{depositWallet:<DW>, deadline:str,
                   calls:[{target,value,data}]}}
SUBMIT_CREATE   = {type:"WALLET-CREATE", from:<OWNER_EOA>, to:<FACTORY>}

FACTORY         = 0x00000000000Fb5C9ADea0298D729A0CB3823Cc07
IMPLEMENTATION  = 0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB
OWNER_EOA       = 0x721A9E426267502d20bcB8afBe9db25a86dCEB76
DEPOSIT_WALLET  = 0x86b4c55a4df1fbea0f325e842434e0a537caa549

CLOB_SIG_TYPE   = 3   # POLY_1271; maker==signer==DEPOSIT_WALLET; ERC-7739-wrapped 1271 sig (317B)
```

---

## 6. Caveats

- Typehash hex values are computed locally from the official client's EIP-712 type
  definitions (canonical encodeType), not extracted from verified Solidity. The polygonscan
  / etherscan-v2 `getsourcecode` call requires an API key (returned "Missing/Invalid API
  Key"); re-run with a key to copy the literal `bytes32 ... TYPEHASH = keccak256(...)`
  constant from `0x58ca52eb...` source to triple-confirm. The on-chain `eip712Domain()`
  read already confirms the domain.
- The public docs relayer API-reference currently documents only `type` ∈ {SAFE, PROXY}.
  The WALLET / WALLET-CREATE types and `depositWalletParams` shape come from the official
  `builder-relayer-client` source (branch `main`), which is authoritative and more current
  than that page.
- `value` per-call is supported by the struct; for approvals it's "0". No
  operation/delegatecall field exists for DepositWallet calls (plain CALL only).
- The CLOB POLY_1271 L1-binding bug (py-clob-client-v2 #70) is open — verify L1 auth binds
  to the deposit wallet, not the owner EOA, before live order placement.
- LIVE MONEY: nothing here should submit without `PM_APPROVE_LIVE=1` and `--yes`. Never log
  the owner private key, signatures, or builder secret.

---

## 7. Sources

- `Polymarket/builder-relayer-client` (branch `main`):
  `src/builder/deposit-wallet.ts`, `src/builder/derive.ts`, `src/types.ts`,
  `src/constants/index.ts`, `src/endpoints.ts`, `src/config/index.ts`, `src/client.ts`,
  `src/abis/depositWallet.ts`, `examples/executeDepositWallet.ts`,
  `examples/deployDepositWallet.ts`.
  https://github.com/Polymarket/builder-relayer-client
- Polymarket docs: https://docs.polymarket.com/trading/deposit-wallets ,
  https://docs.polymarket.com/api-reference/authentication ,
  https://docs.polymarket.com/api-reference/relayer (submit / get-nonce).
- PolyNode mirror (POLY_1271 wrapped-sig byte layout):
  https://docs.polynode.dev/guides/deposit-wallets
- py-clob-client-v2 issues #53, #70 (maker/signer + L1 binding):
  https://github.com/Polymarket/py-clob-client-v2/issues/70
- On-chain reads (Polygon RPC): eip712Domain(), nonce()=4, owner(), impl 0x58ca52eb...
