/* World Cup Alpha — Analytics dashboard renderer.
 * Pure vanilla JS, inline SVG, no dependencies. Every section degrades to an
 * empty / "pending" / "insufficient sample" state rather than throwing.
 *
 * Feeds (all under ./data/):
 *   existing  : data.json, tracking_data.json, exposure_data.json,
 *               mc_futures.json, advancement_history.json
 *   new (P0+) : predledger.json, winrate.json, tracking_clv_benchmark.json,
 *               rigor.json, risk_pnl.json
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var FEEDS = {};

  // ---- formatting ---------------------------------------------------------
  function num(v, dp) { if (v == null || isNaN(v)) return "—"; return Number(v).toFixed(dp == null ? 2 : dp); }
  function signed(v, dp) { if (v == null || isNaN(v)) return "—"; var n = Number(v); return (n >= 0 ? "+" : "") + n.toFixed(dp == null ? 2 : dp); }
  function pct(v, dp) { if (v == null || isNaN(v)) return "—"; return (Number(v) * 100).toFixed(dp == null ? 1 : dp) + "%"; }
  // value is ALREADY a percentage (0..100), not a fraction — do not re-scale.
  function pctp(v, dp) { if (v == null || isNaN(v)) return "—"; return Number(v).toFixed(dp == null ? 1 : dp) + "%"; }
  function covWidth(v) { return Math.round(Math.min(100, Math.max(0, Number(v) || 0))); } // coverage_pct (0..100) -> bar width%
  function signedPct(v, dp) { if (v == null || isNaN(v)) return "—"; var n = Number(v) * 100; return (n >= 0 ? "+" : "") + n.toFixed(dp == null ? 1 : dp) + "%"; }
  function money(v, cur) { if (v == null || isNaN(v)) return "—"; var s = (cur === "USD" ? "$" : "£"); var n = Number(v); return (n < 0 ? "-" : "") + s + Math.abs(n).toFixed(2); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }
  function clsSign(v) { return (Number(v) >= 0) ? "pos" : "neg"; }

  // ---- generic svg --------------------------------------------------------
  function svg(w, h, body) {
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" class="a-svg" preserveAspectRatio="xMidYMid meet" ' +
      'xmlns="http://www.w3.org/2000/svg" role="img">' + body + "</svg>";
  }
  function empty(msg, pending) { return '<div class="' + (pending ? "a-empty a-pending" : "a-empty") + '">' + esc(msg) + "</div>"; }
  function pendingMsg(name) { return empty(name + " — feed pending (backend builder still running or not yet published)", true); }

  function kpi(label, valueHtml, sub) {
    return '<div class="kpi"><span class="k-label">' + esc(label) + "</span>" +
      '<span class="k-value">' + valueHtml + "</span>" +
      (sub ? '<span class="k-sub">' + sub + "</span>" : "") + "</div>";
  }

  // wilson sub-line "[lo, hi] · n=.."
  function ciSub(o) {
    if (!o || o.n == null) return "";
    if (o.lo == null || o.hi == null) return "n=" + o.n;
    return '<span class="k-ci">[' + pct(o.lo, 0) + ", " + pct(o.hi, 0) + "]</span> &middot; n=" + o.n;
  }

  // =========================================================================
  //  TOP KPI STRIP  (data.json + tracking_data.json)
  // =========================================================================
  function renderKpi() {
    var d = FEEDS.data, t = FEEDS.tracking;
    if (!d && !t) { $("kpi").innerHTML = pendingMsg("Headline"); return; }
    var totals = (d && d.totals) || {};
    var clv = (d && d.clv) || {};
    var s = (t && t.summary) || {};
    var bets = s.bets || {};
    var roi = (totals.wagered ? totals.settled_pl / totals.wagered : null);

    var brierHtml = "—", brierSub = "";
    if (s.model_brier != null) {
      var better = s.market_brier != null && s.model_brier <= s.market_brier;
      brierHtml = '<span class="' + (better ? "pos" : "neg") + '">' + num(s.model_brier, 3) + "</span>" +
        '<span class="dim"> vs ' + num(s.market_brier, 3) + "</span>";
      brierSub = "model vs market &middot; lower better";
    }

    $("kpi").innerHTML =
      kpi("Settled Bets", String(bets.settled || totals.n_bets || 0),
        (bets.won || 0) + "W " + (bets.lost || 0) + "L &middot; all pools") +
      kpi("Realized P&amp;L", '<span class="' + clsSign(totals.settled_pl) + '">' + signed(totals.settled_pl) + "</span>",
        "across venues (faceted)") +
      kpi("ROI", '<span class="' + clsSign(roi) + '">' + signedPct(roi) + "</span>",
        money(totals.wagered) + " wagered") +
      kpi("Avg CLV", '<span class="' + clsSign(clv.avg_clv) + '">' + signedPct(clv.avg_clv) + "</span>",
        (clv.n_with_close || 0) + " with captured close") +
      kpi("Beat Close (placed)", pct(clv.pct_beat_close, 0),
        "placed book only &middot; n=" + (clv.n_with_close || 0) + " &middot; unbiased signal is full-book CLV below") +
      kpi("Brier 1X2", brierHtml, brierSub) +
      kpi("Fixtures Scored", String(s.fixtures_complete || 0),
        (s.model_1x2_correct || 0) + "/" + (s.fixtures_complete || 0) + " model picks");

    var gen = (d && d.meta && d.meta.generated) || (t && t.meta && t.meta.generated) || "";
    $("kpi-meta").textContent = gen ? "as of " + gen.slice(0, 16).replace("T", " ") + " UTC" : "";
    $("intro-meta").textContent = gen ? gen.slice(0, 10) : "";
  }

  // =========================================================================
  //  D · VERDICT  (rigor.json)
  // =========================================================================
  function renderVerdict() {
    var r = FEEDS.rigor;
    if (!r || !r.verdict) { $("verdict-body").innerHTML = pendingMsg("Verdict"); return; }
    var v = r.verdict, m = r.meta || {};
    var lightCls = { grey: "vl-grey", amber: "vl-amber", green: "vl-green", red: "vl-red" }[v.color] || "vl-grey";
    $("verdict-body").innerHTML =
      '<div class="verdict-banner">' +
        '<div class="verdict-light ' + lightCls + '"></div>' +
        '<div class="verdict-text">' +
          '<div class="v-level">' + esc(v.label || v.level) +
            '<span class="v-chip">n=' + (m.n != null ? m.n : "—") +
            " &middot; n_eff=" + (m.n_eff != null ? num(m.n_eff, 1) : "—") +
            (m.stage ? " &middot; " + esc(m.stage) : "") + "</span></div>" +
          '<div class="v-reason">' + esc(v.reason || "") + "</div>" +
        "</div>" +
      "</div>";

    // gates table
    var gates = r.gates || [];
    if (!gates.length) { $("verdict-gates").innerHTML = ""; }
    else {
      var rows = gates.map(function (g) {
        var cls = g.pass === true ? "gate-pass" : g.pass === false ? "gate-fail" : "gate-na";
        var mark = g.pass === true ? "✓ PASS" : g.pass === false ? "✗ FAIL" : "— N/A";
        var val = (g.value == null) ? "—" : (typeof g.value === "number" ? num(g.value, 3) : esc(g.value));
        return "<tr><td class='gate-id'>" + esc(g.id) + "</td><td>" + esc(g.name) +
          "</td><td class='r'>" + val + "</td><td>" + esc(g.threshold || "") +
          "</td><td class='r " + cls + "'>" + mark + "</td></tr>";
      }).join("");
      $("verdict-gates").innerHTML =
        "<table class='gates'><thead><tr><th>Gate</th><th>Statistic</th><th class='r'>Value</th>" +
        "<th>Threshold</th><th class='r'>Result</th></tr></thead><tbody>" + rows + "</tbody></table>";
    }

    // samples-to-significance teaching note
    var st = r.samples_to_sig;
    if (st) {
      $("verdict-samples").innerHTML =
        "<div style='font-family:var(--mono);font-size:11px;color:var(--text-dim);line-height:1.7'>" +
        "Samples to significance &mdash; <b style='color:var(--text)'>CLV ≈ " + (st.clv_n || 25) +
        "</b> clustered (leading, low-variance) vs <b style='color:var(--text)'>ROI ≈ " + (st.roi_n || 3860) +
        "</b> (lagging, outcome-variance). Current effective sample: <b class='" +
        ((st.current_n_eff || 0) >= (st.clv_n || 25) ? "pos" : "neg") + "'>n_eff = " + num(st.current_n_eff, 1) +
        "</b>. That ~150&times; gap is why CLV leads the verdict and ROI lags.</div>";
    }
  }

  // =========================================================================
  //  A · RISK & P&L  (risk_pnl.json)  + exposure_data.json
  // =========================================================================
  function renderRisk() {
    var r = FEEDS.risk;
    if (!r || !r.distribution_gbp) { $("risk-dist").innerHTML = pendingMsg("Risk / P&L distribution"); }
    else {
      var dist = r.distribution_gbp, hist = r.histogram || [], m = r.meta || {};
      $("risk-meta").innerHTML = "Monte-Carlo over " + (m.n_sims || 0).toLocaleString() + " sims &middot; " +
        (m.n_open_positions || 0) + " open positions &middot; <span class='dim'>" + esc(m.fx_note || "FX-disclosed view") + "</span>";
      var skew = (dist.mean != null && dist.median != null && dist.mean > dist.median + 5)
        ? "<div class='cx-lbl' style='padding:8px 2px 0;font-size:10px;line-height:1.5'>Mean (" + money(dist.mean) +
          ") is right-skewed by tail wins; the typical outcome is the median (" + money(dist.median) +
          ") and P(book down) = " + pct(dist.p_book_down, 0) + ". " + esc(m.fx_note || "") + "</div>"
        : "";
      $("risk-dist").innerHTML = histogramSvg(hist, dist) + riskStatRow(dist) + skew;
    }
    renderRiskTeams(r);
    renderRiskExposure();
  }

  function riskStatRow(d) {
    function cell(lbl, v, cls) {
      return "<div class='kpi'><span class='k-label'>" + lbl + "</span><span class='k-value'><span class='" +
        (cls || "") + "'>" + money(v) + "</span></span></div>";
    }
    return "<div class='kpi-grid' style='margin-top:10px'>" +
      cell("Mean P&amp;L", d.mean, clsSign(d.mean)) +
      cell("Median", d.median, clsSign(d.median)) +
      cell("P5", d.p5, clsSign(d.p5)) +
      cell("P95", d.p95, clsSign(d.p95)) +
      cell("VaR 95%", -Math.abs(d.var95), "neg") +
      cell("CVaR 95%", -Math.abs(d.cvar95), "neg") +
      "<div class='kpi'><span class='k-label'>P(book down)</span><span class='k-value'>" + pct(d.p_book_down, 0) + "</span></div>" +
      cell("Hard floor", (d.hard_floor != null ? (d.hard_floor > 0 ? -d.hard_floor : d.hard_floor) : null), "neg") + "</div>";
  }

  function histogramSvg(hist, dist) {
    if (!hist || !hist.length) return empty("No simulated distribution");
    var W = 920, H = 240, P = { l: 44, r: 14, t: 14, b: 28 };
    var maxC = Math.max.apply(null, hist.map(function (b) { return b.count; }));
    var lo = hist[0].bin_lo, hi = hist[hist.length - 1].bin_hi;
    var span = (hi - lo) || 1;
    var xOf = function (val) { return P.l + (val - lo) / span * (W - P.l - P.r); };
    var yOf = function (c) { return H - P.b - (c / maxC) * (H - P.t - P.b); };
    var parts = [];
    hist.forEach(function (b) {
      var x = xOf(b.bin_lo), w = Math.max(1, xOf(b.bin_hi) - xOf(b.bin_lo) - 1);
      var cls = b.bin_hi <= 0 ? "bar-neg" : b.bin_lo >= 0 ? "bar-pos" : "bar-neu";
      parts.push("<rect class='" + cls + "' x='" + x.toFixed(1) + "' y='" + yOf(b.count).toFixed(1) +
        "' width='" + w.toFixed(1) + "' height='" + (yOf(0) - yOf(b.count)).toFixed(1) + "'/>");
    });
    // zero line
    if (lo < 0 && hi > 0) parts.push("<line class='cx-zero' x1='" + xOf(0) + "' y1='" + P.t + "' x2='" + xOf(0) + "' y2='" + (H - P.b) + "'/>");
    // markers: VaR / CVaR / hard floor
    function marker(val, label, color) {
      if (val == null || val < lo || val > hi) return;
      var x = xOf(val);
      parts.push("<line x1='" + x + "' y1='" + P.t + "' x2='" + x + "' y2='" + (H - P.b) + "' stroke='" + color + "' stroke-width='1.2' stroke-dasharray='3 2'/>");
      parts.push("<text class='cx-tick' x='" + x + "' y='" + (P.t + 9) + "' text-anchor='middle' fill='" + color + "'>" + label + "</text>");
    }
    // VaR/CVaR are loss magnitudes in the feed → plot on the loss (negative) side.
    marker(-Math.abs(dist.var95), "VaR", "var(--warn)");
    marker(-Math.abs(dist.cvar95), "CVaR", "var(--neg)");
    marker(dist.hard_floor != null ? (dist.hard_floor > 0 ? -dist.hard_floor : dist.hard_floor) : null, "floor", "var(--neg)");
    marker(dist.mean, "µ", "var(--accent)");
    // x ticks
    [lo, lo + span / 2, hi].forEach(function (v) {
      parts.push("<text class='cx-tick' x='" + xOf(v) + "' y='" + (H - 10) + "' text-anchor='middle'>" + money(v) + "</text>");
    });
    parts.push("<text class='cx-lbl' x='" + (W / 2) + "' y='" + (H - 1) + "' text-anchor='middle'>open-book P&amp;L (£, FX-disclosed)</text>");
    return svg(W, H, parts.join(""));
  }

  function renderRiskTeams(r) {
    var teams = (r && r.per_team) || [];
    if (!teams.length) { $("risk-teams").innerHTML = empty("No per-team contribution"); return; }
    teams = teams.slice().sort(function (a, b) { return Math.abs(b.ev_contribution) - Math.abs(a.ev_contribution); }).slice(0, 12);
    var maxA = Math.max.apply(null, teams.map(function (t) { return Math.abs(t.ev_contribution); })) || 1;
    $("risk-teams").innerHTML = hbar(teams.map(function (t) {
      return { label: t.team, value: t.ev_contribution, max: maxA, fmt: money };
    }));
  }

  function renderRiskExposure() {
    var e = FEEDS.exposure;
    if (!e) { $("risk-exposure").innerHTML = empty("No exposure feed"); return; }
    var p = e.portfolio || {};
    var fx = (e.fixtures || []).length;
    $("risk-exposure").innerHTML =
      "<table class='a-table'><tbody>" +
      "<tr><td>Scenario EV</td><td class='r " + clsSign(p.ev) + "'>" + signed(p.ev) + "</td></tr>" +
      "<tr><td>Best case</td><td class='r pos'>" + signed(p.best) + "</td></tr>" +
      "<tr><td>Worst case (hard floor)</td><td class='r neg'>" + signed(p.worst) + "</td></tr>" +
      "<tr><td>P(profit)</td><td class='r'>" + pct(p.p_profit, 0) + "</td></tr>" +
      "<tr><td>P(loss)</td><td class='r'>" + pct(p.p_loss, 0) + "</td></tr>" +
      "<tr><td>Fixtures in slate</td><td class='r'>" + fx + "</td></tr>" +
      "<tr><td>Scenarios</td><td class='r'>" + (p.n_scenarios || 0) + "</td></tr>" +
      "</tbody></table>";
  }

  // generic horizontal bar list (diverging around 0)
  function hbar(items) {
    if (!items.length) return empty("No data");
    var rowH = 22, W = 440, H = items.length * rowH + 8, lblW = 120, mid = lblW + (W - lblW) / 2;
    var parts = ["<line class='cx-zero' x1='" + mid + "' y1='2' x2='" + mid + "' y2='" + (H - 4) + "'/>"];
    items.forEach(function (it, i) {
      var y = i * rowH + 4, half = (W - lblW) / 2 - 8;
      var w = Math.abs(it.value) / it.max * half;
      var x = it.value >= 0 ? mid : mid - w;
      var cls = it.value >= 0 ? "bar-pos" : "bar-neg";
      parts.push("<text class='cx-lbl' x='" + (lblW - 6) + "' y='" + (y + 14) + "' text-anchor='end'>" + esc(it.label) + "</text>");
      parts.push("<rect class='" + cls + "' x='" + x.toFixed(1) + "' y='" + (y + 4) + "' width='" + Math.max(1, w).toFixed(1) + "' height='12' rx='1'/>");
      parts.push("<text class='cx-tick' x='" + (it.value >= 0 ? (x + w + 3) : (x - 3)) + "' y='" + (y + 14) + "' text-anchor='" + (it.value >= 0 ? "start" : "end") + "'>" + (it.fmt ? it.fmt(it.value) : num(it.value, 2)) + "</text>");
    });
    return svg(W, H, parts.join(""));
  }

  // =========================================================================
  //  A · FUTURES FOREST  (mc_futures.json)
  // =========================================================================
  var forestStage = null;
  function renderForest() {
    var mc = FEEDS.mc;
    if (!mc || !mc.markets) { $("forest").innerHTML = pendingMsg("Futures forest"); return; }
    var stages = Object.keys(mc.markets);
    if (!stages.length) { $("forest").innerHTML = empty("No futures markets"); return; }
    if (!forestStage || stages.indexOf(forestStage) < 0) {
      forestStage = stages.indexOf("win") >= 0 ? "win" : stages[0];
    }
    $("forest-nav").innerHTML = stages.map(function (s) {
      var label = (mc.markets[s].label) || s;
      return "<span class='chip" + (s === forestStage ? " active" : "") + "' data-stage='" + esc(s) + "'>" + esc(label) + "</span>";
    }).join("");
    Array.prototype.forEach.call($("forest-nav").querySelectorAll(".chip"), function (c) {
      c.addEventListener("click", function () { forestStage = c.getAttribute("data-stage"); renderForest(); });
    });
    var mk = mc.markets[forestStage];
    var rows = (mk.rows || []).slice().filter(function (r) { return r.model != null; })
      .sort(function (a, b) { return (b.model || 0) - (a.model || 0); }).slice(0, 16);
    var fgen = (mk.meta && mk.meta.generated) || (mc.meta && mc.meta.generated);
    if (fgen) $("forest-meta").textContent = "as of " + String(fgen).slice(0, 10);
    $("forest").innerHTML = forestSvg(rows) +
      "<div class='legend'><span><span class='sw' style='background:var(--accent)'></span>model</span>" +
      "<span><span class='sw' style='background:var(--polymarket)'></span>market price</span>" +
      "<span class='dim'>bar = |model − market| edge</span></div>";
  }

  function forestSvg(rows) {
    if (!rows.length) return empty("No priced teams at this stage");
    var rowH = 24, W = 920, H = rows.length * rowH + 30, lblW = 130, P = { l: lblW, r: 50, t: 10 };
    var xOf = function (p) { return P.l + p * (W - P.l - P.r); };
    var parts = [];
    [0, 0.25, 0.5, 0.75, 1].forEach(function (g) {
      parts.push("<line class='cx-grid' x1='" + xOf(g) + "' y1='" + P.t + "' x2='" + xOf(g) + "' y2='" + (H - 20) + "'/>");
      parts.push("<text class='cx-tick' x='" + xOf(g) + "' y='" + (H - 6) + "' text-anchor='middle'>" + (g * 100) + "%</text>");
    });
    rows.forEach(function (r, i) {
      var y = P.t + i * rowH + rowH / 2;
      var xm = xOf(r.model), xk = (r.market != null ? xOf(r.market) : null);
      parts.push("<text class='cx-lbl' x='" + (lblW - 8) + "' y='" + (y + 4) + "' text-anchor='end'>" + esc(r.team) + (r.group ? " <tspan class='cx-tick'>" + esc(r.group) + "</tspan>" : "") + "</text>");
      if (xk != null) {
        parts.push("<line class='whisker' x1='" + Math.min(xm, xk) + "' y1='" + y + "' x2='" + Math.max(xm, xk) + "' y2='" + y + "'/>");
        parts.push("<circle class='dot-market' cx='" + xk + "' cy='" + y + "' r='4'/>");
      }
      parts.push("<circle class='dot-model' cx='" + xm + "' cy='" + y + "' r='4.5'/>");
      if (r.edge_pp != null) parts.push("<text class='cx-tick' x='" + (W - P.r + 6) + "' y='" + (y + 4) + "' text-anchor='start' fill='" + (r.side === "back" ? "var(--pos)" : "var(--neg)") + "'>" + (r.edge_pp > 0 ? "+" : "") + num(r.edge_pp, 0) + "pp</text>");
    });
    return svg(W, H, parts.join(""));
  }

  // =========================================================================
  //  A · EVENT MARKETS  (scores_data.json)
  // =========================================================================
  var eventMarketsSelected = null;

  function _emBestImplied(venues, outcome) {
    // Prefer Polymarket mid (no vig); else best (lowest implied = best back odds)
    if (!venues || !venues.length) return null;
    var pm = null, best = null;
    venues.forEach(function (v) {
      var imp = v.implied && v.implied[outcome];
      if (imp == null || !(imp > 0)) return;
      if (v.venue === "polymarket") { pm = imp; }
      if (best === null || imp < best) best = imp;
    });
    return pm !== null ? pm : best;
  }

  function renderEventMarkets() {
    var sd = FEEDS.scores;
    if (!sd || !sd.fixtures || !sd.fixtures.length) {
      $("event-markets-nav").innerHTML = "";
      $("event-markets-body").innerHTML = pendingMsg("Event Markets");
      return;
    }
    var fixtures = sd.fixtures;

    if (eventMarketsSelected === null) eventMarketsSelected = new Set([0]);
    Array.from(eventMarketsSelected).forEach(function (i) {
      if (i >= fixtures.length) eventMarketsSelected.delete(i);
    });
    if (!eventMarketsSelected.size) eventMarketsSelected.add(0);

    $("event-markets-nav").innerHTML = fixtures.map(function (fx, i) {
      var on = eventMarketsSelected.has(i);
      return "<span class='chip" + (on ? " active" : "") + "' data-idx='" + i + "'>" +
        esc(fx.fixture) + "</span>";
    }).join("");

    Array.prototype.forEach.call(
      $("event-markets-nav").querySelectorAll(".chip"),
      function (c) {
        c.addEventListener("click", function () {
          var idx = parseInt(c.getAttribute("data-idx"), 10);
          if (eventMarketsSelected.has(idx)) {
            if (eventMarketsSelected.size > 1) eventMarketsSelected.delete(idx);
          } else {
            eventMarketsSelected.add(idx);
          }
          renderEventMarkets();
        });
      }
    );

    var gen = sd.meta && sd.meta.generated;
    if (gen) $("event-markets-meta").textContent = "as of " + String(gen).slice(0, 10) +
      " · model vs best available price · select one or more games below";

    var html = fixtures
      .filter(function (_, i) { return eventMarketsSelected.has(i); })
      .map(function (fx) { return eventMarketBlock(fx); })
      .join("");

    html += "<div class='legend'>" +
      "<span><span class='sw' style='background:var(--accent)'></span>model</span>" +
      "<span><span class='sw' style='background:var(--polymarket)'></span>market price</span>" +
      "<span class='dim'>bar = |model − market| edge &middot; “model” = no live market data for this outcome</span></div>";

    $("event-markets-body").innerHTML = html;
  }

  function eventMarketBlock(fx) {
    var rows = [];
    var mx = fx.model_1x2 || {};
    var venues = fx.venues || [];

    // 1X2
    rows.push({section: "1X2"});
    rows.push({label: "Home", model: mx.home, market: _emBestImplied(venues, "home")});
    rows.push({label: "Draw", model: mx.draw, market: _emBestImplied(venues, "draw")});
    rows.push({label: "Away", model: mx.away, market: _emBestImplied(venues, "away")});

    // O/U goals
    var ou = fx.over_under;
    if (ou) {
      rows.push({section: "O/U " + (ou.line != null ? ou.line : "2.5") + " Goals"});
      rows.push({label: "Over",  model: ou.over  / 100, market: null});
      rows.push({label: "Under", model: ou.under / 100, market: null});
    }

    // BTTS
    if (fx.btts != null) {
      rows.push({section: "BTTS"});
      rows.push({label: "Yes", model: fx.btts / 100,         market: null});
      rows.push({label: "No",  model: (100 - fx.btts) / 100, market: null});
    }

    // Top scorelines (model vs Polymarket market prob where available)
    var topScores = (fx.scores || []).slice(0, 6);
    if (topScores.length) {
      rows.push({section: "Top Scorelines (model pick & market)"});
      topScores.forEach(function (sc) {
        rows.push({
          label: sc.score,
          model: sc.prob / 100,
          market: sc.pm_prob != null ? sc.pm_prob / 100 : null
        });
      });
    }

    return "<div class='em-fx-head'>" + esc(fx.fixture) + "</div>" + emForestSvg(rows);
  }

  function emForestSvg(rows) {
    if (!rows.length) return empty("No market data");
    var rowH = 22, secH = 18, W = 920, lblW = 160, P = {l: lblW, r: 64, t: 8};
    var H = rows.reduce(function (a, r) { return a + (r.section ? secH : rowH); }, 0) + 22;
    var xOf = function (p) { return P.l + p * (W - P.l - P.r); };
    var parts = [];

    [0, 0.25, 0.5, 0.75, 1].forEach(function (g) {
      parts.push("<line class='cx-grid' x1='" + xOf(g) + "' y1='" + P.t +
        "' x2='" + xOf(g) + "' y2='" + (H - 16) + "'/>");
      parts.push("<text class='cx-tick' x='" + xOf(g) + "' y='" + (H - 4) +
        "' text-anchor='middle'>" + (g * 100) + "%</text>");
    });

    var y = P.t;
    rows.forEach(function (r) {
      if (r.section) {
        var sy = y + secH - 6;
        parts.push("<line x1='" + xOf(0) + "' y1='" + (sy - 5) + "' x2='" + xOf(1) +
          "' y2='" + (sy - 5) + "' stroke='var(--border)' stroke-width='0.8'/>");
        parts.push("<text x='" + (lblW - 8) + "' y='" + sy +
          "' text-anchor='end' class='cx-tick' font-weight='600' fill='var(--accent-2)'>" +
          esc(r.section) + "</text>");
        y += secH;
        return;
      }
      if (r.model == null) { y += rowH; return; }
      var cy = y + rowH / 2;
      var xm = xOf(r.model);
      var xk = r.market != null ? xOf(r.market) : null;
      parts.push("<text class='cx-lbl' x='" + (lblW - 8) + "' y='" + (cy + 4) +
        "' text-anchor='end'>" + esc(r.label) + "</text>");
      if (xk != null) {
        var edgePp = (r.model - r.market) * 100;
        parts.push("<line class='whisker' x1='" + Math.min(xm, xk) + "' y1='" + cy +
          "' x2='" + Math.max(xm, xk) + "' y2='" + cy + "'/>");
        parts.push("<circle class='dot-market' cx='" + xk + "' cy='" + cy + "' r='4'/>");
        parts.push("<text class='cx-tick' x='" + (W - P.r + 5) + "' y='" + (cy + 4) +
          "' text-anchor='start' fill='" + (edgePp >= 0 ? "var(--pos)" : "var(--neg)") + "'>" +
          (edgePp >= 0 ? "+" : "") + num(edgePp, 0) + "pp</text>");
      } else {
        parts.push("<text class='cx-tick' x='" + (W - P.r + 5) + "' y='" + (cy + 4) +
          "' text-anchor='start' fill='var(--muted)'>model</text>");
      }
      parts.push("<circle class='dot-model' cx='" + xm + "' cy='" + cy + "' r='4.5'/>");
      y += rowH;
    });

    return svg(W, H, parts.join(""));
  }

  // =========================================================================
  //  B · WIN-RATE  (winrate.json)
  // =========================================================================
  function renderWinrate() {
    var w = FEEDS.winrate;
    if (!w || !w.headline) { $("winrate-head").innerHTML = pendingMsg("Win-rate"); $("winrate-rolling").innerHTML = ""; return; }
    var h = w.headline, m = w.meta || {};
    $("winrate-meta").innerHTML = "MODEL book n=" + (m.n_model || 0) + " (no selection bias) &middot; REALIZED book n=" + (m.n_realized || 0) +
      (w.low_n ? " &middot; <span class='pill lown'>LOW N — band only</span>" : "");
    $("winrate-head").innerHTML =
      kpi("Model Win-rate", pct(h.model_win_rate && h.model_win_rate.p, 0), ciSub(h.model_win_rate)) +
      kpi("Realized Win-rate", pct(h.realized_win_rate && h.realized_win_rate.p, 0), ciSub(h.realized_win_rate)) +
      kpi("Brier (model)", '<span class="' + ((h.model_brier != null && h.market_brier != null && h.model_brier <= h.market_brier) ? "pos" : "neg") + '">' + num(h.model_brier, 3) + "</span>",
        "vs market " + num(h.market_brier, 3)) +
      kpi("Skill (BSS)", '<span class="' + clsSign(h.bss) + '">' + signed(h.bss, 3) + "</span>", "1 − brier_m/brier_mkt") +
      kpi("Acca Strike", pct(h.acca_strike && h.acca_strike.p, 0), "n=" + ((h.acca_strike && h.acca_strike.n) || 0)) +
      kpi("Coverage", pct(h.coverage, 0), "of paper book settled");
    renderWinrateRolling(w);
    renderSegments(w);
    renderAcca(w);
  }

  function renderWinrateRolling(w) {
    var pts = (w.rolling || []).filter(function (p) { return p.p_roll != null || p.p_cum != null; });
    if (pts.length < 2) { $("winrate-rolling").innerHTML = empty("Win-rate series accumulates as fixtures settle (need ≥ 2 points)"); return; }
    var W = 920, H = 250, P = { l: 40, r: 14, t: 14, b: 26 };
    var n = pts.length;
    var xOf = function (i) { return P.l + (i / (n - 1)) * (W - P.l - P.r); };
    var yOf = function (v) { return H - P.b - v * (H - P.t - P.b); };
    var parts = [];
    [0, 0.25, 0.5, 0.75, 1].forEach(function (g) {
      parts.push("<line class='cx-grid' x1='" + P.l + "' y1='" + yOf(g) + "' x2='" + (W - P.r) + "' y2='" + yOf(g) + "'/>");
      parts.push("<text class='cx-tick' x='" + (P.l - 5) + "' y='" + (yOf(g) + 3) + "' text-anchor='end'>" + (g * 100) + "%</text>");
    });
    // Wilson ribbon
    var rib = pts.filter(function (p) { return p.lo != null && p.hi != null; });
    if (rib.length > 1) {
      var top = rib.map(function (p, i) { return (i ? "L" : "M") + xOf(pts.indexOf(p)) + " " + yOf(p.hi); }).join(" ");
      var bot = rib.slice().reverse().map(function (p) { return "L" + xOf(pts.indexOf(p)) + " " + yOf(p.lo); }).join(" ");
      parts.push("<path class='ribbon' d='" + top + " " + bot + " Z'/>");
    }
    // reference lines: expected model / market
    ["exp_market", "exp_model"].forEach(function (key) {
      var rp = pts.filter(function (p) { return p[key] != null; });
      if (rp.length > 1) parts.push("<path class='line-ref' style='" + (key === "exp_model" ? "stroke:var(--accent);opacity:.5" : "") + "' d='" + rp.map(function (p, i) { return (i ? "L" : "M") + xOf(pts.indexOf(p)) + " " + yOf(p[key]); }).join(" ") + "'/>");
    });
    // rolling line
    var rl = pts.filter(function (p) { return p.p_roll != null; });
    if (rl.length > 1) parts.push("<path class='line-model' d='" + rl.map(function (p, i) { return (i ? "L" : "M") + xOf(pts.indexOf(p)) + " " + yOf(p.p_roll); }).join(" ") + "'/>");
    // x labels (first/mid/last)
    [0, Math.floor(n / 2), n - 1].forEach(function (i) {
      parts.push("<text class='cx-tick' x='" + xOf(i) + "' y='" + (H - 8) + "' text-anchor='middle'>" + esc(pts[i].label || ("#" + (i + 1))) + "</text>");
    });
    $("winrate-rolling").innerHTML = svg(W, H, parts.join("")) +
      "<div class='legend'><span><span class='sw' style='background:var(--accent)'></span>rolling win-rate</span>" +
      "<span><span class='sw' style='background:var(--accent);opacity:.12'></span>Wilson 95%</span>" +
      "<span class='dim'>dashed = model / market implied (reference, model book)</span></div>";
  }

  function renderSegments(w) {
    var segs = (w.segments || []).filter(function (s) { return s.n > 0; });
    if (!segs.length) { $("winrate-segments").innerHTML = empty("No segment data"); return; }
    var W = 440, rowH = 24, H = segs.length * rowH + 26, lblW = 120, P = { r: 12, t: 8 };
    var xOf = function (p) { return lblW + p * (W - lblW - P.r); };
    var parts = [];
    [0, 0.5, 1].forEach(function (g) {
      parts.push("<line class='cx-grid' x1='" + xOf(g) + "' y1='" + P.t + "' x2='" + xOf(g) + "' y2='" + (H - 18) + "'/>");
      parts.push("<text class='cx-tick' x='" + xOf(g) + "' y='" + (H - 5) + "' text-anchor='middle'>" + (g * 100) + "%</text>");
    });
    segs.forEach(function (s, i) {
      var y = P.t + i * rowH + rowH / 2;
      parts.push("<text class='cx-lbl' x='" + (lblW - 6) + "' y='" + (y + 4) + "' text-anchor='end'>" + esc(s.label || s.key) + "</text>");
      if (s.lo != null && s.hi != null) parts.push("<line class='whisker' x1='" + xOf(s.lo) + "' y1='" + y + "' x2='" + xOf(s.hi) + "' y2='" + y + "'/>");
      if (s.p != null) parts.push("<circle class='dot-model' cx='" + xOf(s.p) + "' cy='" + y + "' r='4'/>");
      parts.push("<text class='cx-tick' x='" + (xOf(1) + 2) + "' y='" + (y + 4) + "' text-anchor='start'>n=" + s.n + "</text>");
    });
    $("winrate-segments").innerHTML = svg(W, H, parts.join(""));
  }

  function renderAcca(w) {
    var a = w.acca_autopsy || {};
    var legs = (a.legs || []).filter(function (l) { return l.n > 0; });
    var html = "";
    if (!legs.length) html = empty("No settled accas yet — leg autopsy accumulates from realized accas");
    else {
      html = "<table class='a-table'><thead><tr><th>Leg type</th><th class='r'>Strike</th><th class='r'>Wilson 95%</th><th class='r'>n</th></tr></thead><tbody>" +
        legs.map(function (l) {
          return "<tr><td>" + esc(l.type) + "</td><td class='r'>" + pct(l.p, 0) + "</td><td class='r dim'>" +
            (l.lo != null ? "[" + pct(l.lo, 0) + ", " + pct(l.hi, 0) + "]" : "—") + "</td><td class='r'>" + l.n + "</td></tr>";
        }).join("") + "</tbody></table>";
    }
    if (a.note) html += "<div class='cx-lbl' style='padding:8px 2px 0;font-size:10px'>" + esc(a.note) + "</div>";
    var nm = a.near_miss || [];
    if (nm.length) html += "<div class='cx-lbl' style='padding:6px 2px 0;font-size:10px'>Near-miss: " +
      nm.slice(0, 4).map(function (x) { return esc(x.acca) + " (−" + esc(x.missing_leg) + ")"; }).join("; ") + "</div>";
    $("winrate-acca").innerHTML = html;
  }

  // =========================================================================
  //  C · MODEL CLV — FULL BOOK  (tracking_clv_benchmark.json)
  // =========================================================================
  function renderClv() {
    var c = FEEDS.clvbench;
    if (!c || !c.headline) { $("clv-head").innerHTML = pendingMsg("Model CLV — full book"); return; }
    var h = c.headline, m = c.meta || {};
    $("clv-meta").innerHTML = "full recommendation book: " + (m.n_legs || 0) + " legs &middot; " + (m.n_with_close || 0) +
      " with a captured close &middot; <span class='dim'>fair-vs-fair, pushes excluded</span>";
    var beat = h.beat_rate || {};
    var beatVsPlacebo = (beat.p != null && h.placebo_null != null) ? (beat.p - h.placebo_null) : null;
    $("clv-head").innerHTML =
      kpi("Beat-rate", pct(beat.p, 0), ciSub(beat)) +
      kpi("Placebo null", pct(h.placebo_null, 0), "shuffle baseline (not 0.50)") +
      kpi("Beat vs Placebo", '<span class="' + clsSign(beatVsPlacebo) + '">' + signedPct(beatVsPlacebo, 1) + "</span>", "excess over chance") +
      kpi("CLV median", '<span class="' + clsSign(h.clv_median) + '">' + signedPct(h.clv_median, 2) + "</span>", "trimmed " + signedPct(h.clv_trimmed_mean, 2)) +
      kpi("Brier skill", '<span class="' + clsSign(h.brier_skill) + '">' + signed(h.brier_skill, 3) + "</span>", "vs market, outcome-anchored") +
      kpi("Coverage", pctp(h.coverage_pct, 0), "legs with a close");
    renderClvEdge(c);
    renderClvMarket(c);
    renderClvCoverage(c);
  }

  function renderClvEdge(c) {
    var buckets = (c.by_edge_bucket || []).filter(function (b) { return b.n > 0; });
    if (!buckets.length) { $("clv-edge").innerHTML = empty("No edge-bucket data"); return; }
    var W = 440, H = 230, P = { l: 36, r: 12, t: 12, b: 40 };
    var vals = buckets.map(function (b) { return b.clv_mean || 0; }).concat(buckets.map(function (b) { return b.placebo_clv || 0; }));
    var maxA = Math.max(0.02, Math.max.apply(null, vals.map(Math.abs)));
    var bw = (W - P.l - P.r) / buckets.length;
    var yOf = function (v) { return P.t + (1 - (v + maxA) / (2 * maxA)) * (H - P.t - P.b); };
    var parts = ["<line class='cx-zero' x1='" + P.l + "' y1='" + yOf(0) + "' x2='" + (W - P.r) + "' y2='" + yOf(0) + "'/>"];
    buckets.forEach(function (b, i) {
      var x = P.l + i * bw + bw * 0.2, w = bw * 0.6;
      var v = b.clv_mean || 0;
      var cls = v >= 0 ? "bar-pos" : "bar-neg";
      var y0 = yOf(0), y1 = yOf(v);
      parts.push("<rect class='" + cls + "' x='" + x.toFixed(1) + "' y='" + Math.min(y0, y1).toFixed(1) + "' width='" + w.toFixed(1) + "' height='" + Math.abs(y1 - y0).toFixed(1) + "' rx='1'/>");
      // placebo slope marker
      if (b.placebo_clv != null) parts.push("<line x1='" + x + "' y1='" + yOf(b.placebo_clv) + "' x2='" + (x + w) + "' y2='" + yOf(b.placebo_clv) + "' stroke='var(--warn)' stroke-width='1.5'/>");
      parts.push("<text class='cx-tick' x='" + (x + w / 2) + "' y='" + (H - 24) + "' text-anchor='middle'>" + esc(b.bucket) + "</text>");
      parts.push("<text class='cx-tick' x='" + (x + w / 2) + "' y='" + (H - 12) + "' text-anchor='middle'>n=" + b.n + "</text>");
    });
    $("clv-edge").innerHTML = svg(W, H, parts.join("")) +
      "<div class='legend'><span><span class='sw' style='background:var(--warn)'></span>placebo slope</span>" +
      "<span class='dim'>real edge = excess over placebo, not raw bar height</span></div>";
  }

  function renderClvMarket(c) {
    var legs = (c.by_market || []).filter(function (l) { return l.n > 0; });
    if (!legs.length) { $("clv-market").innerHTML = empty("No per-leg CLV data"); return; }
    $("clv-market").innerHTML = "<table class='a-table'><thead><tr><th>Leg</th><th class='r'>CLV median</th><th class='r'>n</th></tr></thead><tbody>" +
      legs.map(function (l) {
        return "<tr><td>" + esc(String(l.leg).toUpperCase()) + "</td><td class='r " + clsSign(l.clv_median) + "'>" + signedPct(l.clv_median, 2) + "</td><td class='r'>" + l.n + "</td></tr>";
      }).join("") + "</tbody></table>" +
      "<div class='cx-lbl' style='padding:8px 2px 0;font-size:10px'>Draws are the documented under-priced leg — watch this row as N grows. Legs are never pooled across n_outcomes.</div>";
  }

  function renderClvCoverage(c) {
    var pv = c.placed_vs_passed || {};
    var cov = c.coverage_by_market || {};
    var html = "<table class='a-table'><thead><tr><th>Selection-bias scorecard</th><th class='r'>CLV median</th><th class='r'>n</th></tr></thead><tbody>" +
      "<tr><td>Placed (became bets)</td><td class='r " + clsSign(pv.placed && pv.placed.clv_median) + "'>" + signedPct(pv.placed && pv.placed.clv_median, 2) + "</td><td class='r'>" + ((pv.placed && pv.placed.n) || 0) + "</td></tr>" +
      "<tr><td>Passed (paper only)</td><td class='r " + clsSign(pv.passed && pv.passed.clv_median) + "'>" + signedPct(pv.passed && pv.passed.clv_median, 2) + "</td><td class='r'>" + ((pv.passed && pv.passed.n) || 0) + "</td></tr>" +
      "</tbody></table>";
    var covKeys = Object.keys(cov);
    if (covKeys.length) {
      html += "<table class='a-table' style='margin-top:10px'><thead><tr><th>CLV coverage by market</th><th>%</th><th class='r'>n / clv_n</th></tr></thead><tbody>" +
        covKeys.map(function (k) {
          var o = cov[k];
          return "<tr><td>" + esc(k) + "</td><td><div class='cov-bar'><div class='cov-fill' style='width:" + covWidth(o.coverage_pct) + "%'></div></div></td><td class='r'>" + (o.clv_n || 0) + " / " + (o.n || 0) + "</td></tr>";
        }).join("") + "</tbody></table>";
    }
    html += "<div class='cx-lbl' style='padding:8px 2px 0;font-size:10px;line-height:1.5'>" +
      "Coverage is 100% for 1X2 because every priced fixture has a liquid 1X2 close. Markets without a liquid close " +
      "(scoreline, thin advancement, many pre-tournament O/U lines) are structurally un-CLV-able — there, " +
      "<b style='color:var(--text)'>calibration / Brier-vs-outcome</b> is the skill signal, not CLV. " +
      "CLV coverage being high here does <i>not</i> mean the edge is real — see the verdict (D)." +
      (c.note ? " " + esc(c.note) : "") + "</div>";
    $("clv-coverage").innerHTML = html;
  }

  // =========================================================================
  //  P0 · PAPER LEDGER  (predledger.json)
  // =========================================================================
  function renderLedger() {
    var p = FEEDS.predledger;
    if (!p || !p.by_market) { $("ledger-body").innerHTML = pendingMsg("Prediction ledger"); return; }
    var m = p.meta || {}, h = p.headline || {};
    $("ledger-meta").innerHTML = (m.n_predictions || 0) + " predictions &middot; " + (m.n_paper || 0) + " paper / " +
      (m.n_realized || 0) + " money-realized &middot; " + (m.n_with_clv || 0) + " CLV-stamped (" + pctp(h.clv_coverage_pct, 0) + " coverage)";
    var rows = (p.by_market || []).map(function (r) {
      var cov = (p.coverage && p.coverage[r.market]) || {};
      return "<tr><td>" + esc(r.market) + "</td><td class='r'>" + r.n + "</td><td class='r'>" + (r.settled || 0) +
        "</td><td class='r'>" + (r.won || 0) + "/" + (r.lost || 0) + "</td>" +
        "<td class='r'>" + (r.win_rate != null ? pct(r.win_rate, 0) : "—") + "</td>" +
        "<td class='r dim'>" + (r.win_lo != null ? "[" + pct(r.win_lo, 0) + ", " + pct(r.win_hi, 0) + "]" : "—") + "</td>" +
        "<td class='r'>" + (r.model_brier != null ? num(r.model_brier, 3) : "—") + "</td>" +
        "<td><div class='cov-bar'><div class='cov-fill' style='width:" + covWidth(cov.coverage_pct) + "%'></div></div></td></tr>";
    }).join("");
    $("ledger-body").innerHTML =
      "<table class='a-table'><thead><tr><th>Market</th><th class='r'>Rows</th><th class='r'>Settled</th><th class='r'>W/L</th>" +
      "<th class='r'>Win-rate</th><th class='r'>Wilson 95%</th><th class='r'>Brier</th><th>CLV cov.</th></tr></thead><tbody>" +
      rows + "</tbody></table>" +
      "<div class='cx-lbl' style='padding:9px 2px 0;font-size:10px;line-height:1.5'>" +
      "The 1X2 paper book emits all three legs per fixture, so the leg-level win-rate is ~33% <i>by construction</i> " +
      "(exactly one leg wins) &mdash; that is a structural base rate, not model skill. <b style='color:var(--text)'>Brier</b> " +
      "is the meaningful 1X2 forecast-quality metric here. Scoreline / O-U / BTTS / advancement accumulate from the first " +
      "live card build forward (backfill is 1X2-only; no fabrication).</div>";
  }

  // =========================================================================
  //  LOAD + RENDER
  // =========================================================================
  // ---- E // Model vs Venues ----------------------------------------------
  function fmt(v, dp) { return (v == null || isNaN(v)) ? "—" : Number(v).toFixed(dp == null ? 4 : dp); }

  function venuesBars(rows) {
    // Horizontal bars of mean distance with bootstrap CI whiskers; top 12.
    var top = rows.filter(function (r) { return r.mean_distance != null; }).slice(0, 12);
    if (!top.length) return empty("no common-support ranking");
    var W = 720, rowH = 22, padL = 150, padR = 60, padT = 8, padB = 22;
    var H = top.length * rowH + padT + padB;
    var maxD = 0;
    top.forEach(function (r) { maxD = Math.max(maxD, r.ci_hi != null ? r.ci_hi : r.mean_distance); });
    maxD = maxD > 0 ? maxD * 1.08 : 1;
    var x = function (d) { return padL + (d / maxD) * (W - padL - padR); };
    var parts = [];
    top.forEach(function (r, i) {
      var cy = padT + i * rowH + rowH / 2;
      var best = i === 0;
      var col = best ? "var(--accent)" : "var(--muted)";
      parts.push('<text x="' + (padL - 6) + '" y="' + (cy + 3) + '" text-anchor="end" font-size="10" fill="var(--text)">' + esc(r.venue) + '</text>');
      // CI whisker
      if (r.ci_lo != null && r.ci_hi != null) {
        parts.push('<line x1="' + x(r.ci_lo) + '" x2="' + x(r.ci_hi) + '" y1="' + cy + '" y2="' + cy + '" stroke="var(--border-2)" stroke-width="1"/>');
        parts.push('<line x1="' + x(r.ci_lo) + '" x2="' + x(r.ci_lo) + '" y1="' + (cy - 4) + '" y2="' + (cy + 4) + '" stroke="var(--border-2)" stroke-width="1"/>');
        parts.push('<line x1="' + x(r.ci_hi) + '" x2="' + x(r.ci_hi) + '" y1="' + (cy - 4) + '" y2="' + (cy + 4) + '" stroke="var(--border-2)" stroke-width="1"/>');
      }
      parts.push('<circle cx="' + x(r.mean_distance) + '" cy="' + cy + '" r="4" fill="' + col + '"/>');
      parts.push('<text x="' + (W - padR + 4) + '" y="' + (cy + 3) + '" font-size="9" fill="var(--text-dim)">P1 ' + Math.round((r.p_rank1 || 0) * 100) + '%</text>');
    });
    parts.push('<text x="' + padL + '" y="' + (H - 6) + '" font-size="9" fill="var(--muted)">0</text>');
    parts.push('<text x="' + (W - padR) + '" y="' + (H - 6) + '" font-size="9" fill="var(--muted)" text-anchor="end">MAE ' + fmt(maxD, 3) + '</text>');
    return svg(W, H, parts.join(""));
  }

  function miniTable(headers, rows) {
    var h = headers.map(function (x) { return "<th>" + esc(x) + "</th>"; }).join("");
    var b = rows.map(function (r) {
      return "<tr>" + r.map(function (c, i) {
        return '<td' + (i === 0 ? '' : ' class="num"') + '>' + (c == null ? "—" : esc(c)) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    return '<table class="a-tbl"><thead><tr>' + h + "</tr></thead><tbody>" + b + "</tbody></table>";
  }

  function renderVenues() {
    var v = FEEDS.venues;
    if (!v) { if ($("venues-verdict")) $("venues-verdict").innerHTML = pendingMsg("Model vs Venues"); return; }
    var cov = v.coverage || {}, lb = v.leaderboard || {}, meta = v.meta || {};

    if ($("venues-meta")) $("venues-meta").textContent =
      "window " + (meta.window || "—") + " · " + (meta.primary || "") + " · freshness " + (meta.freshness_limit_h || "?") + "h";

    // KPIs
    var kpis = [
      ["Common-support fixtures", lb.n_fixtures != null ? lb.n_fixtures : "—"],
      ["Observations (build×fixture)", cov.n_obs != null ? cov.n_obs : "—"],
      ["Venues compared", cov.n_venues != null ? cov.n_venues : "—"],
      ["Friedman p", lb.friedman && lb.friedman.p != null ? fmt(lb.friedman.p, 5) : "—"],
    ];
    $("venues-kpi").innerHTML = kpis.map(function (k) {
      return '<div class="kpi"><span class="k-label">' + esc(k[0]) + '</span><span class="k-value">' + esc(String(k[1])) + "</span></div>";
    }).join("");

    // Verdict banner
    var verdict = lb.verdict || "—";
    var good = /closest venue/.test(verdict);
    var cls = good ? "a-verdict a-good" : "a-verdict a-warn";
    $("venues-verdict").innerHTML =
      '<div class="' + cls + '">' + esc(verdict) + '</div>' +
      '<p class="a-note">Model side is the <b>ex-market blend</b> (Elo/DC only) — it contains no market consensus, so a venue&rsquo;s closeness is independent evidence. ' +
      'The leave-one-book-out board (each venue vs the consensus of the <i>other</i> books) agrees: <b>' + esc((v.lobo_leaderboard || {}).verdict || "—") + '</b>.</p>' +
      venuesBars(lb.venues || []);

    // Leaderboard table
    var rows = (lb.venues || []).map(function (r) {
      return [r.venue, fmt(r.mean_distance), (r.ci_lo == null ? "—" : fmt(r.ci_lo) + "–" + fmt(r.ci_hi)),
        fmt(r.mean_rank, 2), Math.round((r.p_rank1 || 0) * 100) + "%", r.n];
    });
    $("venues-leaderboard").innerHTML =
      "<div class='sub-head'>Common-support leaderboard (same fixtures, all venues fresh)</div>" +
      miniTable(["Venue", "MAE", "95% CI", "Mean rank", "P(rank1)", "n"], rows);

    // Accuracy: model vs venues (agreement != accuracy)
    var acc = v.accuracy || {};
    var accRows = [];
    if (acc.model) {
      Object.keys(acc.model).forEach(function (c) {
        var m = acc.model[c]; if (m && m.n) accRows.push(["model:" + c, fmt(m.brier), fmt(m.log_loss), m.n]);
      });
    }
    var accVen = acc.venues || {};
    Object.keys(accVen).sort(function (a, b) { return (accVen[a].brier || 9) - (accVen[b].brier || 9); })
      .slice(0, 6).forEach(function (k) { accRows.push([k, fmt(accVen[k].brier), fmt(accVen[k].log_loss), accVen[k].n]); });
    $("venues-accuracy").innerHTML = (acc.n_fixtures
      ? miniTable(["Side", "Brier", "LogLoss", "n"], accRows) +
        "<p class='a-note'>Lower is better. A venue can match the model (zero distance) yet both be wrong — accuracy is scored against realised outcomes, separately from closeness.</p>"
      : empty("accuracy — settled fixtures insufficient", true));

    // Hypotheses (Friedman + BH-FDR)
    var hyp = (v.hypotheses || []).map(function (h) {
      return [h.name, h.independent ? "yes" : "no (circular)", h.p == null ? "—" : fmt(h.p, 5),
        h.q_bh == null ? "—" : fmt(h.q_bh, 5), h.n_fixtures];
    });
    $("venues-hypotheses").innerHTML = hyp.length
      ? miniTable(["Hypothesis", "Independent", "Friedman p", "q (BH)", "fix"], hyp)
      : empty("no hypotheses");

    // Segments
    var seg = (v.segments || []).map(function (s) {
      return [s.segment, s.n_fixtures, s.friedman_p == null ? "—" : fmt(s.friedman_p, 4),
        s.q_bh == null ? "—" : fmt(s.q_bh, 4), s.state === "ok" ? (s.closest || "—") : "insufficient"];
    });
    $("venues-segments").innerHTML = seg.length
      ? miniTable(["Segment", "fix", "p", "q (BH)", "closest"], seg)
      : empty("no segments");

    // Arms: placed vs paper, Polymarket
    var pa = v.placed_arm || {}, pm = v.polymarket || {};
    var unmatched = (pa.unmatched_audit || []).reduce(function (acc2, u) {
      acc2[u.reason] = (acc2[u.reason] || 0) + 1; return acc2;
    }, {});
    var reasons = Object.keys(unmatched).map(function (r) { return r + ": " + unmatched[r]; }).join(", ");
    $("venues-arms").innerHTML =
      '<div class="a-badge ' + (pa.insufficient ? "a-warn" : "a-good") + '">PLACED ARM ' + (pa.insufficient ? "INSUFFICIENT" : "OK") + '</div>' +
      "<p class='a-note'>Linked <b>" + (pa.n_linked || 0) + "</b> / " + (pa.n_model_bets || 0) +
      " source=model bets to their exact preceding build. Unmatched (audited, not dropped): " + esc(reasons || "none") +
      ". Placed legs are selected where model & market disagree — a biased sample vs the full paper book.</p>" +
      '<div class="a-badge a-warn">POLYMARKET ' + esc(pm.state || "—") + '</div>' +
      "<p class='a-note'>" + esc(pm.note || "") + "</p>";
  }

  // =========================================================================
  //  F · MARKET INTELLIGENCE  (market_intel.json)
  // =========================================================================
  var intelSelected = null;

  var INTEL_MKT_LABEL = { moneyline: "1X2", moneyline_lay: "1X2 (lay)", ou: "Over/Under", btts: "BTTS", ah: "Asian H'cap" };
  function intelMktLabel(mt, line) {
    var base = INTEL_MKT_LABEL[mt] || mt;
    return (line != null && line !== "") ? base + " " + line : base;
  }
  function intelSelLabel(sel, fx) {
    if (sel === "Home") return (fx && fx.home) ? fx.home : "Home";
    if (sel === "Away") return (fx && fx.away) ? fx.away : "Away";
    return sel;
  }
  function intelDot(colour) {
    return "<span class='sw' style='background:" + esc(colour || "#9A93B0") + "'></span>";
  }
  function intelStaleBadge(stale, age) {
    var mins = (age == null) ? null : Math.round(age / 60);
    if (stale) return "<span class='a-badge a-warn'>STALE" + (mins != null ? " " + mins + "m" : "") + "</span>";
    return "<span class='a-badge a-good'>FRESH" + (mins != null ? " " + mins + "m" : "") + "</span>";
  }
  function intelPriceCell(s, key, vkey) {
    // s = selection metrics; key='best_odds'|'worst_odds'; vkey='best_venue'|'worst_venue'
    if (!s || s[key] == null) return "—";
    var v = s[vkey], col = (s.venues && s.venues[v] && s.venues[v].colour) || null;
    return intelDot(col) + num(s[key], 2) + " <span class='dim'>" + esc(v) + "</span>";
  }

  function renderMarketIntel() {
    var f = FEEDS.intel;
    if (!f || !f.fixtures) { if ($("intel-spread")) $("intel-spread").innerHTML = pendingMsg("Market Intelligence"); return; }
    var fixtures = f.fixtures || [], meta = f.meta || {}, venues = f.venues || [];

    if ($("intel-meta")) {
      var gen = f.generated_at ? String(f.generated_at).replace("T", " ").replace("Z", "") + " UTC" : "—";
      $("intel-meta").textContent = (meta.n_markets || 0) + " markets · " + (meta.n_fixtures || 0) + " fixtures · as of " + gen;
    }

    if (!fixtures.length) {
      $("intel-kpi").innerHTML = "";
      $("intel-legend").innerHTML = "";
      $("intel-nav").innerHTML = "";
      $("intel-detail").innerHTML = "";
      $("intel-spread").innerHTML = empty("No upcoming fixtures within the capture window — cross-venue prices appear once odds are captured for fixtures kicking off soon.", true);
      $("intel-notes").innerHTML = intelNotes(meta);
      return;
    }

    // ---- KPI strip
    var allMkts = [];
    fixtures.forEach(function (fx) { (fx.markets || []).forEach(function (m) { allMkts.push(m); }); });
    var freshAges = allMkts.map(function (m) { return m.age_secs; }).filter(function (a) { return a != null; });
    var minAge = freshAges.length ? Math.min.apply(null, freshAges) : null;
    var staleN = allMkts.filter(function (m) { return m.stale; }).length;
    var kpis = [
      kpi("Fixtures", String(meta.n_fixtures || fixtures.length)),
      kpi("Markets", String(meta.n_markets || allMkts.length)),
      kpi("Venues tracked", String(venues.length)),
      kpi("Freshest quote", minAge == null ? "—" : Math.round(minAge / 60) + "m",
        staleN + " / " + allMkts.length + " stale"),
    ];
    $("intel-kpi").innerHTML = kpis.join("");

    // ---- venue legend (stable colours, kind, cost, liquidity honesty)
    $("intel-legend").innerHTML =
      "<div class='sub-head'>Venues (stable colours · commission · price source)</div>" +
      "<div class='legend intel-legend'>" + venues.map(function (v) {
        var comm = v.commission ? (Math.round(v.commission * 1000) / 10) + "%" : "0%";
        var liq = v.has_liquidity ? "live" : "relay";
        return "<span title='" + esc(v.kind) + "'>" + intelDot(v.colour) + esc(v.venue) +
          " <span class='dim'>" + esc(v.kind.replace("_", " ")) + " · " + comm + " · " + liq + "</span></span>";
      }).join("") + "</div>";

    // ---- cross-fixture spread headline (moneyline), biggest dislocation first
    var spreadRows = [];
    fixtures.forEach(function (fx, i) {
      var ml = (fx.markets || []).filter(function (m) { return m.market_type === "moneyline"; })[0];
      if (!ml) return;
      var bySel = {};
      (ml.selections || []).forEach(function (s) { bySel[s.selection] = s; });
      var maxImpr = 0;
      (ml.selections || []).forEach(function (s) { if (s.pct_improvement != null) maxImpr = Math.max(maxImpr, s.pct_improvement); });
      spreadRows.push({
        idx: i, fx: fx, ml: ml, bySel: bySel, maxImpr: maxImpr,
      });
    });
    spreadRows.sort(function (a, b) { return b.maxImpr - a.maxImpr; });

    if (!spreadRows.length) {
      $("intel-spread").innerHTML = empty("No 1X2 markets in the current capture.", true);
    } else {
      var head = "<tr><th>Match</th><th>KO</th><th class='num'>Books</th><th>Home best</th><th>Draw best</th><th>Away best</th><th class='num'>Max %Δ</th><th>Quote</th></tr>";
      var body = spreadRows.map(function (r) {
        var ko = r.fx.ko_utc ? String(r.fx.ko_utc).slice(5, 16).replace("T", " ") : "—";
        return "<tr>" +
          "<td>" + esc((r.fx.home || "?") + " v " + (r.fx.away || "?")) + "</td>" +
          "<td class='num'>" + esc(ko) + "</td>" +
          "<td class='num'>" + (r.ml.n_venues || "—") + "</td>" +
          "<td>" + intelPriceCell(r.bySel.Home, "best_odds", "best_venue") + "</td>" +
          "<td>" + intelPriceCell(r.bySel.Draw, "best_odds", "best_venue") + "</td>" +
          "<td>" + intelPriceCell(r.bySel.Away, "best_odds", "best_venue") + "</td>" +
          "<td class='num'>" + (r.maxImpr ? pct(r.maxImpr, 1) : "—") + "</td>" +
          "<td>" + intelStaleBadge(r.ml.stale, r.ml.age_secs) + "</td>" +
          "</tr>";
      }).join("");
      $("intel-spread").innerHTML =
        "<div class='sub-head'>Best price per outcome across all books · sorted by largest cross-venue gap (the execution edge)</div>" +
        "<table class='a-tbl'><thead>" + head + "</thead><tbody>" + body + "</tbody></table>";
    }

    // ---- fixture picker + per-market detail
    if (intelSelected == null || intelSelected >= fixtures.length) intelSelected = (spreadRows[0] ? spreadRows[0].idx : 0);
    $("intel-nav").innerHTML = fixtures.map(function (fx, i) {
      var lbl = (fx.home || "?") + " v " + (fx.away || "?");
      return "<span class='chip" + (i === intelSelected ? " active" : "") + "' data-idx='" + i + "'>" + esc(lbl) + "</span>";
    }).join("");
    Array.prototype.forEach.call($("intel-nav").querySelectorAll(".chip"), function (c) {
      c.addEventListener("click", function () { intelSelected = parseInt(c.getAttribute("data-idx"), 10); renderMarketIntel(); });
    });

    $("intel-detail").innerHTML = intelDetail(fixtures[intelSelected]);
    $("intel-notes").innerHTML = intelNotes(meta);
  }

  function intelDetail(fx) {
    if (!fx) return "";
    var mkts = (fx.markets || []).slice();
    if (!mkts.length) return empty("No markets captured for this fixture.");
    var blocks = mkts.map(function (m) {
      var rows = (m.selections || []).map(function (s) {
        var disagree = (s.largest_disagreement != null && s.disagreement_pair)
          ? pctp(s.largest_disagreement * 100, 1) + " <span class='dim'>" + esc(s.disagreement_pair[0]) + "/" + esc(s.disagreement_pair[1]) + "</span>"
          : "—";
        return "<tr>" +
          "<td>" + esc(intelSelLabel(s.selection, fx)) + "</td>" +
          "<td class='num'>" + (s.consensus_prob != null ? pct(s.consensus_prob, 1) : "—") + "</td>" +
          "<td>" + intelPriceCell(s, "best_odds", "best_venue") + "</td>" +
          "<td>" + intelPriceCell(s, "worst_odds", "worst_venue") + "</td>" +
          "<td class='num'>" + (s.implied_range != null ? pctp(s.implied_range * 100, 1) : "—") + "</td>" +
          "<td class='num'>" + (s.pct_improvement != null ? pct(s.pct_improvement, 1) : "—") + "</td>" +
          "<td>" + disagree + "</td>" +
          "</tr>";
      }).join("");
      return "<div class='sub-panel'><div class='sub-head'>" +
        esc(intelMktLabel(m.market_type, m.line)) +
        " <span class='dim'>· " + (m.n_venues || 0) + " books</span> " + intelStaleBadge(m.stale, m.age_secs) +
        "</div><table class='a-tbl'><thead><tr>" +
        "<th>Selection</th><th class='num'>Consensus</th><th>Best</th><th>Worst</th><th class='num'>Range</th><th class='num'>%Δ best/worst</th><th>Widest split</th>" +
        "</tr></thead><tbody>" + rows + "</tbody></table></div>";
    }).join("");
    var ml = mkts.filter(function (m) { return m.market_type === "moneyline" && m.history; })[0];
    var charts = ml ? intelHistoryBlock(ml.history, fx) : "";
    return "<div class='sub-head'>" + esc((fx.home || "?") + " v " + (fx.away || "?")) +
      (fx.ko_utc ? " <span class='dim'>· KO " + esc(String(fx.ko_utc).slice(0, 16).replace("T", " ")) + " UTC</span>" : "") +
      "</div>" + charts + blocks;
  }

  // Price history: implied-prob + decimal-odds over time, one line per venue.
  function intelHistoryBlock(hist, fx) {
    if (!hist || !hist.series || !hist.series.length) return "";
    var sel = intelSelLabel(hist.selection, fx);
    if (!hist.chartable) {
      return "<div class='two-col'><div class='sub-panel'><div class='sub-head'>Price history · " + esc(sel) +
        "</div>" + empty("history accrues as snapshots are collected (currently " +
          (hist.n_points || 0) + " point" + ((hist.n_points === 1) ? "" : "s") + " — one capture so far)", true) + "</div></div>";
    }
    var legend = "<div class='legend'>" + hist.series.map(function (s) {
      return "<span>" + intelDot(s.colour) + esc(s.venue) + "</span>";
    }).join("") + "</div>";
    return "<div class='two-col'>" +
      "<div class='sub-panel'><div class='sub-head'>Implied probability over time · " + esc(sel) + "</div>" +
        intelHistChart(hist.series, 2, { pct: true }) + "</div>" +
      "<div class='sub-panel'><div class='sub-head'>Decimal odds over time · " + esc(sel) + "</div>" +
        intelHistChart(hist.series, 1, { pct: false }) + "</div>" +
      "</div>" + legend;
  }

  function intelHistChart(series, vi, opts) {
    opts = opts || {};
    var pts = [];
    series.forEach(function (s) { (s.points || []).forEach(function (p) {
      var t = Date.parse(p[0]); var v = p[vi];
      if (!isNaN(t) && v != null && !isNaN(v)) pts.push([t, Number(v)]);
    }); });
    if (pts.length < 2) return empty("not enough points to plot");
    var tMin = Math.min.apply(null, pts.map(function (p) { return p[0]; }));
    var tMax = Math.max.apply(null, pts.map(function (p) { return p[0]; }));
    var vMin = Math.min.apply(null, pts.map(function (p) { return p[1]; }));
    var vMax = Math.max.apply(null, pts.map(function (p) { return p[1]; }));
    if (tMax === tMin) tMax = tMin + 1;
    var pad = (vMax - vMin) * 0.12 || (vMax * 0.05) || 0.05;
    vMin -= pad; vMax += pad;
    var W = 460, H = 180, P = { l: 44, r: 10, t: 10, b: 22 };
    var X = function (t) { return P.l + (t - tMin) / (tMax - tMin) * (W - P.l - P.r); };
    var Y = function (v) { return P.t + (1 - (v - vMin) / (vMax - vMin)) * (H - P.t - P.b); };
    var parts = [];
    [0, 0.5, 1].forEach(function (g) {
      var vv = vMin + g * (vMax - vMin), yy = Y(vv);
      parts.push("<line class='cx-grid' x1='" + P.l + "' y1='" + yy + "' x2='" + (W - P.r) + "' y2='" + yy + "'/>");
      parts.push("<text class='cx-tick' x='" + (P.l - 5) + "' y='" + (yy + 3) + "' text-anchor='end'>" +
        (opts.pct ? Math.round(vv * 100) + "%" : vv.toFixed(2)) + "</text>");
    });
    series.forEach(function (s) {
      var sp = (s.points || []).map(function (p) { return [Date.parse(p[0]), Number(p[vi])]; })
        .filter(function (p) { return !isNaN(p[0]) && !isNaN(p[1]); })
        .sort(function (a, b) { return a[0] - b[0]; });
      if (!sp.length) return;
      var col = s.colour || "#9A93B0";
      if (sp.length === 1) { parts.push("<circle cx='" + X(sp[0][0]) + "' cy='" + Y(sp[0][1]) + "' r='3' fill='" + col + "'/>"); return; }
      var d = sp.map(function (p, i) { return (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
      parts.push("<path d='" + d + "' fill='none' stroke='" + col + "' stroke-width='1.6'/>");
    });
    return svg(W, H, parts.join(""));
  }

  function intelNotes(meta) {
    var notes = (meta && meta.notes) || [];
    if (!notes.length) return "";
    return "<p class='a-note'><b>Honest scope.</b> " + notes.map(esc).join(" ") +
      " Consensus is vig-removed (Shin) over complete books only; best/worst are raw quoted odds. Cross-venue gap (%Δ) is the dependable structural edge — execution/cost, not prediction.</p>";
  }

  function load(name, file) {
    return fetch("./data/" + file, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { FEEDS[name] = j; })
      .catch(function () { FEEDS[name] = null; });
  }

  function boot() {
    Promise.all([
      load("data", "data.json"),
      load("tracking", "tracking_data.json"),
      load("exposure", "exposure_data.json"),
      load("mc", "mc_futures.json"),
      load("advhist", "advancement_history.json"),
      load("predledger", "predledger.json"),
      load("winrate", "winrate.json"),
      load("clvbench", "tracking_clv_benchmark.json"),
      load("rigor", "rigor.json"),
      load("risk", "risk_pnl.json"),
      load("scores", "scores_data.json"),
      load("venues", "venues_benchmark.json"),
      load("intel", "market_intel.json"),
    ]).then(function () {
      var any = Object.keys(FEEDS).some(function (k) { return FEEDS[k]; });
      if (!any) { $("nodata").hidden = false; $("nodata-msg").textContent = "NO DATA FEED — could not load ./data/*.json"; }
      var pendingNew = ["predledger", "winrate", "clvbench", "rigor", "risk"].filter(function (k) { return !FEEDS[k]; });
      if (pendingNew.length) { $("nodata").hidden = false; $("nodata-msg").textContent = "Backend feeds still building: " + pendingNew.join(", ") + " — live panels render once published."; }

      [renderKpi, renderVerdict, renderRisk, renderForest, renderEventMarkets, renderWinrate, renderClv, renderVenues, renderMarketIntel, renderLedger].forEach(function (fn) {
        try { fn(); } catch (e) { /* never let one panel break the page */ if (window.console) console.error(fn.name, e); }
      });
      var gen = (FEEDS.data && FEEDS.data.meta && FEEDS.data.meta.generated) || "";
      $("foot-gen").textContent = gen ? "data generated " + gen.replace("T", " ").slice(0, 19) + " UTC" : "";
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
