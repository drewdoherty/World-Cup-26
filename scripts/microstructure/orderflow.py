#!/usr/bin/env python3
"""Polymarket orderflow analytics — wallets, cohorts, archetypes, jump latency.

Run with::

    PYTHONPATH=src .venv/bin/python scripts/microstructure/orderflow.py
    # or against a fixture / alternate db:
    PYTHONPATH=src .venv/bin/python scripts/microstructure/orderflow.py \
        --db /path/to/fixture.db --out /path/to/out.json

What this is
------------
The ingestion side (``scripts/microstructure/orderflow_ingest``-family) fills
``data/pm_orderflow.db`` with per-market **taker fills** from
``data-api.polymarket.com/trades`` plus market metadata from the gamma API.
This module reads that db STRICTLY READ-ONLY and computes the analytics feed
``site/microstructure/orderflow.json`` the microstructure page renders:

* **Per-wallet P&L** — average-cost position accounting per (wallet, token).
  BUYs add shares at cost; SELLs realize ``(sell - avg_cost) * matched`` with
  sold shares clipped at held shares (excess = short/mint-side flow, tracked
  as *unmatched* and excluded from realized P&L but counted in gross volume).
  Resolved markets settle remaining shares at $1 (winning token) / $0; open
  positions are marked at the **last trade print** seen on the token.
  Wallets whose gross sits in offset-capped ("truncated") markets can be
  missing legs, so their rows carry ``partial_history=true`` unless the
  ingester's ``--backfill-leaderboards`` per-user sweep has fetched their
  complete history (logged as ``user:<wallet>`` rows in ``pm_ingest_log``).
* **Informedness** — USD-weighted average move of the token price over the
  24h after each trade, signed in the trade's favour (cents).
* **Smart/dumb cohorts** — top/bottom decile of qualifying wallets by
  ``z(ROI) + z(informedness)`` (in-sample, WC-only — read the honesty notes).
* **Archetypes** — first-match-wins behavioural buckets (whales, scalpers,
  snipers, longshot-lottery, favourite-grinders, tourists).
* **Jump latency** — price-move-based jump detection on each market's primary
  token (>=6c away from the median of the previous 5 prints, extending while
  prints keep progressing >=1c within 120s), plus cross-market propagation
  from match (1X2) jumps into the same team's futures ladder.

Everything here is TAKER-side only: the data-api trade feed names the taker
wallet on each fill; maker identities are never observed. All caveats are
carried in the JSON's ``honesty_notes``.

Read-only: this script never writes to any database.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(REPO, "data", "pm_orderflow.db")
DEFAULT_OUT = os.path.join(REPO, "site", "microstructure", "orderflow.json")

# Cohort qualification defaults (CLI-overridable).
MIN_TRADES = 5
MIN_GROSS_USD = 200.0

# Jump detection parameters.
JUMP_MIN_MARKET_TRADES = 30   # only scan markets with at least this many fills
JUMP_REF_WINDOW = 5           # reference = median of previous N prints
JUMP_START_MOVE = 0.06        # first print >= 6c away from reference starts a jump
JUMP_PROGRESS_MOVE = 0.01     # subsequent prints must progress >= 1c ...
JUMP_PROGRESS_WINDOW_S = 120  # ... within 120s of the last progressing print
JUMP_FIRST_WINDOW_S = 30      # "first mover" window after jump start
JUMP_MERGE_COOLDOWN_S = 300   # re-triggers on the same market within 5 min of
                              # the last episode print merge into that episode
                              # (echo suppression — one move, one jump)
CROSS_MARKET_WINDOW_S = 1800  # follower jump must start within 30 min of trigger

INFORMEDNESS_HORIZON_S = 24 * 3600
INFORMEDNESS_MIN_OBS = 3

CATEGORY_LABELS: Dict[str, str] = {
    "advancement_r32": "Advance to R32",
    "advancement_r16": "Reach Round of 16",
    "advancement_qf": "Reach Quarterfinals",
    "advancement_sf": "Reach Semifinals",
    "advancement_final": "Reach Final",
    "winner": "Tournament Winner",
    "group_winner": "Group Winner",
    "match_1x2": "Match (1X2)",
    "other_future": "Other Futures",
}
FUTURES_CATEGORIES = {
    "advancement_r32", "advancement_r16", "advancement_qf",
    "advancement_sf", "advancement_final", "group_winner", "winner",
}

ARCHETYPES: List[Tuple[str, str, str]] = [
    ("whale_sharp", "Whale (sharp)", ">=$10k gross USD and positive ROI"),
    ("whale_underwater", "Whale (underwater)", ">=$10k gross USD, flat-or-negative ROI"),
    ("scalper_mm", "Scalper / MM-like", ">=100 trades, balanced buy/sell flow (35-65% buy USD), median trade <$100"),
    ("sniper", "Sniper", "<20 trades, median trade >=$250, positive 24h informedness"),
    ("longshot_lottery", "Longshot lottery", ">=60% of BUY USD at price <= $0.15"),
    ("favorite_grinder", "Favorite grinder", ">=60% of BUY USD at price >= $0.80"),
    ("retail_tourist", "Retail tourist", "<10 trades and <$500 gross USD"),
    ("regular", "Regular", "everything else"),
]

SIZE_BUCKETS: List[Tuple[str, float, float]] = [
    ("<$10", 0.0, 10.0),
    ("$10–50", 10.0, 50.0),
    ("$50–250", 50.0, 250.0),
    ("$250–1k", 250.0, 1000.0),
    ("$1k–5k", 1000.0, 5000.0),
    ("$5k–25k", 5000.0, 25000.0),
    (">$25k", 25000.0, float("inf")),
]

# Our own bot funder wallet — labelled so it is never mistaken for an outside
# whale. Constant lives in wca.pm.trader; fall back to the repo value if the
# import path is unavailable (e.g. run without PYTHONPATH=src).
try:  # pragma: no cover - trivial import shim
    from wca.pm.trader import KNOWN_PROXY_FUNDER as WCA_FUNDER
except Exception:  # noqa: BLE001
    WCA_FUNDER = "0x40231C7f4FC2BBAB720ce9b669eAb4795fCBE191"


# ---------------------------------------------------------------------------
# Small numeric helpers.
# ---------------------------------------------------------------------------


def _fin(x: Optional[float]) -> Optional[float]:
    """None if x is None/NaN/Inf, else float(x)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x: Optional[float], nd: int) -> Optional[float]:
    v = _fin(x)
    return None if v is None else round(v, nd)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _median(vals: List[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _p90(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    idx = max(0, math.ceil(0.9 * len(s)) - 1)
    return s[idx]


def _sanitize(obj: Any) -> Any:
    """Recursively replace non-finite floats with None (contract: no NaN/Inf)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Loading (read-only).
# ---------------------------------------------------------------------------


def load_db(db_path: str) -> Tuple[Dict[str, dict], List[dict], List[str], set, set]:
    """Load markets, trades (ts-sorted), truncation + backfill state. Read-only.

    Returns ``(markets, trades, truncated_slugs, truncated_cids,
    backfilled_wallets)``. ``pm_ingest_log`` rows whose condition_id starts
    with ``user:`` record per-wallet backfills (the ingester's
    ``--backfill-leaderboards`` sweep); a wallet with a ``truncated=0`` user
    row has had its complete in-scope history re-fetched via the per-user
    endpoint, so its P&L is NOT partial even where it traded capped markets.
    """
    uri = "file:%s?mode=ro" % db_path
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        markets: Dict[str, dict] = {}
        for r in con.execute("SELECT * FROM pm_markets"):
            m = dict(r)
            for key in ("outcomes", "token_ids"):
                try:
                    m[key] = json.loads(m.get(key) or "[]")
                except (TypeError, ValueError):
                    m[key] = []
            markets[m["condition_id"]] = m

        trades = [dict(r) for r in con.execute(
            "SELECT id, condition_id, asset, outcome, outcome_index, wallet, name,"
            "       pseudonym, side, size, price, usd, ts, tx_hash "
            "FROM pm_trades ORDER BY ts, id"
        )]

        truncated: List[str] = []
        truncated_cids: set = set()
        backfilled_wallets: set = set()
        try:
            for r in con.execute(
                "SELECT DISTINCT condition_id FROM pm_ingest_log WHERE truncated=1"
            ):
                cid = r["condition_id"] or ""
                if cid.startswith("user:"):
                    continue  # per-wallet backfill rows are not markets
                truncated_cids.add(cid)
                m = markets.get(cid)
                truncated.append((m or {}).get("market_slug") or cid)
            for r in con.execute(
                "SELECT DISTINCT condition_id FROM pm_ingest_log "
                "WHERE truncated=0 AND condition_id LIKE 'user:%'"
            ):
                backfilled_wallets.add(r["condition_id"][5:].lower())
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()
    return markets, trades, sorted(set(truncated)), truncated_cids, backfilled_wallets


# ---------------------------------------------------------------------------
# Wallet accounting.
# ---------------------------------------------------------------------------


class WalletStats:
    """Mutable accumulator for one wallet."""

    __slots__ = (
        "wallet", "name", "trades", "gross_usd", "buy_usd", "sell_usd",
        "usd_list", "buy_usd_longshot", "buy_usd_fav", "category_usd",
        "realized_pnl", "mtm_pnl", "rt_total", "rt_wins", "unmatched_usd",
        "inf_num", "inf_den", "inf_n", "first30_usd", "first30_jumps",
        "truncated_usd",
    )

    def __init__(self, wallet: str) -> None:
        self.wallet = wallet
        self.name: Optional[str] = None
        self.trades = 0
        self.gross_usd = 0.0
        self.buy_usd = 0.0
        self.sell_usd = 0.0
        self.usd_list: List[float] = []
        self.buy_usd_longshot = 0.0
        self.buy_usd_fav = 0.0
        self.category_usd: Dict[str, float] = defaultdict(float)
        self.realized_pnl = 0.0
        self.mtm_pnl = 0.0
        self.rt_total = 0        # closed/settled round-trips
        self.rt_wins = 0
        self.unmatched_usd = 0.0  # sells beyond held shares (mint/short side)
        self.inf_num = 0.0        # USD-weighted informedness numerator
        self.inf_den = 0.0
        self.inf_n = 0            # measurable trades
        self.first30_usd: List[float] = []  # usd of fills inside jump first-30s
        self.first30_jumps = 0
        self.truncated_usd = 0.0  # gross usd sitting in offset-capped markets

    # -- derived --
    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.mtm_pnl

    @property
    def roi(self) -> Optional[float]:
        return (self.total_pnl / self.gross_usd) if self.gross_usd > 0 else None

    @property
    def win_rate(self) -> Optional[float]:
        return (self.rt_wins / self.rt_total) if self.rt_total > 0 else None

    @property
    def informedness_cents(self) -> Optional[float]:
        if self.inf_n < INFORMEDNESS_MIN_OBS or self.inf_den <= 0:
            return None
        return 100.0 * self.inf_num / self.inf_den

    @property
    def median_trade_usd(self) -> float:
        return statistics.median(self.usd_list) if self.usd_list else 0.0

    def archetype(self) -> str:
        gross = self.gross_usd
        roi = self.roi
        buy_share = (self.buy_usd / gross) if gross > 0 else None
        med = self.median_trade_usd
        inf = self.informedness_cents
        if gross >= 10000 and roi is not None and roi > 0:
            return "whale_sharp"
        if gross >= 10000:
            return "whale_underwater"
        if self.trades >= 100 and buy_share is not None and 0.35 <= buy_share <= 0.65 and med < 100:
            return "scalper_mm"
        if self.trades < 20 and med >= 250 and inf is not None and inf > 0:
            return "sniper"
        if self.buy_usd > 0 and self.buy_usd_longshot / self.buy_usd >= 0.60:
            return "longshot_lottery"
        if self.buy_usd > 0 and self.buy_usd_fav / self.buy_usd >= 0.60:
            return "favorite_grinder"
        if self.trades < 10 and gross < 500:
            return "retail_tourist"
        return "regular"


def compute_wallets(
    markets: Dict[str, dict], trades: List[dict], truncated_cids: set
) -> Tuple[Dict[str, WalletStats], Dict[str, List[Tuple[int, float]]]]:
    """Average-cost accounting + informedness. Returns (wallets, token prints).

    Token prints = ts-sorted ``[(ts, price)]`` per asset — reused as the
    last-trade mark and the +24h informedness lookup.
    """
    # Token settle price where the parent market resolved.
    token_settle: Dict[str, float] = {}
    token_resolved: Dict[str, bool] = {}
    for cid, m in markets.items():
        idx = m.get("resolved_outcome_index")
        tokens = m.get("token_ids") or []
        resolved = idx is not None
        for i, tok in enumerate(tokens):
            token_resolved[tok] = resolved
            if resolved:
                token_settle[tok] = 1.0 if i == int(idx) else 0.0

    prints: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for t in trades:  # already ts-sorted
        prints[t["asset"]].append((int(t["ts"]), float(t["price"])))
    print_ts: Dict[str, List[int]] = {tok: [p[0] for p in s] for tok, s in prints.items()}

    wallets: Dict[str, WalletStats] = {}
    positions: Dict[Tuple[str, str], Tuple[float, float]] = {}  # (shares, cost)

    for t in trades:
        w = t["wallet"]
        ws = wallets.get(w)
        if ws is None:
            ws = wallets[w] = WalletStats(w)
        if ws.name is None:
            ws.name = t.get("name") or t.get("pseudonym") or None

        side = (t["side"] or "").upper()
        size = float(t["size"])
        price = float(t["price"])
        usd = float(t["usd"])
        tok = t["asset"]

        ws.trades += 1
        ws.gross_usd += usd
        ws.usd_list.append(usd)
        if t["condition_id"] in truncated_cids:
            ws.truncated_usd += usd
        cat = (markets.get(t["condition_id"]) or {}).get("category") or "other_future"
        ws.category_usd[cat] += usd

        key = (w, tok)
        shares, cost = positions.get(key, (0.0, 0.0))
        if side == "BUY":
            ws.buy_usd += usd
            if price <= 0.15:
                ws.buy_usd_longshot += usd
            if price >= 0.80:
                ws.buy_usd_fav += usd
            positions[key] = (shares + size, cost + size * price)
        else:  # SELL
            ws.sell_usd += usd
            matched = min(size, shares)
            if matched > 1e-12:
                avg = cost / shares if shares > 1e-12 else 0.0
                pnl = (price - avg) * matched
                ws.realized_pnl += pnl
                ws.rt_total += 1
                if pnl > 0:
                    ws.rt_wins += 1
                positions[key] = (shares - matched, cost - avg * matched)
            excess = size - matched
            if excess > 1e-12:
                # Short/mint-side flow we cannot cost — excluded from realized.
                ws.unmatched_usd += excess * price

        # -- informedness: last print strictly after this trade, <= ts+24h --
        series = prints[tok]
        idx = bisect_right(print_ts[tok], int(t["ts"]) + INFORMEDNESS_HORIZON_S) - 1
        if idx >= 0 and series[idx][0] > int(t["ts"]):
            p24 = series[idx][1]
            delta = (p24 - price) if side == "BUY" else (price - p24)
            ws.inf_num += usd * delta
            ws.inf_den += usd
            ws.inf_n += 1

    # -- settle / mark remaining positions --
    last_price = {tok: series[-1][1] for tok, series in prints.items() if series}
    for (w, tok), (shares, cost) in positions.items():
        if shares <= 1e-9:
            continue
        ws = wallets[w]
        avg = cost / shares
        if token_resolved.get(tok):
            pnl = (token_settle.get(tok, 0.0) - avg) * shares
            ws.realized_pnl += pnl
            ws.rt_total += 1
            if pnl > 0:
                ws.rt_wins += 1
        else:
            mark = last_price.get(tok)
            if mark is not None:
                ws.mtm_pnl += (mark - avg) * shares

    return wallets, prints


# ---------------------------------------------------------------------------
# Cohorts.
# ---------------------------------------------------------------------------


def _z(vals: List[float]) -> Tuple[float, float]:
    """(mean, stdev) with stdev floored to avoid div-by-zero."""
    if not vals:
        return 0.0, 1.0
    mu = statistics.mean(vals)
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return mu, (sd if sd > 1e-12 else 1.0)


def build_cohorts(
    wallets: Dict[str, WalletStats], min_trades: int, min_gross: float
) -> Tuple[List[str], List[str], Dict[str, float], str]:
    """Smart/dumb cohorts among qualifying wallets. Returns (smart, dumb, scores, definition)."""
    qual = [
        ws for ws in wallets.values()
        if ws.trades >= min_trades and ws.gross_usd >= min_gross and ws.roi is not None
    ]
    rois = [ws.roi for ws in qual]
    infs = [ws.informedness_cents for ws in qual if ws.informedness_cents is not None]
    mu_r, sd_r = _z([r for r in rois if r is not None])
    mu_i, sd_i = _z(infs)

    scores: Dict[str, float] = {}
    for ws in qual:
        s = (ws.roi - mu_r) / sd_r
        inf = ws.informedness_cents
        if inf is not None:
            s += (inf - mu_i) / sd_i
        scores[ws.wallet] = s

    n = len(qual)
    k = max(10, n // 10)
    k = min(k, n // 2)  # keep cohorts disjoint on small samples
    ranked = sorted(qual, key=lambda ws: scores[ws.wallet], reverse=True)
    smart = [ws.wallet for ws in ranked[:k]]
    dumb = [ws.wallet for ws in ranked[n - k:]] if k > 0 else []

    definition = (
        "Qualifying wallets: >=%d trades AND >=$%d gross USD (n=%d). Score = "
        "z(ROI) + z(informedness_cents), z-scored within the qualifying set "
        "(wallets with null informedness contribute 0 to that term). Smart = "
        "top max(10, n/10) wallets by score, dumb = bottom, both capped at n/2 "
        "so the cohorts stay disjoint (k=%d here). ROI includes mark-to-market "
        "on open positions at last trade price. In-sample, WC-only."
        % (min_trades, int(min_gross), n, k)
    )
    return smart, dumb, scores, definition


# ---------------------------------------------------------------------------
# Jumps / latency.
# ---------------------------------------------------------------------------


def detect_jumps(markets: Dict[str, dict], trades: List[dict]) -> List[dict]:
    """Price-move jump detection on each market's primary (index-0) token.

    A jump starts when a print lands >= JUMP_START_MOVE away from the median
    of the previous JUMP_REF_WINDOW prints, and extends while later prints
    keep progressing >= JUMP_PROGRESS_MOVE in the jump direction within
    JUMP_PROGRESS_WINDOW_S of the last progressing print. reprice_s is 0 for
    a single-print jump (still counted).

    Echo suppression — one move, one episode: after a jump the scan resumes a
    full reference warm-up later (``last_idx + JUMP_REF_WINDOW``) so the
    median window is re-seeded with post-jump prints; without this, every
    print at the new level re-triggers against the stale pre-jump reference
    (each move used to be counted up to ~3x). Any jump that still starts
    within JUMP_MERGE_COOLDOWN_S of the previous episode's last print on the
    same market (bid-ask bounce across a wide spread, residual echoes) is
    merged into that episode rather than counted afresh.
    """
    by_market: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        by_market[t["condition_id"]].append(t)

    jumps: List[dict] = []
    for cid, mtrades in by_market.items():
        if len(mtrades) < JUMP_MIN_MARKET_TRADES:
            continue
        m = markets.get(cid) or {}
        tokens = m.get("token_ids") or []
        primary = tokens[0] if tokens else None
        tp = [
            t for t in mtrades
            if (primary is not None and t["asset"] == primary)
            or (primary is None and (t.get("outcome_index") or 0) == 0)
        ]
        n = len(tp)
        episodes: List[dict] = []
        i = JUMP_REF_WINDOW
        while i < n:
            ref = statistics.median(float(t["price"]) for t in tp[i - JUMP_REF_WINDOW:i])
            p0 = float(tp[i]["price"])
            if abs(p0 - ref) < JUMP_START_MOVE - 1e-9:
                i += 1
                continue
            direction = 1.0 if p0 > ref else -1.0
            start_ts = int(tp[i]["ts"])
            last_price, last_ts, last_idx = p0, start_ts, i
            j = i + 1
            while j < n and int(tp[j]["ts"]) - last_ts <= JUMP_PROGRESS_WINDOW_S:
                pj = float(tp[j]["price"])
                if direction * (pj - last_price) >= JUMP_PROGRESS_MOVE - 1e-9:
                    last_price, last_ts, last_idx = pj, int(tp[j]["ts"]), j
                j += 1
            window = tp[i:last_idx + 1]
            prev = episodes[-1] if episodes else None
            if prev is not None and start_ts - prev["end_ts"] <= JUMP_MERGE_COOLDOWN_S:
                # Same repricing episode still settling — fold in, do not
                # double count. Windows are disjoint so jump_usd adds cleanly;
                # first-30s stats stay anchored to the EPISODE start.
                prev["end_ts"] = max(prev["end_ts"], last_ts)
                prev["reprice_s"] = float(prev["end_ts"] - prev["start_ts"])
                prev["move_cents"] = max(prev["move_cents"], abs(last_price - ref) * 100.0)
                prev["jump_usd"] += sum(float(t["usd"]) for t in window)
                cutoff = prev["start_ts"] + JUMP_FIRST_WINDOW_S
                late30 = [t for t in window if int(t["ts"]) <= cutoff]
                prev["first30_usd"] += sum(float(t["usd"]) for t in late30)
                prev["first30_fills"].extend((t["wallet"], float(t["usd"])) for t in late30)
            else:
                first30 = [t for t in window if int(t["ts"]) <= start_ts + JUMP_FIRST_WINDOW_S]
                episodes.append({
                    "condition_id": cid,
                    "market_slug": m.get("market_slug") or cid,
                    "category": m.get("category") or "other_future",
                    "team": m.get("team"),
                    "start_ts": start_ts,
                    "end_ts": last_ts,
                    "reprice_s": float(last_ts - start_ts),
                    "move_cents": abs(last_price - ref) * 100.0,
                    "jump_usd": sum(float(t["usd"]) for t in window),
                    "first30_usd": sum(float(t["usd"]) for t in first30),
                    "first30_fills": [(t["wallet"], float(t["usd"])) for t in first30],
                })
            i = last_idx + JUMP_REF_WINDOW  # fresh reference warm-up (see docstring)
        jumps.extend(episodes)
    jumps.sort(key=lambda j: j["start_ts"])
    return jumps


def cross_market_rows(jumps: List[dict]) -> List[dict]:
    """Same-team propagation, keyed on the FOLLOWER episode.

    Each futures follower episode appears at most once, matched to the
    NEAREST PRECEDING match-1X2 jump on its team within 30 minutes. (The old
    trigger-keyed pairing re-presented one propagation event once per trigger
    echo, so the recency-sorted top-20 collapsed to a handful of distinct
    pairs from the most recent hours.)
    """
    by_team: Dict[str, List[dict]] = defaultdict(list)
    for t in jumps:
        if t["category"] == "match_1x2" and t.get("team") and t["team"] != "Draw":
            by_team[t["team"]].append(t)
    rows: List[dict] = []
    for f in jumps:
        if f["category"] not in FUTURES_CATEGORIES or not f.get("team"):
            continue
        cands = [
            t for t in by_team.get(f["team"], ())
            if t["start_ts"] <= f["start_ts"] <= t["start_ts"] + CROSS_MARKET_WINDOW_S
        ]
        if not cands:
            continue
        trig = max(cands, key=lambda x: x["start_ts"])  # nearest preceding trigger
        rows.append({
            "team": f["team"],
            "trigger": trig["market_slug"],
            "follower": f["market_slug"],
            "lag_s": _r(f["start_ts"] - trig["start_ts"], 1),
            "follower_move_cents": _r(f["move_cents"], 2),
            "ts_utc": _iso(trig["start_ts"]),
        })
    rows.sort(key=lambda r: r["ts_utc"], reverse=True)
    return rows[:20]


def latency_block(jumps: List[dict], truncated: List[str]) -> dict:
    by_cat: Dict[str, List[dict]] = defaultdict(list)
    for j in jumps:
        by_cat[j["category"]].append(j)
    cat_rows = []
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        js = by_cat[cat]
        shares = [j["first30_usd"] / j["jump_usd"] for j in js if j["jump_usd"] > 0]
        cat_rows.append({
            "category": cat,
            "n_jumps": len(js),
            "median_reprice_s": _r(_median([j["reprice_s"] for j in js]), 1),
            "p90_reprice_s": _r(_p90([j["reprice_s"] for j in js]), 1),
            "median_move_cents": _r(_median([j["move_cents"] for j in js]), 2),
            "first30s_usd_share": _r(statistics.mean(shares) if shares else None, 4),
        })
    notes = [
        "Jump = print >=6c from the median of the previous 5 prints on the market's "
        "primary token, extending while prints progress >=1c in the jump direction "
        "within 120s of the last progressing print; reprice_s = last progressing "
        "print minus first (0 = single-print jump, still counted). After each jump "
        "the reference window is re-seeded with post-jump prints and any re-trigger "
        "on the same market within %ds is merged into the same episode, so n_jumps "
        "counts distinct repricing episodes, not echoes of one move."
        % JUMP_MERGE_COOLDOWN_S,
        "Only markets with >=%d captured fills are scanned; thin markets and "
        "truncated histories (%d markets hit the API history cap — see "
        "window.truncated_markets) under-count jumps."
        % (JUMP_MIN_MARKET_TRADES, len(truncated)),
        "Cross-market rows: each futures follower episode appears at most once, "
        "matched to the nearest preceding match-1X2 jump on the same team within "
        "30 minutes (newest 20 shown); Draw markets are skipped (no team to "
        "propagate to).",
    ]
    return {
        "n_jumps": len(jumps),
        "by_category": cat_rows,
        "cross_market": cross_market_rows(jumps),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Feed assembly.
# ---------------------------------------------------------------------------


def _is_partial(ws: WalletStats, backfilled: set) -> bool:
    """True when the wallet's stored fill history is provably incomplete.

    Gross in an offset-capped market means legs can be missing (small fills
    beyond the cap are unreachable through the market sweeps) — unless the
    wallet's full history was re-swept via the per-user endpoint.
    """
    return ws.truncated_usd > 0 and ws.wallet.lower() not in backfilled


def _wallet_row(ws: WalletStats, backfilled: set) -> dict:
    top_cat = max(ws.category_usd, key=lambda c: ws.category_usd[c]) if ws.category_usd else None
    name = ws.name
    if ws.wallet.lower() == WCA_FUNDER.lower():
        name = "(WCA bot)"
    return {
        "wallet": ws.wallet,
        "name": name,
        "trades": ws.trades,
        "gross_usd": _r(ws.gross_usd, 2),
        "avg_usd": _r(ws.gross_usd / ws.trades if ws.trades else None, 2),
        "realized_pnl": _r(ws.realized_pnl, 2),
        "mtm_pnl": _r(ws.mtm_pnl, 2),
        "total_pnl": _r(ws.total_pnl, 2),
        "roi": _r(ws.roi, 4),
        "win_rate": _r(ws.win_rate, 4),
        "informedness_cents": _r(ws.informedness_cents, 2),
        "archetype": ws.archetype(),
        "top_category": top_cat,
        "partial_history": _is_partial(ws, backfilled),
    }


def _cohort_agg(members: List[WalletStats]) -> dict:
    gross = sum(ws.gross_usd for ws in members)
    pnl = sum(ws.total_pnl for ws in members)
    return {
        "n_wallets": len(members),
        "gross_usd": _r(gross, 2),
        "roi": _r(pnl / gross if gross > 0 else None, 4),
    }


def build(db_path: str, min_trades: int, min_gross: float) -> dict:
    markets, trades, truncated, truncated_cids, backfilled = load_db(db_path)
    wallets, _prints = compute_wallets(markets, trades, truncated_cids)
    smart_ids, dumb_ids, scores, definition = build_cohorts(wallets, min_trades, min_gross)
    smart_set, dumb_set = set(smart_ids), set(dumb_ids)
    jumps = detect_jumps(markets, trades)

    # -- first movers --
    fm: Dict[str, dict] = {}
    for j in jumps:
        seen = set()
        for w, usd in j["first30_fills"]:
            rec = fm.setdefault(w, {"n": 0, "usd": []})
            rec["usd"].append(usd)
            if w not in seen:
                rec["n"] += 1
                seen.add(w)
    n_jumps = len(jumps)
    first_movers = []
    for w, rec in sorted(fm.items(), key=lambda kv: (-kv[1]["n"], -sum(kv[1]["usd"]))):
        ws = wallets.get(w)
        name = ws.name if ws else None
        if w.lower() == WCA_FUNDER.lower():
            name = "(WCA bot)"
        first_movers.append({
            "wallet": w,
            "name": name,
            "n_first_mover": rec["n"],
            "jump_share": _r(rec["n"] / n_jumps if n_jumps else None, 4),
            "avg_usd": _r(statistics.mean(rec["usd"]) if rec["usd"] else None, 2),
            "total_pnl": _r(ws.total_pnl if ws else None, 2),
            "partial_history": _is_partial(ws, backfilled) if ws else False,
        })
    first_movers = first_movers[:15]

    # -- leaderboards --
    smart_rows = sorted(
        (wallets[w] for w in smart_ids), key=lambda ws: ws.total_pnl, reverse=True
    )
    dumb_rows = sorted((wallets[w] for w in dumb_ids), key=lambda ws: ws.total_pnl)
    whale_rows = sorted(wallets.values(), key=lambda ws: ws.gross_usd, reverse=True)

    # -- archetypes --
    arch_members: Dict[str, List[WalletStats]] = defaultdict(list)
    for ws in wallets.values():
        arch_members[ws.archetype()].append(ws)
    total_usd = sum(ws.gross_usd for ws in wallets.values())
    archetypes = []
    for key, label, desc in ARCHETYPES:
        members = arch_members.get(key) or []
        if not members:
            continue
        gross = sum(ws.gross_usd for ws in members)
        pnl = sum(ws.total_pnl for ws in members)
        all_trade_usd = [u for ws in members for u in ws.usd_list]
        archetypes.append({
            "key": key,
            "label": label,
            "description": desc,
            "n_wallets": len(members),
            "gross_usd": _r(gross, 2),
            "usd_share": _r(gross / total_usd if total_usd > 0 else None, 4),
            "median_trade_usd": _r(_median(all_trade_usd), 2),
            "roi": _r(pnl / gross if gross > 0 else None, 4),
        })

    # -- category matrix --
    cat_acc: Dict[str, dict] = defaultdict(
        lambda: {"n": 0, "usd": 0.0, "buy": 0.0, "sell": 0.0,
                 "wallets": set(), "smart": 0.0, "dumb": 0.0}
    )
    for t in trades:
        cat = (markets.get(t["condition_id"]) or {}).get("category") or "other_future"
        a = cat_acc[cat]
        usd = float(t["usd"])
        a["n"] += 1
        a["usd"] += usd
        a["wallets"].add(t["wallet"])
        if (t["side"] or "").upper() == "BUY":
            a["buy"] += usd
        else:
            a["sell"] += usd
        if t["wallet"] in smart_set:
            a["smart"] += usd
        if t["wallet"] in dumb_set:
            a["dumb"] += usd
    category_matrix = []
    for cat in sorted(cat_acc, key=lambda c: -cat_acc[c]["usd"]):
        a = cat_acc[cat]
        two_way = a["buy"] + a["sell"]
        category_matrix.append({
            "category": cat,
            "label": CATEGORY_LABELS.get(cat, cat),
            "n_trades": a["n"],
            "usd": _r(a["usd"], 2),
            "n_wallets": len(a["wallets"]),
            "avg_trade_usd": _r(a["usd"] / a["n"] if a["n"] else None, 2),
            "smart_usd_share": _r(a["smart"] / a["usd"] if a["usd"] > 0 else None, 4),
            "dumb_usd_share": _r(a["dumb"] / a["usd"] if a["usd"] > 0 else None, 4),
            "buy_pressure": _r(a["buy"] / two_way if two_way > 0 else None, 4),
        })

    # -- size distribution --
    size_distribution = []
    for label, lo, hi in SIZE_BUCKETS:
        usds = [float(t["usd"]) for t in trades if lo <= float(t["usd"]) < hi]
        size_distribution.append({"bucket": label, "n": len(usds), "usd": _r(sum(usds), 2)})

    # -- window / headline --
    ts_all = [int(t["ts"]) for t in trades]
    usd_volume = sum(float(t["usd"]) for t in trades)
    buy_total = sum(float(t["usd"]) for t in trades if (t["side"] or "").upper() == "BUY")
    window = {
        "from_utc": _iso(min(ts_all)) if ts_all else None,
        "to_utc": _iso(max(ts_all)) if ts_all else None,
        "n_trades": len(trades),
        "n_wallets": len(wallets),
        "n_markets": len({t["condition_id"] for t in trades}),
        "usd_volume": _r(usd_volume, 2),
        "truncated_markets": truncated,
    }

    smart_agg = _cohort_agg([wallets[w] for w in smart_ids])
    dumb_agg = _cohort_agg([wallets[w] for w in dumb_ids])
    top_whale = whale_rows[0] if whale_rows else None
    med_reprice = _median([j["reprice_s"] for j in jumps])

    def _pct(x: Optional[float]) -> str:
        return "n/a" if x is None else "%+.1f%%" % (100.0 * x)

    headline = [
        {
            "label": "Taker USD volume",
            "value": "$%s across %d fills / %d markets"
            % (format(round(usd_volume), ","), len(trades), window["n_markets"]),
            "caveat": "Taker side only — maker flow and identities unobserved.",
        },
        {
            "label": "Smart cohort ROI",
            "value": "%s (%d wallets, $%s gross)"
            % (_pct(smart_agg["roi"]), smart_agg["n_wallets"],
               format(round(smart_agg["gross_usd"] or 0), ",")),
            "caveat": "In-sample selection — the cohort is picked BY performance.",
        },
        {
            "label": "Dumb cohort ROI",
            "value": "%s (%d wallets, $%s gross)"
            % (_pct(dumb_agg["roi"]), dumb_agg["n_wallets"],
               format(round(dumb_agg["gross_usd"] or 0), ",")),
            "caveat": "Same in-sample caveat; small samples, fade with care.",
        },
        {
            "label": "Overall buy pressure",
            "value": "%.1f%% of USD is taker BUYs"
            % (100.0 * buy_total / usd_volume if usd_volume > 0 else 0.0),
            "caveat": "Every taker BUY has an unseen maker on the other side.",
        },
        {
            "label": "Price jumps detected",
            "value": "%d jumps, median reprice %ss"
            % (n_jumps, "n/a" if med_reprice is None else "%.1f" % med_reprice),
            "caveat": "Move-based detection, unattributed to specific match events.",
        },
    ]
    if top_whale is not None:
        headline.append({
            "label": "Largest wallet",
            "value": "$%s gross, %s ROI"
            % (format(round(top_whale.gross_usd), ","), _pct(top_whale.roi)),
            "caveat": "Gross = both sides of taker fills; not net exposure.",
        })

    honesty_notes = [
        "Taker-side data only: data-api /trades names the taker wallet on each "
        "fill; maker identities (resting orders, market makers) are never seen, "
        "so buy_pressure and all wallet stats describe aggressive flow only.",
        "The trades API hard-caps history at 3,000 rows offset per market; %d "
        "markets hit that cap (listed in window.truncated_markets) and only "
        "carry a deep-history sweep of LARGE trades beyond it — so small-trade "
        "stats there are incomplete AND per-wallet PnL/ROI/win-rate can be "
        "missing legs entirely for wallets active in those markets. Leaderboard "
        "rows carry partial_history=true when affected; %d wallets have had "
        "their complete history re-swept via the per-user endpoint and are "
        "exempt." % (len(truncated), len(backfilled)),
        "Open-position marks are LAST-TRADE prints, not order-book mids — thin "
        "tokens can carry stale or manipulated marks into mtm_pnl and ROI.",
        "Jump detection is purely price-move based and is NOT attributed to "
        "specific match events (no goal-timestamp feed integrated); some "
        "'jumps' are liquidity gaps rather than news.",
        "Smart/dumb cohorts are in-sample, WC-only performance ranks on small "
        "per-wallet samples with survivorship effects — treat as descriptive, "
        "not as a copy-trade signal.",
    ]

    now = datetime.now(timezone.utc).isoformat()
    feed = {
        "generated_utc": now,
        "status": "measured",
        "window": window,
        "headline": headline,
        "cohorts": {"definition": definition, "smart": smart_agg, "dumb": dumb_agg},
        "leaderboards": {
            "smart": [_wallet_row(ws, backfilled) for ws in smart_rows[:15]],
            "dumb": [_wallet_row(ws, backfilled) for ws in dumb_rows[:15]],
            "whales": [_wallet_row(ws, backfilled) for ws in whale_rows[:15]],
            "first_movers": first_movers,
        },
        "archetypes": archetypes,
        "category_matrix": category_matrix,
        "size_distribution": size_distribution,
        "latency": latency_block(jumps, truncated),
        "honesty_notes": honesty_notes,
    }
    return _sanitize(feed)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket orderflow analytics (read-only)")
    ap.add_argument("--db", default=DEFAULT_DB, help="sqlite db path (default data/pm_orderflow.db)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output json path")
    ap.add_argument("--min-trades", type=int, default=MIN_TRADES,
                    help="cohort qualification: min trades (default %d)" % MIN_TRADES)
    ap.add_argument("--min-gross", type=float, default=MIN_GROSS_USD,
                    help="cohort qualification: min gross USD (default %d)" % int(MIN_GROSS_USD))
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit("db not found: %s (run the ingester first)" % args.db)

    feed = build(args.db, args.min_trades, args.min_gross)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(feed, fh, indent=2, ensure_ascii=False, allow_nan=False)

    w = feed["window"]
    print("wrote %s" % args.out)
    print("  window: %s -> %s | %d trades / %d wallets / %d markets / $%.0f"
          % (w["from_utc"], w["to_utc"], w["n_trades"], w["n_wallets"],
             w["n_markets"], w["usd_volume"] or 0))
    c = feed["cohorts"]
    print("  cohorts: smart n=%d roi=%s | dumb n=%d roi=%s"
          % (c["smart"]["n_wallets"], c["smart"]["roi"],
             c["dumb"]["n_wallets"], c["dumb"]["roi"]))
    lat = feed["latency"]
    print("  jumps: %d (%d categories), cross-market pairs: %d"
          % (lat["n_jumps"], len(lat["by_category"]), len(lat["cross_market"])))
    if w["truncated_markets"]:
        print("  truncated markets: %d (history capped; see window.truncated_markets)"
              % len(w["truncated_markets"]))


if __name__ == "__main__":
    main()
