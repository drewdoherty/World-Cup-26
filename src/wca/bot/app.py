"""World Cup Alpha management bot — long-polling command loop.

The bot exposes the ledger read-only reports over Telegram and provides the
human-confirmation gate for staking. It is intentionally simple: one process,
one authorized chat, synchronous long-polling. Heavy work (model refits, odds
pulls) runs elsewhere on cron and only *pushes* results here.

Commands
--------
``/start``        register + show help
``/help``         show command list
``/summary``      portfolio summary (P&L, ROI, CLV, bankroll)
``/clv``          closing-line-value report
``/card``         today's recommended bet card (placeholder until wired)
``/structure``    latest project-structure metrics snapshot
``/ping``         liveness check

Confirmation flow (future): when a recommendation is pushed it carries a token
like ``BET-12``; replying ``Y BET-12`` / ``N BET-12`` confirms or declines.
This module already routes such replies to :func:`handle_confirmation`.
"""

from __future__ import annotations

import glob
import os
import time
from typing import Any, Dict, Optional

from wca.bot.telegram import TelegramClient, TelegramError
from wca.ledger import reports

HELP_TEXT = (
    "*World Cup Alpha* — manager console\n\n"
    "/summary — portfolio P&L, ROI, CLV, bankroll\n"
    "/clv — closing-line-value report\n"
    "/card — today's recommended bet card\n"
    "/structure — project structure metrics\n"
    "/ping — liveness check\n"
    "/help — this message\n\n"
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


def handle_card(db_path: str) -> str:
    # Placeholder: the matchday card generator (blend model -> EV -> Kelly per
    # pool) runs on cron and *pushes* the formatted card here. Until that push is
    # wired, the bot reports its absence honestly. When wired, the cron job calls
    # render_card(recs, pools, score_cards) and sends the result, so the same
    # message carries both the +EV bets and the reconciled scoreline section
    # (top correct scores, O/U 2.5, BTTS) per fixture.
    return (
        "*Today's card*\n"
        "Card generator not wired into the bot loop yet. The cron build emits "
        "blended model probs vs de-vigged best price per match, EV, "
        "quarter-Kelly stakes per pool (Polymarket / Kalshi / sportsbook), plus "
        "a reconciled scoreline section (top correct scores, O/U 2.5, BTTS)."
    )


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
    """Map an incoming message to a reply."""
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
            if not message or "text" not in message:
                continue
            chat_id = message["chat"]["id"]
            text = message["text"]

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

            try:
                reply = dispatch(text, db_path)
            except Exception as exc:  # never let one bad command kill the loop
                reply = "Error handling command: %s" % exc
            try:
                client.send_message(chat_id, reply)
            except TelegramError as exc:
                print("send error: %s" % exc)
