/* Staged calculation renderer for the calculation-log detail panel.
   One record renders top-down as input → initial computation → JEV waves
   → candidates → schedule → execution failure, so a reviewer reads the
   calculation in execution order. build()/label()/hasJev() stay pure
   view-models; rendering is read-only — the log page owns selection,
   layout and lazy painting. */
const SRTrace = (() => {
  function stringify(value) {
    let text;
    if (typeof value === 'object' && value !== null) {
      try { text = JSON.stringify(value); } catch (error) { text = String(value); }
    } else {
      text = String(value);
    }
    return text.length > 120 ? text.slice(0, 117) + '…' : text;
  }

  /* 单一非关键字段缺失统一显示 —，不丢弃问题或整轮。 */
  const DASH = '—';
  const text = value => (value == null || value === '' ? DASH : String(value));

  /* 日志页不加载 routing/graph.js（没有 RTGraph），条件符号必须自带映射。 */
  const OP_SYMBOL = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠' };

  function conditionText(condition) {
    if (!condition || typeof condition !== 'object') return stringify(condition);
    /* 布尔线条件是 op === 'is'（= true / = false）。 */
    if (condition.op === 'is') return '= ' + stringify(condition.value);
    const symbol = Object.prototype.hasOwnProperty.call(OP_SYMBOL, condition.op)
      ? OP_SYMBOL[condition.op] : condition.op;
    return symbol + ' ' + stringify(condition.value);
  }

  function pctText(value) {
    if (value == null || value === '') return DASH;
    const number = Number(value);
    return Number.isFinite(number) ? Math.round(number * 100) + '%' : stringify(value);
  }

  function msText(value) {
    const number = Number(value);
    if (value == null || value === '' || !Number.isFinite(number)) return DASH;
    return number >= 1000 ? (number / 1000).toFixed(2) + 's' : Math.round(number) + 'ms';
  }

  /* 槽位四态：命中 / 未命中 / 未定 / 非法（invalid 是独立状态，不能折叠成未定）。 */
  function statusText(status) {
    if (status === 'true' || status === true) return t('logs.slot.hit');
    if (status === 'false' || status === false) return t('logs.slot.miss');
    if (status === 'invalid') return t('logs.slot.invalid');
    return t('logs.slot.unknown');
  }

  function slotStatus(status) {
    if (status === 'true' || status === true) return 'true';
    if (status === 'false' || status === false) return 'false';
    if (status === 'invalid') return 'invalid';
    return 'unknown';
  }

  /* sticky 按状态原值映射；not_checked 是「未检查」，不能与 miss 混淆。 */
  function stickyText(status) {
    const name = String(status == null || status === '' ? 'not_checked' : status);
    const label = t('logs.sticky.' + name);
    return label.indexOf('logs.sticky.') === 0 ? name : label;
  }

  function label(nodeLabels, id) {
    const key = id == null ? '' : String(id);
    const item = nodeLabels && nodeLabels[key];
    return item && item.title ? String(item.title) : key || DASH;
  }

  /* 目标等节点同时给出显示名与原始 id，便于和 trace/图对照。 */
  function nodeRef(labels, id) {
    const shown = label(labels, id);
    return shown === text(id) ? esc(shown) : esc(shown) + ' <small>' + esc(text(id)) + '</small>';
  }

  function judgment(name, body) {
    return `<div class="lg-judgment"><b>${esc(name)}</b><span>${body}</span></div>`;
  }

  function listText(value) {
    return Array.isArray(value) && value.length ? value.map(item => text(item)).join(', ') : DASH;
  }

  /* 不可用原因按原值映射（unregistered / breaker_open）；未知值回退原样显示。 */
  function unavailableText(reason) {
    const label = t('logs.unavailable.' + reason);
    return label.indexOf('logs.unavailable.') === 0 ? reason : label;
  }

  /* ── 输入准备 ──────────────────────────────────────────────────── */
  function inputHtml(record, model) {
    const time = model.input.time || {};
    const win = model.input.window || {};
    const periods = Array.isArray(time.active_periods) ? listText(time.active_periods) : DASH;
    const tokens = (win.tokens_before == null && win.tokens_after == null)
      ? DASH
      : `${esc(text(win.tokens_before))} → ${esc(text(win.tokens_after))}` +
        (win.truncated ? ` · ${esc(t('logs.f.truncated'))}` : '');
    const prompt = String(record.text || '');
    return `<section class="lg-input"><div class="lg-sec-h">${esc(t('logs.input'))}</div>
      <div class="lg-judgments">
        ${judgment(t('logs.f.periods'), esc(periods))}
        ${judgment(t('logs.f.timezone'), esc(text(time.timezone)))}
        ${judgment(t('logs.f.window'), tokens)}
        ${judgment(t('logs.requestText'), `<span class="lg-prompt-clamp">${esc(prompt)}</span>`)}
      </div>
      ${prompt ? `<details class="lg-sub"><summary>${esc(t('logs.fullText'))}</summary>
        <div class="lg-fulltext">${esc(prompt)}</div></details>` : ''}
    </section>`;
  }

  /* ── 单条判断行 ────────────────────────────────────────────────── */
  function conditionHtml(event, labels) {
    const source = event.source === 'periods'
      ? t('logs.f.periods') : t('logs.f.text');
    const matched = Array.isArray(event.matched) ? listText(event.matched) : DASH;
    return judgment(label(labels, event.node_id),
      `<small>${esc(t('logs.field.source'))} ${esc(source)} · ${esc(t('logs.field.matched'))} ${esc(matched)}</small> ` +
      esc(event.value === true ? 'true' : event.value === false ? 'false' : text(event.value)));
  }

  function gateHtml(event, labels) {
    const value = event.value === true || event.value === false
      ? String(event.value) : statusText(event.value);
    let body = judgment(label(labels, event.node_id),
      `<small>${esc(text(event.operator))}</small> → ${esc(value)}`);
    if (Array.isArray(event.inputs) && event.inputs.length) {
      const rows = event.inputs.map(input => '<div>' +
        esc(text(input && input.port)) + ' · ' + esc(text(input && input.raw)) +
        (input && input.condition ? ' ' + esc(conditionText(input.condition)) : '') +
        ' → ' + esc(statusText(input && input.status)) + '</div>').join('');
      body += `<details class="lg-sub"><summary>${esc(t('logs.field.inputs'))}</summary>
        <div class="lg-inputs">${rows}</div></details>`;
    }
    return `<div class="lg-judgment-row">${body}</div>`;
  }

  function questionHtml(event, labels) {
    const ports = event.ports && typeof event.ports === 'object' ? event.ports : {};
    const probs = Object.entries(ports)
      .filter(([key]) => String(key).indexOf('prob.') === 0)
      .map(([key, value]) => [String(key).slice(5), value])
      .sort((a, b) => Number(b[1]) - Number(a[1]));
    let line = '';
    if (event.selected != null) {
      /* Choice：概率条 + 事件自带的 selected/p_top/margin。 */
      line = `${esc(t('logs.field.selected'))} ${esc(text(event.selected))} · ` +
        `${esc(t('logs.field.pTop'))} ${esc(pctText(event.p_top))} · ` +
        `${esc(t('logs.field.margin'))} ${esc(pctText(event.margin))}`;
    } else if (ports.yes != null || ports.no != null) {
      line = `P(yes) ${esc(pctText(ports.yes))} · P(no) ${esc(pctText(ports.no))}`;
    } else if (ports.value != null) {
      line = `${esc(t('logs.field.value'))} ${esc(text(ports.value))}`;
    } else {
      line = DASH;
    }
    const bars = probs.length ? `<div class="lg-probs">${probs.map(([key, value], index) => {
      const width = Math.max(2, Math.round(Number(value) * 100) || 0);
      return `<div class="lg-prob${index === 0 ? ' sel' : ''}">
        <span class="k" title="${esc(key)}">${esc(key)}</span>
        <span class="track"><i style="width:${width}%"></i></span>
        <span class="v">${esc(pctText(value))}</span>
      </div>`;
    }).join('')}</div>` : '';
    const facts = [];
    if (event.duration_ms != null) {
      facts.push(judgment(t('logs.field.duration'), esc(msText(event.duration_ms))));
    }
    if (event.usage != null) {
      facts.push(judgment(t('logs.field.usage'), esc(stringify(event.usage))));
    }
    return `<div class="lg-question">
      ${judgment(label(labels, event.node_id), line)}
      ${bars}
      ${facts.length ? `<details class="lg-sub"><summary>${esc(t('logs.field.runDetail'))}</summary>
        <div class="lg-judgments">${facts.join('')}</div></details>` : ''}
    </div>`;
  }

  function activateHtml(event, labels) {
    return judgment(label(labels, event.node_id),
      `<small>${esc(t('logs.field.activated'))} · via ${esc(text(event.via_port))}</small>`);
  }

  /* 激活引用键：wave:nodeId:port —— activate 通过 node_id + via_port 引用槽位。 */
  function slotKey(wave, nodeId, port) {
    return String(wave) + ':' + String(nodeId) + ':' + String(port);
  }

  function slotHtml(slot, labels) {
    const condition = slot.condition == null ? '' : ' ' + conditionText(slot.condition);
    return `<li class="lg-slot-row" data-status="${esc(slotStatus(slot.status))}">
      <b>${esc(label(labels, slot.source))}.${esc(text(slot.source_port))}</b>
      <span>${esc(text(slot.raw))}${esc(condition)} → ${esc(statusText(slot.status))}</span>
      <small>${esc(label(labels, slot.node_id))}.${esc(text(slot.port))} · ${esc(slot.edge_id || '')}</small>
    </li>`;
  }

  /* 默认槽位集合 = invalid 或被同 wave activate 引用；完整列表折叠在 details 里。 */
  function slotGroupHtml(phase, labels) {
    const slots = phase.slots;
    if (!slots.length) return '';
    const counts = { true: 0, false: 0, unknown: 0, invalid: 0 };
    slots.forEach(event => { counts[slotStatus(event.status)] += 1; });
    const referenced = new Set();
    phase.activations.forEach(event => {
      if (event && event.via_port != null) {
        referenced.add(slotKey(phase.wave, event.node_id, event.via_port));
      }
    });
    const defaults = slots.filter(event =>
      event.status === 'invalid' || referenced.has(slotKey(phase.wave, event.node_id, event.port)));
    const countLine = [
      `${esc(t('logs.slot.hit'))} ${counts.true}`,
      `${esc(t('logs.slot.miss'))} ${counts.false}`,
      `${esc(t('logs.slot.unknown'))} ${counts.unknown}`,
      `${esc(t('logs.slot.invalid'))} ${counts.invalid}`,
    ].join(' · ');
    return `<div class="lg-slots">
      ${judgment(t('logs.field.slot'), countLine)}
      ${defaults.length ? `<ul class="lg-slot-defaults">${defaults.map(slot =>
        slotHtml(slot, labels)).join('')}</ul>` : ''}
      <details class="lg-slot-all"><summary>${esc(t('logs.slot.all'))}</summary>
        <ul>${slots.map(slot => slotHtml(slot, labels)).join('')}</ul>
      </details>
    </div>`;
  }

  /* ── 阶段（wave 0 = 初始计算，其余为第 n 轮） ──────────────────── */
  function phaseHtml(phase, labels) {
    const title = phase.wave === 0 ? esc(t('logs.initial')) : esc(tf('logs.wave', { n: phase.wave }));
    const failed = !!(phase.waveEvent && phase.waveEvent.failed);
    const error = failed && phase.waveEvent.error ? esc(stringify(phase.waveEvent.error)) : '';
    const items = [
      phase.conditions.map(event => conditionHtml(event, labels)).join(''),
      phase.gates.map(event => gateHtml(event, labels)).join(''),
      phase.questions.map(event => questionHtml(event, labels)).join(''),
      phase.activations.map(event => activateHtml(event, labels)).join(''),
      slotGroupHtml(phase, labels),
    ].filter(Boolean).join('');
    return `<section class="lg-phase${failed ? ' bad' : ''}" data-wave="${esc(phase.wave)}">
      <div class="lg-phase-h"><b>${title}</b>
        ${failed ? `<span class="lg-wave-fail">${esc(t('logs.failedWave'))}${error ? ' · ' + error : ''}</span>` : ''}
      </div>
      <div class="lg-judgments">${items}</div>
    </section>`;
  }

  /* ── 候选与最终选择：按 candidates 原顺序渲染 ──────────────────── */
  function finalHtml(model) {
    const final = model.final;
    if (!final) return '';
    const candidates = Array.isArray(final.candidates) ? final.candidates : [];
    const rows = candidates.map(candidate =>
      `<tr class="lg-candidate-row${candidate.target_id === final.target_id ? ' win' : ''}">
        <td>${nodeRef(model.labels, candidate.target_id)}</td>
        <td>p${esc(text(candidate.priority))}</td>
        <td>${esc(label(model.labels, candidate.via_node))}</td>
        <td>${esc(text(candidate.via_port))}</td>
        <td>${esc(tf('logs.wave', { n: text(candidate.wave) }))}</td>
        <td>${candidate.target_id === final.target_id ? esc(t('logs.finalSelected')) : ''}</td>
      </tr>`).join('');
    const note = candidates.length ? '' : final.kind === 'error_fallback'
      ? `<div class="lg-note">${esc(t('logs.errorFallback'))}</div>`
      : final.kind === 'otherwise' ? `<div class="lg-note">${esc(t('logs.noCandidates'))}</div>` : '';
    const fallback = final.kind === 'error_fallback' && model.fallback
      ? judgment(t('logs.field.failed'), esc(text(model.fallback.reason))) : '';
    return `<section class="lg-final"><div class="lg-sec-h">${esc(t('logs.candidates'))}</div>
      <div class="lg-judgments">
        ${judgment(t('logs.field.target'), nodeRef(model.labels, final.target_id))}
        ${fallback}
      </div>
      ${candidates.length ? `<table class="lg-candidate-table">
        <thead><tr>
          <th>${esc(t('logs.field.target'))}</th><th>${esc(t('logs.field.priority'))}</th>
          <th>${esc(t('logs.field.via'))}</th><th>${esc(t('logs.field.slot'))}</th>
          <th>${esc(t('logs.field.rounds'))}</th><th>${esc(t('logs.finalSelected'))}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>` : note}
    </section>`;
  }

  /* ── 模型调度：完整快照，或旧记录的 Sticky/模型降级 ────────────── */
  function scheduleHtml(model) {
    const schedule = model.schedule;
    if (!schedule) {
      /* routing_error 时调度从未发生；只有旧记录降级才显示 noSchedule。 */
      if (model.error) return '';
      const sticky = model.stickyHit;
      const scheduled = model.scheduled;
      if (!sticky && !scheduled) return '';
      const rows = [
        scheduled ? judgment(t('logs.field.model'), esc(text(scheduled.model))) : '',
        sticky ? judgment(t('logs.f.sticky'), esc(text(sticky.model))) : '',
      ].filter(Boolean).join('');
      return `<section class="lg-schedule"><div class="lg-sec-h">${esc(t('logs.schedule'))}</div>
        <div class="lg-judgments">${rows}</div>
        <div class="lg-note">${esc(t('logs.noSchedule'))}</div>
      </section>`;
    }
    const unavailable = schedule.unavailable && typeof schedule.unavailable === 'object'
      ? Object.entries(schedule.unavailable).map(([name, reason]) =>
        judgment(t('logs.f.unavailable'), esc(text(name)) + ' · ' + esc(unavailableText(String(reason))))
      ).join('') : '';
    const inflight = schedule.inflight && typeof schedule.inflight === 'object'
      ? Object.entries(schedule.inflight).map(([name, count]) => text(name) + ': ' + text(count)).join(' · ')
      : DASH;
    const sticky = schedule.sticky || {};
    const stickyLine = stickyText(sticky.status) +
      (sticky.preferred_model ? ' → ' + text(sticky.preferred_model) : '');
    return `<section class="lg-schedule"><div class="lg-sec-h">${esc(t('logs.schedule'))}</div>
      <div class="lg-judgments">
        ${judgment(t('logs.field.target'), nodeRef(model.labels, schedule.target_id))}
        ${judgment(t('logs.f.strategy'), esc(text(schedule.strategy)))}
        ${judgment(t('logs.f.configured'), esc(listText(schedule.configured_models)))}
        ${judgment(t('logs.f.registered'), esc(listText(schedule.registered_models)))}
        ${judgment(t('logs.f.available'), esc(listText(schedule.available_models)))}
        ${unavailable}
        ${judgment(t('logs.f.inflight'), esc(inflight))}
        ${judgment(t('logs.f.sticky'), esc(stickyLine))}
        ${judgment(t('logs.field.model'),
          '→ ' + esc(text(model.scheduled && model.scheduled.model)))}
      </div>
    </section>`;
  }

  /* ── 路由失败：唯一的红色失败块；正常 unknown 不用红 ──────────── */
  function errorHtml(model) {
    const error = model.error;
    if (!error) return '';
    return `<section class="lg-fail"><div class="lg-sec-h">${esc(t('logs.out.routing'))}</div>
      <div class="lg-judgments">
        ${judgment(t('logs.f.errorStage'), esc(text(error.stage)))}
        ${judgment(t('logs.f.errorClass'), esc(text(error.error_class)))}
        ${judgment(t('logs.f.errorMessage'), esc(text(error.message)))}
      </div>
    </section>`;
  }

  /* ── 纯视图模型：只读归组，不改事件对象、不碰 DOM（渲染交给调用方）。 */
  function build(trace, nodeLabels) {
    const model = {
      labels: nodeLabels && typeof nodeLabels === 'object' ? nodeLabels : {},
      input: { time: null, window: null },
      phases: [],
      fallback: null,
      final: null,
      schedule: null,
      stickyHit: null,
      scheduled: null,
      error: null,
    };
    let current = null;

    /* 按 wave 建唯一阶段；wave 0 收纳窗口期内的关键点。 */
    function ensurePhase(wave) {
      const number = Number.isFinite(Number(wave)) ? Number(wave) : 0;
      let phase = model.phases.find(item => item.wave === number);
      if (!phase) {
        phase = {
          wave: number,
          waveEvent: null,
          conditions: [],
          gates: [],
          questions: [],
          slots: [],
          activations: [],
        };
        model.phases.push(phase);
      }
      return phase;
    }

    (Array.isArray(trace) ? trace : []).forEach(event => {
      if (!event || typeof event !== 'object') return;
      switch (event.event) {
        case 'time': model.input.time = event; break;
        case 'window': model.input.window = event; break;
        case 'condition': current = ensurePhase(0); current.conditions.push(event); break;
        case 'gate': current = ensurePhase(event.wave); current.gates.push(event); break;
        case 'slot': current = ensurePhase(event.wave); current.slots.push(event); break;
        case 'activate': current = ensurePhase(event.wave); current.activations.push(event); break;
        case 'wave': current = ensurePhase(event.wave); current.waveEvent = event; break;
        case 'question': current = ensurePhase(event.wave); current.questions.push(event); break;
        case 'fallback': model.fallback = event; current = null; break;
        case 'final': model.final = event; current = null; break;
        case 'schedule': model.schedule = event; current = null; break;
        case 'sticky_hit': model.stickyHit = event; current = null; break;
        case 'scheduled': model.scheduled = event; current = null; break;
        case 'routing_error': model.error = event; current = null; break;
        default: break;
      }
    });
    return model;
  }

  function hasJev(record) {
    const trace = Array.isArray(record.trace) ? record.trace : [];
    return trace.some(event => event && (event.event === 'wave' || event.event === 'question')) ||
      record.jev_top != null || !!record.jev_model || Number(record.waves) > 0;
  }

  /* 分阶段渲染：输入 → 各轮 → 候选 → 调度 → 失败。 */
  function render(host, record) {
    const model = build(record.trace, record.node_labels || {});
    host.innerHTML = inputHtml(record, model) +
      model.phases.map(phase => phaseHtml(phase, model.labels)).join('') +
      finalHtml(model) + scheduleHtml(model) + errorHtml(model);
  }

  return {
    render,
    build,
    label,
    hasJev,
  };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = SRTrace;
