"""Assemble the ``market_intel.json`` dashboard feed from normalised snapshots.

Pure and network-free: takes a list of :class:`MarketSnapshot` (from any source
via :mod:`wca.intel.normalise`), groups by fixture × market, keeps the latest
quote per (selection, venue), runs :mod:`wca.intel.metrics`, and emits a JSON-
ready dict with a venue-colour legend and per-market staleness flags. The thin
CLI in ``scripts/wca_market_intel.py`` wires this to the live odds store and
writes the file atomically.

Staleness is honest: every quote here may be an OddsAPI relay snapshot (delayed,
no live liquidity), so each market carries its newest quote age and a ``stale``
flag — the dashboard and ``/arb`` must not treat an old price as executable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from wca.intel.metrics import build_market_metrics
from wca.intel.registry import VENUES, venue_colour
from wca.intel.store import MarketSnapshot

#: Quotes older than this (seconds) are flagged stale / not executable.
DEFAULT_STALE_S = 3600.0

#: Honest constraints surfaced in the feed so the dashboard never overclaims.
FEED_NOTES = (
    "Coverage is partial: only moneyline/totals/BTTS/AH-lay are captured via OddsAPI.",
    "Betfair/Smarkets prices arrive via the OddsAPI relay — no live liquidity or true close yet.",
    "Stale quotes are flagged; treat any flagged price as indicative, not executable.",
)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _latest_per_selection(snaps: Sequence[MarketSnapshot]) -> Dict[str, List[Dict[str, object]]]:
    """Newest snapshot per (selection, venue) for one market, in store-row shape."""
    by_sv: Dict[tuple, MarketSnapshot] = {}
    for s in snaps:
        k = (s.selection, s.venue)
        cur = by_sv.get(k)
        if cur is None or (s.ts_utc or "") >= (cur.ts_utc or ""):
            by_sv[k] = s
    out: Dict[str, List[Dict[str, object]]] = {}
    for (sel, _v), s in by_sv.items():
        out.setdefault(sel, []).append({
            "ts_utc": s.ts_utc, "venue": s.venue, "venue_kind": s.venue_kind,
            "selection": s.selection, "line": s.line, "decimal_odds": s.decimal_odds,
            "implied_raw": s.implied_raw, "implied_devig": s.implied_devig,
            "liquidity": s.liquidity,
        })
    return out


def venue_legend() -> List[Dict[str, object]]:
    """The colour/kind legend the dashboard renders once."""
    return [{"venue": v.canon, "kind": v.kind, "colour": v.colour,
             "commission": v.commission, "has_liquidity": v.has_liquidity}
            for v in VENUES.values()]


def build_feed(snaps: Sequence[MarketSnapshot], *, now_utc: str,
               fixture_meta: Optional[Dict[str, Dict[str, object]]] = None,
               models: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
               bankroll: Optional[float] = None, fraction: float = 0.25,
               cap: float = 0.05, stale_s: float = DEFAULT_STALE_S) -> Dict[str, object]:
    """Build the full market-intelligence feed dict.

    ``now_utc`` stamps the run and anchors staleness (scripts pass the wall clock;
    pure callers pass a fixed value). ``fixture_meta`` maps fixture_id ->
    {home, away, ko_utc}. ``models`` maps fixture_id -> market_type -> {selection:
    prob} to overlay model EV / Kelly where available.
    """
    fixture_meta = fixture_meta or {}
    models = models or {}
    now = _parse(now_utc)

    by_fixture: Dict[str, Dict[tuple, List[MarketSnapshot]]] = {}
    for s in snaps:
        by_fixture.setdefault(s.fixture_id, {}).setdefault((s.market_type, s.line), []).append(s)

    fixtures: List[Dict[str, object]] = []
    n_markets = 0
    for fid, markets in by_fixture.items():
        meta = fixture_meta.get(fid, {})
        m_out: List[Dict[str, object]] = []
        for (mt, line), group in sorted(markets.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
            latest = _latest_per_selection(group)
            model = (models.get(fid, {}) or {}).get(mt)
            sel_metrics = build_market_metrics(
                latest, model=model, bankroll=bankroll, fraction=fraction, cap=cap)
            latest_ts = max((q["ts_utc"] for qs in latest.values() for q in qs if q["ts_utc"]),
                            default=None)
            lt = _parse(latest_ts)
            age = (now - lt).total_seconds() if (now and lt) else None
            n_markets += 1
            m_out.append({
                "market_type": mt, "line": line,
                "n_venues": max((m.get("n_venues", 0) for m in sel_metrics), default=0),
                "latest_ts": latest_ts, "age_secs": age,
                "stale": (age is None or age > stale_s),
                "selections": sel_metrics,
            })
        fixtures.append({
            "fixture_id": fid, "home": meta.get("home"), "away": meta.get("away"),
            "ko_utc": meta.get("ko_utc"), "markets": m_out,
        })

    fixtures.sort(key=lambda f: (f.get("ko_utc") or "~", f.get("fixture_id") or ""))
    return {
        "generated_at": now_utc,
        "venues": venue_legend(),
        "fixtures": fixtures,
        "meta": {"n_fixtures": len(fixtures), "n_markets": n_markets,
                 "stale_threshold_s": stale_s, "notes": list(FEED_NOTES)},
    }
