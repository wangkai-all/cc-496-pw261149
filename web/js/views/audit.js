// 运行审计: scoring runs, affected sets, changed tiers, jobs and reset.
import { api } from '../api.js';
import { el, fmtDate, toast, TIER_LABEL, triggerLabel } from '../util.js';

export async function render(ctx) {
  const [runs, jobs] = await Promise.all([api.runs(), api.jobs()]);
  const root = el('div', {});
  root.appendChild(el('h1', {}, '运行审计'));
  root.appendChild(el('p', { class: 'lede' },
    '每次特征修订、客户合并、标签回放都只重算受影响客户，并留下一条运行记录与一条任务记录。score 行不可变，历史预警仍可查询。'));

  root.appendChild(el('div', { class: 'grid cols-4', style: { marginBottom: '18px' } },
    tile('打分运行', runs.items.length, '全部保留'),
    tile('受影响重算', runs.items.filter((r) => r.scope === 'affected').length, '只算受影响客户'),
    tile('任务记录', jobs.items.length, '修订 / 合并 / 标签'),
    tile('分层变化', runs.items.reduce((s, r) => s + (r.changed?.length || 0), 0), '跨层客户次数'),
  ));

  // --- runs -------------------------------------------------------------
  const rows = runs.items.map((r) => el('tr', {},
    el('td', { class: 'nowrap' }, `#${r.id}`),
    el('td', { class: 'nowrap small' }, fmtDate(r.as_of, { withYear: true })),
    el('td', {}, el('span', { class: 'badge' }, triggerLabel(r.trigger))),
    el('td', {}, r.scope === 'full' ? '全量' : '受影响'),
    el('td', { class: 'num' }, `${r.scored_count}`),
    el('td', { class: 'num' }, `${r.skipped_count}`),
    el('td', {},
      r.changed?.length
        ? el('div', { class: 'pill-row' }, r.changed.map((c) =>
          el('a', { class: 'badge late', href: `#/customer/${c.customer_id}` },
            `#${c.customer_id} ${TIER_LABEL[c.prev_tier] || '—'}→${TIER_LABEL[c.tier]}`)))
        : el('span', { class: 'muted tiny' }, '无跨层变化')),
    el('td', { class: 'small muted' }, `v${r.feature_set_version}（${r.feature_set_name.replace(/^v\d+\s*/, '')}）`),
    el('td', { class: 'num small' }, `${r.duration_ms} ms`),
    el('td', { class: 'small muted' }, r.notes || '')));

  root.appendChild(el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '打分运行'),
      el('span', { class: 'hint' }, '按数据截止时间倒序；点击客户标签可查看当时的分层')),
    el('div', { class: 'card flush table-wrap' },
      el('table', { class: 'data compact' },
        el('thead', {}, el('tr', {},
          el('th', {}, '运行'), el('th', {}, '数据截止'), el('th', {}, '触发'), el('th', {}, '范围'),
          el('th', { class: 'num' }, '重算'), el('th', { class: 'num' }, '跳过'),
          el('th', {}, '跨层变化'), el('th', {}, '特征集'), el('th', { class: 'num' }, '耗时'),
          el('th', {}, '备注'))),
        el('tbody', {}, rows)))));

  // --- jobs -------------------------------------------------------------
  const jobRows = jobs.items.map((j) => {
    const res = j.result || {};
    const run = res.run || {};
    return el('tr', {},
      el('td', {}, `#${j.id}`),
      el('td', {}, el('span', { class: `badge ${j.kind === 'merge' ? 'late' : j.kind === 'label_replay' ? 'good' : ''}` }, jobKind(j.kind))),
      el('td', { class: 'small nowrap' }, fmtDate(j.started_at, { withYear: true, withTime: true })),
      el('td', {}, el('div', { class: 'pill-row' }, (j.affected || []).map((id) =>
        el('a', { class: 'badge', href: `#/customer/${id}` }, `#${id}`)))),
      el('td', { class: 'num' }, String(run.scored ?? '—')),
      el('td', { class: 'small' }, (run.changed || []).length
        ? `${(run.changed || []).length} 家跨层`
        : el('span', { class: 'muted' }, '无跨层变化')),
      el('td', { class: 'small muted' }, j.note || ''));
  });
  root.appendChild(el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '任务记录'),
      el('span', { class: 'hint' }, '修订 / 合并 / 标签回放各自生成的任务，包含受影响客户清单')),
    el('div', { class: 'card flush table-wrap' },
      el('table', { class: 'data compact' },
        el('thead', {}, el('tr', {},
          el('th', {}, '任务'), el('th', {}, '类型'), el('th', {}, '时间'), el('th', {}, '受影响客户'),
          el('th', { class: 'num' }, '重算'), el('th', {}, '变化'), el('th', {}, '备注'))),
        el('tbody', {}, jobRows)))));

  // --- actions ----------------------------------------------------------
  root.appendChild(el('div', { class: 'card section' },
    el('div', { class: 'card-head' }, el('div', { class: 'grow' },
      el('h2', {}, '运维操作'),
      el('div', { class: 'sub' }, '重置会从种子数据重新回放全部操作，用于演示和验收'))),
    el('div', { class: 'form-actions' },
      el('button', { class: 'primary', onclick: async () => {
        try {
          await api.runScoring({ trigger: 'manual', notes: '手动全量（审计页）' });
          await ctx.refresh();
          toast('已触发一次全量打分');
        } catch (e) { toast(e.message, true); }
      } }, '立即全量打分'),
      el('button', { onclick: async () => {
        if (!confirm('重置演示数据库？所有本地改动将丢失。')) return;
        try {
          await api.resetDemo();
          ctx.state.asOf = null;
          ctx.state.filters = { tier: '', segment: '', region: '', q: '' };
          await ctx.refresh();
          toast('演示数据已重置');
        } catch (e) { toast(e.message, true); }
      } }, '重置演示数据'))));
  return root;
}

function tile(label, value, sub) {
  return el('div', { class: 'card' }, el('div', { class: 'stat' },
    el('span', { class: 'label' }, label),
    el('span', { class: 'value' }, String(value)),
    el('span', { class: 'delta secondary small' }, sub)));
}

function jobKind(kind) {
  return { merge: '客户合并', label_replay: '标签回放', backfill: '特征回填', rescore: '重算' }[kind] || kind;
}
