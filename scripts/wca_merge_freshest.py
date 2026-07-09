#!/usr/bin/env python
"""Git merge driver: for daemon-written JSON/markdown data artifacts, keep
whichever side has the FRESHER ``generated``/``meta.generated`` timestamp
instead of raising a text conflict.

Scoped fix (2026-07-04) for the recurring class of merge conflicts on
tracked-but-daemon-written files (``site/bet_recs.json``,
``site/advancement_data.json``, ``data/card_latest.md``,
``data/next_latest.md``, ``data/model_predictions.json``) — these are
rebuilt wholesale on every run by whichever machine/CI job runs the refresh
chain, so a line-level text merge is meaningless; the only sane merge is
"pick the newer build." Wired via ``.gitattributes`` + ``git config
merge.freshest.driver``.

This is a SMALL, safe mitigation, not the full Phase-1 increment 2
(untrack daemon artifacts / detached data branch) — that bigger change
would also change how site data is distributed (each machine's
localhost server serves whatever's committed on main once pulled) and
needs its own sign-off. This driver keeps every file tracked on main;
it only stops git from raising a conflict marker when two builds of
the same artifact diverge.

Usage (as a git merge driver, called by git itself)::

    wca_merge_freshest.py %O %A %B %P

``%O``=ancestor, ``%A``=ours, ``%B``=theirs, ``%P``=original path. Exits 0
and leaves the result in ``%A`` (git's merge-driver contract) whichever way
it decides; exits 1 only if BOTH sides are unparseable (falls back to git's
normal conflict markers by returning non-zero, per merge-driver contract).

The ``model_predictions_log.jsonl`` append-only log is handled separately —
it is NOT a "keep the fresher snapshot" file, it needs a UNION merge (every
row is real history). See ``_union_jsonl`` below; wired via a second
attribute pattern.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional


def _generated_of(path: Path) -> Optional[str]:
    """Extract a comparable timestamp string from a data artifact.

    JSON: ``meta.generated`` / ``meta.generated_at`` / top-level ``generated``.
    Markdown: the ``<!-- generated: ... -->`` header used by ``cardcache``.
    Returns None if the file is missing/unparseable (never invents a date).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix == ".json":
        try:
            d = json.loads(text)
        except Exception:
            return None
        meta = d.get("meta") if isinstance(d, dict) else None
        for src in (meta, d):
            if isinstance(src, dict):
                for k in ("generated", "generated_at", "generated_utc"):
                    v = src.get(k)
                    if v:
                        return str(v)
        return None
    m = re.search(r"<!--\s*generated:\s*([^\s>]+(?:\s[^\s>]+)?)\s*-->", text)
    return m.group(1) if m else None


def _union_jsonl(ancestor: Path, ours: Path, theirs: Path) -> str:
    """Union of unique lines from both sides, sorted by their own
    ``generated`` field when present so the merged log stays chronological."""
    seen = set()
    rows = []
    for p in (ours, theirs):
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln or ln in seen:
                    continue
                seen.add(ln)
                try:
                    rows.append((json.loads(ln).get("generated", ""), ln))
                except Exception:
                    rows.append(("", ln))
        except OSError:
            continue
    rows.sort(key=lambda r: r[0])
    return "\n".join(r[1] for r in rows) + ("\n" if rows else "")


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 4:
        print("usage: wca_merge_freshest.py %O %A %B %P", file=sys.stderr)
        return 1
    ancestor_p, ours_p, theirs_p = (Path(a) for a in argv[:3])
    orig_path = argv[3]

    if str(orig_path).endswith(".jsonl"):
        merged = _union_jsonl(ancestor_p, ours_p, theirs_p)
        ours_p.write_text(merged, encoding="utf-8")
        print("wca_merge_freshest: unioned %s (%d lines)"
              % (orig_path, merged.count("\n")))
        return 0

    ours_gen = _generated_of(ours_p)
    theirs_gen = _generated_of(theirs_p)
    if ours_gen is None and theirs_gen is None:
        # Can't tell which is fresher — defer to git's real conflict markers
        # (never silently pick a side when we have no evidence).
        return 1
    keep_theirs = theirs_gen is not None and (ours_gen is None or theirs_gen > ours_gen)
    if keep_theirs:
        ours_p.write_bytes(theirs_p.read_bytes())
    print("wca_merge_freshest: %s -> kept %s (ours=%r theirs=%r)"
          % (orig_path, "theirs" if keep_theirs else "ours", ours_gen, theirs_gen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
