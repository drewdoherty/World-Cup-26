# Bet-builder markets (2026-06-27)

New module `src/wca/models/betbuilder.py` extends the existing prop engines
(`models/props.py`, `models/scorers.py`) to the classic single-fixture
bet-builder surface, plus a CLI (`scripts/wca_betbuilder.py`) and a `/betbuilder`
bot command.

## Markets added

| Market | Subject | Model | Distribution | Source of rate |
|---|---|---|---|---|
| Team total goals | team | Poisson(λ_team) from Dixon-Coles | Poisson | DC model |
| Team total shots | team | base × damped attack scaling | NegBinom | players.db `shots_pm` / prior |
| Team total SoT | team | base × damped attack scaling | NegBinom | players.db `sot_pm` / prior |
| Team total fouls | team | base rate | NegBinom | players.db `fouls_pm` / prior |
| Team total corners | team | `CornersModel` attack-share split | NegBinom | calibrated (WC18+22) |
| Match total cards | match | `CardsModel` × aggression | NegBinom | calibrated (WC18+22) |
| Player SoT | player | sot_p90 × min/90 × attack-context | NegBinom | players.db `sot_p90` / prior |
| Player fouls | player | fouls_p90 × min/90 | NegBinom | players.db `fouls_p90` / prior |
| Player to be booked | player | 1 − exp(−yellow intensity) | Bernoulli | players.db `yellows_p90` / prior |
| Player to score (any/first/2+) | player | re-export of `ScorerPricer` | Poisson thinning | npxg share (override store) |

`player_to_score` is delegated to the already-calibrated `ScorerPricer` rather
than re-implemented.

## Method notes

- **NB parameterisation** matches `props.py`: mean `mu`, dispersion `k`,
  `Var = mu + mu²/k`, `k→∞` is Poisson; over/under on half-integer lines, so
  `P(over L) = 1 − CDF(floor L)`.
- **Attack scaling** (shots/SoT) uses a damped elasticity around the tournament
  base, mirroring `CornersModel`: `mean = base·(1 + ε·(λ_team/λ̄ − 1))`,
  `ε = 0.6` (shots track xG more tightly than corners' 0.30). With ε the mean is
  monotone in λ but does not over-spread across fixtures.
- **Fair odds** are `1/p` (no margin). `price_with_overround()` adds a target
  book margin for display; `ev_vs_offer()` computes EV against a real offered
  price **net of venue commission** (Betfair 2%, Smarkets/Polymarket 0%).

## Calibration provenance & honesty

- Corners/cards constants are the StatsBomb **WC2018+2022** fit already in
  `props.py` (corners 8.97/match, k≈158; cards 3.41/match, k≈6.9).
- Shots/SoT/fouls **team and player priors are order-of-magnitude WC values**
  (`TEAM_PRIORS`, `PLAYER_P90_PRIORS`, `PLAYER_DISPERSION`) pending a refit. They
  are constructor/method arguments so the data pipeline replaces them without
  code changes. **They will be sharp per-player only once `players.db` per-90
  rates are available** (Phase-2 branch; not on `main` yet). Until then the
  player markets run on injected rates or the override store and the booking
  probability is roughly uniform across players (a deliberately modest prior, so
  the fallback never manufactures a confident edge).
- `RateStore` reads `players.db` (`team_rates`, `players`) read-only when present
  and degrades silently to priors otherwise — callers never branch on it.

## Venue reality (why most of these are model-only)

SoT, cards, corners and fouls are **sportsbook-only** — they are not on the
Betfair Exchange, and the project has **no sportsbook odds feed** beyond
TheOddsAPI player-prop markets. So these are published as **model fair odds**
(decision support / a sanity bar against a book's price), not a priced edge. EV
can only be computed where an offered price is supplied (`ev_vs_offer`). Player
SoT *can* be EV'd where TheOddsAPI exposes `player_shots_on_target`. Adding a
sportsbook odds source is the prerequisite to turning these into staked edges.

## How it's wired

- `scripts/wca_betbuilder.py` reads `data/model_predictions.json` (per-fixture
  DC λ already persisted by the card build), prices every market per upcoming
  fixture, and writes `data/betbuilder_latest.{md,json}` (offline, cron-safe).
- `/betbuilder` serves the cached card with the standard provenance/staleness
  banner, mirroring `/next` and `/goalscorers`.

## Validation
- `tests/test_betbuilder.py`: fair-odds consistency, monotonicity in λ,
  minutes proration, fee math, overround, NB/Poisson behaviour, RateStore
  fallback, payload shape (11 tests).
- Spot check (offline run): Salah anytime 3.05 (p=0.33), Lukaku vs NZ 2.04
  (p=0.49), Uruguay (λ0.80) goals O1.5 5.24 vs Spain (λ1.46) 2.33 — all
  directionally sane.
