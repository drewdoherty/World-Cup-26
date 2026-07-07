"""Pinning tests for the canonical desk selection rule (``wca.selection``).

This module is the SAFETY PIN for the single source-of-truth selection rule
that every bet-ranking / selection / sizing surface imports. If any of these
break, a real-money ordering or cash-floor decision has changed — treat that as
intentional only when the 2026-07-07 canonical ruling itself is being revised
(``wca.selection`` is a human-approved-change file, like the execution caps).

Canonical rule (user-confirmed 2026-07-07):
  1. bucket by MODEL prob (PRIMARY): moneyline >=0.50, mid 0.25-0.50,
     longshot <0.25 — higher bucket ALWAYS ranks above lower, regardless of EV;
  2. further-out fixtures first (SECONDARY): raw continuous hours-to-kickoff,
     descending;
  3. EV breaks ties ONLY (tertiary), within the same bucket + further-out tier;
  4. no cash on longshots (model <0.25): strict ``< 0.25`` floor.
"""
from __future__ import annotations

import datetime

import pytest

from wca.selection import (
    LONGSHOT_PROB,
    PROB_BUCKETS,
    bucket_rank,
    hours_out,
    longshot_no_cash,
    preference_sort_key,
    prob_bucket,
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
# hours_out — continuous raw float, further-out sorts first via negation.
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
# preference_sort_key — the full (bucket, -hours_out, -ev) ordering.
# ---------------------------------------------------------------------------


class TestPreferenceSortKey:
    NOW = datetime.datetime(2026, 7, 2, 12, 0, 0)
    KICK = {
        "near": "2026-07-02T14:00:00Z",   # 2h out
        "mid_out": "2026-07-04T12:00:00Z",  # 48h out
        "far": "2026-07-05T14:00:00Z",    # ~74h out
    }

    @staticmethod
    def _p(match, prob, ev):
        return {"match_desc": match, "model_prob": prob, "ev": ev}

    def test_bucket_beats_ev(self):
        # A high-EV longshot must rank BELOW a low-EV moneyline.
        ml = self._p("near", 0.60, 0.02)
        dog = self._p("near", 0.15, 0.90)
        ordered = sorted([dog, ml], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["model_prob"] for p in ordered] == [0.60, 0.15]

    def test_further_out_beats_ev_within_bucket(self):
        # Same bucket: further-out fixture ranks before higher EV.
        near_hi = self._p("near", 0.55, 0.50)
        far_lo = self._p("far", 0.55, 0.01)
        ordered = sorted([near_hi, far_lo], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["match_desc"] for p in ordered] == ["far", "near"]

    def test_ev_breaks_ties_same_bucket_same_fixture(self):
        lo = self._p("near", 0.55, 0.04)
        hi = self._p("near", 0.55, 0.09)
        ordered = sorted([lo, hi], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert [p["ev"] for p in ordered] == [0.09, 0.04]

    def test_full_mixed_slate_exact_order(self):
        # Mixed buckets / hours / EV incl. ties and boundary values.
        ml_far = self._p("far", 0.60, 0.05)       # moneyline, 74h
        ml_near_hi = self._p("near", 0.50, 0.40)  # moneyline (0.50 boundary), 2h, big EV
        mid_far = self._p("far", 0.25, 0.30)      # mid (0.25 boundary), 74h
        mid_mid = self._p("mid_out", 0.40, 0.99)  # mid, 48h, huge EV
        dog = self._p("far", 0.2499, 0.99)        # longshot (just below floor), 74h, huge EV
        slate = [dog, mid_mid, ml_near_hi, mid_far, ml_far]
        ordered = sorted(slate, key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        # Expected: both moneylines first (far before near — further-out beats
        # EV), then both mids (far(74h) before mid_out(48h)), then the longshot
        # last despite its 0.99 EV.
        assert [p["model_prob"] for p in ordered] == [0.60, 0.50, 0.25, 0.40, 0.2499]

    def test_missing_kickoff_degrades_gracefully(self):
        known_far = self._p("far", 0.60, 0.02)
        unknown = self._p("unknown_fixture", 0.60, 0.02)
        ordered = sorted([unknown, known_far], key=lambda p: preference_sort_key(p, self.KICK, self.NOW))
        assert ordered[0]["match_desc"] == "far"  # known-further beats unknown (0h)

    def test_key_shape_is_three_tuple(self):
        k = preference_sort_key(self._p("near", 0.6, 0.1), self.KICK, self.NOW)
        assert isinstance(k, tuple) and len(k) == 3
        assert k[0] == 0  # moneyline bucket rank
