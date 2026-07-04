"""wca_merge_freshest: the 'freshest wins' + append-only-union merge driver.

Regression guard for the 2026-07-04 recurring-conflict class: two builders
(CI + a manual dev-box refresh) race the same tracked, daemon-rebuilt data
files. Text merge is meaningless for these (whole file regenerated each
run) — the driver must pick the newer build, never invent a date, and union
(never drop) rows from the append-only prediction log.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "wca_merge_freshest.py"


def _run(ancestor, ours, theirs, orig_path):
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(ancestor), str(ours), str(theirs), orig_path],
        capture_output=True, text=True,
    )


def test_keeps_theirs_when_fresher(tmp_path):
    ancestor = tmp_path / "a.json"; ancestor.write_text("{}")
    ours = tmp_path / "ours.json"
    ours.write_text(json.dumps({"meta": {"generated": "2026-07-04 04:00:00 UTC"}}))
    theirs = tmp_path / "theirs.json"
    theirs.write_text(json.dumps({"meta": {"generated": "2026-07-04 05:16:34 UTC"},
                                  "actionable_count": 19}))
    r = _run(ancestor, ours, theirs, "site/bet_recs.json")
    assert r.returncode == 0
    assert json.loads(ours.read_text())["actionable_count"] == 19


def test_keeps_ours_when_fresher(tmp_path):
    ancestor = tmp_path / "a.json"; ancestor.write_text("{}")
    ours = tmp_path / "ours.json"
    ours.write_text(json.dumps({"meta": {"generated": "2026-07-04 06:00:00 UTC"},
                                "actionable_count": 22}))
    theirs = tmp_path / "theirs.json"
    theirs.write_text(json.dumps({"meta": {"generated": "2026-07-04 05:00:00 UTC"}}))
    r = _run(ancestor, ours, theirs, "site/bet_recs.json")
    assert r.returncode == 0
    assert json.loads(ours.read_text())["actionable_count"] == 22


def test_defers_to_git_when_both_unparseable(tmp_path):
    ancestor = tmp_path / "a.json"; ancestor.write_text("{}")
    ours = tmp_path / "ours.json"; ours.write_text("not json")
    theirs = tmp_path / "theirs.json"; theirs.write_text("also not json")
    r = _run(ancestor, ours, theirs, "site/bet_recs.json")
    assert r.returncode == 1, "must defer to real conflict markers, never guess"


def test_jsonl_unions_without_dropping_rows(tmp_path):
    ancestor = tmp_path / "a.jsonl"; ancestor.write_text("")
    ours = tmp_path / "ours.jsonl"
    ours.write_text('{"generated":"a","x":1}\n{"generated":"b","x":2}\n')
    theirs = tmp_path / "theirs.jsonl"
    theirs.write_text('{"generated":"a","x":1}\n{"generated":"c","x":3}\n')
    r = _run(ancestor, ours, theirs, "data/model_predictions_log.jsonl")
    assert r.returncode == 0
    rows = [json.loads(l) for l in ours.read_text().splitlines()]
    assert [row["x"] for row in rows] == [1, 2, 3]  # union, sorted by generated, no dupes


def test_markdown_generated_header_parsed(tmp_path):
    ancestor = tmp_path / "a.md"; ancestor.write_text("")
    ours = tmp_path / "ours.md"
    ours.write_text("<!-- generated: 2026-07-04T04:00:00 -->\nold card\n")
    theirs = tmp_path / "theirs.md"
    theirs.write_text("<!-- generated: 2026-07-04T05:16:00 -->\nnew card\n")
    r = _run(ancestor, ours, theirs, "data/card_latest.md")
    assert r.returncode == 0
    assert "new card" in ours.read_text()
