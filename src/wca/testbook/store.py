"""Storage for the isolated paper-trading test book.

Pure SQLite, network-free, fully unit-testable. A paper bet is a binary YES
position bought at ``entry_price`` (a Polymarket share price in [0,1]) for
``stake_usd``: it buys ``stake_usd / entry_price`` shares, so

    win  -> payout = shares * $1  ->  pl = stake * (1 - entry_price) / entry_price
    lose -> payout = 0            ->  pl = -stake
    void -> pl = 0

Every bet records a ``resolution_basis`` so the FT-result vs advance distinction
is first-class in tracking:

* ``FT``       — full-time 1X2 (90' + stoppage; a draw is possible).
* ``advance``  — progress after extra-time + penalties.
* ``prop``     — player/team prop (SOT, cards, anytime scorer, …).
* ``exact``    — exact score.   ``corners`` / ``cards`` / ``totals`` / ``btts`` …
* ``outright`` — tournament future (champion, golden boot, …).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, Optional

DEFAULT_DB = os.path.join("data", "test_book.db")
DEFAULT_SEED_USD = 2000.0

RESOLUTION_BASES = ("FT", "advance", "prop", "exact", "corners", "cards",
                    "totals", "btts", "handicap", "outright", "other")

_DDL_BETS = """
CREATE TABLE IF NOT EXISTS paper_bets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc           TEXT NOT NULL,
    fixture          TEXT,
    market_type      TEXT NOT NULL,
    selection        TEXT NOT NULL,
    resolution_basis TEXT NOT NULL DEFAULT 'other',
    token_id         TEXT,
    entry_price      REAL NOT NULL,
    stake_usd        REAL NOT NULL,
    model_prob       REAL,
    edge             REAL,
    kelly_fraction   REAL,
    status           TEXT NOT NULL DEFAULT 'open',
    exit_price       REAL,
    settled_pl       REAL,
    settled_ts       TEXT,
    notes            TEXT
)
"""

_DDL_BANKROLL = """
CREATE TABLE IF NOT EXISTS bankroll_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc    TEXT NOT NULL,
    kind      TEXT NOT NULL,
    amount    REAL NOT NULL,
    balance   REAL NOT NULL,
    note      TEXT
)
"""

# Latest mark-to-market price per open bet (history kept for an equity curve).
_DDL_MARKS = """
CREATE TABLE IF NOT EXISTS marks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT NOT NULL,
    paper_bet_id  INTEGER NOT NULL,
    mark_price    REAL NOT NULL,
    unrealized_pl REAL NOT NULL
)
"""


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con


_DDL_DECISIONS = """
CREATE TABLE IF NOT EXISTS decision_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT NOT NULL,
    paper_bet_id      INTEGER NOT NULL,
    token_id          TEXT,
    fixture           TEXT,
    resolution_basis  TEXT NOT NULL,
    action            TEXT NOT NULL,          -- add | trim | close
    rule              TEXT NOT NULL,          -- which rule fired | manual
    rule_threshold    REAL,
    q_t               REAL NOT NULL,          -- model prob at decision (INV-4)
    q_source          TEXT NOT NULL,          -- entry_static | card_refresh (INV-3)
    q_staleness_min   INTEGER,
    p_t               REAL NOT NULL,          -- transactable: bid(trim/close) / ask(add) (INV-2)
    p_mid_t           REAL,
    spread_t          REAL,
    depth_t           REAL,
    vol_t             REAL,
    equity_t          REAL NOT NULL,
    kelly_mult        REAL NOT NULL,
    max_stake_frac    REAL NOT NULL,
    stake_before_usd  REAL NOT NULL,
    stake_after_usd   REAL NOT NULL,
    shares_delta      REAL NOT NULL,
    f_target          REAL NOT NULL,          -- INV-1 capped target
    f_kelly_raw       REAL,
    h_before          REAL NOT NULL,
    h_after           REAL NOT NULL,
    gog               REAL NOT NULL,          -- h_after - f_target (headline process score)
    delta_g           REAL NOT NULL,          -- log-growth sacrificed under q_t, <= 0
    exit_spread_cost  REAL,
    cap_binding       INTEGER NOT NULL,
    -- OUTCOME (quarantined, backfilled at settlement — INV-5)
    settled_outcome   TEXT,
    settled_pl        REAL,
    settled_ts        TEXT,
    delta_ev          REAL,
    realized_regret   REAL
)
"""

# Columns added to `marks` so each cycle persists the real (bid/ask) book + q.
_MARK_EXTRA_COLS = (
    ("bid_price", "REAL"), ("ask_price", "REAL"), ("spread", "REAL"),
    ("depth_bid", "REAL"), ("q_at_mark", "REAL"), ("q_source", "TEXT"),
)


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(_DDL_BETS)
    con.execute(_DDL_BANKROLL)
    con.execute(_DDL_MARKS)
    con.execute(_DDL_DECISIONS)
    for col, typ in _MARK_EXTRA_COLS:  # idempotent ALTERs for pre-existing DBs
        try:
            con.execute("ALTER TABLE marks ADD COLUMN %s %s" % (col, typ))
        except sqlite3.OperationalError:
            pass  # already present
    con.execute("CREATE INDEX IF NOT EXISTS ix_pb_status ON paper_bets(status, ts_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_de_settle ON decision_events(settled_outcome, resolution_basis)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_de_bet ON decision_events(paper_bet_id)")
    con.commit()


# --------------------------------------------------------------------------- bankroll


def seed_bankroll(con: sqlite3.Connection, amount: float = DEFAULT_SEED_USD, *,
                  ts_utc: str, note: str = "test book seed") -> bool:
    """Seed the starting bankroll once. No-op if any bankroll event exists."""
    n = con.execute("SELECT COUNT(*) FROM bankroll_events").fetchone()[0]
    if n:
        return False
    con.execute("INSERT INTO bankroll_events(ts_utc, kind, amount, balance, note) VALUES (?,?,?,?,?)",
                (ts_utc, "seed", float(amount), float(amount), note))
    con.commit()
    return True


def realized_balance(con: sqlite3.Connection) -> float:
    row = con.execute("SELECT balance FROM bankroll_events ORDER BY id DESC LIMIT 1").fetchone()
    return float(row[0]) if row else 0.0


def _bankroll_delta(con: sqlite3.Connection, kind: str, amount: float, ts_utc: str, note: str) -> None:
    bal = realized_balance(con) + float(amount)
    con.execute("INSERT INTO bankroll_events(ts_utc, kind, amount, balance, note) VALUES (?,?,?,?,?)",
                (ts_utc, kind, float(amount), bal, note))


# --------------------------------------------------------------------------- bets


def pl_if_win(entry_price: float, stake_usd: float) -> float:
    """P&L of a winning binary YES position bought at ``entry_price``."""
    if entry_price <= 0:
        return 0.0
    return stake_usd * (1.0 - entry_price) / entry_price


# --- Pure decision math (shared by the trader + the decision-quality scorer so
#     the metric and the sizing can never diverge — INV-1). -------------------


def kelly_fraction(q: float, p: float) -> float:
    """Binary-YES Kelly fraction: f* = (q - p)/(1 - p), floored at 0."""
    if not (0.0 < p < 1.0):
        return 0.0
    return max(0.0, (q - p) / (1.0 - p))


def f_target(q: float, p: float, kelly_mult: float, max_stake_frac: float) -> float:
    """The stake fraction the trader could ACTUALLY take: capped fractional Kelly.

    Scoring against this (never raw Kelly) is INV-1 — comparing a decision to an
    unreachable raw-Kelly target manufactures a fake negative-sizing bias.
    """
    return min(kelly_mult * kelly_fraction(q, p), max_stake_frac)


def g_logwealth(f: float, q: float, p: float) -> float:
    """Expected log-growth of staking fraction ``f`` on a YES at price ``p`` under
    belief ``q``: g = q·ln(1 + f·(1-p)/p) + (1-q)·ln(1-f). Outcome never enters."""
    import math

    if not (0.0 < p < 1.0) or f >= 1.0:
        return float("-inf") if f >= 1.0 else 0.0
    b = (1.0 - p) / p
    win = 1.0 + f * b
    lose = 1.0 - f
    if win <= 0 or lose <= 0:
        return float("-inf")
    return q * math.log(win) + (1.0 - q) * math.log(lose)


def deployed_capital(con: sqlite3.Connection) -> float:
    """Total stake currently tied up in open paper positions."""
    row = con.execute("SELECT COALESCE(SUM(stake_usd),0) FROM paper_bets WHERE status='open'").fetchone()
    return float(row[0] or 0.0)


def log_paper_bet(con: sqlite3.Connection, *, ts_utc: str, fixture: str, market_type: str,
                  selection: str, resolution_basis: str, entry_price: float, stake_usd: float,
                  token_id: Optional[str] = None, model_prob: Optional[float] = None,
                  edge: Optional[float] = None, kelly_fraction: Optional[float] = None,
                  notes: Optional[str] = None) -> int:
    """Record an open paper position. Returns the new bet id.

    Stake is reserved from the bankroll (so realized balance reflects cash at
    risk); it is returned with P&L at settlement.
    """
    cur = con.execute(
        "INSERT INTO paper_bets(ts_utc, fixture, market_type, selection, resolution_basis,"
        " token_id, entry_price, stake_usd, model_prob, edge, kelly_fraction, status, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (ts_utc, fixture, market_type, selection, resolution_basis, token_id,
         float(entry_price), float(stake_usd), model_prob, edge, kelly_fraction, notes))
    bet_id = int(cur.lastrowid)
    _bankroll_delta(con, "stake", -float(stake_usd), ts_utc, "stake bet #%d" % bet_id)
    con.commit()
    return bet_id


def open_bets(con: sqlite3.Connection) -> List[Dict[str, object]]:
    return [dict(r) for r in con.execute("SELECT * FROM paper_bets WHERE status='open' ORDER BY id")]


def settle(con: sqlite3.Connection, bet_id: int, *, outcome: str, ts_utc: str,
           exit_price: Optional[float] = None) -> float:
    """Settle a paper bet. ``outcome`` in {'won','lost','void'}; returns P&L.

    Stake + P&L are credited back to the bankroll (void returns the stake).
    """
    row = con.execute("SELECT entry_price, stake_usd, status FROM paper_bets WHERE id=?", (bet_id,)).fetchone()
    if row is None or row["status"] != "open":
        return 0.0
    entry, stake = float(row["entry_price"]), float(row["stake_usd"])
    if outcome == "won":
        pl = pl_if_win(entry, stake)
    elif outcome == "void":
        pl = 0.0
    else:
        pl = -stake
    con.execute("UPDATE paper_bets SET status=?, settled_pl=?, settled_ts=?, exit_price=? WHERE id=?",
                (outcome, pl, ts_utc, exit_price, bet_id))
    # Return stake + realize P&L into the bankroll.
    _bankroll_delta(con, "settle", stake + pl, ts_utc, "settle bet #%d (%s)" % (bet_id, outcome))
    con.commit()
    return pl


def record_mark(con: sqlite3.Connection, bet_id: int, mark_price: float, ts_utc: str, *,
                bid_price: Optional[float] = None, ask_price: Optional[float] = None,
                spread: Optional[float] = None, depth_bid: Optional[float] = None,
                q_at_mark: Optional[float] = None, q_source: Optional[str] = None) -> float:
    """Mark an open position to ``mark_price`` (CLOB mid); returns unrealised P&L.

    The bid/ask/depth/q fields persist the real book + belief at mark time so the
    decision scorer can use a transactable exit price (INV-2) and stratify by q
    staleness (INV-3)."""
    row = con.execute("SELECT entry_price, stake_usd FROM paper_bets WHERE id=? AND status='open'",
                      (bet_id,)).fetchone()
    if row is None:
        return 0.0
    entry, stake = float(row["entry_price"]), float(row["stake_usd"])
    shares = stake / entry if entry > 0 else 0.0
    unreal = shares * float(mark_price) - stake
    con.execute(
        "INSERT INTO marks(ts_utc, paper_bet_id, mark_price, unrealized_pl,"
        " bid_price, ask_price, spread, depth_bid, q_at_mark, q_source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts_utc, bet_id, float(mark_price), unreal, bid_price, ask_price, spread,
         depth_bid, q_at_mark, q_source))
    con.commit()
    return unreal


# --------------------------------------------------------------------------- trim / close


def trim(con: sqlite3.Connection, bet_id: int, shares_sold: float, exit_price: float,
         ts_utc: str) -> float:
    """Sell ``shares_sold`` of an open YES position at ``exit_price`` (the bid).

    Reduces the position IN PLACE (no row split — keeps the marks join intact),
    returns the realised P&L on the trimmed shares. If it would sell the whole
    position, it routes to :func:`close`.
    """
    row = con.execute("SELECT entry_price, stake_usd, status FROM paper_bets WHERE id=?",
                      (bet_id,)).fetchone()
    if row is None or row["status"] != "open":
        return 0.0
    p0, stake = float(row["entry_price"]), float(row["stake_usd"])
    shares0 = stake / p0 if p0 > 0 else 0.0
    sold = min(max(0.0, float(shares_sold)), shares0)
    if sold <= 0:
        return 0.0
    if shares0 - sold <= 1e-9:
        return close(con, bet_id, exit_price, ts_utc)
    proceeds = sold * float(exit_price)
    new_stake = (shares0 - sold) * p0
    con.execute("UPDATE paper_bets SET stake_usd=? WHERE id=?", (new_stake, bet_id))
    _bankroll_delta(con, "trim", proceeds, ts_utc, "trim %.2f sh bet #%d" % (sold, bet_id))
    con.commit()
    return proceeds - sold * p0


def close(con: sqlite3.Connection, bet_id: int, exit_price: float, ts_utc: str) -> float:
    """Exit a whole open position at ``exit_price`` (the bid) BEFORE settlement.

    Status becomes ``closed`` (distinct from outcome-settled won/lost/void);
    realised P&L is booked vs the cost basis. Returns realised P&L.
    """
    row = con.execute("SELECT entry_price, stake_usd, status FROM paper_bets WHERE id=?",
                      (bet_id,)).fetchone()
    if row is None or row["status"] != "open":
        return 0.0
    p0, stake = float(row["entry_price"]), float(row["stake_usd"])
    shares0 = stake / p0 if p0 > 0 else 0.0
    proceeds = shares0 * float(exit_price)
    pl = proceeds - stake
    con.execute("UPDATE paper_bets SET status='closed', settled_pl=?, settled_ts=?, exit_price=? WHERE id=?",
                (pl, ts_utc, float(exit_price), bet_id))
    _bankroll_delta(con, "close", proceeds, ts_utc, "close bet #%d @ %.3f" % (bet_id, exit_price))
    con.commit()
    return pl


# --------------------------------------------------------------------------- decision log


def log_decision(con: sqlite3.Connection, *, action: str, rule: str, bet_id: int,
                 token_id: Optional[str], fixture: Optional[str], resolution_basis: str,
                 q_t: float, q_source: str, p_t: float, equity_t: float,
                 kelly_mult: float, max_stake_frac: float, entry_price: float,
                 stake_before: float, stake_after: float, shares_delta: float,
                 p_mid_t: Optional[float] = None, spread_t: Optional[float] = None,
                 depth_t: Optional[float] = None, vol_t: Optional[float] = None,
                 q_staleness_min: Optional[int] = None, rule_threshold: Optional[float] = None,
                 ts_utc: str = "") -> int:
    """Log an add/trim/close as a decision event with all PROCESS scores computed
    INLINE from decision-time inputs only (the structural no-hindsight wall, INV-5).
    Outcome columns are left NULL and backfilled later by settlement."""
    fk_raw = kelly_fraction(q_t, p_t)
    ft = f_target(q_t, p_t, kelly_mult, max_stake_frac)
    cap_binding = 1 if (kelly_mult * fk_raw > max_stake_frac + 1e-12) else 0
    eq = equity_t if equity_t > 0 else 1.0
    p0 = entry_price if entry_price > 0 else p_t
    h_before = (stake_before / p0) * p_t / eq if p0 > 0 else 0.0
    h_after = (stake_after / p0) * p_t / eq if p0 > 0 else 0.0
    gog = h_after - ft
    dg = g_logwealth(h_after, q_t, p_t) - g_logwealth(ft, q_t, p_t)
    esc = (((p_mid_t - p_t) * shares_delta) if (action in ("trim", "close") and p_mid_t is not None)
           else None)
    cur = con.execute(
        "INSERT INTO decision_events(ts_utc, paper_bet_id, token_id, fixture, resolution_basis,"
        " action, rule, rule_threshold, q_t, q_source, q_staleness_min, p_t, p_mid_t, spread_t,"
        " depth_t, vol_t, equity_t, kelly_mult, max_stake_frac, stake_before_usd, stake_after_usd,"
        " shares_delta, f_target, f_kelly_raw, h_before, h_after, gog, delta_g, exit_spread_cost,"
        " cap_binding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts_utc, bet_id, token_id, fixture, resolution_basis, action, rule, rule_threshold,
         q_t, q_source, q_staleness_min, p_t, p_mid_t, spread_t, depth_t, vol_t, eq,
         kelly_mult, max_stake_frac, stake_before, stake_after, shares_delta, ft, fk_raw,
         h_before, h_after, gog, dg, esc, cap_binding))
    con.commit()
    return int(cur.lastrowid)


# --------------------------------------------------------------------------- reporting


def report(con: sqlite3.Connection) -> Dict[str, object]:
    """Headline test-book state: balance, exposure, realised/unrealised P&L."""
    realized = realized_balance(con)
    deployed = deployed_capital(con)
    seed_row = con.execute("SELECT amount FROM bankroll_events WHERE kind='seed' ORDER BY id LIMIT 1").fetchone()
    seed = float(seed_row[0]) if seed_row else 0.0
    settled = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(settled_pl),0) FROM paper_bets"
        " WHERE status IN ('won','lost','void','closed')"
    ).fetchone()
    n_settled, realized_pl = int(settled[0]), float(settled[1] or 0.0)
    n_open = con.execute("SELECT COUNT(*) FROM paper_bets WHERE status='open'").fetchone()[0]
    # Latest unrealised P&L per still-open bet.
    unreal = 0.0
    for r in con.execute(
        "SELECT m.unrealized_pl FROM marks m JOIN ("
        "  SELECT paper_bet_id, MAX(id) mid FROM marks GROUP BY paper_bet_id) last"
        " ON m.id=last.mid JOIN paper_bets b ON b.id=m.paper_bet_id WHERE b.status='open'"):
        unreal += float(r[0])
    equity = realized + deployed + unreal  # cash + stake-at-risk + MTM gain/loss
    by_basis = {r[0]: {"n": r[1], "pl": float(r[2] or 0.0)} for r in con.execute(
        "SELECT resolution_basis, COUNT(*), COALESCE(SUM(settled_pl),0) FROM paper_bets"
        " GROUP BY resolution_basis")}
    return {
        "seed": seed, "realized_balance": realized, "deployed": deployed,
        "n_open": int(n_open), "n_settled": n_settled, "realized_pl": realized_pl,
        "unrealized_pl": unreal, "equity": equity,
        "roi_pct": (100.0 * (equity - seed) / seed) if seed else 0.0,
        "by_basis": by_basis,
    }
