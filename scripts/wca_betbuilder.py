#!/usr/bin/env python3
"""Build the bet-builder card: team totals + player props per upcoming fixture.

Prices the classic bet-builder surface (team totals goals/shots/SoT/fouls/
corners, match cards, player SoT/fouls/to-be-booked, player to score) from the
team/player model, using the Dixon-Coles ``lambda`` per team that the card
build already persisted to ``data/model_predictions.json``.

Sourcing is offline (predictions snapshot + ``data/players.json`` overrides +
optional ``players.db`` rates) so it runs on cron without a live odds pull.
Outputs:
    data/betbuilder_latest.md    (cache the bot's /betbuilder reads)
    data/betbuilder_latest.json  (machine-readable full payload)

Honesty: SoT/cards/corners/fouls are sportsbook-only and the project has no
sportsbook odds feed beyond TheOddsAPI player props, so these are model FAIR
odds — decision support, not a priced/EV'd edge.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wca import cardcache  # noqa: E402
from wca.models.betbuilder import RateStore, fixture_betbuilder  # noqa: E402
from wca.models.scorers import load_player_overrides  # noqa: E402
from wca.selection import hours_out as _sel_hours_out  # noqa: E402


def _load_fixtures(preds_json: str):
    if not os.path.exists(preds_json):
        return []
    with open(preds_json, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for f in data.get("fixtures", []):
        lam_h, lam_a = f.get("lambda_home"), f.get("lambda_away")
        fx = f.get("fixture", "")
        if lam_h is None or lam_a is None or " vs " not in fx:
            continue
        home, away = fx.split(" vs ", 1)
        out.append((home.strip(), away.strip(), float(lam_h), float(lam_a),
                    f.get("kickoff", "")))
    return out


def _render_md(payloads, generated: str) -> str:
    L = ["*Bet builder* — model fair odds (sportsbook markets; model-only)\n"]
    for p in payloads:
        L.append(f"*{p['fixture']}*  (λ {p['lambda_home']:.2f}–{p['lambda_away']:.2f})")
        # team totals: show goals + SoT lines compactly
        tt = {}
        for row in p["team_totals"]:
            tt.setdefault(row["subject"], {}).setdefault(row["market"], []).append(row)
        for team, markets in tt.items():
            goals = markets.get("team_total_goals", [])
            sot = markets.get("team_total_sot", [])
            g = ", ".join(f"{r['line']:.1f}:{r['fair_over']}" for r in goals if r['fair_over'])
            s = ", ".join(f"{r['line']:.1f}:{r['fair_over']}" for r in sot if r['fair_over'])
            L.append(f"  {team} goals O[{g}] · SoT O[{s}]")
        # top scorers
        for sc in p["player_to_score"][:4]:
            fa = sc.get("fair_anytime")
            L.append(f"  ⚽ {sc['subject']} anytime {fa if fa else '—'} "
                     f"(p={sc['p_anytime']:.2f})")
        # player props (booked)
        booked = [pp for pp in p["player_props"] if pp["market"] == "player_to_be_booked"]
        for b in booked[:4]:
            L.append(f"  🟨 {b['subject']} booked {b['fair']} (p={b['prob']:.2f})")
        L.append("")
    L.append("_Fair odds only — no margin. Compare to the book's price before "
             "staking; SoT/cards/corners not on the exchange._")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preds", default="data/model_predictions.json")
    ap.add_argument("--players", default="data/players.json")
    ap.add_argument("--players-db", default="data/players.db")
    ap.add_argument("--out-md", default="data/betbuilder_latest.md")
    ap.add_argument("--out-json", default="data/betbuilder_latest.json")
    ap.add_argument("--max-fixtures", type=int, default=8)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Canonical selection rule (wca.selection): FURTHER-OUT fixtures first, so the
    # --max-fixtures cap drops the imminent (thin-edge) fixtures, not the distant
    # (more-likely-mispriced) ones. `_load_fixtures` tuples carry kickoff at [4].
    # (Betbuilder emits fair odds only — no stakes here — so this is ordering, not
    # sizing; its scorer/prop legs are structurally <25c decision-support/no-cash.)
    fixtures = sorted(
        _load_fixtures(args.preds),
        key=lambda fx: _sel_hours_out({"match_desc": "_"}, {"_": fx[4]}),
        reverse=True,
    )[: args.max_fixtures]
    overrides = load_player_overrides(args.players)
    store = RateStore(args.players_db if os.path.exists(args.players_db) else None)

    payloads = []
    for home, away, lam_h, lam_a, kickoff in fixtures:
        pay = fixture_betbuilder(home, away, lam_h, lam_a, store=store, scorers=overrides)
        pay["kickoff"] = kickoff
        payloads.append(pay)

    md = _render_md(payloads, generated)
    cardcache.write_card(md, path=args.out_md, ts_utc=generated)
    d = os.path.dirname(args.out_json)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump({"generated": generated, "fixtures": payloads}, fh, indent=2)

    if not args.quiet:
        print(md)
        print(f"\n[wrote {args.out_md} and {args.out_json}; {len(payloads)} fixtures]")


if __name__ == "__main__":
    main()
