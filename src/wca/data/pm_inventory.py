"""Polymarket WC match-event inventory cache.

Caches a dynamic inventory of 2026 FIFA World Cup match markets from the
Polymarket Gamma API into the SQLite ledger (``pm_inventory`` table). The
/accas interactive command reads ONLY from this cache — no live Gamma API
calls at command time.

Build-time refresh (``refresh_pm_events``):
 • Fetches markets only for the NEXT ``max_fixtures`` scheduled fixtures.
 • Deduplicates API calls: skips any fixture whose entry was fetched less
   than ``max_age_hours`` ago and is still open.
 • Preserves a credit reserve: stops after ``max_requests`` API calls per
   run and reports (used, remaining budget) in the returned summary.
 • Reports credits used/remaining in the return dict.

Polymarket Gamma is free (no API key, no per-call fee), so "credits" here
refers to HTTP requests (rate-limit courtesy).

Table schema (created idempotently):
  pm_inventory(
    id INTEGER PRIMARY KEY,
    fixture          TEXT,    -- "Brazil vs France"
    fixture_token    TEXT,    -- normalised sort-key
    question         TEXT,    -- full market question from Gamma
    outcome          TEXT,    -- "Yes" / outcome label
    outcome_token    TEXT,    -- normalised outcome tokens (for matching)
    token_id         TEXT,    -- clobTokenId for the YES token
    price            REAL,    -- mid-price (bestBid+bestAsk)/2 or outcomePrices
    liquidity        REAL,    -- market liquidity in USDC
    neg_risk         INTEGER, -- 1 if negRisk market
    settlement_rules TEXT,    -- raw question text (authoritative settlement)
    fetched_utc      TEXT,    -- ISO timestamp of last fetch
    closed           INTEGER  -- 1 if market resolved/closed
  )

  Unique constraint on (fixture_token, outcome_token) — upserted on refresh.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_DDL = """
CREATE TABLE IF NOT EXISTS pm_inventory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture       TEXT    NOT NULL,
    fixture_token TEXT    NOT NULL,
    question      TEXT    NOT NULL,
    outcome       TEXT    NOT NULL,
    outcome_token TEXT    NOT NULL,
    token_id      TEXT,
    price         REAL,
    liquidity     REAL,
    neg_risk      INTEGER NOT NULL DEFAULT 0,
    settlement_rules TEXT,
    fetched_utc   TEXT    NOT NULL,
    closed        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(fixture_token, outcome_token)
)
"""
_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_pm_inv_fixture ON pm_inventory(fixture_token)",
    "CREATE INDEX IF NOT EXISTS idx_pm_inv_fetched ON pm_inventory(fetched_utc)",
)

_DEFAULT_MAX_AGE_HOURS = 2.0     # re-fetch entries older than this
_DEFAULT_MAX_FIXTURES = 5        # build-time: only next N fixtures
_DEFAULT_MAX_REQUESTS = 20       # HTTP-request budget per refresh run


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _toks(s: Any) -> List[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split()
            if len(t) > 2]


def _fixture_token(fixture: str) -> str:
    return " ".join(sorted(_toks(fixture)))


def _outcome_token(outcome: str) -> str:
    return " ".join(sorted(_toks(outcome)))


def _age_hours(fetched_utc: str, now: datetime) -> float:
    try:
        dt = datetime.strptime(fetched_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def init_db(con: sqlite3.Connection) -> None:
    """Create pm_inventory table and indexes. Idempotent."""
    con.execute(_DDL)
    for idx in _IDX:
        con.execute(idx)
    con.commit()


def _upsert_market(
    con: sqlite3.Connection,
    fixture: str,
    question: str,
    outcome: str,
    token_id: Optional[str],
    price: Optional[float],
    liquidity: Optional[float],
    neg_risk: bool,
    settlement_rules: str,
    fetched_utc: str,
    closed: bool = False,
) -> None:
    ftok = _fixture_token(fixture)
    otok = _outcome_token(outcome)
    con.execute(
        "INSERT INTO pm_inventory "
        "(fixture, fixture_token, question, outcome, outcome_token, token_id, "
        " price, liquidity, neg_risk, settlement_rules, fetched_utc, closed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(fixture_token, outcome_token) DO UPDATE SET "
        "price=excluded.price, liquidity=excluded.liquidity, "
        "token_id=excluded.token_id, fetched_utc=excluded.fetched_utc, "
        "closed=excluded.closed, settlement_rules=excluded.settlement_rules",
        (fixture, ftok, question, outcome, otok, token_id,
         price, liquidity, 1 if neg_risk else 0,
         settlement_rules, fetched_utc, 1 if closed else 0),
    )


def _parse_liquidity(market: Dict[str, Any]) -> Optional[float]:
    liq = market.get("liquidity") or market.get("volume")
    try:
        return float(liq) if liq is not None else None
    except (TypeError, ValueError):
        return None


def _process_event(
    event: Dict[str, Any],
    fixture: str,
    con: sqlite3.Connection,
    fetched_utc: str,
) -> int:
    """Extract markets from one Gamma event and upsert into pm_inventory.

    Returns the number of rows inserted/updated.
    """
    count = 0
    markets = event.get("markets") or []
    closed = bool(event.get("closed") or event.get("resolved"))

    for m in markets:
        question = str(m.get("question") or m.get("groupItemTitle") or "")
        if not question:
            continue

        # Resolve token IDs and prices from the decoded market dict
        from wca.data.polymarket import _parse_json_array, _yes_token_and_price

        outcomes = _parse_json_array(m.get("outcomes")) or []
        token_ids = _parse_json_array(m.get("clobTokenIds")) or []
        prices_raw = _parse_json_array(m.get("outcomePrices")) or []

        prices_f: List[float] = []
        for p in prices_raw:
            try:
                prices_f.append(float(p))
            except (TypeError, ValueError):
                prices_f.append(float("nan"))

        liq = _parse_liquidity(m)
        neg_risk = bool(m.get("negRisk", False))

        for i, outcome in enumerate(outcomes):
            tid = str(token_ids[i]) if i < len(token_ids) else None
            price = prices_f[i] if i < len(prices_f) and not (
                prices_f[i] != prices_f[i]  # isnan check
            ) else None

            # Override price with mid of bestBid/bestAsk when available for YES
            if str(outcome).strip().lower() == "yes":
                bb, ba = m.get("bestBid"), m.get("bestAsk")
                try:
                    if bb is not None and ba is not None:
                        bb_f, ba_f = float(bb), float(ba)
                        if bb_f > 0 and ba_f > 0:
                            price = (bb_f + ba_f) / 2.0
                except (TypeError, ValueError):
                    pass

            _upsert_market(
                con, fixture, question, str(outcome), tid,
                price, liq, neg_risk, question, fetched_utc, closed)
            count += 1

    return count


def refresh_pm_events(
    fixture_names: List[str],
    db_path: str,
    *,
    max_fixtures: int = _DEFAULT_MAX_FIXTURES,
    max_age_hours: float = _DEFAULT_MAX_AGE_HOURS,
    max_requests: int = _DEFAULT_MAX_REQUESTS,
) -> Dict[str, Any]:
    """Build-time refresh of the pm_inventory cache.

    Only fetches events for the NEXT ``max_fixtures`` scheduled fixtures.
    Skips fixtures whose cache entry is fresh (< ``max_age_hours`` old).
    Stops after ``max_requests`` HTTP calls and reports the budget.

    Returns a summary dict:
      {requests_used, requests_budget, fixtures_refreshed, rows_upserted,
       skipped_fresh, errors, fixture_log}
    """
    try:
        from wca.data.polymarket import find_world_cup_markets
    except ImportError:
        return {"error": "wca.data.polymarket not available"}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    init_db(con)
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")

    # Check which fixtures need refreshing
    to_refresh: List[str] = []
    skipped = 0
    for name in fixture_names[:max_fixtures]:
        ftok = _fixture_token(name)
        row = con.execute(
            "SELECT MAX(fetched_utc) FROM pm_inventory WHERE fixture_token=? AND closed=0",
            (ftok,),
        ).fetchone()
        last_fetched = row[0] if row else None
        if last_fetched and _age_hours(last_fetched, now) < max_age_hours:
            skipped += 1
        else:
            to_refresh.append(name)

    requests_used = 0
    rows_upserted = 0
    errors: List[str] = []
    fixture_log: List[Dict[str, Any]] = []

    if not to_refresh:
        con.close()
        return {
            "requests_used": 0, "requests_budget": max_requests,
            "fixtures_refreshed": 0, "rows_upserted": 0,
            "skipped_fresh": skipped, "errors": [], "fixture_log": [],
        }

    # Single Gamma fetch for all WC events (one request covers all fixtures)
    events: List[Dict[str, Any]] = []
    try:
        if requests_used < max_requests:
            events = find_world_cup_markets(include_closed=False)
            requests_used += 1
    except Exception as exc:
        errors.append("find_world_cup_markets: %s" % exc)

    from wca.data.teamnames import canonical as _canonical

    for fixture in to_refresh:
        if requests_used >= max_requests:
            errors.append("request budget exhausted; %s not refreshed" % fixture)
            break

        parts = [p.strip() for p in fixture.replace(" vs ", " v ").split(" v ", 1)]
        if len(parts) != 2:
            errors.append("cannot parse fixture: %s" % fixture)
            continue

        home_c = _canonical(parts[0])
        away_c = _canonical(parts[1])

        # Find the matching event(s) for this fixture
        matched = []
        for ev in events:
            title = (ev.get("title") or "").lower()
            teams_in_title = [_canonical(t.strip())
                              for t in re.split(r" vs\.? | v\.? ", title, flags=re.IGNORECASE)
                              if t.strip()]
            if {home_c, away_c} <= set(teams_in_title):
                matched.append(ev)
            # Also check per-market groupItemTitle teams
            elif any(
                {home_c, away_c} <= {
                    _canonical(git.strip())
                    for git in (m.get("groupItemTitle") or "").split(" vs ")
                    if git.strip()
                }
                for m in (ev.get("markets") or [])
            ):
                matched.append(ev)

        if not matched:
            # Try a targeted search for this fixture (costs a request)
            if requests_used < max_requests:
                try:
                    from wca.data.polymarket import search_events
                    more = search_events("%s %s" % (parts[0], parts[1]), limit=10)
                    requests_used += 1
                    matched.extend(more)
                except Exception as exc:
                    errors.append("search %s: %s" % (fixture, exc))

        count = 0
        for ev in matched:
            count += _process_event(ev, fixture, con, now_str)
        con.commit()
        rows_upserted += count
        fixture_log.append({"fixture": fixture, "events_matched": len(matched), "rows": count})

    con.close()
    return {
        "requests_used": requests_used,
        "requests_budget": max_requests,
        "fixtures_refreshed": len(to_refresh),
        "rows_upserted": rows_upserted,
        "skipped_fresh": skipped,
        "errors": errors,
        "fixture_log": fixture_log,
    }


def get_cached_pm_events(
    db_path: str,
    fixture_names: Optional[List[str]] = None,
    *,
    include_closed: bool = False,
) -> List[Dict[str, Any]]:
    """Read PM inventory from cache (interactive, no network calls).

    Returns a list of row dicts. Each row has:
    {fixture, fixture_token, question, outcome, outcome_token, token_id,
     price, liquidity, neg_risk, settlement_rules, fetched_utc, closed}.
    """
    rows: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        # Ensure table exists (idempotent, harmless)
        try:
            con.execute(_DDL)
            for idx in _IDX:
                con.execute(idx)
        except Exception:
            pass

        conditions = []
        params: List[Any] = []
        if not include_closed:
            conditions.append("closed=0")
        if fixture_names:
            ftoks = [_fixture_token(n) for n in fixture_names]
            placeholders = ",".join("?" * len(ftoks))
            conditions.append("fixture_token IN (%s)" % placeholders)
            params.extend(ftoks)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        for row in con.execute(
            "SELECT * FROM pm_inventory %s ORDER BY fixture_token, question" % where,
            params,
        ).fetchall():
            rows.append(dict(row))
        con.close()
    except Exception:
        pass
    return rows


def pm_price_for_leg(
    db_path: str,
    fixture: str,
    market: str,
    selection: str,
) -> Optional[Dict[str, Any]]:
    """Convenience: look up a PM price for one leg from the cache.

    Returns {price, pm_odds, token_id, question, neg_risk, settlement_rules}
    or None if no matching row.
    """
    ftok = _fixture_token(fixture)
    otok = _outcome_token(selection)
    try:
        con = sqlite3.connect(db_path)
        row = con.execute(
            "SELECT price, token_id, question, neg_risk, settlement_rules "
            "FROM pm_inventory WHERE fixture_token=? AND outcome_token=? AND closed=0 "
            "ORDER BY fetched_utc DESC LIMIT 1",
            (ftok, otok),
        ).fetchone()
        con.close()
        if row is None:
            return None
        price = float(row[0] or 0)
        if not (0 < price < 1):
            return None
        return {
            "price": price,
            "pm_odds": round(1.0 / price, 3),
            "token_id": str(row[1] or ""),
            "question": str(row[2] or ""),
            "neg_risk": bool(row[3]),
            "settlement_rules": str(row[4] or ""),
        }
    except Exception:
        return None
