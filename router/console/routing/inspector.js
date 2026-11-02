/* RTInspector · 三分支检查器（规格 §6.6）
   ------------------------------------------------------------------
   render(selection)：
     · node  → 按节点类型生成配置字段（问题/选项/关键词/时段/算子/模型/priority…）
     · edge  → 两端只读 + 类型 + 条件表单（复用 RTCondPop） + 删除
     · graph → 名称 / 时区 / 粘性 / otherwise_target / error_fallback + 限额
   所有写入走 App.change：一次表单提交 = 一个历史项，不在 keystroke 上写草稿。
   选项 key 改名在提交前用探针快照预演（renameOptionKey），冲突时留在编辑器并给出
   具体原因；删除节点/边/选项交给 App.requestDelete 的确认框（级联清单在那里生成）。
   ------------------------------------------------------------------ */
(function (root) {
  'use strict';

  const esc = value => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const t = key => (typeof root.t === 'function' ? root.t(key) : key);
  const tf = (key, vars) => (typeof root.tf === 'function' ? root.tf(key, vars) : t(key));
  const app = () => root.App || null;
  const clone = value => JSON.parse(JSON.stringify(value));
  const error = message => ({ ok: false, message: message });

  const COMMON_TIMEZONES = [
    'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Singapore', 'Asia/Kolkata', 'Europe/London',
    'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'UTC'
  ];

  let bound = false;

  /* ── 小工具 ─────────────────────────────────────────────────────── */

  function nodeOf(id) {
    const current = app();
    return current && current.graph ? root.RTGraph.findNode(current.graph, id) : null;
  }

  function selectionOf() {
    const current = app();
    if (current && current.kit) return current.kit.getSelection();
    return (current && current.selection) || { kind: null, id: null };
  }

  function findModel(id) {
    return ((app() && app().models) || []).find(model => model && model.id === id) || null;
  }

  function reasoningOf(model) {
    if (!model) return { efforts: [], default: null };
    let reasoning = model.reasoning;
    if (!reasoning && model.profile && typeof model.profile === 'object') reasoning = model.profile.reasoning;
    if (!reasoning || reasoning.declared === false || reasoning.supported === false) {
      return { efforts: [], default: null };
    }
    const efforts = Array.isArray(reasoning.efforts)
      ? reasoning.efforts.map(item => (typeof item === 'string' ? item : item && item.id)).filter(Boolean)
      : [];
    return { efforts: efforts, default: reasoning.default || null };
  }

  function nodeKindLabel(node) {
    if (!node) return '';
    if (node.type === 'start') return t('cv.card.start');
    if (node.type === 'question') {
      if (node.question_type === 'choice') return t('cv.card.choice');
      if (node.question_type === 'noul') return t('cv.card.noul');
      return t('cv.card.score');
    }
    if (node.type === 'logic') return t('cv.op.' + node.operator);
    if (node.type === 'condition') {
      return node.source === 'text' ? t('cv.card.text') : t('cv.card.periods');
    }
    if (node.type === 'target') return t('cv.node.target');
    return node.type;
  }

  function portLabel(node, portId) {
    if (!node) return portId;
    const port = root.RTGraph.portOf(node, portId, app().graph);
    if (!port) return portId;
    return port.label + ' · ' + portId;
  }

  function conditionText(edge) {
    if (!edge) return '';
    const graph = app().graph;
    const type = root.RTGraph.edgeType(edge, graph);
    const condition = edge.condition;
    if (type === 'boolean') {
      if (condition == null) return '= true';
      if (condition.op === 'is' && typeof condition.value === 'boolean') {
        return condition.value ? '= true' : '= false';
      }
      return t('cv.cond.invalid');
    }
    if (!condition) return t('cv.cond.unset');
    const symbol = root.RTGraph.OP[condition.op];
    if (!symbol || !Number.isFinite(Number(condition.value))) return t('cv.cond.invalid');
    return symbol + ' ' + Number(condition.value);
  }

  function fieldValue(form, name) {
    const element = form.querySelector('[data-field="' + name + '"]');
    if (!element) return '';
    if (element.type === 'checkbox') return element.checked;
    return element.value;
  }

  function optionRows(form) {
    return Array.from(form.querySelectorAll('[data-opt-row]')).map(row => ({
      orig: row.dataset.optOrig || '',
      key: (row.querySelector('[data-opt-field="key"]') || {}).value || '',
      label: (row.querySelector('[data-opt-field="label"]') || {}).value || '',
      capability: (row.querySelector('[data-opt-field="capability"]') || {}).value || ''
    }));
  }

  function modelRows(form) {
    return Array.from(form.querySelectorAll('[data-model-row]')).map(row => ({
      model: (row.querySelector('[data-model-field="model"]') || {}).value || '',
      reasoning: (row.querySelector('[data-model-field="reasoning"]') || {}).value || ''
    }));
  }

  /* ── 节点表单片段 ───────────────────────────────────────────────── */

  function optionRowHTML(option, orig) {
    const key = option.key == null ? '' : String(option.key);
    return '<div class="rtc-opt-edit" data-opt-row="' + esc(key) + '" data-opt-orig="' + esc(orig) + '">' +
      '<div class="field-row">' +
      '<label class="field">' + esc(t('cv.ins.optionKey')) +
      '<input type="text" data-opt-field="key" value="' + esc(key) + '" aria-label="' + esc(t('cv.ins.optionKey')) + '"></label>' +
      '<label class="field">' + esc(t('cv.ins.optionLabel')) +
      '<input type="text" data-opt-field="label" value="' + esc(option.label == null ? '' : option.label) +
      '" aria-label="' + esc(t('cv.ins.optionLabel')) + '"></label>' +
      '</div>' +
      '<label class="field">' + esc(t('cv.ins.optionCapability')) +
      '<textarea rows="2" data-opt-field="capability" aria-label="' + esc(t('cv.ins.optionCapability')) + '">' +
      esc(option.capability == null ? '' : option.capability) + '</textarea></label>' +
      '<button type="button" class="btn sm ghost" data-act="opt-delete" data-opt-key="' + esc(key) +
      '" aria-label="' + esc(tf('cv.ins.optionDeleteAria', { key: key })) + '">' +
      esc(t('cv.ins.optionDelete')) + '</button>' +
      '</div>';
  }

  function reasoningOptionsHTML(model, selected) {
    const info = reasoningOf(model);
    const values = info.efforts.slice();
    const value = selected || '';
    if (value && values.indexOf(value) < 0) values.push(value);
    return '<option value="">' + esc(t('cv.ins.reasoningNone')) + '</option>' +
      values.map(id => '<option value="' + esc(id) + '"' + (id === value ? ' selected' : '') + '>' +
        esc(id) + '</option>').join('');
  }

  function modelRowHTML(item) {
    const modelId = (item && item.model) || '';
    const known = !!findModel(modelId);
    const options = ((app() && app().models) || []).map(model =>
      '<option value="' + esc(model.id) + '"' + (model.id === modelId ? ' selected' : '') + '>' +
      esc(model.id) + '</option>').join('');
    const extra = !known && modelId
      ? '<option value="' + esc(modelId) + '" selected>' + esc(modelId) + '</option>' : '';
    const reasoning = item && item.reasoning ? item.reasoning : '';
    return '<div class="rtc-model-edit" data-model-row>' +
      '<label class="field">' + esc(t('cv.ins.model')) +
      '<select data-model-field="model" aria-label="' + esc(t('cv.ins.model')) + '">' +
      options + extra + '</select></label>' +
      '<label class="field">' + esc(t('cv.ins.reasoning')) +
      '<select data-model-field="reasoning" aria-label="' + esc(t('cv.ins.reasoning')) + '">' +
      reasoningOptionsHTML(findModel(modelId), reasoning) + '</select></label>' +
      '<button type="button" class="btn sm ghost" data-act="model-remove" aria-label="' +
      esc(t('cv.ins.modelRemove')) + '">' + esc(t('cv.ins.modelRemove')) + '</button>' +
      '</div>';
  }

  function fieldsForNode(node) {
    let html = '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.node')) +
      '<span class="pill rtc-ins-type">' + esc(nodeKindLabel(node)) + '</span></div>' +
      '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.id')) + '</span><b>' + esc(node.id) + '</b></div>' +
      '<label class="field">' + esc(t('cv.ins.titleField')) +
      '<input type="text" data-field="title" value="' + esc(node.title || '') + '" aria-label="' +
      esc(t('cv.ins.titleField')) + '"></label></div>';

    if (node.type === 'start') {
      return html + '<div class="rtc-ins-note">' + esc(t('cv.ins.startNote')) + '</div>';
    }

    if (node.type === 'question') {
      html += '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.question')) + '</div>' +
        '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.questionType')) + '</span><b data-ins-question-type="' +
        esc(node.question_type) + '">' + esc(nodeKindLabel(node)) + '</b></div>' +
        '<label class="field">' + esc(t('cv.ins.instructions')) +
        '<textarea rows="3" data-field="instructions" aria-label="' + esc(t('cv.ins.instructions')) + '">' +
        esc(node.instructions || '') + '</textarea></label>';
      if (node.question_type === 'choice') {
        html += '<div class="rtc-ins-sh">' + esc(t('cv.ins.options')) + '</div>' +
          '<div data-opt-rows>' + (node.options || []).map(option => optionRowHTML(option, option.key)).join('') +
          '</div><button type="button" class="btn sm" data-act="opt-add">' +
          esc(t('cv.ins.optionAdd')) + '</button>';
      } else if (node.question_type === 'score') {
        html += '<label class="field">' + esc(t('cv.ins.criteria')) +
          '<input type="text" data-field="criteria" value="' + esc((node.criteria || []).join(', ')) +
          '" aria-label="' + esc(t('cv.ins.criteria')) + '"></label>';
      }
      html += '</div>';
    } else if (node.type === 'logic') {
      html += '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.logic')) + '</div>' +
        '<label class="field">' + esc(t('cv.ins.operator')) +
        '<select data-field="operator" aria-label="' + esc(t('cv.ins.operator')) + '">' +
        ['and', 'or', 'not'].map(operator =>
          '<option value="' + esc(operator) + '"' + (node.operator === operator ? ' selected' : '') + '>' +
          esc(t('cv.op.' + operator)) + '</option>').join('') +
        '</select></label></div>';
    } else if (node.type === 'condition') {
      html += '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' +
        esc(node.source === 'text' ? t('cv.card.text') : t('cv.card.periods')) + '</div>';
      if (node.source === 'text') {
        html += '<label class="field">' + esc(t('cv.ins.keywords')) +
          '<textarea rows="2" data-field="keywords" aria-label="' + esc(t('cv.ins.keywords')) + '">' +
          esc((node.contains_any || []).join(', ')) + '</textarea></label>';
      } else {
        const names = ((app() && app().periods) || []).slice();
        (node.periods || []).forEach(name => { if (names.indexOf(name) < 0) names.push(name); });
        html += '<div class="rtc-ins-checks" data-field="periods">' +
          names.map(name => '<label class="rtc-check"><input type="checkbox" value="' + esc(name) + '"' +
            ((node.periods || []).indexOf(name) >= 0 ? ' checked' : '') + '> ' + esc(name) + '</label>').join('') +
          '</div>';
      }
      html += '</div>';
    } else if (node.type === 'target') {
      const models = (node.models && node.models.length) ? node.models : [{ model: '', reasoning: '' }];
      html += '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.models')) + '</div>' +
        '<div data-model-rows>' + models.map(modelRowHTML).join('') + '</div>' +
        '<button type="button" class="btn sm" data-act="model-add">' + esc(t('cv.ins.modelAdd')) + '</button>' +
        '<label class="field">' + esc(t('cv.ins.priority')) +
        '<input type="number" min="1" step="1" data-field="priority" value="' +
        esc(node.priority == null ? 1 : node.priority) + '" aria-label="' + esc(t('cv.ins.priority')) + '"></label>' +
        '<label class="field">' + esc(t('cv.ins.strategy')) +
        '<select data-field="strategy" aria-label="' + esc(t('cv.ins.strategy')) + '">' +
        strategyOptions(node.strategy) + '</select></label>' +
        '<label class="field">' + esc(t('cv.ins.capability')) +
        '<textarea rows="2" data-field="capability" aria-label="' + esc(t('cv.ins.capability')) + '">' +
        esc(node.capability || '') + '</textarea></label></div>';
    }
    return html;
  }

  function strategyOptions(selected) {
    const values = ['least_inflight'];
    if (selected && values.indexOf(selected) < 0) values.push(selected);
    return values.map(value => '<option value="' + esc(value) + '"' +
      (value === selected ? ' selected' : '') + '>' + esc(value) + '</option>').join('');
  }

  function formTail(saveAction, extraActions) {
    return '<div class="rtc-ins-error" data-ins-error role="alert" hidden></div>' +
      '<div class="rtc-dialog-acts">' + (extraActions || '') +
      '<button type="submit" class="btn sm pri" data-act="' + saveAction + '">' +
      esc(t('cv.ins.save')) + '</button></div>';
  }

  function nodeBranch(node) {
    return '<div class="rtc-ins" data-ins-branch="node" data-ins-id="' + esc(node.id) +
      '" data-ins-node-type="' + esc(node.type) + '">' +
      '<div class="rtc-ins-acts"><button type="button" class="btn sm danger" data-act="ins-delete-node">' +
      esc(t('cv.ins.deleteNode')) + '</button></div>' +
      '<form data-ins-form="node" novalidate>' + fieldsForNode(node) +
      formTail('ins-save-node') + '</form></div>';
  }

  function edgeBranch(edge) {
    const graph = app().graph;
    const source = root.RTGraph.findNode(graph, edge.source);
    const target = root.RTGraph.findNode(graph, edge.target);
    const type = root.RTGraph.edgeType(edge, graph);
    const typeLabel = type === 'number' ? t('cv.ins.typeNumber') : t('cv.ins.typeBoolean');
    return '<div class="rtc-ins" data-ins-branch="edge" data-ins-id="' + esc(edge.id) +
      '" data-edge-type="' + esc(type) + '">' +
      '<form data-ins-form="edge" novalidate>' +
      '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.edge')) + '</div>' +
      '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.source')) + '</span><b title="' +
      esc(edge.source + '.' + edge.source_port) + '">' + esc((source ? source.id : edge.source) + ' · ' +
        portLabel(source, edge.source_port)) + '</b></div>' +
      '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.target')) + '</span><b title="' +
      esc(edge.target + '.' + edge.target_port) + '">' + esc((target ? target.id : edge.target) + ' · ' +
        edge.target_port) + '</b></div>' +
      '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.type')) + '</span><b>' + esc(typeLabel) + '</b></div>' +
      '<div class="rtc-ins-row"><span>' + esc(t('cv.ins.condition')) + '</span><b data-edge-condition>' +
      esc(conditionText(edge)) + '</b></div></div>' +
      '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.condition')) + '</div>' +
      '<div class="rtc-cond-fields" data-cond-fields>' + root.RTCondPop.formHTML(edge) + '</div>' +
      '<div class="rtc-cond-error" data-cond-error role="alert" hidden></div></div>' +
      formTail('ins-save-edge',
        '<button type="button" class="btn sm danger" data-act="ins-delete-edge">' +
        esc(t('cv.ins.deleteEdge')) + '</button>') +
      '</form></div>';
  }

  function statCell(value, label) {
    return '<div class="rtc-statcell"><b>' + esc(value) + '</b><span>' + esc(label) + '</span></div>';
  }

  function limitRow(name, used, limit) {
    return '<div class="rtc-ins-row" data-limit="' + esc(name) + '"><span>' +
      esc(t('cv.ins.limit' + name.charAt(0).toUpperCase() + name.slice(1))) + '</span><b>' +
      esc(tf('cv.ins.limitValue', { used: used, limit: limit })) + '</b></div>';
  }

  function graphBranch(graph, tree) {
    const nodes = (graph.nodes || []).length;
    const edges = (graph.edges || []).length;
    const questions = (graph.nodes || []).filter(node => node.type === 'question').length;
    const limits = root.RTGraph.LIMITS;
    const targets = (graph.nodes || []).filter(node => node.type === 'target');
    const targetOptions = selected => '<option value="">' + esc(t('cv.ins.none')) + '</option>' +
      targets.map(node => '<option value="' + esc(node.id) + '"' +
        (node.id === selected ? ' selected' : '') + '>' + esc(node.id) + '</option>').join('');
    const zones = COMMON_TIMEZONES.slice();
    try {
      if (typeof Intl.supportedValuesOf === 'function') {
        Intl.supportedValuesOf('timeZone').forEach(zone => { if (zones.indexOf(zone) < 0) zones.push(zone); });
      }
    } catch (error) { /* 浏览器不支持时只用常用时区 */ }
    const currentZone = (tree && tree.timezone) || 'Asia/Shanghai';
    if (zones.indexOf(currentZone) < 0) zones.push(currentZone);
    const sticky = (tree && tree.sticky) || { enabled: false, ttl_seconds: 1800 };
    return '<div class="rtc-ins" data-ins-branch="graph" data-ins-id="root">' +
      '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.inspectorStats')) + '</div>' +
      '<div class="rtc-statgrid">' + statCell(nodes, t('cv.inspectorNodes')) +
      statCell(edges, t('cv.inspectorEdges')) + statCell(questions, t('cv.inspectorQuestions')) + '</div></div>' +
      '<form data-ins-form="graph" novalidate>' +
      '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.inspectorGraph')) + '</div>' +
      '<label class="field">' + esc(t('cv.ins.graphName')) +
      '<input type="text" data-field="name" value="' + esc((tree && tree.name) || '') +
      '" aria-label="' + esc(t('cv.ins.graphName')) + '"></label>' +
      '<label class="field">' + esc(t('cv.ins.timezone')) +
      '<input type="text" list="cvTzList" data-field="timezone" value="' + esc(currentZone) +
      '" aria-label="' + esc(t('cv.ins.timezone')) + '">' +
      '<datalist id="cvTzList">' + zones.map(zone => '<option value="' + esc(zone) + '"></option>').join('') +
      '</datalist></label>' +
      '<label class="rtc-check"><input type="checkbox" data-field="sticky"' +
      (sticky.enabled ? ' checked' : '') + '> ' + esc(t('cv.ins.sticky')) + '</label>' +
      '<label class="field">' + esc(t('cv.ins.stickyTtl')) +
      '<input type="number" min="1" step="1" data-field="stickyTtl" value="' +
      esc(sticky.ttl_seconds == null ? 1800 : sticky.ttl_seconds) + '" aria-label="' +
      esc(t('cv.ins.stickyTtl')) + '"></label>' +
      '<label class="field">' + esc(t('cv.ins.otherwise')) +
      '<select data-field="otherwise" aria-label="' + esc(t('cv.ins.otherwise')) + '">' +
      targetOptions(graph.otherwise_target) + '</select></label>' +
      '<label class="field">' + esc(t('cv.ins.errorFallback')) +
      '<select data-field="errorFallback" aria-label="' + esc(t('cv.ins.errorFallback')) + '">' +
      targetOptions(graph.error_fallback) + '</select></label></div>' +
      '<div class="rtc-ins-sec"><div class="rtc-ins-sh">' + esc(t('cv.ins.limits')) + '</div>' +
      limitRow('nodes', nodes, limits.nodes) + limitRow('edges', edges, limits.edges) +
      limitRow('questions', questions, limits.questions) +
      '<div class="rtc-ins-row" data-limit="waves"><span>' + esc(t('cv.ins.limitWaves')) + '</span><b>' +
      esc(tf('cv.ins.limitWavesValue', { limit: limits.waves })) + '</b></div></div>' +
      formTail('ins-save-graph') + '</form></div>';
  }

  /* ── render ─────────────────────────────────────────────────────── */

  function render(selection) {
    const body = document.getElementById('cvInspectorBody');
    if (!body) return;
    bind();
    const current = app();
    if (!current || !current.graph) {
      body.innerHTML = '<div class="rtc-ins-note">' + esc(t('cv.ins.notReady')) + '</div>';
      return;
    }
    const selected = selection || selectionOf();
    const sub = document.getElementById('cvInspectorSub');
    if (selected && selected.kind === 'node') {
      const node = nodeOf(selected.id);
      if (node) {
        body.innerHTML = nodeBranch(node);
        if (sub) sub.textContent = node.id;
        return;
      }
    }
    if (selected && selected.kind === 'edge') {
      const edge = (current.graph.edges || []).find(item => item && item.id === selected.id);
      if (edge) {
        body.innerHTML = edgeBranch(edge);
        if (sub) sub.textContent = edge.id;
        return;
      }
    }
    body.innerHTML = graphBranch(current.graph, current.tree);
    if (sub) sub.textContent = 'root';
  }

  /* ── 提交：节点 ─────────────────────────────────────────────────── */

  function applyOptionRows(treeRoot, node, rows) {
    const keys = Object.create(null);
    const labels = Object.create(null);
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      const key = row.key.trim();
      if (!key) return error(t('cv.ins.errOptionKey'));
      if (/\s/.test(key) || key.indexOf('.') >= 0 || key.length > 64) {
        return error(t('cv.ins.errOptionKeyInvalid'));
      }
      if (keys[key]) return error(tf('cv.ins.errOptionKeyExists', { key: key }));
      keys[key] = true;
      const label = row.label.trim();
      if (!label) return error(t('cv.ins.errOptionLabel'));
      if (labels[label]) return error(tf('cv.ins.errOptionLabelDup', { label: label }));
      labels[label] = true;
      if (!row.capability.trim()) return error(t('cv.ins.errOptionCapability'));
    }
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      const key = row.key.trim();
      const label = row.label.trim();
      const capability = row.capability.trim();
      if (row.orig) {
        if (row.orig !== key) {
          const renamed = root.RTGraph.renameOptionKey(treeRoot, node.id, row.orig, key);
          /* 上面的预校验已覆盖空 key / 空白 / 句点 / 冲突；走到这里说明节点已失效。 */
          if (!renamed.ok) return error(t('cv.ins.errOptionRename'));
        }
        const option = (node.options || []).find(item => item && item.key === key);
        if (option) {
          option.label = label;
          option.capability = capability;
        }
      } else {
        node.options = node.options || [];
        node.options.push({ key: key, label: label, capability: capability });
      }
    }
    return { ok: true };
  }

  function applyNodeValues(treeRoot, nodeId, values) {
    const node = root.RTGraph.findNode(treeRoot, nodeId);
    if (!node) return error(t('cv.ins.nodeMissing'));
    if (values.title !== undefined) {
      const title = String(values.title || '').trim();
      if (title) node.title = title;
      else delete node.title;
    }
    if (node.type === 'start') return { ok: true };

    if (node.type === 'question') {
      const instructions = String(values.instructions || '').trim();
      if (!instructions) return error(t('cv.ins.errInstructions'));
      node.instructions = instructions;
      if (node.question_type === 'choice') {
        const applied = applyOptionRows(treeRoot, node, values.options || []);
        if (!applied.ok) return applied;
      } else if (node.question_type === 'score') {
        const parts = String(values.criteria || '').split(/[,，\s]+/).filter(Boolean);
        const levels = parts.map(Number);
        if (levels.length < root.RTGraph.LIMITS.criteriaMin || levels.length > root.RTGraph.LIMITS.criteriaMax ||
            levels.some(level => !Number.isFinite(level))) {
          return error(t('cv.ins.errCriteria'));
        }
        node.criteria = levels;
      }
    } else if (node.type === 'logic') {
      if (['and', 'or', 'not'].indexOf(values.operator) < 0) return error(t('cv.ins.errOperator'));
      node.operator = values.operator;
    } else if (node.type === 'condition') {
      if (node.source === 'text') {
        const words = String(values.keywords || '').split(/[,，\n]+/).map(word => word.trim()).filter(Boolean);
        if (!words.length) return error(t('cv.ins.errKeywords'));
        node.contains_any = words;
      } else {
        const periods = values.periods || [];
        if (!periods.length) return error(t('cv.ins.errPeriods'));
        node.periods = periods.slice();
      }
    } else if (node.type === 'target') {
      const models = (values.models || []).filter(item => item.model);
      if (!models.length) return error(t('cv.ins.errModels'));
      node.models = models.map(item => (item.reasoning
        ? { model: item.model, reasoning: item.reasoning }
        : { model: item.model }));
      const priority = Number(values.priority);
      if (!Number.isInteger(priority) || priority < 1) return error(t('cv.ins.errPriority'));
      const duplicate = (treeRoot.nodes || []).some(item =>
        item && item.type === 'target' && item.id !== node.id && item.priority === priority);
      if (duplicate) return error(tf('cv.ins.errPriorityDup', { value: priority }));
      node.priority = priority;
      if (values.strategy) node.strategy = values.strategy;
      const capability = String(values.capability || '').trim();
      if (capability) node.capability = capability;
      else delete node.capability;
    }
    return { ok: true };
  }

  function submitNode(form) {
    const current = app();
    const selected = current.kit.getSelection();
    if (selected.kind !== 'node') return error(t('cv.ins.nodeMissing'));
    const node = nodeOf(selected.id);
    if (!node) return error(t('cv.ins.nodeMissing'));
    const values = {
      title: fieldValue(form, 'title'),
      instructions: fieldValue(form, 'instructions'),
      criteria: fieldValue(form, 'criteria'),
      operator: fieldValue(form, 'operator'),
      keywords: fieldValue(form, 'keywords'),
      priority: fieldValue(form, 'priority'),
      strategy: fieldValue(form, 'strategy'),
      capability: fieldValue(form, 'capability'),
      options: optionRows(form),
      models: modelRows(form),
      periods: Array.from(form.querySelectorAll('[data-field="periods"] input:checked'))
        .map(input => input.value)
    };
    const probe = clone(current.tree);
    const check = applyNodeValues(probe.root, selected.id, values);
    if (!check.ok) return check;
    current.change('behavior', tree => {
      applyNodeValues(tree.root, selected.id, values);
    });
    return { ok: true };
  }

  /* ── 提交：边（复用 RTCondPop 唯一提交路径） ─────────────────────── */

  function submitEdge(form) {
    const current = app();
    const selected = current.kit.getSelection();
    if (selected.kind !== 'edge') return error(t('cv.cond.edgeMissing'));
    const edge = (current.graph.edges || []).find(item => item && item.id === selected.id);
    if (!edge) return error(t('cv.cond.edgeMissing'));
    const draft = root.RTCondPop.readForm(form, edge);
    if (!draft.ok) return draft;
    const result = root.RTCondPop.validateAndCommit(edge.id, draft.condition);
    return result.ok ? { ok: true } : result;
  }

  /* ── 提交：图设置 ───────────────────────────────────────────────── */

  function isTimeZone(value) {
    if (!value) return false;
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: value }).format(new Date());
      return true;
    } catch (error) {
      return false;
    }
  }

  function submitGraph(form) {
    const current = app();
    const graph = current.graph;
    const name = String(fieldValue(form, 'name') || '').trim();
    if (!name) return error(t('cv.ins.errName'));
    const timezone = String(fieldValue(form, 'timezone') || '').trim();
    if (!isTimeZone(timezone)) return error(tf('cv.ins.errTimezone', { value: timezone }));
    const ttl = Number(fieldValue(form, 'stickyTtl'));
    if (!Number.isInteger(ttl) || ttl < 1) return error(t('cv.ins.errStickyTtl'));
    const targetIds = (graph.nodes || []).filter(node => node.type === 'target').map(node => node.id);
    const otherwise = String(fieldValue(form, 'otherwise') || '') || null;
    const errorFallback = String(fieldValue(form, 'errorFallback') || '') || null;
    if (otherwise && targetIds.indexOf(otherwise) < 0) return error(t('cv.ins.errFallback'));
    if (errorFallback && targetIds.indexOf(errorFallback) < 0) return error(t('cv.ins.errFallback'));
    const sticky = { enabled: !!fieldValue(form, 'sticky'), ttl_seconds: ttl };
    current.change('behavior', tree => {
      tree.name = name;
      tree.timezone = timezone;
      tree.sticky = sticky;
      tree.root.otherwise_target = otherwise;
      tree.root.error_fallback = errorFallback;
    });
    return { ok: true };
  }

  function showError(form, message) {
    const container = form.closest('[data-ins-branch]') || form;
    const box = container.querySelector('[data-ins-error]');
    if (!box) return;
    box.textContent = String(message == null ? '' : message);
    box.hidden = false;
  }

  function onSubmit(event) {
    const form = event.target.closest('[data-ins-form]');
    if (!form) return;
    event.preventDefault();
    const kind = form.dataset.insForm;
    const result = kind === 'node' ? submitNode(form)
      : kind === 'edge' ? submitEdge(form)
        : submitGraph(form);
    if (!result.ok) showError(form, result.message);
  }

  /* ── 交互 ───────────────────────────────────────────────────────── */

  function onClick(event) {
    const action = event.target.closest('[data-act]');
    if (!action || !action.closest('#cvInspectorBody')) return;
    const name = action.dataset.act;
    const current = app();
    const selected = current.kit.getSelection();
    if (name === 'ins-delete-node') {
      if (selected.kind === 'node') current.requestDelete({ kind: 'node', id: selected.id });
      return;
    }
    if (name === 'ins-delete-edge') {
      if (selected.kind === 'edge') current.requestDelete({ kind: 'edge', id: selected.id });
      return;
    }
    if (name === 'opt-add') {
      const rows = document.querySelector('#cvInspectorBody [data-opt-rows]');
      if (!rows) return;
      const used = new Set(Array.from(rows.querySelectorAll('[data-opt-row]')).map(row => row.dataset.optRow));
      let index = 1;
      while (used.has('opt' + index)) index += 1;
      const option = {
        key: 'opt' + index,
        label: tf('cv.ins.newOptionLabel', { n: index }),
        capability: t('cv.ins.newOptionCapability')
      };
      rows.insertAdjacentHTML('beforeend', optionRowHTML(option, ''));
      const added = rows.querySelector('[data-opt-row]:last-child');
      if (added) added.dataset.optNew = '1';
      return;
    }
    if (name === 'opt-delete') {
      const row = action.closest('[data-opt-row]');
      if (!row) return;
      if (!row.dataset.optOrig) {
        row.parentNode.removeChild(row);
        return;
      }
      if (selected.kind !== 'node') return;
      current.requestDelete({ kind: 'option', nodeId: selected.id, key: row.dataset.optOrig });
      return;
    }
    if (name === 'model-add') {
      const rows = document.querySelector('#cvInspectorBody [data-model-rows]');
      if (!rows) return;
      const first = ((app() && app().models) || [])[0] || null;
      const reasoning = reasoningOf(first).default || '';
      rows.insertAdjacentHTML('beforeend', modelRowHTML({
        model: first ? first.id : '',
        reasoning: reasoning
      }));
      return;
    }
    if (name === 'model-remove') {
      const row = action.closest('[data-model-row]');
      if (!row) return;
      const rows = row.parentNode;
      if (rows && rows.querySelectorAll('[data-model-row]').length <= 1) {
        showError(row.closest('[data-ins-form]') || row, t('cv.ins.errModels'));
        return;
      }
      row.parentNode.removeChild(row);
    }
  }

  function onChange(event) {
    const select = event.target.closest('[data-model-field="model"]');
    if (!select) return;
    const row = select.closest('[data-model-row]');
    if (!row) return;
    const reasoning = row.querySelector('[data-model-field="reasoning"]');
    if (!reasoning) return;
    const model = findModel(select.value);
    const info = reasoningOf(model);
    reasoning.innerHTML = reasoningOptionsHTML(model, info.default || '');
  }

  function bind() {
    if (bound) return;
    const body = document.getElementById('cvInspectorBody');
    if (!body) return;
    bound = true;
    body.addEventListener('click', onClick);
    body.addEventListener('submit', onSubmit);
    body.addEventListener('change', onChange);
  }

  bind();

  root.RTInspector = {
    render: render,
    applyNodeValues: applyNodeValues,
    isTimeZone: isTimeZone
  };
})(window);
