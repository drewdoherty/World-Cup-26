"""Tests for wca.matched (pure matched-betting math) and the calc CLI.

Canonical examples are hand-computed and checked to high precision.  The two
profit branches of a free bet are asserted equal (the defining property of a
locked free-bet extraction).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from wca import matched


# ---------------------------------------------------------------------------
# best_lay_commission.
# ---------------------------------------------------------------------------


class TestBestLayCommission:
    def test_known_venues(self) -> None:
        assert matched.best_lay_commission("smarkets") == 0.02
        assert matched.best_lay_commission("smarkets_commfree") == 0.0
        assert matched.best_lay_commission("betfair") == 0.06
        assert matched.best_lay_commission("betfair_basic") == 0.02
        assert matched.best_lay_commission("matchbook") == 0.02

    def test_case_insensitive(self) -> None:
        assert matched.best_lay_commission("Smarkets") == 0.02
        assert matched.best_lay_commission("  BETFAIR  ") == 0.06

    def test_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            matched.best_lay_commission("nope")


# ---------------------------------------------------------------------------
# qualifying_bet.
# ---------------------------------------------------------------------------


class TestQualifyingBet:
    def test_canonical_back5_lay52_stake10_comm2pct(self) -> None:
        """back 5.0 / lay 5.2 / stake 10 / 2% commission.

        lay_stake = 10 * 5.0 / (5.2 - 0.02) = 50 / 5.18 = 9.6525096...
        liability = lay_stake * 4.2 = 40.5405...
        Both profit branches ~ -0.5405 (the qualifying loss).
        """
        r = matched.qualifying_bet(5.0, 5.2, 10.0, commission=0.02)
        assert abs(r.lay_stake - (50.0 / 5.18)) < 1e-9
        assert abs(r.liability - (50.0 / 5.18) * 4.2) < 1e-9
        # Both branches equal (the qualifying loss).
        assert abs(r.profit_if_back_wins - r.profit_if_lay_wins) < 1e-9
        assert abs(r.worst_case - (-0.5405405405)) < 1e-6
        assert r.worst_case < 0  # qualifying loss is negative
        # rating = worst_case / back_stake
        assert abs(r.rating - (r.worst_case / 10.0)) < 1e-12
        assert abs(r.rating - (-0.05405405)) < 1e-6

    def test_zero_commission_qualifying_loss_smaller(self) -> None:
        r0 = matched.qualifying_bet(5.0, 5.2, 10.0, commission=0.0)
        r2 = matched.qualifying_bet(5.0, 5.2, 10.0, commission=0.02)
        # Lower commission -> less qualifying loss (worst_case closer to 0).
        assert r0.worst_case > r2.worst_case

    def test_perfect_match_zero_loss(self) -> None:
        """back == lay, 0 commission -> zero qualifying loss exactly."""
        r = matched.qualifying_bet(3.0, 3.0, 20.0, commission=0.0)
        assert abs(r.worst_case) < 1e-9
        assert abs(r.rating) < 1e-9

    def test_as_dict_rounds_money(self) -> None:
        r = matched.qualifying_bet(5.0, 5.2, 10.0, commission=0.02)
        d = r.as_dict()
        assert d["lay_stake"] == round(r.lay_stake, 2)
        assert d["liability"] == round(r.liability, 2)
        # full precision retained internally
        assert r.lay_stake != round(r.lay_stake, 2)

    def test_lay_odds_le_one_raises(self) -> None:
        with pytest.raises(ValueError):
            matched.qualifying_bet(5.0, 1.0, 10.0)
        with pytest.raises(ValueError):
            matched.qualifying_bet(5.0, 0.9, 10.0)

    def test_back_odds_le_one_raises(self) -> None:
        with pytest.raises(ValueError):
            matched.qualifying_bet(1.0, 5.2, 10.0)

    def test_bad_commission_raises(self) -> None:
        with pytest.raises(ValueError):
            matched.qualifying_bet(5.0, 5.2, 10.0, commission=1.0)
        with pytest.raises(ValueError):
            matched.qualifying_bet(5.0, 5.2, 10.0, commission=-0.01)


# ---------------------------------------------------------------------------
# free_bet_snr.
# ---------------------------------------------------------------------------


class TestFreeBetSnr:
    def test_canonical_free30_back6_lay62_comm2pct(self) -> None:
        """SNR free bet 30 @ back 6.0 / lay 6.2 / 2% commission.

        lay_stake = 30*(6-1)/(6.2-0.02) = 150/6.18 = 24.27184...
        locked = 150 - lay_stake*5.2 = 23.78640...
        retention = locked/30 = 0.79288...  (~79%, in the 78-80% band)
        """
        r = matched.free_bet_snr(6.0, 6.2, 30.0, commission=0.02)
        assert abs(r.lay_stake - (150.0 / 6.18)) < 1e-9
        # back-wins == lay-wins (the locked-profit equality).
        assert abs(r.profit_if_back_wins - r.profit_if_lay_wins) < 1e-9
        assert abs(r.locked_profit - 23.7864077669) < 1e-6
        assert 0.78 <= r.retention_pct <= 0.80
        assert abs(r.retention_pct - r.locked_profit / 30.0) < 1e-12
        # rating mirrors retention for free bets.
        assert abs(r.rating - r.retention_pct) < 1e-12

    def test_zero_commission_higher_retention(self) -> None:
        r0 = matched.free_bet_snr(6.0, 6.2, 30.0, commission=0.0)
        r2 = matched.free_bet_snr(6.0, 6.2, 30.0, commission=0.02)
        assert r0.retention_pct > r2.retention_pct
        # 0% commission canonical: lay = 150/6.2 = 24.1935..; locked=150-lay*5.2
        assert abs(r0.lay_stake - (150.0 / 6.2)) < 1e-9
        assert abs(r0.retention_pct - 0.8064516129) < 1e-6

    def test_back_lay_win_equal_various(self) -> None:
        """The two branches must be equal across a range of inputs."""
        for back, lay, comm in [
            (4.0, 4.1, 0.0), (10.0, 11.0, 0.05),
            (2.5, 2.6, 0.02), (8.0, 8.4, 0.06),
        ]:
            r = matched.free_bet_snr(back, lay, 25.0, commission=comm)
            assert abs(r.profit_if_back_wins - r.profit_if_lay_wins) < 1e-9, (
                "branches unequal for back=%s lay=%s comm=%s" % (back, lay, comm)
            )

    def test_as_dict_has_retention(self) -> None:
        r = matched.free_bet_snr(6.0, 6.2, 30.0, commission=0.02)
        d = r.as_dict()
        assert d["retention_pct"] is not None
        assert d["back_stake"] == 0.0

    def test_lay_odds_le_one_raises(self) -> None:
        with pytest.raises(ValueError):
            matched.free_bet_snr(6.0, 1.0, 30.0)

    def test_bad_commission_raises(self) -> None:
        with pytest.raises(ValueError):
            matched.free_bet_snr(6.0, 6.2, 30.0, commission=1.0)


# ---------------------------------------------------------------------------
# free_bet_sr.
# ---------------------------------------------------------------------------


class TestFreeBetSr:
    def test_branches_equal_and_higher_than_snr(self) -> None:
        sr = matched.free_bet_sr(6.0, 6.2, 30.0, commission=0.02)
        snr = matched.free_bet_snr(6.0, 6.2, 30.0, commission=0.02)
        assert abs(sr.profit_if_back_wins - sr.profit_if_lay_wins) < 1e-9
        # Stake-returned retains more value than stake-not-returned, because the
        # returned stake adds ~free_stake of value on the back-win side.
        assert sr.retention_pct > snr.retention_pct
        # Closed form: retention = (1-c)*lay_stake/free_stake, and SR's lay
        # stake exceeds SNR's by exactly free_stake/(lay-c), so the retention
        # uplift is (1-c)/(lay-c).
        expected_uplift = (1.0 - 0.02) / (6.2 - 0.02)
        assert abs((sr.retention_pct - snr.retention_pct) - expected_uplift) < 1e-9

    def test_canonical_sr(self) -> None:
        """SR free bet 30 @ back 6.0 / lay 6.2 / 2%.

        lay_stake = 30*6.0/(6.2-0.02) = 180/6.18 = 29.12621...
        locked = 180 - lay*5.2; both branches equal.
        """
        r = matched.free_bet_sr(6.0, 6.2, 30.0, commission=0.02)
        assert abs(r.lay_stake - (180.0 / 6.18)) < 1e-9
        assert abs(r.profit_if_back_wins - r.profit_if_lay_wins) < 1e-9


# ---------------------------------------------------------------------------
# CLI smoke tests.
# ---------------------------------------------------------------------------


SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
CLI = str(SCRIPTS_DIR / "wca_matched.py")
PYTHON = sys.executable


class TestCalcCliSmoke:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([PYTHON, CLI] + list(args), capture_output=True, text=True)

    def test_calc_qualifying_venue(self) -> None:
        r = self._run("calc", "qualifying", "--back", "5.0", "--lay", "5.2",
                      "--stake", "10", "--venue", "smarkets")
        assert r.returncode == 0, r.stderr
        assert "Qualifying Bet" in r.stdout
        assert "Lay stake" in r.stdout

    def test_calc_freebet_commission(self) -> None:
        r = self._run("calc", "freebet", "--back", "6.0", "--lay", "6.2",
                      "--stake", "30", "--commission", "0.02")
        assert r.returncode == 0, r.stderr
        assert "Locked profit" in r.stdout
        assert "Retention" in r.stdout

    def test_main_callable_directly(self) -> None:
        spec = importlib.util.spec_from_file_location("wca_matched", CLI)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        module.main(["calc", "qualifying", "--back", "5.0", "--lay", "5.2",
                     "--stake", "10", "--commission", "0.02"])
