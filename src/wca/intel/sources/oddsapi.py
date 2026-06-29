"""OddsAPI source adapter.

Wraps :func:`wca.intel.normalise.from_oddsapi_rows` (which already maps the
``odds_snapshots`` row shape -> MarketSnapshots and devigs each market). This
adapter adds only the *advertised capability* layer the planner/CLI need: which
canon venues ride the OddsAPI relay, which markets OddsAPI actually returns for
these books, and a credit-cost estimate so the budget governor can plan.

Honest scope: today OddsAPI sells only ``h2h`` (moneyline), ``h2h_lay``
(moneyline lay on the exchange), ``totals`` (OU) and ``btts`` for these books —
NOT corners/cards/shots. ``supported_markets`` reflects that; do not extend it
to markets the provider doesn't sell.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from wca.intel import normalise
from wca.intel.registry import VENUES, EXCHANGE, SPORTSBOOK
from wca.intel.store import MarketSnapshot

#: OddsAPI canonical market_types (post-normalise.oddsapi_market_type mapping).
_SUPPORTED: Tuple[str, ...] = ("moneyline", "moneyline_lay", "ou", "btts")


def _relay_venues() -> Tuple[str, ...]:
    """Canon names of exchange/sportsbook venues whose ``source`` is the OddsAPI
    relay (``theoddsapi``). Derived from the registry so a new relay book needs
    no edit here."""
    return tuple(
        v.canon for v in VENUES.values()
        if v.source == "theoddsapi" and v.kind in (EXCHANGE, SPORTSBOOK)
    )


class OddsApiSource:
    """Adapter for the existing TheOddsAPI relay capture."""

    name = "theoddsapi"
    supported_markets: Tuple[str, ...] = _SUPPORTED

    def __init__(self) -> None:
        self.venues: Tuple[str, ...] = _relay_venues()

    def to_snapshots(self, raw_rows: Sequence[dict]) -> List[MarketSnapshot]:
        """``odds_snapshots``-shaped dict rows -> MarketSnapshots (devigged)."""
        return normalise.from_oddsapi_rows(raw_rows)

    @staticmethod
    def cost_estimate(n_markets: int, n_regions: int) -> int:
        """OddsAPI credit cost of one fetch ≈ ``markets × regions``.

        Mirrors TheOddsAPI's documented billing (one credit per market per
        region per request). The budget governor uses this to decide how many
        markets it can afford before nearing the credit floor. Clamped to >=0.
        """
        return max(0, int(n_markets)) * max(0, int(n_regions))
