"""Tests for the data-archival pipeline (wca.archive).

All writes go to a temp directory; no network and no cloud creds are required.
Covers the four load-bearing properties: schema stability, append idempotency,
ledger-snapshot round-trip, and degrade-to-local with no cloud creds — plus the
crash-proof TEE contract (additive, never raises, never mutates wca.db).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pyarrow.dataset as pa_ds
import pytest

from wca.archive import schemas, tee
from wca.archive.backends import LocalBackend, S3Backend, make_backend
from wca.archive.config import ArchiveConfig, CloudConfig
from wca.archive.ledger import snapshot_ledger
from wca.archive.store import ArchiveStore


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> ArchiveStore:
    return ArchiveStore(ArchiveConfig(root=str(tmp_path / "archive")))


def _parts(root: Path, dataset: str):
    return sorted((root / dataset).rglob("*.parquet"))


def _read(root: Path, dataset: str):
    return pa_ds.dataset(str(root / dataset)).to_table()


def _typed(schema) -> list:
    return [(n, str(schema.field(n).type)) for n in schema.names]


# ---------------------------------------------------------------------------
# Schema stability.
# ---------------------------------------------------------------------------


class TestSchemaStability:
    def test_raw_schema_is_fixed_across_varied_payloads(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        # Two structurally different payloads must not change the on-disk schema.
        s.write_raw("oddsapi", "odds", {"a": 1, "nested": {"x": [1, 2]}})
        s.write_raw("polymarket", "events", [{"q": "z"}, {"q": "y", "extra": 9}])
        table = _read(tmp_path / "archive", "raw")
        expected = schemas.build_schema(schemas.RAW_FIELDS)
        assert _typed(table.schema) == _typed(expected)
        assert table.num_rows == 2

    def test_odds_schema_stable_with_missing_columns(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        # A frame missing several optional columns still coerces to the schema.
        df = pd.DataFrame(
            [
                {"event_id": "E1", "market": "h2h", "outcome_name": "Home", "decimal_odds": 2.1},
                {"event_id": "E1", "market": "h2h", "outcome_name": "Away", "decimal_odds": 3.4},
            ]
        )
        s.write_odds(df, "theoddsapi")
        table = _read(tmp_path / "archive", "odds")
        expected = schemas.build_schema(schemas.ODDS_FIELDS)
        assert _typed(table.schema) == _typed(expected)
        assert table.num_rows == 2

    def test_model_schema_and_normalization(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        payload = {
            "meta": {"generated": "2026-06-26T18:30:58"},
            "fixtures": [
                {
                    "match_id": "m1",
                    "fixture": "Norway vs France",
                    "kickoff": "2026-06-26 19:00:00+00:00",
                    "model": {"home": 0.15, "draw": 0.21, "away": 0.64},
                    "lambda_home": 0.8,
                    "lambda_away": 1.56,
                }
            ],
        }
        s.write_model(payload)
        table = _read(tmp_path / "archive", "model_predictions").to_pylist()
        assert _typed(_read(tmp_path / "archive", "model_predictions").schema) == _typed(
            schemas.build_schema(schemas.MODEL_FIELDS)
        )
        assert table[0]["match_id"] == "m1"
        assert table[0]["p_away"] == pytest.approx(0.64)
        # write_model also keeps a verbatim raw copy under venue=model.
        raw = _read(tmp_path / "archive", "raw").to_pylist()
        assert any(r["venue"] == "model" for r in raw)


# ---------------------------------------------------------------------------
# Append idempotency.
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_identical_raw_payload_written_once(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        payload = {"events": [{"id": "x", "price": 0.5}]}
        first = s.write_raw("polymarket", "events", payload)
        second = s.write_raw("polymarket", "events", payload)
        assert first and second == []  # second is a dedup no-op
        assert len(_parts(tmp_path / "archive", "raw")) == 1
        assert _read(tmp_path / "archive", "raw").num_rows == 1

    def test_dedup_survives_new_store_via_manifest(self, tmp_path: Path) -> None:
        payload = {"events": [{"id": "x"}]}
        _store(tmp_path).write_raw("polymarket", "events", payload)
        # A fresh store (cold seen-cache) must read the manifest and still skip.
        again = _store(tmp_path).write_raw("polymarket", "events", payload)
        assert again == []
        assert len(_parts(tmp_path / "archive", "raw")) == 1

    def test_distinct_payloads_both_written(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        s.write_raw("betfair", "listMarketBook", {"v": 1})
        s.write_raw("betfair", "listMarketBook", {"v": 2})
        assert len(_parts(tmp_path / "archive", "raw")) == 2


# ---------------------------------------------------------------------------
# Ledger snapshot round-trip (and non-mutation of the live DB).
# ---------------------------------------------------------------------------


class TestLedgerSnapshot:
    def _seed_db(self, tmp_path: Path) -> str:
        from wca.ledger import store as ledger_store

        db = str(tmp_path / "wca.db")
        ledger_store.record_bet(
            ts_utc="2026-06-26T14:00:00",
            match_id="GRP_A_01",
            match_desc="Mexico vs Canada",
            market="1X2",
            selection="Home",
            platform="Bet365",
            decimal_odds=2.10,
            stake=25.0,
            db_path=db,
        )
        return db

    def test_round_trip_and_source_untouched(self, tmp_path: Path) -> None:
        db = self._seed_db(tmp_path)
        before = hashlib.sha256(Path(db).read_bytes()).hexdigest()

        s = _store(tmp_path)
        res = snapshot_ledger(db_path=db, store=s, ts_utc="2026-06-26T15:00:00")

        # Live DB file bytes must be unchanged (snapshot is read-only).
        after = hashlib.sha256(Path(db).read_bytes()).hexdigest()
        assert before == after

        # bets parquet round-trips the row.
        assert res["n_bets"] == 1
        bets = _read(tmp_path / "archive", "ledger_bets").to_pylist()
        assert bets[0]["selection"] == "Home"
        assert bets[0]["stake"] == pytest.approx(25.0)
        assert bets[0]["bet_market"] == "1X2"
        assert bets[0]["snapshot_ts"] == "2026-06-26T15:00:00"

        # gz DB copy opens and contains the bet.
        gz = Path(res["db_gz"])
        assert gz.exists()
        restored = tmp_path / "restored.db"
        with gzip.open(str(gz), "rb") as f_in:
            restored.write_bytes(f_in.read())
        conn = sqlite3.connect(str(restored))
        n = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
        conn.close()
        assert n == 1

    def test_missing_db_is_graceful(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        res = snapshot_ledger(db_path=str(tmp_path / "nope.db"), store=s)
        assert res["n_bets"] == 0
        assert res["db_gz"] is None


# ---------------------------------------------------------------------------
# Backend selection / degrade-to-local.
# ---------------------------------------------------------------------------


class TestBackendDegrade:
    def test_no_cloud_creds_is_local(self, tmp_path: Path) -> None:
        cfg = ArchiveConfig(root=str(tmp_path), cloud=None)
        assert isinstance(make_backend(cfg), LocalBackend)
        assert make_backend(cfg).has_cloud is False

    def test_partial_cloud_env_yields_no_cloud(self, monkeypatch) -> None:
        monkeypatch.setenv("WCA_ARCHIVE_S3_BUCKET", "b")
        monkeypatch.delenv("WCA_ARCHIVE_S3_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("WCA_ARCHIVE_S3_SECRET_ACCESS_KEY", raising=False)
        cfg = ArchiveConfig.from_env()
        assert cfg.cloud is None
        assert isinstance(make_backend(cfg), LocalBackend)

    def test_complete_cloud_degrades_without_boto3(self, tmp_path: Path, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _no_boto3(name, *a, **k):
            if name == "boto3":
                raise ImportError("simulated: boto3 absent")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_boto3)
        cfg = ArchiveConfig(
            root=str(tmp_path),
            cloud=CloudConfig(bucket="b", access_key_id="k", secret_access_key="s"),
        )
        assert isinstance(make_backend(cfg), LocalBackend)  # degraded

    def test_complete_cloud_uses_s3_when_boto3_present(self, tmp_path: Path) -> None:
        pytest.importorskip("boto3")
        cfg = ArchiveConfig(
            root=str(tmp_path),
            cloud=CloudConfig(bucket="b", access_key_id="k", secret_access_key="s"),
        )
        assert isinstance(make_backend(cfg), S3Backend)

    def test_writes_land_locally_with_no_cloud(self, tmp_path: Path) -> None:
        s = _store(tmp_path)
        assert s.backend.has_cloud is False
        s.write_raw("oddsapi", "odds", {"ok": True})
        assert _parts(tmp_path / "archive", "raw")


# ---------------------------------------------------------------------------
# TEE contract: additive, env-gated, never raises.
# ---------------------------------------------------------------------------


class TestTeeContract:
    def test_tee_writes_locally(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("WCA_ARCHIVE_DIR", str(tmp_path / "arch"))
        monkeypatch.delenv("WCA_ARCHIVE_ENABLED", raising=False)
        tee.reset()
        tee.raw("oddsapi", "odds", {"x": 1})
        assert list((tmp_path / "arch" / "raw").rglob("*.parquet"))
        tee.reset()

    def test_disabled_is_noop(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("WCA_ARCHIVE_DIR", str(tmp_path / "arch"))
        monkeypatch.setenv("WCA_ARCHIVE_ENABLED", "0")
        tee.reset()
        tee.raw("oddsapi", "odds", {"x": 1})
        tee.model_payload({"fixtures": [{"match_id": "m"}]})
        assert not (tmp_path / "arch").exists()
        tee.reset()

    def test_tee_never_raises_on_bad_input(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("WCA_ARCHIVE_DIR", str(tmp_path / "arch"))
        tee.reset()
        # None frame, empty payloads, weird types — all must be silent no-ops.
        tee.odds_frame(None, "theoddsapi")
        tee.model_payload({})
        tee.model_payload(None)
        tee.raw("x", "y", {"set": {1, 2, 3}})  # non-JSON-native -> default=str
        tee.reset()
