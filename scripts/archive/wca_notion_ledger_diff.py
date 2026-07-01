#!/usr/bin/env python
"""Read-only reconciliation report: canonical ledger vs the Notion mirror.

Writes NOTHING. Lists bets missing from Notion, orphaned in Notion, and rows whose
status / P&L disagree — so you can see exactly how far the manual Notion import has
drifted from the ledger. Run on the MINI for the canonical ledger; needs a Notion
integration token (NOTION_TOKEN) to read Notion via the REST API.

    NOTION_TOKEN=secret_… PYTHONPATH=src python3 scripts/wca_notion_ledger_diff.py
    PYTHONPATH=src python3 scripts/wca_notion_ledger_diff.py --db data/wca.db
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.ledger import notion_diff as ND  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "wca.db"))
    ap.add_argument("--notion-db", default=ND.NOTION_DB_ID)
    ap.add_argument("--limit", type=int, default=40, help="max rows to list per section")
    args = ap.parse_args(argv)

    ledger = ND.read_ledger(args.db)
    notion = ND.read_notion(args.notion_db)
    if not notion:
        print("⚠ No Notion rows read — set NOTION_TOKEN (integration token with access "
              "to the WCA Bet Ledger DB). Showing ledger-side only.")
    d = ND.diff_ledger_notion(ledger, notion)

    print("\n=== LEDGER ↔ NOTION RECONCILIATION (read-only) ===")
    print("ledger rows: %d   notion rows: %d   %s"
          % (d["ledger_n"], d["notion_n"], "IN SYNC ✅" if d["in_sync"] else "DRIFTED ⚠"))

    miss = d["missing_in_notion"]
    print("\nMissing from Notion: %d" % len(miss))
    for r in miss[:args.limit]:
        print("  #%-4s [%s] %-26s %s" % (r["id"], r.get("status"), str(r.get("match"))[:26],
                                         str(r.get("selection"))[:30]))
    if len(miss) > args.limit:
        print("  …and %d more" % (len(miss) - args.limit))

    orph = d["orphan_in_notion"]
    print("\nOrphaned in Notion (not in ledger): %d" % len(orph))
    for r in orph[:args.limit]:
        print("  #%-4s [%s]" % (r["id"], r.get("status")))

    mm = d["mismatched"]
    print("\nStatus/P&L mismatches: %d" % len(mm))
    for r in mm[:args.limit]:
        print("  #%-4s %s  %s" % (r["id"], r["diffs"], str(r.get("selection"))[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
