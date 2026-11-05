/* RTTryRun · 真实草稿试算 / 画布回放 / 轨迹抽屉 / 按波次整理（规格 §6.8 / §6.9）
   ------------------------------------------------------------------
   回放唯一数据源：POST /admin/try 返回的 trace（§3.9）。rebuild() 只消费事件，
   绝不调用 RTGraph.evaluateCondition、也不读取 graph_scenarios 的 answers。
   卡片回写走 cards.js 已有的 data-live / data-slot / data-edge 契约；边回写走
   canvas.js 的 edgeEl() + class。
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const byId = id => document.getElementById(id);
  const app = () => root.App || null;
  const t = key => root.t(key);
  const tf = (key, vars) => root.tf(key, vars);
  const esc = value => root.RTCards.escape(value);
  /* 画布上节点用中文显示名；抽屉里同样显示名称，原始 id 留在 tooltip。 */
  const nodeName = id => {
    const graph = app() && app().graph;
    const node = graph && root.RTGraph ? root.RTGraph.findNode(graph, id) : null;
    return node && root.RTCards ? root.RTCards.nodeName(node, graph) : (id || '—');
  };
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

  const ROW_KINDS = {
    condition: true, slot: true, gate: true, question: true, activate: true,
    fallback: true, final: true, sticky_hit: true, scheduled: true
  };
  const ROW_LABEL_KEYS = {
    condition: 'cv.tryRow.condition', slot: 'cv.tryRow.slot', gate: 'cv.tryRow.gate',
    question: 'cv.tryRow.question', activate: 'cv.tryRow.activate',
    fallback: 'cv.tryRow.fallback', final: 'cv.tryRow.final',
    sticky_hit: 'cv.tryRow.sticky', scheduled: 'cv.tryRow.scheduled'
  };
  const KIND_KEYS = {
    target: 'cv.tryKind.target', otherwise: 'cv.tryKind.fallback',
    error_fallback: 'cv.tryKind.fallback', sticky: 'cv.tryKind.sticky'
  };
  const SELECTION_KEYS = {
    target: 'cv.trySelection.target', sticky: 'cv.trySelection.sticky',
    otherwise: 'cv.trySelection.fallback', error_fallback: 'cv.trySelection.fallback'
  };

  let state = null;        /* 最近一次回放（与 App.replay 同一对象） */
  let lastRun = null;      /* {text, now} 供「重跑」复用固定 now */
  let runSeq = 0;
  let drawerOpen = false;
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

  function conditionText(condition) {
    if (condition === null || condition === undefined) return '= true';
    if (condition.op === 'is') return condition.value === true ? '= true' : '= false';
    const symbol = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠' }[condition.op] || condition.op;
    return symbol + ' ' + num(condition.value);
  }

  function statusText(status) {
    if (status === 'unknown') return t('cv.tryUnknown');
    if (status === 'invalid') return t('cv.tryInvalid');
    return status;
  }

  function slotLine(slot) {
    const pieces = [rawText(slot.raw)];
    if (slot.condition !== undefined && slot.condition !== null) pieces.push(conditionText(slot.condition));
    else if (typeof slot.raw === 'boolean') pieces.push('= true');
    pieces.push('→', statusText(slot.status));
    return pieces.join(' ');
  }

  /* ── Step 3 · trace-only rebuild ──────────────────────────────── */
  function rebuild(trace, graph) {
    const events = Array.isArray(trace) ? trace : [];
    const source = graph || (app() && app().graph) || { nodes: [], edges: [] };
    const nodeStates = Object.create(null);
    const edgeStates = Object.create(null);
    const waves = [];
    const timeline = [];
    const meta = { timezone: '', active_periods: [], tokens_before: null, tokens_after: null, truncated: false };
    let candidates = [];
    let final = null;
    let fallback = null;

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
          timeline.push({ kind: 'condition', wave: 0, node_id: event.node_id, value: event.value, matched: nodeState.matched });
          break;
        }
        case 'slot':
          applySlot(event);
          timeline.push({
            kind: 'slot', wave: event.wave, node_id: event.node_id, edge_id: event.edge_id,
            port: event.port, source: event.source, source_port: event.source_port,
            raw: event.raw, condition: event.condition, status: event.status
          });
          break;
        case 'gate': {
          const nodeState = ensureNode(event.node_id, 'logic');
          nodeState.status = 'done';
          nodeState.wave = event.wave;
          nodeState.value = event.value;
          nodeState.ports = Object.assign({}, nodeState.ports || {}, { out: event.value });
          nodeState.inputs = clone(event.inputs || []);
          nodeState.operator = event.operator;
          timeline.push({
            kind: 'gate', wave: event.wave, node_id: event.node_id, operator: event.operator,
            inputs: nodeState.inputs, value: event.value
          });
          break;
        }
        case 'activate': {
          const nodeState = ensureNode(event.node_id, '');
          nodeState.activated = true;
          nodeState.wave = event.wave;
          nodeState.via_port = event.via_port;
          if (nodeState.type === 'target') nodeState.status = 'activated';
          timeline.push({ kind: 'activate', wave: event.wave, node_id: event.node_id, via_port: event.via_port });
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
          timeline.push({
            kind: 'wave', wave: wave.wave, questions: wave.questions,
            duration_ms: wave.duration_ms, failed: wave.failed, error: wave.error
          });
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
          timeline.push({
            kind: 'question', wave: event.wave, node_id: event.node_id, ports: nodeState.ports,
            selected: event.selected, p_top: event.p_top, margin: event.margin,
            duration_ms: event.duration_ms, usage: nodeState.usage
          });
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
          timeline.push({ kind: 'fallback', wave: lastWave(), target_id: event.target_id, reason: fallback.reason });
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
          timeline.push({
            kind: 'final', wave: targetState.wave, target_id: event.target_id, final_kind: final.kind,
            candidates: final.candidates, priority: final.priority
          });
          break;
        }
        case 'sticky_hit': {
          if (final) {
            final.sticky_hit = true;
            final.model = event.model;
          }
          timeline.push({ kind: 'sticky_hit', wave: lastWave(), model: event.model });
          break;
        }
        case 'scheduled': {
          if (final) {
            final.model = event.model;
            if (event.reason) final.selection_reason = event.reason;
          }
          timeline.push({
            kind: 'scheduled', wave: lastWave(), model: event.model,
            reason: event.reason, dry_run: event.dry_run
          });
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
      timeline: timeline,
      meta: meta
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
        delete paths[index].dataset.status;
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
      /* §3.9 question 事件顶层已有 p_top/margin；旧 trace 的 ports 副本只作回退。 */
      const pTop = nodeState.p_top !== undefined && nodeState.p_top !== null
        ? nodeState.p_top : (nodeState.ports ? nodeState.ports.p_top : undefined);
      const margin = nodeState.margin !== undefined && nodeState.margin !== null
        ? nodeState.margin : (nodeState.ports ? nodeState.ports.margin : undefined);
      setLive(el, 'p_top', done ? numOrDash(pTop) : '—');
      setLive(el, 'margin', done ? numOrDash(margin) : '—');
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
    const appObj = app();
    const kit = appObj && appObj.kit;
    const errorEdges = errorPathEdges(replay);
    (graph.edges || []).forEach(edge => {
      const path = kit && kit.edgeEl ? kit.edgeEl(edge.id) : null;
      if (!path) return;
      const edgeState = replay.edgeStates[edge.id];
      const status = edgeState ? edgeState.status : 'unknown';
      EDGE_STATE_CLASSES.forEach(name => path.classList.remove(name));
      path.classList.add('is-' + (status === 'true' ? 'true' : status === 'false' ? 'false'
        : status === 'invalid' ? 'invalid' : 'unknown'));
      path.dataset.status = status;
      if (errorEdges.indexOf(edge.id) >= 0) path.classList.add('is-error-path');
    });
  }

  function apply(replay) {
    state = replay;
    if (!replay || !replay.final) {
      renderResult(null);
      renderHud(null);
      renderDrawer(null);
      return;
    }
    const appObj = app();
    const graph = appObj && appObj.graph;
    if (!graph) return;
    removeReplayClasses();
    (graph.nodes || []).forEach(node => {
      const el = findNodeEl(node.id);
      if (!el) return;
      const nodeState = replay.nodeStates[node.id] || { status: 'not-triggered', ports: null };
      el.classList.add('is-replay');
      if (node.type === 'start') paintStart(el);
      else if (node.type === 'question') paintQuestion(el, node, nodeState);
      else if (node.type === 'logic') paintLogic(el, nodeState);
      else if (node.type === 'condition') paintCondition(el, nodeState);
      else if (node.type === 'target') paintTarget(el, nodeState);
    });
    paintSlots(replay, graph);
    paintEdges(replay, graph);
    renderResult(replay);
    renderHud(replay);
    renderDrawer(replay);
  }

  /* ── 结论 / HUD / 抽屉 DOM ────────────────────────────────────── */
  function totalDuration(replay) {
    return replay.waves.reduce((sum, wave) => sum + (Number(wave.duration_ms) || 0), 0);
  }

  function finalKindLabel(replay) {
    const kind = replay.final.sticky_hit ? 'sticky' : replay.final.kind;
    return t(KIND_KEYS[kind] || KIND_KEYS.target);
  }

  function selectionReason(replay) {
    const reason = replay.final.sticky_hit ? 'sticky' : (replay.final.selection_reason || replay.final.kind);
    const key = SELECTION_KEYS[reason] || SELECTION_KEYS.target;
    return t(key);
  }

  function finalReason(replay) {
    const final = replay.final;
    if (final.kind === 'error_fallback') {
      const failed = replay.waves.find(wave => wave.failed);
      return t('cv.tryErrorPath') + (failed && failed.error ? ' · ' + failed.error : '');
    }
    if (final.kind === 'otherwise') return t('cv.tryFallbackReason');
    if (final.candidates.length > 1) {
      const winner = final.candidates.find(candidate => candidate.target_id === final.target_id) || final.candidates[0];
      const rival = final.candidates.find(candidate => candidate.target_id !== final.target_id);
      return tf('cv.tryPriorityWin', {
        winner: winner.target_id, priority: winner.priority,
        rival: rival.target_id, rivalPriority: rival.priority
      });
    }
    return tf('cv.tryUniqueTarget', { target: final.target_id, priority: final.priority });
  }

  function candidateChips(replay) {
    const final = replay.final;
    if (!final.candidates.length) {
      return '<span class="rtc-try-none">' + esc(t('cv.tryNoCandidates')) + '</span>' +
        '<span class="rtc-try-op">→</span>' +
        '<span class="rtc-try-pick">' + esc(t('cv.tryFallback')) + ' ' + esc(nodeName(final.target_id)) + '</span>';
    }
    return final.candidates.map((candidate, index) =>
      (index ? '<span class="rtc-try-op">&lt;</span>' : '') +
      '<span class="rtc-try-cand' + (candidate.target_id === final.target_id ? ' is-win' : '') + '"' +
      ' data-candidate="' + esc(candidate.target_id) + '" data-priority="' + esc(candidate.priority) + '">' +
      esc(nodeName(candidate.target_id)) + '<i>p' + esc(candidate.priority) + '</i></span>').join('') +
      '<span class="rtc-try-op">→</span><span class="rtc-try-pick">' +
      esc(tf('cv.tryPick', { target: final.target_id })) + '</span>';
  }

  function renderResult(replay) {
    const host = byId('cvTryResult');
    if (!host) return;
    if (!replay || !replay.final) {
      host.hidden = true;
      host.innerHTML = '';
      delete host.dataset.final;
      delete host.dataset.kind;
      delete host.dataset.selection;
      return;
    }
    const final = replay.final;
    host.hidden = false;
    host.dataset.final = final.target_id;
    host.dataset.kind = final.kind;
    host.dataset.selection = final.sticky_hit ? 'sticky' : (final.selection_reason || final.kind);
    const kindClass = final.kind === 'target' && !final.sticky_hit ? 'ok'
      : final.kind === 'otherwise' ? 'cand' : 'fb';
    host.innerHTML =
      '<div class="rtc-try-res-h">' +
        '<span class="rtc-try-kind is-' + kindClass + '">' + esc(finalKindLabel(replay)) + '</span>' +
        '<span class="rtc-try-lbl">' + esc(t('cv.tryFinal')) + '</span>' +
        '<b class="rtc-try-final" title="' + esc(final.target_id) + '">' + esc(nodeName(final.target_id)) + '</b>' +
        '<span class="rtc-try-sub">' + (final.model ? esc(final.model) + ' · ' : '') +
          esc(tf('cv.tryPriorityMeta', { priority: final.priority })) + ' · ' + esc(selectionReason(replay)) + '</span>' +
        '<span class="rtc-try-meta">' +
          esc(tf('cv.tryWaves', { n: replay.waves.length })) + ' · ' +
          esc(tf('cv.tryCalls', { n: replay.waves.length })) + ' · ' +
          esc(ms(totalDuration(replay))) + '</span>' +
        '<span class="rtc-try-acts">' +
          '<button type="button" class="btn sm" id="cvTryDrawerBtn" data-try-act="drawer">' +
            esc(t('cv.tryDrawerBtn')) + '</button>' +
          '<button type="button" class="btn sm" id="cvTryWavesBtn" data-try-act="waves">' +
            esc(t('cv.tryWavesLayout')) + '</button>' +
          '<button type="button" class="btn sm" id="cvTryRerun" data-try-act="rerun">' +
            esc(t('cv.tryRerun')) + '</button>' +
          '<button type="button" class="btn sm" id="cvTryClear" data-try-act="clear">' +
            esc(t('cv.tryClear')) + '</button>' +
        '</span>' +
      '</div>' +
      '<div class="rtc-try-cmp"><span class="rtc-try-cmp-l">' + esc(t('cv.tryCandidates')) + '</span>' +
        candidateChips(replay) + '</div>' +
      '<div class="rtc-try-reason">' + esc(finalReason(replay)) + '</div>';
  }

  function waveSlotRows(replay, wave) {
    return (replay.timeline || []).filter(row =>
      row.kind === 'slot' && row.wave === wave && row.status === 'true');
  }

  function waveConditionTitle(replay, wave) {
    const rows = waveSlotRows(replay, wave);
    if (!rows.length) return t('cv.tryNoHitSlot');
    return rows.map(row => nodeName(row.node_id) + '.' + row.port + ' ' + slotLine(row))
      .join(t('cv.trySepClause'));
  }

  function renderHud(replay) {
    const host = byId('cvReplayHud');
    if (!host) return;
    if (!replay || !replay.final) {
      host.hidden = true;
      host.innerHTML = '';
      return;
    }
    host.hidden = false;
    const waves = replay.waves.map(wave =>
      '<span class="rtc-hud-wave' + (wave.failed ? ' is-failed' : '') + '" data-hud-wave="' + esc(wave.wave) +
      '" title="' + esc(waveConditionTitle(replay, wave.wave)) + '">' +
      esc(tf('cv.tryWave', { n: wave.wave })) + ' · ' +
      esc(tf('cv.tryWaveQuestions', { n: wave.questions.length })) + ' · ' +
      esc(wave.failed ? t('cv.tryWaveFailed') : ms(wave.duration_ms)) + '</span>').join('');
    host.innerHTML = '<span class="rtc-hud-tt">' + esc(t('cv.tryHud')) + '</span>' + waves +
      '<span class="rtc-hud-final' + (replay.final.kind === 'error_fallback' ? ' is-failed' : '') + '">→ ' +
      esc(nodeName(replay.final.target_id)) + '</span>';
  }

  function rowLabel(kind) {
    return t(ROW_LABEL_KEYS[kind] || 'cv.tryRow.slot');
  }

  function rowText(row) {
    switch (row.kind) {
      case 'condition': {
        const matched = (row.matched || []).join(t('cv.trySepList'));
        return matched
          ? tf('cv.tryConditionRow', { matched: matched, value: String(row.value) })
          : t('cv.tryConditionNone') + ' → ' + String(row.value);
      }
      case 'slot':
        /* §6.9 示例是 `in_1 0.84 > 0.65 → true`：节点 id 在中间列，槽位号必须留在行文本里。 */
        return (row.port ? row.port + ' ' : '') + slotLine(row);
      case 'gate': {
        const inputs = (row.inputs || []).map(input =>
          input.port + ' ' + rawText(input.raw) + ' ' +
          (input.condition ? conditionText(input.condition) : '= true') + ' → ' + statusText(input.status))
          .join(t('cv.trySepClause'));
        return tf('cv.tryGateRow', { operator: row.operator, inputs: inputs, value: String(row.value) });
      }
      case 'question': {
        const pTop = row.p_top !== undefined && row.p_top !== null
          ? row.p_top : (row.ports ? row.ports.p_top : undefined);
        const margin = row.margin !== undefined && row.margin !== null
          ? row.margin : (row.ports ? row.ports.margin : undefined);
        if (row.selected !== undefined) {
          return tf('cv.tryQuestionRow', {
            selected: row.selected,
            pTop: numOrDash(pTop), margin: numOrDash(margin)
          });
        }
        if (row.ports && Object.prototype.hasOwnProperty.call(row.ports, 'yes')) {
          return tf('cv.tryNoulRow', { yes: numOrDash(row.ports.yes), no: numOrDash(row.ports.no) });
        }
        return tf('cv.tryScoreRow', { value: numOrDash(row.ports ? row.ports.value : undefined) });
      }
      case 'activate':
        return tf('cv.tryActivateRow', { port: row.via_port });
      case 'fallback':
        return tf('cv.tryFallbackRow', { target: row.target_id, reason: row.reason || '' });
      case 'final':
        return tf('cv.tryFinalRow', {
          kind: t(KIND_KEYS[row.final_kind] || KIND_KEYS.target),
          target: row.target_id, priority: row.priority
        });
      case 'sticky_hit':
        return tf('cv.tryStickyModel', { model: row.model });
      case 'scheduled':
        return tf('cv.tryScheduledRow', { model: row.model, reason: row.reason || '' });
      default:
        return '';
    }
  }

  function drawerGroupLabel(group) {
    if (group === 'request') return t('cv.tryRequest');
    if (group === 'final') return t('cv.trySelectTarget');
    return tf('cv.tryWave', { n: group });
  }

  function rowsHTML(replay) {
    let html = '';
    let group = null;
    (replay.timeline || []).forEach(row => {
      if (!ROW_KINDS[row.kind]) return;
      const rowGroup = (row.kind === 'fallback' || row.kind === 'final' ||
        row.kind === 'scheduled' || row.kind === 'sticky_hit')
        ? 'final' : (row.wave === 0 ? 'request' : row.wave);
      if (rowGroup !== group) {
        group = rowGroup;
        html += '<div class="rtc-dw-group">' + esc(drawerGroupLabel(group)) + '</div>';
      }
      const edgeId = row.kind === 'slot' ? row.edge_id : '';
      const nodeId = row.node_id || row.target_id || '';
      /* 槽位行等价于一个按钮：鼠标、Enter、Space 都触发同一个定位动作（§6.11）。 */
      const keyboard = edgeId ? ' tabindex="0" role="button"' : '';
      html += '<div class="rtc-dw-row rtc-dw-row--' + esc(row.kind) + (edgeId ? ' is-clickable' : '') + '"' +
        (edgeId ? ' data-trace-edge="' + esc(edgeId) + '"' : '') +
        (nodeId ? ' data-trace-node="' + esc(nodeId) + '"' : '') + keyboard + '>' +
        '<span class="rtc-dw-k">' + esc(rowLabel(row.kind)) + '</span>' +
        '<span class="rtc-dw-n" title="' + esc(nodeId) + '">' + esc(nodeName(nodeId)) + '</span>' +
        '<span class="rtc-dw-t">' + esc(rowText(row)) + '</span>' +
        '</div>';
    });
    return html;
  }

  function closeDrawer() {
    const wasOpen = drawerOpen || document.body.classList.contains('has-drawer');
    drawerOpen = false;
    renderDrawer(state);
    return wasOpen;
  }

  function isDrawerOpen() {
    return drawerOpen;
  }

  function renderDrawer(replay) {
    const host = byId('cvTraceDrawer');
    if (!host) return;
    if (!replay || !replay.final || !drawerOpen) {
      host.hidden = true;
      host.innerHTML = '';
      document.body.classList.remove('has-drawer');
      return;
    }
    host.hidden = false;
    document.body.classList.add('has-drawer');
    const waveCards = replay.waves.map(wave => {
      const conds = waveSlotRows(replay, wave.wave);
      return '<div class="rtc-dw-wave' + (wave.failed ? ' is-failed' : '') + '" data-wave-card="' + esc(wave.wave) + '">' +
        '<div class="rtc-dw-wave-h"><b>' + esc(tf('cv.tryWave', { n: wave.wave })) + '</b>' +
          '<span class="rtc-dw-badge' + (wave.failed ? ' is-failed' : '') + '">' +
          esc(wave.failed ? t('cv.tryWaveFailed') : tf('cv.tryWaveQuestions', { n: wave.questions.length })) +
          '</span></div>' +
        '<div class="rtc-dw-wave-m">' + esc(tf('cv.tryWaveQuestions', { n: wave.questions.length })) +
          ' · ' + esc(ms(wave.duration_ms)) + '</div>' +
        (wave.failed && wave.error ? '<div class="rtc-dw-wave-e">' + esc(wave.error) + '</div>' : '') +
        (conds.length
          ? '<div class="rtc-dw-cond-h">' + esc(t('cv.tryWaveConditions')) + '</div>' +
            conds.slice(0, 8).map(row => '<div class="rtc-dw-cond">' +
              esc(nodeName(row.node_id) + '.' + row.port + ' ' + slotLine(row)) + '</div>').join('')
          : '') +
        '</div>';
    }).join('');
    host.innerHTML =
      '<div class="rtc-drawer-h">' +
        '<span class="rtc-dw-tt">' + esc(t('cv.tryDrawer')) + '</span>' +
        '<span class="rtc-dw-sub">' +
          esc(tf('cv.tryDrawerSub', {
            target: replay.final.target_id,
            waves: replay.waves.length,
            calls: replay.waves.length
          })) + '</span>' +
        '<button type="button" class="btn sm rtc-dw-close" data-try-act="drawer-close" aria-label="' +
          esc(t('cv.tryDrawerHide')) + '">✕</button>' +
      '</div>' +
      '<div class="rtc-drawer-body">' +
        '<div class="rtc-drawer-waves">' + waveCards + '</div>' +
        '<div class="rtc-drawer-rows">' + rowsHTML(replay) + '</div>' +
      '</div>';
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
    const appObj = app();
    if (appObj && appObj.kit && typeof appObj.kit.setLocked === 'function') appObj.kit.setLocked(busy);
    if (appObj && typeof appObj.updateToolbar === 'function') appObj.updateToolbar();
    document.body.classList.toggle('is-try-running', !!busy);
  }

  function collapsePanels() {
    const appObj = app();
    if (appObj && typeof appObj.collapsePanels === 'function') {
      appObj.collapsePanels();
      return;
    }
    const palette = byId('cvPalette');
    if (palette) palette.classList.add('is-collapsed');
    const inspector = byId('cvInspector');
    if (inspector) inspector.classList.add('is-collapsed');
  }

  function hideResultPanels() {
    renderResult(null);
    renderHud(null);
    drawerOpen = false;
    renderDrawer(null);
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
    const appObj = app();
    if (appObj) {
      appObj.replay = null;
      /* 运行中 reset（清除/换组）必须同时释放锁，不能把 tryRunning 留给迟到的 finally。 */
      appObj.tryRunning = false;
    }
    clearWaveHeader();
    hideResultPanels();
    removeReplayClasses();
    setBusy(false);
  }

  function clear() {
    reset();
    const appObj = app();
    if (appObj && typeof appObj.render === 'function') appObj.render();
    if (appObj && appObj.toast) appObj.toast(t('cv.tryClearDone'));
    return true;
  }

  function payloadFor(text, now) {
    const appObj = app();
    const payload = {
      root: root.RTGraph.clone(appObj.graph),
      timezone: appObj.tree && appObj.tree.timezone ? appObj.tree.timezone : 'Asia/Shanghai',
      sticky: root.RTGraph.clone((appObj.tree && appObj.tree.sticky) || { enabled: false, ttl_seconds: 1800 }),
      text: text,
      session_id: ''
    };
    if (now) payload.now = now;
    return payload;
  }

  async function run(text, options) {
    const appObj = app();
    if (!appObj || !appObj.tree || !appObj.graph) return false;
    if (!appObj.booted) {
      if (appObj.toast) appObj.toast(t('cv.toastNotReady'), 'warn');
      return false;
    }
    if (appObj.tryRunning) return false;
    const opts = options || {};
    const inputText = String(text == null ? '' : text);
    const now = opts.now === undefined || opts.now === null ? null : String(opts.now);
    lastRun = { text: inputText, now: now };
    const token = ++runSeq;
    state = null;
    appObj.replay = null;
    appObj.tryRunning = true;
    try {
      /* 收起面板/重绘/锁画布都在 try 内：任一步抛错也必须走 finally 释放运行锁。 */
      hideResultPanels();
      collapsePanels();
      /* 运行锁会拒绝所有 App.change()：条件浮层若还开着，确认只会得到
         false。先关掉它，避免用户以为「确认成功」而实际草稿未变。 */
      if (root.RTCondPop && typeof root.RTCondPop.isOpen === 'function' &&
          root.RTCondPop.isOpen()) root.RTCondPop.close();
      removeReplayClasses();
      if (typeof appObj.render === 'function') appObj.render();
      setBusy(true);
      const input = byId('cvTryInput');
      if (input && input.value !== inputText) input.value = inputText;
      /* api.js 的 api 是顶层 const（不是 window 属性），这里按全局词法名取。 */
      const client = typeof api !== 'undefined' ? api : root.api;
      const response = await client.post('/admin/try', payloadFor(inputText, now));
      if (token !== runSeq) return false;
      const rebuilt = rebuild(response.trace || [], appObj.graph);
      rebuilt.outcome = response.outcome || null;
      rebuilt.answers = response.answers || null;
      rebuilt.warnings = Array.isArray(response.warnings) ? response.warnings : [];
      rebuilt.draft = response.draft === true;
      rebuilt.trace = Array.isArray(response.trace) ? response.trace : [];
      rebuilt.text = inputText;
      rebuilt.now = now;
      rebuilt.id = token;
      rebuilt.request = { text: inputText, now: now };
      state = rebuilt;
      appObj.replay = rebuilt;
      apply(rebuilt);
      return true;
    } catch (error) {
      if (token !== runSeq) return false;
      state = null;
      appObj.replay = null;
      try {
        hideResultPanels();
        removeReplayClasses();
        if (typeof appObj.render === 'function') appObj.render();
      } catch (cleanupError) {
        /* 清理失败不能盖住原始错误；finally 仍会释放运行锁。 */
      }
      if (appObj.toast) {
        appObj.toast(tf('cv.tryFailed', {
          err: typeof root.apiErrText === 'function' ? root.apiErrText(error) : String(error)
        }), 'error');
      }
      return false;
    } finally {
      if (token === runSeq) {
        appObj.tryRunning = false;
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
    const appObj = app();
    const replay = state || (appObj && appObj.replay);
    if (!appObj || !appObj.graph) return false;
    if (!replay || !replay.final) {
      if (appObj.toast) appObj.toast(t('cv.tryWavesLayoutNone'), 'warn');
      return false;
    }
    const graph = appObj.graph;
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
      if (typeof appObj.change !== 'function') return false;
      const accepted = appObj.change('layout', tree => {
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
    if (appObj.kit && appObj.kit.fit) appObj.kit.fit({ padding: 24 });
    if (appObj.toast) appObj.toast(t(changedPosition ? 'cv.tryWavesLayoutDone' : 'cv.tryWavesLayoutSame'));
    return true;
  }

  /* ── 试算台绑定 ───────────────────────────────────────────────── */
  function renderChips() {
    const host = byId('cvTryChips');
    if (!host) return;
    const appObj = app();
    const busy = !!(appObj && appObj.tryRunning);
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
    if (name === 'drawer') {
      drawerOpen = true;
      renderDrawer(state);
      return;
    }
    if (name === 'drawer-close') {
      closeDrawer();
      return;
    }
    if (name === 'rerun') { rerun(); return; }
    if (name === 'clear') { clear(); return; }
    if (name === 'waves') { layoutByWaves(); }
  }

  function locateTraceEdge(edgeId) {
    const appObj = app();
    if (!appObj || !appObj.kit || !edgeId) return;
    appObj.kit.focusEdge(edgeId);
    if (root.RTCondPop) root.RTCondPop.open(edgeId);
  }

  function onDrawerClick(event) {
    const close = event.target.closest('[data-try-act="drawer-close"]');
    if (close) {
      closeDrawer();
      return;
    }
    const appObj = app();
    if (!appObj || !appObj.kit) return;
    const row = event.target.closest('[data-trace-edge]');
    if (row) {
      locateTraceEdge(row.dataset.traceEdge);
      return;
    }
    const nodeRow = event.target.closest('[data-trace-node]');
    if (nodeRow) appObj.kit.focusNode(nodeRow.dataset.traceNode);
  }

  function onDrawerKeydown(event) {
    if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
    const row = event.target.closest('[data-trace-edge]');
    if (!row) return;
    event.preventDefault();
    locateTraceEdge(row.dataset.traceEdge);
  }

  function init() {
    if (bound) return;
    bound = true;
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
        const appObj = app();
        if (appObj && appObj.tryRunning) return;
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
    const drawer = byId('cvTraceDrawer');
    if (drawer) {
      drawer.addEventListener('click', onDrawerClick);
      drawer.addEventListener('keydown', onDrawerKeydown);
    }
  }

  function refresh() {
    renderChips();
    renderWaveHeader();
    if (state) apply(state);
    else {
      renderResult(null);
      renderHud(null);
      if (!drawerOpen) renderDrawer(null);
    }
  }

  function samples() {
    return SAMPLES.map((sample, index) => ({
      index: index, label: t(sample.labelKey), text: sample.text, now: sample.now
    }));
  }

  root.RTTryRun = {
    rebuild: rebuild,
    run: run,
    rerun: rerun,
    clear: clear,
    reset: reset,
    apply: apply,
    init: init,
    refresh: refresh,
    samples: samples,
    layoutByWaves: layoutByWaves,
    closeDrawer: closeDrawer,
    isDrawerOpen: isDrawerOpen
  };
})(window);
