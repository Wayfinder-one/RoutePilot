/* Calculation log — a disclosure list.
   Each row is one request and stays one glance wide (model, outcome,
   latency, evidence chips). Clicking a row opens its decision evidence
   inline, below the row: request text, facts, JEV probabilities, time path
   and reasoning. Rows do not navigate elsewhere and never become a side
   panel; several rows can stay open so two decisions can be compared. */
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
};
/* Graph v3 selection_reason enum: target | sticky | otherwise | error_fallback.
   "fallback" is the pre-graph value and stays accepted so old in-memory rows
   still count until the process restarts. */
const FALLBACK_REASONS = ['otherwise', 'error_fallback', 'fallback'];
const isFallback = record => FALLBACK_REASONS.indexOf(record.selection_reason) >= 0;
const fallbackLabel = record => {
  if (record.selection_reason === 'error_fallback') return 'logs.reason.errorFallback';
  if (record.selection_reason === 'otherwise') return 'logs.reason.otherwise';
  return 'logs.reason.fallback';
};
const FILTERS = {
  all: () => true,
  jev: record => record.jev_top != null,
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
  const timed = requests.map(record => Number(record.latency_ms)).filter(Number.isFinite);
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
    <span class="lg-lat">${esc(ms(record.latency_ms))}</span>
    <span class="lg-model"><span class="ar" aria-hidden="true">→</span>${esc(record.model || '—')}</span>
    <span class="lg-chev" aria-hidden="true">›</span>
    <span class="sr-only">${esc(outcome ? t(outcome.key) : '')}</span>
  </button>`;
}

/* ── expanded evidence ─────────────────────────────────────────── */
function probBars(values) {
  const entries = Object.entries(values).sort((a, b) => Number(b[1]) - Number(a[1]));
  const top = entries.length ? Number(entries[0][1]) : 0;
  return entries.map(([key, value]) => {
    const width = Math.max(2, Math.round(Number(value) * 100));
    return `<div class="lg-prob${Number(value) === top ? ' sel' : ''}">
      <span class="k" title="${esc(key)}">${esc(key)}</span>
      <span class="track"><i style="width:${width}%"></i></span>
      <span class="v">${pct(value)}</span>
    </div>`;
  }).join('');
}

function factHtml(label, value, mono) {
  return `<div class="lg-fact"><div class="k">${esc(label)}</div><div class="v${mono ? ' mono' : ''}">${value}</div></div>`;
}

/* ── decision summary: answer "why this model" in three compact rows ──
   The decision path and low-frequency facts stay folded below; the
   default view only carries what a reviewer needs at a glance. */
function summaryHtml(record) {
  const trace = Array.isArray(record.trace) ? record.trace : [];
  const waves = trace.filter(event => event && event.event === 'wave');
  const final = trace.find(event => event && event.event === 'final');
  const candidates = final && Array.isArray(final.candidates) ? final.candidates : [];
  const rounds = Number.isFinite(Number(record.waves)) ? Number(record.waves) : waves.length;
  const reason = record.selection_reason;
  const reasonKey = reason === 'error_fallback' ? 'logs.reason.errorFallback'
    : reason === 'otherwise' ? 'logs.reason.otherwise'
      : reason === 'sticky' ? 'logs.reason.sticky' : null;
  const head = [
    `<span class="lg-sum-model">→ ${esc(record.model || '—')}</span>`,
    record.target_id ? `<span class="lg-sum-target">${esc(record.target_id)}</span>` : '',
    reasonKey ? `<span class="lg-chip fallback">${esc(t(reasonKey))}</span>` : '',
    `<span class="lg-sum-kv">${esc(t('logs.sum.rounds'))}<b>${esc(String(rounds))}</b></span>`,
    `<span class="lg-sum-kv">${esc(t('logs.f.routing'))}<b>${esc(ms(record.routing_ms))}</b></span>`,
    record.sticky_hit ? `<span class="lg-chip sticky">${esc(t('logs.f.stickyHit'))}</span>` : '',
  ].filter(Boolean).join('');
  const candRow = `<div class="lg-sum-row"><span class="lg-sum-k">${esc(t('logs.sum.candidates'))}</span>${
    candidates.length
      ? `<span class="lg-cands">${candidates.map(item =>
        `<span class="lg-cand${item.target_id === record.target_id ? ' win' : ''}">` +
        `<b>${esc(item.target_id)}</b>` +
        `<i>${esc(t('logs.field.priority'))} ${esc(String(item.priority))}</i>` +
        `<i>${esc(tf('logs.sum.round', { n: item.wave }))}</i>` +
        `<i>${esc(t('logs.sum.via'))} ${esc(item.via_node || '—')}</i></span>`).join('')}</span>`
      : `<span class="mut">${esc(t('logs.sum.noCandidates'))}</span>`
  }</div>`;
  const waveRow = waves.length
    ? `<div class="lg-sum-row"><span class="lg-sum-k">${esc(t('logs.sum.rounds'))}</span><span class="lg-waves">${
      waves.map(item =>
        `<span class="lg-wave${item.failed ? ' bad' : ''}">` +
        `<b>${esc(tf('logs.sum.round', { n: item.wave }))}</b>` +
        `<i>${esc((item.questions || []).join(', ') || '—')}</i>` +
        `<i>${esc(item.failed ? t('logs.field.failed') : ms(item.duration_ms))}</i></span>`).join('')
    }</span></div>`
    : '';
  return `<section class="lg-block lg-sum"><div class="lg-sum-head">${head}</div>${candRow}${waveRow}</section>`;
}

function detailHtml(record) {
  const facts = [
    factHtml(t('logs.f.time'), esc(fmtTime(record.ts)), true),
    factHtml(t('logs.f.routing'), esc(ms(record.routing_ms)), true),
    factHtml(t('logs.f.sticky'), record.sticky_hit
      ? `<span class="lg-ok">${esc(t('logs.f.stickyHit'))}</span>`
      : `<span class="mut">${esc(t('logs.f.stickyMiss'))}</span>`),
    factHtml(t('logs.f.tree'), esc(record.tree_id || '—')),
    factHtml(t('logs.f.snapshot'), esc(String(record.snapshot_revision || '—').slice(0, 12)), true),
    factHtml(t('logs.f.policy'), esc(record.policy_path || '—'), true),
  ];
  if (record.total_tokens != null) {
    facts.push(factHtml(t('logs.f.tokens'), `${Number(record.prompt_tokens || 0)} → ${Number(record.total_tokens)}`, true));
  }
  if (record.upstream_status != null) {
    facts.push(factHtml(t('logs.f.upstream'), `${esc(String(record.upstream_status))} · ${esc(ms(record.upstream_ms))}`, true));
  }
  if (record.fallback_reason) {
    facts.push(factHtml(t('logs.f.fallback'), esc(record.fallback_reason), true));
  }

  const prompt = String(record.text || '').trim();
  const probabilities = record.jev_probabilities && Object.keys(record.jev_probabilities).length
    ? probBars(record.jev_probabilities) : '';
  const reasoning = String(record.reasoning || '').trim();
  const warning = String(record.reasoning_warning || '').trim();
  const traceCount = SRTrace.count(record.trace);

  return `<div class="lg-expand-in">
    ${summaryHtml(record)}
    ${prompt ? `<section class="lg-block"><div class="lg-sec-h">${esc(t('logs.sec.prompt'))}</div><div class="lg-prompt">${esc(prompt)}</div></section>` : ''}
    ${probabilities ? `<section class="lg-block"><div class="lg-sec-h">${esc(t('logs.sec.probs'))}</div><div class="lg-probs">${probabilities}</div></section>` : ''}
    ${reasoning ? `<section class="lg-block"><div class="lg-sec-h">${esc(t('logs.sec.reasoning'))}</div><div class="lg-quote">${esc(reasoning)}</div></section>` : ''}
    ${warning ? `<div class="lg-note"><b>${esc(t('logs.reasoningWarning'))}</b>${esc(warning)}</div>` : ''}
    <details class="lg-more lg-more--trace">
      <summary>${esc(tf('logs.kp.title', { n: traceCount }))}</summary>
      <div class="lg-trace" data-trace></div>
    </details>
    <details class="lg-more">
      <summary>${esc(t('logs.facts'))}</summary>
      <div class="lg-facts">${facts.join('')}</div>
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

/* The raw time path is only built when its <details> is expanded. */
function paintTrace(item, record) {
  const host = item.querySelector('.lg-trace');
  if (!host || host.dataset.ready === '1') return;
  SRTrace.render(host, record.trace);
  host.dataset.ready = '1';
}

function bindTrace(item) {
  const details = item.querySelector('details.lg-more--trace');
  if (!details || details.dataset.bound === '1') return;
  details.dataset.bound = '1';
  details.addEventListener('toggle', () => {
    if (!details.open) return;
    const record = requests.find(entry => entry.id === item.dataset.id);
    if (record) paintTrace(item, record);
  });
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
    bindTrace(item);
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
