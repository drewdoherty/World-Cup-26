"""Percent display convention + selection-rule visibility (user ruling 2026-07-08).

Pins the bot-command overhaul:

1. **Percent format everywhere** — every command shows ``model X% / mkt Y%``;
   an executable decimal price appears ONLY as its implied % (venue tagged);
   the Polymarket ¢ convention survives (¢ IS a percent).
2. **+EV indicated everywhere** — every displayed selection carries its edge
   plus an explicit ``✅+EV`` / ``❌−EV`` marker (``EV?`` when no live price),
   ordered by the CANONICAL rule (:mod:`wca.selection` — imported, never
   re-implemented).
3. **Widened display, unchanged gates** — /card gains a WATCH tier
   (near-threshold 0–2pp rows + withheld reason_code telemetry) that is
   display-only: the staked output is byte-identical with or without it.
4. **NEW /matchevents** — +EV MONEYLINE-bucket exotics only (boundary at
   model 0.50), killed-for-cash markets excluded, defensive feed consumption
   with honest empty-feed lines.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

from wca import displayfmt  # noqa: E402
from wca.bot import app  # noqa: E402
from wca.card import (  # noqa: E402
    PoolConfig,
    RankedCard,
    Recommendation,
    format_ranked_card,
    rank_card,
)
from wca.selection import preference_sort_key  # noqa: E402

# A bare decimal price like "@ 2.05" / "@ *4.08*" — banned on every surface.
_DECIMAL_ODDS_RE = re.compile(r"@\s*\*?\d+\.\d{2}\*?(?!\d)(?!\s*impl)(?!¢)")


# ---------------------------------------------------------------------------
# wca.displayfmt — the shared helpers.
# ---------------------------------------------------------------------------


class TestDisplayFmt:
    def test_pct_and_implied(self):
        assert displayfmt.pct(0.579) == "57.9%"
        assert displayfmt.pct(None) == "?"
        assert displayfmt.implied_pct(4.0) == "25.0%"
        assert displayfmt.implied_pct(None) == "?"
        assert displayfmt.implied_pct(0.9) == "?"  # unusable price, no fabrication

    def test_edge_and_ev_strings(self):
        assert displayfmt.edge_pp(0.058) == "+5.8pp"
        assert displayfmt.edge_pp(-0.012) == "-1.2pp"
        assert displayfmt.ev_str(0.058) == "+5.8%"

    def test_ev_marker(self):
        assert displayfmt.ev_marker(0.01) == "✅+EV"
        assert displayfmt.ev_marker(-0.01) == "❌−EV"
        assert displayfmt.ev_marker(0.0) == "❌−EV"  # zero edge is not +EV
        # No live price -> +EV is UNVERIFIABLE, never dressed up as +EV.
        assert displayfmt.ev_marker(None) == "EV?"

    def test_bucket_tag_boundaries_match_wca_selection(self):
        # Inclusive lower bounds per the canonical rule.
        assert displayfmt.bucket_tag(0.50) == "ML"
        assert displayfmt.bucket_tag(0.4999) == "MID"
        assert displayfmt.bucket_tag(0.25) == "MID"
        assert displayfmt.bucket_tag(0.2499) == "LS"


# ---------------------------------------------------------------------------
# /card — format_ranked_card: percent format, rule ordering, WATCH separation.
# ---------------------------------------------------------------------------


def _rec(team, model, edge, h2k, odds=2.5, mkt=None, category="favourite",
         watch=False, stake=10.0):
    return Recommendation(
        match_id="m-%s" % team,
        match_desc="%s vs Foo" % team,
        commence_time="2026-07-12T18:00:00Z",
        selection="home",
        selection_team=team,
        best_book="smarkets",
        best_odds=odds,
        model_prob=model,
        market_prob=mkt if mkt is not None else model - edge / 2,
        elo_prob=model,
        dc_prob=model,
        edge=edge,
        ev_per_unit=edge,
        stakes={"sb": 0.0 if watch else stake},
        venue="smarkets",
        raw_edge=edge,
        hours_to_kickoff=h2k,
        imminent=False,
        category=category,
        indicative=False,
        watch=watch,
    )


def _mixed_recs():
    """Mixed feed exercising bucket > hours-term > EV (the canonical key).

    Trade-card recs are 90-min MATCH markets, so post-2026-07-09 the hours-out
    term is NEUTRAL and EV breaks ties within the bucket.
    """
    return [
        # MID bucket, huge EV, furthest out — must still rank BELOW every ML.
        _rec("MidHighEV", model=0.30, edge=0.20, h2k=200.0),
        # ML bucket, near kickoff, BEST EV among MLs -> ranks first (EV wins).
        _rec("MlNear", model=0.55, edge=0.10, h2k=5.0),
        # ML bucket, further out, LOWER EV -> ranks below MlNear (hours neutral).
        _rec("MlFar", model=0.60, edge=0.05, h2k=72.0),
    ]


class TestCardPercentAndOrdering:
    def test_ordering_matches_wca_selection(self):
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        ranked = rank_card(_mixed_recs())
        text = format_ranked_card(ranked, pools)
        # The rendered order must follow the canonical key: any ML above the
        # MID regardless of EV (MidHighEV has the biggest EV of the slate), and
        # for MATCH markets EV breaks ties within the ML bucket (MlNear over
        # MlFar — the higher EV; hours-out is neutral post-2026-07-09).
        pos = {team: text.index(team) for team in ("MlFar", "MlNear", "MidHighEV")}
        assert pos["MlNear"] < pos["MlFar"] < pos["MidHighEV"]
        # Cross-check against the CANONICAL key itself (never re-derived): the
        # MID row sorts last through wca.selection.preference_sort_key, and the
        # match default keeps the hours term neutral.
        proposals = [{"model_prob": r.model_prob, "ev": r.edge,
                      "match_desc": r.match_desc, "team": r.selection_team}
                     for r in _mixed_recs()]
        expected = sorted(proposals, key=lambda p: preference_sort_key(p, {}))
        assert expected[-1]["team"] == "MidHighEV"
        assert [p["team"] for p in expected] == ["MlNear", "MlFar", "MidHighEV"]

    def test_percent_format_no_decimal_odds(self):
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        text = format_ranked_card(rank_card(_mixed_recs()), pools)
        assert not _DECIMAL_ODDS_RE.search(text), text
        # Executable price shown as implied % with the venue tagged.
        assert "back *40.0%* impl via *smarkets*" in text
        assert "model 60.0%" in text
        # +EV marker + bucket tags visible.
        assert "✅+EV" in text
        assert "[ML·FAV]" in text and "[MID·FAV]" in text
        # The ordering convention is stated on the card.
        assert "wca.selection" in text

    def test_watch_tier_separated_and_after_picks(self):
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        watch = [_rec("WatchRow", model=0.62, edge=0.01, h2k=30.0, watch=True,
                      stake=0.0)]
        text = format_ranked_card(rank_card(_mixed_recs()), pools, watch=watch)
        assert "— WATCH (near-threshold" in text
        assert "NOT staked" in text
        # Watch rows render after every staked pick and carry the marker.
        watch_hdr = text.index("— WATCH")
        for team in ("MlFar", "MlNear", "MidHighEV"):
            assert text.index(team) < watch_hdr
        wline = text[watch_hdr:]
        assert "WatchRow" in wline and "✅+EV" in wline
        assert "~ [ML]" in wline

    def test_no_watch_tier_when_empty(self):
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        text = format_ranked_card(rank_card(_mixed_recs()), pools, watch=[])
        assert "WATCH" not in text


class TestBuildCardWatchSink:
    """watch_sink widens the DISPLAY only — staked output is invariant."""

    @pytest.fixture(scope="class")
    def slate(self):
        from tests.test_scores import (
            _synthetic_fixtures_meta,
            _synthetic_odds,
            _synthetic_results,
        )
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        models = fit_models(_synthetic_results(rng), half_life_years=8.0)
        return models, _synthetic_odds(), _synthetic_fixtures_meta()

    def test_staked_output_invariant_and_watch_band(self, slate):
        from wca.card import build_card

        models, odds, meta = slate
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        min_edge = 0.02
        sink = []
        recs_with = build_card(models, odds, pools, meta, min_edge=min_edge,
                               watch_sink=sink)
        recs_without = build_card(models, odds, pools, meta, min_edge=min_edge)
        assert recs_with == recs_without  # gate untouched, byte-identical
        for w in sink:
            assert w.watch is True
            assert 0.0 <= w.edge < min_edge  # the near-threshold band only
            assert all(s == 0.0 for s in w.stakes.values())  # never staked

    def test_watch_rows_collected_below_a_high_floor(self, slate, monkeypatch):
        """With a high floor every +EV row lands in the sink — proving the
        sink actually collects (the synthetic slate has +EV outcomes).

        The LIVE shrink-to-market (WCA_SHRINK_LIVE, default on) compresses the
        synthetic model-vs-market edges toward zero, which is orthogonal to the
        watch-sink MECHANISM under test here — so pin the flag off to keep the
        raw +EV edges the mechanism needs. The shrink itself is exercised in
        tests/test_shrink_live.py.
        """
        monkeypatch.setenv("WCA_SHRINK_LIVE", "0")
        from wca.card import build_card

        models, odds, meta = slate
        pools = [PoolConfig(name="sb", bankroll=1000.0)]
        sink = []
        recs = build_card(models, odds, pools, meta, min_edge=0.99,
                          watch_sink=sink)
        assert recs == []
        assert sink, "expected the +EV synthetic outcomes in the watch sink"


# ---------------------------------------------------------------------------
# /card handler — WATCH/WITHHELD telemetry appendix from site/bet_recs.json.
# ---------------------------------------------------------------------------


def _bet_recs_feed():
    return {
        "meta": {"generated": "2026-07-09 09:00:00 UTC"},
        "match_singles": [], "advancement_futures": [], "event_props": [],
        "withheld": [
            {  # near-threshold: 0 <= edge < 2pp -> listed individually
                "fixture": "Alpha vs Bravo", "selection": "home",
                "model_prob": 0.55, "price": 1.869, "edge": 0.015,
                "ev_net": 0.01, "reason_code": "edge_below_floor",
                "withheld_reason": "edge_below_floor:0.0150<0.0200",
            },
            {  # negative edge -> summarised by reason_code only
                "fixture": "Charlie vs Delta", "selection": "away",
                "model_prob": 0.58, "price": 1.618, "edge": -0.038,
                "reason_code": "edge_below_floor",
                "withheld_reason": "edge_below_floor:-0.0380<0.0200",
            },
            {
                "fixture": "Echo vs Foxtrot", "selection": "home",
                "model_prob": 0.22, "price": 6.0, "edge": 0.05,
                "reason_code": "longshot_no_cash",
                "withheld_reason": "longshot filter",
            },
        ],
    }


class TestHandleCardWatchSection:
    def test_near_threshold_listed_with_reason_code(self, tmp_path):
        from wca import cardcache

        recs_path = tmp_path / "bet_recs.json"
        recs_path.write_text(json.dumps(_bet_recs_feed()), encoding="utf-8")
        card_path = str(tmp_path / "card_latest.md")
        cardcache.write_card("*World Cup Alpha — bet card* (0 staked picks)",
                             card_path, ts_utc="2026-07-09T09:30:00")
        out = app.handle_card("unused.db", card_path=card_path,
                              now_utc="2026-07-09T10:00:00",
                              recs_path=str(recs_path))
        assert "WATCH / WITHHELD" in out
        # The near-threshold row: percent format + marker + reason_code.
        assert "[ML] Alpha vs Bravo — home" in out
        assert "model 55.0%" in out
        assert "mkt 53.5%" in out          # implied % of 1.869
        assert "+1.5pp" in out and "✅+EV" in out
        assert "edge_below_floor" in out
        # The others are summarised by reason_code, count included.
        assert "longshot_no_cash ×1" in out
        # Negative-edge row is NOT in the near-threshold list.
        assert "[ML] Charlie vs Delta" not in out
        assert not _DECIMAL_ODDS_RE.search(out), out

    def test_missing_feed_yields_honest_line(self, tmp_path):
        from wca import cardcache

        card_path = str(tmp_path / "card_latest.md")
        cardcache.write_card("*World Cup Alpha — bet card* (0 staked picks)",
                             card_path, ts_utc="2026-07-09T09:30:00")
        out = app.handle_card("unused.db", card_path=card_path,
                              now_utc="2026-07-09T10:00:00",
                              recs_path=str(tmp_path / "missing.json"))
        assert "watch tier unavailable" in out


# ---------------------------------------------------------------------------
# /matchevents — moneyline-bucket +EV exotics only.
# ---------------------------------------------------------------------------


def _event_feed(rows):
    return {"meta": {"generated": "2026-07-09 09:00:00 UTC"}, "recs": rows}


def _event_row(**kw):
    row = {
        "fixture": "Alpha vs Bravo", "market": "totals",
        "selection": "Under 2.5", "model_prob": 0.62, "market_prob": 0.55,
        "venue": "polymarket", "settlement": "90-min",
        "kickoff": "2099-12-31T20:00:00+00:00",
    }
    row.update(kw)
    return row


class TestMatchEvents:
    def test_moneyline_bucket_boundary_at_050(self, tmp_path):
        p = tmp_path / "event_market_recs.json"
        p.write_text(json.dumps(_event_feed([
            _event_row(selection="AT the boundary", model_prob=0.50,
                       market_prob=0.45),
            _event_row(selection="BELOW the boundary", model_prob=0.4999,
                       market_prob=0.40),
        ])), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(p),
                                     scores_path=str(tmp_path / "none.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        # 0.50 is INSIDE the moneyline bucket (inclusive lower bound).
        assert "AT the boundary" in out
        assert "BELOW the boundary" not in out
        assert "below the moneyline bucket" in out

    def test_positive_net_edge_required(self, tmp_path):
        p = tmp_path / "event_market_recs.json"
        p.write_text(json.dumps(_event_feed([
            _event_row(selection="PlusEV", model_prob=0.62, market_prob=0.55),
            _event_row(selection="MinusEV", model_prob=0.55, market_prob=0.60),
            _event_row(selection="ZeroEdge", model_prob=0.55, market_prob=0.55),
        ])), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(p),
                                     scores_path=str(tmp_path / "none.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        actionable = out[out.index("*Actionable"):]
        head = actionable[:actionable.index("_excluded")]
        assert "PlusEV" in head
        assert "MinusEV" not in head and "ZeroEdge" not in head
        assert "−EV or zero-edge" in out

    def test_row_format_pct_edge_stake_settlement(self, tmp_path):
        p = tmp_path / "event_market_recs.json"
        p.write_text(json.dumps(_event_feed([_event_row()])), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(p),
                                     scores_path=str(tmp_path / "none.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        assert "model 62.0% / mkt 55.0% (polymarket)" in out
        assert "+7.0pp" in out and "✅+EV" in out
        assert "[ML]" in out
        assert "settles 90-min" in out
        # PM venue -> display-only ¼-Kelly $ stake (or an honest n/a).
        assert ("PM ¼-Kelly, display-only" in out) or ("stake n/a" in out)
        assert not _DECIMAL_ODDS_RE.search(out), out

    def test_killed_markets_never_actionable(self, tmp_path):
        p = tmp_path / "event_market_recs.json"
        p.write_text(json.dumps(_event_feed([
            _event_row(selection="exact 1-0", market="exact_score",
                       model_prob=0.60, market_prob=0.40),
        ])), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(p),
                                     scores_path=str(tmp_path / "none.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        assert "exact 1-0" not in out[out.index("*Actionable"):].split("_excluded")[0]
        assert "killed-for-cash" in out

    def test_ordering_match_ev_first_hours_neutral(self, tmp_path):
        p = tmp_path / "event_market_recs.json"
        p.write_text(json.dumps(_event_feed([
            _event_row(fixture="Near vs Kick", selection="NearRow",
                       model_prob=0.60, market_prob=0.50,  # bigger edge
                       kickoff="2099-01-01T20:00:00+00:00"),
            _event_row(fixture="Far vs Kick", selection="FarRow",
                       model_prob=0.55, market_prob=0.50,  # smaller edge, further out
                       kickoff="2099-06-01T20:00:00+00:00"),
        ])), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(p),
                                     scores_path=str(tmp_path / "none.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        # /matchevents is a single-match 90-min exotics view = MATCH markets.
        # Post-2026-07-09 the hours-out term is NEUTRAL, so within the shared
        # moneyline bucket the BIGGER-EDGE row ranks first (NearRow), NOT the
        # further-out one — the OPPOSITE of the pre-2026-07-09 rule.
        assert out.index("NearRow") < out.index("FarRow")

    def test_missing_feed_falls_back_with_honest_hint(self, tmp_path):
        scores = {
            "meta": {"generated": "2026-07-09 09:00:00 UTC"},
            "fixtures": [{
                "fixture": "Alpha vs Bravo",
                "kickoff": "2099-12-31T20:00:00+00:00",
                "over_under": {"line": 2.5, "over": 38.1, "under": 61.9},
                "btts": 40.3,
                "scores": [{"score": "1-0", "prob": 17.5, "pm_prob": 13.5}],
            }],
        }
        sp = tmp_path / "scores_data.json"
        sp.write_text(json.dumps(scores), encoding="utf-8")
        out = app.handle_matchevents(recs_path=str(tmp_path / "absent.json"),
                                     scores_path=str(sp),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        # Honest hint that the real feed is not built yet.
        assert "event-market feed not yet built" in out
        assert "wca_event_markets.py" in out
        # Model-only rows (no live totals/BTTS prices): shown, NOT actionable.
        assert "Model-only" in out and "+EV unverifiable" in out
        assert "Under 2.5 goals" in out and "model 61.9%" in out
        assert "BTTS No" in out  # 1 - 40.3% = 59.7% -> moneyline bucket
        # Exact score: killed market, counted as excluded, never actionable.
        assert "killed-for-cash" in out
        # No live-priced actionable rows -> the honest none-line.
        assert "none — no live-priced moneyline-bucket exotic" in out

    def test_both_feeds_missing_is_honest(self, tmp_path):
        out = app.handle_matchevents(recs_path=str(tmp_path / "a.json"),
                                     scores_path=str(tmp_path / "b.json"),
                                     db_path=str(tmp_path / "db.db"),
                                     now_utc="2026-07-09T10:00:00")
        assert "no event-market candidates" in out
        assert "the feeds carry no rows" in out

    def test_registered_in_dispatch_and_help(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "EVENT_RECS_PATH", str(tmp_path / "a.json"))
        monkeypatch.setattr(app, "SCORES_FEED_PATH", str(tmp_path / "b.json"))
        monkeypatch.chdir(tmp_path)
        out = app.dispatch("/matchevents", db_path=str(tmp_path / "db.db"))
        assert "Match events" in out
        assert "/matchevents" in app.HELP_TEXT
        assert any(c["command"] == "matchevents" for c in app._TELEGRAM_COMMANDS)


# ---------------------------------------------------------------------------
# /today — percent + markers; /pm — ¢ + markers; /boost — implied %.
# ---------------------------------------------------------------------------


class TestTodayPercent:
    def test_match_single_row_percent_and_marker(self, tmp_path):
        recs = {
            "meta": {"generated": "2026-07-09 09:00:00 UTC"},
            "match_singles": [{
                "fixture": "France vs Morocco", "selection": "home",
                "model_prob": 0.5796, "price": 1.6179, "ev_net": -0.0623,
                "stake": 0.0,
            }],
            "advancement_futures": [{
                "team": "Brazil", "stage": "SF", "model_prob": 0.62,
                "pm_price": 0.45, "ev_net": 0.15, "stake": 40.0,
            }],
        }
        rp = tmp_path / "bet_recs.json"
        rp.write_text(json.dumps(recs), encoding="utf-8")
        out = app.handle_today(db_path=str(tmp_path / "db.db"),
                               recs_path=str(rp),
                               ideas_path=str(tmp_path / "ideas.json"),
                               promos_path=str(tmp_path / "promos.json"))
        assert "model 58.0% / mkt 61.8%" in out
        assert "❌−EV" in out
        assert "[1X2·ML]" in out
        # PM advancement: ¢ convention (¢ IS a percent) + marker.
        assert "mkt 45¢ (PM)" in out and "✅+EV" in out
        assert not _DECIMAL_ODDS_RE.search(out), out


class TestParkedOrderPercent:
    def test_cents_bucket_and_marker(self):
        proposal = {
            "match_desc": "Alpha vs Bravo",
            "market_question": "Will Alpha win?", "outcome": "Yes",
            "price": 0.24, "size": 100.0, "size_usd": 24.0,
            "model_prob": 0.30, "ev": 0.28,
        }
        out = app.format_parked_order("PM-1", proposal)
        assert "@ 24¢" in out
        assert "model 30.0¢" in out
        assert "EV +28.0% ✅+EV" in out
        assert "[MID]" in out
        assert "@ 0.24" not in out  # bare decimal-probability form retired


class TestBoostPercent:
    def test_verdict_shows_implied_pct_not_decimals(self):
        boost = SimpleNamespace(site="bet365", fixture="Brazil vs Morocco",
                                market="Match Result", selection="Brazil",
                                boosted_odds=2.5, was_odds=1.8)
        ev = SimpleNamespace(priceable=True, model_prob=0.45, fair_odds=2.22,
                             edge=0.125, is_plus_ev=True, reason="model blend")
        out = app.format_boost_verdict(boost, ev)
        assert "@ 40.0% impl (bet365)" in out
        assert "was 55.6% impl" in out
        assert "model 45.0%" in out
        assert "✅ *+EV*" in out
        assert "2.50" not in out and "1.80" not in out and "2.22" not in out


# ---------------------------------------------------------------------------
# /goalscorers — no-cash flag on <25% model legs (canonical floor).
# ---------------------------------------------------------------------------


class TestScorerNoCash:
    def test_sub25_model_leg_flagged_no_cash_and_unstaked(self):
        from wca.nextmatch import GoalscorerFixture, GoalscorerLine
        from wca.nextmatch import format_goalscorer_card

        leg = GoalscorerLine(
            player="Long Shot", team="Alpha",
            anytime_book_odds=6.0, anytime_book="Book A",
            first_book_odds=None, first_book=None, xg_per_game=0.2,
            model_p_anytime=0.20, model_fair_anytime=5.0,
            model_p_first=None, model_fair_first=None,
        )
        fx = GoalscorerFixture(
            home="Alpha", away="Bravo", commence_time="2026-07-12T18:00:00Z",
            goalscorers={"home": [leg], "away": []},
        )
        text = format_goalscorer_card([fx])
        # +EV (0.2*6-1 = +20%) but <25% model -> flagged, NO cash stake.
        assert "(<25% model — NO CASH)" in text
        line = [ln for ln in text.splitlines() if "Any" in ln][0]
        assert "£" not in line
        assert "−73.9% leak" in text
