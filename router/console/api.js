/* Same-origin admin API client. Throws on non-2xx with the parsed detail. */
const api = {
  async req(method, path, body) {
    const r = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(r.status); e.detail = data.detail; throw e; }
    return data;
  },
  get(p) { return this.req('GET', p); },
  put(p, b) { return this.req('PUT', p, b); },
  post(p, b) { return this.req('POST', p, b); },
};
/* Shared helpers used by every page (ported from the demo). */
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function fmt(x) { return x == null ? '—' : Number(x).toFixed(2); }
function toast(msg) {
  const d = document.createElement('div');
  d.className = 'toast'; d.textContent = msg;
  document.body.appendChild(d); setTimeout(() => d.remove(), 2000);
}
