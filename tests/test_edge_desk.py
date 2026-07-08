"""Tests for the SHADOW-ONLY Advancement Edge Desk (scripts/wca_edge_desk.py).

All tests are offline: the five input feeds come from JSON fixtures in
tests/fixtures/edge_desk/ (happy path) plus in-memory mutations for the edge
cases (freshness boundaries incl. the model stamp + PM-blind marker, projected
vs real deciding ties, side-attribution uncertainty, withheld near-misses,
decided-but-actionable legs, negative edge + hot orderflow, longshot cash
rule).

Fixture verdict map (FRESH_NOW clock) — every 4-label path is exercised:

  SHADOW_ADD    Morocco/SF (real QF tie), Brazil/QF (bet_recs YES buy)
  WATCH         Nordland/SF (side_attribution_uncertain — reviewer scenario),
                Bergland/SF (projected deciding tie), Midland/SF
                (near-threshold edge), Colombia/QF (negative edge + HOT flow),
                Morocco/group_winner (projected group tie),
                Morocco/Final + Switzerland/SF (longshot_no_cash)
  WITHHOLD      Doneland/QF (decided leg still actionable in bet_recs),
                Morocco 1X2 near-miss (bet_recs.withheld)
  DO_NOT_TRADE  Coldland/SF (negative edge, flow not hot)
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "edge_desk")
# Injected clock ~1h after the fixture stamps → every freshness check passes.
FRESH_NOW = "2026-07-07T10:00:00Z"
# Injected clock 25h later → every source (orderflow max 24h) is stale.
STALE_NOW = "2026-07-08T11:00:00Z"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wca_edge_desk",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "wca_edge_desk.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


def _fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def _feeds():
    return {
        "advancement": _fixture("advancement_data"),
        "bet_recs": _fixture("bet_recs"),
        "scores_markets": _fixture("scores_markets"),
        "pm_ideas": _fixture("pm_ideas"),
        "orderflow": _fixture("orderflow"),
    }


def _build(feeds=None, generated=FRESH_NOW, **kwargs):
    f = feeds or _feeds()
    return MOD.build_feed(f["advancement"], f["bet_recs"], f["pm_ideas"],
                          f["orderflow"], f["scores_markets"],
                          generated=generated, **kwargs)


def _row(feed, team, stage):
    matches = [r for r in feed["rows"]
               if r["team"] == team and r["stage"] == stage]
    assert len(matches) == 1, "expected exactly one %s/%s row" % (team, stage)
    return matches[0]


def _check(feed, name):
    matches = [c for c in feed["freshness"]["checks"] if c["name"] == name]
    assert len(matches) == 1, "expected exactly one %r freshness check" % name
    return matches[0]


# ---------------------------------------------------------------- meta / shape

def test_meta_house_conventions():
    feed = _build()
    meta = feed["meta"]
    assert meta["schema_version"] == 2
    assert meta["generated_at"] == FRESH_NOW
    assert meta["shadow_only"] is True
    assert isinstance(meta["caveats"], list) and meta["caveats"]
    # sources: {path: generated stamp of that feed} — all five inputs.
    assert meta["sources"]["site/advancement_data.json"] == "2026-07-07 09:00:00 UTC"
    assert meta["sources"]["site/bet_recs.json"] == "2026-07-07 09:05:00 UTC"
    assert meta["sources"]["site/scores_markets.json"] == "2026-07-07 08:55:00 UTC"
    assert meta["sources"]["site/pm_ideas.json"] == "2026-07-07T08:50:00Z"
    assert meta["sources"]["site/microstructure/orderflow.json"] == "2026-07-07T08:45:00+00:00"
    assert meta["n_rows"] == len(feed["rows"]) == 12
    assert meta["n_by_verdict"] == {"SHADOW_ADD": 2, "WATCH": 7,
                                    "WITHHOLD": 2, "DO_NOT_TRADE": 1}


def test_verdict_enum_four_labels_never_executing():
    feed = _build()
    assert feed["meta"]["verdict_enum"] == ["SHADOW_ADD", "WATCH", "WITHHOLD",
                                            "DO_NOT_TRADE"]
    for row in feed["rows"]:
        assert row["verdict"] in feed["meta"]["verdict_enum"]
        # Nothing mistakable for live execution.
        for banned in ("TRADE ", "PLACE", "FIRE", "BUY NOW"):
            assert banned not in row["verdict"]
        # The CLV/history blocker is stamped on every single row.
        assert row["gates"]["clv_history"]["pass"] is False
        assert "CLV" in row["gates"]["clv_history"]["reason"]
        assert "price/CLV capture restored" in row["gates"]["clv_history"]["reason"]
    assert feed["clv_history_blocker"]["blocked"] is True
    # The stale/missing-history blocker is an explicit meta caveat too.
    assert any("price capture + CLV stamping" in c for c in feed["meta"]["caveats"])


def test_settlement_basis_on_every_row_and_meta():
    feed = _build()
    spec_string = "PM advancement includes ET+pens; 1X2 is 90 minutes only"
    assert feed["meta"]["settlement_basis"] == spec_string
    for row in feed["rows"]:
        assert spec_string in row["settlement_basis"]
        assert "settlement" in row["gates"]
    assert any("90 minutes" in c for c in feed["meta"]["caveats"])


# ------------------------------------------------------------------- universe

def test_universe_requires_model_and_pm_quote():
    feed = _build()
    pairs = {(r["team"], r["stage"]) for r in feed["rows"]}
    # Brazil/SF has a model prob but NO PM quote anywhere → not in the universe.
    assert ("Brazil", "SF") not in pairs
    # Eliminatia: decided everywhere, no quotes.
    assert "Eliminatia" not in {r["team"] for r in feed["rows"]}
    # Model-only legs are counted, not fabricated (21 undecided legs without a
    # quote in the fixture, net of the bet_recs-covered Brazil/QF).
    assert feed["meta"]["n_advancement_legs_without_pm_quote"] == 21


def test_bet_recs_only_row_has_explicit_yes_side():
    feed = _build()
    row = _row(feed, "Brazil", "QF")           # advancement_data has pm: {}
    assert row["origin"] == "bet_recs"
    assert row["pm_price"] == 0.71
    assert row["edge_adj"] == 0.0573
    assert "bet_recs.advancement_futures[brazil_qf_pm]" in row["pm_price_source"]
    assert row["side"] == "YES"
    assert row["side_confidence"] == "explicit"
    assert row["gates"]["side_attribution"]["pass"] is True
    assert row["verdict"] == "SHADOW_ADD"


def test_group_winner_stage_quotes_included():
    feed = _build()
    row = _row(feed, "Morocco", "group_winner")
    assert row["pm_price"] == 0.3
    assert row["edge_adj"] == 0.041
    assert row["bucket"] == "mid"
    assert "group stage" in row["market_label"]


def test_decided_leg_still_actionable_in_bet_recs_is_withheld_not_dropped():
    feed = _build()
    row = _row(feed, "Doneland", "QF")         # model 1.0 but bet_recs ADD
    assert row["leg_state"] == "decided"
    assert row["bet_rec"]["id"] == "doneland_qf_pm"
    assert row["verdict"] == "WITHHOLD"
    assert any("decided" in r and "bet_recs" in r for r in row["verdict_reasons"])


def test_decided_leg_without_bet_rec_is_do_not_trade():
    feeds = _feeds()
    for t in feeds["advancement"]["teams"]:
        if t["team"] == "Doneland":
            t["model"]["SF"] = 1.0
            t["pm"]["SF"] = {"pm": 0.95, "edge_adj": 0.03}
    feed = _build(feeds)
    row = _row(feed, "Doneland", "SF")
    assert row["leg_state"] == "decided"
    assert row["verdict"] == "DO_NOT_TRADE"
    assert any("decided_leg" in r for r in row["verdict_reasons"])


def test_withheld_near_miss_included_with_reason_and_1x2_settlement():
    feed = _build()
    rows = [r for r in feed["rows"] if r["origin"] == "bet_recs.withheld"]
    assert len(rows) == 1                      # only the informative near-miss
    row = rows[0]
    assert row["team"] == "Morocco" and row["market"] == "1X2"
    assert row["verdict"] == "WITHHOLD"
    assert row["withheld_reason"] == "model_prob 16% < floor 20%"
    assert row["model_prob"] == 0.1635
    assert row["book_price_decimal"] == 6.9653
    assert "NOT PM" in row["book_edge_source"]
    assert "90-minute" in row["settlement_basis"]
    assert "90 minutes" in row["gates"]["settlement"]["note"]
    # KO context joins the actual fixture (France vs Morocco QF, real).
    assert row["knockout_context"]["next_match"]["opponent"] == "France"
    assert row["knockout_context"]["next_match"]["tie_status"] == "real"
    # No-price props / unsupported markets are counted, not fabricated.
    assert feed["meta"]["n_withheld_excluded_uninformative"] == 2


# ----------------------------------------------------------------- happy path

def test_happy_path_shadow_add_with_traceable_numbers():
    feed = _build()
    row = _row(feed, "Morocco", "SF")
    # Every number matches its named source-feed field verbatim.
    assert row["model_prob"] == 0.55
    assert row["pm_price"] == 0.44
    assert row["edge_adj"] == 0.1026
    assert "advancement_data.teams[Morocco].pm[SF]" in row["pm_price_source"]
    assert row["side"] == "YES" and row["position_prob"] == 0.55
    assert row["side_confidence"] == "derived"
    assert row["bucket"] == "moneyline"
    assert row["bet_rec"]["id"] == "morocco_sf_pm"
    assert row["bet_rec"]["stake"] == 106.51
    for gate in ("freshness", "price_present", "edge", "projection",
                 "side_attribution", "min_prob_cash"):
        assert row["gates"][gate]["pass"] is True, gate
    assert row["verdict"] == "SHADOW_ADD"
    # SHADOW_ADD still explicitly non-executing.
    assert any("BLOCKED" in r for r in row["verdict_reasons"])


# ------------------------------------------------- knockout context (MED-4/c)

def test_ko_context_real_qf_tie():
    feed = _build()
    ctx = _row(feed, "Morocco", "SF")["knockout_context"]
    tie = ctx["deciding_tie"]
    assert tie["opponent"] == "France"
    assert tie["round"] == "Quarter-finals"
    assert tie["date"] == "2026-07-09"
    assert tie["match_no"] == 97
    assert tie["tie_status"] == "real" and tie["played"] is False
    # 90-min split oriented to the candidate team (Morocco away).
    split = tie["model_split_90min"]
    assert split == {"team_win": 0.192, "draw": 0.2821, "opp_win": 0.526,
                     "basis": "1X2_90min"}
    assert tie["top_scoreline"] == "1-0" and tie["top_scoreline_prob"] == 0.178
    assert "1X2_90min" in tie["settlement_basis"]


def test_ko_context_projected_qf_tie():
    feed = _build()
    tie = _row(feed, "Bergland", "SF")["knockout_context"]["deciding_tie"]
    assert tie["tie_status"] == "projected"
    assert tie["opponent"] == "Coastalia"


def test_ko_context_real_ft_scored_r16_tie():
    feed = _build()
    tie = _row(feed, "Doneland", "QF")["knockout_context"]["deciding_tie"]
    assert tie["round"] == "Round of 16"
    assert tie["tie_status"] == "real"
    assert tie["played"] is True and tie["ft"] == "1-0"


def test_ko_context_missing_tie_gets_reason_never_a_guess():
    feed = _build()
    ctx = _row(feed, "Morocco", "Final")["knockout_context"]
    assert ctx["deciding_tie"] is None
    assert "does not reach this stage" in ctx["deciding_tie_reason"]
    # group_winner has no knockout deciding tie by construction.
    gw = _row(feed, "Morocco", "group_winner")["knockout_context"]
    assert gw["deciding_tie"] is None
    assert "group stage" in gw["deciding_tie_reason"]


def test_next_match_is_first_unplayed_ko_game():
    feed = _build()
    nm = _row(feed, "Morocco", "SF")["knockout_context"]["next_match"]
    assert nm["opponent"] == "France" and nm["match_no"] == 97
    # Doneland played its R16 → next match is the (projected) QF.
    nm = _row(feed, "Doneland", "QF")["knockout_context"]["next_match"]
    assert nm["round"] == "Quarter-finals" and nm["opponent"] == "Brazil"


# --------------------------------------------------- projection gate (MED-2/e)

def test_projected_deciding_tie_downgrades_shadow_add_to_watch():
    feed = _build()
    row = _row(feed, "Bergland", "SF")
    # Everything else passes...
    for gate in ("freshness", "price_present", "edge", "side_attribution",
                 "min_prob_cash"):
        assert row["gates"][gate]["pass"] is True, gate
    # ...but the deciding tie is projected → WATCH, reason recorded.
    assert row["gates"]["projection"]["pass"] is False
    assert "PROJECTED" in row["gates"]["projection"]["reason"]
    assert row["verdict"] == "WATCH"
    assert any(r.startswith("projected_tie:") for r in row["verdict_reasons"])


def test_real_deciding_tie_does_not_downgrade():
    feed = _build()
    row = _row(feed, "Morocco", "SF")
    assert row["gates"]["projection"]["pass"] is True
    assert row["verdict"] == "SHADOW_ADD"


def test_projected_group_tie_downgrades_group_winner_stage():
    feed = _build()
    row = _row(feed, "Morocco", "group_winner")   # level with Tieland on pts+gd+gf
    assert row["group_context"]["projected_tie"] is True
    assert row["group_context"]["tied_with"] == ["Tieland"]
    assert row["gates"]["projection"]["pass"] is False
    assert row["verdict"] == "WATCH"
    assert any("projected group-position tie" in c
               for c in feed["meta"]["caveats"])


# ------------------------------------------------ side attribution (HIGH-2)

def test_side_attribution_uncertain_reviewer_scenario():
    """Reviewer's concrete scenario: priceMap Yes=0.50 stale (no bestBid),
    bestAsk=0.40, model=0.45. advancement.py computes edge_adj on the YES ask:
    0.45 - 0.40 - fee(0.40) = +0.0428, but the desk's sign test (model < pm)
    says NO. The quoted mid can only justify |0.45-0.50| - fee(0.50) = +0.0425
    on the derived side, so edge_adj exceeds it → attribution UNCERTAIN, row
    capped at WATCH, never SHADOW_ADD."""
    feed = _build()
    row = _row(feed, "Nordland", "SF")         # fixture carries this scenario
    assert row["model_prob"] == 0.45 and row["pm_price"] == 0.5
    assert row["edge_adj"] == 0.0428
    assert row["side_confidence"] == "uncertain"
    assert row["gates"]["side_attribution"]["pass"] is False
    assert "side_attribution_uncertain" in row["gates"]["side_attribution"]["reason"]
    assert row["verdict"] == "WATCH"
    assert row["verdict"] != "SHADOW_ADD"
    assert any("side_attribution_uncertain" in r for r in row["verdict_reasons"])
    assert any("side attribution UNCERTAIN" in c for c in feed["meta"]["caveats"])


def test_side_attribution_helper_flags_mid_edge_impossible():
    # edge_adj > 0 while the derived side's mid edge is <= 0 after fees.
    side, pos, conf, reason = MOD._side_attribution(0.50, 0.4995, 0.03)
    assert conf == "uncertain" and "side_attribution_uncertain" in reason
    # Consistent case: quoted mid fully explains the edge.
    side, pos, conf, reason = MOD._side_attribution(0.55, 0.44, 0.1026)
    assert side == "YES" and conf == "derived" and reason is None
    # NO side, consistent (mid edge covers edge_adj).
    side, pos, conf, reason = MOD._side_attribution(0.3296, 0.655, 0.3136)
    assert side == "NO" and pos == 0.6704 and conf == "derived"


# ------------------------------------------------ orderflow (MED-1 / LOW-1)

def test_orderflow_honesty_notes_carried_verbatim_everywhere():
    feeds = _feeds()
    notes = feeds["orderflow"]["honesty_notes"]
    assert len(notes) == 2
    feed = _build(feeds)
    for note in notes:                          # verbatim, ALL of them
        assert note in feed["meta"]["caveats"]
    row = _row(feed, "Morocco", "SF")
    assert row["orderflow"]["honesty_notes"] == notes


def test_hot_flow_is_relative_to_category_baseline_with_method_recorded():
    feed = _build()
    hot_meta = feed["meta"]["orderflow_hot"]
    assert "mean + 1 sample sd" in hot_meta["method"]
    assert hot_meta["n_categories"] == 6
    assert hot_meta["hot_threshold"] is not None
    assert hot_meta["mean_buy_pressure"] < hot_meta["hot_threshold"]
    # advancement_qf (0.95) is the only category above mean+1sd (~0.882);
    # structurally buy-heavy categories like match-adjacent 0.83/0.7997 are NOT.
    assert _row(feed, "Colombia", "QF")["orderflow"]["hot"] is True
    assert _row(feed, "Morocco", "SF")["orderflow"]["hot"] is False
    assert _row(feed, "Coldland", "SF")["orderflow"]["hot"] is False


def test_orderflow_context_fields_and_no_fake_joins():
    feed = _build()
    flow = _row(feed, "Colombia", "QF")["orderflow"]
    assert flow["signal_level"] == "category"
    assert flow["avg_trade_usd"] == 80.6
    assert flow["n_trades"] == 31000            # n stated
    assert flow["smart_usd_share"] == 0.05
    assert flow["dumb_usd_share"] == 0.01
    assert "omitted rather than faked" in flow["no_exact_join"]
    # Jump-latency summary joined when the category has one, else None.
    assert flow["latency"]["p90_reprice_s"] == 198.0
    assert _row(feed, "Morocco", "SF")["orderflow"]["latency"] is None


def test_negative_edge_with_hot_flow_never_exceeds_watch():
    feed = _build()
    row = _row(feed, "Colombia", "QF")
    assert row["orderflow"]["hot"] is True
    assert row["edge_adj"] == -0.0054
    assert row["gates"]["edge"]["pass"] is False
    assert row["verdict"] == "WATCH"            # informative, NEVER SHADOW_ADD
    assert row["verdict"] != "SHADOW_ADD"
    assert any("hot_flow_without_edge" in r for r in row["verdict_reasons"])
    assert any("can NEVER turn a negative model edge into a trade" in r
               for r in row["verdict_reasons"])


def test_negative_edge_without_hot_flow_is_do_not_trade():
    feed = _build()
    row = _row(feed, "Coldland", "SF")
    assert row["edge_adj"] == -0.011
    assert row["orderflow"]["hot"] is False
    assert row["verdict"] == "DO_NOT_TRADE"


def test_orderflow_note_says_context_only():
    feed = _build()
    for row in feed["rows"]:
        if row["orderflow"] is not None:
            assert "NEVER overrides" in row["orderflow"]["note"]


# --------------------------------------------------- edge gate / longshots

def test_near_threshold_edge_is_watch_not_shadow_add():
    feed = _build()
    row = _row(feed, "Midland", "SF")           # +0.015 <= MIN_EDGE 0.02
    assert row["edge_adj"] == 0.015
    assert row["gates"]["edge"]["pass"] is False
    assert "near_threshold_edge" in row["gates"]["edge"]["reason"]
    assert row["verdict"] == "WATCH"
    assert any("near_threshold_edge" in r for r in row["verdict_reasons"])


def test_longshot_positive_edge_capped_at_watch_with_reason():
    feed = _build()
    row = _row(feed, "Switzerland", "SF")       # model 14%, +edge
    assert row["edge_adj"] == 0.0371 and row["edge_adj"] > 0
    assert row["bucket"] == "longshot"
    assert row["gates"]["min_prob_cash"]["pass"] is False
    assert row["verdict"] == "WATCH"            # can NEVER be SHADOW_ADD
    assert any("longshot_no_cash" in r for r in row["verdict_reasons"])
    row = _row(feed, "Morocco", "Final")        # longshot too
    assert row["verdict"] == "WATCH"
    assert any("longshot_no_cash" in r for r in row["verdict_reasons"])


def test_no_side_edge_buckets_on_position_probability():
    feed = _build()
    row = _row(feed, "Colombia", "QF")          # model .5982 < pm .605 → NO side
    assert row["side"] == "NO"
    assert row["position_prob"] == 0.4018
    assert "derived" in row["position_prob_source"]
    assert row["bucket"] == "mid"


# --------------------------------------------- explicit side from the feed


def _set_side(feeds, team, stage, side, ask=None):
    """Add the (new) explicit side/ask fields to a fixture pm entry."""
    for t in feeds["advancement"]["teams"]:
        if t["team"] == team:
            entry = dict(t["pm"][stage])
            entry["side"] = side
            if ask is not None:
                entry["ask"] = ask
            t["pm"][stage] = entry


def test_feed_side_preferred_over_derivation():
    """The reviewer's stale-print scenario resolves itself once the feed
    emits the side: Nordland's edge_adj +0.0428 belongs to YES (ask 0.40)
    even though model < mid — with pm[stage].side present there is nothing
    to derive, so the HIGH-2 uncertainty guard must NOT fire."""
    feeds = _feeds()
    _set_side(feeds, "Nordland", "SF", "YES", ask=0.4)
    feed = _build(feeds)
    row = _row(feed, "Nordland", "SF")
    assert row["side"] == "YES"
    assert row["side_source"] == "feed"
    assert row["side_confidence"] == "explicit"
    assert row["position_prob"] == 0.45
    assert row["bucket"] == "mid"                 # buckets on the FEED side
    assert row["gates"]["side_attribution"]["pass"] is True
    assert "advancement_data" in row["side_note"]
    assert "feed-emitted side" in row["edge_source"]
    assert row["verdict"] == "SHADOW_ADD"         # was WATCH when derived
    assert not any("side attribution UNCERTAIN" in c
                   for c in feed["meta"]["caveats"])


def test_feed_side_no_buckets_on_position_probability():
    feeds = _feeds()
    _set_side(feeds, "Nordland", "SF", "NO")
    feed = _build(feeds)
    row = _row(feed, "Nordland", "SF")
    assert row["side"] == "NO"
    assert row["side_source"] == "feed"
    assert row["position_prob"] == 0.55           # 1 - model_prob
    assert row["bucket"] == "moneyline"
    assert row["gates"]["side_attribution"]["pass"] is True


def test_malformed_feed_side_falls_back_to_derivation():
    # Anything but a literal "YES"/"NO" is verified, never trusted.
    feeds = _feeds()
    _set_side(feeds, "Nordland", "SF", "Maybe")
    feed = _build(feeds)
    row = _row(feed, "Nordland", "SF")
    assert row["side_source"] == "derived"
    assert row["side_confidence"] == "uncertain"  # guard still fires
    assert row["verdict"] == "WATCH"


def test_side_source_recorded_on_every_row():
    feed = _build()
    # The fixture feed predates pm[stage].side → every advancement_data row
    # is a derivation; bet_recs rows carry their explicit YES buy; withheld
    # book near-misses have no PM side at all.
    assert _row(feed, "Morocco", "SF")["side_source"] == "derived"
    assert _row(feed, "Nordland", "SF")["side_source"] == "derived"
    assert _row(feed, "Brazil", "QF")["side_source"] == "bet_recs"
    withheld = [r for r in feed["rows"]
                if r["origin"] == "bet_recs.withheld"][0]
    assert withheld["side_source"] is None
    for row in feed["rows"]:
        assert "side_source" in row


# ------------------------------------------------------ pm_ideas (LOW-2/3)

def test_related_pm_ideas_joined_on_canonical_names_with_settlement_tag():
    feed = _build()
    row = _row(feed, "Morocco", "SF")
    assert row["related_pm_ideas"]["n"] == 1
    idea = row["related_pm_ideas"]["ideas"][0]
    assert idea["match"] == "Morocco vs France"
    assert idea["settlement_basis"] == "1X2_90min"
    assert _row(feed, "Switzerland", "SF")["related_pm_ideas"]["n"] == 0


def test_idea_join_uses_teamname_aliases_not_substrings():
    # "USA" canonicalises to "United States" (wca.data.teamnames).
    teams = MOD._idea_teams({"match": "USA vs Wales", "selection": "USA"})
    assert teams == {"United States", "Wales"}
    # No raw-substring behaviour: "Moroc" must NOT match anything.
    assert "Morocco" not in MOD._idea_teams({"match": "Moroc vs France",
                                             "selection": ""})


# ----------------------------------------------- freshness (HIGH-1, LOW-4/5/6)

def test_fresh_wrapper_but_stale_model_stamp_fails_freshness():
    """HIGH-1 boundary: the 30-min publish re-stamps meta.generated, but the
    sim cache behind model_prob/edge_adj can be arbitrarily old — a fresh
    wrapper stamp must NOT mask a stale model stamp."""
    feeds = _feeds()
    feeds["advancement"]["meta"]["generated"] = "2026-07-07 09:55:00 UTC"  # 5 min old
    feeds["advancement"]["meta"]["model_generated"] = "2026-07-06 19:00 UTC"  # 15h old
    feed = _build(feeds)
    assert _check(feed, "advancement")["pass"] is True      # wrapper fresh
    model = _check(feed, "advancement_model")
    assert model["pass"] is False and "stale" in model["reason"]
    assert feed["freshness"]["pass"] is False
    for row in feed["rows"]:
        assert row["gates"]["freshness"]["pass"] is False
        assert row["verdict"] in ("DO_NOT_TRADE", "WITHHOLD")


def test_model_stamp_and_age_exposed_in_meta():
    feed = _build()
    assert feed["meta"]["advancement_model_generated"] == "2026-07-07 07:20 UTC"
    assert feed["meta"]["advancement_model_age_hours"] == 2.67
    model = _check(feed, "advancement_model")
    assert model["pass"] is True
    assert model["max_age_secs"] == 14 * 3600


def test_pm_blind_marker_fails_freshness():
    feeds = _feeds()
    feeds["advancement"]["meta"]["n_pm_markets"] = 0
    feed = _build(feeds)
    check = _check(feed, "advancement_pm_markets")
    assert check["pass"] is False and "PM-BLIND" in check["reason"]
    assert feed["freshness"]["pass"] is False
    assert any("PM-BLIND" in c for c in feed["meta"]["caveats"])


@pytest.mark.parametrize("name,stamp_key,max_h", [
    ("advancement", None, 3),
    ("bet_recs", None, 3),
    ("scores_markets", None, 3),
    ("pm_ideas", None, 6),
    ("orderflow", None, 24),
])
def test_per_source_freshness_boundaries(name, stamp_key, max_h):
    """LOW-5: just-under passes, just-over fails, per source threshold.
    Clock = FRESH_NOW (2026-07-07T10:00:00Z)."""
    def set_stamp(feeds, stamp):
        if name == "orderflow":
            feeds["orderflow"]["generated_utc"] = stamp
        else:
            feeds[name]["meta"]["generated"] = stamp
    # 1 minute inside the threshold.
    feeds = _feeds()
    inside = "2026-07-%02dT%02d:01:00Z" % (7 - (max_h + 14) // 24,
                                           (10 - max_h) % 24)
    set_stamp(feeds, inside)
    assert _check(_build(feeds), name)["pass"] is True, inside
    # 1 minute beyond the threshold.
    feeds = _feeds()
    outside = "2026-07-%02dT%02d:59:00Z" % (7 - (max_h + 15) // 24,
                                            (10 - max_h - 1) % 24)
    set_stamp(feeds, outside)
    check = _check(_build(feeds), name)
    assert check["pass"] is False and "stale" in check["reason"], outside


def test_model_stamp_boundary():
    feeds = _feeds()
    feeds["advancement"]["meta"]["model_generated"] = "2026-07-06 20:01 UTC"  # 13h59
    assert _check(_build(feeds), "advancement_model")["pass"] is True
    feeds["advancement"]["meta"]["model_generated"] = "2026-07-06 19:59 UTC"  # 14h01
    assert _check(_build(feeds), "advancement_model")["pass"] is False


def test_future_dated_stamp_fails_closed_beyond_skew_tolerance():
    """LOW-6: a stamp from the future must not pass as 'age <= max'."""
    feeds = _feeds()
    feeds["bet_recs"]["meta"]["generated"] = "2026-07-07 11:00:00 UTC"  # +1h
    check = _check(_build(feeds), "bet_recs")
    assert check["pass"] is False
    assert "future-dated" in check["reason"] and "fail closed" in check["reason"]
    # Small clock skew (2 min ahead) is tolerated.
    feeds = _feeds()
    feeds["bet_recs"]["meta"]["generated"] = "2026-07-07 10:02:00 UTC"
    assert _check(_build(feeds), "bet_recs")["pass"] is True


def test_stale_feeds_fail_freshness_gate_and_block_every_actionable_row():
    feed = _build(generated=STALE_NOW)
    assert feed["freshness"]["pass"] is False
    assert any("stale" in (c["reason"] or "") for c in feed["freshness"]["checks"])
    assert any("freshness gate FAILED" in c for c in feed["meta"]["caveats"])
    for row in feed["rows"]:
        assert row["gates"]["freshness"]["pass"] is False
        assert row["verdict"] in ("DO_NOT_TRADE", "WITHHOLD")
    assert feed["meta"]["n_by_verdict"]["SHADOW_ADD"] == 0
    assert feed["meta"]["n_by_verdict"]["WATCH"] == 0


def test_missing_source_fails_closed_with_reason():
    feeds = _feeds()
    feed = MOD.build_feed(feeds["advancement"], feeds["bet_recs"],
                          feeds["pm_ideas"], None, feeds["scores_markets"],
                          generated=FRESH_NOW,
                          load_errors={"orderflow": "file not found: x"})
    assert feed["freshness"]["pass"] is False
    assert feed["meta"]["sources"]["site/microstructure/orderflow.json"] is None
    assert any("source unavailable" in c for c in feed["meta"]["caveats"])
    row = _row(feed, "Morocco", "SF")
    assert row["orderflow"]["buy_pressure"] is None
    assert row["orderflow"]["hot"] is None
    assert "unavailable" in row["orderflow"]["reason"]


def test_unparseable_source_stamp_fails_closed():
    feeds = _feeds()
    feeds["bet_recs"]["meta"]["generated"] = "not a timestamp"
    check = _check(_build(feeds), "bet_recs")
    assert check["pass"] is False
    assert "no parseable" in check["reason"]


def test_missing_scores_markets_yields_reasons_not_guesses():
    feeds = _feeds()
    feed = MOD.build_feed(feeds["advancement"], feeds["bet_recs"],
                          feeds["pm_ideas"], feeds["orderflow"], None,
                          generated=FRESH_NOW,
                          load_errors={"scores_markets": "file not found: x"})
    ctx = _row(feed, "Morocco", "SF")["knockout_context"]
    assert ctx["deciding_tie"] is None
    assert "unavailable" in ctx["deciding_tie_reason"]
    # Unknown tie context is treated as projected (downgrade, not a guess).
    assert _row(feed, "Morocco", "SF")["gates"]["projection"]["pass"] is False


# ---------------------------------------------------------- ordering / purity

def test_ordering_bucket_then_further_out_stage_then_edge():
    feed = _build()
    got = [(r["team"], r["stage"]) for r in feed["rows"]]
    assert got == [
        # tier 0 — moneyline bucket: SF legs (further-out rank 2) by edge desc…
        ("Morocco", "SF"),        # +0.1026
        ("Bergland", "SF"),       # +0.0800
        ("Nordland", "SF"),       # +0.0428
        ("Midland", "SF"),        # +0.0150
        ("Coldland", "SF"),       # -0.0110
        # …then the QF leg (rank 3) despite its larger edge than some SF rows.
        ("Brazil", "QF"),         # +0.0573
        # mid bucket: QF (rank 3) before group_winner (rank 6).
        ("Colombia", "QF"),
        ("Morocco", "group_winner"),
        # longshot bucket: Final (rank 1) before SF (rank 2).
        ("Morocco", "Final"),
        ("Switzerland", "SF"),
        # tier 1 — decided legs.
        ("Doneland", "QF"),
        # tier 2 — withheld near-misses.
        ("Morocco", None),
    ]


def test_bucket_convention_imported_from_wca_selection():
    # Canonical selection rule IMPORTED, not replicated (PR #170 follow-up
    # closed): identity with wca.selection, so the desk can never drift.
    import wca.selection as selection
    assert MOD.PROB_BUCKETS is selection.PROB_BUCKETS
    assert MOD.prob_bucket is selection.prob_bucket
    assert MOD.longshot_no_cash is selection.longshot_no_cash
    assert MOD.LONGSHOT_PROB == selection.LONGSHOT_PROB == 0.25
    assert MOD.PROB_BUCKETS == ((0.50, "moneyline"), (0.25, "mid"),
                                (0.0, "longshot"))
    assert MOD.prob_bucket(0.55) == "moneyline"
    assert MOD.prob_bucket(0.30) == "mid"
    assert MOD.prob_bucket(0.10) == "longshot"
    meta = _build()["meta"]
    assert "wca.selection" in meta["selection_rule"]["source"]
    assert "follow_up" not in meta["selection_rule"]   # follow-up shipped


def test_min_edge_imported_from_betrecs():
    # MIN_EDGE comes from its defining module (scripts/wca_betrecs.py) — the
    # edge desk put scripts/ on sys.path when it loaded, so import directly.
    import wca_betrecs
    assert MOD.MIN_EDGE_ADJ == wca_betrecs.MIN_EDGE == 0.02
    assert "wca_betrecs" in _build()["meta"]["edge_gate"]["source"]


def test_deterministic_output():
    a = json.dumps(_build(), sort_keys=True)
    b = json.dumps(_build(), sort_keys=True)
    assert a == b


def test_mutation_isolation_between_builds():
    feeds = _feeds()
    before = copy.deepcopy(feeds)
    _build(feeds)
    assert feeds == before                     # build_feed is pure


def test_unparseable_generated_raises_in_build_feed():
    with pytest.raises(ValueError):
        _build(generated="garbage o'clock")


# ------------------------------------------------------------------ CLI / files

def _cli_argv(out, generated=FRESH_NOW):
    return ["--advancement", os.path.join(FIXTURES, "advancement_data.json"),
            "--bet-recs", os.path.join(FIXTURES, "bet_recs.json"),
            "--scores-markets", os.path.join(FIXTURES, "scores_markets.json"),
            "--pm-ideas", os.path.join(FIXTURES, "pm_ideas.json"),
            "--orderflow", os.path.join(FIXTURES, "orderflow.json"),
            "--out", str(out), "--generated", generated]


def test_cli_generates_deterministic_file(tmp_path):
    out = tmp_path / "advancement_edge_desk.json"
    argv = _cli_argv(out)
    assert MOD.main(argv) == 0
    first = out.read_text()
    payload = json.loads(first)
    assert payload["meta"]["generated_at"] == FRESH_NOW
    assert payload["meta"]["n_rows"] == 12
    assert MOD.main(argv) == 0
    assert out.read_text() == first            # byte-identical rerun


def test_cli_default_out_is_advancement_edge_desk():
    # Spec-preferred feed name (renamed from edge_desk.json; no alias — the
    # panel + publish wiring moved with it in the same PR).
    assert MOD.DEFAULT_OUT == os.path.join("site", "advancement_edge_desk.json")


def test_cli_unparseable_generated_hard_errors(tmp_path):
    out = tmp_path / "advancement_edge_desk.json"
    rc = MOD.main(_cli_argv(out, generated="not-a-time"))
    assert rc == 2                             # non-zero, fail closed
    assert not out.exists()                    # nothing written


def test_cli_missing_input_file_still_emits_honest_feed(tmp_path):
    out = tmp_path / "advancement_edge_desk.json"
    argv = _cli_argv(out)
    argv[argv.index("--orderflow") + 1] = str(tmp_path / "nope.json")
    assert MOD.main(argv) == 0
    payload = json.loads(out.read_text())
    assert payload["freshness"]["pass"] is False
    assert any("source unavailable" in c for c in payload["meta"]["caveats"])


def test_committed_feeds_smoke():
    """The generator must run standalone offline from the committed site feeds."""
    root = os.path.join(os.path.dirname(__file__), "..")
    paths = {name: os.path.join(root, rel)
             for name, rel in MOD.DEFAULT_PATHS.items()}
    if not all(os.path.exists(p) for p in paths.values()):
        pytest.skip("committed site feeds not present in this checkout")
    feed = MOD.generate(paths, generated=FRESH_NOW)
    assert feed["meta"]["schema_version"] == 2
    assert feed["meta"]["shadow_only"] is True
    assert feed["clv_history_blocker"]["blocked"] is True
    for row in feed["rows"]:
        assert row["verdict"] in ("SHADOW_ADD", "WATCH", "WITHHOLD",
                                  "DO_NOT_TRADE")
        assert "PM advancement includes ET+pens" in row["settlement_basis"]
