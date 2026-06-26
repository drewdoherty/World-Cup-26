"""Tests for the CLV-gated sportsbook-pool bankroll ladder wiring.

``wca.card.resolve_pool_bankroll`` reads the ledger's settled-with-close CLV
statistics (``wca.ledger.reports.staking_stats``), runs the pre-registered
``wca.markets.kelly.KellyPolicy`` ladder to find the earned rung, and maps the
rung index onto the governance bankroll ladder £1500 / £2500 / £5000.

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

from wca.card import (
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
    assert res.kelly_fraction == kelly_mod.KellyPolicy().rungs[expected_rung].fraction
    assert not res.constrained


@pytest.mark.parametrize("n_settled,clv,rolling", [(50, -0.05, -0.05), (100, -0.01, -0.01)])
def test_rung0_negative_clv_constrains_sizing(monkeypatch, n_settled, clv, rolling):
    """Operating rule 1: rung 0 + negative CLV is the minimal-stakes regime.

    The rung stays 0 (negative CLV can never promote), but the *sizing base* is
    clamped to a floor off actual capital and the Kelly fraction is shrunk —
    deliberately, so real money keeps accruing CLV at minimal size rather than
    sizing off the £1,500 notional pool the ledger has not yet earned the right
    to deploy.
    """
    from wca.card import (
        RUNG0_NEGATIVE_CLV_BASE_FRACTION,
        RUNG0_NEGATIVE_CLV_KELLY_FLOOR,
        DEFAULT_ACTUAL_CAPITAL_GBP,
    )

    _patch_stats(monkeypatch, n_settled, clv, rolling)
    res = resolve_pool_bankroll("ignored.db")
    assert res.rung == 0
    assert res.constrained is True
    # Base clamped to the floor (£750 = 25% of £3,000), well below the £1,500 pool.
    expected_base = min(
        LADDER_BANKROLLS[0],
        RUNG0_NEGATIVE_CLV_BASE_FRACTION * DEFAULT_ACTUAL_CAPITAL_GBP,
    )
    assert res.bankroll == expected_base
    assert res.bankroll < LADDER_BANKROLLS[0]
    # Kelly fraction shrunk below the rung-0 (quarter-Kelly) fraction.
    base_fraction = kelly_mod.KellyPolicy().rungs[0].fraction
    assert res.kelly_fraction == base_fraction * RUNG0_NEGATIVE_CLV_KELLY_FLOOR
    assert res.kelly_fraction < base_fraction
    assert "NEGATIVE CLV" in res.constraint_note
    assert res.actual_capital == DEFAULT_ACTUAL_CAPITAL_GBP


def test_demotion_rolling50_negative(monkeypatch):
    """Earned rung 2 but a negative rolling-50 CLV demotes one rung to 1."""
    _patch_stats(monkeypatch, n_settled=120, clv_to_date=0.02, rolling50_clv=-0.01)
    res = resolve_pool_bankroll("ignored.db")
    assert res.rung == 1
    assert res.bankroll == 2500.0
    # Demoted from the rung-2 fraction down to the rung-1 fraction.
    assert res.kelly_fraction == kelly_mod.KellyPolicy().rungs[1].fraction


def test_reason_mentions_rung_and_counts(monkeypatch):
    _patch_stats(monkeypatch, n_settled=0, clv_to_date=None, rolling50_clv=None)
    res = resolve_pool_bankroll("ignored.db")
    assert "rung 0" in res.reason
    assert "0/50" in res.reason          # progress toward the next rung
    assert "1500" in res.reason
    assert "n/a" in res.reason           # CLV not yet available


def test_override_uses_manual_but_reports_earned_rung(monkeypatch):
    """--bankroll override wins, but the reason still names the earned rung."""
    _patch_stats(monkeypatch, n_settled=50, clv_to_date=0.05, rolling50_clv=0.05)
    res = resolve_pool_bankroll("ignored.db", override=750.0)
    assert res.bankroll == 750.0           # manual figure wins
    assert res.rung == 1                   # but the earned rung is still reported
    assert "override" in res.reason.lower()
    assert "2500" in res.reason            # the ladder figure it would have set


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
    assert res.bankroll == 1500.0
    assert res.n_settled == 0


def test_e2e_50_positive_clv_rung1() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 50, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 50
    assert res.clv_to_date is not None and res.clv_to_date > 0
    assert res.rung == 1
    assert res.bankroll == 2500.0


def test_e2e_100_positive_clv_rung2() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 100, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 100
    assert res.rung == 2
    assert res.bankroll == 5000.0


def test_e2e_50_negative_clv_stays_rung0() -> None:
    """50 settled but losing CLV is the minimal-stakes regime: hold rung 0.

    Operating rule 1: the rung stays 0, but the sizing base is clamped to the
    rung-0 negative-CLV floor (£750, off £3,000 actual capital) rather than the
    £1,500 notional pool, and ``constrained`` is flagged.
    """
    from wca.card import (
        RUNG0_NEGATIVE_CLV_BASE_FRACTION,
        DEFAULT_ACTUAL_CAPITAL_GBP,
    )

    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 50, id_tag="N", **_NEG)
    res = resolve_pool_bankroll(db)
    assert res.n_settled == 50
    assert res.clv_to_date is not None and res.clv_to_date < 0
    assert res.rung == 0
    assert res.constrained is True
    assert res.bankroll == min(
        1500.0, RUNG0_NEGATIVE_CLV_BASE_FRACTION * DEFAULT_ACTUAL_CAPITAL_GBP
    )


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
    assert res.bankroll == 2500.0


def test_e2e_override_flag() -> None:
    db = _tmp_db()
    store.init_db(db)
    _add_settled_with_clv(db, 100, id_tag="P", **_POS)
    res = resolve_pool_bankroll(db, override=1234.0)
    assert res.bankroll == 1234.0
    assert res.rung == 2                        # earned rung still surfaced
    assert "override" in res.reason.lower()
