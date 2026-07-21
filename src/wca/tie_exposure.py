# src/wca/tie_exposure.py — cross-feed dedup for economically-identical bets.
"""Detects when two recommended legs express the SAME real-money bet.

Two "advance"-type legs can be economically identical even though they come
from different builders pricing different Polymarket instruments. Example:
"England to advance" (``wca_event_markets.py``'s per-tie ET+pens market) and
"England reach_SF" (``wca_betrecs.py``'s advancement-futures market) both pay
off exactly when England wins its QF vs Norway — same real-world event, two
different tradable contracts. Sizing both independently double-counts one
exposure (CLAUDE.md "Whole-book: size ALL bets together; worst case respects
the hard cash floor").

This module is intentionally separate from ``wca.selection`` (the
human-approved-change file for bucket/EV ordering): dedup decides which of
two ALREADY-selected legs for the SAME bet keeps its stake, it does not
reorder or re-rank anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from wca.data.teamnames import canonical

REASON_DUP_TIE_EXPOSURE = "dup_tie_exposure"


def tie_key(team_a: str, team_b: str) -> frozenset:
    """Canonical, order-independent identity for a single knockout tie."""
    return frozenset({canonical(team_a), canonical(team_b)})


def same_bet_key(team: Optional[str], stage: Optional[str]) -> Optional[Tuple[str, str]]:
    """Identity for "``team`` reaches ``stage``" — the actual bet being made.

    Returns ``None`` when either side is unknown. A missing stage must never
    be guessed: a false-negative here just skips a dedup opportunity; a
    false-positive would wrongly zero a legitimate, independent leg.
    """
    if not team or not stage:
        return None
    return (canonical(team), stage)


def find_cross_feed_duplicates(
    event_market_legs: List[Dict[str, Any]],
    advancement_futures_legs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pair up legs from the two feeds that stake the SAME (team, stage).

    ``event_market_legs`` items need: ``team``, ``tie_stage``, ``stake_usd``.
    ``advancement_futures_legs`` items need: ``team``, ``stage``, ``stake``.

    Returns one dict per (team, stage) with a CASH (stake > 0) candidate in
    BOTH feeds: ``{"key": (team, stage), "event_market": <leg>,
    "advancement_futures": <leg>}``.
    """
    em_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for leg in event_market_legs:
        if float(leg.get("stake_usd") or 0.0) <= 0.0:
            continue
        # Side guard (2026-07-14): a lay/fade advance leg pays when the team
        # does NOT advance — the OPPOSITE exposure to a backed futures rung,
        # not the same bet. Never pair it. Missing side = legacy back leg.
        if str(leg.get("side") or "back").strip().lower() == "lay":
            continue
        key = same_bet_key(leg.get("team"), leg.get("tie_stage"))
        if key is None:
            continue
        em_by_key[key] = leg

    dupes: List[Dict[str, Any]] = []
    for leg in advancement_futures_legs:
        if float(leg.get("stake") or 0.0) <= 0.0:
            continue
        # Side guard (2026-07-14): advancement_futures rows are SIDED now
        # (side-aware position bucketing). A NO-side rung pays when the team
        # does NOT reach the stage — the OPPOSITE of an event-market "team to
        # advance" back leg on the same (team, stage). Pairing them would
        # zero one side of what is actually a partial hedge. Missing side =
        # legacy YES-only feed.
        if str(leg.get("side") or "YES").strip().upper() == "NO":
            continue
        key = same_bet_key(leg.get("team"), leg.get("stage"))
        if key is None:
            continue
        em_leg = em_by_key.get(key)
        if em_leg is not None:
            dupes.append({"key": key, "event_market": em_leg,
                          "advancement_futures": leg})
    return dupes


def resolve_duplicate(dupe: Dict[str, Any]) -> str:
    """Return which side to ZERO: ``"event_market"`` or ``"advancement_futures"``.

    Keeps the leg with the better net edge. Ties broken toward the
    event-market leg: it settles on THIS tie's ET+pens outcome only, while
    the futures leg is a multi-hop instrument that also prices stages the
    tie hasn't reached yet — a noisier read of the same near-term event.
    """
    em_ev = float(dupe["event_market"].get("ev") or 0.0)
    af_ev = float(dupe["advancement_futures"].get("ev_net") or 0.0)
    return "advancement_futures" if af_ev <= em_ev else "event_market"
