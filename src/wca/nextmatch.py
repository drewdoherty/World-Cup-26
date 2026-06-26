"""Next-match preview card: one fixture, every angle.

Builds a single-fixture Telegram card for the *next* upcoming match in the
odds slate:

* winner — the blended 1X2 (same Elo/DC/market blend the bet card uses),
  with fair odds, the best available book price and the edge per outcome;
* corners — the calibrated :class:`wca.models.props.CornersModel` driven by
  the Dixon-Coles expected-goals lambdas, at a configurable line;
* top goalscorers — the top 2 players per team (4 total). For each: a
  market-implied goals-per-game rate (``-ln(1 - anytime prob)`` from the best
  book anytime price — there is no per-player 2026 goal-count feed) plus the
  best **sportsbook** anytime + first-goalscorer odds (Odds API player-prop
  markets) and the **Polymarket** "1+ goals" anytime price. Polymarket carries
  no per-player first-goalscorer market, which is flagged in the block's note.
  Players are split onto the right side via ``data/squads.json``;
* scorelines — the reconciled Dixon-Coles score matrix (same reconciliation
  as the main card's scorelines section) plus O/U 2.5 and BTTS.

Like the rest of the card pipeline this module only *recommends*; nothing
here places a bet. The heavy build runs on cron (``scripts/wca_build_card.py``
writes ``data/next_latest.md``) and the bot serves the cache via ``/next``.
"""

from __future__ import annotations

import json
import math
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from wca.card import (
    OUTCOMES,
    BlendWeights,
    FittedModels,
    _FixtureBlend,
    _iter_fixture_blends,
    best_price,
    net_odds,
    venue_label,
)
from wca.markets import kelly as kelly_mod
from wca.models.props import CornersModel
from wca.models.scores import ScorelineCard, scoreline_card

DEFAULT_CORNERS_LINE = 8.5
ANYTIME_SCORER_MARKET = "player_goal_scorer_anytime"
FIRST_SCORER_MARKET = "player_first_goal_scorer"
SCORER_MARKETS = "%s,%s" % (ANYTIME_SCORER_MARKET, FIRST_SCORER_MARKET)
DEFAULT_SQUADS_PATH = "data/squads.json"


@dataclass
class ScorerPrice:
    """Best market price for one player in the anytime-scorer market."""

    player: str
    best_odds: float
    best_book: str
    implied: float  # raw 1/odds — vig NOT removed (anytime is not a simplex)


@dataclass
class GoalscorerLine:
    """One player's anytime + first-goalscorer prices across sources.

    All probabilities/odds are pulled live at build time — none are stored.
    Any field that could not be resolved is ``None`` and rendered as ``--``.
    """

    player: str
    team: str  # canonical team, or "" when the squad split could not place them
    # Anytime goalscorer.
    anytime_book_odds: Optional[float] = None   # best (max) sportsbook decimal odds
    anytime_book: Optional[str] = None
    anytime_pm_odds: Optional[float] = None     # 1 / PM YES price (decimal)
    anytime_pm_price: Optional[float] = None    # raw PM YES price (probability)
    # First goalscorer (sportsbook only — Polymarket has no per-player FGS market).
    first_book_odds: Optional[float] = None
    first_book: Optional[str] = None
    # Market-implied tournament scoring rate (goals per game). Derived from the
    # de-vig-free best anytime price as lambda = -ln(1 - p_anytime); this is the
    # market's expected goals/game for the player, NOT an observed 2026 count.
    xg_per_game: Optional[float] = None
    # Player-level MODEL prices (StatsBomb npxg-share + DC team lambda, via
    # wca.models.scorers.ScorerPricer). Present only when the player has a share
    # in data/players.json (or the empirical props_players.csv); otherwise None
    # and the line stays market-only. These drive the Kelly edge/stake.
    model_p_anytime: Optional[float] = None
    model_fair_anytime: Optional[float] = None
    model_p_first: Optional[float] = None
    model_fair_first: Optional[float] = None
    share_source: Optional[str] = None  # provenance of the npxg share, if priced

    @property
    def anytime_implied(self) -> Optional[float]:
        """Raw implied probability from the best sportsbook anytime odds."""
        if self.anytime_book_odds and self.anytime_book_odds > 1.0:
            return 1.0 / self.anytime_book_odds
        return None


@dataclass
class NextMatchCard:
    """Everything the /next Telegram card renders for one fixture."""

    home: str
    away: str
    commence_time: str
    # outcome -> (blended_prob, best_book or None, best_odds, edge)
    winner: Dict[str, Tuple[float, Optional[str], float, float]]
    corners_line: float
    corners_p_over: float
    corners_mu: float
    scores: ScorelineCard
    scorers: List[ScorerPrice] = field(default_factory=list)
    # Top goalscorers split by team: home -> [GoalscorerLine], away -> [...].
    goalscorers: Dict[str, List[GoalscorerLine]] = field(default_factory=dict)
    goalscorer_note: str = ""  # basis / data-gap note shown under the block
    min_edge: float = 0.02
    # Staking: quarter-Kelly on the resolved sportsbook-pool bankroll (threaded
    # in by the build from the same CLV ladder the bet card uses).
    bankroll: float = 1500.0
    kelly_fraction: float = 0.25
    kelly_cap: float = 0.05


def select_next_blend(blends: Sequence[_FixtureBlend]) -> Optional[_FixtureBlend]:
    """The fixture kicking off first (min commence_time), or None if empty."""
    if not blends:
        return None
    return min(blends, key=lambda fb: str(fb.fx["commence_time"]))


def top_scorers_from_odds(
    scorer_df: Optional[pd.DataFrame],
    top_n: int = 5,
    market: str = ANYTIME_SCORER_MARKET,
) -> List[ScorerPrice]:
    """Best anytime-scorer price per player, ranked by implied probability.

    ``scorer_df`` is the flat frame from :func:`wca.data.theoddsapi.get_event_odds`
    (may be ``None`` / empty / missing the market — all degrade to ``[]``).
    Shortest price = market favourite, so ranking by raw implied probability
    matches the books' own ordering even though the vig is left in.
    """
    if scorer_df is None or scorer_df.empty or "market" not in scorer_df.columns:
        return []
    rows = scorer_df[scorer_df["market"] == market].copy()
    if rows.empty:
        return []
    # Player-prop outcomes carry the player in ``description`` with
    # outcome_name = "Yes"; fall back to outcome_name for feeds without it.
    if "outcome_description" in rows.columns:
        desc = rows["outcome_description"].fillna("").astype(str)
        rows["_player"] = desc.where(desc != "", rows["outcome_name"].astype(str))
    else:
        rows["_player"] = rows["outcome_name"].astype(str)
    out: List[ScorerPrice] = []
    for player, grp in rows.groupby("_player"):
        # Best price for the punter is the MAX odds across books.
        idx = grp["decimal_odds"].astype(float).idxmax()
        odds = float(grp.loc[idx, "decimal_odds"])
        if odds <= 1.0:
            continue
        out.append(
            ScorerPrice(
                player=str(player),
                best_odds=odds,
                best_book=str(grp.loc[idx, "bookmaker_title"]),
                implied=1.0 / odds,
            )
        )
    out.sort(key=lambda s: s.implied, reverse=True)
    return out[:top_n]


def _norm_name(name: str) -> str:
    """Accent/case/whitespace-insensitive player-name key."""
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def _name_key(name: str) -> str:
    """First-initial + surname loose key (mirrors polymarket._player_key)."""
    parts = _norm_name(name).split()
    if len(parts) < 2:
        return ""
    return parts[0][:1] + "|" + parts[-1]


def load_squads(path: str = DEFAULT_SQUADS_PATH) -> Dict[str, List[str]]:
    """Load the per-team squad name lists (keys beginning with ``_`` ignored).

    Returns ``{canonical_team: [player, ...]}``; ``{}`` when the file is absent
    so callers degrade to "team unknown" rather than crashing.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: list(v) for k, v in raw.items() if not k.startswith("_")}


def _team_for_player(
    player: str,
    squads: Dict[str, List[str]],
    home: str,
    away: str,
) -> str:
    """Return the canonical team for *player*, restricted to {home, away}.

    Matches exact-normalised first, then the loose first-initial+surname key.
    Returns ``""`` when the player is in neither fixture squad list.
    """
    from wca.data.teamnames import canonical

    targets = {canonical(home): home, canonical(away): away}
    want, want_key = _norm_name(player), _name_key(player)
    fuzzy = ""
    for team_name, roster in squads.items():
        if canonical(team_name) not in targets:
            continue
        for rp in roster:
            if _norm_name(rp) == want:
                return canonical(team_name)
            if want_key and _name_key(rp) == want_key and not fuzzy:
                fuzzy = canonical(team_name)
    return fuzzy


def _best_book_odds(rows: pd.DataFrame) -> Tuple[Optional[float], Optional[str]]:
    """Best (max) decimal odds + book title for one player's market rows."""
    if rows.empty:
        return None, None
    odds = pd.to_numeric(rows["decimal_odds"], errors="coerce")
    if odds.dropna().empty:
        return None, None
    idx = odds.idxmax()
    o = float(odds.loc[idx])
    if o <= 1.0:
        return None, None
    book = rows.loc[idx].get("bookmaker_title")
    return o, (str(book) if book is not None else None)


def _player_rows(scorer_df: pd.DataFrame, market: str) -> Dict[str, pd.DataFrame]:
    """Group a market's rows by player (description, falling back to name)."""
    rows = scorer_df[scorer_df["market"] == market].copy()
    if rows.empty:
        return {}
    if "outcome_description" in rows.columns:
        desc = rows["outcome_description"].fillna("").astype(str)
        rows["_player"] = desc.where(desc != "", rows["outcome_name"].astype(str))
    else:
        rows["_player"] = rows["outcome_name"].astype(str)
    return {str(p): g for p, g in rows.groupby("_player")}


def build_goalscorers(
    home: str,
    away: str,
    scorer_df: Optional[pd.DataFrame],
    *,
    top_n_per_team: int = 2,
    squads_path: str = DEFAULT_SQUADS_PATH,
    pm_events: Optional[List[dict]] = None,
    pm_lookup: bool = True,
    lambda_home: float = 0.0,
    lambda_away: float = 0.0,
    players_path: str = "data/players.json",
) -> Tuple[Dict[str, List[GoalscorerLine]], str]:
    """Top-N goalscorers per team with anytime + first odds (book + Polymarket).

    Ranks every player in the anytime market by best-price implied probability,
    splits them onto the home/away side via ``data/squads.json``, keeps the top
    ``top_n_per_team`` per side, then attaches:

    * best **sportsbook** anytime + first-goalscorer decimal odds (Odds API
      player-prop markets — max across books);
    * the **Polymarket** "1+ goals" price (anytime equivalent; Polymarket has no
      per-player first-goalscorer market);
    * the market-implied **goals-per-game** rate ``-ln(1 - p_anytime)``.

    Returns ``({"home": [...], "away": [...]}, note)`` where *note* records the
    pricing basis and any data that could not be obtained.
    """
    from wca.data.teamnames import canonical

    home_c, away_c = canonical(home), canonical(away)
    empty: Dict[str, List[GoalscorerLine]] = {"home": [], "away": []}
    if scorer_df is None or scorer_df.empty or "market" not in scorer_df.columns:
        return empty, "no sportsbook scorer market available for this fixture"

    anytime = _player_rows(scorer_df, ANYTIME_SCORER_MARKET)
    first = _player_rows(scorer_df, FIRST_SCORER_MARKET)
    if not anytime:
        return empty, "no anytime-scorer market available for this fixture"

    squads = load_squads(squads_path)

    # Build one ranked line per player from the anytime market.
    ranked: List[GoalscorerLine] = []
    for player, grp in anytime.items():
        a_odds, a_book = _best_book_odds(grp)
        if a_odds is None:
            continue
        p_any = 1.0 / a_odds
        line = GoalscorerLine(
            player=player,
            team=_team_for_player(player, squads, home, away),
            anytime_book_odds=a_odds,
            anytime_book=a_book,
            xg_per_game=(-math.log(1.0 - p_any) if 0.0 < p_any < 1.0 else None),
        )
        f_odds, f_book = _best_book_odds(first.get(player, pd.DataFrame()))
        line.first_book_odds, line.first_book = f_odds, f_book
        ranked.append(line)

    ranked.sort(key=lambda l: l.anytime_implied or 0.0, reverse=True)

    # Players the squad split could not attribute to either fixture side. A
    # large count usually means the fixture's teams are absent from squads.json.
    unplaced = sum(1 for line in ranked if line.team == "")

    by_team: Dict[str, List[GoalscorerLine]] = {"home": [], "away": []}
    for line in ranked:
        if line.team == home_c and len(by_team["home"]) < top_n_per_team:
            by_team["home"].append(line)
        elif line.team == away_c and len(by_team["away"]) < top_n_per_team:
            by_team["away"].append(line)
        if len(by_team["home"]) >= top_n_per_team and len(by_team["away"]) >= top_n_per_team:
            break

    # Attach Polymarket anytime ("1+ goals") prices for the selected players.
    pm_missing: List[str] = []
    if pm_lookup:
        try:
            from wca.data import polymarket as pm

            if pm_events is None:
                pm_events = pm.find_world_cup_markets(include_closed=False)
            for side in ("home", "away"):
                for line in by_team[side]:
                    res = pm.resolve_player_anytime_token(
                        home, away, line.player, events=pm_events
                    )
                    if res is not None and 0.0 < float(res["price"]) < 1.0:
                        line.anytime_pm_price = float(res["price"])
                        line.anytime_pm_odds = 1.0 / float(res["price"])
                    else:
                        pm_missing.append(line.player)
        except Exception:  # network/parse failure must not break the card
            pm_missing = ["(Polymarket lookup failed)"]

    # Player-level MODEL pricing: StatsBomb npxg-share (data/players.json
    # override store) + the DC team lambda, via ScorerPricer. Only players with
    # a known share are priced — we never invent a share — so the rest stay
    # market-only. The model price is what the Kelly edge/stake is taken against.
    n_priced = 0
    if lambda_home > 0.0 and lambda_away > 0.0:
        try:
            from wca.data.teamnames import canonical as _canon
            from wca.models.scorers import ScorerPricer, load_player_overrides

            overrides = load_player_overrides(players_path)
            exact: Dict[Tuple[str, str], "object"] = {}
            loose: Dict[Tuple[str, str], "object"] = {}
            for tname, recs in overrides.items():
                tc = _canon(tname)
                for rec in recs:
                    exact[(tc, _norm_name(rec.name))] = rec
                    lk = _name_key(rec.name)
                    if lk:
                        loose.setdefault((tc, lk), rec)
            pricer = ScorerPricer()
            total_lambda = lambda_home + lambda_away
            for side in ("home", "away"):
                team_lambda = lambda_home if side == "home" else lambda_away
                for line in by_team[side]:
                    rec = exact.get((line.team, _norm_name(line.player)))
                    if rec is None:
                        lk = _name_key(line.player)
                        rec = loose.get((line.team, lk)) if lk else None
                    if rec is None:
                        continue
                    sl = pricer.price_player(rec, team_lambda, total_lambda)
                    line.model_p_anytime = sl.p_anytime
                    line.model_fair_anytime = sl.fair_anytime
                    line.model_p_first = sl.p_first
                    line.model_fair_first = sl.fair_first
                    line.share_source = rec.source
                    n_priced += 1
        except Exception:  # a pricing failure must not break the card
            n_priced = 0

    # Compose the basis / data-gap note.
    notes = [
        "basis: goals/game = market-implied xG/game (-ln(1-anytime prob)); "
        "no per-player 2026 goal counts are tracked",
    ]
    if not squads:
        notes.append("squads.json missing — players not split by team")
    if unplaced:
        notes.append("%d market player(s) not in squad lists" % unplaced)
    if pm_missing:
        notes.append("no PM 1+ goals market: " + ", ".join(pm_missing))
    notes.append("Polymarket has no per-player first-goalscorer market")
    if n_priced:
        notes.append(
            "stake = ¼-Kelly vs best book where the player-level model "
            "(StatsBomb npxg-share × DC λ) shows +EV"
        )
    else:
        notes.append(
            "no player-level model share for these players "
            "(data/players.json) — goalscorers shown market-only, not Kelly-sized"
        )
    return by_team, "; ".join(notes)


def build_next_match(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    weights: BlendWeights = BlendWeights(),
    scorer_df: Optional[pd.DataFrame] = None,
    corners_line: float = DEFAULT_CORNERS_LINE,
    corners_model: Optional[CornersModel] = None,
    min_edge: float = 0.02,
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    top_k_scores: int = 6,
    top_scorers_per_team: int = 2,
    squads_path: str = DEFAULT_SQUADS_PATH,
    pm_events: Optional[List[dict]] = None,
    pm_lookup: bool = True,
    bankroll: float = 1500.0,
    kelly_fraction: float = 0.25,
    kelly_cap: float = 0.05,
    players_path: str = "data/players.json",
) -> Optional[NextMatchCard]:
    """Build the next-match preview, or None when the slate is empty.

    ``odds_df`` should already be filtered to the look-ahead window by the
    caller (same frame the main card build uses); the earliest kickoff among
    fixtures with a usable market wins. ``scorer_df`` is the optional per-event
    anytime + first-goalscorer pull for that fixture; ``pm_lookup`` resolves the
    Polymarket "1+ goals" price per selected player (set False to skip network).
    """
    blends = _iter_fixture_blends(models, odds_df, fixtures_meta, weights, host_nations)
    fb = select_next_blend(blends)
    if fb is None:
        return None

    winner: Dict[str, Tuple[float, Optional[str], float, float]] = {}
    for outcome in OUTCOMES:
        p = fb.blended[outcome]
        book, odds = best_price(fb.books, outcome)
        # Fee-adjusted edge off the chosen venue's net price; display the gross
        # price + a clean venue label (Betfair / Polymarket).
        edge = (kelly_mod.edge(p, net_odds(book, odds))
                if book is not None and odds > 1.0 else float("nan"))
        winner[outcome] = (p, venue_label(book), odds, edge)

    pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
    scores = scoreline_card(
        pred,
        (fb.blended["home"], fb.blended["draw"], fb.blended["away"]),
        home=fb.home,
        away=fb.away,
        top_k=top_k_scores,
        min_edge=min_edge,
    )

    cm = corners_model or CornersModel()
    lam_h = float(getattr(pred, "lambda_home", 0.0) or 0.0)
    lam_a = float(getattr(pred, "lambda_away", 0.0) or 0.0)
    p_over = cm.prob_over(corners_line, lam_h, lam_a)
    mu = cm.mean_total(lam_h, lam_a)

    goalscorers, gs_note = build_goalscorers(
        fb.home,
        fb.away,
        scorer_df,
        top_n_per_team=top_scorers_per_team,
        squads_path=squads_path,
        pm_events=pm_events,
        pm_lookup=pm_lookup,
        lambda_home=lam_h,
        lambda_away=lam_a,
        players_path=players_path,
    )

    return NextMatchCard(
        home=fb.home,
        away=fb.away,
        commence_time=str(fb.fx["commence_time"]),
        winner=winner,
        corners_line=corners_line,
        corners_p_over=p_over,
        corners_mu=mu,
        scores=scores,
        scorers=top_scorers_from_odds(scorer_df),
        goalscorers=goalscorers,
        goalscorer_note=gs_note,
        min_edge=min_edge,
        bankroll=bankroll,
        kelly_fraction=kelly_fraction,
        kelly_cap=kelly_cap,
    )


def _fmt_odds(o: Optional[float]) -> str:
    """Decimal odds or ``--`` when unavailable."""
    return "%.2f" % o if o and o > 1.0 else "--"


def _model_suffix(
    model_p: Optional[float],
    model_fair: Optional[float],
    book_odds: Optional[float],
    card: "NextMatchCard",
) -> str:
    """`` | model <fair> <edge%> £<stake>`` for a priced goalscorer leg.

    Empty when the player has no model price (no share). The Kelly stake is
    quarter-Kelly of the card bankroll vs the best book odds, shown only on a
    positive model edge (``model_p * book_odds - 1 > 0``).
    """
    if not model_p or not model_fair or model_fair <= 1.0:
        return ""
    s = " | model %.2f" % model_fair
    if book_odds and book_odds > 1.0:
        edge = model_p * book_odds - 1.0
        s += " %+.0f%%" % (edge * 100)
        stk = kelly_mod.stake(
            model_p, book_odds, card.bankroll, card.kelly_fraction, card.kelly_cap
        )
        if stk > 0:
            s += " £%.2f" % stk
    return s


def _goalscorer_team_blocks(card) -> List[str]:
    """Per-team player lines (no section header / note).

    Shared by the single-fixture /next block and the multi-fixture
    /goalscorers card. ``card`` need only expose ``goalscorers`` / ``home`` /
    ``away`` and the Kelly fields read by :func:`_model_suffix`.
    """
    gs = card.goalscorers or {}
    out: List[str] = []
    side_team = {"home": card.home, "away": card.away}
    for side in ("home", "away"):
        rows = gs.get(side) or []
        if not rows:
            continue
        out.append("_%s_" % side_team[side])
        # Per team, flag the "most likely" scorer (shortest anytime price) and
        # the "best EV" pick (highest model edge vs best book — only when the
        # player-level model could price them). These are the picks the subtitle
        # says the ¼-Kelly £ stake is sized on.
        likely = None
        best_ev = None
        best_ev_val = 0.0
        for ln in rows:
            if ln.anytime_book_odds and (
                likely is None or ln.anytime_book_odds < likely.anytime_book_odds
            ):
                likely = ln
            if ln.model_p_anytime and ln.anytime_book_odds:
                ev = ln.model_p_anytime * ln.anytime_book_odds - 1.0
                if ev > best_ev_val:
                    best_ev_val, best_ev = ev, ln
        for ln in rows:
            tags = []
            if ln is likely:
                tags.append("⭐ most likely")
            if ln is best_ev:
                tags.append("💰 best EV")
            tag = ("  " + " · ".join(tags)) if tags else ""
            gpg = ("%.2f g/g" % ln.xg_per_game) if ln.xg_per_game else "g/g --"
            out.append("  %s  (%s)%s" % (ln.player[:20], gpg, tag))
            out.append(
                "    Any  bk %s%s / PM %s%s"
                % (
                    _fmt_odds(ln.anytime_book_odds),
                    (" (%s)" % ln.anytime_book) if ln.anytime_book else "",
                    _fmt_odds(ln.anytime_pm_odds),
                    _model_suffix(
                        ln.model_p_anytime, ln.model_fair_anytime,
                        ln.anytime_book_odds, card,
                    ),
                )
            )
            out.append(
                "    1st  bk %s%s / PM --%s"
                % (
                    _fmt_odds(ln.first_book_odds),
                    (" (%s)" % ln.first_book) if ln.first_book else "",
                    _model_suffix(
                        ln.model_p_first, ln.model_fair_first,
                        ln.first_book_odds, card,
                    ),
                )
            )
    return out


def _format_goalscorers(card: NextMatchCard) -> List[str]:
    """Render the per-team top-goalscorers block (compact, phone-width)."""
    gs = card.goalscorers or {}
    if not (gs.get("home") or gs.get("away")):
        # Fall back to the legacy flat anytime list if the split is empty.
        if card.scorers:
            out = ["*Anytime scorer* (best book price, vig in)"]
            for s in card.scorers:
                out.append(
                    "  %-18s %5.2f (%s)  imp %.0f%%"
                    % (s.player[:18], s.best_odds, s.best_book, s.implied * 100)
                )
            return out
        return ["*Top goalscorers* — no scorer market available yet."]

    out = ["*Top goalscorers* — best book / Polymarket"]
    out.extend(_goalscorer_team_blocks(card))
    if card.goalscorer_note:
        out.append("_%s_" % card.goalscorer_note)
    return out


def format_next_match(card: Optional[NextMatchCard]) -> str:
    """Telegram Markdown for the next-match card (phone-width friendly)."""
    if card is None:
        return "*Next match*\nNo upcoming fixture with a usable market in the current window."

    lines: List[str] = [
        "⚽ *Next match* — %s vs %s" % (card.home, card.away),
        "Kickoff %s" % card.commence_time,
        "",
        "*Winner* (model blend)",
    ]
    names = {"home": card.home, "draw": "Draw", "away": card.away}
    staked = False
    for outcome in OUTCOMES:
        p, book, odds, edge = card.winner[outcome]
        fair = (1.0 / p) if p > 0 else float("inf")
        line = "  %-14s %5.1f%%  fair %.2f" % (names[outcome][:14], p * 100, fair)
        if book is not None and odds > 1.0:
            flag = " ✅" if edge >= card.min_edge else ""
            line += "  best %.2f (%s) %+.1f%%%s" % (odds, book, edge * 100, flag)
            stk = kelly_mod.stake(
                p, odds, card.bankroll, card.kelly_fraction, card.kelly_cap
            )
            if stk > 0:
                line += "  £%.2f" % stk
                staked = True
        lines.append(line)
    if staked:
        lines.append(
            "  _stake = ¼-Kelly @ £%.0f bankroll (+EV picks)_" % card.bankroll
        )

    p_over = card.corners_p_over
    lines.append("")
    lines.append("*Corners* (model, exp %.1f)" % card.corners_mu)
    lines.append(
        "  O/U %.1f: over %.1f%% / under %.1f%%  fair %.2f / %.2f"
        % (
            card.corners_line,
            p_over * 100,
            (1.0 - p_over) * 100,
            (1.0 / p_over) if p_over > 0 else float("inf"),
            (1.0 / (1.0 - p_over)) if p_over < 1 else float("inf"),
        )
    )

    lines.append("")
    lines.extend(_format_goalscorers(card))

    c = card.scores
    lines.append("")
    lines.append("*Scorelines* (top %d)" % len(c.top_scorelines))
    lines.append(
        " | ".join("%d-%d %.1f%%" % (h, a, p * 100) for h, a, p in c.top_scorelines)
    )
    ou25 = c.over_under.get(2.5)
    if ou25 is not None:
        lines.append(
            "  O/U 2.5: over %.1f%% / under %.1f%%   BTTS %.1f%%"
            % (ou25[0] * 100, ou25[1] * 100, c.btts * 100)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /goalscorers — anytime + first-goalscorer card for the next N fixtures.
# ---------------------------------------------------------------------------


@dataclass
class GoalscorerFixture:
    """One fixture's goalscorer block for the multi-game /goalscorers card.

    Carries the same fields :func:`_goalscorer_team_blocks` / :func:`_model_suffix`
    read off a :class:`NextMatchCard`, so the shared renderer works for both.
    """

    home: str
    away: str
    commence_time: str
    goalscorers: Dict[str, List[GoalscorerLine]] = field(default_factory=dict)
    goalscorer_note: str = ""
    # Flat top-anytime fallback (both teams, unsplit) used when the squad split
    # fails — so a fixture whose teams are absent from squads.json still shows
    # its most-likely scorers + best anytime price instead of nothing.
    scorers: List[ScorerPrice] = field(default_factory=list)
    bankroll: float = 1500.0
    kelly_fraction: float = 0.25
    kelly_cap: float = 0.05


def build_goalscorer_card(
    models: FittedModels,
    odds_df: pd.DataFrame,
    fixtures_meta: pd.DataFrame,
    scorer_by_event: Dict[str, pd.DataFrame],
    *,
    weights: BlendWeights = BlendWeights(),
    host_nations: Sequence[str] = ("United States", "Mexico", "Canada", "USA"),
    top_k_fixtures: int = 5,
    top_n_per_team: int = 2,
    squads_path: str = DEFAULT_SQUADS_PATH,
    players_path: str = "data/players.json",
    bankroll: float = 1500.0,
    kelly_fraction: float = 0.25,
    kelly_cap: float = 0.05,
    pm_events: Optional[List[dict]] = None,
    pm_lookup: bool = True,
) -> List[GoalscorerFixture]:
    """Goalscorer blocks for the next ``top_k_fixtures`` fixtures by kickoff.

    ``scorer_by_event`` maps a fixture's ``event_id`` to its per-event
    anytime+first-goalscorer odds frame (pulled by the caller); a missing entry
    degrades that fixture to "no scorer market". Player-level model pricing and
    Kelly stakes follow the same rules as :func:`build_goalscorers`.
    """
    blends = _iter_fixture_blends(models, odds_df, fixtures_meta, weights, host_nations)
    blends = sorted(blends, key=lambda fb: str(fb.fx["commence_time"]))[:top_k_fixtures]
    # Resolve the Polymarket events list once and reuse it across fixtures.
    if pm_lookup and pm_events is None:
        try:
            from wca.data import polymarket as pm

            pm_events = pm.find_world_cup_markets(include_closed=False)
        except Exception:
            pm_events = []

    out: List[GoalscorerFixture] = []
    for fb in blends:
        try:
            pred = models.dc.predict(fb.home, fb.away, neutral=fb.neutral, warn=False)
            lam_h = float(getattr(pred, "lambda_home", 0.0) or 0.0)
            lam_a = float(getattr(pred, "lambda_away", 0.0) or 0.0)
        except Exception:
            lam_h = lam_a = 0.0
        scorer_df = scorer_by_event.get(str(fb.fx.get("event_id")))
        goalscorers, note = build_goalscorers(
            fb.home,
            fb.away,
            scorer_df,
            top_n_per_team=top_n_per_team,
            squads_path=squads_path,
            pm_events=pm_events,
            pm_lookup=pm_lookup,
            lambda_home=lam_h,
            lambda_away=lam_a,
            players_path=players_path,
        )
        # If the squad split placed nobody but the market exists, fall back to a
        # flat top-anytime list (both teams) so the fixture still shows recs.
        flat: List[ScorerPrice] = []
        if not (goalscorers.get("home") or goalscorers.get("away")) and scorer_df is not None:
            flat = top_scorers_from_odds(scorer_df, top_n=6)
        out.append(
            GoalscorerFixture(
                home=fb.home,
                away=fb.away,
                commence_time=str(fb.fx["commence_time"]),
                goalscorers=goalscorers,
                goalscorer_note=note,
                scorers=flat,
                bankroll=bankroll,
                kelly_fraction=kelly_fraction,
                kelly_cap=kelly_cap,
            )
        )
    return out


def _fmt_kickoff(ts: str) -> str:
    """Compact UTC kickoff label, e.g. ``Jun 19 19:00Z``."""
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%MZ")
    except Exception:
        return str(ts)


def format_goalscorer_card(fixtures: List[GoalscorerFixture]) -> str:
    """Telegram Markdown for the multi-fixture /goalscorers card."""
    if not fixtures:
        return (
            "⚽ *Goalscorers*\n"
            "No upcoming fixtures with a usable market in the current window."
        )
    out = [
        "⚽ *Goalscorers* — next %d games" % len(fixtures),
        "_anytime + first · best book / Polymarket; "
        "¼-Kelly £ stake on most likely + best EV_",
        "",
    ]
    for fx in fixtures:
        out.append(
            "*%s vs %s*  _%s_" % (fx.home, fx.away, _fmt_kickoff(fx.commence_time))
        )
        gs = fx.goalscorers or {}
        if gs.get("home") or gs.get("away"):
            out.extend(_goalscorer_team_blocks(fx))
            if fx.goalscorer_note:
                out.append("_%s_" % fx.goalscorer_note)
        elif fx.scorers:
            # Squad split unavailable: show the flat top-anytime list (both teams).
            out.append("_top anytime (both teams — add squad to split + FGS)_")
            for s in fx.scorers:
                out.append(
                    "  %-20s %5.2f (%s)  imp %.0f%%"
                    % (s.player[:20], s.best_odds, s.best_book, s.implied * 100)
                )
        else:
            out.append(
                "  _%s_" % (fx.goalscorer_note or "no scorer market for this fixture")
            )
        out.append("")
    return "\n".join(out).rstrip()
