/* World Cup Alpha — prediction tracking page.
 *
 * Loads ./tracking_data.json (cache-busted) and renders:
 *   0. headline stat tiles (picks correct, Brier model vs market, P/L, CLV);
 *   1. two bars per completed fixture: the FINAL (FT) scoreline outcome
 *      (home/away goal split) and a diverging "result correctness" bar that
 *      runs right (green) when the model beat the de-vigged market on the
 *      realised outcome and left (red) when it didn't;
 *   2. a per-fixture prediction scoreboard (picks, Brier, scorelines, O/U,
 *      BTTS — tick/cross per leg);
 *   3. a calibration scatter (predicted prob vs 0/1 outcome, jittered);
 *   4. CLV vs P/L scatter for settled bets with a closing line captured.
 *
 * Charts are inline SVG built as strings — no chart libraries, no CDN.
 * Everything degrades to a clean "no data" state.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // 0..1 probability -> "57.4%".
  function prob01(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function num(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(dp === undefined ? 2 : dp);
  }
  function signed(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v);
    return (n >= 0 ? "+" : "") + n.toFixed(dp === undefined ? 2 : dp);
  }
  function signedPct(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v) * 100;
    return (n >= 0 ? "+" : "") + n.toFixed(dp === undefined ? 2 : dp) + "%";
  }
  function tick(ok) {
    if (ok === null || ok === undefined) return '<span class="dim">—</span>';
    return ok
      ? '<span class="tr-hit">&#10003;</span>'
      : '<span class="tr-miss">&#10007;</span>';
  }
  function legLabel(leg) {
    return leg === "home" ? "HOME" : leg === "away" ? "AWAY" : leg === "draw" ? "DRAW" : "—";
  }
  // "Home vs Away" -> ["Home", "Away"], else null.
  function split(fixture) {
    var parts = String(fixture || "").split(/\s+vs\s+/i);
    return parts.length === 2
      ? [parts[0].trim(), parts[1].trim()]
      : null;
  }
  // "Home vs Away" + leg -> the team (or DRAW) the leg refers to.
  function legTeam(fixture, leg) {
    var parts = String(fixture || "").split(/\s+vs\s+/i);
    if (leg === "home" && parts.length === 2) return parts[0];
    if (leg === "away" && parts.length === 2) return parts[1];
    if (leg === "draw") return "Draw";
    return legLabel(leg);
  }
  function dateOnly(ts) {
    var t = String(ts || "");
    return t.length >= 10 ? t.slice(0, 10) : t;
  }

  // ---- 0. headline stats --------------------------------------------------

  function statTile(label, valueHtml, subHtml) {
    return '<div class="track-stat">' +
      '<span class="tick-label">' + label + '</span>' +
      '<span class="tick-value">' + valueHtml + '</span>' +
      (subHtml ? '<span class="track-stat-sub">' + subHtml + '</span>' : '') +
    '</div>';
  }

  function renderStats(d) {
    var s = d.summary || {};
    var bets = s.bets || {};
    var n = s.fixtures_complete || 0;

    var brierHtml = "—", brierSub = "";
    if (s.model_brier !== null && s.model_brier !== undefined) {
      var modelBetter = s.market_brier !== null && s.model_brier <= s.market_brier;
      brierHtml = '<span class="' + (modelBetter ? "pos" : "neg") + '">' +
        num(s.model_brier, 3) + '</span>' +
        '<span class="dim"> vs ' + num(s.market_brier, 3) + '</span>';
      brierSub = "model vs market &middot; lower is better";
    }

    var plClass = (bets.pl || 0) >= 0 ? "pos" : "neg";
    var clvClass = (bets.avg_clv || 0) >= 0 ? "pos" : "neg";

    $("stats").innerHTML =
      statTile("Fixtures Complete", String(n), "") +
      statTile("Model Picks", esc((s.model_1x2_correct || 0) + "/" + n), "modal 1X2 pick correct") +
      statTile("Market Picks", esc((s.market_1x2_correct || 0) + "/" + n), "closing favourite correct") +
      statTile("Brier 1X2", brierHtml, brierSub) +
      statTile("Bet P/L",
        '<span class="' + plClass + '">' + esc(signed(bets.pl)) + '</span>',
        (bets.settled || 0) + " settled &middot; " + (bets.won || 0) + "W " + (bets.lost || 0) + "L &middot; all pools") +
      statTile("Avg CLV",
        '<span class="' + clvClass + '">' + esc(signedPct(bets.avg_clv)) + '</span>',
        (bets.clv_count || 0) + " bets with captured close");

    $("stats-meta").textContent = n
      ? n + " fixture" + (n === 1 ? "" : "s") + " scored"
      : "awaiting first results";
  }

  // ---- 1. FT scoreline + result-correctness (model vs market) -------------

  // Bar A: the FINAL (FT) scoreline as a home-vs-away goal split. The track is
  // divided proportionally to goals scored; the winning side (or both, on a
  // draw) is tinted by the realised outcome so the picture reads at a glance.
  function scorelineBar(f) {
    var hg = f.home_goals, ag = f.away_goals;
    var parts = split(f.fixture);
    var homeName = parts ? parts[0] : "Home";
    var awayName = parts ? parts[1] : "Away";
    if (hg === null || hg === undefined || ag === null || ag === undefined) {
      return '<div class="tr-bar-row">' +
        '<span class="tr-bar-lbl">FT</span>' +
        '<span class="tr-bar-track"></span>' +
        '<span class="tr-bar-val num">' + esc(f.score || "—") + '</span>' +
      '</div>';
    }
    hg = Number(hg); ag = Number(ag);
    var total = hg + ag;
    // 0-0 has no goals to split — show a neutral, full-width draw band.
    var homePct = total > 0 ? (hg / total) * 100 : 50;
    var awayPct = total > 0 ? (ag / total) * 100 : 50;
    var homeCls = f.outcome === "home" ? "tr-ft-home-win" : "tr-ft-home";
    var awayCls = f.outcome === "away" ? "tr-ft-away-win" : "tr-ft-away";
    if (f.outcome === "draw") { homeCls = "tr-ft-draw"; awayCls = "tr-ft-draw"; }
    var seg =
      '<span class="tr-ft-seg ' + homeCls + '" style="width:' + homePct.toFixed(1) +
        '%">' + (hg > 0 ? esc(String(hg)) : "") + '</span>' +
      '<span class="tr-ft-seg ' + awayCls + '" style="width:' + awayPct.toFixed(1) +
        '%">' + (ag > 0 ? esc(String(ag)) : "") + '</span>';
    return '<div class="tr-bar-row">' +
      '<span class="tr-bar-lbl">FT</span>' +
      '<span class="tr-bar-track tr-ft-track">' + seg + '</span>' +
      '<span class="tr-bar-val num" title="' + esc(homeName) + ' ' + hg + ' – ' +
        ag + ' ' + esc(awayName) + '">' + esc(f.score || (hg + "-" + ag)) +
      '</span>' +
    '</div>';
  }

  // Bar B: diverging result-correctness bar. result_edge = model − market
  // probability assigned to the outcome that actually happened. Positive runs
  // right (green, model beat the market); negative runs left (red).
  function resultBar(f) {
    var edge = f.result_edge;
    if (edge === null || edge === undefined || isNaN(edge)) {
      return '<div class="tr-bar-row">' +
        '<span class="tr-bar-lbl">M vs Mkt</span>' +
        '<span class="tr-bar-track tr-div-track"><span class="tr-div-mid"></span></span>' +
        '<span class="tr-bar-val num dim">—</span>' +
      '</div>';
    }
    edge = Number(edge);
    // Edge lives in [-1, 1]; clamp to a ±0.5 visual span so typical gaps fill
    // the half-track without tiny edges vanishing.
    var SPAN = 0.5;
    var half = Math.min(Math.abs(edge) / SPAN, 1) * 50;
    if (half < 1 && edge !== 0) half = 1;
    var pos = edge >= 0;
    var fill = pos
      ? '<span class="tr-div-fill tr-div-pos" style="left:50%;width:' +
          half.toFixed(1) + '%"></span>'
      : '<span class="tr-div-fill tr-div-neg" style="right:50%;width:' +
          half.toFixed(1) + '%"></span>';
    return '<div class="tr-bar-row">' +
      '<span class="tr-bar-lbl">M vs Mkt</span>' +
      '<span class="tr-bar-track tr-div-track">' + fill +
        '<span class="tr-div-mid"></span></span>' +
      '<span class="tr-bar-val num ' + (pos ? "pos" : "neg") + '">' +
        esc(signedPct(edge, 1)) + '</span>' +
    '</div>';
  }

  function renderOutcomeBars(d) {
    var fixtures = (d.fixtures || []).filter(function (f) { return !f.pending; });
    if (!fixtures.length) {
      $("outcome-bars").innerHTML =
        '<div class="empty">No completed fixtures yet</div>';
      return;
    }
    var html = fixtures.map(function (f) {
      return '<div class="tr-pair">' +
        '<div class="tr-pair-head">' +
          '<span class="tr-pair-title">' + esc(f.fixture) + '</span>' +
          '<span class="tr-pair-actual">' + esc(f.score || "") + ' &middot; ' +
            esc(legTeam(f.fixture, f.outcome)) + '</span>' +
        '</div>' +
        scorelineBar(f) +
        resultBar(f) +
      '</div>';
    }).join("");

    html += '<div class="vt-note">top bar is the final (FT) scoreline as a ' +
      'home–away goal split; bottom bar runs right/green when the model beat ' +
      'the de-vigged closing market on the realised result, left/red when it ' +
      'didn’t (model − market probability on what happened)</div>';
    $("outcome-bars").innerHTML = html;
  }

  // ---- 2. prediction scoreboard -------------------------------------------

  function renderScoreboard(d) {
    var fixtures = (d.fixtures || []).filter(function (f) { return !f.pending; });
    $("board-meta").textContent = fixtures.length
      ? fixtures.length + " fixture" + (fixtures.length === 1 ? "" : "s")
      : "";
    if (!fixtures.length) {
      $("scoreboard").innerHTML =
        '<div class="empty">Nothing settled yet — picks appear here after full time</div>';
      return;
    }

    function pickCell(f, leg, ok) {
      if (!leg) return '<td class="dim">—</td>';
      return '<td>' + esc(legTeam(f.fixture, leg)) + ' ' + tick(ok) + '</td>';
    }

    var rows = fixtures.map(function (f) {
      var bm = f.brier_model, bk = f.brier_market;
      var brierCls = (bm !== null && bk !== null)
        ? (bm <= bk ? "pos" : "neg") : "";
      var top = f.top_scoreline || {};
      var topHtml = top.score
        ? esc(top.score) + ' <span class="dim">(' + esc(num(top.prob, 1)) + '%)</span> ' + tick(top.hit)
        : '<span class="dim">—</span>';
      var ouHtml = f.ou25
        ? esc(f.ou25.model_over >= 0.5 ? "over" : "under") + ' ' + tick(f.ou25.hit)
        : '<span class="dim">—</span>';
      var bttsHtml = f.btts
        ? esc(f.btts.model >= 0.5 ? "yes" : "no") + ' ' + tick(f.btts.hit)
        : '<span class="dim">—</span>';
      return '<tr>' +
        '<td class="pos-time dim num">' + esc(dateOnly(f.date)) + '</td>' +
        '<td class="pos-match" title="' + esc(f.fixture) + '">' + esc(f.fixture) + '</td>' +
        '<td class="num">' + esc(f.score || "—") + '</td>' +
        pickCell(f, f.model_pick, f.model_correct) +
        pickCell(f, f.market_pick, f.market_correct) +
        '<td class="r num"><span class="' + brierCls + '">' + esc(num(bm, 3)) + '</span>' +
          '<span class="dim"> / ' + esc(num(bk, 3)) + '</span></td>' +
        '<td>' + topHtml + '</td>' +
        '<td class="r">' + tick(f.top6_hit) + '</td>' +
        '<td>' + ouHtml + '</td>' +
        '<td>' + bttsHtml + '</td>' +
      '</tr>';
    }).join("");

    $("scoreboard").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Date</th><th>Fixture</th><th>FT</th>' +
          '<th>Model Pick</th><th>Market Pick</th>' +
          '<th class="r">Brier M/Mkt</th>' +
          '<th>Top Scoreline</th><th class="r">Top-6</th>' +
          '<th>O/U 2.5</th><th>BTTS</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  // ---- SVG scatter helpers ------------------------------------------------

  var SVG_W = 460, SVG_H = 300;
  var PAD = { l: 46, r: 14, t: 14, b: 34 };

  function sx(frac) { return PAD.l + frac * (SVG_W - PAD.l - PAD.r); }
  function sy(frac) { return SVG_H - PAD.b - frac * (SVG_H - PAD.t - PAD.b); }

  function svgOpen() {
    return '<svg viewBox="0 0 ' + SVG_W + ' ' + SVG_H +
      '" xmlns="http://www.w3.org/2000/svg" role="img">';
  }

  // ---- 3. calibration scatter ----------------------------------------------

  function renderCalibration(d) {
    var fixtures = (d.fixtures || []).filter(function (f) { return !f.pending; });
    var pts = [];
    fixtures.forEach(function (f) {
      ["home", "draw", "away"].forEach(function (leg) {
        var won = leg === f.outcome ? 1 : 0;
        if (f.model_1x2 && f.model_1x2[leg] !== null && f.model_1x2[leg] !== undefined) {
          pts.push({ p: f.model_1x2[leg], y: won, kind: "model",
            label: f.fixture + " — " + legLabel(leg) });
        }
        if (f.market_1x2 && f.market_1x2[leg] !== null && f.market_1x2[leg] !== undefined) {
          pts.push({ p: f.market_1x2[leg], y: won, kind: "market",
            label: f.fixture + " — " + legLabel(leg) });
        }
      });
    });
    if (!pts.length) {
      $("calibration").innerHTML =
        '<div class="chart-empty">No scored 1X2 legs yet</div>';
      return;
    }

    var parts = [svgOpen()];
    // Grid + axes.
    [0, 0.25, 0.5, 0.75, 1].forEach(function (g) {
      parts.push('<line class="cx-grid" x1="' + sx(g) + '" y1="' + sy(0) +
        '" x2="' + sx(g) + '" y2="' + sy(1) + '"/>');
      parts.push('<text class="cx-tick" x="' + sx(g) + '" y="' + (sy(0) + 16) +
        '" text-anchor="middle">' + Math.round(g * 100) + '%</text>');
    });
    [0, 1].forEach(function (g) {
      parts.push('<text class="cx-tick" x="' + (PAD.l - 8) + '" y="' +
        (sy(g) + 3) + '" text-anchor="end">' + (g ? "WON" : "LOST") + '</text>');
    });
    parts.push('<line class="cx-axis" x1="' + sx(0) + '" y1="' + sy(0) +
      '" x2="' + sx(1) + '" y2="' + sy(0) + '"/>');
    // Diagonal reference: perfect calibration.
    parts.push('<line class="tr-diag" x1="' + sx(0) + '" y1="' + sy(0) +
      '" x2="' + sx(1) + '" y2="' + sy(1) + '"/>');

    // Deterministic jitter so repeated loads look identical.
    pts.forEach(function (pt, i) {
      var jitter = 0.035 + ((i * 37) % 17) / 16 * 0.07;
      var yFrac = pt.y === 1 ? 1 - jitter : jitter;
      parts.push('<circle class="tr-dot-' + pt.kind + '" cx="' + sx(pt.p) +
        '" cy="' + sy(yFrac) + '" r="4"><title>' + esc(pt.label) + ' — ' +
        esc(pt.kind) + ' ' + esc(prob01(pt.p)) + ' — ' +
        (pt.y ? "happened" : "didn't") + '</title></circle>');
    });

    // Legend.
    parts.push('<circle class="tr-dot-model" cx="' + (sx(0) + 8) + '" cy="' +
      (PAD.t + 6) + '" r="4"/><text class="cx-legend" x="' + (sx(0) + 17) +
      '" y="' + (PAD.t + 9) + '">model</text>');
    parts.push('<circle class="tr-dot-market" cx="' + (sx(0) + 70) + '" cy="' +
      (PAD.t + 6) + '" r="4"/><text class="cx-legend" x="' + (sx(0) + 79) +
      '" y="' + (PAD.t + 9) + '">market</text>');

    parts.push('</svg>');
    $("calibration").innerHTML =
      '<div class="chart-canvas">' + parts.join("") + '</div>' +
      '<div class="vt-note">every 1X2 leg vs whether it happened; points should ' +
      'hug the diagonal as the sample grows</div>';
  }

  // ---- 4. CLV vs P/L scatter -----------------------------------------------

  function renderClvPl(d) {
    var bets = (d.bets || []).filter(function (b) {
      return b.clv !== null && b.clv !== undefined &&
        b.pl !== null && b.pl !== undefined;
    });
    $("clv-meta").textContent = bets.length
      ? bets.length + " settled bet" + (bets.length === 1 ? "" : "s") + " with close"
      : "";
    if (!bets.length) {
      $("clv-pl").innerHTML =
        '<div class="chart-empty">No settled bets with a captured closing line</div>';
      return;
    }

    var xs = bets.map(function (b) { return Number(b.clv) * 100; });
    var ys = bets.map(function (b) { return Number(b.pl); });
    var xMax = Math.max(Math.max.apply(null, xs.map(Math.abs)), 2) * 1.25;
    var yMax = Math.max(Math.max.apply(null, ys.map(Math.abs)), 2) * 1.25;

    function fx(v) { return (v + xMax) / (2 * xMax); }
    function fy(v) { return (v + yMax) / (2 * yMax); }

    var parts = [svgOpen()];
    // Frame grid ticks.
    [-xMax / 1.25, 0, xMax / 1.25].forEach(function (v) {
      parts.push('<text class="cx-tick" x="' + sx(fx(v)) + '" y="' +
        (SVG_H - PAD.b + 16) + '" text-anchor="middle">' +
        (v >= 0 ? "+" : "") + v.toFixed(1) + '%</text>');
    });
    [-yMax / 1.25, 0, yMax / 1.25].forEach(function (v) {
      parts.push('<text class="cx-tick" x="' + (PAD.l - 8) + '" y="' +
        (sy(fy(v)) + 3) + '" text-anchor="end">' +
        (v >= 0 ? "+" : "") + v.toFixed(0) + '</text>');
    });
    // Quadrant lines at zero.
    parts.push('<line class="tr-quad" x1="' + sx(fx(0)) + '" y1="' + sy(0) +
      '" x2="' + sx(fx(0)) + '" y2="' + sy(1) + '"/>');
    parts.push('<line class="tr-quad" x1="' + sx(0) + '" y1="' + sy(fy(0)) +
      '" x2="' + sx(1) + '" y2="' + sy(fy(0)) + '"/>');
    // Axis labels.
    parts.push('<text class="cx-legend" x="' + sx(1) + '" y="' +
      (SVG_H - PAD.b + 16) + '" text-anchor="end">CLV %</text>');
    parts.push('<text class="cx-legend" x="' + (PAD.l + 2) + '" y="' +
      (PAD.t + 9) + '">P/L</text>');

    bets.forEach(function (b) {
      var cls = Number(b.pl) >= 0 ? "tr-dot-win" : "tr-dot-loss";
      parts.push('<circle class="' + cls + '" cx="' + sx(fx(Number(b.clv) * 100)) +
        '" cy="' + sy(fy(Number(b.pl))) + '" r="5"><title>' +
        esc((b.match || "") + " — " + (b.selection || "")) + ' — CLV ' +
        esc(signedPct(b.clv)) + ', P/L ' + esc(signed(b.pl)) + '</title></circle>');
    });

    parts.push('</svg>');
    $("clv-pl").innerHTML =
      '<div class="chart-canvas">' + parts.join("") + '</div>' +
      '<div class="vt-note">top-right = beat the close and won; if CLV is real ' +
      'edge, dots drift toward the right half over time</div>';
  }

  // ---- pending --------------------------------------------------------------

  function renderPending(d) {
    var pending = d.pending || [];
    $("pending-meta").textContent = pending.length
      ? pending.length + " fixture" + (pending.length === 1 ? "" : "s")
      : "";
    if (!pending.length) {
      $("pending").innerHTML = '<div class="empty">Nothing on the board</div>';
      return;
    }
    var rows = pending.map(function (p) {
      var triple = p.model_1x2 || {};
      var top = p.top_scoreline || {};
      var pickProb = p.model_pick ? triple[p.model_pick] : null;
      return '<tr>' +
        '<td class="pos-time dim num">' + esc(dateOnly(p.kickoff)) + '</td>' +
        '<td class="pos-match">' + esc(p.fixture) + '</td>' +
        '<td>' + esc(p.model_pick ? legTeam(p.fixture, p.model_pick) : "—") +
          ' <span class="dim num">' + esc(prob01(pickProb)) + '</span></td>' +
        '<td class="num">' + esc(prob01(triple.home)) + ' / ' +
          esc(prob01(triple.draw)) + ' / ' + esc(prob01(triple.away)) + '</td>' +
        '<td class="num">' + esc(top.score || "—") +
          (top.prob !== null && top.prob !== undefined
            ? ' <span class="dim">(' + esc(num(top.prob, 1)) + '%)</span>' : "") +
        '</td>' +
      '</tr>';
    }).join("");
    $("pending").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Kickoff</th><th>Fixture</th><th>Model Pick</th>' +
          '<th>H / D / A</th><th>Top Scoreline</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  // ---- boot ------------------------------------------------------------------

  function renderFooter(d) {
    var gen = (d.meta && d.meta.generated) ? d.meta.generated : "";
    $("foot-gen").textContent = gen ? ("Generated " + gen) : "Generated —";
  }

  function showNoData(msg) {
    $("nodata-msg").textContent = msg || "NO DATA FEED";
    $("nodata").hidden = false;
  }

  function render(d) {
    renderStats(d);
    renderOutcomeBars(d);
    renderScoreboard(d);
    renderCalibration(d);
    renderClvPl(d);
    renderPending(d);
    renderFooter(d);
  }

  function load() {
    var url = "./tracking_data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        render(d);
      })
      .catch(function () {
        showNoData("TRACKING FEED UNAVAILABLE");
        render({ summary: {}, fixtures: [], pending: [], bets: [], meta: {} });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
