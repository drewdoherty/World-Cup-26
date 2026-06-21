/* Advancement Tracking — real Polymarket probabilities by match number.
 * Loads tracking_buckets.json and renders Chart.js with bucket switching.
 */
(function () {
  "use strict";

  var chart = null;
  var data = {};
  var currentBucket = null;

  // Color palette for teams (consistent line styles)
  var colorMap = {
    France: { c: "#185FA5", d: [] },
    Spain: { c: "#E24B4A", d: [6, 4] },
    England: { c: "#7F77DD", d: [2, 3] },
    Argentina: { c: "#378ADD", d: [8, 3, 2, 3] },
    Brazil: { c: "#639922", d: [10, 4] },
    Canada: { c: "#378ADD", d: [] },
    Switzerland: { c: "#1D9E75", d: [6, 4] },
    Qatar: { c: "#BA7517", d: [10, 4] },
    "Czech Republic": { c: "#E24B4A", d: [6, 4] },
    "South Korea": { c: "#639922", d: [2, 3] },
    Iran: { c: "#7F77DD", d: [8, 3, 2, 3] },
    Australia: { c: "#1D9E75", d: [] },
    Colombia: { c: "#639922", d: [10, 4] },
    USA: { c: "#378ADD", d: [6, 4] },
    Mexico: { c: "#E24B4A", d: [] },
    Uruguay: { c: "#185FA5", d: [2, 3] },
  };

  function getColor(team) {
    return colorMap[team] || { c: "#888780", d: [] };
  }

  function loadData() {
    return fetch("./tracking_buckets.json")
      .then(function (r) { return r.ok ? r.json() : Promise.reject("fetch failed"); })
      .catch(function (e) {
        console.error("tracking_buckets.json load failed:", e);
        return {};
      });
  }

  function renderBucketNav(buckets) {
    var nav = document.getElementById("adv-bucket-nav");
    if (!nav) return;
    var html = "";
    Object.keys(buckets).forEach(function (key, idx) {
      var label = buckets[key].label;
      var active = idx === 0 ? " active" : "";
      html +=
        '<button class="adv-bucket-chip' +
        active +
        '" data-bucket="' +
        key +
        '" style="padding:6px 12px;border-radius:6px;border:1px solid #1d2731;background:#0a0e12;color:#cfe3d0;cursor:pointer;font-size:12px' +
        (active ? ";border-color:#4fd6e0;color:#4fd6e0" : "") +
        '">' +
        label +
        "</button>";
    });
    nav.innerHTML = html;

    document.querySelectorAll(".adv-bucket-chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        renderBucket(this.dataset.bucket);
        document.querySelectorAll(".adv-bucket-chip").forEach(function (b) {
          b.style.borderColor = b === btn ? "#4fd6e0" : "#1d2731";
          b.style.color = b === btn ? "#4fd6e0" : "#cfe3d0";
        });
      });
    });
  }

  function renderBucket(bucketKey) {
    if (!data[bucketKey]) return;
    currentBucket = bucketKey;
    var bucket = data[bucketKey];
    var teams = Object.keys(bucket.teams || {});

    if (chart) chart.destroy();

    var ctx = document.getElementById("advChart").getContext("2d");
    var datasets = [];

    // Determine x range
    var allPoints = [];
    teams.forEach(function (t) {
      (bucket.teams[t] || []).forEach(function (pt) {
        allPoints.push(pt.m);
      });
    });
    var maxMatch = Math.max.apply(null, allPoints.length > 0 ? allPoints : [0]);

    teams.forEach(function (team) {
      var col = getColor(team);
      var series = bucket.teams[team] || [];
      datasets.push({
        label: team,
        data: series.map(function (pt) {
          return { x: pt.m, y: pt.p };
        }),
        borderColor: col.c,
        backgroundColor: col.c,
        borderDash: col.d,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 4,
        tension: 0.2,
        fill: false,
      });
    });

    var isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var tick = isDark ? "#9bb0bd" : "#5f5e5a";
    var grid = isDark ? "rgba(255,255,255,.08)" : "rgba(0,0,0,.08)";

    chart = new Chart(ctx, {
      type: "line",
      data: { datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (c) {
                return "match #" + c[0].parsed.x;
              },
              label: function (c) {
                return c.dataset.label + ": " + c.parsed.y + "%";
              },
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: 0,
            max: Math.max(maxMatch + 1, 37),
            ticks: { color: tick, stepSize: 4 },
            grid: { color: grid },
            title: { display: true, text: "matches played", color: tick, font: { size: 11 } },
          },
          y: {
            min: 0,
            max: 100,
            ticks: { color: tick, callback: function (v) { return v + "%"; } },
            grid: { color: grid },
            title: { display: true, text: bucket.y_label, color: tick, font: { size: 11 } },
          },
        },
      },
    });
  }

  function init() {
    loadData().then(function (buckets) {
      data = buckets;
      if (Object.keys(buckets).length > 0) {
        renderBucketNav(buckets);
        renderBucket(Object.keys(buckets)[0]);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
