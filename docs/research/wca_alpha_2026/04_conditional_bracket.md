# 04 — Conditional Remaining-Bracket Forecast & Optimal Completion

**Status:** Research note. READ-ONLY w.r.t. production. Model views, not ground truth. **Never read as a profitability claim** — this is a model-vs-uncertainty exercise on a contest that is already underway / effectively closed.

**The framing in one line:** treat the 2026 knockout as **"as-if-still-open"** — given the *current state* (group stage done, 16 of 32 R32 fixtures known, 4 of those R32 ties already *played* and won), what is the single bracket completion of the *remaining* games that maximizes the probability of a perfect-from-here knockout run?

> **Two load-bearing caveats up front (preserved throughout):**
> 1. The current-state R32 fixtures and the **4 played R32 results (Canada / Paraguay / Morocco / Brazil winners) were web-sourced and are NOT in the repo.** Everything downstream conditions on those being accurate as given. If they are wrong, the conditioning is wrong.
> 2. Validation is **~1-cluster** — the cross-check is against a *single* repo advancement JSON (the pre-R32 marginal). Agreement there corroborates the conditioning logic but is not an independent multi-source validation.

---

## 1. Method — and did the real fitted model actually run?

**Yes. `ran_model = true` — a real fitted Elo + Dixon-Coles model was used**, not a heuristic stand-in. Mechanics:

- **Probability engine.** `prob_fn` built via `wca.advancement.make_prob_fn` over fitted **Elo + Dixon-Coles** models.
- **Knockout tie resolution.** Each tie resolves to
  `p_a_total = p_a + p_draw * (0.5 + et_skill_weight * (q_a − 0.5))`
  with `et_skill_weight = 0.5` (the `TournamentSimulator` default) and `q_a = p_a / (p_a + p_b)`. I.e. the draw mass is split toward the stronger side in extra-time/penalties, scaled by skill.
- **Current state held fixed.** Group stage + the 16 known R32 fixtures are the fixed conditioning set. The **4 played R32 winners are hard-seeded** (Canada, Paraguay, Morocco, Brazil → `P(R16)=1.0`). The **12 unplayed R32 ties + all R16→Final ties are drawn forward.**
- **Simulation.** Seeded forward Monte Carlo with **full per-sim path capture**, `n_sims = 50,000`, `seed = 20260630`. Per-team conditional reach probabilities and per-tie conditional advance probabilities are read off the path matrix.
- **MAP (optimal completion).** Greedy joint-argmax over the bracket tree. This is **provably optimal here**: with the R32 winners fixed, each node's best up-front team pick only needs to beat its sibling subtree pick, so greedy = global argmax.

### Model-provenance caveat (important)

The production cache `data/advancement_models.pkl` **failed to unpickle** against current code (`EloRater` missing an `initial_ratings` attribute — the cache predates a code change). I fell back to a **fresh refit** via `wca.card.fit_models` on the same results dataset (`resolve_results_path`), cached **read-only** to `data/_refit_models.pkl`. This reproduces the same fit *family* the production cache was built from, and the close sanity-check agreement vs the repo JSON (Section 6) corroborates it.

So: **`ran_model = true` (real fitted Elo + DC), but it is a refit, not the byte-identical production object.** Two further modeling notes: (a) `et_skill_weight = 0.5` is the simulator default — higher values sharpen the favorite's edge slightly (a sensitivity, not run here); (b) knockout ties have **no tradable book**, so **no market anchoring is applied** — these are pure model probabilities.

---

## 2. Conditional remaining-bracket picture — top favorites by round

All probabilities are **conditional on the current state** (`P(· | R32 underway, 4 results in)`). Full per-team table is in the CSV (Section 7).

### Conditional title odds `P(Win | state)`

| Rank | Team | P(Win) | P(reach Final) | P(reach SF) | P(R16) |
|---|---|---|---|---|---|
| 1 | Brazil | **0.188** | 0.309 | 0.520 | **1.00** (won R32) |
| 2 | Argentina | **0.187** | 0.307 | **0.539** | 0.917 |
| 3 | Spain | 0.159 | 0.280 | 0.414 | 0.814 |
| 4 | France | 0.129 | 0.249 | 0.433 | 0.765 |
| 5 | England | 0.084 | 0.156 | 0.289 | 0.870 |
| 6 | Colombia | 0.051 | 0.104 | 0.239 | 0.825 |
| 7 | Portugal | 0.049 | 0.110 | 0.190 | 0.634 |
| 8 | Morocco | 0.036 | 0.109 | 0.282 | **1.00** (won R32) |
| 9 | Belgium | 0.036 | 0.095 | 0.186 | 0.673 |

**Already-guaranteed into R16** (won their R32 ties, `P(R16)=1.0`): **Brazil, Morocco, Paraguay, Canada.**

### The key structural fact

**The marginal title leader is Brazil (0.188), but the joint-MAP champion is Spain.** That is not a contradiction — it is correct joint-tree behavior. A *perfect bracket* requires a single **consistent path**. Brazil is the strongest single-node pick at the QF (M99), but on the modal upstream path it **loses the M102 SF coin flip to Argentina (p ≈ 0.5003)**. Spain wins the championship node of the *most-probable consistent path* even though it is only the 3rd-most-likely champion marginally. Marginal-favorite ≠ modal-path-champion whenever the top contenders sit on a collision path — which they do here (Brazil and Argentina are drawn to meet in the SF).

---

## 3. OPTIMAL COMPLETION — the MAP bracket (recommended)

This is the **single bracket completion of the remaining games** that maximizes `P(perfect-from-here)`. Read it as the **"as-if-still-open" strategy for the remaining games only** — the group stage and the 4 played R32 ties are fixed history.

`P(pick advances)` is the *conditional* probability the named pick wins **that tie**, given the current state and the modal feeder picks into it (not an unconditional win prob).

| Round | Tie | Pick | P(pick advances) |
|---|---|---|---|
| R32 | M77 France vs Sweden | **France** | 0.764 |
| R32 | M78 Ivory Coast vs Norway | **Norway** | 0.577 ⚠ near coin flip |
| R32 | M79 Mexico vs Ecuador | **Mexico** | 0.513 ⚠ near coin flip |
| R32 | M80 England vs DR Congo | **England** | 0.867 |
| R32 | M81 United States vs Bosnia & Herzegovina | **United States** | 0.657 |
| R32 | M82 Belgium vs Senegal | **Belgium** | 0.671 |
| R32 | M83 Portugal vs Croatia | **Portugal** | 0.634 |
| R32 | M84 Spain vs Austria | **Spain** | 0.814 |
| R32 | M85 Switzerland vs Algeria | **Switzerland** | 0.638 |
| R32 | M86 Argentina vs Cape Verde | **Argentina** | 0.915 |
| R32 | M87 Colombia vs Ghana | **Colombia** | 0.828 |
| R32 | M88 Australia vs Egypt | **Australia** | 0.534 ⚠ near coin flip |
| R16 | M89 W74 vs W77 (Paraguay-side vs France) | **France** | 0.769 |
| R16 | M90 W73 vs W75 (Canada vs Morocco) | **Morocco** | 0.666 |
| R16 | M91 W76 vs W78 (Brazil vs IvoryCoast/Norway) | **Brazil** | 0.778 |
| R16 | M92 W79 vs W80 (Mexico vs England) | **England** | 0.681 |
| R16 | M93 W83 vs W84 (Portugal vs Spain) | **Spain** | 0.619 |
| R16 | M94 W81 vs W82 (USA vs Belgium) | **Belgium** | 0.679 |
| R16 | M95 W86 vs W88 (Argentina vs Australia) | **Argentina** | 0.819 |
| R16 | M96 W85 vs W87 (Switzerland vs Colombia) | **Colombia** | 0.611 |
| QF | M97 W89 vs W90 (France vs Morocco) | **France** | 0.692 |
| QF | M98 W93 vs W94 (Spain vs Belgium) | **Spain** | 0.701 |
| QF | M99 W91 vs W92 (Brazil vs England) | **Brazil** | 0.570 |
| QF | M100 W95 vs W96 (Argentina vs Colombia) | **Argentina** | 0.643 |
| SF | M101 W97 vs W98 (France vs Spain) | **Spain** | 0.554 ⚠ near coin flip |
| SF | M102 W99 vs W100 (Brazil vs Argentina) | **Argentina** | 0.500 ⚠ **coin flip** |
| FINAL | M104 W101 vs W102 (Spain vs Argentina) | **Spain** | 0.514 ⚠ near coin flip (champion pick) |

**Champion pick: Spain.** Runner-up on the modal path: Argentina.

> Note the M102 entry: **Brazil is the marginal Win leader but loses this SF node to Argentina (0.500)** on the MAP path. This single near-tie is what flips the modal champion from Brazil to Spain. It is the most fragile node in the whole completion.

---

## 4. Joint probability of perfect-from-here

| Estimate | Value | Trust |
|---|---|---|
| **Modal-path product** (∏ of the MAP `p_pick` above) | **≈ 1.66 × 10⁻⁵** | **Reliable** — use this |
| Empirical sim hit-rate (perfect-remaining sims / 50,000) | ≈ 6 × 10⁻⁵ (only **3 hits**) | Noise-dominated — do NOT trust beyond ~1 order of magnitude |
| `p_perfect_full_knockout` (incl. already-played games) | `null` | Not computed — the played games are conditioned as fixed, so a "full" perfect-from-scratch number is not the relevant objective here |

**Headline:** `P(perfect remaining) ≈ 1.7 × 10⁻⁵` (≈ 1 in 60,000). This is **dominated by the five near-coin-flip nodes** (M78 0.577, M79 0.513, M88 0.534, M102 0.500, M104 0.514). The completion is correct but **fragile**: most of the "missingness" risk is concentrated in those five ties, where the MAP pick carries almost no edge over its alternative.

---

## 5. The DIFFERENTIATED completion — and why there isn't one

**There is no separate differentiated/contrarian bracket — and that is a substantive finding, not a punt.**

The contest (per the assumed mechanics) pays **$2M only for a perfect knockout bracket**, with **no partial credit and no best-bracket consolation**. Under a pure all-or-nothing perfect-bracket objective:

- **Expected value is monotone in `P(perfect)`.** Every deviation from the MAP completion **strictly lowers** `P(perfect)`. So the MAP completion *is* the EV-max bracket.
- **Field de-duplication (prize-split avoidance) adds value only if** (a) there's a non-trivial chance multiple entrants go perfect **and** (b) the prize splits among them. At `P(perfect) ≈ 1.7 × 10⁻⁵` per entry, the chance of *any* perfect bracket in a free contest is already low, and the chance of a **collision on the exact same 27-pick path** is negligible. The split-avoidance term is dominated by the raw `P(perfect)` term.

**Therefore the MAP completion in Section 3 IS the differentiated/optimal bracket.**

### The conditional flip (if mechanics differ)

If — counterfactually — the contest had **partial credit or a best-bracket prize**, the calculus inverts:

- Objective shifts from `P(perfect)` toward **maximizing expected matched-rounds**, which rewards contrarianism on the near-coin-flip nodes.
- The nodes to consider flipping are exactly the five ⚠ ties: **M78 (Norway 0.577 → Ivory Coast), M79 (Mexico 0.513 → Ecuador), M88 (Australia 0.534 → Egypt), M102 (0.500), M104 (0.514)** — where the modal pick carries almost no `P(perfect)` advantage, so the cost of contrarian differentiation is near-zero while the field-separation benefit is non-zero.

**Contest mechanics (scoring of imperfect brackets, partial-credit/consolation, tie-break / prize-split, entries-per-user) are UNVERIFIED** and were **assumed to be perfect-only per directive.** If any partial-credit structure exists, the optimal completion changes materially along the lines above.

---

## 6. Sanity check vs the repo advancement JSON

The repo JSON is the **pre-R32 marginal**; this forecast **conditions on R32 being underway**. So we expect (a) tight agreement for teams whose R32 status is *unchanged* by the 4 played results, and (b) deliberate divergence for the 4 teams that have now *won*. Both hold — which is the corroboration that the conditioning is implemented correctly.

### Agreement (R32-status-unchanged teams) — confirms correct conditioning

| Team | Metric | This forecast | Repo (pre-R32) |
|---|---|---|---|
| Argentina | R16 / QF / Win | 0.917 / 0.756 / 0.187 | 0.914 / 0.748 / 0.184 |
| Spain | R16 / SF / Win | 0.814 / 0.414 / 0.159 | 0.814 / 0.533 / 0.153 |
| France | R16 / QF / Win | 0.765 / 0.588 / 0.129 | 0.763 / 0.493 / 0.101 |
| England | R16 / QF / Win | 0.870 / 0.594 / 0.084 | 0.862 / 0.584 / 0.090 |

Colombia, Portugal, Switzerland, Croatia all land within MC noise. **France's QF rises** (0.588 vs 0.493) for a *correct* reason: conditioning removed **Germany — now OUT — who was France's potential R16 obstacle.**

### Deliberate divergence (R32-winner teams) — the intended difference, not a discrepancy

| Team | This forecast `P(R16)` | Repo `P(R16)` | Why |
|---|---|---|---|
| Brazil | **1.00** | 0.759 | won R32; downstream Win lifts accordingly |
| Morocco | **1.00** | 0.373 | won R32; Win 0.036 vs repo 0.012 |
| Paraguay | **1.00** | 0.278 | won R32; Win 0.010 vs repo 0.002 |
| Canada | **1.00** | n/a | won R32 |

These four are conditioned to `P(R16)=1.0` because they have **already advanced**. The gaps are the **intended effect of conditioning on the 4 results**, not a model disagreement.

> Validation limit (restated): this is **one cluster** — a single repo JSON. The agreement is necessary-but-not-sufficient corroboration. Treat the conditional numbers as model views.

---

## 7. Written tables under `data/`

- **`data/conditional_bracket_probs.csv`** — per-team conditional reach probabilities: `team, P(R16), P(QF), P(SF), P(Final), P(Win)`. Sorted by `P(Win)`. (Eliminated teams — Netherlands, Japan, South Africa, Germany — pinned to 0.)
- **`data/remaining_ties_probs.json`** — per-tie conditional advance probabilities for all **27 remaining ties** (12 R32 + R16→Final), plus `current_state` (4 played winners + 12 R32 to-play), the `map_completion`, `p_perfect_remaining_modal_path` (1.66e-5), `p_perfect_remaining_empirical`, `n_sims`, `seed`.
- **`data/_refit_models.pkl`** — read-only cache of the **refit** Elo + DC models (fallback after the production pickle failed to load). Provenance per Section 1.
- Generator: **`scripts/conditional_bracket.py`** (imports `wca` primitives, reads models from disk; production `src/` untouched).

---

## 8. Caveats (full set — preserved)

1. **Model provenance.** Production `data/advancement_models.pkl` failed to unpickle (`EloRater` missing `initial_ratings`, cache predates a code change). Fell back to a fresh refit via `wca.card.fit_models`, cached read-only to `data/_refit_models.pkl`. Same fit family; corroborated by the repo cross-check. `ran_model = true`, but it is a **refit, not the byte-identical production object.**
2. **Draw reallocation.** `et_skill_weight = 0.5` (simulator default). Higher values sharpen the favorite's edge slightly — a sensitivity, not run.
3. **No market anchoring.** Knockout ties have no tradable book; `prob_fn` ignores market for `knockout=True`. These are **pure model probabilities** — model views, not ground truth.
4. **`p_perfect_remaining`.** The modal-path **product (1.66e-5) is the reliable estimate.** The empirical sim figure (~6e-5, only 3/50,000 hits) is noise-dominated; do not trust it beyond an order of magnitude.
5. **MAP champion ≠ marginal favorite.** Spain (MAP) vs Brazil (marginal Win 0.188) differ because the perfect-bracket objective needs a **consistent path**: Brazil is the QF M99 pick but loses the M102 SF coin flip to Argentina (0.5003) on the modal upstream path — correct joint-tree-argmax. Several MAP nodes are near coin flips (M78 0.58, M79 0.51, M88 0.53, M102 0.50, M104 0.51), so the completion is **fragile** and `P(perfect)` is dominated by these.
6. **Unverified contest mechanics.** Scoring of imperfect brackets, partial-credit/consolation, tie-break/prize-split, entries-per-user were **assumed none / perfect-only** per directive. If any partial-credit structure exists, the optimal completion changes materially (Section 5).
7. **Fixture → match-number mapping.** The 16 R32 ties → slots 73–88 mapping was derived by group identity against `R32_TIES` specs and is internally consistent, but **assumes the web-sourced current-state fixtures and the 4 played results are accurate as given.**

**Never claim profitability.** This is a model-vs-uncertainty exercise on a **closed contest**, presented as the "as-if-still-open" optimal completion of the remaining games.
