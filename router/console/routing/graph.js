/* RTGraph · 画布 v4 纯函数层（规格 §2.2–§2.8 / §5 / §6.1）
   ------------------------------------------------------------------
   ES module（浏览器 <script type="module"> / Node import 同一份源码）。

   约束：
   · 只做纯函数（不读 DOM、不读 window.MOCK、不发请求），可被 node 直测；
   · 所有函数显式接收 graph（root：{id,type,nodes,edges,error_fallback}）
     或完整 envelope（{name,departments,enabled,default,timezone,sticky,root}）；
   · 算法移植自 docs/od-design/routing-canvas-2026-11-02/mock.js，但去掉了固定场景与组状态。
   ------------------------------------------------------------------ */
const VERSION = 4;
const KIND = 'sr-routing-graph';
const LIMITS = {
  nodes: 200,
  edges: 400,
  questions: 32,
  waves: 8,
  options: 255,
  criteriaMin: 2,
  criteriaMax: 10
};
const THRESHOLD_LIMIT = 1e9;
const NUM_OPS = ['gt', 'gte', 'lt', 'lte', 'eq', 'neq'];
const OP = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠' };
const LOGIC = {
  and: { symbol: '∧', name: '与 AND' },
  or: { symbol: '∨', name: '或 OR' }
};
const NODE_TYPES = ['start', 'question', 'logic', 'condition', 'target'];
const NODE_ORDER = { start: 0, condition: 1, question: 2, logic: 3, target: 4 };
const REJECT_MESSAGE = '只接受 sr-routing-graph v4（本期不做旧格式兼容与转换）';
const PARSE_ERROR_ZH = '图解析失败：root 不符合 sr-routing-graph v4 结构（未知字段或类型非法）';
const PARSE_ERROR_EN = 'Graph parse failed: root does not conform to the sr-routing-graph v4 structure (unknown fields or invalid types)';
const HINTS = [
  '比较条件写在输入槽位上：数值线拖进任何布尔槽位后，点条件即可改成 > 0.75 这类阈值，不需要再放比较门。',
  '同一数值端口可以连到同一个判断门的两个槽位，分别写 > 0.3 与 < 0.8，就是区间条件。',
  '布尔槽位不写条件即为直接通过；要反向判断可以开启取反。',
  '条件阈值 0.65 / 0.70 / 0.60 是示例值：上线前请按实际问题独立校准。'
];
const HINT_EN = [
  'Comparison conditions live on input slots: drop a numeric edge into any boolean slot and edit the threshold (for example > 0.75); no separate comparison node is needed.',
  'Fan the same numeric output into two slots of one AND gate with > 0.3 and < 0.8 to express a range condition.',
  'A boolean slot without a condition passes through directly; enable negation for the opposite test.',
  'Thresholds 0.65 / 0.70 / 0.60 are examples: calibrate them for the real questions before enabling the graph.'
];

const clone = value => JSON.parse(JSON.stringify(value));
const P = (id, dir, type, extra) => Object.assign({ id, dir, type }, extra);
/* 这些节点字段的线上契约是数组；对象/数字会让卡片渲染 .map 直接抛错，
   因此 validate 视为结构错误（graph_parse_error），parseImport 据此拒绝。 */
const ARRAY_NODE_FIELDS = ['models', 'options', 'contains_any', 'periods'];

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== '';
}

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function slotIndex(port) {
  const match = /^in_([1-9][0-9]*)$/.exec(typeof port === 'string' ? port : '');
  if (!match) return null;
  const index = Number(match[1]);
  return Number.isSafeInteger(index) && index <= LIMITS.edges ? index : null;
}

function slotSortKey(port) {
  const index = slotIndex(port);
  return index === null ? Number.MAX_SAFE_INTEGER : index;
}

/* ── 0. canonical JSON（键排序、去 undefined，供行为/可编辑比较） ───── */

/* Python json.dumps 用 float repr 序列化行为字段：整数值浮点输出 "1.0"，
   而 JS 默认输出 "1"。用哨兵包装让 stableStringify 按 Python 语义输出，
   使 RTGraph.behaviorCanonical 与 router.routing.tree.tree_revision_of 逐字节一致。 */
const PY_FLOAT = Symbol('routepilot.pyFloat');
const pyFloat = value => ({ [PY_FLOAT]: true, value });

function pythonFloat(value) {
  if (!Number.isFinite(value)) return 'null';
  if (Object.is(value, -0)) return '-0.0';
  const [mantissa, expText] = value.toExponential().split('e');
  const exponent = Number(expText);
  const sign = mantissa.startsWith('-') ? '-' : '';
  const digits = mantissa.replace('-', '').replace('.', '');
  const absolute = Math.abs(value);
  /* Python repr 的指数切换点是 1e-4 / 1e16，且指数至少两位；与 JS 的
     1e-6 / 1e21 不同，必须显式还原，否则 1e-5、1e20 等会与 Python 不一致。 */
  if (absolute !== 0 && (absolute < 1e-4 || absolute >= 1e16)) {
    const head = digits[0];
    const tail = digits.slice(1);
    const exponentSign = exponent < 0 ? '-' : '+';
    const exponentDigits = String(Math.abs(exponent)).padStart(2, '0');
    return sign + head + (tail ? '.' + tail : '') + 'e' + exponentSign + exponentDigits;
  }
  if (exponent >= 0) {
    if (digits.length <= exponent + 1) {
      return sign + digits + '0'.repeat(exponent + 1 - digits.length) + '.0';
    }
    return sign + digits.slice(0, exponent + 1) + '.' + digits.slice(exponent + 1);
  }
  return sign + '0.' + '0'.repeat(-exponent - 1) + digits;
}

function stableStringify(value) {
  if (value === undefined) return 'null';
  if (value !== null && typeof value === 'object' && value[PY_FLOAT] === true) {
    return pythonFloat(value.value);
  }
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']';
  const keys = Object.keys(value).filter(key => value[key] !== undefined).sort();
  return '{' + keys.map(key => JSON.stringify(key) + ':' + stableStringify(value[key])).join(',') + '}';
}

/* ── 1. 图访问与端口 ───────────────────────────────────────────────── */

function graphOf(value) {
  if (isRecord(value)) {
    if (value.type === 'graph' && Array.isArray(value.nodes) && Array.isArray(value.edges)) return value;
    if (isRecord(value.root) && value.root.type === 'graph' &&
        Array.isArray(value.root.nodes) && Array.isArray(value.root.edges)) return value.root;
  }
  return null;
}

function findNode(graph, nodeId) {
  if (!graph || !Array.isArray(graph.nodes)) return null;
  return graph.nodes.find(node => node && node.id === nodeId) || null;
}

function edgesOf(graph) {
  return graph && Array.isArray(graph.edges) ? graph.edges.filter(edge => isRecord(edge)) : [];
}

function slotPorts(node, graphValue) {
  const used = edgesOf(graphValue)
    .filter(edge => edge.target === node.id)
    .map(edge => edge.target_port);
  let minSlots = 1;
  if (node.type === 'logic') minSlots = 2;
  let maxUsed = 0;
  used.forEach(port => {
    const index = slotIndex(port);
    if (index !== null) maxUsed = Math.max(maxUsed, index);
  });
  const total = Math.min(LIMITS.edges, Math.max(minSlots, maxUsed + 1));
  const isOr = node.type === 'question' || node.type === 'target';
  const slots = [];
  for (let index = 1; index <= total; index += 1) {
    const id = 'in_' + index;
    slots.push(P(id, 'in', 'boolean', {
      label: id,
      short: id,
      slot: true,
      occupied: used.indexOf(id) >= 0,
      desc: isOr
        ? '布尔输入槽位（多个槽位按 OR）。数值线需要配置比较条件；布尔线默认直接通过。'
        : '布尔输入槽位，先按条件求值，再参与 ' + (LOGIC[node.operator] ? LOGIC[node.operator].name : '逻辑') + '；数值线必须配置比较条件。'
    }));
  }
  return slots;
}

function portsOf(node, graphValue) {
  if (!node || typeof node !== 'object') return [];
  switch (node.type) {
    case 'start':
      return [P('out', 'out', 'boolean', {
        label: 'out', short: 'true', desc: '恒为 true：连到哪些问题，哪些就是第一批问题'
      })];
    case 'question': {
      const list = slotPorts(node, graphValue);
      if (node.question_type === 'choice') {
        (Array.isArray(node.options) ? node.options : []).forEach(option => {
          const key = option && option.key;
          const label = option && option.label;
          list.push(P('selected.' + key, 'out', 'boolean', {
            label: '选中 · ' + label, short: '选中', group: 'option:' + key,
            optionLabel: label, desc: '「' + label + '」是否为本次最终选中项（true/false）'
          }));
          list.push(P('prob.' + key, 'out', 'number', {
            label: '置信度 · ' + label, short: '置信度', group: 'option:' + key,
            optionLabel: label, desc: '「' + label + '」的归一化概率 [0,1]；连入布尔槽位时需要写比较条件'
          }));
        });
        list.push(P('p_top', 'out', 'number', {
          label: 'p_top', short: 'p_top', group: 'summary', desc: '最高答案概率 [0,1]'
        }));
        list.push(P('margin', 'out', 'number', {
          label: 'margin', short: 'margin', group: 'summary', desc: '前两名概率差 [0,1]'
        }));
      } else if (node.question_type === 'noul') {
        list.push(P('yes', 'out', 'number', {
          label: 'yes · P(yes)', short: 'yes', group: 'summary',
          desc: 'P(yes)，范围 [0,1]；连入布尔槽位时需要写条件，例如 > 0.6'
        }));
        list.push(P('no', 'out', 'number', {
          label: 'no · 1-P(yes)', short: 'no', group: 'summary', desc: '1 - P(yes)，范围 [0,1]'
        }));
      } else if (node.question_type === 'score') {
        list.push(P('value', 'out', 'number', {
          label: 'value', short: 'value', group: 'summary',
          desc: '决策服务返回的有限数值（不归一化）'
        }));
      }
      return list;
    }
    case 'logic': {
      const list = slotPorts(node, graphValue);
      list.push(P('out', 'out', 'boolean', {
        label: 'out', short: 'out', desc: '三值逻辑输出：true / false / unknown'
      }));
      return list;
    }
    case 'condition':
      return [P('out', 'out', 'boolean', {
        label: 'out', short: 'out', desc: '请求开始时即可求值；命中为 true'
      })];
    case 'target':
      return slotPorts(node, graphValue);
    default:
      return [];
  }
}

function portOf(node, portId, graphValue) {
  return portsOf(node, graphValue).find(port => port.id === portId) || null;
}

function portDesc(node, portId, graphValue) {
  const port = portOf(node, portId, graphValue);
  return port ? port.desc : '';
}

function sourcePortOf(graphValue, edge) {
  const source = findNode(graphValue, edge.source);
  return source ? portOf(source, edge.source_port, graphValue) : null;
}

function edgeType(edge, graphValue) {
  const port = sourcePortOf(graphValue, edge);
  return port && port.type === 'number' ? 'number' : 'boolean';
}

function logicName(node) {
  return node && LOGIC[node.operator] ? LOGIC[node.operator].name : '逻辑门';
}

function nodeLabel(node) {
  if (!node) return '';
  if (node.type === 'start') return '开始';
  if (node.type === 'question') {
    if (node.question_type === 'choice') return '单选问题';
    if (node.question_type === 'noul') return '是否问题';
    return '打分问题';
  }
  if (node.type === 'logic') return LOGIC[node.operator] ? LOGIC[node.operator].name : '判断门';
  if (node.type === 'condition') return node.source === 'text' ? '文本关键词' : '时间段';
  if (node.type === 'target') return node.title || node.id;
  return node.type;
}

/* ── 2. 槽位条件（六算子 + boolean + 三值） ───────────────────────── */

function conditionInvalidReason(edge, graphValue) {
  const source = findNode(graphValue, edge.source);
  const port = source ? portOf(source, edge.source_port, graphValue) : null;
  if (!port) return '来源端口不存在';
  const condition = edge.condition;
  if (port.type === 'number') {
    if (condition == null || typeof condition !== 'object') return '数值输入必须配置比较条件';
    if (NUM_OPS.indexOf(condition.op) < 0) return '比较算子必须是 > ≥ < ≤ = ≠ 之一';
    if (typeof condition.value !== 'number' || !Number.isFinite(condition.value)) return '比较阈值必须是有限数值';
    if (Math.abs(condition.value) > THRESHOLD_LIMIT) return '比较阈值绝对值不能超过 1e9';
    return null;
  }
  if (condition == null) return null;
  if (condition.op !== 'is' || typeof condition.value !== 'boolean') {
    return '布尔输入条件只支持默认通过与取反';
  }
  return null;
}

function defaultCondition(sourceType) {
  return sourceType === 'number' ? { op: 'gt', value: 0.5 } : null;
}

function conditionLabel(edge, graphValue) {
  const type = edgeType(edge, graphValue);
  const condition = edge.condition;
  if (type === 'boolean') {
    if (!condition) return '= true';
    if (condition.op === 'is' && typeof condition.value === 'boolean') {
      return condition.value ? '= true' : '= false';
    }
    return '条件非法';
  }
  if (!condition || !OP[condition.op] || !isFiniteNumber(Number(condition.value))) return '未配置条件';
  return OP[condition.op] + ' ' + Number(condition.value);
}

function evaluateCondition(edge, graphValue, value) {
  const reason = conditionInvalidReason(edge, graphValue);
  if (reason) return { status: 'invalid', result: 'unknown', reason: reason };
  if (value === 'unknown' || value == null) return { status: 'unknown', result: 'unknown' };
  if (edgeType(edge, graphValue) === 'number') {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      return { status: 'invalid', result: 'unknown', reason: '数值输入必须是有限数值' };
    }
    const right = Number(edge.condition.value);
    let result;
    switch (edge.condition.op) {
      case 'gt': result = value > right; break;
      case 'gte': result = value >= right; break;
      case 'lt': result = value < right; break;
      case 'lte': result = value <= right; break;
      case 'eq': result = value === right; break;
      case 'neq': result = value !== right; break;
      default: result = 'unknown';
    }
    return result === 'unknown'
      ? { status: 'unknown', result: 'unknown' }
      : { status: result ? 'true' : 'false', result: result };
  }
  if (typeof value !== 'boolean') {
    return { status: 'invalid', result: 'unknown', reason: '布尔输入必须是 true / false' };
  }
  if (!edge.condition) return { status: value ? 'true' : 'false', result: value };
  const expected = !!edge.condition.value;
  return { status: value === expected ? 'true' : 'false', result: value === expected };
}

/* ── 3. 连线合法性（§2.4 / §2.6） ─────────────────────────────────── */

function reaches(graphValue, from, to, seen) {
  if (from === to) return true;
  const visited = seen || Object.create(null);
  if (visited[from]) return false;
  visited[from] = true;
  return edgesOf(graphValue).filter(edge => edge.source === from)
    .some(edge => reaches(graphValue, edge.target, to, visited));
}

function canConnect(graphValue, draft) {
  if (!isRecord(graphValue) || !Array.isArray(graphValue.nodes) || !Array.isArray(graphValue.edges)) {
    return { ok: false, reason: '图数据非法' };
  }
  const source = findNode(graphValue, draft.source);
  const target = findNode(graphValue, draft.target);
  if (!source || !target) return { ok: false, reason: '节点不存在' };
  if (source.id === target.id) return { ok: false, reason: '不能连接到节点自身' };
  const sourcePort = portOf(source, draft.source_port, graphValue);
  const targetPort = portOf(target, draft.target_port, graphValue);
  if (!sourcePort || !targetPort) return { ok: false, reason: '端口不存在（节点配置可能已变更）' };
  if (sourcePort.dir !== 'out' || targetPort.dir !== 'in') {
    return { ok: false, reason: '只能从输出端口连到输入槽位' };
  }
  if (!targetPort.slot) return { ok: false, reason: '目标不是输入槽位' };

  /* 导入会保留非对象条目交给 validate 报错；探测连线时不能因此抛异常。 */
  const edges = edgesOf(graphValue);
  const duplicated = edges.some(edge =>
    edge.source === draft.source && edge.source_port === draft.source_port &&
    edge.target === draft.target && edge.target_port === draft.target_port);
  if (duplicated) return { ok: false, reason: '这条连线已存在' };

  const occupied = edges.some(edge =>
    edge.target === draft.target && edge.target_port === draft.target_port);
  if (occupied) {
    return { ok: false, reason: '该输入槽位已连接一条线，请使用空槽位（数值线可以连到同一个节点的下一个 in_N）' };
  }

  if (sourcePort.type !== 'number' && sourcePort.type !== 'boolean') {
    return { ok: false, reason: '未知的输出类型' };
  }
  if (reaches(graphValue, target.id, source.id)) {
    return { ok: false, reason: '这条连线会形成环，图必须是有向无环图' };
  }
  return { ok: true };
}

function nextSlot(graphValue, nodeId) {
  const used = edgesOf(graphValue)
    .filter(edge => edge.target === nodeId)
    .map(edge => edge.target_port);
  for (let index = 1; index <= LIMITS.edges; index += 1) {
    if (used.indexOf('in_' + index) < 0) return 'in_' + index;
  }
  return null;
}

function addEdge(graphValue, draft) {
  const check = canConnect(graphValue, draft);
  if (!check.ok) return { ok: false, reason: check.reason };
  const source = findNode(graphValue, draft.source);
  const sourcePort = portOf(source, draft.source_port, graphValue);
  const type = sourcePort.type;
  let condition = draft.condition;
  if (condition === undefined) condition = defaultCondition(type);
  const base = 'e-' + draft.source + '-' + String(draft.source_port).replace(/\./g, '-') +
    '-' + draft.target + '-' + draft.target_port;
  let id = base;
  let suffix = 2;
  while (graphValue.edges.some(edge => edge.id === id)) {
    id = base + '-' + suffix;
    suffix += 1;
  }
  const edge = {
    id: id,
    source: draft.source,
    source_port: draft.source_port,
    target: draft.target,
    target_port: draft.target_port,
    condition: condition
  };
  graphValue.edges.push(edge);
  return { ok: true, edge: edge };
}

/* ── 4. 图编辑操作（原子、无悬空边） ───────────────────────────────── */

function removeEdge(graphValue, edgeId) {
  if (!graphValue || !Array.isArray(graphValue.edges)) return { ok: false, reason: '图数据非法' };
  const index = graphValue.edges.findIndex(edge => edge.id === edgeId);
  if (index < 0) return { ok: false, reason: '连线不存在：' + edgeId };
  const edge = graphValue.edges.splice(index, 1)[0];
  return { ok: true, edge: edge };
}

function removeNode(graphValue, nodeId) {
  if (!graphValue || !Array.isArray(graphValue.nodes) || !Array.isArray(graphValue.edges)) {
    return { ok: false, reason: '图数据非法' };
  }
  const index = graphValue.nodes.findIndex(node => node && node.id === nodeId);
  if (index < 0) return { ok: false, reason: '节点不存在：' + nodeId };
  const node = graphValue.nodes.splice(index, 1)[0];
  const removed = [];
  for (let cursor = graphValue.edges.length - 1; cursor >= 0; cursor -= 1) {
    const edge = graphValue.edges[cursor];
    if (edge.source === nodeId || edge.target === nodeId) removed.unshift(graphValue.edges.splice(cursor, 1)[0]);
  }
  const fallbackCleared = [];
  if (graphValue.error_fallback === nodeId) {
    graphValue.error_fallback = null;
    fallbackCleared.push('error_fallback');
  }
  return { ok: true, node: node, edges: removed, fallbackCleared: fallbackCleared };
}

function removeOption(graphValue, nodeId, key) {
  const node = findNode(graphValue, nodeId);
  if (!node) return { ok: false, reason: '节点不存在：' + nodeId };
  if (node.type !== 'question' || node.question_type !== 'choice') {
    return { ok: false, reason: '只有 choice 问题有选项' };
  }
  const options = Array.isArray(node.options) ? node.options : [];
  const index = options.findIndex(option => option && option.key === key);
  if (index < 0) return { ok: false, reason: '选项不存在：' + key };
  const option = options.splice(index, 1)[0];
  const ports = ['selected.' + key, 'prob.' + key];
  const removed = [];
  for (let cursor = graphValue.edges.length - 1; cursor >= 0; cursor -= 1) {
    const edge = graphValue.edges[cursor];
    if (edge.source === nodeId && ports.indexOf(edge.source_port) >= 0) {
      removed.unshift(graphValue.edges.splice(cursor, 1)[0]);
    }
  }
  return { ok: true, option: option, edges: removed };
}

function renameOptionKey(graphValue, nodeId, oldKey, newKey) {
  const node = findNode(graphValue, nodeId);
  if (!node) return { ok: false, reason: '节点不存在：' + nodeId };
  if (node.type !== 'question' || node.question_type !== 'choice') {
    return { ok: false, reason: '只有 choice 问题有选项' };
  }
  const options = Array.isArray(node.options) ? node.options : [];
  const option = options.find(item => item && item.key === oldKey);
  if (!option) return { ok: false, reason: '选项不存在：' + oldKey };
  if (typeof newKey !== 'string' || newKey.trim() === '') {
    return { ok: false, reason: '选项 key 不能为空' };
  }
  const value = newKey.trim();
  if (/\s/.test(value)) return { ok: false, reason: '选项 key 不能包含空白字符' };
  if (value.indexOf('.') >= 0) return { ok: false, reason: '选项 key 不能包含句点（端口名分隔符）' };
  if (value.length > 64) return { ok: false, reason: '选项 key 不能超过 64 个字符' };
  if (value === oldKey) return { ok: true, key: value, edges: [] };
  if (options.some(item => item && item.key === value)) {
    return { ok: false, reason: '选项 key 已存在：' + value };
  }
  option.key = value;
  /* 端口名来自用户输入（如 constructor / toString）：用无原型对象避免命中原型链。 */
  const mapping = Object.create(null);
  mapping['selected.' + oldKey] = 'selected.' + value;
  mapping['prob.' + oldKey] = 'prob.' + value;
  const updated = [];
  edgesOf(graphValue).forEach(edge => {
    if (edge.source === nodeId && Object.prototype.hasOwnProperty.call(mapping, edge.source_port)) {
      edge.source_port = mapping[edge.source_port];
      updated.push(edge);
    }
  });
  return { ok: true, key: value, edges: updated };
}

/* ── 5. 校验（§5；客户端即时反馈，Issue 形状与 422 detail 一致） ───── */

function issue(code, zh, en, extra) {
  return Object.assign({
    code: code,
    level: 'hard',
    zh: zh,
    en: en,
    node_id: '',
    edge_id: '',
    port: ''
  }, extra || {});
}

function finishIssues(issues) {
  issues.sort((a, b) =>
    String(a.level).localeCompare(String(b.level)) ||
    String(a.code).localeCompare(String(b.code)) ||
    String(a.node_id).localeCompare(String(b.node_id)) ||
    String(a.edge_id).localeCompare(String(b.edge_id)) ||
    String(a.port).localeCompare(String(b.port)) ||
    String(a.zh).localeCompare(String(b.zh)));
  return issues;
}

function hintIssues() {
  return HINTS.map((text, index) => ({
    code: 'hint',
    level: 'hint',
    zh: text,
    en: HINT_EN[index],
    node_id: '',
    edge_id: '',
    port: ''
  }));
}

/* ── 运行配置（自包含画布 JSON 的四个顶层字段） ─────────────────────
   Python 事实源：router/routing/runtime.py、router/jev/config.py、
   router/jev/window.py、router/routing/conditions.py、router/reasoning.py。
   四字段必须成组出现：全缺失或部分缺失都拒绝解析/导入/保存，运行配置绝不再从
   数据库补齐（服务端 router/routing/tree.py::tree_from_dict 同规则）。 */
const RUNTIME_FIELDS = ['backends', 'jev', 'jev_window', 'time_periods'];
/* 密钥与派生的密钥状态只存在于服务端：文档任意层级出现即拒绝。
   RUNTIME_SECRET_FIELDS 是对外展示的字段名；匹配用规范化集合（与 Python
   runtime._secret_key 一致：大小写无关，去掉 -/_ 后比对固定集合）。 */
const RUNTIME_SECRET_FIELDS = ['api_key', 'key_present', 'key_source', 'authorization'];
const SECRET_KEY_NAMES = ['apikey', 'authorization', 'keypresent', 'keysource'];

function isSecretField(key) {
  const normalized = key.toLowerCase().replace(/[-_]/g, '');
  return SECRET_KEY_NAMES.indexOf(normalized) >= 0;
}

/* 递归剔除密钥字段的导出投影：即使内部状态被污染，serialize 也不导出密钥。 */
function withoutSecretFields(value) {
  if (Array.isArray(value)) return value.map(withoutSecretFields);
  if (!isRecord(value)) return value;
  const result = {};
  Object.keys(value).forEach(key => {
    if (!isSecretField(key)) result[key] = withoutSecretFields(value[key]);
  });
  return result;
}
const BACKENDS_FIELDS = ['providers', 'models'];
const PROVIDER_FIELDS = ['name', 'base_url', 'key_env', 'reasoning_format'];
const MODEL_FIELDS = ['id', 'upstream', 'provider', 'profile'];
/* 与 Python snapshot_dict() 同一份白名单：id 必填，密钥字段（api_key/key_present/key_source）
   不在白名单里，出现在 jev 内即被 runtimeKeys 拒绝。 */
const JEV_FIELDS = ['id', 'base_url', 'model', 'key_env', 'timeout_seconds', 'instructions', 'state_template'];
const JEV_WINDOW_FIELDS = ['input_tokens', 'context_tokens'];
const TIME_PERIOD_FIELDS = ['name', 'days', 'start', 'end'];
const REASONING_PROFILE_FIELDS = ['format', 'efforts', 'default', 'supports_reasoning_effort'];
/* router/reasoning.py 的 FORMAT_IDS（provider.reasoning_format 的合法值）。 */
const REASONING_FORMATS = [
  'openai', 'deepseek', 'qwen', 'qwen-chat-template', 'openrouter', 'zai',
  'together', 'string-thinking', 'ant-ling'
];
const RUNTIME_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const RUNTIME_ENV_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
const RUNTIME_LIMITS = {
  inputTokensMin: 256,
  contextTokensMin: 512,
  maxTokens: 131072,
  providerBaseUrlMax: 500,
  upstreamMax: 200,
  capabilityMax: 2000,
  timeoutMin: 0.1,
  timeoutMax: 120
};
/* 与 router/jev/config.py 的默认值逐字符一致：缺省键在导入时补齐。 */
const RUNTIME_DEFAULTS = {
  timeout_seconds: 10,
  key_env: '',
  reasoning_format: 'openai',
  instructions: '你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明，选择最合适处理这条请求的模型。',
  state_template: '【待判断的用户请求】\n{request}\n\n【任务】请选择最合适处理这条请求的模型。'
};

/* ── 导入契约白名单：未知字段/未知类型/非法时区一律 graph_parse_error / invalid_timezone ── */
/* canvas 是可选顶层字段：画布坐标只出现在文档顶层，节点对象不再携带 position。 */
const IMPORT_FIELDS = [
  'kind', 'version', 'name', 'departments', 'enabled', 'default',
  'timezone', 'sticky', 'root', 'canvas'
].concat(RUNTIME_FIELDS);
/* API envelope 是内部树形态：runtime 以嵌套对象携带；扁平四字段也接受（Python tree_to_dict）。 */
const ENVELOPE_FIELDS = [
  'id', 'name', 'enabled', 'default', 'departments', 'timezone', 'sticky',
  'root', 'tree_revision', 'updated_at', 'runtime', 'document_sha256'
].concat(RUNTIME_FIELDS);
const ROOT_FIELDS = ['id', 'type', 'nodes', 'edges', 'error_fallback'];
/* 导入文档的节点契约：只有逻辑字段。内部草稿节点仍保留 position 工作状态。 */
const NODE_FIELDS = {
  start: ['id', 'type', 'title'],
  question: ['id', 'type', 'question_type', 'instructions', 'title', 'options', 'criteria'],
  logic: ['id', 'type', 'operator', 'title'],
  condition: ['id', 'type', 'source', 'contains_any', 'periods', 'title'],
  /* strategy / capability 已下线；仍允许旧文档携带（读取时忽略，导出时丢弃）。 */
  target: ['id', 'type', 'models', 'priority', 'strategy', 'capability', 'title'],
};
const EDGE_FIELDS = ['id', 'source', 'source_port', 'target', 'target_port', 'condition'];
const QUESTION_TYPES = ['choice', 'noul', 'score'];

function isTimeZone(value) {
  if (!isNonEmptyString(value)) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value }).format(new Date());
    return true;
  } catch (error) {
    return false;
  }
}

function addUnknownFields(value, allowed, label, issues, nodeId) {
  if (!isRecord(value)) return;
  const unknown = Object.keys(value).filter(key => allowed.indexOf(key) < 0).sort();
  if (!unknown.length) return;
  issues.push(issue(
    'graph_parse_error',
    label + ' 含未知字段：' + unknown.join(', '),
    label + ' has unknown fields: ' + unknown.join(', '),
    { node_id: nodeId || '' }
  ));
}

/* 画布坐标集中在文档顶层 canvas：{node_id: {x, y}}，引用不存在的节点一律拒绝。 */
function validateCanvas(value, graph, issues) {
  if (value === undefined) return;
  if (!isRecord(value)) {
    issues.push(issue('graph_parse_error', 'canvas 必须是对象', 'canvas must be an object'));
    return;
  }
  const nodeIds = new Set((Array.isArray(graph.nodes) ? graph.nodes : [])
    .filter(isRecord)
    .map(node => (isNonEmptyString(node.id) ? node.id : ''))
    .filter(Boolean));
  Object.keys(value).forEach(nodeId => {
    if (!nodeIds.has(nodeId)) {
      issues.push(issue('graph_parse_error', 'canvas 引用了不存在的节点：' + nodeId,
        'canvas references an unknown node: ' + nodeId, { node_id: nodeId }));
      return;
    }
    const coords = value[nodeId];
    if (!isRecord(coords)) {
      issues.push(issue('graph_parse_error', 'canvas.' + nodeId + ' 必须是 {x,y} 对象',
        'canvas.' + nodeId + ' must be an {x,y} object', { node_id: nodeId }));
      return;
    }
    addUnknownFields(coords, ['x', 'y'], 'canvas.' + nodeId, issues, nodeId);
    if (!isFiniteNumber(coords.x) || !isFiniteNumber(coords.y)) {
      issues.push(issue('graph_parse_error', 'canvas.' + nodeId + ' 的 x/y 必须是有限数值',
        'canvas.' + nodeId + '.x/y must be finite numbers', { node_id: nodeId }));
    }
  });
}

function validateContract(value, graph, issues) {
  addUnknownFields(graph, ROOT_FIELDS, 'graph', issues, '');
  validateCanvas(value.canvas, graph, issues);
  graph.nodes.filter(isRecord).forEach(node => {
    const allowed = Object.prototype.hasOwnProperty.call(NODE_FIELDS, node.type) ? NODE_FIELDS[node.type] : null;
    /* NODE_FIELDS 是文档契约（节点不含 position）；内部草稿保留 node.position
       工作状态，因此内部校验放行它，文档级严格性由 parseImport 在分发前检查。 */
    if (allowed) {
      addUnknownFields(node, allowed.concat('position'), node.type + ' node', issues, String(node.id || ''));
    }
    if (node.type === 'question' && QUESTION_TYPES.indexOf(node.question_type) < 0) {
      issues.push(issue('graph_parse_error', '未知问题类型：' + String(node.question_type),
        'Unknown question type: ' + String(node.question_type), { node_id: String(node.id || '') }));
    }
    if (node.type === 'condition' && ['text', 'periods'].indexOf(node.source) < 0) {
      issues.push(issue('graph_parse_error', '未知条件来源：' + String(node.source),
        'Unknown condition source: ' + String(node.source), { node_id: String(node.id || '') }));
    }
    (Array.isArray(node.options) ? node.options : []).filter(isRecord).forEach(option => {
      addUnknownFields(option, ['key', 'label', 'capability'], 'question option', issues, String(node.id || ''));
    });
    (Array.isArray(node.models) ? node.models : []).filter(isRecord).forEach(model => {
      addUnknownFields(model, ['model', 'reasoning'], 'target model', issues, String(node.id || ''));
    });
  });
  graph.edges.filter(isRecord).forEach(edge => {
    addUnknownFields(edge, EDGE_FIELDS, 'edge', issues, String(edge.target || ''));
    if (isRecord(edge.condition)) {
      addUnknownFields(edge.condition, ['op', 'value'], 'edge condition', issues, String(edge.target || ''));
    }
  });
  if (value !== graph && isRecord(value) && isRecord(value.root)) {
    addUnknownFields(value, ENVELOPE_FIELDS, 'tree', issues, '');
    if (typeof value.name !== 'string') {
      issues.push(issue('graph_parse_error', 'tree.name 必须是字符串', 'tree.name must be a string'));
    }
    if (Object.prototype.hasOwnProperty.call(value, 'enabled') && typeof value.enabled !== 'boolean') {
      issues.push(issue('graph_parse_error', 'enabled 必须是 boolean', 'enabled must be a boolean'));
    }
    if (Object.prototype.hasOwnProperty.call(value, 'default') && typeof value.default !== 'boolean') {
      issues.push(issue('graph_parse_error', 'default 必须是 boolean', 'default must be a boolean'));
    }
    if (value.default === true && value.enabled !== true) {
      issues.push(issue('graph_parse_error', 'default graph must be enabled',
        'default graph must be enabled'));
    }
    if (Object.prototype.hasOwnProperty.call(value, 'departments')) {
      try {
        normalizeDepartments(value.departments);
      } catch (error) {
        issues.push(issue('graph_parse_error', 'departments 非法：' + error.message,
          'Invalid departments: ' + error.message));
      }
    }
    if (!isNonEmptyString(value.timezone)) {
      issues.push(issue('graph_parse_error', 'timezone 必须是非空字符串', 'timezone must be a nonempty string'));
    } else if (!isTimeZone(value.timezone)) {
      issues.push(issue('invalid_timezone', '时区非法：' + value.timezone,
        'Invalid timezone: ' + value.timezone));
    }
    if (!isRecord(value.sticky)) {
      issues.push(issue('graph_parse_error', 'sticky 必须是对象', 'sticky must be an object'));
    } else {
      addUnknownFields(value.sticky, ['enabled', 'ttl_seconds'], 'sticky', issues, '');
      if (Object.prototype.hasOwnProperty.call(value.sticky, 'enabled') &&
          typeof value.sticky.enabled !== 'boolean') {
        issues.push(issue('graph_parse_error', 'sticky.enabled 必须是 boolean',
          'sticky.enabled must be a boolean'));
      }
      if (Object.prototype.hasOwnProperty.call(value.sticky, 'ttl_seconds') &&
          (!Number.isInteger(value.sticky.ttl_seconds) ||
           value.sticky.ttl_seconds < 1 || value.sticky.ttl_seconds > 31536000)) {
        issues.push(issue('graph_parse_error', 'sticky.ttl_seconds 必须是 [1,31536000] 内的整数',
          'sticky.ttl_seconds must be an integer in [1,31536000]'));
      }
    }
    addRuntimeEnvelopeIssues(value, issues);
  }
}

function validate(value) {
  const graph = graphOf(value);
  if (!graph) {
    return finishIssues([issue('graph_parse_error', PARSE_ERROR_ZH, PARSE_ERROR_EN)]);
  }
  const issues = [];
  const nodes = graph.nodes.filter(node => isRecord(node));
  const edges = graph.edges.filter(edge => isRecord(edge));
  if (nodes.length !== graph.nodes.length) {
    issues.push(issue('graph_parse_error', '存在非法节点（不是对象）', 'Invalid node entry (not an object)'));
  }
  if (edges.length !== graph.edges.length) {
    issues.push(issue('graph_parse_error', '存在非法连线（不是对象）', 'Invalid edge entry (not an object)'));
  }
  validateContract(value, graph, issues);

  /* 用户数据做 key 的映射一律用无原型对象，避免 constructor / __proto__ 误判。 */
  const byId = Object.create(null);
  const duplicates = Object.create(null);
  nodes.forEach(node => {
    if (!isNonEmptyString(node.id)) {
      issues.push(issue('graph_parse_error', '节点缺少非空 id', 'Node is missing a non-empty id'));
      return;
    }
    if (byId[node.id]) {
      duplicates[node.id] = true;
      return;
    }
    byId[node.id] = node;
  });
  Object.keys(duplicates).sort().forEach(id => {
    issues.push(issue('duplicate_node_id', '节点 id 重复：' + id, 'Duplicate node id: ' + id, { node_id: id }));
  });

  const edgeIds = Object.create(null);
  edges.forEach(edge => {
    if (!isNonEmptyString(edge.id)) {
      issues.push(issue('graph_parse_error', '连线缺少非空 id', 'Edge is missing a non-empty id'));
      return;
    }
    if (edgeIds[edge.id]) {
      issues.push(issue('duplicate_edge_id', '边 id 重复：' + edge.id, 'Duplicate edge id: ' + edge.id, { edge_id: edge.id }));
    }
    edgeIds[edge.id] = true;
  });

  const startCount = nodes.filter(node => node.type === 'start').length;
  if (startCount !== 1) {
    issues.push(issue(
      'start_count',
      'start 节点数量必须为 1（当前 ' + startCount + ' 个）',
      'Exactly one start node is required (found ' + startCount + ')'
    ));
  }

  nodes.forEach(node => {
    /* 字段类型属于结构契约：非数组的同名字段先报 graph_parse_error，
       避免后续 renderAll 在 cards.js 里 .map 崩溃（半应用状态）。 */
    ARRAY_NODE_FIELDS.forEach(field => {
      if (Object.prototype.hasOwnProperty.call(node, field) && !Array.isArray(node[field])) {
        const id = String(node.id == null ? '' : node.id);
        issues.push(issue(
          'graph_parse_error',
          '节点字段 ' + field + ' 必须是数组（' + id + '）',
          'Node field ' + field + ' must be an array (' + id + ')',
          { node_id: id }
        ));
      }
    });
    if (NODE_TYPES.indexOf(node.type) < 0) {
      issues.push(issue(
        'graph_parse_error',
        '未知节点类型：' + String(node.type) + '（本期只支持 start / question / logic / condition / target）',
        'Unknown node type: ' + String(node.type),
        { node_id: String(node.id || '') }
      ));
      return;
    }
    if (node.type === 'condition') {
      if (node.source === 'text' && (!Array.isArray(node.contains_any) || node.contains_any.length === 0)) {
        issues.push(issue(
          'condition_config_invalid',
          '条件节点配置非法：source=text 需要非空 contains_any（' + node.id + '）',
          'Invalid condition node: source=text requires a non-empty contains_any (' + node.id + ')',
          { node_id: node.id }
        ));
      }
      if (node.source === 'periods' && (!Array.isArray(node.periods) || node.periods.length === 0)) {
        issues.push(issue(
          'condition_config_invalid',
          '条件节点配置非法：source=periods 需要非空时段列表（' + node.id + '）',
          'Invalid condition node: source=periods requires a non-empty period list (' + node.id + ')',
          { node_id: node.id }
        ));
      }
    }
    if (node.type === 'question') {
      if (!isNonEmptyString(node.instructions)) {
        issues.push(issue(
          'question_incomplete',
          '问题说明不能为空：' + node.id,
          'Question instructions must not be empty: ' + node.id,
          { node_id: node.id }
        ));
      }
      if (node.question_type === 'choice') {
        const options = Array.isArray(node.options) ? node.options : [];
        if (options.length < 2) {
          issues.push(issue(
            'question_incomplete',
            'choice 选项至少需要 2 个（当前 ' + options.length + ' 个）：' + node.id,
            'A choice question needs at least 2 options (found ' + options.length + '): ' + node.id,
            { node_id: node.id }
          ));
        }
        if (options.length > LIMITS.options) {
          issues.push(issue(
            'limit_exceeded',
            '选项数量超过上限 ' + LIMITS.options + '（当前 ' + options.length + ' 个）：' + node.id,
            'Too many options: limit is ' + LIMITS.options + ' (found ' + options.length + '): ' + node.id,
            { node_id: node.id }
          ));
        }
        const keys = Object.create(null);
        const labels = Object.create(null);
        options.forEach(option => {
          if (!isRecord(option)) {
            issues.push(issue('question_incomplete', '选项必须是对象：' + node.id, 'Option must be an object: ' + node.id, { node_id: node.id }));
            return;
          }
          const key = option.key;
          if (!isNonEmptyString(key)) {
            issues.push(issue('question_incomplete', '选项 key 不能为空：' + node.id, 'Option key must not be empty: ' + node.id, { node_id: node.id }));
          } else if (keys[key]) {
            issues.push(issue('question_incomplete', '选项 key 重复：' + key + '（' + node.id + '）', 'Duplicate option key: ' + key + ' (' + node.id + ')', { node_id: node.id }));
          }
          if (isNonEmptyString(key)) keys[key] = true;
          const label = option.label;
          if (!isNonEmptyString(label)) {
            issues.push(issue('question_incomplete', '选项 label 不能为空：' + String(key) + '（' + node.id + '）', 'Option label must not be empty: ' + String(key) + ' (' + node.id + ')', { node_id: node.id }));
          } else if (labels[label]) {
            issues.push(issue('question_incomplete', '选项 label 重复：' + label + '（' + node.id + '）', 'Duplicate option label: ' + label + ' (' + node.id + ')', { node_id: node.id }));
          }
          if (isNonEmptyString(label)) labels[label] = true;
          if (!isNonEmptyString(option.capability)) {
            issues.push(issue('question_incomplete', '选项 capability 不能为空：' + String(key) + '（' + node.id + '）', 'Option capability must not be empty: ' + String(key) + ' (' + node.id + ')', { node_id: node.id }));
          }
        });
      }
      if (node.question_type === 'score') {
        if (node.criteria == null) {
          issues.push(issue(
            'score_criteria_required',
            'score 问题必须配置评分档 criteria：' + node.id,
            'A score question requires criteria levels: ' + node.id,
            { node_id: node.id }
          ));
        } else {
          const levels = node.criteria;
          const broken = !Array.isArray(levels) ||
            levels.length < LIMITS.criteriaMin || levels.length > LIMITS.criteriaMax ||
            levels.some(level => !isFiniteNumber(level));
          if (broken) {
            issues.push(issue(
              'score_criteria_invalid',
              'score 的 criteria 必须是 ' + LIMITS.criteriaMin + '–' + LIMITS.criteriaMax + ' 个有限数值：' + node.id,
              'Score criteria must be ' + LIMITS.criteriaMin + ' to ' + LIMITS.criteriaMax + ' finite numbers: ' + node.id,
              { node_id: node.id }
            ));
          }
        }
      }
    }
    if (node.type === 'logic' && !LOGIC[node.operator]) {
      issues.push(issue(
        'logic_operator_invalid',
        '判断门算子非法：' + String(node.operator) + '（' + node.id + '）',
        'Invalid logic operator: ' + String(node.operator) + ' (' + node.id + ')',
        { node_id: node.id }
      ));
    }
    if (node.type === 'target') {
      if (typeof node.priority !== 'number' || !Number.isInteger(node.priority) || node.priority < 1) {
        issues.push(issue(
          'target_priority_invalid',
          'target priority 必须是正整数：' + node.id,
          'Target priority must be a positive integer: ' + node.id,
          { node_id: node.id }
        ));
      }
    }
  });

  const priorities = Object.create(null);
  nodes.filter(node => node.type === 'target').forEach(node => {
    if (typeof node.priority !== 'number' || !Number.isInteger(node.priority)) return;
    if (priorities[node.priority]) {
      issues.push(issue(
        'target_priority_invalid',
        'target priority 必须唯一：' + node.id,
        'Target priority must be unique: ' + node.id,
        { node_id: node.id }
      ));
    }
    priorities[node.priority] = true;
  });

  const fallback = graph.error_fallback;
  if (!isNonEmptyString(fallback)) {
    issues.push(issue(
      'fallback_missing',
      '兜底模型未配置：没有 target 激活或决策失败时需要兜底目标',
      'Fallback model is not configured: a fallback target is required when no target activates or a decision fails',
      { port: 'error_fallback' }
    ));
  } else {
    const node = byId[fallback];
    if (!node) {
      issues.push(issue('fallback_missing', '兜底模型指向的节点不存在：' + fallback, 'Fallback model points to a missing node: ' + fallback, { node_id: fallback, port: 'error_fallback' }));
    } else if (node.type !== 'target') {
      issues.push(issue('fallback_missing', '兜底模型必须指向 target 节点：' + fallback, 'Fallback model must point to a target node: ' + fallback, { node_id: fallback, port: 'error_fallback' }));
    }
  }

  const adjacency = Object.create(null);
  const backAdjacency = Object.create(null);
  nodes.forEach(node => {
    if (!isNonEmptyString(node.id)) return;
    adjacency[node.id] = [];
    backAdjacency[node.id] = [];
  });
  const incomingCount = Object.create(null);
  const slotGroups = Object.create(null);
  edges.forEach(edge => {
    const source = byId[edge.source];
    const target = byId[edge.target];
    if (!source) {
      issues.push(issue(
        'edge_endpoint_missing',
        '边的起点节点不存在：' + String(edge.source) + '（' + String(edge.id) + '）',
        'Edge source node does not exist: ' + String(edge.source) + ' (' + String(edge.id) + ')',
        { node_id: String(edge.source || ''), edge_id: String(edge.id || ''), port: String(edge.source_port || '') }
      ));
    }
    if (!target) {
      issues.push(issue(
        'edge_endpoint_missing',
        '边的目标节点不存在：' + String(edge.target) + '（' + String(edge.id) + '）',
        'Edge target node does not exist: ' + String(edge.target) + ' (' + String(edge.id) + ')',
        { node_id: String(edge.target || ''), edge_id: String(edge.id || ''), port: String(edge.target_port || '') }
      ));
    }
    if (!source || !target) return;

    if (adjacency[edge.source]) adjacency[edge.source].push(edge.target);
    if (backAdjacency[edge.source]) {
      backAdjacency[edge.source].push({
        target: edge.target,
        edge_id: String(edge.id || ''),
        port: String(edge.target_port || '')
      });
    }
    incomingCount[edge.target] = (incomingCount[edge.target] || 0) + 1;

    const sourcePort = portOf(source, edge.source_port, graph);
    const targetPort = portOf(target, edge.target_port, graph);
    const slotOk = target.type !== 'start' && target.type !== 'condition' &&
      targetPort !== null && targetPort.slot === true;
    if (!sourcePort || sourcePort.dir !== 'out') {
      issues.push(issue(
        'edge_port_missing',
        '输出端口不存在：' + edge.source + '.' + String(edge.source_port) + '（' + String(edge.id) + '）',
        'Output port does not exist: ' + edge.source + '.' + String(edge.source_port) + ' (' + String(edge.id) + ')',
        { node_id: edge.source, edge_id: String(edge.id || ''), port: String(edge.source_port || '') }
      ));
    }
    if (!slotOk) {
      issues.push(issue(
        'edge_port_missing',
        '输入槽位不存在：' + edge.target + '.' + String(edge.target_port) + '（' + String(edge.id) + '）',
        'Input slot does not exist: ' + edge.target + '.' + String(edge.target_port) + ' (' + String(edge.id) + ')',
        { node_id: edge.target, edge_id: String(edge.id || ''), port: String(edge.target_port || '') }
      ));
    }
    if (slotOk) {
      const slotKey = edge.target + '::' + edge.target_port;
      (slotGroups[slotKey] = slotGroups[slotKey] || []).push(edge.id);
    }
    if (sourcePort && sourcePort.dir === 'out' && slotOk) {
      const reason = conditionInvalidReason(edge, graph);
      if (reason) {
        const tail = edge.source + '.' + edge.source_port + ' → ' + edge.target + '.' + edge.target_port;
        const code = sourcePort.type === 'number'
          ? (edge.condition == null ? 'numeric_edge_condition_missing' : 'numeric_edge_condition_invalid')
          : 'boolean_edge_condition_invalid';
        issues.push(issue(code, reason + '：' + tail, 'Invalid slot condition (' + reason + '): ' + tail, {
          node_id: edge.target, edge_id: String(edge.id || ''), port: String(edge.target_port || '')
        }));
      }
    }
  });

  Object.keys(slotGroups).sort().forEach(slotKey => {
    const ids = slotGroups[slotKey];
    if (ids.length < 2) return;
    const parts = slotKey.split('::');
    const keeper = ids.slice().sort()[0];
    ids.slice().sort().forEach(edgeId => {
      if (edgeId === keeper) return;
      issues.push(issue(
        'slot_conflict',
        '同一槽位存在多条入边：' + parts[0] + '.' + parts[1],
        'Multiple incoming edges on the same slot: ' + parts[0] + '.' + parts[1],
        { node_id: parts[0], edge_id: String(edgeId || ''), port: parts[1] }
      ));
    });
  });

  /* 环检测：DFS 回边；回边保留 edge_id / 目标槽位，Issue 可定位（§5.1 / §6.10）。 */
  const state = Object.create(null);
  const backEdges = [];
  function visit(nodeId) {
    state[nodeId] = 1;
    (backAdjacency[nodeId] || []).slice().sort((a, b) => {
      if (a.target !== b.target) return a.target < b.target ? -1 : 1;
      if (a.edge_id !== b.edge_id) return a.edge_id < b.edge_id ? -1 : 1;
      if (a.port !== b.port) return a.port < b.port ? -1 : 1;
      return 0;
    }).forEach(edge => {
      const current = state[edge.target] || 0;
      if (current === 1) {
        backEdges.push(edge);
      } else if (current === 0) {
        visit(edge.target);
      }
    });
    state[nodeId] = 2;
  }
  Object.keys(adjacency).sort().forEach(nodeId => {
    if (!state[nodeId]) visit(nodeId);
  });
  backEdges.forEach(back => {
    issues.push(issue(
      'edge_cycle',
      '图中有环：' + back.target,
      'The graph contains a cycle at ' + back.target,
      { node_id: back.target, edge_id: back.edge_id, port: back.port }
    ));
  });

  nodes.forEach(node => {
    if (node.type !== 'logic') return;
    const count = incomingCount[node.id] || 0;
    if ((node.operator === 'and' || node.operator === 'or') && count < 2) {
      issues.push(issue(
        'logic_slot_count',
        'AND / OR 至少需要 2 个输入（当前 ' + count + ' 个）：' + node.id,
        'AND / OR need at least 2 inputs (found ' + count + '): ' + node.id,
        { node_id: node.id }
      ));
    }
  });

  /* 可达性：start 与第 0 阶段 condition 节点都是源（§5.2）。 */
  const sources = nodes.filter(node => node.type === 'start' || node.type === 'condition')
    .map(node => node.id);
  const reachable = Object.create(null);
  const queue = sources.slice();
  while (queue.length) {
    const nodeId = queue.pop();
    if (reachable[nodeId] || !adjacency[nodeId]) continue;
    reachable[nodeId] = true;
    adjacency[nodeId].forEach(target => { if (adjacency[target] !== undefined) queue.push(target); });
  }
  nodes.filter(node => node.type === 'question').forEach(node => {
    if (!reachable[node.id]) {
      issues.push(issue(
        'question_unreachable',
        '问题节点从 start 与第 0 阶段条件均不可达：' + node.id,
        'Question node is reachable from neither the start node nor any phase-0 condition: ' + node.id,
        { node_id: node.id }
      ));
    }
  });

  if (graph.nodes.length > LIMITS.nodes) {
    issues.push(issue('limit_exceeded', '节点数超过上限 ' + LIMITS.nodes + '（当前 ' + graph.nodes.length + ' 个）', 'Too many nodes: limit is ' + LIMITS.nodes + ' (found ' + graph.nodes.length + ')'));
  }
  if (graph.edges.length > LIMITS.edges) {
    issues.push(issue('limit_exceeded', '边数超过上限 ' + LIMITS.edges + '（当前 ' + graph.edges.length + ' 条）', 'Too many edges: limit is ' + LIMITS.edges + ' (found ' + graph.edges.length + ')'));
  }
  const questionCount = nodes.filter(node => node.type === 'question').length;
  if (questionCount > LIMITS.questions) {
    issues.push(issue('limit_exceeded', '问题节点超过上限 ' + LIMITS.questions + '（当前 ' + questionCount + ' 个）', 'Too many question nodes: limit is ' + LIMITS.questions + ' (found ' + questionCount + ')'));
  }
  const chain = questionChainDepth(nodes, adjacency);
  if (chain > LIMITS.waves) {
    issues.push(issue('limit_exceeded', '最长问题链 ' + chain + ' 波，超过上限 ' + LIMITS.waves + ' 波', 'Longest question chain is ' + chain + ' waves, limit is ' + LIMITS.waves));
  }

  return finishIssues(issues).concat(hintIssues());
}

function questionChainDepth(nodes, adjacency) {
  const byId = Object.create(null);
  nodes.forEach(node => { if (isNonEmptyString(node.id)) byId[node.id] = node; });
  const indegree = Object.create(null);
  Object.keys(byId).forEach(id => { indegree[id] = 0; });
  Object.keys(adjacency).forEach(source => {
    adjacency[source].forEach(target => {
      if (indegree[target] !== undefined) indegree[target] += 1;
    });
  });
  const queue = Object.keys(indegree).filter(id => indegree[id] === 0).sort();
  const order = [];
  for (let index = 0; index < queue.length; index += 1) {
    const id = queue[index];
    order.push(id);
    (adjacency[id] || []).forEach(target => {
      if (indegree[target] === undefined) return;
      indegree[target] -= 1;
      if (indegree[target] === 0) queue.push(target);
    });
  }
  const depth = Object.create(null);
  order.forEach(id => {
    const node = byId[id];
    const preds = Object.keys(adjacency).filter(source => adjacency[source].indexOf(id) >= 0);
    const maxPred = preds.reduce((max, source) => Math.max(max, depth[source] || 0), 0);
    if (node.type === 'start' || node.type === 'condition') depth[id] = 0;
    else if (node.type === 'question') depth[id] = maxPred + 1;
    else depth[id] = maxPred;
  });
  return Object.keys(byId)
    .filter(id => byId[id].type === 'question')
    .reduce((max, id) => Math.max(max, depth[id] || 0), 0);
}

/* ── 5b. 运行配置（backends / jev / jev_window / time_periods） ──────
   Python 事实源：runtime_from_dict / runtime_to_dict / runtime_behavior_dict。
   导入侧做与 Python 等价的严格解析与默认值补齐；canonical 侧输出与
   tree_behavior_dict 逐字节一致的投影（整数值数字归一、非整数走 Python repr）。 */

function hasOwnField(value, key) {
  return value !== null && typeof value === 'object' &&
    Object.prototype.hasOwnProperty.call(value, key);
}

function runtimeIssue(issues, zh, en) {
  issues.push(issue('graph_parse_error', zh, en));
}

/* Python runtime._text：strip 后非空、长度上限、无控制字符。 */
function runtimeText(value, label, maxLen, issues) {
  if (typeof value !== 'string') {
    runtimeIssue(issues, label + ' 必须是字符串', label + ' must be a string');
    return null;
  }
  const text = value.trim();
  if (!text || text.length > maxLen || /[\u0000-\u001f\u007f]/.test(text)) {
    runtimeIssue(issues, label + ' 为空、过长或含控制字符',
      label + ' is empty, too long, or contains control characters');
    return null;
  }
  return text;
}

/* Python runtime._id：_text 后再匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$。 */
function runtimeId(value, label, issues) {
  const text = runtimeText(value, label, 64, issues);
  if (text !== null && !RUNTIME_ID_RE.test(text)) {
    runtimeIssue(issues, label + ' 非法：' + text, 'Invalid ' + label + ': ' + text);
    return null;
  }
  return text;
}

/* Python reject_unknown_fields：严格键集合（api_key 等派生字段也会在这里被拒绝）。 */
function runtimeKeys(value, allowed, label, issues) {
  const unknown = Object.keys(value).filter(key => allowed.indexOf(key) < 0).sort();
  if (!unknown.length) return true;
  runtimeIssue(issues, label + ' 含未知字段：' + unknown.join(', '),
    label + ' has unknown fields: ' + unknown.join(', '));
  return false;
}

/* 文档任意层级出现密钥/派生字段即拒绝：密钥永不进入画布 JSON。
   显式栈遍历：导入内容不可信，深度嵌套不能把 parseImport 变成栈溢出。 */
function findSecretField(value) {
  const stack = [value];
  while (stack.length) {
    const current = stack.pop();
    if (Array.isArray(current)) {
      current.forEach(item => stack.push(item));
      continue;
    }
    if (!isRecord(current)) continue;
    const keys = Object.keys(current);
    for (let index = 0; index < keys.length; index += 1) {
      if (isSecretField(keys[index])) return keys[index];
      stack.push(current[keys[index]]);
    }
  }
  return null;
}

/* Python _json_copy：profile 只允许 JSON 值（有限数字、字符串、布尔、null、数组、对象）。 */
function isJsonValue(value) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  if (isRecord(value)) return Object.keys(value).every(key => isJsonValue(value[key]));
  return false;
}

/* Python JevSettings 的 base_url：http/https、无 query/fragment。 */
function isHttpUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
    if (!url.hostname || url.search || url.hash) return false;
    return true;
  } catch (error) {
    return false;
  }
}

/* Python JevSettings 的 state_template：恰好一个 {request} 占位（{{ / }} 为转义）。 */
function templateFieldIssues(value, label, issues) {
  const fields = [];
  let index = 0;
  while (index < value.length) {
    const char = value[index];
    if (char === '{') {
      if (value[index + 1] === '{') { index += 2; continue; }
      const close = value.indexOf('}', index + 1);
      if (close < 0) {
        runtimeIssue(issues, label + ' 含未闭合的格式字段', label + ' has an unclosed format field');
        return;
      }
      fields.push(value.slice(index + 1, close).split(':')[0].split('!')[0]);
      index = close + 1;
      continue;
    }
    if (char === '}') {
      if (value[index + 1] === '}') { index += 2; continue; }
      runtimeIssue(issues, label + ' 含未配对的 }', label + ' has an unmatched }');
      return;
    }
    index += 1;
  }
  if (fields.length !== 1 || fields[0] !== 'request') {
    runtimeIssue(issues, label + ' 必须且只能包含一个 {request} 占位',
      label + ' must contain exactly one {request} placeholder');
  }
}

function normalizeRuntimeProviders(raw, issues) {
  if (!Array.isArray(raw)) {
    runtimeIssue(issues, 'backends.providers 必须是数组', 'backends.providers must be a list');
    return [];
  }
  const providers = [];
  const seen = Object.create(null);
  raw.forEach((item, index) => {
    const label = 'backends.providers[' + index + ']';
    if (!isRecord(item)) {
      runtimeIssue(issues, label + ' 必须是对象', label + ' must be an object');
      return;
    }
    runtimeKeys(item, PROVIDER_FIELDS, label, issues);
    const name = runtimeId(item.name, label + '.name', issues);
    if (name !== null) {
      if (seen[name]) runtimeIssue(issues, 'provider 名称重复：' + name, 'Duplicate provider: ' + name);
      seen[name] = true;
    }
    const keyEnv = hasOwnField(item, 'key_env') ? item.key_env : RUNTIME_DEFAULTS.key_env;
    const keyEnvOk = typeof keyEnv === 'string' && (!keyEnv || RUNTIME_ENV_RE.test(keyEnv));
    if (!keyEnvOk) {
      runtimeIssue(issues, label + '.key_env 必须是空串或合法环境变量名',
        label + '.key_env must be empty or a valid environment variable name');
    }
    const reasoningFormat = hasOwnField(item, 'reasoning_format')
      ? item.reasoning_format : RUNTIME_DEFAULTS.reasoning_format;
    const formatOk = typeof reasoningFormat === 'string' &&
      REASONING_FORMATS.indexOf(reasoningFormat) >= 0;
    if (!formatOk) {
      runtimeIssue(issues, label + '.reasoning_format 非法：' + String(reasoningFormat),
        label + '.reasoning_format is invalid: ' + String(reasoningFormat));
    }
    const baseUrl = runtimeText(item.base_url, label + '.base_url',
      RUNTIME_LIMITS.providerBaseUrlMax, issues);
    if (name === null || baseUrl === null) return;
    providers.push({
      name: name,
      base_url: baseUrl.replace(/\/+$/, ''),
      key_env: keyEnv,
      reasoning_format: reasoningFormat
    });
  });
  return providers;
}

/* Python reasoning.validate_model_profile 的「形状级」移植：只做对象/键集合/
   默认档位与 capability 归一；完整 reasoning 规则仍以服务端为准。 */
function normalizeModelProfile(raw, label, issues) {
  if (!isRecord(raw)) {
    runtimeIssue(issues, label + ' 必须是对象', label + ' must be an object');
    return null;
  }
  if (!isJsonValue(raw)) {
    runtimeIssue(issues, label + ' 只允许 JSON 值', label + ' must contain only JSON values');
    return null;
  }
  const profile = clone(raw);
  if (hasOwnField(profile, 'reasoning')) {
    const reasoning = profile.reasoning;
    if (reasoning === null) {
      delete profile.reasoning;
    } else if (reasoning === false) {
      /* 显式 false = 明确不支持思考，原样保留。 */
    } else if (!isRecord(reasoning)) {
      runtimeIssue(issues, label + '.reasoning 必须是对象或 false',
        label + '.reasoning must be an object or false');
    } else {
      runtimeKeys(reasoning, REASONING_PROFILE_FIELDS, label + '.reasoning', issues);
      const efforts = reasoning.efforts;
      const effortsOk = isRecord(efforts) && Object.keys(efforts).length > 0;
      if (!effortsOk) {
        runtimeIssue(issues, label + '.reasoning.efforts 必须是非空对象',
          label + '.reasoning.efforts must be a non-empty object');
      }
      const normalized = { efforts: effortsOk ? clone(efforts) : {} };
      if (hasOwnField(reasoning, 'format')) {
        if (typeof reasoning.format !== 'string' ||
            REASONING_FORMATS.indexOf(reasoning.format) < 0) {
          runtimeIssue(issues, label + '.reasoning.format 非法：' + String(reasoning.format),
            label + '.reasoning.format is invalid: ' + String(reasoning.format));
        } else {
          normalized.format = reasoning.format;
        }
      }
      if (hasOwnField(reasoning, 'default') && reasoning.default !== null) {
        const level = reasoning.default;
        if (typeof level !== 'string' || !effortsOk || !hasOwnField(efforts, level)) {
          runtimeIssue(issues, label + '.reasoning.default 必须是 efforts 中的档位',
            label + '.reasoning.default must be one of the efforts levels');
        } else {
          normalized.default = level;
        }
      }
      if (hasOwnField(reasoning, 'supports_reasoning_effort')) {
        if (typeof reasoning.supports_reasoning_effort !== 'boolean') {
          runtimeIssue(issues, label + '.reasoning.supports_reasoning_effort 必须是布尔值',
            label + '.reasoning.supports_reasoning_effort must be a boolean');
        } else {
          normalized.supports_reasoning_effort = reasoning.supports_reasoning_effort;
        }
      }
      profile.reasoning = normalized;
    }
  }
  /* capability：字符串或 null，trim 后空串删除、长度 ≤ 2000（Python 同规则）。 */
  if (hasOwnField(profile, 'capability')) {
    const capability = profile.capability;
    if (capability === null) {
      delete profile.capability;
    } else if (typeof capability !== 'string') {
      runtimeIssue(issues, label + '.capability 必须是字符串或 null',
        label + '.capability must be a string or null');
    } else {
      const text = capability.trim();
      if (text.length > RUNTIME_LIMITS.capabilityMax) {
        runtimeIssue(issues, label + '.capability 不能超过 ' + RUNTIME_LIMITS.capabilityMax + ' 个字符',
          label + '.capability must not exceed ' + RUNTIME_LIMITS.capabilityMax + ' characters');
      } else if (text) {
        profile.capability = text;
      } else {
        delete profile.capability;
      }
    }
  }
  return profile;
}

function normalizeRuntimeModels(raw, providers, issues) {
  if (!Array.isArray(raw)) {
    runtimeIssue(issues, 'backends.models 必须是数组', 'backends.models must be a list');
    return [];
  }
  const formats = Object.create(null);
  providers.forEach(provider => { formats[provider.name] = provider.reasoning_format; });
  const models = [];
  const seen = Object.create(null);
  raw.forEach((item, index) => {
    const label = 'backends.models[' + index + ']';
    if (!isRecord(item)) {
      runtimeIssue(issues, label + ' 必须是对象', label + ' must be an object');
      return;
    }
    runtimeKeys(item, MODEL_FIELDS, label, issues);
    const id = runtimeId(item.id, label + '.id', issues);
    if (id !== null) {
      if (seen[id]) runtimeIssue(issues, 'model id 重复：' + id, 'Duplicate model: ' + id);
      seen[id] = true;
    }
    const provider = runtimeId(item.provider, label + '.provider', issues);
    if (provider !== null && formats[provider] === undefined) {
      runtimeIssue(issues, 'model 引用了未知 provider：' + provider, 'Unknown provider: ' + provider);
    }
    const upstream = runtimeText(item.upstream, label + '.upstream', RUNTIME_LIMITS.upstreamMax, issues);
    const profile = normalizeModelProfile(item.profile, label + '.profile', issues);
    if (id === null || provider === null || upstream === null || profile === null) return;
    models.push({ id: id, upstream: upstream, provider: provider, profile: profile });
  });
  return models;
}

function normalizeRuntimeJev(raw, issues) {
  if (raw === null) return null;
  if (!isRecord(raw)) {
    runtimeIssue(issues, 'jev 必须是对象或 null', 'jev must be an object or null');
    return null;
  }
  runtimeKeys(raw, JEV_FIELDS, 'jev', issues);
  /* Python runtime._jev：id 必填（服务快照的身份），key_env 缺省为空串。 */
  if (!hasOwnField(raw, 'id')) {
    runtimeIssue(issues, 'jev.id 是必填字段', 'jev.id is required');
    return null;
  }
  const id = runtimeId(raw.id, 'jev.id', issues);
  const keyEnv = hasOwnField(raw, 'key_env') ? raw.key_env : RUNTIME_DEFAULTS.key_env;
  const keyEnvOk = typeof keyEnv === 'string' && (!keyEnv || RUNTIME_ENV_RE.test(keyEnv));
  if (!keyEnvOk) {
    runtimeIssue(issues, 'jev.key_env 必须是空串或合法环境变量名',
      'jev.key_env must be empty or a valid environment variable name');
  }
  let baseUrl = null;
  if (typeof raw.base_url !== 'string') {
    runtimeIssue(issues, 'jev.base_url 必须是 http/https URL', 'jev.base_url must be an http/https URL');
  } else {
    const text = raw.base_url.trim().replace(/\/+$/, '');
    if (!isHttpUrl(text)) {
      runtimeIssue(issues, 'jev.base_url 必须是 http/https URL 且不含 query/fragment',
        'jev.base_url must be an http/https URL without query or fragment');
    } else {
      baseUrl = text;
    }
  }
  const model = raw.model;
  if (typeof model !== 'string' || !model.trim()) {
    runtimeIssue(issues, 'jev.model 不能为空', 'jev.model must be nonempty');
  }
  const timeout = hasOwnField(raw, 'timeout_seconds')
    ? raw.timeout_seconds : RUNTIME_DEFAULTS.timeout_seconds;
  if (typeof timeout !== 'number' || !Number.isFinite(timeout) ||
      timeout < RUNTIME_LIMITS.timeoutMin || timeout > RUNTIME_LIMITS.timeoutMax) {
    runtimeIssue(issues, 'jev.timeout_seconds 必须是 [' + RUNTIME_LIMITS.timeoutMin + ', ' +
      RUNTIME_LIMITS.timeoutMax + '] 内的有限数值',
    'jev.timeout_seconds must be a finite number in [' + RUNTIME_LIMITS.timeoutMin + ', ' +
      RUNTIME_LIMITS.timeoutMax + ']');
  }
  const instructions = hasOwnField(raw, 'instructions')
    ? raw.instructions : RUNTIME_DEFAULTS.instructions;
  if (typeof instructions !== 'string' || !instructions.trim()) {
    runtimeIssue(issues, 'jev.instructions 不能为空', 'jev.instructions must be nonempty');
  }
  const stateTemplate = hasOwnField(raw, 'state_template')
    ? raw.state_template : RUNTIME_DEFAULTS.state_template;
  if (typeof stateTemplate !== 'string') {
    runtimeIssue(issues, 'jev.state_template 必须是字符串', 'jev.state_template must be a string');
  } else {
    templateFieldIssues(stateTemplate, 'jev.state_template', issues);
  }
  if (id === null || baseUrl === null) return null;
  /* 键顺序与 Python snapshot_dict() 一致：id 在前、key_env 在 model 之后。 */
  return {
    id: id,
    base_url: baseUrl,
    model: model,
    key_env: keyEnv,
    timeout_seconds: timeout,
    instructions: instructions,
    state_template: stateTemplate
  };
}

function normalizeRuntimeWindow(raw, issues) {
  if (!isRecord(raw)) {
    runtimeIssue(issues, 'jev_window 必须是对象', 'jev_window must be an object');
    return null;
  }
  runtimeKeys(raw, JEV_WINDOW_FIELDS, 'jev_window', issues);
  const input = raw.input_tokens;
  const context = raw.context_tokens;
  const inputOk = typeof input === 'number' && Number.isInteger(input);
  const contextOk = typeof context === 'number' && Number.isInteger(context);
  if (!inputOk) {
    runtimeIssue(issues, 'jev_window.input_tokens 必须是整数', 'jev_window.input_tokens must be an integer');
  } else if (input < RUNTIME_LIMITS.inputTokensMin || input > RUNTIME_LIMITS.maxTokens) {
    runtimeIssue(issues, 'jev_window.input_tokens 必须在 ' + RUNTIME_LIMITS.inputTokensMin +
      '..' + RUNTIME_LIMITS.maxTokens + ' 之间',
    'jev_window.input_tokens must be in ' + RUNTIME_LIMITS.inputTokensMin +
      '..' + RUNTIME_LIMITS.maxTokens);
  }
  if (!contextOk) {
    runtimeIssue(issues, 'jev_window.context_tokens 必须是整数', 'jev_window.context_tokens must be an integer');
  } else if (context < RUNTIME_LIMITS.contextTokensMin || context > RUNTIME_LIMITS.maxTokens) {
    runtimeIssue(issues, 'jev_window.context_tokens 必须在 ' + RUNTIME_LIMITS.contextTokensMin +
      '..' + RUNTIME_LIMITS.maxTokens + ' 之间',
    'jev_window.context_tokens must be in ' + RUNTIME_LIMITS.contextTokensMin +
      '..' + RUNTIME_LIMITS.maxTokens);
  }
  if (inputOk && contextOk && input > context) {
    runtimeIssue(issues, 'jev_window.input_tokens 不能大于 context_tokens',
      'jev_window.input_tokens cannot exceed context_tokens');
  }
  return { input_tokens: input, context_tokens: context };
}

/* Python TimePeriod.from_dict / __post_init__ 的字段与校验。 */
function normalizeRuntimePeriods(raw, issues) {
  if (!Array.isArray(raw)) {
    runtimeIssue(issues, 'time_periods 必须是数组', 'time_periods must be a list');
    return [];
  }
  const periods = [];
  const seen = Object.create(null);
  const expectedKeys = TIME_PERIOD_FIELDS.slice().sort().join(',');
  raw.forEach((item, index) => {
    const label = 'time_periods[' + index + ']';
    if (!isRecord(item)) {
      runtimeIssue(issues, label + ' 必须是对象', label + ' must be an object');
      return;
    }
    if (Object.keys(item).sort().join(',') !== expectedKeys) {
      runtimeIssue(issues, label + ' 必须且只能包含 name/days/start/end',
        label + ' needs exactly name/days/start/end');
      return;
    }
    const name = item.name;
    if (typeof name !== 'string' || !name.trim()) {
      runtimeIssue(issues, label + '.name 不能为空', label + '.name must be nonempty');
    } else if (seen[name]) {
      runtimeIssue(issues, '时段名称重复：' + name, 'Duplicate period name: ' + name);
    } else {
      seen[name] = true;
    }
    const days = item.days;
    const daySeen = Object.create(null);
    if (!Array.isArray(days) || !days.length) {
      runtimeIssue(issues, label + '.days 必须是非空数组', label + '.days must be a non-empty list');
    } else {
      days.forEach(day => {
        if (typeof day !== 'number' || !Number.isInteger(day) || day < 0 || day > 6) {
          runtimeIssue(issues, label + '.days 只能是 0..6 的整数', label + '.days must contain weekdays in [0,6]');
          return;
        }
        if (daySeen[day]) {
          runtimeIssue(issues, label + '.days 不能重复', label + '.days must be unique');
        }
        daySeen[day] = true;
      });
    }
    const times = ['start', 'end'];
    times.forEach(field => {
      const value = item[field];
      const match = typeof value === 'string' ? /^(\d{1,2}):(\d{1,2})$/.exec(value) : null;
      const hour = match ? Number(match[1]) : -1;
      const minute = match ? Number(match[2]) : -1;
      if (!match || hour > 23 || minute > 59) {
        runtimeIssue(issues, label + '.' + field + ' 必须使用 HH:MM',
          label + '.' + field + ' must use HH:MM');
      }
    });
    periods.push({ name: name, days: days, start: item.start, end: item.end });
  });
  return periods;
}

/* 严格解析四字段 → 规范化 runtime（默认值补齐）；任何 graph_parse_error 时返回 null。 */
function normalizeRuntime(raw, issues) {
  if (!isRecord(raw)) {
    runtimeIssue(issues, '运行配置必须是对象', 'Runner config must be an object');
    return null;
  }
  const before = issues.length;
  runtimeKeys(raw, RUNTIME_FIELDS, 'runtime', issues);
  const missing = RUNTIME_FIELDS.filter(field => !hasOwnField(raw, field));
  if (missing.length) {
    runtimeIssue(issues, '运行配置字段必须成组出现，缺少：' + missing.join(', '),
      'Runtime fields must appear together; missing: ' + missing.join(', '));
    return null;
  }
  let providers = [];
  let models = [];
  if (!isRecord(raw.backends)) {
    runtimeIssue(issues, 'backends 必须是对象', 'backends must be an object');
  } else {
    runtimeKeys(raw.backends, BACKENDS_FIELDS, 'backends', issues);
    providers = normalizeRuntimeProviders(raw.backends.providers, issues);
    models = normalizeRuntimeModels(raw.backends.models, providers, issues);
  }
  const jev = normalizeRuntimeJev(raw.jev, issues);
  const window = normalizeRuntimeWindow(raw.jev_window, issues);
  const periods = normalizeRuntimePeriods(raw.time_periods, issues);
  if (issues.length !== before) return null;
  return {
    backends: { providers: providers, models: models },
    jev: jev,
    jev_window: window,
    time_periods: periods
  };
}

function pickRuntimeFields(value) {
  const out = {};
  RUNTIME_FIELDS.forEach(field => { out[field] = value[field]; });
  return out;
}

/* 内部树（嵌套 runtime）或 API envelope（扁平四字段）里取 runtime；无 runtime 返回 null。
   严格契约下内部树恒带嵌套 runtime；扁平模板只出现在保存/试算 envelope 里。
   返回值是「四字段容器」，未做字段校验（validateRuntime 负责）。 */
function runtimeOf(value) {
  if (!isRecord(value)) return null;
  if (isRecord(value.runtime)) return value.runtime;
  if (RUNTIME_FIELDS.some(field => hasOwnField(value, field))) return value;
  return null;
}

function hasRuntime(value) {
  return isRecord(runtimeOf(value));
}

/* 对「四字段容器」做成组校验 + 严格解析；flat 时先剔除 envelope 的其它键。 */
function addRuntimeGroupIssues(raw, issues, flat) {
  const local = [];
  const missing = RUNTIME_FIELDS.filter(field => !hasOwnField(raw, field));
  if (missing.length) {
    runtimeIssue(local, '运行配置字段必须成组出现，缺少：' + missing.join(', '),
      'Runtime fields must appear together; missing: ' + missing.join(', '));
  } else {
    normalizeRuntime(flat ? pickRuntimeFields(raw) : raw, local);
  }
  local.forEach(item => issues.push(item));
}

/* validate / validateRuntime 共用：只接受完整运行配置（嵌套 runtime 或扁平四字段）。
   `runtime: null` 与「完全没有运行字段」都是旧文档形态，一律报硬错误：
   前端不再维护「保存时再物化」的第二条路径。 */
function addRuntimeEnvelopeIssues(value, issues) {
  const hasNested = hasOwnField(value, 'runtime');
  if (hasNested) {
    if (!isRecord(value.runtime)) {
      runtimeIssue(issues, '运行配置必须是对象：不接受 runtime:null（旧文档请重新导入完整 v4 文档）',
        'Runner config must be an object: runtime:null is not accepted (re-import a complete v4 document)');
      return;
    }
    addRuntimeGroupIssues(value.runtime, issues, false);
    return;
  }
  addRuntimeGroupIssues(value, issues, true);
}

/* 公开 API：合法返回 []；支持 runtime 对象、内部树（嵌套）与 API envelope（扁平）。
   缺少运行配置或 runtime:null 都是 graph_parse_error（保存/启用前的硬错误）。 */
function validateRuntime(value) {
  const issues = [];
  if (!isRecord(value)) {
    runtimeIssue(issues, '运行配置必须是对象', 'Runner config must be an object');
    return finishIssues(issues);
  }
  addRuntimeEnvelopeIssues(value, issues);
  return finishIssues(issues);
}

/* 导出/保存前的完整性守卫：缺字段或部分运行字段 → 抛带字段名的明确错误。
   serialize() 与 controller 的保存 payload 共用（服务端同样会 422）。 */
function requireCompleteRuntime(value, action) {
  const runtime = runtimeOf(value);
  const missing = RUNTIME_FIELDS.filter(field => !runtime || !hasOwnField(runtime, field));
  if (missing.length) {
    throw new Error('画布缺少运行配置字段（' + action + '）：' + missing.join(', ') +
      '；四字段 backends/jev/jev_window/time_periods 必须成组出现');
  }
  const issues = [];
  const normalized = normalizeRuntime(pickRuntimeFields(runtime), issues);
  if (normalized === null) {
    const first = issues.find(item => item.code === 'graph_parse_error') || issues[0];
    throw new Error('画布运行配置非法（' + action + '）：' + (first ? first.zh : 'invalid runtime'));
  }
  return runtimeView(normalized);
}

/* Python _behavior_numbers 的 JS 等价：整数值数字输出整数；非整数用 pyFloat
   包装成 Python repr（JSON.parse 无法区分 10 与 10.0，统一按整数归一）。 */
function runtimeBehaviorNumbers(value) {
  if (typeof value === 'number') return Number.isInteger(value) ? value : pyFloat(value);
  if (Array.isArray(value)) return value.map(runtimeBehaviorNumbers);
  if (isRecord(value)) {
    const out = {};
    Object.keys(value).forEach(key => { out[key] = runtimeBehaviorNumbers(value[key]); });
    return out;
  }
  return value;
}

/* runtime → 只含契约字段的深拷贝投影（剔除未知/派生字段），保持数组输入顺序。 */
function runtimeView(runtime) {
  const backends = isRecord(runtime) && isRecord(runtime.backends) ? runtime.backends : {};
  const providers = (Array.isArray(backends.providers) ? backends.providers : [])
    .filter(isRecord)
    .map(provider => ({
      name: provider.name,
      base_url: provider.base_url,
      key_env: provider.key_env,
      reasoning_format: provider.reasoning_format
    }));
  const models = (Array.isArray(backends.models) ? backends.models : [])
    .filter(isRecord)
    .map(model => ({
      id: model.id,
      upstream: model.upstream,
      provider: model.provider,
      profile: isRecord(model.profile) ? withoutSecretFields(model.profile) : {}
    }));
  const jevRaw = isRecord(runtime) && isRecord(runtime.jev) ? runtime.jev : null;
  const jev = jevRaw === null ? null : {
    id: jevRaw.id,
    base_url: jevRaw.base_url,
    model: jevRaw.model,
    key_env: hasOwnField(jevRaw, 'key_env') ? jevRaw.key_env : RUNTIME_DEFAULTS.key_env,
    timeout_seconds: hasOwnField(jevRaw, 'timeout_seconds')
      ? jevRaw.timeout_seconds : RUNTIME_DEFAULTS.timeout_seconds,
    instructions: hasOwnField(jevRaw, 'instructions')
      ? jevRaw.instructions : RUNTIME_DEFAULTS.instructions,
    state_template: hasOwnField(jevRaw, 'state_template')
      ? jevRaw.state_template : RUNTIME_DEFAULTS.state_template
  };
  const windowRaw = isRecord(runtime) && isRecord(runtime.jev_window) ? runtime.jev_window : {};
  const periods = (isRecord(runtime) && Array.isArray(runtime.time_periods)
    ? runtime.time_periods : [])
    .filter(isRecord)
    .map(period => ({
      name: period.name,
      days: Array.isArray(period.days) ? period.days.slice() : [],
      start: period.start,
      end: period.end
    }));
  return {
    backends: { providers: providers, models: models },
    jev: jev,
    jev_window: {
      input_tokens: windowRaw.input_tokens,
      context_tokens: windowRaw.context_tokens
    },
    time_periods: periods
  };
}

/* 契约投影：tree / runtime 对象 / 扁平 envelope 均可，无 runtime 返回 null。
   canonical 用它做字段/顺序归一，数字是原生 JS number（Python repr 由
   behaviorCanonical 里的 runtimeBehaviorNumbers 处理）。 */
function runtimeBehaviorView(value) {
  const runtime = runtimeOf(value);
  return runtime ? runtimeView(runtime) : null;
}

/* 导出用投影（serialize / 保存 payload）：与 runtimeBehaviorView 同一份字段集合、
   同样接受 tree / runtime 对象 / 扁平 envelope；无 runtime 返回 null。 */
function runtimeDocumentView(value) {
  const runtime = runtimeOf(value);
  return runtime ? runtimeView(runtime) : null;
}

/* ── 6. 行为 canonical / changeKind（§2.8） ─────────────────────────── */

/* 已下线的 target 字段：旧文档仍可导入，但不在草稿/导出里继续携带。 */
function stripRemovedTargetFields(root) {
  if (!isRecord(root) || !Array.isArray(root.nodes)) return root;
  root.nodes.forEach(node => {
    if (!isRecord(node) || node.type !== 'target') return;
    delete node.strategy;
    delete node.capability;
  });
  return root;
}

function normalizedNode(node, includeDisplay) {
  const output = { id: String(node.id == null ? '' : node.id), type: node.type };
  if (node.type === 'question') {
    output.question_type = node.question_type;
    output.instructions = node.instructions;
    /* 与 Python graph_behavior_dict 对齐：只投影该问题类型真正携带的字段，
       noul 不出现 options/criteria，choice 不出现 criteria，score 不出现 options。
       惰性空字段不进入 behavior，避免语义无变化的编辑 bump revision。 */
    if (node.question_type === 'choice' && Array.isArray(node.options) && node.options.length) {
      output.options = node.options.map(option => ({
        key: option && option.key,
        label: option && option.label,
        capability: option && option.capability
      }));
    } else if (node.question_type === 'score' && node.criteria != null) {
      output.criteria = Array.isArray(node.criteria)
        ? node.criteria.map(level => (typeof level === 'number' ? pyFloat(level) : level))
        : node.criteria;
    }
  } else if (node.type === 'logic') {
    output.operator = node.operator;
  } else if (node.type === 'condition') {
    output.source = node.source;
    if (Array.isArray(node.contains_any) && node.contains_any.length) {
      output.contains_any = node.contains_any.slice();
    }
    if (Array.isArray(node.periods) && node.periods.length) {
      output.periods = node.periods.slice();
    }
  } else if (node.type === 'target') {
    output.models = (Array.isArray(node.models) ? node.models : []).map(item => ({
      model: item && item.model,
      reasoning: item && item.reasoning != null ? item.reasoning : null
    }));
    output.priority = node.priority;
  }
  if (includeDisplay) {
    output.title = node.title == null ? '' : node.title;
    output.position = isRecord(node.position) &&
      isFiniteNumber(node.position.x) && isFiniteNumber(node.position.y)
      ? { x: node.position.x, y: node.position.y }
      : null;
  }
  return output;
}

function normalizedEdge(edge) {
  const condition = edge.condition;
  return {
    source: edge.source,
    source_port: edge.source_port,
    target: edge.target,
    target_port: edge.target_port,
    condition: condition == null ? null : {
      op: condition.op,
      /* 数值阈值在 Python 侧是 float（1 → "1.0"），布尔 is 保持 true/false。 */
      value: typeof condition.value === 'number' ? pyFloat(condition.value) : condition.value
    }
  };
}

function sortNodes(nodes) {
  return nodes.slice().sort((a, b) => {
    if (a.id < b.id) return -1;
    if (a.id > b.id) return 1;
    return 0;
  });
}

function sortEdges(edges) {
  return edges.slice().sort((a, b) => {
    if (a.target !== b.target) return a.target < b.target ? -1 : 1;
    const slotA = slotSortKey(a.target_port);
    const slotB = slotSortKey(b.target_port);
    if (slotA !== slotB) return slotA - slotB;
    if (a.source !== b.source) return a.source < b.source ? -1 : 1;
    if (a.source_port !== b.source_port) return a.source_port < b.source_port ? -1 : 1;
    return 0;
  });
}

function rootView(graph, includeDisplay) {
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  return {
    type: 'graph',
    error_fallback: graph.error_fallback == null ? null : graph.error_fallback,
    nodes: sortNodes(nodes.filter(node => isRecord(node)).map(node => normalizedNode(node, includeDisplay))),
    edges: sortEdges(edges.filter(edge => isRecord(edge)).map(normalizedEdge))
  };
}

/* Python len()/sorted() 的 Unicode 语义：按码点（而不是 UTF-16 码元）计数与排序。
   非 BMP 字符（如 emoji）在 UTF-16 里是代理对，`.length` 与默认 `sort()` 会与服务端
   码点口径分歧，进而让 behavior canonical / behavior_sha256 跨运行时不一致。 */
function compareCodePoints(left, right) {
  const a = Array.from(left);
  const b = Array.from(right);
  const length = Math.min(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    const difference = a[index].codePointAt(0) - b[index].codePointAt(0);
    if (difference !== 0) return difference;
  }
  return a.length - b.length;
}

/* 部门规范化：唯一实现。导入、API envelope 与 createMinimalTree 共用；
   失败一律抛 Error，由调用方（parseImport）转成 graph_parse_error。 */
function normalizeDepartments(value) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new Error('departments must be a list of strings');
  const values = value.map(item => {
    if (typeof item !== 'string') throw new Error('departments must be a list of strings');
    const department = item.trim();
    /* 与 Python 的 len(value) > 64 及控制字符检查一样按码点扫描。 */
    const codePoints = Array.from(department);
    if (!codePoints.length || codePoints.length > 64 ||
        codePoints.some(char => char.codePointAt(0) < 0x20 || char.codePointAt(0) === 0x7f)) {
      throw new Error('department is empty, too long, or contains control characters');
    }
    return department;
  });
  if (new Set(values).size !== values.length) throw new Error('duplicate department');
  return values.sort(compareCodePoints);
}

/* 宽松版本：行为/可编辑 canonical 必须永远可计算（changeKind 对任意草稿调用），
   非法部门在 validate 里作为硬 Issue 报告，不在这里抛异常。 */
function departmentsOf(tree) {
  try {
    return normalizeDepartments(isRecord(tree) ? tree.departments : undefined);
  } catch (error) {
    return [];
  }
}

function envelopeParts(tree) {
  const value = isRecord(tree) ? tree : {};
  const sticky = isRecord(value.sticky) ? value.sticky : {};
  return {
    name: value.name == null ? '' : value.name,
    departments: departmentsOf(value),
    timezone: isNonEmptyString(value.timezone) ? value.timezone : 'Asia/Shanghai',
    sticky: {
      enabled: sticky.enabled === true,
      ttl_seconds: isFiniteNumber(Number(sticky.ttl_seconds)) ? Number(sticky.ttl_seconds) : 1800
    },
    root: graphOf(value) || { id: 'root', type: 'graph', nodes: [], edges: [] }
  };
}

function behaviorCanonical(tree) {
  const parts = envelopeParts(tree);
  const view = {
    departments: parts.departments,
    timezone: parts.timezone,
    sticky: parts.sticky,
    root: rootView(parts.root, false)
  };
  /* 自包含运行配置属于行为：四字段参与 canonical（与服务端 tree_behavior_dict 同序）。
     整数值数字归一为 int、非整数走 Python repr，保证与 tree_revision_of 逐字节一致。 */
  const runtime = runtimeBehaviorView(tree);
  if (runtime) Object.assign(view, runtimeBehaviorNumbers(runtime));
  return stableStringify(view);
}

function editableCanonical(tree, stripPosition) {
  const parts = envelopeParts(tree);
  const view = rootView(parts.root, true);
  if (stripPosition) {
    view.nodes.forEach(node => { delete node.position; });
  }
  const canonical = {
    name: parts.name,
    departments: parts.departments,
    timezone: parts.timezone,
    sticky: parts.sticky,
    root: view
  };
  /* runtime 变更属于 behavior（不是 display/layout），但必须参与 clean 判定。 */
  const runtime = runtimeBehaviorView(tree);
  if (runtime) canonical.runtime = runtime;
  return stableStringify(canonical);
}

function changeKind(before, after) {
  if (editableCanonical(before, false) === editableCanonical(after, false)) return 'clean';
  if (behaviorCanonical(before) !== behaviorCanonical(after)) return 'behavior';
  if (editableCanonical(before, true) !== editableCanonical(after, true)) return 'display';
  return 'layout';
}

/* ── 7. 自动布局（DAG 分层；只写 position） ────────────────────────── */

function layoutPositions(mode, graphValue) {
  const graph = graphOf(graphValue) || graphValue || { nodes: [], edges: [] };
  const nodes = (Array.isArray(graph.nodes) ? graph.nodes : []).filter(node => isRecord(node) && isNonEmptyString(node.id));
  const adjacency = Object.create(null);
  const indegree = Object.create(null);
  nodes.forEach(node => {
    adjacency[node.id] = [];
    indegree[node.id] = 0;
  });
  edgesOf(graph).forEach(edge => {
    if (adjacency[edge.source] && indegree[edge.target] !== undefined) {
      adjacency[edge.source].push(edge.target);
      indegree[edge.target] += 1;
    }
  });
  const depth = Object.create(null);
  nodes.forEach(node => { depth[node.id] = 0; });
  const queue = Object.keys(indegree).filter(id => indegree[id] === 0).sort();
  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const id = queue[cursor];
    adjacency[id].forEach(target => {
      depth[target] = Math.max(depth[target], depth[id] + 1);
      indegree[target] -= 1;
      if (indegree[target] === 0) queue.push(target);
    });
  }
  const columns = Object.create(null);
  nodes.forEach(node => {
    const column = Number.isFinite(depth[node.id]) ? depth[node.id] : 0;
    (columns[column] = columns[column] || []).push(node);
  });
  const positions = Object.create(null);
  Object.keys(columns).map(Number).sort((a, b) => a - b).forEach(column => {
    columns[column].sort((a, b) => {
      const orderA = NODE_ORDER[a.type] == null ? 9 : NODE_ORDER[a.type];
      const orderB = NODE_ORDER[b.type] == null ? 9 : NODE_ORDER[b.type];
      if (orderA !== orderB) return orderA - orderB;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
    columns[column].forEach((node, index) => {
      positions[node.id] = { x: 30 + column * 220, y: 30 + index * 118 };
    });
  });
  return positions;
}

/* ── 8. 导入 / 导出 ────────────────────────────────────────────────── */

/* 顶层 canvas → 内部 node.position（文件文档与 API envelope 共用同一边界）。 */
function dispatchCanvas(tree) {
  const canvas = isRecord(tree) && isRecord(tree.canvas) ? tree.canvas : {};
  const root = isRecord(tree) && isRecord(tree.root) ? tree.root : { nodes: [] };
  (Array.isArray(root.nodes) ? root.nodes : []).forEach(node => {
    if (!isRecord(node)) return;
    /* 节点 id 来自用户数据：用 hasOwnProperty 避免 constructor / __proto__ 命中原型链。 */
    const coords = isNonEmptyString(node.id) &&
      Object.prototype.hasOwnProperty.call(canvas, node.id) ? canvas[node.id] : null;
    if (isRecord(coords) && isFiniteNumber(coords.x) && isFiniteNumber(coords.y)) {
      node.position = { x: coords.x, y: coords.y };
    }
  });
  return tree;
}

/* 内部 node.position → 顶层 canvas（导出前调用；就地修改并返回 tree）。 */
/* 顶层 canvas 收集器。`round` 只用于文件导出（旧 serialize 的整洁格式）；
   API 保存必须保留原始数值——服务端原样存回，取整会让回包坐标与草稿不一致，
   草稿永远判成 layout dirty。 */
function collectCanvas(tree, options) {
  const round = !!(options && options.round);
  const root = isRecord(tree) && isRecord(tree.root) ? tree.root : null;
  /* 节点 id 来自用户数据：用无原型对象，避免 __proto__ 之类的 id 丢失坐标。 */
  const canvas = Object.create(null);
  if (root && Array.isArray(root.nodes)) {
    root.nodes.forEach(node => {
      if (!isRecord(node)) return;
      const position = node.position;
      if (isNonEmptyString(node.id) && isRecord(position) &&
          isFiniteNumber(position.x) && isFiniteNumber(position.y)) {
        canvas[node.id] = round
          ? { x: Math.round(position.x), y: Math.round(position.y) }
          : { x: position.x, y: position.y };
      }
      delete node.position;
    });
  }
  if (Object.keys(canvas).length) tree.canvas = canvas; else delete tree.canvas;
  return tree;
}

function serialize(tree) {
  const parts = envelopeParts(tree);
  const document = {
    kind: KIND,
    version: VERSION,
    name: parts.name,
    departments: parts.departments,
    enabled: isRecord(tree) && tree.enabled === true,
    default: isRecord(tree) && tree.default === true,
    timezone: parts.timezone,
    sticky: parts.sticky,
    root: stripRemovedTargetFields(clone(parts.root))
  };
  /* 自包含文档：四字段无条件平铺（jev 可为 null）；缺字段/部分字段时抛明确错误，
     与 parseImport、服务端 tree_from_dict 一样不维护「旧文档」导出分支。 */
  Object.assign(document, requireCompleteRuntime(tree, '导出'));
  /* 导出：节点位置收集进顶层 canvas，节点对象只留逻辑字段；没有坐标时不输出 canvas。 */
  return JSON.stringify(collectCanvas(document, { round: true }), null, 2);
}

function parseImport(text) {
  if (typeof text !== 'string') {
    return { ok: false, error: '导入内容必须是 JSON 字符串', issues: [] };
  }
  let data;
  try {
    data = JSON.parse(text);
  } catch (error) {
    return { ok: false, error: 'JSON 解析失败：' + error.message, issues: [] };
  }
  if (!isRecord(data) || data.kind !== KIND || data.version !== VERSION) {
    return { ok: false, error: REJECT_MESSAGE, issues: [] };
  }
  const unknownImportFields = Object.keys(data).filter(key => IMPORT_FIELDS.indexOf(key) < 0).sort();
  if (unknownImportFields.length) {
    const text = '导入 envelope 含未知字段：' + unknownImportFields.join(', ');
    return {
      ok: false,
      error: text,
      issues: [issue('graph_parse_error', text, 'Import envelope has unknown fields: ' + unknownImportFields.join(', '))],
    };
  }
  /* canvas 与四字段 runtime 都是可选顶层字段：缺失不报，成组规则单独校验。 */
  const missingImportFields = IMPORT_FIELDS
    .filter(field => field !== 'canvas' && RUNTIME_FIELDS.indexOf(field) < 0)
    .filter(field => !Object.prototype.hasOwnProperty.call(data, field)).sort();
  if (missingImportFields.length) {
    const fields = missingImportFields.join(', ');
    return {
      ok: false,
      error: PARSE_ERROR_ZH,
      issues: [issue(
        'graph_parse_error',
        '图解析失败：导入 envelope 缺少必需字段：' + fields,
        'Graph parse failed: import envelope is missing required fields: ' + fields
      )]
    };
  }
  /* v4 envelope：enabled/default 必须是 boolean 且 default => enabled；
     departments 必须能规范化（失败转成 graph_parse_error，绝不让异常中断导入）。 */
  const lifecycleIssues = [];
  if (typeof data.enabled !== 'boolean') {
    lifecycleIssues.push(issue('graph_parse_error', 'enabled 必须是 boolean',
      'enabled must be a boolean'));
  }
  if (typeof data.default !== 'boolean') {
    lifecycleIssues.push(issue('graph_parse_error', 'default 必须是 boolean',
      'default must be a boolean'));
  }
  if (data.default === true && data.enabled !== true) {
    lifecycleIssues.push(issue('graph_parse_error', 'default graph must be enabled',
      'default graph must be enabled'));
  }
  let departments = [];
  try {
    departments = normalizeDepartments(data.departments);
  } catch (error) {
    lifecycleIssues.push(issue('graph_parse_error', 'departments 非法：' + error.message,
      'Invalid departments: ' + error.message));
  }
  if (lifecycleIssues.length) {
    return { ok: false, error: PARSE_ERROR_ZH, issues: finishIssues(lifecycleIssues) };
  }
  /* 密钥与派生的密钥状态只存在于服务端：文档任意层级出现即整体拒绝。 */
  const secretField = findSecretField(data);
  if (secretField) {
    const textZh = '导入文档不允许出现密钥字段：' + secretField;
    return {
      ok: false,
      error: textZh,
      issues: [issue(
        'graph_parse_error',
        textZh,
        'Import document must not contain secret fields: ' + secretField
      )]
    };
  }
  const raw = data.root;
  if (!isRecord(raw) || raw.type !== 'graph' || !Array.isArray(raw.nodes) || !Array.isArray(raw.edges)) {
    /* kind/version 正确的 v4 但 root 形状非法：按 §5.2 报 graph_parse_error（zh/en），
       固定拒绝文案只留给旧 jev / version 2 / 错误 kind。 */
    return {
      ok: false,
      error: PARSE_ERROR_ZH,
      issues: [issue('graph_parse_error', PARSE_ERROR_ZH, PARSE_ERROR_EN)]
    };
  }
  const graph = stripRemovedTargetFields(clone(raw));
  graph.id = isNonEmptyString(graph.id) ? graph.id : 'root';
  graph.type = 'graph';
  /* 非对象条目不做静默丢弃：保留在 tree 中交给 validate 报 graph_parse_error。 */
  if (!graph.error_fallback && graph.error_fallback !== null) graph.error_fallback = null;

  /* 严格单格式：节点对象不再接受 position，画布坐标只能来自顶层 canvas。 */
  const legacyPositions = graph.nodes.filter(node =>
    isRecord(node) && Object.prototype.hasOwnProperty.call(node, 'position'));
  if (legacyPositions.length) {
    const ids = legacyPositions.map(node => String(node.id || '')).sort().join(', ');
    return {
      ok: false,
      error: PARSE_ERROR_ZH,
      issues: [issue(
        'graph_parse_error',
        '图解析失败：节点对象不再接受 position 字段（画布坐标请改用顶层 canvas）：' + ids,
        'Graph parse failed: node objects no longer accept a position field (use the top-level canvas): ' + ids
      )]
    };
  }

  /* 四字段必须成组出现（严格自包含）：全缺失或部分缺失都整体拒绝，绝不再接受
     「旧文档」并交给数据库补齐。 */
  const missingRuntime = RUNTIME_FIELDS.filter(field => !hasOwnField(data, field)).sort();
  if (missingRuntime.length) {
    const textZh = '运行配置字段必须成组出现，缺少：' + missingRuntime.join(', ');
    return {
      ok: false,
      error: textZh,
      issues: [issue(
        'graph_parse_error',
        textZh,
        'Runtime fields must appear together; missing: ' + missingRuntime.join(', ')
      )]
    };
  }
  const runtimeIssues = [];
  const runtime = normalizeRuntime(pickRuntimeFields(data), runtimeIssues);
  /* 运行时默认值（jev 缺省键、key_env、reasoning_format）在导入时补齐，
     使 serialize(parseImport(x)) 稳定且行为 canonical 与 Python 一致。 */
  if (runtime === null) {
    return { ok: false, error: PARSE_ERROR_ZH, issues: finishIssues(runtimeIssues) };
  }

  /* canvas 形状/引用校验在分发前完成；坐标集中顶层 canvas，导入时分发回内部 node.position。 */
  const canvasIssues = [];
  validateCanvas(data.canvas, graph, canvasIssues);
  dispatchCanvas({ root: graph, canvas: data.canvas });

  const positions = layoutPositions('dag', graph);
  graph.nodes.forEach(node => {
    if (!isRecord(node)) return;
    const position = node.position;
    const valid = isRecord(position) && isFiniteNumber(position.x) && isFiniteNumber(position.y);
    if (!valid) {
      const fallback = positions[node.id] || { x: 30, y: 30 };
      node.position = { x: fallback.x, y: fallback.y };
    }
  });

  const rawSticky = data.sticky === null ? {} : data.sticky;
  const tree = {
    name: data.name,
    departments: departments,
    enabled: false,
    default: false,
    timezone: data.timezone,
    sticky: rawSticky,
    root: graph,
    /* 导入输出的内部树只带嵌套 runtime；保存/试算时再由 runtimeDocumentView 投影成
       扁平四字段（服务端文档形态），内部不保留任何「无 runtime」中间态。 */
    runtime: runtime
  };
  const issues = finishIssues(canvasIssues.concat(validate(tree)));
  /* 结构错误在应用前拒绝：非对象节点/边或非数组字段会让 renderAll 半途崩溃，
     必须让 applyImport 拿到 ok:false 而不是一个「带 Issue 的成功」。 */
  /* 拒绝文案：存在 graph_parse_error 时沿用固定解析失败文案（既有测试与
     toast 依赖它），其余硬错误（如 invalid_timezone）用其首条 Issue 文案。 */
  const parseError = issues.find(item => item.code === 'graph_parse_error');
  const hard = parseError || issues.find(item => item.level !== 'hint');
  if (hard) return { ok: false, error: parseError ? PARSE_ERROR_ZH : hard.zh, issues: issues };
  /* 合法输入通过校验后再归一 sticky。 */
  tree.sticky = {
    enabled: rawSticky.enabled === true,
    ttl_seconds: rawSticky.ttl_seconds === undefined ? 1800 : rawSticky.ttl_seconds
  };
  return { ok: true, tree: tree, issues: issues };
}

/* ── 9. 新建最小图 ─────────────────────────────────────────────────── */

/* 新建最小图：必须收到完整 runtime（createRuntime()/createMinimalTree 调用方负责），
   模型也必须在 runtime 快照里（Python 保存时 target 引用快照外模型是硬错误）。
   缺 runtime、runtime 非法或模型不在快照里都抛明确错误，不做数据库/默认物化。 */
function createMinimalTree(model, runtime) {
  const targetModel = isNonEmptyString(model)
    ? model
    : (isRecord(runtime) && isRecord(runtime.backends) &&
        Array.isArray(runtime.backends.models) && runtime.backends.models.length
      ? runtime.backends.models[0].id
      : '');
  const issues = [];
  const normalized = isRecord(runtime)
    ? normalizeRuntime(runtime, issues)
    : null;
  if (normalized === null) {
    const first = issues.find(item => item.code === 'graph_parse_error') || issues[0];
    throw new Error('createMinimalTree 需要完整运行配置：' +
      (first ? first.zh : 'runtime must be a complete backends/jev/jev_window/time_periods object'));
  }
  const known = normalized.backends.models.some(item => item && item.id === targetModel);
  if (!known) {
    throw new Error('运行配置中没有模型 ' + String(targetModel) +
      '：画布 target 必须引用快照内模型（runtime is missing model ' + String(targetModel) + '）');
  }
  return {
    name: '新决策组',
    departments: [],
    enabled: false,
    default: false,
    timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    runtime: normalized,
    root: {
      id: 'root',
      type: 'graph',
      nodes: [
        { id: 'start', type: 'start', position: { x: 40, y: 180 } },
        {
          id: 'target-1',
          type: 'target',
          models: [{ model: targetModel }],
          priority: 1,
          position: { x: 420, y: 180 }
        }
      ],
      edges: [
        {
          id: 'e-start-out-target-1-in_1',
          source: 'start',
          source_port: 'out',
          target: 'target-1',
          target_port: 'in_1',
          condition: null
        }
      ],
      error_fallback: 'target-1'
    }
  };
}

/* ── 10. 导出 ──────────────────────────────────────────────────────── */

export {
  VERSION,
  KIND,
  LIMITS,
  NUM_OPS,
  OP,
  LOGIC,
  NODE_TYPES,
  HINTS,
  THRESHOLD_LIMIT,
  REJECT_MESSAGE,
  RUNTIME_FIELDS,
  RUNTIME_SECRET_FIELDS,
  isSecretField,
  withoutSecretFields,
  RUNTIME_DEFAULTS,
  RUNTIME_LIMITS,
  REASONING_FORMATS,
  clone,
  stableStringify,
  normalizeDepartments,
  graphOf,
  findNode,
  portsOf,
  portOf,
  portDesc,
  nodeLabel,
  logicName,
  edgeType,
  conditionLabel,
  conditionInvalidReason,
  defaultCondition,
  evaluateCondition,
  canConnect,
  isTimeZone,
  nextSlot,
  addEdge,
  removeEdge,
  removeNode,
  removeOption,
  renameOptionKey,
  validate,
  behaviorCanonical,
  changeKind,
  runtimeOf,
  hasRuntime,
  validateRuntime,
  runtimeBehaviorView,
  runtimeDocumentView,
  layoutPositions,
  dispatchCanvas,
  collectCanvas,
  serialize,
  parseImport,
  createMinimalTree,
};
