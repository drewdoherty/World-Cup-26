// Visuals tab — progressive enhancement only. The page is meaningful with no
// JS (static SVG exhibits); this just stamps the footer and renders Exhibit 3
// (advancement-over-time) IF its history file exists yet.
(function () {
  "use strict";

  var gen = document.getElementById("foot-gen");
  if (gen) gen.textContent = "generated " + new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";

  // Exhibit 3: render only when advancement_history.json has >= 2 snapshots.
  fetch("./advancement_history.json", { cache: "no-store" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(function (hist) {
      if (!hist || !Array.isArray(hist.snapshots) || hist.snapshots.length < 2) return;
      var el = document.getElementById("adv-chart");
      var meta = document.getElementById("adv-meta");
      if (meta) meta.textContent = hist.snapshots.length + " snapshots";
      if (!el) return;
      el.classList.remove("empty");
      el.innerHTML = renderAdvancementTable(hist.snapshots);
    });

  function renderAdvancementTable(snaps) {
    // Show top teams' P(reach final) across snapshots — delta = model drift.
    var teams = {};
    snaps.forEach(function (s) {
      Object.keys(s.probs || {}).forEach(function (t) { teams[t] = true; });
    });
    var latest = snaps[snaps.length - 1].probs || {};
    var ordered = Object.keys(teams).sort(function (a, b) {
      return (latest[b] || 0) - (latest[a] || 0);
    }).slice(0, 12);
    var head = "<tr><th>team</th>" + snaps.map(function (s) {
      return "<th>" + (s.label || s.date || "") + "</th>";
    }).join("") + "<th>&Delta;</th></tr>";
    var rows = ordered.map(function (t) {
      var cells = snaps.map(function (s) {
        var v = (s.probs || {})[t];
        return "<td>" + (v == null ? "&mdash;" : (v * 100).toFixed(1) + "%") + "</td>";
      }).join("");
      var first = (snaps[0].probs || {})[t], last = latest[t];
      var d = (first != null && last != null) ? ((last - first) * 100) : null;
      var dCell = d == null ? "&mdash;" :
        '<span style="color:' + (d >= 0 ? "#3fe08a" : "#e0603f") + '">' +
        (d >= 0 ? "+" : "") + d.toFixed(1) + "</span>";
      return "<tr><td>" + t + "</td>" + cells + "<td>" + dCell + "</td></tr>";
    }).join("");
    return '<table class="tbl"><thead>' + head + "</thead><tbody>" + rows + "</tbody></table>";
  }
})();
