"""Pinning tests for the canonical desk selection rule (``wca.selection``).

This module is the SAFETY PIN for the single source-of-truth selection rule
that every bet-ranking / selection / sizing surface imports. If any of these
break, a real-money ordering or cash-floor decision has changed — treat that as
intentional only when the canonical ruling itself is being revised
(``wca.selection`` is a human-approved-change file, like the execution caps).

Canonical rule (user-confirmed 2026-07-07; category-conditional refinement
2026-07-09):
  1. bucket by MODEL prob (PRIMARY): moneyline >=0.50, mid 0.25-0.50,
     longshot <0.25 — higher bucket ALWAYS ranks above lower, regardless of EV;
  2. further-out fixtures first (SECONDARY) — CATEGORY-CONDITIONAL: kept for
     multi-week FUTURES/advancement (raw hours descending); NEUTRALISED for
     90-min MATCH markets (contributes 0), so EV breaks ties within the bucket
     (backtest 2026-07-09, n=1,046 resolved PM markets: no early premium after
     fees for match markets);
  3. EV breaks ties — the effective secondary key for match markets;
  4. no cash on longshots (model <0.25): strict ``< 0.25`` floor.
"""
from __future__ import annotations

import datetime

import pytest

from wca.selection import (
    LONGSHOT_PROB,
    MARKET_FUTURES,
    MARKET_MATCH,
    PROB_BUCKETS,
    bucket_rank,
    hours_out,
    hours_out_term,
    longshot_no_cash,
    preference_sort_key,
    prob_bucket,
    resolve_market_kind,
)


# ---------------------------------------------------------------------------
# Boundaries — inclusive lower bounds; cash floor is strict <0.25.
# ---------------------------------------------------------------------------


class TestBucketBoundaries:
    @pytest.mark.parametrize(
        "prob, expected",
        [
            (1.00, "moneyline"),
            (0.51, "moneyline"),
            (0.50, "moneyline"),   # inclusive lower bound
            (0.4999, "mid"),       # just below 0.50 -> mid
            (0.35, "mid"),
            (0.2500, "mid"),       # inclusive lower bound
            (0.2499, "longshot"),  # just below 0.25 -> longshot
            (0.10, "longshot"),
            (0.0, "longshot"),
        ],
    )
    def test_prob_bucket_boundaries(self, prob, expected):
        assert prob_bucket(prob) == expected

    def test_none_and_falsy_probs_are_longshot(self):
        assert prob_bucket(None) == "longshot"
        assert prob_bucket(0) == "longshot"

    @pytest.mark.parametrize(
        "prob, rank",
        [(0.60, 0), (0.50, 0), (0.4999, 1), (0.25, 1), (0.2499, 2), (0.05, 2)],
    )
    def test_bucket_rank(self, prob, rank):
        assert bucket_rank(prob) == rank

    def test_bucket_definition_matches_constant(self):
        # PROB_BUCKETS drives prob_bucket; keep them consistent.
        assert PROB_BUCKETS == ((0.50, "moneyline"), (0.25, "mid"), (0.0, "longshot"))
        assert LONGSHOT_PROB == 0.25


class TestLongshotNoCash:
    def test_cash_floor_is_strict(self):
        # Exactly 0.25 is a stakeable MID -> NOT no-cash.
        assert longshot_no_cash(0.25) is False
        # Just below the floor IS a no-cash longshot.
        assert longshot_no_cash(0.2499) is True

    @pytest.mark.parametrize(
        "prob, no_cash",
        [(0.60, False), (0.50, False), (0.30, False), (0.25, False),
         (0.2499, True), (0.10, True), (0.0, True), (None, True)],
    )
    def test_no_cash_predicate(self, prob, no_cash):
        assert longshot_no_cash(prob) is no_cash


# ---------------------------------------------------------------------------
# hours_out — continuous raw float (whether it FEEDS the sort is decided by
# hours_out_term from the candidate's category; see TestHoursOutTerm).
# ---------------------------------------------------------------------------


class TestHoursOut:
    NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
    KICK = {
        "A vs B": "2026-07-02T14:00:00Z",  # 2h out
        "C vs D": "2026-07-05T14:00:00Z",  # ~74h out
    }

    def test_continuous_value(self):
        p = {"match_desc": "A vs B"}
        assert hours_out(p, self.KICK, self.NOW) == pytest.approx(2.0)
        q = {"match_desc": "C vs D"}
        assert hours_out(q, self.KICK, self.NOW) == pytest.approx(74.0)

    def test_unknown_kickoff_is_zero(self):
        assert hours_out({"match_desc": "X vs Y"}, self.KICK, self.NOW) == 0.0
        assert hours_out({"match_desc": ""}, {}, self.NOW) == 0.0

    def test_past_kickoff_clamped_to_zero(self):
        past = {"A vs B": "2026-07-01T00:00:00Z"}  # before NOW
        assert hours_out({"match_desc": "A vs B"}, past, self.NOW) == 0.0


# ---------------------------------------------------------------------------
# resolve_market_kind — category resolution + the safe default (2026-07-09).
# ---------------------------------------------------------------------------


class TestResolveMarketKind:
    def test_unknown_defaults_to_match(self):
        # SAFE DEFAULT: nothing declared -> match (hours-out neutral). Match is
        # the bulk of the book and the backtest says neutral is correct there.
        assert resolve_market_kind() == MARKET_MATCH
        assert resolve_market_kind(None, "", "1x2", "totals", "btts") == MARKET_MATCH
        assert resolve_market_kind("90min") == MARKET_MATCH

    def test_explicit_kind_wins_verbatim(self):
        assert resolve_market_kind(MARKET_MATCH) == MARKET_MATCH
        assert resolve_market_kind(MARKET_FUTURES) == MARKET_FUTURES

    @pytest.mark.parametrize(
        "hint",
        ["advancement", "reach_QF", "group_winner", "group winner",
         "outright", "to_win", "tournament", "futures"],
    )
    def test_futures_markers_resolve_to_futures(self, hint):
        assert resolve_market_kind(hint) == MARKET_FUTURES

    def test_bare_win_does_not_false_match(self):
        # "win" as a loose substring would wrongly grab match-market labels.
        # These must stay MATCH (the winner-futures case is caught by
        # "outright"/"to_win"/"tournament"/explicit MARKET_FUTURES instead).
        assert resolve_market_kind("winner") == MARKET_MATCH
        assert resolve_market_kind("winning margin") == MARKET_MATCH

    def test_single_match_advance_leg_is_match(self):
        # A same-match "Team to Advance" leg settles ET+pens but resolves WITHIN
        # the match — the ruling targets MULTI-WEEK futures, so this stays MATCH.
        assert resolve_market_kind("ET+pens", "advance") == MARKET_MATCH


# ---------------------------------------------------------------------------
# hours_out_term — the ONE place the match/futures conditional lives.
# ---------------------------------------------------------------------------


class TestHoursOutTerm:
    def test_match_is_neutral(self):
        # 90-min match markets: hours-out contributes 0 regardless of value.
        assert hours_out_term(0.0, MARKET_MATCH) == 0.0
        assert hours_out_term(74.0, MARKET_MATCH) == 0.0
        assert hours_out_term(999.0, MARKET_MATCH) == 0.0

    def test_match_is_the_default(self):
        assert hours_out_term(74.0) == 0.0  # default kind = match

    def test_futures_keeps_further_out_first(self):
        # Futures: -hours, so the larger (further-out) value sorts first.
        assert hours_out_term(74.0, MARKET_FUTURES) == pytest.approx(-74.0)
        assert hours_out_term(2.0, MARKET_FUTURES) == pytest.approx(-2.0)
        assert (hours_out_term(74.0, MARKET_FUTURES)
                < hours_out_term(2.0, MARKET_FUTURES))  # 74h ranks before 2h


# ---------------------------------------------------------------------------
# preference_sort_key — (bucket, category-conditional hours_term, -ev).
# ---------------------------------------------------------------------------


class TestPreferenceSortKey:
    NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
    KICK = {
        "near": "2026-07-02T14:00:00Z",   # 2h out
        "mid_out": "2026-07-04T12:00:00Z",  # 48h out
        "far": "2026-07-05T14:00:00Z",    # ~74h out
    }

    @staticmethod
    def _p(match, prob, ev, **extra):
        p = {"match_desc": match, "model_prob": prob, "ev": ev}
        p.update(extra)
        return p

    def test_bucket_beats_ev(self):
        # A high-EV longshot must rank BELOW a low-EV moneyline (unchanged).
        ml = self._p("near", 0.60, 0.02)
        dog = self._p("near", 0.15, 0.90)
        ordered = sorted([dog, ml], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["model_prob"] for p in ordered] == [0.60, 0.15]

    # --- MATCH markets: hours-out NEUTRAL, EV breaks ties (2026-07-09) --------

    def test_match_ev_beats_further_out_within_bucket(self):
        # THE 2026-07-09 REVERSAL for match markets: a NEARER +EV pick now ranks
        # ABOVE a further-out lower-EV one in the same bucket (opposite of the
        # pre-2026-07-09 "further-out beats EV"). Default kind = match.
        near_hi = self._p("near", 0.55, 0.50)
        far_lo = self._p("far", 0.55, 0.01)
        ordered = sorted([near_hi, far_lo], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["match_desc"] for p in ordered] == ["near", "far"]

    def test_ev_breaks_ties_same_bucket_same_fixture(self):
        lo = self._p("near", 0.55, 0.04)
        hi = self._p("near", 0.55, 0.09)
        ordered = sorted([lo, hi], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["ev"] for p in ordered] == [0.09, 0.04]

    def test_match_full_mixed_slate_exact_order(self):
        # Match markets (default). Mixed buckets / hours / EV incl. ties and
        # boundary values. Within a bucket EV now wins (hours neutral).
        ml_far_lo = self._p("far", 0.60, 0.05)     # moneyline, 74h, small EV
        ml_near_hi = self._p("near", 0.50, 0.40)   # moneyline (0.50 boundary), 2h, BIG EV
        mid_far_lo = self._p("far", 0.25, 0.30)    # mid (0.25 boundary), 74h
        mid_mid_hi = self._p("mid_out", 0.40, 0.99)  # mid, 48h, HUGE EV
        dog = self._p("far", 0.2499, 0.99)         # longshot (just below floor), huge EV
        slate = [dog, mid_mid_hi, ml_near_hi, mid_far_lo, ml_far_lo]
        ordered = sorted(slate, key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        # Moneylines first (near-BIG-EV 0.40 before far-small-EV 0.05 — EV wins,
        # hours neutral), then mids (mid_mid 0.99 EV before mid_far 0.30 EV),
        # then the longshot last despite its 0.99 EV.
        assert [p["ev"] for p in ordered] == [0.40, 0.05, 0.99, 0.30, 0.99]
        assert [p["model_prob"] for p in ordered] == [0.50, 0.60, 0.40, 0.25, 0.2499]

    def test_match_missing_kickoff_does_not_matter(self):
        # Hours neutral for match: known/unknown kickoff makes no difference,
        # so EV (here equal) then stable order — neither hours term contributes.
        known_far = self._p("far", 0.60, 0.09)
        unknown = self._p("unknown_fixture", 0.60, 0.02)
        ordered = sorted([unknown, known_far], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        # EV breaks the tie (0.09 > 0.02), NOT hours-out.
        assert ordered[0]["match_desc"] == "far"

    # --- FUTURES markets: further-out-first KEPT (2026-07-09) -----------------

    def test_futures_further_out_beats_ev_within_bucket(self):
        # Futures/advancement: further-out fixture STILL ranks before higher EV.
        near_hi = self._p("near", 0.55, 0.50, market="advancement")
        far_lo = self._p("far", 0.55, 0.01, market="advancement")
        ordered = sorted([near_hi, far_lo], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["match_desc"] for p in ordered] == ["far", "near"]

    def test_futures_kind_via_explicit_arg(self):
        # The explicit market_kind arg overrides candidate fields.
        near_hi = self._p("near", 0.55, 0.50)
        far_lo = self._p("far", 0.55, 0.01)
        ordered = sorted([near_hi, far_lo],
                         key=lambda p: preference_sort_key(p, self.KICK, self.NOW,
                                                           market_kind=MARKET_FUTURES))
        assert [p["match_desc"] for p in ordered] == ["far", "near"]

    def test_futures_missing_kickoff_degrades_gracefully(self):
        known_far = self._p("far", 0.60, 0.02, market="advancement")
        unknown = self._p("unknown_fixture", 0.60, 0.02, market="advancement")
        ordered = sorted([unknown, known_far], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert ordered[0]["match_desc"] == "far"  # known-further beats unknown (0h)

    # --- Mixed feed: each category ordered by its own rule --------------------

    def test_mixed_feed_orders_each_category_correctly(self):
        # A feed carrying BOTH match and futures candidates: match rows use EV
        # within the bucket (hours neutral); futures rows keep further-out-first.
        # All moneyline bucket so they interleave purely on the secondary/EV.
        m_near_hi = self._p("near", 0.60, 0.50)                       # match, near, big EV
        m_far_lo = self._p("far", 0.60, 0.05)                        # match, far, small EV
        f_near_hi = self._p("near", 0.60, 0.50, market="advancement")  # futures, near, big EV
        f_far_lo = self._p("far", 0.60, 0.05, market="advancement")   # futures, far, small EV
        # Sort match rows: EV wins -> near before far.
        m_order = sorted([m_far_lo, m_near_hi],
                         key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["match_desc"] for p in m_order] == ["near", "far"]
        # Sort futures rows: further-out wins -> far before near.
        f_order = sorted([f_near_hi, f_far_lo],
                         key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["match_desc"] for p in f_order] == ["far", "near"]

    def test_boundary_same_teams_match_1x2_vs_futures_advance(self):
        # BOUNDARY: same two teams, one 90-min 1X2 (match) vs one ET+pens
        # advancement (futures). hours_out applies to the advancement one ONLY.
        # The 1X2 row's hours term is neutral; the advancement row keeps -hours.
        m = self._p("far", 0.60, 0.10, settlement="90min", family="1x2")
        f = self._p("far", 0.60, 0.10, market="advancement", settlement="ET+pens")
        km = preference_sort_key(m, self.KICK, self.NOW)
        kf = preference_sort_key(f, self.KICK, self.NOW)
        # Same bucket + same EV, but the futures row carries a negative hours
        # term (-74h) while the match row carries 0 -> the futures row's key is
        # strictly smaller (sorts first) purely because of the conditional.
        assert km[0] == kf[0]                    # same bucket rank
        assert km[1] == 0.0                       # match: hours neutral
        assert kf[1] == pytest.approx(-74.0)      # futures: -hours retained
        assert kf < km                            # advancement sorts first

    def test_key_shape_is_three_tuple(self):
        k = preference_sort_key(self._p("near", 0.6, 0.1), self.KICK, self.NOW)
        assert isinstance(k, tuple) and len(k) == 3
        assert k[0] == 0  # moneyline bucket rank
