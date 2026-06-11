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
    "We bet the 2026 World Cup as a disciplined quant. Three feeds come in — " +
    "historical international results (martj42), live bookmaker odds (The Odds " +
    "API, UK region) and Polymarket prices. Two models are fitted on the history " +
    "— an Elo rating with an ordered-logit 1X2, and a time-decayed Dixon-Coles " +
    "goals model. Each fixture's books are de-vigged with Shin, the per-column " +
    "median is taken as market consensus, then Elo + Dixon-Coles + market are " +
    "blended into one (home, draw, away) — market weighted 50% because the market " +
    "is hard to beat. We line-shop the best price per outcome, keep only edges " +
    "clearing 2%, size at quarter-Kelly capped 5% per bet and 5% same-day. The " +
    "system never places a bet — it emits a card; a human places it, screenshots " +
    "the slip, and the Telegram bot reads it via Claude vision into the SQLite " +
    "ledger after a yes. A snapshot daemon records the closing line so the ledger " +
    "can compute CLV — did we beat the close?";

  var MENTAL =
    "history + market → blend → edge filter → quarter-Kelly card " +
    "→ human places → ledger → CLV";

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
          name: "Historical results",
          role: "martj42 results.csv — the model training history.",
          file: "src/wca/data/results.py",
          params: [
            ["dest", "data/raw/results.csv"],
            ["refresh", "skip if mtime == today (UTC)"],
            ["fields", "date, teams, scores Int64, tournament, city, country, neutral"]
          ],
          inputs: ["GitHub raw CSV"],
          outputs: ["played-match frame", "outcome H/D/A"]
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
            ["note", "NOT in the bet-card blend"]
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
      tag: "fit on history",
      blurb: "Two models fitted on history plus the de-vigged market baseline.",
      cards: [
        {
          name: "International Elo",
          role: "Rating engine + ordered-logit (proportional-odds) 1X2.",
          file: "src/wca/models/elo.py",
          params: [
            ["initial", "1500.0"],
            ["home adv", "100.0 (+host on neutral)"],
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
            ["rho", "fitted, applied to (0,0)(0,1)(1,0)(1,1)"]
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
          file: "src/wca/card.py",
          params: [
            ["w_elo", "0.25"],
            ["w_dc", "0.25"],
            ["w_market", "0.50 (market-anchored prior)"],
            ["status", "unfitted prior; backtest deferred"]
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
            ["per-bet cap", "0.05 (5%)"],
            ["same-day cap", "0.05 (5%) scaled together"]
          ],
          inputs: ["edge", "pool bankroll"],
          outputs: ["stake per pool", "ranked Recommendations"]
        },
        {
          name: "Kelly ladder (gap)",
          role: "CLV-gated promotion — defined & tested, NOT yet wired into build_card.",
          file: "src/wca/markets/kelly.py",
          params: [
            ["rung0", "c=0.25 until 50 settled w/ closing odds"],
            ["rung1", "c=0.35 (≥50 settled & CLV>0)"],
            ["rung2", "c=0.50 (≥100 settled & CLV>0)"],
            ["demote", "rolling-50 CLV<0 → down one rung"],
            ["max_odds_unvalidated", "10.0 (longshot filter on rung0)"]
          ],
          inputs: ["staking_stats (n_settled, clv_to_date, rolling50_clv)"],
          outputs: ["would set fraction + longshot filter"]
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
      blurb: "The system never auto-bets. A human places, the bot confirms to ledger.",
      cards: [
        {
          name: "Bet card",
          role: "Ranked recommendations emitted for a human to place.",
          file: "scripts/wca_build_card.py",
          params: [
            ["pool", "name=main, bankroll=1000.0"],
            ["sort", "edge descending"],
            ["never", "the system does not place bets"]
          ],
          inputs: ["surviving picks + stakes"],
          outputs: ["formatted card → cache → bot / site"]
        },
        {
          name: "Telegram bot",
          role: "Authorized-chat console; reads reports, drives the confirm flow.",
          file: "src/wca/bot/app.py",
          params: [
            ["commands", "8: /summary /bets /clv /card /scores /structure /ping /help"],
            ["auth", "only TELEGRAM_CHAT_ID; others get their id, no data"],
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
            ["no/n", "discard; expires on restart"],
            ["pushed-bet", "Y BET-<id> ack only (wiring pending)"]
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
      blurb: "Ledger + reports turn settled bets into the north-star KPIs.",
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

  // Money flow: pools + checkpoints, sizing, kill rule.
  var POOLS = [
    {
      cls: "sportsbook",
      name: "Sportsbook",
      amt: "£1,000",
      sub: "notional",
      notes: [
        "scales → £2.5k at ~20 settled bets (avg CLV ≥ 0)",
        "scales → £5k at ~50 settled bets"
      ]
    },
    {
      cls: "polymarket",
      name: "Polymarket",
      amt: "$1,310",
      sub: "actual USD",
      notes: ["price feed is scores-page enrichment only"]
    },
    {
      cls: "kalshi",
      name: "Kalshi",
      amt: "—",
      sub: "planned",
      notes: ["pool plumbed through venue rollups, client TBD"]
    }
  ];

  var MONEY_STEPS = [
    ["Pools", "sportsbook £ / polymarket $ / kalshi planned"],
    ["Quarter-Kelly + caps", "f × 0.25, 5% per-bet, 5% same-day"],
    ["Bets", "human places, bot confirms to ledger"],
    ["Settle", "win (o−1)·stake / loss −stake"],
    ["CLV feedback", "(odds_taken / closing_odds) − 1"]
  ];

  var KILL_RULE =
    "KILL RULE — real-money sportsbook pauses if CLV is negative after ~50 " +
    "settled bets. Free / promo bets risk no cash: they count toward max win but " +
    "contribute zero to max loss.";

  // Improvement map per stage (§9).
  var IMPROVE = [
    {
      stage: "Ingestion",
      items: [
        "Kalshi client mirroring the Polymarket reader — lights up the third pool.",
        "Betfair exchange as a sharper CLV reference and eventual execution venue.",
        "Totals / correct-score market ingestion (get_odds already parses outcome_point)."
      ]
    },
    {
      stage: "Modeling",
      items: [
        "Player-level ratings → lineup-aware DC inputs (biggest accuracy lever).",
        "Weather / altitude covariates for a tri-country 2026 tournament.",
        "Fit the blend weights on a calibration backtest (replace the 0.25/0.25/0.50 prior).",
        "Backtest harness to pick DC xi (now hand-set 8y) and ridge by out-of-sample log-loss."
      ]
    },
    {
      stage: "Decision",
      items: [
        "Wire KellyPolicy into build_card (gap): rung promotion, longshot filter, arb exemption.",
        "Promo / boost EV models (2-Up, super-sub) — needs goal-timing distributions.",
        "Arbitrage / middling detection across the line-shopped book set."
      ]
    },
    {
      stage: "Bankroll & ledger",
      items: [
        "Auto-populate closing_odds from odds_snapshots — closes the CLV loop, no manual entry.",
        "Finish pushed-bet ledger write (Y BET-<id> currently ack-only)."
      ]
    },
    {
      stage: "In-play / surfaces",
      items: [
        "In-play models fed by the 3-min in-game snapshots (live 1X2 / next-goal).",
        "Real correct-score book prices into the scores page (drop approx_1x2).",
        "Tournament Monte Carlo with an Elo/DC prob_fn to price outright / advancement."
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
