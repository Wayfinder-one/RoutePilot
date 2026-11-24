/* RTTryRun · 真实草稿试算 / 画布回放 / 按波次整理（规格 §6.8 / §6.9）
   ------------------------------------------------------------------
   回放唯一数据源：POST /admin/try 返回的 trace（§3.9）。rebuild() 只消费事件，
   绝不调用 RTGraph.evaluateCondition、也不读取 graph_scenarios 的 answers。
   卡片回写走 cards.js 已有的 data-live / data-slot / data-edge 契约；边回写走
   canvas.js 的 edgeEl() + class。试算只展示画布回放与结论，不再有执行轨迹抽屉；
   日志快照复用同一套 rebuild()/apply()，通过 ``readOnly`` 关闭重跑/清除/整理。

   ES module：app.js 装配时调用 configureTryRun({ store, getKit, getBooted,
   change, toast, render, collapsePanels, updateToolbar, readOnly })；运行锁写回
   Store，回放结果由 current() 暴露给装配层重绘。
   ------------------------------------------------------------------ */
import * as RTGraph from './graph.js';
import { api, apiErrText } from '../api.js';
import { t, tf } from '../i18n.js';
import { escape as esc, nodeName as nodeNameOf } from './cards.js';
import { close as closeCondition, conditionLabel, conditionText, isOpen as isCondPopOpen } from './condpop.js';

let deps = null;
export function configureTryRun(next) { deps = next || null; }
const storeNow = () => (deps && deps.store ? deps.store : null);
const graphNow = () => (storeNow() ? storeNow().graph : null);
const treeNow = () => (storeNow() ? storeNow().tree : null);
const kitNow = () => (deps && typeof deps.getKit === 'function' ? deps.getKit() : null);
const isBootedNow = () => !!(deps && typeof deps.getBooted === 'function' && deps.getBooted());
const renderNow = () => { if (deps && typeof deps.render === 'function') deps.render(); };
const toastNow = (message, tone) => { if (deps && typeof deps.toast === 'function') deps.toast(message, tone); };
const updateToolbarNow = () => { if (deps && typeof deps.updateToolbar === 'function') deps.updateToolbar(); };
/* 日志快照：只回放，不提供重跑/清除/整理等写入口。 */
const isReadOnly = () => !!(deps && deps.readOnly);
const byId = id => document.getElementById(id);
/* 画布上节点用中文显示名；抽屉里同样显示名称，原始 id 留在 tooltip。 */
const nodeName = id => {
  const graph = graphNow();
  const node = graph ? RTGraph.findNode(graph, id) : null;
  return node ? nodeNameOf(node, graph) : (id || '—');
};
/* 试算范围：只有 select 显式选 route_set 才走已发布路由集，其余（含控件缺失）
   一律按当前草稿处理，保持既有行为。 */
function currentScope() {
  const select = byId('cvTryScope');
  return select && select.value === 'route_set' ? 'route_set' : 'draft';
}
/* 路由集结果只描述已发布图，节点标题不能被当前草稿画布「翻译」：同一 id 在
   两张图里含义不同，直接显示原始 id 才是结果真正指向的节点。 */
function resultNodeName(replay, id) {
  return replay && replay.scope === 'route_set' ? String(id) : nodeName(id);
}
const clone = value => (value == null ? value : JSON.parse(JSON.stringify(value)));

/* 五个样例 = 文本 + 固定 now。文本与 tests/fixtures/graph_scenarios.json 完全一致，
   now 取场景值，保证任意时刻运行都可复现；答案仍只来自实时 /admin/try。 */
const SAMPLES = [
  { labelKey: 'cv.trySample1', text: '10kV 线路跳闸，请安排抢修工单并做故障定位。', now: '2026-11-02T12:00:00+08:00' },
  { labelKey: 'cv.trySample2', text: '把这段话翻译成英文：设备巡检完成。', now: '2026-11-02T23:30:00+08:00' },
  { labelKey: 'cv.trySample3', text: '帮我生成一份抢修工单模板。', now: '2026-11-02T09:00:00+08:00' },
  { labelKey: 'cv.trySample4', text: '停电了，但我不确定该找谁处理。', now: '2026-11-02T12:00:00+08:00' },
  { labelKey: 'cv.trySample5', text: '帮我写一份月度运维总结。', now: '2026-11-02T23:30:00+08:00' }
];

const NODE_STATE_CLASSES = ['is-replay', 'is-dim', 'is-final', 'is-cand', 'is-fail',
  'is-fallback', 'is-unknown'];
const SLOT_STATE_CLASSES = ['is-true', 'is-false', 'is-unknown', 'is-invalid', 'is-error-path'];
const EDGE_STATE_CLASSES = ['is-true', 'is-false', 'is-unknown', 'is-invalid', 'is-error-path'];

/* 只有这些事件把 trace 变成「图上的执行状态」。time/window/request_outcome 与
   未知事件只描述广延元数据；sticky_hit/scheduled 只在 final 已存在时补字段，
   都不能单独把静态图涂成回放。 */
const GRAPH_EVENTS = new Set([
  'condition', 'slot', 'gate', 'activate', 'wave', 'question',
  'fallback', 'final', 'routing_error'
]);

const KIND_KEYS = {
  target: 'cv.tryKind.target', otherwise: 'cv.tryKind.fallback',
  error_fallback: 'cv.tryKind.fallback', sticky: 'cv.tryKind.sticky'
};

/* runtime_mismatch 的资源类型 → i18n 文案键（字面量键便于 i18n 死键检查）。 */
const MISMATCH_KIND_KEYS = {
  provider: 'cv.tryMismatchKind.provider',
  model: 'cv.tryMismatchKind.model',
  jev: 'cv.tryMismatchKind.jev'
};

let state = null;        /* 最近一次回放（与 App.replay 同一对象） */
let lastRun = null;      /* {text, now} 供「重跑」复用固定 now */
let runSeq = 0;
let bound = false;
let waveLayout = null;   /* 最近一次按波次整理的列信息（未触发列头用） */
let waveHeader = null;

/* ── 格式 ─────────────────────────────────────────────────────── */
function num(value) {
  if (typeof value !== 'number' || !isFinite(value)) return String(value);
  return String(Math.round(value * 1000) / 1000);
}

function numOrDash(value) {
  return value === undefined || value === null ? '—' : num(value);
}

function ms(value) {
  const number = Number(value);
  return (isFinite(number) ? Math.round(number) : 0) + ' ms';
}

function rawText(raw) {
  if (raw === null || raw === undefined) return t('cv.tryUnknown');
  if (typeof raw === 'boolean') return String(raw);
  if (typeof raw === 'number') return num(raw);
  return String(raw);
}

function statusText(status) {
  if (status === 'unknown') return t('cv.tryUnknown');
  if (status === 'invalid') return t('cv.tryInvalid');
  return status;
}

function slotLine(slot) {
  const graph = graphNow();
  const edge = graph && (graph.edges || []).find(item => item && item.id === slot.edge_id);
  const label = edge
    ? conditionText(edge, graph)
    : conditionLabel(slot.condition, { start: false });
  const pieces = [rawText(slot.raw)];
  if (slot.condition !== undefined && slot.condition !== null) pieces.push(label);
  else if (typeof slot.raw === 'boolean') pieces.push(label);
  pieces.push('→', statusText(slot.status));
  /* 开始节点的默认边没有条件文案（label 为空）：不要留下空片段和双空格。 */
  return pieces.filter(piece => piece !== '').join(' ');
}

/* ── Step 3 · trace-only rebuild ──────────────────────────────── */
function rebuild(trace, graph) {
  const events = Array.isArray(trace) ? trace : [];
  const source = graph || graphNow() || { nodes: [], edges: [] };
  const nodeStates = Object.create(null);
  const edgeStates = Object.create(null);
  const waves = [];
  const meta = { timezone: '', active_periods: [], tokens_before: null, tokens_after: null, truncated: false };
  let candidates = [];
  let final = null;
  let fallback = null;
  let error = null;

  function ensureNode(id, type) {
    if (!nodeStates[id]) {
      nodeStates[id] = {
        id: id, type: type || '', status: type === 'logic' ? 'unknown' : 'not-triggered',
        wave: null, ports: null, slots: Object.create(null), slotHistory: Object.create(null),
        activated: false, candidate: false, final: false, error: null
      };
    }
    return nodeStates[id];
  }

  (source.nodes || []).forEach(node => {
    if (!node || !node.id) return;
    if (node.type === 'question') {
      nodeStates[node.id] = {
        id: node.id, type: 'question', status: 'not-triggered', wave: null, ports: null,
        slots: Object.create(null), slotHistory: Object.create(null), activated: false,
        selected: null, p_top: null, margin: null, duration_ms: null, usage: null, error: null
      };
    } else if (node.type === 'logic') {
      nodeStates[node.id] = {
        id: node.id, type: 'logic', status: 'unknown', wave: null, value: null, ports: null,
        slots: Object.create(null), slotHistory: Object.create(null), inputs: [], operator: node.operator, error: null
      };
    } else if (node.type === 'target') {
      nodeStates[node.id] = {
        id: node.id, type: 'target', status: 'not-triggered', wave: null,
        slots: Object.create(null), slotHistory: Object.create(null), candidate: false,
        final: false, priority: null, kind: null, via_port: null, via_node: null, error: null
      };
    } else {
      nodeStates[node.id] = {
        id: node.id, type: node.type, status: 'done', wave: 0, ports: { out: true },
        slots: Object.create(null), slotHistory: Object.create(null), error: null
      };
    }
  });
  (source.edges || []).forEach(edge => {
    if (!edge || !edge.id) return;
    edgeStates[edge.id] = {
      edge_id: edge.id, node_id: edge.target, port: edge.target_port,
      source: edge.source, source_port: edge.source_port,
      status: 'unknown', raw: null, condition: null, wave: null, history: []
    };
  });
  Object.keys(nodeStates).forEach(id => {
    const nodeState = nodeStates[id];
    if (nodeState.type !== 'start') return;
    nodeState.status = 'done';
    nodeState.wave = 0;
    nodeState.ports = { out: true };
  });

  function lastWave() {
    return waves.length ? waves[waves.length - 1].wave : 0;
  }

  function applySlot(event) {
    const nodeState = ensureNode(event.node_id, '');
    const record = {
      slot: event.port, edge_id: event.edge_id, source: event.source, source_port: event.source_port,
      raw: event.raw, condition: event.condition, status: event.status, wave: event.wave
    };
    nodeState.slots[event.port] = record;
    if (!nodeState.slotHistory[event.port]) nodeState.slotHistory[event.port] = [];
    nodeState.slotHistory[event.port].push(record);
    const edgeState = edgeStates[event.edge_id] ||
      (edgeStates[event.edge_id] = { edge_id: event.edge_id, history: [] });
    Object.assign(edgeState, record, { node_id: event.node_id, port: event.port });
    edgeState.history = (edgeState.history || []).concat(record);
  }

  events.forEach(event => {
    if (!event || typeof event !== 'object') return;
    switch (event.event) {
      case 'time':
        meta.timezone = event.timezone || '';
        meta.active_periods = (event.active_periods || []).slice();
        break;
      case 'window':
        meta.tokens_before = event.tokens_before;
        meta.tokens_after = event.tokens_after;
        meta.truncated = event.truncated === true;
        break;
      case 'condition': {
        const nodeState = ensureNode(event.node_id, 'condition');
        nodeState.status = 'done';
        nodeState.wave = 0;
        nodeState.ports = Object.assign({}, nodeState.ports || {}, { out: event.value });
        nodeState.value = event.value;
        nodeState.matched = (event.matched || []).slice();
        nodeState.source = event.source;
        break;
      }
      case 'slot':
        applySlot(event);
        break;
      case 'gate': {
        const nodeState = ensureNode(event.node_id, 'logic');
        nodeState.status = 'done';
        nodeState.wave = event.wave;
        nodeState.value = event.value;
        nodeState.ports = Object.assign({}, nodeState.ports || {}, { out: event.value });
        nodeState.inputs = clone(event.inputs || []);
        nodeState.operator = event.operator;
        break;
      }
      case 'activate': {
        const nodeState = ensureNode(event.node_id, '');
        nodeState.activated = true;
        nodeState.wave = event.wave;
        nodeState.via_port = event.via_port;
        if (nodeState.type === 'target') nodeState.status = 'activated';
        break;
      }
      case 'wave': {
        const wave = {
          wave: event.wave,
          questions: (event.questions || []).slice(),
          duration_ms: event.duration_ms,
          failed: event.failed === true,
          error: event.error == null ? null : String(event.error)
        };
        waves.push(wave);
        if (wave.failed) {
          wave.questions.forEach(id => {
            const nodeState = ensureNode(id, 'question');
            nodeState.status = 'failed';
            nodeState.wave = wave.wave;
            nodeState.error = wave.error;
          });
          candidates = [];
          Object.keys(nodeStates).forEach(id => { nodeStates[id].candidate = false; });
        }
        break;
      }
      case 'question': {
        const nodeState = ensureNode(event.node_id, 'question');
        nodeState.status = 'done';
        nodeState.wave = event.wave;
        nodeState.ports = Object.assign({}, event.ports || {});
        nodeState.duration_ms = event.duration_ms;
        nodeState.usage = clone(event.usage);
        if (event.selected !== undefined) nodeState.selected = event.selected;
        if (event.p_top !== undefined) nodeState.p_top = event.p_top;
        if (event.margin !== undefined) nodeState.margin = event.margin;
        break;
      }
      case 'fallback': {
        fallback = {
          target_id: event.target_id,
          reason: event.reason == null ? null : String(event.reason),
          edge_ids: []
        };
        candidates = [];
        Object.keys(nodeStates).forEach(id => { nodeStates[id].candidate = false; });
        break;
      }
      case 'final': {
        final = {
          target_id: event.target_id,
          kind: event.kind || 'target',
          priority: event.priority == null ? null : event.priority,
          candidates: (event.candidates || []).map(candidate => ({
            target_id: candidate.target_id,
            priority: candidate.priority,
            via_node: candidate.via_node,
            via_port: candidate.via_port,
            wave: candidate.wave
          })),
          model: null,
          selection_reason: event.kind || 'target',
          sticky_hit: false,
          reason: null,
          wave: lastWave()
        };
        candidates = final.candidates;
        Object.keys(nodeStates).forEach(id => {
          if (nodeStates[id].type === 'target') nodeStates[id].candidate = false;
        });
        final.candidates.forEach(candidate => {
          const nodeState = ensureNode(candidate.target_id, 'target');
          nodeState.candidate = true;
          nodeState.priority = candidate.priority;
          nodeState.via_node = candidate.via_node;
          nodeState.via_port = candidate.via_port;
          nodeState.wave = candidate.wave;
        });
        const targetState = ensureNode(event.target_id, 'target');
        targetState.final = true;
        targetState.kind = final.kind;
        targetState.priority = final.priority;
        if (targetState.status === 'not-triggered') targetState.status = 'activated';
        if (targetState.wave === null || targetState.wave === undefined) targetState.wave = final.wave;
        final.wave = targetState.wave;
        if (final.kind === 'error_fallback') {
          if (!fallback) fallback = { target_id: event.target_id, reason: null, edge_ids: [] };
          fallback.target_id = event.target_id;
        }
        break;
      }
      case 'routing_error':
        /* error 与 final 独立：命中目标后的调度/上游失败不得清除 final，
           也绝不把失败合成一个「最终目标」。 */
        error = {
          stage: event.stage || '',
          error_class: event.error_class || '',
          message: event.message || ''
        };
        break;
      case 'sticky_hit': {
        if (final) {
          final.sticky_hit = true;
          final.model = event.model;
        }
        break;
      }
      case 'scheduled': {
        if (final) {
          final.model = event.model;
          if (event.reason) final.selection_reason = event.reason;
        }
        break;
      }
      default:
        break;
    }
  });

  if (final && final.kind === 'error_fallback') {
    /* §6.8「error_fallback 路径整链琥珀」：只点亮回退目标的入边（+ 目标节点），
       不能把此前所有 true 槽位边都算成异常路径。 */
    const edgeIds = Object.create(null);
    Object.keys(edgeStates).forEach(id => {
      if (edgeStates[id].node_id === final.target_id) edgeIds[id] = true;
    });
    if (!fallback) fallback = { target_id: final.target_id, reason: null, edge_ids: [] };
    fallback.edge_ids = Object.keys(edgeIds).sort();
  }
  if (final) final.waves = waves;

  return {
    nodeStates: nodeStates,
    edgeStates: edgeStates,
    waves: waves,
    candidates: candidates,
    final: final,
    fallback: fallback,
    error: error,
    meta: meta,
    hasGraphEvents: events.some(event =>
      !!event && typeof event === 'object' && GRAPH_EVENTS.has(event.event))
  };
}

/* ── 卡片 / 边回写 ────────────────────────────────────────────── */
function findNodeEl(nodeId) {
  const layer = byId('cvNodes');
  if (!layer) return null;
  const all = layer.querySelectorAll('[data-node-id]');
  for (let index = 0; index < all.length; index += 1) {
    if (all[index].dataset.nodeId === nodeId) return all[index];
  }
  return null;
}

function findSlotEl(nodeId, portId) {
  const node = findNodeEl(nodeId);
  if (!node) return null;
  const rows = node.querySelectorAll('.rtc-slot[data-slot]');
  for (let index = 0; index < rows.length; index += 1) {
    if (rows[index].dataset.slot === portId) return rows[index];
  }
  return null;
}

function liveEl(scope, name) {
  if (!scope) return null;
  const all = scope.querySelectorAll('[data-live]');
  for (let index = 0; index < all.length; index += 1) {
    if (all[index].dataset.live === name) return all[index];
  }
  return null;
}

function setLive(scope, name, text) {
  const el = liveEl(scope, name);
  if (el) el.textContent = text;
}

function markNode(el, mode, classes) {
  el.dataset.replay = mode;
  (classes || []).forEach(name => el.classList.add(name));
}

function removeReplayClasses() {
  const layer = byId('cvNodes');
  if (layer) {
    const nodes = layer.querySelectorAll('[data-node-id]');
    for (let index = 0; index < nodes.length; index += 1) {
      const el = nodes[index];
      NODE_STATE_CLASSES.forEach(name => el.classList.remove(name));
      delete el.dataset.replay;
      delete el.dataset.replayKind;
      delete el.dataset.wave;
      const slots = el.querySelectorAll('.rtc-slot[data-slot]');
      for (let slotIndex = 0; slotIndex < slots.length; slotIndex += 1) {
        SLOT_STATE_CLASSES.forEach(name => slots[slotIndex].classList.remove(name));
        delete slots[slotIndex].dataset.status;
      }
      const gates = el.querySelectorAll('.rtc-gate');
      for (let gateIndex = 0; gateIndex < gates.length; gateIndex += 1) {
        gates[gateIndex].textContent = '';
        gates[gateIndex].classList.remove('is-ok', 'is-false', 'is-unk');
      }
      const selected = el.querySelectorAll('.rtc-opt.is-sel');
      for (let optIndex = 0; optIndex < selected.length; optIndex += 1) selected[optIndex].classList.remove('is-sel');
      const keywords = el.querySelectorAll('.rtc-kw');
      for (let kwIndex = 0; kwIndex < keywords.length; kwIndex += 1) {
        keywords[kwIndex].classList.remove('is-hit', 'is-miss');
      }
    }
  }
  const svg = byId('cvEdges');
  if (svg) {
    svg.classList.remove('is-replay');
    const paths = svg.querySelectorAll('path.rtc-edge');
    for (let index = 0; index < paths.length; index += 1) {
      EDGE_STATE_CLASSES.forEach(name => paths[index].classList.remove(name));
      /* final 边的绿色是独立 class：不清它会从一次 final 回放漏进无图事件快照。 */
      paths[index].classList.remove('is-final-edge');
      delete paths[index].dataset.status;
      delete paths[index].dataset.step;
    }
  }
}

function paintQuestion(el, node, nodeState) {
  const done = nodeState.status === 'done';
  if (nodeState.status === 'failed') markNode(el, 'failed', ['is-fail']);
  else if (!done) markNode(el, 'not-triggered', ['is-dim']);
  else markNode(el, 'done');
  setLive(el, 'state', done
    ? tf('cv.tryReturned', { n: nodeState.wave })
    : (nodeState.status === 'failed' ? t('cv.tryFailedShort') : t('cv.tryNotTriggered')));
  if (done && nodeState.wave !== null && nodeState.wave !== undefined) el.dataset.wave = String(nodeState.wave);
  else delete el.dataset.wave;
  if (node.question_type === 'choice') {
    const options = el.querySelectorAll('.rtc-opt');
    for (let index = 0; index < options.length; index += 1) {
      const option = options[index];
      const key = option.dataset.optkey;
      const selected = done && nodeState.ports && nodeState.ports['selected.' + key] === true;
      option.classList.toggle('is-sel', !!selected);
      const probability = done && nodeState.ports ? nodeState.ports['prob.' + key] : undefined;
      setLive(option, 'prob.' + key, probability === undefined ? '—' : num(probability));
    }
  } else if (node.question_type === 'noul') {
    setLive(el, 'yes', done && nodeState.ports ? numOrDash(nodeState.ports.yes) : '—');
    setLive(el, 'no', done && nodeState.ports ? numOrDash(nodeState.ports.no) : '—');
  } else {
    setLive(el, 'value', done && nodeState.ports ? numOrDash(nodeState.ports.value) : '—');
  }
}

function paintLogic(el, nodeState) {
  const gate = el.querySelector('.rtc-gate');
  const done = nodeState.status === 'done';
  markNode(el, done ? 'done' : 'unknown');
  if (!gate) return;
  gate.classList.remove('is-ok', 'is-false', 'is-unk');
  if (done) {
    gate.textContent = String(nodeState.value);
    gate.classList.add(nodeState.value ? 'is-ok' : 'is-false');
  } else {
    gate.textContent = t('cv.tryUnknown');
    gate.classList.add('is-unk');
  }
}

function paintCondition(el, nodeState) {
  const value = nodeState.ports ? nodeState.ports.out : undefined;
  const mode = value === true ? 'hit' : value === false ? 'miss' : 'unknown';
  markNode(el, mode, value === true ? ['is-replay'] : value === false ? [] : ['is-unknown']);
  setLive(el, 'state', value === true ? t('cv.tryHit') : value === false ? t('cv.tryMiss') : t('cv.tryNotRun'));
  const keywords = el.querySelectorAll('.rtc-kw');
  for (let index = 0; index < keywords.length; index += 1) {
    keywords[index].classList.toggle('is-hit', value === true);
    keywords[index].classList.toggle('is-miss', value === false);
  }
}

function paintTarget(el, nodeState) {
  let mode = 'not-triggered';
  const classes = [];
  if (nodeState.final) {
    mode = nodeState.kind === 'error_fallback' ? 'fallback' : 'final';
    classes.push(nodeState.kind === 'error_fallback' ? 'is-fallback' : 'is-final');
  } else if (nodeState.candidate) {
    mode = 'candidate';
    classes.push('is-cand');
  } else {
    classes.push('is-dim');
  }
  markNode(el, mode, classes);
  if (nodeState.kind) el.dataset.replayKind = nodeState.kind;
  const foot = el.querySelector('.rtc-tfoot [data-live="state"]') || el.querySelector('.rtc-tstate');
  if (!foot) return;
  if (nodeState.final) {
    foot.textContent = nodeState.kind === 'error_fallback'
      ? tf('cv.tryFallbackPick', { priority: nodeState.priority })
      : tf('cv.tryFinalPick', { priority: nodeState.priority });
  } else if (nodeState.candidate) {
    foot.textContent = tf('cv.tryCandidate', { priority: nodeState.priority });
  } else {
    foot.textContent = t('cv.tryNotInRun');
  }
}

function paintStart(el) {
  markNode(el, 'done');
  setLive(el, 'state', 'true');
}

function errorPathEdges(replay) {
  return replay && replay.fallback && replay.final && replay.final.kind === 'error_fallback'
    ? (replay.fallback.edge_ids || [])
    : [];
}

/* 最终命中链路：只有胜出候选到最终目标的那条入边涂绿色；
   兜底/异常路径不涂绿，仍由 errorPathEdges 的琥珀整链表达。 */
function finalEdgeIds(replay, graph) {
  const ids = Object.create(null);
  if (!replay || !replay.final || replay.final.kind === 'error_fallback') return ids;
  const final = replay.final;
  (final.candidates || []).forEach(candidate => {
    if (!candidate || candidate.target_id !== final.target_id) return;
    const edge = (graph.edges || []).find(item => item && item.source === candidate.via_node &&
      item.target === candidate.target_id && item.target_port === candidate.via_port);
    if (edge) ids[edge.id] = true;
  });
  return ids;
}

/* 边第一次通过的波次：后端每轮都会重评所有槽位，只看最后一次会把整条链路
   压到最深的一波；history 里第一条 status=true 才是它真正被选中的时刻。 */
function edgeWaveOf(edgeState) {
  if (!edgeState) return null;
  const history = edgeState.history || [];
  for (let index = 0; index < history.length; index += 1) {
    const record = history[index];
    if (record && record.status === 'true') return record.wave;
  }
  return edgeState.wave;
}

/* 波次 → 色阶：开始/条件（第 0 轮）最浅，之后每波加深一档，最多三档。
   整条被走通的链路都要着色，否则前缀是灰的、只红末端，一眼看不出完整路径。 */
function waveStepOf(replay, wave) {
  if (wave === 0 || wave === null || wave === undefined) return 1;
  const waves = (replay.waves || []).map(item => item.wave);
  const index = waves.indexOf(wave);
  if (index < 0) return 1;
  return Math.min(index + 2, 3);
}

function paintSlots(replay, graph) {
  const errorEdges = errorPathEdges(replay);
  (graph.edges || []).forEach(edge => {
    const row = findSlotEl(edge.target, edge.target_port);
    if (!row) return;
    const edgeState = replay.edgeStates[edge.id];
    const status = edgeState ? edgeState.status : 'unknown';
    row.dataset.status = status;
    row.dataset.edge = edge.id;
    row.classList.remove.apply(row.classList, SLOT_STATE_CLASSES);
    row.classList.add('is-' + (status === 'true' ? 'true' : status === 'false' ? 'false'
      : status === 'invalid' ? 'invalid' : 'unknown'));
    /* 回退目标的入边槽位与 SVG 边一起走整链琥珀。 */
    if (errorEdges.indexOf(edge.id) >= 0) row.classList.add('is-error-path');
    const live = row.querySelector('.rtc-live');
    if (live) {
      live.textContent = edgeState
        ? rawText(edgeState.raw) + ' → ' + statusText(edgeState.status)
        : t('cv.tryUnknown');
    }
    row.title = edgeState ? slotLine(edgeState) : t('cv.tryUnknown');
  });
}

function paintEdges(replay, graph) {
  const svg = byId('cvEdges');
  if (svg) svg.classList.add('is-replay');
  const kit = kitNow();
  const errorEdges = errorPathEdges(replay);
  const finalEdges = finalEdgeIds(replay, graph);
  (graph.edges || []).forEach(edge => {
    const path = kit && kit.edgeEl ? kit.edgeEl(edge.id) : null;
    if (!path) return;
    const edgeState = replay.edgeStates[edge.id];
    const status = edgeState ? edgeState.status : 'unknown';
    EDGE_STATE_CLASSES.forEach(name => path.classList.remove(name));
    path.classList.remove('is-final-edge');
    delete path.dataset.step;
    path.classList.add('is-' + (status === 'true' ? 'true' : status === 'false' ? 'false'
      : status === 'invalid' ? 'invalid' : 'unknown'));
    path.dataset.status = status;
    if (status === 'true') path.dataset.step = String(waveStepOf(replay, edgeWaveOf(edgeState)));
    if (finalEdges[edge.id]) path.classList.add('is-final-edge');
    if (errorEdges.indexOf(edge.id) >= 0) path.classList.add('is-error-path');
  });
}

function apply(replay) {
  state = replay;
  if (!replay) {
    renderResult(null);
    return;
  }
  /* 路由集结果没有当前画布坐标：只渲染结论/事实，绝不 paint、也不清掉当前草稿
     的静态图。草稿与日志快照继续回放到当前画布（逐字保留既有行为）。 */
  const graph = replay.scope === 'route_set' ? null : graphNow();
  if (graph) {
    /* 先清旧回放样式：无图事件时只保留静态图，绝不能继承上一次回放的涂色。 */
    removeReplayClasses();
    if (replay.hasGraphEvents) {
      (graph.nodes || []).forEach(node => {
        const el = findNodeEl(node.id);
        if (!el) return;
        const nodeState = replay.nodeStates[node.id] || { status: 'not-triggered', ports: null };
        el.classList.add('is-replay');
        /* 波次也写在卡片上：类型色条在回放时让位给链路色阶（目标卡保持类型色，
           最终命中由 is-final 单独着色）。 */
        if (node.type !== 'target' && nodeState.status !== 'not-triggered' &&
          nodeState.wave !== null && nodeState.wave !== undefined) {
          el.dataset.wave = String(nodeState.wave);
        } else if (node.type !== 'question') {
          delete el.dataset.wave;
        }
        if (node.type === 'start') paintStart(el);
        else if (node.type === 'question') paintQuestion(el, node, nodeState);
        else if (node.type === 'logic') paintLogic(el, nodeState);
        else if (node.type === 'condition') paintCondition(el, nodeState);
        else if (node.type === 'target') paintTarget(el, nodeState);
      });
      paintSlots(replay, graph);
      paintEdges(replay, graph);
    }
  }
  renderResult(replay);
}

/* ── 结论 / HUD / 抽屉 DOM ────────────────────────────────────── */
function totalDuration(replay) {
  return replay.waves.reduce((sum, wave) => sum + (Number(wave.duration_ms) || 0), 0);
}

function finalKindLabel(replay) {
  const kind = replay.final.sticky_hit ? 'sticky' : replay.final.kind;
  return t(KIND_KEYS[kind] || KIND_KEYS.target);
}

/* 候选比较脚注：把「谁赢、为什么」写成一句人话；优先级数字大者优先，所以用 > 不用 <。 */
function candidateNote(replay) {
  const final = replay.final;
  if (final.kind === 'error_fallback') {
    const failed = replay.waves.find(wave => wave.failed);
    return t('cv.tryErrorPath') + (failed && failed.error ? ' · ' + failed.error : '');
  }
  if (final.kind === 'otherwise') return t('cv.tryFallbackReason');
  if (final.candidates.length > 1) {
    const winner = final.candidates.find(candidate => candidate.target_id === final.target_id) || final.candidates[0];
    const rival = final.candidates.find(candidate => candidate.target_id !== final.target_id) || final.candidates[1];
    if (!rival) {
      return tf('cv.tryUniqueTarget', {
        target: resultNodeName(replay, final.target_id), priority: final.priority
      });
    }
    if (winner.priority === rival.priority) {
      return tf('cv.tryPriorityTie', {
        priority: winner.priority, winner: resultNodeName(replay, winner.target_id)
      });
    }
    return tf('cv.tryPriorityWin', {
      winner: winner.target_id, priority: winner.priority,
      rival: rival.target_id, rivalPriority: rival.priority
    });
  }
  return tf('cv.tryUniqueTarget', {
    target: resultNodeName(replay, final.target_id), priority: final.priority
  });
}

function factHTML(labelKey, value) {
  return '<span class="rtc-try-fact"><i>' + esc(t(labelKey)) + '</i><b>' + esc(value) + '</b></span>';
}

/* 画布快照与数据库目录的差异只是 hint：展示资源/字段与两侧值，
   绝不把它升级为运行失败或重跑语义。值可能很长（instructions/state_template/
   大 profile），单行只展示截断预览；完整值不进 DOM，避免撑爆提示行。 */
const MISMATCH_VALUE_MAX = 120;

function mismatchValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch (error) { return String(value); }
  }
  return rawText(value);
}

function mismatchPreview(value) {
  const text = mismatchValue(value);
  return text.length <= MISMATCH_VALUE_MAX
    ? text
    : text.slice(0, MISMATCH_VALUE_MAX - 1) + '…';
}

function mismatchHTML(replay) {
  const warnings = Array.isArray(replay.warnings) ? replay.warnings : [];
  const drifts = warnings.filter(item => item && item.code === 'runtime_mismatch');
  if (!drifts.length) return '';
  const lines = drifts.map(item => {
    const params = item.params || {};
    const kindKey = MISMATCH_KIND_KEYS[params.resource_kind];
    const kind = kindKey ? t(kindKey) : String(params.resource_kind || '');
    const line = tf('cv.tryMismatchLine', {
      kind: kind, id: params.resource_id || '', field: params.field || ''
    });
    const values = tf('cv.tryMismatchValues', {
      canvas: mismatchPreview(params.canvas),
      database: mismatchPreview(params.database)
    });
    return '<div class="rtc-try-cmp"><span class="rtc-try-note is-warn">' +
      esc(line) + ' · ' + esc(values) + '</span></div>';
  });
  return '<div class="rtc-try-mismatch" data-runtime-mismatch="' + drifts.length + '">' +
    '<div class="rtc-try-cmp"><span class="rtc-try-cmp-l">' +
      esc(t('cv.tryMismatchTitle')) + '</span></div>' +
    lines.join('') + '</div>';
}

function candidateChips(replay) {
  const final = replay.final;
  if (!final.candidates.length) {
    return '<span class="rtc-try-none">' + esc(t('cv.tryNoCandidates')) + '</span>' +
      '<span class="rtc-try-op">→</span>' +
      '<span class="rtc-try-pick">' + esc(t('cv.tryFallback')) + ' ' +
        esc(resultNodeName(replay, final.target_id)) + '</span>';
  }
  return final.candidates.map((candidate, index) => {
    const win = candidate.target_id === final.target_id;
    return (index ? '<span class="rtc-try-op">›</span>' : '') +
      '<span class="rtc-try-cand' + (win ? ' is-win' : '') + '"' +
      ' data-candidate="' + esc(candidate.target_id) + '" data-priority="' + esc(candidate.priority) + '"' +
      ' title="' + esc(candidate.target_id) + '">' +
      esc(resultNodeName(replay, candidate.target_id)) + '<i>p' + esc(candidate.priority) + '</i>' +
      (win ? '<em>' + esc(t('cv.tryWin')) + '</em>' : '') +
      '</span>';
  }).join('');
}

function renderResult(replay) {
  const host = byId('cvTryResult');
  if (!host) return;
  if (!replay || (!replay.final && !replay.error)) {
    host.hidden = true;
    host.innerHTML = '';
    delete host.dataset.final;
    delete host.dataset.kind;
    delete host.dataset.selection;
    delete host.dataset.error;
    delete host.dataset.tree;
    delete host.dataset.graphSelection;
    return;
  }
  /* error 与 final 独立维护：后续调度/上游失败既不清除 final，也不被 final 掩盖。 */
  if (replay.error) host.dataset.error = 'routing_error';
  else delete host.dataset.error;
  if (!replay.final) {
    /* 无 final 的失败回放：只读失败结论，不显示候选/优先级/波次等成功语义。 */
    const error = replay.error;
    host.hidden = false;
    delete host.dataset.final;
    host.dataset.kind = 'routing_error';
    host.dataset.selection = 'routing_error';
    /* 失败回放也可能带选图证据（route_set 选图后执行失败）：照样暴露，避免
       dataset.tree 停在上一次成功回放的旧值。 */
    host.dataset.tree = replay.treeId || '';
    host.dataset.graphSelection = replay.graphSelection || '';
    host.innerHTML =
      '<div class="rtc-try-res-h">' +
        '<span class="rtc-try-kind is-fb">' + esc(t('cv.tryRoutingFailed')) + '</span>' +
        '<span class="rtc-try-lbl">' + esc(t('cv.tryFinal')) + '</span>' +
        '<b class="rtc-try-final">' + esc(error.error_class || t('cv.tryUnknown')) + '</b>' +
        (error.stage ? '<span class="rtc-try-id">' + esc(error.stage) + '</span>' : '') +
        '<span class="rtc-try-acts">' +
          (isReadOnly() ? '' :
            '<button type="button" class="btn sm" id="cvTryRerun" data-try-act="rerun">' +
              esc(t('cv.tryRerun')) + '</button>' +
            '<button type="button" class="btn sm" id="cvTryClear" data-try-act="clear">' +
              esc(t('cv.tryClear')) + '</button>') +
        '</span>' +
      '</div>' +
      '<div class="rtc-try-facts">' +
        factHTML('cv.tryFactStage', error.stage || '—') +
        factHTML('cv.tryFactError', error.error_class || '—') +
      '</div>' +
      (error.message
        ? '<div class="rtc-try-cmp"><span class="rtc-try-note is-warn">' +
            esc(error.message) + '</span></div>'
        : '') +
      mismatchHTML(replay);
    return;
  }
  const final = replay.final;
  host.hidden = false;
  host.dataset.final = final.target_id;
  host.dataset.kind = final.kind;
  host.dataset.selection = final.sticky_hit ? 'sticky' : (final.selection_reason || final.kind);
  /* 实际选图证据与最终目标分离：tree_id / graph_selection 描述「选了哪张图」，
     dataset.selection 描述「目标为什么胜出」，两者不能互相覆盖。 */
  host.dataset.tree = replay.treeId || '';
  host.dataset.graphSelection = replay.graphSelection || '';
  const kindClass = final.kind === 'target' && !final.sticky_hit ? 'ok'
    : final.kind === 'otherwise' ? 'cand' : 'fb';
  const priorityText = final.priority === null || final.priority === undefined
    ? '—' : 'p' + final.priority;
  const waveText = tf('cv.tryWaves', { n: replay.waves.length }) + ' · ' +
    tf('cv.tryCalls', { n: replay.waves.length });
  /* 命中目标之后的失败是附加事实：复用失败分支的样式与文案，不重写 final。 */
  const errorBadge = replay.error
    ? '<span class="rtc-try-kind is-fb" data-routing-error>' +
      esc(t('cv.tryRoutingFailed')) + '</span>'
    : '';
  const errorFacts = replay.error
    ? factHTML('cv.tryFactStage', replay.error.stage || '—') +
      factHTML('cv.tryFactError', replay.error.error_class || '—')
    : '';
  const errorMessage = replay.error && replay.error.message
    ? '<div class="rtc-try-cmp"><span class="rtc-try-note is-warn">' +
      esc(replay.error.message) + '</span></div>'
    : '';
  /* 路由集回放不属于当前草图：不提供「按波次整理」，否则会按另一张图的波次
     改写当前草稿的节点位置。日志快照（只读）依旧不提供任何写入口。 */
  const wavesAction = !isReadOnly() && replay.scope !== 'route_set'
    ? '<button type="button" class="btn sm" id="cvTryWavesBtn" data-try-act="waves">' +
      esc(t('cv.tryWavesLayout')) + '</button>'
    : '';
  host.innerHTML =
    '<div class="rtc-try-res-h">' +
      '<span class="rtc-try-kind is-' + kindClass + '">' + esc(finalKindLabel(replay)) + '</span>' +
      errorBadge +
      '<span class="rtc-try-lbl">' + esc(t('cv.tryFinal')) + '</span>' +
      '<b class="rtc-try-final" title="' + esc(final.target_id) + '">' +
        esc(resultNodeName(replay, final.target_id)) + '</b>' +
      '<span class="rtc-try-id">' + esc(final.target_id) + '</span>' +
      (final.model ? '<span class="rtc-try-model">' + esc(final.model) + '</span>' : '') +
      '<span class="rtc-try-acts">' +
        (isReadOnly() ? '' :
          wavesAction +
          '<button type="button" class="btn sm" id="cvTryRerun" data-try-act="rerun">' +
            esc(t('cv.tryRerun')) + '</button>' +
          '<button type="button" class="btn sm" id="cvTryClear" data-try-act="clear">' +
            esc(t('cv.tryClear')) + '</button>') +
      '</span>' +
    '</div>' +
    '<div class="rtc-try-facts">' +
      factHTML('cv.tryFactTree', replay.treeId || '—') +
      factHTML('cv.tryFactSelection', replay.graphSelection == null ? '—' : replay.graphSelection) +
      factHTML('cv.tryFactPriority', priorityText) +
      factHTML('cv.tryFactWaves', waveText) +
      factHTML('cv.tryFactCost', ms(totalDuration(replay))) +
      errorFacts +
    '</div>' +
    '<div class="rtc-try-cmp">' +
      '<span class="rtc-try-cmp-l">' + esc(t('cv.tryCandidates')) + '</span>' +
      candidateChips(replay) +
      '<span class="rtc-try-note' + (final.kind === 'error_fallback' ? ' is-warn' : '') + '">' +
        esc(candidateNote(replay)) + '</span>' +
    '</div>' +
    errorMessage +
    mismatchHTML(replay);
}

/* ── run 生命周期 ─────────────────────────────────────────────── */
function setBusy(busy) {
  const runButton = byId('cvTryRun');
  if (runButton) {
    runButton.disabled = busy;
    runButton.innerHTML = busy
      ? '<span class="rtc-spin" aria-hidden="true"></span>' + esc(t('cv.tryRunning'))
      : esc(t('cv.tryRun'));
  }
  const topButton = byId('cvTryBtn');
  if (topButton) topButton.disabled = busy;
  /* 请求在途时输入框与样例 chip 一律禁用：chip 不能在飞行中改写输入。 */
  const input = byId('cvTryInput');
  if (input) input.disabled = busy;
  const chips = byId('cvTryChips');
  if (chips) {
    const buttons = chips.querySelectorAll('button');
    for (let index = 0; index < buttons.length; index += 1) buttons[index].disabled = busy;
  }
  const kit = kitNow();
  if (kit && typeof kit.setLocked === 'function') kit.setLocked(busy);
  updateToolbarNow();
  document.body.classList.toggle('is-try-running', !!busy);
}

function collapsePanels() {
  if (deps && typeof deps.collapsePanels === 'function') {
    deps.collapsePanels();
    return;
  }
  const palette = byId('cvPalette');
  if (palette) palette.classList.add('is-collapsed');
  const inspector = byId('cvInspector');
  if (inspector) inspector.classList.add('is-collapsed');
}

function hideResultPanels() {
  renderResult(null);
}

/* 按波次整理后，未触发列没有节点 id 可读；补一个 world 坐标里的列头标签。 */
function clearWaveHeader() {
  if (waveHeader && waveHeader.parentNode) waveHeader.parentNode.removeChild(waveHeader);
  waveHeader = null;
  waveLayout = null;
}

function renderWaveHeader() {
  if (!waveLayout) { clearWaveHeader(); return; }
  const world = byId('cvWorld');
  if (!world) return;
  if (!waveHeader || waveHeader.parentNode !== world) {
    waveHeader = document.createElement('div');
    waveHeader.className = 'rtc-wavehead';
    world.appendChild(waveHeader);
  }
  waveHeader.textContent = t('cv.tryUntriggeredColumn');
  waveHeader.style.left = waveLayout.x + 'px';
  waveHeader.style.top = waveLayout.y + 'px';
}

/* 数据/浮层复位，不重绘卡片（供 App.change 在 renderAll 前调用）。 */
function reset() {
  runSeq += 1;
  state = null;
  const store = storeNow();
  /* 运行中 reset（清除/换组）必须同时释放锁，不能把 tryRunning 留给迟到的 finally。 */
  if (store) store.setTryRunning(false);
  clearWaveHeader();
  hideResultPanels();
  removeReplayClasses();
  setBusy(false);
}

function clear() {
  reset();
  renderNow();
  toastNow(t('cv.tryClearDone'));
  return true;
}

function payloadFor(text, now) {
  /* 试算范围必须先判断（Task 9）：route_set 是「真实已发布路由集」请求，只带
     text/session_id/可选 department/now，绝不携带草稿的 root/tree_id/canvas/
     timezone/sticky 与四运行字段 —— 这些字段一旦出现，服务端就会切到草稿路径，
     按部门选图就不会发生。 */
  const scopeSelect = byId('cvTryScope');
  const scope = scopeSelect ? String(scopeSelect.value || 'draft') : 'draft';
  if (scope === 'route_set') {
    const payload = { text: text, session_id: '' };
    const departmentInput = byId('cvTryDepartment');
    const department = departmentInput ? String(departmentInput.value || '').trim() : '';
    if (department) payload.department = department;
    if (now) payload.now = now;
    return payload;
  }
  const graph = graphNow();
  const tree = treeNow();
  /* 画布坐标是内部工作状态：试算 payload 顶层只带 canvas，节点对象不带 position。 */
  const boundary = RTGraph.collectCanvas({ root: clone(graph) });
  const payload = {
    root: boundary.root,
    canvas: boundary.canvas,
    timezone: tree && tree.timezone ? tree.timezone : 'Asia/Shanghai',
    sticky: clone((tree && tree.sticky) || { enabled: false, ttl_seconds: 1800 }),
    text: text,
    session_id: ''
  };
  /* 完整草稿请求无条件携带四运行字段：试算严格使用画布快照，服务端不按任何
     图或数据库继承。画布不变量保证 runtime 完整；万一缺失则退回引用已存树
     （tree_id），由后端继承该树的已存快照，而不是发一个 root-only 半草稿。 */
  const runtime = RTGraph.runtimeDocumentView(tree);
  if (runtime) Object.assign(payload, runtime);
  else if (tree && tree.id) payload.tree_id = tree.id;
  if (now) payload.now = now;
  return payload;
}

async function run(text, options) {
  const store = storeNow();
  if (!store || !store.tree || !store.graph) return false;
  if (!isBootedNow()) {
    toastNow(t('cv.toastNotReady'), 'warn');
    return false;
  }
  if (store.tryRunning) return false;
  const opts = options || {};
  const inputText = String(text == null ? '' : text);
  const now = opts.now === undefined || opts.now === null ? null : String(opts.now);
  lastRun = { text: inputText, now: now };
  const token = ++runSeq;
  state = null;
  store.setTryRunning(true);
  try {
    /* 收起面板/重绘/锁画布都在 try 内：任一步抛错也必须走 finally 释放运行锁。 */
    hideResultPanels();
    collapsePanels();
    /* 运行锁会拒绝所有 store.change()：条件浮层若还开着，确认只会得到
       false。先关掉它，避免用户以为「确认成功」而实际草稿未变。 */
    if (isCondPopOpen()) closeCondition();
    removeReplayClasses();
    renderNow();
    setBusy(true);
    const input = byId('cvTryInput');
    if (input && input.value !== inputText) input.value = inputText;
    /* 范围在发请求前定格：运行期间用户改 select 也不能把回放归到另一个范围，
       回放与请求体（payloadFor 同 tick 读取）必须描述同一次试算。 */
    const requestScope = currentScope();
    const response = await api.post('/admin/try', payloadFor(inputText, now));
    if (token !== runSeq) return false;
    /* 路由集回放不属于当前画布：用空图重建，target/candidate/wave 全部来自 trace
       与 response.outcome；草稿范围才拿当前图作为回放底图。 */
    const replayGraph = requestScope === 'route_set' ? { nodes: [], edges: [] } : graphNow();
    const rebuilt = rebuild(response.trace || [], replayGraph);
    rebuilt.scope = requestScope;
    rebuilt.outcome = response.outcome || null;
    rebuilt.answers = response.answers || null;
    rebuilt.warnings = Array.isArray(response.warnings) ? response.warnings : [];
    rebuilt.draft = response.draft === true;
    /* 实际选图证据（Task 9）：route_set 路径返回 tree_id + graph_selection；
       草稿路径的 graph_selection 是 null，tree_id 是服务端的草稿快照 id（draft）。 */
    rebuilt.treeId = typeof response.tree_id === 'string' ? response.tree_id : '';
    rebuilt.graphSelection = typeof response.graph_selection === 'string'
      ? response.graph_selection : null;
    rebuilt.trace = Array.isArray(response.trace) ? response.trace : [];
    rebuilt.text = inputText;
    rebuilt.now = now;
    rebuilt.id = token;
    rebuilt.request = { text: inputText, now: now };
    state = rebuilt;
    apply(rebuilt);
    return true;
  } catch (error) {
    if (token !== runSeq) return false;
    state = null;
    try {
      hideResultPanels();
      removeReplayClasses();
      renderNow();
    } catch (cleanupError) {
      /* 清理失败不能盖住原始错误；finally 仍会释放运行锁。 */
    }
    toastNow(tf('cv.tryFailed', { err: apiErrText(error) }), 'error');
    return false;
  } finally {
    if (token === runSeq) {
      store.setTryRunning(false);
      setBusy(false);
    }
  }
}

function rerun() {
  if (!lastRun) return run('');
  return run(lastRun.text, { now: lastRun.now });
}

/* ── Step 4 · 按波次整理（只写 position，一次 App.change） ──────── */
function layoutByWaves() {
  const replay = state;
  /* 防御：路由集结果描述的是已发布图，绝不能拿它的波次去重排当前草稿画布
     （按钮已隐藏，这里再拦一次直接调用）。 */
  if (replay && replay.scope === 'route_set') {
    toastNow(t('cv.tryRouteSetLayoutBlocked'), 'warn');
    return false;
  }
  const graph = graphNow();
  if (!graph) return false;
  if (!replay || !replay.final) {
    toastNow(t('cv.tryWavesLayoutNone'), 'warn');
    return false;
  }
  const maxWave = replay.waves.reduce((max, wave) => Math.max(max, wave.wave), 0);
  const waveOf = Object.create(null);
  graph.nodes.forEach(node => {
    const nodeState = replay.nodeStates[node.id] || {};
    if (node.type === 'start' || node.type === 'condition') waveOf[node.id] = 0;
    else if (node.type === 'question') {
      waveOf[node.id] = (nodeState.status === 'done' || nodeState.status === 'failed') &&
        Number.isInteger(nodeState.wave) ? nodeState.wave : null;
    } else if (node.type === 'target') {
      if (nodeState.final) waveOf[node.id] = Number.isInteger(nodeState.wave) ? nodeState.wave : maxWave;
      else if (nodeState.candidate && Number.isInteger(nodeState.wave)) waveOf[node.id] = nodeState.wave;
      else waveOf[node.id] = null;
    } else {
      waveOf[node.id] = null;
    }
  });
  /* 逻辑门取所有输入的最大波次；输入未触发则门也进入未触发列。 */
  let changed = true;
  while (changed) {
    changed = false;
    graph.nodes.forEach(node => {
      if (node.type !== 'logic' || waveOf[node.id] !== null) return;
      const incoming = (graph.edges || []).filter(edge => edge.target === node.id);
      if (!incoming.length) return;
      const inputWaves = incoming.map(edge => waveOf[edge.source]);
      if (inputWaves.some(wave => !Number.isInteger(wave))) return;
      waveOf[node.id] = Math.max.apply(null, inputWaves);
      changed = true;
    });
  }
  const columns = Object.create(null);
  const untriggered = [];
  graph.nodes.forEach(node => {
    const wave = waveOf[node.id];
    if (Number.isInteger(wave)) (columns[wave] = columns[wave] || []).push(node);
    else untriggered.push(node);
  });
  const COLUMN_WIDTH = 260;
  const ROW_HEIGHT = 122;
  const PAD = 30;
  const positions = Object.create(null);
  Object.keys(columns).map(Number).sort((a, b) => a - b).forEach(wave => {
    columns[wave].slice().sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
      .forEach((node, index) => {
        positions[node.id] = { x: PAD + wave * COLUMN_WIDTH, y: PAD + index * ROW_HEIGHT };
      });
  });
  untriggered.slice().sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .forEach((node, index) => {
      positions[node.id] = { x: PAD + (maxWave + 1) * COLUMN_WIDTH, y: PAD + index * ROW_HEIGHT };
    });
  const changedPosition = graph.nodes.some(node => {
    const next = positions[node.id];
    const current = node.position || {};
    return !!next && (next.x !== current.x || next.y !== current.y);
  });
  /* 与自动整理一致：没移动就不入历史；App.change 拒绝（运行中/只读）时不得报成功。 */
  if (changedPosition) {
    if (!deps || typeof deps.change !== 'function') return false;
    const accepted = deps.change('layout', tree => {
      tree.root.nodes.forEach(node => {
        if (positions[node.id]) node.position = { x: positions[node.id].x, y: positions[node.id].y };
      });
    });
    if (accepted === false) return false;
  }
  if (untriggered.length) {
    waveLayout = { x: PAD + (maxWave + 1) * COLUMN_WIDTH, y: PAD - 18 };
    renderWaveHeader();
  } else {
    clearWaveHeader();
  }
  const kit = kitNow();
  if (kit && kit.fit) kit.fit({ padding: 24 });
  toastNow(t(changedPosition ? 'cv.tryWavesLayoutDone' : 'cv.tryWavesLayoutSame'));
  return true;
}

/* ── 试算台绑定 ───────────────────────────────────────────────── */
/* 试算范围（Task 9）：draft = 当前草稿（默认，既有行为）；route_set = 已发布
   实际路由集，按部门选图。控件由模块注入到输入行与样例 chips 之间，不新增
   静态 HTML；可见性与禁用状态由 app.js 的 applyTryScope() 统一维护。 */
function ensureScopeControls() {
  const bar = byId('cvTryBar');
  if (!bar || byId('cvTryScope')) return;
  const host = document.createElement('div');
  host.className = 'rtc-try-scope';
  host.id = 'cvTryScopeRow';
  host.innerHTML =
    '<label class="rtc-try-scope-field">' +
      '<span data-i18n="cv.tryScope">' + esc(t('cv.tryScope')) + '</span>' +
      '<select id="cvTryScope" data-i18n-aria="cv.tryScope" aria-label="' + esc(t('cv.tryScope')) + '">' +
        '<option value="draft" data-i18n="cv.tryScopeDraft">' + esc(t('cv.tryScopeDraft')) + '</option>' +
        '<option value="route_set" data-i18n="cv.tryScopeRouteSet">' + esc(t('cv.tryScopeRouteSet')) + '</option>' +
      '</select></label>' +
    '<label class="rtc-try-scope-field" id="cvTryDepartmentLabel" hidden>' +
      '<span data-i18n="cv.tryDepartment">' + esc(t('cv.tryDepartment')) + '</span>' +
      '<input id="cvTryDepartment" type="text" autocomplete="off" spellcheck="false"' +
      ' data-i18n-placeholder="cv.tryDepartmentPlaceholder" aria-label="' + esc(t('cv.tryDepartment')) +
      '" placeholder="' + esc(t('cv.tryDepartmentPlaceholder')) + '"></label>' +
    '<span class="rtc-try-scope-hint" data-i18n="cv.tryScopeHint">' + esc(t('cv.tryScopeHint')) + '</span>';
  const row = bar.querySelector('.rtc-try-row');
  if (row && row.nextSibling) bar.insertBefore(host, row.nextSibling);
  else bar.appendChild(host);
}

function renderChips() {
  const host = byId('cvTryChips');
  if (!host) return;
  const store = storeNow();
  const busy = !!(store && store.tryRunning);
  host.innerHTML = SAMPLES.map((sample, index) =>
    '<button type="button" class="rtc-trychip" data-try-sample="' + index + '"' +
    (busy ? ' disabled' : '') + '>' +
    esc(t(sample.labelKey)) + '</button>').join('');
  const input = byId('cvTryInput');
  if (input) input.placeholder = t('cv.tryPlaceholder');
}

function onAction(event) {
  const action = event.target.closest('[data-try-act]');
  if (!action) return;
  const name = action.dataset.tryAct;
  if (name === 'rerun') { rerun(); return; }
  if (name === 'clear') { clear(); return; }
  if (name === 'waves') { layoutByWaves(); }
}

function init() {
  if (bound) return;
  bound = true;
  ensureScopeControls();
  renderChips();
  const input = byId('cvTryInput');
  if (input) {
    input.addEventListener('keydown', event => {
      if (event.key !== 'Enter' || event.isComposing) return;
      event.preventDefault();
      run(input.value);
    });
  }
  const chips = byId('cvTryChips');
  if (chips) {
    chips.addEventListener('click', event => {
      const chip = event.target.closest('[data-try-sample]');
      if (!chip) return;
      const store = storeNow();
      if (store && store.tryRunning) return;
      const sample = SAMPLES[Number(chip.dataset.trySample)];
      if (!sample) return;
      const textInput = byId('cvTryInput');
      if (textInput) textInput.value = sample.text;
      run(sample.text, { now: sample.now });
    });
  }
  const runButton = byId('cvTryRun');
  if (runButton) {
    runButton.addEventListener('click', () => {
      const textInput = byId('cvTryInput');
      run(textInput ? textInput.value : '');
    });
  }
  const topButton = byId('cvTryBtn');
  if (topButton) {
    topButton.addEventListener('click', () => {
      const textInput = byId('cvTryInput');
      if (textInput) textInput.focus();
      run(textInput ? textInput.value : '');
    });
  }
  const result = byId('cvTryResult');
  if (result) result.addEventListener('click', onAction);
}

function refresh() {
  ensureScopeControls();
  renderChips();
  renderWaveHeader();
  if (state) apply(state);
  else renderResult(null);
}

function samples() {
  return SAMPLES.map((sample, index) => ({
    index: index, label: t(sample.labelKey), text: sample.text, now: sample.now
  }));
}

function current() {
  return state;
}

export {
  rebuild,
  run,
  rerun,
  clear,
  reset,
  apply,
  init,
  refresh,
  samples,
  layoutByWaves,
  current
};
