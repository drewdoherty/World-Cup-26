"""Configuration + the safe environment handed to spawned agents.

One :class:`ConductorConfig` carries every knob the runner/manager need:
where the repo is, the parallelism cap, the token budget, which CLIs to call,
and — critically — the *sanitised* environment that agent subprocesses run
under so they can never touch live money or the real ledger.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from wca.conductor.models import Engine

# Env keys that must NEVER reach a spawned agent's environment.
_DEFAULT_STRIP = ("POLYMARKET_PRIVATE_KEY",)
# Overrides forced onto every spawned agent so any code it runs stays dry.
_DEFAULT_OVERRIDES = {"PM_DRY_RUN": "1", "WCA_DB_PATH": "data/dev.db"}

# User/Homebrew bin dirs that hold gh / claude / codex but are frequently
# ABSENT from the PATH of a non-login launch context (launchd, cron, a GUI app
# spawning the conductor). When the conductor itself is started this way, its
# inherited PATH is the minimal "/usr/bin:/bin:/usr/sbin:/sbin" and every CLI
# look-up (gh, claude, codex) fails with "not found" even though the binary is
# installed — this is THE root cause of the swarm's "gh CLI not found" errors.
# We always fold these onto PATH for both the binary look-up and the spawned
# subprocess so an installed tool is found regardless of how we were launched.
_EXTRA_PATH_DIRS = (
    "~/.local/bin",
    "~/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def _augmented_path(base: Optional[str] = None) -> str:
    """``base`` PATH with the known user/Homebrew bin dirs guaranteed present.

    Existing entries are preserved and ordered first; the extra dirs are
    appended only if missing. Never drops anything already on PATH.
    """
    current = base if base is not None else os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    seen = set(parts)
    for raw in _EXTRA_PATH_DIRS:
        d = os.path.expanduser(raw)
        if d not in seen and os.path.isdir(d):
            parts.append(d)
            seen.add(d)
    return os.pathsep.join(parts)


def _default_claude_args() -> List[str]:
    # Headless JSON output (so we can parse the result + token usage) and
    # auto-accept edits so the agent can actually modify files in its worktree.
    return ["--output-format", "json", "--permission-mode", "acceptEdits"]


def _default_codex_args() -> List[str]:
    # codex exec defaults to a READ-ONLY sandbox, so without this the agent
    # can't modify files and every task lands as NO_CHANGES. workspace-write
    # lets it edit within the worktree (approval is already "never" in exec).
    return ["--sandbox", "workspace-write"]


@dataclass
class ConductorConfig:
    repo_root: Path
    base_branch: str = "main"
    worktrees_dir: Optional[Path] = None  # defaults to repo_root/.claude/worktrees
    branch_prefix: str = "conductor"

    max_parallel: int = 3
    token_budget: Optional[int] = None  # None / 0 -> unlimited
    codex_auto_limit: int = 1  # max queued/running automatic Codex tasks
    disabled_engines: List[str] = field(default_factory=list)  # e.g. ["codex"] when exhausted/off

    git_bin: str = "git"
    gh_bin: str = "gh"
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    claude_args: List[str] = field(default_factory=_default_claude_args)
    codex_args: List[str] = field(default_factory=_default_codex_args)
    agent_timeout: float = 1800.0  # 30 min hard cap per agent run

    create_pr: bool = True   # attempt `gh pr create`; falls back to compare link
    push: bool = True        # push the branch (False -> local-only dry run)

    strip_env_keys: List[str] = field(default_factory=lambda: list(_DEFAULT_STRIP))
    safe_env_overrides: Dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_OVERRIDES))

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root)
        if self.worktrees_dir is None:
            self.worktrees_dir = self.repo_root / ".claude" / "worktrees"
        else:
            self.worktrees_dir = Path(self.worktrees_dir)
        if self.token_budget is not None and self.token_budget <= 0:
            self.token_budget = None
        if self.max_parallel < 1:
            self.max_parallel = 1
        if self.codex_auto_limit < 0:
            self.codex_auto_limit = 0
        self.disabled_engines = [e.strip().lower() for e in self.disabled_engines if e and e.strip()]

    def is_disabled(self, engine: str) -> bool:
        return Engine.coerce(engine).value in self.disabled_engines

    # -- env --------------------------------------------------------------

    def agent_env(self) -> Dict[str, str]:
        """The environment a spawned agent runs under.

        Inherits the current process env, then **removes** secret keys and
        **forces** dry-run overrides. This is the load-bearing safety guard:
        even if an agent runs project code, it gets ``PM_DRY_RUN=1`` and the
        dev DB, with no Polymarket key present.
        """
        env = dict(os.environ)
        for key in self.strip_env_keys:
            env.pop(key, None)
        env.update(self.safe_env_overrides)
        # Guarantee gh/claude/codex are discoverable even when the conductor was
        # launched with a minimal PATH (launchd/cron/GUI). See _EXTRA_PATH_DIRS.
        env["PATH"] = _augmented_path(env.get("PATH"))
        return env

    def resolve_bin(self, name: str) -> Optional[str]:
        """Absolute path to *name*, searching the augmented PATH.

        Returns the input unchanged if it is already an absolute/relative path
        that exists; otherwise looks it up on the augmented PATH so an installed
        tool in ``~/.local/bin`` etc. is found regardless of how we were
        launched. ``None`` only when the tool is genuinely not installed.
        """
        if "/" in name:
            return name if os.path.exists(name) else None
        return shutil.which(name, path=_augmented_path())

    # -- per-engine CLI ---------------------------------------------------

    def cli_for(self, engine: str) -> "tuple[str, List[str]]":
        """Return ``(binary, extra_args)`` for *engine*."""
        eng = Engine.coerce(engine)
        if eng is Engine.CLAUDE:
            return self.claude_bin, list(self.claude_args)
        return self.codex_bin, list(self.codex_args)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_env(cls, repo_root: "os.PathLike[str] | str", **overrides: object) -> "ConductorConfig":
        """Build a config from env vars, with explicit ``**overrides`` winning.

        Recognised env: ``CLAUDE_BIN``, ``CODEX_BIN``, ``GH_BIN``,
        ``WCA_CONDUCTOR_MAX_PARALLEL``, ``WCA_CONDUCTOR_TOKEN_BUDGET``,
        ``WCA_CONDUCTOR_BASE_BRANCH``, ``WCA_CONDUCTOR_BRANCH_PREFIX``,
        ``WCA_CONDUCTOR_CODEX_AUTO_LIMIT``.
        """
        def _int(name: str, default: Optional[int]) -> Optional[int]:
            raw = os.environ.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        kwargs: Dict[str, object] = dict(
            repo_root=Path(repo_root),
            base_branch=os.environ.get("WCA_CONDUCTOR_BASE_BRANCH", "main"),
            branch_prefix=os.environ.get("WCA_CONDUCTOR_BRANCH_PREFIX", "conductor"),
            max_parallel=_int("WCA_CONDUCTOR_MAX_PARALLEL", 3) or 3,
            token_budget=_int("WCA_CONDUCTOR_TOKEN_BUDGET", None),
            codex_auto_limit=_int("WCA_CONDUCTOR_CODEX_AUTO_LIMIT", 1) or 0,
            disabled_engines=[e for e in os.environ.get("WCA_CONDUCTOR_DISABLED_ENGINES", "").split(",") if e.strip()],
            gh_bin=os.environ.get("GH_BIN", "gh"),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            codex_bin=os.environ.get("CODEX_BIN", "codex"),
        )
        kwargs.update(overrides)
        return cls(**kwargs)  # type: ignore[arg-type]
