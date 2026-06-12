"""CLI: manage model prediction snapshots for historical comparison.

Usage::

    python scripts/wca_snapshots.py list              # List all snapshots
    python scripts/wca_snapshots.py compare A B       # Compare snapshots A and B
    python scripts/wca_snapshots.py create            # Create a new snapshot now
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


SNAPSHOTS_DIR = Path("data/snapshots")
CARD_PATH = Path("data/card_latest.md")
SITE_DATA_PATH = Path("site/data.json")


def list_snapshots() -> List[str]:
    """List all snapshot IDs in reverse chronological order."""
    if not SNAPSHOTS_DIR.exists():
        return []

    snapshots = sorted([d.name for d in SNAPSHOTS_DIR.iterdir() if d.is_dir()])
    return sorted(snapshots, reverse=True)  # newest first


def load_snapshot(snapshot_id: str) -> Dict[str, Any]:
    """Load snapshot metadata and data."""
    snap_dir = SNAPSHOTS_DIR / snapshot_id

    metadata = {}
    if (snap_dir / "metadata.json").exists():
        with open(snap_dir / "metadata.json") as f:
            metadata = json.load(f)

    card_text = ""
    if (snap_dir / "card.md").exists():
        with open(snap_dir / "card.md") as f:
            card_text = f.read()

    site_data = {}
    if (snap_dir / "site_data.json").exists():
        with open(snap_dir / "site_data.json") as f:
            site_data = json.load(f)

    return {
        "snapshot_id": snapshot_id,
        "metadata": metadata,
        "card": card_text,
        "site_data": site_data,
    }


def cmd_list(args: argparse.Namespace) -> None:
    """List all snapshots with metadata."""
    snaps = list_snapshots()

    if not snaps:
        print("No snapshots found.")
        return

    print(f"Found {len(snaps)} snapshot(s):\n")

    for snap_id in snaps:
        snap = load_snapshot(snap_id)
        meta = snap.get("metadata", {})

        ts = meta.get("created_utc", "unknown")
        model_version = meta.get("model_version", "unknown")
        fixtures = meta.get("fixtures_with_predictions", 0)
        positions = meta.get("open_positions", 0)

        print(f"  {snap_id}")
        print(f"    Model: {model_version}, Fixtures: {fixtures}, Open positions: {positions}")


def cmd_create(args: argparse.Namespace) -> None:
    """Create a new snapshot of the current model state."""
    # Generate snapshot ID
    snapshot_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    snap_dir = SNAPSHOTS_DIR / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Copy card and site data
    if CARD_PATH.exists():
        shutil.copy(CARD_PATH, snap_dir / "card.md")

    if SITE_DATA_PATH.exists():
        shutil.copy(SITE_DATA_PATH, snap_dir / "site_data.json")

    # Create metadata
    site_data = {}
    if SITE_DATA_PATH.exists():
        with open(SITE_DATA_PATH) as f:
            site_data = json.load(f)

    metadata = {
        "snapshot_id": snapshot_id,
        "created_utc": datetime.utcnow().isoformat() + "Z",
        "model_version": "v1",
        "blend_weights": {"elo": 0.25, "dixon_coles": 0.25, "market": 0.50},
        "devigging_method": "Shin",
        "kelly_fraction": 0.25,
        "bankroll": site_data.get("totals", {}).get("wagered", 1500.0),
        "fixtures_with_predictions": len(site_data.get("predictions", [])),
        "open_positions": len(site_data.get("positions", [])),
        "closed_positions": len(site_data.get("closed_positions", [])),
        "totals": site_data.get("totals", {}),
        "clv_metrics": site_data.get("clv", {}),
    }

    with open(snap_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Snapshot created: {snapshot_id}")
    print(f"  Location: {snap_dir}")
    print(f"  Fixtures: {metadata['fixtures_with_predictions']}")
    print(f"  Open positions: {metadata['open_positions']}")


def cmd_compare(args: argparse.Namespace) -> None:
    """Compare two snapshots."""
    snap_a_id = args.snapshot_a
    snap_b_id = args.snapshot_b

    snap_a = load_snapshot(snap_a_id)
    snap_b = load_snapshot(snap_b_id)

    if not snap_a.get("metadata"):
        print(f"Error: Snapshot {snap_a_id} not found or incomplete.")
        sys.exit(1)

    if not snap_b.get("metadata"):
        print(f"Error: Snapshot {snap_b_id} not found or incomplete.")
        sys.exit(1)

    meta_a = snap_a["metadata"]
    meta_b = snap_b["metadata"]
    site_a = snap_a["site_data"]
    site_b = snap_b["site_data"]

    print(f"\n=== Snapshot Comparison ===\n")
    print(f"Snapshot A: {snap_a_id}")
    print(f"Snapshot B: {snap_b_id}\n")

    # Metadata comparison
    print("Metadata:")
    print(f"  Model version: {meta_a.get('model_version')} → {meta_b.get('model_version')}")
    print(f"  Fixtures: {meta_a.get('fixtures_with_predictions')} → {meta_b.get('fixtures_with_predictions')}")
    print(f"  Open positions: {meta_a.get('open_positions')} → {meta_b.get('open_positions')}")

    # Totals comparison
    totals_a = meta_a.get("totals", {})
    totals_b = meta_b.get("totals", {})

    print(f"\nTotals (GBP):")
    wagered_a = float(totals_a.get("wagered", 0))
    wagered_b = float(totals_b.get("wagered", 0))
    print(f"  Wagered: £{wagered_a:.2f} → £{wagered_b:.2f} (Δ £{wagered_b - wagered_a:.2f})")

    open_a = float(totals_a.get("open_stake", 0))
    open_b = float(totals_b.get("open_stake", 0))
    print(f"  Open stake: £{open_a:.2f} → £{open_b:.2f} (Δ £{open_b - open_a:.2f})")

    # CLV comparison
    clv_a = meta_a.get("clv_metrics", {})
    clv_b = meta_b.get("clv_metrics", {})

    print(f"\nCLV Metrics:")
    avg_clv_a = clv_a.get("avg_clv") or 0
    avg_clv_b = clv_b.get("avg_clv") or 0
    print(f"  Avg CLV: {avg_clv_a:.4f} → {avg_clv_b:.4f} (Δ {avg_clv_b - avg_clv_a:.4f})")

    # Fixture predictions comparison
    preds_a = site_a.get("predictions", [])
    preds_b = site_b.get("predictions", [])

    print(f"\nPredictions:")
    print(f"  Fixtures: {len(preds_a)} → {len(preds_b)}")

    # Find fixtures in both snapshots and compare
    fixtures_a = {p["fixture"]: p for p in preds_a}
    fixtures_b = {p["fixture"]: p for p in preds_b}

    common = set(fixtures_a.keys()) & set(fixtures_b.keys())
    if common:
        print(f"  Common fixtures ({len(common)}):")
        for fixture in sorted(common)[:5]:  # Show first 5
            fa = fixtures_a[fixture]
            fb = fixtures_b[fixture]
            scores_a = len(fa.get("scores", []))
            scores_b = len(fb.get("scores", []))
            print(f"    • {fixture}: {scores_a} → {scores_b} scorelines")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage model prediction snapshots for historical comparison."
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List all snapshots")
    subparsers.add_parser("create", help="Create a new snapshot of current model state")

    compare = subparsers.add_parser("compare", help="Compare two snapshots")
    compare.add_argument("snapshot_a", help="First snapshot ID (YYYYMMDD-HHMMSS)")
    compare.add_argument("snapshot_b", help="Second snapshot ID (YYYYMMDD-HHMMSS)")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "create":
        cmd_create(args)
    elif args.command == "compare":
        cmd_compare(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
