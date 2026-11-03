/* Routing time-path renderer for the calculation-log detail panel.
   Input is one request record's `trace` (list of engine events, oldest
   first); output is a vertical timeline. Rendering is read-only — the log
   page owns selection and layout. */
const SRTrace = (() => {
  /* Graph-v3 executor events plus the pre-graph names that can still exist in
     the in-memory ring from an older process. Unknown events deliberately
     fall back to their raw token instead of disappearing. */
  const LABELS = {
    time: 'logs.ev.time', window: 'logs.ev.window', jev_input_window: 'logs.ev.window',
    condition: 'logs.ev.condition', slot: 'logs.ev.slot', gate: 'logs.ev.gate',
    activate: 'logs.ev.activate', wave: 'logs.ev.wave', question: 'logs.ev.question',
    final: 'logs.ev.final', fallback: 'logs.ev.fallback', sticky_hit: 'logs.ev.sticky',
    scheduled: 'logs.ev.scheduled', jev_choice: 'logs.ev.jev',
    branch_selected: 'logs.ev.branch', branch_skipped: 'logs.ev.branchSkip',
    branch_otherwise: 'logs.ev.branchOther',
  };
  /* Tones drive the timeline dot: green = decided, amber = degraded,
     red = error. Anything unknown keeps the neutral dot. */
  const TONES = {
    scheduled: 'ok', sticky_hit: 'ok', branch_skipped: 'warn',
    fallback: 'warn', jev_choice: 'ok', final: 'ok', gate: 'ok',
    activate: 'ok', condition: 'ok', question: 'ok',
  };
  /* Event fields worth showing, in a stable order, with an i18n label key. */
  const FIELDS = [
    ['wave', 'logs.field.wave'],
    ['node_id', 'logs.field.node'], ['port', 'logs.field.port'],
    ['source', 'logs.field.source'], ['source_port', 'logs.field.sourcePort'],
    ['via_port', 'logs.field.viaPort'], ['edge_id', 'logs.field.edge'],
    ['operator', 'logs.field.operator'], ['status', 'logs.field.status'],
    ['raw', 'logs.field.raw'], ['condition', 'logs.field.condition'],
    ['value', 'logs.field.value'], ['inputs', 'logs.field.inputs'],
    ['questions', 'logs.field.questions'], ['matched', 'logs.field.matched'],
    ['ports', 'logs.field.ports'],
    ['selected', 'logs.field.selected'], ['selected_key', 'logs.field.selected'],
    ['p_top', 'logs.field.pTop'], ['margin', 'logs.field.margin'],
    ['usage', 'logs.field.usage'], ['failed', 'logs.field.failed'],
    ['model', 'logs.field.model'], ['target_id', 'logs.field.target'],
    ['kind', 'logs.field.kind'], ['candidates', 'logs.field.candidates'],
    ['priority', 'logs.field.priority'], ['reason', 'logs.field.reason'],
    ['dry_run', 'logs.field.dryRun'], ['error', 'logs.field.error'],
    ['input_tokens_limit', 'logs.field.limit'],
    ['tokens_before', 'logs.field.before'], ['tokens_after', 'logs.field.after'],
    ['truncated', 'logs.field.truncated'],
    ['policy_path', 'logs.field.path'], ['child_id', 'logs.field.child'],
  ];

  function tone(event) {
    if (event.error || event.reason === 'jev_error') return 'bad';
    if (event.event === 'fallback') return 'warn';
    if ((event.event === 'window' || event.event === 'jev_input_window') && event.truncated) return 'warn';
    return TONES[event.event] || '';
  }

  /* Long structured fields (inputs/ports/candidates) stay one line. */
  function stringify(value) {
    let text;
    if (typeof value === 'object' && value !== null) {
      try { text = JSON.stringify(value); } catch (error) { text = String(value); }
    } else {
      text = String(value);
    }
    return text.length > 220 ? text.slice(0, 217) + '…' : text;
  }

  function conditionText(value) {
    if (!value || typeof value !== 'object') return stringify(value);
    if (!value.op) return stringify(value.value);
    const symbol = (typeof RTGraph !== 'undefined' && RTGraph.OP && RTGraph.OP[value.op]) || value.op;
    return symbol + ' ' + stringify(value.value);
  }

  function display(field, value) {
    if (field === 'p_top' || field === 'margin') return pct(value);
    if (field === 'condition') return conditionText(value);
    if (field === 'raw' || field === 'value') {
      if (value === true) return 'true';
      if (value === false) return 'false';
      if (value == null) return '—';
    }
    if (field === 'questions' && Array.isArray(value)) return value.join(', ');
    if (field === 'matched' && Array.isArray(value)) return value.join(', ');
    return stringify(value);
  }

  function pairs(event) {
    const out = [];
    if (Array.isArray(event.active_periods) && event.active_periods.length) {
      out.push([t('logs.field.periods'), event.active_periods.join(', ')]);
    }
    if (event.timezone) out.push([t('logs.field.tz'), event.timezone]);
    FIELDS.forEach(([field, labelKey]) => {
      const value = event[field];
      /* 只有槽位/门/条件事件里 raw/value 的 null（unknown）是有意义证据；
         其他事件缺字段一律省略，避免整条时间线被 “上游值 —” 噪音淹没。 */
      const nullIsEvidence = (field === 'raw' || field === 'value') &&
        ['slot', 'gate', 'condition'].indexOf(event.event) >= 0;
      if (value == null && !nullIsEvidence) return;
      if (value === '') return;
      out.push([t(labelKey), display(field, value)]);
    });
    return out;
  }

  function stepHtml(event, index) {
    const kind = event.event || 'event';
    const label = LABELS[kind] ? t(LABELS[kind]) : kind;
    const timing = event.duration_ms != null ? Math.round(event.duration_ms) + 'ms' : '';
    const chips = pairs(event)
      .map(([key, value]) => `<span class="lg-ev"><b>${esc(key)}</b>${esc(value)}</span>`)
      .join('');
    return `<li class="lg-step" data-kind="${esc(tone(event))}">
      <div class="lg-step-h"><span class="lg-step-n">${index + 1}</span><b>${esc(label)}</b><span class="lg-step-t">${esc(timing)}</span></div>
      ${chips ? `<div class="lg-step-d">${chips}</div>` : ''}
    </li>`;
  }

  return {
    render(host, trace) {
      const events = Array.isArray(trace) ? trace : [];
      host.innerHTML = events.length
        ? `<ol class="lg-timeline">${events.map(stepHtml).join('')}</ol>`
        : `<div class="lg-none">${esc(t('logs.traceEmpty'))}</div>`;
    },
  };
})();
