#!/usr/bin/env python3
"""PM in-play monitor — live-window loop over open exposure + stale quotes.

Runs on the MACBOOK (the only box with a Polymarket route, VPN up) during live
World Cup matches. Each poll (30-60 s, ``--poll``) it:

1. Reconciles match state from (a) the ``data/live_scores.json`` scores feed
   (freshness-gated; the repo has no automated in-game score source today —
   see ``wca.inplay.load_feed_scores`` for the schema any scorer/human can
   write) and (b) PM's own prices as an implicit signal (BTTS/totals mids near
   0/1; >=10c 1X2 jumps via PR #179's ``detect_jump``). Sources are labelled;
   conflicts prefer the feed; a scoreline is never fabricated.
2. Marks OPEN PM positions (Data API, read-only) against developments and
   pings position / impact / mark / MTM on each state change.
3. Scans for settlement-lagged quotes (detectors in :mod:`wca.inplay`):
   BTTS-Yes with both teams scored, Over past its line, impossible
   exact-score rows still bid, FT-settled 1X2, and same-team advancement
   ladder rungs lagging a >=10c 1X2 move (ladder-lag, PR #179 logic reused).
   Edges are computed after fee (0.03*p*(1-p)) on the ACTUAL walked book;
   < $25 executable is ignored.
4. Pings Telegram immediately (``--paper`` writes to a file instead) and parks
   fireable proposals via the pluggable relay (SSH to the mini when reachable,
   else the git artifact relay — see the :mod:`wca.inplay` module docstring
   for the full relay design). THE MONITOR NEVER PLACES OR FIRES ORDERS — the
   human's ``Y PM-<n>`` on the mini (behind ``PM_DRY_RUN``) is the only fire
   path, with the trader's static caps still enforced there.
5. Appends every state change / opportunity / ping / park outcome to
   ``data/pm_inplay_log.jsonl`` (the post-match audit trail).

Live windows: scheduled kickoff -> +130 min wall-clock. Fixtures come from
``site/scores_markets.json`` (dates + model priors) joined with kickoff
times from ``data/pm_orderflow.db`` ``pm_markets.game_start_time``; ``--match
"Home,Away,2026-07-09T20:00Z"`` overrides/adds a fixture explicitly (use this
on matchday — the orderflow DB is only as fresh as its last ingest).

Matchday usage (MacBook, NordVPN up)::

    PYTHONPATH=src ./.venv/bin/python scripts/wca_pm_inplay_monitor.py \
        --match "France,Morocco,2026-07-09T20:00Z"           # live
    ... --paper          # pings to data/pm_inplay_pings.log, no Telegram/park
    ... --poll 30        # cadence inside a live window (default 45s)

Ctrl-C exits cleanly; restarts are idempotent (pinged keys are replayed from
the session log, the relay dedupes by proposal uid).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
for p in (str(_REPO / "src"), str(_REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from wca import inplay  # noqa: E402

# PR #179's paper harness — REUSED for jump detection, book fetch/walk and
# last-trade polling rather than duplicated (read-only public GETs only).
import wca_ladderlag_papertest as ladderlag  # noqa: E402

DEFAULT_POLL_SECS = 45.0
IDLE_SLEEP_SECS = 60.0
ORDERFLOW_DB = _REPO / "data" / "pm_orderflow.db"
SCORES_MARKETS = _REPO / "site" / "scores_markets.json"
PINGS_PAPER_LOG = _REPO / "data" / "pm_inplay_pings.log"

# Known bot proxy funder (mirrors wca.pm.trader.KNOWN_PROXY_FUNDER as a
# LITERAL — this module must never import the trader). Env overrides.
_KNOWN_FUNDER = "0x40231C7f4FC2BBAB720ce9b669eAb4795fCBE191"


def _load_dotenv(path: str = ".env") -> None:
    p = _REPO / path
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Fixture discovery (windows) — scores_markets dates + orderflow kickoffs
# ---------------------------------------------------------------------------


def _parse_kick(ts: str) -> Optional[datetime]:
    raw = (ts or "").strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.fromisoformat(raw) if fmt is None else datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def discover_fixtures(
    *,
    matches_cli: List[str],
    orderflow_db: Path = ORDERFLOW_DB,
    scores_markets: Path = SCORES_MARKETS,
) -> List[Dict[str, Any]]:
    """Fixtures with kickoff times: ``{home, away, kickoff, match_key, priors}``.

    ``--match "Home,Away,ISO-kickoff"`` entries win; otherwise unplayed games
    from site/scores_markets.json (date-level) are joined with per-match
    ``game_start_time`` from pm_orderflow.db (read-only). Fixtures with no
    resolvable kickoff timestamp are dropped (a window needs a clock).
    """
    fixtures: List[Dict[str, Any]] = []
    seen = set()

    for spec in matches_cli:
        parts = [s.strip() for s in spec.split(",")]
        if len(parts) != 3:
            print("bad --match %r (want 'Home,Away,ISO-kickoff')" % spec, file=sys.stderr)
            continue
        kick = _parse_kick(parts[2])
        if kick is None:
            print("bad kickoff in --match %r" % spec, file=sys.stderr)
            continue
        key = "%s vs %s" % (parts[0], parts[1])
        fixtures.append({"home": parts[0], "away": parts[1], "kickoff": kick,
                         "match_key": key, "priors": {}})
        seen.add(key.lower())

    # site/scores_markets.json: today's unplayed games + model priors.
    games: List[Dict[str, Any]] = []
    try:
        doc = json.loads(scores_markets.read_text())
        for grp in ("group_games", "r32_games", "r16_games", "qf_games",
                    "sf_games", "final_games"):
            games.extend(g for g in (doc.get(grp) or []) if isinstance(g, dict))
    except (OSError, json.JSONDecodeError, AttributeError):
        games = []

    kicks = _kickoffs_from_orderflow(orderflow_db)
    for g in games:
        if g.get("ft") is not None or g.get("projected"):
            continue
        home, away = str(g.get("home") or ""), str(g.get("away") or "")
        key = "%s vs %s" % (home, away)
        if not home or not away or key.lower() in seen:
            continue
        kick = kicks.get(frozenset((home.lower(), away.lower())))
        if kick is None:
            continue
        fixtures.append({
            "home": home, "away": away, "kickoff": kick, "match_key": key,
            "priors": {k: g.get(k) for k in ("x1x2", "over25", "btts", "eg")},
        })
        seen.add(key.lower())
    return fixtures


def _kickoffs_from_orderflow(db_path: Path) -> Dict[frozenset, datetime]:
    """``frozenset({home,away}) -> kickoff`` from pm_markets (read-only)."""
    import sqlite3

    if not db_path.exists():
        return {}
    out: Dict[frozenset, datetime] = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT event_title, game_start_time FROM pm_markets "
            "WHERE category='match_1x2' AND game_start_time IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    for title, gst in rows:
        kick = _parse_kick(str(gst or ""))
        head = str(title or "").replace(" vs. ", " vs ")
        parts = [p.strip() for p in head.split(" vs ")]
        if kick and len(parts) == 2:
            out.setdefault(frozenset((parts[0].lower(), parts[1].lower())), kick)
    return out


def in_live_window(fx: Dict[str, Any], now: datetime) -> bool:
    return fx["kickoff"] <= now < fx["kickoff"] + timedelta(minutes=inplay.LIVE_WINDOW_MINUTES)


# ---------------------------------------------------------------------------
# Token discovery (gamma) — BTTS/totals/exact are NOT in pm_orderflow.db
# (its ingester drops aux events by design), so they resolve via gamma here.
# ---------------------------------------------------------------------------


def _tokens_of(market: Dict[str, Any]) -> Tuple[str, str]:
    """(yes_token, no_token) from a gamma market dict ('' when unknown)."""
    from wca.data.polymarket import _parse_json_array

    ids = _parse_json_array(market.get("clobTokenIds")) or []
    outcomes = [str(o).lower() for o in (_parse_json_array(market.get("outcomes")) or [])]
    yes_idx = outcomes.index("yes") if "yes" in outcomes else 0
    no_idx = 1 - yes_idx
    yes = str(ids[yes_idx]) if yes_idx < len(ids) else ""
    no = str(ids[no_idx]) if 0 <= no_idx < len(ids) else ""
    return yes, no


def discover_match_tokens(home: str, away: str, events: List[Dict[str, Any]]) -> List[inplay.MarketToken]:
    """Resolve one live match's in-play tokens from gamma events.

    Returns MarketTokens for: 1X2 (home/away/draw YES), BTTS, totals lines
    ("more than X.5 goals"), and exact-score rows (with NO tokens for the
    impossible-row detector).
    """
    import re
    from wca.data.teamnames import canonical

    home_c, away_c = canonical(home), canonical(away)
    out: List[inplay.MarketToken] = []

    def _fixture_titled(ev) -> bool:
        head = str(ev.get("title") or "").split(" - ")[0].replace(" vs. ", " vs ")
        parts = [p.strip() for p in head.split(" vs ")]
        return len(parts) == 2 and {canonical(parts[0]), canonical(parts[1])} == {home_c, away_c}

    for ev in events:
        title = str(ev.get("title") or "")
        tl = title.lower()
        if not _fixture_titled(ev):
            continue
        markets = ev.get("markets") or []
        if " - " not in title:
            # Bare full-match event: the 1X2 markets.
            for m in markets:
                q = str(m.get("question") or "")
                git = str(m.get("groupItemTitle") or "").strip()
                yes, no = _tokens_of(m)
                if not yes:
                    continue
                if git.lower().startswith("draw") or "end in a draw" in q.lower():
                    team = "draw"
                elif git and canonical(git) == home_c:
                    team = "home"
                elif git and canonical(git) == away_c:
                    team = "away"
                else:
                    continue
                out.append(inplay.MarketToken(
                    kind="1x2", question=q, yes_token=yes, no_token=no,
                    team=team, neg_risk=bool(m.get("negRisk", False))))
        elif "both teams to score" in tl:
            for m in markets:
                yes, no = _tokens_of(m)
                if yes:
                    out.append(inplay.MarketToken(
                        kind="btts", question=str(m.get("question") or title),
                        yes_token=yes, no_token=no,
                        neg_risk=bool(m.get("negRisk", False))))
        elif "total goals" in tl or "more markets" in tl:
            for m in markets:
                q = str(m.get("question") or m.get("groupItemTitle") or "")
                mm = re.search(r"(?:more than|over)\s+(\d+(?:\.\d+)?)\s+goals", q, re.I)
                if not mm:
                    continue
                yes, no = _tokens_of(m)
                if yes:
                    out.append(inplay.MarketToken(
                        kind="total", question=q, yes_token=yes, no_token=no,
                        line=float(mm.group(1)),
                        neg_risk=bool(m.get("negRisk", False))))
        elif "exact score" in tl:
            head = title.split(" - ")[0].replace(" vs. ", " vs ")
            pm_home = head.split(" vs ")[0].strip()
            flip = canonical(pm_home) != home_c
            for m in markets:
                label = str(m.get("groupItemTitle") or m.get("question") or "")
                mm = re.search(r"(\d+)\s*[-–]\s*(\d+)", label)
                if not mm:
                    continue
                a, b = int(mm.group(1)), int(mm.group(2))
                score = (b, a) if flip else (a, b)
                yes, no = _tokens_of(m)
                if yes:
                    out.append(inplay.MarketToken(
                        kind="exact", question=str(m.get("question") or label),
                        yes_token=yes, no_token=no, score=score,
                        neg_risk=bool(m.get("negRisk", False))))
    return out


def discover_ladder_tokens(team: str, orderflow_db: Path = ORDERFLOW_DB) -> List[inplay.MarketToken]:
    """Same-team advancement rung tokens from pm_orderflow.db (read-only),
    via PR #179's discovery."""
    refs_by_team: Dict[str, list] = {}
    try:
        _, refs_by_team = ladderlag.discover_tokens_for_date(
            orderflow_db, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001 — no db on this box -> no ladder rungs
        return []
    out = []
    for ref in ladderlag.ladder_tokens_for_team(refs_by_team, team):
        if (ref.outcome or "").strip().lower() not in ("", "yes"):
            continue
        out.append(inplay.MarketToken(
            kind="ladder", question="%s — %s" % (team, ref.category),
            yes_token=ref.token_id, team=team, rung=ref.category))
    return out


# ---------------------------------------------------------------------------
# Open-exposure awareness (read-only Data API positions)
# ---------------------------------------------------------------------------


def fetch_open_positions() -> List[Any]:
    """Open PM positions for the bot funder (read-only; [] on any failure)."""
    try:
        from wca.pm.positions import fetch_positions  # read-only module (no signing)

        wallet = (os.environ.get("POLYMARKET_FUNDER") or "").strip() or _KNOWN_FUNDER
        return fetch_positions(wallet, limit=200, open_only=True)
    except Exception:  # noqa: BLE001 — exposure ping is best-effort
        return []


def positions_for_match(positions: List[Any], home: str, away: str) -> List[Any]:
    keys = (home.lower(), away.lower())
    out = []
    for p in positions:
        text = ("%s %s" % (getattr(p, "title", ""), getattr(p, "event_slug", ""))).lower()
        if any(k in text for k in keys):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class _Stop(Exception):
    pass


def _install_sigint():
    def _handler(signum, frame):  # noqa: ARG001
        raise _Stop()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


class Pinger:
    """Telegram (live) or file (paper) ping sink."""

    def __init__(self, paper: bool, paper_log: Path = PINGS_PAPER_LOG):
        self.paper = paper
        self.paper_log = paper_log
        self._client = None
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get(
            "TELEGRAM_ADMIN_USER_ID")

    def send(self, text: str) -> bool:
        if self.paper:
            try:
                self.paper_log.parent.mkdir(parents=True, exist_ok=True)
                with self.paper_log.open("a") as fh:
                    fh.write("[%s]\n%s\n\n" % (inplay.now_iso(), text))
            except OSError:
                pass
            print(text)
            return True
        try:
            if self._client is None:
                from wca.bot.telegram import TelegramClient

                self._client = TelegramClient()
            if not self.chat_id:
                print("WARNING: TELEGRAM_CHAT_ID unset — ping not sent", file=sys.stderr)
                return False
            self._client.send_message(self.chat_id, text)
            return True
        except Exception as exc:  # noqa: BLE001 — a ping failure never kills the loop
            print("telegram send failed: %s" % exc, file=sys.stderr)
            return False


class MatchWatcher:
    """Per-live-match state: tokens, price history, dedupe, last state sig."""

    def __init__(self, fx: Dict[str, Any], tokens: List[inplay.MarketToken],
                 ladder: Dict[str, List[inplay.MarketToken]]):
        self.fx = fx
        self.tokens = tokens
        self.ladder = ladder  # side ('home'/'away') -> rungs
        self.history = ladderlag.PriceHistoryBuffer()
        self.jumped_tokens: set = set()
        self.rung_pre_ref: Dict[str, float] = {}
        self.frozen_sides: set = set()  # sides whose rung refs froze at jump time
        self.last_sig: Optional[str] = None
        self.last_feed: Optional[inplay.FeedScore] = None
        self.pre_goal_1x2_mid: Dict[str, float] = {}

    def token(self, kind: str, **match) -> List[inplay.MarketToken]:
        out = []
        for t in self.tokens:
            if t.kind != kind:
                continue
            if all(getattr(t, k) == v for k, v in match.items()):
                out.append(t)
        return out


def _mid_from_book(book: Optional[dict]) -> Optional[float]:
    bids, asks = inplay.parse_book(book)
    if bids and asks:
        return (bids[0].price + asks[0].price) / 2.0
    if asks:
        return asks[0].price
    if bids:
        return bids[0].price
    return None


def run_loop(args) -> int:
    _load_dotenv(args.env)
    log_path = str(_REPO / "data" / "pm_inplay_log.jsonl")
    dedupe = inplay.DedupeRegistry(inplay.replay_pinged_keys(log_path))
    pinger = Pinger(paper=args.paper)
    ssh_relay = inplay.SshRelay()
    git_relay = inplay.GitArtifactRelay(str(_REPO))

    fixtures = discover_fixtures(matches_cli=args.match or [])
    if not fixtures:
        print("no fixtures with kickoff times found — pass --match 'Home,Away,ISO'")
        return 1
    print("monitor armed for %d fixture(s):" % len(fixtures))
    for fx in fixtures:
        print("  %s @ %s" % (fx["match_key"], fx["kickoff"].isoformat()))
    inplay.append_log({"type": "start", "fixtures": [f["match_key"] for f in fixtures],
                       "paper": args.paper}, log_path)

    watchers: Dict[str, MatchWatcher] = {}
    _install_sigint()
    try:
        while True:
            now = datetime.now(timezone.utc)
            live = [fx for fx in fixtures if in_live_window(fx, now)]
            if not live:
                nxt = min((fx["kickoff"] for fx in fixtures if fx["kickoff"] > now),
                          default=None)
                if nxt is None and all(
                    now >= fx["kickoff"] + timedelta(minutes=inplay.LIVE_WINDOW_MINUTES)
                    for fx in fixtures
                ):
                    print("all windows closed — exiting")
                    return 0
                time.sleep(IDLE_SLEEP_SECS)
                continue

            for fx in live:
                w = watchers.get(fx["match_key"])
                if w is None:
                    w = _open_watcher(fx, log_path)
                    watchers[fx["match_key"]] = w
                _poll_match(w, dedupe, pinger, ssh_relay, git_relay, log_path, args)
            time.sleep(max(30.0, min(60.0, args.poll)))
    except _Stop:
        print("\ninterrupted — state persisted in %s; restart is idempotent" % log_path)
        inplay.append_log({"type": "stop"}, log_path)
        return 0


def _open_watcher(fx: Dict[str, Any], log_path: str) -> MatchWatcher:
    print("[%s] window OPEN — discovering tokens" % fx["match_key"])
    tokens: List[inplay.MarketToken] = []
    try:
        from wca.data.polymarket import find_world_cup_markets

        events = find_world_cup_markets(include_closed=False)
        tokens = discover_match_tokens(fx["home"], fx["away"], events)
    except Exception as exc:  # noqa: BLE001
        print("gamma discovery failed for %s: %s" % (fx["match_key"], exc), file=sys.stderr)
    ladder = {
        "home": discover_ladder_tokens(fx["home"]),
        "away": discover_ladder_tokens(fx["away"]),
    }
    n_ladder = sum(len(v) for v in ladder.values())
    print("  tokens: %d match markets (%s), %d ladder rungs" % (
        len(tokens), ",".join(sorted({t.kind for t in tokens})) or "none", n_ladder))
    inplay.append_log({"type": "window_open", "match": fx["match_key"],
                       "n_tokens": len(tokens), "n_ladder": n_ladder}, log_path)
    return MatchWatcher(fx, tokens, ladder)


def _poll_match(w: MatchWatcher, dedupe: inplay.DedupeRegistry, pinger: Pinger,
                ssh_relay, git_relay, log_path: str, args) -> None:
    fx = w.fx
    now_ts = time.time()

    # --- (1) match state: feed + PM-implied ------------------------------
    feed = inplay.load_feed_scores(str(_REPO / inplay.FEED_SCORES_PATH),
                                   now_ts=now_ts).get(fx["match_key"])
    btts_mid = None
    over_mids: Dict[float, Optional[float]] = {}
    books: Dict[str, Optional[dict]] = {}

    def book_of(token_id: str) -> Optional[dict]:
        if token_id not in books:
            books[token_id] = ladderlag.fetch_raw_book(token_id)
        return books[token_id]

    for t in w.token("btts"):
        btts_mid = _mid_from_book(book_of(t.yes_token))
    for t in w.token("total"):
        over_mids[t.line] = _mid_from_book(book_of(t.yes_token))

    state = inplay.reconcile_state(fx["match_key"], feed,
                                   btts_yes_mid=btts_mid, over_mids=over_mids)
    sig = state.state_sig()
    state_changed = sig != w.last_sig
    if state_changed:
        inplay.append_log({"type": "state_change", "match": fx["match_key"],
                           "sig": sig, "conflicts": state.conflicts,
                           "feed": (feed.__dict__ if feed else None),
                           "pm_mids": state.pm_mids}, log_path)
        w.last_sig = sig

    # goal detection from the feed (for exposure impact + goal-lag note)
    goal_side = None
    if feed is not None and w.last_feed is not None:
        if feed.home_goals > w.last_feed.home_goals:
            goal_side = "home"
        elif feed.away_goals > w.last_feed.away_goals:
            goal_side = "away"

    # --- (2) open-exposure awareness on material developments ------------
    if state_changed and not args.no_exposure:
        _exposure_ping(w, state, goal_side, pinger, log_path)

    # --- (3) 1X2 jump tracking (ladder-lag triggers + goal-lag note) ------
    jumps: List[Tuple[inplay.MarketToken, Any, Any]] = []
    for t in w.token("1x2"):
        point = ladderlag.fetch_last_trade(t.yes_token)
        if point is None:
            mid = _mid_from_book(book_of(t.yes_token))
            if mid is None:
                continue
            point = ladderlag.PricePoint(ts=now_ts, price=mid, notional=None)
        w.history.add(t.yes_token, point)
        if t.yes_token not in w.jumped_tokens:
            jump = ladderlag.detect_jump(w.history.history(t.yes_token))
            if jump is not None:
                pre, post, thin = jump
                w.jumped_tokens.add(t.yes_token)
                if t.team in ("home", "away"):
                    w.frozen_sides.add(t.team)  # freeze pre-jump rung refs
                if not thin:
                    jumps.append((t, pre, post))
        # remember pre-goal mids for the goal-lag informational ping
        if goal_side is None:
            mid_now = _mid_from_book(book_of(t.yes_token))
            if mid_now is not None:
                w.pre_goal_1x2_mid[t.yes_token] = mid_now

    if goal_side is not None:
        _goal_lag_note(w, state, goal_side, pinger, dedupe, sig, log_path, book_of)

    # --- (4) opportunity scanner ------------------------------------------
    opps: List[inplay.Opportunity] = []
    for t in w.token("btts"):
        o = inplay.detect_btts(state, t, book_of(t.yes_token))
        if o:
            opps.append(o)
    for t in w.token("total"):
        o = inplay.detect_ou_over(state, t, book_of(t.yes_token))
        if o:
            opps.append(o)
    if feed is not None:
        for t in w.token("exact"):
            if t.score is None or not inplay.exact_impossible(t.score, feed):
                continue
            yes_bids, _ = inplay.parse_book(book_of(t.yes_token))
            o = inplay.detect_exact_impossible(
                state, t, book_of(t.no_token) if t.no_token else None,
                yes_bid=yes_bids[0].price if yes_bids else None)
            if o:
                opps.append(o)
        for t in w.token("1x2"):
            o = inplay.detect_ft_winner(state, t, book_of(t.yes_token))
            if o:
                opps.append(o)
    for t, pre, post in jumps:
        side = t.team if t.team in ("home", "away") else None
        if side is None:
            continue
        team_name = fx[side]
        for rung in w.ladder.get(side, []):
            # Pre-jump reference only (frozen at jump time). If we never saw
            # the rung before the jump we cannot claim its quote is stale.
            ref = w.rung_pre_ref.get(rung.yes_token)
            o = inplay.detect_ladder_lag(
                state, rung, book_of(rung.yes_token), trigger_team=team_name,
                jump_pre=pre.price, jump_post=post.price, rung_pre_ref=ref)
            if o:
                opps.append(o)

    # --- (5) ping + park (deduped) ----------------------------------------
    for opp in opps:
        if not dedupe.should_ping(opp, sig):
            continue
        dedupe.mark(opp, sig)
        inplay.append_log({"type": "opportunity", "match": fx["match_key"],
                           "dedupe_key": opp.dedupe_key(sig),
                           "opportunity": inplay.to_parked_proposal(opp),
                           "edge": opp.edge, "reason": opp.reason}, log_path)
        proposal = inplay.to_parked_proposal(opp)
        if args.paper:
            relay_res = inplay.PaperRelay().park(proposal)
        else:
            relay = inplay.select_relay(ssh_relay, git_relay)
            relay_res = relay.park(proposal)
        text = inplay.format_opportunity_ping(
            opp, relay_name=relay_res.relay if relay_res.ok else
            ("%s failed: %s" % (relay_res.relay, relay_res.detail)),
            pm_token=relay_res.pm_token, conflicts=state.conflicts)
        sent = pinger.send(text)
        inplay.append_log({"type": "ping", "match": fx["match_key"],
                           "dedupe_key": opp.dedupe_key(sig), "sent": sent,
                           "relay": relay_res.relay, "relay_ok": relay_res.ok,
                           "relay_detail": relay_res.detail,
                           "pm_token": relay_res.pm_token,
                           "uid": opp.uid}, log_path)

    # --- (6) refresh PRE-jump rung references for the NEXT poll ------------
    # Done at the END of the poll so that when a jump is detected in poll N,
    # the staleness comparison uses the mark from poll N-1 (genuinely
    # pre-jump, at most one poll old) — the same-poll book fetch is already
    # post-jump and would flag "stale" tautologically. Frozen sides (jump
    # already fired) keep their last pre-jump marks. NOTE: fresh fetches, not
    # this poll's cache, would be wasteful — the ≤poll-cadence granularity is
    # accepted and documented.
    for side in ("home", "away"):
        if side in w.frozen_sides:
            continue
        for rung in w.ladder.get(side, []):
            mid = _mid_from_book(book_of(rung.yes_token))
            if mid is not None:
                w.rung_pre_ref[rung.yes_token] = mid

    w.last_feed = feed if feed is not None else w.last_feed


def _exposure_ping(w: MatchWatcher, state: inplay.MatchState,
                   goal_side: Optional[str], pinger: Pinger, log_path: str) -> None:
    fx = w.fx
    positions = positions_for_match(fetch_open_positions(), fx["home"], fx["away"])
    if not positions:
        return
    scoring_team = fx[goal_side] if goal_side in ("home", "away") else ""
    other_team = fx["away" if goal_side == "home" else "home"] if goal_side else ""
    rows = []
    for p in positions:
        rows.append({
            "title": getattr(p, "title", "?"),
            "outcome": getattr(p, "outcome", ""),
            "size": getattr(p, "size", 0.0),
            "avg_price": getattr(p, "avg_price", 0.0),
            "cur_price": getattr(p, "cur_price", 0.0),
            "impact": inplay.classify_impact(
                getattr(p, "title", ""), getattr(p, "outcome", ""),
                scoring_team, other_team) if goal_side else "state change",
        })
    text = inplay.format_exposure_ping(fx["match_key"], state, rows)
    sent = pinger.send(text)
    inplay.append_log({"type": "exposure_ping", "match": fx["match_key"],
                       "n_positions": len(rows), "sent": sent}, log_path)


def _goal_lag_note(w: MatchWatcher, state: inplay.MatchState, goal_side: str,
                   pinger: Pinger, dedupe: inplay.DedupeRegistry, sig: str,
                   log_path: str, book_of) -> None:
    """Informational ping (never parked): a goal per the feed with the scoring
    team's 1X2 quote barely moved — fair value in-play is model-dependent, so
    this is surfaced for the human, not auto-priced."""
    fx = w.fx
    for t in w.token("1x2", team=goal_side):
        pre = w.pre_goal_1x2_mid.get(t.yes_token)
        mid = _mid_from_book(book_of(t.yes_token))
        if pre is None or mid is None or (mid - pre) >= inplay.GOAL_LAG_MIN_MOVE:
            continue
        key = "goal_lag:%s:%s:%s" % (fx["match_key"], t.yes_token, sig)
        if key in dedupe._seen:  # noqa: SLF001 — same registry, simple key
            continue
        dedupe._seen.add(key)  # noqa: SLF001
        team = fx[goal_side]
        text = ("ℹ️ *IN-PLAY note — %s*: %s scored (%s [feed]) but their win quote "
                "moved only %+.1fc (%s→%s). No park — in-play fair value is "
                "model-dependent; review manually. settles: 90-min basis"
                % (fx["match_key"], team, _state(state), (mid - pre) * 100,
                   inplay._cents(pre), inplay._cents(mid)))
        pinger.send(text)
        inplay.append_log({"type": "ping", "match": fx["match_key"],
                           "dedupe_key": key, "sent": True,
                           "relay": "none", "relay_ok": True,
                           "relay_detail": "informational only"}, log_path)


def _state(state: inplay.MatchState) -> str:
    return state.feed.scoreline if state.feed else "?"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="PM in-play monitor (never places orders; parks + pings only)")
    ap.add_argument("--match", action="append", default=[],
                    help="explicit fixture: 'Home,Away,2026-07-09T20:00Z' (repeatable)")
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL_SECS,
                    help="in-window poll cadence seconds, clamped 30-60 (default 45)")
    ap.add_argument("--paper", action="store_true",
                    help="log pings to data/pm_inplay_pings.log instead of Telegram; "
                         "park intents are logged, nothing is relayed")
    ap.add_argument("--no-exposure", action="store_true",
                    help="skip the open-exposure MTM pings (detectors only)")
    ap.add_argument("--env", default=".env", help="dotenv file to load")
    args = ap.parse_args(argv)
    return run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
