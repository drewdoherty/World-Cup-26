from __future__ import annotations

import json

import pytest

from wca import accas
from wca.bot import app


def _fixture(
    name,
    *,
    model=None,
    prices=None,
    over=55.0,
    under=45.0,
    btts=48.0,
):
    model = model or {"home": 0.52, "draw": 0.28, "away": 0.20}
    prices = prices or {"home": 1.90, "draw": 3.60, "away": 4.50}
    return {
        "fixture": name,
        "scores": [
            {"score": "1-0", "prob": 16.0, "fair": 6.25},
            {"score": "1-1", "prob": 12.0, "fair": 8.33},
        ],
        "over_under": {"line": 2.5, "over": over, "under": under},
        "btts": btts,
        "model_1x2": model,
        "venues": [
            {
                "venue": "betfair_sb_uk",
                "selection_prices": prices,
                "implied": {},
                "edge_vs_model": {},
            }
        ],
    }


def _feed():
    return {
        "meta": {"generated": "2026-06-19 10:00:00 UTC"},
        "fixtures": [
            _fixture(
                "Scotland vs Morocco",
                model={"home": 0.24, "draw": 0.31, "away": 0.45},
                prices={"home": 4.00, "draw": 3.40, "away": 2.05},
                over=42.0,
                under=58.0,
                btts=44.0,
            ),
            _fixture(
                "England vs Ghana",
                model={"home": 0.61, "draw": 0.24, "away": 0.15},
                prices={"home": 1.62, "draw": 4.20, "away": 6.00},
            ),
            _fixture("United States vs Australia"),
            _fixture("Mexico vs South Korea"),
            _fixture("Brazil vs Haiti"),
        ],
    }


def test_promo_catalog_contains_user_requested_accounts_and_terms():
    rows = accas.build_promo_accas(_feed())
    by_site = {(r["site"], r["title"]): r for r in rows}

    virgin = by_site[("Virgin Bet", "50% winnings boost — Scotland vs Morocco bet builder")]
    assert virgin["accounts"] == ["A1", "A2"]
    assert virgin["min_legs"] == 4
    assert virgin["min_total_odds"] == pytest.approx(2.0)
    assert virgin["boost_pct"] == pytest.approx(50.0)
    assert "4+ legs" in virgin["notes"]
    assert "Bet Builders only" in virgin["notes"]

    paddy = by_site[("Paddy Power", "England/Ghana free acca")]
    assert paddy["accounts"] == ["A1", "A2"]
    assert paddy["min_legs"] == 3
    assert paddy["min_total_odds"] == pytest.approx(2.0)

    betfair = by_site[("Betfair Sportsbook", "Max £10 free bet acca")]
    assert betfair["max_free_bet"] == pytest.approx(10.0)
    assert betfair["min_leg_odds"] == pytest.approx(1.5)

    betfred = by_site[("Betfred", "ENG/SCOT World Cup bet builder")]
    assert betfred["min_legs"] == 3
    assert betfred["min_total_odds"] == pytest.approx(4.0)


def test_betfair_acca_uses_only_legs_at_1_5_or_better():
    rows = accas.build_promo_accas(_feed())
    betfair = next(r for r in rows if r["site"] == "Betfair Sportsbook")
    assert betfair["status"] == "ready"
    assert len(betfair["legs"]) >= 3
    assert all(float(leg["odds"]) >= 1.5 for leg in betfair["legs"])
    assert betfair["total_odds"] >= 1.5 ** 3


def test_virgin_builder_is_component_ranked_not_joint_priced():
    rows = accas.build_promo_accas(_feed())
    virgin = next(r for r in rows if r["site"] == "Virgin Bet")
    assert virgin["status"] == "ready"
    assert len(virgin["legs"]) == 4
    assert "joint bet-builder EV not priced yet" in virgin["reason"]
    assert [leg["market"] for leg in virgin["legs"]] == [
        "Match result",
        "Goals",
        "BTTS",
        "Score lean",
    ]


def test_bet_builder_below_min_total_is_incomplete():
    feed = {
        "meta": {"generated": "2026-06-19 10:00:00 UTC"},
        "fixtures": [
            _fixture(
                "Scotland vs Morocco",
                model={"home": 0.80, "draw": 0.12, "away": 0.08},
                prices={"home": 1.20, "draw": 8.0, "away": 13.0},
                over=80.0,
                under=20.0,
                btts=80.0,
            )
        ],
    }
    rows = accas.build_promo_accas(feed)
    betfred = next(r for r in rows if r["site"] == "Betfred")
    assert betfred["status"] == "incomplete"
    assert "below promo minimum" in betfred["reason"]
    assert betfred["total_odds"] < betfred["min_total_odds"]


def test_specific_fixture_missing_is_pending_not_fabricated():
    feed = {"meta": {"generated": "x"}, "fixtures": [_fixture("Brazil vs Haiti")]}
    rows = accas.build_promo_accas(feed)
    paddy = next(r for r in rows if r["site"] == "Paddy Power")
    virgin = next(r for r in rows if r["site"] == "Virgin Bet")
    assert paddy["status"] == "pending"
    assert paddy["legs"] == []
    assert "fixture not present" in paddy["reason"]
    assert virgin["status"] == "pending"
    assert virgin["legs"] == []


def test_format_includes_promos_accounts_and_warning():
    out = accas.format_promo_accas(accas.build_promo_accas(_feed()))
    assert "Promo accas / bet builders" in out
    assert "Paddy Power" in out
    assert "Betfair Sportsbook" in out
    assert "Betfred" in out
    assert "Virgin Bet" in out
    assert "A1/A2" in out
    assert "50% winnings boost" in out
    assert "joint-priced" in out


def test_handle_accas_loads_scores_json(tmp_path):
    path = tmp_path / "scores_data.json"
    path.write_text(json.dumps(_feed()), encoding="utf-8")
    out = app.handle_accas(scores_path=str(path))
    assert "Promo accas / bet builders" in out
    assert "Virgin Bet" in out
    assert "Scotland vs Morocco" in out


def test_dispatch_routes_accas(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "handle_accas", lambda: "ACCAS OK")
    assert app.dispatch("/accas", str(tmp_path / "x.db")) == "ACCAS OK"
