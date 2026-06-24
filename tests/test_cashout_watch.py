"""Tests for the cash-out watcher loop brain (wca.pm.cashout_watch) and the
dedup/claim/cooldown state (wca.pm.cashout_state). All I/O is injected; no
network. ``now`` is a wall-clock epoch (seconds), matching the daemon.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from wca.pm import cashout_state
from wca.pm.cashout_watch import CashoutWatcher, WatchConfig
from wca.pm.positions import Position


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_cw_")
    os.close(fd)
    os.unlink(path)
    return path


def _pos(title="Exact Score: United States 0 - 0 Paraguay?", outcome="Yes",
         asset="TOK1", size=100.0):
    return Position(
        asset=asset, condition_id="0xc", size=size, avg_price=0.10,
        cur_price=0.08, outcome=outcome, title=title, slug="s",
        event_slug="fifwc-usa-par", end_date="2026-06-13",
        neg_risk=True, redeemable=False, current_value=8.0,
    )


# 0-0 dead: Paraguay (away) scored.
_SCORES_GOAL = [{
    "home_team": "United States", "away_team": "Paraguay",
    "scores": [{"name": "United States", "score": "0"},
               {"name": "Paraguay", "score": "1"}],
}]
# Still 0-0.
_SCORES_NIL = [{
    "home_team": "United States", "away_team": "Paraguay",
    "scores": [{"name": "United States", "score": "0"},
               {"name": "Paraguay", "score": "0"}],
}]

_FAT_BOOK = {"bids": [{"price": "0.06", "size": "500"}]}   # plenty of value
_THIN_BOOK = {"bids": [{"price": "0.001", "size": "500"}]}  # ~nothing


# Structured execute_fn results (matching wca.bot.app.execute_cashout shape).
_SOLD = {"outcome": "sold", "submitted": True, "settled": True, "message": "sold"}
_DRYARM = {"outcome": "dry_run", "submitted": False, "settled": False,
           "dry_run": True, "message": "dry-arm"}
_NOFILL = {"outcome": "no_fill", "submitted": True, "settled": False, "message": "no fill"}
_SETTLEFAIL = {"outcome": "unconfirmed", "submitted": True, "settled": False,
               "error": "boom", "message": "booking failed"}


def _book_fn(book):
    return lambda asset: book


def _exec(result):
    """An execute_fn that records calls and returns a fixed structured result."""
    calls = []

    def fn(p):
        calls.append(p)
        return result

    fn.calls = calls
    return fn


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestState:
    def test_claim_is_atomic_and_dedups(self):
        db = _tmp_db()
        assert cashout_state.claim("A", "m", db_path=db) is True
        assert cashout_state.claim("A", "m", db_path=db) is False
        assert cashout_state.is_handled("A", db) is True

    def test_claim_upgrades_observed(self):
        db = _tmp_db()
        cashout_state.observe("A", "m", 100.0, db_path=db)
        assert cashout_state.is_handled("A", db) is False  # observed != handled
        assert cashout_state.claim("A", "m", db_path=db) is True  # observed -> claimed
        assert cashout_state.claim("A", "m", db_path=db) is False  # now locked

    def test_observe_persists_first_epoch(self):
        db = _tmp_db()
        assert cashout_state.observe("A", "m", 100.0, db_path=db) == 100.0
        # A later observe keeps the ORIGINAL first-seen epoch (cooldown anchor).
        assert cashout_state.observe("A", "m", 999.0, db_path=db) == 100.0

    def test_clear_allows_reclaim(self):
        db = _tmp_db()
        cashout_state.claim("A", "m", db_path=db)
        cashout_state.clear("A", db)
        assert cashout_state.is_handled("A", db) is False

    def test_settle_failed_is_handled(self):
        db = _tmp_db()
        cashout_state.claim("A", "m", db_path=db)
        cashout_state.set_phase("A", "settle_failed", db_path=db)
        assert cashout_state.is_handled("A", db) is True


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class TestWatcherCooldown:
    def test_kill_within_cooldown_waits(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=45, arm=True), db)
        ex = _exec(_SOLD)
        acts = w.tick([_pos()], _SCORES_GOAL, now=100.0,
                      book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "cooldown"
        assert ex.calls == []

    def test_kill_sells_after_cooldown_when_armed(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=45, arm=True), db)
        ex = _exec(_SOLD)
        w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        acts = w.tick([_pos()], _SCORES_GOAL, now=146.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "sold"
        assert len(ex.calls) == 1 and ex.calls[0]["side"] == "SELL"
        assert cashout_state.get_phase("TOK1", db) == "sold"

    def test_cooldown_persists_across_restart(self):
        """A new watcher instance (simulated restart) honours the ORIGINAL
        first-seen time — the cooldown is not reset (review finding 5)."""
        db = _tmp_db()
        w1 = CashoutWatcher(WatchConfig(var_cooldown_s=45, arm=True), db)
        w1.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK),
                execute_fn=_exec(_SOLD))  # observes at 100
        w2 = CashoutWatcher(WatchConfig(var_cooldown_s=45, arm=True), db)  # "restart"
        ex = _exec(_SOLD)
        acts = w2.tick([_pos()], _SCORES_GOAL, now=146.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "sold"  # not reset back to a fresh 45s wait
        assert len(ex.calls) == 1


class TestWatcherDedup:
    def test_sold_position_not_resold(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_SOLD)
        w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        w.tick([_pos()], _SCORES_GOAL, now=101.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert len(ex.calls) == 1

    def test_preclaimed_not_executed(self):
        db = _tmp_db()
        cashout_state.claim("TOK1", "m", db_path=db)
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_SOLD)
        w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert ex.calls == []


class TestWatcherResultStateMachine:
    def test_settle_failed_keeps_locked_and_alerts(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_SETTLEFAIL)
        acts = w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "settle_failed"
        assert cashout_state.get_phase("TOK1", db) == "settle_failed"
        # Locked: a live order went out; never auto-retry.
        w.tick([_pos()], _SCORES_GOAL, now=101.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert len(ex.calls) == 1

    def test_dry_arm_does_not_lock_position(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_DRYARM)
        acts = w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "dry_arm"
        # Not locked for a future LIVE run...
        assert cashout_state.is_handled("TOK1", db) is False
        # ...but suppressed from re-firing this same run (no spam).
        w.tick([_pos()], _SCORES_GOAL, now=101.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert len(ex.calls) == 1

    def test_no_fill_retries_next_tick(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_NOFILL)
        w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert cashout_state.get_phase("TOK1", db) == "observed"  # not locked
        # A FOK that didn't fill is retried on the next tick.
        w.tick([_pos()], _SCORES_GOAL, now=101.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert len(ex.calls) == 2


class TestWatcherShadow:
    def test_shadow_never_executes_or_claims(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=False), db)
        ex = _exec(_SOLD)
        acts = w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "shadow_sell" and "proposal" in acts[0]
        assert ex.calls == []
        assert cashout_state.is_handled("TOK1", db) is False


class TestWatcherReversal:
    def test_reversal_cancels_pending_sell(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=45, arm=True), db)
        ex = _exec(_SOLD)
        w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        acts = w.tick([_pos()], _SCORES_NIL, now=120.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "reversal"
        # A later genuine kill starts the cooldown afresh, not an instant sell.
        acts2 = w.tick([_pos()], _SCORES_GOAL, now=121.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts2[0]["action"] == "cooldown"
        assert ex.calls == []


class TestWatcherValueGate:
    def test_thin_book_no_value(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True, min_proceeds=1.0), db)
        ex = _exec(_SOLD)
        acts = w.tick([_pos()], _SCORES_GOAL, now=100.0, book_fn=_book_fn(_THIN_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "no_value"
        assert ex.calls == []

    def test_no_score_match_skips(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_SOLD)
        other = [{"home_team": "Spain", "away_team": "Japan",
                  "scores": [{"name": "Spain", "score": "1"}, {"name": "Japan", "score": "0"}]}]
        acts = w.tick([_pos()], other, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts[0]["action"] == "no_match"
        assert ex.calls == []

    def test_non_killable_kind_ignored(self):
        db = _tmp_db()
        w = CashoutWatcher(WatchConfig(var_cooldown_s=0, arm=True), db)
        ex = _exec(_SOLD)
        acts = w.tick(
            [_pos(title="Will Canada win on 2026-06-12?", asset="TW")],
            _SCORES_GOAL, now=100.0, book_fn=_book_fn(_FAT_BOOK), execute_fn=ex)
        assert acts == []
        assert ex.calls == []
