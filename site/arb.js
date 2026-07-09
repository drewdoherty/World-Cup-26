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

  // Market-kind badge — SAFETY-CRITICAL. Advancement (Polymarket moneyline)
  // pays if the team PROGRESSES, including extra-time / penalties. A 90-minute
  // 1X2 pays only on the 90'+stoppage result, so a KO tie that goes to ET/pens
  // is a DRAW for that market. The two are genuinely different bets now that
  // KOs have ET+pens; the badge colours (purple = advance, blue = 90') and
  // labels must make them impossible to confuse at a glance.
  function marketKindBadge(kind, label) {
    if (kind === "advancement") {
      return '<span class="arb-badge badge-mkt-adv" title="Polymarket moneyline — pays if the team progresses, including extra-time and penalties">'
        + esc(label || "ADVANCE · ET+PENS") + "</span>";
    }
    if (kind === "result_90") {
      return '<span class="arb-badge badge-mkt-90" title="1X2 — settles on the score after 90 minutes + stoppage only. A knockout tie that goes to ET/penalties is a DRAW for this market.">'
        + esc(label || "90-MIN 1X2") + "</span>";
    }
    return "";
  }

  // 90-minute 1X2 split for an advancement rec's next KO tie, from the team's
  // perspective: "win / draw / opp" with the DRAW emphasised (a KO draw at 90'
  // is a real, common outcome that sends the tie to ET/pens).
  function matchSplit(m) {
    if (!m || typeof m !== "object") return "—";
    var w = m.team_win, d = m.draw, o = m.opp_win;
    if (w == null && d == null && o == null) return "—";
    function p(v) { return v == null || isNaN(+v) ? "—" : Math.round(Number(v) * 100); }
    return '<span class="mk-split">'
      + '<span class="mk-w" title="team win (90′)">' + p(w) + "</span>"
      + '<span class="mk-sep">/</span>'
      + '<span class="mk-d" title="draw at 90′ — goes to ET/pens">' + p(d) + "</span>"
      + '<span class="mk-sep">/</span>'
      + '<span class="mk-o" title="opponent win (90′)">' + p(o) + "</span>"
      + "</span>";
  }

  function evClass(v) {
    if (v === null || v === undefined || isNaN(+v)) return "";
    return Number(v) > 0 ? "pos bold" : (Number(v) < 0 ? "neg" : "dim");
  }

  // --------------------------------------------------------------------------
  // One-click PLACE (localhost-only). SAFETY: this entire module is inert on
  // any non-localhost host, so a non-local copy of the page can never render or
  // fire a button. Guarded so a missing config or a non-localhost load never
  // throws.
  // --------------------------------------------------------------------------

  // True only on a genuine local dev load. Everything below short-circuits off
  // this — the render helpers emit nothing when it is false.
  var IS_LOCAL = (function () {
    try {
      var h = (location && location.hostname) || "";
      return h === "localhost" || h === "127.0.0.1" || h === "[::1]" || h === "::1";
    } catch (e) { return false; }
  })();

  var PLACE_ENDPOINT = "http://127.0.0.1:8010/place";
  var PLACE_TOKEN_KEY = "wcaPlaceToken";  // localStorage key for the shared secret

  function placeToken() {
    // The shared secret the user configured (matches server WCA_PLACE_TOKEN).
    // Prompt ONCE if unset; a cancelled prompt leaves it unset (button errors
    // rather than firing without a secret).
    try {
      var t = window.localStorage.getItem(PLACE_TOKEN_KEY);
      if (t && t.trim()) return t.trim();
      var entered = window.prompt(
        "One-time setup: paste the WCA place-server shared secret " +
        "(WCA_PLACE_TOKEN). Stored locally in this browser only.");
      if (entered && entered.trim()) {
        window.localStorage.setItem(PLACE_TOKEN_KEY, entered.trim());
        return entered.trim();
      }
    } catch (e) { /* localStorage/prompt unavailable — fall through */ }
    return null;
  }

  function makeNonce() {
    // Per-click idempotency token.
    var rnd;
    try { rnd = Math.random().toString(36).slice(2); } catch (e) { rnd = "" + Math.random(); }
    return "web-" + Date.now() + "-" + rnd;
  }

  // Render the PLACE cell for one row. Returns "" unless local AND the row is a
  // fireable Polymarket ADD (so 01/02 rows only get a live button when they
  // actually carry a polymarket-venue order).
  function placeCell(r) {
    if (!IS_LOCAL || !r) return "";
    var isPm = String(r.venue || "").toLowerCase() === "polymarket";
    var isAdd = String(r.action_label || "") === "ADD";
    if (!isPm || !isAdd || r.stale || !r.id) {
      return '<td data-label="Place"><span class="wca-place-note">&mdash;</span></td>';
    }
    return '<td data-label="Place">' +
      '<button type="button" class="wca-place-btn" data-place-id="' + esc(r.id) + '">Place</button>' +
      "</td>";
  }

  // POST one placement to the local server. Never throws to the caller.
  function postPlace(recId) {
    var token = placeToken();
    if (!token) {
      return Promise.resolve({ ok: false, message: "no place token configured (set the shared secret)" });
    }
    var body = JSON.stringify({ rec_id: recId, nonce: makeNonce() });
    return fetch(PLACE_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-WCA-Place-Token": token },
      body: body,
    })
      .then(function (resp) {
        return resp.json().catch(function () {
          return { ok: false, message: "server returned non-JSON (HTTP " + resp.status + ")" };
        });
      })
      .catch(function (e) {
        return { ok: false, message: "place server unreachable (" + (e && e.message || e) + ")" };
      });
  }

  // Delegated click handler wired once. Only active on localhost.
  function wirePlace() {
    if (!IS_LOCAL) return;
    document.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest(".wca-place-btn") : null;
      if (!btn || btn.disabled) return;
      var recId = btn.getAttribute("data-place-id");
      if (!recId) return;

      // Clear any prior inline error on this cell.
      var cell = btn.parentNode;
      var prevErr = cell && cell.querySelector ? cell.querySelector(".wca-place-err") : null;
      if (prevErr) prevErr.parentNode.removeChild(prevErr);

      btn.disabled = true;
      var restore = btn.textContent;
      btn.textContent = "…";

      postPlace(recId).then(function (res) {
        if (res && res.ok) {
          btn.textContent = res.dry_run ? "DRY ✓" : "PLACED ✓";
          btn.classList.add("placed");
          // keep disabled — prevents further clicks on a placed row
          // Refresh the KPI strip / feed so exposure reflects the new order.
          try { load(); } catch (e) { /* refresh best-effort */ }
        } else {
          btn.disabled = false;
          btn.textContent = restore;
          var msg = (res && res.message) || "place failed";
          var span = document.createElement("span");
          span.className = "wca-place-err";
          span.textContent = msg;
          if (cell) cell.appendChild(span);
        }
      });
    });
  }

  // Reveal the localhost-only PLACE <th> columns (hidden by default in HTML).
  function revealPlaceColumns() {
    if (!IS_LOCAL) return;
    var cols = document.querySelectorAll(".wca-place-col");
    for (var i = 0; i < cols.length; i++) { cols[i].hidden = false; }
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
      // 90-min result badge (older data without market_kind falls back to the
      // plain market label so nothing throws and the cell is never blank).
      var mktBadge = r.market_kind
        ? marketKindBadge(r.market_kind, r.market_label)
        : '<span class="arb-badge badge-market">' + esc(r.market || "") + "</span>";
      return "<tr>" +
        '<td data-label="Fixture">' + esc(r.fixture || "") + ' <span class="dim">' + esc(r.kickoff ? r.kickoff.slice(0, 10) : "") + "</span></td>" +
        '<td data-label="Market">' + mktBadge + "</td>" +
        '<td data-label="Selection"><strong>' + esc(r.team || r.selection || "") + "</strong></td>" +
        '<td data-label="Model %" class="r">' + pct(r.model_prob) + "</td>" +
        '<td data-label="Price" class="r num">' + priceStr(r.price) + ' <span class="dim" style="font-size:9px">' + esc(r.price_source === "devig_consensus" ? "devig" : "") + "</span>" + priceAgeTag + "</td>" +
        '<td data-label="Edge" class="r ' + evClass(r.edge) + '">' + signPct(r.edge) + "</td>" +
        '<td data-label="Net EV" class="r ' + evClass(r.ev_net) + '">' + signPct(r.ev_net) + "</td>" +
        '<td data-label="Stake" class="r">' + gbp(r.stake) + "</td>" +
        '<td data-label="Action">' + actionBadge(r.action_label) + "</td>" +
        '<td data-label="Promo">' + promoBadge(r.promo_status, r.promo && r.promo.name) + "</td>" +
        '<td data-label="Source">' + venueBadge(r.venue) + " " + staleBadge(r.stale) + " " + modelAgeTag + "</td>" +
        placeCell(r) +
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
        placeCell(r) +
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
      // Market-kind badge: advancement recs are Polymarket moneylines (pay on
      // progression, incl. ET+pens). Fall back gracefully for older data.
      var mktBadge = marketKindBadge(r.market_kind || "advancement", r.market_label);
      // Opponent + next-KO-tie 90' split (guard: older data has neither).
      var opp = r.opponent
        ? '<strong>' + esc(r.opponent) + "</strong>"
          + (r.match_round ? ' <span class="dim">' + esc(r.match_round) + "</span>" : "")
        : "—";
      var split = matchSplit(r.match_1x2);
      return "<tr>" +
        '<td data-label="Team"><strong>' + esc(r.team || "") + '</strong> <span class="dim">' + esc(r.group || "") + "</span></td>" +
        '<td data-label="Market">' + mktBadge + "</td>" +
        '<td data-label="Stage"><span class="arb-badge badge-market">' + esc(r.stage || "") + "</span></td>" +
        '<td data-label="Opponent (next KO)">' + opp + "</td>" +
        '<td data-label="90′ 1X2 (W/D/L)" class="r" title="model 90-minute result for the next KO tie — a draw here goes to ET/pens">' + split + "</td>" +
        '<td data-label="Model %" class="r">' + pct(r.model_prob) + "</td>" +
        '<td data-label="PM Price" class="r num">' + priceStr(r.pm_price) + "</td>" +
        '<td data-label="PM Fee" class="r dim">' + pct(r.pm_fee, 2) + "</td>" +
        '<td data-label="Net EV" class="r ' + evClass(r.ev_net) + '">' + signPct(r.ev_net) + "</td>" +
        '<td data-label="Stake ($)" class="r">' + usd(r.stake) + "</td>" +
        '<td data-label="Action">' + actionBadge(r.action_label) + "</td>" +
        '<td data-label="Source"><span class="arb-badge badge-pm">PM</span> ' + staleBadge(r.stale) + " " + modelAge + "</td>" +
        placeCell(r) +
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
        // Isolate every panel: one render throwing must not blank the page or
        // trip the global "FEED UNAVAILABLE" banner (that is reserved for an
        // actual fetch/parse failure, handled by .catch below).
        try { renderKpis(data); } catch (e) { console.error("renderKpis failed", e); }
        try { renderSingles(data.match_singles || []); } catch (e) { console.error("renderSingles failed", e); }
        try { renderProps(data.event_props || []); } catch (e) { console.error("renderProps failed", e); }
        try { renderAdv(data.advancement_futures || []); } catch (e) { console.error("renderAdv failed", e); }
        try { renderArbs(data.guaranteed_arbs || []); } catch (e) { console.error("renderArbs failed", e); }
        try { renderWithheld(data.withheld || []); } catch (e) { console.error("renderWithheld failed", e); }
        try { renderFooter(data); } catch (e) { console.error("renderFooter failed", e); }
      })
      .catch(function () {
        showNoData("BET RECS FEED UNAVAILABLE — run wca_betrecs.py to regenerate");
      });
  }

  wireFilters();
  wireDensity();
  wirePlace();            // no-op unless localhost

  function boot() {
    revealPlaceColumns(); // no-op unless localhost
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
