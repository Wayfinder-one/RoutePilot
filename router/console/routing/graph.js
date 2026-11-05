/* RTGraph · 画布 v3 纯函数层（规格 §2.2–§2.8 / §5 / §6.1）
   ------------------------------------------------------------------
   浏览器：<script src="routing/graph.js"></script> → window.RTGraph
   Node：  require('router/console/routing/graph.js') → RTGraph

   约束：
   · 只做纯函数（不读 DOM、不读 window.MOCK、不发请求），可被 node 直测；
   · 所有函数显式接收 graph（root：{id,type,nodes,edges,error_fallback}）
     或完整 envelope（{name,timezone,sticky,root}）；
   · 算法移植自 docs/od-design/routing-canvas-2026-11-02/mock.js，但去掉了固定场景与组状态。
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const VERSION = 3;
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
    or: { symbol: '∨', name: '或 OR' },
    not: { symbol: '¬', name: '非 NOT' }
  };
  const NODE_TYPES = ['start', 'question', 'logic', 'condition', 'target'];
  const NODE_ORDER = { start: 0, condition: 1, question: 2, logic: 3, target: 4 };
  const REJECT_MESSAGE = '只接受 sr-routing-graph v3（本期不做旧格式兼容与转换）';
  const PARSE_ERROR_ZH = '图解析失败：root 不符合 sr-routing-graph v3 结构（未知字段或类型非法）';
  const PARSE_ERROR_EN = 'Graph parse failed: root does not conform to the sr-routing-graph v3 structure (unknown fields or invalid types)';
  const HINTS = [
    '比较条件写在输入槽位上：数值线拖进任何布尔槽位后，点条件即可改成 > 0.75 这类阈值，不需要再放比较门。',
    '同一数值端口可以连到同一个判断门的两个槽位，分别写 > 0.3 与 < 0.8，就是区间条件。',
    '布尔槽位不写条件等价于 = true；要反向判断可以把条件改成 = false，或显式加 NOT 门。',
    '条件阈值 0.65 / 0.70 / 0.60 是示例值：上线前请按实际问题独立校准。'
  ];
  const HINT_EN = [
    'Comparison conditions live on input slots: drop a numeric edge into any boolean slot and edit the threshold (for example > 0.75); no separate comparison node is needed.',
    'Fan the same numeric output into two slots of one AND gate with > 0.3 and < 0.8 to express a range condition.',
    'A boolean slot without a condition means = true; use = false (or an explicit NOT gate) for the opposite test.',
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

  function stableStringify(value) {
    if (value === undefined) return 'null';
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
    if (node.type === 'logic' && node.operator !== 'not') minSlots = 2;
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
          ? '布尔输入槽位（多个槽位按 OR）。数值线需要配置比较条件；布尔线可缺省 = true。'
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
      return '布尔输入条件只能是 = true 或 = false';
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

  /* ── 导入契约白名单：未知字段/未知类型/非法时区一律 graph_parse_error / invalid_timezone ── */
  const IMPORT_FIELDS = ['kind', 'version', 'name', 'timezone', 'sticky', 'root'];
  const ENVELOPE_FIELDS = ['id', 'name', 'enabled', 'timezone', 'sticky', 'root', 'tree_revision', 'updated_at'];
  /* otherwise_target stays readable for pre-merge v3 imports, then is removed. */
  const ROOT_FIELDS = ['id', 'type', 'nodes', 'edges', 'otherwise_target', 'error_fallback'];
  const NODE_FIELDS = {
    start: ['id', 'type', 'title', 'position'],
    question: ['id', 'type', 'question_type', 'instructions', 'title', 'options', 'criteria', 'position'],
    logic: ['id', 'type', 'operator', 'title', 'position'],
    condition: ['id', 'type', 'source', 'contains_any', 'periods', 'title', 'position'],
    target: ['id', 'type', 'models', 'priority', 'strategy', 'capability', 'title', 'position'],
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

  function validateContract(value, graph, issues) {
    addUnknownFields(graph, ROOT_FIELDS, 'graph', issues, '');
    graph.nodes.filter(isRecord).forEach(node => {
      const allowed = Object.prototype.hasOwnProperty.call(NODE_FIELDS, node.type) ? NODE_FIELDS[node.type] : null;
      if (allowed) addUnknownFields(node, allowed, node.type + ' node', issues, String(node.id || ''));
      if (isRecord(node.position)) {
        addUnknownFields(node.position, ['x', 'y'], 'position', issues, String(node.id || ''));
      }
      if (node.type === 'question' && QUESTION_TYPES.indexOf(node.question_type) < 0) {
        issues.push(issue('graph_parse_error', '未知问题类型：' + String(node.question_type),
          'Unknown question type: ' + String(node.question_type), { node_id: String(node.id || '') }));
      }
      if (node.type === 'condition' && ['text', 'periods'].indexOf(node.source) < 0) {
        issues.push(issue('graph_parse_error', '未知条件来源：' + String(node.source),
          'Unknown condition source: ' + String(node.source), { node_id: String(node.id || '') }));
      }
      if (node.type === 'target' && node.strategy != null && node.strategy !== 'least_inflight') {
        issues.push(issue('graph_parse_error', 'target.strategy 必须为 least_inflight',
          'target.strategy must be least_inflight', { node_id: String(node.id || '') }));
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
      if (node.operator === 'not' && count !== 1) {
        issues.push(issue(
          'logic_slot_count',
          'NOT 必须恰好有 1 个输入（当前 ' + count + ' 个）：' + node.id,
          'NOT needs exactly 1 input (found ' + count + '): ' + node.id,
          { node_id: node.id }
        ));
      }
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

  /* ── 6. 行为 canonical / changeKind（§2.8） ─────────────────────────── */

  function normalizedNode(node, includeDisplay) {
    const output = { id: String(node.id == null ? '' : node.id), type: node.type };
    if (node.type === 'question') {
      output.question_type = node.question_type;
      output.instructions = node.instructions;
      output.options = (Array.isArray(node.options) ? node.options : []).map(option => ({
        key: option && option.key,
        label: option && option.label,
        capability: option && option.capability
      }));
      output.criteria = Array.isArray(node.criteria) ? node.criteria.slice() : null;
    } else if (node.type === 'logic') {
      output.operator = node.operator;
    } else if (node.type === 'condition') {
      output.source = node.source;
      output.contains_any = Array.isArray(node.contains_any) ? node.contains_any.slice() : [];
      output.periods = Array.isArray(node.periods) ? node.periods.slice() : [];
    } else if (node.type === 'target') {
      output.models = (Array.isArray(node.models) ? node.models : []).map(item => ({
        model: item && item.model,
        reasoning: item && item.reasoning != null ? item.reasoning : null
      }));
      output.priority = node.priority;
      output.strategy = node.strategy || 'least_inflight';
    }
    if (includeDisplay) {
      output.title = node.title == null ? '' : node.title;
      if (node.type === 'target') output.capability = node.capability == null ? '' : node.capability;
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
        value: condition.value
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

  function envelopeParts(tree) {
    const value = isRecord(tree) ? tree : {};
    const sticky = isRecord(value.sticky) ? value.sticky : {};
    return {
      name: value.name == null ? '' : value.name,
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
    return stableStringify({
      timezone: parts.timezone,
      sticky: parts.sticky,
      root: rootView(parts.root, false)
    });
  }

  function editableCanonical(tree, stripPosition) {
    const parts = envelopeParts(tree);
    const view = rootView(parts.root, true);
    if (stripPosition) {
      view.nodes.forEach(node => { delete node.position; });
    }
    return stableStringify({
      name: parts.name,
      timezone: parts.timezone,
      sticky: parts.sticky,
      root: view
    });
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

  function serialize(tree) {
    const parts = envelopeParts(tree);
    const rootValue = clone(parts.root);
    (Array.isArray(rootValue.nodes) ? rootValue.nodes : []).forEach(node => {
      if (!isRecord(node)) return;
      const position = node.position;
      if (isRecord(position) && isFiniteNumber(position.x) && isFiniteNumber(position.y)) {
        node.position = { x: Math.round(position.x), y: Math.round(position.y) };
      } else {
        /* 非法坐标不当作已定位：导出时丢弃，导入会按 DAG 重新布局。 */
        delete node.position;
      }
    });
    return JSON.stringify({
      kind: KIND,
      version: VERSION,
      name: parts.name,
      timezone: parts.timezone,
      sticky: parts.sticky,
      root: rootValue
    }, null, 2);
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
    const missingImportFields = IMPORT_FIELDS
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
    const raw = data.root;
    if (!isRecord(raw) || raw.type !== 'graph' || !Array.isArray(raw.nodes) || !Array.isArray(raw.edges)) {
      /* kind/version 正确的 v3 但 root 形状非法：按 §5.2 报 graph_parse_error（zh/en），
         固定拒绝文案只留给旧 jev / version 2 / 错误 kind。 */
      return {
        ok: false,
        error: PARSE_ERROR_ZH,
        issues: [issue('graph_parse_error', PARSE_ERROR_ZH, PARSE_ERROR_EN)]
      };
    }
    const graph = clone(raw);
    graph.id = isNonEmptyString(graph.id) ? graph.id : 'root';
    graph.type = 'graph';
    /* 旧 v3 图可能仍带 otherwise_target：读取时折叠为统一兜底，写回时不再输出。 */
    if (graph.error_fallback == null && isNonEmptyString(graph.otherwise_target)) {
      graph.error_fallback = graph.otherwise_target;
    }
    delete graph.otherwise_target;
    /* 非对象条目不做静默丢弃：保留在 tree 中交给 validate 报 graph_parse_error。 */
    if (!graph.error_fallback && graph.error_fallback !== null) graph.error_fallback = null;

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
      timezone: data.timezone,
      sticky: rawSticky,
      root: graph
    };
    const issues = validate(tree);
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

  function createMinimalTree(model) {
    const targetModel = isNonEmptyString(model) ? model : 'qwen3-8b';
    return {
      name: '新决策组',
      timezone: 'Asia/Shanghai',
      sticky: { enabled: false, ttl_seconds: 1800 },
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
            strategy: 'least_inflight',
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

  const RTGraph = {
    VERSION: VERSION,
    KIND: KIND,
    LIMITS: LIMITS,
    NUM_OPS: NUM_OPS,
    OP: OP,
    LOGIC: LOGIC,
    NODE_TYPES: NODE_TYPES,
    HINTS: HINTS,
    THRESHOLD_LIMIT: THRESHOLD_LIMIT,
    REJECT_MESSAGE: REJECT_MESSAGE,
    clone: clone,
    stableStringify: stableStringify,
    graphOf: graphOf,
    findNode: findNode,
    portsOf: portsOf,
    ports: portsOf,
    portOf: portOf,
    portDesc: portDesc,
    nodeLabel: nodeLabel,
    logicName: logicName,
    edgeType: edgeType,
    conditionLabel: conditionLabel,
    conditionInvalidReason: conditionInvalidReason,
    defaultCondition: defaultCondition,
    evaluateCondition: evaluateCondition,
    canConnect: canConnect,
    isTimeZone: isTimeZone,
    nextSlot: nextSlot,
    addEdge: addEdge,
    removeEdge: removeEdge,
    removeNode: removeNode,
    removeOption: removeOption,
    renameOptionKey: renameOptionKey,
    validate: validate,
    behaviorCanonical: behaviorCanonical,
    changeKind: changeKind,
    layoutPositions: layoutPositions,
    serialize: serialize,
    parseImport: parseImport,
    createMinimalTree: createMinimalTree
  };

  root.RTGraph = RTGraph;
  if (typeof module !== 'undefined' && module.exports) module.exports = RTGraph;
})(typeof window !== 'undefined' ? window : globalThis);
