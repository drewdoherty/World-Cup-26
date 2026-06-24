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
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from wca import cardcache
from wca.bot.telegram import (
    TelegramClient,
    TelegramError,
    image_document_file_id,
)
from wca.ledger import reports
from wca.ledger.store import record_bet, settle_cashout

logger = logging.getLogger(__name__)

CARD_PATH = "data/card_latest.md"
NEXT_PATH = "data/next_latest.md"
GOALSCORERS_PATH = "data/goalscorers_latest.md"
SCORES_FEED_PATH = "site/scores_data.json"
CARD_MAX_AGE_HOURS = 6.0
# The odds feed behind /accas and /boost should refresh frequently; warn beyond
# this. Structure snapshots change rarely, so its window is generous.
SCORES_FEED_MAX_AGE_HOURS = 6.0
STRUCTURE_MAX_AGE_HOURS = 24.0 * 30.0


def _normalize_ts(ts: Optional[str]) -> Optional[str]:
    """Normalise 'YYYY-MM-DD HH:MM:SS UTC' (feed style) or ISO to ISO-T form."""
    if not ts:
        return ts
    t = ts.strip()
    if t.endswith(" UTC"):
        t = t[:-4].strip()
    if " " in t and "T" not in t:
        t = t.replace(" ", "T", 1)
    return t


def _staleness_age_hours(generated: Optional[str], now_utc: Optional[str]):
    """Hours between *generated* and *now_utc* (ISO/feed strings); None if N/A."""
    t_gen = cardcache._parse_iso(_normalize_ts(generated) or "")
    t_now = cardcache._parse_iso(_normalize_ts(now_utc) or "")
    if t_gen is None or t_now is None:
        return None
    return (t_now - t_gen) / 3600.0


def _stale_banner(generated: Optional[str], now_utc: Optional[str],
                  max_age_hours: float, label: str = "data") -> str:
    """A prominent staleness banner, or '' when fresh / age is unknown.

    Used so that *every* cache-backed command makes staleness impossible to
    miss — the bot never silently serves data that is older than its window.
    """
    age = _staleness_age_hours(generated, now_utc)
    if age is not None and age > max_age_hours:
        return (
            "⚠️ *STALE %s* — generated %s (%.1fh ago; the scheduled build may be "
            "lagging, treat with caution)\n\n" % (label, generated, age)
        )
    return ""


def _feed_generated(path: str) -> Optional[str]:
    """Read ``meta.generated`` from a JSON feed; None if absent/unreadable."""
    try:
        import json

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return (data.get("meta") or {}).get("generated")
    except Exception:
        return None
    return None

# Telegram slash-command menu (registered with setMyCommands at startup).
# /restart and /structure are intentionally omitted: restart is admin-only
# and confusing to expose publicly; structure changes rarely.
_TELEGRAM_COMMANDS = [
    {"command": "summary",     "description": "Portfolio P&L, ROI, CLV, bankroll by pool"},
    {"command": "bets",        "description": "Open bets, stakes, max win / max loss by venue"},
    {"command": "clv",         "description": "Closing-line-value report"},
    {"command": "card",        "description": "Today's recommended bet card"},
    {"command": "next",        "description": "Next match preview: winner, corners, scorers"},
    {"command": "goalscorers", "description": "Anytime + first-goalscorer recs, next 5 games"},
    {"command": "scores",      "description": "Predicted FT scorelines per fixture"},
    {"command": "accas",       "description": "4+ leg accumulators, next 5 matches"},
    {"command": "pm",          "description": "Polymarket parked orders + trader status"},
    {"command": "settle",      "description": "Settle a bet: /settle <id> <outcome> [odds]"},
    {"command": "boost",       "description": "Price a bookmaker boost vs the model"},
    {"command": "ping",        "description": "Liveness check"},
    {"command": "help",        "description": "Command list"},
]

HELP_TEXT = (
    "*World Cup Alpha* — manager console\n\n"
    "/summary — portfolio P&L, ROI, CLV, bankroll by pool\n"
    "/bets — open bets, stakes, max win / max loss by venue\n"
    "/clv — closing-line-value report\n"
    "/card — today's recommended bet card\n"
    "/next — next match preview: winner, corners, scorers, scorelines\n"
    "/goalscorers — anytime + first-goalscorer recs, next 5 games\n"
    "/scores — predicted FT scorelines per fixture\n"
    "/accas — 4+ leg accumulators (next 5 matches, min 2.0 odds per leg)\n"
    "/structure — project structure metrics\n"
    "/pm — Polymarket parked orders + trader status\n"
    "/settle — settle a bet (usage: `/settle <bet-id> <outcome> [closing-odds]`)\n"
    "/boost — price a bookmaker price-boost vs the model (usage below)\n"
    "/restart — restart the bot (admin; `/restart pull` redeploys first)\n"
    "/ping — liveness check\n"
    "/help — this message\n\n"
    "\U0001F4F8 Send a betslip *screenshot* and I'll parse the selections, then "
    "log them to the ledger once you reply `yes`.\n"
    "Tag the photo caption (or yes-reply) to set provenance: `a2` (account 2), "
    "`offer`, `punt`, `model` — default is account 1 / model.\n"
    "⚡ Caption a screenshot with `boost` and I'll read the enhanced price "
    "and tell you if it beats the model's fair odds (no ledger write).\n"
    "⚡ Or type it: "
    "`/boost <site> | <match> | <market> | <selection> | <odds> [was <odds>] [inplay]`\n"
    "e.g. `/boost bet365 | Brazil vs Morocco | Match Result | Brazil | 2.5 was 1.8`\n"
    "Confirm a pushed bet with `Y BET-<id>`, decline with `N BET-<id>`.\n"
    "Execute a parked Polymarket order with `Y PM-<n>`, discard with `N PM-<n>`.\n"
    "Unfilled PM orders auto-redeem after 24h; `REDEEM ALL` or `REDEEM <order-id>` "
    "cancels them now and frees the pUSD."
)


def _authorized(chat_id: int | str, allowed: Optional[str]) -> bool:
    """Only configured chats may drive the bot. Empty config = lock out.

    ``allowed`` is a comma-separated list of chat ids (TELEGRAM_CHAT_ID), so a
    private chat and a group chat can both be authorized, e.g.
    ``TELEGRAM_CHAT_ID=12345678,-1001234567890``. Group ids are negative.
    """
    if not allowed:
        return False
    allowed_ids = {part.strip() for part in str(allowed).split(",") if part.strip()}
    return str(chat_id) in allowed_ids


READ_ONLY_MSG = (
    "🔒 Read-only: bets and order confirmations are admin-only in this chat. "
    "You can use /next, /goalscorers, /scores, /card, /summary, /bets, /clv, /ping."
)

# Lone yes/no (betslip confirm) or Y/N BET-<id> / PM-<n> (order confirm) —
# anything that can write the ledger or execute an order.
# ``yes``/``no`` (optionally trailed by account/source tag overrides such as
# ``yes a2 offer``) confirm a parked betslip; ``Y/N BET-<id>`` / ``Y/N PM-<n>``
# confirm a pushed bet or parked order. All of these can move money / write the
# ledger, so they are admin-gated.
_TAG_TOKEN = r"(?:account\s*[12]|acc[12]|a[12]|[12]|model|offer|punt)"
_MONEY_RE = re.compile(
    r"^\s*(?:"
    r"[yn]\s+(?:bet|pm)-\d+"            # Y/N BET-<id> | Y/N PM-<n>
    r"|(?:yes|no|y|n)(?:\s+" + _TAG_TOKEN + r")*"  # yes/no [+ tag overrides]
    r"|redeem\s+\S+"                   # REDEEM ALL | REDEEM <order_id>
    r")\s*$",
    re.IGNORECASE,
)


def _is_money_action(text: str) -> bool:
    return bool(_MONEY_RE.match(text or ""))


def _is_admin(user_id: str, admin: Optional[str]) -> bool:
    """True when the sender may perform money-touching actions.

    With TELEGRAM_ADMIN_USER_ID unset, everyone in an authorized chat is
    treated as admin (single-user setups, original behaviour). Once set, only
    that user id can confirm orders or log bets.
    """
    if not admin:
        return True
    return str(user_id) == str(admin)


# ---------------------------------------------------------------------------
# /restart — privileged, side-effecting. Handled in run() (not dispatch()) so
# it can authenticate the *sender* and reply BEFORE the process exits, exactly
# like the yes/no order confirms. See _supervised() for the restart mechanism.
# ---------------------------------------------------------------------------

RESTART_DENIED_MSG = (
    "🔒 /restart is admin-only. Set TELEGRAM_ADMIN_USER_ID and send it from that "
    "account (or from the single authorized chat)."
)


def _is_restart_command(text: str) -> bool:
    """True for ``/restart`` (optionally ``/restart pull``), @botname tolerated."""
    if not text or not text.strip():
        return False
    head = text.strip().split()[0].split("@")[0].lower()
    return head == "/restart"


def _supervised() -> bool:
    """True when a supervisor will respawn us after a clean exit.

    On the prod Mac mini the bot runs under **launchd with ``KeepAlive=true``**
    (label ``com.wca.bot``, see deploy/macmini/install.sh), so a clean
    ``sys.exit(0)`` IS the restart — launchd relaunches within ThrottleInterval
    (~30s). When NOT supervised we must re-exec ourselves instead.

    Detection order (first hit wins):
      1. ``WCA_RESTART_MODE`` env — set it in the plist to be unambiguous:
         ``supervised``/``exit`` ⇒ True, ``unsupervised``/``exec`` ⇒ False;
      2. heuristic: launchd/systemd reparent a managed daemon to PID 1, so a
         parent pid of 1 means "supervised".
    """
    mode = os.environ.get("WCA_RESTART_MODE", "").strip().lower()
    if mode in ("supervised", "exit", "launchd", "keepalive", "systemd"):
        return True
    if mode in ("unsupervised", "exec", "none", "self"):
        return False
    try:
        return os.getppid() == 1
    except OSError:
        return False


def perform_restart() -> None:
    """Restart this process; never returns on success.

    Supervised → exit cleanly so the supervisor respawns us. Unsupervised →
    re-exec in place with the original interpreter + argv. Raising ``OSError``
    (exec failed) is left to the caller to turn into an honest reply.
    """
    if _supervised():
        sys.exit(0)  # launchd KeepAlive relaunches within ThrottleInterval
        return  # defensive: sys.exit raises in prod; guard the fall-through
    os.execv(sys.executable, [sys.executable, *sys.argv])  # self re-exec


def _git_pull(repo_root: str = ".") -> "tuple[bool, str]":
    """`git pull --ff-only`; returns (ok, trimmed_output). Never raises."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_root, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out = "\n".join(p for p in ((res.stdout or "").strip(), (res.stderr or "").strip()) if p)
        return res.returncode == 0, out[-500:]
    except Exception as exc:  # noqa: BLE001 — a pull hiccup must not crash the bot
        return False, str(exc)


def handle_restart(text: str, *, is_admin: bool, repo_root: str = ".") -> "tuple[str, bool]":
    """Authenticate + (optionally) pull. Returns ``(reply, should_restart)``.

    ``should_restart`` is False for a denied caller or a failed ``/restart
    pull`` — the caller sends the reply and only restarts when it is True.
    """
    if not is_admin:
        return RESTART_DENIED_MSG, False
    parts = text.strip().split()
    prefix = ""
    if len(parts) > 1 and parts[1].lower() == "pull":
        ok, out = _git_pull(repo_root)
        tail = ("\n%s" % out) if out else ""
        if not ok:
            return "⚠️ `git pull` failed — NOT restarting." + tail, False
        prefix = "⬇️ Pulled latest." + tail + "\n"
    how = "supervisor will respawn (~30s)" if _supervised() else "self re-exec"
    return prefix + "♻️ Restarting… (%s)" % how, True


# ---------------------------------------------------------------------------
# Command handlers — each returns the reply text.
# ---------------------------------------------------------------------------


from wca.venues import canon_platform

# Backward-compat alias: existing call sites/tests rely on the private name.
_canon_platform = canon_platform


_VENUE_SYMBOL = {"sportsbook": "£", "polymarket": "$", "polymarket-auto": "$", "kalshi": "$"}


def _venue_of(platform: Optional[str]) -> str:
    """Map a raw platform string to its canonical venue (currency pool).

    Restored 2026-06-18: the "normalise platform names" commit removed this
    helper but left call sites in ``_pool_rows`` and ``handle_bets``, crashing
    ``/bets`` and ``/summary`` with a NameError. Substring match is robust to
    however platform names are normalised (case/spacing).
    """
    p = (platform or "").lower()
    if "polymarket" in p:
        return "polymarket"
    if "kalshi" in p:
        return "kalshi"
    return "sportsbook"


def _pool_rows(db_path: str) -> Dict[str, Dict[str, float]]:
    """Per-venue money picture from the ledger (deposits tagged in reason)."""
    import sqlite3

    pools: Dict[str, Dict[str, float]] = {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT platform, status, stake, settled_pl, decimal_odds, notes, account FROM bets"):
            v = _venue_of(r["platform"])
            # Polymarket runs two physical accounts: acct 1 = manual (MetaMask),
            # acct 2 = the automated bot (World-Cup-26 deposit wallet, Mac mini).
            # Split them so auto vs manual P&L is legible.
            if v == "polymarket":
                v = "polymarket-auto" if str(r["account"]) == "2" else "polymarket"
            d = pools.setdefault(
                v, {"open": 0.0, "settled_pl": 0.0, "settled_staked": 0.0, "deposited": 0.0, "n": 0}
            )
            d["n"] += 1
            if r["status"] == "open":
                d["open"] += float(r["stake"] or 0.0)
            elif r["status"] in ("won", "lost", "cashed"):
                # cashed = Polymarket cash-out; realised P&L like won/lost.
                d["settled_pl"] += float(r["settled_pl"] or 0.0)
                d["settled_staked"] += float(r["stake"] or 0.0)
        for e in con.execute("SELECT amount, reason FROM bankroll_events"):
            reason = (e["reason"] or "").lower()
            # Longest tag first: "pool=polymarket" is a substring of
            # "pool=polymarket-auto", so the sub-pool must be checked first.
            for v in ("polymarket-auto", "polymarket", "kalshi", "sportsbook"):
                if "pool=" + v in reason:
                    pools.setdefault(
                        v, {"open": 0.0, "settled_pl": 0.0, "settled_staked": 0.0, "deposited": 0.0, "n": 0}
                    )
                    pools[v]["deposited"] += float(e["amount"] or 0.0)
                    break
        # The auto wallet is funded out of the manual polymarket pool. Until a
        # transfer is ledgered as pool=polymarket-auto, impute the minimum
        # transfer that covers its open stakes and losses, so the sub-pool's
        # bank never goes negative and the two rows still sum to the truth.
        auto = pools.get("polymarket-auto")
        parent = pools.get("polymarket")
        if auto is not None and parent is not None:
            transfer = auto["open"] - (auto["deposited"] + auto["settled_pl"])
            if transfer > 0:
                auto["deposited"] += transfer
                parent["deposited"] -= transfer
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
    for v in ("sportsbook", "polymarket", "polymarket-auto", "kalshi"):
        if v not in pools:
            continue
        d = pools[v]
        sym = _VENUE_SYMBOL[v]
        bank = d["deposited"] + d["settled_pl"]
        at_risk = d["open"]
        pl = d["settled_pl"]
        pool_table_rows.append(
            "%-15s %s%9.2f  %s%8.2f  %s%+8.2f"
            % (v, sym, bank, sym, at_risk, sym, pl)
        )

    # Never sum across currencies: aggregate per symbol (£ pools vs $ pools).
    by_ccy: Dict[str, Dict[str, float]] = {}
    for v, d in pools.items():
        sym = _VENUE_SYMBOL.get(v, "$")
        c = by_ccy.setdefault(sym, {"open": 0.0, "settled_staked": 0.0, "settled_pl": 0.0})
        c["open"] += d["open"]
        c["settled_staked"] += d.get("settled_staked", 0.0)
        c["settled_pl"] += d["settled_pl"]

    def per_ccy(fmt: str, key: str) -> str:
        return " / ".join(
            fmt % (sym, by_ccy[sym][key]) for sym in ("£", "$") if sym in by_ccy
        ) or "0.00"

    def roi_per_ccy() -> str:
        parts = []
        for sym in ("£", "$"):
            if sym not in by_ccy:
                continue
            staked = by_ccy[sym]["settled_staked"]
            r = (by_ccy[sym]["settled_pl"] / staked) if staked else float("nan")
            parts.append("%s %s" % (sym, pct(r)))
        return " / ".join(parts) or "N/A"

    lines = [
        "\U0001f4b0 *World Cup Alpha — portfolio*",
        "Bets: %d (open %d / won %d / lost %d / void %d)"
        % (s["total_bets"], s["open_bets"], s["won_bets"], s["lost_bets"], s["void_bets"]),
        "At risk (open): %s" % per_ccy("%s%.2f", "open"),
        "Settled staked: %s   P&L: %s" % (per_ccy("%s%.2f", "settled_staked"), per_ccy("%s%+.2f", "settled_pl")),
        "ROI: %s   Avg CLV: %s   Beat close: %s"
        % (roi_per_ccy(), pct(s["avg_clv"]), pct(s["pct_beat_close"])),
        "",
        "*Bankroll by pool*",
    ]

    if pool_table_rows:
        lines.append("```")
        lines.append("%-15s %10s  %9s  %9s" % ("POOL", "BANK", "AT RISK", "P&L"))
        lines.extend(pool_table_rows)
        lines.append("```")

    for v in ("sportsbook", "polymarket", "polymarket-auto", "kalshi"):
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
        cols = {r[1] for r in con.execute("PRAGMA table_info(bets)")}
        acc_sel = "account" if "account" in cols else "'1' AS account"
        src_sel = "source" if "source" in cols else "'model' AS source"
        rows = con.execute(
            "SELECT id, match_desc, selection, platform, decimal_odds, stake, notes, "
            "%s, %s FROM bets WHERE status = 'open' ORDER BY platform, id"
            % (acc_sel, src_sel)
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
    for venue in ("sportsbook", "polymarket", "polymarket-auto", "kalshi"):
        if venue not in by_venue:
            continue
        sym = _VENUE_SYMBOL[venue]
        v_win = v_loss = 0.0
        lines.append("")
        lines.append("*%s*" % venue.upper())
        # Two lines per bet, max ~34 chars wide, so the code block survives a
        # phone screen without wrapping into soup:
        #   #12 S Korea v Czech Republic
        #       BTTS - No    1.95 £8→£7.60
        lines.append("```")
        first = True
        for r in by_venue[venue]:
            stake = float(r["stake"] or 0.0)
            odds = float(r["decimal_odds"] or 0.0)
            # Free-stake convention: notes BEGIN with "FREE" or contain "SNR"
            # (stake-not-returned). A loose substring match falsely flagged
            # real-money bets whose notes merely mention a free-bet promo.
            note_l = (r["notes"] or "").lower()
            is_free = note_l.startswith("free") or "snr" in note_l
            win = stake * (odds - 1.0)
            loss = 0.0 if is_free else stake
            v_win += win
            v_loss += loss
            # Compact provenance tag: source initial (m/o/p) glued to the id,
            # plus an "A2" marker for second-account bets. Stays inside the
            # ~34-char phone width, e.g. "#17p A2 Canada v Bosnia".
            src_tag = (r["source"] or "model")[:1].lower()
            a2 = " A2" if str(r["account"]) == "2" else ""
            head = "#%d%s%s " % (r["id"], src_tag, a2)
            budget = 34 - len(head)
            match = (r["match_desc"] or "").replace(" vs ", " v ")
            if len(match) > budget:
                match = match[: budget - 1] + "…"
            sel = (r["selection"] or "")
            if len(sel) > 12:
                sel = sel[:11] + "…"
            if not first:
                lines.append("")
            first = False
            lines.append("%s%s" % (head, match))
            lines.append("     %-12s %5.2f %s%g%s→%s%.2f" % (
                sel, odds, sym, round(stake, 2),
                "(free)" if is_free else "", sym, win))
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

    Rule 1 (provenance): every reply leads with source path + generation
    timestamp + fetch time.  No generation timestamp -> NO BET.
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
    generated = cached.get("generated")
    if not generated:
        return (
            "*Today's card*\n"
            "NO BET — `%s` has no generation timestamp; "
            "data age is unknown." % card_path
        )
    body = cached.get("text") or "(empty card)"
    banner = _stale_banner(generated, now_utc, CARD_MAX_AGE_HOURS, label="card")
    provenance = "_Source: %s — generated %s UTC — fetched %s_" % (
        card_path, generated, now_utc
    )
    return "%s*Today's card*\n%s\n\n%s" % (banner, provenance, body)


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

    banner = _stale_banner(generated, now_utc, CARD_MAX_AGE_HOURS, label="card")
    lines = [banner + header if banner else header, ""]
    for fx in fixtures:
        scores = fx.get("scores") or []
        if not scores:
            continue

        # Fixture name on its own line.
        lines.append("*%s*" % fx["fixture"])

        # xG forecast from the model (home – away).
        xg_home = fx.get("xg_home")
        xg_away = fx.get("xg_away")
        if xg_home is not None and xg_away is not None:
            lines.append("xG %.2f – %.2f" % (xg_home, xg_away))

        # Top score (most likely) + up to 4 runner-ups.
        top = scores[0]
        top_str = "*%s* (%s%%)" % (top["score"], _fmt_prob(top["prob"]))
        runners = scores[1:5]
        runner_strs = [
            "%s %s%%" % (s["score"], _fmt_prob(s["prob"])) for s in runners
        ]
        score_line = top_str
        if runner_strs:
            score_line += "  | " + " | ".join(runner_strs)
        lines.append(score_line)

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


def handle_next(
    next_path: str = NEXT_PATH,
    now_utc: Optional[str] = None,
) -> str:
    """Serve the cached next-match preview written by ``scripts/wca_build_card.py``.

    The preview (blended winner probs, corners model, market anytime scorers,
    reconciled scoreline distribution) is built on cron alongside the main
    card; this handler only reads the cache and flags staleness.

    Rule 1 (provenance): every reply leads with source path + generation
    timestamp.  No generation timestamp -> NO BET.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cached = cardcache.read_card(
        next_path, now_utc=now_utc, max_age_hours=CARD_MAX_AGE_HOURS
    )
    if cached is None:
        return (
            "*Next match*\n"
            "No preview cached yet. The cron build (`scripts/wca_build_card.py`) "
            "writes it alongside the main card — try again after the next build."
        )
    generated = cached.get("generated")
    if not generated:
        return (
            "*Next match*\n"
            "NO BET — `%s` has no generation timestamp; "
            "data age is unknown." % next_path
        )
    body = cached.get("text") or "(empty preview)"
    banner = _stale_banner(generated, now_utc, CARD_MAX_AGE_HOURS, label="card")
    provenance = "_Source: %s — generated %s UTC_\n\n" % (next_path, generated)
    return banner + provenance + body


def handle_goalscorers(
    goalscorers_path: str = GOALSCORERS_PATH,
    now_utc: Optional[str] = None,
) -> str:
    """Serve the cached /goalscorers card written by ``scripts/wca_build_card.py``.

    Anytime + first-goalscorer recommendations for the next few fixtures (best
    book / Polymarket prices, with player-level model edges + Kelly £ stakes
    where a share exists). Built on cron alongside the main card; this handler
    reads the cache and flags staleness.

    Rule 1 (provenance): every reply leads with source path + generation
    timestamp.  No generation timestamp -> NO BET.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    cached = cardcache.read_card(
        goalscorers_path, now_utc=now_utc, max_age_hours=CARD_MAX_AGE_HOURS
    )
    if cached is None:
        return (
            "*Goalscorers*\n"
            "No card cached yet. The cron build (`scripts/wca_build_card.py`) "
            "writes it alongside the main card — try again after the next build."
        )
    generated = cached.get("generated")
    if not generated:
        return (
            "*Goalscorers*\n"
            "NO BET — `%s` has no generation timestamp; "
            "data age is unknown." % goalscorers_path
        )
    body = cached.get("text") or "(empty card)"
    banner = _stale_banner(generated, now_utc, CARD_MAX_AGE_HOURS, label="card")
    provenance = "_Source: %s — generated %s UTC_\n\n" % (goalscorers_path, generated)
    return banner + provenance + body


def _fmt_prob(prob: Optional[float]) -> str:
    """Format a probability as a compact percentage string (1 d.p.)."""
    if prob is None:
        return "?"
    return "%.1f" % prob


def handle_structure(docs_dir: Optional[str] = None) -> str:
    """Latest project-structure metrics from docs/architecture/structure_*.md.

    Sends only the metrics table (the Mermaid chart is useless in Telegram).
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

    # Keep only the metrics section (the table).
    marker = "## Metrics"
    idx = content.find(marker)
    metrics_part = content[idx + len(marker):].strip() if idx >= 0 else content.strip()

    # The snapshot date is just a day (YYYY-MM-DD); treat it as midnight UTC for
    # the staleness check so an ancient snapshot is flagged.
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    banner = _stale_banner(
        "%sT00:00:00" % date, now_utc, STRUCTURE_MAX_AGE_HOURS, label="snapshot",
    )
    return "%s*Project structure* (%s)\n\n%s" % (banner, date, metrics_part)


def handle_settle(text: str, db_path: str) -> str:
    """Log a bet settlement: ``/settle <bet-id> <outcome> [closing-odds]``."""
    import sqlite3

    parts = text.strip().split()
    if len(parts) < 3:
        return (
            "Usage: `/settle <bet-id> <outcome> [closing-odds]`\n\n"
            "Examples:\n"
            "`/settle 42 won 3.20` — bet 42 won at 3.20\n"
            "`/settle 43 lost` — bet 43 lost\n"
            "`/settle 44 void` — bet 44 voided"
        )

    try:
        bet_id = int(parts[1])
        outcome = parts[2].lower()
        closing_odds = None
        if len(parts) > 3:
            closing_odds = float(parts[3])
    except (ValueError, IndexError):
        return "Invalid syntax. Try: `/settle 42 won 3.20`"

    if outcome not in ("won", "lost", "void"):
        return "Outcome must be 'won', 'lost', or 'void'."

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # Fetch the open bet
        row = con.execute(
            "SELECT id, stake, decimal_odds, model_prob, closing_odds, source, "
            "market, selection FROM bets WHERE id = ? AND status = 'open'",
            (bet_id,),
        ).fetchone()

        if not row:
            return f"No open bet with ID {bet_id}."

        stake = float(row["stake"] or 0.0)
        odds_backed = float(row["decimal_odds"] or 0.0)
        is_free = str(row["source"] or "model") == "offer"
        is_lay = "lay" in (str(row["market"] or "") + " " + str(row["selection"] or "")).lower()

        # Explicit closing odds win; otherwise fall back to the close the
        # snapshot daemon auto-captured at kickoff (mirrors wca_settle.py, so
        # /settle never wipes an auto-captured close on void).
        if closing_odds is None and row["closing_odds"] is not None:
            closing_odds = float(row["closing_odds"])
        if outcome in ("won", "lost") and closing_odds is None:
            return (
                f"Outcome '{outcome}' needs closing odds and bet {bet_id} has "
                f"no auto-captured close yet. Try: `/settle {bet_id} {outcome} <odds>`"
            )

        # Realized P&L pays at the price the bet was BACKED at (the close only
        # feeds CLV, never the payout). Free bets are stake-not-returned (a loss
        # costs £0); lays risk the LIABILITY (stake*(odds-1)), not the stake.
        if outcome == "void":
            settled_pl = 0.0
        elif is_lay:
            liability = stake * (odds_backed - 1)
            settled_pl = stake if outcome == "won" else -liability
        elif outcome == "won":
            settled_pl = stake * (odds_backed - 1)
        else:  # lost
            settled_pl = 0.0 if is_free else -stake

        # CLV: backed price vs closing line — the ledger-wide convention
        # (ratio - 1), shared with wca.ledger.store.set_closing_odds and
        # wca_settle.py so the clv column means one thing across all rows.
        clv = None
        if odds_backed > 0 and closing_odds and closing_odds > 0:
            clv = odds_backed / closing_odds - 1.0

        # Update ledger (ensure the lazily-added settled_ts column exists, so
        # the bot can settle on a DB whose first settlement came through here).
        from datetime import datetime, timezone

        from wca.ledger import store as _store
        _store._ensure_settled_ts_column(con)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        con.execute(
            "UPDATE bets SET status = ?, settled_pl = ?, closing_odds = ?, clv = ?, settled_ts = ? WHERE id = ?",
            (outcome, settled_pl, closing_odds, clv, now_utc, bet_id),
        )
        con.commit()

        # Format reply
        lines = [f"✅ Bet {bet_id} settled as *{outcome}*"]
        if closing_odds:
            lines.append(f"Closing odds: {closing_odds:.2f}")
        lines.append(f"Realized P&L: {settled_pl:+.2f}")
        if clv is not None:
            lines.append(f"CLV: {clv:+.4f} ({clv*100:+.2f}%)")
        reply = "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        con.close()

    # Settling moves a bet from open -> closed, so the site's closed-positions
    # feed is now stale. Regenerate + push it immediately rather than waiting up
    # to an hour for the publish cron. Best-effort (never blocks the reply).
    _autosync(db_path, reason="bet %d settled %s" % (bet_id, outcome))
    return reply


# ---------------------------------------------------------------------------
# Price-boost evaluation.
#
# A boost is *priced* against the model, never logged: the reply is the whole
# deliverable. This keeps the bot decoupled from Component A (the ledger) —
# we deliberately do NOT write the DB or the ledger from the boost path in v1.
# Both the `/boost` text command and a `boost`-captioned photo funnel through
# the same `wca.boosts.evaluate_boost` + `format_boost_verdict`.
# ---------------------------------------------------------------------------


def parse_boost_command(text: str) -> Optional[Any]:
    """Parse a ``/boost`` command line into a :class:`wca.boosts.Boost`.

    Grammar::

        /boost <site> | <match> | <market> | <selection> | <odds> [was <odds>] [inplay]

    The five pipe-separated fields are required. The last field carries the
    boosted decimal odds and may be trailed by ``was <odds>`` (the pre-boost
    price) and/or ``inplay`` (mark the offer as live). Returns ``None`` if the
    line does not have the five fields or the odds are unparseable.
    """
    from wca.boosts import Boost
    from wca.bot.vision import fractional_to_decimal

    # Drop the leading "/boost" token.
    body = re.sub(r"^\s*/boost(?:@\S+)?\s*", "", text or "", flags=re.IGNORECASE)
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 5:
        return None
    site, match, market, selection, tail = parts[0], parts[1], parts[2], parts[3], parts[4]
    if not (site and match and market and selection and tail):
        return None

    # The tail is "<odds> [was <odds>] [inplay]" in any trailing order.
    is_inplay = False
    if re.search(r"\binplay\b", tail, re.IGNORECASE) or re.search(r"\bin-?play\b", tail, re.IGNORECASE):
        is_inplay = True
        tail = re.sub(r"\bin-?play\b", " ", tail, flags=re.IGNORECASE)

    was_odds: Optional[float] = None
    was_m = re.search(r"\bwas\s+([0-9/.]+|evs|evens)", tail, re.IGNORECASE)
    if was_m:
        try:
            was_odds = fractional_to_decimal(was_m.group(1))
        except ValueError:
            was_odds = None
        tail = tail[: was_m.start()] + tail[was_m.end():]

    odds_token = tail.strip().split()[0] if tail.strip() else ""
    try:
        boosted = fractional_to_decimal(odds_token)
    except ValueError:
        return None

    return Boost(
        site=site,
        fixture=match,
        market=market,
        selection=selection,
        boosted_odds=boosted,
        was_odds=was_odds,
        is_inplay=is_inplay,
    )


def format_boost_verdict(boost: Any, ev: Any) -> str:
    """Render a Markdown verdict for a priced boost (no ledger write).

    Three shapes: ``✅ +EV`` (edge %, fair odds, model prob), ``❌ not +EV``
    (same numbers, so the reader sees how far under fair it sits), and
    ``⚠️ can't price`` (+ the honest reason from :class:`wca.boosts.BoostEval`).
    """
    site = (getattr(boost, "site", "") or "?").strip() or "?"
    fixture = (getattr(boost, "fixture", "") or "?").strip() or "?"
    market = (getattr(boost, "market", "") or "?").strip() or "?"
    selection = (getattr(boost, "selection", "") or "?").strip() or "?"
    boosted = getattr(boost, "boosted_odds", None)
    was = getattr(boost, "was_odds", None)

    boosted_str = ("%.2f" % boosted) if boosted else "?"
    was_str = (" (was %.2f)" % was) if was else ""
    header = "⚡ *Boost* — %s\n%s — *%s* @ %s%s" % (
        site, fixture, selection, boosted_str, was_str,
    )

    if not getattr(ev, "priceable", False):
        return "%s\n⚠️ *can't price* — %s" % (header, getattr(ev, "reason", "no model price"))

    model_prob = getattr(ev, "model_prob", None)
    fair = getattr(ev, "fair_odds", None)
    edge = getattr(ev, "edge", None)
    prob_str = ("%.1f%%" % (model_prob * 100.0)) if model_prob is not None else "?"
    fair_str = ("%.2f" % fair) if fair else "?"
    edge_str = ("%+.1f%%" % (edge * 100.0)) if edge is not None else "?"

    tag = "✅ *+EV*" if getattr(ev, "is_plus_ev", False) else "❌ *not +EV*"
    return "%s\n%s — edge %s | fair %s | model %s\n_%s_" % (
        header, tag, edge_str, fair_str, prob_str, getattr(ev, "reason", ""),
    )


def handle_boost(text: str, *, scores_path: str = "site/scores_data.json") -> str:
    """`/boost` — price a typed boost against the model feed (no ledger write)."""
    from wca import boosts

    boost = parse_boost_command(text)
    if boost is None:
        return (
            "Usage: `/boost <site> | <match> | <market> | <selection> | <odds> "
            "[was <odds>] [inplay]`\n\n"
            "Example:\n"
            "`/boost bet365 | Brazil vs Morocco | Match Result | Brazil | 2.5 was 1.8`\n"
            "`/boost SkyBet | Qatar vs Switzerland | Over 2.5 Goals | Over | 2.2`"
        )
    ev = boosts.evaluate_boost(boost, boosts.load_scores_feed(scores_path))
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    banner = _stale_banner(
        _feed_generated(scores_path), now_utc, SCORES_FEED_MAX_AGE_HOURS,
        label="odds feed",
    )
    return banner + format_boost_verdict(boost, ev)


def handle_accas(scores_path: str = "site/scores_data.json") -> str:
    """`/accas` — multi-leg accumulators for the next 5 matches (4+ legs, min 2.0 odds).

    Reads model fair odds from scores_data.json via accas.load_odds_df(), applies
    the rung-0 longshot guard (odds > 10x excluded), and builds 4+ leg accas.
    Odds shown are model fair values (1/model_prob), not market prices.

    Rule 1 (provenance): every reply leads with source path + generation
    timestamp.  No generation timestamp -> NO BET.
    """
    from wca import accas

    try:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        generated = _feed_generated(scores_path)
        banner = _stale_banner(generated, now_utc, SCORES_FEED_MAX_AGE_HOURS, label="odds feed")

        if not generated:
            return (
                "*Accumulators*\n"
                "NO BET — `%s` has no generation timestamp; "
                "data age is unknown." % scores_path
            )

        provenance = "_Source: %s — %s — fetched %s UTC_\n\n" % (
            scores_path, generated, now_utc
        )

        odds_df = accas.load_odds_df(scores_path)
        if odds_df.empty:
            return (
                "%s%s*Accumulators*\n"
                "NO BET — no fixture data in `%s`." % (banner, provenance, scores_path)
            )

        acca_list = accas.build_accas_from_odds(
            odds_df, max_fixtures=5, min_legs=4, min_leg_odds=2.0
        )
        if not acca_list:
            return (
                "%s%s*Accumulators*\n"
                "NO BET — no valid 4+ leg accas found with ≥2.0 fair-odds legs "
                "in the next 5 fixtures. All qualifying legs may be short-price "
                "favourites (fair odds < 2.0) or fewer than 4 fixtures have "
                "model data." % (banner, provenance)
            )

        return banner + provenance + accas.format_accas(acca_list)
    except Exception as exc:
        return "*Accumulators*\nError building accas: %s" % exc


def handle_boost_photo(
    image_bytes: bytes,
    *,
    scores_path: str = "site/scores_data.json",
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Read a boost screenshot, price it, and return the verdict (no ledger write).

    Used when a photo's caption contains ``boost``: instead of the betslip
    ingest flow, we extract the single boosted selection via vision and price it
    against the model. Nothing is parked or written — the verdict is the reply.
    """
    from wca import boosts
    from wca.bot.vision import VisionError, extract_boost

    try:
        boost = extract_boost(image_bytes, api_key=api_key, model=model)
    except VisionError as exc:
        return "Couldn't read that boost: %s" % exc
    except Exception as exc:  # never crash the loop on a vision hiccup
        return "Vision error: %s" % exc
    ev = boosts.evaluate_boost(boost, boosts.load_scores_feed(scores_path))
    return format_boost_verdict(boost, ev)


# ---------------------------------------------------------------------------
# Betslip-screenshot ingestion.
# ---------------------------------------------------------------------------

# Per-chat parked extractions awaiting a yes/no confirmation. Kept in-process:
# the bot is single-instance and a pending slip that is never confirmed simply
# expires when the process restarts.
_PENDING_PHOTO_BETS: Dict[Any, List[Any]] = {}

# Resolved account/source tags for each parked slip, keyed by chat id and kept
# in lockstep with ``_PENDING_PHOTO_BETS``. Caption tags are remembered here so
# a bare ``yes`` reply still logs with the tags shown at parse time, while a
# tagged reply (``yes a2 offer``) can override them.
_PENDING_PHOTO_TAGS: Dict[Any, Dict[str, str]] = {}


def _slug(text: str) -> str:
    """Compact, deterministic match id fragment from a free-text description."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_").upper()
    return s[:48] or "UNKNOWN"


def _enrich_bets_from_card(
    bets: List[Any],
    card_path: str = CARD_PATH,
) -> List[Any]:
    """Enrich extracted bets with model_prob and ev from the cached card.

    Matches bets by (match, selection, odds ±0.05) and populates model_prob/ev
    if found in the card. This allows screenshot bets to have model context
    even though they were extracted from an image.
    """
    if not os.path.exists(card_path):
        return bets

    try:
        import re
        card_text = open(card_path, "r", encoding="utf-8").read()

        # Extract picks: "*1. Match* — Selection @ *odds*" + "model X% ... edge *+Y%*"
        picks: Dict[str, Dict[str, float]] = {}
        lines = card_text.split("\n")
        current_match = None

        for line in lines:
            # Pick header
            m = re.match(r"^\*\d+\.\s*(.+?)\*\s*—\s*(.+?)\s*@\s*\*([0-9.]+)\*", line.strip())
            if m:
                current_match = {
                    "match": m.group(1).strip(),
                    "selection": m.group(2).strip(),
                    "odds": float(m.group(3)),
                    "model_prob": None,
                    "ev": None,
                }
                continue

            # Model/edge line
            if current_match and "model" in line.lower():
                model_m = re.search(r"model\s+([0-9.]+)%", line)
                edge_m = re.search(r"edge\s*\*?([+-]?[0-9.]+)", line)
                if model_m:
                    current_match["model_prob"] = float(model_m.group(1)) / 100.0
                if edge_m:
                    try:
                        current_match["ev"] = float(edge_m.group(1)) / 100.0
                    except ValueError:
                        pass

                key = (current_match["match"], current_match["selection"], current_match["odds"])
                picks[key] = current_match

        # Enrich bets
        for bet in bets:
            bet_match = (getattr(bet, "match_desc", "") or "").lower()
            bet_sel = (getattr(bet, "selection", "") or "").lower()
            bet_odds = float(getattr(bet, "decimal_odds", 0) or 0)

            # Fuzzy match against picks
            for (pm, ps, po), pick in picks.items():
                pm_lower = pm.lower()
                ps_lower = ps.lower()

                if (pm_lower in bet_match or bet_match in pm_lower) and \
                   (ps_lower in bet_sel or bet_sel in ps_lower) and \
                   abs(bet_odds - po) < 0.05:
                    bet.model_prob = pick["model_prob"]
                    bet.ev = pick["ev"]
                    break
    except Exception:
        pass  # Silently fail; enrichment is optional

    return bets


def resolve_tags(
    text: Optional[str],
    *,
    default_account: str = "1",
    default_source: str = "punt",
    allow_bare_account: bool = False,
) -> Dict[str, str]:
    """Parse account/source tags out of a screenshot caption or yes-reply.

    Screenshot ingests default to source=punt (a discretionary bet unless the
    caption says otherwise — free bets are auto-detected as 'offer' from the
    slip, and 'model' must be tagged explicitly), account=1; a caption can
    override either dimension. Recognised tokens
    (case-insensitive, word-boundary matched anywhere in the text):

      account: ``account 2`` / ``acc2`` / ``a2`` -> account="2"
               ``account 1`` / ``acc1`` / ``a1`` -> account="1"
      source : ``model`` -> "model", ``offer`` -> "offer", ``punt`` -> "punt"

    With ``allow_bare_account=True`` (the yes-reply path), a bare digit token is
    also accepted, e.g. ``yes 2`` / ``yes 2 punt`` / ``yes punt 2`` -> account
    "2". This is opt-in so caption text (which may contain stray digits like a
    stake) never false-matches an account.

    Last matching token wins for each dimension. Returns
    ``{"account": ..., "source": ...}``.
    """
    account = default_account
    source = default_source
    t = " " + (text or "").lower() + " "
    bare2 = bare1 = False
    if allow_bare_account:
        # Strip the leading verb so ``yes``/``y`` is never read as a token, then
        # look for a standalone account digit anywhere in the remainder.
        rest = re.sub(r"^\s*(?:yes|y|no|n)\b", " ", t, flags=re.IGNORECASE)
        bare2 = bool(re.search(r"\b2\b", rest))
        bare1 = bool(re.search(r"\b1\b", rest))
    if re.search(r"\b(account\s*2|acc2|a2)\b", t) or bare2:
        account = "2"
    elif re.search(r"\b(account\s*1|acc1|a1)\b", t) or bare1:
        account = "1"
    if re.search(r"\bmodel\b", t):
        source = "model"
    elif re.search(r"\boffer\b", t):
        source = "offer"
    elif re.search(r"\bpunt\b", t):
        source = "punt"
    return {"account": account, "source": source}


_SOURCE_WORD = {"model": "model", "offer": "offer", "punt": "punt"}


def _format_extracted(bets: List[Any], tags: Optional[Dict[str, str]] = None) -> str:
    """Human-readable confirmation prompt for parsed selections.

    ``tags`` (resolved account/source) is echoed so the user can correct a
    mis-tag in the yes-reply before anything is written.
    """
    from wca.bot.vision import currency_symbol

    lines = ["*Parsed %d bet(s) from your slip:*" % len(bets)]
    for i, b in enumerate(bets, 1):
        sym = currency_symbol(getattr(b, "currency", None))
        odds = ("%.2f" % b.decimal_odds) if b.decimal_odds else "?"
        stake = ("%s%.2f" % (sym, b.stake)) if b.stake is not None else sym + "?"
        book = b.bookmaker or "?"
        flag = "  ⚡boost" if getattr(b, "is_boost", False) else ""
        warn = "" if getattr(b, "confidence", 1.0) >= 0.6 else "  ⚠️low-conf"
        # A combo prints the market label (Bet Builder / Accumulator) as a
        # heading so the single combined price + stake reads as ONE bet, with
        # the legs broken out underneath.
        if getattr(b, "is_combo", False):
            lines.append(
                "%d. %s — *%s* @ %s | stake %s | %s%s%s"
                % (i, b.match_desc, getattr(b, "market", "Combo"),
                   odds, stake, book, flag, warn)
            )
            for leg in (b.selection or "").split(" + "):
                if leg.strip():
                    lines.append("    • %s" % leg.strip())
        else:
            lines.append(
                "%d. %s — *%s* @ %s | stake %s | %s%s%s"
                % (i, b.match_desc, b.selection, odds, stake, book, flag, warn)
            )
    if tags:
        acct = tags.get("account", "1")
        src = _SOURCE_WORD.get(tags.get("source", "punt"), tags.get("source", "punt"))
        lines.append(
            "\nTags: account *%s* | source *%s*  "
            "(override in your reply, e.g. `yes a2 offer`)" % (acct, src)
        )
    lines.append(
        "\nReply *yes* to log all to the ledger, *no* to discard. "
        "Tag the reply to set provenance, e.g. `yes 2 offer` / `yes punt` "
        "(account `1`/`2`, source `model`/`offer`/`punt`; default 1 / punt)."
    )
    return "\n".join(lines)


def handle_photo(
    image_bytes: bytes,
    chat_id: Any,
    pending: Optional[Dict[Any, List[Any]]] = None,
    *,
    caption: Optional[str] = None,
    pending_tags: Optional[Dict[Any, Dict[str, str]]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Extract bets from a betslip image and park them for confirmation.

    ``caption`` is the photo's message caption; account/source tags in it
    (``a2``, ``offer``, ``punt``, ``model`` …) are resolved via
    :func:`resolve_tags` and echoed in the confirmation prompt. Screenshot
    ingests default to ``account="1"`` / ``source="model"`` unless the caption
    says otherwise.
    """
    if pending is None:
        pending = _PENDING_PHOTO_BETS
    if pending_tags is None:
        pending_tags = _PENDING_PHOTO_TAGS
    from wca.bot.vision import extract_bets_from_image, VisionError

    try:
        bets = extract_bets_from_image(image_bytes, api_key=api_key, model=model)
    except VisionError as exc:
        return "Couldn't read that slip: %s" % exc
    except Exception as exc:  # never crash the loop on a vision hiccup
        return "Vision error: %s" % exc
    if not bets:
        return "No bets detected. Send a clearer screenshot of the full slip."
    # Enrich with model data from the card (optional — silently skipped if not found)
    bets = _enrich_bets_from_card(bets, card_path=CARD_PATH)
    tags = resolve_tags(caption)
    # A free bet (purple gift icon / 'Free Bet' label, detected by vision) is a
    # promo — stake-not-returned — so it must be source='offer' for the ledger's
    # free-bet P&L/exposure math. Auto-apply UNLESS the caption explicitly set a
    # source token, which the user's intent should always win over.
    free_detected = any(getattr(b, "is_free_bet", False) for b in bets)
    explicit_source = bool(re.search(r"\b(model|offer|punt)\b", (caption or "").lower()))
    if free_detected and not explicit_source:
        tags = {**tags, "source": "offer"}
    pending[chat_id] = bets
    pending_tags[chat_id] = tags
    reply = _format_extracted(bets, tags)
    if free_detected:
        reply = "🎁 *Free bet detected* (stake not returned → tagged `offer`).\n\n" + reply
    return reply


_YESNO_RE = re.compile(r"^\s*(yes|y|no|n)\b", re.IGNORECASE)

# Y/N PM-<n> and Y/N BET-<id> are *order* confirmations (handled by
# handle_confirmation), NOT betslip yes/no. handle_photo_confirmation must defer
# them so e.g. "Y PM-5" executes the parked Polymarket order instead of logging
# a parked betslip on the leading "Y" (the 2026-06-19 mis-route).
_ORDER_CONFIRM_RE = re.compile(r"^\s*[yn]\s+(?:bet|pm)-\d+", re.IGNORECASE)


def handle_photo_confirmation(
    text: str,
    chat_id: Any,
    db_path: str,
    pending: Optional[Dict[Any, List[Any]]] = None,
    *,
    pending_tags: Optional[Dict[Any, Dict[str, str]]] = None,
    ts_utc: Optional[str] = None,
) -> Optional[str]:
    """Resolve a parked betslip on a ``yes`` / ``no`` reply. None if not applicable.

    The reply may carry tag overrides, e.g. ``yes a2 offer``; these take
    precedence over the tags resolved from the caption at parse time. A bare
    ``yes`` logs with the parse-time (caption) tags, defaulting to
    ``account="1"`` / ``source="model"`` for an untagged screenshot.
    """
    # An order confirm (Y/N PM-<n> / Y/N BET-<id>) is never a betslip yes/no —
    # defer it to handle_confirmation even when a betslip is parked, so the order
    # executes instead of the slip being logged on the leading "Y".
    if _ORDER_CONFIRM_RE.match(text or ""):
        return None
    if pending is None:
        pending = _PENDING_PHOTO_BETS
    if pending_tags is None:
        pending_tags = _PENDING_PHOTO_TAGS
    if chat_id not in pending:
        return None
    m = _YESNO_RE.match(text or "")
    if not m:
        return None  # leave the slip parked; let normal command routing proceed
    ans = m.group(1).lower()
    bets = pending.pop(chat_id)
    parked_tags = pending_tags.pop(chat_id, None) or {"account": "1", "source": "punt"}
    if ans in {"no", "n"}:
        return "Discarded %d parsed selection(s)." % len(bets)

    # Reply tags override the parked (caption) tags for each dimension.
    tags = resolve_tags(
        text,
        default_account=parked_tags.get("account", "1"),
        default_source=parked_tags.get("source", "punt"),
        allow_bare_account=True,
    )
    account = tags["account"]
    source = tags["source"]

    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    logged: List[str] = []
    for b in bets:
        match_id = "MANUAL_" + _slug(b.match_desc)
        # A combo (Bet Builder / accumulator) is ONE bet at ONE combined price
        # with ONE total stake; its leg breakdown lives in notes, never as extra
        # rows. Tag offer/free bets "SNR" (stake-not-returned) so the /bets and
        # /summary P&L pools treat the stake as non-cash (the existing
        # convention: notes containing "SNR").
        extra = []
        if getattr(b, "is_combo", False):
            extra.append(getattr(b, "market", None) or "Combo")
        combo_notes = getattr(b, "notes", "") or ""
        if combo_notes:
            extra.append(combo_notes)
        if source == "offer" or getattr(b, "is_free_bet", False):
            extra.append("SNR")
        note = "screenshot ingest; currency=%s; conf %.2f%s%s" % (
            getattr(b, "currency", None) or "GBP",
            getattr(b, "confidence", 0.0),
            "; boost" if getattr(b, "is_boost", False) else "",
            ("; " + "; ".join(extra)) if extra else "",
        )
        try:
            bid = record_bet(
                ts_utc,
                match_id,
                b.match_desc,
                b.market or "unknown",
                b.selection,
                _canon_platform(b.bookmaker or "unknown"),
                float(b.decimal_odds or 0.0),
                float(b.stake or 0.0),
                # model_prob and ev are populated by _enrich_bets_from_card() when
                # the card has a matching pick; None for accas/bet-builders/promos.
                model_prob=getattr(b, "model_prob", None),
                ev=getattr(b, "ev", None),
                notes=note,
                account=account,
                source=source,
                db_path=db_path,
            )
            logged.append("#%d %s @ %s" % (bid, b.selection, b.decimal_odds or "?"))
        except Exception as exc:  # report per-bet failure, keep going
            logged.append("ERR %s: %s" % (b.selection, exc))
    _autosync(db_path, "screenshot ingest")
    a2 = " (A2)" if account == "2" else ""
    return "Logged %d to the ledger [%s%s]:\n%s" % (
        len(logged), source, a2, "\n".join(logged)
    )


def _autosync(db_path: str, reason: str) -> None:
    """Regenerate + push the site after a ledger write. Never raises."""
    try:
        from wca import sync

        ok = sync.push_site(reason=reason, db_path=db_path)
        if ok:
            print("[bot] site auto-synced (%s)" % reason)
    except Exception as exc:  # the bot must survive any sync failure
        print("[bot] autosync skipped: %s" % exc)


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
# integer token suffix; value is the proposal dict.  Backed by the
# ``pm_parked`` SQLite table so proposals survive bot restarts and cross the
# process boundary (the propose CLI parks; the bot daemon executes).  The
# in-memory dict remains the test seam.
_PENDING_ORDERS: Dict[int, Dict[str, Any]] = {}
_PM_SEQ = {"n": 0}

_PARKED_DB_ENV = "WCA_DB"
_PARKED_DB_DEFAULT = "data/wca.db"


def _parked_db_path() -> str:
    return os.environ.get(_PARKED_DB_ENV, _PARKED_DB_DEFAULT)


def _parked_conn(db_path: Optional[str] = None):
    import sqlite3

    conn = sqlite3.connect(db_path or _parked_db_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pm_parked ("
        " n INTEGER PRIMARY KEY AUTOINCREMENT,"
        " proposal_json TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'parked',"
        " ts_utc TEXT NOT NULL)"
    )
    return conn


def _parked_load(n: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    import json as _json

    try:
        conn = _parked_conn(db_path)
        try:
            row = conn.execute(
                "SELECT proposal_json FROM pm_parked WHERE n=? "
                "AND status IN ('parked','failed')",
                (n,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - DB issues degrade to in-memory only
        return None
    return _json.loads(row[0]) if row else None


def _parked_set_status(n: int, status: str, db_path: Optional[str] = None) -> None:
    try:
        conn = _parked_conn(db_path)
        try:
            conn.execute("UPDATE pm_parked SET status=? WHERE n=?", (status, n))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _parked_list(db_path: Optional[str] = None) -> List[Any]:
    import json as _json

    try:
        conn = _parked_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT n, proposal_json FROM pm_parked WHERE status='parked' ORDER BY n"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []
    return [(n, _json.loads(pj)) for n, pj in rows]


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
    import json as _json

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Under pytest, never persist to the real ledger unless a test DB is
    # explicitly pointed at via WCA_DB (mirrors the sync.push_site guard).
    if "PYTEST_CURRENT_TEST" in os.environ and _PARKED_DB_ENV not in os.environ:
        _PM_SEQ["n"] += 1
        n = _PM_SEQ["n"]
        _PENDING_ORDERS[n] = dict(proposal)
        return "PM-%d" % n
    try:
        conn = _parked_conn()
        try:
            cur = conn.execute(
                "INSERT INTO pm_parked (proposal_json, status, ts_utc) "
                "VALUES (?, 'parked', ?)",
                (_json.dumps(dict(proposal)), ts),
            )
            conn.commit()
            n = int(cur.lastrowid)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - degrade to in-memory sequencing
        _PM_SEQ["n"] += 1
        n = _PM_SEQ["n"]
    _PM_SEQ["n"] = max(_PM_SEQ["n"], n)
    _PENDING_ORDERS[n] = dict(proposal)
    return "PM-%d" % n


def describe_pm_selection(proposal: Dict[str, Any]) -> str:
    """Plain-English statement of what a Polymarket BUY actually backs.

    A Polymarket outcome is a bare ``Yes``/``No`` against a market *question*,
    which is meaningless without it ("No @ 0.08" — no on what?).  We derive the
    human meaning purely from the proposal's own ``market_question`` +
    ``outcome`` (never fabricated):

    * draw market  -> "the DRAW" (Yes) / "NO draw — either team wins" (No)
    * moneyline    -> "<Team> to WIN" (Yes) / "<Team> NOT to win" (No)
    * anything else falls back to the verbatim question + outcome.
    """
    import re as _re

    q = (proposal.get("market_question") or proposal.get("label") or "").strip()
    outcome = (proposal.get("outcome") or proposal.get("selection") or "").strip()
    yes = outcome.lower() in ("yes", "y", "true")
    ql = q.lower()
    if "draw" in ql or "tie" in ql:
        return "the DRAW" if yes else "NO draw — either team wins"
    m = _re.search(r"will\s+(.+?)\s+win\b", q, _re.IGNORECASE)
    if m:
        team = m.group(1).strip().rstrip("?").strip()
        return ("%s to WIN" % team) if yes else ("%s NOT to win" % team)
    if q:
        return "%s = %s" % (q, outcome or "?")
    return outcome or "?"


def format_parked_order(token: str, proposal: Dict[str, Any]) -> str:
    """Human confirmation prompt for one parked Polymarket order.

    Shows the match, a plain-English reading of what the Yes/No actually backs,
    the verbatim market question + outcome, price, stake, model prob and EV — so
    a bare "No @ 0.08" can never be confirmed without knowing what it means.
    """
    side = str(proposal.get("side", "BUY")).upper()
    price = float(proposal.get("price", 0.0))
    size = float(proposal.get("size", 0.0))
    notional = price * size

    match_desc = proposal.get("match_desc", "")
    outcome = proposal.get("outcome") or proposal.get("selection") or ""
    question = (proposal.get("market_question") or proposal.get("label") or "").strip()
    backing = describe_pm_selection(proposal)

    model_prob = float(proposal.get("model_prob", 0.0))
    ev_pct = float(proposal.get("ev", 0.0)) * 100.0  # ev stored as decimal (0.28 = 28%)
    size_usd = float(proposal.get("size_usd", notional))

    pm_pool_usd = 2500.0
    pct_pm = (size_usd / pm_pool_usd * 100.0) if pm_pool_usd > 0 else 0.0

    header = ("*%s* — backing %s" % (match_desc, backing)) if match_desc else ("*%s*" % backing)
    qline = ("\n    %s → *%s*" % (question, outcome)) if question else ""
    return (
        "%s%s @ %.2f | $%.2f | model %.1f%% | ev %+.1f%% | %.1f%% PM pool\n"
        "→ `Y %s` execute | `N %s` discard"
        % (header, qline, price, size_usd, model_prob * 100.0, ev_pct, pct_pm, token, token)
    )


def push_parked_order(proposal: Dict[str, Any]) -> str:
    """Park a proposal and return the user-facing confirmation message."""
    token = park_order(proposal)
    return format_parked_order(token, _PENDING_ORDERS[int(token.split("-")[1])])


def _pm_dry_run() -> bool:
    """Polymarket dry-run flag from env (default ON for safety)."""
    return os.environ.get("PM_DRY_RUN", "1").strip().lower() not in {"0", "false", "no", ""}


def _alert_admin(text: str) -> bool:
    """Best-effort Telegram DM to the admin for safety-critical order alerts.

    Used when a live Polymarket order may have reached the chain without being
    fully logged (see :func:`_execute_parked_order`).  Never raises and never
    makes a network call under pytest; returns True only if a message was sent.
    """
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if not admin:
        logger.error("ADMIN ALERT (TELEGRAM_ADMIN_USER_ID unset): %s", text)
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        logger.warning("ADMIN ALERT (suppressed under pytest): %s", text)
        return False
    try:
        TelegramClient().send_message(admin, text)
        return True
    except Exception as exc:  # an alert failure must never break order handling
        logger.error("failed to send admin alert (%s): %s", exc, text)
        return False


def _verify_live_order_logged(
    db_path: str, bid: Optional[int], token_id: str
) -> List[str]:
    """Names of the log artifacts MISSING for a just-placed live order.

    A live (on-chain) order must leave BOTH a ledger row (``bets``) and a
    ``pm_order_log`` row.  Returns whichever are absent so the caller can alert;
    an empty list means fully logged.  Querying problems are reported rather
    than swallowed so a verification failure never reads as success.
    """
    import sqlite3

    missing: List[str] = []
    try:
        conn = sqlite3.connect(db_path)
        try:
            if bid is None or conn.execute(
                "SELECT 1 FROM bets WHERE id = ?", (bid,)
            ).fetchone() is None:
                missing.append("ledger row")
            row = conn.execute(
                "SELECT 1 FROM pm_order_log "
                "WHERE token_id = ? AND dry_run = 0 LIMIT 1",
                (str(token_id),),
            ).fetchone()
            if row is None:
                missing.append("pm_order_log row")
        finally:
            conn.close()
    except Exception as exc:
        logger.error("could not verify live-order logging: %s", exc)
        missing.append("verification failed (%s)" % exc)
    return missing


def _reconcile_sell_fill(
    wallet: Optional[str],
    asset: str,
    seen_hashes: set,
    *,
    retries: int = 5,
    delay: float = 1.5,
    fetch: Optional[Any] = None,
) -> Optional[tuple]:
    """Poll the Data API ``/trades`` for NEW SELL fills of *asset*.

    The order-POST response is an unreliable place to read the filled size from
    (its ``makingAmount`` etc. are signed base-unit echoes, not the match). The
    ``/trades`` feed is authoritative: ``size`` is in human share units and
    ``price`` is per share, so ``proceeds = sum(size*price)`` over the SELL fills
    that appeared since we snapshotted ``seen_hashes`` (just before placing).

    Returns ``(filled_size, proceeds, [tx_hashes])`` — ``(0.0, 0.0, [])`` if none
    appear within the window (FOK that didn't fill) — or ``None`` if there is no
    wallet to query (can't confirm).
    """
    if not wallet:
        return None
    if fetch is None:
        from wca.pm import positions as _positions
        fetch = _positions.fetch_trades
    import time as _time

    for i in range(max(1, retries)):
        try:
            trades = fetch(wallet)
        except Exception:  # noqa: BLE001
            trades = []
        new = [
            t for t in trades
            if getattr(t, "asset", None) == str(asset)
            and getattr(t, "side", "") == "SELL"
            and getattr(t, "tx_hash", "") and t.tx_hash not in seen_hashes
        ]
        if new:
            filled = round(sum(t.size for t in new), 6)
            proceeds = round(sum(t.size * t.price for t in new), 6)
            return filled, proceeds, [t.tx_hash for t in new]
        if i < retries - 1:
            _time.sleep(delay)
    return 0.0, 0.0, []


def execute_cashout(
    proposal: Dict[str, Any],
    db_path: str,
    *,
    trader: Any,
    dry_run: bool,
    ts_utc: Optional[str] = None,
    reconcile_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """Place a cash-out SELL and book ONLY what ACTUALLY filled (from /trades).

    Single source of truth for both the Telegram confirm path and the autonomous
    daemon. Returns a structured result whose ``outcome`` field tells callers
    (esp. the watcher) exactly what happened, so a live order is never confused
    with a dry-run and a real fill is never confused with an unconfirmed one::

        outcome ∈ {no_trader, place_failed, dry_run, sold, no_fill,
                   unconfirmed, settle_failed}
        {submitted, settled, filled_size, proceeds, order_id, dry_run,
         outcome, error, message}

    Invariants:
      * NEVER calls :func:`record_bet` — a SELL must not create a phantom long.
      * Dry-run: signs (via place_order) but does NOT submit and does NOT touch
        the ledger (outcome ``dry_run``).
      * Live: books via :func:`settle_cashout` using the size/proceeds read back
        from the ``/trades`` feed (``reconcile_fn`` is injectable for tests).
        ``settled`` is True ONLY when a real fill was found AND booked. A live
        order whose fill we cannot confirm is ``unconfirmed`` (submitted, not
        settled — alert, never auto-retry); a FOK that didn't fill is ``no_fill``
        (retry next tick).
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    token_id = proposal["token_id"]
    price = float(proposal.get("price", 0.0))
    size = float(proposal.get("size", 0.0))
    label = proposal.get("label") or proposal.get("market") or "market"
    outcome = proposal.get("outcome") or proposal.get("selection") or ""
    match_desc = proposal.get("match_desc") or ("%s %s" % (label, outcome)).strip()
    match_id = proposal.get("match_id") or ("PM_" + _slug(match_desc))
    order_type = str(proposal.get("order_type") or "FOK")
    sel_label = (outcome or label)

    res: Dict[str, Any] = {
        "submitted": False, "settled": False, "filled_size": 0.0,
        "proceeds": 0.0, "order_id": None, "dry_run": bool(dry_run),
        "outcome": "place_failed", "error": None, "message": "",
    }
    if trader is None:
        res["outcome"] = "no_trader"
        res["error"] = "no trader"
        res["message"] = "cash-out not placed — no trader configured"
        return res

    wallet = getattr(trader, "funder", None)
    # Snapshot existing SELL fills for this token BEFORE placing, so the
    # reconciler only counts fills our order produced.
    seen: set = set()
    if not dry_run and reconcile_fn is None and wallet:
        try:
            from wca.pm import positions as _positions
            seen = {t.tx_hash for t in _positions.fetch_trades(wallet)
                    if t.asset == str(token_id) and t.side == "SELL" and t.tx_hash}
        except Exception:  # noqa: BLE001
            seen = set()

    try:
        out = trader.place_order(
            token_id, price, size, "SELL",
            neg_risk=bool(proposal.get("neg_risk", False)),
            dry_run=dry_run, order_type=order_type, de_risk=True,
            market_question=(
                "%s %s" % (proposal.get("market_question") or proposal.get("label") or "",
                           proposal.get("event_slug") or "")
            ).strip(),
        )
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
        res["message"] = "cash-out FAILED to place — %s" % exc
        return res

    order_id = (out or {}).get("orderID") or (out or {}).get("orderId")
    res["order_id"] = order_id or ("dry-run" if dry_run else None)

    if dry_run:
        res["outcome"] = "dry_run"
        res["message"] = (
            "cash-out DRY-ARM (signed, not submitted): $%.2f SELL %s @ %.2f | "
            "ledger untouched" % (price * size, sel_label, price)
        )
        return res

    res["submitted"] = True
    # Confirm the actual fill from the trades feed.
    if reconcile_fn is not None:
        rec = reconcile_fn(token_id, size)            # tests: (filled, proceeds) | None
        rec = None if rec is None else (rec[0], rec[1])
    else:
        r = _reconcile_sell_fill(wallet, token_id, seen)
        rec = None if r is None else (r[0], r[1])

    if rec is None:
        res["outcome"] = "unconfirmed"
        res["error"] = "fill unconfirmed (no wallet/trades)"
        res["message"] = (
            "⚠️ cash-out SELL placed (order %s) but FILL UNCONFIRMED — NOT booked; "
            "reconcile manually." % order_id
        )
        return res
    filled, proceeds_actual = rec
    if filled <= 0:
        res["outcome"] = "no_fill"
        res["message"] = "cash-out SELL not filled (FOK killed); nothing sold"
        return res

    proceeds = round(float(proceeds_actual), 6)
    try:
        info = settle_cashout(
            proceeds,
            token_id=token_id,
            selection=proposal.get("selection") or outcome,
            shares_sold=filled,
            template={
                "match_id": match_id, "match_desc": match_desc,
                "market": proposal.get("market") or "polymarket",
                "selection": proposal.get("selection") or outcome,
                "account": str(proposal.get("account") or "1"),
                "source": str(proposal.get("source") or "model"),
            },
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        res["outcome"] = "settle_failed"
        res["error"] = "settle failed: %s" % exc
        res["filled_size"] = filled
        res["message"] = (
            "⚠️ cash-out SELL filled (order %s, %.2f sh = $%.2f) but BOOKING "
            "FAILED — %s. Reconcile manually." % (order_id, filled, proceeds, exc)
        )
        return res

    _autosync(db_path, "polymarket cash-out")
    res["outcome"] = "sold"
    res["settled"] = True
    res["filled_size"] = filled
    res["proceeds"] = info["proceeds"]
    partial = (" (PARTIAL %.0f/%.0f sh)" % (filled, size)) if filled < size - 1e-6 else ""
    res["message"] = (
        "cash-out LIVE — sold %.2f sh = $%.2f, P&L $%+.2f over %d row(s)%s | order %s"
        % (filled, info["proceeds"], info["pl"],
           info["rows_cashed"] + info["rows_split"], partial, order_id)
    )
    return res


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
        # Resolve the funder from env, falling back to the known proxy (Gnosis
        # safe) — never the empty EOA — when POLYMARKET_FUNDER is unset.  The
        # USDC lives in the proxy, so a live order must sign with maker=proxy.
        from wca.pm.trader import resolve_funder_from_env

        funder, sig_type, used_fallback = resolve_funder_from_env()
        if used_fallback:
            logger.warning(
                "POLYMARKET_FUNDER unset; using known proxy %s (sig type %s). "
                "Set POLYMARKET_FUNDER in .env to silence.",
                funder,
                sig_type,
            )
        try:
            trader = ClobTrader(key, funder=funder, signature_type=sig_type)
        except Exception as exc:
            return "PM-%d: could not init trader (%s)." % (n, exc)

    price = float(proposal.get("price", 0.0))
    size = float(proposal.get("size", 0.0))
    side = str(proposal.get("side", "BUY")).upper()

    # SELL = cash-out: place + book through the single execute_cashout path. It
    # reads the ACTUAL fill and books only what filled — never a phantom open
    # long, never overstated proceeds — so it must NOT fall through to record_bet.
    if side == "SELL":
        res = execute_cashout(
            proposal, db_path, trader=trader, dry_run=dry_run, ts_utc=ts_utc
        )
        return "PM-%d %s." % (n, res.get("message") or "cash-out processed")

    # --- BUY entry: place a (resting) GTC order and record the new position. ---
    try:
        from wca.pm.trader import LiveOrderUnconfirmed
    except Exception:  # pragma: no cover - trader import validated above
        LiveOrderUnconfirmed = None  # type: ignore[assignment]
    try:
        result = trader.place_order(
            proposal["token_id"],
            price,
            size,
            side,
            neg_risk=bool(proposal.get("neg_risk", False)),
            dry_run=dry_run,
            order_type=str(proposal.get("order_type") or "GTC"),
            # Forward the resolved market question (plus the WC event slug, which
            # carries the "fifwc" provenance keyword) so the trader's WC-keyword
            # allowlist actually gates the live path. Single-match questions like
            # "Will X win on <date>?" have no WC keyword on their own.
            market_question=(
                "%s %s"
                % (
                    proposal.get("market_question") or proposal.get("label") or "",
                    proposal.get("event_slug") or "",
                )
            ).strip(),
        )
    except Exception as exc:
        # A live POST that may have reached the chain (network error / 5xx /
        # accepted-but-unlogged) raises LiveOrderUnconfirmed. Treat it as a
        # safety event: alert the admin and do NOT report a clean "order
        # failed" (which would invite a double-spend retry of a possibly-live
        # order). All other exceptions are genuine pre-POST rejections.
        if LiveOrderUnconfirmed is not None and isinstance(exc, LiveOrderUnconfirmed):
            _alert_admin(
                "⚠️ PM-%d LIVE order may be ON-CHAIN but is UNLOGGED.\n"
                "%s\n"
                "token=%s side=%s price=%.4f size=%.4f notional=$%.2f order_id=%s\n"
                "Reconcile data/wca.db against the wallet at "
                "data-api.polymarket.com/activity?user=<funder> BEFORE any retry "
                "— do NOT blindly resend (double-spend risk)."
                % (
                    n,
                    exc,
                    getattr(exc, "token_id", proposal.get("token_id")),
                    getattr(exc, "side", side),
                    float(getattr(exc, "price", price)),
                    float(getattr(exc, "size", size)),
                    float(getattr(exc, "notional", price * size)),
                    getattr(exc, "order_id", None),
                )
            )
            return (
                "PM-%d: ⚠️ order UNCONFIRMED — it may have been placed "
                "on-chain but could not be confirmed/logged (%s). Admin alerted; "
                "verify the wallet on-chain before retrying." % (n, exc)
            )
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
    model_prob = proposal.get("model_prob")
    # PM price IS the market-implied probability (no vig on a prediction market).
    market_prob_devig = price if 0.0 < price < 1.0 else None
    ev_frac = proposal.get("ev")  # fractional edge from kelly.edge(), displayed as %
    try:
        bid = record_bet(
            ts_utc,
            match_id,
            match_desc,
            proposal.get("market") or "pm_moneyline",  # enables auto-CLV via closecapture
            outcome or label,
            "polymarket",
            decimal_odds,
            round(price * size, 2),
            model_prob=model_prob,
            market_prob_devig=market_prob_devig,
            ev=ev_frac,
            notes=notes,
            account="1",
            source="model",
            db_path=db_path,
        )
    except Exception as exc:
        # The order is LIVE (place_order returned) but the ledger write failed.
        # Alert so the on-chain fill gets reconciled rather than discovered late.
        if not dry_run:
            _alert_admin(
                "⚠️ PM-%d LIVE order placed (order id %s) but the ledger write "
                "FAILED: %s. token=%s — backfill data/wca.db against the wallet "
                "on-chain." % (n, order_id, exc, proposal.get("token_id"))
            )
        return "PM-%d: order ok but ledger write failed — %s" % (n, exc)

    # Safeguard: a live (on-chain) order must leave BOTH a ledger row and a
    # pm_order_log row. Verify and alert on any gap so an on-chain order can
    # never silently go unlogged again (2026-06-15 regression guard).
    if not dry_run:
        missing = _verify_live_order_logged(db_path, bid, proposal["token_id"])
        if missing:
            _alert_admin(
                "⚠️ PM-%d LIVE order placed (order id %s, ledger #%s) but logging "
                "is INCOMPLETE — missing %s. Reconcile data/wca.db against the "
                "wallet on-chain." % (n, order_id, bid, " and ".join(missing))
            )

    _autosync(db_path, "polymarket order")
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
        from_db = False
        if proposal is None and pending_orders is _PENDING_ORDERS:
            # Cross-process / post-restart: fall back to the persisted queue.
            proposal = _parked_load(n, db_path)
            from_db = proposal is not None
        if proposal is None:
            return "PM-%d is not a parked order (expired or already handled)." % n
        if verb == "N":
            if pending_orders is _PENDING_ORDERS:
                _parked_set_status(n, "discarded", db_path)
            label = proposal.get("label") or proposal.get("market") or "order"
            return "Discarded parked order PM-%d (%s)." % (n, label)
        result = _execute_parked_order(
            n, proposal, db_path, ts_utc=ts_utc, trader=trader
        )
        # An UNCONFIRMED live order may already be on-chain: do NOT re-park it
        # (a blind Y PM-n retry would risk a double-fill). Record a distinct
        # status and force manual on-chain reconciliation (2026-06-15 guard).
        unconfirmed = isinstance(result, str) and "UNCONFIRMED" in result
        if unconfirmed:
            if pending_orders is _PENDING_ORDERS or from_db:
                _parked_set_status(n, "unconfirmed", db_path)
            return result
        # A failed POST must NOT consume the proposal: keep it retryable
        # (live bug 2026-06-12: CLOB 400 marked the order "executed" and the
        # user's retry got "not a parked order").
        failed = isinstance(result, str) and "order failed" in result
        if failed:
            pending_orders[n] = proposal
            if pending_orders is _PENDING_ORDERS or from_db:
                _parked_set_status(n, "failed", db_path)
            return result + "\nStill parked — retry with Y PM-%d once fixed." % n
        if pending_orders is _PENDING_ORDERS or from_db:
            _parked_set_status(n, "executed", db_path)
        return result

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

    merged: Dict[int, Dict[str, Any]] = {n: p for n, p in _parked_list(db_path)}
    merged.update(_PENDING_ORDERS)
    if merged:
        lines.append("")
        lines.append("*Parked orders*")
        for n in sorted(merged):
            lines.append("  " + format_parked_order("PM-%d" % n, merged[n]))
    else:
        lines.append("")
        lines.append("No parked orders.")
    return "\n".join(lines)


def _pm_daily_spend(db_path: str, *, day_utc: Optional[str] = None) -> Optional[float]:
    """Today's Polymarket spend from a ``pm_order_log`` table, or None if absent.

    Counts only *live* (``dry_run = 0``) notional so dry-run signings — which
    the trader also logs — never inflate the reported spend.  Tolerant of a
    missing table / column (the order log is optional): returns ``None`` so
    ``/pm`` simply omits the line rather than erroring.
    """
    import sqlite3

    if day_utc is None:
        day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = sqlite3.connect(db_path)
    try:
        # Prefer the live-only sum (canonical schema has a dry_run column).
        try:
            cur = con.execute(
                "SELECT COALESCE(SUM(notional), 0.0) FROM pm_order_log "
                "WHERE substr(ts_utc, 1, 10) = ? AND dry_run = 0",
                (day_utc,),
            )
        except sqlite3.OperationalError:
            # Older / hand-rolled table without a dry_run column.
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


def handle_redeem(text: str, db_path: str = "data/wca.db") -> str:
    """Instant-override redeem: cancel an open Polymarket order now (or all).

    ``REDEEM ALL`` cancels every open order; ``REDEEM <order_id>`` cancels one.
    Frees the reserved pUSD without waiting for the 24h auto-redeem cron
    (:mod:`scripts.wca_pm_redeem`).  Cancelling only removes unfilled orders, so
    it never risks money — but it writes to the exchange, hence admin-gated.
    """
    from wca.pm import redeem as redeem_core

    parts = text.strip().split()
    target = parts[1] if len(parts) > 1 else ""
    if not target:
        return "Usage: `REDEEM ALL` or `REDEEM <order_id>` (ids shown in the proposal message)."

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        return "REDEEM: POLYMARKET_PRIVATE_KEY not set — cannot reach the CLOB."
    try:
        from wca.pm.trader import ClobTrader, resolve_funder_from_env
    except Exception as exc:  # noqa: BLE001
        return "REDEEM: trader unavailable (%s)." % exc
    funder, sig_type, _ = resolve_funder_from_env()
    try:
        trader = ClobTrader(key, funder=funder, signature_type=sig_type)
    except Exception as exc:  # noqa: BLE001
        return "REDEEM: could not init trader (%s)." % exc
    try:
        orders = trader.open_orders()
    except Exception as exc:  # noqa: BLE001
        return "REDEEM: could not fetch open orders (%s)." % exc

    redeem_all = target.lower() == "all"
    order_id = None if redeem_all else target
    now_epoch = datetime.now(timezone.utc).timestamp()
    selected = redeem_core.select_orders_to_redeem(
        orders, now_epoch, redeem_all=redeem_all, order_id=order_id,
    )
    if not selected:
        if not orders:
            return "REDEEM: no open orders."
        return "REDEEM: no open order matches `%s`." % target

    done, freed, fails = [], 0.0, []
    for order, _reason in selected:
        oid = redeem_core.order_id_of(order) or "?"
        rem = redeem_core.unfilled_size(order) or 0.0
        price = order.get("price")
        freed += (float(price) * rem) if price not in (None, "") else 0.0
        try:
            trader.cancel_order(oid)
            done.append(oid)
        except Exception as exc:  # noqa: BLE001 — keep going, report at end
            fails.append("%s: %s" % (oid, exc))

    msg = "🧹 Redeemed %d order(s), freed ~$%.2f pUSD." % (len(done), freed)
    if fails:
        msg += "\n⚠️ %d failed: %s" % (len(fails), "; ".join(fails[:5]))
    return msg


def dispatch(text: str, db_path: str) -> str:
    """Map an incoming text message to a reply."""
    confirm = handle_confirmation(text, db_path)
    if confirm is not None:
        return confirm

    if text.strip().lower().split()[:1] == ["redeem"]:
        return handle_redeem(text, db_path)

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
    if cmd == "/next":
        return handle_next(next_path=NEXT_PATH)
    if cmd == "/goalscorers":
        return handle_goalscorers(goalscorers_path=GOALSCORERS_PATH)
    if cmd == "/scores":
        return handle_scores(card_path=CARD_PATH)
    if cmd == "/accas":
        return handle_accas()
    if cmd == "/structure":
        return handle_structure()
    if cmd == "/pm":
        return handle_pm(db_path)
    if cmd == "/settle":
        return handle_settle(text, db_path)
    if cmd == "/boost":
        return handle_boost(text)
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
    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if admin:
        print("Admin gate active: money actions restricted to user %s" % admin)
    else:
        print("WARNING: TELEGRAM_ADMIN_USER_ID unset — all chat members can confirm orders.")

    # Register the slash-command menu so the '/' button shows the available
    # commands in the Telegram client.  Best-effort: a failure here must never
    # prevent the bot from starting.
    try:
        client.set_my_commands(_TELEGRAM_COMMANDS)
        print("Telegram command menu registered (%d commands)." % len(_TELEGRAM_COMMANDS))
    except Exception as exc:
        print("WARNING: could not register Telegram commands: %s" % exc)

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

            from_user = str((message.get("from") or {}).get("id") or "")

            # 1) Photo. A `boost`-captioned photo is priced against the model
            #    (read-only: no ledger write, so it is NOT admin-gated). Any
            #    other photo is treated as a betslip screenshot -> parse + park
            #    for confirmation, which is admin-only since a ledger write
            #    follows the confirm.
            if "photo" in message or image_document_file_id(message):
                caption = message.get("caption") or ""
                is_boost_photo = bool(re.search(r"\bboost\b", caption, re.IGNORECASE))
                if is_boost_photo:
                    try:
                        image = client.download_photo(message)
                        reply = (
                            handle_boost_photo(image)
                            if image
                            else "No photo found in that message."
                        )
                    except TelegramError as exc:
                        reply = "Couldn't download the image: %s" % exc
                    except Exception as exc:
                        reply = "Error reading image: %s" % exc
                elif not _is_admin(from_user, admin):
                    reply = READ_ONLY_MSG
                else:
                    try:
                        image = client.download_photo(message)
                        reply = (
                            handle_photo(image, chat_id, caption=caption)
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

            # 1b) /restart — privileged + side-effecting, so handled here (not in
            #     read-only dispatch()): we authenticate the sender, reply, THEN
            #     restart, so the "♻️ Restarting…" message is delivered first.
            if _is_restart_command(text):
                reply, do_restart = handle_restart(
                    text, is_admin=_is_admin(from_user, admin)
                )
                try:
                    client.send_message(chat_id, reply)
                except TelegramError as exc:
                    print("send error: %s" % exc)
                if do_restart:
                    print("/restart by user %s — restarting" % from_user)
                    try:
                        perform_restart()  # never returns on success
                    except SystemExit:
                        raise  # clean exit -> launchd KeepAlive respawns us
                    except Exception as exc:  # exec failed AND no supervisor
                        try:
                            client.send_message(
                                chat_id,
                                "⚠️ Restart failed (%s). Restart me manually on "
                                "the mini: `launchctl kickstart -k "
                                "gui/$(id -u)/com.wca.bot`." % exc,
                            )
                        except TelegramError:
                            pass
                continue

            # 2) Money-touching text (yes/no betslip confirms, Y/N BET-/PM-
            #    order confirms) is admin-gated; everything else is read-only
            #    and available to any authorized chat member.
            reply = None
            is_money = _is_money_action(text)
            if is_money and not _is_admin(from_user, admin):
                reply = READ_ONLY_MSG
            if reply is None:
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
