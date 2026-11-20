// 客户档案: score, evidence chain, revision timeline, labels, warnings, actions.
import { api } from '../api.js';
import {
  el, fmtDate, fmtScore, fmtNum, TIER_LABEL, toast,
  closeReasonLabel, verdictLabel, labelValueLabel, fmtUnit,
} from '../util.js';
import { contributionBars, scoreLines } from '../charts.js';

export async function render(ctx) {
  const { asOf } = ctx;
  const id = ctx.state.customerId;
  if (!id) {
    const list = await api.portfolio(asOf);
    const root = el('div', {});
    root.appendChild(el('h1', {}, '客户档案'));
    root.appendChild(el('p', { class: 'lede' }, '选择一位客户查看证据链与数据时间。'));
    const rows = list.items.map((it) => {
      const openBtn = el('button', {
        class: 'sm',
        onclick: () => ctx.navigate('customer', { customerId: it.customer_id }),
      }, '打开');
      return el('tr', {},
        el('td', {}, it.name),
        el('td', {}, el('span', { class: 'chip' },
          el('span', { class: `dot ${it.tier}` }), TIER_LABEL[it.tier])),
        el('td', { class: 'num' }, fmtScore(it.score)),
        el('td', {}, openBtn));
    });
    root.appendChild(el('div', { class: 'card flush table-wrap' },
      el('table', { class: 'data' },
        el('thead', {}, el('tr', {},
          el('th', {}, '客户'), el('th', {}, '分层'),
          el('th', { class: 'num' }, '风险分'), el('th', {}, ''))),
        el('tbody', {}, rows))));
    return root;
  }

  const d = await api.customer(id, asOf);
  const root = el('div', {});
  const exp = d.explanation;
  const cur = d.entity_customer || d.customer;

  // --- header ----------------------------------------------------------
  const head = el('div', { class: 'card' },
    el('div', { class: 'cust-head' },
      el('div', { class: 'who' },
        el('h1', { style: { marginBottom: '2px' } },
          cur.name,
          d.entity !== d.customer.id ? el('span', { class: 'badge', style: { marginLeft: '8px' } }, `已合并自 #${d.customer.id}`) : null),
        el('div', { class: 'meta-row' },
          kv('客户 ID', `#${d.entity}`),
          kv('类型', cur.segment), kv('区域', cur.region),
          d.entity !== d.customer.id ? kv('状态', `已并入 #${d.entity}`) : null,
          kv('数据截止', fmtDate(asOf, { withYear: true })),
          kv('特征集', `v${exp.feature_set_version}`),
        ),
        d.merge_chain.length ? el('div', { class: 'warn-box', style: { marginTop: '10px' } },
          `合并链：${d.merge_chain.map((m) => `#${m.from} → #${m.to}（${fmtDate(m.effective_at)} 生效，${fmtDate(m.available_at)} 可知）`).join('；')}`,
          '。评分在该时点按合并后实体进行，历史分数不做改写。') : null,
        (d.aliases || []).length > 1
          ? el('div', { class: 'note', style: { marginTop: '8px' } },
            `当前实体包含 ${d.aliases.map((a) => `#${a.id} ${a.name}`).join('、')}；证据链取各账套修订的并集（同一特征取业务时间最新的一版）。`)
          : null,
      ),
      el('div', { class: 'scorebox' },
        el('div', { class: 'of' }, '风险分'),
        el('div', { class: 'score' }, fmtScore(exp.score)),
        el('div', {}, el('span', { class: 'chip', style: { marginTop: '4px' } },
          el('span', { class: `dot ${exp.tier}` }), TIER_LABEL[exp.tier])),
        d.latest && d.latest.tier_changed
          ? el('div', { class: 'tiny muted', style: { marginTop: '6px' } }, `上次为 ${TIER_LABEL[d.latest.prev_tier]}`)
          : null),
    ),
  );
  root.appendChild(head);

  // --- quality strip ----------------------------------------------------
  root.appendChild(el('div', { class: 'grid cols-3 section' },
    qualityCard('数据质量', [
      ['缺失特征', `${Object.keys(exp.missing).length} 个`],
      ['陈旧特征（>45 天）', `${exp.stale_features} 个`],
      ['迟到特征', `${exp.late_features} 个`],
    ]),
    qualityCard('标签', ((known) => known.length ? known.slice(-1).map((l) =>
      [labelValueLabel(l.value), l.backdated ? '回溯标签' : '常规标签']) : [['无标签', '—']])(d.labels.filter((l) => l.known_at_as_of)).concat([
      ['标签版本', `v${d.labels.filter((l) => l.known_at_as_of).length ? d.labels.filter((l) => l.known_at_as_of).slice(-1)[0].label_version : 0}`],
      ...(d.labels.some((l) => !l.known_at_as_of) ? [['截止后录入', `${d.labels.filter((l) => !l.known_at_as_of).length} 个版本（不计入当前判断）`]] : []),
    ])),
    qualityCard('告警', (() => {
      const open = d.warnings.find((w) => !w.closed_at);
      return [
        ['打开中', open ? `#${open.id}（${TIER_LABEL[open.tier]}）` : '无'],
        ['历史告警', `${d.warnings.length} 条`],
        ['反馈', `${d.warnings.reduce((s, w) => s + (w.feedback?.length || 0), 0)} 条`],
      ];
    })()),
  ));

  // --- evidence ---------------------------------------------------------
  const evidenceItems = buildEvidenceItems(d);
  const evCard = el('div', { class: 'card section' },
    el('div', { class: 'card-head' },
      el('div', { class: 'grow' },
        el('h2', {}, '证据链'),
        el('div', { class: 'sub' },
          `加权贡献 = 权重 × 方向 × 归一化偏离；合计 ${fmtScore(exp.score)} 分（基准 50 分）。数据时间取 effective_at，可用时间取 available_at。`)),
    ),
    contributionBars(evidenceItems),
  );
  root.appendChild(evCard);

  // evidence table with reasons and times
  const evTable = el('table', { class: 'data' },
    el('thead', {}, el('tr', {},
      el('th', {}, '特征'), el('th', {}, '取值'), el('th', { class: 'num' }, '权重'),
      el('th', { class: 'num' }, '贡献'), el('th', {}, '数据时间'), el('th', {}, '可用时间'),
      el('th', { class: 'num' }, '陈旧度'), el('th', {}, '状态'))),
    el('tbody', {}, evidenceItems.map((it) => {
      let statusCell = '正常';
      if (it.value == null) statusCell = el('span', { class: 'badge warn' }, it.missing_reason_label || '缺失');
      else if (it.late) statusCell = el('span', { class: 'badge late' }, `迟到 ${it.late_days} 天`);
      return el('tr', {},
        el('td', {}, it.label),
        el('td', { class: 'small' }, it.value == null ? '缺失' : it.display),
        el('td', { class: 'num' }, String(it.weight)),
        el('td', { class: 'num' }, `${it.contribution >= 0 ? '+' : ''}${it.contribution.toFixed(1)}`),
        el('td', { class: 'small' }, fmtDate(it.effective_at, { withYear: true })),
        el('td', { class: 'small' }, fmtDate(it.available_at, { withYear: true })),
        el('td', { class: 'num' }, it.stale_days == null ? '—' : `${it.stale_days} 天`),
        el('td', {}, statusCell));
    })));
  root.appendChild(el('div', { class: 'card flush table-wrap section' }, evTable));

  // --- score history ----------------------------------------------------
  if (d.history.length > 1) {
    const series = [{
      customer_id: d.entity,
      name: cur.name,
      points: d.history.map((h) => ({ as_of: h.as_of, score: h.score, tier: h.tier })),
    }];
    root.appendChild(el('div', { class: 'section' },
      el('div', { class: 'section-head' }, el('h2', {}, '评分轨迹'),
        el('span', { class: 'hint' }, '历史分数按当时可见数据计算，不随后来的修订回写')),
      el('div', { class: 'card' }, scoreLines(series, { height: 220 }))));
  }

  // --- revisions --------------------------------------------------------
  root.appendChild(revisionSection(d, asOf, ctx));

  // --- labels -----------------------------------------------------------
  root.appendChild(labelSection(d, asOf, ctx));

  // --- warnings ---------------------------------------------------------
  root.appendChild(warningSection(d, ctx));

  // --- action panel -----------------------------------------------------
  root.appendChild(actionPanel(d, ctx));

  return root;
}

function qualityCard(title, rows) {
  return el('div', { class: 'card' },
    el('h3', {}, title),
    el('div', { class: 'evidence-list' }, rows.map(([k, v]) =>
      el('div', { class: 'evidence-item', style: { padding: '4px 0' } },
        el('span', { class: 'label small' }, k),
        el('span', { style: { marginLeft: 'auto', fontWeight: 600 } }, v)))));
}

function kv(k, v) {
  return el('span', { class: 'kv' }, el('span', { class: 'k' }, k), el('span', {}, v));
}

function buildEvidenceItems(d) {
  const exp = d.explanation;
  return exp.evidence.map((it) => ({
    ...it,
    display: it.value == null ? '缺失' : fmtUnit(it.value, it.unit),
    missing_reason_label: it.missing_reason_label,
  }));
}

function revisionSection(d, asOf, ctx) {
  const rows = d.revisions.slice(0, 40).map((r) => {
    const status = r.status === 'superseded' ? '已被取代'
      : r.is_current_pick ? '当前采用' : '已在库';
    return el('tr', {},
      el('td', { class: 'nowrap' }, r.label),
      el('td', { class: 'nowrap' }, fmtDate(r.effective_at, { withYear: true })),
      el('td', { class: 'num nowrap' }, r.value == null ? '缺失' : fmtNum(r.value, 2)),
      el('td', { class: 'nowrap small' }, fmtDate(r.available_at, { withYear: true })),
      el('td', {},
        el('div', { class: 'pill-row' },
          r.is_current_pick ? el('span', { class: 'badge good' }, '当前采用') : null,
          r.is_late_for_as_of ? el('span', { class: 'badge late' }, '截止后到达') : null,
          r.status === 'superseded' ? el('span', { class: 'badge' }, '已被取代') : null,
          r.missing_reason ? el('span', { class: 'badge warn' }, reasonLabel(r.missing_reason)) : null)),
      el('td', { class: 'small muted' }, r.note || r.source));
  });
  const card = el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '特征修订历史'),
      el('span', { class: 'hint' }, `按 effective_at 倒序；当前截止 ${fmtDate(asOf)}，此后到达的修订标为“截止后到达”`)));
  card.appendChild(el('div', { class: 'card flush table-wrap' },
    el('table', { class: 'data compact' },
      el('thead', {}, el('tr', {},
        el('th', {}, '特征'), el('th', {}, '数据时间'), el('th', { class: 'num' }, '取值'),
        el('th', {}, '可用时间'), el('th', {}, '状态'), el('th', {}, '来源/备注'))),
      el('tbody', {}, rows))));
  return card;
}

function reasonLabel(code) {
  return {
    not_collected: '未采集', source_outage: '上游故障', below_maturity: '未满成熟窗口',
    opt_out: '客户拒绝', not_applicable: '业务不适用', pending_backfill: '等待回填',
  }[code] || code;
}

function labelSection(d, asOf, ctx) {
  const card = el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '标签与回溯'),
      el('span', { class: 'hint' }, '标签按版本追加；撤回旧版本不会删除历史')));
  if (!d.labels.length) {
    card.appendChild(el('div', { class: 'card empty' }, '暂无标签'));
    return card;
  }
  card.appendChild(el('div', { class: 'card flush table-wrap' },
    el('table', { class: 'data compact' },
      el('thead', {}, el('tr', {},
        el('th', {}, '版本'), el('th', {}, '取值'), el('th', {}, '业务时间'),
        el('th', {}, '进入系统'), el('th', {}, '延迟'), el('th', {}, '状态'), el('th', {}, '备注'))),
      el('tbody', {}, d.labels.map((l) => el('tr', {},
        el('td', {}, `v${l.label_version}`),
        el('td', {}, el('span', { class: `badge ${l.value === 'churned' ? 'bad' : l.value === 'retained' ? 'good' : ''}` }, labelValueLabel(l.value))),
        el('td', { class: 'nowrap small' }, fmtDate(l.effective_at, { withYear: true })),
        el('td', { class: 'nowrap small' }, fmtDate(l.available_at, { withYear: true })),
        el('td', { class: 'num small' }, delayDays(l)),
        el('td', {}, el('div', { class: 'pill-row' },
          l.retracted ? el('span', { class: 'badge' }, '已撤销') : null,
          l.backdated ? el('span', { class: 'badge late' }, '回溯') : null,
          !l.known_at_as_of ? el('span', { class: 'badge' }, '截止后录入') : null)),
        el('td', { class: 'small muted' }, l.note || l.source)))))));
  return card;
}

function delayDays(l) {
  const a = new Date(l.available_at).getTime();
  const b = new Date(l.effective_at).getTime();
  const d = Math.round((a - b) / 86400000);
  return Number.isNaN(d) ? '—' : `${d} 天`;
}

function warningSection(d, ctx) {
  const card = el('div', { class: 'section' },
    el('div', { class: 'section-head' }, el('h2', {}, '告警记录'),
      el('span', { class: 'hint' }, '去重：同一客户仅一条打开告警')));
  if (!d.warnings.length) {
    card.appendChild(el('div', { class: 'card empty' }, '该客户没有告警记录'));
    return card;
  }
  for (const w of d.warnings) {
    const successor = d.warnings.find((x) => x.supersedes === w.id);
    card.appendChild(el('div', { class: 'alert-item' },
      el('div', { class: 'head' },
        el('span', { class: 'chip' }, el('span', { class: `dot ${w.tier}` }), TIER_LABEL[w.tier]),
        el('div', { class: 'grow' },
          el('div', { style: { fontWeight: 600 } }, `告警 #${w.id}`),
          el('div', { class: 'tiny muted' },
            `${fmtDate(w.first_seen_at, { withYear: true, withTime: true })} → ${w.closed_at ? fmtDate(w.closed_at, { withYear: true, withTime: true }) : '持续中'} · 命中 ${w.occurrences} 次`)),
        w.closed_at ? el('span', { class: 'badge' }, closeReasonLabel(w.close_reason)) : el('span', { class: 'badge bad' }, '打开中')),
      w.feedback?.length ? el('div', { style: { marginTop: '6px' } }, w.feedback.map((f) =>
        el('div', { class: 'feedback-row' },
          el('span', { class: 'badge' }, verdictLabel(f.verdict)),
          el('div', { class: 'grow' }, f.note || '（无备注）',
            el('div', { class: 'tiny muted' }, f.by_user)),
          f.outcome ? el('span', { class: `badge ${f.outcome === 'churned' ? 'bad' : 'good'}` },
            f.outcome === 'churned' ? '结果：流失' : '结果：留存') : el('span', { class: 'badge' }, '结果待定')))) : null,
      successor
        ? el('div', { class: 'note', style: { marginTop: '8px' } },
          `该告警已由 #${successor.id} 接续${w.close_reason === 'dismissed' ? '：迟到的流失标签推翻了此前的误报判定' : ''}；历史记录保留可查。`)
        : w.closed_at && w.close_reason === 'dismissed'
          ? el('div', { class: 'warn-box', style: { marginTop: '8px' } }, '此告警以“误报”关闭；若之后到达的标签确认流失，告警中心会重新打开或新建接续告警。') : null,
    ));
  }
  return card;
}

function actionPanel(d, ctx) {
  const historical = ctx.asOf && ctx.bootstrap && ctx.asOf !== ctx.bootstrap.latest_as_of;
  const card = el('div', { class: 'card section action-panel' },
    el('div', { class: 'card-head' }, el('div', { class: 'grow' },
      el('h2', {}, '修订操作'),
      el('div', { class: 'sub' }, '每次操作都会生成独立运行与审计记录，只重算受影响的客户'))));
  if (historical) {
    card.appendChild(el('div', { class: 'warn-box' },
      `当前处于历史截止时间（${fmtDate(ctx.asOf, { withYear: true })}）：页面只读。新增修订、合并与标签回放会写入“现在”，请切回“最新”。`));
    return card;
  }

  const features = d.all_features || [];
  const featureSel = el('select', { 'aria-label': '选择特征' },
    features.map((f) => el('option', { value: f.code }, `${f.label}（${f.code}）`)));
  const valueInput = el('input', { type: 'number', step: '0.01', placeholder: '新取值（留空表示缺失）', 'aria-label': '新取值' });
  const reasonSel = el('select', { 'aria-label': '缺失原因' },
    el('option', { value: '' }, '缺失原因（取值留空时必填）'),
    el('option', { value: 'source_outage' }, '上游故障'),
    el('option', { value: 'not_collected' }, '未采集'),
    el('option', { value: 'below_maturity' }, '未满成熟窗口'),
    el('option', { value: 'opt_out' }, '客户拒绝'),
    el('option', { value: 'not_applicable' }, '业务不适用'),
    el('option', { value: 'pending_backfill' }, '等待回填'));
  const effInput = el('input', { type: 'datetime-local', 'aria-label': '数据时间' });
  const availInput = el('input', { type: 'datetime-local', 'aria-label': '可用时间' });
  const noteInput = el('input', { placeholder: '备注（可选）', 'aria-label': '修订备注' });
  const nowLocal = (plusMin = 0) => {
    const t = new Date(Date.now() + plusMin * 60000);
    const p = (n) => String(n).padStart(2, '0');
    return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}T${p(t.getHours())}:${p(t.getMinutes())}`;
  };
  effInput.value = nowLocal();
  availInput.value = nowLocal();

  card.appendChild(el('section', {},
    el('h3', {}, '新增特征修订'),
    el('div', { class: 'form-grid' },
      el('div', { class: 'full' }, el('label', {}, '特征'), featureSel),
      el('div', {}, el('label', {}, '新取值'), valueInput),
      el('div', {}, el('label', {}, '缺失原因'), reasonSel),
      el('div', {}, el('label', {}, '数据时间 effective_at'), effInput),
      el('div', {}, el('label', {}, '可用时间 available_at'), availInput),
      el('div', { class: 'full' }, el('label', {}, '备注'), noteInput)),
    el('div', { class: 'form-actions' },
      el('button', { class: 'primary', onclick: async () => {
        const value = valueInput.value === '' ? null : Number(valueInput.value);
        if (value == null && !reasonSel.value) {
          toast('取值留空时必须选择缺失原因', true);
          return;
        }
        try {
          const res = await api.reviseFeature({
            customer_id: d.entity,
            code: featureSel.value,
            value,
            missing_reason: value == null ? reasonSel.value : null,
            effective_at: new Date(effInput.value).toISOString().replace(/\.\d+Z$/, 'Z'),
            available_at: new Date(availInput.value).toISOString().replace(/\.\d+Z$/, 'Z'),
            note: noteInput.value || null,
          });
          await ctx.afterAction(`已写入修订 #${res.revision_id}，重算 ${res.run.scored} 家客户`);
        } catch (e) { toast(e.message, true); }
      } }, '提交修订并重算'),
      el('span', { class: 'form-note' }, '只重算该客户（合并后实体）。可用时间晚于数据截止的修订会显示为迟到。'))));

  card.appendChild(el('section', {},
    el('h3', {}, '标签回放 / 撤销'),
    labelForm(d, ctx)));

  card.appendChild(el('section', {},
    el('h3', {}, '客户合并'),
    mergeForm(d, ctx)));

  return card;
}

function labelForm(d, ctx) {
  const valueSel = el('select', { 'aria-label': '标签取值' },
    el('option', { value: 'churned' }, '流失'),
    el('option', { value: 'retained' }, '留存'),
    el('option', { value: 'unknown' }, '未知'));
  const effInput = el('input', { type: 'datetime-local', 'aria-label': '标签业务时间' });
  const availInput = el('input', { type: 'datetime-local', 'aria-label': '标签进入系统时间' });
  const noteInput = el('input', { placeholder: '备注（可选）', 'aria-label': '标签备注' });
  const retract = el('input', { type: 'checkbox', id: 'retract-box' });
  const p = (n) => String(n).padStart(2, '0');
  const nowLocal = () => {
    const t = new Date();
    return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}T${p(t.getHours())}:${p(t.getMinutes())}`;
  };
  effInput.value = nowLocal();
  availInput.value = nowLocal();
  const submit = async () => {
    try {
      const res = await api.replayLabel({
        customer_id: d.entity,
        value: valueSel.value,
        effective_at: new Date(effInput.value).toISOString().replace(/\.\d+Z$/, 'Z'),
        available_at: new Date(availInput.value).toISOString().replace(/\.\d+Z$/, 'Z'),
        retract: retract.checked,
        note: noteInput.value || null,
        source: 'manual_replay',
      });
      await ctx.afterAction(`标签 v${res.version} 已写入${res.backdated ? '（回溯）' : ''}`);
    } catch (e) { toast(e.message, true); }
  };
  return el('div', {},
    el('div', { class: 'form-grid' },
      el('div', {}, el('label', {}, '取值'), valueSel),
      el('div', {}, el('label', { for: 'retract-box' }, '撤销上一版本'),
        el('label', { style: { display: 'flex', gap: '6px', alignItems: 'center', color: 'var(--text-primary)' } },
          retract, el('span', { class: 'small' }, '撤回上一条标签（保留记录）'))),
      el('div', {}, el('label', {}, '业务时间 effective_at'), effInput),
      el('div', {}, el('label', {}, '进入系统 available_at'), availInput),
      el('div', { class: 'full' }, el('label', {}, '备注'), noteInput)),
    el('div', { class: 'form-actions' },
      el('button', { class: 'primary', onclick: submit }, '写入标签'),
      el('span', { class: 'form-note' }, '回溯 = 业务时间早于进入系统时间；验证页会展示它对命中判定的影响。')));
}

function mergeForm(d, ctx) {
  const candidates = (d.customers || []).filter((c) => c.id !== d.entity && !c.merged_into);
  const target = el('select', { 'aria-label': '并入目标客户' },
    candidates.map((c) => el('option', { value: c.id }, `#${c.id} ${c.name}`)));
  const reasonInput = el('input', { placeholder: '合并原因', 'aria-label': '合并原因' });
  const p = (n) => String(n).padStart(2, '0');
  const nowLocal = () => {
    const t = new Date();
    return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}T${p(t.getHours())}:${p(t.getMinutes())}`;
  };
  const eff = el('input', { type: 'datetime-local', 'aria-label': '合并生效时间' });
  eff.value = nowLocal();
  const activeMerges = (d.merges || []).filter((m) => !m.undone_at);
  return el('div', {},
    el('div', { class: 'form-grid' },
      el('div', {}, el('label', {}, '并入目标'), target),
      el('div', {}, el('label', {}, '生效时间'), eff),
      el('div', { class: 'full' }, el('label', {}, '原因'), reasonInput)),
    el('div', { class: 'form-actions' },
      el('button', { onclick: async () => {
        if (!target.value) { toast('没有可合并的目标客户', true); return; }
        try {
          const res = await api.merge({
            from_id: d.entity, to_id: Number(target.value),
            effective_at: new Date(eff.value).toISOString().replace(/\.\d+Z$/, 'Z'),
            reason: reasonInput.value || '人工合并',
          });
          await ctx.afterAction(`已合并：#${d.entity} → #${target.value}，重算 ${res.run.scored} 家`);
        } catch (e) { toast(e.message, true); }
      } }, `将 #${d.entity} 并入所选客户`)),
    activeMerges.length ? mergeList(activeMerges, ctx) : null,
  );
}

function mergeList(merges, ctx) {
  const rows = merges.map((m) => {
    const undoBtn = el('button', {
      class: 'sm ghost',
      onclick: async () => {
        try {
          await api.undoMerge({ merge_id: m.id });
          await ctx.afterAction(`已撤销合并 #${m.id}`);
        } catch (e) { toast(e.message, true); }
      },
    }, '撤销');
    return el('tr', {},
      el('td', {}, `#${m.from_customer} → #${m.to_customer}`),
      el('td', { class: 'small' }, fmtDate(m.effective_at, { withYear: true })),
      el('td', { class: 'small' }, fmtDate(m.available_at, { withYear: true })),
      el('td', {}, undoBtn));
  });
  return el('details', { class: 'disclosure' },
    el('summary', {}, `当前生效的合并（${merges.length}）`),
    el('div', { class: 'table-wrap' },
      el('table', { class: 'data compact' },
        el('thead', {}, el('tr', {},
          el('th', {}, '合并'), el('th', {}, '生效'), el('th', {}, '可用'), el('th', {}, ''))),
        el('tbody', {}, rows))));
}
