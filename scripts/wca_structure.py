#!/usr/bin/env python
"""Project structure analytics generator.

Scans the repository (src/wca/, scripts/, tests/), computes structural
metrics, and emits:

1. ``docs/architecture/structure_<YYYY-MM-DD>.md`` — dated snapshot with a
   Mermaid flowchart of the current pipeline, a metrics table, and a one-line
   complexity index.
2. An auto-managed block in ``README.md`` between the markers
   ``<!-- WCA:STRUCTURE:START -->`` and ``<!-- WCA:STRUCTURE:END -->``.
3. ``docs/architecture/history.csv`` — one row per run date (re-running on
   the same day replaces that day's row), powering a complexity-over-time
   table + Mermaid xychart in the README block once >= 2 rows exist.

Stdlib only. Run from anywhere: paths resolve relative to this file.
"""

from __future__ import annotations

import csv
import datetime
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

START_MARKER = "<!-- WCA:STRUCTURE:START -->"
END_MARKER = "<!-- WCA:STRUCTURE:END -->"

HISTORY_FIELDS = [
    "date", "modules", "loc", "tests", "data_sources", "bot_commands",
    "complexity_index",
]

# Packages whose LOC we report individually (label -> path relative to ROOT).
PACKAGES = [
    ("wca (top-level)", "src/wca"),
    ("wca.data", "src/wca/data"),
    ("wca.models", "src/wca/models"),
    ("wca.markets", "src/wca/markets"),
    ("wca.ledger", "src/wca/ledger"),
    ("wca.bot", "src/wca/bot"),
    ("wca.sim", "src/wca/sim"),
    ("scripts", "scripts"),
    ("tests", "tests"),
]

# External data-source client modules (existence-checked).
DATA_SOURCE_FILES = [
    "src/wca/data/theoddsapi.py",   # TheOddsAPI odds feed
    "src/wca/data/polymarket.py",   # Polymarket Gamma client
    "src/wca/data/results.py",      # martj42 historical results
]

# ---------------------------------------------------------------------------
# Pipeline graph: hardcoded topology, existence-checked per node. Nodes whose
# backing module does not exist are omitted (and so are their edges).
# ---------------------------------------------------------------------------

PIPELINE_NODES = [
    # (node_id, label, path relative to ROOT)
    ("ODDS", "TheOddsAPI odds feed", "src/wca/data/theoddsapi.py"),
    ("POLY", "Polymarket Gamma", "src/wca/data/polymarket.py"),
    ("RES", "martj42 results history", "src/wca/data/results.py"),
    ("SNAP", "Odds snapshots / CLV replay", "src/wca/data/snapshot.py"),
    ("ELO", "Elo ratings", "src/wca/models/elo.py"),
    ("DC", "Dixon-Coles", "src/wca/models/dixon_coles.py"),
    ("DEVIG", "Shin devig", "src/wca/markets/devig.py"),
    ("KELLY", "Quarter-Kelly staking", "src/wca/markets/kelly.py"),
    ("CARD", "Card generator: blend + line shop", "src/wca/card.py"),
    ("SIM", "2026 tournament Monte Carlo", "src/wca/sim/tournament2026.py"),
    ("LEDGER", "Ledger: bets + CLV", "src/wca/ledger/store.py"),
    ("CLI", "CLI wca_cli", "scripts/wca_cli.py"),
    ("BOT", "Telegram bot", "src/wca/bot/app.py"),
]

PIPELINE_EDGES = [
    ("ODDS", "SNAP"),
    ("ODDS", "DEVIG"),
    ("POLY", "DEVIG"),
    ("RES", "ELO"),
    ("RES", "DC"),
    ("ELO", "CARD"),
    ("DC", "CARD"),
    ("DEVIG", "CARD"),
    ("ELO", "SIM"),
    ("DC", "SIM"),
    ("KELLY", "CARD"),
    ("CARD", "LEDGER"),
    ("LEDGER", "CLI"),
    ("LEDGER", "BOT"),
    ("CARD", "CLI"),
    ("CARD", "BOT"),
]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def count_code_lines(path):
    """Code lines in a file: not blank, not pure-comment. Simple heuristic."""
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            n += 1
    return n


def _py_files(rel_dir, recursive=True):
    """Absolute paths of .py files under ROOT/rel_dir."""
    base = os.path.join(ROOT, rel_dir)
    if not os.path.isdir(base):
        return []
    out = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in sorted(filenames):
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                out.append(os.path.join(base, name))
    return sorted(out)


def compute_metrics(root=ROOT):
    """Compute all structure metrics. Returns a plain dict."""
    # Module count: .py files under src/wca + scripts, excluding __init__.py.
    modules = [
        p for p in _py_files("src/wca") + _py_files("scripts")
        if os.path.basename(p) != "__init__.py"
    ]

    # LOC per package (non-recursive for "wca (top-level)" so subpackages
    # are not double-counted).
    loc_per_package = {}
    for label, rel in PACKAGES:
        recursive = label != "wca (top-level)"
        files = _py_files(rel, recursive=recursive)
        loc_per_package[label] = sum(count_code_lines(p) for p in files)
    total_loc = sum(
        count_code_lines(p)
        for p in _py_files("src/wca") + _py_files("scripts") + _py_files("tests")
    )

    # Test count: occurrences of "def test_" under tests/.
    tests = 0
    for p in _py_files("tests"):
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            tests += len(re.findall(r"\bdef test_", fh.read()))

    # Data sources: existence-checked external client modules.
    data_sources = sum(
        1 for rel in DATA_SOURCE_FILES if os.path.isfile(os.path.join(root, rel))
    )

    # Model classes: top-level class definitions in src/wca/models.
    model_classes = 0
    for p in _py_files("src/wca/models"):
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            model_classes += len(re.findall(r"^class \w+", fh.read(), re.M))

    # Bot commands: parse HELP_TEXT entries ("/cmd — ...") in bot/app.py.
    bot_commands = 0
    app_path = os.path.join(root, "src/wca/bot/app.py")
    if os.path.isfile(app_path):
        with open(app_path, "r", encoding="utf-8", errors="replace") as fh:
            bot_commands = len(set(re.findall(r'"/(\w+) ', fh.read())))

    metrics = {
        "modules": len(modules),
        "loc": total_loc,
        "loc_per_package": loc_per_package,
        "tests": tests,
        "data_sources": data_sources,
        "model_classes": model_classes,
        "bot_commands": bot_commands,
    }
    metrics["complexity_index"] = complexity_index(metrics)
    return metrics


def complexity_index(metrics):
    """Weighted structural complexity: modules + tests/10 + data_sources*2."""
    return round(
        metrics["modules"] + metrics["tests"] / 10.0 + metrics["data_sources"] * 2,
        1,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def build_mermaid(root=ROOT):
    """Mermaid flowchart of the current pipeline (existing modules only)."""
    present = {
        node_id: label
        for node_id, label, rel in PIPELINE_NODES
        if os.path.isfile(os.path.join(root, rel))
    }
    lines = ["flowchart TD"]
    for node_id, label, _rel in PIPELINE_NODES:
        if node_id in present:
            lines.append('    %s["%s"]' % (node_id, label))
    for src, dst in PIPELINE_EDGES:
        if src in present and dst in present:
            lines.append("    %s --> %s" % (src, dst))
    return "\n".join(lines)


def render_metrics_table(metrics):
    rows = [
        ("Modules (src + scripts, excl. __init__)", "%d" % metrics["modules"]),
        ("Code lines (LOC, total)", "%d" % metrics["loc"]),
    ]
    for label, _rel in PACKAGES:
        rows.append(
            ("LOC: %s" % label, "%d" % metrics["loc_per_package"].get(label, 0))
        )
    rows += [
        ("Tests (def test_)", "%d" % metrics["tests"]),
        ("Data sources", "%d" % metrics["data_sources"]),
        ("Model classes", "%d" % metrics["model_classes"]),
        ("Bot commands", "%d" % metrics["bot_commands"]),
    ]
    out = ["| Metric | Value |", "| --- | --- |"]
    for name, value in rows:
        out.append("| %s | %s |" % (name, value))
    return "\n".join(out)


def render_complexity_line(metrics):
    return (
        "**Complexity index: %.1f** "
        "(modules + tests/10 + data sources × 2)" % metrics["complexity_index"]
    )


def render_history_section(rows):
    """Markdown table + xychart-beta for the run history (>= 2 rows)."""
    out = ["### Complexity over time", ""]
    out.append("| Date | Modules | LOC | Tests | Data sources | Bot commands | Complexity |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        out.append(
            "| %s | %s | %s | %s | %s | %s | %s |"
            % (
                r["date"], r["modules"], r["loc"], r["tests"],
                r["data_sources"], r["bot_commands"], r["complexity_index"],
            )
        )
    out.append("")
    out.append("```mermaid")
    out.append("xychart-beta")
    out.append('    title "Complexity index over time"')
    out.append(
        "    x-axis [%s]" % ", ".join('"%s"' % r["date"] for r in rows)
    )
    values = [float(r["complexity_index"]) for r in rows]
    lo = min(values)
    hi = max(values)
    pad = max((hi - lo) * 0.2, 1.0)
    out.append(
        '    y-axis "Complexity index" %.1f --> %.1f' % (lo - pad, hi + pad)
    )
    out.append("    line [%s]" % ", ".join("%.1f" % v for v in values))
    out.append("```")
    return "\n".join(out)


def render_snapshot(metrics, mermaid, date_str):
    """Full dated snapshot markdown document."""
    return (
        "# Project Structure — %s\n\n"
        "Auto-generated by `scripts/wca_structure.py`. Do not edit by hand.\n\n"
        "## Pipeline\n\n"
        "```mermaid\n%s\n```\n\n"
        "## Metrics\n\n"
        "%s\n\n"
        "%s\n" % (date_str, mermaid, render_metrics_table(metrics),
                  render_complexity_line(metrics))
    )


def render_readme_block(metrics, mermaid, date_str, history_rows):
    parts = [
        START_MARKER,
        "## Project Structure Analytics",
        "",
        "_Auto-generated by `scripts/wca_structure.py` on %s. Do not edit this "
        "block by hand._" % date_str,
        "",
        "```mermaid",
        mermaid,
        "```",
        "",
        render_metrics_table(metrics),
        "",
        render_complexity_line(metrics),
    ]
    if len(history_rows) >= 2:
        parts += ["", render_history_section(history_rows)]
    parts.append(END_MARKER)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def update_history(csv_path, metrics, date_str):
    """Append today's row (or replace it if the date already exists)."""
    rows = load_history(csv_path)
    rows = [r for r in rows if r["date"] != date_str]
    rows.append(
        {
            "date": date_str,
            "modules": str(metrics["modules"]),
            "loc": str(metrics["loc"]),
            "tests": str(metrics["tests"]),
            "data_sources": str(metrics["data_sources"]),
            "bot_commands": str(metrics["bot_commands"]),
            "complexity_index": "%.1f" % metrics["complexity_index"],
        }
    )
    rows.sort(key=lambda r: r["date"])
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def load_history(csv_path):
    if not os.path.isfile(csv_path):
        return []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def update_readme(readme_path, block):
    """Replace the marker block in README, inserting markers if absent.

    Idempotent: running twice with the same block leaves the file unchanged.
    """
    with open(readme_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    if START_MARKER in text and END_MARKER in text:
        pattern = re.compile(
            re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER), re.S
        )
        new_text = pattern.sub(lambda _m: block, text, count=1)
    else:
        # Insert after the first heading block (i.e. just before the second
        # heading), or append at the end if there is only one heading.
        lines = text.splitlines(True)
        heading_idxs = [
            i for i, line in enumerate(lines) if line.startswith("#")
        ]
        if len(heading_idxs) >= 2:
            insert_at = heading_idxs[1]
            new_text = (
                "".join(lines[:insert_at])
                + block + "\n\n"
                + "".join(lines[insert_at:])
            )
        else:
            new_text = text.rstrip("\n") + "\n\n" + block + "\n"

    if new_text != text:
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write(new_text)


def generate(root=ROOT, date_str=None):
    """Run the full generation pipeline. Returns the metrics dict."""
    if date_str is None:
        date_str = datetime.date.today().isoformat()

    metrics = compute_metrics(root)
    mermaid = build_mermaid(root)

    arch_dir = os.path.join(root, "docs", "architecture")
    os.makedirs(arch_dir, exist_ok=True)

    snapshot_path = os.path.join(arch_dir, "structure_%s.md" % date_str)
    with open(snapshot_path, "w", encoding="utf-8") as fh:
        fh.write(render_snapshot(metrics, mermaid, date_str))

    history_rows = update_history(
        os.path.join(arch_dir, "history.csv"), metrics, date_str
    )

    block = render_readme_block(metrics, mermaid, date_str, history_rows)
    update_readme(os.path.join(root, "README.md"), block)

    return metrics


def main():
    metrics = generate()
    print("Structure snapshot written.")
    print(
        "modules=%d loc=%d tests=%d data_sources=%d model_classes=%d "
        "bot_commands=%d complexity_index=%.1f"
        % (
            metrics["modules"], metrics["loc"], metrics["tests"],
            metrics["data_sources"], metrics["model_classes"],
            metrics["bot_commands"], metrics["complexity_index"],
        )
    )


if __name__ == "__main__":
    main()
