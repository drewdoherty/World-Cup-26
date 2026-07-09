# World Cup Alpha — standing rules for AI sessions

Real money is live on this system. Read this before changing anything.
Where this file and older docs disagree, this file + `ARCHITECTURE.md` win.

## Goal & spec

Quant betting on the 2026 World Cup (football ONLY). PRIMARY: advancement /
knockout futures on Polymarket. SECONDARY: maximum extraction from boosts and
returning-customer offers (matched or high-EV unmatched). Model = Elo +
Dixon-Coles + de-vigged market blend; **CLV is the primary KPI** — a market
without price capture + CLV stamping does not get real money.

## Sizing (single source of truth)

**ONE combined bankroll: £3,000 ± TOTAL realised P&L across GBP books AND
Polymarket** ($↔£ at the fixed $1.33/£), **¼-Kelly of the running total**,
expressed in £ for GBP venues and $ for PM (same pot, FX'd — never per-venue
£3,000 each; that double-counts). Implemented in `wca.markets.bankroll` +
`card.full_pools()` / `resolve_pool_bankroll()` (`WCA_FULL_POOLS=0` restores
the legacy split; the CLV rung ladder is reference-only). Execution caps are
static fail-closed constants (`pm/trader.py`: $160/order, $1,000/day, $400
cash-out; fire backstop $200) — changing them is a human-approved code change.

## Selection rules (encoded, do not regress)

**The rule lives in ONE place: `src/wca/selection.py`.** Every bet-ranking /
selection / sizing surface imports it (`bucket_rank`, `longshot_no_cash`,
`hours_out`, `preference_sort_key`); it is a **human-approved-change file** like
the execution caps — editing `PROB_BUCKETS` / `LONGSHOT_PROB` /
`preference_sort_key` moves ALL real-money orderings at once. Full spec + the
per-surface compliance table: `docs/SELECTION_RULES.md`.

Canonical rule (user-confirmed 2026-07-07), key `(bucket_rank, -hours_out, -ev)`:
- **Bucket by MODEL prob (PRIMARY)**: moneyline `≥0.50` / mid `0.25–0.50` /
  longshot `<0.25` (inclusive lower bounds). A higher bucket ALWAYS ranks above
  a lower one, regardless of EV.
- **Further-out fixtures first (SECONDARY)**: raw continuous hours-to-kickoff,
  descending (thin early markets are more likely mispriced) — never bucketed.
- **EV breaks ties ONLY (tertiary)**, within the same bucket + further-out tier.
- **No cash on longshots (`<0.25` model)**: strict floor — free-bet/lottery only
  (stake forced to 0, flagged, may still be DISPLAYED dimmed). `longshot_no_cash`
  is applied at the SIZING step, kept SEPARATE from the sort so a surface can
  show a longshot dimmed while sizing it at zero.
- **REPLACE ruling (2026-07-07)**: "longshot" is now defined PURELY by model
  prob `<0.25`. This RETIRES the older 2026-06-29 "cut all market
  outright-underdogs regardless of prob" decision — a market outsider the model
  rates 25–49% is now a STAKEABLE MID. The market-relative FAV/2ND-FAV/longshot
  categories (`card.classify_outcome` / `_CATEGORY_PRIORITY`) survive ONLY as
  cosmetic DISPLAY labels; they no longer feed the sort key or the cash-cut.
- Killed as −EV leaks (do NOT resurrect for cash): correct score, scorer
  props, un-boosted SGMs. Boost-hedged SGMs are different: `wca.boostlock`.
- Whole-book: size ALL bets together; worst case respects the hard cash floor.

## Display conventions (user-chosen, do not "improve")

- `/card`: classic format — decimal odds, `model % / mkt %`, `[elo/dc]`,
  stake in the pick's own pool currency, verbose CUT reasons, scorelines
  appendix. NO bankroll-model footer.
- `/pm` + Action Desk trade ideas: Polymarket ¢ convention, $ stakes,
  bucket-grouped (moneylines → mid → longshots), hours-out tags
  (`site/pm_ideas.json` feed).
- 1X2 settles at 90 minutes; PM advancement includes ET+pens — the two must
  never be visually confusable; settlement basis is flagged on every surface.

## Live-money gates

- `PM_DRY_RUN` gates PM execution (mini `.env` = LIVE). Never arm live from
  the dev box. Proposals park in `pm_parked`; a human `Y PM-<n>` fires
  (single Y can fire a tagged BATCH — check `/pm` first).
- Model changes ship SHADOW-FIRST (dual-written, CLV-compared) before they
  touch pricing or sizing. F7 goal-blend is in shadow now (`gb_lambda_*`).
- New markets need: price capture, CLV stamping, settlement automation —
  before real money.

## Data discipline

- "What's live" = `origin/main`. The canonical ledger `data/wca.db` lives
  ONLY on the Mac mini — never mutate it from the dev box; read it over SSH
  with `sqlite3.connect` + `PRAGMA query_only=ON` (`-readonly` fails, WAL).
- Never commit stale branch data over CI-fresh data (`site/*.json`,
  `data/*_latest.md` are daemon/CI-written).
- `data/raw/results.csv` LAGS 2–3 days — don't treat absence as "not played".
- No fabricated numbers, ever: every reported figure comes from a computation
  actually run, with n stated; unverifiable claims are labelled.

## Ops

- Mini = production (ssh `andrewdoherty@Drews-Mac-mini.local` — the .55 IP is
  stale), repo `~/World-Cup-26`, launchd `com.wca.*`. Deploy = merge to main
  → autopull (5 min) or manual pull; **new launchd jobs need
  `bash deploy/macmini/install.sh` run on the mini by a human**.
- Unwedge recipe: back up dirty tracked files FIRST (tar), then
  `git reset --hard origin/main`, then kickstart. Agents never run destructive
  recovery unprompted.
- The conductor may switch this repo's branch mid-session: verify the branch
  before every commit; use scratchpad worktrees for multi-step builds
  (venv resolves `wca` to the main repo — set `PYTHONPATH=<worktree>/src`).
- Tests: `./.venv/bin/pytest -q` must be green before push; the CI gate is
  advisory-only (free-plan private repo), so local green is the real gate.
- Telegram: ops/trades via @gamble1_bot; code via @WorldCupDev conductor
  (never places bets). Progress pings go to the TELEGRAM_CHAT_ID chat.

## Session handoffs

Current state-of-play + ranked work queue: `docs/HANDOFF_2026-07-03.md`
(branch map, environment topology incl. the PM network block + VPN/LAN
trade-off, live watch-items, /task templates). Conductor agents: read it
before starting any task.

## Standing decisions

- Betfair execution: NO-BUILD (ADR-003). Read-only CLV reference at most;
  Smarkets first if a GBP exchange is ever needed.
- Sites: localhost-ONLY — `site/` (8000) primary, `site-analytics/` (8001)
  frozen pending consolidation (post-tournament); publish via the mini
  `publish` job. Vercel REMOVED entirely 2026-07-08 (no hosted deploys).
- Overhaul plan + gates: `docs/overhaul/PHASE1_DESIGN.md` (tournament track
  vs post-tournament track); rollback tag `pre-overhaul-2026-07-01`.
