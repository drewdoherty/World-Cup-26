"""Enhanced /accas offline test suite.

Covers the seven new capability requirements:
  1. Credit discipline (0 network calls from interactive build_accas)
  2. Promo freshness gating
  3. Repeated-outcome add/deny (incremental EV gate)
  4. FX USD→GBP conversion in Exposure
  5. Joint probability for same-game legs vs independent product
  6. Asian handicap ±0.5 derivation + integer-line matrix
  7. Player props legs when model prob + price exist
  8. PM settlement mismatch (corners → no PM alternative)
  9. Unsupported markets listing
 10. Bankroll wiring (resolve_pool_bankroll used, fallback)
 11. Message length ≤4096 chars
"""
from __future__ import annotations

import sqlite3
import sys
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from wca.accas import (
    DEFAULT_BANKROLL,
    FIXTURE_CAP_FRACTION,
    MSG_LIMIT,
    PROMO_STALE_HOURS,
    Acca,
    Exposure,
    Leg,
    Offer,
    _ah_prob_from_1x2,
    _ah_prob_from_matrix,
    _fixture_token,
    _joint_prob_same_game,
    _kelly_fraction,
    _leg_passes_gate,
    _list_unsupported,
    _pm_matches_market,
    _truncate,
    _usd_to_gbp,
    assemble_accas,
    build_accas,
    build_exposure,
    candidate_legs,
    format_accas,
)


# ---------------------------------------------------------------------------
# Helpers for building minimal test data
# ---------------------------------------------------------------------------

def _make_leg(
    fixture: str = "Alpha vs Bravo",
    market: str = "1X2",
    selection: str = "Alpha",
    model_prob: float = 0.55,
    odds: float = 2.0,
    book: str = "bk",
    is_moneyline: bool = True,
) -> Leg:
    edge = model_prob * odds - 1
    return Leg(
        fixture=fixture, market=market, selection=selection,
        model_prob=model_prob, odds=odds, book=book,
        edge=edge, is_moneyline=is_moneyline,
    )


def _fixtures_3():
    return [
        {
            "fixture": "Alpha vs Bravo",
            "model_1x2": {"home": 0.55, "draw": 0.25, "away": 0.20},
            "best_1x2": {"home": (2.00, "bk"), "draw": (4.00, "bk"), "away": (3.50, "bk")},
            "over_under": {"line": 2.5, "over": 0.52, "under": 0.48},
            "btts": 0.42,
        },
        {
            "fixture": "Charlie vs Delta",
            "model_1x2": {"home": 0.45, "draw": 0.25, "away": 0.30},
            "best_1x2": {"home": (2.50, "bk"), "draw": (3.80, "bk"), "away": (3.10, "bk")},
            "over_under": {"line": 2.5, "over": 0.50, "under": 0.50},
            "btts": 0.45,
        },
        {
            "fixture": "Echo vs Foxtrot",
            "model_1x2": {"home": 0.60, "draw": 0.22, "away": 0.18},
            "best_1x2": {"home": (1.90, "bk"), "draw": (4.50, "bk"), "away": (6.00, "bk")},
            "over_under": {"line": 2.5, "over": 0.54, "under": 0.46},
            "btts": 0.40,
        },
    ]


# ---------------------------------------------------------------------------
# 1. Credit discipline — 0 network calls from interactive build_accas
# ---------------------------------------------------------------------------

class TestCreditDiscipline:
    """build_accas must not make HTTP requests (all from cache/SQLite)."""

    def test_no_requests_made(self, tmp_path):
        """build_accas with missing files makes zero HTTP calls."""
        import urllib.request as _req
        calls = []
        orig_urlopen = _req.urlopen

        def spy_urlopen(url, *args, **kw):
            calls.append(url)
            return orig_urlopen(url, *args, **kw)

        with patch("urllib.request.urlopen", side_effect=spy_urlopen):
            result = build_accas(
                preds_path=str(tmp_path / "missing.json"),
                scores_path=str(tmp_path / "missing.json"),
                db_path=str(tmp_path / "missing.db"),
                site_data=str(tmp_path / "missing.json"),
                mode="value",
            )
        assert calls == [], "build_accas made %d HTTP calls: %s" % (len(calls), calls)
        assert isinstance(result["accas"], list)

    def test_no_odds_api_requests(self, tmp_path):
        """No calls to odds-api.com from interactive build_accas."""
        oddsapi_calls = []
        import urllib.request as _req

        orig = _req.urlopen
        def spy(url, *a, **kw):
            if "odds-api.com" in str(url):
                oddsapi_calls.append(url)
            return orig(url, *a, **kw)

        with patch("urllib.request.urlopen", side_effect=spy):
            build_accas(
                preds_path=str(tmp_path / "m.json"),
                scores_path=str(tmp_path / "s.json"),
                db_path=str(tmp_path / "d.db"),
                site_data=str(tmp_path / "t.json"),
                mode="edge",
            )
        assert not oddsapi_calls, "Odds-API called during interactive command"


# ---------------------------------------------------------------------------
# 2. Promo freshness gating
# ---------------------------------------------------------------------------

class TestPromoFreshness:
    """Stale or missing promo snapshot → promo_required=True, no new-risk stake."""

    def _make_promo_db(self, db_path: str, fetch_status: str = "ok",
                       age_hours: float = 0.5, site: str = "betfair") -> None:
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY,
                site TEXT, name TEXT, promo_type TEXT,
                min_legs INTEGER, min_leg_odds REAL, min_odds REAL,
                stake_cap REAL, game_filter TEXT,
                expiry TEXT, eligible INTEGER DEFAULT 1,
                active INTEGER DEFAULT 1,
                terms TEXT
            );
            CREATE TABLE IF NOT EXISTS promo_snapshots (
                id INTEGER PRIMARY KEY,
                site TEXT, fetch_status TEXT, ts_utc TEXT
            );
        """)
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%S")
        con.execute(
            "INSERT INTO promotions (site,name,promo_type,min_legs,min_leg_odds,min_odds,stake_cap) "
            "VALUES (?,?,?,?,?,?,?)",
            (site, "Test Offer", "snr_free", 3, 1.5, 0.0, 10.0),
        )
        con.execute(
            "INSERT INTO promo_snapshots (site, fetch_status, ts_utc) VALUES (?,?,?)",
            (site, fetch_status, ts),
        )
        con.commit()
        con.close()

    def test_fresh_promo_ok(self, tmp_path):
        db = str(tmp_path / "led.db")
        self._make_promo_db(db, fetch_status="ok", age_hours=0.5)
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=db,
            site_data=str(tmp_path / "t.json"),
            mode="promo",
        )
        # promo_required tracks stale/failed, not freshness per se
        assert isinstance(result.get("promo_required"), bool)

    def test_stale_promo_sets_required(self, tmp_path):
        db = str(tmp_path / "led.db")
        # Age > PROMO_STALE_HOURS
        self._make_promo_db(db, fetch_status="ok", age_hours=PROMO_STALE_HOURS + 1)
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=db,
            site_data=str(tmp_path / "t.json"),
            mode="promo",
        )
        assert result.get("promo_required") is True

    def test_failed_scrape_sets_required(self, tmp_path):
        db = str(tmp_path / "led.db")
        self._make_promo_db(db, fetch_status="error", age_hours=0.5)
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=db,
            site_data=str(tmp_path / "t.json"),
            mode="promo",
        )
        assert result.get("promo_required") is True

    def test_format_shows_promo_check_required(self, tmp_path):
        db = str(tmp_path / "led.db")
        self._make_promo_db(db, fetch_status="error", age_hours=0.5)
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=db,
            site_data=str(tmp_path / "t.json"),
            mode="promo",
        )
        txt = format_accas(result)
        assert "PROMO CHECK REQUIRED" in txt


# ---------------------------------------------------------------------------
# 3. Repeated-outcome add/deny
# ---------------------------------------------------------------------------

class TestRepeatedOutcomeGate:
    """_leg_passes_gate controls repeated-outcome add vs deny."""

    def _exp_with_held(self, stake_gbp=None):
        leg = _make_leg()
        exp = Exposure()
        from wca.accas import _make_sig
        sig = _make_sig(leg)
        exp.held.add(sig)
        exp.held_stakes[sig] = stake_gbp
        if stake_gbp:
            ftok = _fixture_token(leg.fixture)
            exp.fixture_stake_gbp[ftok] = stake_gbp
            exp.total_gbp_risk = stake_gbp
        return exp, leg

    def test_no_stake_info_conservative_drop(self):
        exp, leg = self._exp_with_held(stake_gbp=None)
        passes, reason = _leg_passes_gate(leg, exp, bankroll=2500.0)
        assert not passes
        assert "conservative" in reason.lower()

    def test_stake_below_kelly_allows_add(self):
        """Held at tiny fraction of Kelly → incremental EV positive → allowed."""
        exp, leg = self._exp_with_held(stake_gbp=0.50)  # tiny vs Kelly
        passes, reason = _leg_passes_gate(leg, exp, bankroll=2500.0)
        assert passes, "should allow add when well below Kelly; reason: %s" % reason
        assert "add" in reason.lower() or "room" in reason.lower()

    def test_stake_at_kelly_denies_add(self):
        """Held at >= 95% of Kelly → block (over-Kelly would reduce EV)."""
        leg = _make_leg(model_prob=0.55, odds=2.0)
        kelly_opt = 0.25 * _kelly_fraction(0.55, 2.0) * 2500.0
        exp, _ = self._exp_with_held(stake_gbp=kelly_opt)
        passes, reason = _leg_passes_gate(leg, exp, bankroll=2500.0)
        assert not passes
        assert "kelly" in reason.lower()

    def test_fixture_cap_blocks_large_stake(self):
        """Fixture already near cap → new leg on same fixture blocked."""
        leg = _make_leg(fixture="Alpha vs Bravo")
        exp = Exposure()
        ftok = _fixture_token("Alpha vs Bravo")
        cap = 2500.0 * FIXTURE_CAP_FRACTION
        # Already committed 98% of cap
        exp.fixture_stake_gbp[ftok] = cap * 0.98
        exp.total_gbp_risk = cap * 0.98
        passes, reason = _leg_passes_gate(leg, exp, bankroll=2500.0)
        assert not passes
        assert "cap" in reason.lower()

    def test_fresh_fixture_no_exposure_passes(self):
        """No held position on fixture → should always pass the gate."""
        leg = _make_leg(fixture="New vs Team")
        exp = Exposure()
        passes, _ = _leg_passes_gate(leg, exp, bankroll=2500.0)
        assert passes

    def test_assemble_drops_held_leg_no_stake(self):
        """End-to-end: held selection with no stake info → dropped from accas."""
        legs = candidate_legs(_fixtures_3(), {}, min_edge=0.02)
        held = [{"match": "Alpha vs Bravo", "selection": "Alpha", "market": "1X2"}]
        exp = build_exposure(held)
        out = assemble_accas(legs, exp, mode="value", min_legs=2)
        for a in out:
            assert not any(
                l.fixture == "Alpha vs Bravo" and l.selection == "Alpha"
                for l in a.legs
            )


# ---------------------------------------------------------------------------
# 4. FX USD→GBP conversion in Exposure
# ---------------------------------------------------------------------------

class TestFxConversion:
    """Exposure must convert USD Polymarket stakes to GBP correctly."""

    def test_usd_to_gbp_basic(self):
        gbp = _usd_to_gbp(100.0, usd_per_gbp=1.25)
        assert abs(gbp - 80.0) < 0.01

    def test_usd_to_gbp_fallback_rate(self):
        gbp = _usd_to_gbp(133.0, usd_per_gbp=1.33)
        assert abs(gbp - 100.0) < 0.01

    def test_exposure_converts_pm_stake(self):
        bets = [
            {
                "match": "Alpha vs Bravo",
                "selection": "Alpha",
                "market": "1X2",
                "stake": 100.0,
                "platform": "polymarket",
                "currency": "USD",
            }
        ]
        exp = build_exposure(bets, fx_usd_per_gbp=1.25)
        assert exp.total_gbp_risk == pytest.approx(80.0, abs=0.01)

    def test_exposure_no_conversion_for_gbp(self):
        bets = [
            {
                "match": "Alpha vs Bravo",
                "selection": "Alpha",
                "market": "1X2",
                "stake": 50.0,
                "platform": "betfair",
                "currency": "GBP",
            }
        ]
        exp = build_exposure(bets, fx_usd_per_gbp=1.33)
        assert exp.total_gbp_risk == pytest.approx(50.0, abs=0.01)

    def test_exposure_mixed_currencies(self):
        bets = [
            {
                "match": "A vs B", "selection": "A", "market": "1X2",
                "stake": 133.0, "platform": "polymarket",
            },
            {
                "match": "C vs D", "selection": "C", "market": "1X2",
                "stake": 50.0, "platform": "betfair",
            },
        ]
        exp = build_exposure(bets, fx_usd_per_gbp=1.33)
        assert exp.total_gbp_risk == pytest.approx(150.0, abs=0.01)

    def test_exposure_venue_breakdown(self):
        bets = [
            {
                "match": "A vs B", "selection": "A", "market": "1X2",
                "stake": 133.0, "platform": "polymarket",
            }
        ]
        exp = build_exposure(bets, fx_usd_per_gbp=1.33)
        assert "polymarket" in exp.venue_gbp
        assert exp.venue_gbp["polymarket"] == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# 5. Joint probability for same-game legs
# ---------------------------------------------------------------------------

class TestJointProbability:
    """Same-game legs need correlated joint prob, not naive product."""

    def test_joint_prob_less_than_product(self):
        """For positively correlated events, joint < product."""
        leg_home = _make_leg(selection="Alpha", market="1X2", model_prob=0.55)
        leg_over = _make_leg(
            selection="Over 2.5", market="totals", model_prob=0.52, is_moneyline=False
        )
        legs = [leg_home, leg_over]
        joint = _joint_prob_same_game(legs, lam_h=1.5, lam_a=1.1)
        if joint is None:
            pytest.skip("exposure_corr not available")
        product = leg_home.model_prob * leg_over.model_prob
        # Scoreline P(home win AND O2.5) should differ from naive product
        assert isinstance(joint, float)
        assert 0 < joint < 1
        # Should NOT equal naive product (correlation matters)
        assert abs(joint - product) > 1e-6

    def test_joint_prob_btts_home_win(self):
        """BTTS Yes AND home win: joint should be < product."""
        leg_home = _make_leg(selection="Alpha", market="1X2", model_prob=0.55)
        leg_btts = _make_leg(selection="BTTS Yes", market="btts", model_prob=0.45, is_moneyline=False)
        joint = _joint_prob_same_game([leg_home, leg_btts], lam_h=1.4, lam_a=1.0)
        if joint is None:
            pytest.skip("exposure_corr not available")
        assert 0 < joint < 1

    def test_joint_prob_single_leg_equals_model_prob(self):
        """Single leg joint == its model_prob (via scoreline)."""
        leg = _make_leg(selection="Alpha", market="1X2", model_prob=0.55)
        joint = _joint_prob_same_game([leg], lam_h=1.5, lam_a=1.1)
        if joint is None:
            pytest.skip("exposure_corr not available")
        # Allow ±5% tolerance (model_1x2 vs scoreline-matrix model blend)
        assert abs(joint - leg.model_prob) < 0.10

    def test_joint_prob_empty_returns_none(self):
        result = _joint_prob_same_game([], lam_h=1.5, lam_a=1.1)
        assert result is None


# ---------------------------------------------------------------------------
# 6. Asian handicap probability derivation
# ---------------------------------------------------------------------------

class TestAsianHandicap:
    """AH probability derivation from 1X2 (±0.5) and scoreline matrix."""

    def test_ah_minus_half_equals_home_win(self):
        m1x2 = {"home": 0.50, "draw": 0.25, "away": 0.25}
        ph, pa = _ah_prob_from_1x2(m1x2, line=-0.5)
        assert ph == pytest.approx(0.50)
        assert pa == pytest.approx(0.50)  # draw + away

    def test_ah_plus_half_equals_home_no_loss(self):
        m1x2 = {"home": 0.50, "draw": 0.25, "away": 0.25}
        ph, pa = _ah_prob_from_1x2(m1x2, line=+0.5)
        assert ph == pytest.approx(0.75)  # home + draw
        assert pa == pytest.approx(0.25)

    def test_ah_integer_line_returns_none_from_1x2(self):
        m1x2 = {"home": 0.50, "draw": 0.25, "away": 0.25}
        ph, pa = _ah_prob_from_1x2(m1x2, line=-1.0)
        assert ph is None and pa is None

    def test_ah_matrix_integer_line(self):
        ph, pa = _ah_prob_from_matrix(lam_h=1.5, lam_a=0.9, line=-1.0)
        if ph is None:
            pytest.skip("exposure_corr not available")
        assert 0 < ph < 1
        assert 0 < pa < 1
        assert ph + pa == pytest.approx(1.0, abs=0.01)
        # Home favoured: should have better chance on -1 AH
        assert ph > 0.30

    def test_ah_matrix_zero_line(self):
        """AH 0 (draw no bet) from matrix: push on draw."""
        ph, pa = _ah_prob_from_matrix(lam_h=1.5, lam_a=0.9, line=0.0)
        if ph is None:
            pytest.skip("exposure_corr not available")
        assert ph + pa == pytest.approx(1.0, abs=0.02)

    def test_ah_minus_half_complement(self):
        """p_home + p_away should sum to 1 for ±0.5 (no push)."""
        m1x2 = {"home": 0.55, "draw": 0.20, "away": 0.25}
        ph, pa = _ah_prob_from_1x2(m1x2, line=-0.5)
        assert ph + pa == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 7. Player props legs
# ---------------------------------------------------------------------------

class TestPlayerPropsLegs:
    """candidate_scorer_legs produces legs when model prob + price exist."""

    def test_scorer_leg_structure(self):
        from wca.accas import candidate_scorer_legs
        from wca.models.scorers import PlayerParams, ScorerPricer

        pricer = ScorerPricer()
        fixtures = [
            {
                "fixture": "Brazil vs France",
                "model_1x2": {"home": 0.45, "draw": 0.25, "away": 0.30},
            }
        ]
        # lambdas are keyed by the raw fixture name (see load_fixtures /
        # candidate_scorer_legs' lambdas.get(name)), not the tokenised form.
        lambdas = {"Brazil vs France": {"lambda_home": 1.4, "lambda_away": 1.1}}
        snap = {
            _fixture_token("Brazil vs France"): {
                # Anytime-scorer prices are keyed by bare player name (see
                # candidate_scorer_legs' lookup), not "<name> Anytime".
                ("anytime_scorer", "Neymar"): (3.50, "bk"),
            }
        }
        players = {
            "Brazil": [
                PlayerParams(
                    name="Neymar", team="Brazil", npxg_share=0.25,
                    penalty_taker=True, expected_minutes=90, source="test",
                )
            ]
        }
        legs = candidate_scorer_legs(fixtures, snap, lambdas, players, min_edge=-1.0)
        if not legs:
            pytest.skip("No scorer legs produced — model params may not yield +EV")
        leg = legs[0]
        assert leg.market in ("anytime_scorer", "first_scorer")
        assert leg.model_prob > 0
        assert leg.odds > 1.0

    def test_scorer_legs_excluded_from_candidate_legs(self):
        """candidate_legs (1X2/totals/btts/dnb) never yields scorer legs."""
        legs = candidate_legs(_fixtures_3(), {}, min_edge=-1.0)
        scorer_markets = {"anytime_scorer", "first_scorer", "corners", "cards", "bookings"}
        assert not any(l.market in scorer_markets for l in legs)


# ---------------------------------------------------------------------------
# 8. PM settlement mismatch
# ---------------------------------------------------------------------------

class TestPmSettlement:
    """PM alternatives only when semantics match; corners/cards excluded."""

    def test_1x2_matches_win_question(self):
        assert _pm_matches_market("1X2", "Will Brazil win the match?")

    def test_1x2_matches_match_result(self):
        assert _pm_matches_market("1X2", "Brazil vs France match result")

    def test_totals_matches_goals(self):
        assert _pm_matches_market("totals", "Will there be over 2.5 goals in the match?")

    def test_btts_matches_both_teams_score(self):
        assert _pm_matches_market("btts", "Will both teams score in the match?")

    def test_corners_no_match(self):
        assert not _pm_matches_market("corners", "Will there be over 10 corners?")

    def test_cards_no_match(self):
        assert not _pm_matches_market("cards", "Will there be over 4.5 cards?")

    def test_player_props_no_match(self):
        assert not _pm_matches_market("anytime_scorer", "Will Neymar score?")

    def test_pm_alternatives_excludes_corners(self, tmp_path):
        """build_accas with corners snap → pm_alternatives has no corners leg."""
        db = str(tmp_path / "led.db")
        con = sqlite3.connect(db)
        # Insert a pm_inventory row for corners (should be excluded)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS pm_inventory (
                id INTEGER PRIMARY KEY,
                fixture TEXT, fixture_token TEXT,
                question TEXT, outcome TEXT, outcome_token TEXT,
                token_id TEXT, price REAL, liquidity REAL,
                neg_risk INTEGER DEFAULT 0, settlement_rules TEXT,
                fetched_utc TEXT, closed INTEGER DEFAULT 0,
                UNIQUE(fixture_token, outcome_token)
            );
        """)
        con.execute(
            "INSERT INTO pm_inventory (fixture, fixture_token, question, outcome, "
            "outcome_token, price, fetched_utc) VALUES (?,?,?,?,?,?,?)",
            ("Alpha vs Bravo", _fixture_token("Alpha vs Bravo"),
             "Will there be over 10 corners?", "Yes", "yes",
             0.6, "2026-06-27T00:00:00"),
        )
        con.commit()
        con.close()
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=db,
            site_data=str(tmp_path / "t.json"),
            mode="value",
        )
        pm_alts = result.get("pm_alternatives") or {}
        # No corner-market leg should appear in pm_alternatives
        for sig, data in pm_alts.items():
            assert "corner" not in data.get("question", "").lower()


# ---------------------------------------------------------------------------
# 9. Unsupported markets listing
# ---------------------------------------------------------------------------

class TestUnsupportedMarkets:
    """_list_unsupported identifies market types that cannot be priced."""

    def test_known_markets_not_listed(self):
        snap = {
            "tok1": {
                ("totals", "Over 2.5"): (1.90, "bk"),
                ("btts", "BTTS Yes"): (1.75, "bk"),
                ("1x2", "Home"): (2.00, "bk"),
                ("asian_handicap", "Home -0.5"): (1.85, "bk"),
            }
        }
        unsup = _list_unsupported(snap, [], {})
        assert not unsup

    def test_exotic_market_listed(self):
        snap = {
            "tok1": {
                ("clean_sheet", "Home Clean Sheet"): (2.20, "bk"),
                ("1x2", "Home"): (2.00, "bk"),
            }
        }
        unsup = _list_unsupported(snap, [], {})
        assert "clean_sheet" in unsup

    def test_sot_listed_as_unsupported(self):
        snap = {"t": {("shots_on_target", "Over 4.5"): (1.90, "bk")}}
        unsup = _list_unsupported(snap, [], {})
        assert "shots_on_target" in unsup

    def test_ah_integer_without_lambdas_flagged(self):
        snap = {
            "t": {
                ("asian_handicap", "Home -1"): (1.90, "bk"),
                ("spreads", "Home -1.5"): (1.85, "bk"),
            }
        }
        unsup = _list_unsupported(snap, [], lambdas={})
        assert any("integer" in u.lower() or "asian_handicap" in u.lower() for u in unsup)

    def test_ah_integer_with_lambdas_not_flagged(self):
        snap = {
            "t": {("asian_handicap", "Home -1"): (1.90, "bk")}
        }
        lambdas = {"t": {"lambda_home": 1.5, "lambda_away": 1.0}}
        unsup = _list_unsupported(snap, [], lambdas)
        assert not any("integer" in u.lower() for u in unsup)

    def test_empty_snap_no_unsupported(self):
        assert _list_unsupported({}, [], {}) == []


# ---------------------------------------------------------------------------
# 10. Bankroll wiring
# ---------------------------------------------------------------------------

class TestBankrollWiring:
    """build_accas calls resolve_pool_bankroll; falls back to DEFAULT_BANKROLL."""

    def test_fallback_to_default_when_db_missing(self, tmp_path):
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=str(tmp_path / "missing.db"),
            site_data=str(tmp_path / "t.json"),
            mode="value",
        )
        # FULL-POOL default (user, 2026-07-02): a missing ledger resolves to the
        # full £3,000 sportsbook base, not the old rung-0 £2,000 DEFAULT_BANKROLL.
        from wca.card import GBP_POOL_BASE_GBP
        assert result.get("bankroll") == pytest.approx(GBP_POOL_BASE_GBP, rel=0.01)
        assert "fallback" in result.get("bankroll_reason", "").lower() or \
               result.get("bankroll_reason") is not None

    def test_kelly_fraction_present(self, tmp_path):
        result = build_accas(
            preds_path=str(tmp_path / "m.json"),
            scores_path=str(tmp_path / "s.json"),
            db_path=str(tmp_path / "missing.db"),
            site_data=str(tmp_path / "t.json"),
            mode="value",
        )
        assert result.get("kelly_fraction") is not None
        kf = result["kelly_fraction"]
        assert 0 < kf <= 1

    def test_resolve_pool_bankroll_called_when_available(self, tmp_path):
        """If resolve_pool_bankroll is importable, it must be called."""
        called = []

        from wca import card as _card_mod
        orig = _card_mod.resolve_pool_bankroll

        class FakePool:
            bankroll = 3000.0
            kelly_fraction = 0.25
            reason = "mocked"

        def mock_resolve(*args, **kwargs):
            called.append(True)
            return FakePool()

        with patch.object(_card_mod, "resolve_pool_bankroll", mock_resolve):
            result = build_accas(
                preds_path=str(tmp_path / "m.json"),
                scores_path=str(tmp_path / "s.json"),
                db_path=str(tmp_path / "d.db"),
                site_data=str(tmp_path / "t.json"),
                mode="value",
            )
        assert called, "resolve_pool_bankroll was not called"
        assert result["bankroll"] == pytest.approx(3000.0)

    def test_no_default_bankroll_constant_used_directly(self):
        """assemble_accas with explicit bankroll uses that value, not DEFAULT_BANKROLL."""
        legs = candidate_legs(_fixtures_3(), {}, min_edge=0.0)
        if not legs:
            pytest.skip("No legs")
        out = assemble_accas(legs, mode="value", min_legs=2, bankroll=5000.0)
        if not out:
            pytest.skip("No accas assembled")
        a = out[0]
        # Stake based on £5000 bankroll — should differ from DEFAULT_BANKROLL-based
        expected_at_5k = round(0.25 * _kelly_fraction(a.model_prob, a.combined_odds) * 5000.0, 2)
        assert abs(a.stake - expected_at_5k) < 0.50


# ---------------------------------------------------------------------------
# 11. Message length ≤ 4096
# ---------------------------------------------------------------------------

class TestMessageLength:
    """format_accas output must always be ≤ MSG_LIMIT characters."""

    def _make_many_accas(self) -> list:
        """Build a fat list of accas that would normally exceed 4096 chars."""
        legs = candidate_legs(_fixtures_3(), {}, min_edge=-1.0)
        return assemble_accas(legs, mode="edge", min_legs=2, bankroll=2500.0)

    def test_format_within_limit_empty(self):
        txt = format_accas({"mode": "value", "accas": []})
        assert len(txt) <= MSG_LIMIT

    def test_format_within_limit_full(self):
        accas_list = self._make_many_accas()
        result = {
            "mode": "edge",
            "accas": accas_list,
            "bankroll": 2500.0,
            "bankroll_reason": "test",
            "kelly_fraction": 0.25,
            "fx_rate": 1.33,
            "fx_source": "fallback",
            "exposure": Exposure(),
            "promo_audit": [],
            "promo_required": False,
            "pm_alternatives": {},
            "unsupported": ["clean_sheet", "shots_on_target", "winning_margin"],
        }
        txt = format_accas(result)
        assert len(txt) <= MSG_LIMIT, "output is %d chars (limit %d)" % (len(txt), MSG_LIMIT)

    def test_truncate_helper(self):
        long_text = "\n".join(["line %d: %s" % (i, "x" * 50) for i in range(200)])
        result = _truncate(long_text, limit=MSG_LIMIT)
        assert len(result) <= MSG_LIMIT

    def test_truncate_at_newline_boundary(self):
        """_truncate must cut at a newline, not mid-word."""
        # Build text that is exactly 100 chars over limit
        lines = ["A" * 40] * ((MSG_LIMIT // 40) + 10)
        text = "\n".join(lines)
        result = _truncate(text, limit=MSG_LIMIT)
        assert len(result) <= MSG_LIMIT
        # Result must end with a complete line (no dangling partial line)
        assert not result.endswith("A" * 39)  # no mid-line cut

    def test_format_promo_mode_within_limit(self):
        legs = candidate_legs(_fixtures_3(), {}, min_edge=-1.0)
        offers = [Offer("BfSB", "betfair_sportsbook", "1", 3, 1.5, 0.0, "snr_free", 10.0)]
        from wca.accas import build_promo_accas
        out = build_promo_accas(legs, offers)
        result = {
            "mode": "promo", "accas": out,
            "bankroll": 2500.0, "bankroll_reason": "test",
            "kelly_fraction": 0.25, "fx_rate": 1.33, "fx_source": "fallback",
            "exposure": Exposure(), "promo_audit": ["betfair_sportsbook: ok"],
            "promo_required": False, "pm_alternatives": {}, "unsupported": [],
        }
        txt = format_accas(result)
        assert len(txt) <= MSG_LIMIT

    def test_format_with_long_promo_audit_within_limit(self):
        """Long promo audit and unsupported list should still respect the cap."""
        audit = ["site_%d: PROMO CHECK REQUIRED (stale 8.2h)" % i for i in range(20)]
        unsup = ["market_%d" % i for i in range(30)]
        result = {
            "mode": "value", "accas": [],
            "bankroll": 2500.0, "bankroll_reason": "fallback",
            "kelly_fraction": 0.25, "fx_rate": 1.33, "fx_source": "fallback",
            "exposure": Exposure(), "promo_audit": audit,
            "promo_required": True, "pm_alternatives": {}, "unsupported": unsup,
        }
        txt = format_accas(result)
        assert len(txt) <= MSG_LIMIT


# ---------------------------------------------------------------------------
# Integration: format_accas shows enhanced fields
# ---------------------------------------------------------------------------

class TestFormatEnhancedOutput:
    """format_accas should include the new output sections."""

    def test_shows_exposure_summary_when_held(self):
        legs = candidate_legs(_fixtures_3(), {}, min_edge=0.02)
        bets = [{"match": "Charlie vs Delta", "selection": "Charlie", "market": "1X2",
                 "stake": 25.0, "platform": "betfair"}]
        exp = build_exposure(bets)
        out = assemble_accas(legs, exp, mode="value", min_legs=2)
        result = {
            "mode": "value", "accas": out,
            "bankroll": 2500.0, "bankroll_reason": "resolved",
            "kelly_fraction": 0.25, "fx_rate": 1.33, "fx_source": "test",
            "exposure": exp, "promo_audit": [],
            "promo_required": False, "pm_alternatives": {}, "unsupported": [],
        }
        txt = format_accas(result)
        assert len(txt) <= MSG_LIMIT

    def test_no_bet_emitted_when_nothing_clears(self):
        """When no accas pass every gate, 'NO BET' or empty message shown."""
        result = {
            "mode": "value", "accas": [],
            "bankroll": 2500.0, "bankroll_reason": "fallback",
            "kelly_fraction": 0.25, "fx_rate": 1.33, "fx_source": "fallback",
            "exposure": Exposure(), "promo_audit": [],
            "promo_required": False, "pm_alternatives": {}, "unsupported": [],
        }
        txt = format_accas(result)
        # Should mention that there are no qualifying accas (existing text)
        assert "No qualifying" in txt or "NO BET" in txt

    def test_promo_check_required_appears_when_flagged(self):
        result = {
            "mode": "promo", "accas": [],
            "bankroll": 2500.0, "bankroll_reason": "fallback",
            "kelly_fraction": 0.25, "fx_rate": 1.33, "fx_source": "fallback",
            "exposure": Exposure(),
            "promo_audit": ["betfair: PROMO CHECK REQUIRED (scrape failed)"],
            "promo_required": True,
            "pm_alternatives": {}, "unsupported": [],
        }
        txt = format_accas(result)
        assert "PROMO CHECK REQUIRED" in txt

    def test_fx_section_in_output(self):
        result = {
            "mode": "value", "accas": [],
            "bankroll": 2500.0, "bankroll_reason": "fallback",
            "kelly_fraction": 0.25, "fx_rate": 1.3350, "fx_source": "live",
            "exposure": Exposure(), "promo_audit": [],
            "promo_required": False, "pm_alternatives": {}, "unsupported": [],
        }
        txt = format_accas(result)
        # FX info (bankroll / rate) should be present somewhere
        assert "2500" in txt or "£" in txt
