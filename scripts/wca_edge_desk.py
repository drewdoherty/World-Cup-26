#!/usr/bin/env python
"""Advancement Edge Desk — SHADOW-ONLY decision-support feed (site/edge_desk.json).

Joins the four committed site feeds into one ranked desk of Polymarket
advancement/outright opportunities, each carrying explicit pass/fail gates and
a verdict that is NEVER a trade instruction:

* ``site/advancement_data.json``          — model chain + PM quotes + group tables
* ``site/bet_recs.json``                  — actionable advancement futures (stakes)
* ``site/pm_ideas.json``                  — parked /pm trade ideas (context join)
* ``site/microstructure/orderflow.json``  — PM taker-flow aggregates (context ONLY)

Hard rules encoded here (do not regress):

* SHADOW-ONLY. The verdict enum is {SHADOW_CANDIDATE, DO_NOT_TRADE} — there is
  no TRADE verdict and this script never touches the ledger, Telegram, or any
  execution path. Advancement markets have no fixed close, so no CLV stamping
  exists for them yet; per the live-money gate ("a market without price capture
  + CLV stamping does not get real money") the ``clv_history`` gate is BLOCKED
  on every row until that plumbing ships.
* Orderflow is context, never a signal override: a negative fee-adjusted edge
  is DO_NOT_TRADE no matter how hot the taker flow looks.
* Likely-PnL rule: model <25% longshots never pass the cash gate (0-for-12 /
  0-for-20 in backtests).
* Every numeric field is copied from a named source-feed field (``*_source``
  strings); when an input is missing the field is ``null`` plus a reason —
  never a guess. Aggregates state their n.

Deterministic and fully offline: reads only the local JSON feeds, never the
network; the only timestamp is the injectable ``--generated`` clock.

Ordering follows the encoded selection rules (wca_pm_propose conventions):
prob-bucket first (moneyline >=50c, mid 25-50c, longshot <25c — via
``wca_pm_propose.prob_bucket`` on the probability the POSITION pays out), then
fee-adjusted edge descending (rows with no edge last), then team/stage for a
stable tie-break. Futures have no kickoff, so the hours-out preference term
does not apply. Consumers must render feed order — no client re-sorting.

Usage
-----
    python3 scripts/wca_edge_desk.py \
        [--advancement site/advancement_data.json] [--bet-recs site/bet_recs.json] \
        [--pm-ideas site/pm_ideas.json] [--orderflow site/microstructure/orderflow.json] \
        [--out site/edge_desk.json] [--generated 2026-07-07T09:00:00Z]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SCHEMA_VERSION = 1

# Source-of-truth ordering convention (selection rules; user 2026-07-02).
_spec = importlib.util.spec_from_file_location(
    "wca_pm_propose", os.path.join(_HERE, "wca_pm_propose.py"))
_pm_propose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pm_propose)
prob_bucket = _pm_propose.prob_bucket
_BUCKET_RANK = {"moneyline": 0, "mid": 1, "longshot": 2}

# Default feed locations (repo-relative), matching deploy/publish_site.sh.
DEFAULT_PATHS = {
    "advancement": os.path.join("site", "advancement_data.json"),
    "bet_recs": os.path.join("site", "bet_recs.json"),
    "pm_ideas": os.path.join("site", "pm_ideas.json"),
    "orderflow": os.path.join("site", "microstructure", "orderflow.json"),
}

# Freshness gate thresholds (seconds). Judgment-call defaults, documented for
# review: advancement_data + bet_recs are rebuilt by the 30-min publish loop
# (3h = six missed cycles); pm_ideas refreshes with pmpropose runs (2h game
# interval → 6h grace); orderflow is an hourly job that MUST run on match days
# (24h grace covers quiet days without masking a dead job for long).
FRESHNESS_MAX_AGE_S = {
    "advancement": 3 * 3600,
    "bet_recs": 3 * 3600,
    "pm_ideas": 6 * 3600,
    "orderflow": 24 * 3600,
}

# Where each source stamps its build time.
_STAMP_FIELDS = {
    "advancement": ("meta", "generated"),
    "bet_recs": ("meta", "generated"),
    "pm_ideas": ("meta", "generated"),
    "orderflow": ("generated_utc",),
}

# Advancement stage → orderflow category key (site/microstructure/orderflow.json
# category_matrix). "win" is the outright-winner category.
STAGE_TO_FLOW_CATEGORY = {
    "R32": "advancement_r32",
    "R16": "advancement_r16",
    "QF": "advancement_qf",
    "SF": "advancement_sf",
    "Final": "advancement_final",
    "win": "winner",
}

# Taker buy-pressure at/above this is tagged "hot". Context only — the edge
# gate is computed first and hot flow can never flip a negative edge.
HOT_BUY_PRESSURE = 0.70

CLV_BLOCKER_REASON = (
    "BLOCKED: no CLV stamping / price-capture history exists for PM advancement "
    "markets (no fixed close; convergence metrics still COLLECTING). Live-money "
    "gate: a market without price capture + CLV stamping does not get real money. "
    "This desk is SHADOW-ONLY until that plumbing ships."
)

SETTLEMENT_NOTE = (
    "All rows settle on ADVANCEMENT incl. extra time + penalties — NOT 90-min 1X2."
)

VERDICT_ENUM = ("SHADOW_CANDIDATE", "DO_NOT_TRADE")


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


def _freshness(feeds, source_paths, load_errors, now_dt):
    """Per-source freshness checks against the injected clock. Fail-closed:
    a missing/unparseable stamp fails that source's check."""
    checks = []
    for name in ("advancement", "bet_recs", "pm_ideas", "orderflow"):
        max_age = FRESHNESS_MAX_AGE_S[name]
        entry = {
            "source": source_paths[name],
            "stamp": None,
            "age_secs": None,
            "max_age_secs": max_age,
            "pass": False,
            "reason": None,
        }
        feed = feeds.get(name)
        if feed is None:
            entry["reason"] = load_errors.get(name) or "source missing"
        else:
            stamp = _stamp_of(name, feed)
            entry["stamp"] = stamp
            ts = _parse_ts(stamp)
            if ts is None:
                entry["reason"] = "no parseable generated stamp"
            else:
                age = (now_dt - ts).total_seconds()
                entry["age_secs"] = round(age, 1)
                if age <= max_age:
                    entry["pass"] = True
                else:
                    entry["reason"] = "stale: %.1fh old > %.1fh max" % (
                        age / 3600.0, max_age / 3600.0)
        checks.append(entry)
    return {"pass": all(c["pass"] for c in checks), "checks": checks}


def _bet_rec_index(bet_recs):
    """(team_lower, stage) -> advancement_futures row."""
    idx = {}
    if isinstance(bet_recs, dict):
        for row in bet_recs.get("advancement_futures") or []:
            team, stage = row.get("team"), row.get("stage")
            if team and stage:
                idx[(team.lower(), stage)] = row
    return idx


def _related_pm_ideas(pm_ideas, team):
    """Parked /pm ideas mentioning the team (context join, substring match)."""
    if not isinstance(pm_ideas, dict):
        return None
    needle = team.lower()
    out = []
    for idea in pm_ideas.get("ideas") or []:
        hay = " ".join(str(idea.get(k) or "") for k in ("match", "selection")).lower()
        if needle and needle in hay:
            out.append({k: idea.get(k) for k in
                        ("bucket", "match", "selection", "side",
                         "price_c", "model_c", "ev_pct", "size_usd")})
    return out


def _orderflow_context(orderflow, stage):
    """Taker-flow aggregates for the row's market category. Context ONLY."""
    note = "taker-side flow only; context — NEVER overrides the edge gate"
    if not isinstance(orderflow, dict):
        return {"category": STAGE_TO_FLOW_CATEGORY.get(stage), "n_trades": None,
                "usd": None, "buy_pressure": None, "smart_usd_share": None,
                "dumb_usd_share": None, "hot": None,
                "reason": "orderflow feed unavailable", "note": note}
    category = STAGE_TO_FLOW_CATEGORY.get(stage)
    row = None
    for cand in orderflow.get("category_matrix") or []:
        if cand.get("category") == category:
            row = cand
            break
    if row is None:
        return {"category": category, "n_trades": None, "usd": None,
                "buy_pressure": None, "smart_usd_share": None,
                "dumb_usd_share": None, "hot": None,
                "reason": "category %r not in orderflow category_matrix" % category,
                "note": note}
    bp = row.get("buy_pressure")
    return {
        "category": category,
        "n_trades": row.get("n_trades"),
        "usd": row.get("usd"),
        "buy_pressure": bp,
        "smart_usd_share": row.get("smart_usd_share"),
        "dumb_usd_share": row.get("dumb_usd_share"),
        "hot": (bp is not None and bp >= HOT_BUY_PRESSURE),
        "reason": None,
        "note": note,
    }


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


def _gate(passed, reason=None):
    return {"pass": bool(passed), "reason": None if passed else (reason or "failed")}


def _build_row(team_row, stage, *, advancement, bet_rec_idx, pm_ideas,
               orderflow, freshness):
    team = team_row.get("team")
    model_prob = _dig(team_row, ("model", stage))

    # PM price + fee-adjusted edge: advancement_data first, bet_recs fallback.
    # advancement_data's edge_adj is the BETTER-SIDE (YES or NO) fee-adjusted
    # edge but the feed drops the side column (wca.advancement.AdvancementEdge
    # keeps it; wca_advancement_data._pm_by_team_stage doesn't emit it), so the
    # side is re-implied here from sign(model - pm_yes_mid) and clearly
    # labelled as derived. bet_recs advancement_futures rows are YES buys.
    pm_price = edge_adj = None
    pm_src = edge_src = None
    pm_reason = None
    side = side_note = None
    position_prob = None
    position_prob_source = None
    pm_quote = _dig(team_row, ("pm", stage))
    br = bet_rec_idx.get(((team or "").lower(), stage))
    if isinstance(pm_quote, dict) and pm_quote.get("pm") is not None:
        pm_price = pm_quote.get("pm")
        edge_adj = pm_quote.get("edge_adj")
        pm_src = "advancement_data.teams[%s].pm[%s].pm (YES mid)" % (team, stage)
        edge_src = ("advancement_data.teams[%s].pm[%s].edge_adj "
                    "(better-side fee-adjusted edge)" % (team, stage))
        if model_prob is not None:
            side = "YES" if model_prob >= pm_price else "NO"
            position_prob = round(
                model_prob if side == "YES" else 1.0 - model_prob, 4)
            position_prob_source = (
                "= model_prob" if side == "YES"
                else "= 1 - model_prob (derived; NO side)")
            side_note = ("derived from sign(model - pm_yes_mid): "
                         "advancement_data omits the traded side")
    elif br is not None and br.get("pm_price") is not None:
        pm_price = br.get("pm_price")
        edge_adj = br.get("edge_adj")
        pm_src = "bet_recs.advancement_futures[%s].pm_price" % br.get("id")
        edge_src = "bet_recs.advancement_futures[%s].edge_adj" % br.get("id")
        side = "YES"
        side_note = "bet_recs advancement futures are YES buys"
        position_prob = model_prob
        position_prob_source = "= model_prob"
    else:
        pm_reason = ("no PM quote for %s/%s in advancement_data.teams[].pm or "
                     "bet_recs.advancement_futures" % (team, stage))

    # Bucket on the probability that the POSITION pays out (falls back to the
    # team's advancement prob when no side can be implied — no price → the
    # price_present gate already fails the row).
    bucket_prob = position_prob if position_prob is not None else model_prob
    bucket = prob_bucket(bucket_prob)
    flow = _orderflow_context(orderflow, stage)
    group_ctx = _group_context(advancement, team_row)
    ideas = _related_pm_ideas(pm_ideas, team or "")

    gates = {}
    gates["freshness"] = _gate(
        freshness["pass"],
        "stale/missing inputs: " + "; ".join(
            "%s (%s)" % (c["source"], c["reason"]) for c in freshness["checks"]
            if not c["pass"]))
    gates["price_present"] = _gate(pm_price is not None, pm_reason)
    gates["edge_positive"] = _gate(
        edge_adj is not None and edge_adj > 0,
        ("no fee-adjusted edge available" if edge_adj is None else
         "fee-adjusted edge %+0.4f <= 0 — DO_NOT_TRADE regardless of orderflow"
         % edge_adj))
    gates["min_prob_cash"] = _gate(
        bucket_prob is not None and bucket_prob >= 0.25,
        ("model probability unavailable" if bucket_prob is None else
         "position pays out at model %.0f%% < 25%% — no cash on longshots "
         "(likely-PnL rule; 0-for-12 in backtests)" % (bucket_prob * 100)))
    gates["clv_history"] = _gate(False, CLV_BLOCKER_REASON)

    # Verdict: candidate iff every gate except the standing CLV blocker passes.
    # The blocker keeps the whole desk shadow-only; it is surfaced, not waived.
    blocking = [n for n in ("freshness", "price_present", "edge_positive",
                            "min_prob_cash") if not gates[n]["pass"]]
    if blocking:
        verdict = "DO_NOT_TRADE"
        verdict_reasons = [gates[n]["reason"] for n in blocking]
    else:
        verdict = "SHADOW_CANDIDATE"
        verdict_reasons = ["all data gates pass; live money remains blocked by "
                           "the clv_history gate (see clv_history_blocker)"]

    return {
        "team": team,
        "group": team_row.get("group"),
        "stage": stage,
        "market": "advancement",
        "market_label": ("Win tournament · incl. ET+pens" if stage == "win"
                         else "Advance · incl. ET+pens"),
        "model_prob": model_prob,
        "model_source": "advancement_data.teams[%s].model[%s]" % (team, stage),
        "pm_price": pm_price,
        "pm_price_source": pm_src,
        "pm_price_reason": pm_reason,
        "edge_adj": edge_adj,
        "edge_source": edge_src,
        "side": side,
        "side_note": side_note,
        "position_prob": position_prob,
        "position_prob_source": position_prob_source,
        "bucket": bucket,
        "bet_rec": None if br is None else {
            "id": br.get("id"),
            "stake": br.get("stake"),
            "currency": br.get("currency"),
            "action_label": br.get("action_label"),
            "ev_net": br.get("ev_net"),
            "pm_fee": br.get("pm_fee"),
            "source": "bet_recs.advancement_futures[%s]" % br.get("id"),
        },
        "related_pm_ideas": {
            "n": None if ideas is None else len(ideas),
            "ideas": ideas or [],
            "reason": "pm_ideas feed unavailable" if ideas is None else None,
        },
        "orderflow": flow,
        "group_context": group_ctx,
        "gates": gates,
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
    }


def _sort_key(row):
    edge = row.get("edge_adj")
    return (
        _BUCKET_RANK.get(row.get("bucket"), 3),
        -(edge if edge is not None else float("-inf")),
        row.get("team") or "",
        row.get("stage") or "",
    )


def build_feed(advancement, bet_recs, pm_ideas, orderflow, *, generated,
               source_paths=None, load_errors=None):
    """Assemble the edge-desk payload from already-parsed feeds (pure)."""
    source_paths = source_paths or {k: DEFAULT_PATHS[k] for k in DEFAULT_PATHS}
    load_errors = load_errors or {}
    feeds = {"advancement": advancement, "bet_recs": bet_recs,
             "pm_ideas": pm_ideas, "orderflow": orderflow}
    now_dt = _parse_ts(generated) or datetime.now(timezone.utc)

    freshness = _freshness(feeds, source_paths, load_errors, now_dt)

    caveats = ["SHADOW-ONLY: decision-support feed; no verdict here is a trade "
               "instruction and nothing is wired to execution.",
               SETTLEMENT_NOTE,
               CLV_BLOCKER_REASON]
    for name in ("advancement", "bet_recs", "pm_ideas", "orderflow"):
        if feeds[name] is None:
            caveats.append("source unavailable: %s (%s)" % (
                source_paths[name], load_errors.get(name) or "not loaded"))
    for check in freshness["checks"]:
        if not check["pass"] and check["reason"] and "stale" in check["reason"]:
            caveats.append("freshness gate FAILED for %s — %s"
                           % (check["source"], check["reason"]))
    if isinstance(orderflow, dict):
        truncated = _dig(orderflow, ("window", "truncated_markets")) or []
        if truncated:
            caveats.append(
                "orderflow history truncated for n=%d markets (data-api 3k-row "
                "cap) — flow aggregates there are incomplete; buy-pressure/share "
                "numbers are context only" % len(truncated))
        caveats.append("orderflow is taker-side only: maker identities/flow are "
                       "unobserved; hot-flow tags can never override the edge gate")

    # Row universe: every (team, stage in meta.stages) still undecided
    # (0 < model < 1) in advancement_data. Decided legs (0.0 eliminated /
    # 1.0 already secured) are not tradable edges and are skipped.
    rows = []
    bet_rec_idx = _bet_rec_index(bet_recs)
    stages = _dig(advancement or {}, ("meta", "stages")) or []
    for team_row in (advancement or {}).get("teams") or []:
        for stage in stages:
            prob = _dig(team_row, ("model", stage))
            if prob is None or not (0.0 < prob < 1.0):
                continue
            rows.append(_build_row(
                team_row, stage, advancement=advancement,
                bet_rec_idx=bet_rec_idx, pm_ideas=pm_ideas,
                orderflow=orderflow, freshness=freshness))
    rows.sort(key=_sort_key)

    if advancement is None:
        caveats.append("advancement_data unavailable — row universe is EMPTY "
                       "(no fabricated rows)")

    n_candidates = sum(1 for r in rows if r["verdict"] == "SHADOW_CANDIDATE")
    tie_rows = sum(1 for r in rows if (r["group_context"] or {}).get("projected_tie"))
    if tie_rows:
        caveats.append("projected group-position tie flagged on n=%d rows — "
                       "check FIFA tiebreakers before trusting group-dependent "
                       "paths" % tie_rows)

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated,
            "shadow_only": True,
            "sources": {source_paths[k]: _stamp_of(k, feeds[k])
                        for k in ("advancement", "bet_recs", "pm_ideas", "orderflow")},
            "caveats": caveats,
            "n_rows": len(rows),
            "n_candidates": n_candidates,
            "verdict_enum": list(VERDICT_ENUM),
            "freshness_max_age_s": {source_paths[k]: FRESHNESS_MAX_AGE_S[k]
                                    for k in FRESHNESS_MAX_AGE_S},
            "ordering": ("prob-bucket on position payout prob (moneyline>=50c, "
                         "mid 25-50c, longshot <25c) then fee-adjusted edge "
                         "desc, nulls last; wca_pm_propose selection-rule "
                         "convention — render in feed order, do not re-sort"),
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
    """Load the four feeds from ``paths`` and build the payload."""
    feeds, errors, display = {}, {}, {}
    for name in ("advancement", "bet_recs", "pm_ideas", "orderflow"):
        feeds[name], errors[name] = load_json(paths[name])
        display[name] = _display_path(paths[name])
    return build_feed(
        feeds["advancement"], feeds["bet_recs"], feeds["pm_ideas"],
        feeds["orderflow"], generated=generated,
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
    parser.add_argument("--pm-ideas",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["pm_ideas"]))
    parser.add_argument("--orderflow",
                        default=os.path.join(_ROOT, DEFAULT_PATHS["orderflow"]))
    parser.add_argument("--out",
                        default=os.path.join(_ROOT, "site", "edge_desk.json"))
    parser.add_argument("--generated", default=None,
                        help="ISO-8601 UTC stamp for meta.generated_at + the "
                             "freshness clock (tests inject a fixed time)")
    args = parser.parse_args(argv)

    paths = {"advancement": args.advancement, "bet_recs": args.bet_recs,
             "pm_ideas": args.pm_ideas, "orderflow": args.orderflow}
    payload = generate(paths, generated=args.generated or _now_iso_z())
    _write_atomic(args.out, payload)
    meta = payload["meta"]
    print("edge_desk: wrote %s — %d rows, %d shadow candidates, freshness %s"
          % (args.out, meta["n_rows"], meta["n_candidates"],
             "PASS" if payload["freshness"]["pass"] else "FAIL"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
