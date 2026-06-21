"""Tests for the best-effort site auto-sync."""
from __future__ import annotations

import wca.sync as sync
from wca.ledger.store import record_bet


def test_refresh_site_data_writes_json(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "virginbet",
               2.0, 5.0, db_path=db)
    out = tmp_path / "data.json"
    # Point the writer at a temp output by patching write_site_data target via REPO.
    monkeypatch.setattr(sync, "_REPO", str(tmp_path))
    (tmp_path / "site").mkdir()
    (tmp_path / "data").mkdir()
    ok = sync.refresh_site_data(db_path=db)
    assert ok is True
    assert (tmp_path / "site" / "data.json").exists()


def test_push_site_skips_entirely_under_pytest(tmp_path, monkeypatch):
    # PYTEST_CURRENT_TEST is always set during a test run, so push_site must
    # be a no-op: no git, no refresh of the (real) repo. This is the guard that
    # stops bot tests from clobbering the live site.
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "virginbet",
               2.0, 5.0, db_path=db)

    def boom(*a, **k):
        raise AssertionError("git must not run under pytest")

    monkeypatch.setattr(sync, "_git", boom)
    assert sync.push_site(reason="x", db_path=db, enabled=True) is False


def test_push_site_disabled_only_refreshes(tmp_path, monkeypatch):
    # Opt out of the pytest guard to exercise the real enabled=False path.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "virginbet",
               2.0, 5.0, db_path=db)
    monkeypatch.setattr(sync, "_REPO", str(tmp_path))
    (tmp_path / "site").mkdir()
    (tmp_path / "data").mkdir()
    called = {"git": False}

    def fake_git(*a, **k):
        called["git"] = True
        raise AssertionError("git should not run when disabled")

    monkeypatch.setattr(sync, "_git", fake_git)
    result = sync.push_site(reason="x", db_path=db, enabled=False)
    assert result is False
    assert called["git"] is False
    assert (tmp_path / "site" / "data.json").exists()  # still refreshed


def test_push_site_defaults_to_local_only(tmp_path, monkeypatch):
    # Private/local operation is the default: unless WCA_AUTOPUSH=1 is explicitly
    # set, bot/daemon syncs regenerate JSON but do not touch git/Vercel.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("WCA_AUTOPUSH", raising=False)
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "virginbet",
               2.0, 5.0, db_path=db)
    monkeypatch.setattr(sync, "_REPO", str(tmp_path))
    (tmp_path / "site").mkdir()
    (tmp_path / "data").mkdir()
    called = {"git": False}

    def fake_git(*a, **k):
        called["git"] = True
        raise AssertionError("git should not run unless WCA_AUTOPUSH=1")

    monkeypatch.setattr(sync, "_git", fake_git)
    result = sync.push_site(reason="x", db_path=db)
    assert result is False
    assert called["git"] is False
    assert (tmp_path / "site" / "data.json").exists()


def test_push_site_never_raises_on_git_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    db = str(tmp_path / "t.db")
    record_bet("2026-06-11T10:00:00", "M", "A vs B", "h2h", "A", "virginbet",
               2.0, 5.0, db_path=db)
    monkeypatch.setattr(sync, "_REPO", str(tmp_path))
    (tmp_path / "site").mkdir()
    (tmp_path / "data").mkdir()

    def boom(*a, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(sync, "_git", boom)
    # Must swallow the error and return False, not raise.
    assert sync.push_site(reason="x", db_path=db, enabled=True) is False


def test_autosync_swallows_errors(tmp_path, monkeypatch):
    import wca.bot.app as app

    def boom(**k):
        raise RuntimeError("nope")

    monkeypatch.setattr("wca.sync.push_site", boom)
    # _autosync must never raise even if push_site blows up.
    app._autosync(str(tmp_path / "x.db"), "test")
