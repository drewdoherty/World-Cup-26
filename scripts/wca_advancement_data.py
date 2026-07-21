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
# Sidecar recording the KO ties the cached sim was CONDITIONED on (its pinned
# set). The state-freshness gate compares reality against THIS — not against a
# fresh load_played_knockout_results(), which can know more than the cached sim
# did. A cache without the sidecar has unknown conditioning -> force a re-sim.
PINS_JSON = "data/advancement_sim_pins.json"
RESULTS_JSON = "data/processed/wc2026_results.json"
# Real-bracket source for the sim-consistency validator (fix 2026-07-13).
# DOCUMENTED CHOICE: site/scores_markets.json (built by
# scripts/wca_scores_markets_data.py from the same results spine + shootouts
# this script's sim conditioning uses, refreshed by the same publish job) —
# a plain local file, NO new network calls. Its knockout rows carry exactly
# what the validator needs: ``projected`` (False = both participants
# determined by real results, never model-inferred) and ``ft`` (None = not
# yet played), i.e. "two known real teams, not yet played".
SCORES_JSON = "site/scores_markets.json"
# scores_markets round list -> advancement.py stage code (round the tie is AT).
_KO_ROUND_STAGE = (("r32_games", "R32"), ("r16_games", "R16"),
                   ("qf_games", "QF"), ("sf_games", "SF"),
                   ("final_games", "F"))
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
    """Fit models then simulate current + pre-tournament tournaments.

    Returns ``(cur, pre, ko_pinned)`` — the pinned knockout set is surfaced so
    the caller can persist it (``PINS_JSON``) for the state-freshness gate.
    """
    from wca.card import DEFAULT_DC_LEVEL_TARGET, fit_models
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results

    # Same total-goals level anchor as the live card (fix 2026-07-08): the
    # unanchored fit's KO totals ran ~1.86 goals vs 2.70 realised, inflating
    # draws and understating favourites throughout the sim.
    models = fit_models(
        load_results(resolve_results_path()),
        dc_level_target=DEFAULT_DC_LEVEL_TARGET,
    )
    # Pin BOTH played group results (results=None auto-loads them) AND played
    # knockout ties incl. penalty-shootout winners (ko_results). Without the
    # latter the sim re-plays every knockout from scratch, so an eliminated team
    # keeps a large survival probability (e.g. Germany P(R16)=0.72 after losing
    # its R32 shootout) — the panel numbers must reflect real KO eliminations.
    ko_pinned = adv.load_played_knockout_results()
    cur = adv.run_advancement(models, ko_results=ko_pinned)
    pre = adv.run_advancement(models, results=[])   # pre-tournament baseline
    return cur, pre, ko_pinned


def _load_pins(path=PINS_JSON):
    """Cached-sim pinned KO set as ``{frozenset(pair): winner}``, or None.

    ``None`` = unknown conditioning (legacy cache predating the sidecar, or an
    unreadable file) — the caller treats that as a stale cache / fails closed.
    """
    try:
        raw = json.load(open(path))
        return {
            frozenset((str(a), str(b))): str(w)
            for a, b, w in (raw.get("ko_pinned") or [])
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _write_pins(ko_pinned, path=PINS_JSON):
    payload = {
        "generated": _now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "ko_pinned": sorted([sorted(pair) + [w] for pair, w in ko_pinned.items()]),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def _pm_by_team_stage(sim_df):
    """``{team: {stage: {pm, edge_adj, side, ask, stake_usd, path_scale}}}``
    from the live PM markets, plus the per-team path-exposure blocks.

    ``pm`` is the YES mid; ``edge_adj`` is the fee-adjusted edge of whichever
    side (YES/NO) the sim favours — so ``side`` names that side explicitly and
    ``ask`` is the executable buy price of that side that ``edge_adj`` was
    computed against (``AdvancementEdge.pm_price``). Without ``side`` a
    consumer must re-derive it from sign(model - mid), which mis-attributes
    against a stale-print mid (the Edge Desk's HIGH-2
    ``side_attribution_uncertain`` guard existed for exactly that) — emit it
    at the source instead. Additive fields: pm/edge_adj are unchanged.

    ``stake_usd`` / ``path_scale`` (fix 2026-07-08) carry the SIZING SOURCE's
    path-capped ¼-Kelly stake per rung: one team's nested advancement rungs
    (same side) are one correlated exposure, jointly capped by the tightest
    staked rung's ¼-Kelly (``wca.advancement.apply_path_exposure_caps``).
    Downstream sizers (wca_betrecs) treat ``stake_usd`` as a hard per-rung
    ceiling so the Action Desk can never re-stack the path independently.

    Returns ``(by_team, n_matched, path_exposure)`` where ``path_exposure`` is
    ``{team: {side: {total_stake_usd, cap_usd, scaling_applied, ...}}}`` in
    this feed's stage dialect (``Final``, not ``F``), for site/bot display.
    """
    try:
        pm_events = polymarket.find_world_cup_markets(include_closed=False)
        edges = adv.compare_to_polymarket(sim_df, pm_events)
    except Exception as exc:  # noqa: BLE001 — PM must never break the feed.
        print("polymarket pairing failed (%s); continuing" % exc, file=sys.stderr)
        return {}, 0, {}
    out = {}
    if edges is None or edges.empty:
        return out, 0, {}
    for _, e in edges.iterrows():
        st = _PM_STAGE.get(str(e["stage"]))
        if st is None:
            continue
        out.setdefault(str(e["team"]), {})[st] = {
            "pm": round(float(e["pm_yes_mid"]), 4),
            "edge_adj": round(float(e["fee_adj_edge"]), 4),
            "side": str(e["side"]),
            "ask": round(float(e["pm_price"]), 4),
            "stake_usd": round(float(e["stake"]), 2),
            "path_scale": round(float(e["path_scale"]), 4),
        }
    path_exposure = {}
    for team, sides in adv.path_exposure_summary(edges).items():
        blocks = {}
        for side, blk in sides.items():
            blk = dict(blk)
            blk["cap_stage"] = _PM_STAGE.get(blk["cap_stage"], blk["cap_stage"])
            blk["stages"] = [_PM_STAGE.get(s, s) for s in blk["stages"]]
            blocks[side] = blk
        path_exposure[str(team)] = blocks
    return out, int(len(edges)), path_exposure


def _undecided_bracket_ties(ko_pinned, scores_path=None):
    """``(ties, alive_teams, can_check_win_sum)`` from the scores feed.

    Reads :data:`SCORES_JSON` (see the constant for the documented
    bracket-source choice) and returns:

    * ``ties`` — ``(home, away, stage)`` for every NON-projected knockout row
      with ``ft=None``: two known real teams, not yet played (canonical
      names). Collected across ALL rounds, so a later-round tie whose feeders
      are both decided is checked even while its own round is mid-play.
    * ``alive_teams`` — non-projected R32 participants minus the losers of
      every decided knockout tie (decisive scoreline -> lower side; drawn ->
      the row's ``winner`` field, else the sim pin for that pair), or ``None``
      when some decided tie's winner cannot be determined.
    * ``can_check_win_sum`` — False exactly when ``alive_teams`` is ``None``
      (a drawn, unpinned tie: the alive set is indeterminable, and the
      state-staleness gate is already firing for that pair).

    Raises on a missing/unreadable scores feed — the caller stamps ok=None.
    """
    from wca.data.teamnames import canonical

    with open(scores_path or SCORES_JSON, encoding="utf-8") as fh:
        d = json.load(fh)

    ties = []
    alive = set()
    losers = set()
    can_win_sum = True
    for key, stage in _KO_ROUND_STAGE:
        for g in d.get(key) or []:
            if g.get("projected"):
                continue  # model-inferred matchup, not a real bracket tie
            home = canonical(str(g.get("home") or "").strip())
            away = canonical(str(g.get("away") or "").strip())
            if not home or not away:
                continue
            if stage == "R32":
                alive.add(home)
                alive.add(away)
            ft = g.get("ft")
            if not ft:
                ties.append((home, away, stage))
                continue
            # Decided tie: prune the loser from the alive set.
            try:
                hg, ag = (int(x) for x in str(ft).split("-", 1))
            except ValueError:
                can_win_sum = False
                continue
            if hg > ag:
                losers.add(away)
            elif ag > hg:
                losers.add(home)
            else:
                # Drawn 90' -> ET/pens: the row's winner field, else the pin.
                w = g.get("winner") or (ko_pinned or {}).get(
                    frozenset((home, away))
                )
                w = canonical(str(w)) if w else None
                if w == home:
                    losers.add(away)
                elif w == away:
                    losers.add(home)
                else:
                    can_win_sum = False  # winner unknown -> alive set unknown
    alive -= losers
    if not alive:
        can_win_sum = False
    return ties, (sorted(alive) if can_win_sum else None), can_win_sum


def _consistency_block(sim_df, ko_pinned, scores_path=None):
    """``meta.consistency`` block: {checked_ties, failures, ok, tolerance}.

    Runs :func:`wca.advancement.stage_prob_consistency` over the undecided
    real bracket ties. ``ok`` is ``None`` when the check could not run at all
    (scores feed missing/unreadable) — distinct from ``True`` (checked,
    clean) and ``False`` (checked, VIOLATED). A failed check never kills the
    feed here: it is stamped + warned; whether the inconsistent build may
    replace a consistent one is :func:`_refuse_inconsistent_overwrite`'s job.
    """
    try:
        ties, alive, can_win = _undecided_bracket_ties(
            ko_pinned, scores_path=scores_path
        )
        rep = adv.stage_prob_consistency(
            sim_df, ties, alive_teams=alive, check_win_sum=can_win
        )
    except Exception as exc:  # noqa: BLE001 — the check must never kill the feed.
        print("consistency check unavailable (%s); stamping ok=null" % exc,
              file=sys.stderr)
        return {"checked_ties": 0, "failures": [], "ok": None,
                "tolerance": adv.CONSISTENCY_DEFAULT_TOLERANCE,
                "error": str(exc)}
    if rep["ok"] is False:
        print(
            "WARNING: ADVANCEMENT SIM CONSISTENCY CHECK FAILED — %d of %d "
            "checked sums off 1.0 by more than %.3f: %s. The sim's bracket "
            "conditioning is suspect (unpinned upstream tie? check "
            "data/raw/shootouts.csv and %s); stage probabilities for these "
            "teams are contaminated."
            % (len(rep["failures"]), rep["checked_ties"] + (1 if rep["win_sum"] else 0),
               rep["tolerance"],
               "; ".join(
                   "%s @%s sum=%.4f" % ("+".join(f.get("teams", [])),
                                        f.get("stage", f.get("check", "?")),
                                        f.get("sum", float("nan")))
                   for f in rep["failures"]),
               PINS_JSON),
            file=sys.stderr,
        )
    return {"checked_ties": rep["checked_ties"], "failures": rep["failures"],
            "ok": rep["ok"], "tolerance": rep["tolerance"]}


def _refuse_inconsistent_overwrite(out_path, consistency):
    """True when this build must NOT overwrite the existing feed.

    Same bad-data-never-replaces-good-data principle as the PM-blind guard
    below (2026-07-03): refuse ONLY the ok:true -> ok:false transition — an
    existing feed that PASSED the consistency check is never clobbered by a
    build that FAILS it, unless ``WCA_ALLOW_INCONSISTENT=1``. A new ``ok`` of
    ``True``/``None`` (clean / could-not-check) never refuses, nor does an
    existing feed without a passing consistency stamp.
    """
    if not consistency or consistency.get("ok") is not False:
        return False
    if os.environ.get("WCA_ALLOW_INCONSISTENT") == "1":
        return False
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception:  # noqa: BLE001 — no/unreadable feed: write the honest one
        return False
    return (((existing.get("meta") or {}).get("consistency")) or {}).get(
        "ok"
    ) is True


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

    ko_pinned = _load_pins()
    cache_stale = recs is None or age is None or age[0] > args.max_age_hours
    if not cache_stale and ko_pinned is None:
        # Legacy cache without the pins sidecar: the sim's KO conditioning is
        # unknown, so the state-freshness gate cannot trust it. Re-run once
        # (the new cache writes the sidecar alongside).
        print("model cache lacks the pins sidecar (%s); re-running sim" % PINS_JSON)
        cache_stale = True

    if cache_stale:
        try:
            cur, pre, ko_pinned = _run_sim()
            recs = _sim_to_records(cur, pre)
            with open(MODEL_JSON, "w", encoding="utf-8") as fh:
                json.dump(recs, fh, indent=2)
            _write_pins(ko_pinned)
            model_generated = _now().strftime("%Y-%m-%d %H:%M UTC")
            print("re-ran advancement sim (%d teams, %d KO ties pinned)"
                  % (len(recs), len(ko_pinned)))
        except Exception as exc:  # noqa: BLE001
            print("sim failed (%s); using cached model probs" % exc, file=sys.stderr)
            if recs is None:
                print("no model data available; aborting", file=sys.stderr)
                return 1

    sim_df = _records_to_simdf(recs)
    pm, n_pm, path_exposure = _pm_by_team_stage(sim_df)
    groups = _group_tables()

    # State-freshness gate (2026-07-08): a team whose knockout tie has kicked
    # off but is NOT pinned in the sim's conditioning set has phantom stage
    # probabilities (the sim replays a decided tie — USA showed P(QF)=0.317
    # after its Jul-6 elimination). Stamp the reason per team so downstream
    # (wca_betrecs.build_advancement_futures) withholds instead of sizing.
    # ko_pinned=None (sim failed AND no sidecar) fails closed: every kicked-off
    # KO tie counts as unsettled.
    try:
        state_stale = adv.knockout_state_staleness(ko_pinned)
    except Exception as exc:  # noqa: BLE001 — the gate must never kill the feed.
        print("state-staleness scan failed (%s); continuing without"
              % exc, file=sys.stderr)
        state_stale = {}

    # Bracket-consistency validator (2026-07-13): output-side complement of the
    # input-side staleness gate above — for every undecided real knockout tie
    # the two teams' P(next stage) must sum to ~1, and P(win) over the alive
    # set must sum to ~1. A violation means the sim's conditioning silently
    # un-anchored (France showed P(SF)=0.5691 with its QF win pinned, because
    # the upstream Morocco–Netherlands R16 was missing from the pin set).
    # Stamped in meta + warned; never kills the feed (guard at write time).
    consistency = _consistency_block(sim_df, ko_pinned)

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
        entry = {
            "team": t, "group": r.get("group"),
            "model": model, "bucket": bucket, "delta": (delta or None),
            "pm": pm.get(t, {}),
        }
        # Same-team nested-path exposure (fix 2026-07-08): total path-capped
        # stake vs the tightest-rung ¼-Kelly cap, per traded side, so the
        # site/bot can display the correlated-path sizing explicitly.
        if t in path_exposure:
            entry["path_exposure"] = path_exposure[t]
        if t in state_stale:
            entry["state_stale_reason"] = state_stale[t]
        teams.append(entry)
    teams.sort(key=lambda x: -(x["model"].get("win") or 0.0))

    data = {
        "meta": {
            "generated": _now().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "model_generated": model_generated,
            "stages": STAGES,
            "n_pm_markets": n_pm,
            "n_ko_pinned": (None if ko_pinned is None else len(ko_pinned)),
            "n_state_stale": len(state_stale),
            "consistency": consistency,
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

    # NEVER clobber a CONSISTENT feed with an INCONSISTENT rebuild (2026-07-13,
    # same principle as the PM-blind guard above): an existing feed whose
    # meta.consistency.ok is true is kept when this build's check FAILED —
    # the failure usually means THIS host's conditioning inputs (shootouts /
    # results / pins) are stale, not that reality changed.
    if _refuse_inconsistent_overwrite(args.out, consistency):
        print(
            "%s: rebuild FAILS the sim-consistency check (%d failure(s): %s) "
            "but the existing feed passed it — KEEPING the existing feed. Fix "
            "the sim conditioning (shootouts/results/pins) and rebuild, or "
            "set WCA_ALLOW_INCONSISTENT=1 to force the overwrite."
            % (args.out, len(consistency.get("failures") or []),
               "; ".join(
                   "%s @%s sum=%s" % ("+".join(f.get("teams", [])),
                                      f.get("stage", f.get("check", "?")),
                                      f.get("sum"))
                   for f in (consistency.get("failures") or [])) or "n/a"),
            file=sys.stderr,
        )
        return 0

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print("%s: %d teams, %d groups, pm_markets=%d, model=%s"
          % (args.out, len(teams), len(groups), n_pm, model_generated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
