"""Producer: card recommendations -> Polymarket parked-order proposals.

This is the *read-and-size* half of the Polymarket loop. It reuses the
matchday card generator (:func:`wca.card.build_card`) for the Polymarket pool
only, resolves each recommendation to a live Polymarket YES token via
:func:`wca.data.polymarket.resolve_outcome_token`, and sizes a quarter-Kelly
stake **at the Polymarket price** (not the sportsbook price the card shopped).

Nothing here parks or places an order. It returns proposal dicts; the CLI
(``scripts/wca_pm_propose.py``) pushes each through the bot's park gate
(``push_parked_order``) and notifies Telegram, and execution stays behind the
``Y PM-<n>`` confirmation + ``PM_DRY_RUN`` flag.

Sizing
------
For a recommendation with modelled win probability ``p`` and a Polymarket YES
price ``q`` (so decimal odds ``1/q``), the quarter-Kelly stake on ``pool_usd``
is :func:`wca.markets.kelly.stake` ``(p, 1/q, pool_usd, fraction, cap)``. The
result is then hard-capped at ``min(max_order_usd, cap * pool_usd)`` so a
single order can never exceed the per-order USD limit *or* the per-bet bankroll
fraction, whichever is tighter. ``edge`` is recomputed against the Polymarket
price too, because the sportsbook edge the card carries was earned at a
different price.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from wca.card import (
    BlendWeights,
    FittedModels,
    PoolConfig,
    build_card,
)
from wca.data.polymarket import resolve_outcome_token
from wca.markets import kelly as kelly_mod

# Below this notional an order is not worth parking (dust / rounding).
_MIN_ORDER_USD = 1.0


def build_pm_proposals(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    pool_usd: float,
    *,
    min_edge: float = 0.02,
    max_order_usd: float = 30.0,
    fraction: float = 0.25,
    cap: float = 0.05,
    events: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Turn the card's recommendations into Polymarket parked-order proposals.

    Parameters
    ----------
    models:
        Fitted Elo + Dixon-Coles models (:func:`wca.card.fit_models`).
    odds_df:
        Flat sportsbook odds frame with h2h rows — only used to *find* +EV
        selections; the stake is sized against the Polymarket price.
    fixtures_meta:
        Results-schedule rows for neutral/host resolution (same as the card).
    pool_usd:
        The Polymarket bankroll pool in USDC (e.g. 2500.0).
    min_edge:
        Minimum sportsbook edge for the card to surface a selection.
    max_order_usd:
        Absolute per-order USD ceiling.
    fraction:
        Kelly multiplier (quarter Kelly by default).
    cap:
        Per-bet cap as a fraction of ``pool_usd``.
    events:
        Optional pre-fetched Polymarket events to resolve tokens against (avoids
        a network call). When ``None`` the live World Cup events are fetched
        once per :func:`resolve_outcome_token` call that needs them.

    Returns
    -------
    list of proposal dicts, each carrying::

        {token_id, side: "BUY", price, size_usd, shares, market_question,
         outcome, match_desc, model_prob, ev, neg_risk}

    Recommendations with no resolvable token, or a sized stake below
    ``$1``, are skipped.
    """
    # Size the card on a single Polymarket pool. We use this pool's bankroll
    # only to *rank* and gate; the per-proposal stake is recomputed at the
    # Polymarket price below so it reflects what we actually pay there.
    pool = PoolConfig(
        name="polymarket",
        bankroll=float(pool_usd),
        currency="USD",
        kelly_fraction=fraction,
        per_bet_cap=cap,
    )
    recs = build_card(
        models,
        odds_df,
        [pool],
        fixtures_meta=fixtures_meta,
        weights=BlendWeights(),
        min_edge=min_edge,
    )

    hard_cap = min(float(max_order_usd), float(cap) * float(pool_usd))

    proposals: List[Dict[str, Any]] = []
    for rec in recs:
        # rec.match_desc is "<home> vs <away>" in canonical spelling.
        home, _, away = rec.match_desc.partition(" vs ")
        resolved = resolve_outcome_token(
            home.strip(),
            away.strip(),
            rec.selection_team,
            events=events,
        )
        if resolved is None:
            continue

        price = float(resolved["price"])
        if not (0.0 < price < 1.0):
            continue

        pm_odds = 1.0 / price
        # Quarter-Kelly at the Polymarket price, then hard-capped.
        size_usd = kelly_mod.stake(
            rec.model_prob, pm_odds, float(pool_usd), fraction=fraction, cap=cap
        )
        size_usd = min(size_usd, hard_cap)
        if size_usd < _MIN_ORDER_USD:
            continue

        ev = kelly_mod.edge(rec.model_prob, pm_odds)
        proposals.append(
            {
                "token_id": resolved["token_id"],
                "side": "BUY",
                "price": price,
                "size_usd": size_usd,
                "shares": size_usd / price,
                "market_question": resolved["market_question"],
                # The WC event slug (e.g. ``fifwc-bra-mar-2026-06-13``) proves
                # World-Cup provenance for the trader's keyword allowlist; the
                # single-match question itself carries no "world cup"/"fifa"
                # keyword.  The bot folds this into the allowlist check.
                "event_slug": resolved.get("event_slug", ""),
                "outcome": resolved["outcome"],
                "match_desc": rec.match_desc,
                "model_prob": rec.model_prob,
                "ev": ev,
                "neg_risk": bool(resolved["neg_risk"]),
            }
        )

    return proposals
