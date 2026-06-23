/* World Cup Alpha — scores, exposure & blind-spots page.
 *
 * Loads ./scores_data.json (model-vs-market) and ./exposure_data.json
 * (portfolio exposure) — both cache-busted — and renders:
 *   1. a Risk & Blind Spots panel: portfolio P&L distribution, upside/downside
 *      correlation, the outcomes you're NOT covered on, and gap-plug ideas;
 *   2. per fixture: a scoreline ladder, your exposure to each result (with the
 *      events/accas driving it), and the single best market price per leg.
 *
 * Everything degrades to a clean "no data" state. No external assets, no CDN.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------
  function pct1(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(1) + "%";
  }
  function prob01(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function num(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(dp === undefined ? 2 : dp);
  }
  function gbp(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v);
    return (n < 0 ? "-£" : "£") + Math.abs(n).toFixed(2);
  }
  function edgePct(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v) * 100;
    return (n >= 0 ? "+" : "") + n.toFixed(1) + "%";
  }
  function edgeClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "edge-flat";
    var n = Number(v);
    if (n >= 0.02) return "edge-up";
    if (n <= -0.02) return "edge-down";
    return "edge-flat";
  }
  function pnlClass(v) {
    var n = Number(v);
    if (n > 0.5) return "pos";
    if (n < -0.5) return "neg";
    return "dim";
  }
  function timeOnly(ts) {
    if (!ts) return "";
    var t = String(ts);
    var idx = t.indexOf("T");
    if (idx >= 0) return t.slice(idx + 1, idx + 6);
    idx = t.indexOf(" ");
    if (idx >= 0) return t.slice(idx + 1, idx + 6);
    return t;
  }
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- scoreline ladder ---------------------------------------------------
  // Green bar = model implied probability; blue bar = Polymarket exact-score
  // market probability (when a PM correct-score market exists for the fixture).
  // Both bars share one scale (maxProb) so their lengths are comparable; hover a
  // row for model vs PM. pm_prob is a percentage (matching prob), or absent.
  function renderScoreLadder(fx) {
    var scores = (fx.scores || []).slice(0, 6);
    if (!scores.length) return '<div class="empty">no scorelines</div>';
    var hasPM = scores.some(function (s) { return s.pm_prob != null; });
    var maxProb = scores.reduce(function (m, s) {
      return Math.max(m, Number(s.prob || 0), Number(s.pm_prob || 0));
    }, 0);
    return scores.map(function (s) {
      var frac = maxProb > 0 ? (Number(s.prob || 0) / maxProb) : 0;
      var modelBar = '<span class="sl-track"><span class="sl-fill sl-model" style="width:' +
        (frac * 100).toFixed(1) + '%"></span></span>';
      var bars, title;
      if (hasPM) {
        var hasThis = s.pm_prob != null;
        var pmFrac = (hasThis && maxProb > 0) ? (Number(s.pm_prob) / maxProb) : 0;
        bars = '<span class="sl-bars">' + modelBar +
          '<span class="sl-track"><span class="sl-fill sl-pm" style="width:' +
            (pmFrac * 100).toFixed(1) + '%"></span></span></span>';
        title = s.score + " — model " + pct1(s.prob) +
          " · PM " + (hasThis ? pct1(s.pm_prob) : "n/a");
      } else {
        bars = '<span class="sl-bars">' + modelBar + '</span>';
        title = s.score + " — model " + pct1(s.prob);
      }
      return '<div class="score-row" title="' + esc(title) + '">' +
        '<span class="score-label">' + esc(s.score) + '</span>' +
        bars +
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

  // ---- best-price summary (replaces the full venue table) -----------------
  // For each leg, the single best venue price + its edge vs the model fair.
  function renderBestPrice(fx) {
    var model = fx.model_1x2 || null;
    var venues = fx.venues || [];
    if (!venues.length || !model) {
      return '<div class="venue-empty">No priced markets matched</div>';
    }
    var legs = [
      { key: "home", label: "home" },
      { key: "draw", label: "draw" },
      { key: "away", label: "away" },
    ];
    var cells = legs.map(function (leg) {
      var best = null;
      venues.forEach(function (v) {
        var price = (v.selection_prices || {})[leg.key];
        var edge = (v.edge_vs_model || {})[leg.key];
        if (price === null || price === undefined || isNaN(price)) return;
        if (best === null || price > best.price) {
          best = { price: price, edge: edge, venue: v.venue };
        }
      });
      var fair = model[leg.key];
      var fairDec = (fair && fair > 0) ? (1 / Number(fair)) : null;
      if (!best) {
        return '<div class="bp-leg"><span class="bp-k">' + leg.label + '</span>' +
          '<span class="bp-fair">fair ' + num(fairDec) + '</span>' +
          '<span class="bp-px dim">—</span></div>';
      }
      return '<div class="bp-leg"><span class="bp-k">' + leg.label + '</span>' +
        '<span class="bp-fair">fair ' + num(fairDec) + '</span>' +
        '<span class="bp-px ' + edgeClass(best.edge) + ' num">' + num(best.price) +
          ' <span class="bp-edge">' + esc(edgePct(best.edge)) + '</span></span>' +
        '<span class="bp-venue dim" title="' + esc(best.venue) + '">' + esc(best.venue) + '</span>' +
      '</div>';
    }).join("");
    return '<div class="bestprice">' + cells + '</div>';
  }

  // ---- per-fixture exposure -----------------------------------------------
  function renderExposure(exp) {
    if (!exp || !exp.results || !exp.results.length) {
      return '<div class="venue-empty">No exposure on this match</div>';
    }
    var maxAbs = exp.results.reduce(function (m, r) {
      return Math.max(m, Math.abs(Number(r.net_pnl || 0)));
    }, 1);
    var rows = exp.results.map(function (r) {
      var net = Number(r.net_pnl || 0);
      var frac = Math.min(Math.abs(net) / maxAbs, 1);
      var side = net >= 0 ? "exp-pos" : "exp-neg";
      var blind = r.blindspot
        ? '<span class="exp-blind" title="meaningful probability, no positive exposure">BLIND</span>' : '';
      var plug = "";
      if (r.blindspot && r.plug) {
        plug = '<div class="exp-plug">' +
          (r.plug.available
            ? esc(r.plug.recommendation) + ' <span class="dim">(' +
              esc(r.plug.outcome) + ' @ ' + num(r.plug.best_odds) + ' ' +
              esc(r.plug.best_venue) + ')</span>'
            : esc(r.plug.note)) +
          '</div>';
      }
      return '<div class="exp-row">' +
          '<span class="exp-out">' + esc(r.outcome) + blind + '</span>' +
          '<span class="exp-p dim">' + esc(prob01(r.prob, 0)) + '</span>' +
          '<span class="exp-track"><span class="exp-fill ' + side +
            '" style="width:' + (frac * 100).toFixed(1) + '%"></span></span>' +
          '<span class="exp-net ' + pnlClass(net) + ' num">' + gbp(net) + '</span>' +
        '</div>' + plug;
    }).join("");
    // events touching this match (scorelines / props / accas)
    var ev = (exp.events || []).map(function (e) {
      return '<span class="exp-chip" title="' + esc(e.type) + '">' +
        esc(e.selection) + '</span>';
    }).join("");
    var evHtml = ev ? '<div class="exp-events"><span class="exp-evlabel">also on:</span>' + ev + '</div>' : '';
    var s = exp.summary || {};
    var summ = '<div class="exp-summ dim">max win ' + gbp(s.max_win) +
      ' · stake at risk ' + gbp(s.stake_at_risk) + '</div>';
    return rows + evHtml + summ;
  }

  // ---- date formatting helper ---------------------------------------------
  function fmtDate(d) {
    var months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    var parts = d.split("-");
    if (parts.length < 3) return d;
    var m = months[parseInt(parts[1], 10) - 1] || parts[1];
    return m + " " + parseInt(parts[2], 10);
  }

  // ---- fixture card -------------------------------------------------------
  function renderFixtureCard(fx, exp, dateKey) {
    var kickoff = timeOnly(fx.kickoff || (exp && exp.kickoff));
    var kickHtml = kickoff ? '<span class="sc-kick num">' + esc(kickoff) + '</span>' : '';
    var dateAttr = dateKey ? ' data-date="' + esc(dateKey) + '"' : '';
    return '<div class="sc-card"' + dateAttr + '>' +
        '<div class="sc-head">' +
          '<span class="sc-title">' + esc(fx.fixture) + '</span>' + kickHtml +
        '</div>' +
        '<div class="sc-body sc-body3">' +
          '<div class="sc-col"><div class="sc-coltag">scorelines</div>' +
            renderScoreLadder(fx) + renderModelFooter(fx) + '</div>' +
          '<div class="sc-col"><div class="sc-coltag">your exposure</div>' +
            renderExposure(exp) + '</div>' +
          '<div class="sc-col"><div class="sc-coltag">best price</div>' +
            renderBestPrice(fx) + '</div>' +
        '</div>' +
      '</div>';
  }

  // ---- risk & blind-spots panel -------------------------------------------
  function statChip(label, value, cls) {
    return '<div class="rk-stat"><span class="rk-stat-v ' + (cls || "") + '">' +
      esc(value) + '</span><span class="rk-stat-l">' + esc(label) + '</span></div>';
  }

  function renderRisk(exp) {
    if (!exp || !exp.portfolio) {
      $("risk").innerHTML = '<div class="empty">No exposure feed</div>';
      $("risk-meta").textContent = "";
      return;
    }
    var p = exp.portfolio, c = exp.correlation || {};
    var strip = '<div class="rk-strip">' +
      statChip("EV (slate)", gbp(p.ev), pnlClass(p.ev)) +
      statChip("best case", gbp(p.best), "pos") +
      statChip("worst case", gbp(p.worst), "neg") +
      statChip("P(profit)", prob01(p.p_profit, 0), "pos") +
      statChip("P(loss)", prob01(p.p_loss, 0), "neg") +
      statChip("P(win ≥ £50)", prob01(p.p_big_win, 0), "") +
    '</div>';

    var narr = c.narrative
      ? '<div class="rk-narr">' + esc(c.narrative) + '</div>' : '';

    var bs = (exp.blindspots || []);
    var bsHtml;
    if (!bs.length) {
      bsHtml = '<div class="rk-ok">No meaningful blind spots — every probable outcome carries positive exposure.</div>';
    } else {
      bsHtml = bs.map(function (b) {
        var plug = b.plug && b.plug.available
          ? '<div class="rk-plug">' + esc(b.plug.recommendation) +
            ' <span class="dim">(' + esc(b.plug.outcome) + ' @ ' +
            num(b.plug.best_odds) + ' ' + esc(b.plug.best_venue) + ', EV ' +
            (b.plug.ev_pct >= 0 ? "+" : "") + num(b.plug.ev_pct, 1) + '%)</span></div>'
          : (b.plug ? '<div class="rk-plug dim">' + esc(b.plug.note) + '</div>' : '');
        return '<div class="rk-bs">' +
          '<div class="rk-bs-head">' +
            '<span class="exp-blind">BLIND</span> ' +
            '<b>' + esc(b.fixture) + '</b> — <b>' + esc(b.outcome) + '</b>' +
            '<span class="dim"> · ' + prob01(b.prob, 0) + ' likely · net ' +
            gbp(b.net_pnl) + '</span>' +
          '</div>' + plug +
        '</div>';
      }).join("");
    }

    var worst = (c.worst_states || []).slice(0, 3).map(function (s) {
      return '<div class="rk-state"><span class="neg num">' + gbp(s.pnl) +
        '</span><span class="dim"> · ' + prob01(s.prob, 1) + ' · ' +
        esc((s.results || []).join(" / ")) + '</span></div>';
    }).join("");
    var best = (c.best_states || []).slice(0, 2).map(function (s) {
      return '<div class="rk-state"><span class="pos num">' + gbp(s.pnl) +
        '</span><span class="dim"> · ' + prob01(s.prob, 1) + ' · ' +
        esc((s.results || []).join(" / ")) + '</span></div>';
    }).join("");

    $("risk").innerHTML = strip + narr +
      '<div class="rk-cols">' +
        '<div class="rk-block"><div class="rk-h">Blind spots — outcomes you\'re not covered on</div>' + bsHtml + '</div>' +
        '<div class="rk-block"><div class="rk-h">Worst result-states</div>' + (worst || '<div class="dim">—</div>') +
          '<div class="rk-h" style="margin-top:8px">Best result-states</div>' + (best || '<div class="dim">—</div>') +
        '</div>' +
      '</div>';
    var off = [];
    if ((exp.off_slate_accas || []).length) off.push((exp.off_slate_accas).length + " off-slate acca");
    if ((exp.unmapped || []).length) off.push((exp.unmapped).length + " unmapped");
    $("risk-meta").textContent = (bs.length + " blind spot" + (bs.length === 1 ? "" : "s")) +
      (off.length ? "  ·  " + off.join(", ") : "");
  }

  // ---- scores grid --------------------------------------------------------
  function renderScores(scoresData, expData) {
    var fixtures = scoresData.fixtures || [];
    var expByName = {};
    (expData && expData.fixtures || []).forEach(function (f) { expByName[f.fixture] = f; });
    if (!fixtures.length) {
      $("scores").innerHTML = '<div class="empty">No fixture predictions</div>';
      $("scores-meta").textContent = "0";
      return;
    }

    // Collect unique dates from kickoffs (YYYY-MM-DD), preserving order
    var dates = [], seenDates = {};
    fixtures.forEach(function (fx) {
      var exp = expByName[fx.fixture];
      var ko = fx.kickoff || (exp && exp.kickoff);
      var dk = ko ? String(ko).slice(0, 10) : "";
      if (dk && !seenDates[dk]) { seenDates[dk] = true; dates.push(dk); }
    });
    dates.sort();

    // Build card HTML with data-date attributes
    var cardsHtml = fixtures.map(function (fx) {
      var exp = expByName[fx.fixture];
      var ko = fx.kickoff || (exp && exp.kickoff);
      var dk = ko ? String(ko).slice(0, 10) : "";
      return renderFixtureCard(fx, exp, dk);
    }).join("");

    // Build tabs only when there are multiple distinct dates
    var tabsHtml = "";
    if (dates.length > 1) {
      var items = '<button class="sc-tab sc-tab-active" data-filter="all">All</button>';
      items += dates.map(function (d) {
        return '<button class="sc-tab" data-filter="' + esc(d) + '">' + esc(fmtDate(d)) + '</button>';
      }).join("");
      tabsHtml = '<div class="sc-tabs">' + items + '</div>';
    }

    $("scores-meta").textContent = fixtures.length + " fixture" + (fixtures.length === 1 ? "" : "s");
    $("scores").innerHTML = tabsHtml + '<div class="sc-cards">' + cardsHtml + '</div>';

    // Wire up tab filtering
    if (dates.length > 1) {
      $("scores").addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest(".sc-tab") : null;
        if (!btn) return;
        var filter = btn.getAttribute("data-filter");
        var allTabs = $("scores").querySelectorAll(".sc-tab");
        for (var i = 0; i < allTabs.length; i++) {
          allTabs[i].classList.toggle("sc-tab-active", allTabs[i] === btn);
        }
        var cards = $("scores").querySelectorAll(".sc-card");
        var count = 0;
        for (var j = 0; j < cards.length; j++) {
          var show = filter === "all" || cards[j].getAttribute("data-date") === filter;
          cards[j].style.display = show ? "" : "none";
          if (show) count++;
        }
        $("scores-meta").textContent = count + " fixture" + (count === 1 ? "" : "s");
      });
    }
  }

  function renderFooter(scoresData, expData) {
    var gen = (scoresData.meta && scoresData.meta.generated) ||
      (expData && expData.meta && expData.meta.generated) || "";
    $("foot-gen").textContent = gen ? ("Generated " + gen) : "Generated —";
  }

  function showNoData(msg) {
    var el = $("nodata");
    $("nodata-msg").textContent = msg || "NO DATA FEED";
    el.hidden = false;
  }

  // ---- advancement: model edge vs Polymarket + group standings ------------
  function renderEdgeMatrix(d) {
    var el = $("adv-edge");
    if (!el) return;
    var teams = (d.teams || []).filter(function (t) {
      return t.model && (Number(t.model.win) || 0) > 0.003;
    });
    teams.sort(function (a, b) { return (Number(b.model.win) || 0) - (Number(a.model.win) || 0); });
    teams = teams.slice(0, 18);
    if (!teams.length) { el.innerHTML = '<div class="empty">No advancement data yet</div>'; return; }
    var stages = [["group_winner", "Grp"], ["R32", "R32"], ["R16", "R16"], ["QF", "QF"],
                  ["SF", "SF"], ["Final", "Final"], ["win", "Win"]];
    function bg(v) {
      if (v === null) return "transparent";
      var a = Math.min(1, Math.abs(v) / 0.15) * 0.8 + 0.06;
      return (v >= 0 ? "rgba(63,224,138," : "rgba(224,96,63,") + a.toFixed(2) + ")";
    }
    var leg = '<div class="adv-edge-leg">' +
      '<span><span class="d" style="background:rgba(63,224,138,0.85)"></span>model above market &mdash; back</span>' +
      '<span><span class="d" style="background:rgba(224,96,63,0.85)"></span>model below market &mdash; fade</span>' +
      '<span style="color:var(--muted)">cells: edge in points &middot; intensity = size</span></div>';
    var cols = "132px repeat(" + stages.length + ", 1fr)";
    var h = '<div class="adv-edge-grid" style="grid-template-columns:' + cols + '"><div></div>';
    stages.forEach(function (s) { h += '<div class="eh">' + s[1] + "</div>"; });
    teams.forEach(function (t) {
      h += '<div class="et">' + esc(t.team) + "</div>";
      stages.forEach(function (s) {
        var pmObj = t.pm ? t.pm[s[0]] : null;
        var v = (pmObj && pmObj.edge_adj != null) ? Number(pmObj.edge_adj) : null;
        var txt = v === null ? '<span style="color:var(--muted)">&middot;</span>'
          : (v >= 0 ? "+" : "") + (v * 100).toFixed(1);
        h += '<div class="ec" style="background:' + bg(v) + '">' + txt + "</div>";
      });
    });
    h += "</div>";
    el.innerHTML = leg + '<div class="adv-edge-wrap">' + h + "</div>";
  }

  function renderGroups(d) {
    var el = $("adv-groups");
    if (!el) return;
    var groups = d.groups || {};
    var letters = Object.keys(groups).sort();
    if (!letters.length) { el.innerHTML = '<div class="empty">No group results yet</div>'; return; }
    var html = letters.map(function (g) {
      var rows = (groups[g] || []).map(function (r) {
        var gd = (r.gd > 0 ? "+" : "") + r.gd;
        return '<tr class="' + (r.pos <= 2 ? "adv-q" : "") + '"><td>' + r.pos +
          '</td><td class="adv-tm">' + esc(r.team) + "</td><td>" + r.p + "</td><td>" +
          r.w + "</td><td>" + r.d + "</td><td>" + r.l + "</td><td>" + gd +
          '</td><td class="adv-pt">' + r.pts + "</td></tr>";
      }).join("");
      return '<div class="adv-grp"><div class="adv-grp-h">Group ' + esc(g) + "</div>" +
        '<table class="adv-gt"><thead><tr><th></th><th>team</th><th>P</th><th>W</th>' +
        "<th>D</th><th>L</th><th>GD</th><th>Pt</th></tr></thead><tbody>" + rows +
        "</tbody></table></div>";
    }).join("");
    el.innerHTML = '<div class="adv-grid">' + html + "</div>";
  }

  function renderAdvancement(d) {
    if (!d) return;
    var mg = d.meta || {};
    var em = $("adv-edge-meta");
    if (em) em.textContent = (mg.n_pm_markets ? mg.n_pm_markets + " PM markets" : (mg.model_generated || ""));
    var gm = $("adv-groups-meta");
    if (gm) gm.textContent = (d.groups ? Object.keys(d.groups).length + " groups" : "");
    try { renderEdgeMatrix(d); } catch (e) {}
    try { renderGroups(d); } catch (e) {}
  }

  // ---- boot ---------------------------------------------------------------
  function fetchJson(url) {
    return fetch(url + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  function load() {
    Promise.all([
      fetchJson("./scores_data.json").catch(function () { return null; }),
      fetchJson("./exposure_data.json").catch(function () { return null; }),
      fetchJson("./advancement_data.json").catch(function () { return null; }),
    ]).then(function (res) {
      var scoresData = res[0], expData = res[1], advData = res[2];
      if (!scoresData) { showNoData("SCORES FEED UNAVAILABLE"); scoresData = { fixtures: [], meta: {} }; }
      if (!expData) { showNoData("EXPOSURE FEED UNAVAILABLE"); }
      renderAdvancement(advData);
      renderRisk(expData);
      renderScores(scoresData, expData);
      renderFooter(scoresData, expData);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
