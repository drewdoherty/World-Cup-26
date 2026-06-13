"""World Cup Alpha promotions monitor daemon.

The motivating chore: every matchday the books rotate price boosts, money-back
specials and acca bonuses, and a small bankroll lives or dies on catching the
genuinely +EV ones before they're pulled. Doing this by hand ("check the Offers
Hub each morning") is exactly the kind of toil this platform exists to remove.
This daemon is the systematic version. Every ``--interval`` it:

  1. Ensures the promo tables exist; on an empty catalog (or ``--seed``) seeds
     from the hand-verified recon doc so the site is useful immediately.
  2. For each site in :data:`wca.promos.SITES`, fetches the promotions hub (and a
     dedicated boosts page if the registry sets one), extracts offer-like blocks,
     reconciles them against the catalog (new / changed / removed / unchanged),
     and records an honest fetch snapshot — including ``blocked`` / ``empty``,
     which is the *expected* outcome for most Cloudflare-protected book hubs from
     a plain ``requests`` GET.
  3. If the :mod:`wca.boosts` engine is available, grades each parseable,
     not-in-play scraped boost against the model (``evaluate_boost``) and logs the
     evaluation; boosts whose price we can't parse are logged unpriceable with a
     reason, so the boost stream is honest rather than silently lossy.
  4. Pushes NEW / CHANGED promotions and newly-found +EV boosts to Telegram
     (capped at ``--max-per-cycle``, highest-edge first), then marks them pushed.

Design rules (match :mod:`scripts.wca_newsd`):

* Per-site failures are logged and skipped — one blocked hub never kills a cycle.
* ``--once`` runs a single cycle and exits (cron / CI / tests).
* Clean SIGTERM / Ctrl-C shutdown; unbuffered ``print`` logging.
* **Never push during pytest** — a ``PYTEST_CURRENT_TEST`` guard mirrors
  :mod:`scripts.wca_newsd`, so a test cycle scrapes/diffs/inserts but never hits
  Telegram. The bot token is never logged.

Honesty note: we do NOT run a headless browser or evade bot protection. Most
book promo hubs will come back ``blocked`` / ``empty`` to this scraper; that is
recorded faithfully and the day-1 catalog leans on the recon seed. See
:mod:`wca.promos` for the full rationale.

Chat target resolution (admin chat only), mirroring wca_newsd:
    ``WCA_NEWS_CHAT_ID`` -> ``TELEGRAM_ADMIN_USER_ID`` -> first of ``TELEGRAM_CHAT_ID``.

Usage::

    python scripts/wca_promosd.py                 # loop forever
    python scripts/wca_promosd.py --once          # single cycle (cron / CI)
    python scripts/wca_promosd.py --once --no-seed --max-per-cycle 3
"""
from __future__ import annotations

import argparse
import os
import re as _re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Make ``src`` importable when run as a plain script (mirror wca_newsd.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The boost engine is owned by another agent and may not exist yet. Import it
# lazily-but-eagerly here, guarded, so this daemon works/tests before it lands.
try:  # pragma: no cover - exercised by whether boosts.py exists
    from wca import boosts  # type: ignore
except Exception:  # noqa: BLE001 - module absent or import-time error
    boosts = None  # type: ignore

# Flag flipped by the signal handler so the loop can break cleanly.
_STOP = {"requested": False}


# ---------------------------------------------------------------------------
# small infra (mirrors wca_newsd)
# ---------------------------------------------------------------------------

# A Telegram bot token looks like ``123456789:AA...`` and ends up in request
# URLs; scrub any token-shaped substring before logging (defence-in-depth at the
# daemon's only logging boundary). Copied from wca_newsd for the same reason.
_TOKEN_RE = _re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{20,}")
_BOT_URL_RE = _re.compile(r"/bot\d{6,}:[A-Za-z0-9_\-]{20,}")


def _redact(msg: str) -> str:
    s = _BOT_URL_RE.sub("/bot<redacted>", str(msg))
    return _TOKEN_RE.sub("<redacted>", s)


def _log(msg: str) -> None:
    print("[promosd] %s" % _redact(msg), flush=True)


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (no python-dotenv dependency). Mirrors wca_newsd."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def resolve_chat_id() -> Optional[str]:
    """The promo-alert chat id (admin only). Token is never read/logged here."""
    cid = os.environ.get("WCA_NEWS_CHAT_ID")
    if cid:
        return cid.strip()
    cid = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if cid:
        return cid.strip()
    multi = os.environ.get("TELEGRAM_CHAT_ID", "")
    first = multi.split(",")[0].strip() if multi else ""
    return first or None


def _build_client():
    """Construct a TelegramClient from env, or None if no token configured."""
    try:
        from wca.bot.telegram import TelegramClient

        return TelegramClient()
    except Exception as exc:  # noqa: BLE001 - missing token, etc.
        _log("telegram client unavailable (%s); alerts will not be sent" % exc)
        return None


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------


def _md_escape(s: str) -> str:
    """Escape the chars that break Telegram legacy-Markdown (mirror wca.news)."""
    if not s:
        return ""
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def format_promo_alert(site: str, kind: str, promo: Dict[str, Any]) -> str:
    """Render a concise Markdown alert for a new/changed promotion."""
    head = "🎁 *PROMO* · %s · %s" % (_md_escape(site), _md_escape(kind))
    lines = [head, _md_escape((promo.get("title") or "").strip())]
    desc = (promo.get("description") or "").strip()
    if desc and desc != (promo.get("title") or "").strip():
        if len(desc) > 240:
            desc = desc[:237].rstrip() + "…"
        lines.append("_%s_" % _md_escape(desc))
    url = (promo.get("url") or "").strip()
    if url:
        lines.append("🔗 %s" % url)
    return "\n".join(lines)


def format_boost_alert(ev: Dict[str, Any]) -> str:
    """Render a concise Markdown alert for a newly-found +EV boost."""
    head = "📈 *+EV BOOST* · %s" % _md_escape(ev.get("site") or "")
    lines = [head]
    fx = (ev.get("fixture") or "").strip()
    sel = (ev.get("selection") or "").strip()
    if fx:
        lines.append("📊 %s" % _md_escape(fx))
    detail = "   *%s*" % _md_escape(sel) if sel else "   selection"
    bo = ev.get("boosted_odds")
    if bo is not None:
        detail += " @ %s" % _fmt_odds(bo)
    lines.append(detail)
    edge = ev.get("edge")
    fair = ev.get("fair_odds")
    if edge is not None:
        bits = ["edge %+.1f%%" % (float(edge) * 100.0)]
        if fair is not None:
            bits.append("fair %s" % _fmt_odds(fair))
        lines.append("   %s" % " | ".join(bits))
    return "\n".join(lines)


def _fmt_odds(o: Any) -> str:
    if o is None:
        return "—"
    try:
        return "%.2f" % float(o)
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# one cycle
# ---------------------------------------------------------------------------


def run_cycle(
    db_path: str,
    max_per_cycle: int,
    seed: Optional[bool] = False,
    timeout: float = 12.0,
    client: Any = None,
    chat_id: Optional[str] = None,
    fetch=None,
    now_utc: Optional[str] = None,
) -> Dict[str, int]:
    """Execute one full scrape / diff / boost-eval / push cycle.

    ``seed`` is tri-state: ``True`` forces a recon re-seed; ``False`` (default)
    auto-seeds only when the catalog is empty; ``None`` (``--no-seed``) never
    seeds even when empty.

    Returns a small stats dict for logging/tests. Telegram is never contacted
    when ``PYTEST_CURRENT_TEST`` is set. Injectables for tests: ``fetch`` (a
    callable ``(url) -> (http_status, text, fetch_status)`` overriding
    :func:`wca.promos.fetch_page`), ``client`` (stub TelegramClient), ``chat_id``,
    and ``now_utc`` (pin the timestamp for determinism).
    """
    from wca import promos

    fetch = fetch or (lambda url: promos.fetch_page(url, timeout=timeout))
    now = now_utc or promos._now_utc()

    stats = {
        "sites": 0, "ok": 0, "blocked": 0, "empty": 0, "error": 0,
        "new": 0, "changed": 0, "removed": 0,
        "boosts_seen": 0, "boosts_priceable": 0, "boosts_plus_ev": 0,
        "pushed": 0,
    }

    conn = promos._connect(db_path)
    try:
        promos.init_db(conn)

        # 1) Seed when forced (seed=True), or when the catalog is empty unless
        #    seeding is explicitly disabled (seed=None from --no-seed).
        n_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        if seed is True or (seed is False and n_promos == 0):
            counts = promos.seed_from_recon(conn, now_utc=now)
            _log("seeded from recon: signup=%d ongoing=%d (catalog had %d rows)"
                 % (counts["signup"], counts["ongoing"], n_promos))

        # Load the boost-grading scores feed once per cycle if the engine exists.
        scores_feed = None
        if boosts is not None:
            try:
                scores_feed = boosts.load_scores_feed()
            except Exception as exc:  # noqa: BLE001 - missing/garbled feed
                _log("boosts.load_scores_feed failed (%s); boosts unpriceable this cycle"
                     % exc)
                scores_feed = None

        # Collected for the push step.
        new_changed: List[Tuple[str, str, Dict[str, Any]]] = []  # (site, kind, promo)
        plus_ev_boosts: List[Tuple[int, Dict[str, Any]]] = []  # (eval_id, view)

        # 2) Per-site scrape + diff.
        for entry in promos.SITES:
            stats["sites"] += 1
            site = entry["name"]
            kind = entry.get("kind", "")
            expect = bool(entry.get("expect_promos", True))
            urls: List[str] = [entry["promos_url"]]
            if entry.get("boosts_url"):
                urls.append(entry["boosts_url"])

            site_candidates: List[Dict[str, Any]] = []
            primary_status = "error"
            primary_http: Optional[int] = None
            for url in urls:
                try:
                    http_status, text, fetch_status = fetch(url)
                except Exception as exc:  # noqa: BLE001 - isolate per-site
                    _log("%s: fetch error for %s (%s)" % (site, url, exc))
                    http_status, text, fetch_status = None, None, "error"
                if url == entry["promos_url"]:
                    primary_status, primary_http = fetch_status, http_status
                stats[fetch_status] = stats.get(fetch_status, 0) + 1
                if fetch_status == "ok" and text:
                    try:
                        site_candidates.extend(promos.extract_promos(text, site))
                    except Exception as exc:  # noqa: BLE001
                        _log("%s: extract error (%s)" % (site, exc))

            # Snapshot the primary page result honestly. For exchanges /
            # prediction markets that don't run promos, an empty/ok with nothing
            # found is the truthful "no traditional promos here", not a miss.
            notes = None
            if not expect:
                notes = "no bookmaker-style promos expected (exchange/prediction market)"
            n_found = len(site_candidates)
            promos.record_snapshot(
                conn, site, entry["promos_url"], primary_http,
                primary_status, n_found, notes=notes, ts_utc=now,
            )

            # Only reconcile when we actually scraped something OK; a blocked /
            # empty fetch must NOT wipe scraped offers we found previously, so we
            # skip diff entirely (which would otherwise mark them removed).
            if primary_status != "ok" and n_found == 0:
                continue

            diff = promos.diff_and_upsert(
                conn, site, site_candidates, now, source="scrape"
            )
            stats["new"] += len(diff["new"])
            stats["changed"] += len(diff["changed"])
            stats["removed"] += len(diff["removed"])

            # Collect new/changed promotions for the push step + grade boosts.
            touched_fps = set(diff["new"]) | set(diff["changed"])
            for cand in site_candidates:
                fp = promos.fingerprint(
                    site, cand.get("title") or "", cand.get("description") or ""
                )
                if fp not in touched_fps:
                    continue
                new_changed.append((site, kind, cand))
                if cand.get("promo_type") == "boost":
                    eid, view, is_ev = _grade_boost(
                        conn, site, cand, scores_feed, now
                    )
                    stats["boosts_seen"] += 1
                    if view.get("_priceable"):
                        stats["boosts_priceable"] += 1
                    if is_ev:
                        stats["boosts_plus_ev"] += 1
                        plus_ev_boosts.append((eid, view))

        # 3/4) Push NEW/CHANGED promos + +EV boosts (capped, highest-edge first).
        under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if not under_pytest:
            if client is None:
                client = _build_client()
            if chat_id is None:
                chat_id = resolve_chat_id()

        plus_ev_boosts.sort(key=lambda t: (t[1].get("edge") or -1.0), reverse=True)

        pushed = 0
        pushed_boost_ids: List[int] = []
        # Boosts first (they're the time-sensitive, gradable signal), then promos.
        for eid, view in plus_ev_boosts:
            if pushed >= max_per_cycle:
                break
            text = format_boost_alert(view)
            if under_pytest:
                pushed += 1  # exercised, never sent
                continue
            if client is None or not chat_id:
                _log("no telegram client/chat configured; skipping boost push")
                break
            try:
                client.send_message(chat_id, text)
                pushed += 1
                pushed_boost_ids.append(eid)
            except Exception as exc:  # noqa: BLE001 - one bad send != dead cycle
                _log("boost send failed (%s); will retry next cycle" % exc)

        for site, kind, promo in new_changed:
            if pushed >= max_per_cycle:
                break
            # Boosts already pushed via the +EV path above; skip duplicates.
            if promo.get("promo_type") == "boost":
                continue
            text = format_promo_alert(site, kind, promo)
            if under_pytest:
                pushed += 1
                continue
            if client is None or not chat_id:
                _log("no telegram client/chat configured; skipping promo push")
                break
            try:
                client.send_message(chat_id, text)
                pushed += 1
            except Exception as exc:  # noqa: BLE001
                _log("promo send failed (%s); will retry next cycle" % exc)

        if pushed_boost_ids:
            promos.mark_boost_pushed(conn, pushed_boost_ids)
        stats["pushed"] = pushed
    finally:
        conn.close()

    _log(
        "cycle done: sites=%d ok=%d blocked=%d empty=%d error=%d | "
        "new=%d changed=%d removed=%d | boosts seen=%d priceable=%d +EV=%d | pushed=%d"
        % (stats["sites"], stats["ok"], stats["blocked"], stats["empty"],
           stats["error"], stats["new"], stats["changed"], stats["removed"],
           stats["boosts_seen"], stats["boosts_priceable"],
           stats["boosts_plus_ev"], stats["pushed"])
    )
    return stats


def _grade_boost(
    conn: Any,
    site: str,
    cand: Dict[str, Any],
    scores_feed: Any,
    now_utc: str,
) -> Tuple[int, Dict[str, Any], bool]:
    """Grade one scraped boost candidate and record a ``boost_evals`` row.

    Returns ``(eval_id, view, is_plus_ev)`` where ``view`` is a dict suitable for
    :func:`format_boost_alert` (plus a private ``_priceable`` flag). When the
    boost text can't be parsed, OR the :mod:`wca.boosts` engine is unavailable,
    the row is recorded ``priceable=False`` with an honest reason and
    ``is_plus_ev=False`` — we never guess a price or an edge.
    """
    from wca import promos

    parsed = promos.parse_boost_text(cand.get("description") or cand.get("title") or "")
    if not parsed:
        eid = promos.record_boost_eval(
            conn, ts_utc=now_utc, site=site, fixture=None, market=None,
            selection=(cand.get("title") or "")[:120] or None,
            boosted_odds=None, was_odds=None, model_prob=None, fair_odds=None,
            edge=None, is_plus_ev=False, priceable=False,
            reason="could not parse boost text", source="scrape",
        )
        return eid, {"site": site, "selection": cand.get("title"),
                     "_priceable": False}, False

    selection = parsed.get("selection")
    boosted = parsed.get("boosted_odds")
    was = parsed.get("was_odds")

    # No engine -> visible but ungraded (priceable text, but no model verdict).
    if boosts is None:
        eid = promos.record_boost_eval(
            conn, ts_utc=now_utc, site=site, fixture=None, market=None,
            selection=selection, boosted_odds=boosted, was_odds=was,
            model_prob=None, fair_odds=None, edge=None,
            is_plus_ev=False, priceable=False,
            reason="boost engine (wca.boosts) unavailable", source="scrape",
        )
        return eid, {"site": site, "selection": selection,
                     "boosted_odds": boosted, "_priceable": False}, False

    # Engine available: build a Boost and evaluate it (pure function).
    try:
        boost = boosts.Boost(
            site=site, fixture="", market="", selection=selection or "",
            boosted_odds=float(boosted), was_odds=was, is_inplay=False,
        )
        ev = boosts.evaluate_boost(boost, scores_feed or {})
    except Exception as exc:  # noqa: BLE001 - engine hiccup -> log unpriceable
        eid = promos.record_boost_eval(
            conn, ts_utc=now_utc, site=site, fixture=None, market=None,
            selection=selection, boosted_odds=boosted, was_odds=was,
            model_prob=None, fair_odds=None, edge=None,
            is_plus_ev=False, priceable=False,
            reason="boost evaluation failed: %s" % exc, source="scrape",
        )
        return eid, {"site": site, "selection": selection,
                     "boosted_odds": boosted, "_priceable": False}, False

    is_ev = bool(getattr(ev, "is_plus_ev", False))
    eid = promos.record_boost_eval(
        conn, ts_utc=now_utc, site=site, fixture="", market="",
        selection=selection, boosted_odds=boosted, was_odds=was,
        model_prob=getattr(ev, "model_prob", None),
        fair_odds=getattr(ev, "fair_odds", None),
        edge=getattr(ev, "edge", None),
        is_plus_ev=is_ev,
        priceable=bool(getattr(ev, "priceable", True)),
        reason=getattr(ev, "reason", None), source="scrape",
    )
    view = {
        "site": site, "fixture": "", "selection": selection,
        "boosted_odds": boosted,
        "model_prob": getattr(ev, "model_prob", None),
        "fair_odds": getattr(ev, "fair_odds", None),
        "edge": getattr(ev, "edge", None),
        "_priceable": True,
    }
    return eid, view, is_ev


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


def _startup_line(args) -> None:
    from wca import promos

    chat = resolve_chat_id()
    chat_disp = chat if chat else "<none configured>"
    engine = "available" if boosts is not None else "absent (boosts skipped)"
    _log(
        "starting: sites=%d | boost engine=%s | interval=%gs | "
        "max_per_cycle=%d | timeout=%gs | chat=%s | NOTE: most book hubs are "
        "Cloudflare-protected — 'blocked'/'empty' is the expected scrape outcome; "
        "day-1 catalog leans on the recon seed"
        % (len(promos.SITES), engine, args.interval, args.max_per_cycle,
           args.timeout, chat_disp)
    )


def run(db_path: str, args) -> None:
    _install_signal_handlers()
    _startup_line(args)
    try:
        while True:
            try:
                run_cycle(
                    db_path,
                    max_per_cycle=args.max_per_cycle,
                    seed=args.seed,
                    timeout=args.timeout,
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
    parser = argparse.ArgumentParser(
        description="World Cup Alpha promotions monitor daemon"
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite db path")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    parser.add_argument("--interval", type=float, default=3600.0,
                        help="seconds between cycles (default 3600)")
    parser.add_argument("--max-per-cycle", type=int, default=5,
                        help="max alerts pushed per cycle, highest-edge first "
                             "(default 5)")
    parser.add_argument("--timeout", type=float, default=12.0,
                        help="per-page fetch timeout seconds (default 12)")
    parser.add_argument("--seed", dest="seed", action="store_const", const=True,
                        help="force a recon re-seed this run (default: auto-seed "
                             "only when the catalog is empty)")
    parser.add_argument("--no-seed", dest="seed", action="store_const", const=None,
                        help="never seed, even if the catalog is empty")
    parser.set_defaults(seed=False)
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit (cron / CI / testing)")
    args = parser.parse_args(argv)

    _load_dotenv(args.env)
    run(db_path=args.db, args=args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
