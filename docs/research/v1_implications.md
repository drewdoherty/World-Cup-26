# v1 Implications — What the Combined Evidence Says

**Date:** 2026-06-10. **Scope:** v1 = international Elo + time-decayed Dixon-Coles + Shin-devig + logistic blend + quarter-Kelly + CLV tracking. Derived from the merged bibliography; every claim below traces to a primary source unless flagged. Max 2 pages.

## The one-sentence finding
No statistical model in the entire reviewed literature beats a sharp closing line on Brier/log-loss (Forrest 2005; Ridall 2025; Schauberger & Groll 2018; Hvattum 2010) — so **v1's job is not to beat the market, it is to (a) be well-calibrated against the de-vigged close and (b) bank CLV by getting on early where the model and market diverge**, with capital preservation as the binding constraint.

## What v1 SHOULD do
1. **Weight the market heavily in the blend.** Market out-forecasts every structural model (Hvattum 2010: Elo RPS 0.213 vs market 0.200; Schauberger 2018: RF beats market at match level by ~0.008 but NOT at tournament level). Egidi (2018) puts the optimal model/market mix near 0.5/0.5 on *dense* league data; with *sparse* international data the market deserves more. **Blend on log-odds; start market 0.6 / structural 0.4**, and widen the structural share only on a specific thesis (confirmed rotation, debut team with no Elo, identified dead rubber).
2. **Use goal-margin-aware structural inputs, not pure W/D/L.** ELO-Goals > ELO-Result (Wunderlich-Memmert); pi-ratings > Elo on RPS (Constantinou). Cap goal margin at 4 to limit blow-out noise.
3. **Keep Dixon-Coles as the goal core with the ρ correction.** Model family barely matters (penaltyblog: all within 4th-decimal RPS); DC has the best tooling and ρ is well-validated (~-0.13). Truncate the score matrix at 10 goals.
4. **Exclude/down-weight minnows in fitting.** San Marino inflated Cyprus's attack by 36% (Kovalchik 2022). Drop teams with Elo < ~1350 from parameter estimation; apply match-importance weights (WC=4, continental=3, qualifier=2.5, friendly=1; rotated friendly 0.5 — Ley 2019).
5. **Fit the Elo→probability scale empirically.** Most national-team Elo is statistically unconverged (Szczecinski 2026); do not plug Elo differences into the 400-divisor — fit a logistic on 5-7 years of internationals.
6. **Calibrate with Platt scaling on a held-out set, then monitor reliability diagrams.** Isotonic overfits below ~500 examples (Niculescu-Mizil 2005); we have ~192 historical WC group matches. Apply a between-cycle discount (~0.76 to pre-2022 data; Ridall 2025 ω_b).
7. **Track CLV vs the Betfair Exchange de-vigged close as the primary KPI** (Buchdahl: ~50 bets for CLV significance vs ~2,500-4,000 for ROI; Franck 2010: exchange beats every book). Log-loss vs the de-vigged close is the secondary KPI; **never optimise on raw Brier alone** (beatthebookie 2022: best Brier ≠ most profitable).
8. **Line-shop across all books, place early, bet singles, migrate sharp volume to Betfair.** Best-of-N odds ≈ efficient (Angelini 2019); early lines hold less info; parlay overrounds kill multibet-Kelly (Grant & Buchen).

## What v1 should NOT do
- **Do NOT claim or expect a profitable edge from the model itself.** Every profitability result in the literature is in-sample, pre-modern-market, or missing CIs (Dixon-Coles 1997, Constantinou 2013, Wilkens 2026 10-15%, Buchdahl WoC 5-6%). They show the market is *hard to beat*, not that v1 will beat it.
- **Do NOT use full or half Kelly** (Maslov-Zhang drawdown exponent; Baker-McHale shrinkage). Do NOT sum independent quarter-Kelly stakes on multi-match days (Whitrow). Do NOT spread a stake across H/D/A — bet only the highest-edge outcome (Whelan 2025).
- **Do NOT carry the +100 Elo / club home-advantage into neutral 2026 venues** (set ~0, or ~+30-40 Elo for USA/Canada/Mexico host group games only).
- **Do NOT use RPS as the headline metric** (Wheatcroft) or accuracy at all (Gneiting-Raftery).
- **Do NOT interpret the Shin z as an insider/sharp signal** (Whelan 2025) — keep Shin as a mechanical de-vig only.
- **Do NOT treat Pinnacle as available** (not in The Odds API) or **Bet365 as machine-readable** (no feed). Do NOT compare a knockout 1X2 price against a "to advance" price (different settlement basis).

## Parameter recommendations (with the contradiction flagged)
| Parameter | Recommendation | Basis / confidence |
|---|---|---|
| **DC time-decay half-life** | **~1,000-1,100 days (3yr), cross-validate over [600, 1500]** | Ley 2019 (international men's, strongest evidence). **CONTRADICTED** below. |
| ρ (low-score) | Estimate jointly; expect ~-0.13; grid [-0.20, 0] | Dixon-Coles; Maher |
| Home advantage | 0 neutral; +30-40 Elo / γ≈1.05 host group games | eloratings recalibrated for neutral venues |
| Blend (log-odds) | market 0.6 / structural 0.4 start; fit by log-loss on 2018+2022 holdout | Egidi (0.4-0.6); tournament_forecasting (0.6-0.7) |
| Calibration | Platt (2-param) on held-out WC group matches | Niculescu-Mizil; small-sample |
| **Kelly fraction** | **α=0.25 nominal, but effective f = α·S·f* with Baker-McHale S≈0.5-0.7 → 0.15-0.20 of full Kelly for the first 30 bets** | MacLean; Baker-McHale; practitioner_clv |
| EV / edge threshold | Bet only when model p − de-vigged market p ≥ ~3pp (≈2% gross Kelly edge) | bayesian_ensembles; staking |
| Per-bet cap | min(quarter-Kelly, 5% of bankroll) ≈ $30-50 on $1,000 | Thorp; Ziemba-Hausch |
| Simultaneous-day cap | total open exposure ≤ 15-20% bankroll; do not sum stakes | Whitrow; Thorp |
| Same-team/group correlation | 0.7× haircut on summed correlated stakes | Whelan; Whitrow |
| Minimum odds | no hard floor in evidence; in practice avoid extreme favourites (>~85% implied) where de-vig/Shin is unstable and FLB is small | Strumbelj; statsbet FLB |
| Monitoring trigger | 7-day mean CLV < 0 → cut to ~1/8 Kelly and review | Baker-McHale; Buchdahl |

## Where the evidence is WEAK or CONTRADICTORY (be honest)
- **Time-decay half-life is genuinely contradicted across our own sections.** ratings.md + goal_models.md + tournament_forecasting.md converge on **~3 years (1,095 days, Ley 2019, international)**. But **bayesian_ensembles.md recommends ~140 days (decay 0.005/day)** — that figure is extrapolated from Rue-Salvesen/Ridall **EPL** dynamics and is almost certainly wrong for national teams playing ~8-15 matches/year (fast decay throws away the little data that exists). **Resolution: use the 3-year Ley value; the 140-day number is a club-football artefact. Cross-validate before trusting either.**
- **Optimal blend weight is unsettled:** 0.5 (Egidi, dense leagues) vs 0.6-0.7 market (tournament_forecasting). Both are reasonable; must be fit on 2018/2022 holdout, not assumed.
- **No international-tournament market-efficiency study exists** — the entire efficiency literature is domestic European leagues. The 48-team / 3rd-place-qualifier format is unprecedented; all calibration is on 32-team WCs. Expect larger errors on minnow and dead-rubber matches.
- **FLB direction is inconsistent:** classical longshot bias (Snowberg) vs reverse/favourite bias on modern Betfair (Angelini 2022). Do not build a back-favourites or fade-longshots rule.
- **Dead-rubber and shootout adjustments are anecdotal/small-sample** (n<500 shootouts; 58%±5% first-kicker). Use wide uncertainty, reduce stakes, do not treat as precise inputs.
- **The Baker-McHale shrinkage S** itself needs a sample-size input that is shaky for sparse international data — directionally right (use < quarter-Kelly), numerically uncertain.

## The 3 biggest open risks
1. **Account longevity, not model quality, is the binding constraint.** 4.3% of UK accounts restricted, >50% of them net losers (UKGC 2025); Paddy Power/WH/Bet365 restrict winners fast. **Mitigation:** spread 5-7 books, mug bets, vary stakes/timing, migrate every sharp/CLV-positive bet to Betfair Exchange — but the Betfair **live API key (£499, 1-3 day activation) will not be ready for matchday 1**, so matchday-1 betting is manual, and Bahrain account-use is an unresolved Betfair ToS grey area (verify with support, no VPN).
2. **Calibration/validation gap.** v1 has never been backtested; ~192 historical WC group matches is too few for confident calibration, and the 48-team format means even that sample is partly off-distribution. We could be systematically mis-priced on exactly the minnow/dead-rubber matches where we think we have edge. **Mitigation:** backtest match-by-match on 2018 + 2022 vs de-vigged close before sizing up; start at 0.15-0.20 effective Kelly; gate stakes on a consensus check (model agrees with exchange) and on CLV staying positive over the first 30 bets.
3. **Data/benchmark fragility.** Pinnacle (the literature's gold-standard close) is not accessible; Bet365 has no feed; international xG is sparse (FBref dead since Jan 2026); Polymarket's SDK is broken for new wallets; Betfair historical World Cup coverage is only "likely." **Mitigation:** Betfair Exchange de-vigged close is the CLV benchmark (statsbet 2026 confirms it ≈ bet365 on accuracy); reconstruct Elo from the martj42 CSV; treat any single odds source as fallible (Tetlock: even liquid prices aren't exact truth).
