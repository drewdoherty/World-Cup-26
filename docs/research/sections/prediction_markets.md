# Prediction Markets: Annotated Literature Review

**Topic:** Prediction market efficiency for sports — foundational theory, empirical comparisons with bookmakers, documented arbitrage, fees and transaction costs, liquidity and price impact, and longshot bias.

**Scope of review:** 9 works (6 peer-reviewed papers, 3 serious practitioner/academic-adjacent sources). Coverage spans 1997–2026. Primary KPI for v1 is closing line value (CLV) against de-vigged market baselines; secondary KPI is Brier/log-loss calibration.

---

## 1. Wolfers, J. & Zitzewitz, E. (2004). "Prediction Markets." *Journal of Economic Perspectives*, 18(2), 107–126. NBER WP 10504.

### Summary
The foundational survey establishing the theoretical and empirical case for prediction markets as information-aggregation mechanisms. Wolfers and Zitzewitz define the contract types used in prediction markets (binary outcome, index, and spread contracts), argue that market prices aggregate dispersed information efficiently under a broad set of conditions, and review the empirical track record across political, economic, and sports markets. The paper remains the canonical entry point for the field.

### Methodology
Conceptual framework plus meta-review of existing empirical studies across multiple prediction market platforms (Iowa Electronic Markets, TradeSports, Intrade). No new primary data collection; the authors synthesise existing calibration studies and accuracy benchmarks.

### Data
Cross-market survey: political elections (IEM), economic indicators (various), sports events (TradeSports). No single dataset; relies on secondary citations.

### Key Findings
- Prediction market prices are, on average, well-calibrated: events priced at 70% happen roughly 70% of the time across most platforms.
- Markets outperform "moderately sophisticated benchmarks" (polls, expert panels, simple statistical models) in most domains studied.
- Carefully designed spread contracts can reveal market expectations about means, medians, and distributional uncertainty, beyond binary probabilities.
- Conditional markets — where one contract pays contingent on another event — effectively reveal regression-coefficient-style causal beliefs.

### Effect Sizes / Numbers
The calibration evidence in this paper is qualitative and graphical rather than formally quantified with Brier scores; the authors do not report aggregate RMSE across the studies they survey. The claim about outperforming benchmarks is supported by citation to individual studies rather than a meta-analytic aggregate. **This is a limitation: no single summary effect size is provided.**

### Relevance to v1
The paper provides theoretical justification for treating de-vigged bookmaker closing odds as the best available probability baseline — which is exactly the "Shin-devigged market baseline" in v1. The finding that markets outperform moderately sophisticated models suggests our Elo/Dixon-Coles blend must clear a high bar before adding value above the market line.

### Implementation Ideas
- Use closing-line prices from Betfair Exchange (peer-to-peer, lowest overround) as the benchmark for CLV measurement, not retail bookmaker prices.
- Treat Betfair closing odds as a second calibration target alongside our own model output.
- Do not assume our model is better than the market as a prior; test this empirically game-by-game throughout the group stage.

### Skepticism Note
This paper is now 22 years old. TradeSports (the main sports platform cited) closed in 2008. Modern prediction markets (Polymarket, Kalshi) and sharp sportsbooks (Pinnacle, Betfair Exchange) have grown substantially in liquidity and efficiency. The theoretical claims hold, but the empirical calibration evidence is from thin, early-2000s markets. Update with more recent studies.

---

## 2. Wolfers, J. & Zitzewitz, E. (2006). "Interpreting Prediction Market Prices as Probabilities." NBER Working Paper 12200.

### Summary
A theoretically important follow-on to the 2004 survey, written in direct response to Manski (2004) who argued that prediction market prices are *not* necessarily equal to mean beliefs. Wolfers and Zitzewitz derive sufficient conditions under which prices do correspond to mean beliefs, and show that even when those conditions are not met exactly, prices remain close to mean beliefs for a broad class of utility functions and belief distributions.

### Methodology
Formal theoretical model. Traders are assumed to have heterogeneous beliefs and CRRA utility. The authors solve for equilibrium prices as a function of risk aversion parameters and belief distributions, then simulate how much prices deviate from true mean beliefs under different parameter regimes.

### Data
Theoretical model plus calibration against IEM political prediction market prices. No sports-specific data.

### Key Findings
- Under log utility (CRRA = 1), the prediction market price exactly equals the wealth-weighted mean belief.
- For a broad class of risk-aversion parameters (CRRA 0.5–2), prices deviate from mean beliefs by less than 2 percentage points in most scenarios.
- The key drivers of price-vs-probability divergence are (1) extreme risk aversion and (2) highly skewed belief distributions. Both are more likely in thin, speculative markets than in well-established sports markets.
- Risk aversion causes prices to compress toward 50%, i.e., favourites are underpriced and longshots overpriced — the same direction as the favourite-longshot bias, but for a different theoretical reason.

### Effect Sizes / Numbers
Simulations show typical price-to-probability divergence of 1–3 percentage points under realistic parameters. At extreme risk aversion (CRRA > 5), divergence can reach 5–10 pp, but such values are implausible for liquid markets with many participants.

### Relevance to v1
This paper directly validates the practice (used in our v1) of treating de-vigged Betfair closing odds as probability estimates. The 1–3 pp systematic bias from risk aversion is small enough that the Shin devigging method (which models this bias explicitly) should adequately correct for it.

### Implementation Ideas
- The Shin devigging method accounts for exactly the risk-aversion/informed-money distortion this paper models. Our existing Shin baseline is theoretically well-motivated.
- For World Cup binary markets (e.g., "will X qualify from group"), risk-aversion compression is small when markets are liquid. For long-shot tournament winner contracts, divergence could be larger; be cautious interpreting those as true probabilities.

### Skepticism Note
The model is theoretical; calibration is tested only against IEM political markets, which are structurally different from sports betting exchanges. The finding that prices are "close to" true probabilities does not mean they are *equal* — a 2–3 pp systematic bias is meaningful for Kelly staking.

---

## 3. Tetlock, P. C. (2008). "Liquidity and Prediction Market Efficiency." Working paper, Columbia Business School (SSRN 929916).

### Summary
A major empirical paper using TradeSports data to test whether greater liquidity improves prediction market accuracy. Tetlock finds a **counterintuitive negative result**: higher liquidity is not associated with better calibration and sometimes correlates with *greater* mispricing. The mechanism he proposes is that limit-order traders on TradeSports are naive about adverse selection — they do not realise they are being picked off by better-informed traders — and their presence slows information incorporation rather than accelerating it.

### Methodology
Panel regression using TradeSports binary outcome contracts from March 2003 to October 2006 (three-plus years). Liquidity measured three ways: (1) bid-ask spread, (2) total trading volume, (3) open interest (market depth). Efficiency measured as deviation of prices from realised outcomes. Fixed effects for event type (sporting vs. financial) and time-to-resolution.

### Data
TradeSports exchange data, ~3.5 years of continuous 30-minute snapshots. Mix of sporting events (primarily NFL, NBA) and financial/political contracts.

### Key Findings
- Liquidity does not reduce price deviations from outcomes; in some specifications it increases them.
- The favourite-longshot bias persists even in the most liquid TradeSports contracts.
- Limit orders executing during "informative" events (major news breaks) earn **negative expected returns**, consistent with informed traders taking the other side.
- The adverse selection problem is more severe in sports markets (where private information from injury news etc. exists) than in financial contracts.

### Effect Sizes / Numbers
The paper documents that limit orders placed around the time of major news events have negative expected returns, though exact basis-point figures are not universally reported in abstract/summary sources. The key quantitative contribution is the regression coefficient on liquidity being zero or slightly positive in predicting mispricing — meaning an extra unit of volume does *not* reduce the bid-ask deviation from true probability.

### Relevance to v1
Critical finding: **do not assume that a liquid Betfair or Polymarket price is a perfect probability estimate.** Active limit-order placement by naive participants can keep prices away from efficiency even in seemingly liquid markets. In the hours before a World Cup match, our CLV measurement may reflect adverse selection dynamics rather than pure probability discovery.

### Implementation Ideas
- Measure CLV against prices at least 1 hour before kick-off (rather than the very last price), to avoid the most intense informed-trader activity in the final minutes.
- Treat the Betfair closing price as a floor on market information, not as an exact true probability. Weight our model output to move slightly toward the closing price, not entirely replace it.
- Consider whether "closing line" for CLV purposes should be 5 minutes before kick-off (maximum information incorporation) or 60 minutes before (less adverse selection noise).

### Skepticism Note
TradeSports closed in 2008 and was a relatively illiquid market by modern standards. Betfair Exchange and modern decentralised prediction markets have orders of magnitude more depth. The adverse-selection finding may be less severe in today's more liquid markets. Tetlock's 2008 result should be treated as a warning rather than a current empirical law.

---

## 4. Levitt, S. D. (2004). "Why Are Gambling Markets Organised so Differently from Financial Markets?" *The Economic Journal*, 114(495), 223–246.

### Summary
Steven Levitt uses a unique proprietary dataset from an NCAA tournament betting contest to show that bookmakers do *not* balance books — they intentionally take positions and exploit bettor biases. The paper inverts the conventional wisdom that bookmakers set prices to equalise action on both sides (thereby eliminating their own risk); instead, Levitt shows they set prices to maximise expected profit by anticipating which side naive bettors will back disproportionately.

### Methodology
Analysis of betting records from a large real-money NFL betting contest, combined with final outcomes. Tests whether the balanced-book hypothesis holds (i.e., whether roughly 50% of dollars are on each side of each game), and whether the side receiving more action wins or loses more frequently.

### Data
NFL football season data. The balanced-book hypothesis was tested using the actual distribution of wagers across sides. Only 20% of games had 50–55% of wagers on one side; in the majority, one side received 66% or more of all bets.

### Key Findings
- The balanced-book hypothesis is rejected: bookmakers routinely accept imbalanced action.
- The side receiving a disproportionate majority of wagers loses more than 50% of the time — exactly what the "exploiting bettor bias" model predicts.
- Bookmakers are better forecasters than bettors (they set prices; bettors choose sides) and they profit from this information advantage.
- The required breakeven winning percentage for bettors against the standard NFL spread is 52.38%, and the data show bettors achieve well below this on average.

### Effect Sizes / Numbers
The majority of games had 66%+ of wagers on one side. The implication is that bookmakers capture a risk premium above the standard vig by correctly identifying which side will lose. Levitt does not report a specific Sharpe ratio or ROI figure for bookmakers in the abstract, but the directional finding is strongly significant.

### Relevance to v1
Confirms that bookmaker prices are *not* pure probability estimates — they are contaminated by the bookmaker's exploitation of bettor bias. This is the core theoretical reason why Shin devigging (which models informed-money extraction) outperforms simpler additive/multiplicative devigging. Our baseline should account for this price distortion.

### Implementation Ideas
- Do not use raw bookmaker odds as the probability baseline. Always devig (Shin method preferred per the ScienceDirect "On determining probability forecasts" study, which found Shin outperformed 217 of 412 bookmaker/sport pairs).
- Be aware that for popular World Cup teams (England, Brazil, Argentina), the Levitt effect predicts that bookmakers shade lines against them to exploit recreational bettor loyalty. This means those teams are likely slightly more overpriced by UK sportsbooks than the Betfair Exchange price suggests.

### Skepticism Note
The dataset is from an NFL betting contest — participants may not be representative of all bettors. The NFL/US context may not generalise perfectly to European football with different market structures (UK bookmakers, Asian handicap markets). The 20+ year age of this paper is also relevant; the rise of betting exchanges and sharp Asian handicap syndicates has pushed bookmakers toward more balanced pricing since 2004.

---

## 5. Franck, E., Verbeek, E. & Nüesch, S. (2010). "Prediction Accuracy of Different Market Structures — Bookmakers versus a Betting Exchange." *International Journal of Forecasting*, 26(3), 448–459.

### Summary
The most directly relevant comparison of traditional bookmakers vs. a betting exchange (Betfair) for football outcome prediction. Using 5,478 games across three seasons from the "Big Five" European leagues (England, France, Germany, Italy, Spain), the authors show that Betfair prices systematically outperform every single bookmaker in predictive accuracy, and that the gap is attributable to bookmakers shading prices to exploit supporter sentiment rather than pure probability.

### Methodology
Out-of-sample forecast evaluation using Brier scores and simulated returns. Compares eight different bookmakers' closing odds against Betfair Exchange closing prices for the same matches. Also tests for bookmaker sentiment shading using team fan-base proxies.

### Data
5,478 football matches from England (Premier League), France (Ligue 1), Germany (Bundesliga), Italy (Serie A), Spain (La Liga), spanning three seasons up to 2008/09. Closing odds from 8 named bookmakers; Betfair closing exchange prices.

### Key Findings
- Betfair Exchange closing prices outperform every bookmaker individually in terms of Brier score.
- Betfair also outperforms the consensus average of all bookmakers.
- A combined bet (backing at bookmaker, laying at Betfair) yields a **guaranteed positive return in 19.2% of matches** — a measure of systematic bookmaker mispricing relative to the exchange.
- Bookmakers shade prices toward teams with large fan bases (evidence: English and Spanish leagues), offering sub-par odds on popular teams.

### Effect Sizes / Numbers
- 19.2% of Big Five matches contained guaranteed arbitrage between a bookmaker and Betfair Exchange.
- The calibration advantage of Betfair over the average bookmaker corresponds to a meaningful Brier score reduction (exact figures require access to the full paper, but the 19.2% arb rate is the key headline).

### Relevance to v1
Directly validates using Betfair Exchange (or Smarkets) as the primary CLV benchmark rather than retail UK bookmakers. The 19.2% arb finding is extraordinary — it means using retail bookmaker prices as a probability baseline introduces systematic error for nearly 1 in 5 matches. Our system should lock in Betfair Exchange closing price as the definitive probability reference.

### Implementation Ideas
- Record both the Betfair closing price and the best available retail bookmaker price at the time of bet placement. CLV is measured against Betfair close, not bookmaker close.
- For teams with large UK fan bases (England, most Premier League clubs), expect bookmakers to shade odds. This is a structural edge that our model should capture if our probability estimate is anchored to exchange prices.
- Systematic book/exchange arb is not accessible without a Betfair account (which is available to this user). Even if the 19.2% figure has shrunk since 2009 due to market maturation, the directional finding is likely still valid.

### Skepticism Note
The 2010 data predates the current era of pricing algorithms and sharp account management. The 19.2% guaranteed return figure has almost certainly declined as bookmakers have improved their exchange-monitoring tools. Replication with post-2015 data would be valuable; the directional finding (exchange beats bookmakers) is widely replicated but the magnitude is probably overstated for 2026.

---

## 6. Spann, M. & Skiera, B. (2009). "Sports Forecasting: A Comparison of the Forecast Accuracy of Prediction Markets, Betting Odds and Tipsters." *Journal of Forecasting*, 28(1), 55–72.

### Summary
Compares three forecasting approaches — a real-money prediction market, fixed-odds bookmaker lines, and expert tipsters — using three seasons of German Bundesliga data. The key findings are that prediction markets and betting odds perform nearly identically in predictive accuracy, both strongly outperform expert tipsters, and combined forecasts add modest value. Despite positive prediction accuracy, none of the approaches generated systematic profit due to the high takeout (25%) of the state-owned German bookmaker used.

### Methodology
Direct head-to-head comparison across 678 games with predictions available from all three sources (up to 837 games for two-way comparisons). Accuracy metrics: percentage of correct match-winner predictions, RMSE, and simulated profit under fixed-stake betting.

### Data
German Bundesliga, three seasons (1999–2000 through 2001–2002). 837 games for prediction market vs. odds; 721 for tipsters; 678 for all three combined.

### Key Findings
- Prediction market accuracy: 54.28% (all three combined games), 16.20% simulated profit.
- Betting odds accuracy: 53.69%, 13.49% simulated profit.
- Tipsters: 42.63%, -0.19% (essentially break-even on raw accuracy but useless for profit).
- Naive home-win benchmark: 50.88%, 12.44% simulated profit.
- When all three methods agreed (380 of 678 games): accuracy rose to 57.11%, 13.86% profit.
- Combining predictions via a rule-based method (backing only when all three agree) substantially improves accuracy over any single method.

### Effect Sizes / Numbers
- 54.28% vs. 53.69%: prediction market edge over odds is less than 1 percentage point — not practically significant.
- The 57.11% accuracy when all three agree vs. 54.28% average is approximately +3 pp, which is more meaningful.
- Despite positive accuracy, none yielded systematic profit under 25% takeout — confirming that edge must exceed takeout substantially to be realised.

### Relevance to v1
The near-identical accuracy of prediction markets and bookmaker odds in this study supports treating them as interchangeable probability sources. The actionable finding for v1 is the **consensus signal**: when our Elo/Dixon-Coles model agrees with both the bookmaker and exchange price, the bet has higher conviction. When only our model disagrees with the market, conviction should be lower.

### Implementation Ideas
- Implement a "consensus score" that measures how many of: (1) our model, (2) Betfair exchange price, (3) best available retail bookmaker odds — agree on the edge direction. Only bet at full Kelly when all three agree.
- The 25% takeout result should not discourage; UK and Betfair taxes are far lower (Betfair charges 2–5% on winnings, not 25% takeout).

### Skepticism Note
The data is from German Bundesliga 1999–2002 and the "prediction market" in this study was probably Intrade/Betfair in an early, illiquid form. Bundesliga is a three-outcome market (home/draw/away) which makes it structurally harder than binary World Cup match-winner markets. The specific accuracy percentages (54% vs. 53%) should not be taken as universal constants.

---

## 7. Borghesi, R. (2009). "An Examination of Prediction Market Efficiency: NBA Contracts on TradeSports." *Journal of Prediction Markets*, 3(2).

### Summary
Tests weak-form efficiency of NBA prediction market contracts on TradeSports. Finds evidence of systematic mispricing in specific price bands, with contracts priced around $25 winning less frequently than implied, and contracts priced around $75 winning more frequently. This is a departure from the standard favourite-longshot bias (where longshots are overpriced); instead, the bias is partially reversed compared to NFL markets. NBA contracts show greater efficiency than the NFL TradeSports market studied by O'Connor and Zhou.

### Methodology
Calibration analysis: actual win frequencies by price decile vs. implied probabilities. Standard chi-square tests for departure from efficiency. Comparison to NFL results.

### Data
NBA TradeSports contracts (all available games). Data collection assistance acknowledged for NBA contract data.

### Key Findings
- Contracts around the $25 level (25% implied win probability) won less frequently than expected — consistent with classic longshot overpricing.
- Contracts around the $75 level won more frequently than expected — consistent with favourite underpricing.
- The magnitudes of deviations are smaller than those in the NFL TradeSports market studied contemporaneously.
- Low-priced contracts near $2.50 won more frequently than predicted, contradicting simpler longshot-bias models (a "reverse longshot" for extreme underdogs).

### Effect Sizes / Numbers
Abstract-level: deviations "less than those in the NFL market." Exact percentage point deviations require full paper access. The $2.50 finding is the most counterintuitive and potentially exploitable result.

### Relevance to v1
Caution: the study is from a closed, illiquid exchange. However, the finding that mispricing patterns differ between sports and between probability ranges is directly relevant to our calibration strategy. For World Cup group matches, the equivalent observation would be that heavy favourites (e.g., Brazil vs. an African debutant) may be systematically underpriced even on the exchange.

### Implementation Ideas
- Check calibration of our model vs. Betfair specifically in the 70–85% implied-probability range (heavy group-stage favourites), as this band shows the largest deviations in TradeSports NBA data.
- After the group stage, plot predicted vs. actual win rates by probability decile to build an ongoing calibration curve.

### Skepticism Note
TradeSports NBA data from the 2000s. Very thin by modern standards. The extreme-longshot $2.50 result (wins more often than implied) contradicts most other literature and could be noise in a small sample. Do not overfit to this specific finding.

---

## 8. O'Connor, P. & Zhou, F. (2008). "The TradeSports NFL Prediction Market: An Analysis of Market Efficiency, Transaction Costs, and Bettor Preferences." *Journal of Prediction Markets*, 2(1).

### Summary
Detailed analysis of 1,587 NFL point-spread contracts on TradeSports during the 2005/06 season. Finds no traditional favourite-longshot bias but does find a systematic bias in the opposite direction: the market consistently underestimated the probability of the favoured team covering the spread by approximately 10 percentage points across all odds categories. Transaction costs were roughly half those of traditional US bookmakers, but the informational bias was persistent.

### Methodology
Calibration of 1,587 NFL TradeSports point-spread contracts vs. realised outcomes. Volume analysis for in-play vs. pre-game trading. Comparison of effective takeout rates (TradeSports vs. Las Vegas bookmakers).

### Data
TradeSports NFL 2005/06 season, 1,587 contracts. Trading volume data from platform logs.

### Key Findings
- No traditional favourite-longshot bias detected in the spread market.
- Consistent "reverse" bias: favourites (those covering the spread) were underestimated by approximately **10 percentage points** across all price categories.
- TradeSports effective takeout ~2.2% for popular contracts vs. ~4.55% for traditional legal bookmakers — a meaningful cost advantage.
- In-running trading volume was **roughly twice** pre-game volume.
- Volume correlated with team market size (larger-market NFL teams attracted more trading).

### Effect Sizes / Numbers
- 10 pp systematic underestimation of favourites covering the spread — a large and persistent bias.
- TradeSports takeout 2.2% vs. 4.55% traditional: a 52% reduction in transaction costs.
- In-play volume approximately 2x pre-game volume.

### Relevance to v1
The 10 pp favourite-underpricing finding is large if real and general. For World Cup group matches, the equivalent would be heavy favourites (Brazil, Argentina, Spain, France) being systematically underpriced in prediction markets relative to true probability. However, this finding is from point-spread NFL contracts on a thin exchange, so generalisation is uncertain.

### Implementation Ideas
- The transaction cost comparison is directly relevant: Betfair Exchange (2–5% on winnings, not on stakes) is far cheaper than UK retail bookmakers (typical overrounds 8–12% for football). Route all volume through the exchange.
- Monitor in-play price movements — the 2x volume finding suggests the bulk of price discovery happens in-play. For live betting strategy (post-v1), this is important.

### Skepticism Note
The 10 pp underpricing of favourites is very large and specific to NFL point-spread markets on TradeSports. It may reflect a structural quirk of how point-spread prediction markets work, not a general finding. European football result markets (no spread) may not exhibit the same bias. Treat with caution.

---

## 9. Angelini, G., De Angelis, L. & Singleton, C. (2022). "Informational Efficiency and Behaviour Within In-Play Prediction Markets." *International Journal of Forecasting*, 38(1), 282–299.

### Summary
The most recent and methodologically sophisticated paper in this review. Uses Betfair Exchange football betting data to test informational efficiency around clean informational events — specifically the moment the first goal is scored. Finds pre-match and in-play mispricing explained by a **reverse favourite-longshot bias** (favourite bias), with market prices underestimating the probability of the longshot recovering. Mispricing is largest when the goal is scored late and by a longshot team, as the market is slow to update to the correct posterior probability.

### Methodology
Event-study design using first-goal timing as an exogenous informational shock. High-frequency Betfair Exchange data for English Premier League and other European leagues. Bayesian updating model specifies what prices "should" be immediately after a goal; actual prices are compared to the theoretical posterior. Mispricing magnitude is measured as a function of match state (lead size, time remaining, goal scorer identity).

### Data
Betfair Exchange in-play betting data for football. Multiple European leagues. High-frequency (sub-minute) price data around goal events.

### Key Findings
- Evidence of **reverse favourite-longshot bias** (favourite bias): markets underestimate the winning probability of the trailing team — even after goal events.
- Mispricing is *larger* when a goal is scored by the longshot team later in the match — exactly when updating should be most impactful.
- The market is slow to incorporate news that favours the underdog, suggesting lingering anchoring to pre-match probabilities.
- Pre-match prices on Betfair also exhibit slight favourite bias (contrary to the classical longshot bias from parimutuel horse racing).

### Effect Sizes / Numbers
The paper finds statistically significant mispricing in a direction opposite to the traditional longshot bias. Exact basis-point effects require full paper access, but the finding of a *reverse* bias on a modern, liquid exchange is the key result — it contradicts the naive expectation that Betfair always overprices longshots.

### Relevance to v1
This is the most directly applicable study. The finding that Betfair Exchange exhibits **favourite bias** (not longshot bias) for football is directly relevant to our CLV measurement strategy. If the exchange systematically underprices longshots pre-match, our model needs to be willing to back longshots when the model says they are underpriced, even if the Betfair price looks "low."

### Implementation Ideas
- Build a calibration diagnostic: after each matchday, compare predicted probabilities vs. Betfair closing prices by implied probability decile. If we see consistent "model says X% but market says Y%" patterns in the 15–35% range, this is the favourite-bias effect in action.
- For World Cup group matches involving clear underdogs (African nations vs. European giants), our model may be more accurate than the exchange in estimating the true win probability of the underdog.
- Do not blindly shrink our model toward the exchange price. The shrinkage should be asymmetric: heavier shrinkage in 40–60% range (where market is most efficient) and lighter shrinkage in 15–30% range (where favourite bias is largest).

### Skepticism Note
This finding of reverse longshot bias on Betfair is not universally replicated. The TradeSports literature found different directional biases, and some studies find no systematic bias at all on modern exchanges. The result is from a specific event-study design (first goal); it may not apply to pre-match prices in the same magnitude. Also, exploiting in-play mispricing in real time requires automated systems that v1 does not yet have.

---

## Supplementary Note: Snowberg & Wolfers (2010). "Explaining the Favourite-Longshot Bias: Is it Risk-Love or Misperceptions?" *Journal of Political Economy*, 118(4), 723–746.

### Summary
Resolves the longstanding debate about the source of the favourite-longshot bias in horse racing using compound lottery data. Finds strong evidence for **probability misperception** (over-weighting small probabilities, consistent with Prospect Theory) rather than risk-love as the primary mechanism. This matters for prediction markets because it means the bias is cognitive, not preference-based, and therefore more likely to persist even among sophisticated participants.

### Key Findings
- Expected returns: approximately 85% for 1/1 favourites, 75% for 20/1 longshots, 55% for 50/1 longshots in UK horse racing.
- Compound lottery pricing reveals systematic over-weighting of small probabilities, not systematic risk preference.
- The finding is consistent across UK horse racing data.

### Implementation Idea for v1
The 15–30% implied-probability range is where this bias is most actionable. Our logistic blend should place more weight on our model (vs. the market) when the market-implied probability for the underdog is in the 10–25% range, since this is where misperception is largest. This aligns with Angelini et al.'s favourite-bias finding on Betfair as well.

---

## Practical Implications for v1 System

The v1 system uses: international Elo + time-decayed Dixon-Coles + Shin-devigged market baseline + logistic blend + quarter-Kelly staking. The literature above yields the following concrete, prioritised actions:

### 1. Betfair Exchange is the CLV benchmark, not retail bookmakers (High priority)
Franck et al. (2010) find that Betfair Exchange closing prices outperform every bookmaker including the consensus, and that 19.2% of Big Five matches contained guaranteed bookmaker/exchange arbitrage. Retail bookmaker prices are contaminated by sentiment shading (Levitt 2004). Use Betfair closing price as the denominator in all CLV calculations. For the user's context (UK-licensed apps including Betfair Exchange), this is directly accessible.

### 2. Shin devigging is theoretically justified and empirically validated (High priority)
Levitt (2004) confirms that bookmakers shade prices to exploit bettor bias, not to balance books. Wolfers & Zitzewitz (2006) show that risk-aversion causes systematic price compression toward 50%. The Shin method, which models informed-money extraction explicitly, is preferred over additive/multiplicative devigging. The ScienceDirect meta-study found Shin outperformed 217 of 412 bookmaker/sport pairs; use Shin as the devigging baseline in v1.

### 3. Favourite-bias is more relevant than longshot-bias for modern Betfair football markets (Medium priority)
Angelini et al. (2022) find a **reverse** favourite-longshot bias on Betfair Exchange for football (the market underprices longshots, not overprices them). This means: (a) our model may have genuine edge backing underdogs when we are above the exchange price, and (b) shrinkage toward the exchange price should be weaker in the 15–30% probability range than in the 45–55% range.

### 4. Liquidity is not a proxy for accuracy — apply appropriate skepticism to thin markets (Medium priority)
Tetlock (2008) finds that liquidity does not improve prediction market accuracy and can worsen it via naive limit-order provision. For less-traded World Cup matches (group-stage matches from weaker confederations, late fixtures where interest is lower), the Betfair closing price is less reliable than for marquee fixtures. Weight the model more heavily and the market less in these cases.

### 5. Transaction cost economics favour the exchange; route volume accordingly (Medium priority)
O'Connor & Zhou (2008) document that TradeSports takeout was 2.2% vs. 4.55% for traditional bookmakers — a 52% reduction in costs. Betfair Exchange structure is similar. For any bet size where both Betfair and a retail bookmaker offer the same implied probability, prefer Betfair due to lower structural vig. However, retail bookmakers may offer better prices for promotional reasons (enhanced odds); always check. Betfair's 5% commission on winning bets implies an effective takeout of ~2.5% on a 50% probability bet, which is competitive.

### 6. Consensus signal adds value; avoid acting on model-only edges in isolation (Lower priority)
Spann & Skiera (2009) find that accuracy improves to 57.11% when all three sources agree, vs. 54.28% for any single source. For v1, implement a simple consensus check: when our model and Betfair both suggest the same side has value, bet at full quarter-Kelly. When only our model disagrees with both the bookmaker and exchange, apply a 50% Kelly reduction as a model uncertainty discount.

### 7. Polymarket/Kalshi are not useful liquidity sources for the 2026 World Cup (Lower priority for v1)
As of 2025–2026, Polymarket and Kalshi sports markets are dominated by US sports (NFL, NBA, MLB account for 90%+ of volume on Kalshi). World Cup markets exist but have thin liquidity and wide spreads for anything outside the top 5–10 most-bet matches. The Kim et al. (2025) lead-lag paper on LLM-filtered Granger causality between prediction markets and sportsbooks is preliminary and methodologically complex. Do not build a prediction-market-vs-sportsbook arbitrage strategy for v1; assess after the group stage when more liquidity data is available.

### 8. Proof-of-edge standard: closing line value positive in aggregate, Brier score below Betfair baseline (Ongoing KPI)
The literature consistently shows that markets are hard to beat (Spann & Skiera: no systematic profit despite positive accuracy). The correct standard for v1 is not "profitable in the group stage" (sample too small) but "positive CLV against Betfair close in aggregate." If we are systematically beating the Betfair closing line by even 1–2%, that is strong evidence of edge, consistent with the findings across all these papers.

---

## Works Cited

1. Wolfers, J. & Zitzewitz, E. (2004). Prediction Markets. *Journal of Economic Perspectives*, 18(2), 107–126.
2. Wolfers, J. & Zitzewitz, E. (2006). Interpreting Prediction Market Prices as Probabilities. NBER WP 12200.
3. Tetlock, P. C. (2008). Liquidity and Prediction Market Efficiency. SSRN 929916.
4. Levitt, S. D. (2004). Why Are Gambling Markets Organised so Differently from Financial Markets? *Economic Journal*, 114(495), 223–246.
5. Franck, E., Verbeek, E. & Nüesch, S. (2010). Prediction Accuracy of Different Market Structures — Bookmakers versus a Betting Exchange. *International Journal of Forecasting*, 26(3), 448–459.
6. Spann, M. & Skiera, B. (2009). Sports Forecasting: A Comparison of the Forecast Accuracy of Prediction Markets, Betting Odds and Tipsters. *Journal of Forecasting*, 28(1), 55–72.
7. Borghesi, R. (2009). An Examination of Prediction Market Efficiency: NBA Contracts on TradeSports. *Journal of Prediction Markets*, 3(2).
8. O'Connor, P. & Zhou, F. (2008). The TradeSports NFL Prediction Market: An Analysis of Market Efficiency, Transaction Costs, and Bettor Preferences. *Journal of Prediction Markets*, 2(1).
9. Angelini, G., De Angelis, L. & Singleton, C. (2022). Informational Efficiency and Behaviour Within In-Play Prediction Markets. *International Journal of Forecasting*, 38(1), 282–299.
10. Snowberg, E. & Wolfers, J. (2010). Explaining the Favourite-Longshot Bias: Is it Risk-Love or Misperceptions? *Journal of Political Economy*, 118(4), 723–746. [Supplementary note]
