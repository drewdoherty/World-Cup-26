/* World Cup Alpha — trading-terminal front-end.
 *
 * Loads ./data.json (cache-busted) and renders the ticker, venues, positions
 * and predictions panels. Everything degrades to a clean "no data" state if
 * the feed is missing or malformed. No external assets, no CDN.
 *
 * The ONLY optional external call is an opportunistic enrichment fetch to
 * gamma-api.polymarket.com (live market prices); it is fully guarded and the
 * page works identically when that request is blocked or fails.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  // ---- formatting helpers -------------------------------------------------

  var SYM = { GBP: "£", USD: "$", EUR: "€" };
  function sym(ccy) { return SYM[ccy] || "£"; }

  function money(n, ccy) {
    if (n === null || n === undefined || isNaN(n)) return sym(ccy) + "0.00";
    var sign = n < 0 ? "-" : "";
    return sign + sym(ccy) + Math.abs(n).toLocaleString("en-GB", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  function signedMoney(n, ccy) {
    if (n === null || n === undefined || isNaN(n)) n = 0;
    var sign = n < 0 ? "-" : "+";
    return sign + sym(ccy) + Math.abs(n).toLocaleString("en-GB", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  // Render a per-currency map {GBP: {...}, USD: {...}} field as "£a + $b".
  // Currencies are NEVER summed — £ and $ are different units.
  function moneyByCcy(byCcy, field, signed) {
    var parts = [];
    ["GBP", "USD", "EUR"].forEach(function (ccy) {
      var blk = (byCcy || {})[ccy];
      if (!blk) return;
      var v = Number(blk[field] || 0);
      if (v === 0 && field !== "settled_pl") return;
      parts.push(signed ? signedMoney(v, ccy) : money(v, ccy));
    });
    return parts.length ? parts.join(" + ") : money(0);
  }
  // HTML variant of moneyByCcy for SIGNED P&L: each currency component is
  // coloured by ITS OWN sign. A losing $ leg must never be painted green just
  // because the aggregate (or the £ leg) happens to be positive.
  function moneyByCcyHTML(byCcy, field) {
    var parts = [];
    ["GBP", "USD", "EUR"].forEach(function (ccy) {
      var blk = (byCcy || {})[ccy];
      if (!blk) return;
      var v = Number(blk[field] || 0);
      if (v === 0 && field !== "settled_pl") return;
      parts.push('<span class="' + (v < 0 ? "neg" : "pos") + '">' +
        esc(signedMoney(v, ccy)) + "</span>");
    });
    return parts.length ? parts.join(" + ")
      : '<span class="pos">' + esc(signedMoney(0)) + "</span>";
  }
  function pct(n, dp) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return (n * 100).toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function dash(v) {
    return (v === null || v === undefined || v === "") ? "—" : v;
  }
  // Format an already-percentage number (0..100, as emitted by the card
  // scoreline parser) to one decimal place; null/undefined/NaN -> em-dash.
  function pct1(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(1) + "%";
  }
  function num(v, dp) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toFixed(dp === undefined ? 2 : dp);
  }
  // Signed percentage with an explicit + on positives (for edge columns).
  function pctSigned(n, dp) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    var s = (n * 100).toFixed(dp === undefined ? 1 : dp);
    return (n >= 0 ? "+" : "") + s + "%";
  }
  // Compact a verbose market string for the dense table column. Long
  // descriptive markets (bet-builders, prop questions) collapse to a short tag;
  // the full text stays available via the cell's title attribute.
  function marketShort(m) {
    if (!m) return "—";
    var s = String(m).trim();
    var map = {
      h2h: "1X2", match_odds: "1X2", "match odds": "1X2",
      outright_golden_boot: "Golden Boot", golden_boot: "Golden Boot",
      outright_winner: "Winner", BETBUILDER: "Bet Builder",
      polymarket: "PM", over_under: "O/U", totals: "O/U",
      scoreline: "Score", correct_score: "Score", draw_no_bet: "DNB",
      btts: "BTTS", double_chance: "Dbl Chance",
      player_goal_scorer_anytime: "ATGS", player_first_goal_scorer: "FGS",
      player_shots_on_target: "Shots OT"
    };
    if (map[s]) return map[s];
    var key = s.toLowerCase();
    if (map[key]) return map[key];
    // Advancement / outright prop questions -> "<Team> <stage>" (e.g.
    // "Will Japan reach the Round of 16…" -> "Japan R16"). Full text on hover.
    var adv = s.match(/^will\s+(.+?)\s+(reach|win|advance|qualif|be eliminat|progress|top|finish)\b(.*)$/i);
    if (adv) {
      var team = adv[1].replace(/\s+/g, " ").trim();
      var r = (adv[2] + " " + adv[3]).toLowerCase();
      var stage =
        /round of 16|last 16|\br16\b/.test(r) ? "R16" :
        /quarter/.test(r) ? "QF" :
        /semi/.test(r) ? "SF" :
        /win the (world cup|tournament)|lift|champion/.test(r) ? "Winner" :
        /final/.test(r) ? "Final" :
        /eliminat/.test(r) ? "Out" :
        /group/.test(r) ? "Group" :
        /qualif/.test(r) ? "Qualify" : "Advance";
      return team + " " + stage;
    }
    if (/bet ?builder/i.test(s)) return "Bet Builder";
    if (/accumulator|acca/i.test(s)) return "Accumulator";
    // Title-case a bare snake_case key so it reads as words, not code.
    if (/^[a-z0-9_]+$/.test(s)) {
      s = s.replace(/_/g, " ").replace(/\b\w/g, function (c) {
        return c.toUpperCase();
      });
    }
    if (s.length > 18) return s.slice(0, 17) + "…";
    return s;
  }
  // Bet Builder / accumulator selections list each leg; render them stacked on
  // their own line so every leg is readable (not crammed/truncated into one).
  function selDisplay(p) {
    var s = String(p.selection == null ? "" : p.selection).trim();
    if (!s) return "—";
    var combo = /bet ?builder|accumulator|acca/i.test(String(p.market || ""));
    var legs = null;
    if (s.indexOf(" & ") !== -1) legs = s.split(" & ");
    else if (combo && s.indexOf(" / ") !== -1) legs = s.split(" / ");
    if (legs && legs.length > 1) {
      return legs.map(function (leg) {
        return '<span class="leg">' + esc(leg.trim()) + '</span>';
      }).join("");
    }
    return esc(s);
  }
  // Full bet metadata as a row hover-title: every field, including the free-text
  // notes that don't get their own column.
  function metaTitle(p) {
    var parts = [];
    if (p.match) parts.push(p.match);
    if (p.market) parts.push("market: " + p.market);
    if (p.selection) parts.push("selection: " + p.selection);
    if (p.id != null) parts.push("bet #" + p.id);
    if (p.account) parts.push("account " + p.account);
    if (p.notes) parts.push("notes: " + p.notes);
    return parts.join("  |  ");
  }
  // Color class for an EV cell: positive -> pos, negative -> neg, and a
  // missing/non-numeric EV (e.g. an acca with no modelled edge) stays neutral
  // rather than being painted green by a Number(null) === 0 coercion.
  function evClass(v) {
    if (v === null || v === undefined || isNaN(v)) return "dim";
    return Number(v) >= 0 ? "pos" : "neg";
  }
  function timeOnly(ts) {
    if (!ts) return "—";
    // Parse as UTC and render in the viewer's local timezone (consistent
    // with the chart axis). Falls back to the raw string if unparseable.
    var ms = Date.parse(String(ts).indexOf("T") >= 0 &&
      !/[zZ]|[+\-]\d\d:?\d\d$/.test(String(ts)) ? String(ts) + "Z" : String(ts));
    if (!isNaN(ms)) {
      var dt = new Date(ms);
      function p(n) { return (n < 10 ? "0" : "") + n; }
      return p(dt.getHours()) + ":" + p(dt.getMinutes());
    }
    return String(ts);
  }
  // "19 Jun" — calendar date in the viewer's local tz (pairs with timeOnly()).
  function dateShort(ts) {
    if (!ts) return "—";
    var raw = String(ts);
    var ms = Date.parse(raw.indexOf("T") >= 0 &&
      !/[zZ]|[+\-]\d\d:?\d\d$/.test(raw) ? raw + "Z" : raw);
    if (isNaN(ms)) return "";
    var dt = new Date(ms);
    var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return dt.getDate() + " " + MON[dt.getMonth()];
  }
  // Stacked date-over-time cell for the positions tables (the space between the
  // spans renders only when they go inline on the mobile card layout).
  function whenCell(ts) {
    return '<span class="wd">' + esc(dateShort(ts)) + '</span> ' +
           '<span class="wt">' + esc(timeOnly(ts)) + '</span>';
  }

  // Minimal text escaping for any value sourced from data.json before it goes
  // into innerHTML. Defends against a hostile match/selection string.
  function esc(v) {
    if (v === null || v === undefined) return "";
    return String(v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---- renderers ----------------------------------------------------------

  function renderTicker(d) {
    var t = d.totals || {};
    var byCcy = d.totals_by_currency || null;
    var clv = d.clv || {};
    var pl = Number(t.settled_pl || 0);
    var plCls = pl >= 0 ? "pos" : "neg";

    var clvVal = "N/A";
    var hasClv = clv.avg_clv !== null && clv.avg_clv !== undefined &&
      (clv.n_with_close || 0) > 0;
    if (hasClv) {
      var a = Number(clv.avg_clv);
      clvVal = (a >= 0 ? "+" : "") + (a * 100).toFixed(2) + "%";
    }

    // Per-currency display when available (never sums £ with $); falls back
    // to the legacy single-currency totals for old data files.
    var wagered = byCcy ? moneyByCcy(byCcy, "wagered") : money(t.wagered || 0);
    var openExp = byCcy ? moneyByCcy(byCcy, "open_stake") : money(t.open_stake || 0);
    // Per-currency, per-sign coloured P&L (HTML); each leg owns its own colour.
    var plStr = byCcy ? moneyByCcyHTML(byCcy, "settled_pl")
      : '<span class="' + plCls + '">' + esc(signedMoney(pl)) + "</span>";

    // Net return per unit staked (settled P/L ÷ wagered), one tile per
    // currency. A normalised companion to Settled P&L: "+10.7%" means 10.7p
    // of profit for every £1 put through. Never mixes currencies — each unit
    // (£/$/€) gets its own sign-coloured tile; currencies with nothing settled
    // (wagered 0) are skipped.
    var retTicks = [];
    if (byCcy) {
      ["GBP", "USD", "EUR"].forEach(function (ccy) {
        var blk = byCcy[ccy];
        if (!blk) return;
        var w = Number(blk.wagered || 0);
        if (w <= 0) return;
        var r = Number(blk.settled_pl || 0) / w;
        var rStr = (r >= 0 ? "+" : "") + (r * 100).toFixed(1) + "%";
        retTicks.push(["Return / " + sym(ccy) + "1",
          '<span class="' + (r < 0 ? "neg" : "pos") + '">' + esc(rStr) + "</span>", ""]);
      });
    }

    // PRIMARY row — the four headline KPIs. CLV leads (it is the project's
    // primary KPI). "Return" uses the first per-currency return tile (£ where
    // present); any further currency returns spill into the secondary row.
    var primaryReturn = retTicks.length
      ? [retTicks[0][0], retTicks[0][1], ""]
      : ["Return", '<span class="dim">N/A</span>', "dim"];
    var primaryTicks = [
      ["Avg CLV", esc(clvVal), hasClv ? (Number(clv.avg_clv) >= 0 ? "pos" : "neg") : "dim"],
      ["Settled P&L", plStr, ""],
      primaryReturn,
      ["Bet Count", esc(String(t.n_bets || 0)), ""]
    ];

    // SECONDARY row — everything else, collapsed by default behind "more".
    var secondaryTicks = [
      ["Total Wagered", esc(wagered), ""],
      ["Open Exposure", esc(openExp), ""]
    ].concat(retTicks.slice(1));

    // P&L by source (model / offer / punt), moved here from the venues panel.
    // Each currency leg is coloured by its own sign via moneyByCcyHTML.
    var ss = d.source_summary || {};
    [["model", "Model P&L"], ["offer", "Offer P&L"], ["punt", "Punt P&L"]]
      .forEach(function (pair) {
        var byCcy = ss[pair[0]];
        if (byCcy && ["GBP", "USD", "EUR"].some(function (c) { return byCcy[c]; })) {
          secondaryTicks.push([pair[1], moneyByCcyHTML(byCcy, "settled_pl"), ""]);
        }
      });

    function tickHTML(row) {
      return '<div class="tick">' +
        '<div class="tick-label">' + esc(row[0]) + '</div>' +
        '<div class="tick-value num ' + row[2] + '">' + row[1] + '</div>' +
        '</div>';
    }

    // Primary tiles, then a "more" toggle, then the (hidden) secondary tiles.
    var html = primaryTicks.map(tickHTML).join("");
    if (secondaryTicks.length) {
      html += '<button type="button" class="tick-more" id="tick-more" ' +
        'aria-expanded="false">more &#9662;</button>' +
        '<div class="tick-secondary" id="tick-secondary" hidden>' +
        secondaryTicks.map(tickHTML).join("") + '</div>';
    }
    $("ticker-stats").innerHTML = html;

    var moreBtn = $("tick-more");
    if (moreBtn) {
      moreBtn.addEventListener("click", function () {
        var sec = $("tick-secondary");
        if (!sec) return;
        var open = !sec.hidden;
        sec.hidden = open;
        moreBtn.setAttribute("aria-expanded", String(!open));
        moreBtn.innerHTML = open ? "more &#9662;" : "less &#9652;";
      });
    }
  }

  var VENUE_COLOR = { sportsbook: "#4ade80", polymarket: "#60a5fa", kalshi: "#a855f7" };

  // Per-book accent colours — the SINGLE source of truth for every venue
  // surface (venues-panel dots, open + closed row rails, open + closed venue
  // pills). Keys are normalised (lower-cased, separators stripped) so the
  // human-readable ledger strings — "Paddy Power", "Betfair Sportsbook",
  // "Virgin Bet", "Sky Bet" — all resolve instead of falling through to grey.
  // Each in-use venue gets a visually distinct hue; the two Betfair surfaces
  // intentionally share the gold/orange family but stay distinguishable.
  var BOOK_FALLBACK = "#9ca3af";
  var BOOK_COLOR = {
    paddypower: "#22c55e",         // green
    bet365: "#4d7c0f",             // olive / dark green
    virginbet: "#ef4444",          // red
    skybet: "#818cf8",             // indigo
    betway: "#84cc16",             // lime
    betfair: "#facc15",            // gold (exchange)
    betfairexuk: "#facc15",
    betfairexchange: "#facc15",
    betfairsportsbook: "#f97316",  // orange (sportsbook)
    betfred: "#ec4899",            // pink
    williamhill: "#1d4ed8",        // deep blue
    smarkets: "#2dd4bf",           // teal
    matchbook: "#f472b6",          // rose
    coral: "#fb923c",              // amber
    ladbrokes: "#dc2626",          // dark red
    polymarket: "#60a5fa",         // light blue
    kalshi: "#a855f7"              // purple
  };
  function bookColor(name) {
    var k = String(name === null || name === undefined ? "" : name)
      .toLowerCase().replace(/[^a-z0-9]/g, "");
    return BOOK_COLOR[k] || BOOK_FALLBACK;
  }

  // Default display labels for the known venue keys. A venue block may also
  // carry its own .label (e.g. "SPORTSBOOK 1"); that wins when present.
  var VENUE_LABEL = {
    sportsbook: "sportsbook",
    sportsbook_1: "sportsbook 1",
    sportsbook_2: "sportsbook 2",
    polymarket: "polymarket",
    kalshi: "kalshi"
  };
  function venueLabel(key, blk) {
    if (blk && blk.label) return String(blk.label);
    return VENUE_LABEL[key] || key;
  }
  // Map a venue display key back to the platforms[].venue value used for the
  // per-book breakdown. Both sportsbook_1/_2 split out of the combined
  // "sportsbook" venue, which the platforms block is keyed by (no account
  // split there) — so the book breakdown is rendered under Sportsbook 1 only.
  function platformVenueKey(key) {
    return (key === "sportsbook_1" || key === "sportsbook_2") ? "sportsbook" : key;
  }

  function renderVenues(d) {
    var venues = d.venues || {};
    // When the data layer splits sportsbook by account it emits sportsbook_1 /
    // sportsbook_2 (each with a .label) and drops the combined "sportsbook"
    // row. Old data.json has only "sportsbook" — fall back to it.
    var hasSplit = !!(venues.sportsbook_1 || venues.sportsbook_2);
    var order = hasSplit
      ? ["sportsbook_1", "sportsbook_2", "polymarket", "kalshi"]
      : ["sportsbook", "polymarket", "kalshi"];
    // Hide venues with no bets yet (e.g. kalshi pre-launch); bars are scaled
    // within-currency only, so a £ bar and a $ bar are not length-comparable —
    // the per-row amount label carries the truth.
    var active = order.filter(function (k) {
      return Number((venues[k] || {}).n_bets || 0) > 0;
    });
    if (!active.length) active = [hasSplit ? "sportsbook_1" : "sportsbook"];
    var amounts = active.map(function (k) {
      return Number((venues[k] || {}).wagered || 0);
    });
    var max = Math.max.apply(null, amounts.concat([0]));

    $("venues-meta").textContent = moneyByCcy(d.totals_by_currency, "wagered");

    // Per-bookmaker rows nested under each venue, coloured by book. For the two
    // sportsbook accounts we use the per-account breakdown so a1 AND a2 each show
    // their OWN book split (they share books, so the venue-level map can't).
    var plats = d.platforms || {};
    var pba = d.platforms_by_account || {};

    function platformRows(venueKey, venueWagered, ccy, account) {
      var src = (account && pba[account]) ? pba[account] : plats;
      var rows = Object.keys(src).filter(function (p) {
        return (src[p].venue || "sportsbook") === venueKey && Number(src[p].wagered || 0) > 0;
      }).sort(function (a, b) { return src[b].wagered - src[a].wagered; });
      if (!rows.length) return "";
      return '<div class="venue-books">' + rows.map(function (p) {
        var blk = src[p];
        var frac = venueWagered > 0 ? (blk.wagered / venueWagered) : 0;
        var pl = Number(blk.settled_pl || 0);
        var plBit = pl !== 0
          ? ' · <span class="' + (pl >= 0 ? "pos" : "neg") + '">' + signedMoney(pl, ccy) + '</span>'
          : "";
        return '<div class="venue-book-row">' +
          '<span class="book-dot" style="background:' + bookColor(p) + '"></span>' +
          '<span class="book-name">' + esc(p) + '</span>' +
          '<span class="book-bar"><span style="display:block;height:100%;width:' +
            (frac * 100).toFixed(1) + '%;background:' + bookColor(p) + ';opacity:.55"></span></span>' +
          '<span class="book-amt num">' + money(blk.wagered, ccy) +
            ' <span class="dim">(' + blk.n_bets + ')</span>' + plBit + '</span>' +
        '</div>';
      }).join("") + '</div>';
    }

    var html = active.map(function (k, i) {
      var v = venues[k] || {};
      var ccy = v.currency || "GBP";
      var amt = amounts[i];
      var frac = max > 0 ? (amt / max) : 0;
      var nb = Number(v.n_bets || 0);
      // Colour and book-breakdown both key off the underlying venue, so the
      // two sportsbook accounts share the sportsbook colour and the breakdown
      // sits under Sportsbook 1 only.
      var pvk = platformVenueKey(k);
      // Sportsbook splits share the green family but are visually distinct:
      // account 1 keeps the canonical green, account 2 uses a teal-green.
      var color = (k === "sportsbook_2") ? "#34d399"
        : (VENUE_COLOR[pvk] || "#9ca3af");
      // Per-book breakdown: each sportsbook account uses its own per-account
      // split (so a2 shows its books just like a1); other venues use the
      // combined platforms map.
      var acct = (k === "sportsbook_1") ? "1" : (k === "sportsbook_2") ? "2" : null;
      var books = platformRows(pvk, amt, ccy, acct);
      return '' +
        '<div class="venue-row">' +
          '<div class="venue-top">' +
            '<span class="venue-name">' + esc(venueLabel(k, v)) + '</span>' +
            '<span class="venue-amt num">' + money(amt, ccy) + '</span>' +
          '</div>' +
          '<div class="venue-track">' +
            '<div class="venue-fill ' + pvk + '" style="width:' +
              (frac * 100).toFixed(1) + '%;background:' + color + ';opacity:.75"></div>' +
          '</div>' +
          '<div class="venue-sub num">' + nb + ' bet' + (nb === 1 ? '' : 's') +
            ' · open ' + money(v.open_stake || 0, ccy) + '</div>' +
          books +
        '</div>';
    }).join("");

    // (P&L by source moved to the top ticker row — see renderTicker.)

    $("venues").innerHTML = html;
  }

  // Small terminal chip for a bet's source (model/offer/punt). Unknown/absent
  // source -> em-dash cell so old data.json rows stay clean.
  var SRC_ABBR = { model: "MDL", offer: "OFR", punt: "PNT", hedge: "HDG" };
  function sourceChip(src) {
    var s = String(src === null || src === undefined ? "" : src).toLowerCase().trim();
    if (s !== "model" && s !== "offer" && s !== "punt" && s !== "hedge") {
      return '<span class="dim">—</span>';
    }
    return '<span class="src-chip src-' + s + '" title="' + esc(s) + '">' +
      SRC_ABBR[s] + '</span>';
  }
  // "A2" suffix chip on the venue pill, ONLY for account-2 rows. Account 1 (or
  // an absent account on old data) renders nothing, keeping those pills clean.
  function accountSuffix(account) {
    var a = String(account === null || account === undefined ? "" : account).trim();
    return a === "2" ? ' <span class="acct-chip">A2</span>' : '';
  }
  // Manual-override cell (open + closed tables). A bet is "manually overwritten"
  // when its row carries a manual_override note (set on this source-of-truth
  // machine via scripts/wca_override.py); the auto-grader then leaves it alone.
  // Shows a ✎ badge with the full reason on hover; em-dash when untouched.
  function overrideCell(p) {
    var o = (p.manual_override == null) ? "" : String(p.manual_override).trim();
    if (!o) return '<td class="pos-ovr" data-label="Override"><span class="dim">—</span></td>';
    return '<td class="pos-ovr" data-label="Override" title="' + esc(o) +
      '"><span class="ovr-flag" style="color:#f2c14e;font-weight:600">✎ manual</span></td>';
  }

  function renderPositions(d) {
    var pos = d.positions || [];
    if (!pos.length) {
      $("positions").innerHTML = '<div class="empty">No open positions</div>';
      $("positions-meta").textContent = "0";
      return;
    }
    $("positions-meta").textContent = String(pos.length);

    var rows = pos.map(function (p) {
      var venue = esc(p.venue || "sportsbook");
      var book = p.platform || p.venue || "";
      // Colour is keyed off the actual book (platform), falling back to the
      // venue, then to the neutral grey. Used for both the left rail and the
      // pill so the row is identifiable at a glance.
      var col = bookColor(book || venue);
      // Edge = model − market(devig): the model's claimed mispricing. Only
      // meaningful when both are present.
      var edge = (p.model_prob != null && p.market_prob_devig != null)
        ? (p.model_prob - p.market_prob_devig) : null;
      return '<tr title="' + esc(metaTitle(p)) + '" style="border-left:2px solid ' + col + '">' +
        '<td class="num pos-when" data-label="Date">' +
          whenCell(p.ts_utc) + '</td>' +
        '<td class="pos-match" data-label="Match" title="' + esc(p.match) + '">' + esc(dash(p.match)) + '</td>' +
        '<td class="pos-mkt dim" data-label="Market" title="' + esc(p.market) + '">' + esc(marketShort(p.market)) + '</td>' +
        '<td class="pos-sel" data-label="Selection" title="' + esc(p.selection) + '">' + selDisplay(p) + '</td>' +
        '<td class="r num" data-label="Odds">' + esc(num(p.decimal_odds)) + '</td>' +
        '<td class="r num" data-label="Stake">' + esc(money(p.stake, p.currency)) + '</td>' +
        '<td class="r num" data-label="Model">' + esc(pct(p.model_prob, 0)) + '</td>' +
        '<td class="r num dim" data-label="Mkt">' + esc(pct(p.market_prob_devig, 0)) + '</td>' +
        '<td class="r num ' + evClass(edge) + '" data-label="Edge">' +
          esc(edge == null ? '—' : pctSigned(edge, 1)) + '</td>' +
        '<td class="r num ' + evClass(p.ev) + '" data-label="EV">' +
          esc(p.ev === null || p.ev === undefined ? '—' : pct(p.ev, 1)) + '</td>' +
        '<td class="pos-src" data-label="Source">' + sourceChip(p.source) + '</td>' +
        '<td data-label="Venue"><span class="pill book ' + venue + '" style="color:' + col +
          ';border-color:' + col + '">' + esc(p.platform || venue) +
          accountSuffix(p.account) + '</span></td>' +
        overrideCell(p) +
        '</tr>';
    }).join("");

    $("positions").innerHTML =
      '<table class="pos-table pos-table-wide">' +
        '<thead><tr>' +
          '<th>Date</th><th>Match</th><th>Market</th><th>Selection</th>' +
          '<th class="r">Odds</th><th class="r">Stake</th>' +
          '<th class="r">Model</th><th class="r">Mkt</th>' +
          '<th class="r">Edge</th><th class="r">EV</th>' +
          '<th>Source</th><th>Venue</th><th>Override</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  function renderPredictions(d) {
    var preds = d.predictions || [];
    if (!preds.length) {
      $("predictions").innerHTML = '<div class="empty">No fixture predictions</div>';
      $("predictions-meta").textContent = "0";
      return;
    }
    $("predictions-meta").textContent =
      preds.length + " fixture" + (preds.length === 1 ? "" : "s");

    var cards = preds.map(function (fx) {
      var scores = (fx.scores || []).slice(0, 4);
      var maxProb = scores.reduce(function (m, s) {
        return Math.max(m, Number(s.prob || 0));
      }, 0);

      var scoreHtml = scores.map(function (s) {
        var p = Number(s.prob || 0);
        var frac = maxProb > 0 ? (p / maxProb) : 0;
        // Inline background: the stylesheet fill colour is too close to the
        // track on dark displays, which made every bar look identical.
        var fair = (s.fair !== null && s.fair !== undefined && !isNaN(s.fair))
          ? Number(s.fair) : (p > 0 ? 100 / p : null);
        return '<div class="score-row">' +
          '<span class="score-label">' + esc(s.score) + '</span>' +
          '<span class="score-bar"><span class="score-fill" style="width:' +
            (frac * 100).toFixed(1) + '%;background:#4ade80;opacity:.85"></span></span>' +
          '<span class="score-prob">' + p.toFixed(1) + '%' +
            (fair ? ' <span class="dim">· ' + fair.toFixed(1) + '</span>' : '') +
          '</span>' +
        '</div>';
      }).join("");

      var foot = [];
      if (fx.over_under) {
        var ou = fx.over_under;
        // pct1 tolerates null/undefined/NaN so a slim O/U line (line only, no
        // over/under) degrades to an em-dash instead of throwing on .toFixed.
        foot.push('<span><b>O/U ' + esc(num(ou.line, 1)) + '</b> ' +
          esc(pct1(ou.over)) + ' / ' + esc(pct1(ou.under)) + '</span>');
      }
      if (fx.btts !== null && fx.btts !== undefined && !isNaN(fx.btts)) {
        foot.push('<span><b>BTTS</b> ' + esc(pct1(fx.btts)) + '</span>');
      }

      return '<div class="pred-card">' +
        '<div class="pred-title">' + esc(fx.fixture) + '</div>' +
        (scoreHtml || '<div class="empty">no scores</div>') +
        (foot.length ? '<div class="pred-foot">' + foot.join("") + '</div>' : '') +
      '</div>';
    }).join("");

    $("predictions").innerHTML = '<div class="pred-grid">' + cards + '</div>';
  }

  function renderFooter(d) {
    var gen = (d.meta && d.meta.generated) ? d.meta.generated : "";
    $("foot-gen").textContent = gen ? ("Generated " + gen) : "Generated —";
  }

  // ---- charts (pure inline SVG) ------------------------------------------
  // Shared geometry. viewBox is fixed ~640x220; the stylesheet stretches the
  // SVG to the panel width while preserving aspect ratio.
  var CHART_W = 640, CHART_H = 220;
  var CHART_M = { top: 14, right: 58, bottom: 26, left: 38 };
  var PLOT_W = CHART_W - CHART_M.left - CHART_M.right;
  var PLOT_H = CHART_H - CHART_M.top - CHART_M.bottom;

  // Round to a tidy number of decimals for SVG coordinates (keeps markup small
  // and avoids float noise like 123.4000001).
  function r2(n) { return Math.round(n * 100) / 100; }

  // Parse an ISO-ish timestamp ("2026-06-11T12:58:42", optionally trailing Z)
  // to epoch ms. Returns NaN on anything unparseable so callers can filter.
  function tsMs(ts) {
    if (!ts) return NaN;
    var s = String(ts);
    if (s.indexOf("T") >= 0 && !/[zZ]|[+\-]\d\d:?\d\d$/.test(s)) s += "Z";
    var v = Date.parse(s);
    return isNaN(v) ? NaN : v;
  }
  // HH:MM in the VIEWER'S local timezone for an epoch-ms value. Timestamps
  // are stored UTC (tsMs appends Z); rendering local means the chart axis
  // matches the clock on the wall wherever the page is opened.
  function hhmm(ms) {
    var dt = new Date(ms);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(dt.getHours()) + ":" + p(dt.getMinutes());
  }
  // Short viewer-timezone label for the axis (e.g. "UTC+3"), so two people
  // in different timezones reading the same chart aren't confused.
  function tzLabel() {
    var mins = -new Date().getTimezoneOffset();
    if (mins === 0) return "UTC";
    var sign = mins > 0 ? "+" : "-";
    var h = Math.floor(Math.abs(mins) / 60);
    var m = Math.abs(mins) % 60;
    return "UTC" + sign + h + (m ? ":" + (m < 10 ? "0" : "") + m : "");
  }

  function chartEmpty(el, msg) {
    el.innerHTML = '<div class="chart-empty">' + esc(msg) + '</div>';
  }

  // Build the shared chart frame (background grid + y/x axes). yLabels is an
  // array of {y, text}; xTicks an array of {x, text}. Returns an SVG string of
  // just the frame; series are appended by the caller.
  function chartFrame(yLabels, xTicks) {
    var s = '';
    yLabels.forEach(function (yl) {
      s += '<line class="cx-grid" x1="' + r2(CHART_M.left) + '" y1="' + r2(yl.y) +
        '" x2="' + r2(CHART_M.left + PLOT_W) + '" y2="' + r2(yl.y) + '"/>';
      s += '<text class="cx-tick" x="' + r2(CHART_M.left - 6) + '" y="' + r2(yl.y + 3) +
        '" text-anchor="end">' + esc(yl.text) + '</text>';
    });
    // axes
    s += '<line class="cx-axis" x1="' + r2(CHART_M.left) + '" y1="' + r2(CHART_M.top) +
      '" x2="' + r2(CHART_M.left) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
    s += '<line class="cx-axis" x1="' + r2(CHART_M.left) + '" y1="' + r2(CHART_M.top + PLOT_H) +
      '" x2="' + r2(CHART_M.left + PLOT_W) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
    xTicks.forEach(function (xt) {
      s += '<text class="cx-tick" x="' + r2(xt.x) + '" y="' + r2(CHART_M.top + PLOT_H + 14) +
        '" text-anchor="middle">' + esc(xt.text) + '</text>';
    });
    return s;
  }

  // Pick 4-6 evenly spaced x ticks across [t0,t1], mapped through scaleX.
  function timeTicks(t0, t1, scaleX) {
    var n = 5;
    if (t1 <= t0) return [{ x: scaleX(t0), text: hhmm(t0) }];
    var ticks = [];
    for (var i = 0; i < n; i++) {
      var t = t0 + (t1 - t0) * (i / (n - 1));
      ticks.push({ x: scaleX(t), text: hhmm(t) });
    }
    // Tag the final tick with the viewer's timezone so the axis is
    // self-describing (times are rendered local, storage stays UTC).
    if (ticks.length) {
      ticks[ticks.length - 1].text += " " + tzLabel();
    }
    return ticks;
  }

  // (a) LINE MOVEMENT ------------------------------------------------------
  // linemove.json shape (tolerant). Two event shapes are accepted:
  //   * the producer (wca.linemove) shape — an object with .events[], each
  //     event {fixture, kickoff?, series:{home:[[ts,prob],...], draw:[...],
  //     away:[...]}} (three parallel [ts, prob] arrays); and
  //   * a legacy point-list shape — {id?, fixture|match|label, kickoff?,
  //     points:[{ts, home, draw, away}]}.
  // home/draw/away probs may be 0..1 fractions or 0..100 percentages
  // (auto-detected downstream).
  // Either shape may also carry an optional model block — {home, draw, away}
  // pre-match MODEL probabilities (possibly a subset of the three legs) —
  // drawn as dashed horizontal reference lines.
  var LINEMOVE = { events: [], idx: 0, wired: false };

  // Zip the producer's three parallel {leg: [[ts, prob], ...]} arrays into the
  // internal [{t, home, draw, away}] point list, joining legs by timestamp.
  // Returns null if `series` is not that shape so the caller can fall back.
  function pointsFromSeries(series) {
    if (!series || typeof series !== "object" || Array.isArray(series)) return null;
    var legs = ["home", "draw", "away"];
    var hasLeg = legs.some(function (k) { return Array.isArray(series[k]); });
    if (!hasLeg) return null;
    var byTs = {};
    legs.forEach(function (leg) {
      var arr = series[leg];
      if (!Array.isArray(arr)) return;
      arr.forEach(function (pair) {
        if (!Array.isArray(pair) || pair.length < 2) return;
        var ts = pair[0];
        var pt = byTs[ts];
        if (!pt) { pt = byTs[ts] = { ts: ts }; }
        pt[leg] = pair[1];
      });
    });
    return Object.keys(byTs).map(function (ts) { return byTs[ts]; });
  }

  function normLineMove(raw) {
    var events = [];
    var list = [];
    if (Array.isArray(raw)) list = raw;
    else if (raw && Array.isArray(raw.events)) list = raw.events;
    else if (raw && typeof raw === "object") {
      // map keyed by fixture -> points/event
      Object.keys(raw).forEach(function (k) {
        var v = raw[k];
        if (v && (Array.isArray(v) || v.points || v.series)) {
          list.push({ fixture: k,
            points: Array.isArray(v) ? v : v.points,
            series: v && v.series,
            kickoff: v && v.kickoff });
        }
      });
    }
    list.forEach(function (ev) {
      if (!ev || typeof ev !== "object") return;
      var label = ev.fixture || ev.match || ev.label || ev.id || "Fixture";
      // Prefer an explicit point list; otherwise zip the producer's series.
      var rawPts = ev.points;
      if (!Array.isArray(rawPts)) rawPts = pointsFromSeries(ev.series);
      if (!Array.isArray(rawPts)) return;
      var pts = rawPts.map(function (pt) {
        var t = tsMs(pt.ts || pt.time || pt.t);
        return {
          t: t,
          home: numOrNull(pt.home),
          draw: numOrNull(pt.draw),
          away: numOrNull(pt.away)
        };
      }).filter(function (pt) { return !isNaN(pt.t); });
      pts.sort(function (a, b) { return a.t - b.t; });
      if (!pts.length) return;
      events.push({
        label: String(label),
        kickoff: tsMs(ev.kickoff || ev.commence_time || ev.start),
        points: pts,
        model: normModel(ev.model)
      });
    });
    // Chronological by kickoff (soonest first); unknown kickoffs sink to the end.
    events.sort(function (a, b) {
      var ka = isNaN(a.kickoff) ? Infinity : a.kickoff;
      var kb = isNaN(b.kickoff) ? Infinity : b.kickoff;
      return ka - kb;
    });
    return events;
  }
  function numOrNull(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return null;
    return Number(v);
  }
  // Normalise an event's optional model block to {home, draw, away} (numbers
  // or null). Returns null when no leg carries a usable value so callers can
  // skip the model overlay entirely.
  function normModel(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    var model = { home: null, draw: null, away: null }, any = false;
    ["home", "draw", "away"].forEach(function (k) {
      var v = numOrNull(raw[k]);
      if (v !== null) { model[k] = v; any = true; }
    });
    return any ? model : null;
  }
  // Model probs are documented as 0..1 fractions, but tolerate percentages.
  function modelPct(v) { return v > 1.5 ? v : v * 100; }
  // Detect whether prob values are fractions (<=1) or percentages and return a
  // function that maps a raw value to 0..100.
  function pctScaler(events) {
    var max = 0;
    events.forEach(function (ev) {
      ev.points.forEach(function (pt) {
        ["home", "draw", "away"].forEach(function (k) {
          if (pt[k] !== null) max = Math.max(max, pt[k]);
        });
      });
    });
    var asPct = max > 1.5; // values already in 0..100
    return function (v) { return v === null ? null : (asPct ? v : v * 100); };
  }

  function lineSeries(points, key, scaleX, scaleY, toPct) {
    var segs = [], cur = [];
    points.forEach(function (pt) {
      var v = toPct(pt[key]);
      if (v === null) { if (cur.length) { segs.push(cur); cur = []; } return; }
      cur.push(r2(scaleX(pt.t)) + "," + r2(scaleY(v)));
    });
    if (cur.length) segs.push(cur);
    return segs;
  }

  function drawLineMove() {
    var el = $("linemove-canvas");
    if (!el) return;
    var events = LINEMOVE.events;
    if (!events.length) { chartEmpty(el, "No line-movement data"); return; }
    var ev = events[Math.min(LINEMOVE.idx, events.length - 1)] || events[0];
    var pts = ev.points;
    if (!pts.length) { chartEmpty(el, "No line-movement data"); return; }

    var toPct = pctScaler(events);
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var span = t1 - t0 || 1;
    function scaleX(t) { return CHART_M.left + ((t - t0) / span) * PLOT_W; }

    // Auto-fit the y-axis to THIS fixture's actual prob range so small but real
    // moves (pre-match shifts are often <1pp) are visible instead of a flat
    // line pinned to a 0-100% axis. Enforce a minimum window so a near-static
    // fixture isn't absurdly magnified, and pad the extremes.
    var vals = [];
    pts.forEach(function (pt) {
      ["home", "draw", "away"].forEach(function (k) {
        var v = toPct(pt[k]);
        if (v !== null && !isNaN(v)) vals.push(v);
      });
    });
    var model = ev.model || null;
    if (model) {
      ["home", "draw", "away"].forEach(function (k) {
        if (model[k] !== null) vals.push(modelPct(model[k]));
      });
    }
    var lo = vals.length ? Math.min.apply(null, vals) : 0;
    var hi = vals.length ? Math.max.apply(null, vals) : 100;
    var pad = Math.max((hi - lo) * 0.15, 1);   // 15% padding, >=1pp
    lo = Math.max(0, lo - pad);
    hi = Math.min(100, hi + pad);
    if (hi - lo < 6) {                          // minimum 6pp window
      var mid = (hi + lo) / 2;
      lo = Math.max(0, mid - 3);
      hi = Math.min(100, mid + 3);
    }
    var yrange = hi - lo || 1;
    function scaleY(v) { return CHART_M.top + (1 - ((v - lo) / yrange)) * PLOT_H; }

    var yLabels = [lo, lo + yrange * 0.25, lo + yrange * 0.5, lo + yrange * 0.75, hi]
      .map(function (p) { return { y: scaleY(p), text: p.toFixed(1) + "%" }; });
    var svg = chartFrame(yLabels, timeTicks(t0, t1, scaleX));

    var SERIES = [
      { key: "home", color: "#4ade80", name: "Home" },
      { key: "draw", color: "#9ca3af", name: "Draw" },
      { key: "away", color: "#ef4444", name: "Away" }
    ];
    SERIES.forEach(function (sr) {
      lineSeries(pts, sr.key, scaleX, scaleY, toPct).forEach(function (seg) {
        svg += '<polyline class="cx-series" stroke="' + sr.color +
          '" points="' + seg.join(" ") + '"/>';
      });
    });

    // model reference lines: dashed horizontals at the pre-match MODEL prob
    // for each leg the producer resolved (the block may be partial / absent).
    var modelLegs = [];
    if (model) {
      SERIES.forEach(function (sr) {
        if (model[sr.key] === null) return;
        var my = scaleY(modelPct(model[sr.key]));
        svg += '<line stroke="' + sr.color + '" stroke-width="1" ' +
          'stroke-dasharray="5 4" opacity="0.65" x1="' + r2(CHART_M.left) +
          '" y1="' + r2(my) + '" x2="' + r2(CHART_M.left + PLOT_W) +
          '" y2="' + r2(my) + '"/>';
        modelLegs.push(sr);
      });
    }

    // kickoff marker if within range
    if (!isNaN(ev.kickoff) && ev.kickoff >= t0 && ev.kickoff <= t1) {
      var kx = scaleX(ev.kickoff);
      svg += '<line class="cx-kick" x1="' + r2(kx) + '" y1="' + r2(CHART_M.top) +
        '" x2="' + r2(kx) + '" y2="' + r2(CHART_M.top + PLOT_H) + '"/>';
      svg += '<text class="cx-kick-lbl" x="' + r2(kx + 3) + '" y="' +
        r2(CHART_M.top + 8) + '">KO</text>';
    }

    // legend (top-right inside the right margin); solid swatches for the
    // market series, dashed swatches for any model reference lines.
    var lx = CHART_M.left + PLOT_W + 8, ly = CHART_M.top + 4;
    SERIES.forEach(function (sr, i) {
      var y = ly + i * 14;
      svg += '<rect x="' + r2(lx) + '" y="' + r2(y - 6) + '" width="8" height="8" rx="1" fill="' +
        sr.color + '"/>';
      svg += '<text class="cx-legend" x="' + r2(lx + 12) + '" y="' + r2(y + 1) + '">' +
        esc(sr.name) + '</text>';
    });
    // One dashed row covers all model lines: the colour mapping is already
    // established by the solid swatches above, and the narrow right margin
    // (~50px) cannot fit per-leg "Home (model)" labels without clipping.
    if (modelLegs.length) {
      var my2 = ly + SERIES.length * 14;
      var mcolor = modelLegs.length === 1 ? modelLegs[0].color : "#6b7280";
      svg += '<line stroke="' + mcolor + '" stroke-width="2" ' +
        'stroke-dasharray="3 2" opacity="0.65" x1="' + r2(lx) + '" y1="' +
        r2(my2 - 2) + '" x2="' + r2(lx + 8) + '" y2="' + r2(my2 - 2) + '"/>';
      svg += '<text class="cx-legend" x="' + r2(lx + 12) + '" y="' +
        r2(my2 + 1) + '">model</text>';
    }

    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Line movement">' +
      svg + '</svg>';
  }

  function wireLineMoveSelect() {
    var sel = $("linemove-select");
    if (!sel) return;
    var events = LINEMOVE.events;
    if (events.length > 1) {
      sel.hidden = false;
      sel.innerHTML = events.map(function (ev, i) {
        return '<option value="' + i + '">' + esc(ev.label) + '</option>';
      }).join("");
      sel.value = String(LINEMOVE.idx);
      if (!LINEMOVE.wired) {
        sel.addEventListener("change", function () {
          LINEMOVE.idx = Number(sel.value) || 0;
          drawLineMove();
        });
        LINEMOVE.wired = true;
      }
    } else {
      sel.hidden = true;
      sel.innerHTML = "";
    }
  }

  function loadLineMove() {
    var el = $("linemove-canvas");
    if (!el || typeof fetch !== "function") { if (el) chartEmpty(el, "No line-movement data"); return; }
    var url = "./linemove.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (raw) {
        LINEMOVE.events = normLineMove(raw);
        // Default to the first event carrying a model overlay (the card's
        // current slate) rather than whatever happens to sort first.
        LINEMOVE.idx = 0;
        for (var i = 0; i < LINEMOVE.events.length; i++) {
          if (LINEMOVE.events[i].model) { LINEMOVE.idx = i; break; }
        }
        if (!LINEMOVE.events.length) {
          // present but empty -> clean empty state
          var s = $("linemove-select"); if (s) { s.hidden = true; }
          chartEmpty(el, "No line-movement data");
          return;
        }
        wireLineMoveSelect();
        drawLineMove();
      })
      .catch(function () {
        // 404 / blocked / malformed -> hide the whole block per spec.
        var blk = $("chart-linemove");
        if (blk) { blk.hidden = true; }
      });
  }

  // (b) CUMULATIVE STAKED --------------------------------------------------
  // Step-line of cumulative stake over time, one series per currency
  // (GBP solid, USD dashed), built from ts_utc + stake of ALL bets — open
  // positions plus closed ones — so the curve is true turnover and history
  // does not vanish as bets settle.
  var CUM_STYLE = {
    GBP: { color: "#4ade80", dash: "" },
    USD: { color: "#60a5fa", dash: "4 3" },
    EUR: { color: "#a78bfa", dash: "1 3" }
  };

  function drawCumStake(d) {
    var el = $("cumstake-canvas");
    if (!el) return;
    var pos = (d.positions || []).concat(d.closed_positions || []).filter(function (p) {
      return !isNaN(tsMs(p.ts_utc)) && !isNaN(Number(p.stake));
    });
    if (!pos.length) { chartEmpty(el, "No staked positions"); return; }

    // group by currency, sort by time, accumulate
    var byCcy = {};
    pos.forEach(function (p) {
      var ccy = p.currency || "GBP";
      (byCcy[ccy] = byCcy[ccy] || []).push({ t: tsMs(p.ts_utc), stake: Number(p.stake) });
    });
    var ccys = Object.keys(byCcy);
    var series = {}, tMin = Infinity, tMax = -Infinity, vMax = 0;
    ccys.forEach(function (ccy) {
      var arr = byCcy[ccy].slice().sort(function (a, b) { return a.t - b.t; });
      var cum = 0, steps = [];
      arr.forEach(function (e) {
        cum += e.stake;
        steps.push({ t: e.t, v: cum });
        tMin = Math.min(tMin, e.t); tMax = Math.max(tMax, e.t);
      });
      vMax = Math.max(vMax, cum);
      series[ccy] = steps;
    });
    if (!isFinite(tMin)) { chartEmpty(el, "No staked positions"); return; }
    var span = (tMax - tMin) || 1;
    var top = vMax > 0 ? vMax * 1.08 : 1;
    function scaleX(t) { return CHART_M.left + ((t - tMin) / span) * PLOT_W; }
    function scaleY(v) { return CHART_M.top + (1 - (v / top)) * PLOT_H; }

    var yLabels = [0, 0.5, 1].map(function (f) {
      return { y: scaleY(top * f), text: Math.round(top * f).toString() };
    });
    var svg = chartFrame(yLabels, timeTicks(tMin, tMax, scaleX));

    ccys.forEach(function (ccy) {
      var st = CUM_STYLE[ccy] || { color: "#9ca3af", dash: "" };
      var steps = series[ccy];
      // build a step (left-continuous) path: hold then rise
      var pts = [];
      pts.push(r2(scaleX(tMin)) + "," + r2(scaleY(0)));
      var prevV = 0;
      steps.forEach(function (s) {
        pts.push(r2(scaleX(s.t)) + "," + r2(scaleY(prevV)));
        pts.push(r2(scaleX(s.t)) + "," + r2(scaleY(s.v)));
        prevV = s.v;
      });
      pts.push(r2(scaleX(tMax)) + "," + r2(scaleY(prevV)));
      svg += '<polyline class="cx-series" stroke="' + st.color + '"' +
        (st.dash ? ' stroke-dasharray="' + st.dash + '"' : '') +
        ' points="' + pts.join(" ") + '"/>';
      // final value label at the right edge
      var fy = scaleY(prevV);
      svg += '<text class="cx-final" fill="' + st.color + '" x="' +
        r2(CHART_M.left + PLOT_W + 6) + '" y="' + r2(fy + 3) +
        '">' + esc(money(prevV, ccy)) + '</text>';
    });

    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Cumulative staked">' +
      svg + '</svg>';
  }

  // P&L // Realized: one step-line per pool. Sportsbook (£, solid green/red
  // by sign) and prediction markets combined (USD, dashed blue). Currencies
  // are separate lines, never summed; y-axis spans negative territory.
  function drawPnl(d) {
    var el = $("pnl-canvas");
    if (!el) return;
    var ps = d.pnl_series || {};
    var seriesDefs = [
      { key: "sportsbook", color: "#4ade80", dash: null, ccy: (ps.sportsbook || {}).currency || "GBP" },
      { key: "prediction_markets", color: "#60a5fa", dash: "4 3", ccy: (ps.prediction_markets || {}).currency || "USD" }
    ];
    var all = [];
    seriesDefs.forEach(function (sd) {
      sd.pts = (((ps[sd.key] || {}).points) || []).map(function (pt) {
        return { t: tsMs(pt[0]), v: Number(pt[1]) };
      }).filter(function (p) { return !isNaN(p.t) && !isNaN(p.v); });
      all = all.concat(sd.pts);
    });
    if (!all.length) { chartEmpty(el, "No realized P&L yet — appears after settlement"); return; }

    var tMin = Infinity, tMax = -Infinity, vMin = 0, vMax = 0;
    all.forEach(function (p) {
      tMin = Math.min(tMin, p.t); tMax = Math.max(tMax, p.t);
      vMin = Math.min(vMin, p.v); vMax = Math.max(vMax, p.v);
    });
    var pad = Math.max((vMax - vMin) * 0.15, 1);
    vMin -= pad; vMax += pad;
    var span = (tMax - tMin) || 1, vSpan = (vMax - vMin) || 1;
    function scaleX(t) { return CHART_M.left + ((t - tMin) / span) * PLOT_W; }
    function scaleY(v) { return CHART_M.top + (1 - ((v - vMin) / vSpan)) * PLOT_H; }

    var yLabels = [vMin, (vMin + vMax) / 2, vMax].map(function (v) {
      return { y: scaleY(v), text: v.toFixed(0) };
    });
    var svg = chartFrame(yLabels, timeTicks(tMin, tMax, scaleX));
    // zero line for sign orientation
    if (vMin < 0 && vMax > 0) {
      svg += '<line class="cx-grid" stroke-dasharray="2 3" x1="' + r2(CHART_M.left) +
        '" y1="' + r2(scaleY(0)) + '" x2="' + r2(CHART_M.left + PLOT_W) +
        '" y2="' + r2(scaleY(0)) + '"/>';
    }
    seriesDefs.forEach(function (sd) {
      if (!sd.pts.length) return;
      var arr = sd.pts.slice().sort(function (a, b) { return a.t - b.t; });
      var pts = [], prevV = 0;
      pts.push(r2(scaleX(tMin)) + "," + r2(scaleY(0)));
      arr.forEach(function (p) {
        pts.push(r2(scaleX(p.t)) + "," + r2(scaleY(prevV)));  // step
        pts.push(r2(scaleX(p.t)) + "," + r2(scaleY(p.v)));
        prevV = p.v;
      });
      pts.push(r2(scaleX(tMax)) + "," + r2(scaleY(prevV)));
      svg += '<polyline class="cx-series" stroke="' + sd.color + '"' +
        (sd.dash ? ' stroke-dasharray="' + sd.dash + '"' : '') +
        ' points="' + pts.join(" ") + '"/>';
      svg += '<text class="cx-final" fill="' + sd.color + '" x="' +
        r2(CHART_M.left + PLOT_W + 6) + '" y="' + r2(scaleY(prevV) + 3) +
        '">' + esc(signedMoney(prevV, sd.ccy)) + '</text>';
    });
    el.innerHTML = '<svg viewBox="0 0 ' + CHART_W + ' ' + CHART_H +
      '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Realized P&L">' +
      svg + '</svg>';
  }

  function renderCharts(d) {
    drawPnl(d);
    drawCumStake(d);
    loadLineMove();
  }

  function showNoData(msg) {
    var el = $("nodata");
    $("nodata-msg").textContent = msg || "NO DATA FEED";
    el.hidden = false;
  }

  function render(d) {
    renderTicker(d);
    renderVenues(d);
    renderPositions(d);
    renderClosedPositions(d);
    renderPredictions(d);
    renderCharts(d);
    renderFooter(d);
  }

  function renderClosedPositions(d) {
    var el = $("positions-closed");
    if (!el) return;
    var pos = d.closed_positions || [];
    var meta = $("positions-closed-meta");
    if (!pos.length) {
      el.innerHTML = '<div class="empty">No settled bets yet — P&L appears here after results</div>';
      if (meta) meta.textContent = "0";
      return;
    }
    // Per-currency realized totals for the panel meta (never summed across).
    var tot = {};
    pos.forEach(function (p) {
      var c = p.currency || "GBP";
      tot[c] = (tot[c] || 0) + Number(p.pl || 0);
    });
    if (meta) {
      meta.innerHTML = esc(pos.length + " settled · ") +
        Object.keys(tot).map(function (c) {
          return '<span class="' + (tot[c] >= 0 ? "pos" : "neg") + '">' +
            esc(signedMoney(tot[c], c)) + '</span>';
        }).join(" + ");
    }
    var rows = pos.map(function (p) {
      var pl = Number(p.pl);
      var plCls = p.status === "void" ? "dim" : (pl >= 0 ? "pos" : "neg");
      var plTxt = p.status === "void" ? "void" : signedMoney(pl, p.currency);
      return '<tr title="' + esc(metaTitle(p)) +
          '" style="border-left:2px solid ' + bookColor(p.platform) + '">' +
        '<td class="num pos-when" data-label="Settled">' + whenCell(p.settled_ts || p.ts_utc) + '</td>' +
        '<td class="pos-match" data-label="Match" title="' + esc(p.match) + '">' + esc(dash(p.match)) + '</td>' +
        '<td class="pos-mkt dim" data-label="Market" title="' + esc(p.market) + '">' + esc(marketShort(p.market)) + '</td>' +
        '<td class="pos-sel" data-label="Selection" title="' + esc(p.selection) + '">' + selDisplay(p) + '</td>' +
        '<td class="r num" data-label="Odds">' + esc(num(p.decimal_odds)) + '</td>' +
        '<td class="r num" data-label="Stake">' + esc(money(p.stake, p.currency)) + '</td>' +
        '<td class="r num" data-label="Model">' + esc(pct(p.model_prob, 0)) + '</td>' +
        '<td class="r num ' + evClass(p.ev) + '" data-label="EV">' +
          esc(p.ev === null || p.ev === undefined ? "—" : pct(p.ev, 1)) + '</td>' +
        '<td class="r num" data-label="Close">' + esc(num(p.closing_odds)) + '</td>' +
        '<td class="r num ' + plCls + '" data-label="P&L">' + esc(plTxt) + '</td>' +
        '<td class="r num ' + evClass(p.clv) + '" data-label="CLV">' +
          esc(p.clv === null || p.clv === undefined ? "—" : pct(p.clv, 1)) + '</td>' +
        '<td class="pos-src" data-label="Source">' + sourceChip(p.source) + '</td>' +
        '<td data-label="Venue"><span class="pill book ' + esc(p.venue || "sportsbook") +
          '" style="color:' + bookColor(p.platform) + ';border-color:' +
          bookColor(p.platform) + '">' + esc(p.platform || "") +
          accountSuffix(p.account) + '</span></td>' +
        overrideCell(p) +
        '</tr>';
    }).join("");
    el.innerHTML =
      '<table class="pos-table pos-table-wide">' +
        '<thead><tr>' +
          '<th>Settled</th><th>Match</th><th>Market</th><th>Selection</th>' +
          '<th class="r">Odds</th><th class="r">Stake</th>' +
          '<th class="r">Model</th><th class="r">EV</th>' +
          '<th class="r">Close</th><th class="r">P&L</th><th class="r">CLV</th>' +
          '<th>Source</th><th>Venue</th><th>Override</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>';
  }

  // ---- optional Polymarket enrichment (graceful, never required) ---------
  // Opportunistically fetch live prices for any open polymarket positions and
  // annotate their venue pill title with the latest price. Any failure (CORS,
  // offline, blocked) is swallowed — the page already rendered without it.

  function enrichPolymarket(d) {
    try {
      var hasPoly = (d.positions || []).some(function (p) {
        return (p.venue || "").toLowerCase() === "polymarket";
      });
      if (!hasPoly || typeof fetch !== "function") return;
      var url = "https://gamma-api.polymarket.com/markets?closed=false&limit=20";
      fetch(url, { mode: "cors" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (markets) {
          if (!markets || !markets.length) return;
          // Lightweight annotation only; do not disturb layout if shape
          // differs. We just tag the panel meta to show the feed is live.
          var meta = $("positions-meta");
          if (meta) { meta.textContent = meta.textContent + " · live"; }
        })
        .catch(function () { /* blocked/offline — ignore silently */ });
    } catch (e) { /* never let enrichment break the page */ }
  }

  // ---- boot ---------------------------------------------------------------

  function load(attempt) {
    attempt = attempt || 1;
    var url = "./data.json?t=" + Date.now();
    fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (!d || typeof d !== "object") throw new Error("bad payload");
        var nd = $("nodata");
        if (nd) nd.hidden = true; // success always clears the banner
        render(d);
        enrichPolymarket(d);
      })
      .catch(function (err) {
        // A load during a mid-deploy window can transiently 404 — retry with
        // backoff before declaring the feed down, and never strand the page.
        if (attempt < 4) {
          setTimeout(function () { load(attempt + 1); }, attempt * 2000);
          return;
        }
        showNoData("DATA FEED UNAVAILABLE");
        // Still render an all-zero shell so the terminal is never blank.
        render({ totals: {}, venues: {}, clv: {}, positions: [], predictions: [], meta: {} });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
