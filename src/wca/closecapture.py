"""Automatic closing-line capture for the bet ledger.

At kickoff the last pre-kickoff ``odds_snapshots`` pull *is* the market
close.  This module turns that pull into a de-vigged consensus 1X2 (the same
:func:`wca.tracking.devig_consensus` the tracking page uses) and stamps
``closing_odds`` (the fair closing price for the bet's selection) and ``clv``
(``decimal_odds / closing_odds - 1``, the ledger-wide convention shared with
:func:`wca.ledger.store.set_closing_odds`) onto every open 1X2-style bet on
that fixture.  Kickoff times come from the ``commence_time`` field the
snapshot daemon stores in each row's raw JSON — no other fixture source is
needed.

Only match-winner-style markets are stamped automatically: a 1X2 close says
nothing about an exact-score, totals or multi-leg price.  Everything else
keeps the manual path (``scripts/wca_settle.py``).

Design notes
------------
* **Deterministic.**  :func:`capture_closes` never reads the wall clock — the
  caller passes ``now_utc`` — and touches nothing but the connection it is
  handed.  The daemon-facing :func:`capture_closes_db` wrapper supplies the
  clock and the connection.
* **Idempotent.**  A bet is stamped once: candidates are open bets with a
  NULL ``closing_odds``, so re-runs are no-ops and a manually-stamped close
  is never overwritten.
* **Tolerant.**  Unsplittable fixtures, missing snapshots and unmapped
  selections are skipped — a daemon poll must never crash on a weird ledger
  row.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from wca import tracking
from wca.data import teamnames

# Bet ``market`` values (casefolded) that price a match-winner outcome and can
# therefore be scored against a 1X2 close.  Spans the bookmaker, exchange and
# Polymarket spellings present in the ledger.
_X12_MARKETS = frozenset(
    {
        "h2h",
        "full-time result",
        "full time result",
        "match odds",
        "match winner",
        "match",
        "pm_moneyline",
    }
)

_LEGS = ("home", "draw", "away")

# A note substring that opts a bet out of automatic CLV capture entirely
# (matched-betting hedges on third-party accounts carry it).
_EXCLUDE_NOTE = "exclude from clv"


def is_1x2_market(market: Any) -> bool:
    """True when a bet's ``market`` label prices a match-winner outcome."""
    return isinstance(market, str) and market.strip().casefold() in _X12_MARKETS


def _bare_ts(ts: Any) -> str:
    """Strip the UTC offset/Z from an ISO timestamp for bare string compares.

    Placement times in ``bets.ts_utc`` carry no offset
    (``2026-06-11T10:12:38``) while ``commence_time`` does
    (``2026-06-13T01:00:00+00:00``); both are UTC by repo convention, so the
    sortable comparison is the offset-stripped prefix.  Lexicographic ordering
    of these prefixes matches chronological ordering.
    """
    if not isinstance(ts, str):
        return ""
    text = ts.strip().replace("Z", "")
    # Drop a trailing numeric offset like "+00:00" / "-03:00".
    if len(text) >= 6 and text[-3] == ":" and text[-6] in "+-":
        text = text[:-6]
    return text


# A "KO <ISO>" kickoff hint embedded in a bet's notes, e.g.
# "Account 2 dead qualifier ...; KO 2026-06-12T19:00Z".  Offer/qualifier bets
# carry no parseable "Home vs Away" desc, so this hint plus the selection team
# is how they get matched to a fixture (see :func:`_resolve_by_ko_hint`).
_KO_HINT_RE = re.compile(r"\bKO\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)")

# How far a snapshot fixture's kickoff may sit from the noted KO hint and still
# be considered the same match (the API's commence_time drifts a minute or two;
# a team plays at most once per ~3 days, so a generous window stays unique).
_KO_WINDOW_MINUTES = 180


def ko_hint(notes: Any) -> Optional[str]:
    """Bare (offset-stripped) KO timestamp parsed from a bet's notes, or None."""
    if not isinstance(notes, str):
        return None
    match = _KO_HINT_RE.search(notes)
    return _bare_ts(match.group(1)) if match else None


def _parse_dt(bare: str) -> Optional[datetime]:
    """Parse a bare ISO string (no offset) to a naive datetime, or None."""
    if not bare:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(bare, fmt)
        except ValueError:
            continue
    return None


def _within_window(a: str, b: str, minutes: float) -> bool:
    """True when bare timestamps *a* and *b* are within *minutes* of each other."""
    da, db = _parse_dt(a), _parse_dt(b)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) <= minutes * 60.0


def _resolve_by_ko_hint(
    selection: Any,
    ko_bare: Optional[str],
    index: Dict[str, Dict[str, str]],
) -> Optional[str]:
    """Match an unsplittable offer bet to a fixture via KO hint + selection team.

    Returns the ``match_id`` only when *exactly one* snapshot fixture both
    includes the selection's (canonical) team and kicks off within
    :data:`_KO_WINDOW_MINUTES` of the noted KO — otherwise ``None`` (ambiguous
    → safe no-stamp).  The selection-uniqueness alone is unsafe (a team plays
    several fixtures), so the kickoff window is what makes the match unique.
    """
    if not ko_bare:
        return None
    sel_c = _canon(selection)
    if not sel_c:
        return None
    matches = [
        mid
        for mid, info in index.items()
        if sel_c in (_canon(info["home"]), _canon(info["away"]))
        and _within_window(_bare_ts(info["kickoff"]), ko_bare, _KO_WINDOW_MINUTES)
    ]
    return matches[0] if len(matches) == 1 else None


def _canon(name: Any) -> str:
    """Alias-resolved, casefolded team name ('' for non-strings)."""
    if not isinstance(name, str):
        return ""
    return (teamnames.canonical(name) or "").strip().casefold()


def selection_leg(
    selection: Any, home_raw: str, away_raw: str
) -> Optional[Tuple[str, bool]]:
    """Map a bet selection to ``(leg, is_no)`` for a fixture, else ``None``.

    Handles the spellings the ledger actually contains: a plain team name
    (``"Paraguay"``), a draw (``"Draw"`` / ``"The Draw"``) and Polymarket
    moneyline shares with a trailing side token (``"Paraguay Yes"``,
    ``"USA No"``).  ``is_no`` flags the complement: a No share wins when the
    leg does *not* happen, so its fair price comes from ``1 - p(leg)``.
    """
    if not isinstance(selection, str):
        return None
    sel = selection.strip()
    if not sel:
        return None
    is_no = False
    lowered = sel.casefold()
    if lowered.endswith(" yes"):
        sel = sel[: -len(" yes")].strip()
    elif lowered.endswith(" no"):
        sel = sel[: -len(" no")].strip()
        is_no = True
    lowered = sel.casefold()
    if lowered in ("draw", "the draw"):
        return ("draw", is_no)
    sel_c = _canon(sel)
    if sel_c and sel_c == _canon(home_raw):
        return ("home", is_no)
    if sel_c and sel_c == _canon(away_raw):
        return ("away", is_no)
    return None


def fair_closing_odds(
    triple: Dict[str, float], leg: str, is_no: bool
) -> Optional[float]:
    """Fair decimal close for a leg (or its No complement) of a 1X2 triple."""
    try:
        prob = float(triple[leg])
    except (KeyError, TypeError, ValueError):
        return None
    if is_no:
        prob = 1.0 - prob
    if not 0.0 < prob < 1.0:
        return None
    return 1.0 / prob


def match_index(con: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    """Map ``odds_snapshots`` h2h ``match_id`` -> latest fixture metadata.

    Returns ``{match_id: {"home", "away", "kickoff"}}`` parsed from each
    event's most recent raw row, so a rescheduled ``commence_time`` is picked
    up.  Events whose raw JSON lacks any of the three fields are omitted.
    """
    rows = con.execute(
        "SELECT match_id, raw, MAX(ts_utc) FROM odds_snapshots "
        "WHERE market='h2h' AND raw IS NOT NULL GROUP BY match_id"
    ).fetchall()
    index: Dict[str, Dict[str, str]] = {}
    for match_id, raw, _ts in rows:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        home = payload.get("home_team")
        away = payload.get("away_team")
        kickoff = payload.get("commence_time")
        if home and away and kickoff:
            index[str(match_id)] = {
                "home": str(home),
                "away": str(away),
                "kickoff": str(kickoff),
            }
    return index


def consensus_close(
    con: sqlite3.Connection,
    match_id: str,
    home_raw: str,
    away_raw: str,
    kickoff_utc: str,
) -> Optional[Dict[str, Any]]:
    """De-vigged consensus 1X2 at the last h2h capture before kickoff.

    Returns ``{"triple": {home, draw, away}, "ts": str, "books": int}`` or
    ``None`` when no pre-kickoff snapshot (or no complete book triple)
    exists.  Same shape and arithmetic as the tracking feed's market close.
    """
    cutoff = (kickoff_utc or "").replace("Z", "+00:00")
    if not cutoff:
        return None
    last_ts_row = con.execute(
        "SELECT MAX(ts_utc) FROM odds_snapshots "
        "WHERE match_id=? AND market='h2h' AND ts_utc<=?",
        (match_id, cutoff),
    ).fetchone()
    last_ts = last_ts_row[0] if last_ts_row else None
    if not last_ts:
        return None

    rows = con.execute(
        "SELECT raw FROM odds_snapshots "
        "WHERE match_id=? AND market='h2h' AND ts_utc=?",
        (match_id, last_ts),
    ).fetchall()
    books: Dict[str, Dict[str, float]] = {}
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        book = payload.get("bookmaker_key") or payload.get("bookmaker_title")
        outcome = payload.get("outcome_name")
        dec = payload.get("decimal_odds")
        if not book or outcome is None or dec is None:
            continue
        leg = None
        if str(outcome).strip().casefold() == "draw":
            leg = "draw"
        elif _canon(outcome) == _canon(home_raw):
            leg = "home"
        elif _canon(outcome) == _canon(away_raw):
            leg = "away"
        if leg is None:
            continue
        try:
            books.setdefault(str(book), {})[leg] = float(dec)
        except (TypeError, ValueError):
            continue

    triple = tracking.devig_consensus(
        [{"book": book, **prices} for book, prices in books.items()]
    )
    if triple is None:
        return None
    return {"triple": triple, "ts": last_ts, "books": len(books)}


def _pick_event(
    candidates: List[str],
    index: Dict[str, Dict[str, str]],
    placement: Any,
) -> Optional[str]:
    """Choose the snapshot event a bet refers to among same-pair candidates.

    With a single candidate, return it.  With several — a repeated pairing,
    where a group meeting and a later knockout/qualifier rematch both live in
    the append-only ``odds_snapshots`` under different ``match_id``s — return
    the event whose kickoff is the *earliest one at or after the bet's
    placement time*, since a bet always precedes its own fixture's kickoff.
    Returns ``None`` when the pairing is genuinely ambiguous (no candidate
    kicks off at/after placement) — a safe no-stamp the manual path covers.

    This guards against the last-writer-wins collision a flat ``pair ->
    match_id`` map would suffer: without it, an arbitrary one of the two
    events (ordered by opaque hex id) would win and a future-fixture bet
    could be stamped with a stale earlier close, locked in permanently by the
    ``closing_odds IS NULL`` idempotency guard.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    placement_bare = _bare_ts(placement)
    best: Optional[str] = None
    best_k: Optional[str] = None
    for mid in candidates:
        k = _bare_ts(index[mid]["kickoff"])
        if not k:
            continue
        if placement_bare and k < placement_bare:
            continue  # an earlier meeting of the same pair — not this bet's
        if best_k is None or k < best_k:
            best, best_k = mid, k
    return best


# Skip reasons that signal a real coverage gap worth surfacing to the
# operator (vs. transient/expected states like "not kicked off yet").
ACTIONABLE_SKIPS = frozenset({"unsplittable", "ambiguous", "unmatched"})


def _build_team_index(
    con: sqlite3.Connection,
) -> Tuple[Dict[str, Dict[str, str]], Dict[frozenset, List[str]]]:
    """Return ``(match_index, by_teams)`` for fixture resolution.

    ``by_teams`` maps a canonical *unordered* team pair to every snapshot
    ``match_id`` for it — more than one when a fixture recurs (group meeting +
    knockout/qualifier rematch); :func:`_pick_event` disambiguates per bet.
    """
    index = match_index(con)
    by_teams: Dict[frozenset, List[str]] = {}
    for mid, info in index.items():
        key = frozenset((_canon(info["home"]), _canon(info["away"])))
        if len(key) == 2 and "" not in key:
            by_teams.setdefault(key, []).append(mid)
    return index, by_teams


def _resolve_fixture(
    match_desc: Any,
    selection: Any,
    notes: Any,
    placement: Any,
    index: Dict[str, Dict[str, str]],
    by_teams: Dict[frozenset, List[str]],
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a bet to a snapshot ``match_id``; return ``(match_id, reason)``.

    ``reason`` is ``None`` on success, else a skip reason.  Tries the parsed
    ``"Home vs Away"`` desc first (with :func:`_pick_event` rematch
    disambiguation); falls back to the notes' ``KO`` hint + selection team for
    unsplittable offer/qualifier descs (``"Canada (qualifier)"``).
    """
    pair = tracking.split_fixture(match_desc or "")
    if pair is not None:
        key = frozenset((_canon(pair[0]), _canon(pair[1])))
        cands = by_teams.get(key) or []
        mid = _pick_event(cands, index, placement)
        if mid is None:
            return None, ("ambiguous" if cands else "unmatched")
        return mid, None
    # Unsplittable desc — last resort: the KO-hint + selection-team matcher.
    mid = _resolve_by_ko_hint(selection, ko_hint(notes), index)
    if mid is None:
        return None, "unsplittable"
    return mid, None


def _compute_close(
    con: sqlite3.Connection,
    match_id: str,
    selection: Any,
    decimal_odds: Any,
    index: Dict[str, Dict[str, str]],
    closes: Dict[str, Optional[Dict[str, Any]]],
    now_bare: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fair close + CLV for one resolved bet; ``(record, None)`` or ``(None, reason)``.

    ``record`` = ``{"closing", "clv", "backed", "close_ts", "books"}``.
    """
    info = index[match_id]
    kickoff = info["kickoff"].replace("Z", "+00:00")
    if _bare_ts(kickoff) > now_bare:
        return None, "future"  # not kicked off yet — the close isn't in
    if match_id not in closes:
        closes[match_id] = consensus_close(
            con, match_id, info["home"], info["away"], kickoff
        )
    close = closes[match_id]
    if close is None:
        return None, "no_close"
    mapped = selection_leg(selection, info["home"], info["away"])
    if mapped is None:
        return None, "no_leg"
    leg, is_no = mapped
    closing = fair_closing_odds(close["triple"], leg, is_no)
    if closing is None or closing <= 1.0:
        return None, "no_close"
    try:
        backed = float(decimal_odds)
    except (TypeError, ValueError):
        return None, "bad_odds"
    if backed <= 0:
        return None, "bad_odds"
    return (
        {
            "closing": closing,
            "clv": backed / closing - 1.0,
            "backed": backed,
            "close_ts": close["ts"],
            "books": close["books"],
        },
        None,
    )


def _skipper(skipped_out: Optional[List[Dict[str, Any]]]):
    def _skip(bet_id: Any, match_desc: Any, selection: Any, reason: str) -> None:
        if skipped_out is not None:
            skipped_out.append(
                {
                    "bet_id": bet_id,
                    "match": match_desc,
                    "selection": selection,
                    "reason": reason,
                }
            )
    return _skip


def capture_closes(
    con: sqlite3.Connection,
    now_utc: str,
    dry_run: bool = False,
    skipped_out: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Stamp ``closing_odds`` + ``clv`` onto open 1X2 bets that have kicked off.

    Parameters
    ----------
    con:
        Ledger connection (``bets`` + ``odds_snapshots`` tables).
    now_utc:
        ISO-8601 UTC timestamp; only fixtures with ``commence_time <=
        now_utc`` are considered closed (caller supplies the clock).
    dry_run:
        When true, compute and report but write nothing.
    skipped_out:
        Optional list; when provided, one ``{"bet_id", "match", "selection",
        "reason"}`` record is appended for every open 1X2 bet that was *not*
        stamped, so callers can surface coverage gaps (an unsplittable
        ``match_desc`` with no KO hint, an ambiguous rematch pairing, a bet
        whose fixture has no odds snapshot) instead of letting them rot
        silently.  Reasons in :data:`ACTIONABLE_SKIPS` flag real gaps; the
        rest are expected transient states (``future`` / ``no_close`` /
        ``no_leg`` / ``excluded`` / ``bad_odds``).

    Returns one record per stamped bet: ``{"bet_id", "match", "selection",
    "decimal_odds", "closing_odds", "clv", "close_ts", "books"}``.
    """
    now_bare = _bare_ts(now_utc)
    if not now_bare:
        return []
    _skip = _skipper(skipped_out)

    bets = con.execute(
        "SELECT id, ts_utc, match_desc, market, selection, decimal_odds, notes "
        "FROM bets WHERE status='open' AND closing_odds IS NULL"
    ).fetchall()
    candidates = [b for b in bets if is_1x2_market(b[3])]
    if not candidates:
        return []

    index, by_teams = _build_team_index(con)
    closes: Dict[str, Optional[Dict[str, Any]]] = {}
    stamped: List[Dict[str, Any]] = []
    for bet_id, ts_utc, match_desc, _market, selection, decimal_odds, notes in candidates:
        if isinstance(notes, str) and _EXCLUDE_NOTE in notes.casefold():
            _skip(bet_id, match_desc, selection, "excluded")
            continue
        mid, reason = _resolve_fixture(
            match_desc, selection, notes, ts_utc, index, by_teams
        )
        if mid is None:
            _skip(bet_id, match_desc, selection, reason)
            continue
        record, reason = _compute_close(
            con, mid, selection, decimal_odds, index, closes, now_bare
        )
        if record is None:
            _skip(bet_id, match_desc, selection, reason)
            continue
        if not dry_run:
            con.execute(
                "UPDATE bets SET closing_odds=?, clv=? "
                "WHERE id=? AND status='open' AND closing_odds IS NULL",
                (record["closing"], record["clv"], bet_id),
            )
        stamped.append(
            {
                "bet_id": bet_id,
                "match": match_desc,
                "selection": selection,
                "decimal_odds": record["backed"],
                "closing_odds": record["closing"],
                "clv": record["clv"],
                "close_ts": record["close_ts"],
                "books": record["books"],
            }
        )
    if stamped and not dry_run:
        con.commit()
    return stamped


def rebackfill_fair_closes(
    con: sqlite3.Connection,
    now_utc: str,
    dry_run: bool = False,
    skipped_out: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Recompute the fair (de-vigged) close for EVERY kicked-off 1X2 bet.

    Unlike :func:`capture_closes` this spans *all* statuses and *overwrites*
    any existing ``closing_odds`` — its purpose is to normalise the whole
    ``closing_odds`` column onto one basis (the de-vigged consensus), e.g. to
    convert legacy rows stamped with a raw single-book quote.  Idempotent: a
    row already on the fair basis recomputes to the same value.  ``settled_pl``
    is never touched.

    Returns one record per bet whose close was (re)computed, each carrying the
    previous values so the caller can show a before/after diff::

        {"bet_id", "match", "selection", "status", "decimal_odds",
         "old_closing", "new_closing", "old_clv", "new_clv", "changed",
         "close_ts", "books"}
    """
    now_bare = _bare_ts(now_utc)
    if not now_bare:
        return []
    _skip = _skipper(skipped_out)

    bets = con.execute(
        "SELECT id, ts_utc, match_desc, market, selection, decimal_odds, "
        "notes, status, closing_odds, clv FROM bets"
    ).fetchall()
    candidates = [b for b in bets if is_1x2_market(b[3])]
    if not candidates:
        return []

    index, by_teams = _build_team_index(con)
    closes: Dict[str, Optional[Dict[str, Any]]] = {}
    out: List[Dict[str, Any]] = []
    for (bet_id, ts_utc, match_desc, _market, selection, decimal_odds,
         notes, status, old_close, old_clv) in candidates:
        if isinstance(notes, str) and _EXCLUDE_NOTE in notes.casefold():
            _skip(bet_id, match_desc, selection, "excluded")
            continue
        mid, reason = _resolve_fixture(
            match_desc, selection, notes, ts_utc, index, by_teams
        )
        if mid is None:
            _skip(bet_id, match_desc, selection, reason)
            continue
        record, reason = _compute_close(
            con, mid, selection, decimal_odds, index, closes, now_bare
        )
        if record is None:
            _skip(bet_id, match_desc, selection, reason)
            continue
        new_close, new_clv = record["closing"], record["clv"]
        changed = (
            old_close is None
            or abs(float(old_close) - new_close) > 1e-9
        )
        if not dry_run:
            con.execute(
                "UPDATE bets SET closing_odds=?, clv=? WHERE id=?",
                (new_close, new_clv, bet_id),
            )
        out.append(
            {
                "bet_id": bet_id,
                "match": match_desc,
                "selection": selection,
                "status": status,
                "decimal_odds": record["backed"],
                "old_closing": (None if old_close is None else float(old_close)),
                "new_closing": new_close,
                "old_clv": (None if old_clv is None else float(old_clv)),
                "new_clv": new_clv,
                "changed": changed,
                "close_ts": record["close_ts"],
                "books": record["books"],
            }
        )
    if out and not dry_run:
        con.commit()
    return out


def capture_closes_db(
    db_path: str,
    now_utc: Optional[str] = None,
    dry_run: bool = False,
    skipped_out: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Open *db_path* and run :func:`capture_closes` (daemon convenience).

    Defaults ``now_utc`` to the current UTC time.  Missing tables (a fresh
    DB) yield an empty result rather than an error.  ``skipped_out`` is
    forwarded to :func:`capture_closes`.
    """
    if now_utc is None:
        from datetime import timezone

        now_utc = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(db_path)
    try:
        return capture_closes(
            con, now_utc, dry_run=dry_run, skipped_out=skipped_out
        )
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def rebackfill_fair_closes_db(
    db_path: str,
    now_utc: Optional[str] = None,
    dry_run: bool = False,
    skipped_out: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Open *db_path* and run :func:`rebackfill_fair_closes`."""
    if now_utc is None:
        from datetime import timezone

        now_utc = datetime.now(timezone.utc).isoformat()
    con = sqlite3.connect(db_path)
    try:
        return rebackfill_fair_closes(
            con, now_utc, dry_run=dry_run, skipped_out=skipped_out
        )
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
