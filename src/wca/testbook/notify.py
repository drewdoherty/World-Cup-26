"""Telegram pings for paper test-book activity → the @worldcupdevbot chat.

Paper-trading is experimental/dev activity, so it pings the DEV bot
(@worldcupdevbot / the conductor), NOT the real-money @gamble1_bot. Best-effort:
a missing token/chat or a send failure is a silent no-op — it must never break a
trade cycle.

The messages are written to be *acted on manually on the A1 Polymarket account*:
every placement is a BUY instruction (market · outcome · ask price · fair · edge ·
size in $ and shares) and every exit says exactly what was sold, at what price, the
realised P&L, and WHY. A P&L chart is sent alongside via :func:`send_photo`.

Credentials (set on the mini, e.g. in the launchd plist or .env.conductor):
* ``WCA_TESTBOOK_BOT_TOKEN`` — the @worldcupdevbot BotFather token
  (falls back to ``TELEGRAM_BOT_TOKEN``).
* ``WCA_TESTBOOK_CHAT_ID``   — the chat id to post into
  (falls back to ``TELEGRAM_CHAT_ID``).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence


def _cents(p) -> str:
    try:
        return "%.0f¢" % (float(p) * 100)
    except (TypeError, ValueError):
        return "?"


def _pct(x) -> str:
    try:
        return "%+.0f%%" % (100.0 * float(x))
    except (TypeError, ValueError):
        return "?"


def _shares(stake, price) -> float:
    try:
        p = float(price)
        return (float(stake) / p) if p > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _money(ccy: str, amt) -> str:
    try:
        return "%s%s" % (ccy, "{:,.0f}".format(float(amt)))
    except (TypeError, ValueError):
        return "%s?" % ccy


def live_sizing(q, p, bankroll: float, *, kelly_frac: float = 0.25,
                max_frac: float = 0.0) -> Dict[str, object]:
    """Binary-YES fractional-Kelly stake on the LIVE bankroll.

    Thin wrapper over the project sizing rule (:func:`wca.markets.bankroll.
    size_placement`) so the paper pings and the live sizing can never diverge.
    ``max_frac > 0`` caps the fraction; ``0`` = uncapped. Returns
    ``{stake, frac, f_star, capped}``.
    """
    from wca.markets.bankroll import size_placement
    return size_placement(q, p, bankroll, kelly_frac=kelly_frac, max_frac=max_frac)


def _context(basis, fixture, selection) -> str:
    """`basis · fixture` context, dropping blanks and anything already in the selection."""
    sel = str(selection or "").lower()
    bits = [str(x).strip() for x in (basis, fixture) if x and str(x).strip()]
    bits = [b for b in bits if b.lower() not in sel]
    return " · ".join(bits)


def _book_line(report: Optional[Dict[str, object]]) -> Optional[str]:
    if report is None:
        return None
    return "equity $%.0f (ROI %+.1f%%) · %d open · $%.0f cash" % (
        report.get("equity", 0), report.get("roi_pct", 0),
        report.get("n_open", 0), report.get("realized_balance", 0))


def format_activity(pass_result: Dict[str, object], report: Optional[Dict[str, object]] = None,
                    *, max_lines: int = 8, live_bankroll: Optional[float] = None,
                    kelly_frac: float = 0.25, max_frac: float = 0.0, currency: str = "$",
                    hot_frac: float = 0.10, book_scale: float = 1.0) -> Optional[str]:
    """Render a paper trade-pass summary for Telegram, or None if nothing happened.

    Each placement renders as a BUY instruction ready to mirror manually on the A1
    Polymarket account. When ``live_bankroll`` is given, each line carries a
    fractional-Kelly stake (``kelly_frac``·Kelly of the bankroll, ¼-Kelly by
    default) with the % of bankroll; stakes above ``hot_frac`` are flagged ⚠ and
    ``max_frac`` optionally caps them.
    """
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
    book = _book_line(report)
    if book:
        lines.append(book)
    klabel = "¼-Kelly" if abs(kelly_frac - 0.25) < 1e-9 else ("%g×Kelly" % kelly_frac)
    scaled = live_bankroll is not None and book_scale < 0.999
    if placed:
        if live_bankroll is not None:
            hdr = "▶ *BUY on A1 Polymarket* — %s on %s bankroll" % (
                klabel, _money(currency, live_bankroll))
            hdr += (" (book-scaled ×%.2f to fit cap):" % book_scale) if scaled else ":"
            lines.append(hdr)
        else:
            lines.append("▶ *BUY on A1 Polymarket:*")
    for p in placed[:max_lines]:
        sel = str(p.get("selection") or "")
        ctx = _context(p.get("basis"), p.get("fixture"), sel)
        price = p.get("price")
        lines.append("\U0001F7E2 *BUY* %s%s" % (sel, ("  ·  " + ctx) if ctx else ""))
        if live_bankroll is not None:
            lines.append("    @ %s ask · fair %s · edge %s" % (
                _cents(price), _cents(p.get("model")), _pct(p.get("edge", 0))))
            s = live_sizing(p.get("model"), price, live_bankroll,
                            kelly_frac=kelly_frac, max_frac=max_frac)
            stake = float(s["stake"]) * book_scale
            frac = float(s["frac"]) * book_scale
            base_tag = "capped" if s["capped"] else ("⚠ hot" if s["frac"] >= hot_frac else klabel)
            tag = (base_tag + " · book-scaled") if scaled else base_tag
            lines.append("    *stake %s* · %.1f%% of bankroll (%s)" % (
                _money(currency, stake), 100.0 * frac, tag))
        else:
            stake = float(p.get("stake", 0) or 0)
            lines.append("    @ %s ask · fair %s · edge %s · $%.0f (%.0f sh)" % (
                _cents(price), _cents(p.get("model")), _pct(p.get("edge", 0)),
                stake, _shares(stake, price)))
    if n > max_lines:
        lines.append("  …and %d more (see `report`)" % (n - max_lines))
    if live_bankroll is not None and placed:
        caps = []
        if max_frac:
            caps.append("%.0f%%/bet" % (100 * max_frac))
        caps.append("75% whole-book")
        lines.append("_%s (%s) of a £3,000±realised bankroll at $1.33=£1 (USD)._"
                     % (klabel, ", ".join(caps)))
    return "\n".join(lines)


# Human-readable "why we exited" per trader exit rule (see trader.eval_exit_rules).
_EXIT_WHY = {
    "edge_flip_close": "model edge gone — fair fell to/below the market bid",
    "liquidity_exit": "liquidity/spread blowout — exit-cost emergency",
    "over_kelly_trim": "position above ¼-Kelly target — trimmed back to size",
}


def format_exits(actions: Sequence[Dict[str, object]],
                 report: Optional[Dict[str, object]] = None) -> Optional[str]:
    """Render trim/close activity as explicit mirror-on-A1 instructions, or None.

    Each ``action`` dict carries: ``action`` ('close'|'trim'), ``rule``,
    ``selection``, ``fixture``, ``basis``, ``entry_price``, ``exit_price`` (the
    bid we sold into), ``shares_sold``, ``realized_pl``, ``stake_after``,
    ``q`` (model fair) and optionally ``spread``.
    """
    if not actions:
        return None
    lines = ["\U0001F9EA *Test book* — %d exit action(s)" % len(actions),
             "▶ *Mirror on A1 Polymarket:*"]
    for a in actions:
        is_close = a.get("action") == "close"
        verb, emoji = ("EXIT (full)", "\U0001F534") if is_close else ("TRIM", "\U0001F7E0")
        sel = str(a.get("selection") or ("#%s" % a.get("id")))
        ctx = _context(a.get("market") or a.get("basis"), a.get("fixture"), sel)
        tag = ("  ·  " + ctx) if ctx else ""
        lines.append("%s *%s* — %s%s  _(#%s)_" % (emoji, verb, sel, tag, a.get("id")))
        detail = "    SELL %.0f sh @ %s (entry %s) · realised $%+.2f" % (
            float(a.get("shares_sold") or 0), _cents(a.get("exit_price")),
            _cents(a.get("entry_price")), float(a.get("realized_pl") or 0.0))
        if not is_close and a.get("stake_after"):
            detail += " · kept $%.0f open" % float(a["stake_after"])
        lines.append(detail)
        why = _EXIT_WHY.get(a.get("rule"), str(a.get("rule") or "rule fired"))
        if a.get("rule") == "edge_flip_close" and a.get("q") is not None:
            why += " (fair %s ≤ bid %s)" % (_cents(a.get("q")), _cents(a.get("exit_price")))
        elif a.get("rule") == "liquidity_exit" and a.get("spread") is not None:
            why += " (spread %s)" % _cents(a.get("spread"))
        lines.append("    why: %s" % why)
    book = _book_line(report)
    if book:
        lines.append(book)
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


def chart_caption(report: Optional[Dict[str, object]]) -> str:
    """One-line caption for the equity-curve photo."""
    if report is None:
        return "\U0001F9EA Test book — equity"
    return ("\U0001F9EA Test book equity $%.0f (ROI %+.1f%%) · realised $%+.0f · "
            "MTM $%+.0f · %d open" % (
                report.get("equity", 0), report.get("roi_pct", 0),
                report.get("realized_pl", 0), report.get("unrealized_pl", 0),
                report.get("n_open", 0)))


def _creds(token, chat_id):
    tok = token or os.environ.get("WCA_TESTBOOK_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("WCA_TESTBOOK_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    return tok, chat


def send(text: Optional[str], *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Best-effort Telegram send to the dev chat. Never raises; returns success."""
    if not text:
        return False
    tok, chat = _creds(token, chat_id)
    if not tok or not chat:
        return False
    try:
        from wca.bot.telegram import TelegramClient
        TelegramClient(token=tok).send_message(chat, text)
        return True
    except Exception:
        return False


def send_photo(png: Optional[bytes], caption: Optional[str] = None, *,
               token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Best-effort Telegram photo send (the equity/P&L chart). Never raises."""
    if not png:
        return False
    tok, chat = _creds(token, chat_id)
    if not tok or not chat:
        return False
    try:
        from wca.bot.telegram import TelegramClient
        TelegramClient(token=tok).send_photo(chat, png, filename="testbook_pnl.png", caption=caption)
        return True
    except Exception:
        return False
