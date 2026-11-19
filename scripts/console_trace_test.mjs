/* Policy v4 试算/计算日志共享 trace 视图模型测试（Task 9 · brief Step 1）。
 *
 * 运行：node --test scripts/console_trace_test.mjs
 * 约束：不加载 DOM、不发请求；build() 只消费后端原始事件列表
 * （router/routing/policy/evaluator.py 是唯一生产者），不合成事件、
 * 不改名、不做第二次求值。事件集合固定为
 * time/window/jev/question/rule/final/schedule/sticky_hit/scheduled/
 * routing_error/request_outcome；slot/gate/activate/wave 一律不识别。
 *
 * 首个用例是 brief 的逐字测试；其余覆盖：jev_error 与 no_rule_matched
 * 的不同文案、无 JEV 的静态策略、choice 概率条、noul yes/no、score value、
 * 规则未命中与首条命中后跳过的状态、调度失败保留前置 trace、hasJev
 * 只看 jev_calls 与 jev 事件（绝不看 wave）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

/* 旧模块（wave 时代）没有这些导出：命名空间导入 + 解构让缺失导出变成
   undefined，测试按断言逐条失败，而不是整个文件在 import 处爆炸。 */
import * as SRTrace from '../router/console/trace.js';
const {
  build, hasJev, detailHtml, jevHtml, questionsHtml, rulesHtml, finalHtml,
} = SRTrace;

/* answer 轨迹形状来自 evaluator._question_event + jev/serialization.answer_to_dict：
 * choice {type,selected,probabilities,ranking,p_top,margin}
 * noul  {type,yes,no}；score {type,value}。 */
const CHOICE_EVENT = {
  event: 'question', question_id: 'route',
  answer: {
    type: 'choice', selected: 'power',
    probabilities: { power: 0.84, strong: 0.12, fast: 0.04 },
    ranking: ['power', 'strong', 'fast'], p_top: 0.84, margin: 0.72,
  },
};
const NOUL_EVENT = {
  event: 'question', question_id: 'power_business',
  answer: { type: 'noul', yes: 0.92, no: 0.08 },
};
const SCORE_EVENT = {
  event: 'question', question_id: 'complexity',
  answer: { type: 'score', value: 5 },
};

test('build renders one JEV and ordered condition results without waves', () => {
  const model = build([
    { event: 'window', tokens_before: 10, tokens_after: 10, truncated: false },
    { event: 'jev', questions: ['route', 'risk'], duration_ms: 12, failed: false, error: null },
    { event: 'question', question_id: 'risk', answer: { yes: 0.9, no: 0.1 } },
    { event: 'rule', index: 0, name: '高风险', matched: true, conditions: [
      { path: 'when', kind: 'answer', answer: 'risk.yes', op: 'gte',
        expected: 0.8, actual: 0.9, result: true }
    ] },
    { event: 'rule', index: 1, name: '不执行', matched: null, conditions: [] },
    { event: 'final', kind: 'target', target_id: 'safe', reason: null },
    { event: 'scheduled', model: 'fast', reason: 'target', dry_run: true }
  ]);
  assert.equal(model.jev.duration_ms, 12);
  assert.deepEqual(model.rules.map(rule => rule.name), ['高风险', '不执行']);
  assert.equal(model.rules[0].conditions[0].actual, 0.9);
  assert.equal(model.rules[1].matched, null);
  assert.equal(model.final.target_id, 'safe');
  const serialized = JSON.stringify(model);
  assert.equal(serialized.includes('wave'), false);
  assert.equal(serialized.includes('ports'), false);
});

test('JEV block renders the duration with a single unit', () => {
  /* 模板不带单位，单位由 msText 统一给出：45 → 45ms，绝不出现 45msms。 */
  const withDuration = jevHtml(build([
    { event: 'jev', questions: ['route', 'risk'], duration_ms: 45, failed: false, error: null },
  ]));
  assert.ok(withDuration.includes('45ms'), 'the JEV duration is missing its unit');
  assert.equal(withDuration.includes('45msms'), false,
    'the JEV duration rendered a doubled unit');
  /* 长耗时沿用 msText 的秒级格式。 */
  const slow = jevHtml(build([
    { event: 'jev', questions: ['route'], duration_ms: 1200, failed: false, error: null },
  ]));
  assert.ok(slow.includes('1.20s'), 'long JEV durations must keep the msText seconds format');
  /* 缺失耗时干净地显示 —，不渲染 —ms。 */
  const missing = jevHtml(build([
    { event: 'jev', questions: ['route'], duration_ms: null, failed: false, error: null },
  ]));
  assert.ok(missing.includes('—'), 'a missing JEV duration must render the dash');
  assert.equal(missing.includes('—ms'), false, 'a missing JEV duration rendered a dangling unit');
});

test('build ignores slot/gate/activate/wave events entirely', () => {
  const model = build([
    { event: 'wave', wave: 1, questions: ['route'], duration_ms: 9, failed: false },
    { event: 'slot', wave: 1, node_id: 'risk', port: 'in_1', status: 'true' },
    { event: 'gate', wave: 1, node_id: 'logic', operator: 'and', value: true },
    { event: 'activate', wave: 1, node_id: 'power', via_port: 'out' },
    { event: 'condition', node_id: 'kw', value: true, matched: [] },
  ]);
  assert.equal(model.jev, null);
  assert.deepEqual(model.questions, []);
  assert.deepEqual(model.rules, []);
  assert.equal(model.final, null);
  assert.equal(model.schedule, null);
  /* 不合成、不改名：未知事件不进入视图模型的任何区间。 */
  assert.equal(JSON.stringify(model).includes('slot'), false);
  assert.equal(JSON.stringify(model).includes('activate'), false);
});

test('final copy distinguishes jev_error from no_rule_matched', () => {
  const withFinal = reason => build([
    { event: 'jev', questions: ['route'], duration_ms: 3, failed: reason === 'jev_error', error: null },
    { event: 'final', kind: 'fallback', target_id: 'safe', reason: reason },
  ]);
  const jevError = finalHtml(withFinal('jev_error'));
  const noRule = finalHtml(withFinal('no_rule_matched'));
  assert.ok(jevError.includes('决策服务失败'), 'jev_error copy missing');
  assert.equal(jevError.includes('没有规则命中'), false,
    'jev_error must not show the no_rule_matched copy');
  assert.ok(noRule.includes('没有规则命中'), 'no_rule_matched copy missing');
  assert.equal(noRule.includes('决策服务失败'), false,
    'no_rule_matched must not show the jev_error copy');
  /* final 只按 kind + reason 取文案，priority/candidates 是 wave 时代字段。 */
  const legacy = finalHtml(build([
    { event: 'final', kind: 'target', target_id: 'power', reason: null,
      priority: 9, candidates: [{ target_id: 'power', priority: 9, wave: 2 }] },
  ]));
  assert.equal(legacy.includes('候选'), false, 'final view still renders candidates');
  assert.equal(legacy.includes('p9'), false, 'final view still renders priority');
  assert.equal(legacy.includes('wave'), false, 'final view still renders wave data');
});

test('a static policy without questions renders no JEV block', () => {
  const model = build([
    { event: 'time', active_periods: ['normal'], timezone: 'Asia/Shanghai' },
    { event: 'window', tokens_before: 8, tokens_after: 8, truncated: false },
    { event: 'rule', index: 0, name: '工作时段文本命中', matched: true },
    { event: 'final', kind: 'target', target_id: 'strong', reason: null },
  ]);
  assert.equal(model.jev, null);
  assert.deepEqual(model.questions, []);
  assert.equal(model.rules.length, 1);
  assert.equal(jevHtml(model), '', 'a no-JEV trace must not render a JEV block');
  assert.equal(questionsHtml(model), '', 'a no-JEV trace has no question answers either');
  assert.ok(rulesHtml(model).includes('工作时段文本命中'),
    'static rules still render without a JEV block');
});

test('choice answers render selected, p_top, margin and clamped probability bars', () => {
  const model = build([{ event: 'jev', questions: ['route'], duration_ms: 40, failed: false, error: null }, CHOICE_EVENT]);
  const html = questionsHtml(model);
  assert.ok(html.includes('route'), 'question id missing');
  assert.ok(html.includes('选中项'), 'selected label missing');
  assert.ok(html.includes('power'), 'selected value missing');
  assert.ok(html.includes('最高概率'), 'p_top label missing');
  assert.ok(html.includes('84%'), 'p_top value missing');
  assert.ok(html.includes('领先幅度'), 'margin label missing');
  assert.ok(html.includes('72%'), 'margin value missing');
  /* 概率条按后端 ranking 排序，宽度限制在 0–100。 */
  const widths = [...html.matchAll(/width:(\d+)%/g)].map(match => Number(match[1]));
  assert.deepEqual(widths, [84, 12, 4]);
  assert.ok(html.indexOf('power') < html.indexOf('strong'), 'bars are not in ranking order');

  const garbage = build([{
    event: 'question', question_id: 'route',
    answer: { type: 'choice', selected: 'a', probabilities: { a: 1.4, b: -0.4 },
      ranking: ['a', 'b'], p_top: 1.4, margin: 1.8 },
  }]);
  const clamped = questionsHtml(garbage);
  assert.ok(clamped.includes('width:100%'), 'a probability above 1 was not clamped to 100');
  assert.ok(clamped.includes('width:0%'), 'a negative probability was not clamped to 0');
  assert.equal(clamped.includes('width:140%'), false);
  assert.equal(clamped.includes('width:-40%'), false);
});

test('noul answers render yes and no probabilities', () => {
  const html = questionsHtml(build([NOUL_EVENT]));
  assert.ok(html.includes('P(yes)'), 'P(yes) label missing');
  assert.ok(html.includes('92%'), 'yes value missing');
  assert.ok(html.includes('P(no)'), 'P(no) label missing');
  assert.ok(html.includes('8%'), 'no value missing');
});

test('score answers render the numeric value', () => {
  const html = questionsHtml(build([SCORE_EVENT]));
  assert.ok(html.includes('complexity'), 'question id missing');
  assert.ok(html.includes('数值'), 'value label missing');
  assert.ok(html.includes('5'), 'value missing');
});

test('rules view marks misses, keeps trace order and notes the first-match stop', () => {
  const model = build([
    { event: 'jev', questions: ['route'], duration_ms: 40, failed: false, error: null },
    { event: 'rule', index: 0, name: '对抗请求优先走安全目标', matched: false },
    { event: 'rule', index: 1, name: '电力业务优先', matched: true },
    { event: 'final', kind: 'target', target_id: 'power', reason: null },
  ]);
  const html = rulesHtml(model);
  /* 规则按 trace index 排序（1 起显示），命中状态直接来自后端 matched。 */
  assert.ok(html.indexOf('对抗请求优先走安全目标') < html.indexOf('电力业务优先'),
    'rules are not in backend trace order');
  assert.ok(html.includes('未命中'), 'the missed rule is not marked');
  assert.ok(html.includes('命中'), 'the matched rule is not marked');
  assert.ok(html.includes('首条命中后停止'), 'the stop-at-first-match note is missing');

  /* 全部未命中：没有停止备注（首条命中才停止）。 */
  const allMiss = rulesHtml(build([
    { event: 'rule', index: 0, name: '规则一', matched: false },
    { event: 'rule', index: 1, name: '规则二', matched: false },
  ]));
  assert.equal(allMiss.includes('首条命中后停止'), false,
    'a no-match trace must not claim the run stopped at a match');
  assert.equal((allMiss.match(/lg-rule /g) || []).length, 2);
});

test('rule cards render condition paths with pass/fail marks', () => {
  const model = build([
    { event: 'rule', index: 1, name: '复杂请求', matched: false, conditions: [
      { path: 'when.all.0', kind: 'answer', answer: 'route.selected', op: 'eq',
        expected: 'strong', actual: 'fast', result: false },
      { path: 'when.all.1', kind: 'answer', answer: 'severity.value', op: 'gte',
        expected: 5, actual: 10, result: true },
      { path: 'when.all', kind: 'all', result: false },
    ] },
  ]);
  const html = rulesHtml(model);
  /* 紧凑嵌套列表：每个已求值条件一行，路径 + ✓/✗。 */
  const rows = [...html.matchAll(/<li class="lg-cond[^"]*">\s*<span class="mono">([^<]*)<\/span>/g)]
    .map(match => match[1]);
  assert.deepEqual(rows, ['when.all.0', 'when.all.1', 'when.all'],
    'condition rows must keep the backend evaluation order (leaves first, gate last)');
  assert.ok(/✓/.test(html), 'the passing condition is not marked with ✓');
  assert.ok(/✗/.test(html), 'the failing condition is not marked with ✗');

  /* 未求值的规则（matched=null + conditions=[]）不渲染条件列表。 */
  const skipped = rulesHtml(build([
    { event: 'rule', index: 2, name: '主问题选择轻量模型', matched: null, conditions: [] },
  ]));
  assert.equal((skipped.match(/lg-cond\b/g) || []).length, 0,
    'a skipped rule must not render condition rows');
});

test('a skipped rule renders 未求值 and never 未命中', () => {
  /* 单条被跳过的规则（matched=null，首条命中后未求值）。 */
  const skipped = rulesHtml(build([
    { event: 'rule', index: 1, name: '复杂请求', matched: null, conditions: [] },
  ]));
  assert.ok(skipped.includes('未求值'), 'a skipped rule must show 未求值');
  assert.equal(skipped.includes('未命中'), false,
    'a skipped rule rendered the 未命中 copy');

  /* 单条未命中的规则：文案仍是未命中，不串到未求值。 */
  const missed = rulesHtml(build([
    { event: 'rule', index: 0, name: '高风险', matched: false, conditions: [
      { path: 'when', kind: 'answer', answer: 'risk.yes', op: 'gte',
        expected: 0.8, actual: 0.1, result: false },
    ] },
  ]));
  assert.ok(missed.includes('未命中'), 'a missed rule must show 未命中');
  assert.equal(missed.includes('未求值'), false,
    'a missed rule rendered the skipped copy');

  /* 命中 + 后续跳过：停止备注仍然出现（首条命中后停止）。 */
  const stopped = rulesHtml(build([
    { event: 'rule', index: 0, name: '高风险', matched: true, conditions: [
      { path: 'when', kind: 'answer', answer: 'risk.yes', op: 'gte',
        expected: 0.8, actual: 0.9, result: true },
    ] },
    { event: 'rule', index: 1, name: '复杂请求', matched: null, conditions: [] },
  ]));
  assert.ok(stopped.includes('首条命中后停止'),
    'the stop note is missing when later rules were skipped');
  assert.ok(stopped.includes('未求值'), 'the skipped tail must show 未求值');
});

test('a scheduling failure keeps the preceding trace in the view model', () => {
  const trace = [
    { event: 'time', active_periods: ['normal'], timezone: 'Asia/Shanghai' },
    { event: 'window', tokens_before: 10, tokens_after: 10, truncated: false },
    { event: 'jev', questions: ['route', 'power_business', 'adversarial'], duration_ms: 45, failed: false, error: null },
    CHOICE_EVENT,
    { event: 'rule', index: 0, name: '对抗请求优先走安全目标', matched: false },
    { event: 'rule', index: 1, name: '电力业务优先', matched: true },
    { event: 'final', kind: 'target', target_id: 'power', reason: null },
    {
      event: 'schedule', target_id: 'power', strategy: 'least_inflight',
      configured_models: ['qwen3.8-flash-sft'], registered_models: ['qwen3.8-flash-sft'],
      available_models: [], unavailable: { 'qwen3.8-flash-sft': 'unregistered' },
      inflight: {}, sticky: { status: 'disabled', preferred_model: null },
    },
    { event: 'routing_error', stage: 'schedule', error_class: 'no_available_backend', message: "target 'power' has no available backend" },
    {
      event: 'request_outcome', phase: 'outcome', outcome_state: 'routing_error',
      cell: { zh: '路由失败', en: 'Routing failed' }, tone: 'bad',
    },
  ];
  const model = build(trace);
  /* 前置 trace 全部保留：调度失败不清空 JEV/问题/规则/最终目标。 */
  assert.equal(model.jev.failed, false);
  assert.equal(model.questions.length, 1);
  assert.equal(model.rules.length, 2);
  assert.equal(model.final.target_id, 'power');
  assert.equal(model.schedule.strategy, 'least_inflight');
  assert.equal(model.outcome.error.error_class, 'no_available_backend');
  assert.equal(model.outcome.requestOutcome.outcome_state, 'routing_error');
  assert.equal(model.outcome.scheduled, null);

  const html = detailHtml({ text: '调度失败', trace });
  assert.ok(html.includes('电力业务优先'), 'the detail dropped the rules on a schedule failure');
  assert.ok(html.includes('power'), 'the detail dropped the final target on a schedule failure');
  assert.ok(html.includes('路由失败'), 'the routing failure block is missing');
  assert.ok(html.includes('不可用原因') || html.includes('Unavailable'),
    'the schedule snapshot is missing');
});

test('hasJev reads jev_calls and the jev event, never waves', () => {
  const jevTrace = [{ event: 'jev', questions: ['route'], duration_ms: 9, failed: false, error: null }];
  assert.equal(hasJev({ jev_calls: 1, trace: [] }), true);
  assert.equal(hasJev({ jev_calls: 0, trace: jevTrace }), true);
  assert.equal(hasJev({ jev_calls: 0, trace: [] }), false);
  assert.equal(hasJev({ jev_calls: 0, trace: [] , waves: 3 }), false,
    'a wave-era waves counter must not count as a JEV call');
  assert.equal(hasJev({ jev_calls: 0, trace: [{ event: 'wave', wave: 1, questions: ['route'] }] }), false,
    'a wave event must not count as a JEV call');
  assert.equal(hasJev({ jev_calls: null, trace: null }), false);
});
