"""Tests for wca.scorespage and the static scores-page assets.

These exercise:

* the deterministic :func:`wca.scorespage.build_scores_data` builder against a
  synthetic card + a hand-built odds DataFrame (matching the exact columns the
  real ``theoddsapi.get_odds`` returns) + synthetic Polymarket quotes;
* team-name normalisation across "Bosnia & Herzegovina" vs
  "Bosnia and Herzegovina";
* hand-verified implied-probability and edge-vs-model arithmetic;
* the ``approx_1x2`` flag;
* graceful degradation when ``odds_df`` is missing (fixtures still emitted with
  empty ``venues``);
* the JSON write round-trip; and
* that the shipped ``site/scores.html`` references ``./scores_data.json`` and
  contains no external http(s) assets.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

import pandas as pd
import pytest

from wca import scorespage


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SITE_DIR = os.path.join(_REPO_ROOT, "site")


# ---------------------------------------------------------------------------
# Synthetic fixtures.
# ---------------------------------------------------------------------------

# Two fixtures. Mexico vs South Africa has a clean home/draw/away spread; the
# second uses "Bosnia and Herzegovina" (card spelling) to test normalisation
# against an odds feed that spells it "Bosnia & Herzegovina".
_SYNTH_CARD = """<!-- generated: 2026-06-11T11:52:08 -->
*World Cup Alpha — bet card* (1 picks)

*1. Mexico vs South Africa* — Mexico @ *1.44* (betfair_ex_uk)
    model 71.3% / mkt 69.3%  edge *+2.6%*  [elo 83% dc 64%]
    stake: main 14.79

*World Cup Alpha — scorelines* (2 fixtures)

*Mexico vs South Africa*
    1-0  16.9%  fair 5.91  back >= 6.03
    2-0  15.5%  fair 6.45  back >= 6.57
    2-1  10.2%  fair 9.84  back >= 10.03
    1-1  8.8%  fair 11.40  back >= 11.63
    0-0  7.6%  fair 13.09  back >= 13.36
    0-1  5.0%  fair 20.00  back >= 20.41
    O/U 2.5: over 45.8% / under 54.2%   BTTS 39.0%

*Bosnia and Herzegovina vs Wales*
    1-1  13.8%  fair 7.24  back >= 7.39
    1-0  13.0%  fair 7.69  back >= 7.84
    0-1  10.2%  fair 9.81  back >= 10.01
    O/U 2.5: over 38.0% / under 62.0%   BTTS 45.1%
"""

# Hand-computed model 1X2 for Mexico vs South Africa from the scores above.
#   home = 16.9 + 15.5 + 10.2 = 42.6
#   draw =  8.8 +  7.6        = 16.4
#   away =  5.0              =  5.0
#   total = 64.0
_MEX_HOME = 42.6 / 64.0  # 0.665625
_MEX_DRAW = 16.4 / 64.0  # 0.25625
_MEX_AWAY = 5.0 / 64.0   # 0.078125

# The odds-feed columns are exactly those documented for theoddsapi.get_odds.
_ODDS_COLUMNS = [
    "event_id",
    "commence_time",
    "home_team",
    "away_team",
    "bookmaker_key",
    "bookmaker_title",
    "market",
    "outcome_name",
    "outcome_point",
    "decimal_odds",
    "retrieved_at",
]


def _odds_row(**kw):
    base = {col: None for col in _ODDS_COLUMNS}
    base.update(kw)
    return base


def _synth_odds_df() -> pd.DataFrame:
    """One bookmaker for Mexico vs South Africa, plus a Bosnia fixture whose
    feed spelling uses '&' to exercise team-name normalisation."""
    rows = [
        # Mexico vs South Africa @ skybet — h2h three legs.
        _odds_row(
            event_id="E1", commence_time="2026-06-12T18:00:00+00:00",
            home_team="Mexico", away_team="South Africa",
            bookmaker_key="skybet", market="h2h",
            outcome_name="Mexico", decimal_odds=1.50,
        ),
        _odds_row(
            event_id="E1", commence_time="2026-06-12T18:00:00+00:00",
            home_team="Mexico", away_team="South Africa",
            bookmaker_key="skybet", market="h2h",
            outcome_name="Draw", decimal_odds=4.20,
        ),
        _odds_row(
            event_id="E1", commence_time="2026-06-12T18:00:00+00:00",
            home_team="Mexico", away_team="South Africa",
            bookmaker_key="skybet", market="h2h",
            outcome_name="South Africa", decimal_odds=11.00,
        ),
        # A totals row that must be ignored by the 1X2 venue builder.
        _odds_row(
            event_id="E1", commence_time="2026-06-12T18:00:00+00:00",
            home_team="Mexico", away_team="South Africa",
            bookmaker_key="skybet", market="totals",
            outcome_name="Over", outcome_point=2.5, decimal_odds=1.90,
        ),
        # Bosnia & Herzegovina (feed spelling) vs Wales @ williamhill.
        _odds_row(
            event_id="E2", commence_time="2026-06-13T15:00:00+00:00",
            home_team="Bosnia & Herzegovina", away_team="Wales",
            bookmaker_key="williamhill", market="h2h",
            outcome_name="Bosnia & Herzegovina", decimal_odds=2.20,
        ),
        _odds_row(
            event_id="E2", commence_time="2026-06-13T15:00:00+00:00",
            home_team="Bosnia & Herzegovina", away_team="Wales",
            bookmaker_key="williamhill", market="h2h",
            outcome_name="Draw", decimal_odds=3.30,
        ),
        _odds_row(
            event_id="E2", commence_time="2026-06-13T15:00:00+00:00",
            home_team="Bosnia & Herzegovina", away_team="Wales",
            bookmaker_key="williamhill", market="h2h",
            outcome_name="Wales", decimal_odds=3.40,
        ),
    ]
    return pd.DataFrame(rows, columns=_ODDS_COLUMNS)


def _write_card(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".md", prefix="wca_scores_card_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ---------------------------------------------------------------------------
# build_scores_data — structure & scorelines.
# ---------------------------------------------------------------------------


class TestStructure:
    def test_meta_and_fixtures_present(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, now_utc="2026-06-11 12:00 UTC")
        finally:
            os.unlink(card)
        assert data["meta"]["generated"] == "2026-06-11 12:00 UTC"
        names = [fx["fixture"] for fx in data["fixtures"]]
        assert names == [
            "Mexico vs South Africa",
            "Bosnia and Herzegovina vs Wales",
        ]

    def test_scores_carry_score_prob_fair_only(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card)
        finally:
            os.unlink(card)
        mex = data["fixtures"][0]
        top = mex["scores"][0]
        assert set(top.keys()) == {"score", "prob", "fair"}
        assert top["score"] == "1-0"
        assert top["prob"] == pytest.approx(16.9)
        assert top["fair"] == pytest.approx(5.91)

    def test_approx_1x2_flag_always_true(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card)
        finally:
            os.unlink(card)
        for fx in data["fixtures"]:
            assert fx["approx_1x2"] is True

    def test_model_1x2_renormalised_from_topk_scores(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card)
        finally:
            os.unlink(card)
        m = data["fixtures"][0]["model_1x2"]
        assert m["home"] == pytest.approx(_MEX_HOME)
        assert m["draw"] == pytest.approx(_MEX_DRAW)
        assert m["away"] == pytest.approx(_MEX_AWAY)
        # Proper distribution.
        assert m["home"] + m["draw"] + m["away"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Venue comparison — implied / edge math, hand-verified.
# ---------------------------------------------------------------------------


class TestVenues:
    def test_h2h_venue_prices_and_implied(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=_synth_odds_df())
        finally:
            os.unlink(card)

        mex = data["fixtures"][0]
        venues = {v["venue"]: v for v in mex["venues"]}
        assert "skybet" in venues
        sky = venues["skybet"]

        # Prices map to legs by outcome name (totals row ignored).
        assert sky["selection_prices"] == {
            "home": 1.50, "draw": 4.20, "away": 11.00,
        }
        # Implied = 1 / decimal.
        assert sky["implied"]["home"] == pytest.approx(1 / 1.50)
        assert sky["implied"]["draw"] == pytest.approx(1 / 4.20)
        assert sky["implied"]["away"] == pytest.approx(1 / 11.00)

    def test_edge_vs_model_hand_verified(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=_synth_odds_df())
        finally:
            os.unlink(card)

        sky = {v["venue"]: v for v in data["fixtures"][0]["venues"]}["skybet"]
        # edge = model_prob * decimal - 1.
        assert sky["edge_vs_model"]["home"] == pytest.approx(_MEX_HOME * 1.50 - 1)
        assert sky["edge_vs_model"]["draw"] == pytest.approx(_MEX_DRAW * 4.20 - 1)
        assert sky["edge_vs_model"]["away"] == pytest.approx(_MEX_AWAY * 11.00 - 1)

    def test_kickoff_derived_from_odds(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=_synth_odds_df())
        finally:
            os.unlink(card)
        mex = data["fixtures"][0]
        # The commence_time column is a plain ISO string in the synth frame.
        assert mex.get("kickoff", "").startswith("2026-06-12T18:00")

    def test_nat_commence_time_yields_no_kickoff(self) -> None:
        """A NaT commence_time (what get_odds produces when a date fails to
        parse via to_datetime errors='coerce') must NOT leak the string 'NaT'
        as a kickoff — the fixture simply omits the kickoff field."""
        df = pd.DataFrame(
            {
                "event_id": ["E1", "E1", "E1"],
                # NaT column exactly as to_datetime(errors="coerce") yields on
                # an unparseable date, without the dateutil-fallback warning.
                "commence_time": pd.Series([pd.NaT, pd.NaT, pd.NaT], dtype="datetime64[ns, UTC]"),
                "home_team": ["Mexico", "Mexico", "Mexico"],
                "away_team": ["South Africa", "South Africa", "South Africa"],
                "bookmaker_key": ["skybet", "skybet", "skybet"],
                "market": ["h2h", "h2h", "h2h"],
                "outcome_name": ["Mexico", "Draw", "South Africa"],
                "decimal_odds": [1.50, 4.20, 11.00],
            }
        )
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=df)
        finally:
            os.unlink(card)
        mex = data["fixtures"][0]
        # Venue still attached, but no bogus 'NaT' kickoff.
        assert {v["venue"] for v in mex["venues"]} == {"skybet"}
        assert "kickoff" not in mex or mex["kickoff"] != "NaT"
        assert mex.get("kickoff", "") == ""

    def test_team_name_normalisation_bosnia(self) -> None:
        """Card 'Bosnia and Herzegovina' must match feed 'Bosnia &
        Herzegovina' so the williamhill venue attaches to that fixture."""
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=_synth_odds_df())
        finally:
            os.unlink(card)
        bosnia = data["fixtures"][1]
        assert bosnia["fixture"] == "Bosnia and Herzegovina vs Wales"
        venues = {v["venue"]: v for v in bosnia["venues"]}
        assert "williamhill" in venues
        assert venues["williamhill"]["selection_prices"]["home"] == pytest.approx(2.20)
        assert venues["williamhill"]["selection_prices"]["away"] == pytest.approx(3.40)


# ---------------------------------------------------------------------------
# Polymarket quotes.
# ---------------------------------------------------------------------------


class TestPolymarket:
    def test_pm_quote_converted_to_decimal_and_edged(self) -> None:
        card = _write_card(_SYNTH_CARD)
        pm = {
            "Mexico vs South Africa": {"home": 0.70, "draw": 0.22, "away": 0.08},
        }
        try:
            data = scorespage.build_scores_data(
                card, odds_df=_synth_odds_df(), pm_quotes=pm,
            )
        finally:
            os.unlink(card)
        venues = {v["venue"]: v for v in data["fixtures"][0]["venues"]}
        assert "polymarket" in venues
        poly = venues["polymarket"]
        # 0..1 probability -> decimal price 1/p.
        assert poly["selection_prices"]["home"] == pytest.approx(1 / 0.70)
        assert poly["selection_prices"]["draw"] == pytest.approx(1 / 0.22)
        assert poly["selection_prices"]["away"] == pytest.approx(1 / 0.08)
        # Edge uses the converted decimal.
        assert poly["edge_vs_model"]["home"] == pytest.approx(
            _MEX_HOME * (1 / 0.70) - 1
        )

    def test_pm_quote_matched_via_team_name_normalisation(self) -> None:
        """A pm_quotes key spelled with '&' must still match the card's
        'Bosnia and Herzegovina vs Wales' fixture."""
        card = _write_card(_SYNTH_CARD)
        pm = {
            "Bosnia & Herzegovina vs Wales": {
                "home": 0.45, "draw": 0.30, "away": 0.25,
            },
        }
        try:
            data = scorespage.build_scores_data(card, pm_quotes=pm)
        finally:
            os.unlink(card)
        bosnia = data["fixtures"][1]
        venues = {v["venue"]: v for v in bosnia["venues"]}
        assert "polymarket" in venues
        assert venues["polymarket"]["selection_prices"]["home"] == pytest.approx(
            1 / 0.45
        )


# ---------------------------------------------------------------------------
# Graceful degradation.
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_missing_odds_df_still_emits_fixtures_with_empty_venues(self) -> None:
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=None)
        finally:
            os.unlink(card)
        assert len(data["fixtures"]) == 2
        for fx in data["fixtures"]:
            assert fx["venues"] == []
            # Still has scores + an approximate model 1X2.
            assert fx["scores"]
            assert fx["model_1x2"] is not None

    def test_missing_card_yields_empty_fixtures(self) -> None:
        data = scorespage.build_scores_data("/no/such/card.md")
        assert data["fixtures"] == []
        assert data["meta"]["generated"] == ""

    def test_nan_team_cells_do_not_raise(self) -> None:
        """A malformed feed row with NaN home/away team cells (which pandas
        produces from concat/merge/reindex or an all-missing float column) must
        not crash the build — the tolerant contract. The valid Mexico row still
        matches; the NaN row is silently skipped."""
        import numpy as np

        df = pd.DataFrame(
            {
                "home_team": pd.Series(["Mexico", np.nan]),
                "away_team": pd.Series(["South Africa", np.nan]),
                "market": ["h2h", "h2h"],
                "bookmaker_key": ["skybet", "skybet"],
                "outcome_name": ["Mexico", "Foo"],
                "decimal_odds": [1.50, 2.0],
                "commence_time": ["2026-06-12T18:00:00+00:00", None],
            }
        )
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=df)
        finally:
            os.unlink(card)
        mex = data["fixtures"][0]
        venues = {v["venue"]: v for v in mex["venues"]}
        assert "skybet" in venues
        assert venues["skybet"]["selection_prices"]["home"] == pytest.approx(1.50)

    def test_unmatched_fixture_has_empty_venues(self) -> None:
        # Odds frame contains a totally different fixture.
        df = pd.DataFrame(
            [
                _odds_row(
                    event_id="EX", home_team="Brazil", away_team="Spain",
                    bookmaker_key="skybet", market="h2h",
                    outcome_name="Brazil", decimal_odds=2.0,
                ),
            ],
            columns=_ODDS_COLUMNS,
        )
        card = _write_card(_SYNTH_CARD)
        try:
            data = scorespage.build_scores_data(card, odds_df=df)
        finally:
            os.unlink(card)
        for fx in data["fixtures"]:
            assert fx["venues"] == []


# ---------------------------------------------------------------------------
# Write round-trip.
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_round_trip_valid_json(self) -> None:
        card = _write_card(_SYNTH_CARD)
        out_dir = tempfile.mkdtemp(prefix="wca_scores_out_")
        out_path = os.path.join(out_dir, "nested", "scores_data.json")
        try:
            returned = scorespage.write_scores_data(
                card_path=card,
                out_path=out_path,
                odds_df=_synth_odds_df(),
                pm_quotes={"Mexico vs South Africa": {"home": 0.7, "draw": 0.2, "away": 0.1}},
                now_utc="2026-06-11 12:00 UTC",
            )
            assert returned == out_path
            assert os.path.exists(out_path)
            with open(out_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            assert loaded["meta"]["generated"] == "2026-06-11 12:00 UTC"
            assert loaded["fixtures"][0]["fixture"] == "Mexico vs South Africa"
            # The polymarket venue survived the round-trip.
            venues = {v["venue"] for v in loaded["fixtures"][0]["venues"]}
            assert "polymarket" in venues
        finally:
            os.unlink(card)
            if os.path.exists(out_path):
                os.unlink(out_path)


# ---------------------------------------------------------------------------
# Static asset checks.
# ---------------------------------------------------------------------------


class TestSiteAssets:
    def test_scores_html_references_scores_data_and_js(self) -> None:
        with open(os.path.join(_SITE_DIR, "scores.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        assert "./scores.js" in html
        with open(os.path.join(_SITE_DIR, "scores.js"), "r", encoding="utf-8") as fh:
            js = fh.read()
        assert "./scores_data.json" in js

    def test_scores_html_has_no_external_assets(self) -> None:
        with open(os.path.join(_SITE_DIR, "scores.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        urls = re.findall(r"https?://[^\s\"'<>]+", html)
        assert urls == [], "unexpected external URLs in scores.html: %r" % urls

    def test_scores_js_has_no_external_assets(self) -> None:
        with open(os.path.join(_SITE_DIR, "scores.js"), "r", encoding="utf-8") as fh:
            js = fh.read()
        urls = re.findall(r"https?://[^\s\"'<>]+", js)
        assert urls == [], "unexpected external URLs in scores.js: %r" % urls

    def test_index_links_to_scores_page(self) -> None:
        with open(os.path.join(_SITE_DIR, "index.html"), "r", encoding="utf-8") as fh:
            html = fh.read()
        assert "./scores.html" in html
