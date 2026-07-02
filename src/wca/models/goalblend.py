"""Opponent-adjusted, two-timescale Dixon-Coles goal blend (F7).

This module implements the goal-model redesign specified in
``docs/research/wca_alpha_2026/INTEGRITY_AUDIT_AND_GOALMODEL.md`` (Part 2).

Motivation
----------
The naive "average goals so far" intuition fails in knockouts: matchups never
repeat and a team's raw goals-for bakes in *who it already played*. Dixon-Coles
solves opponent-difficulty adjustment structurally — it fits attack/defence
jointly by netting out the opponent's defence in the likelihood
(:meth:`DixonColesModel.expected_lambdas`). We exploit that here by blending two
DC fits on the SAME played history:

1. a **LONG-term** DC (large half-life, the deployed fit) — out-of-sample
   dominated by tens of thousands of historical matches; and
2. a **TOURN-decayed** DC (short half-life) — up-weights the current tournament
   and recent internationals while the *same machinery* still nets out opponent
   defence.

Per team, the attack/defence parameters are convex-blended by a **credibility
weight** ``w_t = n_t / (n_t + k)`` where ``n_t`` is the team's recent
(decay-relevant) match count and ``k`` is a shrinkage constant. Few recent
matches => low ``w_t`` (trust LONG); deep into the tournament => higher ``w_t``.
This is the classic James-Stein / credibility form and reuses the convex-blend
pattern already in :class:`wca.card.BlendWeights`.

The blended attack/defence are grafted onto the LONG model's intercept
(``mu`` / ``rho`` / ``home_advantage``), then the level is re-anchored to the
WC slate via :meth:`DixonColesModel.recalibrate_level` so the goal *level* stays
fixed and only the *opponent-adjusted shape* moves.

Squad adjustment — HONEST DATA LIMIT
------------------------------------
A per-team squad-quality nudge is *hooked* (:func:`squad_log_rate_adjustment`)
but **data-gated**: no calibrated squad-strength feed exists
(``data/squads.json`` covers 2 of 48 teams; ``data/players.json`` records are
all ``source=analyst_estimate`` within-team xG *shares*, not team aggregates).
Per the no-fabrication rule the default is **no squad adjustment** (the hook
returns all-zero nudges) and we say so. Fabricating per-team squad strengths
from analyst-estimate shares and feeding them into EV/sizing is the HIGH-risk
error the directive forbids — explicitly NOT done.

Status: TRACKING-ONLY / OOS-gated. This model does not auto-size real money; it
is a parallel view for later CLV validation. The shrinkage knobs
(``short_half_life_years``, ``credibility_k``) are chosen design defaults, not
yet validated on multi-tournament holdout — see the OOS plan in the design doc.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from wca.models.dixon_coles import (
    DixonColesModel,
    decay_weights,
    xi_from_half_life,
)

# ---------------------------------------------------------------------------
# Design defaults (chosen knobs, NOT yet OOS-validated — see design doc §OOS).
# ---------------------------------------------------------------------------

#: Short DC half-life (years) for the tournament-decay component. ~6 months
#: up-weights the current tournament + recent internationals.
DEFAULT_SHORT_HALF_LIFE_YEARS: float = 0.5

#: Credibility shrinkage constant ``k`` in ``w_t = n_t / (n_t + k)``. The design
#: doc's conservative anti-overfit default: ~k matches reach ``w_t = 0.5``. A
#: team needs ``k`` recent matches before the tournament-decay component carries
#: half the weight; otherwise the LONG fit dominates. Deliberately large because
#: a single World Cup is ~1 cluster and ``k`` is weakly identified from it alone.
DEFAULT_CREDIBILITY_K: float = 10.0


def credibility_weight(n_recent: float, k: float = DEFAULT_CREDIBILITY_K) -> float:
    """Credibility weight ``w = n / (n + k)`` on the tournament-decay component.

    Monotone increasing in ``n_recent`` (more recent matches => trust the
    short-memory fit more) and decreasing in ``k`` (larger ``k`` => more
    shrinkage toward the LONG fit). Always in ``[0, 1)``: ``w(0) = 0`` (pure
    LONG when a team has no recent matches) and ``w -> 1`` as ``n -> inf``.

    Parameters
    ----------
    n_recent:
        Non-negative recent / decay-relevant match count for the team.
    k:
        Positive shrinkage constant.
    """
    n = float(n_recent)
    k = float(k)
    if k <= 0:
        raise ValueError("credibility_k must be positive")
    if n < 0:
        raise ValueError("n_recent must be non-negative")
    if n == 0:
        return 0.0
    return n / (n + k)


def squad_log_rate_adjustment(
    teams: Sequence[str],
    *,
    squad_strength: Optional[Mapping[str, float]] = None,
) -> Dict[str, float]:
    """Per-team additive log-rate squad nudge — DATA-GATED, default no-op.

    Returns ``{team: delta}`` where ``delta`` is an additive nudge (DC log-goal
    units) to be added to a team's blended attack. The HONEST default is **all
    zeros** (no adjustment): there is no calibrated per-team squad-strength feed
    (``squads.json`` = 2 teams attribution-only; ``players.json`` = analyst
    estimates of within-team xG *shares*, not team aggregates). Fabricating squad
    strengths and feeding them into EV/sizing is forbidden.

    When (and only when) a caller supplies a real ``squad_strength`` mapping
    (a future live feed), it is consumed here as a mean-zero re-centred nudge so
    identifiability is preserved. Absent that, every team gets ``0.0``.

    Parameters
    ----------
    teams:
        Teams to return a nudge for.
    squad_strength:
        Optional real squad-strength index ``{team: z}``. Default ``None`` =>
        no fabrication => all-zero nudges (the documented fallback).
    """
    if not squad_strength:
        return {t: 0.0 for t in teams}
    # A real feed was supplied: re-centre mean-zero over the requested teams so
    # the squad nudge does not shift the overall goal level (identifiability).
    vals = np.array([float(squad_strength.get(t, 0.0)) for t in teams], dtype=float)
    vals = vals - vals.mean()
    return {t: float(vals[i]) for i, t in enumerate(teams)}


@dataclass
class GoalBlendConfig:
    """Configuration for the two-timescale goal blend (all defaults documented)."""

    short_half_life_years: float = DEFAULT_SHORT_HALF_LIFE_YEARS
    credibility_k: float = DEFAULT_CREDIBILITY_K
    #: Re-anchor the blended level to this WC-slate target after blending. When
    #: ``None`` the long model's intercept is kept as-is (no level move).
    level_target: Optional[float] = None
    #: Optional real squad-strength feed; default ``None`` => no squad adjustment.
    squad_strength: Optional[Mapping[str, float]] = None

    def __post_init__(self) -> None:
        if self.short_half_life_years <= 0:
            raise ValueError("short_half_life_years must be positive")
        if self.credibility_k <= 0:
            raise ValueError("credibility_k must be positive")


@dataclass
class GoalBlendModel:
    """A blended Dixon-Coles model plus its provenance.

    ``blended`` is a ready-to-query :class:`DixonColesModel` (use
    :meth:`DixonColesModel.expected_lambdas` / ``score_matrix`` exactly like the
    long model). ``weights`` records the per-team credibility weight actually
    applied, for transparency / the tracking artifact.
    """

    blended: DixonColesModel
    long: DixonColesModel
    short: DixonColesModel
    weights: Dict[str, float] = field(default_factory=dict)
    config: GoalBlendConfig = field(default_factory=GoalBlendConfig)
    squad_adjusted: bool = False

    def expected_lambdas(
        self, home: str, away: str, neutral: bool = False, warn: bool = False
    ) -> Tuple[float, float]:
        """Opponent-adjusted ``(lambda_home, lambda_away)`` from the blended fit."""
        return self.blended.expected_lambdas(home, away, neutral=neutral, warn=warn)


def decay_weighted_counts(
    played: "object",
    *,
    half_life_years: float,
    reference_date: Optional[str] = None,
) -> Dict[str, float]:
    """Per-team **decay-weighted** recent match count (effective sample size).

    This is the ``n_t`` the design intends for ``w_t = n_t / (n_t + k)``: each of
    a team's matches contributes its time-decay weight
    ``exp(-xi * days_ago / 365.25)`` (the SAME weighting the short-HL DC fit
    uses), so a team whose only matches are old contributes near-zero ``n_t`` and
    is shrunk toward the LONG fit, while a team active in the current tournament
    accumulates ``n_t`` and earns credibility on the short fit. An *undecayed*
    raw count would saturate ``w_t`` for every established team and defeat the
    shrinkage, so we use the decay-weighted ESS.

    Falls back gracefully: rows without a parseable ``date`` are skipped.
    """
    import pandas as pd  # local import; pandas is a package dependency

    xi = xi_from_half_life(half_life_years)
    dates = pd.to_datetime(played["date"], errors="coerce")
    ref = (
        pd.to_datetime(reference_date)
        if reference_date is not None
        else dates.max()
    )
    days_ago = (ref - dates).dt.total_seconds().to_numpy() / 86400.0
    w = decay_weights(np.nan_to_num(days_ago, nan=1e9), xi)

    counts: Dict[str, float] = {}
    homes = played["home_team"].tolist()
    aways = played["away_team"].tolist()
    for i in range(len(homes)):
        if not np.isfinite(days_ago[i]):
            continue
        wi = float(w[i])
        counts[homes[i]] = counts.get(homes[i], 0.0) + wi
        counts[aways[i]] = counts.get(aways[i], 0.0) + wi
    return counts


def build_goal_blend(
    long_model: DixonColesModel,
    played: "object",
    *,
    reference_date: Optional[str] = None,
    config: Optional[GoalBlendConfig] = None,
) -> GoalBlendModel:
    """Build the two-timescale goal blend from an already-fitted LONG DC.

    Parameters
    ----------
    long_model:
        The deployed (long half-life) :class:`DixonColesModel`, already fitted.
        Used as-is for the LONG component and as the donor of the intercept
        (``mu`` / ``rho`` / ``home_advantage``) for the blended model.
    played:
        The played-results ``DataFrame`` (already filtered, e.g. via
        ``wca.card._played``) used to fit the short-HL component. Must carry the
        same columns ``fit_dataframe`` expects.
    reference_date:
        Reference date for the short-HL decay weighting (typically the slate
        date). Passed straight through to ``fit_dataframe``.
    config:
        :class:`GoalBlendConfig`; defaults documented above.

    Returns
    -------
    GoalBlendModel
    """
    cfg = config or GoalBlendConfig()

    # -- TOURN-decayed (short half-life) DC on the SAME played history. -------
    # Reuse the long model's structural priors / regularisation so the only
    # deliberate difference is the decay rate.
    short = DixonColesModel(
        xi=xi_from_half_life(cfg.short_half_life_years),
        reg_lambda=long_model.reg_lambda,
        min_matches=long_model.min_matches,
        low_data_reg_multiplier=long_model.low_data_reg_multiplier,
        max_goals=long_model.max_goals,
        attack_prior=long_model.attack_prior or None,
        defence_prior=long_model.defence_prior or None,
    )
    short.fit_dataframe(played, reference_date=reference_date)

    # Decay-weighted recent-match ESS per team (the design's ``n_t``). Computed
    # at the SHORT half-life so it tracks the short fit's effective weighting.
    n_recent = decay_weighted_counts(
        played,
        half_life_years=cfg.short_half_life_years,
        reference_date=reference_date,
    )

    # -- Per-team credibility-weighted convex blend of attack/defence. -------
    blended = copy.deepcopy(long_model)
    common = set(long_model.attack) & set(short.attack)
    weights: Dict[str, float] = {}
    squad_nudge = squad_log_rate_adjustment(
        sorted(long_model.attack), squad_strength=cfg.squad_strength
    )
    squad_adjusted = bool(cfg.squad_strength)

    for t in long_model.attack:
        if t in common:
            w = credibility_weight(n_recent.get(t, 0.0), cfg.credibility_k)
        else:
            w = 0.0  # no short-fit info for this team => pure LONG
        weights[t] = w
        atk_long = long_model.attack[t]
        dfc_long = long_model.defence[t]
        atk_short = short.attack.get(t, atk_long)
        dfc_short = short.defence.get(t, dfc_long)
        # Squad nudge (default 0.0) added to attack only, as a log-rate bump.
        blended.attack[t] = (1.0 - w) * atk_long + w * atk_short + squad_nudge.get(t, 0.0)
        blended.defence[t] = (1.0 - w) * dfc_long + w * dfc_short

    # -- Re-centre mean-zero (identifiability) over the blended team set. -----
    if blended.attack:
        amean = float(np.mean(list(blended.attack.values())))
        dmean = float(np.mean(list(blended.defence.values())))
        for t in blended.attack:
            blended.attack[t] -= amean
        for t in blended.defence:
            blended.defence[t] -= dmean

    # -- Re-anchor the level to the WC slate if requested. -------------------
    # The blend grafts short-HL shape onto the long intercept, which can drift
    # the level; re-anchoring fixes it so totals stay calibrated.
    if cfg.level_target is not None:
        from wca.card import apply_wc_level_anchor

        apply_wc_level_anchor(blended, cfg.level_target)

    return GoalBlendModel(
        blended=blended,
        long=long_model,
        short=short,
        weights=weights,
        config=cfg,
        squad_adjusted=squad_adjusted,
    )


# ---------------------------------------------------------------------------
# Tracking artifact: per-fixture long-DC vs blend lambdas (TRACKING-ONLY).
# ---------------------------------------------------------------------------


def fixture_contrast_rows(
    blend: GoalBlendModel,
    fixtures: Sequence[Tuple[str, str, bool]],
    *,
    elo_ratings: Optional[Mapping[str, float]] = None,
) -> List[Dict[str, object]]:
    """Per-fixture long-DC vs blend lambdas for the tracking artifact.

    ``fixtures`` is a sequence of ``(home, away, neutral)``. Each row carries the
    long-only lambdas, the blended lambdas, the credibility weights used, and the
    opponent ELO context (if ``elo_ratings`` is supplied) — clearly labelled
    tracking-only by the writer.
    """
    rows: List[Dict[str, object]] = []
    for home, away, neutral in fixtures:
        la_l, lb_l = blend.long.expected_lambdas(home, away, neutral=neutral, warn=False)
        la_b, lb_b = blend.blended.expected_lambdas(home, away, neutral=neutral, warn=False)
        row: Dict[str, object] = {
            "home": home,
            "away": away,
            "neutral": bool(neutral),
            "lam_home_longDC": round(la_l, 4),
            "lam_away_longDC": round(lb_l, 4),
            "lam_home_blend": round(la_b, 4),
            "lam_away_blend": round(lb_b, 4),
            "total_longDC": round(la_l + lb_l, 4),
            "total_blend": round(la_b + lb_b, 4),
            "w_home": round(blend.weights.get(home, 0.0), 4),
            "w_away": round(blend.weights.get(away, 0.0), 4),
        }
        if elo_ratings is not None:
            row["elo_home"] = round(float(elo_ratings.get(home, float("nan"))), 1)
            row["elo_away"] = round(float(elo_ratings.get(away, float("nan"))), 1)
        rows.append(row)
    return rows


#: Header for the tracking artifact, including the squad-data caveat.
TRACKING_ARTIFACT_NOTE = (
    "TRACKING-ONLY / OOS-gated goal blend (F7). Long-DC vs two-timescale blend "
    "per-fixture lambdas for later out-of-sample CLV validation. NOT used for "
    "staking. Squad adjustment is DATA-GATED (no calibrated squad feed) => no "
    "squad nudge applied (fallback). Shrinkage knobs are chosen defaults, not "
    "yet validated on multi-tournament holdout."
)


def write_tracking_artifact(
    path: "object",
    blend: GoalBlendModel,
    fixtures: Sequence[Tuple[str, str, bool]],
    *,
    elo_ratings: Optional[Mapping[str, float]] = None,
    reference_date: Optional[str] = None,
) -> str:
    """Write the per-fixture long-vs-blend tracking CSV. Returns the path.

    The file is clearly labelled tracking-only (header comment lines prefixed
    ``#``). It is NOT consumed by any staking path.
    """
    import csv
    import os

    rows = fixture_contrast_rows(blend, fixtures, elo_ratings=elo_ratings)
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    fieldnames = [
        "home", "away", "neutral",
        "lam_home_longDC", "lam_away_longDC",
        "lam_home_blend", "lam_away_blend",
        "total_longDC", "total_blend",
        "w_home", "w_away",
    ]
    if elo_ratings is not None:
        fieldnames += ["elo_home", "elo_away"]

    with open(path, "w", newline="") as fh:
        fh.write("# " + TRACKING_ARTIFACT_NOTE + "\n")
        fh.write(
            "# short_half_life_years=%s credibility_k=%s level_target=%s "
            "squad_adjusted=%s reference_date=%s\n"
            % (
                blend.config.short_half_life_years,
                blend.config.credibility_k,
                blend.config.level_target,
                blend.squad_adjusted,
                reference_date,
            )
        )
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path
