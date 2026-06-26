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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { load(); });
  } else { load(); }
})();
