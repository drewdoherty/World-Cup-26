(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };
  var money = function (v) { return "$" + Number(v || 0).toFixed(2); };
  var pct = function (v) { return v == null ? "—" : (Number(v) * 100).toFixed(1) + "%"; };
  var cell = function (v, cls) { return "<td" + (cls ? " class='" + cls + "'" : "") + ">" + esc(v) + "</td>"; };

  function render(d) {
    var s = d.summary || {};
    $("shadow-generated").textContent = "generated " + (d.generated || "unknown");
    $("shadow-foot").textContent = "policy " + (d.policy_version || "unknown") + " · schema " + (d.schema_version || "?");
    var kpis = [
      ["OBSERVATIONS", s.observations || 0], ["DECISIONS", s.decisions || 0],
      ["ENTERED", s.entered || 0], ["ABSTAINED", s.abstained || 0],
      ["OPEN PAPER", money(s.open_stake_usd)], ["SETTLED P&L", money(s.settled_pl_usd)]
    ];
    $("shadow-kpis").innerHTML = kpis.map(function (x) { return "<div><span>" + esc(x[0]) + "</span><strong>" + esc(x[1]) + "</strong></div>"; }).join("");

    var positions = d.open_positions || [];
    $("shadow-positions").innerHTML = positions.map(function (p) {
      return "<tr>" + cell(p.venue) + cell((p.fixture || "") + " / " + p.selection) + cell(p.family) +
        cell(p.side, p.side === "NO" ? "neg" : "pos") + cell(pct(p.entry_price)) +
        cell(pct(p.forecast_prob)) + cell(money(p.stake_usd)) + cell(p.settlement_basis) + "</tr>";
    }).join("") || "<tr><td colspan='8'>No open paper positions.</td></tr>";

    var calibration = d.calibration || [];
    $("shadow-cal-empty").hidden = calibration.length > 0;
    $("shadow-calibration").innerHTML = calibration.map(function (r) {
      return "<tr>" + cell(r.venue) + cell(r.family) + cell(r.forecast_source) + cell(r.n) +
        cell(pct(r.forecast_rate)) + cell(pct(r.actual_rate)) + cell(Number(r.brier).toFixed(4)) +
        cell(Number(r.log_loss).toFixed(4)) + "</tr>";
    }).join("");

    var decisions = d.recent_decisions || [];
    var reasonCounts = {};
    decisions.forEach(function (x) { reasonCounts[x.reason] = (reasonCounts[x.reason] || 0) + 1; });
    $("shadow-decisions").innerHTML = Object.keys(reasonCounts).sort().map(function (k) {
      return "<div class='shadow-audit-row'><span>" + esc(k) + "</span><strong>" + reasonCounts[k] + "</strong></div>";
    }).join("") || "<div class='shadow-empty'>No decisions.</div>";

    var cross = d.cross_venue || [];
    var crossCounts = {};
    cross.forEach(function (x) { var k = x.action + " · " + x.reason; crossCounts[k] = (crossCounts[k] || 0) + 1; });
    $("shadow-cross").innerHTML = Object.keys(crossCounts).sort().map(function (k) {
      return "<div class='shadow-audit-row'><span>" + esc(k) + "</span><strong>" + crossCounts[k] + "</strong></div>";
    }).join("") || "<div class='shadow-empty'>No cross-venue observations.</div>";
  }

  fetch("./shadow_book.json?" + Date.now(), {cache:"no-store"}).then(function (r) {
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }).then(render).catch(function (e) {
    $("shadow-generated").textContent = "feed unavailable: " + e.message;
  });
})();
