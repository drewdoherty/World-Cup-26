# Polymarket V2 — funding & approvals to enable live automated orders

Status (2026-06-12): the V2 signer (`src/wca/pm/signing.py`) is verified correct against the
deployed contracts. The **blocker is on-chain state, not code.** A live probe + direct RPC
balance reads show the trading account is **not funded or approved** for V2.

## The unresolved problem: funds not located on-chain

`scripts/wca_pm_probe.py` and a direct RPC sweep both show **ZERO** of every token at **both**
of our known addresses:

| token | EOA `0x721A…EB76` | Safe `0x4023…E191` |
|---|---|---|
| pUSD `0xC011a7…82DFB` (V2 collateral, 6dp) | 0 | 0 |
| USDC.e `0x2791…84174` | 0 | 0 |
| USDC native `0x3c49…3359` | 0 | 0 |

But the Polymarket UI shows ~$2,500 "available to trade". **Reconcile this FIRST** — there is no
point approving a wallet that holds nothing. Likely explanations to check in the Polymarket UI:
1. The actual deposit/custody address differs from the Safe `0x4023…E191` we recorded. In the
   UI: Deposit / wallet settings → copy the **real** on-chain address it shows, and re-probe that.
2. Funds are still in **USDC.e** somewhere and need converting/wrapping to **pUSD** (V2 changed
   the collateral token) via Polymarket's on-ramp.
3. Polymarket custodies the balance via a relayer until first V2 trade.

## Known V2 facts (from the verified spec, `docs/research/polymarket_v2_spec.md`)

- **Collateral = pUSD** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (6 dp) — *not* USDC.e anymore.
- **Order signs against the exchange chosen by the market's neg-risk flag:**
  - standard V2 exchange `0xE111180000d2663C0091e4f400237545B87B996B`
  - **neg-risk V2 exchange `0xe2222d279d744050d28e00520010520000310F59`** ← both current candidate
    markets are neg_risk, so this is the one that matters now.
- Account class: signer = EOA `0x721A…EB76`, maker = funder Safe `0x4023…E191`, signature_type **2**.

## The 4 approvals the funder Safe must have (once funded with pUSD)

All sent **from the Safe** `0x4023…E191`:
1. **Hold pUSD** — convert/deposit USDC → pUSD so balance > 0.
2. `pUSD.approve(spender = 0xe2222…0F59, MAX)`  (neg-risk exchange; also `0xE111…996B` for standard markets)
3. `pUSD.approve(spender = conditional_tokens 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045, MAX)`
4. `CTF.setApprovalForAll(operator = the V2 exchange for the market type, true)`

## How to actually do the approvals (this repo cannot — eth-account only, no web3 writes)

These are **on-chain writes the user performs**, not the bot. The Safe is a Polymarket-managed
proxy, so the simplest path is usually Polymarket's own UI:
- Placing/“enabling” trading through the Polymarket UI normally sets these approvals automatically
  the first time post-V2. If the user already trades manually post-migration and the Safe **still**
  reads zero allowance, the UI is using a gasless relayer that bypasses the Safe's own ERC20
  approvals — in which case self-signed CLOB automation from the proxy-Safe may not be possible
  without Polymarket exposing the approval, and an **EOA-funded account (sig_type 0)** would be the
  cleaner automation path (user controls approvals directly).

## Gate (do NOT bypass)

`PM_DRY_RUN` stays **1 (ON)**. Before flipping to `0`, re-run `scripts/wca_pm_probe.py` and confirm:
- pUSD balance at the funder Safe **> 0**, AND
- pUSD allowance to `0xe2222…0F59` (neg-risk) **> 0**.
Only then is a first live $1 order (`Y PM-1`) safe to attempt. The funder-refusal guard blocks a
live order on the unproven-EOA fallback regardless.
