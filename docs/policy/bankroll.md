# Bankroll policy — the fungibility rule

**Status:** governance rule (user, 2026-06-30). Single source of truth for "what
is the bankroll?" Referenced from code at `wca.card`
(`DEFAULT_ACTUAL_CAPITAL_GBP`) and `wca.exposure_corr` (`DEFAULT_BANKROLL`).

## The rule

> **Capital is fungible.** Money moves freely across accounts, venues, and
> currencies (£ ↔ $). The staking bankroll is **always the combined effective
> pool**, never the balance sitting in any single account, venue, app, or
> wallet.

The combined pool is **≈ £3,000**:

| Pool | Amount | Holds |
|---|---|---|
| GBP venues | £1,500 | Smarkets (£), Betfair (£), UK sportsbooks (£) |
| Polymarket | $1,995 (≈ £1,500 at the fixed £1 = $1.33) | on-chain USDC |
| **Total** | **≈ £3,000** | unpartitioned, fungible |

## What this means in practice

- **Size off the combined pool** (or, for the sportsbook ladder, the CLV-earned
  rung — see `wca.card`), then **route** the stake to whichever venue holds the
  bet and move funds to cover it. Top-ups and transfers between accounts/venues
  are assumed frictionless for sizing purposes.
- **A single-account balance is a liquidity/routing detail, not a sizing cap.**
  Example: a bet slip showing a **"£276.57"** sportsbook-app wallet balance does
  **not** mean the bankroll is £276.57. Do not shrink a stake — or call a bet
  "too big" — because one wallet looks light. If a venue is short of cash for a
  bet you want, the answer is "transfer in," not "size down."
- Per-venue balances (`venue_balances` in `wca.card.PoolBankroll`) are an
  **input used to split a recommended deployment**, never to size it.
- Currency is just a unit: Polymarket stakes come out in $ natively off the
  $1,995 pool (the 1.33 FX is baked into that figure); every other venue in £.
  Convert at the fixed £1 = $1.33 when reasoning about the combined pool.

## Why it's written down

When evaluating bet slips it is tempting to read the wallet balance on the
screenshot as "the money available" and treat it as a constraint. That is wrong
under this desk's model — capital is one pool spread across venues for routing
and liquidity, not a set of independent budgets. This rule exists so that
mistake does not recur.
