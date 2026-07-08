#!/usr/bin/env python
"""Advancement/Knockout Polymarket Edge Desk — SHADOW-ONLY decision-support
feed (``site/advancement_edge_desk.json``).

Joins the five committed site feeds into one ranked desk of Polymarket
advancement/outright opportunities, each carrying explicit pass/fail gates and
a verdict that is NEVER a trade instruction:

* ``site/advancement_data.json``          — model chain + PM quotes + group tables
* ``site/bet_recs.json``                  — actionable advancement futures + withheld near-misses
* ``site/scores_markets.json``            — knockout bracket (real vs projected ties, 90-min splits)
* ``site/pm_ideas.json``                  — parked /pm trade ideas (context join)
* ``site/microstructure/orderflow.json``  — PM taker-flow aggregates (context ONLY)

Hard rules encoded here (do not regress):

* SHADOW-ONLY. The verdict enum is {SHADOW_ADD, WATCH, WITHHOLD, DO_NOT_TRADE}
  — every label is explicitly non-executing (no PLACE/FIRE/TRADE), this script
  never touches the ledger, Telegram, or any execution path, and the standing
  ``clv_history`` gate is BLOCKED on every row: durable PM/odds price history
  for advancement markets is stale/missing and no CLV stamping exists, so per
  the live-money gate ("a market without price capture + CLV stamping does not
  get real money") ANY real-money upgrade first needs price/CLV capture
  restored.
* Orderflow is context, never a signal override: hot taker flow may upgrade
  confidence on a row that already has a positive model edge, but a negative
  fee-adjusted edge can never exceed WATCH no matter how hot the flow looks.
* Likely-PnL rule: longshots (position pays out at model <25%) can never be
  SHADOW_ADD — capped at WATCH/WITHHOLD with reason ``longshot_no_cash``
  (longshot "edges" went 0-for-12 in backtests).
* Settlement bases must never be confusable: every row carries
  ``settlement_basis`` ("PM advancement includes ET+pens; 1X2 is 90 minutes
  only") and every embedded 90-minute number (knockout-context splits,
  related /pm ideas, withheld 1X2 near-misses) is tagged ``1X2_90min``.
* Traded side: the feed's explicit ``pm[stage].side`` is preferred when
  present (``side_source: "feed"``); for pre-side feeds the side is derived
  from sign(model - mid) and VERIFIED — an ``edge_adj`` the quoted mid cannot
  justify caps the row at WATCH (``side_attribution_uncertain``,
  ``side_source: "derived"``).
* Every numeric field is copied from a named source-feed field (``*_source``
  strings); when an input is missing the field is ``null`` plus a reason —
  never a guess. Aggregates state their n. No fabricated joins: wallet
  signals are category-level only because orderflow.json carries no
  defensible per-market/per-team join key.

Deterministic and fully offline: reads only the local JSON feeds, never the
network; the only timestamp is the injectable ``--generated`` clock (an
unparseable stamp is a hard error, never a silent wall-clock fallback).

Ranking honours the canonical selection rule: bucket by MODEL probability of
the position paying out (moneyline >=50c > mid 25-50c > longshot <25c)
primary, further-out stage first secondary (win > Final > SF > QF > R16 > R32
> group_winner), fee-adjusted edge as the tiebreak only. Decided legs and
withheld near-misses rank after the actionable tier. Consumers must render
feed order — no client re-sorting.

Usage
-----
    python3 scripts/wca_edge_desk.py \
        [--advancement site/advancement_data.json] [--bet-recs site/bet_recs.json] \
        [--scores-markets site/scores_markets.json] [--pm-ideas site/pm_ideas.json] \
        [--orderflow site/microstructure/orderflow.json] \
        [--out site/advancement_edge_desk.json] [--generated 2026-07-07T10:00:00Z]
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Pure module (no heavy deps): ALL team joins go through canonical() — a name
# that fails to resolve produces a garbage join, the most dangerous failure
# mode in the pipeline (see src/wca/data/teamnames.py).
from wca.data.teamnames import canonical  # noqa: E402

# Canonical selection rule, IMPORTED from src/wca/selection.py — the ONE place
# the rule lives (human-approved-change file; CLAUDE.md selection rules).
# moneyline >=50c > mid 25-50c > longshot <25c; strict <25c cash floor. This
# closes the PR #170 follow-up that replicated the thresholds while the
# selection-module PR (#171) was still concurrent. wca.selection is light
# (stdlib-only at import; pandas is lazy inside hours_out), so the desk stays
# a no-heavy-deps offline script.
from wca.selection import (  # noqa: E402
    LONGSHOT_PROB,
    PROB_BUCKETS,
    longshot_no_cash,
    prob_bucket,
)

# Edge gate threshold: betrecs' live actionable-rec threshold (2pp
# fee-adjusted), IMPORTED from its defining module (scripts/wca_betrecs.py is
# import-light: stdlib + wca.markets.bankroll + wca.selection, no pandas).
# Rows with 0 < edge <= MIN_EDGE_ADJ are near-threshold: informative, WATCH
# only. There is no shared constants home for MIN_EDGE beyond betrecs itself —
# importing beats replicating; inventing a new location is out of scope.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from wca_betrecs import MIN_EDGE as MIN_EDGE_ADJ  # noqa: E402

SCHEMA_VERSION = 2

# Bucket display rank derived from the canonical PROB_BUCKETS ordering
# (moneyline 0 > mid 1 > longshot 2); unknown/None buckets sort last (rank 3
# supplied at the call site).
_BUCKET_RANK = {name: rank for rank, (_lo, name) in enumerate(PROB_BUCKETS)}

# Polymarket sports taker-fee coefficient, REPLICATED from
# src/wca/advancement.py::pm_taker_fee (fee per share = 0.03 * p * (1 - p);
# symmetric in p -> 1-p, so one band covers both sides).
PM_TAKER_FEE_COEF = 0.03

# Tolerance for the side-attribution consistency check (see
# _side_attribution): covers the 4-dp rounding of model/pm/edge_adj in
# advancement_data plus fee-curvature between mid and ask. Kept tight so a
# stale-print mid cannot masquerade as a verified attribution; a false
# positive only downgrades SHADOW_ADD -> WATCH (the fail-safe direction).
SIDE_ATTRIBUTION_EPS = 2e-4

# Freshness fails CLOSED on future-dated stamps beyond this skew allowance.
CLOCK_SKEW_TOLERANCE_S = 300

# Default feed locations (repo-relative), matching deploy/publish_site.sh.
DEFAULT_PATHS = {
    "advancement": os.path.join("site", "advancement_data.json"),
    "bet_recs": os.path.join("site", "bet_recs.json"),
    "scores_markets": os.path.join("site", "scores_markets.json"),
    "pm_ideas": os.path.join("site", "pm_ideas.json"),
    "orderflow": os.path.join("site", "microstructure", "orderflow.json"),
}
_SOURCE_ORDER = ("advancement", "bet_recs", "scores_markets", "pm_ideas",
                 "orderflow")
# Spec-preferred feed name. The PR that renamed this from edge_desk.json keeps
# no alias: nothing shipped consumed the old name (the Action Desk panel and
# publish wiring moved with it in the same commit).
DEFAULT_OUT = os.path.join("site", "advancement_edge_desk.json")

# Freshness gate thresholds (seconds). Judgment-call defaults, documented for
# review: advancement_data + bet_recs + scores_markets are rebuilt by the
# 30-min publish loop (3h = six missed cycles); pm_ideas refreshes with
# pmpropose runs (2h game interval -> 6h grace); orderflow is an hourly job
# that MUST run on match days (24h grace covers quiet days without masking a
# dead job for long).
FRESHNESS_MAX_AGE_S = {
    "advancement": 3 * 3600,
    "bet_recs": 3 * 3600,
    "scores_markets": 3 * 3600,
    "pm_ideas": 6 * 3600,
    "orderflow": 24 * 3600,
}
# The wrapper stamp (meta.generated) is re-stamped every publish, but
# model_prob/edge_adj come from the sim cache whose OWN stamp is
# meta.model_generated: wca_advancement_data.py re-sims only when the cache is
# older than --max-age-hours (default 12h) and silently reuses an arbitrarily
# old cache when the sim FAILS. The model stamp therefore gets its own check
# (12h cadence + 2h publish slack): a stale model stamp fails freshness even
# when the wrapper stamp is minutes old.
ADV_MODEL_MAX_AGE_S = 14 * 3600

# Where each source stamps its build time.
_STAMP_FIELDS = {
    "advancement": ("meta", "generated"),
    "bet_recs": ("meta", "generated"),
    "scores_markets": ("meta", "generated"),
    "pm_ideas": ("meta", "generated"),
    "orderflow": ("generated_utc",),
}

# Advancement stage -> orderflow category key (site/microstructure/orderflow.json
# category_matrix). "win" is the outright-winner category.
STAGE_TO_FLOW_CATEGORY = {
    "R32": "advancement_r32",
    "R16": "advancement_r16",
    "QF": "advancement_qf",
    "SF": "advancement_sf",
    "Final": "advancement_final",
    "win": "winner",
    "group_winner": "group_winner",
}

# Reaching <stage> is decided by the team's tie in the PREVIOUS round: the
# scores_markets list holding that tie, per stage. R32/group_winner resolve on
# the group stage (no single deciding tie -> group_context instead).
STAGE_DECIDING_ROUND = {
    "R16": "r32_games",
    "QF": "r16_games",
    "SF": "qf_games",
    "Final": "sf_games",
    "win": "final_games",
}
_KO_ROUND_LISTS = ("r32_games", "r16_games", "qf_games", "sf_games",
                   "final_games")

# Further-out first (more likely mispriced): later-resolving stages rank
# earlier within a bucket. group_winner resolves at the group stage = most
# imminent.
_STAGE_FURTHER_OUT = {"win": 0, "Final": 1, "SF": 2, "QF": 3, "R16": 4,
                      "R32": 5, "group_winner": 6}

CLV_BLOCKER_REASON = (
    "BLOCKED: durable PM/odds price history for advancement markets is "
    "stale/missing and no CLV stamping / price capture exists for them (no "
    "fixed close; convergence metrics still COLLECTING). Live-money gate: a "
    "market without price capture + CLV stamping does not get real money — "
    "ANY real-money upgrade first needs price/CLV capture restored. This desk "
    "is SHADOW-ONLY until that plumbing ships."
)

# Spec-verbatim settlement basis, stamped on EVERY candidate.
SETTLEMENT_BASIS = "PM advancement includes ET+pens; 1X2 is 90 minutes only"
SETTLEMENT_1X2 = "1X2_90min"
SETTLEMENT_NOTE = (
    "Settlement bases must never be confused: PM advancement includes extra "
    "time + penalties; 1X2 settles at 90 minutes. Every embedded 90-minute "
    "number in this feed is tagged %s." % SETTLEMENT_1X2
)

VERDICT_ENUM = ("SHADOW_ADD", "WATCH", "WITHHOLD", "DO_NOT_TRADE")
VERDICT_LEGEND = {
    "SHADOW_ADD": ("all data gates pass in a cash-eligible bucket — SHADOW "
                   "ledger candidate only; the feed-wide clv_history blocker "
                   "keeps every label non-executing"),
    "WATCH": ("informative but blocked by a soft gate (projected tie, "
              "longshot_no_cash, hot-flow-without-edge, near-threshold edge, "
              "side_attribution_uncertain) — never a trade"),
    "WITHHOLD": ("carried from bet_recs.withheld or a hard data gap/state "
                 "mismatch; withheld_reason recorded — never a trade"),
    "DO_NOT_TRADE": "negative/no model edge or a hard gate failed",
}


def _now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value):
    """Parse the feeds' timestamp dialects to an aware UTC datetime, else None.

    Seen in the wild: "2026-07-07 08:38:02 UTC", "2026-07-07 07:20 UTC",
    "2026-07-07T08:18:46Z", "2026-07-07T08:16:05.688434+00:00".
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith(" UTC"):
        core = s[:-4].strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(core, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _dig(obj, path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _stamp_of(name, feed):
    if not isinstance(feed, dict):
        return None
    return _dig(feed, _STAMP_FIELDS[name])


def load_json(path):
    """Read a JSON file; (payload, None) on success else (None, reason)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "file not found: %s" % path
    except (OSError, ValueError) as exc:
        return None, "unreadable (%s): %s" % (exc.__class__.__name__, path)


# --------------------------------------------------------------------- gates


def _age_check(name, source, stamp, max_age, now_dt):
    """One freshness entry. Fail-closed: missing, unparseable AND future-dated
    stamps (beyond clock-skew tolerance) all fail."""
    entry = {"name": name, "source": source, "stamp": stamp, "age_secs": None,
             "max_age_secs": max_age, "pass": False, "reason": None}
    if stamp is None:
        entry["reason"] = "stamp missing"
        return entry
    ts = _parse_ts(stamp)
    if ts is None:
        entry["reason"] = "no parseable generated stamp"
        return entry
    age = (now_dt - ts).total_seconds()
    entry["age_secs"] = round(age, 1)
    if age < -CLOCK_SKEW_TOLERANCE_S:
        entry["reason"] = ("future-dated stamp: %.1f min ahead of the clock, "
                           "beyond the %d-min skew tolerance — fail closed"
                           % (-age / 60.0, CLOCK_SKEW_TOLERANCE_S // 60))
    elif age <= max_age:
        entry["pass"] = True
    else:
        entry["reason"] = "stale: %.1fh old > %.1fh max" % (
            age / 3600.0, max_age / 3600.0)
    return entry


def _freshness(feeds, source_paths, load_errors, now_dt):
    """Per-source freshness + the advancement model-stamp and PM-visibility
    checks. The wrapper stamp alone is NOT sufficient for advancement_data:
    its model numbers come from a sim cache with its own (older) stamp, and a
    PM-blind build (n_pm_markets=0) is a documented poison marker."""
    checks = []
    for name in _SOURCE_ORDER:
        feed = feeds.get(name)
        if feed is None:
            checks.append({
                "name": name, "source": source_paths[name], "stamp": None,
                "age_secs": None, "max_age_secs": FRESHNESS_MAX_AGE_S[name],
                "pass": False,
                "reason": load_errors.get(name) or "source missing",
            })
        else:
            checks.append(_age_check(name, source_paths[name],
                                     _stamp_of(name, feed),
                                     FRESHNESS_MAX_AGE_S[name], now_dt))
    adv = feeds.get("advancement")
    if adv is not None:
        checks.append(_age_check(
            "advancement_model",
            source_paths["advancement"] + " (meta.model_generated)",
            _dig(adv, ("meta", "model_generated")), ADV_MODEL_MAX_AGE_S,
            now_dt))
        n_pm = _dig(adv, ("meta", "n_pm_markets"))
        ok = isinstance(n_pm, (int, float)) and n_pm > 0
        checks.append({
            "name": "advancement_pm_markets",
            "source": source_paths["advancement"] + " (meta.n_pm_markets)",
            "stamp": None, "age_secs": None, "max_age_secs": None,
            "pass": bool(ok),
            "reason": None if ok else (
                "PM-BLIND poison marker: meta.n_pm_markets=%r — the "
                "advancement build could not see Polymarket (documented guard "
                "in wca_advancement_data.py); its quotes are untrustworthy"
                % (n_pm,)),
        })
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def _gate(passed, reason=None, note=None):
    g = {"pass": bool(passed), "reason": None if passed else (reason or "failed")}
    if note is not None:
        g["note"] = note
    return g


# ----------------------------------------------------------------- side logic


def _side_attribution(model_prob, pm_price, edge_adj):
    """Derive the traded side and verify the attribution is defensible.

    FALLBACK ONLY for pre-side feeds: advancement_data emits an explicit
    ``side`` (+ executable ``ask``) per pm entry since 2026-07-07 and the
    build loop prefers it (``side_source: "feed"``); this derivation runs
    only when that field is absent (``side_source: "derived"``).

    Older ``advancement_data`` builds emit the BETTER-SIDE (YES or NO,
    ask-based) fee-adjusted edge as ``edge_adj`` but drop the side column
    (wca.advancement.AdvancementEdge keeps it; _pm_by_team_stage didn't emit
    it), and the ``pm`` value is the YES mid — which silently falls back to
    the stale last print (priceMap) when bestBid/bestAsk are missing. The desk
    re-implies side = YES iff model >= pm, but that attribution can be WRONG
    against a stale-print "mid" (concrete: priceMap Yes=0.50 stale,
    bestAsk=0.40, model=0.45 -> the edge belongs to YES, the sign test says
    NO). The feed carries no price-basis marker, so the desk verifies instead:
    the derived side's own model-vs-mid edge is

        mid_edge = |model - pm| - fee_band,  fee_band = 0.03 * pm * (1 - pm)

    (fee replicated from wca.advancement.pm_taker_fee; symmetric in p -> 1-p,
    so one band covers both sides). A positive ``edge_adj`` is only
    attributable to the derived side when the quoted mid can justify it: if
    ``mid_edge <= 0`` OR ``edge_adj`` exceeds ``mid_edge`` by more than
    SIDE_ATTRIBUTION_EPS, the buy price behind edge_adj diverges from the
    quote beyond the fee band -> attribution is UNCERTAIN and the row is
    capped at WATCH (never SHADOW_ADD).
    """
    side = "YES" if model_prob >= pm_price else "NO"
    position_prob = round(model_prob if side == "YES" else 1.0 - model_prob, 4)
    confidence = "derived"
    reason = None
    if edge_adj is not None and edge_adj > 0:
        fee_band = PM_TAKER_FEE_COEF * pm_price * (1.0 - pm_price)
        mid_edge = abs(model_prob - pm_price) - fee_band
        if mid_edge <= 0 or edge_adj > mid_edge + SIDE_ATTRIBUTION_EPS:
            confidence = "uncertain"
            reason = (
                "side_attribution_uncertain: edge_adj %+0.4f cannot be "
                "justified by the quoted YES mid on the derived %s side "
                "(mid-implied edge %+0.4f after the %0.4f fee band) — the "
                "quote is likely a stale last-print fallback and the edge may "
                "belong to the OTHER side; advancement_data records no "
                "price-basis/side marker to verify"
                % (edge_adj, side, mid_edge, fee_band))
    return side, position_prob, confidence, reason


# ------------------------------------------------------------- context joins


def _idea_teams(idea):
    """Canonical team names an idea references (match 'A vs B' + selection)."""
    teams = set()
    match = str(idea.get("match") or "")
    if " vs " in match:
        for part in match.split(" vs ", 1):
            if part.strip():
                teams.add(canonical(part.strip()))
    sel = str(idea.get("selection") or "").strip()
    if sel:
        teams.add(canonical(sel))
    return teams


def _related_pm_ideas(pm_ideas, team):
    """Parked /pm ideas referencing the team — joined on canonical team names
    (wca.data.teamnames), never raw substrings. Every idea embeds 90-minute
    1X2 prices, so each item is settlement-tagged."""
    if not isinstance(pm_ideas, dict):
        return None
    out = []
    for idea in pm_ideas.get("ideas") or []:
        if team and team in _idea_teams(idea):
            item = {k: idea.get(k) for k in
                    ("bucket", "match", "selection", "side",
                     "price_c", "model_c", "ev_pct", "size_usd")}
            item["settlement_basis"] = SETTLEMENT_1X2
            out.append(item)
    return out


def _hot_baseline(orderflow):
    """Relative hot-flow threshold: taker flow is structurally buy-heavy, so
    an absolute cutoff over-tags. 'Hot' = category buy_pressure >= the
    cross-category mean + 1 sample sd WITHIN this orderflow feed."""
    if not isinstance(orderflow, dict):
        return None
    bps = [c.get("buy_pressure") for c in orderflow.get("category_matrix") or []]
    bps = [float(b) for b in bps if isinstance(b, (int, float))]
    base = {
        "method": ("hot = category buy_pressure >= cross-category mean + 1 "
                   "sample sd within this orderflow feed (taker flow is "
                   "structurally buy-heavy, so an absolute threshold "
                   "over-tags)"),
        "n_categories": len(bps),
        "mean_buy_pressure": None, "sd_buy_pressure": None,
        "hot_threshold": None,
    }
    if len(bps) >= 2:
        mean = statistics.fmean(bps)
        sd = statistics.stdev(bps)
        base["mean_buy_pressure"] = round(mean, 4)
        base["sd_buy_pressure"] = round(sd, 4)
        base["hot_threshold"] = round(mean + sd, 4)
    else:
        base["reason"] = ("fewer than 2 categories with buy_pressure — no "
                          "baseline, hot tags suppressed")
    return base


def _latency_context(orderflow, category):
    rows = _dig(orderflow, ("latency", "by_category")) or []
    for row in rows:
        if row.get("category") == category:
            return {k: row.get(k) for k in
                    ("n_jumps", "median_reprice_s", "p90_reprice_s",
                     "median_move_cents", "first30s_usd_share")}
    return None


def _orderflow_context(orderflow, stage, baseline):
    """Taker-flow aggregates for the row's market CATEGORY. Context ONLY —
    never overrides the edge gate; no exact-market/per-team wallet joins are
    faked (orderflow.json aggregates carry no defensible key for them). The
    source feed's honesty notes ride on every row verbatim."""
    note = "taker-side flow only; context — NEVER overrides the edge gate"
    ctx = {
        "category": STAGE_TO_FLOW_CATEGORY.get(stage),
        "signal_level": "category",
        "n_trades": None, "usd": None, "avg_trade_usd": None,
        "buy_pressure": None, "smart_usd_share": None, "dumb_usd_share": None,
        "hot": None, "latency": None, "honesty_notes": [],
        "no_exact_join": ("exact-market / per-team wallet signals are NOT "
                          "provided: orderflow.json aggregates carry no "
                          "defensible join key — omitted rather than faked"),
        "reason": None, "note": note,
    }
    if not isinstance(orderflow, dict):
        ctx["reason"] = "orderflow feed unavailable"
        return ctx
    ctx["honesty_notes"] = [str(n) for n in orderflow.get("honesty_notes") or []]
    row = None
    for cand in orderflow.get("category_matrix") or []:
        if cand.get("category") == ctx["category"]:
            row = cand
            break
    if row is None:
        ctx["reason"] = ("category %r not in orderflow category_matrix"
                         % ctx["category"])
        return ctx
    bp = row.get("buy_pressure")
    threshold = (baseline or {}).get("hot_threshold")
    ctx.update({
        "n_trades": row.get("n_trades"),
        "usd": row.get("usd"),
        "avg_trade_usd": row.get("avg_trade_usd"),
        "buy_pressure": bp,
        "smart_usd_share": row.get("smart_usd_share"),
        "dumb_usd_share": row.get("dumb_usd_share"),
        "hot": (bool(bp >= threshold)
                if bp is not None and threshold is not None else None),
        "latency": _latency_context(orderflow, ctx["category"]),
    })
    return ctx


def _group_context(advancement, team_row):
    """Projected-tie flag: another team in the group level on pts+gd+gf."""
    group_key = team_row.get("group")
    groups = (advancement or {}).get("groups")
    if not isinstance(groups, dict) or group_key not in groups:
        return {"projected_tie": None, "tied_with": [],
                "reason": "group table for %r not in advancement_data.groups" % group_key}
    table = groups.get(group_key) or []
    me = None
    for row in table:
        if row.get("team") == team_row.get("team"):
            me = row
            break
    if me is None:
        return {"projected_tie": None, "tied_with": [],
                "reason": "team not found in group %r table" % group_key}
    tied = [r.get("team") for r in table
            if r.get("team") != me.get("team")
            and r.get("pts") == me.get("pts")
            and r.get("gd") == me.get("gd")
            and r.get("gf") == me.get("gf")]
    return {"projected_tie": bool(tied), "tied_with": tied,
            "reason": ("level on pts+gd+gf with %s — projected finishing position "
                       "ambiguous on visible tiebreakers" % ", ".join(tied))
                      if tied else None}


def _team_in_game(game, team):
    return team is not None and team in (canonical(game.get("home")),
                                         canonical(game.get("away")))


def _game_context(game, team):
    """One scores_markets game as knockout context for ``team`` (canonical).

    real-vs-projected comes from the game's OWN fields: ``projected`` (bool —
    is the pairing itself only a model projection?) and ``ft`` (final-time
    score string when played). The 90-minute split/top scoreline are model
    context tagged 1X2_90min — NEVER comparable with PM advancement prices.
    """
    home, away = game.get("home"), game.get("away")
    ch, ca = canonical(home), canonical(away)
    ctx = {
        "home": home, "away": away, "opponent": None,
        "round": game.get("round"), "date": game.get("date"),
        "match_no": game.get("match_no"),
        "tie_status": "projected" if game.get("projected") else "real",
        "played": game.get("ft") is not None,
        "ft": game.get("ft"),
        "model_split_90min": None,
        "top_scoreline": game.get("top"),
        "top_scoreline_prob": game.get("topp"),
        "scoreline_orientation": "home-away",
        "settlement_basis": SETTLEMENT_1X2 + " (context only — never compare "
                            "with PM advancement, which includes ET+pens)",
    }
    x = game.get("x1x2")
    split_ok = isinstance(x, (list, tuple)) and len(x) == 3
    if team in (ch, ca):
        is_home = team == ch
        ctx["opponent"] = ca if is_home else ch
        if split_ok:
            ctx["model_split_90min"] = {
                "team_win": x[0] if is_home else x[2],
                "draw": x[1],
                "opp_win": x[2] if is_home else x[0],
                "basis": SETTLEMENT_1X2,
            }
    elif split_ok:
        ctx["model_split_90min"] = {"home_win": x[0], "draw": x[1],
                                    "away_win": x[2], "basis": SETTLEMENT_1X2}
    return ctx


def _knockout_context(scores_markets, team, stage):
    """Knockout context from scores_markets: the tie that DECIDES reaching
    ``stage`` (the team's game in the previous round) + the team's next
    unplayed knockout game. Missing joins get a reason, never a guess."""
    out = {"deciding_tie": None, "deciding_tie_reason": None,
           "next_match": None, "next_match_reason": None}
    if not isinstance(scores_markets, dict):
        out["deciding_tie_reason"] = "scores_markets feed unavailable"
        out["next_match_reason"] = "scores_markets feed unavailable"
        return out
    key = STAGE_DECIDING_ROUND.get(stage)
    if key is None:
        out["deciding_tie_reason"] = (
            "stage %r resolves on the group stage — no single knockout "
            "deciding tie (see group_context)" % stage)
    else:
        game = next((g for g in scores_markets.get(key) or []
                     if _team_in_game(g, team)), None)
        if game is None:
            out["deciding_tie_reason"] = (
                "no tie featuring %s in scores_markets.%s — the projected "
                "bracket's modal path does not reach this stage; tie context "
                "unknown (treated as projected by the projection gate)"
                % (team, key))
        else:
            out["deciding_tie"] = _game_context(game, team)
    for k in _KO_ROUND_LISTS:
        game = next((g for g in scores_markets.get(k) or []
                     if g.get("ft") is None and _team_in_game(g, team)), None)
        if game is not None:
            out["next_match"] = _game_context(game, team)
            break
    if out["next_match"] is None:
        out["next_match_reason"] = ("no unplayed knockout game featuring %s "
                                    "in scores_markets" % team)
    return out


def _fixture_game(scores_markets, fixture):
    """The scores_markets game matching an 'A vs B' fixture string, if any."""
    if not isinstance(scores_markets, dict) or " vs " not in str(fixture or ""):
        return None
    a, b = (canonical(p.strip()) for p in str(fixture).split(" vs ", 1))
    for key in _KO_ROUND_LISTS:
        for g in scores_markets.get(key) or []:
            if {canonical(g.get("home")), canonical(g.get("away"))} == {a, b}:
                return g
    return None


# ------------------------------------------------------------------ verdicts


def _projection_gate(stage, ko_ctx, group_ctx):
    """Projected future tie context is DOWNGRADED vs known real tie context.

    Knockout stages: pass only when the deciding tie exists in the bracket
    with a real (non-projected) pairing. Group-decided stages (R32,
    group_winner): pass unless the group table shows a projected finishing-
    position tie."""
    if stage in ("R32", "group_winner"):
        tied = (group_ctx or {}).get("projected_tie")
        if tied:
            return _gate(False, "projected group-position tie: %s"
                         % ((group_ctx or {}).get("reason")
                            or "tied on visible tiebreakers"))
        return _gate(True, note="stage resolves on the group stage; no "
                                "projected-tie ambiguity in the group table")
    tie = (ko_ctx or {}).get("deciding_tie")
    if tie is None:
        return _gate(False, (ko_ctx or {}).get("deciding_tie_reason")
                     or "deciding tie unknown — treated as projected")
    if tie.get("tie_status") != "real":
        return _gate(False, "deciding %s tie (%s vs %s) is PROJECTED — the "
                            "pairing is a model projection, not a fixed fixture"
                     % (tie.get("round"), tie.get("home"), tie.get("away")))
    return _gate(True)


def _verdict_for_actionable(edge_adj, bucket, gates, flow_hot):
    """4-label mapping for the actionable tier. Precedence: hard fails ->
    DO_NOT_TRADE (except negative-edge + hot flow -> WATCH, still never a
    trade); soft caps -> WATCH; else SHADOW_ADD. Orderflow can upgrade
    confidence only — it can NEVER turn a negative model edge into a trade,
    and longshots can never be SHADOW_ADD."""
    hard = [gates[n]["reason"] for n in ("freshness", "price_present")
            if not gates[n]["pass"]]
    if edge_adj is None:
        return "WITHHOLD", ["data gap: no fee-adjusted edge available from "
                            "any source feed — cannot rank"]
    if hard:
        return "DO_NOT_TRADE", hard
    if edge_adj <= 0:
        if flow_hot:
            return "WATCH", [
                "hot_flow_without_edge: category taker flow is hot but the "
                "fee-adjusted model edge is %+0.4f <= 0 — orderflow is "
                "context only and can NEVER turn a negative model edge into "
                "a trade" % edge_adj]
        return "DO_NOT_TRADE", [gates["edge"]["reason"]]
    soft = []
    if edge_adj <= MIN_EDGE_ADJ:
        soft.append("near_threshold_edge: fee-adjusted edge %+0.4f is inside "
                    "the %0.2f actionable threshold (scripts/wca_betrecs.py "
                    "MIN_EDGE)" % (edge_adj, MIN_EDGE_ADJ))
    if bucket == "longshot":
        soft.append(gates["min_prob_cash"]["reason"] or "longshot_no_cash")
    if not gates["projection"]["pass"]:
        soft.append("projected_tie: %s" % gates["projection"]["reason"])
    if not gates["side_attribution"]["pass"]:
        soft.append(gates["side_attribution"]["reason"]
                    or "side_attribution_uncertain")
    if soft:
        return "WATCH", soft
    return "SHADOW_ADD", [
        "all data gates pass in cash-eligible bucket %r; live money remains "
        "BLOCKED feed-wide by the clv_history gate (see clv_history_blocker)"
        % bucket]


# ---------------------------------------------------------------- row builds


def _bet_rec_index(bet_recs):
    """(canonical team, stage) -> advancement_futures row."""
    idx = {}
    if isinstance(bet_recs, dict):
        for row in bet_recs.get("advancement_futures") or []:
            team, stage = row.get("team"), row.get("stage")
            if team and stage:
                idx[(canonical(team), stage)] = row
    return idx


def _bet_rec_block(br):
    if br is None:
        return None
    return {
        "id": br.get("id"),
        "stake": br.get("stake"),
        "currency": br.get("currency"),
        "action_label": br.get("action_label"),
        "ev_net": br.get("ev_net"),
        "pm_fee": br.get("pm_fee"),
        "source": "bet_recs.advancement_futures[%s]" % br.get("id"),
    }


def _build_adv_row(team, team_row, stage, *, origin, model_prob, pm_price,
                   edge_adj, pm_src, edge_src, side_info, side_source, br,
                   advancement, scores_markets, pm_ideas, orderflow,
                   flow_baseline, freshness):
    side, position_prob, side_confidence, side_reason = side_info
    bucket = prob_bucket(position_prob if position_prob is not None
                         else model_prob)
    decided = model_prob is not None and not (0.0 < model_prob < 1.0)
    flow = _orderflow_context(orderflow, stage, flow_baseline)
    group_ctx = _group_context(
        advancement, team_row if team_row is not None
        else {"team": team, "group": (br or {}).get("group")})
    ko_ctx = _knockout_context(scores_markets, team, stage)
    ideas = _related_pm_ideas(pm_ideas, team)

    gates = {}
    gates["freshness"] = _gate(
        freshness["pass"],
        "stale/missing inputs: " + "; ".join(
            "%s (%s)" % (c["source"], c["reason"]) for c in freshness["checks"]
            if not c["pass"]))
    gates["price_present"] = _gate(pm_price is not None,
                                   "no PM price for %s/%s" % (team, stage))
    gates["edge"] = _gate(
        edge_adj is not None and edge_adj > MIN_EDGE_ADJ,
        ("no fee-adjusted edge available" if edge_adj is None else
         ("fee-adjusted edge %+0.4f <= 0 — no positive model edge (orderflow "
          "can NEVER turn this into a trade)" % edge_adj) if edge_adj <= 0 else
         ("near_threshold_edge: %+0.4f inside the %0.2f actionable threshold "
          "(scripts/wca_betrecs.py MIN_EDGE)" % (edge_adj, MIN_EDGE_ADJ))),
        note="threshold %0.2f replicated from scripts/wca_betrecs.py MIN_EDGE"
             % MIN_EDGE_ADJ)
    gates["settlement"] = _gate(
        True, note=("model advancement prob vs PM advancement quote — same "
                    "settlement basis (ET+pens). Embedded 90-min splits are "
                    "tagged %s and are context only, never compared with PM "
                    "advancement prices as if they settle the same."
                    % SETTLEMENT_1X2))
    gates["projection"] = _projection_gate(stage, ko_ctx, group_ctx)
    hot = bool(flow.get("hot"))
    gates["orderflow"] = _gate(
        True, note=("contextual only — may upgrade confidence, must never "
                    "turn a negative model edge into a trade"
                    + ("; confidence elevated: hot category flow behind a "
                       "positive-edge row" if hot and gates["edge"]["pass"]
                       else "")))
    gates["side_attribution"] = _gate(side_confidence != "uncertain",
                                      side_reason)
    gates["min_prob_cash"] = _gate(
        not longshot_no_cash(position_prob if position_prob is not None
                             else model_prob),
        "longshot_no_cash: position pays out at model %s < %.0f%% — no cash on "
        "longshots (likely-PnL rule; 0-for-12 in backtests)"
        % ("%.0f%%" % (position_prob * 100) if position_prob is not None
           else "?", LONGSHOT_PROB * 100))
    gates["clv_history"] = _gate(False, CLV_BLOCKER_REASON)

    if decided:
        if br is not None:
            verdict = "WITHHOLD"
            verdict_reasons = [
                "decided_leg_state_mismatch: model shows this leg decided "
                "(prob %.1f) but bet_recs still lists it actionable (%s) — "
                "stale rec or settled leg; included and labelled rather than "
                "silently dropped" % (model_prob, (br or {}).get("id"))]
        else:
            verdict = "DO_NOT_TRADE"
            verdict_reasons = [
                "decided_leg: model prob %.1f — any residual PM quote is "
                "settlement/convergence residue, not a model edge"
                % model_prob]
    else:
        verdict, verdict_reasons = _verdict_for_actionable(
            edge_adj, bucket, gates, hot)

    return {
        "team": team,
        "group": ((team_row or {}).get("group") or (br or {}).get("group")),
        "stage": stage,
        "origin": origin,
        "leg_state": "decided" if decided else "undecided",
        "market": "advancement",
        "market_label": ("Win tournament · incl. ET+pens" if stage == "win"
                         else "Win group (settles on group stage)"
                         if stage == "group_winner"
                         else "Advance · incl. ET+pens"),
        "settlement_basis": SETTLEMENT_BASIS,
        "model_prob": model_prob,
        "model_source": ("advancement_data.teams[%s].model[%s]" % (team, stage)
                         if team_row is not None else
                         "bet_recs.advancement_futures[%s].model_prob"
                         % (br or {}).get("id")),
        "pm_price": pm_price,
        "pm_price_source": pm_src,
        "edge_adj": edge_adj,
        "edge_source": edge_src,
        "side": side,
        "side_confidence": side_confidence,
        "side_source": side_source,
        "side_note": ("explicit side emitted by advancement_data "
                      "(pm[stage].side)" if side_source == "feed" else
                      side_reason if side_confidence == "uncertain" else
                      "explicit YES buy from bet_recs"
                      if side_confidence == "explicit"
                      else "derived from sign(model - pm_yes_mid): "
                           "advancement_data omits the traded side"),
        "position_prob": position_prob,
        "position_prob_source": ("= model_prob" if side == "YES"
                                 else "= 1 - model_prob (derived; NO side)"),
        "bucket": bucket,
        "bet_rec": _bet_rec_block(br),
        "knockout_context": ko_ctx,
        "group_context": group_ctx,
        "orderflow": flow,
        "related_pm_ideas": {
            "n": None if ideas is None else len(ideas),
            "ideas": ideas or [],
            "reason": "pm_ideas feed unavailable" if ideas is None else None,
        },
        "gates": gates,
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
    }


def _build_withheld_row(w, *, scores_markets, freshness):
    """A bet_recs.withheld near-miss: informative, labelled WITHHOLD — never
    actionable. Its withheld_reason is carried verbatim. 1X2/prop rows settle
    at 90 minutes / on book rules — never comparable with PM advancement."""
    team = canonical(w.get("team")) if w.get("team") else None
    market = w.get("market")
    model_prob = w.get("model_prob")
    edge = w.get("edge")
    bucket = prob_bucket(model_prob) if model_prob is not None else None
    game = _fixture_game(scores_markets, w.get("fixture"))
    ko_ctx = {"deciding_tie": None,
              "deciding_tie_reason": "withheld near-miss: fixture bet, no "
                                     "advancement deciding tie",
              "next_match": _game_context(game, team) if game else None,
              "next_match_reason": None if game else
              "fixture %r not found in scores_markets knockout rounds"
              % w.get("fixture")}
    basis_1x2 = market == "1X2"
    gates = {
        "freshness": _gate(freshness["pass"], "stale/missing inputs"),
        "price_present": _gate(w.get("price") is not None,
                               "no book price on the withheld row"),
        "edge": _gate(edge is not None and edge > MIN_EDGE_ADJ,
                      "book edge %s not above the %0.2f threshold"
                      % ("%+0.4f" % edge if edge is not None else "n/a",
                         MIN_EDGE_ADJ),
                      note="book edge from bet_recs.withheld — NOT a PM "
                           "fee-adjusted edge"),
        "settlement": _gate(True, note=(
            "settles at 90 minutes (1X2) — NEVER compare with PM advancement "
            "(ET+pens) as if they settle the same" if basis_1x2 else
            "sportsbook %s market — settles on book rules, NOT PM advancement"
            % market)),
        "projection": _gate(True, note="fixture already fixed (near-miss on "
                                       "a real fixture)"),
        "orderflow": _gate(True, note="book-venue near-miss; PM orderflow "
                                      "context not applicable"),
        "side_attribution": _gate(True, note="book selection — no PM side to "
                                             "attribute"),
        "min_prob_cash": _gate(model_prob is not None
                               and not longshot_no_cash(model_prob),
                               "longshot_no_cash: model %s < %.0f%%"
                               % ("%.0f%%" % (model_prob * 100)
                                  if model_prob is not None else "?",
                                  LONGSHOT_PROB * 100)),
        "clv_history": _gate(False, CLV_BLOCKER_REASON),
    }
    return {
        "team": w.get("team"),
        "group": w.get("group") or None,
        "stage": None,
        "origin": "bet_recs.withheld",
        "leg_state": None,
        "market": market,
        "market_label": ("%s · %s" % (w.get("fixture"), w.get("selection"))
                         + (" · settles at 90 min" if basis_1x2 else "")),
        "settlement_basis": (SETTLEMENT_BASIS + " — this row IS the 90-minute "
                             "side" if basis_1x2 else SETTLEMENT_BASIS),
        "fixture": w.get("fixture"),
        "kickoff": w.get("kickoff"),
        "selection": w.get("selection"),
        "model_prob": model_prob,
        "model_source": "bet_recs.withheld[%s].model_prob" % w.get("id"),
        "pm_price": None,
        "pm_price_source": None,
        "book_price_decimal": w.get("price"),
        "book_price_source": "bet_recs.withheld[%s].price (decimal book odds)"
                             % w.get("id"),
        "edge_adj": None,
        "edge_source": None,
        "book_edge": edge,
        "book_edge_source": "bet_recs.withheld[%s].edge (book edge, NOT PM "
                            "fee-adjusted)" % w.get("id"),
        "ev_net": w.get("ev_net"),
        "side": None,
        "side_confidence": None,
        "side_source": None,
        "side_note": None,
        "position_prob": model_prob,
        "position_prob_source": "= model_prob (book selection)",
        "bucket": bucket,
        "bet_rec": None,
        "withheld_reason": w.get("withheld_reason"),
        "knockout_context": ko_ctx,
        "group_context": None,
        "orderflow": None,
        "related_pm_ideas": {"n": 0, "ideas": [], "reason": None},
        "gates": gates,
        "verdict": "WITHHOLD",
        "verdict_reasons": ["withheld by bet_recs: %s" % w.get("withheld_reason")],
    }


def _informative_withheld(w):
    """A withheld row is an informative near-miss only when it carries real
    numbers (model prob + a live price). No-price props / unsupported markets
    are counted, not shown — nothing to rank, nothing to fabricate."""
    return w.get("model_prob") is not None and w.get("price") is not None


# ------------------------------------------------------------------ ordering


def _sort_key(row):
    tier = 2 if row.get("origin") == "bet_recs.withheld" else (
        1 if row.get("leg_state") == "decided" else 0)
    bucket = _BUCKET_RANK.get(row.get("bucket"), 3)
    edge = row.get("edge_adj")
    if edge is None:
        edge = row.get("book_edge")
    edge_key = -(edge if edge is not None else float("-inf"))
    if tier == 2:
        kick = _parse_ts(row.get("kickoff"))
        further = -(kick.timestamp() if kick else float("-inf"))
        return (tier, bucket, further, edge_key,
                row.get("team") or "", row.get("market") or "")
    return (tier, bucket,
            float(_STAGE_FURTHER_OUT.get(row.get("stage"), 9)), edge_key,
            row.get("team") or "", row.get("stage") or "")


# ---------------------------------------------------------------- feed build


def build_feed(advancement, bet_recs, pm_ideas, orderflow, scores_markets, *,
               generated, source_paths=None, load_errors=None):
    """Assemble the edge-desk payload from already-parsed feeds (pure)."""
    source_paths = source_paths or dict(DEFAULT_PATHS)
    load_errors = load_errors or {}
    feeds = {"advancement": advancement, "bet_recs": bet_recs,
             "scores_markets": scores_markets, "pm_ideas": pm_ideas,
             "orderflow": orderflow}
    now_dt = _parse_ts(generated)
    if now_dt is None:
        raise ValueError("unparseable --generated stamp: %r" % (generated,))

    freshness = _freshness(feeds, source_paths, load_errors, now_dt)
    flow_baseline = _hot_baseline(orderflow)

    model_stamp = _dig(advancement or {}, ("meta", "model_generated"))
    model_ts = _parse_ts(model_stamp)
    model_age_h = (round((now_dt - model_ts).total_seconds() / 3600.0, 2)
                   if model_ts else None)

    caveats = ["SHADOW-ONLY: decision-support feed; no verdict here is a "
               "trade instruction and nothing is wired to execution.",
               SETTLEMENT_NOTE,
               CLV_BLOCKER_REASON]
    # Orderflow's OWN honesty notes carried VERBATIM (in-sample/WC-only
    # cohorts, taker-only data, truncated markets, partial wallet history,
    # stale marks) — they also ride on every row's orderflow context.
    if isinstance(orderflow, dict):
        caveats.extend(str(n) for n in orderflow.get("honesty_notes") or [])
        truncated = _dig(orderflow, ("window", "truncated_markets")) or []
        if truncated:
            caveats.append(
                "orderflow history truncated for n=%d markets (data-api 3k-row "
                "cap) — flow aggregates there are incomplete; buy-pressure/share "
                "numbers are context only" % len(truncated))
    for name in _SOURCE_ORDER:
        if feeds[name] is None:
            caveats.append("source unavailable: %s (%s)" % (
                source_paths[name], load_errors.get(name) or "not loaded"))
    for check in freshness["checks"]:
        if not check["pass"] and check["reason"] and (
                "stale" in check["reason"] or "future-dated" in check["reason"]
                or "PM-BLIND" in check["reason"]):
            caveats.append("freshness gate FAILED for %s — %s"
                           % (check["source"], check["reason"]))

    # ---- universe -------------------------------------------------------
    # (1) every advancement_data (team, stage) where BOTH the model prob and
    #     a PM quote exist (incl. group_winner quotes); decided legs kept and
    #     labelled — never silently dropped while bet_recs still lists them;
    # (2) actionable bet_recs advancement futures with no advancement quote;
    # (3) informative bet_recs.withheld near-misses, labelled WITHHOLD.
    rows = []
    bet_rec_idx = _bet_rec_index(bet_recs)
    seen = set()
    n_model_only = 0
    stages = list(_dig(advancement or {}, ("meta", "stages")) or [])
    if "group_winner" not in stages:
        stages.append("group_winner")
    common = dict(advancement=advancement, scores_markets=scores_markets,
                  pm_ideas=pm_ideas, orderflow=orderflow,
                  flow_baseline=flow_baseline, freshness=freshness)
    for team_row in (advancement or {}).get("teams") or []:
        team = canonical(team_row.get("team"))
        for stage in stages:
            prob = _dig(team_row, ("model", stage))
            quote = _dig(team_row, ("pm", stage))
            has_quote = isinstance(quote, dict) and quote.get("pm") is not None
            if prob is None or not has_quote:
                if (prob is not None and 0.0 < prob < 1.0 and not has_quote
                        and (team, stage) not in bet_rec_idx):
                    n_model_only += 1
                continue
            pm_price = quote.get("pm")
            edge_adj = quote.get("edge_adj")
            br = bet_rec_idx.get((team, stage))
            # Prefer the feed's EXPLICIT traded side (advancement_data emits
            # ``side: "YES"|"NO"`` per pm entry since 2026-07-07) — no
            # derivation, no stale-print ambiguity. The sign-derivation +
            # uncertainty guard below survives ONLY as the fallback for
            # pre-side feeds (anything else, incl. malformed values, falls
            # back and is verified rather than trusted).
            feed_side = quote.get("side")
            if feed_side in ("YES", "NO"):
                side_source = "feed"
                side_info = (feed_side,
                             round(prob if feed_side == "YES"
                                   else 1.0 - prob, 4),
                             "explicit", None)
                edge_src = ("advancement_data.teams[%s].pm[%s].edge_adj "
                            "(fee-adjusted edge of the feed-emitted side)"
                            % (team, stage))
            else:
                side_source = "derived"
                side_info = _side_attribution(prob, pm_price, edge_adj)
                edge_src = ("advancement_data.teams[%s].pm[%s].edge_adj "
                            "(better-side fee-adjusted edge; side NOT emitted "
                            "by the feed)" % (team, stage))
            seen.add((team, stage))
            rows.append(_build_adv_row(
                team, team_row, stage, origin="advancement_data",
                model_prob=prob, pm_price=pm_price, edge_adj=edge_adj,
                pm_src="advancement_data.teams[%s].pm[%s].pm (YES mid)"
                       % (team, stage),
                edge_src=edge_src,
                side_info=side_info, side_source=side_source, br=br, **common))
    for (team, stage), br in sorted(bet_rec_idx.items()):
        if (team, stage) in seen:
            continue
        model_prob = br.get("model_prob")
        side_info = ("YES",
                     round(model_prob, 4) if model_prob is not None else None,
                     "explicit", None)
        rows.append(_build_adv_row(
            team, None, stage, origin="bet_recs",
            model_prob=model_prob, pm_price=br.get("pm_price"),
            edge_adj=br.get("edge_adj"),
            pm_src="bet_recs.advancement_futures[%s].pm_price" % br.get("id"),
            edge_src="bet_recs.advancement_futures[%s].edge_adj (YES buy)"
                     % br.get("id"),
            side_info=side_info, side_source="bet_recs", br=br, **common))

    n_withheld_excluded = 0
    for w in (bet_recs or {}).get("withheld") or []:
        if _informative_withheld(w):
            rows.append(_build_withheld_row(
                w, scores_markets=scores_markets, freshness=freshness))
        else:
            n_withheld_excluded += 1

    rows.sort(key=_sort_key)

    if advancement is None:
        caveats.append("advancement_data unavailable — advancement row "
                       "universe is EMPTY (no fabricated rows)")

    n_by_verdict = {v: 0 for v in VERDICT_ENUM}
    for r in rows:
        n_by_verdict[r["verdict"]] += 1
    tie_rows = sum(1 for r in rows
                   if (r.get("group_context") or {}).get("projected_tie"))
    if tie_rows:
        caveats.append("projected group-position tie flagged on n=%d rows — "
                       "check FIFA tiebreakers before trusting group-dependent "
                       "paths" % tie_rows)
    uncertain_rows = sum(1 for r in rows
                         if r.get("side_confidence") == "uncertain")
    if uncertain_rows:
        caveats.append(
            "side attribution UNCERTAIN on n=%d rows (edge_adj cannot be "
            "justified by the quoted YES mid — likely stale-print fallback); "
            "capped at WATCH. These rows lack the explicit pm[stage].side "
            "field (pre-side advancement_data build) — regenerate "
            "advancement_data where PM is reachable." % uncertain_rows)

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "shadow_only": True,
            "settlement_basis": SETTLEMENT_BASIS,
            "sources": {source_paths[k]: _stamp_of(k, feeds[k])
                        for k in _SOURCE_ORDER},
            "advancement_model_generated": model_stamp,
            "advancement_model_age_hours": model_age_h,
            "n_pm_markets": _dig(advancement or {}, ("meta", "n_pm_markets")),
            "caveats": caveats,
            "n_rows": len(rows),
            "n_by_verdict": n_by_verdict,
            "n_advancement_legs_without_pm_quote": n_model_only,
            "n_withheld_excluded_uninformative": n_withheld_excluded,
            "verdict_enum": list(VERDICT_ENUM),
            "verdict_legend": dict(VERDICT_LEGEND),
            "freshness_max_age_s": dict(
                {source_paths[k]: FRESHNESS_MAX_AGE_S[k]
                 for k in FRESHNESS_MAX_AGE_S},
                **{source_paths["advancement"] + " (meta.model_generated)":
                   ADV_MODEL_MAX_AGE_S}),
            "edge_gate": {
                "min_edge_adj": MIN_EDGE_ADJ,
                "source": "imported from scripts/wca_betrecs.py MIN_EDGE "
                          "(current live actionable threshold)",
            },
            "selection_rule": {
                "prob_buckets": [list(b) for b in PROB_BUCKETS],
                "source": "imported from wca.selection (src/wca/selection.py "
                          "— canonical selection rule, human-approved-change "
                          "file)",
            },
            "orderflow_hot": flow_baseline,
            "ordering": ("bucket on the MODEL prob that the position pays out "
                         "(moneyline>=50c, mid 25-50c, longshot <25c) primary; "
                         "further-out stage first secondary (win > Final > SF "
                         "> QF > R16 > R32 > group_winner); fee-adjusted edge "
                         "desc as tiebreak only. Decided legs then withheld "
                         "near-misses rank after the actionable tier. Render "
                         "in feed order — do not re-sort."),
        },
        "freshness": freshness,
        "clv_history_blocker": {"blocked": True, "reason": CLV_BLOCKER_REASON},
        "rows": rows,
    }


def _display_path(path):
    """Repo-relative path for feed metadata (absolute paths stay portable)."""
    abspath = os.path.abspath(path)
    root = _ROOT + os.sep
    return abspath[len(root):] if abspath.startswith(root) else path


def generate(paths, *, generated):
    """Load the five feeds from ``paths`` and build the payload."""
    feeds, errors, display = {}, {}, {}
    for name in _SOURCE_ORDER:
        feeds[name], errors[name] = load_json(paths[name])
        display[name] = _display_path(paths[name])
    return build_feed(
        feeds["advancement"], feeds["bet_recs"], feeds["pm_ideas"],
        feeds["orderflow"], feeds["scores_markets"], generated=generated,
        source_paths=display, load_errors=errors)


def _write_atomic(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".edgedesk_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True, allow_nan=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the SHADOW-ONLY advancement edge-desk feed "
                    "(offline; reads only the local site feeds).")
    parser.add_argument("--advancement",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["advancement"]))
    parser.add_argument("--bet-recs",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["bet_recs"]))
    parser.add_argument("--scores-markets",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["scores_markets"]))
    parser.add_argument("--pm-ideas",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["pm_ideas"]))
    parser.add_argument("--orderflow",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["orderflow"]))
    parser.add_argument("--out", default=os.path.join(_ROOT, DEFAULT_OUT))
    parser.add_argument("--generated", default=None,
                        help="ISO-8601 UTC stamp for meta.generated_at + the "
                             "freshness clock (tests inject a fixed time). An "
                             "unparseable stamp is a HARD ERROR — never a "
                             "silent wall-clock fallback.")
    args = parser.parse_args(argv)

    generated = args.generated or _now_iso_z()
    if _parse_ts(generated) is None:
        print("wca_edge_desk: unparseable --generated stamp %r — refusing to "
              "build against an unknown clock (fail closed)" % (generated,),
              file=sys.stderr)
        return 2

    paths = {"advancement": args.advancement, "bet_recs": args.bet_recs,
             "scores_markets": args.scores_markets, "pm_ideas": args.pm_ideas,
             "orderflow": args.orderflow}
    payload = generate(paths, generated=generated)
    _write_atomic(args.out, payload)
    meta = payload["meta"]
    counts = meta["n_by_verdict"]
    print("edge_desk: wrote %s — %d rows (%s), freshness %s"
          % (args.out, meta["n_rows"],
             ", ".join("%d %s" % (counts[v], v) for v in VERDICT_ENUM),
             "PASS" if payload["freshness"]["pass"] else "FAIL"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
