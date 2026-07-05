# Fable plan — 2026-07-05 (open work queue: decisions + execution plan)

Audience: whoever (human or agent) picks up the open work queue next.
Standing rules live in `CLAUDE.md` (repo root) — read that first. This file
records the owner decisions made on 2026-07-05 against
`docs/HANDOFF_2026-07-03.md` §4's ranked queue, what got built same-day, and
a self-contained execution plan for what's left. Where this file and reality
diverge, re-verify against `origin/main` before acting.

---

## 1. What shipped today (verify against `git log` before trusting this)

- **P2 — settler freshness gate.** PR #166, merged. The default results
  source was already fresh (repointed to `martj42_cleaned.csv` back in PR
  #57 — the handoff's premise was stale). What was actually missing was a
  fail-closed freshness check: `wca_ledger_audit.py` now refuses to settle
  off a source >24h old (`--max-age-hours`, `--skip-freshness-check` escape
  hatch). Tests added. **Open follow-up**: the 24h default was never checked
  against the real martj42_cleaned refresh cadence on quiet/non-matchday
  days — confirm it doesn't false-positive before relying on it unattended.
- **P4 — `prop_calibration.json` generator.** PR #167, merged. New
  `scripts/wca_prop_calibration.py` wires `CornersModel`/`CardsModel`
  (90-min-refit constants) + player-pipeline priors into the file
  `wca_betrecs.py` has always expected but nothing ever produced. Display
  calibration only — `build_event_props` still correctly withholds these
  from cash (no live book-price feed exists for corners/cards). launchd
  `propcal` job config added but **NOT activated** — needs a human to run
  `bash deploy/macmini/install.sh` on the mini.
- **P5 (partial) — watchdog git-behind alert.** PR pending review (branch
  `feat/watchdog-git-behind-alert`) — extends `com.wca.watchdog` to detect
  and alert when the mini falls behind `origin/main` by more than ~10 min,
  reusing the existing admin-alert path. Scoped deliberately narrow: this
  does NOT touch the daemon-artifact-untracking question (§3 below).
- **P6 (partial) — totals→λ prior, SHADOW ONLY.** PR pending review (branch
  `feat/totals-lambda-prior-shadow`) — de-vigs the OddsAPI totals O/U quote
  into an implied goal expectation, blends with the model's own DC lambda,
  logs both side-by-side. **Not wired into live pricing/sizing** — that
  graduation is a separate decision gated on an out-of-sample CLV
  comparison, per CLAUDE.md's shadow-first rule.

**Process note, read before repeating this pattern**: the P4 agent was
authorized to self-merge under narrow conditions (display-only, no
money-movement path, green suite) and did so — flagged by the harness as a
self-merge-without-review pattern worth naming explicitly even though the
diff checked out clean on manual review after the fact. Every other PR this
round was deliberately left open for a human to merge. **Default to that**:
on this repo, code that touches settlement, sizing, execution, or the
model's pricing path should not be self-merged by an agent, full stop.

## 2. Owner decisions on record (2026-07-05)

| Item | Decision | Why |
|---|---|---|
| P1 — PM execution path for the mini | **Deferred.** Keep the manual MacBook+VPN workflow. | No architecture chosen yet among the 3 scoped options (proxy / relay / network-route); revisit properly post-tournament, not under match-day time pressure. |
| P5 — `feat/site-lilac-forest` + `feat/site-ops-overhaul` | **Fold into the Phase-1 site-consolidation design**, then close both. | Both do real, overlapping site work; owner wants the ideas kept, not two competing rewrites left to rot. **Not yet executed** — this is a mining/design pass, not a same-day build (see §3). |
| P5 — untrack daemon artifacts + off-box ledger replication | **Do both.** | Kills the autostash-clobber bug at the root (bitten this session already) and reduces the mini being a single point of failure for the canonical ledger. **Not yet executed** — both are real data-pipeline architecture changes with live-system blast radius; see §3 for why they need a design pass first rather than a blind agent build. |
| P6 — quant ladder priority | **Totals→λ prior first**, shadow-only (shipped today, pending review). | Cheapest to build; the underlying totals/BTTS data is already paid for via TheOddsAPI and was completely unused. |

## 3. Scoped but deliberately NOT built today — needs a design pass first

These three are real, greenlit work — they were held back from an
unsupervised agent build specifically because each one changes how live data
moves through a real-money system, and a wrong first cut is expensive to
unwind mid-tournament.

### 3a. Untrack daemon-written artifacts from git

The mini's `git pull --rebase --autostash` restores locally-dirty
daemon-written files (site JSON, card `.md`, `model_predictions_log.jsonl`)
over freshly-pulled values — the root cause the custom `merge=freshest` git
driver (`.gitattributes`, PR #162/#163) currently papers over. The clean fix
is to stop tracking these files in git entirely. But: **the mini's autopull
IS the current publish mechanism** for these files — untrack them and
something else has to carry fresh data from wherever it's built (MacBook or
a CI runner) to the mini and to the public site. Before writing code,
whoever picks this up needs to answer: what replaces `git pull` as the
transport for these specific files (rsync/scp on a timer? the already-parked
Turso cloud-publish path from PR #127? something else)? Do NOT start
migrating files off git tracking without that transport already working
end-to-end in shadow, or the site goes dark mid-tournament.

### 3b. Off-box ledger replication

`data/wca.db` (the canonical ledger) lives only on the mini. Reducing that
single-point-of-failure risk means picking a replication target (Turso,
scheduled encrypted backup to a second box, S3-style snapshot) and a
consistency model (the ledger is written by multiple daemons — replication
must not race writes or drift the ledger a human might act on). `com.wca.backup`
already exists (15-min local backup cadence) — read what it currently does
before assuming this needs to be built from scratch; it may only need an
off-box destination bolted onto an existing mechanism.

### 3c. Site consolidation (site-lilac-forest / site-ops-overhaul)

Both branches contain real, non-overlapping-in-intent site work (a dashboard
prototype and a declutter+Turso-publish overhaul respectively — see PR #127
body for the site-ops-overhaul side's own honest gap list, e.g. the
Risk/Blind-Spots panel still missing $1,655 of live PM futures exposure).
Mining these into one Phase-1 site design means actually reading both diffs
end to end and deciding what survives — not a mechanical merge (300+ files
between them, some data-blob churn). A good next `/task`: "read both
branches' diffs against current main, produce a single consolidated site
design doc listing what to keep/drop/redo from each, then close both."

## 4. Still fully open (untouched today)

- **P1** — see §2, deferred by owner decision.
- **P3** — `pm_ideas.json` production, blocked entirely on P1.
- **P6 remainder** — sharp-book weighting, F7 graduation on OOS CLV (needs
  the CLV comparison to actually run and clear), CLOB tick-capture daemon,
  ET/pens goal-rate model, full-slate prediction ledger. Ranked by
  backtested ROI in `docs/research/` — work down that ranking one `/task`
  at a time, reporting back after each rather than batching.
- **P7** — goal-timestamp fusion for orderflow jump-detection. Blocked on a
  goal-minute feed; the analyst-CSV pattern already used for the
  player-events pipeline is judged an acceptable source if someone wants to
  unblock it rather than wait for something better.

## 5. Live desk / real-money watch-items carried over (not code, don't forget)

- Possible stale 8-leg advancement batch may still sit in `pm_parked` —
  never approve with a bare `Y`; re-propose on fresh prices first.
- The $868 cash placement + position-trim task from an earlier session was
  interrupted mid-investigation and never resumed. Re-verify the current
  cash/position state before picking it back up — it will have moved.
- France futures ladder, Portugal smart-wallet divergence, and the France
  advancement market-vs-model divergence (~35% vs ~18%) all need a fresh
  re-quote before anyone acts on the numbers quoted in the 07-03 handoff —
  those are now stale by definition.

## 6. How to hand any of §3/§4 to the conductor

Same convention as the 07-03 handoff — one self-contained `/task` per item,
always citing this file:

```
/task Read docs/FABLE_PLAN_2026-07-05.md §3a. Design (don't yet build) the
replacement transport for daemon-written site/data JSON once they're
untracked from git — evaluate rsync-on-a-timer vs the parked Turso
cloud-publish path from PR #127. Produce a one-page design doc + a shadow
proof-of-concept for the chosen path BEFORE touching .gitignore or removing
anything from git tracking. One PR (design doc + shadow POC only).
```
