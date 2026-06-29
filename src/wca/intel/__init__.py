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
    MarketSnapshot, SNAPSHOT_COLUMNS, METRIC_COLUMNS, ensure_schema,
    append_snapshots, append_metrics, latest_per_selection,
)
from wca.intel.metrics import (  # noqa: F401
    selection_metrics, consensus_probs, build_market_metrics,
)
from wca.intel.feed import build_feed, venue_legend  # noqa: F401
from wca.intel.poller import (  # noqa: F401
    PollingConfig, Fixture, FixturePlan, plan_polls, load_polling_config, default_polling_config,
)
from wca.intel.arb import ArbLeg, ArbOpportunity, scan_market, format_arb_report  # noqa: F401
from wca.intel import normalise, sources  # noqa: F401
