/* Same-origin admin API client. Throws on non-2xx with the parsed detail.

   Auth: the console signs in with email + password (POST /admin/login) and
   keeps the opaque session token in this browser's localStorage
   ("sr_admin_token"). The static ROUTER_ADMIN_TOKEN was removed on 2026-10-26
   and there is no URL token handoff either: scripts and demo browsers sign in
   normally (or seed localStorage) instead of leaking a session token through
   the address bar. On a 401 the session is gone: clear the token and bounce to
   login.html?next=<this page>; the login page itself never bounces (login.js
   shows the error instead). */
import { tf } from './i18n.js';

const api = {
  TOKEN_KEY: 'sr_admin_token',
  TIMEOUT_MS: 15000,
  _bouncing: false,
  token() { return localStorage.getItem(this.TOKEN_KEY) || ''; },
  setToken(t) {
    if (t && String(t).trim()) localStorage.setItem(this.TOKEN_KEY, String(t).trim());
    else localStorage.removeItem(this.TOKEN_KEY);
  },
  /* 登录页上的 401 是「密码错」，不是会话过期：不跳转，交给 login.js 展示。 */
  onLoginPage() {
    return /(?:^|\/)login\.html$/.test(window.location.pathname);
  },
  /* 站内绝对路径（含查询/锚点），登录成功后回到这一页。 */
  currentPath() {
    return window.location.pathname + window.location.search + window.location.hash;
  },
  toLogin() {
    if (this._bouncing || this.onLoginPage()) return;
    this._bouncing = true;
    const next = encodeURIComponent(this.currentPath());
    window.location.href = 'login.html?next=' + next + '&expired=1';
  },
  async req(method, path, body, opts) {
    const headers = {};
    if (body !== undefined && body !== null) headers['Content-Type'] = 'application/json';
    const tok = this.token();
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    /* 管理面请求必须有上限：挂起的 /admin/models 不能冻结画布启动。 */
    const timeoutMs = opts && Number(opts.timeoutMs) > 0 ? Number(opts.timeoutMs) : this.TIMEOUT_MS;
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const r = await fetch(path, {
        method,
        headers: Object.keys(headers).length ? headers : undefined,
        body: body === undefined || body === null ? undefined : JSON.stringify(body),
        signal: controller ? controller.signal : undefined,
      });
      if (r.status === 401) {
        this.setToken('');
        if (!(opts && opts.noBounce)) this.toLogin();
      }
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const e = new Error(r.status);
        e.status = r.status;
        e.detail = data.detail;
        throw e;
      }
      return data;
    } catch (cause) {
      if (controller && controller.signal.aborted) {
        const seconds = Math.round(timeoutMs / 1000);
        const e = new Error(typeof tf === 'function'
          ? tf('api.timeout', { seconds: seconds, path: path })
          : 'Request timed out after ' + seconds + 's: ' + path);
        e.status = 0;
        e.timeout = true;
        e.cause = cause;
        throw e;
      }
      throw cause;
    } finally {
      if (timer) clearTimeout(timer);
    }
  },
  get(p, o) { return this.req('GET', p, undefined, o); },
  put(p, b, o) { return this.req('PUT', p, b, o); },
  post(p, b, o) { return this.req('POST', p, b, o); },
  del(p) { return this.req('DELETE', p); },
  /* 邮箱 + 密码换会话 token（成功后落到 localStorage，调用方只管跳页）。 */
  async login(email, password) {
    const data = await this.req('POST', '/admin/login', { email, password });
    if (data && data.token) this.setToken(data.token);
    return data;
  },
  /* 退出：无论服务端是否成功都清掉本地 token；登出接口的 401 不该再弹回登录页。 */
  async logout() {
    try {
      await this.req('POST', '/admin/logout', undefined, { noBounce: true });
    } finally {
      this.setToken('');
    }
  },
  /* 当前身份；静态管理 token 时返回 {id:null, email:null, role:"admin", service:true}。 */
  me() { return this.req('GET', '/admin/me'); },
};

/* FastAPI error detail: a string, or a 422 validation array ([{loc,msg,...}]).
   Always String() before showing it in the DOM. */
function apiErrText(e) {
  const pick = x => {
    if (x == null) return '';
    if (typeof x === 'string') return x;
    if (Array.isArray(x)) return x.map(pick).filter(Boolean).join('; ');
    if (typeof x === 'object') {
      const msg = x.msg || x.detail || x.zh || x.en || '';
      return msg ? String(msg) : JSON.stringify(x);
    }
    return String(x);
  };
  const out = pick(e && e.detail);
  if (out) return out;
  const m = e && e.message != null ? String(e.message) : '';
  return m === '[object Object]' ? JSON.stringify(e) : m;
}

/* Shared helpers used by every page. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmt(x) { return x == null || x === '' ? '—' : Number(x).toFixed(2); }
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

export { api, apiErrText, esc, fmt, fmtTime, toast };
