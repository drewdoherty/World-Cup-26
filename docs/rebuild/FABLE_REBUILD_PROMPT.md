# Fable rebuild prompt — "Football Alpha" (successor to World-Cup-26)

> Paste everything below the line into a fresh Fable session on the new MacBook.
> The legacy repo may be present read-only for reference; the new project is a
> separate, clean repository.

---

You are rebuilding a quantitative football-trading system from scratch on a
single fresh MacBook. The predecessor project (`World-Cup-26`) ran real money
on the 2026 World Cup and is now retired: the tournament is over, players are
back at their clubs, and the system's ideas — but **none of its code
execution** — carry forward.

## Mission

Identify profitable trade opportunities across **all football markets** with
tradeable liquidity by comparing model probability vs market price, on **both
sides**: back +EV YES, and lay −EV positions by taking the NO/complement side.
Always work from the actual **bid/offer on both the YES and NO books** (as the
legacy "market event forest" did), never a single mid price. **CLV remains the
primary KPI**: a market without price capture and closing-line stamping does
not get real money.

Scope at launch: Big-5 European leagues (EPL, La Liga, Serie A, Bundesliga,
Ligue 1), UEFA club competitions (Champions League, Europa League, Conference
League), domestic cups (FA Cup, etc.), and opportunistically anything else
with a liquid market. Club football has a repeating weekly rhythm — the
system must be league-season-native, not tournament-native.

## Hard constraints (non-negotiable)

1. **DO NOT run any legacy World-Cup-26 code.** It is a security risk. You may
   READ the old repo for ideas, constants, prompts, and docs; you may port
   logic by re-implementing it fresh; you must never execute, import, or
   copy-paste-then-run legacy modules, scripts, or notebooks.
2. **One device.** Everything — bot, schedulers, data, ledger, site — runs on
   this MacBook. There is no Mac mini, no remote production box, no SSH split
   between "dev box" and "prod". The canonical SQLite ledger lives locally.
3. **No conductor.** The legacy multi-agent conductor/orchestrator
   (`@WorldCupDev`, `conductor/`, `.env.conductor`) is retired entirely. One
   repo, one bot, human-in-the-loop via Telegram.
4. **Rebuild `.env` from scratch.** No legacy `.env` is reused. Create a fresh
   `.env.example` with only the variables the new system needs (expected set:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_USER_ID`,
   `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIG_TYPE`,
   `PM_DRY_RUN`, `ODDS_API_KEY` or successor odds-feed key, `ANTHROPIC_API_KEY`
   if betslip OCR is kept, plus any new data-feed keys). Ask the human for
   values; never invent them. `PM_DRY_RUN=1` is the default everywhere.
5. **Venues:** Polymarket is the PRIMARY execution venue. Hyperliquid (HIP-3
   sports markets) is the ALTERNATIVE — **read-only at first**: price/CLV
   cross-reference and arb detection only; execution wiring is a later phase
   once Polymarket is solid. GBP bookmakers, Betfair, matched betting, boosts,
   accas, and promo extraction are all OUT of scope — do not rebuild them.
6. **Simplify the structure.** The legacy `src/wca` had ~60 top-level modules
   plus 12 sub-packages. Target roughly:

   ```
   src/fa/
     data/        # ingestion: fixtures, results, odds, Polymarket, Hyperliquid
     models/      # elo, dixon_coles, blend/shrink, montecarlo, eventgrid
     markets/     # devig, kelly, bankroll, selection (human-approved file)
     pm/          # Polymarket CLOB trader, signing, propose, redeem
     hl/          # Hyperliquid read-only client
     ledger/      # bets, fills, CLV stamps, settlement (SQLite)
     bot/         # Telegram @gamble1_bot
     site/        # single static site + JSON feed builders
   ```

   Kill on sight (−EV or tournament-specific dead weight): accumulators,
   boosts/boostlock, promos, matched betting, scorer props for cash, correct
   score for cash, the Vercel remnants, Notion sync, the testbook/predledger
   duplication (one paper ledger only), the intel poller unless it earns its
   place, and the 48-team World Cup simulator (replace per §"Models").

## Sizing & risk (carry forward, restated)

- ONE combined bankroll, starting figure supplied by the human at setup
  (same rule as before: start ± total realised P&L), **¼-Kelly** of the
  running total, sized whole-book with a hard worst-case cash floor.
- Static fail-closed execution caps as code constants in the trader
  (per-order USD, rolling-daily USD, cash-out per order); changing them is a
  human-approved code change, never a config default.
- Selection rules live in ONE human-approved module (port the legacy
  `selection.py` idea): bucket by model prob (moneyline ≥0.50 / mid
  0.25–0.50 / longshot <0.25), higher bucket always outranks lower, EV breaks
  ties; **no cash on model <0.25 longshots** (display dimmed, stake zero).
  Re-validate the legacy "futures further-out first, match markets
  hours-neutral" secondary key against club-season data before keeping it.
- Human-confirm execution flow: model proposes → order parks → Telegram
  `Y PM-<n>` fires (batch confirm supported). `PM_DRY_RUN` gates everything;
  going live is an explicit human act. Model changes ship shadow-first
  (dual-written, CLV-compared) before touching sizing.

## Data overhaul (§3)

The legacy system was international-football-native (martj42
international_results, StatsBomb WC open data, The Odds API, a 2–3-day-lagged
results.csv). That is insufficient for club football. Rebuild the data layer:

- **Results/fixtures:** evaluate and choose from football-data.co.uk
  (free, Big-5 history + closing odds), OpenFootball, API-Football or
  football-data.org (paid tiers) for live fixtures/results across leagues AND
  cup competitions. A modest paid budget (~£50–150/mo) is approved if it
  materially improves accuracy/latency — justify the choice with a short
  written comparison before subscribing.
- **Ratings:** ClubElo (free) as the Elo backbone for club sides; consider
  seeding Dixon-Coles from multiple seasons with time decay.
- **xG / player data:** StatsBomb open data where it covers needs; FBref/
  Understat-style xG only via terms-compliant access. Player props are NOT a
  launch market — collect the data only when a props market is actually
  planned.
- **Market data:** Polymarket Gamma + CLOB (order books, both sides, price
  history) and Hyperliquid's public API. Persist odds/book snapshots durably
  from day one — closing-line capture is the CLV backbone.
- **Accuracy gate:** before any real money, run a reconciliation pass proving
  the chosen feeds agree with ground truth (scores, kickoff times, team-name
  mapping across sources) over ≥ one full recent season. No fabricated
  numbers, ever: every reported figure comes from a computation actually run,
  with n stated.

## Models — the "under the hood" map (§4)

Borrow these legacy ideas, re-implemented fresh, and FIX the consistency
problem: the old system had multiple surfaces independently recomputing
probabilities from different odds snapshots (e.g. `advancement.py` re-derived
1X2 from live books while `/card` used its own blend — the two could
disagree). The new architecture rule is:

> **One probability store.** A single pricing pass per cycle produces the
> canonical per-fixture 1X2 + scoreline grid (and per-competition Monte Carlo
> outputs), stamped with snapshot IDs. Every downstream surface — trade recs,
> event forest, advancement/outright pages, bot commands, sizing — reads from
> that store. Nothing recomputes probabilities independently.

Components:

1. **Elo (ClubElo-seeded) + Dixon-Coles** (time-decayed, low-score corrected)
   → per-fixture scoreline grid → 1X2, totals, BTTS, exact score.
2. **De-vig (Shin) + market blend + shrink-to-market.** The legacy backtests
   showed the raw model rarely beat the de-vigged market (blend weights ended
   0.10/0.30/0.60 Elo/DC/market; shrink `p' = p_mkt + k(p_model − p_mkt)`,
   k=0.5 ≥0.25 / k=0.25 below, renormalised). Keep this humility: the shrunk
   line is the live line; raw kept alongside for scoring; kill-switch env var.
   Re-fit weights on club data with a proper backtest before going live.
3. **Competition progression Monte Carlo** — the successor to the World Cup
   sim: a generic engine that takes a competition structure (league table +
   remaining fixtures; or knockout bracket with two-leg/away-rules/ET+pens
   settlement) and simulates forward from the canonical fixture grids →
   title/top-4/relegation/advancement probabilities for futures markets.
   Must handle UCL/UEL league-phase + knockout, and domestic cup brackets.
   90-minute vs to-advance settlement is flagged on every surface and never
   visually confusable.
4. **Event-market grid ("forest") engine** — classify live Polymarket event
   markets, price each from the canonical scoreline grid, compare model vs
   the actual best bid/offer on BOTH the YES and NO side, and emit
   BACK-YES / BACK-NO (lay) signals with net-of-fees edge. A missing price
   is shown as "EV?" — never dressed up as +EV.
5. **Scoreline reconciliation:** the scoreline matrix is reconciled to exactly
   the canonical 1X2 so no surface ever contradicts another.
6. **Validation harness:** Brier/log-loss scoreboard raw→shrunk→market,
   shadow-mode dual-writes for every model change, and statistical CLV gates
   (sequential test) before anything graduates to live sizing.

## Bot (keep the structure)

Keep @gamble1_bot with the legacy shape: long-polling Telegram loop, chat-ID
gated, admin-gated money actions, launchd `KeepAlive` supervision. Port these
commands (trimmed to the new scope): `/summary`, `/bets` (wire names stay for
compatibility; display copy says "trades"), `/clv`, `/card` (today's trade
card), `/next`, `/matchevents` (forest picks), `/scores`, `/today`, `/pm`
(parked orders + trader status + daily spend), `/settle`, `/restart`, `/ping`,
`/help`, and the `Y/N PM-<n>` confirm grammar. Drop: `/accas`, `/boost`,
`/goalscorers`, `/betbuilder` (out-of-scope markets). Betslip-screenshot OCR
is optional — keep only if bookmaker slips remain relevant (they shouldn't).
Display conventions carry forward: percentages everywhere (Polymarket ¢ is a
percent), explicit ✅+EV / ❌−EV / EV? markers, bucket tags, and
"trade/trades" wording in all user-visible copy.

## One site (§5) — consolidation, not deletion

Replace the two localhost sites (8000 + 8001) with ONE static site on one
port, stdlib server, fewer pages. All legacy content groups were reviewed and
kept, but merged:

1. **Trades** — successor to `tracking.html`: open positions, stakes,
   max win/loss, realised P&L, CLV per trade, settlement state. Better bet
   tracking is an explicit goal: one ledger, every trade stamped with model
   prob, taken price, close price, CLV, and outcome.
2. **Trade Recs** — successor to `arb.html`/`bet_recs.json`: ranked live
   recommendations with bucket tags, EV markers, and park/confirm status.
3. **Forest** — the event-market model-vs-market view, now showing YES and NO
   best bid/offer per outcome with back/lay signals.
4. **Futures** — Monte Carlo competition outputs (title/top-4/relegation/
   advancement) vs market, merging the old advancement + visuals pages.
5. **Model health** — merge the old analytics site's Verdict, Model CLV, and
   Paper Ledger tabs plus benchmarks and the shadow scoreboard into one page.
6. **Risk & flow** — merge exposure/correlation, line-move, and the useful
   microstructure feeds (liquidity, order flow) into one page; discard the
   microstructure sub-pages that never drove a decision.

Localhost-only, no hosted deploys, published by a local scheduled job.

## Build order

Phase 0: repo scaffold, `.env.example`, ledger schema, fresh Polymarket
read-only client. Phase 1: data layer + reconciliation gate. Phase 2: models
+ canonical probability store + paper trading with CLV stamping. Phase 3: bot
+ site. Phase 4: parked-order execution behind `PM_DRY_RUN` + human confirm.
Phase 5: live, small, with the caps. Hyperliquid read-only lands in Phase 2
as a comparison feed. Tests green locally before every push; ask the human
before anything irreversible or money-touching.

Start by proposing the ledger schema and the data-source comparison, and ask
any questions you need answered before Phase 0.
