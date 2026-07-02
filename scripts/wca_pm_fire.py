#!/usr/bin/env python
"""One-shot Polymarket order placer for a single bet-recs advancement rec.

Runs ON THE MINI (the box that owns the canonical ledger, ``data/wca.db``).
Given a rec id from ``site/bet_recs.json`` it:

  1. Loads the rec and refuses anything that is not an actionable, non-stale
     ``ADD`` on the ``polymarket`` venue.
  2. RE-RESOLVES the live Polymarket market for the rec's ``team``+``stage`` at
     fire time (never trusts the stale ``pm_price`` in the JSON): the same
     ``find_world_cup_markets`` + YES-token resolution the advancement pipeline
     uses.  Refuses if there is no live market or the price moved beyond a
     sanity band vs the rec.
  3. Sizes with a HARD USD cap: ``usd = min(rec.stake, --max-usd)`` clamped to
     ``--max-usd``; ``size_shares = usd / price``.
  4. Enforces IDEMPOTENCY via a ``pm_fire_log`` table keyed by rec-id + nonce —
     a second fire of the same rec within the window (or with a matching nonce)
     is refused, so a double-click / retry can never double-spend.
  5. Builds the proposal and calls
     ``wca.bot.app._execute_parked_order`` — the SAME signed-order + ledger path
     the Telegram bot uses.  That function honours ``PM_DRY_RUN`` (DEFAULT ON):
     dry-run signs but does NOT submit and does NOT touch the ledger's live
     order log.

SAFETY: nothing here forces live mode.  ``PM_DRY_RUN`` defaults to ON (see
``wca.bot.app._pm_dry_run``); the order goes live only when the human exports
``PM_DRY_RUN=0`` in the environment that invokes this script.

Output: exactly one JSON line on stdout:
    {"ok":bool,"dry_run":bool,"order_id":..,"bid":..,"usd":..,"size":..,
     "price":..,"rec_id":..,"message":..}

Usage
-----
    # dry-run (default) against a real rec id
    PM_DRY_RUN=1 python scripts/wca_pm_fire.py --rec-id belgium_qf_pm \
        --max-usd 100 --db data/wca.db --bet-recs site/bet_recs.json \
        --nonce click-abc123
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# Make ``src`` importable when run directly (worktree / mini both work).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ---------------------------------------------------------------------------
# Defaults / knobs
# ---------------------------------------------------------------------------

# HARD ceiling on a single fire, in USD notional.  --max-usd can only LOWER
# the effective cap, never raise it above this absolute backstop.
ABSOLUTE_MAX_USD: float = 200.0  # raised 2026-07-02 with the full-pool sizing (was 100)

# Idempotency window: a fresh fire of the SAME rec-id inside this many minutes
# is refused even with a different nonce (guards a fast double-click / retry).
IDEMPOTENCY_WINDOW_MIN: int = 30

# Price sanity band: refuse if the live YES price has moved by more than this
# fraction (absolute, in price points) away from the rec's stored ``pm_price``.
# e.g. 0.08 = 8 cents of price on a 0..1 probability scale.
PRICE_SANITY_BAND: float = 0.08


# ---------------------------------------------------------------------------
# Rec loading / validation
# ---------------------------------------------------------------------------

def _load_rec(
    bet_recs_path: str,
    rec_id: Optional[str],
    *,
    team: Optional[str] = None,
    selection: Optional[str] = None,
    stage: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the advancement_futures rec matching id (or team+stage), else None."""
    with open(bet_recs_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    recs = data.get("advancement_futures") or []
    if rec_id:
        for r in recs:
            if str(r.get("id")) == str(rec_id):
                return r
        return None
    # Fallback: explicit team/stage (+ optional selection) lookup.
    for r in recs:
        if team and str(r.get("team")).lower() != str(team).lower():
            continue
        if stage and str(r.get("stage")).lower() != str(stage).lower():
            continue
        if selection and str(r.get("selection")).lower() != str(selection).lower():
            continue
        return r
    return None


def _validate_rec(rec: Dict[str, Any]) -> Optional[str]:
    """Return a refusal reason string if the rec is not fireable, else None."""
    if str(rec.get("action_label")) != "ADD":
        return "rec action_label is %r, not 'ADD' — refusing" % rec.get("action_label")
    if rec.get("stale"):
        return "rec is stale (%s) — refusing" % (rec.get("stale_reason") or "stale")
    if str(rec.get("venue")) != "polymarket":
        return "rec venue is %r, not 'polymarket' — refusing" % rec.get("venue")
    if not rec.get("team") or not rec.get("stage"):
        return "rec missing team/stage — cannot re-resolve"
    return None


# ---------------------------------------------------------------------------
# Live PM re-resolution (never trust the stale stored pm_price)
# ---------------------------------------------------------------------------

def _resolve_live_market(
    rec: Dict[str, Any],
    *,
    find_markets: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Re-resolve the rec's team+stage to a LIVE Polymarket YES token + price.

    Reuses the exact advancement-pipeline machinery: ``find_world_cup_markets``
    for the event list, ``PM_STAGE_EVENTS`` to map an event title to a stage,
    ``_team_markets`` to find the team's market, and ``_yes_token_and_price`` to
    pull the CURRENT YES token id + best price + neg_risk + question + slug.

    ``find_markets`` is injectable for tests. Returns the resolution dict
    (``token_id``, ``price``, ``neg_risk``, ``market_question``, ``event_slug``,
    ``event_title``, ``market_title``) or None when no live market matches.
    """
    from wca import advancement as adv
    from wca.data.polymarket import _yes_token_and_price
    from wca.data.teamnames import canonical  # canonical team-name normaliser

    if find_markets is None:
        from wca.data.polymarket import find_world_cup_markets as find_markets

    want_stage = str(rec.get("stage"))
    want_team = canonical(str(rec.get("team")))

    events = find_markets() or []
    for event in events:
        title = str(event.get("title") or "").strip()
        stage = adv.PM_STAGE_EVENTS.get(title)
        if stage is None or stage != want_stage:
            continue
        for team, market in adv._team_markets(event):
            if team != want_team:
                continue
            resolved = _yes_token_and_price(market, event)
            if resolved is None:
                continue
            resolved = dict(resolved)
            resolved["market_title"] = title
            return resolved
    return None


def _price_within_band(live_price: float, rec_price: float, band: float) -> bool:
    """True if the live YES price is within ``band`` price-points of the rec."""
    try:
        return abs(float(live_price) - float(rec_price)) <= band + 1e-9
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Idempotency (pm_fire_log)
# ---------------------------------------------------------------------------

def _ensure_fire_log(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pm_fire_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id     TEXT NOT NULL,
            nonce      TEXT NOT NULL,
            ts_utc     TEXT NOT NULL,
            dry_run    INTEGER NOT NULL,
            usd        REAL,
            size       REAL,
            price      REAL,
            token_id   TEXT,
            order_id   TEXT,
            bid        INTEGER,
            ok         INTEGER NOT NULL,
            message    TEXT
        )
        """
    )
    # A given (rec_id, nonce) may be recorded at most once — the unique index is
    # the last line of defence against a retry with the same nonce.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_pm_fire_log_rec_nonce "
        "ON pm_fire_log (rec_id, nonce)"
    )
    conn.commit()


def _idempotency_block(
    conn: sqlite3.Connection,
    rec_id: str,
    nonce: str,
    *,
    window_min: int,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return a refusal reason if this fire would be a duplicate, else None.

    Blocks when:
      * the exact (rec_id, nonce) was already fired (retry / double-POST), OR
      * ANY successful fire of this rec_id happened within ``window_min``
        minutes (fast double-click with a fresh nonce).
    Only ``ok=1`` prior rows count as a real prior fire; a prior refusal does
    not lock the rec out.
    """
    row = conn.execute(
        "SELECT ts_utc, dry_run, order_id FROM pm_fire_log "
        "WHERE rec_id = ? AND nonce = ? AND ok = 1 LIMIT 1",
        (rec_id, nonce),
    ).fetchone()
    if row is not None:
        return "duplicate fire: rec %s + nonce already recorded (order_id=%s)" % (
            rec_id, row[2],
        )

    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_min * 60
    for ts_utc, _dry, order_id in conn.execute(
        "SELECT ts_utc, dry_run, order_id FROM pm_fire_log "
        "WHERE rec_id = ? AND ok = 1",
        (rec_id,),
    ).fetchall():
        try:
            t = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            ts = t.timestamp()
        except Exception:
            continue
        if ts >= cutoff:
            mins = int((now.timestamp() - ts) / 60)
            return (
                "rec %s already fired %dm ago (< %dm window; order_id=%s) — "
                "refusing double-fire" % (rec_id, mins, window_min, order_id)
            )
    return None


def _record_fire(
    conn: sqlite3.Connection,
    *,
    rec_id: str,
    nonce: str,
    ts_utc: str,
    dry_run: bool,
    usd: float,
    size: float,
    price: float,
    token_id: str,
    order_id: Optional[str],
    bid: Optional[int],
    ok: bool,
    message: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pm_fire_log "
        "(rec_id, nonce, ts_utc, dry_run, usd, size, price, token_id, "
        " order_id, bid, ok, message) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec_id, nonce, ts_utc, 1 if dry_run else 0, usd, size, price,
            token_id, order_id, bid, 1 if ok else 0, message,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Ledger read-back (order id / bid parsed from _execute_parked_order message)
# ---------------------------------------------------------------------------

def _parse_execute_message(msg: str) -> Tuple[Optional[str], Optional[int]]:
    """Best-effort pull of ``order id`` and ``ledger #`` from the bot message."""
    order_id: Optional[str] = None
    bid: Optional[int] = None
    for token in ("order id ", "order_id="):
        if token in msg:
            tail = msg.split(token, 1)[1].strip()
            order_id = tail.split()[0].strip(".,|") if tail else None
            break
    if "ledger #" in msg:
        tail = msg.split("ledger #", 1)[1].strip()
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            bid = int(digits)
    return order_id, bid


# ---------------------------------------------------------------------------
# Notion (best-effort; skip cleanly if no single-bet helper exists)
# ---------------------------------------------------------------------------

def _maybe_notion_sync(bid: Optional[int], db_path: str) -> Optional[str]:
    """Best-effort Notion sync of a just-booked live bet.

    The repo ships a *diff-based* reconcile (``wca.ledger.notion_diff``) but no
    single-row "append this bet to Notion" helper and no way to safely invent
    credentials here. Per spec we therefore SKIP and report why. Never raises.
    """
    return (
        "notion sync skipped — no single-bet append helper exists "
        "(only wca.ledger.notion_diff, a bulk diff/reconcile); run the diff "
        "reconcile separately if a Notion row is needed"
    )


# ---------------------------------------------------------------------------
# Core fire
# ---------------------------------------------------------------------------

def fire(
    *,
    rec: Dict[str, Any],
    rec_id: str,
    nonce: str,
    max_usd: float,
    db_path: str,
    now: Optional[datetime] = None,
    trader: Optional[Any] = None,
    find_markets: Optional[Any] = None,
    execute_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Validate, re-resolve, size, idempotency-guard, and place one PM order.

    ``trader`` / ``find_markets`` / ``execute_fn`` are injectable for tests. In
    production they default to the live ClobTrader path (via
    ``_execute_parked_order``), the live ``find_world_cup_markets``, and the
    real advancement resolution.
    """
    from wca.bot.app import _pm_dry_run

    if now is None:
        now = datetime.now(timezone.utc)
    ts_utc = now.strftime("%Y-%m-%dT%H:%M:%S")
    dry_run = _pm_dry_run()

    def _fail(message: str) -> Dict[str, Any]:
        return {
            "ok": False, "dry_run": dry_run, "order_id": None, "bid": None,
            "usd": 0.0, "size": 0.0, "price": None, "rec_id": rec_id,
            "message": message,
        }

    # (a) validate rec
    reason = _validate_rec(rec)
    if reason:
        return _fail(reason)

    # HARD cap: clamp to the lower of --max-usd and the absolute backstop.
    effective_cap = min(float(max_usd), ABSOLUTE_MAX_USD)
    if effective_cap <= 0:
        return _fail("effective USD cap <= 0 — refusing")

    # (b) re-resolve the LIVE market (current price, never stored pm_price)
    try:
        resolved = _resolve_live_market(rec, find_markets=find_markets)
    except Exception as exc:  # noqa: BLE001 — a resolution failure must refuse cleanly
        return _fail("live market re-resolution failed: %s" % exc)
    if resolved is None:
        return _fail(
            "no live Polymarket market for %s / %s — refusing (market may have "
            "resolved or delisted)" % (rec.get("team"), rec.get("stage"))
        )

    live_price = float(resolved["price"])
    rec_price = float(rec.get("pm_price") or 0.0)
    if not (0.0 < live_price < 1.0):
        return _fail("live price %.4f out of (0,1) — refusing" % live_price)
    if rec_price > 0 and not _price_within_band(live_price, rec_price, PRICE_SANITY_BAND):
        return _fail(
            "live price %.4f moved > %.2f from rec price %.4f — refusing (re-run "
            "bet_recs)" % (live_price, PRICE_SANITY_BAND, rec_price)
        )

    # (c) size with the hard cap
    usd = min(float(rec.get("stake") or 0.0), effective_cap)
    if usd <= 0:
        return _fail("sized USD <= 0 (rec stake %.2f, cap %.2f)" % (
            float(rec.get("stake") or 0.0), effective_cap))
    size_shares = usd / live_price

    # (d) idempotency guard (+ table bootstrap) — do this on the SAME connection
    # we will record on so the read and the guard see a consistent view.
    conn = sqlite3.connect(db_path)
    try:
        _ensure_fire_log(conn)
        blocked = _idempotency_block(
            conn, rec_id, nonce, window_min=IDEMPOTENCY_WINDOW_MIN, now=now
        )
        if blocked:
            conn.close()
            return _fail(blocked)
    except Exception as exc:  # noqa: BLE001 — cannot verify idempotency -> refuse
        conn.close()
        return _fail("idempotency check failed (%s) — refusing" % exc)

    # (e) build the proposal + call the SAME signed-order/ledger path as the bot.
    proposal = {
        "token_id": resolved["token_id"],
        "price": live_price,
        "size": round(size_shares, 6),          # SHARES, not USD
        "side": "BUY",
        "neg_risk": bool(resolved.get("neg_risk", False)),
        "order_type": "GTC",
        "market_question": resolved.get("market_question") or resolved.get("market_title") or "",
        "event_slug": resolved.get("event_slug") or "",
        "label": resolved.get("market_title") or rec.get("stage") or "advancement",
        "outcome": rec.get("selection") or "Yes",
        "market": rec.get("market") or "advancement",
        "model_prob": rec.get("model_prob"),
        "ev": rec.get("ev_net"),
        "match_desc": "%s %s (advancement)" % (rec.get("team"), rec.get("stage")),
    }

    if execute_fn is None:
        from wca.bot.app import _execute_parked_order as execute_fn

    try:
        msg = execute_fn(0, proposal, db_path, ts_utc=ts_utc, trader=trader)
    except Exception as exc:  # noqa: BLE001 — never leave a half-state unreported
        try:
            _record_fire(
                conn, rec_id=rec_id, nonce=nonce, ts_utc=ts_utc, dry_run=dry_run,
                usd=usd, size=size_shares, price=live_price,
                token_id=str(resolved["token_id"]), order_id=None, bid=None,
                ok=False, message="execute raised: %s" % exc,
            )
        finally:
            conn.close()
        return _fail("order execute raised: %s" % exc)

    order_id, bid = _parse_execute_message(str(msg))
    # A real refusal from the bot path (trader unavailable, key missing, order
    # failed) never carries a ledger id; treat those as failures.
    lowered = str(msg).lower()
    ok = not any(
        k in lowered for k in (
            "unavailable", "not set", "could not init", "order failed",
            "unconfirmed", "ledger write failed",
        )
    )

    notion_note = None
    if ok and not dry_run:
        notion_note = _maybe_notion_sync(bid, db_path)

    try:
        _record_fire(
            conn, rec_id=rec_id, nonce=nonce, ts_utc=ts_utc, dry_run=dry_run,
            usd=usd, size=size_shares, price=live_price,
            token_id=str(resolved["token_id"]), order_id=order_id, bid=bid,
            ok=ok, message=str(msg),
        )
    finally:
        conn.close()

    return {
        "ok": bool(ok),
        "dry_run": dry_run,
        "order_id": order_id,
        "bid": bid,
        "usd": round(usd, 2),
        "size": round(size_shares, 6),
        "price": round(live_price, 6),
        "rec_id": rec_id,
        "message": str(msg),
        "notion": notion_note,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rec-id", default=None, help="rec id from bet_recs.json")
    ap.add_argument("--team", default=None)
    ap.add_argument("--selection", default=None)
    ap.add_argument("--stage", default=None)
    ap.add_argument(
        "--max-usd", type=float, default=100.0,
        help="hard USD cap on the fire (clamped to the absolute %.0f backstop)"
        % ABSOLUTE_MAX_USD,
    )
    ap.add_argument("--db", default="data/wca.db")
    ap.add_argument("--bet-recs", default="site/bet_recs.json")
    ap.add_argument(
        "--nonce", default=None,
        help="per-click idempotency token; auto-generated if omitted",
    )
    args = ap.parse_args(argv)

    nonce = args.nonce or ("auto-%d" % int(time.time() * 1000))
    rec = _load_rec(
        args.bet_recs, args.rec_id,
        team=args.team, selection=args.selection, stage=args.stage,
    )
    rec_id = args.rec_id or (rec.get("id") if rec else None) or "unknown"

    if rec is None:
        out = {
            "ok": False, "dry_run": None, "order_id": None, "bid": None,
            "usd": 0.0, "size": 0.0, "price": None, "rec_id": rec_id,
            "message": "rec not found in %s (id=%r team=%r stage=%r)"
            % (args.bet_recs, args.rec_id, args.team, args.stage),
        }
        print(json.dumps(out))
        return 1

    result = fire(
        rec=rec,
        rec_id=str(rec.get("id") or rec_id),
        nonce=nonce,
        max_usd=args.max_usd,
        db_path=args.db,
    )
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
