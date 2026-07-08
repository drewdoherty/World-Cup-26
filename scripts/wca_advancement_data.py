#!/usr/bin/env python
"""Build site/advancement_data.json for the Visuals tournament-progression panel.

Per team it emits: the model Monte-Carlo stage probabilities (reach
R32/R16/QF/SF/Final/win, plus group winner), the matching live Polymarket implied
probability + fee-adjusted edge, and — separately — the 12 full group-stage
tables.

The model sim (fit Elo+DC then simulate, ~2-3 min, dominated by the fit) is the
slow part, so it is cached in ``data/advancement_current_vs_pretournament.json``
and only re-run when that cache is older than ``--max-age-hours`` (default 12).
Polymarket (free public Gamma API) and the group standings are recomputed every
run. Designed to run in the hourly publish: a cached run is ~10s.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pandas as pd  # noqa: E402

from wca import advancement as adv  # noqa: E402
from wca import tracking  # noqa: E402
from wca.data import polymarket  # noqa: E402

MODEL_JSON = "data/advancement_current_vs_pretournament.json"
RESULTS_JSON = "data/processed/wc2026_results.json"
# Progression stages shown on the chart x-axis (group winner kept separate).
STAGES = ["R32", "R16", "QF", "SF", "Final", "win"]
_COL = {
    "R32": "P(R32)", "R16": "P(R16)", "QF": "P(QF)", "SF": "P(SF)",
    "Final": "P(Final)", "win": "P(win)", "group_winner": "P(group_winner)",
}
# Polymarket stage codes (advancement.py) -> our keys.
_PM_STAGE = {"R32": "R32", "R16": "R16", "QF": "QF", "SF": "SF",
             "F": "Final", "win": "win", "GW": "group_winner"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _mtime_hours(path):
    if not os.path.exists(path):
        return None
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
    return (_now() - ts).total_seconds() / 3600.0, ts


def _records_to_simdf(recs):
    rows = []
    for r in recs:
        row = {"team": r.get("team"), "group": r.get("group")}
        for col in _COL.values():
            row[col] = r.get(col)
        rows.append(row)
    return pd.DataFrame(rows).set_index("team")


def _sim_to_records(cur, pre):
    out = []
    for team in cur.index:
        r = {"team": team, "group": str(cur.loc[team, "group"])}
        for col in _COL.values():
            v = float(cur.loc[team, col])
            r[col] = round(v, 4)
            if pre is not None and team in pre.index:
                r[col + "_delta"] = round(v - float(pre.loc[team, col]), 4)
        out.append(r)
    return out


def _run_sim():
    """Fit models then simulate current + pre-tournament tournaments."""
    from wca.card import fit_models
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results

    models = fit_models(load_results(resolve_results_path()))
    # Pin BOTH played group results (results=None auto-loads them) AND played
    # knockout ties incl. penalty-shootout winners (ko_results). Without the
    # latter the sim re-plays every knockout from scratch, so an eliminated team
    # keeps a large survival probability (e.g. Germany P(R16)=0.72 after losing
    # its R32 shootout) — the panel numbers must reflect real KO eliminations.
    cur = adv.run_advancement(models, ko_results=adv.load_played_knockout_results())
    pre = adv.run_advancement(models, results=[])   # pre-tournament baseline
    return cur, pre


def _pm_by_team_stage(sim_df):
    """``{team: {stage: {pm, edge_adj, side, ask}}}`` from the live PM markets.

    ``pm`` is the YES mid; ``edge_adj`` is the fee-adjusted edge of whichever
    side (YES/NO) the sim favours — so ``side`` names that side explicitly and
    ``ask`` is the executable buy price of that side that ``edge_adj`` was
    computed against (``AdvancementEdge.pm_price``). Without ``side`` a
    consumer must re-derive it from sign(model - mid), which mis-attributes
    against a stale-print mid (the Edge Desk's HIGH-2
    ``side_attribution_uncertain`` guard existed for exactly that) — emit it
    at the source instead. Additive fields: pm/edge_adj are unchanged.
    """
    try:
        pm_events = polymarket.find_world_cup_markets(include_closed=False)
        edges = adv.compare_to_polymarket(sim_df, pm_events)
    except Exception as exc:  # noqa: BLE001 — PM must never break the feed.
        print("polymarket pairing failed (%s); continuing" % exc, file=sys.stderr)
        return {}, 0
    out = {}
    if edges is None or edges.empty:
        return out, 0
    for _, e in edges.iterrows():
        st = _PM_STAGE.get(str(e["stage"]))
        if st is None:
            continue
        out.setdefault(str(e["team"]), {})[st] = {
            "pm": round(float(e["pm_yes_mid"]), 4),
            "edge_adj": round(float(e["fee_adj_edge"]), 4),
            "side": str(e["side"]),
            "ask": round(float(e["pm_price"]), 4),
        }
    return out, int(len(edges))


def _group_tables():
    try:
        results = json.load(open(RESULTS_JSON)).get("results", [])
    except (OSError, json.JSONDecodeError):
        results = []
    st = tracking.compute_group_standings(results)
    groups = {}
    for g in sorted(adv.WC2026_GROUPS):
        teams = [{"team": t, **st[t]} for t in st if st[t].get("group") == g]
        teams.sort(key=lambda x: x.get("position", 9))
        groups[g] = [
            {"pos": x["position"], "team": x["team"], "p": x["played"],
             "w": x["won"], "d": x["drawn"], "l": x["lost"], "gf": x["gf"],
             "ga": x["ga"], "gd": x["gd"], "pts": x["points"]}
            for x in teams
        ]
    return groups


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the advancement site feed.")
    ap.add_argument("--out", default="site/advancement_data.json")
    ap.add_argument("--max-age-hours", type=float, default=12.0,
                    help="Re-run the Monte-Carlo sim when the model cache is older.")
    args = ap.parse_args(argv)

    recs = None
    model_generated = None
    age = _mtime_hours(MODEL_JSON)
    if age is not None:
        try:
            recs = json.load(open(MODEL_JSON))
            model_generated = age[1].strftime("%Y-%m-%d %H:%M UTC")
        except (OSError, json.JSONDecodeError):
            recs = None

    if recs is None or age is None or age[0] > args.max_age_hours:
        try:
            cur, pre = _run_sim()
            recs = _sim_to_records(cur, pre)
            with open(MODEL_JSON, "w", encoding="utf-8") as fh:
                json.dump(recs, fh, indent=2)
            model_generated = _now().strftime("%Y-%m-%d %H:%M UTC")
            print("re-ran advancement sim (%d teams)" % len(recs))
        except Exception as exc:  # noqa: BLE001
            print("sim failed (%s); using cached model probs" % exc, file=sys.stderr)
            if recs is None:
                print("no model data available; aborting", file=sys.stderr)
                return 1

    sim_df = _records_to_simdf(recs)
    pm, n_pm = _pm_by_team_stage(sim_df)
    groups = _group_tables()

    teams = []
    for r in recs:
        t = r["team"]
        model = {st: r.get(col) for st, col in _COL.items()}
        # Canonical model-prob bucket per stage (wca.selection): drives the
        # server-side no-cash gate + greying in adv_edge_matrix.js so the client
        # never has to re-derive the <25c longshot floor.
        bucket = {st: adv.prob_bucket(model.get(st)) for st in model}
        delta = {st: r[col + "_delta"] for st, col in _COL.items()
                 if (col + "_delta") in r}
        teams.append({
            "team": t, "group": r.get("group"),
            "model": model, "bucket": bucket, "delta": (delta or None),
            "pm": pm.get(t, {}),
        })
    teams.sort(key=lambda x: -(x["model"].get("win") or 0.0))

    data = {
        "meta": {
            "generated": _now().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "model_generated": model_generated,
            "stages": STAGES,
            "n_pm_markets": n_pm,
        },
        "teams": teams,
        "groups": groups,
    }
    # NEVER clobber a PM-aware feed with a PM-blind rebuild (2026-07-03): a
    # host that cannot reach Polymarket (the mini's network block) used to
    # overwrite a good committed feed with n_pm_markets=0, silently killing
    # every advancement rec downstream. Same principle as the card cache's
    # empty-result guard: bad data never replaces good data.
    if n_pm == 0:
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
            if (existing.get("meta") or {}).get("n_pm_markets"):
                print(
                    "%s: rebuild is PM-BLIND (n_pm_markets=0) but the existing "
                    "feed has %s live markets — KEEPING the existing feed "
                    "(fix PM reachability on this host, or rebuild where PM "
                    "is reachable)."
                    % (args.out, (existing.get("meta") or {}).get("n_pm_markets"))
                )
                return 0
        except Exception:
            pass  # no existing/unreadable feed -> write the honest blind one

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print("%s: %d teams, %d groups, pm_markets=%d, model=%s"
          % (args.out, len(teams), len(groups), n_pm, model_generated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
