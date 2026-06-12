"""Tests for wca.offers (promo-extraction tracker) and the offer CLI.

Crucially includes the ISOLATION test: recording offers must NOT change the
model-bet ledger (wca.ledger.reports.summary) counts — promo extraction is
tracked apart from model edge / CLV.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from wca import offers
from wca.ledger import store, reports


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_offers_test_")
    os.close(fd)
    os.unlink(path)
    return path


# ---------------------------------------------------------------------------
# CRUD.
# ---------------------------------------------------------------------------


class TestRecordOffer:
    def test_insert_returns_id(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer(
            account_holder="me", bookmaker="Bet365",
            offer_desc="Bet 10 get 30", offer_type="free_snr",
            qualifying_stake=10.0, qualifying_loss=0.54,
            free_bet_value=30.0, lay_venue="smarkets",
            status="qualified", db_path=db,
        )
        assert isinstance(oid, int) and oid >= 1
        row = offers.get_offer(oid, db_path=db)
        assert row["account_holder"] == "me"
        assert row["bookmaker"] == "Bet365"
        assert abs(float(row["free_bet_value"]) - 30.0) < 1e-9
        assert row["status"] == "qualified"

    def test_invalid_status_raises(self) -> None:
        db = _tmp_db()
        with pytest.raises(ValueError):
            offers.record_offer("me", "Bet365", status="bogus", db_path=db)

    def test_defaults_status_claimed(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer("mum", "William Hill", db_path=db)
        row = offers.get_offer(oid, db_path=db)
        assert row["status"] == "claimed"


class TestUpdateOffer:
    def test_update_fields(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer("me", "Bet365", free_bet_value=30.0,
                                  status="qualified", db_path=db)
        offers.update_offer(oid, status="extracted", extracted_value=23.79,
                            db_path=db)
        row = offers.get_offer(oid, db_path=db)
        assert row["status"] == "extracted"
        assert abs(float(row["extracted_value"]) - 23.79) < 1e-9

    def test_update_unknown_field_raises(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer("me", "Bet365", db_path=db)
        with pytest.raises(ValueError):
            offers.update_offer(oid, nonsense=1, db_path=db)

    def test_update_invalid_status_raises(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer("me", "Bet365", db_path=db)
        with pytest.raises(ValueError):
            offers.update_offer(oid, status="bogus", db_path=db)

    def test_update_no_fields_raises(self) -> None:
        db = _tmp_db()
        oid = offers.record_offer("me", "Bet365", db_path=db)
        with pytest.raises(ValueError):
            offers.update_offer(oid, db_path=db)

    def test_update_nonexistent_raises(self) -> None:
        db = _tmp_db()
        offers.init_db(db)
        with pytest.raises(KeyError):
            offers.update_offer(9999, status="extracted", db_path=db)


class TestListOffers:
    def test_list_ordered(self) -> None:
        db = _tmp_db()
        offers.record_offer("me", "Bet365", db_path=db)
        offers.record_offer("mum", "Sky Bet", db_path=db)
        rows = offers.list_offers(db_path=db)
        assert len(rows) == 2
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)

    def test_empty(self) -> None:
        db = _tmp_db()
        assert offers.list_offers(db_path=db) == []


# ---------------------------------------------------------------------------
# Summary math.
# ---------------------------------------------------------------------------


class TestOffersSummary:
    def test_summary_math(self) -> None:
        db = _tmp_db()
        offers.record_offer("me", "Bet365", free_bet_value=30.0,
                            qualifying_loss=0.54, extracted_value=23.79,
                            status="extracted", db_path=db)
        offers.record_offer("me", "Sky Bet", free_bet_value=20.0,
                            qualifying_loss=0.30, extracted_value=16.00,
                            status="extracted", db_path=db)
        offers.record_offer("mum", "William Hill", free_bet_value=40.0,
                            qualifying_loss=1.00, status="qualified", db_path=db)

        s = offers.offers_summary(db_path=db)
        assert s["n_offers"] == 3
        assert s["by_status"] == {"extracted": 2, "qualified": 1}
        assert abs(s["total_free_bet_value"] - 90.0) < 1e-9
        assert abs(s["total_qualifying_loss"] - 1.84) < 1e-9
        assert abs(s["total_extracted"] - 39.79) < 1e-9
        # net_locked = extracted - qualifying_loss = 39.79 - 1.84 = 37.95
        assert abs(s["net_locked"] - 37.95) < 1e-9

    def test_empty_summary(self) -> None:
        db = _tmp_db()
        s = offers.offers_summary(db_path=db)
        assert s["n_offers"] == 0
        assert s["by_status"] == {}
        assert s["net_locked"] == 0.0


# ---------------------------------------------------------------------------
# ISOLATION: offers must not touch the model-bet ledger / CLV.
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_recording_offers_does_not_change_ledger_summary(self) -> None:
        """The core invariant: promo extraction is tracked apart from model edge.

        Recording offers in the SAME db file must leave the bets-ledger summary
        (total/open/won/lost counts, staked, P&L) completely unchanged.
        """
        db = _tmp_db()

        # Seed a real model bet + deposit, snapshot the ledger summary.
        store.add_bankroll_event("2026-06-10T12:00:00", 1000.0, db_path=db)
        bid = store.record_bet(
            ts_utc="2026-06-11T14:00:00", match_id="M1", match_desc="Test",
            market="1X2", selection="Home", platform="Bet365",
            decimal_odds=2.0, stake=25.0, db_path=db,
        )
        store.settle_bet(bid, "won", db_path=db)
        before = reports.summary(db_path=db)

        # Now hammer the offers table in the same db.
        for i in range(5):
            oid = offers.record_offer(
                "me", "Book%d" % i, free_bet_value=30.0,
                qualifying_loss=0.5, extracted_value=23.5,
                status="extracted", db_path=db,
            )
            offers.update_offer(oid, notes="extracted via smarkets", db_path=db)

        after = reports.summary(db_path=db)

        # Every ledger figure must be identical.
        for key in ("total_bets", "open_bets", "won_bets", "lost_bets",
                    "void_bets", "total_staked", "total_pl", "total_deposited",
                    "current_bankroll"):
            assert before[key] == after[key], "ledger key %r changed" % key

    def test_offers_table_separate_from_bets(self) -> None:
        """sb_offers and bets are distinct tables; offers never write to bets."""
        db = _tmp_db()
        offers.record_offer("me", "Bet365", free_bet_value=30.0, db_path=db)

        import sqlite3
        conn = sqlite3.connect(db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        # offers table present; if bets table exists it must be empty.
        assert "sb_offers" in names
        n_offers = conn.execute("SELECT COUNT(*) FROM sb_offers").fetchone()[0]
        assert n_offers == 1
        if "bets" in names:
            n_bets = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
            assert n_bets == 0
        conn.close()


# ---------------------------------------------------------------------------
# CLI smoke tests.
# ---------------------------------------------------------------------------


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
CLI = str(SCRIPTS_DIR / "wca_matched.py")
PYTHON = sys.executable


class TestOfferCliSmoke:
    def _run(self, *args: str, db: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, CLI, "--db", db] + list(args), capture_output=True, text=True
        )

    def test_add_list_update_summary(self) -> None:
        db = _tmp_db()
        r = self._run(
            "offer", "add", "--holder", "me", "--bookmaker", "Bet365",
            "--desc", "Bet 10 get 30", "--type", "free_snr",
            "--qual-stake", "10", "--qual-loss", "0.54",
            "--free-bet", "30", "--lay-venue", "smarkets",
            "--status", "qualified", db=db,
        )
        assert r.returncode == 0, r.stderr
        assert "Recorded offer id=" in r.stdout

        rl = self._run("offer", "list", db=db)
        assert rl.returncode == 0, rl.stderr
        assert "Bet365" in rl.stdout

        ru = self._run("offer", "update", "--id", "1",
                       "--status", "extracted", "--extracted", "23.79", db=db)
        assert ru.returncode == 0, ru.stderr

        rs = self._run("offer", "summary", db=db)
        assert rs.returncode == 0, rs.stderr
        assert "NET locked" in rs.stdout
        assert "23.79" in rs.stdout
