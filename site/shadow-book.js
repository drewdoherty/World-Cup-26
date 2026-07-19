(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); };
  var money = function (v) { return "$" + Number(v || 0).toFixed(2); };
  var pct = function (v) { return v == null ? "—" : (Number(v) * 100).toFixed(1) + "%"; };
  var pp = function (v) { return v == null ? "—" : (Number(v) >= 0 ? "+" : "") + (Number(v) * 100).toFixed(1) + "pp"; };
  var cell = function (v, cls) { return "<td" + (cls ? " class='" + cls + "'" : "") + ">" + esc(v) + "</td>"; };
  var allPositions = [];
  var filtersBound = false;
  var sortButtonsBound = false;
  var sortKey = "current";
  var sortDir = -1;
  var activeBucket = null;

  function numberValue(id) {
    var el = $(id);
    if (!el || el.value === "") return null;
    var n = Number(el.value);
    return isFinite(n) ? n : null;
  }

  function between(value, minId, maxId, scale) {
    if (value == null) return false;
    var min = numberValue(minId);
    var max = numberValue(maxId);
    var n = Number(value) * (scale || 1);
    return (min == null || n >= min) && (max == null || n <= max);
  }

  function checked(id) { return !!($(id) && $(id).checked); }

  function bucketNames(p) {
    var names = [];
    var family = String(p.family || "other");
    var market = Number(p.current_outcome_market_prob);
    var taker = Number(p.taker_edge_at_ask);
    var passive = Number(p.passive_edge_at_bid_before_fill);
    if (family === "1x2") names.push("moneyline");
    if (family !== "1x2" && family !== "advance") names.push("event market");
    if (family === "scorer_prop") names.push("scorer props");
    if (p.exploration || p.forecast_source !== "production_model") names.push("market-only");
    else names.push("model-backed");
    if (p.current_quote_basis === "independent_token_book") names.push("independent quotes");
    if (p.execution_style === "marketable_limit") names.push("marketable limits");
    else names.push("legacy entries");
    if (market < 0.10) names.push("price <10%");
    else if (market < 0.25) names.push("price 10–25%");
    else if (market < 0.50) names.push("price 25–50%");
    else if (market < 0.75) names.push("price 50–75%");
    else if (market < 0.90) names.push("price 75–90%");
    else names.push("price >90%");
    if (taker > 0) names.push("taker +EV");
    if (passive > 0) names.push("passive +EV");
    if (taker > 0 && passive > 0) names.push("both +EV");
    if (taker < 0) names.push("taker -EV");
    if (market >= 0.20 && market <= 0.80 && taker > 0) names.push("mid +EV");
    if (market < 0.20 && taker > 0) names.push("longshot +EV");
    if (market > 0.80 && taker > 0) names.push("short-price +EV");
    if (family === "1x2" && taker > 0) names.push("moneyline +EV");
    return names;
  }

  function renderBuckets() {
    var counts = {};
    allPositions.forEach(function (p) {
      bucketNames(p).forEach(function (name) { counts[name] = (counts[name] || 0) + 1; });
    });
    var preferred = ["moneyline +EV", "mid +EV", "longshot +EV", "short-price +EV",
      "taker +EV", "passive +EV", "both +EV", "taker -EV", "model-backed", "market-only",
      "independent quotes", "marketable limits", "legacy entries", "event market", "scorer props",
      "price <10%", "price 10–25%", "price 25–50%", "price 50–75%", "price 75–90%", "price >90%"];
    $("shadow-buckets-list").innerHTML = preferred.filter(function (name) {
      return counts[name] != null;
    }).map(function (name) {
      var selected = activeBucket === name ? " selected" : "";
      return "<button type='button' class='shadow-bucket" + selected + "' data-bucket='" + esc(name) + "'>" +
        esc(name) + " <strong>" + counts[name] + "</strong></button>";
    }).join("");
    Array.prototype.forEach.call($("shadow-buckets-list").querySelectorAll("[data-bucket]"), function (button) {
      button.addEventListener("click", function () {
        activeBucket = activeBucket === button.getAttribute("data-bucket") ? null : button.getAttribute("data-bucket");
        renderBuckets();
        renderPositions();
      });
    });
  }

  function matchesFilter(p) {
    var taker = Number(p.taker_edge_at_ask);
    var passive = Number(p.passive_edge_at_bid_before_fill);
    if (checked("filter-taker-positive") && !(taker > 0)) return false;
    if (checked("filter-passive-positive") && !(passive > 0)) return false;
    if (checked("filter-taker-negative") && !(taker < 0)) return false;
    if (checked("filter-independent") && p.current_quote_basis !== "independent_token_book") return false;
    if (checked("filter-marketable") && p.execution_style !== "marketable_limit") return false;
    if (checked("filter-scorers") && p.family !== "scorer_prop") return false;
    if (activeBucket && bucketNames(p).indexOf(activeBucket) === -1) return false;
    if (!between(p.current_outcome_market_prob, "range-current-min", "range-current-max", 100)) return false;
    if (!between(p.current_side_ask, "range-ask-min", "range-ask-max", 100)) return false;
    if (!between(p.taker_edge_at_ask, "range-taker-min", "range-taker-max", 100)) return false;
    if (!between(p.passive_edge_at_bid_before_fill, "range-passive-min", "range-passive-max", 100)) return false;
    if (!between(p.stake_usd, "range-stake-min", "range-stake-max", 1)) return false;
    return true;
  }

  function renderPositions() {
    var visible = allPositions.filter(matchesFilter);
    visible.sort(function (a, b) {
      var av = sortValue(a, sortKey), bv = sortValue(b, sortKey);
      if (av < bv) return -1 * sortDir;
      if (av > bv) return 1 * sortDir;
      return Number(a.id || 0) - Number(b.id || 0);
    });
    $("shadow-filter-status").textContent = visible.length + " of " + allPositions.length + " rows shown" +
      (activeBucket ? " · " + activeBucket : "");
    $("shadow-positions").innerHTML = visible.map(function (p) {
      var source = p.exploration ? "market-only" : (p.forecast_source || "unknown");
      return "<tr>" + cell(p.venue) + cell((p.fixture || "") + " / " + p.selection) + cell(p.family) +
        cell(source, p.exploration ? "dim" : "pos") +
        cell("BUY " + p.side, p.side === "NO" ? "neg" : "pos") +
        cell(pct(p.current_outcome_market_prob)) + cell(pct(p.current_side_bid)) +
        cell(pct(p.current_side_ask)) + cell(pct(p.synthetic_parity_reference), "dim") +
        cell(pp(p.no_vs_parity), "dim") + cell(pct(p.cross_both_asks), "dim") +
        cell(pct(p.entry_cost)) +
        cell(p.execution_style || "—") + cell(pct(p.forecast_prob)) +
        cell(pp(p.taker_edge_at_ask)) + cell(pp(p.passive_edge_at_bid_before_fill)) +
        cell(money(p.stake_usd)) +
        cell(p.settlement_basis) + "</tr>";
    }).join("") || "<tr><td colspan='18'>No rows match the current filters.</td></tr>";
  }

  function sortValue(p, key) {
    var map = {
      venue: p.venue, fixture: (p.fixture || "") + " / " + (p.selection || ""),
      family: p.family, source: p.exploration ? "market-only" : (p.forecast_source || "unknown"),
      side: p.side, current: p.current_outcome_market_prob,
      bid: p.current_side_bid, ask: p.current_side_ask, parity: p.synthetic_parity_reference,
      no_gap: p.no_vs_parity, pair_asks: p.cross_both_asks, entry_cost: p.entry_cost,
      execution: p.execution_style, forecast: p.forecast_prob, taker: p.taker_edge_at_ask,
      passive: p.passive_edge_at_bid_before_fill, stake: p.stake_usd, basis: p.settlement_basis
    };
    var value = map[key];
    if (value == null) return typeof value === "string" ? "" : -Infinity;
    return typeof value === "number" ? value : String(value).toLowerCase();
  }

  function bindFilters() {
    if (filtersBound) return;
    filtersBound = true;
    var controls = document.querySelectorAll(".shadow-filters input");
    Array.prototype.forEach.call(controls, function (el) {
      el.addEventListener("input", function () {
        $("shadow-filter-status").textContent = "Filters changed — press Apply filters";
      });
      el.addEventListener("change", function () {
        $("shadow-filter-status").textContent = "Filters changed — press Apply filters";
      });
    });
    $("shadow-filter-apply").addEventListener("click", renderPositions);
    $("shadow-filter-reset").addEventListener("click", function () {
      Array.prototype.forEach.call(controls, function (el) {
        if (el.type === "checkbox") el.checked = false;
        else el.value = "";
      });
      activeBucket = null;
      renderBuckets();
      renderPositions();
    });
    if (!sortButtonsBound) {
      sortButtonsBound = true;
      Array.prototype.forEach.call(document.querySelectorAll(".shadow-sort"), function (button) {
        button.addEventListener("click", function () {
          var next = button.getAttribute("data-sort");
          if (sortKey === next) sortDir *= -1;
          else { sortKey = next; sortDir = 1; }
          Array.prototype.forEach.call(document.querySelectorAll(".shadow-sort"), function (other) {
            other.classList.remove("active");
            other.removeAttribute("aria-sort");
          });
          button.classList.add("active");
          button.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
          renderPositions();
        });
      });
    }
  }

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

    allPositions = (d.open_positions || []).slice().sort(function (a, b) {
      return Number(b.current_outcome_market_prob || 0) - Number(a.current_outcome_market_prob || 0);
    });
    bindFilters();
    renderBuckets();
    renderPositions();

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
