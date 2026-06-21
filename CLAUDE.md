# CLAUDE.md

Guidance for Claude Code and other agents/LLMs working in this repository.

## Contribution workflow (all agents/devices/LLMs)

1. `main` is the only source of truth for code. NEVER commit directly to main.
2. Start every task: `git fetch origin && git switch main && git pull --ff-only` then `git switch -c <type>/<short-name>` (e.g. feat/news-filter, fix/ledger-clv).
3. Work only on that branch. Commit small. Push with `git push -u origin HEAD`.
4. Open a PR to main on GitHub and squash-merge it; delete the branch after.
5. NEVER edit data/wca.db or .env. Live state belongs to the Mac mini only. For any local run use PM_DRY_RUN=1 and a throwaway .env.dev.
6. To continue another model's work, check out THEIR branch (`git fetch && git switch <their-branch>`) — do not start a parallel branch for the same task.
7. One task = one branch = one PR. Keep PRs small so they merge before they collide.
