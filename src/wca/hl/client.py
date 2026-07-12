"""Hyperliquid HIP-4 outcome-market ``/info`` client — READ-ONLY.

Everything here was verified against live API responses captured during the
2026-07-09 venue recon (dump files cited below live in the recon capture,
summarised + preserved in ``docs/research/hl_venue_recon_2026-07-09.md`` and
``tests/fixtures/hl_xvenue/``). No keys, no signing, no ``/exchange`` — this
client can only observe.

Addressing (the key that unlocks everything; recon ``docs_asset_ids.md``):

* ``encoding = 10 * outcome_id + side``  (side 0/1, ordered per the
  ``sideSpecs`` list in ``outcomeMeta`` — e.g. champion markets are
  ``["Yes", "No"]``; QF match markets are ``[teamA, teamB]`` with NO draw).
* L2/trades/candles coin string: ``"#<encoding>"`` (Argentina-champion Yes,
  outcome 173 side 0 -> ``"#1730"``).
* Token name ``"+<encoding>"``; order asset id ``100_000_000 + encoding``
  (documented for completeness ONLY — this module never places orders).

Verified-working ``POST /info`` request types (recon ``probe_*.json``):

* ``{"type": "outcomeMeta"}``                       -> all outcome specs +
  ``questions`` (grouped sets with ``fallbackOutcome`` / ``namedOutcomes`` /
  ``settledNamedOutcomes``).
* ``{"type": "settledOutcome", "outcome": N}``      -> ``settleFraction`` +
  human ``details`` (``null`` while unsettled). Champion (question 32) No
  legs settle EARLY on mathematical elimination — observed live on outcome
  172 Algeria ("eliminated ... can no longer win").
* ``{"type": "l2Book", "coin": "#<enc>"}``          -> standard L2, MAX 20
  LEVELS PER SIDE over REST (use the WS ``l2Book`` channel or ``nSigFigs``
  aggregation for deeper needs).
* ``{"type": "recentTrades", "coin": "#<enc>"}``, ``candleSnapshot``,
  ``allMids`` (includes ``#`` coins), ``userFills``.

NOT available (all HTTP 422, n=15 negative probes 2026-07-09): outcomeCtxs,
outcomeMetaAndCtxs, outcomeAssetCtxs, outcomeStates, outcomeL2Book,
outcomeBook, activeAssetCtx on ``#`` coins, tokenDetails on ``+`` tokens,
outcomeOpenInterest, outcomeSupply, questionMeta, ... => there is NO public
open-interest / day-volume ctx endpoint for outcome markets; 24h volume must
be derived from candles. Outcome tokens are also absent from ``spotMeta``.

The official Hyperliquid Python SDK (master, 2026-07-09) has ZERO outcome
support — asset ids/coins must be constructed manually as above.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
_TIMEOUT = 15
_HEADERS = {
    "User-Agent": "WorldCupAlpha/0.1 (read-only research; contact via GitHub)",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# --------------------------------------------------------------------------
# Addressing helpers (pure; recon docs_asset_ids.md)
# --------------------------------------------------------------------------

def encoding(outcome_id: int, side: int) -> int:
    """HIP-4 side encoding: ``10 * outcome_id + side`` (side must be 0 or 1)."""
    if side not in (0, 1):
        raise ValueError("HIP-4 outcome markets have exactly two sides (0/1); got %r" % (side,))
    return 10 * int(outcome_id) + side


def coin(outcome_id: int, side: int) -> str:
    """Coin string accepted by l2Book/recentTrades/candleSnapshot: ``#<enc>``."""
    return "#%d" % encoding(outcome_id, side)


def token_name(outcome_id: int, side: int) -> str:
    """Outcome token name: ``+<enc>`` (absent from spotMeta; reference only)."""
    return "+%d" % encoding(outcome_id, side)


def order_asset_id(outcome_id: int, side: int) -> int:
    """Order asset id ``100_000_000 + enc``. Documented for completeness —
    nothing in ``wca.hl`` places orders (read-only venue, no live-money gate
    cleared)."""
    return 100_000_000 + encoding(outcome_id, side)


def is_vpn_drop(exc: BaseException) -> bool:
    """True when *exc* looks like the NordVPN-tunnel drop signature.

    Both api.hyperliquid.xyz and Polymarket are only reachable from the dev
    box over the VPN; when the tunnel drops, TLS handshakes start failing
    with ``SSL: WRONG_VERSION_NUMBER``. Callers should REPORT this and stop
    — never retry blindly (standing network note, 2026-07-09 recon).
    """
    if isinstance(exc, requests.exceptions.SSLError) or isinstance(
        getattr(exc, "__cause__", None), requests.exceptions.SSLError
    ):
        return "WRONG_VERSION_NUMBER" in str(exc)
    return False


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class HLInfoClient:
    """Thin read-only wrapper over ``POST {HL_INFO_URL}``.

    ``session`` is injectable for tests (anything with a ``.post(url, data=…,
    headers=…, timeout=…)`` returning a requests-like response); default is
    the ``requests`` module itself. No credentials anywhere.
    """

    def __init__(self, url: str = HL_INFO_URL, timeout: int = _TIMEOUT, session: Any = None):
        self.url = url
        self.timeout = timeout
        self._session = session if session is not None else requests

    def _post(self, payload: Dict[str, Any]) -> Any:
        resp = self._session.post(
            self.url, data=json.dumps(payload), headers=_HEADERS, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    # -- verified request types --------------------------------------------

    def outcome_meta(self) -> Dict[str, Any]:
        """All outcome specs + grouped ``questions``."""
        return self._post({"type": "outcomeMeta"})

    def settled_outcome(self, outcome_id: int) -> Optional[Dict[str, Any]]:
        """``settleFraction`` + ``details`` for a settled outcome; None if unsettled."""
        return self._post({"type": "settledOutcome", "outcome": int(outcome_id)})

    def l2_book(
        self,
        outcome_id: int,
        side: int,
        n_sig_figs: Optional[int] = None,
        mantissa: Optional[int] = None,
    ) -> Dict[str, Any]:
        """L2 book for one side of an outcome (REST caps at 20 levels/side)."""
        payload: Dict[str, Any] = {"type": "l2Book", "coin": coin(outcome_id, side)}
        if n_sig_figs is not None:
            payload["nSigFigs"] = int(n_sig_figs)
        if mantissa is not None:
            payload["mantissa"] = int(mantissa)
        return self._post(payload)

    def recent_trades(self, outcome_id: int, side: int) -> List[Dict[str, Any]]:
        return self._post({"type": "recentTrades", "coin": coin(outcome_id, side)})

    def candle_snapshot(
        self, outcome_id: int, side: int, interval: str, start_ms: int, end_ms: int
    ) -> List[Dict[str, Any]]:
        """1h candles are the only 24h-volume source (no ctx endpoint exists).

        NOTE: side-0 and side-1 candle volumes are the SAME tape mirrored
        (verified across all 12 WC markets in the recon) — never sum sides.
        """
        return self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin(outcome_id, side),
                    "interval": interval,
                    "startTime": int(start_ms),
                    "endTime": int(end_ms),
                },
            }
        )

    def all_mids(self) -> Dict[str, str]:
        """Mids for every coin, ``#`` outcome coins included."""
        return self._post({"type": "allMids"})


# --------------------------------------------------------------------------
# Response parsing (pure)
# --------------------------------------------------------------------------

def parse_l2_book(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise an HL l2Book payload to sorted float levels.

    Input shape (verified, recon ``l2book_*.json``)::

        {"coin": "#2021", "time": 1783620884518,
         "levels": [[{"px": "0.9354", "sz": "527.0", "n": 2}, ...],   # bids
                    [{"px": "0.936",  "sz": "...",   "n": 1}, ...]]}  # asks

    Returns ``{"coin", "time_ms", "bids": [(px, sz)...] desc,
    "asks": [(px, sz)...] asc}``. Sizes are SHARES (1 share = $1 max payout;
    szDecimals=0 observed — all 480 visible level sizes in the recon capture
    were integers).
    """
    levels = payload.get("levels") or [[], []]
    bids = sorted(
        ((float(l["px"]), float(l["sz"])) for l in levels[0]), key=lambda x: -x[0]
    )
    asks = sorted(
        ((float(l["px"]), float(l["sz"])) for l in levels[1]), key=lambda x: x[0]
    )
    return {
        "coin": payload.get("coin"),
        "time_ms": int(payload.get("time") or 0),
        "bids": bids,
        "asks": asks,
    }


def best_bid_ask(
    book: Dict[str, Any]
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """(bid, bid_sz, ask, ask_sz) from a :func:`parse_l2_book` dict (None if empty)."""
    bid, bid_sz = (book["bids"][0] if book["bids"] else (None, None))
    ask, ask_sz = (book["asks"][0] if book["asks"] else (None, None))
    return bid, bid_sz, ask, ask_sz


def outcomes_by_id(meta: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """Index an ``outcomeMeta`` response's outcomes list by outcome id."""
    return {int(o["outcome"]): o for o in meta.get("outcomes", [])}


def side_names(meta: Dict[str, Any], outcome_id: int) -> List[str]:
    """Ordered side names for *outcome_id* (defines the 0/1 side encoding)."""
    spec = outcomes_by_id(meta).get(int(outcome_id))
    if spec is None:
        raise KeyError("outcome %r not in outcomeMeta" % (outcome_id,))
    return [s.get("name") for s in spec.get("sideSpecs", [])]
