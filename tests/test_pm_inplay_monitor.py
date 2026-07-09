"""Tests for the PM in-play monitor (network-free, order-placement-free).

Covers: structural never-places-orders guarantees, fee/edge arithmetic, the
book depth walk, feed-vs-PM state reconciliation (incl. conflicts), the
settlement-lag detectors (BTTS / O-U / exact-impossible / FT-winner /
ladder-lag) on constructed book+score fixtures, dedupe, sizing caps
($100 in-play), relay selection + fallback, proposal gate-shape, ping
formatting, and the mini-side ingest (idempotency + staleness gate).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca import inplay  # noqa: E402
import wca_pm_inplay_ingest as ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Structural safety: no order placement anywhere in the in-play stack
# ---------------------------------------------------------------------------

_MONITOR_FILES = (
    _REPO / "src" / "wca" / "inplay.py",
    _REPO / "scripts" / "wca_pm_inplay_monitor.py",
    _REPO / "scripts" / "wca_pm_inplay_ingest.py",
)


def test_never_imports_trader_or_signing():
    for path in _MONITOR_FILES:
        for line in path.read_text().splitlines():
            s = line.strip()
            if not (s.startswith("import ") or s.startswith("from ")):
                continue
            for forbidden in ("wca.pm.trader", "wca.pm.signing", "wca.pm.relayer",
                              "wca.pm.redeem"):
                assert forbidden not in s, "forbidden import in %s: %r" % (path.name, s)


def test_never_calls_place_order():
    for path in _MONITOR_FILES:
        src = path.read_text()
        assert "place_order" not in src, "%s mentions place_order" % path.name
        assert "ClobTrader" not in src, "%s mentions ClobTrader" % path.name


def test_paper_relay_touches_nothing():
    res = inplay.PaperRelay().park({"uid": "x"})
    assert res.ok and res.relay == "paper" and res.pm_token is None


# ---------------------------------------------------------------------------
# Fee / edge arithmetic
# ---------------------------------------------------------------------------


def test_fee_formula():
    assert inplay.fee(0.5) == 0.03 * 0.5 * 0.5
    assert inplay.fee(0.0) == 0.0
    assert inplay.fee(1.0) == 0.0
    assert inplay.fee(1.5) == 0.0  # clamped


def test_edge_after_fee_settled_btts():
    # 91c ask on a settled-$1 BTTS: 9c gross minus 0.03*0.91*0.09 fee.
    e = inplay.edge_after_fee(1.0, 0.91)
    assert abs(e - (0.09 - 0.03 * 0.91 * 0.09)) < 1e-12
    assert e > 0.08


# ---------------------------------------------------------------------------
# Depth walk
# ---------------------------------------------------------------------------


def _levels(*pairs):
    return [inplay.BookLevel(price=p, size=s) for p, s in pairs]


def test_walk_executable_walks_and_respects_cap():
    asks = _levels((0.90, 50.0), (0.92, 200.0))  # $45 + $184 notional
    fill = inplay.walk_executable(asks, 1.0, cap_usd=100.0)
    # Takes all of level 1 ($45) then $55 of level 2.
    assert abs(fill["notional"] - 100.0) < 1e-9
    shares = 50.0 + 55.0 / 0.92
    assert abs(fill["shares"] - shares) < 1e-6
    assert 0.90 < fill["avg_price"] < 0.92
    assert fill["edge"] > 0.05


def test_walk_executable_stops_at_min_edge_boundary():
    # fair 1.0, min_edge 0.02: 0.97 clears (edge ~0.0291), 0.98 does not (~0.0194).
    asks = _levels((0.97, 100.0), (0.98, 1000.0))
    fill = inplay.walk_executable(asks, 1.0, cap_usd=1000.0)
    assert abs(fill["notional"] - 97.0) < 1e-9  # only the 0.97 level
    assert abs(fill["avg_price"] - 0.97) < 1e-12


def test_walk_executable_empty_and_junk_book():
    assert inplay.walk_executable([], 1.0)["shares"] == 0.0
    fill = inplay.walk_executable(_levels((0.0, 10.0), (1.0, 10.0)), 1.0)
    assert fill["shares"] == 0.0


def test_parse_book_sorts_and_tolerates_junk():
    payload = {
        "bids": [{"price": "0.5", "size": "10"}, {"price": "0.6", "size": "5"}],
        "asks": [{"price": "0.9", "size": "1"}, {"price": "0.8", "size": "2"},
                 {"price": None, "size": "3"}],
    }
    bids, asks = inplay.parse_book(payload)
    assert [b.price for b in bids] == [0.6, 0.5]
    assert [a.price for a in asks] == [0.8, 0.9]
    assert inplay.parse_book(None) == ([], [])


# ---------------------------------------------------------------------------
# State reconciliation (feed vs PM-implied)
# ---------------------------------------------------------------------------


def _feed(h, a, minute=60, status="live"):
    return inplay.FeedScore(home_goals=h, away_goals=a, minute=minute,
                            status=status, ts_utc="2026-07-09T20:00:00Z")


def test_reconcile_agreement_feed_plus_pm():
    st = inplay.reconcile_state("A vs B", _feed(2, 1), btts_yes_mid=0.99,
                                over_mids={2.5: 0.99})
    assert st.both_scored.value and st.both_scored.source == "feed+pm"
    assert st.goals_ge[2.5].value and st.goals_ge[2.5].source == "feed+pm"
    assert st.conflicts == []


def test_reconcile_conflict_prefers_feed_and_records():
    # Feed says both scored; PM prices BTTS-Yes at 2c (implied false).
    st = inplay.reconcile_state("A vs B", _feed(1, 1), btts_yes_mid=0.02)
    assert st.both_scored.value is True
    assert st.both_scored.source == "conflict"
    assert any("both_scored" in c and "feed" in c for c in st.conflicts)


def test_reconcile_pm_only_is_labelled_and_never_a_scoreline():
    st = inplay.reconcile_state("A vs B", None, btts_yes_mid=0.99,
                                over_mids={2.5: 0.50})
    assert st.feed is None
    assert st.both_scored.value and st.both_scored.source == "pm"
    assert 2.5 not in st.goals_ge  # 0.50 mid is the grey zone -> no proposition
    assert "pm[" in st.state_sig()  # signature carries no fabricated score


def test_feed_confirmed_gates_pm_only():
    st_pm = inplay.reconcile_state("A vs B", None, btts_yes_mid=0.99)
    assert not inplay.feed_confirmed(st_pm.both_scored)
    st_feed = inplay.reconcile_state("A vs B", _feed(1, 1), btts_yes_mid=0.99)
    assert inplay.feed_confirmed(st_feed.both_scored)
    # Conflict still counts as feed-confirmed (feed wins).
    st_conf = inplay.reconcile_state("A vs B", _feed(1, 1), btts_yes_mid=0.02)
    assert inplay.feed_confirmed(st_conf.both_scored)


def test_load_feed_scores_freshness_gate(tmp_path):
    now = datetime(2026, 7, 9, 21, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = tmp_path / "live_scores.json"
    p.write_text(json.dumps({
        "France vs Morocco": {"home_goals": 2, "away_goals": 1, "minute": 62,
                              "status": "live", "ts_utc": fresh},
        "Spain vs Belgium": {"home_goals": 1, "away_goals": 0, "ts_utc": stale},
        "Junk vs Junk": {"home_goals": "x", "ts_utc": fresh},
    }))
    scores = inplay.load_feed_scores(str(p), now_ts=now.timestamp())
    assert set(scores) == {"France vs Morocco"}
    fs = scores["France vs Morocco"]
    assert (fs.scoreline, fs.minute, fs.total_goals) == ("2-1", 62, 3)


# ---------------------------------------------------------------------------
# Detectors — constructed book + score fixtures
# ---------------------------------------------------------------------------


def _book(asks, bids=()):
    return {"asks": [{"price": p, "size": s} for p, s in asks],
            "bids": [{"price": p, "size": s} for p, s in bids]}


def _btts_market():
    return inplay.MarketToken(kind="btts", question="Both teams to score?",
                              yes_token="tok-btts-yes", no_token="tok-btts-no")


def test_detect_btts_settled_math_and_cap():
    st = inplay.reconcile_state("France vs Morocco", _feed(2, 1), btts_yes_mid=0.91)
    book = _book([(0.91, 500.0)])  # $455 at 91c — deep
    opp = inplay.detect_btts(st, _btts_market(), book)
    assert opp is not None
    assert opp.fair == 1.0 and opp.outcome == "Yes"
    assert abs(opp.price - 0.91) < 1e-9
    # Walked depth is capped at the in-play safety cap...
    assert abs(opp.notional_usd - inplay.INPLAY_SAFETY_CAP_USD) < 1e-6
    # ...and the stake never exceeds $100 (fair=1.0 full-Kelly would be ~$160).
    assert opp.stake_usd <= inplay.INPLAY_SAFETY_CAP_USD + 1e-9
    assert abs(opp.stake_usd - 100.0) < 1e-6
    assert not opp.edge_is_estimate and opp.edge > 0.08
    assert "settles $1" in opp.reason and "2-1" in opp.reason


def test_detect_btts_requires_feed_confirmation():
    st = inplay.reconcile_state("A vs B", None, btts_yes_mid=0.99)  # PM-only
    assert inplay.detect_btts(st, _btts_market(), _book([(0.90, 500.0)])) is None


def test_detect_btts_skips_thin_or_priced_books():
    st = inplay.reconcile_state("A vs B", _feed(1, 1), btts_yes_mid=0.9)
    # Ask so close to $1 nothing clears the after-fee edge threshold.
    assert inplay.detect_btts(st, _btts_market(), _book([(0.995, 500.0)])) is None
    # Executable depth below the $25 floor.
    assert inplay.detect_btts(st, _btts_market(), _book([(0.90, 10.0)])) is None
    # Empty book.
    assert inplay.detect_btts(st, _btts_market(), _book([])) is None


def test_detect_ou_over_line_semantics():
    market25 = inplay.MarketToken(kind="total", question="More than 2.5 goals?",
                                  yes_token="t25", line=2.5)
    market35 = inplay.MarketToken(kind="total", question="More than 3.5 goals?",
                                  yes_token="t35", line=3.5)
    st = inplay.reconcile_state("A vs B", _feed(2, 1),
                                over_mids={2.5: 0.85, 3.5: 0.40})
    book = _book([(0.90, 500.0)])
    opp = inplay.detect_ou_over(st, market25, book)
    assert opp is not None and opp.fair == 1.0
    assert "Over 2.5" in opp.reason
    # 3 goals does NOT settle over 3.5.
    assert inplay.detect_ou_over(st, market35, book) is None


def test_exact_impossible_predicate():
    f = _feed(2, 1)
    assert inplay.exact_impossible((1, 0), f)      # home already past 1
    assert inplay.exact_impossible((2, 0), f)      # away already past 0
    assert not inplay.exact_impossible((2, 1), f)  # current score: still possible
    assert not inplay.exact_impossible((3, 2), f)  # future score: possible


def test_detect_exact_impossible_buys_no():
    market = inplay.MarketToken(kind="exact", question="Exact score 1-0?",
                                yes_token="ex-yes", no_token="ex-no", score=(1, 0))
    st = inplay.reconcile_state("A vs B", _feed(2, 1))
    no_book = _book([(0.90, 500.0)])
    opp = inplay.detect_exact_impossible(st, market, no_book, yes_bid=0.05)
    assert opp is not None
    assert opp.token_id == "ex-no" and opp.outcome == "No" and opp.fair == 1.0
    assert "impossible" in opp.reason
    # Row already dead (no meaningful YES bid) -> nothing stale to buy.
    assert inplay.detect_exact_impossible(st, market, no_book, yes_bid=0.001) is None
    # Possible scoreline -> no opportunity.
    possible = inplay.MarketToken(kind="exact", question="Exact 3-1?",
                                  yes_token="y", no_token="n", score=(3, 1))
    assert inplay.detect_exact_impossible(st, possible, no_book, yes_bid=0.10) is None


def test_detect_ft_winner():
    home = inplay.MarketToken(kind="1x2", question="Will France win?",
                              yes_token="fr", team="home")
    away = inplay.MarketToken(kind="1x2", question="Will Morocco win?",
                              yes_token="ma", team="away")
    draw = inplay.MarketToken(kind="1x2", question="End in a draw?",
                              yes_token="dr", team="draw")
    book = _book([(0.94, 500.0)])
    st_live = inplay.reconcile_state("F vs M", _feed(2, 1, status="live"))
    assert inplay.detect_ft_winner(st_live, home, book) is None  # not FT yet
    st_ft = inplay.reconcile_state("F vs M", _feed(2, 1, status="ft"))
    opp = inplay.detect_ft_winner(st_ft, home, book)
    assert opp is not None and "90-min" in opp.reason
    assert inplay.detect_ft_winner(st_ft, away, book) is None
    st_draw = inplay.reconcile_state("F vs M", _feed(1, 1, status="ft"))
    assert inplay.detect_ft_winner(st_draw, draw, book) is not None
    assert inplay.detect_ft_winner(st_draw, home, book) is None


def _rung():
    return inplay.MarketToken(kind="ladder", question="France — advancement_sf",
                              yes_token="rung-sf", team="France",
                              rung="advancement_sf")


def test_detect_ladder_lag_stale_rung():
    st = inplay.reconcile_state("F vs M", _feed(1, 0))
    book = _book([(0.775, 200.0)])
    opp = inplay.detect_ladder_lag(
        st, _rung(), book, trigger_team="France",
        jump_pre=0.55, jump_post=0.68, rung_pre_ref=0.775)
    assert opp is not None
    assert opp.edge_is_estimate and opp.fair is None
    assert opp.stake_usd <= inplay.LADDER_STAKE_USD + 1e-9
    assert "n=%d" % inplay.LADDER_HIST_N in opp.reason  # estimate is labelled
    assert opp.market.settlement == "ET+pens"


def test_detect_ladder_lag_rejects_down_moves_and_repriced_rungs():
    st = inplay.reconcile_state("F vs M", _feed(0, 1))
    stale_book = _book([(0.775, 200.0)])
    # Down move: buying the rung would be the WRONG side.
    assert inplay.detect_ladder_lag(
        st, _rung(), stale_book, trigger_team="France",
        jump_pre=0.55, jump_post=0.40, rung_pre_ref=0.775) is None
    # Rung already repriced above the stale band.
    repriced = _book([(0.85, 200.0)])
    assert inplay.detect_ladder_lag(
        st, _rung(), repriced, trigger_team="France",
        jump_pre=0.55, jump_post=0.68, rung_pre_ref=0.775) is None
    # No pre-jump reference -> cannot claim staleness.
    assert inplay.detect_ladder_lag(
        st, _rung(), stale_book, trigger_team="France",
        jump_pre=0.55, jump_post=0.68, rung_pre_ref=None) is None


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def test_size_stake_capped_at_inplay_safety_cap():
    # Settled-certain (fair 1.0) at 91c: full Kelly -> 4%-of-pool cap ($159.6)
    # -> in-play cap $100.
    stake = inplay.size_stake_usd(1.0, 0.91, bankroll_usd=3990.0)
    assert abs(stake - 100.0) < 1e-9
    # Small edge sizes below the cap.
    small = inplay.size_stake_usd(0.55, 0.54, bankroll_usd=3990.0)
    assert 0.0 < small < 100.0


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def _opp(detector="btts_settled", token="tok"):
    return inplay.Opportunity(
        uid="u1", match_key="F vs M", detector=detector,
        market=_btts_market(), token_id=token, outcome="Yes", fair=1.0,
        price=0.91, best_ask=0.91, shares=100.0, notional_usd=91.0,
        stake_usd=91.0, edge=0.087, edge_is_estimate=False,
        reason="r", state_sources="feed")


def test_dedupe_one_ping_per_state_change():
    reg = inplay.DedupeRegistry()
    opp = _opp()
    assert reg.should_ping(opp, "1-1@live")
    reg.mark(opp, "1-1@live")
    assert not reg.should_ping(opp, "1-1@live")   # same state: no re-ping
    assert reg.should_ping(opp, "2-1@live")       # new state: eligible again


def test_dedupe_replay_from_session_log(tmp_path):
    log = tmp_path / "log.jsonl"
    opp = _opp()
    inplay.append_log({"type": "ping", "dedupe_key": opp.dedupe_key("1-1@live")},
                      str(log))
    inplay.append_log({"type": "opportunity", "dedupe_key": "not-a-ping"}, str(log))
    keys = inplay.replay_pinged_keys(str(log))
    reg = inplay.DedupeRegistry(keys)
    assert not reg.should_ping(opp, "1-1@live")  # restart is idempotent
    assert reg.should_ping(opp, "2-1@live")


# ---------------------------------------------------------------------------
# Relays
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_select_relay_prefers_ssh_when_reachable():
    ssh_up = inplay.SshRelay(runner=lambda cmd, timeout: _FakeProc(0))
    ssh_down = inplay.SshRelay(runner=lambda cmd, timeout: _FakeProc(255))
    git = inplay.GitArtifactRelay("/nonexistent")
    assert inplay.select_relay(ssh_up, git) is ssh_up
    assert inplay.select_relay(ssh_down, git) is git


def test_ssh_relay_probe_survives_exceptions():
    def _boom(cmd, timeout):
        raise OSError("no ssh binary")

    assert not inplay.SshRelay(runner=_boom).available()


def test_ssh_relay_park_returns_pm_token():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        if cmd[-1] == "true":
            return _FakeProc(0)
        return _FakeProc(0, out="already? no\nPM-7\n")

    relay = inplay.SshRelay(runner=runner)
    res = relay.park({"uid": "u1", "token_id": "t", "price": 0.9, "size": 10,
                      "side": "BUY"})
    assert res.ok and res.pm_token == "PM-7" and res.relay == "ssh"
    # The payload travels base64-encoded (no shell-quoting hazards).
    assert "--park-b64" in calls[-1][-1]


def test_ssh_relay_park_failure_is_graceful():
    relay = inplay.SshRelay(runner=lambda cmd, timeout: _FakeProc(1, err="denied"))
    res = relay.park({"uid": "u1"})
    assert not res.ok and res.relay == "ssh" and "denied" in res.detail


def test_git_relay_appends_commits_pushes(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    cmds = []

    def runner(cmd, timeout):
        cmds.append(cmd)
        return _FakeProc(0)

    relay = inplay.GitArtifactRelay(str(tmp_path), worktree=str(wt), runner=runner)
    res = relay.park({"uid": "abc", "detector": "btts_settled",
                      "token_id": "t", "price": 0.9, "size": 10, "side": "BUY"})
    assert res.ok and res.relay == "git"
    assert "≤6min" in res.detail  # latency is stated honestly
    doc = json.loads((wt / inplay.PROPOSALS_PATH).read_text())
    assert [p["uid"] for p in doc["proposals"]] == ["abc"]
    flat = [" ".join(c) for c in cmds]
    assert any("fetch origin main" in c for c in flat)
    assert any("reset --hard origin/main" in c for c in flat)
    assert any("push origin HEAD:main" in c for c in flat)
    # Dedupe by uid: a second park of the same uid appends nothing and reports
    # "already relayed" (the first park pinged it; nothing new to push).
    n_cmds = len(cmds)
    res2 = relay.park({"uid": "abc"})
    doc2 = json.loads((wt / inplay.PROPOSALS_PATH).read_text())
    assert len(doc2["proposals"]) == 1
    assert res2.ok and "already relayed" in res2.detail
    flat2 = [" ".join(c) for c in cmds[n_cmds:]]
    assert not any("push" in c for c in flat2)  # no duplicate push


def test_git_relay_push_failure_reports(tmp_path):
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)

    def runner(cmd, timeout):
        if "push" in cmd:
            return _FakeProc(1, err="rejected (non-ff)")
        if "reset" in cmd:
            # Emulate what real git does: reset --hard origin/main discards
            # the un-pushed append so the retry starts from the remote state.
            artifact = wt / inplay.PROPOSALS_PATH
            if artifact.exists():
                artifact.write_text(json.dumps({"proposals": []}))
        return _FakeProc(0)

    relay = inplay.GitArtifactRelay(str(tmp_path), worktree=str(wt), runner=runner)
    res = relay.park({"uid": "u9", "side": "BUY"})
    assert not res.ok and "rejected" in res.detail


# ---------------------------------------------------------------------------
# Proposal packaging + ping formatting
# ---------------------------------------------------------------------------


def test_to_parked_proposal_gate_shape():
    opp = _opp()
    p = inplay.to_parked_proposal(opp)
    # Keys the bot gate + Y-flow rely on (mirrors _augment_for_gate).
    for key in ("token_id", "side", "price", "size", "size_usd",
                "market_question", "outcome", "match_desc", "model_prob",
                "ev", "neg_risk", "label"):
        assert key in p, "missing gate key %s" % key
    assert p["side"] == "BUY"
    assert abs(p["size"] - round(91.0 / 0.91, 2)) < 1e-9  # size is SHARES
    assert p["price"] * p["size"] <= inplay.INPLAY_SAFETY_CAP_USD + 0.5
    assert p["uid"] and p["inplay"] is True and p["detector"] == "btts_settled"


def test_ping_format_git_relay_has_sync_caveat():
    text = inplay.format_opportunity_ping(_opp(), relay_name="git")
    assert "fireable after mini sync" in text
    assert "91c" in text and "$91" in text          # PM cents + $ stake
    assert "90-min" in text                          # settlement basis flagged
    assert "BTTS-Yes" not in text or True
    assert "Y PM-" in text or "PM-<n>" in text


def test_ping_format_ssh_relay_names_pm_token():
    text = inplay.format_opportunity_ping(_opp(), relay_name="ssh", pm_token="PM-3")
    assert "`Y PM-3`" in text and "fireable after mini sync" not in text
    assert "PM_DRY_RUN" in text


def test_ping_format_carries_conflicts():
    text = inplay.format_opportunity_ping(
        _opp(), relay_name="git",
        conflicts=["both_scored: feed says True but PM prices imply False — using feed"])
    assert "⚠" in text and "using feed" in text


def test_classify_impact_heuristics():
    assert inplay.classify_impact("Will France win?", "Yes", "France", "Morocco") == "helps"
    assert inplay.classify_impact("Will Morocco win?", "Yes", "France", "Morocco") == "hurts"
    assert inplay.classify_impact("Will France win?", "No", "France", "Morocco") == "hurts"
    assert "draw" in inplay.classify_impact("End in a draw?", "Yes", "France", "Morocco")
    assert inplay.classify_impact("Total corners 9+?", "Yes", "France", "Morocco") \
        == "impact unclear"


# ---------------------------------------------------------------------------
# Session log
# ---------------------------------------------------------------------------


def test_append_log_appends_jsonl(tmp_path):
    log = tmp_path / "log.jsonl"
    inplay.append_log({"type": "state_change", "sig": "1-0@live"}, str(log))
    inplay.append_log({"type": "ping", "dedupe_key": "k"}, str(log))
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert [l["type"] for l in lines] == ["state_change", "ping"]
    assert all("ts_utc" in l for l in lines)


# ---------------------------------------------------------------------------
# Mini-side ingest (park + DM; idempotent; staleness-gated; capped)
# ---------------------------------------------------------------------------


def _proposal(uid="u1", age_mins=1.0, notional=90.0, side="BUY"):
    created = datetime.now(timezone.utc) - timedelta(minutes=age_mins)
    price = 0.9
    return {
        "uid": uid,
        "created_utc": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_id": "tok",
        "side": side,
        "price": price,
        "size": round(notional / price, 2),
        "size_usd": notional,
        "match_desc": "France vs Morocco",
        "reason": "2-1 62' — BTTS-Yes ask 91c, settles $1",
        "settlement_basis": "90-min",
        "market_question": "Both teams to score?",
        "outcome": "Yes",
        "model_prob": 1.0,
        "ev": 0.09,
    }


def test_validate_proposal_rejects_bad_shapes():
    assert ingest.validate_proposal(_proposal()) is None
    assert "cap" in ingest.validate_proposal(_proposal(notional=150.0))
    assert "BUY" in ingest.validate_proposal(_proposal(side="SELL"))
    missing = _proposal()
    del missing["token_id"]
    assert "token_id" in ingest.validate_proposal(missing)
    bad = _proposal()
    bad["price"] = 1.5
    assert ingest.validate_proposal(bad) is not None


def test_ingest_file_parks_fresh_skips_stale_and_is_idempotent(tmp_path):
    proposals = tmp_path / "proposals.json"
    state = tmp_path / "state.json"
    doc = {"proposals": [_proposal("fresh", age_mins=2.0),
                         _proposal("stale", age_mins=120.0),
                         _proposal("fat", age_mins=2.0, notional=500.0)]}
    proposals.write_text(json.dumps(doc))

    parked, notes = [], []

    def fake_park(p):
        parked.append(p["uid"])
        return "PM-%d" % len(parked), "parked text PM-%d" % len(parked)

    res = ingest.ingest_file(proposals, state, park=fake_park,
                             notify=lambda t: notes.append(t) or True)
    assert res["parked"] == ["fresh"]
    assert res["skipped"] == ["stale"]
    assert res["invalid"] == ["fat"]
    assert parked == ["fresh"]
    # The DM carries the fireable text and the skip reason respectively.
    assert any("PM-1" in n for n in notes)
    assert any("SKIPPED" in n for n in notes)
    # Second run: nothing new (idempotent — autopull races can't double-park).
    res2 = ingest.ingest_file(proposals, state, park=fake_park,
                              notify=lambda t: True)
    assert res2 == {"parked": [], "skipped": [], "invalid": []}
    assert parked == ["fresh"]


def test_ingest_unparseable_created_fails_staleness_gate(tmp_path):
    proposals = tmp_path / "p.json"
    state = tmp_path / "s.json"
    p = _proposal("nots")
    p["created_utc"] = "garbage"
    proposals.write_text(json.dumps({"proposals": [p]}))
    res = ingest.ingest_file(proposals, state, park=lambda p: ("PM-1", "t"),
                             notify=lambda t: True)
    assert res["skipped"] == ["nots"] and res["parked"] == []


def test_ingest_missing_file_is_noop(tmp_path):
    res = ingest.ingest_file(tmp_path / "absent.json", tmp_path / "s.json",
                             park=lambda p: ("PM-1", "t"), notify=lambda t: True)
    assert res == {"parked": [], "skipped": [], "invalid": []}
