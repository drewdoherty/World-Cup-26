(function () {
  "use strict";

  // --------------------------------------------------------------------------
  // DOM helpers
  // --------------------------------------------------------------------------

  var $ = function (id) { return document.getElementById(id); };

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function pct(v, dp) {
    if (v === null || v === undefined || isNaN(+v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }

  function signPct(v, dp) {
    if (v === null || v === undefined || isNaN(+v)) return "—";
    var n = (Number(v) * 100).toFixed(dp === undefined ? 1 : dp);
    return (Number(v) > 0 ? "+" : "") + n + "%";
  }

  function gbp(v) { return v == null || isNaN(+v) ? "—" : "£" + Number(v).toFixed(2); }
  function usd(v) { return v == null || isNaN(+v) ? "—" : "$" + Number(v).toFixed(2); }
  function currency(v, cur) {
    if (v == null || isNaN(+v)) return "—";
    return cur === "USD" ? usd(v) : gbp(v);
  }
  function priceStr(v) { return v == null || isNaN(+v) ? "—" : Number(v).toFixed(3); }
  function num(v, dp) { return v == null || isNaN(+v) ? "—" : Number(v).toFixed(dp === undefined ? 2 : dp); }

  function ageFmt(secs) {
    if (secs === null || secs === undefined || isNaN(+secs)) return null;
    var s = +secs;
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return Math.round(s / 3600) + "h";
  }

  function ageTag(secs, staleThreshSecs) {
    var label = ageFmt(secs);
    if (!label) return "";
    var isStale = staleThreshSecs != null && secs > staleThreshSecs;
    return '<span class="age-tag' + (isStale ? " stale" : "") + '" title="data age">' + esc(label) + " ago</span>";
  }

  function venueBadge(v) {
    if (!v) return "";
    var lv = v.toLowerCase();
    if (lv.indexOf("poly") >= 0) return '<span class="arb-badge badge-pm">PM</span>';
    if (lv.indexOf("kalshi") >= 0) return '<span class="arb-badge badge-kal">KAL</span>';
    if (lv.indexOf("smarket") >= 0) return '<span class="arb-badge badge-sb">SMK</span>';
    if (lv.indexOf("betfair") >= 0) return '<span class="arb-badge badge-sb">BFX</span>';
    return '<span class="arb-badge badge-mod">' + esc(v.toUpperCase().slice(0, 4)) + "</span>";
  }

  function actionBadge(label) {
    if (!label) return "";
    var lbl = label.toUpperCase();
    if (lbl === "ADD") return '<span class="arb-badge badge-add">ADD</span>';
    if (lbl === "DIVERSIFY") return '<span class="arb-badge badge-div">DIV</span>';
    if (lbl === "HEDGE") return '<span class="arb-badge badge-hedge">HED</span>';
    return '<span class="arb-badge badge-mod">' + esc(lbl) + "</span>";
  }

  function promoBadge(status, name) {
    if (!status || status === "none") return "";
    if (status === "PROMO CHECK REQUIRED") {
      return '<span class="arb-badge badge-pc-req" title="' + esc(name || "") + '">PROMO?</span>';
    }
    if (status === "applied") {
      return '<span class="arb-badge badge-promo" title="' + esc(name || "") + '">PROMO</span>';
    }
    return "";
  }

  function staleBadge(stale) {
    return stale ? '<span class="arb-badge badge-stale">STALE</span>' : "";
  }

  function evClass(v) {
    if (v === null || v === undefined || isNaN(+v)) return "";
    return Number(v) > 0 ? "pos bold" : (Number(v) < 0 ? "neg" : "dim");
  }

  // --------------------------------------------------------------------------
  // Fetch
  // --------------------------------------------------------------------------

  function fetchJson(url) {
    return fetch(url + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  // --------------------------------------------------------------------------
  // Filter state
  // --------------------------------------------------------------------------

  var filters = {
    fixture: "",
    market: "",
    venue: "",
    currency: "",
    action: null,        // "ADD" | "HEDGE" | null
    posev: false,        // only +EV
    stale: false,        // show stale (default: hide)
  };

  function filterRow(r) {
    if (!r) return false;

    // Text fixture/team filter
    if (filters.fixture) {
      var q = filters.fixture.toLowerCase();
      var fix = (r.fixture || r.team || "").toLowerCase();
      var team = (r.team || "").toLowerCase();
      if (fix.indexOf(q) < 0 && team.indexOf(q) < 0) return false;
    }

    // Market
    if (filters.market) {
      var mkt = (r.market || "").toLowerCase();
      var filterMkt = filters.market.toLowerCase();
      if (filterMkt === "arb") {
        if (!r.guaranteed_pct) return false;
      } else if (filterMkt === "advancement") {
        if (mkt !== "advancement") return false;
      } else {
        if (mkt.indexOf(filterMkt) < 0) return false;
      }
    }

    // Venue
    if (filters.venue) {
      var ven = (r.venue || "").toLowerCase();
      if (ven.indexOf(filters.venue.toLowerCase()) < 0) return false;
    }

    // Currency
    if (filters.currency) {
      if ((r.currency || "") !== filters.currency) return false;
    }

    // Action label
    if (filters.action) {
      if ((r.action_label || "").toUpperCase() !== filters.action) return false;
    }

    // +EV only
    if (filters.posev) {
      if (!r.ev_net || r.ev_net <= 0) return false;
    }

    // Stale filter: by default hide stale rows; if stale chip active, show all
    if (!filters.stale && r.stale) return false;

    return true;
  }

  // --------------------------------------------------------------------------
  // Renderers
  // --------------------------------------------------------------------------

  function renderSingles(rows) {
    var visible = (rows || []).filter(filterRow);
    $("cnt-singles").textContent = visible.length + " rec" + (visible.length === 1 ? "" : "s");
    var tbody = $("body-singles");
    var empty = $("empty-singles");

    if (!visible.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var html = visible.map(function (r) {
      var ages = r.ages || {};
      var priceAgeTag = ageTag(ages.price_secs, 7200);
      var modelAgeTag = ageTag(ages.model_secs, 86400);
      return "<tr>" +
        '<td data-label="Fixture">' + esc(r.fixture || "") + ' <span class="dim">' + esc(r.kickoff ? r.kickoff.slice(0, 10) : "") + "</span></td>" +
        '<td data-label="Market"><span class="arb-badge badge-market">' + esc(r.market || "") + "</span></td>" +
        '<td data-label="Selection"><strong>' + esc(r.team || r.selection || "") + "</strong></td>" +
        '<td data-label="Model %" class="r">' + pct(r.model_prob) + "</td>" +
        '<td data-label="Price" class="r num">' + priceStr(r.price) + ' <span class="dim" style="font-size:9px">' + esc(r.price_source === "devig_consensus" ? "devig" : "") + "</span>" + priceAgeTag + "</td>" +
        '<td data-label="Edge" class="r ' + evClass(r.edge) + '">' + signPct(r.edge) + "</td>" +
        '<td data-label="Net EV" class="r ' + evClass(r.ev_net) + '">' + signPct(r.ev_net) + "</td>" +
        '<td data-label="Stake" class="r">' + gbp(r.stake) + "</td>" +
        '<td data-label="Action">' + actionBadge(r.action_label) + "</td>" +
        '<td data-label="Promo">' + promoBadge(r.promo_status, r.promo && r.promo.name) + "</td>" +
        '<td data-label="Source">' + venueBadge(r.venue) + " " + staleBadge(r.stale) + " " + modelAgeTag + "</td>" +
        "</tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  function renderProps(rows) {
    var visible = (rows || []).filter(filterRow);
    $("cnt-props").textContent = visible.length + " rec" + (visible.length === 1 ? "" : "s");
    var tbody = $("body-props");
    var empty = $("empty-props");

    if (!visible.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var html = visible.map(function (r) {
      return "<tr>" +
        '<td data-label="Fixture">' + esc(r.fixture || "") + "</td>" +
        '<td data-label="Market"><span class="arb-badge badge-market">' + esc(r.market || "") + "</span></td>" +
        '<td data-label="Selection">' + esc(r.selection || "") + "</td>" +
        '<td data-label="Model %" class="r">' + pct(r.model_prob) + "</td>" +
        '<td data-label="Price" class="r num">' + priceStr(r.price) + "</td>" +
        '<td data-label="Net EV" class="r ' + evClass(r.ev_net) + '">' + signPct(r.ev_net) + "</td>" +
        '<td data-label="Stake" class="r">' + currency(r.stake, r.currency) + "</td>" +
        '<td data-label="Action">' + actionBadge(r.action_label) + "</td>" +
        '<td data-label="Source">' + venueBadge(r.venue) + " " + staleBadge(r.stale) + "</td>" +
        "</tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  function renderAdv(rows) {
    var visible = (rows || []).filter(filterRow);
    $("cnt-adv").textContent = visible.length + " rec" + (visible.length === 1 ? "" : "s");
    var tbody = $("body-adv");
    var empty = $("empty-adv");

    if (!visible.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var html = visible.map(function (r) {
      var ages = r.ages || {};
      var modelAge = ageTag(ages.model_secs, 86400);
      return "<tr>" +
        '<td data-label="Team"><strong>' + esc(r.team || "") + '</strong> <span class="dim">' + esc(r.group || "") + "</span></td>" +
        '<td data-label="Stage"><span class="arb-badge badge-market">' + esc(r.stage || "") + "</span></td>" +
        '<td data-label="Model %" class="r">' + pct(r.model_prob) + "</td>" +
        '<td data-label="PM Price" class="r num">' + priceStr(r.pm_price) + "</td>" +
        '<td data-label="PM Fee" class="r dim">' + pct(r.pm_fee, 2) + "</td>" +
        '<td data-label="Net EV" class="r ' + evClass(r.ev_net) + '">' + signPct(r.ev_net) + "</td>" +
        '<td data-label="Stake ($)" class="r">' + usd(r.stake) + "</td>" +
        '<td data-label="Action">' + actionBadge(r.action_label) + "</td>" +
        '<td data-label="Source"><span class="arb-badge badge-pm">PM</span> ' + staleBadge(r.stale) + " " + modelAge + "</td>" +
        "</tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  function renderArbs(rows) {
    var visible = (rows || []).filter(filterRow);
    $("cnt-arbs").textContent = visible.length + " arb" + (visible.length === 1 ? "" : "s");
    var tbody = $("body-arbs");
    var empty = $("empty-arbs");

    if (!visible.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var html = visible.map(function (a) {
      var legs = (a.legs || []).map(function (l) {
        return venueBadge(l.venue) + " " + esc(l.side || "") + " @ " + priceStr(l.price) + " (" + currency(l.stake, l.currency) + ")";
      }).join(" &middot; ");
      return "<tr>" +
        '<td data-label="Fixture">' + esc(a.fixture || a.market || "") + ' <span class="dim">' + esc(a.selection || "") + "</span></td>" +
        '<td data-label="Legs" style="font-size:11px">' + legs + "</td>" +
        '<td data-label="Fee-adj edge" class="r ' + evClass(a.fee_adj_edge) + '">' + signPct(a.fee_adj_edge) + "</td>" +
        '<td data-label="Guaranteed %" class="r pos bold">' + pct(a.guaranteed_pct) + "</td>" +
        '<td data-label="Liquidity"><span class="dim" style="font-size:10px">' + esc(a.liquidity_note || "quoted") + "</span></td>" +
        "</tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  function renderWithheld(rows) {
    var all = (rows || []);
    $("cnt-withheld").textContent = all.length + " row" + (all.length === 1 ? "" : "s");
    var tbody = $("body-withheld");
    var empty = $("empty-withheld");

    if (!all.length) {
      tbody.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var html = all.map(function (r) {
      return "<tr>" +
        '<td data-label="Fixture">' + esc(r.fixture || r.team || "") + ' <span class="dim">' + esc(r.market || "") + "</span></td>" +
        '<td data-label="Selection">' + esc(r.selection || r.team || "") + "</td>" +
        '<td data-label="Model %" class="r dim">' + pct(r.model_prob) + "</td>" +
        '<td data-label="Edge" class="r dim">' + signPct(r.edge) + "</td>" +
        '<td data-label="Why withheld" class="warn" style="font-size:10px">' + esc(r.withheld_reason || "") + "</td>" +
        '<td data-label="Stale">' + staleBadge(r.stale) + "</td>" +
        "</tr>";
    }).join("");
    tbody.innerHTML = html;
  }

  // --------------------------------------------------------------------------
  // KPI strip
  // --------------------------------------------------------------------------

  function renderKpis(data) {
    var meta = data.meta || {};
    var sb = meta.sportsbook_pool || {};
    var pm = meta.pm_pool || {};
    var exp = meta.open_exposure || {};
    var ages = meta.ages || {};

    // Freshness
    var modelAge = ageFmt(ages.model_secs);
    $("kpi-freshness").textContent = modelAge ? modelAge + " old" : "—";
    if (ages.model_secs > 86400) $("kpi-freshness").classList.add("warn");

    // Bankrolls
    $("kpi-bankroll").textContent = sb.bankroll ? "£" + Number(sb.bankroll).toLocaleString() : "—";
    $("kpi-pm-bankroll").textContent = pm.bankroll ? "$" + Number(pm.bankroll).toLocaleString() : "—";

    // Open exposure
    var wc = exp.worst_case;
    $("kpi-exposure").textContent = wc != null ? "£" + Number(wc).toFixed(0) : "—";
    if (wc != null && wc < -200) $("kpi-exposure").className = "arb-kpi-value neg";

    // Counts
    $("kpi-actionable").textContent = meta.actionable_count || 0;
    $("kpi-withheld").textContent = meta.withheld_count || 0;

    // Rung / CLV
    var rungStr = "Rung " + (sb.rung !== undefined ? sb.rung : "—");
    if (sb.clv_to_date != null) rungStr += " / CLV " + (Number(sb.clv_to_date) >= 0 ? "+" : "") + (Number(sb.clv_to_date) * 100).toFixed(1) + "%";
    $("kpi-rung").textContent = rungStr;

    // Section freshness hints
    var mAge = ages.model_secs;
    var aAge = ages.advancement_secs;
    if ($("age-singles") && mAge) $("age-singles").textContent = "model " + ageFmt(mAge) + " ago";
    if ($("age-adv") && aAge) $("age-adv").textContent = "adv " + ageFmt(aAge) + " ago";
  }

  function renderFooter(data) {
    var gen = (data.meta || {}).generated;
    if ($("foot-gen") && gen) $("foot-gen").textContent = "generated " + gen;
  }

  // --------------------------------------------------------------------------
  // Filter wiring
  // --------------------------------------------------------------------------

  var _recs = null;

  function applyFilters() {
    if (!_recs) return;
    renderSingles(_recs.match_singles || []);
    renderProps(_recs.event_props || []);
    renderAdv(_recs.advancement_futures || []);
    renderArbs(_recs.guaranteed_arbs || []);
    // Withheld is not filtered by most controls but respects text search
    renderWithheld(_recs.withheld || []);
  }

  function wireFilters() {
    $("f-fixture").addEventListener("input", function () {
      filters.fixture = this.value.trim();
      applyFilters();
    });
    $("f-market").addEventListener("change", function () {
      filters.market = this.value;
      applyFilters();
    });
    $("f-venue").addEventListener("change", function () {
      filters.venue = this.value;
      applyFilters();
    });
    $("f-currency").addEventListener("change", function () {
      filters.currency = this.value;
      applyFilters();
    });

    // Chip toggles
    ["f-chip-add", "f-chip-hedge", "f-chip-posev", "f-chip-stale"].forEach(function (id) {
      var btn = $(id);
      if (!btn) return;
      btn.addEventListener("click", function () {
        var active = this.getAttribute("data-active") === "true";
        this.setAttribute("data-active", active ? "false" : "true");
        var filterType = this.getAttribute("data-filter");
        var val = this.getAttribute("data-val");
        if (filterType === "action") {
          filters.action = active ? null : val;
          // Deactivate sibling chips in action group
          if (!active) {
            ["f-chip-add", "f-chip-hedge"].forEach(function (otherId) {
              if (otherId !== id) {
                $(otherId) && $(otherId).setAttribute("data-active", "false");
              }
            });
          }
        } else if (filterType === "posev") {
          filters.posev = !active;
        } else if (filterType === "stale") {
          filters.stale = !active;
        }
        applyFilters();
      });
    });

    // Keyboard: Enter on fixture input
    $("f-fixture").addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        this.value = "";
        filters.fixture = "";
        applyFilters();
      }
    });
  }

  // --------------------------------------------------------------------------
  // Density toggle
  // --------------------------------------------------------------------------

  var _compact = false;
  function wireDensity() {
    var btn = $("density-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      _compact = !_compact;
      document.body.classList.toggle("density-compact", _compact);
      btn.textContent = _compact ? "Comfort" : "Compact";
    });
  }

  // --------------------------------------------------------------------------
  // Main load
  // --------------------------------------------------------------------------

  function showNoData(msg) {
    var el = $("arb-nodata");
    if (!el) return;
    el.hidden = false;
    var msgEl = $("arb-nodata-msg");
    if (msgEl) msgEl.textContent = msg || "BET RECS FEED UNAVAILABLE";
  }

  function load() {
    fetchJson("./bet_recs.json")
      .then(function (data) {
        _recs = data;
        renderKpis(data);
        renderSingles(data.match_singles || []);
        renderProps(data.event_props || []);
        renderAdv(data.advancement_futures || []);
        renderArbs(data.guaranteed_arbs || []);
        renderWithheld(data.withheld || []);
        renderFooter(data);
      })
      .catch(function () {
        showNoData("BET RECS FEED UNAVAILABLE — run wca_betrecs.py to regenerate");
      });
  }

  wireFilters();
  wireDensity();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
