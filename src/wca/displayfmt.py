# src/wca/displayfmt.py — percent display convention (user ruling 2026-07-08).
"""Shared helpers for the PERCENT display convention on every bot surface.

USER RULING (2026-07-08, supersedes the 2026-07-03 "classic decimal" card
convention): ALL Telegram-bot commands display odds as PERCENTAGES
(``model X% / mkt Y%``), never bare decimal odds. Where a book's decimal
price is the executable number it is shown as its implied percentage with
the venue tagged. Polymarket's ¢ convention survives — ¢ IS a percent.
Alongside the percentages, every displayed selection carries its edge and a
clear ``+EV`` / ``−EV`` marker, and the canonical selection-rule ordering
(:mod:`wca.selection`: moneylines over mids over longshots) stays visible.

These helpers are FORMATTING ONLY. No gate, cap, sizing or selection logic
lives here — they turn numbers the caller already computed into strings.
"""
from typing import Optional

from wca.selection import prob_bucket

#: Display tag per model-prob bucket (canonical buckets, wca.selection).
BUCKET_TAGS = {"moneyline": "ML", "mid": "MID", "longshot": "LS"}


def pct(p: Optional[float], dp: int = 1) -> str:
    """A probability (0-1) as a percent string: ``0.579`` -> ``"57.9%"``."""
    if p is None:
        return "?"
    return "%.*f%%" % (dp, float(p) * 100.0)


def implied_prob(decimal_odds: Optional[float]) -> Optional[float]:
    """Implied probability (0-1) of a decimal price; None when unusable."""
    try:
        o = float(decimal_odds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if o <= 1.0:
        return None
    return 1.0 / o


def implied_pct(decimal_odds: Optional[float], dp: int = 1) -> str:
    """A decimal price as its implied percent: ``4.08`` -> ``"24.5%"``.

    This is THE way an executable book price is displayed under the
    2026-07-08 ruling (never the bare decimal); tag the venue at the call
    site, e.g. ``"24.5% impl (polymarket)"``.
    """
    p = implied_prob(decimal_odds)
    return pct(p, dp) if p is not None else "?"


def edge_pp(x: Optional[float], dp: int = 1) -> str:
    """A probability GAP (0-1 scale) in percentage points: ``0.058`` -> ``"+5.8pp"``."""
    if x is None:
        return "?"
    return "%+.*fpp" % (dp, float(x) * 100.0)


def ev_str(ev: Optional[float], dp: int = 1) -> str:
    """An EV-per-unit-stake (0-1 scale) as a signed percent: ``"+5.8%"``."""
    if ev is None:
        return "?"
    return "%+.*f%%" % (dp, float(ev) * 100.0)


def ev_marker(edge: Optional[float]) -> str:
    """The mandatory +EV / −EV marker (ruling: indicated EVERYWHERE).

    ``edge`` is whatever signed edge/EV quantity the surface computed
    (probability gap or EV-per-unit — only the SIGN is read). ``None``
    (no market price -> EV unverifiable) yields an explicit "EV?" so a
    missing price is never dressed up as +EV.
    """
    if edge is None:
        return "EV?"
    return "✅+EV" if float(edge) > 0.0 else "❌−EV"


def bucket_tag(model_prob: Optional[float]) -> str:
    """Selection-rule bucket tag for display: ``ML`` / ``MID`` / ``LS``.

    Buckets come from :func:`wca.selection.prob_bucket` (the canonical rule
    — never re-implemented here); this only maps them to short tags so the
    moneylines-over-longshots ordering is visible on every surface.
    """
    return BUCKET_TAGS[prob_bucket(model_prob)]
