"""Best-effort site sync: regenerate site/data.json and push so the live
Vercel site tracks the ledger automatically after a bot write.

Design:
* Fully best-effort — every failure is swallowed and logged; the bot must
  never crash because a push failed (e.g. a transient network blip or a
  concurrent manual push). The next sync catches up.
* Regenerating ``data.json`` is cheap (reads the ledger + cached card; no
  model fit), so it is safe to run inline on the bot's reply path.
* Git work runs through a short-timeout subprocess and a rebase-pull first to
  avoid clobbering concurrent commits.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SITE_FILES = ["site/data.json", "site/scores_data.json", "site/linemove.json"]


def _log(msg: str) -> None:
    sys.stderr.write("[sync] %s\n" % msg)


def refresh_site_data(db_path: str = "data/wca.db") -> bool:
    """Regenerate site/data.json from the current ledger. Returns success."""
    try:
        from wca.sitedata import write_site_data
        import datetime as _dt

        now = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        write_site_data(db_path, out_path=os.path.join(_REPO, "site", "data.json"),
                         card_path=os.path.join(_REPO, "data", "card_latest.md"),
                         now_utc=now)
        return True
    except Exception as exc:  # never propagate to the bot
        _log("refresh failed: %s" % exc)
        return False


def _git(args: List[str], timeout: float = 45.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", _REPO] + args, capture_output=True,
                          text=True, timeout=timeout)


def push_site(reason: str = "ledger update", db_path: str = "data/wca.db",
              enabled: Optional[bool] = None) -> bool:
    """Regenerate site data and push the site JSON files. Best-effort.

    Set ``WCA_AUTOPUSH=0`` to disable pushing (regenerate only). Returns True
    only if a push actually succeeded.
    """
    if enabled is None:
        enabled = os.environ.get("WCA_AUTOPUSH", "1") != "0"
    refreshed = refresh_site_data(db_path)
    if not enabled:
        return False
    try:
        # Only proceed if the site JSON actually changed.
        changed = [f for f in _SITE_FILES
                   if os.path.exists(os.path.join(_REPO, f))]
        if not changed:
            return False
        st = _git(["status", "--porcelain"] + changed)
        if not st.stdout.strip():
            return False  # nothing to commit
        _git(["pull", "--rebase", "--autostash", "-q"])
        _git(["add"] + changed)
        cm = _git(["commit", "-q", "-m", "Auto-sync site: %s" % reason,
                   "--no-verify"])
        if cm.returncode != 0:
            _log("commit skipped: %s" % (cm.stderr.strip() or cm.stdout.strip()))
            return False
        ps = _git(["push", "-q"])
        if ps.returncode != 0:
            _log("push failed: %s" % ps.stderr.strip())
            return False
        return True
    except Exception as exc:
        _log("push_site error: %s" % exc)
        return False
