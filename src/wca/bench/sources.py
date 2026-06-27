"""Data loaders for the benchmark harness.

Every loader prefers the #71 parquet archive (``data/archive/...``) and falls
back to the live legacy source when the archive is empty or absent, so the
harness runs today and auto-upgrades as the archive fills. All reads are
read-only; nothing here mutates the ledger or the archive.

Unified frames returned
-----------------------
predictions : generated, fixture, home, away, kickoff, kickoff_date, match_id,
              p_home, p_draw, p_away (model), m_home, m_draw, m_away (market),
              lambda_home, lambda_away
results     : dict (home_norm, away_norm, date) -> (hs, as_, outcome in {H,D,A})
closing     : dict match_id -> {"home","draw","away"} fair closing probs,
              plus team_key -> match_id index for team+date joins
bets        : DataFrame with canonical market/venue/edge/pl/clv columns
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd

from wca.bench.marketmap import canonical_market

# ---------------------------------------------------------------------------
# Team-name normalisation (light; enough to join odds-feed names to martj42)
# ---------------------------------------------------------------------------

_ALIASES = {
    "usa": "united states",
    "united states of america": "united states",
    "korea republic": "south korea",
    "south korea": "south korea",
    "ir iran": "iran",
    "iran": "iran",
    "côte d'ivoire": "ivory coast",
    "cote d'ivoire": "ivory coast",
    "czechia": "czech republic",
    "china pr": "china",
}


def norm_team(name: str) -> str:
    if not name:
        return ""
    s = str(name).strip().lower()
    s = s.replace("&", "and")
    s = " ".join(s.split())
    return _ALIASES.get(s, s)


def _split_fixture(fixture: str) -> Tuple[str, str]:
    if not fixture or " vs " not in fixture:
        return "", ""
    h, a = fixture.split(" vs ", 1)
    return h.strip(), a.strip()


def _kickoff_date(kickoff: str) -> str:
    """Best-effort YYYY-MM-DD from a mixed-format kickoff string."""
    if not kickoff:
        return ""
    s = str(kickoff).strip().replace("T", " ")
    return s[:10]


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------

def _archive_predictions(archive_dir: str) -> Optional[pd.DataFrame]:
    path = os.path.join(archive_dir, "model_predictions")
    if not os.path.isdir(path):
        return None
    try:
        import pyarrow.dataset as ds

        tbl = ds.dataset(path).to_table().to_pandas()
    except Exception:
        return None
    if tbl.empty:
        return None
    rows = []
    for _, r in tbl.iterrows():
        h, a = _split_fixture(r.get("fixture", ""))
        rows.append({
            "generated": r.get("ts_utc", ""),
            "fixture": r.get("fixture", ""),
            "home": h, "away": a,
            "kickoff": r.get("kickoff", ""),
            "kickoff_date": _kickoff_date(r.get("kickoff", "")),
            "match_id": r.get("match_id", ""),
            "p_home": r.get("p_home"), "p_draw": r.get("p_draw"), "p_away": r.get("p_away"),
            "m_home": None, "m_draw": None, "m_away": None,
            "lambda_home": r.get("lambda_home"), "lambda_away": r.get("lambda_away"),
        })
    return pd.DataFrame(rows)


def _jsonl_predictions(path: str) -> pd.DataFrame:
    rows = []
    if not os.path.exists(path):
        return pd.DataFrame()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            h, a = _split_fixture(r.get("fixture", ""))
            model = r.get("model", {}) or {}
            market = r.get("market", {}) or {}
            rows.append({
                "generated": r.get("generated", ""),
                "fixture": r.get("fixture", ""),
                "home": h, "away": a,
                "kickoff": r.get("kickoff", ""),
                "kickoff_date": _kickoff_date(r.get("kickoff", "")),
                "match_id": r.get("match_id", ""),
                "p_home": model.get("home"), "p_draw": model.get("draw"),
                "p_away": model.get("away"),
                "m_home": market.get("home"), "m_draw": market.get("draw"),
                "m_away": market.get("away"),
                "lambda_home": r.get("lambda_home"), "lambda_away": r.get("lambda_away"),
            })
    return pd.DataFrame(rows)


def load_predictions(archive_dir: str = "data/archive",
                     jsonl_path: str = "data/model_predictions_log.jsonl") -> pd.DataFrame:
    """Load model predictions (archive parquet preferred, jsonl fallback)."""
    df = _archive_predictions(archive_dir)
    src = "archive"
    if df is None or df.empty:
        df = _jsonl_predictions(jsonl_path)
        src = "jsonl"
    if not df.empty:
        df.attrs["source"] = src
    return df


def latest_per_fixture(preds: pd.DataFrame) -> pd.DataFrame:
    """Keep the most recent build per (home, away, kickoff_date).

    match_id is unstable across builds, so dedup on the normalised fixture +
    kickoff date rather than the id.
    """
    if preds.empty:
        return preds
    df = preds.copy()
    df["_hk"] = df["home"].map(norm_team)
    df["_ak"] = df["away"].map(norm_team)
    df["_key"] = list(zip(df["_hk"], df["_ak"], df["kickoff_date"]))
    df = df.sort_values("generated").drop_duplicates("_key", keep="last")
    return df.drop(columns=["_hk", "_ak"])


# ---------------------------------------------------------------------------
# Results (realized outcomes)
# ---------------------------------------------------------------------------

def load_results(csv_path: str = "data/raw/martj42_cleaned.csv",
                 fallback: str = "data/raw/results.csv",
                 tournament: str = "FIFA World Cup") -> Dict[Tuple[str, str, str], Tuple[int, int, str]]:
    """Map (home_norm, away_norm, date) -> (home_score, away_score, outcome)."""
    path = csv_path if os.path.exists(csv_path) else fallback
    out: Dict[Tuple[str, str, str], Tuple[int, int, str]] = {}
    if not os.path.exists(path):
        return out
    df = pd.read_csv(path)
    if tournament:
        df = df[df["tournament"] == tournament]
    for _, r in df.iterrows():
        hs, as_ = r.get("home_score"), r.get("away_score")
        if pd.isna(hs) or pd.isna(as_):
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        outcome = "H" if hs > as_ else ("A" if as_ > hs else "D")
        key = (norm_team(r["home_team"]), norm_team(r["away_team"]), str(r["date"])[:10])
        out[key] = (hs, as_, outcome)
    return out


def lookup_result(results: Dict, home: str, away: str, kickoff_date: str
                  ) -> Optional[Tuple[int, int, str]]:
    """Find a result allowing +/-1 day (kickoff UTC may roll past midnight)."""
    h, a = norm_team(home), norm_team(away)
    if (h, a, kickoff_date) in results:
        return results[(h, a, kickoff_date)]
    try:
        base = date.fromisoformat(kickoff_date)
    except (ValueError, TypeError):
        return None
    for delta in (-1, 1):
        d = (base + timedelta(days=delta)).isoformat()
        if (h, a, d) in results:
            return results[(h, a, d)]
    return None


# ---------------------------------------------------------------------------
# Closing lines (walk-forward: latest snapshot at/just before kickoff)
# ---------------------------------------------------------------------------

def _devig_three(odds_home: float, odds_draw: float, odds_away: float
                 ) -> Optional[Dict[str, float]]:
    vals = [odds_home, odds_draw, odds_away]
    if any(o is None or o <= 1.0 for o in vals):
        return None
    raw = [1.0 / o for o in vals]
    total = sum(raw)
    if total <= 0:
        return None
    return {"home": raw[0] / total, "draw": raw[1] / total, "away": raw[2] / total}


def load_closing_lines(db_path: str, kickoffs: Optional[Dict[str, str]] = None
                       ) -> Dict[str, object]:
    """Closing 1X2 fair probs per match from ``odds_snapshots``.

    Closing = the consensus (mean across books) of the latest snapshot strictly
    at/before kickoff for each h2h selection, devigged multiplicatively.

    Returns ``{"by_match": {match_id: {home,draw,away}},
               "team_index": {(home_norm,away_norm,date): match_id}}``.
    kickoffs maps match_id -> kickoff ISO; when absent we fall back to the max
    snapshot time per match as a proxy for the closing instant.
    """
    by_match: Dict[str, Dict[str, float]] = {}
    team_index: Dict[Tuple[str, str, str], str] = {}
    if not os.path.exists(db_path):
        return {"by_match": by_match, "team_index": team_index}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Build per-match metadata (teams, kickoff) from raw payloads.
        meta: Dict[str, Tuple[str, str, str, str]] = {}  # match_id -> (home, away, date, commence)
        rows = con.execute(
            "SELECT match_id, raw FROM odds_snapshots WHERE market='h2h' "
            "GROUP BY match_id"
        ).fetchall()
        for mid, raw in rows:
            try:
                d = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                d = {}
            h, a = d.get("home_team", ""), d.get("away_team", "")
            ct = d.get("commence_time", "")
            kd = _kickoff_date(ct)
            meta[mid] = (h, a, kd, ct)
            if h and a and kd:
                team_index[(norm_team(h), norm_team(a), kd)] = mid

        for mid in meta:
            h, a, kd, ct = meta[mid]
            # Closing cutoff = exact kickoff (commence_time) when known, else an
            # external override, else end of the match day. Kept in ISO 'T' form
            # so the string comparison against ts_utc is correctly ordered.
            ko_iso = ct or (kickoffs or {}).get(mid) or f"{kd}T23:59:59+00:00"
            # latest snapshot ts at/before kickoff
            row = con.execute(
                "SELECT MAX(ts_utc) FROM odds_snapshots "
                "WHERE match_id=? AND market='h2h' AND ts_utc<=?",
                (mid, ko_iso),
            ).fetchone()
            close_ts = row[0] if row else None
            if not close_ts:
                row = con.execute(
                    "SELECT MAX(ts_utc) FROM odds_snapshots "
                    "WHERE match_id=? AND market='h2h'", (mid,)).fetchone()
                close_ts = row[0] if row else None
            if not close_ts:
                continue
            # consensus odds per selection at the closing snapshot timestamp
            sel_odds: Dict[str, float] = {}
            for sel, avg in con.execute(
                "SELECT selection, AVG(decimal_odds) FROM odds_snapshots "
                "WHERE match_id=? AND market='h2h' AND ts_utc=? GROUP BY selection",
                (mid, close_ts)):
                sel_odds[sel] = avg
            o_home = sel_odds.get(h)
            o_away = sel_odds.get(a)
            o_draw = sel_odds.get("Draw") or sel_odds.get("draw")
            fair = _devig_three(o_home, o_draw, o_away)
            if fair:
                by_match[mid] = fair
    finally:
        con.close()
    return {"by_match": by_match, "team_index": team_index}


# ---------------------------------------------------------------------------
# Bets (placed) ledger
# ---------------------------------------------------------------------------

def load_bets(db_path: str, archive_dir: str = "data/archive") -> pd.DataFrame:
    """Load settled+open bets with canonical market/venue and computed edge.

    Prefers the ``ledger_bets`` parquet snapshot when present (latest), else the
    live ``bets`` table.
    """
    df = _archive_bets(archive_dir)
    src = "archive"
    if df is None or df.empty:
        df = _db_bets(db_path)
        src = "db"
    if df.empty:
        return df
    df["market_family"] = df["market"].map(canonical_market)
    df["venue"] = df["platform"].fillna("unknown").map(_venue_family)
    # edge = model EV per unit at the odds backed: model_prob * odds - 1
    df["edge_calc"] = df.apply(
        lambda r: (r["model_prob"] * r["decimal_odds"] - 1.0)
        if pd.notna(r.get("model_prob")) and pd.notna(r.get("decimal_odds")) else None, axis=1)
    df.attrs["source"] = src
    return df


def _db_bets(db_path: str) -> pd.DataFrame:
    if not os.path.exists(db_path):
        return pd.DataFrame()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return pd.read_sql_query("SELECT * FROM bets", con)
    finally:
        con.close()


def _archive_bets(archive_dir: str) -> Optional[pd.DataFrame]:
    path = os.path.join(archive_dir, "ledger_bets")
    if not os.path.isdir(path):
        return None
    try:
        import pyarrow.dataset as ds

        tbl = ds.dataset(path).to_table().to_pandas()
    except Exception:
        return None
    if tbl.empty:
        return None
    # keep only the latest snapshot, and rename bet_market->market
    if "snapshot_ts" in tbl.columns:
        tbl = tbl[tbl["snapshot_ts"] == tbl["snapshot_ts"].max()]
    if "bet_market" in tbl.columns and "market" not in tbl.columns:
        tbl = tbl.rename(columns={"bet_market": "market"})
    return tbl


def _venue_family(platform: str) -> str:
    s = str(platform).strip().lower()
    if "betfair" in s:
        return "betfair"
    if "smarket" in s:
        return "smarkets"
    if "poly" in s or "pm" in s:
        return "polymarket"
    if "bet365" in s:
        return "bet365"
    if "betfred" in s:
        return "betfred"
    return s or "unknown"
