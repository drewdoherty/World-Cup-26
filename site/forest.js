/* Event Markets — Model vs Market forest (main site).
 *
 * Fetches ./forest_data.json (written by scripts/wca_event_markets.py from the
 * production model + a live Polymarket Gamma/CLOB snapshot) and renders a
 * per-fixture dot-plot: a MODEL dot and a MARKET dot per outcome with exact
 * probability annotations, and a signal-coloured bar between them:
 *   green  = model >= market + 2pp  -> BACK the outcome
 *   red    = market >= model + 2pp  -> LAY the outcome / back the complement
 *   grey   = inside the 2pp band    -> no trade signal
 * Where PM lists no market for an outcome the row is labelled "no PM market";
 * where the model cannot price a market fairly the row shows the market dot
 * only with the (honest) reason. Never fabricates a value — everything comes
 * from the feed.
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
  var pct = function (v, dp) {
    if (v == null || isNaN(v)) return "—";
    return (v * 100).toFixed(dp == null ? 1 : dp) + "%";
  };
  var empty = function (msg) { return "<div class='empty'>" + esc(msg) + "</div>"; };

  var SIGNAL_PP = 2.0; // |model - market| >= 2pp -> trade signal

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
        "upcoming fixtures have model + Polymarket coverage.</div>";
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
      var flag = fx.has_market ? "" : "<i class='fx-nm'>no PM markets</i>";
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
        " · model vs Polymarket · select one or more games";
      $("foot-gen").textContent = "feed generated " + String(gen);
    }

    var html = fixtures
      .filter(function (_, i) { return selected.has(i); })
      .map(function (fx) { return fixtureBlock(fx); })
      .join("");

    html += "<div class='legend'>" +
      "<span><span class='sw' style='background:var(--accent)'></span>model</span>" +
      "<span><span class='sw' style='background:var(--polymarket)'></span>Polymarket price</span>" +
      "<span><span class='sw sw-bar' style='background:var(--pos)'></span>BACK signal (model &ge; market + 2pp)</span>" +
      "<span><span class='sw sw-bar' style='background:var(--neg)'></span>LAY signal (market &ge; model + 2pp)</span>" +
      "<span><span class='sw sw-bar' style='background:var(--border-2)'></span>inside 2pp &middot; no trade</span>" +
      "<span class='dim'>&ldquo;no PM market&rdquo; = Polymarket lists no market for this outcome &middot; " +
      "dimmed rows = display-only, never cash (longshot / killed family) &middot; " +
      "&ldquo;Team to Advance&rdquo; settles ET+pens, everything else 90 minutes</span></div>";

    $("forest-body").innerHTML = html;
  }

  function fixtureBlock(fx) {
    var rows = fx.rows || [];
    var ko = fx.kickoff ? "<span class='em-ko'>" + esc(String(fx.kickoff).replace("T", " ").slice(0, 16)) + "</span>" : "";
    var nolive = fx.has_market ? "" : "<span class='em-nolive'>NO PM MARKETS</span>";
    return "<div class='em-fx-head'>" + esc(fx.fixture) + ko + nolive + "</div>" +
      forestSvg(rows);
  }

  function rowTitle(r) {
    var bits = [];
    if (r.model != null) bits.push("model " + pct(r.model));
    if (r.model_source) bits.push("model source: " + r.model_source);
    if (r.model == null && r.model_null_reason) bits.push("model: " + r.model_null_reason);
    if (r.market != null) {
      bits.push("market " + pct(r.market) +
        (r.price_source ? " (" + r.price_source + ")" : ""));
    } else if (r.market_null_reason) {
      bits.push("market: " + r.market_null_reason);
    }
    if (r.edge_pp != null) bits.push("edge " + (r.edge_pp >= 0 ? "+" : "") + num(r.edge_pp, 1) + "pp");
    if (r.signal) bits.push("signal: " + (r.signal === "back" ? "BACK" : "LAY / back the complement"));
    if (r.warning) bits.push("WARNING: " + r.warning);
    if (r.settlement === "ET+pens") bits.push("settles ET+pens (advancement basis)");
    if (r.captured_utc) bits.push("captured " + r.captured_utc);
    return bits.join(" · ");
  }

  function forestSvg(rows) {
    if (!rows.length) return empty("No market data");
    var rowH = 22, secH = 20, W = 1010, lblW = 190;
    var P = { l: lblW, r: 168, t: 8 };
    var annX = W - P.r + 10;   // "model% / market%" annotation column
    var sigX = W - 72;         // right-hand pp / signal label column
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
        var adv = r.settlement === "ET+pens";
        parts.push("<line x1='" + xOf(0) + "' y1='" + (sy - 5) + "' x2='" + xOf(1) +
          "' y2='" + (sy - 5) + "' stroke='var(--border)' stroke-width='0.8'/>");
        parts.push("<g>" + (r.note ? "<title>" + esc(r.note) + "</title>" : "") +
          "<text x='" + (lblW - 8) + "' y='" + sy +
          "' text-anchor='end' class='cx-tick' font-weight='600' fill='" +
          (adv ? "var(--warn)" : "var(--accent)") + "'>" +
          esc(r.section) + (adv ? " · SETTLES ET+PENS" : "") + "</text></g>");
        y += secH;
        return;
      }
      if (r.model == null && r.market == null) {
        // Fully unpriced row (e.g. no PM props event + nothing to model):
        // state both absences explicitly rather than drawing nothing.
        var cy0 = y + rowH / 2;
        parts.push("<g opacity='0.6'><title>" + esc(rowTitle(r)) + "</title>" +
          "<text class='cx-lbl' x='" + (lblW - 8) + "' y='" + (cy0 + 4) +
          "' text-anchor='end'>" + esc(r.label) + "</text>" +
          "<text class='cx-tick' x='" + (xOf(0) + 4) + "' y='" + (cy0 + 4) +
          "' fill='var(--muted)'>" + esc(r.market_null_reason || "no PM market") +
          "</text></g>");
        y += rowH;
        return;
      }
      var cy = y + rowH / 2;
      var xm = r.model != null ? xOf(r.model) : null;
      var xk = r.market != null ? xOf(r.market) : null;
      var dim = r.dimmed ? " opacity='0.5'" : "";
      var g = "<g" + dim + "><title>" + esc(rowTitle(r)) + "</title>";
      g += "<text class='cx-lbl' x='" + (lblW - 8) + "' y='" + (cy + 4) +
        "' text-anchor='end'>" + esc(r.label) +
        (r.warning ? " ⚠" : "") + "</text>";

      // Edge bar: coloured by the +/-2pp back/lay trade-signal rule.
      if (xm != null && xk != null) {
        var edgePp = r.edge_pp != null ? r.edge_pp : (r.model - r.market) * 100;
        var barColor = "var(--border-2)";
        if (r.signal === "back" || (r.signal == null && edgePp >= SIGNAL_PP)) barColor = "var(--pos)";
        else if (r.signal === "lay" || (r.signal == null && edgePp <= -SIGNAL_PP)) barColor = "var(--neg)";
        g += "<line class='whisker' style='stroke:" + barColor + "' x1='" +
          Math.min(xm, xk) + "' y1='" + cy + "' x2='" + Math.max(xm, xk) +
          "' y2='" + cy + "'/>";
        var sigTxt = (edgePp >= 0 ? "+" : "") + num(edgePp, 1) + "pp";
        var sigColor = r.signal ? (r.signal === "back" ? "var(--pos)" : "var(--neg)") : "var(--muted)";
        g += "<text class='cx-tick' x='" + sigX + "' y='" + (cy + 4) +
          "' text-anchor='start' fill='" + sigColor + "'>" + sigTxt +
          (r.signal ? (r.signal === "back" ? " BACK" : " LAY") : "") + "</text>";
      } else if (xk != null) {
        g += "<text class='cx-tick' x='" + (W - 2) + "' y='" + (cy + 4) +
          "' text-anchor='end' fill='var(--muted)'>no model</text>";
      } else {
        g += "<text class='cx-tick' x='" + (W - 2) + "' y='" + (cy + 4) +
          "' text-anchor='end' fill='var(--muted)'>no PM market</text>";
      }

      // Exact model / market probability annotation next to the row.
      var ann = "<tspan fill='var(--accent)'>" +
        (r.model != null ? pct(r.model) : "—") + "</tspan>" +
        "<tspan fill='var(--muted)'> / </tspan>" +
        "<tspan fill='var(--polymarket)'>" +
        (r.market != null ? pct(r.market) : "—") + "</tspan>";
      g += "<text class='cx-tick' x='" + annX + "' y='" + (cy + 4) +
        "' text-anchor='start'>" + ann + "</text>";

      if (xk != null) {
        g += "<circle class='dot-market' cx='" + xk + "' cy='" + cy + "' r='4'/>";
      }
      if (xm != null) {
        g += "<circle class='dot-model' cx='" + xm + "' cy='" + cy + "' r='4.5'/>";
      }
      g += "</g>";
      parts.push(g);
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
