"""Socio-economic structural priors for international football.

This module is the project's adaptation of the socio-economic forecasting idea
popularised by Joachim Klement and grounded academically in

    Hoffmann, R., Ging, L. C. and Ramasamy, B. (2002). "The Socio-Economic
    Determinants of International Soccer Performance." Journal of Applied
    Economics, 5(2):253-272.

That literature shows a country's long-run footballing strength is partly
explained by structural variables — population, per-capita wealth (with a
strong *inverted-U*: returns diminish and then reverse past a peak), the
cultural importance of football, and confederation/region — explaining on the
order of half the cross-sectional variance.

Two deliberately narrow uses here
----------------------------------
1. **A prior, not a signal.** The structural strength index is converted into a
   *shrinkage prior* for the Dixon-Coles attack/defence parameters. For the many
   2026 minnows with only a handful of internationals, the likelihood is weak
   and the model otherwise shrinks them to the global mean (a poor prior for a
   genuine minnow). Shrinking instead toward a structural estimate is a
   strictly better-informed prior in exactly the low-data regime where it
   matters. It is *swamped* by the likelihood for data-rich teams.
2. **A divergence flag.** :func:`outright_divergence` compares the structural
   view of who *should* be strong against the model's (or the market's) outright
   probabilities, purely to surface long-shot teams whose data quality is worth
   a human look. It is never a stake trigger.

None of this touches liquid 1X2 pricing, where the de-vigged market already
subsumes structural information far more efficiently than five coarse variables.

The numbers in ``data/structural/country_factors.csv`` are approximate and
coarse on purpose: they parameterise a prior, not a point forecast.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Data location & loading.
# ---------------------------------------------------------------------------

#: Repository-root-relative default path to the curated factor table.
DEFAULT_FACTORS_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "structural" / "country_factors.csv"
)

#: Per-capita wealth (USD) at which the inverted-U peaks. Above this, additional
#: wealth carries no extra footballing return (and slightly negative on a log
#: scale). Klement cites diminishing returns around this level.
GDP_PEAK_USD = 60000.0

#: Confederation strength offsets (in raw-index units, added before z-scoring).
#: CONMEBOL/UEFA are the historically dominant regions per capita.
CONFEDERATION_OFFSET: Dict[str, float] = {
    "CONMEBOL": 0.55,
    "UEFA": 0.40,
    "CAF": -0.05,
    "CONCACAF": -0.20,
    "AFC": -0.30,
    "OFC": -0.65,
}

#: Weights on the (population, gdp) terms in the raw structural index.
_W_POP = 1.0
_W_GDP = 0.6

#: Default magnitude (in DC log-goal units) of the attack/defence prior at one
#: standard deviation of structural strength. Small: a gentle nudge, not a fact.
DEFAULT_PRIOR_SCALE = 0.15


@dataclass(frozen=True)
class CountryFactors:
    """Structural inputs for one national team."""

    team: str
    confederation: str
    population_m: float
    gdp_per_capita_usd: float
    football_culture: float
    home_altitude_m: float


def load_country_factors(
    path: Optional[Path] = None,
) -> Dict[str, CountryFactors]:
    """Load the curated structural factor table, keyed by canonical team name.

    Lines beginning with ``#`` are treated as comments and skipped. The header
    row names the columns; see ``data/structural/country_factors.csv``.
    """
    p = Path(path) if path is not None else DEFAULT_FACTORS_PATH
    rows: List[str] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.lstrip().startswith("#"):
                continue
            rows.append(line)
    reader = csv.DictReader(rows)
    out: Dict[str, CountryFactors] = {}
    for r in reader:
        team = r["team"].strip()
        out[team] = CountryFactors(
            team=team,
            confederation=r["confederation"].strip(),
            population_m=float(r["population_m"]),
            gdp_per_capita_usd=float(r["gdp_per_capita_usd"]),
            football_culture=float(r["football_culture"]),
            home_altitude_m=float(r["home_altitude_m"]),
        )
    return out


# ---------------------------------------------------------------------------
# The structural strength index.
# ---------------------------------------------------------------------------


def _population_term(f: CountryFactors) -> float:
    """Talent-pool term: log-population, *conditioned on football culture*.

    A large population only converts into footballing strength where football is
    the dominant sport (the Hoffmann/Klement population x culture interaction):
    India and the USA have huge populations but modest football culture.
    """
    pop = max(f.population_m, 1e-3)
    return math.log10(pop) * f.football_culture


def _gdp_term(f: CountryFactors) -> float:
    """Inverted-U in per-capita wealth, peaking at :data:`GDP_PEAK_USD`.

    Implemented as a downward parabola in log-wealth centred on the peak, so the
    term is 0 at the peak and negative on either side — capturing both "too poor
    to build football infrastructure" and "rich enough that other sports/leisure
    compete football away" (diminishing and then reversing returns).
    """
    gdp = max(f.gdp_per_capita_usd, 1.0)
    d = math.log(gdp / GDP_PEAK_USD)
    return -(d * d)


def _confederation_term(f: CountryFactors) -> float:
    return CONFEDERATION_OFFSET.get(f.confederation, 0.0)


def raw_strength(f: CountryFactors) -> float:
    """Un-standardised structural strength index for one team."""
    return (
        _W_POP * _population_term(f)
        + _W_GDP * _gdp_term(f)
        + _confederation_term(f)
    )


def strength_index(
    factors: Mapping[str, CountryFactors],
) -> Dict[str, float]:
    """Return a mean-zero, unit-variance structural strength per team.

    The raw index is z-scored across the supplied teams so the result is a
    dimensionless relative measure (positive = structurally stronger than the
    pool average). Mean-zero is required so it can serve as an identifiable
    shrinkage target for the mean-zero Dixon-Coles attack/defence vectors.
    """
    if not factors:
        return {}
    raw = {t: raw_strength(f) for t, f in factors.items()}
    vals = list(raw.values())
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var) if var > 0 else 1.0
    return {t: (v - mean) / sd for t, v in raw.items()}


# ---------------------------------------------------------------------------
# Dixon-Coles shrinkage priors.
# ---------------------------------------------------------------------------


def build_dc_priors(
    strength: Mapping[str, float],
    scale: float = DEFAULT_PRIOR_SCALE,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Map a structural strength index to Dixon-Coles attack/defence priors.

    A structurally stronger team is expected to both score more (higher attack)
    and concede less (higher defence parameter, which *reduces* the opponent's
    expected goals in the DC parameterisation), so both priors move with the
    same sign and magnitude ``scale * z``. ``scale`` is the prior strength at one
    standard deviation, in DC log-goal units; keep it small.
    """
    attack = {t: scale * z for t, z in strength.items()}
    defence = {t: scale * z for t, z in strength.items()}
    return attack, defence


def dc_priors_from_factors(
    path: Optional[Path] = None,
    scale: float = DEFAULT_PRIOR_SCALE,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Convenience: load the factor table and return ``(attack, defence)`` priors."""
    factors = load_country_factors(path)
    strength = strength_index(factors)
    return build_dc_priors(strength, scale=scale)


# ---------------------------------------------------------------------------
# P3: structural divergence flag (informational only).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """One team's structural-vs-model outright disagreement."""

    team: str
    model_prob: float
    structural_prob: float
    log_ratio: float  # ln(structural / model); >0 => structural is higher


def structural_outright_probs(
    strength: Mapping[str, float],
    temperature: float = 1.0,
) -> Dict[str, float]:
    """Softmax of structural strength into a crude outright-winner distribution.

    This is a toy distribution used only for the divergence flag: it expresses
    "if winning were driven purely by structural strength, who would it be?".
    ``temperature`` sharpens (>1) or flattens (<1) the distribution.
    """
    if not strength:
        return {}
    t = max(temperature, 1e-6)
    mx = max(strength.values())
    exps = {k: math.exp((v - mx) / t) for k, v in strength.items()}
    z = sum(exps.values())
    return {k: v / z for k, v in exps.items()}


def outright_divergence(
    strength: Mapping[str, float],
    model_probs: Mapping[str, float],
    temperature: float = 1.0,
    min_log_ratio: float = 0.5,
) -> List[Divergence]:
    """Flag teams where the structural view and the model disagree on outrights.

    Parameters
    ----------
    strength:
        Structural strength index (see :func:`strength_index`).
    model_probs:
        The model's (or market's) outright-win probability per team — e.g. the
        ``P(win)`` column from :func:`wca.advancement.run_advancement`.
    temperature:
        Softmax temperature for the structural distribution.
    min_log_ratio:
        Only divergences with ``|ln(structural/model)| >= min_log_ratio`` are
        returned, sorted by magnitude descending.

    Returns a list of :class:`Divergence`, never a stake. A large positive
    log-ratio means the structural prior likes a team the model is cold on —
    typically a long-shot worth a data-quality check, *not* a bet.
    """
    struct = structural_outright_probs(strength, temperature=temperature)
    eps = 1e-9
    out: List[Divergence] = []
    for team, sp in struct.items():
        mp = float(model_probs.get(team, 0.0))
        lr = math.log((sp + eps) / (mp + eps))
        if abs(lr) >= min_log_ratio:
            out.append(
                Divergence(
                    team=team,
                    model_prob=mp,
                    structural_prob=sp,
                    log_ratio=lr,
                )
            )
    out.sort(key=lambda d: abs(d.log_ratio), reverse=True)
    return out
