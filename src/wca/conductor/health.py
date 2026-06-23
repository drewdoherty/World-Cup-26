"""Engine health/availability probing (Claude-only).

The router must not dispatch to a logged-out engine. We probe Claude's auth
state cheaply and cache it (see :class:`ConductorManager`) so ``/claude`` /
``/task`` report a clear "not logged in" reason instead of failing with a bare
"agent exited 1". Codex was removed from the swarm (2026-06).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from wca.conductor import runner
from wca.conductor.config import ConductorConfig
from wca.conductor.models import Engine


@dataclass
class EngineHealth:
    engine: str
    ok: bool
    reason: str
    checked_at: float = 0.0


def probe_claude(cfg: ConductorConfig, timeout: float = 30.0) -> EngineHealth:
    eng = Engine.CLAUDE.value
    # Resolve on the AUGMENTED PATH (incl. ~/.local/bin, Homebrew) so an
    # installed CLI is found even under a minimal launch env.
    binary = cfg.resolve_bin(cfg.claude_bin)
    if binary is None:
        return EngineHealth(eng, False, "claude CLI not found (%s)" % cfg.claude_bin)
    try:
        res = runner._run(
            [binary, "-p", "ok", "--output-format", "json"],
            env=cfg.agent_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return EngineHealth(eng, False, "claude probe timed out")
    obj = runner._last_json_object(res.stdout or "")
    if obj is not None and obj.get("is_error"):
        msg = str(obj.get("result") or obj.get("error") or "").strip()
        if "login" in msg.lower() or "logged in" in msg.lower():
            return EngineHealth(eng, False, "claude not logged in — run `claude setup-token`")
        return EngineHealth(eng, False, (msg[:120] or "claude error"))
    if res.returncode != 0 and obj is None:
        detail = (res.stderr or "").strip()[:120] or "claude exited %d" % res.returncode
        return EngineHealth(eng, False, detail)
    return EngineHealth(eng, True, "ok")


def probe_engine(cfg: ConductorConfig, engine: str) -> EngineHealth:
    Engine.coerce(engine)  # validate (raises on anything but claude)
    return probe_claude(cfg)
