#!/usr/bin/env python
"""Data-archival CLI: ledger snapshots, model-output capture, and status.

The TEE hooks in the odds / Polymarket / Betfair clients archive each payload
inline as it is ingested. This CLI covers the *scheduled* artifacts that are
not tied to a single fetch:

    # Point-in-time ledger snapshot (gz DB copy + bets parquet) AND capture the
    # current model outputs (card / predictions / advancement) as raw payloads:
    python scripts/wca_archive.py snapshot --db data/wca.db --env .env

    # Files only (no ledger DB) — e.g. from CI where wca.db is unavailable:
    python scripts/wca_archive.py snapshot --no-ledger

    # Where is the archive + what's in it:
    python scripts/wca_archive.py info

Storage target + cloud mirror are configured purely via env vars (see
docs/research/data_archival.md and wca.archive.config). With no cloud creds the
archive degrades to the local directory (WCA_ARCHIVE_DIR, default data/archive).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make ``src`` importable when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (mirrors wca_snapshotd) — secrets stay in the env."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# Model-output files captured verbatim on each scheduled run.
_MODEL_FILES = [
    ("card", "data/card_latest.md"),
    ("next", "data/next_latest.md"),
    ("goalscorers", "data/goalscorers_latest.md"),
    ("predictions", "data/model_predictions.json"),
    ("advancement", "data/advancement_current_vs_pretournament.json"),
    ("advancement_latest", "data/advancement_latest.json"),
]


def _capture_model_files(store) -> int:
    """Archive whatever model outputs exist on disk as raw 'model' payloads."""
    n = 0
    for kind, rel in _MODEL_FILES:
        p = Path(rel)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if rel.endswith(".json"):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {"_text": text}
        else:
            payload = {"_text": text, "_path": rel}
        try:
            wrote = store.write_raw("model", kind, payload, kind=kind)
            n += 1 if wrote else 0
        except Exception as exc:  # noqa: BLE001 — never fail the whole run.
            print("  ! %s archive failed: %s" % (rel, exc), file=sys.stderr)
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="World Cup Alpha data archival.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Ledger snapshot + model-output capture.")
    p_snap.add_argument("--db", default="data/wca.db", help="SQLite ledger path.")
    p_snap.add_argument("--env", default=".env", help="dotenv file to load.")
    p_snap.add_argument("--no-ledger", action="store_true",
                        help="Skip the DB snapshot (e.g. in CI without wca.db).")
    p_snap.add_argument("--no-model", action="store_true",
                        help="Skip capturing model-output files.")

    p_info = sub.add_parser("info", help="Print archive location + dataset counts.")
    p_info.add_argument("--env", default=".env", help="dotenv file to load.")

    args = parser.parse_args(argv)
    _load_dotenv(getattr(args, "env", ".env"))

    from wca.archive.store import ArchiveStore

    store = ArchiveStore.from_env()

    if args.cmd == "info":
        print(json.dumps(store.info(), indent=2))
        return 0

    if args.cmd == "snapshot":
        print("archive root: %s  backend: %s" % (store.root, store.backend.describe()))
        if not args.no_ledger:
            from wca.archive.ledger import snapshot_ledger

            res = snapshot_ledger(db_path=args.db, store=store)
            print("ledger: %d bets -> %s" % (res["n_bets"], res.get("db_gz_rel") or res.get("db_gz")))
        if not args.no_model:
            n = _capture_model_files(store)
            print("model outputs captured: %d file(s)" % n)
        return 0

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
