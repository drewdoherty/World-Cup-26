"""Generate ``site/bet_recs.json`` — the Action Desk feed.

Builds a deterministic, multi-section bet recommendation feed from cached data.
No model fit, no live Odds API pull. Free Polymarket reads only (cached via
``site/advancement_data.json`` which the scheduled build already refreshes).

Sections emitted
----------------
* match_singles     — 1X2 singles where blended model > de-vigged consensus.
                      Top-3 per fixture by net EV. Moneyline-gate enforced on
                      any non-1X2 market (not currently active — no live book
                      prices for BTTS/totals in cache).
* event_props       — calibrated corners/cards/scorers; empty when real price
                      snapshots are older than PRICE_STALE_SECS.
* advancement_futures — Monte Carlo sim vs Polymarket; conditioned on results
                        to date.  PM fees applied. Quarter-Kelly on PM pool.
* guaranteed_arbs   — settlement-safe cross-venue arbs (fee/FX-adjusted).
* withheld          — rows that fail any gate (drift, stale, edge, caps).

Risk governance
---------------
Uses ``wca.card.resolve_pool_bankroll`` against ``data/wca.db`` (falls back to
rung-0 defaults when ledger is absent so the feed can be regenerated from CI
without the runtime DB). Flat quarter-Kelly at every rung; the rung scales the
bankroll, not the fraction.

GBP and USD are kept strictly separate. FX is disclosed when combined in the
dashboard but never silently mixed.

Usage::

    PYTHONPATH=src python scripts/wca_betrecs.py [--db PATH] [--out PATH]
        [--min-edge FLOAT] [--stale-model-hours INT] [--pm-bankroll FLOAT]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wca.advancement import (  # noqa: E402  — resolved-market bounds (single source)
    PM_RESOLVED_HI,
    PM_RESOLVED_LO,
)
from wca.markets import bankroll as pm_rule  # noqa: E402  (needs src on path)
from wca.selection import (  # noqa: E402  — canonical desk selection rule
    MARKET_MATCH,
    bucket_rank,
    hours_out as _sel_hours_out,
    hours_out_term,
    longshot_no_cash,
)

# ---------------------------------------------------------------------------
# Constants matching card.py governance
# ---------------------------------------------------------------------------

FLAT_KELLY_FRACTION: float = 0.25
DEFAULT_BANKROLL_GBP: float = 2000.0    # rung-0 sportsbook pool
# Polymarket pool: the GLOBAL RULE (wca.markets.bankroll) — ¼-Kelly of
# £3,000 ± realised P&L at $1.33/£. This is the BASE (P&L-unknown) figure;
# main() adjusts it from the live ledger when one is present. The old
# hardcoded $1,310 silently overrode the rule.
DEFAULT_PM_BANKROLL_USD: float = pm_rule.pm_bankroll_usd()
PER_BET_CAP: float = 0.05               # 5% hard cap per bet (sportsbook, card governance)
PM_PER_BET_CAP: float = pm_rule.PM_MAX_STAKE_FRAC  # 4% per bet (PM global rule)
DAILY_EXPOSURE_CAP: float = 0.25        # 25% total daily cap
SELECTION_MIN_PROB: float = 0.20        # hard floor on model prob
LONGSHOT_PROB: float = 0.25             # minnow filter threshold
MIN_EDGE: float = 0.02                  # minimum edge gate (2pp)
MODEL_STALE_HOURS: int = 24             # model older than this → withheld
PRICE_STALE_SECS: int = 7200           # 2h — price age beyond which rows go withheld
ADV_STALE_SECS: int = 6 * 3600         # 6h — advancement feed age beyond which futures are withheld
FX_FALLBACK_GBP_USD: float = 1.27      # fallback when no FX available

# PM taker fee: fee = 0.03 × p × (1 - p)
PM_FEE_RATE: float = 0.03

OUTCOMES = ("home", "draw", "away")


# ---------------------------------------------------------------------------
# Withheld-row reason taxonomy (telemetry — see docs/HANDOFF_2026-07-03.md /
# the 2026-07-08 gate-fill-telemetry review).
#
# ``reason_code`` is a NEW, machine-greppable field added alongside the
# existing free-text ``withheld_reason`` (never renamed/removed — site/arb.js
# only ever reads ``withheld_reason`` as opaque display text, so adding a
# field is behaviour-safe; see PR description). Every candidate a gate drops —
# not just the historically-instrumented ones — must append a withheld row
# carrying one of these codes so the 2pp edge-floor's true rejection cost
# becomes measurable instead of a bare ``continue``.
#
# Codes are intentionally granular (one per gate) rather than reusing a single
# generic "filtered" bucket, so ``wca_telemetry_report.py`` can show a
# breakdown by gate.
# ---------------------------------------------------------------------------
REASON_MISSING_MODEL_OR_MARKET = "missing_model_or_market"
REASON_KICKOFF_PAST = "kickoff_past"
REASON_MISSING_PRICE = "missing_price"
REASON_BAD_DEVIG_PRICE = "bad_devig_price"
REASON_BELOW_MIN_PROB = "below_min_prob"
REASON_LONGSHOT_FILTER = "longshot_filter"
REASON_EDGE_BELOW_FLOOR = "edge_below_floor"
REASON_STALE_MODEL = "stale_model"
REASON_ZERO_STAKE = "zero_stake"
REASON_TOP3_CAP = "top3_per_fixture_cap"
REASON_NO_LIVE_PRICE = "no_live_price"
REASON_UNSUPPORTED = "unsupported"
REASON_MISSING_PM_PRICE = "missing_pm_price"
REASON_MISSING_PM_MODEL_PROB = "missing_model_prob"
REASON_LONGSHOT_NO_CASH = "longshot_no_cash"
REASON_STALE_ADVANCEMENT = "stale_advancement"
# Added alongside the advancement KO-correctness gates (#177, landed on
# origin/main after this branch forked): both already emitted a withheld row
# (not a silent ``continue``), but lacked a reason_code — extending the
# taxonomy here keeps the breakdown complete rather than leaving these two
# gates as "(no reason_code)" rows in wca_telemetry_report.py.
REASON_RESOLVED_MARKET = "resolved_market"
REASON_STATE_STALE = "state_stale"


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


def _age_secs(ts_str: Optional[str]) -> Optional[int]:
    """Return seconds since ISO/UTC timestamp, or None if unparseable."""
    if not ts_str:
        return None
    # Normalise: strip Z, UTC, and +HH:MM / -HH:MM tz suffixes before parsing.
    s = str(ts_str).strip()
    s = s.rstrip("Z").replace(" UTC", "")
    # Strip +00:00 / -05:30 style suffix
    if len(s) > 6 and s[-6] in ("+", "-") and s[-3] == ":":
        s = s[:-6]
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            gen = dt.datetime.strptime(s, fmt)
            return max(0, int((_utcnow() - gen).total_seconds()))
        except ValueError:
            continue
    return None


def _hours_out(kickoff: Optional[str]) -> float:
    """Continuous hours until ``kickoff`` (0.0 when unknown/past).

    Adapter over :func:`wca.selection.hours_out` (the canonical secondary
    selection key) for betrecs' per-rec ``kickoff`` strings — further-out
    fixtures sort first (thin/soft early markets are more likely mispriced).
    """
    if not kickoff:
        return 0.0
    return _sel_hours_out({"match_desc": "_"}, {"_": kickoff})


def _singles_sort_key(rec: Dict[str, Any]) -> Tuple[int, float, float]:
    """Canonical desk ordering for 1X2 single recs (wca.selection).

    ``(bucket_rank(model_prob), hours_term, -ev_net)`` — model-prob bucket
    first (moneyline > mid > longshot, regardless of EV), then the
    category-conditional hours term, EV descending as the tie-break.

    These are 90-min MATCH markets (1X2 singles), so the hours term is NEUTRAL
    (:func:`wca.selection.hours_out_term` with ``MARKET_MATCH`` -> 0.0): the
    2026-07-09 backtest found no early premium after fees for match markets, so
    EV breaks ties within the bucket. Further-out-first is kept only for
    multi-week futures/advancement (see ``build_advancement_futures``, which
    uses its own stage-depth secondary key). The conditional lives in
    ``wca.selection`` — do not re-hardcode ``-hours`` here.
    """
    return (
        bucket_rank(rec.get("model_prob")),
        hours_out_term(_hours_out(rec.get("kickoff")), MARKET_MATCH),
        -float(rec.get("ev_net") or 0.0),
    )


def _pm_fee(p: float) -> float:
    return PM_FEE_RATE * p * (1.0 - p)


def _kelly_stake(
    p: float,
    price: float,
    bankroll: float,
    fraction: float = FLAT_KELLY_FRACTION,
    cap: float = PER_BET_CAP,
) -> float:
    """Fractional-Kelly stake, hard-capped at ``cap × bankroll``."""
    if price <= 1.0 or p <= 0.0 or bankroll <= 0.0:
        return 0.0
    b = price - 1.0
    f_full = (p * price - 1.0) / b
    if f_full <= 0.0:
        return 0.0
    f = min(f_full * fraction, cap)
    return round(f * bankroll, 2)


def _net_ev(p: float, price: float) -> float:
    """EV per unit stake: p × price − 1."""
    return round(p * price - 1.0, 6)


def _devig_price(devig_prob: float) -> Optional[float]:
    """Implied price from de-vigged probability. Returns None if ≤ 0."""
    if devig_prob is None or devig_prob <= 0.0:
        return None
    return round(1.0 / devig_prob, 4)


# ---------------------------------------------------------------------------
# Bankroll resolution
# ---------------------------------------------------------------------------

def _resolve_sportsbook_pool(db_path: str) -> Dict[str, Any]:
    """Try to read CLV ladder from ledger; return rung-0 defaults if absent."""
    try:
        from wca.card import resolve_pool_bankroll
        pb = resolve_pool_bankroll(db_path)
        return {
            "bankroll": pb.bankroll,
            "rung": pb.rung,
            "kelly_fraction": pb.kelly_fraction,
            "per_bet_cap": PER_BET_CAP,
            "max_stake": round(pb.bankroll * PER_BET_CAP, 2),
            "n_settled": pb.n_settled,
            "clv_to_date": pb.clv_to_date,
            "reason": pb.reason,
            "currency": "GBP",
            "source": "ledger",
        }
    except Exception:
        return {
            "bankroll": DEFAULT_BANKROLL_GBP,
            "rung": 0,
            "kelly_fraction": FLAT_KELLY_FRACTION,
            "per_bet_cap": PER_BET_CAP,
            "max_stake": round(DEFAULT_BANKROLL_GBP * PER_BET_CAP, 2),
            "n_settled": 0,
            "clv_to_date": None,
            "reason": "rung 0 default (ledger unavailable)",
            "currency": "GBP",
            "source": "default",
        }


def _pm_pool(bankroll_usd: float, source: str = "base") -> Dict[str, Any]:
    return {
        "bankroll": round(float(bankroll_usd), 2),
        "kelly_fraction": pm_rule.PM_KELLY_FRACTION,
        "per_bet_cap": PM_PER_BET_CAP,
        "max_stake": round(float(bankroll_usd) * PM_PER_BET_CAP, 2),
        "currency": "USD",
        "source": source,
    }


def _pm_realised_pnl_usd(db_path: str) -> Optional[float]:
    """Realised Polymarket P&L (USD) from the live ledger; None if unreadable.

    Opened read-only so a regen never mutates the runtime DB. ``settled_pl``
    already reflects free-bet/lay/cash-out accounting (ledger store rules).
    """
    import sqlite3

    if not Path(db_path).exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            row = con.execute(
                "SELECT COALESCE(SUM(settled_pl), 0.0) FROM bets "
                "WHERE platform = 'polymarket' AND settled_pl IS NOT NULL"
            ).fetchone()
        finally:
            con.close()
        return float(row[0]) if row is not None else None
    except Exception:
        return None


def _ledger_open_count(db_path: str) -> Optional[int]:
    """Count of ``status='open'`` rows in the live ledger; None if unreadable.

    Opened read-only so a regen never mutates the runtime DB.
    """
    import sqlite3

    if not Path(db_path).exists():
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM bets WHERE status='open'").fetchone()[0]
        finally:
            con.close()
        return int(n)
    except Exception:
        return None


def _open_exposure(db_path: str, exposure_feed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Open-exposure block derived from the LIVE ledger (F6).

    ``n_open`` is counted straight from ``data/wca.db`` (``status='open'``) so it
    can never drift from a stale shipped feed.  The EV / best / worst / p_profit
    figures come from :func:`wca.exposure_dashboard.compute_dashboard_metrics`
    (the honest, currency-coherent, ledger-driven engine).  When the runtime DB
    is absent (CI regen from a checkout), we fall back to the exposure feed and
    flag the source so the staleness is visible rather than silent.
    """
    n_live = _ledger_open_count(db_path)
    feed = exposure_feed or {}
    feed_metrics = feed.get("metrics") or {}

    if n_live is not None:
        metrics: Dict[str, Any] = {}
        try:
            from wca.exposure_dashboard import compute_dashboard_metrics

            res = compute_dashboard_metrics(db_path)
            metrics = res.get("metrics") or {}
            # Prefer the engine's own open count (same DB); fall back to our count.
            n_live = int(res.get("n_open_bets", n_live) or n_live)
        except Exception:
            metrics = {}
        return {
            "ev": metrics.get("ev"),
            "best_case": metrics.get("best_case"),
            "worst_case": metrics.get("worst_case"),
            "best_case_usd": metrics.get("best_case_usd"),
            "worst_case_usd": metrics.get("worst_case_usd"),
            "p_profit": metrics.get("p_profit"),
            "n_open": n_live,
            "source": "ledger",
        }

    # No runtime DB — degrade to the feed, marked stale-sourced.
    n_feed = int(
        feed_metrics.get("n_open_bets") or feed.get("n_open_bets") or 0
    )
    return {
        "ev": feed_metrics.get("ev"),
        "best_case": feed_metrics.get("best_case"),
        "worst_case": feed_metrics.get("worst_case"),
        "best_case_usd": feed_metrics.get("best_case_usd"),
        "worst_case_usd": feed_metrics.get("worst_case_usd"),
        "p_profit": feed_metrics.get("p_profit"),
        "n_open": n_feed,
        "source": "feed (ledger unavailable)",
    }


# ---------------------------------------------------------------------------
# Feed loaders
# ---------------------------------------------------------------------------

def _load_json(path: str, default: Any = None) -> Tuple[Any, Optional[int]]:
    """Load JSON file and return (data, age_secs). Returns default on error."""
    try:
        p = Path(path)
        data = json.loads(p.read_text())
        ts = (
            (data.get("meta") or {}).get("generated")
            or (data.get("meta") or {}).get("generated_at")
        )
        age = _age_secs(ts)
        return data, age
    except Exception:
        return default, None


def _venue_balances_from_data(data_json: Dict[str, Any]) -> Dict[str, float]:
    """Extract per-venue open stakes from data.json."""
    venues = data_json.get("venues") or {}
    out: Dict[str, float] = {}
    for k, v in venues.items():
        if isinstance(v, dict) and "open_stake" in v:
            out[k] = float(v["open_stake"])
    return out


def _fx_from_arb_data(arb_json: Dict[str, Any]) -> Tuple[float, str]:
    """Extract GBP/USD rate; fall back to constant."""
    meta = arb_json.get("meta") or {}
    rate = meta.get("fx_usd_per_gbp") or meta.get("fx_gbp_usd") or FX_FALLBACK_GBP_USD
    src = meta.get("fx_source") or "fallback"
    return float(rate), str(src)


# ---------------------------------------------------------------------------
# Knockout bracket enrichment (scores_markets.json)
#
# Advancement (Polymarket moneyline) recs settle if the team PROGRESSES — that
# includes extra-time and penalties. A 90-minute 1X2 rec settles only on the
# score after 90'+stoppage, so a knockout tie that goes to ET/pens is a DRAW
# for the 1X2 market. Now that KOs have ET+pens these are genuinely different
# markets; we surface the team's next KO tie (opponent + 90' 1X2 split) so an
# advancement rec is never confused with — or placed as — a 90-min result bet.
# ---------------------------------------------------------------------------

# KO round order, earliest → latest. Used to walk from the current round
# forward when finding a team's next unplayed tie.
_KO_ROUND_KEYS = ("r32_games", "r16_games", "qf_games", "sf_games", "final_games")


def _next_ko_tie(team: str, scores_markets: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find ``team``'s next *unplayed* knockout tie in the projected bracket.

    Walks r32 → r16 → qf → sf → final and returns the first tie (earliest
    round) that contains ``team`` (as home or away) and is unplayed (``ft`` is
    None). Returns ``None`` if the team has no upcoming KO tie in the bracket
    or ``scores_markets`` is unavailable.
    """
    if not scores_markets or not team:
        return None
    for round_key in _KO_ROUND_KEYS:
        for tie in (scores_markets.get(round_key) or []):
            if not isinstance(tie, dict):
                continue
            if tie.get("ft") is not None:
                continue  # already played — not their next tie
            if tie.get("home") == team or tie.get("away") == team:
                return tie
    return None


def _match_1x2_for_team(team: str, tie: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Orient a tie's 90-minute 1X2 split from ``team``'s perspective.

    ``x1x2`` is [P(home win 90'), P(draw), P(away win 90')]. Returns
    {"team_win", "draw", "opp_win"} rounded to 3dp, or None if unavailable.
    """
    x1x2 = tie.get("x1x2")
    if not isinstance(x1x2, (list, tuple)) or len(x1x2) < 3:
        return None
    home_p, draw_p, away_p = float(x1x2[0]), float(x1x2[1]), float(x1x2[2])
    if tie.get("home") == team:
        team_p, opp_p = home_p, away_p
    elif tie.get("away") == team:
        team_p, opp_p = away_p, home_p
    else:
        return None
    return {
        "team_win": round(team_p, 3),
        "draw": round(draw_p, 3),
        "opp_win": round(opp_p, 3),
    }


def _enrich_advancement_rec(rec: Dict[str, Any], scores_markets: Dict[str, Any]) -> None:
    """Add opponent / next-KO-tie context to an advancement rec, in place.

    Never raises: on any missing data the context fields are set to null so
    the front-end shows an em-dash rather than a wrong or stale opponent.
    """
    rec["market_kind"] = "advancement"
    rec["market_label"] = "Advance · incl. ET+pens"
    # Defaults (older data / no bracket → em-dash on the page).
    rec.setdefault("opponent", None)
    rec.setdefault("match_round", None)
    rec.setdefault("match_1x2", None)

    tie = _next_ko_tie(rec.get("team") or "", scores_markets)
    if not tie:
        return

    team = rec.get("team") or ""
    opponent = tie.get("away") if tie.get("home") == team else tie.get("home")
    rec["opponent"] = opponent
    rec["match_round"] = tie.get("round")
    rec["match_1x2"] = _match_1x2_for_team(team, tie)


# ---------------------------------------------------------------------------
# Match singles builder
# ---------------------------------------------------------------------------

def _label_action(
    fixture: str,
    selection: str,
    open_fixtures: set,
    blind_spots: List[str],
) -> str:
    """Return ADD / DIVERSIFY / HEDGE."""
    team_lower = selection.lower()
    if any(bs.lower() in team_lower or team_lower in bs.lower() for bs in blind_spots):
        return "HEDGE"
    if fixture in open_fixtures:
        return "DIVERSIFY"
    return "ADD"


def _promo_status(fixture: str, venue: str, promos_data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (promo_name, promo_status_code).

    promo_status_code: "applied" | "PROMO CHECK REQUIRED" | "none"
    """
    sites = promos_data.get("sites") or []
    for site in sites:
        name = (site.get("name") or "").lower()
        if venue.lower() not in name and name not in venue.lower():
            continue
        boosts = site.get("boosts") or []
        for b in boosts:
            title = b.get("title") or ""
            if "match odds" in title.lower() or "power price" in title.lower():
                return title, "PROMO CHECK REQUIRED"
    return None, "none"


def build_match_singles(
    predictions: List[Dict[str, Any]],
    sb_pool: Dict[str, Any],
    open_fixtures: set,
    blind_spots: List[str],
    promos_data: Dict[str, Any],
    model_age_secs: Optional[int],
    min_edge: float = MIN_EDGE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build match singles from blended model vs de-vigged consensus.

    Returns (actionable, withheld).
    """
    bankroll = sb_pool["bankroll"]
    kelly_frac = sb_pool["kelly_fraction"]
    max_stake = sb_pool["max_stake"]

    model_stale = model_age_secs is not None and model_age_secs > MODEL_STALE_HOURS * 3600

    actionable: List[Dict[str, Any]] = []
    withheld: List[Dict[str, Any]] = []

    for fix in (predictions or []):
        fixture = fix.get("fixture") or ""
        kickoff = fix.get("kickoff") or ""
        group = fix.get("group") or ""
        generated = fix.get("generated") or ""
        model = fix.get("model") or {}
        market = fix.get("market") or {}

        teams = fixture.split(" vs ")
        team_map = {
            "home": teams[0] if teams else "Home",
            "draw": "Draw",
            "away": teams[1] if len(teams) > 1 else "Away",
        }

        if not model or not market:
            # Whole fixture dropped — no model or no market side to compare.
            # One withheld row per outcome so the candidate count stays
            # honest (previously a bare ``continue``, zero telemetry).
            for outcome in OUTCOMES:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team_map[outcome],
                    "model_prob": None, "price": None, "edge": None, "ev_net": None,
                    "stake": 0.0, "currency": "GBP",
                    "withheld_reason": "missing model or market data for fixture",
                    "reason_code": REASON_MISSING_MODEL_OR_MARKET,
                    "stale": None, "stale_reason": None,
                })
            continue

        # Kickoff guard: skip if kickoff is in the past (> 3h ago)
        if kickoff:
            kick_age = _age_secs(kickoff)
            if kick_age is not None and kick_age > 3 * 3600:
                for outcome in OUTCOMES:
                    withheld.append({
                        "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                        "fixture": fixture, "kickoff": kickoff, "group": group,
                        "market": "1X2", "selection": outcome, "team": team_map[outcome],
                        "model_prob": None, "price": None, "edge": None, "ev_net": None,
                        "stake": 0.0, "currency": "GBP",
                        "withheld_reason": "kickoff %ds in the past (> 3h stale fixture)" % kick_age,
                        "reason_code": REASON_KICKOFF_PAST,
                        "stale": None, "stale_reason": None,
                    })
                continue

        recs_this_fixture: List[Dict[str, Any]] = []

        for outcome in OUTCOMES:
            p_model = float(model.get(outcome) or 0.0)
            p_devig = float(market.get(outcome) or 0.0)

            if p_devig <= 0.0 or p_model <= 0.0:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team_map[outcome],
                    "model_prob": round(p_model, 4) if p_model else None,
                    "price": None, "edge": None, "ev_net": None, "stake": 0.0,
                    "currency": "GBP",
                    "withheld_reason": "missing price: model=%.4f devig=%.4f" % (p_model, p_devig),
                    "reason_code": REASON_MISSING_PRICE,
                    "stale": None, "stale_reason": None,
                })
                continue

            price = _devig_price(p_devig)
            if price is None or price <= 1.0:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team_map[outcome],
                    "model_prob": round(p_model, 4), "price": price, "edge": None,
                    "ev_net": None, "stake": 0.0, "currency": "GBP",
                    "withheld_reason": "bad de-vig price: %s" % (price,),
                    "reason_code": REASON_BAD_DEVIG_PRICE,
                    "stale": None, "stale_reason": None,
                })
                continue

            edge = round(p_model - p_devig, 6)
            ev = _net_ev(p_model, price)

            team = team_map[outcome]

            stale = model_stale
            stale_reason = "model feed stale (>%dh)" % MODEL_STALE_HOURS if stale else None

            # Selection rules
            if p_model < SELECTION_MIN_PROB:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team,
                    "model_prob": round(p_model, 4), "price": price,
                    "edge": round(edge, 4), "ev_net": round(ev, 4), "stake": 0.0,
                    "currency": "GBP", "withheld_reason": "model_prob %.0f%% < floor %.0f%%" % (p_model * 100, SELECTION_MIN_PROB * 100),
                    "reason_code": REASON_BELOW_MIN_PROB,
                    "stale": stale, "stale_reason": stale_reason,
                })
                continue

            if p_model < LONGSHOT_PROB and edge > 0:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team,
                    "model_prob": round(p_model, 4), "price": price,
                    "edge": round(edge, 4), "ev_net": round(ev, 4), "stake": 0.0,
                    "currency": "GBP", "withheld_reason": "longshot filter: prob %.0f%% < %.0f%% (minnow risk)" % (p_model * 100, LONGSHOT_PROB * 100),
                    "reason_code": REASON_LONGSHOT_FILTER,
                    "stale": stale, "stale_reason": stale_reason,
                })
                continue

            if edge < min_edge:
                # THE 2pp edge-floor gate the telemetry review named directly
                # (previously a bare ``continue`` — no withheld row, no way to
                # measure how many candidates this floor actually rejects).
                # Reason is machine-greppable: "edge_below_floor:<edge><min_edge>".
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team,
                    "model_prob": round(p_model, 4), "price": price,
                    "edge": round(edge, 4), "ev_net": round(ev, 4), "stake": 0.0,
                    "currency": "GBP",
                    "withheld_reason": "edge_below_floor:%.4f<%.4f" % (edge, min_edge),
                    "reason_code": REASON_EDGE_BELOW_FLOOR,
                    "stale": stale, "stale_reason": stale_reason,
                })
                continue

            if stale:
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team,
                    "model_prob": round(p_model, 4), "price": price,
                    "edge": round(edge, 4), "ev_net": round(ev, 4), "stake": 0.0,
                    "currency": "GBP", "withheld_reason": stale_reason or "stale",
                    "reason_code": REASON_STALE_MODEL,
                    "stale": True, "stale_reason": stale_reason,
                })
                continue

            stake = _kelly_stake(p_model, price, bankroll, kelly_frac, PER_BET_CAP)
            stake = min(stake, max_stake)

            if stake <= 0:
                # Kelly stake rounded to 0 after the per-bet cap (e.g. a tiny
                # positive edge against a small bankroll) — a real candidate
                # that survived every gate above but sizes to nothing.
                withheld.append({
                    "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                    "fixture": fixture, "kickoff": kickoff, "group": group,
                    "market": "1X2", "selection": outcome, "team": team,
                    "model_prob": round(p_model, 4), "price": price,
                    "edge": round(edge, 4), "ev_net": round(ev, 4), "stake": 0.0,
                    "currency": "GBP",
                    "withheld_reason": "stake rounded to zero (kelly=%.2f, bankroll=%.2f)" % (stake, bankroll),
                    "reason_code": REASON_ZERO_STAKE,
                    "stale": stale, "stale_reason": stale_reason,
                })
                continue

            action = _label_action(fixture, team, open_fixtures, blind_spots)
            promo_name, promo_st = _promo_status(fixture, "smarkets", promos_data)

            rec = {
                "id": "%s_%s_1x2" % (fixture.lower().replace(" vs ", "_vs_").replace(" ", "_"), outcome),
                "fixture": fixture,
                "kickoff": kickoff,
                "group": group,
                "market": "1X2",
                # 1X2 settles on the 90'+stoppage result only. In a knockout,
                # a tie that goes to ET/pens is a DRAW for this market — this
                # is a genuinely different bet from a PM advancement moneyline.
                "market_kind": "result_90",
                "market_label": "90-min 1X2 (+ stoppage)",
                "selection": outcome,
                "team": team,
                "venue": "smarkets",
                "currency": "GBP",
                "model_prob": round(p_model, 4),
                "price": price,
                "price_source": "devig_consensus",
                "edge": round(edge, 4),
                "ev_net": round(ev, 4),
                "stake": stake,
                "action_label": action,
                "current_exposure": {"fixture_open": fixture in open_fixtures},
                "proposed_risk": {"stake": stake, "max_loss": stake},
                "promo": {"name": promo_name} if promo_name else None,
                "promo_status": promo_st if promo_st != "none" else None,
                "ages": {
                    "model_secs": model_age_secs,
                    "price_secs": model_age_secs,
                    "exposure_secs": None,
                },
                "stale": stale,
                "stale_reason": stale_reason,
                "tags": ["model", "1X2", "devig_price"],
            }
            recs_this_fixture.append(rec)

        # Top-3 per fixture by the canonical desk key (wca.selection):
        # model-prob bucket, then further-out fixture, then EV as tie-break.
        recs_this_fixture.sort(key=_singles_sort_key)
        for rec in recs_this_fixture[:3]:
            actionable.append(rec)
        for rec in recs_this_fixture[3:]:
            rec["withheld_reason"] = "top-3 per fixture cap"
            rec["reason_code"] = REASON_TOP3_CAP
            withheld.append(rec)

    # Cross-fixture ranking: same canonical key (bucket, -hours_out, -ev_net).
    actionable.sort(key=_singles_sort_key)
    return actionable, withheld


# ---------------------------------------------------------------------------
# Event/player props builder
# ---------------------------------------------------------------------------

def build_event_props(
    prop_cal: Dict[str, Any],
    model_predictions: List[Dict[str, Any]],
    sb_pool: Dict[str, Any],
    price_age_secs: Optional[int],
    model_age_secs: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build calibrated prop recommendations.

    Currently only emits records when real market price snapshots are fresh
    enough (< PRICE_STALE_SECS). Player scorer models are omitted — no real
    player xG-share inputs are wired yet.

    Returns (actionable, withheld).
    """
    withheld: List[Dict[str, Any]] = []

    # No live book prices in cache: emit honest withheld rows for all props.
    fixtures_cal = prop_cal.get("fixtures") or []
    for fix_cal in fixtures_cal:
        fixture = fix_cal.get("fixture") or ""
        for mkt, label in [("corners", "corners over 8.5"), ("cards", "cards over 2.5")]:
            mkt_data = fix_cal.get(mkt) or {}
            if not mkt_data:
                continue
            withheld.append({
                "id": "%s_%s" % (fixture.lower().replace(" ", "_"), mkt),
                "fixture": fixture,
                "market": mkt,
                "selection": label,
                "withheld_reason": "no live book price snapshot (props require sportsbook feed)",
                "reason_code": REASON_NO_LIVE_PRICE,
                "stale": True,
                "stale_reason": "price snapshot absent or older than %dh" % (PRICE_STALE_SECS // 3600),
                "tags": ["model", "prop", "no_price"],
            })

    # Scorer props explicitly noted as not actionable
    withheld.append({
        "id": "scorer_props_all",
        "fixture": "ALL",
        "market": "anytime_scorer",
        "selection": "—",
        "withheld_reason": "player xG-share + penalty-taker injection not yet wired (no real inputs)",
        "reason_code": REASON_UNSUPPORTED,
        "stale": False,
        "stale_reason": None,
        "tags": ["model", "scorer", "unsupported"],
    })

    return [], withheld


# ---------------------------------------------------------------------------
# Advancement/futures builder
# ---------------------------------------------------------------------------

def build_advancement_futures(
    adv_data: Dict[str, Any],
    pm_pool: Dict[str, Any],
    adv_age_secs: Optional[int],
    scores_markets: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build advancement/futures recs from cached advancement_data.json.

    The advancement module already computes fee-adjusted edges and quarter-Kelly
    stakes for the PM pool. We re-apply governance caps here for consistency.

    Returns (actionable, withheld).
    """
    bankroll = pm_pool["bankroll"]
    kelly_frac = pm_pool["kelly_fraction"]
    max_stake = pm_pool["max_stake"]

    # Advancement futures move fast in the knockouts — teams get ELIMINATED,
    # and a stale feed keeps recommending knocked-out sides at phantom edges
    # (observed 2026-07-02: Bosnia still shown post-elimination on a 15h-old
    # feed). The dedicated 6h gate is deliberately far tighter than the 24h
    # model gate; PM advancement hygiene rule: re-run before acting.
    adv_stale = adv_age_secs is not None and adv_age_secs > ADV_STALE_SECS
    model_stale = adv_age_secs is not None and adv_age_secs > MODEL_STALE_HOURS * 3600

    actionable: List[Dict[str, Any]] = []
    withheld: List[Dict[str, Any]] = []

    scores_markets = scores_markets or {}

    meta = adv_data.get("meta") or {}
    stages_available = set(meta.get("stages") or [])

    # PM-BLIND guard (2026-07-03): the advancement builder stamps how many
    # LIVE Polymarket markets it matched. Zero means the feed was built from
    # cached/stale prices (observed: the mini's Polymarket network block left
    # a fresh-stamped feed still recommending an ELIMINATED team at an old
    # $65.50-cap stake). Fresh timestamp + dead inputs must never look
    # actionable — withhold everything.
    pm_blind = not meta.get("n_pm_markets")

    for team_entry in (adv_data.get("teams") or []):
        team = team_entry.get("team") or ""
        group = team_entry.get("group") or ""
        model_probs = team_entry.get("model") or {}
        pm_data = team_entry.get("pm") or {}
        delta = team_entry.get("delta") or {}
        # State-freshness gate (2026-07-08): the feed builder stamps a reason
        # on any team whose knockout tie has kicked off but is NOT pinned in
        # the sim's conditioning set — its stage probabilities are phantom
        # (USA showed P(QF)=0.317 after its Jul-6 elimination; Egypt
        # P(R16)=0.4708 after winning its Jul-3 shootout). Withhold, never size.
        state_reason = team_entry.get("state_stale_reason")

        for stage, pm_info in (pm_data.items() if isinstance(pm_data, dict) else []):
            pm_price = pm_info.get("pm")
            edge_adj = pm_info.get("edge_adj")

            if pm_price is None or edge_adj is None:
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(float(model_probs.get(stage) or 0.0), 4),
                    "pm_price": None, "ev_net": None, "stake": 0.0,
                    "withheld_reason": "missing PM price or edge_adj for stage",
                    "reason_code": REASON_MISSING_PM_PRICE,
                })
                continue

            p_model = float(model_probs.get(stage) or 0.0)
            if p_model <= 0.0:
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": 0.0, "pm_price": round(pm_price, 4),
                    "ev_net": None, "stake": 0.0,
                    "withheld_reason": "missing/zero model probability for stage",
                    "reason_code": REASON_MISSING_PM_MODEL_PROB,
                })
                continue

            # Fee-adjusted EV: back YES at pm_price, fee = PM_FEE_RATE×p×(1-p)
            fee = _pm_fee(pm_price)
            net_cost = pm_price + fee
            ev = p_model - net_cost

            # Resolved-market guard (2026-07-08): a YES quote pinned at ≥0.98
            # or ≤0.02 means the market has effectively settled — any "edge"
            # against it is a sim-vs-reality disagreement, not a trade.
            # (compare_to_polymarket now drops these at build time; this guard
            # covers feeds built before the fix.)
            if pm_price >= PM_RESOLVED_HI or pm_price <= PM_RESOLVED_LO:
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(p_model, 4), "pm_price": round(pm_price, 4),
                    "ev_net": round(ev, 4), "stake": 0.0,
                    "withheld_reason": "resolved market (pm=%.2f) — outcome "
                                       "effectively decided; no tradable edge"
                                       % pm_price,
                    "reason_code": REASON_RESOLVED_MARKET,
                })
                continue

            if ev < MIN_EDGE:
                # THE 2pp edge-floor gate on the advancement side — same
                # telemetry gap as build_match_singles: previously a bare
                # ``continue``, no withheld row, cost unmeasurable.
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(p_model, 4), "pm_price": round(pm_price, 4),
                    "ev_net": round(ev, 4), "stake": 0.0,
                    "withheld_reason": "edge_below_floor:%.4f<%.4f" % (ev, MIN_EDGE),
                    "reason_code": REASON_EDGE_BELOW_FLOOR,
                })
                continue

            if state_reason:
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(p_model, 4), "pm_price": round(pm_price, 4),
                    "ev_net": round(ev, 4), "stake": 0.0,
                    "withheld_reason": state_reason,
                    "reason_code": REASON_STATE_STALE,
                })
                continue

            # Canonical cash floor (wca.selection.longshot_no_cash; user
            # 2026-07-07): a <25c model-prob advancement side is free-bet /
            # lottery only — never cash — so it is withheld from the actionable
            # feed even when +EV. (These sub-25c futures used to size a capped
            # cash stake here; the REPLACE ruling zeroes them.)
            if longshot_no_cash(p_model):
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(p_model, 4), "pm_price": round(pm_price, 4),
                    "ev_net": round(ev, 4), "stake": 0.0,
                    "withheld_reason": "model-prob longshot (%.0f%% < 25%%) — no cash "
                                       "(free-bet/lottery only)" % (p_model * 100),
                    "reason_code": REASON_LONGSHOT_NO_CASH,
                })
                continue

            price = 1.0 / net_cost if net_cost > 0 else 0.0
            stake = _kelly_stake(p_model, price, bankroll, kelly_frac, PER_BET_CAP)
            stake = min(stake, max_stake)

            # Same-team nested-path exposure cap (fix 2026-07-08). One team's
            # advancement rungs are NESTED (win ⊂ Final ⊂ SF ⊂ QF ⊂ R16 ⊂
            # R32) — one correlated path leg, not independent bets. The
            # sizing source (wca.advancement.apply_path_exposure_caps) emits
            # per-rung PATH-CAPPED stakes into the feed (``stake_usd``: the
            # team's same-side rung stakes sum to at most the tightest staked
            # rung's ¼-Kelly); respect it here as a HARD per-rung ceiling so
            # this builder's independent re-derivation can never re-stack the
            # path (observed: Morocco SF $112 + Morocco Final $33 on one
            # path). YES-side only — this loop only ever backs YES, and a
            # NO-side feed stake is a different exposure (never co-capped).
            # Older feeds without the field are unaffected. Reduces only.
            path_capped = False
            feed_rung_stake = pm_info.get("stake_usd")
            if (
                pm_info.get("side") == "YES"
                and isinstance(feed_rung_stake, (int, float))
                and float(feed_rung_stake) < stake
            ):
                stake = float(feed_rung_stake)
                path_capped = True

            if stake <= 0:
                # Kelly stake rounded to 0 after the per-bet cap — survived
                # every gate above but sizes to nothing.
                withheld.append({
                    "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                    "team": team, "group": group, "stage": stage,
                    "market": "advancement", "selection": "reach_%s" % stage,
                    "venue": "polymarket", "currency": "USD",
                    "model_prob": round(p_model, 4), "pm_price": round(pm_price, 4),
                    "ev_net": round(ev, 4), "stake": 0.0,
                    "withheld_reason": "stake rounded to zero (kelly=%.2f, bankroll=%.2f)" % (stake, bankroll),
                    "reason_code": REASON_ZERO_STAKE,
                })
                continue

            stale = model_stale or adv_stale or pm_blind
            if pm_blind:
                stale_reason = (
                    "advancement feed built with NO live PM markets "
                    "(n_pm_markets=0 — network-blocked or PM fetch failed); "
                    "prices/stakes are cached — re-run after the fix"
                )
            elif model_stale:
                stale_reason = "advancement model stale (>%dh)" % MODEL_STALE_HOURS
            elif adv_stale:
                stale_reason = (
                    "advancement feed stale (>%dh) — teams may be eliminated; "
                    "re-run before acting" % (ADV_STALE_SECS // 3600)
                )
            else:
                stale_reason = None

            rec = {
                "id": "%s_%s_pm" % (team.lower().replace(" ", "_"), stage.lower()),
                "team": team,
                "group": group,
                "stage": stage,
                "market": "advancement",
                "selection": "reach_%s" % stage,
                "venue": "polymarket",
                "currency": "USD",
                "model_prob": round(p_model, 4),
                "pm_price": round(pm_price, 4),
                "pm_fee": round(fee, 4),
                "price": round(price, 4),
                "edge_adj": round(edge_adj, 4),
                "ev_net": round(ev, 4),
                "stake": round(stake, 2),
                "action_label": "ADD",
                "ages": {
                    "model_secs": adv_age_secs,
                    "price_secs": adv_age_secs,
                },
                "stale": stale,
                "stale_reason": stale_reason,
                "tags": ["model", "advancement", "polymarket"],
            }
            if path_capped:
                # Stake was bound by the same-team nested-path cap emitted by
                # the sizing source (advancement_data pm[stage].stake_usd).
                rec["path_capped"] = True

            # Enrich with the team's next KO tie (opponent + 90' 1X2 split) and
            # market-kind labels so an advancement moneyline is never confused
            # with a 90-minute 1X2 result bet. Never crashes on missing data.
            _enrich_advancement_rec(rec, scores_markets)

            if stale:
                rec["withheld_reason"] = stale_reason
                rec["reason_code"] = REASON_STALE_ADVANCEMENT
                withheld.append(rec)
            else:
                actionable.append(rec)

    # Canonical desk ordering (wca.selection; user 2026-07-07):
    #   1. model-prob bucket (moneyline > mid > longshot), ALWAYS;
    #   2. further-out first — deeper knockout stage (higher = further out);
    #   3. EV descending breaks ties within a bucket + stage tier.
    # `_stage_further_out` is the inverse of the old nearest-first `_stage_order`:
    # group_winner is the nearest-term market, Win the furthest out.
    # This is a MULTI-WEEK FUTURES surface (wca.selection.MARKET_FUTURES): the
    # 2026-07-09 category-conditional refinement KEEPS further-out-first here
    # (the proven +6-7% early edge) via this stage-depth key — it never used
    # wca.selection.hours_out, so it is intentionally UNCHANGED (only match
    # markets had their hours-out term neutralised). Cf. `_singles_sort_key`.
    _stage_further_out = {
        "group_winner": 0, "R32": 1, "R16": 2, "QF": 3, "SF": 4,
        "Final": 5, "win": 6,
    }
    actionable.sort(key=lambda r: (
        bucket_rank(r["model_prob"]),
        -_stage_further_out.get(r["stage"], 0),
        -r["ev_net"],
    ))
    return actionable, withheld


# ---------------------------------------------------------------------------
# Guaranteed arbs builder (pass-through from arb_data.json)
# ---------------------------------------------------------------------------

def build_guaranteed_arbs(
    arb_data: Dict[str, Any],
    arb_age_secs: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pass through guaranteed arbs from arb_data.json, adding liquidity note."""
    arbs = arb_data.get("arbs") or []
    out: List[Dict[str, Any]] = []
    for a in arbs:
        rec = dict(a)
        rec["tags"] = rec.get("tags") or ["arb", "settlement_safe"]
        # Require quoted depth or label unverified
        if not any("depth" in str(l) for l in (rec.get("legs") or [])):
            rec["liquidity_note"] = "price-only, liquidity unverified"
        rec["ages"] = {"price_secs": arb_age_secs}
        out.append(rec)
    out.sort(key=lambda r: -(r.get("guaranteed_pct") or 0.0))
    return out, []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/wca.db")
    ap.add_argument("--out", default="site/bet_recs.json")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE)
    ap.add_argument("--stale-model-hours", type=int, default=MODEL_STALE_HOURS)
    ap.add_argument("--pm-bankroll", type=float, default=None,
                    help="override PM pool (USD); default = global rule "
                         "(£3,000·FX ± ledger realised P&L)")
    ap.add_argument("--predictions", default="data/model_predictions.json")
    ap.add_argument("--advancement", default="site/advancement_data.json")
    ap.add_argument("--exposure", default="site/exposure_dashboard.json")
    ap.add_argument("--data", default="site/data.json")
    ap.add_argument("--promos", default="site/promos_data.json")
    ap.add_argument("--arb-data", default="site/arb_data.json")
    ap.add_argument("--prop-cal", default="data/prop_calibration.json")
    ap.add_argument("--scores-markets", default="site/scores_markets.json")
    args = ap.parse_args()

    # Bankroll governance
    sb_pool = _resolve_sportsbook_pool(args.db)
    if args.pm_bankroll is not None:
        pm_pool_data = _pm_pool(args.pm_bankroll, source="cli-override")
    else:
        pm_pnl = _pm_realised_pnl_usd(args.db)
        if pm_pnl is None:
            pm_pool_data = _pm_pool(pm_rule.pm_bankroll_usd(), source="base (no ledger)")
        else:
            pm_pool_data = _pm_pool(
                pm_rule.pm_bankroll_usd(pm_pnl),
                source="ledger (realised $%+.2f)" % pm_pnl,
            )

    # Load feeds with ages
    predictions_raw, pred_age = _load_json(args.predictions, {"fixtures": []})
    predictions = (predictions_raw or {}).get("fixtures") or []
    adv_raw, adv_age = _load_json(args.advancement, {})
    exposure_raw, exp_age = _load_json(args.exposure, {})
    data_raw, _ = _load_json(args.data, {})
    promos_raw, promo_age = _load_json(args.promos, {})
    arb_raw, arb_age = _load_json(args.arb_data, {})
    prop_cal_raw, prop_age = _load_json(args.prop_cal, {})
    # Projected KO bracket — for advancement-rec opponent + 90' 1X2 context.
    # Guarded: missing/unreadable → enrichment fields simply stay null.
    scores_markets_raw, _ = _load_json(args.scores_markets, {})

    # FX
    fx_rate, fx_src = _fx_from_arb_data(arb_raw or {})

    # Open exposure context — n_open / EV are derived from the LIVE ledger
    # (F6), never from the possibly-stale shipped exposure feed.
    blind_spots_raw = (exposure_raw or {}).get("blind_spots") or []
    blind_spots = [str(bs) for bs in blind_spots_raw if isinstance(bs, str)]
    open_exposure = _open_exposure(args.db, exposure_raw)
    open_fixtures: set = set()  # TODO: parse from ledger if available

    # Build sections
    match_singles, withheld_ms = build_match_singles(
        predictions, sb_pool, open_fixtures, blind_spots, promos_raw or {},
        model_age_secs=pred_age, min_edge=args.min_edge,
    )
    event_props, withheld_ep = build_event_props(
        prop_cal_raw or {}, predictions, sb_pool,
        price_age_secs=prop_age, model_age_secs=pred_age,
    )
    adv_futures, withheld_af = build_advancement_futures(
        adv_raw or {}, pm_pool_data, adv_age_secs=adv_age,
        scores_markets=scores_markets_raw or {},
    )
    guar_arbs, withheld_ga = build_guaranteed_arbs(arb_raw or {}, arb_age_secs=arb_age)
    withheld = withheld_ms + withheld_ep + withheld_af + withheld_ga

    actionable_count = len(match_singles) + len(event_props) + len(adv_futures) + len(guar_arbs)

    payload = {
        "meta": {
            "generated": _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "monitoring_only": True,
            "sportsbook_pool": sb_pool,
            "pm_pool": pm_pool_data,
            "open_exposure": open_exposure,
            "fx": {
                "gbp_usd": fx_rate,
                "source": fx_src,
                "note": "currencies kept separate; FX disclosed for combined dashboard only",
            },
            "ages": {
                "model_secs": pred_age,
                "advancement_secs": adv_age,
                "price_secs": None,
                "promo_secs": promo_age,
                "exposure_secs": exp_age,
            },
            "coverage": {
                "match_singles": (
                    "1X2 from blended model (10%% Elo + 30%% DC + 60%% market) vs "
                    "de-vigged consensus. Price = market-implied (no best-price pull). "
                    "Top-3 per fixture by net EV. BTTS/totals: withheld — no live book price."
                ),
                "event_props": (
                    "Corners/cards: calibrated but withheld — no live sportsbook price snapshot. "
                    "Player scorer: withheld — player xG-share not yet wired."
                ),
                "advancement_futures": (
                    "Monte Carlo sim vs Polymarket advancement markets. "
                    "PM taker fee (3%% × p × (1-p)) applied. Quarter-Kelly on PM pool ($%.0f)." % pm_pool_data["bankroll"]
                ),
                "guaranteed_arbs": "Settlement-safe cross-venue arbs (fee/FX-adjusted). Currently empty.",
            },
            "actionable_count": actionable_count,
            "withheld_count": len(withheld),
        },
        "match_singles": match_singles,
        "event_props": event_props,
        "advancement_futures": adv_futures,
        "guaranteed_arbs": guar_arbs,
        "withheld": withheld,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))

    print(
        "wrote %s — match_singles=%d, adv_futures=%d, arbs=%d, withheld=%d (pred_age=%s, adv_age=%s)" % (
            out, len(match_singles), len(adv_futures), len(guar_arbs), len(withheld),
            ("%ds" % pred_age if pred_age is not None else "?"),
            ("%ds" % adv_age if adv_age is not None else "?"),
        )
    )
    if (pred_age or 0) > MODEL_STALE_HOURS * 3600:
        print("  WARNING: model predictions stale (%dh) — match singles may be withheld." % (pred_age // 3600))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
