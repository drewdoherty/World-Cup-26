/* World Cup Alpha — model-vs-market scores page.
 *
 * Loads ./scores_data.json (cache-busted) and renders, per fixture:
 *   1. a scoreline ladder (score, probability bar, fair odds), and
 *   2. a compact venue table comparing the model-implied fair 1X2 against
 *      each bookmaker's (and Polymarket's) priced odds, tinting each cell
 *      green/red when the venue price beats / misses the model fair value.
 *
 * Everything degrades to a clean "no data" state. No external assets, no CDN.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------

  // Format an already-percentage number (0..100, as emitted by the card
  // scoreline parser) to one decimal place; null/undefined/NaN -> em-dash.
  function pct1(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(1) + "%";
  }
  // Format a 0..1 probability to a percentage.
  function prob01(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function num(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(dp === undefined ? 2 : dp);
  }
  // Signed edge as a percentage, e.g. +3.4% / -1.1%.
  function edgePct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v) * 100;
    return (n >= 0 ? "+" : "") + n.toFixed(1) + "%";
  }
  // Tint class for a venue price cell given the edge vs model fair.
  // edge >= +2% -> beats (green), edge <= -2% -> misses (red dim), else flat.
  function edgeClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "edge-flat";
    var n = Number(v);
    if (n >= 0.02) return "edge-up";
    if (n <= -0.02) return "edge-down";
    return "edge-flat";
  }
  function timeOnly(ts) {
    if (!ts) return "";
    var t = String(ts);
    var idx = t.indexOf("T");
    if (idx >= 0) return t.slice(idx + 1, idx + 6); // HH:MM
    return t;
  }

  // Minimal text escaping for any value sourced from scores_data.json before
  // it goes into innerHTML.
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- renderers ----------------------------------------------------------

  function renderScoreLadder(fx) {
    var scores = (fx.scores || []).slice(0, 6);
    if (!scores.length) {
      return '<div class="empty">no scorelines</div>';
    }
    var maxProb = scores.reduce(function (m, s) {
      return Math.max(m, Number(s.prob || 0));
    }, 0);

    return scores.map(function (s) {
      var p = Number(s.prob || 0);
      var frac = maxProb > 0 ? (p / maxProb) : 0;
      return '<div class="score-row">' +
        '<span class="score-label">' + esc(s.score) + '</span>' +
        '<span class="score-bar"><span class="score-fill" style="width:' +
          (frac * 100).toFixed(1) + '%"></span></span>' +
        '<span class="score-prob">' + esc(pct1(s.prob)) + '</span>' +
        '<span class="score-fair num dim">' + esc(num(s.fair)) + '</span>' +
      '</div>';
    }).join("");
  }

  function renderModelFooter(fx) {
    var foot = [];
    if (fx.over_under) {
      var ou = fx.over_under;
      foot.push('<span><b>O/U ' + esc(num(ou.line, 1)) + '</b> ' +
        esc(pct1(ou.over)) + ' / ' + esc(pct1(ou.under)) + '</span>');
    }
    if (fx.btts !== null && fx.btts !== undefined && !isNaN(fx.btts)) {
      foot.push('<span><b>BTTS</b> ' + esc(pct1(fx.btts)) + '</span>');
    }
    if (!foot.length) return "";
    return '<div class="pred-foot">' + foot.join("") + '</div>';
  }

  // The model-fair row + one row per venue. Each venue cell is tinted by its
  // edge vs the model fair price for that leg.
  function renderVenueTable(fx) {
    var model = fx.model_1x2 || null;
    var venues = fx.venues || [];

    if (!venues.length) {
      return '<div class="venue-empty">No priced markets matched</div>';
    }

    // Model fair row: the implied fair decimal price (1 / model_prob) per leg.
    function fairCell(p) {
      if (p === null || p === undefined || isNaN(p) || Number(p) <= 0) {
        return '<td class="vt-cell vt-fair">—</td>';
      }
      var dec = 1 / Number(p);
      return '<td class="vt-cell vt-fair num">' + num(dec) +
        '<span class="vt-imp">' + prob01(p, 0) + '</span></td>';
    }

    var modelRow = '<tr class="vt-model-row">' +
      '<td class="vt-venue">model fair</td>' +
      fairCell(model ? model.home : null) +
      fairCell(model ? model.draw : null) +
      fairCell(model ? model.away : null) +
    '</tr>';

    function priceCell(price, edge) {
      var cls = edgeClass(edge);
      if (price === null || price === undefined || isNaN(price)) {
        return '<td class="vt-cell ' + cls + '">—</td>';
      }
      return '<td class="vt-cell ' + cls + ' num">' + num(price) +
        '<span class="vt-edge">' + esc(edgePct(edge)) + '</span></td>';
    }

    var venueRows = venues.map(function (v) {
      var prices = v.selection_prices || {};
      var edge = v.edge_vs_model || {};
      return '<tr>' +
        '<td class="vt-venue" title="' + esc(v.venue) + '">' + esc(v.venue) + '</td>' +
        priceCell(prices.home, edge.home) +
        priceCell(prices.draw, edge.draw) +
        priceCell(prices.away, edge.away) +
      '</tr>';
    }).join("");

    var approxNote = fx.approx_1x2
      ? '<div class="vt-note">model 1X2 is approximate (top-k scores only)</div>'
      : '';

    return '<table class="venue-table">' +
        '<thead><tr>' +
          '<th class="vt-venue">venue</th>' +
          '<th>home</th><th>draw</th><th>away</th>' +
        '</tr></thead>' +
        '<tbody>' + modelRow + venueRows + '</tbody>' +
      '</table>' + approxNote;
  }

  function renderFixtureCard(fx) {
    var kickoff = timeOnly(fx.kickoff);
    var kickHtml = kickoff
      ? '<span class="sc-kick num">' + esc(kickoff) + '</span>'
      : '';
    return '<div class="sc-card">' +
        '<div class="sc-head">' +
          '<span class="sc-title">' + esc(fx.fixture) + '</span>' +
          kickHtml +
        '</div>' +
        '<div class="sc-body">' +
          '<div class="sc-ladder">' +
            renderScoreLadder(fx) +
            renderModelFooter(fx) +
          '</div>' +
          '<div class="sc-venues">' +
            renderVenueTable(fx) +
          '</div>' +
        '</div>' +
      '</div>';
  }

  function renderScores(d) {
    var fixtures = d.fixtures || [];
    if (!fixtures.length) {
      $("scores").innerHTML = '<div class="empty">No fixture predictions</div>';
      $("scores-meta").textContent = "0";
      return;
    }
    $("scores-meta").textContent =
      fixtures.length + " fixture" + (fixtures.length === 1 ? "" : "s");
    $("scores").innerHTML = fixtures.map(renderFixtureCard).join("");
  }

  function renderFooter(d) {
    var gen = (d.meta && d.meta.generated) ? d.meta.generated : "";
    $("foot-gen").textContent = gen ? ("Generated " + gen) : "Generated —";
  }

  function showNoData(msg) {
    var el = $("nodata");
    $("nodata-msg").textContent = msg || "NO DATA FEED";
    el.hidden = false;
  }

  function render(d) {
    renderScores(d);
    renderFooter(d);
  }

  // ---- boot ---------------------------------------------------------------

  function load() {
    var url = "./scores_data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        render(d);
      })
      .catch(function (err) {
        showNoData("SCORES FEED UNAVAILABLE");
        render({ fixtures: [], meta: {} });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
