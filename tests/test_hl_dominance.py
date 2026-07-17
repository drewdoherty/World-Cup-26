from __future__ import annotations

import pytest

from wca.hl.dominance import advance_probability, evaluate_advancement_vs_1x2


def test_advance_probability_uses_draw_conditional_branch():
    assert advance_probability(0.30, 0.40, 0.60) == pytest.approx(0.54)


def test_buy_superset_detects_directly_executable_candidate():
    got = evaluate_advancement_vs_1x2(
        hl_advance_yes_ask=0.52,
        hl_advance_no_ask=0.49,
        pm_team_yes_ask=0.31,
        pm_team_no_ask=0.44,
        pm_draw_yes_ask=0.28,
        hl_settlement_fee=0.0,
    )
    assert got["buy_superset"]["status"] == "ARB_CANDIDATE"
    assert got["buy_superset"]["margin"] > 0
    assert got["buy_cover"]["status"] == "NO_ARB"


def test_unknown_hl_settlement_fee_never_claims_arb():
    got = evaluate_advancement_vs_1x2(
        hl_advance_yes_ask=0.50,
        hl_advance_no_ask=0.55,
        pm_team_yes_ask=0.30,
        pm_team_no_ask=0.40,
        pm_draw_yes_ask=0.30,
    )
    assert got["buy_superset"]["margin"] > 0
    assert got["buy_superset"]["status"] == "CANDIDATE_FEE_UNVERIFIED"
    assert got["settlement_fee_verified"] is False


def test_fee_can_remove_candidate():
    got = evaluate_advancement_vs_1x2(
        hl_advance_yes_ask=0.50,
        hl_advance_no_ask=0.55,
        pm_team_yes_ask=0.30,
        pm_team_no_ask=0.40,
        pm_draw_yes_ask=0.30,
        hl_settlement_fee=0.11,
    )
    assert got["buy_superset"]["status"] == "NO_ARB"


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.1])
def test_prices_fail_closed(bad):
    with pytest.raises(ValueError):
        evaluate_advancement_vs_1x2(
            hl_advance_yes_ask=bad,
            hl_advance_no_ask=0.5,
            pm_team_yes_ask=0.3,
            pm_team_no_ask=0.4,
            pm_draw_yes_ask=0.3,
        )

