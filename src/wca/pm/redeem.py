"""Polymarket unfilled-order redemption — emulated GTD ("fill and kill" at 24h).

The V2 CTF-Exchange order struct the bot signs against has **no expiration
field** (V2 dropped V1's ``expiration``; its ``timestamp`` is creation-time-in-ms,
not an expiry — see :mod:`wca.pm.signing`).  So native signed-GTD is impossible
on the exchange we trade.  We get the identical behaviour by resting orders as
GTC and *redeeming* (cancelling) any that are still unfilled past a deadline,
which returns the reserved pUSD.

This module is the pure, testable core:

* :func:`order_age_hours`        — age of a CLOB order (created_at, or local log)
* :func:`select_orders_to_redeem`— which open orders to cancel (24h / all / one)
* :func:`best_bid_ask`           — top-of-book from a CLOB ``/book`` payload
* :func:`pct_off_market`         — how far a resting price is from filling
* :func:`format_unfilled_orders` — the Telegram "Unfilled orders" section

``scripts/wca_pm_redeem.py`` is the CLI/cron that fetches live orders and calls
``cancel_order``; the bot wires the instant-override command and the proposal
message uses :func:`format_unfilled_orders`.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# Default kill window: an order unfilled this long is redeemed.
MAX_AGE_HOURS_DEFAULT = 24.0


def iso_to_epoch(ts: str) -> Optional[float]:
    """Parse a stored ``pm_order_log.ts_utc`` ISO string to Unix seconds (UTC)."""
    if not ts:
        return None
    s = ts.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(
                tzinfo=_dt.timezone.utc
            ).timestamp()
        except ValueError:
            continue
    return None


def log_epoch_by_id(db_path: str) -> Dict[str, float]:
    """Map CLOB order id -> placement epoch from ``pm_order_log`` (live rows).

    Best-effort: a missing table / unreadable DB yields an empty map rather than
    raising, so callers degrade to the CLOB ``created_at`` only.
    """
    out: Dict[str, float] = {}
    try:
        con = sqlite3.connect(db_path)
        try:
            rows = con.execute(
                "SELECT order_id, ts_utc FROM pm_order_log "
                "WHERE order_id IS NOT NULL AND order_id != '' AND dry_run = 0"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return out
    for oid, ts in rows:
        ep = iso_to_epoch(ts or "")
        if oid and ep is not None:
            out[str(oid)] = ep
    return out


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def order_id_of(order: Dict[str, Any]) -> Optional[str]:
    """The CLOB order id under any of its observed key spellings."""
    for k in ("id", "orderID", "order_id", "orderId"):
        v = order.get(k)
        if v:
            return str(v)
    return None


def order_created_epoch(order: Dict[str, Any]) -> Optional[float]:
    """Order creation time in **Unix seconds** from a CLOB order, if present.

    Polymarket returns ``created_at`` in seconds; tolerate camelCase and a
    milliseconds value (>1e12 ⇒ ms) defensively.
    """
    for k in ("created_at", "createdAt", "creation_time", "timestamp"):
        v = _to_float(order.get(k))
        if v is None:
            continue
        return v / 1000.0 if v > 1e12 else v
    return None


def order_age_hours(
    order: Dict[str, Any],
    now_epoch: float,
    *,
    log_epoch_by_id: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """Age of ``order`` in hours.

    Prefer the CLOB ``created_at``; fall back to the local ``pm_order_log``
    placement time keyed by order id (``log_epoch_by_id``).  Returns ``None``
    when neither source knows the order's age (caller decides how to treat it —
    the cron is conservative and skips unknown-age orders unless ``--all``).
    """
    created = order_created_epoch(order)
    if created is None and log_epoch_by_id is not None:
        oid = order_id_of(order)
        if oid is not None:
            created = log_epoch_by_id.get(oid)
    if created is None:
        return None
    return max(0.0, (now_epoch - created) / 3600.0)


def unfilled_size(order: Dict[str, Any]) -> Optional[float]:
    """Remaining (unmatched) size, ``original_size - size_matched`` when both
    are present; else the plain ``size``/``original_size``."""
    orig = _to_float(order.get("original_size") or order.get("originalSize") or order.get("size"))
    matched = _to_float(order.get("size_matched") or order.get("sizeMatched"))
    if orig is None:
        return None
    if matched is None:
        return orig
    return max(0.0, orig - matched)


def select_orders_to_redeem(
    orders: List[Dict[str, Any]],
    now_epoch: float,
    *,
    max_age_hours: float = MAX_AGE_HOURS_DEFAULT,
    order_id: Optional[str] = None,
    redeem_all: bool = False,
    log_epoch_by_id: Optional[Dict[str, float]] = None,
) -> List[Tuple[Dict[str, Any], str]]:
    """Pick the open orders to cancel, with a human reason for each.

    Precedence:
      * ``order_id``   → exactly that order (instant override of one).
      * ``redeem_all`` → every open order (instant override of all).
      * otherwise      → orders whose age ≥ ``max_age_hours``.  Orders whose age
        is unknown are **skipped** (never blind-cancel something we can't date).

    Returns ``[(order, reason), ...]`` preserving input order.
    """
    selected: List[Tuple[Dict[str, Any], str]] = []
    for o in orders:
        oid = order_id_of(o)
        if order_id is not None:
            if oid == str(order_id):
                selected.append((o, "instant override (order %s)" % oid))
            continue
        if redeem_all:
            selected.append((o, "instant override (redeem all)"))
            continue
        age = order_age_hours(o, now_epoch, log_epoch_by_id=log_epoch_by_id)
        if age is None:
            continue  # conservative: unknown age is not auto-redeemed
        if age >= max_age_hours:
            selected.append((o, "unfilled %.1fh ≥ %.0fh" % (age, max_age_hours)))
    return selected


def best_bid_ask(book: Any) -> Tuple[Optional[float], Optional[float]]:
    """Top-of-book ``(best_bid, best_ask)`` from a CLOB ``/book`` payload.

    Polymarket returns ``{"bids": [{price,size}...], "asks": [{price,size}...]}``
    with each side sorted; we take the max bid and min ask rather than trust the
    ordering.  Missing/empty sides yield ``None``.
    """
    if not isinstance(book, dict):
        return None, None

    def _prices(side_key: str) -> List[float]:
        out: List[float] = []
        for lvl in book.get(side_key) or []:
            p = _to_float(lvl.get("price") if isinstance(lvl, dict) else None)
            if p is not None:
                out.append(p)
        return out

    bids = _prices("bids")
    asks = _prices("asks")
    return (max(bids) if bids else None, min(asks) if asks else None)


def pct_off_market(side: str, price: float, book: Any) -> Optional[float]:
    """How far a resting order price is from *filling*, as a percentage.

    The "relevant side" is the side you fill **against**:
      * BUY  → the best **ask** (you fill when the ask reaches your price).
      * SELL → the best **bid**.

    Returns a signed percentage relative to that reference:
      * ``> 0`` ⇒ away from filling (BUY below ask / SELL above bid).
      * ``<= 0`` ⇒ marketable (would cross immediately).
    ``None`` when the relevant book side is empty.
    """
    best_bid, best_ask = best_bid_ask(book)
    s = (side or "").upper()
    if s == "BUY":
        ref = best_ask
        if ref is None or ref == 0:
            return None
        return (ref - price) / ref * 100.0
    if s == "SELL":
        ref = best_bid
        if ref is None or ref == 0:
            return None
        return (price - ref) / ref * 100.0
    return None


def format_unfilled_orders(
    orders: List[Dict[str, Any]],
    books_by_token: Dict[str, Any],
    now_epoch: float,
    *,
    label_by_id: Optional[Dict[str, str]] = None,
    log_epoch_by_id: Optional[Dict[str, float]] = None,
    max_age_hours: float = MAX_AGE_HOURS_DEFAULT,
) -> str:
    """Render the Telegram "Unfilled orders" section.

    Each line shows market/side/price, the relevant best bid/ask with the
    %-off-market, the order's age, and a ``→ redeem`` hint.  ``label_by_id`` maps
    order id → a human market label (from the parked proposal); falls back to the
    order's own ``market``/``asset_id``.  Returns ``""`` when there are no open
    orders (caller omits the section).
    """
    if not orders:
        return ""

    lines = ["📂 *Unfilled PM orders* — %d open" % len(orders)]
    for o in orders:
        oid = order_id_of(o) or "?"
        side = (o.get("side") or "?").upper()
        price = _to_float(o.get("price"))
        token = str(o.get("asset_id") or o.get("token_id") or o.get("tokenId") or "")
        label = (label_by_id or {}).get(oid) or o.get("market") or (token[:10] + "…" if token else "?")

        book = books_by_token.get(token)
        best_bid, best_ask = best_bid_ask(book)
        off = pct_off_market(side, price, book) if price is not None else None
        ref_name = "ask" if side == "BUY" else "bid"
        ref_val = best_ask if side == "BUY" else best_bid

        age = order_age_hours(o, now_epoch, log_epoch_by_id=log_epoch_by_id)
        rem = unfilled_size(o)

        pieces = ["%s %s @ %s" % (side, label, ("%.2f" % price) if price is not None else "?")]
        if rem is not None:
            pieces.append("%.1f sh left" % rem)
        if ref_val is not None and off is not None:
            tag = "marketable" if off <= 0 else "%.1f%% off %s" % (off, ref_name)
            pieces.append("%s %.2f (%s)" % (ref_name, ref_val, tag))
        if age is not None:
            due = "DUE" if age >= max_age_hours else "%.0fh left" % max(0.0, max_age_hours - age)
            pieces.append("age %.1fh, %s" % (age, due))
        lines.append("• " + " | ".join(pieces))
        lines.append("    → `REDEEM %s` to cancel now" % oid)

    lines.append("_Auto-redeems unfilled orders after %.0fh. `REDEEM ALL` cancels every open order._" % max_age_hours)
    return "\n".join(lines)
