"""Deterministic overlay that turns the raw martj42 mirror into a *cleaned*
results dataset the model can trust.

The raw download (``data/raw/results.csv``) is treated as an immutable upstream
mirror: :func:`wca.data.results.download_results` re-fetches it daily and would
silently clobber any hand-edits. So corrections live *outside* the raw file, in
``data/corrections.json``, and are re-applied on top to produce
``data/raw/martj42_cleaned.csv`` — the file every consumer actually reads.

Correction record schema (``data/corrections.json``)::

    {
      "corrections": [
        {"date": "2026-06-06", "home_team": "Bermuda", "away_team": "Cape Verde",
         "corrected_home_score": 0, "corrected_away_score": 3,
         "source": "FOX/VAVEL/ESPN",
         # optional, only required for INSERTs of omitted fixtures:
         "tournament": "Friendly", "city": "...", "country": "...",
         "neutral": true}
      ]
    }

The overlay is **idempotent**: applying a correction whose score already matches
is a no-op, so it is safe to run on every CI tick regardless of whether the raw
mirror happens to already carry the fix.

Match key is ``(date, home_team, away_team)`` — the same key the rest of the
pipeline uses. ``home_team``/``away_team`` must be the *canonical* (martj42)
spelling; corrections produced by the reconciliation pipeline pass external
names through :func:`wca.data.teamnames.canonical` first.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

RAW_DEST = "data/raw/results.csv"
CLEANED_DEST = "data/raw/martj42_cleaned.csv"
CORRECTIONS_PATH = "data/corrections.json"

# Column order of the martj42 CSV. Kept explicit so INSERTed rows line up.
COLUMNS = [
    "date", "home_team", "away_team", "home_score", "away_score",
    "tournament", "city", "country", "neutral",
]


# ---------------------------------------------------------------------------
# Corrections IO
# ---------------------------------------------------------------------------

def load_corrections(path: str = CORRECTIONS_PATH) -> List[Dict[str, Any]]:
    """Load the corrections list. Returns ``[]`` if the file is absent."""
    p = Path(path)
    if not p.exists():
        return []
    obj = json.loads(p.read_text())
    if isinstance(obj, dict):
        return list(obj.get("corrections", []))
    if isinstance(obj, list):
        return obj
    raise ValueError(f"unrecognised corrections shape in {path!r}")


def save_corrections(
    corrections: List[Dict[str, Any]], path: str = CORRECTIONS_PATH
) -> None:
    """Write the corrections list back, sorted by date then teams."""
    ordered = sorted(
        corrections,
        key=lambda c: (c.get("date", ""), c.get("home_team", ""), c.get("away_team", "")),
    )
    Path(path).write_text(
        json.dumps({"corrections": ordered}, indent=2, ensure_ascii=False) + "\n"
    )


def merge_correction(
    corrections: List[Dict[str, Any]], new: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], bool]:
    """Insert or update *new* in *corrections* keyed on (date, home, away).

    Returns ``(merged_list, changed)`` where *changed* is False when an
    identical record already existed (so callers can avoid no-op commits).
    """
    key = (new["date"], new["home_team"], new["away_team"])
    out: List[Dict[str, Any]] = []
    replaced = False
    changed = False
    for c in corrections:
        if (c.get("date"), c.get("home_team"), c.get("away_team")) == key:
            replaced = True
            if (c.get("corrected_home_score") != new.get("corrected_home_score")
                    or c.get("corrected_away_score") != new.get("corrected_away_score")):
                changed = True
                out.append(new)
            else:
                out.append(c)  # identical scores -> keep existing record
        else:
            out.append(c)
    if not replaced:
        out.append(new)
        changed = True
    return out, changed


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

def _norm_neutral(v: Any) -> str:
    return "TRUE" if str(v).strip().lower() in ("true", "1", "yes") else "FALSE"


def apply_corrections(
    raw_df: pd.DataFrame, corrections: List[Dict[str, Any]]
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """Apply *corrections* to a raw martj42 frame (read as strings).

    The frame MUST be read with ``dtype=str, keep_default_na=False`` so that
    untouched rows round-trip byte-for-byte (in particular the ``TRUE``/``FALSE``
    casing of the ``neutral`` column).

    Returns ``(cleaned_df, audit)``. Each audit entry records before/after and
    whether the row was an UPDATE or an INSERT.
    """
    df = raw_df.copy()
    audit: List[Dict[str, Any]] = []
    inserts: List[Dict[str, Any]] = []

    for c in corrections:
        d, h, a = c["date"], c["home_team"], c["away_team"]
        hs = str(c["corrected_home_score"])
        as_ = str(c["corrected_away_score"])
        mask = (df["date"] == d) & (df["home_team"] == h) & (df["away_team"] == a)
        n = int(mask.sum())
        if n > 1:
            raise ValueError(f"ambiguous correction key {(d, h, a)} matches {n} rows")
        if n == 1:
            i = df.index[mask][0]
            before = f'{df.at[i, "home_score"]}-{df.at[i, "away_score"]}'
            after = f"{hs}-{as_}"
            if before != after:
                df.at[i, "home_score"] = hs
                df.at[i, "away_score"] = as_
                audit.append({
                    "type": "update", "date": d, "home": h, "away": a,
                    "before": before, "after": after, "source": c.get("source", ""),
                })
        else:
            # INSERT an omitted fixture. Requires tournament metadata.
            row = {
                "date": d, "home_team": h, "away_team": a,
                "home_score": hs, "away_score": as_,
                "tournament": c.get("tournament", "Friendly"),
                "city": c.get("city", ""),
                "country": c.get("country", ""),
                "neutral": _norm_neutral(c.get("neutral", False)),
            }
            inserts.append(row)
            audit.append({
                "type": "insert", "date": d, "home": h, "away": a,
                "before": "(missing)", "after": f"{hs}-{as_}",
                "source": c.get("source", ""),
            })

    if inserts:
        df = pd.concat([df, pd.DataFrame(inserts, columns=COLUMNS)], ignore_index=True)
        # Keep chronological order so downstream "most recent" slices are correct.
        df = df.sort_values("date", kind="stable").reset_index(drop=True)

    return df, audit


def validate(df: pd.DataFrame, raw_df: pd.DataFrame) -> None:
    """Assert the cleaned frame is internally sane. Raises on any violation."""
    # No NEW duplicate keys beyond whatever the raw mirror already carried.
    raw_dups = int(raw_df.duplicated(["date", "home_team", "away_team"]).sum())
    new_dups = int(df.duplicated(["date", "home_team", "away_team"]).sum())
    if new_dups > raw_dups:
        raise ValueError(f"cleaning introduced duplicate keys ({raw_dups} -> {new_dups})")
    # Dates ISO.
    if not df["date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all():
        bad = df.loc[~df["date"].str.match(r"^\d{4}-\d{2}-\d{2}$"), "date"].head().tolist()
        raise ValueError(f"non-ISO dates present: {bad}")
    # Scores: blank or non-negative ints.
    for col in ("home_score", "away_score"):
        s = df[col].astype(str).str.strip()
        bad_mask = ~(s.isin(["", "NA", "nan"]) | s.str.match(r"^\d+$"))
        if bad_mask.any():
            raise ValueError(f"invalid {col} values: {df.loc[bad_mask, col].head().tolist()}")
    # Row count never shrinks.
    if len(df) < len(raw_df):
        raise ValueError(f"cleaned frame lost rows ({len(raw_df)} -> {len(df)})")


def build_cleaned(
    raw_path: str = RAW_DEST,
    corrections_path: str = CORRECTIONS_PATH,
    out_path: str = CLEANED_DEST,
) -> Dict[str, Any]:
    """Read raw + corrections, write the cleaned CSV, return an audit summary."""
    raw_df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    corrections = load_corrections(corrections_path)
    cleaned, audit = apply_corrections(raw_df, corrections)
    validate(cleaned, raw_df)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cleaned.to_csv(out_path, index=False)
    summary = {
        "raw_rows": len(raw_df),
        "cleaned_rows": len(cleaned),
        "corrections_applied": len(audit),
        "updates": sum(1 for a in audit if a["type"] == "update"),
        "inserts": sum(1 for a in audit if a["type"] == "insert"),
        "audit": audit,
        "out_path": out_path,
    }
    logger.info(
        "build_cleaned: %d raw -> %d cleaned (%d updates, %d inserts)",
        summary["raw_rows"], summary["cleaned_rows"],
        summary["updates"], summary["inserts"],
    )
    return summary


def resolve_results_path(prefer_cleaned: bool = True) -> str:
    """Return the path consumers should load.

    Prefers the cleaned dataset when it exists, falling back to the raw mirror
    so nothing breaks in environments (e.g. unit tests) where the cleaned file
    has not been built yet.
    """
    if prefer_cleaned:
        for cand in (CLEANED_DEST, os.path.join(os.getcwd(), CLEANED_DEST)):
            if Path(cand).exists():
                return CLEANED_DEST
    return RAW_DEST
