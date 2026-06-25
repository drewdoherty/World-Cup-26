"""Monte-Carlo valuation of the WCA betting book.

Module A flagship: value the OPEN book against many simulated tournament
outcomes to produce a P&L *distribution* (not a point EV).  See
:mod:`wca.mc.pnl` for the public surface.
"""

from .pnl import (
    OpenPosition,
    PnlResult,
    build_risk_pnl,
    load_open_positions,
    settle_vectorised,
    simulate_book,
    wilson,
)

__all__ = [
    "OpenPosition",
    "PnlResult",
    "build_risk_pnl",
    "load_open_positions",
    "settle_vectorised",
    "simulate_book",
    "wilson",
]
