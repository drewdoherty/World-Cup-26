"""Telegram pings for paper test-book activity → the @worldcupdevbot chat.

Paper-trading is experimental/dev activity, so it pings the DEV bot
(@worldcupdevbot / the conductor), NOT the real-money @gamble1_bot. Best-effort:
a missing token/chat or a send failure is a silent no-op — it must never break a
trade cycle.

Credentials (set on the mini, e.g. in the launchd plist or .env.conductor):
* ``WCA_TESTBOOK_BOT_TOKEN`` — the @worldcupdevbot BotFather token
  (falls back to ``TELEGRAM_BOT_TOKEN``).
* ``WCA_TESTBOOK_CHAT_ID``   — the chat id to post into
  (falls back to ``TELEGRAM_CHAT_ID``).
"""

from __future__ import annotations

import os
from typing import Dict, Optional


def _cents(p) -> str:
    try:
        return "%.0f¢" % (float(p) * 100)
    except (TypeError, ValueError):
        return "?"


def format_activity(pass_result: Dict[str, object], report: Optional[Dict[str, object]] = None,
                    *, max_lines: int = 8) -> Optional[str]:
    """Render a paper trade-pass summary for Telegram, or None if nothing happened."""
    placed = pass_result.get("placed") or []
    n = pass_result.get("n_placed", len(placed))
    if not n and not pass_result.get("suspicious"):
        return None  # quiet pass — don't ping
    head = "\U0001F9EA *Test book* — paper pass placed *%d* (%d candidates" % (
        n, pass_result.get("candidates", 0))
    if pass_result.get("suspicious"):
        head += ", %d suspicious skipped" % pass_result["suspicious"]
    head += ")"
    lines = [head]
    if report is not None:
        lines.append("equity $%.0f (ROI %+.1f%%) · %d open · $%.0f cash" % (
            report.get("equity", 0), report.get("roi_pct", 0),
            report.get("n_open", 0), report.get("realized_balance", 0)))
    for p in placed[:max_lines]:
        lines.append("  +[%s] %s @ %s · model %s · edge %+.0f%% · $%.0f" % (
            p.get("basis"), str(p.get("selection"))[:32], _cents(p.get("price")),
            _cents(p.get("model")), 100 * float(p.get("edge", 0)), float(p.get("stake", 0))))
    if n > max_lines:
        lines.append("  …and %d more" % (n - max_lines))
    return "\n".join(lines)


def format_settlement(summary: Dict[str, object], report: Optional[Dict[str, object]] = None) -> Optional[str]:
    """Render a settlement summary, or None if nothing settled."""
    s = summary.get("settled") or {}
    n = sum(int(v) for v in s.values())
    if not n:
        return None
    msg = "\U0001F9EA *Test book* — settled %dW/%dL/%dV · P&L $%+.2f" % (
        s.get("won", 0), s.get("lost", 0), s.get("void", 0), summary.get("pl", 0.0))
    if report is not None:
        msg += "\nequity $%.0f (ROI %+.1f%%)" % (report.get("equity", 0), report.get("roi_pct", 0))
    return msg


def send(text: Optional[str], *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Best-effort Telegram send to the dev chat. Never raises; returns success."""
    if not text:
        return False
    tok = token or os.environ.get("WCA_TESTBOOK_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("WCA_TESTBOOK_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        from wca.bot.telegram import TelegramClient
        TelegramClient(token=tok).send_message(chat, text)
        return True
    except Exception:
        return False
