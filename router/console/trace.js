/* Policy v4 trace 视图模型与共享区块视图（Task 9 · 设计 §5.5/§8）。
   ------------------------------------------------------------------
   build() 只消费后端原始事件列表（router/routing/policy/evaluator.py +
   scheduling.py 是唯一生产者）：不合成事件、不改名、不做第二次求值。
   事件集合固定为 time/window/jev/question/rule/final/schedule/sticky_hit/
   scheduled/routing_error/request_outcome；wave/slot/gate/activate 一律
   不识别（Policy v4 已删除这些概念）。

   一次决策至多一个 jev 事件（§5.1）：JEV 区块只取该事件；questions 保留
   答案详情（choice 概率/选中、noul yes/no、score value）；rules 按 trace
   顺序排列，直接显示后端 matched 三态（true 命中 / false 未命中 /
   null 未求值，null 绝不当 false 渲染），已求值规则内嵌逐条件结果列表
   （路径 + ✓/✗，全部来自后端 conditions 事件）；final 只按 kind + reason
   取文案，绝不读 priority/candidates。

   区块视图（jevHtml/questionsHtml/rulesHtml/finalHtml/…）被两处复用：
   路由页试算面板（routing/tryrun.js）与计算日志详情（logs.js）；文案与
   数据全部来自后端，前端只做展示。渲染统一 esc 名称与答案，概率条宽度
   限制在 0–100，耗时沿用 msText。
   ------------------------------------------------------------------ */
import { t, tf } from './i18n.js';
import { esc } from './api.js';

/* 单一非关键字段缺失统一显示 —，不丢问题、不丢规则行。 */
const DASH = '—';
const text = value => (value == null || value === '' ? DASH : String(value));

function stringify(value) {
  let out;
  if (typeof value === 'object' && value !== null) {
    try { out = JSON.stringify(value); } catch (error) { out = String(value); }
  } else {
    out = String(value);
  }
  return out.length > 120 ? out.slice(0, 117) + '…' : out;
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

function judgment(name, body) {
  return `<div class="lg-judgment"><b>${esc(name)}</b><span>${body}</span></div>`;
}

function listText(value) {
  return Array.isArray(value) && value.length ? value.map(item => text(item)).join(', ') : DASH;
}

/* 区块外壳：data-sec 供页面/QA 定位（jev/questions/rules/final/…）。 */
function section(key, title, body) {
  return `<section class="lg-block" data-sec="${esc(key)}">` +
    `<div class="lg-sec-h">${esc(title)}</div>${body}</section>`;
}

/* ── 纯视图模型：只读归组，不改事件对象、不碰 DOM ────────────────── */
function build(trace) {
  const model = {
    input: { time: null, window: null },
    jev: null,
    questions: [],
    rules: [],
    schedule: null,
    outcome: { stickyHit: null, scheduled: null, error: null, requestOutcome: null },
    final: null,
  };
  (Array.isArray(trace) ? trace : []).forEach(event => {
    if (!event || typeof event !== 'object') return;
    switch (event.event) {
      case 'time': model.input.time = event; break;
      case 'window': model.input.window = event; break;
      case 'jev': model.jev = event; break;
      case 'question': model.questions.push(event); break;
      case 'rule': model.rules.push(event); break;
      case 'final': model.final = event; break;
      case 'schedule': model.schedule = event; break;
      case 'sticky_hit': model.outcome.stickyHit = event; break;
      case 'scheduled': model.outcome.scheduled = event; break;
      case 'routing_error': model.outcome.error = event; break;
      case 'request_outcome': model.outcome.requestOutcome = event; break;
      default: break; /* 未知（含 wave 时代）事件原样跳过，不合成 */
    }
  });
  return model;
}

/* hasJev 只看 jev_calls 与 jev 事件，绝不查 wave（Task 9 裁决）。 */
function hasJev(record) {
  const source = record && typeof record === 'object' ? record : {};
  const model = build(Array.isArray(source.trace) ? source.trace : []);
  return Number(source.jev_calls) > 0 || model.jev != null;
}

/* ── 输入准备：time/window + 请求文本（文本只来自实时 ring/试算响应） ── */
function inputHtml(record, model) {
  const time = model.input.time || {};
  const win = model.input.window || {};
  const hasWindow = win.tokens_before != null || win.tokens_after != null;
  const prompt = String((record && record.text) || '');
  if (!model.input.time && !hasWindow && !prompt) return '';
  const periods = Array.isArray(time.active_periods) ? listText(time.active_periods) : DASH;
  const tokens = hasWindow
    ? `${esc(text(win.tokens_before))} → ${esc(text(win.tokens_after))}` +
      (win.truncated ? ` · ${esc(t('logs.f.truncated'))}` : '')
    : DASH;
  return section('input', t('logs.input'), `<div class="lg-judgments">
      ${judgment(t('logs.f.periods'), esc(periods))}
      ${judgment(t('logs.f.timezone'), esc(text(time.timezone)))}
      ${judgment(t('logs.f.window'), tokens)}
      ${prompt ? judgment(t('logs.requestText'), `<span class="lg-prompt-clamp">${esc(prompt)}</span>`) : ''}
    </div>` +
    (prompt ? `<details class="lg-sub"><summary>${esc(t('logs.fullText'))}</summary>
      <div class="lg-fulltext">${esc(prompt)}</div></details>` : ''));
}

/* ── 一次 JEV：成功一行摘要；失败给出错误文本（兜底由 final 呈现） ── */
function jevHtml(model) {
  const jev = model.jev;
  if (!jev) return '';
  const count = Array.isArray(jev.questions) ? jev.questions.length : 0;
  const head = jev.failed
    ? tf('pv.tryJevFailed', { err: text(jev.error) })
    : tf('pv.tryJev', { n: count, ms: msText(jev.duration_ms) });
  return section('jev', head, '');
}

/* ── 各问题答案：choice 概率条 + 选中/p_top/margin；noul yes/no；score value ── */
function questionsHtml(model) {
  if (!model.questions.length) return '';
  return section('questions', t('pv.tryAnswers'),
    `<div class="lg-judgments">${model.questions.map(questionHtml).join('')}</div>`);
}

function questionHtml(event) {
  const answer = event && event.answer && typeof event.answer === 'object' ? event.answer : null;
  const type = answer ? String(answer.type || '') : '';
  let line = DASH;
  if (type === 'choice') {
    line = `${esc(t('pv.out.selected'))} ${esc(text(answer.selected))} · ` +
      `${esc(t('pv.out.p_top'))} ${esc(pctText(answer.p_top))} · ` +
      `${esc(t('pv.out.margin'))} ${esc(pctText(answer.margin))}`;
  } else if (type === 'noul') {
    line = `${esc(t('pv.out.yes'))} ${esc(pctText(answer.yes))} · ` +
      `${esc(t('pv.out.no'))} ${esc(pctText(answer.no))}`;
  } else if (type === 'score') {
    line = `${esc(t('pv.out.value'))} ${esc(text(answer.value))}`;
  }
  return `<div class="lg-question">${judgment(text(event.question_id), line)}${probBars(answer)}</div>`;
}

/* 概率条：顺序沿用后端 ranking（缺失时按事件原键序），宽度限制 0–100。
   这里只排展示顺序，绝不依据答案重算任何命中结论。 */
function probBars(answer) {
  const probs = answer && answer.probabilities && typeof answer.probabilities === 'object'
    ? answer.probabilities : {};
  const keys = Object.keys(probs);
  if (!keys.length) return '';
  const order = Array.isArray(answer.ranking) && answer.ranking.length === keys.length
    ? answer.ranking.map(String) : keys;
  const selected = answer.selected != null ? String(answer.selected)
    : (Array.isArray(answer.ranking) && answer.ranking.length ? String(answer.ranking[0]) : '');
  const bars = order.map(key => {
    const width = Math.min(100, Math.max(0, Math.round(Number(probs[key]) * 100) || 0));
    return `<div class="lg-prob${key === selected ? ' sel' : ''}">
      <span class="k" title="${esc(key)}">${esc(key)}</span>
      <span class="track"><i style="width:${width}%"></i></span>
      <span class="v">${esc(pctText(probs[key]))}</span>
    </div>`;
  }).join('');
  return `<div class="lg-probs">${bars}</div>`;
}

/* ── 逐条规则：trace 顺序 + 后端 matched；首条命中即停（后续未求值） ── */
function rulesHtml(model) {
  if (!model.rules.length) return '';
  const rows = model.rules.map(ruleHtml).join('');
  /* 求值器命中即 break：末条规则命中，或存在被跳过的规则（matched=null），
     都说明求值没有跑完全部规则，明确提示首条命中后停止。 */
  const stopped = (model.rules[model.rules.length - 1].matched === true
    || model.rules.some(rule => rule.matched == null))
    ? `<div class="lg-note">${esc(t('pv.tryRulesStopped'))}</div>` : '';
  return section('rules', t('pv.tryRules'), rows + stopped);
}

/* matched 三态：true 命中 / false 未命中 / null 未求值（首条命中后跳过）。
   null 绝不能当作 false 渲染，也不得自行补算。 */
function ruleFlagKey(rule) {
  if (rule.matched === true) return 'pv.tryHit';
  if (rule.matched === false) return 'pv.tryMiss';
  return 'pv.trySkipped';
}

/* 已求值规则的逐条件紧凑列表：路径 + ✓/✗，全部来自后端 conditions 事件。 */
function conditionRows(rule) {
  const conditions = Array.isArray(rule.conditions) ? rule.conditions : [];
  if (!conditions.length) return '';
  const rows = conditions.map(condition => {
    const pass = condition.result === true;
    return `<li class="lg-cond${pass ? ' ok' : ' no'}">` +
      `<span class="mono">${esc(text(condition.path))}</span>` +
      `<span class="lg-cond-flag">${pass ? '✓' : '✗'}</span></li>`;
  }).join('');
  return `<ul class="lg-rule-conds">${rows}</ul>`;
}

function ruleHtml(rule) {
  const hit = rule.matched === true;
  const skipped = rule.matched == null;
  return `<div class="lg-rule ${hit ? 'is-hit' : skipped ? 'is-skip' : 'is-miss'}" data-rule-index="${esc(text(rule.index))}">
    <div class="lg-rule-line">
      <span class="mono">${esc(text(Number(rule.index) + 1))}</span>
      <span class="lg-rule-name">${esc(text(rule.name))}</span>
      <span class="lg-rule-flag">${esc(t(ruleFlagKey(rule)))}</span>
    </div>
    ${conditionRows(rule)}
  </div>`;
}

/* ── 最终目标：只按 kind + reason；命中规则名不在此区（由记录字段展示） ── */
function finalHtml(model) {
  const final = model.final;
  if (!final) return '';
  const fallback = final.kind === 'fallback';
  const pill = `<span class="pill ${fallback ? 'warn' : 'ok'}">${esc(t(fallback ? 'pv.tryFallback' : 'pv.tryTarget'))}</span>`;
  const reasonKey = final.reason === 'jev_error' || final.reason === 'no_rule_matched'
    ? 'pv.tryReason.' + final.reason : '';
  const reason = reasonKey ? judgment(t('pv.finalReason'), esc(t(reasonKey))) : '';
  return section('final', t('pv.tryFinal'), `<div class="lg-judgments">
      ${judgment(t('logs.field.target'), pill + ' <b class="mono">' + esc(text(final.target_id)) + '</b>')}
      ${reason}
    </div>`);
}

/* ── 模型调度快照：目标/策略/模型可用性/粘性（全部来自 schedule 事件） ── */
function scheduleHtml(model) {
  const schedule = model.schedule;
  if (!schedule) return '';
  const unavailable = schedule.unavailable && typeof schedule.unavailable === 'object'
    ? Object.entries(schedule.unavailable).map(([name, reason]) =>
      judgment(t('logs.f.unavailable'),
        esc(text(name)) + ' · ' + esc(unavailableText(reason)))
    ).join('') : '';
  const inflight = schedule.inflight && typeof schedule.inflight === 'object'
    ? Object.entries(schedule.inflight).map(([name, count]) => text(name) + ': ' + text(count)).join(' · ')
    : DASH;
  const sticky = schedule.sticky || {};
  const stickyLine = stickyText(sticky.status) +
    (sticky.preferred_model ? ' → ' + text(sticky.preferred_model) : '');
  return section('schedule', t('logs.schedule'), `<div class="lg-judgments">
      ${judgment(t('logs.field.target'), esc(text(schedule.target_id)))}
      ${judgment(t('logs.f.strategy'), esc(text(schedule.strategy)))}
      ${judgment(t('logs.f.configured'), esc(listText(schedule.configured_models)))}
      ${judgment(t('logs.f.registered'), esc(listText(schedule.registered_models)))}
      ${judgment(t('logs.f.available'), esc(listText(schedule.available_models)))}
      ${unavailable}
      ${judgment(t('logs.f.inflight'), esc(inflight))}
      ${judgment(t('logs.f.sticky'), esc(stickyLine))}
    </div>`);
}

/* sticky 状态按原值映射（disabled/no_session/not_checked/miss/hit/unavailable）；
   not_checked 是「未检查」，不能与 miss 混淆；未知值回退原样显示。 */
function stickyText(status) {
  const name = String(status == null || status === '' ? 'not_checked' : status);
  const label = t('logs.sticky.' + name);
  return label.indexOf('logs.sticky.') === 0 ? name : label;
}

/* 不可用原因按原值映射（unregistered / breaker_open）；未知值回退原样。 */
function unavailableText(reason) {
  const label = t('logs.unavailable.' + reason);
  return label.indexOf('logs.unavailable.') === 0 ? String(reason) : label;
}

/* ── 执行结果：粘性命中/调度模型/路由失败/最终 outcome ─────────────── */
function outcomeHtml(model) {
  const outcome = model.outcome;
  const rows = [];
  if (outcome.scheduled) {
    rows.push(judgment(t('logs.field.model'),
      '→ ' + esc(text(outcome.scheduled.model)) +
      (outcome.scheduled.dry_run === true ? ' · ' + esc(t('logs.dryRun')) : '')));
  }
  if (outcome.stickyHit) {
    rows.push(judgment(t('logs.f.sticky'), esc(text(outcome.stickyHit.model))));
  }
  if (outcome.requestOutcome) {
    rows.push(judgment(t('logs.outcome'), esc(outcomeText(outcome.requestOutcome.outcome_state))));
  }
  const head = rows.length ? `<div class="lg-judgments">${rows.join('')}</div>` : '';
  const error = outcome.error ? `<div class="lg-fail"><div class="lg-sec-h">${esc(t('logs.out.routing'))}</div>
      <div class="lg-judgments">
        ${judgment(t('logs.f.errorStage'), esc(text(outcome.error.stage)))}
        ${judgment(t('logs.f.errorClass'), esc(text(outcome.error.error_class)))}
        ${judgment(t('logs.f.errorMessage'), esc(stringify(outcome.error.message)))}
      </div></div>` : '';
  if (!head && !error) return '';
  return section('outcome', t('logs.outcome'), head + error);
}

function outcomeText(state) {
  const label = t('logs.out.' + state);
  return label.indexOf('logs.out.') === 0 ? text(state) : label;
}

/* ── 组合视图：计算日志详情（输入 → JEV → 答案 → 规则 → 最终 → 调度 → 结果） ── */
function detailHtml(record) {
  const source = record && typeof record === 'object' ? record : {};
  const model = build(Array.isArray(source.trace) ? source.trace : []);
  const body = inputHtml(source, model) + jevHtml(model) + questionsHtml(model) +
    rulesHtml(model) + finalHtml(model) + scheduleHtml(model) + outcomeHtml(model);
  /* SQLite 恢复行没有 trace：统一显示空态，绝不编造过程。 */
  return body || `<div class="lg-none">${esc(t('logs.traceEmpty'))}</div>`;
}

export {
  build, hasJev, detailHtml,
  inputHtml, jevHtml, questionsHtml, rulesHtml, finalHtml, scheduleHtml, outcomeHtml,
};
