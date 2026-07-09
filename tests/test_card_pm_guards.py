"""Guards against Polymarket multi-market contamination + single-source staking.

Covers the 2026-06-29 phantom-edge fix at the card layer (the parser-level fix
lives in tests/test_odds_source.py):

* the coherence guard in ``_index_odds`` drops an impossible "sub-fair" book
  (implied probs summing < 1.0) — the signature of prices merged across
  different Polymarket markets (full / halftime / second-half); and
* the single-source guard flags a pick priced by only one book as
  ``indicative`` and does not auto-stake it unless WCA_STAKE_SINGLE_SOURCE is set.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from wca.card import (
    _index_odds,
    build_card,
    default_pools,
    fit_models,
)


def _row(book, name, odd, *, eid="e1", home="Alpha", away="Bravo"):
    return {
        "event_id": eid, "home_team": home, "away_team": away,
        "commence_time": "2026-07-01T18:00:00Z", "market": "h2h",
        "bookmaker_key": book, "outcome_name": name, "decimal_odds": odd,
    }


# ---------------------------------------------------------------------------
# Coherence guard in _index_odds
# ---------------------------------------------------------------------------


def test_coherence_guard_drops_subfair_book():
    # Frankenstein book: longest leg per outcome -> 1/1.83 + 1/5.40 + 1/11.76
    # = 0.816 < 1.0, impossible for a real single market. Must be dropped.
    df = pd.DataFrame([
        _row("polymarket", "Alpha", 1.83),
        _row("polymarket", "Draw", 5.40),
        _row("polymarket", "Bravo", 11.76),
    ])
    fx = list(_index_odds(df).values())[0]
    assert "polymarket" not in fx["books"]


def test_coherence_guard_keeps_coherent_book():
    # Real full-match book: 1/1.31 + 1/5.40 + 1/11.76 = 1.03 >= 1.0 -> kept.
    df = pd.DataFrame([
        _row("polymarket", "Alpha", 1.31),
        _row("polymarket", "Draw", 5.40),
        _row("polymarket", "Bravo", 11.76),
    ])
    fx = list(_index_odds(df).values())[0]
    assert "polymarket" in fx["books"]
    assert fx["books"]["polymarket"]["home"] == 1.31


# ---------------------------------------------------------------------------
# Single-source (indicative) guard in build_card
# ---------------------------------------------------------------------------


def _synthetic_results(rng, n=240):
    teams = ["Alpha", "Bravo", "Cienega", "Delta", "Echo", "Foxtrot"]
    base = pd.Timestamp("2024-01-01")
    rows = []
    for k in range(n):
        i, j = rng.choice(len(teams), size=2, replace=False)
        rows.append({
            "date": base + pd.Timedelta(days=int(k)),
            "home_team": teams[i], "away_team": teams[j],
            "home_score": int(rng.poisson(1.5)),
            "away_score": int(rng.poisson(1.1)),
            "tournament": "Friendly", "neutral": False,
        })
    return pd.DataFrame(rows)


def _fixtures_meta():
    return pd.DataFrame([{
        "home_team": "Alpha", "away_team": "Bravo", "neutral": False,
        "country": "", "home_score": np.nan, "away_score": np.nan,
    }])


def _pm_only_odds():
    # Coherent single PM book (1/1.80 + 1/3.50 + 1/4.50 = 1.06) but ONE source.
    return pd.DataFrame([
        _row("polymarket", "Alpha", 1.80),
        _row("polymarket", "Draw", 3.50),
        _row("polymarket", "Bravo", 4.50),
    ])


def _two_book_odds():
    return pd.DataFrame([
        _row("polymarket", "Alpha", 1.80),
        _row("polymarket", "Draw", 3.50),
        _row("polymarket", "Bravo", 4.50),
        _row("smarkets", "Alpha", 1.78),
        _row("smarkets", "Draw", 3.55),
        _row("smarkets", "Bravo", 4.60),
    ])


def _models():
    return fit_models(_synthetic_results(np.random.default_rng(7)), half_life_years=8.0)


def test_single_source_picks_are_indicative_and_unstaked(monkeypatch):
    monkeypatch.delenv("WCA_STAKE_SINGLE_SOURCE", raising=False)
    recs = build_card(_models(), _pm_only_odds(), default_pools(), _fixtures_meta(),
                      min_edge=-1.0, now="2026-06-20T12:00:00Z")
    assert recs
    assert all(r.indicative for r in recs)
    assert all(all(v == 0.0 for v in r.stakes.values()) for r in recs)


def test_single_source_override_stakes(monkeypatch):
    monkeypatch.setenv("WCA_STAKE_SINGLE_SOURCE", "1")
    # Pin the LIVE shrink off (default on): this fixture is a single PM-only
    # book, so the de-vigged "market" reference IS that same PM price and the
    # shrink collapses the model-vs-market edge to ~0 (correctly — there is no
    # second price to disagree with), leaving nothing to stake. That is
    # orthogonal to the single-source-override MECHANISM under test here (that a
    # non-indicative PM outcome gets staked once the override is set). The shrink
    # is exercised in tests/test_shrink_live.py.
    monkeypatch.setenv("WCA_SHRINK_LIVE", "0")
    recs = build_card(_models(), _pm_only_odds(), default_pools(), _fixtures_meta(),
                      min_edge=-1.0, now="2026-06-20T12:00:00Z")
    assert recs
    assert all(not r.indicative for r in recs)
    # At least one +EV outcome is actually sized once the override is set.
    assert any(v > 0.0 for r in recs for v in r.stakes.values())


def test_two_books_are_confirmed_not_indicative(monkeypatch):
    monkeypatch.delenv("WCA_STAKE_SINGLE_SOURCE", raising=False)
    recs = build_card(_models(), _two_book_odds(), default_pools(), _fixtures_meta(),
                      min_edge=-1.0, now="2026-06-20T12:00:00Z")
    assert recs
    assert all(not r.indicative for r in recs)
