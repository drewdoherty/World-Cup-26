# Phase 3 — Objective Functions for Completing the Remaining Bracket

**Status:** research deliverable (Phase 3). READ-ONLY w.r.t. production. No trade, no `src/` edit.
**Date of record:** 2026-06-30.
**Scope:** a formal/retrospective completion exercise. The contest is CLOSED and no entry exists; this constructs the *objective and method* that a perfect-bracket completion *would* use "as if still open for the remaining games." **No profitability is claimed.** Every probability here is conditional on an untuned forecasting model (`et_skill_weight` not fit) and on UNVERIFIED scoring/tie-break rules. Preserve that uncertainty downstream.

---

## 0. Current state `S0` (the conditioning set)

The group stage is fixed. Four Round-of-32 matches are decided and conditioned on:

| Match | Advanced | Eliminated |
|---|---|---|
| M73 | Canada | Germany |
| M74 | Paraguay | Netherlands |
| M75 | Morocco | Japan |
| M76 | Brazil | South Africa |

**Remaining undecided set** `R` = {12 remaining R32 ties} ∪ {R16: M89–M96} ∪ {QF: M97–M100} ∪ {SF: M101, M102} ∪ {Final: M104}. Match 103 (3rd-place) is **excluded** from the advancement bracket unless the contest grades it (UNVERIFIED).

A completed bracket `B` assigns a picked winner `b_m` to every `m ∈ R`, subject to **feed-consistency**: the team picked at `m` must be one of the two teams picked at `m`'s feeder matches (`KNOCKOUT_FEED`), and at R32 must be a valid participant under `R32_TIES` plus the realized thirds allocation. `Ω ~ P(· | S0)` is a random completed tournament under the simulator's joint law; `W_m(Ω)` is the realized winner of `m`.

**Verified contest structure** (`00_foundation.md` §2): a single **$2,000,000** prize for a **PERFECT** R32→Final bracket; **assume NO partial credit**; **free entry**. Prize-split rule among multiple perfect entries and tie-break rule are **UNVERIFIED** — this is a genuine fork that the analysis carries explicitly.

---

## 1. The three candidate objectives

Payoff is an indicator times a (possibly split) prize `V`:

```
U(B) = V · 1{ b_m = W_m(Ω) for every m ∈ R }.
```

### Objective A — MAP / maximize joint P(perfect) **[BINDING]**

```
A*(B) = P( b_m = W_m(Ω) for all m ∈ R | S0 )
B_A   = argmax_B  P( ∩_{m∈R} {b_m = W_m} | S0 ).
```

For a fixed prize, `E[U(B)] = V · A*(B)`, so the *only* lever you control is the joint hit probability `P(perfect-from-here)`. Maximizing an all-or-nothing (0–1) payoff is, by the Bayes decision rule under 0–1 loss, exactly choosing the **posterior MODE** of the joint distribution over remaining outcomes — a **maximum-a-posteriori (MAP)** bracket completion. The object being moded is a functional of the **posterior-predictive** law over `Ω` conditioned on `S0`.

> *Decision-theory backbone:* under 0–1 ("all-or-nothing") loss the Bayes-optimal action is the posterior mode — **Larsen & Marx, _An Introduction to Mathematical Statistics and Its Applications_, 4e**, Bayesian estimation / decision-theory & loss-function chapter (~Sec 5.8 / Ch. 14 in this edition; *page-exact cite pending — this copy is image-scanned with no text layer; chapter-level cite per corpus manifest entry #1*).
> *Predictive-distribution backbone:* the completion objective is a functional of the posterior-predictive distribution — **Rachev, Hsu, Bagasheva & Fabozzi, _Bayesian Methods in Finance_**, "Prior and Posterior Distributions": Posterior Distributions **p.124**; Predictive (posterior-predictive) Distributions **p.126** (TOC verified).

### Objective B — maximize expected contest SCORE **[INERT here]**

```
ScoreEV(B) = E_Ω[ Σ_{m∈R} g_m · 1{b_m=W_m(Ω)} ] = Σ_{m∈R} g_m · P(b_m = W_m),
```

with `g_m ≥ 0` the points for correctly calling `m` (round-weighted if escalating). By linearity of expectation this **decomposes into a SUM of per-slot terms** — each term being the joint probability that `b_m` both *reaches* `m` under `B`'s earlier picks *and* wins it. Maximizing `ScoreEV` is the right objective **only if scoring is additive/separable across matches** (paid per correct slot). Under a pure perfect-bracket lottery the score function is **multiplicative** (a product of indicators), not additive, so "expected correct slots" is not proportional to — and is generally not maximized by — the bracket that maximizes `P(perfect)`. **Objective B is therefore inert for the verified contest** and must not drive picks.

### Objective C — beat-the-field EV with prize-splitting **[OVERLAY]**

```
EV_C(B) = V · E[ 1{B perfect} / (1+K) | S0 ],
```

where `K` = number of OTHER entries also perfect. This is Objective A with a **dilution** correction; it collapses *to* Objective A if ties are broken by draw / earliest submission rather than equal split. Treated as a second-order overlay in §4.

### Why A binds and B does not

Expected-correct-slots rewards hedging toward high-**marginal**-probability picks even when they are mutually low-**joint**-probability. The lottery rewards the single most-probable **joint** path. Concretely, `argmax_B Σ_m P(b_m correct)` (sum of marginals) can differ from `argmax_B P(∩_m b_m correct)` (the joint), because the latter must respect **reachability dependence** — a team can be the picked QF winner only on paths where it also won its R16 pick. Given "perfect → $2M, assume no consolation," **Objective A is the operative objective.**

---

## 2. Exact formulation of the joint objective

A bracket is a full feasible assignment, so

```
P(perfect) = Σ_ω 1{ω consistent with B} · P(ω | S0)
           = E_Ω[ Π_{m∈R} 1{b_m = W_m(Ω)} ].
```

The **naïve (WRONG) surrogate** is the product of marginal pick-probabilities `Π_m P(b_m=W_m) = Π_m q_m` ("marginal-pick"). This equals `A*` **only under match independence, which is FALSE here** (§3). By the chain rule along the bracket DAG,

```
P(perfect) = Π_m P( b_m = W_m | b_{feeders of m} all correct ),
```

and the conditioning events are non-trivial; the marginal product drops them.

**Monte-Carlo estimator (captures all correlation for free):**

```
Â*(B) = (1/N) Σ_{s=1}^N Π_{m∈R} 1{ b_m = W_m(ω_s) }
      = (# sims in which B is perfectly correct) / N.
```

Each simulated tournament `ω_s` is a single internally-consistent **joint** draw, so this single estimator embeds every reachability constraint and shared-team coupling.

---

## 3. The dependence (correlation) argument

Knockout matches are **not** independent, for two coupled reasons:

1. **Reachability.** A team contests `m` only if it won its feeder matches (`KNOCKOUT_FEED`); the participants of `m` are themselves random, determined by earlier outcomes.
2. **Mutual-exclusion / shared-team coupling.** The same team appears as a candidate at multiple downstream nodes, so the teams filling a QF are deterministic functions of the R16 outcomes — inducing strong dependence among slot-correctness indicators.

**Directional consequence.** Pick team X to win the Final. The marginal `P(X wins Final)` already integrates over all of X's paths. But a *bracket* also pins X's R16/QF/SF opponents-beaten via your earlier picks. The joint event "X wins R16 pick AND QF pick AND … AND Final" is one connected path; its probability is **not** the product of four marginal round-win probabilities, because conditioning on X having reached the QF (by beating *your specific* picked R16 opponent) changes the QF distribution. The marginal product double-counts / omits exactly this conditioning.

> *Chain-rule / multiplication-principle backbone, and why multiplying marginals is invalid without independence:* **Mosteller, _Fifty Challenging Problems in Probability with Solutions_** — conditional-probability set (~p.idx32), multiplication principle (~p.idx53), independence (~p.idx8) (verified text).

**Why the sim-mean captures correlation for free.** In `Â*(B) = (1/N) Σ_s Π_m 1{b_m=W_m(ω_s)}`, within each `ω_s` reachability and shared-team coupling are mechanically enforced by the simulator (a team that loses its R16 simply is not present in that sim's QF). So the indicator is 1 only on fully self-consistent paths, and the empirical mean of the product is — by the plug-in principle — a consistent estimator of `E[product] = P(perfect)`, correlations included. **No covariance terms need to be modeled.**

**The one missing primitive.** The codebase audit (`02_codebase_audit.md`, forecasting-core §1, Gaps + Reuse rec #1–2) flags that `TournamentSimulator._run_knockout` (`src/wca/sim/tournament2026.py`) already computes per-sim match winners but **discards** them, returning only marginal `reach[]`/`win[]`. Marginal `reach[]` **cannot** value a correlated all-or-nothing bracket — that is the product-of-marginals world. Exposing the `(n_sims × match_no→winner)` matrix (`return_paths=True`) is the single change that turns it into an **exact joint scorer** (the joint world).

> *DP composition note:* the forecasting kernel `prob_fn(a, b, knockout=True)` (`src/wca/advancement.py:159` → `tournament2026._play_ko`, with the `et_skill_weight` ET/penalty model) is **pairwise-Markov** — given the two contestants, the outcome law does not depend on history. This is what makes the Bellman/DP recursion exact *conditional on contestants*, and lets Monte-Carlo supply the reach distribution over contestants; the two methods compose cleanly.

---

## 4. Optimization-method comparison

Two layers: a **search** layer (which candidate brackets to consider) and a **scoring** layer (evaluating `A*`). Monte-Carlo is the only sound scoring primitive; the rest are search strategies.

| Method | Optimality for the perfect-bracket lottery (Obj. A) | Complexity | When best |
|---|---|---|---|
| **Marginal/greedy per-tie modal pick** (`q_m`-argmax, independently) | Optimal for **Obj. B** under additive scoring + independence. For **Obj. A** a **heuristic/seed only** — maximizes `Σ q_m`, not the joint `Π`. Provably suboptimal whenever the modal R16 pick is incompatible with the modal QF pick. | `O(#matches)` from one sim pass (`reach[]`/`win[]` already produced). Trivial. | Additive scoring; or the **chalk SEED** for MAP search; quick baseline. **Not** the lottery answer. |
| **MAP-bracket search** (greedy-swap / hill-climb / restarts from chalk seed, scored as mean-over-sims all-correct) | Targets **Obj. A exactly**. No global guarantee from local search, but restarts/beam reliably find the posterior-mode bracket; each candidate's value is computed exactly up to MC noise. | Each eval `O(N · #matches)`; swap search visits `O(#matches · branching)` candidates, all scored against the **same cached path matrix**. Cheap. | **Recommended workhorse** when the joint path matrix is available. Handles dependence automatically. |
| **Dynamic programming over the bracket tree** (Bellman from R32 up; `V(node)=max_{picked winner} P(win|contestants)·continuation`) | **Exactly optimal** for max-joint *if* the model is conditionally pair-Markov (it is). Exact for the conditional-on-bracket-shape problem. | Complication: which teams reach each node is itself random (495 thirds combos + combinatorial reachability). Pure DP enumerates reachable contestant sets per node → blows up unless paired with sim-estimated reach probs. Tractable per fixed bracket-shape branch / late rounds. | Provably-optimal late-round / post-R32 subtrees with a manageable reachable-team set. Best combined with MC for reach distributions. |
| **Beam search** (keep top-W partial brackets by partial joint prob, expand round by round) | Approximate for Obj. A; recovers MAP for wide enough `W`. Strictly better than greedy, strictly cheaper than full DP. Tunable, no global guarantee. | `O(W · branching · N)` with the cached matrix. Linear in beam width. | Reachable-team state space too large for exact DP but greedy too myopic — the practical middle ground for a full R32→Final completion. |
| **Integer / combinatorial optimization** (binary program: max log-joint or linear surrogate s.t. feed constraints) | Exact for a **linear** objective (`Σ log q_m`, or expected-slots) under feed constraints — but the **true** max-joint objective is **not separable/linear**, so IP optimizes a surrogate unless you encode the full joint as one binary-feasibility row per sim path (a **scenario IP**: large but exact, max-coverage form). | Scenario IP: `O(N)` constraints — heavy but solvable; linearized marginal version is tiny. Feed-consistency is clean logical constraints over `R32_TIES`/`KNOCKOUT_FEED`. | **Constrained portfolio-of-brackets** design (multiple entries that must be diversified; a chalk-overlap budget for Obj. C). Overkill for a single unconstrained MAP bracket. |
| **Pure Monte-Carlo argmax** (enumerate candidates, score each as all-correct sim fraction, take max) | **Unbiased estimator of Obj. A per candidate**, exact as `N→∞`. As a *search* it is only as good as the candidate set (cannot enumerate all feasible brackets). | `O(N)` per candidate against the cached matrix. **Rare-event variance:** `P(perfect)` may be `~1e-3` to `1e-6`; standard error `≈ sqrt(p(1−p)/N)`, so naïve MC needs `N ≫ 1/p` or importance sampling. | **Always the SCORING engine** (the only method capturing full correlation directly). Pair with greedy/beam/DP as the search layer. |

> *Bellman / principle of optimality and the value-function recursion:* **Sydsaeter, Hammond, Seierstad & Strom, _Further Mathematics for Economic Analysis_** — Dynamic Programming / Optimal Control chapters (*page-exact cite pending — image-scanned, no text layer; chapter-level cite per manifest entry #15*).
> *Combinatorial enumeration of bracket paths and DP-on-a-tree recursions:* **Zhou, _A Practical Guide to Quantitative Finance Interviews_** — probability/combinatorics & DP problem sets (*page-exact cite pending — image-scanned; chapter-level cite per manifest entry #10*).

**Recommended composition.** Seed with the per-slot chalk bracket (`q_m`-argmax from marginal `win[]`/`reach[]`); refine with **greedy-swap + beam search** scored against the cached joint path matrix; optionally **verify late-round subtrees with exact Bellman DP** (kernel is pair-Markov). Use **MC purely as the scorer**, with large `N` or importance sampling because `P(perfect)` is a rare event.

---

## 5. Public-field model (chalk, crowding, differentiation, EV-per-perfect)

> *Source caveat:* "chalk" is **inferred** from prediction-market winner prices + seeding, **not** from observed contest-entry data — the exact public bracket-entry distribution is **UNVERIFIED**. Treat field-overlap fractions qualitatively. PM snapshot 2026-06-30: France 27.3%, Argentina 19.6%, Spain 11.3%, England 10.2%, Brazil 7.0%, Portugal 6.1%, Morocco 4.1% (defirate.com; Polymarket corroboration).

### 5.1 The chalk (modal) bracket

The crowd's modal completion takes the market/seed favorite in each live R32 tie, then runs the biggest names deep.

**Per-tie chalk R32 picks** (decided R32 already fixed: Canada, Paraguay, Morocco, Brazil): France over Sweden; England over DR Congo; United States over Bosnia; Belgium over Senegal; Portugal over Croatia (name-brand pick despite model only **63%**); Spain over Austria; Switzerland over Algeria; Argentina over Cape Verde; Colombia over Ghana.

**Three near-coin-flip ties** — where public chalk most likely diverges from the model:
- **Ivory Coast vs Norway** — model favors **Norway ~57%**; Norway (Haaland) is also the bigger public name, so public chalk **also = Norway**.
- **Mexico vs Ecuador** — model **Mexico ~52%**; public almost certainly piles on host Mexico.
- **Australia vs Egypt** — model **Australia ~53%**; a genuine toss-up the public will split.

**Deep chalk / where public money piles.** The over-weighted champion line is **France (PM 27%)** and **Argentina (PM 20%)** as the two finalists, with **Spain (11%)** and **England (10%)** as the other semifinalists. The single most-replicated full bracket: chalk R32 favorites everywhere → R16/QF advancing the four market darlings → SF = France, Argentina, Spain, England → **Final France vs Argentina → Champion France** (Argentina close second-most-popular champion).

**Key model-vs-market conflict.** The repo model ranks title odds **Argentina 18.4% > Spain 15.3% > Brazil 14.1% > France 10.1% > England 9.0%** — i.e. the model thinks the public **over-rates France** and **under-rates Brazil/Spain** relative to where the chalk crowd concentrates.

### 5.2 Field crowding (where a perfect bracket gets shared)

Over-concentration is heaviest at the **top** of the bracket and on **name brands** — exactly where a perfect bracket is co-held.

1. **Champion/finalist slots.** France (PM 27%) + Argentina (20%) absorb ~47% of title belief between two teams; any perfect bracket ending France–Argentina will be co-held by a large share of survivors.
2. **The four "obvious" deep teams** (France, Argentina, Spain, England) — sending all four to the semis is the **densest cluster**; QF/SF crowding compounds there.
3. **Big-name R32 favorites in lopsided ties** (Argentina 91%, England 86%, Colombia 83%, Spain 81%, France 76%) are near-universal — correct but **worthless for differentiation** since almost everyone holds them.

*Crowding proxy.* An HHI-style win-prob² / Σwin-prob² shows the title pick is dominated by ~5 teams; the favorite-longshot bias likely makes France's **share of public CHAMPION picks even larger than 27%** (public over-rounds toward the single market favorite). From the QF onward, the marginal entrant looks almost identical to every other entrant.

**Crowding is THIN in:** the 3 coin-flip R32 ties (Norway/Ivory Coast, Mexico/Ecuador, Australia/Egypt), and in the identity of the eventual champion among the chasing pack (**Brazil, Colombia, Portugal**) — which the model rates materially higher than their PM-implied popularity (esp. **Brazil: model 14.1% vs PM 7%**).

### 5.3 EV-per-perfect reasoning

```
EV(B) ≈ Prize · P(B perfect) · E[ 1 / (1 + K) ],   Prize = $2,000,000,
```

`K` = number of OTHER entries also perfect **and identical where it matters** (sharing the exact champion/final/path). Three drivers:

1. **`P(you perfect)`** — even the chalkiest completion multiplies many sub-1 conditional probs (champion line ~10–27%, times each round), so realistic `P(perfect) ~ 1e-3` to `1e-5`: tiny, and roughly equal across "good" brackets, so **EV is not won or lost here much**.
2. **The split term `E[1/(1+K)]`** is **where strategy lives.** With `N` total entrants and a fraction `f` sharing your exact deep path, `K ~ Binomial(N−1, f · P(those picks all hit))`. For the modal France–Argentina-final path `f` is large (could be 10–40% of the field on the champion alone) → conditional on that path hitting, you split $2M many ways. For a Brazil- or Argentina-champion contrarian path `f` is far smaller → `1/(1+K)` much closer to 1.
3. **The trade you are pricing.** A lever that cuts `P(perfect)` by relative factor `r (<1)` but cuts expected co-winners by factor `c (<1)` is **+EV iff `r/c > 1`** (de-dup payout multiplier `1/c` exceeds probability cost `1/r`).

---

## 6. Prize-split / field-differentiation analysis (Objective C overlay)

With equal splitting among all perfect brackets,

```
EV_C(B) ≈ V · P(B perfect) · E[ 1/(1+K) | B perfect ].
```

Two competing forces:

- **(i) Raw hit rate** `P(B perfect)` — maximized by the MAP/chalk bracket (Objective A). Pure max-P ignores the field.
- **(ii) Dilution** `E[1/(1+K) | B perfect]` — **smallest exactly for the chalk bracket**, because the public concentrates on modal picks. If fraction `f_m` of the field picks `b_m` at `m`, the expected number of co-perfect entrants conditional on you being perfect scales roughly with `Π_m f_m` along your bracket (others must match the *same realized winners*). The chalk bracket maximizes `Π_m f_m` → maximizes `E[K | perfect]` → **maximizes dilution**.

**De-chalk rule.** Swapping pick at `m` from chalk team `c` to alternative `a`:

```
EV_C(B_a)/EV_C(B_c) ≈ [ P(a wins m | reached) / P(c wins m | reached) ] × [ E(1/(1+K_a)) / E(1/(1+K_c)) ].
```

First bracket (<1) is the hit-rate cost; second (>1, fewer of the field rode `a`) is the de-dilution benefit. **De-chalk when the second factor exceeds the inverse of the first** — i.e., on matches where the public is **heavily on a near-coinflip favorite** (`f_m` high but `q_m` only slightly above 0.5): small hit-rate sacrifice, large overlap reduction. **Do NOT de-chalk** on lopsided matches where the favorite is both popular AND genuinely ~90% (cost dominates).

### Differentiation levers (ranked, with the `r`/`1/c` read)

| Lever | Model cost (`r`) | De-dup benefit (`1/c`) | Verdict |
|---|---|---|---|
| **Argentina over France as champion** | `r ≥ 1` — model **favors** Argentina (18.4%) over France (10.1%), so this **gains** P(correct) | Sits in the smaller of the two top buckets | **Strictly +EV** — free de-dup; the default chalk-ish champion |
| **Brazil as champion** instead of France | `r ~ 0.5–0.7` (model 14.1% vs France 10.1%, but moves off the single most-crowded bucket; PM France 27% vs Brazil 7%) | `1/c` potentially **3–5×** | **Plausibly +EV** — best risk-adjusted de-dup lever in the bracket |
| **Spain deep run** (SF 41% / Final 26%, barely below France, well above England; public 11%) | small | meaningful | Cheap de-dup — send Spain to the final |
| **Ecuador over Mexico** (model ~48% vs 52%; public heavy on host) | `r ~ 0.95` | large `1/c` | **Clearly +EV** — near-free 1-of-12 differentiator |
| **Egypt over Australia** (~47% vs 53%) | `r ~ 0.95` | large `1/c` | **Clearly +EV** |
| **Ivory Coast over Norway** | `r ~ 0.86` (model already favors Norway; flipping costs **~14 pts**) | modest | **NOT recommended** |
| One mid-bracket upset (e.g. **Colombia** QF 54% / win 5.3% over a chalk side; public underweights) | low | high | Low-cost, high-de-dup |
| Flipping **80–90% favorites** (Argentina/Spain/England/France R32) | `r ~ 0.1–0.2` | little `1/c` (no one is fading them) | **Clearly −EV** — hold all lopsided favorites |

**Practical rule given unknown `N`:** hold all lopsided favorites; take the **second-most-popular champion where it costs no model edge** (Argentina or Brazil over France); spend the differentiation budget on the **2–3 coin-flip R32 ties** (Ecuador, Egypt).

**The genuine fork.** If the prize is **NOT split** — single winner drawn, or earliest-submission wins — the dilution term collapses to a constant and `EV_C` **reduces to Objective A** (max `P(perfect)`, i.e. **pure chalk**). With a $2M lottery and a large free-entry field, `P(any bracket perfect)` is tiny and `K` is usually 0 conditional on a sufficiently de-chalked bracket being perfect, which paradoxically pushes **back** toward "just maximize `P(perfect)`" unless the field is enormous and chalk-concentrated. **Net: prize-split nudges the optimum a few picks off pure chalk on popular-coinflip matches, but Objective A remains the backbone.**

> *Differentiation as variance-seeking under skewed payoffs (sensitivity branch only):* the de-chalk-for-share trade is analogous to bankroll/edge sizing for skewed bets — **Thorp, _A Man for All Markets_** (manifest #9) and **Sinclair, _Volatility Trading_, 2e** (manifest #8), Kelly / edge-and-variance and sizing under skewed payoffs.

---

## 7. Sensitivity to unverified scoring

**The binding objective is a discontinuous function of an UNVERIFIED rule (perfect-only vs partial-credit).** If a partial-credit / best-bracket / round-points layer turns out to exist, the recommendation **FLIPS** from Objective A toward Objective B, and the optimal bracket generally becomes **MORE chalk, not less.**

1. **Multiplicative → additive.** `Score = Σ_m g_m·1{b_m=W_m}` is linear in slot indicators, so `EV = Σ_m g_m·P(b_m=W_m)` decomposes per slot. You no longer need a single self-consistent perfect path — you bank points per correct slot independently. The 0–1 loss that justified the posterior MODE is replaced by a separable loss whose Bayes action is the **per-slot marginal-modal pick** (reachability-weighted). The greedy/marginal method — inert under A — becomes (near-)optimal.
2. **Picks shift toward high-MARGINAL teams.** Under max-joint you may pick a slightly-less-likely team because it threads a more coherent path; under expected-score you want the highest `P(b_m=W_m)` per high-weight deep slot. Round-escalating `g_m` make correctly identifying finalists/champion dominate, so the model's outright `win[]` probabilities become first-order and you bias hard to favorites in SF/Final slots.
3. **Ranked best-bracket payout curve.** Then `max E[payout(score)]` is a transform of the score distribution; a **convex/top-heavy** curve re-introduces a variance/tail consideration (you may want a higher-variance, more-differentiated bracket to win a top-heavy prize — echoing Kelly/edge-and-variance, Thorp & Sinclair). That can push **back toward de-chalking** — opposite to the mean-maximizing pull in (2) — so direction depends on the payout curve's convexity.
4. **Tooling impact.** Under additive scoring you can **drop** the `(n_sims × match)` joint path matrix and use the cheaper marginal `reach[]`/`win[]` outputs (EV is a sum of marginals). The expensive joint primitive is required **only** for the perfect-bracket (Objective A/C) world.

**Bottom line:** for the verified contest (perfect → $2M, assume no consolation) **Objective A binds and the joint path matrix is mandatory.** The instant any additive/partial-credit or ranked-payout layer is confirmed, **re-solve under Objective B** (marginal-modal, chalk-leaning, deep-round weighted using `win[]`), and for a convex top-heavy curve layer back controlled differentiation. **Keep BOTH solvers ready; do not hard-commit the pipeline to max-joint.**

---

## 8. Recommendation

1. **ADOPT OBJECTIVE A** as the binding objective: maximize the **joint** probability that all remaining picks (12 R32 ties + R16 + QF + SF + Final) are correct — the **MAP / posterior-mode** bracket conditioned on `S0` (M73 Canada, M74 Paraguay, M75 Morocco, M76 Brazil advanced). It is the unique objective justified by the verified perfect-only, no-partial-credit, free-entry structure. **Expected-correct-slots and expected-points are INERT and must not drive picks.**

2. **BUILD ON THE EXISTING ENGINE, ONE NEW PRIMITIVE.** Expose `TournamentSimulator(return_paths=True)` to emit the `(n_sims × match_no→winner)` matrix from `_run_knockout` (`src/wca/sim/tournament2026.py`), conditioned on M73–M76 via the played-results mechanism (`advancement.load_played_group_results` pattern, extended to seed the decided R32). Score any candidate as the **fraction of sims in which every picked winner matches** — captures all reachability/correlation for free. This must be a **NEW research script** under `docs/research/wca_alpha_2026/scripts` that **imports** wca primitives; **do NOT modify `src/`** (guardrail).

3. **SEARCH RECIPE.** Seed with the per-slot chalk bracket (`q_m`-argmax from marginal `win[]`/`reach[]`); refine with greedy-swap + beam search scored against the cached joint path matrix (MAP-bracket search); optionally verify late-round subtrees with exact Bellman DP (`prob_fn(a,b,knockout=True)` is pair-Markov). Use **MC purely as the scorer**, large `N` or importance sampling (rare event; SE `≈ sqrt(p(1−p)/N)`).

4. **PRIZE-SPLIT OVERLAY (Objective C).** If the prize splits among perfect brackets (UNVERIFIED), apply a **light de-chalking pass** — move picks off the modal team **only** on popular near-coinflip matches (high `f_m`, `q_m` barely >0.5: take **Argentina (or Brazil) over France** as champion at ~zero/positive model cost; **Ecuador over Mexico**, **Egypt over Australia**), **never** on lopsided favorites. Backbone stays Objective A; differentiation is a second-order adjustment and **collapses to pure max-`P(perfect)`** if ties are broken by draw/earliest-submission.

5. **KEEP A SECOND SOLVER ON THE SHELF (sensitivity).** If partial-credit/round-points or a ranked best-bracket payout is ever confirmed, **re-solve under Objective B** (additive expected-score → marginal-modal, chalk-leaning, deep-round weighted using `win[]`); for a convex top-heavy payout, layer back controlled differentiation. Under additive scoring the joint matrix is unnecessary.

6. **GUARDRAIL / SCOPE.** The contest is **CLOSED** and no entry exists; this is a formal/retrospective completion ("as if open for remaining games"), valuable as the **joint-probability engine** that remaining-knockout and advancement pricing both need. **No profitability claim is made.** `P(perfect)` for any single bracket is small and dominated by **model and rule uncertainty** (`et_skill_weight` untuned; scoring/tie-break rules unverified). Preserve that uncertainty in any downstream use.

---

## 9. Library citations (consolidated)

- **Rachev, Hsu, Bagasheva & Fabozzi — _Bayesian Methods in Finance_** — "Prior and Posterior Distributions": Posterior Distributions **p.124**; Predictive (posterior-predictive) Distributions and Portfolio Selection **p.126** (TOC verified). *Imported idea:* the bracket-completion objective is a functional of the posterior-predictive distribution over remaining outcomes given `S0`; max-joint = posterior MODE of that predictive law.
- **Larsen & Marx — _An Introduction to Mathematical Statistics and Its Applications_, 4e** — Bayesian estimation / decision-theory & loss-function material (~Sec 5.8 / Ch. 14 in this edition). *Imported idea:* under 0–1 (all-or-nothing) loss the Bayes-optimal action is the posterior MODE → the perfect-bracket lottery's optimal completion is the MAP bracket. *(Page-exact cite pending: front matter image-scanned, no text layer; chapter-level cite per corpus manifest entry #1.)*
- **Sydsaeter, Hammond, Seierstad & Strom — _Further Mathematics for Economic Analysis_** — Dynamic Programming / Optimal Control chapters (manifest #15). *Imported idea:* Bellman's principle of optimality and the value-function recursion for the DP-over-the-bracket-tree method. *(Page-exact cite pending: image-scanned, no text layer; chapter-level cite per manifest.)*
- **Mosteller — _Fifty Challenging Problems in Probability with Solutions_** — conditional probability (~p.idx32), multiplication principle (~p.idx53), independence (~p.idx8) (verified text). *Imported idea:* the chain-rule factorization `P(perfect)=Π P(b_m correct | feeders correct)` and why multiplying marginals is invalid without independence.
- **Zhou — _A Practical Guide to Quantitative Finance Interviews_** — probability/combinatorics & DP problem sets (manifest #10). *Imported idea:* combinatorial enumeration of bracket paths and DP-on-a-tree recursions for path-probability. *(Page-exact cite pending: image-scanned, no text layer; chapter-level cite per manifest.)*
- **Thorp — _A Man for All Markets_** (manifest #9) and **Sinclair — _Volatility Trading_, 2e** (manifest #8) — Kelly / edge-and-variance and sizing under skewed payoffs. *Imported idea (sensitivity branch only):* a convex/top-heavy ranked payout curve makes optimal differentiation trade mean for favorable variance, analogous to bankroll/edge sizing for skewed bets.

---

## 10. Files of record (absolute paths)

- Bracket topology + sim engine — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/src/wca/sim/tournament2026.py` (`R32_TIES`, `KNOCKOUT_FEED`, `THIRDS_ALLOCATION`, `_run_knockout`, `_play_ko`).
- Model→bracket bridge — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/src/wca/advancement.py:159` (`make_prob_fn`, `load_played_group_results`).
- Audit of the missing joint-path primitive — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/docs/research/wca_alpha_2026/02_codebase_audit.md` (forecasting-core §1, Gaps + Reuse rec #1–2).
- Verified contest rules — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/docs/research/wca_alpha_2026/00_foundation.md` §2.
- Corpus / citations — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/docs/research/wca_alpha_2026/01_corpus_manifest.md`.
- Repo model (per-team P(R32..win), conditional matchup edges) — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/data/advancement_current_vs_pretournament.json`.
- Public-field analysis script — `/Users/andrewdoherty/Desktop/Coding/World Cup Alpha/docs/research/wca_alpha_2026/scripts/public_field_chalk.py`.
- PM/public-favorite snapshot (2026-06-30) — defirate.com world-cup-odds; Polymarket world-cup-winner market.

---

*Uncertainty preserved throughout: forecasting model untuned (`et_skill_weight` not fit); prize-split and tie-break rules UNVERIFIED (a genuine objective-selecting fork); public-field distribution inferred from PM prices + seeding, not observed entries; `P(perfect)` is small and dominated by model and rule uncertainty. No profitability is claimed; the contest is closed.*
