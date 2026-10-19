/* Calculation log page: the newest persisted requests, the signals that were
   produced for each, how each signal was computed (JEV question / rule), and
   the final routing decision. Data: GET /admin/requests/recent. */
const $ = s => document.querySelector(s);

let requests = [];
let questions = null;
let meta = {};
let pipeStations = [];
let SIG = {};
let filter = 'all';
let openIds = new Set();

const sigSpec = k => SIG[k] || { zh: k, en: k, group: 'rule', values: [] };
const sigName = k => (LANG === 'zh' ? sigSpec(k).zh : sigSpec(k).en) || k;
const valLabel = (k, v) => {
  const vals = sigSpec(k).values || [];
  const hit = vals.find(x => String(x[0]) === String(v));
  return hit ? (LANG === 'zh' ? hit[1] : (hit[2] || hit[1])) : String(v ?? '');
};
const MODEL_ORDER = ['jailbreak', 'pii', 'domain', 'fact_check', 'modality',
  'complexity', 'user_feedback', 'similarity', 'reask'];

function nodeText(n) {
  if (!n) return '';
  return n.signal ? `${sigName(n.signal)} = ${valLabel(n.signal, n.value) || n.value}` : '';
}
function machine(r) {
  const station = pipeStations.find(st => st.id === r.node_id);
  let leaf = null;
  const walk = n => {
    if (!n || leaf) return;
    if (n.id === r.branch_id) { leaf = n; return; }
    (n.children || []).forEach(walk);
  };
  if (station) walk(station.root);
  return { node: station, branch: leaf ? { name: nodeText(leaf) } : null };
}

function decisionHtml(r) {
  if (r.decision === 'block') return `<span class="dec block">✕ ${t('logs.blocked')}</span>`;
  if (r.decision === 'route') return `<span class="dec route">→ ${esc(r.model || '?')}</span>`;
  return `<span class="dec none">${t('logs.none')}</span>`;
}

function signalChips(pack) {
  const order = MODEL_ORDER.filter(k => pack[k])
    .concat(Object.keys(pack).filter(k => !MODEL_ORDER.includes(k) && !pack[k].is_rule));
  let out = order.map(k => {
    const s = pack[k];
    let val = s.value;
    if (Array.isArray(val)) val = val.length ? val.map(v => v).join(', ') : '—';
    else if (s.value == null) val = '—';
    else if (k === 'similarity') val = String(val);
    else val = valLabel(k, val);
    const conf = s.confidence != null ? `<span class="c">${pct(s.confidence)}</span>` : '';
    return `<span class="sc ${k === 'pii' ? 'pii' : ''}"><b>${esc(sigName(k))}</b> ${esc(val)}${conf}</span>`;
  }).join('');
  const rules = Object.entries(pack).filter(([k, s]) => s.is_rule)
    .slice(0, 8).map(([k, s]) =>
      `<span class="sc-rule">${esc(sigName(k))} ${esc(Array.isArray(s.value) ? s.value.join(',') : s.value)}</span>`).join('');
  return `<div class="log-signals">${out}${rules || ''}</div>`;
}

function methodHtml(r) {
  const pack = r.signals || {};
  const temp = meta.temperature != null ? Number(meta.temperature).toFixed(2) : '—';
  const rows = [];
  for (const k of MODEL_ORDER) {
    const s = pack[k];
    if (!s) continue;
    if (k === 'similarity' || k === 'reask') {
      rows.push(methodRow(sigName(k), t('logs.similarity'), 'no-embedding'));
      continue;
    }
    if (k === 'pii') {
      const cfg = questions && questions.signals && questions.signals.pii;
      const q = cfg ? cfg.instructions : '—';
      rows.push(methodRow(sigName(k), `${q}`.replace('{zh}', '…'), tf('logs.jev.pii')));
      continue;
    }
    const cfg = questions && questions.signals && questions.signals[k];
    const q = cfg ? (Array.isArray(cfg.instructions) ? cfg.instructions[0] : cfg.instructions) : '—';
    rows.push(methodRow(sigName(k), q, tf('logs.jev', { tmp: temp })));
  }
  const ruleNames = Object.entries(pack).filter(([k, s]) => s.is_rule).map(([k, s]) =>
    sigName(k) + '=' + (Array.isArray(s.value) ? s.value.join(',') : s.value));
  if (ruleNames.length) rows.push(methodRow(t('logs.rule'), ruleNames.join(' · '), 'c=1'));
  const { node, branch } = machine(r);
  const where = node ? `${esc(node.name)}${branch ? ' · ' + esc(branch.name) : ''}` : '—';
  return `<div class="log-detail">
    <div class="method" style="border-bottom:1px solid var(--line)">
      <span class="mn">${t('logs.decision')}</span>
      <span class="mq">${node ? t('logs.node') + ': ' + where : '—'}
        ${r.score != null ? ' · ' + t('logs.score') + ' ' + fmt(r.score) : ''}</span>
      <span class="mf">${esc(r.id || '')}</span>
    </div>
    <div class="method" style="border-bottom:1px solid var(--line)">
      <span class="mn">${t('logs.latency')}</span>
      <span class="mq">${r.latency_ms != null ? Math.round(r.latency_ms) + ' ms' : '—'}</span>
      <span class="mf">${t('logs.tagger')}: ${esc(meta.name || '—')}</span>
    </div>
    ${treePathHtml(r)}
    ${rows.join('') || `<div class="mut" style="font-size:11.5px;padding:6px 0">${t('logs.nosignals')}</div>`}
  </div>`;
}

function methodRow(name, question, formula) {
  return `<div class="method"><span class="mn">${esc(name)}</span>
    <span class="mq">${esc(question)}</span>
    <span class="mf">${esc(formula)}</span></div>`;
}

/* SPEC-TREE-4 §4.4: the split layer path of the deciding node, stored per
   request as requests.tree_path and shown layer by layer here. */
function treePathHtml(r) {
  if (!r.tree_path) return '';
  let steps;
  try { steps = JSON.parse(r.tree_path); } catch (e) { return ''; }
  if (!Array.isArray(steps) || !steps.length) return '';
  const cells = steps.map(s => {
    const conf = (s.conf === 1.0 && s.floor == null)
      ? t('p4.deterministic')
      : pct(s.conf) + (s.floor != null ? ` ≥${Number(s.floor).toFixed(2)}` : '');
    const cls = s.pass ? '' : 'sc pii';
    return `<span class="sc ${cls}"><b>${esc(sigName(s.signal))}</b> ${esc(valLabel(s.signal, s.value))} <span class="c">${esc(conf)}${s.pass ? '' : ' ·' + t('p4.cut')}</span></span>`;
  });
  return `<div class="method"><span class="mn">${esc(t('p4.treePath'))}</span>
    <span class="mq" style="display:flex;gap:5px;flex-wrap:wrap">${cells.join('<span class="mf">→</span>')}</span>
    <span class="mf">${esc(t('p4.splitNode'))}</span></div>`;
}

function rowHtml(r) {
  const open = openIds.has(r.id);
  const text = String(r.text || '').replace(/\s+/g, ' ');
  return `<article class="log-row ${open ? 'open' : ''}" data-id="${esc(r.id)}">
    <div class="log-head">
      <span class="log-time">${esc(fmtTime(r.ts))}</span>
      ${decisionHtml(r)}
      <span class="log-text" title="${esc(text)}">${esc(text.length > 90 ? text.slice(0, 90) + '…' : text)}</span>
      <span class="log-lat">${r.latency_ms != null ? Math.round(r.latency_ms) + 'ms' : '—'}</span>
    </div>
    ${signalChips(r.signals || {})}
    ${methodHtml(r)}
  </article>`;
}

function render() {
  const shown = requests.filter(r => filter === 'all' || r.decision === filter);
  $('#countPill').textContent = (LANG === 'zh' ? '共 ' : 'total ') + shown.length;
  $('#taggerPill').innerHTML = `<span class="dot" style="color:var(--indigo)"></span>` +
    (LANG === 'zh' ? '引擎 <b>' + esc(meta.name || '?') + '</b>' : 'engine <b>' + esc(meta.name || '?') + '</b>');
  $('#list').innerHTML = shown.map(rowHtml).join('') ||
    `<div class="empty">${t('logs.empty')}</div>`;
}

async function load() {
  const limit = $('#limit').value || '20';
  const [reqBody, qBody, pipeBody, sigBody] = await Promise.all([
    api.get(`/admin/requests/recent?limit=${limit}`),
    api.get('/admin/signal-questions').catch(() => ({ config: null, meta: {} })),
    api.get('/admin/pipeline'),
    api.get('/admin/signals'),
  ]);
  requests = reqBody.requests || [];
  questions = qBody.config || null;
  meta = (qBody.meta && Object.keys(qBody.meta).length) ? qBody.meta : (meta || {});
  if (!meta.name) {
    // older server without /signal-questions meta: fall back to /tagger
    try { meta = (await api.get('/admin/tagger')).tagger || meta; } catch (e) { /* ignore */ }
  }
  pipeStations = pipeBody.stations || [];
  sigBody.signals.forEach(s => { SIG[s.key] = s; });
  render();
}

$('#refresh').addEventListener('click', () => load());
$('#limit').addEventListener('change', () => load());
$('#filter').addEventListener('change', e => { filter = e.target.value; render(); });
$('#list').addEventListener('click', e => {
  const row = e.target.closest('[data-id]');
  if (!row) return;
  const id = row.dataset.id;
  if (openIds.has(id)) openIds.delete(id); else openIds.add(id);
  render();
});
window.onLangChange = () => render();
load();
