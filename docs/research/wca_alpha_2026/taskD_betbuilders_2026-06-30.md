# Task D — Exact bet-builder legs (P1 PaddyPower, P2 Betfair) — 2026-06-30

Improved model = `harden/xg-totals` (level anchor 2.81 goals/match, Δμ=+0.18492 ⇒ λ×1.2031).
1X2 (supremacy) INVARIANT; only totals/Over/BTTS move. Joint pricing via reconciled
Dixon-Coles scoreline matrix (`scores.reconcile_scoreline_matrix`), correlation-aware.

England–DR Congo improved λ: England 2.404, DR Congo 0.530 (total 2.93). Reconciled 1X2
0.753 / 0.179 / 0.068.

## Leg-level edges (England–DR Congo, model fair vs PaddyPower single)
- England win 1.25 (fair 1.329) −5.9%
- England −1.5 1.57* (fair 1.869) −16.0%
- Over 2.5 1.85 (fair 1.834) **+0.9%**
- Over 1.5 1.25 (fair 1.277) −2.2%
- England team Over 1.5 1.53* (fair 1.500) **+2.0%**
- Kane anytime 1.80* (fair 1.845) −2.4%
- Bellingham anytime 3.40* (fair 4.632) −26.6% (book too short)
(* = PP feed lacked spreads/team_totals/scorer; indicative WH/Betfair-exchange, CONFIRM IN PP UI)

Only the totals legs (Over 2.5, team Over 1.5) are model-cheap — exactly the xG-grounded edge.
Moneyline/handicap/scorer legs are near-fair or book-short = NOT alpha.

## P1 — PaddyPower 2×£5, 3-leg SGM, combined ≥2.0
SGM combined priced via JOINT matrix (legs positively correlated; naive product overstates).
Book SGM estimate = geometric mean of naive product and full-joint-corrected (books apply
partial correlation + margin). EVs are indicative; free-bet value is mechanical.

**P1-A (Kane-free, preferred):** England win + Over 2.5 + England team Over 1.5
- joint P 0.485, model-fair 2.063; book est ~2.66 (naive 3.54). ≥2.0 ✓.

**P1-B (Kane):** England −1.5 + Over 1.5 + Kane anytime
- joint P 0.345, model-fair 2.897; book est ~2.86. ≥2.0 ✓. Near-neutral EV.

## P2 — Betfair £10, 3 legs ~1.5 each (cross-game ⇒ INDEPENDENT, naive product correct)
xG-grounded Over 2.5 is strongly +EV in high-λ later games:
- Spain–Austria Over 2.5 @1.73 (fair 1.387) **+24.7%** (Jul2)
- Argentina–Cape Verde Over 2.5 @1.62 (fair 1.405) **+15.3%** (Jul3)
- Colombia win vs Ghana @1.50 (fair 1.497) +0.2% (Jul4)
Combined book 4.204 vs model-fair 2.917 ⇒ +44% EV. Legs 1.73/1.62/1.50 (slightly above
1.5 each; combined 4.20 > 3.375 target — the grounded Over edges sit at 1.6–1.7).

## Exposure (data/wca.db, read-only)
- OPEN: Harry Kane Golden Boot £10@7.5 (betfair_sportsbook) ⇒ P1-B Kane leg stacks same player
  (mild positive correlation; acceptable for a £5 free bet but prefer P1-A to avoid).
- Old England–Ghana builders are settled/void (Ghana was prior R32 opponent; now DR Congo). No
  live England–DR Congo exposure.
- P2 legs (Spain/Argentina/Colombia totals) are not present in current open positions — no double-load.

NOTE: exact SGM/builder prices MUST be confirmed in the PaddyPower / Betfair UI at placement;
API does not return combined builder prices. Analysis only — no orders placed.
