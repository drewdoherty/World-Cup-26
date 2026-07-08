"""Tests for ``scripts/wca_telemetry_report.py`` (read-only reporting CLI).

Covers: withheld-row reason_code breakdown from a bet_recs.json-shaped file,
fill-rate stats from a pm_fill_log.jsonl-shaped file, and the "no data yet"
clean-output behaviour for both sections when their source is missing/empty.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_telemetry_report as report  # noqa: E402


# ---------------------------------------------------------------------------
# Section 1: withheld breakdown.
# ---------------------------------------------------------------------------

class TestWithheldSection:
    def test_missing_file_reports_no_data_yet(self, tmp_path):
        out = report.render_withheld_section(str(tmp_path / "nope.json"))
        assert "no data yet" in out

    def test_empty_withheld_list_reports_no_data_yet(self, tmp_path):
        p = tmp_path / "bet_recs.json"
        p.write_text(json.dumps({"withheld": []}))
        out = report.render_withheld_section(str(p))
        assert "no data yet" in out

    def test_malformed_json_reports_no_data_yet(self, tmp_path):
        p = tmp_path / "bet_recs.json"
        p.write_text("{not json")
        out = report.render_withheld_section(str(p))
        assert "no data yet" in out

    def test_missing_withheld_key_reports_no_data_yet(self, tmp_path):
        p = tmp_path / "bet_recs.json"
        p.write_text(json.dumps({"match_singles": []}))
        out = report.render_withheld_section(str(p))
        assert "no data yet" in out

    def test_counts_by_reason_code(self, tmp_path):
        withheld = [
            {"reason_code": "edge_below_floor"},
            {"reason_code": "edge_below_floor"},
            {"reason_code": "longshot_filter"},
        ]
        p = tmp_path / "bet_recs.json"
        p.write_text(json.dumps({"withheld": withheld}))

        summary = report.summarize_withheld(withheld)
        assert summary["total"] == 3
        assert summary["by_reason_code"]["edge_below_floor"] == 2
        assert summary["by_reason_code"]["longshot_filter"] == 1
        assert summary["missing_reason_code"] == 0

        out = report.render_withheld_section(str(p))
        assert "edge_below_floor" in out
        assert "longshot_filter" in out
        assert "total withheld candidates: 3" in out

    def test_rows_without_reason_code_flagged_not_silently_dropped(self, tmp_path):
        withheld = [
            {"reason_code": "edge_below_floor"},
            {"withheld_reason": "some pre-telemetry row with no reason_code"},
        ]
        p = tmp_path / "bet_recs.json"
        p.write_text(json.dumps({"withheld": withheld}))

        summary = report.summarize_withheld(withheld)
        assert summary["missing_reason_code"] == 1

        out = report.render_withheld_section(str(p))
        assert "1 withheld row(s) have NO reason_code" in out


# ---------------------------------------------------------------------------
# Section 2: fill-rate stats.
# ---------------------------------------------------------------------------

class TestFillLogSection:
    def test_missing_file_reports_no_data_yet(self, tmp_path):
        out = report.render_fill_log_section(str(tmp_path / "nope.jsonl"))
        assert "no data yet" in out

    def test_empty_file_reports_no_data_yet(self, tmp_path):
        p = tmp_path / "fills.jsonl"
        p.write_text("")
        out = report.render_fill_log_section(str(p))
        assert "no data yet" in out

    def _write_rows(self, path: Path, rows) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_fill_rate_counts_matched_and_unmatched_live_orders(self, tmp_path):
        p = tmp_path / "fills.jsonl"
        rows = [
            {"kind": "placed", "order_id": "o1", "dry_run": False},
            {"kind": "placed", "order_id": "o2", "dry_run": False},
            {"kind": "placed", "order_id": None, "dry_run": True},
            {"kind": "fill_observed", "order_id": "o1", "status": "filled"},
        ]
        self._write_rows(p, rows)

        summary = report.summarize_fill_log(rows)
        assert summary["placed_total"] == 3
        assert summary["placed_live"] == 2
        assert summary["placed_dry_run"] == 1
        assert summary["live_orders_with_matched_fill_row"] == 1
        assert summary["live_orders_without_fill_row"] == 1

        out = report.render_fill_log_section(str(p))
        assert "orders placed:        3  (live=2, dry_run=1)" in out
        assert "live orders with a matched fill_observed row: 1 / 2" in out
        assert "live orders with NO fill_observed row" in out

    def test_no_live_orders_reports_no_data_yet_for_fill_rate(self, tmp_path):
        p = tmp_path / "fills.jsonl"
        rows = [{"kind": "placed", "order_id": None, "dry_run": True}]
        self._write_rows(p, rows)
        out = report.render_fill_log_section(str(p))
        assert "live fill-rate: no data yet" in out

    def test_mid_rounding_crossed_percentage(self, tmp_path):
        p = tmp_path / "fills.jsonl"
        rows = [
            {"kind": "mid_rounding", "crossed_to_ask": True, "crossed_to_bid": False},
            {"kind": "mid_rounding", "crossed_to_ask": False, "crossed_to_bid": False},
        ]
        self._write_rows(p, rows)
        summary = report.summarize_fill_log(rows)
        assert summary["mid_rounding_total"] == 2
        assert summary["mid_rounding_crossed"] == 1

        out = report.render_fill_log_section(str(p))
        assert "ROUND_HALF_UP crossed onto touch (1-tick book): 1 / 2 (50.0%)" in out

    def test_no_mid_rounding_rows_reports_no_data_yet(self, tmp_path):
        p = tmp_path / "fills.jsonl"
        rows = [{"kind": "placed", "order_id": "o1", "dry_run": False}]
        self._write_rows(p, rows)
        out = report.render_fill_log_section(str(p))
        assert "mid-rounding tick-snap: no data yet" in out


# ---------------------------------------------------------------------------
# CLI entry point smoke test.
# ---------------------------------------------------------------------------

class TestMainCLI:
    def test_main_runs_clean_with_no_data(self, tmp_path, capsys):
        rc = report.main([
            "--bet-recs", str(tmp_path / "nope.json"),
            "--fill-log", str(tmp_path / "nope.jsonl"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no data yet" in out
        assert "WITHHELD ROWS BY reason_code" in out
        assert "PM FILL LIFECYCLE" in out

    def test_main_runs_clean_with_data(self, tmp_path, capsys):
        bet_recs = tmp_path / "bet_recs.json"
        bet_recs.write_text(json.dumps({
            "withheld": [{"reason_code": "edge_below_floor"}],
        }))
        fill_log = tmp_path / "fills.jsonl"
        fill_log.write_text(json.dumps({"kind": "placed", "order_id": "o1", "dry_run": False}) + "\n")

        rc = report.main(["--bet-recs", str(bet_recs), "--fill-log", str(fill_log)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "edge_below_floor" in out
        assert "orders placed:        1" in out
