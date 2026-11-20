// App shell: routing (hash), data loading, tab wiring.
import { api, state, effectiveAsOf } from './api.js';
import { el, fmtDate, toast } from './util.js';
import * as overview from './views/overview.js';
import * as alerts from './views/alerts.js';
import * as customer from './views/customer.js';
import * as versions from './views/versions.js';
import * as validation from './views/validation.js';
import * as audit from './views/audit.js';

const VIEWS = { overview, alerts, customer, versions, validation, audit };
const app = document.getElementById('app');

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [view, id] = raw.split('/');
  return { view: VIEWS[view] ? view : 'overview', id };
}

export function navigate(view, params = {}) {
  const parts = [view];
  if (params.customerId) parts.push(params.customerId);
  location.hash = `#/${parts.join('/')}`;
}

function setActiveTab(view) {
  for (const b of document.querySelectorAll('#tabs button')) {
    const on = b.dataset.view === view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-current', on ? 'page' : 'false');
  }
}

async function render() {
  const { view, id } = parseHash();
  state.view = view;
  if (view === 'customer' && id) state.customerId = Number(id);
  setActiveTab(view);
  app.classList.add('loading');
  app.replaceChildren(el('div', { class: 'muted', style: { padding: '40px 0' } }, '正在载入…'));
  try {
    const ctx = {
      asOf: effectiveAsOf(),
      bootstrap: state.bootstrap,
      state,
      navigate,
      refresh: render,
      afterAction: async (msg, opts = {}) => {
        toast(msg);
        await refreshBootstrap();
        if (opts.stayOffsets) {
          const y = window.scrollY;
          await render();
          window.scrollTo(0, y);
        } else {
          await render();
        }
      },
    };
    const node = await VIEWS[view].render(ctx);
    app.replaceChildren(node);
    if (VIEWS[view].afterRender) VIEWS[view].afterRender(ctx);
  } catch (err) {
    console.error(err);
    app.replaceChildren(el('div', { class: 'card' },
      el('h2', {}, '加载失败'),
      el('p', { class: 'secondary' }, err.message),
      el('button', { class: 'primary', onclick: () => render() }, '重试')));
  } finally {
    app.classList.remove('loading');
  }
}

async function refreshBootstrap() {
  state.bootstrap = await api.bootstrap();
  fillAsOfSelect();
}

function fillAsOfSelect() {
  const sel = document.getElementById('asof-select');
  const b = state.bootstrap;
  if (!b) return;
  const opts = (b.as_of_options || []).slice().reverse();
  sel.replaceChildren(
    el('option', { value: '' }, `最新（${fmtDate(b.latest_as_of, { withYear: true })}）`),
    ...opts.map((o) => el('option', { value: o.as_of },
      `${fmtDate(o.as_of, { withYear: true })} · ${o.scored_count} 家`)),
  );
  sel.value = state.asOf || '';
}

function wireChrome() {
  document.getElementById('asof-select').addEventListener('change', (e) => {
    state.asOf = e.target.value || null;
    render();
  });
  for (const b of document.querySelectorAll('#tabs button')) {
    b.addEventListener('click', () => navigate(b.dataset.view));
  }
  const themeBtn = document.getElementById('theme-toggle');
  themeBtn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
    if (next) document.documentElement.setAttribute('data-theme', next);
    else document.documentElement.removeAttribute('data-theme');
    themeBtn.textContent = next === 'light' ? '◑ 浅色' : next === 'dark' ? '◐ 深色' : '◐ 主题';
  });
  window.addEventListener('hashchange', render);
}

async function boot() {
  wireChrome();
  try {
    await refreshBootstrap();
    await render();
  } catch (err) {
    app.replaceChildren(el('div', { class: 'card' },
      el('h2', {}, '无法连接后端'),
      el('p', { class: 'secondary' }, err.message),
      el('p', { class: 'small muted' }, '请确认服务已启动：python3 -m churn.server')));
  }
}

boot();
