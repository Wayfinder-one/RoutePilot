/* RTGraph 纯函数层契约测试（Task 9，规格 §2.2–§2.8 / §5）。
 *
 * 运行：node --test scripts/console_graph_test.mjs
 * 加载：graph.js / trace.js 是原生 ES module，浏览器与 Node 共用同一份源码。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import * as RTGraph from '../router/console/routing/graph.js';
import * as SRTrace from '../router/console/trace.js';

const EXAMPLE_URL = new URL('../router/console/routing/example.json', import.meta.url);

function readExample() {
  /* 示例文件是完整 v4 文档：parseImport 校验并把顶层 canvas 分发回内部 node.position，
     返回带坐标的 root（沿用旧 helper「返回 root」的契约）。 */
  const parsed = RTGraph.parseImport(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  assert.equal(parsed.ok, true,
    'example.json must import cleanly: ' + JSON.stringify(parsed.issues));
  return parsed.tree.root;
}

function exampleGraph() {
  return RTGraph.clone(readExample());
}

/* 严格自包含：新草稿的 runtime 必须是完整四字段快照，且 target 模型在快照内。 */
function minimalRuntime(modelId) {
  const model = modelId || 'qwen3-8b';
  return {
    backends: {
      providers: [{ name: 'local', base_url: 'http://upstream.test/v1', key_env: '', reasoning_format: 'openai' }],
      models: [{ id: model, upstream: model, provider: 'local', profile: {} }]
    },
    jev: null,
    jev_window: { input_tokens: 1024, context_tokens: 4096 },
    time_periods: []
  };
}

function minimalTree(modelId) {
  const model = modelId || 'qwen3-8b';
  return RTGraph.createMinimalTree(model, minimalRuntime(model));
}

/* 手工文档的完整 runtime：target 引用的模型必须在快照里（严格契约会拒绝）。 */
function documentRuntime(modelIds, periods) {
  const ids = Array.isArray(modelIds) && modelIds.length ? modelIds : ['qwen3-8b'];
  const runtime = minimalRuntime(ids[0], periods);
  runtime.backends.models = ids.map(id => ({
    id: id, upstream: id, provider: 'local', profile: {}
  }));
  return runtime;
}

function exampleRuntime() {
  const parsed = RTGraph.parseImport(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  assert.equal(parsed.ok, true, JSON.stringify(parsed.issues));
  return parsed.tree.runtime;
}

function targetOf(graph) {
  return graph.nodes.find(node => node.type === 'target');
}

function legacyNotTree() {
  const tree = minimalTree();
  const graph = tree.root;
  graph.nodes.push(
    { id: 'q', type: 'question', question_type: 'choice', instructions: '能力？', options: [
      { key: 'easy', label: '轻量', capability: '简单。' },
      { key: 'strong', label: '增强', capability: '复杂。' },
    ], position: { x: 200, y: 200 } },
    { id: 'legacy-not', type: 'logic', operator: 'not', position: { x: 400, y: 200 } },
  );
  graph.edges.push(
    { id: 'e-start-q', source: 'start', source_port: 'out', target: 'q', target_port: 'in_1', condition: null },
    { id: 'e-q-not', source: 'q', source_port: 'prob.strong', target: 'legacy-not', target_port: 'in_1',
      condition: { op: 'gt', value: 0.65 } },
    { id: 'e-not-target', source: 'legacy-not', source_port: 'out', target: targetOf(graph).id,
      target_port: 'in_2', condition: null },
  );
  return tree;
}

function codesOf(issues) {
  return issues.filter(issue => issue.level !== 'hint').map(issue => issue.code);
}

function hardIssues(issues) {
  return issues.filter(issue => issue.level !== 'hint');
}

function importText(tree) {
  /* 新格式：节点对象不带 position，坐标只出现在顶层 canvas。
     helper 把内部 node.position 收集进 canvas，既有用例仍能表达「带坐标的完整 envelope」。 */
  const root = RTGraph.clone(tree.root);
  const canvas = {};
  (Array.isArray(root.nodes) ? root.nodes : []).forEach(node => {
    if (!node || typeof node !== 'object' || Array.isArray(node)) return;
    const position = node.position;
    if (position && typeof position === 'object' && !Array.isArray(position) &&
        Number.isFinite(position.x) && Number.isFinite(position.y)) {
      canvas[node.id] = { x: position.x, y: position.y };
    }
    delete node.position;
  });
  const document = {
    kind: 'sr-routing-graph',
    version: 4,
    name: tree.name == null ? '' : tree.name,
    departments: RTGraph.normalizeDepartments(tree.departments),
    enabled: tree.enabled === true,
    default: tree.default === true,
    timezone: tree.timezone == null ? 'Asia/Shanghai' : tree.timezone,
    sticky: tree.sticky == null ? { enabled: false, ttl_seconds: 1800 } : tree.sticky,
    root: root,
  };
  /* 严格自包含：导入文档必须带完整四字段快照（helpers 构造的树同样如此）。 */
  const runtime = tree.runtime;
  if (!runtime) throw new Error('importText helper needs a tree with a complete runtime');
  RTGraph.RUNTIME_FIELDS.forEach(field => { document[field] = RTGraph.clone(runtime[field]); });
  if (Object.keys(canvas).length) document.canvas = canvas;
  return JSON.stringify(document);
}

function scoreEdge(condition) {
  return {
    id: 'e-score-value-target-in_2',
    source: 'score',
    source_port: 'value',
    target: 'target-1',
    target_port: 'in_2',
    condition,
  };
}

function choiceGraph() {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  const node = {
    id: 'capability',
    type: 'question',
    question_type: 'choice',
    instructions: '这条请求需要哪个能力档位？',
    options: [
      { key: 'easy', label: '轻量模型', capability: '简单问答。' },
      { key: 'standard', label: '标准模型', capability: '中等写作。' },
      { key: 'strong', label: '增强模型', capability: '复杂分析。' },
    ],
    position: { x: 200, y: 200 },
  };
  graph.nodes.push(node);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'capability', source_port: 'selected.easy', target: target.id, target_port: 'in_2',
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'capability', source_port: 'prob.easy', target: target.id, target_port: 'in_3',
    condition: { op: 'gt', value: 0.5 },
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'capability', source_port: 'selected.standard', target: target.id, target_port: 'in_4',
  }).ok, true);
  return { graph, node, target };
}

/* ── 1. 直方示例（brief 原文，不得改动断言） ───────────────────────── */

test('layout-only changes keep behavior canonical and titles are display changes', () => {
  const before = minimalTree('qwen3-8b');
  const moved = RTGraph.clone(before);
  moved.root.nodes[0].position = { x: 900, y: 700 };
  assert.equal(RTGraph.behaviorCanonical(before), RTGraph.behaviorCanonical(moved));
  assert.equal(RTGraph.changeKind(before, moved), 'layout');
  moved.root.nodes[0].title = '入口';
  assert.equal(RTGraph.changeKind(before, moved), 'display');
  moved.timezone = 'UTC';
  assert.equal(RTGraph.changeKind(before, moved), 'behavior');
});

test('slot numbers remain stable and the smallest hole is reused', () => {
  const graph = minimalTree('qwen3-8b');
  const target = graph.root.nodes.find(node => node.type === 'target');
  graph.root.edges[0].target_port = 'in_3';
  assert.equal(RTGraph.nextSlot(graph.root, target.id), 'in_1');
});

/* ── 2. 条件求值（六算子 / boolean false / unknown / invalid） ──────── */

test('numeric slot conditions support all six operators', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: [1, 10], position: { x: 0, y: 0 },
  });
  const cases = [
    ['gt', 5, 7, true],
    ['gte', 7, 7, true],
    ['lt', 5, 3, true],
    ['lte', 3, 3, true],
    ['eq', 0.84, 0.84, true],
    ['neq', 0.5, 0.84, true],
    ['gt', 5, 5, false],
    ['gte', 7, 6.9, false],
    ['lt', 5, 9, false],
    ['lte', 3, 3.1, false],
    ['eq', 0.5, 0.84, false],
    ['neq', 0.84, 0.84, false],
  ];
  assert.equal(target.id, 'target-1');
  for (const [op, threshold, value, expected] of cases) {
    const result = RTGraph.evaluateCondition(scoreEdge({ op, value: threshold }), graph, value);
    assert.equal(result.status, expected ? 'true' : 'false', `${op} ${threshold} vs ${value}`);
    assert.equal(result.result, expected);
  }
});

test('boolean conditions default to true and support is false', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  const edge = condition => ({
    id: 'e-start-target', source: 'start', source_port: 'out',
    target: target.id, target_port: 'in_1', condition,
  });
  const status = (condition, value) => RTGraph.evaluateCondition(edge(condition), graph, value).status;

  assert.equal(status(null, true), 'true');
  assert.equal(status(null, false), 'false');
  assert.equal(status({ op: 'is', value: false }, false), 'true');
  assert.equal(status({ op: 'is', value: false }, true), 'false');
  assert.equal(status({ op: 'is', value: true }, true), 'true');
  assert.equal(status({ op: 'is', value: true }, false), 'false');
  assert.equal(status(null, 'unknown'), 'unknown');
  assert.equal(status(null, undefined), 'unknown');
});

test('boolean condition guidance uses pass-through and negation terminology', () => {
  const graph = exampleGraph();
  const hints = RTGraph.validate(graph).filter(issue => issue.level === 'hint');
  const capability = RTGraph.findNode(graph, 'capability');
  const input = RTGraph.portOf(capability, 'in_1', graph);
  const text = hints.map(issue => issue.zh + '\n' + issue.en).join('\n') + '\n' + input.desc;

  assert.doesNotMatch(text, /= (?:true|false)/);
  assert.match(text, /直接通过/);
  assert.match(text, /取反/);
  assert.match(text, /pass(?:es)? through/i);
  assert.match(text, /negat/i);
});

test('unknown upstream values propagate as unknown, never as false', () => {
  const graph = minimalTree().root;
  graph.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: [1, 10], position: { x: 0, y: 0 },
  });
  const numeric = RTGraph.evaluateCondition(scoreEdge({ op: 'gt', value: 0.5 }), graph, 'unknown');
  assert.equal(numeric.status, 'unknown');
  assert.equal(numeric.result, 'unknown');
  assert.equal(numeric.result === false, false);
});

test('invalid conditions are reported with a specific reason', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: [1, 10], position: { x: 0, y: 0 },
  });
  assert.equal(RTGraph.conditionInvalidReason(scoreEdge(null), graph), '数值输入必须配置比较条件');
  assert.match(RTGraph.conditionInvalidReason(scoreEdge({ op: 'is', value: true }), graph), /算子/);
  assert.match(RTGraph.conditionInvalidReason(scoreEdge({ op: 'gt', value: NaN }), graph), /有限数值/);
  assert.match(RTGraph.conditionInvalidReason(scoreEdge({ op: 'gt', value: 1e10 }), graph), /1e9/);

  const booleanEdge = {
    id: 'e-start-target', source: 'start', source_port: 'out',
    target: target.id, target_port: 'in_1', condition: { op: 'gt', value: 0.5 },
  };
  assert.match(RTGraph.conditionInvalidReason(booleanEdge, graph), /布尔/);

  const result = RTGraph.evaluateCondition(scoreEdge(null), graph, 0.9);
  assert.equal(result.status, 'invalid');
  assert.equal(result.result, 'unknown');
  assert.ok(result.reason);
});

/* ── 3. 端口 / 类型 ─────────────────────────────────────────────────── */

test('port registry matches the graph v4 contract', () => {
  const graph = exampleGraph();
  const capability = graph.nodes.find(node => node.id === 'capability');
  const selected = RTGraph.portOf(capability, 'selected.easy', graph);
  const prob = RTGraph.portOf(capability, 'prob.easy', graph);
  assert.equal(selected.type, 'boolean');
  assert.equal(selected.dir, 'out');
  assert.equal(prob.type, 'number');
  assert.equal(RTGraph.portOf(capability, 'prob.easy', graph).group, 'option:easy');
  assert.equal(RTGraph.portOf(capability, 'nope', graph), null);

  const domain = graph.nodes.find(node => node.id === 'domain');
  assert.deepEqual(
    RTGraph.portsOf(domain, graph).filter(port => port.dir === 'out').map(port => port.id).sort(),
    ['no', 'yes'],
  );
  assert.ok(RTGraph.portsOf(domain, graph).filter(port => port.dir === 'out').every(port => port.type === 'number'));

  const condition = graph.nodes.find(node => node.id === 'cond-text');
  assert.equal(RTGraph.portOf(condition, 'out', graph).type, 'boolean');

  const logic = graph.nodes.find(node => node.id === 'and-power');
  const slots = RTGraph.portsOf(logic, graph).filter(port => port.slot);
  assert.equal(slots.length >= 2, true);
  assert.ok(slots.filter(port => port.occupied).every(port => port.type === 'boolean'));
  assert.equal(RTGraph.portOf(logic, 'out', graph).type, 'boolean');

  const target = graph.nodes.find(node => node.id === 'power-target');
  assert.ok(RTGraph.portsOf(target, graph).every(port => port.slot && port.type === 'boolean'));

  graph.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: [1, 10], position: { x: 0, y: 0 },
  });
  assert.equal(RTGraph.portOf(graph.nodes.find(node => node.id === 'score'), 'value', graph).type, 'number');

  assert.equal(RTGraph.edgeType(graph.edges.find(edge => edge.source_port === 'prob.strong'), graph), 'number');
  assert.equal(RTGraph.edgeType(graph.edges.find(edge => edge.source_port === 'selected.strong'), graph), 'boolean');
});

test('slot numbers are stable after removals and reuse the smallest hole', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  const extra = {
    id: 'cond', type: 'condition', source: 'periods', periods: ['peak'],
    position: { x: 0, y: 0 },
  };
  graph.nodes.push(extra);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'cond', source_port: 'out', target: target.id, target_port: 'in_2',
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'cond', source_port: 'out', target: target.id, target_port: 'in_3',
  }).ok, true);
  assert.equal(RTGraph.nextSlot(graph, target.id), 'in_4');

  const third = graph.edges.find(edge => edge.target === target.id && edge.target_port === 'in_3');
  assert.equal(RTGraph.removeEdge(graph, third.id).ok, true);
  assert.equal(RTGraph.nextSlot(graph, target.id), 'in_3');
  assert.deepEqual(
    graph.edges.filter(edge => edge.target === target.id).map(edge => edge.target_port).sort(),
    ['in_1', 'in_2'],
  );
});

/* ── 4. 连线合法性 ─────────────────────────────────────────────────── */

test('one output port can fan out to several slots', () => {
  const graph = minimalTree().root;
  graph.nodes.push({
    id: 'q1', type: 'question', question_type: 'noul', instructions: '是否需要？',
    position: { x: 0, y: 0 },
  });
  assert.equal(RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: 'q1', target_port: 'in_1',
  }).ok, true);
  const added = RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'q1', target_port: 'in_1',
  });
  assert.equal(added.ok, true);
  assert.equal(RTGraph.edgeType(added.edge, graph), 'boolean');
  assert.equal(added.edge.condition, null);
  assert.equal(RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: 'q1', target_port: 'in_2',
  }).ok, true);
});

test('connect rejects self, duplicate, occupied slots and cycles', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);

  const self = RTGraph.canConnect(graph, { source: 'start', source_port: 'out', target: 'start', target_port: 'in_1' });
  assert.equal(self.ok, false);
  assert.match(self.reason, /自身/);

  const duplicate = RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: target.id, target_port: 'in_1',
  });
  assert.equal(duplicate.ok, false);
  assert.match(duplicate.reason, /已存在/);

  graph.nodes.push({
    id: 'cond', type: 'condition', source: 'periods', periods: ['peak'],
    position: { x: 0, y: 0 },
  });
  const occupied = RTGraph.canConnect(graph, {
    source: 'cond', source_port: 'out', target: target.id, target_port: 'in_1',
  });
  assert.equal(occupied.ok, false);
  assert.match(occupied.reason, /槽位|占用/);

  graph.nodes.push(
    { id: 'and-1', type: 'logic', operator: 'and', position: { x: 0, y: 0 } },
    { id: 'and-2', type: 'logic', operator: 'and', position: { x: 0, y: 0 } },
  );
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'and-1', target_port: 'in_1',
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'and-1', source_port: 'out', target: 'and-2', target_port: 'in_1',
  }).ok, true);
  const cycle = RTGraph.canConnect(graph, {
    source: 'and-2', source_port: 'out', target: 'and-1', target_port: 'in_2',
  });
  assert.equal(cycle.ok, false);
  assert.match(cycle.reason, /环/);
  assert.equal(RTGraph.canConnect(graph, {
    source: 'cond', source_port: 'out', target: 'no-such-node', target_port: 'in_1',
  }).ok, false);
});

test('canConnect tolerates malformed edge entries kept by import instead of throwing', () => {
  const tree = minimalTree();
  const graph = tree.root;
  const target = targetOf(graph);
  /* parseImport 故意保留非对象 entry 以便 validate 报 graph_parse_error；
     端口探测/快速创建不能因此抛异常。 */
  graph.edges.push(null, 42, 'not-an-edge', [1, 2]);
  const check = RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: target.id, target_port: 'in_2',
  });
  assert.equal(check.ok, true, 'a trailing malformed edge blocked a valid probe');
  const duplicate = RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: target.id, target_port: 'in_1',
  });
  assert.equal(duplicate.ok, false);
  assert.match(duplicate.reason, /已存在/);
  assert.equal(RTGraph.canConnect(graph, {
    source: 'start', source_port: 'out', target: target.id, target_port: 'in_1',
  }).ok, false);
  const imported = RTGraph.parseImport(JSON.stringify(Object.assign({
    kind: 'sr-routing-graph', version: 4, name: 'x', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 }, root: graph,
  }, documentRuntime(['qwen3-8b']))));
  assert.equal(imported.ok, false, 'non-object edges made parseImport return ok');
  assert.equal(hardIssues(imported.issues).some(issue => issue.code === 'graph_parse_error'), true,
    'malformed edge entries must still surface as a validation issue');
});

test('addEdge writes default conditions and stable unique ids', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push(
    { id: 'score', type: 'question', question_type: 'score', instructions: '评分。', criteria: [1, 10], position: { x: 0, y: 0 } },
    { id: 'q1', type: 'question', question_type: 'noul', instructions: '是否需要？', position: { x: 0, y: 0 } },
  );
  const numeric = RTGraph.addEdge(graph, {
    source: 'score', source_port: 'value', target: 'q1', target_port: 'in_1',
  });
  assert.equal(numeric.ok, true);
  assert.deepEqual(numeric.edge.condition, { op: 'gt', value: 0.5 });
  assert.equal(numeric.edge.source_port, 'value');
  assert.ok(numeric.edge.id);

  const boolean = RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'q1', target_port: 'in_2',
  });
  assert.equal(boolean.ok, true);
  assert.equal(boolean.edge.condition, null);

  const ids = graph.edges.map(edge => edge.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: target.id, target_port: 'in_1',
  }).ok, false);
});

/* ── 5. 校验（可达性 / 结构） ───────────────────────────────────────── */

test('phase-0 conditions count as reachability sources for questions', () => {
  const graph = {
    id: 'root',
    type: 'graph',
    nodes: [
      { id: 'start', type: 'start', position: { x: 0, y: 0 } },
      { id: 'cond', type: 'condition', source: 'text', contains_any: ['抢修'], position: { x: 0, y: 0 } },
      { id: 'q', type: 'question', question_type: 'noul', instructions: '是否需要？', position: { x: 0, y: 0 } },
      { id: 'target', type: 'target', models: [{ model: 'qwen3-8b' }], priority: 1, position: { x: 0, y: 0 } },
    ],
    edges: [
      { id: 'e1', source: 'start', source_port: 'out', target: 'target', target_port: 'in_1', condition: null },
      { id: 'e2', source: 'cond', source_port: 'out', target: 'q', target_port: 'in_1', condition: null },
    ],
    error_fallback: 'target',
  };
  assert.equal(codesOf(RTGraph.validate(graph)).includes('question_unreachable'), false);

  graph.edges = graph.edges.filter(edge => edge.id !== 'e2');
  const issues = RTGraph.validate(graph);
  assert.ok(issues.some(issue => issue.code === 'question_unreachable' && issue.node_id === 'q'));
});

test('validate reports structural problems with locatable codes', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({ id: 'mystery', type: 'mystery', position: { x: 0, y: 0 } });
  graph.nodes.push({ id: 'start', type: 'start', position: { x: 0, y: 0 } });
  graph.edges.push({
    id: 'e-dup-slot', source: 'start', source_port: 'out',
    target: target.id, target_port: 'in_1', condition: null,
  });
  graph.error_fallback = null;

  const issues = RTGraph.validate(graph);
  const codes = codesOf(issues);
  assert.ok(codes.includes('graph_parse_error'));
  assert.ok(codes.includes('duplicate_node_id'));
  assert.ok(codes.includes('start_count'));
  assert.ok(issues.some(issue => issue.code === 'slot_conflict' && issue.node_id === target.id && issue.port === 'in_1' && issue.edge_id));
  assert.ok(issues.some(issue => issue.code === 'fallback_missing' && issue.port === 'error_fallback'));
  graph.error_fallback = 'missing-target';
  assert.ok(RTGraph.validate(graph).some(issue => issue.code === 'fallback_missing' && issue.node_id === 'missing-target'));
  assert.ok(issues.every(issue => typeof issue.zh === 'string' && issue.zh.length > 0));
  assert.ok(issues.every(issue => typeof issue.en === 'string' && issue.en.length > 0));
});

test('validate rejects NOT and enforces AND/OR at least two slots', () => {
  const graph = minimalTree().root;
  graph.nodes.push(
    { id: 'and', type: 'logic', operator: 'and', position: { x: 0, y: 0 } },
    { id: 'not', type: 'logic', operator: 'not', position: { x: 0, y: 0 } },
  );
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'and', target_port: 'in_1',
  }).ok, true);
  const issues = RTGraph.validate(graph);
  assert.ok(issues.some(issue => issue.code === 'logic_operator_invalid' && issue.node_id === 'not'));
  assert.ok(issues.some(issue => issue.code === 'logic_slot_count' && issue.node_id === 'and'));
});

test('legacy NOT import is rejected without migration or draft mutation', () => {
  const original = legacyNotTree();
  const before = JSON.stringify(original);
  const parsed = RTGraph.parseImport(importText(original));
  assert.equal(parsed.ok, false);
  assert.equal(parsed.tree, undefined);
  assert.ok(parsed.issues.some(issue =>
    issue.code === 'logic_operator_invalid' && issue.node_id === 'legacy-not'));
  assert.equal(JSON.stringify(original), before);
});


test('unknown fields on a rejected NOT node cannot disappear during import', () => {
  const original = legacyNotTree();
  original.root.nodes.find(node => node.id === 'legacy-not').unexpected = true;
  const parsed = RTGraph.parseImport(importText(original));
  assert.equal(parsed.ok, false);
  assert.equal(parsed.tree, undefined);
  assert.ok(parsed.issues.some(issue => issue.code === 'graph_parse_error'));
});

test('validate reports dangling ports, invalid conditions and question config', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({
    id: 'choice', type: 'question', question_type: 'choice', instructions: '',
    options: [
      { key: 'a', label: '', capability: '' },
      { key: 'a', label: '重复', capability: '说明' },
    ],
    position: { x: 0, y: 0 },
  });
  graph.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: [1], position: { x: 0, y: 0 },
  });
  graph.nodes.push({
    id: 'cond', type: 'condition', source: 'text', contains_any: [], position: { x: 0, y: 0 },
  });
  graph.edges.push({
    id: 'e-dangling-port', source: 'choice', source_port: 'selected.gone',
    target: target.id, target_port: 'in_2', condition: null,
  });
  graph.edges.push({
    id: 'e-bad-numeric', source: 'score', source_port: 'value',
    target: target.id, target_port: 'in_3', condition: null,
  });

  const codes = codesOf(RTGraph.validate(graph));
  assert.ok(codes.includes('edge_port_missing'));
  assert.ok(codes.includes('numeric_edge_condition_missing'));
  assert.ok(codes.includes('question_incomplete'));
  assert.ok(codes.includes('score_criteria_invalid'));
  assert.ok(codes.includes('condition_config_invalid'));
});

/* ── 6. 自动布局 / 导入导出 ─────────────────────────────────────────── */

test('layoutPositions lays the graph out by DAG depth', () => {
  const graph = exampleGraph();
  const positions = RTGraph.layoutPositions('dag', graph);
  assert.deepEqual(Object.keys(positions).sort(), graph.nodes.map(node => node.id).sort());
  for (const position of Object.values(positions)) {
    assert.equal(Number.isFinite(position.x), true);
    assert.equal(Number.isFinite(position.y), true);
  }
  assert.equal(positions.start.x < positions['power-target'].x, true);
  assert.equal(positions['capability'].x < positions['and-power'].x, true);
  assert.deepEqual(RTGraph.layoutPositions('dag', graph), positions);
});

test('import fills missing positions with the DAG layout', () => {
  const root = readExample();
  root.nodes.forEach(node => { delete node.position; });
  const text = JSON.stringify(Object.assign({
    kind: 'sr-routing-graph', version: 4, name: '电力示例', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 }, root,
  }, exampleRuntime()));
  const result = RTGraph.parseImport(text);
  assert.equal(result.ok, true);
  for (const node of result.tree.root.nodes) {
    assert.equal(Number.isFinite(node.position.x), true, node.id + '.x');
    assert.equal(Number.isFinite(node.position.y), true, node.id + '.y');
  }
  assert.ok(new Set(result.tree.root.nodes.map(node => node.position.x)).size > 1);
  assert.ok(new Set(result.tree.root.nodes.map(node => node.position.y)).size > 2);

  const again = RTGraph.parseImport(text);
  assert.deepEqual(
    again.tree.root.nodes.map(node => [node.id, node.position]),
    result.tree.root.nodes.map(node => [node.id, node.position]),
  );
});

test('serialize and parseImport round-trip the complete envelope', () => {
  const tree = {
    name: '电力优先决策组',
    timezone: 'Asia/Shanghai',
    sticky: { enabled: true, ttl_seconds: 1800 },
    root: readExample(),
    runtime: exampleRuntime(),
  };
  const text = RTGraph.serialize(tree);
  const exported = JSON.parse(text);
  assert.equal(exported.kind, 'sr-routing-graph');
  assert.equal(exported.version, 4);
  assert.equal(exported.name, tree.name);
  assert.deepEqual(exported.departments, []);
  assert.equal(exported.enabled, false);
  assert.equal(exported.default, false);
  assert.equal(exported.timezone, tree.timezone);
  assert.deepEqual(exported.sticky, tree.sticky);
  assert.equal(exported.root.nodes.length, 14);
  assert.equal(exported.root.edges.length, 14);

  const result = RTGraph.parseImport(text);
  assert.equal(result.ok, true);
  assert.equal(RTGraph.behaviorCanonical(result.tree), RTGraph.behaviorCanonical(tree));
  assert.equal(result.tree.name, tree.name);
  assert.equal(result.tree.timezone, tree.timezone);
  assert.deepEqual(result.tree.sticky, tree.sticky);
  assert.deepEqual(result.tree.root.error_fallback, 'light-target');
  assert.equal(hardIssues(result.issues).length, 0);
  assert.equal(result.issues.filter(issue => issue.level === 'hint').length, 4);

  const rewritten = RTGraph.parseImport(RTGraph.serialize(result.tree));
  assert.equal(RTGraph.behaviorCanonical(rewritten.tree), RTGraph.behaviorCanonical(tree));
});

test('legacy and version-2 imports are rejected with the fixed message', () => {
  const expected = '只接受 sr-routing-graph v4（本期不做旧格式兼容与转换）';
  const payloads = [
    JSON.stringify({ kind: 'sr-routing-graph', version: 2, name: '旧版本', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: '2', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ version: 3, name: '没有 kind', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ kind: 'jev', version: 3, root: { type: 'jev', branches: [] } }),
  ];
  for (const text of payloads) {
    const result = RTGraph.parseImport(text);
    assert.equal(result.ok, false, text);
    assert.equal(result.error, expected, text);
    assert.deepEqual(result.issues, []);
  }

  const malformed = RTGraph.parseImport('{not json');
  assert.equal(malformed.ok, false);
  assert.match(malformed.error, /JSON/);
  assert.equal(RTGraph.parseImport('[]').ok, false);
});

test('kind/version-correct v4 documents with a malformed root report graph_parse_error', () => {
  const payloads = [
    JSON.stringify({ kind: 'sr-routing-graph', version: 4, root: { type: 'jev', branches: [] } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: 4, root: { type: 'graph', nodes: {} } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: 4, name: '没有 root' }),
  ];
  for (const text of payloads) {
    const result = RTGraph.parseImport(text);
    assert.equal(result.ok, false, text);
    assert.notEqual(result.error, RTGraph.REJECT_MESSAGE, text);
    assert.equal(result.issues.length, 1, text);
    const issue = result.issues[0];
    assert.equal(issue.code, 'graph_parse_error', text);
    assert.equal(issue.level, 'hard', text);
    assert.match(issue.zh, /图解析失败/);
    assert.match(issue.en, /Graph parse failed/);
    assert.equal(issue.node_id, '');
    assert.equal(issue.edge_id, '');
    assert.equal(issue.port, '');
  }
});

/* ── 7. 新建 / 删除 / 选项级联 ──────────────────────────────────────── */

test('otherwise_target import is rejected as an unknown field', () => {
  const raw = JSON.parse(RTGraph.serialize(minimalTree()));
  raw.root.otherwise_target = raw.root.error_fallback;
  const result = RTGraph.parseImport(JSON.stringify(raw));
  assert.equal(result.ok, false);
  assert.match(result.error, /未知字段/);
  const issue = result.issues.find(item => item.code === 'graph_parse_error');
  assert.ok(issue);
  assert.match(issue.zh, /graph 含未知字段：otherwise_target/);
  assert.match(issue.en, /graph has unknown fields: otherwise_target/);
});

test('createMinimalTree builds a saveable start-to-target graph', () => {
  const tree = minimalTree();
  const graph = tree.root;
  assert.equal(tree.timezone, 'Asia/Shanghai');
  assert.deepEqual(tree.sticky, { enabled: false, ttl_seconds: 1800 });
  assert.equal(graph.type, 'graph');
  assert.equal(graph.id, 'root');
  assert.equal(graph.nodes.filter(node => node.type === 'start').length, 1);
  const targets = graph.nodes.filter(node => node.type === 'target');
  assert.equal(targets.length, 1);
  assert.equal(targets[0].priority, 1);
  assert.deepEqual(targets[0].models, [{ model: 'qwen3-8b' }]);
  assert.equal('strategy' in targets[0], false);
  assert.equal('capability' in targets[0], false);
  assert.equal(graph.error_fallback, targets[0].id);
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].source, 'start');
  assert.equal(graph.edges[0].source_port, 'out');
  assert.equal(graph.edges[0].condition, null);
  assert.deepEqual(hardIssues(RTGraph.validate(graph)), []);
  assert.equal(RTGraph.changeKind(tree, RTGraph.clone(tree)), 'clean');
});

test('removeNode atomically removes the node, its edges and fallback references', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({
    id: 'q1', type: 'question', question_type: 'noul', instructions: '是否需要？',
    position: { x: 0, y: 0 },
  });
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'q1', target_port: 'in_1',
  }).ok, true);
  const removed = RTGraph.removeNode(graph, 'q1');
  assert.equal(removed.ok, true);
  assert.equal(removed.node.id, 'q1');
  assert.equal(removed.edges.length, 1);
  assert.equal(graph.nodes.some(node => node.id === 'q1'), false);
  assert.equal(graph.edges.some(edge => edge.source === 'q1' || edge.target === 'q1'), false);

  const removedTarget = RTGraph.removeNode(graph, target.id);
  assert.equal(removedTarget.ok, true);
  assert.equal(graph.nodes.some(node => node.id === target.id), false);
  assert.equal(graph.edges.length, 0);
  assert.equal(graph.error_fallback, null);
  assert.ok(removedTarget.fallbackCleared.includes('error_fallback'));

  const missing = RTGraph.removeNode(graph, 'no-such-node');
  assert.equal(missing.ok, false);
  assert.ok(missing.reason);
});

test('removeEdge removes only the targeted edge', () => {
  const graph = minimalTree().root;
  const target = targetOf(graph);
  graph.nodes.push({
    id: 'cond', type: 'condition', source: 'periods', periods: ['peak'],
    position: { x: 0, y: 0 },
  });
  const added = RTGraph.addEdge(graph, {
    source: 'cond', source_port: 'out', target: target.id, target_port: 'in_2',
  });
  assert.equal(RTGraph.removeEdge(graph, added.edge.id).ok, true);
  assert.equal(graph.edges.some(edge => edge.id === added.edge.id), false);
  assert.equal(graph.edges.length, 1);
  assert.equal(RTGraph.removeEdge(graph, added.edge.id).ok, false);
});

test('removeOption cascades selected/prob edges and leaves other ports intact', () => {
  const { graph } = choiceGraph();
  const removed = RTGraph.removeOption(graph, 'capability', 'easy');
  assert.equal(removed.ok, true);
  assert.equal(removed.option.key, 'easy');
  assert.equal(removed.edges.length, 2);
  assert.deepEqual(removed.edges.map(edge => edge.source_port).sort(), ['prob.easy', 'selected.easy']);
  const node = graph.nodes.find(item => item.id === 'capability');
  assert.deepEqual(node.options.map(option => option.key), ['standard', 'strong']);
  assert.ok(graph.edges.every(edge => !/\.easy$/.test(edge.source_port)));
  assert.ok(graph.edges.some(edge => edge.source_port === 'selected.standard'));

  assert.equal(RTGraph.removeOption(graph, 'capability', 'easy').ok, false);
  assert.equal(RTGraph.removeOption(graph, 'no-such-node', 'standard').ok, false);
});

test('renameOptionKey rewrites source ports and keeps edge identity and conditions', () => {
  const { graph } = choiceGraph();
  const beforeIds = graph.edges.map(edge => edge.id);
  const result = RTGraph.renameOptionKey(graph, 'capability', 'easy', 'light');
  assert.equal(result.ok, true);
  assert.equal(result.key, 'light');
  assert.equal(result.edges.length, 2);
  assert.equal(graph.nodes.find(node => node.id === 'capability').options[0].key, 'light');
  assert.ok(graph.edges.some(edge => edge.source_port === 'selected.light'));
  assert.ok(graph.edges.some(edge => edge.source_port === 'prob.light'));
  assert.ok(graph.edges.every(edge => !/\.easy$/.test(edge.source_port)));
  assert.deepEqual(graph.edges.map(edge => edge.id), beforeIds);
  assert.deepEqual(
    graph.edges.find(edge => edge.source_port === 'prob.light').condition,
    { op: 'gt', value: 0.5 },
  );
  assert.equal(RTGraph.portOf(graph.nodes.find(node => node.id === 'capability'), 'selected.light', graph).type, 'boolean');
});

test('renameOptionKey refuses conflicts and invalid keys without mutating the graph', () => {
  const { graph } = choiceGraph();
  const snapshot = RTGraph.clone(graph);

  const conflict = RTGraph.renameOptionKey(graph, 'capability', 'easy', 'standard');
  assert.equal(conflict.ok, false);
  assert.match(conflict.reason, /已存在/);
  assert.deepEqual(graph, snapshot);

  assert.equal(RTGraph.renameOptionKey(graph, 'capability', 'easy', '').ok, false);
  assert.equal(RTGraph.renameOptionKey(graph, 'capability', 'easy', '   ').ok, false);
  assert.equal(RTGraph.renameOptionKey(graph, 'capability', 'easy', 'has space').ok, false);
  assert.equal(RTGraph.renameOptionKey(graph, 'capability', 'nope', 'other').ok, false);
  assert.equal(RTGraph.renameOptionKey(graph, 'no-such-node', 'a', 'b').ok, false);
});

test('renameOptionKey handles port names that collide with Object.prototype properties', () => {
  const { graph, target } = choiceGraph();
  /* 含原型属性名的非法端口边：旧实现用 {} 查表会把 mapping['toString'] 命中
     Object.prototype.toString（truthy），把这条无关边的端口改写成函数。 */
  graph.edges.push({
    id: 'e-prototype-junk',
    source: 'capability',
    source_port: 'toString',
    target: target.id,
    target_port: 'in_5',
    condition: null,
  });
  /* constructor / toString 是 Object.prototype 上的名字：端口映射必须用无原型对象。 */
  const toConstructor = RTGraph.renameOptionKey(graph, 'capability', 'easy', 'constructor');
  assert.equal(toConstructor.ok, true);
  assert.equal(toConstructor.key, 'constructor');
  assert.equal(toConstructor.edges.length, 2, 'the foreign toString edge must not join the rename');
  assert.ok(graph.edges.some(edge => edge.source_port === 'selected.constructor'));
  assert.ok(graph.edges.some(edge => edge.source_port === 'prob.constructor'));
  assert.equal(graph.edges.find(edge => edge.id === 'e-prototype-junk').source_port, 'toString',
    'prototype-named foreign port was rewritten through Object.prototype');
  assert.equal(RTGraph.portOf(graph.nodes.find(node => node.id === 'capability'),
    'selected.constructor', graph).type, 'boolean');

  const toToString = RTGraph.renameOptionKey(graph, 'capability', 'constructor', 'toString');
  assert.equal(toToString.ok, true);
  assert.equal(toToString.key, 'toString');
  assert.equal(toToString.edges.length, 2);
  assert.ok(graph.edges.some(edge => edge.source_port === 'selected.toString'));
  assert.ok(graph.edges.some(edge => edge.source_port === 'prob.toString'));
  assert.ok(graph.edges.every(edge => !/\.constructor$/.test(edge.source_port)));
  assert.equal(graph.edges.find(edge => edge.id === 'e-prototype-junk').source_port, 'toString');

  const back = RTGraph.renameOptionKey(graph, 'capability', 'toString', 'valueOf');
  assert.equal(back.ok, true);
  assert.ok(graph.edges.some(edge => edge.source_port === 'selected.valueOf'));
  assert.ok(graph.edges.some(edge => edge.source_port === 'prob.valueOf'));
  assert.equal(graph.edges.find(edge => edge.id === 'e-prototype-junk').source_port, 'toString');
});

/* ── 8. 行为 canonical / changeKind ─────────────────────────────────── */

test('changeKind separates display fields from behavior fields', () => {
  const before = minimalTree();
  const cases = [];

  const title = RTGraph.clone(before);
  title.root.nodes[1].title = '兜底目标';
  cases.push([title, 'display']);

  /* 已下线的 target 字段不再参与任何 canonical：改它等同于没有改动。 */
  const legacy = RTGraph.clone(before);
  legacy.root.nodes[1].capability = '兜底说明';
  legacy.root.nodes[1].strategy = 'least_inflight';
  cases.push([legacy, 'clean']);

  const renamed = RTGraph.clone(before);
  renamed.name = '另一个名字';
  cases.push([renamed, 'display']);

  const layout = RTGraph.clone(before);
  layout.root.nodes[1].position = { x: 640, y: 480 };
  cases.push([layout, 'layout']);

  // 节点/边数组重排 + 边 id 变化不是行为变化。
  const reordered = RTGraph.clone(before);
  reordered.root.nodes.reverse();
  reordered.root.edges[0].id = 'renamed-edge-id';
  cases.push([reordered, 'clean']);

  const threshold = RTGraph.clone(before);
  threshold.root.edges[0].condition = { op: 'is', value: true };
  cases.push([threshold, 'behavior']);

  const sticky = RTGraph.clone(before);
  sticky.sticky.enabled = true;
  cases.push([sticky, 'behavior']);

  for (const [after, expected] of cases) {
    assert.equal(RTGraph.changeKind(before, after), expected);
  }
  assert.equal(RTGraph.changeKind(before, RTGraph.clone(before)), 'clean');
  assert.equal(RTGraph.behaviorCanonical(before), RTGraph.behaviorCanonical(reordered));
  assert.notEqual(RTGraph.behaviorCanonical(before), RTGraph.behaviorCanonical(threshold));
});

test('behaviorCanonical orders nodes by id and edges by slot number', () => {
  const tree = minimalTree();
  const graph = tree.root;
  graph.nodes.push(
    { id: 'target-0', type: 'target', models: [{ model: 'qwen3-8b' }], priority: 2, position: { x: 0, y: 0 } },
    { id: 'target-2', type: 'target', models: [{ model: 'qwen3-8b' }], priority: 3, position: { x: 0, y: 0 } },
  );
  const slot = (target, port, id) => ({
    id, source: 'start', source_port: 'out', target, target_port: port, condition: null,
  });
  /* 数组顺序故意打乱：target 升序 → 槽位号数值升序（in_10 排在 in_2 之后）。 */
  graph.edges.unshift(
    slot('target-2', 'in_10', 'e-target-2'),
    slot('target-1', 'in_2', 'e-target-1-in-2'),
    slot('target-0', 'in_1', 'e-target-0'),
  );
  const canonical = RTGraph.behaviorCanonical(tree);
  assert.equal(typeof canonical, 'string');
  const payload = JSON.parse(canonical);
  assert.equal(payload.timezone, tree.timezone);
  assert.deepEqual(payload.sticky, { enabled: false, ttl_seconds: 1800 });
  assert.equal(payload.root.type, 'graph');
  assert.deepEqual(payload.root.nodes.map(node => node.id),
    ['start', 'target-0', 'target-1', 'target-2']);
  assert.deepEqual(payload.root.edges.map(edge => edge.target + ':' + edge.target_port),
    ['target-0:in_1', 'target-1:in_1', 'target-1:in_2', 'target-2:in_10'],
    'canonical edges are not sorted by target then numeric slot');
  assert.equal('position' in payload.root.nodes[0], false);
  assert.equal('title' in payload.root.nodes[0], false);
  assert.equal('capability' in payload.root.nodes[2], false);
  assert.equal('id' in payload.root.edges[0], false);

  for (const node of payload.root.nodes) {
    if (node.type === 'target') {
      assert.equal('strategy' in node, false);
      assert.equal('capability' in node, false);
    }
  }
});

test('parseImport output is behavior-stable across coordinate-only round trips', () => {
  const tree = {
    name: '坐标测试', timezone: 'Asia/Shanghai',
    sticky: { enabled: true, ttl_seconds: 600 }, root: readExample(),
    runtime: exampleRuntime(),
  };
  const imported = RTGraph.parseImport(RTGraph.serialize(tree));
  assert.equal(imported.ok, true);
  const moved = RTGraph.clone(imported.tree);
  moved.root.nodes.forEach((node, index) => { node.position = { x: index * 13, y: index * 7 }; });
  assert.equal(RTGraph.changeKind(imported.tree, moved), 'layout');
  assert.equal(RTGraph.behaviorCanonical(imported.tree), RTGraph.behaviorCanonical(moved));
});

/* ── 9. 健壮性回归（Task 9 fix round） ──────────────────────────────── */

test('parseImport validates canvas coordinates instead of coercing null', () => {
  /* 旧格式把坐标写在 node.position；新格式只在顶层 canvas，非法坐标必须拒绝而不是补 0。 */
  const document = JSON.parse(importText(minimalTree()));
  document.root.nodes.forEach(node => { delete node.position; });

  document.canvas = { start: { x: null, y: null } };
  const rejected = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(rejected.ok, false, 'null canvas coordinates were accepted');
  assert.ok(rejected.issues.some(issue => issue.code === 'graph_parse_error'));

  /* 0 是合法坐标：不能被当成缺失而改写成自动布局。 */
  document.canvas = { start: { x: 0, y: 0 } };
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  const start = result.tree.root.nodes.find(node => node.id === 'start');
  assert.deepEqual(start.position, { x: 0, y: 0 });
  for (const node of result.tree.root.nodes) {
    assert.equal(Number.isFinite(node.position.x), true, node.id + '.x');
    assert.equal(Number.isFinite(node.position.y), true, node.id + '.y');
  }
  const laidOut = RTGraph.layoutPositions('dag', result.tree.root);
  assert.deepEqual(Object.keys(laidOut).sort(), result.tree.root.nodes.map(node => node.id).sort());
});

test('parseImport rejects non-object node and edge entries before applying', () => {
  const tree = minimalTree();
  tree.root.nodes = [1];
  tree.root.edges = [1];
  const result = RTGraph.parseImport(importText(tree));
  assert.equal(result.ok, false, 'parseImport accepted non-object node/edge entries');
  assert.match(result.error, /图解析失败/, 'the rejection must use graph_parse_error copy');
  assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error' &&
    issue.level === 'hard'));
  assert.equal('tree' in result, false, 'a rejected import still returned a tree to apply');
});

test('parseImport rejects non-array node fields (models/options/contains_any/periods) before applying', () => {
  const cases = [
    tree => { targetOf(tree.root).models = 5; },
    tree => { tree.root.nodes.push({ id: 'q', type: 'question', question_type: 'choice', instructions: '选。', options: 5 }); },
    tree => { tree.root.nodes.push({ id: 'c', type: 'condition', source: 'text', contains_any: 5 }); },
    tree => { tree.root.nodes.push({ id: 'p', type: 'condition', source: 'periods', periods: 5 }); },
  ];
  for (const mutate of cases) {
    const tree = minimalTree();
    mutate(tree);
    const result = RTGraph.parseImport(importText(tree));
    assert.equal(result.ok, false, 'parseImport accepted a non-array node field');
    assert.match(result.error, /图解析失败/, 'the rejection must use graph_parse_error copy');
    assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error' &&
      issue.level === 'hard'), 'missing hard graph_parse_error issue');
    assert.equal('tree' in result, false, 'a rejected import still returned a tree to apply');
  }

  /* validate 本身也必须报结构错误（服务端 422 数组与本地校验共用同一码）。 */
  const malformed = minimalTree();
  targetOf(malformed.root).models = 5;
  assert.ok(RTGraph.validate(malformed.root).some(issue =>
    issue.code === 'graph_parse_error' && /models/.test(issue.zh)));
});

test('canonical comparison tolerates malformed draft fields', () => {
  const tree = minimalTree();
  tree.root.nodes.push({
    id: 'score', type: 'question', question_type: 'score', instructions: '评分。',
    criteria: 5, position: { x: 0, y: 0 },
  });
  tree.root.nodes.push({
    id: 'choice', type: 'question', question_type: 'choice', instructions: '选择。',
    options: 5, position: { x: 0, y: 0 },
  });
  tree.root.nodes.push({
    id: 'cond', type: 'condition', source: 'text', contains_any: 5, periods: 5,
    position: { x: 0, y: 0 },
  });
  targetOf(tree.root).models = 5;

  assert.doesNotThrow(() => RTGraph.behaviorCanonical(tree));
  assert.equal(RTGraph.changeKind(tree, RTGraph.clone(tree)), 'clean');
  assert.ok(RTGraph.validate(tree).some(issue => issue.code === 'score_criteria_invalid'));
});

test('prototype-named ids and option keys are not treated as duplicates', () => {
  const graph = minimalTree().root;
  graph.nodes.push(
    { id: 'constructor', type: 'condition', source: 'text', contains_any: ['抢修'], position: { x: 0, y: 0 } },
    { id: 'other', type: 'condition', source: 'text', contains_any: ['故障'], position: { x: 0, y: 0 } },
  );
  assert.equal(
    RTGraph.validate(graph).some(issue => issue.code === 'duplicate_node_id'),
    false,
    'constructor / other must not collide',
  );

  const optionGraph = minimalTree().root;
  optionGraph.nodes.push({
    id: 'choice', type: 'question', question_type: 'choice', instructions: '选择。',
    options: [
      { key: 'constructor', label: '构造', capability: '说明' },
      { key: 'other', label: '其他', capability: '说明' },
    ],
    position: { x: 0, y: 0 },
  });
  const optionIssues = RTGraph.validate(optionGraph);
  assert.equal(
    optionIssues.some(issue => issue.code === 'question_incomplete' && /选项 key 重复/.test(issue.zh)),
    false,
    'constructor option key must not collide',
  );
});

test('edge_cycle issues carry the offending edge id and slot', () => {
  const graph = minimalTree().root;
  graph.nodes.push({ id: 'gate', type: 'logic', operator: 'and', position: { x: 0, y: 0 } });
  graph.edges.push(
    { id: 'e-start-gate-in_1', source: 'start', source_port: 'out', target: 'gate', target_port: 'in_1', condition: null },
    { id: 'e-gate-gate-in_2', source: 'gate', source_port: 'out', target: 'gate', target_port: 'in_2', condition: null },
  );
  const cycle = RTGraph.validate(graph).filter(issue => issue.code === 'edge_cycle');
  assert.equal(cycle.length, 1);
  assert.equal(cycle[0].node_id, 'gate');
  assert.equal(cycle[0].edge_id, 'e-gate-gate-in_2');
  assert.equal(cycle[0].port, 'in_2');
});

test('serialize round-trip preserves positions and titles, dropping removed target fields', () => {
  const tree = minimalTree();
  const [start, target] = tree.root.nodes;
  start.title = '入口';
  start.position = { x: 41, y: 52 };
  target.title = '兜底目标';
  target.capability = '兜底说明';
  target.position = { x: 611, y: 72 };

  const result = RTGraph.parseImport(RTGraph.serialize(tree));
  assert.equal(result.ok, true);
  const importedStart = result.tree.root.nodes.find(node => node.id === 'start');
  const importedTarget = result.tree.root.nodes.find(node => node.id === 'target-1');
  assert.deepEqual(importedStart.position, { x: 41, y: 52 });
  assert.equal(importedStart.title, '入口');
  assert.deepEqual(importedTarget.position, { x: 611, y: 72 });
  assert.equal(importedTarget.title, '兜底目标');
  assert.equal('capability' in importedTarget, false);
  assert.equal('strategy' in importedTarget, false);
  assert.equal(RTGraph.changeKind(tree, result.tree), 'clean');
});

test('invalid but present positions are not accepted as coordinates', () => {
  const tree = minimalTree();
  const [start, target] = tree.root.nodes;
  start.position = { x: NaN, y: 5 };
  target.position = { x: '', y: '' };

  const text = RTGraph.serialize(tree);
  assert.equal(text.includes('NaN'), false);
  const exported = JSON.parse(text);
  assert.equal('position' in exported.root.nodes[0], false);
  assert.equal('position' in exported.root.nodes[1], false);

  const result = RTGraph.parseImport(text);
  assert.equal(result.ok, true);
  for (const node of result.tree.root.nodes) {
    assert.equal(Number.isFinite(node.position.x), true, node.id + '.x');
    assert.equal(Number.isFinite(node.position.y), true, node.id + '.y');
    assert.notEqual(node.position.x, 0, node.id + '.x must not be coerced');
    assert.notEqual(node.position.y, 0, node.id + '.y must not be coerced');
  }
});

test('validate reports duplicate target priorities', () => {
  const graph = minimalTree().root;
  graph.nodes.push({
    id: 'target-2', type: 'target', models: [{ model: 'qwen3-32b' }], priority: 1,
    position: { x: 0, y: 0 },
  });
  const issues = RTGraph.validate(graph).filter(issue => issue.code === 'target_priority_invalid');
  assert.ok(issues.some(issue => issue.node_id === 'target-2' && /唯一/.test(issue.zh)));
});

test('validate enforces limit boundaries exactly (200/201 nodes, 400/401 edges, 32/33 questions)', () => {
  const limitIssues = (graph, pattern) =>
    RTGraph.validate(graph).filter(issue => issue.code === 'limit_exceeded' && pattern.test(issue.zh));

  const withNodes = count => {
    const graph = minimalTree().root;
    for (let index = graph.nodes.length; index < count; index += 1) {
      graph.nodes.push({
        id: 'filler-node-' + index, type: 'condition', source: 'text',
        contains_any: ['故障'], position: { x: 0, y: 0 },
      });
    }
    assert.equal(graph.nodes.length, count);
    return graph;
  };
  assert.equal(withNodes(RTGraph.LIMITS.nodes).nodes.length, 200);
  assert.equal(limitIssues(withNodes(RTGraph.LIMITS.nodes), /节点数超过上限/).length, 0,
    'exactly 200 nodes must be allowed');
  assert.equal(limitIssues(withNodes(RTGraph.LIMITS.nodes + 1), /节点数超过上限/).length, 1,
    '201 nodes must be flagged');

  const withEdges = count => {
    const graph = minimalTree().root;
    while (graph.edges.length < count) {
      graph.edges.push({
        id: 'filler-edge-' + graph.edges.length, source: 'start', source_port: 'out',
        target: 'target-1', target_port: 'in_' + (graph.edges.length + 1), condition: null,
      });
    }
    assert.equal(graph.edges.length, count);
    return graph;
  };
  assert.equal(withEdges(RTGraph.LIMITS.edges).edges.length, 400);
  assert.equal(limitIssues(withEdges(RTGraph.LIMITS.edges), /边数超过上限/).length, 0,
    'exactly 400 edges must be allowed');
  assert.equal(limitIssues(withEdges(RTGraph.LIMITS.edges + 1), /边数超过上限/).length, 1,
    '401 edges must be flagged');

  const withQuestions = count => {
    const graph = minimalTree().root;
    for (let index = 0; index < count; index += 1) {
      graph.nodes.push({
        id: 'question-' + index, type: 'question', question_type: 'noul',
        instructions: '是否需要？', position: { x: 0, y: 0 },
      });
    }
    return graph;
  };
  assert.equal(withQuestions(RTGraph.LIMITS.questions).nodes.filter(node => node.type === 'question').length, 32);
  assert.equal(limitIssues(withQuestions(RTGraph.LIMITS.questions), /问题节点超过上限/).length, 0,
    'exactly 32 questions must be allowed');
  assert.equal(limitIssues(withQuestions(RTGraph.LIMITS.questions + 1), /问题节点超过上限/).length, 1,
    '33 questions must be flagged');
});

test('validate allows an 8-wave question chain and flags a 9th wave', () => {
  const chainGraph = count => {
    const graph = minimalTree().root;
    graph.nodes = [graph.nodes.find(node => node.type === 'start')];
    graph.edges = [];
    let source = { node: 'start', port: 'out' };
    for (let index = 1; index <= count; index += 1) {
      const id = 'q' + index;
      graph.nodes.push({
        id, type: 'question', question_type: 'choice',
        instructions: '第 ' + index + ' 波问题。',
        options: [
          { key: 'easy', label: '轻量', capability: '简单问答' },
          { key: 'strong', label: '增强', capability: '复杂分析' },
        ],
        position: { x: 0, y: 0 },
      });
      graph.edges.push({
        id: 'e-in-' + index, source: source.node, source_port: source.port,
        target: id, target_port: 'in_1', condition: null,
      });
      source = { node: id, port: 'selected.easy' };
    }
    return graph;
  };
  const chainIssues = graph =>
    RTGraph.validate(graph).filter(issue => issue.code === 'limit_exceeded' && /问题链/.test(issue.zh));
  assert.deepEqual(chainIssues(chainGraph(RTGraph.LIMITS.waves)), [],
    'exactly 8 question waves must be allowed');
  const tooDeep = chainIssues(chainGraph(RTGraph.LIMITS.waves + 1));
  assert.equal(tooDeep.length, 1, 'a 9-wave chain must be flagged');
  assert.match(tooDeep[0].zh, /9 波/);
});

/* ── 10. 计算日志 / 时间路径静态契约（Task 14） ─────────────────────── */

test('logs render presenter fields and keep no legacy adapters', () => {
  const read = name => fs.readFileSync(new URL('../router/console/' + name, import.meta.url), 'utf8');
  const logs = read('logs.js');
  assert.match(logs, /\['error_fallback', 'fallback', 'otherwise'\]/,
    'logs fallback filter does not know the graph fallback enum');
  assert.doesNotMatch(logs, /selection_reason === 'fallback'/,
    'logs still uses the pre-graph fallback enum');
  /* Task 7：展示字段全部由 API presenter 生成，控制台不再适配旧事件。 */
  assert.match(logs, /record\.route_label/, 'logs does not render the presenter route label');
  assert.match(logs, /event\.phase/, 'logs does not consume presenter phases');
  assert.doesNotMatch(logs, /const LEGACY/, 'logs still carries the legacy event table');
  assert.doesNotMatch(logs, /function legacy\(/, 'logs still carries the legacy adapter');
  assert.doesNotMatch(logs, /legacyFields|legacyRoute/, 'logs still carries legacy field adapters');
  const i18n = read('i18n.js');
  for (const prefix of ['logs.legacy.', 'logs.ev.', 'logs.reason.']) {
    assert.equal(i18n.split("'" + prefix).length - 1, 0,
      'removed i18n namespace still present: ' + prefix);
  }
  assert.equal(i18n.split("'logs.sticky.unavailable'").length - 1, 2,
    'scheduler sticky unavailable status has no bilingual copy');
});

test('calculation log detail shows the process inline and keeps technical facts folded', () => {
  const read = name => fs.readFileSync(new URL('../router/console/' + name, import.meta.url), 'utf8');
  const logs = read('logs.js');

  /* 计算日志：详情按 trace 事件分阶段内联渲染，不再交给 SRTrace 的旧分节渲染器。 */
  assert.match(logs, /function stages\(record\)/);
  assert.match(logs, /path\.length \? path\.map\(stageHtml\)\.join/);
  assert.match(logs, /class="lg-stage /);
  assert.doesNotMatch(logs, /lg-more--trace/);
  assert.doesNotMatch(logs, /logs\.sec\.probs/);
  assert.match(logs, /routing_error/);
  /* 技术事实折叠在 <details> 里，不展开时不占位。 */
  assert.match(logs, /<details class="lg-raw">/);
  assert.doesNotMatch(logs, /SRTrace\./);
});

function attachQuestion(tree, questionType) {
  tree.root.nodes.push({
    id: 'q-review', type: 'question', question_type: questionType,
    instructions: '审查导入契约',
  });
  tree.root.edges.push({
    id: 'e-start-q-review', source: 'start', source_port: 'out',
    target: 'q-review', target_port: 'in_1', condition: null,
  });
}

test('review-import-rejects-unsupported-question-type', () => {
  const tree = minimalTree();
  attachQuestion(tree, 'unsupported');
  const result = RTGraph.parseImport(importText(tree));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error'));
});

test('review-import-rejects-unknown-fields-at-every-contract-layer', () => {
  const cases = [
    tree => { tree.root.unexpected = true; },
    tree => { tree.root.nodes[0].legacy = true; },
    tree => { tree.root.edges[0].legacy = true; },
    tree => { targetOf(tree.root).models[0].upstream = 'legacy-upstream'; },
    tree => { tree.sticky.legacy = true; },
  ];
  for (const mutate of cases) {
    const tree = minimalTree();
    mutate(tree);
    const result = RTGraph.parseImport(importText(tree));
    assert.equal(result.ok, false);
    assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error'));
  }
  /* 顶层 canvas 同样是契约层：未知坐标键必须拒绝。 */
  const withCanvas = JSON.parse(importText(minimalTree()));
  withCanvas.canvas.start.z = 3;
  const canvasResult = RTGraph.parseImport(JSON.stringify(withCanvas));
  assert.equal(canvasResult.ok, false);
  assert.ok(canvasResult.issues.some(issue => issue.code === 'graph_parse_error'));
  const text = importText(minimalTree());
  const envelope = JSON.parse(text);
  envelope.legacy = true;
  const result = RTGraph.parseImport(JSON.stringify(envelope));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error'));
});

test('review-import-rejects-missing-required-envelope-fields', () => {
  for (const field of ['name', 'timezone', 'sticky', 'root']) {
    const envelope = JSON.parse(importText(minimalTree()));
    delete envelope[field];
    const result = RTGraph.parseImport(JSON.stringify(envelope));
    assert.equal(result.ok, false, field);
    assert.equal(result.issues.length, 1, field);
    assert.equal(result.issues[0].code, 'graph_parse_error', field);
    assert.match(result.issues[0].zh, /图解析失败/, field);
    assert.match(result.issues[0].en, /Graph parse failed/, field);
  }
});

test('review-import-rejects-invalid-envelope-timezone', () => {
  const tree = minimalTree();
  tree.timezone = 'Mars/Olympus';
  const result = RTGraph.parseImport(importText(tree));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(issue => issue.code === 'invalid_timezone'));
});

test('review-import-keeps-valid-v4-example-accepted', () => {
  /* 示例文件本身就是完整 v4 文档（画布坐标在顶层 canvas），直接导入验证契约。 */
  const result = RTGraph.parseImport(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.equal(hardIssues(result.issues).length, 0);
});

test('review-slot-cap-rejects-in-9999-without-materializing-rows', () => {
  const tree = minimalTree();
  const target = targetOf(tree.root);
  tree.root.edges[0].target_port = 'in_9999';

  const issues = hardIssues(RTGraph.validate(tree.root));
  assert.ok(issues.some(issue => issue.code === 'edge_port_missing' && issue.port === 'in_9999'));
  assert.equal(RTGraph.portsOf(target, tree.root).length, 1);
  assert.equal(RTGraph.portOf(target, 'in_9999', tree.root), null);

  tree.root.edges[0].target_port = 'in_400';
  const validIssues = hardIssues(RTGraph.validate(tree.root));
  assert.equal(validIssues.some(issue => issue.code === 'edge_port_missing'), false);
  assert.equal(RTGraph.portsOf(target, tree.root).length, 400);
  assert.ok(RTGraph.portOf(target, 'in_400', tree.root));
  assert.equal(RTGraph.portOf(target, 'in_401', tree.root), null);
});

test('review-next-slot-uses-in-400-then-reports-exhaustion', () => {
  const tree = minimalTree();
  const target = targetOf(tree.root);
  tree.root.edges = Array.from({ length: 399 }, (_, index) => ({
    id: 'e-' + (index + 1), source: 'start', source_port: 'out',
    target: target.id, target_port: 'in_' + (index + 1), condition: null,
  }));
  assert.equal(RTGraph.nextSlot(tree.root, target.id), 'in_400');
  tree.root.edges.push({
    id: 'e-400', source: 'start', source_port: 'out',
    target: target.id, target_port: 'in_400', condition: null,
  });
  assert.equal(RTGraph.nextSlot(tree.root, target.id), null);
});

test('review-import-rejects-invalid-envelope-values-before-normalization', () => {
  const cases = [
    ['sticky-enabled-string', tree => { tree.sticky.enabled = 'yes'; }],
    ['sticky-ttl-string', tree => { tree.sticky.ttl_seconds = 'bad'; }],
    ['sticky-ttl-zero', tree => { tree.sticky.ttl_seconds = 0; }],
    ['sticky-array', tree => { tree.sticky = []; }],
    ['timezone-number', tree => { tree.timezone = 123; }],
    ['name-number', tree => { tree.name = 123; }],
  ];
  for (const [name, mutate] of cases) {
    const tree = minimalTree();
    mutate(tree);
    const result = RTGraph.parseImport(importText(tree));
    assert.equal(result.ok, false, name + ' was accepted');
    assert.ok(result.issues.some(issue => issue.code === 'graph_parse_error'),
      name + ' did not return graph_parse_error: ' + JSON.stringify(result.issues));
    assert.equal(result.tree, undefined, name + ' returned a normalized tree after rejection');
  }

  const valid = minimalTree();
  valid.name = '合法 envelope';
  valid.timezone = 'UTC';
  valid.sticky = { enabled: true, ttl_seconds: 60 };
  const accepted = RTGraph.parseImport(importText(valid));
  assert.equal(accepted.ok, true, JSON.stringify(accepted.issues));
  assert.equal(accepted.tree.name, '合法 envelope');
  assert.equal(accepted.tree.timezone, 'UTC');
  assert.deepEqual(accepted.tree.sticky, { enabled: true, ttl_seconds: 60 });
});

test('review-chinese-node-names-contract', () => {
  const read = name => fs.readFileSync(new URL('../router/console/' + name, import.meta.url), 'utf8');
  const cards = read('routing/cards.js');
  assert.match(cards, /function nodeName\(node, graph\)/, 'cards.js lost the Chinese node names');
  assert.match(cards, /^\s*nodeName,$/m, 'cards.js does not export nodeName');
  assert.match(cards, /cv\.name\.question\./, 'question cards do not use the Chinese name labels');
  /* ESM：inspector / tryrun 直接从 cards.js 导入 nodeName；condpop 由
     configureCondPop({ nodeName }) 注入，避免与 cards.js 形成循环依赖。 */
  /* 断言 import 语句而不是裸名字：纯格式化（换行/引号/空格）不应让用例误红。 */
  assert.match(read('routing/inspector.js'),
    /import\s*\{[^}]*\bnodeName\b[^}]*\}\s*from\s*['"]\.\/cards\.js['"]/,
    'routing/inspector.js does not import nodeName from cards.js');
  /* tryrun 的本地别名可以任意命名：断言「从 cards.js 导入了 nodeName」+「真的调用了
     这个本地名」，不钉死 alias 名字或单行格式（纯重命名/换行不应让用例误红）。 */
  const tryrun = read('routing/tryrun.js');
  const tryrunCards = tryrun.match(/import\s*\{([^}]*)\}\s*from\s*['"]\.\/cards\.js['"]/);
  assert.ok(tryrunCards, 'routing/tryrun.js does not import from cards.js');
  const tryrunAlias = tryrunCards[1].match(/\bnodeName\s+as\s+([A-Za-z_$][\w$]*)/);
  const tryrunNodeName = tryrunAlias
    ? tryrunAlias[1]
    : (/\bnodeName\b/.test(tryrunCards[1]) ? 'nodeName' : null);
  assert.ok(tryrunNodeName, 'routing/tryrun.js does not import nodeName from cards.js');
  assert.match(tryrun, new RegExp('(?:^|[^\\w$])' +
    tryrunNodeName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\('),
  'routing/tryrun.js does not call the node-name helper it imported');
  /* 必须是调用点，不能靠 import/定义本身蒙混过关（定义是 `const nodeNameOf = (`）。 */
  assert.match(read('routing/condpop.js'), /\bnodeNameOf\s*\(/,
    'routing/condpop.js does not call the injected node-name helper');
  assert.match(read('routing/app.js'),
    /configureCondPop\(\{[^}]*\bnodeName:\s*nodeName\b[^}]*\}\)/,
    'app.js does not inject nodeName into configureCondPop');
  /* 卡片不再渲染 OR 说明（说明仅保留在检查器里）。 */
  assert.doesNotMatch(cards, /cv\.slot\.orNote/, 'card still renders the OR note');
  const i18n = read('i18n.js');
  for (const key of ['cv.name.start', 'cv.name.question.choice', 'cv.name.question.noul',
    'cv.name.question.score', 'cv.name.logic.and', 'cv.name.logic.or',
    'cv.name.condition.text', 'cv.name.condition.periods', 'cv.name.target']) {
    assert.equal(i18n.split("'" + key + "'").length - 1, 2,
      'i18n key ' + key + ' is not defined in both zh and en');
  }
});

/* ── 11. 计算日志纯视图模型（Task 4：SRTrace.build / label / hasJev） ─ */

test('calculation log model groups real execution order without dropping slots', () => {
  const trace = [
    { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
    { event: 'window', tokens_before: 20, tokens_after: 20, truncated: false },
    { event: 'condition', node_id: 'c', source: 'text', matched: ['抢修'], value: true },
    { event: 'slot', wave: 0, node_id: 'q', port: 'in_1', source: 'start', source_port: 'out', raw: true, condition: null, status: 'true' },
    { event: 'activate', wave: 0, node_id: 'q', via_port: 'in_1' },
    { event: 'wave', wave: 1, questions: ['q'], duration_ms: 12, failed: false, error: null },
    { event: 'question', wave: 1, node_id: 'q', ports: { value: 7.5 }, duration_ms: 12, usage: { input_tokens: 20 } },
    { event: 'slot', wave: 1, node_id: 't', port: 'in_1', source: 'q', source_port: 'value', raw: 7.5, condition: { op: 'gt', value: 5 }, status: 'true' },
    { event: 'activate', wave: 1, node_id: 't', via_port: 'in_1' },
    { event: 'final', target_id: 't', kind: 'target', priority: 1, candidates: [{ target_id: 't', priority: 1, via_node: 'q', via_port: 'in_1', wave: 1 }] },
    { event: 'schedule', target_id: 't', configured_models: ['fast'], registered_models: ['fast'], available_models: ['fast'], unavailable: {}, inflight: { fast: 0 }, sticky: { status: 'disabled', preferred_model: null } },
    { event: 'scheduled', model: 'fast', reason: 'target', dry_run: false },
  ];
  const model = SRTrace.build(trace, { q: { title: '严重度', type: 'question' } });

  assert.equal(model.input.time.timezone, 'Asia/Shanghai');
  assert.equal(model.phases.length, 2);
  assert.equal(model.phases[0].wave, 0);
  assert.equal(model.phases[1].wave, 1);
  assert.equal(model.phases[0].slots.length, 1);
  assert.equal(model.phases[1].slots[0].raw, 7.5);
  assert.equal(model.phases[1].questions[0].ports.value, 7.5);
  assert.equal(model.final.candidates.length, 1);
  assert.equal(model.schedule.sticky.status, 'disabled');
  assert.equal(model.scheduled.model, 'fast');
  assert.equal(SRTrace.label({ q: { title: '严重度' } }, 'q'), '严重度');
  assert.equal(SRTrace.label({}, 'q'), 'q');
});

test('calculation log JEV filter includes noul score and failed waves', () => {
  assert.equal(SRTrace.hasJev({ trace: [{ event: 'question', ports: { yes: 0.8, no: 0.2 } }] }), true);
  assert.equal(SRTrace.hasJev({ trace: [{ event: 'question', ports: { value: 4 } }] }), true);
  assert.equal(SRTrace.hasJev({ trace: [{ event: 'wave', failed: true }] }), true);
  assert.equal(SRTrace.hasJev({ waves: 1 }), true);
  assert.equal(SRTrace.hasJev({ trace: [{ event: 'condition', value: true }] }), false);
});

test('calculation log model keeps failures separate from final selection', () => {
  const model = SRTrace.build([
    { event: 'final', target_id: 'safe', kind: 'error_fallback', candidates: [], priority: 1 },
    { event: 'routing_error', stage: 'schedule', error_class: 'no_available_backend', message: 'none' },
  ], {});
  assert.equal(model.final.kind, 'error_fallback');
  assert.equal(model.error.error_class, 'no_available_backend');
});

test('calculation log model distinguishes failed waves from normal no-candidate fallback', () => {
  const failed = SRTrace.build([
    { event: 'wave', wave: 1, questions: ['q'], duration_ms: 5, failed: true, error: 'timeout' },
    { event: 'fallback', target_id: 'safe', reason: 'timeout' },
    { event: 'final', target_id: 'safe', kind: 'error_fallback', candidates: [], priority: 1 },
  ], {});
  const otherwise = SRTrace.build([
    { event: 'final', target_id: 'safe', kind: 'otherwise', candidates: [], priority: 1 },
  ], {});

  assert.equal(failed.phases[0].waveEvent.failed, true);
  assert.equal(failed.fallback.reason, 'timeout');
  assert.equal(failed.final.kind, 'error_fallback');
  assert.equal(otherwise.fallback, null);
  assert.equal(otherwise.final.kind, 'otherwise');
});

/* ── 12. 画布坐标边界：顶层 canvas ⇄ 内部 node.position（Task 3） ──── */

test('import dispatches top-level canvas into node positions', () => {
  const document = Object.assign({
    kind: 'sr-routing-graph', version: 4, name: '导入', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: 't1',
      nodes: [
        { id: 'start', type: 'start' },
        { id: 't1', type: 'target', models: [{ model: 'fast' }], priority: 1 }
      ],
      edges: []
    },
    canvas: { start: { x: 11, y: 22 } }
  }, documentRuntime(['fast']));
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  const start = result.tree.root.nodes.find(node => node.id === 'start');
  assert.deepEqual(start.position, { x: 11, y: 22 });
});

test('export collects node positions into a top-level canvas', () => {
  const tree = Object.assign({
    name: '导出', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: null,
      nodes: [
        { id: 'start', type: 'start', position: { x: 3, y: 4 } },
        { id: 't1', type: 'target', models: [{ model: 'fast' }], priority: 1 }
      ],
      edges: []
    }
  }, { runtime: documentRuntime(['fast']) });
  const document = JSON.parse(RTGraph.serialize(tree));
  assert.deepEqual(document.canvas, { start: { x: 3, y: 4 } });
  for (const node of document.root.nodes) assert.equal('position' in node, false);
});

test('import rejects a node-level position field', () => {
  const document = Object.assign({
    kind: 'sr-routing-graph', version: 4, name: '', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph', error_fallback: null,
      nodes: [{ id: 'start', type: 'start', position: { x: 1, y: 2 } }],
      edges: []
    }
  }, documentRuntime(['qwen3-8b']));
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(item => item.code === 'graph_parse_error'));
});

test('invalid canvas shapes are rejected on import', () => {
  const base = Object.assign({
    kind: 'sr-routing-graph', version: 4, name: '', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: { id: 'root', type: 'graph', error_fallback: null,
      nodes: [{ id: 'start', type: 'start' }], edges: [] }
  }, documentRuntime(['qwen3-8b']));
  for (const canvas of [[], { ghost: { x: 1, y: 2 } }, { start: { x: 1 } }]) {
    const result = RTGraph.parseImport(JSON.stringify(Object.assign({}, base, { canvas })));
    assert.equal(result.ok, false, JSON.stringify(canvas));
  }
});

/* ── 10. 自包含运行配置（backends / jev / jev_window / time_periods） ───
   四字段是画布 JSON 的运行配置事实源：Python 端 runtime.py 已完成，
   这里钉住控制台数据层的导入/导出/严格校验/行为 canonical 语义。 */

function selfContainedGraph() {
  return {
    id: 'root', type: 'graph', error_fallback: 'fast-target',
    nodes: [
      { id: 'start', type: 'start' },
      { id: 'fast-target', type: 'target', models: [{ model: 'fast', reasoning: 'high' }], priority: 1 },
      { id: 'strong-target', type: 'target', models: [{ model: 'strong', reasoning: null }], priority: 2 }
    ],
    edges: [
      { id: 'e-start-fast', source: 'start', source_port: 'out', target: 'fast-target', target_port: 'in_1', condition: null },
      { id: 'e-start-strong', source: 'start', source_port: 'out', target: 'strong-target', target_port: 'in_1', condition: null }
    ]
  };
}

/* 故意混入需要归一化的写法：provider 名/base_url 带空白与尾部斜杠、
   缺 key_env / reasoning_format、jev 缺 timeout/instructions/state_template、
   capability 带空白、profile 里放整数值数字（131072）与指数浮点（1e-5）。 */
function selfContainedDocument() {
  return {
    kind: 'sr-routing-graph', version: 4, name: '自包含决策组', departments: [], enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: true, ttl_seconds: 600 },
    backends: {
      providers: [
        { name: ' local ', base_url: ' http://runtime.test/v1/ ', key_env: 'RUNTIME_API_KEY', reasoning_format: 'openai' },
        { name: 'backup', base_url: 'https://backup.test/v1' }
      ],
      models: [
        {
          id: 'fast', upstream: 'fast-upstream', provider: 'local',
          profile: { capability: '  轻量  ', context_window: 131072, rate: 1e-5 }
        },
        {
          id: 'strong', upstream: 'strong-upstream', provider: 'backup',
          profile: { reasoning: { efforts: { off: null, high: 'high' }, default: 'off' } }
        }
      ]
    },
    jev: { id: 'jev-test', base_url: 'http://jev.test/v1', model: 'jev-model' },
    jev_window: { input_tokens: 1024, context_tokens: 4096 },
    time_periods: [
      { name: 'peak', days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' },
      { name: 'night', days: [5, 6], start: '22:00', end: '06:00' }
    ],
    root: selfContainedGraph(),
    canvas: {
      start: { x: 40, y: 180 },
      'fast-target': { x: 320, y: 120 },
      'strong-target': { x: 320, y: 260 }
    }
  };
}

function expectedRuntime() {
  const defaults = RTGraph.RUNTIME_DEFAULTS;
  return {
    backends: {
      providers: [
        { name: 'local', base_url: 'http://runtime.test/v1', key_env: 'RUNTIME_API_KEY', reasoning_format: 'openai' },
        { name: 'backup', base_url: 'https://backup.test/v1', key_env: '', reasoning_format: 'openai' }
      ],
      models: [
        {
          id: 'fast', upstream: 'fast-upstream', provider: 'local',
          profile: { capability: '轻量', context_window: 131072, rate: 1e-5 }
        },
        {
          id: 'strong', upstream: 'strong-upstream', provider: 'backup',
          profile: { reasoning: { efforts: { off: null, high: 'high' }, default: 'off' } }
        }
      ]
    },
    jev: {
      id: 'jev-test', base_url: 'http://jev.test/v1', model: 'jev-model', key_env: '',
      timeout_seconds: 10,
      instructions: defaults.instructions, state_template: defaults.state_template
    },
    jev_window: { input_tokens: 1024, context_tokens: 4096 },
    time_periods: [
      { name: 'peak', days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' },
      { name: 'night', days: [5, 6], start: '22:00', end: '06:00' }
    ]
  };
}

function importSelfContained() {
  const result = RTGraph.parseImport(JSON.stringify(selfContainedDocument()));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  return result;
}

function assertRuntimeRejected(mutate, label) {
  const document = selfContainedDocument();
  mutate(document);
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, false, label + ' was accepted');
  assert.equal(result.tree, undefined, label + ' returned a partial tree');
  assert.ok(result.issues.some(item => item.code === 'graph_parse_error'),
    label + ' did not report graph_parse_error: ' + JSON.stringify(result.issues));
}

test('self-contained documents import, normalize defaults and round-trip the runtime', () => {
  const result = importSelfContained();
  assert.deepEqual(result.tree.runtime, expectedRuntime());
  assert.equal(result.tree.name, '自包含决策组');
  assert.equal(hardIssues(result.issues).length, 0);
  assert.equal(RTGraph.hasRuntime(result.tree), true);
  /* 严格契约：新草稿同样携带完整运行快照，没有「无 runtime」的第二条路径。 */
  assert.equal(RTGraph.hasRuntime(minimalTree()), true);
  assert.deepEqual(Object.keys(minimalTree().runtime).sort(),
    ['backends', 'jev', 'jev_window', 'time_periods']);

  const text = RTGraph.serialize(result.tree);
  const document = JSON.parse(text);
  assert.equal(document.kind, 'sr-routing-graph');
  assert.deepEqual(document.backends, expectedRuntime().backends);
  assert.deepEqual(document.jev, expectedRuntime().jev);
  assert.deepEqual(document.jev_window, expectedRuntime().jev_window);
  assert.deepEqual(document.time_periods, expectedRuntime().time_periods);
  /* 导出文档不含任何密钥/派生字段的键名。 */
  for (const secret of ['api_key', 'key_present', 'key_source']) {
    assert.equal(text.includes(secret), false, 'serialize leaked ' + secret);
  }

  const rewritten = RTGraph.parseImport(text);
  assert.equal(rewritten.ok, true, JSON.stringify(rewritten.issues));
  assert.deepEqual(rewritten.tree.runtime, expectedRuntime());
  assert.equal(RTGraph.behaviorCanonical(rewritten.tree), RTGraph.behaviorCanonical(result.tree));

  /* 导出/保存 payload 用的投影：tree 与 runtime 对象两种入参等价。 */
  assert.deepEqual(RTGraph.runtimeDocumentView(result.tree), expectedRuntime());
  assert.deepEqual(RTGraph.runtimeDocumentView(result.tree.runtime), expectedRuntime());
  assert.deepEqual(RTGraph.runtimeDocumentView(minimalTree()), minimalTree().runtime);
});

test('serialize refuses tampered runtime secrets and never exports key material', () => {
  const imported = importSelfContained();
  /* 严格导出：运行配置里的密钥/派生密钥字段是解析非法态，serialize 直接拒绝，
     而不是把内部被污染的草稿「洗一遍」后继续导出（写路径同样会先被拒绝）。 */
  const tampered = RTGraph.clone(imported.tree);
  tampered.runtime.backends.providers[0].api_key = 'sk-should-not-be-exported';
  tampered.runtime.backends.providers[0].key_source = 'stored';
  tampered.runtime.jev.api_key = 'sk-should-not-be-exported';
  tampered.runtime.jev.key_present = true;
  assert.throws(() => RTGraph.serialize(tampered),
    /api_key|key_source|未知字段/, 'serialize exported a runtime carrying secret fields');

  /* 合法文档的导出文本只含四字段快照，不含任何密钥/派生字段键名。 */
  const text = RTGraph.serialize(imported.tree);
  const runtime = JSON.parse(text);
  for (const secret of ['api_key', 'key_present', 'key_source', 'authorization']) {
    assert.equal(text.includes(secret), false, 'serialize leaked ' + secret);
  }
  assert.deepEqual(runtime.backends.providers, expectedRuntime().backends.providers);
  assert.deepEqual(runtime.backends.models[0].id, expectedRuntime().backends.models[0].id);
  assert.deepEqual(runtime.jev, expectedRuntime().jev);

  /* 保存/试算 payload 的投影边界仍然脱敏：profile 里嵌套的密钥元数据被剔除。 */
  const metadata = RTGraph.clone(imported.tree);
  metadata.runtime.backends.models[0].profile.metadata = {
    labels: [{ 'API-KEY': 'sk-should-not-be-exported', tier: 'keep-me' }],
    authorization: 'sk-should-not-be-exported'
  };
  const view = RTGraph.runtimeDocumentView(metadata);
  assert.deepEqual(view.backends.models[0].profile.metadata, { labels: [{ tier: 'keep-me' }] });
  assert.equal(JSON.stringify(view).includes('sk-should-not-be-exported'), false);
});

test('self-contained documents accept a null jev block', () => {
  const document = selfContainedDocument();
  document.jev = null;
  const result = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.equal(result.tree.runtime.jev, null);
  const exported = JSON.parse(RTGraph.serialize(result.tree));
  assert.equal(Object.prototype.hasOwnProperty.call(exported, 'jev'), true);
  assert.equal(exported.jev, null);
  const rewritten = RTGraph.parseImport(RTGraph.serialize(result.tree));
  assert.deepEqual(rewritten.tree.runtime, result.tree.runtime);
});

test('legacy documents without the four runtime fields are rejected', () => {
  /* 旧文档（去掉四字段的同一份图）现在必须在 parseImport 阶段整体拒绝，
     不再有「无 runtime 文档 + 运行时从数据库补齐」的兼容分支。 */
  const legacyDocument = JSON.parse(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  for (const field of RTGraph.RUNTIME_FIELDS) delete legacyDocument[field];
  const parsed = RTGraph.parseImport(JSON.stringify(legacyDocument));
  assert.equal(parsed.ok, false, 'a legacy document without runtime was accepted');
  assert.equal(parsed.tree, undefined);
  const issue = parsed.issues.find(item => item.code === 'graph_parse_error');
  assert.ok(issue, 'legacy rejection must be graph_parse_error');
  assert.match(issue.zh, /运行配置字段必须成组出现/);
  assert.match(issue.en, /Runtime fields must appear together/);

  /* 显式 runtime:null 同属被删除的控制契约：顶层 runtime 是未知 envelope 字段。 */
  const explicitNull = JSON.parse(fs.readFileSync(EXAMPLE_URL, 'utf8'));
  explicitNull.runtime = null;
  assert.equal(RTGraph.parseImport(JSON.stringify(explicitNull)).ok, false);

  /* serialize 也不再输出旧文档形态：缺 runtime 的树导出必须失败。 */
  const broken = minimalTree();
  delete broken.runtime;
  assert.throws(() => RTGraph.serialize(broken), /backends/);
});

test('runtime fields must appear together and accept no sibling runtime key', () => {
  for (const field of RTGraph.RUNTIME_FIELDS) {
    const document = selfContainedDocument();
    for (const other of RTGraph.RUNTIME_FIELDS) {
      if (other !== field) delete document[other];
    }
    const result = RTGraph.parseImport(JSON.stringify(document));
    assert.equal(result.ok, false, 'only ' + field + ' was accepted');
    const hard = hardIssues(result.issues);
    assert.equal(hard.length > 0, true, field);
    for (const item of hard) {
      assert.equal(item.code, 'graph_parse_error', field);
      assert.equal(item.level, 'hard', field);
      assert.equal(typeof item.zh, 'string', field);
      assert.equal(typeof item.en, 'string', field);
    }
  }

  const missingOne = selfContainedDocument();
  delete missingOne.time_periods;
  assert.equal(RTGraph.parseImport(JSON.stringify(missingOne)).ok, false);

  /* envelope 顶层只认这四个字段名：嵌套 runtime 或拼错的兄弟字段一律拒绝。 */
  const nested = selfContainedDocument();
  nested.runtime = {};
  assert.equal(RTGraph.parseImport(JSON.stringify(nested)).ok, false);
  const extraSibling = selfContainedDocument();
  extraSibling.jev_window_extra = { input_tokens: 1, context_tokens: 2 };
  assert.equal(RTGraph.parseImport(JSON.stringify(extraSibling)).ok, false);
});

test('documents containing any secret or derived key field are rejected', () => {
  const cases = [
    ['provider api_key', document => { document.backends.providers[0].api_key = 'sk-must-not-persist'; }],
    ['provider key_present', document => { document.backends.providers[0].key_present = true; }],
    ['provider key_source', document => { document.backends.providers[0].key_source = 'stored'; }],
    ['jev api_key', document => { document.jev.api_key = 'sk-must-not-persist'; }],
    ['jev key_present', document => { document.jev.key_present = false; }],
    ['jev key_source', document => { document.jev.key_source = 'environment'; }],
    ['profile api_key', document => { document.backends.models[0].profile.api_key = 'sk-must-not-persist'; }],
    ['profile API-KEY', document => { document.backends.models[0].profile['API-KEY'] = 'sk-must-not-persist'; }],
    ['profile Authorization', document => { document.backends.models[0].profile.Authorization = 'Bearer sk-must-not-persist'; }],
    ['profile nested object', document => {
      document.backends.models[0].profile.metadata = { auth: { authorization: 'sk-must-not-persist' } };
    }],
    ['profile nested array', document => {
      document.backends.models[0].profile.metadata = [{ key_source: 'stored' }];
    }],
    ['model key_source', document => { document.backends.models[1].key_source = 'environment'; }],
    ['period api_key', document => { document.time_periods[0].api_key = 'sk-must-not-persist'; }]
  ];
  for (const [label, mutate] of cases) assertRuntimeRejected(mutate, label);
});

test('secret detection normalizes case and separators without over-matching', () => {
  /* 与 Python runtime._secret_key 同一规则：大小写/连字符/下划线无关。 */
  for (const key of ['api_key', 'API-KEY', 'ApiKey', 'authorization', 'AUTHORIZATION',
    'key_present', 'Key-Source']) {
    assert.equal(RTGraph.isSecretField(key), true, key + ' must be treated as secret');
  }
  /* 只包含 "key" 的正常元数据必须保留，不能被模糊匹配误伤。 */
  for (const key of ['monkey', 'key', 'keys', 'keychain', 'hot_key', 'keyed']) {
    assert.equal(RTGraph.isSecretField(key), false, key + ' must stay a normal field');
  }
  assert.deepEqual(RTGraph.withoutSecretFields({
    api_key: 'x', nested: [{ authorization: 'y', tier: 'keep' }], monkey: 1
  }), { nested: [{ tier: 'keep' }], monkey: 1 });
});

test('invalid backend catalogs are rejected', () => {
  const cases = [
    ['duplicate provider', document => { document.backends.providers.push(RTGraph.clone(document.backends.providers[1])); }],
    ['duplicate model', document => { document.backends.models.push(RTGraph.clone(document.backends.models[1])); }],
    ['dangling provider', document => { document.backends.models[1].provider = 'missing'; }],
    ['invalid reasoning_format', document => { document.backends.providers[0].reasoning_format = 'chatml'; }],
    ['non-string reasoning_format', document => { document.backends.providers[0].reasoning_format = 7; }],
    ['invalid key_env', document => { document.backends.providers[0].key_env = '9BAD-ENV'; }],
    ['invalid provider name', document => { document.backends.providers[1].name = 'bad name'; }],
    ['invalid model id', document => { document.backends.models[1].id = '.hidden'; }],
    ['profile not an object', document => { document.backends.models[1].profile = []; }],
    ['capability not a string', document => { document.backends.models[1].profile = { capability: 7 }; }],
    ['upstream empty', document => { document.backends.models[1].upstream = '   '; }],
    ['provider base_url empty', document => { document.backends.providers[1].base_url = ' '; }],
    ['providers not a list', document => { document.backends.providers = {}; }],
    ['backends unknown field', document => { document.backends.extra = true; }],
    ['provider unknown field', document => { document.backends.providers[1].note = 'x'; }],
    ['model unknown field', document => { document.backends.models[1].upstream_key = 'x'; }],
    ['reasoning not an object', document => { document.backends.models[1].profile = { reasoning: 'high' }; }],
    ['reasoning efforts not an object', document => { document.backends.models[1].profile = { reasoning: { efforts: [] } }; }],
    ['reasoning unknown key', document => { document.backends.models[1].profile = { reasoning: { efforts: { off: null, high: 'high' }, temperature: 1 } }; }]
  ];
  for (const [label, mutate] of cases) assertRuntimeRejected(mutate, label);
});

test('invalid jev, window and time period values are rejected', () => {
  const cases = [
    ['jev not an object', document => { document.jev = 'http://jev.test'; }],
    ['jev unknown field', document => { document.jev.extra = true; }],
    ['jev base_url not http', document => { document.jev.base_url = 'ftp://jev.test/v1'; }],
    ['jev base_url with query', document => { document.jev.base_url = 'http://jev.test/v1?x=1'; }],
    ['jev model empty', document => { document.jev.model = '  '; }],
    ['jev timeout zero', document => { document.jev.timeout_seconds = 0; }],
    ['jev timeout too large', document => { document.jev.timeout_seconds = 121; }],
    ['jev timeout string', document => { document.jev.timeout_seconds = '10'; }],
    ['jev instructions empty', document => { document.jev.instructions = '   '; }],
    ['jev state_template without request', document => { document.jev.state_template = 'no placeholder'; }],
    ['jev state_template with extra field', document => { document.jev.state_template = '{request}{other}'; }],
    ['window input below minimum', document => { document.jev_window.input_tokens = 255; }],
    ['window context below minimum', document => { document.jev_window.context_tokens = 511; }],
    ['window input above maximum', document => { document.jev_window.input_tokens = 131073; }],
    ['window input above context', document => { document.jev_window.input_tokens = 8192; }],
    ['window tokens not integers', document => { document.jev_window.input_tokens = 1024.5; }],
    ['window unknown field', document => { document.jev_window.budget = 1; }],
    ['time_periods not a list', document => { document.time_periods = {}; }],
    ['period unknown field', document => { document.time_periods[0].extra = true; }],
    ['period missing end', document => { delete document.time_periods[0].end; }],
    ['duplicate period name', document => { document.time_periods[1].name = 'peak'; }],
    ['period empty days', document => { document.time_periods[0].days = []; }],
    ['period day out of range', document => { document.time_periods[0].days = [0, 7]; }],
    ['period duplicate day', document => { document.time_periods[0].days = [1, 1]; }],
    ['period day not an integer', document => { document.time_periods[0].days = [0, 1.5]; }],
    ['period start out of range', document => { document.time_periods[0].start = '24:00'; }],
    ['period end out of range', document => { document.time_periods[0].end = '09:60'; }],
    ['period start format', document => { document.time_periods[0].start = '9am'; }],
    ['period name empty', document => { document.time_periods[0].name = ' '; }]
  ];
  for (const [label, mutate] of cases) assertRuntimeRejected(mutate, label);
});

test('strict runtime contract rejects missing fields, jev.id and runtime:null', () => {
  const imported = importSelfContained();
  /* 缺任一运行字段：整体拒绝并点名缺失字段。 */
  for (const field of RTGraph.RUNTIME_FIELDS) {
    const document = JSON.parse(RTGraph.serialize(imported.tree));
    delete document[field];
    const result = RTGraph.parseImport(JSON.stringify(document));
    assert.equal(result.ok, false, 'import accepted a document without ' + field);
    const issue = result.issues.find(item => item.code === 'graph_parse_error');
    assert.ok(issue && issue.zh.includes(field),
      'missing ' + field + ' did not produce a field-specific graph_parse_error');
  }
  /* JEV 快照必须带服务 id（Python runtime._jev 同规则）。 */
  const withoutId = JSON.parse(RTGraph.serialize(imported.tree));
  delete withoutId.jev.id;
  const rejected = RTGraph.parseImport(JSON.stringify(withoutId));
  assert.equal(rejected.ok, false);
  assert.ok(rejected.issues.some(item => item.code === 'graph_parse_error' && /jev\.id/.test(item.zh)));
  /* runtime 控制字段（嵌套对象 / null）不是文档契约。 */
  for (const control of [null, { backends: withoutId.backends }]) {
    const document = JSON.parse(RTGraph.serialize(imported.tree));
    document.runtime = control;
    assert.equal(RTGraph.parseImport(JSON.stringify(document)).ok, false,
      'a runtime control field was accepted: ' + JSON.stringify(control));
  }
});

test('complete round-trip preserves the four runtime fields and canvas coordinates', () => {
  const imported = importSelfContained();
  /* 给每个节点不同的坐标：导出必须把内部 position 收集到顶层 canvas 并原样保留。 */
  imported.tree.root.nodes.forEach((node, index) => {
    node.position = { x: 13 * index + 1, y: 7 * index + 2 };
  });
  const document = JSON.parse(RTGraph.serialize(imported.tree));
  assert.deepEqual(Object.keys(document).sort(), [
    'backends', 'canvas', 'default', 'departments', 'enabled', 'jev', 'jev_window',
    'kind', 'name', 'root', 'sticky', 'time_periods', 'timezone', 'version'
  ].sort());
  assert.equal(document.runtime, undefined);
  const parsedBack = RTGraph.parseImport(JSON.stringify(document));
  assert.equal(parsedBack.ok, true, JSON.stringify(parsedBack.issues));
  assert.deepEqual(parsedBack.tree.runtime, imported.tree.runtime);
  assert.deepEqual(parsedBack.tree.root.nodes.map(node => node.position),
    imported.tree.root.nodes.map(node => node.position),
    'the top-level canvas coordinates did not survive the round-trip');
  assert.equal(RTGraph.behaviorCanonical(parsedBack.tree),
    RTGraph.behaviorCanonical(imported.tree));
});

test('runtime canonical keeps declared order and normalizes integral numbers', () => {
  const imported = importSelfContained();
  const canonical = RTGraph.behaviorCanonical(imported.tree);
  assert.ok(canonical.includes('"timeout_seconds":10'), canonical);
  assert.ok(canonical.includes('"context_window":131072'), canonical);
  /* 非整数浮点必须走 Python repr（1e-05），否则与 tree_revision_of 不一致。 */
  assert.ok(canonical.includes('"rate":1e-05'), canonical);
  assert.equal(canonical.includes('10.0'), false);

  const integerDocument = selfContainedDocument();
  integerDocument.jev.timeout_seconds = 10;
  integerDocument.backends.models[0].profile.context_window = 131072;
  const integerText = JSON.stringify(integerDocument);
  const floatText = integerText
    .replace('"timeout_seconds":10', '"timeout_seconds":10.0')
    .replace('"context_window":131072', '"context_window":131072.0');
  assert.notEqual(integerText, floatText);
  const integerImport = RTGraph.parseImport(integerText);
  const floatImport = RTGraph.parseImport(floatText);
  assert.equal(integerImport.ok, true, JSON.stringify(integerImport.issues));
  assert.equal(floatImport.ok, true, JSON.stringify(floatImport.issues));
  assert.equal(RTGraph.behaviorCanonical(integerImport.tree), RTGraph.behaviorCanonical(floatImport.tree));

  /* providers / models / time_periods 保持输入顺序，不排序。 */
  const reversedProviders = selfContainedDocument();
  reversedProviders.backends.providers.reverse();
  const reversedImport = RTGraph.parseImport(JSON.stringify(reversedProviders));
  assert.equal(reversedImport.ok, true, JSON.stringify(reversedImport.issues));
  assert.notEqual(RTGraph.behaviorCanonical(reversedImport.tree), canonical);

  const reversedPeriods = selfContainedDocument();
  reversedPeriods.time_periods.reverse();
  const reversedPeriodsImport = RTGraph.parseImport(JSON.stringify(reversedPeriods));
  assert.equal(reversedPeriodsImport.ok, true);
  assert.notEqual(RTGraph.behaviorCanonical(reversedPeriodsImport.tree), canonical);

  const reversedModels = selfContainedDocument();
  reversedModels.backends.models.reverse();
  const reversedModelsImport = RTGraph.parseImport(JSON.stringify(reversedModels));
  assert.equal(reversedModelsImport.ok, true);
  assert.notEqual(RTGraph.behaviorCanonical(reversedModelsImport.tree), canonical);
});

test('runtimeBehaviorView projects only the Python behavior shape', () => {
  const imported = importSelfContained();
  const view = RTGraph.runtimeBehaviorView(imported.tree);
  assert.deepEqual(Object.keys(view).sort(), ['backends', 'jev', 'jev_window', 'time_periods']);
  assert.deepEqual(Object.keys(view.backends).sort(), ['models', 'providers']);
  assert.deepEqual(view.backends.providers[1],
    { name: 'backup', base_url: 'https://backup.test/v1', key_env: '', reasoning_format: 'openai' });
  assert.deepEqual(view.backends.models[0].profile, { capability: '轻量', context_window: 131072, rate: 1e-5 });
  assert.deepEqual(view.jev_window, { input_tokens: 1024, context_tokens: 4096 });
  assert.deepEqual(view.time_periods[0], { name: 'peak', days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' });

  /* tree / runtime 对象 / flat envelope 三种入参都要能读取。 */
  assert.deepEqual(RTGraph.runtimeBehaviorView(imported.tree.runtime), view);
  const flat = Object.assign({}, imported.tree.runtime, { name: 'envelope' });
  assert.deepEqual(RTGraph.runtimeBehaviorView(flat), view);
  const minimalView = RTGraph.runtimeBehaviorView(minimalTree());
  assert.deepEqual(Object.keys(minimalView).sort(), ['backends', 'jev', 'jev_window', 'time_periods']);
  assert.equal(minimalView.jev, null);
  /* 投影函数对「完全没有 runtime」保持返回 null（内部草稿兜底）；解析入口已严格拒绝。 */
  assert.equal(RTGraph.runtimeBehaviorView({ runtime: null }), null);

  /* 派生/未知字段不进 canonical：即使调用方污染了 runtime。 */
  const tampered = RTGraph.clone(imported.tree);
  tampered.runtime.backends.providers[0].api_key = 'sk-should-not-be-hashed';
  tampered.runtime.backends.providers[0].key_source = 'stored';
  tampered.runtime.jev.key_present = true;
  const tamperedView = RTGraph.runtimeBehaviorView(tampered);
  const serialized = JSON.stringify(tamperedView);
  assert.equal(serialized.includes('sk-should-not-be-hashed'), false);
  assert.equal(serialized.includes('key_source'), false);
  assert.equal(serialized.includes('key_present'), false);
});

test('runtime edits classify as behavior changes, not display or layout', () => {
  const imported = importSelfContained();
  const tree = imported.tree;
  assert.equal(RTGraph.changeKind(tree, RTGraph.clone(tree)), 'clean');

  const jevChanged = RTGraph.clone(tree);
  jevChanged.runtime.jev.model = 'jev-model-2';
  assert.equal(RTGraph.changeKind(tree, jevChanged), 'behavior');

  const timeoutChanged = RTGraph.clone(tree);
  timeoutChanged.runtime.jev.timeout_seconds = 12.5;
  assert.equal(RTGraph.changeKind(tree, timeoutChanged), 'behavior');

  const providerChanged = RTGraph.clone(tree);
  providerChanged.runtime.backends.providers[1].base_url = 'https://backup.test/v2';
  assert.equal(RTGraph.changeKind(tree, providerChanged), 'behavior');

  const modelChanged = RTGraph.clone(tree);
  modelChanged.runtime.backends.models[1].profile = {};
  assert.equal(RTGraph.changeKind(tree, modelChanged), 'behavior');

  const periodsChanged = RTGraph.clone(tree);
  periodsChanged.runtime.time_periods[0].start = '08:00';
  assert.equal(RTGraph.changeKind(tree, periodsChanged), 'behavior');

  const runtimeRemoved = RTGraph.clone(tree);
  runtimeRemoved.runtime = null;
  assert.equal(RTGraph.changeKind(tree, runtimeRemoved), 'behavior');

  const renamed = RTGraph.clone(tree);
  renamed.name = '改名';
  assert.equal(RTGraph.changeKind(tree, renamed), 'display');

  const moved = RTGraph.clone(tree);
  moved.root.nodes[0].position = { x: 1, y: 2 };
  assert.equal(RTGraph.changeKind(tree, moved), 'layout');

  /* 严格契约下草稿必须自带完整 runtime（没有「保存时再从数据库物化」的路径）；
     整组换成另一份 runtime 快照仍是行为变更。 */
  const otherRuntime = RTGraph.clone(minimalTree());
  otherRuntime.runtime = RTGraph.clone(tree.runtime);
  assert.equal(RTGraph.changeKind(minimalTree(), otherRuntime), 'behavior');
});

test('validateRuntime requires a complete runtime and reports graph_parse_error otherwise', () => {
  const imported = importSelfContained();
  assert.deepEqual(RTGraph.validateRuntime(imported.tree), []);
  assert.deepEqual(RTGraph.validateRuntime(imported.tree.runtime), []);
  /* 严格契约：没有 runtime / runtime:null 都是硬错误（保存前本地也会拦住）。 */
  for (const empty of [null, undefined, { runtime: null }, {}]) {
    const emptyIssues = RTGraph.validateRuntime(empty);
    assert.ok(emptyIssues.length > 0, 'missing runtime was accepted: ' + JSON.stringify(empty));
    assert.ok(emptyIssues.every(item => item.code === 'graph_parse_error' && item.level !== 'hint'));
  }
  assert.deepEqual(RTGraph.validateRuntime(minimalTree()), []);

  const broken = RTGraph.clone(imported.tree.runtime);
  broken.jev_window.input_tokens = 4096;
  broken.jev_window.context_tokens = 1024;
  broken.time_periods[0].days = [9];
  const issues = RTGraph.validateRuntime(broken);
  assert.ok(issues.length >= 2, JSON.stringify(issues));
  for (const item of issues) {
    assert.equal(item.code, 'graph_parse_error');
    assert.equal(item.level, 'hard');
    assert.equal(typeof item.zh, 'string');
    assert.equal(typeof item.en, 'string');
    assert.equal(item.node_id, '');
    assert.equal(item.edge_id, '');
    assert.equal(item.port, '');
  }

  /* 嵌套 runtime 缺字段属于「成组出现」错误。 */
  const partial = RTGraph.clone(imported.tree);
  delete partial.runtime.jev_window;
  const partialIssues = RTGraph.validateRuntime(partial);
  assert.ok(partialIssues.some(item => item.code === 'graph_parse_error'));

  const flatPartial = RTGraph.clone(imported.tree.runtime);
  delete flatPartial.time_periods;
  assert.ok(RTGraph.validateRuntime(flatPartial).some(item => item.code === 'graph_parse_error'));
});

test('parseImport keeps runtime on the tree and validate covers envelope runtime fields', () => {
  const imported = importSelfContained();
  assert.deepEqual(hardIssues(RTGraph.validate(imported.tree)), []);

  /* API envelope 形态（flat 四字段）也必须被 validate 识别。 */
  const envelope = {
    id: 'tree-1',
    enabled: true,
    name: imported.tree.name,
    timezone: imported.tree.timezone,
    sticky: imported.tree.sticky,
    root: RTGraph.clone(imported.tree.root)
  };
  Object.assign(envelope, RTGraph.clone(imported.tree.runtime));
  assert.deepEqual(hardIssues(RTGraph.validate(envelope)), []);

  const badEnvelope = RTGraph.clone(envelope);
  badEnvelope.backends.providers[0].api_key = 'sk-must-not-persist';
  const badEnvelopeIssues = hardIssues(RTGraph.validate(badEnvelope));
  assert.ok(badEnvelopeIssues.some(item => item.code === 'graph_parse_error'),
    JSON.stringify(badEnvelopeIssues));
});

/* ── 14. v4 文档契约：departments / enabled / default ──────────────── */

function v4Document(overrides) {
  /* 导入文档的坐标只能来自顶层 canvas：把内部 position 收集到 canvas。 */
  const root = RTGraph.clone(minimalTree().root);
  const canvas = {};
  root.nodes.forEach(node => {
    if (!node || !node.position) return;
    canvas[node.id] = node.position;
    delete node.position;
  });
  const document = Object.assign({
    kind: 'sr-routing-graph', version: 4, name: 'v4', departments: [],
    enabled: false, default: false, timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: root,
  }, documentRuntime(['qwen3-8b']), overrides || {});
  if (Object.keys(canvas).length) document.canvas = canvas;
  return document;
}

test('v4 documents require departments, enabled and default', () => {
  for (const field of ['departments', 'enabled', 'default']) {
    const document = v4Document();
    delete document[field];
    const result = RTGraph.parseImport(JSON.stringify(document));
    assert.equal(result.ok, false, 'import accepted a v4 document without ' + field);
    assert.equal(result.tree, undefined);
    const issue = (result.issues || []).find(item => item.code === 'graph_parse_error');
    assert.ok(issue, 'missing ' + field + ' did not report graph_parse_error');
    assert.ok(issue.zh.includes(field), 'issue does not name ' + field + ': ' + issue.zh);
  }
});

test('v4 import normalizes and sorts departments', () => {
  const result = RTGraph.parseImport(JSON.stringify(
    v4Document({ departments: [' 法务部 ', '财务部'] })));
  assert.equal(result.ok, true, JSON.stringify(result.issues));
  assert.deepEqual(result.tree.departments, ['法务部', '财务部']);
  /* 内部树固定 lifecycle off：导入永远不产生启用/默认图。 */
  assert.equal(result.tree.enabled, false);
  assert.equal(result.tree.default, false);
});

test('v4 rejects duplicate, empty, control-character and non-string departments', () => {
  for (const departments of [['a', 'a'], ['  '], [1], 'x', ['a'.repeat(65)], ['a\u0001']]) {
    const result = RTGraph.parseImport(JSON.stringify(v4Document({ departments })));
    assert.equal(result.ok, false, 'import accepted departments ' + JSON.stringify(departments));
    assert.ok((result.issues || []).some(item => item.code === 'graph_parse_error'),
      'invalid departments did not report graph_parse_error');
  }
});

test('v4 rejects a default graph that is not enabled', () => {
  const result = RTGraph.parseImport(JSON.stringify(
    v4Document({ enabled: false, default: true })));
  assert.equal(result.ok, false);
  assert.ok(result.issues.some(item =>
    item.code === 'graph_parse_error' && /default graph must be enabled/.test(item.en)));
  for (const enabled of ['yes', 1, null]) {
    const rejected = RTGraph.parseImport(JSON.stringify(v4Document({ enabled })));
    assert.equal(rejected.ok, false, 'non-boolean enabled was accepted: ' + String(enabled));
  }
});

test('v4 serialize writes lifecycle/departments and behavior canonical excludes lifecycle', () => {
  const imported = RTGraph.parseImport(JSON.stringify(
    v4Document({ departments: ['财务部'] })));
  assert.equal(imported.ok, true, JSON.stringify(imported.issues));
  const exported = JSON.parse(RTGraph.serialize(imported.tree));
  assert.equal(exported.version, 4);
  assert.deepEqual(exported.departments, ['财务部']);
  assert.equal(exported.enabled, false);
  assert.equal(exported.default, false);

  const canonical = RTGraph.behaviorCanonical(imported.tree);
  assert.ok(canonical.includes('"departments":["财务部"]'), canonical);
  /* 生命周期不参与行为 canonical；部门参与。 */
  const enabled = Object.assign(RTGraph.clone(imported.tree), { enabled: true, default: true });
  assert.equal(RTGraph.behaviorCanonical(enabled), canonical);
  const other = Object.assign(RTGraph.clone(imported.tree), {
    departments: ['财务部', '法务部'],
  });
  assert.notEqual(RTGraph.behaviorCanonical(other), canonical);
});

test('lifecycle flags are outside behavior canonical and changeKind; departments are inside', () => {
  const imported = RTGraph.parseImport(JSON.stringify(v4Document({ departments: ['财务部'] })));
  assert.equal(imported.ok, true, JSON.stringify(imported.issues));
  const tree = imported.tree;
  /* 启停/默认切换是服务端生命周期写：不得进入 behavior 或 editable canonical，
     否则一次 lifecycle 回包就会把干净草稿判脏、或把部门编辑当成生命周期丢弃。 */
  const enabled = Object.assign(RTGraph.clone(tree), { enabled: true, default: true });
  const disabled = Object.assign(RTGraph.clone(tree), { enabled: false, default: false });
  assert.equal(RTGraph.changeKind(tree, enabled), 'clean',
    'enabled/default leaked into the editable canonical');
  assert.equal(RTGraph.changeKind(enabled, disabled), 'clean');
  assert.equal(RTGraph.behaviorCanonical(enabled), RTGraph.behaviorCanonical(disabled));
  /* departments 是行为（参与 tree_revision）：必须判 behavior，不能只判 display。 */
  const department = Object.assign(RTGraph.clone(tree), { departments: ['财务部', '法务部'] });
  assert.equal(RTGraph.changeKind(tree, department), 'behavior',
    'a department edit must be a behavior change');
  const renamed = Object.assign(RTGraph.clone(tree), { name: '改名' });
  assert.equal(RTGraph.changeKind(tree, renamed), 'display',
    'a name edit must stay a display change');
});

test('createMinimalTree returns v4 lifecycle defaults', () => {
  const tree = RTGraph.createMinimalTree('qwen3-8b', minimalRuntime('qwen3-8b'));
  assert.deepEqual(tree.departments, []);
  assert.equal(tree.enabled, false);
  assert.equal(tree.default, false);
  assert.deepEqual(RTGraph.normalizeDepartments(undefined), []);
  assert.deepEqual(RTGraph.normalizeDepartments(['财务部', '法务部']), ['法务部', '财务部']);
  assert.throws(() => RTGraph.normalizeDepartments(['a', 'a']), /duplicate department/);
  assert.throws(() => RTGraph.normalizeDepartments('x'), /list of strings/);
});

/* Python 侧 len()/sorted() 按 Unicode 码点计数与排序；非 BMP 字符（emoji）在
   UTF-16 下是代理对，用 .length / 默认 sort() 会与 Python 分歧，进而导致
   behavior_sha256 跨运行时不一致。此用例锁定码点口径。 */
test('v4 departments use Python code-point length and ordering', () => {
  assert.deepEqual(
    RTGraph.normalizeDepartments(['😀', 'Ａ', '财务部']),
    ['财务部', 'Ａ', '😀'],
    'departments must sort in code-point order (Python sorted), not UTF-16 order');
  const sixtyFour = '😀'.repeat(64);
  assert.deepEqual(RTGraph.normalizeDepartments([sixtyFour]), [sixtyFour],
    '64 code points must be accepted even though they are 128 UTF-16 code units');
  assert.throws(() => RTGraph.normalizeDepartments(['😀'.repeat(65)]),
    /department is empty, too long, or contains control characters/);
});
