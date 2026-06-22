"""Engine health/availability probing for availability-aware routing.

The router must not dispatch to a logged-out engine. We probe each engine's
auth state cheaply and cache it (see :class:`ConductorManager`), so ``/task``
— and even an explicit ``/claude`` when Claude is logged out — can fall back
to a healthy engine instead of failing with "agent exited 1".

Probes:
* **claude** — a real ``claude -p "ok" --output-format json`` (the only
  reliable signal; a logged-out CLI returns ``is_error`` + "Not logged in" on
  stdout instantly). Reuses :func:`runner._last_json_object`.
* **codex** — a cheap file heuristic (``~/.codex/auth.json`` present &
  non-empty) to avoid spending Codex tokens just to check health.
"""

from __future__ import annotations

import os
import shutil
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


def _bin_present(binary: str) -> bool:
    """True if *binary* is runnable (abs path exists, or bare name on PATH)."""
    if not binary:
        return False
    if "/" in binary:
        return os.path.exists(binary)
    return shutil.which(binary) is not None


def probe_claude(cfg: ConductorConfig, timeout: float = 30.0) -> EngineHealth:
    eng = Engine.CLAUDE.value
    if not _bin_present(cfg.claude_bin):
        return EngineHealth(eng, False, "claude CLI not found (%s)" % cfg.claude_bin)
    try:
        res = runner._run(
            [cfg.claude_bin, "-p", "ok", "--output-format", "json"],
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


def probe_codex(cfg: ConductorConfig) -> EngineHealth:
    eng = Engine.CODEX.value
    if not _bin_present(cfg.codex_bin):
        return EngineHealth(eng, False, "codex CLI not found (%s)" % cfg.codex_bin)
    auth = os.path.expanduser("~/.codex/auth.json")
    try:
        if os.path.exists(auth) and os.path.getsize(auth) > 0:
            return EngineHealth(eng, True, "ok")
    except OSError:
        pass
    return EngineHealth(eng, False, "codex not logged in — run `codex login`")


def probe_engine(cfg: ConductorConfig, engine: str) -> EngineHealth:
    if Engine.coerce(engine) is Engine.CLAUDE:
        return probe_claude(cfg)
    return probe_codex(cfg)
