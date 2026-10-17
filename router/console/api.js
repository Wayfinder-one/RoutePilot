/* Same-origin admin API client. Throws on non-2xx with the parsed detail.

   When the server runs with ROUTER_ADMIN_TOKEN, every /admin call needs
   "Authorization: Bearer <token>". The token is kept in this browser's
   localStorage ("sr_admin_token"); a ?token=... query parameter is accepted
   once, stored and stripped from the URL. On a 401 the console asks for the
   token and retries the request once. */
const api = {
  TOKEN_KEY: 'sr_admin_token',
  token() { return localStorage.getItem(this.TOKEN_KEY) || ''; },
  setToken(t) {
    if (t && t.trim()) localStorage.setItem(this.TOKEN_KEY, t.trim());
    else localStorage.removeItem(this.TOKEN_KEY);
  },
  async req(method, path, body, _retried) {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    const tok = this.token();
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const r = await fetch(path, {
      method,
      headers: Object.keys(headers).length ? headers : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401 && !_retried) {
      const entered = window.prompt('需要 /admin 鉴权令牌（Admin token）：', '');
      if (entered && entered.trim()) {
        this.setToken(entered);
        return this.req(method, path, body, true);
      }
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(r.status); e.detail = data.detail; throw e; }
    return data;
  },
  get(p) { return this.req('GET', p); },
  put(p, b) { return this.req('PUT', p, b); },
  post(p, b) { return this.req('POST', p, b); },
};

/* Accept ?token=... once (handy for shared demo links), then strip it. */
(function () {
  try {
    const u = new URL(window.location.href);
    const q = u.searchParams.get('token');
    if (q) {
      api.setToken(q);
      u.searchParams.delete('token');
      window.history.replaceState({}, '', u.toString());
    }
  } catch (e) { /* ignore */ }
})();

/* Shared helpers used by every page. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmt(x) { return x == null || x === '' ? '—' : Number(x).toFixed(2); }
function pct(x) { return x == null || x === '' ? '—' : Math.round(100 * Number(x)) + '%'; }
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts).slice(11, 19) || String(ts);
  return d.toLocaleTimeString([], { hour12: false }) +
    (d.toDateString() === new Date().toDateString() ? '' : ' ' + d.toLocaleDateString());
}
function toast(msg, ms) {
  const d = document.createElement('div');
  d.className = 'toast'; d.textContent = msg;
  document.body.appendChild(d); setTimeout(() => d.remove(), ms || 2200);
}
/* Group key -> CSS color variable (matches signals.css tokens). */
const GROUP_COLOR = { safety: 'var(--g-safety)', domain: 'var(--g-domain)',
  difficulty: 'var(--g-cx)', context: 'var(--g-ctx)', session: 'var(--g-sess)',
  rule: 'var(--g-rule)' };
function sigColor(group) { return GROUP_COLOR[group] || 'var(--g-rule)'; }
