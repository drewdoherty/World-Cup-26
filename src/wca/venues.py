"""Canonical bookmaker / venue name normalisation.

Leaf module: MUST NOT import from ``wca.bot`` (avoid circular deps). This is
the single source of truth for venue-name canonicalisation, applied at every
ledger write path so the live site never shows ``Bet365`` and ``bet365`` (or
``Unknown`` and ``''``) as separate books.

Betfair Exchange (betfair_ex_uk, "Betfair Exchange", "betfair ex") -> "Betfair"
Betfair Sportsbook ("Betfair Sportsbook", bare "betfair", betfair_sportsbook) -> "Betfair Sportsbook"
Hard rule: bare "Betfair" always maps to sportsbook. Exchange must be explicit.
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
    # Exchange variants — ONLY explicit exchange tokens map to "Betfair" (exchange).
    # Hard rule: bare "betfair" is treated as sportsbook, not exchange.
    if pl in ("betfair_ex_uk", "betfair_ex_eu", "betfair exchange", "betfair ex"):
        return "Betfair"
    if "betfair" in pl and "exchange" in pl:
        return "Betfair"
    # Sportsbook variants — bare "betfair" defaults here (hard rule per account routing policy).
    if pl in ("betfair", "betfair_sportsbook", "betfair sportsbook", "betfair sports"):
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
        # Note: "betfair" is caught by the hard-rule block above; not reached here.
    }
    if pl in _MAP:
        return _MAP[pl]
    # Return as-is (title-case if all-lower or all-slug)
    return p if any(c.isupper() for c in p) else p.title().replace("_", " ")


# ---------------------------------------------------------------------------
# OddsAPI bookmaker-key canonicalisation (for the Model-vs-Venue benchmark).
# ---------------------------------------------------------------------------

#: Raw OddsAPI ``bookmaker_key`` values seen in ``odds_snapshots.raw`` -> the
#: canonical venue string produced by :func:`canon_platform`. Keeping this as an
#: explicit table (rather than guessing) means a new book shows up verbatim and
#: is easy to spot, instead of being silently mis-merged. The two Betfair keys
#: map to the SAME canonical names the ledger uses (exchange vs sportsbook are
#: genuinely different venues and must not merge).
_ODDSAPI_BOOK_KEYS = {
    "betfair_ex_uk": "Betfair",            # exchange
    "betfair_ex_eu": "Betfair",            # exchange
    "betfair_sb_uk": "Betfair Sportsbook",
    "betfair_sb_eu": "Betfair Sportsbook",
    "skybet": "Sky Bet",
    "paddypower": "Paddy Power",
    "sport888": "888sport",
    "smarkets": "Smarkets",
    "casumo": "Casumo",
    "livescorebet": "LiveScore Bet",
    "grosvenor": "Grosvenor",
    "virginbet": "Virgin Bet",
    "leovegas": "LeoVegas",
    "ladbrokes_uk": "Ladbrokes",
    "coral": "Coral",
    "betfred_uk": "Betfred",
    "williamhill": "William Hill",
    "unibet_uk": "Unibet",
    "boylesports": "BoyleSports",
    "betway": "Betway",
    "matchbook": "Matchbook",
    "betvictor": "BetVictor",
}

#: Venues that settle on the regulated/exchange model and are treated as the
#: "executable" tier (exchange commission applies on net winnings).
EXCHANGE_VENUES = ("Betfair", "Smarkets", "Matchbook")


def canon_book(raw: str) -> str:
    """Canonicalise an OddsAPI ``bookmaker_key`` (or any venue label) to a
    stable display name, so the Model-vs-Venue benchmark never double-counts a
    book under two spellings.

    Resolution order: the explicit OddsAPI-key table first (these are the keys
    that appear in ``odds_snapshots.raw``), then fall back to the shared
    :func:`canon_platform` normaliser for ledger-style names. Empty / unknown
    -> ``"Unknown"``.
    """
    p = (raw or "").strip()
    if not p:
        return "Unknown"
    key = p.lower()
    if key in _ODDSAPI_BOOK_KEYS:
        return _ODDSAPI_BOOK_KEYS[key]
    return canon_platform(p)


def is_exchange(canon_name: str) -> bool:
    """True if a canonical venue name is a betting exchange (commission model)."""
    return (canon_name or "") in EXCHANGE_VENUES
