"""Tests for gate-fill telemetry in ``scripts/wca_betrecs.py``.

Motivation (2026-07-08 adversarial review): the 2pp edge floor rejects the
large majority of candidates but its cost was UNMEASURABLE because several
reject paths dropped candidates with a bare ``continue`` — no withheld row,
no ``reason_code``. This file:

1. Proves every previously-silent drop path now emits a withheld row with a
   machine-greppable ``reason_code`` (Section 1).
2. Proves the ACTIONABLE output (what a human can actually act on) is
   BYTE-IDENTICAL to the pre-telemetry behaviour on a realistic mixed fixture
   feed — telemetry-only, zero behaviour change (Section 2, the most
   important test here). The pre-change module is loaded straight from git
   (the exact commit this branch forked from) so the comparison is against
   real prior behaviour, not a hand-maintained literal that could silently
   drift out of sync with what "before" actually did.
3. Smoke-tests the new ``reason_code`` field never appears on actionable rows
   and never replaces the untouched ``withheld_reason`` free text that
   site/arb.js renders.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_betrecs as br  # noqa: E402  (the script under test — current version)


# ---------------------------------------------------------------------------
# Load the PRE-TELEMETRY module straight from git, for the invariance test.
#
# This is the exact commit ``feat/gate-fill-telemetry`` was branched from —
# origin/main immediately before any withheld-row instrumentation landed.
# Loading it dynamically (rather than hand-copying expected output into this
# file) means the invariance assertion is checked against what the code
# actually did, not against a transcription that could drift.
# ---------------------------------------------------------------------------
_BASELINE_REV = "100af5d"


def _load_baseline_module():
    try:
        src = subprocess.run(
            ["git", "show", "%s:scripts/wca_betrecs.py" % _BASELINE_REV],
            cwd=str(_REPO), capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except Exception as exc:  # pragma: no cover - environment without git history
        pytest.skip("cannot load baseline revision %s from git: %s" % (_BASELINE_REV, exc))
        return None
    if not src.strip():
        pytest.skip("baseline revision %s produced empty content" % _BASELINE_REV)
        return None

    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="wca_betrecs_baseline_"))
    tmp_path = tmp_dir / "wca_betrecs_baseline.py"
    tmp_path.write_text(src)

    spec = importlib.util.spec_from_file_location("wca_betrecs_baseline", str(tmp_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def br_baseline():
    return _load_baseline_module()


# ---------------------------------------------------------------------------
# Shared fixture builders (mirrors tests/test_betrecs.py conventions).
# ---------------------------------------------------------------------------

def _fix(
    fixture: str = "Team A vs Team B",
    kickoff: str = "2099-12-31 21:00:00+00:00",
    group: str = "X",
    model_home: float = 0.55,
    model_draw: float = 0.25,
    model_away: float = 0.20,
    devig_home: float = 0.50,
    devig_draw: float = 0.28,
    devig_away: float = 0.22,
    generated: str = "2099-12-31 20:00:00 UTC",
) -> Dict[str, Any]:
    return {
        "fixture": fixture,
        "kickoff": kickoff,
        "group": group,
        "generated": generated,
        "model": {"home": model_home, "draw": model_draw, "away": model_away},
        "market": {"home": devig_home, "draw": devig_draw, "away": devig_away},
    }


def _sb_pool(bankroll: float = 2000.0) -> Dict[str, Any]:
    return {
        "bankroll": bankroll,
        "rung": 0,
        "kelly_fraction": 0.25,
        "per_bet_cap": 0.05,
        "max_stake": bankroll * 0.05,
        "currency": "GBP",
    }


def _pm_pool(bankroll: float = 1310.0) -> Dict[str, Any]:
    return {
        "bankroll": bankroll,
        "kelly_fraction": 0.25,
        "per_bet_cap": 0.05,
        "max_stake": bankroll * 0.05,
        "currency": "USD",
    }


def _mixed_fixture_feed() -> List[Dict[str, Any]]:
    """A realistic feed exercising every gate in build_match_singles."""
    return [
        # Clean +EV home pick -> actionable.
        _fix("Actionable vs Team", model_home=0.60, devig_home=0.45),
        # Missing model/market entirely -> whole-fixture drop.
        {"fixture": "Blind vs Spot", "kickoff": "2099-12-31 21:00:00+00:00",
         "group": "X", "generated": "2099-12-31 20:00:00 UTC", "model": {}, "market": {}},
        # Kickoff far in the past -> whole-fixture drop.
        _fix("Past vs Match", kickoff="2000-01-01 00:00:00+00:00"),
        # Below SELECTION_MIN_PROB floor (0.20).
        _fix("Tiny vs Prob", model_home=0.10, devig_home=0.05, model_draw=0.05, model_away=0.05,
             devig_draw=0.02, devig_away=0.02),
        # Longshot filter: model < 0.25 with positive edge.
        _fix("Longshot vs Team", model_home=0.22, devig_home=0.15, model_draw=0.05, model_away=0.05,
             devig_draw=0.02, devig_away=0.02),
        # Edge below the 2pp floor (positive but tiny edge) -> THE gate under review.
        _fix("Thinedge vs Team", model_home=0.51, devig_home=0.50, model_draw=0.05, model_away=0.05,
             devig_draw=0.02, devig_away=0.02),
        # Negative edge -> not even a candidate (model < devig).
        _fix("Negedge vs Team", model_home=0.40, devig_home=0.52, model_draw=0.05, model_away=0.05,
             devig_draw=0.02, devig_away=0.02),
    ]


# ---------------------------------------------------------------------------
# Section 1: every silently-dropped candidate now emits a withheld row with
# a reason_code.
# ---------------------------------------------------------------------------

class TestWithheldCompleteness:
    def test_missing_model_or_market_withheld(self):
        fixs = [{"fixture": "Blind vs Spot", "kickoff": "2099-12-31 21:00:00+00:00",
                 "group": "X", "generated": "2099-12-31 20:00:00 UTC",
                 "model": {}, "market": {}}]
        recs, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        assert recs == []
        codes = {w.get("reason_code") for w in withheld}
        assert br.REASON_MISSING_MODEL_OR_MARKET in codes
        # One row per outcome (home/draw/away) — candidate count preserved.
        rows = [w for w in withheld if w.get("reason_code") == br.REASON_MISSING_MODEL_OR_MARKET]
        assert len(rows) == 3
        assert {r["selection"] for r in rows} == {"home", "draw", "away"}

    def test_kickoff_past_withheld(self):
        fixs = [_fix("Past vs Match", kickoff="2000-01-01 00:00:00+00:00")]
        recs, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        assert recs == []
        rows = [w for w in withheld if w.get("reason_code") == br.REASON_KICKOFF_PAST]
        assert len(rows) == 3

    def test_missing_price_withheld(self):
        fixs = [_fix("Zero vs Prob", model_home=0.0, devig_home=0.50)]
        _, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w
        assert home_w[0]["reason_code"] == br.REASON_MISSING_PRICE

    def test_below_min_prob_reason_code(self):
        fixs = [_fix(model_home=0.10, devig_home=0.05)]
        _, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w
        assert home_w[0]["reason_code"] == br.REASON_BELOW_MIN_PROB

    def test_longshot_filter_reason_code(self):
        fixs = [_fix(model_home=0.22, devig_home=0.15)]
        _, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w
        assert home_w[0]["reason_code"] == br.REASON_LONGSHOT_FILTER

    def test_edge_below_floor_now_withheld_with_reason_code(self):
        """THE gate the review named: previously a bare ``continue``."""
        # model=0.27, devig=0.25 -> edge = 0.02 exactly at... use edge < 0.02
        fixs = [_fix(model_home=0.27, devig_home=0.26, model_draw=0.05, model_away=0.05,
                     devig_draw=0.02, devig_away=0.02)]
        recs, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        home_recs = [r for r in recs if r["selection"] == "home"]
        assert home_recs == []  # still correctly not actionable (behaviour unchanged)
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w, "edge-below-floor candidate must now appear in withheld"
        assert home_w[0]["reason_code"] == br.REASON_EDGE_BELOW_FLOOR
        assert home_w[0]["withheld_reason"].startswith("edge_below_floor:")

    def test_stale_reason_code(self):
        fixs = [_fix(model_home=0.60, devig_home=0.50)]
        stale_age = (br.MODEL_STALE_HOURS + 1) * 3600
        _, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=stale_age)
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w
        assert home_w[0]["reason_code"] == br.REASON_STALE_MODEL

    def test_zero_stake_withheld_with_reason_code(self):
        """A tiny positive edge against a minuscule bankroll rounds stake to 0."""
        fixs = [_fix(model_home=0.51, devig_home=0.485, model_draw=0.05, model_away=0.05,
                     devig_draw=0.02, devig_away=0.02)]
        tiny_pool = _sb_pool(bankroll=0.01)
        recs, withheld = br.build_match_singles(fixs, tiny_pool, set(), [], {}, model_age_secs=10)
        home_recs = [r for r in recs if r["selection"] == "home"]
        assert home_recs == []
        home_w = [w for w in withheld if w.get("selection") == "home"]
        assert home_w
        assert home_w[0]["reason_code"] == br.REASON_ZERO_STAKE

    def test_top3_cap_reason_code(self):
        fixs = [_fix(
            model_home=0.55, devig_home=0.45,
            model_draw=0.35, devig_draw=0.25,
            model_away=0.30, devig_away=0.22,
        )]
        # All 3 outcomes +EV -> at most 3 actionable, nothing capped here since
        # there are only 3 outcomes; assert the reason_code constant exists and
        # is wired (see TestTopThreeAndMoneylineRule in test_betrecs.py for the
        # capping behaviour itself).
        recs, withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        assert len(recs) <= 3
        assert br.REASON_TOP3_CAP == "top3_per_fixture_cap"

    def test_advancement_missing_pm_price_reason_code(self):
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "NoPrice", "group": "A", "model": {"QF": 0.60},
                          "pm": {"QF": {"pm": None, "edge_adj": None}}, "delta": {}}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        assert any(w.get("reason_code") == br.REASON_MISSING_PM_PRICE for w in withheld)

    def test_advancement_missing_model_prob_reason_code(self):
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "NoModel", "group": "A", "model": {},
                          "pm": {"QF": {"pm": 0.40, "edge_adj": 0.05}}, "delta": {}}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        assert any(w.get("reason_code") == br.REASON_MISSING_PM_MODEL_PROB for w in withheld)

    def test_advancement_edge_below_floor_reason_code(self):
        """Advancement side of THE 2pp gate: previously a bare ``continue``."""
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "ThinEdge", "group": "A", "model": {"QF": 0.55},
                          "pm": {"QF": {"pm": 0.545, "edge_adj": 0.001}}, "delta": {}}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        rows = [w for w in withheld if w.get("reason_code") == br.REASON_EDGE_BELOW_FLOOR]
        assert rows
        assert rows[0]["withheld_reason"].startswith("edge_below_floor:")

    def test_advancement_longshot_no_cash_reason_code(self):
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "Longshot", "group": "A", "model": {"QF": 0.20},
                          "pm": {"QF": {"pm": 0.10, "edge_adj": 0.08}}, "delta": {}}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        assert any(w.get("reason_code") == br.REASON_LONGSHOT_NO_CASH for w in withheld)

    def test_advancement_zero_stake_reason_code(self):
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "TinyBankroll", "group": "A", "model": {"QF": 0.60},
                          "pm": {"QF": {"pm": 0.40, "edge_adj": 0.15}}, "delta": {}}]}
        tiny_pool = _pm_pool(bankroll=0.01)
        recs, withheld = br.build_advancement_futures(adv, tiny_pool, adv_age_secs=10)
        assert recs == []
        assert any(w.get("reason_code") == br.REASON_ZERO_STAKE for w in withheld)

    def test_advancement_stale_reason_code(self):
        adv = {"meta": {"generated": "2020-01-01 00:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "OldTeam", "group": "A", "model": {"QF": 0.60},
                          "pm": {"QF": {"pm": 0.45, "edge_adj": 0.10}}, "delta": {}}]}
        stale_age = (br.MODEL_STALE_HOURS + 1) * 3600
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=stale_age)
        assert recs == []
        assert any(w.get("reason_code") == br.REASON_STALE_ADVANCEMENT for w in withheld)

    def test_advancement_resolved_market_reason_code(self):
        """Resolved-market guard (pm price pinned near 0/1): reason_code wired."""
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "AllButOver", "group": "A", "model": {"QF": 0.97},
                          "pm": {"QF": {"pm": 0.99, "edge_adj": -0.02}}, "delta": {}}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        rows = [w for w in withheld if w.get("reason_code") == br.REASON_RESOLVED_MARKET]
        assert rows
        assert "resolved market" in rows[0]["withheld_reason"]

    def test_advancement_state_stale_reason_code(self):
        """Per-team state-freshness guard (KO tie kicked off but not pinned
        in the sim's conditioning set): reason_code wired."""
        adv = {"meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF"], "n_pm_markets": 12},
               "teams": [{"team": "PhantomTeam", "group": "A", "model": {"QF": 0.60},
                          "pm": {"QF": {"pm": 0.40, "edge_adj": 0.15}}, "delta": {},
                          "state_stale_reason": "eliminated 2026-07-06 — sim not yet re-conditioned"}]}
        recs, withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        assert recs == []
        rows = [w for w in withheld if w.get("reason_code") == br.REASON_STATE_STALE]
        assert rows
        assert rows[0]["withheld_reason"] == "eliminated 2026-07-06 — sim not yet re-conditioned"

    def test_event_props_reason_codes(self):
        prop_cal = {"fixtures": [{"fixture": "X vs Y", "corners": {"mean": 9.0}}]}
        _, withheld = br.build_event_props(
            prop_cal=prop_cal, model_predictions=[], sb_pool=_sb_pool(),
            price_age_secs=None, model_age_secs=60,
        )
        corner_w = [w for w in withheld if w.get("market") == "corners"]
        assert corner_w
        assert corner_w[0]["reason_code"] == br.REASON_NO_LIVE_PRICE
        scorer_w = [w for w in withheld if w.get("market") == "anytime_scorer"]
        assert scorer_w
        assert scorer_w[0]["reason_code"] == br.REASON_UNSUPPORTED

    def test_every_withheld_row_in_mixed_feed_has_a_reason_code(self):
        """No withheld row should ever lack a reason_code going forward."""
        _, withheld = br.build_match_singles(
            _mixed_fixture_feed(), _sb_pool(), set(), [], {}, model_age_secs=10,
        )
        assert withheld, "fixture feed should produce withheld rows"
        missing = [w for w in withheld if not w.get("reason_code")]
        assert missing == [], "withheld rows without reason_code: %r" % missing


# ---------------------------------------------------------------------------
# Section 2: actionable-output invariance — the most important test.
#
# Telemetry must be purely additive: the ACTIONABLE feed (what a human can
# act on) is byte-identical before and after this change, on a realistic
# mixed feed hitting every gate. Only the withheld list may differ (grow).
# ---------------------------------------------------------------------------

class TestActionableOutputInvariance:
    def test_match_singles_actionable_identical_to_baseline(self, br_baseline):
        fixs = _mixed_fixture_feed()
        new_recs, new_withheld = br.build_match_singles(
            fixs, _sb_pool(), set(), [], {}, model_age_secs=10,
        )
        old_recs, old_withheld = br_baseline.build_match_singles(
            fixs, _sb_pool(), set(), [], {}, model_age_secs=10,
        )
        assert new_recs == old_recs, (
            "actionable match_singles changed by telemetry-only edit:\n"
            "old=%r\nnew=%r" % (old_recs, new_recs)
        )
        # The withheld list must GROW (previously-silent drops now appear) but
        # never shrink relative to baseline.
        assert len(new_withheld) >= len(old_withheld)

    def test_advancement_actionable_identical_to_baseline(self, br_baseline):
        adv = {
            "meta": {"generated": "2099-12-31 20:00:00 UTC", "stages": ["QF", "SF"], "n_pm_markets": 12},
            "teams": [
                {"team": "Brazil", "group": "A", "model": {"QF": 0.70, "SF": 0.45},
                 "pm": {"QF": {"pm": 0.45, "edge_adj": 0.20}, "SF": {"pm": 0.30, "edge_adj": 0.12}},
                 "delta": {}},
                {"team": "ThinEdgeTeam", "group": "B", "model": {"QF": 0.55},
                 "pm": {"QF": {"pm": 0.545, "edge_adj": 0.001}}, "delta": {}},
                {"team": "NoPriceTeam", "group": "C", "model": {"QF": 0.60},
                 "pm": {"QF": {"pm": None, "edge_adj": None}}, "delta": {}},
                {"team": "LongshotTeam", "group": "D", "model": {"QF": 0.20},
                 "pm": {"QF": {"pm": 0.10, "edge_adj": 0.08}}, "delta": {}},
            ],
        }
        new_recs, new_withheld = br.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        old_recs, old_withheld = br_baseline.build_advancement_futures(adv, _pm_pool(), adv_age_secs=10)
        # Side-aware position fields (fix 2026-07-14) are ADDITIVE on every
        # actionable row: side / position_prob / position_bucket /
        # position_price. Strip them so this pin keeps proving that every
        # field the baseline emitted is untouched — this all-YES fixture must
        # still match the baseline's gates, EV, stakes and ordering exactly.
        _additive_2026_07_14 = {"side", "position_prob", "position_bucket",
                                "position_price"}
        stripped = [{k: v for k, v in r.items() if k not in _additive_2026_07_14}
                    for r in new_recs]
        assert stripped == old_recs, (
            "actionable advancement_futures changed beyond the documented "
            "additive side-aware fields:\n"
            "old=%r\nnew=%r" % (old_recs, new_recs)
        )
        assert len(new_withheld) >= len(old_withheld)

    def test_event_props_actionable_identical_to_baseline(self, br_baseline):
        prop_cal = {"fixtures": [{"fixture": "X vs Y", "corners": {"mean": 9.0}, "cards": {"mean": 3.0}}]}
        new_recs, _ = br.build_event_props(prop_cal, [], _sb_pool(), None, 60)
        old_recs, _ = br_baseline.build_event_props(prop_cal, [], _sb_pool(), None, 60)
        assert new_recs == old_recs == []

    def test_existing_withheld_reason_free_text_unchanged(self, br_baseline):
        """``withheld_reason`` (read by site/arb.js) must be byte-identical for
        every row that ALREADY existed in the baseline withheld output — only
        NEW rows (previously-silent drops) and the additive reason_code field
        may differ."""
        fixs = _mixed_fixture_feed()
        _, new_withheld = br.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)
        _, old_withheld = br_baseline.build_match_singles(fixs, _sb_pool(), set(), [], {}, model_age_secs=10)

        old_by_id = {w["id"]: w for w in old_withheld}
        new_by_id: Dict[str, List[Dict[str, Any]]] = {}
        for w in new_withheld:
            new_by_id.setdefault(w["id"], []).append(w)

        for wid, old_row in old_by_id.items():
            matches = new_by_id.get(wid, [])
            assert matches, "baseline withheld id %r missing from new output" % wid
            same_reason = [m for m in matches if m.get("withheld_reason") == old_row.get("withheld_reason")]
            assert same_reason, (
                "withheld_reason text changed for id %r: old=%r new=%r"
                % (wid, old_row.get("withheld_reason"), [m.get("withheld_reason") for m in matches])
            )
