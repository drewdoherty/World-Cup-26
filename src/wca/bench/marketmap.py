"""Canonicalise the free-text market labels found in the ledger.

The ``bets`` table accumulated 40+ inconsistent market strings over manual and
automated logging ("Match Odds", "Full-time result", "Full Time Result",
"h2h", "Match Winner", "MATCH", "bet_builder_acca", ...). Any per-market
benchmark breakdown is meaningless until these collapse onto a stable family
key. This is pure string work; the mapping is intentionally explicit and
ordered (first match wins) so it is auditable.

Separators (``_ - /``) are normalised to spaces before matching, so
``"bet_builder_acca"`` and ``"Bet Builder / Acca"`` land in the same family.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# Canonical families, ordered most-specific first. A combo that mentions a
# bet-builder/acca is classified as such even if it also names "correct score".
_RULES: List[Tuple[str, str]] = [
    (r"(acca|accumulator|treble|double|bet ?builder|betbuilder|sgm|same game)",
     "acca_betbuilder"),
    (r"(both teams to score|btts)", "btts"),
    (r"(shots? on target|\bsot\b)", "shots_on_target"),
    (r"(corners?)", "corners"),
    (r"(cards?|bookings?|to be booked|carded)", "cards"),
    (r"(correct score|exact score)", "correct_score"),
    (r"(first goal ?scorer|anytime|to score|goal ?scorer|score or assist)",
     "goalscorer"),
    (r"(total goals|over.?under|\btotals?\b|goals? o ?u)", "totals"),
    (r"(asian handicap|handicap)", "handicap"),
    (r"(golden boot|outright|winner of|to win the|top goalscorer)", "outright"),
    (r"(advancement|reach the|be eliminated|round of|to qualify|group winner)",
     "advancement"),
    (r"(match odds|match winner|full ?time result|1x2|moneyline|match result|"
     r"to win match|\bmatch\b|\bh2h\b|h2h lay)", "1x2"),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), fam) for pat, fam in _RULES]


def canonical_market(raw: str) -> str:
    """Map a free-text market label onto a canonical family key.

    Returns ``"other"`` when nothing matches so unknowns surface rather than
    silently merging into a real family.
    """
    if not raw:
        return "other"
    s = re.sub(r"[_\-/]+", " ", str(raw).strip().lower())
    s = re.sub(r"\s+", " ", s)
    for pat, fam in _COMPILED:
        if pat.search(s):
            return fam
    return "other"
