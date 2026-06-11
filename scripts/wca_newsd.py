"""World Cup Alpha news / injury / lineup alert daemon.

The motivating miss: the Wataru Endo withdrawal (Japan captain ruled out) was
tradable for *hours* because the market hadn't moved — but it was only caught
because a friend texted. This daemon is the systematic version. Every
``--interval`` it:

  1. Determines **teams of interest** = teams with a fixture kicking off within
     ``--horizon`` hours (from :func:`wca.linemove.robust_event_meta`) PLUS
     every team named in an *open* ledger bet.
  2. Fetches the core RSS sources + per-team Google-News queries for those
     teams, scoring each item for relevance (injury / ruled-out / suspension /
     referee / lineup).
  3. Dedupes against ``news_items`` and inserts the new ones.
  4. For new items with ``score >= --min-score`` and ``pushed = 0``, sends a
     Telegram alert via :class:`wca.bot.telegram.TelegramClient` to the news
     chat, **with current odds context** so the alert is immediately
     actionable, then marks them pushed. Pushes are capped at
     ``--max-per-cycle`` (highest scores first; the overflow is logged).

Design rules (match the other daemons):

* Per-source failures are logged and skipped — one dead feed never kills a
  cycle (handled inside :func:`wca.news.gather_items`).
* ``--once`` runs a single cycle and exits (cron / tests).
* Clean SIGTERM / Ctrl-C shutdown; PYTHONUNBUFFERED-friendly ``print`` logging.
* **Never push during pytest** — a ``PYTEST_CURRENT_TEST`` guard mirrors
  :mod:`wca.sync`, so a test cycle computes and inserts but never hits Telegram.

Honesty note: there is **no Twitter / X source** — X has no free API and we do
not scrape or fake it. Google News RSS surfaces the *reporting* of breaking
tweets (an outlet writing up the scoop) usually within minutes, which is what
we act on. See :mod:`wca.news` for the full source rationale.

Chat target resolution (push to the admin chat only):
    ``WCA_NEWS_CHAT_ID`` -> else ``TELEGRAM_ADMIN_USER_ID`` -> else the first id
    in ``TELEGRAM_CHAT_ID`` (comma-separated). The token is never logged.

Usage::

    python scripts/wca_newsd.py                  # loop forever
    python scripts/wca_newsd.py --once            # single cycle (cron/test)
    python scripts/wca_newsd.py --min-score 5 --horizon 48 --max-per-cycle 3
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

# Make ``src`` importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_SNAP_DIR = str(_REPO_ROOT / "data" / "raw" / "snapshots")

# Flag flipped by the signal handler so the loop can break cleanly.
_STOP = {"requested": False}


# ---------------------------------------------------------------------------
# small infra (mirrors the snapshot daemon)
# ---------------------------------------------------------------------------


# A Telegram bot token looks like ``123456789:AA...`` and the Bot API embeds it
# in the request URL (``api.telegram.org/bot<token>/sendMessage``). A
# ``requests`` transport error stringifies that URL, so an un-redacted log of the
# exception would leak the token. We scrub any bot-token-shaped substring and any
# ``/bot<token>/`` URL segment before printing — defence-in-depth at the daemon's
# only logging boundary, regardless of what the underlying client puts in its
# exception text.
import re as _re

_TOKEN_RE = _re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}")
_BOT_URL_RE = _re.compile(r"/bot\d{6,}:[A-Za-z0-9_\-]{20,}")


def _redact(msg: str) -> str:
    s = _BOT_URL_RE.sub("/bot<redacted>", str(msg))
    return _TOKEN_RE.sub("<redacted>", s)


def _log(msg: str) -> None:
    print("[newsd] %s" % _redact(msg), flush=True)


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no python-dotenv dependency)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# teams of interest
# ---------------------------------------------------------------------------


def teams_from_meta(event_meta: Dict[str, Dict[str, Any]], horizon_h: float,
                    now: Optional[datetime] = None) -> Set[str]:
    """Teams with a fixture kicking off within *horizon_h* hours of *now*.

    Past matches still inside their (short) live window count too: a kickoff up
    to ~2.5h ago is still 'in play' and absolutely news-relevant (lineups,
    in-game injuries). We include anything from ``now - 2.5h`` to
    ``now + horizon``.
    """
    now = now or _now()
    lo = now - timedelta(hours=2.5)
    hi = now + timedelta(hours=horizon_h)
    teams: Set[str] = set()
    for meta in event_meta.values():
        ko = _parse_iso(meta.get("kickoff", ""))
        if ko is None:
            # No kickoff parseable: be conservative and include it.
            for side in ("home", "away"):
                if meta.get(side):
                    teams.add(meta[side])
            continue
        if lo <= ko <= hi:
            for side in ("home", "away"):
                if meta.get(side):
                    teams.add(meta[side])
    return teams


def teams_from_open_bets(db_path: str) -> Set[str]:
    """Every WC team named in an *open* ledger bet (match_desc + selection).

    Reads the ``bets`` table directly (status='open') and matches the free-text
    ``match_desc`` / ``selection`` against the 48 WC2026 squads. We can't trust
    a structured team column to exist, so we scan the descriptions for any
    known team name (alias-aware via :func:`wca.news.teams_in_text`).
    """
    import sqlite3

    from wca import news
    from wca.advancement import WC2026_GROUPS

    all_teams: List[str] = [t for group in WC2026_GROUPS.values() for t in group]
    found: Set[str] = set()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return found
    try:
        try:
            rows = conn.execute(
                "SELECT match_desc, selection FROM bets WHERE status = 'open'"
            ).fetchall()
        except sqlite3.Error:
            return found
        for r in rows:
            blob = "%s %s" % (r["match_desc"] or "", r["selection"] or "")
            for t in news.teams_in_text(blob, all_teams):
                found.add(t)
    finally:
        conn.close()
    return found


def compute_teams_of_interest(db_path: str, horizon_h: float,
                              now: Optional[datetime] = None) -> List[str]:
    """Union of upcoming-fixture teams and open-bet teams, sorted for stable logs."""
    from wca import linemove

    try:
        meta = linemove.robust_event_meta(_SNAP_DIR)
    except Exception as exc:  # noqa: BLE001
        _log("event-meta load failed (%s); falling back to open bets only" % exc)
        meta = {}
    teams = set(teams_from_meta(meta, horizon_h, now=now))
    teams |= teams_from_open_bets(db_path)
    return sorted(teams)


# ---------------------------------------------------------------------------
# chat target
# ---------------------------------------------------------------------------


def resolve_chat_id() -> Optional[str]:
    """The news-alert chat id (admin only). Token is never read/logged here."""
    cid = os.environ.get("WCA_NEWS_CHAT_ID")
    if cid:
        return cid.strip()
    cid = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if cid:
        return cid.strip()
    multi = os.environ.get("TELEGRAM_CHAT_ID", "")
    first = multi.split(",")[0].strip() if multi else ""
    return first or None


# ---------------------------------------------------------------------------
# one cycle
# ---------------------------------------------------------------------------


def run_cycle(
    db_path: str,
    min_score: int,
    horizon_h: float,
    max_per_cycle: int,
    client: Any = None,
    chat_id: Optional[str] = None,
    fetch=None,
    now: Optional[datetime] = None,
    max_age_hours: Optional[float] = None,
) -> Dict[str, int]:
    """Execute one full scan/score/insert/push cycle.

    Returns a small stats dict for logging/tests: ``{"teams", "fetched",
    "new", "eligible", "pushed", "overflow", "stale"}``. Telegram is *not*
    contacted when ``PYTEST_CURRENT_TEST`` is set (test guard) — items are still
    scored and inserted, and ``pushed`` reflects what *would* have been sent
    only insofar as a client is actually invoked.

    ``max_age_hours`` (when set) suppresses *pushing* items whose ``published``
    date is older than that window — Google News returns a long backlog, and we
    only want to ping the phone about *fresh* news. Stale items are still
    inserted (so they dedupe and never re-trigger) but counted under ``stale``.
    Items with an unparseable/missing date are treated as fresh (fail-open: we'd
    rather over-alert than miss a scoop with a malformed timestamp).

    Parameters are injectable so tests drive deterministic engine functions:
    ``fetch`` overrides the per-feed fetch; ``client`` is a stub TelegramClient;
    ``chat_id`` overrides env resolution.
    """
    from wca import linemove, news

    stats = {"teams": 0, "fetched": 0, "new": 0, "eligible": 0,
             "pushed": 0, "overflow": 0, "stale": 0}

    teams = compute_teams_of_interest(db_path, horizon_h, now=now)
    stats["teams"] = len(teams)
    if not teams:
        _log("no teams of interest this cycle (no upcoming fixtures, no open bets)")
        return stats

    # Gather (per-source failures isolated inside gather_items).
    gather_kwargs = {}
    if fetch is not None:
        gather_kwargs["fetch"] = fetch
    items = news.gather_items(teams, **gather_kwargs)
    stats["fetched"] = len(items)

    # Score everything; tag teams onto each item from its text too.
    scores: Dict[str, int] = {}
    for it in items:
        named = news.teams_in_text("%s %s" % (it.title, it.summary), teams)
        for t in named:
            if t not in it.teams:
                it.teams.append(t)
        scores[it.uid] = news.score_item(it, teams)

    # Dedupe + insert.
    conn = news.connect(db_path)
    try:
        fresh = news.new_items(conn, items, scores=scores)
        stats["new"] = len(fresh)

        # Eligible = new AND score>=min AND not pushed AND recent enough.
        cutoff = None
        if max_age_hours is not None:
            cutoff = (now or _now()) - timedelta(hours=max_age_hours)
        eligible = []
        for r in fresh:
            if int(r["score"]) < min_score or int(r["pushed"]) != 0:
                continue
            if cutoff is not None:
                pub = news.parse_published(r["published"] or "")
                if pub is not None and pub < cutoff:
                    stats["stale"] += 1
                    continue  # too old to be actionable; already stored
            eligible.append(r)
        eligible.sort(key=lambda r: int(r["score"]), reverse=True)
        stats["eligible"] = len(eligible)

        if len(eligible) > max_per_cycle:
            overflow = eligible[max_per_cycle:]
            stats["overflow"] = len(overflow)
            _log(
                "%d eligible items exceed cap %d; deferring %d (will re-alert "
                "next cycle): %s"
                % (
                    len(eligible),
                    max_per_cycle,
                    len(overflow),
                    "; ".join("[s%d] %s" % (int(r["score"]), (r["title"] or "")[:60])
                              for r in overflow),
                )
            )
        to_push = eligible[:max_per_cycle]

        # Resolve odds-context meta once.
        try:
            event_meta = linemove.robust_event_meta(_SNAP_DIR)
        except Exception:  # noqa: BLE001
            event_meta = {}

        # HARD GUARD: never contact Telegram during a test run. Mirrors
        # wca.sync.push_site — a test exercising this path must not ping the
        # real phone. We still mark items so the test can assert the bookkeeping.
        under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))

        if not under_pytest:
            if client is None:
                client = _build_client()
            if chat_id is None:
                chat_id = resolve_chat_id()

        pushed_uids: List[str] = []
        for r in to_push:
            uid = r["uid"]
            item = news.NewsItem(
                title=r["title"] or "",
                link=r["link"] or "",
                source=r["source"] or "",
                summary=r["summary"] or "",
                published=r["published"] or "",
                teams=[t for t in (r["teams"] or "").split(",") if t],
            )
            odds = _odds_for_item(db_path, item, event_meta)
            text = news.format_alert(item, int(r["score"]), odds)

            if under_pytest:
                # Compute everything (so format_alert/odds_context are exercised)
                # but never send. Do NOT mark pushed: nothing actually went out.
                continue
            if client is None or not chat_id:
                _log("no telegram client/chat configured; skipping send of %s" % uid)
                continue
            try:
                client.send_message(chat_id, text)
                pushed_uids.append(uid)
            except Exception as exc:  # noqa: BLE001 - one bad send != dead cycle
                _log("send failed for %s (%s); will retry next cycle" % (uid, exc))

        if pushed_uids:
            news.mark_pushed(conn, pushed_uids)
        stats["pushed"] = len(pushed_uids)
    finally:
        conn.close()

    _log(
        "cycle done: teams=%d fetched=%d new=%d eligible=%d pushed=%d "
        "overflow=%d stale=%d"
        % (stats["teams"], stats["fetched"], stats["new"], stats["eligible"],
           stats["pushed"], stats["overflow"], stats["stale"])
    )
    return stats


def _odds_for_item(db_path: str, item: Any, event_meta: Dict[str, Dict[str, Any]]):
    """Best-effort odds context for the team the story is actually *about*.

    ``item.teams`` mixes two kinds of tag: teams whose name appears in the
    headline/summary *text*, and teams that are merely the Google-News *query*
    that surfaced the story (Google's search is fuzzy — a Japan-withdrawal story
    can show up in the United-States query feed). Pairing a Japan headline with
    the USA's odds line would be misleading, so we prefer teams actually *named
    in the text* and only fall back to the query-tag teams if the text names
    none. Within each bucket we keep the tagged order. Best-effort: any odds
    lookup error for one team is skipped, not raised.
    """
    from wca import news

    text = "%s %s" % (getattr(item, "title", "") or "", getattr(item, "summary", "") or "")
    named = set(news.teams_in_text(text, item.teams or []))
    # Text-named teams first (the story's real subject), then the rest (query
    # tags), de-duplicated while preserving order.
    ordered: List[str] = []
    for team in list(item.teams or []):
        if team in named and team not in ordered:
            ordered.append(team)
    for team in list(item.teams or []):
        if team not in ordered:
            ordered.append(team)

    for team in ordered:
        try:
            ctx = news.odds_context(db_path, team, event_meta)
        except Exception:  # noqa: BLE001 - odds context is best-effort
            ctx = None
        if ctx:
            return ctx
    return None


def _build_client():
    """Construct a TelegramClient from env, or None if no token configured."""
    try:
        from wca.bot.telegram import TelegramClient

        return TelegramClient()
    except Exception as exc:  # noqa: BLE001 - missing token, etc.
        _log("telegram client unavailable (%s); alerts will not be sent" % exc)
        return None


# ---------------------------------------------------------------------------
# loop / signals / main
# ---------------------------------------------------------------------------


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):  # noqa: ANN001
        _log("received signal %s; shutting down after current sleep" % signum)
        _STOP["requested"] = True

    signal.signal(signal.SIGTERM, _handler)
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):  # pragma: no cover - non-main thread
        pass


def _interruptible_sleep(seconds: float) -> None:
    remaining = float(seconds)
    while remaining > 0 and not _STOP["requested"]:
        slice_s = min(1.0, remaining)
        time.sleep(slice_s)
        remaining -= slice_s


def _startup_line(db_path: str, args) -> None:
    """Document the configuration on startup (chat id only, never the token)."""
    from wca import news

    teams = compute_teams_of_interest(db_path, args.horizon)
    n_sources = len(news.SOURCES) + len(news.google_news_queries(teams))
    chat = resolve_chat_id()
    chat_disp = chat if chat else "<none configured>"
    _log(
        "starting: sources=%d (%d core + %d google-news) | teams_of_interest=%d %s "
        "| min_score=%d | horizon=%gh | interval=%gs | max_per_cycle=%d "
        "| chat=%s | NOTE: no Twitter/X source (no free API; Google News carries the reporting)"
        % (
            n_sources,
            len(news.SOURCES),
            len(news.google_news_queries(teams)),
            len(teams),
            "[" + ", ".join(teams[:8]) + ("…" if len(teams) > 8 else "") + "]",
            args.min_score,
            args.horizon,
            args.interval,
            args.max_per_cycle,
            chat_disp,
        )
    )


def run(db_path: str, args) -> None:
    _install_signal_handlers()
    _startup_line(db_path, args)
    try:
        while True:
            try:
                run_cycle(
                    db_path,
                    min_score=args.min_score,
                    horizon_h=args.horizon,
                    max_per_cycle=args.max_per_cycle,
                    max_age_hours=args.max_age_hours,
                )
            except Exception as exc:  # noqa: BLE001 - a cycle error != dead daemon
                _log("cycle error (continuing): %s" % exc)
            if args.once or _STOP["requested"]:
                break
            _interruptible_sleep(args.interval)
            if _STOP["requested"]:
                break
    except KeyboardInterrupt:  # pragma: no cover - defensive
        _log("interrupted; exiting cleanly")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="World Cup Alpha news alert daemon")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger/news path")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    parser.add_argument("--interval", type=float, default=600.0,
                        help="seconds between cycles (default 600)")
    parser.add_argument("--horizon", type=float, default=72.0,
                        help="hours ahead a fixture counts as 'of interest' (default 72)")
    parser.add_argument("--min-score", type=int, default=4,
                        help="minimum relevance score to push an alert (default 4)")
    parser.add_argument("--max-per-cycle", type=int, default=5,
                        help="max alerts pushed per cycle, highest score first (default 5)")
    parser.add_argument("--max-age-hours", type=float, default=36.0,
                        help="suppress pushing items published older than this many "
                             "hours (Google News returns a long backlog); items with "
                             "no/unparseable date are treated as fresh (default 36)")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (cron / testing)")
    args = parser.parse_args(argv)

    _load_dotenv(args.env)
    run(db_path=args.db, args=args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
