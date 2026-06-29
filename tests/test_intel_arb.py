"""Tests for the cross-venue arbitrage scanner (wca.intel.arb) and the /arb bot
handler. Pure/deterministic — ``now`` is injected, no network."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from wca.intel import arb


NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)


def _row(venue, odds, *, age_s=30, kind="sportsbook", liquidity=None):
    ts = (NOW - timedelta(seconds=age_s)).isoformat()
    return {"venue": venue, "decimal_odds": odds, "implied_raw": 1.0 / odds,
            "implied_devig": None, "ts_utc": ts, "venue_kind": kind,
            "liquidity": liquidity}


# --------------------------------------------------------------------------- #
# (a) cross-book back arb
# --------------------------------------------------------------------------- #


def test_cross_book_arb_detected_when_inverse_sum_below_one():
    # 1/2.6 + 1/4.0 + 1/4.2 = 0.8727 < 1 -> arb (~14.6%).
    latest = {
        "Home": [_row("bet365", 2.6)],
        "Draw": [_row("Betway", 4.0)],
        "Away": [_row("Paddy Power", 4.2)],
    }
    opps = arb.scan_market(latest, market_type="moneyline",
                           fixture="A vs B", now=NOW)
    cross = [o for o in opps if o.arb_type == "cross_book"]
    assert len(cross) == 1
    o = cross[0]
    assert o.guaranteed_return_pct > 0.14
    # Stakes split across the GBP total and sum back to it.
    assert abs(sum(l.stake for l in o.legs) - o.total_stake) < 0.05
    assert o.total_stake_currency == "GBP"
    # Sportsbook backs at fresh prices are executable (no exchange depth gate).
    assert o.actionable is True and o.stale is False


def test_cross_book_picks_best_price_across_venues():
    # Worst books alone wouldn't arb; best-per-selection does.
    latest = {
        "Home": [_row("bet365", 2.0), _row("Betway", 2.6)],   # best 2.6
        "Draw": [_row("Paddy Power", 3.5), _row("bet365", 4.0)],
        "Away": [_row("Betway", 3.8), _row("Paddy Power", 4.2)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    cross = [o for o in opps if o.arb_type == "cross_book"]
    assert cross, "best-price arb should be found"
    legs = {l.selection: l for l in cross[0].legs}
    assert legs["Home"].venue == "Betway" and legs["Home"].odds == 2.6


def test_cross_book_no_arb_when_inverse_sum_at_or_above_one():
    # 1/1.9 + 1/3.5 + 1/4.0 = 1.063 > 1 -> no arb.
    latest = {
        "Home": [_row("bet365", 1.9)],
        "Draw": [_row("Betway", 3.5)],
        "Away": [_row("Paddy Power", 4.0)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    assert [o for o in opps if o.arb_type == "cross_book"] == []


def test_cross_book_guaranteed_return_consistent_across_legs():
    latest = {
        "Over": [_row("bet365", 2.1)],
        "Under": [_row("Betway", 2.1)],
    }
    opps = arb.scan_market(latest, market_type="ou", fixture="A vs B", now=NOW)
    o = [x for x in opps if x.arb_type == "cross_book"][0]
    # Each leg's payout (stake * net_odds) is equal -> a true lock.
    payouts = [l.stake * l.net_odds for l in o.legs]
    assert max(payouts) - min(payouts) < 0.05
    # And that payout / total = 1 + return.
    assert abs(payouts[0] / o.total_stake - (1 + o.guaranteed_return_pct)) < 1e-3


# --------------------------------------------------------------------------- #
# (b) back-vs-lay (commission reduces the edge)
# --------------------------------------------------------------------------- #


def test_back_lay_opportunity_found_and_commission_reduces_it():
    # Back 3.0 at a sportsbook, lay 2.5 on Smarkets (2% comm) -> lock.
    latest = {"Home": [_row("bet365", 3.0)]}
    lay_latest = {"Home": [_row("Smarkets", 2.5, kind="exchange")]}
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B",
                           now=NOW, lay_latest=lay_latest)
    bl = [o for o in opps if o.arb_type == "back_lay"]
    assert len(bl) == 1
    o = bl[0]
    assert o.guaranteed_return_pct > 0
    assert {l.side for l in o.legs} == {"back", "lay"}
    lay_leg = [l for l in o.legs if l.side == "lay"][0]
    # Lay net (2% comm) is below the no-commission value 1 + 1/(2.5-1) = 1.6667.
    assert lay_leg.net_odds < 1.6667
    # Exchange leg depth unknown on relay odds -> indicative.
    assert o.actionable is False and o.liquidity_known is False


def test_back_lay_only_pairs_exchanges_for_the_lay_leg():
    # A sportsbook in the lay slot must be ignored (you cannot lay a book).
    latest = {"Home": [_row("bet365", 3.0)]}
    lay_latest = {"Home": [_row("Betway", 2.5, kind="sportsbook")]}
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B",
                           now=NOW, lay_latest=lay_latest)
    assert [o for o in opps if o.arb_type == "back_lay"] == []


# --------------------------------------------------------------------------- #
# (c) PM-vs-book
# --------------------------------------------------------------------------- #


def test_pm_book_arb_detected():
    # PM YES @ 0.30 (decimal ~3.33) for Away; books cover Home + Draw cheaply.
    pm_decimal = 1.0 / 0.30
    latest = {
        "Home": [_row("bet365", 2.7)],
        "Draw": [_row("Betway", 5.0)],
        "Away": [_row("polymarket", pm_decimal, kind="prediction_market")],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    pm = [o for o in opps if o.arb_type == "pm_book"]
    assert pm, "PM-vs-book arb should be detected"
    o = pm[0]
    buy = [l for l in o.legs if l.side == "buy_yes"][0]
    assert buy.venue == "polymarket" and buy.currency == "USD"
    # PM/FX risk -> always indicative.
    assert o.actionable is False


# --------------------------------------------------------------------------- #
# staleness gate
# --------------------------------------------------------------------------- #


def test_staleness_gate_marks_old_quote_indicative():
    latest = {
        "Home": [_row("bet365", 2.6, age_s=10_000)],   # well past default 300s
        "Draw": [_row("Betway", 4.0, age_s=10)],
        "Away": [_row("Paddy Power", 4.2, age_s=10)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B",
                           now=NOW, staleness_s=300.0)
    o = [x for x in opps if x.arb_type == "cross_book"][0]
    assert o.stale is True and o.actionable is False
    assert o.confidence == "indicative"
    stale_legs = [l for l in o.legs if l.stale]
    assert len(stale_legs) == 1 and stale_legs[0].venue == "bet365"


def test_fresh_quotes_are_actionable():
    latest = {
        "Home": [_row("bet365", 2.6, age_s=10)],
        "Draw": [_row("Betway", 4.0, age_s=10)],
        "Away": [_row("Paddy Power", 4.2, age_s=10)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    o = [x for x in opps if x.arb_type == "cross_book"][0]
    assert o.actionable is True and o.confidence == "executable"


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


def test_format_report_empty_is_honest():
    out = arb.format_arb_report([])
    assert "No arbs" in out and "indicative" in out.lower()


def test_format_report_lists_opportunity_and_stake_split():
    latest = {
        "Home": [_row("bet365", 2.6)],
        "Draw": [_row("Betway", 4.0)],
        "Away": [_row("Paddy Power", 4.2)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    out = arb.format_arb_report(opps, now=NOW)
    assert "A vs B" in out
    assert "return" in out and "%" in out
    assert "bet365" in out
    assert "Indicative only" in out  # the never-auto-bet footer


def test_format_report_flags_all_indicative_banner():
    latest = {
        "Home": [_row("bet365", 2.6, age_s=10_000)],
        "Draw": [_row("Betway", 4.0, age_s=10_000)],
        "Away": [_row("Paddy Power", 4.2, age_s=10_000)],
    }
    opps = arb.scan_market(latest, market_type="moneyline", fixture="A vs B", now=NOW)
    out = arb.format_arb_report(opps, now=NOW)
    assert "All indicative" in out


# --------------------------------------------------------------------------- #
# /arb bot handler
# --------------------------------------------------------------------------- #


def _seed_odds_db(path, *, home="Brazil", away="Spain", arb=True, fresh=True):
    """Create an odds_snapshots table with one fixture's moneyline across books."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE odds_snapshots (ts_utc TEXT, source TEXT, match_id TEXT, "
        "market TEXT, selection TEXT, decimal_odds REAL, raw TEXT)"
    )
    import json
    # Timestamp relative to REAL now (handle_arb uses datetime.now() for its
    # lookback window) so these rows are always inside the 48h scan window.
    real_now = datetime.now(timezone.utc)
    ts = (real_now - timedelta(seconds=30 if fresh else 10_000)).isoformat()
    # Arbing odds (sum 1/o < 1) when arb=True, else a normal overround book.
    if arb:
        prices = {"Brazil": ("bet365", 2.6), "Draw": ("betway", 4.0),
                  "Spain": ("paddypower", 4.2)}
    else:
        prices = {"Brazil": ("bet365", 1.9), "Draw": ("betway", 3.5),
                  "Spain": ("paddypower", 4.0)}
    mid = "match-1"
    for outcome, (book, odds) in prices.items():
        raw = json.dumps({
            "event_id": mid, "home_team": home, "away_team": away,
            "bookmaker_key": book, "outcome_name": outcome,
            "outcome_point": None, "commence_time": NOW.isoformat(),
        })
        con.execute(
            "INSERT INTO odds_snapshots VALUES (?,?,?,?,?,?,?)",
            (ts, "theoddsapi", mid, "h2h", outcome, odds, raw),
        )
    con.commit()
    con.close()


def test_handle_arb_empty_db_no_crash(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()  # exists but no odds_snapshots table
    out = app.handle_arb("/arb", db)
    assert isinstance(out, str)
    assert "Arbitrage scan" in out
    assert "No `odds_snapshots`" in out or "No odds" in out


def test_handle_arb_missing_db_file_is_friendly(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "nope.db")  # never created
    out = app.handle_arb("/arb", db)
    assert isinstance(out, str)
    # sqlite creates the file on connect, so this hits the no-table branch.
    assert "Arbitrage scan" in out


def test_handle_arb_detects_seeded_fixture(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "odds.db")
    _seed_odds_db(db, arb=True, fresh=True)
    out = app.handle_arb("/arb", db)
    assert "Brazil vs Spain" in out
    assert "cross-book" in out
    assert "return" in out


def test_handle_arb_team_filter(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "odds.db")
    _seed_odds_db(db, home="Brazil", away="Spain", arb=True, fresh=True)
    # A filter that matches -> report; one that doesn't -> "no arbs" honest msg.
    assert "Brazil vs Spain" in app.handle_arb("/arb brazil", db)
    miss = app.handle_arb("/arb germany", db)
    assert "No arbs" in miss


def test_handle_arb_no_arb_when_overround(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "odds.db")
    _seed_odds_db(db, arb=False, fresh=True)
    out = app.handle_arb("/arb", db)
    assert "No arbs" in out


# --------------------------------------------------------------------------- #
# dispatch routing
# --------------------------------------------------------------------------- #


def test_dispatch_routes_arb(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "odds.db")
    _seed_odds_db(db, arb=True, fresh=True)
    out = app.dispatch("/arb", db)
    assert "Arbitrage scan" in out
    assert "Brazil vs Spain" in out


def test_dispatch_arb_empty_db(tmp_path):
    from wca.bot import app
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    out = app.dispatch("/arb", db)
    assert isinstance(out, str) and "Arbitrage scan" in out
