/* Routing decision-path renderer for the calculation-log detail panel.
   Only key judgments (slot/gate/condition) and value outputs
   (question/final) are shown; wave/window/scheduled/activate/sticky events
   belong to the summary area above. Each case lists the fields it needs.
   Rendering is read-only — the log page owns selection and layout. */
const SRTrace = (() => {
  /* 保留的关键点事件；其余事件不渲染（用户明确选择不留完整事件入口）。 */
  const KEEP = ['slot', 'gate', 'condition', 'question', 'final'];
  const LABELS = {
    slot: 'logs.kp.slot', gate: 'logs.kp.gate', condition: 'logs.kp.condition',
    question: 'logs.kp.question', final: 'logs.kp.final',
  };

  function stringify(value) {
    let text;
    if (typeof value === 'object' && value !== null) {
      try { text = JSON.stringify(value); } catch (error) { text = String(value); }
    } else {
      text = String(value);
    }
    return text.length > 120 ? text.slice(0, 117) + '…' : text;
  }

  /* 日志页不加载 routing/graph.js（没有 RTGraph），条件符号必须自带映射。 */
  const OP_SYMBOL = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠' };

  function conditionText(value) {
    if (!value || typeof value !== 'object') return stringify(value);
    if (!value.op) return stringify(value.value);
    const symbol = OP_SYMBOL[value.op] || value.op;
    return symbol + ' ' + stringify(value.value);
  }

  function pctText(value) {
    if (value == null || value === '') return '—';
    const number = Number(value);
    return Number.isFinite(number) ? Math.round(number * 100) + '%' : stringify(value);
  }

  function statusText(status) {
    if (status === 'true' || status === true) return t('logs.kp.hit');
    if (status === 'false' || status === false) return t('logs.kp.miss');
    if (status === 'invalid') return t('logs.kp.invalid');
    return t('logs.kp.unknown');
  }

  /* 绿点 = 判断为真；灰点 = 判断为假（明确结论）；琥珀 = 未定；红点 = 非法值。 */
  function tone(event) {
    if (!event) return '';
    if (event.status === 'invalid') return 'bad';
    if (event.status === 'unknown' || event.value === null) return 'warn';
    if (event.status === 'false' || event.value === false) return '';
    return 'ok';
  }

  /* 每类关键点只取需要的字段；缺失字段不渲染。 */
  function chips(event) {
    const out = [];
    const push = (label, value) => {
      if (value == null || value === '') return;
      out.push(`<span class="lg-ev"><b>${esc(label)}</b>${esc(value)}</span>`);
    };
    const node = event.node_id ? String(event.node_id) : '';
    switch (event.event) {
      case 'slot': {
        push(t('logs.field.node'), node + (event.port ? '.' + String(event.port) : ''));
        if (event.condition != null) push(t('logs.field.condition'), conditionText(event.condition));
        push(t('logs.field.raw'), event.raw == null ? '—' : stringify(event.raw));
        push(t('logs.field.status'), statusText(event.status));
        break;
      }
      case 'gate': {
        push(t('logs.field.node'), node);
        push(t('logs.field.operator'), String(event.operator || ''));
        if (Array.isArray(event.inputs)) {
          push(t('logs.field.inputs'), event.inputs.map(item => statusText(item && item.status)).join(' / '));
        }
        push(t('logs.field.value'),
          event.value === true ? 'true' : event.value === false ? 'false' : statusText(event.value));
        break;
      }
      case 'condition': {
        push(t('logs.field.node'), node);
        push(t('logs.kp.source'),
          t(event.source === 'periods' ? 'logs.kp.sourcePeriods' : 'logs.kp.sourceText'));
        if (Array.isArray(event.matched)) {
          push(t('logs.kp.matched'), event.matched.length ? event.matched.join(', ') : '—');
        }
        push(t('logs.field.value'),
          event.value === true ? 'true' : event.value === false ? 'false' : statusText(event.value));
        break;
      }
      case 'question': {
        push(t('logs.field.node'), node);
        if (event.selected != null) {
          push(t('logs.field.selected'), String(event.selected));
          push(t('logs.field.pTop'), pctText(event.p_top));
          push(t('logs.field.margin'), pctText(event.margin));
        } else if (event.ports && typeof event.ports === 'object') {
          const entries = Object.entries(event.ports)
            .filter(([key]) => ['yes', 'no', 'value'].indexOf(key) >= 0);
          if (entries.length) {
            push(t('logs.kp.valueOutput'), entries.map(([key, value]) =>
              key + ' ' + (key === 'value' ? stringify(value) : pctText(value))).join(' · '));
          }
        }
        break;
      }
      case 'final': {
        push(t('logs.field.target'), String(event.target_id || '—'));
        push(t('logs.field.kind'), String(event.kind || ''));
        if (event.priority != null) push(t('logs.field.priority'), String(event.priority));
        if (Array.isArray(event.candidates)) push(t('logs.kp.candidates'), String(event.candidates.length));
        break;
      }
      default:
        break;
    }
    return out;
  }

  function stepHtml(event) {
    const label = LABELS[event.event] ? t(LABELS[event.event]) : event.event;
    const body = chips(event);
    return `<li class="lg-step" data-kind="${esc(tone(event))}">
      <div class="lg-step-h"><b>${esc(label)}</b></div>
      ${body.length ? `<div class="lg-step-d">${body.join('')}</div>` : ''}
    </li>`;
  }

  function kept(trace) {
    return (Array.isArray(trace) ? trace : [])
      .filter(event => event && KEEP.indexOf(event.event) >= 0);
  }

  return {
    count(trace) {
      return kept(trace).length;
    },
    render(host, trace) {
      const events = kept(trace);
      host.innerHTML = events.length
        ? `<ol class="lg-timeline">${events.map(stepHtml).join('')}</ol>`
        : `<div class="lg-none">${esc(t('logs.traceEmpty'))}</div>`;
    },
  };
})();
