/* Policy v4 编辑器（Task 8 · brief Step 5 / 规格 §8）
   ------------------------------------------------------------------
   renderEditor(store, models, periods, hooks) 只编辑数据，不求解路由：
   问题（增删/类型切换/instructions/choice 选项增删排序/score 评分档）、
   目标（models + reasoning）、规则（增删/上下移动/target/name/条件树
   answer/all/any/not/text_contains_any/period_in）、策略设置
   （name/timezone/sticky/fallback）。
   · 所有 mutation 经 hooks.change → store.change（一次编辑 = 一个历史项）；
   · 渲染全部走 esc() 转义，绝不 innerHTML 注入原始用户值；
   · 前端只做必填与引用定位提示（软提示 + 422 issue 定位），最终校验以
     后端 422 为准；这里没有、也不允许有第二套规则求值器；
   · choice 选项超过 8 个只显示 optionCountHint 文案，不禁用保存。
   ------------------------------------------------------------------ */
import { t, tf } from '../i18n.js';
import { esc } from '../api.js';
import { optionCountHint } from './policy.js';

const EDIT_STATE = {
  bound: false,
  store: null,
  models: [],
  periods: [],
  change: null,
  toast: null,
};

const NODE_TYPES = [
  ['answer', 'pv.cond.answer'],
  ['all', 'pv.cond.all'],
  ['any', 'pv.cond.any'],
  ['not', 'pv.cond.not'],
  ['text_contains_any', 'pv.cond.text'],
  ['period_in', 'pv.cond.period'],
];
const NUMERIC_OPS = ['gt', 'gte', 'lt', 'lte', 'eq', 'neq'];

/* ── 小工具 ─────────────────────────────────────────────────────── */

function optionHtml(value, label, selected) {
  return '<option value="' + esc(value) + '"' + (selected ? ' selected' : '') + '>' +
    esc(label) + '</option>';
}

function selectHtml(field, cpath, options, ariaKey, issuePath) {
  return '<select data-cfield="' + esc(field) + '" data-cpath="' + esc(cpath) + '"' +
    ' aria-label="' + esc(t(ariaKey)) + '"' +
    ' data-issue-path="' + esc(issuePath || cpath) + '">' + options + '</select>';
}

function buttonHtml(act, label, attrs) {
  return '<button type="button" class="btn sm ghost pv-mini" data-act="' + esc(act) + '"' +
    (attrs || '') + ' title="' + esc(label) + '" aria-label="' + esc(label) + '">' +
    esc(label) + '</button>';
}

/* 简单 key（问题/目标/选项）：与后端 ID 规则同向的必填提示。 */
function validKey(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 64 &&
    !/\s/.test(value) && value.indexOf('.') < 0;
}

function parentOf(root, path) {
  const segments = String(path).split('.');
  const key = segments.pop();
  let cursor = root;
  for (const segment of segments) cursor = cursor[segment];
  return { parent: cursor, key: key };
}

function getByPath(root, path) {
  let cursor = root;
  for (const segment of String(path).split('.')) cursor = cursor[segment];
  return cursor;
}

function renameKey(object, from, to) {
  const next = {};
  Object.keys(object).forEach(key => { next[key === from ? to : key] = object[key]; });
  return next;
}

function nextMapId(object, prefix) {
  let index = 1;
  while (Object.prototype.hasOwnProperty.call(object, prefix + index)) index += 1;
  return prefix + index;
}

function firstModelId() {
  return EDIT_STATE.models.length ? EDIT_STATE.models[0].id : 'qwen3-8b';
}

/* ── 条件树 ─────────────────────────────────────────────────────── */

function condNodeType(node) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) return '';
  if (Array.isArray(node.all)) return 'all';
  if (Array.isArray(node.any)) return 'any';
  if (node.not !== undefined) return 'not';
  if (typeof node.answer === 'string') return 'answer';
  if (Array.isArray(node.text_contains_any)) return 'text_contains_any';
  if (Array.isArray(node.period_in)) return 'period_in';
  return '';
}

function defaultLeaf(policy) {
  const entries = Object.entries((policy && policy.questions) || {});
  const first = entries[0];
  if (first && first[1].type === 'choice') {
    const key = (first[1].options && first[1].options[0] && first[1].options[0].key) || '';
    return { answer: first[0] + '.selected', eq: key };
  }
  if (first && first[1].type === 'noul') return { answer: first[0] + '.yes', gte: 0.5 };
  if (first && first[1].type === 'score') return { answer: first[0] + '.value', gte: 5 };
  return { text_contains_any: [t('pv.cond.keyword')] };
}

function defaultNode(policy, type, periods) {
  if (type === 'all') return { all: [defaultLeaf(policy)] };
  if (type === 'any') return { any: [defaultLeaf(policy)] };
  if (type === 'not') return { not: defaultLeaf(policy) };
  if (type === 'text_contains_any') return { text_contains_any: [t('pv.cond.keyword')] };
  if (type === 'period_in') return { period_in: periods && periods.length ? [periods[0]] : [] };
  return defaultLeaf(policy);
}

function answerParts(node) {
  const answer = String(node && node.answer != null ? node.answer : '');
  const dot = answer.indexOf('.');
  const op = Object.keys(node || {}).find(key => key !== 'answer') || 'eq';
  return {
    questionId: dot >= 0 ? answer.slice(0, dot) : answer,
    output: dot >= 0 ? answer.slice(dot + 1) : '',
    op: op,
    value: node ? node[op] : '',
  };
}

function answerOutputs(question) {
  const type = question && question.type;
  if (type === 'choice') {
    const outs = [
      { value: 'selected', key: 'pv.out.selected' },
      { value: 'p_top', key: 'pv.out.p_top' },
      { value: 'margin', key: 'pv.out.margin' },
    ];
    (question.options || []).forEach(option =>
      outs.push({ value: 'prob.' + option.key, key: 'pv.out.prob' }));
    return outs;
  }
  if (type === 'noul') {
    return [{ value: 'yes', key: 'pv.out.yes' }, { value: 'no', key: 'pv.out.no' }];
  }
  if (type === 'score') return [{ value: 'value', key: 'pv.out.value' }];
  return [];
}

/* answer 条件的输出标签（prob.<key> 带上选项 label）。 */
function outputLabel(question, output) {
  if (output.indexOf('prob.') === 0 && question && Array.isArray(question.options)) {
    const key = output.slice('prob.'.length);
    const option = question.options.find(item => item && item.key === key);
    return tf('pv.out.prob') + ' ' + key + (option ? ' · ' + option.label : '');
  }
  return t('pv.out.' + output);
}

/* 由 questionId/output/op/value 重建一个 answer 条件节点（恰好两个键）。 */
function rebuildAnswer(questionId, output, op, value) {
  const node = { answer: questionId + '.' + output };
  node[op] = value;
  return node;
}

function defaultForOutput(question, output) {
  if (output === 'selected') {
    const key = question && Array.isArray(question.options) && question.options[0]
      ? question.options[0].key : '';
    return { op: 'eq', value: key };
  }
  return { op: 'gte', value: 0.5 };
}

/* 引用定位提示：answer 路径指向的问题不存在时在规则卡上提示。
   这是 map 存在性检查，不是规则求值器。 */
function collectAnswerPaths(node, out) {
  if (!node || typeof node !== 'object') return;
  if (typeof node.answer === 'string') { out.push(node.answer); return; }
  if (Array.isArray(node.all)) node.all.forEach(child => collectAnswerPaths(child, out));
  else if (Array.isArray(node.any)) node.any.forEach(child => collectAnswerPaths(child, out));
  else if (node.not !== undefined) collectAnswerPaths(node.not, out);
}

function unknownQuestionKeys(policy, when) {
  const answers = [];
  collectAnswerPaths(when, answers);
  const questions = (policy && policy.questions) || {};
  const unknown = [];
  answers.forEach(path => {
    const qid = String(path).split('.')[0];
    if (!questions[qid] && unknown.indexOf(qid) < 0) unknown.push(qid);
  });
  return unknown;
}

function renderCond(policy, node, path) {
  const type = condNodeType(node);
  const typeOptions = NODE_TYPES.map(entry =>
    optionHtml(entry[0], t(entry[1]), entry[0] === type)).join('');
  let body = '';

  if (type === 'answer') {
    const parts = answerParts(node);
    const questions = (policy && policy.questions) || {};
    const question = questions[parts.questionId];
    const questionOptions = Object.keys(questions).map(qid =>
      optionHtml(qid, qid + ' · ' + t('pv.q.type' +
        (questions[qid].type === 'choice' ? 'Choice' :
          questions[qid].type === 'noul' ? 'Noul' : 'Score')),
      qid === parts.questionId)).join('');
    /* 当前问题可能已被改名：保留原值让用户看见并修复（引用定位提示）。 */
    const questionSelect = (question ? '' :
      optionHtml(parts.questionId, parts.questionId + ' · ' + t('pv.r.unknownQuestionShort'), true)) +
      questionOptions;

    const outputs = answerOutputs(question);
    const outputOptions = outputs.map(out =>
      optionHtml(out.value, outputLabel(question, out.value), out.value === parts.output)).join('');
    const outputSelect = (outputs.some(out => out.value === parts.output) ? '' :
      optionHtml(parts.output, parts.output, true)) + outputOptions;

    const isSel = parts.output === 'selected';
    const ops = isSel ? ['eq'] : NUMERIC_OPS;
    const opOptions = ops.map(op =>
      optionHtml(op, t('pv.op.' + op), op === parts.op)).join('');

    let valueControl;
    if (isSel && question && Array.isArray(question.options) && question.options.length) {
      const keyOptions = question.options.map(option =>
        optionHtml(option.key, option.key + ' · ' + option.label,
          String(parts.value) === option.key)).join('');
      valueControl = selectHtml('value', path,
        (question.options.some(option => option.key === String(parts.value)) ? '' :
          optionHtml(String(parts.value), String(parts.value), true)) + keyOptions,
        'pv.cond.value', path + '.' + parts.op);
    } else {
      const numeric = typeof parts.value === 'number'
        ? parts.value
        : (String(parts.value) === '' ? 0 : Number(parts.value) || 0);
      valueControl = '<input type="number" step="any" data-cfield="value" data-cpath="' + esc(path) +
        '" value="' + esc(numeric) + '" aria-label="' + esc(t('pv.cond.value')) + '"' +
        ' data-issue-path="' + esc(path + '.' + parts.op) + '">';
    }

    body =
      '<div class="pv-answer-row">' +
      selectHtml('question', path, questionSelect, 'pv.cond.question') +
      selectHtml('output', path, outputSelect, 'pv.cond.output') +
      selectHtml('op', path, opOptions, 'pv.cond.op', path + '.' + parts.op) +
      valueControl +
      '</div>';
  } else if (type === 'all' || type === 'any') {
    const children = Array.isArray(node[type]) ? node[type] : [];
    body = children.map((child, index) =>
      '<div class="pv-cond-child">' +
      renderCond(policy, child, path + '.' + type + '.' + index) +
      buttonHtml('cond-del', t('pv.cond.childDel'),
        ' data-cact="child-del" data-cpath="' + esc(path + '.' + type + '.' + index) + '"') +
      '</div>').join('') +
      '<button type="button" class="btn sm dashed pv-cond-add" data-cact="child-add"' +
      ' data-cpath="' + esc(path) + '" data-ctype="' + esc(type) + '">' +
      esc(tf('pv.cond.addChild', { op: t('pv.cond.' + type) })) + '</button>';
  } else if (type === 'not') {
    body = '<div class="pv-cond-child">' +
      renderCond(policy, node.not, path + '.not') + '</div>';
  } else if (type === 'text_contains_any') {
    body = '<label class="pv-field"><span class="pv-lbl">' + esc(t('pv.cond.keywords')) + '</span>' +
      '<input type="text" data-cfield="keywords" data-cpath="' + esc(path) + '" value="' +
      esc((node.text_contains_any || []).join(', ')) + '" spellcheck="false"></label>';
  } else if (type === 'period_in') {
    const selected = node.period_in || [];
    body = '<div class="pv-periods" role="group" aria-label="' + esc(t('pv.cond.period')) + '">' +
      EDIT_STATE.periods.map(name =>
        '<label class="pv-check"><input type="checkbox" data-cfield="period" data-cpath="' + esc(path) +
        '" value="' + esc(name) + '"' + (selected.indexOf(name) >= 0 ? ' checked' : '') + '>' +
        esc(name) + '</label>').join('') +
      '</div>';
  }

  return '<div class="pv-cond is-' + esc(type || 'none') + '" data-cond="' + esc(path) +
    '" data-cond-type="' + esc(type) + '" data-issue-path="' + esc(path) + '">' +
    '<div class="pv-cond-h">' +
    selectHtml('type', path, typeOptions, 'pv.cond.type') +
    '</div>' +
    '<div class="pv-cond-b">' + body + '</div>' +
    '</div>';
}

/* ── 各分区渲染 ─────────────────────────────────────────────────── */

function sectionElement(id) {
  return document.getElementById(id);
}

function sectionBody(id) {
  const section = sectionElement(id);
  return section ? section.querySelector('[data-sec-body]') : null;
}

function applyCollapsed(store, id) {
  const section = sectionElement(id);
  if (!section) return;
  const collapsed = !!(store.layout && store.layout.collapsed && store.layout.collapsed[id]);
  section.classList.toggle('is-collapsed', collapsed);
  const toggle = section.querySelector('[data-act="sec-toggle"]');
  if (toggle) toggle.setAttribute('aria-expanded', String(!collapsed));
}

function renderQuestions(store) {
  const host = sectionBody('policyQuestions');
  if (!host) return;
  const policy = store.policy;
  if (!policy) { host.innerHTML = ''; return; }
  const questions = policy.questions || {};
  host.innerHTML = Object.keys(questions).map(qid => questionCard(qid, questions[qid])).join('') ||
    '<p class="pv-empty">' + esc(t('pv.q.empty')) + '</p>';
}

function questionCard(qid, question) {
  const type = question.type || 'choice';
  const instructions = question.instructions == null ? '' : String(question.instructions);
  let body = '';
  if (type === 'choice') {
    const options = Array.isArray(question.options) ? question.options : [];
    const rows = options.map((option, index) =>
      '<div class="pv-opt" data-opt="' + index + '" data-issue-path="' +
      esc('questions.' + qid + '.options.' + index) + '">' +
      '<span class="pv-opt-tools">' +
      buttonHtml('opt-up', t('pv.up'), ' data-opt="' + index + '"') +
      buttonHtml('opt-down', t('pv.down'), ' data-opt="' + index + '"') +
      '</span>' +
      '<input type="text" class="pv-key mono" data-field="opt-key" value="' + esc(option && option.key) +
      '" aria-label="' + esc(t('pv.q.optKey')) + '" spellcheck="false">' +
      '<input type="text" data-field="opt-label" value="' + esc(option && option.label) +
      '" aria-label="' + esc(t('pv.q.optLabel')) + '" spellcheck="false">' +
      '<input type="text" data-field="opt-capability" value="' + esc(option && option.capability) +
      '" aria-label="' + esc(t('pv.q.optCap')) + '" spellcheck="false">' +
      buttonHtml('opt-del', t('pv.del'), ' data-opt="' + index + '"') +
      '</div>').join('');
    const hint = optionCountHint(question);
    body = '<div class="pv-opts">' + rows +
      '<button type="button" class="btn sm dashed" data-act="opt-add">' +
      esc(t('pv.q.optAdd')) + '</button></div>' +
      (hint ? '<p class="pv-hint" role="note">' + esc(hint) + '</p>' : '');
  } else if (type === 'score') {
    const criteria = Array.isArray(question.criteria) ? question.criteria : [];
    body = '<label class="pv-field"><span class="pv-lbl">' + esc(t('pv.q.criteria')) + '</span>' +
      '<input type="text" data-field="criteria" value="' + esc(criteria.join(', ')) +
      '" spellcheck="false"></label>';
  }
  return '<article class="pv-card pv-q" data-qid="' + esc(qid) + '" data-issue-path="' +
    esc('questions.' + qid) + '">' +
    '<header class="pv-card-h">' +
    '<input type="text" class="pv-key mono" data-field="qid" value="' + esc(qid) +
    '" aria-label="' + esc(t('pv.q.key')) + '" spellcheck="false">' +
    '<select data-field="qtype" aria-label="' + esc(t('pv.q.type')) + '">' +
    optionHtml('choice', t('pv.q.typeChoice'), type === 'choice') +
    optionHtml('noul', t('pv.q.typeNoul'), type === 'noul') +
    optionHtml('score', t('pv.q.typeScore'), type === 'score') +
    '</select>' +
    buttonHtml('q-del', t('pv.del'), ' data-qid="' + esc(qid) + '"') +
    '</header>' +
    '<label class="pv-field' + (instructions ? '' : ' is-empty') + '"><span class="pv-lbl">' +
    esc(t('pv.q.instructions')) + '</span>' +
    '<textarea data-field="instructions" rows="2" data-issue-path="' +
    esc('questions.' + qid + '.instructions') + '" spellcheck="false">' + esc(instructions) + '</textarea></label>' +
    body +
    '</article>';
}

function reasoningEfforts(model) {
  const reasoning = model && (model.reasoning ||
    (model.profile && typeof model.profile === 'object' ? model.profile.reasoning : null));
  if (!reasoning || reasoning.declared === false || reasoning.supported === false) return null;
  const efforts = reasoning.efforts && typeof reasoning.efforts === 'object' ? reasoning.efforts : null;
  if (!efforts) return null;
  return Object.keys(efforts).map(key => [key, efforts[key]]);
}

function modelSelectOptions(models, current) {
  const ids = models.map(model => model.id);
  const extra = (current && ids.indexOf(current) < 0)
    ? optionHtml(current, current, true) : '';
  return extra + models.map(model =>
    optionHtml(model.id, model.id, model.id === current)).join('');
}

function renderTargets(store) {
  const host = sectionBody('policyTargets');
  if (!host) return;
  const policy = store.policy;
  if (!policy) { host.innerHTML = ''; return; }
  const targets = policy.targets || {};
  host.innerHTML = Object.keys(targets).map(tid => targetCard(tid, targets[tid])).join('') ||
    '<p class="pv-empty">' + esc(t('pv.t.empty')) + '</p>';
}

function targetCard(tid, target) {
  const models = Array.isArray(target.models) ? target.models : [];
  const rows = models.map((entry, index) => {
    const modelId = entry && entry.model ? entry.model : '';
    const model = EDIT_STATE.models.find(item => item.id === modelId);
    const efforts = reasoningEfforts(model);
    const current = entry && entry.reasoning != null ? String(entry.reasoning) : '';
    let reasoningControl;
    if (efforts) {
      const options = ['<option value="">' + esc(t('pv.t.reasoningNone')) + '</option>']
        .concat(efforts.map(effort =>
          optionHtml(String(effort[1]), effort[0], current === String(effort[1]))));
      if (current && !efforts.some(effort => String(effort[1]) === current)) {
        options.push(optionHtml(current, current, true));
      }
      reasoningControl = '<select data-field="m-reasoning" aria-label="' +
        esc(t('pv.t.reasoning')) + '">' + options.join('') + '</select>';
    } else {
      reasoningControl = '<span class="pv-mut">' + esc(t('pv.t.reasoningUndeclared')) + '</span>' +
        (current ? ' <code class="mono">' + esc(current) + '</code>' : '');
    }
    return '<div class="pv-model" data-mi="' + index + '" data-issue-path="' +
      esc('targets.' + tid + '.models.' + index) + '">' +
      '<select data-field="m-model" aria-label="' + esc(t('pv.t.model')) + '">' +
      modelSelectOptions(EDIT_STATE.models, modelId) + '</select>' +
      reasoningControl +
      buttonHtml('m-del', t('pv.t.modelRemove'), ' data-mi="' + index + '"') +
      '</div>';
  }).join('');
  return '<article class="pv-card pv-t" data-tid="' + esc(tid) + '" data-issue-path="' +
    esc('targets.' + tid) + '">' +
    '<header class="pv-card-h">' +
    '<input type="text" class="pv-key mono" data-field="tid" value="' + esc(tid) +
    '" aria-label="' + esc(t('pv.t.key')) + '" spellcheck="false">' +
    buttonHtml('t-del', t('pv.del'), ' data-tid="' + esc(tid) + '"') +
    '</header>' +
    '<div class="pv-models">' + rows +
    '<button type="button" class="btn sm dashed" data-act="m-add">' +
    esc(t('pv.t.modelAdd')) + '</button></div>' +
    '</article>';
}

function renderRules(store) {
  const host = sectionBody('policyRules');
  if (!host) return;
  const policy = store.policy;
  if (!policy) { host.innerHTML = ''; return; }
  const rules = Array.isArray(policy.rules) ? policy.rules : [];
  host.innerHTML = rules.map((rule, index) => ruleCard(policy, index, rule)).join('') ||
    '<p class="pv-empty">' + esc(t('pv.r.empty')) + '</p>';
}

function ruleCard(policy, index, rule) {
  const targetIds = Object.keys((policy && policy.targets) || {});
  const targetOptions = (targetIds.indexOf(rule.target) < 0
    ? optionHtml(rule.target, rule.target, true) : '') +
    targetIds.map(tid => optionHtml(tid, tid, tid === rule.target)).join('');
  const unknown = unknownQuestionKeys(policy, rule.when);
  return '<article class="pv-card pv-rule" data-ri="' + index + '" data-issue-path="' +
    esc('rules.' + index) + '">' +
    '<header class="pv-card-h">' +
    '<span class="pv-rule-idx mono" title="' + esc(t('pv.firstHitHint')) + '">' + (index + 1) + '</span>' +
    '<input type="text" data-field="r-name" value="' + esc(rule.name == null ? '' : rule.name) +
    '" placeholder="' + esc(t('pv.r.name')) + '" aria-label="' + esc(t('pv.r.name')) +
    '" spellcheck="false" data-issue-path="' + esc('rules.' + index + '.name') + '">' +
    '<select data-field="r-target" aria-label="' + esc(t('pv.r.target')) +
    '" data-issue-path="' + esc('rules.' + index + '.target') + '">' + targetOptions + '</select>' +
    '<span class="pv-rule-tools">' +
    buttonHtml('r-up', t('pv.up'), ' data-ri="' + index + '"') +
    buttonHtml('r-down', t('pv.down'), ' data-ri="' + index + '"') +
    buttonHtml('r-del', t('pv.del'), ' data-ri="' + index + '"') +
    '</span>' +
    '</header>' +
    '<div class="pv-cond-root">' +
    renderCond(policy, rule.when, 'rules.' + index + '.when') +
    '</div>' +
    (unknown.length
      ? '<p class="pv-ref-hint" role="note">' +
      esc(tf('pv.r.unknownQuestion', { keys: unknown.join('、') })) + '</p>'
      : '') +
    '</article>';
}

function renderSettings(store) {
  const host = sectionBody('policySettings');
  if (!host) return;
  const policy = store.policy;
  if (!policy) { host.innerHTML = ''; return; }
  const targetIds = Object.keys(policy.targets || {});
  const fallback = policy.fallback == null ? '' : policy.fallback;
  const sticky = policy.sticky || {};
  host.innerHTML =
    '<div class="pv-card">' +
    '<label class="pv-field' + (policy.name ? '' : ' is-empty') + '"><span class="pv-lbl">' +
    esc(t('pv.s.name')) + '</span>' +
    '<input type="text" data-field="s-name" value="' + esc(policy.name == null ? '' : policy.name) +
    '" spellcheck="false"></label>' +
    '<label class="pv-field"><span class="pv-lbl">' + esc(t('pv.s.timezone')) + '</span>' +
    '<input type="text" class="mono" data-field="s-timezone" value="' +
    esc(policy.timezone == null ? '' : policy.timezone) + '" spellcheck="false">' +
    '<span class="pv-mut">' + esc(t('pv.s.timezoneHint')) + '</span></label>' +
    '<label class="pv-field"><span class="pv-lbl">' + esc(t('pv.s.fallback')) + '</span>' +
    '<select data-field="s-fallback" data-issue-path="fallback">' +
    (targetIds.indexOf(fallback) < 0 ? optionHtml(fallback, fallback, true) : '') +
    targetIds.map(tid => optionHtml(tid, tid, tid === fallback)).join('') +
    '</select></label>' +
    '<div class="pv-field pv-sticky">' +
    '<label class="pv-check"><input type="checkbox" data-field="s-sticky-enabled"' +
    (sticky.enabled === true ? ' checked' : '') + '>' + esc(t('pv.s.sticky')) + '</label>' +
    '<label class="pv-inline"><span class="pv-lbl">' + esc(t('pv.s.stickyTtl')) + '</span>' +
    '<input type="number" min="1" step="1" class="mono" data-field="s-sticky-ttl" value="' +
    esc(sticky.ttl_seconds == null ? 1800 : sticky.ttl_seconds) +
    '" data-issue-path="sticky.ttl_seconds"></label>' +
    '</div>' +
    '</div>';
}

/* ── 编辑器是否正被键盘使用（保存回包后避免整页重绘丢焦点） ─────── */

export function editorEditing() {
  const active = document.activeElement;
  if (!active) return false;
  const editor = document.querySelector('.rtc-editor');
  if (!editor || !editor.contains(active)) return false;
  return /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || '');
}

/* ── 422 issue 定位：把服务端 path 落到编辑器里的具体控件 ────────── */

export function revealPath(path) {
  const target = path == null ? '' : String(path);
  if (!target) return false;
  const nodes = Array.prototype.slice.call(
    document.querySelectorAll('.rtc-editor [data-issue-path]'));
  let best = null;
  nodes.forEach(node => {
    const nodePath = node.dataset.issuePath;
    if (target === nodePath || target.indexOf(nodePath + '.') === 0) {
      if (!best || nodePath.length > best.dataset.issuePath.length) best = node;
    }
  });
  if (!best) return false;
  best.scrollIntoView({ block: 'center' });
  best.classList.add('is-issue-focus');
  setTimeout(() => {
    best.classList.remove('is-issue-focus');
    if (typeof best.focus === 'function' &&
      /^(INPUT|TEXTAREA|SELECT)$/.test(best.tagName || '')) best.focus();
  }, 900);
  return true;
}

/* ── 渲染入口（brief 接口：renderEditor(store, models, periods, hooks)） ── */

export function renderEditor(store, models, periods, hooks) {
  const options = hooks || {};
  EDIT_STATE.store = store;
  EDIT_STATE.models = Array.isArray(models) ? models : [];
  EDIT_STATE.periods = Array.isArray(periods) ? periods : [];
  EDIT_STATE.change = options.change ||
    ((kind, mutate) => store.change(kind, mutate));
  EDIT_STATE.toast = options.toast || (() => {});
  bindEditor();
  renderQuestions(store);
  renderTargets(store);
  renderRules(store);
  renderSettings(store);
  ['policyQuestions', 'policyTargets', 'policyRules', 'policySettings', 'policyTry']
    .forEach(id => applyCollapsed(store, id));
}

function bindEditor() {
  if (EDIT_STATE.bound) return;
  const editor = document.querySelector('.rtc-editor');
  if (!editor) return;
  EDIT_STATE.bound = true;
  editor.addEventListener('change', onEditorEvent);
  editor.addEventListener('click', onEditorEvent);
}

function onEditorEvent(event) {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (event.type === 'change' && (target.dataset.field || target.dataset.cfield)) {
    handleField(target);
    return;
  }
  if (event.type === 'click') {
    const action = target.closest ? target.closest('[data-act], [data-cact]') : null;
    if (action) handleAction(action);
  }
}

/* ── 字段提交（change = blur/Enter；一次提交 = 一个历史项） ─────── */

function warn(key, vars) {
  const text = vars ? tf(key, vars) : t(key);
  EDIT_STATE.toast(text, 'warn');
}

function handleField(input) {
  const store = EDIT_STATE.store;
  if (!store || !store.draft) return;
  if (input.dataset.cfield) {
    handleCondField(input);
    return;
  }
  const field = input.dataset.field;
  if (!field) return;
  const card = input.closest('[data-qid], [data-tid], [data-ri]');
  const policy = store.policy;

  /* 问题/目标改名：key 合法且不重复才提交；引用处靠提示与 422 定位修复。 */
  if (field === 'qid' || field === 'tid') {
    const isQuestion = field === 'qid';
    const current = card ? card.dataset[isQuestion ? 'qid' : 'tid'] : '';
    const next = input.value.trim();
    const map = isQuestion ? policy.questions : policy.targets;
    if (next === current) return;
    if (!validKey(next)) { input.value = current; warn('pv.errKey'); return; }
    if (Object.prototype.hasOwnProperty.call(map || {}, next)) {
      input.value = current;
      warn('pv.errKeyDup', { key: next });
      return;
    }
    EDIT_STATE.change('behavior', draft => {
      const targetMap = isQuestion ? draft.policy.questions : draft.policy.targets;
      targetMap[next] = targetMap[current];
      delete targetMap[current];
    });
    return;
  }

  if (card && card.dataset.qid) {
    const qid = card.dataset.qid;
    const question = (policy.questions || {})[qid];
    if (!question) return;
    if (field === 'instructions') {
      EDIT_STATE.change('behavior', draft => {
        draft.policy.questions[qid].instructions = input.value;
      });
      return;
    }
    if (field === 'qtype') {
      EDIT_STATE.change('behavior', draft => {
        const target = draft.policy.questions[qid];
        if (input.value === 'choice') {
          target.type = 'choice';
          if (!Array.isArray(target.options) || !target.options.length) {
            target.options = [
              { key: 'a', label: t('pv.q.newOptionA'), capability: t('pv.q.newOptionCap') },
              { key: 'b', label: t('pv.q.newOptionB'), capability: t('pv.q.newOptionCap') },
            ];
          }
          delete target.criteria;
        } else if (input.value === 'noul') {
          target.type = 'noul';
          delete target.options;
          delete target.criteria;
        } else if (input.value === 'score') {
          target.type = 'score';
          if (!Array.isArray(target.criteria) || target.criteria.length < 2) {
            target.criteria = [1, 5, 10];
          }
          delete target.options;
        }
      });
      return;
    }
    if (field === 'criteria') {
      const raw = String(input.value).split(/[,，]/).map(s => s.trim()).filter(Boolean);
      const numbers = raw.map(Number);
      if (raw.length < 2 || raw.length > 10 || numbers.some(n => !Number.isFinite(n))) {
        warn('pv.errCriteria');
        return;
      }
      EDIT_STATE.change('behavior', draft => {
        draft.policy.questions[qid].criteria = numbers;
      });
      return;
    }
    /* choice 选项字段：行内 data-opt 定位。 */
    const row = input.closest('[data-opt]');
    if (row && question.type === 'choice') {
      const index = Number(row.dataset.opt);
      const options = question.options || [];
      if (field === 'opt-key') {
        const next = input.value.trim();
        const current = options[index] && options[index].key;
        if (next === current) return;
        if (!validKey(next)) { input.value = current; warn('pv.errKey'); return; }
        if (options.some((option, at) => at !== index && option && option.key === next)) {
          input.value = current;
          warn('pv.errKeyDup', { key: next });
          return;
        }
        EDIT_STATE.change('behavior', draft => {
          draft.policy.questions[qid].options[index].key = next;
        });
        return;
      }
      if (field === 'opt-label' || field === 'opt-capability') {
        const prop = field === 'opt-label' ? 'label' : 'capability';
        EDIT_STATE.change('behavior', draft => {
          draft.policy.questions[qid].options[index][prop] = input.value;
        });
      }
    }
    return;
  }

  if (card && card.dataset.tid) {
    const tid = card.dataset.tid;
    const target = (policy.targets || {})[tid];
    if (!target) return;
    const row = input.closest('[data-mi]');
    const index = row ? Number(row.dataset.mi) : -1;
    if (field === 'm-model' && index >= 0) {
      EDIT_STATE.change('behavior', draft => {
        const entry = draft.policy.targets[tid].models[index];
        entry.model = input.value;
        const model = EDIT_STATE.models.find(item => item.id === input.value);
        if (!reasoningEfforts(model) && entry.reasoning != null) delete entry.reasoning;
      });
      return;
    }
    if (field === 'm-reasoning' && index >= 0) {
      EDIT_STATE.change('behavior', draft => {
        const entry = draft.policy.targets[tid].models[index];
        if (input.value === '') delete entry.reasoning;
        else entry.reasoning = input.value;
      });
    }
    return;
  }

  if (card && card.dataset.ri !== undefined) {
    const index = Number(card.dataset.ri);
    if (field === 'r-name') {
      EDIT_STATE.change('behavior', draft => {
        draft.policy.rules[index].name = input.value;
      });
      return;
    }
    if (field === 'r-target') {
      EDIT_STATE.change('behavior', draft => {
        draft.policy.rules[index].target = input.value;
      });
    }
    return;
  }

  /* 策略设置（无卡片上下文）。 */
  if (field === 's-name') {
    EDIT_STATE.change('behavior', draft => { draft.policy.name = input.value; });
    return;
  }
  if (field === 's-timezone') {
    EDIT_STATE.change('behavior', draft => { draft.policy.timezone = input.value.trim(); });
    return;
  }
  if (field === 's-fallback') {
    EDIT_STATE.change('behavior', draft => { draft.policy.fallback = input.value; });
    return;
  }
  if (field === 's-sticky-enabled') {
    EDIT_STATE.change('behavior', draft => {
      draft.policy.sticky = draft.policy.sticky || { enabled: false, ttl_seconds: 1800 };
      draft.policy.sticky.enabled = input.checked === true;
    });
    return;
  }
  if (field === 's-sticky-ttl') {
    const ttl = Number(input.value);
    if (!Number.isInteger(ttl) || ttl < 1) { warn('pv.errTtl'); return; }
    EDIT_STATE.change('behavior', draft => {
      draft.policy.sticky = draft.policy.sticky || { enabled: false, ttl_seconds: 1800 };
      draft.policy.sticky.ttl_seconds = ttl;
    });
  }
}

/* ── 条件树字段 ─────────────────────────────────────────────────── */

function handleCondField(input) {
  const store = EDIT_STATE.store;
  const path = input.dataset.cpath;
  const field = input.dataset.cfield;
  if (!store || !store.draft || !path) return;

  if (field === 'type') {
    EDIT_STATE.change('behavior', draft => {
      const { parent, key } = parentOf(draft.policy, path);
      parent[key] = defaultNode(draft.policy, input.value, EDIT_STATE.periods);
    });
    return;
  }
  if (field === 'question' || field === 'output' || field === 'op' || field === 'value') {
    EDIT_STATE.change('behavior', draft => {
      const node = getByPath(draft.policy, path);
      const parts = answerParts(node);
      const questions = draft.policy.questions || {};
      if (field === 'question') {
        const question = questions[input.value];
        const outputs = answerOutputs(question);
        const output = outputs.length ? outputs[0].value : parts.output;
        const seed = defaultForOutput(question, output);
        const { parent, key } = parentOf(draft.policy, path);
        parent[key] = rebuildAnswer(input.value, output, seed.op, seed.value);
        return;
      }
      if (field === 'output') {
        const question = questions[parts.questionId];
        const seed = defaultForOutput(question, input.value);
        const { parent, key } = parentOf(draft.policy, path);
        parent[key] = rebuildAnswer(parts.questionId, input.value, seed.op, seed.value);
        return;
      }
      if (field === 'op') {
        const nextOp = parts.output === 'selected' ? 'eq' : input.value;
        const { parent, key } = parentOf(draft.policy, path);
        parent[key] = rebuildAnswer(parts.questionId, parts.output, nextOp, parts.value);
        return;
      }
      /* value：selected → 选项 key（字符串）；数值输出 → 有限数值。 */
      if (parts.output === 'selected') {
        const { parent, key } = parentOf(draft.policy, path);
        parent[key] = rebuildAnswer(parts.questionId, parts.output, parts.op, input.value);
      } else {
        const numeric = Number(input.value);
        const { parent, key } = parentOf(draft.policy, path);
        parent[key] = rebuildAnswer(parts.questionId, parts.output, parts.op,
          Number.isFinite(numeric) ? numeric : 0);
      }
    });
    return;
  }
  if (field === 'keywords') {
    const keywords = String(input.value).split(/[,，]/).map(s => s.trim()).filter(Boolean);
    if (!keywords.length) { warn('pv.cond.errKeywords'); return; }
    EDIT_STATE.change('behavior', draft => {
      const node = getByPath(draft.policy, path);
      node.text_contains_any = keywords;
    });
    return;
  }
  if (field === 'period') {
    const host = input.closest('.pv-periods');
    if (!host) return;
    const names = Array.prototype.slice.call(host.querySelectorAll('input:checked'))
      .map(el => el.value);
    if (!names.length) { warn('pv.cond.errPeriods'); return; }
    EDIT_STATE.change('behavior', draft => {
      const node = getByPath(draft.policy, path);
      node.period_in = names;
    });
  }
}

/* ── 按钮动作 ───────────────────────────────────────────────────── */

function handleAction(button) {
  const store = EDIT_STATE.store;
  if (!store || !store.draft) return;

  /* 条件树子节点动作（data-cact 优先：child 按钮同时带 data-act 兜底）。 */
  if (button.dataset.cact) {
    const path = button.dataset.cpath;
    if (button.dataset.cact === 'child-add') {
      const type = button.dataset.ctype || 'all';
      EDIT_STATE.change('behavior', draft => {
        const node = getByPath(draft.policy, path);
        node[type].push(defaultLeaf(draft.policy));
      });
      return;
    }
    if (button.dataset.cact === 'child-del') {
      /* all/any 至少保留一个子条件：先拒绝，绝不推入空历史。 */
      const current = parentOf(store.policy, path);
      if (!Array.isArray(current.parent) || current.parent.length <= 1) {
        warn('pv.cond.errChild');
        return;
      }
      EDIT_STATE.change('behavior', draft => {
        const { parent, key } = parentOf(draft.policy, path);
        parent.splice(Number(key), 1);
      });
      return;
    }
    return;
  }

  const act = button.dataset.act;
  if (!act) return;
  const card = button.closest('[data-qid], [data-tid], [data-ri]');

  if (act === 'sec-toggle') {
    const sec = button.dataset.sec;
    if (!sec) return;
    EDIT_STATE.change('layout', draft => {
      draft.layout.collapsed = draft.layout.collapsed || {};
      draft.layout.collapsed[sec] = !draft.layout.collapsed[sec];
    });
    return;
  }

  if (act === 'q-add') {
    EDIT_STATE.change('behavior', draft => {
      const qid = nextMapId(draft.policy.questions, 'q');
      draft.policy.questions[qid] = {
        type: 'choice',
        instructions: t('pv.q.newInstructions'),
        options: [
          { key: 'a', label: t('pv.q.newOptionA'), capability: t('pv.q.newOptionCap') },
          { key: 'b', label: t('pv.q.newOptionB'), capability: t('pv.q.newOptionCap') },
        ],
      };
    });
    return;
  }
  if (act === 'q-del' && card) {
    const qid = card.dataset.qid;
    EDIT_STATE.change('behavior', draft => { delete draft.policy.questions[qid]; });
    return;
  }
  if (act === 'opt-add' && card) {
    const qid = card.dataset.qid;
    EDIT_STATE.change('behavior', draft => {
      const options = draft.policy.questions[qid].options || [];
      const index = options.length + 1;
      options.push({
        key: 'o' + index,
        label: t('pv.q.newOptionLabel') + ' ' + index,
        capability: t('pv.q.newOptionCap'),
      });
      draft.policy.questions[qid].options = options;
    });
    return;
  }
  if ((act === 'opt-up' || act === 'opt-down') && card) {
    const qid = card.dataset.qid;
    const index = Number(button.dataset.opt);
    const delta = act === 'opt-up' ? -1 : 1;
    const options = ((store.policy.questions || {})[qid] || {}).options || [];
    const at = index + delta;
    if (at < 0 || at >= options.length) return;
    EDIT_STATE.change('behavior', draft => {
      const list = draft.policy.questions[qid].options;
      const moved = list[index];
      list[index] = list[at];
      list[at] = moved;
    });
    return;
  }
  if (act === 'opt-del' && card) {
    const qid = card.dataset.qid;
    const index = Number(button.dataset.opt);
    EDIT_STATE.change('behavior', draft => {
      draft.policy.questions[qid].options.splice(index, 1);
    });
    return;
  }

  if (act === 't-add') {
    EDIT_STATE.change('behavior', draft => {
      const tid = nextMapId(draft.policy.targets, 't');
      draft.policy.targets[tid] = { models: [{ model: firstModelId() }] };
    });
    return;
  }
  if (act === 't-del' && card) {
    const tid = card.dataset.tid;
    EDIT_STATE.change('behavior', draft => { delete draft.policy.targets[tid]; });
    return;
  }
  if (act === 'm-add' && card) {
    const tid = card.dataset.tid;
    EDIT_STATE.change('behavior', draft => {
      const target = draft.policy.targets[tid];
      target.models = target.models || [];
      target.models.push({ model: firstModelId() });
    });
    return;
  }
  if (act === 'm-del' && card) {
    const tid = card.dataset.tid;
    const index = Number(button.dataset.mi);
    const models = (((store.policy.targets || {})[tid] || {}).models) || [];
    if (models.length <= 1) { warn('pv.t.errModels'); return; } /* 至少保留一个模型 */
    EDIT_STATE.change('behavior', draft => {
      draft.policy.targets[tid].models.splice(index, 1);
    });
    return;
  }

  if (act === 'r-add') {
    EDIT_STATE.change('behavior', draft => {
      const firstTarget = Object.keys(draft.policy.targets)[0] || '';
      draft.policy.rules.push({
        name: t('pv.r.newName'),
        when: defaultLeaf(draft.policy),
        target: firstTarget,
      });
    });
    return;
  }
  if ((act === 'r-up' || act === 'r-down') && card) {
    const index = Number(card.dataset.ri);
    const delta = act === 'r-up' ? -1 : 1;
    const rules = store.policy.rules || [];
    const at = index + delta;
    if (at < 0 || at >= rules.length) return;
    EDIT_STATE.change('behavior', draft => {
      const list = draft.policy.rules;
      const moved = list[index];
      list[index] = list[at];
      list[at] = moved;
    });
    return;
  }
  if (act === 'r-del' && card) {
    const index = Number(card.dataset.ri);
    if (!(store.policy.rules || [])[index]) return;
    EDIT_STATE.change('behavior', draft => { draft.policy.rules.splice(index, 1); });
  }
}
