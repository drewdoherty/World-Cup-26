# Polymarket Relayer Wallet-Action Spec — Safe `execTransaction` via gasless relayer

Research phase only. NO code written. Purpose: enable the trading proxy (Polymarket
Gnosis Safe `0x86b4c55a4df1fbea0f325e842434e0a537caa549`, owner/signer EOA
`0x721A9E426267502d20bcB8afBe9db25a86dCEB76`) to set ERC-20 (pUSD) approvals to the V2
exchanges **without holding gas**, by submitting an EOA-signed Safe transaction to the
Polymarket relayer (which broadcasts and pays gas).

Date of capture: 2026-06-12. Chain: Polygon mainnet (chainId 137). Relayer: v2.

Every load-bearing fact is cited. Where a secondary summary (SDK docs.rs text) could not be
confirmed against the deployed Safe contract / EIP-712 standard, it is flagged in §7
Caveats and must be verified by reading the deployed Safe bytecode before going live.

---

## 0. TL;DR for the Port phase

To approve pUSD from the proxy to a V2 exchange with no gas:

1. `GET https://relayer-v2.polymarket.com/nonce?address=<EOA owner>&type=SAFE` → Safe nonce.
2. Build calldata = `approve(spender, MAX)` = `0x095ea7b3` + 32-byte left-padded spender +
   32 bytes of `0xff…ff` (MAX uint256). `to` = pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`.
3. Build the Gnosis-Safe `SafeTx` struct (defaults: value 0, operation 0/CALL, safeTxGas 0,
   baseGas 0, gasPrice 0, gasToken 0x0, refundReceiver 0x0, nonce from step 1).
4. Compute EIP-712 digest over the Safe domain (verifyingContract = the **proxy Safe
   address**, chainId 137) and SafeTx type, sign with the EOA private key, pack as
   `r‖s‖v` (65 bytes).
5. `POST https://relayer-v2.polymarket.com/submit` with `type:"SAFE"`, `from`=EOA owner,
   `to`=pUSD token, `proxyWallet`=proxy Safe, `data`=approve calldata, `nonce`,
   `signature`, and `signatureParams` (the gas/operation fields). Builder or Relayer API
   auth headers required.
6. Poll `GET /transaction?id=<transactionID>` until `STATE_CONFIRMED`.
7. Repeat for the second spender (std + neg-risk exchange).

Inert by default: nothing above should run without an explicit live-mode env flag.

---

## 1. Relayer endpoints

Base URL: **`https://relayer-v2.polymarket.com`**
Source: [Submit a transaction — Polymarket docs](https://docs.polymarket.com/api-reference/relayer/submit-a-transaction)
("URL: `https://relayer-v2.polymarket.com/submit`, Method: POST"); confirmed by the Rust SDK
[`RelayerClient`](https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/struct.RelayerClient.html)
("Deploys Safe via Relayer v2 API").

| Action | Method + path | Notes |
|---|---|---|
| Get Safe nonce | `GET /nonce?address={ownerEOA}&type={SAFE\|SAFECREATE}` | `SAFE` = exec; `SAFECREATE` = deploy. From `RelayerClient::get_next_nonce` ("GET /nonce?address={address}&type={SAFE\|SAFECREATE}"). |
| Submit tx | `POST /submit` | Returns `{transactionID, state: STATE_NEW}` immediately. |
| Transaction status | `GET /transaction?id={transactionID}` | From `RelayerClient::get_transaction_status` ("/transaction?id=xxx"). Poll until terminal state (`STATE_CONFIRMED` / failure). |

Source for nonce + transaction endpoints: [`RelayerClient` docs.rs](https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/struct.RelayerClient.html)
(`get_next_nonce`, `get_transaction_status`, `poll_until_confirmed`, `submit_transaction`
"Posts to /submit endpoint for SafeCreate and Safe execution transactions").

Note on the *new* deposit-wallet relayer (different system): it uses `POST /submit` with
`type:"WALLET"`/`"WALLET-CREATE"` and `GET /nonce?...&type=WALLET`. That path is for
ERC-1967 deposit wallets + POLY_1271, NOT our existing Safe (sig type 2). We use the
**SAFE** path. Source: [deposit-wallets.md](https://docs.polymarket.com/trading/deposit-wallets.md).

---

## 2. `/submit` payload (type = SAFE)

Source: [Submit a transaction](https://docs.polymarket.com/api-reference/relayer/submit-a-transaction).

```
POST https://relayer-v2.polymarket.com/submit
Content-Type: application/json
```

| Field | Type | Meaning |
|---|---|---|
| `from` | address | Signer = the Safe owner EOA (`0x721A9E…EB76`). |
| `to` | address | Target contract of the inner call = pUSD token for approve. |
| `proxyWallet` | address | The user's Polymarket proxy/Safe (`0x86b4c5…a549`). |
| `data` | hex string | Encoded inner call data (the `approve(...)` calldata). |
| `nonce` | string | Safe nonce from `GET /nonce?...type=SAFE`. |
| `signature` | hex string | EIP-712 SafeTx signature, packed `r‖s‖v` (65 bytes). |
| `signatureParams` | object | Gas + Safe exec params (see below). |
| `type` | enum string | `"SAFE"` (Gnosis Safe exec) or `"PROXY"`. Use `"SAFE"`. |

`signatureParams` subfields (source: same page):

| Field | Type | Default for an approve |
|---|---|---|
| `gasPrice` | string | `"0"` |
| `operation` | string | `"0"` (CALL) |
| `safeTxnGas` | string | `"0"` |
| `baseGas` | string | `"0"` |
| `gasToken` | address | `0x0000000000000000000000000000000000000000` |
| `refundReceiver` | address | `0x0000000000000000000000000000000000000000` |

These mirror the SafeTx struct gas fields; relayer sponsors gas, so all gas/refund fields
are zero. Defaults confirmed by SDK: [`safe` module](https://docs.rs/polymarket-sdk/latest/polymarket_sdk/safe/index.html)
("safeTxGas: 0, baseGas: 0, gasPrice: 0, gasToken: 0x0…0, refundReceiver: 0x0…0,
operation: CALL (0)").

> ⚠️ Field name is `safeTxnGas` in the docs payload table but `safeTxGas` in the EIP-712
> struct and SDK. Treat as the same value; verify exact JSON key against a live relayer
> client before submit (§7).

Response: `{ "transactionID": "...", "state": "STATE_NEW" }`, then poll
`GET /transaction?id=...` for `transactionHash` / `STATE_CONFIRMED`.

---

## 3. Signing scheme — what the EOA signs

The EOA owner signs a **Gnosis Safe `SafeTx`** EIP-712 typed message. The relayer then
calls `execTransaction(...)` on the Safe, passing this signature; the Safe (1-of-1 multisig
owned by the EOA) validates it and executes the inner call.

Source: [Polymarket Proxy wallet docs](https://docs.polymarket.com/developers/proxy-wallet)
("multi-step transactions can be executed atomically and transactions can be relayed by
relayers"; "1-of-1 multisig wallets granting the user sole rights"; "calls to the wallet
can be performed by Polymarket on behalf of the user based on valid signatures") and the
Gnosis Safe contract internals.

### 3.1 EIP-712 types

`SAFE_TX_TYPEHASH` = `0xbb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8`
= `keccak256("SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")`.
Source: Gnosis Safe 1.3.0 contract; [Gnosis Safe Internals Pt.3](https://medium.com/@cizeon/gnosis-safe-internals-part-3-signing-transactions-93fcced50a29)
and deployed singleton [Safe 1.3.0 (Polygon)](https://polygonscan.com/address/0x3e5c63644e683549055b9be8653de26e0b4cd36e).

SafeTx struct (order matters for encoding):
```
to            address   = inner-call target (pUSD token for approve)
value         uint256   = 0
data          bytes     = inner calldata (approve(spender, MAX))
operation     uint8     = 0 (CALL)
safeTxGas     uint256   = 0
baseGas       uint256   = 0
gasPrice      uint256   = 0
gasToken      address   = 0x00…00
refundReceiver address  = 0x00…00
nonce         uint256   = Safe nonce from GET /nonce
```

### 3.2 EIP-712 domain

Safe **v1.3.0** domain separator type is:
`DOMAIN_SEPARATOR_TYPEHASH` =
`0x47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218`
= `keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")`.
Source: Safe 1.3.0 contract constant, per [search result on Safe 1.3.0 typehashes](https://medium.com/@cizeon/gnosis-safe-internals-part-3-signing-transactions-93fcced50a29).

So the domain has **only** two fields:
```
chainId            = 137
verifyingContract  = the proxy Safe address (0x86b4c5…a549)   <-- NOT a factory, NOT an exchange
```
There is **no `name` and no `version`** in the Safe 1.3.0 domain separator. (The Rust SDK
docs.rs text says domain name "Safe" / version "1.3.0" — that contradicts the deployed
contract's `EIP712Domain(uint256 chainId,address verifyingContract)`. The contract wins;
see §7.)

### 3.3 Digest + packing

`digest = keccak256(0x19 ‖ 0x01 ‖ domainSeparator ‖ keccak256(SAFE_TX_TYPEHASH ‖ enc(SafeTx)))`
where `enc(data)` uses `keccak256(data)` for the dynamic `bytes data` field.
Sign `digest` with the EOA key (ECDSA secp256k1). Pack as **`r (32) ‖ s (32) ‖ v (1)`** =
65 bytes; `v` in {27,28}. This is the format `execTransaction` expects.
Source: SDK [`compute_safe_tx_digest` / `pack_signature_for_safe_tx`](https://docs.rs/polymarket-sdk/latest/polymarket_sdk/safe/index.html)
("formats the ECDSA signature (r, s, v) into the format required for Safe's execTransaction");
Gnosis Safe expects packed `{bytes32 r}{bytes32 s}{uint8 v}` (search result, §1 sources).

eth-account note: `eth_account.Account._sign_hash` / `signHash` (or
`encode_typed_data` + `sign_message`) yields `(r, s, v)`; concatenate big-endian as above.
Stdlib + eth-account only; no web3 needed (we build calldata + digest by hand).

---

## 4. Nonce management

- Fetch immediately before signing: `GET /nonce?address={ownerEOA}&type=SAFE`.
  Source: `RelayerClient::get_next_nonce` ("GET /nonce?address={address}&type={SAFE|SAFECREATE}"),
  [docs.rs](https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/struct.RelayerClient.html).
- The returned value is the next Safe `nonce` and goes into BOTH the SafeTx struct (signed)
  and the `/submit` body `nonce` field — they must match.
- The Safe nonce increments per executed tx; for two approvals (std + neg-risk exchange),
  submit the first, wait for `STATE_CONFIRMED`, then re-fetch the nonce for the second
  (or fetch once and use n, n+1 if the relayer accepts queued nonces — verify, §7).
- `SAFECREATE` is a separate nonce namespace for deploying the Safe (not needed; our Safe
  is already deployed and holds positions).
- This relayer nonce is distinct from the on-chain account nonce and from CLOB order nonces.

---

## 5. Encoding `approve(spender, MAX)` without web3

ERC-20 `approve(address,uint256)` selector = first 4 bytes of
`keccak256("approve(address,uint256)")` = **`0x095ea7b3`**. (Standard ERC-20; confirmable by
hashing the signature.)

Calldata layout (4 + 32 + 32 = 68 bytes):
```
0x095ea7b3
  + 000000000000000000000000<spender 20 bytes>          (left-padded to 32)
  + ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff   (MAX uint256)
```
- `spender` = V2 exchange:
  - std exchange `0xE111180000d2663C0091e4f400237545B87B996B`
  - neg-risk exchange `0xe2222d279d744050d28e00520010520000310F59`
- `to` (SafeTx.to and /submit `to`) = pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`.
- MAX = `2**256 - 1` (unlimited allowance, matches UI behavior).

SDK confirms approve is encoded as a generic call and submitted as a Safe/Proxy
transaction with `{to: token, data: calldata, value: "0"}`. Sources:
[builder-relayer-client](https://github.com/Polymarket/builder-relayer-client)
("`encodeFunctionData({abi: erc20Abi, functionName: 'approve', args: [spenderAddress, maxUint256]})`";
"Transaction objects: `{to: tokenAddress, data: calldata, value: '0'}`") and SDK helper
`encode_erc20_approve` ([docs.rs safe module](https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/index.html)).
We build the 68-byte calldata by string/bytes concatenation (no ABI lib required).

---

## 6. Relayer restrictions, rate limits, fees

- **Target allowlist:** NOT disclosed in public docs. [Submit a transaction](https://docs.polymarket.com/api-reference/relayer/submit-a-transaction)
  explicitly lists "Contract allowlist — Not Disclosed in Documentation." The relayer is
  known to execute ERC-20 approvals + ERC-1155 setApprovalForAll + trade/transfer calls
  (the SDK exposes `encode_erc20_approve`, `encode_erc20_transfer`,
  `encode_erc1155_set_approval_for_all`), so approve-to-exchange is within its normal
  function. Whether it rejects arbitrary `to` is unverified (§7).
- **Rate limits:** NOT disclosed publicly (same page: "Rate limits — Not Disclosed").
- **Fees:** For Safe exec the gas/refund fields are zero (gasless, relayer-sponsored). The
  separate *deposit* relayer (`relayer-deposits`) charges a bps + min-floor fee on deposits,
  but that is a different flow and not relevant to approvals. Source:
  [relayer-deposits README](https://github.com/Polymarket/relayer-deposits) ("relayer picks
  the lesser of the fee from the request and the fee it calculates itself").

---

## 7. Caveats / must-verify before live submit

1. **Domain name/version contradiction.** Rust SDK docs.rs text claims the Safe domain has
   `name:"Safe", version:"1.3.0"`. The deployed Safe 1.3.0 `DOMAIN_SEPARATOR_TYPEHASH` =
   `keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")` has NO name/version.
   **Read the proxy Safe's `domainSeparator()` / `VERSION()` over RPC** before signing; if
   the proxy is a Polymarket *custom* 1-of-1 proxy (factory `0xaacfeea03eb1561c4e67d661e40682bd20e3541b`)
   rather than a stock Gnosis Safe, its domain/typehash may differ. Compute the expected
   domain separator both ways and compare to the on-chain value.
2. **`safeTxnGas` vs `safeTxGas` JSON key.** Docs payload table uses `safeTxnGas`; struct
   uses `safeTxGas`. Confirm the exact key the live relayer expects (capture a real request
   from an official client, or test on a zero-value no-op).
3. **Auth headers.** `/submit` needs either Builder API keys (`POLY_BUILDER_API_KEY`,
   `POLY_BUILDER_TIMESTAMP`, `POLY_BUILDER_PASSPHRASE`, `POLY_BUILDER_SIGNATURE` — HMAC) or
   Relayer API keys (`RELAYER_API_KEY`, `RELAYER_API_KEY_ADDRESS`). Determine which set our
   account already has; CLOB L1/L2 creds are SEPARATE and not reused here
   ([deposit-wallets.md](https://docs.polymarket.com/trading/deposit-wallets.md): "Relayer
   auth and CLOB auth are independent").
4. **Allowlist.** Confirm the relayer will execute an `approve` whose `to` is the pUSD token
   (it should — that's exactly what the UI's "Enable trading" does — but it's undocumented).
5. **Nonce queuing.** Verify whether two approvals can be signed at nonce n and n+1 and
   submitted back-to-back, or must be strictly serialized through `STATE_CONFIRMED`.
6. **pUSD vs USDC.e.** Per the V2 spec §349, both pUSD and USDC.e allowances from the funder
   were zero. Confirm which token the V2 exchange actually pulls (the V2 spec flags pUSD
   `0xC011a7E…2DFB`); approve the correct collateral. The proxy here is `0x86b4c5…a549`
   (positions), distinct from funder Safe `0x40231C…E191` in the V2 spec — confirm WHICH
   address must hold the allowance (the `maker`/source-of-funds in the order).

---

## 8. Primary sources

- Submit a transaction (endpoint, payload, signatureParams, undisclosed allowlist/limits/fees):
  https://docs.polymarket.com/api-reference/relayer/submit-a-transaction
- Deposit wallets / relayer + auth separation (context, WALLET vs SAFE path):
  https://docs.polymarket.com/trading/deposit-wallets.md
- Proxy wallet (Safe 1-of-1, relayed exec model, factory):
  https://docs.polymarket.com/developers/proxy-wallet
- RelayerClient (nonce/submit/transaction endpoints, approval-check helpers, EIP-712 build):
  https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/struct.RelayerClient.html
- safe module (SafeTx fields, defaults, digest+packing helpers, encode_erc20_approve, addrs):
  https://docs.rs/polymarket-rs-sdk/latest/polymarket_rs_sdk/safe/index.html
  https://docs.rs/polymarket-sdk/latest/polymarket_sdk/safe/index.html
- builder-relayer-client (RelayerTxType SAFE/PROXY, approve calldata via encodeFunctionData,
  {to,data,value} tx objects): https://github.com/Polymarket/builder-relayer-client
- relayer-deposits (deposit fee model — different flow): https://github.com/Polymarket/relayer-deposits
- Gnosis Safe 1.3.0 SAFE_TX_TYPEHASH + domain typehash + r‖s‖v packing:
  https://medium.com/@cizeon/gnosis-safe-internals-part-3-signing-transactions-93fcced50a29
  https://polygonscan.com/address/0x3e5c63644e683549055b9be8653de26e0b4cd36e
