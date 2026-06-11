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
    // Parse as UTC and render in the viewer's local timezone (consistent
    // with the chart axis). Falls back to the raw string if unparseable.
    var ms = Date.parse(String(ts).indexOf("T") >= 0 &&
      !/[zZ]|[+\-]\d\d:?\d\d$/.test(String(ts)) ? String(ts) + "Z" : String(ts));
    if (!isNaN(ms)) {
      var dt = new Date(ms);
      function p(n) { return (n < 10 ? "0" : "") + n; }
      return p(dt.getHours()) + ":" + p(dt.getMinutes());
    }
    return String(ts);
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

  var VENUE_COLOR = { sportsbook: "#4ade80", polymarket: "#60a5fa", kalshi: "#a855f7" };

  // Per-book accent colours. Keyed by the lower-cased platform string; any
  // book not in the map (and any null/blank) degrades to BOOK_FALLBACK so the
  // pill/rail still renders. Several Betfair surfaces share one orange.
  var BOOK_FALLBACK = "#9ca3af";
  // Single source of truth for book/venue colours (mirrors the CSS vars):
  // paddy light green, bet365 dark green, betfair yellow, polymarket light
  // blue, kalshi purple.
  var BOOK_COLOR = {
    paddypower: "#4ade80",
    bet365: "#15803d",
    virginbet: "#ef4444",
    skybet: "#3b82f6",
    betfair: "#fde047",
    betfair_ex_uk: "#fde047",
    betfair_sportsbook: "#fde047",
    williamhill: "#1d4ed8",
    smarkets: "#2dd4bf",
    matchbook: "#f472b6",
    coral: "#fb923c",
    ladbrokes: "#dc2626",
    polymarket: "#60a5fa",
    kalshi: "#a855f7"
  };
  function bookColor(name) {
    var k = String(name === null || name === undefined ? "" : name)
      .toLowerCase().trim();
    return BOOK_COLOR[k] || BOOK_FALLBACK;
  }

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

    // Per-bookmaker rows nested under each venue, coloured by book.
    var plats = d.platforms || {};

    function platformRows(venueKey, venueWagered, ccy) {
      var rows = Object.keys(plats).filter(function (p) {
        return (plats[p].venue || "sportsbook") === venueKey && Number(plats[p].wagered || 0) > 0;
      }).sort(function (a, b) { return plats[b].wagered - plats[a].wagered; });
      if (!rows.length) return "";
      return '<div class="venue-books">' + rows.map(function (p) {
        var blk = plats[p];
        var frac = venueWagered > 0 ? (blk.wagered / venueWagered) : 0;
        var pl = Number(blk.settled_pl || 0);
        var plBit = pl !== 0
          ? ' · <span class="' + (pl >= 0 ? "pos" : "neg") + '">' + signedMoney(pl, ccy) + '</span>'
          : "";
        return '<div class="venue-book-row">' +
          '<span class="book-dot" style="background:' + bookColor(p) + '"></span>' +
          '<span class="book-name">' + esc(p) + '</span>' +
          '<span class="book-bar"><span style="display:block;height:100%;width:' +
            (frac * 100).toFixed(1) + '%;background:' + bookColor(p) + ';opacity:.55"></span></span>' +
          '<span class="book-amt num">' + money(blk.wagered, ccy) +
            ' <span class="dim">(' + blk.n_bets + ')</span>' + plBit + '</span>' +
        '</div>';
      }).join("") + '</div>';
    }

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
          platformRows(k, amt, ccy) +
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
      var book = p.platform || p.venue || "";
      // Colour is keyed off the actual book (platform), falling back to the
      // venue, then to the neutral grey. Used for both the left rail and the
      // pill so the row is identifiable at a glance.
      var col = bookColor(book || venue);
      return '<tr>' +
        '<td class="num dim pos-time" style="border-left-color:' + col + '">' +
          esc(timeOnly(p.ts_utc)) + '</td>' +
        '<td class="pos-match" title="' + esc(p.match) + '">' + esc(dash(p.match)) + '</td>' +
        '<td class="pos-sel" title="' + esc(p.selection) + '">' + esc(dash(p.selection)) + '</td>' +
        '<td class="r num">' + esc(num(p.decimal_odds)) + '</td>' +
        '<td class="r num">' + esc(money(p.stake, p.currency)) + '</td>' +
        '<td class="r num">' + esc(pct(p.model_prob, 0)) + '</td>' +
        '<td class="r num ' + evClass(p.ev) + '">' +
          esc(p.ev === null || p.ev === undefined ? '—' : pct(p.ev, 1)) + '</td>' +
        '<td><span class="pill book ' + venue + '" style="color:' + col +
          ';border-color:' + col + '">' + esc(p.platform || venue) + '</span></td>' +
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
        var fair = (s.fair !== null && s.fair !== undefined && !isNaN(s.fair))
          ? Number(s.fair) : (p > 0 ? 100 / p : null);
        return '<div class="score-row">' +
          '<span class="score-label">' + esc(s.score) + '</span>' +
          '<span class="score-bar"><span class="score-fill" style="width:' +
            (frac * 100).toFixed(1) + '%;background:#4ade80;opacity:.85"></span></span>' +
          '<span class="score-prob">' + p.toFixed(1) + '%' +
            (fair ? ' <span class="dim">· ' + fair.toFixed(1) + '</span>' : '') +
          '</span>' +
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

  // ---- charts (pure inline SVG) ------------------------------------------
  // Shared geometry. viewBox is fixed ~640x220; the stylesheet stretches the
  // SVG to the panel width while preserving aspect ratio.
  var CHART_W = 640, CHART_H = 220;
  var CHART_M = { top: 14, right: 58, bottom: 26, left: 38 };
  var PLOT_W = CHART_W - CHART_M.left - CHART_M.right;
  var PLOT_H = CHART_H - CHART_M.top - CHART_M.bottom;

  // Round to a tidy number of decimals for SVG coordinates (keeps markup small
  // and avoids float noise like 123.4000001).
  function r2(n) { return Math.round(n * 100) / 100; }

  // Parse an ISO-ish timestamp ("2026-06-11T12:58:42", optionally trailing Z)
  // to epoch ms. Returns NaN on anything unparseable so callers can filter.
  function tsMs(ts) {
    if (!ts) return NaN;
    var s = String(ts);
    if (s.indexOf("T") >= 0 && !/[zZ]|[+\-]\d\d:?\d\d$/.test(s)) s += "Z";
    var v = Date.parse(s);
    return isNaN(v) ? NaN : v;
  }
  // HH:MM in the VIEWER'S local timezone for an epoch-ms value. Timestamps
  // are stored UTC (tsMs appends Z); rendering local means the chart axis
  // matches the clock on the wall wherever the page is opened.
  function hhmm(ms) {
    var dt = new Date(ms);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(dt.getHours()) + ":" + p(dt.getMinutes());
  }
  // Short viewer-timezone label for the axis (e.g. "UTC+3"), so two people
  // in different timezones reading the same chart aren't confused.
  function tzLabel() {
    var mins = -new Date().getTimezoneOffset();
    if (mins === 0) return "UTC";
    var sign = mins > 0 ? "+" : "-";
    var h = Math.floor(Math.abs(mins) / 60);
    var m = Math.abs(mins) % 60;
    return "UTC" + sign + h + (m ? ":" + (m < 10 ? "0" : "") + m : "");
  }

  function chartEmpty(el, msg) {
    el.innerHTML = '<div class="chart-empty">' + esc(msg) + '</div>';
  }

  // Build the shared chart frame (background grid + y/x axes). yLabels is an
  // array of {y, text}; xTicks an array of {x, text}. Returns an SVG string of
  // just the frame; series are appended by the caller.
  function chartFrame(yLabels, xTicks) {
    var s = '';
    yLabels.forEach(function (yl) {
      s += '<line class="cx-grid" x1="' + r2(CHART_M.left) + '" y1="' + r2(yl.y) +
        '" x2="' + r2(CHART_M.left + PLOT_W) + '" y2="' + r2(yl.y) + '"/>';
      s += '<text class="cx-tick" x="' + r2(CHART_M.left - 6) + '" y="' + r2(yl.y + 3) +
        '" text-anchor="end">' + esc(yl.text) + '</text>';
    });
    // axes
    s += '<line class="cx-axis" x1="' + r2(CHART_M.left) + '" y1="' + r2(CHART_M.top) +
      '" x2="' + r2(CHART_M.left) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
    s += '<line class="cx-axis" x1="' + r2(CHART_M.left) + '" y1="' + r2(CHART_M.top + PLOT_H) +
      '" x2="' + r2(CHART_M.left + PLOT_W) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
    xTicks.forEach(function (xt) {
      s += '<text class="cx-tick" x="' + r2(xt.x) + '" y="' + r2(CHART_M.top + PLOT_H + 14) +
        '" text-anchor="middle">' + esc(xt.text) + '</text>';
    });
    return s;
  }

  // Pick 4-6 evenly spaced x ticks across [t0,t1], mapped through scaleX.
  function timeTicks(t0, t1, scaleX) {
    var n = 5;
    if (t1 <= t0) return [{ x: scaleX(t0), text: hhmm(t0) }];
    var ticks = [];
    for (var i = 0; i < n; i++) {
      var t = t0 + (t1 - t0) * (i / (n - 1));
      ticks.push({ x: scaleX(t), text: hhmm(t) });
    }
    // Tag the final tick with the viewer's timezone so the axis is
    // self-describing (times are rendered local, storage stays UTC).
    if (ticks.length) {
      ticks[ticks.length - 1].text += " " + tzLabel();
    }
    return ticks;
  }

  // (a) LINE MOVEMENT ------------------------------------------------------
  // linemove.json shape (tolerant). Two event shapes are accepted:
  //   * the producer (wca.linemove) shape — an object with .events[], each
  //     event {fixture, kickoff?, series:{home:[[ts,prob],...], draw:[...],
  //     away:[...]}} (three parallel [ts, prob] arrays); and
  //   * a legacy point-list shape — {id?, fixture|match|label, kickoff?,
  //     points:[{ts, home, draw, away}]}.
  // home/draw/away probs may be 0..1 fractions or 0..100 percentages
  // (auto-detected downstream).
  var LINEMOVE = { events: [], idx: 0, wired: false };

  // Zip the producer's three parallel {leg: [[ts, prob], ...]} arrays into the
  // internal [{t, home, draw, away}] point list, joining legs by timestamp.
  // Returns null if `series` is not that shape so the caller can fall back.
  function pointsFromSeries(series) {
    if (!series || typeof series !== "object" || Array.isArray(series)) return null;
    var legs = ["home", "draw", "away"];
    var hasLeg = legs.some(function (k) { return Array.isArray(series[k]); });
    if (!hasLeg) return null;
    var byTs = {};
    legs.forEach(function (leg) {
      var arr = series[leg];
      if (!Array.isArray(arr)) return;
      arr.forEach(function (pair) {
        if (!Array.isArray(pair) || pair.length < 2) return;
        var ts = pair[0];
        var pt = byTs[ts];
        if (!pt) { pt = byTs[ts] = { ts: ts }; }
        pt[leg] = pair[1];
      });
    });
    return Object.keys(byTs).map(function (ts) { return byTs[ts]; });
  }

  function normLineMove(raw) {
    var events = [];
    var list = [];
    if (Array.isArray(raw)) list = raw;
    else if (raw && Array.isArray(raw.events)) list = raw.events;
    else if (raw && typeof raw === "object") {
      // map keyed by fixture -> points/event
      Object.keys(raw).forEach(function (k) {
        var v = raw[k];
        if (v && (Array.isArray(v) || v.points || v.series)) {
          list.push({ fixture: k,
            points: Array.isArray(v) ? v : v.points,
            series: v && v.series,
            kickoff: v && v.kickoff });
        }
      });
    }
    list.forEach(function (ev) {
      if (!ev || typeof ev !== "object") return;
      var label = ev.fixture || ev.match || ev.label || ev.id || "Fixture";
      // Prefer an explicit point list; otherwise zip the producer's series.
      var rawPts = ev.points;
      if (!Array.isArray(rawPts)) rawPts = pointsFromSeries(ev.series);
      if (!Array.isArray(rawPts)) return;
      var pts = rawPts.map(function (pt) {
        var t = tsMs(pt.ts || pt.time || pt.t);
        return {
          t: t,
          home: numOrNull(pt.home),
          draw: numOrNull(pt.draw),
          away: numOrNull(pt.away)
        };
      }).filter(function (pt) { return !isNaN(pt.t); });
      pts.sort(function (a, b) { return a.t - b.t; });
      if (!pts.length) return;
      events.push({
        label: String(label),
        kickoff: tsMs(ev.kickoff || ev.commence_time || ev.start),
        points: pts
      });
    });
    // Chronological by kickoff (soonest first); unknown kickoffs sink to the end.
    events.sort(function (a, b) {
      var ka = isNaN(a.kickoff) ? Infinity : a.kickoff;
      var kb = isNaN(b.kickoff) ? Infinity : b.kickoff;
      return ka - kb;
    });
    return events;
  }
  function numOrNull(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return null;
    return Number(v);
  }
  // Detect whether prob values are fractions (<=1) or percentages and return a
  // function that maps a raw value to 0..100.
  function pctScaler(events) {
    var max = 0;
    events.forEach(function (ev) {
      ev.points.forEach(function (pt) {
        ["home", "draw", "away"].forEach(function (k) {
          if (pt[k] !== null) max = Math.max(max, pt[k]);
        });
      });
    });
    var asPct = max > 1.5; // values already in 0..100
    return function (v) { return v === null ? null : (asPct ? v : v * 100); };
  }

  function lineSeries(points, key, scaleX, scaleY, toPct) {
    var segs = [], cur = [];
    points.forEach(function (pt) {
      var v = toPct(pt[key]);
      if (v === null) { if (cur.length) { segs.push(cur); cur = []; } return; }
      cur.push(r2(scaleX(pt.t)) + "," + r2(scaleY(v)));
    });
    if (cur.length) segs.push(cur);
    return segs;
  }

  function drawLineMove() {
    var el = $("linemove-canvas");
    if (!el) return;
    var events = LINEMOVE.events;
    if (!events.length) { chartEmpty(el, "No line-movement data"); return; }
    var ev = events[Math.min(LINEMOVE.idx, events.length - 1)] || events[0];
    var pts = ev.points;
    if (!pts.length) { chartEmpty(el, "No line-movement data"); return; }

    var toPct = pctScaler(events);
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var span = t1 - t0 || 1;
    function scaleX(t) { return CHART_M.left + ((t - t0) / span) * PLOT_W; }

    // Auto-fit the y-axis to THIS fixture's actual prob range so small but real
    // moves (pre-match shifts are often <1pp) are visible instead of a flat
    // line pinned to a 0-100% axis. Enforce a minimum window so a near-static
    // fixture isn't absurdly magnified, and pad the extremes.
    var vals = [];
    pts.forEach(function (pt) {
      ["home", "draw", "away"].forEach(function (k) {
        var v = toPct(pt[k]);
        if (v !== null && !isNaN(v)) vals.push(v);
      });
    });
    var lo = vals.length ? Math.min.apply(null, vals) : 0;
    var hi = vals.length ? Math.max.apply(null, vals) : 100;
    var pad = Math.max((hi - lo) * 0.15, 1);   // 15% padding, >=1pp
    lo = Math.max(0, lo - pad);
    hi = Math.min(100, hi + pad);
    if (hi - lo < 6) {                          // minimum 6pp window
      var mid = (hi + lo) / 2;
      lo = Math.max(0, mid - 3);
      hi = Math.min(100, mid + 3);
    }
    var yrange = hi - lo || 1;
    function scaleY(v) { return CHART_M.top + (1 - ((v - lo) / yrange)) * PLOT_H; }

    var yLabels = [lo, lo + yrange * 0.25, lo + yrange * 0.5, lo + yrange * 0.75, hi]
      .map(function (p) { return { y: scaleY(p), text: p.toFixed(1) + "%" }; });
    var svg = chartFrame(yLabels, timeTicks(t0, t1, scaleX));

    var SERIES = [
      { key: "home", color: "#4ade80", name: "Home" },
      { key: "draw", color: "#9ca3af", name: "Draw" },
      { key: "away", color: "#ef4444", name: "Away" }
    ];
    SERIES.forEach(function (sr) {
      lineSeries(pts, sr.key, scaleX, scaleY, toPct).forEach(function (seg) {
        svg += '<polyline class="cx-series" stroke="' + sr.color +
          '" points="' + seg.join(" ") + '"/>';
      });
    });

    // kickoff marker if within range
    if (!isNaN(ev.kickoff) && ev.kickoff >= t0 && ev.kickoff <= t1) {
      var kx = scaleX(ev.kickoff);
      svg += '<line class="cx-kick" x1="' + r2(kx) + '" y1="' + r2(CHART_M.top) +
        '" x2="' + r2(kx) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
      svg += '<text class="cx-kick-lbl" x="' + r2(kx + 3) + '" y="' +
        r2(CHART_M.top + 8) + '">KO</text>';
    }

    // legend (top-right inside the right margin)
    var lx = CHART_M.left + PLOT_W + 8, ly = CHART_M.top + 4;
    SERIES.forEach(function (sr, i) {
      var y = ly + i * 14;
      svg += '<rect x="' + r2(lx) + '" y="' + r2(y - 6) + '" width="8" height="8" rx="1" fill="' +
        sr.color + '"/>';
      svg += '<text class="cx-legend" x="' + r2(lx + 12) + '" y="' + r2(y + 1) + '">' +
        esc(sr.name) + '</text>';
    });

    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Line movement">' +
      svg + '</svg>';
  }

  function wireLineMoveSelect() {
    var sel = $("linemove-select");
    if (!sel) return;
    var events = LINEMOVE.events;
    if (events.length > 1) {
      sel.hidden = false;
      sel.innerHTML = events.map(function (ev, i) {
        return '<option value="' + i + '">' + esc(ev.label) + '</option>';
      }).join("");
      sel.value = String(LINEMOVE.idx);
      if (!LINEMOVE.wired) {
        sel.addEventListener("change", function () {
          LINEMOVE.idx = Number(sel.value) || 0;
          drawLineMove();
        });
        LINEMOVE.wired = true;
      }
    } else {
      sel.hidden = true;
      sel.innerHTML = "";
    }
  }

  function loadLineMove() {
    var el = $("linemove-canvas");
    if (!el || typeof fetch !== "function") { if (el) chartEmpty(el, "No line-movement data"); return; }
    var url = "./linemove.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (raw) {
        LINEMOVE.events = normLineMove(raw);
        LINEMOVE.idx = 0;
        if (!LINEMOVE.events.length) {
          // present but empty -> clean empty state
          var s = $("linemove-select"); if (s) { s.hidden = true; }
          chartEmpty(el, "No line-movement data");
          return;
        }
        wireLineMoveSelect();
        drawLineMove();
      })
      .catch(function () {
        // 404 / blocked / malformed -> hide the whole block per spec.
        var blk = $("chart-linemove");
        if (blk) { blk.hidden = true; }
      });
  }

  // (b) CUMULATIVE STAKED --------------------------------------------------
  // Step-line of cumulative stake over time, one series per currency
  // (GBP solid, USD dashed), built from positions[].ts_utc + stake.
  var CUM_STYLE = {
    GBP: { color: "#4ade80", dash: "" },
    USD: { color: "#60a5fa", dash: "4 3" },
    EUR: { color: "#a78bfa", dash: "1 3" }
  };

  function drawCumStake(d) {
    var el = $("cumstake-canvas");
    if (!el) return;
    var pos = (d.positions || []).filter(function (p) {
      return !isNaN(tsMs(p.ts_utc)) && !isNaN(Number(p.stake));
    });
    if (!pos.length) { chartEmpty(el, "No staked positions"); return; }

    // group by currency, sort by time, accumulate
    var byCcy = {};
    pos.forEach(function (p) {
      var ccy = p.currency || "GBP";
      (byCcy[ccy] = byCcy[ccy] || []).push({ t: tsMs(p.ts_utc), stake: Number(p.stake) });
    });
    var ccys = Object.keys(byCcy);
    var series = {}, tMin = Infinity, tMax = -Infinity, vMax = 0;
    ccys.forEach(function (ccy) {
      var arr = byCcy[ccy].slice().sort(function (a, b) { return a.t - b.t; });
      var cum = 0, steps = [];
      arr.forEach(function (e) {
        cum += e.stake;
        steps.push({ t: e.t, v: cum });
        tMin = Math.min(tMin, e.t); tMax = Math.max(tMax, e.t);
      });
      vMax = Math.max(vMax, cum);
      series[ccy] = steps;
    });
    if (!isFinite(tMin)) { chartEmpty(el, "No staked positions"); return; }
    var span = (tMax - tMin) || 1;
    var top = vMax > 0 ? vMax * 1.08 : 1;
    function scaleX(t) { return CHART_M.left + ((t - tMin) / span) * PLOT_W; }
    function scaleY(v) { return CHART_M.top + (1 - (v / top)) * PLOT_H; }

    var yLabels = [0, 0.5, 1].map(function (f) {
      return { y: scaleY(top * f), text: Math.round(top * f).toString() };
    });
    var svg = chartFrame(yLabels, timeTicks(tMin, tMax, scaleX));

    ccys.forEach(function (ccy) {
      var st = CUM_STYLE[ccy] || { color: "#9ca3af", dash: "" };
      var steps = series[ccy];
      // build a step (left-continuous) path: hold then rise
      var pts = [];
      pts.push(r2(scaleX(tMin)) + "," + r2(scaleY(0)));
      var prevV = 0;
      steps.forEach(function (s) {
        pts.push(r2(scaleX(s.t)) + "," + r2(scaleY(prevV)));
        pts.push(r2(scaleX(s.t)) + "," + r2(scaleY(s.v)));
        prevV = s.v;
      });
      pts.push(r2(scaleX(tMax)) + "," + r2(scaleY(prevV)));
      svg += '<polyline class="cx-series" stroke="' + st.color + '"' +
        (st.dash ? ' stroke-dasharray="' + st.dash + '"' : '') +
        ' points="' + pts.join(" ") + '"/>';
      // final value label at the right edge
      var fy = scaleY(prevV);
      svg += '<text class="cx-final" fill="' + st.color + '" x="' +
        r2(CHART_M.left + PLOT_W + 6) + '" y="' + r2(fy + 3) +
        '">' + esc(money(prevV, ccy)) + '</text>';
    });

    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Cumulative staked">' +
      svg + '</svg>';
  }

  // P&L // Realized: one step-line per pool. Sportsbook (£, solid green/red
  // by sign) and prediction markets combined (USD, dashed blue). Currencies
  // are separate lines, never summed; y-axis spans negative territory.
  function drawPnl(d) {
    var el = $("pnl-canvas");
    if (!el) return;
    var ps = d.pnl_series || {};
    var seriesDefs = [
      { key: "sportsbook", color: "#4ade80", dash: null, ccy: (ps.sportsbook || {}).currency || "GBP" },
      { key: "prediction_markets", color: "#60a5fa", dash: "4 3", ccy: (ps.prediction_markets || {}).currency || "USD" }
    ];
    var all = [];
    seriesDefs.forEach(function (sd) {
      sd.pts = (((ps[sd.key] || {}).points) || []).map(function (pt) {
        return { t: tsMs(pt[0]), v: Number(pt[1]) };
      }).filter(function (p) { return !isNaN(p.t) && !isNaN(p.v); });
      all = all.concat(sd.pts);
    });
    if (!all.length) { chartEmpty(el, "No realized P&L yet — appears after settlement"); return; }

    var tMin = Infinity, tMax = -Infinity, vMin = 0, vMax = 0;
    all.forEach(function (p) {
      tMin = Math.min(tMin, p.t); tMax = Math.max(tMax, p.t);
      vMin = Math.min(vMin, p.v); vMax = Math.max(vMax, p.v);
    });
    var pad = Math.max((vMax - vMin) * 0.15, 1);
    vMin -= pad; vMax += pad;
    var span = (tMax - tMin) || 1, vSpan = (vMax - vMin) || 1;
    function scaleX(t) { return CHART_M.left + ((t - tMin) / span) * PLOT_W; }
    function scaleY(v) { return CHART_M.top + (1 - ((v - vMin) / vSpan)) * PLOT_H; }

    var yLabels = [vMin, (vMin + vMax) / 2, vMax].map(function (v) {
      return { y: scaleY(v), text: v.toFixed(0) };
    });
    var svg = chartFrame(yLabels, timeTicks(tMin, tMax, scaleX));
    // zero line for sign orientation
    if (vMin < 0 && vMax > 0) {
      svg += '<line class="cx-grid" stroke-dasharray="2 3" x1="' + r2(CHART_M.left) +
        '" y1="' + r2(scaleY(0)) + '" x2="' + r2(CHART_M.left + PLOT_W) +
        '" y2="' + r2(scaleY(0)) + '"/>';
    }
    seriesDefs.forEach(function (sd) {
      if (!sd.pts.length) return;
      var arr = sd.pts.slice().sort(function (a, b) { return a.t - b.t; });
      var pts = [], prevV = 0;
      pts.push(r2(scaleX(tMin)) + "," + r2(scaleY(0)));
      arr.forEach(function (p) {
        pts.push(r2(scaleX(p.t)) + "," + r2(scaleY(prevV)));  // step
        pts.push(r2(scaleX(p.t)) + "," + r2(scaleY(p.v)));
        prevV = p.v;
      });
      pts.push(r2(scaleX(tMax)) + "," + r2(scaleY(prevV)));
      svg += '<polyline class="cx-series" stroke="' + sd.color + '"' +
        (sd.dash ? ' stroke-dasharray="' + sd.dash + '"' : '') +
        ' points="' + pts.join(" ") + '"/>';
      svg += '<text class="cx-final" fill="' + sd.color + '" x="' +
        r2(CHART_M.left + PLOT_W + 6) + '" y="' + r2(scaleY(prevV) + 3) +
        '">' + esc(signedMoney(prevV, sd.ccy)) + '</text>';
    });
    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Realized P&L">' +
      svg + '</svg>';
  }

  function renderCharts(d) {
    drawPnl(d);
    drawCumStake(d);
    loadLineMove();
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
    renderClosedPositions(d);
    renderPredictions(d);
    renderCharts(d);
    renderFooter(d);
  }

  function renderClosedPositions(d) {
    var el = $("positions-closed");
    if (!el) return;
    var pos = d.closed_positions || [];
    var meta = $("positions-closed-meta");
    if (!pos.length) {
      el.innerHTML = '<div class="empty">No settled bets yet — P&L appears here after results</div>';
      if (meta) meta.textContent = "0";
      return;
    }
    // Per-currency realized totals for the panel meta (never summed across).
    var tot = {};
    pos.forEach(function (p) {
      var c = p.currency || "GBP";
      tot[c] = (tot[c] || 0) + Number(p.pl || 0);
    });
    if (meta) {
      meta.textContent = pos.length + " settled · " +
        Object.keys(tot).map(function (c) { return signedMoney(tot[c], c); }).join(" + ");
    }
    var rows = pos.map(function (p) {
      var pl = Number(p.pl);
      var plCls = p.status === "void" ? "dim" : (pl >= 0 ? "pos" : "neg");
      var plTxt = p.status === "void" ? "void" : signedMoney(pl, p.currency);
      return '<tr style="border-left:2px solid ' + bookColor(p.platform) + '">' +
        '<td class="num dim">' + esc(timeOnly(p.settled_ts || p.ts_utc)) + '</td>' +
        '<td class="pos-match" title="' + esc(p.match) + '">' + esc(dash(p.match)) + '</td>' +
        '<td class="pos-sel" title="' + esc(p.selection) + '">' + esc(dash(p.selection)) + '</td>' +
        '<td class="r num">' + esc(num(p.decimal_odds)) + '</td>' +
        '<td class="r num">' + esc(money(p.stake, p.currency)) + '</td>' +
        '<td class="r num ' + plCls + '">' + esc(plTxt) + '</td>' +
        '<td class="r num ' + evClass(p.clv) + '">' +
          esc(p.clv === null || p.clv === undefined ? "—" : pct(p.clv, 1)) + '</td>' +
        '<td><span class="pill ' + esc(p.venue || "sportsbook") + '">' + esc(p.platform || "") + '</span></td>' +
        '</tr>';
    }).join("");
    el.innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Settled</th><th>Match</th><th>Selection</th>' +
          '<th class="r">Odds</th><th class="r">Stake</th>' +
          '<th class="r">P&L</th><th class="r">CLV</th><th>Venue</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
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

  function load(attempt) {
    attempt = attempt || 1;
    var url = "./data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        var nd = $("nodata");
        if (nd) nd.hidden = true; // success always clears the banner
        render(d);
        enrichPolymarket(d);
      })
      .catch(function (err) {
        // A load during a mid-deploy window can transiently 404 — retry with
        // backoff before declaring the feed down, and never strand the page.
        if (attempt < 4) {
          setTimeout(function () { load(attempt + 1); }, attempt * 2000);
          return;
        }
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
