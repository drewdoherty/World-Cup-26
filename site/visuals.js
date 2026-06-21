// Visuals tab. Static SVG exhibits are meaningful with no JS; this stamps the
// footer, renders the data-driven tournament panels from advancement_data.json
// (progression chart, model-vs-Polymarket table, group standings), and fills
// Exhibit 3 (advancement-over-time) from advancement_history.json.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function pctv(p) {
    if (p === null || p === undefined || isNaN(p)) return "&mdash;";
    return (Number(p) * 100).toFixed(1) + "%";
  }

  var gen = $("foot-gen");
  if (gen) gen.textContent = "generated " + new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";

  var PROG_STAGES = ["R32", "R16", "QF", "SF", "Final", "win"];
  var STAGE_LABEL = { R32: "R32", R16: "R16", QF: "QF", SF: "SF", Final: "Final", win: "Win", group_winner: "Group" };
  var PALETTE = ["#3fe08a", "#60a5fa", "#e0a03f", "#e0603f", "#a855f7", "#2dd4bf",
                 "#f472b6", "#84cc16", "#fb923c", "#818cf8", "#f43f5e", "#22d3ee"];
  var TOP = 12;

  // ---- tournament progression (survival) chart -----------------------------

  function progTip(t) {
    var lines = [t.team + " (" + (t.group || "?") + ")  — modelled survival"];
    PROG_STAGES.forEach(function (s) {
      var m = t.model ? t.model[s] : null;
      var pm = (t.pm && t.pm[s]) ? t.pm[s].pm : null;
      var line = "  " + (STAGE_LABEL[s] || s) + ": model " +
        (m == null ? "-" : (m * 100).toFixed(1) + "%");
      if (pm != null) line += "  · PM " + (pm * 100).toFixed(1) + "%";
      lines.push(line);
    });
    return lines.join("\n");
  }

  function renderProgression(d) {
    var teams = d.teams || [];
    if (!teams.length) { $("adv-progress").innerHTML = '<div class="empty">No advancement data yet</div>'; return; }
    var stageKeys = (d.meta && d.meta.stages) || PROG_STAGES;
    var cols = [{ key: "_grp", label: "Group" }].concat(stageKeys.map(function (s) {
      return { key: s, label: STAGE_LABEL[s] || s };
    }));
    var W = 720, H = 360, P = { l: 40, r: 16, t: 14, b: 40 };
    var pw = W - P.l - P.r, ph = H - P.t - P.b, n = cols.length;
    function cx(i) { return P.l + (n === 1 ? 0 : i / (n - 1) * pw); }
    function cy(p) { return P.t + (1 - Math.max(0, Math.min(1, p))) * ph; }
    function val(t, c) { return c.key === "_grp" ? 1.0 : (Number(t.model && t.model[c.key]) || 0); }

    var parts = ['<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" role="img" class="viz-svg">'];
    [0, 0.25, 0.5, 0.75, 1.0].forEach(function (g) {
      var y = cy(g);
      parts.push('<line x1="' + P.l + '" y1="' + y.toFixed(1) + '" x2="' + (W - P.r) + '" y2="' + y.toFixed(1) + '" stroke="#191d23"/>');
      parts.push('<text x="' + (P.l - 6) + '" y="' + (y + 3).toFixed(1) + '" fill="#4a525c" font-size="9" text-anchor="end" font-family="monospace">' + (g * 100) + '%</text>');
    });
    cols.forEach(function (c, i) {
      parts.push('<line x1="' + cx(i).toFixed(1) + '" y1="' + P.t + '" x2="' + cx(i).toFixed(1) + '" y2="' + (H - P.b) + '" stroke="#15181d"/>');
      parts.push('<text x="' + cx(i).toFixed(1) + '" y="' + (H - P.b + 16) + '" fill="#6b7480" font-size="9.5" text-anchor="middle" font-family="monospace">' + esc(c.label) + '</text>');
    });
    function line(t, stroke, sw, op) {
      var pts = cols.map(function (c, i) { return cx(i).toFixed(1) + "," + cy(val(t, c)).toFixed(1); }).join(" ");
      return '<polyline points="' + pts + '" fill="none" stroke="' + stroke + '" stroke-width="' + sw + '" opacity="' + op + '"><title>' + esc(progTip(t)) + '</title></polyline>';
    }
    teams.slice(TOP).forEach(function (t) { parts.push(line(t, "#2a3038", 1, 0.5)); });
    teams.slice(0, TOP).forEach(function (t, k) {
      var col = PALETTE[k % PALETTE.length];
      parts.push(line(t, col, 1.8, 0.92));
      parts.push('<circle cx="' + cx(n - 1).toFixed(1) + '" cy="' + cy(val(t, cols[n - 1])).toFixed(1) + '" r="2.4" fill="' + col + '"/>');
    });
    parts.push('</svg>');

    var legend = '<div class="adv-legend">' + teams.slice(0, TOP).map(function (t, k) {
      return '<span class="adv-leg"><span class="adv-dot" style="background:' + PALETTE[k % PALETTE.length] + '"></span>' +
        esc(t.team) + ' <span class="dim">' + pctv(t.model && t.model.win) + '</span></span>';
    }).join("") + '</div>';
    $("adv-progress").innerHTML = parts.join("") + legend;
  }

  // ---- model implied advancement vs Polymarket -----------------------------

  var MVP_STAGE = "win";
  var MVP_STAGES = [["R32", "Reach R32"], ["R16", "Reach R16"], ["QF", "Reach QF"],
                    ["SF", "Reach SF"], ["Final", "Reach Final"], ["win", "Win it"],
                    ["group_winner", "Win group"]];

  function drawMvp(d) {
    var rows = (d.teams || []).map(function (t) {
      var m = t.model ? t.model[MVP_STAGE] : null;
      var pmObj = t.pm ? t.pm[MVP_STAGE] : null;
      var pm = pmObj ? pmObj.pm : null;
      return { team: t.team, group: t.group, model: m, pm: pm,
               edge: (m != null && pm != null) ? (Number(m) - Number(pm)) : null };
    }).filter(function (r) { return r.model != null && Number(r.model) > 0.0005; });
    rows.sort(function (a, b) { return (b.model || 0) - (a.model || 0); });
    rows = rows.slice(0, 20);
    var head = "<tr><th>team</th><th>grp</th><th class='r'>model</th><th class='r'>polymarket</th><th class='r'>edge</th></tr>";
    var body = rows.map(function (r) {
      var edge = r.edge == null ? '<span class="dim">&mdash;</span>'
        : '<span style="color:' + (r.edge >= 0 ? "#3fe08a" : "#e0603f") + '">' +
          (r.edge >= 0 ? "+" : "") + (r.edge * 100).toFixed(1) + "</span>";
      return "<tr><td>" + esc(r.team) + "</td><td class='dim'>" + esc(r.group || "") +
        "</td><td class='r'>" + pctv(r.model) + "</td><td class='r'>" +
        (r.pm == null ? '<span class="dim">&mdash;</span>' : pctv(r.pm)) +
        "</td><td class='r'>" + edge + "</td></tr>";
    }).join("");
    $("adv-mvp").innerHTML = '<table class="tbl adv-tbl"><thead>' + head + "</thead><tbody>" + body + "</tbody></table>";
  }

  function renderMvp(d) {
    $("adv-mvp-controls").innerHTML = MVP_STAGES.map(function (s) {
      return '<button class="adv-stage" data-stage="' + s[0] + '"' +
        (s[0] === MVP_STAGE ? ' aria-pressed="true"' : "") + ">" + esc(s[1]) + "</button>";
    }).join("");
    drawMvp(d);
    Array.prototype.forEach.call(document.querySelectorAll("#adv-mvp-controls .adv-stage"), function (btn) {
      btn.addEventListener("click", function () {
        MVP_STAGE = btn.getAttribute("data-stage");
        Array.prototype.forEach.call(document.querySelectorAll("#adv-mvp-controls .adv-stage"), function (b) {
          b.removeAttribute("aria-pressed");
        });
        btn.setAttribute("aria-pressed", "true");
        drawMvp(d);
      });
    });
  }

  // ---- group standings -----------------------------------------------------

  function renderGroups(d) {
    var groups = d.groups || {};
    var letters = Object.keys(groups).sort();
    if (!letters.length) { $("adv-groups").innerHTML = '<div class="empty">No group results yet</div>'; return; }
    var html = letters.map(function (g) {
      var rows = (groups[g] || []).map(function (r) {
        var gd = (r.gd > 0 ? "+" : "") + r.gd;
        return '<tr class="' + (r.pos <= 2 ? "adv-q" : "") + '"><td>' + r.pos +
          '</td><td class="adv-tm">' + esc(r.team) + '</td><td>' + r.p + '</td><td>' +
          r.w + '</td><td>' + r.d + '</td><td>' + r.l + '</td><td>' + gd +
          '</td><td class="adv-pt">' + r.pts + '</td></tr>';
      }).join("");
      return '<div class="adv-grp"><div class="adv-grp-h">Group ' + esc(g) + '</div>' +
        '<table class="adv-gt"><thead><tr><th></th><th>team</th><th>P</th><th>W</th>' +
        '<th>D</th><th>L</th><th>GD</th><th>Pt</th></tr></thead><tbody>' + rows +
        '</tbody></table></div>';
    }).join("");
    $("adv-groups").innerHTML = '<div class="adv-grid">' + html + "</div>";
  }

  // Advancement panels (progression, model-vs-Polymarket, standings) moved to
  // Scores & Markets (scores.html / scores.js) as the edge matrix + standings.

  // ---- Exhibit 1: live model-edge-vs-Polymarket matrix ---------------------
  // Renders the same edge⇄kelly matrix as Scores & Markets via the shared
  // window.WCAEdgeMatrix, fed live from advancement_data.json (cache-busted) so
  // the table reflects current model-vs-market state, never a stale snapshot.
  fetch("./advancement_data.json?t=" + Date.now(), { cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(function (adv) {
      if (!adv) return;
      var mg = adv.meta || {};
      var meta = $("adv-edge-meta");
      if (meta) meta.textContent = mg.n_pm_markets ? mg.n_pm_markets + " PM markets" : (mg.model_generated || "");
      var foot = $("adv-edge-foot");
      if (foot && mg.generated) {
        foot.textContent = "model blend (Elo + Dixon-Coles) vs Shin-devigged Polymarket · data " + mg.generated;
      }
      if (window.WCAEdgeMatrix) window.WCAEdgeMatrix("adv-edge", adv);
    });

  // ---- Exhibit 3: advancement over time (existing) -------------------------

  fetch("./advancement_history.json", { cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(function (hist) {
      if (!hist || !Array.isArray(hist.snapshots) || hist.snapshots.length < 2) return;
      var el = $("adv-chart");
      var meta = $("adv-meta");
      if (meta) meta.textContent = hist.snapshots.length + " snapshots";
      if (!el) return;
      el.classList.remove("empty");
      el.innerHTML = renderAdvancementTable(hist.snapshots);
    });

  function renderAdvancementTable(snaps) {
    var teams = {};
    snaps.forEach(function (s) { Object.keys(s.probs || {}).forEach(function (t) { teams[t] = true; }); });
    var latest = snaps[snaps.length - 1].probs || {};
    var ordered = Object.keys(teams).sort(function (a, b) { return (latest[b] || 0) - (latest[a] || 0); }).slice(0, 12);
    var head = "<tr><th>team</th>" + snaps.map(function (s) { return "<th>" + (s.label || s.date || "") + "</th>"; }).join("") + "<th>&Delta;</th></tr>";
    var rows = ordered.map(function (t) {
      var cells = snaps.map(function (s) {
        var v = (s.probs || {})[t];
        return "<td>" + (v == null ? "&mdash;" : (v * 100).toFixed(1) + "%") + "</td>";
      }).join("");
      var first = (snaps[0].probs || {})[t], last = latest[t];
      var dd = (first != null && last != null) ? ((last - first) * 100) : null;
      var dCell = dd == null ? "&mdash;" :
        '<span style="color:' + (dd >= 0 ? "#3fe08a" : "#e0603f") + '">' + (dd >= 0 ? "+" : "") + dd.toFixed(1) + "</span>";
      return "<tr><td>" + t + "</td>" + cells + "<td>" + dCell + "</td></tr>";
    }).join("");
    return '<table class="tbl"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>";
  }
})();
