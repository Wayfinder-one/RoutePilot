/* Calculation log — a disclosure list with a staged calculation view.
   Each row is one request and stays one glance wide (model, outcome,
   latency, evidence chips). Clicking a row opens it inline, below the row:
   a compact decision summary, the staged calculation process (input →
   initial computation → JEV waves → candidates → schedule → failure),
   the execution result, and folded technical facts. Rows do not navigate
   elsewhere and never become a side panel; several rows can stay open so
   two decisions can be compared, and the process is painted lazily —
   only for rows that are actually opened. */
const $ = selector => document.querySelector(selector);
let requests = [];
let filter = 'all';
const openIds = new Set();

const OUTCOME = {
  completed: { key: 'logs.out.completed', tone: 'ok', chip: null },
  upstream_error: { key: 'logs.out.upstream', tone: 'bad', chip: 'bad' },
  client_disconnected: { key: 'logs.out.disconnected', tone: 'warn', chip: 'warn' },
  cancelled: { key: 'logs.out.cancelled', tone: 'warn', chip: 'warn' },
  incomplete: { key: 'logs.out.incomplete', tone: '', chip: 'warn' },
  routing_error: { key: 'logs.out.routing', tone: 'bad', chip: 'bad' },
};
/* Graph v3 selection_reason enum: target | sticky | error_fallback.
   "otherwise" and "fallback" remain accepted for historical ledger rows. */
const FALLBACK_REASONS = ['otherwise', 'error_fallback', 'fallback'];
const isFallback = record => FALLBACK_REASONS.indexOf(record.selection_reason) >= 0;
const fallbackLabel = () => 'logs.reason.fallback';
/* 统一耗时口径：行、统计卡和摘要都走这一个取值函数。 */
function latencyValue(record) {
  for (const key of ['total_ms', 'latency_ms', 'routing_ms']) {
    const raw = record[key];
    const value = Number(raw);
    if (raw != null && raw !== '' && Number.isFinite(value)) return value;
  }
  return null;
}
const FILTERS = {
  all: () => true,
  jev: record => SRTrace.hasJev(record),
  sticky: record => !!record.sticky_hit,
  fallback: isFallback,
};

function ms(value) {
  if (value == null || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return number >= 1000 ? (number / 1000).toFixed(2) + 's' : Math.round(number) + 'ms';
}

function shown() {
  return requests.filter(FILTERS[filter] || FILTERS.all);
}

/* ── summary strip ─────────────────────────────────────────────── */
function renderStats() {
  const sticky = requests.filter(record => record.sticky_hit).length;
  const fallback = requests.filter(isFallback).length;
  const timed = requests.map(latencyValue).filter(Number.isFinite);
  const average = timed.length ? timed.reduce((sum, value) => sum + value, 0) / timed.length : null;
  const cards = [
    ['logs.stat.requests', String(requests.length), ''],
    ['logs.stat.sticky', String(sticky), sticky ? 'ok' : ''],
    ['logs.stat.fallback', String(fallback), fallback ? 'warn' : ''],
    ['logs.stat.latency', ms(average), ''],
  ];
  $('#stats').innerHTML = cards.map(([key, value, tone]) =>
    `<div class="lg-stat ${tone}"><div class="n">${esc(value)}</div><div class="l">${esc(t(key))}</div></div>`).join('');
}

/* ── filter tabs ───────────────────────────────────────────────── */
function renderTabs() {
  document.querySelectorAll('#filter button').forEach(button => {
    const key = button.dataset.filter;
    const count = requests.filter(FILTERS[key] || FILTERS.all).length;
    button.setAttribute('aria-pressed', String(key === filter));
    button.innerHTML = `${esc(t('logs.filter.' + key))}<span class="c">${count}</span>`;
  });
}

/* ── rows ──────────────────────────────────────────────────────── */
function rowHtml(record, open) {
  const outcome = OUTCOME[record.outcome_state] || null;
  const text = String(record.text || '').replace(/\s+/g, ' ').trim();
  const chips = [];
  const hasConfidence = record.jev_top != null;
  /* Only anomalies get a word in the row; success is the green dot. */
  if (outcome && outcome.chip) {
    chips.push(`<span class="lg-chip ${outcome.chip}">${esc(t(outcome.key))}</span>`);
  }
  if (record.sticky_hit || record.selection_reason === 'sticky') {
    chips.push(`<span class="lg-chip sticky">${esc(t('logs.reason.sticky'))}</span>`);
  }
  if (hasConfidence) chips.push(`<span class="lg-chip jev">JEV ${pct(record.jev_top)}</span>`);
  if (isFallback(record)) {
    chips.push(`<span class="lg-chip fallback">${esc(t(fallbackLabel(record)))}</span>`);
  }
  return `<button type="button" class="lg-row" aria-expanded="${open}" aria-controls="detail-${esc(record.id)}"
      title="${esc(text)}">
    <span class="lg-dot ${outcome ? outcome.tone : ''}" aria-hidden="true"></span>
    <span class="lg-time">${esc(fmtTime(record.ts))}</span>
    <span class="lg-text">${text ? esc(text) : `<span class="mut">${esc(t('logs.noText'))}</span>`}</span>
    ${chips.join('')}
    <span class="lg-lat">${esc(ms(latencyValue(record)))}</span>
    <span class="lg-model"><span class="ar" aria-hidden="true">→</span>${esc(record.model || '—')}</span>
    <span class="lg-chev" aria-hidden="true">›</span>
    <span class="sr-only">${esc(outcome ? t(outcome.key) : '')}</span>
  </button>`;
}

/* ── expanded evidence ─────────────────────────────────────────── */
function factHtml(label, value, mono) {
  return `<div class="lg-fact"><div class="k">${esc(label)}</div><div class="v${mono ? ' mono' : ''}">${value}</div></div>`;
}

/* ── compact decision summary: one glance, everything raw below ──
   Final model, target display name, selection type, routing latency,
   JEV rounds and total latency. Candidates/waves/probabilities live in
   the staged process section instead of repeating here. */
function summaryHtml(record) {
  const trace = Array.isArray(record.trace) ? record.trace : [];
  const waves = trace.filter(event => event && event.event === 'wave');
  const rounds = Number.isFinite(Number(record.waves)) ? Number(record.waves) : waves.length;
  const reason = record.selection_reason;
  const final = trace.reduce(
    (found, event) => event && event.event === 'final' ? event : found,
    null,
  );
  const finalKind = final && final.kind;
  let reasonKey = null;
  if (finalKind === 'target') reasonKey = 'logs.reason.target';
  else if (finalKind === 'otherwise') reasonKey = 'logs.reason.otherwise';
  else if (finalKind === 'error_fallback') reasonKey = 'logs.reason.errorFallback';
  else if (record.selection_reason === 'target') reasonKey = 'logs.reason.target';
  else if (isFallback(record)) reasonKey = 'logs.reason.fallback';

  const reasonTone = reasonKey === 'logs.reason.otherwise' ||
    reasonKey === 'logs.reason.errorFallback' || reasonKey === 'logs.reason.fallback'
    ? ' fallback' : '';
  const stickyChip = record.outcome_state !== 'routing_error' &&
    (record.selection_reason === 'sticky' || record.sticky_hit)
    ? `<span class="lg-chip sticky">${esc(t('logs.reason.sticky'))}</span>` : '';
  const head = [
    `<span class="lg-sum-model">→ ${esc(record.model || '—')}</span>`,
    `<span class="lg-sum-target">${esc(SRTrace.label(record.node_labels, record.target_id))}</span>`,
    record.outcome_state === 'routing_error' || !reasonKey
      ? ''
      : `<span class="lg-chip${reasonTone}">${esc(t(reasonKey))}</span>`,
    stickyChip,
    `<span class="lg-sum-kv">${esc(t('logs.field.rounds'))}<b>${esc(String(rounds))}</b></span>`,
    `<span class="lg-sum-kv">${esc(t('logs.f.routing'))}<b>${esc(ms(record.routing_ms))}</b></span>`,
    `<span class="lg-sum-kv">${esc(t('logs.f.total'))}<b>${esc(ms(latencyValue(record)))}</b></span>`,
  ].filter(Boolean).join('');
  return `<section class="lg-block lg-sum"><div class="lg-sum-head">${head}</div></section>`;
}

/* ── execution result: what the chosen upstream did ────────────── */
function executionHtml(record) {
  const outcome = OUTCOME[record.outcome_state];
  return `<section class="lg-block lg-execution"><div class="lg-sec-h">${esc(t('logs.execution'))}</div>
    <div class="lg-facts">
      ${factHtml(t('logs.f.upstreamModel'), esc(record.upstream_model || '—'), true)}
      ${factHtml(t('logs.f.upstreamStatus'), esc(record.upstream_status == null ? '—' : String(record.upstream_status)), true)}
      ${factHtml(t('logs.f.upstreamMs'), esc(ms(record.upstream_ms)), true)}
      ${factHtml(t('logs.f.ttft'), esc(ms(record.ttft_ms)), true)}
      ${factHtml(t('logs.f.total'), esc(ms(record.total_ms)), true)}
      ${factHtml(t('logs.f.tokensIn'), esc(record.prompt_tokens == null ? '—' : String(record.prompt_tokens)), true)}
      ${factHtml(t('logs.f.tokensOut'), esc(record.completion_tokens == null ? '—' : String(record.completion_tokens)), true)}
      ${factHtml(t('logs.f.tokensTotal'), esc(record.total_tokens == null ? '—' : String(record.total_tokens)), true)}
      ${factHtml(t('logs.f.outcome'), esc(t(outcome ? outcome.key : 'logs.out.incomplete')))}
    </div>
  </section>`;
}

/* ── folded technical facts: ids/revisions/session live here ───── */
function technicalFacts(record) {
  const reasoning = String(record.reasoning || '').trim();
  const warning = String(record.reasoning_warning || '').trim();
  return [
    factHtml(t('logs.f.id'), esc(record.id || '—'), true),
    factHtml(t('logs.f.time'), esc(fmtTime(record.ts)), true),
    factHtml(t('logs.f.tree'), esc(record.tree_id || '—'), true),
    factHtml(t('logs.f.treeRev'), esc(record.tree_revision == null ? '—' : String(record.tree_revision)), true),
    factHtml(t('logs.f.snapshot'), esc(record.snapshot_revision == null ? '—' : String(record.snapshot_revision)), true),
    factHtml(t('logs.f.policy'), esc(record.policy_path || '—'), true),
    factHtml(t('logs.f.session'), esc(record.session_hash || '—'), true),
    factHtml(t('logs.f.jevModel'), esc(record.jev_model || '—'), true),
    factHtml(t('logs.sec.reasoning'), esc(reasoning || '—')),
    factHtml(t('logs.reasoningWarning'), esc(warning || '—')),
  ];
}

function detailHtml(record) {
  const hasTrace = Array.isArray(record.trace);
  return `<div class="lg-expand-in">
    ${summaryHtml(record)}
    ${hasTrace
      ? `<section class="lg-block"><div class="lg-sec-h">${esc(t('logs.process'))}</div><div class="lg-process" data-trace></div></section>`
      : `<div class="lg-note">${esc(t('logs.historyDetail'))}</div>`}
    ${record.outcome_state === 'routing_error' ? '' : executionHtml(record)}
    <details class="lg-more">
      <summary>${esc(t('logs.technical'))}</summary>
      <div class="lg-facts">${technicalFacts(record).join('')}</div>
    </details>
  </div>`;
}

function itemHtml(record) {
  const open = openIds.has(record.id);
  return `<article class="lg-item${open ? ' open' : ''}" data-id="${esc(record.id)}">
    ${rowHtml(record, open)}
    <div class="lg-expand" id="detail-${esc(record.id)}"${open ? '' : ' hidden'}>${detailHtml(record)}</div>
  </article>`;
}

/* The staged process is only built when its row is opened. */
function paintTrace(item, record) {
  const host = item.querySelector('[data-trace]');
  if (!host || host.dataset.ready === '1') return;
  SRTrace.render(host, record);
  host.dataset.ready = '1';
}

function render() {
  const rows = shown();
  $('#countPill').innerHTML = `${rows.length}<span class="mut"> / ${requests.length}</span>`;
  renderStats();
  renderTabs();
  $('#list').innerHTML = rows.map(itemHtml).join('')
    || `<div class="lg-list-empty"><b>${esc(t(requests.length ? 'logs.emptyFilter' : 'logs.empty'))}</b></div>`;
  document.querySelectorAll('#list .lg-item').forEach(item => {
    const record = requests.find(entry => entry.id === item.dataset.id);
    if (!record) return;
    if (item.classList.contains('open')) paintTrace(item, record);
  });
}

/* ── events ────────────────────────────────────────────────────── */
function toggle(item) {
  const id = item.dataset.id;
  const opening = !openIds.has(id);
  if (opening) openIds.add(id); else openIds.delete(id);
  item.classList.toggle('open', opening);
  const button = item.querySelector('.lg-row');
  const panel = item.querySelector('.lg-expand');
  if (button) button.setAttribute('aria-expanded', String(opening));
  if (panel) panel.hidden = !opening;
  if (!opening) return;
  const record = requests.find(entry => entry.id === id);
  if (record) paintTrace(item, record);
}

$('#refresh').addEventListener('click', load);
$('#filter').addEventListener('click', event => {
  const button = event.target.closest('button[data-filter]');
  if (!button || button.dataset.filter === filter) return;
  filter = button.dataset.filter;
  render();
});
$('#list').addEventListener('click', event => {
  const row = event.target.closest('.lg-row');
  if (row) toggle(row.closest('.lg-item'));
});

async function load() {
  try {
    const [logs, jev] = await Promise.all([api.get('/admin/requests/recent'), api.get('/admin/jev')]);
    requests = logs.requests || [];
    for (const id of Array.from(openIds)) {
      if (!requests.some(record => record.id === id)) openIds.delete(id);
    }
    $('#taggerPill').textContent = (jev.config && jev.config.model) || 'JEV';
    render();
  } catch (error) {
    requests = [];
    openIds.clear();
    $('#countPill').textContent = '0';
    $('#stats').innerHTML = '';
    $('#list').innerHTML = `<div class="lg-list-empty"><b>${esc(apiErrText(error))}</b></div>`;
  }
}

window.onLangChange = render;
load();
