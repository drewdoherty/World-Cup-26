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

# 1X2 outcome order used throughout: home, draw, away.
OUTCOMES = ("home", "draw", "away")


@dataclass
class BlendWeights:
    """Convex weights over (Elo, Dixon-Coles, market). Must sum to 1."""

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
    """Fit Elo (rating + outcome) and Dixon-Coles on the results history."""
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
    w = weights.normalised()
    fixtures = _index_odds(odds_df)

    # Build a quick lookup of neutral/host from the schedule by team pair.
    # Prefer *unplayed* (scheduled) rows so a historical friendly between the
    # same two teams can't overwrite the World Cup fixture's neutral/host flags.
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

    recs: List[Recommendation] = []
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
        elo_map = {"home": e_h, "draw": e_d, "away": e_a}
        dc_map = {"home": d_h, "draw": d_d, "away": d_a}
        mkt_map = {"home": m_h, "draw": m_d, "away": m_a}
        team_map = {"home": home, "draw": "Draw", "away": away}

        for outcome in OUTCOMES:
            book, odds = best_price(books, outcome)  # type: ignore[arg-type]
            if book is None or odds <= 1.0:
                continue
            p = blended[outcome]
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
                    match_id=str(fx["event_id"]),
                    match_desc="%s vs %s" % (home, away),
                    commence_time=str(fx["commence_time"]),
                    selection=outcome,
                    selection_team=team_map[outcome],
                    best_book=book,
                    best_odds=odds,
                    model_prob=p,
                    market_prob=mkt_map[outcome],
                    elo_prob=elo_map[outcome],
                    dc_prob=dc_map[outcome],
                    edge=e,
                    ev_per_unit=e,
                    stakes=stakes,
                )
            )

    recs.sort(key=lambda r: r.edge, reverse=True)
    return recs


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
