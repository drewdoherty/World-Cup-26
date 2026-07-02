"""Polymarket orderflow ingestion — who traded what, when, at what price.

WHY this exists
---------------
The project's Polymarket capture so far is *price*-level (``pm_snapshots``,
CLOB ``prices-history``): it can say a market moved, never **who moved it**.
The public data-api ``/trades`` feed exposes every taker fill with the proxy
wallet, side, size and price — enough to build wallet-level P&L, smart/dumb
cohorts, whale tracking and news-latency analytics on the exact markets we
trade (advancement rungs, 1X2, tournament futures). That feed is capped at
3,500 rows per market filter (offset ceiling 3000, ``before``/``after``
ignored), so the history decays; this module exists to sweep it into a local
sqlite (``data/pm_orderflow.db``) repeatedly and incrementally before it
scrolls out of the window.

Scope (what gets ingested)
--------------------------
All 2026 FIFA World Cup **team-level** markets:

* advancement rungs — advance-to-knockouts (R32), reach R16 / QF / SF / Final;
* tournament winner + group winners (A–L);
* match 1X2 — the bare ``fifwc-<home>-<away>-<date>`` full-match events
  (team-win + draw markets), closed and open;
* other team-level futures — continent winner, furthest-advancing /
  worst-placed confederation ladders, stage-of-elimination, group runner-up /
  last place, qualification.

Player props and novelty markets (announcer-says, records-broken, player goal
counts, squads, halftime shows, ...) are deliberately excluded: their flow says
nothing about the team-outcome markets we price.

Design notes
------------
* Discovery unions the gamma tags the WC events actually carry
  (``fifa-world-cup`` is the one the ``fifwc-`` match events use; the winner /
  futures events also carry ``2026-fifa-world-cup`` / ``wc-tournament-futures``)
  and de-dupes by ``conditionId``. Categorisation is an explicit slug
  allowlist — an unknown event is *dropped*, never guessed into scope.
* Trades are keyed ``UNIQUE(tx_hash, wallet, asset, side, size, price, ts)``
  and inserted with ``INSERT OR IGNORE`` so re-runs are idempotent; a rerun
  stops paging a market as soon as a full page yields zero new rows. The API
  exposes no fill id and serialises the SAME fill with different float noise
  depending on the filter used (seen live: price ``0.31`` on the plain sweep
  vs ``0.309999999998807`` on a CASH sweep), so ``size``/``price`` are rounded
  to a canonical grid before insert — otherwise every fill reachable through
  two filters double-counts.
* Markets that still have unseen fills at the offset-3000 ceiling are marked
  ``truncated`` in ``pm_ingest_log`` and additionally swept with the
  ``filterType=CASH&filterAmount=100/500`` large-trade filters, which reach
  deeper history for the whale cohort even when small fills are lost.
* **Open-only mode (hourly refresh).** Trades only happen while a market is
  OPEN; once closed the tape is frozen. Every ``pm_ingest_log`` row records
  ``market_closed`` — the market's closed state *at sweep time* — so
  ``run(open_only=True)`` can skip a market IFF it is closed now AND its most
  recent log row has ``market_closed=1`` (its frozen tape already got a full
  sweep after close). A market that flipped closed since its last sweep still
  gets exactly one final sweep; never-swept markets always sweep. A sweep
  that FAILS (transport error, not the offset cap) is stamped
  ``market_closed=0`` + ``failed=1`` whatever discovery saw, so a failed
  final sweep is retried next run instead of freezing a capture gap. Discovery
  still runs every time (refreshes closed flags, finds newly listed events),
  so an hourly run touches ~the open markets instead of the whole universe
  without ever losing fills to the offset cap.
* Never touches ``data/wca.db``. Own sqlite file, own schema, read-only on
  every Polymarket endpoint.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (research; contact via GitHub)",
    "Accept": "application/json",
}
_TIMEOUT = 30
#: Polite inter-request pause (seconds).
REQUEST_SLEEP = 0.12
#: data-api hard caps: max page size, max offset (offset>3000 -> error).
TRADES_PAGE_LIMIT = 500
TRADES_MAX_OFFSET = 3000
#: CASH-filter sweep thresholds (USD) for markets that hit the offset ceiling.
CASH_SWEEP_AMOUNTS = (100, 500)
#: Canonical rounding applied to size/price before insert. The dedupe key is
#: UNIQUE(tx_hash, wallet, asset, side, size, price, ts) and the data-api has
#: no fill id, but it serialises the same fill with different float noise per
#: filter — rounding makes the key compare canonical values. Polymarket's
#: price tick is 0.001 and sizes are 6dp, so 4/6 decimals are lossless.
PRICE_DECIMALS = 4
SIZE_DECIMALS = 6

#: Gamma tags that 2026-WC events actually carry (verified 2026-07-02: the
#: ``fifwc-*`` match events are tagged ``fifa-world-cup``; winner/futures also
#: carry ``2026-fifa-world-cup`` / ``wc-tournament-futures``; team props carry
#: ``wc-team-props``; the advancement/group events carry ``world-cup``).
DISCOVERY_TAGS: Tuple[str, ...] = (
    "world-cup",
    "fifa-world-cup",
    "2026-fifa-world-cup",
    "wc-tournament-futures",
    "wc-team-props",
)

# ---------------------------------------------------------------------------
# Categorisation (slug allowlist)
# ---------------------------------------------------------------------------

#: Ancillary per-fixture event slugs (mirrors wca.data.polymarket_odds — the
#: 2026-06-29 phantom-edge bug taught us these reuse team names as market
#: titles and must never be admitted next to the bare full-match event).
_AUX_SLUG_MARKERS: Tuple[str, ...] = (
    "halftime-result",
    "second-half-result",
    "exact-score",
    "more-markets",
    "total-corners",
    "total-goals",
    "player-props",
    "first-to-score",
    "both-teams-to-score",
    "double-chance",
)

#: Bare full-match event slug: ``fifwc-<home>-<away>-2026-MM-DD``.
_MATCH_SLUG_RE = re.compile(r"^fifwc-[a-z0-9]+(?:-[a-z0-9]+)*-2026-\d{2}-\d{2}$")

#: Advancement rung event slug -> category.
_ADVANCEMENT_SLUGS: Tuple[Tuple[str, str], ...] = (
    ("world-cup-team-to-advance-to-knockout-stages", "advancement_r32"),
    ("world-cup-nation-to-reach-round-of-16", "advancement_r16"),
    ("world-cup-nation-to-reach-quarterfinals", "advancement_qf"),
    ("world-cup-nation-to-reach-semifinals", "advancement_sf"),
    ("world-cup-nation-to-reach-final", "advancement_final"),
)

_GROUP_WINNER_RE = re.compile(r"^world-cup-group-[a-l]-winner(?:-\d+)?$")

#: Team-level tournament futures admitted as ``other_future``. Outcome /
#: progression / placement futures only — team *stat* props (highest-scoring,
#: clean sheets, ...) and everything player/novelty stay out.
_OTHER_FUTURE_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"^world-cup-[a-z-]+-stage-of-elimination(?:-\d+)?$"),
    re.compile(r"^world-cup-furthest-advancing-[a-z-]+$"),
    re.compile(r"^world-cup-worst-placed-[a-z-]+(?:-\d+)?$"),
    re.compile(r"^which-continent-will-win-the-world-cup$"),
    re.compile(r"^world-cup-group-[a-l]-(?:second|last)-place(?:-\d+)?$"),
    re.compile(r"^world-cup-group-of-champion(?:-\d+)?$"),
    re.compile(r"^world-cup-third-place-teams-to-advance(?:-\d+)?$"),
    re.compile(r"^2026-fifa-world-cup-which-countries-qualify$"),
    re.compile(r"^uefa-wc-qualifying-group-[a-z]-winner$"),
    re.compile(r"^fifa-world-cup-uefa-group-[a-z]-winner$"),
    re.compile(r"^fifa-world-cup-2026-qualification-longshots-parlay$"),
    re.compile(r"^which-team-will-replace-iran-at-world-cup$"),
    re.compile(r"^will-iran-play-in-the-world-cup$"),
    re.compile(r"^world-cup-will-[a-z-]+-play-[a-z-]+(?:-\d+)?$"),
    re.compile(r"^will-a-nation-that-has-never-won-the-world-cup[a-z0-9-]*$"),
    re.compile(r"^world-cup-unbeaten-champion$"),
)

#: groupItemTitles that are confederations / continents, not teams.
_NON_TEAM_GROUPS = {
    "europe", "south america", "north america", "central america", "africa",
    "asia", "oceania", "uefa", "conmebol", "concacaf", "caf", "afc", "ofc",
    "other", "field", "another team", "other team",
}


def categorize_event(event: Dict[str, Any]) -> Optional[str]:
    """Bucket a gamma event into the orderflow category enum, or ``None``.

    Allowlist by slug: only event families we can defend as *team-level* 2026
    World Cup markets return a category; everything else (player props,
    novelty, non-2026, other sports) returns ``None`` and is dropped.
    """
    slug = str(event.get("slug") or "").lower()
    if not slug or "cricket" in slug:
        return None
    # 2026-only guard: match slugs embed the 2026 date; futures/qualification
    # events must end 2025+ (kills the 2018/2022 events under fifa-world-cup).
    end = str(event.get("endDate") or event.get("startDate") or "")
    year = end[:4]
    if year and year.isdigit() and int(year) < 2025 and "2026" not in slug:
        return None
    if any(marker in slug for marker in _AUX_SLUG_MARKERS):
        return None
    if _MATCH_SLUG_RE.match(slug):
        return "match_1x2"
    for prefix, cat in _ADVANCEMENT_SLUGS:
        if slug.startswith(prefix):
            return cat
    if slug == "world-cup-winner":
        return "winner"
    if _GROUP_WINNER_RE.match(slug):
        return "group_winner"
    for pat in _OTHER_FUTURE_RES:
        if pat.match(slug):
            return "other_future"
    return None


# ---------------------------------------------------------------------------
# Team extraction
# ---------------------------------------------------------------------------

_Q_WIN_ON_RE = re.compile(r"^will (.+?) win on \d{4}-\d{2}-\d{2}\?$", re.I)
_Q_GENERIC_RE = re.compile(
    r"^will (.+?) (?:win|reach|advance|qualify|be eliminated|make)\b", re.I
)
_ELIM_TITLE_RE = re.compile(r"(?:world cup:?\s*)?(.+?)\s+stage of elimination", re.I)


_CONFED_WORD_RE = re.compile(r"\b(uefa|conmebol|concacaf|caf|afc|ofc|ocf)\b", re.I)


def _canonical_team(name: str) -> Optional[str]:
    """Normalise a candidate team name; ``None`` for confeds / junk."""
    name = (name or "").strip()
    if not name or name.lower() in _NON_TEAM_GROUPS or _CONFED_WORD_RE.search(name):
        return None
    try:
        from wca.data.teamnames import canonical

        return canonical(name)
    except Exception:  # pragma: no cover - teamnames is stdlib-only, but be safe
        return name


def extract_team(category: str, event: Dict[str, Any], market: Dict[str, Any]) -> Optional[str]:
    """Normalised team for one market (``'Draw'`` for the 1X2 draw leg).

    Resolution order: draw detection, ``groupItemTitle`` (the per-team market
    label Polymarket uses inside grouped events), then question / event-title
    regexes. ``None`` when nothing team-shaped is derivable (contract allows
    NULL, never a guess).
    """
    question = str(market.get("question") or "")
    git = str(market.get("groupItemTitle") or "").strip()
    if category == "match_1x2":
        if git.lower().startswith("draw") or "end in a draw" in question.lower():
            return "Draw"
        if git:
            return _canonical_team(git)
        m = _Q_WIN_ON_RE.match(question.strip())
        return _canonical_team(m.group(1)) if m else None
    if category == "other_future":
        m = _ELIM_TITLE_RE.search(str(event.get("title") or ""))
        if m:
            return _canonical_team(m.group(1))
    if git:
        return _canonical_team(git)
    m = _Q_GENERIC_RE.match(question.strip())
    return _canonical_team(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, *, retries: int = 5) -> Any:
    """GET *url* returning parsed JSON, with backoff on 429/5xx/transport errors."""
    delay = 1.0
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == retries - 1:
                raise
            logger.warning("GET %s failed (%s); retrying in %.0fs", url, exc, delay)
            time.sleep(delay)
            delay *= 2
    return None  # pragma: no cover


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pm_markets (
  condition_id TEXT PRIMARY KEY,
  event_slug TEXT, market_slug TEXT, question TEXT, event_title TEXT,
  category TEXT,
  team TEXT,
  outcomes TEXT,
  token_ids TEXT,
  closed INTEGER NOT NULL DEFAULT 0,
  resolved_outcome_index INTEGER,
  end_date TEXT, game_start_time TEXT,
  volume REAL, liquidity REAL, fetched_utc TEXT
);
CREATE TABLE IF NOT EXISTS pm_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  condition_id TEXT NOT NULL, asset TEXT NOT NULL,
  outcome TEXT, outcome_index INTEGER,
  wallet TEXT NOT NULL, name TEXT, pseudonym TEXT,
  side TEXT NOT NULL,
  size REAL NOT NULL,
  price REAL NOT NULL,
  usd REAL NOT NULL,
  ts INTEGER NOT NULL,
  tx_hash TEXT,
  UNIQUE(tx_hash, wallet, asset, side, size, price, ts)
);
CREATE INDEX IF NOT EXISTS idx_trades_cid_ts ON pm_trades(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON pm_trades(wallet);
CREATE TABLE IF NOT EXISTS pm_ingest_log (
  condition_id TEXT, run_utc TEXT, n_fetched INTEGER, n_new INTEGER, truncated INTEGER,
  market_closed INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0
);
"""


#: Serialises all sqlite writes when ingesting with a thread pool (fetches
#: are parallel — the network is the bottleneck — writes are cheap and brief).
_DB_LOCK = threading.Lock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the orderflow sqlite at *db_path*."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.executescript(_SCHEMA)
    # Migration: dbs created before the open-only refresh lack the
    # market_closed column on pm_ingest_log (CREATE IF NOT EXISTS never
    # retrofits it). Pre-migration rows default 0 = "state at sweep time
    # unknown / open", which safely forces one more full sweep of every
    # closed market before the open-only skip can kick in.
    cols = {r[1] for r in con.execute("PRAGMA table_info(pm_ingest_log)")}
    if "market_closed" not in cols:
        con.execute(
            "ALTER TABLE pm_ingest_log "
            "ADD COLUMN market_closed INTEGER NOT NULL DEFAULT 0"
        )
    # Migration: failed=1 marks a sweep that errored out mid-run (transport
    # failure / bad response) — as opposed to one that merely hit the offset
    # cap. Pre-migration rows default 0 = "believed successful", which is
    # what they were treated as when written.
    if "failed" not in cols:
        con.execute(
            "ALTER TABLE pm_ingest_log ADD COLUMN failed INTEGER NOT NULL DEFAULT 0"
        )
    con.commit()
    return con


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _json_array(raw: Any) -> List[Any]:
    """Decode Polymarket's JSON-string-encoded arrays, tolerantly."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _resolved_index(closed: bool, prices: List[Any]) -> Optional[int]:
    """Index of the outcome that settled at ~$1, else ``None``.

    A resolved market shows one outcomePrice >= 0.99; anything ambiguous
    (zero or multiple near-1 prices, unresolved-but-closed) stays NULL.
    """
    if not closed or not prices:
        return None
    winners = []
    for i, p in enumerate(prices):
        try:
            if float(p) >= 0.99:
                winners.append(i)
        except (TypeError, ValueError):
            continue
    return winners[0] if len(winners) == 1 else None


def _iter_tag_events(tag: str, closed: str) -> Iterable[Dict[str, Any]]:
    """Paginate gamma ``/events`` for one tag/closed combination."""
    offset = 0
    while True:
        data = _get_json(
            GAMMA_BASE + "/events",
            params={"tag_slug": tag, "closed": closed, "limit": 100, "offset": offset},
        )
        page = data if isinstance(data, list) else (data or {}).get("data", [])
        if not page:
            return
        for event in page:
            yield event
        if len(page) < 100:
            return
        offset += 100
        time.sleep(REQUEST_SLEEP)


def discover_markets() -> List[Dict[str, Any]]:
    """Enumerate every in-scope 2026-WC market across the discovery tags.

    Returns one dict per market (de-duped by ``conditionId``) shaped like a
    ``pm_markets`` row. Events that fail :func:`categorize_event` are dropped.
    """
    seen_events: set = set()
    markets: Dict[str, Dict[str, Any]] = {}
    fetched = _now_utc()
    for tag in DISCOVERY_TAGS:
        for closed in ("false", "true"):
            for event in _iter_tag_events(tag, closed):
                ev_key = event.get("slug") or event.get("id")
                if ev_key in seen_events:
                    continue
                seen_events.add(ev_key)
                category = categorize_event(event)
                if category is None:
                    continue
                for m in event.get("markets") or []:
                    cid = m.get("conditionId")
                    if not cid or cid in markets:
                        continue
                    outcomes = _json_array(m.get("outcomes"))
                    prices = _json_array(m.get("outcomePrices"))
                    token_ids = [str(t) for t in _json_array(m.get("clobTokenIds"))]
                    m_closed = bool(m.get("closed"))
                    try:
                        volume = float(m.get("volume") or 0.0)
                    except (TypeError, ValueError):
                        volume = 0.0
                    try:
                        liquidity = float(m.get("liquidity") or 0.0)
                    except (TypeError, ValueError):
                        liquidity = 0.0
                    markets[cid] = {
                        "condition_id": cid,
                        "event_slug": event.get("slug"),
                        "market_slug": m.get("slug"),
                        "question": m.get("question"),
                        "event_title": event.get("title"),
                        "category": category,
                        "team": extract_team(category, event, m),
                        "outcomes": json.dumps(outcomes),
                        "token_ids": json.dumps(token_ids),
                        "closed": 1 if m_closed else 0,
                        "resolved_outcome_index": _resolved_index(m_closed, prices),
                        "end_date": m.get("endDate") or event.get("endDate"),
                        "game_start_time": m.get("gameStartTime"),
                        "volume": volume,
                        "liquidity": liquidity,
                        "fetched_utc": fetched,
                    }
            time.sleep(REQUEST_SLEEP)
    logger.info("discovered %d in-scope markets from %d events", len(markets), len(seen_events))
    return list(markets.values())


_MARKET_COLS = (
    "condition_id", "event_slug", "market_slug", "question", "event_title",
    "category", "team", "outcomes", "token_ids", "closed",
    "resolved_outcome_index", "end_date", "game_start_time", "volume",
    "liquidity", "fetched_utc",
)


def upsert_markets(con: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    """Insert-or-update market rows; returns the number processed."""
    sql = (
        "INSERT INTO pm_markets (%s) VALUES (%s) "
        "ON CONFLICT(condition_id) DO UPDATE SET %s"
        % (
            ", ".join(_MARKET_COLS),
            ", ".join("?" for _ in _MARKET_COLS),
            ", ".join(f"{c}=excluded.{c}" for c in _MARKET_COLS if c != "condition_id"),
        )
    )
    n = 0
    for row in rows:
        con.execute(sql, tuple(row.get(c) for c in _MARKET_COLS))
        n += 1
    con.commit()
    return n


# ---------------------------------------------------------------------------
# Trade ingestion
# ---------------------------------------------------------------------------


def _insert_trades(con: sqlite3.Connection, condition_id: str, page: List[Dict[str, Any]]) -> int:
    """INSERT OR IGNORE one page of data-api trades; returns rows actually added.

    Thread-safe: takes the module write-lock (pool workers share *con*).
    """
    with _DB_LOCK:
        return _insert_trades_locked(con, condition_id, page)


def _insert_trades_locked(con: sqlite3.Connection, condition_id: str, page: List[Dict[str, Any]]) -> int:
    n_new = 0
    for t in page:
        try:
            # Canonical grid (see PRICE_DECIMALS/SIZE_DECIMALS): the UNIQUE
            # key must compare identical values for the same fill regardless
            # of which /trades filter served it.
            size = round(float(t.get("size") or 0.0), SIZE_DECIMALS)
            price = round(float(t.get("price") or 0.0), PRICE_DECIMALS)
            ts = int(t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        wallet = t.get("proxyWallet") or ""
        side = t.get("side") or ""
        asset = str(t.get("asset") or "")
        if not wallet or not side or not asset or ts <= 0:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO pm_trades "
            "(condition_id, asset, outcome, outcome_index, wallet, name, pseudonym,"
            " side, size, price, usd, ts, tx_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.get("conditionId") or condition_id,
                asset,
                t.get("outcome"),
                t.get("outcomeIndex"),
                wallet,
                t.get("name") or None,
                t.get("pseudonym") or None,
                side,
                size,
                price,
                round(size * price, 6),
                ts,
                t.get("transactionHash"),
            ),
        )
        n_new += cur.rowcount if cur.rowcount > 0 else 0
    return n_new


def _page_trades(
    con: sqlite3.Connection,
    condition_id: str,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, bool, bool]:
    """Sweep one /trades filter for *condition_id* across the offset window.

    Returns ``(n_fetched, n_new, exhausted, failed)``:

    * ``exhausted`` is False when the sweep stopped short of the natural end
      of history — either the offset-3000 ceiling was hit while full pages
      were still producing new rows (deeper history exists but is unreachable
      through this filter) or a request failed mid-sweep.
    * ``failed`` is True ONLY for the transport-failure / bad-response case.
      The distinction matters downstream: retrying a *capped* frozen tape
      recovers nothing (``failed=False``), while retrying after an outage
      recovers everything still in the window (``failed=True``) — so
      :func:`ingest_trades` must not stamp a failed sweep of a closed market
      ``market_closed=1`` (final) or the open-only skip would drop those
      fills forever.
    """
    n_fetched = 0
    n_new = 0
    for offset in range(0, TRADES_MAX_OFFSET + 1, TRADES_PAGE_LIMIT):
        params: Dict[str, Any] = {
            "market": condition_id,
            "limit": TRADES_PAGE_LIMIT,
            "offset": offset,
        }
        if extra_params:
            params.update(extra_params)
        try:
            data = _get_json(DATA_API_BASE + "/trades", params=params)
        except requests.RequestException as exc:
            # Mid-sweep failure: unswept depth may remain. exhausted=False
            # makes ingest_trades flag the market truncated=1 (disclosed as
            # incomplete + CASH-swept) instead of logging a complete run —
            # a truncated=0 log here would let the incremental early-stop
            # silently skip the never-fetched deeper pages forever.
            logger.warning("trades fetch failed for %s offset %d: %s", condition_id, offset, exc)
            return n_fetched, n_new, False, True
        if not isinstance(data, list):  # {"error": ...} or unexpected shape
            logger.warning("non-list trades response for %s offset %d: %r", condition_id, offset, data)
            return n_fetched, n_new, False, True  # same: do not claim completeness
        page_new = _insert_trades(con, condition_id, data)
        n_fetched += len(data)
        n_new += page_new
        time.sleep(REQUEST_SLEEP)
        if len(data) < TRADES_PAGE_LIMIT:
            return n_fetched, n_new, True, False  # natural end of history
        if page_new == 0:
            return n_fetched, n_new, True, False  # incremental rerun: hit known rows
        if offset == TRADES_MAX_OFFSET:
            return n_fetched, n_new, False, False  # ceiling hit, still finding new rows
    return n_fetched, n_new, True, False  # pragma: no cover


def ingest_trades(
    con: sqlite3.Connection, condition_id: str, *, market_closed: int = 0
) -> Tuple[int, int, int]:
    """Ingest all reachable taker fills for one market.

    Primary unfiltered sweep first; if it exhausts the offset window while
    still producing new rows — or fails mid-sweep, leaving unknown depth —
    the market is *truncated*: sweep the CASH filters to recover deeper
    large-trade history, and log ``truncated=1`` so the history is disclosed
    as incomplete rather than silently logged complete.

    *market_closed* is the market's closed flag as observed by the discovery
    pass that scheduled this sweep; it is stamped onto the ``pm_ingest_log``
    row so open-only runs can tell "swept while still open (may have gained
    fills since)" from "swept after close (tape frozen, capture final)".

    Failure path (guaranteed-final-sweep invariant): when any sweep FAILED
    (transport error / bad response — as opposed to merely hitting the
    offset cap on a frozen tape) the log row is stamped ``market_closed=0``
    and ``failed=1`` regardless of the discovery flag. A failed sweep of a
    newly-closed market must not count as its one final sweep — the tape is
    frozen but still readable, so the next open-only run retries it instead
    of skipping it forever. The genuine cap-only case keeps
    ``market_closed=1``: retrying a capped frozen tape recovers nothing.
    Returns ``(n_fetched, n_new, truncated)`` and writes a ``pm_ingest_log`` row.
    """
    n_fetched, n_new, exhausted, failed = _page_trades(con, condition_id)
    truncated = 0
    if not exhausted:
        truncated = 1
        for amount in CASH_SWEEP_AMOUNTS:
            f, n, _, cash_failed = _page_trades(
                con, condition_id,
                {"filterType": "CASH", "filterAmount": amount},
            )
            n_fetched += f
            n_new += n
            # A failed CASH fallback also forfeits "final": on a capped
            # closed market those deep large-trade pages are only ever
            # recovered if the next run retries.
            failed = failed or cash_failed
    with _DB_LOCK:
        con.execute(
            "INSERT INTO pm_ingest_log "
            "(condition_id, run_utc, n_fetched, n_new, truncated, market_closed, failed) "
            "VALUES (?,?,?,?,?,?,?)",
            (condition_id, _now_utc(), n_fetched, n_new, truncated,
             1 if (market_closed and not failed) else 0,
             1 if failed else 0),
        )
        con.commit()
    return n_fetched, n_new, truncated


# ---------------------------------------------------------------------------
# Per-wallet backfill (leaderboard completeness)
# ---------------------------------------------------------------------------
#
# Busy ("truncated") markets lose small fills beyond the offset ceiling, so a
# featured wallet's position there can be missing legs — poisoning its PnL /
# ROI / win-rate. The /trades ``user`` filter opens a fresh 3,500-row window
# per (user[, market]) combination, so a wallet's exact history is cheaply
# recoverable for the handful of wallets the site actually renders.

#: pm_ingest_log condition_id prefix for per-wallet backfill log rows.
USER_LOG_PREFIX = "user:"


def _page_user_trades(
    con: sqlite3.Connection,
    wallet: str,
    known_cids: set,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, bool]:
    """Sweep ``/trades?user=<wallet>`` (optionally + market filter).

    Only fills whose ``conditionId`` is a known in-scope market are inserted
    (the user filter returns the wallet's flow across ALL of Polymarket).
    No zero-new early stop: this is a completeness pass — the gaps live in
    the *deeper* pages, past fills we already hold. Same return semantics as
    :func:`_page_trades` (``exhausted=False`` = window capped or failed).
    """
    n_fetched = 0
    n_new = 0
    for offset in range(0, TRADES_MAX_OFFSET + 1, TRADES_PAGE_LIMIT):
        params: Dict[str, Any] = {"user": wallet, "limit": TRADES_PAGE_LIMIT, "offset": offset}
        if extra_params:
            params.update(extra_params)
        try:
            data = _get_json(DATA_API_BASE + "/trades", params=params)
        except requests.RequestException as exc:
            logger.warning("user trades fetch failed for %s offset %d: %s", wallet, offset, exc)
            return n_fetched, n_new, False
        if not isinstance(data, list):
            logger.warning("non-list user trades response for %s offset %d: %r", wallet, offset, data)
            return n_fetched, n_new, False
        in_scope = [t for t in data if str(t.get("conditionId") or "") in known_cids]
        n_new += _insert_trades(con, "", in_scope)
        n_fetched += len(data)
        time.sleep(REQUEST_SLEEP)
        if len(data) < TRADES_PAGE_LIMIT:
            return n_fetched, n_new, True  # natural end of the wallet's history
        if offset == TRADES_MAX_OFFSET:
            return n_fetched, n_new, False  # >3,500 fills behind this filter
    return n_fetched, n_new, True  # pragma: no cover


def backfill_wallet(
    con: sqlite3.Connection, wallet: str, known_cids: set
) -> Tuple[int, int, int]:
    """Backfill one wallet's complete in-scope fill history.

    One unfiltered user sweep first; if the wallet has more platform-wide
    fills than one window holds, fall back to per-market ``user+market``
    sweeps over every in-scope market the wallet is (now) known to trade —
    each filter combination gets its own fresh offset window. Logs a
    ``pm_ingest_log`` row with ``condition_id='user:<wallet>'``;
    ``truncated=0`` means the wallet's in-scope history is believed complete
    as of the run. Returns ``(n_fetched, n_new, truncated)``.
    """
    n_fetched, n_new, exhausted = _page_user_trades(con, wallet, known_cids)
    truncated = 0
    if not exhausted:
        with _DB_LOCK:
            cids = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT condition_id FROM pm_trades WHERE wallet=?", (wallet,)
                )
            ]
        for cid in cids:
            if cid not in known_cids:
                continue
            f, n, ex = _page_user_trades(con, wallet, known_cids, {"market": cid})
            n_fetched += f
            n_new += n
            if not ex:
                truncated = 1  # >3,500 fills in ONE market — still capped
    with _DB_LOCK:
        con.execute(
            "INSERT INTO pm_ingest_log (condition_id, run_utc, n_fetched, n_new, truncated) "
            "VALUES (?,?,?,?,?)",
            (USER_LOG_PREFIX + wallet, _now_utc(), n_fetched, n_new, truncated),
        )
        con.commit()
    return n_fetched, n_new, truncated


def backfill_wallets(
    db_path: str | Path, wallets: Iterable[str]
) -> Dict[str, Any]:
    """Backfill several wallets sequentially (politeness > speed here).

    Intended for the <=60 wallets the orderflow page actually renders; the
    analytics generator treats wallets with a ``truncated=0`` user log row as
    complete and drops their ``partial_history`` flag.
    """
    con = connect(db_path)
    summary: Dict[str, Any] = {"wallets": 0, "fetched": 0, "new": 0, "still_truncated": []}
    try:
        known_cids = {r[0] for r in con.execute("SELECT condition_id FROM pm_markets")}
        for wallet in wallets:
            f, n, truncated = backfill_wallet(con, wallet, known_cids)
            summary["wallets"] += 1
            summary["fetched"] += f
            summary["new"] += n
            if truncated:
                summary["still_truncated"].append(wallet)
            logger.info("backfilled %s: fetched %d, new %d%s",
                        wallet, f, n, " (STILL capped)" if truncated else "")
    finally:
        con.close()
    return summary


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    db_path: str | Path = "data/pm_orderflow.db",
    *,
    discover_only: bool = False,
    max_markets: Optional[int] = None,
    workers: int = 8,
    resume: bool = False,
    open_only: bool = False,
) -> Dict[str, Any]:
    """Discover markets, upsert them, then ingest trades for each.

    Fetches run on a thread pool (*workers*) because the wall-clock cost is
    almost entirely data-api round-trips; sqlite writes are serialised behind
    :data:`_DB_LOCK`. With ``resume=True`` markets already present in
    ``pm_ingest_log`` are skipped and any orphan trades from a market that was
    mid-sweep when a previous run was killed are deleted first (the incremental
    early-stop would otherwise leave its deeper pages unswept forever).

    With ``open_only=True`` (the hourly refresh mode) discovery still runs in
    full — it refreshes the closed flags and picks up newly listed markets —
    but a market's trade sweep is skipped IFF it is closed now AND its most
    recent ``pm_ingest_log`` row has ``market_closed=1``: a closed market's
    tape is frozen, so once it has received one full sweep *after* close its
    capture is final (guaranteed-final-sweep). A market that flipped closed
    since its last sweep (last log row ``market_closed=0``, which includes all
    pre-migration rows) still gets exactly one final sweep; never-swept
    markets always sweep. The invariant holds on the failure path too:
    :func:`ingest_trades` stamps a FAILED sweep ``market_closed=0`` even when
    discovery saw the market closed, so a final sweep that dies mid-outage is
    retried on the next run rather than counted as final. A market can
    therefore never gain fills we skip.
    Returns a small summary dict (counts + truncated market slugs) for the CLI.
    """
    con = connect(db_path)
    markets = discover_markets()
    n_markets = upsert_markets(con, markets)
    summary: Dict[str, Any] = {"markets_discovered": n_markets, "truncated": []}
    if discover_only:
        con.close()
        return summary
    todo = sorted(markets, key=lambda m: (m["category"] or "", m["market_slug"] or ""))
    if resume:
        done = {r[0] for r in con.execute("SELECT DISTINCT condition_id FROM pm_ingest_log")}
        con.execute(
            "DELETE FROM pm_trades WHERE condition_id NOT IN "
            "(SELECT DISTINCT condition_id FROM pm_ingest_log)"
        )
        con.commit()
        todo = [m for m in todo if m["condition_id"] not in done]
        logger.info("resume: %d markets already ingested, %d to go", len(done), len(todo))
    if open_only:
        # Latest log row per market (max rowid = last inserted; the table is
        # append-only). market_closed=1 there means the tape was already
        # frozen when that sweep ran — nothing new can ever appear.
        last_closed = dict(
            con.execute(
                "SELECT condition_id, market_closed FROM pm_ingest_log "
                "WHERE rowid IN "
                "(SELECT MAX(rowid) FROM pm_ingest_log GROUP BY condition_id)"
            )
        )
        before = len(todo)
        todo = [
            m for m in todo
            if not (m["closed"] and last_closed.get(m["condition_id"]) == 1)
        ]
        summary["skipped_closed_final"] = before - len(todo)
        logger.info(
            "open-only: skipping %d closed markets already final-swept, %d to ingest",
            before - len(todo), len(todo),
        )
    if max_markets is not None:
        todo = todo[:max_markets]
    total_fetched = 0
    total_new = 0
    n_done = 0
    progress_lock = threading.Lock()

    def _one(m: Dict[str, Any]) -> None:
        nonlocal total_fetched, total_new, n_done
        n_fetched, n_new, truncated = ingest_trades(
            con, m["condition_id"], market_closed=int(m["closed"] or 0)
        )
        with progress_lock:
            total_fetched += n_fetched
            total_new += n_new
            n_done += 1
            if truncated:
                summary["truncated"].append(m.get("market_slug") or m["condition_id"])
            if n_done % 50 == 0 or n_done == len(todo):
                logger.info(
                    "ingest %d/%d markets (fetched %d, new %d)",
                    n_done, len(todo), total_fetched, total_new,
                )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, m) for m in todo]
        for fut in as_completed(futures):
            fut.result()  # re-raise worker exceptions
    summary["markets_ingested"] = len(todo)
    summary["trades_fetched"] = total_fetched
    summary["trades_new"] = total_new
    con.close()
    return summary


# ---------------------------------------------------------------------------
# Freshness (ops) — is the capture actually still capturing?
# ---------------------------------------------------------------------------
#
# launchd ignores the exit code of StartInterval jobs and the watchdog only
# monitors daemons, so a permanently failing hourly ingest (API change,
# sustained TLS block) is silent while the data-api offset window scrolls
# match-day fills away for good. These pure helpers back the
# ``--check-freshness`` gate in scripts/pm_orderflow_ingest.py (the Telegram
# I/O lives in the CLI). Decision logic mirrors wca.pm1x2snapshot's stale
# gate (not imported: that module is not on this branch).


def _parse_utc(s: str) -> datetime:
    """Parse the ISO-ish UTC stamps this module writes (trailing Z ok)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def last_successful_sweep_utc(con: sqlite3.Connection) -> Optional[str]:
    """UTC stamp of the most recent SUCCESSFUL market sweep, or None.

    "Successful" = a ``pm_ingest_log`` market row with ``failed=0`` (rows
    from dbs predating the ``failed`` column count as successful, which is
    how they were treated when written). ``user:`` backfill rows are ignored
    — they say nothing about the recurring market capture. None = the market
    sweep has never succeeded (or never ran).
    """
    try:
        row = con.execute(
            "SELECT MAX(run_utc) FROM pm_ingest_log "
            "WHERE failed=0 AND condition_id NOT LIKE ?",
            (USER_LOG_PREFIX + "%",),
        ).fetchone()
    except sqlite3.OperationalError:  # pre-'failed'-column db, opened ro
        row = con.execute(
            "SELECT MAX(run_utc) FROM pm_ingest_log WHERE condition_id NOT LIKE ?",
            (USER_LOG_PREFIX + "%",),
        ).fetchone()
    return row[0] if row and row[0] else None


def seconds_since_last_successful_sweep(
    con: sqlite3.Connection, now_iso: Optional[str] = None
) -> Optional[float]:
    """Seconds since the last successful market sweep; None if never."""
    last = last_successful_sweep_utc(con)
    if not last:
        return None
    now_dt = _parse_utc(now_iso) if now_iso else datetime.now(timezone.utc)
    return max(0.0, (now_dt - _parse_utc(last)).total_seconds())


def should_alert_stale(
    age_secs: Optional[float],
    last_alert_age_secs: Optional[float],
    threshold_secs: float,
) -> bool:
    """Whether a freshness alert should fire now (debounced, never spammy).

    * ``age_secs is None`` (never succeeded) -> always alert.
    * Below ``threshold_secs`` -> never alert.
    * Above threshold -> alert once, then only again once staleness has grown
      by another full threshold (3h, then 6h, then 9h... not every cycle).
    """
    if age_secs is None:
        return True
    if age_secs < threshold_secs:
        return False
    if last_alert_age_secs is None:
        return True
    return age_secs >= last_alert_age_secs + threshold_secs
