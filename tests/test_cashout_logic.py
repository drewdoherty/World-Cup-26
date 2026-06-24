"""Tests for the pure cash-out logic (wca.pm.cashout) and position parsing
(wca.pm.positions). These predicates decide whether real money gets dumped, so
they are tested exhaustively. No network.
"""

from __future__ import annotations

import pytest

from wca.pm import cashout
from wca.pm.positions import Position


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize("title,expected", [
        ("Exact Score: Qatar 0 - 2 Switzerland?", cashout.KIND_EXACT),
        ("Exact Score: United States 0 - 0 Paraguay?", cashout.KIND_EXACT),
        ("Will there be over 2.5 goals in Brazil vs Morocco?", cashout.KIND_TOTALS),
        ("Total goals Under 3.5 in Spain vs Japan", cashout.KIND_TOTALS),
        ("Will both teams score in Brazil vs Morocco?", cashout.KIND_BTTS),
        ("Will Canada win on 2026-06-12?", cashout.KIND_TEAM_WIN),
        ("Will the match end in a draw?", cashout.KIND_TEAM_WIN),
        ("Will Japan reach the Round of 16 at the 2026 FIFA World Cup?", cashout.KIND_ADVANCEMENT),
        ("Group A winner", cashout.KIND_ADVANCEMENT),
    ])
    def test_kinds(self, title, expected):
        assert cashout.classify_market(title) == expected

    def test_killable_set(self):
        assert cashout.KILLABLE_KINDS == (
            cashout.KIND_EXACT, cashout.KIND_TOTALS, cashout.KIND_BTTS
        )


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------


class TestParsing:
    def test_parse_exact_score(self):
        ex = cashout.parse_exact_score("Exact Score: Qatar 0 - 2 Switzerland?")
        assert (ex.home, ex.home_goals, ex.away, ex.away_goals) == ("Qatar", 0, "Switzerland", 2)

    def test_parse_exact_score_double_digit_and_multiword(self):
        ex = cashout.parse_exact_score("Exact Score: South Korea 1 - 1 Bosnia-Herzegovina?")
        assert ex.home == "South Korea" and ex.away == "Bosnia-Herzegovina"
        assert ex.home_goals == 1 and ex.away_goals == 1

    def test_parse_exact_score_none(self):
        assert cashout.parse_exact_score("Will Canada win?") is None

    @pytest.mark.parametrize("title,phrase,line", [
        ("Will there be over 2.5 goals in A vs B?", "over", 2.5),
        ("Total goals Under 3.5", "under", 3.5),
        ("O 1.5 goals", "over", 1.5),
    ])
    def test_parse_totals(self, title, phrase, line):
        assert cashout.parse_totals_line(title) == (phrase, line)

    def test_parse_match_teams_from_in_clause(self):
        assert cashout.parse_match_teams(
            "Will there be over 2.5 goals in Brazil vs Morocco?"
        ) == ("Brazil", "Morocco")

    def test_parse_match_teams_exact_uses_order(self):
        assert cashout.parse_match_teams(
            "Exact Score: Qatar 0 - 2 Switzerland?"
        ) == ("Qatar", "Switzerland")

    def test_parse_match_teams_hyphenated_home_name(self):
        # The hyphen in "Bosnia-Herzegovina" must NOT be mistaken for the
        # home/away separator (greedy ' vs ' split).
        assert cashout.parse_match_teams(
            "Will both teams score in Bosnia-Herzegovina vs Serbia?"
        ) == ("Bosnia-Herzegovina", "Serbia")


# ---------------------------------------------------------------------------
# Effective direction
# ---------------------------------------------------------------------------


class TestEffectiveDirection:
    @pytest.mark.parametrize("phrase,outcome,expected", [
        ("over", "Yes", "over"),
        ("over", "No", "under"),
        ("under", "Yes", "under"),
        ("under", "No", "over"),
    ])
    def test_direction(self, phrase, outcome, expected):
        assert cashout.effective_totals_direction(phrase, outcome) == expected


# ---------------------------------------------------------------------------
# Kill predicates
# ---------------------------------------------------------------------------


class TestExactScoreKill:
    def test_alive_at_target(self):
        v = cashout.evaluate_position(
            title="Exact Score: United States 0 - 0 Paraguay?",
            outcome="Yes", home_goals=0, away_goals=0,
        )
        assert v.kind == cashout.KIND_EXACT and v.killed is False

    def test_dead_when_away_scores(self):
        v = cashout.evaluate_position(
            title="Exact Score: United States 0 - 0 Paraguay?",
            outcome="Yes", home_goals=0, away_goals=1,
        )
        assert v.killed is True

    def test_dead_when_home_exceeds(self):
        # Holding 1-1: dies when home reaches 2.
        v = cashout.evaluate_position(
            title="Exact Score: A 1 - 1 B?", outcome="Yes",
            home_goals=2, away_goals=1,
        )
        assert v.killed is True

    def test_alive_at_one_one_for_one_one_bet(self):
        v = cashout.evaluate_position(
            title="Exact Score: A 1 - 1 B?", outcome="Yes",
            home_goals=1, away_goals=1,
        )
        assert v.killed is False

    def test_alive_below_target(self):
        # 0-2 bet still alive at 0-1.
        v = cashout.evaluate_position(
            title="Exact Score: Qatar 0 - 2 Switzerland?", outcome="Yes",
            home_goals=0, away_goals=1,
        )
        assert v.killed is False


class TestTotalsKill:
    def test_under_alive_then_dead(self):
        title = "Will there be over 2.5 goals in A vs B?"
        # Holding "No" == betting Under 2.5.
        assert cashout.evaluate_position(
            title=title, outcome="No", home_goals=1, away_goals=1).killed is False
        assert cashout.evaluate_position(
            title=title, outcome="No", home_goals=2, away_goals=1).killed is True

    def test_over_never_killed_by_goal(self):
        title = "Will there be over 2.5 goals in A vs B?"
        v = cashout.evaluate_position(
            title=title, outcome="Yes", home_goals=3, away_goals=0)
        assert v.killed is False

    def test_under_phrased_title_held_yes(self):
        title = "Total goals Under 2.5 in A vs B"
        # "Yes" on an Under market == betting under.
        assert cashout.evaluate_position(
            title=title, outcome="Yes", home_goals=2, away_goals=1).killed is True


class TestBttsKill:
    def test_btts_no_alive_then_dead(self):
        title = "Will both teams score in A vs B?"
        assert cashout.evaluate_position(
            title=title, outcome="No", home_goals=1, away_goals=0).killed is False
        assert cashout.evaluate_position(
            title=title, outcome="No", home_goals=1, away_goals=1).killed is True

    def test_btts_yes_never_killed(self):
        title = "Will both teams score in A vs B?"
        assert cashout.evaluate_position(
            title=title, outcome="Yes", home_goals=1, away_goals=1).killed is False


class TestGradientNotKilled:
    def test_team_win_not_killed(self):
        v = cashout.evaluate_position(
            title="Will Canada win on 2026-06-12?", outcome="Yes",
            home_goals=0, away_goals=3)
        assert v.killed is False


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


class TestOrient:
    def test_basic(self):
        scores = [{"name": "Brazil", "score": 1}, {"name": "Morocco", "score": 0}]
        assert cashout.orient_score("Brazil", "Morocco", scores) == (1, 0)

    def test_reversed_list(self):
        scores = [{"name": "Morocco", "score": 0}, {"name": "Brazil", "score": 2}]
        assert cashout.orient_score("Brazil", "Morocco", scores) == (2, 0)

    def test_canonical_alias(self):
        scores = [{"name": "USA", "score": 0}, {"name": "Paraguay", "score": 1}]
        assert cashout.orient_score("United States", "Paraguay", scores) == (0, 1)

    def test_missing_team_returns_none(self):
        scores = [{"name": "Brazil", "score": 1}, {"name": "Spain", "score": 0}]
        assert cashout.orient_score("Brazil", "Morocco", scores) is None

    def test_garbage_score_returns_none(self):
        scores = [{"name": "Brazil", "score": "x"}, {"name": "Morocco", "score": 0}]
        assert cashout.orient_score("Brazil", "Morocco", scores) is None

    def test_too_few_scores_returns_none(self):
        assert cashout.orient_score("A", "B", [{"name": "A", "score": 1}]) is None


class TestFindScoresEvent:
    def _ev(self, h, a):
        return {"home_team": h, "away_team": a,
                "scores": [{"name": h, "score": "0"}, {"name": a, "score": "0"}]}

    def test_single_match(self):
        ev = cashout.find_scores_event("Brazil", "Morocco",
                                       [self._ev("Spain", "Japan"), self._ev("Brazil", "Morocco")])
        assert ev is not None and ev["home_team"] == "Brazil"

    def test_no_match_returns_none(self):
        assert cashout.find_scores_event("Brazil", "Morocco",
                                         [self._ev("Spain", "Japan")]) is None

    def test_ambiguous_duplicate_returns_none(self):
        # Two feed rows for the same pairing (stale/duplicate) -> SKIP, never guess.
        dup = [self._ev("Brazil", "Morocco"), self._ev("Morocco", "Brazil")]
        assert cashout.find_scores_event("Brazil", "Morocco", dup) is None


# ---------------------------------------------------------------------------
# SELL proposal builder
# ---------------------------------------------------------------------------


def _pos(**kw):
    base = dict(
        asset="TOK1", condition_id="0xc", size=100.0, avg_price=0.10,
        cur_price=0.08, outcome="Yes", title="Exact Score: A 0 - 0 B?",
        slug="s", event_slug="fifwc-a-b", end_date="2026-06-13",
        neg_risk=True, redeemable=False, current_value=8.0,
    )
    base.update(kw)
    return Position(**base)


class TestBuildSellProposal:
    def test_basic_proposal(self):
        p = cashout.build_sell_proposal(_pos(), best_bid=0.06, min_proceeds=1.0)
        assert p["side"] == "SELL"
        assert p["token_id"] == "TOK1"
        assert p["price"] == pytest.approx(0.06)
        assert p["size"] == pytest.approx(100.0)
        assert p["neg_risk"] is True
        assert p["order_type"] == "FOK"
        assert p["est_proceeds"] == pytest.approx(6.0)

    def test_no_bid_returns_none(self):
        assert cashout.build_sell_proposal(_pos(), best_bid=None) is None
        assert cashout.build_sell_proposal(_pos(), best_bid=0.0) is None

    def test_below_min_proceeds_returns_none(self):
        # 100 sh * 0.005 = $0.50 < $1 floor.
        assert cashout.build_sell_proposal(
            _pos(), best_bid=0.005, min_proceeds=1.0) is None

    def test_clamped_to_bid_depth(self):
        p = cashout.build_sell_proposal(
            _pos(size=100.0), best_bid=0.06, bid_size=30.0, min_proceeds=1.0)
        assert p["size"] == pytest.approx(30.0)

    def test_undercut_ticks(self):
        p = cashout.build_sell_proposal(
            _pos(), best_bid=0.06, undercut_ticks=1, tick=0.01, min_proceeds=1.0)
        assert p["price"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Order-book parsing
# ---------------------------------------------------------------------------


class TestBookHelpers:
    def test_best_bid(self):
        book = {"bids": [{"price": "0.05", "size": "20"},
                         {"price": "0.06", "size": "10"}],
                "asks": [{"price": "0.20", "size": "5"}]}
        assert cashout.best_bid_from_book(book) == (0.06, 10.0)

    def test_best_bid_empty(self):
        assert cashout.best_bid_from_book({"bids": []}) is None
        assert cashout.best_bid_from_book({}) is None

    def test_marketable_plan_single_level(self):
        book = {"bids": [{"price": "0.06", "size": "100"}]}
        price, shares, proceeds = cashout.marketable_sell_plan(book, 50)
        assert price == pytest.approx(0.06)
        assert shares == pytest.approx(50.0)
        assert proceeds == pytest.approx(3.0)

    def test_marketable_plan_walks_levels(self):
        book = {"bids": [{"price": "0.06", "size": "10"},
                         {"price": "0.05", "size": "100"}]}
        # Want 30: take 10@0.06 + 20@0.05 = 0.6 + 1.0 = 1.6; limit = 0.05.
        price, shares, proceeds = cashout.marketable_sell_plan(book, 30)
        assert price == pytest.approx(0.05)
        assert shares == pytest.approx(30.0)
        assert proceeds == pytest.approx(1.6)

    def test_marketable_plan_respects_floor(self):
        book = {"bids": [{"price": "0.06", "size": "10"},
                         {"price": "0.02", "size": "100"}]}
        # Floor 0.05 stops us hitting the 0.02 level: only 10 shares fill.
        price, shares, proceeds = cashout.marketable_sell_plan(book, 50, min_price=0.05)
        assert shares == pytest.approx(10.0)
        assert price == pytest.approx(0.06)

    def test_marketable_plan_empty_book(self):
        assert cashout.marketable_sell_plan({"bids": []}, 10) is None
