"""Read-only Polymarket position inventory (public Data API).

Lifts the positions-poll that ``scripts/wca_pm_watch.py`` does inline into a
reusable, typed module the cash-out path shares. Pure HTTP read — no signing, no
private key, no local state. The Data API ``/positions`` endpoint returns one
row per held position; the fields we keep are exactly what a cash-out SELL needs
to be constructed and booked:

* ``asset``       — the ERC-1155 outcome-token id, i.e. ``place_order``'s
                    ``token_id`` and the ledger ``token_id``.
* ``size``        — shares currently held (what we can sell).
* ``avg_price``   — our cost basis per share.
* ``cur_price``   — the current mark (≈ best-bid/ask mid from Polymarket).
* ``neg_risk``    — feeds the ``neg_risk`` signing flag.
* ``outcome``     — "Yes"/"No" (which side of the binary market we hold).
* ``title`` / ``event_slug`` — used to classify the market and orient the score.

See :mod:`wca.pm.cashout` for the pure classification / kill-predicate logic
that consumes these rows.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Account-1 Polymarket proxy wallet (Gnosis safe holding the USDC). Same wallet
# wca_pm_watch.py polls. Override per call for a different account.
ACCOUNT1_WALLET = "0x86b4c55a4df1fbea0f325e842434e0a537caa549"
_DATA_API = "https://data-api.polymarket.com/positions"
_TRADES_API = "https://data-api.polymarket.com/trades"
_TIMEOUT = 30


@dataclass
class Position:
    """One held Polymarket outcome-token position (Data API ``/positions`` row)."""

    asset: str            # ERC-1155 token id == place_order token_id
    condition_id: str
    size: float           # shares held
    avg_price: float      # cost basis per share
    cur_price: float      # current mark
    outcome: str          # "Yes" / "No"
    title: str
    slug: str
    event_slug: str
    end_date: str
    neg_risk: bool
    redeemable: bool
    current_value: float

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Position":
        def _f(key: str) -> float:
            try:
                return float(d.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            asset=str(d.get("asset") or ""),
            condition_id=str(d.get("conditionId") or ""),
            size=_f("size"),
            avg_price=_f("avgPrice"),
            cur_price=_f("curPrice"),
            outcome=str(d.get("outcome") or ""),
            title=str(d.get("title") or ""),
            slug=str(d.get("slug") or ""),
            event_slug=str(d.get("eventSlug") or ""),
            end_date=str(d.get("endDate") or ""),
            neg_risk=bool(d.get("negativeRisk", False)),
            redeemable=bool(d.get("redeemable", False)),
            current_value=_f("currentValue"),
        )

    @property
    def is_open(self) -> bool:
        """Held and not yet resolved (resolved markets are redeemable, mark 0)."""
        return self.size > 0 and not self.redeemable


@dataclass
class Trade:
    """One executed fill from the Data API ``/trades`` endpoint.

    The authoritative record of what actually filled (``size`` in human share
    units, ``price`` per share) — used to book a cash-out from REAL fills rather
    than guessing from the order-POST response. Keyed by ``tx_hash``.
    """

    side: str             # "BUY" / "SELL"
    asset: str            # ERC-1155 token id
    size: float           # shares filled
    price: float          # USDC per share
    tx_hash: str
    timestamp: int
    title: str

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Trade":
        def _f(key: str) -> float:
            try:
                return float(d.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            side=str(d.get("side") or "").upper(),
            asset=str(d.get("asset") or ""),
            size=_f("size"),
            price=_f("price"),
            tx_hash=str(d.get("transactionHash") or ""),
            timestamp=int(d.get("timestamp") or 0),
            title=str(d.get("title") or ""),
        )


def _fetch_json(url: str, *, timeout: int, session: Optional[Any]) -> List[Dict[str, Any]]:
    if session is not None:
        # requests-like session injected for tests.
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    req = urllib.request.Request(url, headers={"User-Agent": "wca-cashout/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_raw(
    wallet: str, *, limit: int, timeout: int, session: Optional[Any]
) -> List[Dict[str, Any]]:
    url = "%s?user=%s&limit=%d" % (_DATA_API, wallet, limit)
    return _fetch_json(url, timeout=timeout, session=session)


def fetch_trades(
    wallet: str,
    *,
    limit: int = 100,
    session: Optional[Any] = None,
    timeout: int = _TIMEOUT,
) -> List[Trade]:
    """Fetch *wallet*'s executed fills (newest first) as typed :class:`Trade`s.

    This is the ground-truth fill feed the cash-out booking reconciles against —
    after a SELL we look here for the actual filled size/price instead of trusting
    the order-POST response.
    """
    url = "%s?user=%s&limit=%d" % (_TRADES_API, wallet, limit)
    raw = _fetch_json(url, timeout=timeout, session=session)
    return [Trade.from_api(d) for d in (raw or [])]


def fetch_positions(
    wallet: str = ACCOUNT1_WALLET,
    *,
    limit: int = 100,
    open_only: bool = False,
    session: Optional[Any] = None,
    timeout: int = _TIMEOUT,
) -> List[Position]:
    """Fetch held positions for *wallet* as typed :class:`Position` rows.

    ``open_only`` drops resolved/redeemable rows (mark already settled to 0/1).
    ``session`` (a requests-like object) is injectable for tests; production
    uses stdlib ``urllib``.
    """
    raw = _fetch_raw(wallet, limit=limit, timeout=timeout, session=session)
    out = [Position.from_api(d) for d in (raw or [])]
    if open_only:
        out = [p for p in out if p.is_open]
    return out
