"""Accumulator generator for the /accas bot command.

Model-driven, exposure-aware, EV-moneyline-first. Reads the cached model card
(``data/model_predictions.json`` for the blended 1X2, ``site/scores_data.json``
for O/U + BTTS model probs and per-venue 1X2 prices) plus the latest odds
snapshot (``odds_snapshots`` for totals/BTTS book prices) — never a live model
fit, so it is fast enough for an interactive command. Display-only: it NEVER
writes to the ledger and never triggers a site push.

Markets we can price (off the Dixon-Coles matrix, reconciled to the blend):
1X2 moneyline, Over/Under total goals, BTTS, Draw-No-Bet. Markets we cannot
(corners, cards, shots-on-target, goalscorers, any player prop) are excluded —
see PLAYER_PROP_TODO. Modes: ``value`` (default, moneyline +EV first),
``hedge`` (favour legs that offset the held cluster), ``longshot`` (allow
>=4.0 legs), ``promo`` (qualify live offers, optimise the offer's value metric).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Markets the model prices natively. Player props are deliberately absent:
PLAYER_PROP_TODO = (
    "corners/cards/shots-on-target/goalscorers need a player-event model "
    "(roadmap) — never put an unpriced leg in a model acca."
)

#: Exchange commission haircut applied to the effective price (6% until July).
COMMISSION = {"betfair_ex_uk": 0.06, "smarkets": 0.06, "matchbook": 0.06}

#: A leg counts as a "longshot" (skipped outside longshot mode) below this prob
#: or above this price.
LONGSHOT_PROB = 0.12
LONGSHOT_ODDS = 9.0

#: Low-win ("value") default: never emit a 100x+ lottery. Accas are assembled
#: shortest-first and a candidate is dropped once its combined price exceeds
#: this ceiling, which keeps the product in the modest ~2-8x band the punter
#: actually wins from time to time. Legacy "edge"/"longshot" modes are uncapped.
VALUE_MAX_COMBINED = 12.0

DEFAULT_MIN_EDGE = 0.02
DEFAULT_BANKROLL = 2500.0
KELLY_FRACTION = 0.25


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Leg:
    fixture: str
    market: str           # "1X2" | "totals" | "btts" | "dnb"
    selection: str        # e.g. "Draw", "Over 2.5", "BTTS No", team name
    model_prob: float
    odds: float           # raw best book odds
    book: str
    edge: float           # model_prob * eff_odds - 1 (commission-adjusted)
    is_moneyline: bool

    @property
    def is_longshot(self) -> bool:
        return self.model_prob < LONGSHOT_PROB or self.odds > LONGSHOT_ODDS


@dataclass
class Acca:
    legs: List[Leg]
    combined_odds: float
    model_prob: float
    edge: float
    stake: float
    label: str = ""
    note: str = ""


@dataclass
class Exposure:
    """A normalised view of what the book is already long."""
    # fixture-token -> count of held bets touching it
    fixture_count: Dict[str, int] = field(default_factory=dict)
    # set of "fixturetoken|selectiontoken" the book already holds
    held: set = field(default_factory=set)
    # team-token -> count of held bets long that team
    team_long: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _toks(s: Any) -> List[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split() if len(t) > 2]


def _fixture_token(fixture: str) -> str:
    return " ".join(sorted(_toks(fixture)))


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


# ---------------------------------------------------------------------------
# Candidate legs (pure)
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
                    legs.append(Leg(name, "dnb", "%s (DNB)" % team, float(p), float(odds), book, edge, True))
    return legs


def _split_fixture(name: str) -> Tuple[str, str]:
    for sep in (" vs ", " v "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return name, ""


# ---------------------------------------------------------------------------
# Exposure (pure)
# ---------------------------------------------------------------------------
def build_exposure(open_bets: List[Dict[str, Any]]) -> Exposure:
    """Normalise held bets (sportsbook + Polymarket positions) into an Exposure."""
    exp = Exposure()
    for b in open_bets or []:
        fixture = b.get("match") or b.get("match_desc") or ""
        sel = b.get("selection") or ""
        ftok = _fixture_token(fixture)
        if ftok:
            exp.fixture_count[ftok] = exp.fixture_count.get(ftok, 0) + 1
        sig = "%s|%s" % (ftok, " ".join(sorted(_toks(sel))))
        exp.held.add(sig)
        for t in set(_toks(fixture)) & set(_toks(sel)):
            exp.team_long[t] = exp.team_long.get(t, 0) + 1
    return exp


def _leg_held(leg: Leg, exp: Exposure) -> bool:
    sig = "%s|%s" % (_fixture_token(leg.fixture), " ".join(sorted(_toks(leg.selection))))
    return sig in exp.held


def _leg_concentration(leg: Leg, exp: Exposure) -> int:
    """How concentrated the book already is on this leg's fixture/team."""
    c = exp.fixture_count.get(_fixture_token(leg.fixture), 0)
    for t in _toks(leg.selection):
        c += exp.team_long.get(t, 0)
    return c


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

    The default ``value`` mode is a LOW-LEVEL-WIN builder: among the +EV legs it
    ranks by MODEL PROBABILITY (favourites / shortest fair odds first) rather
    than by edge, assembles 2-4 legs shortest-first, and drops any combination
    whose combined price exceeds ``max_combined_odds`` (default
    :data:`VALUE_MAX_COMBINED`). That keeps the product in a modest ~2-8x band
    that actually hits, instead of stacking the model's high-edge underdogs and
    draws into a 300x+ lottery. The legacy edge-maximising behaviour lives in
    ``edge`` mode (and ``longshot``, which additionally allows >=4.0 legs) — see
    /card vs /longshots. ``hedge`` favours legs that offset the held cluster.
    """
    exposure = exposure or Exposure()
    # Drop duplicates of held positions.
    legs = [L for L in legs if not _leg_held(L, exposure)]
    # Longshot policy.
    if mode != "longshot":
        legs = [L for L in legs if not L.is_longshot]
    if not legs:
        return []

    # Low-win is the default; "edge"/"longshot" keep the legacy edge-max ranking.
    low_win = mode not in ("edge", "longshot", "hedge")
    if max_combined_odds is None and low_win:
        max_combined_odds = VALUE_MAX_COMBINED

    def rank_key(L: Leg):
        conc = _leg_concentration(L, exposure)
        if mode == "hedge":
            # Prefer legs that REDUCE concentration; moneyline still tie-breaks.
            return (conc, not L.is_moneyline, -L.edge)
        if not low_win:
            # legacy edge-max: moneyline first, then edge, then less-concentrated.
            return (not L.is_moneyline, -L.edge, conc)
        # low-win: moneyline first, then FAVOURITES (highest model prob), then
        # edge as a tie-break, then less-concentrated. Ranking by prob (not edge)
        # is what stops the high-edge draw/underdog stack.
        return (not L.is_moneyline, -L.model_prob, -L.edge, conc)

    legs = sorted(legs, key=rank_key)

    # Best leg per fixture (one selection per match — mutual-exclusion guard).
    best_by_fixture: Dict[str, Leg] = {}
    for L in legs:
        k = _fixture_token(L.fixture)
        if k not in best_by_fixture:
            best_by_fixture[k] = L
    anchors = sorted(best_by_fixture.values(), key=rank_key)
    if len(anchors) < min_legs:
        return []

    accas: List[Acca] = []
    # Shortest-first: the 2-leg is the most likely to win, larger ones add
    # variety. Combined odds only grow as legs are added, so once we blow the
    # ceiling every larger size does too — stop.
    seen = set()
    for n in range(min_legs, min(len(anchors), max_legs) + 1):
        chosen = anchors[:n]
        o, p, edge = _combined(chosen)
        if max_combined_odds and o > max_combined_odds:
            break
        if edge <= 0:
            continue  # whole acca must stay +EV
        sig = tuple(_fixture_token(L.fixture) + L.selection for L in chosen)
        if sig in seen:
            continue
        seen.add(sig)
        stake = round(kelly_fraction * _kelly_fraction(p, o) * bankroll, 2)
        note = _exposure_note(chosen, exposure)
        accas.append(Acca(chosen, round(o, 2), p, edge, stake, note=note))
        if len(accas) >= max_accas:
            break
    return accas


def _exposure_note(legs: List[Leg], exp: Exposure) -> str:
    adds = [L.fixture for L in legs if _leg_concentration(L, exp) > 0]
    if adds:
        return "adds to existing exposure on: " + ", ".join(sorted(set(adds)))
    return "diversifies — no overlap with current book"


# ---------------------------------------------------------------------------
# Promo mode
# ---------------------------------------------------------------------------
@dataclass
class Offer:
    name: str
    venue: str
    account: str
    min_legs: int
    min_leg_odds: float          # 0 if only a combined floor applies
    min_combined_odds: float     # 0 if only per-leg applies
    kind: str                    # "snr_free" | "lose_free" | "qualifier"
    max_stake: float
    game_restrict: Optional[str] = None  # fixture-token substring, e.g. "england ghana"


#: Known live offers (terms are stable; the promotions table is free-text).
OFFER_TEMPLATES: List[Offer] = [
    Offer("Betfair SB free-bet acca", "betfair_sportsbook", "1", 3, 1.5, 0.0, "snr_free", 10.0),
    Offer("Paddy Eng-Gha money-back", "paddypower", "1", 3, 2.0, 0.0, "lose_free", 50.0, "england ghana"),
    Offer("Betfred ENG/SCOT builder", "betfred", "1", 3, 0.0, 4.0, "qualifier", 10.0),
]

SNR_RETENTION = 0.70


def build_promo_accas(
    legs: List[Leg],
    offers: Optional[List[Offer]] = None,
    exposure: Optional[Exposure] = None,
) -> List[Acca]:
    """Per offer, build a qualifying acca optimised for the offer's value metric.

    SNR free bets maximise combined odds (retention rises with odds);
    "lose->free-bet" insurance sizes toward max (effective risk shown in note);
    qualifiers minimise legs/odds to just clear the floor.
    """
    offers = offers if offers is not None else OFFER_TEMPLATES
    exposure = exposure or Exposure()
    out: List[Acca] = []
    for off in offers:
        pool = list(legs)
        if off.game_restrict:
            want = set(off.game_restrict.split())
            pool = [L for L in pool if want <= set(_toks(L.fixture))]
        if off.min_leg_odds:
            pool = [L for L in pool if L.odds >= off.min_leg_odds]
        # One leg per fixture.
        best_by_fixture: Dict[str, Leg] = {}
        if off.game_restrict:
            # Same-game offer: legs are within one match — keep distinct markets,
            # but never two mutually-exclusive 1X2 legs.
            seen_1x2 = False
            kept: List[Leg] = []
            for L in sorted(pool, key=lambda x: -x.odds):  # higher odds first (SNR value)
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

        # Order for the offer: a qualifier wants the cheapest legs that clear the
        # combined floor (lowest variance); SNR / lose->free want the best +EV
        # legs that meet the per-leg floor (NOT pure longshot-stacking, which
        # gives a near-zero-hit lottery with poor real retention).
        if off.kind == "qualifier" and off.min_combined_odds:
            pool = sorted(pool, key=lambda x: x.odds)  # cheapest legs first
        else:
            pool = sorted(pool, key=lambda x: -x.edge)  # best +EV legs first

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
        p = 1.0
        for L in chosen:
            p *= L.model_prob
        # Value metric / note per offer kind.
        if off.kind == "snr_free":
            note = "SNR free bet @ %s: retains ~£%.0f of value (%.0f%% of £%.0f)" % (
                round(o, 1), SNR_RETENTION * off.max_stake * (1 - 1 / o) if o > 1 else 0,
                SNR_RETENTION * 100, off.max_stake)
        elif off.kind == "lose_free":
            eff_risk = off.max_stake * (1 - SNR_RETENTION)
            note = "stake £%.0f, lose->free bet: effective risk ~£%.0f; combined %s" % (
                off.max_stake, eff_risk, round(o, 1))
        else:
            note = "qualifier: 3+ legs @ combined %s (clears %s floor)" % (
                round(o, 1), off.min_combined_odds or off.min_leg_odds)
        if off.game_restrict:
            note += " | same-game; confirm each leg >=%.1f on the app" % off.min_leg_odds
        lbl = "%s [%s a%s]" % (off.name, off.venue, off.account)
        out.append(Acca(chosen, round(o, 2), p, _eff_edge(chosen), off.max_stake, label=lbl, note=note))
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
# IO loaders
# ---------------------------------------------------------------------------
def load_fixtures(
    preds_path: str = "data/model_predictions.json",
    scores_path: str = "site/scores_data.json",
    db_path: str = "data/wca.db",
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[Tuple[str, str], Tuple[float, str]]]]:
    """Load per-fixture model probs + best 1X2 book prices + snapshot derivatives."""
    preds = _read_json(preds_path)
    scores = _read_json(scores_path)
    finished = _finished_tokens()

    # model 1X2 by fixture token
    model_1x2: Dict[str, Dict[str, float]] = {}
    for fx in (preds.get("fixtures") if isinstance(preds, dict) else preds) or []:
        m = fx.get("model") or {}
        if m:
            model_1x2[_fixture_token(fx.get("fixture"))] = {
                "home": m.get("home"), "draw": m.get("draw"), "away": m.get("away")}

    fixtures: List[Dict[str, Any]] = []
    for f in (scores.get("fixtures") if isinstance(scores, dict) else []) or []:
        name = f.get("fixture") or ""
        if _is_finished(name, finished):
            continue
        ftok = _fixture_token(name)
        # best 1X2 odds per outcome from venues
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
    return fixtures, snapshot_prices


def _pct_to_frac(d: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k in ("home", "draw", "away"):
        v = d.get(k)
        out[k] = (v / 100.0 if isinstance(v, (int, float)) and v > 1 else v)
    return out


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _load_snapshot_derivatives(db_path: str) -> Dict[str, Dict[Tuple[str, str], Tuple[float, str]]]:
    """Best totals/btts book prices per fixture-token from the latest snapshot."""
    out: Dict[str, Dict[Tuple[str, str], Tuple[float, str]]] = {}
    try:
        con = sqlite3.connect(db_path)
        m = con.execute("SELECT MAX(ts_utc) FROM odds_snapshots").fetchone()[0]
        if not m:
            return out
        rows = con.execute(
            "SELECT market, selection, decimal_odds, raw, source FROM odds_snapshots "
            "WHERE ts_utc=? AND market IN ('totals','btts')", (m,)).fetchall()
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


def load_open_bets(db_path: str = "data/wca.db", site_data: str = "site/data.json") -> List[Dict[str, Any]]:
    """Merged exposure: ledger open bets + live Polymarket positions."""
    out: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(db_path)
        for mid, md, sel, mk in con.execute(
                "SELECT match_id, match_desc, selection, market FROM bets WHERE status='open'"):
            out.append({"match": md, "selection": sel, "market": mk})
        con.close()
    except Exception:
        pass
    data = _read_json(site_data)
    for p in (data.get("positions") if isinstance(data, dict) else []) or []:
        if str(p.get("id", "")).startswith("pm-"):
            out.append({"match": p.get("match") or "", "selection": p.get("selection") or "",
                        "market": p.get("market") or ""})
    return out


# ---------------------------------------------------------------------------
# Orchestration + formatting
# ---------------------------------------------------------------------------
def build_accas(
    *,
    preds_path: str = "data/model_predictions.json",
    scores_path: str = "site/scores_data.json",
    db_path: str = "data/wca.db",
    site_data: str = "site/data.json",
    mode: str = "value",
    min_edge: Optional[float] = None,
    bankroll: float = DEFAULT_BANKROLL,
) -> Dict[str, Any]:
    fixtures, snap = load_fixtures(preds_path, scores_path, db_path)
    exposure = build_exposure(load_open_bets(db_path, site_data))
    # Low-win accepts any genuinely +EV favourite (even a thin edge); the legacy
    # edge-max modes keep the stiffer 2% floor so a longshot has to really pay.
    if min_edge is None:
        min_edge = 0.0 if mode in ("value", "low_win") else DEFAULT_MIN_EDGE
    legs = candidate_legs(fixtures, snap, min_edge=min_edge)
    if mode == "promo":
        accas = build_promo_accas(legs, exposure=exposure)
    else:
        accas = assemble_accas(legs, exposure, mode=mode, bankroll=bankroll)
    return {"mode": mode, "accas": accas, "n_legs": len(legs), "n_fixtures": len(fixtures)}


def format_accas(result: Dict[str, Any]) -> str:
    mode = result.get("mode", "value")
    accas = result.get("accas") or []
    title = {"value": "Accas — low-level win (favourites, +EV)",
             "edge": "Accas — max edge (high-edge underdogs)",
             "hedge": "Accas — hedge the book",
             "longshot": "Accas — longshots (>=4.0 legs)",
             "promo": "Accas — promo / offer extraction"}.get(mode, "Accas")
    if not accas:
        if mode in ("value", "low_win"):
            return ("\U0001f3af *%s*\nNo qualifying low-win accas — no +EV favourite "
                    "legs at modest (<=%.0fx) combined odds on the current card. The "
                    "book is shading the favourites, so the only +EV legs are "
                    "high-edge draws/underdogs. Try `/accas edge` for those (longshot "
                    "lottery), or wait for a fresher card." % (title, VALUE_MAX_COMBINED))
        return ("\U0001f3af *%s*\nNo qualifying accas — no legs cleared the +EV gate "
                "on the current card (or all overlap your book). Try `/accas longshot` "
                "or wait for a fresher card." % title)
    lines = ["\U0001f3af *%s*" % title, ""]
    for i, a in enumerate(accas, 1):
        head = "*%s*" % (a.label or "Acca %d" % i)
        lines.append("%s — *%s* @ *%.2f*" % (head, _fmt_legs_count(a), a.combined_odds))
        for L in a.legs:
            lines.append("   • %s — %s (%s) @ %.2f  [%.0f%% / %+.0f%%]" % (
                L.fixture, L.selection, L.market, L.odds, L.model_prob * 100, L.edge * 100))
        if mode == "promo":
            lines.append("   _%s_" % a.note)
        else:
            lines.append("   model %.1f%% · edge *%+.0f%%* · ¼-Kelly *£%.2f*" % (
                a.model_prob * 100, a.edge * 100, a.stake))
            lines.append("   _%s_" % a.note)
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _fmt_legs_count(a: Acca) -> str:
    return "%d-leg" % len(a.legs)
