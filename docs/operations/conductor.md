# Dev-conductor (v0)

A **dev-only** Telegram bot that fans tasks out to headless Claude Code
agents (**Claude-only** since 2026-06; Codex was removed), **sequential by
default**, GitHub-handled:

```
/task <task>     → dispatch to Claude Code, headless, fresh worktree+branch → PR
/claude <task>   → alias of /task
/status          → per-task table (read from real results, never fabricated)
/cancel <id>     → cancel a not-yet-started task
/help            → usage + runtime warnings
```

This is **infrastructure, not the betting bot.** It never touches live state.

## Pasting screenshots (visual debugging)

Attach a screenshot to a Telegram message and the agent reads it as visual
context — for debugging a UI bug, a stack trace, a chart, etc.

* **Photo + a slash command in the caption** → `/claude debug why this header
  wraps` runs that command with the image attached.
* **Photo + a plain caption** (no slash) → auto-routed as a `/task` so "snap a
  screenshot, describe the bug, send" just works.
* **Photo with no caption** → the bot asks you to add an instruction (a bare
  image isn't actionable).

Send **multiple** screenshots at once (a Telegram album) and they're grouped by
`media_group_id` and attached to a single task; only the captioned member needs
the instruction.

How it flows: the bot downloads the image(s) (`TelegramClient.save_image`,
highest resolution; also accepts an `image/*` document) into
`data/conductor_uploads/`. At run time `runner.stage_images` copies them into the
task's worktree under `.conductor_inbox/` and points the agent's prompt at them
(so the agent's Read tool can open them cwd-relative — no extra permission
grant). Screenshots are debug *input* and never reach a PR, guarded two ways: the
inbox is **deleted before commit**, and `.conductor_inbox/` is added to the
repo's `info/exclude` so `git add -A` can't stage it even on a stale base. The
`data/conductor_uploads/` originals are gitignored and **age-pruned** (>24h) by
the background watcher, so `/retry` can still re-attach a recent screenshot.

## Getting output back (files & charts)

The bot can reply with **files and images**, not just text:

* **Report files auto-attach.** When a task finishes (DONE/PUSHED), any
  report-like files it ADDED/MODIFIED — `.md`, `.csv`, `.txt`, `.png`, `.svg`,
  `.pdf` — are sent back to the chat (images inline, the rest as documents).
  Generated `site/` & `data/` feeds and code files are excluded; capped at 6
  files, read straight from the task branch via git (works after the worktree is
  reclaimed). `/report <id>` re-fetches them on demand.
* **`/chart`** renders the conductor's token spend per task as a PNG. This needs
  `matplotlib` in the venv (an optional extra — `pip install matplotlib`); if it
  isn't installed, `/chart` falls back to the `/usage` text table.

To get a *report* back, phrase the task to WRITE a file (e.g. "...and write the
findings to `docs/reports/x.md`") — the conductor only commits files, so a pure
"explain X" task produces no artifact to attach. Live MODEL charts
(edges/CLV/exposure) belong in `@gamble1_bot`, not here.

## Sequential vs parallel

`max_parallel` defaults to **1 (sequential)**. An earlier 8-way "swarm" raced on
the shared `.git` worktree registry/index and produced collisions, so the safe
default is one task at a time. Opt back into parallelism deliberately with
`WCA_CONDUCTOR_MAX_PARALLEL=N` (or `--max-parallel N`) once the race is fully
ruled out.

## Architecture

```
scripts/wca_conductor.py        Telegram long-poll loop (house TelegramClient)
src/wca/conductor/
  models.py     Engine / TaskStatus enums, TaskRecord, AgentResult, PrResult
  config.py     ConductorConfig — caps, budget, CLI paths, SAFE agent env
  runner.py     per-task pipeline: worktree → agent → commit → push → PR
  manager.py    ConductorManager — thread pool (cap), token budget, status, preflight
  dispatcher.py /task routing: Claude-only (Codex removed 2026-06)
```

Every external command in `runner.py` goes through one `_run` seam, so the
whole pipeline is unit-tested offline (`tests/test_conductor.py`, 24 tests).

## Guardrails (baked in, not bolted on)

| Guard | Where | Effect |
|-------|-------|--------|
| **PR-only** | `runner.create_worktree` / `commit_and_push` | refuse the base branch; each task is a fresh branch in a throwaway worktree. `main` is never committed or pushed. |
| **Max-parallel cap** | `manager` thread pool | hard ceiling on concurrent agents (`--max-parallel`, default 3). |
| **Token budget** | `manager.submit` | optional total-token ceiling; over-budget submissions are **rejected**, not silently queued. |
| **Claude-only routing** | `dispatcher` / `manager.submit_auto` | every `/task` runs on Claude; a logged-out Claude is reported, not silently dropped. |
| **Dry-run env** | `config.agent_env` | every agent runs with `PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, and `POLYMARKET_PRIVATE_KEY` **stripped**. Agent-run code can't touch live money/ledger. |
| **Live-ledger refusal** | `scripts/wca_conductor.py` | the bot exits if `WCA_DB_PATH` resolves to `wca.db`. |
| **Honest reporting** | `runner` / `manager.status_table` | status comes from real agent output + PR results. |

## Runtime prerequisites

v0 is fully built and unit-tested, but to run **end-to-end** a host needs:

1. **`claude` CLI on PATH** — for `/claude`. (The Desktop app bundles its own
   binary off-PATH; install the CLI or set `CLAUDE_BIN=/abs/path`.)
2. **`gh` authenticated** — `gh auth login`. Without it, PRs gracefully fall
   back to a **compare link** (branch is still pushed; you click to open the PR).
3. **`.env.dev`** with a real `TELEGRAM_BOT_TOKEN` for the *dev* bot (a
   separate BotFather bot from the betting bot — they can't share a token).

`/help` and the startup log surface any missing prerequisite via `preflight()`.

## Running

Launch from the **main checkout** (so worktrees land in `.claude/worktrees/`
where `scripts/wca_worktree_cleanup.sh` expects them):

```bash
python scripts/wca_conductor.py --env .env.dev
# smoke test without pushing anything:
python scripts/wca_conductor.py --env .env.dev --no-push
```

Useful flags: `--max-parallel N`, `--token-budget N` (0 = unlimited),
`--base-branch`, `--branch-prefix`, `--no-pr` (push but don't open PRs).
Spawned worktrees accumulate under `.claude/worktrees/`; reclaim them with the
existing `bash scripts/wca_worktree_cleanup.sh --force`.

## Running always-on on the Mac mini

The conductor is a KeepAlive launchd daemon (`com.wca.conductor`) alongside
`@gamble1_bot`, with its **own** dry-run env so it can never touch the live
ledger. On the mini, in the repo root:

```bash
# 1. code: already on main (or: git pull). The 'conductor' daemon is in services.env.
# 2. prerequisites in the venv + on PATH:
.venv/bin/pip install matplotlib          # optional — enables /chart (else text fallback)
#    the mini also needs the `claude` CLI installed and `gh auth login` done.
# 3. config — a SEPARATE env from the live .env (different bot, dry-run, dev DB):
cp .env.conductor.example .env.conductor
$EDITOR .env.conductor                    # @WorldCupDev token, admin id, CLAUDE_CODE_OAUTH_TOKEN
# 4. register + start the daemon (idempotent):
bash deploy/macmini/install.sh
# 5. verify:
launchctl list | grep com.wca.conductor
tail -f logs/conductor.log                # then send /help to @WorldCupDev
```

`.env.conductor` is gitignored. The daemon **crash-loops if it is missing** (no
bot token) — create it *before* `install.sh`. Worktrees accumulate on the mini
too; run `bash scripts/wca_worktree_cleanup.sh --force` periodically (it removes
local checkouts only — pushed branches/PRs are untouched). `autopull` restarts
the daemon when conductor code changes; in-memory task history resets on restart
(known limit).

## Known limits (v0, honest)

- Headless agents are less steerable than interactive — keep tasks well-scoped.
- `/cancel` only stops tasks that **haven't started**; a running agent
  subprocess isn't killed in v0.
- No persistence: task history is in-memory and resets on restart.

## Roadmap

- **v1** — dispatcher shipped (`/task`, Claude-only); still to do: task
  splitting and per-task token caps.
- **v2** — read each PR diff to report accurately; optional auto-merge of green
  PRs; a completeness critic.
