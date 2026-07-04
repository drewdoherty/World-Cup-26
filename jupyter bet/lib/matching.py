"""Cross-venue market matching — auditable, never fuzzy-names-alone.

A candidate PM↔sportsbook (or PM↔model) match must agree on ALL of:
  1. canonical team pair          (wca.data.teamnames.canonical, both teams)
  2. kickoff within tolerance     (Params.kickoff_tolerance_h)
  3. market type                  (h2h ↔ moneyline etc., mapped table below)
  4. line / handicap              (exact, when the market has one)
  5. period                       (FT vs 1H…)
  6. settlement basis             (90min vs et-pens — NEVER bridged: PM
                                   advancement ≠ 1X2, hard rule)
Each criterion contributes to a confidence score; matches land in one of
ACCEPTED (score ≥ Params.min_match_confidence, all hard gates pass),
REJECTED (a hard gate failed — reason recorded), or AMBIGUOUS (soft score
between). ``overrides.yaml`` pins or bans specific pairs by ID and always
wins (reason: "manual_override").
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Tuple

import polars as pl
import yaml

import lib.bootstrap as bt
import lib.ids as ids

OVERRIDES_PATH = bt.JB_ROOT / "overrides.yaml"

# Market-type vocabulary mapping per source → canonical
MARKET_TYPE_MAP = {
    ("theoddsapi", "h2h"): "1x2",
    ("theoddsapi", "h2h_3_way"): "1x2",
    ("theoddsapi", "totals"): "totals",
    ("theoddsapi", "btts"): "btts",
    ("theoddsapi", "spreads"): "handicap",
    ("theoddsapi", "draw_no_bet"): "dnb",
    ("polymarket", "moneyline"): "1x2",
    ("polymarket", "totals"): "totals",
    ("polymarket", "advance"): "advance",
    ("polymarket", "champion"): "outright",
}

HARD_GATES = ("teams", "kickoff", "market_type", "line", "period", "settlement")


def load_overrides() -> Dict[str, str]:
    """{'<id_a>||<id_b>': 'accept'|'reject'} from overrides.yaml."""
    if not OVERRIDES_PATH.exists():
        return {}
    raw = yaml.safe_load(OVERRIDES_PATH.read_text()) or {}
    out = {}
    for row in raw.get("pairs", []):
        out[f"{row['a']}||{row['b']}"] = row["verdict"]
    return out


def team_pair_key(home: str, away: str) -> Tuple[str, str]:
    """Order-independent canonical team pair."""
    a, b = ids.slug(home), ids.slug(away)
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def score_match(a: Dict[str, Any], b: Dict[str, Any], *,
                kickoff_tolerance_h: float = 3.0) -> Dict[str, Any]:
    """Score one candidate pair. `a`/`b` are canonical market dicts with:
    home, away, kickoff_utc (tz-aware), market_type, line, period,
    settlement, source, source_market_id.
    Returns {verdict, confidence, reasons: [str], checks: {gate: bool}}."""
    checks: Dict[str, Optional[bool]] = {}
    reasons: List[str] = []

    checks["teams"] = team_pair_key(a["home"], a["away"]) == team_pair_key(b["home"], b["away"])
    if not checks["teams"]:
        reasons.append(f"teams differ: {a['home']}/{a['away']} vs {b['home']}/{b['away']}")

    ka, kb = a.get("kickoff_utc"), b.get("kickoff_utc")
    if ka is None or kb is None:
        checks["kickoff"] = None
        reasons.append("kickoff missing on one side")
    else:
        dh = abs((ka - kb).total_seconds()) / 3600
        checks["kickoff"] = dh <= kickoff_tolerance_h
        if not checks["kickoff"]:
            reasons.append(f"kickoff {dh:.1f}h apart > {kickoff_tolerance_h}h")

    checks["market_type"] = a["market_type"] == b["market_type"]
    if not checks["market_type"]:
        reasons.append(f"market type {a['market_type']} vs {b['market_type']}")

    la, lb = a.get("line"), b.get("line")
    checks["line"] = (la == lb) or (la is None and lb is None)
    if not checks["line"]:
        reasons.append(f"line {la} vs {lb}")

    checks["period"] = (a.get("period") or "FT") == (b.get("period") or "FT")
    if not checks["period"]:
        reasons.append(f"period {a.get('period')} vs {b.get('period')}")

    checks["settlement"] = a.get("settlement") == b.get("settlement") and \
        a.get("settlement") != ids.S_UNKNOWN
    if not checks["settlement"]:
        reasons.append(
            f"settlement {a.get('settlement')} vs {b.get('settlement')} "
            "(90min vs et-pens are NEVER the same market)")

    hard_fail = any(checks[g] is False for g in HARD_GATES)
    known = [g for g in HARD_GATES if checks[g] is not None]
    confidence = (sum(1 for g in known if checks[g]) / len(HARD_GATES))
    verdict = "rejected" if hard_fail else (
        "accepted" if confidence >= 0.999 else "ambiguous")
    return {"verdict": verdict, "confidence": round(confidence, 3),
            "reasons": reasons, "checks": checks}


def match_frames(cand_a: pl.DataFrame, cand_b: pl.DataFrame, *,
                 kickoff_tolerance_h: float = 3.0,
                 min_confidence: float = 0.9) -> pl.DataFrame:
    """All-pairs match between two canonical market frames (small n — WC).
    Emits one row per considered pair with verdict/confidence/reasons; the
    notebook splits into accepted/rejected/ambiguous tables."""
    overrides = load_overrides()
    rows: List[Dict[str, Any]] = []
    a_rows = cand_a.to_dicts()
    b_rows = cand_b.to_dicts()
    for a in a_rows:
        pk_a = team_pair_key(a["home"], a["away"])
        for b in b_rows:
            if team_pair_key(b["home"], b["away"]) != pk_a:
                continue  # cheap prefilter — never emitted, teams disjoint
            s = score_match(a, b, kickoff_tolerance_h=kickoff_tolerance_h)
            key = f"{a['source_market_id']}||{b['source_market_id']}"
            if key in overrides:
                s["verdict"] = {"accept": "accepted", "reject": "rejected"}[overrides[key]]
                s["reasons"] = [f"manual_override:{overrides[key]}"]
            if s["verdict"] == "ambiguous" and s["confidence"] >= min_confidence \
                    and not any(s["checks"][g] is False for g in HARD_GATES):
                s["verdict"] = "accepted"
            rows.append({
                "a_source": a["source"], "a_market_id": a["source_market_id"],
                "b_source": b["source"], "b_market_id": b["source_market_id"],
                "event_a": f"{a['home']} vs {a['away']}",
                "market_type_a": a["market_type"], "market_type_b": b["market_type"],
                "verdict": s["verdict"], "confidence": s["confidence"],
                "reasons": "; ".join(s["reasons"]) or "all gates passed",
            })
    schema = {"a_source": pl.Utf8, "a_market_id": pl.Utf8, "b_source": pl.Utf8,
              "b_market_id": pl.Utf8, "event_a": pl.Utf8,
              "market_type_a": pl.Utf8, "market_type_b": pl.Utf8,
              "verdict": pl.Utf8, "confidence": pl.Float64, "reasons": pl.Utf8}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


# --------------------------------------------------------------------------
# PM market canonicalisation.
#
# Real conventions (verified against data/pm_orderflow.db, 2026-07-04):
#   * Match markets: event_slug ``fifwc-<a3>-<b3>-<YYYY-MM-DD>``, one market
#     per outcome (market_slug suffix ``-<a3>``/``-<b3>``/``-draw``), each a
#     Yes/No question ("Will Australia win on 2026-07-03?"). Together they
#     form the 1X2, settling at 90 minutes.
#   * Advancement/futures: event_slug like ``world-cup-nation-to-reach-*``,
#     no game_start_time, ET+pens settlement.
#   * The DB's ``category`` column carries PRODUCTION's classification
#     (match_1x2, advancement_r32…final, group_winner, winner, other_future)
#     — used as the primary signal; text fallbacks handle live-Gamma rows
#     where category is absent.
# --------------------------------------------------------------------------
_MATCH_EV_SLUG = re.compile(
    r"^fifwc-(?P<a>[a-z]{3})-(?P<b>[a-z]{3})-(?P<date>\d{4}-\d{2}-\d{2})$")
_WIN_Q = re.compile(r"^will (?P<team>.+?) win on \d{4}-\d{2}-\d{2}\?$", re.I)


def classify_pm_market(row: Dict[str, Any]) -> str:
    """match_1x2 | advance | group_winner | outright | other_future —
    category column first, question text as fallback (live Gamma rows)."""
    cat = (row.get("category") or "").strip()
    if cat == "match_1x2":
        return "match_1x2"
    if cat.startswith("advancement"):
        return "advance"
    if cat == "group_winner":
        return "group_winner"
    if cat == "winner":
        return "outright"
    if cat == "other_future":
        return "other_future"
    ql = (row.get("question") or "").lower()
    if _MATCH_EV_SLUG.match(row.get("event_slug") or "") or _WIN_Q.match(ql):
        return "match_1x2"
    if "reach" in ql or "advance" in ql or "eliminated" in ql:
        return "advance"
    if "win group" in ql or "win uefa group" in ql:
        return "group_winner"
    if "win the 2026 fifa world cup" in ql:
        return "outright"
    return "other_future"


def pm_match_events(markets: pl.DataFrame) -> pl.DataFrame:
    """Assemble PM match_1x2 outcome-markets into one canonical row per
    MATCH: home/away (slug order = home first), kickoff, and the three
    outcome condition_ids. Rows that can't be assembled are returned in the
    companion 'unparsed' count by the caller via the n_markets check."""
    rows: List[Dict[str, Any]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in markets.to_dicts():
        if classify_pm_market(r) != "match_1x2":
            continue
        grouped.setdefault(r.get("event_slug") or "?", []).append(r)
    for ev_slug, group in sorted(grouped.items()):
        m = _MATCH_EV_SLUG.match(ev_slug)
        if not m:
            continue
        a3, b3 = m["a"], m["b"]
        names: Dict[str, str] = {}
        cids: Dict[str, str] = {}
        ko = None
        for r in group:
            slug_suffix = (r.get("market_slug") or "").rsplit("-", 1)[-1]
            ko = ko or _parse_ts(r.get("game_start_time"))
            wq = _WIN_Q.match((r.get("question") or "").strip())
            if slug_suffix == "draw" or "draw" in (r.get("question") or "").lower():
                cids["draw"] = r["condition_id"]
            elif wq and slug_suffix in (a3, b3):
                names[slug_suffix] = wq["team"].strip()
                cids["home" if slug_suffix == a3 else "away"] = r["condition_id"]
        if "home" not in cids or "away" not in cids:
            continue
        rows.append({
            "event_slug": ev_slug, "home": names.get(a3, a3),
            "away": names.get(b3, b3), "kickoff_utc": ko,
            "cid_home": cids.get("home"), "cid_away": cids.get("away"),
            "cid_draw": cids.get("draw"),
            "n_outcome_markets": len(group),
        })
    schema = {"event_slug": pl.Utf8, "home": pl.Utf8, "away": pl.Utf8,
              "kickoff_utc": pl.Datetime("us", "UTC"), "cid_home": pl.Utf8,
              "cid_away": pl.Utf8, "cid_draw": pl.Utf8,
              "n_outcome_markets": pl.Int64}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def pm_canonical_matches(markets: pl.DataFrame) -> pl.DataFrame:
    """Canonical market frame (one row per PM MATCH market) for matching
    against sportsbook frames. PM match 1X2 settles at 90 minutes —
    settlement carries S_90MIN; advancement never appears here."""
    ev = pm_match_events(markets)
    rows = []
    for r in ev.to_dicts():
        rows.append({"source": "polymarket",
                     "source_market_id": r["event_slug"],
                     "home": r["home"], "away": r["away"],
                     "kickoff_utc": r["kickoff_utc"], "market_type": "1x2",
                     "line": None, "period": "FT",
                     "settlement": ids.S_90MIN})
    schema = {"source": pl.Utf8, "source_market_id": pl.Utf8, "home": pl.Utf8,
              "away": pl.Utf8, "kickoff_utc": pl.Datetime("us", "UTC"),
              "market_type": pl.Utf8, "line": pl.Float64, "period": pl.Utf8,
              "settlement": pl.Utf8}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    # normalise bare '+00' / '+0000' offsets (pm_markets style) for %z
    if re.search(r"[+-]\d{2}$", s):
        s = s + "00"
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            t = dt.datetime.strptime(s, fmt)
            return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else \
                t.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None
