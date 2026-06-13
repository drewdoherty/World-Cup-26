"""2026 World Cup venues and venue/geography-aware host advantage.

The classic single-host home boost (~100 Elo points) is mis-specified for 2026
for two reasons, both flagged by Joachim Klement's co-host note:

1. **Dilution across three co-hosts.** A boost shared three ways, with each host
   only "at home" in its own country (and only in the group stage — the
   knockout bracket roams), should not equal a lone host's full edge.
2. **Geography / altitude.** Mexico's marquee venues sit at altitude — above all
   Estadio Azteca in Mexico City (~2240 m) — which taxes visiting sea-level
   teams far more than a Toronto or Dallas fixture does.

This module supplies the venue table and a single function,
:func:`host_advantage_points`, that converts the legacy flat bonus into a
diluted, altitude-aware one. It is **opt-in**: callers pass an explicit
``factor`` and ``altitude_coef``; the defaults reproduce the legacy full bonus so
nothing changes unless a caller asks for it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

DEFAULT_VENUES_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "structural" / "venues_2026.csv"
)

#: Altitude gap (metres, venue minus visitor's home) below which altitude has no
#: meaningful effect. Only large gaps (e.g. a sea-level side at Mexico City) bite.
ALTITUDE_THRESHOLD_M = 1000.0

#: Default dilution factor for a co-host's home bonus. 1.0 reproduces the legacy
#: full single-host bonus; the venue-aware path uses a smaller value.
DEFAULT_HOST_FACTOR = 1.0

#: Elo points added per metre of altitude gap above the threshold. ~25 points at
#: a sea-level side playing Mexico City (gap ~1240 m above threshold).
DEFAULT_ALTITUDE_COEF = 0.02


@dataclass(frozen=True)
class Venue:
    city: str
    stadium: str
    country: str
    altitude_m: float


def load_venues(path: Optional[Path] = None) -> Dict[str, Venue]:
    """Load the 2026 venue table keyed by city name (matching the .ics LOCATION)."""
    p = Path(path) if path is not None else DEFAULT_VENUES_PATH
    rows = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.lstrip().startswith("#"):
                continue
            rows.append(line)
    reader = csv.DictReader(rows)
    out: Dict[str, Venue] = {}
    for r in reader:
        city = r["city"].strip()
        out[city] = Venue(
            city=city,
            stadium=r["stadium"].strip(),
            country=r["country"].strip(),
            altitude_m=float(r["altitude_m"]),
        )
    return out


def altitude_penalty_points(
    venue_altitude_m: float,
    visitor_home_altitude_m: float,
    altitude_coef: float = DEFAULT_ALTITUDE_COEF,
    threshold_m: float = ALTITUDE_THRESHOLD_M,
) -> float:
    """Extra host points from altitude, taxing a lowland visitor.

    Returns ``altitude_coef * max(0, (venue - visitor_home) - threshold)``. Zero
    when the visitor is already used to comparable altitude, or the venue is low.
    """
    gap = (venue_altitude_m - visitor_home_altitude_m) - threshold_m
    if gap <= 0:
        return 0.0
    return altitude_coef * gap


def host_advantage_points(
    base_home_advantage: float,
    *,
    factor: float = DEFAULT_HOST_FACTOR,
    venue_altitude_m: Optional[float] = None,
    visitor_home_altitude_m: Optional[float] = None,
    altitude_coef: float = DEFAULT_ALTITUDE_COEF,
) -> float:
    """Venue/geography-aware host bonus, in Elo points.

    ``base_home_advantage * factor`` is the (diluted) crowd/familiarity bonus;
    an altitude term is added when both altitudes are supplied. With the default
    ``factor=1.0`` and no altitudes this returns ``base_home_advantage`` exactly
    — i.e. the legacy behaviour — so the venue-aware path is strictly opt-in.
    """
    pts = base_home_advantage * factor
    if venue_altitude_m is not None and visitor_home_altitude_m is not None:
        pts += altitude_penalty_points(
            venue_altitude_m,
            visitor_home_altitude_m,
            altitude_coef=altitude_coef,
        )
    return pts
