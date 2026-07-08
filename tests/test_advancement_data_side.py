"""advancement_data pm block must emit the traded side (+ executable ask).

``_pm_by_team_stage`` used to drop ``AdvancementEdge.side``/``pm_price`` on
the floor, forcing the Edge Desk to re-derive the side from sign(model - mid)
— which mis-attributes against a stale-print mid (HIGH-2,
``side_attribution_uncertain``). These tests pin the source fix: every pm
entry now carries ``side: "YES"|"NO"`` and ``ask`` (the buy price of that side
that ``edge_adj`` was computed against), while the legacy ``pm``/``edge_adj``
keys are byte-for-byte unchanged.

Fully offline: Polymarket discovery is monkeypatched with fixture events; the
real ``wca.advancement.compare_to_polymarket`` maths runs end-to-end.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "src"))

import wca_advancement_data as mod  # noqa: E402


def _sim_df():
    # Real entered teams (the market matcher drops non-entered names).
    recs = [
        {"team": "Morocco", "group": "C", "P(SF)": 0.55},
        {"team": "Brazil", "group": "C", "P(SF)": 0.30},
        {"team": "Netherlands", "group": "F", "P(SF)": 0.45},
    ]
    return mod._records_to_simdf(recs)


_EVENTS = [{
    "title": "World Cup: Nation To Reach Semifinals",
    "markets": [
        # YES side favoured: mid 0.44, ask 0.46;
        # edge = 0.55 - 0.46 - 0.03*0.46*0.54 = +0.082548.
        {"groupItemTitle": "Morocco", "bestBid": 0.42, "bestAsk": 0.46},
        # NO side favoured: mid 0.65, NO ask = 1 - bid = 0.36;
        # edge = 0.70 - 0.36 - 0.03*0.36*0.64 = +0.333088.
        {"groupItemTitle": "Brazil", "bestBid": 0.64, "bestAsk": 0.66},
        # The reviewer's stale-print scenario (HIGH-2): priceMap Yes=0.50 is
        # a stale last print (no bestBid), the executable ask is 0.40. The
        # edge belongs to YES (0.45-0.40-fee = +0.0428 > NO's +0.0425) even
        # though model < mid — exactly the case the derived sign test gets
        # WRONG. The feed must name the side so consumers never re-derive it.
        {"groupItemTitle": "Netherlands", "bestAsk": 0.40,
         "priceMap": {"Yes": 0.50}},
    ],
}]


def test_pm_block_emits_side_and_ask(monkeypatch):
    monkeypatch.setattr(mod.polymarket, "find_world_cup_markets",
                        lambda include_closed=False: _EVENTS)
    out, n, path_exposure = mod._pm_by_team_stage(_sim_df())
    assert n == 3

    yes = out["Morocco"]["SF"]
    assert {k: yes[k] for k in ("pm", "edge_adj", "side", "ask")} == {
        "pm": 0.44, "edge_adj": 0.0825, "side": "YES", "ask": 0.46}

    no = out["Brazil"]["SF"]
    assert {k: no[k] for k in ("pm", "edge_adj", "side", "ask")} == {
        "pm": 0.65, "edge_adj": 0.3331, "side": "NO", "ask": 0.36}

    # Legacy consumers: pm is still the YES mid and edge_adj the better-side
    # fee-adjusted edge — the new keys are strictly additive (stake_usd /
    # path_scale carry the sizing source's path-capped ¼-Kelly per rung).
    for entry in (yes, no):
        assert set(entry) == {"pm", "edge_adj", "side", "ask",
                              "stake_usd", "path_scale"}
        assert entry["stake_usd"] > 0.0
        # Single staked rung per (team, side) family here — never scaled.
        assert entry["path_scale"] == 1.0

    # Per-team path-exposure blocks: single-rung families are uncapped
    # (total == cap, no scaling) and keyed by the traded side.
    yes_blk = path_exposure["Morocco"]["YES"]
    assert yes_blk["scaling_applied"] is False
    assert yes_blk["total_stake_usd"] == yes_blk["cap_usd"]
    assert yes_blk["stages"] == ["SF"] and yes_blk["cap_stage"] == "SF"
    no_blk = path_exposure["Brazil"]["NO"]
    assert no_blk["scaling_applied"] is False
    assert no_blk["total_stake_usd"] == no_blk["cap_usd"]


def test_pm_block_names_side_in_stale_print_scenario(monkeypatch):
    monkeypatch.setattr(mod.polymarket, "find_world_cup_markets",
                        lambda include_closed=False: _EVENTS)
    out, _, _ = mod._pm_by_team_stage(_sim_df())
    row = out["Netherlands"]["SF"]
    # Sign(model - mid) would say NO (0.45 < 0.50); the true edge side is YES
    # at the 0.40 ask. The feed states it so nothing downstream has to guess.
    assert row["side"] == "YES"
    assert row["ask"] == 0.40
    assert row["pm"] == 0.50            # YES mid (stale last print)
    assert row["edge_adj"] == 0.0428


def test_pm_failure_still_returns_empty_not_raise(monkeypatch):
    def _boom(include_closed=False):
        raise RuntimeError("no PM route on this host")
    monkeypatch.setattr(mod.polymarket, "find_world_cup_markets", _boom)
    out, n, path_exposure = mod._pm_by_team_stage(_sim_df())
    assert out == {} and n == 0 and path_exposure == {}
