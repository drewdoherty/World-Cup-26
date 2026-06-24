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
  function signPct(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var s = (Number(v) * 100).toFixed(dp === undefined ? 1 : dp);
    return (Number(v) > 0 ? "+" : "") + s + "%";
  }
  function usd(v) { return v == null || isNaN(v) ? "—" : "$" + Number(v).toFixed(2); }
  function gbp(v) { return v == null || isNaN(v) ? "—" : "£" + Number(v).toFixed(2); }
  function num(v, dp) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(dp || 2); }
  function price(v) { return v == null || isNaN(v) ? "—" : Number(v).toFixed(3); }

  function fetchJson(url) {
    return fetch(url + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  // -------------------------------------------------------------------------
  // NEW 01 // Best Bets — both sides, fractional Kelly
  // -------------------------------------------------------------------------
  function sideCell(s, isBest) {
    if (!s) return "—";
    var cls = (s.ev != null && s.ev > 0) ? "pos" : "neg";
    var tag = isBest ? "<strong>" + esc(s.side) + "</strong>" : '<span class="dim">' + esc(s.side) + "</span>";
    return tag + ' @ ' + price(s.price) + ' <span class="' + cls + '">' + signPct(s.ev) + "</span>";
  }

  function recRows(list, opts) {
    opts = opts || {};
    return (list || []).map(function (r) {
      var stakeCell = opts.flagged
        ? '<span class="dim">' + usd(r.stake) + "</span>"
        : '<strong>' + usd(r.stake) + "</strong>";
      var note = opts.flagged
        ? '<td class="neg" style="font-size:10px">' + esc(r.flag || "") + "</td>"
        : '<td class="r pos">' + signPct(r.edge) + "</td>";
      var driftCls = (r.drift != null && r.drift > 0.1) ? "neg" : "dim";
      return '<tr>' +
        '<td class="pos-match">' + esc(r.team) +
          '<span class="dim"> · ' + esc(r.best_side === "YES" ? r.yes.desc : r.no.desc) + "</span></td>" +
        '<td class="r">' + num(r.model_prob * 100, 1) + "%</td>" +
        '<td class="r">' + price(r.live_mid) +
          ' <span class="' + driftCls + '" style="font-size:10px">Δ' + signPct(r.live_mid - r.baseline_pm) + "</span></td>" +
        '<td>' + sideCell(r.yes, r.best_side === "YES") + '<br>' + sideCell(r.no, r.best_side === "NO") + "</td>" +
        '<td class="r">' + stakeCell + "</td>" +
        note +
      "</tr>";
    }).join("");
  }

  function renderBestBets(data) {
    if (!data) { $("betrecs-table").innerHTML = '<div class="empty" style="padding:18px;color:var(--text-dim)">BET RECS FEED UNAVAILABLE</div>'; return; }
    var m = data.meta || {};
    var best = data.best_bets || [];
    var flagged = data.flagged || [];
    $("betrecs-meta").textContent =
      (best.length ? best.length + " rec" + (best.length === 1 ? "" : "s") : "none") +
      (flagged.length ? " · " + flagged.length + " withheld" : "") +
      (m.generated ? " · " + esc(m.generated) : "");
    if (m.per_bet_cap != null) $("betrecs-cap").textContent = pct(m.per_bet_cap, 0);
    if (m.max_stake != null) $("betrecs-max").textContent = usd(m.max_stake);
    $("betrecs-stale").hidden = !m.model_stale;
    if ($("betrecs-cov")) {
      $("betrecs-cov").innerHTML = "Coverage: " + esc(m.coverage || "") +
        (m.model_generated ? " · model feed " + esc(m.model_generated) : "") +
        (m.live_fetch_error ? ' · <span class="neg">live fetch error: ' + esc(m.live_fetch_error) + "</span>" : "");
    }

    var head = '<table class="pos-table"><thead><tr>' +
      '<th>Best bet</th><th class="r">Model</th><th class="r">PM (Δ vs model)</th>' +
      '<th>Both sides (back@ · fee-adj EV)</th><th class="r">½-Kelly</th><th class="r">Edge</th>' +
      '</tr></thead><tbody>';
    if (!best.length) {
      $("betrecs-table").innerHTML = '<div class="empty" style="padding:18px;color:var(--text-dim)">No positive-edge model-backed bets right now.</div>';
    } else {
      $("betrecs-table").innerHTML = head + recRows(best) + "</tbody></table>";
    }

    if (flagged.length) {
      $("betrecs-flagged-wrap").hidden = false;
      $("betrecs-flagged").innerHTML =
        '<table class="pos-table"><thead><tr>' +
        '<th>Withheld bet</th><th class="r">Model</th><th class="r">PM (Δ vs model)</th>' +
        '<th>Both sides</th><th class="r">½-Kelly</th><th>Why withheld</th>' +
        '</tr></thead><tbody>' + recRows(flagged, { flagged: true }) + "</tbody></table>";
    } else {
      $("betrecs-flagged-wrap").hidden = true;
    }
  }

  // -------------------------------------------------------------------------
  // NEW 02 // Prop Arbs — Polymarket vs Sportsbook
  // -------------------------------------------------------------------------
  function renderPropArbs(data) {
    var pa = (data && data.prop_arbs) || {};
    var internal = pa.pm_internal || [];
    var book = pa.pm_vs_book || [];
    var all = internal.concat(book);
    $("proparb-meta").textContent = all.length ? all.length + " opp" + (all.length === 1 ? "" : "s") : "none";
    if ($("proparb-note")) {
      $("proparb-note").textContent = pa.pm_vs_book_note || "";
    }
    if (!all.length) {
      $("proparb-table").innerHTML = '<div class="empty" style="padding:18px;color:var(--text-dim)">No settlement-safe prop arbs right now.</div>';
      return;
    }
    var rows = all.map(function (a) {
      var legs = (a.legs || []).map(function (l) {
        return esc(l.venue) + " " + esc(l.side) + " @ " + price(l.price) + " (" + usd(l.stake) + ")";
      }).join("  ·  ");
      return '<tr>' +
        '<td class="pos-match">' + esc(a.team || "") + '<span class="dim"> · ' + esc(a.market || a.kind) + "</span></td>" +
        '<td><span class="dim" style="font-size:10px">' + legs + "</span></td>" +
        '<td class="r pos">' + pct(a.guaranteed_pct) + "</td>" +
      "</tr>";
    }).join("");
    $("proparb-table").innerHTML =
      '<table class="pos-table"><thead><tr><th>Market</th><th>Legs (settlement-safe)</th><th class="r">Guaranteed %</th></tr></thead><tbody>' +
      rows + "</tbody></table>";
  }

  // -------------------------------------------------------------------------
  // EXISTING 03/04/05 // pure-arb monitor (1X2 family)
  // -------------------------------------------------------------------------
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
    if (!hist) { if ($("hist-summary")) $("hist-summary").innerHTML = '<span class="dim">no historical feed</span>'; return; }
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
    var m = (data && data.meta) || {};
    if ($("foot-gen") && m.generated) $("foot-gen").textContent = "bet recs generated " + esc(m.generated);
  }
  function showNoData(msg) {
    var el = $("nodata"); if (!el) return;
    el.hidden = false; if ($("nodata-msg")) $("nodata-msg").textContent = msg || "NO DATA";
  }

  function load() {
    Promise.all([
      fetchJson("./bet_recs.json").catch(function () { return null; }),
      fetchJson("./arb_data.json").catch(function () { return null; }),
      fetchJson("./arb_history.json").catch(function () { return null; })
    ]).then(function (res) {
      var recs = res[0], arbData = res[1], hist = res[2];
      if (!recs) showNoData("BET RECS FEED UNAVAILABLE");
      renderBestBets(recs);
      renderPropArbs(recs || {});
      if (!arbData) arbData = { arbs: [], meta: {}, hypothetical: {} };
      renderArbs(arbData); renderHistory(hist); renderHypo(arbData);
      renderFooter(recs || arbData);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
})();
