/* RTGraph 纯函数层契约测试（Task 9，规格 §2.2–§2.8 / §5）。
 *
 * 运行：node --test scripts/console_graph_test.mjs
 * 加载：graph.js 必须同时可用于浏览器（window.RTGraph）与 Node（module.exports）。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const RTGraph = require('../router/console/routing/graph.js');

const EXAMPLE_URL = new URL('../router/console/routing/example.json', import.meta.url);

function readExample() {
  return JSON.parse(fs.readFileSync(EXAMPLE_URL, 'utf8'));
}

function exampleGraph() {
  return RTGraph.clone(readExample());
}

function minimalTree() {
  return RTGraph.createMinimalTree('qwen3-8b');
}

function targetOf(graph) {
  return graph.nodes.find(node => node.type === 'target');
}

function codesOf(issues) {
  return issues.filter(issue => issue.level !== 'hint').map(issue => issue.code);
}

function hardIssues(issues) {
  return issues.filter(issue => issue.level !== 'hint');
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
  const before = RTGraph.createMinimalTree('qwen3-8b');
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
  const graph = RTGraph.createMinimalTree('qwen3-8b');
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

test('port registry matches the graph v3 contract', () => {
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
    otherwise_target: 'target',
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
  graph.otherwise_target = null;
  graph.error_fallback = 'missing-target';

  const issues = RTGraph.validate(graph);
  const codes = codesOf(issues);
  assert.ok(codes.includes('unknown_node_type'));
  assert.ok(codes.includes('duplicate_node_id'));
  assert.ok(codes.includes('start_count'));
  assert.ok(issues.some(issue => issue.code === 'slot_conflict' && issue.node_id === target.id && issue.port === 'in_1' && issue.edge_id));
  assert.ok(issues.some(issue => issue.code === 'fallback_missing' && issue.port === 'otherwise_target'));
  assert.ok(issues.some(issue => issue.code === 'fallback_missing' && issue.node_id === 'missing-target'));
  assert.ok(issues.every(issue => typeof issue.zh === 'string' && issue.zh.length > 0));
  assert.ok(issues.every(issue => typeof issue.en === 'string' && issue.en.length > 0));
});

test('validate enforces NOT one slot and AND/OR at least two slots', () => {
  const graph = minimalTree().root;
  graph.nodes.push(
    { id: 'and', type: 'logic', operator: 'and', position: { x: 0, y: 0 } },
    { id: 'not', type: 'logic', operator: 'not', position: { x: 0, y: 0 } },
  );
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'and', target_port: 'in_1',
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'not', target_port: 'in_1',
  }).ok, true);
  assert.equal(RTGraph.addEdge(graph, {
    source: 'start', source_port: 'out', target: 'not', target_port: 'in_2',
  }).ok, true);

  const issues = RTGraph.validate(graph).filter(issue => issue.code === 'logic_slot_count');
  assert.deepEqual(issues.map(issue => issue.node_id).sort(), ['and', 'not']);
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
  const text = JSON.stringify({
    kind: 'sr-routing-graph', version: 3, name: '电力示例', timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 }, root,
  });
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
  };
  const text = RTGraph.serialize(tree);
  const exported = JSON.parse(text);
  assert.equal(exported.kind, 'sr-routing-graph');
  assert.equal(exported.version, 3);
  assert.equal(exported.name, tree.name);
  assert.equal(exported.timezone, tree.timezone);
  assert.deepEqual(exported.sticky, tree.sticky);
  assert.equal(exported.root.nodes.length, 15);
  assert.equal(exported.root.edges.length, 15);

  const result = RTGraph.parseImport(text);
  assert.equal(result.ok, true);
  assert.equal(RTGraph.behaviorCanonical(result.tree), RTGraph.behaviorCanonical(tree));
  assert.equal(result.tree.name, tree.name);
  assert.equal(result.tree.timezone, tree.timezone);
  assert.deepEqual(result.tree.sticky, tree.sticky);
  assert.deepEqual(result.tree.root.otherwise_target, 'standard-target');
  assert.deepEqual(result.tree.root.error_fallback, 'light-target');
  assert.equal(hardIssues(result.issues).length, 0);
  assert.equal(result.issues.filter(issue => issue.level === 'hint').length, 4);

  const rewritten = RTGraph.parseImport(RTGraph.serialize(result.tree));
  assert.equal(RTGraph.behaviorCanonical(rewritten.tree), RTGraph.behaviorCanonical(tree));
});

test('legacy and version-2 imports are rejected with the fixed message', () => {
  const expected = '只接受 sr-routing-graph v3（本期不做旧格式兼容与转换）';
  const payloads = [
    JSON.stringify({ kind: 'sr-routing-graph', version: 2, name: '旧版本', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: '2', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ version: 3, name: '没有 kind', root: { type: 'graph', nodes: [], edges: [] } }),
    JSON.stringify({ kind: 'jev', version: 3, root: { type: 'jev', branches: [] } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: 3, root: { type: 'jev', branches: [] } }),
    JSON.stringify({ kind: 'sr-routing-graph', version: 3, root: { type: 'graph', nodes: {} } }),
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

/* ── 7. 新建 / 删除 / 选项级联 ──────────────────────────────────────── */

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
  assert.equal(targets[0].strategy, 'least_inflight');
  assert.equal(graph.otherwise_target, targets[0].id);
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
  assert.equal(graph.otherwise_target, null);
  assert.equal(graph.error_fallback, null);
  assert.ok(removedTarget.fallbackCleared.includes('otherwise_target'));

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

/* ── 8. 行为 canonical / changeKind ─────────────────────────────────── */

test('changeKind separates display fields from behavior fields', () => {
  const before = minimalTree();
  const cases = [];

  const title = RTGraph.clone(before);
  title.root.nodes[1].title = '兜底目标';
  cases.push([title, 'display']);

  const capability = RTGraph.clone(before);
  capability.root.nodes[1].capability = '兜底说明';
  cases.push([capability, 'display']);

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
  const canonical = RTGraph.behaviorCanonical(tree);
  assert.equal(typeof canonical, 'string');
  const payload = JSON.parse(canonical);
  assert.equal(payload.timezone, tree.timezone);
  assert.deepEqual(payload.sticky, { enabled: false, ttl_seconds: 1800 });
  assert.equal(payload.root.type, 'graph');
  assert.deepEqual(payload.root.nodes.map(node => node.id), ['start', 'target-1']);
  assert.equal('position' in payload.root.nodes[0], false);
  assert.equal('title' in payload.root.nodes[0], false);
  assert.equal('capability' in payload.root.nodes[1], false);
  assert.equal('id' in payload.root.edges[0], false);

  for (const node of payload.root.nodes) {
    if (node.type === 'target') assert.equal('strategy' in node, true);
  }
});

test('parseImport output is behavior-stable across coordinate-only round trips', () => {
  const tree = {
    name: '坐标测试', timezone: 'Asia/Shanghai',
    sticky: { enabled: true, ttl_seconds: 600 }, root: readExample(),
  };
  const imported = RTGraph.parseImport(RTGraph.serialize(tree));
  assert.equal(imported.ok, true);
  const moved = RTGraph.clone(imported.tree);
  moved.root.nodes.forEach((node, index) => { node.position = { x: index * 13, y: index * 7 }; });
  assert.equal(RTGraph.changeKind(imported.tree, moved), 'layout');
  assert.equal(RTGraph.behaviorCanonical(imported.tree), RTGraph.behaviorCanonical(moved));
});
