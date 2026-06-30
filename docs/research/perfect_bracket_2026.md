# Polymarket "Perfect" Bracket — 2026 WC Knockout Analytics

**Contest:** predict the winner of all 31 knockout games (16 R32 · 8 R16 · 4 QF · 2 SF · 1 Final).
Prize: **$2M** for a perfect 31/31, **$100k** for best bracket if none perfect.
Tiebreakers: (1) accuracy, (2) total-goals prediction, (3) earliest submission.

> ⚠️ **Deadline:** Polymarket's posted rules close entries **2026-06-29 11:59:59 ET
> (15:59:59 UTC)**. Generated after that — treat as model showcase / methodology
> unless the on-site deadline differs. Verify on polymarket.com/perfect.

Source model: `site/advancement_data.json` (model_generated 2026-06-29 07:15 UTC) —
the rebuilt Elo+Dixon-Coles Monte-Carlo with full group-stage results baked in.
Bracket tree: `src/wca/sim/tournament2026.py` KNOCKOUT_FEED. Tool: `scripts/wca_perfect_bracket.py`.

## How to play it optimally

Two distinct objectives, two strategies:

1. **$2M perfect** — maximise P(all 31 right). The optimum is the **joint-mode
   bracket: the favourite at every single game.** Nothing clever; any deviation
   from the model favourite strictly lowers P(perfect). The bracket below is that.
2. **$100k best** — maximise E[# correct]. Still favourite-at-every-game, *but* if
   you expect many rivals to submit the same chalk you must **differentiate** to
   win uniquely. Do it in the cheapest coin-flips (below), not the locks.

## The model-optimal bracket

| Round | Picks |
|---|---|
| **Champion** | **Argentina** (beats Spain in final, 60%) |
| Final | Argentina vs **Spain** |
| SF | **Spain** > France · **Argentina** > Brazil |
| QF | **France** > Netherlands · **Spain** > Belgium · **Brazil** > England · **Argentina** > Colombia |
| R16 | France>Germany · Netherlands>Canada · Brazil>Norway · England>Mexico · Spain>Portugal · Belgium>USA · Argentina>Australia · Colombia>Switzerland |
| R32 | Canada · Germany · Netherlands · Brazil · France · Norway · Mexico · England · USA · Belgium · Portugal · Spain · Switzerland · Argentina · Colombia · Australia |

## Headline numbers

- **P(perfect 31/31) ≈ 8.6e-6 ≈ 1 in 116,000** (upper bound — see note). For
  comparison a flat 70%/game gives 1 in ~62k; we're lower because several R32/R16
  ties are 52–58% coin-flips that drag the product down.
- **E[correct picks] = 18.0 / 31** — the realistic target for the $100k.
- The single biggest perfect-killers are the near-coin-flips: **Mexico/Ecuador
  (52%)**, **Australia/Egypt (53%)**, **Norway/Ivory Coast (58%)**, and the
  all-favourite late rounds (Final 60%, Arg/Bra SF 58%).

## Safest locks (do not deviate)
Argentina R32 91% · England R32 86% · Colombia R32 83% · Argentina QF 82% ·
Spain R32 81% · Netherlands R16 80%.

## Cheapest differentiation (if the field is chalk-heavy)
Flip these first — lowest probability cost per unit of uniqueness:
Mexico↔Ecuador (R32, ~48/52), Australia↔Egypt (R32, ~47/53), Ivory Coast↔Norway
(R32, ~42/58). One contrarian R32 upset costs ~8–15% of E[correct] but can make
an otherwise-shared bracket unique.

## Where the model most disagrees with the Polymarket price
(model > market edge on a chalk pick → the field is likely to *under*-back these,
so leaning into them both raises your bracket's edge and differentiates it):

- **France** deep run — +16 QF / +15.7 SF / +11.4 R16 pts vs market.
- **Belgium** to QF +15.4 · **USA** R16 +14.7 · **Germany** R16 +12.4 · **Brazil** QF/SF +10.

The most differentiated *positive-EV* lean is therefore **France to the
semi-final** (model 33.5% vs market ~ implied lower) and **Belgium over the USA**
in R16 — both are model favourites the market underrates.

## Caveats
- R16+ per-game conditional = P(reach n+1)/P(reach n) is the model's *average-
  opponent* rate; on the all-favourites path the opponent is itself a favourite,
  so the true conditional is a touch lower → **P(perfect) is an upper bound** and
  E[correct] a slight over-estimate. Exact figures need the joint sim
  (`scripts/wca_advancement.py`, currently slow on the dev box).
- **Total-goals tiebreaker** not yet computed — needs the scoreline/xG model
  (`src/wca/scorespage.py`) over the 31 ties; rough knockout avg ≈ 2.4–2.6 g/game.
