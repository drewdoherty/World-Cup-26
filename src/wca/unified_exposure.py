"""Unified correlated exposure across the card (match-result) and advancement book.

An advancement bet ("Brazil reach SF") is a *parlay of match results*, so it is
positively correlated with (a) the team's shallower advancement legs (R16 ⊂ QF ⊂
SF ⊂ Final ⊂ win are nested — backing two is ~one oversized bet), and (b) any
match-result bet on the same team. The card (blend 1X2) and the MC advancement
book were sized in isolation, so nothing stopped the book holding "France SF"
*and* a card "France win" as if independent — they lose together.

This layer nets BOTH books into a single per-team directional ("long the team")
exposure and sizes new bets against that joint book, with a hard whole-book
deploy ceiling so a buffer is always retained.

Pure / deterministic: numpy-free, stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Advancement ladder, shallow -> deep. A deeper YES bet implies all shallower ones.
LADDER = ("group_winner", "R32", "R16", "QF", "SF", "Final", "win")


@dataclass
class Position:
    team: str
    stage: str           # a LADDER stage, "match", "outright"(=win), "fade", "xs"
    stake: float
    price: float         # entry implied prob (1/odds) — for reference
    kind: str            # "adv" | "match" | "fade" | "xs" | "outright"


def _is_long(p: Position) -> bool:
    """A bet that pays off when the team does *well* (correlated long)."""
    return p.kind in ("adv", "match", "outright")


def team_long_exposure(positions: Sequence[Position]) -> Dict[str, float]:
    """Per-team correlated 'long' exposure: $ that all lose together if the team flops.

    Sums same-direction (long) stakes per team — the honest worst-case downside,
    since a team crashing out voids its whole advancement ladder *and* its
    match-win bets at once. Fades / exact-scores are not part of a team's long.
    """
    out: Dict[str, float] = {}
    for p in positions:
        if _is_long(p):
            out[p.team] = out.get(p.team, 0.0) + p.stake
    return out


def implied_ko_wins(stage: str) -> Optional[int]:
    """How many knockout wins a 'reach <stage>' YES bet implies (rough, 48-team WC)."""
    order = {"R32": 0, "R16": 1, "QF": 2, "SF": 3, "Final": 4, "win": 5}
    return order.get(stage)


def shrink_q(q: float, p: float) -> float:
    """Shrink the model probability toward the market, harder for longshots.

    Liquid PM sports markets are near-calibrated (Le 2026), so the market price is
    a strong fair-value anchor and a big model-vs-market gap on a low-p outcome is
    usually model error. Weight on the model rises with p (favourites trusted more):
    ``q_shrunk = w*q + (1-w)*p``, ``w = clip(0.25 + 0.75*p, 0.25, 0.9)``.
    """
    w = min(0.9, max(0.25, 0.25 + 0.75 * p))
    return w * q + (1.0 - w) * p


def edge_floor(p: float) -> float:
    """Minimum (shrunk) edge required to act, rising steeply as p falls.

    Estimation-error convexity and the favourite-longshot bias are worst in the
    low-probability tail, and there's no convergence to bail out a hold-to-
    resolution bet — so demand far more edge on longshots than on favourites.
    """
    if p >= 0.55:
        return 0.05
    if p >= 0.35:
        return 0.09
    if p >= 0.20:
        return 0.15
    return 1.0  # effectively prohibitive below 0.20


def screen(model: float, pm: float) -> Dict[str, object]:
    """Literature-grounded screen for one buy-YES candidate.

    Returns ``{verdict, q_shrunk, edge_shrunk, reason}`` with verdict in
    {"PASS","DROP","NO_EDGE"}. Hard-drops the overpriced-longshot trap (low p with
    the model at a large multiple of the line), then requires the shrunk edge to
    clear the probability-scaled floor.
    """
    if model <= pm:
        return {"verdict": "NO_EDGE", "q_shrunk": model, "edge_shrunk": model - pm,
                "reason": "model<=market (not a buy)"}
    if pm < 0.20 and model >= 1.8 * pm:
        return {"verdict": "DROP", "q_shrunk": shrink_q(model, pm), "edge_shrunk": None,
                "reason": "overpriced-longshot trap: p<0.20 and model>=1.8x line (likely model error)"}
    qs = shrink_q(model, pm)
    e = qs - pm
    floor = edge_floor(pm)
    if e < floor:
        return {"verdict": "DROP", "q_shrunk": qs, "edge_shrunk": round(e, 3),
                "reason": "shrunk edge %.3f < floor %.2f for p=%.2f" % (e, floor, pm)}
    return {"verdict": "PASS", "q_shrunk": qs, "edge_shrunk": round(e, 3),
            "reason": "shrunk edge %.3f clears floor %.2f" % (e, floor)}


def quarter_kelly_stake(model: float, price: float, bankroll: float,
                        frac: float = 0.25, cell_cap_frac: float = 0.05) -> float:
    """Fractional-Kelly stake for a binary YES at PM price ``price`` (=implied prob).

    Kelly fraction f* = (q - p) / (1 - p); stake = frac * f* * bankroll, capped at
    ``cell_cap_frac`` of bankroll. Returns 0 when there's no edge (q <= p).
    """
    if model <= price or price >= 1.0 or price <= 0.0:
        return 0.0
    f = (model - price) / (1.0 - price)
    return min(cell_cap_frac * bankroll, frac * f * bankroll)


@dataclass
class Plan:
    bankroll: float
    existing_long: Dict[str, float]
    existing_total: float
    trims: List[Dict[str, object]] = field(default_factory=list)
    new_orders: List[Dict[str, object]] = field(default_factory=list)
    topups: List[Dict[str, object]] = field(default_factory=list)
    deploy_budget: float = 0.0
    new_total: float = 0.0
    combined: float = 0.0
    buffer: float = 0.0


def build_plan(
    open_positions: Sequence[Position],
    candidates: Sequence[Dict[str, object]],
    *,
    bankroll: float,
    frac: float = 0.25,
    cell_cap_frac: float = 0.05,
    team_cap_frac: float = 0.10,
    deploy_frac: float = 0.75,
    min_model: float = 0.25,
    haircut_edge: float = 0.10,
) -> Plan:
    """Build the unified trim + new-order plan netting both books.

    ``candidates`` are ``{team, stage, model, pm, kind}`` rows (model > pm = buy-YES
    value). New bets are quarter-Kelly, capped per cell AND per *joint* per-team
    long exposure (existing + new), inside a whole-book deploy ceiling that always
    leaves a cash buffer. Trims are flagged only where an open long is no longer
    +EV to hold (model < current pm).
    """
    existing_long = team_long_exposure(open_positions)
    existing_total = sum(p.stake for p in open_positions)
    plan = Plan(bankroll=bankroll, existing_long=existing_long, existing_total=existing_total)

    # Trims: open long advancement/outright positions now -EV to hold (model < pm).
    for p in open_positions:
        if p.kind not in ("adv", "outright"):
            continue
        mp = _model_pm_for(p, candidates)
        if mp is None:
            continue
        model, pm = mp
        if model + 1e-9 < pm:  # market now prices it above model fair value
            plan.trims.append({
                "team": p.team, "stage": p.stage, "stake": round(p.stake, 2),
                "model": round(model, 3), "pm": round(pm, 3),
                "hold_edge": round(model - pm, 3),
                "reason": "model<price → not +EV to hold; sell ~at %.2f vs fair %.2f" % (pm, model),
            })

    deploy_budget = max(0.0, deploy_frac * bankroll - existing_total)
    cell_cap = cell_cap_frac * bankroll
    team_cap = team_cap_frac * bankroll
    team_used = dict(existing_long)
    spent = 0.0

    held_keys = {(p.team, p.stage) for p in open_positions if p.kind in ("adv", "outright")}
    held_teams = set(existing_long.keys())
    ranked = sorted(
        [c for c in candidates if float(c["model"]) > float(c["pm"]) and float(c["model"]) >= min_model],
        key=lambda c: -((float(c["model"]) - float(c["pm"])) * (0.5 + float(c["model"]))),
    )
    for c in ranked:
        team, stage = c["team"], c["stage"]
        model, pm = float(c["model"]), float(c["pm"])
        stake = quarter_kelly_stake(model, pm, bankroll, frac, cell_cap_frac)
        if model < 0.45 and (model - pm) > haircut_edge:
            stake *= 0.5  # noisy big-edge advancement (hygiene)
        stake = min(stake, cell_cap,
                    team_cap - team_used.get(team, 0.0),
                    deploy_budget - spent)
        if stake < 5.0:
            continue
        # The exact cell is already in the book -> this is a TOP-UP, not a new bet.
        # Surface separately so it's never mislabelled as a fresh position.
        if (team, stage) in held_keys:
            plan.topups.append({
                "team": team, "stage": stage, "model": round(model, 3), "pm": round(pm, 3),
                "edge": round(model - pm, 3), "add_stake": round(stake, 1),
                "already_held": round(existing_long.get(team, 0.0), 1),
            })
            continue
        plan.new_orders.append({
            "team": team, "stage": stage, "model": round(model, 3), "pm": round(pm, 3),
            "edge": round(model - pm, 3), "stake": round(stake, 1),
            "ko_wins": implied_ko_wins(stage),
            "team_already_long": round(team_used.get(team, 0.0), 1),
            # nested = team is already long a DIFFERENT advancement stage (correlated)
            "nested_with_held": (team in held_teams),
        })
        team_used[team] = team_used.get(team, 0.0) + stake
        spent += stake

    plan.deploy_budget = round(deploy_budget, 2)
    plan.new_total = round(spent, 2)
    plan.combined = round(existing_total + spent, 2)
    plan.buffer = round(bankroll - plan.combined, 2)
    return plan


def _model_pm_for(p: Position, candidates: Sequence[Dict[str, object]]) -> Optional[Tuple[float, float]]:
    """Current (model, pm) for an open position, looked up from the model rows."""
    for c in candidates:
        if c["team"] == p.team and c.get("stage") == p.stage:
            return float(c["model"]), float(c["pm"])
    return None
