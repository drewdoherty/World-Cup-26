"""Promo-aware accumulator / bet-builder briefs for the Telegram bot.

The original ``/accas`` path tried to manufacture generic accumulator picks
from a wide odds frame that the live project no longer emits.  This module is
deliberately narrower and more useful: it turns the current ``scores_data``
feed into a small operator brief for the real bookmaker promos currently worth
using.

Honesty rules:

* A promo is always shown, even when the fixture is not in the current feed.
* A candidate leg is marked priceable only when it comes from model 1X2,
  O/U 2.5, BTTS, or listed correct-score probabilities.
* Bet-builder legs for cards/corners/SOT/scorers are not invented here.  The
  brief says they need manual book pricing / a future event model.
* Same-game bet builders are labelled as component-ranked, not joint-probability
  priced.  Correlation-aware builder EV belongs in the event-model upgrade.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PromoSpec:
    site: str
    title: str
    accounts: Tuple[str, ...]
    min_legs: int
    min_total_odds: Optional[float] = None
    min_leg_odds: Optional[float] = None
    max_leg_odds: Optional[float] = None
    max_free_bet: Optional[float] = None
    fixture_terms: Tuple[str, ...] = ()
    any_team_terms: Tuple[str, ...] = ()
    venue_keys: Tuple[str, ...] = ()
    require_model: bool = False
    bet_builder: bool = False
    boost_pct: Optional[float] = None
    notes: str = ""
    manual_legs: Tuple[str, ...] = ()
    manual_price_note: str = ""


PROMOS: Tuple[PromoSpec, ...] = (
    PromoSpec(
        site="Paddy Power",
        title="Money-back acca insurance x2",
        accounts=("A1 token 1", "A1 token 2"),
        min_legs=3,
        min_total_odds=2.0,
        min_leg_odds=1.2,
        max_leg_odds=2.2,
        venue_keys=("paddypower",),
        require_model=True,
        notes=(
            "Two A1 money-back tokens. Build 3 favourite-ish match-winner legs at Paddy. "
            "Money-back value comes from exactly-one-leg-misses; verify in-app refund/free-bet terms."
        ),
    ),
    PromoSpec(
        site="Paddy Power",
        title="England/Ghana £5 free Bet Builder x2",
        accounts=("A1 token 1", "A1 token 2"),
        min_legs=3,
        min_total_odds=2.0,
        fixture_terms=("England", "Ghana"),
        bet_builder=True,
        notes=(
            "Two £5 free Bet Builder tokens. Pre-match only, 3+ legs, min odds EVS/2.0. "
            "Free stake is SNR; treat as offer extraction, not model CLV."
        ),
        manual_legs=(
            "England -1.5 handicap",
            "England team goals over 2.5",
            "England win to nil / Ghana under 0.5 goals",
        ),
        manual_price_note=(
            "User constraint: 3+ legs and each chosen leg should be EVS/2.0+ where the app enforces it. "
            "If any leg is below EVS, swap to Kane 2+ SOT, Saka 1+ SOT, England 7+ corners, or England win both halves."
        ),
    ),
    PromoSpec(
        site="Bet365",
        title="Already logged England/Ghana builder — do not duplicate",
        accounts=("A1",),
        min_legs=3,
        min_total_odds=5.52,
        fixture_terms=("England", "Ghana"),
        bet_builder=True,
        notes=(
            "Existing £5 offer builder already placed/logged. It should be one combined row, not three separate open legs."
        ),
        manual_legs=(
            "Home Team Minus 2.5 Goals: England -2.5 Goals",
            "Handicap Betting: England (-2.0)",
            "Correct Score Combinations: England to win 1-0, 2-0 or 3-0",
        ),
        manual_price_note=(
            "Exposure context only. Do not place again unless deliberately using another free token. "
            "Note: combined with -2.5/-2.0 this is effectively concentrated around 3-0."
        ),
    ),
    PromoSpec(
        site="Betfair Sportsbook",
        title="Max £10 free bet acca",
        accounts=("A1", "A2"),
        min_legs=3,
        min_leg_odds=1.5,
        max_leg_odds=6.0,
        max_free_bet=10.0,
        venue_keys=("betfair_sb_uk", "betfair_sportsbook"),
        notes="Minimum 3 legs, every leg 1.5+. Bot caps suggested legs at 6.0 to avoid lottery-ticket promo use.",
    ),
    PromoSpec(
        site="Betfred",
        title="ENG/SCOT World Cup bet builder",
        accounts=("A1", "A2"),
        min_legs=3,
        min_total_odds=4.0,
        any_team_terms=("England", "Scotland"),
        bet_builder=True,
        notes=(
            "Single bet builder on any England/Scotland WC match, 3+ legs, 3/1+, "
            "min stake £10, pre-built builders do not apply, paid on settlement."
        ),
        manual_legs=(
            "Favourite -1.5 handicap",
            "Favourite team goals over 2.5",
            "Favourite win to nil / opponent under 0.5 goals",
        ),
        manual_price_note=(
            "Manual price in the app. For England/Ghana, use England as favourite. "
            "If using a Scotland fixture, check existing exposure first and avoid doubling a specific score/result."
        ),
    ),
    PromoSpec(
        site="Betfred",
        title="Brazil/Haiti in-play £5 trigger",
        accounts=("A1", "A2"),
        min_legs=1,
        min_total_odds=2.0,
        fixture_terms=("Brazil", "Haiti"),
        notes=(
            "Bet £5 in-play single at EVS/2.0+ on BRA/HAI to get £5 free bets. "
            "Restricted follow-up free bets to NED/SWE in-play."
        ),
        manual_legs=(
            "Brazil -2.5 handicap",
            "Brazil win + over 2.5 goals",
            "Brazil team goals over 2.5",
        ),
        manual_price_note="Use one qualifying in-play single only; pick whichever of these is EVS/2.0+ in app.",
    ),
    PromoSpec(
        site="Virgin Bet",
        title="England game free bet builder",
        accounts=("A1", "A2"),
        min_legs=3,
        min_total_odds=2.0,
        fixture_terms=("England", "Ghana"),
        bet_builder=True,
        notes=(
            "Get a free Bet Builder on every England WC game after qualifying cash bets. "
            "Min 3 selections, odds EVS/2.0+, £1-£20, stakes not returned."
        ),
        manual_legs=(
            "England win",
            "England team goals over 1.5",
            "Ghana team goals under 1.5",
        ),
        manual_price_note=(
            "If this only needs combined EVS, use the safer three above. If every leg must be EVS+, use the more aggressive "
            "Paddy-style version: England -1.5, England team O2.5, England win to nil."
        ),
    ),
    PromoSpec(
        site="Virgin Bet",
        title="50% winnings boost — Scotland vs Morocco bet builder",
        accounts=("A1", "A2"),
        min_legs=3,
        min_total_odds=2.0,
        fixture_terms=("Scotland", "Morocco"),
        bet_builder=True,
        boost_pct=50.0,
        notes=(
            "Selected-event winnings boost. One bet/customer, combined min odds EVS/2.0+, "
            "Bet Builders only, max stake £50."
        ),
        manual_legs=(
            "Under 3.5 goals",
            "BTTS No",
            "Scotland under 1.5 team goals",
        ),
        manual_price_note=(
            "Exposure guard: this is deliberately low-score/anti-Scotland, not another Morocco result bet. "
            "Keep stake small because the existing book already has Scotland/Morocco exposure."
        ),
    ),
)


def load_scores_feed(path: str = "site/scores_data.json") -> Dict[str, Any]:
    """Load the scores/market feed, tolerantly."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _median(vals: List[float]) -> Optional[float]:
    xs = sorted(v for v in vals if v > 0)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def load_latest_snapshot_feed(
    pattern: str = "data/raw/snapshots/oddsapi_multi_uk_*.json",
) -> Dict[str, Any]:
    """Build a scores-feed-like fixture list from the newest raw Odds API snapshot."""
    paths = sorted(glob.glob(pattern))
    if not paths:
        return {}
    path = paths[-1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}

    by_event: Dict[str, List[Dict[str, Any]]] = {}
    generated = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("event_id") or row.get("id") or "")
        if not eid:
            continue
        by_event.setdefault(eid, []).append(row)
        generated = max(generated, str(row.get("retrieved_at") or ""))

    fixtures: List[Dict[str, Any]] = []
    for event_rows in by_event.values():
        first = event_rows[0]
        home = str(first.get("home_team") or "").strip()
        away = str(first.get("away_team") or "").strip()
        if not home or not away:
            continue
        by_book: Dict[str, Dict[str, Any]] = {}
        imps = {"home": [], "draw": [], "away": []}  # type: Dict[str, List[float]]
        over_imps: List[float] = []
        under_imps: List[float] = []
        for row in event_rows:
            try:
                odds = float(row.get("decimal_odds") or 0.0)
            except (TypeError, ValueError):
                continue
            if odds <= 1.0:
                continue
            market = str(row.get("market") or "")
            name = str(row.get("outcome_name") or "")
            if market == "h2h":
                side = None
                if name == home:
                    side = "home"
                elif name == away:
                    side = "away"
                elif name.lower() == "draw":
                    side = "draw"
                if side is None:
                    continue
                imps[side].append(1.0 / odds)
                book = str(row.get("bookmaker_key") or row.get("bookmaker_title") or "book")
                venue = by_book.setdefault(
                    book,
                    {
                        "venue": book,
                        "selection_prices": {},
                        "implied": {},
                        "edge_vs_model": {},
                    },
                )
                venue["selection_prices"][side] = odds
            elif market == "totals":
                try:
                    point = float(row.get("outcome_point"))
                except (TypeError, ValueError):
                    continue
                if abs(point - 2.5) > 1e-9:
                    continue
                if name.lower() == "over":
                    over_imps.append(1.0 / odds)
                elif name.lower() == "under":
                    under_imps.append(1.0 / odds)

        h = _median(imps["home"]) or 0.0
        d = _median(imps["draw"]) or 0.0
        a = _median(imps["away"]) or 0.0
        total = h + d + a
        model = (
            {"home": h / total, "draw": d / total, "away": a / total}
            if total > 0
            else {"home": 0.5, "draw": 0.25, "away": 0.25}
        )
        over = _median(over_imps)
        under = _median(under_imps)
        if over is not None and under is not None and (over + under) > 0:
            over_pct = 100.0 * over / (over + under)
        else:
            over_pct = 50.0
        fixtures.append(
            {
                "fixture": "%s vs %s" % (home, away),
                "kickoff": str(first.get("commence_time") or ""),
                "scores": [],
                "over_under": {"line": 2.5, "over": over_pct, "under": 100.0 - over_pct},
                "btts": None,
                "model_1x2": model,
                "model_source": "market_consensus",
                "venues": list(by_book.values()),
            }
        )
    return {"meta": {"generated": generated or os.path.basename(path)}, "fixtures": fixtures}


def merge_snapshot_feed(feed: Dict[str, Any], snapshot_feed: Dict[str, Any]) -> Dict[str, Any]:
    """Merge newest raw snapshot fixtures into an existing scores feed."""
    if not snapshot_feed or not snapshot_feed.get("fixtures"):
        return feed
    merged = dict(feed or {})
    by_fixture: Dict[str, Dict[str, Any]] = {}
    for fx in (feed or {}).get("fixtures") or []:
        if isinstance(fx, dict) and fx.get("fixture"):
            by_fixture[str(fx["fixture"])] = dict(fx)
    for fx in snapshot_feed.get("fixtures") or []:
        if isinstance(fx, dict) and fx.get("fixture"):
            old = by_fixture.get(str(fx["fixture"]), {})
            merged_fx = dict(old)
            merged_fx.update(fx)
            # Keep true model probabilities from the existing feed when the
            # raw snapshot only carries market-consensus pseudo-probs.
            if old.get("model_source") and old.get("model_source") != "market_consensus":
                merged_fx["model_1x2"] = old.get("model_1x2")
                merged_fx["model_source"] = old.get("model_source")
            by_fixture[str(fx["fixture"])] = merged_fx
    merged["fixtures"] = list(by_fixture.values())
    gen_old = str(((feed or {}).get("meta") or {}).get("generated") or "")
    gen_new = str((snapshot_feed.get("meta") or {}).get("generated") or "")
    merged["meta"] = {"generated": max(gen_old, gen_new)}
    return merged


def load_model_predictions_feed(path: str = "data/model_predictions.json") -> Dict[str, Any]:
    """Load the current model 1X2 file as a lightweight acca feed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return {}
    fixtures = []
    for row in obj.get("fixtures") or []:
        if not isinstance(row, dict) or not row.get("fixture"):
            continue
        fixtures.append(
            {
                "fixture": str(row.get("fixture")),
                "kickoff": str(row.get("kickoff") or ""),
                "scores": [],
                "over_under": {},
                "btts": None,
                "model_1x2": row.get("model") or {},
                "model_source": "model_predictions",
                "venues": [],
            }
        )
    return {"meta": {"generated": str((obj.get("meta") or {}).get("generated") or "")}, "fixtures": fixtures}


def merge_model_predictions(feed: Dict[str, Any], model_feed: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay true model probabilities onto a snapshot/scores feed."""
    if not model_feed or not model_feed.get("fixtures"):
        return feed
    merged = dict(feed or {})
    by_fixture: Dict[str, Dict[str, Any]] = {}
    for fx in (feed or {}).get("fixtures") or []:
        if isinstance(fx, dict) and fx.get("fixture"):
            by_fixture[str(fx["fixture"])] = dict(fx)
    for fx in model_feed.get("fixtures") or []:
        if not isinstance(fx, dict) or not fx.get("fixture"):
            continue
        old = by_fixture.get(str(fx["fixture"]), {})
        new = dict(old)
        for key in ("fixture", "kickoff", "model_1x2", "model_source"):
            if fx.get(key):
                new[key] = fx.get(key)
        if not new.get("venues"):
            new["venues"] = old.get("venues") or []
        by_fixture[str(fx["fixture"])] = new
    merged["fixtures"] = list(by_fixture.values())
    gen_old = str(((feed or {}).get("meta") or {}).get("generated") or "")
    gen_new = str((model_feed.get("meta") or {}).get("generated") or "")
    merged["meta"] = {"generated": max(gen_old, gen_new)}
    return merged


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _fixture_matches(fixture: str, terms: Sequence[str]) -> bool:
    low = _norm(fixture)
    return all(_norm(t) in low for t in terms)


def _fixture_has_any(fixture: str, terms: Sequence[str]) -> bool:
    low = _norm(fixture)
    return any(_norm(t) in low for t in terms)


def _fixtures(feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    fs = feed.get("fixtures") if isinstance(feed, dict) else None
    return [f for f in fs or [] if isinstance(f, dict)]


def _best_price(
    fx: Dict[str, Any],
    side: str,
    venue_keys: Sequence[str] = (),
) -> Tuple[Optional[float], Optional[str]]:
    best: Optional[float] = None
    venue: Optional[str] = None
    for v in fx.get("venues") or []:
        if not isinstance(v, dict):
            continue
        venue_raw = str(v.get("venue") or "")
        if venue_keys and venue_raw.lower() not in {x.lower() for x in venue_keys}:
            continue
        prices = v.get("selection_prices") or {}
        try:
            price = float(prices.get(side))
        except (TypeError, ValueError):
            continue
        if price > 1.0 and (best is None or price > best):
            best = price
            venue = venue_raw or "?"
    return best, venue


def _prob_to_fair(p: float) -> Optional[float]:
    return (1.0 / p) if p > 0.0 else None


def _fixture_sides(fixture: str) -> Tuple[str, str]:
    if " vs " in fixture:
        h, _, a = fixture.partition(" vs ")
        return h.strip(), a.strip()
    if " v " in fixture:
        h, _, a = fixture.partition(" v ")
        return h.strip(), a.strip()
    return fixture.strip(), "Away"


def _display_venue(venue: Optional[str]) -> str:
    """Human label safe for Telegram's legacy Markdown parser."""
    if not venue:
        return ""
    raw = str(venue)
    mapping = {
        "betfair_ex_uk": "Betfair Exchange",
        "betfair_ex_eu": "Betfair Exchange",
        "betfair_sb_uk": "Betfair Sportsbook",
        "paddypower": "Paddy Power",
        "paddy_power": "Paddy Power",
        "virginbet": "Virgin Bet",
        "virgin_bet": "Virgin Bet",
        "bet365": "bet365",
        "smarkets": "Smarkets",
        "matchbook": "Matchbook",
    }
    return mapping.get(raw.lower(), raw.replace("_", " ").strip().title())


def _candidate_1x2_legs(
    fx: Dict[str, Any],
    venue_keys: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    fixture = str(fx.get("fixture") or "?")
    home, away = _fixture_sides(fixture)
    labels = {"home": home, "draw": "Draw", "away": away}
    model = fx.get("model_1x2") or {}
    out: List[Dict[str, Any]] = []
    for side in ("home", "draw", "away"):
        try:
            p = float(model.get(side))
        except (TypeError, ValueError):
            continue
        price, venue = _best_price(fx, side, venue_keys)
        fair = _prob_to_fair(p)
        if fair is None:
            continue
        odds = price if price is not None else fair
        true_model = str(fx.get("model_source") or "") != "market_consensus"
        edge = (p * price - 1.0) if price is not None and true_model else None
        out.append(
            {
                "fixture": fixture,
                "market": "1X2",
                "selection": labels[side],
                "model_prob": p,
                "fair_odds": fair,
                "odds": odds,
                "venue": venue,
                "edge": edge,
                "priced": price is not None,
                "model_source": fx.get("model_source") or "",
            }
        )
    return out


def _component_builder_legs(fx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rank model-native same-game components for a bet-builder brief."""
    fixture = str(fx.get("fixture") or "?")
    legs: List[Dict[str, Any]] = []

    # Best 1X2 side by model probability.
    sides = sorted(_candidate_1x2_legs(fx), key=lambda x: -float(x["model_prob"]))
    if sides:
        leg = dict(sides[0])
        leg["market"] = "Match result"
        legs.append(leg)

    ou = fx.get("over_under") or {}
    try:
        line = float(ou.get("line", 2.5))
        over = float(ou.get("over")) / 100.0
        under = float(ou.get("under")) / 100.0
        if over >= under:
            p, sel = over, "Over %.1f goals" % line
        else:
            p, sel = under, "Under %.1f goals" % line
        legs.append(
            {
                "fixture": fixture,
                "market": "Goals",
                "selection": sel,
                "model_prob": p,
                "fair_odds": _prob_to_fair(p),
                "odds": _prob_to_fair(p),
                "venue": None,
                "edge": None,
                "priced": False,
            }
        )
    except (TypeError, ValueError):
        pass

    try:
        yes = float(fx.get("btts")) / 100.0
        no = 1.0 - yes
        if yes >= no:
            p, sel = yes, "BTTS Yes"
        else:
            p, sel = no, "BTTS No"
        legs.append(
            {
                "fixture": fixture,
                "market": "BTTS",
                "selection": sel,
                "model_prob": p,
                "fair_odds": _prob_to_fair(p),
                "odds": _prob_to_fair(p),
                "venue": None,
                "edge": None,
                "priced": False,
            }
        )
    except (TypeError, ValueError):
        pass

    scores = fx.get("scores") or []
    if scores:
        top = scores[0]
        try:
            p = float(top.get("prob")) / 100.0
        except (TypeError, ValueError):
            p = 0.0
        if p > 0.0:
            legs.append(
                {
                    "fixture": fixture,
                    "market": "Score lean",
                    "selection": "Top score %s" % (top.get("score") or "?"),
                    "model_prob": p,
                    "fair_odds": _prob_to_fair(p),
                    "odds": _prob_to_fair(p),
                    "venue": None,
                    "edge": None,
                    "priced": False,
                }
            )

    return legs


def _select_cross_fixture_legs(
    fixtures: Iterable[Dict[str, Any]],
    *,
    min_legs: int,
    min_leg_odds: Optional[float],
    max_leg_odds: Optional[float],
    min_total_odds: Optional[float],
    venue_keys: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """Pick one model-preferred leg per fixture satisfying promo constraints."""
    pool: List[Dict[str, Any]] = []
    for fx in fixtures:
        legs = _candidate_1x2_legs(fx, venue_keys)
        if min_leg_odds is not None:
            legs = [l for l in legs if float(l["odds"]) >= min_leg_odds]
        if max_leg_odds is not None:
            legs = [l for l in legs if float(l["odds"]) <= max_leg_odds]
        if not legs:
            continue
        # Prefer positive edge if there is a priced market. If nothing is
        # genuinely +EV, choose hit-probability over "least bad" longshots;
        # promo/free-bet extraction needs legs that can actually come in.
        legs = sorted(
            legs,
            key=lambda l: (
                max(float(l["edge"]), 0.0) if l.get("edge") is not None else 0.0,
                float(l["model_prob"]),
            ),
            reverse=True,
        )
        pool.append(legs[0])

    pool = sorted(
        pool,
        key=lambda l: (
            max(float(l["edge"]), 0.0) if l.get("edge") is not None else 0.0,
            float(l["model_prob"]),
        ),
        reverse=True,
    )
    selected: List[Dict[str, Any]] = []
    total = 1.0
    for leg in pool:
        selected.append(leg)
        total *= float(leg["odds"])
        if len(selected) >= min_legs and (
            min_total_odds is None or total >= min_total_odds
        ):
            break
    if len(selected) < min_legs:
        return []
    if min_total_odds is not None and total < min_total_odds:
        return []
    return selected


def _candidate_fixtures(feed: Dict[str, Any], spec: PromoSpec) -> List[Dict[str, Any]]:
    fs = _fixtures(feed)
    if spec.fixture_terms:
        fs = [f for f in fs if _fixture_matches(str(f.get("fixture") or ""), spec.fixture_terms)]
    elif spec.any_team_terms:
        fs = [f for f in fs if _fixture_has_any(str(f.get("fixture") or ""), spec.any_team_terms)]
    if spec.require_model:
        fs = [f for f in fs if str(f.get("model_source") or "") != "market_consensus"]
    if spec.fixture_terms:
        return fs
    if spec.any_team_terms:
        return fs
    return fs


def build_promo_accas(feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return one operator brief per configured promo."""
    out: List[Dict[str, Any]] = []
    for spec in PROMOS:
        matches = _candidate_fixtures(feed, spec)
        status = "ready"
        reason = ""
        legs: List[Dict[str, Any]] = []

        if not matches:
            status = "pending"
            if spec.fixture_terms:
                reason = "fixture not present in current scores feed: %s" % " vs ".join(spec.fixture_terms)
            elif spec.any_team_terms:
                reason = "no current fixture involving %s in scores feed" % "/".join(spec.any_team_terms)
            else:
                reason = "no current fixtures in scores feed"
        elif spec.manual_legs:
            fx_name = str(matches[0].get("fixture") or " / ".join(spec.fixture_terms or spec.any_team_terms))
            legs = [
                {
                    "fixture": fx_name,
                    "market": "Manual builder leg" if spec.min_legs > 1 else "Manual promo leg",
                    "selection": leg,
                    "model_prob": 0.0,
                    "fair_odds": None,
                    "odds": 0.0,
                    "venue": None,
                    "edge": None,
                    "priced": False,
                    "manual": True,
                }
                for leg in spec.manual_legs
            ]
            status = "manual"
            if len(legs) < spec.min_legs:
                status = "incomplete"
                reason = "manual template has fewer legs than promo minimum"
            else:
                reason = spec.manual_price_note or "manual app pricing required"
        elif spec.bet_builder:
            # Pick the first matched fixture. In the live feed this should be
            # the targeted England/Scotland fixture. Components are ranked but
            # not joint-priced.
            legs = _component_builder_legs(matches[0])[:spec.min_legs]
            if len(legs) < spec.min_legs:
                status = "incomplete"
                reason = "not enough model-native builder components"
            else:
                component_total = 1.0
                for leg in legs:
                    component_total *= float(leg.get("odds") or 1.0)
                if spec.min_total_odds is not None and component_total < spec.min_total_odds:
                    status = "incomplete"
                    reason = (
                        "component fair-odds total %.2f is below promo minimum %.2f; "
                        "only use if the app's actual builder price clears the threshold"
                        % (component_total, spec.min_total_odds)
                    )
                else:
                    reason = "component-ranked only; joint bet-builder EV not priced yet"
        else:
            legs = _select_cross_fixture_legs(
                matches,
                min_legs=spec.min_legs,
                min_leg_odds=spec.min_leg_odds,
                max_leg_odds=spec.max_leg_odds,
                min_total_odds=spec.min_total_odds,
                venue_keys=spec.venue_keys,
            )
            if not legs:
                status = "incomplete"
                reason = "not enough qualifying 1X2 legs in current feed"
            else:
                reason = "cross-fixture 1X2 candidate; check book accepts all legs"

        total_odds = 1.0
        for l in legs:
            total_odds *= float(l.get("odds") or 1.0)

        out.append(
            {
                "site": spec.site,
                "title": spec.title,
                "accounts": list(spec.accounts),
                "min_legs": spec.min_legs,
                "min_total_odds": spec.min_total_odds,
                "min_leg_odds": spec.min_leg_odds,
                "max_leg_odds": spec.max_leg_odds,
                "max_free_bet": spec.max_free_bet,
                "venue_keys": list(spec.venue_keys),
                "require_model": spec.require_model,
                "boost_pct": spec.boost_pct,
                "bet_builder": spec.bet_builder,
                "notes": spec.notes,
                "status": status,
                "reason": reason,
                "manual_price_note": spec.manual_price_note,
                "legs": legs,
                "total_odds": total_odds if legs else None,
            }
        )
    return out


def _fmt_money(x: Optional[float]) -> str:
    return ("£%.0f" % x) if x is not None else ""


def _fmt_leg(leg: Dict[str, Any]) -> str:
    if leg.get("manual"):
        return "%s — %s (%s; price in app)" % (
            leg.get("fixture", "?"),
            leg.get("selection", "?"),
            leg.get("market", "Manual leg"),
        )
    p = float(leg.get("model_prob") or 0.0)
    odds = float(leg.get("odds") or 0.0)
    venue = _display_venue(leg.get("venue"))
    priced = " @ %.2f%s" % (odds, (" " + venue) if venue else "")
    if not leg.get("priced") and venue is None:
        priced = " fair %.2f" % odds if odds > 0 else ""
    edge = leg.get("edge")
    edge_s = (" edge %+.1f%%" % (float(edge) * 100.0)) if edge is not None else ""
    label = "model" if leg.get("model_source") != "market_consensus" else "mkt-consensus"
    return "%s — %s (%s)%s | %s %.1f%%%s" % (
        leg.get("fixture", "?"),
        leg.get("selection", "?"),
        leg.get("market", "?"),
        priced,
        label,
        p * 100.0,
        edge_s,
    )


def format_promo_accas(rows: List[Dict[str, Any]]) -> str:
    """Format promo acca briefs for Telegram Markdown."""
    if not rows:
        return "🎟 *Promo accas*\nNo promo briefs configured."

    lines = [
        "🎟 *Promo accas / bet builders*",
        "_Use these as offer-extraction candidates. Log placed slips as `offer`; only model-native legs carry model probabilities._",
        "",
    ]

    for row in rows:
        accts = "/".join(row.get("accounts") or [])
        status = str(row.get("status") or "?")
        icon = "✅" if status == "ready" else ("📝" if status == "manual" else ("🟡" if status == "incomplete" else "⏳"))
        bits = ["%d+ legs" % int(row.get("min_legs") or 0)]
        if row.get("min_leg_odds") is not None:
            bits.append("each %.2f+" % float(row["min_leg_odds"]))
        if row.get("max_leg_odds") is not None:
            bits.append("leg cap %.2f" % float(row["max_leg_odds"]))
        if row.get("min_total_odds") is not None:
            bits.append("combined %.2f+" % float(row["min_total_odds"]))
        if row.get("max_free_bet") is not None:
            bits.append("max free bet %s" % _fmt_money(row.get("max_free_bet")))
        if row.get("boost_pct") is not None:
            bits.append("+%.0f%% winnings boost" % float(row["boost_pct"]))

        lines.append("%s *%s — %s* (%s)" % (icon, row["site"], row["title"], accts))
        lines.append("   Terms: %s" % "; ".join(bits))
        if row.get("notes"):
            lines.append("   Note: %s" % row["notes"])
        if row.get("reason"):
            lines.append("   Status: %s" % row["reason"])

        legs = row.get("legs") or []
        if legs:
            total = row.get("total_odds")
            if total and status != "manual":
                lines.append("   Candidate total: %.2f" % float(total))
            elif status == "manual":
                lines.append("   Candidate total: app-priced — only place if it clears the terms.")
            for i, leg in enumerate(legs, 1):
                lines.append("   %d. %s" % (i, _fmt_leg(leg)))
        else:
            lines.append("   Candidate: not currently priceable from feed.")
        lines.append("")

    lines.append("⚠️ Bet builders are component-ranked, not joint-priced yet. Avoid adding corners/cards/SOT/scorers unless you mark the slip as offer/punt and accept that the model is not pricing that leg.")
    return "\n".join(lines).rstrip()


# Backwards-compatible names for older imports/tests.
def build_accas_from_odds(odds_df: Any, fixtures_meta: Any = None, **_: Any) -> List[Dict[str, Any]]:
    if isinstance(odds_df, dict):
        return build_promo_accas(odds_df)
    return []


def format_accas(accas: List[Dict[str, Any]]) -> str:
    return format_promo_accas(accas)
