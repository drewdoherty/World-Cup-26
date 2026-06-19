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

import json
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
    max_free_bet: Optional[float] = None
    fixture_terms: Tuple[str, ...] = ()
    any_team_terms: Tuple[str, ...] = ()
    bet_builder: bool = False
    boost_pct: Optional[float] = None
    notes: str = ""


PROMOS: Tuple[PromoSpec, ...] = (
    PromoSpec(
        site="Paddy Power",
        title="England/Ghana free acca",
        accounts=("A1", "A2"),
        min_legs=3,
        min_total_odds=2.0,
        fixture_terms=("England", "Ghana"),
        notes="3+ legs, combined odds EVS/2.0+. Use as offer, not model CLV.",
    ),
    PromoSpec(
        site="Betfair Sportsbook",
        title="Max £10 free bet acca",
        accounts=("A1", "A2"),
        min_legs=3,
        min_leg_odds=1.5,
        max_free_bet=10.0,
        notes="Minimum 3 legs, every leg 1.5+.",
    ),
    PromoSpec(
        site="Betfred",
        title="ENG/SCOT World Cup bet builder",
        accounts=("A1", "A2"),
        min_legs=3,
        min_total_odds=4.0,
        any_team_terms=("England", "Scotland"),
        bet_builder=True,
        notes="Single bet builder on any England/Scotland WC match, 3+ legs, 3/1+.",
    ),
    PromoSpec(
        site="Virgin Bet",
        title="50% winnings boost — Scotland vs Morocco bet builder",
        accounts=("A1", "A2"),
        min_legs=4,
        min_total_odds=2.0,
        fixture_terms=("Scotland", "Morocco"),
        bet_builder=True,
        boost_pct=50.0,
        notes=(
            "Screenshot terms: one bet/customer/day, 4+ legs, combined odds EVS/2.0+, "
            "Bet Builders only, max stakes apply."
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


def _best_price(fx: Dict[str, Any], side: str) -> Tuple[Optional[float], Optional[str]]:
    best: Optional[float] = None
    venue: Optional[str] = None
    for v in fx.get("venues") or []:
        if not isinstance(v, dict):
            continue
        prices = v.get("selection_prices") or {}
        try:
            price = float(prices.get(side))
        except (TypeError, ValueError):
            continue
        if price > 1.0 and (best is None or price > best):
            best = price
            venue = str(v.get("venue") or "?")
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


def _candidate_1x2_legs(fx: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        price, venue = _best_price(fx, side)
        fair = _prob_to_fair(p)
        if fair is None:
            continue
        odds = price if price is not None else fair
        edge = (p * price - 1.0) if price is not None else None
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
    min_total_odds: Optional[float],
) -> List[Dict[str, Any]]:
    """Pick one model-preferred leg per fixture satisfying promo constraints."""
    pool: List[Dict[str, Any]] = []
    for fx in fixtures:
        legs = _candidate_1x2_legs(fx)
        if min_leg_odds is not None:
            legs = [l for l in legs if float(l["odds"]) >= min_leg_odds]
        if not legs:
            continue
        # Prefer positive edge if there is a priced market, otherwise highest model p.
        legs = sorted(
            legs,
            key=lambda l: (
                float(l["edge"]) if l.get("edge") is not None else -9.0,
                float(l["model_prob"]),
            ),
            reverse=True,
        )
        pool.append(legs[0])

    pool = sorted(pool, key=lambda l: -float(l["model_prob"]))
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
        return [f for f in fs if _fixture_matches(str(f.get("fixture") or ""), spec.fixture_terms)]
    if spec.any_team_terms:
        return [f for f in fs if _fixture_has_any(str(f.get("fixture") or ""), spec.any_team_terms)]
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
                min_total_odds=spec.min_total_odds,
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
                "max_free_bet": spec.max_free_bet,
                "boost_pct": spec.boost_pct,
                "bet_builder": spec.bet_builder,
                "notes": spec.notes,
                "status": status,
                "reason": reason,
                "legs": legs,
                "total_odds": total_odds if legs else None,
            }
        )
    return out


def _fmt_money(x: Optional[float]) -> str:
    return ("£%.0f" % x) if x is not None else ""


def _fmt_leg(leg: Dict[str, Any]) -> str:
    p = float(leg.get("model_prob") or 0.0)
    odds = float(leg.get("odds") or 0.0)
    venue = leg.get("venue")
    priced = " @ %.2f%s" % (odds, (" " + venue) if venue else "")
    if not leg.get("priced") and venue is None:
        priced = " fair %.2f" % odds if odds > 0 else ""
    edge = leg.get("edge")
    edge_s = (" edge %+.1f%%" % (float(edge) * 100.0)) if edge is not None else ""
    return "%s — %s (%s)%s | model %.1f%%%s" % (
        leg.get("fixture", "?"),
        leg.get("selection", "?"),
        leg.get("market", "?"),
        priced,
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
        icon = "✅" if status == "ready" else ("🟡" if status == "incomplete" else "⏳")
        bits = ["%d+ legs" % int(row.get("min_legs") or 0)]
        if row.get("min_leg_odds") is not None:
            bits.append("each %.2f+" % float(row["min_leg_odds"]))
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
            if total:
                lines.append("   Candidate total: %.2f" % float(total))
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
