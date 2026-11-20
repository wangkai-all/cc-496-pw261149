// API client + tiny state store.

export const state = {
  bootstrap: null,
  asOf: null, // null = latest
  view: 'overview',
  customerId: null,
  alertStatus: 'open',
  filters: { tier: '', segment: '', region: '', q: '' },
  validationPolicy: 'eventual',
  compareVersion: null,
  historyMode: 'tiers',
};

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const msg = data && data.error ? data.error : `请求失败 (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

const qs = (params = {}) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') s.set(k, v);
  }
  const str = s.toString();
  return str ? `?${str}` : '';
};

export const api = {
  bootstrap: () => request('GET', '/bootstrap'),
  portfolio: (asOf, filters = {}) => request('GET', `/portfolio${qs({ as_of: asOf, ...filters })}`),
  riskHistory: (asOf) => request('GET', `/risk-history${qs({ as_of: asOf })}`),
  customer: (id, asOf) => request('GET', `/customer/${id}${qs({ as_of: asOf })}`),
  warnings: (asOf, status = 'open', customerId) =>
    request('GET', `/warnings${qs({ as_of: asOf, status, customer_id: customerId })}`),
  warning: (id) => request('GET', `/warnings/${id}`),
  runs: () => request('GET', '/runs'),
  jobs: () => request('GET', '/jobs'),
  versions: (asOf) => request('GET', `/versions${qs({ as_of: asOf })}`),
  validation: (asOf) => request('GET', `/validation${qs({ as_of: asOf })}`),
  runScoring: (body) => request('POST', '/scoring/run', body),
  reviseFeature: (body) => request('POST', '/features/revise', body),
  merge: (body) => request('POST', '/customers/merge', body),
  undoMerge: (body) => request('POST', '/customers/undo-merge', body),
  replayLabel: (body) => request('POST', '/labels/replay', body),
  feedback: (id, body) => request('POST', `/warnings/${id}/feedback`, body),
  closeWarning: (id, body) => request('POST', `/warnings/${id}/close`, body),
  reopenWarning: (id, body) => request('POST', `/warnings/${id}/reopen`, body),
  activateSet: (version) => request('POST', '/feature-sets/activate', { version }),
  resetDemo: () => request('POST', '/demo/reset', {}),
};

export function effectiveAsOf() {
  return state.asOf || (state.bootstrap && state.bootstrap.latest_as_of) || null;
}
