/* benchmarks.js — WCA Benchmarking page
 *
 * Renders the JSON emitted by scripts/build_benchmarks.py verbatim — every
 * number on this page is read from D (the fetched JSON), never hard-coded.
 * (Regression, 2026-07: this file used to render a hand-authored JSON shape
 * with fields the builder never emits — a one-off "Norway/France spotlight",
 * a fabricated priority-sorted suggestions list, StatsBomb-vs-WC2026 baseline
 * deltas that were typed in by hand — so rebuilding the data never actually
 * updated the page's narrative. The builder's real schema is the only
 * source of truth now.)
 */

(async function () {
  const root = document.getElementById('bench-root');

  let D;
  try {
    const r = await fetch('./benchmarks_data.json');
    D = await r.json();
  } catch (e) {
    root.innerHTML = `<div class="bench-loading">Failed to load benchmark data: ${e.message}</div>`;
    return;
  }

  if (D.error || !D.metrics) {
    const n = (D.meta && D.meta.n) || 0;
    root.innerHTML = `<div class="bench-loading">No benchmark data yet (n=${n}). Run scripts/build_benchmarks.py once fixtures have settled.</div>`;
    return;
  }

  root.innerHTML = renderAll(D);
  drawCalibration(D);
  drawGoalsDist(D);
})();

/* ---- Top-level render ---- */
function renderAll(D) {
  const parts = [];
  parts.push(renderKPIs(D.metrics, D.meta));
  parts.push(renderGoalsSection(D.goals_calibration, D.wc26_summary, D.statsbomb_context));
  parts.push(renderCalibrationSection());
  if (D.suggestions && D.suggestions.length) parts.push(renderSuggestions(D.suggestions));
  parts.push(renderMatchTable(D.outcome_table || []));
  return `<div class="bench-grid">${parts.join('')}</div>`;
}

/* ---- KPI strip ---- */
function renderKPIs(m, meta) {
  const bss = m.model.bss;
  const bssColor = bss >= 0 ? 'var(--pos)' : 'var(--neg)';

  // Best-performing proprietary component vs market, derived from the data
  // (never hard-coded): whichever of elo/dc/model has the LEAST-negative (or
  // most-positive) BSS is "closest to / beating the market".
  const components = ['elo', 'dc', 'model']
    .filter((k) => m[k] && typeof m[k].bss === 'number')
    .map((k) => ({ key: k, bss: m[k].bss, brier: m[k].brier }));
  const best = components.length
    ? components.reduce((a, b) => (b.bss > a.bss ? b : a))
    : null;
  const labelFor = { elo: 'Elo', dc: 'DC', model: 'Blend' };

  const narrativeBits = [];
  if (components.length) {
    const others = components.filter((c) => c.key !== best.key);
    if (others.length) {
      const othersTxt = others
        .map((c) => `${labelFor[c.key]} (Brier ${D3(c.brier)}, BSS ${D3(c.bss)})`)
        .join(' and ');
      narrativeBits.push(`${othersTxt} trail the market.`.replace(/^(\w)/, (c) => c.toUpperCase()));
    }
    const bssTxt = `BSS of ${bss >= 0 ? '+' : ''}${D3(bss)}`;
    const verdict = bss > 0.02
      ? 'beats the market — the blend is adding positive value.'
      : bss < -0.02
        ? 'trails the market — the proprietary components are adding net noise, not signal.'
        : 'is near-breakeven with the market.';
    narrativeBits.push(`${bssTxt} for the deployed blend ${verdict}`);
  }

  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">1X2 Accuracy // ${meta.n_matched} matched fixtures</span>
      <span class="panel-meta">${meta.n_results} settled results total &mdash; first prediction per fixture vs actual</span>
    </div>
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">Model Brier</div>
        <div class="kpi-val">${D3(m.model.brier)}</div>
        <div class="kpi-sub">lower is better</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Market Brier</div>
        <div class="kpi-val">${D3(m.market.brier)}</div>
        <div class="kpi-sub">benchmark</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Brier Skill Score</div>
        <div class="kpi-val" style="color:${bssColor}">${bss >= 0 ? '+' : ''}${D3(bss)}</div>
        <div class="kpi-sub">model vs market &mdash; &gt;0 = beats market</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Model Accuracy</div>
        <div class="kpi-val">${pct(m.model.accuracy)}</div>
        <div class="kpi-sub">${m.model.n} matches</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Market Accuracy</div>
        <div class="kpi-val">${pct(m.market.accuracy)}</div>
        <div class="kpi-sub">same top-pick</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Model Log-Loss</div>
        <div class="kpi-val">${D3(m.model.logloss)}</div>
        <div class="kpi-sub">market: ${D3(m.market.logloss)}</div>
      </div>
    </div>
    <div style="padding:0 16px 12px; font-size:11px; color:var(--text-dim); line-height:1.5">
      ${narrativeBits.join(' ')}
    </div>
  </section>`;
}

/* ---- Goals / xG section ---- */
function renderGoalsSection(gc, wc26, sb) {
  if (!gc || !gc.n) {
    return `
    <section class="panel">
      <div class="panel-head"><span class="panel-label">Goals Calibration // xG vs Actual</span></div>
      <div class="bench-loading">No fixtures with logged &lambda; yet (n=0) &mdash; lambda persistence started 2026-06-26.</div>
    </section>`;
  }

  const wc22 = sb && sb.wc2022;
  const wc18 = sb && sb.wc2018;
  const wc26Avg = wc26 && wc26.matches ? wc26.avg_goals_per_match : gc.mean_actual_total;
  const wc26N = wc26 && wc26.matches ? wc26.matches : gc.n;

  const bars = [{ label: 'WC2026', val: wc26Avg, color: 'var(--neg)' }];
  if (wc22) bars.push({ label: 'WC2022', val: wc22.avg_goals_per_match, color: 'var(--accent)' });
  if (wc18) bars.push({ label: 'WC2018', val: wc18.avg_goals_per_match, color: 'var(--text-dim)' });
  const maxBar = Math.max(4.0, ...bars.map((b) => b.val));

  const calibPct = gc.calibration_factor_total != null
    ? ((gc.calibration_factor_total - 1) * 100)
    : null;

  return `
  <section class="panel">
    <div class="panel-head">
      <span class="panel-label">Goals Calibration // &lambda; vs Actual (n=${gc.n})</span>
      ${calibPct != null ? `<span class="panel-meta" style="color:${calibPct >= 0 ? 'var(--neg)' : 'var(--text-dim)'}">${calibPct >= 0 ? '+' : ''}${calibPct.toFixed(1)}% vs model &lambda;</span>` : ''}
    </div>
    <div class="spotlight">
      <div class="spot-item">
        <div class="spot-label">Actual Goals/Match</div>
        <div class="spot-val" style="color:var(--neg)">${gc.mean_actual_total.toFixed(2)}</div>
        <div class="spot-delta">${gc.n} matched matches (&lambda; logged)${wc26N !== gc.n ? ` &middot; ${wc26N} total settled` : ''}</div>
      </div>
      <div class="spot-item" style="border-right:none">
        <div class="spot-label">Model &lambda; Total (mean)</div>
        <div class="spot-val" style="color:var(--warn)">${gc.mean_lambda_total.toFixed(2)}</div>
        <div class="spot-delta">${calibPct != null ? `calibration factor ${gc.calibration_factor_total.toFixed(2)}x` : 'n/a'}</div>
      </div>
    </div>
    ${bars.map((b) => `
    <div class="comp-bar-row">
      <div class="comp-bar-label">${b.label}</div>
      <div class="comp-bar-track">
        <div class="comp-bar-fill" style="width:${Math.min(100, (b.val / maxBar) * 100).toFixed(1)}%;background:${b.color}"></div>
      </div>
      <div class="comp-bar-val" style="color:${b.color}">${b.val.toFixed(2)}</div>
    </div>`).join('')}
    <div style="padding:10px 16px;font-size:10px;color:var(--muted)">
      O2.5 actual rate: <span style="color:var(--text)">${pct(gc.mean_ou25_actual)}</span>
      &nbsp;&mdash;&nbsp; model-predicted: <span style="color:var(--text-dim)">${pct(gc.mean_ou25_pred)}</span>
      ${wc22 ? `&nbsp;&mdash;&nbsp; WC2022 baseline: <span style="color:var(--text-dim)">${pct(wc22.over25_rate)}</span>` : ''}
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <span class="panel-label">Goals Distribution // Actual vs Model-Expected (Poisson)</span>
      <span class="panel-meta">${gc.n} matched matches</span>
    </div>
    <div class="cal-wrap">
      <canvas class="dist-canvas" id="dist-canvas"></canvas>
    </div>
    <div style="padding:0 16px 10px; display:flex; gap:16px; font-size:10px; color:var(--text-dim)">
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--neg);border-radius:2px;margin-right:4px"></span>Actual</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--accent);border-radius:2px;margin-right:4px"></span>Model-expected (Poisson &lambda;)</span>
    </div>
  </section>`;
}

/* ---- Calibration chart section ---- */
function renderCalibrationSection() {
  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">Reliability Diagram // 1X2 Probability Calibration</span>
      <span class="panel-meta">binned predicted vs actual hit rate &mdash; diagonal = perfect</span>
    </div>
    <div class="cal-wrap">
      <canvas class="cal-canvas" id="cal-canvas"></canvas>
    </div>
    <div style="padding:0 16px 10px; display:flex; gap:16px; font-size:10px; color:var(--text-dim)">
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--accent);border-radius:2px;margin-right:4px"></span>Model</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--polymarket);border-radius:2px;margin-right:4px"></span>Market (de-vigged)</span>
      <span style="color:var(--muted)">Dashed diagonal = perfect calibration</span>
    </div>
  </section>`;
}

/* ---- Improvement suggestions ---- */
const _SEVERITY_RANK = { high: 0, medium: 1, low: 2, info: 3 };

function renderSuggestions(suggestions) {
  // The builder (scripts/build_benchmarks.py) does not emit a `priority`
  // field — sort by severity instead (high first), stable on insertion order
  // within a tier, so this never crashes on real builder output.
  const sorted = [...suggestions].sort((a, b) => {
    const ra = _SEVERITY_RANK[a.severity] ?? 99;
    const rb = _SEVERITY_RANK[b.severity] ?? 99;
    return ra - rb;
  });
  const items = sorted.map((s) => `
    <div class="suggestion">
      <div class="sug-badge ${s.severity}">${s.severity}</div>
      <div class="sug-body">
        <div class="sug-cat">${s.category}</div>
        <div class="sug-title">${s.title}</div>
        <div class="sug-stat">${s.stat}</div>
        <div class="sug-detail">${s.detail}</div>
      </div>
    </div>`).join('');
  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">Modelling Improvement Suggestions</span>
      <span class="panel-meta">data-driven &mdash; ranked by severity &mdash; WC2026 + StatsBomb WC18+22</span>
    </div>
    ${items}
  </section>`;
}

/* ---- Match-by-match table ---- */
function renderMatchTable(table) {
  if (!table.length) {
    return `
    <section class="panel bench-full">
      <div class="panel-head"><span class="panel-label">Match-by-Match Results</span></div>
      <div class="bench-loading">No matched fixtures yet.</div>
    </section>`;
  }
  const rows = table.map(m => {
    const correct = m.model_correct === 1;
    const brier = m.model_brier;
    const brierCls = brier < 0.1 ? 'brier-good' : brier > 0.45 ? 'brier-bad' : 'brier-neutral';
    const edge = m.model_edge_brier;
    const edgeSign = edge >= 0 ? '+' : '';
    const edgeCls = edge < 0 ? 'brier-good' : edge > 0.02 ? 'brier-bad' : 'brier-neutral';
    return `
    <tr>
      <td title="${m.fixture}">${m.fixture}</td>
      <td>${m.score}</td>
      <td class="${correct ? 'correct' : 'wrong'}">${correct ? '✓' : '✗'}</td>
      <td>${fmtProb(m.model.home)}</td>
      <td>${fmtProb(m.model.draw)}</td>
      <td>${fmtProb(m.model.away)}</td>
      <td class="${brierCls} num">${D3(brier)}</td>
      <td class="${edgeCls} num">${edgeSign}${D3(edge)}</td>
    </tr>`;
  }).join('');

  const n = table.length;
  const nCorrect = table.filter(m => m.model_correct === 1).length;
  const avgBrier = (table.reduce((s, m) => s + m.model_brier, 0) / n).toFixed(3);
  const avgEdge = (table.reduce((s, m) => s + m.model_edge_brier, 0) / n);
  const edgeSign = avgEdge >= 0 ? '+' : '';

  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">Match-by-Match Results // First Prediction per Fixture</span>
      <span class="panel-meta">${nCorrect}/${n} correct &mdash; avg Brier ${avgBrier} &mdash; model edge vs market ${edgeSign}${avgEdge.toFixed(3)}</span>
    </div>
    <div style="overflow-x:auto">
      <table class="match-table">
        <thead>
          <tr>
            <th>Fixture</th>
            <th>Score</th>
            <th>Hit</th>
            <th>P(H)</th>
            <th>P(D)</th>
            <th>P(A)</th>
            <th>Brier</th>
            <th>&Delta;Mkt</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div style="padding:8px 16px;font-size:10px;color:var(--muted)">
      &Delta;Mkt = model Brier minus market Brier (negative = model beats market on that match).
      Model predictions from first build per fixture in data/model_predictions_log.jsonl.
    </div>
  </section>`;
}

/* ---- Canvas: Calibration ---- */
function drawCalibration(D) {
  const canvas = document.getElementById('cal-canvas');
  if (!canvas) return;
  const W = canvas.offsetWidth || 800;
  const H = 220;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');

  const pad = { top: 14, right: 20, bottom: 30, left: 38 };
  const cW = W - pad.left - pad.right;
  const cH = H - pad.top - pad.bottom;

  const scaleX = v => pad.left + v * cW;
  const scaleY = v => pad.top + (1 - v) * cH;

  // Grid & diagonal
  ctx.strokeStyle = '#DCD3EE';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (i / 4) * cH;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    const x = pad.left + (i / 4) * cW;
    ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, H - pad.bottom); ctx.stroke();
  }
  // Diagonal
  ctx.strokeStyle = '#C8C3E4';
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(scaleX(0), scaleY(0)); ctx.lineTo(scaleX(1), scaleY(1)); ctx.stroke();
  ctx.setLineDash([]);

  // Axes labels
  ctx.fillStyle = '#9390B2';
  ctx.font = '10px ui-monospace, monospace';
  ctx.textAlign = 'center';
  for (let v = 0; v <= 1; v += 0.25) {
    ctx.fillText(v.toFixed(2), scaleX(v), H - pad.bottom + 14);
  }
  ctx.textAlign = 'right';
  for (let v = 0; v <= 1; v += 0.25) {
    ctx.fillText(v.toFixed(2), pad.left - 5, scaleY(v) + 4);
  }

  const calBins = D.calibration_bins || {};
  // Series
  const series = [
    { data: calBins.model, color: '#6D4AD0' },
    { data: calBins.market, color: '#2563B0' },
  ].filter((s) => Array.isArray(s.data) && s.data.length);

  series.forEach(({ data, color }) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    data.forEach((b, i) => {
      const x = scaleX(b.mean_pred);
      const y = scaleY(b.hit_rate);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    data.forEach(b => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(scaleX(b.mean_pred), scaleY(b.hit_rate), 4, 0, Math.PI * 2);
      ctx.fill();
    });
  });
}

/* ---- Canvas: Goals distribution ---- */
function drawGoalsDist(D) {
  const canvas = document.getElementById('dist-canvas');
  if (!canvas) return;
  const gc = D.goals_calibration;
  if (!gc || !gc.n || !gc.dist_goals) return;

  const W = canvas.offsetWidth || 500;
  const H = 160;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');

  const xs = gc.dist_goals;
  const actual = gc.dist_actual;
  const expected = gc.dist_expected;
  const maxVal = Math.max(...actual, ...expected) * 1.1;

  const pad = { top: 10, right: 10, bottom: 28, left: 8 };
  const bw = Math.floor((W - pad.left - pad.right) / xs.length);
  const each = bw / 2 - 2;

  const scaleY = v => (H - pad.bottom) - v / maxVal * (H - pad.top - pad.bottom);

  xs.forEach((x, i) => {
    const bx = pad.left + i * bw;
    const bottom = H - pad.bottom;

    // Actual
    ctx.fillStyle = '#6D4AD0';
    ctx.fillRect(bx + 1, scaleY(actual[i]), each, bottom - scaleY(actual[i]));

    // Model-expected
    ctx.fillStyle = '#9390B2';
    ctx.fillRect(bx + each + 3, scaleY(expected[i]), each, bottom - scaleY(expected[i]));

    // X label
    ctx.fillStyle = '#9390B2';
    ctx.font = '10px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(x, bx + bw / 2, H - 8);
  });

  // Axis
  ctx.strokeStyle = '#DCD3EE';
  ctx.beginPath();
  ctx.moveTo(pad.left, H - pad.bottom);
  ctx.lineTo(W - pad.right, H - pad.bottom);
  ctx.stroke();
}

/* ---- Helpers ---- */
function D3(v) { return v.toFixed(3); }
function pct(v) { return (v * 100).toFixed(1) + '%'; }
function fmtProb(v) { return (v * 100).toFixed(0) + '%'; }
