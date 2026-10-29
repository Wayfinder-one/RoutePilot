const SRTrace = (() => {
  function probabilities(values) {
    const entries = Object.entries(values || {});
    if (!entries.length) return '';
    return `<div class="rtt-probs">${entries.map(([key, value]) => `<div class="bar-row"><span class="bl">${esc(key)}</span><div class="scorebar"><i style="width:${Math.round(Number(value) * 100)}%"></i></div><span class="confv">${pct(value)}</span></div>`).join('')}</div>`;
  }

  function eventHtml(event) {
    const type = event.event || 'event';
    const details = [];
    if (event.active_periods) details.push((event.active_periods || []).join(', ') || '—');
    if (event.child_id) details.push('→ ' + event.child_id);
    if (event.selected_key) details.push('selected: ' + event.selected_key);
    if (event.p_top != null) details.push(`p_top=${pct(event.p_top)} margin=${pct(event.margin)}`);
    if (event.model) details.push(event.model);
    if (event.target_id) details.push(event.target_id);
    if (event.reason) details.push(event.reason);
    if (event.error) details.push(event.error);
    return `<div class="tstep"><span class="tt">${esc(type)}</span><span class="desc">${esc(details.join(' · '))}</span></div>`;
  }

  function summary(title, record) {
    const chips = [record.selection_reason, record.policy_path, record.fallback_reason].filter(Boolean);
    if (record.sticky_hit) chips.push('sticky');
    return `<div class="rtt-summary"><b>${esc(title || '—')}</b>${chips.map(chip => `<span class="pill">${esc(chip)}</span>`).join('')}</div>`;
  }

  function render(host, { title, record, answer, trace }) {
    host.innerHTML = `<div class="rtt">${summary(title, record)}${probabilities(answer)}<div class="trace">${(trace || []).map(eventHtml).join('')}</div></div>`;
  }

  return {
    renderTry(host, data) {
      const outcome = data.outcome || {};
      render(host, { title: outcome.model, record: outcome, answer: data.jev && data.jev.probabilities, trace: data.trace });
    },
    renderRecord(host, record) {
      render(host, { title: record.model, record, answer: record.jev_probabilities, trace: record.trace });
    },
  };
})();
