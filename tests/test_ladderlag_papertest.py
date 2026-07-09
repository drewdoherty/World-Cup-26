"""Tests for the ladder-lag matchday paper-trading harness (network-free).

PAPER ONLY. This test module never touches the network (all fetch functions
are injected fakes) and never imports pm/trader.py -- it exercises jump
detection, book-walking fill math, mark idempotency, report aggregation, and
token discovery against a throwaway sqlite fixture.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import wca_ladderlag_papertest as m  # noqa: E402


# ---------------------------------------------------------------------------
# No forbidden imports (structural paper-only guarantee)
# ---------------------------------------------------------------------------


def test_module_never_imports_trader_or_pm_execution():
    src = Path(m.__file__).read_text()
    # Only mentions of pm/trader.py should be in comments/docstrings/help text,
    # never an actual `import` statement.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import wca.pm") or stripped.startswith("from wca.pm"):
            raise AssertionError(f"forbidden execution import found: {line!r}")
        if "trader" in stripped and (stripped.startswith("import") or stripped.startswith("from")):
            raise AssertionError(f"forbidden trader import found: {line!r}")


# ---------------------------------------------------------------------------
# Jump detection
# ---------------------------------------------------------------------------


def test_detect_jump_finds_move_within_window():
    hist = [
        m.PricePoint(ts=0, price=0.40),
        m.PricePoint(ts=60, price=0.41),
        m.PricePoint(ts=120, price=0.52),  # +0.12 vs ts=0, within 5min window
    ]
    out = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert out is not None
    pre, post, thin = out
    assert pre.price == 0.40
    assert post.price == 0.52
    assert m.jump_direction(pre, post) == "up"
    assert thin is False  # notional unknown on both points -- not flagged thin


def test_detect_jump_ignores_move_outside_window():
    hist = [
        m.PricePoint(ts=0, price=0.40),
        m.PricePoint(ts=400, price=0.52),  # 400s > 300s window
    ]
    assert m.detect_jump(hist, threshold=0.10, window_secs=300) is None


def test_detect_jump_ignores_small_move():
    hist = [
        m.PricePoint(ts=0, price=0.40),
        m.PricePoint(ts=60, price=0.45),  # only +0.05
    ]
    assert m.detect_jump(hist, threshold=0.10, window_secs=300) is None


def test_detect_jump_direction_down():
    hist = [m.PricePoint(ts=0, price=0.60), m.PricePoint(ts=30, price=0.45)]
    out = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert out is not None
    pre, post, thin = out
    assert m.jump_direction(pre, post) == "down"


def test_detect_jump_empty_or_single_point_history():
    assert m.detect_jump([]) is None
    assert m.detect_jump([m.PricePoint(ts=0, price=0.5)]) is None


def test_detect_jump_returns_earliest_qualifying_pair():
    # First qualifying jump is at j=1 (ts=60) vs i=0 (ts=0): 0.55-0.40=0.15 >= 0.10
    hist = [
        m.PricePoint(ts=0, price=0.40),
        m.PricePoint(ts=60, price=0.55),
        m.PricePoint(ts=90, price=0.70),
    ]
    pre, post, thin = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert (pre.ts, post.ts) == (0, 60)


# ---------------------------------------------------------------------------
# Thin-print classification
# ---------------------------------------------------------------------------


def test_is_thin_print_flags_small_notional():
    pt = m.PricePoint(ts=0, price=0.40, notional=12.0)
    assert m.is_thin_print(pt, floor_usd=50.0) is True


def test_is_thin_print_false_for_large_notional():
    pt = m.PricePoint(ts=0, price=0.40, notional=500.0)
    assert m.is_thin_print(pt, floor_usd=50.0) is False


def test_is_thin_print_false_when_notional_unknown():
    pt = m.PricePoint(ts=0, price=0.40, notional=None)
    assert m.is_thin_print(pt, floor_usd=50.0) is False


def test_detect_jump_flags_thin_print_on_pre_point():
    hist = [
        m.PricePoint(ts=0, price=0.40, notional=10.0),  # thin print
        m.PricePoint(ts=60, price=0.55, notional=5000.0),
    ]
    pre, post, thin = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert thin is True


def test_detect_jump_flags_thin_print_on_post_point():
    hist = [
        m.PricePoint(ts=0, price=0.40, notional=5000.0),
        m.PricePoint(ts=60, price=0.55, notional=8.0),  # thin print
    ]
    pre, post, thin = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert thin is True


def test_detect_jump_not_thin_when_both_prints_large():
    hist = [
        m.PricePoint(ts=0, price=0.40, notional=5000.0),
        m.PricePoint(ts=60, price=0.55, notional=5000.0),
    ]
    pre, post, thin = m.detect_jump(hist, threshold=0.10, window_secs=300)
    assert thin is False


# ---------------------------------------------------------------------------
# Book-walking fill math
# ---------------------------------------------------------------------------


def test_walk_book_fill_single_level_covers_notional():
    asks = [m.BookLevel(price=0.50, size=1000.0)]
    out = m.walk_book_fill(asks, notional_usd=50.0)
    assert out["shares"] == 100.0
    assert out["avg_price"] == 0.50
    assert out["notional_filled"] == 50.0
    assert out["exhausted"] is False


def test_walk_book_fill_walks_multiple_levels():
    asks = [
        m.BookLevel(price=0.50, size=20.0),   # $10 notional at this level
        m.BookLevel(price=0.55, size=1000.0),  # rest here
    ]
    out = m.walk_book_fill(asks, notional_usd=50.0)
    # level 1: 20 shares @ 0.50 = $10; remaining $40 -> 40/0.55 shares @ 0.55
    expected_shares = 20.0 + 40.0 / 0.55
    assert abs(out["shares"] - expected_shares) < 1e-9
    assert abs(out["notional_filled"] - 50.0) < 1e-9
    # avg price should be between the two level prices
    assert 0.50 < out["avg_price"] < 0.55
    assert out["exhausted"] is False


def test_walk_book_fill_exhausts_book_before_notional():
    asks = [m.BookLevel(price=0.50, size=10.0)]  # only $5 notional available
    out = m.walk_book_fill(asks, notional_usd=50.0)
    assert out["notional_filled"] == 5.0
    assert out["exhausted"] is True


def test_walk_book_fill_empty_book():
    out = m.walk_book_fill([], notional_usd=50.0)
    assert out["shares"] == 0.0
    assert out["avg_price"] is None
    assert out["exhausted"] is False


def test_walk_book_fill_skips_invalid_levels():
    asks = [m.BookLevel(price=0.0, size=100.0), m.BookLevel(price=0.60, size=100.0)]
    out = m.walk_book_fill(asks, notional_usd=30.0)
    assert out["avg_price"] == 0.60


def test_book_from_payload_sorts_bids_desc_asks_asc():
    book = {
        "bids": [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "5"}],
        "asks": [{"price": "0.55", "size": "8"}, {"price": "0.50", "size": "3"}],
    }
    bids, asks = m.book_from_payload(book)
    assert [b.price for b in bids] == [0.45, 0.40]
    assert [a.price for a in asks] == [0.50, 0.55]


def test_book_from_payload_handles_none_and_malformed():
    assert m.book_from_payload(None) == ([], [])
    bids, asks = m.book_from_payload({"bids": [{"price": "bad", "size": "1"}], "asks": []})
    assert bids == []
    assert asks == []


def test_best_bid_ask_from_levels():
    bids = [m.BookLevel(price=0.45, size=10.0)]
    asks = [m.BookLevel(price=0.50, size=5.0)]
    out = m.best_bid_ask(bids, asks)
    assert out == {"best_bid": 0.45, "best_ask": 0.50, "bid_size": 10.0, "ask_size": 5.0}


def test_best_bid_ask_empty_sides():
    out = m.best_bid_ask([], [])
    assert out == {"best_bid": None, "best_ask": None, "bid_size": None, "ask_size": None}


# ---------------------------------------------------------------------------
# P&L math
# ---------------------------------------------------------------------------


def test_worst_case_fee_symmetric_and_bounded():
    assert m.worst_case_fee(0.5) == 0.03 * 0.5 * 0.5
    assert m.worst_case_fee(0.0) == 0.0
    assert m.worst_case_fee(1.0) == 0.0
    # clamps out-of-range inputs
    assert m.worst_case_fee(-1.0) == 0.0
    assert m.worst_case_fee(2.0) == 0.0


def test_paper_pnl_profit_case():
    out = m.paper_pnl(fill_price=0.40, mark_price=0.50, shares=100.0)
    gross = (0.50 - 0.40) * 100.0
    assert out["gross_pnl"] == gross
    assert out["net_pnl"] < out["gross_pnl"]  # fee always haircuts
    assert out["fee"] > 0


def test_paper_pnl_loss_case():
    out = m.paper_pnl(fill_price=0.50, mark_price=0.40, shares=100.0)
    assert abs(out["gross_pnl"] - (-10.0)) < 1e-9
    assert out["net_pnl"] < out["gross_pnl"]  # fee makes losses worse


# ---------------------------------------------------------------------------
# Event persistence (jsonl) + mark idempotency
# ---------------------------------------------------------------------------


def _make_event(event_id="ev1", detected_ts=1000.0, fill_price=0.40, shares=100.0,
                thin_print=False, fetch_latency_ms=250.0):
    return m.RungEvent(
        event_id=event_id,
        trigger_token_id="trig-tok",
        trigger_condition_id="trig-cid",
        trigger_team="France",
        jump_pre_price=0.40,
        jump_post_price=0.55,
        jump_direction="up",
        jump_pre_ts=990.0,
        jump_post_ts=1000.0,
        detected_ts=detected_ts,
        fetch_latency_ms=fetch_latency_ms,
        thin_print=thin_print,
        rung_token_id="rung-tok",
        rung_category="advancement_qf",
        rung_condition_id="rung-cid",
        rung_outcome="Yes",
        pre_jump_ref_price=0.20,
        book_best_bid=0.19,
        book_best_ask=fill_price,
        book_bid_size=500.0,
        book_ask_size=500.0,
        fill_price=fill_price,
        fill_shares=shares,
        fill_notional=fill_price * shares,
        fill_exhausted=False,
    )


def test_append_and_load_roundtrip(tmp_path):
    path = tmp_path / "events.jsonl"
    ev = _make_event()
    m.append_event(ev, path)
    loaded = m.load_events(path)
    assert len(loaded) == 1
    assert loaded[0].event_id == "ev1"
    assert loaded[0].fill_price == 0.40


def test_load_events_skips_unparseable_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    ev = _make_event()
    path.write_text(json.dumps(ev.to_dict()) + "\n" + "not valid json\n")
    loaded = m.load_events(path)
    assert len(loaded) == 1


def test_load_events_missing_file_returns_empty(tmp_path):
    assert m.load_events(tmp_path / "nope.jsonl") == []


def test_mark_events_sets_10m_30m_and_2h_at_correct_ages():
    offset_10m, offset_30m, offset_2h = m.MARK_OFFSETS_SECS
    ev = _make_event(detected_ts=0.0)
    fetch_calls = []

    def fake_fetch(token_id):
        fetch_calls.append(token_id)
        return 0.60

    # before 10m: no marks
    out = m.mark_events([ev], fetch_mid=fake_fetch, now_ts=10.0)
    assert out[0].mark_10m is None
    assert out[0].mark_30m is None
    assert out[0].mark_2h is None
    assert fetch_calls == []

    # at/after 10m, before 30m: only mark_10m
    ev2 = _make_event(detected_ts=0.0)
    out = m.mark_events([ev2], fetch_mid=fake_fetch, now_ts=offset_10m + 1)
    assert out[0].mark_10m == 0.60
    assert out[0].mark_30m is None
    assert out[0].mark_2h is None

    # at/after 30m, before 2h: mark_10m + mark_30m
    ev3 = _make_event(detected_ts=0.0)
    out = m.mark_events([ev3], fetch_mid=fake_fetch, now_ts=offset_30m + 1)
    assert out[0].mark_10m == 0.60
    assert out[0].mark_30m == 0.60
    assert out[0].mark_2h is None

    # at/after 2h: all three marks
    ev4 = _make_event(detected_ts=0.0)
    out = m.mark_events([ev4], fetch_mid=fake_fetch, now_ts=offset_2h + 1)
    assert out[0].mark_10m == 0.60
    assert out[0].mark_30m == 0.60
    assert out[0].mark_2h == 0.60


def test_mark_events_is_idempotent_never_refetches_existing_mark():
    offset_2h = m.MARK_OFFSETS_SECS[2]
    ev = _make_event(detected_ts=0.0)
    ev.mark_10m = 0.41
    ev.mark_30m = 0.42  # already marked
    ev.mark_30m_ts = 1800.0
    calls = []

    def fake_fetch(token_id):
        calls.append(token_id)
        return 0.99  # would clobber if called

    out = m.mark_events([ev], fetch_mid=fake_fetch, now_ts=offset_2h + 1)
    assert out[0].mark_10m == 0.41  # untouched
    assert out[0].mark_30m == 0.42  # untouched
    assert out[0].mark_2h == 0.99   # newly set
    assert calls == ["rung-tok"]    # fetched only once, for the 2h mark


def test_mark_events_handles_fetch_returning_none():
    offset_10m = m.MARK_OFFSETS_SECS[0]
    ev = _make_event(detected_ts=0.0)
    out = m.mark_events([ev], fetch_mid=lambda tid: None, now_ts=offset_10m + 1)
    assert out[0].mark_10m is None  # left unset, will retry next run


def test_rewrite_events_is_atomic_and_readable(tmp_path):
    path = tmp_path / "events.jsonl"
    ev1, ev2 = _make_event("a"), _make_event("b")
    m.rewrite_events([ev1, ev2], path)
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    loaded = m.load_events(path)
    assert {e.event_id for e in loaded} == {"a", "b"}


def test_mark_then_reload_roundtrips_marks(tmp_path):
    """Full resumability check: mark, persist, reload, marks survive."""
    path = tmp_path / "events.jsonl"
    ev = _make_event(detected_ts=0.0)
    m.append_event(ev, path)

    offset_2h = m.MARK_OFFSETS_SECS[2]
    events = m.load_events(path)
    events = m.mark_events(events, fetch_mid=lambda tid: 0.61, now_ts=offset_2h + 5)
    m.rewrite_events(events, path)

    reloaded = m.load_events(path)
    assert reloaded[0].mark_10m == 0.61
    assert reloaded[0].mark_30m == 0.61
    assert reloaded[0].mark_2h == 0.61

    # calling mark again with a "poisoned" fetch must not change anything
    reloaded2 = m.mark_events(reloaded, fetch_mid=lambda tid: 0.0, now_ts=offset_2h + 999)
    assert reloaded2[0].mark_10m == 0.61
    assert reloaded2[0].mark_30m == 0.61
    assert reloaded2[0].mark_2h == 0.61


# ---------------------------------------------------------------------------
# Report aggregation (fixture jsonl, n>=20 and n<20 paths)
# ---------------------------------------------------------------------------


def _fixture_events(n, *, with_marks=True, winners=None, thin_print_idxs=None,
                    latency_ms=250.0):
    """Build n synthetic events; if winners is given, index i is a "hit"
    (positive 2h pnl) iff i in winners, else all events are profitable."""
    thin_print_idxs = thin_print_idxs or set()
    out = []
    for i in range(n):
        is_hit = (i in winners) if winners is not None else True
        mark_2h = 0.55 if is_hit else 0.30
        ev = _make_event(event_id=f"ev{i}", detected_ts=0.0, fill_price=0.40, shares=100.0,
                         thin_print=(i in thin_print_idxs), fetch_latency_ms=latency_ms)
        if with_marks:
            ev.mark_10m = 0.42
            ev.mark_30m = 0.45
            ev.mark_2h = mark_2h
        out.append(ev)
    return out


def test_report_insufficient_events_message(tmp_path):
    events = _fixture_events(5)
    rows = m.build_report_rows(events)
    agg = m.aggregate_report(rows)
    assert agg.sufficient is False
    out = m.format_report(rows, agg)
    assert "insufficient events (<20)" in out
    assert "n=5" in out


def test_report_sufficient_events_prints_aggregate():
    events = _fixture_events(25)
    rows = m.build_report_rows(events)
    agg = m.aggregate_report(rows)
    assert agg.sufficient is True
    assert agg.n_events == 25
    assert agg.n_with_2h_pnl == 25
    assert agg.hit_rate_2h == 1.0  # all fixture events profit (0.40 -> 0.55)
    out = m.format_report(rows, agg)
    assert "insufficient" not in out
    assert "hit_rate_2h=1.000" in out


def test_aggregate_report_mixed_hit_rate():
    events = _fixture_events(20, winners=set(range(12)))  # 12/20 hits
    rows = m.build_report_rows(events)
    agg = m.aggregate_report(rows)
    assert agg.hit_rate_2h == 0.60


def test_aggregate_report_excludes_thin_print_by_default():
    # 20 real events all profitable, plus 10 thin-print events that would
    # look like losers if counted -- they must not touch the aggregate.
    real = _fixture_events(20, winners=None, thin_print_idxs=set())
    thin = _fixture_events(10, with_marks=True, winners=set(), thin_print_idxs=set(range(10)))
    for i, ev in enumerate(thin):
        ev.event_id = f"thin{i}"
    rows = m.build_report_rows(real + thin)
    agg = m.aggregate_report(rows, exclude_thin_print=True)
    assert agg.n_events == 30          # raw count includes everything
    assert agg.n_thin_print == 10
    assert agg.n_with_2h_pnl == 20     # thin-print rows excluded from stats
    assert agg.hit_rate_2h == 1.0      # unpolluted by the thin-print losers


def test_aggregate_report_can_include_thin_print_when_asked():
    real = _fixture_events(20, winners=None, thin_print_idxs=set())
    thin = _fixture_events(10, with_marks=True, winners=set(), thin_print_idxs=set(range(10)))
    for i, ev in enumerate(thin):
        ev.event_id = f"thin{i}"
    rows = m.build_report_rows(real + thin)
    agg = m.aggregate_report(rows, exclude_thin_print=False)
    assert agg.n_with_2h_pnl == 30
    assert agg.hit_rate_2h == 20 / 30


def test_aggregate_report_stratifies_by_latency_bucket():
    fast = _fixture_events(10, winners=None, latency_ms=100.0)      # bucket [0-500)
    slow = _fixture_events(10, winners=set(), latency_ms=5000.0)    # bucket [2000-10000)
    for i, ev in enumerate(slow):
        ev.event_id = f"slow{i}"
    rows = m.build_report_rows(fast + slow)
    agg = m.aggregate_report(rows)
    by_label = {s.label: s for s in agg.by_latency}
    fast_stratum = next(s for s in agg.by_latency if s.n == 10 and s.hit_rate_2h == 1.0)
    slow_stratum = next(s for s in agg.by_latency if s.n == 10 and s.hit_rate_2h == 0.0)
    assert fast_stratum is not slow_stratum
    total_n = sum(s.n for s in agg.by_latency)
    assert total_n == 20


def test_latency_bucket_label_covers_all_buckets():
    labels = {m._latency_bucket_label(v) for v in (0.0, 499.0, 1999.0, 9999.0, 999999.0)}
    assert len(labels) == len(m.LATENCY_BUCKETS_MS)


def test_aggregate_report_handles_no_marks_yet():
    events = _fixture_events(20, with_marks=False)
    rows = m.build_report_rows(events)
    agg = m.aggregate_report(rows)
    assert agg.n_events == 20
    assert agg.n_with_2h_pnl == 0
    assert agg.hit_rate_2h is None
    assert agg.mean_pnl_2h is None
    out = m.format_report(rows, agg)
    assert "--" in out  # unmarked rows render as placeholders, not crash


def test_build_report_rows_pnl_matches_paper_pnl():
    ev = _make_event(fill_price=0.40, shares=100.0)
    ev.mark_2h = 0.50
    rows = m.build_report_rows([ev])
    expected = m.paper_pnl(0.40, 0.50, 100.0)["net_pnl"]
    assert rows[0].pnl_2h == expected


def test_report_cli_reads_fixture_jsonl_file(tmp_path, capsys):
    path = tmp_path / "fixture.jsonl"
    events = _fixture_events(21)
    m.rewrite_events(events, path)
    args = m.build_parser().parse_args(["report", "--events", str(path)])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "n=21" in captured.out
    assert "hit_rate_2h=" in captured.out


# ---------------------------------------------------------------------------
# Token discovery (read-only sqlite fixture, no real pm_orderflow.db needed)
# ---------------------------------------------------------------------------


def _build_fixture_db(path: Path):
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE pm_markets (condition_id TEXT, category TEXT, team TEXT, "
        "outcomes TEXT, token_ids TEXT, market_slug TEXT, game_start_time TEXT)"
    )
    rows = [
        # France 1X2 match today
        ("cid-match-1", "match_1x2", "France",
         json.dumps(["Yes", "No"]), json.dumps(["tok-fra-yes", "tok-fra-no"]),
         "fifwc-france-morocco-2026-07-08", "2026-07-08T18:00:00Z"),
        # France advancement rungs (same team)
        ("cid-qf-fra", "advancement_qf", "France",
         json.dumps(["Yes", "No"]), json.dumps(["tok-fra-qf-yes", "tok-fra-qf-no"]),
         "world-cup-nation-to-reach-quarterfinals-france", None),
        ("cid-sf-fra", "advancement_sf", "France",
         json.dumps(["Yes", "No"]), json.dumps(["tok-fra-sf-yes", "tok-fra-sf-no"]),
         "world-cup-nation-to-reach-semifinals-france", None),
        # Different team's match, should not show up in France's ladder
        ("cid-match-2", "match_1x2", "Morocco",
         json.dumps(["Yes", "No"]), json.dumps(["tok-mor-yes", "tok-mor-no"]),
         "fifwc-france-morocco-2026-07-08", "2026-07-08T18:00:00Z"),
        ("cid-winner-mor", "winner", "Morocco",
         json.dumps(["Yes", "No"]), json.dumps(["tok-mor-w-yes", "tok-mor-w-no"]),
         "world-cup-winner", None),
        # A match on a different date -- must be excluded
        ("cid-match-old", "match_1x2", "Brazil",
         json.dumps(["Yes", "No"]), json.dumps(["tok-bra-yes", "tok-bra-no"]),
         "fifwc-brazil-x-2026-07-01", "2026-07-01T18:00:00Z"),
    ]
    con.executemany(
        "INSERT INTO pm_markets VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    con.commit()
    con.close()


def test_discover_tokens_for_date_finds_1x2_and_ladder(tmp_path):
    db = tmp_path / "fixture_orderflow.db"
    _build_fixture_db(db)
    match_refs, ladder_by_team = m.discover_tokens_for_date(db, "2026-07-08")

    match_token_ids = {r.token_id for r in match_refs}
    assert "tok-fra-yes" in match_token_ids
    assert "tok-mor-yes" in match_token_ids
    assert "tok-bra-yes" not in match_token_ids  # wrong date excluded

    assert "France" in ladder_by_team
    fra_ladder_ids = {r.token_id for r in ladder_by_team["France"]}
    assert fra_ladder_ids == {"tok-fra-qf-yes", "tok-fra-qf-no",
                              "tok-fra-sf-yes", "tok-fra-sf-no"}

    assert "Morocco" in ladder_by_team
    mor_ladder_ids = {r.token_id for r in ladder_by_team["Morocco"]}
    assert mor_ladder_ids == {"tok-mor-w-yes", "tok-mor-w-no"}


def test_discover_tokens_missing_db_returns_empty(tmp_path):
    match_refs, ladder_by_team = m.discover_tokens_for_date(tmp_path / "missing.db", "2026-07-08")
    assert match_refs == []
    assert ladder_by_team == {}


def test_ladder_tokens_for_team_none_team_returns_empty():
    assert m.ladder_tokens_for_team({"France": ["x"]}, None) == []


def test_json_list_handles_malformed_and_non_list():
    assert m._json_list(None) == []
    assert m._json_list("not json") == []
    assert m._json_list(json.dumps({"a": 1})) == []
    assert m._json_list(json.dumps(["a", "b"])) == ["a", "b"]


# ---------------------------------------------------------------------------
# process_jump (full trigger fan-out, all I/O injected -- no network)
# ---------------------------------------------------------------------------


def test_process_jump_creates_one_event_per_ladder_token():
    trigger = m.TokenRef(token_id="trig", condition_id="trig-cid",
                         category="match_1x2", team="France", outcome="Yes",
                         market_slug="s", game_start_time="2026-07-08T18:00:00Z")
    ladder = [
        m.TokenRef(token_id="rung1", condition_id="c1", category="advancement_qf",
                  team="France", outcome="Yes", market_slug=None, game_start_time=None),
        m.TokenRef(token_id="rung2", condition_id="c2", category="advancement_sf",
                  team="France", outcome="Yes", market_slug=None, game_start_time=None),
    ]
    pre = m.PricePoint(ts=0.0, price=0.40)
    post = m.PricePoint(ts=30.0, price=0.55)

    books = {
        "rung1": {"bids": [{"price": "0.19", "size": "500"}],
                  "asks": [{"price": "0.20", "size": "500"}]},
        "rung2": {"bids": [{"price": "0.09", "size": "500"}],
                  "asks": [{"price": "0.10", "size": "500"}]},
    }

    events = m.process_jump(
        trigger_ref=trigger, pre=pre, post=post, ladder_refs=ladder,
        fetch_book=lambda tid: books[tid],
        fetch_ref_price=lambda tid: 0.15,
        detected_ts=100.0,
    )
    assert len(events) == 2
    assert {e.rung_token_id for e in events} == {"rung1", "rung2"}
    ev1 = next(e for e in events if e.rung_token_id == "rung1")
    assert ev1.jump_direction == "up"
    assert ev1.book_best_ask == 0.20
    assert ev1.fill_price == 0.20
    assert ev1.pre_jump_ref_price == 0.15
    assert ev1.fetch_latency_ms >= 0.0


def test_process_jump_handles_missing_book_gracefully():
    trigger = m.TokenRef(token_id="trig", condition_id="trig-cid",
                         category="match_1x2", team="France", outcome="Yes",
                         market_slug=None, game_start_time=None)
    ladder = [m.TokenRef(token_id="rung1", condition_id="c1", category="advancement_qf",
                         team="France", outcome="Yes", market_slug=None, game_start_time=None)]
    pre, post = m.PricePoint(ts=0.0, price=0.40), m.PricePoint(ts=10.0, price=0.55)
    events = m.process_jump(
        trigger_ref=trigger, pre=pre, post=post, ladder_refs=ladder,
        fetch_book=lambda tid: None, fetch_ref_price=lambda tid: None,
        detected_ts=5.0,
    )
    assert len(events) == 1
    assert events[0].fill_price is None
    assert events[0].fill_shares == 0.0


def test_process_jump_empty_ladder_produces_no_events():
    trigger = m.TokenRef(token_id="trig", condition_id="trig-cid",
                         category="match_1x2", team="France", outcome="Yes",
                         market_slug=None, game_start_time=None)
    pre, post = m.PricePoint(ts=0.0, price=0.40), m.PricePoint(ts=10.0, price=0.55)
    events = m.process_jump(
        trigger_ref=trigger, pre=pre, post=post, ladder_refs=[],
        fetch_book=lambda tid: None, fetch_ref_price=lambda tid: None,
    )
    assert events == []


def test_process_jump_stamps_thin_print_onto_every_event():
    trigger = m.TokenRef(token_id="trig", condition_id="trig-cid",
                         category="match_1x2", team="France", outcome="Yes",
                         market_slug=None, game_start_time=None)
    ladder = [
        m.TokenRef(token_id="rung1", condition_id="c1", category="advancement_qf",
                  team="France", outcome="Yes", market_slug=None, game_start_time=None),
        m.TokenRef(token_id="rung2", condition_id="c2", category="advancement_sf",
                  team="France", outcome="Yes", market_slug=None, game_start_time=None),
    ]
    pre, post = m.PricePoint(ts=0.0, price=0.40), m.PricePoint(ts=10.0, price=0.55)
    events = m.process_jump(
        trigger_ref=trigger, pre=pre, post=post, ladder_refs=ladder,
        fetch_book=lambda tid: None, fetch_ref_price=lambda tid: None,
        thin_print=True,
    )
    assert len(events) == 2
    assert all(e.thin_print is True for e in events)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_enforces_min_interval():
    limiter = m.RateLimiter(max_per_sec=10.0)  # min_interval = 0.1s
    clock = {"t": 0.0}

    def fake_clock():
        return clock["t"]

    limiter.wait(clock=fake_clock)  # first call, no sleep needed (last=0)
    clock["t"] = 0.05  # only 0.05s elapsed, less than 0.1s min interval

    import time as _time
    slept = []
    orig_sleep = _time.sleep
    _time.sleep = lambda s: slept.append(s)
    try:
        limiter.wait(clock=fake_clock)
    finally:
        _time.sleep = orig_sleep
    assert len(slept) == 1
    assert slept[0] > 0


# ---------------------------------------------------------------------------
# CLI: watch mode "no live matches" clean exit (structural, no network needed
# since discover_tokens_for_date short-circuits on a missing db)
# ---------------------------------------------------------------------------


def test_watch_no_matches_exits_clean(tmp_path, capsys):
    rc = m.run_watch(
        db_path=tmp_path / "missing.db",
        events_path=tmp_path / "events.jsonl",
        date_str="2026-07-08",
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "no live matches" in captured.out


def test_run_watch_cli_wiring_no_matches(tmp_path, capsys):
    args = m.build_parser().parse_args([
        "watch", "--db", str(tmp_path / "missing.db"),
        "--events", str(tmp_path / "events.jsonl"), "--date", "2026-07-08",
    ])
    rc = args.func(args)
    assert rc == 0
    assert "no live matches" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fetch_last_trade (network-free, monkeypatched requests.get)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_fetch_last_trade_parses_price_and_notional(monkeypatch):
    payload = [{"price": "0.42", "size": "119.5", "timestamp": "1751900000"}]
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResp(payload))
    pt = m.fetch_last_trade("tok")
    assert pt is not None
    assert pt.price == 0.42
    assert abs(pt.notional - (0.42 * 119.5)) < 1e-9
    assert pt.ts == 1751900000.0


def test_fetch_last_trade_empty_response_returns_none(monkeypatch):
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResp([]))
    assert m.fetch_last_trade("tok") is None


def test_fetch_last_trade_empty_token_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: called.append(1) or _FakeResp([]))
    assert m.fetch_last_trade("") is None
    assert called == []


def test_fetch_last_trade_swallows_network_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.fetch_last_trade("tok") is None


def test_fetch_last_trade_malformed_row_returns_none(monkeypatch):
    payload = [{"price": "bad", "size": "1"}]
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResp(payload))
    assert m.fetch_last_trade("tok") is None


# ---------------------------------------------------------------------------
# fetch_raw_book (network-free, monkeypatched requests.get)
# ---------------------------------------------------------------------------


def test_fetch_raw_book_returns_dict(monkeypatch):
    payload = {"bids": [{"price": "0.4", "size": "10"}], "asks": []}
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: _FakeResp(payload))
    out = m.fetch_raw_book("tok")
    assert out == payload


def test_fetch_raw_book_empty_token_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(m.requests, "get", lambda *a, **k: called.append(1) or _FakeResp({}))
    assert m.fetch_raw_book("") is None
    assert called == []


def test_fetch_raw_book_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(m.requests, "get", boom)
    assert m.fetch_raw_book("tok") is None
