/* 计算日志重设计 v2 · 共享仿真数据
 * ------------------------------------------------------------------
 * 字段与事件名对齐生产实现：
 *   router/console/trace.js   —— build() 归组的 14 类事件
 *   router/console/logs.js    —— 行字段、outcome / selection_reason
 *   router/api/routes_v1.py   —— _decision_record
 * 三份原型共用这一份数据，保证「同一条真实记录」在三种信息架构下可直接比对。
 * 纯静态稿，不连后端；直接用浏览器打开同目录的 a / b / c HTML 即可。
 */

/* ── 七段主干：所有方案共用同一套阶段定义 ─────────────────────────
 * 把计算过程抽象成固定的 7 个阶段，是「一眼看出计算路径」的关键：
 * 命中 = 实线节点，跳过 = 虚线空节点，中止 = 红点后全空。
 * 阶段名沿用生产 trace.js 的语义，不另造词。                       */
const STAGES = [
  { key: 'IN', name: '输入准备', abbr: '输入' },
  { key: 'COND', name: '初始条件', abbr: '条件' },
  { key: 'GATE', name: '门与槽位', abbr: '门' },
  { key: 'JEV', name: 'JEV 轮次', abbr: 'JEV' },
  { key: 'FINAL', name: '候选与选择', abbr: '候选' },
  { key: 'SCHED', name: '模型调度', abbr: '调度' },
  { key: 'MODEL', name: '执行结果', abbr: '模型' },
];

const TRACE_LOGS = [
  {
    id: 'r-8841', ts: '2026-11-04T17:12:08', text: '继续上一题：把刚才的模块拆成三个服务，并给出迁移顺序和回滚方案',
    model: 'qwen3-32b', outcome: 'completed', selection_reason: 'target',
    routing_ms: 46, total_ms: 1284, upstream_ms: 1240, ttft_ms: 310, upstream_status: 200,
    upstream_model: 'qwen3-32b', prompt_tokens: 812, completion_tokens: 356, total_tokens: 1168,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: 'a91f0c72', jev_model: 'NeoHorse-JEV-4B', waves: 2, jev_top: 0.92,
    reasoning: '标准档：多步拆解与回滚方案需要长链一致性，短答案模型容易遗漏依赖顺序。',
    node_labels: {
      root: { title: '入口' }, 'cond-day': { title: '白天窗口' }, 'cond-long': { title: '长文本' },
      'q-ability': { title: '能力判断' }, 'gate-strong': { title: '高置信门' },
      't-strong': { title: '标准档目标' }, 't-fast': { title: '快速档目标' },
    },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 812, tokens_after: 812, truncated: false },
      { event: 'condition', node_id: 'cond-day', source: 'periods', matched: ['白天'], value: true },
      { event: 'condition', node_id: 'cond-long', source: 'text', matched: ['迁移', '回滚'], value: true },
      { event: 'condition', node_id: 'cond-vision', source: 'text', matched: [], value: false },
      { event: 'wave', wave: 0 },
      { event: 'gate', wave: 0, node_id: 'gate-strong', operator: 'AND', value: true,
        inputs: [{ port: 'day', raw: 'true', condition: { op: 'is', value: true }, status: 'true' },
                 { port: 'long', raw: 'true', condition: { op: 'is', value: true }, status: 'true' }] },
      { event: 'slot', wave: 0, node_id: 'gate-strong', port: 'day', source: 'cond-day', source_port: 'hit',
        raw: 'true', condition: { op: 'is', value: true }, status: 'true', edge_id: 'e-day' },
      { event: 'slot', wave: 0, node_id: 'gate-strong', port: 'long', source: 'cond-long', source_port: 'hit',
        raw: 'true', condition: { op: 'is', value: true }, status: 'true', edge_id: 'e-long' },
      { event: 'activate', wave: 0, node_id: 'q-ability', via_port: 'ready' },
      { event: 'wave', wave: 1, failed: false },
      { event: 'question', wave: 1, node_id: 'q-ability', selected: 'strong', p_top: 0.92, margin: 0.71,
        ports: { 'prob.strong': 0.92, 'prob.fast': 0.21, 'prob.vision': 0.04 }, duration_ms: 38,
        usage: { prompt: 812, completion: 24 } },
      { event: 'gate', wave: 1, node_id: 'gate-strong', operator: 'gte', value: true },
      { event: 'slot', wave: 1, node_id: 't-strong', port: 'high', source: 'q-ability', source_port: 'strong',
        raw: '0.92', condition: { op: 'gte', value: 0.6 }, status: 'true', edge_id: 'e-strong' },
      { event: 'activate', wave: 1, node_id: 't-strong', via_port: 'high' },
      { event: 'final', kind: 'target', target_id: 't-strong', candidates: [
        { target_id: 't-strong', priority: 1, via_node: 'q-ability', via_port: 'high', wave: 1 },
        { target_id: 't-fast', priority: 2, via_node: 'root', via_port: 'otherwise', wave: 0 },
      ] },
      { event: 'schedule', target_id: 't-strong', strategy: 'priority',
        configured_models: ['qwen3-32b', 'qwen3-8b'], registered_models: ['qwen3-32b', 'qwen3-8b'],
        available_models: ['qwen3-32b', 'qwen3-8b'], unavailable: {}, inflight: { 'qwen3-32b': 1 },
        sticky: { status: 'hit', preferred_model: 'qwen3-32b' } },
      { event: 'scheduled', model: 'qwen3-32b' },
    ],
  },
  {
    id: 'r-8840', ts: '2026-11-04T17:11:33', text: '再补充一句：回滚时数据库怎么保持一致？',
    model: 'qwen3-32b', outcome: 'completed', selection_reason: 'sticky', sticky_hit: true,
    routing_ms: 3, total_ms: 1024, upstream_ms: 1002, ttft_ms: 288, upstream_status: 200,
    upstream_model: 'qwen3-32b', prompt_tokens: 96, completion_tokens: 214, total_tokens: 310,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: 'a91f0c72', jev_model: 'NeoHorse-JEV-4B', waves: 0,
    node_labels: { root: { title: '入口' }, 't-strong': { title: '标准档目标' } },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 96, tokens_after: 96, truncated: false },
      { event: 'sticky_hit', model: 'qwen3-32b', path: 'root' },
      { event: 'scheduled', model: 'qwen3-32b' },
    ],
  },
  {
    id: 'r-8839', ts: '2026-11-04T17:10:57', text: '帮我把这段 Python 改成 Rust，保留原有错误类型和日志格式',
    model: 'qwen3-8b', outcome: 'completed', selection_reason: 'otherwise',
    routing_ms: 61, total_ms: 860, upstream_ms: 795, ttft_ms: 205, upstream_status: 200,
    upstream_model: 'qwen3-8b', prompt_tokens: 640, completion_tokens: 780, total_tokens: 1420,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: '77bd03e1', jev_model: 'NeoHorse-JEV-4B', waves: 2, jev_top: 0.31,
    reasoning: '候选概率分散（fast 0.41 / strong 0.31 / vision 0.28），margin 0.08 低于阈值，不采信 JEV 结论。',
    node_labels: {
      root: { title: '入口' }, 'cond-day': { title: '白天窗口' }, 'q-ability': { title: '能力判断' },
      'gate-strong': { title: '高置信门' }, 't-safe': { title: '安全兜底目标' },
    },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 11840, tokens_after: 640, truncated: true },
      { event: 'condition', node_id: 'cond-day', source: 'periods', matched: ['白天'], value: true },
      { event: 'condition', node_id: 'cond-code', source: 'text', matched: [], value: false },
      { event: 'wave', wave: 0 },
      { event: 'gate', wave: 0, node_id: 'q-ability', operator: 'AND', value: true },
      { event: 'slot', wave: 0, node_id: 'q-ability', port: 'day', source: 'cond-day', source_port: 'hit',
        raw: 'true', condition: { op: 'is', value: true }, status: 'true', edge_id: 'e-day' },
      { event: 'activate', wave: 0, node_id: 'q-ability', via_port: 'day' },
      { event: 'wave', wave: 1, failed: false },
      { event: 'question', wave: 1, node_id: 'q-ability', selected: 'fast', p_top: 0.31, margin: 0.08,
        ports: { 'prob.fast': 0.41, 'prob.strong': 0.31, 'prob.vision': 0.28 }, duration_ms: 55,
        usage: { prompt: 640, completion: 31 } },
      { event: 'gate', wave: 1, node_id: 'gate-strong', operator: 'gte', value: false },
      { event: 'slot', wave: 1, node_id: 't-safe', port: 'otherwise', source: 'root', source_port: 'otherwise',
        raw: '0.31', condition: { op: 'lt', value: 0.6 }, status: 'true', edge_id: 'e-fallback' },
      { event: 'final', kind: 'otherwise', target_id: 't-safe', candidates: [] },
      { event: 'schedule', target_id: 't-safe', strategy: 'priority',
        configured_models: ['qwen3-32b', 'qwen3-8b'], registered_models: ['qwen3-32b', 'qwen3-8b'],
        available_models: ['qwen3-8b'], unavailable: { 'qwen3-32b': 'breaker_open' },
        inflight: {}, sticky: { status: 'no_session' } },
      { event: 'scheduled', model: 'qwen3-8b' },
    ],
  },
  {
    id: 'r-8838', ts: '2026-11-04T17:09:44', text: '分析这份财报里的三个异常项，并给出结论',
    model: 'qwen3-32b', outcome: 'upstream_error', selection_reason: 'target',
    routing_ms: 44, total_ms: 830, upstream_ms: 780, ttft_ms: null, upstream_status: 502,
    upstream_model: 'qwen3-32b', prompt_tokens: 1204, completion_tokens: 0, total_tokens: 1204,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: '5e20c81d', jev_model: 'NeoHorse-JEV-4B', waves: 1, jev_top: 0.88,
    node_labels: { root: { title: '入口' }, 'q-ability': { title: '能力判断' }, 't-strong': { title: '标准档目标' } },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 1204, tokens_after: 1204, truncated: false },
      { event: 'condition', node_id: 'cond-day', source: 'periods', matched: ['白天'], value: true },
      { event: 'wave', wave: 0 },
      { event: 'activate', wave: 0, node_id: 'q-ability', via_port: 'day' },
      { event: 'wave', wave: 1, failed: false },
      { event: 'question', wave: 1, node_id: 'q-ability', selected: 'strong', p_top: 0.88, margin: 0.64,
        ports: { 'prob.strong': 0.88, 'prob.fast': 0.24, 'prob.vision': 0.05 }, duration_ms: 36 },
      { event: 'activate', wave: 1, node_id: 't-strong', via_port: 'high' },
      { event: 'final', kind: 'target', target_id: 't-strong', candidates: [
        { target_id: 't-strong', priority: 1, via_node: 'q-ability', via_port: 'high', wave: 1 },
      ] },
      { event: 'schedule', target_id: 't-strong', strategy: 'priority',
        configured_models: ['qwen3-32b', 'qwen3-8b'], registered_models: ['qwen3-32b', 'qwen3-8b'],
        available_models: ['qwen3-32b', 'qwen3-8b'], unavailable: {}, inflight: {},
        sticky: { status: 'not_checked' } },
      { event: 'scheduled', model: 'qwen3-32b' },
    ],
  },
  {
    id: 'r-8837', ts: '2026-11-04T17:08:12', text: '把这段 30 页的合同按风险等级重排，输出对照表',
    model: null, outcome: 'routing_error', selection_reason: null,
    routing_ms: 128, total_ms: null, upstream_ms: null, ttft_ms: null, upstream_status: null,
    upstream_model: null, prompt_tokens: null, completion_tokens: null, total_tokens: null,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: 'c0fa9187', jev_model: 'NeoHorse-JEV-4B', waves: 2, jev_top: null,
    node_labels: { root: { title: '入口' }, 'q-ability': { title: '能力判断' } },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 26310, tokens_after: 4096, truncated: true },
      { event: 'condition', node_id: 'cond-day', source: 'periods', matched: ['白天'], value: true },
      { event: 'condition', node_id: 'cond-long', source: 'text', matched: ['合同'], value: true },
      { event: 'wave', wave: 0 },
      { event: 'gate', wave: 0, node_id: 'q-ability', operator: 'AND', value: true },
      { event: 'activate', wave: 0, node_id: 'q-ability', via_port: 'ready' },
      { event: 'wave', wave: 1, failed: true, error: 'JEVDownstreamTimeout: 3000ms' },
      { event: 'routing_error', stage: 'jev_wave_1', error_class: 'TimeoutError',
        message: 'JEV 决策服务 3000ms 未返回，本轮结果全部作废' },
    ],
  },
  {
    id: 'r-8836', ts: '2026-11-04T17:07:05', text: '当前 /v1/models 里有哪些模型？',
    model: 'qwen3-8b', outcome: 'completed', selection_reason: 'target',
    routing_ms: 2, total_ms: 210, upstream_ms: 208, ttft_ms: 96, upstream_status: 200,
    upstream_model: 'qwen3-8b', prompt_tokens: 18, completion_tokens: 96, total_tokens: 114,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root.direct',
    session_hash: null, jev_model: 'NeoHorse-JEV-4B', waves: 0,
    node_labels: { root: { title: '入口' }, 'cond-direct': { title: '直连关键词' }, 't-direct': { title: '直连目标' } },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 18, tokens_after: 18, truncated: false },
      { event: 'condition', node_id: 'cond-direct', source: 'text', matched: ['/v1/models'], value: true },
      { event: 'wave', wave: 0 },
      { event: 'gate', wave: 0, node_id: 't-direct', operator: 'is', value: true },
      { event: 'activate', wave: 0, node_id: 't-direct', via_port: 'direct' },
      { event: 'final', kind: 'target', target_id: 't-direct', candidates: [
        { target_id: 't-direct', priority: 0, via_node: 'cond-direct', via_port: 'direct', wave: 0 },
      ] },
      { event: 'schedule', target_id: 't-direct', strategy: 'priority',
        configured_models: ['qwen3-8b'], registered_models: ['qwen3-8b'],
        available_models: ['qwen3-8b'], unavailable: {}, inflight: {}, sticky: { status: 'disabled' } },
      { event: 'scheduled', model: 'qwen3-8b' },
    ],
  },
  {
    id: 'r-8835', ts: '2026-11-04T17:06:31', text: '把这张表按季度汇总，输出 markdown，并标出同比变化超过 20% 的行',
    model: 'qwen3-32b', outcome: 'cancelled', selection_reason: 'target',
    routing_ms: 39, total_ms: 2600, upstream_ms: 2510, ttft_ms: 402, upstream_status: 200,
    upstream_model: 'qwen3-32b', prompt_tokens: 220, completion_tokens: 64, total_tokens: 284,
    tree_id: 'console-smoke', tree_revision: 12, snapshot_revision: 38, policy_path: 'root',
    session_hash: '31de77aa', jev_model: 'NeoHorse-JEV-4B', waves: 1, jev_top: 0.66,
    node_labels: { root: { title: '入口' }, 'q-ability': { title: '能力判断' }, 't-strong': { title: '标准档目标' } },
    trace: [
      { event: 'time', active_periods: ['白天'], timezone: 'Asia/Shanghai' },
      { event: 'window', tokens_before: 220, tokens_after: 220, truncated: false },
      { event: 'condition', node_id: 'cond-day', source: 'periods', matched: ['白天'], value: true },
      { event: 'wave', wave: 0 },
      { event: 'activate', wave: 0, node_id: 'q-ability', via_port: 'day' },
      { event: 'wave', wave: 1, failed: false },
      { event: 'question', wave: 1, node_id: 'q-ability', selected: 'strong', p_top: 0.66, margin: 0.29,
        ports: { 'prob.strong': 0.66, 'prob.fast': 0.37, 'prob.vision': 0.02 }, duration_ms: 33 },
      { event: 'activate', wave: 1, node_id: 't-strong', via_port: 'high' },
      { event: 'final', kind: 'target', target_id: 't-strong', candidates: [
        { target_id: 't-strong', priority: 1, via_node: 'q-ability', via_port: 'high', wave: 1 },
      ] },
      { event: 'schedule', target_id: 't-strong', strategy: 'priority',
        configured_models: ['qwen3-32b', 'qwen3-8b'], registered_models: ['qwen3-32b', 'qwen3-8b'],
        available_models: ['qwen3-32b', 'qwen3-8b'], unavailable: {}, inflight: {},
        sticky: { status: 'miss' } },
      { event: 'scheduled', model: 'qwen3-32b' },
    ],
  },
];

const OUTCOME_LABEL = {
  completed: '已完成', upstream_error: '上游错误', client_disconnected: '断连',
  cancelled: '已取消', incomplete: '未完成', routing_error: '路由失败',
};
const OUTCOME_TONE = {
  completed: '', upstream_error: 'bad', client_disconnected: 'warn',
  cancelled: 'warn', incomplete: 'warn', routing_error: 'bad',
};
const ROUTE_LABEL = {
  r_8841: 'JEV 目标命中', r_8840: '会话粘性直达', r_8839: '低置信兜底',
  r_8838: '上游 502', r_8837: '路由中止', r_8836: '关键词直连', r_8835: '客户端取消',
};

/* ── 工具 ─────────────────────────────────────────────────────── */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g,
    char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
function clock(ts) { return String(ts).slice(11, 19); }
function pct(value) {
  const number = Number(value);
  return value == null || !Number.isFinite(number) ? '—' : Math.round(number * 100) + '%';
}
function msText(value) {
  const number = Number(value);
  if (value == null || value === '' || !Number.isFinite(number)) return '—';
  return number >= 1000 ? (number / 1000).toFixed(2) + 's' : Math.round(number) + 'ms';
}
function dash(value) { return value == null || value === '' ? '—' : String(value); }
function labelOf(record, id) {
  const item = record.node_labels && record.node_labels[String(id)];
  return item && item.title ? String(item.title) : dash(id);
}
function routeLabel(record) {
  if (record.route_label) return record.route_label;
  const key = record.id.replace(/-/g, '_');
  if (ROUTE_LABEL[key]) return ROUTE_LABEL[key];
  if (record.outcome === 'routing_error') return '路由中止';
  if (record.outcome === 'upstream_error') return '上游 ' + dash(record.upstream_status);
  if (record.outcome === 'cancelled') return '客户端取消';
  if (record.sticky_hit) return '会话粘性直达';
  if (record.selection_reason === 'otherwise' || record.selection_reason === 'fallback') return '低置信兜底';
  if (derivePath(record)[3].skipped) return '关键词直连';
  return 'JEV 目标命中';
}

/* ── 从真实 trace 派生七段主干 ───────────────────────────────────
 * 纯视图模型：只读归组，不改事件对象。三份原型全部调用它，
 * 因此「路径」不是画上去的装饰，而是可由生产 trace 直接算出来的。 */
function derivePath(record) {
  const trace = Array.isArray(record.trace) ? record.trace : [];
  const pick = name => trace.filter(event => event && event.event === name);
  const time = pick('time')[0] || {};
  const win = pick('window')[0] || {};
  const conditions = pick('condition');
  const gates = pick('gate');
  const slots = pick('slot');
  const questions = pick('question');
  const waves = pick('wave');
  const failedWave = waves.find(event => event.failed);
  const final = pick('final')[0] || null;
  const fallback = pick('fallback')[0] || null;
  const schedule = pick('schedule')[0] || null;
  const sticky = pick('sticky_hit')[0] || null;
  const scheduled = pick('scheduled')[0] || null;
  const error = pick('routing_error')[0] || null;
  const halted = !!error;
  const periods = Array.isArray(time.active_periods) && time.active_periods.length
    ? time.active_periods.join('/') : '—';
  const skip = index => ({ skipped: true, halted: false, value: '—', cell: '·', tone: 'skip', evidence: [], ms: null });

  /* 1 · 输入准备 */
  const inTokens = win.tokens_after == null ? '—' : win.tokens_after + 'tok';
  const IN = {
    skipped: false, halted: false, tone: '', ms: null,
    value: periods + ' · ' + inTokens + (win.truncated ? ' · 已截断' : ''),
    cell: win.truncated ? '截断' : inTokens,
    evidence: [
      ['时段', periods], ['时区', dash(time.timezone)],
      ['Token 窗口', win.tokens_before == null ? '—' : win.tokens_before + ' → ' + dash(win.tokens_after)],
      ['请求文本', record.text],
    ],
  };

  /* 2 · 初始条件 */
  const hit = conditions.filter(event => event.value === true).length;
  const COND = conditions.length ? {
    skipped: false, halted: false, tone: '', ms: null,
    value: hit + '/' + conditions.length + ' 命中',
    cell: hit + '/' + conditions.length,
    evidence: conditions.map(event => [
      labelOf(record, event.node_id),
      (event.value === true ? '命中 ' : '未命中 ') + (Array.isArray(event.matched) && event.matched.length ? event.matched.join(', ') : '—'),
    ]),
  } : skip(2);

  /* 3 · 门与槽位（wave 0 的静态判定） */
  const gate0 = gates.find(event => Number(event.wave) === 0) || null;
  const GATE = (gate0 || slots.length || pick('activate').length) ? {
    skipped: false, halted: false,
    tone: gate0 && gate0.value === false ? 'warn' : '', ms: null,
    value: (gate0 ? String(gate0.operator || 'AND') + ' ' + String(gate0.value) : '无门 · 直接激活') + ' · ' + slots.length + ' 槽位',
    cell: gate0 ? String(gate0.value) : '直达',
    evidence: [
      ['门', gate0 ? labelOf(record, gate0.node_id) + ' · ' + String(gate0.operator) + ' → ' + String(gate0.value) : '未设门'],
      ['槽位统计', '命中 ' + slots.filter(e => e.status === 'true' || e.status === true).length +
        ' · 未命中 ' + slots.filter(e => e.status === 'false' || e.status === false).length +
        ' · 非法 ' + slots.filter(e => e.status === 'invalid').length],
      ...slots.slice(0, 3).map(event => [labelOf(record, event.source) + '.' + dash(event.source_port),
        dash(event.raw) + ' → ' + dash(event.status)]),
    ],
  } : skip(3);

  /* 4 · JEV 轮次：wave 0 属初始计算，只有 wave>0 / question 才算 JEV 轮次 */
  const jevWaves = waves.filter(event => Number(event.wave) > 0);
  let JEV = skip(4);
  if (jevWaves.length || questions.length) {
    const top = questions.reduce((best, event) => Number(event.p_top) > best ? Number(event.p_top) : best, 0);
    JEV = {
      skipped: false, halted: false, tone: failedWave ? 'bad' : (top && top < 0.6 ? 'warn' : ''),
      ms: questions.reduce((sum, event) => sum + (Number(event.duration_ms) || 0), 0) || null,
      value: failedWave
        ? '第 ' + failedWave.wave + ' 轮作废 · ' + dash(failedWave.error)
        : jevWaves.length + ' 轮 · p_top ' + pct(top || null),
      cell: failedWave ? 'FAIL' : pct(top || null),
      evidence: questions.map(event => [
        labelOf(record, event.node_id) + ' · 第 ' + dash(event.wave) + ' 轮',
        '选中 ' + dash(event.selected) + ' · p_top ' + pct(event.p_top) + ' · margin ' + pct(event.margin) +
        (event.duration_ms != null ? ' · ' + event.duration_ms + 'ms' : ''),
      ]).concat(failedWave ? [['失败原因', dash(failedWave.error)]] : []),
    };
  }

  /* 5 · 候选与最终选择 */
  let FINAL = skip(5);
  if (final || fallback) {
    const kind = final ? final.kind : 'fallback';
    const winner = final && Array.isArray(final.candidates)
      ? final.candidates.find(candidate => candidate.target_id === final.target_id) : null;
    const warning = kind === 'otherwise' || kind === 'error_fallback' || kind === 'fallback';
    FINAL = {
      skipped: false, halted: false, tone: warning ? 'warn' : '', ms: null,
      value: kind === 'target' ? '目标命中 · ' + labelOf(record, final.target_id)
        : kind === 'otherwise' ? '无候选兜底'
        : kind === 'error_fallback' ? '异常兜底' : '兜底',
      cell: kind === 'target' ? (winner ? 'p' + winner.priority : '命中') : '兜底',
      evidence: [
        ['最终目标', final ? labelOf(record, final.target_id) + ' (' + dash(final.target_id) + ')' : dash(fallback && fallback.target)],
        ['选择类型', kind],
        ...((final && final.candidates) || []).map(candidate => [
          '候选 p' + dash(candidate.priority),
          labelOf(record, candidate.target_id) + ' · via ' + labelOf(record, candidate.via_node) + '.' + dash(candidate.via_port) +
          ' · 第 ' + dash(candidate.wave) + ' 轮' + (candidate.target_id === (final && final.target_id) ? ' ✓ 选中' : ''),
        ]),
      ],
    };
  }

  /* 6 · 模型调度 */
  let SCHED = skip(6);
  if (schedule || sticky || scheduled) {
    const available = schedule && Array.isArray(schedule.available_models) ? schedule.available_models : [];
    const unavailable = schedule && schedule.unavailable ? Object.keys(schedule.unavailable) : [];
    SCHED = {
      skipped: false, halted: false, tone: unavailable.length ? 'warn' : '', ms: null,
      value: sticky ? '会话粘性 · ' + dash(sticky.model)
        : (schedule ? dash(schedule.strategy) + ' · ' + available.length + ' 可用' : '直接调度'),
      cell: sticky ? '粘性' : (schedule ? dash(schedule.strategy) : '调度'),
      evidence: sticky ? [['会话粘性', dash(sticky.model) + ' · ' + dash(sticky.path)]]
        : [
          ['调度策略', dash(schedule && schedule.strategy)],
          ['配置 / 已注册 / 可用',
            (schedule && Array.isArray(schedule.configured_models) ? schedule.configured_models.length : '—') + ' / ' +
            (schedule && Array.isArray(schedule.registered_models) ? schedule.registered_models.length : '—') + ' / ' + available.length],
          ['可用模型', available.length ? available.join(', ') : '—'],
          ['不可用', unavailable.length ? unavailable.map(name => name + ' · ' + schedule.unavailable[name]).join('；') : '—'],
          ['粘性检查', schedule && schedule.sticky ? dash(schedule.sticky.status) : '—'],
        ],
    };
  }

  /* 7 · 执行结果 */
  const MODEL = {
    skipped: false, halted: false, tone: OUTCOME_TONE[record.outcome] || '', ms: null,
    value: record.model ? record.model + ' · ' + msText(record.total_ms) : OUTCOME_LABEL[record.outcome],
    cell: record.model || OUTCOME_LABEL[record.outcome],
    evidence: [
      ['上游模型', dash(record.upstream_model)],
      ['状态 / 耗时', dash(record.upstream_status) + ' · ' + msText(record.upstream_ms)],
      ['首 token', msText(record.ttft_ms)],
      ['Token', record.total_tokens == null ? '—' : record.prompt_tokens + ' → ' + record.completion_tokens + ' (合计 ' + record.total_tokens + ')'],
      ['结果', OUTCOME_LABEL[record.outcome] || '—'],
    ],
  };

  let stages = [IN, COND, GATE, JEV, FINAL, SCHED, MODEL];
  /* 路由失败：失败点之后的阶段全部标记「中止」，不留看似跑过的空格。 */
  if (halted) {
    /* 失败点所在阶段仍要完整呈现（含失败原因），只把它之后的阶段标为中止。 */
    const stopAt = failedWave ? 3 : error.stage && /condition|window|input/.test(error.stage) ? 1 : 2;
    stages = stages.map((stage, index) => index > stopAt
      ? { skipped: true, halted: true, value: '中止', cell: '×', tone: 'halt', evidence: [], ms: null }
      : stage);
  }
  return stages.map((stage, index) => Object.assign({ key: STAGES[index].key, name: STAGES[index].name }, stage));
}

/* 供方案 C 的终端日志使用：把原始 trace 压成带缩进的事件行。 */
function eventLines(record) {
  const trace = Array.isArray(record.trace) ? record.trace : [];
  return trace.map((event, index) => {
    const pairs = [];
    Object.keys(event).forEach(key => {
      if (key === 'event' || key === 'inputs' || key === 'ports' || key === 'candidates' ||
          key === 'configured_models' || key === 'registered_models' || key === 'available_models') return;
      const value = event[key];
      if (value == null || typeof value === 'object') return;
      pairs.push(key + '=' + value);
    });
    if (event.ports) {
      Object.keys(event.ports).forEach(key => { pairs.push(key + '=' + event.ports[key]); });
    }
    return {
      index: index + 1,
      name: event.event,
      detail: pairs.join('  '),
      tone: event.failed || event.event === 'routing_error' ? 'bad'
        : event.event === 'final' || event.event === 'scheduled' ? 'hit'
        : event.event === 'fallback' || (event.kind && event.kind !== 'target') ? 'warn' : '',
    };
  });
}

/* 筛选也基于实际事件 / 阶段，不依赖固定的第 4 列。
   sticky / fallback / error 仍读业务字段，属迁移兼容层，见 README「剩余耦合点」。 */
const FILTERS = {
  all: () => true,
  jev: record => deriveStages(record).some(stage => stage.id === 'jev'),
  sticky: record => !!record.sticky_hit || record.selection_reason === 'sticky',
  fallback: record => ['otherwise', 'error_fallback', 'fallback'].indexOf(record.selection_reason) >= 0,
  error: record => ['routing_error', 'upstream_error'].indexOf(record.outcome) >= 0,
};


/* ── 回填更早 13 条，凑满生产口径的「最近 20 条」 ────────────────
 * 由 7 类真实路径模板派生：trace / outcome / selection_reason 完全一致，
 * 只改时间戳、请求文本与耗时 token，保证路径形状与事件流自洽。
 * 原型因此能真实反映 20 条时的页面密度，而不是 7 条留下的假留白。 */
(function backfillOlder() {
  const seeds = TRACE_LOGS.slice();
  const texts = [
    '把上面的接口改成批量版本，说明幂等键怎么设计',
    '用 200 字总结这轮压测的三个瓶颈',
    '这段 SQL 为什么走不上索引？给出执行计划解读',
    '翻译这段英文技术说明，保留术语表',
    '列出这份配置里所有超时项，并标注默认值',
    '帮我写一个最小复现脚本，能稳定触发这个竞态',
    '对比 gRPC 和 HTTP/2 在这个场景下的取舍',
    '把这个函数拆成两个，补上边界条件测试',
    '解释一下日志里这条 warn 的含义',
    '把刚才的结论整理成三段，语气正式一些',
    '检查这段正则有没有灾难性回溯风险',
    '给出这三个模型的部署成本对比表',
    '继续：再补一版带重试的伪代码',
  ];
  const shapes = [0, 5, 2, 0, 1, 3, 6, 0, 4, 2, 5, 0, 1];
  function shift(ts, seconds) {
    const parts = ts.split('T')[1].split(':').map(Number);
    let total = parts[0] * 3600 + parts[1] * 60 + parts[2] - seconds;
    const h = String(Math.floor(total / 3600)).padStart(2, '0');
    const m = String(Math.floor(total % 3600 / 60)).padStart(2, '0');
    const sec = String(total % 60).padStart(2, '0');
    return ts.split('T')[0] + 'T' + h + ':' + m + ':' + sec;
  }
  texts.forEach((text, i) => {
    const seed = seeds[shapes[i]];
    const drift = (i % 5) * 71 + 23;
    TRACE_LOGS.push(Object.assign({}, seed, {
      id: 'r-' + (8834 - i),
      ts: shift('2026-11-04T17:06:31', (i + 1) * 73 + (i % 3) * 19),
      text: text,
      total_ms: seed.total_ms == null ? null : seed.total_ms + drift,
      routing_ms: Number(seed.routing_ms || 0) + (i % 4) * 3,
      upstream_ms: seed.upstream_ms == null ? null : seed.upstream_ms + drift,
      ttft_ms: seed.ttft_ms == null ? null : seed.ttft_ms + (i % 4) * 9,
      prompt_tokens: seed.prompt_tokens == null ? null : seed.prompt_tokens + (i % 6) * 17,
      completion_tokens: seed.completion_tokens == null ? null : seed.completion_tokens + (i % 5) * 11,
      total_tokens: seed.total_tokens == null ? null : seed.total_tokens + (i % 6) * 17 + (i % 5) * 11,
    }));
  });
})();


/* ══════════════════════════════════════════════════════════════
   事件驱动阶段模型（方案 B 定稿方向）
   ──────────────────────────────────────────────────────────────
   问题：固定 7 段主干把「当前有哪些功能」写进了展示层。决策组没配时间段，
        日志仍然画一个「输入准备」格子并硬塞「白天 / Asia/Shanghai」；
        每加一个信号就要改一次日志。
   原则：日志只认识「事件 → 阶段」这个抽象，不认识任何具体功能。
        1. 阶段集合 = trace 中实际出现过的 stage，没配置的阶段不产生节点、
           不预留空位、不显示任何默认值。
        2. 新事件自带 phase / label / cell / summary / facts / tone，
           渲染器纯投影，后端新增功能时前端零改动。
        3. 历史（未自描述）事件走一次性兼容映射，复用 derivePath 的旧摘要；
           这是迁移适配层，不是每加一个功能就改的展示逻辑。
   ══════════════════════════════════════════════════════════════ */
const EVT_LEGACY_STAGE = {
  time:'input', window:'input', condition:'condition',
  gate:'gate', slot:'gate', activate:'gate',
  wave:'jev', question:'jev', final:'final', fallback:'final',
  schedule:'schedule', sticky_hit:'schedule', scheduled:'schedule',
  routing_error:'error',
};
const EVT_LEGACY_LABEL = {
  input:'输入准备', condition:'初始条件', gate:'门与槽位', jev:'JEV 轮次',
  final:'候选与选择', schedule:'模型调度', error:'路由中止',
};
/* 旧阶段 → derivePath 的固定索引，仅用于给历史数据补摘要。 */
const EVT_PATH_INDEX = { input:0, condition:1, gate:2, jev:3, final:4, schedule:5 };
const EVT_CONTRACT_KEYS = new Set(['event','phase','stage','label','cell','summary','facts','tone']);
const EVT_TONE_RANK = { '':0, hit:1, warn:2, bad:3 };

function evtStageOf(event) {
  if (event.phase) return String(event.phase);
  /* 生产 routing_error.stage 表示「失败于哪一步」，不是展示阶段，故单独归一化。 */
  if (event.event === 'routing_error') return 'error';
  const inJevWave = Number(event.wave) > 0;
  if (event.event === 'wave') return inJevWave ? 'jev' : 'gate';
  if (['gate','slot','activate','question'].indexOf(event.event) >= 0 && inJevWave) return 'jev';
  return EVT_LEGACY_STAGE[event.event] || event.event;
}
function evtScalar(value) {
  if (value == null) return '—';
  if (Array.isArray(value)) return value.length ? value.map(evtScalar).join(', ') : '—';
  if (typeof value === 'object') return null;
  return String(value);
}
function evtTone(event) {
  if (event.tone) return event.tone;
  if (event.failed || event.event === 'routing_error' || event.error) return 'bad';
  if (event.event === 'fallback' || (event.kind && event.kind !== 'target')) return 'warn';
  if (event.event === 'final' || event.event === 'scheduled') return 'hit';
  return '';
}
/* 阶段顺序 = trace 中首次出现顺序（生产 trace 本就按执行顺序追加）。 */
function deriveStages(record) {
  const legacyPath = derivePath(record);   // 只为历史数据补摘要，不决定阶段集合
  const groups = new Map();
  (Array.isArray(record.trace) ? record.trace : []).forEach(event => {
    if (!event || typeof event !== 'object') return;
    const id = evtStageOf(event);
    if (!groups.has(id)) groups.set(id, { id, label: event.label || EVT_LEGACY_LABEL[id] || id,
      tone: '', events: [] });
    const group = groups.get(id);
    group.events.push(event);
    const tone = evtTone(event);
    if (EVT_TONE_RANK[tone] > EVT_TONE_RANK[group.tone]) group.tone = tone;
  });
  return [...groups.values()].map((group, index) => {
    const described = group.events.filter(event => event.summary).pop();
    const last = group.events[group.events.length - 1];
    const legacy = legacyPath[EVT_PATH_INDEX[group.id]];
    const summary = described ? described.summary
      : legacy && !legacy.skipped ? legacy.value
      : last.message || last.error || last.summary
        || group.events.length + ' 个事件 · ' + group.events.map(event => event.event).join('/');
    const facts = described && Array.isArray(described.facts) ? described.facts.slice(0, 3)
      : legacy && !legacy.skipped ? legacy.evidence.slice(0, 3)
      : Object.keys(last).filter(key => !EVT_CONTRACT_KEYS.has(key))
          .map(key => [key, evtScalar(last[key])])
          .filter(([, value]) => value !== null && value !== '—').slice(0, 3);
    const cell = (described && described.cell) || last.cell
      || (legacy && !legacy.skipped ? legacy.cell : null)
      || (group.tone === 'bad' ? 'FAIL' : evtScalar(Object.values(last)[1]) || group.label);
    return { id: group.id, label: group.label, cell: String(cell).slice(0, 14),
      summary, facts, tone: group.tone, count: group.events.length, index: index + 1 };
  });
}

/* 反例样本：默认决策组没有配置时间段 / 条件 / JEV，
   trace 里就没有 time / condition / wave 事件，日志自然不会出现「白天」「时区」。
   三个事件全部自描述，渲染器不认识「默认决策组」这个概念也能正确显示。 */
TRACE_LOGS.unshift({
  id:'r-8842', ts:'2026-11-04T17:13:20', text:'把这段日志按时间排序，输出前 10 条',
  model:'qwen3-8b', outcome:'completed', selection_reason:'target',
  routing_ms:2, total_ms:210, upstream_ms:208, ttft_ms:96, upstream_status:200,
  upstream_model:'qwen3-8b', prompt_tokens:18, completion_tokens:96, total_tokens:114,
  tree_id:'default', tree_revision:1, snapshot_revision:1, policy_path:'root',
  session_hash:null, jev_model:null, waves:0,
  node_labels:{}, route_label:'默认决策组 · 直连',
  trace:[
    { event:'window', phase:'window', label:'Token 窗口', cell:'18tok', tone:'',
      summary:'18 → 18 tok', facts:[['输入','18'],['输出','18'],['截断','否']],
      tokens_before:18, tokens_after:18, truncated:false },
    { event:'schedule', phase:'schedule', label:'模型调度', cell:'priority', tone:'hit',
      summary:'priority · 1 可用', facts:[['策略','priority'],['可用','qwen3-8b']],
      strategy:'priority', available_models:['qwen3-8b'] },
    { event:'scheduled', phase:'model', label:'执行结果', cell:'qwen3-8b', tone:'hit',
      summary:'qwen3-8b · 210ms', facts:[['模型','qwen3-8b'],['上游状态','200'],['总耗时','210ms']],
      model:'qwen3-8b' },
  ],
});

/* 新样本置顶后仍维持生产口径的 20 条，移除最早的一条回填记录。 */
TRACE_LOGS.splice(TRACE_LOGS.length - 1, 1);
