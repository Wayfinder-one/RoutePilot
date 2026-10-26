/* Unified routing-tree view (console rewrite, task-13 pre-work, 2026-10-26).

   Pure renderer: it turns one canonical tree root (SRTreeModel shape) plus the
   model catalog into DOM. It NEVER mutates the tree and never interprets the
   node arrays: every out-edge comes from SRTreeModel.childRefs, so the editor,
   the engine and the view agree on the graph.

   Four node types and three out-edge kinds are addressable by data attributes
   the page delegates on (no listeners are attached here):

     cards   [data-node-id][data-node-type="branch|nl_choice|route|block"]
             [data-node-role="root|otherwise"] (absent for normal nodes)
     edges   [data-edge-kind="children|optionChild|otherwise"]
             [data-option-key="<key>"]  + [data-option-kind="model|child"]
     zones   [data-rt-drop="when|weights"][data-node-id]  (explicit drop areas)
     drags   [data-rt-drag="node"][data-node-id]
     actions [data-rt-action="..."][data-node-id] etc. (see ACTION list below)

   Direct `model` candidates are rendered as an edge with the model id and the
   capability text from GET /admin/models (`modelsById` row: top-level
   `capability`); the capability is display-only and is never copied into the
   tree JSON. `child` candidates render their description plus the nested
   subtree. `otherwise` is an explicit, labelled edge and — like the root —
   never carries a `when` affordance.

   Class names use the new `rt-*` layer; pipeline.css gets these styles in the
   wiring round (this module intentionally ships no CSS). Labels resolve
   through the page's t() when loaded, with built-in zh fallbacks so the
   renderer is testable before the i18n keys land.

   Browser-only (DOM); loaded after routing-tree-model.js. */
"use strict";

const SRTreeView = (function () {
  /* ── labels (zh fallbacks; t() wins when the key exists) ─────────────── */
  const FALLBACK = {
    "tree.root": "入口",
    "tree.when": "进入条件",
    "tree.when.add": "＋ 条件",
    "tree.otherwise": "兜底出边 otherwise",
    "tree.branch": "普通分支",
    "tree.nl_choice": "自然语言选择",
    "tree.route": "路由",
    "tree.block": "拦截",
    "tree.instructions": "节点问题",
    "tree.minProb": "最高候选概率 ≥",
    "tree.minMargin": "前两名概率差 ≥",
    "tree.weights": "加权条件",
    "tree.minScore": "达成率 ≥",
    "tree.addWeight": "＋ 加权条件",
    "tree.addChild": "＋ 子节点",
    "tree.addOption.model": "＋ 模型候选",
    "tree.addOption.child": "＋ 子候选",
    "tree.addOtherwise": "＋ otherwise 兜底",
    "tree.removeOtherwise": "移除 otherwise",
    "tree.removeNode": "删除节点",
    "tree.moveUp": "上移",
    "tree.moveDown": "下移",
    "tree.editNode": "编辑节点",
    "tree.editOption": "编辑候选",
    "tree.removeOption": "删除候选",
    "tree.editWhen": "编辑进入条件",
    "tree.confidence": "置信 ≥",
    "tree.deterministic": "确定性",
    "tree.capabilityMissing": "未配置能力说明",
    "tree.modelMissing": "模型目录缺失",
    "tree.directModel": "直接模型",
    "tree.childTarget": "子分支",
    "tree.emptyChildren": "还没有子节点",
    "tree.emptyOptions": "还没有候选",
    "tree.optionKey": "候选 key",
    "tree.reasoning": "推理",
    "tree.confMin": "置信 ≥",
    "tree.drag": "拖动排序",
  };
  function tr(key, vars) {
    let text = FALLBACK[key] != null ? FALLBACK[key] : key;
    try {
      if (typeof t === "function") {
        const hit = t(key);
        if (hit && hit !== key) text = hit;
      }
    } catch (err) { /* i18n not ready in some harnesses */ }
    if (vars) {
      Object.keys(vars).forEach(k => {
        text = text.split("{" + k + "}").join(String(vars[k]));
      });
    }
    return text;
  }
  function escHTML(value) {
    if (typeof esc === "function") return esc(value);
    return String(value == null ? "" : value)
      .replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  }
  function sigLabel(key) {
    try { if (typeof sigName === "function") return sigName(key); } catch (err) { /* noop */ }
    return String(key == null ? "" : key);
  }
  function valueLabel(key, value) {
    try {
      if (typeof valLabel === "function") {
        const label = valLabel(key, value);
        if (label) return label;
      }
    } catch (err) { /* noop */ }
    return String(value == null ? "" : value);
  }
  function isRuleSignal(key) {
    try { if (typeof isRuleSig === "function") return isRuleSig(key); } catch (err) { /* noop */ }
    return false;
  }
  function reasonLabel(effort) {
    if (!effort) return "";
    try {
      if (typeof t === "function") {
        const hit = t("reason." + effort);
        if (hit && hit !== "reason." + effort) return hit;
      }
    } catch (err) { /* noop */ }
    return String(effort);
  }

  /* ── data helpers (read-only) ────────────────────────────────────────── */

  function isTerminal(node) {
    return !!node && (node.type === "route" || node.type === "block");
  }
  function canHaveWhen(node, context) {
    return !!node && !(context && (context.isRoot || context.isOtherwise));
  }
  function modelRowOf(modelsById, modelId) {
    if (!modelsById || !modelId) return null;
    if (typeof modelsById.get === "function") return modelsById.get(modelId) || null;
    return modelsById[modelId] || null;
  }
  function capabilityOf(modelsById, modelId) {
    const row = modelRowOf(modelsById, modelId);
    if (!row) return {text: "", missing: "model"};
    const text = typeof row.capability === "string" ? row.capability.trim() : "";
    return {text: text, missing: text ? "" : "capability"};
  }
  function nodeTypeLabel(type) {
    return tr("tree." + type) !== "tree." + type ? tr("tree." + type) : String(type);
  }
  function whenText(when) {
    if (!when) return "";
    return sigLabel(when.signal) + " " + when.op + " " +
      valueLabel(when.signal, when.value);
  }

  /* ── attribute/action helpers ────────────────────────────────────────── */

  const kebab = name => "data-" + String(name).replace(/[A-Z]/g, c => "-" + c.toLowerCase());
  function attrs(map) {
    return Object.keys(map || {}).map(key => {
      const value = map[key];
      if (value === undefined || value === null || value === false) return "";
      return " " + kebab(key) + '="' + escHTML(value) + '"';
    }).join("");
  }
  function button(action, map, label, title, cls) {
    const aria = escHTML(title || label);
    return '<button type="button" class="rt-act' + (cls ? " " + cls : "") +
      '" data-rt-action="' + action + '"' + attrs(map) +
      ' title="' + aria + '" aria-label="' + aria + '">' + escHTML(label) + "</button>";
  }
  const iconButton = (action, map, glyph, title, cls) =>
    button(action, map, glyph, title, (cls || "") + " rt-icon");

  /* ── chips ───────────────────────────────────────────────────────────── */

  function whenChipHTML(node) {
    const when = node.when;
    let text = '<span class="rt-when-label">' + escHTML(sigLabel(when.signal)) + "</span>" +
      '<span class="rt-when-op">' + escHTML(when.op) + "</span>" +
      '<span class="rt-when-value">' + escHTML(valueLabel(when.signal, when.value)) + "</span>";
    if (when.conf_min != null) {
      text += '<span class="rt-when-conf" data-when-conf="' + escHTML(when.conf_min) + '">' +
        escHTML(tr("tree.confidence")) + Number(when.conf_min).toFixed(2) + "</span>";
    } else if (isRuleSignal(when.signal)) {
      text += '<span class="rt-when-det">' + escHTML(tr("tree.deterministic")) + "</span>";
    }
    return '<button type="button" class="rt-act rt-when" data-rt-action="edit-when"' +
      attrs({nodeId: node.id}) +
      attrs({"when-signal": when.signal, "when-op": when.op, "when-value": when.value}) +
      ' title="' + escHTML(tr("tree.editWhen")) + '" aria-label="' +
      escHTML(tr("tree.editWhen")) + '">' + text + "</button>";
  }
  function whenZoneHTML(node, context) {
    if (!canHaveWhen(node, context)) return "";
    const inner = node.when ? whenChipHTML(node)
      : button("edit-when", {nodeId: node.id}, tr("tree.when.add"), tr("tree.when.add"),
               "rt-when rt-when-add");
    return '<span class="rt-when-zone" data-rt-drop="when"' + attrs({nodeId: node.id}) + ">" +
      inner + "</span>";
  }

  function weightChipHTML(node, weight, index) {
    return button("edit-weight", {nodeId: node.id, weightIndex: index},
      sigLabel(weight.signal) + " · " + valueLabel(weight.signal, weight.value) +
        " ×" + Number(weight.weight),
      tr("tree.weights") + " " + index, "rt-weight");
  }

  /* ── node card ───────────────────────────────────────────────────────── */

  function nodeHeadHTML(node, options, context) {
    const parts = ['<div class="rt-node-head">'];
    parts.push('<span class="rt-type" data-type="' + escHTML(node.type) + '">' +
      escHTML(nodeTypeLabel(node.type)) + "</span>");
    if (context.isRoot) {
      parts.push('<span class="rt-root-tag">' + escHTML(tr("tree.root")) + "</span>");
    } else {
      parts.push(whenZoneHTML(node, context));
    }
    parts.push('<span class="rt-node-id">' + escHTML(node.id) + "</span>");
    parts.push('<span class="rt-node-actions">');
    parts.push(iconButton("edit-node", {nodeId: node.id}, "✎", tr("tree.editNode")));
    if (!context.isRoot && !context.isOtherwise) {
      parts.push(iconButton("remove-node", {nodeId: node.id}, "−", tr("tree.removeNode")));
    }
    parts.push("</span></div>");
    return parts.join("");
  }

  function weightsHTML(node) {
    const weights = Array.isArray(node.weights) ? node.weights : [];
    const chips = weights.map((weight, index) => weightChipHTML(node, weight, index)).join("");
    let min = "";
    if (node.min_score != null) {
      min = '<span class="rt-min-score" data-min-score="' + escHTML(node.min_score) + '">' +
        escHTML(tr("tree.minScore")) + " " + escHTML(node.min_score) + "</span>";
    }
    return '<span class="rt-weights" data-rt-drop="weights"' + attrs({nodeId: node.id}) + ">" +
      chips + min +
      iconButton("add-weight", {nodeId: node.id}, "＋", tr("tree.addWeight"), "rt-add-weight") +
      "</span>";
  }

  function outcomeHTML(node, options) {
    if (node.type === "block") {
      return '<span class="rt-outcome" data-outcome="block">' +
        escHTML(tr("tree.block")) + "</span>";
    }
    let html = '<span class="rt-outcome" data-outcome="route"' +
      attrs({"model-id": node.model}) + ">" +
      '<span class="rt-model">' + escHTML(node.model) + "</span>";
    if (node.reasoning) {
      html += '<span class="rt-reason" data-reasoning="' + escHTML(node.reasoning) + '">' +
        escHTML(tr("tree.reasoning")) + " " + escHTML(reasonLabel(node.reasoning)) + "</span>";
    }
    return html + "</span>";
  }

  function terminalBodyHTML(node, options) {
    return '<div class="rt-node-body rt-terminal-body">' + outcomeHTML(node, options) +
      weightsHTML(node) + "</div>";
  }

  function nlChoiceBodyHTML(node, options) {
    const parts = ['<div class="rt-node-body">'];
    parts.push('<p class="rt-instructions">' + escHTML(node.instructions || "") + "</p>");
    parts.push('<p class="rt-thresholds"' +
      attrs({"min-prob": node.min_prob, "min-margin": node.min_margin}) + ">" +
      escHTML(tr("tree.minProb")) + " " + escHTML(node.min_prob) + " · " +
      escHTML(tr("tree.minMargin")) + " " + escHTML(node.min_margin) + "</p>");
    parts.push(optionsHTML(node, options));
    parts.push(optionAddRowHTML(node));
    parts.push(otherwiseHTML(node, options, {isRoot: false, isOtherwise: false}));
    return parts.join("") + "</div>";
  }

  function branchBodyHTML(node, options) {
    const refs = childRefs(node).filter(ref => ref.edgeKind === "children");
    const parts = ['<div class="rt-node-body">'];
    parts.push('<div class="rt-children"' + attrs({"parent-id": node.id}) + ">" +
      (refs.length ? refs.map(ref => edgeHTML(ref, options)).join("")
        : '<div class="rt-empty">' + escHTML(tr("tree.emptyChildren")) + "</div>") +
      "</div>");
    parts.push('<div class="rt-action-row">' +
      button("add-child", {nodeId: node.id}, tr("tree.addChild"), tr("tree.addChild")) +
      "</div>");
    parts.push(otherwiseHTML(node, options, {isRoot: false, isOtherwise: false}));
    return parts.join("") + "</div>";
  }

  function optionsHTML(node, options) {
    const refs = childRefs(node).filter(ref => ref.edgeKind === "optionChild");
    if (!refs.length) {
      return '<div class="rt-options"' + attrs({"parent-id": node.id}) + ">" +
        '<div class="rt-empty">' + escHTML(tr("tree.emptyOptions")) + "</div></div>";
    }
    return '<div class="rt-options"' + attrs({"parent-id": node.id}) + ">" +
      refs.map(ref => edgeHTML(ref, options)).join("") + "</div>";
  }

  function optionAddRowHTML(node) {
    return '<div class="rt-action-row">' +
      button("add-option", {nodeId: node.id, optionKind: "model"},
        tr("tree.addOption.model"), tr("tree.addOption.model")) +
      button("add-option", {nodeId: node.id, optionKind: "child"},
        tr("tree.addOption.child"), tr("tree.addOption.child")) +
      "</div>";
  }

  function otherwiseHTML(node, options, context) {
    if (isTerminal(node)) return "";
    const ref = childRefs(node).find(item => item.edgeKind === "otherwise");
    if (!ref) {
      return '<div class="rt-otherwise-row">' +
        button("add-otherwise", {nodeId: node.id}, tr("tree.addOtherwise"),
               tr("tree.addOtherwise"), "rt-add-otherwise") +
        "</div>";
    }
    return '<div class="rt-otherwise-row"><div class="rt-edge rt-edge-otherwise"' +
      attrs({"edge-kind": "otherwise", "parent-id": node.id}) + ">" +
      '<div class="rt-edge-head">' +
      '<span class="rt-edge-label">' + escHTML(tr("tree.otherwise")) + "</span>" +
      iconButton("remove-otherwise", {nodeId: node.id}, "−", tr("tree.removeOtherwise")) +
      "</div>" + nodeHTML(ref.node, options, {isRoot: false, isOtherwise: true}) +
      "</div></div>";
  }

  /* ── edges (one place that knows every out-edge kind) ────────────────── */

  function moveButtonsHTML(ref) {
    return iconButton("move-node", {nodeId: ref.node.id, direction: -1, edgeIndex: ref.index},
      "↑", tr("tree.moveUp")) +
      iconButton("move-node", {nodeId: ref.node.id, direction: 1, edgeIndex: ref.index},
      "↓", tr("tree.moveDown"));
  }
  function dragHandleHTML(ref) {
    return '<button type="button" class="rt-act rt-drag" data-rt-drag="node"' +
      attrs({nodeId: ref.node.id}) +
      ' draggable="false" title="' + escHTML(tr("tree.drag")) + '" aria-label="' +
      escHTML(tr("tree.drag")) + '">⋮⋮</button>';
  }

  function childrenEdgeHTML(ref, options) {
    const sortable = ref.parent.children.length > 1;
    return '<div class="rt-edge rt-edge-children"' +
      attrs({"edge-kind": "children", "edge-index": ref.index,
             "parent-id": ref.parent.id, "rt-sortable": sortable ? "1" : false}) + ">" +
      '<div class="rt-edge-head">' + dragHandleHTML(ref) + moveButtonsHTML(ref) + "</div>" +
      nodeHTML(ref.node, options, {isRoot: false, isOtherwise: false}) + "</div>";
  }

  function modelOptionEdgeHTML(ref, options) {
    const option = ref.parent.options[ref.index];
    const capability = capabilityOf(options.modelsById, option.model);
    const sortable = ref.parent.options.length > 1;
    const capText = capability.missing === "model"
      ? tr("tree.modelMissing") : capability.missing === "capability"
        ? tr("tree.capabilityMissing") : capability.text;
    return '<div class="rt-edge rt-edge-option rt-edge-model"' +
      attrs({"edge-kind": "optionChild", "option-kind": "model",
             "option-key": option.key, "parent-id": ref.parent.id,
             "rt-sortable": sortable ? "1" : false}) + ">" +
      '<div class="rt-edge-head">' + dragHandleHTML(ref) + moveButtonsHTML(ref) +
      '<span class="rt-option-key" data-option-key="' + escHTML(option.key) + '">' +
      escHTML(tr("tree.directModel")) + " · " + escHTML(option.key) + "</span>" +
      '<span class="rt-model"' +
      (capability.missing === "model" ? ' data-missing="model"' : "") + ">" +
      escHTML(option.model) + "</span>" +
      (option.reasoning ? '<span class="rt-reason" data-reasoning="' +
        escHTML(option.reasoning) + '">' + escHTML(reasonLabel(option.reasoning)) + "</span>" : "") +
      '<span class="rt-capability"' +
      (capability.missing ? ' data-missing="' + escHTML(capability.missing) + '"' : "") +
      ' title="' + escHTML(capVersion(capability, option.model)) + '">' +
      escHTML(capText) + "</span>" +
      iconButton("edit-option", {nodeId: ref.parent.id, optionKey: option.key},
                 "✎", tr("tree.editOption")) +
      iconButton("remove-option", {nodeId: ref.parent.id, optionKey: option.key},
                 "−", tr("tree.removeOption")) +
      "</div></div>";
  }
  function capVersion(capability, modelId) {
    if (capability.missing === "model") return tr("tree.modelMissing") + ": " + modelId;
    if (capability.missing === "capability") return tr("tree.capabilityMissing") + ": " + modelId;
    return capability.text;
  }

  function childOptionEdgeHTML(ref, options) {
    const option = ref.parent.options[ref.index];
    const sortable = ref.parent.options.length > 1;
    return '<div class="rt-edge rt-edge-option rt-edge-child"' +
      attrs({"edge-kind": "optionChild", "option-kind": "child",
             "option-key": option.key, "parent-id": ref.parent.id,
             "rt-sortable": sortable ? "1" : false}) + ">" +
      '<div class="rt-edge-head">' + dragHandleHTML(ref) + moveButtonsHTML(ref) +
      '<span class="rt-option-key" data-option-key="' + escHTML(option.key) + '">' +
      escHTML(tr("tree.childTarget")) + " · " + escHTML(option.key) + "</span>" +
      '<span class="rt-option-desc">' + escHTML(option.description) + "</span>" +
      iconButton("edit-option", {nodeId: ref.parent.id, optionKey: option.key},
                 "✎", tr("tree.editOption")) +
      iconButton("remove-option", {nodeId: ref.parent.id, optionKey: option.key},
                 "−", tr("tree.removeOption")) +
      "</div>" + nodeHTML(ref.node, options, {isRoot: false, isOtherwise: false}) + "</div>";
  }

  function edgeHTML(ref, options) {
    if (ref.edgeKind === "children") return childrenEdgeHTML(ref, options);
    if (ref.edgeKind === "optionChild") {
      return ref.parent.options[ref.index].child == null
        ? modelOptionEdgeHTML(ref, options) : childOptionEdgeHTML(ref, options);
    }
    return ""; // otherwise is rendered by otherwiseHTML (its own labelled row)
  }

  /* ── public render ───────────────────────────────────────────────────── */

  let currentOptions = {modelsById: {}};
  function childRefs(node) {
    if (typeof SRTreeModel === "undefined" || !SRTreeModel ||
        typeof SRTreeModel.childRefs !== "function") {
      throw new Error("SRTreeView: SRTreeModel not loaded");
    }
    return SRTreeModel.childRefs(node);
  }

  function nodeHTML(node, options, context) {
    const opts = options || currentOptions;
    const ctx = context || {isRoot: false, isOtherwise: false};
    const body = isTerminal(node) ? terminalBodyHTML(node, opts)
      : node.type === "nl_choice" ? nlChoiceBodyHTML(node, opts)
        : branchBodyHTML(node, opts);
    return '<div class="rt-node" role="treeitem"' +
      attrs({"node-id": node.id, "node-type": node.type,
             "node-role": ctx.isRoot ? "root" : (ctx.isOtherwise ? "otherwise" : null)}) + ">" +
      nodeHeadHTML(node, opts, ctx) + body + "</div>";
  }

  function render(container, options) {
    if (!container || typeof container !== "object") {
      throw new Error("SRTreeView.render: container required");
    }
    const opts = options || {};
    const root = opts.root;
    if (!root || typeof root !== "object" || !root.id) {
      throw new Error("SRTreeView.render: root node required");
    }
    currentOptions = opts;
    container.innerHTML = '<div class="rt-tree" role="tree"' +
      attrs({"root-id": root.id}) + ">" +
      nodeHTML(root, opts, {isRoot: true, isOtherwise: false}) + "</div>";
    if (opts.selectedNodeId) {
      const cards = container.querySelectorAll("[data-node-id][data-node-type]");
      Array.prototype.forEach.call(cards, card => {
        if (card.getAttribute("data-node-id") === opts.selectedNodeId) {
          card.classList.add("is-selected");
        }
      });
    }
    return container;
  }

  return {
    render: render,
    nodeHTML: function (node, options) {
      return nodeHTML(node, options || currentOptions, {isRoot: false, isOtherwise: false});
    },
    edgeHTML: edgeHTML,
    whenText: whenText,
    nodeTypeLabel: nodeTypeLabel,
    isTerminal: isTerminal,
    canHaveWhen: canHaveWhen,
    capabilityOf: function (modelsById, modelId) {
      return capabilityOf(modelsById, modelId);
    },
  };
})();
