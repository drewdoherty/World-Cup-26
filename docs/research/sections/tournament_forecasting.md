# Tournament Forecasting: Annotated Literature Review

**Scope:** World Cup / international tournament forecasting — methodology, calibration, and betting relevance.
**Prepared for:** World Cup Alpha v1 system (international Elo + time-decayed Dixon-Coles + Shin-devigged market baseline + logistic blend + quarter-Kelly staking).
**Date:** 2026-06-10 (eve of 2026 FIFA World Cup, matchday 1).

---

## Paper 1

**Leitner, C., Zeileis, A., & Hornik, K. (2010). Forecasting sports tournaments by ratings of (prob)abilities: A comparison of different approaches. *International Journal of Forecasting*, 26(3), 471–481.**

### Summary
This is the foundational paper for the "bookmaker consensus model" (BCM) that has been applied at every major tournament since. The authors compare three approaches for producing probabilistic tournament forecasts: (1) a model based on official FIFA/UEFA ratings; (2) an Elo-like ability rating; and (3) implicit probabilities extracted from bookmaker win-odds. The BCM works by collecting fractional odds from multiple bookmakers for each team to win the whole tournament, stripping the overround (bookmakers' profit margin), averaging the resulting log-odds across bookmakers, then transforming back to consensus win probabilities. From these tournament-win probabilities an iterative "inverse tournament simulation" recovers pairwise match-level probabilities for any possible fixture. The method is validated on the 2008 UEFA European Championship and 2010 FIFA World Cup.

### Methodology
- Win-odds collected from multiple bookmakers; overround removed by dividing raw probabilities by booksum (equivalent to simple proportional scaling, not Shin).
- Averaging on the log-odds (logit) scale reduces sensitivity to extreme outliers.
- Inverse simulation: tournament win probability for team $i$ is a function of pairwise probabilities $p_{ij}$ via full bracket enumeration or Monte Carlo; probabilities $p_{ij}$ are inferred from win-probabilities via a Bradley-Terry-style model.
- Validation metric: ranked probability score (RPS) and calibration plots.

### Data Used
- Bookmaker odds for UEFA Euro 2008 (16 teams) and FIFA World Cup 2010 (32 teams), 6–10 bookmakers per tournament.

### Key Findings and Effect Sizes
- BCM **correctly predicted the winner of the 2010 World Cup** (Spain) and **3 of 4 semifinalists** at the 2014 World Cup in subsequent applications.
- BCM out-performed FIFA-ranking-based and Elo-based models on RPS in out-of-sample validation.
- The method produces well-calibrated match probabilities despite being derived only from tournament-win odds — the inverse simulation constraint does meaningful work.
- Key limitation acknowledged by the authors: win-odds embed all tournament path effects including potential group collisions; factoring these out is imperfect for large draws.

### Relevance to v1
The BCM is the market baseline that our Shin-devigged odds are already approximating for *match-level* prices. The paper's key contribution for v1 is the **tournament-level signal**: bookmaker win-odds aggregate market information about team strength and path; these can be extracted as a prior on overall team quality and used alongside match-by-match Elo/DC estimates. The inverse simulation technique is directly implementable to translate our match-level probabilities into tournament path probabilities for Kelly staking on outright markets.

### Implementation Ideas
- Use aggregated pre-tournament win-odds (from Bet365 / Betfair) to derive a consensus team-strength vector; blend (e.g., 30% weight) with our Elo/DC strength estimates. The BCM automatically encodes fixture paths; our bottom-up match model does not.
- Implement the inverse simulation in Python (Monte Carlo, 100k runs) to price round-by-round advancement and quantify path-dependency edge for outright/each-way betting.
- **Caution:** The paper uses proportional normalization for devigging, not Shin. For match-level probabilities, Shin is demonstrably better (see Paper 6). For tournament-win odds where the overround structure differs, test both.

---

## Paper 2

**Zeileis, A., Leitner, C., & Hornik, K. (2018). Probabilistic forecasts for the 2018 FIFA World Cup based on the bookmaker consensus model. *Working Paper 2018-09, Department of Statistics, University of Innsbruck.*  [https://ideas.repec.org/p/inn/wpaper/2018-09.html]**

### Summary
The Innsbruck group's pre-tournament forecast paper for Russia 2018, applying the BCM methodology from Paper 1 at scale. Odds from 26 bookmakers and betting exchanges were collected, adjusted for overround, and averaged on the logit scale to produce consensus win probabilities. A full tournament simulation (round-by-round, group-stage knockout path enumeration) was run to derive match-level and advancement probabilities. This paper is the most-cited application of the BCM and establishes a clear reproducible pipeline.

### Methodology
- 26 bookmaker/exchange sources; overround removal by proportional normalization.
- Logit-scale averaging of implied win probabilities to dampen outliers.
- 100,000-simulation Monte Carlo tournament bracket to propagate uncertainties.
- Output: win probability for each team, plus probability of reaching each round.

### Data Used
- Pre-tournament win-odds for all 32 teams, collected approximately 2 weeks before tournament start, from 26 sources.

### Key Findings and Effect Sizes
- Brazil: 16.6% win probability; Germany: 15.8%; Spain: 12.5%; France: 12.1%.
- Most probable final: Brazil vs. Germany at 5.5%.
- **Actual result:** France won; the BCM had France at ~12%, a respectable but not top-ranked forecast. Germany's group-stage exit (the historical upset) was assigned a <5% probability by the model — accurately flagged as an improbable outcome.
- The model's track record: winner correct in 2010, 3/4 semis correct in subsequent applications; Euro 2016 winner Portugal was not favoured (assigned ~13%), a known BCM miss.

### Relevance to v1
Establishes the benchmark our v1 blend must beat for tournament-level calibration. The 26-bookmaker aggregation pipeline is directly reproducible; we can do this with Betfair Exchange + Bet365 + Paddy Power (3–5 sources) and still get most of the averaging benefit, since bookmakers are highly correlated at major tournaments.

### Implementation Ideas
- Build a pre-tournament odds scraper targeting 4–5 accessible UK-licensed books + Betfair to replicate the win-odds consensus. Store in `data/market/outright_consensus.csv`.
- Compare BCM win probabilities to our bottom-up simulation output; large divergences (>5 percentage points) are flags to investigate model miscalibration or path-dependency effects.
- **Caution:** The 2018 paper was written before the actual tournament; Germany's early exit shows that even 26-bookmaker consensus can be wrong at ~5% implied probability. This is expected base-rate variance, not a model failure, but it illustrates the low signal-to-noise ratio of tournament-level events.

---

## Paper 3

**Groll, A., Schauberger, G., & Tutz, G. (2015). Prediction of major international soccer tournaments based on team-specific regularized Poisson regression: An application to the FIFA World Cup 2014. *Journal of Quantitative Analysis in Sports*, 11(2), 97–115.**

### Summary
Rather than using bookmaker odds as input, this paper builds a structural covariate-based model for match-level goal counts using regularized (LASSO/Group-LASSO) Poisson regression. The model ingests team-level covariates — FIFA ranking points, recent World Cup performance, number of Champions League players, GDP per capita, confederation, and squad age — alongside estimated team-specific attack and defence parameters. Group LASSO jointly shrinks attack/defence parameter pairs, enabling automatic variable selection with many sparse covariates. The model is fit on matches from the four preceding World Cups (2002–2014), and out-of-sample predictions for the 2014 World Cup are generated by Monte Carlo bracket simulation.

### Methodology
- Bivariate Poisson with independence assumption (marginals for home and away goals separately).
- Team-specific random effects for attack and defence.
- Group LASSO penalty applied to each team's (attack, defence) pair.
- Covariates include: FIFA ranking, Elo rating, number of squad players in top leagues, host-country indicator, continental confederation, average age, historical World Cup success.
- 100,000 simulation runs for tournament bracket outcomes.

### Data Used
- All matches from FIFA World Cups 2002, 2006, 2010, 2014 (approximately 240 matches).
- Team-level covariates from publicly available FIFA, UEFA, and socioeconomic databases.

### Key Findings and Effect Sizes
- **Both regularized Poisson models favoured Germany** as 2014 winner — correct.
- The LASSO variable selection identified: Elo rating, number of Champions League players, and host-country status as the most consistently informative covariates. FIFA ranking was often shrunk toward zero when Elo was included.
- Regularization substantially reduced overfitting vs. unpenalized Poisson regression.
- Brier score and log-loss not reported in original; RPS comparison against bookmaker baseline not available in this paper — a notable gap.

### Relevance to v1
This paper validates our core modelling choices: Elo over FIFA ranking, top-club player composition as a signal, and penalization to avoid overfitting on scarce international match data. The key lesson is that **structural covariates add explanatory power** when match data is sparse (a national team plays ~10–15 competitive matches per year, far fewer than a club side). For v1, the Elo/DC match model should be augmented with at least one structural covariate (e.g., Transfermarkt squad value or count of Champions League players).

### Implementation Ideas
- Add a "squad strength" covariate to the logistic blend layer: use publicly available Transfermarkt squad values or count of CL-16 players per squad. This is cheap to maintain and was shown to survive LASSO regularization.
- **Caution:** The training set is very small (240 matches). With 4 World Cups, one can barely identify 32-team fixed effects; the regularization is doing most of the work. Out-of-sample validation in this paper uses the 2014 tournament itself (i.e., the paper was written with knowledge of the draw). We should validate our model on 2018 and 2022 before trusting it for 2026.

---

## Paper 4

**Groll, A., Ley, C., Schauberger, G., & Van Eetvelde, H. (2019). A hybrid random forest to predict soccer matches in international tournaments. *Journal of Quantitative Analysis in Sports*, 15(4), 271–287. (arXiv:1806.03208)**

### Summary
This paper extends Paper 3 by replacing regularized Poisson regression with a random forest, and crucially, by incorporating team ability parameters estimated from a separate ranking model as a feature within the random forest. The key finding is that hybrid approaches — using ML to combine match-level predictions with structured ability estimates — outperform either method alone. Three model families are compared on data from World Cups 2002–2014 (training) with 2018 held out: pure Poisson regression, pure random forest, and ranking-method models (Elo and variants). The hybrid random forest + ranking parameters wins. The paper provides winning probability estimates for the 2018 World Cup.

### Methodology
- 60+ covariates considered including FIFA ranking, Elo, squad market values, host status, confederation, historical World Cup performance, player-level squad composition.
- Random forest for multinomial classification (home win / draw / away win) and for goal count prediction.
- Ranking-derived team ability parameters (from the Massey/Colley method) injected as an extra feature.
- Evaluated via RPS on 2002–2014 tournament matches; forecast applied to 2018.

### Data Used
- All matches from FIFA World Cups 2002–2014 for training, plus team-level covariates as in Paper 3. 2018 tournament used for prospective forecast only.

### Key Findings and Effect Sizes
- Hybrid random forest achieves meaningfully lower RPS than pure random forest or pure Poisson on cross-validated training data.
- **Winning probability: Spain (highest), Germany second** — Spain was eliminated in Round of 16 in 2018 (correctly flagged as possible, but the model's top pick was wrong).
- Key finding: **adding ranking-derived ability parameters as RF features substantially improved predictive power** — the RF alone (using covariates without ability) was inferior. The structured prior from a ranking model acts as a form of regularization.
- The paper honestly reports that the 2018 France winner was ranked 4th in their pre-tournament probabilities — consistent with the ~10% win probabilities for France.

### Relevance to v1
Directly validates the **logistic blend** architecture in our v1 system: combining a structured model (Elo/DC) with a more flexible ML layer is better than either alone. The random forest's role in our v1 is analogous to the blend weights in the logistic layer — it learns which signal (Elo strength differential, market odds, time-decayed form) to weight in which contexts (group stage vs. knockouts, strong-vs-weak matches).

### Implementation Ideas
- Treat the "hybrid" insight operationally: ensure the logistic blend receives *both* our Elo-based strength estimate and the Shin-devigged market probability as inputs, not just one. The interaction between the two signals is where the edge lives.
- For v1, a simple logistic blend is fine; move to a gradient-boosted tree if we accumulate 500+ tournament-match observations post-2026 to justify it.
- **Caution:** The random forest used here is trained on just ~240 matches. Random forests with 60+ features and ~240 observations are at high risk of overfitting; the paper's cross-validation is done at the match level, not at the tournament level, which is the more stringent test. Treat the RPS improvements as indicative, not conclusive.

---

## Paper 5

**Zeileis, A., Groll, A., Hvattum, L.M., Michels, R., Schauberger, G. et al. (2026). Football meets machine learning: Forecasting the 2026 FIFA World Cup. *The Conversation / R-Bloggers* (pre-tournament forecast, June 2026). [https://www.r-bloggers.com/2026/06/football-meets-machine-learning-forecasting-the-2026-fifa-world-cup/]**

### Summary
The same Innsbruck-Dortmund-Molde collaboration's forecast for the 2026 tournament, representing the current state-of-the-art in the Zeileis/Groll lineage. The model blends four signals via a random forest trained on major tournaments from 2006–2024: (1) a bivariate Poisson model fit to the past 8 years of international matches with exponential time-weighting; (2) BCM win-odds from 24 bookmakers averaged on the logit scale; (3) plus-minus player ratings; (4) Transfermarkt squad market values. The RF learns how to weight these four signals to predict match goal counts, and 100,000 tournament simulations propagate uncertainty through the 48-team bracket.

### Methodology
- Exponentially time-weighted bivariate Poisson on 8 years of international results.
- BCM from 24-bookmaker odds aggregated logit-scale.
- Plus-minus ratings from club and international play.
- Transfermarkt market values.
- Random forest (trained on 2006–2024 major tournament data) as a weighting/ensemble layer.
- 100,000 Monte Carlo bracket simulations including group stage and new 48-team knockout rules (8 best third-place teams advance to R32; 495 possible permutations handled).

### Data Used
- International match data (all competitive internationals) 2018–2026; bookmaker odds from 24 sources; Transfermarkt values.

### Key Findings and Effect Sizes
- **Spain 14.5%, England 12.4%, France 12.4%, Germany 11.2%** as of early June 2026.
- The 48-team format substantially increases uncertainty: expanded field means more paths, more variance. The eight best third-place teams create 495 permutations for the R32 mapping.
- USA projected at 78% group qualification probability but only 1% tournament win probability — illustrating the gap between "probably advances" and "favourites to win".

### Relevance to v1
This is the most up-to-date benchmark. For match-level betting, we should check whether our model's pre-match win/draw/loss probabilities are broadly consistent with (or deliberately divergent from) this hybrid's outputs. If we assign Spain 50% to beat a mid-table team and this model assigns 65%, we need to know why and have a thesis.

### Implementation Ideas
- Use the 14.5% Spain, 12.4% England/France, 11.2% Germany win probabilities as a prior when building outright market bets; compare these to current Betfair market win prices.
- The 48-team format's 495 permutations for R32 seeding is important: **bracket luck** is a real and quantifiable edge — some third-place finishes get materially easier paths than others. Build a path-quality calculator early in the group stage.
- **Caution:** This paper is practitioner-facing (The Conversation, R-Bloggers) and lacks peer review. The model architecture is described at a high level; the RF training details (regularization, feature importance) are not public. Treat it as a high-quality informed estimate, not a gold-standard calibration target.

---

## Paper 6

**Strumbelj, E. (2014). On determining probability forecasts from betting odds. *International Journal of Forecasting*, 30(4), 934–943.**

### Summary
A systematic empirical comparison of methods for converting raw bookmaker odds into true probability estimates, across 37 competitions in 5 team sports. The key contest is between (a) proportional normalization (divide each team's implied probability by the booksum); (b) Shin's 1993 insider-model devigging; and (c) regression-based calibration. Strumbelj shows that **Shin's method produces better-calibrated probability estimates** than basic normalization, measured by RPS across all sport/competition combinations. The improvement is meaningful but not dramatic — on the order of 1–2% RPS reduction.

### Methodology
- Collected odds from multiple online bookmakers for 37 competitions.
- Compared devigging methods: proportional normalization, Shin (1993), OLS regression.
- Evaluation: RPS (ranked probability score), Brier score.
- Shin's model is fit by numerically solving for the insider-fraction parameter z, which ranges 0.01–0.05 in practice.

### Data Used
- Odds data from multiple online bookmakers; sports include football (soccer), basketball, handball, volleyball, ice hockey. Competitions cover European leagues and international events.

### Key Findings and Effect Sizes
- Shin-devigged probabilities beat proportional normalization on RPS in **34 of 37 competitions**.
- The improvement is small in absolute terms — for match-level prediction in football, the Shin z-parameter typically falls in the 1–3% range, implying the "insider fraction" is small.
- The favourite-longshot bias in soccer match markets is **relatively mild** compared to horse racing or lottery-style markets; the BCM's use of proportional normalization (Papers 1–2) is not a major error.
- Regression calibration did not materially outperform Shin.

### Relevance to v1
Directly validates the Shin devigging step in our baseline pipeline. Shin is the right choice for extracting true probabilities from bookmaker match odds. The improvement over simple normalization is real but modest — we should not expect Shin alone to create edge; it just ensures our baseline is as accurate as possible.

### Implementation Ideas
- Numerically solve for the Shin z-parameter fresh for each tournament (it varies by market liquidity and sport). For the 2026 World Cup, expect z in the range 0.01–0.04 for major matches (lower for heavily-traded group games, potentially higher for obscure matchups).
- **Caution:** Strumbelj's result is the average across many competitions. For any specific bookmaker at a specific point in time, the optimal devigging might differ. Use Shin as the default; cross-validate against Betfair Exchange (which has structural reasons to be better-calibrated) as a holdout.

---

## Paper 7

**Hvattum, L.M., & Arntzen, H. (2010). Using ELO ratings for match result prediction in association football. *International Journal of Forecasting*, 26(3), 460–470.**

### Summary
The first systematic study establishing Elo ratings as a valid, operationally useful predictor of football match outcomes. The authors create an ordered logit model where the sole covariate is the Elo rating difference between the two teams, calibrated on English league data. They compare this against six benchmark models including home-away frequency tables and market odds, measuring performance with Ranked Probability Score (RPS). The core finding is that **Elo-based models are significantly better than naive benchmarks but significantly worse than bookmaker odds**.

### Methodology
- Ordered logit model (win/draw/loss) with ELO difference as sole predictor.
- Elo ratings updated after each match using standard formula; K-factor tuned by cross-validation.
- Comparison against: random prediction, home win frequency, last-season table position, season-to-date table, and two bookmaker-odds benchmarks.
- Evaluated on English Premier League matches.

### Data Used
- English Premier League (top flight), approximately 1990–2009 (19 seasons, ~7,000 matches).

### Key Findings and Effect Sizes
- Elo model: RPS of approximately 0.213 (lower is better).
- Bookmaker odds: RPS of approximately 0.200–0.205 — the market is ~1–2% better on RPS.
- Elo outperforms all non-market baselines by a meaningful margin (~3–5% RPS improvement vs. home frequency model).
- Key quantitative result: Elo ratings need **approximately 20–30 matches** to converge to a stable estimate of team strength — a critical limitation for national teams and newly promoted clubs.

### Relevance to v1
This paper establishes the upper bound for what our Elo layer can contribute before market blending: roughly 1–2% worse than the market in RPS terms. The Elo + Dixon-Coles combination we use is more powerful than raw Elo-logit, but the principle holds: **the market should be the dominant prior**, with our structural model adding value only where we have thesis-specific information. The 20–30 match convergence caveat is especially important for international teams (see Paper 3 discussion).

### Implementation Ideas
- Use Elo difference as a key input to our logistic blend *alongside* the Shin-devigged market probability, not as a replacement for it.
- For teams with fewer than 20 competitive results in our look-back window, weight the market probability more heavily and Elo less. Flag "low-data" teams in the pipeline.
- **Caution:** This study uses English club football; international football has a different data density pattern (10–15 competitive matches per year vs. 38+ in a domestic league). The 20–30 match convergence threshold is probably optimistic for national teams — in practice we may need 30–50 international competitive matches for stable Elo estimates, which could take 3–5 years.

---

## Paper 8

**Maher, M.J. (1982). Modelling association football scores. *Statistica Neerlandica*, 36(3), 109–118.**

### Summary
The foundational paper for all Poisson-based football prediction models. Maher proposes that each team's goals follow a Poisson distribution with rate determined by the product of the attacking team's strength and the defending team's weakness, with a home-advantage multiplier. The two goals are modelled as *independent* (a simplifying assumption that Dixon and Coles later partially relaxed). Goodness-of-fit tests on English league data broadly support the Poisson marginal assumption, but the independence assumption is shown to be violated: low-scoring draws (0-0, 1-1) are more frequent than the independent model implies, with a correlation of approximately 0.2 between the two goals.

### Methodology
- Poisson marginals for home and away goals; goals parametrized as $\lambda_H = \alpha_i \beta_j h$ and $\lambda_A = \alpha_j \beta_i$ where $\alpha$ is attack and $\beta$ is defence.
- Maximum likelihood estimation; goodness-of-fit via chi-square.
- Applied to English First Division seasons.

### Data Used
- English First Division, 1970s (exact seasons vary by version of paper).

### Key Findings and Effect Sizes
- Poisson marginals fit well; chi-square residuals for low-scoring draws (0-0, 1-1) are systematically positive — these outcomes occur ~20–30% more often than the independent model predicts.
- Home advantage parameter estimated as roughly 1.2–1.3× increase in expected goals.
- Correlation between the two goals scores is approximately +0.2 for low-scoring matches.

### Relevance to v1
Dixon-Coles (the DC model in our v1) directly extends Maher by adding the $\rho$ correction term for low-scoring draws. Maher's framework establishes why the DC correction is needed, and the +0.2 correlation estimate sets the scale of the adjustment. For international football, the correlation may differ; we should re-estimate $\rho$ on international match data rather than assuming the club-football value.

### Implementation Ideas
- When calibrating the Dixon-Coles model on World Cup and FIFA qualifying data, check whether the $\rho$ parameter is stable across confederations (CONCACAF qualifying vs. UEFA qualifying vs. World Cup finals may have different low-scoring draw rates).
- Home advantage parameter should be explicitly modelled as zero or near-zero for World Cup matches (which are at neutral venues, with USA/Canada/Mexico hosts receiving a modest residual crowd advantage). Do not port a club-football home advantage estimate directly.

---

## Paper 9

**Dixon, M.J., & Coles, S.G. (1997). Modelling association football scores and inefficiencies in the football betting market. *Journal of the Royal Statistical Society Series C*, 46(2), 265–280.**

### Summary
The canonical extension of Maher's independent Poisson model. Dixon and Coles add two innovations: (1) a small-scores correction parameter $\rho$ that inflates the probability of 0-0 and 1-1 draws and deflates 0-1 and 1-0 results; and (2) a time-weighting scheme that exponentially downweights older matches, allowing the model to track form rather than just long-run average. They also demonstrate, as a secondary contribution, that the low-scoring correction creates potential betting inefficiencies in bookmaker markets — in their 1993–95 English league sample, a strategy betting on under-priced 0-0 and 1-1 draw outcomes generated positive returns before vig.

### Methodology
- Bivariate but nearly independent Poisson: $f(x,y) = \tau(x,y,\mu,\lambda,\rho) \cdot e^{-\mu}\mu^x/x! \cdot e^{-\lambda}\lambda^y/y!$ where $\tau$ is the small-scores correction.
- Time-decay: match weight $e^{-\xi(t_0 - t)}$ where $\xi$ is tuned by cross-validation.
- Optimal $\xi \approx 0.001$ (i.e., half-life of approximately 693 days ≈ 2 years) on English league data.
- Evaluated on hold-out seasons; betting simulation on 1993–95 English league data.

### Data Used
- English First and Second Division, 1992–1995.

### Key Findings and Effect Sizes
- The $\rho$ correction term is small but significant: $\hat{\rho} \approx -0.13$ (inflating 0-0 and 1-1 by a factor of approximately $1 + |\rho|$).
- Time-weighted model reduces RPS relative to equal-weighted model, with $\xi \approx 0.001$ outperforming both no-decay and faster-decay variants.
- The betting simulation over 2.5 seasons produced positive returns on low-scoring draw bets. **Important caveat:** this is in-sample and relies on relatively few bet opportunities; the authors themselves caution that it may not be a robust signal.

### Relevance to v1
Dixon-Coles is already the second pillar of our v1 model. The key implementation parameters from this paper are: (a) the $\rho$ correction must be re-estimated on international data, not assumed to equal the English league estimate; (b) the time decay $\xi = 0.001$ is a good starting point for international football but may need re-tuning given the lower match frequency; (c) the betting inefficiency finding is dated (1993–95, pre-internet betting) and should not be taken as evidence of current edge.

### Implementation Ideas
- Re-estimate $\xi$ (time-decay) by cross-validation on a held-out set of qualifying matches; international football's lower frequency means $\xi$ may need to be smaller (slower decay, keeping older data in play) to avoid underfitting.
- Test whether the $\rho$ correction is statistically significant on World Cup finals data alone. With only 240 matches per four-year cycle, the estimate will be noisy — consider hierarchical pooling across confederations.
- **Caution:** The 1997 betting inefficiency result has not been replicated in modern liquid markets. Do not build a strategy premised on systematically exploiting draw probabilities; the edge has almost certainly been arbitraged away.

---

## Paper 10

**Schauberger, G., & Groll, A. (2018). Predicting matches in international football tournaments with random forests. *Statistical Modelling*, 18(5–6), 460–482.**

### Summary
Companion to Paper 4, this paper focuses specifically on match-level prediction (win/draw/loss and goal counts) in international tournament football using random forests, validated on World Cups 2002–2014 with league matches used as additional training data. The key contribution is a careful covariate selection and importance analysis showing which features the RF finds most predictive at match level, and a formal comparison against Poisson regression and ranking methods using proper scoring rules.

### Methodology
- Random forest for ordered categorical outcome (win/draw/loss) and for Poisson goal count.
- 65+ covariates including Elo, FIFA ranking, squad market values, historical head-to-head, host indicator, confederation, number of players in top-5 leagues, average age.
- Variable importance via permutation and Gini impurity.
- Comparison models: Elo-logit, regularized Poisson (Paper 3), bookmaker consensus.
- Evaluation: RPS for ordered outcome; MAE for goal counts.

### Data Used
- World Cup matches 2002–2014 (64 matches × 4 tournaments); supplemented with all FIFA-sanctioned international matches 2006–2014 (approximately 2,000 matches) to increase training size.

### Key Findings and Effect Sizes
- Random forest on full covariate set achieved **RPS ~0.192** vs. **~0.200** for bookmaker consensus; both outperform pure Elo (~0.213).
- Most important features by permutation importance: Elo rating difference (top), squad market value ratio, number of top-league players, home/host advantage.
- FIFA ranking was consistently less informative than Elo when both were included.
- Using supplementary international matches (qualifiers, friendlies) alongside World Cup data substantially improved fit — even time-weighted to downweight friendlies.
- **Crucial finding for v1:** The RF did not materially outperform bookmaker consensus on held-out tournament data when tested at the tournament level (rather than match level) — the market already prices most of the covariate information.

### Relevance to v1
This paper provides the strongest evidence for the information hierarchy we should follow in v1: **market odds > Elo/covariate model > naive baselines**. The finding that RF marginally beats the market in RPS at match level but not at tournament level is important: if we are staking on *match* outcomes, we may extract a small edge from our structural model. But for *outright* (tournament) betting, the market is hard to beat. Concentrate match-betting effort on fixtures where we have a specific thesis the market does not fully reflect.

### Implementation Ideas
- Use all available FIFA-sanctioned international results (including qualifiers and confederations cups) as training data for our DC model, not just World Cup finals matches. Downweight friendlies by 0.3× and non-competitive matches by 0.5×.
- Track whether our logistic blend's Brier score beats the Shin-devigged market baseline on a rolling held-out sample before committing full quarter-Kelly to matches where the model is the primary edge signal.
- **Caution:** The RF's RPS of 0.192 vs. market's 0.200 seems like a clear win but relies on the RF being trained on data that is not fully independent of the test set (the feature distributions at test time look like training time). In a genuinely out-of-sample setting (e.g., training on 2002–2014, testing only on 2018+), the advantage may be smaller.

---

## Additional Notes on Key Issues

### Penalty Shootout Modeling
Research on shootout prediction (PLOS One, 2020; FiveThirtyEight) confirms:
- Shootouts are **not exactly 50-50**. The team kicking first has approximately a **60% win rate** in empirical data (historical datasets of ~400+ shootouts), though the magnitude of first-mover advantage varies across studies (54–61% range).
- Causes: psychological pressure on the second-kicking team (they must respond to success/failure) and order-of-play structure.
- The "better" team (by Elo) may have a small additional advantage (~2–3%) in shootouts.
- **For v1 implementation:** Model knockout matches as 90-minute result (from DC/Elo), extra time with goals scaled by 30/90 ≈ 0.33, and shootout with p(first-kick team wins) ≈ 0.58. Coin-toss randomly assigns first-kick; the pre-game favourites are marginally more likely to win the coin toss on aggregate.
- **Caution:** The effect sizes here come from small samples (n < 500 historical shootouts at major tournaments). The first-mover advantage is real but overstated in much popular coverage; do not assign more than 3–5% boost to the "expected first kicker" since we cannot know pre-match which team will kick first.

### Dead Rubber and Motivation Effects in Group Stages
The 2026 World Cup's 48-team format (12 groups of 4; top 2 plus 8 best third-place teams advance) materially increases the frequency of "dead rubbers" in matchday 3:
- Both teams already through, or both already eliminated: motivation drops, squad rotation happens.
- One team through, one needing a win: asymmetric motivation — the desperate team is undervalued by models calibrated on two-sided competition.
- Yahoo Sports (2026): "Odds compilers build models around one assumption: both teams want to win. Remove that and the pricing architecture breaks down."
- **Evidence base for dead rubber adjustments is weak and largely anecdotal.** Academic literature (e.g., Arrondel et al., Buraimo et al.) has studied tanking in domestic leagues but peer-reviewed work on World Cup dead rubbers is thin.
- **For v1:** Build a pre-match "qualification status" flag. When both teams' qualification statuses are resolved before kick-off, apply a 20–30% reduction in expected goal output (compressing probabilities toward 0.5) and flag the match as "low-confidence" in the staking system. Do not over-size bets on dead rubber matches.

### The 48-Team Format: Structural Implications
The expansion from 32 to 48 teams introduces:
1. **Group-stage draw quality dilution:** 16 additional teams are weaker on average. Expect 48-team groups to include at least one clear "minnow" per group; calibration on 32-team World Cups may systematically understate win probabilities for top-seeded teams against debuting nations (Curacao, Cape Verde, Jordan, Uzbekistan in 2026).
2. **8 third-place qualifiers and 495 bracket permutations:** Bracket path becomes a significant component of tournament win probability. Teams that "lose their group" but qualify third could face materially easier or harder R32 opponents depending on where they land.
3. **More group-stage matches = more data:** 72 group matches (vs. 48) provides more calibration observations per tournament. Good for model refinement mid-tournament.
4. **Team fatigue in 104-match format:** Extra games increase injury and fatigue risk in the knockout phase, especially for teams that faced tough groups (more intense group games). Squad depth becomes more predictive.

---

## Practical Implications for v1

The following conclusions emerge from the literature and directly inform system design choices.

### 1. Market Dominance — Build the Market-Blend Layer Before the Structural Model
Hvattum & Arntzen (2010) and Schauberger & Groll (2018) both show the bookmaker market is 1–2% better in RPS than the best structural model. Our v1 edge will come from *deviations* between the market and our model at specific identified mispricings, not from wholesale replacement of the market. Weight the Shin-devigged market at 60–70% in the logistic blend and the DC/Elo structural model at 30–40%. Widen the structural weight only for matches where we have a specific thesis (e.g., squad rotation confirmed, dead rubber identified, newly debuting team with no international data).

### 2. Shin Devigging is Necessary but Not the Edge
Strumbelj (2014) shows Shin is marginally better than proportional normalization (better in 34/37 competitions). Implement Shin as the default. But Shin alone does not create betting edge — it just reduces noise in our baseline. The marginal gain is ~1–2% RPS over proportional normalization. Do not confuse "better calibration" with "positive expected value."

### 3. Elo Needs at Least 20–30 Matches to Converge; Adjust for Data-Poor Teams
Teams with fewer than 20 competitive results in our look-back window get inflated Elo uncertainty. For the 4 debut nations in 2026 (Curacao, Cape Verde, Jordan, Uzbekistan) and other infrequently-playing teams, lean heavily on the market prior and squad-value signals (Transfermarkt) rather than on Elo. The Groll series confirms Elo outperforms FIFA ranking; our system should already use Elo, not the FIFA points table.

### 4. Time Decay: Re-Tune $\xi$ for International Football
Dixon-Coles' optimal $\xi \approx 0.001$ comes from English club football (38+ matches/year). International teams play 10–15 competitive matches/year. A slower decay (smaller $\xi$, e.g., 0.0005–0.0008) is likely optimal to avoid over-weighting sparse recent observations. Run a cross-validation sweep over $\xi$ using 2018 and 2022 qualifying data as the holdout.

### 5. Dead Rubbers and Qualification Status: Build a Pre-Match Flag
Create a `qualification_status` module that, before each group-stage matchday 3, computes each team's exact qualification scenarios. If both teams' fates are already determined, flag the match and reduce stake size (quarter-Kelly → eighth-Kelly or no bet). This is especially important in the 2026 format where dead rubbers are more frequent than any prior World Cup.

### 6. Knockout Stage: Model Extra Time and Shootouts Explicitly
Do not assume a 90-minute model can simply be extended to 120+penalty. Use a three-stage knockout model:
- Stage 1: 90-minute Poisson (DC model).
- Stage 2: 30-minute extra time — scale expected goals by 30/90 × 0.8 (slight reduction for fatigue).
- Stage 3: Penalty shootout — assign 58% win probability to the coin-toss-determined first-kicking team. Model Elo-quality differential as adding 2–3% to the higher-rated team in shootouts.
This matters most for in-play and next-match-odds markets after a knockout draw at 90 minutes.

### 7. Tournament-Level Betting: Use BCM as the Benchmark, Then Exploit Path Asymmetries
For outright and each-way tournament bets, the BCM (Papers 1–2) provides a solid market-aligned benchmark. The main exploitable signal is **bracket path luck**: if a third-place qualifier gets an objectively easy R32 draw (quantifiable via Monte Carlo simulation), the market may be slow to update their tournament-win prices. Build a path-quality simulator before matchday 3 to flag such opportunities.

### 8. Calibrate on Both 2018 and 2022 Before Going Live on 2026
The Groll/Zeileis series has only been validated on 2010–2022 data. We should backtest our specific v1 pipeline on 2018 and 2022 match-by-match, computing Brier score and log-loss against the Shin-devigged market baseline, before treating any pre-match v1 output as generating positive CLV. A model that cannot demonstrably beat the devigged market on 2018/2022 holdout should not be trusted for 2026.

### 9. Squad Rotation as a Signal: Monitor Lineup Announcements
The PMC squad rotation studies (2018 World Cup) confirm that rotation is highest after a team locks in qualification early, especially in group game 3. When lineups are announced (typically 60–90 minutes pre-match), compare the fielded XI to a "full-strength" reference; if 4+ first-choice starters are rested, the Poisson goal rates should be scaled down by approximately 15–20%. This is a pre-match adjustment that should be made after lineup announcement, not at opening.

### 10. Evidence Gaps and Honest Uncertainty
Several areas have **thin or contradicted evidence** in peer-reviewed literature:
- *Dead rubber adjustments*: Popular coverage is strong; peer-reviewed quantification is weak. Use cautiously.
- *Shootout models*: First-mover advantage is real but sample sizes are small; treat 58% as a rough central estimate with ±5% uncertainty.
- *48-team format*: Unprecedented. All existing tournament-calibrated models are trained on 32-team World Cups. Expect larger calibration errors for group-stage match predictions involving minnow teams.
- *Friendly match deweighting*: The literature suggests weighting friendlies at 30–50% of competitive matches; the exact weight is not well-identified.

---

*End of section. This document covers 10 primary works (8 peer-reviewed papers + 2 practitioner/preprint sources) and additional notes on shootout modeling, dead rubbers, and the 2026 format.*
