/* Event Markets — Model vs Market forest (main site).
 *
 * Fetches ./forest_data.json (written by scripts/wca_forest_data.py from the
 * real model + live-odds pipeline) and renders a per-fixture dot-plot: a MODEL
 * dot and a MARKET dot per outcome, with a bar = |model - market| edge. Where
 * no live market price exists for an outcome, only the model dot is drawn and
 * labelled "model". Never fabricates a value — everything comes from the feed.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var num = function (v, dp) {
    if (v == null || isNaN(v)) return "–";
    return Number(v).toFixed(dp == null ? 1 : dp);
  };
  var empty = function (msg) { return "<div class='empty'>" + esc(msg) + "</div>"; };

  var selected = null; // Set of selected fixture indices
  var FEED = null;

  function svg(w, h, inner) {
    return "<svg class='a-svg' viewBox='0 0 " + w + " " + h +
      "' preserveAspectRatio='xMidYMid meet' role='img'>" + inner + "</svg>";
  }

  function render() {
    var d = FEED;
    if (!d || !d.fixtures || !d.fixtures.length) {
      $("forest-nav").innerHTML = "";
      $("forest-body").innerHTML =
        "<div class='fpending'>No live event-market data. The feed is generated when " +
        "upcoming fixtures have model + odds coverage.</div>";
      return;
    }
    var fixtures = d.fixtures;

    if (selected === null) selected = new Set([0]);
    Array.from(selected).forEach(function (i) {
      if (i >= fixtures.length) selected.delete(i);
    });
    if (!selected.size) selected.add(0);

    $("forest-nav").innerHTML = fixtures.map(function (fx, i) {
      var on = selected.has(i);
      var flag = fx.has_market ? "" : "<i class='fx-nm'>model only</i>";
      return "<span class='fx-chip" + (on ? " on" : "") + "' data-idx='" + i + "'>" +
        esc(fx.fixture) + flag + "</span>";
    }).join("");

    Array.prototype.forEach.call(
      $("forest-nav").querySelectorAll(".fx-chip"),
      function (c) {
        c.addEventListener("click", function () {
          var idx = parseInt(c.getAttribute("data-idx"), 10);
          if (selected.has(idx)) {
            if (selected.size > 1) selected.delete(idx);
          } else {
            selected.add(idx);
          }
          render();
        });
      }
    );

    var gen = d.meta && d.meta.generated;
    if (gen) {
      $("forest-meta").textContent = "as of " + String(gen).slice(0, 16) +
        " · model vs best available price · select one or more games";
      $("foot-gen").textContent = "feed generated " + String(gen);
    }

    var html = fixtures
      .filter(function (_, i) { return selected.has(i); })
      .map(function (fx) { return fixtureBlock(fx); })
      .join("");

    html += "<div class='legend'>" +
      "<span><span class='sw' style='background:var(--accent)'></span>model</span>" +
      "<span><span class='sw' style='background:var(--polymarket)'></span>market price</span>" +
      "<span class='dim'>bar = |model − market| edge &middot; “model” = no live market for this outcome</span></div>";

    $("forest-body").innerHTML = html;
  }

  function fixtureBlock(fx) {
    var rows = fx.rows || [];
    var ko = fx.kickoff ? "<span class='em-ko'>" + esc(String(fx.kickoff).replace("T", " ").slice(0, 16)) + "</span>" : "";
    var nolive = fx.has_market ? "" : "<span class='em-nolive'>NO LIVE MARKET</span>";
    return "<div class='em-fx-head'>" + esc(fx.fixture) + ko + nolive + "</div>" +
      forestSvg(rows);
  }

  function forestSvg(rows) {
    if (!rows.length) return empty("No market data");
    var rowH = 22, secH = 18, W = 920, lblW = 168, P = { l: lblW, r: 64, t: 8 };
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
          "' text-anchor='end' class='cx-tick' font-weight='600' fill='var(--accent)'>" +
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

  function boot() {
    fetch("./forest_data.json", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) { FEED = d; render(); })
      .catch(function (err) {
        var nd = $("nodata");
        if (nd) { nd.hidden = false; }
        var msg = $("nodata-msg");
        if (msg) { msg.textContent = "NO DATA FEED — forest_data.json unavailable"; }
        $("forest-body").innerHTML =
          "<div class='fpending'>Could not load forest_data.json (" + esc(err.message) + ").</div>";
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
