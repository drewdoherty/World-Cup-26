"""Tests for the watchdog.sh git-behind-origin alert (P5 / PHASE1_DESIGN.md §9 increment 2).

autopull.sh runs `git pull --rebase --autostash` every 5 min but is silent on
failure (network blip, rebase conflict, job not firing). The watchdog now also
does a READ-ONLY `git fetch` + `git rev-list --count HEAD..origin/main` each
run and alerts (via the same Telegram `notify()` path used for daemon-down
alerts) once the repo has been measured behind for N consecutive samples.

These tests build a REAL local git repo + a REAL local "origin" remote (both
under a temp dir) so `git fetch`/`git rev-list` exercise genuine git behavior
with no network access — only `launchctl` and `curl` are stubbed (via a fake
PATH) since those would otherwise touch real launchd / the real Telegram API.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_SH = REPO_ROOT / "deploy" / "macmini" / "watchdog.sh"


def _run_git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo_with_origin(tmp_path: Path):
    """Create origin.git (bare) + a local clone-equivalent checkout at HEAD == origin."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git(["init", "--bare", "-q", "-b", "main"], origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(["init", "-q", "-b", "main"], seed)
    _run_git(["config", "user.email", "test@example.com"], seed)
    _run_git(["config", "user.name", "Test"], seed)
    (seed / "README.md").write_text("seed\n")
    _run_git(["add", "README.md"], seed)
    _run_git(["commit", "-q", "-m", "seed"], seed)
    _run_git(["remote", "add", "origin", str(origin)], seed)
    _run_git(["push", "-q", "origin", "main"], seed)

    repo = tmp_path / "repo"
    _run_git(["clone", "-q", str(origin), str(repo)], tmp_path)
    _run_git(["config", "user.email", "test@example.com"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    return origin, seed, repo


def _advance_origin(origin: Path, seed: Path, n: int = 1):
    """Push n new commits to origin via the seed checkout (repo's clone stays behind)."""
    for i in range(n):
        (seed / f"file_{os.urandom(3).hex()}.txt").write_text(f"change {i}\n")
        _run_git(["add", "-A"], seed)
        _run_git(["commit", "-q", "-m", f"advance {i}"], seed)
    _run_git(["push", "-q", "origin", "main"], seed)


def _write_stub_bin(bin_dir: Path, launchctl_out: str = "", curl_log: Path | None = None):
    """Fake `launchctl` (report nothing running -> no daemons flagged) and `curl`
    (record invocations instead of hitting the network)."""
    bin_dir.mkdir(parents=True, exist_ok=True)

    launchctl = bin_dir / "launchctl"
    launchctl.write_text(f"#!/bin/bash\ncat <<'EOF'\n{launchctl_out}\nEOF\n")
    launchctl.chmod(launchctl.stat().st_mode | stat.S_IEXEC)

    curl = bin_dir / "curl"
    log_line = f'echo "curl called: $*" >> "{curl_log}"\n' if curl_log else ""
    curl.write_text(f"#!/bin/bash\n{log_line}exit 0\n")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)


def _setup_watchdog_env(tmp_path: Path, repo: Path, curl_log: Path):
    """Lay out deploy/macmini/{watchdog.sh,services.env} + logs/ + .env inside `repo`
    (which is a real git checkout tracking a real local origin), matching the
    REPO_ROOT layout watchdog.sh expects (HERE/../.. from deploy/macmini)."""
    deploy_dir = repo / "deploy" / "macmini"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "watchdog.sh").write_text(WATCHDOG_SH.read_text())
    (deploy_dir / "watchdog.sh").chmod(0o755)
    # Minimal services.env: one daemon that will always show as "not loaded"
    # under the stub launchctl (harmless — asserted alerts below only check
    # for the "git.behind" label so daemon-down noise doesn't interfere), so
    # only the git-behind check's alerts are exercised by these tests.
    # NOTE: bash 3.2 (macOS default) throws "unbound variable" on
    # `"${arr[@]}"` for an empty array under `set -u`, so this must be
    # non-empty to match production services.env (always >=1 daemon).
    (deploy_dir / "services.env").write_text(
        'WCA_DAEMONS=(noop)\nWCA_LABEL_PREFIX="com.wca.test"\n'
    )
    (repo / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=fake-token\nTELEGRAM_ADMIN_USER_ID=12345\n"
    )
    (repo / "logs").mkdir(exist_ok=True)

    bin_dir = tmp_path / "stubbin"
    _write_stub_bin(bin_dir, curl_log=curl_log)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def _run_watchdog(repo: Path, env: dict):
    return subprocess.run(
        ["bash", str(repo / "deploy" / "macmini" / "watchdog.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_up_to_date_repo_never_alerts(tmp_path):
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    # Run only twice: the fixture's "noop" daemon is deliberately never
    # loaded (see _setup_watchdog_env), so by the 2nd (STRIKES-th) run it
    # trips its OWN alert — expected, unrelated to git-behind. We only care
    # that the git-behind path stays silent while the repo is current.
    for _ in range(2):
        result = _run_watchdog(repo, env)
        assert result.returncode == 0, result.stderr
        assert "git.behind" not in result.stderr

    state = (repo / "logs" / "watchdog.state").read_text()
    assert "git.behind 0" in state
    if curl_log.exists():
        assert "git.behind" not in curl_log.read_text()


def test_behind_repo_does_not_alert_on_first_strike(tmp_path):
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    _advance_origin(origin, seed, n=2)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    result = _run_watchdog(repo, env)
    assert result.returncode == 0, result.stderr
    assert "git.behind" not in result.stderr  # below GIT_BEHIND_STRIKES (default 2)
    assert not curl_log.exists()

    state = (repo / "logs" / "watchdog.state").read_text()
    assert "git.behind 1" in state


def test_behind_repo_alerts_after_two_consecutive_checks(tmp_path):
    """Debounce: only alert once behind for GIT_BEHIND_STRIKES consecutive runs
    (default 2, ~10 min @ 5-min cadence) so an in-flight autopull cycle doesn't
    false-positive on a single sample."""
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    _advance_origin(origin, seed, n=3)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    r1 = _run_watchdog(repo, env)
    assert "git.behind" not in r1.stderr
    assert not curl_log.exists()

    r2 = _run_watchdog(repo, env)
    assert r2.returncode == 0, r2.stderr
    assert "git.behind" in r2.stderr
    assert "3 commit(s) behind" in r2.stderr
    assert curl_log.exists()
    assert "sendMessage" in curl_log.read_text()

    state = (repo / "logs" / "watchdog.state").read_text()
    assert "git.behind 2" in state


def test_recovery_alert_after_catching_up(tmp_path):
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    _advance_origin(origin, seed, n=1)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    _run_watchdog(repo, env)
    _run_watchdog(repo, env)  # crosses strike threshold, alerts
    curl_log.unlink(missing_ok=True)

    # Catch up: pull the repo to HEAD == origin/main.
    subprocess.run(["git", "pull", "-q", "origin", "main"], cwd=repo, check=True, env=env)

    r3 = _run_watchdog(repo, env)
    assert r3.returncode == 0, r3.stderr
    assert "recovered" in r3.stderr
    state = (repo / "logs" / "watchdog.state").read_text()
    assert "git.behind 0" in state


def test_custom_strike_threshold_env_var(tmp_path):
    """WCA_WATCHDOG_GIT_BEHIND_STRIKES overrides the default debounce window."""
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    _advance_origin(origin, seed, n=1)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)
    env["WCA_WATCHDOG_GIT_BEHIND_STRIKES"] = "1"

    result = _run_watchdog(repo, env)
    assert result.returncode == 0, result.stderr
    assert "git.behind" in result.stderr
    assert curl_log.exists()


def test_fetch_failure_treated_as_behind(tmp_path):
    """If origin is unreachable, that's exactly the kind of silent staleness
    this check exists to catch — it must strike (and eventually alert), not
    silently reset to 0."""
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    # Break the remote so `git fetch` fails.
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")],
        cwd=repo,
        check=True,
        env=env,
    )

    r1 = _run_watchdog(repo, env)
    assert "git.behind" not in r1.stderr
    state1 = (repo / "logs" / "watchdog.state").read_text()
    assert "git.behind 1" in state1

    r2 = _run_watchdog(repo, env)
    assert "git.behind" in r2.stderr
    assert "fetch failed" in r2.stderr
    assert curl_log.exists()


def test_watchdog_never_mutates_working_tree_or_resets(tmp_path):
    """The git-behind check must be read-only: no pull/rebase/reset — HEAD stays
    put even though origin has moved ahead."""
    origin, seed, repo = _init_repo_with_origin(tmp_path)
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _advance_origin(origin, seed, n=2)
    curl_log = tmp_path / "curl.log"
    env = _setup_watchdog_env(tmp_path, repo, curl_log)

    _run_watchdog(repo, env)
    _run_watchdog(repo, env)

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert before_head == after_head

    # The test harness itself drops untracked scaffolding (.env, deploy/,
    # logs/) into the repo — that's fixture setup, not something watchdog.sh
    # touched. What matters is that no *tracked* file was modified/staged and
    # HEAD didn't move, i.e. the check never pulled/rebased/reset.
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status.strip() == ""
