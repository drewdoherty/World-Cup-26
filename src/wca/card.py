"""Matchday card generator.

Ties the whole pipeline together for a slate of upcoming fixtures:

1.  Fit international Elo (rating + ordered-logit outcome model) and a
    time-decayed Dixon-Coles model on the full results history.
2.  For each fixture in an odds frame, de-vig every book's 1X2 prices with
    Shin and aggregate to a market-consensus fair probability (the baseline).
3.  Blend Elo, Dixon-Coles and the market baseline into a single probability
    per outcome.
4.  Take the *best* (max) decimal price available across books per outcome and
    compute the edge/EV of backing it at the blended probability.
5.  Size a quarter-Kelly stake per pool, capped per-bet, and emit a card.

Design notes
------------
The blend deliberately anchors a large weight on the de-vigged market because
the market is hard to beat; the model's job is to flag where the *best
available price* (after line-shopping across books) diverges enough from the
consensus to clear the vig. Blend weights are pre-backtest priors and are
documented as such — they are not yet fitted (that needs the calibration
backtest, deferred). Until then they are conservative on purpose.

This module produces *recommendations only*; nothing here places a bet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from wca.data.teamnames import canonical
from wca.markets import devig as devig_mod
from wca.markets import kelly as kelly_mod
from wca.models.dixon_coles import DixonColesModel
from wca.models.elo import EloOutcomeModel, EloRater
from wca.models.scores import ScorelineCard, scoreline_card

# 1X2 outcome order used throughout: home, draw, away.
OUTCOMES = ("home", "draw", "away")


@dataclass
class BlendWeights:
    """Convex weights over (Elo, Dixon-Coles, market). Must sum to 1.

    Defaults are **0.25 / 0.25 / 0.50** and were *kept* after the blend backtest
    (see ``backtests/`` and ``docs/research/backtests/``). The fitted blend does
    NOT beat the de-vigged market with confidence: delta mean -0.0031 nats,
    95% CI [-0.0224, +0.0155], P(fitted beats market)=60.2% on n=64. The
    deployed weights are statistically indistinguishable from both the fitted
    blend and market-only (all within ~0.003 nats, heavily overlapping bootstrap
    CIs), so there is no decision-grade reason to change them.

    There is a weak, directionally consistent signal that DC > Elo (Step 1 LOTO
    and Step 3 both favour DC; fitted w_elo=0.00, pooled relative optimum
    w_elo/(w_elo+w_dc)=0.15). If the desk wants to act on it, the single
    conservative move is ``BlendWeights(elo=0.10, dc=0.30, market=0.60)`` —
    shift weight from Elo to DC and nudge market up. Do NOT zero out Elo or
    adopt the raw single-tournament fit (0.00/0.32/0.68); that over-fits one
    World Cup. Re-fit after a second tournament with closing odds (WC2026 group
    stage) before any larger change.
    """

    elo: float = 0.25
    dc: float = 0.25
    market: float = 0.50

    def normalised(self) -> "BlendWeights":
        s = self.elo + self.dc + self.market
        if s <= 0:
            raise ValueError("blend weights must sum to a positive number")
        return BlendWeights(self.elo / s, self.dc / s, self.market / s)


@dataclass
class PoolConfig:
    """A bankroll pool with its own Kelly sizing parameters."""

    name: str
    bankroll: float
    currency: str = "GBP"
    kelly_fraction: float = 0.25
    per_bet_cap: float = 0.05
    daily_exposure_cap: float = 0.05


# ---------------------------------------------------------------------------
# CLV-gated bankroll ladder (governance wiring for the sportsbook pool).
# ---------------------------------------------------------------------------

# Notional sportsbook-pool bankroll for each rung of the pre-registered Kelly
# ladder (``wca.markets.kelly.KellyPolicy``). The ladder's rung *index* —
# earned by settled-with-close bet count AND positive to-date CLV — selects
# which notional pool the desk is cleared to deploy. This is the bankroll
# governance agreed with the user:
#
#   rung 0  ->  £1,000   (start; until 50 settled-with-close bets)
#   rung 1  ->  £2,500   (50+ settled AND to-date CLV > 0)
#   rung 2  ->  £5,000   (100+ settled AND to-date CLV > 0; ceiling)
#
# Demotion (rolling-50 CLV < 0) steps the index back down and so the bankroll
# down with it. The *kill rule* (pause real money if avg CLV < 0 after ~50
# bets) is the desk's jurisdiction, not encoded here — it is a pause, not a
# resize, and the ladder simply holds rung 0 in that regime.
LADDER_BANKROLLS: Tuple[float, ...] = (1000.0, 2500.0, 5000.0)


@dataclass
class PoolBankroll:
    """Resolved sportsbook-pool bankroll and the evidence that earned it.

    ``bankroll`` is the notional pool the ladder clears; ``kelly_fraction`` is
    the rung's fractional-Kelly multiplier (so callers can size with the *same*
    fraction the ladder authorises). ``reason`` is a one-line human-readable
    explanation suitable for the card header, e.g.
    ``"rung £1000 — 0/50 settled-with-close bets (CLV n/a)"``.
    """

    bankroll: float
    rung: int
    kelly_fraction: float
    reason: str
    n_settled: int
    clv_to_date: Optional[float]


def resolve_pool_bankroll(
    db_path: str,
    policy: Optional["kelly_mod.KellyPolicy"] = None,
    bankrolls: Sequence[float] = LADDER_BANKROLLS,
    override: Optional[float] = None,
    currency_symbol: str = "£",
) -> PoolBankroll:
    """Resolve the sportsbook-pool bankroll from ledger CLV via the Kelly ladder.

    Reads the settled-with-close CLV statistics from the ledger
    (``wca.ledger.reports.staking_stats``), runs the pre-registered
    :class:`~wca.markets.kelly.KellyPolicy` ladder to find the earned rung, and
    maps that rung index onto the governance bankroll ladder
    (:data:`LADDER_BANKROLLS`: £1000 / £2500 / £5000).

    The rung is *earned* by evidence, never by time or a hot streak: rung 1
    needs 50+ settled-with-close bets and positive to-date CLV; rung 2 needs
    100+ and positive CLV; a negative rolling-50 CLV demotes one rung. See the
    policy's docstring for the full rules.

    Parameters
    ----------
    db_path:
        SQLite ledger path.
    policy:
        The Kelly ladder to apply. Defaults to a fresh
        :class:`~wca.markets.kelly.KellyPolicy` (the pre-registered ladder).
    bankrolls:
        Notional pool per rung, index-aligned with ``policy.rungs``. Defaults
        to the governance ladder £1000 / £2500 / £5000.
    override:
        If not ``None``, use this bankroll verbatim (the ``--bankroll`` CLI
        override). The ledger is still read so the card can report the rung the
        evidence *would* have earned alongside the manual figure.
    currency_symbol:
        Symbol used in the ``reason`` string (display only).

    Returns
    -------
    PoolBankroll
        The resolved bankroll plus the rung, Kelly fraction and a one-line
        reason for the card header.
    """
    from wca.ledger.reports import staking_stats

    if policy is None:
        policy = kelly_mod.KellyPolicy()

    if len(bankrolls) != len(policy.rungs):
        raise ValueError(
            "bankrolls (%d) must align one-to-one with policy.rungs (%d)"
            % (len(bankrolls), len(policy.rungs))
        )

    stats = staking_stats(db_path)
    n_settled = int(stats["n_settled"])
    clv_to_date = stats["clv_to_date"]
    rolling50 = stats["rolling50_clv"]

    fraction, rung, _policy_reason = policy.evaluate(
        n_settled=n_settled,
        clv_to_date=clv_to_date,
        rolling50_clv=rolling50,
    )

    ladder_bankroll = float(bankrolls[rung])

    # Threshold of the *next* rung, for the "X/Y settled" progress hint.
    if rung + 1 < len(policy.rungs):
        next_threshold = policy.rungs[rung + 1].min_settled
    else:
        next_threshold = policy.rungs[rung].min_settled

    clv_str = ("%+.4f" % clv_to_date) if clv_to_date is not None else "n/a"

    if override is not None:
        bankroll = float(override)
        reason = (
            "%s%.0f (manual override) — ladder would set rung %d "
            "(%s%.0f) from %d/%d settled-with-close bets, CLV %s"
            % (
                currency_symbol, bankroll, rung, currency_symbol,
                ladder_bankroll, n_settled, next_threshold, clv_str,
            )
        )
    else:
        bankroll = ladder_bankroll
        reason = (
            "rung %d %s%.0f — %d/%d settled-with-close bets, CLV %s, "
            "Kelly fraction %.2f"
            % (
                rung, currency_symbol, bankroll, n_settled, next_threshold,
                clv_str, fraction,
            )
        )

    return PoolBankroll(
        bankroll=bankroll,
        rung=rung,
        kelly_fraction=fraction,
        reason=reason,
        n_settled=n_settled,
        clv_to_date=clv_to_date,
    )


@dataclass
class Recommendation:
    match_id: str
    match_desc: str
    commence_time: str
    selection: str  # home/draw/away
    selection_team: str
    best_book: str
    best_odds: float
    model_prob: float
    market_prob: float
    elo_prob: float
    dc_prob: float
    edge: float
    ev_per_unit: float
    stakes: Dict[str, float] = field(default_factory=dict)  # pool name -> stake


@dataclass
class FittedModels:
    rater: EloRater
    elo_outcome: EloOutcomeModel
    dc: DixonColesModel
    n_matches: int


# ---------------------------------------------------------------------------
# Model fitting.
# ---------------------------------------------------------------------------


def _played(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with real (non-NA) integer scores, sorted by date."""
    d = df.copy()
    for col in ("home_score", "away_score"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["home_score", "away_score"])
    d["home_score"] = d["home_score"].astype(int)
    d["away_score"] = d["away_score"].astype(int)
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["date"]).sort_values("date", kind="mergesort")
    if "neutral" in d.columns:
        d["neutral"] = d["neutral"].astype(bool)
    return d.reset_index(drop=True)


def fit_models(
    results: pd.DataFrame,
    half_life_years: float = 8.0,
    reference_date: Optional[str] = None,
) -> FittedModels:
    """Fit Elo (rating + outcome) and Dixon-Coles on the results history.

    Dixon-Coles half-life
    ---------------------
    ``half_life_years`` defaults to **8.0**, deliberately *kept* after the
    half-life backtest (see ``backtests/`` and ``docs/research/backtests/``).
    The evidence does not support moving it:

    * DC-only: the pooled best is hl=4 (log-loss 0.9773) but it beats the
      deployed hl=8 (0.9789) by only **+0.0016 log-loss** — not decision-grade
      on ~211 holdout matches. Only 2 of 3 holdouts favour 4 over 8
      (deployed-minus-best per block: WC2018 +0.0028, WC2022 -0.0143,
      Euro2024+Copa2024 +0.0130); WC2022 strongly prefers *longer* memory (16).
    * Blend (the 50/50 Elo+DC mix the card actually deploys): the pooled best
      *is* hl=8 (0.9817); hl=4 only ties (0.9824 vs 0.9817).

    8.0 is a sensible compromise between the divergent per-tournament optima
    (2-4 for European-summer tournaments vs 16 for the anomalous Qatar winter
    WC), not a value that should move. Revisit only with a larger holdout
    (more tournaments / club-data augmentation).
    """
    played = _played(results)

    # -- Elo ratings --------------------------------------------------------
    rater = EloRater()
    out = rater.rate_matches(played, return_history=True)
    history = out["history"]

    # -- Elo ordered-logit outcome model -----------------------------------
    # Reconstruct the pre-match rating diff (with home advantage on non-neutral
    # venues) and the realised ordinal outcome for every historical match.
    diffs: List[float] = []
    outcomes: List[int] = []
    hist_scores = played[["home_score", "away_score"]].to_numpy()
    for rec, (hs, as_) in zip(history, hist_scores):
        adv = 0.0 if rec["neutral"] else rater.home_advantage
        diff = (rec["home_rating_pre"] + adv) - rec["away_rating_pre"]
        diffs.append(diff)
        outcomes.append(2 if hs > as_ else (1 if hs == as_ else 0))
    elo_outcome = EloOutcomeModel().fit(diffs, outcomes)

    # -- Dixon-Coles --------------------------------------------------------
    from wca.models.dixon_coles import xi_from_half_life

    dc = DixonColesModel(xi=xi_from_half_life(half_life_years))
    dc.fit_dataframe(played, reference_date=reference_date)

    return FittedModels(rater=rater, elo_outcome=elo_outcome, dc=dc, n_matches=len(played))


# ---------------------------------------------------------------------------
# Per-fixture probabilities.
# ---------------------------------------------------------------------------


def elo_probs(
    models: FittedModels, home: str, away: str, neutral: bool, host: Optional[str] = None
) -> Tuple[float, float, float]:
    """Elo (home, draw, away) via the ordered-logit outcome model."""
    diff = models.rater._rating_diff(home, away, neutral=neutral, host=host)
    return models.elo_outcome.predict_proba(diff)


def dc_probs(
    models: FittedModels, home: str, away: str, neutral: bool
) -> Tuple[float, float, float]:
    """Dixon-Coles (home, draw, away) from the scoreline matrix."""
    pred = models.dc.predict(home, away, neutral=neutral, warn=False)
    return pred.one_x_two()


def market_consensus(book_prices: Dict[str, Dict[str, float]]) -> Optional[np.ndarray]:
    """De-vig each complete book with Shin, return median fair (home,draw,away).

    ``book_prices`` maps book -> {home,draw,away: decimal_odds}. Books missing
    any of the three outcomes are skipped. Returns ``None`` if no book has a
    complete 1X2.
    """
    fair_rows: List[np.ndarray] = []
    for prices in book_prices.values():
        if not all(o in prices and prices[o] > 1.0 for o in OUTCOMES):
            continue
        odds = [prices[o] for o in OUTCOMES]
        try:
            fair_rows.append(devig_mod.shin(odds))
        except Exception:
            continue
    if not fair_rows:
        return None
    arr = np.vstack(fair_rows)
    med = np.median(arr, axis=0)
    return med / med.sum()  # renormalise after the per-column median


# ---------------------------------------------------------------------------
# Card construction.
# ---------------------------------------------------------------------------


def _index_odds(odds_df: pd.DataFrame) -> Dict[str, Dict[str, object]]:
    """Group the flat odds frame into per-fixture h2h price books.

    Returns fixture_key -> {meta, books: {book: {home/draw/away: odds}}}.
    The Odds API h2h outcome names are the team names plus 'Draw'.
    """
    fixtures: Dict[str, Dict[str, object]] = {}
    h2h = odds_df[odds_df["market"] == "h2h"]
    for (eid, home, away, commence), grp in h2h.groupby(
        ["event_id", "home_team", "away_team", "commence_time"], sort=False
    ):
        books: Dict[str, Dict[str, float]] = {}
        for book, bgrp in grp.groupby("bookmaker_key"):
            prices: Dict[str, float] = {}
            for _, r in bgrp.iterrows():
                name = str(r["outcome_name"])
                odd = float(r["decimal_odds"])
                if name == home:
                    prices["home"] = odd
                elif name == away:
                    prices["away"] = odd
                elif name.lower() == "draw":
                    prices["draw"] = odd
            if prices:
                books[str(book)] = prices
        fixtures[str(eid)] = {
            "event_id": str(eid),
            "home": str(home),
            "away": str(away),
            "commence_time": str(commence),
            "books": books,
        }
    return fixtures


def best_price(books: Dict[str, Dict[str, float]], outcome: str) -> Tuple[Optional[str], float]:
    """Best (max) decimal odds for an outcome across books, with the book name."""
    best_book, best = None, 0.0
    for book, prices in books.items():
        o = prices.get(outcome)
        if o is not None and o > best:
            best, best_book = o, book
    return best_book, best


@dataclass
class _FixtureBlend:
    """Per-fixture blend state shared by the recommendation and score pipelines."""

    fx: Dict[str, object]
    home: str  # canonical
    away: str  # canonical
    neutral: bool
    host: Optional[str]
    books: Dict[str, Dict[str, float]]
    blended: Dict[str, float]  # home/draw/away
    elo_map: Dict[str, float]
    dc_map: Dict[str, float]
    mkt_map: Dict[str, float]


def _meta_lookup(
    fixtures_meta: Optional[pd.DataFrame],
) -> Dict[Tuple[str, str], Dict[str, object]]:
    """Build the neutral/host lookup keyed by canonical team pair.

    Prefer *unplayed* (scheduled) rows so a historical friendly between the same
    two teams can't overwrite the World Cup fixture's neutral/host flags.
    """
    meta_lookup: Dict[Tuple[str, str], Dict[str, object]] = {}
    if fixtures_meta is not None and not fixtures_meta.empty:
        fm = fixtures_meta.copy()
        if "home_score" in fm.columns:
            scores = pd.to_numeric(fm["home_score"], errors="coerce")
            fm = pd.concat([fm[scores.notna()], fm[scores.isna()]])  # played first, scheduled last (wins)
        for _, r in fm.iterrows():
            meta_lookup[(str(r["home_team"]), str(r["away_team"]))] = {
                "neutral": bool(r["neutral"]) if "neutral" in r else True,
                "country": str(r.get("country", "")),
            }
    return meta_lookup


def _iter_fixture_blends(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights,
    host_nations: Sequence[str],
) -> List[_FixtureBlend]:
    """Compute the blended 1X2 for every fixture with a usable market.

    Shared by :func:`build_card` and :func:`build_score_cards` so both pipelines
    bet against the *same* blended probabilities. Fixtures without a complete
    market consensus are skipped (no blend is well-defined).
    """
    w = weights.normalised()
    fixtures = _index_odds(odds_df)
    meta_lookup = _meta_lookup(fixtures_meta)

    out: List[_FixtureBlend] = []
    for fx in fixtures.values():
        # Display names come from the odds feed; model/meta lookups MUST use the
        # canonical results-dataset spelling or they fall back to default
        # ratings and emit garbage edges.
        home_disp, away_disp = fx["home"], fx["away"]
        home, away = canonical(home_disp), canonical(away_disp)
        books = fx["books"]  # type: ignore[assignment]

        meta = meta_lookup.get((home, away), {"neutral": True, "country": ""})
        neutral = bool(meta["neutral"])
        host = None
        country = str(meta.get("country", ""))
        if neutral and country in host_nations:
            if home in host_nations:
                host = home
            elif away in host_nations:
                host = away
        elif not neutral:
            host = home  # genuine home team

        e_h, e_d, e_a = elo_probs(models, home, away, neutral=neutral, host=host)
        d_h, d_d, d_a = dc_probs(models, home, away, neutral=neutral)
        mkt = market_consensus(books)  # type: ignore[arg-type]
        if mkt is None:
            continue
        m_h, m_d, m_a = float(mkt[0]), float(mkt[1]), float(mkt[2])

        blended = {
            "home": w.elo * e_h + w.dc * d_h + w.market * m_h,
            "draw": w.elo * e_d + w.dc * d_d + w.market * m_d,
            "away": w.elo * e_a + w.dc * d_a + w.market * m_a,
        }
        out.append(
            _FixtureBlend(
                fx=fx,
                home=home,
                away=away,
                neutral=neutral,
                host=host,
                books=books,  # type: ignore[arg-type]
                blended=blended,
                elo_map={"home": e_h, "draw": e_d, "away": e_a},
                dc_map={"home": d_h, "draw": d_d, "away": d_a},
                mkt_map={"home": m_h, "draw": m_d, "away": m_a},
            )
        )
    return out


def build_card(
    models: FittedModels,
    odds_df: pd.DataFrame,
    pools: Sequence[PoolConfig],
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
) -> List[Recommendation]:
    """Generate ranked recommendations for every +EV outcome in the slate.

    Parameters
    ----------
    models:
        Fitted Elo + DC models.
    odds_df:
        Flat odds frame from ``theoddsapi.get_odds`` (needs h2h rows).
    pools:
        Bankroll pools to size stakes for.
    fixtures_meta:
        Results-schedule rows for the upcoming fixtures, used for the
        ``neutral`` flag and host nation (columns: home_team, away_team,
        neutral, optionally country).
    weights:
        Blend weights (Elo, DC, market).
    min_edge:
        Minimum edge (EV per unit) to include a recommendation.
    host_nations:
        Nations that receive host advantage on a neutral venue.
    """
    blends = _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations
    )

    recs: List[Recommendation] = []
    for fb in blends:
        home, away = fb.home, fb.away
        team_map = {"home": home, "draw": "Draw", "away": away}

        for outcome in OUTCOMES:
            book, odds = best_price(fb.books, outcome)
            if book is None or odds <= 1.0:
                continue
            p = fb.blended[outcome]
            e = kelly_mod.edge(p, odds)
            if e < min_edge:
                continue
            stakes: Dict[str, float] = {}
            for pool in pools:
                stakes[pool.name] = kelly_mod.stake(
                    p, odds, pool.bankroll,
                    fraction=pool.kelly_fraction, cap=pool.per_bet_cap,
                )
            recs.append(
                Recommendation(
                    match_id=str(fb.fx["event_id"]),
                    match_desc="%s vs %s" % (home, away),
                    commence_time=str(fb.fx["commence_time"]),
                    selection=outcome,
                    selection_team=team_map[outcome],
                    best_book=book,
                    best_odds=odds,
                    model_prob=p,
                    market_prob=fb.mkt_map[outcome],
                    elo_prob=fb.elo_map[outcome],
                    dc_prob=fb.dc_map[outcome],
                    edge=e,
                    ev_per_unit=e,
                    stakes=stakes,
                )
            )

    recs.sort(key=lambda r: r.edge, reverse=True)
    return recs


def build_score_cards(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    top_k: int = 6,
) -> List[ScorelineCard]:
    """Full-time scoreline cards reconciled to the *same* blended 1X2 as the bets.

    For every fixture with a usable market this builds the Dixon-Coles score
    matrix and reconciles it (via :func:`wca.models.scores.reconcile_scoreline_matrix`)
    to the blended ``(home, draw, away)`` probability the card pipeline bets
    against, then derives the top scorelines, over/under and BTTS from the
    reconciled matrix. The returned list is aligned one-to-one with the fixtures
    that survive market filtering, in odds-feed order.

    Parameters
    ----------
    models, odds_df, fixtures_meta, weights, host_nations:
        Same as :func:`build_card`; ``weights`` MUST match those used for the
        recommendations so the scorelines are consistent with the picks.
    min_edge:
        Edge threshold stored on each card for its ``min_price`` helper.
    top_k:
        Number of top scorelines per fixture (default 6).
    """
    blends = _iter_fixture_blends(
        models, odds_df, fixtures_meta, weights, host_nations
    )

    cards: List[ScorelineCard] = []
    for fb in blends:
        pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
        target = (
            fb.blended["home"],
            fb.blended["draw"],
            fb.blended["away"],
        )
        cards.append(
            scoreline_card(
                pred,
                target,
                home=fb.home,
                away=fb.away,
                top_k=top_k,
                min_edge=min_edge,
            )
        )
    return cards


def apply_daily_exposure_caps(
    recs: List[Recommendation], pools: Sequence[PoolConfig]
) -> List[Recommendation]:
    """Scale each pool's stakes down so same-day total respects its cap."""
    pool_by_name = {p.name: p for p in pools}
    for name, pool in pool_by_name.items():
        stakes = np.array([r.stakes.get(name, 0.0) for r in recs], dtype=float)
        scaled = kelly_mod.simultaneous_exposure_scale(
            stakes, pool.daily_exposure_cap, pool.bankroll
        )
        for r, s in zip(recs, scaled):
            r.stakes[name] = float(s)
    return recs


def format_card(recs: Sequence[Recommendation], pools: Sequence[PoolConfig]) -> str:
    """Human-readable card for the terminal or Telegram (Markdown)."""
    if not recs:
        return "*No +EV bets* on the current slate at the configured threshold."
    lines = ["*World Cup Alpha — bet card* (%d picks)" % len(recs), ""]
    for i, r in enumerate(recs, 1):
        stake_str = "  ".join(
            "%s %.2f" % (p.name, r.stakes.get(p.name, 0.0)) for p in pools
        )
        lines.append(
            "*%d. %s* — %s @ *%.2f* (%s)\n"
            "    model %.1f%% / mkt %.1f%%  edge *%+.1f%%*  [elo %.0f%% dc %.0f%%]\n"
            "    stake: %s"
            % (
                i, r.match_desc, r.selection_team, r.best_odds, r.best_book,
                r.model_prob * 100, r.market_prob * 100, r.edge * 100,
                r.elo_prob * 100, r.dc_prob * 100, stake_str,
            )
        )
    return "\n".join(lines)


def format_scores(
    cards: Sequence[ScorelineCard], min_edge: float = 0.02
) -> str:
    """Human-readable scoreline card for the terminal or Telegram (Markdown).

    Per fixture: the top-6 scorelines (``"2-1  12.3%  fair 8.13  back >= 8.46"``)
    followed by one line with over/under 2.5 and BTTS probabilities. The
    ``back >=`` price is the minimum decimal odds at which backing that
    scoreline clears ``min_edge`` (each card's own ``min_edge`` is used; the
    argument is a display-only fallback for cards that predate it).
    """
    if not cards:
        return "*No scoreline cards* for the current slate."
    lines: List[str] = ["*World Cup Alpha — scorelines* (%d fixtures)" % len(cards)]
    for c in cards:
        me = getattr(c, "min_edge", min_edge)
        lines.append("")
        lines.append("*%s vs %s*" % (c.home, c.away))
        for h, a, p in c.top_scorelines:
            lines.append(
                "    %d-%d  %.1f%%  fair %.2f  back >= %.2f"
                % (h, a, p * 100, c.fair_odds(p), c.min_price(p, me))
            )
        ou25 = c.over_under.get(2.5)
        p_over = ou25[0] if ou25 is not None else float("nan")
        lines.append(
            "    O/U 2.5: over %.1f%% / under %.1f%%   BTTS %.1f%%"
            % (p_over * 100, (1.0 - p_over) * 100, c.btts * 100)
        )
    return "\n".join(lines)
