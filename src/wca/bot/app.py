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
    "/summary — portfolio P&L, ROI, CLV, bankroll\n"
    "/clv — closing-line-value report\n"
    "/card — today's recommended bet card\n"
    "/structure — project structure metrics\n"
    "/ping — liveness check\n"
    "/help — this message\n\n"
    "\U0001F4F8 Send a betslip *screenshot* and I'll parse the selections, then "
    "log them to the ledger once you reply `yes`.\n"
    "Confirm a pushed bet with `Y BET-<id>`, decline with `N BET-<id>`."
)


def _authorized(chat_id: int | str, allowed: Optional[str]) -> bool:
    """Only the configured chat may drive the bot. Empty config = lock out."""
    if not allowed:
        return False
    return str(chat_id) == str(allowed)


# ---------------------------------------------------------------------------
# Command handlers — each returns the reply text.
# ---------------------------------------------------------------------------


def handle_summary(db_path: str) -> str:
    s = reports.summary(db_path=db_path)

    def pct(v: float) -> str:
        return "N/A" if v != v else "%.2f%%" % (v * 100)

    return (
        "*Portfolio summary*\n"
        "Bets: %d (open %d / won %d / lost %d / void %d)\n"
        "Staked: %.2f   P&L: %.2f   ROI: %s\n"
        "Avg CLV: %s   Beat close: %s\n"
        "Bankroll: %.2f (deposited %.2f)"
        % (
            s["total_bets"], s["open_bets"], s["won_bets"], s["lost_bets"], s["void_bets"],
            s["total_staked"], s["total_pl"], pct(s["roi"]),
            pct(s["avg_clv"]), pct(s["pct_beat_close"]),
            s["current_bankroll"], s["total_deposited"],
        )
    )


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


def handle_confirmation(text: str, db_path: str) -> Optional[str]:
    """Route `Y BET-<id>` / `N BET-<id>` replies. Returns None if not a confirm."""
    parts = text.strip().split()
    if len(parts) != 2:
        return None
    verb, token = parts[0].upper(), parts[1].upper()
    if verb not in {"Y", "N"} or not token.startswith("BET-"):
        return None
    # Stake placement against the ledger is wired with the card generator.
    action = "confirmed" if verb == "Y" else "declined"
    return "Bet %s %s. (Ledger write pending card-generator wiring.)" % (token, action)


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
    if cmd == "/clv":
        return handle_clv(db_path)
    if cmd == "/card":
        return handle_card(db_path)
    if cmd == "/structure":
        return handle_structure()
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
