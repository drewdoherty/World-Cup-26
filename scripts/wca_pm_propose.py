"""CLI: produce Polymarket parked-order proposals from tonight's card.

Pipeline
--------
1. Load ``.env`` (ODDS_API_KEY, TELEGRAM_*, POLYMARKET_FUNDER, ...).
2. Fit the Elo + Dixon-Coles models on the results history.
3. Pull live World Cup h2h odds and keep the next ``--hours-ahead`` window.
4. Build Polymarket-pool proposals (:func:`wca.pm.propose.build_pm_proposals`),
   resolving each card pick to a live Polymarket YES token + price.
5. For each proposal: park it via :func:`wca.bot.app.push_parked_order` (which
   returns the ``PM-<n>`` confirmation text) and send that text to
   ``TELEGRAM_ADMIN_USER_ID`` via :class:`wca.bot.telegram.TelegramClient`.

This script NEVER places an order. It only parks proposals and notifies the
admin; execution stays behind the bot's ``Y PM-<n>`` confirmation gate and the
``PM_DRY_RUN`` flag. Use ``--dry-print`` to inspect the proposals (and the
resolved token ids / prices) without touching Telegram.

Usage::

    python scripts/wca_pm_propose.py --dry-print
    python scripts/wca_pm_propose.py            # parks + notifies the admin
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

# Polymarket pool bankroll in USDC (project charter: $2,500 quarter-Kelly).
# The funder fallback (known proxy, never the empty EOA) lives in
# wca.pm.trader.resolve_funder_from_env so the producer and the bot agree.
_DEFAULT_POOL_USD = 2500.0


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (same pattern as the other scripts); never echoes values."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _resolve_funder() -> str:
    """Return POLYMARKET_FUNDER, warning + falling back to the known proxy.

    Delegates to :func:`wca.pm.trader.resolve_funder_from_env` so the producer
    and the bot share one fallback (the proxy, never the empty EOA).
    """
    from wca.pm.trader import resolve_funder_from_env

    funder, _sig_type, used_fallback = resolve_funder_from_env()
    if used_fallback:
        print(
            "WARNING: POLYMARKET_FUNDER not set — falling back to the known "
            "Polymarket proxy %s. USDC sits in the proxy, never the EOA; set "
            "POLYMARKET_FUNDER in .env to silence this." % funder,
            file=sys.stderr,
        )
    return funder


def _augment_for_gate(proposal: dict) -> dict:
    """Add the bot-gate keys (``size`` = shares, ``label``) to a proposal.

    The bot's park/execute gate sizes the order in *shares* (it computes the USD
    notional as ``price * size`` and passes ``size`` straight to
    ``place_order``), and renders a human label from ``proposal['label']``. The
    producer emits ``shares`` / ``size_usd``; mirror ``shares`` onto ``size``
    and derive a label so the parked order both executes and reads correctly.
    """
    p = dict(proposal)
    p["size"] = float(proposal["shares"])  # gate sizes in shares
    home, _, _ = str(proposal.get("match_desc", "")).partition(" vs ")
    # A compact label: the question is the most informative human string.
    p["label"] = proposal.get("market_question") or proposal.get("match_desc") or "market"
    return p


def _format_proposal_line(i: int, p: dict) -> str:
    from wca.bot.app import describe_pm_selection

    match_desc = p.get("match_desc", "")
    market_q = (p.get("market_question") or "").strip()
    outcome = p.get("outcome") or "?"
    backing = describe_pm_selection(p)
    qline = ("\n    %s → %s" % (market_q, outcome)) if market_q else ""
    return (
        "*%d. %s* — backing %s%s\n"
        "    @ %.2f | $%.2f | model %.1f%% | ev %+.1f%%%s"
        % (
            i,
            match_desc,
            backing,
            qline,
            p["price"],
            p["size_usd"],
            p["model_prob"] * 100.0,
            p["ev"] * 100.0,
            " [neg_risk]" if p.get("neg_risk") else "",
        )
    )


def _next_5_matches(odds_df, now_dt) -> list:
    """Return up to the 5 soonest distinct fixtures at/after ``now_dt``.

    Each item is ``(home_team, away_team, commence_time)``. ``now_dt`` is a
    naive-UTC datetime (as used elsewhere in this script).
    """
    import pandas as pd

    if odds_df is None or odds_df.empty or "commence_time" not in odds_df.columns:
        return []
    df = odds_df.copy()
    ct = pd.to_datetime(df["commence_time"], errors="coerce", utc=True)
    df["_ct"] = ct
    now = pd.Timestamp(now_dt, tz="UTC")
    df = df[df["_ct"] >= now]
    df = df.sort_values("_ct").drop_duplicates(subset=["home_team", "away_team"], keep="first")
    out = []
    for _, r in df.head(5).iterrows():
        home = str(r.get("home_team") or "").strip()
        away = str(r.get("away_team") or "").strip()
        if home and away:
            out.append((home, away, r["_ct"]))
    return out


def _outcome_token(sel: str, home: str, away: str) -> str:
    """Normalise a selection/outcome to 'HOME' | 'AWAY' | 'DRAW' | canonical name."""
    from wca.data.teamnames import canonical

    s = (sel or "").strip()
    if s.lower() in ("draw", "tie", "x"):
        return "DRAW"
    cs = canonical(s)
    if cs == canonical(home):
        return "HOME"
    if cs == canonical(away):
        return "AWAY"
    return cs


# 1X2 result tokens — the only exposure a result/1X2 Polymarket bet can offset.
_RESULT_TOKENS = ("HOME", "DRAW", "AWAY")


def _pm_result_token(p: dict, home: str, away: str):
    """The 1X2 result token a Polymarket BUY backs ('HOME'/'DRAW'/'AWAY'), or
    ``None`` if it is not a clean home/draw/away *result* bet.

    A PM outcome is a bare Yes/No against a market *question*, so the real
    selection lives in ``market_question`` — not ``outcome`` (which is just
    'Yes'). Parsed exactly like :func:`wca.bot.app.describe_pm_selection`:
    'Will X win? Yes' -> X's token, 'end in a draw? Yes' -> DRAW. A No-side or a
    prop/non-result question returns None, so a result bet is never mislabelled
    a 1X2 hedge/add of an unrelated market (the McTominay-prop bug).
    """
    import re

    q = (p.get("market_question") or p.get("label") or "").strip()
    outcome = (p.get("outcome") or p.get("selection") or "").strip().lower()
    if outcome not in ("yes", "y", "true"):
        return None
    ql = q.lower()
    if "draw" in ql or "tie" in ql:
        return "DRAW"
    m = re.search(r"will\s+(.+?)\s+win\b", q, re.IGNORECASE)
    if m:
        tok = _outcome_token(m.group(1).strip().rstrip("?").strip(), home, away)
        return tok if tok in _RESULT_TOKENS else None
    return None


def _reset_parked_for_new_batch(db_path: str):
    """Clear the parked queue so a fresh batch numbers from ``PM-1`` again.

    The ``pm_parked`` table is only a handshake queue — every executed order is
    audited in ``bets`` + ``pm_order_log``, not here — so it is safe to wipe and
    reset its autoincrement before parking a new batch. We DELETE the rows and
    reset ``sqlite_sequence`` so the next INSERT gets ``n=1``.

    Safety: if any ``unconfirmed`` row exists (a live order that may be on-chain
    but is not yet reconciled), we must NOT reuse its ``PM-<n>``. In that case we
    fall back to merely *expiring* the parked rows (numbering continues upward),
    so a possibly-live order's token can never collide with a new proposal.

    Returns ``(n_cleared, did_reset)``. ``did_reset`` is ``True`` only when the
    wipe actually committed (verified by re-reading the row count), so a transient
    lock can never make us *claim* a reset that did not happen.
    """
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        # Wait up to 10s for any concurrent writer (e.g. an odds-snapshot insert)
        # rather than failing the wipe outright — a swallowed lock would silently
        # leave the old autoincrement and break count-from-1.
        con.execute("PRAGMA busy_timeout=10000")
        has = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pm_parked'"
        ).fetchone()
        if not has:
            return (0, True)  # fresh table: first insert is n=1 anyway
        n_parked = con.execute(
            "SELECT COUNT(*) FROM pm_parked WHERE status='parked'"
        ).fetchone()[0]
        unconf = con.execute(
            "SELECT COUNT(*) FROM pm_parked WHERE status='unconfirmed'"
        ).fetchone()[0]
        if unconf:
            con.execute("UPDATE pm_parked SET status='expired' WHERE status='parked'")
            con.commit()
            return (n_parked, False)
        con.execute("DELETE FROM pm_parked")
        seq_tbl = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if seq_tbl:
            con.execute("DELETE FROM sqlite_sequence WHERE name='pm_parked'")
        con.commit()
        # Verify the wipe really took (no rows left) before claiming a reset.
        remaining = con.execute("SELECT COUNT(*) FROM pm_parked").fetchone()[0]
        return (n_parked, remaining == 0)
    except sqlite3.OperationalError:
        # Persistent lock (e.g. a concurrent propose run): degrade gracefully —
        # don't crash the send and don't claim a reset that didn't happen.
        return (0, False)
    finally:
        con.close()


def _build_exposure_section(proposals: list, odds_df, db_path: str, now_dt) -> str:
    """Exposure + hedge header across ALL fixtures with a Polymarket edge or open
    sportsbook exposure (no longer limited to the next 5 games).

    Uses the *decomposed* ledger exposure (``decompose_multileg=True``) so
    accumulators / bet-builders are surfaced leg-by-leg and no open bet is
    missed. Money is shown in its real currency ($ for Polymarket/Kalshi, £ for
    sportsbooks). Polymarket proposals are classified against existing 1X2
    result exposure:

    * 🛡 HEDGE — a result market on a *different* outcome than one we already
      back (wins when that bet loses). Acca/builder result legs count too, but a
      single-leg hedge only *partially* offsets an all-or-nothing multi.
    * ➕ ADD — same result side we already back (raises exposure).
    * ✅ EV pick — no result exposure to offset.
    Non-result exposure (props, handicaps, scorers, corners…) is listed for
    awareness; a 1X2 bet does NOT hedge it.
    """
    from wca.data.teamnames import canonical
    from wca.ledger.reports import sportsbook_open_exposure_by_match, currency_symbol
    from wca.bot.app import describe_pm_selection

    try:
        exposure = sportsbook_open_exposure_by_match(
            db_path, sources=None, decompose_multileg=True
        )
    except Exception:
        exposure = {}

    props_by_key: dict = {}
    title_by_key: dict = {}
    ha_by_key: dict = {}
    for n, p in enumerate(proposals, 1):
        md = str(p.get("match_desc") or "")
        parts = [x.strip() for x in md.split(" vs ")]
        if len(parts) != 2:
            continue
        key = frozenset(canonical(x) for x in parts)
        props_by_key.setdefault(key, []).append((n, p))
        title_by_key.setdefault(key, "%s vs %s" % (parts[0], parts[1]))
        ha_by_key.setdefault(key, (parts[0], parts[1]))

    for key, e in exposure.items():
        md = e.get("match_desc", "")
        if key not in ha_by_key and md and "|" not in md and " vs " in md:
            h, _, a = md.partition(" vs ")
            ha_by_key[key] = (h.strip(), a.strip())
            title_by_key.setdefault(key, "%s vs %s" % (h.strip(), a.strip()))
        title_by_key.setdefault(key, "/".join(sorted(key)))

    if not props_by_key and not exposure:
        return ""

    def _res_token(name):
        s = (name or "").strip().lower()
        return "DRAW" if s in ("draw", "the draw") else canonical(name)

    def _key_rank(key):
        ps = props_by_key.get(key, [])
        best = max((float(p.get("ev", 0.0)) for _n, p in ps), default=None)
        return (0, -best) if best is not None else (1, 0.0)

    ordered = sorted(set(props_by_key) | set(exposure), key=_key_rank)

    lines = ["🎯 *Exposure & hedges — all opportunities:*", ""]
    saw_leg_hedge = False
    for key in ordered:
        title = title_by_key.get(key, "/".join(sorted(key)))
        e = exposure.get(key)
        mprops = props_by_key.get(key, [])
        home, away = ha_by_key.get(key, (None, None))

        backed: set = set()
        res_cells, nonres_cells = [], []

        if e:
            for sel, oc in e["outcomes"].items():
                sym = currency_symbol(oc.get("currency"))
                plats = "/".join(sorted(p for p in oc.get("platforms", set()) if p)) or "?"
                tok = _outcome_token(sel, home or "", away or "")
                if tok in _RESULT_TOKENS:
                    label = {"HOME": home, "AWAY": away, "DRAW": "Draw"}.get(tok) or sel
                    backed.add(_res_token(label))
                    res_cells.append("%s %s%.0f@risk [%s]" % (label, sym, oc["risk"], plats))
                else:
                    nonres_cells.append("%s %s%.0f@risk [%s]" % (sel[:24], sym, oc["risk"], plats))
            for leg in e.get("legs", []):
                sym = currency_symbol(leg.get("currency"))
                bt = leg.get("bet_type", "multi")
                if leg.get("is_result"):
                    backed.add(_res_token(leg["team"]))
                    res_cells.append("%s %s%.0f@risk [%s leg]" % (leg["team"][:18], sym, leg["risk"], bt))
                else:
                    nonres_cells.append("%s %s%.0f@risk [%s leg]" % (leg["selection"][:22], sym, leg["risk"], bt))

        if res_cells:
            hdr = "⚠️ %s — result exposure: %s" % (title, ", ".join(res_cells))
            if nonres_cells:
                hdr += "  | non-result (1X2 does NOT hedge): %s" % "; ".join(nonres_cells)
            lines.append(hdr)
        elif nonres_cells:
            lines.append("ℹ️ %s — open (non-result, 1X2 does NOT hedge): %s"
                         % (title, "; ".join(nonres_cells)))
        elif mprops:
            lines.append("✅ %s — EV picks (no existing exposure):" % title)
        else:
            continue

        for n, p in sorted(mprops, key=lambda np: -float(np[1].get("ev", 0.0))):
            sel = describe_pm_selection(p)
            tok = _pm_result_token(p, home or "", away or "")
            body = "PM-%d %s @ %.2f (ev %+.1f%%, $%.0f)" % (
                n, sel, p["price"], float(p.get("ev", 0.0)) * 100.0, p["size_usd"],
            )
            if tok in _RESULT_TOKENS and backed:
                ptok = {"HOME": _res_token(home), "AWAY": _res_token(away), "DRAW": "DRAW"}.get(tok)
                if ptok in backed:
                    lines.append("    ➕ ADD %s — same result side, raises exposure" % body)
                else:
                    has_leg = bool(e and any(l.get("is_result") for l in e.get("legs", [])))
                    saw_leg_hedge = saw_leg_hedge or has_leg
                    note = " (partial — multi is all-or-nothing)" if has_leg else ""
                    lines.append("    🛡 HEDGE %s — offsets %s%s"
                                 % (body, "/".join(sorted(backed)), note))
            else:
                lines.append("    ✅ %s" % body)
        if not mprops and res_cells:
            lines.append("    (no Polymarket market resolved to hedge this)")

    if saw_leg_hedge:
        lines.append("")
        lines.append("_⚠️ Acca/builder legs are all-or-nothing: a single-leg hedge "
                     "only partially offsets the multi._")
    return "\n".join(lines) + "\n"


def _unfilled_orders_section(proposals: list, db_path: str) -> str:
    """Best-effort "Unfilled PM orders" section for the proposal message.

    Pulls the account's live open orders + books and renders each with its
    %-off-market and age via :mod:`wca.pm.redeem`.  Any failure (no key, CLOB
    unreachable, region block) returns ``""`` so the proposal message still
    sends — the unfilled list is informational, never load-bearing.
    """
    try:
        import datetime as _dt

        from wca.pm import redeem as redeem_core
        from wca.pm.trader import ClobTrader, resolve_funder_from_env

        key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        if not key:
            return ""
        funder, sig_type, _ = resolve_funder_from_env()
        trader = ClobTrader(key, funder=funder, signature_type=sig_type)
        orders = trader.open_orders()
        if not orders:
            return ""

        # token -> human label from the current proposals.
        tok_label = {}
        for p in proposals:
            tok = str(p.get("token_id") or "")
            if tok:
                tok_label[tok] = "%s — %s" % (p.get("match_desc", ""), p.get("outcome", ""))

        label_by_id, books = {}, {}
        for o in orders:
            tok = str(o.get("asset_id") or o.get("token_id") or "")
            oid = redeem_core.order_id_of(o)
            if oid and tok in tok_label:
                label_by_id[oid] = tok_label[tok]
            if tok and tok not in books:
                try:
                    books[tok] = trader.get_order_book(tok)
                except Exception:  # noqa: BLE001 — a missing book just omits %-off
                    books[tok] = None

        now_epoch = _dt.datetime.now(_dt.timezone.utc).timestamp()
        return redeem_core.format_unfilled_orders(
            orders, books, now_epoch,
            label_by_id=label_by_id,
            log_epoch_by_id=redeem_core.log_epoch_by_id(db_path),
        )
    except Exception:  # noqa: BLE001 — never block the proposal message
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Produce Polymarket parked-order proposals from the card."
    )
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument(
        "--hours-ahead",
        type=float,
        default=720.0,
        help="Include fixtures starting within this many hours (default 720 = "
             "30 days, i.e. ALL upcoming moneyline opportunities; lower it to "
             "restrict to the near term)",
    )
    parser.add_argument(
        "--regions",
        default="uk",
        help="Comma-separated Odds API regions (default: uk)",
    )
    parser.add_argument(
        "--pool-usd",
        type=float,
        default=_DEFAULT_POOL_USD,
        help="Polymarket pool bankroll in USDC (default 2500)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.02,
        help="Minimum sportsbook edge to surface a selection (default 0.02)",
    )
    parser.add_argument(
        "--max-order-usd",
        type=float,
        default=30.0,
        help="Absolute per-order USD ceiling (default 30)",
    )
    parser.add_argument(
        "--dry-print",
        action="store_true",
        help="Print proposals (with resolved token ids) without pushing to Telegram",
    )
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)

    funder = _resolve_funder()

    now_dt = datetime.datetime.utcnow()
    cutoff_dt = now_dt + datetime.timedelta(hours=args.hours_ahead)

    # Heavy imports after arg parsing so --help stays fast.
    try:
        from wca.data.results import load_results  # type: ignore[attr-defined]
        from wca.data.cleaning import resolve_results_path
        from wca.card import fit_models
        from wca.data import theoddsapi
        from wca.pm.propose import build_pm_proposals
    except ImportError as exc:
        print("ERROR: could not import wca pipeline modules: %s" % exc, file=sys.stderr)
        return 1

    # -- models -----------------------------------------------------------
    try:
        results = load_results(resolve_results_path())
        models = fit_models(results)
    except Exception as exc:
        print("ERROR: model fitting failed: %s" % exc, file=sys.stderr)
        return 1

    # -- odds -------------------------------------------------------------
    try:
        odds_df, quota = theoddsapi.get_odds(
            "soccer_fifa_world_cup", regions=args.regions, markets="h2h"
        )
    except Exception as exc:
        print("ERROR: odds pull failed: %s" % exc, file=sys.stderr)
        return 1

    import pandas as pd

    if not odds_df.empty and "commence_time" in odds_df.columns:
        ct = pd.to_datetime(odds_df["commence_time"], errors="coerce", utc=True)
        ct_naive = ct.dt.tz_localize(None) if ct.dt.tz is None else ct.dt.tz_convert(None)
        mask = (ct_naive >= now_dt) & (ct_naive <= cutoff_dt)
        odds_df = odds_df[mask].copy()

    # -- proposals --------------------------------------------------------
    try:
        proposals = build_pm_proposals(
            models,
            odds_df,
            fixtures_meta=results,
            pool_usd=args.pool_usd,
            min_edge=args.min_edge,
            max_order_usd=args.max_order_usd,
        )
    except Exception as exc:
        print("ERROR: proposal build failed: %s" % exc, file=sys.stderr)
        return 1

    total_size = sum(p["size_usd"] for p in proposals)
    print(
        "Funder (maker): %s | pool $%.0f | %d proposal(s), total $%.2f"
        % (funder, args.pool_usd, len(proposals), total_size)
    )
    quota_str = (
        "quota remaining=%s" % quota.remaining
        if quota is not None and getattr(quota, "remaining", None) is not None
        else "quota=unknown"
    )
    print("Odds %s" % quota_str)

    if not proposals:
        print("No proposals to park (no +EV pick resolved to a live token).")
        return 0

    if args.dry_print:
        exposure_section = _build_exposure_section(
            proposals, odds_df, args.db, now_dt
        )
        if exposure_section:
            print("\n" + exposure_section)
        print("-- proposals (dry-print; nothing parked or sent) --")
        for i, p in enumerate(proposals, 1):
            print(_format_proposal_line(i, p))
        return 0

    # -- park + notify (single message) -----------------------------------
    from wca.bot.app import push_parked_order
    from wca.bot.telegram import TelegramClient, TelegramError

    admin = os.environ.get("TELEGRAM_ADMIN_USER_ID")
    if not admin:
        print(
            "ERROR: TELEGRAM_ADMIN_USER_ID not set — cannot notify. "
            "Proposals not parked.",
            file=sys.stderr,
        )
        return 1

    try:
        client = TelegramClient()
    except TelegramError as exc:
        print("ERROR: Telegram client init failed: %s" % exc, file=sys.stderr)
        return 1

    # Clear the previous batch BEFORE parking the new one, so (a) a stale
    # proposal for an already-played match can never be executed via a leftover
    # `Y PM-<n>`, and (b) this batch numbers from PM-1 again. Skips the reset
    # (expire-only) if an unconfirmed/possibly-on-chain order is in flight.
    n_cleared, did_reset = _reset_parked_for_new_batch(args.db)
    if n_cleared or did_reset:
        print(
            "Cleared %d stale parked proposal(s); PM numbering %s."
            % (n_cleared, "reset to 1" if did_reset else "continues (in-flight order)")
        )

    # Park all proposals first
    parked_texts = []
    for p in proposals:
        parked_texts.append(push_parked_order(_augment_for_gate(p)))

    # Build single message with exposure + all proposals
    exposure_section = _build_exposure_section(proposals, odds_df, args.db, now_dt)

    message_body = "🎯 *Polymarket Trade Ideas* — %d picks\n\n" % len(proposals)
    if exposure_section:
        message_body += exposure_section + "\n"

    message_body += "*Proposals:*\n"
    for i, text in enumerate(parked_texts, 1):
        message_body += "\n" + text

    # Resting orders from earlier batches that are still unfilled — shown with
    # %-off-market + age so the user can let them ride or redeem instantly.
    unfilled_section = _unfilled_orders_section(proposals, args.db)
    if unfilled_section:
        message_body += "\n\n" + unfilled_section

    message_body += (
        "\n\n_PM_DRY_RUN gates execution. Confirm each with `Y PM-<n>`. "
        "Unfilled orders auto-redeem after 24h; `REDEEM ALL` / `REDEEM <id>` to cancel now._"
    )

    # Send — split on paragraph boundaries if over Telegram's 4096-char limit
    # (proposing ALL moneyline opportunities can exceed a single message).
    try:
        for part in _split_for_telegram(message_body):
            client.send_message(admin, part)
        print(
            "Parked + notified %d proposal(s) to admin %s."
            % (len(proposals), admin)
        )
    except TelegramError as exc:
        print("send error: %s" % exc, file=sys.stderr)
        return 1

    return 0


def _split_for_telegram(text: str, limit: int = 4000):
    """Yield message chunks ≤ ``limit`` chars, splitting on blank lines (then
    single newlines, then hard) so a long proposal/exposure block is delivered
    intact across multiple Telegram messages."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for block in text.split("\n\n"):
        piece = (buf + "\n\n" + block) if buf else block
        if len(piece) <= limit:
            buf = piece
            continue
        if buf:
            parts.append(buf)
            buf = ""
        if len(block) <= limit:
            buf = block
            continue
        # A single block still too long: split on newlines, then hard-cut.
        for line in block.split("\n"):
            cand = (buf + "\n" + line) if buf else line
            if len(cand) <= limit:
                buf = cand
            else:
                if buf:
                    parts.append(buf)
                while len(line) > limit:
                    parts.append(line[:limit])
                    line = line[limit:]
                buf = line
    if buf:
        parts.append(buf)
    return parts


if __name__ == "__main__":
    sys.exit(main())
