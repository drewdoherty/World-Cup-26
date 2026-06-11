"""Canonical team-name normalisation.

Different data sources spell national teams differently. The model ratings and
Dixon-Coles tables are keyed on the martj42 results-dataset spelling
("United States", "Bosnia and Herzegovina"), but the odds feed (The Odds API)
and prediction markets use their own conventions ("USA", "Bosnia &
Herzegovina"). A name that fails to resolve silently falls back to a default
rating and produces a *garbage* edge — the single most dangerous failure mode
in the pipeline — so every external team name MUST be passed through
:func:`canonical` before any model lookup.

Add new aliases here as they surface. Keep the canonical (right-hand) side
spelled exactly as it appears in ``data/raw/results.csv``.
"""

from __future__ import annotations

# alias (as seen in an external feed) -> canonical (martj42 results spelling)
ALIASES = {
    "USA": "United States",
    "Bosnia & Herzegovina": "Bosnia and Herzegovina",
    # Common alternates kept for robustness across feeds:
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "South Korea": "South Korea",
    "Korea Republic": "South Korea",
    "IR Iran": "Iran",
    "Côte d'Ivoire": "Ivory Coast",
    "Cote d'Ivoire": "Ivory Coast",
    "Türkiye": "Turkey",
    "Czechia": "Czech Republic",
    "Cabo Verde": "Cape Verde",
    "DR Congo": "DR Congo",
    "Republic of Ireland": "Republic of Ireland",
}


def canonical(name: str) -> str:
    """Return the canonical results-dataset spelling for a team name."""
    if name is None:
        return name
    return ALIASES.get(name.strip(), name.strip())
