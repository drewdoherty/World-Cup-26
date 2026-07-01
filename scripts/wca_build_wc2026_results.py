#!/usr/bin/env python
"""Derive ``data/processed/wc2026_results.json`` from the authoritative feed.

The processed results file is consumed by the settlement / backfill / win-rate /
rigor pipelines (:mod:`wca.predledger.settle`, :mod:`wca.predledger.backfill`,
:mod:`wca.winrate`, :mod:`wca.rigor.build`) and the card's WC level anchor
(:mod:`wca.card`).  Historically it was *hand-maintained* — one row per finished
fixture — which let it drift stale (a frozen 31-match subset) while the cleaned
martj42 dataset (``data/raw/martj42_cleaned.csv``, the same source ``/card`` and
every model consumer reads via ``wca.data.cleaning``) carried every played
match.

This script makes the processed file a *derived* artefact: it reads the played
WC2026 matches straight from ``martj42_cleaned.csv`` and emits the exact schema
the consumers expect — ``{"results": [{date, fixture, kickoff_utc, score,
outcome}], "_comment": ...}``.

No fabrication
--------------
* ``date`` / ``score`` come straight from the cleaned dataset.
* ``outcome`` (home/draw/away) is *computed* from the score.
* ``fixture`` is ``"<home> vs <away>"`` with both names canonicalised via
  :func:`wca.data.teamnames.canonical` (the spelling every consumer keys on).
* ``kickoff_utc`` is carried over from a *real* source only — the existing
  hand-maintained file (authoritative for the matches it covers) or the
  model-predictions log (the kickoff the system actually used pre-match).  When
  neither has it the field is **omitted**, never invented; the consumers fall
  back to ``date`` (see ``wca.tracking`` line ~659).

Played-match filter (matches the audit's definition)
----------------------------------------------------
A row is a played WC2026 match iff ``date >= 2026-06-11`` **and** its
``tournament`` contains ``"World"`` **and** both ``home_score`` / ``away_score``
parse as integers.

Usage
-----
    PYTHONPATH=src python scripts/wca_build_wc2026_results.py \
        [--src data/raw/martj42_cleaned.csv] \
        [--out data/processed/wc2026_results.json] \
        [--prev data/processed/wc2026_results.json] \
        [--log data/model_predictions_log.jsonl]

``--prev`` defaults to ``--out`` so an in-place refresh preserves the existing
hand-maintained ``kickoff_utc`` values.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca.data.teamnames import canonical  # noqa: E402

# Played-match filter constants (mirror the audit definition).
WC2026_START_DATE = "2026-06-11"
TOURNAMENT_SUBSTR = "World"

_DEFAULT_SRC = "data/raw/martj42_cleaned.csv"
_DEFAULT_OUT = "data/processed/wc2026_results.json"
_DEFAULT_LOG = "data/model_predictions_log.jsonl"

_COMMENT = (
    "Auto-derived from data/raw/martj42_cleaned.csv by "
    "scripts/wca_build_wc2026_results.py. One row per PLAYED 2026 World Cup "
    "fixture (date>=2026-06-11, tournament contains 'World', numeric scores). "
    "outcome (home/draw/away) is computed from the score; fixture names are "
    "canonicalised. kickoff_utc is carried from a real source (the prior file "
    "or the model-predictions log) only — omitted, never invented, when "
    "unavailable; consumers fall back to date. Re-run this script to refresh; "
    "do not hand-edit."
)


def _is_int(value: Any) -> bool:
    try:
        int(str(value).strip())
        return True
    except (TypeError, ValueError):
        return False


def _outcome_from_scores(home: int, away: int) -> str:
    if home > away:
        return "home"
    if home < away:
        return "away"
    return "draw"


def _canon_pair(home: str, away: str) -> Tuple[str, str]:
    return (canonical(home), canonical(away))


def _split_fixture(fixture: str) -> Optional[Tuple[str, str]]:
    if not fixture or " vs " not in fixture:
        return None
    home, away = fixture.split(" vs ", 1)
    return home.strip(), away.strip()


def _normalise_kickoff(value: Any) -> Optional[str]:
    """Normalise a kickoff timestamp to ``...Z`` form; None if empty."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Model-log form "2026-06-13 01:00:00+00:00" -> ISO-Z.
    text = text.replace(" ", "T").replace("+00:00", "Z")
    # Existing-file millisecond form "...:00.000Z" -> "...:00Z".
    text = text.replace(".000Z", "Z")
    return text


def load_played_matches(src_path: str) -> List[Dict[str, Any]]:
    """Read the played WC2026 matches from the cleaned martj42 dataset."""
    out: List[Dict[str, Any]] = []
    with open(src_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            date = (row.get("date") or "").strip()
            tournament = row.get("tournament") or ""
            hs = row.get("home_score")
            as_ = row.get("away_score")
            if date < WC2026_START_DATE:
                continue
            if TOURNAMENT_SUBSTR not in tournament:
                continue
            if not (_is_int(hs) and _is_int(as_)):
                continue
            home = (row.get("home_team") or "").strip()
            away = (row.get("away_team") or "").strip()
            if not home or not away:
                continue
            out.append(
                {
                    "date": date,
                    "home": home,
                    "away": away,
                    "home_score": int(str(hs).strip()),
                    "away_score": int(str(as_).strip()),
                }
            )
    return out


def _kickoff_index(prev_path: Optional[str], log_path: Optional[str]) -> Dict[Tuple[str, str], str]:
    """Build a ``canonical (home, away) -> kickoff_utc`` map from real sources.

    Priority: the existing hand-maintained results file (authoritative for the
    matches it covers), then the model-predictions log (the kickoff the system
    actually used).  Returns only entries with a real, non-empty timestamp.
    """
    index: Dict[Tuple[str, str], str] = {}

    # Lowest priority first; later writes win, so apply the log then the file.
    if log_path and Path(log_path).exists():
        for line in Path(log_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pair = _split_fixture(rec.get("fixture") or "")
            ko = _normalise_kickoff(rec.get("kickoff"))
            if pair and ko:
                index[_canon_pair(*pair)] = ko

    if prev_path and Path(prev_path).exists():
        try:
            prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
        except ValueError:
            prev = {}
        for r in (prev or {}).get("results", []):
            pair = _split_fixture(r.get("fixture") or "")
            ko = _normalise_kickoff(r.get("kickoff_utc"))
            if pair and ko:
                index[_canon_pair(*pair)] = ko  # file overrides the log

    return index


def build_results(
    src_path: str,
    prev_path: Optional[str] = None,
    log_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive the processed-results payload from the cleaned dataset."""
    matches = load_played_matches(src_path)
    kickoffs = _kickoff_index(prev_path, log_path)

    results: List[Dict[str, Any]] = []
    for m in matches:
        home = canonical(m["home"])
        away = canonical(m["away"])
        fixture = f"{home} vs {away}"
        outcome = _outcome_from_scores(m["home_score"], m["away_score"])
        row: Dict[str, Any] = {
            "date": m["date"],
            "fixture": fixture,
            "score": f"{m['home_score']}-{m['away_score']}",
            "outcome": outcome,
        }
        ko = kickoffs.get((home, away))
        if ko:
            row["kickoff_utc"] = ko
        results.append(row)

    # Stable, deterministic order: by date, then fixture.
    results.sort(key=lambda r: (r["date"], r["fixture"]))
    return {"results": results, "_comment": _COMMENT}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Derive wc2026_results.json from martj42_cleaned.")
    ap.add_argument("--src", default=_DEFAULT_SRC, help="cleaned martj42 CSV")
    ap.add_argument("--out", default=_DEFAULT_OUT, help="output processed results JSON")
    ap.add_argument(
        "--prev",
        default=None,
        help="prior results JSON to carry kickoff_utc from (defaults to --out)",
    )
    ap.add_argument("--log", default=_DEFAULT_LOG, help="model-predictions log (kickoff fallback)")
    args = ap.parse_args(argv)

    prev_path = args.prev if args.prev is not None else args.out
    payload = build_results(args.src, prev_path=prev_path, log_path=args.log)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    n = len(payload["results"])
    n_ko = sum(1 for r in payload["results"] if r.get("kickoff_utc"))
    print(f"{args.out}: {n} played matches ({n_ko} with kickoff_utc).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
