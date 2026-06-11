/* World Cup Alpha — trading-terminal front-end.
 *
 * Loads ./data.json (cache-busted) and renders the ticker, venues, positions
 * and predictions panels. Everything degrades to a clean "no data" state if
 * the feed is missing or malformed. No external assets, no CDN.
 *
 * The ONLY optional external call is an opportunistic enrichment fetch to
 * gamma-api.polymarket.com (live market prices); it is fully guarded and the
 * page works identically when that request is blocked or fails.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------

  var SYM = { GBP: "£", USD: "$", EUR: "€" };
  function sym(ccy) { return SYM[ccy] || "£"; }

  function money(n, ccy) {
    if (n === null || n === undefined || isNaN(n)) return sym(ccy) + "0.00";
    var sign = n < 0 ? "-" : "";
    return sign + sym(ccy) + Math.abs(n).toLocaleString("en-GB", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  function signedMoney(n, ccy) {
    if (n === null || n === undefined || isNaN(n)) n = 0;
    var sign = n < 0 ? "-" : "+";
    return sign + sym(ccy) + Math.abs(n).toLocaleString("en-GB", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  // Render a per-currency map {GBP: {...}, USD: {...}} field as "£a + $b".
  // Currencies are NEVER summed — £ and $ are different units.
  function moneyByCcy(byCcy, field, signed) {
    var parts = [];
    ["GBP", "USD", "EUR"].forEach(function (ccy) {
      var blk = (byCcy || {})[ccy];
      if (!blk) return;
      var v = Number(blk[field] || 0);
      if (v === 0 && field !== "settled_pl") return;
      parts.push(signed ? signedMoney(v, ccy) : money(v, ccy));
    });
    return parts.length ? parts.join(" + ") : money(0);
  }
  function pct(n, dp) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return (n * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function dash(v) {
    return (v === null || v === undefined || v === "") ? "—" : v;
  }
  // Format an already-percentage number (0..100, as emitted by the card
  // scoreline parser) to one decimal place; null/undefined/NaN -> em-dash.
  function pct1(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(1) + "%";
  }
  function num(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(dp === undefined ? 2 : dp);
  }
  // Color class for an EV cell: positive -> pos, negative -> neg, and a
  // missing/non-numeric EV (e.g. an acca with no modelled edge) stays neutral
  // rather than being painted green by a Number(null) === 0 coercion.
  function evClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "dim";
    return Number(v) >= 0 ? "pos" : "neg";
  }
  function timeOnly(ts) {
    if (!ts) return "—";
    var t = String(ts);
    var idx = t.indexOf("T");
    if (idx >= 0) return t.slice(idx + 1, idx + 6); // HH:MM
    return t;
  }

  // Minimal text escaping for any value sourced from data.json before it goes
  // into innerHTML. Defends against a hostile match/selection string.
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- renderers ----------------------------------------------------------

  function renderTicker(d) {
    var t = d.totals || {};
    var byCcy = d.totals_by_currency || null;
    var clv = d.clv || {};
    var pl = Number(t.settled_pl || 0);
    var plCls = pl >= 0 ? "pos" : "neg";

    var clvVal = "N/A";
    var hasClv = clv.avg_clv !== null && clv.avg_clv !== undefined &&
      (clv.n_with_close || 0) > 0;
    if (hasClv) {
      var a = Number(clv.avg_clv);
      clvVal = (a >= 0 ? "+" : "") + (a * 100).toFixed(2) + "%";
    }

    // Per-currency display when available (never sums £ with $); falls back
    // to the legacy single-currency totals for old data files.
    var wagered = byCcy ? moneyByCcy(byCcy, "wagered") : money(t.wagered || 0);
    var openExp = byCcy ? moneyByCcy(byCcy, "open_stake") : money(t.open_stake || 0);
    var plStr = byCcy ? moneyByCcy(byCcy, "settled_pl", true) : signedMoney(pl);

    var ticks = [
      ["Total Wagered", wagered, ""],
      ["Open Exposure", openExp, ""],
      ["Settled P&L", plStr, plCls],
      ["Avg CLV", clvVal, hasClv ? (Number(clv.avg_clv) >= 0 ? "pos" : "neg") : "dim"],
      ["Bet Count", String(t.n_bets || 0), ""]
    ];

    $("ticker-stats").innerHTML = ticks.map(function (row) {
      return '<div class="tick">' +
        '<div class="tick-label">' + esc(row[0]) + '</div>' +
        '<div class="tick-value num ' + row[2] + '">' + esc(row[1]) + '</div>' +
        '</div>';
    }).join("");
  }

  var VENUE_COLOR = { sportsbook: "#4ade80", polymarket: "#60a5fa", kalshi: "#2dd4bf" };

  function renderVenues(d) {
    var venues = d.venues || {};
    var order = ["sportsbook", "polymarket", "kalshi"];
    // Hide venues with no bets yet (e.g. kalshi pre-launch); bars are scaled
    // within-currency only, so a £ bar and a $ bar are not length-comparable —
    // the per-row amount label carries the truth.
    var active = order.filter(function (k) {
      return Number((venues[k] || {}).n_bets || 0) > 0;
    });
    if (!active.length) active = ["sportsbook"];
    var amounts = active.map(function (k) {
      return Number((venues[k] || {}).wagered || 0);
    });
    var max = Math.max.apply(null, amounts.concat([0]));

    $("venues-meta").textContent = moneyByCcy(d.totals_by_currency, "wagered");

    var html = active.map(function (k, i) {
      var v = venues[k] || {};
      var ccy = v.currency || "GBP";
      var amt = amounts[i];
      var frac = max > 0 ? (amt / max) : 0;
      var nb = Number(v.n_bets || 0);
      var color = VENUE_COLOR[k] || "#9ca3af";
      return '' +
        '<div class="venue-row">' +
          '<div class="venue-top">' +
            '<span class="venue-name">' + esc(k) + '</span>' +
            '<span class="venue-amt num">' + money(amt, ccy) + '</span>' +
          '</div>' +
          '<div class="venue-track">' +
            '<div class="venue-fill ' + k + '" style="width:' +
              (frac * 100).toFixed(1) + '%;background:' + color + ';opacity:.75"></div>' +
          '</div>' +
          '<div class="venue-sub num">' + nb + ' bet' + (nb === 1 ? '' : 's') +
            ' · open ' + money(v.open_stake || 0, ccy) + '</div>' +
        '</div>';
    }).join("");

    $("venues").innerHTML = html;
  }

  function renderPositions(d) {
    var pos = d.positions || [];
    if (!pos.length) {
      $("positions").innerHTML = '<div class="empty">No open positions</div>';
      $("positions-meta").textContent = "0";
      return;
    }
    $("positions-meta").textContent = String(pos.length);

    var rows = pos.map(function (p) {
      var venue = esc(p.venue || "sportsbook");
      return '<tr>' +
        '<td class="num dim">' + esc(timeOnly(p.ts_utc)) + '</td>' +
        '<td class="pos-match" title="' + esc(p.match) + '">' + esc(dash(p.match)) + '</td>' +
        '<td class="pos-sel" title="' + esc(p.selection) + '">' + esc(dash(p.selection)) + '</td>' +
        '<td class="r num">' + esc(num(p.decimal_odds)) + '</td>' +
        '<td class="r num">' + esc(money(p.stake, p.currency)) + '</td>' +
        '<td class="r num">' + esc(pct(p.model_prob, 0)) + '</td>' +
        '<td class="r num ' + evClass(p.ev) + '">' +
          esc(p.ev === null || p.ev === undefined ? '—' : pct(p.ev, 1)) + '</td>' +
        '<td><span class="pill ' + venue + '">' + esc(p.platform || venue) + '</span></td>' +
        '</tr>';
    }).join("");

    $("positions").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Time</th><th>Match</th><th>Selection</th>' +
          '<th class="r">Odds</th><th class="r">Stake</th>' +
          '<th class="r">Model</th><th class="r">EV</th><th>Venue</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  function renderPredictions(d) {
    var preds = d.predictions || [];
    if (!preds.length) {
      $("predictions").innerHTML = '<div class="empty">No fixture predictions</div>';
      $("predictions-meta").textContent = "0";
      return;
    }
    $("predictions-meta").textContent =
      preds.length + " fixture" + (preds.length === 1 ? "" : "s");

    var cards = preds.map(function (fx) {
      var scores = (fx.scores || []).slice(0, 4);
      var maxProb = scores.reduce(function (m, s) {
        return Math.max(m, Number(s.prob || 0));
      }, 0);

      var scoreHtml = scores.map(function (s) {
        var p = Number(s.prob || 0);
        var frac = maxProb > 0 ? (p / maxProb) : 0;
        // Inline background: the stylesheet fill colour is too close to the
        // track on dark displays, which made every bar look identical.
        return '<div class="score-row">' +
          '<span class="score-label">' + esc(s.score) + '</span>' +
          '<span class="score-bar"><span class="score-fill" style="width:' +
            (frac * 100).toFixed(1) + '%;background:#4ade80;opacity:.85"></span></span>' +
          '<span class="score-prob">' + p.toFixed(1) + '%</span>' +
        '</div>';
      }).join("");

      var foot = [];
      if (fx.over_under) {
        var ou = fx.over_under;
        // pct1 tolerates null/undefined/NaN so a slim O/U line (line only, no
        // over/under) degrades to an em-dash instead of throwing on .toFixed.
        foot.push('<span><b>O/U ' + esc(num(ou.line, 1)) + '</b> ' +
          esc(pct1(ou.over)) + ' / ' + esc(pct1(ou.under)) + '</span>');
      }
      if (fx.btts !== null && fx.btts !== undefined && !isNaN(fx.btts)) {
        foot.push('<span><b>BTTS</b> ' + esc(pct1(fx.btts)) + '</span>');
      }

      return '<div class="pred-card">' +
        '<div class="pred-title">' + esc(fx.fixture) + '</div>' +
        (scoreHtml || '<div class="empty">no scores</div>') +
        (foot.length ? '<div class="pred-foot">' + foot.join("") + '</div>' : '') +
      '</div>';
    }).join("");

    $("predictions").innerHTML = '<div class="pred-grid">' + cards + '</div>';
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
    renderTicker(d);
    renderVenues(d);
    renderPositions(d);
    renderPredictions(d);
    renderFooter(d);
  }

  // ---- optional Polymarket enrichment (graceful, never required) ---------
  // Opportunistically fetch live prices for any open polymarket positions and
  // annotate their venue pill title with the latest price. Any failure (CORS,
  // offline, blocked) is swallowed — the page already rendered without it.

  function enrichPolymarket(d) {
    try {
      var hasPoly = (d.positions || []).some(function (p) {
        return (p.venue || "").toLowerCase() === "polymarket";
      });
      if (!hasPoly || typeof fetch !== "function") return;
      var url = "https://gamma-api.polymarket.com/markets?closed=false&limit=20";
      fetch(url, { mode: "cors" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (markets) {
          if (!markets || !markets.length) return;
          // Lightweight annotation only; do not disturb layout if shape
          // differs. We just tag the panel meta to show the feed is live.
          var meta = $("positions-meta");
          if (meta) { meta.textContent = meta.textContent + " · live"; }
        })
        .catch(function () { /* blocked/offline — ignore silently */ });
    } catch (e) { /* never let enrichment break the page */ }
  }

  // ---- boot ---------------------------------------------------------------

  function load() {
    var url = "./data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        render(d);
        enrichPolymarket(d);
      })
      .catch(function (err) {
        showNoData("DATA FEED UNAVAILABLE");
        // Still render an all-zero shell so the terminal is never blank.
        render({ totals: {}, venues: {}, clv: {}, positions: [], predictions: [], meta: {} });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
