"""Odds normalisation: decimal -> implied -> vig-adjusted, and a mapper from the
existing ``odds_snapshots`` rows into the generalised :class:`MarketSnapshot`.

Vig removal reuses :func:`wca.markets.devig.shin` over the COMPLETE market (all
selections together) — so ``implied_devig`` is only filled when the market is
complete; otherwise it's left ``None`` (we never fabricate a fair price from a
partial book).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from wca.markets import devig
from wca.intel.registry import venue_for
from wca.intel.store import MarketSnapshot


def implied_from_decimal(odds: float) -> Optional[float]:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return (1.0 / o) if o > 1.0 else None


def _mins_to_ko(ts_utc: Optional[str], ko_utc: Optional[str]) -> Optional[float]:
    from datetime import datetime, timezone
    def p(t):
        if not t:
            return None
        try:
            d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    a, b = p(ts_utc), p(ko_utc)
    return round((b - a).total_seconds() / 60.0, 1) if (a and b) else None


def normalise_market(*, source: str, venue: str, market_type: str,
                     selection_odds: Dict[str, float], ts_utc: str,
                     fixture_id: Optional[str] = None, ko_utc: Optional[str] = None,
                     line: Optional[float] = None, liquidity: Optional[Dict[str, float]] = None,
                     method: str = "shin") -> List[MarketSnapshot]:
    """Normalise one venue's quote for one complete market into MarketSnapshots.

    ``selection_odds`` maps selection -> decimal odds. ``implied_devig`` is filled
    (Shin over all selections) only when every selection has valid odds and there
    are >=2; otherwise it's None (no fabrication from a partial book).
    """
    sels = [s for s in selection_odds if implied_from_decimal(selection_odds[s]) is not None]
    devig_probs: Dict[str, float] = {}
    if len(sels) >= 2:
        try:
            probs = devig.devig([float(selection_odds[s]) for s in sels], method=method)
            devig_probs = {s: float(p) for s, p in zip(sels, probs)}
        except Exception:
            devig_probs = {}
    v = venue_for(venue)
    mtk = _mins_to_ko(ts_utc, ko_utc)
    out: List[MarketSnapshot] = []
    for s, odds in selection_odds.items():
        ir = implied_from_decimal(odds)
        if ir is None:
            continue
        liq = (liquidity or {}).get(s)
        out.append(MarketSnapshot(
            ts_utc=ts_utc, source=source, venue=(v.canon if v else venue),
            venue_kind=(v.kind if v else None), market_type=market_type, selection=s,
            line=line, decimal_odds=float(odds), implied_raw=ir,
            implied_devig=devig_probs.get(s), liquidity=liq,
            fixture_id=fixture_id, ko_utc=ko_utc, mins_to_ko=mtk,
        ))
    return out


#: OddsAPI market key -> our canonical market_type.
_ODDSAPI_MARKET = {"h2h": "moneyline", "totals": "ou", "btts": "btts", "h2h_lay": "moneyline_lay"}


def oddsapi_market_type(market: str) -> str:
    return _ODDSAPI_MARKET.get(market, market)


def _leg(outcome: str, home: str, away: str) -> str:
    o = (outcome or "").strip().lower()
    if o in ("draw", "tie"):
        return "Draw"
    if o == (home or "").strip().lower():
        return "Home"
    if o == (away or "").strip().lower():
        return "Away"
    return outcome  # totals (Over/Under), player names, etc. pass through


def from_oddsapi_rows(rows: Sequence[dict]) -> List[MarketSnapshot]:
    """Map existing ``odds_snapshots`` rows into MarketSnapshots, devigging each
    (match, market, venue, ts) group. Each row: ts_utc, match_id, market, raw{...}.
    """
    groups: Dict[tuple, Dict[str, float]] = {}
    meta: Dict[tuple, dict] = {}
    for r in rows:
        raw = r.get("raw") or {}
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        venue = raw.get("bookmaker_key") or r.get("source")
        home, away = raw.get("home_team", ""), raw.get("away_team", "")
        mt = oddsapi_market_type(r.get("market", ""))
        sel = _leg(raw.get("outcome_name", r.get("selection", "")), home, away)
        line = raw.get("outcome_point")
        k = (r.get("match_id"), mt, line, venue, r.get("ts_utc"))
        groups.setdefault(k, {})[sel] = r.get("decimal_odds")
        meta[k] = {"ko": raw.get("commence_time"), "src": r.get("source", "theoddsapi")}
    out: List[MarketSnapshot] = []
    for (match_id, mt, line, venue, ts), sel_odds in groups.items():
        out.extend(normalise_market(
            source=meta[(match_id, mt, line, venue, ts)]["src"], venue=venue, market_type=mt,
            selection_odds=sel_odds, ts_utc=ts, fixture_id=match_id, line=line,
            ko_utc=meta[(match_id, mt, line, venue, ts)]["ko"],
        ))
    return out
