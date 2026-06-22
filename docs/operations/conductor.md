# Dev-conductor (v0)

A **dev-only** Telegram bot that fans tasks out to headless coding agents.
One interface, both models, **sequential by default**, GitHub-handled:

```
/task <task>     → automatic routing, Claude-first; Codex only for small mechanical edits
/claude <task>   → Claude Code, headless, in a fresh worktree+branch → PR
/codex  <task>   → Codex, headless, same flow
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
  dispatcher.py automatic /task routing: Claude-first, Codex-scarce
```

Every external command in `runner.py` goes through one `_run` seam, so the
whole pipeline is unit-tested offline (`tests/test_conductor.py`, 24 tests).

## Guardrails (baked in, not bolted on)

| Guard | Where | Effect |
|-------|-------|--------|
| **PR-only** | `runner.create_worktree` / `commit_and_push` | refuse the base branch; each task is a fresh branch in a throwaway worktree. `main` is never committed or pushed. |
| **Max-parallel cap** | `manager` thread pool | hard ceiling on concurrent agents (`--max-parallel`, default 3). |
| **Token budget** | `manager.submit` | optional total-token ceiling; over-budget submissions are **rejected**, not silently queued. |
| **Codex conservation** | `dispatcher` / `manager.submit_auto` | `/task` sends background/high-context work to Claude by default; Codex auto-routing is capped (`WCA_CONDUCTOR_CODEX_AUTO_LIMIT`, default 1). |
| **Dry-run env** | `config.agent_env` | every agent runs with `PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, and `POLYMARKET_PRIVATE_KEY` **stripped**. Agent-run code can't touch live money/ledger. |
| **Live-ledger refusal** | `scripts/wca_conductor.py` | the bot exits if `WCA_DB_PATH` resolves to `wca.db`. |
| **Honest reporting** | `runner` / `manager.status_table` | status comes from real agent output + PR results. |

## Runtime prerequisites

v0 is fully built and unit-tested, but to run **end-to-end** a host needs:

1. **`claude` CLI on PATH** — for `/claude`. (The Desktop app bundles its own
   binary off-PATH; install the CLI or set `CLAUDE_BIN=/abs/path`.)
2. **`codex` CLI on PATH** — for `/codex`. Set `CODEX_BIN=/abs/path` if needed.
3. **`gh` authenticated** — `gh auth login`. Without it, PRs gracefully fall
   back to a **compare link** (branch is still pushed; you click to open the PR).
4. **`.env.dev`** with a real `TELEGRAM_BOT_TOKEN` for the *dev* bot (a
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
Set `WCA_CONDUCTOR_CODEX_AUTO_LIMIT=0` to make `/task` Claude-only while
keeping explicit `/codex` available for manual overrides.

Spawned worktrees accumulate under `.claude/worktrees/`; reclaim them with the
existing `bash scripts/wca_worktree_cleanup.sh --force`.

## Known limits (v0, honest)

- Headless agents are less steerable than interactive — keep tasks well-scoped.
- `/cancel` only stops tasks that **haven't started**; a running agent
  subprocess isn't killed in v0.
- Codex token accounting is best-effort (its stdout isn't structured like
  `claude --output-format json`); `/status` may show 0 tokens for Codex.
- No persistence: task history is in-memory and resets on restart.

## Roadmap

- **v1** — dispatcher is partially shipped (`/task`, Claude-first Codex
  conservation); still to do: task splitting and per-task token caps.
- **v2** — read each PR diff to report accurately; optional auto-merge of green
  PRs; a completeness critic.
