/* Calculation log page: the newest requests kept in memory (detail is never
   persisted), the stations the pipeline walked, why each station hit or
   missed, the decision path against its confidence floors, the weighted score
   boards and the signal pack. Data: GET /admin/requests/recent (memory ring). */
const $ = s => document.querySelector(s);

const MODEL_ORDER = ['jailbreak', 'pii', 'domain', 'fact_check', 'modality',
  'complexity', 'user_feedback', 'similarity', 'reask'];

let requests = [];
let questions = null;
let meta = {};
let stations = [];
let SIG = {};
let filter = 'all';
const openIds = new Set();

const sigSpec = k => SIG[k] || { zh: k, en: k, values: [] };
const sigName = k => (LANG === 'zh' ? sigSpec(k).zh : sigSpec(k).en) || k;
const valLabel = (k, v) => {
  const vals = sigSpec(k).values || [];
  const hit = vals.find(x => String(x[0]) === String(v));
  return hit ? (LANG === 'zh' ? hit[1] : (hit[2] || hit[1])) : String(v ?? '');
};
const stationName = id => (stations.find(s => s.id === id) || {}).name || '';
const lastStep = st => {
  const path = Array.isArray(st.path) ? st.path : [];
  return path.length ? path[path.length - 1] : null;
};

/* ---------- header row ---------- */
function decisionHtml(r) {
  if (r.decision === 'block') return `<span class="dec block">✕ ${t('logs.blocked')}</span>`;
  if (r.decision === 'route') return `<span class="dec route">→ ${esc(r.model || '?')}</span>`;
  return `<span class="dec none">${t('logs.none')}</span>`;
}
function sessionHtml(r) {
  if (r.session_state === 'sticky') return `<span class="dec none">${t('logs.sticky')}</span>`;
  if (r.session_state === 'unlocked') return `<span class="dec none">${t('logs.unlocked')}</span>`;
  return '';
}

function traceChips(r) {
  const trace = Array.isArray(r.trace) ? r.trace : [];
  const pack = r.signals || {};
  const used = new Set();
  trace.forEach(st => {
    (st.path || []).forEach(s => s.signal && used.add(s.signal));
    (st.board || []).forEach(b => (b.lines || []).forEach(l => l.signal && used.add(l.signal)));
  });
  let keys;
  if (used.size) keys = MODEL_ORDER.filter(k => pack[k] && used.has(k));
  else keys = MODEL_ORDER.filter(k => pack[k]).slice(0, 4);
  const chips = keys.map(k => {
    const s = pack[k];
    let val = s.value;
    if (Array.isArray(val)) val = val.length ? val.join(', ') : '—';
    else if (val == null) val = '—';
    else val = valLabel(k, val);
    const conf = s.confidence != null ? `<span class="c">${pct(s.confidence)}</span>` : '';
    return `<span class="sc"><b>${esc(sigName(k))}</b> ${esc(val)}${conf}</span>`;
  }).join('');
  const rules = Object.entries(pack).filter(([, s]) => s.is_rule).slice(0, 6).map(([k, s]) =>
    `<span class="sc-rule">${esc(sigName(k))} ${esc(Array.isArray(s.value) ? s.value.join(',') : s.value)}</span>`
  ).join('');
  return `<div class="log-signals">${chips}${rules}</div>`;
}

/* ---------- computation flow ---------- */
function confBar(conf, floor, pass) {
  const w = Math.round(100 * Math.max(0, Math.min(1, Number(conf) || 0)));
  const tick = floor != null
    ? `<u class="tick" style="left:${Math.round(100 * Math.min(1, Number(floor)))}%"></u>` : '';
  return `<span class="flow-conf${pass === false ? ' cut' : ''}"><i style="width:${w}%"></i>${tick}</span>`;
}
function condText(s) {
  const name = sigName(s.signal);
  const val = valLabel(s.signal, s.value) || s.value || '';
  return `${name} = ${val}`;
}
function bannerHtml(r) {
  const kind = r.decision === 'block' ? 'block' : (r.decision === 'route' ? 'route' : 'none');
  const label = r.decision === 'block' ? '✕ ' + t('logs.blocked')
    : r.decision === 'route' ? '→ ' + (r.model || '—') : t('logs.none');
  const via = stationName(r.node_id);
  const score = r.score != null ? ' · ' + t('logs.score') + ' ' + fmt(r.score) : '';
  return `<div class="flow-banner ${kind}">
    <span class="flow-m">${esc(label)}</span>
    <span class="flow-via">${via ? esc(t('logs.via') + ' ' + via) + score : ''}</span>
    <span class="flow-id mono">${esc(r.id || '')}</span></div>`;
}
function stationTrack(r) {
  const trace = Array.isArray(r.trace) ? r.trace : [];
  if (!trace.length) return '';
  const rows = trace.map((st, i) => {
    const hit = st.result === 'hit';
    const step = hit ? null : lastStep(st);
    let detail = '';
    let scorecard = '';
    if (hit) {
      detail = `<span class="flow-tag win">★ ${esc(t('logs.hit'))}</span>`;
    } else if ((st.board || []).length) {
      // weighted leaf matched every condition but score < min_score: the last
      // path step says pass=true, so name the real reason and show the board
      detail = `<span class="flow-tag cut">${esc(t('logs.belowMinScore'))}</span>`;
      scorecard = `<div class="flow-sec"><div class="flow-h">${esc(t('logs.scorecard'))}</div>${boardBlocksHtml(st.board)}</div>`;
    } else if (step) {
      const cut = step.m === 1 && !step.pass;
      detail = `<span class="flow-tag${cut ? ' cut' : ''}">${esc(cut ? t('logs.belowFloor') : t('logs.notMatched'))}</span>
        <span class="flow-cond">${esc(condText(step))}</span>
        <span class="mono">${step.conf != null ? pct(step.conf) : '—'}${step.conf_min != null ? ' ≥ ' + pct(step.conf_min) : ''}</span>`;
    } else {
      detail = `<span class="flow-tag">${esc(t('logs.noChild'))}</span>`;
    }
    return `<div class="flow-ststep ${hit ? 'hit' : 'miss'}">
      <span class="flow-idx mono">${i + 1}</span>
      <b class="flow-nm">${esc(st.name || st.station_id || '')}</b>${detail}${scorecard}</div>`;
  }).join('');
  return `<div class="flow-track">${rows}</div>`;
}
function pathHtml(r) {
  const hit = (Array.isArray(r.trace) ? r.trace : []).find(st => st.result === 'hit');
  const path = hit && Array.isArray(hit.path) ? hit.path : [];
  if (!path.length) return '';
  const steps = path.map(s => `<div class="flow-pathstep">
      <span class="flow-cond">${esc(condText(s))}</span>
      ${s.conf_min == null ? `<span class="mut">${esc(t('logs.deterministic'))}</span>`
        : `${confBar(s.conf, s.conf_min, s.pass)}<span class="mono">${s.conf != null ? pct(s.conf) : '—'} ≥ ${pct(s.conf_min)}</span>`}
    </div>`).join('');
  return `<div class="flow-sec"><div class="flow-h">${esc(t('logs.path'))}</div>
    <div class="flow-paths">${steps}</div></div>`;
}
function boardBlocksHtml(board) {
  return (board || []).map(b => {
    const pctW = Math.round(100 * Math.max(0, Math.min(1, Number(b.score) || 0)));
    const tick = b.min_score != null
      ? `<u class="tick" style="left:${Math.round(100 * Math.min(1, Number(b.min_score)))}%"></u>` : '';
    const lines = (b.lines || []).map(l => `<tr>
        <td>${esc(condText(l))}</td>
        <td class="mono">${Number(l.weight).toFixed(1)}</td>
        <td class="mono">${pct(l.c)}</td>
        <td class="mono">${l.m}</td>
        <td class="mono">${Number(l.contrib).toFixed(2)}</td></tr>`).join('');
    return `<div class="flow-board${b.winner ? ' win' : ''}">
      <div class="flow-boardhead">${b.winner ? '★ ' : ''}${esc(t('logs.total'))}
        <span class="mono">${fmt(b.score)} / ${fmt(b.sum_w)}</span>
        <span class="flow-conf${b.passed ? '' : ' cut'}"><i style="width:${pctW}%"></i>${tick}</span>
        <span class="mono">≥ ${fmt(b.min_score)}</span>
        <span class="flow-tag${b.passed ? ' win' : ' cut'}">${b.passed ? t('logs.passed') : t('logs.failed')}</span>
      </div>
      <table class="flow-lines"><tr><th>${t('logs.sig')}</th><th>w</th><th>c</th><th>m</th><th>w·c·m</th></tr>${lines}</table>
    </div>`;
  }).join('');
}
function boardHtml(r) {
  const hit = (Array.isArray(r.trace) ? r.trace : []).find(st => st.result === 'hit');
  const board = hit && Array.isArray(hit.board) ? hit.board : [];
  if (!board.length) return '';
  return `<div class="flow-sec"><div class="flow-h">${esc(t('logs.scorecard'))}</div>${boardBlocksHtml(board)}</div>`;
}
function methodRowsHtml(r) {
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
    const cfg = questions && questions.signals && questions.signals[k];
    if (k === 'pii') {
      const q = cfg && cfg.instructions ? cfg.instructions : '—';
      rows.push(methodRow(sigName(k), String(q).replace('{zh}', '…'), tf('logs.jev.pii')));
      continue;
    }
    const q = cfg ? (Array.isArray(cfg.instructions) ? cfg.instructions[0] : cfg.instructions) : '—';
    rows.push(methodRow(sigName(k), q || '—', tf('logs.jev', { tmp: temp })));
  }
  return rows.join('');
}
function methodRow(name, question, formula) {
  return `<div class="method"><span class="mn">${esc(name)}</span>
    <span class="mq">${esc(question)}</span>
    <span class="mf">${esc(formula)}</span></div>`;
}
function signalPanelHtml(r) {
  const pack = r.signals || {};
  const model = Object.entries(pack).filter(([, s]) => !s.is_rule);
  const sigs = model.map(([k, s]) => {
    let v = s.value;
    if (Array.isArray(v)) v = v.length ? v.join(', ') : '—';
    else if (v == null) v = '—';
    else v = valLabel(k, v);
    const bar = s.confidence != null
      ? `${confBar(s.confidence, null, true)}<span class="mono">${pct(s.confidence)}</span>` : '';
    return `<span class="flow-sig"><b>${esc(sigName(k))}</b><span>${esc(v)}</span>${bar}</span>`;
  }).join('');
  const rules = Object.entries(pack).filter(([, s]) => s.is_rule).map(([k, s]) =>
    `<span class="sc-rule">${esc(sigName(k))} ${esc(Array.isArray(s.value) ? s.value.join(',') : s.value)}</span>`
  ).join('');
  return `<div class="flow-sec"><div class="flow-h">${esc(t('logs.signals'))}</div>
    <div class="flow-sigs">${sigs || `<span class="mut">${esc(t('logs.nosignals'))}</span>`}</div>
    <div class="log-signals" style="padding:5px 0 0 0">${rules}</div>
    <details class="flow-how"><summary>${esc(t('logs.method'))}</summary>${methodRowsHtml(r)}</details>
  </div>`;
}
function legacyHtml(r) {
  return `<div class="flow-legacy">${esc(t('logs.oldLog'))}</div>
    <div class="log-detail" style="border-top:0;padding:0;background:transparent">${methodRowsHtml(r)}</div>`;
}
/* Session continuation short-circuits the pipeline (trace=[] by design): show a
   session banner, never the "old log" fallback. */
function sessionBannerHtml(r) {
  const sticky = r.session_state === 'sticky';
  return `<div class="flow-banner route"><span class="flow-m">${esc(sticky ? t('logs.sticky') : t('logs.unlocked'))}</span>
    <span class="flow-via">${esc(t('logs.via') + ' ' + (r.model || '—'))}</span>
    <span class="flow-id mono">${esc(r.id || '')}</span></div>`;
}

function rowHtml(r) {
  const open = openIds.has(r.id);
  const text = String(r.text || '').replace(/\s+/g, ' ');
  const trace = Array.isArray(r.trace) ? r.trace : null;
  const session = r.session_state === 'sticky' || r.session_state === 'unlocked';
  const detail = trace && trace.length
    ? bannerHtml(r) + stationTrack(r) + pathHtml(r) + boardHtml(r) + signalPanelHtml(r)
    : (session ? sessionBannerHtml(r) + signalPanelHtml(r) : legacyHtml(r));
  return `<article class="log-row ${open ? 'open' : ''}" data-id="${esc(r.id)}">
    <div class="log-head">
      <span class="log-time">${esc(fmtTime(r.ts))}</span>
      ${decisionHtml(r)}${sessionHtml(r)}
      <span class="log-text" title="${esc(text)}">${esc(text.length > 90 ? text.slice(0, 90) + '…' : text)}</span>
      <span class="log-lat">${r.latency_ms != null ? Math.round(r.latency_ms) + 'ms' : '—'}</span>
    </div>
    ${traceChips(r)}
    <div class="log-detail">${detail}</div>
  </article>`;
}

function render() {
  const shown = requests.filter(r => filter === 'all' || r.decision === filter);
  $('#countPill').textContent = (LANG === 'zh' ? '共 ' : 'total ') + shown.length;
  $('#taggerPill').innerHTML = `<span class="dot" style="color:var(--indigo)"></span>` +
    (LANG === 'zh' ? '引擎 <b>' + esc(meta.name || '?') + '</b>' : 'engine <b>' + esc(meta.name || '?') + '</b>');
  $('#list').innerHTML = shown.map(rowHtml).join('') || `<div class="empty">${t('logs.empty')}</div>`;
}

async function load() {
  const [reqBody, qBody, pipeBody, sigBody] = await Promise.all([
    api.get('/admin/requests/recent'),
    api.get('/admin/signal-questions').catch(() => ({ config: null, meta: {} })),
    api.get('/admin/pipeline'),
    api.get('/admin/signals'),
  ]);
  requests = reqBody.requests || [];
  questions = qBody.config || null;
  meta = (qBody.meta && Object.keys(qBody.meta).length) ? qBody.meta : (meta || {});
  if (!meta.name) {
    try { meta = (await api.get('/admin/tagger')).tagger || meta; } catch (e) { /* ignore */ }
  }
  stations = pipeBody.stations || [];
  sigBody.signals.forEach(s => { SIG[s.key] = s; });
  render();
}

$('#refresh').addEventListener('click', () => load());
$('#filter').addEventListener('change', e => { filter = e.target.value; render(); });
$('#list').addEventListener('click', e => {
  const row = e.target.closest('[data-id]');
  if (!row) return;
  if (e.target.closest('details')) return;   // "怎么算的" owns its disclosure
  const id = row.dataset.id;
  if (openIds.has(id)) openIds.delete(id); else openIds.add(id);
  render();
});
window.onLangChange = () => render();
load();
