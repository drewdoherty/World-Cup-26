"""Tests for the project-structure analytics generator and bot command."""

import importlib.util
import os
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "wca_structure.py")

_spec = importlib.util.spec_from_file_location("wca_structure", SCRIPT_PATH)
wca_structure = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wca_structure)


def test_metrics_sane_on_this_repo():
    metrics = wca_structure.compute_metrics()
    assert metrics["modules"] > 10
    assert metrics["tests"] > 100
    assert metrics["loc"] > 1000
    assert metrics["data_sources"] == 3
    assert metrics["model_classes"] >= 2
    assert metrics["bot_commands"] >= 5
    # The complexity_index metric has been removed.
    assert "complexity_index" not in metrics


def test_mermaid_contains_existing_pipeline_nodes_only():
    chart = wca_structure.build_mermaid()
    assert chart.startswith("flowchart TD")
    # Modules that exist must appear...
    for node in ("ELO", "DC", "DEVIG", "KELLY", "CARD", "LEDGER", "BOT", "CLI"):
        assert node in chart
    # ...and every edge references declared-and-present nodes only.
    present = {n for n, _label, rel in wca_structure.PIPELINE_NODES
               if os.path.isfile(os.path.join(REPO_ROOT, rel))}
    for line in chart.splitlines():
        if "-->" in line:
            src, dst = [s.strip() for s in line.split("-->")]
            assert src in present and dst in present


def test_readme_marker_replacement_is_idempotent(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "# Title\n\nIntro paragraph.\n\n## Section\n\nBody text.\n",
        encoding="utf-8",
    )
    block = (
        wca_structure.START_MARKER + "\ncontent v1\n" + wca_structure.END_MARKER
    )

    wca_structure.update_readme(str(readme), block)
    first = readme.read_text(encoding="utf-8")
    # Markers inserted after the first heading block (before "## Section").
    assert first.index(wca_structure.START_MARKER) < first.index("## Section")
    assert "content v1" in first

    # Running again with the same block changes nothing.
    wca_structure.update_readme(str(readme), block)
    assert readme.read_text(encoding="utf-8") == first

    # A new block replaces the old one without duplicating markers.
    block2 = (
        wca_structure.START_MARKER + "\ncontent v2\n" + wca_structure.END_MARKER
    )
    wca_structure.update_readme(str(readme), block2)
    second = readme.read_text(encoding="utf-8")
    assert "content v2" in second
    assert "content v1" not in second
    assert second.count(wca_structure.START_MARKER) == 1
    assert second.count(wca_structure.END_MARKER) == 1


def test_history_replaces_same_day_row(tmp_path):
    csv_path = str(tmp_path / "history.csv")
    metrics = wca_structure.compute_metrics()
    rows = wca_structure.update_history(csv_path, metrics, "2026-06-10")
    assert len(rows) == 1
    rows = wca_structure.update_history(csv_path, metrics, "2026-06-10")
    assert len(rows) == 1  # same-day rerun replaces, not appends
    rows = wca_structure.update_history(csv_path, metrics, "2026-06-11")
    assert len(rows) == 2
    assert [r["date"] for r in rows] == ["2026-06-10", "2026-06-11"]
    # With >= 2 rows the README block grows a metrics-over-time history table.
    block = wca_structure.render_readme_block(
        metrics, wca_structure.build_mermaid(), "2026-06-11", rows
    )
    assert "Metrics over time" in block
    assert "Complexity" not in block
    assert "xychart-beta" not in block


def test_bot_structure_dispatch_returns_text(tmp_path):
    from wca.bot.app import dispatch, handle_structure

    reply = dispatch("/structure", db_path=":memory:")
    assert isinstance(reply, str)
    assert reply.strip()

    # With an explicit docs dir containing a snapshot, the reply carries the
    # metrics table but not the Mermaid chart.
    docs = tmp_path / "arch"
    docs.mkdir()
    (docs / "structure_2026-06-11.md").write_text(
        "# Project Structure — 2026-06-11\n\n"
        "## Pipeline\n\n```mermaid\nflowchart TD\n    A --> B\n```\n\n"
        "## Metrics\n\n| Metric | Value |\n| --- | --- |\n| Modules | 15 |\n",
        encoding="utf-8",
    )
    reply = handle_structure(docs_dir=str(docs))
    assert "2026-06-11" in reply
    assert "| Modules | 15 |" in reply
    assert "Complexity" not in reply
    assert "mermaid" not in reply
    assert "flowchart" not in reply

    # HELP_TEXT advertises the command.
    from wca.bot.app import HELP_TEXT
    assert "/structure" in HELP_TEXT
