/* Routing time-path renderer for the calculation-log detail panel.
   Input is one request record's `trace` (list of engine events, oldest
   first); output is a vertical timeline. Rendering is read-only — the log
   page owns selection and layout. */
const SRTrace = (() => {
  const LABELS = {
    time: 'logs.ev.time', jev_choice: 'logs.ev.jev', sticky_hit: 'logs.ev.sticky',
    fallback: 'logs.ev.fallback', scheduled: 'logs.ev.scheduled',
    branch_selected: 'logs.ev.branch', branch_skipped: 'logs.ev.branchSkip',
    branch_otherwise: 'logs.ev.branchOther', jev_input_window: 'logs.ev.window',
  };
  /* Tones drive the timeline dot: green = decided, amber = degraded,
     red = error. Anything unknown keeps the neutral dot. */
  const TONES = {
    scheduled: 'ok', sticky_hit: 'ok', branch_skipped: 'warn',
    fallback: 'warn', jev_choice: 'ok',
  };
  /* Event fields worth showing, in a stable order, with a display label. */
  const FIELDS = [
    ['selected_key', 'selected'], ['p_top', 'p_top'], ['margin', 'margin'],
    ['model', 'model'], ['target_id', 'target'], ['child_id', 'child'], ['node_id', 'node'],
    ['policy_path', 'path'], ['reason', 'reason'], ['error', 'error'],
    ['input_tokens_limit', 'limit'], ['tokens_before', 'before'], ['tokens_after', 'after'],
    ['truncated', 'truncated'],
  ];

  function tone(event) {
    if (event.error || event.reason === 'jev_error') return 'bad';
    if (event.event === 'fallback') return 'warn';
    if (event.event === 'jev_input_window' && event.truncated) return 'warn';
    return TONES[event.event] || '';
  }

  function pairs(event) {
    const out = [];
    if (Array.isArray(event.active_periods) && event.active_periods.length) {
      out.push(['periods', event.active_periods.join(', ')]);
    }
    if (event.timezone) out.push(['tz', event.timezone]);
    FIELDS.forEach(([field, label]) => {
      const value = event[field];
      if (value == null || value === '') return;
      if (field === 'p_top' || field === 'margin') out.push([label, pct(value)]);
      else out.push([label, String(value)]);
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
