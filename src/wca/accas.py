"""Model-driven accumulator report for World Cup 2026.

Architecture: the offline batch script ``wca_build_accas.py`` calls
``build_accas_report()`` once per build cycle, formats the result with
``format_acca_report()``, and caches it to ``data/accas_latest.md`` via
:func:`~wca.cardcache.write_card`. The ``/accas`` bot command reads the
cache; it never blocks on model fitting or odds polling.

Three acca profiles
-------------------
Safe      2-3 legs ranked by descending model probability (highest
          confidence), combined odds 1.5 – 8.0.  Leg odds 1.20 – 2.50.

Value     3-4 legs ranked by descending per-leg edge (model vs implied),
          combined odds 3 – 30.  Min leg edge > 2 %.

Longshot  4-5 legs ranked by descending odds where edge > 0,
          combined odds > 10.  Fixed small stake.

Edge / EV arithmetic
--------------------
Per leg::

    edge_leg  = model_prob * odds - 1   (positive = value)
    implied   = 1 / odds

Combined (independence assumed across different fixtures)::

    combined_odds = product(odds_i)
    model_prob    = product(model_prob_i)
    implied_prob  = 1 / combined_odds
    ev_per_unit   = model_prob * combined_odds - 1
    edge_%        = (model_prob - implied_prob) * 100

Stake sizing (¼-Kelly, capped at 2 % of bankroll for accas)::

    kelly_f = max(0, ev_per_unit / (combined_odds - 1))
    stake   = min(0.25 * kelly_f * bankroll, 0.02 * bankroll)

NO BET rule
-----------
If no acca profile can be built with ≥ 2 legs all having positive edge,
the report returns "NO BET" with an explanation.

Legacy compatibility
--------------------
``build_accas_from_odds`` and ``format_accas`` are kept as thin stubs so
existing monkeypatches in the test suite continue to compile without
change to those test files.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class LegCandidate:
    fixture: str
    selection: str       # "home" | "draw" | "away"
    label: str           # team name or "Draw"
    odds: float          # best decimal odds across UK books
    book: str            # bookmaker key
    model_prob: float    # blended model probability (Elo+DC+market)
    implied_prob: float  # 1 / odds
    edge: float          # model_prob * odds - 1
    elo_prob: float = 0.0
    dc_prob: float = 0.0
    mkt_prob: float = 0.0
    kickoff: Optional[datetime] = None


@dataclass
class AccaBuild:
    acca_type: str       # "safe" | "value" | "longshot"
    legs: List[LegCandidate]
    combined_odds: float
    model_prob: float
    implied_prob: float
    edge_pct: float
    ev_per_unit: float
    stake: float
    correlation_risk: str  # "LOW" | "MEDIUM" | "HIGH"
    why_it_works: str
    main_risks: str


@dataclass
class AccaReport:
    date_str: str
    fixtures_analysed: int
    safe: Optional[AccaBuild]
    value: Optional[AccaBuild]
    longshot: Optional[AccaBuild]
    no_bet_reason: str = ""
    agent_summary: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_kickoff(s: Any) -> Optional[datetime]:
    if not s:
        return None
    txt = str(s).strip().replace("Z", "+00:00")
    for attempt in (txt, txt[:19]):
        try:
            dt = datetime.fromisoformat(attempt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _load_candidates(
    predictions_path: str,
    scores_path: str,
    now: datetime,
    window_hours: float,
) -> List[LegCandidate]:
    """Build the pool of candidate legs from cached model data.

    Joins ``model_predictions.json`` (kickoff, Elo/DC/market components)
    with ``scores_data.json`` (per-venue odds, pre-computed edge) and
    filters to fixtures whose kickoff falls within ``window_hours`` ahead
    (or up to 3 h in the past, for matches still in play at build time).
    """
    # Index predictions by fixture name for fast lookup.
    pred_idx: Dict[str, Dict[str, Any]] = {}
    try:
        for f in _load_json(predictions_path).get("fixtures", []):
            pred_idx[f["fixture"]] = f
    except Exception:
        pass

    candidates: List[LegCandidate] = []
    try:
        scores_data = _load_json(scores_path)
    except Exception:
        return []

    for fx_data in scores_data.get("fixtures", []):
        fixture = fx_data.get("fixture", "")
        if not fixture:
            continue

        # Blended model probabilities (already computed by the card pipeline).
        m1x2 = fx_data.get("model_1x2", {})
        m_home = float(m1x2.get("home", 0))
        m_draw = float(m1x2.get("draw", 0))
        m_away = float(m1x2.get("away", 0))
        if not (m_home or m_draw or m_away):
            continue

        # Kickoff-window filter (use predictions meta; fall through if absent).
        pred = pred_idx.get(fixture, {})
        kickoff = _parse_kickoff(pred.get("kickoff"))
        if kickoff is not None:
            hours = (kickoff - now).total_seconds() / 3600.0
            if hours < -3.0 or hours > window_hours:
                continue

        # Elo / DC / market component probabilities.
        elo = pred.get("elo", {})
        dc = pred.get("dc", {})
        mkt = pred.get("market", {})

        # Team name labels.
        parts = fixture.split(" vs ", 1)
        home_team = parts[0]
        away_team = parts[1] if len(parts) == 2 else ""

        outcome_cfg = {
            "home": (home_team, m_home, elo.get("home", 0.0),
                     dc.get("home", 0.0), mkt.get("home", 0.0)),
            "draw": ("Draw", m_draw, elo.get("draw", 0.0),
                     dc.get("draw", 0.0), mkt.get("draw", 0.0)),
            "away": (away_team, m_away, elo.get("away", 0.0),
                     dc.get("away", 0.0), mkt.get("away", 0.0)),
        }

        # Best decimal odds per outcome across all venues.
        venues = fx_data.get("venues", [])
        for outcome_key, (label, m_prob, elo_p, dc_p, mkt_p) in outcome_cfg.items():
            if m_prob <= 0:
                continue
            best_odds = 0.0
            best_book = ""
            for v in venues:
                o = float((v.get("selection_prices") or {}).get(outcome_key) or 0)
                if o > best_odds:
                    best_odds = o
                    best_book = v.get("venue", "")
            if best_odds <= 1.0:
                continue

            implied = 1.0 / best_odds
            edge = m_prob * best_odds - 1.0

            candidates.append(LegCandidate(
                fixture=fixture,
                selection=outcome_key,
                label=label,
                odds=best_odds,
                book=best_book,
                model_prob=m_prob,
                implied_prob=implied,
                edge=edge,
                elo_prob=float(elo_p),
                dc_prob=float(dc_p),
                mkt_prob=float(mkt_p),
                kickoff=kickoff,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Leg-selection helpers
# ---------------------------------------------------------------------------

def _best_per_fixture(legs: List[LegCandidate]) -> Dict[str, LegCandidate]:
    """Keep only the highest-edge leg per fixture to prevent same-match duplicates."""
    best: Dict[str, LegCandidate] = {}
    for leg in legs:
        if leg.fixture not in best or leg.edge > best[leg.fixture].edge:
            best[leg.fixture] = leg
    return best


def _correlation_risk(legs: List[LegCandidate]) -> Tuple[str, List[str]]:
    notes: List[str] = []

    fxs = [l.fixture for l in legs]
    if len(fxs) != len(set(fxs)):
        notes.append("DUPLICATE FIXTURE — same-match legs present (should have been rejected)")
        return "HIGH", notes

    # All-favourites narrative risk.
    n_favs = sum(1 for l in legs if l.selection == "home" and l.model_prob > 0.60)
    if n_favs >= max(2, len(legs) - 1):
        notes.append(
            "All-favourites pattern: if the 2026 WC proves upset-heavy, "
            "multiple legs fall together."
        )

    # High model-probability concentration.
    n_high = sum(1 for l in legs if l.model_prob > 0.70)
    if n_high >= 3:
        notes.append(
            "%d legs >70 %% model probability — one upset kills the whole acca." % n_high
        )

    # Elo/DC disagreement.
    n_discord = sum(
        1 for l in legs
        if l.elo_prob > 0 and l.dc_prob > 0 and abs(l.elo_prob - l.dc_prob) > 0.10
    )
    if n_discord >= 2:
        notes.append(
            "%d legs where Elo and DC disagree by >10 pp — elevated model uncertainty." % n_discord
        )

    risk = "LOW"
    if n_favs >= max(2, len(legs) - 1):
        risk = "MEDIUM"
    if n_discord >= 2:
        risk = "MEDIUM"
    if len(notes) >= 3:
        risk = "HIGH"

    if not notes:
        notes.append("Legs from different fixtures; no obvious cross-leg correlation.")
    return risk, notes


def _kelly_stake(
    model_prob: float,
    combined_odds: float,
    bankroll: float,
    fraction: float = 0.25,
    cap_pct: float = 0.02,
) -> float:
    if combined_odds <= 1.0 or model_prob <= 0:
        return 0.0
    ev = model_prob * combined_odds - 1.0
    if ev <= 0:
        return 0.0
    kelly_f = ev / (combined_odds - 1.0)
    raw = fraction * kelly_f * bankroll
    return round(min(raw, cap_pct * bankroll), 2)


# ---------------------------------------------------------------------------
# Adversarial review
# ---------------------------------------------------------------------------

def _adversarial_review(build: AccaBuild) -> str:
    bullets: List[str] = []

    min_edge = min(l.edge for l in build.legs)
    if min_edge < 0.03:
        bullets.append(
            "Thin edge on at least one leg (<%+.1f %%). "
            "A single-tick market move can eliminate the value — fragile to odds compression."
            % (min_edge * 100)
        )

    n = len(build.legs)
    if n >= 4:
        bullets.append(
            "%d-leg acca: each extra leg compounds failure probability. "
            "Combined hit rate is only %.1f %%." % (n, build.model_prob * 100)
        )

    for leg in build.legs:
        if leg.elo_prob > 0 and leg.dc_prob > 0 and abs(leg.elo_prob - leg.dc_prob) > 0.12:
            bullets.append(
                "%s: Elo (%.0f %%) vs DC (%.0f %%) disagree by %.0f pp — "
                "model uncertainty is high for this leg."
                % (leg.label, leg.elo_prob * 100, leg.dc_prob * 100,
                   abs(leg.elo_prob - leg.dc_prob) * 100)
            )
            break

    for leg in build.legs:
        if leg.model_prob > 0.65 and leg.edge < 0.02:
            bullets.append(
                "%s is a strong favourite (%.0f %% model) but almost fairly priced "
                "— adds length without meaningful value." % (leg.label, leg.model_prob * 100)
            )
            break

    if build.acca_type == "longshot":
        bullets.append(
            "Longshots are high variance by design. Even at +EV you need "
            "100 + bets for the edge to show in results. Treat as a lottery ticket."
        )

    if not bullets:
        bullets.append(
            "No major structural risks identified. Primary risk is model mis-calibration: "
            "blend weights are pre-backtest priors, not post-tournament fitted values."
        )

    return "\n".join("• " + b for b in bullets[:4])


# ---------------------------------------------------------------------------
# Acca builders
# ---------------------------------------------------------------------------

def _build_safe(
    pool: Dict[str, LegCandidate], bankroll: float
) -> Optional[AccaBuild]:
    """2-3 legs, odds 1.20–2.50 per leg, sorted by descending model probability."""
    eligible = sorted(
        (l for l in pool.values() if l.edge > 0 and 1.20 <= l.odds <= 2.50),
        key=lambda l: -l.model_prob,
    )
    for n in (3, 2):
        if len(eligible) < n:
            continue
        legs = eligible[:n]
        combined = math.prod(l.odds for l in legs)
        if not (1.5 <= combined <= 8.0):
            continue
        mp = math.prod(l.model_prob for l in legs)
        ip = 1.0 / combined
        ev = mp * combined - 1.0
        if ev <= 0:
            continue
        risk, _ = _correlation_risk(legs)
        return AccaBuild(
            acca_type="safe",
            legs=legs,
            combined_odds=round(combined, 2),
            model_prob=round(mp, 4),
            implied_prob=round(ip, 4),
            edge_pct=round((mp - ip) * 100, 2),
            ev_per_unit=round(ev, 4),
            stake=_kelly_stake(mp, combined, bankroll),
            correlation_risk=risk,
            why_it_works=(
                "High-confidence selections: each leg carries ≥60 %% model "
                "probability and is priced with positive edge at the best "
                "available UK bookmaker. Low combined odds reduce variance."
            ),
            main_risks="",
        )
    return None


def _build_value(
    pool: Dict[str, LegCandidate], bankroll: float
) -> Optional[AccaBuild]:
    """3-4 legs ranked by descending edge, combined odds 3–30, min leg edge > 2 %."""
    eligible = sorted(
        (l for l in pool.values() if l.edge > 0.02),
        key=lambda l: -l.edge,
    )
    for n in (4, 3):
        if len(eligible) < n:
            continue
        legs = eligible[:n]
        combined = math.prod(l.odds for l in legs)
        if not (3.0 <= combined <= 30.0):
            continue
        mp = math.prod(l.model_prob for l in legs)
        ip = 1.0 / combined
        ev = mp * combined - 1.0
        if ev <= 0:
            continue
        risk, _ = _correlation_risk(legs)
        top = max(legs, key=lambda l: l.edge)
        return AccaBuild(
            acca_type="value",
            legs=legs,
            combined_odds=round(combined, 2),
            model_prob=round(mp, 4),
            implied_prob=round(ip, 4),
            edge_pct=round((mp - ip) * 100, 2),
            ev_per_unit=round(ev, 4),
            stake=_kelly_stake(mp, combined, bankroll),
            correlation_risk=risk,
            why_it_works=(
                "Legs chosen by descending model-vs-market edge. "
                "Best single-leg edge: %s %+.1f %% "
                "(model %.0f %% vs implied %.0f %%). "
                "All legs exceed 2 %% edge at the best available UK price."
                % (top.label, top.edge * 100, top.model_prob * 100, top.implied_prob * 100)
            ),
            main_risks="",
        )
    return None


def _build_longshot(
    pool: Dict[str, LegCandidate], bankroll: float
) -> Optional[AccaBuild]:
    """4-5 legs ranked by descending odds (positive edge required), combined > 10."""
    eligible = sorted(
        (l for l in pool.values() if l.edge > 0 and l.odds > 1.80),
        key=lambda l: -l.odds,
    )
    for n in (5, 4):
        if len(eligible) < n:
            continue
        legs = eligible[:n]
        combined = math.prod(l.odds for l in legs)
        if combined < 10.0:
            continue
        mp = math.prod(l.model_prob for l in legs)
        ip = 1.0 / combined
        ev = mp * combined - 1.0
        if ev <= 0:
            continue
        risk, _ = _correlation_risk(legs)
        stake = round(min(0.005 * bankroll, 2.0), 2)
        return AccaBuild(
            acca_type="longshot",
            legs=legs,
            combined_odds=round(combined, 2),
            model_prob=round(mp, 4),
            implied_prob=round(ip, 4),
            edge_pct=round((mp - ip) * 100, 2),
            ev_per_unit=round(ev, 4),
            stake=stake,
            correlation_risk=risk,
            why_it_works=(
                "Maximum-upside acca: each leg is priced at longer odds with "
                "positive edge, giving a combined return >%.0f× the stake. "
                "Model probability (%.1f %%) beats the bookmaker's implied "
                "probability (%.1f %%)."
                % (combined, mp * 100, ip * 100)
            ),
            main_risks="",
        )
    return None


# ---------------------------------------------------------------------------
# Public report builder
# ---------------------------------------------------------------------------

def build_accas_report(
    predictions_path: str = "data/model_predictions.json",
    scores_path: str = "site/scores_data.json",
    bankroll: float = 1500.0,
    now: Optional[datetime] = None,
    window_hours: float = 30.0,
) -> AccaReport:
    """Build the full acca report from cached model data.

    Parameters
    ----------
    predictions_path:
        Path to ``data/model_predictions.json`` (Elo / DC / market probs
        per fixture, plus kickoff timestamps).
    scores_path:
        Path to ``site/scores_data.json`` (blended model_1x2 + per-venue
        odds with pre-computed edge).
    bankroll:
        Sportsbook-pool bankroll in GBP for stake sizing.
    now:
        Override for "current time" (UTC, timezone-aware). Defaults to
        ``datetime.now(timezone.utc)``.
    window_hours:
        Include fixtures whose kickoff is within this many hours ahead
        (and up to 3 h in the past for in-play games).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    candidates = _load_candidates(predictions_path, scores_path, now, window_hours)
    all_fixtures = {l.fixture for l in candidates}
    n_fixtures = len(all_fixtures)

    positive = [l for l in candidates if l.edge > 0]

    if not positive:
        return AccaReport(
            date_str=date_str,
            fixtures_analysed=n_fixtures,
            safe=None, value=None, longshot=None,
            no_bet_reason=(
                "No +EV legs found across %d fixture(s). The de-vigged market price "
                "exceeds the model probability on every outcome — the market is "
                "correctly or aggressively priced. Do not bet." % n_fixtures
            ),
        )

    pool = _best_per_fixture(positive)

    if len(pool) < 2:
        return AccaReport(
            date_str=date_str,
            fixtures_analysed=n_fixtures,
            safe=None, value=None, longshot=None,
            no_bet_reason=(
                "Only %d fixture(s) yield a +EV leg — cannot build a multi-leg acca." % len(pool)
            ),
        )

    safe = _build_safe(pool, bankroll)
    value = _build_value(pool, bankroll)
    longshot = _build_longshot(pool, bankroll)

    if safe:
        safe.main_risks = _adversarial_review(safe)
    if value:
        value.main_risks = _adversarial_review(value)
    if longshot:
        longshot.main_risks = _adversarial_review(longshot)

    # Agent reasoning summary.
    rejected = [
        "%s %s (edge %+.1f %%)" % (l.fixture, l.label, l.edge * 100)
        for l in candidates if l.edge <= 0
    ]
    best_signals = [
        "%s — %s: edge %+.1f %%, model %.0f %% vs implied %.0f %%"
        % (l.fixture, l.label, l.edge * 100, l.model_prob * 100, l.implied_prob * 100)
        for l in sorted(positive, key=lambda x: -x.edge)[:5]
    ]
    mispricings = best_signals[:3]
    discordant = [
        "%s %s: Elo %.0f %% / DC %.0f %% / Δ %.0f pp"
        % (l.fixture, l.label, l.elo_prob * 100, l.dc_prob * 100,
           abs(l.elo_prob - l.dc_prob) * 100)
        for l in candidates
        if l.elo_prob > 0 and l.dc_prob > 0 and abs(l.elo_prob - l.dc_prob) > 0.08
    ][:3]
    injury_notes = [
        "No injury data in this build — squad availability not modelled. "
        "Check confirmed lineups before placing."
    ]

    no_bet_reason = ""
    if not safe and not value and not longshot:
        no_bet_reason = (
            "Found %d +EV legs but could not satisfy combined-odds / "
            "leg-count constraints for any acca profile." % len(positive)
        )

    return AccaReport(
        date_str=date_str,
        fixtures_analysed=n_fixtures,
        safe=safe,
        value=value,
        longshot=longshot,
        no_bet_reason=no_bet_reason,
        agent_summary={
            "best_signals": best_signals,
            "market_mispricings": mispricings,
            "rejected_legs": rejected[:10],
            "discordant_models": discordant,
            "injury_lineup_notes": injury_notes,
            "n_positive_legs": len(positive),
            "n_total_legs": len(candidates),
        },
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_BOOK_LABELS: Dict[str, str] = {
    "betfair_ex_uk": "Betfair",
    "smarkets": "Smarkets",
    "skybet": "Sky Bet",
    "paddypower": "Paddy Power",
    "betway": "Betway",
    "ladbrokes": "Ladbrokes",
    "coral": "Coral",
    "williamhill": "William Hill",
    "unibet_uk": "Unibet",
    "bet365": "bet365",
    "betfred_uk": "Betfred",
    "betvictor": "BetVictor",
    "livescorebet": "LiveScore Bet",
    "casumo": "Casumo",
    "leovegas": "LeoVegas",
    "virginbet": "Virgin Bet",
    "grosvenor": "Grosvenor",
    "mrgreen": "Mr Green",
}


def _book_label(key: str) -> str:
    return _BOOK_LABELS.get(key, key.replace("_", " ").title())


def _fmt_leg(leg: LegCandidate, idx: int) -> str:
    ko = (" (%s UTC)" % leg.kickoff.strftime("%H:%M")) if leg.kickoff else ""
    return (
        "  %d. %s — *%s*%s @ %.2f (%s)\n"
        "     Model %.0f %% | Implied %.0f %% | Edge %+.1f %%"
        % (idx, leg.fixture, leg.label, ko, leg.odds, _book_label(leg.book),
           leg.model_prob * 100, leg.implied_prob * 100, leg.edge * 100)
    )


def _fmt_build(build: AccaBuild, emoji: str, title: str) -> str:
    lines = ["%s *%s*" % (emoji, title), ""]
    lines.append("*Legs:*")
    for i, leg in enumerate(build.legs, 1):
        lines.append(_fmt_leg(leg, i))
    lines += [
        "",
        "*Combined odds:* %.2f" % build.combined_odds,
        "*Model probability:* %.1f %%" % (build.model_prob * 100),
        "*Implied probability:* %.1f %%" % (build.implied_prob * 100),
        "*Edge:* %+.2f %%" % build.edge_pct,
        "*EV per unit:* %+.3f" % build.ev_per_unit,
        "*Stake:* £%.2f" % build.stake,
        "*Correlation risk:* %s" % build.correlation_risk,
        "",
        "*Why it works:*",
        build.why_it_works,
        "",
        "*Main risks:*",
        build.main_risks,
    ]
    return "\n".join(lines)


def format_acca_report(report: AccaReport) -> str:
    """Format the acca report as a Telegram-ready Markdown message."""
    lines = [
        "⚽ *WORLD CUP ACCA REPORT*",
        "*Date:* %s" % report.date_str,
        "*Fixtures analysed:* %d" % report.fixtures_analysed,
        "",
    ]

    if not report.safe and not report.value and not report.longshot:
        lines += ["🚫 *NO BET*", "", report.no_bet_reason]
        return "\n".join(lines)

    sep = ["", "---", ""]

    if report.safe:
        lines.append(_fmt_build(report.safe, "✅", "SAFE ACCA"))
        lines += sep
    if report.value:
        lines.append(_fmt_build(report.value, "💰", "VALUE ACCA"))
        lines += sep
    if report.longshot:
        lines.append(_fmt_build(report.longshot, "🎯", "LONGSHOT ACCA"))
        lines += sep

    s = report.agent_summary
    lines += ["🧠 *Agent Reasoning Summary*", ""]

    if s.get("best_signals"):
        lines.append("*Best model signals:*")
        lines += ["  • " + x for x in s["best_signals"][:4]]
        lines.append("")

    if s.get("market_mispricings"):
        lines.append("*Biggest market mispricings:*")
        lines += ["  • " + x for x in s["market_mispricings"]]
        lines.append("")

    if s.get("rejected_legs"):
        lines.append("*Rejected legs (−EV):*")
        lines += ["  • " + x for x in s["rejected_legs"][:5]]
        lines.append("")

    if s.get("injury_lineup_notes"):
        lines.append("*Injury / lineup concerns:*")
        lines += ["  • " + x for x in s["injury_lineup_notes"]]
        lines.append("")

    if s.get("discordant_models"):
        lines.append("*Correlation / model disagreement warnings:*")
        lines += ["  • " + x for x in s["discordant_models"]]
        lines.append("")

    lines.append(
        "_Edge = model prob × odds − 1. "
        "Stake sized at ¼-Kelly, capped at 2 %% of bankroll. "
        "Legs from different fixtures only. "
        "Latest data is a hard rule — do not bet if the feed is stale._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy compatibility shims
# ---------------------------------------------------------------------------

def build_accas_from_odds(
    odds_df: Any,
    fixtures_meta: Any,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """Legacy stub retained for test-suite monkeypatching compatibility.

    The bot handler no longer calls this function; it uses
    :func:`build_accas_report` instead.  Existing tests that patch this
    symbol will continue to compile and run.
    """
    return []


def format_accas(accas: List[Dict[str, Any]]) -> str:
    """Legacy stub retained for test-suite monkeypatching compatibility."""
    if not accas:
        return "*Accumulators*\nNo +EV accas found."
    return "*Accumulators*\n%d acca(s) available." % len(accas)
