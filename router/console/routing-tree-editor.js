/* Unified routing-tree editor forms (console rewrite, task-13 pre-work).

   HTML builders + pure data helpers for the four node types. The module owns
   *forms*, not the popover/DOM lifecycle: the page mounts `…FormHTML()` output,
   reads it back with `read…Form()` (or `readFormValues()`), then applies the
   plain values with `apply…Form()`, which returns `{node, issues}`. Issues use
   the backend `Issue` shape `{code, zh, en, node_id, level}` so the caller can
   render local and 422 findings with one component. Error display is the
   caller's job; the editor never touches the popover.

   Field contract (all within the form scope, no global ids):
     when      [data-field="when.signal|value|op|conf_min"] inside [data-when-form]
     node      [data-field="node.model|reasoning|instructions|min_prob|min_margin|min_score"]
     weights   [data-weight-row] rows with [data-weight-field="signal|value|weight"]
     options   [data-option-form][data-option-kind="model|child"] with
               [data-option-field="key|model|reasoning|description"]
     actions   [data-rt-form-action="add-weight|remove-option|add-option"] (+ data attrs)
   Callback actions in the tree itself (add child / otherwise / remove …) are
   rendered by SRTreeView; this module only builds the edit forms.

   `when.op` is never user-typed: it is derived from `SignalSpec.multi`
   ("contains" for multi-value signals, "eq" otherwise), matching the backend
   contract in router/pipeline/validation.py.

   Browser-only (DOM helpers are scope-local; nothing is bound globally);
   loaded after routing-tree-model.js and shared.js. */
"use strict";

const SRTreeEditor = (function () {
  const DEFAULT_WEIGHT = 0.5;
  const DEFAULT_MIN_SCORE = 0.5;
  const DEFAULT_MIN_PROB = 0.45;
  const DEFAULT_MIN_MARGIN = 0.1;

  const FALLBACK = {
    "tree.signal": "信号",
    "tree.value": "取值",
    "tree.value.placeholder": "取值",
    "tree.confMin": "置信下限",
    "tree.clearCondition": "移除条件",
    "tree.model": "模型",
    "tree.reasoning": "推理档位",
    "tree.reasonAuto": "透传",
    "tree.weights": "加权条件",
    "tree.addWeight": "＋ 加权条件",
    "tree.minScore": "达成率门槛",
    "tree.instructions": "节点问题",
    "tree.minProb": "最高候选概率门槛",
    "tree.minMargin": "前两名概率差门槛",
    "tree.options": "候选（按顺序参与选择）",
    "tree.directModel": "直接模型",
    "tree.childTarget": "子分支",
    "tree.optionKey": "候选 key",
    "tree.optionDescription": "分支说明",
    "tree.optionModel": "候选模型",
    "tree.capability": "能力说明",
    "tree.capabilityMissing": "未配置能力说明",
    "tree.modelMissing": "模型目录缺失",
    "tree.addOption.model": "＋ 模型候选",
    "tree.addOption.child": "＋ 子候选",
    "tree.removeOption": "删除候选",
    "tree.removeWeight": "删除加权条件",
    "tree.branchNote": "普通分支没有专属字段；子节点与 otherwise 在树中增删。",
    "tree.childTargetNote": "子分支的节点内容在树中展开编辑，这里只配置候选 key 与说明。",
    "tree.noSignal": "未设置条件",
  };
  function tr(key, vars) {
    let text = FALLBACK[key] != null ? FALLBACK[key] : key;
    try {
      if (typeof t === "function") {
        const hit = t(key);
        if (hit && hit !== key) text = hit;
      }
    } catch (err) { /* i18n not ready */ }
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
  const kebab = name => "data-" + String(name).replace(/[A-Z]/g, c => "-" + c.toLowerCase());

  function issue(code, nodeId, zh, en, level) {
    return {code: code, zh: zh, en: en || zh, node_id: nodeId || "", level: level || "hard"};
  }
  function hardIssues(issues) { return issues.some(item => item.level !== "warn"); }

  /* ── signals ─────────────────────────────────────────────────────────── */

  function signalSpecOf(signals, key) {
    if (!signals) return null;
    if (Array.isArray(signals)) return signals.find(spec => spec && spec.key === key) || null;
    return signals[key] || null;
  }
  function signalList(signals) {
    if (!signals) return [];
    if (Array.isArray(signals)) return signals.filter(Boolean);
    return Object.keys(signals).map(key => signals[key]).filter(Boolean);
  }
  function opForSignal(signals, signalKey) {
    const spec = signalSpecOf(signals, signalKey);
    return spec && spec.multi ? "contains" : "eq";
  }
  const isRuleSignal = spec => !!spec && spec.kind === "rule";
  function signalValues(spec) {
    return (spec && Array.isArray(spec.values) ? spec.values : []).map(entry =>
      Array.isArray(entry) ? entry : [entry.key, entry.zh, entry.en]);
  }
  function isFreeSignal(spec) {
    return !spec || spec.free === true || !signalValues(spec).length;
  }

  /* ── when form ───────────────────────────────────────────────────────── */

  function optionTag(value, label, selected) {
    return '<option value="' + escHTML(value) + '"' +
      (String(selected) === String(value) ? " selected" : "") + ">" + escHTML(label) + "</option>";
  }

  function valueControlHTML(spec, value) {
    if (isFreeSignal(spec)) {
      return '<input type="text" data-field="when.value" value="' +
        escHTML(value == null ? "" : value) + '" placeholder="' +
        escHTML(tr("tree.value.placeholder")) + '">';
    }
    const options = signalValues(spec).map(entry =>
      optionTag(entry[0], valueLabel(spec.key, entry[0]) || entry[1], value)).join("");
    return '<select data-field="when.value">' + optionTag("", tr("tree.value"), value === "" || value == null ? "" : null) +
      options + "</select>";
  }

  function whenFormHTML(input) {
    const opts = input || {};
    const spec = signalSpecOf(opts.signals, opts.when && opts.when.signal);
    const when = opts.when || null;
    const op = when ? (when.op || opForSignal(opts.signals, when.signal)) : "eq";
    const signals = signalList(opts.signals);
    const signalOptions = optionTag("", tr("tree.noSignal"), when ? when.signal : "") +
      signals.map(specRow => optionTag(specRow.key, sigLabel(specRow.key),
        when ? when.signal : "")).join("");
    const confFloor = spec && !isRuleSignal(spec)
      ? '<label class="rt-field"><span>' + escHTML(tr("tree.confMin")) + "</span>" +
        '<input type="number" min="0" max="1" step="0.01" data-field="when.conf_min" value="' +
        (when && when.conf_min != null ? escHTML(when.conf_min) : "") + '"></label>'
      : "";
    return '<div class="rt-form rt-when-form" data-when-form>' +
      '<label class="rt-field"><span>' + escHTML(tr("tree.signal")) + "</span>" +
      '<select data-field="when.signal">' + signalOptions + "</select></label>" +
      '<label class="rt-field"><span>' + escHTML(tr("tree.value")) + "</span>" +
      valueControlHTML(spec, when ? when.value : "") + "</label>" +
      '<input type="hidden" data-field="when.op" value="' + escHTML(op) + '">' +
      confFloor +
      '<button type="button" class="rt-act" data-when-action="clear" data-field="when.clear">' +
      escHTML(tr("tree.clearCondition")) + "</button>" +
      "</div>";
  }

  function readField(scope, name) {
    const el = scope && scope.querySelector('[data-field="' + name + '"]');
    return el ? el.value : undefined;
  }
  function readWhenForm(scope) {
    const signal = readField(scope, "when.signal");
    const value = readField(scope, "when.value");
    const conf = readField(scope, "when.conf_min");
    const op = readField(scope, "when.op");
    const out = {signal: signal === undefined ? "" : signal,
                 value: value === undefined ? "" : value};
    if (op !== undefined && op !== "") out.op = op;
    if (conf !== undefined && conf !== "") {
      const number = Number(conf);
      out.conf_min = Number.isFinite(number) ? number : null;
    }
    return out;
  }

  function readFormValues(scope) {
    const values = {};
    if (!scope || !scope.querySelectorAll) return values;
    Array.prototype.forEach.call(scope.querySelectorAll("[data-field]"), el => {
      values[el.getAttribute("data-field")] = el.value;
    });
    return values;
  }

  function applyWhenForm(node, values, options) {
    const opts = options || {};
    const context = opts.context || {};
    const issues = [];
    const input = values || {};
    if (context.isRoot || context.isOtherwise) {
      issues.push(issue("when_not_allowed", node.id,
        "根节点 / otherwise 节点不能配置 when 条件",
        "The root / otherwise node cannot carry a when condition"));
      return {node: node, issues: issues};
    }
    const signal = typeof input.signal === "string" ? input.signal.trim() : "";
    if (!signal) {
      if (node.when == null) return {node: node, issues: issues};
      return {node: Object.assign({}, node, {when: null}), issues: issues};
    }
    const spec = signalSpecOf(opts.signals, signal);
    if (!spec) {
      issues.push(issue("unknown_signal", node.id, "未知信号 " + signal,
        "Unknown signal " + signal));
      return {node: node, issues: issues};
    }
    const value = String(input.value == null ? "" : input.value).trim();
    if (!value) {
      issues.push(issue("missing_value", node.id, "条件缺少取值", "Condition has no value"));
    } else if (!isFreeSignal(spec) &&
               !signalValues(spec).some(entry => String(entry[0]) === value)) {
      issues.push(issue("unknown_signal_value", node.id,
        "信号 " + signal + " 的取值 " + value + " 不在可选范围内",
        "Value " + value + " is not valid for signal " + signal));
    }
    const when = {signal: signal, value: value, op: opForSignal(opts.signals, signal)};
    if (input.conf_min !== undefined && input.conf_min !== null && input.conf_min !== "") {
      const conf = Number(input.conf_min);
      if (!Number.isFinite(conf) || conf < 0 || conf > 1) {
        issues.push(issue("invalid_threshold", node.id,
          "置信门槛 conf_min 必须是 [0,1] 内的数值",
          "The confidence floor must be a number in [0,1]"));
      } else if (isRuleSignal(spec)) {
        issues.push(issue("rule_signal_floor", node.id,
          "规则信号 " + signal + " 是确定性的，置信门槛不会生效",
          "Rule signal " + signal + " is deterministic; the confidence floor never applies",
          "warn"));
      } else {
        when.conf_min = conf;
      }
    }
    if (hardIssues(issues)) return {node: node, issues: issues};
    return {node: Object.assign({}, node, {when: when}), issues: issues};
  }

  /* ── reasoning / model helpers ───────────────────────────────────────── */

  function modelRowOf(modelsById, modelId) {
    if (!modelsById || !modelId) return null;
    if (typeof modelsById.get === "function") return modelsById.get(modelId) || null;
    return modelsById[modelId] || null;
  }
  function modelIds(modelsById) {
    if (!modelsById) return [];
    if (typeof modelsById.keys === "function") return Array.from(modelsById.keys());
    return Object.keys(modelsById);
  }
  function reasoningEfforts(modelsById, modelId) {
    const row = modelRowOf(modelsById, modelId);
    const reasoning = row && row.reasoning;
    if (!reasoning || !reasoning.declared || reasoning.supported === false) return [];
    return Array.isArray(reasoning.efforts) ? reasoning.efforts : [];
  }
  function effortLabel(effort) {
    if (!effort) return "";
    if (effort.zh) return effort.zh;
    if (effort.en) return effort.en;
    return String(effort.id);
  }
  function reasoningOptionsHTML(modelId, modelsById, selected) {
    const efforts = reasoningEfforts(modelsById, modelId);
    const current = selected == null ? "" : String(selected);
    let html = optionTag("", tr("tree.reasonAuto"), current);
    let known = current === "";
    efforts.forEach(effort => {
      if (String(effort.id) === current) known = true;
      html += optionTag(effort.id, effortLabel(effort), current);
    });
    if (!known) {
      html += optionTag(current, current + " ⚠", current);
    }
    return html;
  }
  function capabilityOf(modelsById, modelId) {
    const row = modelRowOf(modelsById, modelId);
    if (!row) return {text: "", missing: "model"};
    const text = typeof row.capability === "string" ? row.capability.trim() : "";
    return {text: text, missing: text ? "" : "capability"};
  }

  /* ── weights ─────────────────────────────────────────────────────────── */

  function weightValueControlHTML(signals, signal, value) {
    const spec = signalSpecOf(signals, signal);
    if (isFreeSignal(spec)) {
      return '<input type="text" data-weight-field="value" value="' +
        escHTML(value == null ? "" : value) + '" placeholder="' +
        escHTML(tr("tree.value.placeholder")) + '">';
    }
    return '<select data-weight-field="value">' +
      optionTag("", tr("tree.value"), value === "" || value == null ? "" : null) +
      signalValues(spec).map(entry =>
        optionTag(entry[0], valueLabel(spec.key, entry[0]) || entry[1], value)).join("") +
      "</select>";
  }

  function weightRowHTML(input) {
    const opts = input || {};
    const weight = opts.weight || {signal: "", value: "", weight: DEFAULT_WEIGHT};
    const signals = signalList(opts.signals);
    const signalOptions = optionTag("", tr("tree.signal"), weight.signal) +
      signals.map(spec => optionTag(spec.key, sigLabel(spec.key), weight.signal)).join("");
    return '<div class="rt-weight-row" data-weight-row>' +
      '<select data-weight-field="signal">' + signalOptions + "</select>" +
      weightValueControlHTML(opts.signals, weight.signal, weight.value) +
      '<input type="number" min="0" max="1" step="0.05" data-weight-field="weight" value="' +
      escHTML(weight.weight == null ? DEFAULT_WEIGHT : weight.weight) + '">' +
      '<button type="button" class="rt-act" data-rt-form-action="remove-weight"' +
      (opts.index == null ? "" : ' data-weight-index="' + escHTML(opts.index) + '"') +
      ' title="' + escHTML(tr("tree.removeWeight")) + '">−</button>' +
      "</div>";
  }

  function readWeightRow(row) {
    const read = name => {
      const el = row.querySelector('[data-weight-field="' + name + '"]');
      return el ? el.value : "";
    };
    const weight = Number(read("weight"));
    return {signal: read("signal"), value: read("value"),
            weight: Number.isFinite(weight) ? weight : DEFAULT_WEIGHT};
  }

  function addWeight(node, input) {
    const opts = input || {};
    const issues = [];
    if (!node || (node.type !== "route" && node.type !== "block")) {
      issues.push(issue("not_terminal", node && node.id,
        "只有 route/block 终止节点能配置加权条件",
        "Only a route/block terminal can carry weighted conditions"));
      return {node: node, issues: issues};
    }
    const signal = typeof opts.signal === "string" ? opts.signal.trim() : "";
    if (!signal) {
      issues.push(issue("missing_value", node.id, "加权条件缺少信号", "Weighted condition has no signal"));
      return {node: node, issues: issues};
    }
    const spec = signalSpecOf(opts.signals, signal);
    if (!spec) {
      issues.push(issue("unknown_signal", node.id, "未知信号 " + signal, "Unknown signal " + signal));
      return {node: node, issues: issues};
    }
    const value = String(opts.value == null ? "" : opts.value).trim();
    if (!value) {
      issues.push(issue("missing_value", node.id, "加权条件缺少取值", "Weighted condition has no value"));
    } else if (!isFreeSignal(spec) &&
               !signalValues(spec).some(entry => String(entry[0]) === value)) {
      issues.push(issue("unknown_signal_value", node.id,
        "信号 " + signal + " 的取值 " + value + " 不在可选范围内",
        "Value " + value + " is not valid for signal " + signal));
    }
    const weights = Array.isArray(node.weights) ? node.weights : [];
    if (weights.some(item => item.signal === signal && item.value === value)) {
      issues.push(issue("duplicate_weight", node.id, "加权条件重复：信号与取值已存在",
        "Duplicate weighted condition"));
    }
    const weight = opts.weight == null ? DEFAULT_WEIGHT : Number(opts.weight);
    if (!Number.isFinite(weight) || weight < 0 || weight > 1) {
      issues.push(issue("invalid_threshold", node.id,
        "加权条件的权重必须是 [0,1] 内的数值",
        "A weight must be a number in [0,1]"));
    }
    if (hardIssues(issues)) return {node: node, issues: issues};
    const minScore = node.min_score == null ? DEFAULT_MIN_SCORE : node.min_score;
    return {
      node: Object.assign({}, node, {weights: weights.concat([{signal: signal, value: value, weight: weight}]),
        min_score: minScore}),
      issues: issues,
    };
  }

  function weightsSectionHTML(node, signals) {
    const weights = Array.isArray(node.weights) ? node.weights : [];
    const rows = weights.map((weight, index) =>
      weightRowHTML({weight: weight, signals: signals, index: index})).join("");
    return '<div class="rt-form-section rt-weights-form" data-weights-form>' +
      '<p class="rt-form-label">' + escHTML(tr("tree.weights")) + "</p>" + rows +
      '<button type="button" class="rt-act" data-rt-form-action="add-weight">' +
      escHTML(tr("tree.addWeight")) + "</button>" +
      '<label class="rt-field"><span>' + escHTML(tr("tree.minScore")) + "</span>" +
      '<input type="number" min="0" max="1" step="0.05" data-field="node.min_score" value="' +
      (node.min_score == null ? "" : escHTML(node.min_score)) + '"></label>' +
      "</div>";
  }

  /* ── option forms ────────────────────────────────────────────────────── */

  function optionFormHTML(input) {
    const opts = input || {};
    const option = opts.option || {};
    const kind = opts.kind || (option.child != null ? "child" : "model");
    const map = opts.index == null ? {} : {"option-index": opts.index};
    const head = '<div class="rt-option-form" data-option-form' +
      ' data-option-kind="' + escHTML(kind) + '"' +
      (option.key != null ? ' data-option-key="' + escHTML(option.key) + '"' : "") +
      Object.keys(map).map(key => " " + kebab(key) + '="' + escHTML(map[key]) + '"').join("") + ">";
    let body = '<label class="rt-field"><span>' + escHTML(tr("tree.optionKey")) + "</span>" +
      '<input type="text" data-option-field="key" value="' +
      escHTML(option.key == null ? "" : option.key) + '"></label>';
    if (kind === "model") {
      const capabilities = capabilityOf(opts.modelsById, option.model);
      const rows = modelIds(opts.modelsById).map(id =>
        optionTag(id, id, option.model)).join("");
      const capText = capabilities.missing === "model" ? tr("tree.modelMissing")
        : capabilities.missing === "capability" ? tr("tree.capabilityMissing")
          : capabilities.text;
      body += '<label class="rt-field"><span>' + escHTML(tr("tree.optionModel")) + "</span>" +
        '<select data-option-field="model">' + optionTag("", tr("tree.model"), option.model) +
        rows + "</select></label>" +
        '<label class="rt-field"><span>' + escHTML(tr("tree.reasoning")) + "</span>" +
        '<select data-option-field="reasoning">' +
        reasoningOptionsHTML(option.model, opts.modelsById, option.reasoning) + "</select></label>" +
        '<p class="rt-cap-preview" data-capability="' + escHTML(capabilities.text) + '"' +
        (capabilities.missing ? ' data-missing="' + escHTML(capabilities.missing) + '"' : "") +
        ">" + escHTML(tr("tree.capability")) + "：" + escHTML(capText) + "</p>";
    } else {
      body += '<label class="rt-field"><span>' + escHTML(tr("tree.optionDescription")) + "</span>" +
        '<textarea data-option-field="description" rows="2">' +
        escHTML(option.description == null ? "" : option.description) + "</textarea></label>" +
        '<p class="rt-form-note">' + escHTML(tr("tree.childTargetNote")) + "</p>";
    }
    body += '<button type="button" class="rt-act" data-rt-form-action="remove-option"' +
      ' data-option-key="' + escHTML(option.key == null ? "" : option.key) + '">' +
      escHTML(tr("tree.removeOption")) + "</button>";
    return head + body + "</div>";
  }

  function readOptionForm(scope) {
    const read = name => {
      const el = scope && scope.querySelector('[data-option-field="' + name + '"]');
      return el ? el.value : undefined;
    };
    const out = {};
    const key = read("key");
    if (key !== undefined) out.key = key;
    const model = read("model");
    if (model !== undefined) out.model = model;
    const reasoning = read("reasoning");
    if (reasoning !== undefined) out.reasoning = reasoning;
    const description = read("description");
    if (description !== undefined) out.description = description;
    return out;
  }

  function applyOptionForm(currentOption, values, options) {
    const opts = options || {};
    const input = values || {};
    const kind = opts.kind || (currentOption && currentOption.child != null ? "child" : "model");
    const issues = [];
    const key = typeof input.key === "string" ? input.key.trim() : "";
    const taken = Array.isArray(opts.takenKeys) ? opts.takenKeys : [];
    const ownKey = currentOption && typeof currentOption.key === "string"
      ? currentOption.key : null;
    if (!key) {
      issues.push(issue("option_key_required", "", "候选需要非空 key",
        "A candidate needs a nonempty key"));
    } else if (key !== ownKey && taken.some(existing => existing === key)) {
      // takenKeys are the *other* candidates; keeping the current key is fine.
      issues.push(issue("duplicate_option_key", "",
        "候选 key 重复：" + key, "Duplicate candidate key: " + key));
    }
    if (kind === "model") {
      const model = typeof input.model === "string" ? input.model.trim() : "";
      if (!model) {
        issues.push(issue("option_model_required", "", "直接模型候选需要模型",
          "A direct-model candidate needs a model"));
      } else if (opts.modelsById && !modelRowOf(opts.modelsById, model)) {
        issues.push(issue("unknown_model", "", "模型目录中不存在：" + model,
          "Unknown model: " + model));
      }
      if (hardIssues(issues)) return {option: currentOption, issues: issues};
      return {
        option: {key: key, model: model,
                 reasoning: input.reasoning == null || input.reasoning === ""
                   ? null : String(input.reasoning)},
        issues: issues,
      };
    }
    const description = typeof input.description === "string" ? input.description.trim() : "";
    if (!description) {
      issues.push(issue("option_description_required", "",
        "child 候选需要非空分支说明", "A child candidate needs a nonempty description"));
    }
    if (hardIssues(issues)) return {option: currentOption, issues: issues};
    return {
      option: {key: key, description: description, child: currentOption && currentOption.child},
      issues: issues,
    };
  }

  /* ── node forms ──────────────────────────────────────────────────────── */

  function whenSectionHTML(node, options, context) {
    if (context && (context.isRoot || context.isOtherwise)) return "";
    return '<div class="rt-form-section">' +
      '<p class="rt-form-label">' + escHTML(tr("tree.signal")) + "</p>" +
      whenFormHTML({when: node.when, signals: options.signals}) + "</div>";
  }

  function routeFieldsHTML(node, options) {
    const capabilities = capabilityOf(options.modelsById, node.model);
    const models = modelIds(options.modelsById).map(id => optionTag(id, id, node.model)).join("");
    const capText = capabilities.missing === "model" ? tr("tree.modelMissing")
      : capabilities.missing === "capability" ? tr("tree.capabilityMissing")
        : capabilities.text;
    return '<div class="rt-form-section">' +
      '<label class="rt-field"><span>' + escHTML(tr("tree.model")) + "</span>" +
      '<select data-field="node.model">' + optionTag("", tr("tree.model"), node.model) +
      models + "</select></label>" +
      '<label class="rt-field"><span>' + escHTML(tr("tree.reasoning")) + "</span>" +
      '<select data-field="node.reasoning">' +
      reasoningOptionsHTML(node.model, options.modelsById, node.reasoning) + "</select></label>" +
      '<p class="rt-cap-preview" data-capability="' + escHTML(capabilities.text) + '"' +
      (capabilities.missing ? ' data-missing="' + escHTML(capabilities.missing) + '"' : "") +
      ">" + escHTML(capText) + "</p>" +
      weightsSectionHTML(node, options.signals) +
      "</div>";
  }

  function nlChoiceFieldsHTML(node, options) {
    const optionForms = (node.options || []).map((option, index) =>
      optionFormHTML({option: option, index: index, modelsById: options.modelsById})).join("");
    return '<div class="rt-form-section">' +
      '<label class="rt-field"><span>' + escHTML(tr("tree.instructions")) + "</span>" +
      '<textarea data-field="node.instructions" rows="3">' +
      escHTML(node.instructions == null ? "" : node.instructions) + "</textarea></label>" +
      '<label class="rt-field"><span>' + escHTML(tr("tree.minProb")) + "</span>" +
      '<input type="number" min="0" max="1" step="0.01" data-field="node.min_prob" value="' +
      escHTML(node.min_prob == null ? DEFAULT_MIN_PROB : node.min_prob) + '"></label>' +
      '<label class="rt-field"><span>' + escHTML(tr("tree.minMargin")) + "</span>" +
      '<input type="number" min="0" max="1" step="0.01" data-field="node.min_margin" value="' +
      escHTML(node.min_margin == null ? DEFAULT_MIN_MARGIN : node.min_margin) + '"></label>' +
      "</div>" +
      '<div class="rt-form-section"><p class="rt-form-label">' + escHTML(tr("tree.options")) + "</p>" +
      optionForms +
      '<div class="rt-action-row">' +
      '<button type="button" class="rt-act" data-rt-form-action="add-option" data-option-kind="model">' +
      escHTML(tr("tree.addOption.model")) + "</button>" +
      '<button type="button" class="rt-act" data-rt-form-action="add-option" data-option-kind="child">' +
      escHTML(tr("tree.addOption.child")) + "</button>" +
      "</div></div>";
  }

  function branchFieldsHTML() {
    return '<div class="rt-form-section"><p class="rt-form-note">' +
      escHTML(tr("tree.branchNote")) + "</p></div>";
  }

  function nodeFormHTML(input) {
    const opts = input || {};
    const node = opts.node;
    if (!node || !node.type) throw new Error("SRTreeEditor.nodeFormHTML: node required");
    const context = opts.context || {};
    let fields = "";
    if (node.type === "route") fields = routeFieldsHTML(node, opts);
    else if (node.type === "block") fields = weightsSectionHTML(node, opts.signals);
    else if (node.type === "nl_choice") fields = nlChoiceFieldsHTML(node, opts);
    else if (node.type === "branch") fields = branchFieldsHTML();
    else throw new Error("SRTreeEditor.nodeFormHTML: unknown node type " + node.type);
    return '<div class="rt-form rt-node-form" data-node-form data-node-type="' +
      escHTML(node.type) + '" data-node-id="' + escHTML(node.id) + '">' +
      whenSectionHTML(node, opts, context) + fields + "</div>";
  }

  function readNodeForm(scope) {
    const values = readFormValues(scope);
    const out = {};
    if (values["node.model"] !== undefined) out.model = values["node.model"];
    if (values["node.reasoning"] !== undefined) out.reasoning = values["node.reasoning"];
    if (values["node.instructions"] !== undefined) out.instructions = values["node.instructions"];
    ["min_prob", "min_margin", "min_score"].forEach(name => {
      const key = "node." + name;
      if (values[key] === undefined) return;
      const raw = values[key];
      if (raw === "") { out[name] = null; return; }
      const number = Number(raw);
      out[name] = Number.isFinite(number) ? number : raw;
    });
    /* A terminal form always renders [data-weights-form] (even with zero rows):
       an empty row set means "weights cleared", not "leave the old array". */
    const weightsForm = scope && scope.querySelector("[data-weights-form]");
    if (weightsForm) {
      out.weights = Array.prototype.map.call(
        weightsForm.querySelectorAll("[data-weight-row]"), row => readWeightRow(row));
    }
    /* Inline candidate rows are editable in the nl_choice form: they must be
       read back, otherwise edited key/model/description would be silently
       dropped on save (task-14 review C-3). */
    const optionForms = scope ? scope.querySelectorAll("[data-option-form]") : [];
    if (optionForms.length) {
      out.options = Array.prototype.map.call(optionForms, form => Object.assign(
        {kind: form.getAttribute("data-option-kind") || "model"},
        readOptionForm(form)));
    }
    const whenScope = scope && scope.querySelector("[data-when-form]");
    if (whenScope) out.when = readWhenForm(whenScope);
    return out;
  }

  function unitValue(raw) {
    if (raw === undefined || raw === null || raw === "") return null;
    const number = Number(raw);
    if (!Number.isFinite(number) || number < 0 || number > 1) return NaN;
    return number;
  }

  function applyNodeForm(node, values, options) {
    const opts = options || {};
    const input = values || {};
    const context = opts.context || {};
    let issues = [];
    let next = node;
    if (input.when !== undefined) {
      const whenResult = applyWhenForm(node, input.when, {signals: opts.signals, context: context});
      next = whenResult.node;
      issues = issues.concat(whenResult.issues);
    }
    if (next.type === "route") {
      if (input.model !== undefined) {
        const model = String(input.model == null ? "" : input.model).trim();
        if (!model) {
          issues.push(issue("option_model_required", next.id, "路由节点需要模型",
            "A route node needs a model"));
        } else if (opts.modelsById && !modelRowOf(opts.modelsById, model)) {
          issues.push(issue("unknown_model", next.id, "模型目录中不存在：" + model,
            "Unknown model: " + model));
        }
        next = Object.assign({}, next, {model: model});
      }
      if (input.reasoning !== undefined) {
        const reasoning = input.reasoning == null || input.reasoning === ""
          ? null : String(input.reasoning);
        const efforts = reasoningEfforts(opts.modelsById, next.model);
        if (reasoning && efforts.length &&
            !efforts.some(effort => String(effort.id) === reasoning)) {
          issues.push(issue("invalid_reasoning", next.id,
            "模型未声明该推理档位：" + reasoning,
            "The model does not declare this reasoning effort: " + reasoning));
        }
        next = Object.assign({}, next, {reasoning: reasoning});
      }
    }
    if (next.type === "route" || next.type === "block") {
      if (input.min_score !== undefined) {
        const minScore = unitValue(input.min_score);
        if (Number.isNaN(minScore)) {
          issues.push(issue("invalid_threshold", next.id,
            "达成率门槛 min_score 必须是 [0,1] 内的数值",
            "min_score must be a number in [0,1]"));
          next = Object.assign({}, next, {min_score: null});
        } else {
          next = Object.assign({}, next, {min_score: minScore});
        }
      }
      if (Array.isArray(input.weights)) {
        const weights = [];
        input.weights.forEach(item => {
          const signal = item && typeof item.signal === "string" ? item.signal.trim() : "";
          const spec = signalSpecOf(opts.signals, signal);
          if (!signal || !spec) {
            issues.push(issue("unknown_signal", next.id, "未知加权信号：" + (signal || "—"),
              "Unknown weighted signal"));
            return;
          }
          const value = String(item.value == null ? "" : item.value).trim();
          if (!value) {
            issues.push(issue("missing_value", next.id, "加权条件缺少取值",
              "A weighted condition has no value"));
            return;
          }
          const weight = item.weight == null ? DEFAULT_WEIGHT : Number(item.weight);
          if (!Number.isFinite(weight) || weight < 0 || weight > 1) {
            issues.push(issue("invalid_threshold", next.id,
              "加权条件的权重必须是 [0,1] 内的数值",
              "A weight must be a number in [0,1]"));
            return;
          }
          weights.push({signal: signal, value: value, weight: weight});
        });
        next = Object.assign({}, next, {weights: weights});
      }
    }
    if (next.type === "nl_choice") {
      if (input.instructions !== undefined) {
        next = Object.assign({}, next, {instructions: String(input.instructions == null
          ? "" : input.instructions)});
      }
      ["min_prob", "min_margin"].forEach(name => {
        if (input[name] === undefined) return;
        const value = unitValue(input[name]);
        if (Number.isNaN(value) || value === null) {
          issues.push(issue("invalid_threshold", next.id,
            name + " 必须是 [0,1] 内的数值", name + " must be a number in [0,1]"));
          return;
        }
        next = Object.assign({}, next, {[name]: value});
      });
      if (Array.isArray(input.options)) {
        const current = Array.isArray(next.options) ? next.options : [];
        if (input.options.length !== current.length) {
          issues.push(issue("option_count_changed", next.id,
            "候选数量在表单里发生了变化，请重新打开节点编辑器",
            "The candidate count changed while the form was open; reopen the node editor"));
        } else {
          const updated = [];
          const seen = new Set();
          input.options.forEach((values, index) => {
            const base = current[index];
            const kind = values.kind || (base && base.child != null ? "child" : "model");
            const taken = current
              .map((option, itemIndex) => itemIndex === index ? null : option.key)
              .filter(key => key != null);
            const result = applyOptionForm(base, values, {
              kind: kind, modelsById: opts.modelsById, takenKeys: taken,
            });
            issues = issues.concat(result.issues);
            updated.push(result.option);
            if (result.option && updated.length === index + 1 && result.option.key) {
              if (seen.has(result.option.key)) {
                issues.push(issue("duplicate_option_key", next.id,
                  "候选 key 重复：" + result.option.key,
                  "Duplicate candidate key: " + result.option.key));
              }
              seen.add(result.option.key);
            }
          });
          if (!issues.some(item => item.level !== "warn")) {
            next = Object.assign({}, next, {options: updated});
          }
        }
      }
    }
    return {node: next, issues: issues};
  }

  /* ── structure mutations (pure; the caller then calls SRTreeModel.updateNode /
     store.setDraft — the renderer never mutates) ──────────────────────── */

  function notContainer(node, expected) {
    return [issue("not_container", node && node.id,
      "只有 " + expected + " 节点能做这个结构操作",
      "Only " + expected + " nodes support this structure operation")];
  }

  function addChild(node, child) {
    if (!node || node.type !== "branch") {
      return {node: node, issues: notContainer(node, "branch")};
    }
    const children = Array.isArray(node.children) ? node.children : [];
    return {node: Object.assign({}, node, {children: children.concat([child])}), issues: []};
  }

  function removeChild(node, childId) {
    if (!node || node.type !== "branch") {
      return {node: node, issues: notContainer(node, "branch")};
    }
    const children = Array.isArray(node.children) ? node.children : [];
    if (!children.some(child => child.id === childId)) {
      return {node: node, issues: [issue("unknown_child", node.id,
        "找不到要删除的子节点：" + childId, "Unknown child node: " + childId)]};
    }
    return {
      node: Object.assign({}, node, {children: children.filter(child => child.id !== childId)}),
      issues: [],
    };
  }

  function addOption(node, option) {
    if (!node || node.type !== "nl_choice") {
      return {node: node, issues: notContainer(node, "nl_choice")};
    }
    const options = Array.isArray(node.options) ? node.options : [];
    if (options.some(existing => existing.key === option.key)) {
      return {node: node, issues: [issue("duplicate_option_key", node.id,
        "候选 key 重复：" + option.key, "Duplicate candidate key: " + option.key)]};
    }
    return {node: Object.assign({}, node, {options: options.concat([option])}), issues: []};
  }

  function removeOption(node, key) {
    if (!node || node.type !== "nl_choice") {
      return {node: node, issues: notContainer(node, "nl_choice")};
    }
    const options = Array.isArray(node.options) ? node.options : [];
    if (!options.some(option => option.key === key)) {
      return {node: node, issues: [issue("unknown_option", node.id,
        "找不到候选：" + key, "Unknown candidate: " + key)]};
    }
    return {
      node: Object.assign({}, node, {options: options.filter(option => option.key !== key)}),
      issues: [],
    };
  }

  function replaceOption(node, key, option) {
    if (!node || node.type !== "nl_choice") {
      return {node: node, issues: notContainer(node, "nl_choice")};
    }
    const options = Array.isArray(node.options) ? node.options : [];
    const index = options.findIndex(existing => existing.key === key);
    if (index < 0) {
      return {node: node, issues: [issue("unknown_option", node.id,
        "找不到候选：" + key, "Unknown candidate: " + key)]};
    }
    const next = options.slice();
    next[index] = option;
    return {node: Object.assign({}, node, {options: next}), issues: []};
  }

  function setOtherwise(node, child) {
    if (!node || (node.type !== "branch" && node.type !== "nl_choice")) {
      return {node: node, issues: notContainer(node, "branch/nl_choice")};
    }
    return {node: Object.assign({}, node, {otherwise: child}), issues: []};
  }

  function clearOtherwise(node) {
    if (!node || (node.type !== "branch" && node.type !== "nl_choice")) {
      return {node: node, issues: notContainer(node, "branch/nl_choice")};
    }
    return {node: Object.assign({}, node, {otherwise: null}), issues: []};
  }

  /* ── constructors / id helpers ───────────────────────────────────────── */

  function emptyNode(type, id, options) {
    const opts = options || {};
    if (!type || ["route", "block", "branch", "nl_choice"].indexOf(type) < 0) {
      throw new Error("SRTreeEditor.emptyNode: unknown node type " + type);
    }
    const node = {id: id, type: type};
    if (opts.when != null) node.when = opts.when;
    if (type === "route") {
      if (!opts.model) throw new Error("SRTreeEditor.emptyNode: route needs a model");
      node.model = opts.model;
      node.reasoning = opts.reasoning == null ? null : opts.reasoning;
      node.weights = [];
      return node;
    }
    if (type === "block") {
      node.weights = [];
      return node;
    }
    if (type === "branch") {
      node.children = [];
      node.otherwise = null;
      return node;
    }
    node.instructions = opts.instructions == null ? "" : String(opts.instructions);
    node.min_prob = opts.min_prob == null ? DEFAULT_MIN_PROB : opts.min_prob;
    node.min_margin = opts.min_margin == null ? DEFAULT_MIN_MARGIN : opts.min_margin;
    node.options = [];
    node.otherwise = null;
    return node;
  }

  function emptyOption(kind, key, options) {
    const opts = options || {};
    if (kind === "model") {
      if (!opts.model) throw new Error("SRTreeEditor.emptyOption: model option needs a model");
      return {key: key, model: opts.model,
              reasoning: opts.reasoning == null ? null : opts.reasoning};
    }
    if (!opts.child) throw new Error("SRTreeEditor.emptyOption: child option needs a child node");
    return {key: key, description: opts.description == null ? "" : String(opts.description),
            child: opts.child};
  }

  function collectIds(root) {
    const ids = new Set();
    if (typeof SRTreeModel === "undefined" || !SRTreeModel) {
      throw new Error("SRTreeEditor: SRTreeModel not loaded");
    }
    try {
      SRTreeModel.walk(root, node => { ids.add(node.id); });
    } catch (err) {
      // a duplicate id already breaks walk but its ids are still collected
    }
    return ids;
  }
  function uniqueNodeId(root, prefix) {
    const ids = collectIds(root);
    const base = prefix || "n";
    for (let index = 1; index < 100000; index++) {
      const candidate = base + index;
      if (!ids.has(candidate)) return candidate;
    }
    throw new Error("SRTreeEditor.uniqueNodeId: exhausted");
  }
  function nextOptionKey(node) {
    const used = new Set((node && node.options ? node.options : []).map(option => option.key));
    for (let index = 1; index < 100000; index++) {
      const candidate = "option" + index;
      if (!used.has(candidate)) return candidate;
    }
    throw new Error("SRTreeEditor.nextOptionKey: exhausted");
  }

  return {
    opForSignal: opForSignal,
    signalList: signalList,
    signalSpecOf: signalSpecOf,
    whenFormHTML: whenFormHTML,
    readWhenForm: readWhenForm,
    applyWhenForm: applyWhenForm,
    nodeFormHTML: nodeFormHTML,
    readNodeForm: readNodeForm,
    applyNodeForm: applyNodeForm,
    optionFormHTML: optionFormHTML,
    readOptionForm: readOptionForm,
    applyOptionForm: applyOptionForm,
    weightRowHTML: weightRowHTML,
    readWeightRow: readWeightRow,
    addWeight: addWeight,
    reasoningOptionsHTML: reasoningOptionsHTML,
    readFormValues: readFormValues,
    emptyNode: emptyNode,
    emptyOption: emptyOption,
    uniqueNodeId: uniqueNodeId,
    nextOptionKey: nextOptionKey,
    addChild: addChild,
    removeChild: removeChild,
    addOption: addOption,
    removeOption: removeOption,
    replaceOption: replaceOption,
    setOtherwise: setOtherwise,
    clearOtherwise: clearOtherwise,
  };
})();
