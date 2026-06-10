# Polymarket Recon — 2026 FIFA World Cup

**Date:** 2026-06-10 | **Analyst:** Recon subagent | **Status:** Pre-tournament (first match ~25 hours away)

---

## Key Facts Table

| # | Fact | Confidence | Source |
|---|------|-----------|--------|
| 1 | Bahrain IP is NOT geoblocked — live API call to `/api/geoblock` returned `blocked: false`, country `BH` | **confirmed** | `https://polymarket.com/api/geoblock` (live call) |
| 2 | 33 countries fully blocked; Bahrain absent from list. Middle East exceptions: Iran, Iraq, Lebanon, Syria, Yemen only | **confirmed** | https://docs.polymarket.com/api-reference/geoblock |
| 3 | UK is on the fully-blocked list | **confirmed** | https://docs.polymarket.com/api-reference/geoblock |
| 4 | Sports markets taker fee = 3% of `C × p × (1-p)` — peaks at 0.75 USDC per 100 shares at $0.50 | **confirmed** | https://docs.polymarket.com/trading/fees |
| 5 | Makers never pay fees; receive 25% daily pUSD rebate (sports category) | **confirmed** | https://docs.polymarket.com/trading/fees |
| 6 | No deposit or withdrawal fees on Polymarket's own platform | **confirmed** | https://docs.polymarket.com/trading/fees |
| 7 | MoonPay onramp fee ~2-3%; CEX-to-Polygon route costs ~$0.01 gas on Polygon | **likely** | https://docs.polymarket.com/trading/bridge/deposit + secondary sources |
| 8 | World Cup Winner outright market: $1,900,627,218 volume, ~$379M liquidity. Spain 16.4%, France 16.1%, England 10.8% (minor rounding from original report: Spain 16.5% / France 16% / England 10.9% — all within 0.1pp) | **confirmed** | https://polymarket.com/event/world-cup-winner (live fetch 2026-06-10) |
| 9 | Individual group-stage match markets (moneyline 3-way: Home/Draw/Away): live with $100K–$1.83M volume per match | **confirmed** | https://polymarket.com/sports/world-cup/games |
| 10 | Match markets resolve on **90 minutes + stoppage time only**; extra time and penalties excluded | **confirmed** | https://polymarket.com/sports/world-cup/fifwc-mex-rsa-2026-06-11 (live market rules) |
| 11 | Draws ARE a tradeable outcome in match markets (3-way moneyline); resolves as distinct "Draw" outcome | **confirmed** | https://polymarket.com/sports/world-cup/fifwc-mex-rsa-2026-06-11 |
| 12 | Group winner markets resolve via "official tiebreak procedure of the 2026 FIFA World Cup" if teams tie | **confirmed** | https://polymarket.com/event/world-cup-group-a-winner |
| 13 | Resolution via UMA Optimistic Oracle: 2-hour challenge window; dispute escalates to token-holder vote (~48h) | **confirmed** | https://docs.polymarket.com/concepts/resolution |
| 14 | Gamma API: public, no auth, base `https://gamma-api.polymarket.com`; rate limits 300-500 req/10s on key endpoints | **confirmed** | https://docs.polymarket.com/api-reference/rate-limits |
| 15 | CLOB API: `https://clob.polymarket.com`; trading requires L1 (EIP-712 wallet sign) then L2 (HMAC-SHA256) headers | **confirmed** | https://docs.polymarket.com/developers/CLOB/authentication |
| 16 | Known SDK bug (May 2026): ALL new users (post April 28, 2026) using the deposit wallet (POLY_1271 sig type) cannot place orders via Python, TypeScript, OR Rust SDKs — L1 auth binds API key to EOA, not deposit wallet. "Rust SDK unaffected" is INCORRECT: issue #70 (py-clob-client-v2) confirmed rs-clob-client-v2 has the same gap. | **confirmed** | https://github.com/Polymarket/clob-client-v2/issues/65 + https://github.com/Polymarket/py-clob-client-v2/issues/70 |
| 17 | 500+ total World Cup markets in the FIFA World Cup category (513 per search index); the main predictions listing shows ~20 curated/featured markets but the category is broader. 12 group winner markets (Groups A–L) confirmed. | **confirmed** | https://polymarket.com/predictions/2026-fifa-world-cup + search results |
| 18 | Collateral is pUSD (Polygon-native USDC wrapper); wins redeem at $1.00/share via CTF collateral adapter | **confirmed** | https://docs.polymarket.com/concepts/resolution |
| 19 | Using a VPN to circumvent geo-restrictions violates ToS and may cause account suspension | **confirmed** | https://help.polymarket.com/en/articles/13364163-geographic-restrictions |

---

## (a) Market Inventory — 2026 World Cup

### Market Types Live as of 2026-06-10

| Market Type | Count | Volume (approx) | Liquidity (approx) | Notes |
|------------|-------|-----------------|-------------------|-------|
| Outright Tournament Winner (binary per team) | ~40 teams | $1.9B total | $379M total | Spain 16.5%, France 16%, England 10.9% |
| Group Winners (A–L) | 12 | ~$630K per group | varies | Resolves June 27 |
| Individual Match Markets (3-way moneyline) | 9+ live, more to open | $100K–$1.83M each | varies | MEX vs RSA $1.83M highest |
| Match spread/totals/goal props | multiple per match | smaller | thin | Available on higher-profile games |
| Player props (Golden Boot, Ball, Assists, Clean Sheets) | 5 | ~$300K total | thin | |
| Advancement (reach R16, QF, SF, Final) | ~48 | varies | varies | Binary per team per round |
| Availability markets (Messi, Neymar, Yamal) | 4 | up to $3M | moderate | |
| Squad selection | 6 | smaller | thin | Resolved June 7 |

**Total active World Cup markets: 100+**
**Aggregate volume: well above $2B when all sub-markets included**

Primary URL: https://polymarket.com/predictions/2026-fifa-world-cup
Games listing: https://polymarket.com/sports/world-cup/games

---

## (b) Bahrain Access — Legal and Practical

### Result: ACCESSIBLE — Not Geoblocked

A live call to `https://polymarket.com/api/geoblock` from the research environment (Bahrain IP `193.188.123.116`) returned:

```json
{ "blocked": false, "country": "BH", "region": "13" }
```

Bahrain is **not** in the 33-country blocked list on the official geo-restriction documentation. The only Middle Eastern countries fully blocked are Iran, Iraq, Lebanon, Syria, and Yemen — all under OFAC sanctions or AML requirements.

### Practical Access Steps for Bahrain

1. No VPN required (and VPN use would violate ToS)
2. Create account via web wallet (MetaMask + Polygon) or Magic Link/email
3. Deposit USDC on Polygon (see section c for costs)
4. Trade freely — no close-only restrictions apply

### Important Legal Caveat

Polymarket does not hold a gambling licence from any regulatory body. Bahrain's online gambling laws are unclear for prediction markets specifically (no confirmed enforcement action). This is a user legal risk to self-assess.

**Note:** The UK, where the user holds licensed gambling app accounts, IS on the blocked list — Polymarket cannot be accessed from UK IPs.

---

## (c) Fee Structure

### Trading Fees

Polymarket uses a **taker-only** fee model. Makers always receive rebates.

**Formula:** `fee = C × feeRate × p × (1 − p)`
- C = number of shares
- p = price per share
- Peaks at p=0.50 (50% probability), tapers to near-zero at extremes

| Category | Taker Fee Rate | Maker Rebate |
|----------|---------------|--------------|
| Sports (incl. soccer) | **3%** | 25% daily pUSD |
| Finance / Politics / Tech | 4% | 25% |
| Economics / Culture / Other | 5% | 25% |
| Crypto | 7% | 20% |
| Geopolitics / World Events | **0% (free)** | — |

**Effective cost examples at 100 shares:**
- Sports market at $0.50 (peak): **$0.75 fee**
- Sports market at $0.30 or $0.70: **$0.63 fee**
- Sports market at $0.10 or $0.90: **$0.27 fee**

### Deposit / Withdrawal Fees

- **Polymarket charges zero deposit or withdrawal fees**
- Polygon gas fees: ~$0.01 per transaction (negligible)

### Onramp Costs

| Method | Estimated Cost |
|--------|---------------|
| Coinbase/Kraken USDC → Polygon withdraw | ~$0–1 total (exchange fee + ~$0.01 gas) |
| MoonPay (card) | ~2–3% (~$20–30 on $1,000) |
| Bridge from Ethereum mainnet | Variable; $5–50 depending on bridge/gas |
| Third-party bridges (DeBridge, Across) for >$50K | Varies; recommended by Polymarket docs |

**Recommended route for $1,000 bankroll:** Buy USDC on Coinbase or Kraken, withdraw directly to Polygon Mainnet wallet address. Cost: under $1.

### Bid/Ask Spreads

Spreads are set by liquidity providers, not Polymarket. On high-volume markets (World Cup Winner, major match moneylines):
- Liquid outright markets: 1–3 cents
- Individual match markets with $500K+ volume: ~2–5 cents
- Low-volume group-stage matches (<$100K): can be 5–15 cents

No official documentation for typical spreads — these are empirical observations from secondary sources.

---

## (d) Soccer Match Market Settlement Rules

### Confirmed Resolution Rule (from live Mexico vs South Africa market)

> "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time. Extra time and penalty shoot-outs are excluded."

**Source:** https://polymarket.com/sports/world-cup/fifwc-mex-rsa-2026-06-11

### Key Settlement Details

| Question | Answer |
|----------|--------|
| Settlement basis | 90 minutes + injury/stoppage time only |
| Extra time included? | NO |
| Penalties included? | NO |
| Draws in group-stage match markets | YES — Draw is a distinct third outcome (~20-30¢ for balanced matches) |
| Primary resolution source | FIFA official statistics (published within 2 hours of match end) |
| Fallback source | Consensus of credible reporting |
| If game postponed | Market stays open until completion |
| If game cancelled permanently | Resolves to "Neither" |
| Official score corrections | NOT re-applied after market resolution |

### Group Winner Markets

Resolution rule for group winner markets:
> "This market will resolve according to the team that wins Group A in the 2026 FIFA World Cup group stage, scheduled for June 11-27, 2026. If multiple teams tie as group winners, this market will resolve according to the official tiebreak procedure of the 2026 FIFA World Cup."

No manual adjudication — it follows FIFA's official tiebreak rules (goal difference, goals scored, head-to-head, etc.).

### Tournament Winner Market

Resolves based on "the national team that wins the 2026 FIFA World Cup" per FIFA. Teams eliminated from the knockout stage resolve immediately to "No." This implies **extra time and penalties are included for the tournament winner** (since FIFA determines the tournament winner via the full match including those periods) — but the specific market rules rely on "official information from FIFA."

### UMA Oracle Resolution Process

1. Proposer posts $750 pUSD bond and submits outcome
2. 2-hour challenge window
3. If disputed: second proposal round; second dispute triggers UMA DVM token-holder vote (~48 hours)
4. Winning tokens redeem at $1.00/share

---

## (e) API Access

### Gamma API (Market Data — Public)

**Base URL:** `https://gamma-api.polymarket.com`
**Authentication:** None required for read access

| Endpoint | Description | Rate Limit |
|----------|-------------|-----------|
| `GET /events` | List events (query with `?q=`, `?closed=false`, `?tag_slug=soccer`) | 500 req/10s |
| `GET /events/{id}` | Single event with all associated markets | 500 req/10s |
| `GET /markets` | List markets with filters | 300 req/10s |
| `GET /markets/{id}` | Single market detail | 300 req/10s |
| `GET /public-search` | Full-text search across markets | 350 req/10s |

**Key response fields:** `question`, `description`, `volume`, `liquidity`, `outcomePrices`, `resolveTime`, `startDate`

### CLOB API (Order Book and Trading)

**Base URL:** `https://clob.polymarket.com`
**Authentication:** Required for trading (L1 + L2); order book reads may not require auth

| Endpoint | Description | Auth | Rate Limit |
|----------|-------------|------|-----------|
| `GET /order-book/{token_id}` | Live bids and asks | Optional | 1,500 req/10s |
| `GET /price?token_id=` | Mid price | Optional | 1,500 req/10s |
| `GET /midpoint?token_id=` | Midpoint | Optional | 1,500 req/10s |
| `GET /prices-history` | Historical price series | Optional | 1,000 req/10s |
| `POST /order` | Place order | Required (L2) | 5,000/10s burst |
| `DELETE /order/{id}` | Cancel order | Required (L2) | included above |
| `GET /orders` | My open orders | Required (L2) | 900 req/10s |
| `GET /trades` | My trades | Required (L2) | 900 req/10s |

### Data API (Historical / Positions)

**Base URL:** `https://data-api.polymarket.com`

| Endpoint | Description |
|----------|-------------|
| `GET /activity` | Global recent trade feed |
| `GET /positions/{address}` | Wallet positions |
| `GET /activity/{address}` | Wallet trade history |
| `GET /time-series/{token_id}` | Historical price series |

### WebSocket (Real-time)

`wss://ws-subscriptions-clob.polymarket.com/ws/market` — subscribe by `token_id` for real-time order book and price updates. Limit: 5 connections/IP.

### Authentication Flow (for trading)

**Step 1 — L1 (one-time credential generation):**
```
POST https://clob.polymarket.com/auth/api-key
Headers: POLY_ADDRESS, POLY_SIGNATURE (EIP-712), POLY_TIMESTAMP, POLY_NONCE
Returns: apiKey (UUID), secret (base64), passphrase (string)
```

**Step 2 — L2 (per-request trading auth):**
```
Headers: POLY_ADDRESS, POLY_SIGNATURE (HMAC-SHA256 of timestamp+method+path+body),
         POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE
```

**SDK:** Official Python (`py-clob-client`), TypeScript (`clob-client`), Rust clients available.

**Known issue (May 2026 — CORRECTED):** ALL new users (post April 28, 2026) who follow the deposit wallet flow (POLY_1271 / sig type 3) cannot place orders via Python, TypeScript, OR Rust SDKs. The bug: L1 auth signs as the raw EOA, binding the API key to the EOA, while orders are signed by the deposit wallet — triggering `"the order signer address has to be the address of the API KEY"`. The original characterisation as "email/Magic Link only" and "Rust unaffected" are both incorrect. Issues: TypeScript (#65 clob-client-v2), Python+Rust (#70 py-clob-client-v2) — both open/unresolved as of 2026-06-10.

---

## User Actions Required

1. **Verify Bahrain access personally** — The API confirmed BH is not blocked as of today. Upon arriving in Bahrain, visit `https://polymarket.com/api/geoblock` in a browser to confirm your live IP is not blocked. Do NOT use a VPN (ToS violation).

2. **Set up a proper EOA wallet** — Use MetaMask or a hardware wallet (not Magic Link / email sign-up) if you intend to use the API for automated trading. The May 2026 SDK bug affects email-signup accounts.

3. **Fund via USDC on Polygon** — Buy USDC on Coinbase or Kraken and withdraw directly to Polygon mainnet. This minimises onramp costs to under $1 on a $1,000 bankroll. Avoid MoonPay card onramp (~$20-30 fee).

4. **Accept counterparty/settlement risk** — Polymarket resolves via UMA Optimistic Oracle, not a regulated sportsbook. Disputed markets can take 4-6 days. Understand this is non-custodial DeFi — no FSCS/FGCS protection.

5. **Note the 90-minute rule** — Match markets settle on 90 min + stoppage time only. This is the same as most UK-licensed books for match result. However: no BTTS, no HT/FT, no Asian lines are confirmed available — check individual market pages.

6. **Check UK access separately** — Your UK-licensed accounts (Paddy Power, Sky Bet, etc.) are NOT usable on Polymarket. UK is on the blocked list. Polymarket is your Bahrain-accessible option only.

7. **Review Bahrain local laws** — No specific enforcement action against Polymarket noted in Bahrain, but the user should satisfy themselves that participation in prediction markets using USDC/crypto is lawful under Bahraini regulations.

8. **Monitor the SDK bug (CORRECTED scope)** — The May 2026 SDK bug affects ALL new deposit-wallet users across Python, TypeScript, AND Rust SDKs (not just email/Magic Link, not just Python/TypeScript). Track https://github.com/Polymarket/clob-client-v2/issues/65 (TypeScript) and https://github.com/Polymarket/py-clob-client-v2/issues/70 (Python + Rust) for fixes. Until patched, no programmatic order placement is possible for new deposit-wallet accounts via any official SDK.

---

## Appendix: Raw API Response — Bahrain Geoblock Check

Endpoint: `GET https://polymarket.com/api/geoblock`
Response (June 10, 2026):
```json
{
  "blocked": false,
  "ip": "193.188.123.116",
  "country": "BH",
  "region": "13"
}
```

---

*Sources: Polymarket docs (docs.polymarket.com), Polymarket help center (help.polymarket.com), live Gamma API calls, live geoblock API call, Polymarket GitHub (github.com/Polymarket), secondary: tradetheoutcome.com, datawallet.com, predictionhunt.com*

---

## Verification Notes (Adversarial Fact-Check — 2026-06-10)

This section records what was independently verified by a second analyst and what was changed.

### What Was Checked

| Claim | Method | Result |
|-------|--------|--------|
| Bahrain geoblock status | Live fetch to `https://polymarket.com/api/geoblock` | Confirmed: `{blocked:false, country:"BH"}` |
| Blocked countries list / UK blocked | Fetched `https://help.polymarket.com/en/articles/13364163-geographic-restrictions` | Confirmed: 33 countries blocked, UK on list, Bahrain absent |
| Sports taker fee formula (3%, peaks $0.75/100 shares) | Fetched `https://docs.polymarket.com/trading/fees` | Confirmed |
| Match settlement = 90 min + stoppage only (no ET/pens) | Fetched live Mexico vs South Africa market page | Confirmed via explicit market rules text |
| UMA Oracle: $750 bond, 2h window, 4-6 days disputed | Fetched `https://docs.polymarket.com/concepts/resolution` | Confirmed |
| Gamma API rate limits | Fetched `https://docs.polymarket.com/api-reference/rate-limits` | Confirmed: /events 500 req/10s, /markets 300 req/10s |
| SDK bug: scope, affected SDKs, and "Rust unaffected" claim | Fetched GitHub issues #65 (clob-client-v2) and #70 (py-clob-client-v2) | Bug confirmed but scope was WRONG — see changes below |
| World Cup winner market volume and probabilities | Fetched `https://polymarket.com/event/world-cup-winner` | Minor rounding differences confirmed (within 0.1pp) |
| "100+ World Cup markets" claim | Fetched predictions page and search results | Category has 513 markets; main listing shows ~20 curated; 12 group winner markets confirmed |

### Changes Made to This Report

1. **SDK bug scope (fact #16, SDK section, Action #8) — MATERIAL CORRECTION:**
   - **Original claim:** "email/Magic Link deposit-wallet users cannot place orders via Python/TypeScript SDKs; Rust SDK unaffected."
   - **What is actually true:** The bug affects ALL new users (post April 28, 2026) using the POLY_1271 deposit wallet flow. This is not limited to email/Magic Link accounts. The Rust SDK (rs-clob-client-v2 v0.5.1) is ALSO affected — issue #70 confirms "same gap" in the Rust implementation. Issue #65 covers TypeScript; issue #70 covers Python and Rust. Both remain open/unresolved.
   - **Implication:** There is currently NO SDK path for programmatic order placement for new deposit wallet accounts, regardless of programming language. This is a more severe blocker than originally characterised.

2. **World Cup market count (fact #17):**
   - Original: "over 100 total World Cup markets"
   - Updated: 513 markets in the FIFA World Cup category per search index; the main predictions page features ~20 curated markets. The "100+" figure was conservative but the correct number is substantially higher. Characterisation updated for accuracy.

3. **Outright winner probabilities (fact #8):**
   - Original: Spain 16.5%, France 16%, England 10.9%
   - Live data: Spain 16.4%, France 16.1%, England 10.8%
   - Difference is rounding only (0.1pp or less); probabilities move continuously. No material correction needed, but noted.

4. **Deposit funding route guidance (fact #6/#7):**
   - The official Polymarket deposit docs (`/trading/bridge/deposit`) no longer reference Coinbase/Kraken/MoonPay by name. The CEX-to-Polygon USDC withdrawal recommendation is sound but is derived from general crypto knowledge, not official Polymarket documentation.
   - Confidence on the specific cost figures (MoonPay ~2-3%, CEX under $1) is downgraded from "confirmed" to "likely" since we could not verify from official Polymarket docs.

### Claims That Were NOT Changed (independently confirmed)

- Bahrain not geoblocked: confirmed via live API call
- UK blocked: confirmed
- 90-minute settlement rule: confirmed via exact quote from live market page
- 3% sports taker fee formula: confirmed
- No platform deposit/withdrawal fees: confirmed
- $750 UMA bond, 2-hour challenge window: confirmed
- 4-6 day disputed resolution timeline: confirmed
- 12 group winner markets: confirmed
- Group winner resolves via FIFA official tiebreak: confirmed
- VPN usage violates ToS: confirmed
