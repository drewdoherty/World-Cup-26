"""Tests for the derived-metrics builder and the market_intel feed assembler."""

from __future__ import annotations

from wca.intel import metrics, feed
from wca.intel.normalise import normalise_market


def _market(odds_by_venue):
    """{venue: {sel: odds}} -> latest_per_selection shape {sel: [rows]}."""
    out = {}
    for venue, sel_odds in odds_by_venue.items():
        for sel, o in sel_odds.items():
            out.setdefault(sel, []).append(
                {"venue": venue, "decimal_odds": o, "implied_raw": 1.0 / o,
                 "implied_devig": None, "ts_utc": "2026-06-28T10:00:00Z", "venue_kind": None})
    return out


# --------------------------------------------------------------------------- #
# selection_metrics
# --------------------------------------------------------------------------- #


def test_selection_metrics_best_worst_and_spread():
    quotes = [
        {"venue": "bet365", "decimal_odds": 2.00},
        {"venue": "Smarkets", "decimal_odds": 2.20},   # best for a backer
        {"venue": "Betway", "decimal_odds": 1.90},      # worst
    ]
    m = metrics.selection_metrics("Home", quotes)
    assert m["best_venue"] == "Smarkets" and m["best_odds"] == 2.20
    assert m["worst_venue"] == "Betway" and m["worst_odds"] == 1.90
    assert m["n_venues"] == 3
    assert abs(m["pct_improvement"] - (2.20 / 1.90 - 1)) < 1e-6   # stored rounded to 6dp
    # implied range = 1/1.90 - 1/2.20
    assert abs(m["implied_range"] - (1 / 1.90 - 1 / 2.20)) < 1e-9
    assert m["disagreement_pair"] == ("Betway", "Smarkets")   # hi implied, lo implied
    assert m["venues"]["Smarkets"]["colour"]                  # colour attached


def test_selection_metrics_empty():
    assert metrics.selection_metrics("Home", [])["n_venues"] == 0


# --------------------------------------------------------------------------- #
# consensus + EV / Kelly overlay
# --------------------------------------------------------------------------- #


def test_consensus_only_from_complete_books_and_sums_to_one():
    latest = _market({
        "bet365": {"Home": 1.8, "Draw": 3.6, "Away": 4.5},     # complete
        "Smarkets": {"Home": 1.85, "Draw": 3.7, "Away": 4.6},  # complete
        "Betway": {"Home": 1.9},                                # partial -> ignored
    })
    cons = metrics.consensus_probs(latest)
    assert abs(sum(cons.values()) - 1.0) < 1e-9
    assert cons["Home"] > cons["Draw"] > cons["Away"]


def test_consensus_none_when_no_complete_book():
    latest = _market({"bet365": {"Home": 1.8}, "Smarkets": {"Draw": 3.6}})
    cons = metrics.consensus_probs(latest)
    assert all(v is None for v in cons.values())


def test_build_market_metrics_overlays_model_ev_and_kelly():
    latest = _market({
        "bet365": {"Home": 2.0, "Draw": 3.5, "Away": 4.0},
        "Smarkets": {"Home": 2.1, "Draw": 3.6, "Away": 4.2},   # best Home @2.1, exchange (2% comm)
    })
    out = metrics.build_market_metrics(
        latest, model={"Home": 0.55, "Draw": 0.25, "Away": 0.20}, bankroll=2000.0)
    home = next(m for m in out if m["selection"] == "Home")
    assert home["best_venue"] == "Smarkets" and home["best_odds"] == 2.1
    assert home["model_prob"] == 0.55
    # +EV (0.55 * net_odds - 1 > 0) and a positive capped stake
    assert home["ev_vs_model"] > 0
    assert 0 < home["kelly_stake"] <= 0.05 * 2000.0
    assert home["consensus_prob"] is not None


# --------------------------------------------------------------------------- #
# feed assembly
# --------------------------------------------------------------------------- #


def _snaps():
    s = normalise_market(source="theoddsapi", venue="bet365", market_type="moneyline",
                         selection_odds={"Home": 1.8, "Draw": 3.6, "Away": 4.5},
                         ts_utc="2026-06-28T10:00:00Z", fixture_id="m1",
                         ko_utc="2026-06-28T19:00:00Z")
    s += normalise_market(source="theoddsapi", venue="Smarkets", market_type="moneyline",
                          selection_odds={"Home": 1.85, "Draw": 3.7, "Away": 4.6},
                          ts_utc="2026-06-28T10:00:00Z", fixture_id="m1",
                          ko_utc="2026-06-28T19:00:00Z")
    return s


def test_build_feed_structure_and_legend():
    f = feed.build_feed(_snaps(), now_utc="2026-06-28T10:30:00Z",
                        fixture_meta={"m1": {"home": "Mexico", "away": "Canada",
                                             "ko_utc": "2026-06-28T19:00:00Z"}})
    assert f["meta"]["n_fixtures"] == 1 and f["meta"]["n_markets"] == 1
    assert any(v["venue"] == "Smarkets" for v in f["venues"])     # legend present
    fx = f["fixtures"][0]
    assert fx["home"] == "Mexico" and fx["away"] == "Canada"
    mkt = fx["markets"][0]
    assert mkt["market_type"] == "moneyline" and mkt["n_venues"] == 2
    assert mkt["stale"] is False                                  # 30 min < 1h threshold
    assert len(mkt["selections"]) == 3
    assert f["meta"]["notes"]                                     # honest constraints surfaced


def test_build_feed_flags_stale_quotes():
    f = feed.build_feed(_snaps(), now_utc="2026-06-29T10:30:00Z")  # >24h later
    assert f["fixtures"][0]["markets"][0]["stale"] is True


def test_append_metrics_persists_to_market_metrics():
    import sqlite3
    from wca.intel import store
    con = sqlite3.connect(":memory:")
    store.ensure_schema(con)
    latest = _market({"bet365": {"Home": 2.0, "Draw": 3.5, "Away": 4.0},
                      "Smarkets": {"Home": 2.1, "Draw": 3.6, "Away": 4.2}})
    rows = metrics.build_market_metrics(latest, model={"Home": 0.55, "Draw": 0.25, "Away": 0.20},
                                        bankroll=2000.0)
    n = store.append_metrics(con, "2026-06-28T10:00:00Z", "m1", "moneyline", None, rows)
    assert n == 3
    got = con.execute("SELECT selection, best_odds, best_venue, model_prob FROM market_metrics "
                      "WHERE selection='Home'").fetchone()
    assert got == ("Home", 2.1, "Smarkets", 0.55)


def test_build_feed_keeps_newest_quote_per_venue():
    early = normalise_market(source="theoddsapi", venue="bet365", market_type="moneyline",
                             selection_odds={"Home": 2.0, "Away": 2.0},
                             ts_utc="2026-06-28T09:00:00Z", fixture_id="m1")
    late = normalise_market(source="theoddsapi", venue="bet365", market_type="moneyline",
                            selection_odds={"Home": 1.5, "Away": 3.0},
                            ts_utc="2026-06-28T10:00:00Z", fixture_id="m1")
    f = feed.build_feed(early + late, now_utc="2026-06-28T10:10:00Z")
    home = next(s for s in f["fixtures"][0]["markets"][0]["selections"] if s["selection"] == "Home")
    assert home["best_odds"] == 1.5   # newest, not the stale 2.0
