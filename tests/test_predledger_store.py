"""Tests for wca.predledger.store.

All tests use temporary SQLite files for full isolation.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from wca.predledger import store
from wca.ledger import store as ledger_store


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="wca_predledger_test_")
    os.close(fd)
    os.unlink(path)
    return path


def _pred_row(
    match_id="GRP_A_01",
    market="1X2",
    selection="Home",
    line=-1,
    stage="",
    n_outcomes=3,
    model_prob=0.52,
    offered_odds=2.10,
    devig_method="multiplicative",
    ts_utc="2026-06-11T14:00:00",
    notes=None,
) -> dict:
    return dict(
        match_id=match_id,
        stage=stage,
        market=market,
        selection=selection,
        line=line,
        n_outcomes=n_outcomes,
        model_prob=model_prob,
        offered_odds=offered_odds,
        devig_method=devig_method,
        ts_utc=ts_utc,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# ensure_schema.
# ---------------------------------------------------------------------------


class TestEnsureSchema:
    def test_creates_tables_views_indexes(self) -> None:
        db = _tmp_db()
        store.ensure_schema(db)
        import sqlite3
        conn = sqlite3.connect(db)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        views = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}
        conn.close()
        assert "predictions" in names
        assert "accas" in names
        assert "acca_legs" in names
        assert "schema_meta" in names
        assert "v_model_book" in views
        assert "v_realized_book" in views

    def test_idempotent(self) -> None:
        db = _tmp_db()
        store.ensure_schema(db)
        store.ensure_schema(db)  # second call must not raise

    def test_does_not_touch_bets_or_odds_snapshots(self) -> None:
        db = _tmp_db()
        # Init the existing ledger schema first.
        ledger_store.init_db(db)
        import sqlite3
        conn = sqlite3.connect(db)
        bets_cols_before = {r[1] for r in conn.execute(
            "PRAGMA table_info(bets)"
        ).fetchall()}
        conn.close()

        store.ensure_schema(db)

        conn = sqlite3.connect(db)
        bets_cols_after = {r[1] for r in conn.execute(
            "PRAGMA table_info(bets)"
        ).fetchall()}
        conn.close()
        # ensure_schema must not add or remove columns from bets.
        assert bets_cols_before == bets_cols_after

    def test_schema_meta_version_set(self) -> None:
        db = _tmp_db()
        store.ensure_schema(db)
        import sqlite3
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='predledger_version'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "1"


# ---------------------------------------------------------------------------
# upsert_predictions.
# ---------------------------------------------------------------------------


class TestUpsertPredictions:
    def test_basic_insert_returns_id(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        assert len(ids) == 1
        assert len(ids[0]) == 32  # truncated SHA-256 hex

    def test_idempotent_upsert_single_row(self) -> None:
        """Two upserts of the same row must produce exactly ONE row."""
        db = _tmp_db()
        ids1 = store.upsert_predictions([_pred_row()], db_path=db)
        ids2 = store.upsert_predictions([_pred_row()], db_path=db)
        assert ids1 == ids2

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_null_line_1x2_two_upserts_one_row(self) -> None:
        """Two upserts of a NULL-line (line=-1) 1X2 row → ONE row (PK guard)."""
        db = _tmp_db()
        row = _pred_row(market="1X2", selection="Home", line=-1)
        store.upsert_predictions([row], db_path=db)
        store.upsert_predictions([row], db_path=db)

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_different_selections_produce_different_rows(self) -> None:
        db = _tmp_db()
        rows = [
            _pred_row(selection="Home"),
            _pred_row(selection="Draw"),
            _pred_row(selection="Away"),
        ]
        ids = store.upsert_predictions(rows, db_path=db)
        assert len(set(ids)) == 3

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert count == 3

    def test_upsert_updates_model_prob(self) -> None:
        """Second upsert with updated model_prob overwrites the mutable field."""
        db = _tmp_db()
        row = _pred_row(model_prob=0.50)
        ids = store.upsert_predictions([row], db_path=db)
        pid = ids[0]

        row2 = _pred_row(model_prob=0.60)
        store.upsert_predictions([row2], db_path=db)

        pred = store.get_prediction(pid, db_path=db)
        assert abs(float(pred["model_prob"]) - 0.60) < 1e-9

    def test_upsert_preserves_placed_flag(self) -> None:
        """Upsert after link_bet must not reset placed to 0."""
        db = _tmp_db()
        ledger_store.init_db(db)
        bet_id = ledger_store.record_bet(
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
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        pid = ids[0]
        store.link_bet(pid, bet_id, db_path=db)

        # Re-upsert must not touch placed.
        store.upsert_predictions([_pred_row()], db_path=db)
        pred = store.get_prediction(pid, db_path=db)
        assert pred["placed"] == 1

    def test_futures_row_null_match_id_keyed_on_stage(self) -> None:
        """Futures predictions (match_id=None) are keyed on the stage sentinel."""
        db = _tmp_db()
        row = _pred_row(
            match_id=None,
            stage="GROUP_A_WINNER",
            market="OUTRIGHT",
            selection="France",
            line=-1,
        )
        ids = store.upsert_predictions([row], db_path=db)
        assert len(ids) == 1

        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["match_id"] is None
        assert pred["stage"] == "GROUP_A_WINNER"

    def test_futures_row_null_match_id_idempotent(self) -> None:
        """Two upserts of the same futures row → ONE row."""
        db = _tmp_db()
        row = _pred_row(
            match_id=None,
            stage="SF_WINNER",
            market="OUTRIGHT",
            selection="Brazil",
            line=-1,
        )
        store.upsert_predictions([row], db_path=db)
        store.upsert_predictions([row], db_path=db)

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_model_odds_computed_from_model_prob(self) -> None:
        """model_odds = 1 / model_prob when not supplied explicitly."""
        db = _tmp_db()
        row = _pred_row(model_prob=0.5)
        row.pop("notes", None)
        ids = store.upsert_predictions([row], db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert abs(float(pred["model_odds"]) - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# link_bet.
# ---------------------------------------------------------------------------


class TestLinkBet:
    def test_link_sets_placed(self) -> None:
        """FK link_bet must set placed=1 on the prediction."""
        db = _tmp_db()
        ledger_store.init_db(db)

        bet_id = ledger_store.record_bet(
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
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        pid = ids[0]

        pred_before = store.get_prediction(pid, db_path=db)
        assert pred_before["placed"] == 0

        store.link_bet(pid, bet_id, db_path=db)

        pred_after = store.get_prediction(pid, db_path=db)
        assert pred_after["placed"] == 1
        assert pred_after["bet_id"] == bet_id

    def test_link_nonexistent_prediction_raises(self) -> None:
        db = _tmp_db()
        ledger_store.init_db(db)
        with pytest.raises(KeyError):
            store.link_bet("nonexistent_id_00000000000000", 1, db_path=db)


# ---------------------------------------------------------------------------
# settle_prediction.
# ---------------------------------------------------------------------------


class TestSettlePrediction:
    def test_settle_won(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        store.settle_prediction(ids[0], "won", db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["outcome"] == "won"

    def test_settle_lost(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        store.settle_prediction(ids[0], "lost", db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["outcome"] == "lost"

    def test_settle_void(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        store.settle_prediction(ids[0], "void", db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["outcome"] == "void"

    def test_invalid_outcome_raises(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        with pytest.raises(ValueError, match="won.*lost.*void"):
            store.settle_prediction(ids[0], "push", db_path=db)

    def test_nonexistent_prediction_raises(self) -> None:
        db = _tmp_db()
        store.ensure_schema(db)
        with pytest.raises(KeyError):
            store.settle_prediction("notavalidid0000000000000000000", "won", db_path=db)


# ---------------------------------------------------------------------------
# set_prediction_close.
# ---------------------------------------------------------------------------


class TestSetPredictionClose:
    def test_clv_positive_when_beat_close(self) -> None:
        """offered=2.10, close=1.90 → CLV = 2.10/1.90 - 1 > 0."""
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row(offered_odds=2.10)], db_path=db)
        store.set_prediction_close(ids[0], 1.90, db_path=db)

        pred = store.get_prediction(ids[0], db_path=db)
        expected = (2.10 / 1.90) - 1.0
        assert abs(float(pred["clv"]) - expected) < 1e-9
        assert pred["clv"] > 0

    def test_clv_negative_when_missed_close(self) -> None:
        """offered=1.80, close=2.20 → CLV = 1.80/2.20 - 1 < 0."""
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row(offered_odds=1.80)], db_path=db)
        store.set_prediction_close(ids[0], 2.20, db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["clv"] < 0

    def test_clv_null_when_no_offered_odds(self) -> None:
        """CLV stays NULL when the prediction has no offered_odds."""
        db = _tmp_db()
        row = _pred_row()
        row["offered_odds"] = None
        ids = store.upsert_predictions([row], db_path=db)
        store.set_prediction_close(ids[0], 2.00, db_path=db)
        pred = store.get_prediction(ids[0], db_path=db)
        assert pred["clv"] is None
        assert abs(float(pred["closing_odds"]) - 2.00) < 1e-9

    def test_invalid_closing_odds_raises(self) -> None:
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        with pytest.raises(ValueError):
            store.set_prediction_close(ids[0], 0.9, db_path=db)

    def test_nonexistent_prediction_raises(self) -> None:
        db = _tmp_db()
        store.ensure_schema(db)
        with pytest.raises(KeyError):
            store.set_prediction_close("notavalidid0000000000000000000", 2.0, db_path=db)


# ---------------------------------------------------------------------------
# Views: v_model_book and v_realized_book.
# ---------------------------------------------------------------------------


class TestViews:
    def test_v_model_book_returns_all_predictions(self) -> None:
        db = _tmp_db()
        rows = [
            _pred_row(selection="Home"),
            _pred_row(selection="Draw"),
            _pred_row(selection="Away"),
        ]
        store.upsert_predictions(rows, db_path=db)
        book = store.model_book(db_path=db)
        assert len(book) == 3

    def test_v_realized_book_paper_and_realized(self) -> None:
        """View join returns both paper (no bet) and realized (bet-linked) rows."""
        db = _tmp_db()
        ledger_store.init_db(db)

        # Paper prediction: no bet linked, outcome settled.
        ids_paper = store.upsert_predictions(
            [_pred_row(match_id="M1", selection="Home")], db_path=db
        )
        store.settle_prediction(ids_paper[0], "won", db_path=db)

        # Realized prediction: bet linked, outcome settled.
        bet_id = ledger_store.record_bet(
            ts_utc="2026-06-11T14:00:00",
            match_id="M2",
            match_desc="Test match",
            market="1X2",
            selection="Away",
            platform="Bet365",
            decimal_odds=3.00,
            stake=20.0,
            db_path=db,
        )
        ids_real = store.upsert_predictions(
            [_pred_row(match_id="M2", selection="Away")], db_path=db
        )
        store.link_bet(ids_real[0], bet_id, db_path=db)
        store.settle_prediction(ids_real[0], "lost", db_path=db)

        rows = store.realized_book(db_path=db)
        book_types = {r["prediction_id"]: r["book_type"] for r in rows}

        assert book_types[ids_paper[0]] == "paper"
        assert book_types[ids_real[0]] == "realized"

    def test_v_realized_book_includes_unsettled(self) -> None:
        """v_realized_book shows all predictions, not only settled ones."""
        db = _tmp_db()
        ids = store.upsert_predictions([_pred_row()], db_path=db)
        rows = store.realized_book(db_path=db)
        assert any(r["prediction_id"] == ids[0] for r in rows)


# ---------------------------------------------------------------------------
# upsert_acca / acca_legs.
# ---------------------------------------------------------------------------


class TestAccaLegs:
    def test_acca_legs_reference_valid_predictions(self) -> None:
        """acca_legs must reference existing prediction rows (FK enforced)."""
        db = _tmp_db()
        # Insert 3 predictions.
        rows = [
            _pred_row(match_id="M1", selection="Home"),
            _pred_row(match_id="M2", selection="Away"),
            _pred_row(match_id="M3", selection="Draw"),
        ]
        pred_ids = store.upsert_predictions(rows, db_path=db)

        # Build an acca from those predictions.
        acca_id = store.upsert_acca(pred_ids, ts_utc="2026-06-11T14:00:00", db_path=db)

        # Verify acca_legs rows exist and reference the correct predictions.
        import sqlite3
        conn = sqlite3.connect(db)
        legs = conn.execute(
            "SELECT leg_index, prediction_id FROM acca_legs WHERE acca_id = ? ORDER BY leg_index",
            (acca_id,),
        ).fetchall()
        conn.close()

        assert len(legs) == 3
        leg_pids = {r[1] for r in legs}
        assert leg_pids == set(pred_ids)

    def test_acca_with_invalid_prediction_raises(self) -> None:
        """FK violation when acca_legs references a non-existent prediction."""
        db = _tmp_db()
        store.ensure_schema(db)

        fake_pid = "a" * 32
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_acca([fake_pid], ts_utc="2026-06-11T14:00:00", db_path=db)

    def test_upsert_acca_idempotent(self) -> None:
        """Two upserts of the same acca (same prediction set) → one row."""
        db = _tmp_db()
        pred_ids = store.upsert_predictions(
            [_pred_row(selection="Home"), _pred_row(selection="Away")], db_path=db
        )

        aid1 = store.upsert_acca(pred_ids, ts_utc="2026-06-11T14:00:00", db_path=db)
        aid2 = store.upsert_acca(pred_ids, ts_utc="2026-06-11T14:00:00", db_path=db)
        assert aid1 == aid2

        import sqlite3
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM accas").fetchone()[0]
        conn.close()
        assert count == 1

    def test_acca_same_set_different_order_same_id(self) -> None:
        """Acca id is order-independent (sorted set hash)."""
        db = _tmp_db()
        pred_ids = store.upsert_predictions(
            [_pred_row(selection="Home"), _pred_row(selection="Away")], db_path=db
        )

        aid1 = store.upsert_acca([pred_ids[0], pred_ids[1]], ts_utc="2026-06-11T14:00:00", db_path=db)
        aid2 = store.upsert_acca([pred_ids[1], pred_ids[0]], ts_utc="2026-06-11T14:00:00", db_path=db)
        assert aid1 == aid2


# ---------------------------------------------------------------------------
# Dev-box guard.
# ---------------------------------------------------------------------------


class TestDevBoxGuard:
    def test_wca_db_write_raises_without_env(self, monkeypatch) -> None:
        """Writing to a path ending in wca.db must raise PermissionError."""
        monkeypatch.delenv("WCA_ALLOW_PROD_DB", raising=False)
        with pytest.raises(PermissionError, match="WCA_ALLOW_PROD_DB"):
            store.ensure_schema("data/wca.db")

    def test_wca_db_write_allowed_with_env(self, tmp_path, monkeypatch) -> None:
        """WCA_ALLOW_PROD_DB=1 bypasses the guard."""
        monkeypatch.setenv("WCA_ALLOW_PROD_DB", "1")
        fake_wca = str(tmp_path / "wca.db")
        store.ensure_schema(fake_wca)  # must not raise

    def test_non_prod_db_never_guarded(self) -> None:
        """Temp dev.db paths are never blocked regardless of env."""
        db = _tmp_db()
        store.ensure_schema(db)  # must not raise
