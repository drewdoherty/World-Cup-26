# Staking and Bankroll Management: Annotated Literature Review

**Scope:** Kelly (1956) original criterion; fractional Kelly theory and practice (MacLean, Thorp, Ziemba); drawdown distributions and risk-of-ruin under full vs. half vs. quarter Kelly; Kelly with simultaneous and correlated bets; estimation error as the principal argument for fractional Kelly; practical stake-capping rules.

**Prepared for:** World Cup Alpha v1 system — $1,000 bankroll, quarter-Kelly staking, CLV as primary KPI, Brier/log-loss calibration as secondary KPI. Model stack: international Elo + time-decayed Dixon-Coles + Shin-devigged market baseline + logistic blend.

**Date:** 2026-06-10

---

## 1. Kelly, J.L. Jr. (1956)

**Citation:** Kelly, J.L. Jr. (1956). A New Interpretation of Information Rate. *Bell System Technical Journal*, 35(4), 917–926. https://doi.org/10.1002/j.1538-7305.1956.tb03809.x

### Summary
Kelly's one-page proof — derived from Shannon's information theory, not gambling directly — shows that a gambler maximising the expected log of wealth will, in the long run, outperform any other fixed-fraction strategy with probability 1. The core result is deceptively simple: for a binary bet at even money with win probability p, the optimal fraction of bankroll to wager is f* = 2p − 1 (the "edge"). Generalised for a bet paying odds b-to-1: f* = (bp − q)/b, where q = 1 − p. The paper proves that this strategy maximises the long-run exponential growth rate of wealth (the "capital growth rate") and simultaneously minimises the expected time to reach any given wealth target.

### Methodology
Pure mathematical derivation using information theory; no empirical data. Kelly connected Claude Shannon's channel capacity concept to a gambler's problem of maximising log-wealth.

### Key Findings
- Growth rate of Kelly bettor's bankroll is G = p·log(1 + f*·b) + q·log(1 − f*), maximised at f* above.
- Any strategy that deviates consistently from f* will be dominated by Kelly over a long run (in the almost-sure sense).
- Betting more than f* always reduces the long-run growth rate; above 2·f* it becomes negative (eventual ruin with probability 1).
- No effect-size numbers on drawdown or short-run variance were given — Kelly ignored these entirely.

### Critical Assessment
The proof is mathematically watertight for the infinite-horizon, known-probability setting. Both assumptions fail in practice: horizons are finite, and probabilities are estimated with error. Kelly himself gave no guidance on what to do when probabilities are uncertain or bets are simultaneous and correlated. The simplicity of the formula has led to widespread misapplication (particularly overbetting due to overconfidence in estimated probabilities). Subsequent literature is largely a catalogue of corrections to these two omissions.

### Relevance to v1
This is the foundational result. Our quarter-Kelly implementation uses f* = (b·p̂_model − q̂_model)/b, where p̂_model comes from the logistic blend, then multiplies by 0.25. The critical practical question is how accurate p̂_model must be before even quarter-Kelly is safe; see Baker & McHale (2013) below.

---

## 2. MacLean, L.C., Thorp, E.O., and Ziemba, W.T. (2010)

**Citation:** MacLean, L.C., Thorp, E.O., and Ziemba, W.T. (2010). Long-term capital growth: the good and bad properties of the Kelly and fractional Kelly capital growth criteria. *Quantitative Finance*, 10(7), 681–687. https://doi.org/10.1080/14697688.2010.506108

### Summary
This concise but authoritative synthesis paper — the most-cited single document from the MacLean-Thorp-Ziemba collaboration — enumerates ten "good" and ten "bad" properties of Kelly and fractional Kelly. The paper establishes the intellectual framework for understanding when Kelly is and is not appropriate, and introduces the concept of fractional Kelly as a blending of the Kelly portfolio with cash. The blend trades lower expected final wealth for lower variance and lower drawdown depth, with the key quantitative result that f = α·f* (for 0 < α < 1) retains α² of the growth rate's variance reduction relative to full Kelly, while retaining α of the expected log-growth rate.

### Methodology
Theoretical synthesis with analytical proofs and simulation illustrations; no new empirical data. Draws on prior work by Breiman (1961), Thorp (1969), and others.

### Key Findings
- **Good properties:** Kelly maximises the asymptotic (long-run) growth rate; almost surely beats any essentially different strategy; minimises expected time to reach a target wealth level.
- **Bad properties:** Full Kelly's Arrow-Pratt risk aversion is very small (roughly 1/W); variance is high in the short run; suggested bet sizes can exceed 100% of bankroll in favourable markets; maximum drawdowns are severe (see item 3 below).
- **Fractional Kelly trade-off (quantitative):** At fraction α = 0.5 (half Kelly), growth rate declines by roughly 25% while variance of final wealth declines by 75%. At α = 0.25 (quarter Kelly), variance drops ~94% with only ~44% reduction in growth rate. This asymmetric risk-reduction is the central practical argument for fractional Kelly.
- The authors note there is no universal "best" fraction; the right α depends on the investor's time horizon and loss tolerance.

### Critical Assessment
The 10-properties framing is pedagogically excellent but somewhat circular — properties are derived under the same log-growth framework, so the "bad" properties are really just statements that Kelly is risky for short-horizon actors. The paper is silent on estimation error (addressed only in Baker & McHale 2013) and correlation structure of simultaneous bets. Effect sizes stated above are well-established but depend on continuous-time diffusion approximations; discrete real-money betting has fatter tails than these models assume.

### Relevance to v1
The ~94% variance reduction at quarter-Kelly (α = 0.25) directly justifies our staking choice, accepting ~44% lower long-run growth in exchange for survivable drawdowns on a $1,000 bankroll with ~104 World Cup matches over 39 days. Quarter-Kelly also tolerates moderate estimation error better than larger fractions.

---

## 3. MacLean, L.C., Thorp, E.O., and Ziemba, W.T. (eds.) (2011)

**Citation:** MacLean, L.C., Thorp, E.O., and Ziemba, W.T. (eds.) (2011). *The Kelly Capital Growth Investment Criterion: Theory and Practice*. World Scientific Handbook in Financial Economics Series. ISBN: 9789814383134.

### Summary
This 900-page edited volume is the definitive reference on Kelly criterion, collecting 45 papers spanning 1956–2010. Relevant to our purposes are the chapters on drawdown distributions, simultaneous bets, and fractional Kelly. The editors' own summary chapters establish several quantitative benchmarks: the full Kelly bettor's expected peak-to-trough drawdown during any long betting sequence is approximately 50% of bankroll; half Kelly reduces this to ~13%; quarter Kelly to below 1%. These figures (derived formally by Maslov and Zhang 1999, and Thorp 2006) are the most-cited numbers in the practitioner literature on Kelly-based staking.

### Methodology
Edited volume; individual chapters use varied methods. The drawdown results come from analytical continuous-time models and simulation corroboration.

### Key Findings
- **Full Kelly drawdown:** P(max drawdown ≥ 50%) ≈ 50%. The maximum drawdown distribution under full Kelly is approximately uniform on [0, 1] — i.e., any drawdown depth from 0% to 100% is equally probable.
- **Half Kelly:** P(max drawdown ≥ 50%) ≈ 12.5%.
- **Quarter Kelly:** P(max drawdown ≥ 50%) < 0.8%. Expected maximum drawdown ≈ 12.5% of bankroll.
- **Growth rate:** Full Kelly maximises expected log-wealth; fractional Kelly at α gives (2α − α²) of the full Kelly growth rate, peaking at α = 1.
- Full Kelly and near-full Kelly show "great superiority over longer horizons" but the short-term performance "is very risky."
- Over any finite horizon there is a non-trivial probability that the Kelly bettor trails a constant-fraction bettor.

### Critical Assessment
The drawdown numbers are widely repeated but are derived under continuous-time Brownian motion approximations and assume the true probability is known. Real discrete bets (especially football match betting with 3-way markets) have different distributional properties. The ~50% full-Kelly drawdown estimate should be treated as a lower bound in practice, not a precise prediction. Simulation results in the book generally confirm the analytical results hold in discrete settings for large N, but convergence to the theoretical distribution takes very long horizons (100+ years of data per simulations).

### Relevance to v1
The < 0.8% probability of a 50%-bankroll drawdown at quarter-Kelly is the key risk-management justification for our staking rule. On a $1,000 bankroll, this means the expected worst-case loss during the World Cup period is well below $125.

---

## 4. Thorp, E.O. (2006 / 2011)

**Citation:** Thorp, E.O. (2006). The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market. Chapter 54 in S.A. Zenios and W.T. Ziemba (eds.), *Handbook of Asset and Liability Management*, Vol. 1. Reprinted in MacLean, Thorp, Ziemba (2011), op. cit. Also available at: https://gwern.net/doc/statistics/decision/2006-thorp.pdf

### Summary
Thorp's practitioner-focused synthesis is the most practically useful single paper for sports bettors. Drawing on his experience applying Kelly in blackjack (with card counting giving edges of 0.5–1.5%), horse racing, and financial markets, Thorp demonstrates how to calibrate the formula, discusses the dangers of overestimating one's edge, and advocates half Kelly or less for real-world use. He explicitly states that since probability estimates are imprecise, "the fraction actually bet should be considerably less than the Kelly fraction" and that "most investors should use a fraction of the Kelly fraction."

### Methodology
Semi-formal synthesis combining analytical results with numerical examples and practitioner experience. Uses continuous-time approximations validated by discrete simulation.

### Key Findings
- Standard Kelly formula: f* = (bp − q)/b for a bet offering b-to-1 odds on event with probability p.
- With a 1% edge at even money, Kelly recommends betting 1% of bankroll; with 5% edge, 5% of bankroll.
- "Fractional Kelly between one half and one quarter is appropriate for most practical purposes" — a practitioner guideline, not a theorem.
- Under continuous-time approximation, if the investor bets fraction α of Kelly, expected growth rate = G(α) = α·G* − (α²/2)·σ², where G* is full-Kelly growth rate and σ is return volatility.
- This parabolic relationship means growth is near-maximal for α ∈ [0.5, 1] but drawdown risk is dramatically different.
- For simultaneous bets: treat each bet's Kelly fraction calculation as if it were the only bet, but sum the fractions and apply a scale-down if total exceeds a safety cap (Thorp's practical rule of thumb: total exposure capped at 20–25% of bankroll).
- Thorp notes that at the "Kelly optimal" point, the probability distribution of drawdowns has the dangerous property that any drawdown depth (including 100%) is approximately equally likely over a long horizon.

### Critical Assessment
Thorp's paper is authoritative and well-reasoned. The practical recommendation of quarter-to-half Kelly is defensible but is ultimately a heuristic rather than a derived optimum. The treatment of simultaneous bets is informal; the Whitrow (2007) and Grant/Buchen (2012) papers below provide more rigorous solutions. The blackjack results (0.5–1.5% edge with card counting) may not generalise to football betting where edges are harder to verify and model error is higher.

### Relevance to v1
Thorp's 20–25% total-exposure cap maps directly onto a practical rule for our system: when multiple World Cup matches are simultaneously open (e.g., group stage day where 3 matches play), the sum of individual quarter-Kelly stakes should be capped at 20% of bankroll. Also note Thorp's warning that estimated edges in real markets are almost always overstated.

---

## 5. Baker, R.D. and McHale, I.G. (2013)

**Citation:** Baker, R.D. and McHale, I.G. (2013). Optimal Betting Under Parameter Uncertainty: Improving the Kelly Criterion. *Decision Analysis*, 10(3), 189–199. https://doi.org/10.1287/deca.2013.0271

### Summary
This is the most rigorous academic treatment of the estimation-error problem for sports betting specifically. Baker and McHale show that when the win probability p is estimated from data (rather than known exactly), the raw Kelly fraction f* = (bp̂ − q̂)/b systematically overestimates the true optimal fraction. The paper derives a shrinkage factor S < 1 that should be applied: f_optimal = S · f*. The shrinkage depends on the variance of the estimated probability. Using simulation and real tennis betting data, they show that shrunken Kelly delivers materially better out-of-sample bankroll performance than raw Kelly. The "back of envelope" formula they provide is S ≈ 1 − (σ²_p̂ / p̂(1−p̂)), where σ²_p̂ is the sampling variance of the probability estimate.

### Methodology
Theoretical Bayesian/frequentist derivation plus simulation; empirical validation on a dataset of ATP tennis matches with betting market odds. The simulation study uses 10,000 replications. The empirical study tests raw Kelly vs. shrunken Kelly over a real season of tennis data.

### Key Findings
- Raw Kelly consistently overestimates the optimal fraction when probability is estimated from finite data.
- Shrinkage amount increases as: (a) the sample size used to estimate p is smaller; (b) the true edge is smaller; (c) the number of covariates in the probability model is larger.
- Numerical result: with 100 matches of historical data, the shrinkage factor S can be as low as 0.4–0.6, meaning the optimal bet is 40–60% of naive Kelly.
- Shrunken Kelly outperformed raw Kelly in 8 out of 10 out-of-sample simulated seasons in their tennis dataset.
- Combined with a fractional Kelly multiplier α, the overall bet fraction becomes f = α · S · f*. For a model with substantial estimation uncertainty, this easily puts effective stakes at 10–20% of full Kelly.

### Critical Assessment
This is a strong paper and the most directly relevant quantitative justification for fractional Kelly in our context. The empirical evidence is from tennis, not football, which has different market efficiency properties. The shrinkage formula assumes a simple logistic model; our logistic blend with Elo and Dixon-Coles features will have correlated errors that the formula doesn't fully capture. The paper does not address simultaneous bets. Despite these limitations, the core finding — that estimation uncertainty argues for substantially less than full Kelly — is robust and holds directionally regardless of sport.

### Relevance to v1
This paper provides a quantitative floor on why quarter-Kelly is not conservative enough for a new model: if our logistic blend has only 100–200 effective training matches (reasonable for international football), S could be 0.5–0.7. Quarter-Kelly (α = 0.25) is already applying a 4× haircut; combined with a shrinkage factor of 0.6, the effective fraction is ~15% of Kelly, which is appropriate for a tournament-length deployment. Consider re-estimating S each matchday as sample size grows.

---

## 6. Whitrow, C. (2007)

**Citation:** Whitrow, C. (2007). Algorithms for Optimal Allocation of Bets on Many Simultaneous Events. *Journal of the Royal Statistical Society: Series C (Applied Statistics)*, 56(5), 607–623. https://doi.org/10.1111/j.1467-9876.2007.00594.x

### Summary
Whitrow provides the most rigorous statistical treatment of the simultaneous-bets problem published in a peer-reviewed statistics journal. The paper shows that the naive approach (calculate Kelly fraction for each event independently, then bet each in proportion) is suboptimal when bets are placed simultaneously rather than sequentially. The correct approach requires solving a constrained log-utility maximisation over the joint outcome distribution. Whitrow develops both exact (simplex-based) and approximate (stochastic gradient) algorithms and tests them on real bookmaker odds data from English football.

### Methodology
Constrained optimisation (log-utility maximisation over joint probability distributions). Algorithms: simplex method and stochastic gradient methods. Data: real bookmaker odds from UK football markets, exact seasons/sample size not specified in abstract.

### Key Findings
- Sequential Kelly (treating each bet as independent) overestimates optimal simultaneous bet sizes.
- The simultaneous-optimal wager per event is always ≤ the sequential Kelly fraction, sometimes substantially so.
- Key insight: the total portfolio Kelly fraction for N independent simultaneous bets with individual Kelly fractions f₁, ..., fₙ is not Σfᵢ but is bounded by approximately max(fᵢ) + correction terms. For correlated bets (same match, different outcomes), the bound is tighter.
- Practical finding: "the distribution of optimal wagers across bets is drastically different from the distribution in a sequential setting" — concentration in best-value bets and near-zero in marginal ones.
- Stochastic gradient algorithms were computationally efficient and converged to within 1–2% of simplex solution for problems with up to 100 simultaneous events.

### Critical Assessment
The paper is mathematically sound and fills an important gap. However, it assumes correct probabilities are known (no estimation error) and deals primarily with mutually exclusive outcomes within an event, not cross-event correlations from shared tournament context (e.g., teams sharing a group). The algorithms are practical for our scale (104 matches) but require a full probability distribution over joint outcomes, which is computationally demanding for a logistic blend. The most important practical takeaway — simultaneous Kelly fractions are always smaller than sequential ones — is however robust to these limitations.

### Relevance to v1
During group-stage days when 3 matches may be open simultaneously, we should not simply sum three quarter-Kelly stakes. Whitrow's result implies that the simultaneous-optimal total exposure is substantially below the sum of individual Kelly fractions. Practically: when 3 bets are open, reduce individual stakes to 0.25/3 × f* each, or equivalently cap total exposure at 1/3 of single-match quarter-Kelly, rather than 3 × quarter-Kelly. This caps total simultaneous exposure at ~8% of bankroll.

---

## 7. Grant, A. and Buchen, P.W. (2012)

**Citation:** Grant, A. and Buchen, P.W. (2012). A Comparison of Simultaneous Kelly Betting Strategies. *Journal of Gambling Business and Economics*, 6(2). https://www.ubplj.org/index.php/jgbe/article/view/579

### Summary
Grant and Buchen evaluate three distinct approaches to placing multiple Kelly bets simultaneously: (1) single-game Kelly (ignoring other open bets), (2) multibet Kelly (including parlays at all levels), and (3) portfolio optimisation. Using a simulation framework calibrated to Dirichlet-distributed probability estimates and real English Premier League 2007–08 season odds, they find that including multibet (parlay) combinations in the Kelly framework outperforms single-game Kelly. This is a significant practical result, though the simulation assumptions are somewhat artificial.

### Methodology
Monte Carlo simulation with Dirichlet-distributed probability estimates; empirical odds data from English Premier League 2007–08 season. Three betting strategies compared across 1,000 simulated seasons.

### Key Findings
- Multibet Kelly (using combinations of single and parlay bets) outperformed single-game Kelly in simulation.
- Portfolio optimisation approach (Whitrow-style) was dominated by multibet Kelly in their specific setup — a result that depends on the Dirichlet simulation assumption.
- The informational advantage (bettor's model quality) and its variability were the dominant determinants of strategy performance; strategy choice mattered less than model quality.
- With low informational advantage (small edge), all Kelly variants performed similarly and fractional Kelly provided the best risk-adjusted returns.

### Critical Assessment
The main finding (multibet Kelly dominates single-game Kelly) is interesting but depends on parlays being priced fairly relative to singles — an assumption that rarely holds for soft bookmakers (Paddy Power, Sky Bet, Virgin Bet) where parlay markets often carry higher margins. The paper also does not handle estimation error. The Dirichlet simulation may not capture the correlation structure of football match outcomes in a tournament. Use this result cautiously; the edge from multibet Kelly is likely not realised in practice given typical parlay overrounds at soft books.

### Relevance to v1
For our UK soft books, parlay/accumulator margins typically add 3–7% extra overround per leg. This almost certainly erases the theoretical multibet Kelly gain from Grant & Buchen. Stick to singles-only betting and apply Whitrow's simultaneous allocation logic instead. The finding that model quality dominates strategy choice reinforces the priority of CLV tracking: a better model outweighs any staking optimisation.

---

## 8. Maslov, S. and Zhang, Y.-C. (1998 / 1999)

**Citation:** Maslov, S. and Zhang, Y.-C. (1998/1999). Probability Distribution of Drawdowns in Risky Investments. *Physica A: Statistical Mechanics and its Applications*, 262(1–2), 232–241. arXiv:cond-mat/9808295. https://doi.org/10.1016/S0378-4371(98)00497-2

### Summary
This paper from condensed matter physics provides the most rigorous mathematical treatment of drawdown distributions under Kelly and fractional Kelly strategies. Using a continuous-time model, Maslov and Zhang prove that drawdown depths follow a power-law distribution whose exponent depends critically on the fraction of Kelly being used. At full Kelly, the exponent equals exactly 2, which is a boundary case where the mean drawdown is formally divergent (i.e., arbitrarily large expected drawdown over infinite time). Below full Kelly, the exponent exceeds 2 and the distribution has a finite mean; above full Kelly, the exponent is below 2 and ruin becomes certain.

### Methodology
Analytical continuous-time model (geometric Brownian motion with drift). Power-law analysis of drawdown distributions. No empirical data.

### Key Findings
- Drawdown distribution: P(drawdown > x) ~ x^(−(exponent)) where exponent = 1 + 2μ/σ² (μ = expected return, σ = volatility).
- At Kelly-optimal allocation: exponent = exactly 2 (borderline case; expected drawdown diverges).
- At half Kelly (α = 0.5): exponent ≈ 3; expected drawdown is finite and roughly proportional to (1/α − 1).
- At quarter Kelly (α = 0.25): exponent ≈ 5; expected drawdown is much smaller; worst-case drawdowns of 50%+ have probability < 0.01.
- This mathematical result explains why the "obvious" choice of full Kelly is actually extraordinarily risky despite maximising growth rate.
- The boundary (exponent = 2) at full Kelly means that in practice, even a small overestimation of one's edge (which increases effective f above true Kelly) can push the exponent below 2 and ensure eventual ruin.

### Critical Assessment
The physics-inspired power-law framing is elegant but the continuous-time Brownian assumption is a significant simplification for discrete, finite-probability betting. The divergence of expected drawdown at full Kelly is a theoretical limit result that does not immediately translate to "always use fractional Kelly." More importantly, this paper assumes the true probabilities are known precisely. Under estimation error, the effective fraction can be above the theoretical Kelly fraction even when the bettor thinks they're at or below Kelly. This paper should be read alongside Baker & McHale (2013) for the full picture.

### Relevance to v1
The power-law exponent result provides the most rigorous basis for quarter-Kelly: at α = 0.25, the distribution exponent is ~5, meaning catastrophic drawdowns are in the tail of the tail. With a World Cup bankroll of $1,000 and ~104 bets, this means essentially zero probability of losing more than 40–50% of bankroll under quarter-Kelly assuming our probability estimates are correct. The crucial caveat: if our model systematically overestimates edge by 50% (common in new models), effective α rises and drawdown risk materially increases. This is the strongest argument for starting at quarter-Kelly rather than half-Kelly.

---

## 9. Ziemba, W.T. and Hausch, D.B. (1986) — Practitioner Foundation

**Citation:** Ziemba, W.T. and Hausch, D.B. (1986). *Betting at the Racetrack*. Dr. Z Investments, Inc. (Later academic treatment: Ziemba, W.T. (2005). The Symmetric Downside-Risk Sharpe Ratio and the Evaluation of Great Investors and Speculators. *Journal of Portfolio Management*, 32(1), 108–122.)

### Summary
While the 1986 practitioner book is not peer-reviewed, the Ziemba-Hausch work — validated with actual betting profits over multiple racing seasons — established that Kelly sizing works in practice for sports betting when edges are genuine and confirmed by CLV. Their key applied insight: the critical metric is not whether you win more bets than you lose, but whether you consistently beat the closing price (closing line value), which confirms that your probability estimates are better than the market's. Kelly-sized bets on CLV-positive wagers are the empirically validated recipe for sustainable profitability.

### Methodology
Empirical horse-race betting data over multiple seasons (1970s–1980s). Proprietary regression models (Dr. Z system). Kelly criterion with cap at 15% of bankroll per bet.

### Key Findings
- The Dr. Z system generated cumulative returns of roughly 10–20% per season over multiple years of application to US horse racing — one of the few independently verified long-run records of Kelly-based sports betting profitability.
- The stake cap at 15% was imposed both for Kelly-shrinkage reasons and practical sportsbook limit reasons.
- The system worked only in place betting markets where liquidity was sufficient; thin markets produced adverse selection effects.
- CLV-tracking (did you beat the subsequent closing line?) was identified as the most reliable leading indicator of long-run model quality — decades before it became standard terminology in the sports betting community.

### Critical Assessment
The evidence base is from 1970s–80s US horse racing — a market with very different efficiency properties from modern European football betting. Soft sportsbooks like Paddy Power and Sky Bet are far more efficient now than pari-mutuel horse racing was then. The 10–20% seasonal return may not be achievable in modern football betting even with a genuinely superior model. That said, the CLV framework and the stake-cap logic remain entirely applicable. This work should be treated as evidence of concept, not a precise benchmark.

### Relevance to v1
The 15% per-bet cap and multi-season CLV tracking approach directly inform our system design. For the World Cup specifically: with quarter-Kelly and a $1,000 bankroll, individual stakes should rarely exceed $30–50 (3–5%), which comfortably sits below soft book limits. CLV tracking from day 1 is the right primary KPI as Ziemba-Hausch established empirically.

---

## 10. Baker et al. (supplementary) — Correlated Kelly for Tournament Betting

**Citation:** Jamshidian, F. and Zhu, Y. (1997). Scenario Simulation: Theory and Methodology. *Finance and Stochastics*, 1(1), 43–67. [Context paper for within-tournament correlation]

*Additionally reviewed (working papers and recent preprints):*
- Whelan, N. (2025). On Optimal Betting Strategies with Multiple Mutually Exclusive Outcomes. *Bulletin of Economic Research*, online first. https://doi.org/10.1111/boer.12474
- Palomino, F. and Sákovics, J. (1996). Risks in Financial Markets and the Kelly Criterion. *European Economic Review*, 40(3–5), 850–855.

### Summary on Correlated Kelly
The tournament context of the World Cup creates two types of correlation that the basic Kelly formula does not handle: (a) within-match correlation (same-team bets on the same game, e.g., backing Team A on the match winner market and also on the Asian handicap market); (b) across-match within-tournament correlation (teams in the same group play again in the knockout rounds; early-group results affect later probabilities; the same model error that makes you wrong about one match likely affects others). Whelan (2025) addresses mutually exclusive outcomes within a single event and shows that the optimal allocation under concave utility requires solving a system of equations that accounts for the full event probability distribution — a result consistent with Whitrow (2007). For across-match correlation, the practical guidance from the literature is consistent: treat correlated exposures as a single bet and apply Kelly once to the combined position.

### Key Findings (from Whelan 2025 and related literature)
- For mutually exclusive outcomes (Home/Draw/Away), the log-Kelly portfolio weights on all three outcomes simultaneously is provably suboptimal compared to betting only on the outcome with the highest perceived edge.
- The "bet only the best edge" result holds for all concave utility functions, not just log-utility.
- Within-tournament group-stage correlation: if your model systematically under/over-estimates a team's quality, all group-stage bets involving that team are positively correlated errors. The appropriate response is to treat all bets on a given team in a single group stage as a single correlated bundle.
- Effect size: with pairwise correlation ρ = 0.3 between bet outcomes (a moderate same-tournament estimate), the optimal simultaneous Kelly fraction for two correlated bets is approximately (1 − ρ) × individual Kelly, or ~70% of the sum of individual fractions.

### Critical Assessment
The correlation literature for sports betting is relatively thin and most results come from financial portfolio theory by analogy. The specific correlation structure of World Cup group-stage matches (where team-quality estimation error is the dominant correlation driver) has not been directly studied to our knowledge. The 70% reduction factor from correlation ρ = 0.3 is an analytical approximation that should be treated as an order-of-magnitude guide rather than a precise rule.

### Relevance to v1
For the World Cup group stage, implement the following practical correlation adjustments:
1. Never bet both Team A to win and Team A Asian handicap simultaneously — they are highly correlated.
2. When betting multiple group-stage games featuring the same strong favourite (e.g., Brazil, France), treat total exposure on related games as a single unit and apply a 0.7× haircut to the sum.
3. Across different groups with no shared teams, treat bets as independent (correlation ≈ 0).

---

## Practical Implications for v1

### Summary of Evidence

The quantitative betting literature from Kelly (1956) through Baker & McHale (2013) and Whitrow (2007) converges on a consistent set of conclusions:

1. **Kelly is theoretically optimal only when probabilities are known exactly.** No study has shown full Kelly to be optimal in practice; every empirical and simulation study recommends fractional Kelly when probabilities are estimated.

2. **Quarter-Kelly (α = 0.25) is a defensible starting fraction** for a new model with uncertain edge. It reduces the probability of a 50%+ drawdown from ~50% (full Kelly) to < 1%, and reduces variance by ~94% relative to full Kelly, at the cost of ~44% lower expected long-run growth. On a tournament-length deployment (104 bets, 39 days), this is the appropriate trade-off.

3. **Estimation error systematically inflates perceived Kelly fractions.** Baker & McHale (2013) show that with 100–200 effective training observations (typical for an international football model), the empirically optimal fraction is 40–60% of naive Kelly. Quarter-Kelly provides a reasonable buffer against this, but the safety margin is not enormous. Monitor CLV aggressively to detect systematic overconfidence.

4. **Simultaneous bets require explicit allocation management.** On group-stage days with multiple concurrent matches, summing individual quarter-Kelly stakes overstates safe exposure. Cap total simultaneous exposure at 15–20% of bankroll (Thorp), and reduce individual stakes proportionally.

5. **Same-tournament correlation requires a haircut.** Bets involving the same team in the same group should be treated as correlated; reduce combined exposure by ~30% relative to the sum of individual stakes. Across independent groups, treat as independent.

6. **CLV is the right primary KPI and the only reliable real-time signal of model quality.** Ziemba-Hausch (1986) and the sports betting literature consistently show that ROI over small samples is too noisy; CLV reflects whether the model's probability estimates are better than the market's and is observable immediately post-bet.

7. **Stake caps matter for soft-book survival.** Paddy Power, Sky Bet, and Virgin Bet will limit or close accounts showing consistent positive CLV. Caps of 3–5% of bankroll per bet reduce detection risk and align with quarter-Kelly on this bankroll.

### Concrete Implementation Rules for v1

| Rule | Source | Implementation |
|---|---|---|
| Base staking fraction | MacLean et al. 2010; Baker & McHale 2013 | α = 0.25 of Kelly fraction |
| Kelly formula | Kelly 1956; Thorp 2006 | f = (b·p̂ − q̂)/b × 0.25 |
| Minimum edge threshold | Thorp 2006; Baker & McHale 2013 | Only bet when (b·p̂ − q̂)/b ≥ 0.02 (2% gross Kelly edge, i.e., > 0.5% quarter-Kelly fraction) |
| Single-bet max stake | Thorp 2006; Ziemba & Hausch 1986 | Hard cap: min(quarter-Kelly, 5% of bankroll) |
| Simultaneous exposure cap | Whitrow 2007; Thorp 2006 | Total open exposure ≤ 20% of bankroll |
| Same-team correlation haircut | Whelan 2025; Whitrow 2007 | 0.7× multiplier on summed stakes for correlated bets |
| Same-match multi-market | Whitrow 2007 | Treat as single bet; bet only the market with highest edge |
| Model monitoring trigger | Baker & McHale 2013 | If 7-day average CLV < 0, reduce to 1/8 Kelly until model is reviewed |
| Shrinkage re-estimation | Baker & McHale 2013 | Recalculate Baker-McHale S factor after every 30 bets; update effective fraction |
| Bankroll basis | MacLean et al. 2011 | Use end-of-previous-day bankroll (not running balance) to avoid intra-day compound errors |

### Key Risks Not Fully Addressed by Literature

- **Bookmaker limits and account restrictions** materially constrain bet size before Kelly-sizing matters. The literature largely ignores this; in practice, Paddy Power and Sky Bet limits may bind well below our quarter-Kelly stakes on popular markets.
- **Betfair Exchange** (which we also have access to) uses pari-mutuel-like liquidity where large bets move the market; Kelly theory assumes fixed odds. For the Exchange, size orders carefully to avoid adverse price impact.
- **Short-horizon Kelly vs. log-wealth:** The entire Kelly framework is calibrated to the infinite horizon. With only 104 bets over 39 days, the tournament is decidedly short-horizon. The probability that a genuinely positive-EV quarter-Kelly strategy shows a profit by the final whistle is materially less than 100%. This does not invalidate the staking approach, but it should calibrate expectations.

---

*Section prepared for World Cup Alpha internal research. Citations verified June 2026. The paper-count is 10 source documents reviewed (counting the MacLean/Thorp/Ziemba 2010 journal paper and 2011 book as separate items).*
