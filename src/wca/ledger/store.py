"""SQLite-backed bet ledger for the World Cup Alpha platform.

All state is stored in a single SQLite database file (default
``data/wca.db``). Every public function accepts an explicit ``db_path``
argument so tests can use temporary files without touching the project
database.

Tables
------
bets
    One row per placed bet, with settlement and closing-line columns
    populated lazily.
bankroll_events
    Deposits and withdrawals.
odds_snapshots
    Created by the odds-collection module; this module will create the
    table if it is missing but will not alter its schema.

Closing-line value (CLV)
------------------------
CLV is the single most important bet-quality signal.  We use the
*return-ratio* form:

    CLV% = (decimal_odds_taken / closing_odds) - 1

A positive value means the bettor secured better odds than the closing
price, i.e. they "beat the close".  The closing line is the last price
available just before the match kicks off and is a strong proxy for the
efficient market consensus.

Reference: Levitt (2004) "Why are gambling markets organised differently
from financial markets?", *The Economic Journal* 114(495):223-246; and
the practical treatment in Benter (1994) "Computer based horse race
handicapping and wagering systems", in *Efficiency of Racetrack Betting
Markets* (Hausch, Lo & Ziemba eds).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

logger = logging.getLogger(__name__)

# Statuses whose ``settled_pl`` is a *realised* profit/loss that belongs in the
# bankroll curve, P&L totals and per-pool P&L.  ``cashed`` (a mid-match
# Polymarket cash-out, see :func:`settle_cashout`) joins ``won``/``lost`` here:
# a cash-out realises P&L = sale proceeds − cost basis.  ``cashed`` is
# deliberately *excluded* from CLV / calibration (it carries no closing line and
# no clean binary outcome) — those reports gate on ``clv``/``model_prob`` being
# present, which a cash-out never sets.
REALIZED_STATUSES = ("won", "lost", "cashed")
# Statuses that are no longer "open" (money no longer at risk on that row).
CLOSED_STATUSES = ("won", "lost", "void", "cashed")

from wca.venues import canon_platform


# ---------------------------------------------------------------------------
# Default path (relative to the repo root, used only when the caller does not
# pass an explicit db_path).
# ---------------------------------------------------------------------------

_DEFAULT_DB = "data/wca.db"

# ---------------------------------------------------------------------------
# Schema DDL.
# ---------------------------------------------------------------------------

_DDL_BETS = """
CREATE TABLE IF NOT EXISTS bets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT    NOT NULL,
    match_id            TEXT    NOT NULL,
    match_desc          TEXT    NOT NULL,
    market              TEXT    NOT NULL,
    selection           TEXT    NOT NULL,
    platform            TEXT    NOT NULL,
    decimal_odds        REAL    NOT NULL,
    stake               REAL    NOT NULL,
    model_prob          REAL,
    market_prob_devig   REAL,
    ev                  REAL,
    kelly_fraction      REAL,
    status              TEXT    NOT NULL DEFAULT 'open',
    settled_pl          REAL,
    closing_odds        REAL,
    clv                 REAL,
    notes               TEXT,
    manual_override     TEXT
)
"""

_DDL_BANKROLL_EVENTS = """
CREATE TABLE IF NOT EXISTS bankroll_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc  TEXT    NOT NULL,
    amount  REAL    NOT NULL,
    reason  TEXT
)
"""

# odds_snapshots is owned by the odds-collection module; we just ensure the
# table exists with the canonical schema so ledger queries can JOIN it if
# needed.
_DDL_ODDS_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    ts_utc      TEXT,
    source      TEXT,
    match_id    TEXT,
    market      TEXT,
    selection   TEXT,
    decimal_odds REAL,
    raw         TEXT
)
"""


# ---------------------------------------------------------------------------
# Connection helpers.
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """Return a connection with foreign-keys and WAL-mode enabled.

    Delegates the open to :func:`wca.db.connect`, which picks the shared Turso
    (libSQL) database when ``WCA_DB_URL`` is set and otherwise falls back to a
    local ``sqlite3.connect(db_path)`` — the historical behaviour. The
    ``row_factory`` and the two PRAGMAs are (re)applied here so this function is
    byte-for-byte behaviour-identical to the legacy implementation whenever
    ``WCA_DB_URL`` is unset (dev, tests, and the mini until cut-over).
    """
    import os

    from wca import db as _wca_db

    conn = _wca_db.connect(db_path)
    if os.environ.get("WCA_DB_URL"):
        # Shared Turso (libSQL) path. The experimental client may not accept a
        # sqlite3 ``row_factory`` nor the SQLite-only WAL pragma; apply the same
        # setup best-effort so a capability gap never breaks a read. NOTE: this
        # branch is unexercised by the test suite (no real Turso instance in CI).
        try:  # pragma: no cover - requires a live libSQL connection
            conn.row_factory = sqlite3.Row  # type: ignore[assignment]
        except Exception:
            pass
        for _pragma in ("PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON"):
            try:  # pragma: no cover - requires a live libSQL connection
                conn.execute(_pragma)
            except Exception:
                pass
        return conn

    # Local sqlite fallback: byte-for-byte identical to the legacy behaviour.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = _DEFAULT_DB) -> None:
    """Create all tables if they do not yet exist.

    Safe to call on an existing database; it is a no-op if the tables are
    already present.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  The file and any intermediate directories
        must already exist (or will be created by SQLite automatically for
        the file itself).
    """
    with _connect(db_path) as conn:
        conn.execute(_DDL_BETS)
        conn.execute(_DDL_BANKROLL_EVENTS)
        conn.execute(_DDL_ODDS_SNAPSHOTS)
        _ensure_account_source_columns(conn)
        _ensure_settled_ts_column(conn)
        _ensure_manual_override_column(conn)
        _ensure_cashout_columns(conn)


# ---------------------------------------------------------------------------
# Bet recording and lifecycle.
# ---------------------------------------------------------------------------


def record_bet(
    ts_utc: str,
    match_id: str,
    match_desc: str,
    market: str,
    selection: str,
    platform: str,
    decimal_odds: float,
    stake: float,
    model_prob: Optional[float] = None,
    market_prob_devig: Optional[float] = None,
    ev: Optional[float] = None,
    kelly_fraction: Optional[float] = None,
    notes: Optional[str] = None,
    account: str = "1",
    source: str = "model",
    token_id: Optional[str] = None,
    sync_site: bool = False,
    db_path: str = _DEFAULT_DB,
) -> int:
    """Insert a new open bet into the ledger and return its row ID.

    ``account`` separates physical betting accounts (e.g. "1" = own, "2" = a
    second account) so analytics can split a single venue across them.
    ``source`` tags WHY the bet was placed — "model" (from the card/scanners),
    "offer" (free-bet / promo extraction), or "punt" (a directional bet made on
    judgement, not the model). Keeps the CLV experiment separable from promo
    and discretionary activity.

    Parameters
    ----------
    ts_utc:
        ISO-8601 timestamp of bet placement (UTC), e.g. ``"2026-06-11T14:00:00"``.
    match_id:
        Unique identifier for the match, e.g. ``"GRP_A_01"``.
    match_desc:
        Human-readable match description, e.g. ``"Mexico vs Canada"``.
    market:
        Bet market type, e.g. ``"1X2"`` or ``"BTTS"``.
    selection:
        The specific outcome backed, e.g. ``"Home"`` or ``"Over 2.5"``.
    platform:
        Bookmaker or exchange name, e.g. ``"Bet365"``.
    decimal_odds:
        Decimal (European) odds at which the bet was placed.
    stake:
        Currency amount staked.
    model_prob:
        Model-derived win probability for this selection (optional).
    market_prob_devig:
        De-vigged market-implied probability for this selection (optional).
    ev:
        Expected value of the bet in currency (optional).
    kelly_fraction:
        Kelly fraction used to size this bet (optional).
    notes:
        Free-text notes (optional).
    sync_site:
        When True, regenerate + publish the site feed after the insert so the
        newly recorded bet shows up on the site without a separate manual step
        (see :func:`_sync_site_after_record`). Best-effort and never raises.
        Defaults to False so the low-level ledger write never triggers a git
        publish on its own: the manual ``wca_cli bet add`` path opts in, while
        the bot paths regenerate once per batch via their own ``_autosync``.
    db_path:
        Path to the SQLite database file.

    Returns
    -------
    int
        The auto-assigned row ``id`` of the newly inserted bet.
    """
    init_db(db_path)
    # Canonicalise the venue name at the single write choke point so every
    # write path (bot screenshot, wca_cli, bot/app direct insert) is normalised.
    platform = canon_platform(platform)
    sql = """
        INSERT INTO bets
            (ts_utc, match_id, match_desc, market, selection, platform,
             decimal_odds, stake, model_prob, market_prob_devig, ev,
             kelly_fraction, status, notes, account, source, token_id)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
    """
    with _connect(db_path) as conn:
        _ensure_account_source_columns(conn)
        _ensure_cashout_columns(conn)
        cur = conn.execute(
            sql,
            (
                ts_utc,
                match_id,
                match_desc,
                market,
                selection,
                platform,
                float(decimal_odds),
                float(stake),
                float(model_prob) if model_prob is not None else None,
                float(market_prob_devig) if market_prob_devig is not None else None,
                float(ev) if ev is not None else None,
                float(kelly_fraction) if kelly_fraction is not None else None,
                notes,
                str(account or "1"),
                str(source or "model"),
                str(token_id) if token_id else None,
            ),
        )
        bet_id = cur.lastrowid
    # Fire the site-sync OUTSIDE the connection context so the row is committed
    # before the feed is regenerated. Never let a sync failure lose the write.
    if sync_site:
        _sync_site_after_record(db_path, bet_id)
    return bet_id


def _sync_site_after_record(db_path: str, bet_id: int) -> None:
    """Regenerate + publish the site feed after a bet is recorded (best-effort).

    Kept as a module-level function (not inlined) so it is a single, clear hook
    that callers and tests can monkeypatch. Delegates to ``wca.sync.push_site``,
    which already self-guards under pytest and only pushes when the feed actually
    changed; any failure here must never break the ledger write.
    """
    try:
        from wca import sync

        sync.push_site(reason="bet %d recorded" % bet_id, db_path=db_path)
    except Exception:
        pass


def _ensure_account_source_columns(conn) -> None:
    """Add account/source columns to pre-existing databases (idempotent)."""
    for col, ddl in (("account", "TEXT DEFAULT '1'"), ("source", "TEXT DEFAULT 'model'")):
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN %s %s" % (col, ddl))
        except Exception:
            pass


def _ensure_settled_ts_column(conn) -> None:
    """Add the settled_ts column to pre-existing databases (idempotent)."""
    try:
        conn.execute("ALTER TABLE bets ADD COLUMN settled_ts TEXT")
    except Exception:
        pass  # already present


def _ensure_manual_override_column(conn) -> None:
    """Add the manual_override column to pre-existing databases (idempotent).

    ``manual_override`` holds a free-text note when a bet has been hand-edited
    on the source-of-truth machine (via ``scripts/wca_override.py``). When set,
    automated graders/backfills must leave the bet untouched so the manual
    correction is never clobbered.
    """
    try:
        conn.execute("ALTER TABLE bets ADD COLUMN manual_override TEXT")
    except Exception:
        pass  # already present


def _ensure_cashout_columns(conn) -> None:
    """Add cash-out columns to pre-existing databases (idempotent).

    ``token_id``       the Polymarket ERC-1155 outcome-token id this row backs,
                       so a held position can be matched to its ledger rows
                       without parsing free-text. Populated on new Polymarket
                       rows; older rows may be NULL (matched by selection then).
    ``cashout_proceeds`` USDC actually received when the row was cashed out
                       (sale price × shares for this row's slice). ``settled_pl``
                       still holds the realised P&L (proceeds − cost basis) so
                       all the existing P&L aggregations work unchanged.
    """
    for col in ("token_id TEXT", "cashout_proceeds REAL"):
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN %s" % col)
        except Exception:
            pass  # already present


def settle_bet(
    bet_id: int,
    result: str,
    db_path: str = _DEFAULT_DB,
    settled_ts_utc: Optional[str] = None,
) -> None:
    """Mark a bet as won or lost and compute the profit/loss.

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to settle.
    result:
        ``"won"`` or ``"lost"``; case-insensitive.
    db_path:
        Path to the SQLite database file.
    settled_ts_utc:
        ISO timestamp of settlement; defaults to the current UTC time. Stored
        so realized-P&L curves can be plotted over settlement time.

    Raises
    ------
    ValueError
        If ``result`` is not ``"won"`` or ``"lost"``, or if the bet is not
        currently open.
    KeyError
        If no bet with ``bet_id`` exists.
    """
    result_lower = result.strip().lower()
    if result_lower not in ("won", "lost"):
        raise ValueError("result must be 'won' or 'lost', got %r" % result)

    init_db(db_path)
    with _connect(db_path) as conn:
        # source/account are added lazily for pre-existing DBs; ensure present
        # so the free-bet/lay-aware settlement below can read source.
        _ensure_account_source_columns(conn)
        row = conn.execute(
            "SELECT status, stake, decimal_odds, source, market, selection "
            "FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)
        if row["status"] != "open":
            raise ValueError(
                "bet %d has status %r; only open bets can be settled" % (bet_id, row["status"])
            )

        stake_val = float(row["stake"])
        odds_val = float(row["decimal_odds"])
        source = str(row["source"] or "model")
        is_free = source == "offer"
        is_lay = "lay" in (str(row["market"] or "") + " " + str(row["selection"] or "")).lower()
        # P&L conventions:
        #  - Back bet:  won -> (odds-1)*stake ;  lost -> -stake.
        #  - Free bet (source='offer', stake NOT returned): won -> (odds-1)*stake
        #    (profit only) ; lost -> £0 (no stake at risk).
        #  - Lay bet (Bet Against): won -> +backer stake ; lost -> -LIABILITY
        #    (stake*(odds-1)), because a lay risks the liability, not the stake.
        if is_lay:
            liability = (odds_val - 1.0) * stake_val
            pl = stake_val if result_lower == "won" else -liability
        elif result_lower == "won":
            pl = (odds_val - 1.0) * stake_val
        else:  # lost
            pl = 0.0 if is_free else -stake_val

        _ensure_settled_ts_column(conn)
        if settled_ts_utc is None:
            import datetime as _dt

            settled_ts_utc = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "UPDATE bets SET status = ?, settled_pl = ?, settled_ts = ? WHERE id = ?",
            (result_lower, pl, settled_ts_utc, bet_id),
        )


def void_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> None:
    """Void a bet (stake returned, no P&L impact).

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to void.
    db_path:
        Path to the SQLite database file.

    Raises
    ------
    KeyError
        If no bet with ``bet_id`` exists.
    ValueError
        If the bet is already settled or voided.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)
        if row["status"] != "open":
            raise ValueError(
                "bet %d has status %r; only open bets can be voided" % (bet_id, row["status"])
            )
        _ensure_settled_ts_column(conn)
        import datetime as _dt

        conn.execute(
            "UPDATE bets SET status = 'void', settled_pl = 0.0, settled_ts = ? WHERE id = ?",
            (_dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"), bet_id),
        )


# ---------------------------------------------------------------------------
# Cash-out: close a Polymarket position (or part of it) at a sale price.
# ---------------------------------------------------------------------------


def _row_shares(stake: float, decimal_odds: float) -> float:
    """Outcome shares a Polymarket BUY row represents.

    Polymarket buys are recorded with ``stake = shares × fill_price`` and
    ``decimal_odds = 1 / fill_price`` (see ``wca_pm_watch.py`` and the bot's
    ``_execute_parked_order``), so ``shares = stake × decimal_odds``.
    """
    s, o = float(stake), float(decimal_odds)
    if s <= 0 or o <= 0:
        return 0.0
    return s * o


def open_position_rows(
    *,
    token_id: Optional[str] = None,
    selection: Optional[str] = None,
    platform: str = "polymarket",
    db_path: str = _DEFAULT_DB,
) -> List[sqlite3.Row]:
    """Return the OPEN ledger rows for one Polymarket position, FIFO by id.

    Matched by ``token_id`` when supplied *and* present on the row; otherwise by
    exact ``selection`` string (the legacy match key — ``pm_watch`` rows carry no
    token id). At least one of ``token_id`` / ``selection`` is required.
    """
    if not token_id and not selection:
        raise ValueError("open_position_rows requires token_id or selection")
    init_db(db_path)
    with _connect(db_path) as conn:
        _ensure_account_source_columns(conn)
        _ensure_cashout_columns(conn)
        if token_id:
            rows = conn.execute(
                "SELECT * FROM bets WHERE platform=? AND status='open' "
                "AND token_id=? ORDER BY id",
                (platform, str(token_id)),
            ).fetchall()
            if rows:
                return rows
            # No token-tagged rows (older fills predate the token_id column);
            # fall back to selection if we were given one.
            if not selection:
                return []
        return conn.execute(
            "SELECT * FROM bets WHERE platform=? AND status='open' "
            "AND selection=? ORDER BY id",
            (platform, selection),
        ).fetchall()


def settle_cashout(
    proceeds: float,
    *,
    token_id: Optional[str] = None,
    selection: Optional[str] = None,
    shares_sold: Optional[float] = None,
    platform: str = "polymarket",
    template: Optional[dict] = None,
    settled_ts_utc: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> dict:
    """Close a Polymarket position (or part of it) at a cash-out sale.

    Marks the matched OPEN buy row(s) ``'cashed'`` with
    ``settled_pl = proceeds_slice − cost_basis`` — so a cash-out flows through
    every existing realised-P&L aggregation (bankroll curve, ``/summary``, per-
    pool P&L) unchanged, while staying out of CLV/calibration (it sets no
    closing line). It never calls :func:`record_bet`, so no phantom *open* row
    is created.

    Sizing. The sale is allocated FIFO across the open rows by share count
    (uniform sale price). When ``shares_sold`` is less than the position, the
    boundary row is *split*: the sold slice becomes a new ``'cashed'`` row and
    the unsold remainder stays ``'open'`` (its stake scaled down, odds kept, so
    its implied share count is exactly the remainder). When ``shares_sold``
    exceeds what the ledger knows we hold (an untracked fill), the excess
    proceeds are booked as one extra ``'cashed'`` row with zero cost basis so no
    realised money is silently dropped.

    Parameters
    ----------
    proceeds:
        Total USDC received for ``shares_sold`` (sale price × shares). ``>= 0``.
    token_id / selection:
        How to find the position's open rows; ``token_id`` wins. One required.
    shares_sold:
        Shares actually sold. ``None`` => sell the entire known open position.
    template:
        Descriptive fields (``match_id``, ``match_desc``, ``market``,
        ``selection``, ``account``, ``source``) used only when a row must be
        created from scratch (untracked excess). Falls back to the first matched
        row, then to safe defaults.
    settled_ts_utc:
        Settlement timestamp; defaults to now (UTC).

    Returns
    -------
    dict
        ``{"bet_ids": [...], "shares_sold", "proceeds", "cost_basis", "pl",
        "rows_cashed", "rows_split", "untracked_shares"}``.

    Raises
    ------
    ValueError
        If neither token_id nor selection is given, ``proceeds`` is negative,
        or ``shares_sold`` is non-positive.
    KeyError
        If no open rows match AND we cannot size the sale (``shares_sold`` is
        ``None`` so there is nothing to close).
    """
    proceeds = float(proceeds)
    if proceeds < 0:
        raise ValueError("proceeds must be >= 0, got %r" % proceeds)
    if shares_sold is not None and float(shares_sold) <= 0:
        raise ValueError("shares_sold must be positive, got %r" % shares_sold)

    if settled_ts_utc is None:
        import datetime as _dt

        settled_ts_utc = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    rows = open_position_rows(
        token_id=token_id, selection=selection, platform=platform, db_path=db_path
    )
    total_held = sum(_row_shares(r["stake"], r["decimal_odds"]) for r in rows)

    if shares_sold is None:
        if not rows or total_held <= 0:
            raise KeyError(
                "no open position to cash out (token_id=%r selection=%r): "
                "%d open row(s), %.4f sellable shares"
                % (token_id, selection, len(rows), total_held)
            )
        shares_sold = total_held
    shares_sold = float(shares_sold)

    # Relative tolerance: position-API sizes and ledger-implied shares can differ
    # by rounding dust; treat a sale within tol of the held amount as "all".
    tol = max(1e-6, 1e-4 * max(total_held, shares_sold))
    sale_price = proceeds / shares_sold if shares_sold > 0 else 0.0

    summary = {
        "bet_ids": [],
        "shares_sold": round(shares_sold, 6),
        "proceeds": round(proceeds, 6),
        "cost_basis": 0.0,
        "pl": 0.0,
        "rows_cashed": 0,
        "rows_split": 0,
        "untracked_shares": 0.0,
    }
    cost_basis = 0.0

    with _connect(db_path) as conn:
        _ensure_account_source_columns(conn)
        _ensure_cashout_columns(conn)
        _ensure_settled_ts_column(conn)

        remaining = shares_sold
        for r in rows:
            if remaining <= tol:
                break
            shares_i = _row_shares(r["stake"], r["decimal_odds"])
            if shares_i <= 0:
                # A zero-share open row (stake or odds <= 0) can't be cashed out;
                # it would otherwise sit 'open' forever. Surface it.
                logger.warning(
                    "settle_cashout: skipping open bet id=%s with zero sellable "
                    "shares (stake=%s odds=%s)", r["id"], r["stake"], r["decimal_odds"]
                )
                continue
            stake_i = float(r["stake"])
            odds_i = float(r["decimal_odds"])
            consume = min(shares_i, remaining)
            proceeds_i = round(consume * sale_price, 6)

            if consume >= shares_i - tol:
                # Whole row sold.
                pl_i = round(proceeds_i - stake_i, 6)
                conn.execute(
                    "UPDATE bets SET status='cashed', settled_pl=?, "
                    "cashout_proceeds=?, settled_ts=?, notes=? WHERE id=?",
                    (
                        pl_i,
                        proceeds_i,
                        settled_ts_utc,
                        _append_note(r["notes"], "cashed %.4f sh @ %.4f" % (shares_i, sale_price)),
                        r["id"],
                    ),
                )
                summary["bet_ids"].append(int(r["id"]))
                summary["rows_cashed"] += 1
                cost_basis += stake_i
            else:
                # Partial row: split. Remainder stays open; sold slice -> new row.
                cost_i = round(stake_i * (consume / shares_i), 6)
                remainder_stake = round(stake_i - cost_i, 6)
                pl_i = round(proceeds_i - cost_i, 6)
                conn.execute(
                    "UPDATE bets SET stake=? WHERE id=?",
                    (remainder_stake, r["id"]),
                )
                new_id = _insert_cashed_slice(
                    conn,
                    template_row=r,
                    stake=cost_i,
                    decimal_odds=odds_i,
                    settled_pl=pl_i,
                    cashout_proceeds=proceeds_i,
                    settled_ts=settled_ts_utc,
                    note="cashout slice %.4f sh @ %.4f" % (consume, sale_price),
                )
                summary["bet_ids"].append(new_id)
                summary["rows_split"] += 1
                cost_basis += cost_i
            remaining -= consume

        # Sold more than the ledger knew we held -> book the excess proceeds so
        # realised P&L stays faithful (zero cost basis: we never recorded buying
        # these shares).
        if remaining > tol:
            excess_proceeds = round(remaining * sale_price, 6)
            tmpl = template or (dict(rows[0]) if rows else None)
            new_id = _insert_cashed_slice(
                conn,
                template_row=tmpl,
                stake=0.0,
                decimal_odds=(1.0 / sale_price) if sale_price > 0 else 0.0,
                settled_pl=excess_proceeds,
                cashout_proceeds=excess_proceeds,
                settled_ts=settled_ts_utc,
                note="untracked %.4f sh cashed @ %.4f (no recorded cost basis)"
                % (remaining, sale_price),
                token_id=token_id,
                selection=selection,
                platform=platform,
            )
            summary["bet_ids"].append(new_id)
            summary["rows_cashed"] += 1
            summary["untracked_shares"] = round(remaining, 6)

    summary["cost_basis"] = round(cost_basis, 6)
    summary["pl"] = round(proceeds - cost_basis, 6)
    return summary


def _append_note(existing: Optional[str], add: str) -> str:
    existing = (existing or "").strip()
    return (existing + " | " + add) if existing else add


def _insert_cashed_slice(
    conn,
    *,
    template_row,
    stake: float,
    decimal_odds: float,
    settled_pl: float,
    cashout_proceeds: float,
    settled_ts: str,
    note: str,
    token_id: Optional[str] = None,
    selection: Optional[str] = None,
    platform: str = "polymarket",
) -> int:
    """Insert one ``'cashed'`` row (a sold slice / untracked excess).

    Descriptive fields come from ``template_row`` (a matched open row or a
    caller-supplied dict); explicit overrides win. This is NOT an open row, so
    it is not a phantom — it records realised cash-out money.
    """
    def _g(key, default=None):
        if template_row is None:
            return default
        try:
            return template_row[key]
        except (KeyError, IndexError, TypeError):
            try:
                return template_row.get(key, default)  # dict
            except AttributeError:
                return default

    import datetime as _dt

    ts_utc = _g("ts_utc") or _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        """
        INSERT INTO bets
            (ts_utc, match_id, match_desc, market, selection, platform,
             decimal_odds, stake, model_prob, status, settled_pl,
             cashout_proceeds, settled_ts, notes, account, source, token_id)
        VALUES (?,?,?,?,?,?,?,?,?, 'cashed', ?,?,?,?,?,?,?)
        """,
        (
            ts_utc,
            _g("match_id", "PM_CASHOUT"),
            _g("match_desc", ""),
            _g("market", "polymarket"),
            selection if selection is not None else _g("selection", ""),
            _g("platform", platform),
            float(decimal_odds),
            float(stake),
            _g("model_prob"),
            float(settled_pl),
            float(cashout_proceeds),
            settled_ts,
            note,
            str(_g("account", "1") or "1"),
            str(_g("source", "model") or "model"),
            str(token_id) if token_id else _g("token_id"),
        ),
    )
    return int(cur.lastrowid)


def set_closing_odds(
    bet_id: int,
    closing_odds: float,
    db_path: str = _DEFAULT_DB,
) -> None:
    """Record closing odds for a bet and compute CLV.

    CLV formula (return-ratio form)
    --------------------------------
    CLV% = (decimal_odds_taken / closing_odds) - 1

    A positive CLV means the bettor obtained better odds than the closing
    line — they "beat the close".  A negative CLV means the line moved
    against them after placement.

    Closing-odds basis (keep consistent across the column!)
    -------------------------------------------------------
    ``closing_odds`` should be the **de-vigged fair consensus** price at the
    last capture before kick-off — the same basis the automatic capture path
    (:mod:`wca.closecapture`) and the tracking feed
    (``scripts/wca_tracking_data.py``) use.  A raw single-book quote (which
    still carries the bookmaker's vig) is systematically shorter than the
    fair price and will *overstate* CLV relative to auto-captured rows, so
    averaging the two bases in one column mixes definitions.  Prefer letting
    the daemon stamp the fair close automatically; pass an explicit price
    here only when you have a fair (vig-removed) number.

    Parameters
    ----------
    bet_id:
        The ``id`` of the bet row to update.
    closing_odds:
        Last-traded / closing decimal odds for this selection.
    db_path:
        Path to the SQLite database file.

    Raises
    ------
    KeyError
        If no bet with ``bet_id`` exists.
    ValueError
        If ``closing_odds`` is not strictly greater than 1.0.
    """
    c_odds = float(closing_odds)
    if c_odds <= 1.0:
        raise ValueError("closing_odds must be > 1.0, got %r" % c_odds)

    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT decimal_odds FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError("no bet with id=%d" % bet_id)

        taken_odds = float(row["decimal_odds"])
        # CLV% = (odds_taken / closing_odds) - 1
        # Positive = beat the close; negative = line moved against us.
        clv = (taken_odds / c_odds) - 1.0

        conn.execute(
            "UPDATE bets SET closing_odds = ?, clv = ? WHERE id = ?",
            (c_odds, clv, bet_id),
        )


# ---------------------------------------------------------------------------
# Bankroll events.
# ---------------------------------------------------------------------------


def add_bankroll_event(
    ts_utc: str,
    amount: float,
    reason: Optional[str] = None,
    db_path: str = _DEFAULT_DB,
) -> int:
    """Record a deposit (positive amount) or withdrawal (negative amount).

    Parameters
    ----------
    ts_utc:
        ISO-8601 UTC timestamp.
    amount:
        Currency amount; positive for deposits, negative for withdrawals.
    reason:
        Optional free-text description.
    db_path:
        Path to the SQLite database file.

    Returns
    -------
    int
        Auto-assigned row ``id``.
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO bankroll_events (ts_utc, amount, reason) VALUES (?, ?, ?)",
            (ts_utc, float(amount), reason),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Read helpers used by reports.py.
# ---------------------------------------------------------------------------


def get_bet(bet_id: int, db_path: str = _DEFAULT_DB) -> Optional[sqlite3.Row]:
    """Return the row for a single bet, or ``None`` if not found."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()


def all_bets(db_path: str = _DEFAULT_DB) -> list:
    """Return all bet rows as a list of :class:`sqlite3.Row` objects."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bets ORDER BY id"
        ).fetchall()


def all_bankroll_events(db_path: str = _DEFAULT_DB) -> list:
    """Return all bankroll-event rows ordered by id."""
    init_db(db_path)
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM bankroll_events ORDER BY id"
        ).fetchall()
