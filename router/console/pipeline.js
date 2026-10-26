/* Unified routing-tree console — composition root (2026-10-26 rewrite).

   This file wires the modules into the page and owns *only* page concerns:
   boot data, tree list, editor popovers, import/export, try drawer, keyboard
   shortcuts and the DND controller lifecycle.

   Node semantics live in exactly one place each:
     routing-tree-model.js   pure tree data / full out-edge model / mutations
     routing-tree-store.js   per-tree draft+saved+dirty+revision state
     routing-tree-view.js    read-only DOM renderer
     routing-tree-editor.js  per-type forms + pure apply helpers
     routing-tree-dnd.js     destroyable pointer drag controller

   No second node interpreter here: this file never walks `children`/`options`
   itself, it asks the model/view/editor.

   Backend contract (Task 9, frozen): GET/POST /admin/trees,
   GET/PUT /admin/trees/{id}, POST /admin/trees/{id}/copy|enable|disable,
   POST /admin/try {tree_id,text,headers,n_messages}. Saves never send
   `enabled`; activation always re-reads the server list. */
"use strict";

const $ = (selector, scope = document) => (scope || document).querySelector(selector);
const $$ = (selector, scope = document) => Array.from((scope || document).querySelectorAll(selector));
const nid = prefix => (prefix || "n") + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

const NODE_TYPES = ["branch", "nl_choice", "route", "block"];
const DEFAULT_CONF_MIN = 0.7;      // model-signal when floor (engine default, mirrors the tree editor default)
const DEFAULT_WEIGHT = 0.5;
const REV_CHARS = 8;               // short revision display

/* ── UI-only state (per-tree state lives in SRTreeStore) ─────────────── */
let SIGNALS = [];
let GROUPS = [];
let KEYWORD_GROUPS = [];
let TIME_PERIODS = [];
let dnd = null;
let pendingSignal = null;          // palette click -> explicit drop zone
let popContext = null;             // {kind, nodeId, anchor} of the open popover
let tryBusy = false;
let lastDropAt = 0;

const shortRev = rev => (rev ? String(rev).slice(0, REV_CHARS) : "—");
const modelsById = () => SRTreeStore.state.modelsById;
const currentTreeId = () => SRTreeStore.selectedTreeId();
const currentTree = () => SRTreeStore.selectedTree();
const currentDraft = () => SRTreeStore.draft(currentTreeId());

/* ── i18n / issue helpers ────────────────────────────────────────────── */
const tr = (key, vars) => (vars ? tf(key, vars) : t(key));
const groupName = group => t('group.' + group);
function issueText(item) {
  if (item == null) return "";
  if (typeof item === "string") return item;
  const zh = item.zh || item.code || "";
  const en = item.en || item.zh || item.code || "";
  return LANG === "zh" ? zh : en;
}
function errText(err) {
  const detail = err && err.detail;
  if (Array.isArray(detail)) return detail.map(issueText).filter(Boolean).join("; ");
  if (detail) return issueText(detail) || String(detail);
  return (err && err.message != null) ? String(err.message) : String(err);
}
function issueList(err) {
  return err && Array.isArray(err.detail) ? err.detail : null;
}
function localIssue(code, message) {
  return {code: code, zh: message, en: message, level: "hard", node_id: ""};
}

/* ── chrome: issues / warnings / dirty / revisions ───────────────────── */
function renderIssues(list) {
  const box = $("#issues");
  if (!box) return;
  const items = Array.isArray(list) ? list : [];
  box.innerHTML = items.map(item => {
    const nodeId = item && item.node_id ? item.node_id : "";
    const locate = nodeId
      ? `<button type="button" class="iv-locate" data-issue-node="${esc(nodeId)}">${esc(nodeId)}</button>`
      : "";
    const level = item && item.level === "warn" ? "warn" : "hard";
    return `<div class="iv ${level}">• ${esc(issueText(item))}${locate}</div>`;
  }).join("");
}
function renderWarnings(list) {
  const box = $("#warnings");
  if (!box) return;
  const items = Array.isArray(list) ? list : [];
  box.innerHTML = items.map(item => `<div class="iv warn">• ${esc(issueText(item))}</div>`).join("");
  box.classList.toggle("show", items.length > 0);
}
function renderChrome() {
  const tree = currentTree();
  const dirty = tree ? SRTreeStore.isDirty(tree.id) : false;
  const save = $("#saveTree");
  if (save) {
    save.classList.toggle("has-dirty", dirty);
    save.title = dirty ? tr("pipeline.dirty") : tr("pipeline.clean");
  }
  const pill = $("#snapshotPill");
  if (pill) {
    const snapshot = SRTreeStore.snapshotRevision();
    const revision = tree ? tree.tree_revision : "";
    pill.textContent = tree
      ? tr("pipeline.snapshot", {snapshot: shortRev(snapshot), tree: shortRev(revision)})
      : tr("pipeline.noSnapshot");
    pill.title = tr("pipeline.snapshotTitle") + " " + (snapshot || "—");
  }
}

/* ── tree list ───────────────────────────────────────────────────────── */
function renderTreeList() {
  const list = SRTreeStore.trees();
  const count = $("#treeCount");
  if (count) count.textContent = tr("pipeline.treeCount", {n: list.length});
  const box = $("#treeList");
  if (!box) return;
  if (!list.length) {
    box.innerHTML = `<div class="rt-tree-empty">${esc(tr("pipeline.noTrees"))}</div>`;
    return;
  }
  const selected = currentTreeId();
  box.innerHTML = list.map(tree => {
    const active = tree.id === selected;
    const dirty = SRTreeStore.isDirty(tree.id);
    const state = tree.enabled ? tr("pipeline.enabled") : tr("pipeline.disabled");
    const toggle = tree.enabled ? tr("pipeline.disableTree") : tr("pipeline.enableTree");
    return `<div class="rt-tree-row${active ? " is-selected" : ""}${tree.enabled ? " is-enabled" : ""}"
        role="listitem" data-tree-id="${esc(tree.id)}">
      <button type="button" class="rt-tree-pick" aria-pressed="${active ? "true" : "false"}">
        <span class="rt-tree-name">${esc(tree.name || tree.id)}</span>
        <span class="rt-tree-meta">
          <span class="rt-badge${tree.enabled ? " on" : ""}">${esc(state)}</span>
          ${dirty ? `<span class="rt-dot" title="${esc(tr("pipeline.dirty"))}"></span>` : ""}
          <code>${esc(shortRev(tree.tree_revision))}</code>
        </span>
      </button>
      <span class="rt-tree-actions">
        <button type="button" class="rt-mini" data-tree-action="${tree.enabled ? "disable" : "enable"}"
                data-tree-id="${esc(tree.id)}" title="${esc(toggle)}" aria-label="${esc(toggle)}">
          ${tree.enabled ? "⏻" : "▶"}
        </button>
        <button type="button" class="rt-mini" data-tree-action="copy" data-tree-id="${esc(tree.id)}"
                title="${esc(tr("pipeline.copyTree"))}" aria-label="${esc(tr("pipeline.copyTree"))}">⧉</button>
      </span>
    </div>`;
  }).join("");
}

function renderTree() {
  const canvas = $("#treeRoot");
  if (!canvas) return;
  const tree = currentTree();
  const root = currentDraft();
  if (!tree || !root) {
    canvas.innerHTML = `<div class="rt-empty-canvas">${esc(tr("pipeline.noSelected"))}</div>`;
    return;
  }
  SRTreeView.render(canvas, {root: root, modelsById: modelsById()});
}

function renderAll() {
  renderTreeList();
  renderTree();
  renderChrome();
}

/* ── palette (signal library: drag source + click-to-pick) ───────────── */
function renderPalette() {
  const body = $("#palBody");
  if (!body) return;
  const parts = [];
  GROUPS.forEach(group => {
    const items = SIGNALS.filter(spec => spec.group === group);
    if (!items.length) return;
    parts.push(`<section class="grp"><h3>${esc(groupName(group))}<span class="rl"></span></h3>
      <div class="chips">${items.map(spec =>
        `<button type="button" class="sig${pendingSignal === spec.key ? " is-picked" : ""}"
                 data-sig="${esc(spec.key)}" draggable="false"
                 title="${esc(tr("pipeline.palSigTitle"))}">${esc(sigName(spec.key))}</button>`).join("")}
      </div></section>`);
  });
  body.innerHTML = parts.join("");
  const counter = $("#palCount");
  if (counter) counter.textContent = tr("pipeline.signalCount", {n: SIGNALS.length});
  const hint = $("#palHint");
  if (hint) {
    hint.textContent = pendingSignal
      ? tr("pipeline.palHintPicked", {name: sigName(pendingSignal)})
      : tr("pipeline.palHint");
  }
}

/* ── generic popover ─────────────────────────────────────────────────── */
function popHead(title, sub) {
  return `<div class="pop-head">
      <b>${esc(title)}</b>${sub ? `<span class="sub">${esc(sub)}</span>` : ""}
      <button type="button" class="pop-x" data-popclose aria-label="${esc(t("common.close"))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>`;
}
function popFoot(applyLabel) {
  return `<div class="pop-foot">
      <span class="pop-err" id="popErr"></span>
      <button type="button" class="pop-ok" data-pop="apply">${esc(applyLabel || t("common.save"))}</button>
    </div>`;
}
function placePop(anchor) {
  const pop = $("#pop");
  if (!anchor || !pop || pop.hidden) return;
  const rect = anchor.getBoundingClientRect();
  const width = pop.offsetWidth;
  const height = pop.offsetHeight;
  const left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12);
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - 12) top = Math.max(12, rect.top - height - 8);
  if (top < 12) top = Math.min(rect.bottom + 8, window.innerHeight - height - 12);
  pop.style.left = left + "px";
  pop.style.top = top + "px";
}
function openPop(html, anchor, context) {
  const pop = $("#pop");
  pop.hidden = false;
  pop.innerHTML = html;
  popContext = context || {anchor: anchor};
  popContext.anchor = anchor;
  placePop(anchor);
  const first = pop.querySelector("input, select, textarea, .pop-ok");
  if (first) first.focus();
  return pop;
}
function closePop() {
  const pop = $("#pop");
  pop.hidden = true;
  pop.innerHTML = "";
  popContext = null;
}
function showPopIssues(list) {
  const box = $("#popErr");
  if (!box) return;
  const issues = Array.isArray(list) ? list : [];
  box.innerHTML = issues.map(item =>
    `<div class="iv ${item.level === "warn" ? "warn" : "hard"}">• ${esc(issueText(item))}</div>`).join("");
  box.classList.toggle("show", issues.length > 0);
  if (popContext && popContext.anchor) placePop(popContext.anchor);
  else placePop(popContext && popContext.nodeId ? $("#pop") : null);
}

/* ── draft updates ───────────────────────────────────────────────────── */
function commitDraft(nextDraft, options) {
  const opts = options || {};
  const treeId = currentTreeId();
  if (!treeId || !nextDraft) return false;
  SRTreeStore.setDraft(treeId, nextDraft);
  renderAll();
  if (opts.toast) toastUndo(opts.toast);
  return true;
}
function patchNode(nodeId, updater, options) {
  const draft = currentDraft();
  if (!draft) return false;
  const ref = SRTreeModel.findNodeRef(draft, nodeId);
  if (!ref) return false;
  return commitDraft(SRTreeModel.updateNode(draft, nodeId, updater), options);
}

/* ── form bindings ───────────────────────────────────────────────────── */
function refreshFormModelBits(formEl) {
  const modelSelect = formEl.querySelector('[data-field="node.model"]');
  if (!modelSelect) return;
  modelSelect.addEventListener("change", () => {
    const reasoning = formEl.querySelector('[data-field="node.reasoning"]');
    if (reasoning) {
      reasoning.innerHTML = SRTreeEditor.reasoningOptionsHTML(
        modelSelect.value, modelsById(), reasoning.value);
    }
    const preview = formEl.querySelector(".rt-cap-preview");
    if (preview) {
      const capability = SRTreeView.capabilityOf(modelsById(), modelSelect.value);
      preview.textContent = capability.missing === "model" ? tr("tree.modelMissing")
        : capability.missing === "capability" ? tr("tree.capabilityMissing") : capability.text;
      if (capability.missing) preview.setAttribute("data-missing", capability.missing);
      else preview.removeAttribute("data-missing");
    }
  });
}
function bindWhenForm(scope) {
  const form = scope.querySelector("[data-when-form]");
  if (!form) return;
  const signalSelect = form.querySelector('[data-field="when.signal"]');
  if (!signalSelect || signalSelect.dataset.bound === "1") return;
  signalSelect.dataset.bound = "1";
  signalSelect.addEventListener("change", () => {
    const previous = SRTreeEditor.readWhenForm(form);
    const signalKey = signalSelect.value;
    const spec = SRTreeEditor.signalSpecOf(SIGNALS, signalKey);
    const when = signalKey ? {
      signal: signalKey,
      value: "",
      op: SRTreeEditor.opForSignal(SIGNALS, signalKey),
      conf_min: spec && spec.kind !== "rule" ? DEFAULT_CONF_MIN : previous.conf_min,
    } : null;
    form.outerHTML = SRTreeEditor.whenFormHTML({when: when, signals: SIGNALS});
    bindWhenForm(scope);
    const fresh = scope.querySelector('[data-when-form] [data-field="when.value"]');
    if (fresh && typeof fresh.focus === "function") fresh.focus();
    if (popContext && popContext.anchor) placePop(popContext.anchor);
  });
}
function bindWeightRowSignals(scope) {
  $$("[data-weight-row]", scope).forEach(row => {
    const signalSelect = row.querySelector('[data-weight-field="signal"]');
    if (!signalSelect || signalSelect.dataset.bound === "1") return;
    signalSelect.dataset.bound = "1";
    signalSelect.addEventListener("change", () => {
      const weightInput = row.querySelector('[data-weight-field="weight"]');
      const weight = {
        signal: signalSelect.value, value: "",
        weight: weightInput && weightInput.value !== "" ? Number(weightInput.value) : DEFAULT_WEIGHT,
      };
      row.outerHTML = SRTreeEditor.weightRowHTML({weight: weight, signals: SIGNALS});
      bindWeightRowSignals(scope);
      const fresh = scope.querySelector("[data-weight-row] [data-weight-field='value'], " +
        "[data-weight-row] [data-weight-field='signal']");
      if (fresh) fresh.focus();
      if (popContext && popContext.anchor) placePop(popContext.anchor);
    });
  });
}
function bindNodeForm(formEl, nodeId, context) {
  refreshFormModelBits(formEl);
  bindWhenForm(formEl);
  bindWeightRowSignals(formEl);
  formEl.addEventListener("click", event => {
    const actionEl = event.target.closest("[data-rt-form-action]");
    if (!actionEl) return;
    const action = actionEl.dataset.rtFormAction;
    if (action === "add-weight") {
      event.preventDefault();
      const holder = formEl.querySelector("[data-weights-form]");
      if (!holder) return;
      const first = SIGNALS.length ? SIGNALS[0].key : "";
      holder.insertAdjacentHTML("beforeend",
        SRTreeEditor.weightRowHTML({weight: {signal: first, value: "", weight: DEFAULT_WEIGHT}, signals: SIGNALS}));
      bindWeightRowSignals(holder);
      if (popContext && popContext.anchor) placePop(popContext.anchor);
    } else if (action === "remove-weight") {
      event.preventDefault();
      const row = actionEl.closest("[data-weight-row]");
      if (row) row.remove();
      if (popContext && popContext.anchor) placePop(popContext.anchor);
    } else if (action === "remove-option") {
      event.preventDefault();
      closePop();
      removeOption(nodeId, actionEl.dataset.optionKey);
    } else if (action === "add-option") {
      event.preventDefault();
      const kind = actionEl.dataset.optionKind;
      closePop();
      if (kind === "child") openNewChildOption(nodeId);
      else openNewModelOption(nodeId);
    }
  });
}

/* ── node editors ────────────────────────────────────────────────────── */
function refContext(ref) {
  return {
    isRoot: !ref || !ref.parent,
    isOtherwise: !!ref && ref.edgeKind === "otherwise",
  };
}
function nodeAnchorFn(nodeId) {
  return () => $("#treeRoot").querySelector(
    `[data-node-id="${CSS.escape(nodeId)}"][data-node-type]`);
}
function openNodeEditor(nodeId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref) return;
  const node = ref.node;
  const context = refContext(ref);
  const anchor = nodeAnchorFn(nodeId);
  const html = popHead(tr("pipeline.editNodeTitle", {type: SRTreeView.nodeTypeLabel(node.type)}),
    node.id) + SRTreeEditor.nodeFormHTML({
    node: node, signals: SIGNALS, modelsById: modelsById(), context: context,
  }) + popFoot(t("common.save"));
  const pop = openPop(html, anchor(), {kind: "node", nodeId: nodeId, anchor: anchor});
  const formEl = pop.querySelector("[data-node-form]");
  bindNodeForm(formEl, nodeId, context);
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    const values = SRTreeEditor.readNodeForm(formEl);
    const result = SRTreeEditor.applyNodeForm(node, values, {
      signals: SIGNALS, modelsById: modelsById(), context: context,
    });
    if (result.issues.some(item => item.level !== "warn")) {
      showPopIssues(result.issues);
      return;
    }
    closePop();
    patchNode(nodeId, () => result.node);
  });
}
function openWhenEditor(nodeId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref) return;
  const context = refContext(ref);
  if (context.isRoot || context.isOtherwise) { toastUndo(tr("pipeline.whenNotAllowed")); return; }
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-drop="when"][data-node-id="${CSS.escape(nodeId)}"]`) || nodeAnchorFn(nodeId)();
  const html = popHead(tr("pipeline.editWhenTitle"), ref.node.id) +
    SRTreeEditor.whenFormHTML({when: ref.node.when, signals: SIGNALS}) + popFoot(t("common.save"));
  const pop = openPop(html, anchor(), {kind: "when", nodeId: nodeId, anchor: anchor});
  bindWhenForm(pop);
  const formEl = pop.querySelector("[data-when-form]");
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    // the signal select rebuilds the form, so always read the live node
    const values = SRTreeEditor.readWhenForm(pop.querySelector("[data-when-form]"));
    const result = SRTreeEditor.applyWhenForm(ref.node, values, {signals: SIGNALS, context: context});
    if (result.issues.some(item => item.level !== "warn")) { showPopIssues(result.issues); return; }
    closePop();
    patchNode(nodeId, () => result.node, {toast: tr("pipeline.conditionSet")});
  });
  const clear = pop.querySelector('[data-when-action="clear"]');
  if (clear) clear.addEventListener("click", () => {
    closePop();
    patchNode(nodeId, node => Object.assign({}, node, {when: null}));
  });
}
function openOptionEditor(parentId, optionKey) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref || ref.node.type !== "nl_choice") return;
  const parent = ref.node;
  const index = parent.options.findIndex(option => option.key === optionKey);
  if (index < 0) return;
  const option = parent.options[index];
  const kind = option.child != null ? "child" : "model";
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-action="edit-option"][data-option-key="${CSS.escape(optionKey)}"]`) ||
    nodeAnchorFn(parentId)();
  const html = popHead(tr("pipeline.editOptionTitle"), `${parent.id} · ${optionKey}`) +
    SRTreeEditor.optionFormHTML({option: option, kind: kind, modelsById: modelsById(), index: index}) +
    popFoot(t("common.save"));
  const pop = openPop(html, anchor(), {kind: "option", nodeId: parentId, optionKey: optionKey, anchor: anchor});
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    const formEl = pop.querySelector("[data-option-form]");
    const values = SRTreeEditor.readOptionForm(formEl);
    const taken = parent.options.filter(item => item.key !== optionKey).map(item => item.key);
    const result = SRTreeEditor.applyOptionForm(option, values, {
      kind: kind, modelsById: modelsById(), takenKeys: taken,
    });
    if (result.issues.some(item => item.level !== "warn")) { showPopIssues(result.issues); return; }
    const replaced = SRTreeEditor.replaceOption(parent, optionKey, result.option);
    if (replaced.issues.length) { showPopIssues(replaced.issues); return; }
    closePop();
    patchNode(parentId, () => replaced.node);
  });
  const remove = pop.querySelector('[data-rt-form-action="remove-option"]');
  if (remove) remove.addEventListener("click", () => { closePop(); removeOption(parentId, optionKey); });
}
function openWeightEditor(nodeId, index) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref || !Array.isArray(ref.node.weights) || !ref.node.weights[index]) return;
  const weight = ref.node.weights[index];
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-action="edit-weight"][data-weight-index="${index}"]`) || nodeAnchorFn(nodeId)();
  const html = popHead(tr("pipeline.editWeightTitle"), ref.node.id) +
    SRTreeEditor.weightRowHTML({weight: weight, signals: SIGNALS, index: index}) +
    popFoot(t("common.save"));
  const pop = openPop(html, anchor(), {kind: "weight", nodeId: nodeId, anchor: anchor});
  bindWeightRowSignals(pop);
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    const values = SRTreeEditor.readWeightRow(pop.querySelector("[data-weight-row]"));
    const issues = [];
    if (!SRTreeEditor.signalSpecOf(SIGNALS, values.signal)) issues.push(localIssue("unknown_signal", tr("pipeline.badSignal")));
    if (!values.value) issues.push(localIssue("missing_value", tr("pipeline.badValue")));
    if (!Number.isFinite(values.weight) || values.weight < 0 || values.weight > 1) {
      issues.push(localIssue("invalid_threshold", tr("pipeline.badWeight")));
    }
    if (issues.length) { showPopIssues(issues); return; }
    closePop();
    patchNode(nodeId, node => Object.assign({}, node, {
      weights: node.weights.map((item, itemIndex) => itemIndex === index
        ? {signal: values.signal, value: values.value, weight: values.weight} : item),
    }));
  });
}

/* ── structure changes ───────────────────────────────────────────────── */
function uniqueNodeId(prefix) {
  return SRTreeEditor.uniqueNodeId(currentDraft(), prefix || "n");
}
function defaultRouteModel() {
  const ids = Array.from(modelsById().keys());
  return ids.length ? ids[0] : "";
}
function newNodeOfType(type, options) {
  const opts = options || {};
  if (type === "route") {
    const model = opts.model || defaultRouteModel();
    if (!model) return null;
    return SRTreeEditor.emptyNode("route", uniqueNodeId("n"), {model: model});
  }
  return SRTreeEditor.emptyNode(type, uniqueNodeId("n"));
}
function nodeTypeButtonsHTML() {
  return `<div class="rt-type-picker">${NODE_TYPES.map(type =>
    `<button type="button" class="rt-type-choice" data-new-node-type="${type}">${esc(SRTreeView.nodeTypeLabel(type))}</button>`).join("")}</div>`;
}
function openNewNodePicker(parentId, target) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref) return;
  const actionName = target === "otherwise" ? "add-otherwise" : "add-child";
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-action="${actionName}"][data-node-id="${CSS.escape(parentId)}"]`) || nodeAnchorFn(parentId)();
  const html = popHead(tr("pipeline.newNodeTitle"), SRTreeView.nodeTypeLabel(ref.node.type)) +
    nodeTypeButtonsHTML();
  const pop = openPop(html, anchor(), {kind: "new-node", nodeId: parentId, target: target, anchor: anchor});
  pop.querySelectorAll("[data-new-node-type]").forEach(button => {
    button.addEventListener("click", () => {
      const newNode = newNodeOfType(button.dataset.newNodeType);
      if (!newNode) { showPopIssues([localIssue("no_models", tr("pipeline.noModels"))]); return; }
      const result = target === "otherwise"
        ? SRTreeEditor.setOtherwise(ref.node, newNode)
        : SRTreeEditor.addChild(ref.node, newNode);
      if (result.issues.length) { showPopIssues(result.issues); return; }
      closePop();
      patchNode(parentId, () => result.node, {toast: tr("pipeline.nodeAdded")});
    });
  });
}
function openNewModelOption(parentId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref || ref.node.type !== "nl_choice") return;
  const parent = ref.node;
  const modelIds = Array.from(modelsById().keys());
  if (!modelIds.length) { toastUndo(tr("pipeline.noModels")); return; }
  const suggestedKey = SRTreeEditor.nextOptionKey(parent);
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-action="add-option"][data-option-kind="model"][data-node-id="${CSS.escape(parentId)}"]`);
  const html = popHead(tr("pipeline.addModelOption"), parent.id) +
    `<label class="rt-field"><span>${esc(tr("pipeline.optionKey"))}</span>
       <input type="text" id="newOptionKey" value="${esc(suggestedKey)}"></label>
     <label class="rt-field"><span>${esc(tr("tree.model"))}</span>
       <select id="newOptionModel">${modelIds.map(id =>
         `<option value="${esc(id)}">${esc(id)}</option>`).join("")}</select></label>` +
    popFoot(t("common.add"));
  const pop = openPop(html, anchor(), {kind: "new-option", nodeId: parentId, anchor: anchor});
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    const key = ($("#newOptionKey").value || "").trim();
    const option = SRTreeEditor.emptyOption("model", key, {model: $("#newOptionModel").value});
    const result = SRTreeEditor.addOption(parent, option);
    if (result.issues.length) { showPopIssues(result.issues); return; }
    closePop();
    patchNode(parentId, () => result.node, {toast: tr("pipeline.optionAdded")});
  });
}
function openNewChildOption(parentId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref || ref.node.type !== "nl_choice") return;
  const parent = ref.node;
  const modelIds = Array.from(modelsById().keys());
  const suggestedKey = SRTreeEditor.nextOptionKey(parent);
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-action="add-option"][data-option-kind="child"][data-node-id="${CSS.escape(parentId)}"]`);
  const html = popHead(tr("pipeline.addChildOption"), parent.id) +
    `<label class="rt-field"><span>${esc(tr("pipeline.optionKey"))}</span>
       <input type="text" id="newOptionKey" value="${esc(suggestedKey)}"></label>
     <label class="rt-field"><span>${esc(tr("pipeline.optionDescription"))}</span>
       <textarea id="newOptionDesc" rows="2" placeholder="${esc(tr("pipeline.descriptionPlaceholder"))}"></textarea></label>
     <label class="rt-field"><span>${esc(tr("pipeline.childNodeType"))}</span>
       <select id="newChildType">${NODE_TYPES.map(type =>
         `<option value="${type}"${type === "route" ? " selected" : ""}>${esc(SRTreeView.nodeTypeLabel(type))}</option>`).join("")}</select></label>
     <label class="rt-field" id="newChildModelRow"><span>${esc(tr("tree.model"))}</span>
       <select id="newChildModel">${modelIds.map(id =>
         `<option value="${esc(id)}">${esc(id)}</option>`).join("")}</select></label>` +
    popFoot(t("common.add"));
  const pop = openPop(html, anchor(), {kind: "new-child-option", nodeId: parentId, anchor: anchor});
  const typeSelect = $("#newChildType");
  const syncModelRow = () => {
    const row = $("#newChildModelRow");
    if (row) row.style.display = typeSelect.value === "route" ? "" : "none";
  };
  typeSelect.addEventListener("change", syncModelRow);
  syncModelRow();
  pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
    const key = ($("#newOptionKey").value || "").trim();
    const description = ($("#newOptionDesc").value || "").trim();
    const child = newNodeOfType(typeSelect.value, {
      model: $("#newChildModel") ? $("#newChildModel").value : "",
    });
    if (!child) { showPopIssues([localIssue("no_models", tr("pipeline.noModels"))]); return; }
    const option = SRTreeEditor.emptyOption("child", key, {child: child, description: description});
    const result = SRTreeEditor.addOption(parent, option);
    if (result.issues.length) { showPopIssues(result.issues); return; }
    closePop();
    patchNode(parentId, () => result.node, {toast: tr("pipeline.optionAdded")});
  });
}
function removeWithUndo(nextDraft, message) {
  const before = SRTreeModel.clone(currentDraft());
  if (!commitDraft(nextDraft)) return;
  toastUndo(message, () => commitDraft(before));
}
function removeNodeById(nodeId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref) return;
  if (!ref.parent) { toastUndo(tr("pipeline.cannotRemoveRoot")); return; }
  let next;
  try { next = SRTreeModel.removeNode(draft, nodeId); }
  catch (err) { toastUndo(String((err && err.message) || err)); return; }
  removeWithUndo(next, tr("pipeline.nodeRemoved"));
}
function removeOption(parentId, optionKey) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref || ref.node.type !== "nl_choice") return;
  const result = SRTreeEditor.removeOption(ref.node, optionKey);
  if (result.issues.length) { renderIssues(result.issues); return; }
  removeWithUndo(SRTreeModel.updateNode(draft, parentId, () => result.node), tr("pipeline.optionRemoved"));
}
function removeOtherwise(parentId) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, parentId) : null;
  if (!ref) return;
  const result = SRTreeEditor.clearOtherwise(ref.node);
  if (result.issues.length) { renderIssues(result.issues); return; }
  removeWithUndo(SRTreeModel.updateNode(draft, parentId, () => result.node),
    tr("pipeline.otherwiseRemoved"));
}
function moveNodeBy(nodeId, delta) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref || ref.edgeKind === "root" || ref.edgeKind === "otherwise") return;
  const next = SRDnd.applyMove(draft, {nodeId: nodeId, targetIndex: ref.index + delta});
  if (JSON.stringify(next) === JSON.stringify(draft)) return;
  commitDraft(next);
}

/* ── signal drops: only explicit when / weights zones ────────────────── */
function applySignalToWhen(nodeId, signalKey, value) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref) return;
  const values = {signal: signalKey, value: value};
  const spec = SRTreeEditor.signalSpecOf(SIGNALS, signalKey);
  if (spec && spec.kind !== "rule") values.conf_min = DEFAULT_CONF_MIN;
  const result = SRTreeEditor.applyWhenForm(ref.node, values,
    {signals: SIGNALS, context: refContext(ref)});
  if (result.issues.some(item => item.level !== "warn")) { renderIssues(result.issues); return; }
  patchNode(nodeId, () => result.node, {toast: tr("pipeline.conditionSet")});
}
function applySignalToWeights(nodeId, signalKey, value) {
  const draft = currentDraft();
  const ref = draft ? SRTreeModel.findNodeRef(draft, nodeId) : null;
  if (!ref) return;
  const result = SRTreeEditor.addWeight(ref.node, {signal: signalKey, value: value, signals: SIGNALS});
  if (result.issues.length) { renderIssues(result.issues); return; }
  patchNode(nodeId, () => result.node, {toast: tr("pipeline.weightAdded")});
}
function dropSignalOn(signalKey, target) {
  if (!target || !target.nodeId) return;
  lastDropAt = Date.now();
  const zone = target.zone;
  const anchor = () => $("#treeRoot").querySelector(
    `[data-rt-drop="${zone}"][data-node-id="${CSS.escape(target.nodeId)}"]`);
  openValuePicker(signalKey, anchor, value => {
    if (zone === "when") applySignalToWhen(target.nodeId, signalKey, value);
    else applySignalToWeights(target.nodeId, signalKey, value);
  });
}

/* ── signal -> value picker (generic UI; no node semantics) ──────────── */
function listChoices(signalKey) {
  if (signalKey === "keyword") return KEYWORD_GROUPS.slice();
  if (signalKey === "time_window") return TIME_PERIODS.slice();
  return [];
}
function openValuePicker(signalKey, anchorFn, onPick) {
  const spec = SRTreeEditor.signalSpecOf(SIGNALS, signalKey);
  const anchor = typeof anchorFn === "function" ? anchorFn : () => anchorFn;
  const free = !spec || !Array.isArray(spec.values) || !spec.values.length;
  const values = free ? [] : spec.values.map(entry => Array.isArray(entry) ? entry : [entry.key, entry.zh, entry.en]);
  const head = popHead(tr("pipeline.pickValueTitle", {signal: sigName(signalKey)}), "");
  const body = free
    ? `<label class="rt-field"><span>${esc(tr("pipeline.value"))}</span>
         <input type="text" id="pickValueInput" list="pickValueList"
                placeholder="${esc(tr("pipeline.freePlaceholder"))}"></label>
       <datalist id="pickValueList">${listChoices(signalKey).map(item =>
         `<option value="${esc(item)}">`).join("")}</datalist>`
    : `<div class="pop-vals" id="pickValueList">${values.map(entry =>
        `<button type="button" data-pick-value="${esc(entry[0])}">${esc(
          LANG === "zh" ? (entry[1] || entry[0]) : (entry[2] || entry[1] || entry[0]))}</button>`).join("")}</div>`;
  const pop = openPop(head + body + popFoot(t("common.add")), anchor(), {kind: "value-picker", anchor: anchor});
  if (free) {
    pop.querySelector('[data-pop="apply"]').addEventListener("click", () => {
      const value = ($("#pickValueInput").value || "").trim();
      if (!value) { $("#pickValueInput").focus(); return; }
      closePop();
      onPick(value);
    });
    $("#pickValueInput").addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); pop.querySelector('[data-pop="apply"]').click(); }
    });
  } else {
    const apply = pop.querySelector('[data-pop="apply"]');
    if (apply) apply.style.display = "none";
    pop.querySelectorAll("[data-pick-value]").forEach(button => {
      button.addEventListener("click", () => {
        const value = button.dataset.pickValue;
        closePop();
        onPick(value);
      });
    });
  }
}
function openSignalPicker(anchorEl, onPick) {
  const anchor = () => anchorEl || $("#palBody .sig");
  const html = popHead(tr("pipeline.pickSignalTitle"), "") +
    `<div class="pop-vals" id="pickSignalList">${SIGNALS.map(spec =>
      `<button type="button" data-pick-signal="${esc(spec.key)}">${esc(sigName(spec.key))}</button>`).join("")}</div>`;
  const pop = openPop(html, anchor(), {kind: "signal-picker", anchor: anchor});
  pop.querySelectorAll("[data-pick-signal]").forEach(button => {
    button.addEventListener("click", () => {
      const signalKey = button.dataset.pickSignal;
      closePop();
      openValuePicker(signalKey, anchor, value => onPick(signalKey, value));
    });
  });
}

/* ── delegated canvas actions ────────────────────────────────────────── */
function onCanvasClick(event) {
  const zoneEl = event.target.closest("[data-rt-drop]");
  if (zoneEl && pendingSignal) {
    event.preventDefault();
    const signalKey = pendingSignal;
    pendingSignal = null;
    renderPalette();
    dropSignalOn(signalKey, {nodeId: zoneEl.dataset.nodeId, zone: zoneEl.dataset.rtDrop});
    return;
  }
  const actionEl = event.target.closest("[data-rt-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.rtAction;
  const nodeId = actionEl.dataset.nodeId;
  if (action === "edit-node") openNodeEditor(nodeId);
  else if (action === "edit-when") openWhenEditor(nodeId);
  else if (action === "edit-option") openOptionEditor(nodeId, actionEl.dataset.optionKey);
  else if (action === "remove-option") removeOption(nodeId, actionEl.dataset.optionKey);
  else if (action === "edit-weight") openWeightEditor(nodeId, Number(actionEl.dataset.weightIndex));
  else if (action === "add-weight") openSignalPicker(actionEl, (signalKey, value) =>
    applySignalToWeights(nodeId, signalKey, value));
  else if (action === "add-child") openNewNodePicker(nodeId, "children");
  else if (action === "add-otherwise") openNewNodePicker(nodeId, "otherwise");
  else if (action === "remove-otherwise") removeOtherwise(nodeId);
  else if (action === "remove-node") removeNodeById(nodeId);
  else if (action === "move-node") moveNodeBy(nodeId, Number(actionEl.dataset.direction));
  else if (action === "add-option") {
    if (actionEl.dataset.optionKind === "child") openNewChildOption(nodeId);
    else openNewModelOption(nodeId);
  }
}
function onCanvasKeydown(event) {
  if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
  const holder = event.target.closest(
    '[data-rt-action="move-node"], [data-rt-drag="node"], [data-node-id][data-node-type]');
  if (!holder) return;
  const nodeId = holder.getAttribute("data-node-id");
  if (!nodeId) return;
  event.preventDefault();
  moveNodeBy(nodeId, event.key === "ArrowUp" ? -1 : 1);
}

/* ── tree list actions ───────────────────────────────────────────────── */
function selectTree(treeId) {
  try { SRTreeStore.select(treeId); }
  catch (err) { toastUndo(errText(err)); return; }
  closePop();
  renderAll();
}
function treeIdFromName(name) {
  const base = String(name || "").toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+|[-._]+$/g, "").slice(0, 48);
  const prefix = /^[a-z0-9]/.test(base) ? base : "tree";
  const taken = new Set(SRTreeStore.trees().map(tree => tree.id));
  let candidate = `${prefix}-${Date.now().toString(36)}`;
  if (taken.has(candidate.slice(0, 64))) {
    candidate = `${candidate}-${Math.random().toString(36).slice(2, 7)}`;
  }
  return candidate.slice(0, 64);
}
function openCreateTree() {
  const modelIds = Array.from(modelsById().keys());
  const anchor = () => $("#newTree");
  const html = popHead(tr("pipeline.treeCreateTitle"), "") +
    `<label class="rt-field"><span>${esc(tr("pipeline.treeName"))}</span>
       <input type="text" id="newTreeName" placeholder="${esc(tr("pipeline.treeNamePlaceholder"))}"></label>
     <label class="rt-field"><span>${esc(tr("pipeline.rootType"))}</span>
       <select id="newTreeRootType">${NODE_TYPES.map(type =>
         `<option value="${type}"${type === "branch" ? " selected" : ""}>${esc(SRTreeView.nodeTypeLabel(type))}</option>`).join("")}</select></label>
     <label class="rt-field" id="newTreeModelRow"><span>${esc(tr("tree.model"))}</span>
       <select id="newTreeModel">${modelIds.map(id =>
         `<option value="${esc(id)}">${esc(id)}</option>`).join("")}</select></label>
     <p class="rt-form-note">${esc(tr("pipeline.newTreeNote"))}</p>` + popFoot(t("pipeline.treeCreate"));
  const pop = openPop(html, anchor(), {kind: "create-tree", anchor: anchor});
  const typeSelect = $("#newTreeRootType");
  const syncModelRow = () => {
    const row = $("#newTreeModelRow");
    if (row) row.style.display = typeSelect.value === "route" ? "" : "none";
  };
  typeSelect.addEventListener("change", syncModelRow);
  syncModelRow();
  pop.querySelector('[data-pop="apply"]').addEventListener("click", async () => {
    const name = ($("#newTreeName").value || "").trim();
    if (!name) { showPopIssues([localIssue("name_required", tr("pipeline.treeNameRequired"))]); return; }
    if (typeSelect.value === "route" && !modelIds.length) {
      showPopIssues([localIssue("no_models", tr("pipeline.noModels"))]);
      return;
    }
    try {
      const root = SRTreeEditor.emptyNode(typeSelect.value, `root-${Date.now().toString(36)}`, {
        model: $("#newTreeModel") ? $("#newTreeModel").value : undefined,
      });
      await SRTreeStore.create(name, root);
      closePop();
      renderAll();
      toastUndo(tr("pipeline.treeCreated"));
    } catch (err) {
      showPopIssues(issueList(err) || [localIssue("create", errText(err))]);
    }
  });
}
function openCopyTree(treeId) {
  const tree = SRTreeStore.tree(treeId);
  if (!tree) return;
  const suggested = tr("pipeline.copySuffix", {name: tree.name || tree.id});
  const anchor = () => $("#treeList").querySelector(`[data-tree-id="${CSS.escape(treeId)}"]`);
  const html = popHead(tr("pipeline.treeCopyTitle"), tree.name || tree.id) +
    `<label class="rt-field"><span>${esc(tr("pipeline.treeName"))}</span>
       <input type="text" id="copyTreeName" value="${esc(suggested)}"></label>
     <p class="rt-form-note">${esc(tr("pipeline.copyNote"))}</p>` + popFoot(t("pipeline.treeCopy"));
  const pop = openPop(html, anchor(), {kind: "copy-tree", treeId: treeId, anchor: anchor});
  pop.querySelector('[data-pop="apply"]').addEventListener("click", async () => {
    const name = ($("#copyTreeName").value || "").trim();
    if (!name) { showPopIssues([localIssue("name_required", tr("pipeline.treeNameRequired"))]); return; }
    try {
      await SRTreeStore.copy(treeId, {id: treeIdFromName(name), name: name});
      closePop();
      renderAll();
      toastUndo(tr("pipeline.treeCopied"));
    } catch (err) {
      showPopIssues(issueList(err) || [localIssue("copy", errText(err))]);
    }
  });
}
async function treeAction(action, treeId) {
  if (action === "copy") { openCopyTree(treeId); return; }
  try {
    if (action === "enable") { await SRTreeStore.enable(treeId); toastUndo(tr("pipeline.treeEnabled")); }
    else if (action === "disable") { await SRTreeStore.disable(treeId); toastUndo(tr("pipeline.treeDisabled")); }
  } catch (err) {
    const issues = issueList(err);
    if (issues) renderIssues(issues);
    toastUndo(errText(err));
    return;
  }
  renderAll();
}

/* ── save / export / import ──────────────────────────────────────────── */
async function saveTree() {
  const treeId = currentTreeId();
  if (!treeId) { toastUndo(tr("pipeline.noSelected")); return; }
  try {
    const out = await SRTreeStore.save(treeId);
    renderIssues([]);
    renderWarnings(out.warnings || []);
    renderAll();
    toastUndo(t("common.saved"));
  } catch (err) {
    const issues = issueList(err);
    renderIssues(issues || [localIssue("save", errText(err))]);
    toastUndo(tr("pipeline.saveFail"));
  }
}
function exportTree() {
  const tree = currentTree();
  const root = currentDraft();
  if (!tree || !root) { toastUndo(tr("pipeline.noSelected")); return; }
  const payload = {
    format: "sr-routing-tree",
    version: 1,
    name: tree.name || tree.id,
    root: SRTreeModel.clone(root),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {type: "application/json"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${tree.id}.json`;
  document.body.appendChild(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 0);
  toastUndo(tr("pipeline.exportOk", {name: link.download}));
}
async function importTree(file) {
  try {
    const parsed = JSON.parse(await file.text());
    const payload = parsed && typeof parsed === "object" ? parsed : {};
    if (payload.format && payload.format !== "sr-routing-tree") {
      throw new Error(tr("pipeline.importBadFormat"));
    }
    const rawRoot = payload.root != null ? payload.root
      : (payload.id && payload.type ? payload : null);
    if (!rawRoot) throw new Error(tr("pipeline.importEmpty"));
    const root = SRTreeModel.normalizeNode(rawRoot, {root: true, otherwise: false});
    commitDraft(root, {toast: tr("pipeline.importOk")});
    renderWarnings([]);
  } catch (err) {
    toastUndo(tr("pipeline.importBad", {err: errText(err)}));
  } finally {
    const input = $("#importFile");
    if (input) input.value = "";
  }
}

/* ── try drawer ──────────────────────────────────────────────────────── */
function openSim() {
  document.body.classList.add("sim-open");
  $("#sim").setAttribute("aria-hidden", "false");
  renderSimText();
  renderTry();
  $("#simText").focus();
}
function closeSim() {
  document.body.classList.remove("sim-open");
  $("#sim").setAttribute("aria-hidden", "true");
}
function renderSimText() {
  $("#simText").placeholder = tr("pipeline.tryPlaceholder");
  $("#simRun").textContent = tr("pipeline.tryRun");
}
function renderTry() {
  const out = $("#simOut");
  if (!out) return;
  const treeId = currentTreeId();
  const tree = currentTree();
  const result = treeId ? SRTreeStore.lastTry(treeId) : null;
  const dirtyWarn = tree && SRTreeStore.isDirty(tree.id)
    ? `<div class="sim-warn">⚠ ${esc(tr("pipeline.tryDirtyWarn"))}</div>` : "";
  if (!tree || !treeId) {
    out.innerHTML = `<b>${esc(tr("pipeline.tryEmptyTitle"))}</b>${esc(tr("pipeline.tryEmpty"))}`;
    return;
  }
  if (!result) {
    out.innerHTML = dirtyWarn + `<b>${esc(tr("pipeline.tryEmptyTitle"))}</b>` +
      esc(tr("pipeline.tryEmpty"));
    return;
  }
  const stale = result.tree_id !== treeId ||
    (result.tree_revision && tree.tree_revision &&
     result.tree_revision !== tree.tree_revision);
  const staleWarn = stale ? `<div class="sim-warn">⚠ ${esc(tr("pipeline.tryStale"))}</div>` : "";
  out.innerHTML = dirtyWarn + staleWarn + '<div class="sim-trace" id="simTrace"></div>';
  /* one shared renderer for the try drawer AND the calculation log */
  SRTraceView.renderRoutingTrace($("#simTrace"), result, {tree: tree});
}

async function runTry() {
  const text = ($("#simText").value || "").trim();
  if (!text) { $("#simText").focus(); return; }
  const treeId = currentTreeId();
  if (!treeId) { toastUndo(tr("pipeline.noSelected")); return; }
  if (tryBusy) return;
  tryBusy = true;
  $("#simRun").disabled = true;
  try {
    await SRTreeStore.tryTree(treeId, {text: text});
    renderTry();
  } catch (err) {
    const issues = issueList(err);
    $("#simOut").innerHTML = `<b>${esc(tr("pipeline.tryFail"))}</b>${esc(
      issues ? issues.map(issueText).join("; ") : errText(err))}`;
  }
  tryBusy = false;
  $("#simRun").disabled = false;
}

/* ── toast ───────────────────────────────────────────────────────────── */
let toastTimer = null;
function toastUndo(message, undo) {
  const el = $("#t4toast");
  if (!el) return;
  el.innerHTML = `<span>${esc(message)}</span>` +
    (undo ? `<button type="button">${esc(tr("pipeline.undo"))}</button>` : "");
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), undo ? 6000 : 3200);
  if (undo) {
    el.querySelector("button").addEventListener("click", () => {
      undo();
      el.classList.remove("show");
    }, {once: true});
  }
}

/* ── server clock (time_window judges by server time) ────────────────── */
function startServerClock() {
  startServerClockTick(() => {
    const el = $("#serverClockPill");
    if (!el) return;
    el.innerHTML = `${esc(tr("pipeline.serverClock"))} <b>${esc(fmtServerTime(serverNow()))}</b>`;
  });
}

/* ── DND lifecycle (destroy before create: never double-bind) ────────── */
function rebuildDnd() {
  if (dnd) { dnd.destroy(); dnd = null; }
  const root = $("#treeRoot");
  if (!root) return;
  dnd = SRDnd.createDndController({
    root: root,
    onMove: info => {
      const draft = currentDraft();
      if (!draft) return;
      const next = SRDnd.applyMove(draft, info);
      if (JSON.stringify(next) !== JSON.stringify(draft)) commitDraft(next);
    },
    onDropSignal: (signalKey, target) => dropSignalOn(signalKey, target),
  });
}

/* ── wiring ──────────────────────────────────────────────────────────── */
function bindChrome() {
  $("#saveTree").addEventListener("click", saveTree);
  $("#openSim").addEventListener("click", openSim);
  $("#simClose").addEventListener("click", closeSim);
  $("#simMask").addEventListener("click", closeSim);
  $("#simRun").addEventListener("click", runTry);
  $("#simText").addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      runTry();
    }
  });
  $("#exportTree").addEventListener("click", exportTree);
  $("#importTree").addEventListener("click", () => $("#importFile").click());
  $("#importFile").addEventListener("change", event => {
    const file = event.target.files && event.target.files[0];
    if (file) importTree(file);
  });
  $("#newTree").addEventListener("click", openCreateTree);
  $("#palToggle").addEventListener("click", () => {
    const palette = $("#palette");
    const open = palette.classList.contains("closed");
    palette.classList.toggle("closed", !open);
    $("#palToggle").setAttribute("aria-expanded", String(open));
  });
  $("#palBody").addEventListener("click", event => {
    const chip = event.target.closest("[data-sig]");
    if (!chip) return;
    if (Date.now() - lastDropAt < 400) return;   // a completed drag already applied it
    pendingSignal = pendingSignal === chip.dataset.sig ? null : chip.dataset.sig;
    renderPalette();
  });
  $("#treeList").addEventListener("click", event => {
    const actionEl = event.target.closest("[data-tree-action]");
    if (actionEl) {
      event.preventDefault();
      treeAction(actionEl.dataset.treeAction, actionEl.dataset.treeId);
      return;
    }
    const row = event.target.closest("[data-tree-id]");
    if (row) selectTree(row.getAttribute("data-tree-id"));
  });
  $("#treeRoot").addEventListener("click", onCanvasClick);
  $("#treeRoot").addEventListener("keydown", onCanvasKeydown);
  $("#issues").addEventListener("click", event => {
    const locate = event.target.closest("[data-issue-node]");
    if (locate) highlightNode(locate.getAttribute("data-issue-node"));
  });
  $("#pop").addEventListener("click", event => {
    if (event.target.closest("[data-popclose]")) closePop();
  });
  document.addEventListener("mousedown", event => {
    const pop = $("#pop");
    if (pop.hidden) return;
    if (pop.contains(event.target)) return;
    if (event.target.closest(
      "[data-rt-action],[data-rt-form-action],[data-rt-drop],[data-popclose],#newTree,#treeList")) return;
    closePop();
  });
  document.addEventListener("keydown", event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveTree();
    } else if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      openSim();
      runTry();
    } else if (event.key === "Escape") {
      if (!$("#pop").hidden) { closePop(); return; }
      if (document.body.classList.contains("sim-open")) closeSim();
    }
  });
  window.addEventListener("resize", () => {
    if (!$("#pop").hidden && popContext && popContext.anchor) placePop(popContext.anchor());
  });
}
function highlightNode(nodeId) {
  const card = $("#treeRoot").querySelector(`[data-node-id="${CSS.escape(nodeId)}"][data-node-type]`);
  if (!card) return;
  card.classList.add("is-error");
  card.scrollIntoView({block: "center", behavior: "smooth"});
  setTimeout(() => card.classList.remove("is-error"), 2400);
}

/* ── boot ────────────────────────────────────────────────────────────── */
async function boot() {
  try {
    const [signalBody] = await Promise.all([
      api.get("/admin/signals"),
      SRTreeStore.load(),
    ]);
    GROUPS = Array.isArray(signalBody.groups) && signalBody.groups.length
      ? signalBody.groups
      : ["safety", "domain", "difficulty", "context", "session", "rule"];
    SIGNALS = Array.isArray(signalBody.signals) ? signalBody.signals : [];
    KEYWORD_GROUPS = Array.isArray(signalBody.keyword_groups) ? signalBody.keyword_groups : [];
    TIME_PERIODS = Array.isArray(signalBody.time_periods) ? signalBody.time_periods : [];
    setSignalRegistry(SIGNALS);
    setServerOffset(signalBody.now);
    startServerClock();
    renderPalette();
    renderAll();
  } catch (err) {
    const issues = issueList(err);
    renderIssues(issues || [localIssue("load", tr("pipeline.loadFail", {err: errText(err)}))]);
    toastUndo(tr("pipeline.loadFail", {err: errText(err)}));
  }
}

window.onLangChange = () => {
  renderPalette();
  renderAll();
  renderSimText();
  renderTry();
  rebuildDnd();
};

bindChrome();
renderChrome();
renderPalette();
renderTree();
boot();
