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

  // ---- enhanced Scorelines panel: all current+next-stage games, pivotable
  //      by stage or by team (data from scores_markets.json) ----------------
  function renderScoresMarkets(d) {
    var el = $("scores");
    if (!el) return false;
    if (!d || !d.group_games || !d.group_games.length) return false;
    if ($("scores-meta")) {
      $("scores-meta").textContent = d.group_games.length + " games · model markets";
    }
    var byTeam = d.by_team || {};
    var teams = Object.keys(byTeam).sort();
    var state = { mode: "stage", stage: "group", team: (byTeam.Iran ? "Iran" : teams[0]), sel: null };
    var pc = function (v) { return Math.round((v || 0) * 100); };
    function bar(x) {
      return '<span class="sm-bar"><i class="sm-h" style="width:' + pc(x[0]) +
        '%"></i><i class="sm-d" style="width:' + pc(x[1]) + '%"></i><i class="sm-a" style="width:' +
        pc(x[2]) + '%"></i></span>';
    }
    function fixKey(g) { return g.home + " v " + g.away; }
    function ftCell(g) {
      // full-time score to the LEFT of the model bar (blank for upcoming games)
      return g.ft
        ? '<span class="sm-ft">' + g.ft[0] + "–" + g.ft[1] + "</span>"
        : '<span class="sm-ft sm-ft-up">·</span>';
    }
    function gameRow(g) {
      var key = fixKey(g);
      return '<div class="sm-row' + (key === state.sel ? " sm-sel" : "") +
        '" data-fix="' + esc(key) + '">' +
        '<div class="sm-fix"><span class="sm-tm">' + esc(g.home) + " v " + esc(g.away) +
        '</span><span class="sm-meta">' + esc(String(g.date).slice(5)) + " · Grp " + esc(g.group) +
        '</span></div>' +
        '<div class="sm-1x2">' + ftCell(g) + bar(g.x1x2) + '<span class="sm-n">' + pc(g.x1x2[0]) + "·" +
        pc(g.x1x2[1]) + "·" + pc(g.x1x2[2]) + "</span></div>" +
        '<div class="sm-m"><b>' + esc(g.top) + "</b> " + pc(g.topp) + "%</div>" +
        '<div class="sm-m">O2.5 ' + pc(g.over25) + "%</div>" +
        '<div class="sm-m">BTTS ' + pc(g.btts) + "%</div>" +
        '<div class="sm-m sm-dim">xg ' + g.eg[0] + "-" + g.eg[1] + "</div></div>";
    }
    function groupedRows(games) {
      var byG = {};
      games.forEach(function (g) { (byG[g.group] = byG[g.group] || []).push(g); });
      return Object.keys(byG).sort().map(function (g) {
        return '<div class="sm-grp"><div class="sm-gh">GROUP ' + g + "</div>" +
          byG[g].map(gameRow).join("") + "</div>";
      }).join("");
    }
    function contentGroup() {
      // every group-stage fixture, played + upcoming, grouped by group
      return groupedRows(d.group_games || []);
    }
    function contentStage() {
      if (state.stage === "group") {
        // BY STAGE shows what's left of the current stage (upcoming only)
        return groupedRows((d.group_games || []).filter(function (g) {
          return g.status !== "FT";
        }));
      }
      if (state.stage === "r32") {
        return '<div class="sm-note">Projected qualifiers (model) — actual fixtures set once the group stage finalises.</div>' +
          '<div class="sm-projgrid">' + (d.r32_projected || []).map(function (p) {
            return '<div class="sm-proj"><div class="sm-gh">GRP ' + esc(p.group) + "</div>" +
              p.teams.map(function (t) {
                return '<div class="sm-pt"><span class="sm-pos">' + t.pos + "</span>" +
                  esc(t.team) + '<span class="sm-padv">' + pc(t.p_adv) + "% adv</span></div>";
              }).join("") + "</div>";
          }).join("") + "</div>";
      }
      return '<div class="empty">' + esc(state.stage.toUpperCase()) + " — not yet reached</div>";
    }
    function contentTeam() {
      var t = byTeam[state.team];
      if (!t) return '<div class="empty">No data</div>';
      var a = t.adv || {};
      var chip = function (lbl, v) {
        return "<span>" + lbl + " <b>" + (v != null ? pc(v) + "%" : "—") + "</b></span>";
      };
      var advLine = '<div class="sm-tadv">' + chip("Advance (R32)", a.R32) +
        chip("Win group", a.group_winner) + chip("Reach R16", a.R16) +
        chip("Reach QF", a.QF) + chip("Reach final", a.Final) + "</div>";
      var games = (t.games || []).map(gameRow).join("") ||
        '<div class="empty">No upcoming games this stage</div>';
      return advLine + '<div class="sm-grp"><div class="sm-gh">' + esc(state.team) +
        " — UPCOMING</div>" + games + "</div>";
    }
    function render() {
      var modes = '<div class="sm-modes">' +
        '<span class="sm-mtab' + (state.mode === "stage" ? " on" : "") + '" data-mode="stage">BY STAGE</span>' +
        '<span class="sm-mtab' + (state.mode === "group" ? " on" : "") + '" data-mode="group">BY GROUP</span>' +
        '<span class="sm-mtab' + (state.mode === "team" ? " on" : "") + '" data-mode="team">BY TEAM</span></div>';
      var sub;
      if (state.mode === "group") {
        sub = "";
      } else if (state.mode === "stage") {
        sub = '<div class="sm-stages">' + (d.stages || []).map(function (s) {
          var cls = "sm-stab" + (s.key === state.stage ? " on" : "") +
            (s.status === "next" ? " sm-next" : "") + (s.status === "locked" ? " sm-lock" : "");
          var bdg = s.status === "current" ? '<i class="sm-bdg">● ' + s.count + " left</i>" :
            (s.status === "next" ? '<i class="sm-bdg">▷ next</i>' : "");
          return '<span class="' + cls + '" data-stage="' + s.key + '">' + esc(s.label) + bdg + "</span>";
        }).join("") + "</div>";
      } else {
        sub = '<div class="sm-chips">' + teams.map(function (tm) {
          return '<span class="sm-chip' + (tm === state.team ? " on" : "") + '" data-team="' +
            esc(tm) + '">' + esc(tm) + "</span>";
        }).join("") + "</div>";
      }
      var body = state.mode === "group" ? contentGroup()
        : state.mode === "stage" ? contentStage() : contentTeam();
      el.innerHTML = modes + sub + '<div class="sm-cbody">' + body + "</div>";
    }
    if (!el._smWired) {
      el.addEventListener("click", function (e) {
        if (!e.target.closest) return;
        var t = e.target.closest("[data-mode],[data-stage],[data-team]");
        if (t) {
          if (t.getAttribute("data-mode")) state.mode = t.getAttribute("data-mode");
          else if (t.getAttribute("data-stage")) {
            if (t.className.indexOf("sm-lock") >= 0) return;
            state.stage = t.getAttribute("data-stage");
          } else if (t.getAttribute("data-team")) state.team = t.getAttribute("data-team");
          render();
          return;
        }
        // click a fixture row → select it (green); clears any prior selection
        var row = e.target.closest(".sm-row");
        if (row && row.getAttribute("data-fix")) {
          var key = row.getAttribute("data-fix");
          state.sel = (state.sel === key) ? null : key;
          render();
        }
      });
      el._smWired = true;
    }
    render();
    return true;
  }

  // ---- advancement: model edge vs Polymarket + group standings ------------
  // The edge⇄kelly matrix renderer lives in the shared adv_edge_matrix.js (one
  // source of truth, also used by the Visuals page) so the two can never drift.
  // scores.html loads it before this file.
  function renderEdgeMatrix(d) {
    if (window.WCAEdgeMatrix) window.WCAEdgeMatrix("adv-edge", d);
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
      fetchJson("./scores_markets.json").catch(function () { return null; }),
    ]).then(function (res) {
      var scoresData = res[0], expData = res[1], advData = res[2], marketsData = res[3];
      if (!expData) { showNoData("EXPOSURE FEED UNAVAILABLE"); }
      renderAdvancement(advData);
      renderRisk(expData);
      // Enhanced panel from scores_markets.json; fall back to the legacy
      // card-fixture view if that feed is unavailable.
      var enhanced = renderScoresMarkets(marketsData);
      if (!enhanced) {
        if (!scoresData) { showNoData("SCORES FEED UNAVAILABLE"); scoresData = { fixtures: [], meta: {} }; }
        renderScores(scoresData, expData);
      }
      renderFooter({
        meta: {
          generated: (marketsData && marketsData.meta && marketsData.meta.generated) ||
            (scoresData && scoresData.meta && scoresData.meta.generated) || ""
        }
      }, expData);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
