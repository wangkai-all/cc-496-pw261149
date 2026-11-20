// 版本对比: feature-set versions side by side, per-customer deltas.
import { api } from '../api.js';
import { el, fmtScore, fmtDate, TIER_LABEL, toast } from '../util.js';

export async function render(ctx) {
  const { asOf } = ctx;
  const data = await api.versions(asOf);
  const root = el('div', {});
  root.appendChild(el('h1', {}, '版本对比'));
  root.appendChild(el('p', { class: 'lede' },
    '同一批特征修订、同一个数据截止时间，仅切换特征集版本。对比可以回答“是数据变了，还是权重变了”。切换生效版本会记录在运行审计中。'));

  // --- version cards ----------------------------------------------------
  const cards = el('div', { class: 'grid cols-2', style: { marginBottom: '18px' } });
  for (const s of data.sets) {
    cards.appendChild(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', { class: 'grow' },
          el('h2', {}, s.name,
            el('span', { class: 'badge', style: { marginLeft: '8px' } }, `v${s.version}`)),
          el('div', { class: 'sub' }, s.notes)),
        s.active ? el('span', { class: 'badge good' }, '当前生效') : null),
      el('div', { class: 'pill-row', style: { marginBottom: '10px' } },
        Object.entries(s.weights).map(([code, w]) =>
          el('span', { class: 'badge' }, `${code} ${w}`))),
      s.active ? null : el('button', {
        class: 'sm', onclick: async () => {
          try {
            await api.activateSet(s.version);
            await ctx.afterAction(`特征集 v${s.version} 已设为生效版本`);
          } catch (e) { toast(e.message, true); }
        },
      }, `切换到 v${s.version}`)));
  }
  root.appendChild(cards);

  // --- diff table -------------------------------------------------------
  const versions = data.sets.map((s) => s.version);
  const rows = el('tbody', {}, data.rows.map((r) => {
    const cells = [];
    for (const v of versions) {
      const item = r.versions[String(v)] || r.versions[v];
      const chg = item.delta != null && item.delta !== 0;
      cells.push(el('td', { class: 'num' },
        el('div', {}, fmtScore(item.score)),
        el('div', { class: 'tiny' },
          chg ? el('span', { class: item.delta > 0 ? 'up' : 'down', style: { color: item.delta > 0 ? 'var(--div-pos)' : 'var(--good-ink)' } },
            `${item.delta > 0 ? '+' : ''}${item.delta.toFixed(1)}`) : el('span', { class: 'muted' }, '—'),
          item.tier_changed ? el('span', { class: 'badge late', style: { marginLeft: '6px' } },
            `→ ${TIER_LABEL[item.tier]}`) : null)));
    }
    return el('tr', {},
      el('td', {},
        el('div', { style: { fontWeight: 600 } }, r.name),
        el('div', { class: 'tiny muted' }, `#${r.customer_id}`)),
      ...cells);
  }));

  root.appendChild(el('div', { class: 'card flush table-wrap' },
    el('table', { class: 'data' },
      el('thead', {}, el('tr', {},
        el('th', {}, '客户'),
        ...data.sets.map((s) => el('th', { class: 'num' }, `${s.name}（v${s.version}）`)))),
      rows)));

  root.appendChild(el('p', { class: 'chart-note' },
    `数据截止 ${fmtDate(asOf, { withYear: true })}；差值以最早版本为基准。切换权重后，新的打分运行会按生效版本记录 feature_set_version。`));

  // --- detail for one customer -----------------------------------------
  const sel = el('select', { 'aria-label': '选择客户查看逐特征差异' },
    data.rows.map((r) => el('option', { value: r.customer_id }, `${r.name}（#${r.customer_id}）`)));
  const detail = el('div', {});
  const drawDetail = () => {
    const row = data.rows.find((r) => String(r.customer_id) === sel.value) || data.rows[0];
    detail.replaceChildren(versionEvidence(row, versions));
  };
  sel.addEventListener('change', drawDetail);
  root.appendChild(el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '逐特征贡献差异'),
      el('span', { class: 'hint' }, '同一客户在不同权重下的贡献分')),
    el('div', { class: 'filters' }, el('div', { class: 'field' }, el('label', {}, '客户'), sel)),
    detail));
  drawDetail();
  return root;
}

function versionEvidence(row, versions) {
  const first = row.versions[String(versions[0])] || row.versions[versions[0]];
  const last = row.versions[String(versions[versions.length - 1])] || row.versions[versions[versions.length - 1]];
  const codes = [...new Set([...Object.keys(first.evidence), ...Object.keys(last.evidence)])];
  const rows = codes.map((c) => {
    const a = first.evidence[c] ?? 0;
    const b = last.evidence[c] ?? 0;
    return el('tr', {},
      el('td', {}, c),
      el('td', { class: 'num' }, a.toFixed(1)),
      el('td', { class: 'num' }, b.toFixed(1)),
      el('td', { class: 'num' }, el('span', {
        style: { color: b - a > 0 ? 'var(--div-pos)' : b - a < 0 ? 'var(--good-ink)' : 'var(--text-muted)' },
      }, `${b - a > 0 ? '+' : ''}${(b - a).toFixed(1)}`)));
  });
  return el('div', { class: 'card flush table-wrap' },
    el('table', { class: 'data compact' },
      el('thead', {}, el('tr', {},
        el('th', {}, '特征'), el('th', { class: 'num' }, 'v' + versions[0]),
        el('th', { class: 'num' }, 'v' + versions[versions.length - 1]), el('th', { class: 'num' }, '差异'))),
      el('tbody', {}, rows)));
}
