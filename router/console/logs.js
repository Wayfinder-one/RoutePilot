/* Calculation log page: the newest requests kept in memory (detail is never
   persisted). The expanded row renders the routing trace through the SAME
   shared renderer as the pipeline try drawer (`SRTraceView`), so one backend
   event always produces the same text on both pages.

   Data: GET /admin/requests/recent (memory ring) + GET /admin/trees (id ->
   name map, read once per page load and cached) + GET /admin/signals (label
   registry) + GET /admin/tagger (engine pill). The retired station/pipeline
   explanation and `GET /admin/pipeline` are gone. */
const $ = s => document.querySelector(s);

let requests = [];
let meta = {};
let filter = 'all';
const openIds = new Set();

/* tree id -> tree row, read once per page load (never per row; no refresh). */
let treesById = {};
let treesPromise = null;
function ensureTrees() {
  if (!treesPromise) {
    treesPromise = api.get('/admin/trees')
      .then(body => {
        treesById = {};
        (body.trees || []).forEach(tree => { if (tree && tree.id) treesById[tree.id] = tree; });
        return treesById;
      })
      .catch(() => ({}));        // tree list unavailable: names stay empty
  }
  return treesPromise;
}

/* ---------- header row ---------- */
const reasonLabel = lvl => t('reason.' + lvl);
function reasonBadge(r) {
  if (!r.reasoning) return '';
  return `<span class="dec reason">· ${esc(t('logs.reasoning'))} ${esc(reasonLabel(r.reasoning))}</span>`;
}
function reasoningWarn(r) {
  if (!r.reasoning_warning) return '';
  return `<span class="dec warn" title="${esc(t('logs.reasoningWarn'))}">⚠ ${esc(t('logs.reasoning'))}</span>`;
}
/* Model-input window (JEV head-truncation): the model judged only the first N
   tokens while the log keeps the full text. */
function inputTruncBadge(r) {
  if (!r.input_truncated) return '';
  const vis = r.input_visible_tokens != null ? r.input_visible_tokens : '?';
  const tot = r.input_tokens != null ? r.input_tokens : '?';
  const tip = t('logs.truncatedTip').replace('{vis}', vis).replace('{tot}', tot);
  return `<span class="dec warn" title="${esc(tip)}">✂ ${esc(t('logs.truncated'))} ${vis}/${tot}</span>`;
}
function decisionHtml(r) {
  if (r.decision === 'block') return `<span class="dec block">✕ ${t('logs.blocked')}</span>`;
  if (r.decision === 'route') {
    return `<span class="dec route">→ ${esc(r.model || '?')}</span>` +
      reasonBadge(r) + reasoningWarn(r);
  }
  return `<span class="dec none">${t('logs.none')}</span>`;
}

/* ---------- signal evidence (pack shape: {value, confidence, is_rule}) ---- */
function confBar(conf) {
  const width = Math.round(100 * Math.max(0, Math.min(1, Number(conf) || 0)));
  return `<span class="flow-conf"><i style="width:${width}%"></i></span>`;
}
function signalValue(key, payload) {
  let value = payload && typeof payload === 'object' ? payload.value : payload;
  if (Array.isArray(value)) value = value.length ? value.join(', ') : '—';
  else if (value == null) value = '—';
  else value = valLabel(key, value) || value;
  return String(value);
}
function traceChips(r) {
  const pack = r.signals && typeof r.signals === 'object' ? r.signals : {};
  const entries = Object.entries(pack);
  const model = entries.filter(([, s]) => s && !s.is_rule).slice(0, 6);
  const chips = model.map(([key, s]) => {
    const conf = s.confidence != null
      ? `<span class="c">${pct(s.confidence)}</span>` : '';
    const pii = key === 'pii' ? ' pii' : '';
    return `<span class="sc${pii}"><b>${esc(sigName(key))}</b> ` +
      `${esc(signalValue(key, s))}${conf}</span>`;
  }).join('');
  const rules = entries.filter(([, s]) => s && s.is_rule).slice(0, 6).map(([key, s]) =>
    `<span class="sc-rule">${esc(sigName(key))} ${esc(signalValue(key, s))}</span>`).join('');
  return `<div class="log-signals">${chips}${rules}</div>`;
}
function signalPanelHtml(r) {
  const pack = r.signals && typeof r.signals === 'object' ? r.signals : {};
  const entries = Object.entries(pack);
  const sigs = entries.filter(([, s]) => s && !s.is_rule).map(([key, s]) => {
    const bar = s.confidence != null
      ? `${confBar(s.confidence)}<span class="mono">${pct(s.confidence)}</span>` : '';
    return `<span class="flow-sig"><b>${esc(sigName(key))}</b>` +
      `<span>${esc(signalValue(key, s))}</span>${bar}</span>`;
  }).join('');
  if (!sigs && !entries.some(([, s]) => s && s.is_rule)) return '';
  return `<div class="flow-sec"><div class="flow-h">${esc(t('logs.signals'))}</div>` +
    `<div class="flow-sigs">${sigs || `<span class="mut">${esc(t('logs.nosignals'))}</span>`}</div></div>`;
}

/* ---------- rows ---------- */
function rowHtml(r) {
  const open = openIds.has(r.id);
  const text = String(r.text || '').replace(/\s+/g, ' ');
  return `<article class="log-row ${open ? 'open' : ''}" data-id="${esc(r.id)}">
    <div class="log-head">
      <span class="log-time">${esc(fmtTime(r.ts))}</span>
      ${decisionHtml(r)}
      ${inputTruncBadge(r)}
      <span class="log-text" title="${esc(text)}">${esc(text.length > 90 ? text.slice(0, 90) + '…' : text)}</span>
      <span class="log-lat">${r.latency_ms != null ? Math.round(r.latency_ms) + 'ms' : '—'}</span>
    </div>
    ${traceChips(r)}
    <div class="log-detail"><div class="log-trace" data-trace-for="${esc(r.id)}"></div>${signalPanelHtml(r)}</div>
  </article>`;
}

/* the shared renderer is mounted into each OPEN row (one DOM write per row) */
function renderOpenTraces() {
  const byId = new Map(requests.map(r => [r.id, r]));
  document.querySelectorAll('.log-row.open [data-trace-for]').forEach(host => {
    const record = byId.get(host.getAttribute('data-trace-for'));
    if (!record) return;
    const tree = record.tree_id ? treesById[record.tree_id] || null : null;
    SRTraceView.renderRoutingTrace(host, record, {tree: tree});
  });
}

function render() {
  const shown = requests.filter(r => filter === 'all' || r.decision === filter);
  $('#countPill').textContent = (LANG === 'zh' ? '共 ' : 'total ') + shown.length;
  $('#taggerPill').innerHTML = `<span class="dot" style="color:var(--indigo)"></span>` +
    (LANG === 'zh' ? '引擎 ' : 'engine ') + '<b>' + esc(meta.name || '?') + '</b>';
  $('#list').innerHTML = shown.map(rowHtml).join('') ||
    `<div class="empty">${t('logs.empty')}</div>`;
  $('#list').classList.toggle('has-open', openIds.size > 0);
  renderOpenTraces();
}

async function load() {
  const [reqBody, tagBody, sigBody] = await Promise.all([
    api.get('/admin/requests/recent'),
    api.get('/admin/tagger').catch(() => ({ tagger: {} })),
    api.get('/admin/signals'),
  ]);
  requests = reqBody.requests || [];
  meta = (tagBody && tagBody.tagger) || meta || {};
  setSignalRegistry(sigBody.signals);
  await ensureTrees();               // cached after the first page load
  render();
}

$('#refresh').addEventListener('click', () => load());
$('#filter').addEventListener('change', e => { filter = e.target.value; render(); });
$('#list').addEventListener('click', e => {
  const row = e.target.closest('[data-id]');
  if (!row) return;
  if (e.target.closest('details')) return;   // trace evidence owns its disclosure
  const id = row.dataset.id;
  if (openIds.has(id)) openIds.delete(id); else openIds.add(id);
  render();
});
window.onLangChange = () => render();
load();
