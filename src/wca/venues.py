"""Canonical bookmaker / venue name normalisation.

Leaf module: MUST NOT import from ``wca.bot`` (avoid circular deps). This is
the single source of truth for venue-name canonicalisation, applied at every
ledger write path so the live site never shows ``Bet365`` and ``bet365`` (or
``Unknown`` and ``''``) as separate books.

Betfair Exchange (betfair_ex_uk, "Betfair Exchange", bare "Betfair") -> "Betfair"
Betfair Sportsbook ("Betfair Sportsbook", betfair_sportsbook)        -> "Betfair Sportsbook"
These two are genuinely different venues and are NOT merged.
"""
from __future__ import annotations


def canon_platform(raw: str) -> str:
    """Normalise a bookmaker name to the canonical DB string.

    Empty / ``None`` / any-case ``"unknown"`` -> ``"Unknown"``.
    Brand ``bet365`` is canonical lowercase (deliberate casing).
    """
    p = (raw or "").strip()
    pl = p.lower()
    # Empty / unknown sentinel
    if pl == "" or pl == "unknown":
        return "Unknown"
    # Non-sportsbook venue tokens are exact lowercase pool keys used by the
    # ledger venue routing (_venue_of / sub-pool selection). Preserve verbatim —
    # title-casing them would silently fork the currency pools.
    if pl in ("polymarket", "polymarket-auto", "kalshi"):
        return pl
    # Exchange variants — bare "Betfair" or anything mentioning "exchange"
    if pl in ("betfair", "betfair_ex_uk", "betfair_ex_eu", "betfair exchange", "betfair ex"):
        return "Betfair"
    if "betfair" in pl and "exchange" in pl:
        return "Betfair"
    # Sportsbook variants
    if pl in ("betfair_sportsbook", "betfair sportsbook", "betfair sports"):
        return "Betfair Sportsbook"
    if "betfair" in pl and ("sports" in pl or "sb" in pl):
        return "Betfair Sportsbook"
    # Other known normalizations — merge duplicate casings/spellings.
    _MAP = {
        "paddy power": "Paddy Power",
        "paddypower": "Paddy Power",
        "skybet": "Sky Bet",
        "sky bet": "Sky Bet",
        "virgin bet": "Virgin Bet",
        "virginbet": "Virgin Bet",
        "bet 365": "bet365",
        "bet365": "bet365",
        "betfred": "Betfred",
        "betway": "Betway",
        "ladbrokes": "Ladbrokes",
        "betfair": "Betfair",  # fallback bare match (already caught above but safety)
    }
    if pl in _MAP:
        return _MAP[pl]
    # Return as-is (title-case if all-lower or all-slug)
    return p if any(c.isupper() for c in p) else p.title().replace("_", " ")
