// 告警中心: dedupe/close/reopen, feedback, warning event history.
import { api } from '../api.js';
import {
  el, fmtDate, fmtScore, toast, TIER_LABEL, closeReasonLabel, verdictLabel,
} from '../util.js';

export async function render(ctx) {
  const { asOf } = ctx;
  const status = ctx.state.alertStatus || 'open';
  const [data] = await Promise.all([api.warnings(asOf, status)]);
  const historical = asOf && ctx.bootstrap && asOf !== ctx.bootstrap.latest_as_of;
  const root = el('div', {});
  root.appendChild(el('h1', {}, '告警中心'));
  root.appendChild(el('p', { class: 'lede' },
    '同一客户只保留一条打开中的告警：重复命中会合并计数，而不是生成重复告警。关闭后仍可查询，也可手动重新打开。客户合并会关闭被并入方的告警。'));
  if (historical) {
    root.appendChild(el('div', { class: 'warn-box' },
      `当前按 ${fmtDate(asOf, { withYear: true })} 的数据截止查看历史告警：状态由事件日志重建，操作按钮已停用。切回“最新”即可反馈、关闭或重开。`));
  }

  const counts = data.counts;
  root.appendChild(el('div', { class: 'grid cols-4', style: { marginBottom: '18px' } },
    statTile('打开中', counts.open, `共 ${counts.open + counts.closed} 条历史告警`),
    statTile('已关闭', counts.closed, '全部保留可查'),
    statTile('误报关闭', counts.dismissed, '由运营判定'),
    statTile('去重命中', counts.deduped_events, '重复触发被合并进已有告警'),
  ));

  const seg = el('div', { class: 'segmented', role: 'tablist', 'aria-label': '告警状态' });
  for (const [code, label] of [['open', '打开中'], ['closed', '已关闭'], ['all', '全部']]) {
    seg.appendChild(el('button', {
      class: status === code ? 'active' : '', role: 'tab', 'aria-selected': status === code ? 'true' : 'false',
      onclick: () => { ctx.state.alertStatus = code; ctx.refresh(); },
    }, label));
  }
  root.appendChild(el('div', { class: 'filters', style: { marginTop: '4px' } }, seg));

  if (!data.items.length) {
    root.appendChild(el('div', { class: 'card empty' }, status === 'open' ? '当前没有打开中的告警' : '没有记录'));
    return root;
  }

  const list = el('div', {});
  for (const w of data.items) {
    list.appendChild(alertCard(w, ctx, historical));
  }
  root.appendChild(list);
  return root;
}

function statTile(label, value, sub) {
  return el('div', { class: 'card' },
    el('div', { class: 'stat' },
      el('span', { class: 'label' }, label),
      el('span', { class: 'value' }, String(value)),
      el('span', { class: 'delta secondary small' }, sub)));
}

function alertCard(w, ctx, historical = false) {
  const isOpen = !w.closed_at;
  const card = el('div', { class: 'alert-item' });

  card.appendChild(el('div', { class: 'head' },
    el('span', { class: 'chip' }, el('span', { class: `dot ${w.tier}` }), TIER_LABEL[w.tier]),
    el('div', { class: 'grow' },
      el('div', { style: { fontWeight: 650, fontSize: '15px' } },
        el('a', { href: `#/customer/${w.customer_id}` }, w.customer_name),
        el('span', { class: 'muted small', style: { marginLeft: '8px', fontWeight: 400 } },
          `告警 #${w.id} · ${w.segment} · ${w.region}`)),
      el('div', { class: 'tiny muted' },
        `首次命中 ${fmtDate(w.first_seen_at, { withYear: true, withTime: true })} · 最近命中 ${fmtDate(w.last_seen_at, { withYear: true, withTime: true })}`
        + (historical ? ` · 截至 ${fmtDate(ctx.asOf, { withYear: true })} ${isOpen ? '打开中' : '已关闭'}` : ''))),
    el('div', { style: { textAlign: 'right' } },
      el('div', { style: { fontSize: '24px', fontWeight: 650 } },
        w.current ? fmtScore(w.current.score) : '—'),
      el('div', { class: 'tiny muted' }, w.current ? `当前${TIER_LABEL[w.current.tier]}` : '无最新评分')),
  ));

  card.appendChild(el('div', { class: 'meta-row' },
    kv('去重键', w.dedupe_key),
    kv('累计命中', `${w.occurrences} 次`),
    kv('未关闭', isOpen ? '是' : '否'),
    isOpen ? null : kv('关闭原因', closeReasonLabel(w.close_reason)),
    isOpen ? null : kv('关闭时间', fmtDate(w.closed_at, { withYear: true, withTime: true })),
    w.supersedes ? kv('接续', `#${w.supersedes}`) : null,
  ));

  if (w.feedback && w.feedback.length) {
    card.appendChild(el('div', { style: { marginTop: '10px' } },
      el('div', { class: 'tiny muted', style: { marginBottom: '2px' } }, '反馈记录'),
      ...w.feedback.map((f) => feedbackRow(f))));
  }

  if (w.events && w.events.length) {
    card.appendChild(el('details', { class: 'disclosure' },
      el('summary', {}, `时间线（${w.events.length} 条事件）`),
      el('ul', { class: 'timeline' },
        w.events.map((ev) => el('li', { class: `k-${ev.kind}` },
          el('span', { class: 'at' }, fmtDate(ev.at, { withYear: true, withTime: true })),
          el('span', { class: 'kind' }, kindLabel(ev.kind)),
          ev.detail)))));
  }

  // --- actions ---
  if (historical) {
    card.appendChild(el('div', { class: 'actions' },
      el('span', { class: 'tiny muted', style: { alignSelf: 'center' } },
        '历史截止时间下只读；切回“最新”可记录反馈或改变告警状态。')));
    return card;
  }
  const actions = el('div', { class: 'actions' });
  if (isOpen) {
    const note = el('input', { placeholder: '反馈备注（可选）', 'aria-label': '反馈备注', style: { minWidth: '190px' } });
    const send = async (verdict) => {
      try {
        await api.feedback(w.id, { verdict, note: note.value || null, user: '运营台' });
        await ctx.afterAction(`已记录“${verdictLabel(verdict)}”反馈`, { stayOffsets: true });
      } catch (e) { toast(e.message, true); }
    };
    actions.appendChild(el('span', { class: 'small secondary', style: { alignSelf: 'center' } }, '反馈：'));
    for (const v of ['valid', 'invalid', 'unsure']) {
      actions.appendChild(el('button', { class: 'sm', onclick: () => send(v) }, verdictLabel(v)));
    }
    actions.appendChild(note);
    actions.appendChild(el('span', { style: { width: '10px' } }));
    for (const [reason, label] of [['dismissed', '关闭（误报）'], ['resolved', '关闭（已处理）'], ['duplicate', '标记重复']]) {
      actions.appendChild(el('button', {
        class: 'sm ghost',
        onclick: async () => {
          try {
            await api.closeWarning(w.id, { reason, user: '运营台', note: note.value || null });
            await ctx.afterAction(`告警 #${w.id} 已关闭：${settleLabel(reason)}`, { stayOffsets: true });
          } catch (e) { toast(e.message, true); }
        },
      }, label));
    }
  } else {
    actions.appendChild(el('button', {
      class: 'sm',
      onclick: async () => {
        try {
          await api.reopenWarning(w.id, { user: '运营台' });
          await ctx.afterAction(`告警 #${w.id} 已重新打开`, { stayOffsets: true });
        } catch (e) { toast(e.message, true); }
      },
    }, '重新打开'));
    actions.appendChild(el('span', { class: 'tiny muted', style: { alignSelf: 'center' } },
      '关闭记录永久保留；重新打开会以同一条告警继续累计命中。'));
  }
  card.appendChild(actions);
  return card;
}

function feedbackRow(f) {
  const outcome = f.outcome
    ? el('span', { class: `badge ${f.outcome === 'churned' ? 'bad' : 'good'}` },
      `结果：${f.outcome === 'churned' ? '流失' : '留存'}${f.outcome_retracted ? '（已撤回）' : ''}`)
    : el('span', { class: 'badge' }, '结果待定');
  return el('div', { class: 'feedback-row' },
    el('span', { class: 'badge' }, verdictLabel(f.verdict)),
    el('div', { class: 'grow' },
      el('div', {}, f.note || '（无备注）'),
      el('div', { class: 'tiny muted' }, `${f.by_user} · ${fmtDate(f.at, { withYear: true, withTime: true })}`)),
    outcome);
}

function kv(k, v) {
  return el('span', { class: 'kv' }, el('span', { class: 'k' }, k), el('span', {}, v));
}

function kindLabel(kind) {
  return {
    opened: '打开', refreshed: '状态刷新', closed: '关闭', reopened: '重开', deduped: '去重命中',
  }[kind] || kind;
}

function settleLabel(reason) {
  return { dismissed: '误报', resolved: '已处理', duplicate: '重复' }[reason] || reason;
}
