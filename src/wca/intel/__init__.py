"""Market Intelligence & Cross-Venue Analytics subsystem.

Phase-0 foundation: a generalised, source-pluggable market-snapshot store, a
venue registry, and odds normalisation (decimal -> implied -> vig-adjusted).
Built to accumulate a historical market-intelligence database that later powers
cross-venue spread analytics, price-discovery / lead-lag research, CLV, and the
``/arb`` scanner.

Reuses (does not re-implement): ``wca.markets.devig`` (vig removal),
``wca.venues`` (canonical venue names), and later ``wca.venuesbench`` (consensus
/ spread), ``wca.markets.kelly`` (sizing), ``wca.rigor.clv`` (CLV / volatility).
See docs/market_intelligence_design.md.
"""

from wca.intel.registry import VENUES, MARKET_TYPES, Venue, venue_for, venue_colour  # noqa: F401
from wca.intel.store import (  # noqa: F401
    MarketSnapshot, SNAPSHOT_COLUMNS, ensure_schema, append_snapshots, latest_per_selection,
)
from wca.intel import normalise  # noqa: F401
