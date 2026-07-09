"""Bookmaker / exchange **promotions** catalog and scraper.

This is the data layer of the "promo operations system": a continuously-refreshed
catalog of the sign-up offers, ongoing tournament promotions and daily price
boosts the books are running, so the World Cup card builder (and the human at
the terminal) can see at a glance what is claimable today and which boosts are
genuinely +EV against the model.

It is the *promotions* sibling of :mod:`wca.offers`. The two are deliberately
distinct: :mod:`wca.offers` is the **execution ledger** of matched-betting
extraction (qualifying losses, free-bet face value, locked cash); this module is
the **discovery / monitoring** layer (what offers *exist* in the wild). They
share the ``data/wca.db`` file but never each other's tables.

ISOLATION (read this)
---------------------
Like :mod:`wca.offers`, this module owns only its own tables and is forbidden
from touching the model-edge ledger:

* It creates and writes ONLY ``promotions``, ``promo_snapshots`` and
  ``boost_evals``.
* It NEVER touches ``bets`` / ``bankroll_events`` (the CLV experiment) or
  ``sb_offers`` (the matched-betting ledger owned by :mod:`wca.offers`).
* A regression test mirrors ``tests/test_offers.py::TestIsolation`` and asserts
  that running every promo operation in this module against a db that already
  holds ``bets`` and ``sb_offers`` rows leaves those row counts untouched.

Honesty about scraping
----------------------
We are scraping public promotions hubs with stdlib + ``requests`` only — no
headless browser, no JS execution, no bot-evasion. The honest reality is that
**most bookmaker promo hubs are Cloudflare-protected single-page apps** that
will return a challenge page (or a 403/429) to a plain ``requests`` GET, or that
render their offers entirely client-side so the initial HTML carries no offer
text. We therefore:

* Treat a block / challenge / non-200 as the *expected, normal* outcome and
  record it honestly in ``promo_snapshots`` (``fetch_status='blocked'`` etc.)
  rather than pretending we scraped nothing because there was nothing there.
* **Never fabricate an offer.** :func:`extract_promos` returns ``[]`` when its
  keyword heuristics match nothing — a guess is worse than a gap.
* Seed the catalog from the hand-verified recon doc (``docs/recon/uk_books.md``)
  so the site is useful on day one even while live scraping is mostly blocked.
  Seeds are marked ``source='seed'`` and are never auto-removed by a scrape run.
* For exchanges (Smarkets, Matchbook, Betfair Exchange) and the prediction
  market (Polymarket) that simply do **not** run bookmaker-style promotions, the
  registry flags ``expect_promos=False`` so an empty fetch is recorded as the
  honest "no traditional promos here" rather than as a scrape failure.

Public API (kept stable for ``wca_promosd.py`` / ``wca.promosdata`` / tests):

* ``SITES``                         — registry of books / exchanges to monitor.
* ``init_db`` / ``_connect`` / ``_now_utc`` — schema + connection helpers.
* ``fetch_page(url, ...)``          — robust single-page fetch -> (status, text, fetch_status).
* ``extract_promos(html, site)``    — heuristic offer extraction (never fabricates).
* ``fingerprint(site, title, desc)``— stable dedupe key.
* ``record_snapshot(...)``          — log one fetch attempt.
* ``diff_and_upsert(...)``          — upsert candidates, mark vanished as removed.
* ``seed_from_recon(...)``          — parse the recon doc into seed rows.
* ``record_boost_eval(...)``        — log one boost evaluation.
* ``active_promotions`` / ``latest_snapshot_per_site`` / ``recent_boost_evals``
  / ``signup_offers``               — reader views for the feed builder.
* ``parse_boost_text(text)``        — best-effort (selection, odds, was_odds) extraction.

Everything here is stdlib + ``requests`` only. HTML is reduced to text with
regex (NO BeautifulSoup), exactly like :func:`wca.news._strip_html`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

_DEFAULT_DB = "data/wca.db"

# ---------------------------------------------------------------------------
# Site registry
# ---------------------------------------------------------------------------

#: Browser-ish User-Agent. A bare ``python-requests`` UA is 403'd instantly by
#: most book CDNs; this at least gets us a challenge page we can classify rather
#: than a blank refusal. Mirrors :data:`wca.news.USER_AGENT` in intent.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: Hard cap on a single promotions page we will buffer + parse (bytes). Promo
#: hubs are heavy SPAs, so this is more generous than the RSS cap in
#: :mod:`wca.news`, but still bounded so one runaway page can't exhaust memory
#: in the long-running daemon.
MAX_PAGE_BYTES = 12 * 1024 * 1024

#: The books / exchanges / prediction markets we monitor.
#:
#: ``kind``          — 'book' | 'exchange' | 'prediction_market' (informational,
#:                     surfaced on the site).
#: ``promos_url``    — public promotions-hub URL fetched for ongoing/signup promos.
#: ``boosts_url``    — optional dedicated price-boost page (defaults to None).
#: ``expect_promos`` — when False, an empty fetch is recorded honestly as
#:                     "no traditional promos here" rather than a scrape miss
#:                     (exchanges + Polymarket don't run bookmaker promotions).
#: ``manual_check``  — when True, a real-browser recon pass (2026-07-08) confirmed
#:                     this hub is a pure client-rendered SPA shell (or is behind
#:                     an active Cloudflare/Akamai JS challenge) with NO offer text
#:                     in the server-rendered HTML a plain ``requests`` GET can see
#:                     — server-side scraping cannot ever recover it without a
#:                     headless browser (out of scope; see the module docstring).
#:                     We keep fetching it anyway (a redesign could make it
#:                     scrapeable, and the scrape-health history stays honest),
#:                     but the daemon ALSO surfaces it as a dated manual-check
#:                     entry (site + url + reason) so the human's daily sweep is a
#:                     short click-through list instead of a false "nothing here".
#: ``manual_check_reason`` — one-line, human-readable justification (what we saw
#:                     when we probed it), shown verbatim on the site.
SITES: List[Dict[str, Any]] = [
    # --- Bookmakers (run sign-up offers, ongoing promos and price boosts) ---
    {
        "name": "Paddy Power",
        "kind": "book",
        "promos_url": "https://www.paddypower.com/promotions",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "web-components app-shell (SVG sprite preloads only); offer text is "
            "fetched client-side, nothing server-rendered to parse"
        ),
    },
    {
        "name": "Sky Bet",
        "kind": "book",
        "promos_url": "https://www.skybet.com/offers",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "active Cloudflare JS challenge (/cdn-cgi/challenge-platform/) on "
            "every fetch — correctly recorded as 'blocked', not fixable server-side"
        ),
    },
    {
        "name": "Bet365",
        "kind": "book",
        "promos_url": "https://www.bet365.com/#/AVR/B1/",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "403 on every fetch, and the promo hub is a '#/' SPA hash-route a "
            "plain GET can never resolve differently anyway"
        ),
    },
    {
        "name": "Virgin Bet",
        "kind": "book",
        "promos_url": "https://www.virginbet.com/promotions",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "genuine empty shell — body is just "
            "<div id=\"clientAppRoot\"></div>, offers load via client-side XHR "
            "with no discoverable open JSON endpoint"
        ),
    },
    {
        "name": "William Hill",
        "kind": "book",
        "promos_url": "https://sports.williamhill.com/betting/en-gb/apps/promotions",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "200 but an empty shell (one deferred bundle.js); the site's own "
            "GraphQL API (gql-cs.williamhill.com) is auth-gated (403 unauthenticated)"
        ),
    },
    {
        "name": "Ladbrokes",
        "kind": "book",
        "promos_url": "https://sports.ladbrokes.com/promotions",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "Entain/bwin client-bootstrap SPA — empty <body>, real content "
            "loads via a session-cookie-bound clientconfig API with no static "
            "guessable JSON URL"
        ),
    },
    {
        "name": "Unibet",
        "kind": "book",
        "promos_url": "https://www.unibet.co.uk/promotions",
        "boosts_url": None,
        "expect_promos": True,
    },
    {
        "name": "Betfair Sportsbook",
        "kind": "book",
        "promos_url": "https://www.betfair.com/sport/promotions",
        "boosts_url": None,
        "expect_promos": True,
        "manual_check": True,
        "manual_check_reason": (
            "403 on every fetch (Cloudflare) even after following the redirect "
            "to /betting/"
        ),
    },
    # --- Exchanges (peer-to-peer; no bookmaker-style promos) ---
    {
        "name": "Smarkets",
        "kind": "exchange",
        "promos_url": "https://smarkets.com/promotions",
        "boosts_url": None,
        "expect_promos": False,
        "manual_check": True,
        "manual_check_reason": (
            "active Cloudflare JS challenge (/cdn-cgi/challenge-platform/) — "
            "correctly recorded as 'blocked'; exchange, no promos expected anyway"
        ),
    },
    {
        "name": "Matchbook",
        "kind": "exchange",
        "promos_url": "https://www.matchbook.com/promotions",
        "boosts_url": None,
        "expect_promos": False,
        "manual_check": True,
        "manual_check_reason": (
            "genuine empty shell (<div id=\"root\"></div>, 0 bytes of body text); "
            "exchange, no promos expected anyway"
        ),
    },
    {
        "name": "Betfair Exchange",
        "kind": "exchange",
        "promos_url": "https://www.betfair.com/exchange/plus/promotions",
        "boosts_url": None,
        "expect_promos": False,
        "manual_check": True,
        "manual_check_reason": (
            "same Cloudflare block as Betfair Sportsbook; exchange, no promos "
            "expected anyway"
        ),
    },
    # --- Prediction market (no promos) ---
    {
        "name": "Polymarket",
        "kind": "prediction_market",
        "promos_url": "https://polymarket.com",
        "boosts_url": None,
        "expect_promos": False,
    },
]


def site_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Return the :data:`SITES` registry entry for *name* (or ``None``)."""
    for s in SITES:
        if s["name"] == name:
            return s
    return None


def manual_check_sites() -> List[Dict[str, str]]:
    """Registry entries flagged ``manual_check`` -> ``{site, url, reason}``.

    This is the human's daily 2-minute click-through list: sources a real probe
    confirmed are pure client-rendered SPA shells or sit behind an active
    bot-challenge, so server-side scraping structurally cannot recover them
    (see :data:`SITES`'s ``manual_check`` docs). We keep fetching these sites
    every cycle regardless — a site redesign could make one scrapeable again,
    and the scrape-health history stays honest either way — but this view is
    what the feed builder surfaces as an explicit "go look yourself" list rather
    than silently presenting an empty scrape as "no promos".
    """
    out: List[Dict[str, str]] = []
    for entry in SITES:
        if not entry.get("manual_check"):
            continue
        out.append(
            {
                "site": entry["name"],
                "url": entry.get("promos_url") or "",
                "reason": entry.get("manual_check_reason") or "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Connection / schema helpers
# ---------------------------------------------------------------------------

#: 'signup' | 'ongoing' | 'boost' | 'watchlist'
PROMO_TYPES = ("signup", "ongoing", "boost", "watchlist")
#: 'scrape' | 'seed' | 'vision' | 'manual'
PROMO_SOURCES = ("scrape", "seed", "vision", "manual")

_DDL_PROMOTIONS = """
CREATE TABLE IF NOT EXISTS promotions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site            TEXT    NOT NULL,
    promo_type      TEXT    NOT NULL,
    title           TEXT,
    description     TEXT,
    terms           TEXT,
    url             TEXT,
    fingerprint     TEXT    NOT NULL UNIQUE,
    first_seen_utc  TEXT    NOT NULL,
    last_seen_utc   TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active',
    source          TEXT    NOT NULL
)
"""

_DDL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS promo_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT    NOT NULL,
    site          TEXT    NOT NULL,
    url           TEXT,
    http_status   INTEGER,
    fetch_status  TEXT    NOT NULL,
    n_found       INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
)
"""

_DDL_BOOST_EVALS = """
CREATE TABLE IF NOT EXISTS boost_evals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc       TEXT    NOT NULL,
    site         TEXT,
    fixture      TEXT,
    market       TEXT,
    selection    TEXT,
    boosted_odds REAL,
    was_odds     REAL,
    model_prob   REAL,
    fair_odds    REAL,
    edge         REAL,
    is_plus_ev   INTEGER NOT NULL DEFAULT 0,
    priceable    INTEGER NOT NULL DEFAULT 0,
    reason       TEXT,
    source       TEXT    NOT NULL,
    pushed       INTEGER NOT NULL DEFAULT 0
)
"""


def _connect(db_path: str = _DEFAULT_DB) -> sqlite3.Connection:
    """Open the db with WAL + row factory (mirrors :func:`wca.offers._connect`)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the three promo tables if absent. Idempotent.

    Accepts an open connection (the daemon owns one per cycle). NEVER creates or
    alters ``bets`` / ``bankroll_events`` / ``sb_offers`` — it owns only
    ``promotions`` / ``promo_snapshots`` / ``boost_evals``.
    """
    conn.execute(_DDL_PROMOTIONS)
    conn.execute(_DDL_SNAPSHOTS)
    conn.execute(_DDL_BOOST_EVALS)
    # Helpful indexes for the reader views; harmless if they already exist.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promotions_site_status "
        "ON promotions(site, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_site_ts "
        "ON promo_snapshots(site, ts_utc)"
    )
    conn.commit()


def _now_utc() -> str:
    """Current UTC time as a compact ISO-ish string (matches :mod:`wca.offers`)."""
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Page fetching (mirrors wca.news.fetch_feed robustness)
# ---------------------------------------------------------------------------

#: Body markers that betray a Cloudflare / bot-challenge / geo-block interstitial
#: even when the HTTP status is a deceptive 200. Lower-cased substring match.
#:
#: NOTE on the bare word "captcha": earlier versions of this tuple included it
#: as a standalone marker, which false-positived on ordinary SPA login-form i18n
#: JSON embedded in an otherwise perfectly scrapeable page (e.g. Unibet ships a
#: ``"captchaRequired":"Please verify that you are not a robot"`` login-validation
#: string on every page load — that is *not* a challenge shown to us, just a label
#: for a form the user never sees on a promotions page). A real captcha
#: interstitial reads as a sentence directed at the visitor ("complete the
#: captcha", "verify you are human", "i'm not a robot" checkbox copy), so we
#: require one of those *phrases* instead of the bare token. Genuine Cloudflare/
#: Akamai/Incapsula JS-challenge pages are still caught by the other, more
#: specific markers below (``challenge-platform``, ``cf-browser-verification``,
#: etc.) regardless of this change.
_BLOCK_MARKERS: Tuple[str, ...] = (
    "cf-browser-verification",
    "cf-challenge",
    "checking your browser",
    "challenge-platform",
    "attention required",
    "access denied",
    "request blocked",
    "you have been blocked",
    "enable javascript and cookies to continue",
    "ddos protection by cloudflare",
    "complete the captcha",
    "solve the captcha",
    "captcha to continue",
    "verify you are human",
    "i'm not a robot",
    "im not a robot",
    "incapsula incident",
    "error 1020",
    "ray id",
)

#: A body shorter than this (after we got a 200) is treated as effectively empty
#: — a JS shell with no server-rendered content. Tuned generously: real promo
#: hubs that DO render server-side carry kilobytes of offer text.
_MIN_USEFUL_BODY = 512


def _read_capped(resp: Any, max_bytes: int = MAX_PAGE_BYTES) -> bytes:
    """Read a response body without buffering more than *max_bytes*.

    Mirrors :func:`wca.news._read_capped`: prefer ``iter_content`` streaming so a
    hostile/oversized page is abandoned without materialising it; fall back to a
    plain ``.content`` read (hard-sliced) for stubbed test responses that don't
    implement streaming. Over-cap bodies are dropped (``b""``) rather than
    truncated.
    """
    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        try:
            buf = bytearray()
            for chunk in iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return b""
            return bytes(buf)
        except Exception:  # noqa: BLE001 - streaming hiccup -> fall back below
            pass
    content = getattr(resp, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    if len(content) > max_bytes:
        return b""
    return content


def fetch_page(
    url: str,
    session: Any = None,
    timeout: float = 12.0,
) -> Tuple[Optional[int], Optional[str], str]:
    """Fetch one promotions page robustly.

    Returns ``(http_status, text, fetch_status)`` where ``fetch_status`` is one
    of ``'ok' | 'blocked' | 'error' | 'empty'``:

    * ``'blocked'`` — HTTP 403/429, or a 200 whose body is a Cloudflare / bot /
      geo challenge interstitial (see :data:`_BLOCK_MARKERS`). This is the
      *expected* outcome for most book hubs from a plain ``requests`` GET.
    * ``'error'``   — any other non-200 status, or a transport failure.
    * ``'empty'``   — a 200 with no / too-little body (a JS shell that renders
      its offers client-side, so the initial HTML carries nothing to extract).
    * ``'ok'``      — a 200 with a usable amount of body text.

    Never raises: every transport/decoding error is mapped to a status, exactly
    like :func:`wca.news.fetch_feed`. ``requests`` is imported lazily so this
    module parses standalone. ``session`` is injectable for tests (a stub with a
    ``.get`` method); a stub that doesn't accept ``stream=`` is retried plainly.
    """
    import requests  # lazy: keep module import-light / standalone-parseable

    sess = session or requests
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    try:
        resp = sess.get(url, timeout=timeout, headers=headers, stream=True)
    except TypeError:
        # An injected stub session may not accept ``stream=`` — retry plainly.
        try:
            resp = sess.get(url, timeout=timeout, headers=headers)
        except Exception:  # noqa: BLE001 - transport error
            return None, None, "error"
    except Exception:  # noqa: BLE001 - transport error
        return None, None, "error"

    status = getattr(resp, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None

    # Explicit block statuses, regardless of body.
    if status_int in (403, 429):
        return status_int, None, "blocked"
    # Any other non-200 is an error (404, 5xx, redirects we didn't follow, ...).
    if status_int is not None and status_int != 200:
        return status_int, None, "error"

    raw = _read_capped(resp)
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")

    low = text.lower()
    if any(marker in low for marker in _BLOCK_MARKERS):
        return status_int, text, "blocked"
    if len(text.strip()) < _MIN_USEFUL_BODY:
        return status_int, text, "empty"
    return status_int, text, "ok"


# ---------------------------------------------------------------------------
# HTML -> text + heuristic offer extraction
# ---------------------------------------------------------------------------

_RE_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_RE_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\f\v]+")


def _strip_html_to_lines(html_text: str) -> List[str]:
    """Reduce an HTML document to a list of visible text lines.

    Removes ``<script>``/``<style>``/comments, replaces block-ish tags with line
    breaks so distinct offer blocks stay on distinct lines, collapses the rest of
    the tags to spaces, unescapes entities, and splits on newlines. Mirrors the
    spirit of :func:`wca.news._strip_html` but keeps line structure (we need it
    to find offer-like *blocks*).
    """
    if not html_text:
        return []
    s = _RE_SCRIPT.sub(" ", html_text)
    s = _RE_STYLE.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)
    # Turn structural tags into newlines so adjacent blocks don't fuse.
    s = re.sub(
        r"</?(?:p|div|li|ul|ol|br|h[1-6]|section|article|tr|td|th|"
        r"header|footer|span|a)\b[^>]*>",
        "\n",
        s,
        flags=re.IGNORECASE,
    )
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)
    lines: List[str] = []
    for raw in s.split("\n"):
        line = _RE_WS.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return lines


# Keyword patterns that mark an offer-like blurb. Order/structure is documented
# rather than clever — readability beats density here.
_RE_BET_GET = re.compile(
    r"\bbet\b[^.\n]{0,30}?(?:£|\beuro|\bgbp)?\s*\d+[^.\n]{0,30}?\bget\b",
    re.IGNORECASE,
)
_RE_MONEY = re.compile(r"£\s?\d[\d,]*")
_RE_FRACTIONAL_ODDS = re.compile(r"\b\d{1,3}\s*/\s*\d{1,3}\b")
_RE_PROMO_CODE = re.compile(r"\b(?:promo|bonus)\s*code[: ]+([A-Z0-9]{4,12})\b", re.IGNORECASE)

#: Phrases that, present in a text block, mark it as a promotion. Lower-cased
#: substring match; tuned to the recon doc's vocabulary.
_PROMO_KEYWORDS: Tuple[str, ...] = (
    "free bet",
    "free bets",
    "money back",
    "money-back",
    "price boost",
    "odds boost",
    "enhanced odds",
    "enhanced price",
    "2 up",
    "two goals ahead",
    "acca",
    "bet builder",
    "boost",
    "bet £",
    "get £",
    "in free bets",
    "risk free",
    "risk-free",
    "no deposit",
    "deposit bonus",
    "matched free bet",
    "reward",
    "super boost",
    "epic boost",
    "power price",
)

#: Phrases that mean the block describes a PRICE BOOST specifically (a single
#: enhanced selection) rather than a general ongoing offer.
_BOOST_KEYWORDS: Tuple[str, ...] = (
    "price boost",
    "odds boost",
    "enhanced odds",
    "enhanced price",
    "super boost",
    "epic boost",
    "power price",
    "boost",
    "was ",
    "now ",
)

#: Navigation / chrome lines that are never offers even though they trip a
#: keyword (e.g. a menu item literally named "Free Bets"). Drop these.
_NAV_NOISE: Tuple[str, ...] = (
    "cookie",
    "privacy policy",
    "terms and conditions apply",
    "begambleaware",
    "gamble responsibly",
    "18+",
    "sign in",
    "log in",
    "register",
    "download the app",
    "skip to",
    "menu",
)

#: A block must be at least this long to be a plausible offer (kills one-word
#: nav items) but we still accept short, dense "Bet £X Get £Y" blurbs via the
#: regex gate below.
_MIN_OFFER_LEN = 16
#: ...and not absurdly long (a whole T&C wall is not a single offer "title").
_MAX_OFFER_LEN = 400


def _looks_like_offer(line: str) -> bool:
    """True if *line* reads like an offer blurb (keyword + sanity gates)."""
    low = line.lower()
    if any(n in low for n in _NAV_NOISE):
        # ...unless it's clearly an offer body that merely mentions 18+ at the
        # end; require a strong "bet X get Y" signal to rescue it.
        if not _RE_BET_GET.search(line):
            return False
    n = len(line)
    if n > _MAX_OFFER_LEN:
        return False
    has_kw = any(kw in low for kw in _PROMO_KEYWORDS)
    has_betget = bool(_RE_BET_GET.search(line))
    has_money = bool(_RE_MONEY.search(line))
    has_odds = bool(_RE_FRACTIONAL_ODDS.search(line)) or "evens" in low
    # A short line needs a strong structural signal; a longer line can lean on
    # keywords. Never accept on length alone.
    if n < _MIN_OFFER_LEN:
        return has_betget
    if has_betget:
        return True
    if has_kw and (has_money or has_odds or "free bet" in low or "boost" in low):
        return True
    # A bare keyword with no number is too weak (nav item) -> reject.
    return False


def _classify_promo_type(line: str) -> str:
    """'boost' if the block reads like a price boost, else 'ongoing'."""
    low = line.lower()
    # "free bet" / "money back" / "acca" sign-up-ish language -> ongoing.
    boost_hits = sum(1 for kw in _BOOST_KEYWORDS if kw in low)
    # A "was X now Y" or explicit boost wording dominates.
    if "price boost" in low or "odds boost" in low or "enhanced" in low \
            or "super boost" in low or "epic boost" in low or "power price" in low:
        return "boost"
    if "free bet" in low or "money back" in low or "acca" in low:
        return "ongoing"
    if boost_hits >= 1 and _RE_FRACTIONAL_ODDS.search(line):
        return "boost"
    return "ongoing"


def extract_promos(html_text: str, site: str) -> List[Dict[str, Any]]:
    """Heuristically pull offer-like blocks from a promotions page's HTML.

    Strips scripts/styles/comments, collapses tags to text, unescapes entities,
    then keeps lines that pass :func:`_looks_like_offer`. Each kept block becomes
    ``{title, description, promo_type, terms, url}`` where ``promo_type`` is
    ``'boost'`` (reads like a price boost) or ``'ongoing'`` (everything else
    here; sign-ups come from the seed, not the scraper).

    Returns ``[]`` rather than guessing when nothing matches — a fabricated offer
    is worse than an honest gap (see the module docstring). ``site`` is accepted
    for parity / future per-site tuning; it is currently unused in the heuristic.
    """
    lines = _strip_html_to_lines(html_text)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for line in lines:
        if not _looks_like_offer(line):
            continue
        # Dedupe identical blocks within one page (repeated banners).
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        title = line if len(line) <= 120 else (line[:117].rstrip() + "…")
        out.append(
            {
                "title": title,
                "description": line,
                "promo_type": _classify_promo_type(line),
                "terms": "",
                "url": "",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Fingerprinting + boost-text parsing
# ---------------------------------------------------------------------------

_RE_NORM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation/whitespace to a single normalized token run."""
    return _RE_NORM.sub(" ", (text or "").lower()).strip()


def canonical_site_name(name: str) -> str:
    """Map a recon-doc site label onto the :data:`SITES` registry name.

    The recon doc annotates some 2b headers (e.g.
    ``"Betfair Exchange (primary CLV reference)"``) and may carry a bookmaker
    sub-variant in a 2a row (``"Paddy Power (Bet Builder)"``). We strip a trailing
    parenthetical and, when the normalized result matches a registry entry,
    return that entry's canonical name so seed rows attach to the right site card.
    A non-matching name is returned cleaned-but-unchanged (still seeded; it just
    appears as an extra card).
    """
    if not name:
        return name
    base = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    norm = _normalize(base)
    for entry in SITES:
        if _normalize(entry["name"]) == norm:
            return entry["name"]
    return base or name


def fingerprint(site: str, title: str, description: str) -> str:
    """Stable sha1 dedupe key over normalized (site, title, description).

    Whitespace/punctuation/case differences do not change the fingerprint, so a
    re-scrape of the same offer dedupes cleanly; a materially different offer
    body produces a different key (and is treated as a new promotion).
    """
    key = "%s|%s|%s" % (
        _normalize(site),
        _normalize(title),
        _normalize(description),
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# Odds token: try fractional 'a/b' BEFORE the bare decimal, else the decimal
# alternative would greedily swallow just the 'a' of 'a/b' (e.g. '2' of '2/1').
_ODDS_TOKEN = r"\d{1,3}\s*/\s*\d{1,3}|\d{1,3}(?:\.\d{1,2})?"
_RE_WAS_NOW = re.compile(
    r"was\s*(" + _ODDS_TOKEN + r")"
    r".{0,40}?"
    r"(?:now|boosted to|boost(?:ed)?)\s*(" + _ODDS_TOKEN + r")",
    re.IGNORECASE | re.DOTALL,
)


def _frac_to_decimal(frac: str) -> Optional[float]:
    """Convert fractional odds 'a/b' to decimal (a/b + 1). None on failure."""
    m = re.match(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*$", frac)
    if not m:
        return None
    num, den = int(m.group(1)), int(m.group(2))
    if den == 0:
        return None
    return round(num / den + 1.0, 4)


def _to_decimal(token: str) -> Optional[float]:
    """Parse an odds token (decimal '2.5', fractional '6/4', or 'evens')."""
    t = (token or "").strip().lower()
    if not t:
        return None
    if t in ("evens", "even", "evs"):
        return 2.0
    if "/" in t:
        return _frac_to_decimal(t)
    try:
        v = float(t)
    except ValueError:
        return None
    # A decimal price below 1.01 isn't a real selection price.
    return v if v >= 1.01 else None


def parse_boost_text(text: str) -> Optional[dict]:
    """Best-effort extraction of (selection, odds, was_odds) from a boost blob.

    Tries the common "X was 5/1 now 13/2" / "boosted to 7.5" shapes. Returns a
    dict ``{"selection", "boosted_odds", "was_odds"}`` when it can find at least
    a boosted price; returns ``None`` when it can't — the daemon then stores the
    boost as a *visible* promo but records ``priceable=False`` with a
    "could not parse boost text" reason, rather than guessing a price.

    This is intentionally conservative. Book boost copy is wildly inconsistent
    and JS-rendered; a wrong price would corrupt the boost-EV ledger, so a clean
    ``None`` is the honest answer far more often than not.
    """
    if not text:
        return None
    t = text.strip()

    was_odds: Optional[float] = None
    boosted_odds: Optional[float] = None

    m = _RE_WAS_NOW.search(t)
    if m:
        was_odds = _to_decimal(m.group(1))
        boosted_odds = _to_decimal(m.group(2))
    else:
        # Fall back: a single explicit "boosted to <odds>" / "now <odds>".
        m2 = re.search(
            r"(?:boosted to|boost(?:ed)?|now)\s*"
            r"(" + _ODDS_TOKEN + r"|evens)",
            t,
            re.IGNORECASE,
        )
        if m2:
            boosted_odds = _to_decimal(m2.group(1))

    if boosted_odds is None:
        return None

    # Selection = the text before the first "was/now/boost" keyword, trimmed.
    sel = re.split(r"\b(?:was|now|boost(?:ed)?|enhanced|price boost)\b", t, 1,
                   flags=re.IGNORECASE)[0].strip(" -–—:·•\t")
    if len(sel) > 120:
        sel = sel[:117].rstrip() + "…"
    return {
        "selection": sel or None,
        "boosted_odds": boosted_odds,
        "was_odds": was_odds,
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def record_snapshot(
    conn: sqlite3.Connection,
    site: str,
    url: Optional[str],
    http_status: Optional[int],
    fetch_status: str,
    n_found: int,
    notes: Optional[str] = None,
    ts_utc: Optional[str] = None,
) -> int:
    """Log one fetch attempt against a site's promo page. Returns the row id.

    Every cycle records a snapshot per site (even blocked/empty ones) so the
    site's "scrape health" panel shows an honest last-seen + status history
    rather than silently hiding failures.
    """
    ts = ts_utc or _now_utc()
    cur = conn.execute(
        "INSERT INTO promo_snapshots "
        "(ts_utc, site, url, http_status, fetch_status, n_found, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, site, url, http_status, fetch_status, int(n_found), notes),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Diff + upsert
# ---------------------------------------------------------------------------


def diff_and_upsert(
    conn: sqlite3.Connection,
    site: str,
    candidates: Sequence[Dict[str, Any]],
    now_utc: str,
    source: str = "scrape",
) -> Dict[str, List[Any]]:
    """Reconcile freshly-scraped *candidates* against stored promotions.

    For each candidate (a ``{title, description, promo_type, terms, url}`` dict)
    we compute its :func:`fingerprint` and:

    * **new**       — fingerprint unseen -> INSERT (status='active').
    * **unchanged** — fingerprint seen, text identical -> bump ``last_seen_utc``.
    * **changed**   — fingerprint seen but ``terms``/``url`` differ -> bump
      ``last_seen_utc`` and update the mutable fields (the fingerprint itself is
      title+description, so a *changed* body would be a *new* fingerprint; this
      branch catches drift in the secondary fields).

    Then any **active** promotion for this ``(site, source)`` whose fingerprint
    is absent from the candidate set is marked ``status='removed'`` — the offer
    vanished from the hub. **Seeds (`source='seed'`) are never auto-removed by a
    scrape run**: a scrape that comes back blocked must not wipe the hand-curated
    catalog, so removal is scoped to the same ``source`` being reconciled.

    Returns ``{"new": [...], "changed": [...], "removed": [...], "unchanged": [...]}``
    where each list holds the affected fingerprints.
    """
    result: Dict[str, List[Any]] = {
        "new": [],
        "changed": [],
        "removed": [],
        "unchanged": [],
    }

    seen_fps: List[str] = []
    for cand in candidates:
        title = cand.get("title") or ""
        desc = cand.get("description") or ""
        fp = fingerprint(site, title, desc)
        seen_fps.append(fp)
        terms = cand.get("terms") or ""
        url = cand.get("url") or ""
        promo_type = cand.get("promo_type") or "ongoing"

        row = conn.execute(
            "SELECT id, terms, url, status FROM promotions WHERE fingerprint = ?",
            (fp,),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO promotions "
                "(site, promo_type, title, description, terms, url, fingerprint, "
                " first_seen_utc, last_seen_utc, status, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (site, promo_type, title, desc, terms, url, fp,
                 now_utc, now_utc, source),
            )
            result["new"].append(fp)
            continue

        # Existing fingerprint: detect drift in mutable secondary fields and/or
        # a resurrection from 'removed' back to 'active'.
        changed = (
            (row["terms"] or "") != terms
            or (row["url"] or "") != url
            or (row["status"] or "") != "active"
        )
        conn.execute(
            "UPDATE promotions SET last_seen_utc = ?, terms = ?, url = ?, "
            "promo_type = ?, status = 'active' WHERE id = ?",
            (now_utc, terms, url, promo_type, row["id"]),
        )
        if changed:
            result["changed"].append(fp)
        else:
            result["unchanged"].append(fp)

    # Mark vanished promotions removed — scoped to this (site, source) so a
    # scrape never removes seeds and vice-versa.
    placeholders = ",".join("?" for _ in seen_fps) if seen_fps else ""
    if placeholders:
        active_rows = conn.execute(
            "SELECT fingerprint FROM promotions "
            "WHERE site = ? AND source = ? AND status = 'active' "
            "AND fingerprint NOT IN (%s)" % placeholders,
            tuple([site, source] + seen_fps),
        ).fetchall()
    else:
        active_rows = conn.execute(
            "SELECT fingerprint FROM promotions "
            "WHERE site = ? AND source = ? AND status = 'active'",
            (site, source),
        ).fetchall()
    for r in active_rows:
        conn.execute(
            "UPDATE promotions SET status = 'removed', last_seen_utc = ? "
            "WHERE fingerprint = ?",
            (now_utc, r["fingerprint"]),
        )
        result["removed"].append(r["fingerprint"])

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Seeding from the recon doc
# ---------------------------------------------------------------------------

#: A 2a sign-up-table row: "| Bookmaker | Offer | Min Bet/Min Odds | Free Bet
#: Value | Expiry | Promo Code | Source | Confidence |". We split on '|' and
#: tolerate column drift (short rows are skipped).
_SIGNUP_COLS = (
    "bookmaker", "offer", "min_bet_odds", "free_bet_value",
    "expiry", "promo_code", "source", "confidence",
)

#: A 2b sub-section header: "#### Paddy Power".
_RE_H4 = re.compile(r"^####\s+(?P<name>.+?)\s*$")
#: A 2b bullet: "- **Power Prices ...:** Daily enhanced odds ...".
_RE_BULLET = re.compile(r"^[-*]\s+(?P<body>.+?)\s*$")
#: Bold lead-in of a bullet: "**Power Prices / Match Odds Price Boosts:** ...".
_RE_BULLET_LEAD = re.compile(r"^\*\*(?P<lead>.+?)\*\*[:.\s]*(?P<rest>.*)$")


def _md_table_cells(line: str) -> List[str]:
    """Split a markdown table row on '|' into trimmed cells (drop edge empties)."""
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


def _is_table_separator(line: str) -> bool:
    """True for a markdown header-separator row like '|---|---|'."""
    stripped = line.strip().strip("|").replace(" ", "")
    return bool(stripped) and set(stripped) <= set("-:|")


def _parse_signup_table(lines: List[str], start: int) -> List[Dict[str, str]]:
    """Parse the 2a sign-up table starting near *start*; tolerant of drift."""
    rows: List[Dict[str, str]] = []
    in_table = False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("###") or stripped.startswith("## "):
            # Next section header ends the table region.
            if in_table:
                break
            continue
        if not stripped.startswith("|"):
            if in_table:
                # Blank/prose line after table rows -> table over.
                if stripped:
                    break
            continue
        cells = _md_table_cells(stripped)
        # Header row (contains the literal "Bookmaker" label) or separator.
        if _is_table_separator(stripped):
            in_table = True
            continue
        if cells and cells[0].lower() == "bookmaker":
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) < 4:
            continue
        # Map by position, tolerating extra/missing trailing columns.
        row = {col: (cells[i] if i < len(cells) else "")
               for i, col in enumerate(_SIGNUP_COLS)}
        if not row["bookmaker"] or not row["offer"]:
            continue
        rows.append(row)
    return rows


def _split_min_bet_odds(text: str) -> Tuple[str, str]:
    """Split a "£10 at 1/2+" style cell into (min_stake, min_odds) best-effort."""
    if not text:
        return "", ""
    money = _RE_MONEY.search(text)
    min_stake = money.group(0).replace(" ", "") if money else ""
    # min odds: a fractional, a decimal, or "evens".
    odds = ""
    fm = _RE_FRACTIONAL_ODDS.search(text)
    if fm:
        odds = fm.group(0).replace(" ", "")
    elif "evens" in text.lower():
        odds = "evens"
    else:
        dm = re.search(r"\b\d\.\d{1,2}\b", text)
        if dm:
            odds = dm.group(0)
    return min_stake, odds


def _parse_2b_sections(lines: List[str], start: int) -> List[Dict[str, str]]:
    """Parse the 2b ongoing-promo ``#### <Book>`` bullet sections.

    Each bullet becomes a promo dict. We classify a bullet as ``'watchlist'``
    when it is an honest "no promos / check the app" note (so the site flags it
    as something to watch rather than a claimable offer), and ``'ongoing'``
    otherwise. ``boost``-flavoured ongoing bullets (price boosts) are tagged
    ``promo_type='boost'``.
    """
    out: List[Dict[str, str]] = []
    current_site = ""
    for line in lines[start:]:
        stripped = line.rstrip()
        # End of section 2b at the next "## " or "---"-then-"## " top-level.
        if stripped.startswith("## ") and "2b" not in stripped.lower() \
                and "ongoing" not in stripped.lower():
            # A new numbered top-level section (## 3., ## 4. ...) ends 2b.
            if re.match(r"^##\s+\d", stripped) or stripped.startswith("## "):
                break
        m_h4 = _RE_H4.match(stripped)
        if m_h4:
            current_site = canonical_site_name(m_h4.group("name").strip())
            continue
        if not current_site:
            continue
        m_b = _RE_BULLET.match(stripped)
        if not m_b:
            continue
        body = m_b.group("body").strip()
        lead = ""
        rest = body
        m_lead = _RE_BULLET_LEAD.match(body)
        if m_lead:
            lead = m_lead.group("lead").strip().rstrip(":")
            rest = m_lead.group("rest").strip()
        title = lead or (body if len(body) <= 80 else body[:77] + "…")
        low = body.lower()
        if ("no standard" in low or "no major" in low or "no promotions" in low
                or ("no " in low and "promo" in low and "identified" in low)
                or "check the app" in low and "no " in low):
            promo_type = "watchlist"
        elif ("boost" in low or "enhanced odds" in low or "power price" in low
              or "epic boost" in low or "super boost" in low):
            promo_type = "boost"
        else:
            promo_type = "ongoing"
        out.append(
            {
                "site": current_site,
                "title": title,
                "description": rest or body,
                "promo_type": promo_type,
                "terms": body,
                "url": "",
            }
        )
    return out


def seed_from_recon(
    conn: sqlite3.Connection,
    path: str = "docs/recon/uk_books.md",
    now_utc: Optional[str] = None,
) -> Dict[str, int]:
    """Seed the catalog from the hand-verified recon markdown. Idempotent.

    Parses:

    * **section 2a** (the sign-up table) into ``promo_type='signup'`` rows,
      stashing min-stake / min-odds / free-bet value / expiry / promo code in the
      ``terms`` field as a ``key=value`` blob so :func:`signup_offers` can later
      reconstruct the structured columns the site needs; and
    * **section 2b** (the ``#### <Book>`` bullet lists) into ``promo_type='ongoing'``
      / ``'boost'`` / ``'watchlist'`` rows.

    All rows are inserted with ``source='seed'``. Idempotency comes from the
    ``fingerprint`` UNIQUE constraint: re-seeding bumps ``last_seen_utc`` of an
    existing seed rather than duplicating it. Seeds are NEVER auto-removed by a
    scrape (see :func:`diff_and_upsert`). A missing recon file is not fatal —
    we return zero counts.

    Returns ``{"signup": n, "ongoing": n}`` counts of rows created-or-refreshed.
    """
    now = now_utc or _now_utc()
    counts = {"signup": 0, "ongoing": 0}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return counts

    lines = text.splitlines()

    # Locate the 2a / 2b sub-section starts (tolerant of heading drift).
    start_2a = start_2b = None
    for i, line in enumerate(lines):
        low = line.lower()
        if start_2a is None and low.startswith("### 2a"):
            start_2a = i
        if start_2b is None and low.startswith("### 2b"):
            start_2b = i

    signup_candidates: List[Dict[str, Any]] = []
    if start_2a is not None:
        for row in _parse_signup_table(lines, start_2a):
            min_stake, min_odds = _split_min_bet_odds(row.get("min_bet_odds", ""))
            terms = _encode_signup_terms(
                min_stake=min_stake,
                min_odds=min_odds,
                free_bet_value=row.get("free_bet_value", ""),
                expiry=row.get("expiry", ""),
                promo_code=row.get("promo_code", ""),
            )
            signup_candidates.append(
                {
                    "site": canonical_site_name(row["bookmaker"]),
                    "title": row["offer"][:120],
                    "description": row["offer"],
                    "promo_type": "signup",
                    "terms": terms,
                    "url": "",
                }
            )

    ongoing_candidates: List[Dict[str, Any]] = []
    if start_2b is not None:
        ongoing_candidates = _parse_2b_sections(lines, start_2b)

    for cand in signup_candidates:
        if _upsert_seed_row(conn, cand, now):
            counts["signup"] += 1
    for cand in ongoing_candidates:
        if _upsert_seed_row(conn, cand, now):
            counts["ongoing"] += 1

    conn.commit()
    return counts


def _upsert_seed_row(
    conn: sqlite3.Connection, cand: Dict[str, Any], now_utc: str
) -> bool:
    """Insert (or refresh last_seen of) one seed row. Returns True if created."""
    site = cand["site"]
    title = cand.get("title") or ""
    desc = cand.get("description") or ""
    fp = fingerprint(site, title, desc)
    row = conn.execute(
        "SELECT id FROM promotions WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE promotions SET last_seen_utc = ?, terms = ?, "
            "promo_type = ? WHERE id = ?",
            (now_utc, cand.get("terms") or "", cand.get("promo_type") or "ongoing",
             row["id"]),
        )
        return False
    conn.execute(
        "INSERT INTO promotions "
        "(site, promo_type, title, description, terms, url, fingerprint, "
        " first_seen_utc, last_seen_utc, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'seed')",
        (site, cand.get("promo_type") or "ongoing", title, desc,
         cand.get("terms") or "", cand.get("url") or "", fp, now_utc, now_utc),
    )
    return True


# --- structured terms blob for sign-up rows --------------------------------

_SIGNUP_TERM_KEYS = ("min_stake", "min_odds", "free_bet_value", "expiry", "promo_code")


def _encode_signup_terms(**kw: str) -> str:
    """Encode sign-up structured fields into a ``k=v; k=v`` terms blob.

    Stored in ``promotions.terms`` so the site's structured ``signup_offers``
    rows can be reconstructed without a separate table. Empty values are kept (as
    empty strings) so the key order is stable and round-trips cleanly.
    """
    return "; ".join("%s=%s" % (k, (kw.get(k) or "").strip()) for k in _SIGNUP_TERM_KEYS)


def _decode_signup_terms(terms: Optional[str]) -> Dict[str, str]:
    """Decode a ``k=v; k=v`` sign-up terms blob back into a dict."""
    out: Dict[str, str] = {k: "" for k in _SIGNUP_TERM_KEYS}
    if not terms:
        return out
    for part in terms.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k in out:
            out[k] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Boost evaluations
# ---------------------------------------------------------------------------


def record_boost_eval(
    conn: sqlite3.Connection,
    *,
    ts_utc: str,
    site: Optional[str],
    fixture: Optional[str],
    market: Optional[str],
    selection: Optional[str],
    boosted_odds: Optional[float],
    was_odds: Optional[float],
    model_prob: Optional[float],
    fair_odds: Optional[float],
    edge: Optional[float],
    is_plus_ev: bool,
    priceable: bool,
    reason: Optional[str],
    source: str,
    pushed: int = 0,
) -> int:
    """Insert one boost evaluation row and return its id.

    Stores both *priceable* boosts (we could parse the price and the boost
    engine scored it) and *unpriceable* ones (we saw the boost but couldn't get
    a number — ``priceable=False`` with a ``reason``), so the site can show the
    full boost stream honestly rather than only the ones we could grade.
    """
    cur = conn.execute(
        "INSERT INTO boost_evals "
        "(ts_utc, site, fixture, market, selection, boosted_odds, was_odds, "
        " model_prob, fair_odds, edge, is_plus_ev, priceable, reason, source, pushed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts_utc, site, fixture, market, selection,
            _opt_float(boosted_odds), _opt_float(was_odds),
            _opt_float(model_prob), _opt_float(fair_odds), _opt_float(edge),
            1 if is_plus_ev else 0, 1 if priceable else 0,
            reason, source, int(pushed),
        ),
    )
    conn.commit()
    return cur.lastrowid


def mark_boost_pushed(conn: sqlite3.Connection, eval_ids: Sequence[int]) -> None:
    """Flag boost-eval rows as pushed so they are never re-alerted."""
    if not eval_ids:
        return
    conn.executemany(
        "UPDATE boost_evals SET pushed = 1 WHERE id = ?", [(i,) for i in eval_ids]
    )
    conn.commit()


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reader views (used by wca.promosdata.build_promos_data)
# ---------------------------------------------------------------------------


def active_promotions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """All ``status='active'`` promotions, ordered for stable output.

    Ordered by site then promo_type then id so the feed builder's output is
    deterministic for a given DB state.
    """
    return conn.execute(
        "SELECT * FROM promotions WHERE status = 'active' "
        "ORDER BY site, promo_type, id"
    ).fetchall()


def active_boost_promotions(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Active ``promo_type='boost'`` promotions, ANY source (scrape or seed).

    Historically only *scraped* boost candidates ever reached
    :func:`wca.wca_promosd._grade_boost` (the diff/upsert path in the daemon),
    so a book whose scraper never comes back ``ok`` (the common case — most
    hubs are JS-rendered SPAs, see the module docstring) never produced a
    single ``boost_evals`` row even when its recon-seeded catalog correctly
    lists a "Power Prices" / "Super Boost" style row. This view is what lets
    the daemon grade — and thus honestly report on — those seed rows too, not
    just live-scraped ones.
    """
    return conn.execute(
        "SELECT * FROM promotions WHERE status = 'active' AND promo_type = 'boost' "
        "ORDER BY site, id"
    ).fetchall()


def graded_fingerprints_today(conn: sqlite3.Connection, today_utc: str) -> set:
    """Fingerprints of promotions already graded (any ``boost_evals`` row) today.

    ``today_utc`` is a ``YYYY-MM-DD`` prefix; a boost eval's ``fixture`` field
    doesn't carry the source fingerprint directly, so we match on the natural
    key the daemon controls: ``site`` + first 120 chars of ``selection`` (how
    :func:`wca.wca_promosd._grade_seed_boost` derives the eval's selection from
    a promotion's title). This is a best-effort dedup to stop the same static
    seed description being re-inserted every single cycle — it is NOT a
    uniqueness constraint (a duplicate insert is harmless, just noisy), so an
    imperfect match here fails open (re-grades) rather than silently dropping
    a genuinely new boost.
    """
    rows = conn.execute(
        "SELECT DISTINCT site, selection FROM boost_evals WHERE ts_utc LIKE ?",
        (today_utc + "%",),
    ).fetchall()
    return {(r["site"], r["selection"]) for r in rows}


def signup_offers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Active sign-up offers projected into the site's structured shape.

    Reconstructs ``{site, offer, min_odds, min_stake, free_bet_value, expiry,
    promo_code, url}`` from the ``promo_type='signup'`` rows (the structured
    fields were stashed in ``terms`` at seed time by :func:`_encode_signup_terms`).
    """
    rows = conn.execute(
        "SELECT * FROM promotions WHERE status = 'active' AND promo_type = 'signup' "
        "ORDER BY site, id"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        t = _decode_signup_terms(r["terms"])
        out.append(
            {
                "site": r["site"],
                "offer": r["description"] or r["title"] or "",
                "min_odds": t.get("min_odds", ""),
                "min_stake": t.get("min_stake", ""),
                "free_bet_value": t.get("free_bet_value", ""),
                "expiry": t.get("expiry", ""),
                "promo_code": t.get("promo_code", ""),
                "url": r["url"] or "",
            }
        )
    return out


def latest_snapshot_per_site(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    """The most recent ``promo_snapshots`` row per site (by ts_utc, then id)."""
    rows = conn.execute(
        "SELECT * FROM promo_snapshots ORDER BY site, ts_utc, id"
    ).fetchall()
    latest: Dict[str, sqlite3.Row] = {}
    for r in rows:
        # rows ascend by ts_utc/id, so the last seen per site wins.
        latest[r["site"]] = r
    return latest


def latest_ok_snapshot_per_site(conn: sqlite3.Connection) -> Dict[str, str]:
    """Map site -> ts_utc of its most recent ``fetch_status='ok'`` snapshot."""
    rows = conn.execute(
        "SELECT site, ts_utc FROM promo_snapshots WHERE fetch_status = 'ok' "
        "ORDER BY site, ts_utc, id"
    ).fetchall()
    out: Dict[str, str] = {}
    for r in rows:
        out[r["site"]] = r["ts_utc"]
    return out


def recent_boost_evals(conn: sqlite3.Connection, limit: int = 50) -> List[sqlite3.Row]:
    """The most recent boost-eval rows, newest first (capped at *limit*)."""
    return conn.execute(
        "SELECT * FROM boost_evals ORDER BY ts_utc DESC, id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
