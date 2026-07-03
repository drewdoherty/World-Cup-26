/* World Cup Alpha — Market Microstructure & Execution research page.
 *
 * Renders ./microstructure/index.json — a consolidated feed assembled from the
 * verified per-area analysis. Every section carries a confidence badge
 * (measured / indicative / framework-only); nothing is rendered without its
 * caveat. Degrades to a clean "feed unavailable" state. No external assets.
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function badge(conf) {
    var c = String(conf || "").toLowerCase();
    var cls = c.indexOf("measur") >= 0 ? "ms-measured"
      : c.indexOf("indicat") >= 0 ? "ms-indicative"
      : c.indexOf("reject") >= 0 ? "ms-rejected" : "ms-framework";
    var label = c.indexOf("measur") >= 0 ? "measured"
      : c.indexOf("indicat") >= 0 ? "indicative"
      : c.indexOf("reject") >= 0 ? "not supported" : "framework";
    return '<span class="ms-badge ' + cls + '">' + label + "</span>";
  }
  function priorityClass(p) {
    var s = String(p || "").toLowerCase();
    if (s.indexOf("1") >= 0 || s.indexOf("high") >= 0 || s.indexOf("now") >= 0) return "ms-pri-high";
    if (s.indexOf("2") >= 0 || s.indexOf("med") >= 0) return "ms-pri-med";
    return "ms-pri-low";
  }

  // ---- coverage -----------------------------------------------------------
  function renderCoverage(cov) {
    if (!cov) return;
    var tiles = $("ms-cov-tiles");
    var grid = $("ms-coverage");
    var items = [
      ["odds rows", cov.rows],
      ["matches", cov.matches],
      ["bookmakers", cov.books],
      ["exchanges", cov.exchanges],
      ["capture-times", cov.capture_times],
      ["window", cov.window_days != null ? cov.window_days + "d" : cov.window],
      ["markets", cov.markets],
      ["CLV sample", cov.clv_n != null ? "n=" + cov.clv_n : null],
      ["source", cov.sources]
    ].filter(function (x) { return x[1] != null && x[1] !== ""; });
    if (tiles) {
      tiles.innerHTML = items.slice(0, 5).map(function (it) {
        return '<div class="tick"><span class="tick-k">' + esc(it[0]) +
          '</span><span class="tick-v">' + esc(it[1]) + "</span></div>";
      }).join("");
    }
    if (grid) {
      grid.innerHTML = items.map(function (it) {
        return '<div class="ms-cov-card"><div class="ms-cov-val">' + esc(it[1]) +
          '</div><div class="ms-cov-lab">' + esc(it[0]) + "</div></div>";
      }).join("");
    }
    var meta = $("ms-cov-meta");
    if (meta && cov.window) meta.textContent = cov.window;
  }

  function renderHonesty(notes, cov) {
    var el = $("ms-honesty");
    if (!el) return;
    var caveats = (notes || []).concat((cov && cov.caveats) || []);
    if (!caveats.length) { el.hidden = true; return; }
    el.innerHTML = '<div class="ms-honesty-title">⚠ Read this first — what this data can and cannot show</div>' +
      "<ul>" + caveats.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("") + "</ul>";
  }

  // ---- ranked edges -------------------------------------------------------
  function renderRanked(edges) {
    var el = $("ms-ranked");
    if (!el) return;
    if (!edges || !edges.length) { el.innerHTML = '<div class="empty">No verified edges yet.</div>'; return; }
    var rows = edges.map(function (e) {
      return '<tr>' +
        '<td class="ms-rank">' + esc(e.rank != null ? e.rank : "") + "</td>" +
        '<td class="ms-edge-name">' + esc(e.edge) + "</td>" +
        "<td>" + badge(e.confidence) + "</td>" +
        '<td class="ms-num">' + esc(e.ev_per_mo || e.ev || "—") + "</td>" +
        '<td class="ms-num">' + complexityDots(e.complexity) + "</td>" +
        '<td><span class="ms-pri ' + priorityClass(e.priority) + '">' + esc(e.priority || "—") + "</span></td>" +
        '<td class="ms-valid">' + esc(e.validation || "") + "</td>" +
        "</tr>";
    }).join("");
    el.innerHTML = '<table class="ms-table"><thead><tr>' +
      "<th>#</th><th>Structural edge</th><th>Confidence</th><th>Est. £/mo</th>" +
      "<th>Build</th><th>Priority</th><th>Validation gate</th>" +
      "</tr></thead><tbody>" + rows + "</tbody></table>";
    var meta = $("ms-edges-meta");
    if (meta) meta.textContent = edges.length + " ranked";
  }
  function complexityDots(c) {
    var n = parseInt(c, 10); if (isNaN(n)) return esc(c || "—");
    var s = ""; for (var i = 1; i <= 5; i++) s += i <= n ? "●" : "○";
    return '<span class="ms-dots">' + s + "</span>";
  }

  // ---- per-area sections --------------------------------------------------
  function renderAreas(areas) {
    var el = $("ms-areas");
    if (!el) return;
    if (!areas || !areas.length) { el.innerHTML = '<div class="empty">Analysis pending.</div>'; return; }
    el.innerHTML = areas.map(function (a) {
      var kpis = (a.kpis || []).map(function (k) {
        return '<div class="ms-kpi"><div class="ms-kpi-val">' + esc(k.value) +
          '</div><div class="ms-kpi-lab">' + esc(k.label) + "</div>" +
          (k.caveat ? '<div class="ms-kpi-cav">' + esc(k.caveat) + "</div>" : "") + "</div>";
      }).join("");
      return '<section class="panel ms-area">' +
        '<div class="panel-head"><span class="panel-label">' + esc(a.title || a.key) +
        " " + badge(a.confidence) + "</span>" +
        (a.ev_per_mo ? '<span class="panel-meta">est. ' + esc(a.ev_per_mo) + "/mo</span>" : "") +
        "</div>" +
        '<div class="panel-body">' +
        (kpis ? '<div class="ms-kpi-grid">' + kpis + "</div>" : "") +
        (a.narrative ? '<p class="ms-narr">' + esc(a.narrative) + "</p>" : "") +
        "</div></section>";
    }).join("");
  }

  // ---- execution components ----------------------------------------------
  function renderExec(comps) {
    var el = $("ms-exec");
    if (!el) return;
    if (!comps || !comps.length) { el.innerHTML = '<div class="empty">Architecture pending.</div>'; return; }
    el.innerHTML = comps.map(function (c) {
      return '<div class="ms-exec-card">' +
        '<div class="ms-exec-name">' + esc(c.name) + "</div>" +
        '<div class="ms-exec-purpose">' + esc(c.purpose) + "</div>" +
        (c.depends_on ? '<div class="ms-exec-dep"><span>uses</span> ' + esc(c.depends_on) + "</div>" : "") +
        (c.prereq_data ? '<div class="ms-exec-pre"><span>needs</span> ' + esc(c.prereq_data) + "</div>" : "") +
        "</div>";
    }).join("");
  }

  // ---- roadmap ------------------------------------------------------------
  function renderRoadmap(phases) {
    var el = $("ms-roadmap");
    if (!el) return;
    if (!phases || !phases.length) { el.innerHTML = '<div class="empty">Roadmap pending.</div>'; return; }
    el.innerHTML = phases.map(function (p, i) {
      var items = (p.items || []).map(function (it) { return "<li>" + esc(it) + "</li>"; }).join("");
      return '<div class="ms-phase">' +
        '<div class="ms-phase-head"><span class="ms-phase-num">' + (i + 1) + "</span>" +
        '<span class="ms-phase-name">' + esc(p.phase) + "</span></div>" +
        (items ? "<ul>" + items + "</ul>" : "") +
        (p.gate ? '<div class="ms-phase-gate">⛒ gate: ' + esc(p.gate) + "</div>" : "") +
        "</div>";
    }).join("");
  }

  // ==== Polymarket orderflow & wallet intelligence ==========================
  // Renders ./microstructure/orderflow.json into #of-root, fully independent
  // of index.json. Missing/broken feed -> single clean empty-state card; the
  // rest of the page is untouched. All user-derived strings (wallet names,
  // pseudonyms, slugs, notes) pass through esc(); all style-attr widths pass
  // through pctW() which emits clamped numerics only.

  var CAT_LABELS = {
    advancement_r32: "Reach R32",
    advancement_r16: "Reach R16",
    advancement_qf: "Reach QF",
    advancement_sf: "Reach SF",
    advancement_final: "Reach final",
    winner: "Tournament winner",
    group_winner: "Group winner",
    match_1x2: "Match 1X2",
    other_future: "Other futures"
  };
  function catLabel(c) {
    if (c == null || c === "") return "—";
    var k = String(c);
    if (Object.prototype.hasOwnProperty.call(CAT_LABELS, k)) return CAT_LABELS[k];
    return k.replace(/_/g, " ");
  }
  function ofNum(v) {
    if (v == null) return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function fmtInt(v) { v = ofNum(v); return v == null ? "—" : Math.round(v).toLocaleString("en-GB"); }
  function fmtUsd(v) {
    v = ofNum(v); if (v == null) return "—";
    var sign = v < 0 ? "-" : ""; var a = Math.abs(v); var s;
    if (a >= 1e9) s = (a / 1e9).toFixed(a >= 1e10 ? 0 : 2) + "B";
    else if (a >= 1e6) s = (a / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    else if (a >= 1e3) s = (a / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "k";
    else if (a >= 100) s = a.toFixed(0);
    else s = a.toFixed(a >= 10 ? 1 : 2);
    return sign + "$" + s;
  }
  function fmtPnl(v) {
    v = ofNum(v); if (v == null) return '<span class="dim">—</span>';
    var cls = v > 0 ? "pos" : v < 0 ? "neg" : "dim";
    return '<span class="' + cls + '">' + (v > 0 ? "+" : "") + fmtUsd(v) + "</span>";
  }
  function fmtPct(v) { v = ofNum(v); return v == null ? "—" : (v * 100).toFixed(1) + "%"; }
  function fmtRoi(v) {
    v = ofNum(v); if (v == null) return '<span class="dim">—</span>';
    var cls = v > 0 ? "pos" : v < 0 ? "neg" : "dim";
    return '<span class="' + cls + '">' + (v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%</span>";
  }
  function fmtCents(v, signed) {
    v = ofNum(v); if (v == null) return "—";
    return (signed && v > 0 ? "+" : "") + v.toFixed(1) + "¢";
  }
  function fmtSecs(v) {
    v = ofNum(v); if (v == null) return "—";
    if (v >= 3600) return (v / 3600).toFixed(1) + "h";
    if (v >= 90) return (v / 60).toFixed(1) + "m";
    return v.toFixed(1) + "s";
  }
  function pctW(v) { // clamped numeric string for style="width:X%" — injection-safe
    v = ofNum(v); if (v == null) return "0";
    return (Math.max(0, Math.min(1, v)) * 100).toFixed(1);
  }
  function shortAddr(a) {
    var s = String(a == null ? "" : a);
    if (!s) return "—";
    return s.length > 12 ? s.slice(0, 6) + "…" + s.slice(-4) : s;
  }
  function shortTs(t) {
    var s = String(t == null ? "" : t);
    return s ? s.replace("T", " ").slice(0, 16) : "—";
  }
  function walletCell(wallet, name) {
    var h = '<span class="of-wallet" title="' + esc(wallet) + '">' + esc(shortAddr(wallet)) + "</span>";
    if (name) h += ' <span class="of-wname">' + esc(name) + "</span>";
    return h;
  }
  var PARTIAL_TITLE = "partial fill history — this wallet has volume in markets that hit the " +
    "trade-history API cap and has not been user-backfilled; PnL/ROI/win-rate may be missing legs";
  function partialMark(r) {
    return r && r.partial_history ? '<sup class="of-partial" title="' + esc(PARTIAL_TITLE) + '">†</sup>' : "";
  }
  function archChip(a) {
    return a ? '<span class="of-chip">' + esc(a) + "</span>" : '<span class="dim">—</span>';
  }
  function ofPanel(label, meta, body, scroll) {
    // label is a static string built in this file (never user data).
    return '<section class="panel of-panel"><div class="panel-head">' +
      '<span class="panel-label">' + label + "</span>" +
      (meta ? '<span class="panel-meta">' + esc(meta) + "</span>" : "") +
      '</div><div class="panel-body' + (scroll ? " panel-scroll" : "") + '">' + body + "</div></section>";
  }

  // Panel 1 — coverage strip.
  function ofCoveragePanel(w, gen) {
    w = w || {};
    var truncated = w.truncated_markets || [];
    var items = [
      ["taker fills", fmtInt(w.n_trades), null],
      ["wallets", fmtInt(w.n_wallets), null],
      ["markets", fmtInt(w.n_markets), null],
      ["usd volume", fmtUsd(w.usd_volume), null],
      ["window", shortTs(w.from_utc).slice(0, 10) + " → " + shortTs(w.to_utc).slice(0, 10),
        (w.from_utc || "?") + " → " + (w.to_utc || "?")],
      ["truncated mkts", fmtInt(truncated.length),
        truncated.length ? truncated.length + " markets hit the 3000-fill API cap (large-fill sweep only " +
            "beyond; wallet PnL there can be missing legs), e.g. " + truncated.slice(0, 8).join(", ") +
            (truncated.length > 8 ? " … +" + (truncated.length - 8) + " more (full list in orderflow.json)" : "")
          : "no market hit the trade-history API cap"]
    ];
    var cards = items.map(function (it) {
      return '<div class="ms-cov-card"' + (it[2] ? ' title="' + esc(it[2]) + '"' : "") + '>' +
        '<div class="ms-cov-val">' + esc(it[1]) + '</div><div class="ms-cov-lab">' + esc(it[0]) + "</div></div>";
    }).join("");
    return ofPanel("🌊 Orderflow Coverage // Taker Fills Captured",
      gen ? "generated " + shortTs(gen) : "",
      '<div class="ms-cov-grid">' + cards + "</div>");
  }

  // Panel 2 — headline KPIs.
  function ofHeadlinePanel(items) {
    if (!items || !items.length) return "";
    var kpis = items.map(function (k) {
      return '<div class="ms-kpi"><div class="ms-kpi-val">' + esc(k.value) +
        '</div><div class="ms-kpi-lab">' + esc(k.label) + "</div>" +
        (k.caveat ? '<div class="ms-kpi-cav">' + esc(k.caveat) + "</div>" : "") + "</div>";
    }).join("");
    return ofPanel("📈 Headline Reads // What The Flow Says", items.length + " KPIs",
      '<div class="ms-kpi-grid">' + kpis + "</div>");
  }

  // Shared wallet leaderboard table (smart / dumb / whales — WALLETROW cols).
  function walletTable(rows) {
    if (!rows || !rows.length) return '<div class="empty">No wallets qualify yet.</div>';
    var body = rows.map(function (r) {
      var pnlTitle = "realized " + fmtUsd(r.realized_pnl) + " · mark-to-market " + fmtUsd(r.mtm_pnl) +
        (r.partial_history ? " · † " + PARTIAL_TITLE : "");
      var roiTitle = r.win_rate == null ? "win rate n/a" : "win rate " + fmtPct(r.win_rate);
      return "<tr>" +
        '<td class="of-wcell">' + walletCell(r.wallet, r.name) + "</td>" +
        '<td class="ms-num">' + fmtInt(r.trades) + "</td>" +
        '<td class="ms-num">' + fmtUsd(r.gross_usd) + "</td>" +
        '<td class="ms-num" title="' + esc(pnlTitle) + '">' + fmtPnl(r.total_pnl) + partialMark(r) + "</td>" +
        '<td class="ms-num" title="' + esc(roiTitle) + '">' + fmtRoi(r.roi) + "</td>" +
        '<td class="ms-num">' + fmtCents(r.informedness_cents, true) + "</td>" +
        "<td>" + archChip(r.archetype) + "</td>" +
        '<td class="of-cat">' + esc(catLabel(r.top_category)) + "</td></tr>";
    }).join("");
    return '<div class="of-tablewrap"><table class="ms-table of-table"><thead><tr>' +
      "<th>Wallet</th><th>Trades</th><th>Gross</th><th>PnL</th><th>ROI</th><th>Info ¢</th><th>Type</th><th>Top mkt</th>" +
      "</tr></thead><tbody>" + body + "</tbody></table></div>";
  }

  // Panel 3 — smart vs dumb cohorts.
  function cohortCol(title, cls, agg, rows) {
    agg = agg || {};
    return '<div class="of-col">' +
      '<div class="of-col-head"><span class="of-col-name ' + cls + '">' + title + "</span>" +
      '<span class="of-col-agg">' + fmtInt(agg.n_wallets) + " wallets · " + fmtUsd(agg.gross_usd) +
      " gross · " + fmtPct(agg.roi) + " ROI</span></div>" +
      walletTable(rows) + "</div>";
  }
  function ofCohortsPanel(cohorts, lb) {
    cohorts = cohorts || {};
    var smart = (lb && lb.smart) || [], dumb = (lb && lb.dumb) || [];
    if (!smart.length && !dumb.length && !cohorts.definition) return "";
    var body =
      (cohorts.definition ? '<p class="of-caption">' + esc(cohorts.definition) + "</p>" : "") +
      '<div class="of-cols">' +
      cohortCol("🧠 Smart money", "of-smart", cohorts.smart, smart) +
      cohortCol("🐟 Dumb money", "of-dumb", cohorts.dumb, dumb) +
      "</div>";
    return ofPanel("🧠 Smart Money vs Dumb Money // Wallet Cohorts",
      (smart.length + dumb.length) + " wallets listed", body);
  }

  // Panel 4 — whales & first movers.
  function firstMoverTable(rows) {
    if (!rows || !rows.length) return '<div class="empty">No first-mover wallets detected yet.</div>';
    var body = rows.map(function (r) {
      return "<tr>" +
        '<td class="of-wcell">' + walletCell(r.wallet, r.name) + "</td>" +
        '<td class="ms-num">' + fmtInt(r.n_first_mover) + "</td>" +
        '<td class="ms-num">' + fmtPct(r.jump_share) + "</td>" +
        '<td class="ms-num">' + fmtUsd(r.avg_usd) + "</td>" +
        '<td class="ms-num">' + fmtPnl(r.total_pnl) + partialMark(r) + "</td></tr>";
    }).join("");
    return '<div class="of-tablewrap"><table class="ms-table of-table"><thead><tr>' +
      "<th>Wallet</th><th>Jumps led</th><th>Jump share</th><th>Avg $</th><th>PnL</th>" +
      "</tr></thead><tbody>" + body + "</tbody></table></div>";
  }
  function ofWhalesPanel(lb) {
    var whales = (lb && lb.whales) || [], fm = (lb && lb.first_movers) || [];
    if (!whales.length && !fm.length) return "";
    var body = '<div class="of-cols">' +
      '<div class="of-col"><div class="of-col-head"><span class="of-col-name">🐋 Whales — largest gross</span></div>' +
      walletTable(whales) + "</div>" +
      '<div class="of-col"><div class="of-col-head"><span class="of-col-name">⚡ First movers — in before the jump</span></div>' +
      firstMoverTable(fm) + "</div></div>";
    return ofPanel("🐋 Whales &amp; First Movers",
      whales.length + " whales · " + fm.length + " first movers", body);
  }

  // Panel 5 — archetype breakdown (CSS bars by usd_share).
  function ofArchetypesPanel(arch) {
    if (!arch || !arch.length) return "";
    var rows = arch.map(function (a) {
      return '<div class="of-arch-row"' + (a.description ? ' title="' + esc(a.description) + '"' : "") + ">" +
        '<div class="of-arch-lab">' + esc(a.label || a.key) + "</div>" +
        '<div class="of-arch-right">' +
        '<div class="of-bar-track"><div class="of-bar-fill" style="width:' + pctW(a.usd_share) + '%"></div></div>' +
        '<div class="of-arch-meta">' + fmtPct(a.usd_share) + " of $ · " + fmtInt(a.n_wallets) +
        " wallets · " + fmtUsd(a.gross_usd) + " gross · med trade " + fmtUsd(a.median_trade_usd) +
        " · ROI " + fmtRoi(a.roi) + "</div></div></div>";
    }).join("");
    return ofPanel("🧬 Wallet Archetypes // Who Provides The Flow", arch.length + " archetypes", rows);
  }

  // Panel 6 — market preference matrix.
  function ofMatrixPanel(mx) {
    if (!mx || !mx.length) return "";
    var body = mx.map(function (m) {
      var splitTitle = "smart " + fmtPct(m.smart_usd_share) + " · dumb " + fmtPct(m.dumb_usd_share) + " of category $";
      return "<tr>" +
        '<td class="ms-edge-name">' + esc(m.label || catLabel(m.category)) + "</td>" +
        '<td class="ms-num">' + fmtInt(m.n_trades) + "</td>" +
        '<td class="ms-num">' + fmtUsd(m.usd) + "</td>" +
        '<td class="ms-num">' + fmtInt(m.n_wallets) + "</td>" +
        '<td class="ms-num">' + fmtUsd(m.avg_trade_usd) + "</td>" +
        '<td><div class="of-bp" title="taker BUY share of category USD">' +
        '<div class="of-bp-track"><div class="of-bp-fill" style="width:' + pctW(m.buy_pressure) + '%"></div></div>' +
        '<span class="ms-num">' + fmtPct(m.buy_pressure) + "</span></div></td>" +
        '<td><div class="of-bp" title="' + esc(splitTitle) + '">' +
        '<div class="of-split"><div class="of-split-smart" style="width:' + pctW(m.smart_usd_share) + '%"></div>' +
        '<div class="of-split-dumb" style="width:' + pctW(m.dumb_usd_share) + '%"></div></div>' +
        '<span class="ms-num">' + fmtPct(m.smart_usd_share) + " / " + fmtPct(m.dumb_usd_share) + "</span></div></td></tr>";
    }).join("");
    return ofPanel("🗺️ Market Preference Matrix // Where The Money Goes", mx.length + " categories",
      '<div class="of-tablewrap"><table class="ms-table of-table"><thead><tr>' +
      "<th>Market</th><th>Trades</th><th>USD</th><th>Wallets</th><th>Avg trade</th>" +
      "<th>Buy pressure</th><th>Smart / dumb $ share</th>" +
      "</tr></thead><tbody>" + body + "</tbody></table></div>");
  }

  // Panel 7 — trade size distribution (log-scaled CSS bars).
  function ofSizePanel(dist) {
    if (!dist || !dist.length) return "";
    var maxLog = 0, totN = 0;
    dist.forEach(function (b) {
      var u = ofNum(b.usd) || 0;
      if (u > 0) maxLog = Math.max(maxLog, Math.log(u + 1));
      totN += ofNum(b.n) || 0;
    });
    var cols = dist.map(function (b) {
      var u = ofNum(b.usd) || 0;
      var h = (u > 0 && maxLog > 0) ? Math.max(4, 100 * Math.log(u + 1) / maxLog) : 0;
      return '<div class="of-size-col" title="' + esc(fmtInt(b.n) + " trades · " + fmtUsd(b.usd) + " total") + '">' +
        '<div class="of-size-n">' + fmtInt(b.n) + "</div>" +
        '<div class="of-size-barbox"><div class="of-size-bar" style="height:' + h.toFixed(1) + '%"></div></div>' +
        '<div class="of-size-usd">' + fmtUsd(b.usd) + "</div>" +
        '<div class="of-size-lab">' + esc(b.bucket) + "</div></div>";
    }).join("");
    return ofPanel("📏 Trade Size Distribution // Bar = USD (log scale), Label = Count",
      fmtInt(totN) + " fills", '<div class="of-sizes">' + cols + "</div>");
  }

  // Panel 8 — event-reaction latency + honesty notes.
  function ofLatencyPanel(lat, honesty) {
    lat = lat || {};
    var byCat = lat.by_category || [], xm = lat.cross_market || [];
    var notes = (lat.notes || []).concat(honesty || []);
    if (!byCat.length && !xm.length && !notes.length) return "";
    var html = "";
    if (byCat.length) {
      html += '<div class="of-subhead">Reprice speed after goal-sized jumps</div>' +
        '<div class="of-tablewrap"><table class="ms-table of-table"><thead><tr>' +
        "<th>Market</th><th>Jumps</th><th>Median reprice</th><th>P90 reprice</th><th>Median move</th><th>First-30s $ share</th>" +
        "</tr></thead><tbody>" +
        byCat.map(function (c) {
          return '<tr><td class="ms-edge-name">' + esc(catLabel(c.category)) + "</td>" +
            '<td class="ms-num">' + fmtInt(c.n_jumps) + "</td>" +
            '<td class="ms-num">' + fmtSecs(c.median_reprice_s) + "</td>" +
            '<td class="ms-num">' + fmtSecs(c.p90_reprice_s) + "</td>" +
            '<td class="ms-num">' + fmtCents(c.median_move_cents) + "</td>" +
            '<td class="ms-num">' + fmtPct(c.first30s_usd_share) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }
    if (xm.length) {
      html += '<div class="of-subhead">Cross-market lag — trigger moves, correlated market follows</div>' +
        '<div class="of-tablewrap"><table class="ms-table of-table"><thead><tr>' +
        "<th>Team</th><th>Trigger → follower</th><th>Lag</th><th>Follower move</th><th>When (UTC)</th>" +
        "</tr></thead><tbody>" +
        xm.map(function (x) {
          return '<tr><td class="ms-edge-name">' + esc(x.team) + "</td>" +
            "<td>" + esc(catLabel(x.trigger)) + ' <span class="dim">→</span> ' + esc(catLabel(x.follower)) + "</td>" +
            '<td class="ms-num">' + fmtSecs(x.lag_s) + "</td>" +
            '<td class="ms-num">' + fmtCents(x.follower_move_cents) + "</td>" +
            '<td class="ms-num">' + esc(shortTs(x.ts_utc)) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }
    if (notes.length) {
      html += '<div class="ms-honesty of-honesty"><div class="ms-honesty-title">⚠ Interpretation limits</div><ul>' +
        notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul></div>";
    }
    return ofPanel("⏱️ Event-Reaction Latency // How Fast Does Polymarket Reprice",
      lat.n_jumps != null ? fmtInt(lat.n_jumps) + " jumps detected" : "", html);
  }

  function renderOrderflow(d) {
    var el = $("of-root"); if (!el) return;
    var lb = d.leaderboards || {};
    el.innerHTML =
      ofCoveragePanel(d.window, d.generated_utc) +
      ofHeadlinePanel(d.headline) +
      ofCohortsPanel(d.cohorts, lb) +
      ofWhalesPanel(lb) +
      ofArchetypesPanel(d.archetypes) +
      ofMatrixPanel(d.category_matrix) +
      ofSizePanel(d.size_distribution) +
      ofLatencyPanel(d.latency, d.honesty_notes);
  }
  function renderOrderflowEmpty() {
    var el = $("of-root"); if (!el) return;
    el.innerHTML = '<section class="panel"><div class="panel-body"><div class="of-empty">' +
      '<span class="of-empty-dot"></span>' +
      "<span>Orderflow feed not generated yet — run <code>scripts/microstructure/orderflow.py</code></span>" +
      "</div></div></section>";
  }
  function loadOrderflow(attempt) {
    attempt = attempt || 1;
    fetch("./microstructure/orderflow.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        renderOrderflow(d);
      })
      .catch(function () {
        if (attempt < 3) { setTimeout(function () { loadOrderflow(attempt + 1); }, attempt * 1500); return; }
        renderOrderflowEmpty();
      });
  }

  function render(d) {
    renderCoverage(d.coverage);
    renderHonesty(d.honesty_notes, d.coverage);
    renderRanked(d.ranked_edges);
    renderAreas(d.areas);
    renderExec(d.execution_components);
    renderRoadmap(d.roadmap);
    var foot = $("ms-foot");
    if (foot) {
      foot.textContent = "Generated " + (d.generated_at || "—") +
        ". All metrics computed from the project odds database (" +
        ((d.coverage && d.coverage.window) || "see coverage") +
        ") and adversarially verified. Framework-only items are method specs, not data-backed claims.";
    }
  }

  function load(attempt) {
    attempt = attempt || 1;
    fetch("./microstructure/index.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        var nd = $("nodata"); if (nd) nd.hidden = true;
        render(d);
      })
      .catch(function () {
        if (attempt < 3) { setTimeout(function () { load(attempt + 1); }, attempt * 1500); return; }
        var nd = $("nodata"); if (nd) nd.hidden = false;
      });
  }

  function boot() { load(); loadOrderflow(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { boot(); });
  } else { boot(); }
})();
