/* 跨运行时图契约（Node 侧）：与 tests/test_graph_contract.py 共用同一份 fixture。
   ------------------------------------------------------------------
   每个 fixture 是手写的 sr-routing-graph v3 文档；Node 侧走控制台的真实入口：
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
const REQUIRED_FIXTURES = [
  'valid-choice-noul-score',
  'valid-node-edge-shuffle',
  'valid-display-edits',
  'valid-integral-numbers',
  'invalid-empty-graph',
  'invalid-old-otherwise-target',
  'invalid-choice-single-option',
  'invalid-numeric-condition-missing'
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
