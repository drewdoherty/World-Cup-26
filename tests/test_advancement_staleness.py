"""Advancement futures must be WITHHELD when the advancement feed is stale.

Regression guard for 2026-07-02: a 15h-old advancement feed kept recommending
Bosnia and Herzegovina (already eliminated in the R32) at a phantom +16.7%
edge, because the only staleness gate was the 24h model gate. Stale futures
now withhold at ADV_STALE_SECS (6h) with an explicit re-run warning.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_betrecs as br  # noqa: E402


def _adv_data():
    return {
        "meta": {"stages": ["R16"], "n_pm_markets": 31},
        "teams": [
            {
                "team": "Testland",
                "group": "A",
                "model": {"R16": 0.50},
                "pm": {"R16": {"pm": 0.30, "edge_adj": 0.15}},
                "delta": {},
            }
        ],
    }


def _pool():
    return {
        "bankroll": 3990.0,
        "kelly_fraction": 0.25,
        "per_bet_cap": 0.05,
        "max_stake": 199.5,
        "currency": "USD",
    }


def test_fresh_feed_is_actionable():
    actionable, withheld = br.build_advancement_futures(
        _adv_data(), _pool(), adv_age_secs=1800
    )
    assert len(actionable) == 1 and not withheld
    assert actionable[0]["stale"] is False


def test_stale_feed_is_withheld_with_rerun_warning():
    actionable, withheld = br.build_advancement_futures(
        _adv_data(), _pool(), adv_age_secs=15 * 3600
    )
    assert not actionable and len(withheld) == 1
    reason = withheld[0]["withheld_reason"]
    assert "stale" in reason and "re-run" in reason


def test_very_stale_feed_reports_model_staleness():
    actionable, withheld = br.build_advancement_futures(
        _adv_data(), _pool(), adv_age_secs=30 * 3600
    )
    assert not actionable and len(withheld) == 1
    assert "model stale" in withheld[0]["withheld_reason"]


def test_pm_blind_feed_is_withheld_even_when_fresh():
    """A fresh-stamped feed built with ZERO live PM markets (network block)
    must withhold everything — cached prices had an eliminated team actionable."""
    data = _adv_data()
    data["meta"]["n_pm_markets"] = 0
    actionable, withheld = br.build_advancement_futures(
        data, _pool(), adv_age_secs=600
    )
    assert not actionable and len(withheld) == 1
    assert "NO live PM markets" in withheld[0]["withheld_reason"]


def test_live_pm_markets_pass_the_guard():
    data = _adv_data()
    data["meta"]["n_pm_markets"] = 31
    actionable, withheld = br.build_advancement_futures(
        data, _pool(), adv_age_secs=600
    )
    assert len(actionable) == 1 and not withheld


def test_state_stale_team_is_withheld_with_reason():
    """Regression (2026-07-08): a team whose earlier KO tie kicked off but is
    NOT pinned in the sim's conditioning set has phantom probs (USA showed
    P(QF)=0.317 after its Jul-6 elimination). The feed builder stamps
    ``state_stale_reason``; the rec must be withheld with that exact reason,
    never sized — even on a fresh, PM-live feed."""
    reason = ("state-stale: earlier-round tie unsettled in sim "
              "(United States vs Belgium, kicked off 2026-07-06)")
    data = _adv_data()
    data["teams"][0]["state_stale_reason"] = reason
    actionable, withheld = br.build_advancement_futures(
        data, _pool(), adv_age_secs=600
    )
    assert not actionable and len(withheld) == 1
    assert withheld[0]["withheld_reason"] == reason
    assert withheld[0]["stake"] == 0.0


def test_resolved_pm_market_is_withheld():
    """Regression (2026-07-08): a YES quote pinned at >=0.98 / <=0.02 is a
    RESOLVED market — a stale-sim 'edge' against it is phantom, not a trade."""
    data = _adv_data()
    # pm=0.02 vs model 0.50 -> a huge phantom edge; must be withheld anyway.
    data["teams"][0]["pm"]["R16"] = {"pm": 0.02, "edge_adj": 0.45}
    actionable, withheld = br.build_advancement_futures(
        data, _pool(), adv_age_secs=600
    )
    assert not actionable and len(withheld) == 1
    assert "resolved market" in withheld[0]["withheld_reason"]
    assert withheld[0]["stake"] == 0.0
