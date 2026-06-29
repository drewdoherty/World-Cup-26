# AGENTS.md — World Cup Alpha dev-conductor operating guide

This repo is worked by a **swarm**: the in-repo dev-conductor
(`src/wca/conductor/`, driven from the `@worldcupdevbot` Telegram chat) fans
each `/task` out to a headless **Claude Code** agent. Every task gets its own
git **worktree + branch off `main`**, runs the agent, commits, pushes, and
opens a PR. The conductor is **PR-only and dry-run** — it never commits `main`
and never places live bets (`PM_DRY_RUN=1`, `WCA_DB_PATH=data/dev.db`, the
Polymarket key is stripped from the agent env).

**Claude-only since 2026-06.** Codex was removed from the swarm (unreliable
token accounting + auth, and everything routed to Claude in practice). Routing
(`dispatcher.choose_engine`) always returns Claude; there is no `/codex`.

## 1. Root causes this guide closes

| Symptom | Real cause | Fix |
|---|---|---|
| every task "pushed — gh CLI not found" | conductor launched with a minimal PATH (launchd/cron/GUI) lacking `~/.local/bin`; `gh`/`claude` are installed but unresolved | PATH augmented for every agent + PR call (`config.agent_env`, `config.resolve_bin`); PR creation has a REST-API fallback (`runner._open_pr_via_api`, `scripts/gh_pr.sh`) |
| prompt starting with `-`/`--` parsed as a flag | prompt passed as a CLI positional | prompt follows a `--` end-of-options marker in `runner.run_agent` |
| same feature built twice in parallel → divergent, conflicting PRs | two tasks for one feature, each branched off `main` | one-feature-per-task + duplicate pre-flight (`manager.find_active_duplicate`) + the integration-branch rule below |
| `/task "paste the output of #2"` produced nothing | tasks are **isolated** agents with no shared memory | the "tasks are isolated" rule below |

## 2. Operating rules (the swarm MUST follow these)

1. **One feature per task.** Follow-up is a second commit on the *same* branch, not a new task off `main`.
2. **Check for duplicates before dispatch** via `ConductorManager.find_active_duplicate(task)`; cancel the original before re-running.
3. **Tasks are isolated — no cross-task references.** An agent sees only its prompt + the repo. Put everything it needs *in the prompt*; never "continue task #N" or "paste output of #N".
4. **Branch naming is fixed:** `conductor/claude-<slug>-<shortid>` (set in `manager._new_record_locked`).
5. **Prompts are plain imperative text** — lead with a verb. (Leading `-`/`--` is handled, but keep it clean.)
6. **Integration over parallel-merge.** When several related branches exist, create ONE `integrate/<topic>` branch off `main`, merge/cherry-pick the chosen branches into it, resolve conflicts once, get tests green, open one PR. Pick the *better* of any duplicate pair — never both.
7. **Pre-flight file-overlap check.** Two tasks touching the same file (`src/wca/bot/app.py`, `src/wca/accas.py`, `scripts/wca_build_card.py`) must be serialized or merged into one task.
8. **Every task must leave the tree green.** Run `pytest -q` in the worktree before push; a red suite is FAILED, not PUSHED.

## 3. Concurrency

- **Sequential by default** (`max_parallel=1`): parallel runs raced the shared `.git` worktree registry. Opt into a swarm with `WCA_CONDUCTOR_MAX_PARALLEL>1` only when you understand the contention.
- All work runs on Claude; a logged-out Claude is reported via `preflight()` / `/help`, not silently dropped.

## 4. Conductor-app setup (do this once, at the launch level)

Launch the conductor with a login-shell environment (or an explicit PATH) so installed CLIs resolve:

```
gh --version && gh auth status      # gh installed + authed (scopes: repo, workflow)
claude --version                    # or set CLAUDE_BIN to the absolute path
```

If started by launchd/cron/a GUI app, set in its plist/unit:

```
PATH=/Users/<you>/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
```

or export `GH_BIN` / `CLAUDE_BIN` as absolute paths. The code augments PATH
defensively (`config._augmented_path`), but fixing it at the launch level is
the durable fix. For PR creation without `gh`, provide `GH_TOKEN`/`GITHUB_TOKEN`
(the REST fallback and `scripts/gh_pr.sh` use it).

## 5. Data & generated-artifact discipline

**Raw data and generated artifacts are NEVER committed — they are
re-downloadable or rebuilt at deploy.** Git tracks source (code, config, durable
hand-curated datasets), not runtime input or build output.

- **Raw odds-API snapshots** (`data/raw/snapshots/`) — re-downloadable; untracked
  via `data/raw/*`. Mirror to off-repo object storage, never to git.
- **Generated site feeds** (`site/*.json`, `site/microstructure/*.json`) — build
  output of the card/feed/sync jobs. These should be produced at deploy/serve
  time, not version-controlled. See `docs/data-and-artifacts.md` for the current
  publish path and the migration needed before they can be safely untracked.
- The durable, hand-curated exceptions stay tracked and are listed explicitly in
  `.gitignore` (`!data/raw/martj42_cleaned.csv`, `!data/processed/wc2026_results.json`).

## 6. Quick reference

- Entry: `scripts/wca_conductor.py`; pipeline: `runner.py`; fan-out/cap/health/merge: `manager.py`; routing: `dispatcher.py`; safe env + PATH/bin resolution: `config.py`; auth probing: `health.py`.
- PR fallback script: `scripts/gh_pr.sh`. Tests: `tests/test_conductor.py` (offline; the `runner._run` seam is patched).
