// Visuals (folded into Scores & Markets). The static SVG exhibits are
// meaningful with no JS; this module only fills the one data-driven exhibit
// that is unique to Visuals — "Model Outputs Over Time" (#adv-chart), from
// advancement_history.json. The live edge matrix + group standings already
// render on Scores & Markets (scores.js), and the footer is stamped there too,
// so we deliberately do NOT touch #adv-edge / #adv-groups / #foot-gen here to
// avoid double-rendering when this script shares the Scores page.
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- Exhibit: advancement over time --------------------------------------

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
    // Show the REMAINING teams only (latest P > 0) rather than a fixed
    // top-12 slice — a fixed slice pads the table with eliminated 0%
    // teams once the field has narrowed (2026-07-13).
    var ordered = Object.keys(teams)
      .filter(function (t) { return (latest[t] || 0) > 0; })
      .sort(function (a, b) { return (latest[b] || 0) - (latest[a] || 0); });
    var head = "<tr><th>team</th>" + snaps.map(function (s) { return "<th>" + (s.label || s.date || "") + "</th>"; }).join("") + "<th>&Delta;</th></tr>";
    var rows = ordered.map(function (t) {
      var cells = snaps.map(function (s) {
        var v = (s.probs || {})[t];
        return "<td>" + (v == null ? "&mdash;" : (v * 100).toFixed(1) + "%") + "</td>";
      }).join("");
      var first = (snaps[0].probs || {})[t], last = latest[t];
      var dd = (first != null && last != null) ? ((last - first) * 100) : null;
      var dCell = dd == null ? "&mdash;" :
        '<span style="color:' + (dd >= 0 ? "#1A7A4C" : "#C0273A") + '">' + (dd >= 0 ? "+" : "") + dd.toFixed(1) + "</span>";
      return "<tr><td>" + t + "</td>" + cells + "<td>" + dCell + "</td></tr>";
    }).join("");
    return '<table class="tbl"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>";
  }
})();
