"""Tests for the /next next-match preview card (wca.nextmatch + bot handler)."""

import numpy as np
import pandas as pd
import pytest

import json

from wca.nextmatch import (
    ANYTIME_SCORER_MARKET,
    FIRST_SCORER_MARKET,
    GoalscorerFixture,
    GoalscorerLine,
    build_goalscorer_card,
    build_goalscorers,
    build_next_match,
    format_goalscorer_card,
    format_next_match,
    select_next_blends,
    top_scorers_from_odds,
)


# ---------------------------------------------------------------------------
# Synthetic slate (same shape as tests/test_scores.py).
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=200):
    teams = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    base = pd.Timestamp("2022-01-01")
    rows = []
    for k in range(n):
        i, j = rng.choice(len(teams), size=2, replace=False)
        rows.append(
            {
                "date": base + pd.Timedelta(days=int(k)),
                "home_team": teams[i],
                "away_team": teams[j],
                "home_score": int(rng.poisson(1.5)),
                "away_score": int(rng.poisson(1.1)),
                "tournament": "Friendly",
                "neutral": False,
            }
        )
    return pd.DataFrame(rows)


def _odds_rows(event_id, home, away, commence, book_prices):
    rows = []
    for book, prices in book_prices.items():
        for name, odd in prices.items():
            rows.append(
                dict(
                    event_id=event_id,
                    home_team=home,
                    away_team=away,
                    commence_time=commence,
                    market="h2h",
                    bookmaker_key=book,
                    outcome_name=name,
                    decimal_odds=odd,
                )
            )
    return rows


def _synthetic_odds():
    """Two fixtures; evt_late listed FIRST so kickoff ordering is exercised."""
    rows = _odds_rows(
        "evt_late", "Charlie", "Delta", "2026-06-12T18:00:00Z",
        {"book_a": {"Charlie": 2.2, "Draw": 3.3, "Delta": 3.4}},
    )
    rows += _odds_rows(
        "evt_next", "Alpha", "Bravo", "2026-06-11T18:00:00Z",
        {
            "book_a": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.60},
            "book_b": {"Alpha": 2.05, "Draw": 3.30, "Bravo": 3.80},
        },
    )
    return pd.DataFrame(rows)


def _synthetic_odds_simultaneous():
    """Three fixtures: two kick off simultaneously (earliest), one later."""
    rows = _odds_rows(
        "evt_alpha", "Alpha", "Bravo", "2026-06-11T18:00:00Z",
        {"book_a": {"Alpha": 2.10, "Draw": 3.40, "Bravo": 3.60}},
    )
    rows += _odds_rows(
        "evt_charlie", "Charlie", "Delta", "2026-06-11T18:00:00Z",
        {"book_a": {"Charlie": 2.2, "Draw": 3.3, "Delta": 3.4}},
    )
    rows += _odds_rows(
        "evt_late", "Echo", "Alpha", "2026-06-12T18:00:00Z",
        {"book_a": {"Echo": 3.0, "Draw": 3.2, "Alpha": 2.4}},
    )
    return pd.DataFrame(rows)


def _synthetic_fixtures_meta_simultaneous():
    return pd.DataFrame(
        [
            {
                "home_team": h,
                "away_team": a,
                "neutral": False,
                "country": "",
                "home_score": __import__("numpy").nan,
                "away_score": __import__("numpy").nan,
            }
            for h, a in (("Alpha", "Bravo"), ("Charlie", "Delta"), ("Echo", "Alpha"))
        ]
    )


def _synthetic_fixtures_meta():
    return pd.DataFrame(
        [
            {
                "home_team": h,
                "away_team": a,
                "neutral": False,
                "country": "",
                "home_score": np.nan,
                "away_score": np.nan,
            }
            for h, a in (("Alpha", "Bravo"), ("Charlie", "Delta"))
        ]
    )


def _scorer_df():
    rows = []
    for player, book, odds in [
        ("Striker One", "Book A", 2.4),
        ("Striker One", "Book B", 2.6),
        ("Mid Two", "Book A", 4.0),
        ("Defender Three", "Book A", 9.0),
    ]:
        rows.append(
            dict(
                event_id="evt_next",
                market=ANYTIME_SCORER_MARKET,
                bookmaker_key=book.lower().replace(" ", "_"),
                bookmaker_title=book,
                # Real Odds API shape: player props carry the player in
                # ``description`` with outcome name "Yes".
                outcome_name="Yes",
                outcome_description=player,
                decimal_odds=odds,
            )
        )
    return pd.DataFrame(rows)


def _gs_scorer_df():
    """Anytime + first-goalscorer rows for Alpha/Bravo players (Odds API shape)."""
    rows = []
    anytime = [
        ("Alpha Striker", "Book A", 2.5),
        ("Alpha Mid", "Book A", 4.0),
        ("Bravo Striker", "Book A", 3.0),
        ("Bravo Winger", "Book B", 6.0),
        ("Bench Unknown", "Book A", 12.0),  # not in any squad list
    ]
    first = [
        ("Alpha Striker", "Book A", 6.0),
        ("Bravo Striker", "Book B", 8.0),
    ]
    for player, book, odds in anytime:
        rows.append(dict(
            event_id="evt_next", market=ANYTIME_SCORER_MARKET,
            bookmaker_key=book.lower().replace(" ", "_"), bookmaker_title=book,
            outcome_name="Yes", outcome_description=player, decimal_odds=odds,
        ))
    for player, book, odds in first:
        rows.append(dict(
            event_id="evt_next", market=FIRST_SCORER_MARKET,
            bookmaker_key=book.lower().replace(" ", "_"), bookmaker_title=book,
            outcome_name="Yes", outcome_description=player, decimal_odds=odds,
        ))
    return pd.DataFrame(rows)


def _write_squads(tmp_path):
    path = tmp_path / "squads.json"
    path.write_text(json.dumps({
        "_note": "test",
        "Alpha": ["Alpha Striker", "Alpha Mid"],
        "Bravo": ["Bravo Striker", "Bravo Winger"],
    }))
    return str(path)


# ---------------------------------------------------------------------------
# Goalscorers (top 2 per team, anytime + first, book + Polymarket).
# ---------------------------------------------------------------------------


class TestBuildGoalscorers:
    def test_splits_top_two_per_team_with_both_markets(self, tmp_path):
        gs, note = build_goalscorers(
            "Alpha", "Bravo", _gs_scorer_df(),
            squads_path=_write_squads(tmp_path), pm_lookup=False,
        )
        assert [l.player for l in gs["home"]] == ["Alpha Striker", "Alpha Mid"]
        assert [l.player for l in gs["away"]] == ["Bravo Striker", "Bravo Winger"]
        striker = gs["home"][0]
        # Best book anytime odds + first-goalscorer odds attached.
        assert striker.anytime_book_odds == 2.5 and striker.anytime_book == "Book A"
        assert striker.first_book_odds == 6.0
        # Market-implied goals/game = -ln(1 - 1/2.5) = -ln(0.6).
        assert striker.xg_per_game == pytest.approx(-__import__("math").log(0.6), abs=1e-9)
        # Polymarket skipped -> PM fields None; note flags the FGS gap.
        assert striker.anytime_pm_odds is None
        assert "first-goalscorer" in note and "market-implied" in note

    def test_unplaced_player_flagged_not_attributed(self, tmp_path):
        gs, note = build_goalscorers(
            "Alpha", "Bravo", _gs_scorer_df(),
            squads_path=_write_squads(tmp_path), pm_lookup=False,
        )
        names = [l.player for l in gs["home"] + gs["away"]]
        assert "Bench Unknown" not in names
        assert "1 market player(s) not in squad lists" in note

    def test_missing_squads_degrades_gracefully(self, tmp_path):
        gs, note = build_goalscorers(
            "Alpha", "Bravo", _gs_scorer_df(),
            squads_path=str(tmp_path / "absent.json"), pm_lookup=False,
        )
        assert gs == {"home": [], "away": []}
        assert "squads.json missing" in note

    def test_no_scorer_market(self):
        gs, note = build_goalscorers("Alpha", "Bravo", None, pm_lookup=False)
        assert gs == {"home": [], "away": []}
        assert "no sportsbook scorer market" in note

    def test_polymarket_anytime_price_attached(self, tmp_path, monkeypatch):
        import wca.data.polymarket as pmmod

        def fake_resolve(home, away, player, events=None):
            if player == "Alpha Striker":
                return {"price": 0.40, "token_id": "t", "neg_risk": False,
                        "market_question": "Alpha Striker: 1+ goals", "outcome": "Yes"}
            return None

        monkeypatch.setattr(pmmod, "resolve_player_anytime_token", fake_resolve)
        gs, note = build_goalscorers(
            "Alpha", "Bravo", _gs_scorer_df(),
            squads_path=_write_squads(tmp_path), pm_events=[], pm_lookup=True,
        )
        striker = gs["home"][0]
        assert striker.anytime_pm_price == pytest.approx(0.40)
        assert striker.anytime_pm_odds == pytest.approx(2.5)
        # Players without a PM market are reported in the note.
        assert "no PM 1+ goals market" in note


# ---------------------------------------------------------------------------
# Builder.
# ---------------------------------------------------------------------------


class TestBuildNextMatch:
    def _models(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        return fit_models(_synthetic_results(rng), half_life_years=8.0)

    def test_picks_earliest_kickoff(self):
        cards = build_next_match(
            self._models(), _synthetic_odds(), _synthetic_fixtures_meta()
        )
        assert len(cards) == 1
        assert (cards[0].home, cards[0].away) == ("Alpha", "Bravo")
        assert cards[0].commence_time == "2026-06-11T18:00:00Z"

    def test_simultaneous_kickoffs_returns_all(self):
        cards = build_next_match(
            self._models(),
            _synthetic_odds_simultaneous(),
            _synthetic_fixtures_meta_simultaneous(),
        )
        assert len(cards) == 2
        names = {(c.home, c.away) for c in cards}
        assert ("Alpha", "Bravo") in names
        assert ("Charlie", "Delta") in names
        # The late fixture must not be included.
        assert not any(c.home == "Echo" for c in cards)

    def test_sections_populated_and_consistent(self):
        cards = build_next_match(
            self._models(),
            _synthetic_odds(),
            _synthetic_fixtures_meta(),
            scorer_df=_scorer_df(),
        )
        card = cards[0]
        # Winner: probs sum to 1, best prices line-shopped across books.
        probs = [card.winner[o][0] for o in ("home", "draw", "away")]
        assert sum(probs) == pytest.approx(1.0, abs=1e-9)
        p, book, odds, edge = card.winner["home"]
        assert odds == 2.10 and book == "book_a"  # max across books
        assert edge == pytest.approx(p * odds - 1.0, abs=1e-12)
        # Corners: a real probability at the default 8.5 line.
        assert 0.0 < card.corners_p_over < 1.0
        assert card.corners_mu > 0
        # Scorelines: reconciled to the blended 1X2.
        assert card.scores.one_x_two == pytest.approx(tuple(probs), abs=1e-9)
        assert len(card.scores.top_scorelines) == 6
        # Scorers: best price per player, favourite first.
        assert [s.player for s in card.scorers][:2] == ["Striker One", "Mid Two"]
        assert card.scorers[0].best_odds == 2.6 and card.scorers[0].best_book == "Book B"

    def test_empty_slate_returns_empty_list(self):
        empty = pd.DataFrame(
            columns=[
                "event_id", "home_team", "away_team", "commence_time",
                "market", "bookmaker_key", "outcome_name", "decimal_odds",
            ]
        )
        assert build_next_match(self._models(), empty, _synthetic_fixtures_meta()) == []

    def test_scorer_by_event_routes_per_fixture(self):
        scorer_by_event = {"evt_alpha": _scorer_df()}
        cards = build_next_match(
            self._models(),
            _synthetic_odds_simultaneous(),
            _synthetic_fixtures_meta_simultaneous(),
            scorer_by_event=scorer_by_event,
            pm_lookup=False,
        )
        alpha_card = next(c for c in cards if c.home == "Alpha")
        charlie_card = next(c for c in cards if c.home == "Charlie")
        # Alpha fixture has scorer data; Charlie fixture has none.
        assert len(alpha_card.scorers) > 0
        assert len(charlie_card.scorers) == 0


class TestTopScorers:
    def test_handles_missing_inputs(self):
        assert top_scorers_from_odds(None) == []
        assert top_scorers_from_odds(pd.DataFrame()) == []
        df = _scorer_df()
        assert top_scorers_from_odds(df[df["market"] == "nope"]) == []

    def test_top_n_cap(self):
        assert len(top_scorers_from_odds(_scorer_df(), top_n=2)) == 2

    def test_fallback_to_outcome_name_without_description(self):
        df = _scorer_df().drop(columns=["outcome_description"])
        df["outcome_name"] = ["P1", "P1", "P2", "P3"]
        out = top_scorers_from_odds(df)
        assert [s.player for s in out] == ["P1", "P2", "P3"]


# ---------------------------------------------------------------------------
# Formatter + bot handler.
# ---------------------------------------------------------------------------


class TestFormatAndBot:
    def test_format_none(self):
        assert "No upcoming fixture" in format_next_match(None)

    def test_format_empty_list(self):
        assert "No upcoming fixture" in format_next_match([])

    def test_format_full_card(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        models = fit_models(_synthetic_results(rng), half_life_years=8.0)
        cards = build_next_match(
            models, _synthetic_odds(), _synthetic_fixtures_meta(), scorer_df=_scorer_df()
        )
        text = format_next_match(cards)
        assert "Next match" in text and "Alpha vs Bravo" in text
        assert "*Winner*" in text
        assert "O/U 8.5" in text
        assert "Striker One" in text
        assert "*Scorelines*" in text and "BTTS" in text

    def test_format_simultaneous_includes_divider(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        models = fit_models(_synthetic_results(rng), half_life_years=8.0)
        cards = build_next_match(
            models,
            _synthetic_odds_simultaneous(),
            _synthetic_fixtures_meta_simultaneous(),
        )
        assert len(cards) == 2
        text = format_next_match(cards)
        assert "Alpha vs Bravo" in text
        assert "Charlie vs Delta" in text
        assert "─" in text  # divider between simultaneous fixtures

    def test_format_includes_goalscorer_block(self, tmp_path):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        models = fit_models(_synthetic_results(rng), half_life_years=8.0)
        cards = build_next_match(
            models, _synthetic_odds(), _synthetic_fixtures_meta(),
            scorer_df=_gs_scorer_df(), squads_path=_write_squads(tmp_path),
            pm_lookup=False,
        )
        text = format_next_match(cards)
        assert "*Top goalscorers*" in text
        assert "Alpha Striker" in text and "Bravo Striker" in text
        assert "g/g" in text                 # market-implied goals/game basis
        assert "Any  bk" in text and "1st  bk" in text   # both markets rendered
        assert "/ PM --" in text             # PM column present (skipped here)

    def test_handle_next_serves_cache(self, tmp_path):
        from wca.bot.app import handle_next
        from wca.cardcache import write_card

        path = str(tmp_path / "next_latest.md")
        # No cache yet.
        assert "No preview cached" in handle_next(next_path=path)
        write_card("⚽ *Next match* — A vs B", path=path, ts_utc="2026-06-13T10:00:00")
        out = handle_next(next_path=path, now_utc="2026-06-13T11:00:00")
        assert "A vs B" in out and "STALE" not in out
        # Stale after the max age.
        out = handle_next(next_path=path, now_utc="2026-06-14T11:00:00")
        assert "STALE" in out

    def test_dispatch_routes_next(self, tmp_path, monkeypatch):
        from wca.bot import app as bot_app

        path = str(tmp_path / "next_latest.md")
        monkeypatch.setattr(bot_app, "NEXT_PATH", path)
        reply = bot_app.dispatch("/next", db_path=str(tmp_path / "wca.db"))
        assert "Next match" in reply


# ---------------------------------------------------------------------------
# /goalscorers — multi-fixture anytime + first-goalscorer card.
# ---------------------------------------------------------------------------


class TestGoalscorerCard:
    def _models(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        return fit_models(_synthetic_results(rng), half_life_years=8.0)

    def test_build_orders_and_degrades_without_scorers(self):
        # No per-event scorer odds -> each fixture degrades to market-less, but
        # the multi-fixture iteration + kickoff ordering still hold.
        #
        # Canonical selection rule (wca.selection; user 2026-07-07): the
        # /goalscorers card now orders FURTHER-OUT fixtures first (thin early
        # markets are more likely mispriced), so the top_k cap keeps the
        # distant fixtures. This is DISTINCT from the /next SCHEDULE, which is
        # definitionally soonest-first (see test_picks_earliest_kickoff). Under
        # the OLD soonest-first order cards[0] was Alpha vs Bravo (2026-06-11);
        # now it is the later Charlie vs Delta (2026-06-12).
        cards = build_goalscorer_card(
            self._models(), _synthetic_odds(), _synthetic_fixtures_meta(),
            {}, top_k_fixtures=3, pm_lookup=False,
        )
        assert 1 <= len(cards) <= 3
        assert (cards[0].home, cards[0].away) == ("Charlie", "Delta")  # furthest-out KO
        assert cards[0].commence_time == "2026-06-12T18:00:00Z"
        assert not (cards[0].goalscorers.get("home") or cards[0].goalscorers.get("away"))

    def test_format_renders_headers_and_model_stake(self):
        priced = GoalscorerLine(
            player="Test Striker", team="Alpha",
            anytime_book_odds=3.0, anytime_book="Book A",
            first_book_odds=8.0, first_book="Book A", xg_per_game=0.5,
            model_p_anytime=0.45, model_fair_anytime=1.0 / 0.45,
            model_p_first=0.16, model_fair_first=1.0 / 0.16,
        )
        fx1 = GoalscorerFixture(
            home="Alpha", away="Bravo", commence_time="2026-06-11T18:00:00Z",
            goalscorers={"home": [priced], "away": []},
            goalscorer_note="basis note", bankroll=1500.0,
        )
        fx2 = GoalscorerFixture(
            home="Charlie", away="Delta", commence_time="2026-06-12T18:00:00Z",
            goalscorers={"home": [], "away": []},
        )
        text = format_goalscorer_card([fx1, fx2])
        assert "Goalscorers" in text and "next 2 games" in text
        assert "Alpha vs Bravo" in text and "Charlie vs Delta" in text
        assert "Any  bk 3.00" in text and "1st  bk 8.00" in text
        assert "£" in text                       # +EV model edge -> Kelly stake
        assert "no scorer market" in text        # fx2 degrades

    def test_format_empty(self):
        assert "No upcoming fixtures" in format_goalscorer_card([])

    def test_format_flat_fallback_when_no_squad(self):
        from wca.nextmatch import ScorerPrice

        fx = GoalscorerFixture(
            home="Alpha", away="Bravo", commence_time="2026-06-11T18:00:00Z",
            goalscorers={"home": [], "away": []},
            goalscorer_note="44 market player(s) not in squad lists",
            scorers=[ScorerPrice(player="Star Man", best_odds=2.5,
                                 best_book="Book A", implied=0.4)],
        )
        text = format_goalscorer_card([fx])
        assert "top anytime (both teams" in text
        assert "Star Man" in text and "2.50" in text

    def test_handle_goalscorers_serves_cache(self, tmp_path):
        from wca.bot.app import handle_goalscorers
        from wca.cardcache import write_card

        path = str(tmp_path / "goalscorers_latest.md")
        assert "No card cached" in handle_goalscorers(goalscorers_path=path)
        write_card("⚽ *Goalscorers* — next 5 games", path=path, ts_utc="2026-06-13T10:00:00")
        out = handle_goalscorers(goalscorers_path=path, now_utc="2026-06-13T11:00:00")
        assert "Goalscorers" in out and "STALE" not in out
        out = handle_goalscorers(goalscorers_path=path, now_utc="2026-06-14T11:00:00")
        assert "STALE" in out

    def test_dispatch_routes_goalscorers(self, tmp_path, monkeypatch):
        from wca.bot import app as bot_app

        path = str(tmp_path / "goalscorers_latest.md")
        monkeypatch.setattr(bot_app, "GOALSCORERS_PATH", path)
        reply = bot_app.dispatch("/goalscorers", db_path=str(tmp_path / "wca.db"))
        assert "Goalscorers" in reply
