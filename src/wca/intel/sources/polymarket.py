"""Polymarket source adapter (free / Gamma).

Polymarket quotes a YES *probability* directly (the token mid in [0,1]), not
decimal odds — so this adapter converts ``pm_mid`` -> decimal (``1/mid``) and
feeds :func:`wca.intel.normalise.normalise_market`. It is network-free by design:
the live fetch lives in :mod:`wca.pmhistory` / ``scripts/wca_pm_snapshot.py``;
here we accept already-fetched rows so collection stays unit-testable.

Honesty / scope: PM offers binary YES/NO markets (advancement, outright winner,
match moneyline as YES-on-team). We treat each as a single-selection
``moneyline`` snapshot keyed by team unless the caller supplies an explicit
``market_type``/``selection``. Because a single binary market is not a complete
multi-outcome book, ``implied_devig`` is left ``None`` by the normaliser (we
never fabricate a fair price from one leg) — the raw mid is the honest number.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from wca.intel import normalise
from wca.intel.store import MarketSnapshot

_VENUE = "polymarket"


def _decimal_from_mid(mid: object) -> Optional[float]:
    """YES probability in (0,1) -> decimal odds; None if out of range."""
    try:
        p = float(mid)
    except (TypeError, ValueError):
        return None
    if not (0.0 < p < 1.0):
        return None
    return 1.0 / p


class PolymarketSource:
    """Adapter for already-fetched Polymarket quote dicts.

    Each input row is a dict with at least ``pm_mid`` (YES probability) OR
    ``decimal_odds``, plus ``ts_utc``; optional ``selection`` (defaults to
    ``team``), ``market_type`` (defaults to ``moneyline``), ``fixture_id``,
    ``ko_utc``, ``line``. Unknown/blank mids are skipped.
    """

    name = "polymarket"
    venues = (_VENUE,)
    #: PM only sells the binary markets we map to moneyline (incl. outright /
    #: advancement, both stored under "moneyline" with a stage in ``line``/raw).
    supported_markets = ("moneyline",)

    def to_snapshots(self, raw_rows: Sequence[dict]) -> List[MarketSnapshot]:
        out: List[MarketSnapshot] = []
        for r in raw_rows:
            dec = r.get("decimal_odds")
            if dec is None:
                dec = _decimal_from_mid(r.get("pm_mid"))
            if dec is None:
                continue
            selection = r.get("selection") or r.get("team")
            if not selection:
                continue
            market_type = r.get("market_type") or "moneyline"
            out.extend(normalise.normalise_market(
                source=self.name,
                venue=_VENUE,
                market_type=market_type,
                selection_odds={selection: float(dec)},
                ts_utc=r["ts_utc"],
                fixture_id=r.get("fixture_id"),
                ko_utc=r.get("ko_utc"),
                line=r.get("line"),
            ))
        return out
