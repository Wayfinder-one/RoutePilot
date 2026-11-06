/* RTCanvas · 画布内核（规格 §6.4；移植自原型 canvas-core.js 并产品化）
   ------------------------------------------------------------------
   职责：视口平移缩放 / 节点拖拽 / 输出端口拖线（类型 + 合法性校验）/ SVG 贝塞尔边 /
        选中 / 聚焦定位 / 安全区 fit。
   不负责：节点卡片、检查器、试算回放、节点库 —— 由 cards.js / app.js 渲染。
   历史不在这里：undoStack/redoStack 归 DraftStore，画布只把编辑交给 store.change。

   DOM 契约（cards.js 必须遵守）：
     node: <div class="rtc-node" data-node-id="capability" data-node-type="question">…</div>
     port: <span class="rtc-port" data-port data-node="capability" data-port-id="prob.strong"
                 data-port-dir="out" data-port-type="number"></span>
     slot: <div class="rtc-slot" data-slot="in_1">…port…</div>
     端口必须带 data-port / data-node / data-port-id / data-port-dir / data-port-type；
     槽位行带 data-slot（输入端口也带一份，便于只按 data-slot 命中）。
     节点绝对定位由本模块写 left/top（.rtc-node{position:absolute}）。

   用法：
     const kit = create({ stage, world, nodesLayer, svg, store, callbacks });
     kit.init();
     // 页面渲染 cards 后
     kit.sync(); kit.fit();
   ------------------------------------------------------------------ */
import * as RTGraph from './graph.js';
import { t } from '../i18n.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const MIN_K = 0.3;
const MAX_K = 2;
const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const noop = () => {};
const clone = value => (value == null ? value : JSON.parse(JSON.stringify(value)));
  /* Space 是按钮/链接等控件的原生激活键：焦点在控件上时不能把它劫持成平移。 */
  const INTERACTIVE_SELECTOR = 'input, textarea, select, button, a, [role="button"], [contenteditable]';
  function isInteractiveElement(el) {
    return !!(el && el.closest && el.closest(INTERACTIVE_SELECTOR));
  }

function create(opts) {
  const stage = opts.stage;
  const world = opts.world;
  const nodesLayer = opts.nodesLayer;
  const svg = opts.svg;
  const store = opts.store;
  const callbacks = opts.callbacks || {};

  const cb = {
    onSelect: callbacks.onSelect || noop,
    onInvalid: callbacks.onInvalid || noop,
    onQuickCreate: callbacks.onQuickCreate || (() => false),
    onViewport: callbacks.onViewport || noop,
    onEdgeCreated: callbacks.onEdgeCreated || noop,
    onDeleteRequest: callbacks.onDeleteRequest || noop,
    onEscape: callbacks.onEscape || (() => false),
    onClearReplay: callbacks.onClearReplay || noop
  };

    /* 当前 root graph；App 会在恢复历史/重载时整体替换，所以必须是可变引用。 */
    let graph = opts.graph || { id: 'root', type: 'graph', nodes: [], edges: [] };

    const vp = { k: 1, tx: 0, ty: 0 };
    let sel = { kind: null, id: null };
    let drag = null;
    let tempPath = null;
    let spaceDown = false;
    /* 试算在途时锁编辑：仍允许平移/缩放/选中，禁止拖节点、拖线和删除。 */
    let locked = false;
    let edgeEls = Object.create(null);
    let portEls = [];

    function stageRect() { return stage.getBoundingClientRect(); }

    function screenToWorld(clientX, clientY) {
      const rect = stageRect();
      return {
        x: (clientX - rect.left - vp.tx) / vp.k,
        y: (clientY - rect.top - vp.ty) / vp.k
      };
    }

    /* ── 节点 / 端口元素 ─────────────────────────────────────── */
    function nodeEl(id) {
      const all = nodesLayer.querySelectorAll('[data-node-id]');
      for (let index = 0; index < all.length; index += 1) {
        if (all[index].dataset.nodeId === id) return all[index];
      }
      return null;
    }

    function portEl(nodeId, portId) {
      for (let index = 0; index < portEls.length; index += 1) {
        const el = portEls[index];
        if (el.dataset.node === nodeId && el.dataset.portId === portId) return el;
      }
      return null;
    }

    function portAnchor(nodeId, portId) {
      const el = portEl(nodeId, portId);
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      const box = stageRect();
      return {
        x: (rect.left + rect.width / 2 - box.left - vp.tx) / vp.k,
        y: (rect.top + rect.height / 2 - box.top - vp.ty) / vp.k
      };
    }

    function reindexPorts() {
      portEls = Array.prototype.slice.call(nodesLayer.querySelectorAll('[data-port]'));
    }

    /* ── 视口 ─────────────────────────────────────────────────── */
    function applyViewport() {
      world.style.transform = 'translate(' + vp.tx + 'px,' + vp.ty + 'px) scale(' + vp.k + ')';
      cb.onViewport({ k: vp.k, tx: vp.tx, ty: vp.ty });
    }

    function zoomTo(k, anchor) {
      const next = clamp(k, MIN_K, MAX_K);
      const rect = stageRect();
      const point = anchor || { x: rect.width / 2, y: rect.height / 2 };
      const before = { x: (point.x - vp.tx) / vp.k, y: (point.y - vp.ty) / vp.k };
      vp.k = next;
      vp.tx = point.x - before.x * vp.k;
      vp.ty = point.y - before.y * vp.k;
      applyViewport();
    }

    function zoomBy(factor) {
      const rect = stageRect();
      zoomTo(vp.k * factor, { x: rect.width / 2, y: rect.height / 2 });
    }

    function setZoom(k) {
      const rect = stageRect();
      zoomTo(k, { x: rect.width / 2, y: rect.height / 2 });
    }

    function getViewport() { return { k: vp.k, tx: vp.tx, ty: vp.ty }; }

    /* 安全区：避开左上节点库、右上检查器、底部缩放条 / 小地图 / 试算台。 */
    function overlayRect(el) {
      if (!el || el.hidden || !el.getClientRects().length) return null;
      const box = stageRect();
      const rect = el.getBoundingClientRect();
      return {
        left: rect.left - box.left,
        top: rect.top - box.top,
        right: rect.right - box.left,
        bottom: rect.bottom - box.top
      };
    }

    function safeArea(pad) {
      const rect = stageRect();
      const gap = pad == null ? 14 : pad;
      let left = gap;
      let top = gap;
      let right = rect.width - gap;
      let bottom = rect.height - gap;
      const palette = overlayRect(document.getElementById('cvPalette'));
      if (palette) left = Math.max(left, palette.right + 14);
      const inspector = overlayRect(document.getElementById('cvInspector'));
      if (inspector) right = Math.min(right, inspector.left - 14);
      ['cvZoom', 'cvTryBar', 'cvMinimap'].forEach(id => {
        const bar = overlayRect(document.getElementById(id));
        if (bar) bottom = Math.min(bottom, bar.top - 14);
      });
      if (right - left < 220) { left = gap; right = rect.width - gap; }
      if (bottom - top < 180) { top = gap; bottom = rect.height - gap; }
      return { x: left, y: top, w: right - left, h: bottom - top };
    }

    function fit(paddingOrOptions) {
      const options = typeof paddingOrOptions === 'number'
        ? { padding: paddingOrOptions }
        : (paddingOrOptions || {});
      const pad = options.padding == null ? 20 : options.padding;
      const maxScale = options.maxScale == null ? 1.25 : options.maxScale;
      if (!graph.nodes.length) return;
      const area = safeArea(12);
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      graph.nodes.forEach(node => {
        const el = nodeEl(node.id);
        const width = el ? el.offsetWidth : 200;
        const height = el ? el.offsetHeight : 100;
        minX = Math.min(minX, node.position.x);
        minY = Math.min(minY, node.position.y);
        maxX = Math.max(maxX, node.position.x + width);
        maxY = Math.max(maxY, node.position.y + height);
      });
      const width = Math.max(1, maxX - minX);
      const height = Math.max(1, maxY - minY);
      const k = clamp(Math.min((area.w - pad * 2) / width, (area.h - pad * 2) / height), MIN_K, maxScale);
      vp.k = k;
      vp.tx = area.x + area.w / 2 - (minX + width / 2) * k;
      vp.ty = area.y + area.h / 2 - (minY + height / 2) * k;
      applyViewport();
    }

    function focusNode(id, options) {
      const node = graph.nodes.find(item => item.id === id);
      if (!node) return;
      const el = nodeEl(id);
      const rect = stageRect();
      const k = clamp((options && options.scale) || Math.max(vp.k, 0.85), MIN_K, MAX_K);
      vp.k = k;
      vp.tx = rect.width / 2 - (node.position.x + (el ? el.offsetWidth : 200) / 2) * k;
      vp.ty = rect.height / 2 - (node.position.y + (el ? el.offsetHeight : 100) / 2) * k;
      applyViewport();
      select('node', id);
    }

    function focusEdge(id) {
      const edge = graph.edges.find(item => item.id === id);
      if (!edge) return;
      const a = portAnchor(edge.source, edge.source_port);
      const b = portAnchor(edge.target, edge.target_port);
      if (!a || !b) return;
      const rect = stageRect();
      vp.tx = rect.width / 2 - ((a.x + b.x) / 2) * vp.k;
      vp.ty = rect.height / 2 - ((a.y + b.y) / 2) * vp.k;
      applyViewport();
      select('edge', id);
    }

    /* ── 边渲染 ───────────────────────────────────────────────── */
    function bezierPath(a, b) {
      const curve = Math.max(34, Math.abs(b.x - a.x) * 0.45);
      return 'M' + a.x + ',' + a.y + ' C' + (a.x + curve) + ',' + a.y +
        ' ' + (b.x - curve) + ',' + b.y + ' ' + b.x + ',' + b.y;
    }

    function renderEdges() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      edgeEls = Object.create(null);
      graph.edges.forEach(edge => {
        const a = portAnchor(edge.source, edge.source_port);
        const b = portAnchor(edge.target, edge.target_port);
        if (!a || !b) return;
        const d = bezierPath(a, b);
        const kind = RTGraph.edgeType(edge, graph);

        const hit = document.createElementNS(SVG_NS, 'path');
        hit.setAttribute('d', d);
        hit.setAttribute('class', 'rtc-edge-hit');
        hit.setAttribute('data-edge-id', edge.id);
        hit.dataset.edgeKind = kind;
        svg.appendChild(hit);

        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', d);
        path.setAttribute('data-edge-id', edge.id);
        path.dataset.edgeKind = kind;
        path.setAttribute('class', 'rtc-edge' +
          (sel.kind === 'edge' && sel.id === edge.id ? ' is-selected' : ''));
        svg.appendChild(path);
        edgeEls[edge.id] = { path: path, hit: hit };
      });
      if (tempPath) svg.appendChild(tempPath);
    }

    function applySelectionClasses() {
      Array.prototype.forEach.call(nodesLayer.querySelectorAll('[data-node-id]'), el => {
        el.classList.toggle('is-selected', sel.kind === 'node' && el.dataset.nodeId === sel.id);
      });
      Object.keys(edgeEls).forEach(id => {
        edgeEls[id].path.classList.toggle('is-selected', sel.kind === 'edge' && sel.id === id);
      });
    }

    function selectionExists() {
      if (sel.kind === 'node') return !!graph.nodes.find(node => node.id === sel.id);
      if (sel.kind === 'edge') return !!graph.edges.find(edge => edge.id === sel.id);
      return false;
    }

    function sync() {
      graph.nodes.forEach(node => {
        const el = nodeEl(node.id);
        if (!el) return;
        el.style.left = Math.round(node.position.x) + 'px';
        el.style.top = Math.round(node.position.y) + 'px';
      });
      reindexPorts();
      if (sel.kind && !selectionExists()) sel = { kind: null, id: null };
      renderEdges();
      applySelectionClasses();
    }

    function attachGraph(next) {
      if (next && next !== graph) graph = next;
    }

    /* 历史由 DraftStore 持有：setGraph 只换引用并清选中。 */
    function setGraph(next) {
      graph = next || { id: 'root', type: 'graph', nodes: [], edges: [] };
      sel = { kind: null, id: null };
      sync();
    }

    /* ── 选中 / 历史 ──────────────────────────────────────────── */
    function select(kind, id, silent) {
      sel = { kind: kind, id: id };
      applySelectionClasses();
      if (!silent) cb.onSelect({ kind: kind, id: id });
    }

    function clearSelection() { select(null, null); }

    function getSelection() { return { kind: sel.kind, id: sel.id }; }

    function clearReplay() {
      if (svg) {
        svg.classList.remove('is-replay');
        Array.prototype.forEach.call(svg.querySelectorAll('.is-true,.is-false,.is-unknown,.is-num'), el => {
          el.classList.remove('is-true', 'is-false', 'is-unknown', 'is-num');
        });
      }
      cb.onClearReplay();
    }

    /* 拖拽取消（pointercancel / 试算锁）：恢复拖拽前坐标与卡片、边，不产生历史或 dirty。 */
    function rollbackNodeDrag(activeDrag) {
      if (!activeDrag || activeDrag.type !== 'node') return;
      const node = graph.nodes.find(item => item.id === activeDrag.nodeId);
      if (!node) return;
      node.position.x = activeDrag.originX;
      node.position.y = activeDrag.originY;
      activeDrag.el.style.left = Math.round(activeDrag.originX) + 'px';
      activeDrag.el.style.top = Math.round(activeDrag.originY) + 'px';
      renderEdges();
    }

    /* 空槽位只在连线拖拽期间作为放置目标出现（静止时不展示，见 cards.js）。 */
    function setEdgeDragVisual(active) {
      stage.classList.toggle('is-edge-drag', !!active);
    }

    function setLocked(value) {
      locked = !!value;
      stage.classList.toggle('is-locked', locked);
      if (locked) {
        if (drag && drag.type === 'edge') {
          if (tempPath && tempPath.parentNode) tempPath.parentNode.removeChild(tempPath);
          tempPath = null;
          clearCompatible();
        }
        setEdgeDragVisual(false);
        rollbackNodeDrag(drag);
        drag = null;
      }
    }

    /* ── 连线 ─────────────────────────────────────────────────── */
    function addEdge(draft) {
      if (locked) return null;
      const check = RTGraph.canConnect(graph, draft);
      if (!check.ok) { cb.onInvalid(check.reason, draft); return null; }
      let created = null;
      const committed = store.change('behavior', tree => {
        const result = RTGraph.addEdge(tree.root, draft);
        if (result.ok) created = result.edge;
      });
      if (!committed || !created) return null;
      sync();
      cb.onEdgeCreated(created);
      return created;
    }

    /* 供后续任务使用的无历史包装（App.change 内部调用即可）。 */
    function removeNode(id) {
      if (locked) return { ok: false, reason: t('cv.tryEditBlocked') };
      let result = { ok: false };
      if (!store.change('behavior', tree => { result = RTGraph.removeNode(tree.root, id); })) {
        return { ok: false, reason: t('cv.tryEditBlocked') };
      }
      if (!result.ok) return result;
      sel = { kind: null, id: null };
      sync();
      return result;
    }

    function removeEdge(id) {
      if (locked) return { ok: false, reason: t('cv.tryEditBlocked') };
      let result = { ok: false };
      if (!store.change('behavior', tree => { result = RTGraph.removeEdge(tree.root, id); })) {
        return { ok: false, reason: t('cv.tryEditBlocked') };
      }
      if (!result.ok) return result;
      sel = { kind: null, id: null };
      sync();
      return result;
    }

    /* ── 指针交互 ─────────────────────────────────────────────── */
    function isInteractive(target) {
      return !!target.closest('input,button,select,textarea,a,[contenteditable="true"],[data-no-drag]');
    }

    function typingInField(event) {
      const el = event.target;
      if (!el) return false;
      const tag = el.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable === true;
    }

    function startPanDrag(event, keepSelection) {
      drag = {
        type: 'pan',
        startX: event.clientX,
        startY: event.clientY,
        tx: vp.tx,
        ty: vp.ty,
        keepSelection: !!keepSelection
      };
      drag.moved = false;
      stage.setPointerCapture(event.pointerId);
    }

    function onPointerDown(event) {
      if (event.button === 2) return;
      const target = event.target;
      /* 点击画布本体把焦点从旧控件移走：否则后续 Delete/Backspace 会落在
         旧的按钮/链接上（交互元素守卫会正确地忽略它，但用户想删的是画布）。 */
      if (!isInteractive(target) && !target.closest('.rtc-overlay')) {
        const active = document.activeElement;
        if (active && active !== document.body && typeof active.blur === 'function') active.blur();
      }
      if (event.button === 1 || spaceDown) {
        event.preventDefault();
        startPanDrag(event, true);
        return;
      }
      const port = target.closest('[data-port]');
      const ownerEl = target.closest('[data-node-id]');
      if (locked) {
        /* 只读：输出端口不启动连线，节点只选中不拖动；空白/边仍可平移与选中。 */
        if (port && port.dataset.portDir === 'out') return;
        if (ownerEl && !isInteractive(target) && !(port && port.dataset.portDir === 'in')) {
          select('node', ownerEl.dataset.nodeId, true);
          return;
        }
      }
      if (port && port.dataset.portDir === 'out') {
        event.preventDefault();
        startEdgeDrag(event, port);
        return;
      }
      const owner = target.closest('[data-node-id]');
      if (owner && !isInteractive(target) && !(port && port.dataset.portDir === 'in')) {
        event.preventDefault();
        select('node', owner.dataset.nodeId, true);
        startNodeDrag(event, owner.dataset.nodeId, owner);
        return;
      }
      if (owner && isInteractive(target)) {
        /* 卡片里的按钮/输入控件自己处理交互，不要落成空白平移。 */
        return;
      }
      if (port) {
        if (owner) select('node', owner.dataset.nodeId);
        return;
      }
      const edgeHit = target.closest('.rtc-edge-hit');
      if (edgeHit) {
        event.preventDefault();
        select('edge', edgeHit.dataset.edgeId);
        return;
      }
      event.preventDefault();
      startPanDrag(event, false);
    }

    function startNodeDrag(event, nodeId, el) {
      const node = graph.nodes.find(item => item.id === nodeId);
      if (!node || !el) return;
      drag = {
        type: 'node',
        nodeId: nodeId,
        el: el,
        startX: event.clientX,
        startY: event.clientY,
        originX: node.position.x,
        originY: node.position.y,
        moved: false
      };
      stage.setPointerCapture(event.pointerId);
    }

    function startEdgeDrag(event, port) {
      const draft = { source: port.dataset.node, source_port: port.dataset.portId };
      drag = {
        type: 'edge',
        draft: draft,
        startX: event.clientX,
        startY: event.clientY,
        moved: false
      };
      tempPath = document.createElementNS(SVG_NS, 'path');
      tempPath.setAttribute('class', 'rtc-edge-temp');
      svg.appendChild(tempPath);
      setEdgeDragVisual(true);
      highlightCompatible(draft);
      stage.setPointerCapture(event.pointerId);
    }

    function highlightCompatible(draft) {
      portEls.forEach(el => {
        if (el.dataset.portDir !== 'in') return;
        const candidate = {
          source: draft.source,
          source_port: draft.source_port,
          target: el.dataset.node,
          target_port: el.dataset.portId
        };
        const ok = RTGraph.canConnect(graph, candidate).ok;
        el.classList.toggle('is-compatible', ok);
        el.classList.toggle('is-incompatible', !ok);
        const slot = el.parentElement && el.parentElement.closest('[data-slot]');
        if (slot) {
          slot.classList.toggle('is-compatible', ok);
          slot.classList.toggle('is-incompatible', !ok);
        }
      });
    }

    function clearCompatible() {
      portEls.forEach(el => {
        el.classList.remove('is-compatible', 'is-incompatible');
        const slot = el.parentElement && el.parentElement.closest('[data-slot]');
        if (slot) slot.classList.remove('is-compatible', 'is-incompatible');
      });
    }

    function updateTempPath(event) {
      if (!drag || drag.type !== 'edge' || !tempPath) return;
      const a = portAnchor(drag.draft.source, drag.draft.source_port);
      const b = screenToWorld(event.clientX, event.clientY);
      if (a) tempPath.setAttribute('d', bezierPath(a, b));
    }

    function onPointerMove(event) {
      if (!drag) return;
      if (drag.type === 'pan') {
        const dx = event.clientX - drag.startX;
        const dy = event.clientY - drag.startY;
        if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
        vp.tx = drag.tx + dx;
        vp.ty = drag.ty + dy;
        applyViewport();
        return;
      }
      if (drag.type === 'node') {
        const dx = (event.clientX - drag.startX) / vp.k;
        const dy = (event.clientY - drag.startY) / vp.k;
        if (!drag.moved && Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 2) {
          drag.moved = true;
        }
        const node = graph.nodes.find(item => item.id === drag.nodeId);
        if (node) {
          node.position.x = drag.originX + dx;
          node.position.y = drag.originY + dy;
          drag.el.style.left = Math.round(node.position.x) + 'px';
          drag.el.style.top = Math.round(node.position.y) + 'px';
          renderEdges();
        }
        return;
      }
      if (drag.type === 'edge') {
        drag.moved = true;
        updateTempPath(event);
      }
    }

    function findInputPort(el) {
      const port = el.closest('[data-port][data-port-dir="in"]');
      if (port) return port;
      const slot = el.closest('[data-slot]');
      if (slot) return slot.querySelector('[data-port][data-port-dir="in"]');
      return null;
    }

    function insideStage(clientX, clientY) {
      const rect = stageRect();
      return clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom;
    }

    function overOverlay(el) {
      return !!(el && el.closest && el.closest('.rtc-overlay'));
    }

    function finishEdgeDrag(event, finished) {
      const target = document.elementFromPoint(event.clientX, event.clientY);
      const inPort = target ? findInputPort(target) : null;
      const stagePoint = screenToWorld(event.clientX, event.clientY);
      if (inPort) {
        const draft = {
          source: finished.draft.source,
          source_port: finished.draft.source_port,
          target: inPort.dataset.node,
          target_port: inPort.dataset.portId
        };
        const check = RTGraph.canConnect(graph, draft);
        if (check.ok) addEdge(draft);
        else cb.onInvalid(check.reason, draft);
        return;
      }
      /* 落在已有节点（含它自己的端口）上：静默取消，不弹快速创建。 */
      if (target && target.closest('[data-node-id]')) return;
      /* 落回节点库 / 检查器 / 缩放条等浮层：静默取消。 */
      if (!insideStage(event.clientX, event.clientY) || overOverlay(target)) return;
      const handled = cb.onQuickCreate({
        sourceNodeId: finished.draft.source,
        sourcePort: finished.draft.source_port,
        x: stagePoint.x,
        y: stagePoint.y,
        clientX: event.clientX,
        clientY: event.clientY
      });
      if (!handled) cb.onInvalid(t('cv.cancelConnect'), finished.draft);
    }

    function onPointerUp(event) {
      if (!drag) return;
      const finished = drag;
      drag = null;
      try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* ignore */ }

      if (finished.type === 'pan') {
        if (!finished.moved && !finished.keepSelection) select(null, null);
        return;
      }
      if (finished.type === 'node') {
        if (finished.moved) {
          /* 拖拽期间为了实时反馈直接改了 graph；落点确认时先回滚到起点，
             再走 store.change 记录一次 layout 历史（克隆 before/after）。 */
          const node = graph.nodes.find(item => item.id === finished.nodeId);
          if (node) {
            const nextX = node.position.x;
            const nextY = node.position.y;
            node.position.x = finished.originX;
            node.position.y = finished.originY;
            const committed = store.change('layout', tree => {
              const target = (tree.root.nodes || []).find(item => item && item.id === finished.nodeId);
              if (target) target.position = { x: nextX, y: nextY };
            });
            if (!committed) sync();
          }
        }
        cb.onSelect(getSelection());
        return;
      }
      if (finished.type === 'edge') {
        finishEdgeDrag(event, finished);
        if (tempPath && tempPath.parentNode) tempPath.parentNode.removeChild(tempPath);
        tempPath = null;
        clearCompatible();
        setEdgeDragVisual(false);
      }
    }

    function onPointerCancel(event) {
      if (!drag) return;
      if (drag.type === 'edge') {
        if (tempPath && tempPath.parentNode) tempPath.parentNode.removeChild(tempPath);
        tempPath = null;
        clearCompatible();
        setEdgeDragVisual(false);
      }
      rollbackNodeDrag(drag);
      drag = null;
      try { stage.releasePointerCapture(event.pointerId); } catch (error) { /* ignore */ }
    }

    function onWheel(event) {
      if (event.target.closest && event.target.closest('[data-scrollable]')) return;
      event.preventDefault();
      const rect = stageRect();
      const anchor = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      const factor = Math.exp(-event.deltaY * 0.0014);
      zoomTo(vp.k * factor, anchor);
    }

    function onKeyDown(event) {
      const interactive = isInteractiveElement(event.target) || isInteractiveElement(document.activeElement);
      if (event.code === 'Space' && !interactive) {
        if (!spaceDown) {
          spaceDown = true;
          stage.classList.add('is-space-pan');
        }
        event.preventDefault();
        return;
      }
      if (event.key === 'Escape') {
        if (cb.onEscape()) return;
        if (drag && drag.type === 'edge') {
          if (tempPath && tempPath.parentNode) tempPath.parentNode.removeChild(tempPath);
          tempPath = null;
          drag = null;
          clearCompatible();
          return;
        }
        if (sel.kind) select(null, null);
        return;
      }
      if (typingInField(event)) return;
      if ((event.metaKey || event.ctrlKey) && String(event.key).toLowerCase() === 'z') {
        if (locked) return;
        event.preventDefault();
        if (event.shiftKey) store.redo(); else store.undo();
        return;
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (locked || !sel.kind) return;
        /* 焦点在按钮/链接等交互元素上时，Delete/Backspace 是控件自己的键，
           不能在背后删掉当前选中的节点或边（a11y §6.11）。 */
        if (isInteractive(event.target) || isInteractiveElement(document.activeElement)) return;
        event.preventDefault();
        cb.onDeleteRequest({ kind: sel.kind, id: sel.id });
      }
    }

    function onKeyUp(event) {
      if (event.code !== 'Space') return;
      /* 焦点在控件上、且当前没有画布平移时，keyup 也要留给控件自己。 */
      if (!spaceDown && isInteractiveElement(event.target)) return;
      spaceDown = false;
      stage.classList.remove('is-space-pan');
    }

    function onWindowBlur() {
      spaceDown = false;
      stage.classList.remove('is-space-pan');
    }

    /* ── 初始化 / 销毁 ────────────────────────────────────────── */
    function init() {
      svg.setAttribute('width', '8000');
      svg.setAttribute('height', '5000');
      svg.setAttribute('viewBox', '0 0 8000 5000');
      svg.style.position = 'absolute';
      svg.style.left = '0';
      svg.style.top = '0';
      svg.style.overflow = 'visible';
      svg.style.pointerEvents = 'none';
      applyViewport();
      stage.addEventListener('pointerdown', onPointerDown);
      stage.addEventListener('pointermove', onPointerMove);
      stage.addEventListener('pointerup', onPointerUp);
      stage.addEventListener('pointercancel', onPointerCancel);
      stage.addEventListener('wheel', onWheel, { passive: false });
      document.addEventListener('keydown', onKeyDown);
      document.addEventListener('keyup', onKeyUp);
      window.addEventListener('blur', onWindowBlur);
    }

    function destroy() {
      stage.removeEventListener('pointerdown', onPointerDown);
      stage.removeEventListener('pointermove', onPointerMove);
      stage.removeEventListener('pointerup', onPointerUp);
      stage.removeEventListener('pointercancel', onPointerCancel);
      stage.removeEventListener('wheel', onWheel);
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', onWindowBlur);
    }

    return {
      init: init,
      destroy: destroy,
      sync: sync,
      renderEdges: renderEdges,
      attachGraph: attachGraph,
      setGraph: setGraph,
      getGraph: function () { return graph; },
      select: select,
      clearSelection: clearSelection,
      getSelection: getSelection,
      addEdge: addEdge,
      removeNode: removeNode,
      removeEdge: removeEdge,
      fit: fit,
      zoomBy: zoomBy,
      setZoom: setZoom,
      focusNode: focusNode,
      focusEdge: focusEdge,
      getViewport: getViewport,
      screenToWorld: screenToWorld,
      clearReplay: clearReplay,
      setLocked: setLocked,
      isLocked: () => locked,
      nodeEl: nodeEl,
      portEl: portEl,
      edgeEl: id => (edgeEls[id] ? edgeEls[id].path : null),
      safeArea: safeArea
    };
  }

export { create, MIN_K, MAX_K };
