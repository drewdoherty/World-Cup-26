#!/usr/bin/env python
"""Refresh and serve the World Cup Alpha dashboard locally.

This is the private replacement for the old Vercel-first operating mode. It
regenerates the static JSON feeds under ``site/`` and serves that directory on
localhost. No git commands are run and nothing is pushed.
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"


def _run(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def refresh(db_path: str) -> None:
    """Regenerate the static feeds that the terminal site reads."""
    py = sys.executable
    _run([py, "scripts/wca_site.py", "--db", db_path])
    _run([py, "scripts/wca_tracking_data.py", "--db", db_path])
    promos = ROOT / "scripts" / "wca_promos_data.py"
    if promos.exists():
        _run([
            py,
            "scripts/wca_promos_data.py",
            "--db",
            db_path,
            "--scores",
            "site/scores_data.json",
            "--out",
            "site/promos_data.json",
        ])


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib API
        sys.stderr.write("[local-site] " + (fmt % args) + "\n")


def serve(host: str, port: int) -> None:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((host, port), Handler) as httpd:
        print("World Cup Alpha local dashboard: http://%s:%d" % (host, port))
        print("Serving %s" % SITE_DIR)
        httpd.serve_forever()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh and serve the World Cup Alpha dashboard locally."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8742")))
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Serve existing site files without regenerating feeds first.",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="Regenerate feeds and exit without starting the local server.",
    )
    args = parser.parse_args(argv)

    if not args.no_refresh:
        refresh(args.db)
    if args.refresh_only:
        return 0
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

