"""Same-team nested-path exposure aggregation (fix 2026-07-08).

``compare_to_polymarket`` used to size every team-stage row at an INDEPENDENT
quarter-Kelly of the same Polymarket pool, but the stages are NESTED for one
team (win ⊂ Final ⊂ SF ⊂ QF ⊂ R16 ⊂ R32): positions across several rungs of
one team's path are ONE correlated leg, not independent bets (observed live:
Morocco SF $112 + Morocco Final $33 ≈ 4.5% of the pool on a single path).

These tests pin the aggregation rule end to end:

* per ``(team, side)`` family over the nested chain the stake SUM respects the
  quarter-Kelly stake of the TIGHTEST staked rung (deepest stage for YES;
  shallowest for NO — the NO nesting runs the other way), with proportional
  scaling;
* a single staked rung is unchanged;
* NO rows are never co-capped with YES rows of the same team;
* zero-stake rows (longshot no-cash floor) are untouched and never pick the
  cap rung; group-winner stays a separate exposure;
* aggregation only ever REDUCES stakes;
* the Action Desk builder (scripts/wca_betrecs.py) respects the feed-emitted
  per-rung path cap as a hard ceiling.

All offline: hand-built sim frames + Polymarket event fixtures; the realistic
whole-book fixture reproduces the committed 2026-07-08 live feed quotes.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca.advancement import (  # noqa: E402
    NESTED_PATH_STAGES,
    apply_path_exposure_caps,
    compare_to_polymarket,
    path_exposure_summary,
    stage_further_out,
)

import wca_betrecs as br  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_SIM_COLS = [
    "team", "group", "P(R32)", "P(R16)", "P(QF)", "P(SF)", "P(Final)",
    "P(win)", "P(group_winner)",
]


def _sim_df(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_SIM_COLS).set_index("team")


def _mkt(team: str, yes_bid: float, yes_ask: float) -> Dict:
    return {
        "groupItemTitle": team,
        "bestBid": yes_bid,
        "bestAsk": yes_ask,
        "priceMap": {"Yes": 0.5 * (yes_bid + yes_ask)},
    }


def _by_team_stage(out: pd.DataFrame) -> Dict:
    return {(r["team"], r["stage"]): r for _, r in out.iterrows()}


# ---------------------------------------------------------------------------
# Core aggregation rule
# ---------------------------------------------------------------------------


def test_nested_two_rungs_sum_respects_deepest_cap_and_scales_proportionally():
    # Morocco with TWO cash-eligible YES rungs (both model >= 0.25): SF + Final.
    sim = _sim_df([
        ("Morocco", "C", 1.0, 1.0, 0.80, 0.55, 0.30, 0.12, 1.0),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals",
         "markets": [_mkt("Morocco", 0.40, 0.42)]},   # sim 0.55 -> YES edge
        {"title": "World Cup: Nation to Reach Final",
         "markets": [_mkt("Morocco", 0.18, 0.20)]},   # sim 0.30 -> YES edge
    ]
    out = compare_to_polymarket(sim, events)
    rows = _by_team_stage(out)
    sf, fin = rows[("Morocco", "SF")], rows[("Morocco", "F")]
    assert sf["side"] == "YES" and fin["side"] == "YES"
    assert sf["stake_precap"] > 0 and fin["stake_precap"] > 0

    # Cap = the DEEPEST staked rung's independent ¼-Kelly stake (Final here).
    cap = fin["stake_precap"]
    assert sf["path_cap_usd"] == pytest.approx(cap)
    assert fin["path_cap_usd"] == pytest.approx(cap)

    # Sum respects the cap; both rungs were genuinely reduced.
    total = sf["stake"] + fin["stake"]
    assert total <= cap + 1e-9
    assert sf["stake"] < sf["stake_precap"]
    assert fin["stake"] < fin["stake_precap"]

    # Proportional scaling: one common factor, relative sizing preserved.
    expected_scale = cap / (sf["stake_precap"] + fin["stake_precap"])
    assert sf["path_scale"] == pytest.approx(expected_scale)
    assert fin["path_scale"] == pytest.approx(expected_scale)
    assert sf["stake"] == pytest.approx(sf["stake_precap"] * expected_scale)
    assert fin["stake"] == pytest.approx(fin["stake_precap"] * expected_scale)
    assert sf["stake"] / fin["stake"] == pytest.approx(
        sf["stake_precap"] / fin["stake_precap"])


def test_single_staked_rung_unchanged():
    sim = _sim_df([
        ("Morocco", "C", 1.0, 1.0, 0.80, 0.55, 0.30, 0.12, 1.0),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals",
         "markets": [_mkt("Morocco", 0.40, 0.42)]},
    ]
    out = compare_to_polymarket(sim, events)
    row = out.iloc[0]
    assert row["stake"] > 0
    assert row["stake"] == pytest.approx(row["stake_precap"])
    assert row["path_scale"] == pytest.approx(1.0)
    assert row["path_cap_usd"] == pytest.approx(row["stake_precap"])


def test_no_side_never_co_capped_with_yes_and_no_family_caps_at_shallowest():
    # France-shaped team: strong SF/no-Final path -> NO edges on SF AND Final,
    # plus a YES rung on... use a second team for the YES/NO split check.
    sim = _sim_df([
        # Team with one YES rung (Final) and one NO rung (SF): the two sides
        # must remain independent single-rung families (no cross-cap).
        ("Spain", "H", 1.0, 1.0, 1.0, 0.69, 0.42, 0.25, 1.0),
        # Team with TWO NO rungs: NO-SF ⊂ NO-Final, so the NO family cap is
        # the SHALLOWEST staked stage (SF).
        ("France", "I", 1.0, 1.0, 1.0, 0.69, 0.35, 0.19, 1.0),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals",
         "markets": [
             _mkt("Spain", 0.75, 0.76),    # sim 0.69 < NO buy favours NO
             _mkt("France", 0.77, 0.78),   # NO buy 0.23 vs NO prob 0.31
         ]},
        {"title": "World Cup: Nation to Reach Final",
         "markets": [
             _mkt("Spain", 0.35, 0.36),    # sim 0.42 -> YES edge
             _mkt("France", 0.53, 0.54),   # NO buy 0.47 vs NO prob 0.65
         ]},
    ]
    out = compare_to_polymarket(sim, events)
    rows = _by_team_stage(out)

    # Spain: YES-Final and NO-SF are DIFFERENT exposures — both single-rung
    # families, both unscaled, each capped by its own stake only.
    sp_yes, sp_no = rows[("Spain", "F")], rows[("Spain", "SF")]
    assert sp_yes["side"] == "YES" and sp_no["side"] == "NO"
    assert sp_yes["stake"] > 0 and sp_no["stake"] > 0
    assert sp_yes["path_scale"] == pytest.approx(1.0)
    assert sp_no["path_scale"] == pytest.approx(1.0)
    assert sp_yes["stake"] == pytest.approx(sp_yes["stake_precap"])
    assert sp_no["stake"] == pytest.approx(sp_no["stake_precap"])
    assert sp_yes["path_cap_usd"] == pytest.approx(sp_yes["stake_precap"])
    assert sp_no["path_cap_usd"] == pytest.approx(sp_no["stake_precap"])

    # France: two NO rungs form ONE family, capped by the SHALLOWEST staked
    # stage (SF — "fails to reach SF" is the subset event).
    fr_sf, fr_f = rows[("France", "SF")], rows[("France", "F")]
    assert fr_sf["side"] == "NO" and fr_f["side"] == "NO"
    cap = fr_sf["stake_precap"]
    assert fr_sf["path_cap_usd"] == pytest.approx(cap)
    assert fr_f["path_cap_usd"] == pytest.approx(cap)
    assert fr_sf["stake"] + fr_f["stake"] <= cap + 1e-9


def test_zero_stake_longshot_rows_unaffected_and_never_pick_the_cap():
    # Deepest rung (Final) is a <25c model longshot: +EV but cash stake 0.
    # The cap must come from the deepest STAKED rung (SF), so the single
    # staked rung stays unchanged and the zero row stays exactly zero.
    sim = _sim_df([
        ("Morocco", "C", 1.0, 1.0, 0.80, 0.55, 0.10, 0.04, 1.0),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals",
         "markets": [_mkt("Morocco", 0.40, 0.42)]},
        {"title": "World Cup: Nation to Reach Final",
         "markets": [_mkt("Morocco", 0.05, 0.06)]},   # sim 0.10 -> +EV longshot
    ]
    out = compare_to_polymarket(sim, events)
    rows = _by_team_stage(out)
    sf, fin = rows[("Morocco", "SF")], rows[("Morocco", "F")]
    assert fin["side"] == "YES" and fin["no_cash"]
    assert fin["stake"] == 0.0 and fin["stake_precap"] == 0.0
    assert fin["path_scale"] == pytest.approx(1.0)
    assert sf["stake"] == pytest.approx(sf["stake_precap"])
    assert sf["path_cap_usd"] == pytest.approx(sf["stake_precap"])


def test_aggregation_never_increases_any_stake():
    sim = _sim_df([
        ("Morocco", "C", 1.0, 1.0, 0.80, 0.55, 0.30, 0.12, 1.0),
        ("Spain", "H", 1.0, 1.0, 1.0, 0.69, 0.42, 0.26, 0.9),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals",
         "markets": [_mkt("Morocco", 0.40, 0.42), _mkt("Spain", 0.60, 0.62)]},
        {"title": "World Cup: Nation to Reach Final",
         "markets": [_mkt("Morocco", 0.18, 0.20), _mkt("Spain", 0.35, 0.36)]},
        {"title": "World Cup Winner",
         "markets": [_mkt("Spain", 0.18, 0.19)]},
        {"title": "World Cup Group H Winner",
         "markets": [_mkt("Spain", 0.80, 0.82)]},
    ]
    out = compare_to_polymarket(sim, events)
    assert (out["stake"] <= out["stake_precap"] + 1e-12).all()


def test_group_winner_is_a_separate_exposure():
    assert "GW" not in NESTED_PATH_STAGES
    sim = _sim_df([
        ("Spain", "H", 1.0, 1.0, 1.0, 0.69, 0.42, 0.26, 0.9),
    ])
    events = [
        {"title": "World Cup Group H Winner",
         "markets": [_mkt("Spain", 0.80, 0.82)]},   # sim GW 0.9 -> YES edge
        {"title": "World Cup: Nation to Reach Final",
         "markets": [_mkt("Spain", 0.35, 0.36)]},
        {"title": "World Cup Winner",
         "markets": [_mkt("Spain", 0.18, 0.19)]},
    ]
    out = compare_to_polymarket(sim, events)
    rows = _by_team_stage(out)
    gw = rows[("Spain", "GW")]
    # GW keeps its independent stake and is stamped with no path cap...
    assert gw["stake"] == pytest.approx(gw["stake_precap"])
    assert gw["path_scale"] == pytest.approx(1.0)
    assert pd.isna(gw["path_cap_usd"])
    # ...while the chain rungs (Final + win, same YES side) are co-capped by
    # the deepest staked rung (win) WITHOUT counting the GW stake.
    fin, win = rows[("Spain", "F")], rows[("Spain", "win")]
    cap = win["stake_precap"]
    assert fin["path_cap_usd"] == pytest.approx(cap)
    assert fin["stake"] + win["stake"] <= cap + 1e-9


# ---------------------------------------------------------------------------
# Whole-book invariant on a realistic fixture (committed 2026-07-08 quotes)
# ---------------------------------------------------------------------------


def _live_20260708_fixture():
    """Sim probs + reconstructed order books from the committed live feed.

    Quotes are the site/advancement_data.json values of 2026-07-08 (YES mid +
    traded-side ask); bids are recovered as ``2*mid - ask`` for YES-side rows
    and ``1 - no_ask`` for NO-side rows. This is the feed on which the desk
    staked Spain Final + Spain win as two independent full-Kelly rungs.
    """
    sim = _sim_df([
        ("Spain", "H", 1.0, 1.0, 1.0, 0.6917, 0.4161, 0.2519, 1.0),
        ("Argentina", "J", 1.0, 1.0, 1.0, 0.7214, 0.4237, 0.2236, 1.0),
        ("France", "I", 1.0, 1.0, 1.0, 0.6929, 0.3529, 0.1915, 1.0),
        ("England", "L", 1.0, 1.0, 1.0, 0.6999, 0.3726, 0.1777, 1.0),
        ("Belgium", "G", 1.0, 1.0, 1.0, 0.3083, 0.1310, 0.0549, 1.0),
        ("Morocco", "C", 1.0, 1.0, 1.0, 0.3071, 0.0999, 0.0385, 1.0),
    ])
    events = [
        {"title": "World Cup: Nation To Reach Semifinals", "markets": [
            _mkt("Spain", 0.75, 0.76),
            _mkt("Argentina", 0.73, 0.74),
            _mkt("France", 0.77, 0.78),
            _mkt("England", 0.65, 0.66),
            _mkt("Belgium", 0.253, 0.254),
            _mkt("Morocco", 0.223, 0.224),
        ]},
        {"title": "World Cup: Nation to Reach Final", "markets": [
            _mkt("Spain", 0.35, 0.36),
            _mkt("Argentina", 0.392, 0.399),
            _mkt("France", 0.53, 0.54),
            _mkt("England", 0.38, 0.39),
            _mkt("Belgium", 0.066, 0.067),
            _mkt("Morocco", 0.078, 0.079),
        ]},
        {"title": "World Cup Winner", "markets": [
            _mkt("Spain", 0.187, 0.188),
            _mkt("Argentina", 0.187, 0.188),
            _mkt("France", 0.326, 0.327),
            _mkt("England", 0.157, 0.158),
            _mkt("Belgium", 0.024, 0.025),
            _mkt("Morocco", 0.031, 0.032),
        ]},
    ]
    return sim, events


def test_whole_book_invariant_every_family_sum_within_its_path_cap():
    sim, events = _live_20260708_fixture()
    out = compare_to_polymarket(sim, events)
    assert not out.empty

    staked = out[out["stage"].isin(NESTED_PATH_STAGES) & (out["stake"] > 0)]
    assert not staked.empty
    for (team, side), grp in staked.groupby(["team", "side"]):
        cap = grp["path_cap_usd"].iloc[0]
        assert (grp["path_cap_usd"] == cap).all(), (team, side)
        # Whole-book rule: the family's staked rungs sum within the path cap.
        assert grp["stake"].sum() <= cap + 1e-6, (team, side)
        # The cap is the tightest STAKED rung's independent stake.
        depth = grp["stage"].map(stage_further_out)
        tight = depth.idxmax() if side == "YES" else depth.idxmin()
        assert cap == pytest.approx(grp.loc[tight, "stake_precap"]), (team, side)

    # The live overbet this fix targets: Spain Final + Spain win (YES family)
    # must now respect the win rung's independent ¼-Kelly.
    rows = _by_team_stage(out)
    sp_f, sp_w = rows[("Spain", "F")], rows[("Spain", "win")]
    assert sp_f["side"] == "YES" and sp_w["side"] == "YES"
    assert sp_f["stake_precap"] + sp_w["stake_precap"] > sp_w["stake_precap"]
    assert sp_f["stake"] + sp_w["stake"] <= sp_w["stake_precap"] + 1e-9
    # Morocco today has a single cash rung (SF; Final/win are <25c longshots)
    # -> unchanged by the aggregation.
    mo_sf = rows[("Morocco", "SF")]
    assert mo_sf["stake"] == pytest.approx(mo_sf["stake_precap"])
    assert rows[("Morocco", "F")]["stake"] == 0.0


def test_path_exposure_summary_blocks_match_frame():
    sim, events = _live_20260708_fixture()
    out = compare_to_polymarket(sim, events)
    summary = path_exposure_summary(out)

    # Spain YES family: scaled, total == cap, cap rung is the deepest (win).
    blk = summary["Spain"]["YES"]
    assert blk["scaling_applied"] is True
    assert blk["scale"] < 1.0
    assert blk["total_stake_usd"] == pytest.approx(blk["cap_usd"], abs=0.01)
    assert blk["cap_stage"] == "win"
    assert blk["total_stake_precap_usd"] > blk["cap_usd"]
    assert blk["stages"] == ["F", "win"]

    # France NO family: cap rung is the SHALLOWEST staked stage (SF).
    blk = summary["France"]["NO"]
    assert blk["cap_stage"] == "SF"
    assert blk["total_stake_usd"] <= blk["cap_usd"] + 0.01

    # Every emitted family is internally consistent.
    for team, sides in summary.items():
        for side, b in sides.items():
            assert b["total_stake_usd"] <= b["cap_usd"] + 0.01, (team, side)
            assert b["scaling_applied"] == (b["scale"] < 1.0)


def test_apply_path_exposure_caps_empty_frame_is_safe():
    out = apply_path_exposure_caps(
        pd.DataFrame(columns=["team", "side", "stage", "stake"]))
    assert list(out.columns[:4]) == ["team", "side", "stage", "stake"]
    assert {"stake_precap", "path_cap_usd", "path_scale"} <= set(out.columns)
    assert path_exposure_summary(out) == {}


# ---------------------------------------------------------------------------
# Action Desk builder (scripts/wca_betrecs.py) respects the feed's path cap
# ---------------------------------------------------------------------------


def _pm_pool(bankroll: float = 3990.0) -> Dict:
    return {
        "bankroll": bankroll,
        "kelly_fraction": 0.25,
        "per_bet_cap": 0.04,
        "max_stake": bankroll * 0.04,
        "currency": "USD",
    }


def _adv_feed_two_rungs(sf_stake_usd: float, final_stake_usd: float) -> Dict:
    return {
        "meta": {"generated": "2099-12-31 20:00:00 UTC",
                 "stages": ["SF", "Final"], "n_pm_markets": 12},
        "teams": [{
            "team": "TeamX", "group": "A",
            "model": {"SF": 0.55, "Final": 0.30},
            "pm": {
                "SF": {"pm": 0.40, "edge_adj": 0.1352, "side": "YES",
                       "ask": 0.41, "stake_usd": sf_stake_usd,
                       "path_scale": 0.5},
                "Final": {"pm": 0.18, "edge_adj": 0.0953, "side": "YES",
                          "ask": 0.19, "stake_usd": final_stake_usd,
                          "path_scale": 0.5},
            },
            "path_exposure": {"YES": {
                "total_stake_usd": round(sf_stake_usd + final_stake_usd, 2),
                "cap_usd": round(sf_stake_usd + final_stake_usd, 2),
                "scaling_applied": True,
            }},
            "delta": {},
        }],
    }


def test_betrecs_respects_feed_path_cap_per_rung():
    adv = _adv_feed_two_rungs(40.0, 25.0)
    recs, _ = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
    by_stage = {r["stage"]: r for r in recs}
    assert set(by_stage) == {"SF", "Final"}
    # Its own independent ¼-Kelly would exceed both caps; the feed's
    # path-capped rung stakes are hard ceilings.
    assert by_stage["SF"]["stake"] <= 40.0 + 1e-9
    assert by_stage["Final"]["stake"] <= 25.0 + 1e-9
    assert by_stage["SF"]["path_capped"] is True
    assert by_stage["Final"]["path_capped"] is True
    # Whole-team invariant on the Action Desk output.
    assert sum(r["stake"] for r in recs) <= 40.0 + 25.0 + 1e-6


def test_betrecs_uncapped_when_feed_lacks_stake_usd():
    # Legacy feed (pre-fix): no stake_usd -> behaviour byte-identical to the
    # old independent sizing, and no path_capped flag is stamped.
    adv = _adv_feed_two_rungs(40.0, 25.0)
    for stage_info in adv["teams"][0]["pm"].values():
        stage_info.pop("stake_usd")
        stage_info.pop("path_scale")
    adv["teams"][0].pop("path_exposure")
    recs, _ = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
    by_stage = {r["stage"]: r for r in recs}
    assert by_stage["SF"]["stake"] > 40.0
    assert all("path_capped" not in r for r in recs)


def test_betrecs_caps_the_side_the_feed_priced_even_when_no():
    # REWRITTEN for the 2026-07-14 side-aware fix. The pre-fix version of
    # this test pinned "a NO-side feed rung's stake must never cap this
    # builder's YES sizing" — an invariant that existed only because the
    # loop priced YES unconditionally, even when the feed said the edge was
    # on NO. The builder now sizes the SIDE THE FEED PRICED, so the feed's
    # per-rung path-capped ``stake_usd`` is a hard ceiling for that SAME
    # (team, side) exposure — NO included. Reduces only.
    adv = _adv_feed_two_rungs(40.0, 25.0)
    # NO-favoured SF market: position pays 1 - 0.55 = 0.45 at a 0.32 NO ask
    # (ev = 0.45 - 0.32 - fee = +0.1235 clears the 2pp floor); the feed's
    # path-capped NO rung stake is a deliberately tiny $1.
    adv["teams"][0]["pm"]["SF"] = {"pm": 0.70, "edge_adj": 0.1235,
                                   "side": "NO", "ask": 0.32,
                                   "stake_usd": 1.0, "path_scale": 0.5}
    recs, _ = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
    by_stage = {r["stage"]: r for r in recs}
    sf = by_stage["SF"]
    assert sf["side"] == "NO"
    assert sf["position_prob"] == pytest.approx(0.45)
    assert sf["position_price"] == pytest.approx(0.32)
    # Its own independent ¼-Kelly would be far larger; the feed's NO-side
    # rung stake caps the identical NO-side position the builder just sized.
    assert sf["stake"] == pytest.approx(1.0)
    assert sf["path_capped"] is True
    # The untouched YES Final rung keeps its own YES-side feed cap.
    assert by_stage["Final"]["side"] == "YES"
    assert by_stage["Final"]["stake"] <= 25.0 + 1e-9


def test_betrecs_zero_feed_stake_drops_rec_conservatively():
    # stake_usd == 0 on a YES rung (the sizing source found no fee-adjusted
    # Kelly edge at the executable ask) zeroes the rec — the desk defers to
    # the sizing source rather than re-staking from the friendlier mid.
    adv = _adv_feed_two_rungs(0.0, 25.0)
    recs, _ = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
    by_stage = {r["stage"]: r for r in recs}
    assert "SF" not in by_stage
    assert by_stage["Final"]["stake"] <= 25.0 + 1e-9
