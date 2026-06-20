#!/usr/bin/env python
"""Generate the prediction-tracking feed (``site/tracking_data.json``).

Scores the model's *pre-match* predictions against actual results.  Because
V1 never persisted a full pre-match prediction per fixture, this CLI does the
archaeology:

* every historical version of ``data/card_latest.md`` and
  ``site/scores_data.json`` is recovered from git (plus the current working
  tree copies) and timestamped from their embedded ``generated`` markers;
* actual results come from the manually-maintained
  ``data/processed/wc2026_results.json`` (one-line edit per finished match);
* the de-vigged closing 1X2 consensus comes from the ledger DB's
  ``odds_snapshots`` table (last capture before kickoff, per fixture);
* bet-level P/L + CLV aggregates come from the ``bets`` table.

All computation lives in the deterministic :mod:`wca.tracking`; this CLI only
gathers inputs (git, filesystem, SQLite, clock).

Usage
-----
    python scripts/wca_tracking_data.py [--db data/wca.db] \
        [--results data/processed/wc2026_results.json] \
        [--out site/tracking_data.json] [--repo .] [--now "YYYY-MM-DD HH:MM:SS UTC"]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

# Make ``src`` importable when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from wca import tracking  # noqa: E402
from wca.data import teamnames  # noqa: E402


def _now_utc_str() -> str:
    """Current UTC time, same display convention as the other site feeds."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Git archaeology.
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str) -> Optional[str]:
    """Run a git command in *repo*; ``None`` on any failure (no git, no repo)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _historical_versions(repo: str, path: str) -> List[str]:
    """Every committed version of *path* (newest first), as raw text blobs."""
    log = _git(repo, "log", "--format=%H", "--", path)
    if not log:
        return []
    blobs: List[str] = []
    for commit in log.split():
        blob = _git(repo, "show", "%s:%s" % (commit, path))
        if blob:
            blobs.append(blob)
    return blobs


def collect_card_snapshots(repo: str, card_path: str) -> List[Dict[str, Any]]:
    """Card snapshots ``{"generated", "text"}`` from git history + worktree."""
    texts: List[str] = []
    full = os.path.join(repo, card_path)
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            texts.append(fh.read())
    texts.extend(_historical_versions(repo, card_path))

    snapshots: List[Dict[str, Any]] = []
    seen = set()
    for text in texts:
        generated = tracking.card_generated(text)
        if not generated or generated in seen:
            continue
        seen.add(generated)
        snapshots.append({"generated": generated, "text": text})
    return snapshots


def collect_scores_snapshots(repo: str, scores_path: str) -> List[Dict[str, Any]]:
    """Scores-feed snapshots ``{"generated", "fixtures"}`` from git + worktree."""
    raws: List[str] = []
    full = os.path.join(repo, scores_path)
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            raws.append(fh.read())
    raws.extend(_historical_versions(repo, scores_path))

    snapshots: List[Dict[str, Any]] = []
    seen = set()
    for raw in raws:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        generated = ((data.get("meta") or {}).get("generated") or "").strip()
        if not generated or generated in seen:
            continue
        seen.add(generated)
        snapshots.append(
            {"generated": generated, "fixtures": data.get("fixtures") or []}
        )
    return snapshots


# ---------------------------------------------------------------------------
# Ledger DB: closing-odds consensus + bets.
# ---------------------------------------------------------------------------


def _canon(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return (teamnames.canonical(name) or "").strip().casefold()


def _load_match_index(con: sqlite3.Connection) -> Dict[str, Tuple[str, str]]:
    """Map odds_snapshots ``match_id`` -> raw ``(home_team, away_team)``."""
    rows = con.execute(
        "SELECT match_id, MIN(raw) FROM odds_snapshots WHERE market='h2h' "
        "GROUP BY match_id"
    ).fetchall()
    index: Dict[str, Tuple[str, str]] = {}
    for match_id, raw in rows:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        home = payload.get("home_team")
        away = payload.get("away_team")
        if home and away:
            index[match_id] = (home, away)
    return index


def market_close_for_fixture(
    con: sqlite3.Connection,
    match_index: Dict[str, Tuple[str, str]],
    fixture: str,
    kickoff_utc: str,
) -> Optional[Dict[str, Any]]:
    """De-vigged consensus 1X2 at the last odds capture before kickoff."""
    pair = tracking.split_fixture(fixture)
    if pair is None or not kickoff_utc:
        return None
    want = (_canon(pair[0]), _canon(pair[1]))
    match_id = None
    home_raw = away_raw = None
    for mid, (home, away) in match_index.items():
        if (_canon(home), _canon(away)) == want:
            match_id, home_raw, away_raw = mid, home, away
            break
    if match_id is None:
        return None

    # ISO-sortable cutoff: odds_snapshots ts_utc looks like
    # "2026-06-11T19:00:11.239782+00:00"; the kickoff "2026-06-11T19:00:00Z".
    cutoff = kickoff_utc.replace("Z", "+00:00")
    last_ts_row = con.execute(
        "SELECT MAX(ts_utc) FROM odds_snapshots "
        "WHERE match_id=? AND market='h2h' AND ts_utc<=?",
        (match_id, cutoff),
    ).fetchone()
    last_ts = last_ts_row[0] if last_ts_row else None
    if not last_ts:
        return None

    rows = con.execute(
        "SELECT raw FROM odds_snapshots "
        "WHERE match_id=? AND market='h2h' AND ts_utc=?",
        (match_id, last_ts),
    ).fetchall()
    books: Dict[str, Dict[str, float]] = {}
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        book = payload.get("bookmaker_key") or payload.get("bookmaker_title")
        outcome = payload.get("outcome_name")
        dec = payload.get("decimal_odds")
        if not book or outcome is None or dec is None:
            continue
        leg = None
        if str(outcome).strip().casefold() == "draw":
            leg = "draw"
        elif _canon(outcome) == _canon(home_raw):
            leg = "home"
        elif _canon(outcome) == _canon(away_raw):
            leg = "away"
        if leg is None:
            continue
        books.setdefault(book, {})[leg] = float(dec)

    triple = tracking.devig_consensus(
        [
            {"book": book, **prices}
            for book, prices in books.items()
        ]
    )
    if triple is None:
        return None
    return {"triple": triple, "ts": last_ts, "books": len(books)}


def load_bets(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, match_desc, market, selection, decimal_odds, stake, "
        "status, settled_pl, model_prob, market_prob_devig, closing_odds, clv, "
        "source, ev, platform "
        "FROM bets ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the World Cup Alpha prediction-tracking feed.",
    )
    parser.add_argument(
        "--db", default="data/wca.db", help="SQLite ledger path (default: data/wca.db)."
    )
    parser.add_argument(
        "--results",
        default="data/processed/wc2026_results.json",
        help="Manually-maintained results file.",
    )
    parser.add_argument(
        "--out",
        default="site/tracking_data.json",
        help="Destination JSON file (default: site/tracking_data.json).",
    )
    parser.add_argument(
        "--repo",
        default=_ROOT,
        help="Repo root for git archaeology + relative paths (default: repo of this script).",
    )
    parser.add_argument(
        "--card-path", default="data/card_latest.md", help="Card path inside the repo."
    )
    parser.add_argument(
        "--scores-path",
        default="site/scores_data.json",
        help="Scores feed path inside the repo.",
    )
    parser.add_argument(
        "--model-preds-log",
        default="data/model_predictions_log.jsonl",
        help="Append-only model-predictions log written at card-build time; "
             "exact triples here beat the scoreline reconstruction.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help='Override "now" (display only), e.g. "2026-06-13 09:00:00 UTC".',
    )
    args = parser.parse_args(argv)

    now_utc = args.now or _now_utc_str()
    repo = os.path.abspath(args.repo)

    def _resolve(path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(repo, path)

    # --- results ----------------------------------------------------------
    results_path = _resolve(args.results)
    try:
        with open(results_path, encoding="utf-8") as fh:
            results = (json.load(fh) or {}).get("results") or []
    except (OSError, ValueError) as exc:
        print("ERROR: cannot read results file %s (%s)" % (results_path, exc),
              file=sys.stderr)
        return 1

    # --- prediction snapshots (git archaeology) ----------------------------
    snapshots: List[Dict[str, Any]] = []
    snapshots.extend(collect_card_snapshots(repo, args.card_path))
    snapshots.extend(collect_scores_snapshots(repo, args.scores_path))

    # --- exact model predictions (card-build persistence) -------------------
    exact_models: List[Dict[str, Any]] = []
    preds_log_path = _resolve(args.model_preds_log)
    if os.path.exists(preds_log_path):
        try:
            with open(preds_log_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        exact_models.append(row)
        except OSError as exc:
            print("warning: cannot read model predictions log %s (%s)"
                  % (preds_log_path, exc), file=sys.stderr)

    # --- ledger DB ----------------------------------------------------------
    db_path = _resolve(args.db)
    market_closes: Dict[Tuple[str, str], Dict[str, Any]] = {}
    bets: List[Dict[str, Any]] = []
    if os.path.exists(db_path):
        con = sqlite3.connect(db_path)
        try:
            match_index = _load_match_index(con)
            for row in results:
                fixture = row.get("fixture") or ""
                key = tracking.fixture_key(fixture)
                if key is None:
                    continue
                close = market_close_for_fixture(
                    con, match_index, fixture, row.get("kickoff_utc") or ""
                )
                if close is not None:
                    market_closes[key] = close
            bets = load_bets(con)
        finally:
            con.close()
    else:
        print("warning: ledger DB not found at %s — bet stats and closing "
              "consensus omitted" % db_path, file=sys.stderr)

    # --- build + write ------------------------------------------------------
    payload = tracking.build_tracking_data(
        results=results,
        snapshots=snapshots,
        market_closes=market_closes,
        bets=bets,
        now_utc=now_utc,
        exact_models=exact_models,
    )

    out_path = args.out if os.path.isabs(args.out) else os.path.join(
        os.getcwd(), args.out
    )

    # Never clobber a populated feed with an environment-starved one (no
    # ledger DB / shallow clone) — mirrors the linemove/modelpreds guards.
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = None
        if existing and tracking.payload_degraded(payload, existing):
            print(
                "refusing to overwrite %s: new payload is strictly poorer "
                "(missing DB or git history?) — kept existing feed" % out_path,
                file=sys.stderr,
            )
            return 0

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
        fh.write("\n")

    summary = payload["summary"]
    print(out_path)
    print(
        "fixtures=%d  model_correct=%d  market_correct=%d  "
        "brier model/market=%s/%s  snapshots=%d  bets_settled=%d  pl=%.2f"
        % (
            summary["fixtures_complete"],
            summary["model_1x2_correct"],
            summary["market_1x2_correct"],
            summary["model_brier"],
            summary["market_brier"],
            len(snapshots),
            summary["bets"]["settled"],
            summary["bets"]["pl"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
