# World Cup Alpha — Merged Annotated Bibliography

**Compiled:** 2026-06-10 (eve of 2026 FIFA World Cup) by the research synthesizer.
**Inputs:** 8 literature sections (`docs/research/sections/`) + 7 recon reports (`docs/recon/`).
**Method:** Merged and de-duplicated across sections; organised by theme; full citations kept. Each entry carries a strength tag and a one-line "what it actually supports" note. Weakly-sourced or non-primary entries are marked **[WEAK]** or **[PRACTITIONER]** and must not be cited as evidence for a model decision without independent confirmation.

**Strength tags**
- **[PRIMARY]** peer-reviewed, primary source, directly on point.
- **[PRIMARY-ADJ]** peer-reviewed but off-domain (club not international, or different sport) — directional only.
- **[PREPRINT]** arXiv / working paper, not yet refereed.
- **[PRACTITIONER]** blog / book / vendor study — useful but not peer-reviewed; treat numbers sceptically.
- **[WEAK]** in-sample profitability claim, unreplicated, or otherwise load-bearing-but-thin. Do not build on it.

De-duplication note: Dixon-Coles (1997), Shin (1991-93), Strumbelj (2014), Hvattum & Arntzen (2010), Rue-Salvesen (2000), Ley et al. (2019), Groll et al. (2015), Maher (1982), Ridall et al. (2025), Zeileis/Leitner (2010/2018), Franck et al. (2010) each appeared in 2-4 sections. Merged to a single entry; section overlaps recorded.

---

## Theme A — Rating systems (team strength priors)

**Hvattum, L. M., & Arntzen, H. (2010).** Using ELO ratings for match result prediction in association football. *International Journal of Forecasting*, 26(3), 460-470. **[PRIMARY]** *(appeared in: ratings, practitioner_clv, tournament_forecasting)*
Elo difference → ordered logit beats naive baselines but **loses to bookmaker odds**; Elo+market blend beats either alone. ~14,927 English club matches. RPS Elo ≈0.213 vs market ≈0.200-0.205. Establishes the ceiling: Elo is a structural prior, not a market-beater. Caveat: club data, pre-modern-market.

**Lasek, J., Szlávik, Z., & Bhulai, S. (2013).** The predictive power of ranking systems in association football. *International Journal of Applied Pattern Recognition*, 1(1), 27-46. **[PRIMARY]**
The one genuinely **national-team** ranking comparison: World Football Elo Ratings beat all other systems incl. official FIFA ranking and all Elo K-variants. More responsive (higher K) ratings did better. Caveat: data ends 2012, pre-dates FIFA's 2018 switch to an Elo-based ranking — so the "FIFA ranking is poor" claim is now stale.

**World Football Elo Ratings (eloratings.net).** Methodology spec (Runyan, since 1997). **[PRACTITIONER]**
Football-specific Elo: K=60 WC finals / 40 qualifiers / 20 friendlies; +100 home offset (~14pp); goal-margin multiplier G. Battle-tested public prior for all 48 teams. Caveat: heuristic, never likelihood-optimised; +100 home offset is wrong for neutral 2026 venues. Recon (`historical_data.md`) confirms **no API/CSV export** — reconstruct Elo internally from the martj42 results CSV instead of scraping.

**Szczecinski, L. (2026).** New insights into Elo algorithm for practitioners and statisticians. arXiv:2604.03840v1. **[PREPRINT]**
Decouple the ranking model from the prediction model; the 400-divisor is wrong for unconverged teams. Diagnostic: ~80% of national teams have NOT converged after 6 years of FIFA's Elo system → national-team Elo is statistically provisional. Actionable: fit the Elo→probability scale empirically by logistic regression rather than using 400. Caveat: not refereed; correction needs noise-variance estimate that is itself hard to get in small samples.

**Constantinou, A. C., & Fenton, N. E. (2013).** Determining the level of ability of football teams by dynamic ratings (pi-ratings). *Journal of Quantitative Analysis in Sports*, 9(1), 37-50. **[PRIMARY model] / [WEAK profitability]**
Pi-ratings (goal-margin update, separate home/away) slightly beat Elo on RPS (~0.199 vs 0.204). **The profitability claim is the weak part:** learning-rate tuning and profit measurement shared the same five EPL seasons — no held-out window. Do not treat as evidence of a live edge.

**Shelopugin, A., & Sirotkin, A. (2023).** Ratings of European and South American Football Leagues Based on Glicko-2 with Modifications. arXiv:2310.11459. **[PREPRINT]**
Modified Glicko-2 beats vanilla Glicko-2 and gradient-boosting (log-loss 0.5832 vs 0.5896 LightGBM) on ~366k **club** matches. Relevance: Glicko-2's rating-deviation (RD) inflates during inactivity — useful uncertainty signal for sparsely-playing national teams. Caveat: Glicko-2 needs 5-10 games/period; national windows give 2-4, so its volatility term barely moves. No national-team validation.

**Arntzen, H., & Hvattum, L. M. (2021).** Predicting match outcomes using team ratings and player ratings. *Statistical Modelling*, 21(5), 449-470. **[PRIMARY-ADJ]**
Team Elo + player plus-minus combined beats either alone. Motivates a starting-XI / squad-value covariate at line-up time. Caveat: Norwegian club data; national-team plus-minus is much harder to estimate.

**Glickman, M. E. (1995, 1999).** The Glicko system / Parameter estimation in large dynamic paired comparison experiments. *JRSS-C*, 48(3). **[PRIMARY]** *(supplementary; not fully reviewed)*
Source for RD inflation during inactivity. Use only as the conceptual basis for an uncertainty multiplier on stakes.

---

## Theme B — Goal-based score models (the Dixon-Coles core)

**Maher, M. J. (1982).** Modelling association football scores. *Statistica Neerlandica*, 36(3), 109-118. **[PRIMARY]** *(appeared in: goal_models, tournament_forecasting)*
Foundational independent-Poisson attack/defence parameterisation; +0.2 goal correlation, home multiplier ≈1.2-1.3. Foundation only — values are 1970s English club, do not port.

**Dixon, M. J., & Coles, S. G. (1997).** Modelling association football scores and inefficiencies in the football betting market. *JRSS-C*, 46(2), 265-280. **[PRIMARY model] / [WEAK profitability]** *(appeared in: goal_models, market_efficiency, practitioner_clv, tournament_forecasting — 4 sections)*
**The v1 core model.** Low-score correction ρ (≈-0.13) + exponential time-decay ξ (≈0.0065/half-week ≈0.0019/day ≈370-day half-life on EPL). The ρ correction and time-decay are robust and well-validated. **The betting-inefficiency result is in-sample, single-season, 1990s single-bookmaker** — the authors themselves caution against it. Do not treat as live-edge evidence.

**Karlis, D., & Ntzoufras (2003).** Analysis of sports data by using bivariate Poisson models. *JRSS-D*, 52(3), 381-393. **[PRIMARY]**
Principled covariance (λ₃) + diagonal inflation instead of the DC τ-patch. Marginal RPS gain over DC (~0.0001). Diagnostic only — not worth the EM cost for v1.

**Boshnakov, Kharrat & McHale (2017).** A bivariate Weibull count model. *IJF*, 33(2), 458-466. **[PRIMARY model] / [WEAK profitability]**
Weibull-count allows under/over-dispersion; significant negative copula dependence (κ≈-0.46). RPS indistinguishable from DC at 4 d.p. The "positive Kelly returns" claim has **no CIs, no bet counts, no breakdown** — unreproducible. v2 at most.

**Rue, H., & Salvesen, O. (2000).** Prediction and retrospective analysis of soccer matches. *JRSS-D*, 49(3), 399-418. **[PRIMARY model] / [WEAK profitability]** *(appeared in: goal_models, bayesian_ensembles)*
First dynamic-Bayesian football model (random-walk attack/defence, MCMC). Conceptually what time-decay approximates. Positive-return claim is single 1997-98 season, pre-exchange — discount entirely.

**Groll, Schauberger & Tutz (2015).** Regularized Poisson for the FIFA WC 2014. *JQAS*, 11(2), 97-115. **[PRIMARY]** *(appeared in: goal_models, tournament_forecasting)*
The key **sparse-international-data** paper. LASSO keeps Elo + squad market value + WC appearances; FIFA ranking shrinks out when Elo present. Argues for longer lookback + stronger regularisation. Caveat: ~240 WC matches; 2014 fitted with knowledge of 2014 (the 2019 follow-up corrects this).

**penaltyblog (2025).** "Football Prediction Models: Which Ones Work the Best?" pena.lt/y. **[PRACTITIONER]**
Side-by-side RPS on Dutch Eredivisie: DC 0.1914, Weibull 0.1914, Poisson 0.1915, NB/BVP 0.1916 — **all within 4th-decimal noise.** Model family barely matters vs data quality. Suggests ~4-season lookback, ξ≈0.001/day (~693-day half-life). Caveat: blog, no CIs, domestic league.

**Ridall, Titman & Pettitt (2025).** Bayesian state-space models for EPL. *JRSS-C*, 74(3), 717-. **[PRIMARY]** *(appeared in: goal_models, bayesian_ensembles — most important recent benchmark)*
SSM (online conjugate-Gamma + VB, bivariate negative-binomial) cuts cumulative RPS to 17.55 vs DC-equivalent 22.06-22.22 over 14 EPL seasons — a **~21% gain, large unlike penaltyblog's ties.** Between-season forgetting ω_b≈0.76 (teams reset ~24%/season); within-season ω≈0.988 (slow). Captured COVID home-advantage drop. **No statistical model beat the bookmaker.** Calibration: home-win under-predicted ~3%, draws over-predicted ~8%. v2 target; ω_b→pre-cycle discount for v1. Caveat: EPL dense data; gain likely smaller on sparse international.

**Kovalchik, S., & Vaci, N. (2022).** Double Poisson for Euro 2020. *PLOS ONE*, 17(5), e0268511. **[PRIMARY]**
Plain independent Poisson (no ρ, no decay) **won the RSS Euro 2020 prediction competition** — strongest external international validation. Critical data-prep lesson: excluding San Marino cut Cyprus's attack estimate 36% → **exclude/down-weight minnows.** Caveat: single tournament; one win can be luck.

**Ley, Van de Wiele & Van Eetvelde (2019).** Ranking soccer teams: a comparison of ML approaches. *Statistical Modelling*, 19(1), 55-77. **[PRIMARY]** *(appeared in: ratings, goal_models — counted once)*
The strongest empirical decay recommendation for **international men's football: half-life ≈ 3 years (1095 days), ξ≈0.000634/day.** Bivariate/independent Poisson beat Bradley-Terry; time-depreciation + match-importance weights (WC=4, continental=3, qualifier=2.5, friendly=1) improve all models. Caveat: omits the ρ correction; 2007-onward data, possibly disrupted post-COVID.

**Schauberger & Groll (2018).** Predicting matches in international tournaments with random forests. *Statistical Modelling*, 18(5-6), 460-482. **[PRIMARY]** *(tournament_forecasting)*
RF on 65+ covariates RPS ~0.192 vs market ~0.200 vs pure Elo ~0.213 at **match** level — but **RF did NOT beat the market at tournament level.** Most important features: Elo diff (top), squad-value ratio, top-league player count. Adding qualifiers/friendlies as training data (time-weighted) helped. Caveat: train/test feature distributions overlap; out-of-sample gain likely smaller.

**Groll, Ley, Schauberger & Van Eetvelde (2019).** A hybrid random forest for international tournaments. *JQAS*, 15(4), 271-287. **[PRIMARY]** *(appeared in: bayesian_ensembles, tournament_forecasting)*
Hybrid (RF + ranking-derived ability as a feature) beats either alone — direct support for the blend architecture. Caveat: ~240-256 training matches with 60+ features → overfitting risk; no out-of-sample Brier reported for 2018.

**Zeileis, Groll, Hvattum, Michels, Schauberger et al. (2026).** Forecasting the 2026 FIFA World Cup. The Conversation / R-Bloggers. **[PRACTITIONER]** *(tournament_forecasting)*
The current state-of-the-art lineage forecast: bivariate Poisson + 24-book BCM + plus-minus + Transfermarkt, RF-ensembled, 100k sims over the 48-team bracket. Early-June: Spain 14.5%, England 12.4%, France 12.4%, Germany 11.2%; USA 78% to advance / 1% to win. **Use as a sanity-check prior, not a calibration target** (not peer-reviewed, architecture only sketched).

---

## Theme C — Bayesian / ensemble / calibration / scoring rules

**Baio, G., & Blangiardo, M. (2010).** Bayesian hierarchical model for football results. *Journal of Applied Statistics*, 37(2), 253-264. **[PRIMARY]**
Canonical hierarchical Poisson; partial pooling fixes sparse-team estimates but over-shrinks extremes (mixture-prior fix). Directly supports confederation-level shrinkage priors for data-poor 2026 teams. Caveat: 1990s Serie A, no betting test, no Brier reported.

**Egidi, Pauli & Torelli (2018).** Combining historical data and bookmakers' odds in modelling football scores. *Statistical Modelling*, 18(5-6), 436-459. **[PRIMARY]**
**The most direct academic antecedent of the v1 blend.** Convex combination of historical-rate and market-rate (Skellam inversion); posterior mix weight α≈0.4-0.6 (roughly equal). Combined beats either alone on log-loss. Caveat: dense European-league data; α may not transfer to sparse international; no CLV/profit test.

**Zeileis, Leitner & Hornik (2018) / Leitner, Zeileis & Hornik (2010).** Bookmaker consensus model (BCM). Univ. Innsbruck WP 2018-09 / *IJF*, 26(3), 471-481. **[PRIMARY/PRACTITIONER]** *(appeared in: bayesian_ensembles, tournament_forecasting)*
De-vig 26-book tournament-winner odds, average on log-odds scale, Bradley-Terry + inverse simulation → match probabilities. Correct in 2010, 3/4 semis in 2014. **By construction it IS the closing line — cannot generate CLV; use as the outright benchmark, not a signal.** Used proportional (not Shin) de-vigging.

**Niculescu-Mizil & Caruana (2005).** Predicting good probabilities with supervised learning. *ICML 2005*, 625-632. **[PRIMARY-ADJ]**
Platt scaling vs isotonic. **Isotonic overfits below ~500 calibration examples** — decisive for v1 (only ~192 historical WC group matches). Use Platt, not isotonic. Caveat: binary ML datasets, not football.

**Kull, Silva Filho & Flach (2017).** Beta calibration. *AISTATS* PMLR 54, 623-631 / *EJS*, 11(2). **[PRIMARY-ADJ]**
Beta calibration includes the identity map (won't degrade an already-calibrated input like a de-vigged market). 3-8% Brier gain over Platt on skewed scores. Caveat: gain small; 3 params risk overfitting ~64-192 WC matches — Platt is the safer v1 default.

**Gneiting & Raftery (2007).** Strictly proper scoring rules. *JASA*, 102(477), 359-378. **[PRIMARY]**
Theory: Brier and log-loss proper; log-loss strictly proper and local. **Accuracy (fraction correct) is improper — never use as primary metric.**

**Wheatcroft (2021).** The case against the Ranked Probability Score. *JQAS*, 17(4), 273-284. **[PRIMARY]**
Log-loss (ignorance) beats RPS at distinguishing forecaster quality; RPS's distance-sensitivity is unjustified for football. Use **log-loss as primary calibration metric**, RPS only for literature comparability. Caveat: known-truth simulations; on 64-match samples the practical difference is marginal.

---

## Theme D — Market efficiency, de-vigging, favourite-longshot bias

**Shin, H. S. (1991, 1992, 1993).** Insider-trading model & FLB. *Economic Journal* 101/102/103. **[PRIMARY]** *(appeared in: market_efficiency, practitioner_clv, prediction_markets)*
Derives Shin de-vigging: longshots carry higher embedded margin; solve for z (≈2-4% racing) to recover fair probabilities. **The v1 de-vig method.** Caveat: 3-outcome football needs numerical z; insider interpretation now contested (see Whelan).

**Strumbelj (2014).** On determining probability forecasts from betting odds. *IJF*, 30(4), 934-943. **[PRIMARY]** *(appeared in: market_efficiency, practitioner_clv, tournament_forecasting — 3 sections)*
Head-to-head de-vig test (63,861 matches / 37 competitions): **Shin best, power≈Shin, multiplicative worst.** Gain ~1.5pp Brier where a clear favourite exists; small (~0.001-0.002) otherwise. Shin beat proportional in 34/37 competitions. Confirms Shin choice AND that **bookmaker choice matters as much as method** (Pinnacle/sharp > recreational). Caveat: pre-2014, mostly domestic; opening/mid odds not closing.

**Kuypers (2000).** Information and efficiency in a fixed-odds betting market. *Applied Economics*, 32(11), 1353-1363. **[PRIMARY]**
Profit-maximising bookmakers shade odds to exploit bettor bias (not balanced books); "fan tax" on popular teams. Caveat: single book, early-1990s; weak-form-efficiency finding contradicted by later richer-model studies.

**Forrest, Goddard & Simmons (2005).** Odds-setters as forecasters. *IJF*, 21(3), 551-564. **[PRIMARY]**
Bookmakers out-forecast statistical models on Brier and **the gap widened as competition grew** → use market as baseline, not rival. Brier benchmarks: home ~0.231-0.238, draw ~0.195-0.200, away ~0.185-0.198. Caveat: used multiplicative de-vig (Shin would widen the market's edge further); domestic.

**Goddard & Asimakopoulos (2004).** Forecasting football results and fixed-odds efficiency. *Journal of Forecasting*, 23(1), 51-66. **[PRIMARY]**
**Match-significance ("nothing to play for") is the clearest persistent semi-strong inefficiency.** Direct analogue: 2026 final-round dead rubbers / rotation. Caveat: domestic-season incentives; club rotation ≠ national-team rotation; likely more efficiently priced now.

**Franck, Verbeek & Nüesch (2010).** Bookmakers vs a betting exchange. *IJF*, 26(3), 448-459. **[PRIMARY]** *(appeared in: market_efficiency, prediction_markets)*
Betfair closing prices beat every bookmaker AND the consensus on Brier; 19.2% of Big-Five matches had book/exchange arb; bookmakers shade popular teams. **Supports Betfair Exchange close as the CLV benchmark.** Caveat: 2008 data; 19.2% arb almost certainly compressed since.

**Angelini & De Angelis (2019).** Efficiency of online football betting markets. *IJF*, 35(2), 712-721. **[PRIMARY]**
33,060 matches / 41 books / 11 leagues: efficiency is league-dependent; best-available-odds across books ≈ efficient (8/11 leagues); 3 leagues persistently inefficient. **Supports multi-book line-shopping.** Caveat: WC not studied — international efficiency unvalidated; named profitable strategies are data-mined.

**Whelan, K. (2025).** On estimates of insider trading in sports betting. *The Manchester School*, 93(1) / *Scottish J. Pol. Econ.*, 72(5). **[PRIMARY]** *(appeared in: market_efficiency, practitioner_clv)*
Shin's z is biased upward and does **not** measure insider fraction; FLB arises from heterogeneous beliefs + bookmaker market power. **Keep Shin as a mechanical de-vig tool; stop interpreting z as an insider/sharp signal.** Caveat: recent, not widely replicated; operational impact = nil.

**Levitt (2004).** Why are gambling markets organised so differently? *Economic Journal*, 114(495), 223-246. **[PRIMARY]** *(prediction_markets)*
Bookmakers take positions and exploit bias rather than balance books; majority of games had 66%+ of money on one side, and that side loses >50%. Reinforces Shin over naive de-vig; expect popular WC teams (England/Brazil/Argentina) shaded short. Caveat: NFL contest, US, 20+ years old.

**Wilkens, S. (2026).** Can simple models predict football — and beat the odds? Bundesliga. *Journal of Sports Analytics*. **[PRIMARY model] / [WEAK profitability]**
Most-recent published claim of beating modern markets: xG-Skellam + isotonic, **~10% ROI (avg odds) / ~15% (best odds) over 11 Bundesliga seasons, almost entirely on home wins; away wins loss-making.** **Treat the ROI as very high and not transferable** to a once-every-4-years 48-team tournament; xG signal ≠ v1's Elo/DC signal; possible publication/optimisation effects. Actionable lesson only: bookmakers' home-win calibration is the likeliest blind spot; treat away backs more sceptically.

---

## Theme E — Prediction markets (Polymarket / Kalshi / exchanges)

**Wolfers & Zitzewitz (2004, 2006).** Prediction Markets / Interpreting prices as probabilities. *JEP* 18(2) / NBER WP 12200. **[PRIMARY]**
Market prices are well-calibrated and ~equal to mean beliefs (within 1-3pp under realistic risk aversion). Justifies de-vigged exchange close as the probability baseline. Caveat: 22-year-old empirics from thin IEM/TradeSports markets.

**Tetlock (2008).** Liquidity and prediction market efficiency. SSRN 929916. **[PRIMARY]**
Counterintuitive: more liquidity did **not** improve TradeSports calibration; naive limit-order providers get picked off near news. **Do not treat a liquid price as exact truth**, especially in the final minutes. Caveat: thin defunct market; effect likely milder on modern Betfair.

**Spann & Skiera (2009).** Prediction markets vs odds vs tipsters. *Journal of Forecasting*, 28(1), 55-72. **[PRIMARY]**
Markets ≈ odds (54.3% vs 53.7% accuracy), both >> tipsters; **consensus of all three rises to 57.1%.** Supports a consensus/agreement gate before larger stakes. Caveat: 1999-2002 Bundesliga, 25% takeout.

**Angelini, De Angelis & Singleton (2022).** Informational efficiency in in-play prediction markets. *IJF*, 38(1), 282-299. **[PRIMARY]**
Modern Betfair football shows a **reverse FLB (favourite bias)** — the market under-prices longshots and is slow to update toward trailing teams. Implication: do not over-shrink the model toward the exchange in the 15-30% band. Caveat: event-study (first goal), in-play; not universally replicated.

**Snowberg & Wolfers (2010).** Explaining the FLB: risk-love or misperceptions? *JPE*, 118(4), 723-746. **[PRIMARY]**
FLB is driven by probability misperception (prospect-theory weighting), so it persists even among sophisticated participants. Most actionable in the 15-30% band. Caveat: horse racing.

**Borghesi (2009) / O'Connor & Zhou (2008).** NBA / NFL TradeSports efficiency. *J. Prediction Markets*, 3(2) / 2(1). **[PRIMARY-ADJ]**
Sport- and band-specific biases (O'Connor: ~10pp favourite-cover underpricing; TradeSports takeout 2.2% vs 4.55% books). Directional caution that mispricing differs by probability band. Caveat: thin defunct exchange, point-spread NFL/NBA — weak transfer to WC.

**Simon, J. R. (2024).** Inefficient forecasts at the sportsbook: line movement. *Management Science*, 70(12), 8583-8611. **[PRIMARY-ADJ]** *(practitioner_clv)*
Lines overreact (negative autocorrelation) across MLB/NFL/NBA/NHL; contrarian fades were ex-ante profitable. **US sports only — football efficiency untested; apply cautiously.** Supports monitoring (not blindly fading) sharp Pinnacle moves around line-up news.

---

## Theme F — Staking / bankroll / Kelly

**Kelly (1956).** A New Interpretation of Information Rate. *Bell System Tech. J.*, 35(4), 917-926. **[PRIMARY]**
f* = (bp − q)/b maximises long-run log-growth — but only when p is known exactly and horizon is infinite. Both fail here.

**MacLean, Thorp & Ziemba (2010, 2011).** Good and bad properties of (fractional) Kelly / *The Kelly Capital Growth Investment Criterion*. *Quantitative Finance*, 10(7), 681-687 / World Scientific. **[PRIMARY]**
Quarter-Kelly: variance ↓~94%, growth ↓~44% vs full Kelly; P(50% drawdown) ~50% (full) → <0.8% (quarter). The quantitative justification for α=0.25. Caveat: continuous-time, known-probability; real discrete bets have fatter tails.

**Thorp (2006/2011).** The Kelly Criterion in Blackjack, Sports Betting and the Stock Market. **[PRIMARY-ADJ]**
"Bet considerably less than Kelly"; quarter-to-half practical; total simultaneous exposure cap ~20-25%; estimated edges are usually overstated.

**Baker & McHale (2013).** Optimal Betting Under Parameter Uncertainty. *Decision Analysis*, 10(3), 189-199. **[PRIMARY]**
**The decisive estimation-error paper.** Shrinkage S<1 on raw Kelly; with ~100-200 training matches S≈0.4-0.6. So effective fraction = α·S·f* ≈ 0.10-0.20 of full Kelly. Re-estimate S as sample grows. Caveat: tennis; assumes simple logistic, not correlated blend errors.

**Whitrow (2007).** Algorithms for Optimal Allocation of Bets on Many Simultaneous Events. *JRSS-C*, 56(5), 607-623. **[PRIMARY]**
Simultaneous-optimal stakes are always ≤ sequential Kelly. On multi-match days, do NOT sum independent quarter-Kelly stakes. Caveat: assumes known probabilities; no cross-event tournament correlation.

**Grant & Buchen (2012).** A Comparison of Simultaneous Kelly Betting Strategies. *J. Gambling Business & Economics*, 6(2). **[PRIMARY-ADJ]**
Multibet (parlay) Kelly beat single-game Kelly in simulation — **but only if parlays are fairly priced, which soft books' +3-7%/leg overrounds violate.** Model quality dominates strategy choice. Stick to singles.

**Maslov & Zhang (1998/1999).** Probability distribution of drawdowns. *Physica A*, 262(1-2), 232-241. **[PRIMARY]**
Drawdown power-law exponent = 1+2μ/σ²: full Kelly = 2 (mean drawdown diverges), quarter ≈ 5 (catastrophic drawdowns deep in the tail). Strongest argument to start at quarter, not half. Caveat: continuous-time, known-probability.

**Ziemba & Hausch (1986); Ziemba (2005).** *Betting at the Racetrack* / Symmetric Downside-Risk Sharpe. **[PRACTITIONER/PRIMARY]**
Independently-verified Kelly record (~10-20%/season, 1970s-80s US racing) with a 15% per-bet cap; established **CLV as the leading indicator** decades before the term existed. Caveat: pari-mutuel racing, far less efficient than modern football.

**Whelan, N. (2025).** On Optimal Betting Strategies with Multiple Mutually Exclusive Outcomes. *Bulletin of Economic Research*. **[PRIMARY]**
For H/D/A, bet only the single outcome with the highest edge — splitting across outcomes is suboptimal under any concave utility. Supports same-team/same-match correlation haircuts.

---

## Theme G — Practitioner CLV & operations

**Buchdahl, J. (2011, 2017).** *How to Find a Black Cat in a Coal Cellar* / *Squares and Sharps, Suckers and Sharks*. **[PRACTITIONER]**
The CLV evidence base: Pinnacle close r²=0.997 (decile-level, not per-match — common misquote); **~50 bets detect CLV significance vs ~2,500-4,000 bets for ROI significance** → CLV must be the primary KPI on a 104-match tournament. 952-bet sample: 79.4% shortened, avg 3.94%; personal 3.4% actual vs 4.0% projected over ~20k bets. Caveat: not peer-reviewed; "2% CLV → profit" is a heuristic; the 50-bet figure assumes a stable edge.

**Buchdahl, J. (2015/2017).** Using the Wisdom of the Crowd. Football-Data.co.uk. **[PRACTITIONER] / [WEAK]**
Operational template: back books pricing above de-vigged Pinnacle. Reported ~10.38% early ROI **settling to ~5-6% in longer samples** (5.95% over 1,309 bets). Survivorship + account-restriction limited; early-line edge compressing. Treat the 5-6% as an aspirational ceiling, not an expectation.

**statsbet.org / Betformatics (2026).** Betfair vs bet365 closing-odds efficiency. **[PRACTITIONER]** *(practitioner_clv)*
1,635 matches: ECE bet365 1.21% vs Betfair 1.72%; Brier ≈ identical (0.1935 vs 0.1934). Both show the same FLB. PL strong favourites overpriced ~6pp (N=201, small). **Either platform works as a benchmark; favourites need a scepticism check.** Caveat: not peer-reviewed, not replicable.

**Mo_Nbg, beatthebookie.blog (2022).** Scoring functions vs betting profit. **[PRACTITIONER]**
8,877 Big-5 matches, 5 models: **best Brier ≠ most profitable; metric rankings did not match profit rankings;** all models lost vs bet365. **Do not optimise v1 on raw Brier/log-loss alone** — use market-relative metrics + value-filtered simulation. Caveat: blog; hardest possible test (flat stake vs soft book).

**UK Gambling Commission via ukbookmakers.org (2025).** 600k+ accounts restricted. **[PRACTITIONER on UKGC data]** *(practitioner_clv, uk_books recon)*
643,779 accounts (4.3%) restricted; >50% capped to <10% normal stakes; ~52% eventually closed; **only 46.8% of restricted accounts are net-profitable** → restriction is a broad algorithmic net. **Account longevity, not model accuracy, is the binding operational constraint.** Caveat: aggregate; no per-book or timeline detail.

---

## Theme H — Operational recon (data, platforms, settlement)

These are not academic literature; they are load-bearing operational facts that bound what v1 can do. Confidence tags are the recon authors'.

**Odds & fixtures (`odds_apis.md`).** The Odds API key `soccer_fifa_world_cup` covers all 104 matches; UK region includes Betfair Exchange, Paddy Power, Sky Bet, Virgin Bet — **Bet365 absent (no public feed; manual CLV only).** Free tier 500 credits/mo; historical odds paid, WC from 2022-04-03 (no 2018). **Pinnacle is NOT available via The Odds API** (OddsJam carries it but is sales-only). Fixtures: openfootball/worldcup.json (free, 104 matches verified) primary; fixturedownload.com programmatic JSON unreliable.

**Historical data (`historical_data.md`).** martj42/international_results CSV (CC0, ~49k rows, daily updates, **results only — no odds/xG**) is the backbone; **reconstruct Elo internally** (eloratings.net has no API). WC odds backtesting requires scraping OddsPortal (2018/2022) or The Odds API paid (2022 only). International xG sparse — StatsBomb open data (WC 2018/2022) only; **FBref shut down Jan 2026.** Transfermarkt squad values via dcaribou/transfermarkt-datasets.

**Betfair (`betfair.md`).** Live API key = **£499 one-off, ~1-3 business-day manual activation — NOT achievable before matchday 1.** Commission: default 5% (Rewards); must opt into **Basic 2%** (effective July 1). Expert Fee 0% under £25k profit — irrelevant at £1k bankroll. WC liquidity deep (>$8M outright pre-tournament). Match Odds settles **90 min only**; "To Qualify" includes ET/pens. **Bahrain status ambiguous (grey-area ToS); VPN prohibited — verify with support before live bets.**

**UK books (`uk_books.md`).** WC 1X2 overround ~104-107% (Bet365/WH tightest, Sky/Paddy 0.5-1.5% worse); outright 115%+; HT result ~110-112%. Sign-up + 2 Up early-payout + price-boost promos catalogued. **Paddy Power & William Hill most aggressive on restricting winners; Bet365 stake-factoring to 1%.** Mitigations: spread 5-7 accounts, mug bets, avoid round stakes / Skrill-Neteller, migrate sharp volume to Betfair Exchange. Betfair Exchange close = CLV denominator.

**Kalshi (`kalshi.md`).** **CONDITIONAL GO** — Bahrain not restricted, residence-based eligibility, UK citizenship not blocking; binary YES/NO only (no spreads/O-U); maker fees very low; free API. WC outright >$100M volume. **Verify eligibility with support before building.** App US-only (web access).

**Polymarket (`polymarket.md`).** **Bahrain NOT geoblocked (live API confirmed); UK IS blocked.** 3-way moneyline match markets (Draw is a valid outcome), 90-min settlement; sports taker fee 3%·C·p(1−p), makers rebated; UMA oracle (2h challenge, 4-6 day disputes). **Material blocker: May-2026 SDK bug breaks programmatic order placement for ALL new deposit-wallet accounts (Python/TS/Rust) — use a proper EOA wallet.** Group/advancement markets settle full-tie.

**Settlement (`settlement_rules.md`).** All five UK/Betfair platforms + Polymarket settle match result on **90 min + stoppage; ET/pens excluded** → no fake-arb from settlement basis on match result. "To Qualify"/advancement markets include ET/pens everywhere. **Genuine cross-platform comparison surfaces:** (1) group-stage 1X2 UK vs Polymarket (both 90-min 3-way); (2) UK "To Qualify" vs Polymarket advancement (both full-tie). **Virgin Bet is the outlier** (12h abandonment / same-day postponement window). Knockout 1X2 ≠ "to advance" — never compare.

---

## Cut / down-ranked entries (weakly sourced — do NOT cite as evidence)

The following appeared in sections but are explicitly **not** treated as support for any v1 decision:

- **Every in-sample / unvalidated profitability claim:** Dixon-Coles (1997) betting result, Constantinou-Fenton (2013) profit, Rue-Salvesen (2000) ROI, Boshnakov (2017) "positive Kelly returns", Buchdahl Wisdom-of-Crowd 10.38%, Wilkens (2026) 10-15% ROI. Each is in-sample, single-period, pre-modern-market, or missing CIs/bet-counts. Collectively they show *it is hard to beat the market*, not that there is a transferable edge.
- **Zeileis et al. (2026) 2026 WC forecast** and **penaltyblog (2025)**: useful priors/benchmarks, but practitioner/non-refereed — sanity checks only.
- **statsbet.org (2026)**, **beatthebookie (2022)**, **ukbookmakers (2025)**: practitioner; cite for direction, not precise numbers.
- **Jamshidian & Zhu (1997), Palomino & Sákovics (1996):** cited as context in staking.md for correlation/Kelly; not directly reviewed — treat as background only.
- **Shin "insider fraction" interpretation:** kept as a *tool*, killed as a *signal* (Whelan 2025).
- **"Dead rubber" and penalty-shootout adjustments:** flagged by tournament_forecasting itself as anecdotal / small-sample (n<500 shootouts; 58%±5% first-kicker). Use with wide uncertainty, not as a precise input.

---

*End of merged bibliography. ~55 distinct sources across 8 themes; operational recon folded into Theme H. Next review: after the group stage (≈2026-06-27) with live CLV/calibration data.*
