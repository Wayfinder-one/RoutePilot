/* Policy v4 前端纯函数契约测试（Task 8 · brief Step 1/Step 3）。
 *
 * 运行：node --test scripts/policy_contract_test.mjs
 * 约束：不加载 DOM、不发请求；createPolicy / behaviorCanonical /
 * optionCountHint 都是纯函数。behaviorCanonical 只服务前端 dirty 比较，
 * 正式 policy_revision 永远由后端计算，这里不做 Python/JS 跨语言字节
 * 对齐断言；也不得加入 nodes/edges 转换测试（不允许转换层）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  behaviorCanonical,
  clone,
  createPolicy,
  optionCountHint,
} from '../router/console/routing/policy.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const FIXTURE = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'tests/fixtures/policy_v4.json'), 'utf8'));

/* questions/targets 是 map：键序重排不得影响 canonical；数组成员顺序必须影响。 */
function reverseKeys(object) {
  return Object.fromEntries(Object.entries(object).reverse());
}

function withOptionCount(question, count) {
  const options = [];
  for (let index = 0; index < count; index += 1) {
    options.push({ key: 'o' + index, label: '选项' + index, capability: '说明' + index });
  }
  return Object.assign({}, question, { options: options });
}

test('createPolicy returns a complete savable v4 document', () => {
  const policy = createPolicy('qwen3-32b');
  assert.equal(policy.kind, 'sr-routing-policy');
  assert.equal(policy.version, 4);
  assert.equal(typeof policy.name, 'string');
  assert.equal(policy.timezone, 'Asia/Shanghai');
  assert.deepEqual(policy.sticky, { enabled: false, ttl_seconds: 1800 });

  const route = policy.questions.route;
  assert.equal(route.type, 'choice');
  assert.ok(Array.isArray(route.options) && route.options.length >= 2, 'route needs options');
  route.options.forEach(option => {
    assert.equal(typeof option.key, 'string');
    assert.equal(typeof option.label, 'string');
    assert.equal(typeof option.capability, 'string');
  });

  ['fast', 'strong', 'safe'].forEach(id => {
    assert.ok(policy.targets[id], 'target missing: ' + id);
    assert.deepEqual(policy.targets[id].models, [{ model: 'qwen3-32b' }]);
  });

  assert.equal(policy.rules.length, 2);
  policy.rules.forEach(rule => {
    assert.equal(typeof rule.name, 'string');
    assert.equal(rule.when.answer, 'route.selected');
    const op = Object.keys(rule.when).find(key => key !== 'answer');
    assert.ok(op, 'answer condition needs exactly one comparison operator');
    assert.ok(policy.targets[rule.target], 'rule target must be defined in targets');
  });
  assert.equal(policy.fallback, 'safe');
});

test('createPolicy honours modelId and returns independent deep copies', () => {
  assert.deepEqual(createPolicy('m1').targets.strong.models, [{ model: 'm1' }]);
  const fallback = createPolicy().fallback;
  assert.ok(fallback === 'safe');
  assert.deepEqual(createPolicy().targets.fast.models, [{ model: 'qwen3-8b' }]);

  const a = createPolicy();
  const b = createPolicy();
  a.questions.route.options[0].label = '被改掉的标签';
  assert.notEqual(b.questions.route.options[0].label, '被改掉的标签',
    'the factory shares mutable state between calls');
  assert.deepEqual(Object.keys(a), Object.keys(b));
});

test('behaviorCanonical is stable across repeated calls and key reordering', () => {
  const first = behaviorCanonical(FIXTURE);
  assert.equal(typeof first, 'string');
  assert.equal(behaviorCanonical(FIXTURE), first, 'repeated calls produced different strings');

  const questionsMoved = Object.assign({}, FIXTURE, { questions: reverseKeys(FIXTURE.questions) });
  assert.equal(behaviorCanonical(questionsMoved), first,
    'question-map key order changed the canonical');

  const targetsMoved = Object.assign({}, FIXTURE, { targets: reverseKeys(FIXTURE.targets) });
  assert.equal(behaviorCanonical(targetsMoved), first,
    'target-map key order changed the canonical');

  const stickyMoved = Object.assign({}, FIXTURE, {
    sticky: { ttl_seconds: FIXTURE.sticky.ttl_seconds, enabled: FIXTURE.sticky.enabled },
  });
  assert.equal(behaviorCanonical(stickyMoved), first, 'sticky key order changed the canonical');
});

test('behaviorCanonical reacts to rules and options array reordering', () => {
  const base = behaviorCanonical(FIXTURE);

  const rulesMoved = clone(FIXTURE);
  rulesMoved.rules.reverse();
  assert.notEqual(behaviorCanonical(rulesMoved), base,
    'rule order is behaviour in v4 (首条命中生效)');

  const optionsMoved = clone(FIXTURE);
  optionsMoved.questions.route.options.reverse();
  assert.notEqual(behaviorCanonical(optionsMoved), base,
    'choice option order changed the canonical');

  const nestedMoved = clone(FIXTURE);
  nestedMoved.rules[1].when.any.reverse();
  assert.notEqual(behaviorCanonical(nestedMoved), base,
    'condition array order nested in any[] changed the canonical');
});

test('behaviorCanonical ignores the display-only name field', () => {
  const base = behaviorCanonical(FIXTURE);
  const renamed = clone(FIXTURE);
  renamed.name = '完全不同的名字';
  assert.equal(behaviorCanonical(renamed), base, 'renaming changed the canonical');
  const nameless = clone(FIXTURE);
  delete nameless.name;
  assert.equal(behaviorCanonical(nameless), base, 'deleting name changed the canonical');
});

test('optionCountHint stays silent up to 8 options and hints from 9 on', () => {
  const question = FIXTURE.questions.route;
  for (let count = 0; count <= 8; count += 1) {
    assert.equal(optionCountHint(withOptionCount(question, count)), '',
      count + ' options must not produce a hint');
  }
  for (const count of [9, 12, 255]) {
    const hint = optionCountHint(withOptionCount(question, count));
    assert.equal(typeof hint, 'string', 'hint must be a string at ' + count + ' options');
    assert.notEqual(hint, '', count + ' options must produce the evaluation hint');
  }
  /* 非 choice 问题没有 options 可数：恒为空。 */
  assert.equal(optionCountHint(FIXTURE.questions.power_business), '');
  assert.equal(optionCountHint(null), '');
  assert.equal(optionCountHint({}), '');
});

test('clone deep-copies nested documents', () => {
  const policy = createPolicy();
  const copy = clone(policy);
  assert.deepEqual(copy, policy);
  copy.questions.route.options[0].key = 'mutated';
  assert.equal(policy.questions.route.options[0].key, 'fast', 'clone shares nested state');
});
