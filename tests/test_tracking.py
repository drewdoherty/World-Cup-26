"""Tests for wca.tracking and the static tracking-page assets.

These exercise:

* card-history parsing — pick lines with exact ``model % / mkt %`` values and
  the ``<!-- generated: ... -->`` timestamp;
* the approximate model-1X2 reconstruction from a top-k scoreline ladder
  (bucketing, renormalisation, the zero-bucket floor);
* hand-verified Brier / log-loss arithmetic on 1X2 triples;
* de-vigged consensus across bookmaker triples;
* pre-kickoff snapshot selection (a post-kickoff snapshot must never be used);
* the deterministic :func:`wca.tracking.build_tracking_data` end to end with a
  synthetic card, results file rows, a market close and ledger bets; and
* that the shipped ``site/tracking.html`` references ``./tracking_data.json``
  and contains no external http(s) assets.
"""

from __future__ import annotations

import math
import os

import pytest

from wca import tracking


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SITE_DIR = os.path.join(_REPO_ROOT, "site")


# ---------------------------------------------------------------------------
# Synthetic fixtures.
# ---------------------------------------------------------------------------

_SYNTH_CARD = """<!-- generated: 2026-06-11T15:25:01 -->
*World Cup Alpha — bet card* (2 picks)

*1. Mexico vs South Africa* — Mexico @ *1.45* (matchbook)
    model 71.1% / mkt 69.0%  edge *+3.1%*  [elo 83% dc 64%]
    stake: main 17.37
*2. South Korea vs Czech Republic* — Draw @ *3.40* (smarkets)
    model 30.0% / mkt 28.0%  edge *+7.1%*  [elo 29% dc 31%]
    stake: main 5.00

_Pool: rung 0 £1000 — 0/50 settled-with-close bets, CLV n/a, Kelly fraction 0.25_

*World Cup Alpha — scorelines* (2 fixtures)

*Mexico vs South Africa*
    1-0  16.9%  fair 5.92  back >= 6.04
    2-0  15.5%  fair 6.46  back >= 6.59
    2-1  10.1%  fair 9.85  back >= 10.05
    3-0  9.1%  fair 10.96  back >= 11.18
    1-1  8.7%  fair 11.47  back >= 11.70
    0-0  7.6%  fair 13.17  back >= 13.44
    O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%

*South Korea vs Czech Republic*
    1-1  13.9%  fair 7.19  back >= 7.34
    1-0  13.0%  fair 7.66  back >= 7.82
    0-0  11.6%  fair 8.60  back >= 8.77
    0-1  10.2%  fair 9.81  back >= 10.01
    2-1  8.4%  fair 11.87  back >= 12.11
    2-0  7.0%  fair 14.30  back >= 14.59
    O/U 2.5: over 38.0% / under 62.0%   BTTS 45.1%
"""

# A later card — generated AFTER the Mexico kickoff — whose numbers must never
# be used for the Mexico fixture.
_POST_KICKOFF_CARD = """<!-- generated: 2026-06-11T23:58:41 -->
*World Cup Alpha — bet card* (1 picks)

*1. Mexico vs South Africa* — Mexico @ *1.01* (betfair_ex_uk)
    model 99.0% / mkt 99.0%  edge *+0.0%*  [elo 99% dc 99%]
    stake: main 1.00

*World Cup Alpha — scorelines* (1 fixtures)

*Mexico vs South Africa*
    2-0  99.0%  fair 1.01  back >= 1.01
    O/U 2.5: over 1.0% / under 99.0%   BTTS 1.0%
"""

_RESULTS = [
    {
        "date": "2026-06-11",
        "fixture": "Mexico vs South Africa",
        "kickoff_utc": "2026-06-11T19:00:00Z",
        "score": "2-0",
        "outcome": "home",
    },
    {
        "date": "2026-06-12",
        "fixture": "United States vs Paraguay",
        "kickoff_utc": "2026-06-13T01:00:00Z",
        "score": None,
        "outcome": "pending",
    },
]

_BETS = [
    {"id": 1, "match_desc": "Mexico vs South Africa", "selection": "Mexico",
     "decimal_odds": 1.45, "stake": 22.0, "status": "won",
     "settled_pl": 9.9, "clv": 0.035},
    {"id": 2, "match_desc": "Canada vs Bosnia and Herzegovina",
     "selection": "Canada", "decimal_odds": 1.8, "stake": 9.57,
     "status": "lost", "settled_pl": -9.57, "clv": -0.0576},
    {"id": 3, "match_desc": "USA vs Paraguay", "selection": "Paraguay",
     "decimal_odds": 4.2, "stake": 5.68, "status": "open",
     "settled_pl": None, "clv": None},
]


# ---------------------------------------------------------------------------
# Card-history parsing.
# ---------------------------------------------------------------------------


class TestCardParsing:
    def test_generated_timestamp(self):
        assert tracking.card_generated(_SYNTH_CARD) == "2026-06-11T15:25:01"

    def test_generated_missing(self):
        assert tracking.card_generated("no marker here") is None
        assert tracking.card_generated("") is None

    def test_picks_parsed_with_model_and_market(self):
        picks = tracking.parse_card_picks(_SYNTH_CARD)
        assert len(picks) == 2
        mexico = picks[0]
        assert mexico["fixture"] == "Mexico vs South Africa"
        assert mexico["selection"] == "Mexico"
        assert mexico["odds"] == pytest.approx(1.45)
        assert mexico["venue"] == "matchbook"
        assert mexico["model_prob"] == pytest.approx(0.711)
        assert mexico["market_prob"] == pytest.approx(0.690)

    def test_draw_pick_parsed(self):
        picks = tracking.parse_card_picks(_SYNTH_CARD)
        draw = picks[1]
        assert draw["selection"] == "Draw"
        assert draw["model_prob"] == pytest.approx(0.30)
        assert draw["market_prob"] == pytest.approx(0.28)

    def test_empty_card(self):
        assert tracking.parse_card_picks("") == []


class TestLegMapping:
    def test_home_away_draw(self):
        fx = "South Korea vs Czech Republic"
        assert tracking.leg_for_selection(fx, "South Korea") == "home"
        assert tracking.leg_for_selection(fx, "Czech Republic") == "away"
        assert tracking.leg_for_selection(fx, "Draw") == "draw"
        assert tracking.leg_for_selection(fx, "The Draw") == "draw"

    def test_alias_resolution(self):
        # The odds feed says "Korea Republic"; the card says "South Korea".
        fx = "South Korea vs Czech Republic"
        assert tracking.leg_for_selection(fx, "Korea Republic") == "home"
        assert tracking.leg_for_selection("USA vs Paraguay", "Paraguay") == "away"

    def test_unknown_selection(self):
        assert tracking.leg_for_selection("A vs B", "C") is None


class TestOutcomes:
    def test_outcome_from_score(self):
        assert tracking.outcome_from_score("2-0") == "home"
        assert tracking.outcome_from_score("1-1") == "draw"
        assert tracking.outcome_from_score("0-3") == "away"
        assert tracking.outcome_from_score(None) is None
        assert tracking.outcome_from_score("abc") is None


# ---------------------------------------------------------------------------
# 1X2 reconstruction + scoring rules.
# ---------------------------------------------------------------------------


class TestModel1x2:
    def test_buckets_and_renormalisation(self):
        scores = [
            {"score": "1-0", "prob": 30.0},
            {"score": "0-0", "prob": 20.0},
            {"score": "0-1", "prob": 10.0},
        ]
        triple = tracking.model_1x2_from_scorelines(scores)
        # 30/60, 20/60, 10/60 — all buckets above the floor, so untouched.
        assert triple["home"] == pytest.approx(0.5)
        assert triple["draw"] == pytest.approx(1 / 3)
        assert triple["away"] == pytest.approx(1 / 6)
        assert sum(triple.values()) == pytest.approx(1.0)

    def test_zero_bucket_gets_floor(self):
        scores = [
            {"score": "1-0", "prob": 60.0},
            {"score": "0-0", "prob": 20.0},
        ]
        triple = tracking.model_1x2_from_scorelines(scores)
        assert triple["away"] > 0.0  # floored, never exactly zero
        assert triple["away"] == pytest.approx(0.005, rel=0.01)
        assert sum(triple.values()) == pytest.approx(1.0)
        # Log-loss on the floored leg is large but finite.
        assert tracking.log_loss_1x2(triple, "away") < 10

    def test_empty_scores(self):
        assert tracking.model_1x2_from_scorelines([]) is None
        assert tracking.model_1x2_from_scorelines([{"score": "?", "prob": None}]) is None


class TestScoringRules:
    def test_brier_hand_verified(self):
        triple = {"home": 0.5, "draw": 0.3, "away": 0.2}
        # outcome home: (0.5-1)^2 + 0.3^2 + 0.2^2 = 0.25 + 0.09 + 0.04 = 0.38
        assert tracking.brier_1x2(triple, "home") == pytest.approx(0.38)
        # outcome away: 0.25 + 0.09 + 0.64 = 0.98
        assert tracking.brier_1x2(triple, "away") == pytest.approx(0.98)

    def test_brier_perfect_and_worst(self):
        assert tracking.brier_1x2({"home": 1.0, "draw": 0.0, "away": 0.0}, "home") == pytest.approx(0.0)
        assert tracking.brier_1x2({"home": 1.0, "draw": 0.0, "away": 0.0}, "away") == pytest.approx(2.0)

    def test_logloss(self):
        triple = {"home": 0.5, "draw": 0.3, "away": 0.2}
        assert tracking.log_loss_1x2(triple, "home") == pytest.approx(math.log(2))
        assert tracking.brier_1x2(None, "home") is None
        assert tracking.log_loss_1x2(None, "home") is None

    def test_modal_pick(self):
        assert tracking.modal_pick({"home": 0.5, "draw": 0.3, "away": 0.2}) == "home"
        assert tracking.modal_pick({"home": 0.1, "draw": 0.2, "away": 0.7}) == "away"
        assert tracking.modal_pick(None) is None


class TestDevig:
    def test_consensus_hand_verified(self):
        # One book, 5% overround spread proportionally: implied (1/2, 1/4, 1/4)
        # of total 1.05 -> devig home = (1/2)/1.05... use exact arithmetic.
        books = [{"book": "a", "home": 2.0, "draw": 4.0, "away": 4.0}]
        triple = tracking.devig_consensus(books)
        total = 1 / 2.0 + 1 / 4.0 + 1 / 4.0
        assert triple["home"] == pytest.approx((1 / 2.0) / total)
        assert sum(triple.values()) == pytest.approx(1.0)

    def test_incomplete_books_skipped(self):
        books = [
            {"book": "broken", "home": 2.0, "draw": None, "away": 4.0},
            {"book": "ok", "home": 2.0, "draw": 4.0, "away": 4.0},
        ]
        triple = tracking.devig_consensus(books)
        assert triple is not None
        assert tracking.devig_consensus([{"book": "x", "home": None}]) is None
        assert tracking.devig_consensus([]) is None


class TestSnapshotSelection:
    def test_latest_pre_kickoff_wins(self):
        snaps = [
            {"generated": "2026-06-11T10:00:00"},
            {"generated": "2026-06-11T15:25:01"},
            {"generated": "2026-06-11T23:58:41"},  # post-kickoff
        ]
        best = tracking.latest_snapshot_before(snaps, "2026-06-11T19:00:00Z")
        assert best["generated"] == "2026-06-11T15:25:01"

    def test_display_style_timestamps_accepted(self):
        snaps = [{"generated": "2026-06-11 14:53:55 UTC"}]
        best = tracking.latest_snapshot_before(snaps, "2026-06-11T19:00:00Z")
        assert best is snaps[0]

    def test_no_pre_kickoff_snapshot(self):
        snaps = [{"generated": "2026-06-11T23:58:41"}]
        assert tracking.latest_snapshot_before(snaps, "2026-06-11T19:00:00Z") is None
        assert tracking.latest_snapshot_before(snaps, None) is None


# ---------------------------------------------------------------------------
# End-to-end builder.
# ---------------------------------------------------------------------------


class TestBuildTrackingData:
    def _build(self):
        key = tracking.fixture_key("Mexico vs South Africa")
        market_closes = {
            key: {
                "triple": {"home": 0.67, "draw": 0.22, "away": 0.11},
                "ts": "2026-06-11T18:59:00+00:00",
                "books": 20,
            }
        }
        return tracking.build_tracking_data(
            results=_RESULTS,
            snapshots=[
                {"generated": "2026-06-11T15:25:01", "text": _SYNTH_CARD},
                {"generated": "2026-06-11T23:58:41", "text": _POST_KICKOFF_CARD},
            ],
            market_closes=market_closes,
            bets=_BETS,
            now_utc="2026-06-13 09:00:00 UTC",
        )

    def test_meta_and_shape(self):
        data = self._build()
        assert data["meta"]["generated"] == "2026-06-13 09:00:00 UTC"
        for k in ("summary", "fixtures", "pending", "bets"):
            assert k in data

    def test_post_kickoff_snapshot_never_used(self):
        data = self._build()
        fx = data["fixtures"][0]
        assert fx["card_generated"] == "2026-06-11T15:25:01"
        # The 99% post-hoc "prediction" must not leak in.
        assert fx["model_1x2"]["home"] < 0.9

    def test_completed_fixture_scored(self):
        data = self._build()
        fx = data["fixtures"][0]
        assert fx["fixture"] == "Mexico vs South Africa"
        assert fx["outcome"] == "home"
        assert fx["model_pick"] == "home"
        assert fx["model_correct"] is True
        assert fx["market_pick"] == "home"
        assert fx["market_correct"] is True
        assert fx["market_source"] == "closing_devig"
        # Market Brier, hand-verified:
        # (0.67-1)^2 + 0.22^2 + 0.11^2 = 0.1089 + 0.0484 + 0.0121 = 0.1694
        assert fx["brier_market"] == pytest.approx(0.1694, abs=1e-4)
        assert fx["brier_model"] is not None
        # Top scoreline was 1-0 but actual was 2-0: miss, yet inside top-6.
        assert fx["top_scoreline"]["score"] == "1-0"
        assert fx["top_scoreline"]["hit"] is False
        assert fx["top6_hit"] is True
        # 2 goals: under 2.5 hit (model said under), BTTS-no hit (model 39%).
        assert fx["ou25"]["actual_over"] is False
        assert fx["ou25"]["hit"] is True
        assert fx["btts"]["actual"] is False
        assert fx["btts"]["hit"] is True

    def test_pending_fixture_excluded_from_scoring(self):
        data = self._build()
        assert data["summary"]["fixtures_complete"] == 1
        pending = [p["fixture"] for p in data["pending"]]
        assert "United States vs Paraguay" in pending
        # Upcoming fixtures are sourced from the LATEST snapshot only; Korea
        # appears in an older card but not the latest, so it is not pending.
        assert "South Korea vs Czech Republic" not in pending

    def test_bet_aggregates(self):
        data = self._build()
        bets = data["summary"]["bets"]
        assert bets["settled"] == 2  # the open bet is excluded
        assert bets["won"] == 1
        assert bets["lost"] == 1
        assert bets["pl"] == pytest.approx(0.33)
        assert bets["avg_clv"] == pytest.approx((0.035 - 0.0576) / 2, abs=1e-4)
        # The scatter payload only carries settled bets.
        assert [b["id"] for b in data["bets"]] == [1, 2]

    def test_empty_inputs_degrade_cleanly(self):
        data = tracking.build_tracking_data(
            results=[], snapshots=[], market_closes={}, bets=[], now_utc="x"
        )
        assert data["summary"]["fixtures_complete"] == 0
        assert data["summary"]["model_brier"] is None
        assert data["fixtures"] == []
        assert data["pending"] == []

    def test_exact_models_overlay_scoreline_reconstruction(self):
        exact = [
            {
                "generated": "2026-06-11T15:30:00",
                "fixture": "Mexico vs South Africa",
                "model": {"home": 0.713, "draw": 0.187, "away": 0.1},
            },
            {  # post-kickoff row must NOT be used for a completed match
                "generated": "2026-06-11T22:00:00",
                "fixture": "Mexico vs South Africa",
                "model": {"home": 0.99, "draw": 0.005, "away": 0.005},
            },
            {
                "generated": "2026-06-12T08:00:00",
                "fixture": "United States vs Paraguay",
                "model": {"home": 0.439, "draw": 0.295, "away": 0.266},
            },
        ]
        data = tracking.build_tracking_data(
            results=_RESULTS,
            snapshots=[
                {"generated": "2026-06-11T15:25:01", "text": _SYNTH_CARD},
            ],
            market_closes={},
            bets=[],
            now_utc="x",
            exact_models=exact,
        )
        (mexico,) = [
            f for f in data["fixtures"]
            if f["fixture"] == "Mexico vs South Africa"
        ]
        assert mexico["model_source"] == "card_build"
        assert mexico["model_1x2"]["home"] == pytest.approx(0.713)
        (usa,) = [
            p for p in data["pending"]
            if p["fixture"] == "United States vs Paraguay"
        ]
        assert usa["model_source"] == "card_build"
        assert usa["model_1x2"]["away"] == pytest.approx(0.266)

    def test_payload_degraded_guard(self):
        rich = {
            "summary": {
                "fixtures_complete": 3, "model_brier": 0.47,
                "bets": {"settled": 15},
            },
            "fixtures": [{"fixture": "x"}], "pending": [],
        }
        gutted = {
            "summary": {
                "fixtures_complete": 3, "model_brier": None,
                "bets": {"settled": 0},
            },
            "fixtures": [{"fixture": "x"}], "pending": [],
        }
        fewer = {
            "summary": {
                "fixtures_complete": 2, "model_brier": 0.5,
                "bets": {"settled": 15},
            },
            "fixtures": [{"fixture": "x"}], "pending": [],
        }
        richer = {
            "summary": {
                "fixtures_complete": 4, "model_brier": 0.45,
                "bets": {"settled": 16},
            },
            "fixtures": [{"fixture": "x"}], "pending": [],
        }
        empty_old = {"summary": {"fixtures_complete": 0, "model_brier": None,
                                 "bets": {"settled": 0}},
                     "fixtures": [], "pending": []}
        assert tracking.payload_degraded(gutted, rich) is True
        assert tracking.payload_degraded(fewer, rich) is True
        assert tracking.payload_degraded(richer, rich) is False
        assert tracking.payload_degraded(rich, rich) is False
        # An empty existing feed never blocks a write.
        assert tracking.payload_degraded(gutted, empty_old) is False
        # Malformed inputs fail open (never block).
        assert tracking.payload_degraded({}, rich) is False

    def test_exact_models_absent_falls_back_to_scorelines(self):
        data = tracking.build_tracking_data(
            results=_RESULTS,
            snapshots=[
                {"generated": "2026-06-11T15:25:01", "text": _SYNTH_CARD},
            ],
            market_closes={},
            bets=[],
            now_utc="x",
        )
        (mexico,) = [
            f for f in data["fixtures"]
            if f["fixture"] == "Mexico vs South Africa"
        ]
        assert mexico["model_source"] == "scoreline_approx"


# ---------------------------------------------------------------------------
# Static assets.
# ---------------------------------------------------------------------------


class TestSiteAssets:
    def test_tracking_html_wired_up(self):
        html = open(os.path.join(_SITE_DIR, "tracking.html"), encoding="utf-8").read()
        assert "tracking.js" in html
        js = open(os.path.join(_SITE_DIR, "tracking.js"), encoding="utf-8").read()
        assert "./tracking_data.json" in js

    def test_no_external_assets(self):
        for name in ("tracking.html", "tracking.js"):
            text = open(os.path.join(_SITE_DIR, name), encoding="utf-8").read()
            # The SVG xmlns is a namespace identifier, not a fetched asset.
            text = text.replace("http://www.w3.org/2000/svg", "")
            assert "http://" not in text
            assert "https://" not in text

    def test_nav_links_on_every_page(self):
        for name in ("index.html", "scores.html", "visuals.html",
                     "architecture.html"):
            html = open(os.path.join(_SITE_DIR, name), encoding="utf-8").read()
            assert "./tracking.html" in html, name
