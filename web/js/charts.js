// Hand-built SVG charts. Every chart ships a hover/focus layer and a table twin.
import { el, svgEl, attachTooltip, TIERS, TIER_LABEL, TIER_VAR, fmtDate, fmtScore, tableTwin, legend } from './util.js';

const M = { top: 14, right: 12, bottom: 26, left: 34 };

function emptyChart(text) {
  return el('div', { class: 'chart-empty' }, text);
}

/* ------------------------------------------------------------------ */
/* Stacked tier composition per scoring run                            */
/* ------------------------------------------------------------------ */
export function tierComposition(points, { height = 210 } = {}) {
  if (!points || points.length === 0) return emptyChart('暂无打分运行记录');
  const container = el('div', { class: 'chart' });
  const W = 720;
  const H = height;
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const maxTotal = Math.max(1, ...points.map((p) => TIERS.reduce((s, t) => s + (p.counts[t] || 0), 0)));
  const band = innerW / points.length;
  const barW = Math.min(24, band * 0.56);
  const yScale = (v) => innerH - (v / maxTotal) * innerH;
  const tip = attachTooltip(container);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': '各次打分运行的风险分层客户数堆叠柱状图' });

  // gridlines: 4 steps, clean numbers
  const step = Math.max(1, Math.ceil(maxTotal / 4));
  const ticks = [];
  for (let v = 0; v <= maxTotal; v += step) ticks.push(v);
  for (const v of ticks) {
    const y = M.top + yScale(v);
    svg.appendChild(svgEl('line', { class: v === 0 ? 'baseline' : 'gridline',
      x1: M.left, x2: W - M.right, y1: y, y2: y }));
    svg.appendChild(svgEl('text', { class: 'axis-label', x: M.left - 7, y: y + 3.5, 'text-anchor': 'end' }, String(v)));
  }

  points.forEach((p, i) => {
    const x = M.left + band * i + (band - barW) / 2;
    let acc = 0;
    const total = TIERS.reduce((s, t) => s + (p.counts[t] || 0), 0);
    const segs = [];
    TIERS.forEach((t, ti) => {
      const n = p.counts[t] || 0;
      if (!n) return;
      const y0 = M.top + yScale(acc);
      const y1 = M.top + yScale(acc + n);
      const top = y1 + (ti > 0 ? 1 : 0); // 2px surface gap between segments
      const h = Math.max(1, y0 - top - (ti > 0 ? 1 : 0));
      const isTop = TIERS.slice(ti + 1).every((t2) => !(p.counts[t2] || 0));
      const r = isTop ? 4 : 0;
      segs.push(svgEl('path', {
        class: 'mark',
        d: roundedTopRect(x, top, barW, h, r),
        fill: TIER_VAR[t],
        tabindex: '0',
        role: 'img',
        'aria-label': `${fmtDate(p.as_of)} ${TIER_LABEL[t]} ${n} 家`,
      }));
      acc += n;
    });
    const g = svgEl('g', {}, segs);
    g.addEventListener('pointermove', (ev) => {
      const rect = container.getBoundingClientRect();
      tip.show(ev.clientX - rect.left, ev.clientY - rect.top, `${fmtDate(p.as_of, { withYear: true })} · 共 ${total} 家`, [
        ...TIERS.filter((t) => p.counts[t]).map((t) => ({
          name: TIER_LABEL[t], value: `${p.counts[t]} 家`, color: TIER_VAR[t],
        })),
        { name: '触发', value: triggerName(p.trigger) },
      ]);
      g.classList.add('focus');
    });
    g.addEventListener('pointerleave', () => tip.hide());
    g.addEventListener('focusin', () => {
      const rect = container.getBoundingClientRect();
      tip.show(M.left + band * i, 40, `${fmtDate(p.as_of, { withYear: true })} · 共 ${total} 家`,
        TIERS.filter((t) => p.counts[t]).map((t) => ({ name: TIER_LABEL[t], value: `${p.counts[t]} 家`, color: TIER_VAR[t] })));
    });
    g.addEventListener('focusout', () => tip.hide());
    svg.appendChild(g);

    // Label selectively: last point total only.
    if (i === points.length - 1) {
      svg.appendChild(svgEl('text', { class: 'value-label', x: x + barW / 2, y: M.top + yScale(total) - 6,
        'text-anchor': 'middle' }, String(total)));
    }
    const labelEvery = points.length > 9 ? 3 : points.length > 6 ? 2 : 1;
    if (i % labelEvery === 0 || i === points.length - 1) {
      svg.appendChild(svgEl('text', { class: 'axis-label', x: x + barW / 2, y: H - 8,
        'text-anchor': 'middle' }, fmtDate(p.as_of)));
    }
  });

  container.appendChild(svg);
  container.appendChild(tip.node);
  const table = tableTwin(
    ['数据截止', '触发', ...TIERS.map((t) => TIER_LABEL[t]), '合计'],
    points.map((p) => [fmtDate(p.as_of, { withYear: true }), triggerName(p.trigger),
      ...TIERS.map((t) => String(p.counts[t] || 0)),
      String(TIERS.reduce((s, t) => s + (p.counts[t] || 0), 0))]),
    { summary: '查看分层数据表', numeric: [2, 3, 4, 5, 6] },
  );
  return el('div', {}, container,
    legend(TIERS.map((t) => ({ label: TIER_LABEL[t], color: TIER_VAR[t] }))), table);
}

function triggerName(t) {
  return { initial: '初始', manual: '全量', features: '特征', merge: '合并', label: '标签', undo: '撤销' }[t] || t;
}

function roundedTopRect(x, y, w, h, r) {
  if (h <= 0) return '';
  const rr = Math.min(r, h, w / 2);
  if (!rr) return `M${x},${y}h${w}v${h}h${-w}Z`;
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

/* ------------------------------------------------------------------ */
/* Multi-series score lines with crosshair                             */
/* ------------------------------------------------------------------ */
const LINE_COLORS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)', 'var(--series-7)', 'var(--series-8)'];

export function scoreLines(series, { height = 250 } = {}) {
  if (!series || series.length === 0) return emptyChart('当前截止时间没有高风险客户的评分序列');
  const container = el('div', { class: 'chart' });
  const W = 720;
  const H = height;
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom - 8;
  const xVals = [...new Set(series.flatMap((s) => s.points.map((p) => p.as_of)))].sort();
  const xScale = (v) => (xVals.length === 1 ? innerW / 2 : (xVals.indexOf(v) / (xVals.length - 1)) * innerW);
  const yScale = (v) => innerH - (v / 100) * innerH;
  const tip = attachTooltip(container);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '高风险客户风险分随时间变化折线图' });
  for (const v of [0, 25, 50, 75, 100]) {
    const y = M.top + yScale(v);
    svg.appendChild(svgEl('line', { class: v === 0 ? 'baseline' : 'gridline', x1: M.left, x2: W - M.right, y1: y, y2: y }));
    svg.appendChild(svgEl('text', { class: 'axis-label', x: M.left - 7, y: y + 3.5, 'text-anchor': 'end' }, String(v)));
  }
  // threshold lines at 75/55 are tier boundaries; label them
  for (const [th, name] of [[75, '极高'], [55, '高']]) {
    const y = M.top + yScale(th);
    svg.appendChild(svgEl('text', { class: 'axis-label', x: W - M.right, y: y - 4, 'text-anchor': 'end' }, `${name} ${th}`));
  }
  xVals.forEach((x, i) => {
    if (xVals.length > 8 && i % 2 !== 0 && i !== xVals.length - 1) return;
    svg.appendChild(svgEl('text', { class: 'axis-label', x: M.left + xScale(x), y: H - 8, 'text-anchor': 'middle' }, fmtDate(x)));
  });

  const groups = [];
  series.forEach((s, si) => {
    const color = LINE_COLORS[si % LINE_COLORS.length];
    const pts = s.points.map((p) => [M.left + xScale(p.as_of), M.top + yScale(p.score)]);
    if (pts.length > 1) {
      svg.appendChild(svgEl('path', {
        d: pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]},${p[1]}`).join(''),
        fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
        class: 'mark',
      }));
    }
    const last = pts[pts.length - 1];
    svg.appendChild(svgEl('circle', { cx: last[0], cy: last[1], r: 4.5, fill: color,
      stroke: 'var(--surface-1)', 'stroke-width': 2 }));
    const g = svgEl('g', {});
    s.points.forEach((p, pi) => {
      g.appendChild(svgEl('circle', { cx: pts[pi][0], cy: pts[pi][1], r: 8, fill: 'transparent',
        class: 'hit', tabindex: '0', role: 'img',
        'aria-label': `${s.name} ${fmtDate(p.as_of)} 风险分 ${fmtScore(p.score)}` }));
      g.addEventListener('focusin', (ev) => {
        if (ev.target !== g && ev.target.tagName === 'circle') {
          const rect = container.getBoundingClientRect();
          tip.show(pts[pi][0], pts[pi][1], `${s.name} · ${fmtDate(p.as_of, { withYear: true })}`, [
            { name: '风险分', value: fmtScore(p.score), color },
            { name: '分层', value: TIER_LABEL[p.tier] },
          ]);
        }
      });
    });
    g.addEventListener('focusout', () => tip.hide());
    svg.appendChild(g);

    // direct label at line end when <= 4 series
    if (series.length <= 4 && last) {
      svg.appendChild(svgEl('text', { class: 'value-label secondary', x: last[0] + 8, y: last[1] + 3.5 }, s.name));
    }
  });

  // crosshair layer
  const hair = svgEl('line', { x1: 0, x2: 0, y1: M.top, y2: M.top + innerH, stroke: 'var(--text-muted)',
    'stroke-width': 1, 'stroke-dasharray': '0', opacity: 0 });
  svg.appendChild(hair);
  const hitRect = svgEl('rect', { x: M.left, y: M.top, width: innerW, height: innerH, class: 'hit' });
  svg.appendChild(hitRect);
  hitRect.addEventListener('pointermove', (ev) => {
    const rect = container.getBoundingClientRect();
    const scale = rect.width / W;
    const px = (ev.clientX - rect.left) / scale;
    let best = 0;
    let bestD = Infinity;
    xVals.forEach((x, i) => {
      const d = Math.abs(M.left + xScale(x) - px);
      if (d < bestD) { bestD = d; best = i; }
    });
    const x = xVals[best];
    hair.setAttribute('x1', M.left + xScale(x));
    hair.setAttribute('x2', M.left + xScale(x));
    hair.setAttribute('opacity', 0.55);
    const rows = series.map((s, si) => {
      const p = s.points.find((pp) => pp.as_of === x);
      return p ? { name: s.name, value: fmtScore(p.score), color: LINE_COLORS[si % LINE_COLORS.length] } : null;
    }).filter(Boolean);
    tip.show(ev.clientX - rect.left, ev.clientY - rect.top, `${fmtDate(x, { withYear: true })}`, rows);
    svg.querySelectorAll('.mark').forEach((m) => m.classList.remove('dim'));
  });
  hitRect.addEventListener('pointerleave', () => { tip.hide(); hair.setAttribute('opacity', 0); });
  container.appendChild(svg);
  container.appendChild(tip.node);

  const table = tableTwin(
    ['客户', ...xVals.map((x) => fmtDate(x))],
    series.map((s) => [s.name, ...xVals.map((x) => {
      const p = s.points.find((pp) => pp.as_of === x);
      return p ? fmtScore(p.score) : '—';
    })]),
    { summary: '查看评分序列表', numeric: xVals.map((_, i) => i + 1) },
  );
  return el('div', {}, container,
    legend(series.map((s, si) => ({ label: s.name, color: LINE_COLORS[si % LINE_COLORS.length], line: true }))), table);
}

/* ------------------------------------------------------------------ */
/* Evidence contribution bars (diverging around 0)                     */
/* ------------------------------------------------------------------ */
export function contributionBars(items, { height } = {}) {
  if (!items || items.length === 0) return emptyChart('没有可展示的证据');
  const container = el('div', { class: 'chart' });
  const rowH = 34;
  const W = 720;
  const H = height || Math.max(120, items.length * rowH + 40);
  const labelW = 150;
  const innerW = W - labelW - 16;
  const mid = labelW + innerW / 2;
  const maxAbs = Math.max(4, ...items.map((i) => Math.abs(i.contribution || 0)));
  const xScale = (v) => (v / maxAbs) * (innerW / 2 - 10);
  const tip = attachTooltip(container);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '各特征对风险分的贡献条形图' });

  svg.appendChild(svgEl('line', { class: 'baseline', x1: mid, x2: mid, y1: 8, y2: H - 24 }));
  items.forEach((it, i) => {
    const y = 10 + i * rowH;
    const val = it.contribution || 0;
    const w = Math.abs(xScale(val));
    const x = val >= 0 ? mid : mid - w;
    const isMissing = it.value == null;
    const color = isMissing ? 'var(--axis)' : val >= 0 ? 'var(--div-pos)' : 'var(--div-neg)';
    svg.appendChild(svgEl('text', { class: 'axis-label', x: labelW - 8, y: y + 15, 'text-anchor': 'end' },
      it.label));
    const bar = svgEl('path', {
      d: roundedEndRect(x, y, w, 16, val >= 0),
      fill: color, class: 'mark', tabindex: '0', role: 'img',
      'aria-label': `${it.label} 贡献 ${val.toFixed(1)} 分`,
    });
    if (isMissing) bar.setAttribute('opacity', '0.55');
    const showTip = (ev) => {
      const rect = container.getBoundingClientRect();
      const px = ev.clientX != null ? ev.clientX - rect.left : labelW + 40;
      const py = ev.clientY != null ? ev.clientY - rect.top : y;
      tip.show(px, py, it.label, isMissing
        ? [{ name: '缺失原因', value: it.missing_reason_label || '缺失' },
           { name: '未计入', value: '0.0 分' }]
        : [{ name: '当前值', value: it.display },
           { name: '贡献', value: `${val >= 0 ? '+' : ''}${val.toFixed(1)} 分`, color },
           { name: '权重', value: String(it.weight) },
           { name: '数据时间', value: fmtDate(it.effective_at, { withYear: true }) },
           { name: '可用时间', value: fmtDate(it.available_at, { withYear: true }) }]);
    };
    bar.addEventListener('pointermove', showTip);
    bar.addEventListener('pointerleave', () => tip.hide());
    bar.addEventListener('focus', () => showTip({}));
    bar.addEventListener('blur', () => tip.hide());
    svg.appendChild(bar);
    const labelX = val >= 0 ? x + w + 6 : x - 6;
    svg.appendChild(svgEl('text', { class: 'value-label', x: labelX, y: y + 12,
      'text-anchor': val >= 0 ? 'start' : 'end' },
      isMissing ? '缺失' : `${val >= 0 ? '+' : ''}${val.toFixed(1)}`));
  });
  svg.appendChild(svgEl('text', { class: 'axis-label', x: mid, y: H - 6, 'text-anchor': 'middle' },
    '← 降低风险          提高风险 →'));
  container.appendChild(svg);
  container.appendChild(tip.node);

  const table = tableTwin(
    ['特征', '当前值', '权重', '贡献分', '数据时间', '可用时间', '缺失原因'],
    items.map((it) => [it.label,
      it.value == null ? '缺失' : it.display,
      String(it.weight),
      (it.contribution || 0).toFixed(1),
      fmtDate(it.effective_at, { withYear: true }),
      fmtDate(it.available_at, { withYear: true }),
      it.missing_reason_label || '—']),
    { summary: '查看证据链数据表', numeric: [2, 3] },
  );
  return el('div', {}, container,
    legend([
      { label: '提高风险', color: 'var(--div-pos)' },
      { label: '降低风险', color: 'var(--div-neg)' },
      { label: '缺失（未计入）', color: 'var(--axis)' },
    ]), table);
}

function roundedEndRect(x, y, w, h, right) {
  const r = Math.min(4, w, h / 2);
  if (w <= 0.5) return `M${x},${y}h${Math.max(w, 1)}v${h}h${-Math.max(w, 1)}Z`;
  if (right) {
    return `M${x},${y}h${w - r}Q${x + w},${y} ${x + w},${y + r}v${h - 2 * r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
  }
  return `M${x + r},${y}H${x + w}V${y + h}H${x + r}Q${x},${y + h} ${x},${y + h - r}V${y + r}Q${x},${y} ${x + r},${y}Z`;
}

/* ------------------------------------------------------------------ */
/* Sparkline (stat tile trend)                                          */
/* ------------------------------------------------------------------ */
export function sparkline(values, { width = 108, height = 28, color = 'var(--series-1)' } = {}) {
  if (!values || values.length < 2) return el('span');
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [
    2 + (i / (values.length - 1)) * (width - 4),
    height - 3 - ((v - min) / span) * (height - 6),
  ]);
  const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true' },
    svgEl('path', { d: pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(''),
      fill: 'none', stroke: 'var(--text-muted)', 'stroke-width': 1.5, 'stroke-linecap': 'round',
      'stroke-linejoin': 'round' }),
    svgEl('circle', { cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1], r: 3,
      fill: color, stroke: 'var(--surface-1)', 'stroke-width': 1.5 }));
  return svg;
}

/* ------------------------------------------------------------------ */
/* Horizontal ranked bars (validation policy precision, missing reasons) */
/* ------------------------------------------------------------------ */
export function rankedBars(rows, { valueFormat = (v) => String(v), color = 'var(--series-1)',
  height, maxValue = null } = {}) {
  if (!rows || rows.length === 0) return emptyChart('暂无数据');
  const container = el('div', { class: 'chart' });
  const W = 720;
  const rowH = 38;
  const H = height || rows.length * rowH + 26;
  const labelW = 168;
  const innerW = W - labelW - 60;
  const max = maxValue || Math.max(0.0001, ...rows.map((r) => r.value || 0));
  const tip = attachTooltip(container);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': '排名条形图' });
  rows.forEach((r, i) => {
    const y = 8 + i * rowH;
    svg.appendChild(svgEl('text', { class: 'axis-label', x: labelW - 8, y: y + 15, 'text-anchor': 'end' }, r.label));
    const w = ((r.value || 0) / max) * innerW;
    const bar = svgEl('path', { d: roundedEndRect(labelW, y, w, 16, true),
      fill: r.color || color, class: 'mark', tabindex: '0', role: 'img',
      'aria-label': `${r.label} ${valueFormat(r.value)}` });
    const show = (ev) => {
      const rect = container.getBoundingClientRect();
      tip.show(ev.clientX != null ? ev.clientX - rect.left : labelW + w,
        ev.clientY != null ? ev.clientY - rect.top : y, r.label,
        (r.detail || [{ name: '数值', value: valueFormat(r.value) }]));
    };
    bar.addEventListener('pointermove', show);
    bar.addEventListener('pointerleave', () => tip.hide());
    bar.addEventListener('focus', () => show({}));
    bar.addEventListener('blur', () => tip.hide());
    svg.appendChild(bar);
    svg.appendChild(svgEl('text', { class: 'value-label', x: labelW + w + 7, y: y + 12 }, valueFormat(r.value)));
  });
  container.appendChild(svg);
  container.appendChild(tip.node);
  return container;
}
