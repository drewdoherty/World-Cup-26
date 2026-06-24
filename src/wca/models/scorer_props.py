"""Unified, model-driven scorer/props provider — ONE source of truth.

Every command that shows player props (/next, /goalscorers, /accas) and the
``/event`` scanner price from *this* module, so they cannot drift. The model
price is local and free (it reads ``data/players.db`` + the Dixon-Coles team
lambdas), so it is **always available** — even when no bookmaker player-prop
market exists for the fixture. Live bookmaker / Polymarket odds are an *optional
overlay* used only to compute edge/EV; they never gate whether a player is
shown.

Player npxg shares come from ``players.db`` (StatsBomb WC2018+2022 per-90
output), normalised across a team's rated squad players, with the analyst
override store (``data/players.json``) taking precedence where present. No share
is ever invented: a player with no StatsBomb history simply is not priced
(``source='data-pending'``) rather than guessed.

Corner / card scans reuse :mod:`wca.models.events` driven by the same lambdas
and the team rates already in ``players.db``.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from wca.data.players_db import DEFAULT_DB_PATH
from wca.data.teamnames import canonical
from wca.lineup import _players_by_team, team_lineup_strength
from wca.models.events import card_risk, corner_count_dist
from wca.models.scorers import PlayerParams, ScorerLine, ScorerPricer, players_for_team

MODEL_ONLY_LABEL = "model price, no market"


@dataclass
class ScorerScanLine:
    """One player's model scorer prices + optional market overlay/EV."""

    player: str
    team: str
    intensity: float
    model_p_anytime: float
    model_fair_anytime: float
    model_p_first: float
    model_fair_first: float
    share: float
    share_source: str
    # Optional market overlay.
    book_anytime_odds: Optional[float] = None
    book_anytime_name: Optional[str] = None
    pm_anytime_price: Optional[float] = None
    book_first_odds: Optional[float] = None
    label: str = MODEL_ONLY_LABEL

    @property
    def anytime_ev(self) -> Optional[float]:
        """EV per unit on the best book anytime price vs the model prob."""
        if self.book_anytime_odds and self.book_anytime_odds > 1.0:
            return self.model_p_anytime * self.book_anytime_odds
        return None

    @property
    def anytime_edge_pct(self) -> Optional[float]:
        ev = self.anytime_ev
        return None if ev is None else (ev - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Player npxg shares from players.db (the one source of truth)
# ---------------------------------------------------------------------------

def team_scorer_params(
    team: str,
    db_path: str = DEFAULT_DB_PATH,
    overrides_path: str = "data/players.json",
    expected_minutes: float = 90.0,
    conn: Optional[sqlite3.Connection] = None,
    players_by_team=None,
) -> List[PlayerParams]:
    """Per-player :class:`PlayerParams` for a team, shares summing to ~1.

    Shares are each rated player's ``npxg_p90`` divided by the team's rated
    total (so applying them to the team lambda distributes its expected goals
    across the attacking core). The analyst override store wins where it has the
    player — overrides carry penalty-taker flags and curated minutes.
    """
    team_c = canonical(team)
    # All rated players (top_n huge -> no cap), reusing the lineup matcher.
    ls = team_lineup_strength(team_c, conn=conn, db_path=db_path,
                              top_n=10 ** 9, players_by_team=players_by_team)
    contributors = ls.contributors
    total = sum(p.npxg_p90 for p in contributors if p.npxg_p90)
    overrides = {o.name: o for o in players_for_team(team_c, overrides_path)}
    # also index overrides by normalized name for robustness
    out: List[PlayerParams] = []
    used_override = set()
    if total > 0:
        for p in contributors:
            if not p.npxg_p90:
                continue
            ov = overrides.get(p.name)
            if ov is not None:
                used_override.add(ov.name)
                out.append(PlayerParams(
                    name=p.name, team=team_c, npxg_share=ov.npxg_share,
                    penalty_taker=ov.penalty_taker,
                    expected_minutes=ov.expected_minutes,
                    source="players.json override"))
            else:
                out.append(PlayerParams(
                    name=p.name, team=team_c, npxg_share=p.npxg_p90 / total,
                    penalty_taker=False, expected_minutes=expected_minutes,
                    source="players.db (statsbomb npxg-share)"))
    # Override-only players with no StatsBomb history still get priced from the
    # analyst store (e.g. Scotland), since that IS a documented real source.
    for name, ov in overrides.items():
        if name in used_override:
            continue
        out.append(PlayerParams(
            name=ov.name, team=team_c, npxg_share=ov.npxg_share,
            penalty_taker=ov.penalty_taker, expected_minutes=ov.expected_minutes,
            source="players.json override"))
    return out


# ---------------------------------------------------------------------------
# Model scorer lines (works with NO market)
# ---------------------------------------------------------------------------

def model_scorer_lines(
    home: str,
    away: str,
    lambda_home: float,
    lambda_away: float,
    db_path: str = DEFAULT_DB_PATH,
    overrides_path: str = "data/players.json",
    top_n_per_team: int = 2,
    pen_xg: float = 0.18,
) -> Dict[str, List[ScorerScanLine]]:
    """Top-N model-priced scorers per side — independent of any market.

    Returns ``{"home": [...], "away": [...]}`` of :class:`ScorerScanLine`,
    sorted by model anytime probability. Always populated when the squads carry
    StatsBomb history (or analyst overrides); never requires a bookmaker market.
    """
    home_c, away_c = canonical(home), canonical(away)
    total_lambda = lambda_home + lambda_away
    pricer = ScorerPricer(pen_xg=pen_xg)
    out: Dict[str, List[ScorerScanLine]] = {"home": [], "away": []}
    if total_lambda <= 0:
        return out

    # One DB connection + one player index shared across both teams.
    from wca.lineup import _connect
    conn = _connect(db_path)
    try:
        pbt = _players_by_team(conn)
        for side, team, team_lambda in (
            ("home", home_c, lambda_home), ("away", away_c, lambda_away)
        ):
            params = team_scorer_params(
                team, db_path=db_path, overrides_path=overrides_path,
                conn=conn, players_by_team=pbt)
            lines: List[ScorerScanLine] = []
            for pp in params:
                sl: ScorerLine = pricer.price_player(pp, team_lambda, total_lambda)
                if sl.p_anytime <= 0:
                    continue
                lines.append(ScorerScanLine(
                    player=pp.name, team=team, intensity=sl.intensity,
                    model_p_anytime=sl.p_anytime,
                    model_fair_anytime=sl.fair_anytime,
                    model_p_first=sl.p_first, model_fair_first=sl.fair_first,
                    share=pp.npxg_share, share_source=pp.source))
            lines.sort(key=lambda x: x.model_p_anytime, reverse=True)
            out[side] = lines[:top_n_per_team]
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Optional market overlay (book + Polymarket) for EV
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    import unicodedata
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return " ".join(n.lower().split())


def overlay_market(
    lines_by_team: Dict[str, List[ScorerScanLine]],
    scorer_df=None,
    pm_events=None,
    home: str = "",
    away: str = "",
    pm_lookup: bool = False,
) -> Dict[str, List[ScorerScanLine]]:
    """Attach best book anytime/first odds (+ PM price) and label matched lines.

    Pure overlay: never adds or drops players, only enriches them. A line that
    gets a book price is relabelled to show the market is present; the rest keep
    the ``model price, no market`` label.
    """
    from wca.nextmatch import (ANYTIME_SCORER_MARKET, FIRST_SCORER_MARKET,
                               _best_book_odds, _player_rows)

    anytime = _player_rows(scorer_df, ANYTIME_SCORER_MARKET) if scorer_df is not None and not scorer_df.empty else {}
    first = _player_rows(scorer_df, FIRST_SCORER_MARKET) if scorer_df is not None and not scorer_df.empty else {}
    any_by_norm = {_norm(k): v for k, v in anytime.items()}
    first_by_norm = {_norm(k): v for k, v in first.items()}

    for side in ("home", "away"):
        for ln in lines_by_team.get(side, []):
            key = _norm(ln.player)
            grp = any_by_norm.get(key)
            if grp is not None:
                o, b = _best_book_odds(grp)
                if o is not None:
                    ln.book_anytime_odds, ln.book_anytime_name = o, b
                    ln.label = "model + market"
            fgrp = first_by_norm.get(key)
            if fgrp is not None:
                fo, _ = _best_book_odds(fgrp)
                ln.book_first_odds = fo

    if pm_lookup and home and away:
        try:
            from wca.data import polymarket as pm
            if pm_events is None:
                pm_events = pm.find_world_cup_markets(include_closed=False)
            for side in ("home", "away"):
                for ln in lines_by_team.get(side, []):
                    res = pm.resolve_player_anytime_token(home, away, ln.player,
                                                          events=pm_events)
                    if res is not None and 0.0 < float(res["price"]) < 1.0:
                        ln.pm_anytime_price = float(res["price"])
        except Exception:
            pass
    return lines_by_team


# ---------------------------------------------------------------------------
# Corner / card scans (for /event corners|cards)
# ---------------------------------------------------------------------------

def _team_rate(conn: sqlite3.Connection, team: str, col: str) -> Optional[float]:
    row = conn.execute(
        "SELECT %s AS v FROM team_rates WHERE team=?" % col, (canonical(team),)
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


@dataclass
class PropLine:
    """A model price (+ optional market EV) for an over/under prop line."""

    market: str
    line: float
    p_over: float
    fair_over: float
    fair_under: float
    book_over_odds: Optional[float] = None
    label: str = MODEL_ONLY_LABEL

    @property
    def over_ev(self) -> Optional[float]:
        if self.book_over_odds and self.book_over_odds > 1.0:
            return self.p_over * self.book_over_odds
        return None


def corners_scan(home: str, away: str, lambda_home: float, lambda_away: float,
                 lines=(8.5, 9.5, 10.5)) -> List[PropLine]:
    cd = corner_count_dist(lambda_home, lambda_away)
    out = []
    for L in lines:
        po = cd.prob_over(L)
        over, under = cd.fair_over_under(L)
        out.append(PropLine("corners_over_under", L, po, over, under))
    return out


def fixture_scorers_payload(
    home: str, away: str, lambda_home: float, lambda_away: float,
    db_path: str = DEFAULT_DB_PATH, overrides_path: str = "data/players.json",
    top_n_per_team: int = 3,
) -> Dict:
    """A JSON-able model-scorer payload for one fixture (the on-disk source).

    Persisted to ``data/model_scorers.json`` by the build so /accas (and the
    site) consume the SAME model prices as /next and /goalscorers without
    re-fitting anything. Pure model — no market, no network.
    """
    lines = model_scorer_lines(home, away, lambda_home, lambda_away,
                               db_path=db_path, overrides_path=overrides_path,
                               top_n_per_team=top_n_per_team)

    def _one(ln: ScorerScanLine) -> Dict:
        return {
            "player": ln.player, "team": ln.team,
            "p_anytime": round(ln.model_p_anytime, 5),
            "fair_anytime": round(ln.model_fair_anytime, 4),
            "p_first": round(ln.model_p_first, 5),
            "fair_first": round(ln.model_fair_first, 4),
            "npxg_share": round(ln.share, 4),
            "share_source": ln.share_source,
            "label": MODEL_ONLY_LABEL,
        }

    return {
        "fixture": "%s vs %s" % (canonical(home), canonical(away)),
        "home": canonical(home), "away": canonical(away),
        "lambda_home": round(float(lambda_home), 4),
        "lambda_away": round(float(lambda_away), 4),
        "home_scorers": [_one(l) for l in lines["home"]],
        "away_scorers": [_one(l) for l in lines["away"]],
    }


def cards_scan(home: str, away: str, lambda_home: float, lambda_away: float,
               db_path: str = DEFAULT_DB_PATH,
               lines=(3.5, 4.5, 5.5)) -> List[PropLine]:
    fouls_home = fouls_away = reds_home = reds_away = None
    try:
        from wca.lineup import _connect
        conn = _connect(db_path)
        try:
            fouls_home = _team_rate(conn, home, "fouls_pm")
            fouls_away = _team_rate(conn, away, "fouls_pm")
            reds_home = _team_rate(conn, home, "reds_pm")
            reds_away = _team_rate(conn, away, "reds_pm")
        finally:
            conn.close()
    except FileNotFoundError:
        pass
    cr = card_risk(lambda_home, lambda_away, fouls_home=fouls_home,
                   fouls_away=fouls_away, reds_home=reds_home, reds_away=reds_away)
    out = []
    for L in lines:
        po = cr.prob_over(L)
        over, under = cr.fair_over_under(L)
        out.append(PropLine("cards_over_under", L, po, over, under))
    return out, cr.p_red, cr.mean_total
