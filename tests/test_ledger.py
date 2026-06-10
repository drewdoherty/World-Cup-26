"""Tests for wca.ledger.store, wca.ledger.reports, and scripts/wca_cli.py.

All tests use temporary SQLite files so they are fully isolated and safe to
run in any environment or order.
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import os
from pathlib import Path

import pytest

from wca.ledger import store, reports


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    """Create a temp file and return its path (the file is left for the OS to
    clean up via the system temp-directory cleanup policy)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_test_")
    os.close(fd)
    # Remove so SQLite can create a fresh file.
    os.unlink(path)
    return path


# ---------------------------------------------------------------------------
# store.py tests.
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_creates_tables(self) -> None:
        db = _tmp_db()
        store.init_db(db)
        conn = __import__("sqlite3").connect(db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "bets" in names
        assert "bankroll_events" in names
        assert "odds_snapshots" in names

    def test_idempotent(self) -> None:
        db = _tmp_db()
        store.init_db(db)
        store.init_db(db)  # second call must not raise


class TestRecordBet:
    def test_basic_insert_returns_id(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="GRP_A_01",
            match_desc="Mexico vs Canada",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.10,
            stake=25.0,
            db_path=db,
        )
        assert isinstance(bet_id, int)
        assert bet_id >= 1

    def test_full_fields(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="GRP_B_02",
            match_desc="USA vs Wales",
            market="1X2",
            selection="Home",
            platform="Paddy Power",
            decimal_odds=1.85,
            stake=50.0,
            model_prob=0.58,
            market_prob_devig=0.55,
            ev=7.5,
            kelly_fraction=0.06,
            notes="Strong model edge",
            db_path=db,
        )
        row = store.get_bet(bet_id, db_path=db)
        assert row is not None
        assert row["match_id"] == "GRP_B_02"
        assert abs(float(row["model_prob"]) - 0.58) < 1e-9
        assert row["status"] == "open"
        assert row["settled_pl"] is None

    def test_sequential_ids(self) -> None:
        db = _tmp_db()
        ids = [
            store.record_bet(
                ts_utc="2026-06-11T14:00:00",
                match_id="M%d" % i,
                match_desc="Match %d" % i,
                market="1X2",
                selection="Home",
                platform="Bet365",
                decimal_odds=2.0,
                stake=10.0,
                db_path=db,
            )
            for i in range(3)
        ]
        assert ids == sorted(ids)
        assert len(set(ids)) == 3


class TestSettleBet:
    def test_settle_won(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.50,
            stake=40.0,
            db_path=db,
        )
        store.settle_bet(bet_id, "won", db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        assert row["status"] == "won"
        # Net profit = (2.50 - 1) * 40 = 60
        assert abs(float(row["settled_pl"]) - 60.0) < 1e-9

    def test_settle_lost(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Away",
            platform="Sky Bet",
            decimal_odds=3.00,
            stake=30.0,
            db_path=db,
        )
        store.settle_bet(bet_id, "lost", db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        assert row["status"] == "lost"
        assert abs(float(row["settled_pl"]) - (-30.0)) < 1e-9

    def test_settle_unknown_result_raises(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.0,
            stake=10.0,
            db_path=db,
        )
        with pytest.raises(ValueError, match="won.*lost"):
            store.settle_bet(bet_id, "push", db_path=db)

    def test_settle_twice_raises(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.0,
            stake=10.0,
            db_path=db,
        )
        store.settle_bet(bet_id, "won", db_path=db)
        with pytest.raises(ValueError, match="open"):
            store.settle_bet(bet_id, "lost", db_path=db)

    def test_settle_nonexistent_raises(self) -> None:
        db = _tmp_db()
        store.init_db(db)
        with pytest.raises(KeyError):
            store.settle_bet(9999, "won", db_path=db)


class TestVoidBet:
    def test_void_sets_pl_zero(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="BTTS",
            selection="Yes",
            platform="Betfair Exchange",
            decimal_odds=1.95,
            stake=20.0,
            db_path=db,
        )
        store.void_bet(bet_id, db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        assert row["status"] == "void"
        assert abs(float(row["settled_pl"])) < 1e-9

    def test_void_nonexistent_raises(self) -> None:
        db = _tmp_db()
        store.init_db(db)
        with pytest.raises(KeyError):
            store.void_bet(9999, db_path=db)


class TestSetClosingOdds:
    def test_clv_positive_when_beat_close(self) -> None:
        """Bet taken at 2.10, closes at 1.90 -> CLV = 2.10/1.90 - 1 > 0."""
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.10,
            stake=25.0,
            db_path=db,
        )
        store.set_closing_odds(bet_id, 1.90, db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        clv = float(row["clv"])
        expected = (2.10 / 1.90) - 1.0
        assert abs(clv - expected) < 1e-9
        assert clv > 0

    def test_clv_negative_when_missed_close(self) -> None:
        """Bet taken at 1.80, closes at 2.20 -> CLV = 1.80/2.20 - 1 < 0."""
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M2",
            match_desc="Test",
            market="1X2",
            selection="Away",
            platform="Virgin Bet",
            decimal_odds=1.80,
            stake=15.0,
            db_path=db,
        )
        store.set_closing_odds(bet_id, 2.20, db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        clv = float(row["clv"])
        assert clv < 0

    def test_invalid_closing_odds_raises(self) -> None:
        db = _tmp_db()
        bet_id = store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M1",
            match_desc="Test",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.0,
            stake=10.0,
            db_path=db,
        )
        with pytest.raises(ValueError):
            store.set_closing_odds(bet_id, 0.5, db_path=db)

    def test_closing_odds_nonexistent_raises(self) -> None:
        db = _tmp_db()
        store.init_db(db)
        with pytest.raises(KeyError):
            store.set_closing_odds(9999, 2.0, db_path=db)


class TestFullLifecycle:
    """record -> settle -> close-odds -> CLV end-to-end."""

    def test_full_lifecycle(self) -> None:
        db = _tmp_db()

        # 1. Record.
        bet_id = store.record_bet(
            ts_utc="2026-06-11T15:00:00",
            match_id="GRP_C_01",
            match_desc="Argentina vs Saudi Arabia",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=1.40,
            stake=100.0,
            model_prob=0.75,
            market_prob_devig=0.72,
            ev=5.0,
            kelly_fraction=0.04,
            db_path=db,
        )
        row = store.get_bet(bet_id, db_path=db)
        assert row["status"] == "open"

        # 2. Set closing odds (line moved in our favour).
        store.set_closing_odds(bet_id, 1.35, db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        expected_clv = (1.40 / 1.35) - 1.0
        assert abs(float(row["clv"]) - expected_clv) < 1e-9

        # 3. Settle won.
        store.settle_bet(bet_id, "won", db_path=db)
        row = store.get_bet(bet_id, db_path=db)
        assert row["status"] == "won"
        expected_pl = (1.40 - 1.0) * 100.0  # 40
        assert abs(float(row["settled_pl"]) - expected_pl) < 1e-9


# ---------------------------------------------------------------------------
# Bankroll events.
# ---------------------------------------------------------------------------


class TestBankrollEvents:
    def test_record_deposit(self) -> None:
        db = _tmp_db()
        ev_id = store.add_bankroll_event(
            ts_utc="2026-06-10T12:00:00",
            amount=1000.0,
            reason="Initial deposit",
            db_path=db,
        )
        events = store.all_bankroll_events(db_path=db)
        assert len(events) == 1
        assert events[0]["id"] == ev_id
        assert abs(float(events[0]["amount"]) - 1000.0) < 1e-9

    def test_record_withdrawal(self) -> None:
        db = _tmp_db()
        store.add_bankroll_event("2026-06-10T12:00:00", 1000.0, db_path=db)
        store.add_bankroll_event("2026-07-01T12:00:00", -200.0, reason="Withdrawal", db_path=db)
        events = store.all_bankroll_events(db_path=db)
        assert len(events) == 2
        assert float(events[1]["amount"]) < 0


# ---------------------------------------------------------------------------
# reports.py tests.
# ---------------------------------------------------------------------------


def _make_settled_bets(
    db: str,
    n_won: int,
    n_lost: int,
    model_prob: float,
    market_prob: float,
    taken_odds: float = 2.0,
    closing_odds: float = 2.0,
) -> None:
    """Insert synthetic won/lost bets for calibration testing."""
    for i in range(n_won):
        bid = store.record_bet(
            ts_utc="2026-06-12T10:00:0%d" % (i % 10),
            match_id="WON_%d" % i,
            match_desc="Won bet %d" % i,
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=taken_odds,
            stake=10.0,
            model_prob=model_prob,
            market_prob_devig=market_prob,
            db_path=db,
        )
        store.settle_bet(bid, "won", db_path=db)
        store.set_closing_odds(bid, closing_odds, db_path=db)
    for i in range(n_lost):
        bid = store.record_bet(
            ts_utc="2026-06-12T11:00:0%d" % (i % 10),
            match_id="LOST_%d" % i,
            match_desc="Lost bet %d" % i,
            market="1X2",
            selection="Away",
            platform="Sky Bet",
            decimal_odds=taken_odds,
            stake=10.0,
            model_prob=model_prob,
            market_prob_devig=market_prob,
            db_path=db,
        )
        store.settle_bet(bid, "lost", db_path=db)
        store.set_closing_odds(bid, closing_odds, db_path=db)


class TestBankrollCurve:
    def test_curve_sums_correctly(self) -> None:
        db = _tmp_db()
        store.add_bankroll_event("2026-06-10T12:00:00", 1000.0, db_path=db)
        _make_settled_bets(db, n_won=2, n_lost=1, model_prob=0.5, market_prob=0.5)

        curve = reports.bankroll_curve(db_path=db)
        # deposit=1000, won bet: (2.0-1)*10=+10 each x2 = +20,  lost = -10
        # final bankroll = 1000 + 20 - 10 = 1010
        final = float(curve["bankroll"].iloc[-1])
        assert abs(final - 1010.0) < 1e-6

    def test_empty_db_returns_empty_df(self) -> None:
        db = _tmp_db()
        curve = reports.bankroll_curve(db_path=db)
        assert len(curve) == 0


class TestOpenExposure:
    def test_shows_only_open_bets(self) -> None:
        db = _tmp_db()
        b1 = store.record_bet(
            ts_utc="2026-06-11T14:00:00", match_id="M1", match_desc="M1",
            market="1X2", selection="Home", platform="Bet365",
            decimal_odds=2.0, stake=30.0, db_path=db,
        )
        b2 = store.record_bet(
            ts_utc="2026-06-11T15:00:00", match_id="M2", match_desc="M2",
            market="1X2", selection="Home", platform="Bet365",
            decimal_odds=2.0, stake=20.0, db_path=db,
        )
        store.settle_bet(b2, "won", db_path=db)

        exposure = reports.open_exposure(db_path=db)
        # Should contain the 1 open bet + 1 TOTAL summary row.
        assert len(exposure) == 2
        total_row = exposure[exposure["match_id"] == "TOTAL"].iloc[0]
        assert abs(float(total_row["stake"]) - 30.0) < 1e-6


class TestClvReport:
    def test_clv_positive_average(self) -> None:
        db = _tmp_db()
        # All bets taken at 2.10 closing at 1.90 -> positive CLV.
        _make_settled_bets(
            db, n_won=3, n_lost=2,
            model_prob=0.55, market_prob=0.53,
            taken_odds=2.10, closing_odds=1.90,
        )
        data = reports.clv_report(db_path=db)
        assert data["n_bets"] == 5
        expected_avg = (2.10 / 1.90) - 1.0
        assert abs(data["avg_clv"] - expected_avg) < 1e-9
        # All bets beat the close.
        assert abs(data["pct_beat_close"] - 1.0) < 1e-9

    def test_empty_returns_nan(self) -> None:
        db = _tmp_db()
        data = reports.clv_report(db_path=db)
        assert data["n_bets"] == 0
        assert math.isnan(data["avg_clv"])
        assert math.isnan(data["pct_beat_close"])


class TestCalibrationReport:
    def test_known_win_rate(self) -> None:
        """50 won + 50 lost with model_prob=0.6 -> obs win rate=0.5, Brier=(0.6-0.5)^2 + (0.6-0)^2... computed over samples."""
        db = _tmp_db()
        _make_settled_bets(db, n_won=50, n_lost=50, model_prob=0.6, market_prob=0.55)
        data = reports.calibration_report(db_path=db, n_bins=5)

        assert data["n_settled"] == 100
        # Brier score for model: mean((0.6 - outcome)^2)
        # 50 won: (0.6 - 1)^2 = 0.16; 50 lost: (0.6 - 0)^2 = 0.36
        # mean = (50*0.16 + 50*0.36) / 100 = 0.26
        assert abs(data["brier_model"] - 0.26) < 1e-9
        # Brier score for market: mean((0.55 - outcome)^2)
        # 50 won: (0.55-1)^2=0.2025; 50 lost: (0.55-0)^2=0.3025
        # mean = (0.2025+0.3025)/2 = 0.2525
        assert abs(data["brier_market"] - 0.2525) < 1e-9

    def test_bins_contain_bets(self) -> None:
        db = _tmp_db()
        _make_settled_bets(db, n_won=10, n_lost=10, model_prob=0.65, market_prob=0.60)
        data = reports.calibration_report(db_path=db, n_bins=5)
        bins = data["calibration_bins"]
        # All bets have model_prob=0.65 -> fall in bin [0.6, 0.8).
        non_empty = bins[bins["n_bets"] > 0]
        assert len(non_empty) == 1
        assert non_empty.iloc[0]["n_bets"] == 20

    def test_empty_db_returns_nan_brier(self) -> None:
        db = _tmp_db()
        data = reports.calibration_report(db_path=db)
        assert math.isnan(data["brier_model"])
        assert math.isnan(data["brier_market"])
        assert data["n_settled"] == 0


class TestSummary:
    def test_summary_fields_present(self) -> None:
        db = _tmp_db()
        store.add_bankroll_event("2026-06-10T12:00:00", 1000.0, db_path=db)
        _make_settled_bets(db, n_won=3, n_lost=2, model_prob=0.55, market_prob=0.52)

        s = reports.summary(db_path=db)
        required_keys = [
            "total_bets", "open_bets", "won_bets", "lost_bets", "void_bets",
            "total_staked", "total_pl", "roi", "avg_clv", "pct_beat_close",
            "brier_model", "brier_market", "total_deposited", "current_bankroll",
        ]
        for k in required_keys:
            assert k in s, "missing key: %s" % k

    def test_summary_pl_correct(self) -> None:
        db = _tmp_db()
        store.add_bankroll_event("2026-06-10T12:00:00", 500.0, db_path=db)
        _make_settled_bets(
            db, n_won=2, n_lost=2,
            model_prob=0.5, market_prob=0.5,
            taken_odds=2.0,
        )
        s = reports.summary(db_path=db)
        # 2 won * (2.0-1)*10 = +20; 2 lost * -10 = -20; net P&L = 0
        assert abs(s["total_pl"]) < 1e-9
        assert abs(s["roi"]) < 1e-9

    def test_empty_db_summary(self) -> None:
        db = _tmp_db()
        s = reports.summary(db_path=db)
        assert s["total_bets"] == 0
        assert s["total_deposited"] == 0.0
        assert s["current_bankroll"] == 0.0


# ---------------------------------------------------------------------------
# CLI smoke tests.
# ---------------------------------------------------------------------------


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
CLI = str(SCRIPTS_DIR / "wca_cli.py")
PYTHON = sys.executable


class TestCliSmoke:
    """Exercise the CLI via subprocess with a temp db."""

    def _run(self, *args: str, db: str) -> subprocess.CompletedProcess:
        cmd = [PYTHON, CLI, "--db", db] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_bankroll_add(self) -> None:
        db = _tmp_db()
        result = self._run(
            "bankroll", "add",
            "--ts", "2026-06-10T12:00:00",
            "--amount", "1000",
            "--reason", "Initial deposit",
            db=db,
        )
        assert result.returncode == 0
        assert "1000" in result.stdout

    def test_bet_add_and_report_summary(self) -> None:
        db = _tmp_db()
        # Add a bet.
        r = self._run(
            "bet", "add",
            "--match-id", "TEST_01",
            "--match-desc", "Test Match",
            "--market", "1X2",
            "--selection", "Home",
            "--platform", "Bet365",
            "--odds", "2.10",
            "--stake", "20",
            "--model-prob", "0.52",
            "--market-prob-devig", "0.49",
            db=db,
        )
        assert r.returncode == 0, r.stderr

        # Report summary.
        r2 = self._run("report", "summary", db=db)
        assert r2.returncode == 0, r2.stderr
        assert "Total bets" in r2.stdout

    def test_bet_settle_and_close_odds(self) -> None:
        db = _tmp_db()
        # Add a bet.
        r = self._run(
            "bet", "add",
            "--match-id", "M1",
            "--match-desc", "Test",
            "--market", "1X2",
            "--selection", "Home",
            "--platform", "Bet365",
            "--odds", "2.10",
            "--stake", "25",
            db=db,
        )
        assert r.returncode == 0, r.stderr
        # Extract the id from output.
        bet_id = int(r.stdout.strip().split("id=")[-1])

        # Set closing odds.
        r2 = self._run("bet", "close-odds", "--id", str(bet_id), "--odds", "1.90", db=db)
        assert r2.returncode == 0, r2.stderr
        assert "CLV" in r2.stdout

        # Settle won.
        r3 = self._run("bet", "settle", "--id", str(bet_id), "--result", "won", db=db)
        assert r3.returncode == 0, r3.stderr
        assert "won" in r3.stdout

    def test_report_clv(self) -> None:
        db = _tmp_db()
        r = self._run("report", "clv", db=db)
        assert r.returncode == 0, r.stderr
        assert "CLV Report" in r.stdout

    def test_report_calibration(self) -> None:
        db = _tmp_db()
        r = self._run("report", "calibration", db=db)
        assert r.returncode == 0, r.stderr
        assert "Calibration" in r.stdout

    def test_cli_main_callable(self) -> None:
        """Ensure main() can be called directly (non-subprocess path)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("wca_cli", CLI)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        db = _tmp_db()
        module.main(["--db", db, "report", "summary"])
