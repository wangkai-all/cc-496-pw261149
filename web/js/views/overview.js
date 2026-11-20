// 风险总览: KPI row, tier composition, score trends, risk customer table.
import { api } from '../api.js';
import { el, fmtScore, fmtDate, TIERS, TIER_LABEL, TIER_VAR } from '../util.js';
import { tierComposition, scoreLines, sparkline } from '../charts.js';

export async function render(ctx) {
  const { asOf } = ctx;
  const [portfolio, history, bootstrap] = await Promise.all([
    api.portfolio(asOf, ctx.state.filters),
    api.riskHistory(asOf),
    Promise.resolve(ctx.bootstrap),
  ]);

  const root = el('div', {});
  root.appendChild(el('h1', {}, '风险总览'));
  root.appendChild(el('p', { class: 'lede' },
    '每一层风险都附带证据链、数据时间与缺失原因。切换到历史数据截止时间，可看到当时系统“本应知道”的分层；迟到数据与标签不会被回灌到过去。'));

  // --- filter row (one row above everything it scopes) -----------------
  const filters = ctx.state.filters;
  const fTier = el('select', { 'aria-label': '按分层筛选' },
    el('option', { value: '' }, '全部分层'),
    ...bootstrap.tiers.map((t) => el('option', { value: t.code }, t.label)));
  fTier.value = filters.tier;
  const segments = [...new Set(bootstrap.customers.map((c) => c.segment))];
  const regions = [...new Set(bootstrap.customers.map((c) => c.region))];
  const fSegment = el('select', { 'aria-label': '按客户类型筛选' },
    el('option', { value: '' }, '全部客户类型'), ...segments.map((s) => el('option', { value: s }, s)));
  fSegment.value = filters.segment;
  const fRegion = el('select', { 'aria-label': '按区域筛选' },
    el('option', { value: '' }, '全部区域'), ...regions.map((r) => el('option', { value: r }, r)));
  fRegion.value = filters.region;
  const fQ = el('input', { type: 'search', placeholder: '搜索客户名称或 ID', 'aria-label': '搜索客户' });
  fQ.value = filters.q || '';
  const apply = () => {
    ctx.state.filters = { tier: fTier.value, segment: fSegment.value, region: fRegion.value, q: fQ.value };
    ctx.refresh();
  };
  for (const f of [fTier, fSegment, fRegion]) f.addEventListener('change', apply);
  fQ.addEventListener('keydown', (e) => { if (e.key === 'Enter') apply(); });
  fQ.addEventListener('search', apply);

  root.appendChild(el('div', { class: 'filters' },
    el('div', { class: 'field' }, el('label', {}, '分层'), fTier),
    el('div', { class: 'field' }, el('label', {}, '客户类型'), fSegment),
    el('div', { class: 'field' }, el('label', {}, '区域'), fRegion),
    el('div', { class: 'field grow' }, el('label', {}, '搜索'), fQ),
    el('div', { class: 'spacer' }),
    el('button', { class: 'ghost sm', onclick: () => {
      ctx.state.filters = { tier: '', segment: '', region: '', q: '' };
      ctx.refresh();
    } }, '清除筛选'),
  ));

  // --- KPI row ---------------------------------------------------------
  const counts = portfolio.counts;
  const total = portfolio.total || 1;
  const openAlerts = portfolio.open_alerts;
  const prevHist = history.points.length > 1 ? history.points[history.points.length - 2] : null;
  const atRisk = counts.critical + counts.high;
  const prevAtRisk = prevHist ? prevHist.counts.critical + prevHist.counts.high : null;

  const kpi = el('div', { class: 'kpi-row' });
  kpi.appendChild(el('div', { class: 'card' },
    el('div', { class: 'stat' },
      el('span', { class: 'label' }, '高风险客户'),
      el('span', { class: 'hero' }, String(atRisk)),
      el('div', { class: 'hero-sub' },
        el('span', { class: 'secondary small' }, `占在册 ${(atRisk / total * 100).toFixed(0)}%`),
        prevAtRisk != null ? el('span', { class: 'small' },
          prevAtRisk === atRisk ? el('span', { class: 'muted' }, '与上次持平')
            : el('span', { class: atRisk > prevAtRisk ? 'up' : 'down' },
              `${atRisk > prevAtRisk ? '▲' : '▼'} ${Math.abs(atRisk - prevAtRisk)} 家`)) : null,
      ),
      sparkline(history.points.map((p) => p.counts.critical + p.counts.high)),
    )));

  const tile = (label, value, sub) => el('div', { class: 'card' },
    el('div', { class: 'stat' },
      el('span', { class: 'label' }, label),
      el('span', { class: 'value' }, value),
      sub ? el('span', { class: 'delta' }, sub) : null));

  const openByTier = { critical: 0, high: 0 };
  for (const it of portfolio.items) {
    if (it.warning && it.warning.open) openByTier[it.warning.tier] = (openByTier[it.warning.tier] || 0) + 1;
  }
  kpi.appendChild(tile('打开中的告警', String(openAlerts),
    el('span', { class: 'secondary small' },
      `极高 ${openByTier.critical || 0} · 高 ${openByTier.high || 0}`)));
  kpi.appendChild(tile('含迟到数据的客户', String(portfolio.late_entities),
    el('span', { class: 'secondary small' }, '当前截止之后才到达、尚未计入的修订')));
  kpi.appendChild(tile('含缺失特征的客户', String(portfolio.missing_entities),
    el('span', { class: 'secondary small' }, '按原因记录，未计入评分')));
  kpi.appendChild(tile('本次截止时间', fmtDate(asOf),
    el('span', { class: 'secondary small' }, `${history.points.length} 个截止时点已入账`)));
  root.appendChild(kpi);

  // --- charts ----------------------------------------------------------
  const chartRow = el('div', { class: 'grid main-side section' });

  const compCard = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', { class: 'grow' },
        el('h2', {}, '分层构成随时间变化'),
        el('div', { class: 'sub' }, '每个数据截止时点的极高/高/中/低客户数（取该时点前最近一次评分）'),
      )),
    tierComposition(history.points),
  );
  chartRow.appendChild(compCard);

  const mixCard = el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', { class: 'grow' },
        el('h2', {}, '当前分层占比'),
        el('div', { class: 'sub' }, `${fmtDate(asOf)} 截止；打开告警 ${openAlerts} 条`),
      )));
  mixCard.appendChild(tierMix(counts, total));
  mixCard.appendChild(el('div', { class: 'rule' }));
  mixCard.appendChild(missingSummary(portfolio));
  chartRow.appendChild(mixCard);
  root.appendChild(chartRow);

  // --- customer table --------------------------------------------------
  const tableSection = el('div', { class: 'section' },
    el('div', { class: 'section-head' },
      el('h2', {}, '客户风险清单'),
      el('span', { class: 'hint' }, `按分层与分数排序，共 ${portfolio.items.length} 家`)));
  tableSection.appendChild(customerTable(portfolio.items, ctx));
  root.appendChild(tableSection);

  const trendSection = el('div', { class: 'section' },
    el('div', { class: 'section-head' },
      el('h2', {}, '风险变化'),
      el('span', { class: 'hint' }, '当前最高风险客户的评分轨迹；灰色阈值线为分层边界')));
  trendSection.appendChild(el('div', { class: 'card' }, scoreLines(history.series)));
  root.appendChild(trendSection);

  return root;
}

function tierMix(counts, total) {
  const bar = el('div', { class: 'split-bar', role: 'img',
    'aria-label': TIERS.map((t) => `${TIER_LABEL[t]} ${counts[t]} 家`).join('，') });
  for (const t of TIERS) {
    const n = counts[t] || 0;
    if (!n) continue;
    const pct = n / total;
    const seg = el('div', {
      class: `seg${t === 'critical' ? ' on-dark' : ''}`,
      style: { width: `${pct * 100}%`, background: TIER_VAR[t] },
      title: `${TIER_LABEL[t]} ${n} 家（${(pct * 100).toFixed(0)}%）`,
    }, pct > 0.1 ? `${Math.round(pct * 100)}%` : '');
    bar.appendChild(seg);
  }
  const legendRow = el('div', { class: 'legend', style: { marginTop: '10px' } },
    TIERS.map((t) => el('span', { class: 'item' },
      el('span', { class: 'swatch', style: { background: TIER_VAR[t] } }),
      `${TIER_LABEL[t]} ${counts[t] || 0}`)));
  return el('div', {}, bar, legendRow);
}

function missingSummary(portfolio) {
  const byReason = new Map();
  for (const it of portfolio.items) {
    for (const [code, m] of Object.entries(it.missing || {})) {
      const key = m.reason_label || m.reason || '未知';
      byReason.set(key, (byReason.get(key) || 0) + 1);
    }
  }
  const rows = [...byReason.entries()].sort((a, b) => b[1] - a[1]);
  return el('div', {},
    el('h3', {}, '缺失特征的原因分布'),
    rows.length === 0
      ? el('div', { class: 'empty' }, '当前截止时间没有缺失特征')
      : el('div', { class: 'pill-row' }, rows.map(([k, v]) =>
        el('span', { class: 'badge warn' }, `${k} · ${v}`))),
    el('div', { class: 'chart-note' }, '缺失特征不参与打分，但会出现在客户的证据链里，并标注来源与数据时间。'));
}

function customerTable(items, ctx) {
  if (!items.length) {
    return el('div', { class: 'card empty' }, '没有符合筛选条件的客户');
  }
  const rows = items.map((it) => {
    const w = it.warning;
    const alertCell = w
      ? el('span', { class: `badge ${w.open ? 'bad' : ''}` },
        w.open ? `打开 · ${w.occurrences} 次` : closeLabel(w.closed_reason))
      : el('span', { class: 'muted tiny' }, '无告警');
    const top = (it.top_evidence || [])[0];
    return el('tr', { class: 'clickable', tabindex: '0', onclick: () => ctx.navigate('customer', { customerId: it.customer_id }),
      onkeydown: (e) => { if (e.key === 'Enter') ctx.navigate('customer', { customerId: it.customer_id }); },
      'aria-label': `查看 ${it.name} 的客户档案` },
      el('td', {},
        el('div', { style: { fontWeight: 600 } }, it.name),
        el('div', { class: 'tiny muted' }, `#${it.customer_id} · ${it.segment} · ${it.region}`)),
      el('td', {}, el('span', { class: 'chip' },
        el('span', { class: `dot ${it.tier}` }), TIER_LABEL[it.tier])),
      el('td', { class: 'num' }, el('strong', {}, fmtScore(it.score))),
      el('td', { class: 'num small' },
        it.tier_changed ? el('span', { class: 'badge late' }, `较上次 ${TIER_LABEL[it.prev_tier]}`) : el('span', { class: 'muted' }, '—')),
      el('td', { class: 'small' },
        top ? `${topLabel(top)} ${top.contribution >= 0 ? '+' : ''}${top.contribution.toFixed(1)}`
          : el('span', { class: 'muted' }, '无可用特征')),
      el('td', {},
        el('div', { class: 'pill-row' },
          it.missing_count ? el('span', { class: 'badge warn' }, `缺失 ${it.missing_count}`) : null,
          it.stale_features ? el('span', { class: 'badge' }, `陈旧 ${it.stale_features}`) : null,
          it.late_features ? el('span', { class: 'badge late' }, `迟到 ${it.late_features}`) : null,
          !it.missing_count && !it.stale_features && !it.late_features
            ? el('span', { class: 'tiny muted' }, '数据齐全') : null)),
      el('td', {}, alertCell),
      el('td', { class: 'num tiny muted' }, fmtDate(it.as_of)),
    );
  });
  return el('div', { class: 'card flush table-wrap' },
    el('table', { class: 'data' },
      el('thead', {}, el('tr', {},
        el('th', {}, '客户'), el('th', {}, '分层'), el('th', { class: 'num' }, '风险分'),
        el('th', { class: 'num' }, '变化'), el('th', {}, '首要证据'), el('th', {}, '数据质量'),
        el('th', {}, '告警'), el('th', { class: 'num' }, '数据截止'))),
      el('tbody', {}, rows)));
}

function topLabel(ev) {
  const map = {
    logins_30d: '登录', support_tickets_30d: '工单', seat_utilization: '席位',
    invoice_overdue_days: '逾期', nps_survey: 'NPS',
  };
  return map[ev.code] || ev.code;
}

function closeLabel(r) {
  return { dismissed: '误报已关', resolved: '已关闭', duplicate: '重复', recovered: '已回落',
    merged: '已合并', retracted: '已撤销', outcome_confirmed: '已确认' }[r] || '已关闭';
}
