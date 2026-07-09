"""Tests for the 02A Event-Market "Fire → park a PM-<n>" flow.

Network-free and order-placement-free. Covers:

* ``wca.eventfire`` governance gate (validate_fireable) for every failure mode
  (killed family, dimmed / $0, under-signal no-cash, longshot bucket, non-positive
  edge, stale feed) plus the happy path.
* Token resolution: feed token first, advancement orderflow fallback, and the
  null-token-unresolvable hard reject (never guesses).
* Proposal packaging: BUY side, SHARES sizing, settlement carried, stake clamped
  to the hard fire cap, deterministic idempotent uid — and it passes the EXISTING
  in-play ingest ``validate_proposal`` gate (so we neither fork nor loosen it).
* The ``scripts/wca_place_server.py`` ``POST /park-event`` endpoint end-to-end
  over a real loopback ``ThreadingHTTPServer``: non-loopback 403, bad/missing
  token 403, each governance-fail row rejected with the right reason, a VALID
  row parked (relay mocked), dry-run never touching a live path, nonce
  idempotency, and the unresolved-token clean reject.
* The frontend fireable predicate logic (Python mirror of the JS
  ``isFireable``) — the JS itself has a documented manual verification step
  (no JS engine in the repo).
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from wca import eventfire  # noqa: E402
import wca_place_server as place_server  # noqa: E402
import wca_pm_inplay_ingest as ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — a synthetic feed with one fireable + several non-fireable rows
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 8, 16, 0, 0, tzinfo=timezone.utc)
_FRESH = "2026-07-08 15:30:00 UTC"  # 30 min before _NOW (for now=_NOW unit tests)


def _fresh_stamp(mins_ago=15):
    """A 'captured_utc'-style stamp a few minutes before REAL now — used for the
    endpoint tests, which run against the server's real clock (not _NOW)."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _rec(**over):
    base = {
        "fixture": "Argentina vs Switzerland",
        "kickoff": "2026-07-09T20:00:00Z",
        "family": "total_goals",
        "label": "Over 1.5",
        "side": "back",
        "selection": "Over 1.5",
        "settlement": "90min",
        "model_prob": 0.72,
        "price": 0.55,
        "price_c": 55.0,
        "model_c": 72.0,
        "edge_net": 0.02,
        "ev": 0.036,
        "ev_pct": 3.6,
        "bucket": "moneyline",
        "dimmed": False,
        "no_cash_reason": None,
        "stake_usd": 74.35,
        "token_id": "3083218647348548933412010479603238893052666063347659714613",
        "captured_utc": _FRESH,
        "hours_out": 28.0,
    }
    base.update(over)
    return base


def _feed(*recs, generated="2026-07-08 15:26:42 UTC"):
    return {
        "meta": {
            "per_order_cap_usd": 159.6,
            "generated": generated,
            "correlation_cap": "same-fixture cap",
        },
        "recs": list(recs),
    }


def _fresh_feed(*recs):
    """A feed whose rows + meta are fresh vs REAL now (for endpoint tests)."""
    stamp = _fresh_stamp()
    fresh = []
    for r in recs:
        rr = dict(r)
        rr.setdefault("captured_utc", stamp)
        rr["captured_utc"] = stamp
        fresh.append(rr)
    return _feed(*fresh, generated=stamp)


# ---------------------------------------------------------------------------
# eventfire.validate_fireable — happy path + every reject
# ---------------------------------------------------------------------------


def test_validate_happy_path_sized_moneyline():
    feed = _feed(_rec())
    assert eventfire.validate_fireable(feed, feed["recs"][0], now=_NOW) is None


def test_validate_rejects_killed_family():
    r = _rec(family="scorer_prop", selection="Messi anytime", bucket="mid")
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "killed market family" in reason


def test_validate_rejects_correct_score_family():
    r = _rec(family="correct_score", selection="2-1")
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "killed market family" in reason


def test_validate_rejects_dimmed_zero_stake():
    r = _rec(dimmed=True, stake_usd=0.0, no_cash_reason=None)
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "dimmed" in reason


def test_validate_rejects_under_signal_no_cash():
    r = _rec(family="total_goals", selection="Under 2.5", stake_usd=0.0,
             dimmed=True, no_cash_reason="under-signal: DC under-calls unreliable")
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "under-signal" in reason


def test_validate_rejects_longshot_bucket():
    r = _rec(bucket="longshot", model_prob=0.18)
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "longshot" in reason


def test_validate_rejects_zero_stake_even_if_not_dimmed():
    r = _rec(stake_usd=0.0)
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "$0" in reason


def test_validate_rejects_non_positive_edge():
    r = _rec(edge_net=0.0)
    reason = eventfire.validate_fireable(_feed(r), r, now=_NOW)
    assert reason and "edge_net" in reason


def test_validate_rejects_stale_feed():
    r = _rec(captured_utc="2026-07-08 05:00:00 UTC")  # 11h before _NOW
    feed = {"meta": {"generated": "2026-07-08 05:00:00 UTC"}, "recs": [r]}
    reason = eventfire.validate_fireable(feed, r, now=_NOW)
    assert reason and "stale" in reason


def test_validate_rejects_missing_timestamp():
    r = _rec(captured_utc="")
    feed = {"meta": {}, "recs": [r]}
    reason = eventfire.validate_fireable(feed, r, now=_NOW)
    assert reason and "stale" in reason


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def test_resolve_token_from_feed():
    r = _rec()
    tok, reason = eventfire.resolve_token(r)
    assert reason is None and tok == r["token_id"]


def test_resolve_token_null_aux_family_is_hard_reject():
    r = _rec(family="spread", token_id=None, selection="Argentina -1.5")
    tok, reason = eventfire.resolve_token(r)
    assert tok is None and "unresolved token" in reason


def test_resolve_token_null_advance_missing_db_rejects(tmp_path):
    r = _rec(family="advance", token_id=None, selection="England to advance",
             settlement="ET+pens")
    tok, reason = eventfire.resolve_token(r, orderflow_db=tmp_path / "absent.db")
    assert tok is None and "unresolved token" in reason


def test_resolve_token_advance_from_orderflow_db(tmp_path):
    import sqlite3

    db = tmp_path / "pm_orderflow.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE pm_markets (question TEXT, outcomes TEXT, token_ids TEXT, category TEXT)")
    conn.execute(
        "INSERT INTO pm_markets VALUES (?,?,?,?)",
        ("Will Wonderland win the World Cup?",
         '["Yes", "No"]', '["TOKEN_YES_111", "TOKEN_NO_222"]', "advancement_final"))
    conn.commit()
    conn.close()
    r = _rec(family="advance", token_id=None, selection="Wonderland to advance",
             settlement="ET+pens")
    tok, reason = eventfire.resolve_token(r, orderflow_db=db)
    assert reason is None and tok == "TOKEN_YES_111"


def test_resolve_token_ambiguous_stage_rejects(tmp_path):
    import sqlite3

    db = tmp_path / "pm_orderflow.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE pm_markets (question TEXT, outcomes TEXT, token_ids TEXT, category TEXT)")
    for stage, tok in (("advancement_r16", "T_R16"), ("advancement_qf", "T_QF")):
        conn.execute(
            "INSERT INTO pm_markets VALUES (?,?,?,?)",
            ("Will Wonderland reach the next round?",
             '["Yes", "No"]', '["%s", "N"]' % tok, stage))
    conn.commit()
    conn.close()
    r = _rec(family="advance", token_id=None, selection="Wonderland to advance")
    tok, reason = eventfire.resolve_token(r, orderflow_db=db)
    assert tok is None and "ambiguous" in reason


# ---------------------------------------------------------------------------
# Proposal packaging
# ---------------------------------------------------------------------------


def test_build_proposal_shape_and_side():
    feed = _feed(_rec())
    p = eventfire.build_proposal(feed, feed["recs"][0], "TOK", nonce="n1", now=_NOW)
    assert p["side"] == "BUY"
    assert p["token_id"] == "TOK"
    assert p["settlement_basis"] == "90min"
    assert p["size"] == p["shares"] > 0
    assert p["detector"] == "event_market_fire"
    # SHARES sizing: notional = price * size within a cent of size_usd.
    assert abs(p["price"] * p["size"] - p["size_usd"]) < 0.05


def test_build_proposal_carries_advance_settlement():
    r = _rec(family="advance", settlement="ET+pens", selection="Belgium to advance",
             token_id="ADV_TOK", bucket="mid", model_prob=0.44)
    p = eventfire.build_proposal(_feed(r), r, "ADV_TOK", nonce="n2", now=_NOW)
    assert p["settlement_basis"] == "ET+pens"


def test_stake_clamped_to_hard_fire_cap():
    r = _rec(stake_usd=140.0)  # above the $100 hard fire cap
    p = eventfire.build_proposal(_feed(r), r, "TOK", nonce="n3", now=_NOW)
    assert p["size_usd"] == eventfire.HARD_FIRE_CAP_USD == 100.0


def test_uid_deterministic_for_idempotency():
    feed = _feed(_rec())
    a = eventfire.build_proposal(feed, feed["recs"][0], "TOK", nonce="same", now=_NOW)
    b = eventfire.build_proposal(feed, feed["recs"][0], "TOK", nonce="same", now=_NOW)
    c = eventfire.build_proposal(feed, feed["recs"][0], "TOK", nonce="other", now=_NOW)
    assert a["uid"] == b["uid"] and a["uid"] != c["uid"]


def test_proposal_passes_inplay_ingest_validation():
    """The built proposal must satisfy the EXISTING in-play ingest gate — this
    is what lets us reuse that park path without forking or loosening it."""
    r = _rec(stake_usd=140.0)  # even an over-cap rec clamps under the $100 gate
    p = eventfire.build_proposal(_feed(r), r, "TOK", nonce="n4", now=_NOW)
    assert ingest.validate_proposal(p) is None


# ---------------------------------------------------------------------------
# /park-event endpoint over a real loopback server (relay mocked)
# ---------------------------------------------------------------------------

_TOKEN = "test-secret-abc"


class _FakeRelay:
    """Stands in for inplay.SshRelay/GitArtifactRelay; records the proposal."""

    def __init__(self, result):
        self.name = getattr(result, "relay", "ssh")
        self._result = result
        self.parked = []

    def available(self):
        return True

    def park(self, proposal):
        self.parked.append(proposal)
        return self._result


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Boot the real handler on 127.0.0.1:<ephemeral>, with a token + a synthetic
    feed and a mocked relay. Yields (base_url, state)."""
    from wca import inplay

    feed_path = tmp_path / "event_market_recs.json"
    state = {"feed": _fresh_feed(_rec()), "relay": None, "parked": []}

    def _write_feed():
        feed_path.write_text(json.dumps(state["feed"]))
    _write_feed()

    monkeypatch.setenv("WCA_PLACE_TOKEN", _TOKEN)
    monkeypatch.setenv("PM_DRY_RUN", "1")
    monkeypatch.setattr(place_server, "LOCAL_EVENT_RECS", str(feed_path))

    # Default relay: SSH success returning a PM tag. Individual tests can swap it.
    default = inplay.RelayResult(True, "ssh", pm_token="PM-42", detail="parked over ssh")
    fake = _FakeRelay(default)
    state["relay"] = fake
    # select_relay just returns our fake regardless of ssh/git.
    monkeypatch.setattr(inplay, "select_relay", lambda ssh, git: state["relay"])

    httpd = ThreadingHTTPServer((place_server.BIND_HOST, 0), place_server._Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    state["write_feed"] = _write_feed
    try:
        yield "http://127.0.0.1:%d" % port, state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base, path, body, token=_TOKEN):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("X-WCA-Place-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_endpoint_valid_row_parks_and_returns_tag(server):
    base, state = server
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "family": "total_goals",
        "selection": "Over 1.5", "nonce": "web-1"})
    assert status == 200
    assert body["ok"] is True
    assert body["pm_tag"] == "PM-42"
    assert body["dry_run"] is True
    assert body["settlement"] == "90 min"
    # relay actually received a BUY proposal within the hard cap.
    assert len(state["relay"].parked) == 1
    prop = state["relay"].parked[0]
    assert prop["side"] == "BUY" and prop["size_usd"] <= eventfire.HARD_FIRE_CAP_USD


def test_endpoint_missing_token_403(server):
    base, _ = server
    status, body = _post(base, "/park-event",
                         {"fixture": "Argentina vs Switzerland",
                          "selection": "Over 1.5", "nonce": "n"}, token=None)
    assert status == 403 and "token" in body["message"]


def test_endpoint_bad_token_403(server):
    base, _ = server
    status, body = _post(base, "/park-event",
                         {"fixture": "Argentina vs Switzerland",
                          "selection": "Over 1.5", "nonce": "n"}, token="wrong")
    assert status == 403 and "token" in body["message"]


def test_endpoint_non_loopback_403(server, monkeypatch):
    base, _ = server
    # Force the loopback check to fail as if a remote client connected.
    monkeypatch.setattr(place_server._Handler, "_is_loopback", lambda self: False)
    status, body = _post(base, "/park-event",
                         {"fixture": "Argentina vs Switzerland",
                          "selection": "Over 1.5", "nonce": "n"})
    assert status == 403 and "loopback" in body["message"]


def test_endpoint_dimmed_row_rejected(server):
    base, state = server
    state["feed"] = _fresh_feed(_rec(dimmed=True, stake_usd=0.0,
                               selection="Under 2.5", family="total_goals"))
    state["write_feed"]()
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Under 2.5",
        "nonce": "n"})
    assert status == 200 and body["ok"] is False
    assert "not fireable" in body["message"]
    assert not state["relay"].parked  # never reached the relay


def test_endpoint_killed_family_rejected(server):
    base, state = server
    state["feed"] = _fresh_feed(_rec(family="scorer_prop", selection="Messi anytime",
                               bucket="mid"))
    state["write_feed"]()
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Messi anytime",
        "family": "scorer_prop", "nonce": "n"})
    assert status == 200 and body["ok"] is False
    assert "killed market family" in body["message"]
    assert not state["relay"].parked


def test_endpoint_longshot_rejected(server):
    base, state = server
    state["feed"] = _fresh_feed(_rec(bucket="longshot", model_prob=0.15,
                               selection="Draw no bet"))
    state["write_feed"]()
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Draw no bet",
        "nonce": "n"})
    assert status == 200 and body["ok"] is False
    assert "longshot" in body["message"]


def test_endpoint_stale_feed_rejected(server):
    base, state = server
    stale = _rec(captured_utc="2000-01-01 00:00:00 UTC")
    state["feed"] = {"meta": {"generated": "2000-01-01 00:00:00 UTC",
                              "per_order_cap_usd": 159.6}, "recs": [stale]}
    state["write_feed"]()
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Over 1.5",
        "nonce": "n"})
    assert status == 200 and body["ok"] is False
    assert "stale" in body["message"]


def test_endpoint_unresolved_token_clean_reject(server):
    base, state = server
    state["feed"] = _fresh_feed(_rec(family="spread", token_id=None,
                               selection="Argentina -1.5"))
    state["write_feed"]()
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Argentina -1.5",
        "family": "spread", "nonce": "n"})
    assert status == 200 and body["ok"] is False
    assert "unresolved token" in body["message"]
    assert not state["relay"].parked


def test_endpoint_dry_run_never_live(server):
    """PM_DRY_RUN=1 (default) is reflected in the response and the server never
    forwards a live flag — the proposal is only ever PARKED via the relay."""
    base, state = server
    status, body = _post(base, "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Over 1.5",
        "nonce": "n"})
    assert body["ok"] is True and body["dry_run"] is True


def test_endpoint_nonce_idempotency_same_uid(server, monkeypatch):
    """Two clicks with the SAME nonce build the SAME proposal uid — the relay /
    ingest dedupe by uid, so the park is idempotent."""
    base, state = server
    seen = []
    state["relay"].park = lambda p: (seen.append(p["uid"]),
                                     __import__("wca.inplay", fromlist=["RelayResult"])
                                     .RelayResult(True, "ssh", pm_token="PM-7"))[1]
    _post(base, "/park-event", {"fixture": "Argentina vs Switzerland",
                                "selection": "Over 1.5", "nonce": "dup"})
    _post(base, "/park-event", {"fixture": "Argentina vs Switzerland",
                                "selection": "Over 1.5", "nonce": "dup"})
    assert len(seen) == 2 and seen[0] == seen[1]  # identical uid → ingest dedupes


def test_endpoint_git_fallback_returns_pending_sync(server, monkeypatch):
    from wca import inplay

    state = server[1]
    state["relay"] = _FakeRelay(inplay.RelayResult(
        True, "git", pm_token=None, detail="pushed to main; fireable after mini sync"))
    status, body = _post(server[0], "/park-event", {
        "fixture": "Argentina vs Switzerland", "selection": "Over 1.5",
        "nonce": "g1"})
    assert status == 200 and body["ok"] is True
    assert body["pm_tag"] is None and body.get("pending_sync") is True


def test_place_endpoint_unaffected(server):
    """/place still 404s nothing and keeps its own contract (rec_id required)."""
    base, _ = server
    status, body = _post(base, "/place", {"nonce": "n"})  # missing rec_id
    assert status == 400 and "rec_id" in body["message"]


# ---------------------------------------------------------------------------
# Frontend fireable predicate — Python mirror of the JS isFireable()
# (the JS is manually verified in a browser; see the PR's VERIFY section)
# ---------------------------------------------------------------------------

_KILLED = {"exact_score", "scorer_prop", "correct_score"}


def _js_fireable(r):
    """Byte-for-byte mirror of site/arb.html window.wcaEventMarketFireable."""
    if not r:
        return False
    if str(r.get("family", "")).lower() in _KILLED:
        return False
    if r.get("no_cash_reason"):
        return False
    if r.get("dimmed"):
        return False
    if not (float(r.get("stake_usd") or 0) > 0):
        return False
    b = str(r.get("bucket", "")).lower()
    if b not in ("moneyline", "mid"):
        return False
    if not (float(r.get("edge_net") or 0) > 0):
        return False
    return True


def test_frontend_predicate_sized_row_fireable():
    assert _js_fireable(_rec()) is True


def test_frontend_predicate_dimmed_row_not_fireable():
    assert _js_fireable(_rec(dimmed=True, stake_usd=0.0)) is False


def test_frontend_predicate_matches_backend_on_every_row():
    """The frontend and backend gates must AGREE on the sample feed — a row the
    UI shows a button for is one the server would accept (barring token
    resolution / staleness, which the UI cannot see)."""
    feed = _feed(
        _rec(),                                                    # fireable
        _rec(family="scorer_prop", selection="X", bucket="mid"),   # killed
        _rec(dimmed=True, stake_usd=0.0, selection="U2.5"),        # dimmed
        _rec(bucket="longshot", model_prob=0.1, selection="LS"),   # longshot
        _rec(edge_net=0.0, selection="flat"),                      # no edge
    )
    for r in feed["recs"]:
        ui = _js_fireable(r)
        backend = eventfire.validate_fireable(feed, r, now=_NOW) is None
        assert ui == backend, r["selection"]
