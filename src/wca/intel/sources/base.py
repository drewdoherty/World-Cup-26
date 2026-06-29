"""The ``Source`` adapter contract for the Market Intelligence collector.

WHY a protocol rather than a base class: collection has exactly one job per
provider — "raw payload -> list[MarketSnapshot]" — and everything else (the
odds/devig math, the store, the cadence planner) is shared. A minimal protocol
lets each adapter stay a few lines wrapping :mod:`wca.intel.normalise`, while the
CLI can iterate over a heterogeneous list of sources uniformly.

A ``Source`` advertises three things so the planner/CLI can reason about it
without fetching anything:
  * ``name``             — the collector key stamped onto rows (e.g. "theoddsapi").
  * ``venues``           — canon venue names this source provides quotes for.
  * ``supported_markets``— the subset of :data:`wca.intel.registry.MARKET_TYPES`
                           the provider actually sells (honesty gate: we never
                           claim to collect a market a provider doesn't offer).

and one behaviour: :meth:`to_snapshots`, turning raw rows into MarketSnapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from wca.intel.store import MarketSnapshot


@dataclass
class RawQuote:
    """A provider-agnostic raw quote, before normalisation.

    Lightweight carrier used by adapters that fetch their own shape (e.g.
    Polymarket Gamma); the OddsAPI adapter consumes the existing ``odds_snapshots``
    dict shape directly via :func:`wca.intel.normalise.from_oddsapi_rows`, so it
    does not require this type. ``selection_odds`` maps selection -> decimal odds
    for one complete market at one venue/time, mirroring
    :func:`wca.intel.normalise.normalise_market`.
    """

    source: str
    venue: str
    market_type: str
    selection_odds: Dict[str, float]
    ts_utc: str
    fixture_id: Optional[str] = None
    ko_utc: Optional[str] = None
    line: Optional[float] = None
    liquidity: Optional[Dict[str, float]] = field(default=None)


@runtime_checkable
class Source(Protocol):
    """What every collection adapter must expose."""

    #: Collector key stamped onto every produced row's ``source``.
    name: str
    #: Canon venue names (per :data:`wca.intel.registry.VENUES`) this source covers.
    venues: Tuple[str, ...]
    #: Market types this provider actually sells (subset of MARKET_TYPES).
    supported_markets: Tuple[str, ...]

    def to_snapshots(self, raw_rows: Sequence[object]) -> List[MarketSnapshot]:
        """Map this source's raw rows to normalised MarketSnapshots."""
        ...
