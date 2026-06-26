"""Hourly venue-position reconciliation engine (SHADOW -> LIVE safety ladder).

The user places bets MANUALLY at each venue; this job pulls the OPEN POSITIONS
*and the SETTLED positions from the last 24h* from the three venue APIs once an
hour and reconciles them against the SQLite ledger so the canonical ledger stays
in sync with what is actually live AND so resolved bets get settled with VENUE
TRUTH (the venue's own realised P&L + result).

CROSS-MACHINE SPLIT (v2)
------------------------
Betfair's API is reachable from the user's MacBook (the VPN lives there) but NOT
from the Mac mini (the connection is blocked). The canonical ledger
(``data/wca.db``) and the site publish live on the mini. So FETCH and APPLY are
split:

  * FETCH (MacBook, VPN on): :func:`fetch_snapshot` pulls every venue's open +
    settled-24h positions and writes a self-describing JSON snapshot. NO DB
    access.
  * APPLY (mini, canonical ledger): :func:`apply_snapshot` reconciles that
    snapshot against the ledger and — in LIVE — applies the writes.

The classic all-in-one :func:`run_sync` (fetch+apply locally) is kept for
tests/dev.

READ-ONLY on the venues — it NEVER places or cancels orders. The venue fetches
each DEGRADE GRACEFULLY to an empty list (never raise) so one unreachable venue
does not abort the run.

Safety ladder (mirrors the cash-out watcher):

  default            SHADOW  — fetch + reconcile + LOG the proposed ledger
                              changes (inserts, closes AND settles) + refresh the
                              read-only site positions projection. ZERO writes.
  WCA_POSITIONS_LIVE=1  LIVE — apply CONSERVATIVE writes: INSERT new open bets
                              seen at a venue, mark gone-from-venue open ledger
                              bets to a 'closed' status pending settlement, and
                              SETTLE a matched open ledger bet with the venue's
                              own realised P&L + result when the venue reports an
                              unambiguous settle. Idempotent (re-run is a no-op).
                              NEVER places/cancels orders.

Matching is deliberately CONSERVATIVE: a venue position matches a ledger bet
only on (canon venue + normalised selection + normalised market/fixture). Any
position that matches more than one open ledger bet (or vice-versa) goes to a
``review`` list and is NEVER auto-applied. A settle is applied ONLY when the
venue reports an unambiguous WON/LOST result AND it matches exactly one open
ledger bet; ambiguity always routes to review, never to an auto-settle.
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

# Snapshot schema version (bumped if the snapshot shape changes incompatibly).
# v3 adds the per-venue ``venue_status`` map (matcher-safety gate). A v2 snapshot
# (no venue_status) still applies — an absent map is treated as "untracked", i.e.
# legacy-confirmable — see :func:`apply_snapshot`.
SNAPSHOT_VERSION = 3

# Default settled-position lookback window (hours). Open positions are all
# all-current regardless of window; the window only scopes SETTLED fetches so the
# first run can capture a full day of settles.
DEFAULT_SETTLED_LOOKBACK_HOURS = 24

# Status used for a ledger bet no longer seen open at its venue. Distinct from
# 'settled'/'void'/'cashed' — it means "no longer live at the venue, awaiting
# the settler to attach the real result + P&L". It is NOT a realised status.
CLOSED_PENDING_STATUS = "closed"

# Per-venue fetch outcome. ONLY ``VENUE_OK`` (authenticated AND returned a
# complete, non-empty position list) lets a ledger bet be classified
# gone_from_venue/auto-closed. Every other status means "we cannot confirm the
# venue's open positions" -> bets route to review and stay OPEN. This is the
# safety property that kills the false-close bug (a failed/empty fetch wrongly
# making every open ledger bet look 'gone').
VENUE_OK = "ok"            # authenticated + returned a complete position list.
VENUE_AUTH_FAILED = "auth_failed"  # no creds / login rejected — cannot confirm.
VENUE_EMPTY = "empty"      # authed but zero positions — can't tell empty vs silent fail.
VENUE_ERROR = "error"      # fetch raised / network down — cannot confirm.

# The only status under which gone_from_venue/auto-close is permitted.
_CONFIRMABLE_STATUSES = frozenset({VENUE_OK})


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
    out: List[Dict[str, Any]] = []
    for rows in fetch_all_positions_with_status(fetchers)[0].values():
        out.extend(rows)
    return out


# ---------------------------------------------------------------------------
# Auth probes — distinguish "authenticated" from "no creds / login rejected".
# Used by the status-aware fetch so an empty list from an UN-authenticated venue
# is never mistaken for "the account genuinely has no open positions".
# ---------------------------------------------------------------------------


def _betfair_authed() -> bool:
    try:
        from wca.data import betfair_exchange

        return bool(betfair_exchange.creds_available())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Betfair auth probe errored: %s", exc)
        return False


def _smarkets_authed() -> bool:
    try:
        from wca.data import smarkets

        return bool(smarkets.session_login())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Smarkets auth probe errored: %s", exc)
        return False


def _polymarket_authed() -> bool:
    # Polymarket positions come from a public data-api read (no account auth);
    # if the wrapper itself imports/works we treat it as authenticated. A failure
    # to fetch is caught below as VENUE_ERROR.
    try:
        from wca import sitedata  # noqa: F401

        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket auth probe errored: %s", exc)
        return False


# venue name -> (fetcher, auth-probe). Auth probes are skipped for injected
# test fetchers (those are assumed authenticated unless they raise/return empty).
_DEFAULT_AUTH_PROBES: Dict[str, Callable[[], bool]] = {
    "betfair": _betfair_authed,
    "smarkets": _smarkets_authed,
    "polymarket": _polymarket_authed,
}


def _classify_fetch(
    name: str,
    fn: Callable[[], List[Dict[str, Any]]],
    auth_probe: Optional[Callable[[], bool]],
) -> "tuple[str, List[Dict[str, Any]]]":
    """Run one venue fetch and classify the outcome into a VENUE_* status.

    - auth probe says NOT authenticated -> ``auth_failed`` (cannot confirm).
    - fetch raises                       -> ``error``       (cannot confirm).
    - fetch returns []                   -> ``empty``       (cannot confirm).
    - fetch returns rows                 -> ``ok``          (confirmable).
    """
    if auth_probe is not None:
        try:
            if not auth_probe():
                logger.info("%s fetch: auth_failed (no creds / login rejected)", name)
                return VENUE_AUTH_FAILED, []
        except Exception as exc:  # noqa: BLE001 — a broken probe is not-confirmable.
            logger.warning("%s auth probe raised (auth_failed): %s", name, exc)
            return VENUE_AUTH_FAILED, []
    try:
        rows = fn() or []
    except Exception as exc:  # noqa: BLE001 — belt-and-braces.
        logger.warning("%s fetcher raised (status=error): %s", name, exc)
        return VENUE_ERROR, []
    if not rows:
        logger.info("%s fetch: empty (authed but zero positions — not confirmable)", name)
        return VENUE_EMPTY, []
    return VENUE_OK, rows


def fetch_all_positions_with_status(
    fetchers: Optional[Dict[str, Callable[[], List[Dict[str, Any]]]]] = None,
    auth_probes: Optional[Dict[str, Callable[[], bool]]] = None,
) -> "tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]":
    """Pull every venue's open positions AND record a per-venue fetch status.

    Returns ``(rows_by_venue, venue_status)`` where ``venue_status`` maps the
    canonical venue name to a ``VENUE_*`` status. The status is what makes the
    reconcile safe: only ``VENUE_OK`` venues can have their ledger bets marked
    gone_from_venue.

    When ``fetchers`` is supplied (tests), the matching default auth probe is
    used unless ``auth_probes`` overrides it; an injected fetcher with no probe
    is assumed authenticated (so an empty injected list is still ``empty``, and
    a non-empty one is ``ok``).
    """
    use_default = fetchers is None
    fetchers = fetchers or {
        "betfair": fetch_betfair_positions,
        "smarkets": fetch_smarkets_positions,
        "polymarket": fetch_polymarket_positions,
    }
    probes = dict(_DEFAULT_AUTH_PROBES) if use_default else {}
    if auth_probes:
        probes.update(auth_probes)

    rows_by_venue: Dict[str, List[Dict[str, Any]]] = {}
    venue_status: Dict[str, str] = {}
    for name, fn in fetchers.items():
        status, rows = _classify_fetch(name, fn, probes.get(name))
        canon = canon_platform(name)
        rows_by_venue[canon] = rows_by_venue.get(canon, []) + rows
        # If two raw names map to one canon venue, keep the most-confident status.
        prev = venue_status.get(canon)
        venue_status[canon] = _merge_status(prev, status)
    return rows_by_venue, venue_status


def _merge_status(prev: Optional[str], new: str) -> str:
    """Combine two statuses for the same canonical venue, keeping the MOST
    confident (ok beats empty beats auth_failed/error). Used only when two raw
    fetcher names collapse to one canon venue (rare)."""
    order = {VENUE_OK: 3, VENUE_EMPTY: 2, VENUE_AUTH_FAILED: 1, VENUE_ERROR: 1}
    if prev is None:
        return new
    return prev if order.get(prev, 0) >= order.get(new, 0) else new


# ---------------------------------------------------------------------------
# Venue SETTLED fetch (24h lookback, read-only, each degrades to []).
# ---------------------------------------------------------------------------


def fetch_betfair_settled(since_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS
                          ) -> List[Dict[str, Any]]:
    """Settled Betfair Exchange positions over ``since_hours`` (never raises)."""
    try:
        from wca.data import betfair_exchange

        return betfair_exchange.list_cleared_orders(since_hours=since_hours) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Betfair settled fetch errored (degrading): %s", exc)
        return []


def fetch_smarkets_settled(since_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS
                           ) -> List[Dict[str, Any]]:
    """Settled Smarkets positions over ``since_hours`` (never raises)."""
    try:
        from wca.data import smarkets

        return smarkets.list_settled_positions(since_hours=since_hours) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Smarkets settled fetch errored (degrading): %s", exc)
        return []


def fetch_polymarket_settled(since_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS
                             ) -> List[Dict[str, Any]]:
    """Settled (resolved) Polymarket positions over ``since_hours`` (never raises).

    Thin wrapper over :func:`wca.sitedata.settled_pm_positions`; already returns
    the normalised settled shape.
    """
    try:
        from wca import sitedata

        return sitedata.settled_pm_positions(since_hours=since_hours) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket settled fetch errored (degrading): %s", exc)
        return []


def fetch_all_settled(
    since_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS,
    fetchers: Optional[Dict[str, Callable[[int], List[Dict[str, Any]]]]] = None,
) -> List[Dict[str, Any]]:
    """Pull + concatenate all venues' SETTLED positions over ``since_hours``.

    Each fetcher degrades to ``[]`` independently. ``fetchers`` is injectable for
    tests (maps venue -> callable taking ``since_hours``).
    """
    fetchers = fetchers or {
        "betfair": fetch_betfair_settled,
        "smarkets": fetch_smarkets_settled,
        "polymarket": fetch_polymarket_settled,
    }
    out: List[Dict[str, Any]] = []
    for name, fn in fetchers.items():
        try:
            rows = fn(since_hours) or []
        except Exception as exc:  # noqa: BLE001 — belt-and-braces.
            logger.warning("%s settled fetcher raised (degrading to empty): %s", name, exc)
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
    settle: List[Dict[str, Any]] = field(default_factory=list)
    review: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "new_at_venue": self.new_at_venue,
            "gone_from_venue": self.gone_from_venue,
            "matched": self.matched,
            "settle": self.settle,
            "review": self.review,
            "counts": {
                "new_at_venue": len(self.new_at_venue),
                "gone_from_venue": len(self.gone_from_venue),
                "matched": len(self.matched),
                "settle": len(self.settle),
                "review": len(self.review),
            },
        }


def _is_unambiguous_settle(s: Dict[str, Any]) -> bool:
    """True only when a settled-venue row carries an unambiguous WON/LOST result
    AND a numeric realised P&L. Anything else is NOT safe to auto-settle."""
    result = _norm(s.get("result"))
    if result not in ("won", "lost"):
        return False
    pnl = s.get("settled_pnl")
    if pnl is None:
        return False
    try:
        float(pnl)
    except (TypeError, ValueError):
        return False
    return True


def reconcile(
    venue_positions: List[Dict[str, Any]],
    ledger_bets: List[Dict[str, Any]],
    settled_positions: Optional[List[Dict[str, Any]]] = None,
    venue_status: Optional[Dict[str, str]] = None,
) -> Reconciliation:
    """Classify OPEN venue positions vs open ledger bets, and (new in v2) match
    SETTLED venue positions to open ledger bets for venue-truth settlement.

    Open-position classification:
    - ``matched``       one open venue position <-> exactly one ledger bet.
    - ``new_at_venue``  open at a venue, no ledger bet (would INSERT).
    - ``gone_from_venue`` open in ledger, not at the venue, **AND the venue's
                        fetch was confirmable (``VENUE_OK``)** -> would mark
                        CLOSED pending settlement (never auto-compute P&L).

    SAFETY (the critical fix): a ledger bet whose venue fetch was NOT confirmable
    — ``auth_failed`` / ``empty`` / ``error`` (or a venue absent from
    ``venue_status``) — is NEVER classified gone_from_venue. It stays OPEN and is
    routed to ``review`` with reason ``"venue_unavailable"`` ("venue unavailable
    — cannot confirm"). This is what stops a failed/empty venue fetch from
    wrongly auto-closing every open ledger bet at that venue.

    ``venue_status`` maps a canon venue name -> a ``VENUE_*`` status. When it is
    ``None`` (legacy callers / all-in-one tests with no status tracking) every
    venue is treated as confirmable so existing behaviour is preserved.

    Settle classification (v2): a SETTLED venue position with an unambiguous
    WON/LOST result + realised P&L that matches EXACTLY ONE open ledger bet ->
    ``settle``. Any ambiguity routes to ``review`` and is NEVER auto-settled.

    ``review`` collects every ambiguous OR unconfirmable case.
    """
    settled_positions = settled_positions or []

    # Group OPEN venue positions + ledger bets by conservative key.
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

    # Group SETTLED venue positions by the same conservative key.
    s_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for s in settled_positions:
        k = _match_key(s.get("venue"), s.get("selection"), s.get("market"),
                       s.get("fixture_or_event"))
        s_by_key.setdefault(k, []).append(s)

    rec = Reconciliation()

    # --- Settle pass first: claim ledger bets that a venue reports settled. ---
    # A claimed ledger bet is removed from the open-classification below so a
    # just-settled bet is not ALSO flagged gone_from_venue.
    claimed_keys: set = set()
    for k in sorted(s_by_key):
        ss = s_by_key[k]
        ls = l_by_key.get(k, [])
        vs_open = v_by_key.get(k, [])
        confident = (
            len(ss) == 1
            and len(ls) == 1
            and not vs_open  # still open at the venue ⇒ contradictory ⇒ review.
            and _is_unambiguous_settle(ss[0])
        )
        if confident:
            rec.settle.append({"key": k, "venue": ss[0], "ledger": ls[0]})
        else:
            rec.review.append({
                "key": k, "reason": "ambiguous_settle",
                "venue_settled": ss, "ledger_bets": ls, "venue_open": vs_open,
            })
        # Either way the key is CLAIMED: a confident settle handles it, and an
        # ambiguous settle is already in review — neither should be re-classified
        # (and re-reported) by the open-position pass below.
        claimed_keys.add(k)

    # --- Open-position classification over keys NOT claimed by a settle. ---
    seen_keys = (set(v_by_key) | set(l_by_key)) - claimed_keys
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
            # CRITICAL SAFETY GATE: only mark a ledger bet gone_from_venue when
            # its venue's fetch was confirmable (VENUE_OK). An auth_failed /
            # empty / errored / unknown venue CANNOT confirm the bet is gone, so
            # the bet stays OPEN and goes to review instead of being auto-closed.
            bet = ls[0]
            status = _venue_status_for_bet(bet, venue_status)
            if _is_confirmable(status, venue_status):
                rec.gone_from_venue.append(bet)
            else:
                rec.review.append({
                    "key": k,
                    "reason": "venue_unavailable",
                    "detail": "venue unavailable — cannot confirm position is gone",
                    "venue_status": status,
                    "ledger_bets": ls,
                })
    return rec


def _venue_status_for_bet(
    bet: Dict[str, Any], venue_status: Optional[Dict[str, str]]
) -> Optional[str]:
    """Return the VENUE_* status for a ledger bet's venue (None if unknown)."""
    if not venue_status:
        return None
    return venue_status.get(canon_platform(bet.get("platform")))


def _is_confirmable(status: Optional[str], venue_status: Optional[Dict[str, str]]) -> bool:
    """True when a gone_from_venue/auto-close is SAFE for this venue.

    Legacy behaviour: when ``venue_status`` is ``None`` (caller did not track
    status), every venue is treated as confirmable so the existing all-in-one
    tests/dev path is unchanged. Once status IS tracked, ONLY ``VENUE_OK``
    confirms — an unknown / auth_failed / empty / errored venue never does.
    """
    if venue_status is None:
        return True
    return status in _CONFIRMABLE_STATUSES


# ---------------------------------------------------------------------------
# Apply (LIVE only — conservative, idempotent).
# ---------------------------------------------------------------------------


def _now() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _settle_status(result: str) -> str:
    """Map a normalised venue result to the ledger status."""
    return "won" if _norm(result) == "won" else "lost"


def apply_reconciliation(
    rec: Reconciliation, db_path: str
) -> Dict[str, Any]:
    """Apply conservative ledger writes for a reconciliation (LIVE path).

    - INSERT each ``new_at_venue`` position as an OPEN bet (source='manual').
    - SETTLE each ``settle`` ledger bet with the venue's OWN realised P&L +
      result (venue truth — no recomputation). Only OPEN bets are settled, so a
      re-run is a no-op (the bet is no longer open).
    - Mark each ``gone_from_venue`` ledger bet ``CLOSED_PENDING_STATUS``.
    - ``matched`` and ``review`` are left untouched.

    Idempotent: a re-run inserts nothing new (the inserted bets now match), a
    re-settle of an already-settled bet is a no-op (status guard), and
    re-marking an already-closed bet is a no-op. NEVER places/cancels orders.
    """
    from wca.ledger import store

    store.init_db(db_path)
    result = {"inserted": [], "closed": [], "settled": []}

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
        # Ensure the settled_ts column exists on pre-existing DBs (store helper).
        store._ensure_settled_ts_column(conn)  # noqa: SLF001 — same package.

        # --- Venue-truth settles: write the venue's realised P&L directly. ---
        for item in rec.settle:
            ledger = item.get("ledger") or {}
            venue = item.get("venue") or {}
            bet_id = ledger.get("id")
            if bet_id is None:
                continue
            status = _settle_status(venue.get("result"))
            try:
                pnl = float(venue.get("settled_pnl"))
            except (TypeError, ValueError):
                logger.warning("positions_sync settle skipped (bad P&L) for bet %s", bet_id)
                continue
            settled_ts = venue.get("settled_ts") or _now()
            cur = conn.execute(
                "UPDATE bets SET status=?, settled_pl=?, settled_ts=? "
                "WHERE id=? AND status='open'",
                (status, pnl, settled_ts, bet_id),
            )
            if cur.rowcount:
                result["settled"].append({
                    "bet_id": int(bet_id), "result": status, "settled_pl": pnl,
                })

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
    settled_fetchers: Optional[Dict[str, Callable[[int], List[Dict[str, Any]]]]] = None,
    settled_lookback_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS,
) -> Dict[str, Any]:
    """Run one ALL-IN-ONE reconciliation pass (fetch + apply locally).

    Kept for tests/dev; production splits FETCH (MacBook) from APPLY (mini) via
    :func:`fetch_snapshot` / :func:`apply_snapshot`.

    ``live`` defaults to :func:`live_env` (``WCA_POSITIONS_LIVE=1``). In SHADOW
    (default) NO ledger writes occur — only the report + site projection
    refresh. In LIVE, conservative writes are applied (insert / settle / close).
    """
    if live is None:
        live = live_env()
    rows_by_venue, venue_status = fetch_all_positions_with_status(fetchers)
    venue_positions = [r for rows in rows_by_venue.values() for r in rows]
    settled_positions = fetch_all_settled(settled_lookback_hours, settled_fetchers)
    ledger_bets = load_open_ledger_bets(db_path)
    rec = reconcile(venue_positions, ledger_bets, settled_positions, venue_status)

    report = _build_report(rec, len(venue_positions), len(settled_positions),
                           len(ledger_bets), live, db_path,
                           settled_lookback_hours, venue_status)
    return report


def _build_report(
    rec: Reconciliation,
    venue_count: int,
    settled_count: int,
    ledger_count: int,
    live: bool,
    db_path: str,
    settled_lookback_hours: int,
    venue_status: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Assemble the JSON-able report + (in LIVE) apply the reconciliation."""
    mode = "LIVE" if live else "SHADOW"
    counts = rec.as_dict()["counts"]
    report: Dict[str, Any] = {
        "mode": mode,
        "ts_utc": _now(),
        "settled_lookback_hours": settled_lookback_hours,
        "venue_position_count": venue_count,
        "venue_settled_count": settled_count,
        "open_ledger_count": ledger_count,
        # Per-venue fetch status so the operator can SEE which venues were live
        # this run (only VENUE_OK venues can have bets auto-closed).
        "venue_status": dict(venue_status or {}),
        "reconciliation": rec.as_dict(),
        "applied": None,
    }
    report["site_projection_pm_rows"] = refresh_site_projection()

    if live:
        report["applied"] = apply_reconciliation(rec, db_path)
        logger.info(
            "positions_sync LIVE: inserted=%s settled=%s closed=%s",
            report["applied"]["inserted"], report["applied"]["settled"],
            report["applied"]["closed"],
        )
    else:
        logger.info(
            "positions_sync SHADOW: would insert %d, settle %d, close %d, "
            "matched %d, review %d (NO ledger writes)",
            counts["new_at_venue"], counts["settle"], counts["gone_from_venue"],
            counts["matched"], counts["review"],
        )
    return report


# ---------------------------------------------------------------------------
# FETCH / APPLY split (cross-machine: fetch on the MacBook, apply on the mini).
# ---------------------------------------------------------------------------


def fetch_snapshot(
    *,
    settled_lookback_hours: int = DEFAULT_SETTLED_LOOKBACK_HOURS,
    fetchers: Optional[Dict[str, Callable[[], List[Dict[str, Any]]]]] = None,
    settled_fetchers: Optional[Dict[str, Callable[[int], List[Dict[str, Any]]]]] = None,
) -> Dict[str, Any]:
    """FETCH-ONLY: pull every venue's open + settled-24h positions into a
    self-describing snapshot. NO DB ACCESS — this runs on the MacBook (VPN on).

    The snapshot round-trips through JSON and is applied on the mini via
    :func:`apply_snapshot`.
    """
    rows_by_venue, venue_status = fetch_all_positions_with_status(fetchers)
    open_positions = [r for rows in rows_by_venue.values() for r in rows]
    settled_positions = fetch_all_settled(settled_lookback_hours, settled_fetchers)
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "fetched_at": _now(),
        "settled_lookback_hours": settled_lookback_hours,
        # Per-venue fetch status travels WITH the snapshot so the mini's apply
        # step knows which venues were confirmable when the MacBook fetched.
        "venue_status": venue_status,
        "open_positions": open_positions,
        "settled_positions": settled_positions,
        "counts": {
            "open": len(open_positions),
            "settled": len(settled_positions),
        },
    }


def apply_snapshot(
    snapshot: Dict[str, Any],
    db_path: str,
    *,
    live: Optional[bool] = None,
) -> Dict[str, Any]:
    """APPLY a fetched snapshot against the canonical ledger. Runs on the mini.

    Reconciles the snapshot's open + settled positions against the open ledger
    bets and (in LIVE) applies the conservative writes. SHADOW (default) reports
    only. Validates the snapshot shape and degrades safely on a malformed one.
    """
    if live is None:
        live = live_env()
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict")
    ver = snapshot.get("snapshot_version")
    if ver is not None and ver > SNAPSHOT_VERSION:
        logger.warning("snapshot version %s newer than supported %s; proceeding",
                       ver, SNAPSHOT_VERSION)
    open_positions = list(snapshot.get("open_positions") or [])
    settled_positions = list(snapshot.get("settled_positions") or [])
    lookback = int(snapshot.get("settled_lookback_hours") or DEFAULT_SETTLED_LOOKBACK_HOURS)
    # v3+ snapshots carry the per-venue fetch status; a pre-v3 (v2) snapshot has
    # none, in which case we pass None -> legacy "all confirmable" behaviour
    # (only affects snapshots created before this hardening shipped).
    raw_status = snapshot.get("venue_status")
    venue_status = dict(raw_status) if isinstance(raw_status, dict) else None

    ledger_bets = load_open_ledger_bets(db_path)
    rec = reconcile(open_positions, ledger_bets, settled_positions, venue_status)

    report = _build_report(rec, len(open_positions), len(settled_positions),
                           len(ledger_bets), live, db_path, lookback, venue_status)
    report["snapshot_fetched_at"] = snapshot.get("fetched_at")
    report["source"] = "snapshot"
    return report
