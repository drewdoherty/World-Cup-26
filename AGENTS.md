# AGENTS.md - Codex operating guide

World Cup Alpha is a real-money football trading system. Codex is the primary
interactive development agent for this repository. Read this file first, then
`README.md`, `docs/CURRENT_STATE.md`, and `docs/OPERATIONS.md` before changing
anything that can affect production, pricing, staking, feeds, or settlement.

`CLAUDE.md` remains as compatibility context for the separate Telegram
conductor. It is not the Codex runbook. When old handoffs or agent-specific
notes disagree with this file and the current code, verify the code and update
the owned current docs rather than carrying the old claim forward.

## Safety invariants

1. **Mac mini is production.** Its checkout is `~/World-Cup-26`; its
   `data/wca.db` is the canonical real-money ledger. The MacBook checkout and
   local databases are development copies.
2. **Never arm live execution from the MacBook.** Force `PM_DRY_RUN=1`, use
   `WCA_DB_PATH=data/dev.db`, and remove signing keys from any spawned-agent
   environment. Do not infer safety from an unchecked `.env` file.
3. **Human confirmation remains mandatory.** Polymarket proposals are parked;
   a human reviews `/pm` and confirms `Y PM-<n>`. Hyperliquid is research-only
   and has no execution path.
4. **Do not mutate the mini ledger during diagnosis.** Read it over SSH with
   Python `sqlite3`, immediately set `PRAGMA query_only=ON`, and keep the
   connection read-only. Back up before any human-approved repair.
5. **Never expose secrets.** Do not print, paste, commit, or document private
   keys, bot tokens, OAuth tokens, API credentials, or full `.env` contents.
6. **Do not use stale position advice.** Re-read the canonical ledger and live
   venue prices before discussing or taking any money action. Documentation
   must not preserve old trim, sell, hedge, or cash-placement instructions.
7. **Settlement bases are part of the contract.** A 90-minute 1X2 leg is not
   interchangeable with an ET-and-penalties advancement leg. Cancellation,
   postponement, deadline, and half-void clauses must also match before any
   cross-venue result can be called an arbitrage.

## Protected behavior

Treat these as human-approved-change surfaces:

- `src/wca/selection.py`: probability buckets, longshot cash floor, and
  match-versus-futures ordering.
- `src/wca/card.py` and `src/wca/markets/bankroll.py`: combined bankroll,
  foreign-exchange conversion, Kelly sizing, and whole-book limits.
- `src/wca/pm/trader.py` and `scripts/wca_pm_fire.py`: order, daily, cash-out,
  and fire caps plus dry-run enforcement.
- `src/wca/ledger/store.py`: canonical money-ledger writes and settlement.

The current selection rule is model probability first: moneyline `>= 0.50`,
mid `>= 0.25`, longshot `< 0.25`. Longshots receive no cash. Match markets use
EV within a probability bucket; further-out-first is retained only for
multi-week futures and advancement markets.

## Working rules

- Start by checking the branch and dirty state. Other agents and scheduled
  builders may be editing the same checkout; never revert changes you did not
  make.
- Use a `codex/<topic>` branch for code work. Do not commit directly to
  `main`, self-merge, or push generated branch data over fresher `origin/main`
  artifacts.
- Keep one coherent feature per branch. Serialize work that touches shared
  surfaces such as `src/wca/bot/app.py`, `src/wca/card.py`,
  `scripts/wca_build_card.py`, or deployment scripts.
- Prefer the existing source-of-truth modules over new parallel logic. Add an
  abstraction only when it removes real duplication or establishes a tested
  safety boundary.
- Use structured parsers for JSON, CSV, SQLite, and API payloads. Never invent
  a number to fill a missing field; preserve `null`/unknown and explain why.
- Run focused tests while working and `./.venv/bin/pytest -q` before a code
  push. Local green is the real gate even when CI is advisory or absent.
- Verify the current branch immediately before committing. The Telegram
  conductor and other sessions can create or switch worktrees independently.
- Generated `site/*.json` and `data/*_latest.md` files are shared operational
  artifacts. Use the installed `merge=freshest` driver and avoid hand-merging
  generated JSON.

## Research boundaries

- The **event forest** is the complete observable PM match-event universe.
  Priceable and unpriceable rows both remain visible, with model provenance and
  settlement basis attached. Its trade-rec subset is governed separately.
- The **shadow book** uses only `data/shadow_book.db`; all stakes and P&L there
  are simulated. It records observations, entries, and abstentions and cannot
  sign or submit an order.
- Hyperliquid/Polymarket dominance bounds are generic settlement-aware research.
  Positive zero-fee margins remain `CANDIDATE_FEE_UNVERIFIED` until the HL
  settlement fee and every settlement tail are verified.

## Production topology

- MacBook: development, local dashboards, PM access through NordVPN, and the
  PM/HL public-data gateway.
- Mac mini: production daemons, Telegram bots, canonical ledger, backups, and
  primary site publisher.
- GitHub `main`: code deploy bus and tracked-feed transport. The mini autopulls
  every five minutes; new launchd definitions still require a human to run
  `bash deploy/macmini/install.sh` on the mini.
- Sites are localhost-only. There is no supported hosted deployment.

Operational commands, recovery steps, and verified caveats live in
`docs/OPERATIONS.md`. The dated tournament snapshot lives in
`docs/CURRENT_STATE.md`; update that file instead of adding live state here.
