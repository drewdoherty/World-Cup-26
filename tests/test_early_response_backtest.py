from __future__ import annotations

import os
import sqlite3
import sys

import pytest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKTESTS = os.path.join(_REPO_ROOT, "backtests")
if _BACKTESTS not in sys.path:
    sys.path.insert(0, _BACKTESTS)

import early_response_backtest as erb  # noqa: E402


def _make_db(path):
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE bets (
            id INTEGER PRIMARY KEY,
            ts_utc TEXT,
            status TEXT,
            stake REAL,
            settled_pl REAL,
            decimal_odds REAL,
            closing_odds REAL,
            clv REAL,
            ev REAL
        )
        """
    )
    rows = [
        # Overall: stake 100 + 100 + 100 + 50 = 350; P&L 50.47 => 14.42% ROI.
        (1, "2026-06-11T10:00:00", "won", 100.0, 70.0, 1.70, 1.60, 0.0625, -0.01),
        (2, "2026-06-12T10:00:00", "lost", 100.0, -100.0, 2.00, 1.95, 0.0256, 0.07),
        (3, "2026-06-13T10:00:00", "won", 100.0, 83.92, 1.8392, 1.75, 0.0510, 0.15),
        # >=20% cohort: -3.45 / 50 = -6.9%.
        (4, "2026-06-14T10:00:00", "lost", 50.0, -3.45, 2.50, 2.20, 0.1364, 0.25),
        # Excluded: no captured close.
        (5, "2026-06-15T10:00:00", "won", 100.0, 100.0, 2.00, None, None, 0.30),
        # Excluded: open.
        (6, "2026-06-16T10:00:00", "open", 100.0, None, 2.00, 1.90, 0.0526, 0.30),
    ]
    con.executemany(
        """
        INSERT INTO bets
            (id, ts_utc, status, stake, settled_pl, decimal_odds, closing_odds, clv, ev)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    con.close()


def test_synthetic_fixture_reproduces_claims(tmp_path):
    db = tmp_path / "fixture.db"
    _make_db(db)

    result = erb.run_backtest(str(db), min_overall_n=1, min_bucket_n=1)

    assert result.overall.n == 4
    assert result.overall.start_date == "2026-06-11"
    assert result.overall.end_date == "2026-06-14"
    assert result.overall.roi == pytest.approx(0.1442)

    buckets = {b.label: b for b in result.buckets}
    assert buckets["<0%"].n == 1
    assert buckets["5% to <10%"].n == 1
    assert buckets["10% to <20%"].n == 1
    assert buckets[">=20%"].n == 1
    assert buckets[">=20%"].roi == pytest.approx(-0.069)

    assert result.verdict == "reproduced"


def test_missing_db_is_insufficient_sample(tmp_path):
    result = erb.run_backtest(str(tmp_path / "missing.db"))

    assert result.overall.n == 0
    assert result.verdict == "insufficient sample"
