(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function pct(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 2 : dp) + "%";
  }
  function usd(v) { return v == null || isNaN(v) ? "—" : "$" + Number(v).toFixed(2); }
  function gbp(v) { return v == null || isNaN(v) ? "—" : "£" + Number(v).toFixed(2); }
  function num(v, dp) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(dp || 2); }

  function fetchJson(url) {
    return fetch(url + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  function renderArbs(data) {
    var arbs = data.arbs || [];
    var fx = (data.meta || {}).fx_usd_per_gbp;
    $("arb-meta").textContent = (arbs.length ? arbs.length + " opp" + (arbs.length === 1 ? "" : "s") : "none") +
      (fx ? " · GBP/USD " + num(fx, 4) + " (" + esc((data.meta || {}).fx_source || "") + ")" : "");
    if (!arbs.length) {
      $("arb-table").innerHTML = '<div class="empty" style="padding:18px;color:var(--text-dim)">No risk-free opportunities right now.</div>';
      return;
    }
    function stakeStr(legs) {
      return (legs || []).map(function (l) {
        var amt = l.currency === "USD" ? usd(l.stake) : gbp(l.stake);
        return amt + " " + esc(l.venue);
      }).join(" / ");
    }
    function legStr(legs) {
      return (legs || []).map(function (l) {
        return esc(l.side === "win" ? "back" : "oppose") + " " + esc(l.desc);
      }).join("  ·  ");
    }
    function confClass(c) {
      return c === "execution-grade" ? "pos" : (c === "low" ? "neg" : "dim");
    }
    var rows = arbs.map(function (a) {
      return '<tr>' +
        '<td class="pos-time pos-match">' + esc(a.fixture) + '<span class="dim"> · ' + esc(a.selection) + '</span></td>' +
        '<td><strong>' + esc(a.venue_pair) + '</strong><br><span class="dim" style="font-size:10px">' + legStr(a.legs) + '</span></td>' +
        '<td class="r ' + (a.fee_adj_edge > 0 ? 'pos' : 'neg') + '">' + pct(a.fee_adj_edge) + '</td>' +
        '<td class="r">' + stakeStr(a.legs) + '</td>' +
        '<td class="r pos">' + pct(a.guaranteed_pct) + '</td>' +
        '<td class="' + confClass(a.confidence) + '">' + esc(a.confidence || "") + '</td>' +
      '</tr>';
    }).join("");
    $("arb-table").innerHTML =
      '<table class="pos-table"><thead><tr>' +
        '<th>Market</th><th>Venue pair / legs</th>' +
        '<th class="r">Fee-adj edge</th>' +
        '<th class="r">Stake split</th><th class="r">Guaranteed %</th><th>Confidence</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  function renderHistory(hist) {
    if (!hist) return;
    var prov = hist.best_leg_provider || {};
    $("hist-summary").innerHTML =
      "<strong>" + esc(hist.hypothesis || "") + "</strong><br>" +
      "Scanned " + (hist.snapshots_scanned || 0) + " snapshots · true risk-free arbs: <strong>" +
      (hist.true_back_only_arbs || 0) + "</strong> · min overround " + num(hist.min_overround_seen, 4) +
      " · best-leg providers: " + (prov.sportsbook || 0) + " sportsbook / " + (prov.exchange || 0) + " exchange.";
    var near = hist.tightest_near_arbs || [];
    $("hist-near").innerHTML = near.length ?
      ('<table class="pos-table"><thead><tr><th>Tightest near-arb</th><th class="r">Overround</th><th>Best legs (venue class)</th></tr></thead><tbody>' +
        near.map(function (n) {
          var legs = Object.keys(n.best_legs || {}).map(function (k) {
            return esc(k) + ": " + esc(n.best_legs[k].book) + " (" + esc(n.best_legs[k].class) + ")";
          }).join(" · ");
          return '<tr><td class="pos-match">' + esc(n.event) + '</td><td class="r">' + num(n.overround, 4) +
                 '</td><td><span class="dim" style="font-size:10px">' + legs + '</span></td></tr>';
        }).join("") + '</tbody></table>') : '<div class="dim">no near-arbs</div>';
  }

  function renderHypo(data) {
    var h = data.hypothetical || {};
    var pts = h.points || [];
    $("hyp-summary").innerHTML = "Modeled cumulative guaranteed return: <strong style='color:var(--pos)'>" +
      pct(h.cum_pct) + "</strong> over " + pts.length + " detection(s). " +
      "<span class='dim'>(0 realized — see historical analysis; markets efficient.)</span>";
    var svg = $("hyp-chart");
    if (!pts.length) { svg.innerHTML = ''; return; }
    var W = 700, H = 160, pad = 8;
    var ys = pts.map(function (p) { return p.cum_pct || 0; });
    var maxY = Math.max.apply(null, ys.concat([0.0001]));
    var step = pts.length > 1 ? (W - 2 * pad) / (pts.length - 1) : 0;
    var d = pts.map(function (p, i) {
      var x = pad + i * step;
      var y = H - pad - ((p.cum_pct || 0) / maxY) * (H - 2 * pad);
      return (i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    svg.innerHTML =
      '<path d="' + d + '" fill="none" stroke="var(--warn)" stroke-width="1.5" stroke-dasharray="4 3"/>' +
      '<text x="' + pad + '" y="14" fill="var(--muted)" font-size="9">HYPOTHETICAL</text>';
  }

  function renderFooter(data) {
    var m = data.meta || {};
    if ($("foot-gen") && m.generated) $("foot-gen").textContent = "data generated " + esc(m.generated);
  }
  function showNoData(msg) {
    var el = $("nodata"); if (!el) return;
    el.hidden = false; if ($("nodata-msg")) $("nodata-msg").textContent = msg || "NO DATA";
  }

  function load() {
    Promise.all([
      fetchJson("./arb_data.json").catch(function () { return null; }),
      fetchJson("./arb_history.json").catch(function () { return null; })
    ]).then(function (res) {
      var data = res[0], hist = res[1];
      if (!data) { showNoData("ARB FEED UNAVAILABLE"); data = { arbs: [], meta: {}, hypothetical: {} }; }
      renderArbs(data); renderHistory(hist); renderHypo(data); renderFooter(data);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
})();
