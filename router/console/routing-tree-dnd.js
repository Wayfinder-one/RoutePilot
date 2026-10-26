/* Unified routing-tree pointer drag controller (console rewrite, task-13).

   One destroyable controller wraps every pointer gesture the tree needs:

     - reorder: drag a `[data-rt-drag="node"]` handle. The controller only reads
       geometry (sibling midpoints) and reports the *intent*
       `onMove({nodeId, direction, fromIndex, targetIndex})`. Applying it is
       `SRDnd.applyMove(root, info)`, which repeatedly calls
       SRTreeModel.moveNode — the single reorder primitive. The controller never
       splices node arrays and never touches the DOM order itself.
     - signal drop: drag a `[data-sig]` chip (palette) onto an explicit
       `[data-rt-drop="when"|"weights"]` zone. Anything else is a no-op: there
       is NO implicit leaf→fork / auto-regroup / "last station" behaviour.

   Lifecycle: pointerdown/pointermove/pointerup/pointercancel are bound to the
   document at creation and removed by destroy(), so re-renders and language
   switches can call createDndController() again without double binding.
   destroy() is idempotent and also removes any ghost / highlight state.

   Browser-only (DOM); loaded after routing-tree-model.js. */
"use strict";

const SRDnd = (function () {
  const DEFAULTS = {
    handleSelector: '[data-rt-drag="node"]',
    signalSelector: '[data-sig]',
    dropSelector: '[data-rt-drop]',
    sortableSelector: '[data-rt-sortable]',
    threshold: 6,
    zones: {when: true, weights: true},
  };

  function labelOfSignal(signal) {
    try {
      if (typeof sigName === "function") {
        const label = sigName(signal);
        if (label) return label;
      }
    } catch (err) { /* noop */ }
    return String(signal);
  }

  function createDndController(options) {
    const opts = options || {};
    const root = opts.root;
    if (!root || typeof root.addEventListener !== "function") {
      throw new Error("createDndController: root element required");
    }
    const doc = root.ownerDocument || document;
    const handleSelector = opts.handleSelector || DEFAULTS.handleSelector;
    const signalSelector = opts.signalSelector || DEFAULTS.signalSelector;
    const dropSelector = opts.dropSelector || DEFAULTS.dropSelector;
    const sortableSelector = opts.sortableSelector || DEFAULTS.sortableSelector;
    const zones = opts.zones || DEFAULTS.zones;
    const threshold = opts.threshold == null ? DEFAULTS.threshold : opts.threshold;
    const onMove = typeof opts.onMove === "function" ? opts.onMove : function () {};
    const onDropSignal = typeof opts.onDropSignal === "function"
      ? opts.onDropSignal : function () {};

    const state = {
      kind: null, active: false, startX: 0, startY: 0, pointerId: null,
      signal: null, nodeId: null, container: null, item: null, siblings: [],
      fromIndex: -1, targetIndex: null, dropTarget: null, ghost: null, hotEl: null,
    };
    let destroyed = false;

    function closest(node, selector) {
      if (!node || typeof node.closest !== "function") return null;
      return node.closest(selector);
    }
    function clearHot() {
      if (state.hotEl) state.hotEl.classList.remove("is-drop-hot");
      state.hotEl = null;
    }
    function clearIndicators() {
      state.siblings.forEach(el => el.classList.remove("rt-drop-target"));
    }
    function removeGhost() {
      if (state.ghost) {
        state.ghost.remove();
        state.ghost = null;
      }
    }
    function cleanup() {
      clearHot();
      clearIndicators();
      removeGhost();
      if (state.item) state.item.classList.remove("rt-dragging");
      doc.body.classList.remove("rt-dragging");
      state.kind = null;
      state.active = false;
      state.signal = null;
      state.nodeId = null;
      state.container = null;
      state.item = null;
      state.siblings = [];
      state.fromIndex = -1;
      state.targetIndex = null;
      state.dropTarget = null;
      state.startX = 0;
      state.startY = 0;
    }

    function startSignal(event) {
      state.kind = "signal";
      state.signal = event.target.closest(signalSelector).getAttribute("data-sig");
      state.startX = event.clientX;
      state.startY = event.clientY;
      state.pointerId = event.pointerId;
    }

    function startSort(event, handle) {
      const item = closest(handle, sortableSelector);
      if (!item || !root.contains(item)) return;
      const container = item.parentElement;
      if (!container) return;
      const siblings = Array.prototype.filter.call(
        container.children, el => el.matches && el.matches(sortableSelector));
      const fromIndex = siblings.indexOf(item);
      if (fromIndex < 0) return;
      state.kind = "sort";
      state.item = item;
      state.container = container;
      state.siblings = siblings;
      state.fromIndex = fromIndex;
      state.nodeId = handle.getAttribute("data-node-id") || item.getAttribute("data-node-id");
      state.startX = event.clientX;
      state.startY = event.clientY;
      state.pointerId = event.pointerId;
    }

    function onPointerDown(event) {
      if (destroyed) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      if (state.kind) return; // one gesture at a time
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;
      const signalEl = closest(target, signalSelector);
      if (signalEl && signalEl.getAttribute("data-sig")) {
        if (typeof event.preventDefault === "function") event.preventDefault();
        startSignal(event);
        return;
      }
      const handle = closest(target, handleSelector);
      if (handle) startSort(event, handle);
    }

    function activate() {
      state.active = true;
      doc.body.classList.add("rt-dragging");
      if (state.kind === "signal") {
        const ghost = doc.createElement("div");
        ghost.className = "rt-drag-ghost";
        ghost.setAttribute("aria-hidden", "true");
        ghost.textContent = labelOfSignal(state.signal);
        ghost.style.position = "fixed";
        ghost.style.pointerEvents = "none";
        ghost.style.zIndex = "80";
        doc.body.appendChild(ghost);
        state.ghost = ghost;
      } else if (state.item) {
        state.item.classList.add("rt-dragging");
      }
    }

    function moveSignal(event) {
      if (state.ghost) {
        state.ghost.style.left = (event.clientX + 14) + "px";
        state.ghost.style.top = (event.clientY + 10) + "px";
      }
      let target = null;
      if (typeof doc.elementFromPoint === "function") {
        const hit = doc.elementFromPoint(event.clientX, event.clientY);
        const zone = closest(hit, dropSelector);
        if (zone && root.contains(zone)) {
          const zoneName = zone.getAttribute("data-rt-drop");
          if (zones[zoneName]) {
            target = {nodeId: zone.getAttribute("data-node-id") || null, zone: zoneName};
            if (state.hotEl !== zone) {
              clearHot();
              state.hotEl = zone;
              zone.classList.add("is-drop-hot");
            }
          }
        }
      }
      if (!target) {
        clearHot();
        state.dropTarget = null;
        return;
      }
      state.dropTarget = target;
    }

    function moveSort(event) {
      if (!state.container) return;
      const siblings = state.siblings;
      let insertion = 0;
      siblings.forEach(el => {
        const rect = el.getBoundingClientRect();
        if (rect.top + rect.height / 2 < event.clientY) insertion++;
      });
      const target = insertion > state.fromIndex ? insertion - 1 : insertion;
      const clamped = Math.max(0, Math.min(target, siblings.length - 1));
      state.targetIndex = clamped;
      clearIndicators();
      if (clamped !== state.fromIndex && siblings[clamped]) {
        siblings[clamped].classList.add("rt-drop-target");
      }
    }

    function onPointerMove(event) {
      if (destroyed || !state.kind) return;
      if (state.pointerId != null && event.pointerId != null &&
          event.pointerId !== state.pointerId) return;
      if (!state.active) {
        const dx = event.clientX - state.startX;
        const dy = event.clientY - state.startY;
        if (Math.sqrt(dx * dx + dy * dy) < threshold) return;
        activate();
      }
      if (typeof event.preventDefault === "function") event.preventDefault();
      if (state.kind === "signal") moveSignal(event);
      else moveSort(event);
    }

    function finishSort() {
      if (state.targetIndex == null || state.targetIndex === state.fromIndex) return;
      onMove({
        nodeId: state.nodeId,
        direction: state.targetIndex > state.fromIndex ? 1 : -1,
        fromIndex: state.fromIndex,
        targetIndex: state.targetIndex,
      });
    }

    function finishSignal(event) {
      let target = state.dropTarget;
      if (!target && typeof doc.elementFromPoint === "function") {
        const hit = doc.elementFromPoint(event.clientX, event.clientY);
        const zone = closest(hit, dropSelector);
        if (zone && root.contains(zone)) {
          const zoneName = zone.getAttribute("data-rt-drop");
          if (zones[zoneName]) {
            target = {nodeId: zone.getAttribute("data-node-id") || null, zone: zoneName};
          }
        }
      }
      if (target) onDropSignal(state.signal, target);
    }

    function onPointerUp(event) {
      if (destroyed || !state.kind) return;
      if (state.pointerId != null && event.pointerId != null &&
          event.pointerId !== state.pointerId) return;
      const wasActive = state.active;
      const kind = state.kind;
      if (wasActive && kind === "sort") finishSort();
      else if (wasActive && kind === "signal") finishSignal(event);
      cleanup();
    }

    function onPointerCancel() {
      if (destroyed) return;
      cleanup();
    }

    doc.addEventListener("pointerdown", onPointerDown);
    doc.addEventListener("pointermove", onPointerMove, {passive: false});
    doc.addEventListener("pointerup", onPointerUp);
    doc.addEventListener("pointercancel", onPointerCancel);

    return {
      destroy: function () {
        if (destroyed) return;
        destroyed = true;
        cleanup();
        doc.removeEventListener("pointerdown", onPointerDown);
        doc.removeEventListener("pointermove", onPointerMove);
        doc.removeEventListener("pointerup", onPointerUp);
        doc.removeEventListener("pointercancel", onPointerCancel);
      },
    };
  }

  /* Apply one reorder intent: SRTreeModel.moveNode in a loop until the node
     reaches targetIndex (moveNode only steps by one, so dragging three slots
     is three calls of the same primitive). Returns a NEW root; input is never
     mutated. Unsortable edges (root/otherwise/missing) are a no-op. */
  function applyMove(root, info) {
    if (!root || !info || typeof info.nodeId !== "string") {
      throw new Error("SRDnd.applyMove: root and nodeId required");
    }
    if (typeof SRTreeModel === "undefined" || !SRTreeModel ||
        typeof SRTreeModel.findNodeRef !== "function" ||
        typeof SRTreeModel.moveNode !== "function") {
      throw new Error("SRDnd.applyMove: SRTreeModel not loaded");
    }
    let current = root;
    for (let guard = 0; guard < 4096; guard++) {
      const ref = SRTreeModel.findNodeRef(current, info.nodeId);
      if (!ref || ref.edgeKind === "root" || ref.edgeKind === "otherwise") return current;
      const list = ref.edgeKind === "children" ? ref.parent.children : ref.parent.options;
      const max = list.length - 1;
      const rawTarget = Number(info.targetIndex);
      const target = Number.isFinite(rawTarget)
        ? Math.max(0, Math.min(rawTarget, max)) : ref.index;
      if (ref.index === target) return current;
      const direction = target > ref.index ? 1 : -1;
      const next = SRTreeModel.moveNode(current, info.nodeId, direction);
      if (!next || next === current) return current;
      current = next;
    }
    return current;
  }

  return {createDndController: createDndController, applyMove: applyMove};
})();
