"""Conditional REMAINING-bracket forecast for the WC26 $2M perfect-bracket contest.

Phase 4 deliverable. The contest is CLOSED and we never submitted, but per the
user directive we build the strategy AS IF it were still open for the REMAINING
games: optimally COMPLETE the knockout bracket from the CURRENT state, conditioning
on results already decided.

Method
------
1. Build the project's real ``prob_fn`` via ``wca.advancement.make_prob_fn`` using
   the fitted Elo + Dixon-Coles models cached on disk (data/advancement_models.pkl).
   ``prob_fn(a, b, knockout=True) -> (p_a, p_draw, p_b)`` (90-minute triple).
2. Seed the CURRENT knockout state. Group stage is complete and the 16 R32 ties
   are FIXED fixtures (the group-stage outcome is fully encoded by who occupies
   each R32 slot). Four R32 ties are ALREADY PLAYED; their winners are fixed.
3. Forward Monte Carlo (>= 20000 sims) over every UNPLAYED tie from the current
   state to the Final. Each knockout tie's winner is drawn from prob_fn with the
   90-minute draw mass reallocated to a winner via the simulator's ET/pen model
   (p_a_total = p_a + p_draw * (0.5 + et_skill_weight*(q_a-0.5))). The full per-sim
   path is captured (winner of every match number).
4. From the path matrix: per-team conditional P(reach R16/QF/SF/F/Win | state),
   and per-remaining-tie conditional P(each side advances).
5. MAP completion: most-likely winner of each remaining slot (marginal-modal),
   compared against the true joint argmax (tractable here because the bracket
   factorises round-by-round given fixed R32 winners). p_perfect_remaining is the
   product of the modal pick probability along the realised MAP path.

This script is READ-ONLY w.r.t. production. It imports wca primitives and reads
models from disk; it writes ONLY under docs/research/wca_alpha_2026/data.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from wca.advancement import make_prob_fn
from wca.data.teamnames import canonical
from wca.sim.tournament2026 import KNOCKOUT_FEED

# ---------------------------------------------------------------------------
# Paths / config.
# ---------------------------------------------------------------------------
REPO = Path("/Users/andrewdoherty/Desktop/Coding/World Cup Alpha")
MODELS_PKL = REPO / "data" / "advancement_models.pkl"
# Research-local refit cache (the production pkl can be stale vs current EloRater).
LOCAL_MODELS_PKL = (
    REPO / "docs" / "research" / "wca_alpha_2026" / "data" / "_refit_models.pkl"
)
SANITY_JSON = REPO / "data" / "advancement_current_vs_pretournament.json"
OUT_DIR = REPO / "docs" / "research" / "wca_alpha_2026" / "data"
OUT_CSV = OUT_DIR / "conditional_bracket_probs.csv"
OUT_JSON = OUT_DIR / "remaining_ties_probs.json"

N_SIMS = 50000
SEED = 20260630
ET_SKILL_WEIGHT = 0.5  # matches TournamentSimulator default

# ---------------------------------------------------------------------------
# CURRENT R32 STATE (web-sourced as of 2026-06-30; NOT in repo data).
# Each R32 match number 73-88 -> (team_a, team_b). team_a is the nominal "home"
# side passed to prob_fn (only the ratio matters; the sim normalises).
# Mapping of fixtures -> match numbers verified against R32_TIES group-slot specs
# (see the script's accompanying analysis): every fixture maps to exactly one
# slot by group identity.
# ---------------------------------------------------------------------------
R32_FIXTURES: Dict[int, Tuple[str, str]] = {
    73: ("South Africa", "Canada"),          # R-A v R-B   PLAYED: Canada
    74: ("Germany", "Paraguay"),             # W-E v T-E   PLAYED: Paraguay (GER OUT)
    75: ("Netherlands", "Morocco"),          # W-F v R-C   PLAYED: Morocco (NED OUT)
    76: ("Brazil", "Japan"),                 # W-C v R-F   PLAYED: Brazil (JPN OUT)
    77: ("France", "Sweden"),                # W-I v T-I
    78: ("Ivory Coast", "Norway"),           # R-E v R-I
    79: ("Mexico", "Ecuador"),               # W-A v T-A
    80: ("England", "DR Congo"),             # W-L v T-L
    81: ("United States", "Bosnia and Herzegovina"),  # W-D v T-D
    82: ("Belgium", "Senegal"),              # W-G v T-G
    83: ("Portugal", "Croatia"),             # R-K v R-L
    84: ("Spain", "Austria"),                # W-H v R-J
    85: ("Switzerland", "Algeria"),          # W-B v T-B
    86: ("Argentina", "Cape Verde"),         # W-J v R-H
    87: ("Colombia", "Ghana"),               # W-K v T-K
    88: ("Australia", "Egypt"),              # R-D v R-G
}

# R32 ties already PLAYED -> the team that ADVANCED (graded pick).
R32_PLAYED_WINNERS: Dict[int, str] = {
    73: "Canada",
    74: "Paraguay",
    75: "Morocco",
    76: "Brazil",
}

# Round label for each downstream match number (R32 winners feed these).
MATCH_ROUND: Dict[int, str] = {}
for _m in range(89, 97):
    MATCH_ROUND[_m] = "R16"
for _m in range(97, 101):
    MATCH_ROUND[_m] = "QF"
for _m in (101, 102):
    MATCH_ROUND[_m] = "SF"
MATCH_ROUND[104] = "F"

# Reverse feed: match_no -> (src_a, src_b). Excludes 103 (3rd-place playoff).
FEED: Dict[int, Tuple[int, int]] = {mno: (a, b) for (mno, a, b) in KNOCKOUT_FEED}


def load_models():
    """Load fitted Elo+DC models.

    Prefer the production cache (data/advancement_models.pkl). If it fails to
    unpickle cleanly against the CURRENT code (e.g. an EloRater attribute added
    since the cache was written), refit fresh and cache to a RESEARCH-LOCAL pkl
    so production data is never modified. The refit reproduces the same fit
    (``wca.card.fit_models``) the production cache was built from.
    """
    # Try production cache first, validating it actually works with current code.
    try:
        with MODELS_PKL.open("rb") as fh:
            models = pickle.load(fh)
        # Smoke-test: a single prob_fn call exercises the EloRater path.
        _pf = make_prob_fn(models)
        _pf("Brazil", "Argentina", True)
        print("Using production model cache:", MODELS_PKL)
        return models
    except Exception as exc:  # noqa: BLE001
        print(f"Production cache unusable vs current code ({exc!r}); refitting.")

    if LOCAL_MODELS_PKL.exists():
        try:
            with LOCAL_MODELS_PKL.open("rb") as fh:
                models = pickle.load(fh)
            _pf = make_prob_fn(models)
            _pf("Brazil", "Argentina", True)
            print("Using research-local refit cache:", LOCAL_MODELS_PKL)
            return models
        except Exception:  # noqa: BLE001
            pass

    from wca.card import fit_models
    from wca.data.cleaning import resolve_results_path
    from wca.data.results import load_results

    print("Refitting Elo + Dixon-Coles (~2 min)…")
    results = load_results(resolve_results_path())
    models = fit_models(results)
    with LOCAL_MODELS_PKL.open("wb") as fh:
        pickle.dump(models, fh)
    print("Cached research-local refit to", LOCAL_MODELS_PKL)
    return models


def canon_fixtures() -> Dict[int, Tuple[str, str]]:
    return {m: (canonical(a), canonical(b)) for m, (a, b) in R32_FIXTURES.items()}


def p_a_advance(prob_fn, a: str, b: str) -> float:
    """Conditional P(team_a advances) in a knockout tie under the ET/pen model."""
    pa, pdr, pb = prob_fn(a, b, True)
    s = pa + pdr + pb
    pa, pdr, pb = pa / s, pdr / s, pb / s
    decisive = pa + pb
    qa = 0.5 if decisive <= 0 else pa / decisive
    p_et_a = 0.5 + ET_SKILL_WEIGHT * (qa - 0.5)
    return pa + pdr * p_et_a


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    models = load_models()
    # No odds_df / market anchoring: knockout ties have no tradable book here and
    # prob_fn ignores market for knockout=True anyway. Use the model blend.
    prob_fn = make_prob_fn(models)

    fixtures = canon_fixtures()
    rng = np.random.default_rng(SEED)

    # ----- Seed R32 winners per sim --------------------------------------
    # winners_by_match[mno] -> np.ndarray of canonical team names, shape (N_SIMS,)
    winners: Dict[int, np.ndarray] = {}
    # Per-remaining-tie advance prob cache for reporting (R32 unplayed + downstream).
    tie_records: List[dict] = []

    for mno, (a, b) in fixtures.items():
        if mno in R32_PLAYED_WINNERS:
            w = canonical(R32_PLAYED_WINNERS[mno])
            winners[mno] = np.full(N_SIMS, w, dtype=object)
        else:
            p = p_a_advance(prob_fn, a, b)
            u = rng.random(N_SIMS)
            winners[mno] = np.where(u < p, a, b)
            tie_records.append(
                {
                    "round": "R32",
                    "match": mno,
                    "team_a": a,
                    "team_b": b,
                    "p_a_advance": round(float(p), 6),
                    "p_b_advance": round(float(1.0 - p), 6),
                    "map_pick": a if p >= 0.5 else b,
                    "p_map_pick": round(float(max(p, 1.0 - p)), 6),
                    "status": "to_play",
                }
            )

    # ----- Walk the feed for R16 -> Final --------------------------------
    # For each downstream match we resolve participants per sim, then draw the
    # winner using a per-unique-pair vectorised advance probability.
    for mno, (sa, sb) in FEED.items():
        ta = winners[sa]
        tb = winners[sb]
        out = np.empty(N_SIMS, dtype=object)
        # Group sims by the unordered/ordered (a,b) pair to reuse prob_fn.
        u = rng.random(N_SIMS)
        # Build a pair key; keep ordered (a as nominal home) for prob_fn direction.
        pair_keys = np.char.add(np.char.add(ta.astype(str), "|"), tb.astype(str))
        for key in np.unique(pair_keys):
            mask = pair_keys == key
            a_name, b_name = key.split("|", 1)
            p = p_a_advance(prob_fn, a_name, b_name)
            out[mask] = np.where(u[mask] < p, a_name, b_name)
        winners[mno] = out

    # ----- Per-team conditional reach probabilities ----------------------
    # A team "reaches round R" if it is a participant in any match of round R.
    # Participants of round R = winners of the matches feeding round R.
    # Equivalent: team reaches R16 if it won its R32 tie; QF if won its R16 tie;
    # etc.; Win if it won match 104.
    alive_teams = sorted(
        {t for m in fixtures for t in fixtures[m]}
    )
    reach_counts = {
        stage: {t: 0 for t in alive_teams}
        for stage in ("R16", "QF", "SF", "F", "Win")
    }

    # R32 winners -> reached R16.
    for mno in fixtures:
        for w in winners[mno]:
            pass  # replaced by vectorised below

    def count_reach(stage_winner_matches, stage_label):
        # union over matches: a team reaching the stage is a winner of one of the
        # feeder matches. Since each sim a team appears in at most one feeder, sum.
        for mno in stage_winner_matches:
            vals, cnts = np.unique(winners[mno], return_counts=True)
            for v, c in zip(vals, cnts):
                if v in reach_counts[stage_label]:
                    reach_counts[stage_label][v] += int(c)

    # Reached R16 = won R32 (matches 73-88).
    count_reach(list(fixtures.keys()), "R16")
    # Reached QF = won R16 (89-96).
    count_reach(list(range(89, 97)), "QF")
    # Reached SF = won QF (97-100).
    count_reach(list(range(97, 101)), "SF")
    # Reached Final = won SF (101-102).
    count_reach([101, 102], "F")
    # Win = won Final (104).
    count_reach([104], "Win")

    # ----- Downstream tie conditional probs (for the JSON table) ----------
    # For each downstream match, report the marginal P(each distinct participant
    # advances) integrated over the sim (this is the conditional that matters for
    # picking that slot given the current state).
    for mno, (sa, sb) in FEED.items():
        ta = winners[sa]
        tb = winners[sb]
        wn = winners[mno]
        # Marginal advance prob per team that ever appears in this slot.
        appear = np.unique(np.concatenate([ta, tb]))
        adv = {}
        for t in appear:
            in_match = (ta == t) | (tb == t)
            n_in = int(in_match.sum())
            if n_in == 0:
                continue
            n_adv = int(((wn == t) & in_match).sum())
            adv[str(t)] = {
                "p_in_match": round(n_in / N_SIMS, 6),
                "p_advance_given_in": round(n_adv / n_in, 6),
                "p_advance_uncond": round(n_adv / N_SIMS, 6),
            }
        # MAP pick for this slot = team with highest unconditional advance prob.
        map_pick = max(adv.items(), key=lambda kv: kv[1]["p_advance_uncond"])[0]
        tie_records.append(
            {
                "round": MATCH_ROUND[mno],
                "match": mno,
                "feeds_from": [sa, sb],
                "participants": adv,
                "map_pick": map_pick,
                "p_map_pick_uncond": adv[map_pick]["p_advance_uncond"],
                "status": "downstream",
            }
        )

    # ----- Build per-team CSV --------------------------------------------
    rows = []
    for t in alive_teams:
        played_out = False
        rows.append(
            {
                "team": t,
                "P(R16)": round(reach_counts["R16"][t] / N_SIMS, 6),
                "P(QF)": round(reach_counts["QF"][t] / N_SIMS, 6),
                "P(SF)": round(reach_counts["SF"][t] / N_SIMS, 6),
                "P(Final)": round(reach_counts["F"][t] / N_SIMS, 6),
                "P(Win)": round(reach_counts["Win"][t] / N_SIMS, 6),
            }
        )
    df = pd.DataFrame(rows).sort_values("P(Win)", ascending=False).reset_index(drop=True)
    df.to_csv(OUT_CSV, index=False)

    # ----- MAP completion + p_perfect_remaining --------------------------
    # Marginal-modal MAP: pick the modal winner of each REMAINING tie.
    # For unplayed R32 ties: modal = higher single-tie advance prob.
    # For downstream ties: the slot is filled by uncertain participants, so the
    # "pick" in a bracket pool is a TEAM NAME chosen up front. The modal team for
    # that slot is the one with highest unconditional advance prob through it.
    #
    # The joint argmax over the whole remaining bracket, given fixed R32 winners,
    # factorises: each downstream slot's best up-front team pick is the team that
    # maximises P(that team occupies AND wins the slot) = p_advance_uncond, and
    # the R32 picks are independent single ties. We therefore build the MAP bracket
    # greedily and verify consistency (the chain of picks must be a valid path:
    # the team picked at QF must be one of the two R16 picks feeding it, etc.).

    map_bracket: List[dict] = []
    pick_by_match: Dict[int, str] = {}

    # R32 picks (only unplayed ones are "remaining"; played ones are fixed truth).
    r32_pick: Dict[int, str] = {}
    for mno, (a, b) in fixtures.items():
        if mno in R32_PLAYED_WINNERS:
            r32_pick[mno] = canonical(R32_PLAYED_WINNERS[mno])
        else:
            rec = next(r for r in tie_records if r.get("match") == mno and r["round"] == "R32")
            r32_pick[mno] = rec["map_pick"]
            map_bracket.append(
                {
                    "round": "R32",
                    "tie": f"M{mno}: {a} vs {b}",
                    "pick": rec["map_pick"],
                    "p_pick": rec["p_map_pick"],
                    "note": "single tie, to play",
                }
            )
    pick_by_match.update(r32_pick)

    # Downstream picks: build by round, choosing the modal team for the slot but
    # enforcing bracket consistency (pick must be one of the two feeder picks).
    # We compute, for each downstream match, the conditional advance prob of EACH
    # of its two FEEDER picks, and choose the higher -> this is the true joint MAP
    # along the modal path (greedy is optimal here because the path is a tree and
    # each node's pick only needs to beat its sibling subtree's pick).
    def slot_pick_prob(mno: int) -> Tuple[str, float]:
        sa, sb = FEED[mno]
        cand_a = pick_by_match[sa]
        cand_b = pick_by_match[sb]
        # Conditional advance prob of cand_a vs cand_b in this slot.
        p_a = p_a_advance(prob_fn, cand_a, cand_b)
        if p_a >= 0.5:
            return cand_a, p_a
        return cand_b, 1.0 - p_a

    for mno in [89, 90, 91, 92, 93, 94, 95, 96,
                97, 98, 99, 100, 101, 102, 104]:
        pick, p = slot_pick_prob(mno)
        pick_by_match[mno] = pick
        sa, sb = FEED[mno]
        map_bracket.append(
            {
                "round": MATCH_ROUND[mno],
                "tie": f"M{mno}: W{sa} vs W{sb}",
                "pick": pick,
                "p_pick": round(float(p), 6),
                "note": f"feeder picks {pick_by_match[sa]} / {pick_by_match[sb]}",
            }
        )

    # p_perfect_remaining (modal-path product): product of the modal pick prob at
    # every REMAINING decision node along the MAP path. Remaining nodes = 12
    # unplayed R32 ties + 15 downstream ties (89..96,97..100,101,102,104).
    # For downstream nodes use the CONDITIONAL prob that the picked team beats the
    # picked sibling (slot_pick_prob), which is the probability the MAP pick is
    # correct GIVEN the upstream MAP picks all held.
    log_p = 0.0
    factors = []
    for mno, (a, b) in fixtures.items():
        if mno in R32_PLAYED_WINNERS:
            continue
        rec = next(r for r in tie_records if r.get("match") == mno and r["round"] == "R32")
        log_p += np.log(rec["p_map_pick"])
        factors.append((f"R32 M{mno}", rec["p_map_pick"]))
    for mno in [89, 90, 91, 92, 93, 94, 95, 96,
                97, 98, 99, 100, 101, 102, 104]:
        _, p = slot_pick_prob(mno)
        log_p += np.log(p)
        factors.append((f"{MATCH_ROUND[mno]} M{mno}", float(p)))
    p_perfect_modal_path = float(np.exp(log_p))

    # Empirical p_perfect_remaining from the sim: fraction of sims in which EVERY
    # remaining match winner equals the MAP bracket pick for that match.
    correct = np.ones(N_SIMS, dtype=bool)
    for mno, (a, b) in fixtures.items():
        if mno in R32_PLAYED_WINNERS:
            continue
        correct &= (winners[mno] == r32_pick[mno])
    for mno in [89, 90, 91, 92, 93, 94, 95, 96,
                97, 98, 99, 100, 101, 102, 104]:
        correct &= (winners[mno] == pick_by_match[mno])
    p_perfect_empirical = float(correct.mean())

    # ----- Sanity check vs repo JSON -------------------------------------
    sanity = json.loads(SANITY_JSON.read_text())
    sanity_by_team = {canonical(r["team"]): r for r in sanity}
    sanity_rows = []
    for t in alive_teams:
        ref = sanity_by_team.get(t)
        if ref is None:
            continue
        sanity_rows.append(
            {
                "team": t,
                "cond_R16": reach_counts["R16"][t] / N_SIMS,
                "repo_R16": ref.get("P(R16)"),
                "cond_QF": reach_counts["QF"][t] / N_SIMS,
                "repo_QF": ref.get("P(QF)"),
                "cond_Win": reach_counts["Win"][t] / N_SIMS,
                "repo_Win": ref.get("P(win)"),
            }
        )

    # ----- Write JSON table ----------------------------------------------
    out = {
        "method": "seeded forward Monte Carlo with full path capture; prob_fn from "
        "fitted Elo+DC (data/advancement_models.pkl); ET/pen draw reallocation "
        f"(et_skill_weight={ET_SKILL_WEIGHT}); n_sims={N_SIMS}; seed={SEED}",
        "current_state": {
            "r32_played_winners": R32_PLAYED_WINNERS,
            "r32_to_play": [
                f"M{m}: {a} vs {b}"
                for m, (a, b) in fixtures.items()
                if m not in R32_PLAYED_WINNERS
            ],
        },
        "remaining_ties": tie_records,
        "map_completion": map_bracket,
        "p_perfect_remaining_modal_path": round(p_perfect_modal_path, 12),
        "p_perfect_remaining_empirical": round(p_perfect_empirical, 12),
        "n_sims": N_SIMS,
        "seed": SEED,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2))

    # ----- Console summary -----------------------------------------------
    print("=== Conditional remaining-bracket forecast ===")
    print(f"n_sims={N_SIMS} seed={SEED} et_skill_weight={ET_SKILL_WEIGHT}")
    print(f"\nWrote {OUT_CSV}")
    print(f"Wrote {OUT_JSON}")
    print("\nTop conditional title favorites P(Win | state):")
    print(df.head(10).to_string(index=False))
    print("\n=== MAP completion (remaining) ===")
    for r in map_bracket:
        print(f"  [{r['round']:>3}] {r['tie']:<40} -> {r['pick']:<22} p={r['p_pick']:.4f}")
    print(f"\np_perfect_remaining (modal-path product) = {p_perfect_modal_path:.6e}")
    print(f"p_perfect_remaining (empirical from sim)  = {p_perfect_empirical:.6e}")
    print("\n=== Sanity check vs repo JSON (alive teams, sample) ===")
    sdf = pd.DataFrame(sanity_rows).sort_values("cond_Win", ascending=False)
    print(sdf.head(12).to_string(index=False))


if __name__ == "__main__":
    main()
