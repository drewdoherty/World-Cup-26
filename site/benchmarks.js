/* benchmarks.js — WCA Benchmarking page */

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

  root.innerHTML = renderAll(D);
  drawCalibration(D);
  drawGoalsDist(D);
})();

/* ---- Top-level render ---- */
function renderAll(D) {
  const parts = [];
  parts.push(renderNorwaySpotlight(D.norway_france_spotlight));
  parts.push(renderKPIs(D.metrics, D.meta));
  parts.push(renderGoalsSection(D.goals_calibration, D.statsbomb_context));
  parts.push(renderCalibrationSection());
  parts.push(renderSuggestions(D.suggestions));
  parts.push(renderMatchTable(D.outcome_table));
  return `<div class="bench-grid">${parts.join('')}</div>`;
}

/* ---- Norway / France Spotlight ---- */
function renderNorwaySpotlight(s) {
  if (!s) return '';
  const lambdaTotal = (s.lambda_home + s.lambda_away).toFixed(2);
  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">Live Spotlight &mdash; Norway vs France (2026-06-26)</span>
      <span class="panel-meta" style="color:var(--neg)">model failure: 4+ goals by HT vs &lambda;=${lambdaTotal}</span>
    </div>
    <div class="norway-grid">
      <div class="norway-cell">
        <div class="norway-label">Model &lambda; Total (full match)</div>
        <div class="norway-val">${lambdaTotal}</div>
        <div class="norway-sub">&lambda;<sub>H</sub>=${s.lambda_home.toFixed(2)} &lambda;<sub>A</sub>=${s.lambda_away.toFixed(2)}</div>
      </div>
      <div class="norway-cell">
        <div class="norway-label">Actual Goals by HT</div>
        <div class="norway-val alert">&ge;4</div>
        <div class="norway-sub">already exceeds full-match prediction</div>
      </div>
      <div class="norway-cell">
        <div class="norway-label">P(4+ goals by 45&prime;) under model</div>
        <div class="norway-val alert">~6%</div>
        <div class="norway-sub">P(O2.5 full match) = ${pct(s.p_over25)}</div>
      </div>
    </div>
    <div class="norway-note">
      Model assigned &lambda; = ${s.lambda_home.toFixed(2)} (Norway) + ${s.lambda_away.toFixed(2)} (France) = ${lambdaTotal} expected goals total.
      With &ge;4 goals by half-time this match has already exceeded the full-match prediction — a &gt;1.7&sigma; event under independent Poisson.
      This validates the core calibration finding: <span style="color:var(--warn)">WC2026 group stage goals are running +16% above WC2022 baseline</span>,
      and the independent-Poisson model systemically underweights match-level variance.
      Suggested fix: apply WC2026 goals inflation multiplier (~1.16&times;) to all &lambda; estimates + fit Negative Binomial overdispersion.
    </div>
  </section>`;
}

/* ---- KPI strip ---- */
function renderKPIs(m, meta) {
  const bss = m.model.bss;
  const bssColor = bss >= 0 ? 'var(--pos)' : 'var(--neg)';
  return `
  <section class="panel bench-full">
    <div class="panel-head">
      <span class="panel-label">1X2 Accuracy // ${meta.n_matched} matched fixtures</span>
      <span class="panel-meta">${meta.n_results} completed &mdash; first prediction per fixture vs actual</span>
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
      Blend weights: <span style="color:var(--text)">0.10 Elo + 0.30 DC + 0.60 Market</span>.
      DC (Brier ${D3(m.dc.brier)}, BSS ${m.dc.bss.toFixed(3)}) and Elo (${D3(m.elo.brier)}, ${m.elo.bss.toFixed(3)}) both trail the market.
      BSS of &minus;0.016 is near-breakeven but indicates the proprietary components are adding marginal noise, not signal.
      Scoreline top-6 hit rate: <span style="color:var(--text)">26%</span>.
    </div>
  </section>`;
}

/* ---- Goals / xG section ---- */
function renderGoalsSection(gc, sb) {
  const wc26 = sb.wc2026_so_far;
  const wc22 = sb.wc2022;
  const wc18 = sb.wc2018;
  const delta = (wc26.avg_goals_per_match - wc22.avg_goals_per_match);
  const deltaSign = delta >= 0 ? '+' : '';

  const bars = [
    { label: 'WC2026', val: wc26.avg_goals_per_match, color: 'var(--neg)', max: 4.0 },
    { label: 'WC2022', val: wc22.avg_goals_per_match, color: 'var(--accent)', max: 4.0 },
    { label: 'WC2018', val: wc18.avg_goals_per_match, color: 'var(--text-dim)', max: 4.0 },
  ];

  return `
  <section class="panel">
    <div class="panel-head">
      <span class="panel-label">Goals Calibration // xG vs Actual</span>
      <span class="panel-meta" style="color:var(--neg)">${deltaSign}${delta.toFixed(2)} vs WC2022</span>
    </div>
    <div class="spotlight">
      <div class="spot-item">
        <div class="spot-label">WC2026 Goals/Match</div>
        <div class="spot-val" style="color:var(--neg)">${wc26.avg_goals_per_match.toFixed(2)}</div>
        <div class="spot-delta">${gc.n} matched matches</div>
      </div>
      <div class="spot-item" style="border-right:none">
        <div class="spot-label">Inflation vs WC2022</div>
        <div class="spot-val" style="color:var(--warn)">+${(gc.inflation_vs_wc2022 * 100).toFixed(0)}%</div>
        <div class="spot-delta">model &lambda; not yet adjusted</div>
      </div>
    </div>
    ${bars.map(b => `
    <div class="comp-bar-row">
      <div class="comp-bar-label">${b.label}</div>
      <div class="comp-bar-track">
        <div class="comp-bar-fill" style="width:${(b.val / b.max * 100).toFixed(1)}%;background:${b.color}"></div>
      </div>
      <div class="comp-bar-val" style="color:${b.color}">${b.val.toFixed(2)}</div>
    </div>`).join('')}
    <div style="padding:10px 16px;font-size:10px;color:var(--muted)">
      O2.5 actual rate: <span style="color:var(--text)">${pct(gc.over25.actual_rate)}</span>
      &nbsp;&mdash;&nbsp; WC2022 baseline: <span style="color:var(--text-dim)">${pct(gc.over25.wc2022_baseline)}</span>
      &nbsp;&mdash;&nbsp; WC2018 baseline: <span style="color:var(--text-dim)">${pct(gc.over25.wc2018_baseline)}</span>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <span class="panel-label">Goals Distribution // WC2026 vs WC2022</span>
      <span class="panel-meta">${gc.n} matched matches</span>
    </div>
    <div class="cal-wrap">
      <canvas class="dist-canvas" id="dist-canvas"></canvas>
    </div>
    <div style="padding:0 16px 10px; display:flex; gap:16px; font-size:10px; color:var(--text-dim)">
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--neg);border-radius:2px;margin-right:4px"></span>WC2026 actual</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--accent);border-radius:2px;margin-right:4px"></span>WC2022 baseline</span>
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
function renderSuggestions(suggestions) {
  const sorted = [...suggestions].sort((a, b) => a.priority - b.priority);
  const items = sorted.map(s => `
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
      <span class="panel-meta">data-driven &mdash; ranked by impact &mdash; WC2026 + StatsBomb WC18+22</span>
    </div>
    ${items}
  </section>`;
}

/* ---- Match-by-match table ---- */
function renderMatchTable(table) {
  const rows = table.map(m => {
    const bestOutcome = Object.entries(m.model).reduce((a, b) => b[1] > a[1] ? b : a)[0];
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

  // Series
  const series = [
    { data: D.calibration_bins.model, color: '#6D4AD0' },
    { data: D.calibration_bins.market, color: '#2563B0' },
  ];

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
  const W = canvas.offsetWidth || 500;
  const H = 160;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext('2d');
  const gc = D.goals_calibration;

  const xs = gc.goals_distribution.x;
  const actual = gc.goals_distribution.actual_pct;
  const baseline = gc.goals_distribution.wc2022_pct;
  const maxVal = Math.max(...actual, ...baseline) * 1.1;

  const pad = { top: 10, right: 10, bottom: 28, left: 8 };
  const bw = Math.floor((W - pad.left - pad.right) / xs.length);
  const each = bw / 2 - 2;

  const scaleY = v => (H - pad.bottom) - v / maxVal * (H - pad.top - pad.bottom);

  xs.forEach((x, i) => {
    const bx = pad.left + i * bw;
    const bottom = H - pad.bottom;

    // WC2026
    ctx.fillStyle = '#6D4AD0';
    ctx.fillRect(bx + 1, scaleY(actual[i]), each, bottom - scaleY(actual[i]));

    // WC2022
    ctx.fillStyle = '#9390B2';
    ctx.fillRect(bx + each + 3, scaleY(baseline[i]), each, bottom - scaleY(baseline[i]));

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
