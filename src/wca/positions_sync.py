"""Hourly venue-position reconciliation engine (SHADOW -> LIVE safety ladder).

The user places bets MANUALLY at each venue; this job pulls the OPEN POSITIONS
from the three venue APIs once an hour and reconciles them against the SQLite
ledger so the canonical ledger stays in sync with what is actually live.

READ-ONLY on the venues — it NEVER places or cancels orders. The venue fetches
each DEGRADE GRACEFULLY to an empty list (never raise) so one unreachable venue
(currently Betfair on the mini) does not abort the run.

Safety ladder (mirrors the cash-out watcher):

  default            SHADOW  — fetch + reconcile + LOG the proposed ledger
                              changes + refresh the read-only site positions
                              projection. ZERO ledger writes.
  WCA_POSITIONS_LIVE=1  LIVE — apply CONSERVATIVE writes: INSERT new open bets
                              seen at a venue, mark gone-from-venue ledger bets
                              to a 'closed' status pending settlement. Idempotent
                              (re-run is a no-op). NEVER auto-settles P&L and
                              NEVER places/cancels orders — settlement with the
                              real result stays the settler's job.

Matching is deliberately CONSERVATIVE: a venue position matches a ledger bet
only on (canon venue + normalised selection + normalised market/fixture). Any
position that matches more than one open ledger bet (or vice-versa) goes to a
``review`` list and is NEVER auto-applied.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from wca.venues import canon_platform

logger = logging.getLogger(__name__)

# Status used for a ledger bet no longer seen open at its venue. Distinct from
# 'settled'/'void'/'cashed' — it means "no longer live at the venue, awaiting
# the settler to attach the real result + P&L". It is NOT a realised status.
CLOSED_PENDING_STATUS = "closed"


def live_env() -> bool:
    """True when LIVE ledger writes are enabled (``WCA_POSITIONS_LIVE=1``)."""
    return os.environ.get("WCA_POSITIONS_LIVE", "").strip() == "1"


def _norm(s: Any) -> str:
    """Lowercase, collapse whitespace — the conservative match key normaliser."""
    return " ".join(str(s or "").strip().lower().split())


def _match_key(venue: str, selection: Any, market: Any, fixture: Any) -> str:
    """Conservative match key: canon venue + selection + market/fixture.

    Falls back to fixture when market is empty (and vice-versa) so a venue
    position and a ledger bet that carry the descriptor in different fields can
    still align.
    """
    mk = _norm(market) or _norm(fixture)
    return "|".join((canon_platform(venue), _norm(selection), mk))


# ---------------------------------------------------------------------------
# Venue fetch (read-only, each degrades to []).
# ---------------------------------------------------------------------------


def fetch_betfair_positions() -> List[Dict[str, Any]]:
    """Open Betfair Exchange positions (never raises)."""
    try:
        from wca.data import betfair_exchange

        return betfair_exchange.list_current_orders() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Betfair positions fetch errored (degrading): %s", exc)
        return []


def fetch_smarkets_positions() -> List[Dict[str, Any]]:
    """Open Smarkets positions (never raises)."""
    try:
        from wca.data import smarkets

        return smarkets.list_open_positions() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Smarkets positions fetch errored (degrading): %s", exc)
        return []


def fetch_polymarket_positions() -> List[Dict[str, Any]]:
    """Open Polymarket positions, normalised to the shared shape (never raises).

    Thin wrapper over :func:`wca.sitedata.live_pm_positions`.
    """
    try:
        from wca import sitedata

        rows = sitedata.live_pm_positions() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket positions fetch errored (degrading): %s", exc)
        return []
    out: List[Dict[str, Any]] = []
    for p in rows:
        out.append({
            "venue": "polymarket",
            "market": p.get("market"),
            "selection": p.get("selection"),
            "fixture_or_event": p.get("match"),
            "stake": p.get("stake"),
            "size": p.get("shares"),
            "avg_price": p.get("avg_price"),
            "odds": p.get("decimal_odds"),
            "current_value": p.get("cur_value"),
            "current_price": p.get("cur_price"),
            "external_id": p.get("match_id"),
            "account": p.get("account") or "1",
            "token_id": p.get("match_id"),
        })
    return out


def fetch_all_positions(
    fetchers: Optional[Dict[str, Callable[[], List[Dict[str, Any]]]]] = None,
) -> List[Dict[str, Any]]:
    """Pull + concatenate all venues' open positions. Each fetcher degrades to
    ``[]`` independently, so an unreachable venue never aborts the run.

    ``fetchers`` is injectable for tests (maps venue -> callable).
    """
    fetchers = fetchers or {
        "betfair": fetch_betfair_positions,
        "smarkets": fetch_smarkets_positions,
        "polymarket": fetch_polymarket_positions,
    }
    out: List[Dict[str, Any]] = []
    for name, fn in fetchers.items():
        try:
            rows = fn() or []
        except Exception as exc:  # noqa: BLE001 — belt-and-braces.
            logger.warning("%s fetcher raised (degrading to empty): %s", name, exc)
            rows = []
        out.extend(rows)
    return out


# ---------------------------------------------------------------------------
# Ledger read (strictly read-only).
# ---------------------------------------------------------------------------


def load_open_ledger_bets(db_path: str) -> List[Dict[str, Any]]:
    """Return all OPEN ledger bets as plain dicts (read-only)."""
    from wca.ledger import store

    store.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM bets WHERE status='open' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reconcile.
# ---------------------------------------------------------------------------


@dataclass
class Reconciliation:
    new_at_venue: List[Dict[str, Any]] = field(default_factory=list)
    gone_from_venue: List[Dict[str, Any]] = field(default_factory=list)
    matched: List[Dict[str, Any]] = field(default_factory=list)
    review: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "new_at_venue": self.new_at_venue,
            "gone_from_venue": self.gone_from_venue,
            "matched": self.matched,
            "review": self.review,
            "counts": {
                "new_at_venue": len(self.new_at_venue),
                "gone_from_venue": len(self.gone_from_venue),
                "matched": len(self.matched),
                "review": len(self.review),
            },
        }


def reconcile(
    venue_positions: List[Dict[str, Any]],
    ledger_bets: List[Dict[str, Any]],
) -> Reconciliation:
    """Classify venue positions vs open ledger bets.

    - ``matched``       one venue position <-> exactly one ledger bet by key.
    - ``new_at_venue``  open at a venue, no ledger bet (would INSERT).
    - ``gone_from_venue`` open in ledger, not at the venue (would mark CLOSED
                        pending settlement — never auto-compute P&L).
    - ``review``        ambiguous: a key with >1 venue position or >1 ledger bet
                        on either side. NEVER auto-applied.
    """
    # Group by conservative key.
    v_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for v in venue_positions:
        k = _match_key(v.get("venue"), v.get("selection"), v.get("market"),
                       v.get("fixture_or_event"))
        v_by_key.setdefault(k, []).append(v)

    l_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for b in ledger_bets:
        k = _match_key(b.get("platform"), b.get("selection"), b.get("market"),
                       b.get("match_desc"))
        l_by_key.setdefault(k, []).append(b)

    rec = Reconciliation()
    seen_keys = set(v_by_key) | set(l_by_key)
    for k in sorted(seen_keys):
        vs = v_by_key.get(k, [])
        ls = l_by_key.get(k, [])
        if len(vs) > 1 or len(ls) > 1:
            rec.review.append({"key": k, "venue_positions": vs, "ledger_bets": ls})
            continue
        if vs and ls:
            rec.matched.append({"key": k, "venue": vs[0], "ledger": ls[0]})
        elif vs and not ls:
            rec.new_at_venue.append(vs[0])
        elif ls and not vs:
            rec.gone_from_venue.append(ls[0])
    return rec


# ---------------------------------------------------------------------------
# Apply (LIVE only — conservative, idempotent).
# ---------------------------------------------------------------------------


def _now() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def apply_reconciliation(
    rec: Reconciliation, db_path: str
) -> Dict[str, Any]:
    """Apply conservative ledger writes for a reconciliation (LIVE path).

    - INSERT each ``new_at_venue`` position as an OPEN bet (source='manual').
    - Mark each ``gone_from_venue`` ledger bet ``CLOSED_PENDING_STATUS``.
    - ``matched`` and ``review`` are left untouched.

    Idempotent: a re-run inserts nothing new (the inserted bets now match) and
    re-marking an already-closed bet is a no-op. NEVER settles P&L, NEVER
    places/cancels orders.
    """
    from wca.ledger import store

    store.init_db(db_path)
    result = {"inserted": [], "closed": []}

    for v in rec.new_at_venue:
        odds = v.get("odds") or v.get("avg_price")
        stake = v.get("stake") or v.get("size") or 0.0
        try:
            bet_id = store.record_bet(
                ts_utc=_now(),
                match_id=str(v.get("external_id") or "VENUE_SYNC"),
                match_desc=str(v.get("fixture_or_event") or ""),
                market=str(v.get("market") or ""),
                selection=str(v.get("selection") or ""),
                platform=str(v.get("venue") or "Unknown"),
                decimal_odds=float(odds) if odds else 0.0,
                stake=float(stake) if stake else 0.0,
                notes="auto-captured from venue position sync",
                account=str(v.get("account") or "1"),
                source="manual",
                token_id=str(v.get("token_id")) if v.get("token_id") else None,
                sync_site=False,
                db_path=db_path,
            )
            result["inserted"].append(bet_id)
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort.
            logger.warning("positions_sync insert failed for %r: %s", v, exc)

    conn = sqlite3.connect(db_path)
    try:
        for b in rec.gone_from_venue:
            bet_id = b.get("id")
            if bet_id is None:
                continue
            cur = conn.execute(
                "UPDATE bets SET status=? WHERE id=? AND status='open'",
                (CLOSED_PENDING_STATUS, bet_id),
            )
            if cur.rowcount:
                result["closed"].append(int(bet_id))
        conn.commit()
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Site projection refresh (read-only) — SHADOW and LIVE both refresh it.
# ---------------------------------------------------------------------------


def refresh_site_projection() -> Optional[int]:
    """Refresh the read-only live-positions projection the site consumes today.

    This is the same Polymarket positions feed ``live_pm_positions`` already
    powers; here we simply re-pull it so the projection is current. It writes
    nothing to the ledger. Returns the number of live PM rows, or ``None`` on
    failure (never raises).
    """
    try:
        from wca import sitedata

        rows = sitedata.live_pm_positions()
        return len(rows or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("site projection refresh failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def run_sync(
    db_path: str,
    *,
    live: Optional[bool] = None,
    fetchers: Optional[Dict[str, Callable[[], List[Dict[str, Any]]]]] = None,
) -> Dict[str, Any]:
    """Run one reconciliation pass and return a JSON-able report.

    ``live`` defaults to :func:`live_env` (``WCA_POSITIONS_LIVE=1``). In SHADOW
    (default) NO ledger writes occur — only the report + site projection
    refresh. In LIVE, conservative writes are applied.
    """
    if live is None:
        live = live_env()
    venue_positions = fetch_all_positions(fetchers)
    ledger_bets = load_open_ledger_bets(db_path)
    rec = reconcile(venue_positions, ledger_bets)

    mode = "LIVE" if live else "SHADOW"
    report: Dict[str, Any] = {
        "mode": mode,
        "ts_utc": _now(),
        "venue_position_count": len(venue_positions),
        "open_ledger_count": len(ledger_bets),
        "reconciliation": rec.as_dict(),
        "applied": None,
    }

    # Site projection is read-only; refresh in both modes.
    report["site_projection_pm_rows"] = refresh_site_projection()

    if live:
        report["applied"] = apply_reconciliation(rec, db_path)
        logger.info("positions_sync LIVE: inserted=%s closed=%s",
                    report["applied"]["inserted"], report["applied"]["closed"])
    else:
        logger.info(
            "positions_sync SHADOW: would insert %d, close %d, matched %d, review %d "
            "(NO ledger writes)",
            rec.as_dict()["counts"]["new_at_venue"],
            rec.as_dict()["counts"]["gone_from_venue"],
            rec.as_dict()["counts"]["matched"],
            rec.as_dict()["counts"]["review"],
        )
    return report
