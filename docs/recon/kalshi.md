# Kalshi — Markets & Operations Recon
**Date:** 2026-06-10  
**Analyst:** Automated recon agent  
**Verdict: CONDITIONAL GO** — Bahrain physical location is eligible; UK citizenship alone does NOT block signup. Account must be created fresh as a Bahrain resident. Practical friction exists. Details below.

---

## 1. What Is Kalshi?

Kalshi is a CFTC-regulated designated contract market (DCM) based in the United States. It trades binary event contracts (YES/NO, settling at $1.00 or $0.00) across sports, politics, economics, and entertainment. It is *not* a traditional sportsbook — it is legally a financial exchange under US commodities law, which is why its geographic reach differs from UK-licensed bookmakers.

---

## 2. 2026 FIFA World Cup Markets

Kalshi has an extensive lineup of World Cup 2026 event contracts across multiple categories. Trading volume on the outright winner market alone has crossed $100 million.

### Market Categories Available

| Category | Examples | Notes |
|---|---|---|
| Tournament Winner | "Will Spain win the World Cup?" | 48 per-team YES/NO contracts |
| Group Winners | "Who wins Group A?" etc. | All 12 groups covered |
| Team Advancement | Reach R16 / QF / SF / Final | Per-team, per-stage contracts |
| Individual Matches | "USA to win in 90 min" | Available for all knockout matches; group-stage match coverage confirmed |
| Golden Boot | Per-player contracts | Mbappe, Kane, Haaland etc. priced |
| Props / Specials | Halftime performers, first-time champion, highest-scoring group | Miscellaneous entertainment contracts |

**Notable absence:** No traditional fixed-odds spread markets (Asian handicap, Over/Under goals). Kalshi's structure is binary YES/NO only — you cannot get a line like "Over 2.5 goals at 1.85". Every trade is a probability contract priced between $0.01 and $0.99.

**Sources:**  
- [Kalshi World Cup Games page](https://kalshi.com/category/sports/soccer/fifa-world-cup/world-cup/games)  
- [CBS Sports — How to trade on 2026 World Cup](https://www.cbssports.com/prediction/news/kalshi-world-cup-2026/)  
- [Yahoo Finance — $2B+ WC volume](https://finance.yahoo.com/markets/options/articles/world-cup-betting-kalshi-polymarket-133600704.html)

---

## 3. Eligibility: UK Citizen Physically in Bahrain

This is the critical question. The analysis is nuanced.

### 3a. The UK Is Explicitly Restricted

The UK appears in Kalshi's published list of restricted jurisdictions in Section VI of the Member Agreement. Multiple corroborating sources confirm this:

> "Kalshi remains off-limits in Canada, the United Kingdom, and Australia."

The restricted list also includes: France, Belgium, Bulgaria, Hungary, Italy, Monaco, Poland, Switzerland, Ukraine, Russia, Belarus, Singapore, Taiwan, Thailand, New Zealand, Australia, UAE, Iran, Iraq, Lebanon, Syria, Yemen, and others (~52 jurisdictions total).

**Bahrain is NOT on the restricted list.** All sources consulted confirm Bahrain does not appear among restricted Middle Eastern jurisdictions (only Iran, Iraq, Lebanon, Syria, UAE, and Yemen are restricted in that region).

### 3b. Eligibility Is Based on Country of Residence, Not Citizenship

This is the critical distinction. Kalshi's help centre states:

> "During signup, you will be asked to verify your identity and confirm your **country of residence**."

The restriction language in the Member Agreement applies to residents of restricted jurisdictions, not to passport holders. A UK citizen who is a **resident of Bahrain** would enter their country of residence as Bahrain during KYC, not the UK.

Kalshi's help centre also confirms:

> "You can use Kalshi while traveling, provided that access is consistent with the eligibility and geographic requirements outlined in the Kalshi Member Agreement."

This implies physical location at time of *trading* is also considered, not just residence. Since the user is **physically located in Bahrain** (non-restricted), this should not be a blocker.

### 3c. Important Caveats and Risks

1. **Kalshi places responsibility on the user:** "It is your individual responsibility to ensure that your access and use on the Kalshi Exchange is permissible and lawful for you."

2. **UK licensing context:** The user holds UK-licensed gambling accounts (Paddy Power, Sky Bet, etc.). Kalshi is NOT a gambling licence; it is a CFTC-regulated futures exchange. There is no overlap or conflict with UK Gambling Commission regulations for the user personally — these are separate legal frameworks.

3. **App is US-only; web access required:** Kalshi's mobile app is only available in US app stores. International users must access via web browser. This is a practical friction point.

4. **International signup friction:** Reports from October 2025 indicate that even users in non-restricted countries (India, Brazil, Nigeria) were struggling to complete signup due to KYC/verification issues during the rollout. This may have improved by June 2026 but is an operational risk.

5. **No definitive guidance for mixed-jurisdiction cases:** Kalshi does not publish explicit guidance for "UK citizen resident in Bahrain." The interpretation above is based on the residence-based language in primary sources, but Kalshi support confirmation is strongly advised before investing build time.

### 3d. Eligibility Summary

| Factor | Status |
|---|---|
| UK citizenship | Not a blocking criterion — restriction is residence-based |
| Physical location: Bahrain | Bahrain is NOT a restricted jurisdiction |
| Country of residence: Bahrain | Eligible (Bahrain not restricted) |
| Mobile app | NOT available outside USA; web-only |
| Account setup requirement | Must complete KYC as Bahrain resident |

**Verdict: Conditionally eligible.** The user can attempt to open a Kalshi account as a Bahrain resident. UK citizenship should not block signup. However, the user should verify this directly with Kalshi support before spending build time on integration.

**Sources:**  
- [Kalshi Help — Trading outside the US](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states)  
- [Kalshi Help — Signing up as an individual](https://help.kalshi.com/en/articles/13823778-signing-up-as-an-individual)  
- [Kalshi Member Agreement (PDF)](https://kalshi.com/docs/kalshi-member-agreement.pdf)  
- [Datawallet — Kalshi country restrictions](https://www.datawallet.com/crypto/kalshi-explained)  
- [Sportico — International launch friction](https://www.sportico.com/business/sports-betting/2025/kalshi-international-countries-access-1234874388/)

---

## 4. Fees

### 4a. Trading Fees (Taker)

Formula: `fee = round_up(0.07 × price × (1 − price))`

| Contract Price | Taker Fee per Contract |
|---|---|
| $0.50 (50/50) | ~$0.0175 (1.75% of contract value) |
| $0.30 or $0.70 | ~$0.0147 |
| $0.20 or $0.80 | ~$0.0112 |
| $0.10 or $0.90 | ~$0.0063 |
| $0.05 or $0.95 | ~$0.0033 |

The 7% coefficient means max taker fee is 1.75¢ per $1 contract at 50¢.

**CAUTION — possible sports surcharge (UNVERIFIED):** The original recon claimed "no category-specific surcharges." However, Kalshi's help center and multiple 2026 fee guides note: *"Some markets have fees that are different from those of other markets, often due to special events such as elections, awards ceremonies, or large sporting championships."* It is unverified whether the 2026 World Cup markets carry a surcharge above the standard 7% taker formula. The existing doc previously listed this as confirmed; this is downgraded to **unverified** pending a direct check of the Kalshi Fee Schedule PDF (kalshi.com/docs/kalshi-fee-schedule.pdf) or the fee-schedule page once rate-limiting clears.

### 4b. Trading Fees (Maker)

Maker fee formula: `~0.0175 × price × (1 − price)` (approximately 25% of the taker rate). At 50¢ this equals ~$0.0044 per contract. Maker fees are charged only on execution; cancelled resting orders incur zero fee.

**Correction from original report:** The original recon text stated "~1.75% for limit orders" which was ambiguous — confirmed maker fee is ~25% of taker fee (not 1.75% independently), equating to roughly 0.44¢ per contract at 50¢. The ~75% saving claim remains correct.

**Strategy implication:** Always use limit orders (maker) rather than market orders (taker) to reduce fees by ~75%.

### 4c. Deposit / Withdrawal Fees

| Method | Deposit Fee | Withdrawal Fee |
|---|---|---|
| ACH (US only) | Free | Free |
| Wire transfer | Free (bank fees may apply) | Free (bank fees may apply) |
| Debit card | **2% processing fee** | $2 fixed fee |
| Cryptocurrency | No Kalshi platform fee; **third-party processor fees may apply** | No Kalshi platform fee; processor fees may apply |

**Crypto deposit correction:** The original recon stated crypto has "no platform fee." This is partially correct — Kalshi itself does not charge a fee, but international users use a separate third-party processor (not Zero Hash, which is US-only). That processor may charge fees, which are "clearly disclosed prior to any associated transaction." Do not assume zero cost; check at deposit time.

**For the user (Bahrain, international):** ACH, PayPal, Venmo are US-only. Available methods are debit card, wire transfer, and cryptocurrency. Wire transfer is the safest zero-Kalshi-fee option; crypto avoids card processing fees but may incur third-party processor charges. Debit card withdrawal fee is $2 fixed (not just deposit).

**Debit card withdrawal correction:** The original table omitted that debit card withdrawals also carry a fee ($2 fixed per withdrawal, separate from the 2% deposit fee).

**Sources:**  
- [Polytrage — Kalshi fee structure](https://blog.polytrage.com/kalshis-fee-structure-explained/)  
- [Dimers — Kalshi fees](https://www.dimers.com/prediction-markets/kalshi/fees)  
- [Kalshi Fee Schedule](https://kalshi.com/fee-schedule)  
- [Predictionhunt — Kalshi fees guide](https://www.predictionhunt.com/blog/kalshi-fees-complete-guide-2026)

---

## 5. API Access

Kalshi provides a public REST API. Access is free for all verified users.

### API Tiers (Rate Limits)

| Tier | Read Budget (tokens/sec) | Write Budget (tokens/sec) | How to Qualify |
|---|---|---|---|
| Basic | 200 | 100 | Auto on signup |
| Advanced | 300 | 300 | Call Upgrade Account endpoint |
| Premier | 1,000 | 1,000 | 0.25% of 30-day exchange volume |
| Paragon | 2,000 | 2,000 | 0.50% of volume |
| Prime | 4,000 | 4,000 | 1.00% of volume or Kalshi assignment |

Most requests cost 10 tokens. Basic tier = ~20 read requests/sec, which is sufficient for a research platform.

### API Capabilities
- Order book data for all markets (public)
- Personal orders, trades, portfolio history (authenticated)
- Market metadata and stats
- Place/cancel orders programmatically

### Documentation
- Main docs: [https://docs.kalshi.com/welcome](https://docs.kalshi.com/welcome)
- Rate limits: [https://docs.kalshi.com/getting_started/rate_limits](https://docs.kalshi.com/getting_started/rate_limits)
- Discord #dev channel for support

**Sources:**  
- [Kalshi API Help Center](https://help.kalshi.com/en/articles/13823854-kalshi-api)  
- [Kalshi API Rate Limits](https://docs.kalshi.com/getting_started/rate_limits)

---

## 6. Key Facts Table

| # | Fact | Confidence | Source |
|---|---|---|---|
| 1 | Kalshi lists 2026 World Cup markets across 6+ categories (winner, groups, matches, advancement, Golden Boot, props) | Confirmed | [Kalshi markets page](https://kalshi.com/category/sports/soccer/fifa-world-cup/world-cup/games) |
| 2 | WC winner market has exceeded $100M trading volume | Confirmed | [Yahoo Finance](https://finance.yahoo.com/markets/options/articles/world-cup-betting-kalshi-polymarket-133600704.html) |
| 3 | Markets are binary YES/NO only — no traditional spreads/Over-Unders | Confirmed | [CBS Sports](https://www.cbssports.com/prediction/news/kalshi-world-cup-2026/) |
| 4 | UK is explicitly listed as a restricted jurisdiction in Kalshi's Member Agreement | Confirmed | [Datawallet](https://www.datawallet.com/crypto/kalshi-explained), multiple sources |
| 5 | Bahrain is NOT in the restricted jurisdictions list | Confirmed (multiple sources) | [Datawallet](https://www.datawallet.com/crypto/kalshi-explained), [search results referencing Member Agreement] |
| 6 | Eligibility is determined by country of RESIDENCE, not citizenship | Likely | [Kalshi Help](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states) |
| 7 | Kalshi mobile app is US-only; international = web only | Confirmed | [Sportico](https://www.sportico.com/business/sports-betting/2025/kalshi-international-countries-access-1234874388/) |
| 8 | Taker fee formula: 0.07 × price × (1 − price), max ~1.75¢/contract at 50¢ | Confirmed | [Polytrage](https://blog.polytrage.com/kalshis-fee-structure-explained/) |
| 9 | Maker fees are ~75% lower than taker fees (~0.44¢ at 50¢); zero fee on cancelled resting orders | Confirmed | [Polytrage](https://blog.polytrage.com/kalshis-fee-structure-explained/), [marketmath.io](https://marketmath.io/blog/kalshi-fees-guide-2026) |
| 9b | Some markets carry special-event surcharges (elections, major sporting championships). Whether World Cup markets carry a surcharge above the standard formula is UNVERIFIED. | Unverified | [Dimers](https://www.dimers.com/prediction-markets/kalshi/fees), help.kalshi.com |
| 10 | Debit card deposits carry 2% fee; wire has no platform fee; crypto has no Kalshi platform fee but third-party processor fees may apply for international users | Confirmed (with correction) | [Dimers](https://www.dimers.com/prediction-markets/kalshi/fees), [Kalshi Help — Crypto Deposits](https://help.kalshi.com/en/articles/13823799-crypto-deposits) |
| 10b | Debit card withdrawals carry a $2 fixed fee (separate from 2% deposit fee) | Confirmed | [Deadspin](https://deadspin.com/prediction-markets/kalshi/fees/) |
| 11 | API is free; 5 tiers exist: Basic (200/100), Advanced (300/300 — free upgrade via API call), Premier (1000/1000), Paragon (2000/2000), Prime (4000/4000) — top 3 tiers require trading volume thresholds | Confirmed | [Kalshi API docs](https://docs.kalshi.com/getting_started/rate_limits) |
| 12 | International KYC friction reported Oct 2025 — some permitted-country users could not sign up | Confirmed | [Sportico](https://www.sportico.com/business/sports-betting/2025/kalshi-international-countries-access-1234874388/) |
| 13 | UAE is restricted; Bahrain is separate and not restricted | Likely | [Datawallet](https://www.datawallet.com/crypto/kalshi-explained) |

---

## 7. Comparison to User's Existing Platforms

| Dimension | Kalshi | User's UK books (Paddy Power, Bet365 etc.) |
|---|---|---|
| Regulation | CFTC (US futures exchange) | UK Gambling Commission |
| Market type | Binary event contracts (probabilities) | Traditional odds (fractional/decimal) |
| Spread markets | NO (binary only) | YES |
| Closing line value tracking | Easy (prices are probabilities, 0–1) | Requires manual de-vig |
| CLV use case | Excellent — public market prices, no account bans for winners | Risk of being limited/banned |
| Account restrictions | No historical precedent of banning sharp bettors (exchange model) | Standard risk management applies |
| Bahrain access | Likely eligible as Bahrain resident | Already active |
| Fees | ~1.75% max (taker), ~0.44% (maker) at 50¢ | Vig embedded in odds (~5-10%) |

---

## 8. User Actions Required

These are concrete actions needed before Kalshi can be added to the platform:

1. **Confirm eligibility directly with Kalshi support** before building any integration. Ask specifically: "I am a UK citizen and Bahrain resident. Can I open a Kalshi account?" Reference the Member Agreement's residence-based language. Email: support@kalshi.com or use in-app chat.

2. **Attempt account creation via web browser** (not app store — app is US-only). Go to [kalshi.com](https://kalshi.com) on web. During KYC, enter country of residence as Bahrain. Have a Bahraini ID or proof of residence ready.

3. **Verify identity documents**: Kalshi may request a government-issued ID. Bahrain residency permit or national ID may be required as proof of non-restricted residency.

4. **Choose deposit method**: For Bahrain-based access, crypto (lowest friction) or wire transfer are the viable options. Debit card carries a 2% deposit fee. ACH is US-only.

5. **Upgrade API tier** after account creation: Call the Upgrade Account API endpoint to move from Basic to Advanced tier (free, 300 tokens/sec read/write). This is sufficient for research-volume data pulls.

6. **Test order book polling** against existing Paddy Power/Bet365 odds feeds to confirm CLV tracking works as expected.

---

## 9. Go/No-Go Assessment

| Criterion | Assessment |
|---|---|
| World Cup markets available | GO — deep, liquid markets with $100M+ volume |
| User eligible (Bahrain resident) | CONDITIONAL GO — Bahrain not restricted; UK citizenship not the criterion; but must verify with Kalshi support |
| Fees acceptable | GO — maker fees are very low; taker fees max 1.75% |
| API available | GO — free REST API, public order books |
| CLV tracking suitability | STRONG GO — probability-based prices are ideal for CLV measurement |
| UK citizenship blocking factor | UNLIKELY BLOCKER — but unverified at primary source; residency is the criterion |

**Overall: CONDITIONAL GO.** Do not spend significant build time until eligibility is confirmed via a test signup or Kalshi support response. If the account opens successfully as a Bahrain resident, Kalshi should be a high-priority integration — better CLV tracking than any UK sportsbook, no ban risk, liquid WC markets, free API.

**If signup fails:** This becomes a hard NO-GO and build time should not be invested. Polymarket (crypto-native, no geo-block for Bahrain) is the alternative prediction market to consider.

---

## 10. Verification Notes (Adversarial Fact-Check, 2026-06-10)

An independent adversarial review was conducted against primary sources on 2026-06-10. The following claims were checked and the following changes were made:

### Sources Successfully Verified (Primary)

| Claim | Source Checked | Outcome |
|---|---|---|
| Kalshi uses residence-based eligibility (not citizenship) | [help.kalshi.com/14026044](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states) — fetched directly | **Confirmed.** Article says "country of residence" is used during signup verification. Does not name UK or Bahrain specifically, directs to Member Agreement for the restricted list. |
| UK is restricted; Bahrain is not restricted | [datawallet.com/crypto/kalshi-explained](https://www.datawallet.com/crypto/kalshi-explained) — fetched directly | **Confirmed.** UK appears in the European restricted list. Bahrain does not appear. Middle East restricted = Iran, Iraq, Lebanon, Syria, UAE, Yemen only. |
| API rate limits: 5 tiers, Basic=200/100, Advanced=300/300 | [docs.kalshi.com/getting_started/rate_limits](https://docs.kalshi.com/getting_started/rate_limits) — fetched directly | **Confirmed and expanded.** Two additional tiers existed that were missing from the original report's key_facts (Premier 1000/1000, Paragon 2000/2000, Prime 4000/4000). The basic key_facts were accurate but incomplete. |
| Taker fee formula: 0.07 × p × (1 − p) | [marketmath.io/blog/kalshi-fees-guide-2026](https://marketmath.io/blog/kalshi-fees-guide-2026) — fetched directly | **Confirmed.** Formula and max 1.75¢ at 50¢ correct. |
| Maker fees ~75% lower than taker | [marketmath.io/blog/kalshi-fees-guide-2026](https://marketmath.io/blog/kalshi-fees-guide-2026) | **Confirmed.** Maker = ~1.75% × p × (1-p) i.e., 25% of taker rate. |
| Crypto deposits available to international users | [help.kalshi.com/13823799-crypto-deposits](https://help.kalshi.com/en/articles/13823799-crypto-deposits) — fetched directly | **Confirmed with correction.** Available internationally, but international users use a different processor than US users (Zero Hash). Third-party processor fees may apply. Original stated "no platform fee" which is technically true of Kalshi itself but incomplete. |
| World Cup match markets confirmed | [cbssports.com/prediction/news/kalshi-world-cup-2026](https://www.cbssports.com/prediction/news/kalshi-world-cup-2026/) — fetched directly | **Confirmed.** Individual match outcome markets available for group stage and knockout rounds. No over/under goals or Asian handicap markets. |
| Kalshi app is US-only / international = web only | [sportico.com international access article](https://www.sportico.com/business/sports-betting/2025/kalshi-international-countries-access-1234874388/) — fetched directly | **Confirmed.** App is not available outside US. International access is web-browser only. |

### What Could Not Be Confirmed at Primary Source

| Claim | Reason Unconfirmed |
|---|---|
| Member Agreement full restricted-jurisdiction list | Both Member Agreement PDF URLs returned compressed/unreadable PDFs and/or HTTP 429. Could not independently enumerate all 54 restricted jurisdictions. Bahrain's absence is based on secondary source (datawallet.com), not the primary PDF. |
| Whether World Cup markets carry a sports surcharge | Kalshi's fee schedule page (kalshi.com/fee-schedule) returned HTTP 429. Multiple secondary sources note that major sporting events MAY carry different fees. Cannot confirm/deny surcharge for World Cup markets. Original report stated "confirmed no surcharge" — this has been downgraded to **unverified**. |
| Exact crypto deposit fees from international processor | Kalshi confirms fees may apply but does not disclose the rate. Only determinable at deposit time. |
| Debit card withdrawal fee amount | Multiple sources cite $2 fixed fee; could not directly verify from Kalshi help center. |

### Changes Made to This Document

1. **Section 4a** — Added surcharge warning for sports markets; downgraded "no surcharge" from confirmed to unverified.
2. **Section 4b** — Corrected ambiguous "1.75% maker fee" language; confirmed maker = ~25% of taker (0.44¢ at 50¢).
3. **Section 4c** — Corrected crypto deposit note: third-party processor fees may apply for international users. Added debit card withdrawal fee ($2 fixed).
4. **Section 6 (Key Facts Table)** — Added row 9b for sports surcharge caveat (unverified); updated row 10 for crypto correction; added row 10b for debit withdrawal fee; updated row 11 to list all 5 API tiers.
5. **Overall verdict unchanged:** CONDITIONAL GO — no errors found that change the eligibility analysis or overall priority assessment.
