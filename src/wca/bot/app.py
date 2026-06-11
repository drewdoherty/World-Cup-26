"""World Cup Alpha management bot — long-polling command loop.

The bot exposes the ledger read-only reports over Telegram, serves the cached
matchday card, and ingests betslip screenshots into the ledger. It is
intentionally simple: one process, one authorized chat, synchronous
long-polling. Heavy work (model refits, odds pulls, card builds) runs elsewhere
on cron and only *pushes* / caches results for the bot to read.

Commands
--------
``/start``        register + show help
``/help``         show command list
``/summary``      portfolio summary (P&L, ROI, CLV, bankroll)
``/clv``          closing-line-value report
``/card``         today's recommended bet card (read from cache)
``/structure``    latest project-structure metrics snapshot
``/ping``         liveness check

Screenshot ingestion
--------------------
Send a betslip photo and the bot extracts every selection via Claude vision,
replies with what it parsed, and waits for a ``yes`` / ``no`` confirmation
before writing anything to the ledger. This keeps an OCR misread from silently
poisoning the CLV / calibration data.

Confirmation flow: a pushed recommendation carries a token like ``BET-12``;
replying ``Y BET-12`` / ``N BET-12`` confirms or declines.
"""

from __future__ import annotations

import glob
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from wca import cardcache
from wca.bot.telegram import TelegramClient, TelegramError
from wca.ledger import reports
from wca.ledger.store import record_bet

CARD_PATH = "data/card_latest.md"
CARD_MAX_AGE_HOURS = 6.0

HELP_TEXT = (
    "*World Cup Alpha* — manager console\n\n"
    "/summary — portfolio P&L, ROI, CLV, bankroll by pool\n"
    "/bets — open bets, stakes, max win / max loss by venue\n"
    "/clv — closing-line-value report\n"
    "/card — today's recommended bet card\n"
    "/scores — predicted FT scorelines per fixture\n"
    "/structure — project structure metrics\n"
    "/pm — Polymarket parked orders + trader status\n"
    "/ping — liveness check\n"
    "/help — this message\n\n"
    "\U0001F4F8 Send a betslip *screenshot* and I'll parse the selections, then "
    "log them to the ledger once you reply `yes`.\n"
    "Confirm a pushed bet with `Y BET-<id>`, decline with `N BET-<id>`.\n"
    "Execute a parked Polymarket order with `Y PM-<n>`, discard with `N PM-<n>`."
)


def _authorized(chat_id: int | str, allowed: Optional[str]) -> bool:
    """Only the configured chat may drive the bot. Empty config = lock out."""
    if not allowed:
        return False
    return str(chat_id) == str(allowed)


# ---------------------------------------------------------------------------
# Command handlers — each returns the reply text.
# ---------------------------------------------------------------------------


def _venue_of(platform: str) -> str:
    p = (platform or "").lower()
    if "polymarket" in p:
        return "polymarket"
    if "kalshi" in p:
        return "kalshi"
    return "sportsbook"


_VENUE_SYMBOL = {"sportsbook": "£", "polymarket": "$", "kalshi": "$"}


def _pool_rows(db_path: str) -> Dict[str, Dict[str, float]]:
    """Per-venue money picture from the ledger (deposits tagged in reason)."""
    import sqlite3

    pools: Dict[str, Dict[str, float]] = {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT platform, status, stake, settled_pl, decimal_odds, notes FROM bets"):
            v = _venue_of(r["platform"])
            d = pools.setdefault(v, {"open": 0.0, "settled_pl": 0.0, "deposited": 0.0, "n": 0})
            d["n"] += 1
            if r["status"] == "open":
                d["open"] += float(r["stake"] or 0.0)
            elif r["status"] in ("won", "lost"):
                d["settled_pl"] += float(r["settled_pl"] or 0.0)
        for e in con.execute("SELECT amount, reason FROM bankroll_events"):
            reason = (e["reason"] or "").lower()
            for v in ("polymarket", "kalshi", "sportsbook"):
                if "pool=" + v in reason:
                    pools.setdefault(v, {"open": 0.0, "settled_pl": 0.0, "deposited": 0.0, "n": 0})
                    pools[v]["deposited"] += float(e["amount"] or 0.0)
                    break
    except Exception:
        pass
    finally:
        con.close()
    return pools


def handle_summary(db_path: str) -> str:
    s = reports.summary(db_path=db_path)

    def pct(v: float) -> str:
        return "N/A" if v != v else "%.2f%%" % (v * 100)

    pools = _pool_rows(db_path)

    # Build a compact code-block table for the pool rows.
    pool_table_rows = []
    for v in ("sportsbook", "polymarket", "kalshi"):
        if v not in pools:
            continue
        d = pools[v]
        sym = _VENUE_SYMBOL[v]
        bank = d["deposited"] + d["settled_pl"]
        at_risk = d["open"]
        pl = d["settled_pl"]
        pool_table_rows.append(
            "%-12s %s%9.2f  %s%8.2f  %s%+8.2f"
            % (v, sym, bank, sym, at_risk, sym, pl)
        )

    lines = [
        "\U0001f4b0 *World Cup Alpha — portfolio*",
        "Bets: %d (open %d / won %d / lost %d / void %d)"
        % (s["total_bets"], s["open_bets"], s["won_bets"], s["lost_bets"], s["void_bets"]),
        "At risk (open): %.2f   Settled staked: %.2f   P&L: %.2f   ROI: %s"
        % (s.get("open_staked", 0.0), s["total_staked"], s["total_pl"], pct(s["roi"])),
        "Avg CLV: %s   Beat close: %s" % (pct(s["avg_clv"]), pct(s["pct_beat_close"])),
        "",
        "*Bankroll by pool*",
    ]

    if pool_table_rows:
        lines.append("```")
        lines.append("%-12s %10s  %9s  %9s" % ("POOL", "BANK", "AT RISK", "P&L"))
        lines.extend(pool_table_rows)
        lines.append("```")

    for v in ("sportsbook", "polymarket", "kalshi"):
        if v not in pools:
            continue
        d = pools[v]
        sym = _VENUE_SYMBOL[v]
        bank = d["deposited"] + d["settled_pl"]
        lines.append(
            "%s: %s%.2f (deposited %s%.2f, P&L %s%+.2f, at risk %s%.2f)"
            % (v, sym, bank, sym, d["deposited"], sym, d["settled_pl"], sym, d["open"])
        )
    if not pools:
        lines.append("(no pools yet — record deposits with `bankroll add`)")
    return "\n".join(lines)


def handle_bets(db_path: str) -> str:
    """All open bets — odds, stake per bet grouped by venue, max win / max loss.

    Free bets (notes containing "FREE") risk no cash: they count toward max
    win but contribute zero to max loss.
    """
    import sqlite3

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, match_desc, selection, platform, decimal_odds, stake, notes "
            "FROM bets WHERE status = 'open' ORDER BY platform, id"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return "*Open bets*\nNone — the book is flat."

    by_venue: Dict[str, List[Any]] = {}
    for r in rows:
        by_venue.setdefault(_venue_of(r["platform"]), []).append(r)

    lines = ["\U0001f3af *Open bets* (%d)" % len(rows)]
    grand_win = {}
    grand_loss = {}
    for venue in ("sportsbook", "polymarket", "kalshi"):
        if venue not in by_venue:
            continue
        sym = _VENUE_SYMBOL[venue]
        v_win = v_loss = 0.0
        lines.append("")
        lines.append("*%s*" % venue.upper())
        # Code-block table header: ID | match (20ch) | sel (12ch) | odds | stake | to-win
        lines.append("```")
        lines.append("%-4s %-20s %-12s %6s %8s %8s" % ("ID", "Match", "Sel", "Odds", "Stake", "To-win"))
        for r in by_venue[venue]:
            stake = float(r["stake"] or 0.0)
            odds = float(r["decimal_odds"] or 0.0)
            is_free = "free" in (r["notes"] or "").lower()
            win = stake * (odds - 1.0)
            loss = 0.0 if is_free else stake
            v_win += win
            v_loss += loss
            match_trunc = (r["match_desc"] or "")[:20]
            sel_trunc = (r["selection"] or "")[:12]
            free_marker = "(free)" if is_free else ""
            stake_str = "%s%.2f%s" % (sym, stake, " " + free_marker if free_marker else "")
            lines.append(
                "%-4s %-20s %-12s %6.2f %-14s %s%.2f"
                % ("#%d" % r["id"], match_trunc, sel_trunc, odds, stake_str, sym, win)
            )
        lines.append("```")
        lines.append("max win %s%.2f / max loss %s%.2f" % (sym, v_win, sym, v_loss))
        grand_win[sym] = grand_win.get(sym, 0.0) + v_win
        grand_loss[sym] = grand_loss.get(sym, 0.0) + v_loss

    tot_win = " + ".join("%s%.2f" % (s, a) for s, a in grand_win.items())
    tot_loss = " + ".join("%s%.2f" % (s, a) for s, a in grand_loss.items())
    lines.append("")
    lines.append("*TOTAL*  max win %s / max loss %s" % (tot_win, tot_loss))
    return "\n".join(lines)


def handle_clv(db_path: str) -> str:
    d = reports.clv_report(db_path=db_path)

    def pct(v: float) -> str:
        return "N/A" if v != v else "%.2f%%" % (v * 100)

    return (
        "*CLV report*\n"
        "Bets with closing odds: %d\n"
        "Average CLV: %s\n"
        "Beat close: %s"
        % (d["n_bets"], pct(d["avg_clv"]), pct(d["pct_beat_close"]))
    )


def render_card(recs, pools, score_cards=None) -> str:
    """Render the bet card (+ optional scoreline section) as one Telegram message.

    Thin adapter so the bot loop and any cron pusher format cards identically.
    ``recs`` / ``pools`` are the outputs/config of :func:`wca.card.build_card`;
    ``score_cards`` (if given) is the list from :func:`wca.card.build_score_cards`,
    appended below the bets via :func:`wca.card.format_scores`.
    """
    from wca.card import format_card, format_scores

    parts = [format_card(recs, pools)]
    if score_cards:
        parts.append("")
        parts.append(format_scores(score_cards))
    return "\n".join(parts)


def handle_card(
    db_path: str,
    card_path: str = CARD_PATH,
    now_utc: Optional[str] = None,
) -> str:
    """Serve the most recent cached card written by ``scripts/wca_build_card.py``.

    The card generator (model blend -> EV -> Kelly per pool, plus the reconciled
    scoreline section) is too slow to run inline on every Telegram poll, so it
    runs on cron and caches its formatted output. This handler reads that cache
    and flags staleness; if no cache exists yet it says so honestly.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cached = cardcache.read_card(
        card_path, now_utc=now_utc, max_age_hours=CARD_MAX_AGE_HOURS
    )
    if cached is None:
        return (
            "*Today's card*\n"
            "No card cached yet. The cron build (`scripts/wca_build_card.py`) "
            "fits the models, pulls live odds and writes the card here."
        )
    header = "*Today's card*"
    if cached.get("generated"):
        header += " — generated %s UTC" % cached["generated"]
        if cached.get("stale"):
            header += "  ⚠️ STALE"
    body = cached.get("text") or "(empty card)"
    return header + "\n\n" + body


def handle_scores(
    card_path: str = CARD_PATH,
    now_utc: Optional[str] = None,
) -> str:
    """Return predicted full-time scorelines per fixture from the cached card.

    Reads the card written by ``scripts/wca_build_card.py``, parses the
    scorelines section via :func:`wca.sitedata.parse_scorelines`, and formats
    a compact Telegram message.  If no card is cached yet returns an honest
    message telling the user the cron build has not run.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    cached = cardcache.read_card(card_path, now_utc=now_utc, max_age_hours=CARD_MAX_AGE_HOURS)
    if cached is None:
        return (
            "*Predicted scores*\n"
            "No card cached yet. The cron build (`scripts/wca_build_card.py`) "
            "has not run — try again after the next scheduled build."
        )

    from wca.sitedata import parse_scorelines

    card_text = cached.get("text") or ""
    generated = cached.get("generated") or ""
    fixtures = parse_scorelines(card_text)

    if not fixtures:
        return (
            "*Predicted scores*\n"
            "No scorelines section found in the current card. "
            "The build may not have included scoreline predictions."
        )

    header = "⚽ *Predicted scores* — %s" % generated if generated else "⚽ *Predicted scores*"

    lines = [header, ""]
    for fx in fixtures:
        scores = fx.get("scores") or []
        if not scores:
            continue
        # Top score (most likely).
        top = scores[0]
        top_str = "*%s* (%s%%)" % (top["score"], _fmt_prob(top["prob"]))
        # Up to 4 runner-ups (indices 1-4) — kept inline to satisfy existing assertions.
        runners = scores[1:5]
        runner_strs = [
            "%s %s%%" % (s["score"], _fmt_prob(s["prob"])) for s in runners
        ]
        fixture_line = "*%s*: %s" % (fx["fixture"], top_str)
        if runner_strs:
            fixture_line += "  | " + " | ".join(runner_strs)
        lines.append(fixture_line)

        # O/U + BTTS dimmed line (indented).
        ou = fx.get("over_under")
        btts = fx.get("btts")
        if ou is not None:
            ou_str = "O/U %.1f: over %s%% / under %s%%" % (
                ou.get("line") or 2.5,
                _fmt_prob(ou.get("over")),
                _fmt_prob(ou.get("under")),
            )
            if btts is not None:
                ou_str += "   BTTS %s%%" % _fmt_prob(btts)
            lines.append("    " + ou_str)
        # Blank line between fixtures.
        lines.append("")

    # Remove trailing blank line if present.
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def _fmt_prob(prob: Optional[float]) -> str:
    """Format a probability as a compact percentage string (1 d.p.)."""
    if prob is None:
        return "?"
    return "%.1f" % prob


def handle_structure(docs_dir: Optional[str] = None) -> str:
    """Latest project-structure metrics from docs/architecture/structure_*.md.

    Sends only the metrics table + complexity index (the Mermaid chart is
    useless in Telegram).
    """
    if docs_dir is None:
        # src/wca/bot/app.py -> repo root is four levels up.
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        docs_dir = os.path.join(root, "docs", "architecture")

    snapshots = sorted(glob.glob(os.path.join(docs_dir, "structure_*.md")))
    if not snapshots:
        return (
            "*Project structure*\n"
            "No structure snapshot found. Run `scripts/wca_structure.py` first."
        )

    latest = snapshots[-1]
    date = os.path.basename(latest)[len("structure_"):-len(".md")]
    with open(latest, "r", encoding="utf-8") as fh:
        content = fh.read()

    # Keep only the metrics section (table + complexity index line).
    marker = "## Metrics"
    idx = content.find(marker)
    metrics_part = content[idx + len(marker):].strip() if idx >= 0 else content.strip()

    return "*Project structure* (%s)\n\n%s" % (date, metrics_part)


# ---------------------------------------------------------------------------
# Betslip-screenshot ingestion.
# ---------------------------------------------------------------------------

# Per-chat parked extractions awaiting a yes/no confirmation. Kept in-process:
# the bot is single-instance and a pending slip that is never confirmed simply
# expires when the process restarts.
_PENDING_PHOTO_BETS: Dict[Any, List[Any]] = {}


def _slug(text: str) -> str:
    """Compact, deterministic match id fragment from a free-text description."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_").upper()
    return s[:48] or "UNKNOWN"


def _format_extracted(bets: List[Any]) -> str:
    """Human-readable confirmation prompt for parsed selections."""
    from wca.bot.vision import currency_symbol

    lines = ["*Parsed %d selection(s) from your slip:*" % len(bets)]
    for i, b in enumerate(bets, 1):
        sym = currency_symbol(getattr(b, "currency", None))
        odds = ("%.2f" % b.decimal_odds) if b.decimal_odds else "?"
        stake = ("%s%.2f" % (sym, b.stake)) if b.stake is not None else sym + "?"
        book = b.bookmaker or "?"
        flag = "  ⚡boost" if getattr(b, "is_boost", False) else ""
        warn = "" if getattr(b, "confidence", 1.0) >= 0.6 else "  ⚠️low-conf"
        lines.append(
            "%d. %s — *%s* @ %s | stake %s | %s%s%s"
            % (i, b.match_desc, b.selection, odds, stake, book, flag, warn)
        )
    lines.append("\nReply *yes* to log all to the ledger, *no* to discard.")
    return "\n".join(lines)


def handle_photo(
    image_bytes: bytes,
    chat_id: Any,
    pending: Optional[Dict[Any, List[Any]]] = None,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Extract bets from a betslip image and park them for confirmation."""
    if pending is None:
        pending = _PENDING_PHOTO_BETS
    from wca.bot.vision import extract_bets_from_image, VisionError

    try:
        bets = extract_bets_from_image(image_bytes, api_key=api_key, model=model)
    except VisionError as exc:
        return "Couldn't read that slip: %s" % exc
    except Exception as exc:  # never crash the loop on a vision hiccup
        return "Vision error: %s" % exc
    if not bets:
        return "No bets detected. Send a clearer screenshot of the full slip."
    pending[chat_id] = bets
    return _format_extracted(bets)


def handle_photo_confirmation(
    text: str,
    chat_id: Any,
    db_path: str,
    pending: Optional[Dict[Any, List[Any]]] = None,
    *,
    ts_utc: Optional[str] = None,
) -> Optional[str]:
    """Resolve a parked betslip on a lone ``yes`` / ``no``. None if not applicable."""
    if pending is None:
        pending = _PENDING_PHOTO_BETS
    if chat_id not in pending:
        return None
    ans = text.strip().lower()
    if ans not in {"yes", "y", "no", "n"}:
        return None  # leave the slip parked; let normal command routing proceed
    bets = pending.pop(chat_id)
    if ans in {"no", "n"}:
        return "Discarded %d parsed selection(s)." % len(bets)

    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    logged: List[str] = []
    for b in bets:
        match_id = "MANUAL_" + _slug(b.match_desc)
        note = "screenshot ingest; currency=%s; conf %.2f%s" % (
            getattr(b, "currency", None) or "GBP",
            getattr(b, "confidence", 0.0),
            "; boost" if getattr(b, "is_boost", False) else "",
        )
        try:
            bid = record_bet(
                ts_utc,
                match_id,
                b.match_desc,
                b.market or "unknown",
                b.selection,
                b.bookmaker or "unknown",
                float(b.decimal_odds or 0.0),
                float(b.stake or 0.0),
                notes=note,
                db_path=db_path,
            )
            logged.append("#%d %s @ %s" % (bid, b.selection, b.decimal_odds or "?"))
        except Exception as exc:  # report per-bet failure, keep going
            logged.append("ERR %s: %s" % (b.selection, exc))
    return "Logged %d to the ledger:\n%s" % (len(logged), "\n".join(logged))


# ---------------------------------------------------------------------------
# Polymarket parked-order confirmation gate.
#
# A proposed Polymarket trade is *parked* (never auto-executed): the bot pushes
# a one-line summary with a token like ``PM-1`` and waits for an explicit
# ``Y PM-1`` (execute) / ``N PM-1`` (discard).  This is the same human-in-the-
# loop pattern as betslip screenshots and pushed ``BET-<id>`` recommendations,
# applied to live order placement so no order ever fires without a reply.
# ---------------------------------------------------------------------------

# Module-level registry of parked orders awaiting confirmation.  Keyed by the
# integer token suffix; value is the proposal dict.  In-process only: an
# unconfirmed order simply evaporates on restart (fail-safe — never auto-fires).
_PENDING_ORDERS: Dict[int, Dict[str, Any]] = {}
_PM_SEQ = {"n": 0}


def park_order(proposal: Dict[str, Any]) -> str:
    """Park a proposed Polymarket order and return its ``PM-<n>`` token.

    ``proposal`` must carry at least ``token_id``, ``price``, ``size`` and
    ``side`` (BUY/SELL); ``label`` and ``outcome`` are used for the human
    summary, ``neg_risk`` flows through to signing.  The caller pushes the
    returned text to the user.

    Example
    -------
    >>> tok, text = park_order({"label": "Mexico", "outcome": "Yes",
    ...                         "side": "BUY", "price": 0.69, "size": 31.88,
    ...                         "token_id": "123"})  # doctest: +SKIP
    """
    _PM_SEQ["n"] += 1
    n = _PM_SEQ["n"]
    _PENDING_ORDERS[n] = dict(proposal)
    return "PM-%d" % n


def format_parked_order(token: str, proposal: Dict[str, Any]) -> str:
    """Human confirmation prompt for one parked Polymarket order."""
    side = str(proposal.get("side", "BUY")).upper()
    price = float(proposal.get("price", 0.0))
    size = float(proposal.get("size", 0.0))
    notional = price * size
    label = proposal.get("label") or proposal.get("market") or "market"
    outcome = proposal.get("outcome") or proposal.get("selection") or ""
    sel = ("%s %s" % (label, outcome)).strip()
    return (
        "place $%.2f %s %s @ %.2f? "
        "Reply `Y %s` to execute, `N %s` to discard."
        % (notional, sel, side, price, token, token)
    )


def push_parked_order(proposal: Dict[str, Any]) -> str:
    """Park a proposal and return the user-facing confirmation message."""
    token = park_order(proposal)
    return format_parked_order(token, _PENDING_ORDERS[int(token.split("-")[1])])


def _pm_dry_run() -> bool:
    """Polymarket dry-run flag from env (default ON for safety)."""
    return os.environ.get("PM_DRY_RUN", "1").strip().lower() not in {"0", "false", "no", ""}


def _execute_parked_order(
    n: int,
    proposal: Dict[str, Any],
    db_path: str,
    *,
    ts_utc: Optional[str] = None,
    trader: Optional[Any] = None,
) -> str:
    """Sign + (maybe) submit a parked order, then record it to the ledger.

    The trader is imported lazily (and may be injected for tests).  Honours the
    ``PM_DRY_RUN`` env flag: in dry-run the order is signed but not POSTed.  The
    ledger row is tagged ``platform='polymarket'`` with the order id / dry-run
    flag in its notes so the CLV pipeline and ``/summary`` pools pick it up.
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    dry_run = _pm_dry_run()

    if trader is None:
        try:
            from wca.pm.trader import ClobTrader
        except Exception as exc:
            return "PM-%d: trader unavailable (%s). Order not placed." % (n, exc)
        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            return (
                "PM-%d: POLYMARKET_PRIVATE_KEY not set — cannot place. "
                "Add it to .env (see scripts/wca_pm_probe.py)." % n
            )
        funder = os.environ.get("POLYMARKET_FUNDER") or None
        st = os.environ.get("POLYMARKET_SIG_TYPE")
        sig_type = int(st) if st not in (None, "") else None
        try:
            trader = ClobTrader(key, funder=funder, signature_type=sig_type)
        except Exception as exc:
            return "PM-%d: could not init trader (%s)." % (n, exc)

    price = float(proposal.get("price", 0.0))
    size = float(proposal.get("size", 0.0))
    side = str(proposal.get("side", "BUY")).upper()
    try:
        result = trader.place_order(
            proposal["token_id"],
            price,
            size,
            side,
            neg_risk=bool(proposal.get("neg_risk", False)),
            dry_run=dry_run,
        )
    except Exception as exc:
        return "PM-%d: order failed — %s" % (n, exc)

    order_id = (result or {}).get("orderID") or (result or {}).get("orderId") or "dry-run"
    label = proposal.get("label") or proposal.get("market") or "market"
    outcome = proposal.get("outcome") or proposal.get("selection") or ""
    match_desc = proposal.get("match_desc") or ("%s %s" % (label, outcome)).strip()
    match_id = proposal.get("match_id") or ("PM_" + _slug(match_desc))
    decimal_odds = (1.0 / price) if price > 0 else 0.0
    notes = "polymarket order; token=%s; side=%s; %s; order_id=%s" % (
        proposal["token_id"],
        side,
        "DRY-RUN (not submitted)" if dry_run else "LIVE",
        order_id,
    )
    try:
        bid = record_bet(
            ts_utc,
            match_id,
            match_desc,
            proposal.get("market") or "polymarket",
            outcome or label,
            "polymarket",
            decimal_odds,
            round(price * size, 2),
            notes=notes,
            db_path=db_path,
        )
    except Exception as exc:
        return "PM-%d: order ok but ledger write failed — %s" % (n, exc)

    mode = "DRY-RUN (signed, not submitted)" if dry_run else "LIVE — submitted"
    return (
        "Order PM-%d %s.\n$%.2f %s %s @ %.2f | order id %s | ledger #%d"
        % (n, mode, price * size, side, (outcome or label), price, order_id, bid)
    )


def handle_confirmation(
    text: str,
    db_path: str,
    *,
    pending_orders: Optional[Dict[int, Dict[str, Any]]] = None,
    ts_utc: Optional[str] = None,
    trader: Optional[Any] = None,
) -> Optional[str]:
    """Route `Y/N BET-<id>` and `Y/N PM-<n>` replies. None if not a confirm.

    ``BET-<id>`` keeps its existing acknowledgement behaviour.  ``PM-<n>``
    executes (Y) or discards (N) a parked Polymarket order via
    :func:`_execute_parked_order`.  ``pending_orders`` / ``trader`` / ``ts_utc``
    are injectable for tests; production uses the module-level registry.
    """
    if pending_orders is None:
        pending_orders = _PENDING_ORDERS
    parts = text.strip().split()
    if len(parts) != 2:
        return None
    verb, token = parts[0].upper(), parts[1].upper()
    if verb not in {"Y", "N"}:
        return None

    if token.startswith("BET-"):
        # Stake placement against the ledger is wired with the card generator.
        action = "confirmed" if verb == "Y" else "declined"
        return "Bet %s %s. (Ledger write pending card-generator wiring.)" % (token, action)

    if token.startswith("PM-"):
        try:
            n = int(token[len("PM-"):])
        except ValueError:
            return None
        proposal = pending_orders.pop(n, None)
        if proposal is None:
            return "PM-%d is not a parked order (expired or already handled)." % n
        if verb == "N":
            label = proposal.get("label") or proposal.get("market") or "order"
            return "Discarded parked order PM-%d (%s)." % (n, label)
        return _execute_parked_order(
            n, proposal, db_path, ts_utc=ts_utc, trader=trader
        )

    return None


def handle_pm(db_path: str) -> str:
    """`/pm` — parked Polymarket orders + trader status (configured? dry-run?).

    Shows the in-process parked-order queue, whether a private key is
    configured, the dry-run flag, and today's Polymarket spend if a
    ``pm_order_log`` table exists in the ledger.
    """
    lines = ["\U0001f4c8 *Polymarket*"]

    configured = bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
    dry = _pm_dry_run()
    lines.append(
        "Trader: %s | mode: %s"
        % ("configured" if configured else "NOT configured", "DRY-RUN" if dry else "LIVE")
    )
    funder = os.environ.get("POLYMARKET_FUNDER")
    if funder:
        st = os.environ.get("POLYMARKET_SIG_TYPE", "?")
        lines.append("Funder: `%s` (sig type %s)" % (funder, st))

    spend = _pm_daily_spend(db_path)
    if spend is not None:
        lines.append("Spend today: $%.2f" % spend)

    if _PENDING_ORDERS:
        lines.append("")
        lines.append("*Parked orders*")
        for n in sorted(_PENDING_ORDERS):
            lines.append("  " + format_parked_order("PM-%d" % n, _PENDING_ORDERS[n]))
    else:
        lines.append("")
        lines.append("No parked orders.")
    return "\n".join(lines)


def _pm_daily_spend(db_path: str, *, day_utc: Optional[str] = None) -> Optional[float]:
    """Today's Polymarket spend from a ``pm_order_log`` table, or None if absent.

    Tolerant of a missing table / column (the order log is optional): returns
    ``None`` so ``/pm`` simply omits the line rather than erroring.
    """
    import sqlite3

    if day_utc is None:
        day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "SELECT COALESCE(SUM(notional), 0.0) FROM pm_order_log "
            "WHERE substr(ts_utc, 1, 10) = ?",
            (day_utc,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except sqlite3.OperationalError:
        return None  # table not created yet
    except Exception:
        return None
    finally:
        con.close()


def dispatch(text: str, db_path: str) -> str:
    """Map an incoming text message to a reply."""
    confirm = handle_confirmation(text, db_path)
    if confirm is not None:
        return confirm

    cmd = text.strip().split()[0].lower() if text.strip() else ""
    # Strip @botname suffix Telegram appends in group chats.
    cmd = cmd.split("@")[0]

    if cmd in {"/start", "/help"}:
        return HELP_TEXT
    if cmd == "/summary":
        return handle_summary(db_path)
    if cmd == "/bets":
        return handle_bets(db_path)
    if cmd == "/clv":
        return handle_clv(db_path)
    if cmd == "/card":
        return handle_card(db_path)
    if cmd == "/scores":
        return handle_scores(card_path=CARD_PATH)
    if cmd == "/structure":
        return handle_structure()
    if cmd == "/pm":
        return handle_pm(db_path)
    if cmd == "/ping":
        return "pong"
    return "Unknown command. Send /help for the list."


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------


def run(
    db_path: str = "data/wca.db",
    token: Optional[str] = None,
    allowed_chat_id: Optional[str] = None,
    poll_timeout: int = 25,
) -> None:
    """Long-poll Telegram and serve commands until interrupted."""
    client = TelegramClient(token=token)
    allowed = allowed_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not allowed:
        print("WARNING: TELEGRAM_CHAT_ID unset — all messages will be rejected.")

    print("World Cup Alpha bot started. Polling...")
    offset: Optional[int] = None
    while True:
        try:
            updates = client.get_updates(offset=offset, poll_timeout=poll_timeout)
        except TelegramError as exc:
            print("poll error: %s (retry in 5s)" % exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue
            chat_id = message["chat"]["id"]

            if not _authorized(chat_id, allowed):
                # Reply once so an unknown chat learns its own id (for setup),
                # but serve no data.
                try:
                    client.send_message(
                        chat_id,
                        "Unauthorized. This chat id is `%s`." % chat_id,
                    )
                except TelegramError:
                    pass
                continue

            # 1) Betslip screenshot -> parse + park for confirmation.
            if "photo" in message:
                try:
                    image = client.download_photo(message)
                    reply = (
                        handle_photo(image, chat_id)
                        if image
                        else "No photo found in that message."
                    )
                except TelegramError as exc:
                    reply = "Couldn't download the image: %s" % exc
                except Exception as exc:
                    reply = "Error reading image: %s" % exc
                try:
                    client.send_message(chat_id, reply)
                except TelegramError as exc:
                    print("send error: %s" % exc)
                continue

            if "text" not in message:
                continue
            text = message["text"]

            # 2) Pending betslip confirmation (lone yes/no) takes priority.
            try:
                reply = handle_photo_confirmation(text, chat_id, db_path)
            except Exception as exc:
                reply = "Error logging bets: %s" % exc
            if reply is None:
                try:
                    reply = dispatch(text, db_path)
                except Exception as exc:  # never let one bad command kill the loop
                    reply = "Error handling command: %s" % exc

            try:
                client.send_message(chat_id, reply)
            except TelegramError as exc:
                print("send error: %s" % exc)
