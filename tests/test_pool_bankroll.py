"""Tests for the CLV-gated sportsbook-pool bankroll ladder wiring.

``wca.card.resolve_pool_bankroll`` reads the ledger's settled-with-close CLV
statistics (``wca.ledger.reports.staking_stats``), runs the pre-registered
``wca.markets.kelly.KellyPolicy`` ladder to find the earned rung, and maps the
rung index onto the governance bankroll ladder £2000 / £3000 / £3000 (flat
quarter-Kelly at every rung; the rung scales the bankroll, not the fraction).

These tests drive the wiring two ways:

* end-to-end against a real temporary SQLite ledger seeded with synthetic
  settled-with-close bets at each rung boundary (50, 100) and in the
  demotion / kill-rule regimes; and
* directly with a stubbed ``staking_stats`` so the exact rung boundaries are
  pinned without inserting hundreds of rows.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _ladder_mode(monkeypatch):
    """These tests pin the CLV-rung LADDER mechanics, which stay intact behind
    the 2026-07-02 FULL-POOL default (WCA_FULL_POOLS=1). Run them in ladder
    mode explicitly; full-pool behaviour has its own tests."""
    monkeypatch.setenv("WCA_FULL_POOLS", "0")


from wca.card import (
    FLAT_KELLY_FRACTION,
    LADDER_BANKROLLS,
    PoolBankroll,
    resolve_pool_bankroll,
)
from wca.ledger import store
from wca.markets import kelly as kelly_mod


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_pool_test_")
    os.close(fd)
    os.unlink(path)  # let SQLite create a fresh file
    return path


def _add_settled_with_clv(
    db: str,
    n: int,
    *,
    taken_odds: float,
    closing_odds: float,
    status: str = "won",
    ts_prefix: str = "2026-06-12T10",
    id_tag: str = "B",
) -> None:
    """Insert ``n`` settled bets each carrying a CLV of taken/closing - 1.

    CLV sign is controlled by the taken vs closing odds (positive when
    ``taken > closing``). All bets are settled so they count toward the ladder.
    """
    for i in range(n):
        bid = store.record_bet(
            ts_utc="%s:%02d:%02d" % (ts_prefix, i // 60 % 60, i % 60),
            match_id="%s_%d" % (id_tag, i),
            match_desc="%s bet %d" % (id_tag, i),
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=taken_odds,
            stake=10.0,
            db_path=db,
        )
        store.settle_bet(bid, status, db_path=db)
        store.set_closing_odds(bid, closing_odds, db_path=db)


# Odds pairs giving a clean CLV sign.
_POS = dict(taken_odds=2.10, closing_odds=2.00)  # CLV = +0.05
_NEG = dict(taken_odds=1.90, closing_odds=2.00)  # CLV = -0.05


# ---------------------------------------------------------------------------
# Stubbed-stats unit tests: pin the exact rung boundaries.
# ---------------------------------------------------------------------------


class _StubStats:
    """Monkeypatch target replacing reports.staking_stats."""

    def __init__(self, n_settled, clv_to_date, rolling50_clv):
        self._d = {
            "n_settled": n_settled,
            "clv_to_date": clv_to_date,
            "rolling50_clv": rolling50_clv,
        }

    def __call__(self, db_path):  # noqa: D401 - signature mirror
        return self._d


def _patch_stats(monkeypatch, n_settled, clv_to_date, rolling50_clv=None):
    # resolve_pool_bankroll imports staking_stats locally from
    # wca.ledger.reports, so patch it there.
    monkeypatch.setattr(
        "wca.ledger.reports.staking_stats",
        _StubStats(n_settled, clv_to_date, rolling50_clv),
    )


@pytest.mark.parametrize(
    "n_settled,clv,rolling,expected_rung",
    [
        (0, None, None, 0),        # no evidence -> rung 0 (CLV not yet negative)
        (49, 0.05, None, 0),       # one short of the rung-1 threshold
        (50, 0.05, 0.05, 1),       # exactly 50 + positive CLV -> rung 1
        (99, 0.05, 0.05, 1),       # one short of the rung-2 threshold
        (100, 0.05, 0.05, 2),      # exactly 100 + positive CLV -> rung 2
    ],
)
def test_rung_boundaries(monkeypatch, n_settled, clv, rolling, expected_rung):
    """Unconstrained rungs size off the full notional ladder pool.

    These rows are all either rung 1+ (earned, positive CLV) or rung 0 with CLV
    not-yet-negative (no evidence / positive), so the rung-0 negative-CLV floor
    does NOT bite and the sizing base equals the notional ladder pool.
    """
    _patch_stats(monkeypatch, n_settled, clv, rolling)
    res = resolve_pool_bankroll("ignored.db")
    assert res.rung == expected_rung
    assert res.bankroll == LADDER_BANKROLLS[expected_rung]
    # Flat quarter-Kelly at every rung (the ladder scales the bankroll, not the
    # Kelly fraction), so this is 0.25 regardless of rung.
    assert res.kelly_fraction == FLAT_KELLY_FRACTION
    assert not res.constrained


@pytest.mark.parametrize("n_settled,clv,rolling", [(50, -0.05, -0.05), (100, -0.01, -0.01)])
def test_rung0_negative_clv_no_shrink(monkeypatch, n_settled, clv, rolling):
    """Rung 0 + negative CLV: NO shrink (user, 2026-06-26).

    Negative CLV holds the rung at 0 (it can never promote), but staking stays a
    flat quarter-Kelly off the full rung-0 bankroll (£2,000) — the old
    minimal-stakes 1/8-Kelly / £750 floor was removed. ``constrained`` is always
    False under the flat-Kelly policy. (Pausing real money is a separate desk
    call, not a silent fractional shrink.)
    """
    from wca.card import DEFAULT_ACTUAL_CAPITAL_GBP

    _patch_stats(monkeypatch, n_settled, clv, rolling)
    res = resolve_pool_bankroll("ignored.db")
    assert res.rung == 0
    assert res.constrained is False
    assert res.bankroll == LADDER_BANKROLLS[0] == 2000.0
    assert res.kelly_fraction == FLAT_KELLY_FRACTION == 0.25
    assert res.constraint_note == ""
    assert res.actual_capital == DEFAULT_ACTUAL_CAPITAL_GBP


def test_demotion_rolling50_negative(monkeypatch):
    """Earned rung 2 but a negative rolling-50 CLV demotes one rung to 1."""
    _patch_stats(monkeypatch, n_settled=120, clv_to_date=0.02, rolling50_clv=-0.01)
    res = resolve_pool_bankroll("ignored.db")
    assert res.rung == 1
    assert res.bankroll == 3000.0
    # Demotion changes the rung (and so the bankroll); the Kelly fraction is a
    # flat 1/4 at every rung.
    assert res.kelly_fraction == FLAT_KELLY_FRACTION


def test_reason_mentions_rung_and_counts(monkeypatch):
    _patch_stats(monkeypatch, n_settled=0, clv_to_date=None, rolling50_clv=None)
    res = resolve_pool_bankroll("ignored.db")
    assert "rung 0" in res.reason
    assert "0/50" in res.reason          # progress toward the next rung
    assert "2000" in res.reason
    assert "n/a" in res.reason           # CLV not yet available


def test_override_uses_manual_but_reports_earned_rung(monkeypatch):
    """--bankroll override wins, but the reason still names the earned rung."""
    _patch_stats(monkeypatch, n_settled=50, clv_to_date=0.05, rolling50_clv=0.05)
    res = resolve_pool_bankroll("ignored.db", override=750.0)
    assert res.bankroll == 750.0           # manual figure wins
    assert res.rung == 1                   # but the earned rung is still reported
    assert "override" in res.reason.lower()
    assert "3000" in res.reason            # the ladder figure it would have set
    assert res.kelly_fraction == FLAT_KELLY_FRACTION  # flat 1/4 even with override


def test_misaligned_bankrolls_raise(monkeypatch):
    _patch_stats(monkeypatch, n_settled=0, clv_to_date=None)
    with pytest.raises(ValueError):
        resolve_pool_bankroll("ignored.db", bankrolls=(1000.0, 2500.0))


def test_returns_pool_bankroll_type(monkeypatch):
    _patch_stats(monkeypatch, n_settled=0, clv_to_date=None)
    res = resolve_pool_bankroll("ignored.db")
    assert isinstance(res, PoolBankroll)
    assert res.n_settled == 0
    assert res.clv_to_date is None


# ---------------------------------------------------------------------------
# End-to-end against a real ledger.
# ---------------------------------------------------------------------------


def test_e2e_empty_ledger_rung0() -> None:
    db = _tmp_db()
    store.init_db(db)
    res = resolve_pool_bankroll(db)
    assert res.rung == 0
    assert res.bankroll == 2000.0
    assert res.n_settled == 0


def test_e2e_50_positive_clv_rung1() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 50, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 50
    assert res.clv_to_date is not None and res.clv_to_date > 0
    assert res.rung == 1
    assert res.bankroll == 3000.0


def test_e2e_100_positive_clv_rung2() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 100, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 100
    assert res.rung == 2
    assert res.bankroll == 3000.0


def test_e2e_50_negative_clv_stays_rung0() -> None:
    """50 settled but losing CLV holds rung 0 — flat 1/4-Kelly, no shrink.

    Negative CLV can never promote, so the rung stays 0 and the bankroll is the
    full rung-0 £2,000 at a flat quarter-Kelly (the old 1/8-Kelly / £750 floor
    was removed by user instruction 2026-06-26). ``constrained`` stays False.
    """
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 50, id_tag="N", **_NEG)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 50
    assert res.clv_to_date is not None and res.clv_to_date < 0
    assert res.rung == 0
    assert res.constrained is False
    assert res.bankroll == 2000.0
    assert res.kelly_fraction == FLAT_KELLY_FRACTION


def test_e2e_demotion_recent_losses() -> None:
    """Positive to-date CLV earns rung 2, but a losing recent-50 demotes to 1.

    100 strongly-positive bets followed by 50 mildly-negative bets keeps the
    overall mean positive (so rung 2 is earned) while the rolling-50 window is
    negative (so the ladder demotes one rung).
    """
    db = _tmp_db()
    store.init_db(db)
    # Older: strong positive CLV (taken 2.40 vs closing 2.00 -> +0.20 each).
    _add_settled_with_clv(
        db, 100, taken_odds=2.40, closing_odds=2.00, id_tag="OLD",
        ts_prefix="2026-06-10T10",
    )
    # Newer: mild negative CLV; inserted later so they own the recent-50 window.
    _add_settled_with_clv(
        db, 50, taken_odds=1.98, closing_odds=2.00, id_tag="NEW",
        ts_prefix="2026-06-12T10",
    )
    stats = __import__(
        "wca.ledger.reports", fromlist=["staking_stats"]
    ).staking_stats(db)
    assert stats["clv_to_date"] > 0           # overall still positive
    assert stats["rolling50_clv"] < 0         # recent window negative
    res = resolve_pool_bankroll(db)
    assert res.rung == 1                       # demoted from 2 to 1
    assert res.bankroll == 3000.0


def test_e2e_override_flag() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 100, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db, override=1234.0)
    assert res.bankroll == 1234.0
    assert res.rung == 2                        # earned rung still surfaced
    assert "override" in res.reason.lower()
