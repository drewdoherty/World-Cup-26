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

import csv
import glob
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from wca import cardcache
from wca.bot.telegram import (
    TelegramClient,
    TelegramError,
    image_document_file_id,
)
from wca.ledger import reports
from wca.ledger.store import record_bet

logger = logging.getLogger(__name__)

CARD_PATH = "data/card_latest.md"
NEXT_PATH = "data/next_latest.md"
SCORES_FEED_PATH = "site/scores_data.json"
RESULTS_PATH = "data/processed/wc2026_results.json"
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

HELP_TEXT = (
    "*World Cup Alpha* — operator console\n\n"
    "Core commands:\n"
    "/today — live operator brief: next match, active exposure, accas\n"
    "/open or /bets — active open exposure only; FT fixtures are hidden\n"
    "/scores — active predicted FT scorelines; hides FT fixtures\n/goalscorers — next-match anytime scorer prices\n/accas — promo accas / bet builders from current odds feed\n"
    "/boost — price a bookmaker boost vs the model\n"
    "/pm — Polymarket parked orders + trader status\n"
    "/summary — portfolio P&L, ROI, CLV, bankroll by pool\n"
    "/settle — settle a bet: `/settle <id> <won|lost|void> [closing-odds]`\n"
    "/ping — liveness check\n\n"
    "Quiet/debug commands still work: /next, /card, /clv, /structure.\n\n"
    "\U0001F4F8 Send a betslip *screenshot* and I'll parse the selections, then "
    "log them once you reply `yes`. Use tags: `a2`, `offer`, `punt`, `model`.\n"
    "⚡ Caption a screenshot with `boost`, or type: "
    "`/boost <site> | <match> | <market> | <selection> | <odds> [was <odds>] [inplay]`\n"
    "Confirm a pushed sportsbook bet with `Y BET-<id>`; execute a parked Polymarket order with `Y PM-<n>`."
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
    "You can use /today, /next, /scores, /card, /summary, /bets, /accas, /pm, /ping."
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



def _repo_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


def _resolve_repo_path(path: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(_repo_root(), path)


def _fixture_tokens(name: Any) -> Optional[tuple]:
    if not isinstance(name, str):
        return None
    text = name.strip()
    lowered = text.lower()
    for sep in (" vs ", " v "):
        if sep in lowered:
            idx = lowered.find(sep)
            left_raw = text[:idx].strip()
            right_raw = text[idx + len(sep):].strip()
            try:
                from wca.data import teamnames
                left_raw = teamnames.canonical(left_raw)
                right_raw = teamnames.canonical(right_raw)
            except Exception:
                pass
            left = re.sub(r"[^a-z0-9]+", " ", str(left_raw).lower()).strip()
            right = re.sub(r"[^a-z0-9]+", " ", str(right_raw).lower()).strip()
            if left and right:
                return left, right
    return None


def _finished_fixture_tokens(results_path: str = RESULTS_PATH) -> List[tuple[str, str]]:
    try:
        with open(_resolve_repo_path(results_path), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return []
    rows = payload.get("results") if isinstance(payload, dict) else payload
    out: List[tuple[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not row.get("score") or row.get("outcome") in (None, "pending"):
            continue
        toks = _fixture_tokens(str(row.get("fixture") or ""))
        if toks is not None:
            out.append(toks)
    return out


def _matches_finished_fixture(match_desc: Any, finished: List[tuple[str, str]]) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(match_desc or "").lower()).strip()
    if not text:
        return False
    for home, away in finished:
        if home in text and away in text:
            return True
    return False


def _is_match_specific_market(market: Any) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(market or "").lower()).strip()
    if not text:
        return False
    needles = (
        "match result",
        "correct score",
        "asian handicap",
        "handicap",
        "bet builder",
        "acca",
        "both teams",
        "btts",
        "over under",
        "total goals",
        "goalscorer",
        "shots on target",
        "cards",
        "corners",
    )
    return any(n in text for n in needles)


def _is_malformed_match_position(row: Any) -> bool:
    match = str(row["match_desc"] or "").strip()
    if _fixture_tokens(match):
        return False
    if match and match not in {"-", "—", "unknown", "UNKNOWN"}:
        return False
    return _is_match_specific_market(row["market"])


# ---------------------------------------------------------------------------
# Command handlers — each returns the reply text.
# ---------------------------------------------------------------------------


def _canon_platform(raw: str) -> str:
    """Normalise a bookmaker name from a screenshot to the canonical DB string.

    Betfair Exchange (betfair_ex_uk, "Betfair Exchange", bare "Betfair") -> "Betfair"
    Betfair Sportsbook ("Betfair Sportsbook", betfair_sportsbook)        -> "Betfair Sportsbook"
    Everything else: title-case the raw value.
    """
    p = (raw or "").strip()
    pl = p.lower()
    # Exchange variants — bare "Betfair" or anything mentioning "exchange"
    if pl in ("betfair", "betfair_ex_uk", "betfair_ex_eu", "betfair exchange", "betfair ex"):
        return "Betfair"
    if "betfair" in pl and "exchange" in pl:
        return "Betfair"
    # Sportsbook variants
    if pl in ("betfair_sportsbook", "betfair sportsbook", "betfair sports"):
        return "Betfair Sportsbook"
    if "betfair" in pl and ("sports" in pl or "sb" in pl):
        return "Betfair Sportsbook"
    # Other known normalizations
    _MAP = {
        "paddy power": "Paddy Power",
        "paddypower": "Paddy Power",
        "skybet": "Sky Bet",
        "sky bet": "Sky Bet",
        "virgin bet": "Virgin Bet",
        "virginbet": "Virgin Bet",
        "bet 365": "bet365",
        "betfair": "Betfair",  # fallback bare match (already caught above but safety)
    }
    if pl in _MAP:
        return _MAP[pl]
    # Return as-is (title-case if all-lower or all-slug)
    return p if any(c.isupper() for c in p) else p.title().replace("_", " ")


def _venue_of(platform: str) -> str:
    """Map a ledger ``platform`` string to its venue class for pooling/symbols.

    Restored 2026-06-18 (Phase-0 audit): the callers in :func:`_pool_rows`
    (line ~245) and :func:`handle_bets` (line ~352) referenced this helper but
    the definition had been dropped in a refactor, crashing ``/bets`` with a
    ``NameError`` on any open bet and silently voiding the per-venue GBP/USD
    split in ``/summary``. Keys line up with :data:`_VENUE_SYMBOL`.
    """
    p = (platform or "").lower()
    if "polymarket" in p:
        return "polymarket"
    if "kalshi" in p:
        return "kalshi"
    return "sportsbook"


_VENUE_SYMBOL = {"sportsbook": "£", "polymarket": "$", "polymarket-auto": "$", "kalshi": "$"}


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
    for v in ("sportsbook", "polymarket", "polymarket-auto", "kalshi"):
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


def handle_bets(db_path: str, results_path: str = RESULTS_PATH) -> str:
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

    finished = _finished_fixture_tokens(results_path)
    stale_rows = [r for r in rows if _matches_finished_fixture(r["match_desc"], finished)]
    malformed_rows = [r for r in rows if _is_malformed_match_position(r)]
    rows = [
        r for r in rows
        if not _matches_finished_fixture(r["match_desc"], finished)
        and not _is_malformed_match_position(r)
    ]

    if not rows:
        lines = ["\U0001f3af *Active open bets*", "None — no pre-match/in-play exposure."]
        if stale_rows:
            lines.extend(["", "⚠️ *Needs settlement* (FT fixtures hidden from active exposure)"])
            for r in stale_rows[:12]:
                lines.append("#%d %s — %s @ %.2f" % (
                    r["id"], r["match_desc"], r["selection"], float(r["decimal_odds"] or 0.0),
                ))
        if malformed_rows:
            lines.extend(["", "⚠️ *Needs cleanup* (malformed match rows hidden from active exposure)"])
            for r in malformed_rows[:12]:
                lines.append("#%d %s — %s @ %.2f" % (
                    r["id"], r["match_desc"], r["selection"], float(r["decimal_odds"] or 0.0),
                ))
        return "\n".join(lines)

    by_venue: Dict[str, List[Any]] = {}
    for r in rows:
        by_venue.setdefault(_venue_of(r["platform"]), []).append(r)

    title = "\U0001f3af *Active open bets* (%d)" % len(rows)
    if stale_rows:
        title += " — %d FT hidden" % len(stale_rows)
    if malformed_rows:
        title += " — %d malformed hidden" % len(malformed_rows)
    lines = [title]
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
    if stale_rows:
        lines.append("")
        lines.append("⚠️ %d open row(s) are for FT fixtures and are hidden here; settle them with /settle." % len(stale_rows))
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


def _format_score_fixtures(
    fixtures: List[Dict[str, Any]],
    generated: str = "",
    now_utc: Optional[str] = None,
    stale_label: str = "scores feed",
) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    header = "⚽ *Predicted scores* — %s" % generated if generated else "⚽ *Predicted scores*"
    banner = _stale_banner(generated, now_utc, SCORES_FEED_MAX_AGE_HOURS, label=stale_label)
    lines = [banner + header if banner else header, ""]
    for fx in fixtures:
        scores = fx.get("scores") or []
        if not scores:
            continue
        top = scores[0]
        top_str = "*%s* (%s%%)" % (top.get("score"), _fmt_prob(top.get("prob")))
        runner_strs = [
            "%s %s%%" % (r.get("score"), _fmt_prob(r.get("prob")))
            for r in scores[1:5]
        ]
        fixture_line = "*%s*: %s" % (fx.get("fixture"), top_str)
        if runner_strs:
            fixture_line += "  | " + " | ".join(runner_strs)
        lines.append(fixture_line)
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
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _active_score_fixtures_from_feed(scores_path: str = SCORES_FEED_PATH) -> Optional[tuple[str, List[Dict[str, Any]]]]:
    try:
        with open(_resolve_repo_path(scores_path), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    generated = (payload.get("meta") or {}).get("generated") or ""
    finished = _finished_fixture_tokens()
    fixtures = [
        fx for fx in (payload.get("fixtures") or [])
        if not _matches_finished_fixture(fx.get("fixture"), finished)
    ]
    return generated, fixtures


def handle_scores(
    card_path: str = CARD_PATH,
    now_utc: Optional[str] = None,
    scores_path: str = SCORES_FEED_PATH,
) -> str:
    """Return active predicted full-time scorelines.

    Prefer the lightweight scores feed because it carries current market venues
    and can be regenerated without serving the whole raw card. Completed FT
    fixtures are hidden so the command never suggests dead markets. Falls back
    to the card parser only when the feed is absent.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    use_scores_feed = os.path.normpath(str(card_path)) == os.path.normpath("data/card_latest.md")
    if use_scores_feed:
        feed = _active_score_fixtures_from_feed(scores_path)
        if feed is not None:
            generated, fixtures = feed
            if fixtures:
                return _format_score_fixtures(fixtures, generated, now_utc, "scores feed")
            return (
                "⚽ *Predicted scores*\n"
                "No active scoreline feed right now — the cached fixtures are all FT. "
                "Run the local card/scores refresh before using this for new bets."
            )

    cached = cardcache.read_card(card_path, now_utc=now_utc, max_age_hours=CARD_MAX_AGE_HOURS)
    if cached is None:
        return (
            "*Predicted scores*\n"
            "No card cached and no scores feed found yet. Run the local refresh build workflow."
        )

    from wca.sitedata import parse_scorelines

    generated = cached.get("generated") or ""
    fixtures = parse_scorelines(cached.get("text") or "")
    if os.path.normpath(str(card_path)) == os.path.normpath("data/card_latest.md"):
        finished = _finished_fixture_tokens()
        fixtures = [
            fx for fx in fixtures
            if not _matches_finished_fixture(fx.get("fixture"), finished)
        ]
        if not fixtures:
            return "⚽ *Predicted scores*\nNo active scorelines in the current card — cached fixtures are FT."
    elif not fixtures:
        return "⚽ *Predicted scores*\nNo scorelines section found in the supplied card."
    return _format_score_fixtures(fixtures, generated, now_utc, "card")


def handle_next(
    next_path: str = NEXT_PATH,
    now_utc: Optional[str] = None,
) -> str:
    """Serve the cached next-match preview written by ``scripts/wca_build_card.py``.

    The preview (blended winner probs, corners model, market anytime scorers,
    reconciled scoreline distribution) is built on cron alongside the main
    card; this handler only reads the cache and flags staleness.
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
    body = cached.get("text") or "(empty preview)"
    # A stale next-match cache is actively dangerous once that fixture is FT:
    # refuse it instead of showing dead goalscorer/scoreline markets.
    first_line = next((ln for ln in body.splitlines() if ln.startswith("⚽ *Next match*")), "")
    fixture = first_line.split("—", 1)[1].strip() if "—" in first_line else ""
    if fixture and _matches_finished_fixture(fixture, _finished_fixture_tokens()):
        return (
            "⚽ *Next match*\n"
            "No active next-match cache — the cached fixture is FT. "
            "Run the local card/next refresh before using scorer or scoreline markets."
        )
    if cached.get("stale"):
        body = "⚠️ STALE (generated %s UTC)\n\n%s" % (cached.get("generated"), body)
    return body


def _score_to_pair(score: Any) -> Optional[tuple[int, int]]:
    text = str(score or "").strip()
    if "-" not in text:
        return None
    left, _, right = text.partition("-")
    try:
        return int(left.strip()), int(right.strip())
    except ValueError:
        return None


def _poisson_over_25_lambda(target_over: float) -> Optional[float]:
    """Combined-goals lambda whose Poisson P(total >= 3) matches target."""
    if not 0.0 < target_over < 1.0:
        return None

    def over25(lam: float) -> float:
        return 1.0 - math.exp(-lam) * (1.0 + lam + (lam * lam / 2.0))

    lo, hi = 0.01, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if over25(mid) < target_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _score_fixture_lambdas(fx: Dict[str, Any]) -> tuple[float, float]:
    """Approximate team xG from listed scorelines plus O/U 2.5."""
    w_sum = h_sum = a_sum = 0.0
    for row in fx.get("scores") or []:
        pair = _score_to_pair(row.get("score") if isinstance(row, dict) else None)
        if pair is None:
            continue
        try:
            w = float(row.get("prob")) / 100.0
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        h, a = pair
        w_sum += w
        h_sum += h * w
        a_sum += a * w
    listed_h = h_sum / w_sum if w_sum > 0 else 0.0
    listed_a = a_sum / w_sum if w_sum > 0 else 0.0
    ratio = listed_h / (listed_h + listed_a) if (listed_h + listed_a) > 0 else 0.5
    if (listed_h + listed_a) <= 0:
        model = fx.get("model_1x2") or {}
        try:
            ph = float(model.get("home") or 0.0)
            pa = float(model.get("away") or 0.0)
            if ph > 0 or pa > 0:
                ratio = min(max(0.5 + 0.35 * (ph - pa), 0.25), 0.75)
        except (TypeError, ValueError):
            pass

    total = listed_h + listed_a
    ou = fx.get("over_under") or {}
    try:
        line = float(ou.get("line", 2.5))
        over = float(ou.get("over")) / 100.0
    except (TypeError, ValueError):
        line, over = 2.5, 0.0
    if abs(line - 2.5) < 1e-9:
        inferred = _poisson_over_25_lambda(over)
        if inferred is not None:
            total = inferred
    if total <= 0:
        total = 2.4
    return max(total * ratio, 0.05), max(total * (1.0 - ratio), 0.05)


def _median(xs: List[float]) -> Optional[float]:
    vals = sorted(x for x in xs if x > 0)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _parse_utc_datetime(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_snapshot_score_fixture(
    now_utc: Optional[str] = None,
    pattern: str = "data/raw/snapshots/oddsapi_multi_uk_*.json",
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Build a score-feed-like fixture from the latest raw multi-market snapshot."""
    paths = sorted(glob.glob(_resolve_repo_path(pattern)))
    if not paths:
        return None
    path = paths[-1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    now = _parse_utc_datetime(now_utc) if now_utc else datetime.now(timezone.utc)
    min_dt = now - timedelta(hours=2)
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("event_id") or row.get("id") or "")
        if not eid:
            continue
        by_event.setdefault(eid, []).append(row)

    candidates: List[tuple[datetime, Dict[str, Any]]] = []
    for event_rows in by_event.values():
        first = event_rows[0]
        kickoff = _parse_utc_datetime(first.get("commence_time"))
        if kickoff is None or kickoff < min_dt:
            continue
        home = str(first.get("home_team") or "").strip()
        away = str(first.get("away_team") or "").strip()
        if not home or not away:
            continue

        implied = {"home": [], "draw": [], "away": []}  # type: Dict[str, List[float]]
        over_imps: List[float] = []
        under_imps: List[float] = []
        for row in event_rows:
            try:
                odds = float(row.get("decimal_odds") or 0.0)
            except (TypeError, ValueError):
                continue
            if odds <= 1.0:
                continue
            market = str(row.get("market") or "")
            name = str(row.get("outcome_name") or "")
            if market == "h2h":
                key = None
                if _canon_team_for_match(name) == _canon_team_for_match(home):
                    key = "home"
                elif _canon_team_for_match(name) == _canon_team_for_match(away):
                    key = "away"
                elif name.lower() == "draw":
                    key = "draw"
                if key:
                    implied[key].append(1.0 / odds)
            elif market == "totals":
                try:
                    point = float(row.get("outcome_point"))
                except (TypeError, ValueError):
                    continue
                if abs(point - 2.5) > 1e-9:
                    continue
                if name.lower() == "over":
                    over_imps.append(1.0 / odds)
                elif name.lower() == "under":
                    under_imps.append(1.0 / odds)

        med = {k: _median(v) for k, v in implied.items()}
        h = med.get("home") or 0.0
        d = med.get("draw") or 0.0
        a = med.get("away") or 0.0
        s = h + d + a
        if s > 0:
            model_1x2 = {"home": h / s, "draw": d / s, "away": a / s}
        else:
            model_1x2 = {"home": 0.5, "draw": 0.25, "away": 0.25}

        over_med = _median(over_imps)
        under_med = _median(under_imps)
        if over_med is not None and under_med is not None and (over_med + under_med) > 0:
            over_pct = 100.0 * over_med / (over_med + under_med)
        else:
            over_pct = 50.0
        fx = {
            "fixture": "%s vs %s" % (home, away),
            "kickoff": kickoff.isoformat(),
            "scores": [],
            "model_1x2": model_1x2,
            "over_under": {"line": 2.5, "over": over_pct, "under": 100.0 - over_pct},
            "btts": None,
            "_source": os.path.basename(path),
        }
        candidates.append((kickoff, fx))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1].get("_source", ""), candidates[0][1]


def _canon_team_for_match(team: str) -> str:
    try:
        from wca.data import teamnames

        return teamnames.canonical(team)
    except Exception:
        return str(team or "")


def _statsbomb_players_for_team(
    team: str,
    path: str = "data/processed/props_players.csv",
    limit: int = 8,
) -> List[Any]:
    """Empirical WC2018/2022 npxG-share player params for a team."""
    try:
        from wca.models.scorers import PlayerParams
    except Exception:
        return []
    target = _canon_team_for_match(team)
    rows: List[Dict[str, Any]] = []
    try:
        with open(_resolve_repo_path(path), "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if _canon_team_for_match(str(row.get("team") or "")) == target:
                    rows.append(row)
    except Exception:
        return []
    totals = []
    for row in rows:
        try:
            totals.append(max(float(row.get("npxg_sum") or 0.0), 0.0))
        except (TypeError, ValueError):
            totals.append(0.0)
    team_total = sum(totals)
    if team_total <= 0:
        return []
    enriched = []
    for row, npxg in zip(rows, totals):
        if npxg <= 0:
            continue
        try:
            minutes = float(row.get("minutes") or 0.0)
            matches = max(float(row.get("matches") or 1.0), 1.0)
            xg = float(row.get("xg_sum") or 0.0)
        except (TypeError, ValueError):
            minutes, matches, xg = 0.0, 1.0, npxg
        enriched.append(
            (
                npxg,
                PlayerParams(
                    name=str(row.get("player") or "").strip(),
                    team=target,
                    npxg_share=min(max(npxg / team_total, 0.0), 0.75),
                    penalty_taker=(xg - npxg) >= 0.45,
                    expected_minutes=min(max(minutes / matches, 20.0), 90.0),
                    source="statsbomb_wc18_22",
                ),
            )
        )
    enriched.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in enriched[:limit] if p.name]


def _players_for_goalscorers(
    team: str,
    players_path: str = "data/players.json",
    statsbomb_players_path: str = "data/processed/props_players.csv",
    limit: int = 8,
) -> List[Any]:
    try:
        from wca.models.scorers import players_for_team

        players = list(players_for_team(_canon_team_for_match(team), _resolve_repo_path(players_path)))
    except Exception:
        players = []
    seen = {str(getattr(p, "name", "")).lower() for p in players}
    for p in _statsbomb_players_for_team(team, statsbomb_players_path, limit=limit):
        if str(getattr(p, "name", "")).lower() in seen:
            continue
        players.append(p)
        seen.add(str(getattr(p, "name", "")).lower())
        if len(players) >= limit:
            break
    return players[:limit]


def _format_goalscorer_fallback(
    fx: Dict[str, Any],
    generated: str = "",
    now_utc: Optional[str] = None,
    players_path: str = "data/players.json",
    statsbomb_players_path: str = "data/processed/props_players.csv",
) -> str:
    from wca.models.scorers import ScorerPricer

    if now_utc is None:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    fixture = str(fx.get("fixture") or "?")
    home, away = _fixture_sides(fixture) if "_fixture_sides" in globals() else (fixture, "")
    # local helper avoids importing accas just for a tiny string split
    if home == fixture and " vs " in fixture:
        home, _, away = fixture.partition(" vs ")
    elif home == fixture and " v " in fixture:
        home, _, away = fixture.partition(" v ")
    home, away = home.strip(), away.strip()
    lam_h, lam_a = _score_fixture_lambdas(fx)
    total_lam = lam_h + lam_a
    pricer = ScorerPricer()

    banner = _stale_banner(generated, now_utc, SCORES_FEED_MAX_AGE_HOURS, label="scores feed")
    lines: List[str] = []
    if banner:
        lines.append(banner.rstrip())
        lines.append("")
    lines.extend(
        [
            "⚽ *Goalscorers — %s*" % fixture,
            "Model scorer prices from scoreline xG + StatsBomb/player overrides.",
            "No live scorer book prices in the current local feed; use fair odds as a filter, not a bet instruction.",
            "Team xG approx: %s %.2f / %s %.2f" % (home, lam_h, away, lam_a),
            "",
        ]
    )

    for team, lam in ((home, lam_h), (away, lam_a)):
        if not team:
            continue
        players = _players_for_goalscorers(team, players_path, statsbomb_players_path)
        lines.append("*%s*" % team)
        if not players:
            lines.append("  No player-level data yet.")
            lines.append("")
            continue
        priced = [pricer.price_player(p, lam, total_lam) for p in players]
        priced.sort(key=lambda x: x.p_anytime, reverse=True)
        for line in priced[:6]:
            lines.append(
                "  %-22s anytime %.1f%% fair %.2f | first %.1f%% fair %.2f"
                % (
                    line.player[:22],
                    line.p_anytime * 100.0,
                    line.fair_anytime,
                    line.p_first * 100.0,
                    line.fair_first,
                )
            )
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def handle_goalscorers(
    next_path: str = NEXT_PATH,
    now_utc: Optional[str] = None,
    scores_path: str = SCORES_FEED_PATH,
    players_path: str = "data/players.json",
    statsbomb_players_path: str = "data/processed/props_players.csv",
) -> str:
    """Return next-match scorer prices; fall back to scoreline+StatsBomb estimates."""
    text = handle_next(next_path=next_path, now_utc=now_utc)
    if "*Anytime scorer*" not in text:
        feed = _active_score_fixtures_from_feed(scores_path)
        if feed is not None and feed[1]:
            generated, fixtures = feed
            fixtures = sorted(fixtures, key=lambda f: str(f.get("kickoff") or f.get("commence_time") or ""))
            return _format_goalscorer_fallback(
                fixtures[0],
                generated,
                now_utc,
                players_path=players_path,
                statsbomb_players_path=statsbomb_players_path,
            )
        snap = _latest_snapshot_score_fixture(now_utc)
        if snap is None:
            return text
        generated, fx = snap
        return _format_goalscorer_fallback(
            fx,
            "latest raw snapshot %s" % generated,
            now_utc,
            players_path=players_path,
            statsbomb_players_path=statsbomb_players_path,
        )
    if "*Anytime scorer* — no market prices available yet." in text:
        feed = _active_score_fixtures_from_feed(scores_path)
        if feed is not None and feed[1]:
            generated, fixtures = feed
            fixtures = sorted(fixtures, key=lambda f: str(f.get("kickoff") or f.get("commence_time") or ""))
            return _format_goalscorer_fallback(
                fixtures[0],
                generated,
                now_utc,
                players_path=players_path,
                statsbomb_players_path=statsbomb_players_path,
            )
        snap = _latest_snapshot_score_fixture(now_utc)
        if snap is not None:
            generated, fx = snap
            return _format_goalscorer_fallback(
                fx,
                "latest raw snapshot %s" % generated,
                now_utc,
                players_path=players_path,
                statsbomb_players_path=statsbomb_players_path,
            )
    lines = text.splitlines()
    out: List[str] = []
    capture = False
    for line in lines:
        if line.startswith("⚽ *Next match*") or line.startswith("Kickoff "):
            out.append(line)
            continue
        if line.startswith("*Anytime scorer*"):
            capture = True
        elif capture and line.startswith("*") and line.strip():
            break
        if capture:
            out.append(line)
    return "\n".join(out).strip() or text


def _fmt_prob(prob: Optional[float]) -> str:
    """Format a probability as a compact percentage string (1 d.p.)."""
    if prob is None:
        return "?"
    return "%.1f" % prob


def _clip_section(text: str, max_lines: int) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines] + ["…"] )


def handle_today(db_path: str) -> str:
    """Compact operator brief: next match, active exposure, promo builders."""
    sections = ["🧭 *WCA today*"]
    sections.append("")
    sections.append("*Next / in-play*")
    sections.append(_clip_section(handle_next(next_path=NEXT_PATH), 12))
    sections.append("")
    sections.append("*Exposure*")
    sections.append(_clip_section(handle_bets(db_path), 18))
    sections.append("")
    sections.append("*Promos / accas*")
    sections.append(_clip_section(handle_accas(), 16))
    return "\n".join(sections)


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
    """`/accas` — promo acca / bet-builder briefs from the scores feed."""
    from wca import accas

    try:
        scores_feed = accas.load_scores_feed(scores_path)
        if os.path.normpath(str(scores_path)) == os.path.normpath("site/scores_data.json"):
            scores_feed = accas.merge_snapshot_feed(
                scores_feed,
                accas.load_latest_snapshot_feed(),
            )
            finished = _finished_fixture_tokens()
            scores_feed["fixtures"] = [
                fx for fx in (scores_feed.get("fixtures") or [])
                if isinstance(fx, dict) and not _matches_finished_fixture(fx.get("fixture"), finished)
            ]
        if not scores_feed or not scores_feed.get("fixtures"):
            return (
                "🎟 *Promo accas / bet builders*\n"
                "No scores/market feed available yet. Try again after `scores_data.json` is rebuilt."
            )

        acca_list = accas.build_accas_from_odds(scores_feed)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        generated = str((scores_feed.get("meta") or {}).get("generated") or _feed_generated(scores_path) or "")
        banner = _stale_banner(
            generated, now_utc, SCORES_FEED_MAX_AGE_HOURS,
            label="odds feed",
        )
        return banner + accas.format_accas(acca_list)
    except Exception as exc:
        return f"🎟 *Promo accas / bet builders*\nError building accas: {exc}"


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
                _canon_platform(b.bookmaker or "unknown"),
                float(b.decimal_odds or 0.0),
                float(b.stake or 0.0),
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
    if cmd == "/today":
        return handle_today(db_path)
    if cmd == "/summary":
        return handle_summary(db_path)
    if cmd in {"/open", "/bets"}:
        return handle_bets(db_path)
    if cmd == "/clv":
        return handle_clv(db_path)
    if cmd == "/card":
        return handle_card(db_path)
    if cmd == "/next":
        return handle_next(next_path=NEXT_PATH)
    if cmd == "/scores":
        return handle_scores(card_path=CARD_PATH)
    if cmd in {"/goalscorers", "/scorers"}:
        return handle_goalscorers(next_path=NEXT_PATH)
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
