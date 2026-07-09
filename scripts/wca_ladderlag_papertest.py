#!/usr/bin/env python3
"""Matchday paper-trading harness for the "ladder-lag" edge falsification test.

THE EDGE UNDER TEST (measured on historical tape, n=302): after a >=10c move on
a Polymarket 1X2 token during a live match, same-team advancement-ladder tokens
(QF/SF/Final/winner rungs) kept printing within 2c of their pre-jump price for
minutes (stale quotes getting lifted), then drifted +1.9c mean (+2.2c when the
stale print came <=120s after the jump) in the jump direction over 2h; tails
6-34c.

Falsification requires LIVE evidence: at jump time, does the CLOB best ask
actually still sit near the pre-jump level (executable), or was the historical
pattern just prints against already-moved books? This harness watches live
1X2 tokens, detects jumps, and immediately snapshots the real order book on
the same-team ladder rungs to see whether a paper "buy at best ask" fill would
actually have captured the stale price.

PAPER ONLY. This module is structurally incapable of placing a real order:
it never imports ``pm/trader.py`` or anything under ``wca.pm`` that signs or
submits orders, and every network call here is a read-only public GET (CLOB
``/book`` + our own read-only ``pm_orderflow.db`` for token discovery). There
is no execution path in this file at all -- only logging of hypothetical
paper fills to a local JSONL.

Three modes
-----------
``watch``  -- matchday mode (MacBook + VPN). Polls CLOB last-trade/midpoint for
              the 1X2 tokens of today's live matches. A >=10c move within 5min
              is classified thin-print (a single print <$50 notional on
              either side of the move) or not -- thin prints still fire the
              trigger and get recorded, just flagged, since ~33% of raw jumps
              on the historical tape were exactly this. On any detected jump,
              immediately fetches full order books for every same-team ladder
              token and records a paper fill. Then keeps polling those rungs
              and records marks at +10min / +30min / +2h -- the +10min mark
              exists because a parallel historical study found ~80% of the
              ladder drift decays within the first 10 minutes, so it answers
              "was the harness's fetch latency fast enough to matter".
``mark``   -- re-marks any still-open events from the JSONL (idempotent).
``report`` -- prints a per-event table + aggregate stats (thin-print events
              excluded from the aggregate by default) stratified by
              fetch-latency bucket.

Data flow is one append-only JSONL: data/ladderlag_papertest.jsonl. Every
line is a self-contained event record; ``watch`` appends/updates records by
rewriting the file (read-modify-write, same pattern as other WCA JSONL
stores), so Ctrl-C at any point leaves a resumable, valid file -- restart
just re-reads it and continues polling any event still missing its +2h mark.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

import requests  # noqa: E402

# Read-only CLOB book fetch -- reused, not reimplemented (see pmdata.clob_book /
# pm_clob_history.top_of_book). No import of anything under wca.pm here.
from wca.data.pm_clob_history import top_of_book  # noqa: E402

logger = logging.getLogger("wca_ladderlag_papertest")

_BOOK_URL = "https://clob.polymarket.com/book"
_HEADERS = {"User-Agent": "WorldCupAlpha/0.1 (ladderlag-papertest, read-only)",
            "Accept": "application/json"}


def fetch_raw_book(token_id: str, *, timeout: float = 15.0) -> Optional[dict]:
    """Raw CLOB ``/book`` payload (bids/asks price-level arrays) for one
    token. Read-only public GET, same endpoint as ``pmdata.clob_book`` /
    ``pm_clob_history.top_of_book`` -- duplicated as a thin fetch here only
    because this harness needs the full level array (for book-walking a
    paper fill), not just the top-of-book summary. Returns ``None`` on any
    error. NEVER used for order placement -- there is no write path in this
    file."""
    if not token_id:
        return None
    try:
        resp = requests.get(_BOOK_URL, params={"token_id": str(token_id)},
                            headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # pragma: no cover - network best-effort
        logger.debug("raw book fetch failed for %s: %s", token_id, exc)
        return None
    return data if isinstance(data, dict) else None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORDERFLOW_DB = _REPO / "data" / "pm_orderflow.db"
EVENTS_JSONL = _REPO / "data" / "ladderlag_papertest.jsonl"

JUMP_THRESHOLD = 0.10          # >=10c move triggers a jump
JUMP_WINDOW_SECS = 5 * 60      # within 5 minutes
POLL_CADENCE_SECS = 5.0        # 5s cadence on 1X2 tokens during match windows
GLOBAL_RATE_CEILING_PER_SEC = 10.0  # never hammer the public endpoints
PAPER_CLIP_USD = 50.0          # walk the book up to $50 notional
#: +10min/+30min/+2h. The +10min mark was added after a parallel historical
#: study found the drift is front-loaded: full-latency (from the pre-jump
#: print) the ladder drift is +4.2c [2.5, 6.0], but from a realistic 10-minute
#: entry it is only +1.6c [-0.3, +3.4] -- ~80% of the edge is consumed within
#: 10 minutes. Reporting a +10m mark lets `report` show whether the LIVE
#: fetch-to-fill latency is fast enough to catch the move before it decays.
MARK_OFFSETS_SECS = (10 * 60, 30 * 60, 2 * 60 * 60)  # +10min, +30min, +2h
FEE_RATE = 0.03                # worst-case fee: 0.03 * p * (1-p)
MIN_EVENTS_FOR_REPORT = 20

#: A move where either side of the jump (pre or post print) was a single
#: trade below this notional is a "thin print" -- historically ~33% of
#: >=10c jumps on the tape were thin-print moves against an essentially
#: empty book, not a real repriced market. These are excluded as TRIGGERS by
#: default (see detect_jump(..., thin_print_floor_usd=...)); events derived
#: from one are flagged thin_print=True so `report` can filter them out
#: rather than silently mixing them into the aggregate.
THIN_PRINT_NOTIONAL_USD = 50.0

#: Latency buckets (upper bound, ms, exclusive) used to stratify report P&L
#: by book-fetch latency -- the question the live test has to answer is "how
#: fast is fast enough", so results are sliced by how quickly the harness
#: actually got a book snapshot after detecting the jump.
LATENCY_BUCKETS_MS = (500.0, 2000.0, 10000.0, float("inf"))

#: pm_markets.category values that count as "ladder" rungs for a team (see
#: src/wca/pm/orderflow.py categorize_event -- kept as a literal tuple here so
#: this file has zero import dependency on the orderflow ingestion module).
LADDER_CATEGORIES = (
    "advancement_r32",
    "advancement_r16",
    "advancement_qf",
    "advancement_sf",
    "advancement_final",
    "winner",
)
MATCH_1X2_CATEGORY = "match_1x2"


# ---------------------------------------------------------------------------
# Rate limiting -- simple token-bucket-ish sleep gate shared by every fetch
# ---------------------------------------------------------------------------


class RateLimiter:
    """Caps outbound requests to ``max_per_sec`` across the whole process."""

    def __init__(self, max_per_sec: float = GLOBAL_RATE_CEILING_PER_SEC):
        self.min_interval = 1.0 / max_per_sec if max_per_sec > 0 else 0.0
        self._last = 0.0

    def wait(self, *, clock: Optional[callable] = None) -> None:
        now = (clock or time.monotonic)()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = (clock or time.monotonic)()


# ---------------------------------------------------------------------------
# Token discovery (read-only pm_orderflow.db)
# ---------------------------------------------------------------------------


def _ro_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _json_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


@dataclass
class TokenRef:
    """One outcome token: which market/team/category/outcome it belongs to."""

    token_id: str
    condition_id: str
    category: str
    team: Optional[str]
    outcome: Optional[str]
    market_slug: Optional[str]
    game_start_time: Optional[str]


def discover_tokens_for_date(
    db_path: Path, date_str: str
) -> Tuple[List[TokenRef], Dict[str, List[TokenRef]]]:
    """1X2 tokens for matches starting on ``date_str`` (``YYYY-MM-DD``, UTC),
    plus a ``team -> ladder TokenRef list`` map for their same-team rungs.

    Read-only: opens ``pm_orderflow.db`` with ``PRAGMA query_only=ON``. Never
    mutates it. Returns ``([], {})`` if the db is missing (e.g. dev box).
    """
    if not db_path.exists():
        logger.warning("orderflow db not found at %s -- no tokens discovered", db_path)
        return [], {}

    cols = (
        "condition_id, category, team, outcomes, token_ids, market_slug, "
        "game_start_time"
    )
    with _ro_conn(db_path) as conn:
        rows = conn.execute(f"SELECT {cols} FROM pm_markets").fetchall()

    match_1x2: List[TokenRef] = []
    ladder_by_team: Dict[str, List[TokenRef]] = {}

    for condition_id, category, team, outcomes_raw, token_ids_raw, slug, gst in rows:
        outcomes = _json_list(outcomes_raw)
        token_ids = _json_list(token_ids_raw)
        if not token_ids:
            continue
        for idx, tok in enumerate(token_ids):
            outcome = outcomes[idx] if idx < len(outcomes) else None
            ref = TokenRef(
                token_id=tok,
                condition_id=condition_id,
                category=category or "",
                team=team,
                outcome=outcome,
                market_slug=slug,
                game_start_time=gst,
            )
            if category == MATCH_1X2_CATEGORY:
                if gst and str(gst)[:10] == date_str:
                    match_1x2.append(ref)
            elif category in LADDER_CATEGORIES and team:
                ladder_by_team.setdefault(team, []).append(ref)

    return match_1x2, ladder_by_team


def ladder_tokens_for_team(
    ladder_by_team: Dict[str, List[TokenRef]], team: Optional[str]
) -> List[TokenRef]:
    if not team:
        return []
    return list(ladder_by_team.get(team, []))


# ---------------------------------------------------------------------------
# Jump detection (pure function -- unit tested)
# ---------------------------------------------------------------------------


@dataclass
class PricePoint:
    ts: float   # epoch seconds
    price: float
    #: USD notional of the print this point came from (price * size), when
    #: sourced from an actual trade. ``None`` when sourced from a book
    #: mid/top-of-book poll rather than a trade print -- thin-print
    #: classification is skipped (not asserted False) for such points since
    #: we have no size to check.
    notional: Optional[float] = None


def is_thin_print(point: PricePoint, *, floor_usd: float = THIN_PRINT_NOTIONAL_USD) -> bool:
    """True iff ``point`` is known to come from a single print below
    ``floor_usd`` notional. Unknown notional (book-mid-sourced points) is
    NOT flagged thin -- absence of size data is not evidence of a thin
    print."""
    return point.notional is not None and point.notional < floor_usd


def detect_jump(
    history: List[PricePoint],
    *,
    threshold: float = JUMP_THRESHOLD,
    window_secs: float = JUMP_WINDOW_SECS,
    thin_print_floor_usd: float = THIN_PRINT_NOTIONAL_USD,
) -> Optional[Tuple[PricePoint, PricePoint, bool]]:
    """Scan ``history`` (oldest->newest, same token) for a >=``threshold`` move
    within ``window_secs``. Returns ``(pre_jump_point, post_jump_point,
    thin_print)`` for the FIRST qualifying pair found (earliest post-jump
    point that clears the threshold against some earlier point still inside
    the window), or ``None``. Pure / deterministic -- no I/O, no wall-clock
    reads.

    ``thin_print`` is True iff either endpoint of the jump is a single print
    below ``thin_print_floor_usd`` notional (~33% of >=10c jumps on the
    historical tape were exactly this: a lone small print against a thin
    book, not a real reprice). Callers decide whether to use a thin-print
    jump as a trigger at all, or fire it flagged for later report-time
    exclusion -- this function only classifies, it does not filter.
    """
    n = len(history)
    for j in range(1, n):
        post = history[j]
        # earliest i within window_secs of j such that |post.price - i.price| >= threshold
        for i in range(j):
            pre = history[i]
            if post.ts - pre.ts > window_secs:
                continue
            if abs(post.price - pre.price) >= threshold:
                thin = is_thin_print(pre, floor_usd=thin_print_floor_usd) or is_thin_print(
                    post, floor_usd=thin_print_floor_usd
                )
                return pre, post, thin
    return None


def jump_direction(pre: PricePoint, post: PricePoint) -> str:
    return "up" if post.price >= pre.price else "down"


# ---------------------------------------------------------------------------
# Book walking / paper fill math (pure function -- unit tested)
# ---------------------------------------------------------------------------


@dataclass
class BookLevel:
    price: float
    size: float


def walk_book_fill(
    asks: List[BookLevel], notional_usd: float = PAPER_CLIP_USD
) -> Dict[str, Optional[float]]:
    """Simulate buying up to ``notional_usd`` by walking sorted (ascending)
    ``asks``. Returns ``{avg_price, shares, notional_filled, exhausted}``.

    ``asks`` must already be sorted best-to-worst (ascending price); this
    function does not sort defensively so tests can catch ordering bugs
    upstream. Returns all-``None`` fields (shares=0) if ``asks`` is empty or
    the best ask is invalid.
    """
    if not asks:
        return {"avg_price": None, "shares": 0.0, "notional_filled": 0.0, "exhausted": False}

    remaining = notional_usd
    shares = 0.0
    cost = 0.0
    for lvl in asks:
        if remaining <= 0:
            break
        if lvl.price is None or lvl.price <= 0:
            continue
        level_notional = lvl.price * lvl.size
        take_notional = min(remaining, level_notional)
        take_shares = take_notional / lvl.price
        shares += take_shares
        cost += take_notional
        remaining -= take_notional

    if shares <= 0:
        return {"avg_price": None, "shares": 0.0, "notional_filled": 0.0, "exhausted": False}

    avg_price = cost / shares
    return {
        "avg_price": avg_price,
        "shares": shares,
        "notional_filled": cost,
        "exhausted": remaining > 1e-9,  # ran out of book depth before filling notional
    }


def book_from_payload(book: Optional[dict]) -> Tuple[List[BookLevel], List[BookLevel]]:
    """Parse a raw CLOB ``/book`` payload into sorted (bids desc, asks asc)
    ``BookLevel`` lists. Mirrors ``pmdata.book_metrics``'s level parsing."""
    if not book:
        return [], []

    def _levels(side: str) -> List[BookLevel]:
        out = []
        for lv in book.get(side) or []:
            try:
                out.append(BookLevel(price=float(lv["price"]), size=float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids = sorted(_levels("bids"), key=lambda l: l.price, reverse=True)
    asks = sorted(_levels("asks"), key=lambda l: l.price)
    return bids, asks


def best_bid_ask(bids: List[BookLevel], asks: List[BookLevel]) -> Dict[str, Optional[float]]:
    return {
        "best_bid": bids[0].price if bids else None,
        "best_ask": asks[0].price if asks else None,
        "bid_size": bids[0].size if bids else None,
        "ask_size": asks[0].size if asks else None,
    }


# ---------------------------------------------------------------------------
# P&L (pure function -- unit tested)
# ---------------------------------------------------------------------------


def worst_case_fee(price: float) -> float:
    """Worst-case fee per the harness spec: ``0.03 * p * (1-p)`` (fraction of
    notional), applied at report time as a haircut on the mark-to-market."""
    p = max(0.0, min(1.0, price))
    return FEE_RATE * p * (1.0 - p)


def paper_pnl(fill_price: float, mark_price: float, shares: float) -> Dict[str, float]:
    """Paper P&L for a YES-side buy of ``shares`` at ``fill_price``, marked at
    ``mark_price``, after a worst-case round-trip fee on both legs."""
    gross = (mark_price - fill_price) * shares
    fee_notional = (worst_case_fee(fill_price) + worst_case_fee(mark_price)) * shares
    net = gross - fee_notional
    return {"gross_pnl": gross, "fee": fee_notional, "net_pnl": net}


# ---------------------------------------------------------------------------
# Event record (the JSONL schema)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> float:
    return time.time()


@dataclass
class RungEvent:
    """One paper-traded ladder rung, triggered by one 1X2 jump.

    ``event_id`` is unique per (trigger jump, rung token) so a single jump on
    a 1X2 token that fans out to N ladder tokens produces N independent,
    independently-markable events.
    """

    event_id: str
    trigger_token_id: str
    trigger_condition_id: str
    trigger_team: Optional[str]
    jump_pre_price: float
    jump_post_price: float
    jump_direction: str
    jump_pre_ts: float
    jump_post_ts: float
    detected_ts: float
    fetch_latency_ms: float
    #: True iff either endpoint of the TRIGGER jump was a single print below
    #: THIN_PRINT_NOTIONAL_USD notional (~33% of >=10c jumps historically).
    #: Recorded per-event (not filtered out at trigger time) so `report` can
    #: show both the filtered and unfiltered view.
    thin_print: bool = False

    rung_token_id: str = ""
    rung_category: str = ""
    rung_condition_id: str = ""
    rung_outcome: Optional[str] = None

    pre_jump_ref_price: Optional[float] = None  # last trade/mid on the RUNG before trigger
    book_best_bid: Optional[float] = None
    book_best_ask: Optional[float] = None
    book_bid_size: Optional[float] = None
    book_ask_size: Optional[float] = None

    fill_price: Optional[float] = None
    fill_shares: float = 0.0
    fill_notional: float = 0.0
    fill_exhausted: bool = False

    #: +10min/+30min/+2h marks. The +10min mark exists specifically to
    #: answer "how fast is fast enough": a parallel historical study found
    #: ~80% of the ladder drift is consumed within the first 10 minutes after
    #: the trigger jump, so the live fetch-to-fill latency recorded above
    #: only matters if it beats that decay.
    mark_10m: Optional[float] = None
    mark_10m_ts: Optional[float] = None
    mark_30m: Optional[float] = None
    mark_30m_ts: Optional[float] = None
    mark_2h: Optional[float] = None
    mark_2h_ts: Optional[float] = None

    created_utc: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RungEvent":
        known = {f.name for f in RungEvent.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return RungEvent(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# JSONL persistence (append-only file, read-modify-write for marks)
# ---------------------------------------------------------------------------


def load_events(path: Path = EVENTS_JSONL) -> List[RungEvent]:
    if not path.exists():
        return []
    out: List[RungEvent] = []
    with path.open("r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(RungEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning("skipping unparseable jsonl line: %s", exc)
    return out


def append_event(event: RungEvent, path: Path = EVENTS_JSONL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")


def rewrite_events(events: List[RungEvent], path: Path = EVENTS_JSONL) -> None:
    """Idempotent full rewrite, used by ``mark`` after updating in-memory
    events. Writes to a tmp file then renames -- atomic on the same filesystem,
    so a crash mid-write never corrupts the file the next run reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev.to_dict(), sort_keys=True) + "\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# mode: mark
# ---------------------------------------------------------------------------


def mark_events(
    events: List[RungEvent],
    *,
    fetch_mid: callable,
    now_ts: Optional[float] = None,
) -> List[RungEvent]:
    """Fill in ``mark_10m``/``mark_30m``/``mark_2h`` for events old enough and
    not yet marked. Idempotent: an event with a mark already set is left
    untouched (never re-fetched, never overwritten) even if called again.
    ``fetch_mid`` is injected so this is network-free in tests."""
    now = now_ts if now_ts is not None else _now_ts()
    offset_10m, offset_30m, offset_2h = MARK_OFFSETS_SECS
    for ev in events:
        age = now - ev.detected_ts
        if ev.mark_10m is None and age >= offset_10m:
            mid = fetch_mid(ev.rung_token_id)
            if mid is not None:
                ev.mark_10m = mid
                ev.mark_10m_ts = now
        if ev.mark_30m is None and age >= offset_30m:
            mid = fetch_mid(ev.rung_token_id)
            if mid is not None:
                ev.mark_30m = mid
                ev.mark_30m_ts = now
        if ev.mark_2h is None and age >= offset_2h:
            mid = fetch_mid(ev.rung_token_id)
            if mid is not None:
                ev.mark_2h = mid
                ev.mark_2h_ts = now
    return events


def _live_fetch_mid(token_id: str) -> Optional[float]:
    tob = top_of_book(token_id)
    return tob.get("mid") if tob else None


_DATA_API_TRADES_URL = "https://data-api.polymarket.com/trades"


def fetch_last_trade(token_id: str, *, timeout: float = 15.0) -> Optional[PricePoint]:
    """Most recent public trade print for ``token_id``: price + USD notional.

    Read-only GET against the public data-api (same endpoint as
    ``pmdata.data_api_trades``, just capped to the single latest row here).
    This is the "last-trade" half of the spec's "poll CLOB last-trade/
    midpoint" -- carrying a real notional lets :func:`detect_jump` classify
    thin prints, which ``top_of_book``'s mid alone cannot do. Returns
    ``None`` on any error / empty response."""
    if not token_id:
        return None
    try:
        resp = requests.get(_DATA_API_TRADES_URL, params={"market": str(token_id), "limit": 1},
                            headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # pragma: no cover - network best-effort
        logger.debug("last-trade fetch failed for %s: %s", token_id, exc)
        return None
    rows = data if isinstance(data, list) else (data or {}).get("data", [])
    if not rows:
        return None
    row = rows[0]
    try:
        price = float(row["price"])
        size = float(row["size"])
        ts = float(row.get("timestamp") or row.get("ts") or _now_ts())
    except (KeyError, TypeError, ValueError):
        return None
    return PricePoint(ts=ts, price=price, notional=price * size)


# ---------------------------------------------------------------------------
# mode: report
# ---------------------------------------------------------------------------


@dataclass
class ReportRow:
    event_id: str
    rung_category: str
    rung_token_id: str
    thin_print: bool
    fetch_latency_ms: float
    pre_jump_ref: Optional[float]
    fill_price: Optional[float]
    mark_10m: Optional[float]
    mark_30m: Optional[float]
    mark_2h: Optional[float]
    pnl_10m: Optional[float]
    pnl_30m: Optional[float]
    pnl_2h: Optional[float]


def build_report_rows(events: List[RungEvent]) -> List[ReportRow]:
    rows = []
    for ev in events:
        pnl_10m = pnl_30m = pnl_2h = None
        if ev.fill_price is not None and ev.fill_shares:
            if ev.mark_10m is not None:
                pnl_10m = paper_pnl(ev.fill_price, ev.mark_10m, ev.fill_shares)["net_pnl"]
            if ev.mark_30m is not None:
                pnl_30m = paper_pnl(ev.fill_price, ev.mark_30m, ev.fill_shares)["net_pnl"]
            if ev.mark_2h is not None:
                pnl_2h = paper_pnl(ev.fill_price, ev.mark_2h, ev.fill_shares)["net_pnl"]
        rows.append(
            ReportRow(
                event_id=ev.event_id,
                rung_category=ev.rung_category,
                rung_token_id=ev.rung_token_id,
                thin_print=bool(ev.thin_print),
                fetch_latency_ms=ev.fetch_latency_ms,
                pre_jump_ref=ev.pre_jump_ref_price,
                fill_price=ev.fill_price,
                mark_10m=ev.mark_10m,
                mark_30m=ev.mark_30m,
                mark_2h=ev.mark_2h,
                pnl_10m=pnl_10m,
                pnl_30m=pnl_30m,
                pnl_2h=pnl_2h,
            )
        )
    return rows


def _latency_bucket_label(latency_ms: float) -> str:
    lo = 0.0
    for hi in LATENCY_BUCKETS_MS:
        if latency_ms < hi:
            hi_label = "inf" if hi == float("inf") else f"{hi:.0f}"
            return f"[{lo:.0f}-{hi_label})ms"
        lo = hi
    return f">={lo:.0f}ms"  # pragma: no cover - unreachable, last bucket is inf


@dataclass
class LatencyStratum:
    label: str
    n: int
    hit_rate_2h: Optional[float]
    mean_pnl_2h: Optional[float]
    median_pnl_2h: Optional[float]


@dataclass
class Aggregate:
    n_events: int
    n_thin_print: int
    n_with_2h_pnl: int
    hit_rate_2h: Optional[float]
    mean_pnl_2h: Optional[float]
    median_pnl_2h: Optional[float]
    sufficient: bool
    by_latency: List[LatencyStratum] = field(default_factory=list)


def _stats_for(pnls: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if not pnls:
        return None, None, None
    hit_rate = sum(1 for p in pnls if p > 0) / len(pnls)
    return hit_rate, statistics.fmean(pnls), statistics.median(pnls)


def aggregate_report(
    rows: List[ReportRow],
    *,
    min_events: int = MIN_EVENTS_FOR_REPORT,
    exclude_thin_print: bool = True,
) -> Aggregate:
    """Aggregate P&L stats, EXCLUDING thin-print-triggered events by default
    (they are ~33% of raw jumps and historically not a real reprice -- mixing
    them in dilutes the signal the live test is trying to measure). Also
    breaks the 2h P&L down by fetch-latency bucket so the report answers
    "how fast is fast enough" directly."""
    usable = [r for r in rows if not (exclude_thin_print and r.thin_print)]
    n = len(usable)
    pnls_2h = [r.pnl_2h for r in usable if r.pnl_2h is not None]
    hit_rate, mean_pnl, median_pnl = _stats_for(pnls_2h)

    strata = []
    lo = 0.0
    for hi in LATENCY_BUCKETS_MS:
        bucket_rows = [r for r in usable if lo <= r.fetch_latency_ms < hi]
        bucket_pnls = [r.pnl_2h for r in bucket_rows if r.pnl_2h is not None]
        b_hit, b_mean, b_median = _stats_for(bucket_pnls)
        strata.append(
            LatencyStratum(
                label=_latency_bucket_label(lo + 1e-9 if lo > 0 else 0.0),
                n=len(bucket_rows),
                hit_rate_2h=b_hit,
                mean_pnl_2h=b_mean,
                median_pnl_2h=b_median,
            )
        )
        lo = hi

    return Aggregate(
        n_events=len(rows),
        n_thin_print=sum(1 for r in rows if r.thin_print),
        n_with_2h_pnl=len(pnls_2h),
        hit_rate_2h=hit_rate,
        mean_pnl_2h=mean_pnl,
        median_pnl_2h=median_pnl,
        sufficient=n >= min_events,
        by_latency=strata,
    )


def format_report(rows: List[ReportRow], agg: Aggregate) -> str:
    lines = []
    header = (
        f"{'event_id':<24} {'rung':<18} {'thin':>4} {'lat_ms':>8} {'pre_jump':>9} "
        f"{'fill':>7} {'+10m':>7} {'+30m':>7} {'+2h':>7} {'pnl_10m':>9} "
        f"{'pnl_30m':>9} {'pnl_2h':>9}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    def _fmt(x: Optional[float], nd: int = 3) -> str:
        return f"{x:.{nd}f}" if x is not None else "--"

    for r in rows:
        lines.append(
            f"{r.event_id:<24} {r.rung_category:<18} {str(r.thin_print):>4} "
            f"{r.fetch_latency_ms:>8.0f} {_fmt(r.pre_jump_ref):>9} "
            f"{_fmt(r.fill_price):>7} {_fmt(r.mark_10m):>7} {_fmt(r.mark_30m):>7} "
            f"{_fmt(r.mark_2h):>7} {_fmt(r.pnl_10m):>9} {_fmt(r.pnl_30m):>9} {_fmt(r.pnl_2h):>9}"
        )

    lines.append("")
    if not agg.sufficient:
        lines.append(
            f"insufficient events (<{MIN_EVENTS_FOR_REPORT}) -- keep collecting "
            f"(n={agg.n_events}, thin_print={agg.n_thin_print} excluded from stats)"
        )
    else:
        lines.append(
            f"n={agg.n_events}  thin_print_excluded={agg.n_thin_print}  "
            f"n_with_2h_pnl={agg.n_with_2h_pnl}  "
            f"hit_rate_2h={_fmt(agg.hit_rate_2h, 3)}  "
            f"mean_pnl_2h={_fmt(agg.mean_pnl_2h, 4)}  "
            f"median_pnl_2h={_fmt(agg.median_pnl_2h, 4)}  (per ${PAPER_CLIP_USD:.0f} clip)"
        )
        lines.append("")
        lines.append("P&L by fetch-latency bucket (2h mark, thin-print excluded):")
        lines.append(f"{'bucket':<16} {'n':>4} {'hit_rate':>9} {'mean_pnl':>10} {'median_pnl':>11}")
        for s in agg.by_latency:
            lines.append(
                f"{s.label:<16} {s.n:>4} {_fmt(s.hit_rate_2h, 3):>9} "
                f"{_fmt(s.mean_pnl_2h, 4):>10} {_fmt(s.median_pnl_2h, 4):>11}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# mode: watch
# ---------------------------------------------------------------------------


def _event_id(trigger_token_id: str, rung_token_id: str, detected_ts: float) -> str:
    return f"{trigger_token_id[:8]}-{rung_token_id[:8]}-{int(detected_ts)}"


def process_jump(
    *,
    trigger_ref: TokenRef,
    pre: PricePoint,
    post: PricePoint,
    ladder_refs: List[TokenRef],
    fetch_book: callable,
    fetch_ref_price: callable,
    detected_ts: Optional[float] = None,
    thin_print: bool = False,
) -> List[RungEvent]:
    """Core of the ``watch`` trigger path, extracted as a pure-ish function
    (all I/O injected) so it is unit-testable without a network.

    For every same-team ``ladder_refs`` token: fetch the book (latency
    timed), walk it for a paper fill, and build a ``RungEvent``. Latency is
    measured around the FIRST book fetch in the batch (the spec's
    "immediately... log the latency" applies to detection-to-fetch, not
    per-token). ``thin_print`` (from :func:`detect_jump`'s classification) is
    stamped onto every fanned-out event so `report` can exclude thin-print
    triggers from the aggregate without losing the raw record.
    """
    detected = detected_ts if detected_ts is not None else _now_ts()
    direction = jump_direction(pre, post)
    events: List[RungEvent] = []

    fetch_start = time.monotonic()
    for rung in ladder_refs:
        t0 = time.monotonic()
        book = fetch_book(rung.token_id)
        latency_ms = (time.monotonic() - t0) * 1000.0
        bids, asks = book_from_payload(book)
        bba = best_bid_ask(bids, asks)
        fill = walk_book_fill(asks, PAPER_CLIP_USD)
        ref_price = fetch_ref_price(rung.token_id)

        ev = RungEvent(
            event_id=_event_id(trigger_ref.token_id, rung.token_id, detected),
            trigger_token_id=trigger_ref.token_id,
            trigger_condition_id=trigger_ref.condition_id,
            trigger_team=trigger_ref.team,
            jump_pre_price=pre.price,
            jump_post_price=post.price,
            jump_direction=direction,
            jump_pre_ts=pre.ts,
            jump_post_ts=post.ts,
            detected_ts=detected,
            fetch_latency_ms=latency_ms,
            thin_print=thin_print,
            rung_token_id=rung.token_id,
            rung_category=rung.category,
            rung_condition_id=rung.condition_id,
            rung_outcome=rung.outcome,
            pre_jump_ref_price=ref_price,
            book_best_bid=bba["best_bid"],
            book_best_ask=bba["best_ask"],
            book_bid_size=bba["bid_size"],
            book_ask_size=bba["ask_size"],
            fill_price=fill["avg_price"],
            fill_shares=fill["shares"] or 0.0,
            fill_notional=fill["notional_filled"] or 0.0,
            fill_exhausted=bool(fill["exhausted"]),
        )
        events.append(ev)
    total_latency_ms = (time.monotonic() - fetch_start) * 1000.0
    logger.info(
        "jump on %s %s->%s (%s, thin_print=%s): fanned out to %d ladder tokens in %.0fms",
        trigger_ref.token_id, pre.price, post.price, direction, thin_print, len(ladder_refs),
        total_latency_ms,
    )
    return events


class PriceHistoryBuffer:
    """Rolling in-memory last-N-minutes price history per token, used to feed
    :func:`detect_jump` during ``watch`` without re-fetching CLOB history."""

    def __init__(self, keep_secs: float = JUMP_WINDOW_SECS * 2):
        self.keep_secs = keep_secs
        self._by_token: Dict[str, List[PricePoint]] = {}

    def add(self, token_id: str, point: PricePoint) -> None:
        buf = self._by_token.setdefault(token_id, [])
        buf.append(point)
        cutoff = point.ts - self.keep_secs
        while buf and buf[0].ts < cutoff:
            buf.pop(0)

    def history(self, token_id: str) -> List[PricePoint]:
        return list(self._by_token.get(token_id, []))


def run_watch(
    *,
    db_path: Path = ORDERFLOW_DB,
    events_path: Path = EVENTS_JSONL,
    date_str: Optional[str] = None,
    slugs_override: Optional[List[str]] = None,
    max_iterations: Optional[int] = None,
    poll_cadence: float = POLL_CADENCE_SECS,
    sleep_fn: callable = time.sleep,
) -> int:
    """Matchday watch loop. Returns a process-style exit code (0 = clean).

    ``max_iterations`` / ``sleep_fn`` exist purely so this can be smoke-tested
    without actually blocking forever; production use leaves both at defaults
    and relies on Ctrl-C (handled by the caller -- see ``main``).
    """
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    match_refs, ladder_by_team = discover_tokens_for_date(db_path, date_str)

    if slugs_override:
        logger.info("--slugs override supplied: %d explicit token ids", len(slugs_override))
        # Explicit override tokens are treated as trigger candidates with
        # unknown team/ladder linkage -- discovery still supplies the ladder
        # map for any team that happens to match.
        match_refs = match_refs + [
            TokenRef(token_id=t, condition_id="", category=MATCH_1X2_CATEGORY,
                     team=None, outcome=None, market_slug=None, game_start_time=None)
            for t in slugs_override
        ]

    if not match_refs:
        print(f"no live matches for {date_str} -- nothing to watch")
        return 0

    logger.info(
        "watching %d 1X2 tokens across %d teams with ladder rungs",
        len(match_refs), len(ladder_by_team),
    )

    events = load_events(events_path)
    limiter = RateLimiter()
    history = PriceHistoryBuffer()
    triggered_tokens: set = set()

    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            now = _now_ts()
            for ref in match_refs:
                limiter.wait()
                # Prefer a real last-trade print (carries notional, needed for
                # thin-print classification); fall back to book mid if the
                # trade feed has nothing recent.
                point = fetch_last_trade(ref.token_id)
                if point is None:
                    tob = top_of_book(ref.token_id)
                    if tob is None or tob.get("mid") is None:
                        continue
                    point = PricePoint(ts=now, price=float(tob["mid"]), notional=None)
                history.add(ref.token_id, point)

                if ref.token_id in triggered_tokens:
                    continue
                jump = detect_jump(history.history(ref.token_id))
                if jump is None:
                    continue
                pre, post, thin = jump
                triggered_tokens.add(ref.token_id)
                ladder_refs = ladder_tokens_for_team(ladder_by_team, ref.team)
                new_events = process_jump(
                    trigger_ref=ref, pre=pre, post=post, ladder_refs=ladder_refs,
                    fetch_book=fetch_raw_book,
                    fetch_ref_price=_live_fetch_mid,
                    thin_print=thin,
                )
                for ev in new_events:
                    append_event(ev, events_path)
                    events.append(ev)

            events = mark_events(events, fetch_mid=_live_fetch_mid, now_ts=_now_ts())
            rewrite_events(events, events_path)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            sleep_fn(poll_cadence)
    except KeyboardInterrupt:
        logger.info("watch interrupted by user -- state already persisted, safe to resume")
        rewrite_events(events, events_path)
        return 0
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_watch(args: argparse.Namespace) -> int:
    return run_watch(
        db_path=Path(args.db) if args.db else ORDERFLOW_DB,
        events_path=Path(args.events) if args.events else EVENTS_JSONL,
        date_str=args.date,
        slugs_override=args.slugs.split(",") if args.slugs else None,
        max_iterations=args.max_iterations,
    )


def _cmd_mark(args: argparse.Namespace) -> int:
    path = Path(args.events) if args.events else EVENTS_JSONL
    events = load_events(path)
    if not events:
        print("no events to mark")
        return 0
    events = mark_events(events, fetch_mid=_live_fetch_mid)
    rewrite_events(events, path)
    n_marked_10 = sum(1 for e in events if e.mark_10m is not None)
    n_marked_30 = sum(1 for e in events if e.mark_30m is not None)
    n_marked_2h = sum(1 for e in events if e.mark_2h is not None)
    print(f"marked {len(events)} events: {n_marked_10} have +10m, "
          f"{n_marked_30} have +30m, {n_marked_2h} have +2h")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.events) if args.events else EVENTS_JSONL
    events = load_events(path)
    rows = build_report_rows(events)
    agg = aggregate_report(rows)
    print(format_report(rows, agg))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wca_ladderlag_papertest",
        description=(
            "PAPER-ONLY matchday harness testing whether same-team advancement "
            "ladder rungs lag a >=10c 1X2 jump long enough to be executable. "
            "Never places real orders (no pm/trader.py import, no signing)."
        ),
    )
    sub = p.add_subparsers(dest="mode", required=True)

    w = sub.add_parser("watch", help="Matchday polling + paper-trigger loop")
    w.add_argument("--db", help="Path to pm_orderflow.db (default: data/pm_orderflow.db)")
    w.add_argument("--events", help="Path to events jsonl (default: data/ladderlag_papertest.jsonl)")
    w.add_argument("--date", help="UTC date YYYY-MM-DD to watch (default: today)")
    w.add_argument("--slugs", help="Comma-separated explicit token ids override")
    w.add_argument("--max-iterations", type=int, default=None,
                   help="Stop after N poll iterations (testing/smoke only)")
    w.set_defaults(func=_cmd_watch)

    m = sub.add_parser("mark", help="Re-mark open events (+30m/+2h), idempotent")
    m.add_argument("--events", help="Path to events jsonl")
    m.set_defaults(func=_cmd_mark)

    r = sub.add_parser("report", help="Per-event table + aggregate stats")
    r.add_argument("--events", help="Path to events jsonl")
    r.set_defaults(func=_cmd_report)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
