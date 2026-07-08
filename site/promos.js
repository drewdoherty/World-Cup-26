/* World Cup Alpha — promos & offers page.
 *
 * Loads ./promos_data.json (cache-busted, produced by a Python CLI) and renders:
 *   1. sign-up offers table, with personal "Used (a1)" / "Used (a2)" checkboxes;
 *   2. opt-ins — ongoing promos derived from sites[].ongoing, with a1/a2
 *      opt-in toggles;
 *   3. watchlist — recurring promos worth keeping an eye on (title/desc/why);
 *   4. recent boost evaluations, most-recent first, flagging +EV rows;
 *   5. a compact per-site scrape-health strip (status dot + last-ok time).
 *
 * Checkbox / toggle state lives in browser localStorage (no backend) under
 * stable, slugified keys; it is restored on load and persisted on change.
 *
 * No frameworks, no build step, no CDN. Degrades to a clean "no data" state
 * when the feed is missing or empty.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------

  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // 0..1 probability -> "57.4%".
  function prob01(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (Number(v) * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function odds(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(2);
  }
  function signedPct(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    var n = Number(v) * 100;
    return (n >= 0 ? "+" : "") + n.toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function dateOnly(ts) {
    var t = String(ts || "");
    return t.length >= 10 ? t.slice(0, 10) : t;
  }
  function tsCompact(ts) {
    var t = String(ts || "");
    if (t.length >= 16) return t.slice(0, 16).replace("T", " ");
    return t;
  }
  // Stable slug for localStorage keys / dom-safe ids.
  function slug(v) {
    return String(v === null || v === undefined ? "" : v)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "x";
  }
  // Render a value or an em-dash for blanks.
  function orDash(v) {
    return (v === null || v === undefined || v === "") ? "—" : esc(v);
  }
  // Render an external link (or plain dash when no url).
  function linkOr(url, label) {
    var text = orDash(label === undefined ? url : label);
    if (!url) return text;
    return '<a class="promo-link" target="_blank" rel="noopener noreferrer" href="' +
      esc(url) + '">' + text + '</a>';
  }

  // ---- localStorage persistence ------------------------------------------

  var LS_PREFIX = "wca.promo.";

  function lsGet(key) {
    try { return window.localStorage.getItem(LS_PREFIX + key) === "1"; }
    catch (e) { return false; }
  }
  function lsSet(key, on) {
    try {
      if (on) window.localStorage.setItem(LS_PREFIX + key, "1");
      else window.localStorage.removeItem(LS_PREFIX + key);
    } catch (e) { /* private mode / disabled — degrade silently */ }
  }

  // Wire a checkbox so it restores from + writes to localStorage.
  function bindCheckbox(input, key) {
    input.checked = lsGet(key);
    input.addEventListener("change", function () {
      lsSet(key, input.checked);
    });
  }

  // Build a checkbox cell. `key` is the full localStorage sub-key.
  function chkCellHtml(key, dataLabel) {
    return '<td class="promo-chk-cell">' +
      '<span class="promo-chk">' +
        '<input type="checkbox" data-ls-key="' + esc(key) + '"' +
          ' aria-label="' + esc(dataLabel) + '">' +
      '</span>' +
    '</td>';
  }

  // After innerHTML is set, hydrate every [data-ls-key] checkbox in `root`.
  function hydrateCheckboxes(root) {
    var nodes = root.querySelectorAll("input[type=checkbox][data-ls-key]");
    Array.prototype.forEach.call(nodes, function (input) {
      bindCheckbox(input, input.getAttribute("data-ls-key"));
    });
  }

  // ---- 1. sign-up offers --------------------------------------------------

  function renderSignup(d) {
    var offers = d.signup_offers || [];
    $("signup-meta").textContent = offers.length
      ? offers.length + " offer" + (offers.length === 1 ? "" : "s")
      : "";
    if (!offers.length) {
      $("signup-offers").innerHTML =
        '<div class="empty">No sign-up offers tracked yet</div>';
      return;
    }

    var rows = offers.map(function (o) {
      var siteSlug = slug(o.site);
      return '<tr>' +
        '<td class="pos-time pos-match" title="' + esc(o.site) + '">' +
          orDash(o.site) + '</td>' +
        '<td class="pos-match" title="' + esc(o.offer) + '">' +
          linkOr(o.url, o.offer) + '</td>' +
        '<td class="num">' + orDash(o.min_odds) + '</td>' +
        '<td class="num">' + orDash(o.free_bet_value) + '</td>' +
        '<td class="num">' + orDash(o.expiry) + '</td>' +
        '<td>' + orDash(o.promo_code) + '</td>' +
        chkCellHtml("used.a1." + siteSlug, "Used on account 1 — " + (o.site || "")) +
        chkCellHtml("used.a2." + siteSlug, "Used on account 2 — " + (o.site || "")) +
      '</tr>';
    }).join("");

    $("signup-offers").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Site</th><th>Offer</th>' +
          '<th class="num">Min odds</th><th class="num">Free bet</th>' +
          '<th class="num">Expiry</th><th>Promo code</th>' +
          '<th class="promo-chk-cell">Used (a1)</th>' +
          '<th class="promo-chk-cell">Used (a2)</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';

    hydrateCheckboxes($("signup-offers"));
  }

  // ---- 2. opt-ins (ongoing promos) ---------------------------------------

  // Flatten sites[].ongoing into a single list, tagging each with its site.
  function gatherOngoing(d) {
    var out = [];
    (d.sites || []).forEach(function (s) {
      (s.ongoing || []).forEach(function (p) {
        out.push({
          site: s.name,
          title: p.title,
          description: p.description,
          url: p.url
        });
      });
    });
    return out;
  }

  function renderOptins(d) {
    var promos = gatherOngoing(d);
    $("optins-meta").textContent = promos.length
      ? promos.length + " promo" + (promos.length === 1 ? "" : "s")
      : "";
    if (!promos.length) {
      $("optins").innerHTML =
        '<div class="empty">No ongoing promos to opt into</div>';
      return;
    }

    var rows = promos.map(function (p) {
      // promoId = slug of site + title, so it stays stable across reloads.
      var promoId = slug(p.site) + "--" + slug(p.title);
      return '<tr>' +
        '<td class="pos-time pos-match" title="' + esc(p.site) + '">' +
          orDash(p.site) + '</td>' +
        '<td class="pos-match" title="' + esc(p.title) + '">' +
          linkOr(p.url, p.title) + '</td>' +
        '<td class="pos-sel" title="' + esc(p.description) + '">' +
          orDash(p.description) + '</td>' +
        chkCellHtml("optin.a1." + promoId, "Opted in on account 1 — " + (p.title || "")) +
        chkCellHtml("optin.a2." + promoId, "Opted in on account 2 — " + (p.title || "")) +
      '</tr>';
    }).join("");

    $("optins").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>Site</th><th>Promo</th><th>Details</th>' +
          '<th class="promo-chk-cell">Opt-in (a1)</th>' +
          '<th class="promo-chk-cell">Opt-in (a2)</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';

    hydrateCheckboxes($("optins"));
  }

  // ---- 3. watchlist -------------------------------------------------------

  function renderWatchlist(d) {
    var items = d.watchlist || [];
    $("watchlist-meta").textContent = items.length
      ? items.length + " item" + (items.length === 1 ? "" : "s")
      : "";
    if (!items.length) {
      $("watchlist").innerHTML =
        '<div class="empty">Nothing on the watchlist</div>';
      return;
    }
    var html = items.map(function (w) {
      return '<div class="promo-watch-item">' +
        '<div class="promo-watch-top">' +
          '<span class="promo-watch-title">' + orDash(w.title) + '</span>' +
          '<span class="promo-watch-site">' + orDash(w.site) + '</span>' +
        '</div>' +
        (w.description
          ? '<div class="promo-watch-desc">' + esc(w.description) + '</div>' : '') +
        (w.why
          ? '<div class="promo-watch-why">' + esc(w.why) + '</div>' : '') +
      '</div>';
    }).join("");
    $("watchlist").innerHTML = '<div class="promo-watch">' + html + '</div>';
  }

  // ---- 4. recent +EV boosts ----------------------------------------------

  function renderBoosts(d) {
    var raw = (d.boost_evals || []).slice();
    // Most recent first (ts is an ISO-ish string — lexicographic works).
    raw.sort(function (a, b) {
      return String(b.ts || "").localeCompare(String(a.ts || ""));
    });
    $("boosts-meta").textContent = raw.length
      ? raw.length + " eval" + (raw.length === 1 ? "" : "s")
      : "";
    if (!raw.length) {
      $("boosts").innerHTML =
        '<div class="empty">No boosts evaluated yet</div>';
      return;
    }

    var rows = raw.map(function (b) {
      var plus = !!b.is_plus_ev;
      var edgeCls = (b.edge !== null && b.edge !== undefined && !isNaN(b.edge))
        ? (Number(b.edge) >= 0 ? "pos" : "neg") : "";
      return '<tr class="promo-ev-row' + (plus ? " is-plus" : "") + '">' +
        '<td class="pos-time dim num">' + esc(tsCompact(b.ts)) + '</td>' +
        '<td class="promo-chk-cell"><span class="promo-ev' +
          (plus ? " is-plus" : "") + '" title="' +
          (plus ? "+EV" : "not +EV") + '"></span></td>' +
        '<td>' + orDash(b.site) + '</td>' +
        '<td class="pos-match" title="' + esc(b.fixture) + '">' +
          orDash(b.fixture) + '</td>' +
        '<td>' + orDash(b.market) + '</td>' +
        '<td class="pos-sel" title="' + esc(b.selection) + '">' +
          orDash(b.selection) + '</td>' +
        '<td class="num r">' + esc(odds(b.boosted_odds)) + '</td>' +
        '<td class="num r">' + esc(prob01(b.model_prob)) + '</td>' +
        '<td class="num r">' + esc(odds(b.fair_odds)) + '</td>' +
        '<td class="num r"><span class="' + edgeCls + '">' +
          esc(signedPct(b.edge)) + '</span></td>' +
        '<td class="dim">' + orDash(b.source) + '</td>' +
      '</tr>';
    }).join("");

    $("boosts").innerHTML =
      '<table class="pos-table">' +
        '<thead><tr>' +
          '<th>When</th><th class="promo-chk-cell">+EV</th>' +
          '<th>Site</th><th>Fixture</th><th>Market</th><th>Selection</th>' +
          '<th class="r">Boosted</th><th class="r">Model p</th>' +
          '<th class="r">Fair</th><th class="r">Edge</th><th>Source</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  // ---- 5. scrape health ---------------------------------------------------

  function healthClass(status) {
    var s = String(status || "").toLowerCase();
    if (s === "ok") return "ok";
    if (s === "blocked") return "blocked";
    if (s === "error") return "error";
    return "empty";
  }

  function renderHealth(d) {
    var items = d.scrape_health || [];
    if (!items.length) {
      $("scrape-health").innerHTML =
        '<div class="empty">No scrape health reported</div>';
      return;
    }
    var html = items.map(function (h) {
      var cls = healthClass(h.status);
      var status = (h.status || "—");
      var httpBit = (h.http_status !== null && h.http_status !== undefined && h.http_status !== "")
        ? " · HTTP " + esc(h.http_status) : "";
      var lastBit = h.last_ok_utc
        ? " · last ok " + esc(tsCompact(h.last_ok_utc)) : " · never ok";
      return '<div class="promo-health-item">' +
        '<span class="promo-health-dot ' + cls + '"></span>' +
        '<span class="promo-health-site">' + orDash(h.site) + '</span>' +
        '<span class="promo-health-meta">' + esc(status) + httpBit + lastBit + '</span>' +
      '</div>';
    }).join("");
    $("scrape-health").innerHTML = '<div class="promo-health">' + html + '</div>';
  }

  // ---- 6. manual-check ------------------------------------------------------

  function renderManualCheck(d) {
    var items = d.manual_check || [];
    $("manual-check-meta").textContent = items.length
      ? items.length + " source" + (items.length === 1 ? "" : "s")
      : "";
    if (!items.length) {
      $("manual-check").innerHTML =
        '<div class="empty">Nothing flagged for manual check</div>';
      return;
    }
    var html = items.map(function (m) {
      return '<div class="promo-watch-item">' +
        '<div class="promo-watch-top">' +
          '<span class="promo-watch-title">' +
            linkOr(m.url, m.site) +
          '</span>' +
          '<span class="promo-watch-site">' + orDash(m.site) + '</span>' +
        '</div>' +
        (m.reason
          ? '<div class="promo-watch-why">' + esc(m.reason) + '</div>' : '') +
      '</div>';
    }).join("");
    $("manual-check").innerHTML = '<div class="promo-watch">' + html + '</div>';
  }

  // ---- boot ---------------------------------------------------------------

  function renderFooter(d) {
    var gen = (d.meta && d.meta.generated) ? d.meta.generated : "";
    $("foot-gen").textContent = gen ? ("Generated " + gen) : "Generated —";
  }

  function showNoData(msg) {
    $("nodata-msg").textContent = msg || "NO DATA FEED";
    $("nodata").hidden = false;
  }

  function render(d) {
    renderSignup(d);
    renderOptins(d);
    renderWatchlist(d);
    renderBoosts(d);
    renderHealth(d);
    renderManualCheck(d);
    renderFooter(d);
  }

  function load() {
    var url = "./promos_data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        render(d);
      })
      .catch(function () {
        showNoData("PROMOS FEED UNAVAILABLE");
        render({
          meta: {}, sites: [], signup_offers: [], watchlist: [],
          manual_check: [], boost_evals: [], scrape_health: []
        });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
