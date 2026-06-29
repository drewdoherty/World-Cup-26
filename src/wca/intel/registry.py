"""Venue registry for the Market Intelligence subsystem.

Single source of truth for every supported venue: its canonical name, kind
(exchange / sportsbook / prediction market), commission model, whether we get
real liquidity, the markets it supports, and a STABLE display colour reused
across the whole dashboard so cross-venue divergence is instantly visible.

Adding a new sportsbook = one row here (and, if it rides the OddsAPI relay,
nothing else). Canonicalisation defers to :func:`wca.venues.canon_book`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from wca import venues as _venues

#: Canonical market types this subsystem can store (collection is gated by what
#: each source actually offers — see docs).
MARKET_TYPES: Tuple[str, ...] = (
    "moneyline", "draw", "ah", "ou", "btts", "cs", "corners", "cards",
    "shots", "sot", "fgs", "anytime", "team_total", "player_prop",
)

EXCHANGE = "exchange"
SPORTSBOOK = "sportsbook"
PREDICTION_MARKET = "prediction_market"


@dataclass(frozen=True)
class Venue:
    canon: str                 # canonical display name (matches venues.canon_book)
    kind: str                  # EXCHANGE | SPORTSBOOK | PREDICTION_MARKET
    commission: float          # taker/commission on net winnings (exchanges/PM); 0 for sportsbooks
    has_liquidity: bool        # True only where a direct API exposes order-book depth
    colour: str                # stable hex for the dashboard
    source: str                # collector key: 'theoddsapi' | 'polymarket' | 'betfair' | 'smarkets'

    @property
    def is_exchange(self) -> bool:
        return self.kind == EXCHANGE


#: The registry. Commissions mirror wca.arbfx / wca.arb (Betfair 6%, PM ~3%).
#: `has_liquidity` is False wherever odds arrive via the OddsAPI relay (no depth);
#: it flips True once a direct exchange API is wired (Phase 1).
#: Keyed by ``venues.canon_book`` output (NOT raw spelling), so lookups via
#: :func:`venue_for` resolve every alias to one row. Note the canon quirks:
#: ``"Polymarket"`` canonicalises to lowercase ``"polymarket"``; the exchange's
#: canon is ``"Betfair"`` (from OddsAPI ``betfair_ex_uk``) while the plain string
#: ``"Betfair"`` canonicalises to ``"Betfair Sportsbook"``.
VENUES: Dict[str, Venue] = {
    "polymarket":          Venue("polymarket", PREDICTION_MARKET, 0.03, True,  "#7A3FB0", "polymarket"),
    "Betfair":             Venue("Betfair", EXCHANGE, 0.06, False, "#FFB80C", "theoddsapi"),  # exchange (relay for now)
    "Betfair Sportsbook":  Venue("Betfair Sportsbook", SPORTSBOOK, 0.0, False, "#2563B0", "theoddsapi"),
    "Smarkets":            Venue("Smarkets", EXCHANGE, 0.02, False, "#0E7C7B", "theoddsapi"),
    "bet365":              Venue("bet365", SPORTSBOOK, 0.0, False, "#1A7A4C", "theoddsapi"),
    "Paddy Power":         Venue("Paddy Power", SPORTSBOOK, 0.0, False, "#0B7A3B", "theoddsapi"),
    "Betway":              Venue("Betway", SPORTSBOOK, 0.0, False, "#00A826", "theoddsapi"),
}

#: Fallback colour for venues seen in data but not yet registered.
_DEFAULT_COLOUR = "#9A93B0"


def venue_for(name: str) -> Optional[Venue]:
    """Look up a registered Venue by any spelling (via ``canon_book``)."""
    canon = _venues.canon_book(name)
    return VENUES.get(canon)


def venue_colour(name: str) -> str:
    """Stable dashboard colour for a venue (canonicalised); fallback if unknown."""
    v = venue_for(name)
    return v.colour if v else _DEFAULT_COLOUR


def commission_for(name: str) -> float:
    """Commission rate for a venue (0 for sportsbooks); 0 if unknown."""
    v = venue_for(name)
    return v.commission if v else 0.0
