# AGENTS.md — World Cup Alpha dev-conductor operating guide

This repo is worked by a **swarm**: the in-repo dev-conductor
(`src/wca/conductor/`, driven from the `@worldcupdevbot` Telegram chat) fans
each `/task` out to a headless `claude`/`codex` agent. Every task gets its own
git **worktree + branch off `main`**, runs the agent, commits, pushes, and
opens a PR. The conductor is **PR-only and dry-run** — it never commits `main`
and never places live bets (`PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, the
Polymarket key is stripped from the agent env).

This file is the contract that keeps the swarm from colliding with itself. It
was written after a run where 12 tasks produced 11 orphan branches, two
duplicate `/accas` implementations, two duplicate `/goalscorers` fixes, and one
hard failure — all from process gaps, not bad code.

## 1. Root causes this guide closes

| Symptom in `/status` | Real cause | Fix |
|---|---|---|
| every task "pushed — gh CLI not found" | conductor launched with a minimal PATH (launchd/cron/GUI) lacking `~/.local/bin`; `gh`/`claude`/`codex` are installed but unresolved | PATH is augmented for every agent + PR call (`config.agent_env`, `config.resolve_bin`); PR creation now has a REST-API fallback (`runner._open_pr_via_api`, `scripts/gh_pr.sh`) |
| task #5 FAILED: `unknown option '- send a message...'` | prompt starting with `-`/`--` passed as a CLI positional → parsed as a flag | prompt handed **off-argv** (claude: stdin; codex: after `--`) in `runner.run_agent` |
| `/accas` done twice (#4, #7), `/goalscorers` twice (#8, #12) | two tasks for the same feature, each branched off `main` independently → divergent, conflicting impls | one-feature-per-task + duplicate pre-flight (`manager.find_active_duplicate`) + integration-branch rule (below) |
| #2 produced nothing; #3 "paste output of #2" produced nothing | tasks are **isolated** agents with no shared memory; #3 referenced #2's output | the "tasks are isolated" rule (below) |
| everything serialized; "codex unavailable / cap 1" | `codex_auto_limit=1` is a *conservation* cap, and Codex was logged out | routing guidance (below) |

## 2. Operating rules (the swarm MUST follow these)

1. **One feature per task.** A task changes one command/subsystem. Do not file
   two tasks that touch the same files; if a feature needs follow-up, it is a
   second commit on the *same* branch, not a new task off `main`.
2. **Check for duplicates before dispatch.** The bot calls
   `ConductorManager.find_active_duplicate(task)` and refuses/queues a task whose
   slug matches an already-active one. If you must re-run, cancel the original
   first.
3. **Tasks are isolated — no cross-task references.** An agent sees only its
   prompt and the repo. Never write a task like "paste the output of #2" or
   "continue the previous task": there is no shared memory. Put everything the
   agent needs *in the prompt*.
4. **Branch naming is fixed:** `conductor/<engine>-<slug>-<shortid>` (set in
   `manager._new_record_locked`). Don't hand-name branches that collide.
5. **Never start a prompt with `-` or `--`.** It is handled now, but keep
   prompts as plain imperative text; lead with a verb, not a bullet.
6. **Integration over parallel-merge.** When several related branches exist, do
   NOT merge them pairwise into `main`. Create ONE `integrate/<topic>` branch off
   `main`, merge/cherry-pick the chosen branches into it, resolve conflicts once,
   get tests green, open one PR. Pick the *better* of any duplicate pair (see the
   branch audit) — never both.
7. **Pre-flight file-overlap check.** Before dispatching a batch, list the files
   each task is likely to touch. Two tasks touching the same file
   (`src/wca/accas.py`, `src/wca/bot/app.py`, `scripts/wca_build_card.py`) must be
   serialized or merged into one task.
8. **Every task must leave the tree green.** Run `pytest -q` in the worktree
   before push; a task that reds the suite is FAILED, not PUSHED.

## 3. Routing & concurrency

- **Claude-first** is the default route (background / high-context / model work).
  **Codex** is the scarce route for small mechanical edits, capped by
  `codex_auto_limit` (default 1) — a *conservation* cap, not an availability
  gate. When Codex is logged out/unavailable the router falls back to Claude
  automatically (`manager.submit_auto`).
- Raise throughput with `WCA_CONDUCTOR_MAX_PARALLEL` (default 3) and, if Codex is
  healthy and you want more of it, `WCA_CONDUCTOR_CODEX_AUTO_LIMIT`.
- Disable a dead engine explicitly with `WCA_CONDUCTOR_DISABLED_ENGINES=codex`
  so the router stops probing it.

## 4. Conductor-app setup (do this once, at the app/launch level)

The conductor process must be launched with a login-shell environment (or an
explicit PATH) so installed CLIs resolve. Verify on the host:

```
gh --version && gh auth status      # gh installed + authed (scopes: repo, workflow)
claude --version                    # or set CLAUDE_BIN to the absolute path
codex --version                     # or set CODEX_BIN; codex login if used
```

If the conductor is started by launchd/cron/a GUI app, set in its plist/unit:

```
PATH=/Users/<you>/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

or export `GH_BIN`, `CLAUDE_BIN`, `CODEX_BIN` as absolute paths. The code now
augments PATH defensively (`config._augmented_path`), but fixing it at the
launch level is the durable fix. For PR creation without `gh`, provide a token
via `GH_TOKEN`/`GITHUB_TOKEN` (the REST fallback and `scripts/gh_pr.sh` use it).

## 5. Quick reference

- Conductor entry: `scripts/wca_conductor.py`; engine pipeline: `src/wca/conductor/runner.py`;
  fan-out/cap/health: `src/wca/conductor/manager.py`; routing: `dispatcher.py`;
  safe env + PATH/bin resolution: `config.py`; auth probing: `health.py`.
- PR fallback script: `scripts/gh_pr.sh`.
- Tests: `tests/test_conductor.py` (offline; the `runner._run` subprocess seam is patched).
