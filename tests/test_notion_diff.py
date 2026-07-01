"""Tests for the read-only ledger ↔ Notion diff core (wca.ledger.notion_diff)."""

from __future__ import annotations

from wca.ledger import notion_diff as ND


def test_diff_detects_missing_orphan_mismatch():
    ledger = [
        {"id": 1, "status": "won", "pl": 10.0, "match": "A vs B", "selection": "A"},
        {"id": 2, "status": "open", "pl": None, "match": "C vs D", "selection": "C"},
        {"id": 3, "status": "lost", "pl": -5.0, "match": "E vs F", "selection": "E"},
    ]
    notion = [
        {"id": 1, "status": "won", "pl": 10.0},      # exact match
        {"id": 3, "status": "open", "pl": None},      # status + pl mismatch (stale)
        {"id": 9, "status": "won", "pl": 2.0},        # orphan (not in ledger)
    ]
    d = ND.diff_ledger_notion(ledger, notion)
    assert [r["id"] for r in d["missing_in_notion"]] == [2]      # #2 absent from Notion
    assert [r["id"] for r in d["orphan_in_notion"]] == [9]
    assert len(d["mismatched"]) == 1 and d["mismatched"][0]["id"] == 3
    assert "status" in d["mismatched"][0]["diffs"] and "pl" in d["mismatched"][0]["diffs"]
    assert d["in_sync"] is False


def test_in_sync_when_identical():
    rows = [{"id": 1, "status": "won", "pl": 10.0}]
    d = ND.diff_ledger_notion(rows, [{"id": 1, "status": "won", "pl": 10.0}])
    assert d["in_sync"] is True
    assert not d["missing_in_notion"] and not d["orphan_in_notion"] and not d["mismatched"]


def test_pl_tolerance_ignores_rounding():
    d = ND.diff_ledger_notion([{"id": 1, "status": "won", "pl": 10.004}],
                              [{"id": 1, "status": "won", "pl": 10.0}])
    assert d["in_sync"] is True                       # within 0.01 tolerance


def test_read_notion_no_token_returns_empty(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    assert ND.read_notion(token=None) == []
