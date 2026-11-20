// Shared helpers: DOM building (no innerHTML with data), formatting, dates.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') node.innerHTML = v; // only for static, trusted markup
    else if (k in node && k !== 'list' && typeof v !== 'string') node[k] = v;
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else if (c instanceof Node) node.appendChild(c);
    else node.appendChild(document.createTextNode(String(c)));
  }
}

export const svgEl = (tag, attrs = {}, ...children) => {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
};

export const TIERS = ['critical', 'high', 'medium', 'low'];
export const TIER_LABEL = { critical: '极高风险', high: '高风险', medium: '中风险', low: '低风险' };
export const TIER_VAR = {
  critical: 'var(--tier-critical)',
  high: 'var(--tier-high)',
  medium: 'var(--tier-medium)',
  low: 'var(--tier-low)',
};

export function fmtNum(v, digits = 1) {
  if (v == null || Number.isNaN(v)) return '—';
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function fmtPct(v, digits = 0) {
  if (v == null) return '—';
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

export function fmtScore(v) {
  return v == null ? '—' : Number(v).toFixed(1);
}

export function fmtUnit(value, unit) {
  if (value == null) return '缺失';
  if (unit === '%') return `${(value * 100).toFixed(1)}%`;
  return `${fmtNum(value, 1)}${unit || ''}`;
}

// "2026-10-21T00:00:00Z" -> "10-21" / "2026-10-21 08:00"
export function fmtDate(ts, { withTime = false, withYear = false } = {}) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const p = (n) => String(n).padStart(2, '0');
  const date = `${withYear ? d.getFullYear() + '-' : ''}${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return withTime ? `${date} ${p(d.getHours())}:${p(d.getMinutes())}` : date;
}

export function relDays(ts, ref) {
  if (!ts || !ref) return null;
  const a = new Date(ts).getTime();
  const b = new Date(ref).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((a - b) / 86400000);
}

export function daysAgo(ts, ref) {
  const d = relDays(ref, ts);
  if (d == null) return null;
  return d;
}

export function verdictLabel(v) {
  return { valid: '有效', invalid: '误报', unsure: '不确定' }[v] || v;
}
export function closeReasonLabel(r) {
  return {
    dismissed: '人工判定误报',
    resolved: '已处理关闭',
    duplicate: '重复告警',
    recovered: '风险回落自动关闭',
    merged: '客户合并关闭',
    retracted: '标签撤销关闭',
    outcome_confirmed: '结果确认',
  }[r] || r || '—';
}
export function triggerLabel(t) {
  return {
    initial: '初始全量',
    manual: '手动全量',
    features: '特征修订',
    merge: '客户合并',
    label: '标签回溯',
    undo: '撤销合并',
  }[t] || t;
}
export function labelValueLabel(v) {
  return { churned: '流失', retained: '留存', unknown: '未知' }[v] || v;
}

export function debounce(fn, ms = 250) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function toast(msg, isError = false) {
  const node = document.getElementById('toast');
  if (!node) return;
  node.textContent = msg;
  node.classList.toggle('error', !!isError);
  node.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.remove('show'), 3200);
}

// --- tooltip plumbing shared by charts ------------------------------------
export function attachTooltip(container) {
  const tip = el('div', { class: 'tooltip', role: 'tooltip' });
  container.appendChild(tip);
  return {
    node: tip,
    show(x, y, title, rows) {
      tip.replaceChildren();
      if (title) tip.appendChild(el('div', { class: 'tt-title' }, title));
      for (const r of rows) {
        const key = el('span', { class: 'k' });
        if (r.color) key.style.background = r.color;
        tip.appendChild(
          el('div', { class: 'tt-row' },
            r.color ? key : null,
            el('span', { class: 'n' }, r.name),
            el('span', { class: 'v' }, r.value)),
        );
      }
      const rect = container.getBoundingClientRect();
      const tw = tip.offsetWidth || 160;
      const th = tip.offsetHeight || 60;
      let left = x + 14;
      if (left + tw > rect.width - 4) left = Math.max(4, x - tw - 14);
      let top = y - th - 10;
      if (top < 0) top = y + 18;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      tip.classList.add('show');
    },
    hide() { tip.classList.remove('show'); },
  };
}

export function chartCaption(text) {
  return el('div', { class: 'chart-note' }, text);
}

export function legend(items) {
  return el('div', { class: 'legend', role: 'list' },
    items.map((i) => el('span', { class: 'item', role: 'listitem' },
      el('span', {
        class: i.line ? 'line-key' : 'swatch',
        style: { background: i.color },
        'aria-hidden': 'true',
      }),
      i.label)));
}

// Table twin for accessibility: every chart gets one.
export function tableTwin(headers, rows, { summary = '查看数据表', numeric = [] } = {}) {
  return el('details', { class: 'table-twin' },
    el('summary', {}, summary),
    el('div', { class: 'table-wrap' },
      el('table', { class: 'data compact' },
        el('thead', {}, el('tr', {}, headers.map((h, i) =>
          el('th', { class: numeric.includes(i) ? 'num' : '' }, h)))),
        el('tbody', {}, rows.map((r) => el('tr', {}, r.map((c, i) =>
          el('td', { class: numeric.includes(i) ? 'num' : '' }, c))))))));
}
