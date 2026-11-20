// 验证与回溯: label-timing policy comparison, flips, label ledger, missing/late data.
import { api } from '../api.js';
import {
  el, fmtDate, labelValueLabel,
} from '../util.js';
import { rankedBars } from '../charts.js';

export async function render(ctx) {
  const { asOf } = ctx;
  const data = await api.validation(asOf);
  const root = el('div', {});
  root.appendChild(el('h1', {}, '验证与回溯'));
  root.appendChild(el('p', { class: 'lede' },
    '同一个告警库，用三种标签口径复核：复盘时当时能看到的、含迟到与回溯标签的、以及未剔除撤销标签的。口径差异就是“迟到标签”和“撤销”带来的信息价值与噪声。'));

  // --- policy comparison ------------------------------------------------
  const policyLabel = Object.fromEntries(data.policies.map((p) => [p.policy, p.label]));
  root.appendChild(el('div', { class: 'grid cols-3', style: { marginBottom: '8px' } },
    data.policies.map((p) => el('div', { class: 'card' },
      el('div', { class: 'stat' },
        el('span', { class: 'label' }, p.label),
        el('span', { class: 'value' }, p.precision == null ? '—' : `${(p.precision * 100).toFixed(0)}%`),
        el('span', { class: 'delta secondary small' },
          `命中 ${p.hit} · 漏报 ${p.miss} · 未标注 ${p.unlabeled}`),
        el('span', { class: 'tiny muted' }, p.note))))));

  root.appendChild(el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '口径切换带来的判定翻转'),
      el('span', { class: 'hint' }, `告警窗口按首次命中后 ${data.horizon_days} 天计算`))));

  if (!data.flips.length) {
    root.appendChild(el('div', { class: 'card empty' }, '在这个截止时间，三种口径的判定一致'));
  } else {
    const flipRows = data.flips.map((f) => el('tr', {},
      el('td', {}, el('a', { href: `#/customer/${f.customer_id}` }, `#${f.warning_id}`)),
      el('td', {}, f.customer),
      el('td', {}, el('span', { class: 'badge' }, verdictText(f.from))),
      el('td', {}, el('span', { class: `badge ${f.to === 'hit' ? 'good' : 'bad'}` }, verdictText(f.to))),
      el('td', { class: 'num small' }, f.delay_days == null ? '—' : `${f.delay_days} 天`),
      el('td', { class: 'small secondary' }, f.detail)));
    const flipTable = el('table', { class: 'data' },
      el('thead', {}, el('tr', {},
        el('th', {}, '告警'), el('th', {}, '客户'), el('th', {}, '当时口径'), el('th', {}, '结果口径'),
        el('th', {}, '标签延迟'), el('th', {}, '说明'))),
      el('tbody', {}, flipRows));
    root.appendChild(el('div', { class: 'card flush table-wrap' }, flipTable));
  }

  // --- label ledger -----------------------------------------------------
  const ledgerRows = data.labels.map((l) => el('tr', {},
    el('td', {}, el('a', { href: `#/customer/${l.customer_id}` }, l.customer)),
    el('td', {}, `v${l.label_version}`),
    el('td', {}, el('span', { class: `badge ${l.value === 'churned' ? 'bad' : l.value === 'retained' ? 'good' : ''}` },
      labelValueLabel(l.value))),
    el('td', { class: 'small nowrap' }, fmtDate(l.effective_at, { withYear: true })),
    el('td', { class: 'small nowrap' }, fmtDate(l.available_at, { withYear: true })),
    el('td', { class: 'num small' }, `${l.delay_days} 天`),
    el('td', {},
      el('div', { class: 'pill-row' },
        l.backdated ? el('span', { class: 'badge late' }, '回溯') : null,
        l.retracted ? el('span', { class: 'badge' }, '已撤销') : null,
        !l.known_at_as_of ? el('span', { class: 'badge' }, '截止后录入') : null)),
    el('td', { class: 'small' },
      l.effects.length
        ? l.effects.map((e) => {
          const outcome = e.outcome_retracted
            ? '结论已撤回'
            : e.outcome ? `结果 ${labelValueLabel(e.outcome)}` : '结果待定';
          return `反馈#${e.id}（${verdictText(e.verdict)} → ${outcome}）→ 告警#${e.warning_id}${e.warning_closed_at ? ' 已关闭' : ''}`;
        })
        : el('span', { class: 'muted' }, '无关联反馈')),
    el('td', { class: 'small muted' }, l.note || l.source)));

  root.appendChild(el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '标签台账'),
      el('span', { class: 'hint' }, '业务时间与进入系统时间的差，就是“迟到”；撤销会标注在旧版本上，不删除记录')),
    el('div', { class: 'card flush table-wrap' },
      el('table', { class: 'data compact' },
        el('thead', {}, el('tr', {},
          el('th', {}, '客户'), el('th', {}, '版本'), el('th', {}, '取值'), el('th', {}, '业务时间'),
          el('th', {}, '进入系统'), el('th', { class: 'num' }, '延迟'), el('th', {}, '标记'),
          el('th', {}, '影响'), el('th', {}, '备注'))),
        el('tbody', {}, ledgerRows)))));

  // --- missing data reasons --------------------------------------------
  const missingRows = data.missing.map((m) => ({
    label: m.label, value: m.n,
    detail: [{ name: '原因码', value: m.missing_reason }, { name: '特征综合数', value: `${m.n}` }],
  }));
  root.appendChild(el('div', { class: 'grid cols-2 section' },
    el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('div', { class: 'grow' },
        el('h2', {}, '缺失原因分布'),
        el('div', { class: 'sub' }, '缺失不是空白：每种缺失都带原因码，且不参与打分'))),
      missingRows.length ? rankedBars(missingRows, { maxValue: Math.max(1, ...missingRows.map((r) => r.value)) })
        : el('div', { class: 'empty' }, '当前截止时间没有缺失特征')),
    el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('div', { class: 'grow' },
        el('h2', {}, '迟到修订'),
        el('div', { class: 'sub' }, 'available_at 晚于 effective_at：业务上早已发生，但我们看到得晚'))),
      data.late_revisions.length ? el('div', { class: 'table-wrap' },
        el('table', { class: 'data compact' },
          el('thead', {}, el('tr', {},
            el('th', {}, '客户'), el('th', {}, '特征'), el('th', {}, '数据时间'),
            el('th', {}, '可用时间'), el('th', { class: 'num' }, '间隔'))),
          el('tbody', {}, data.late_revisions.slice(0, 10).map((r) => el('tr', {},
            el('td', {}, r.customer_name),
            el('td', { class: 'small' }, r.feature_label),
            el('td', { class: 'small nowrap' }, fmtDate(r.effective_at, { withYear: true })),
            el('td', { class: 'small nowrap' }, fmtDate(r.available_at, { withYear: true })),
            el('td', { class: 'num' }, el('span', { class: 'badge late' }, `${r.gap_days} 天`)))))))
        : el('div', { class: 'empty' }, '没有迟到修订'))));
  return root;
}

function verdictText(v) {
  return { hit: '命中', miss: '漏报', unknown: '未判定', valid: '有效', invalid: '误报', unsure: '不确定' }[v] || v;
}
