# Proposal — Overhaul Risk & Blind Spots (Scores & Markets tab)

**Status:** draft · **Date:** 2026-06-20 · **Owner:** desk

## 1. Summary

The **Risk & Blind Spots** panel and the per-fixture **exposure** column on the
Scores & Markets tab are driven by `site/exposure_data.json`. That feed is
**frozen at 2026-06-13** (it is the only site feed the hourly publish job does
not regenerate), so the tab currently shows last week's fixtures, last week's
open positions, and blind spots for matches that have already been played.
Separately, even when fresh the engine **omits every non-1X2 bet** (scorelines,
player props, the Bet Builder, goalscorer, advancement/outright futures) from
the portfolio P&L distribution and the blind-spot scan, so the headline risk
numbers understate and misrepresent real exposure.

This proposal does two things:
1. **Refresh on the terminal's cadence** (the explicit ask) — a ~1-line fix that
   is free (no API credits).
2. **Overhaul the accuracy** of the exposure/blind-spot engine and the panel UX
   so the numbers are trustworthy and actionable.

## 2. How it works today (grounded)

- **Feed:** `scripts/wca_exposure_data.py` → `src/wca/exposure.py` → `site/exposure_data.json`.
- **Inputs:** open bets from `data/wca.db`, model 1X2 from `data/model_predictions.json`,
  and the newest **cached** h2h snapshot `data/raw/snapshots/oddsapi_h2h_uk_*.json`
  (for plug prices). **No live Odds-API calls** — so it is cheap to run often.
- **Engine (`exposure.py`):**
  - Per-fixture: for each 1X2 outcome, `direct_pnl` (result singles) + `acca_ev`
    (model-conditional acca payoff) → `net_pnl`.
  - **Blind spot** = `net_pnl ≤ £0.50` **and** `model prob ≥ 0.18` (`BLINDSPOT_NET_FLOOR`, `BLINDSPOT_MIN_PROB`).
  - **Portfolio:** full joint enumeration of `{home, draw, away}` across every
    slate fixture (3^N scenarios; the current feed has 81 = 3^4) → EV, best,
    worst, P(profit), P(loss), P(win ≥ £50), worst/best result-states. Assumes
    fixtures are **independent** (multiplies marginal 1X2 probs).
  - **Plug:** best price for a blind-spot outcome from the cached snapshot +
    whether plugging is +EV / marginal / leave-unhedged.
  - Free bets (`source == 'offer'`) are correctly treated as stake-not-returned.
- **UI (`site/scores.js`):** `renderRisk` (stat strip, narrative, blind-spot list
  + plug, worst/best states) and `renderExposure` (per-fixture result ladder with
  `BLIND` tags). The panel **never shows the feed's `generated` time or a stale
  banner**, so a stale feed is indistinguishable from a live one.

## 3. Problems

### A. Stale — wrong cadence (the headline)
`deploy/publish_site.sh` regenerates and commits `data.json`, `linemove.json`,
`scores_data.json`, `tracking_data.json` every hour, but **not**
`exposure_data.json`. It is only ever produced by a manual run, so it is
permanently frozen (currently 7 days old). The terminal refreshes hourly; this
panel does not refresh at all.

### B. Inaccurate — methodology gaps
1. **Non-1X2 bets are excluded from portfolio risk.** Only result singles
   (markets in `{Full-time result, Match Odds, Match Winner, h2h}`) and ACCAs feed
   the scenario P&L. Scorelines, BTTS/O-U, player props, the **Bet Builder (#97)**,
   goalscorer and advancement/Golden-Boot futures are shown per-fixture as
   "events" but contribute **£0** to EV / best / worst / P(profit) and are never
   blind-spot candidates. The headline risk is therefore materially wrong.
2. **No upcoming-only / settled filter.** The slate is "every fixture in
   `model_predictions.json`"; there is no `kickoff > now` or not-settled guard, so
   in-play or just-finished fixtures can still appear as live exposure.
3. **Fragile mapping.** Bets map to fixtures via a market-name allowlist, a regex
   acca-leg parse (`Team+Team`), and fuzzy substring team matching. New market
   spellings or acca/label formats silently fall to `unmapped` / off-slate and
   drop out of the risk picture.
4. **Plug prices from a single cached snapshot** with fuzzy team matching and h2h
   only — stale or missing when snapshots lag, and no plug for non-1X2 blind spots.
5. **Blind spots are 1X2-only.** "Not covered" is judged purely on the match
   result; a probable scoreline/prop you are exposed to (or blind on) is invisible.

### C. UX — staleness and risk are not legible
- No "last updated / stale" indicator → a frozen feed reads as current.
- P(loss) and worst-case mix real-money and free-bet risk without separating the
  **real-money downside** (what can actually be lost) from notional.
- Blind-spot "plug" guidance is buried; no one-glance "are we covered?" verdict.

## 4. Proposed overhaul (phased)

### Phase 0 — Cadence fix (immediate, ~1 hour, zero API cost)
- Add `wca_exposure_data.py` to `deploy/publish_site.sh` (after `wca_site.py`,
  since both read the ledger + `model_predictions.json`) and add
  `site/exposure_data.json` to the `git add` list so it commits + pushes hourly
  with the rest of the feeds.
- Add a **stale banner** to `renderRisk` (mirror the terminal's freshness logic):
  show `meta.generated` and flag `> N hours` old.
- **Outcome:** the panel refreshes on exactly the terminal's cadence and can no
  longer silently show week-old data.

### Phase 1 — Make the risk numbers correct
- **Include all open bets in the P&L scenarios.** Extend the scenario model so
  scorelines, BTTS/O-U, props and the Bet Builder resolve against a richer
  per-fixture outcome space (not just 1X2). Minimum viable: settle each non-1X2
  bet by its **model win probability** as an independent Bernoulli contribution to
  EV and to the per-fixture exposure, and include multi-leg/Bet-Builder combos via
  their model-conditional payoff (as accas already are). Futures (Golden Boot,
  advancement) contribute to EV but sit in a clearly-labelled "futures" bucket,
  not the per-fixture ladder.
- **Upcoming-only slate:** filter to fixtures with `kickoff > now` and exclude
  settled/void bets explicitly.
- **Robust mapping:** replace the market-name allowlist + fuzzy matching with the
  canonical team-name resolver already used elsewhere (`wca.data.teamnames`), and
  surface anything still unmapped prominently (it is risk we cannot see).

### Phase 2 — Robustness & scale
- **Scenario cap / sampling:** 3^N enumeration is fine at N≤6 but explodes (3^8 =
  6561, 3^10 ≈ 59k). Cap exact enumeration at a threshold and switch to Monte-Carlo
  sampling above it (report the method used).
- **Fresher, broader plug odds:** read the latest snapshot per market (not h2h
  only), and reuse the card's `best_price` line-shopping; flag when the snapshot
  itself is stale.
- **Broaden blind spots** beyond 1X2 to the markets we actually hold (e.g. a
  probable BTTS/scoreline outcome with no/negative net).

### Phase 3 — Panel UX refresh
- One-glance verdict: "Covered / N blind spots" + last-updated chip.
- Separate **real-money worst case** from total (free-bet SNR already modelled).
- Make plug actions first-class (outcome · best price · venue · EV · verdict),
  and ensure the panel is mobile-legible (consistent with the recent dashboard
  mobile work).

## 5. Methodology notes / decisions to confirm

- **Independence assumption** (fixtures uncorrelated) is retained — the model has
  no joint structure today. Acceptable; worth stating in the narrative.
- **Free bets = stake-not-returned** stays as-is (correct).
- **Non-1X2 settlement basis:** Phase 1 proposes model-probability Bernoulli
  contributions. Decision needed: is "expected P&L by model prob" the right lens
  for the risk panel, or do you want **per-scoreline** joint scenarios (heavier,
  but exact for scoreline/BTTS/O-U exposure)?
- **Bankroll/scale context:** should the panel show worst case as a % of the
  resolved pool bankroll (the CLV-ladder figure) for context?

## 6. Effort & sequencing

| Phase | Scope | Est. | API cost |
|------|-------|------|----------|
| 0 | publish wiring + stale banner | ~1h | none |
| 1 | all-bets P&L + filters + mapping | ~0.5–1d | none |
| 2 | scenario cap + plug freshness | ~0.5d | none |
| 3 | panel UX | ~0.5d | none |

Phase 0 is independently shippable today and fixes the user-visible complaint
(stale + wrong cadence). Phases 1–3 are the accuracy/UX overhaul and can land
incrementally; all are pure-compute over existing cached inputs (no extra
Odds-API credits). Each phase ships with unit tests in `tests/test_exposure*.py`.

## 7. Recommendation

Ship **Phase 0 now** (cadence + stale banner) so the panel is live and honest,
then schedule **Phase 1** (correct numbers) as the substantive overhaul. Confirm
the two decisions in §5 (non-1X2 settlement lens; bankroll-relative worst case)
before starting Phase 1.
