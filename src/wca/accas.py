"""Accumulator generator for the /accas bot command.

Model-driven, exposure-aware, EV-moneyline-first. Reads the cached model card
(``data/model_predictions.json`` for the blended 1X2 and per-fixture goal
lambdas, ``site/scores_data.json`` for O/U + BTTS model probs and per-venue
1X2 prices) plus the latest odds snapshot (``odds_snapshots`` table for
totals/btts/AH/player-props book prices) — never a live model fit, so it is
fast enough for an interactive command. Display-only: it NEVER writes to the
ledger and never triggers a site push.

Markets priced (off the Dixon-Coles matrix, reconciled to the blend):
  • 1X2 moneyline, Over/Under total goals, BTTS, Draw-No-Bet
  • Asian handicap ±0.5 (from 1X2); ±1.0/±1.5/etc. when cached lambdas allow
  • Anytime/first/2+/3+ goalscorer when cached lambdas + player params + book
    price all exist
  • Corners/cards when calibrated model + book price in snapshot exist

Markets NOT priced (listed in output as "unsupported"):
  • SOT / assists / exotics without an explicit model probability
  • Half-time result (not in current blend)
  • Any market with no cached price

Modes: ``value`` (default, moneyline +EV favourites at modest combined odds),
``edge`` (legacy edge-max underdogs), ``hedge`` (offset held cluster),
``longshot`` (allow >=4.0 legs), ``promo`` (qualify live bookmaker offers).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from wca.selection import (
    LONGSHOT_PROB,
    bucket_rank,
    hours_out as _sel_hours_out,
    longshot_no_cash,
)
from wca.displayfmt import bucket_tag, ev_marker, ev_str, implied_pct, pct
from wca.snapshot_freshness import (
    DEFAULT_MAX_AGE_HOURS as _SNAPSHOT_MAX_AGE_HOURS,
    check_snapshot_freshness as _check_snapshot_freshness,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAYER_PROP_TODO = (
    "SOT/assists/exotic props need explicit model + price — never invent a prob."
)

#: Exchange commission haircut on effective price (6% until July).
COMMISSION = {"betfair_ex_uk": 0.06, "smarkets": 0.06, "matchbook": 0.06}

# LONGSHOT_PROB is the canonical 0.25 cash floor, imported from wca.selection
# (user 2026-07-07). It REPLACES the old accas-local 0.12 threshold: legs the
# model rates 0.12-0.25 that used to be staged for cash are now no-cash
# longshots (free-bet / lottery only). LONGSHOT_ODDS keeps the belt-and-braces
# raw-price guard for degenerate high-odds legs.
LONGSHOT_ODDS = 9.0

#: Low-win default: keep combined product in modest ~2-8x band.
VALUE_MAX_COMBINED = 12.0

DEFAULT_MIN_EDGE = 0.02
#: Fallback sizing base used only when the governance ladder is unavailable
#: (``wca.card.resolve_pool_bankroll`` cannot be imported or raises). Mirrors
#: that ladder's rung-0 deployable bankroll (``card.LADDER_BANKROLLS[0]`` = £2,000
#: now) so the fallback never silently over-sizes vs the real base.
DEFAULT_BANKROLL = 2000.0
KELLY_FRACTION = 0.25

#: Promo scrape freshness gate. Sites stale/blocked beyond this → PROMO CHECK REQUIRED.
PROMO_STALE_HOURS = 6.0

#: Maximum per-fixture net downside as fraction of bankroll (correlated exposure cap).
FIXTURE_CAP_FRACTION = 0.05

#: PM markets whose settlement semantics CAN match a sportsbook leg.
PM_MATCH_MARKETS = frozenset({"1X2", "h2h", "dnb"})
PM_TOTALS_MARKETS = frozenset({"totals"})
PM_BTTS_MARKETS = frozenset({"btts"})

#: Polymarket commission / taker fee.
PM_FEE = 0.0  # fee-free; LP spread already embedded in mid-price

#: Max output length for Telegram.
MSG_LIMIT = 4096

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Leg:
    fixture: str
    market: str           # "1X2" | "totals" | "btts" | "dnb" | "asian_handicap"
                          # | "anytime_scorer" | "first_scorer" | "corners" | "cards"
    selection: str
    model_prob: float
    odds: float           # raw best book odds (gross)
    book: str
    edge: float           # model_prob * eff_odds - 1 (commission-adjusted)
    is_moneyline: bool
    commence_time: str = ""   # fixture kickoff (ISO/UTC) for the further-out key

    @property
    def is_longshot(self) -> bool:
        return self.model_prob < LONGSHOT_PROB or self.odds > LONGSHOT_ODDS

    @property
    def hours_out(self) -> float:
        """Continuous hours to kickoff (0.0 unknown) — canonical secondary key."""
        if not self.commence_time:
            return 0.0
        return _sel_hours_out({"match_desc": "_"}, {"_": self.commence_time})


@dataclass
class Acca:
    legs: List[Leg]
    combined_odds: float
    model_prob: float
    edge: float
    stake: float
    label: str = ""
    note: str = ""
    action: str = "diversify"   # "add" | "diversify" | "hedge"
    downside_gbp: float = 0.0   # worst-case loss (stake) if all legs lose


@dataclass
class Exposure:
    """Portfolio exposure: overlap + concentration + quantified downside."""
    # Legacy fields kept for backward compatibility.
    fixture_count: Dict[str, int] = field(default_factory=dict)
    held: set = field(default_factory=set)          # "fixturetoken|seltokens"
    team_long: Dict[str, int] = field(default_factory=dict)
    # Rich new fields (populated when stake/venue info is available).
    fixture_stake_gbp: Dict[str, float] = field(default_factory=dict)
    held_stakes: Dict[str, Optional[float]] = field(default_factory=dict)
    venue_gbp: Dict[str, float] = field(default_factory=dict)
    account_gbp: Dict[str, float] = field(default_factory=dict)
    source_gbp: Dict[str, float] = field(default_factory=dict)
    total_gbp_risk: float = 0.0
    bet_details: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toks(s: Any) -> List[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split()
            if len(t) > 2]


def _fixture_token(fixture: str) -> str:
    return " ".join(sorted(_toks(fixture)))


def _make_sig(leg: Leg) -> str:
    return "%s|%s" % (_fixture_token(leg.fixture), " ".join(sorted(_toks(leg.selection))))


def _eff(odds: float, book: Optional[str]) -> float:
    c = COMMISSION.get((book or "").strip().lower(), 0.0)
    return 1.0 + (odds - 1.0) * (1.0 - c)


def _kelly_fraction(p: float, odds: float) -> float:
    b = odds - 1.0
    if b <= 0:
        return 0.0
    return max((b * p - (1.0 - p)) / b, 0.0)


def _finished_tokens() -> List[tuple]:
    try:
        from wca.sitedata import _finished_fixture_tokens
        return _finished_fixture_tokens()
    except Exception:
        return []


def _is_finished(fixture: str, finished: List[tuple]) -> bool:
    text = " ".join(_toks(fixture))
    for home, away in finished or []:
        if home in text and away in text:
            return True
    return False


def _split_fixture(name: str) -> Tuple[str, str]:
    for sep in (" vs ", " v "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return name, ""


def _snapshot_age_hours(ts_utc: Optional[str], now: datetime) -> Optional[float]:
    """Hours since a snapshot timestamp (ISO string). None if unparseable."""
    if not ts_utc:
        return None
    try:
        dt = datetime.strptime(ts_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bankroll + FX resolution
# ---------------------------------------------------------------------------


def _resolve_bankroll(db_path: str, default: float = DEFAULT_BANKROLL) -> Tuple[float, float, str]:
    """Resolve the CLV-earned bankroll from the ledger. Returns (bankroll, kelly, reason)."""
    try:
        from wca.card import resolve_pool_bankroll, FLAT_KELLY_FRACTION
        pool = resolve_pool_bankroll(db_path)
        return float(pool.bankroll), float(pool.kelly_fraction), pool.reason
    except Exception as exc:
        reason = "fallback £%.0f (resolve_pool_bankroll unavailable: %s)" % (default, exc)
        return float(default), KELLY_FRACTION, reason


def _get_fx(allow_network: bool = False) -> Tuple[float, str]:
    """GBP→USD rate. Returns (usd_per_gbp, source_tag). Never raises.

    Credit discipline: the interactive ``/accas`` path makes **zero** network
    calls, so the default (``allow_network=False``) returns the cached fallback
    rate without a live fetch. Pass ``allow_network=True`` only from
    non-interactive contexts (daemons / reports) that may spend a live fetch.
    """
    if not allow_network:
        from wca.fx import FALLBACK_USD_PER_GBP
        return FALLBACK_USD_PER_GBP, "fallback"
    try:
        from wca.fx import get_gbp_usd
        r = get_gbp_usd()
        return r.usd_per_gbp, r.source
    except Exception:
        return 1.33, "fallback"


def _usd_to_gbp(usd: float, usd_per_gbp: float) -> float:
    if usd_per_gbp <= 0:
        return 0.0
    return usd / usd_per_gbp


# ---------------------------------------------------------------------------
# Candidate legs — 1X2 / totals / BTTS / DNB (pure)
# ---------------------------------------------------------------------------


def candidate_legs(
    fixtures: List[Dict[str, Any]],
    snapshot_prices: Optional[Dict[str, Dict[Tuple[str, str], Tuple[float, str]]]] = None,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    include_events: bool = True,
) -> List[Leg]:
    """Build priced, +EV legs from cached model probs + best book prices.

    ``fixtures``: list of dicts with keys ``fixture``, ``model_1x2``
    {home,draw,away}, optional ``over_under`` {line,over,under} (over/under as
    0..1), optional ``btts`` (0..1), and ``best_1x2`` {home,draw,away} ->
    (odds, book).
    ``snapshot_prices``: fixture-token -> {(market, selection): (odds, book)}
    for totals/btts derivative book prices.
    """
    snapshot_prices = snapshot_prices or {}
    legs: List[Leg] = []
    for fx in fixtures:
        name = fx.get("fixture") or ""
        ftok = _fixture_token(name)
        m1x2 = fx.get("model_1x2") or {}
        best = fx.get("best_1x2") or {}
        teams = _split_fixture(name)
        # 1X2 moneyline legs
        for key, label in (("home", teams[0]), ("draw", "Draw"), ("away", teams[1])):
            p = m1x2.get(key)
            bo = best.get(key)
            if p is None or not bo:
                continue
            odds, book = bo
            if not odds or odds <= 1.0:
                continue
            edge = float(p) * _eff(odds, book) - 1.0
            if edge >= min_edge:
                legs.append(Leg(name, "1X2", label, float(p), float(odds), book, edge, True))
        if not include_events:
            continue
        prices = snapshot_prices.get(ftok, {})
        # Totals
        ou = fx.get("over_under") or {}
        line = ou.get("line", 2.5)
        for side, p in (("Over", ou.get("over")), ("Under", ou.get("under"))):
            if p is None:
                continue
            sel = "%s %s" % (side, line)
            bo = prices.get(("totals", sel)) or prices.get(("totals", side))
            if not bo:
                continue
            odds, book = bo
            edge = float(p) * _eff(odds, book) - 1.0
            if edge >= min_edge:
                legs.append(Leg(name, "totals", sel, float(p), float(odds), book, edge, False))
        # BTTS
        btts = fx.get("btts")
        if btts is not None:
            for sel, p in (("BTTS Yes", float(btts)), ("BTTS No", 1.0 - float(btts))):
                bo = prices.get(("btts", sel)) or prices.get(("btts", sel.split()[-1]))
                if not bo:
                    continue
                odds, book = bo
                edge = float(p) * _eff(odds, book) - 1.0
                if edge >= min_edge:
                    legs.append(Leg(name, "btts", sel, float(p), float(odds), book, edge, False))
        # Draw-No-Bet (derived from 1X2; priced only if a book DNB price exists)
        ph, pa = m1x2.get("home"), m1x2.get("away")
        if ph is not None and pa is not None and (ph + pa) > 0:
            for team, p in ((teams[0], ph / (ph + pa)), (teams[1], pa / (ph + pa))):
                bo = prices.get(("draw_no_bet", team))
                if not bo:
                    continue
                odds, book = bo
                edge = float(p) * _eff(odds, book) - 1.0
                if edge >= min_edge:
                    legs.append(Leg(name, "dnb", "%s (DNB)" % team, float(p), float(odds), book,
                                    edge, True))
    return legs


# ---------------------------------------------------------------------------
# Asian handicap legs (derived from 1X2 or scoreline matrix)
# ---------------------------------------------------------------------------


def _ah_prob_from_1x2(m1x2: Dict[str, float], line: float) -> Tuple[Optional[float], Optional[float]]:
    """Derive AH home/away probabilities from 1X2 for simple ±0.5 lines.

    Returns (p_home_covers, p_away_covers) or (None, None) if not derivable.
    AH -0.5 home  = P(home win outright)
    AH +0.5 home  = P(home wins or draws)
    (Away side is the complement of each.)
    """
    ph = float(m1x2.get("home") or 0)
    pd = float(m1x2.get("draw") or 0)
    pa = float(m1x2.get("away") or 0)
    if abs(line + 0.5) < 0.01:          # AH home -0.5: home must outright win
        return ph, pa + pd
    if abs(line - 0.5) < 0.01:          # AH home +0.5: home wins or draws
        return ph + pd, pa
    return None, None


def _ah_prob_from_matrix(lam_h: float, lam_a: float, line: float) -> Tuple[Optional[float], Optional[float]]:
    """Derive AH home/away probabilities from the scoreline matrix for any line."""
    try:
        from wca.exposure_corr import scoreline_matrix
    except ImportError:
        return None, None
    try:
        mat = scoreline_matrix(float(lam_h), float(lam_a))
    except Exception:
        return None, None
    p_home = 0.0
    p_away = 0.0
    push_mass = 0.0
    nh, na = mat.shape
    for h in range(nh):
        for a in range(na):
            diff = h - a
            p = float(mat[h, a])
            # AH line: home side wins if diff > line; push if diff == line (integer lines only)
            if abs(diff - line) < 1e-9:  # exact integer push
                push_mass += p
            elif diff > line:
                p_home += p
            else:
                p_away += p
    # Quarter-ball lines (±0.25, ±0.75): half stake each side — skip, complex
    if push_mass > 0.5:
        return None, None  # shouldn't happen; sanity guard
    # Renormalize excluding pushes (which return stake)
    total = p_home + p_away
    if total < 1e-9:
        return None, None
    return p_home / total, p_away / total


def candidate_asian_handicap_legs(
    fixtures: List[Dict[str, Any]],
    snapshot_prices: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]],
    lambdas: Dict[str, Dict[str, float]],
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> List[Leg]:
    """Generate Asian handicap legs from cached data when book prices exist."""
    legs: List[Leg] = []
    for fx in fixtures:
        name = fx.get("fixture") or ""
        ftok = _fixture_token(name)
        m1x2 = fx.get("model_1x2") or {}
        teams = _split_fixture(name)
        prices = snapshot_prices.get(ftok, {})
        lam = lambdas.get(name) or {}
        lam_h = lam.get("lambda_home")
        lam_a = lam.get("lambda_away")

        for line_str, line_val, (home_sel, away_sel) in [
            ("-0.5", -0.5, ("%s -0.5" % teams[0], "%s +0.5" % teams[1])),
            ("+0.5", 0.5,  ("%s +0.5" % teams[0], "%s -0.5" % teams[1])),
            ("-1.0", -1.0, ("%s -1.0" % teams[0], "%s +1.0" % teams[1])),
            ("+1.0", 1.0,  ("%s +1.0" % teams[0], "%s -1.0" % teams[1])),
            ("-1.5", -1.5, ("%s -1.5" % teams[0], "%s +1.5" % teams[1])),
            ("+1.5", 1.5,  ("%s +1.5" % teams[0], "%s -1.5" % teams[1])),
        ]:
            # Derive probabilities: prefer 1X2 method for ±0.5, matrix for others
            if abs(abs(line_val) - 0.5) < 0.01:
                p_home, p_away = _ah_prob_from_1x2(m1x2, line_val)
            elif lam_h is not None and lam_a is not None:
                p_home, p_away = _ah_prob_from_matrix(lam_h, lam_a, line_val)
            else:
                continue

            if p_home is None:
                continue

            for team, p, sel in ((teams[0], p_home, home_sel), (teams[1], p_away, away_sel)):
                # Look for AH price in snapshot
                bo = (prices.get(("asian_handicap", sel))
                      or prices.get(("spreads", sel))
                      or prices.get(("alternate_spreads", sel)))
                if not bo:
                    continue
                odds, book = bo
                if odds <= 1.0:
                    continue
                edge = float(p) * _eff(odds, book) - 1.0
                if edge >= min_edge:
                    legs.append(Leg(name, "asian_handicap", sel, float(p), float(odds),
                                    book, edge, False))
    return legs


# ---------------------------------------------------------------------------
# Player scorer legs (via ScorerPricer + cached lambdas + player params)
# ---------------------------------------------------------------------------


def candidate_scorer_legs(
    fixtures: List[Dict[str, Any]],
    snapshot_prices: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]],
    lambdas: Dict[str, Dict[str, float]],
    players: Optional[Dict[str, List[Any]]] = None,
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
    players_path: str = "data/players.json",
) -> List[Leg]:
    """Generate anytime/first-scorer legs when model prob + cached price exist.

    Only fires when:
     1. Cached lambdas provide team goal expectations.
     2. player params exist (``players`` injection, else ``data/players.json``).
     3. A fresh book price is in the odds snapshot for the scorer market.

    ``players`` (team → ``[PlayerParams]``) may be passed directly to bypass the
    on-disk override file; when ``None`` the cached overrides are loaded from
    ``players_path`` (the interactive path — no network).
    """
    try:
        from wca.models.scorers import ScorerPricer, load_player_overrides
    except ImportError:
        return []

    pricer = ScorerPricer()
    player_db = players if players is not None else load_player_overrides(players_path)
    if not player_db:
        return []

    legs: List[Leg] = []
    for fx in fixtures:
        name = fx.get("fixture") or ""
        ftok = _fixture_token(name)
        lam = lambdas.get(name) or {}
        lam_h = lam.get("lambda_home")
        lam_a = lam.get("lambda_away")
        if lam_h is None or lam_a is None:
            continue
        total_lam = lam_h + lam_a
        if total_lam <= 0:
            continue

        prices = snapshot_prices.get(ftok, {})
        teams = _split_fixture(name)

        for team, team_lam in ((teams[0], lam_h), (teams[1], lam_a)):
            for player in player_db.get(team, []):
                try:
                    line = pricer.price_player(player, team_lam, total_lam)
                except Exception:
                    continue

                # Anytime scorer
                for mk, pname in (("player_props", player.name + " to score anytime"),
                                   ("anytime_scorer", player.name)):
                    bo = prices.get((mk, pname)) or prices.get(("player_props", player.name))
                    if bo:
                        odds, book = bo
                        if odds > 1.0:
                            edge = line.p_anytime * _eff(odds, book) - 1.0
                            if edge >= min_edge:
                                legs.append(Leg(
                                    name, "anytime_scorer",
                                    "%s anytime" % player.name,
                                    line.p_anytime, float(odds), book, edge, False))
                        break

                # First scorer
                for mk, pname in (("first_scorer", player.name),
                                   ("player_props", player.name + " first scorer")):
                    bo = prices.get((mk, pname))
                    if bo:
                        odds, book = bo
                        if odds > 1.0 and line.p_first > 0:
                            edge = line.p_first * _eff(odds, book) - 1.0
                            if edge >= min_edge:
                                legs.append(Leg(
                                    name, "first_scorer",
                                    "%s first" % player.name,
                                    line.p_first, float(odds), book, edge, False))
                        break

    return legs


# ---------------------------------------------------------------------------
# Corners / cards legs (via calibrated models + snapshot prices)
# ---------------------------------------------------------------------------


def candidate_prop_legs(
    fixtures: List[Dict[str, Any]],
    snapshot_prices: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]],
    lambdas: Dict[str, Dict[str, float]],
    *,
    min_edge: float = DEFAULT_MIN_EDGE,
) -> List[Leg]:
    """Corners and cards legs from calibrated models, only when book price exists."""
    try:
        from wca.models.props import CornersModel, CardsModel
    except ImportError:
        return []

    corners_model = CornersModel()
    cards_model = CardsModel()
    legs: List[Leg] = []

    for fx in fixtures:
        name = fx.get("fixture") or ""
        ftok = _fixture_token(name)
        lam = lambdas.get(name) or {}
        lam_h = lam.get("lambda_home")
        lam_a = lam.get("lambda_away")
        if lam_h is None or lam_a is None:
            continue
        prices = snapshot_prices.get(ftok, {})

        # Corners over/under
        for line, line_f in (("9.5", 9.5), ("10.5", 10.5), ("8.5", 8.5)):
            p_over = corners_model.prob_over(line_f, lam_h, lam_a)
            p_under = 1.0 - p_over
            for side, p, sel in (
                ("Over", p_over, "Corners Over %s" % line),
                ("Under", p_under, "Corners Under %s" % line),
            ):
                bo = prices.get(("corners", sel)) or prices.get(("corners", side + " " + line))
                if not bo:
                    continue
                odds, book = bo
                if odds <= 1.0:
                    continue
                edge = float(p) * _eff(odds, book) - 1.0
                if edge >= min_edge:
                    legs.append(Leg(name, "corners", sel, float(p), float(odds), book, edge, False))

        # Cards over/under
        for line, line_f in (("3.5", 3.5), ("4.5", 4.5)):
            p_over = cards_model.prob_over(line_f)
            p_under = 1.0 - p_over
            for side, p, sel in (
                ("Over", p_over, "Cards Over %s" % line),
                ("Under", p_under, "Cards Under %s" % line),
            ):
                bo = prices.get(("cards", sel)) or prices.get(("bookings", side + " " + line))
                if not bo:
                    continue
                odds, book = bo
                if odds <= 1.0:
                    continue
                edge = float(p) * _eff(odds, book) - 1.0
                if edge >= min_edge:
                    legs.append(Leg(name, "cards", sel, float(p), float(odds), book, edge, False))

    return legs


# ---------------------------------------------------------------------------
# Exposure — build + gate
# ---------------------------------------------------------------------------


def build_exposure(
    open_bets: List[Dict[str, Any]],
    fx_usd_per_gbp: float = 1.33,
) -> Exposure:
    """Build Exposure from open bets. Accepts simple {match, selection, market}
    dicts (backward compat) or rich dicts with stake/venue/account/source/currency.
    """
    exp = Exposure()
    for b in open_bets or []:
        fixture = b.get("match") or b.get("match_desc") or ""
        sel = b.get("selection") or ""
        ftok = _fixture_token(fixture)

        # Legacy fields
        if ftok:
            exp.fixture_count[ftok] = exp.fixture_count.get(ftok, 0) + 1
        sig = "%s|%s" % (ftok, " ".join(sorted(_toks(sel))))
        exp.held.add(sig)
        for t in set(_toks(fixture)) & set(_toks(sel)):
            exp.team_long[t] = exp.team_long.get(t, 0) + 1

        # Rich fields (optional — only populated when stake info present)
        stake_native = float(b.get("stake") or 0.0)
        venue = str(b.get("platform") or b.get("venue") or "")
        account = str(b.get("account") or "")
        source = str(b.get("source") or "")
        currency = "USD" if "polymarket" in venue.lower() else "GBP"

        if currency == "USD":
            stake_gbp = _usd_to_gbp(stake_native, fx_usd_per_gbp)
        else:
            stake_gbp = stake_native

        if stake_gbp > 0:
            exp.fixture_stake_gbp[ftok] = exp.fixture_stake_gbp.get(ftok, 0.0) + stake_gbp
            # Record per-sig stake (None if unknown, otherwise cumulative)
            prev = exp.held_stakes.get(sig)
            exp.held_stakes[sig] = (prev or 0.0) + stake_gbp
            exp.total_gbp_risk += stake_gbp
            if venue:
                exp.venue_gbp[venue] = exp.venue_gbp.get(venue, 0.0) + stake_gbp
            if account:
                exp.account_gbp[account] = exp.account_gbp.get(account, 0.0) + stake_gbp
            if source:
                exp.source_gbp[source] = exp.source_gbp.get(source, 0.0) + stake_gbp
        else:
            # No stake info → mark as unknown (None) if not already set
            if sig not in exp.held_stakes:
                exp.held_stakes[sig] = None

        exp.bet_details.append({
            "fixture": fixture, "selection": sel,
            "venue": venue, "account": account,
            "source": source, "currency": currency,
            "stake": stake_native, "stake_gbp": stake_gbp,
        })

    return exp


def _leg_held(leg: Leg, exp: Exposure) -> bool:
    """Legacy helper kept for tests — checks held set only."""
    sig = _make_sig(leg)
    return sig in exp.held


def _leg_concentration(leg: Leg, exp: Exposure) -> int:
    c = exp.fixture_count.get(_fixture_token(leg.fixture), 0)
    for t in _toks(leg.selection):
        c += exp.team_long.get(t, 0)
    return c


def _leg_passes_gate(
    leg: Leg,
    exp: Exposure,
    bankroll: float,
    kelly_fraction: float = KELLY_FRACTION,
) -> Tuple[bool, str]:
    """Incremental portfolio gate. Returns (passes, reason_if_blocked).

    Replaces the old blanket `_leg_held()` removal with:
     1. Same-selection check: if held with a known stake at/above Kelly optimal
        → block (adding more would be over-Kelly). If stake unknown → conservative
        block (same as before). If held at less than Kelly → allow (positive
        incremental EV).
     2. Fixture correlated cap: if adding this leg would push the total GBP
        at risk on that fixture above ``FIXTURE_CAP_FRACTION * bankroll`` → block.
    """
    ftok = _fixture_token(leg.fixture)
    sig = _make_sig(leg)
    proposed_stake = kelly_fraction * _kelly_fraction(leg.model_prob, leg.odds) * bankroll

    # Check 1: exact same selection already held
    if sig in exp.held:
        existing_stake = exp.held_stakes.get(sig)
        if existing_stake is None:
            # No stake info → conservative: treat as at Kelly → block
            return False, "already held (no stake info — conservative drop)"
        kelly_opt = kelly_fraction * _kelly_fraction(leg.model_prob, leg.odds) * bankroll
        if kelly_opt <= 0:
            return False, "already held, zero Kelly optimal"
        ratio = existing_stake / kelly_opt
        if ratio >= 0.95:
            return False, "already held at %.0f%% of Kelly" % (ratio * 100)
        # Below optimal — incremental EV positive: allow
        return True, "add (%.0f%% of Kelly staked; room for more)" % (ratio * 100)

    # Check 2: fixture correlated cap
    existing_fixture = exp.fixture_stake_gbp.get(ftok, 0.0)
    total = existing_fixture + proposed_stake
    cap = bankroll * FIXTURE_CAP_FRACTION
    if total > cap * 1.05 and existing_fixture > 0:
        return False, "fixture cap: £%.0f + £%.0f > £%.0f cap" % (
            existing_fixture, proposed_stake, cap)

    return True, ""


def _exposure_classify(legs: List[Leg], exp: Exposure) -> str:
    """Classify the proposed acca's portfolio action."""
    has_overlap = any(exp.fixture_count.get(_fixture_token(L.fixture), 0) > 0 for L in legs)
    has_offset = any(
        exp.team_long.get(t, 0) > 0 and
        any(exp.team_long.get(t2, 0) > 0 for t2 in _toks(L.selection) if t2 != t)
        for L in legs
        for t in _toks(L.selection)
    )
    if has_offset:
        return "hedge"
    if has_overlap:
        return "add"
    return "diversify"


# ---------------------------------------------------------------------------
# Acca assembly (pure)
# ---------------------------------------------------------------------------


def _combined(legs: List[Leg]) -> Tuple[float, float, float]:
    o, p = 1.0, 1.0
    for L in legs:
        o *= L.odds
        p *= L.model_prob
    edge = o_eff_prod(legs) * p - 1.0
    return o, p, edge


def o_eff_prod(legs: List[Leg]) -> float:
    prod = 1.0
    for L in legs:
        prod *= _eff(L.odds, L.book)
    return prod


def assemble_accas(
    legs: List[Leg],
    exposure: Optional[Exposure] = None,
    *,
    mode: str = "value",
    min_legs: int = 2,
    max_legs: int = 4,
    max_accas: int = 4,
    max_combined_odds: Optional[float] = None,
    bankroll: float = DEFAULT_BANKROLL,
    kelly_fraction: float = KELLY_FRACTION,
) -> List[Acca]:
    """Combine +EV legs into accas, one selection per match, moneyline-first.

    Uses the incremental portfolio gate instead of the old blanket held-removal:
    a leg that is already held but at below-Kelly stake is allowed through
    (positive incremental EV). A leg with unknown stake is conservatively blocked
    (same as the old behavior for test compatibility).
    """
    exposure = exposure or Exposure()

    # Apply incremental EV + cap gate (replaces blanket _leg_held removal)
    legs = [L for L in legs
            if _leg_passes_gate(L, exposure, bankroll, kelly_fraction)[0]]

    # Longshot policy (canonical, wca.selection). Only the explicit "longshot"
    # mode (free-bet / lottery book) may include <25c legs; every CASH mode
    # drops them via longshot_no_cash — this is where the old 0.12-0.25 legs
    # that used to be staged for cash now stop. LONGSHOT_ODDS still guards
    # degenerate high-odds prices (kept in Leg.is_longshot).
    if mode != "longshot":
        legs = [L for L in legs
                if not longshot_no_cash(L.model_prob) and not L.is_longshot]
    if not legs:
        return []

    low_win = mode not in ("edge", "longshot", "hedge")
    if max_combined_odds is None and low_win:
        max_combined_odds = VALUE_MAX_COMBINED

    def rank_key(L: Leg):
        # Canonical desk ordering (wca.selection; user 2026-07-07): model-prob
        # bucket first (moneyline > mid > longshot — replaces the old market-TYPE
        # is_moneyline flag), then further-out fixtures, then EV; portfolio
        # concentration stays as the final tie-break.
        conc = _leg_concentration(L, exposure)
        return (bucket_rank(L.model_prob), -L.hours_out, -L.edge, conc)

    legs = sorted(legs, key=rank_key)

    best_by_fixture: Dict[str, Leg] = {}
    for L in legs:
        k = _fixture_token(L.fixture)
        if k not in best_by_fixture:
            best_by_fixture[k] = L
    anchors = sorted(best_by_fixture.values(), key=rank_key)
    if len(anchors) < min_legs:
        return []

    accas: List[Acca] = []
    seen = set()
    for n in range(min_legs, min(len(anchors), max_legs) + 1):
        chosen = anchors[:n]
        o, p, edge = _combined(chosen)
        if max_combined_odds and o > max_combined_odds:
            break
        if edge <= 0:
            continue
        sig = tuple(_fixture_token(L.fixture) + L.selection for L in chosen)
        if sig in seen:
            continue
        seen.add(sig)
        stake = round(kelly_fraction * _kelly_fraction(p, o) * bankroll, 2)
        action = _exposure_classify(chosen, exposure)
        note = _exposure_note(chosen, exposure)
        downside = stake  # worst case: all legs lose → lose stake
        accas.append(Acca(chosen, round(o, 2), p, edge, stake,
                          note=note, action=action, downside_gbp=downside))
        if len(accas) >= max_accas:
            break
    return accas


def _exposure_note(legs: List[Leg], exp: Exposure) -> str:
    adds = [L.fixture for L in legs if _leg_concentration(L, exp) > 0]
    if adds:
        return "adds to existing exposure on: " + ", ".join(sorted(set(adds)))
    return "diversifies — no overlap with current book"


# ---------------------------------------------------------------------------
# Joint probability for same-game legs
# ---------------------------------------------------------------------------


def _joint_prob_same_game(
    legs: List[Leg],
    lam_h: float,
    lam_a: float,
) -> Optional[float]:
    """Joint win probability for multiple legs on the SAME fixture via scoreline.

    Returns None when the scoreline matrix is unavailable (caller falls back to
    the independent-product approximation with a warning).
    """
    try:
        from wca.exposure_corr import scoreline_matrix
        from wca.exposure_corr import settle_on_scoreline as _settle
    except ImportError:
        return None

    if not legs:
        return None
    fixture = legs[0].fixture
    home, away = _split_fixture(fixture)
    try:
        mat = scoreline_matrix(float(lam_h), float(lam_a))
    except Exception:
        return None

    joint_p = 0.0
    for h in range(mat.shape[0]):
        for a in range(mat.shape[1]):
            cell_p = float(mat[h, a])
            if cell_p < 1e-12:
                continue
            # Build a minimal bet dict for each leg
            all_win = True
            for leg in legs:
                fake = {
                    "type": leg.market,
                    "market": leg.market,
                    "selection": leg.selection,
                    "label": leg.selection,
                    "stake": 1.0,
                    "odds": leg.odds,
                    "free": False,
                }
                pnl = _settle(fake, home, away, h, a)
                if pnl <= 0:
                    all_win = False
                    break
            if all_win:
                joint_p += cell_p
    return joint_p


# ---------------------------------------------------------------------------
# Promo mode — DB-backed + freshness gating
# ---------------------------------------------------------------------------


@dataclass
class Offer:
    name: str
    venue: str
    account: str
    min_legs: int
    min_leg_odds: float
    min_combined_odds: float
    kind: str                        # "snr_free" | "lose_free" | "qualifier"
    max_stake: float
    game_restrict: Optional[str] = None
    expiry: Optional[str] = None     # ISO date string or human label
    terms_summary: Optional[str] = None


#: Hard-coded fallback offers (used when the promotions DB is unavailable).
OFFER_TEMPLATES: List[Offer] = [
    Offer("Betfair SB free-bet acca", "betfair_sportsbook", "1", 3, 1.5, 0.0, "snr_free", 10.0),
    Offer("Paddy Eng-Gha money-back", "paddypower", "1", 3, 2.0, 0.0, "lose_free", 50.0, "england ghana"),
    Offer("Betfred ENG/SCOT builder", "betfred", "1", 3, 0.0, 4.0, "qualifier", 10.0),
]

SNR_RETENTION = 0.70


def _db_row_to_offer(row: Any) -> Optional[Offer]:
    """Convert a promotions DB row to an Offer if it has parseable terms."""
    try:
        site = str(row["site"] or "")
        title = str(row["title"] or row["description"] or "")
        promo_type = str(row["promo_type"] or "")
        terms = str(row["terms"] or "")

        if promo_type not in ("ongoing", "signup"):
            return None

        # Parse structured signup terms (k=v; k=v format)
        parsed: Dict[str, str] = {}
        for part in terms.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                parsed[k.strip()] = v.strip()

        min_odds_str = parsed.get("min_odds", "")
        free_val_str = parsed.get("free_bet_value", "")
        expiry = parsed.get("expiry", "") or None

        # Best-effort numeric extraction
        def _extract_float(s: str) -> Optional[float]:
            m = re.search(r"\d+(?:\.\d+)?", s)
            return float(m.group()) if m else None

        min_odds = _extract_float(min_odds_str) or 0.0
        # Convert fractional (e.g., "1/2" = 0.5 + 1 = 1.5) to decimal
        frac = re.match(r"(\d+)/(\d+)", min_odds_str)
        if frac:
            min_odds = int(frac.group(1)) / int(frac.group(2)) + 1.0

        max_stake = _extract_float(free_val_str) or 0.0

        # Classify kind: signup + money back → lose_free; free bet → snr_free
        title_low = title.lower()
        if "money back" in title_low or "money-back" in title_low or "refund" in title_low:
            kind = "lose_free"
        elif "free bet" in title_low or "free bets" in title_low:
            kind = "snr_free"
        elif "qualifier" in title_low or "acca" in title_low:
            kind = "qualifier"
        else:
            return None  # insufficient info to classify

        return Offer(
            name=title[:60],
            venue=site.lower().replace(" ", "_"),
            account="1",
            min_legs=3,
            min_leg_odds=min_odds,
            min_combined_odds=0.0,
            kind=kind,
            max_stake=max_stake,
            expiry=expiry,
            terms_summary=terms[:80] if terms else None,
        )
    except Exception:
        return None


def _load_db_offers(
    db_path: str,
    stale_hours: float = PROMO_STALE_HOURS,
) -> Tuple[List[Offer], List[str], bool]:
    """Load active promo offers from the DB. Returns (offers, audit_lines, promo_required).

    ``audit_lines``: compact per-site freshness summary for display.
    ``promo_required``: True when any site is stale/blocked → PROMO CHECK REQUIRED.
    """
    offers: List[Offer] = []
    audit_lines: List[str] = []
    promo_required = False
    now = datetime.now(timezone.utc)

    try:
        from wca.promos import active_promotions, latest_snapshot_per_site
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row

        snapshots = latest_snapshot_per_site(con)
        for site, row in sorted(snapshots.items()):
            fetch_status = str(row["fetch_status"] or "")
            ts = str(row["ts_utc"] or "")
            age = _snapshot_age_hours(ts, now)

            if fetch_status == "blocked":
                audit_lines.append("%s: BLOCKED" % site)
                promo_required = True
            elif fetch_status == "error":
                audit_lines.append("%s: ERROR" % site)
                promo_required = True
            elif age is None or age > stale_hours:
                audit_lines.append("%s: %.0fh⚠" % (site, age if age is not None else 999))
                promo_required = True
            else:
                audit_lines.append("%s: %.0fh✓" % (site, age))

        active = active_promotions(con)
        for row in active:
            offer = _db_row_to_offer(row)
            if offer:
                offers.append(offer)
        con.close()
    except Exception:
        pass

    return offers, audit_lines, promo_required


def build_promo_accas(
    legs: List[Leg],
    offers: Optional[List[Offer]] = None,
    exposure: Optional[Exposure] = None,
    lambdas: Optional[Dict[str, Dict[str, float]]] = None,
) -> List[Acca]:
    """Per offer, build a qualifying acca optimised for the offer's value metric.

    SNR free bets maximise combined odds; lose->free insurance sizes to max;
    qualifiers minimise legs/odds to clear the floor. Same-game legs use joint
    probability via the scoreline matrix when lambdas are available.
    """
    offers = offers if offers is not None else OFFER_TEMPLATES
    exposure = exposure or Exposure()
    lambdas = lambdas or {}
    out: List[Acca] = []
    for off in offers:
        pool = list(legs)
        if off.game_restrict:
            want = set(off.game_restrict.split())
            pool = [L for L in pool if want <= set(_toks(L.fixture))]
        if off.min_leg_odds:
            pool = [L for L in pool if L.odds >= off.min_leg_odds]

        best_by_fixture: Dict[str, Leg] = {}
        if off.game_restrict:
            seen_1x2 = False
            kept: List[Leg] = []
            for L in sorted(pool, key=lambda x: -x.odds):
                if L.market == "1X2":
                    if seen_1x2:
                        continue
                    seen_1x2 = True
                kept.append(L)
            pool = kept
        else:
            for L in sorted(pool, key=lambda x: -x.odds):
                k = _fixture_token(L.fixture)
                if k not in best_by_fixture:
                    best_by_fixture[k] = L
            pool = list(best_by_fixture.values())

        if off.kind == "qualifier" and off.min_combined_odds:
            pool = sorted(pool, key=lambda x: x.odds)
        else:
            pool = sorted(pool, key=lambda x: -x.edge)

        chosen: List[Leg] = []
        for L in pool:
            chosen.append(L)
            o = _prod(chosen)
            if len(chosen) >= off.min_legs and (
                not off.min_combined_odds or o >= off.min_combined_odds
            ):
                break
        if len(chosen) < off.min_legs:
            continue
        o = _prod(chosen)
        if off.min_combined_odds and o < off.min_combined_odds:
            continue

        # Joint probability: use scoreline matrix for same-game legs.
        same_game = (off.game_restrict is not None and
                     len({_fixture_token(L.fixture) for L in chosen}) == 1)
        p_joint: Optional[float] = None
        if same_game and chosen:
            ftok = _fixture_token(chosen[0].fixture)
            lam = next((lambdas[f] for f in lambdas if _fixture_token(f) == ftok), None)
            if lam:
                p_joint = _joint_prob_same_game(
                    chosen, lam["lambda_home"], lam["lambda_away"])

        if p_joint is not None:
            p = p_joint
            prob_note = " (joint via matrix)"
        else:
            p = 1.0
            for L in chosen:
                p *= L.model_prob
            prob_note = " (independent approx)" if same_game else ""

        if off.kind == "snr_free":
            note = "SNR free bet @ %s: retains ~£%.0f of value (%.0f%% of £%.0f)%s" % (
                round(o, 1), SNR_RETENTION * off.max_stake * (1 - 1 / o) if o > 1 else 0,
                SNR_RETENTION * 100, off.max_stake, prob_note)
        elif off.kind == "lose_free":
            eff_risk = off.max_stake * (1 - SNR_RETENTION)
            note = "stake £%.0f, lose->free bet: effective risk ~£%.0f; combined %s%s" % (
                off.max_stake, eff_risk, round(o, 1), prob_note)
        else:
            note = "qualifier: 3+ legs @ combined %s (clears %s floor)%s" % (
                round(o, 1), off.min_combined_odds or off.min_leg_odds, prob_note)
        if off.game_restrict:
            note += " | same-game; confirm each leg >=%.1f on the app" % off.min_leg_odds
        if off.expiry:
            note += " | exp: %s" % off.expiry
        lbl = "%s [%s a%s]" % (off.name, off.venue, off.account)
        out.append(Acca(chosen, round(o, 2), p, _eff_edge(chosen),
                        off.max_stake, label=lbl, note=note))
    return out


def _prod(legs: List[Leg]) -> float:
    o = 1.0
    for L in legs:
        o *= L.odds
    return o


def _eff_edge(legs: List[Leg]) -> float:
    return o_eff_prod(legs) * _pprod(legs) - 1.0


def _pprod(legs: List[Leg]) -> float:
    p = 1.0
    for L in legs:
        p *= L.model_prob
    return p


# ---------------------------------------------------------------------------
# Polymarket alternatives (settlement-semantics check)
# ---------------------------------------------------------------------------


def _pm_matches_market(market: str, pm_question: str) -> bool:
    """True if the Polymarket market's settlement semantics match the leg market.

    Only 1X2 / DNB / totals / BTTS match their PM counterparts. Player props,
    corners and cards have different settlement or no PM equivalent.
    """
    mkt = market.lower()
    q = pm_question.lower()
    if mkt in ("1x2", "h2h", "dnb", "draw_no_bet"):
        return ("will" in q and ("win" in q or "draw" in q)) or ("match result" in q)
    if mkt == "totals":
        return "goal" in q and ("over" in q or "under" in q)
    if mkt == "btts":
        return "both teams" in q and "score" in q
    return False  # corners, cards, player props: semantics don't match


def _load_pm_alternatives(
    db_path: str,
    legs: List[Leg],
) -> Dict[str, Dict[str, Any]]:
    """Read Polymarket prices for matched legs from the pm_inventory cache table.

    Returns {leg_sig: {price, token_id, question, neg_risk}} for matching legs.
    """
    result: Dict[str, Dict[str, Any]] = {}
    try:
        con = sqlite3.connect(db_path)
        for leg in legs:
            if leg.market not in ("1X2", "dnb", "totals", "btts"):
                continue
            ftok = _fixture_token(leg.fixture)
            sel_toks = " ".join(sorted(_toks(leg.selection)))
            # Query pm_inventory table if it exists
            try:
                rows = con.execute(
                    "SELECT question, price, token_id, neg_risk, settlement_rules "
                    "FROM pm_inventory "
                    "WHERE fixture_token=? AND outcome_token=?",
                    (ftok, sel_toks),
                ).fetchall()
                for row in rows:
                    if _pm_matches_market(leg.market, str(row[0] or "")):
                        price = float(row[1] or 0)
                        if 0 < price < 1:
                            pm_odds = 1.0 / price * (1.0 - PM_FEE)
                            sig = _make_sig(leg)
                            result[sig] = {
                                "price": price,
                                "pm_odds": round(pm_odds, 3),
                                "token_id": str(row[2] or ""),
                                "question": str(row[0] or ""),
                                "neg_risk": bool(row[3]),
                                "settlement_rules": str(row[4] or ""),
                            }
            except sqlite3.OperationalError:
                pass  # pm_inventory table not yet created
        con.close()
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Unsupported markets listing
# ---------------------------------------------------------------------------


def _list_unsupported(
    snapshot_prices: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]],
    fixtures: List[Dict[str, Any]],
    lambdas: Dict[str, Dict[str, float]],
) -> List[str]:
    """List market types seen in the snapshot that we cannot currently price."""
    known = {
        "totals", "btts", "draw_no_bet", "h2h", "1x2", "moneyline",
        "asian_handicap", "spreads", "alternate_spreads",
        "corners", "cards", "bookings",
        "player_props", "anytime_scorer", "first_scorer",
    }
    unsupported_set = set()
    for prices in snapshot_prices.values():
        for market, sel in prices:
            norm_m = market.lower().replace(" ", "_").replace("/", "_")
            if norm_m not in known:
                unsupported_set.add(market)
    # Also flag AH integer lines when no lambdas available
    has_any_lambdas = bool(lambdas)
    if not has_any_lambdas:
        for prices in snapshot_prices.values():
            for mk, sel in prices:
                if mk in ("asian_handicap", "spreads") and re.search(r"\s-?[12]\b", sel):
                    unsupported_set.add("asian_handicap integer lines (no cached lambdas)")
                    break
    return sorted(unsupported_set)


# ---------------------------------------------------------------------------
# IO loaders
# ---------------------------------------------------------------------------


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _pct_to_frac(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k in ("home", "draw", "away"):
        v = d.get(k)
        out[k] = (v / 100.0 if isinstance(v, (int, float)) and v > 1 else v)
    return out


def _load_snapshot_derivatives(
    db_path: str,
    now: Optional[datetime] = None,
    max_age_hours: float = _SNAPSHOT_MAX_AGE_HOURS,
) -> Dict[str, Dict[Tuple[str, str], Tuple[float, str]]]:
    """Best book prices per fixture-token from the latest snapshot.

    Extended to include asian_handicap / player_props / corners / cards
    markets in addition to the original totals/btts.

    Staleness guard: the newest snapshot's age is checked against
    ``max_age_hours`` (default :data:`wca.snapshot_freshness.DEFAULT_MAX_AGE_HOURS`).
    When the latest snapshot is stale a WARNING is logged and **no** derivative
    prices are returned, so EV is never quoted off hours-old odds.  ``now`` is
    injectable for deterministic tests; it defaults to the wall clock.
    """
    out: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]] = {}
    _MARKETS = (
        "totals", "btts", "draw_no_bet",
        "asian_handicap", "spreads", "alternate_spreads",
        "player_props", "anytime_scorer", "first_scorer",
        "corners", "cards", "bookings",
    )
    try:
        con = sqlite3.connect(db_path)
        m = con.execute("SELECT MAX(ts_utc) FROM odds_snapshots").fetchone()[0]
        if not m:
            return out
        freshness = _check_snapshot_freshness(
            m, now=now, max_age_hours=max_age_hours,
            context="accas odds_snapshots derivatives",
        )
        if freshness.is_stale:
            con.close()
            return out
        placeholders = ",".join("?" * len(_MARKETS))
        rows = con.execute(
            "SELECT market, selection, decimal_odds, raw, source FROM odds_snapshots "
            "WHERE ts_utc=? AND market IN (%s)" % placeholders,
            (m,) + _MARKETS,
        ).fetchall()
        for market, sel, odds, raw, source in rows:
            try:
                r = json.loads(raw)
                fixture = "%s vs %s" % (r.get("home_team"), r.get("away_team"))
            except Exception:
                continue
            ftok = _fixture_token(fixture)
            book = (r.get("bookmaker") if isinstance(r, dict) else None) or source or "book"
            key = (market, sel)
            cur = out.setdefault(ftok, {})
            if key not in cur or odds > cur[key][0]:
                cur[key] = (float(odds), book)
        con.close()
    except Exception:
        pass
    return out


def load_fixtures(
    preds_path: str = "data/model_predictions.json",
    scores_path: str = "site/scores_data.json",
    db_path: str = "data/wca.db",
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, Dict[Tuple[str, str], Tuple[float, str]]],
    Dict[str, Dict[str, float]],
]:
    """Load per-fixture model probs + best 1X2 book prices + snapshot derivatives
    + cached goal lambdas.

    Returns (fixtures, snapshot_prices, lambdas).
    """
    preds = _read_json(preds_path)
    scores = _read_json(scores_path)
    finished = _finished_tokens()

    # Model 1X2 by fixture token
    model_1x2: Dict[str, Dict[str, float]] = {}
    for fx in (preds.get("fixtures") if isinstance(preds, dict) else preds) or []:
        m = fx.get("model") or {}
        if m:
            model_1x2[_fixture_token(fx.get("fixture"))] = {
                "home": m.get("home"), "draw": m.get("draw"), "away": m.get("away")}

    # Lambdas from predictions JSON
    lambdas: Dict[str, Dict[str, float]] = {}
    for fx in (preds.get("fixtures") if isinstance(preds, dict) else preds) or []:
        name = fx.get("fixture") or ""
        lh = fx.get("lambda_home")
        la = fx.get("lambda_away")
        if lh is not None and la is not None:
            lambdas[name] = {"lambda_home": float(lh), "lambda_away": float(la)}

    fixtures: List[Dict[str, Any]] = []
    for f in (scores.get("fixtures") if isinstance(scores, dict) else []) or []:
        name = f.get("fixture") or ""
        if _is_finished(name, finished):
            continue
        ftok = _fixture_token(name)
        best = {"home": None, "draw": None, "away": None}
        for v in f.get("venues") or []:
            sp = v.get("selection_prices") or {}
            book = v.get("venue")
            for k in ("home", "draw", "away"):
                o = sp.get(k)
                if o and (best[k] is None or o > best[k][0]):
                    best[k] = (float(o), book)
        ou = f.get("over_under") or {}
        over = ou.get("over")
        under = ou.get("under")
        btts = f.get("btts")
        fixtures.append({
            "fixture": name,
            # Kickoff (ISO/UTC) for the canonical further-out selection key.
            "commence_time": f.get("kickoff") or f.get("commence_time") or "",
            "model_1x2": model_1x2.get(ftok) or _pct_to_frac(f.get("model_1x2") or {}),
            "best_1x2": best,
            "over_under": {
                "line": ou.get("line", 2.5),
                "over": (over / 100.0) if isinstance(over, (int, float)) else None,
                "under": (under / 100.0) if isinstance(under, (int, float)) else None,
            },
            "btts": (btts / 100.0) if isinstance(btts, (int, float)) else None,
        })

    snapshot_prices = _load_snapshot_derivatives(db_path)
    return fixtures, snapshot_prices, lambdas


def load_open_bets(
    db_path: str = "data/wca.db",
    site_data: str = "site/data.json",
) -> List[Dict[str, Any]]:
    """Merged exposure: ledger open bets + live Polymarket positions.

    Extended to include stake/platform/account/source for rich exposure tracking.
    """
    out: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(db_path)
        for row in con.execute(
                "SELECT match_id, match_desc, selection, market, "
                "stake, platform, account, source "
                "FROM bets WHERE status='open'"):
            out.append({
                "match": row[1], "selection": row[2], "market": row[3],
                "stake": float(row[4] or 0.0),
                "platform": str(row[5] or ""),
                "account": str(row[6] or ""),
                "source": str(row[7] or ""),
            })
        con.close()
    except Exception:
        pass
    data = _read_json(site_data)
    for p in (data.get("positions") if isinstance(data, dict) else []) or []:
        if str(p.get("id", "")).startswith("pm-"):
            out.append({
                "match": p.get("match") or "",
                "selection": p.get("selection") or "",
                "market": p.get("market") or "",
                "stake": float(p.get("stake") or 0.0),
                "platform": "polymarket",
                "account": "pm",
                "source": "model",
            })
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_accas(
    *,
    preds_path: str = "data/model_predictions.json",
    scores_path: str = "site/scores_data.json",
    db_path: str = "data/wca.db",
    site_data: str = "site/data.json",
    players_path: str = "data/players.json",
    mode: str = "value",
    min_edge: Optional[float] = None,
    bankroll: Optional[float] = None,
    allow_network: bool = False,
) -> Dict[str, Any]:
    """Orchestrate the /accas pipeline.

    Interactive path (no live API calls):
     1. Load cached model probs + lambdas from model_predictions.json.
     2. Load cached odds snapshot from SQLite.
     3. Resolve bankroll from ledger CLV ladder (fallback DEFAULT_BANKROLL).
     4. Load FX rate (offline cached fallback; ``allow_network=True`` opts into
        a live fetch from non-interactive callers).
     5. Build candidate legs across all supported markets.
     6. Build rich exposure from open bets.
     7. Assemble accas / promo accas.
     8. Load PM alternatives from pm_inventory cache.
     9. Load promo audit from promotions table.
    """
    fixtures, snap, lambdas = load_fixtures(preds_path, scores_path, db_path)
    open_bets = load_open_bets(db_path, site_data)
    fx_rate, fx_source = _get_fx(allow_network=allow_network)
    exposure = build_exposure(open_bets, fx_usd_per_gbp=fx_rate)

    # Bankroll resolution
    if bankroll is not None:
        resolved_bankroll = float(bankroll)
        kf = KELLY_FRACTION
        bankroll_reason = "manual override £%.0f" % resolved_bankroll
    else:
        resolved_bankroll, kf, bankroll_reason = _resolve_bankroll(db_path, DEFAULT_BANKROLL)

    # Edge gate
    if min_edge is None:
        min_edge = 0.0 if mode in ("value", "low_win") else DEFAULT_MIN_EDGE

    # All candidate legs
    legs = candidate_legs(fixtures, snap, min_edge=min_edge)
    legs += candidate_asian_handicap_legs(fixtures, snap, lambdas, min_edge=min_edge)
    legs += candidate_scorer_legs(fixtures, snap, lambdas, min_edge=min_edge,
                                  players_path=players_path)
    legs += candidate_prop_legs(fixtures, snap, lambdas, min_edge=min_edge)

    # Stamp each leg's kickoff (the canonical further-out selection key) from the
    # fixtures feed, keyed by fixture name — the candidate builders don't carry
    # commence_time on the Leg itself.
    _kick = {fx.get("fixture") or "": (fx.get("commence_time") or "")
             for fx in fixtures}
    for L in legs:
        if not L.commence_time:
            L.commence_time = _kick.get(L.fixture, "")

    # Promo mode: load DB offers (fallback to templates)
    db_offers, promo_audit, promo_required = _load_db_offers(db_path)
    promo_offers = db_offers if db_offers else OFFER_TEMPLATES

    if mode == "promo":
        accas = build_promo_accas(legs, promo_offers, exposure, lambdas)
    else:
        accas = assemble_accas(
            legs, exposure, mode=mode,
            bankroll=resolved_bankroll, kelly_fraction=kf)

    # Cross-acca ranking: canonical desk key on the representative (anchor) leg
    # — model-prob bucket, then further-out fixture, then EV (wca.selection;
    # user 2026-07-07). The anchor is the first leg (already ranked first within
    # the acca). Accas with no legs fall back to their combined edge only.
    def _acca_key(a: Acca) -> Tuple[int, float, float]:
        anchor = a.legs[0] if a.legs else None
        if anchor is None:
            return (3, 0.0, -float(a.edge or 0.0))
        return (bucket_rank(anchor.model_prob), -anchor.hours_out, -float(a.edge or 0.0))

    accas.sort(key=_acca_key)

    # PM alternatives for the top recommendations
    pm_alts = _load_pm_alternatives(db_path, legs[:20])

    # Unsupported markets
    unsupported = _list_unsupported(snap, fixtures, lambdas)

    return {
        "mode": mode,
        "accas": accas,
        "n_legs": len(legs),
        "n_fixtures": len(fixtures),
        "bankroll": resolved_bankroll,
        "bankroll_reason": bankroll_reason,
        "kelly_fraction": kf,
        "fx_rate": fx_rate,
        "fx_source": fx_source,
        "exposure": exposure,
        "promo_audit": promo_audit,
        "promo_required": promo_required,
        "pm_alternatives": pm_alts,
        "unsupported": unsupported,
    }


# ---------------------------------------------------------------------------
# Formatting — ≤4096 chars with deterministic truncation
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = MSG_LIMIT) -> str:
    """Deterministically truncate to ≤limit chars, cutting at a newline."""
    if len(text) <= limit:
        return text
    cutpoint = text.rfind("\n", 0, limit - 30)
    if cutpoint < 50:
        cutpoint = limit - 30
    return text[:cutpoint] + "\n…_(truncated)_"


def _fmt_legs_count(a: Acca) -> str:
    return "%d-leg" % len(a.legs)


def _fmt_exposure_summary(exposure: Exposure, fx_rate: float, fx_source: str) -> List[str]:
    """Compact held-exposure block: venue/account/source/currency."""
    lines: List[str] = []
    if exposure.total_gbp_risk <= 0:
        return lines
    lines.append("*Held exposure:* £%.0f at risk" % exposure.total_gbp_risk)
    if exposure.venue_gbp:
        v_parts = ["%s £%.0f" % (k, v) for k, v in sorted(exposure.venue_gbp.items()) if v > 0]
        if v_parts:
            lines.append("  by venue: " + "  ".join(v_parts))
    if exposure.source_gbp:
        s_parts = ["%s £%.0f" % (k, v) for k, v in sorted(exposure.source_gbp.items()) if v > 0]
        if s_parts:
            lines.append("  by source: " + "  ".join(s_parts))
    lines.append("  (FX: 1 GBP = %.3f USD [%s])" % (fx_rate, fx_source))
    return lines


def _fmt_acca(i: int, a: Acca, mode: str, pm_alts: Dict[str, Any]) -> List[str]:
    """Format one acca into lines. Includes per-leg PM alternative if available."""
    lines: List[str] = []
    head = "*%s*" % (a.label or "Acca %d" % i)
    # Percent convention (ruling 2026-07-08): combined price as its implied
    # (break-even) %, the acca's model % next to it, EV marker on the header.
    lines.append("%s — *%s* — combined impl *%s* vs model *%s* %s  [%s]" % (
        head, _fmt_legs_count(a), implied_pct(a.combined_odds),
        pct(a.model_prob), ev_marker(a.edge),
        {"add": "adds exposure", "hedge": "hedges book", "diversify": "diversifies"}.get(
            a.action, a.action)))

    for L in a.legs:
        pm = pm_alts.get(_make_sig(L))
        pm_str = ""
        if pm and _pm_matches_market(L.market, pm.get("question", "")):
            pm_str = "  PM: %s" % implied_pct(pm["pm_odds"])
        lines.append(
            "   • [%s] %s — %s (%s) — mkt %s impl%s  [model %s · EV %s %s]" % (
                bucket_tag(L.model_prob), L.fixture, L.selection, L.market,
                implied_pct(L.odds),
                " via %s" % L.book if L.book else "",
                pct(L.model_prob, 0), ev_str(L.edge, 0), ev_marker(L.edge))
            + pm_str)

    if mode == "promo":
        lines.append("   _%s_" % a.note)
    else:
        existing_risk = 0.0  # shown in note
        lines.append(
            "   model %s · net EV *%s* %s · ¼-Kelly *£%.2f*" % (
                pct(a.model_prob), ev_str(a.edge, 0), ev_marker(a.edge), a.stake))
        if a.downside_gbp > 0:
            lines.append("   downside if all lose: £%.2f" % a.downside_gbp)
        lines.append("   _%s_" % a.note)
    return lines


def format_accas(result: Dict[str, Any]) -> str:
    """Format the /accas result into a Telegram Markdown reply ≤4096 chars."""
    mode = result.get("mode", "value")
    accas = result.get("accas") or []
    promo_audit = result.get("promo_audit") or []
    promo_required = bool(result.get("promo_required"))
    pm_alts = result.get("pm_alternatives") or {}
    unsupported = result.get("unsupported") or []
    exposure: Optional[Exposure] = result.get("exposure")
    bankroll = result.get("bankroll") or DEFAULT_BANKROLL
    bankroll_reason = result.get("bankroll_reason") or ""
    fx_rate = result.get("fx_rate") or 1.33
    fx_source = result.get("fx_source") or "fallback"

    title = {
        "value": "Accas — low-level win (favourites, +EV)",
        "edge": "Accas — max edge (high-edge underdogs)",
        "hedge": "Accas — hedge the book",
        "longshot": "Accas — longshots (>=4.0 legs)",
        "promo": "Accas — promo / offer extraction",
    }.get(mode, "Accas")

    lines: List[str] = ["\U0001f3af *%s*" % title, ""]

    # Promo audit (always shown in promo mode; compact in other modes if issues)
    if promo_audit and (mode == "promo" or promo_required):
        lines.append("*Promo check:* " + "  ".join(promo_audit[:6]))
        if promo_required:
            lines.append("⚠ *PROMO CHECK REQUIRED* — stale/blocked sites above.")
            if mode == "promo":
                lines.append("_No new-risk stake recommended for venues with stale data._")
        lines.append("")

    # Held exposure summary
    if exposure and exposure.total_gbp_risk > 0:
        lines.extend(_fmt_exposure_summary(exposure, fx_rate, fx_source))
        lines.append("")

    # No accas
    if not accas:
        if promo_required and mode == "promo":
            lines.append("*NO BET* — promo data stale/blocked. Verify manually.")
        elif mode in ("value", "low_win"):
            lines.append(
                "NO BET — no +EV favourite legs at combined implied ≥%.0f%% "
                "(≤%.0fx). Try `/accas edge` for high-edge underdogs."
                % (100.0 / VALUE_MAX_COMBINED, VALUE_MAX_COMBINED))
        else:
            lines.append(
                "NO BET — no qualifying accas cleared the +EV gate "
                "(or all overlap your book). Try `/accas longshot`.")
        # Unsupported markets hint
        if unsupported:
            lines.append("")
            lines.append("_Unsupported (no model/price): %s_" % ", ".join(unsupported[:4]))
        # Bankroll / FX context — shown even with no qualifying bet so the
        # sizing base and cross-currency rate are always visible.
        lines.append("")
        lines.append("_Bankroll £%.0f · FX 1 GBP = %.3f USD [%s]_"
                     % (bankroll, fx_rate, fx_source))
        return _truncate("\n".join(lines))

    # Accas
    for i, a in enumerate(accas, 1):
        acca_lines = _fmt_acca(i, a, mode, pm_alts)
        lines.extend(acca_lines)
        lines.append("")

    # Bankroll footer (brief)
    lines.append("*Bankroll:* %s" % bankroll_reason[:100])

    # Unsupported markets
    if unsupported:
        lines.append("_Unsupported mkts: %s_" % ", ".join(unsupported[:4]))

    # Strip trailing blanks
    while lines and lines[-1] == "":
        lines.pop()

    return _truncate("\n".join(lines))
