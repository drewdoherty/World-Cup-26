"""Tests for the /next next-match preview card (wca.nextmatch + bot handler)."""

import numpy as np
import pandas as pd
import pytest

from wca.nextmatch import (
    ANYTIME_SCORER_MARKET,
    build_next_match,
    format_next_match,
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


# ---------------------------------------------------------------------------
# Builder.
# ---------------------------------------------------------------------------


class TestBuildNextMatch:
    def _models(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        return fit_models(_synthetic_results(rng), half_life_years=8.0)

    def test_picks_earliest_kickoff(self):
        card = build_next_match(
            self._models(), _synthetic_odds(), _synthetic_fixtures_meta()
        )
        assert card is not None
        assert (card.home, card.away) == ("Alpha", "Bravo")
        assert card.commence_time == "2026-06-11T18:00:00Z"

    def test_sections_populated_and_consistent(self):
        card = build_next_match(
            self._models(),
            _synthetic_odds(),
            _synthetic_fixtures_meta(),
            scorer_df=_scorer_df(),
        )
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

    def test_empty_slate_returns_none(self):
        empty = pd.DataFrame(
            columns=[
                "event_id", "home_team", "away_team", "commence_time",
                "market", "bookmaker_key", "outcome_name", "decimal_odds",
            ]
        )
        assert build_next_match(self._models(), empty, _synthetic_fixtures_meta()) is None


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

    def test_format_full_card(self):
        from wca.card import fit_models

        rng = np.random.default_rng(42)
        models = fit_models(_synthetic_results(rng), half_life_years=8.0)
        card = build_next_match(
            models, _synthetic_odds(), _synthetic_fixtures_meta(), scorer_df=_scorer_df()
        )
        text = format_next_match(card)
        assert "Next match" in text and "Alpha vs Bravo" in text
        assert "*Winner*" in text
        assert "O/U 8.5" in text
        assert "Striker One" in text
        assert "*Scorelines*" in text and "BTTS" in text

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
