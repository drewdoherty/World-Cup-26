#!/usr/bin/env python3
"""Build + execute the research notebooks.

    ../.venv/bin/python tools/build_notebooks.py            # all, in order
    ../.venv/bin/python tools/build_notebooks.py 02 06      # subset
    ../.venv/bin/python tools/build_notebooks.py --no-exec  # convert only

Sources are jupytext percent-format scripts in notebooks_src/ (reviewable
diffs); this converts each to notebooks/<name>.ipynb and executes it
top-to-bottom with nbclient — the committed .ipynb ALWAYS reflects a clean
full run. Exits non-zero on the first cell error, printing the offending
cell. Execution order follows the numeric prefix so later notebooks can
read datasets earlier ones produced.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import jupytext
from nbclient import NotebookClient

JB = Path(__file__).resolve().parent.parent
SRC = JB / "notebooks_src"
OUT = JB / "notebooks"


def build(name_filter: list, execute: bool = True, timeout: int = 1800) -> int:
    sources = sorted(SRC.glob("*.py"))
    if name_filter:
        sources = [s for s in sources
                   if any(s.name.startswith(f) for f in name_filter)]
    if not sources:
        print(f"no sources matched {name_filter} in {SRC}")
        return 1
    for src in sources:
        t0 = time.time()
        nb = jupytext.read(src)
        nb.metadata.setdefault("kernelspec", {
            "name": "python3", "language": "python", "display_name": "Python 3"})
        out = OUT / (src.stem + ".ipynb")
        if execute:
            print(f"[build] executing {src.name} …", flush=True)
            client = NotebookClient(
                nb, timeout=timeout, kernel_name="python3",
                resources={"metadata": {"path": str(OUT)}})
            try:
                client.execute()
            except Exception as e:  # noqa: BLE001 — report and fail the build
                jupytext.write(nb, out, fmt="ipynb")  # keep partial for debug
                print(f"[build] FAILED {src.name}: {type(e).__name__}: {e}")
                return 2
        jupytext.write(nb, out, fmt="ipynb")
        print(f"[build] ok {out.name}  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("filters", nargs="*", help="numeric prefixes, e.g. 02 06")
    ap.add_argument("--no-exec", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    sys.exit(build(a.filters, execute=not a.no_exec, timeout=a.timeout))
