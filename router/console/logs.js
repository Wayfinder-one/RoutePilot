const $ = selector => document.querySelector(selector);
let requests = [];
let filter = 'all';
const openIds = new Set();

function badges(record) {
  const values = [`→ ${record.model || '—'}`, record.selection_reason];
  if (record.sticky_hit) values.push('sticky');
  if (record.fallback_reason) values.push(record.fallback_reason);
  if (record.jev_top != null) values.push(`JEV ${pct(record.jev_top)}`);
  return values.map(value => `<span class="dec route">${esc(value)}</span>`).join('');
}

function rowHtml(record) {
  const open = openIds.has(record.id);
  const text = String(record.text || '').replace(/\s+/g, ' ');
  return `<article class="log-row ${open ? 'open' : ''}" data-id="${esc(record.id)}"><div class="log-head"><span class="log-time">${esc(fmtTime(record.ts))}</span><span>${badges(record)}</span><span class="log-text" title="${esc(text)}">${esc(text.slice(0, 100))}</span><span class="log-lat">${record.latency_ms != null ? Math.round(record.latency_ms) + 'ms' : '—'}</span></div><div class="log-detail"><div data-trace="${esc(record.id)}"></div></div></article>`;
}

function render() {
  const shown = requests.filter(record => {
    if (filter === 'sticky') return record.sticky_hit;
    if (filter === 'fallback') return record.selection_reason === 'fallback';
    if (filter === 'jev') return record.jev_top != null;
    return true;
  });
  $('#countPill').textContent = String(shown.length);
  $('#list').classList.toggle('has-open', openIds.size > 0);
  $('#list').innerHTML = shown.map(rowHtml).join('') || `<div class="empty">${t('logs.empty')}</div>`;
  const byId = new Map(requests.map(record => [record.id, record]));
  document.querySelectorAll('[data-trace]').forEach(host => {
    const record = byId.get(host.dataset.trace);
    if (record && openIds.has(record.id)) SRTrace.renderRecord(host, record);
  });
}

async function load() {
  try {
    const [logs, jev] = await Promise.all([api.get('/admin/requests/recent'), api.get('/admin/jev')]);
    requests = logs.requests || [];
    $('#taggerPill').textContent = (jev.config && jev.config.model) || 'JEV';
    render();
  } catch (error) { $('#list').innerHTML = `<div class="empty">${esc(apiErrText(error))}</div>`; }
}

$('#refresh').addEventListener('click', load);
$('#filter').addEventListener('change', event => { filter = event.target.value; render(); });
$('#list').addEventListener('click', event => {
  const row = event.target.closest('[data-id]');
  if (!row) return;
  const id = row.dataset.id;
  if (openIds.has(id)) openIds.delete(id); else openIds.add(id);
  render();
});
window.onLangChange = render;
load();
