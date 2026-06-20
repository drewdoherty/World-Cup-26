# Proposal — Overhaul the Visuals tab (fuse with "Under the Hood", make it live)

**Status:** draft · **Date:** 2026-06-20 · **Owner:** desk

## 1. Summary

The **Visuals** tab is four hand-drawn SVG exhibits with no live data (and a
fifth that is broken), and the **Under the Hood** tab is a beautiful but 100%
**hardcoded** mirror of `docs/architecture/SYSTEM_MAP.md`. Neither refreshes;
both drift stale the moment the code moves. Meanwhile the repo already emits the
exact machine-readable internals these tabs should be showing
(`data/model_predictions.json`, `data/elo_ratings_corrected.json`,
`data/dc_params_corrected.json`, `data/prop_calibration.json`,
`site/tracking_data.json`, `site/exposure_data.json`, `site/linemove.json`).

Proposal: **merge Visuals + Under the Hood into one live "Model Room"** driven by
those feeds, refreshed on the **terminal's hourly cadence**, so the page proves
what the model is actually doing today rather than describing what it did on a
past date.

## 2. Current state (mapped)

### Visuals tab (`site/visuals.html`, `site/visuals.js`)
| # | Exhibit | Data source | Refreshed? | Health |
|---|---------|-------------|-----------|--------|
| 1 | Time-decay weight curve (8y half-life) | hardcoded SVG | n/a | static, fine as explainer |
| 2 | Data → model → output flow | hardcoded SVG | n/a | static, fine as explainer |
| 3 | Advancement history (top-12 P(reach final) across model versions) | `site/advancement_history.json` | **file doesn't exist; not in publish** | **broken — shows a placeholder** |
| 4A | Structural strength Z-scores (sample teams) | hardcoded SVG | n/a | static; opt-in prior, never deployed |
| 4B | Host advantage (legacy vs venue-aware Elo) | hardcoded SVG | n/a | static; opt-in prior, never deployed |

No feed polling; `visuals.js` fetches `advancement_history.json` once and stops.

### Under the Hood tab (`site/architecture.html`, `site/architecture.js`)
Comprehensive 5-stage system map (Ingestion → Models → Decision → Execution →
Feedback) + money-flow + improvement map — **entirely hardcoded** in a data
array (`architecture.js` lines 23–511), explicitly "regenerate by hand when
SYSTEM_MAP.md changes." It already shows specifics that **will** rot: blend
weights `0.10/0.30/0.60` "deployed 2026-06-18", CLV ladder £1.5k/£2.5k/£5k, DC
half-life, edge filter 2%, etc. Nothing validates these against the live code.

### The gap
The site **describes** the model in prose but never **shows the live numbers**,
even though they exist as feeds:

- `data/model_predictions.json` — per-fixture Elo / DC / Market / Blend probs + edge + `generated` ts.
- `data/elo_ratings_corrected.json` — 48 live team Elo ratings + `as_of`.
- `data/dc_params_corrected.json` — xi (≈8y half-life), ridge λ, rho, min-matches, low-data multiplier.
- `data/prop_calibration.json` — corners/cards/scorer priors + per-fixture lambdas.
- `site/tracking_data.json` — model-vs-market Brier / logloss / top-6 hit rate (already built, unused by Visuals).
- `site/exposure_data.json` — portfolio P&L scenarios, blind spots, correlation (see the risk proposal).
- `site/linemove.json` — odds movement over time (large; currently unused by Visuals).
- Ledger reports (`src/wca/ledger/reports.py`): `calibration_report()`, `staking_stats()` (CLV-ladder progress) — only in the bot today.

## 3. Proposed overhaul (phased)

### Phase 0 — Fix what's broken + put it on the terminal's cadence
- **Repair Exhibit 3:** ensure the advancement snapshotter writes
  `site/advancement_history.json` and add it (+ a regeneration step) to
  `deploy/publish_site.sh`'s run list and `git add`. Today it is neither
  produced nor published.
- **Adopt the hourly cadence:** any feed the new page reads must be regenerated
  and committed by `publish_site.sh` (same job that refreshes the terminal).
  `elo_ratings_corrected.json` / `dc_params_corrected.json` /
  `model_predictions.json` are written by the hourly card build — confirm they
  are committed so the site sees them; add the few that aren't.
- **Stamp + stale-flag** every panel with its feed's `generated`/`as_of` time
  (mirroring the terminal), so a stale panel is never mistaken for live.

### Phase 1 — Live "Model Room" panels (fuse Under-the-Hood detail with viz)
Drive these from the feeds above, refreshed hourly:
1. **Model decomposition** — per upcoming fixture: Elo vs DC vs Market vs Blend, edge %, status (from `model_predictions.json`). The Visuals "flow" exhibit becomes a *live* readout.
2. **Live Elo ladder** — sortable team ratings + recent trend (`elo_ratings_corrected.json`).
3. **DC internals readout** — current xi → half-life, ridge λ, rho, low-data shrink (`dc_params_corrected.json`); makes Exhibit 1 reflect the *deployed* value, not a baked constant.
4. **Calibration** — model vs market reliability bins + Brier/logloss/top-6 (`tracking_data.json` + `calibration_report()`). This is the single most credible "is the edge real" visual and it is currently nowhere on the site.
5. **CLV-ladder progress meter** — rung, n_settled/threshold, rolling-50 CLV, on-track/at-risk (`staking_stats()`); the bankroll governance, made visible.
6. **Prop-model priors** — corners/cards/scorer base rates + per-fixture lambdas (`prop_calibration.json`).

### Phase 2 — New analytical visuals
7. **Line-movement timeline** — implied-prob drift per fixture, model line overlaid (`linemove.json`).
8. **Exposure / correlation** — portfolio P&L distribution, worst-case states, blind-spot map (`exposure_data.json`; shared with the risk-panel proposal).
9. **Prediction ladder** — current fixtures' top scorelines + O/U/BTTS (`data.json.predictions` / `scores_data.json`).
10. Keep exhibits 1/2/4 as static explainers, but label them clearly as concept diagrams vs the live panels.

### Phase 3 — Kill static drift on "Under the Hood"
- Generate the architecture/under-the-hood content from **live config + feeds**
  rather than the hand-maintained array: blend weights from `wca.card.BlendWeights`,
  ladder from `resolve_pool_bankroll`, params from the `*_corrected.json`. Either
  emit a `site/under_the_hood.json` at build time, or render the dynamic numbers
  inline so the prose can't contradict the deployed code.

## 4. Cadence (the through-line)

Everything new reads feeds regenerated + committed by the hourly
`publish_site.sh`, exactly like the terminal — no panel may depend on a one-off
manual run (the failure mode that froze Exhibit 3 and the exposure feed). All of
it is pure-compute over existing artifacts → **no extra Odds-API credits**.

## 5. Effort & sequencing

| Phase | Scope | Est. | API cost |
|------|-------|------|----------|
| 0 | repair Exhibit 3 + wire feeds into publish + stale stamps | ~0.5d | none |
| 1 | 6 live Model-Room panels | ~1–1.5d | none |
| 2 | line-move / exposure / prediction visuals | ~1d | none |
| 3 | auto-generate Under-the-Hood from config | ~0.5–1d | none |

Phases are independently shippable; Phase 0 stops the staleness, Phase 1 is the
highest-credibility content (decomposition + calibration), Phase 3 removes the
maintenance burden permanently.

## 6. Decisions to confirm
1. **Merge vs keep two tabs?** Recommend folding Under-the-Hood's live numbers
   into Visuals as one "Model Room", keeping the static explainers; or keep two
   tabs but make both live. Which?
2. **Calibration scope:** model-only, or model **vs** de-vigged market side by
   side (recommended — it's the edge story)?
3. **Exhibit 4 priors** (structural/venue) are opt-in and OFF in production —
   show them as "available, not deployed", or drop from the live page?

## 7. Recommendation
Ship **Phase 0** (repair + cadence) immediately, then **Phase 1** panel 4
(calibration) and panel 1 (decomposition) as the first live content — they
convert the page from "marketing diagram" to "live evidence the model works".
