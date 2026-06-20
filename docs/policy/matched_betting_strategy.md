# Matched Betting Strategy

## Overview

World Cup Alpha uses two distinct matched-betting playbooks depending on the venue:

1. **New customer offers** (Betfair, Virgin Bet, Paddy Power, Bet365, Sky Bet, Coral): signup bonuses that convert to risk-free profit via hedging
2. **Zero-commission venues** (Smarkets): no EV buffer; only +EV on genuine model edge

## New Customer Offers (Sign-up Bonus Route)

### Setup Bonus Mechanics

Most UK sportsbooks offer "bet £10, get £X in free bets":

- **Qualifying bet:** Back £10 at the book at min odds (typically 2.0+) on any market. Example: Mexico @ 1.45 on Betfair Sportsbook.
- **Lay-off hedge:** Simultaneously lay the same outcome at Betfair Exchange or Smarkets. Cost: ~£0.20–0.50 spread (tightest on liquid WC group games).
- **Free bet extraction:** Once the bonus arrives (~24h), place the free bets at high odds (5.0–8.0) and lay each leg at the exchange.

### Expected Value

- **Qualifying bet cost:** £0.20–0.50 spread per book
- **Free bet per book:** £30–£50 typically
- **Extraction:** SNR (stake-not-returned) free bets at 6.0 odds yield ~£23–25 per £30 free bet after hedging
- **Total per book:** £22–25 guaranteed profit

Across 6–8 books over a tournament: **£150–200 risk-free**, which funds early-stage model validation or promo costs.

### Execution Rules

1. **Only back yourself:** Account must be in your own name, funded by your own debit card, operated by you. Do NOT operate accounts in someone else's name (gnoming) — books detect this via device fingerprints and payment patterns; outcome is voided winnings + confiscated balances.

2. **KYC first:** Upload ID immediately after signup, before depositing. Don't let the book freeze you at withdrawal.

3. **Sequence:** Don't sign up to all books on day one. Stagger over 2–3 weeks (one per weekend game). Books restrict promo access ("gubbing") for accounts that only chase value; varying signup dates + occasional mug bets on favorites keeps you under the radar for long-term access.

4. **Don't over-extract:** Place one or two "normal-looking" bets (low odds, small stake) on accounts you want to keep for model bets (Virgin Bet, Betfair Exchange especially).

## Zero-Commission Venues (Smarkets)

### The Difference

Smarkets offers **0% commission on World Cup trades** (instead of signup bonuses). This removes the EV buffer:

- **Betfair Sportsbook:** 5% overround on typical markets; signup bonuses subsidize marginal bets
- **Smarkets:** 0% commission; odds are market consensus with minimal rake
- **Result:** Smarkets prices are tight. No bonus means you need genuine model edge to break even

### Decision Logic

**Only bet Smarkets outright if:**
```
model_prob > (1 / odds) + 0.03
```

Equivalently: if your model says 15% to win and odds are 6.5x (15.4% implied), **don't take it**. You need 2–3% daylight minimum to account for model uncertainty.

**Example:**
- Your model: France 16% to win WC
- Smarkets odds: 5.8x (17.2% implied) — fair, maybe slight overround. Skip.
- Smarkets odds: 5.2x (19.2% implied) — your model has 3% edge. Take it.

### Best Use Cases for Smarkets

1. **Hedging:** Lay off matching bets with 0% commission (saves vs Betfair Exchange 2–4% commission). E.g. qualify at Virgin Bet, lay at Smarkets.
2. **Model mismatches:** Bet mid-tier teams (10–20x) where you think the market is overweighting favorites and underweighting deep-run probability.
3. **NOT outright winner picks** without strong model conviction (World Cup favourite markets are tight and efficient).

## Which Strategy to Use

| Scenario | Use |
|---|---|
| New book, signup bonus available | **Signup bonus route** — lock in risk-free profit, ignore pure odds |
| Matching a qualifying bet | **Smarkets to lay** (0% commission saves money) |
| Your model disagrees on an outright (e.g. thinks a 15.0x team should be 12.0x) | **Smarkets outright** if edge >2–3% |
| Everyday match betting (1X2, anytime scorer) with model picks | **New customer accounts** until offers exhausted, then regulated venues only |

## Pre-Registered Rules

- **Kill rule:** Pause all sportsbook activity if rolling-50 CLV < 0 (per Kelly policy)
- **Promotional bets** (signup bonuses, free bets) are logged as `type=PROMO`, excluded from CLV/calibration, and never count against daily exposure caps (they're hedged/arbs)
- **Smarkets edge threshold:** minimum 2% daylight after accounting for model uncertainty; otherwise skip
