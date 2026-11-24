/* 跨运行时图契约（Node 侧）：与 tests/test_graph_contract.py 共用同一份 fixture。
   ------------------------------------------------------------------
   每个 fixture 是手写的 sr-routing-graph v4 文档；Node 侧走控制台的真实入口：
   `RTGraph.parseImport`（严格解析 + 校验）→ `RTGraph.validate`（硬错误码）→
   `RTGraph.behaviorCanonical`（canonical JSON）→ node:crypto sha256。
   Python 侧由 tests/test_graph_contract.py 对同一 fixture 断言同一组 issue code
   与同一 hash；两端一致才是契约本身。
   ------------------------------------------------------------------ */
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';

import * as RTGraph from '../router/console/routing/graph.js';

const FIXTURES = JSON.parse(
  fs.readFileSync(new URL('../tests/fixtures/graph_contracts.json', import.meta.url), 'utf8')
);
const HEX64 = /^[0-9a-f]{64}$/;
const NUMERIC_OPS = new Set(['gt', 'gte', 'lt', 'lte', 'eq', 'neq']);
const RUNTIME_FIELDS = ['backends', 'jev', 'jev_window', 'time_periods'];
const REQUIRED_FIXTURES = [
  'valid-choice-noul-score',
  'valid-node-edge-shuffle',
  'valid-display-edits',
  'valid-integral-numbers',
  'invalid-empty-graph',
  'invalid-old-otherwise-target',
  'invalid-choice-single-option',
  'invalid-numeric-condition-missing',
  'valid-condition-periods',
  'valid-small-float-threshold',
  'valid-envelope-variation',
  'valid-unicode-departments',
  'valid-old-field-control',
  'valid-self-contained-runtime',
  'invalid-runtime-api-key',
  'invalid-runtime-nested-secret'
];
const BY_NAME = new Map(FIXTURES.map(fixture => [fixture.name, fixture]));

/* 与 Python 的树级 canonical 对齐：sha256(behaviorCanonical(tree))。 */
function behaviorSha256(tree) {
  return crypto.createHash('sha256')
    .update(RTGraph.behaviorCanonical(tree), 'utf8')
    .digest('hex');
}

function hardCodes(issues) {
  return [...new Set(issues.filter(issue => issue.level !== 'hint').map(issue => issue.code))].sort();
}

function nodeContract(document) {
  const result = RTGraph.parseImport(JSON.stringify(document));
  if (!result.ok) {
    const codes = hardCodes(result.issues || []);
    assert.ok(codes.length, 'parseImport rejected the document without an issue code: ' + result.error);
    return { codes, digest: null };
  }
  const codes = hardCodes(RTGraph.validate(result.tree));
  return { codes, digest: codes.length ? null : behaviorSha256(result.tree) };
}

test('graph contract fixtures are well formed', () => {
  assert.ok(FIXTURES.length, 'graph contract fixture set is empty');
  assert.equal(new Set(FIXTURES.map(fixture => fixture.name)).size, FIXTURES.length,
    'duplicate fixture name');
  for (const fixture of FIXTURES) {
    assert.deepEqual(Object.keys(fixture).sort(),
      ['behavior_sha256', 'document', 'issue_codes', 'name']);
    assert.equal(typeof fixture.name, 'string');
    assert.ok(fixture.name.length > 0);
    assert.equal(typeof fixture.document, 'object');
    assert.deepEqual(fixture.issue_codes, [...new Set(fixture.issue_codes)].sort());
    if (fixture.behavior_sha256 === null) {
      assert.ok(fixture.issue_codes.length,
        'an invalid fixture must declare at least one hard issue code');
    } else {
      assert.match(fixture.behavior_sha256, HEX64,
        'valid fixture behavior_sha256 must be 64 lowercase hex characters');
      assert.deepEqual(fixture.issue_codes, [], 'a valid fixture must not declare issue codes');
    }
  }
});

test('graph contract fixtures cover choice/noul/score, boolean/numeric edges and invariances', () => {
  for (const name of REQUIRED_FIXTURES) assert.ok(BY_NAME.has(name), 'missing fixture ' + name);
  const valid = FIXTURES.filter(fixture => fixture.behavior_sha256 !== null);
  assert.ok(valid.length >= 4);
  const roots = valid.map(fixture => fixture.document.root);
  const nodeTypes = new Set(roots.flatMap(root => root.nodes.map(node => node.type)));
  const questionTypes = new Set(roots.flatMap(root =>
    root.nodes.filter(node => node.type === 'question').map(node => node.question_type)));
  const conditions = roots.flatMap(root => root.edges.map(edge => edge.condition));
  for (const type of ['start', 'condition', 'logic', 'question', 'target']) {
    assert.ok(nodeTypes.has(type), 'missing ' + type + ' node in the valid fixture set');
  }
  for (const type of ['choice', 'noul', 'score']) {
    assert.ok(questionTypes.has(type), 'missing ' + type + ' question in the valid fixture set');
  }
  assert.ok(conditions.some(condition => condition === null || condition.op === 'is'),
    'no boolean edge in the valid fixture set');
  assert.ok(conditions.some(condition => condition !== null && NUMERIC_OPS.has(condition.op)),
    'no numeric edge in the valid fixture set');

  /* 主 fixture 必须自成一张覆盖全部节点类型与问题类型的图。 */
  const mainRoot = BY_NAME.get('valid-choice-noul-score').document.root;
  const mainTypes = new Set(mainRoot.nodes.map(node => node.type));
  const mainQuestions = new Set(
    mainRoot.nodes.filter(node => node.type === 'question').map(node => node.question_type)
  );
  for (const type of ['start', 'condition', 'logic', 'question', 'target']) {
    assert.ok(mainTypes.has(type), 'main fixture missing ' + type + ' node');
  }
  for (const type of ['choice', 'noul', 'score']) {
    assert.ok(mainQuestions.has(type), 'main fixture missing ' + type + ' question');
  }

  const mainHash = BY_NAME.get('valid-choice-noul-score').behavior_sha256;
  assert.equal(BY_NAME.get('valid-node-edge-shuffle').behavior_sha256, mainHash,
    'node/edge input-order shuffling must not change the behavior hash');
  assert.equal(BY_NAME.get('valid-display-edits').behavior_sha256, mainHash,
    'title/position edits must not change the behavior hash');
  assert.notEqual(BY_NAME.get('valid-integral-numbers').behavior_sha256, mainHash);

  assert.equal(
    BY_NAME.get('invalid-old-otherwise-target').document.root.otherwise_target,
    'target-a',
    'the old-field fixture must exercise the rejected otherwise_target key'
  );

  /* 补充契约面：periods 条件、浮点指数阈值、信封取值变化与旧字段正向对照。 */
  const conditionNodes = roots.flatMap(root =>
    root.nodes.filter(node => node.type === 'condition'));
  assert.ok(conditionNodes.some(node =>
    node.source === 'periods' && Array.isArray(node.periods) && node.periods.length > 0),
  'no periods-source condition in the valid fixture set');
  assert.ok(conditions.some(condition => condition !== null &&
    NUMERIC_OPS.has(condition.op) && typeof condition.value === 'number' &&
    Math.abs(condition.value) > 0 && Math.abs(condition.value) < 1e-4),
  'no sub-1e-4 numeric threshold in the valid fixture set');

  const envelope = BY_NAME.get('valid-envelope-variation').document;
  assert.notEqual(envelope.timezone, 'Asia/Shanghai');
  const unicodeDepartments = BY_NAME.get('valid-unicode-departments').document.departments;
  assert.ok(unicodeDepartments.length > 0 &&
    unicodeDepartments.some(value => Array.from(value).some(char => char.codePointAt(0) > 0xFFFF)),
  'no non-BMP department in the valid fixture set');

  assert.deepEqual(envelope.sticky, { enabled: false, ttl_seconds: 600 });
  assert.equal(envelope.root.error_fallback, 'target-a');

  const oldDocument = BY_NAME.get('invalid-old-otherwise-target').document;
  const controlDocument = BY_NAME.get('valid-old-field-control').document;
  const expectedControl = JSON.parse(JSON.stringify(oldDocument));
  delete expectedControl.root.otherwise_target;
  assert.deepEqual(controlDocument, expectedControl,
    'the positive control must be the rejected document without otherwise_target');

  /* 自包含运行配置：四字段成组、整数值浮点归一、密钥字段拒绝。 */
  const runtimeDocument = BY_NAME.get('valid-self-contained-runtime').document;
  for (const field of RUNTIME_FIELDS) {
    assert.ok(Object.prototype.hasOwnProperty.call(runtimeDocument, field),
      'self-contained fixture is missing runtime field ' + field);
  }
  assert.equal(runtimeDocument.jev.timeout_seconds, 10,
    'the fixture must pin an integral float (10.0) for jev.timeout_seconds');
  assert.equal(runtimeDocument.backends.models[0].profile.context_window, 131072,
    'the fixture must pin an integral profile number');
  assert.equal(Object.prototype.hasOwnProperty.call(runtimeDocument.backends.providers[0], 'api_key'),
    false, 'the valid fixture must not carry keys');
  const keyDocument = BY_NAME.get('invalid-runtime-api-key').document;
  assert.ok(Object.prototype.hasOwnProperty.call(keyDocument.backends.providers[0], 'api_key'),
    'the invalid fixture must exercise a persisted api_key');
});

test('self-contained runtime fixture imports with normalized defaults', () => {
  const fixture = BY_NAME.get('valid-self-contained-runtime');
  const result = RTGraph.parseImport(JSON.stringify(fixture.document));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  const runtime = result.tree.runtime;
  assert.deepEqual(Object.keys(runtime).sort(), ['backends', 'jev', 'jev_window', 'time_periods']);
  /* 缺省键补默认值：key_env="" / reasoning_format="openai"，jev 用默认文案。 */
  assert.equal(runtime.backends.providers[1].key_env, '');
  assert.equal(runtime.backends.providers[1].reasoning_format, 'openai');
  assert.equal(runtime.jev.timeout_seconds, 10);
  assert.ok(runtime.jev.instructions.length > 0);
  assert.ok(runtime.jev.state_template.includes('{request}'));
  assert.equal(runtime.backends.providers[0].base_url, 'http://json-only.test/v1');
  assert.deepEqual(runtime.backends.models[0].profile,
    { capability: '来自 JSON 的能力', context_window: 131072, rate: 1e-05, reasoning: { efforts: { off: null, high: 'high' }, default: 'off' } });

  /* 密钥 fixture：解析阶段即拒绝，不允许部分接受。 */
  const rejected = RTGraph.parseImport(JSON.stringify(BY_NAME.get('invalid-runtime-api-key').document));
  assert.equal(rejected.ok, false);
  assert.equal(rejected.tree, undefined);
  assert.ok(rejected.issues.some(issue => issue.code === 'graph_parse_error'));
});

/* 严格自包含：四字段必须成组出现；全缺失、部分缺失、jev 缺 id、runtime 控制字段
   一律 graph_parse_error（与 Python tree_from_dict / runtime_from_dict 同规则）。 */
test('strict runtime document requires all four fields together', () => {
  const fixture = BY_NAME.get('valid-self-contained-runtime');
  for (const field of RUNTIME_FIELDS) {
    const document = JSON.parse(JSON.stringify(fixture.document));
    delete document[field];
    const result = RTGraph.parseImport(JSON.stringify(document));
    assert.equal(result.ok, false, 'import accepted a document without ' + field);
    assert.equal(result.tree, undefined);
    const issue = (result.issues || []).find(item => item.code === 'graph_parse_error');
    assert.ok(issue, 'missing ' + field + ' did not report graph_parse_error');
    assert.ok(issue.zh.includes(field) && issue.zh.includes('成组出现'),
      'missing ' + field + ' issue does not name the field: ' + issue.zh);
    assert.ok(issue.en.includes(field), 'missing ' + field + ' en issue does not name the field');
  }
  /* 四个运行字段全缺失 = 旧文档，同样拒绝（不再有旧文档兼容分支）。 */
  const legacy = JSON.parse(JSON.stringify(fixture.document));
  for (const field of RUNTIME_FIELDS) delete legacy[field];
  const legacyResult = RTGraph.parseImport(JSON.stringify(legacy));
  assert.equal(legacyResult.ok, false, 'a runtime-less legacy document was accepted');
  assert.ok(legacyResult.issues.some(item => item.code === 'graph_parse_error'));
});

test('jev.id is required and a nested runtime control field is rejected', () => {
  const fixture = BY_NAME.get('valid-self-contained-runtime');
  const withoutId = JSON.parse(JSON.stringify(fixture.document));
  delete withoutId.jev.id;
  const rejected = RTGraph.parseImport(JSON.stringify(withoutId));
  assert.equal(rejected.ok, false, 'a jev snapshot without id was accepted');
  const issue = rejected.issues.find(item => item.code === 'graph_parse_error');
  assert.ok(issue);
  assert.match(issue.zh, /jev\.id/);
  assert.match(issue.en, /jev\.id/);

  /* runtime 控制字段（旧文档的 runtime:null / 嵌套 runtime）一律拒绝。 */
  const nestedNull = JSON.parse(JSON.stringify(fixture.document));
  nestedNull.runtime = null;
  const nullResult = RTGraph.parseImport(JSON.stringify(nestedNull));
  assert.equal(nullResult.ok, false, 'a runtime:null control field was accepted');
  assert.ok(nullResult.issues.some(item => item.code === 'graph_parse_error'));
  const nestedObject = JSON.parse(JSON.stringify(fixture.document));
  nestedObject.runtime = { backends: fixture.document.backends };
  assert.equal(RTGraph.parseImport(JSON.stringify(nestedObject)).ok, false);
});

test('serialize always emits the four runtime fields and preserves canvas coordinates', () => {
  const fixture = BY_NAME.get('valid-self-contained-runtime');
  const parsed = RTGraph.parseImport(JSON.stringify(fixture.document));
  assert.equal(parsed.ok, true, JSON.stringify(parsed.issues));
  const document = JSON.parse(RTGraph.serialize(parsed.tree));
  for (const field of RUNTIME_FIELDS) {
    assert.ok(Object.prototype.hasOwnProperty.call(document, field),
      'serialize dropped runtime field ' + field);
  }
  assert.deepEqual(document.canvas, fixture.document.canvas,
    'serialize did not preserve the top-level canvas coordinates');
  assert.equal(document.jev.id, fixture.document.jev.id);
  assert.equal(document.jev.key_env, '');
  assert.equal(document.jev.timeout_seconds, 10);
  assert.equal(document.jev_window.input_tokens, 1024);
  assert.deepEqual(document.time_periods, fixture.document.time_periods);
  /* 序列化文本里只有四字段，没有 runtime:null 或嵌套 runtime 键。 */
  assert.equal(document.runtime, undefined);
  assert.equal(RTGraph.serialize(parsed.tree).includes('"runtime"'), false);

  /* serialize 拒绝缺 runtime 的树：导出不维护旧文档分支。 */
  const broken = RTGraph.clone(parsed.tree);
  delete broken.runtime;
  assert.throws(() => RTGraph.serialize(broken), /backends/);
  const partial = RTGraph.clone(parsed.tree);
  delete partial.runtime.jev_window;
  assert.throws(() => RTGraph.serialize(partial), /jev_window/);
});

test('createMinimalTree requires a complete runtime with the target model', () => {
  const fixture = BY_NAME.get('valid-self-contained-runtime');
  const parsed = RTGraph.parseImport(JSON.stringify(fixture.document));
  const tree = RTGraph.createMinimalTree('fast', parsed.tree.runtime);
  assert.deepEqual(tree.runtime.backends.models.map(model => model.id), ['fast', 'backup-model'],
    'createMinimalTree must keep the complete runtime snapshot');
  assert.equal(tree.root.nodes.find(node => node.type === 'target').models[0].model, 'fast');
  assert.equal(hardCodes(RTGraph.validate(tree)).length, 0);
  assert.throws(() => RTGraph.createMinimalTree('fast'), /运行配置/);
  assert.throws(() => RTGraph.createMinimalTree('unknown-model', parsed.tree.runtime), /unknown-model/);
});

for (const fixture of FIXTURES) {
  test('graph contract · ' + fixture.name, () => {
    const { codes, digest } = nodeContract(fixture.document);
    assert.deepEqual(codes, fixture.issue_codes,
      'Node issue codes for ' + fixture.name + ' do not match the fixture');
    if (fixture.behavior_sha256 === null) {
      assert.equal(digest, null);
    } else {
      assert.equal(digest, fixture.behavior_sha256,
        'Node behavior hash for ' + fixture.name + ' does not match the fixture; ' +
        'the Python test asserts the same value');
    }
  });
}
