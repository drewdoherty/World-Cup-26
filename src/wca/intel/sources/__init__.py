"""Source adapters for the Market Intelligence collector.

A *source* turns a venue/provider's raw payload into normalised
:class:`~wca.intel.store.MarketSnapshot`s. The adapter is deliberately thin —
all odds math lives in :mod:`wca.intel.normalise`; an adapter only knows the
shape of its provider's rows, which canon venues it covers, and which markets
that provider actually sells. Keeping adapters network-free (they accept
already-fetched rows) makes the whole collection path unit-testable: the CLI
does IO, the adapters and the planner stay pure.

See :mod:`wca.intel.sources.base` for the ``Source`` protocol.
"""

from wca.intel.sources.base import RawQuote, Source  # noqa: F401
from wca.intel.sources.oddsapi import OddsApiSource  # noqa: F401
from wca.intel.sources.polymarket import PolymarketSource  # noqa: F401

__all__ = ["RawQuote", "Source", "OddsApiSource", "PolymarketSource"]
