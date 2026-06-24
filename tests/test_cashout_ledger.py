"""Tests for the Polymarket cash-out ledger path.

Covers :func:`wca.ledger.store.settle_cashout` (full / partial / FIFO split /
untracked-excess / token-vs-selection matching) and that a ``cashed`` row flows
through the realised-P&L aggregations (``summary``, ``bankroll_curve``) while
staying out of CLV. All tests use temporary SQLite files.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from wca.ledger import store, reports


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_cashout_")
    os.close(fd)
    os.unlink(path)
    return path


def _buy(db, *, selection, token_id, price, shares, **kw):
    """Record a Polymarket BUY the way pm_watch/the bot do: stake = shares*price,
    decimal_odds = 1/price."""
    stake = round(shares * price, 6)
    return store.record_bet(
        ts_utc=kw.get("ts_utc", "2026-06-13T18:00:00"),
        match_id=kw.get("match_id", "M1"),
        match_desc=kw.get("match_desc", "United States vs Paraguay"),
        market=kw.get("market", "Exact Score"),
        selection=selection,
        platform="polymarket",
        decimal_odds=round(1.0 / price, 6),
        stake=stake,
        token_id=token_id,
        db_path=db,
    )


def _row(db, bet_id):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM bets WHERE id=?", (bet_id,)).fetchone()
    finally:
        con.close()


def _shares(row):
    return store._row_shares(row["stake"], row["decimal_odds"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_token_id_and_cashout_columns_exist(self):
        db = _tmp_db()
        store.init_db(db)
        store.record_bet(
            ts_utc="2026-06-13T18:00:00", match_id="M", match_desc="A vs B",
            market="Exact Score", selection="A 0-0 B", platform="polymarket",
            decimal_odds=10.0, stake=1.0, token_id="TOK", db_path=db,
        )
        r = _row(db, 1)
        assert r["token_id"] == "TOK"
        assert "cashout_proceeds" in r.keys()


# ---------------------------------------------------------------------------
# Full cash-out
# ---------------------------------------------------------------------------


class TestFullCashout:
    def test_single_row_full_sale_marks_cashed_with_pl(self):
        db = _tmp_db()
        # 100 shares bought @ 0.10 => stake $10. Sell all @ 0.06 => proceeds $6.
        bid = _buy(db, selection="USA 0-0 PAR", token_id="T1", price=0.10, shares=100)
        out = store.settle_cashout(proceeds=6.0, token_id="T1", db_path=db)

        r = _row(db, bid)
        assert r["status"] == "cashed"
        assert r["cashout_proceeds"] == pytest.approx(6.0)
        assert r["settled_pl"] == pytest.approx(6.0 - 10.0)  # -4.0
        assert out["pl"] == pytest.approx(-4.0)
        assert out["cost_basis"] == pytest.approx(10.0)
        assert out["rows_cashed"] == 1
        assert out["rows_split"] == 0
        assert out["untracked_shares"] == pytest.approx(0.0)

    def test_profitable_cashout_positive_pl(self):
        db = _tmp_db()
        bid = _buy(db, selection="Over", token_id="T2", price=0.40, shares=50)  # $20
        out = store.settle_cashout(proceeds=30.0, token_id="T2", db_path=db)  # @0.60
        assert out["pl"] == pytest.approx(10.0)
        assert _row(db, bid)["status"] == "cashed"

    def test_multi_fill_position_all_rows_cashed(self):
        db = _tmp_db()
        # Two fills of the same token (pm_watch records each separately).
        b1 = _buy(db, selection="USA 1-1 PAR", token_id="T3", price=0.15, shares=10)  # $1.5
        b2 = _buy(db, selection="USA 1-1 PAR", token_id="T3", price=0.10, shares=20)  # $2.0
        # 30 shares total; sell all @ 0.05 => $1.50.
        out = store.settle_cashout(proceeds=1.5, token_id="T3", db_path=db)
        assert out["rows_cashed"] == 2
        assert _row(db, b1)["status"] == "cashed"
        assert _row(db, b2)["status"] == "cashed"
        # Cost basis = 3.5; pl = 1.5 - 3.5 = -2.0
        assert out["pl"] == pytest.approx(-2.0)
        # Proceeds allocated by shares: row1 has 10/30, row2 has 20/30.
        assert _row(db, b1)["cashout_proceeds"] == pytest.approx(1.5 * 10 / 30)
        assert _row(db, b2)["cashout_proceeds"] == pytest.approx(1.5 * 20 / 30)

    def test_default_shares_sold_is_whole_position(self):
        db = _tmp_db()
        _buy(db, selection="X", token_id="T4", price=0.20, shares=25)
        out = store.settle_cashout(proceeds=4.0, token_id="T4", db_path=db)
        assert out["shares_sold"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Partial cash-out (FIFO + split)
# ---------------------------------------------------------------------------


class TestPartialCashout:
    def test_partial_within_single_row_splits(self):
        db = _tmp_db()
        # 100 shares @ 0.10 ($10). Sell 40 @ 0.06 => $2.40.
        bid = _buy(db, selection="USA 0-0 PAR", token_id="T5", price=0.10, shares=100)
        out = store.settle_cashout(
            proceeds=2.4, token_id="T5", shares_sold=40, db_path=db
        )
        assert out["rows_split"] == 1
        assert out["rows_cashed"] == 0

        # Original row reduced to the 60-share remainder, still open.
        orig = _row(db, bid)
        assert orig["status"] == "open"
        assert _shares(orig) == pytest.approx(60.0, rel=1e-4)
        assert orig["stake"] == pytest.approx(6.0, rel=1e-4)

        # New cashed slice for 40 shares.
        new_id = out["bet_ids"][0]
        slice_row = _row(db, new_id)
        assert slice_row["status"] == "cashed"
        assert _shares(slice_row) == pytest.approx(40.0, rel=1e-4)
        assert slice_row["cashout_proceeds"] == pytest.approx(2.4)
        assert slice_row["settled_pl"] == pytest.approx(2.4 - 4.0)  # cost of 40 sh = $4

        # Total shares conserved: 60 open + 40 cashed = 100.
        assert _shares(orig) + _shares(slice_row) == pytest.approx(100.0, rel=1e-4)

    def test_partial_spanning_rows_fifo(self):
        db = _tmp_db()
        b1 = _buy(db, selection="Y", token_id="T6", price=0.10, shares=10)  # $1
        b2 = _buy(db, selection="Y", token_id="T6", price=0.10, shares=10)  # $1
        # Sell 15 of 20 @ 0.05 => $0.75. FIFO: b1 fully (10), b2 partially (5).
        out = store.settle_cashout(
            proceeds=0.75, token_id="T6", shares_sold=15, db_path=db
        )
        assert out["rows_cashed"] == 1  # b1
        assert out["rows_split"] == 1   # b2
        assert _row(db, b1)["status"] == "cashed"
        # b2 reduced to remainder 5 shares, still open
        assert _row(db, b2)["status"] == "open"
        assert _shares(_row(db, b2)) == pytest.approx(5.0, rel=1e-4)


# ---------------------------------------------------------------------------
# Untracked excess & matching
# ---------------------------------------------------------------------------


class TestUntrackedAndMatching:
    def test_sell_more_than_ledger_books_excess(self):
        db = _tmp_db()
        # Ledger knows 10 shares ($1). On-chain we actually sell 30 @ 0.05 = $1.50.
        _buy(db, selection="Z", token_id="T7", price=0.10, shares=10)
        out = store.settle_cashout(
            proceeds=1.5, token_id="T7", shares_sold=30, db_path=db
        )
        assert out["untracked_shares"] == pytest.approx(20.0, rel=1e-4)
        # Two cashed rows: the known one + the untracked-excess one.
        assert out["rows_cashed"] == 2
        # Proceeds fully booked (1.5); cost basis only the $1 we recorded.
        assert out["proceeds"] == pytest.approx(1.5)
        assert out["cost_basis"] == pytest.approx(1.0)

    def test_match_by_selection_when_token_absent(self):
        db = _tmp_db()
        # Legacy row with NO token id (pm_watch style).
        store.record_bet(
            ts_utc="2026-06-13T18:00:00", match_id="M", match_desc="A vs B",
            market="Exact Score", selection="A 0-0 B", platform="polymarket",
            decimal_odds=10.0, stake=1.0, db_path=db,
        )
        out = store.settle_cashout(proceeds=0.5, selection="A 0-0 B", db_path=db)
        assert out["rows_cashed"] == 1
        assert out["pl"] == pytest.approx(-0.5)

    def test_no_open_position_raises(self):
        db = _tmp_db()
        store.init_db(db)
        with pytest.raises(KeyError):
            store.settle_cashout(proceeds=1.0, token_id="NOPE", db_path=db)

    def test_requires_token_or_selection(self):
        db = _tmp_db()
        with pytest.raises(ValueError):
            store.settle_cashout(proceeds=1.0, db_path=db)

    def test_negative_proceeds_raises(self):
        db = _tmp_db()
        _buy(db, selection="Z2", token_id="T8", price=0.10, shares=10)
        with pytest.raises(ValueError):
            store.settle_cashout(proceeds=-1.0, token_id="T8", db_path=db)


# ---------------------------------------------------------------------------
# Reporting fold
# ---------------------------------------------------------------------------


class TestReportingFold:
    def test_summary_counts_cashed_pl_and_excludes_from_clv(self):
        db = _tmp_db()
        _buy(db, selection="USA 0-0 PAR", token_id="T9", price=0.10, shares=100)  # $10
        store.settle_cashout(proceeds=6.0, token_id="T9", db_path=db)

        s = reports.summary(db_path=db)
        assert s["cashed_bets"] == 1
        assert s["open_bets"] == 0
        # Realised P&L includes the -4.0 cash-out.
        assert s["total_pl"] == pytest.approx(-4.0)
        assert s["total_staked"] == pytest.approx(10.0)
        # No closing line was set, so it is absent from CLV.
        clv = reports.clv_report(db_path=db)
        assert clv["n_bets"] == 0

    def test_bankroll_curve_includes_cashed(self):
        db = _tmp_db()
        store.add_bankroll_event("2026-06-13T10:00:00", 100.0, "pool=polymarket", db_path=db)
        _buy(db, selection="W", token_id="T10", price=0.40, shares=50)  # $20
        store.settle_cashout(proceeds=30.0, token_id="T10", db_path=db)  # +10
        curve = reports.bankroll_curve(db_path=db)
        # Final bankroll = 100 deposit + 10 cash-out profit.
        assert float(curve["bankroll"].iloc[-1]) == pytest.approx(110.0)
        assert "cashed" in set(curve["event_type"])

    def test_cashed_not_counted_as_open_in_pool_rows(self):
        db = _tmp_db()
        from wca.bot import app as bot
        _buy(db, selection="P", token_id="T11", price=0.10, shares=100)
        store.settle_cashout(proceeds=6.0, token_id="T11", db_path=db)
        pools = bot._pool_rows(db)
        assert pools["polymarket"]["open"] == pytest.approx(0.0)
        assert pools["polymarket"]["settled_pl"] == pytest.approx(-4.0)
