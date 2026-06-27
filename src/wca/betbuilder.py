"""Same-game *bet builder* construction with correlation-aware pricing.

A "bet builder" (bet365's term; "same-game multi" elsewhere) combines several
selections from a **single match** into one bet. This is fundamentally different
from :mod:`wca.accas`, which combines one leg from each of several *different*
fixtures. Cross-fixture legs are (to first order) independent, so an acca price
is the product of the leg prices. Same-game legs are **correlated** — "Brazil to
win" and "over 1.5 goals" tend to happen together; "Brazil win" and "both teams
to score" pull in opposite directions — so multiplying the individual leg odds
is simply the wrong price. bet365 itself prices builders off a joint model and
the naive multiply is only an upper bound on a positively-correlated builder.

This module prices builders the honest way: off the **reconciled Dixon-Coles
score matrix** that the rest of the platform already bets against
(:mod:`wca.models.scores`). Every leg is a region (boolean mask) of that matrix,
the joint probability of a builder is the mass of the intersection of its legs,
and the fair price is ``1 / joint_probability``. Because all legs read from one
matrix the builder price is automatically consistent with the headline 1X2,
over/under and BTTS numbers on the card.

The matrix is reconstructed from the published scores feed
(``site/scores_data.json``) without needing live odds or a model re-fit: we
calibrate a pair of Poisson goal means ``(lambda_home, lambda_away)`` so that an
independent-Poisson matrix, once reconciled to the feed's ``model_1x2``,
reproduces the feed's published over/under and BTTS as closely as possible
(:func:`calibrate_lambdas`). The within-region scoreline shape is then the
platform's own independent-Poisson prior — the same prior
:func:`wca.models.scores.reconcile_scoreline_matrix` uses for degenerate
regions — so nothing arbitrary is introduced.

``min_odds`` defaults to ``2.0`` (EVS / evens / 1-1): the builder search returns
the *most likely* combination whose correlation-aware fair price still clears
that floor. That matches the common framing "give me a bet builder at minimum
evens" and bet365's own bet-builder promotions, which require the builder to be
priced at evens or greater to qualify.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .models.scores import (
    btts_from_matrix,
    implied_1x2,
    over_under_from_matrix,
    reconcile_scoreline_matrix,
)

# Evens (EVS / 1-1) as decimal odds — the default builder floor.
EVS = 2.0

# Default scoreline grid. 10x10 covers >99.99% of realistic football totals.
DEFAULT_MAX_GOALS = 10


# ---------------------------------------------------------------------------
# Matrix reconstruction from the published scores feed.
# ---------------------------------------------------------------------------


def _poisson_pmf(lam: float, n: int) -> np.ndarray:
    """Truncated Poisson pmf ``p(0..n-1)`` via the stable ``p(k)=p(k-1)*lam/k``."""
    lam = max(float(lam), 0.0)
    out = np.empty(int(n), dtype=float)
    out[0] = np.exp(-lam)
    for k in range(1, int(n)):
        out[k] = out[k - 1] * lam / k
    return out


def independent_poisson_matrix(
    lambda_home: float, lambda_away: float, max_goals: int = DEFAULT_MAX_GOALS
) -> np.ndarray:
    """Normalised independent-Poisson score matrix ``P[h, a]`` of shape (n, n)."""
    n = int(max_goals) + 1
    ph = _poisson_pmf(lambda_home, n)
    pa = _poisson_pmf(lambda_away, n)
    mat = np.outer(ph, pa)
    total = mat.sum()
    return mat / total if total > 0 else mat


def _reconciled(
    lambda_home: float,
    lambda_away: float,
    target_1x2: Tuple[float, float, float],
    max_goals: int,
) -> np.ndarray:
    """Independent-Poisson matrix reconciled to ``target_1x2``."""
    base = independent_poisson_matrix(lambda_home, lambda_away, max_goals)
    return reconcile_scoreline_matrix(
        base, target_1x2, lambdas=(float(lambda_home), float(lambda_away))
    )


def calibrate_lambdas(
    model_1x2: Tuple[float, float, float],
    *,
    ou_line: Optional[float] = None,
    ou_over: Optional[float] = None,
    btts_yes: Optional[float] = None,
    max_goals: int = DEFAULT_MAX_GOALS,
    coarse: int = 41,
    refine: int = 21,
) -> Tuple[float, float]:
    """Goal means ``(lambda_home, lambda_away)`` matching the feed's aggregates.

    Searches ``(lambda_home, lambda_away)`` so that an independent-Poisson matrix
    reconciled to ``model_1x2`` reproduces the published over/under and BTTS. The
    objective is the squared error of the reconciled matrix's ``P(over ou_line)``
    and ``P(BTTS yes)`` versus the supplied targets; targets left ``None`` are
    dropped from the objective. The 1X2 is matched *exactly* by reconciliation
    regardless of the lambdas, so the lambdas are free to fit the second-moment
    information (how goals split into totals and who-scores).

    A deterministic coarse-to-fine grid search is used (no RNG, fully
    reproducible) over ``lambda in [0.05, 5.0]`` for each side. With no over/under
    or BTTS target the problem is under-determined; we then fall back to the goal
    means implied by the reconciled matrix of a neutral ``(1.3, 1.3)`` seed.
    """
    targets: List[Tuple[Callable[[np.ndarray], float], float]] = []
    if ou_line is not None and ou_over is not None:
        line = float(ou_line)
        tgt = float(ou_over)
        tgt = tgt / 100.0 if tgt > 1.0 else tgt
        targets.append((lambda m: over_under_from_matrix(m, line)[0], tgt))
    if btts_yes is not None:
        tgt = float(btts_yes)
        tgt = tgt / 100.0 if tgt > 1.0 else tgt
        targets.append((btts_from_matrix, tgt))

    if not targets:
        # Under-determined: just report the implied means of a neutral reconcile.
        m = _reconciled(1.3, 1.3, model_1x2, max_goals)
        return _matrix_means(m)

    def loss(lam_h: float, lam_a: float) -> float:
        m = _reconciled(lam_h, lam_a, model_1x2, max_goals)
        return float(sum((fn(m) - tgt) ** 2 for fn, tgt in targets))

    def grid_search(
        lo_h: float, hi_h: float, lo_a: float, hi_a: float, n: int
    ) -> Tuple[float, float]:
        hs = np.linspace(lo_h, hi_h, n)
        as_ = np.linspace(lo_a, hi_a, n)
        best = (hs[0], as_[0])
        best_loss = float("inf")
        for lh in hs:
            for la in as_:
                lv = loss(float(lh), float(la))
                if lv < best_loss:
                    best_loss = lv
                    best = (float(lh), float(la))
        return best

    lo, hi = 0.05, 5.0
    bh, ba = grid_search(lo, hi, lo, hi, coarse)
    # Refine in a window around the coarse optimum.
    step = (hi - lo) / (coarse - 1)
    bh, ba = grid_search(
        max(lo, bh - step), min(hi, bh + step),
        max(lo, ba - step), min(hi, ba + step),
        refine,
    )
    return bh, ba


def _matrix_means(matrix: np.ndarray) -> Tuple[float, float]:
    """Expected ``(home_goals, away_goals)`` of a score matrix."""
    m = np.asarray(matrix, dtype=float)
    rows = np.arange(m.shape[0])
    cols = np.arange(m.shape[1])
    return float((rows * m.sum(axis=1)).sum()), float((cols * m.sum(axis=0)).sum())


def parse_fixture(fixture: str) -> Tuple[str, str]:
    """Split ``"Home vs Away"`` (or ``"Home v Away"``) into ``(home, away)``."""
    parts = re.split(r"\s+vs?\s+", str(fixture).strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError("fixture must be 'Home vs Away', got %r" % (fixture,))
    return parts[0].strip(), parts[1].strip()


@dataclass
class FixtureMatrix:
    """A reconciled score matrix for one fixture plus its labels and aggregates."""

    home: str
    away: str
    matrix: np.ndarray
    lambda_home: float
    lambda_away: float

    @property
    def one_x_two(self) -> Tuple[float, float, float]:
        return implied_1x2(self.matrix)


def matrix_from_feed_entry(
    entry: Dict[str, Any], *, max_goals: int = DEFAULT_MAX_GOALS
) -> FixtureMatrix:
    """Build a :class:`FixtureMatrix` from one ``scores_data.json`` fixture dict."""
    home, away = parse_fixture(entry.get("fixture", ""))
    m1x2 = entry.get("model_1x2") or {}
    target = (
        float(m1x2.get("home") or 0.0),
        float(m1x2.get("draw") or 0.0),
        float(m1x2.get("away") or 0.0),
    )
    if sum(target) <= 0:
        raise ValueError("fixture %r has no usable model_1x2" % (entry.get("fixture"),))

    ou = entry.get("over_under") or {}
    btts = entry.get("btts")
    lam_h, lam_a = calibrate_lambdas(
        target,
        ou_line=ou.get("line"),
        ou_over=ou.get("over"),
        btts_yes=btts,
        max_goals=max_goals,
    )
    matrix = _reconciled(lam_h, lam_a, target, max_goals)
    return FixtureMatrix(
        home=home, away=away, matrix=matrix, lambda_home=lam_h, lambda_away=lam_a
    )


def matrix_from_models(
    models: Any,
    home: str,
    away: str,
    *,
    prob_fn: Optional[Callable[[str, str, bool], Tuple[float, float, float]]] = None,
    neutral: bool = True,
    max_goals: int = DEFAULT_MAX_GOALS,
) -> FixtureMatrix:
    """Build a :class:`FixtureMatrix` directly from fitted Elo + Dixon-Coles models.

    Used for fixtures that are not (yet) in the published scores feed — e.g.
    later group games. The Dixon-Coles score matrix for the fixture is reconciled
    to the same Elo/DC/market blend the card pipeline uses (via
    :func:`wca.advancement.make_prob_fn`, market-free for unpriced fixtures), so
    the result is consistent with how the rest of the platform would price the
    match and is *more* faithful than the feed-aggregate calibration (it uses the
    model's real scoreline shape rather than reconstructing one from O/U + BTTS).

    Parameters
    ----------
    models:
        A :class:`wca.card.FittedModels` (Elo rater + outcome model + DC model).
    home, away:
        Team labels; ``home`` is the nominal home side for the blend.
    prob_fn:
        Optional ``prob_fn(home, away, knockout) -> (p_home, p_draw, p_away)``.
        Defaults to :func:`wca.advancement.make_prob_fn` over ``models``.
    neutral:
        Whether the Dixon-Coles matrix is drawn at a neutral venue (default
        ``True`` — all 2026 group games bar host fixtures are neutral).
    """
    from .models.scores import scoreline_card  # local import: keep base deps light

    if prob_fn is None:
        from .advancement import make_prob_fn

        prob_fn = make_prob_fn(models)

    blended = prob_fn(home, away, False)
    pred = models.dc.predict(home, away, neutral=neutral, max_goals=max_goals, warn=False)
    card = scoreline_card(pred, blended, home=home, away=away)
    return FixtureMatrix(
        home=home,
        away=away,
        matrix=card.matrix,
        lambda_home=float(getattr(pred, "lambda_home", 0.0)),
        lambda_away=float(getattr(pred, "lambda_away", 0.0)),
    )


def load_feed(path: str = "site/scores_data.json") -> List[Dict[str, Any]]:
    """Return the ``fixtures`` list from a scores feed, or ``[]`` on failure."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            feed = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(feed, dict):
        return []
    fixtures = feed.get("fixtures") or []
    return [f for f in fixtures if isinstance(f, dict)]


def find_fixture(
    fixtures: Sequence[Dict[str, Any]], query: str
) -> Optional[Dict[str, Any]]:
    """Find the feed entry whose fixture string matches ``query`` (loose).

    Matches if both team tokens of ``query`` appear in the fixture string
    (case-insensitive), so ``"scotland brazil"`` or ``"Scotland vs Brazil"`` both
    resolve the same fixture regardless of home/away order in the query.
    """
    q = query.lower()
    q_tokens = [t for t in re.split(r"\s+vs?\s+|\s+", q) if t]
    for fx in fixtures:
        name = str(fx.get("fixture", "")).lower()
        if not name:
            continue
        if name == q or all(tok in name for tok in q_tokens):
            return fx
    return None


# ---------------------------------------------------------------------------
# Leg catalog. A leg is a region (boolean mask) of the score matrix.
# ---------------------------------------------------------------------------


@dataclass
class Leg:
    """One bet-builder selection: a market, a label, and its matrix region.

    ``family`` groups mutually-exclusive markets (only one leg per family may
    appear in a builder — e.g. you cannot combine two total-goals lines).
    ``prob`` is the standalone probability (mask mass) and is filled in by
    :func:`enumerate_legs`.
    """

    market: str
    selection: str
    family: str
    mask: np.ndarray
    prob: float = 0.0

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.prob if self.prob > 0 else float("inf")

    @property
    def label(self) -> str:
        return "%s: %s" % (self.market, self.selection)


def _grids(shape: Tuple[int, int]):
    nh, na = shape
    H = np.arange(nh)[:, None] * np.ones((1, na), dtype=int)
    A = np.ones((nh, 1), dtype=int) * np.arange(na)[None, :]
    return H, A, H + A


def enumerate_legs(fm: FixtureMatrix, *, max_total_line: float = 4.5) -> List[Leg]:
    """All candidate same-game legs for a fixture, with standalone probabilities.

    Covers the markets a bet365 builder exposes that are computable from a
    full-time score matrix: match result, double chance, draw-no-bet, total
    goals over/under, both-teams-to-score, per-team totals, clean sheets and win
    to nil. Half-time / cards / corners / shots are *not* derivable from a
    goals-only matrix and are deliberately excluded.
    """
    m = fm.matrix
    H, A, T = _grids(m.shape)
    home, away = fm.home, fm.away
    legs: List[Leg] = []

    def add(market: str, selection: str, family: str, mask: np.ndarray) -> None:
        legs.append(Leg(market, selection, family, np.asarray(mask, dtype=bool)))

    # Match result (1X2).
    add("Match Result", "%s to win" % home, "result", H > A)
    add("Match Result", "Draw", "result", H == A)
    add("Match Result", "%s to win" % away, "result", H < A)

    # Double chance.
    add("Double Chance", "%s or Draw" % home, "result", H >= A)
    add("Double Chance", "%s or %s" % (home, away), "result", H != A)
    add("Double Chance", "Draw or %s" % away, "result", H <= A)

    # Draw no bet (push on draw modelled as the win region; informational).
    add("Draw No Bet", "%s" % home, "result", H > A)
    add("Draw No Bet", "%s" % away, "result", H < A)

    # Total goals over/under.
    line = 0.5
    while line <= float(max_total_line) + 1e-9:
        add("Total Goals", "Over %.1f" % line, "total", T > line)
        add("Total Goals", "Under %.1f" % line, "total", T < line)
        line += 1.0

    # Both teams to score.
    btts_yes = (H >= 1) & (A >= 1)
    add("Both Teams To Score", "Yes", "btts", btts_yes)
    add("Both Teams To Score", "No", "btts", ~btts_yes)

    # Per-team totals.
    for tl in (0.5, 1.5, 2.5):
        add("%s Total Goals" % home, "Over %.1f" % tl, "home_total", H > tl)
        add("%s Total Goals" % home, "Under %.1f" % tl, "home_total", H < tl)
        add("%s Total Goals" % away, "Over %.1f" % tl, "away_total", A > tl)
        add("%s Total Goals" % away, "Under %.1f" % tl, "away_total", A < tl)

    # Clean sheets.
    add("%s Clean Sheet" % home, "Yes", "home_cs", A == 0)
    add("%s Clean Sheet" % away, "Yes", "away_cs", H == 0)

    # Win to nil.
    add("%s Win to Nil" % home, "Yes", "home_wtn", (H > A) & (A == 0))
    add("%s Win to Nil" % away, "Yes", "away_wtn", (H < A) & (H == 0))

    # Drop legs whose region exactly duplicates an earlier one (e.g. "Draw No
    # Bet: Brazil" is the same region as "Match Result: Brazil to win"). Keep the
    # first occurrence so the more canonical market name wins.
    deduped: List[Leg] = []
    seen_masks: set = set()
    for leg in legs:
        key = leg.mask.tobytes()
        if key in seen_masks:
            continue
        seen_masks.add(key)
        deduped.append(leg)

    for leg in deduped:
        leg.prob = float(m[leg.mask].sum())
    return deduped


# ---------------------------------------------------------------------------
# Builder pricing and search.
# ---------------------------------------------------------------------------


@dataclass
class BetBuilder:
    """A priced same-game builder.

    ``joint_prob`` is the correlation-aware probability (mass of the intersection
    of all legs). ``fair_odds`` is ``1/joint_prob`` — the honest price.
    ``naive_odds`` multiplies the individual leg prices as if the legs were
    independent (what a correlation-blind multiply gives, and the shape of a
    book's pre-margin builder quote). ``correlation_ratio = naive_odds/fair_odds``
    is >1 when the legs are net positively correlated (the builder is more likely
    than independence implies, so the fair price is shorter than the naive
    multiply) and <1 when net negatively correlated.
    """

    legs: List[Leg]
    joint_prob: float

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.joint_prob if self.joint_prob > 0 else float("inf")

    @property
    def naive_odds(self) -> float:
        o = 1.0
        for leg in self.legs:
            if leg.prob <= 0:
                return float("inf")
            o *= 1.0 / leg.prob
        return o

    @property
    def correlation_ratio(self) -> float:
        fo = self.fair_odds
        return self.naive_odds / fo if fo not in (0.0, float("inf")) else float("nan")


def joint_prob(matrix: np.ndarray, legs: Sequence[Leg]) -> float:
    """Mass of the intersection of all leg regions — the builder's true chance."""
    if not legs:
        return 0.0
    mask = np.ones(matrix.shape, dtype=bool)
    for leg in legs:
        mask &= leg.mask
    return float(matrix[mask].sum())


def _is_minimal(matrix: np.ndarray, legs: Sequence[Leg], jp: float) -> bool:
    """True if every leg meaningfully tightens the builder (no redundant leg).

    A leg is redundant if dropping it leaves the joint probability unchanged —
    i.e. the rest of the builder already implies it (e.g. "Brazil win to nil"
    already implies "Brazil clean sheet"). Such builders are padding and bet365
    would not let you add a fully-implied leg, so we exclude them.
    """
    if len(legs) < 2:
        return True
    for i in range(len(legs)):
        rest = [legs[j] for j in range(len(legs)) if j != i]
        if abs(joint_prob(matrix, rest) - jp) <= 1e-12:
            return False
    return True


def build_bet_builder(
    fm: FixtureMatrix,
    *,
    min_odds: float = EVS,
    min_legs: int = 2,
    max_legs: int = 4,
    candidate_legs: Optional[Sequence[Leg]] = None,
    must_include: Optional[Sequence[str]] = None,
    top_n: int = 5,
) -> List[BetBuilder]:
    """Search same-game builders clearing ``min_odds``, best (most likely) first.

    Enumerates combinations of ``min_legs..max_legs`` legs drawn from distinct
    market families, prices each off the joint matrix, and keeps those whose
    correlation-aware ``fair_odds >= min_odds``. Results are ranked by descending
    ``joint_prob`` so the top builder is the *most probable* selection that still
    pays at least ``min_odds`` (EVS by default). Redundant (non-minimal) builders
    and impossible (zero-probability) combinations are dropped.

    ``must_include`` optionally restricts results to builders containing a leg
    whose label contains each given substring (case-insensitive) — e.g.
    ``["to win"]`` to force a match-result anchor.
    """
    legs = list(candidate_legs) if candidate_legs is not None else enumerate_legs(fm)
    # Only legs with a real, non-degenerate standalone chance are usable.
    legs = [lg for lg in legs if 0.0 < lg.prob < 1.0]
    m = fm.matrix
    must = [s.lower() for s in (must_include or [])]

    out: List[BetBuilder] = []
    for k in range(int(min_legs), int(max_legs) + 1):
        for combo in combinations(legs, k):
            families = [lg.family for lg in combo]
            if len(set(families)) != len(families):
                continue  # one leg per family
            if must and not all(
                any(s in lg.label.lower() for lg in combo) for s in must
            ):
                continue
            jp = joint_prob(m, combo)
            if jp <= 0.0:
                continue  # mutually exclusive legs — impossible builder
            fair = 1.0 / jp
            if fair < float(min_odds) - 1e-9:
                continue
            if not _is_minimal(m, combo, jp):
                continue
            out.append(BetBuilder(legs=list(combo), joint_prob=jp))

    # Most likely qualifying builder first; tie-break on fewer legs then odds.
    out.sort(key=lambda b: (-b.joint_prob, len(b.legs), b.fair_odds))
    # De-duplicate builders that share the same leg set in a different order.
    seen: set = set()
    unique: List[BetBuilder] = []
    for b in out:
        key = frozenset(lg.label for lg in b.legs)
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)
    return unique[: int(top_n)]


# ---------------------------------------------------------------------------
# Risk-aware sizing (quarter-Kelly off the ledger bankroll).
# ---------------------------------------------------------------------------


@dataclass
class SizedBuilder:
    """A builder priced against a real offered price and sized for the bankroll.

    ``offered_odds`` is the price actually available (e.g. bet365's live
    same-game-multi quote). Edge and stake are computed against the model's joint
    probability: a builder is only +EV — and therefore only staked — when the
    offered price exceeds the model-fair price (``offered_odds > builder.fair_odds``,
    equivalently ``edge > 0``). The stake is fractional-Kelly
    (:func:`wca.markets.kelly.stake`), so a non-positive edge stakes nothing.
    """

    builder: BetBuilder
    offered_odds: float
    edge: float
    kelly_fraction: float
    stake: float
    ev: float


def size_bet_builder(
    builder: BetBuilder,
    offered_odds: float,
    bankroll: float,
    *,
    kelly_fraction: float = 0.25,
    per_bet_cap: float = 0.05,
) -> SizedBuilder:
    """Fractional-Kelly stake for one builder at a real offered price.

    Uses the model's joint probability as the win probability and the supplied
    ``offered_odds`` as the price. Delegates the staking arithmetic to
    :func:`wca.markets.kelly.stake` (quarter-Kelly by default, hard-capped at
    ``per_bet_cap`` of bankroll); a non-positive edge yields a zero stake.
    """
    from .markets.kelly import stake as kelly_stake

    p = builder.joint_prob
    o = float(offered_odds)
    edge_val = p * o - 1.0 if o > 0 else -1.0
    s = kelly_stake(p, o, bankroll, fraction=kelly_fraction, cap=per_bet_cap) if o > 1.0 else 0.0
    return SizedBuilder(
        builder=builder,
        offered_odds=o,
        edge=edge_val,
        kelly_fraction=kelly_fraction,
        stake=s,
        ev=edge_val * s,
    )


def apply_slate_cap(
    sized: Sequence[SizedBuilder],
    bankroll: float,
    *,
    daily_exposure_cap: float = 0.05,
    existing_exposure: float = 0.0,
) -> List[SizedBuilder]:
    """Scale a same-slate set of stakes to respect the remaining exposure budget.

    The combined stake across the slate is capped at
    ``daily_exposure_cap * bankroll`` *minus* the ``existing_exposure`` already at
    risk from open bets in the ledger — so today's builders never push total
    exposure past the daily ceiling. Relative sizing is preserved
    (:func:`wca.markets.kelly.simultaneous_exposure_scale`). Returns new
    :class:`SizedBuilder` objects with rescaled ``stake`` and ``ev``.
    """
    from .markets.kelly import simultaneous_exposure_scale

    bank = float(bankroll)
    budget = max(0.0, float(daily_exposure_cap) * bank - float(existing_exposure))
    budget_fraction = (budget / bank) if bank > 0 else 0.0
    scaled = simultaneous_exposure_scale(
        [sb.stake for sb in sized], budget_fraction, bank
    )
    out: List[SizedBuilder] = []
    for sb, new_stake in zip(sized, scaled):
        ns = float(new_stake)
        out.append(
            SizedBuilder(
                builder=sb.builder,
                offered_odds=sb.offered_odds,
                edge=sb.edge,
                kelly_fraction=sb.kelly_fraction,
                stake=ns,
                ev=sb.edge * ns,
            )
        )
    return out


def format_bet_builder(
    fm: FixtureMatrix,
    builders: Sequence[BetBuilder],
    *,
    min_odds: float = EVS,
) -> str:
    """Human-readable (Telegram-style) bet-builder report."""
    title = "%s vs %s" % (fm.home, fm.away)
    if not builders:
        return (
            "🎰 *Bet Builder — %s*\nNo same-game builder clears %.2f (min odds) "
            "from the modelled markets." % (title, min_odds)
        )

    p_h, p_d, p_a = fm.one_x_two
    lines = [
        "🎰 *Bet Builder — %s*" % title,
        "_Min odds %.2f (EVS). Prices are correlation-aware model fair values "
        "from the reconciled score matrix — verify bet365's live builder price "
        "before placing._" % min_odds,
        "_Model 1X2: %s %.0f%% · Draw %.0f%% · %s %.0f%%_"
        % (fm.home, p_h * 100, p_d * 100, fm.away, p_a * 100),
        "",
    ]

    primary = builders[0]
    lines.append(
        "*Builder:* `%.2f` fair  (%.1f%% to land)"
        % (primary.fair_odds, primary.joint_prob * 100)
    )
    for i, leg in enumerate(primary.legs, 1):
        lines.append(
            "  %d. %s  @ `%.2f` (%.0f%%)"
            % (i, leg.label, leg.fair_odds, leg.prob * 100)
        )
    naive = primary.naive_odds
    ratio = primary.correlation_ratio
    if ratio > 1.01:
        corr = "net positively correlated (×%.2f), so the honest fair price " \
               "is shorter" % ratio
    elif ratio < 0.99:
        corr = "net negatively correlated (×%.2f), so the honest fair price " \
               "is longer" % ratio
    else:
        corr = "near-independent (×%.2f)" % ratio
    lines.append("")
    lines.append(
        "  _Naive multiply (legs as independent): `%.2f`. Legs are %s — model-fair "
        "price `%.2f`, not `%.2f`._" % (naive, corr, primary.fair_odds, naive)
    )

    if len(builders) > 1:
        lines.append("")
        lines.append("*Other qualifying builders:*")
        for b in builders[1:]:
            legtxt = " + ".join(lg.label for lg in b.legs)
            lines.append(
                "  – `%.2f` (%.1f%%): %s" % (b.fair_odds, b.joint_prob * 100, legtxt)
            )

    return "\n".join(lines)
