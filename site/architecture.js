/* World Cup Alpha — "Under The Hood" architecture page.
 *
 * Static documentation of the live system, embedded from
 * docs/architecture/SYSTEM_MAP.md (code-verified). No fetch, no external
 * assets, no CDN. The diagram is hand-built in inline SVG + HTML/CSS in the
 * same dark terminal aesthetic as index.html / scores.html.
 *
 * Regenerate this data array alongside the code when SYSTEM_MAP.md changes;
 * this page is documentation, not a live feed.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ======================================================================
  //  DATA — embedded from docs/architecture/SYSTEM_MAP.md
  // ======================================================================

  var LEAD =
    "We trade the 2026 World Cup as a disciplined quant. The training history is the " +
    "martj42 international-results dataset, but we never trust it raw: a cleaning " +
    "overlay corrects and back-fills fixtures, and a verification pipeline " +
    "cross-checks recent results against TWO independent feeds (ESPN + " +
    "TheSportsDB) — a fix is only auto-applied when both agree, everything else " +
    "is parked for human review. Live bookmaker odds (The Odds API, UK) and " +
    "Polymarket prices come in alongside. Two models are fitted on the cleaned " +
    "history — an Elo rating with an ordered-logit 1X2, and an 8-year half-life " +
    "Dixon-Coles goals model — and guarded by walk-forward backtest, blend-fit " +
    "and structural-bias test suites so the parameters aren't overfit. Each " +
    "fixture's books are Shin-de-vigged to a market consensus, then Elo + " +
    "Dixon-Coles + market are blended (market weighted 60% because it is hard to " +
    "beat). We line-shop the best price, keep only edges clearing 2%, and size at " +
    "fractional Kelly off one combined bankroll (GBP sportsbook venues + Polymarket, " +
    "FX'd) with a CLV-gated ladder. The " +
    "system never places a trade — it emits a card; a human places it, screenshots " +
    "the slip, and a 15-command Telegram bot reads it via Claude vision into the " +
    "SQLite ledger after a yes. A snapshot daemon records the closing line so the " +
    "ledger can compute CLV — did we beat the close?";

  var MENTAL =
    "martj42 → clean + 2-source verify → fit + overfit guards → blend → edge " +
    "filter → CLV-laddered Kelly card → human places → ledger → CLV";

  // Five pipeline stages. Each id matches the section anchor.
  var STAGES = [
    {
      id: "ingestion",
      n: "01",
      title: "Ingestion",
      tag: "feeds in",
      blurb: "Every external feed, its exact fields and cadence.",
      cards: [
        {
          name: "Historical results (raw)",
          role: "martj42 results.csv — the pristine upstream mirror, re-downloaded daily.",
          file: "src/wca/data/results.py",
          params: [
            ["dest", "data/raw/results.csv"],
            ["refresh", "skip if mtime == today (UTC)"],
            ["fields", "date, teams, scores Int64, tournament, city, country, neutral"],
            ["note", "never read directly — consumers load the CLEANED overlay"]
          ],
          inputs: ["GitHub raw CSV"],
          outputs: ["raw martj42 mirror"]
        },
        {
          name: "Cleaning overlay + 2-source verification",
          role: "Corrects/back-fills fixtures; auto-applies only what TWO feeds agree on.",
          file: "src/wca/data/cleaning.py · fixture_sources.py · reconcile.py",
          params: [
            ["overlay", "raw + data/corrections.json → data/raw/martj42_cleaned.csv (idempotent)"],
            ["sources", "ESPN scoreboard + TheSportsDB (keyless, defensive)"],
            ["auto-apply", "BOTH agree & differ from martj42 → stage; orientation-aware"],
            ["else", "single-source / disagreement → data/corrections_review.json (human)"],
            ["loader", "resolve_results_path() → cleaned if present, else raw"],
            ["CI", "clean-results.yml 3×/day; audit → data/audit.json"]
          ],
          inputs: ["raw martj42", "ESPN", "TheSportsDB", "corrections.json"],
          outputs: ["martj42_cleaned.csv", "review queue", "audit"]
        },
        {
          name: "Live odds",
          role: "The Odds API v4 h2h prices, flattened one row per outcome.",
          file: "src/wca/data/theoddsapi.py",
          params: [
            ["sport", "soccer_fifa_world_cup"],
            ["regions", "uk"],
            ["markets", "h2h (card/daemon)"],
            ["format", "decimal"],
            ["plan", "paid tier, 20,000 credits/month"]
          ],
          inputs: ["ODDS_API_KEY"],
          outputs: ["odds frame", "QuotaInfo (x-requests-remaining)"]
        },
        {
          name: "Polymarket (Gamma)",
          role: "Read-only WC market prices — scores-page enrichment only.",
          file: "src/wca/data/polymarket.py",
          params: [
            ["api", "gamma-api.polymarket.com"],
            ["filter", "tag_slug=soccer + WC keywords"],
            ["decode", "outcomes / outcomePrices → priceMap"],
            ["note", "NOT in the trade-card blend"]
          ],
          inputs: ["Gamma /events"],
          outputs: ["priceMap", "polymarket venue column"]
        },
        {
          name: "Vision ingestion",
          role: "Reads a betslip screenshot via Claude — never writes the ledger.",
          file: "src/wca/bot/vision.py",
          params: [
            ["model", "claude-sonnet-4-6 (overridable)"],
            ["api version", "2023-06-01"],
            ["max tokens", "1024"],
            ["odds", "fractional/EVS/cents → decimal"]
          ],
          inputs: ["betslip image (base64)"],
          outputs: ["ExtractedBet[] (selection, odds, stake, boost, conf)"]
        },
        {
          name: "Card cache",
          role: "Most-recent formatted card on disk; clock-free read/write.",
          file: "src/wca/cardcache.py",
          params: [
            ["path", "data/card_latest.md"],
            ["header", "<!-- generated: <ISO> -->"],
            ["stale after", "CARD_MAX_AGE_HOURS = 6.0"]
          ],
          inputs: ["formatted card"],
          outputs: ["{text, generated, stale}"]
        },
        {
          name: "Poll scheduler",
          role: "Pure adaptive cadence for the snapshot daemon, clock injected.",
          file: "src/wca/pollsched.py",
          params: [
            ["in-game", "180s (ko ≤ now < ko+130m)"],
            ["pre-close", "300s (within 10 min)"],
            ["idle", "3600s"],
            ["low-quota", "<200 throttle, <60 reserve → 10800s"]
          ],
          inputs: ["kickoffs", "quota_remaining"],
          outputs: ["(delay_seconds, reason)"]
        },
        {
          name: "Snapshot daemon",
          role: "Persists each odds pull two ways for the closing line.",
          file: "scripts/wca_snapshotd.py",
          params: [
            ["raw", "data/raw/snapshots/oddsapi_h2h_uk_<stamp>.json"],
            ["rows", "odds_snapshots via snapshot_all"],
            ["modes", "--once (cron) / loop, clean SIGTERM"]
          ],
          inputs: ["The Odds API h2h"],
          outputs: ["raw JSON audit", "odds_snapshots rows"]
        }
      ]
    },
    {
      id: "models",
      n: "02",
      title: "Models",
      tag: "fit on clean history",
      blurb: "Two models fitted on the cleaned history, de-vigged market baseline, overfit-guarded.",
      cards: [
        {
          name: "International Elo",
          role: "Rating engine + ordered-logit (proportional-odds) 1X2.",
          file: "src/wca/models/elo.py",
          params: [
            ["initial", "1500.0"],
            ["home adv", "100.0 (+host on neutral)"],
            ["host venue-aware", "opt-in, OFF — co-host dilution + altitude (Azteca)"],
            ["K by importance", "friendly 20 / NL 30 / qual 40 / continental 50 / WC 60"],
            ["goal-margin G", "|gd| 2→1.5, 3→1.75, ≥4 ramps"],
            ["logit", "x=diff/400, beta + c_lo ≤ c_hi, MLE NM→BFGS"]
          ],
          inputs: ["results history"],
          outputs: ["team ratings", "elo_prob (h,d,a)"]
        },
        {
          name: "Dixon-Coles",
          role: "Time-decayed bivariate-Poisson goals model with rho correction.",
          file: "src/wca/models/dixon_coles.py",
          params: [
            ["half-life", "8y DEPLOYED (card.py); 2y library default"],
            ["ridge reg_lambda", "0.01 (×5 low-data)"],
            ["min_matches", "5"],
            ["max_goals", "10"],
            ["rho", "fitted, applied to (0,0)(0,1)(1,0)(1,1)"],
            ["structural prior", "opt-in, OFF — shrink minnows to socio-economic target"]
          ],
          inputs: ["results history", "days_ago decay"],
          outputs: ["score matrix", "dc_prob (h,d,a)", "O/U, BTTS, scores"]
        },
        {
          name: "Shin de-vig",
          role: "Removes the bookmaker margin per book (favourite/longshot aware).",
          file: "src/wca/markets/devig.py",
          params: [
            ["method wired", "shin (of multiplicative/power/shin)"],
            ["solves", "insider proportion z∈[0,1) by bisection"],
            ["consensus", "per-column median across books → renormalise"]
          ],
          inputs: ["book 1X2 decimal odds"],
          outputs: ["fair probs", "market_prob (h,d,a)", "z diagnostic"]
        },
        {
          name: "Overfit & bias guards",
          role: "Test suites that keep the fitted params honest, out-of-sample.",
          file: "tests/test_halflife_backtest.py · test_blend_fit.py · test_structural.py",
          params: [
            ["half-life backtest", "walk-forward log-loss/Brier picks DC xi (no in-sample peeking)"],
            ["blend fit", "convex Elo/DC/market weights on a holdout — fitted vs the 0.10/0.30/0.60 prior"],
            ["structural bias", "socio-economic prior: GDP inverted-U, mean-zero priors, probs sum to 1"],
            ["result", "fitted blend ≈ prior (95% CI [-0.022,+0.016] nats) → keep the simple prior"]
          ],
          inputs: ["cleaned history (walk-forward splits)"],
          outputs: ["out-of-sample log-loss/Brier", "overfit verdicts"]
        }
      ]
    },
    {
      id: "decision",
      n: "03",
      title: "Decision",
      tag: "blend → edge → stake",
      blurb: "Blend the three signals, filter on edge, size at quarter-Kelly.",
      cards: [
        {
          name: "Blend 1X2",
          role: "Fixed convex combination of Elo + Dixon-Coles + market.",
          file: "src/wca/card.py (BlendWeights)",
          params: [
            ["w_elo", "0.10"],
            ["w_dc", "0.30"],
            ["w_market", "0.60 (market-anchored prior)"],
            ["deployed", "2026-06-18 — shifted DC>Elo on group-stage evidence"],
            ["status", "prior, not fitted — backtest showed no decision-grade lift"]
          ],
          inputs: ["elo_prob", "dc_prob", "market_prob"],
          outputs: ["blended (p_home, p_draw, p_away)"]
        },
        {
          name: "Edge filter (line shop)",
          role: "Best price across books, keep only edges clearing the gate.",
          file: "src/wca/card.py",
          params: [
            ["best_price", "MAX decimal odds across books"],
            ["edge", "p·odds − 1"],
            ["min_edge", "0.02 (2%)"]
          ],
          inputs: ["blended prob", "all book prices"],
          outputs: ["surviving picks + best_book/best_odds"]
        },
        {
          name: "Quarter-Kelly staking",
          role: "Fractional-Kelly stake per pool with hard caps.",
          file: "src/wca/markets/kelly.py",
          params: [
            ["f_full", "(p·o−1)/(o−1)"],
            ["fraction", "0.25 (quarter Kelly)"],
            ["per-trade cap", "0.05 (5%)"],
            ["same-day cap", "0.05 (5%) scaled together"]
          ],
          inputs: ["edge", "pool bankroll"],
          outputs: ["stake per pool", "ranked Recommendations"]
        },
        {
          name: "CLV-gated Kelly ladder",
          role: "WIRED: ledger CLV picks the rung, setting both bankroll and Kelly fraction.",
          file: "src/wca/card.py (resolve_pool_bankroll) · markets/kelly.py",
          params: [
            ["rung0", "£1,500 · c=0.25 (base, until 50 settled w/ close)"],
            ["rung1", "£2,500 · c=0.35 (≥50 settled & to-date CLV>0)"],
            ["rung2", "£5,000 · c=0.50 (≥100 settled & CLV>0; ceiling)"],
            ["demote", "rolling-50 CLV<0 → down one rung"],
            ["live", "build_card sizes off the rung's bankroll + fraction"],
            ["gap left", "longshot filter (odds>10) & arb exemption not yet applied in card"]
          ],
          inputs: ["staking_stats (n_settled, clv_to_date, rolling50_clv)"],
          outputs: ["pool bankroll + Kelly fraction for sizing"]
        },
        {
          name: "Scoreline reconciliation",
          role: "Rescales the DC score matrix so its 1X2 exactly equals the blend.",
          file: "src/wca/models/scores.py",
          params: [
            ["regions", "home h>a / draw h=a / away h<a, one constant each"],
            ["method", "min-KL / max-entropy, shape preserved"],
            ["outputs", "top-6 scores, O/U 1.5/2.5/3.5, BTTS"],
            ["min back price", "clears min_edge 0.02"]
          ],
          inputs: ["DC matrix", "blended 1X2"],
          outputs: ["scoreline ladder (can't contradict picks)"]
        }
      ]
    },
    {
      id: "execution",
      n: "04",
      title: "Execution",
      tag: "human places",
      blurb: "The system never auto-trades. A human places, the bot confirms to ledger.",
      cards: [
        {
          name: "Trade card",
          role: "Ranked recommendations emitted for a human to place.",
          file: "scripts/wca_build_card.py",
          params: [
            ["pool", "main, bankroll = CLV-ladder rung (£1,500 at rung 0)"],
            ["sort", "edge descending"],
            ["sidecars", "scorelines, accas, goalscorers, next-match previews"],
            ["never", "the system does not place trades"]
          ],
          inputs: ["surviving picks + stakes"],
          outputs: ["formatted card → cache → bot / site"]
        },
        {
          name: "Recommendation surfaces",
          role: "Extra +EV signals built alongside the 1X2 card.",
          file: "accas.py · boosts.py · arb.py · exposure.py · offers.py · promos.py",
          params: [
            ["accas", "4+ legs, next 5 matches, ≥2.0 odds/leg"],
            ["boosts", "/boost prices enhanced odds vs model (honest: unpriceable flagged)"],
            ["arb", "cross-book arbs, commission-aware; refuses ET/pens markets"],
            ["exposure", "whole-book P&L per result; blind-spot + gap-plug detection"],
            ["promos / offers", "matched-betting extraction in ISOLATED tables (never the bets ledger)"]
          ],
          inputs: ["model feed", "book/exchange/PM prices", "promo catalog"],
          outputs: ["accas, boost verdicts, arbs, exposure map, offers"]
        },
        {
          name: "Telegram bot",
          role: "Authorized-chat console; reads reports, drives the confirm flow.",
          file: "src/wca/bot/app.py",
          params: [
            ["15 commands", "/summary /bets /clv /card /next /goalscorers /scores /accas /structure /pm /settle /boost /ping /start /help"],
            ["auth", "only TELEGRAM_CHAT_ID; money actions gated to TELEGRAM_ADMIN_USER_ID"],
            ["transport", "long-poll getUpdates 25s, split >4096"]
          ],
          inputs: ["operator commands", "betslip photos"],
          outputs: ["replies", "confirm prompt"]
        },
        {
          name: "Confirm → ledger",
          role: "Parsed slip parked in memory; only a lone 'yes' writes the ledger.",
          file: "src/wca/bot/app.py",
          params: [
            ["pending", "_PENDING_PHOTO_BETS[chat_id], in-memory"],
            ["yes/y", "record_bet per selection (match_id MANUAL_<slug>)"],
            ["tag overrides", "a1/a2/account1/2, model/offer/punt in the reply"],
            ["no/n", "discard; expires on restart"],
            ["admin", "money writes gated to TELEGRAM_ADMIN_USER_ID when set"]
          ],
          inputs: ["ExtractedBet[]", "operator yes/no"],
          outputs: ["bets rows in SQLite"]
        }
      ]
    },
    {
      id: "feedback",
      n: "05",
      title: "Feedback",
      tag: "CLV / calibration / P&L",
      blurb: "Ledger + reports turn settled trades into the north-star KPIs.",
      cards: [
        {
          name: "SQLite ledger",
          role: "bets + bankroll_events + odds_snapshots; computes CLV on close.",
          file: "src/wca/ledger/store.py",
          params: [
            ["db", "data/wca.db (WAL, FK on)"],
            ["settle", "win (o−1)·stake / loss −stake"],
            ["CLV%", "(odds_taken / closing_odds) − 1"],
            ["pool tag", "reason 'pool=<venue>'"]
          ],
          inputs: ["record_bet", "settle_bet", "set_closing_odds"],
          outputs: ["bets", "bankroll_events", "clv"]
        },
        {
          name: "Reports (CLV / calibration)",
          role: "Stateless reports producing the KPI hierarchy.",
          file: "src/wca/ledger/reports.py",
          params: [
            ["clv_report", "avg_clv, pct_beat_close, n_bets"],
            ["calibration", "Brier model vs market-devig, 5 bins"],
            ["staking_stats", "n_settled, clv_to_date, rolling50_clv"],
            ["summary", "P&L, ROI, bankroll"]
          ],
          inputs: ["ledger rows"],
          outputs: ["CLV", "Brier scores", "ladder inputs"]
        },
        {
          name: "Surfaces (site / dashboard)",
          role: "Clock-free builders rendering the ledger + card to static outputs.",
          file: "src/wca/sitedata.py",
          params: [
            ["site/data.json", "totals_by_currency, venues (+currency), clv, positions, predictions"],
            ["site/scores_data.json", "ladders + venue 1X2 (approx_1x2)"],
            ["dashboard", "self-contained HTML, inline SVG, escaped"]
          ],
          inputs: ["data/wca.db", "data/card_latest.md"],
          outputs: ["terminal site", "scores page", "HTML dashboard"]
        }
      ]
    }
  ];

  // KPI hierarchy (shown under the money strip, mirrors §6).
  var KPIS = [
    ["1", "Closing Line Value", "did we beat the close? — primary"],
    ["2", "Calibration", "Brier vs the de-vigged market"],
    ["3", "Bankroll", "lagging, noisy at this sample size"]
  ];

  // Money flow: ONE combined bankroll (refreshed 2026-07-13 — this used to
  // show three separate pools incl. a "planned" Kalshi pool; the sizing
  // model consolidated onto a single pot per src/wca/markets/bankroll.py
  // and no Kalshi client was ever built).
  var POOLS = [
    {
      cls: "sportsbook",
      name: "Sportsbook (GBP)",
      amt: "£3,000",
      sub: "combined base ± realised P&L",
      notes: ["same pot as Polymarket below, FX'd — never sized as a separate £3,000"]
    },
    {
      cls: "polymarket",
      name: "Polymarket (USD)",
      amt: "$1.33 = £1",
      sub: "project FX rate",
      notes: ["quarter-Kelly of the ONE combined bankroll, expressed in USD"]
    }
  ];

  var MONEY_STEPS = [
    ["Pool", "one combined bankroll, GBP-denominated, FX'd for Polymarket"],
    ["Laddered Kelly + caps", "quarter-Kelly of the combined total, 5% per-trade, 5% same-day"],
    ["Trades", "human places, bot confirms to ledger"],
    ["Settle", "win (o−1)·stake / loss −stake"],
    ["CLV feedback", "(odds_taken / closing_odds) − 1 → sets the next rung"]
  ];

  var KILL_RULE =
    "KILL RULE — real-money sportsbook pauses if CLV is negative after ~50 " +
    "settled trades. Free / promo bets risk no cash: they count toward max win but " +
    "contribute zero to max loss.";

  // Improvement map per stage (§9). Refreshed 2026-07-13 against current
  // code (was showing Kalshi as a live "next" item and Monte Carlo
  // advancement as unshipped — both stale; see git history for evidence).
  var IMPROVE = [
    {
      stage: "Ingestion",
      items: [
        "SHIPPED: cleaning overlay + 2-source (ESPN + TheSportsDB) verification → martj42_cleaned.csv, 3×/day CI.",
        "SHIPPED: Hyperliquid HIP-4 cross-venue monitor vs Polymarket (src/wca/hl/*) — read-only, watcher-only; emits watch labels, never trades.",
        "REMOVED: the Kalshi third-pool plan — sizing consolidated onto one combined bankroll (see Money Flow); no Kalshi client was built.",
        "Betfair exchange as a sharper CLV reference — still NO-BUILD per ADR-003 (standing decision); read-only reference at most.",
        "Totals / correct-score market ingestion (get_odds already parses outcome_point) — still unused."
      ]
    },
    {
      stage: "Modeling",
      items: [
        "SHIPPED: shrink-to-market — the blended prob is shrunk toward the de-vigged market reference before it drives edge/EV/sizing (kill-switch WCA_SHRINK_LIVE, promoted from shadow to LIVE 2026-07-09).",
        "SHIPPED: player-level rate infra (players.db) feeding bet-builder leg pricing. Next: fold it into the main 1X2/DC blend, not just bet-builder legs — the biggest remaining accuracy lever.",
        "SHIPPED (opt-in, off): venue/altitude-aware host advantage — co-host dilution + Azteca altitude tax. Next: altitude/heat into the DC log-mean too.",
        "SHIPPED (opt-in, off): structural socio-economic shrinkage prior for low-data minnows — holdout inconclusive, awaits live 2026 minnow data.",
        "SHIPPED: walk-forward half-life backtest + blend-fit holdout (fitted ≈ 0.10/0.30/0.60 prior, no decision-grade lift) + structural-bias tests."
      ]
    },
    {
      stage: "Decision",
      items: [
        "SHIPPED: CLV-gated ladder wired into build_card — rung sets bankroll AND Kelly fraction.",
        "SHIPPED: sizing now acts on the shrunk (market-anchored) blend, not the raw one — see Modeling.",
        "Gap left: apply the rung-0 longshot filter (odds>10) and arb-exemption inside build_card.",
        "Promo / boost EV models (2-Up, super-sub) — needs goal-timing distributions."
      ]
    },
    {
      stage: "Bankroll & ledger",
      items: [
        "SHIPPED: auto-populate closing_odds from odds_snapshots (closecapture.py) — closes the CLV loop, no manual entry.",
        "Gap left: pushed-bet ledger write is still ack-only for `Y BET-<id>` (PM-<n> orders already write a real ledger confirm)."
      ]
    },
    {
      stage: "In-play / surfaces",
      items: [
        "SHIPPED: tournament Monte Carlo advancement sim (site/advancement_data.json) now live-prices outright/advancement and drives the Team-to-Advance markets end to end.",
        "In-play models fed by the 3-min in-game snapshots (live 1X2 / next-goal) — still planned.",
        "Real correct-score book prices into the scores page (drop approx_1x2) — still planned."
      ]
    }
  ];

  // ======================================================================
  //  RENDER — pipeline SVG
  // ======================================================================

  // Hand-built inline SVG: five boxes + connecting arrows, terminal-styled.
  // Clicking a box scrolls to its stage section. viewBox scales to width.
  function renderPipeline() {
    var n = STAGES.length;
    var vw = 1000, vh = 120;
    var pad = 8;
    var gap = 14;
    var boxW = (vw - pad * 2 - gap * (n - 1)) / n;
    var boxH = 74;
    var boxY = 24;

    var defs =
      '<defs>' +
        '<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" ' +
          'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
          '<path d="M0 0 L10 5 L0 10 z" fill="var(--text-dim)"></path>' +
        '</marker>' +
      '</defs>';

    var parts = [defs];

    for (var i = 0; i < n; i++) {
      var s = STAGES[i];
      var x = pad + i * (boxW + gap);
      var cx = x + boxW / 2;

      // connecting arrow from previous box
      if (i > 0) {
        var ax1 = x - gap + 2;
        var ax2 = x - 2;
        var ay = boxY + boxH / 2;
        parts.push(
          '<line x1="' + (ax1 - 4) + '" y1="' + ay + '" x2="' + ax2 + '" y2="' + ay +
          '" stroke="var(--text-dim)" stroke-width="1.5" marker-end="url(#arrow)"></line>'
        );
      }

      parts.push(
        '<g class="pipe-stage" tabindex="0" role="button" ' +
          'data-stage="' + esc(s.id) + '" ' +
          'aria-label="Jump to ' + esc(s.title) + ' stage">' +
          '<rect x="' + x + '" y="' + boxY + '" width="' + boxW + '" height="' + boxH +
            '" rx="3" class="pipe-box"></rect>' +
          '<text x="' + cx + '" y="' + (boxY + 22) + '" class="pipe-n" ' +
            'text-anchor="middle">' + esc(s.n) + '</text>' +
          '<text x="' + cx + '" y="' + (boxY + 44) + '" class="pipe-title" ' +
            'text-anchor="middle">' + esc(s.title.toUpperCase()) + '</text>' +
          '<text x="' + cx + '" y="' + (boxY + 60) + '" class="pipe-tag" ' +
            'text-anchor="middle">' + esc(s.tag) + '</text>' +
        '</g>'
      );
    }

    var svg =
      '<svg class="pipe-svg" viewBox="0 0 ' + vw + ' ' + vh +
        '" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg" ' +
        'role="img" aria-label="Five-stage pipeline: ingestion, models, decision, execution, feedback">' +
        parts.join("") +
      '</svg>';

    var pipe = $("pipeline");
    pipe.innerHTML = svg;

    // Wire click + keyboard to scroll to the stage section.
    function jump(id) {
      var el = document.getElementById("stage-" + id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    var groups = pipe.querySelectorAll(".pipe-stage");
    Array.prototype.forEach.call(groups, function (g) {
      g.addEventListener("click", function () { jump(g.getAttribute("data-stage")); });
      g.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          jump(g.getAttribute("data-stage"));
        }
      });
    });
  }

  // ======================================================================
  //  RENDER — stage detail sections
  // ======================================================================

  function chip(text, kind) {
    return '<span class="arch-chip ' + (kind || "") + '">' + esc(text) + '</span>';
  }

  function renderCard(c) {
    var params = (c.params || []).map(function (p) {
      return '<div class="arch-param">' +
        '<span class="arch-pk">' + esc(p[0]) + '</span>' +
        '<span class="arch-pv">' + esc(p[1]) + '</span>' +
      '</div>';
    }).join("");

    var ins = (c.inputs || []).map(function (t) { return chip(t, "chip-in"); }).join("");
    var outs = (c.outputs || []).map(function (t) { return chip(t, "chip-out"); }).join("");

    return '<div class="arch-card">' +
      '<div class="arch-card-name">' + esc(c.name) + '</div>' +
      '<div class="arch-card-role">' + esc(c.role) + '</div>' +
      '<div class="arch-params">' + params + '</div>' +
      '<div class="arch-io">' +
        '<div class="arch-io-row">' +
          '<span class="arch-io-lbl">in</span>' + ins +
        '</div>' +
        '<div class="arch-io-arrow">↓</div>' +
        '<div class="arch-io-row">' +
          '<span class="arch-io-lbl">out</span>' + outs +
        '</div>' +
      '</div>' +
      '<code class="arch-file">' + esc(c.file) + '</code>' +
    '</div>';
  }

  function renderStages() {
    var html = STAGES.map(function (s) {
      var cards = (s.cards || []).map(renderCard).join("");
      return '<section class="panel arch-stage" id="stage-' + esc(s.id) + '">' +
        '<div class="panel-head">' +
          '<span class="panel-label">' +
            '<span class="arch-stage-n">' + esc(s.n) + '</span> ' +
            esc(s.title) + ' // ' + esc(s.tag) +
          '</span>' +
          '<a class="arch-top" href="#pipeline">↑ pipeline</a>' +
        '</div>' +
        '<div class="panel-body">' +
          '<p class="arch-blurb">' + esc(s.blurb) + '</p>' +
          '<div class="arch-card-grid">' + cards + '</div>' +
        '</div>' +
      '</section>';
    }).join("");
    $("stages").innerHTML = html;
  }

  // ======================================================================
  //  RENDER — money flow strip
  // ======================================================================

  function renderMoney() {
    var pools = POOLS.map(function (p) {
      var notes = (p.notes || []).map(function (t) {
        return '<li>' + esc(t) + '</li>';
      }).join("");
      return '<div class="money-pool money-' + esc(p.cls) + '">' +
        '<div class="money-pool-top">' +
          '<span class="money-pool-name">' + esc(p.name) + '</span>' +
          '<span class="money-pool-amt num">' + esc(p.amt) + '</span>' +
        '</div>' +
        '<div class="money-pool-sub">' + esc(p.sub) + '</div>' +
        '<ul class="money-pool-notes">' + notes + '</ul>' +
      '</div>';
    }).join("");

    var steps = MONEY_STEPS.map(function (st, i) {
      var arrow = i < MONEY_STEPS.length - 1
        ? '<span class="money-arrow">→</span>' : '';
      return '<span class="money-step">' +
          '<span class="money-step-h">' + esc(st[0]) + '</span>' +
          '<span class="money-step-d">' + esc(st[1]) + '</span>' +
        '</span>' + arrow;
    }).join("");

    var kpis = KPIS.map(function (k) {
      return '<div class="kpi-row">' +
        '<span class="kpi-rank">' + esc(k[0]) + '</span>' +
        '<span class="kpi-name">' + esc(k[1]) + '</span>' +
        '<span class="kpi-desc">' + esc(k[2]) + '</span>' +
      '</div>';
    }).join("");

    $("money").innerHTML =
      '<div class="money-pools">' + pools + '</div>' +
      '<div class="money-flow">' + steps + '</div>' +
      '<div class="money-kill">' + esc(KILL_RULE) + '</div>' +
      '<div class="kpi-block">' +
        '<div class="kpi-head">KPI Hierarchy</div>' + kpis +
      '</div>';
  }

  // ======================================================================
  //  RENDER — improvement map
  // ======================================================================

  function renderImprove() {
    var html = IMPROVE.map(function (g) {
      var items = (g.items || []).map(function (t) {
        return '<li>' + esc(t) + '</li>';
      }).join("");
      return '<div class="imp-group">' +
        '<div class="imp-stage">' + esc(g.stage) + '</div>' +
        '<ul class="imp-list">' + items + '</ul>' +
      '</div>';
    }).join("");
    $("improve").innerHTML = '<div class="imp-grid">' + html + '</div>';
  }

  // ======================================================================
  //  BOOT
  // ======================================================================

  function boot() {
    $("arch-lead").textContent = LEAD;
    $("arch-mental").innerHTML =
      '<span class="arch-mental-lbl">mental model</span> ' +
      '<code class="arch-mental-code">' + esc(MENTAL) + '</code>';
    renderPipeline();
    renderStages();
    renderMoney();
    renderImprove();
    $("foot-gen").textContent =
      "Static documentation — mirrors docs/architecture/SYSTEM_MAP.md (code-verified)";
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
