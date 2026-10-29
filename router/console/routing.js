const $ = selector => document.querySelector(selector);
const STATE = { trees: [], models: [], periods: [], current: null, isNew: false, snapshotRevision: '' };
let sequence = 0;

const uid = prefix => `${prefix}-${Date.now().toString(36)}-${sequence++}`;
const clone = value => JSON.parse(JSON.stringify(value));
const pathKey = path => JSON.stringify(path);

function modelTarget(id) {
  return { id: id || uid('target'), type: 'target', strategy: 'least_inflight', capability: '', models: [{ model: STATE.models[0]?.id || '', reasoning: null }] };
}

function nodeOf(type, id) {
  const nodeId = id || uid(type);
  if (type === 'branch') return { id: nodeId, type, children: [modelTarget()], otherwise: modelTarget() };
  if (type === 'jev') return {
    id: nodeId, type, instructions: '你是一个模型路由器。下面是一条用户请求，请根据每个模型的能力说明，选择最合适处理这条请求的模型。',
    min_prob: 0, min_margin: 0,
    options: [
      { key: 'fast', label: '轻量模型', target: modelTarget() },
      { key: 'strong', label: '增强模型', target: modelTarget() },
    ],
    fallback: modelTarget(),
  };
  return modelTarget(nodeId);
}

function newTree() {
  return { id: uid('tree'), name: '新分流树', enabled: false, timezone: 'Asia/Shanghai', sticky: { enabled: true, ttl_seconds: 1800 }, root: nodeOf('jev', 'root') };
}

function at(path) {
  return path.slice(1).reduce((value, key) => value[key], STATE.current.root);
}

function markDirty() {
  $('#dirty').textContent = t('routing.dirty');
}

function modelOptions(selected) {
  const models = STATE.models.map(model => model.id);
  if (selected && !models.includes(selected)) models.push(selected);
  return models.map(id => `<option value="${esc(id)}" ${id === selected ? 'selected' : ''}>${esc(id)}</option>`).join('');
}

function field(path, name, value, kind = 'text') {
  return `<input type="${kind}" data-path="${esc(pathKey(path))}" data-field="${name}" value="${esc(value ?? '')}" ${kind === 'number' ? 'step="any"' : ''}>`;
}

function whenHtml(node, path) {
  const selected = node.when?.periods || [];
  return `<div class="when-box"><b>${t('routing.when')}</b><div class="period-days">${STATE.periods.map(period => `<label><input type="checkbox" data-path="${esc(pathKey(path))}" data-period="${esc(period.name)}" ${selected.includes(period.name) ? 'checked' : ''}>${esc(period.name)}</label>`).join('') || '—'}</div></div>`;
}

function targetHtml(node, path, showWhen) {
  return `<div class="node" data-node="${esc(pathKey(path))}"><div class="node-head"><select data-path="${esc(pathKey(path))}" data-change-type>${['target', 'jev', 'branch'].map(type => `<option value="${type}" ${type === 'target' ? 'selected' : ''}>${type}</option>`).join('')}</select>${field(path, 'id', node.id)}<span class="pill">least_inflight</span></div>
    <label class="field"><span>${t('routing.capability')}</span>${field(path, 'capability', node.capability || '')}</label>
    <div class="col-h">${t('routing.models')}<button class="btn ghost sm push-right" data-action="add-model" data-path="${esc(pathKey(path))}">${t('common.add')}</button></div>
    ${(node.models || []).map((item, index) => `<div class="model-row"><select data-path="${esc(pathKey([...path, 'models', index]))}" data-field="model">${modelOptions(item.model)}</select>${field([...path, 'models', index], 'reasoning', item.reasoning)}<button class="del" data-action="remove-model" data-path="${esc(pathKey([...path, 'models', index]))}">✕</button></div>`).join('')}${showWhen ? whenHtml(node, path) : ''}</div>`;
}

function jevHtml(node, path, showWhen) {
  return `<div class="node"><div class="node-head"><select data-path="${esc(pathKey(path))}" data-change-type>${['target', 'jev', 'branch'].map(type => `<option value="${type}" ${type === 'jev' ? 'selected' : ''}>${type}</option>`).join('')}</select>${field(path, 'id', node.id)}<span class="pill indigo">JEV</span></div>
    <label class="field"><span>${t('routing.instructions')}</span><textarea rows="3" data-path="${esc(pathKey(path))}" data-field="instructions">${esc(node.instructions)}</textarea></label>
    <div class="field-row"><label class="field"><span>min_prob</span>${field(path, 'min_prob', node.min_prob, 'number')}</label><label class="field"><span>min_margin</span>${field(path, 'min_margin', node.min_margin, 'number')}</label></div>
    <div class="col-h">${t('routing.options')}<button class="btn ghost sm push-right" data-action="add-option" data-path="${esc(pathKey(path))}">${t('common.add')}</button></div>
    <div class="node-options">${(node.options || []).map((option, index) => {
      const optionPath = [...path, 'options', index];
      return `<div class="node"><div class="field-row">${field(optionPath, 'key', option.key)}${field(optionPath, 'label', option.label)}<button class="del" data-action="remove-option" data-path="${esc(pathKey(optionPath))}">✕</button></div>${targetHtml(option.target, [...optionPath, 'target'], false)}</div>`;
    }).join('')}</div>
    <div class="col-h">${t('routing.fallback')}</div>${node.fallback ? targetHtml(node.fallback, [...path, 'fallback'], false) : ''}${showWhen ? whenHtml(node, path) : ''}</div>`;
}

function branchHtml(node, path, showWhen) {
  return `<div class="node"><div class="node-head"><select data-path="${esc(pathKey(path))}" data-change-type>${['target', 'jev', 'branch'].map(type => `<option value="${type}" ${type === 'branch' ? 'selected' : ''}>${type}</option>`).join('')}</select>${field(path, 'id', node.id)}<span class="pill">branch</span></div>
    <div class="col-h">${t('routing.children')}<button class="btn ghost sm push-right" data-action="add-child" data-path="${esc(pathKey(path))}">${t('common.add')}</button></div>
    ${(node.children || []).map((child, index) => `<div class="node"><div class="node-actions"><button class="del" data-action="remove-child" data-path="${esc(pathKey([...path, 'children', index]))}">✕</button></div>${renderNode(child, [...path, 'children', index], true)}</div>`).join('')}
    <div class="col-h">${t('routing.otherwise')}</div>${node.otherwise ? `<div class="node-actions"><button class="del" data-action="remove-otherwise" data-path="${esc(pathKey(path))}">✕</button></div>${renderNode(node.otherwise, [...path, 'otherwise'], false)}` : `<button class="btn dashed sm" data-action="add-otherwise" data-path="${esc(pathKey(path))}">${t('common.add')}</button>`}${showWhen ? whenHtml(node, path) : ''}</div>`;
}

function renderNode(node, path, showWhen) {
  if (node.type === 'branch') return branchHtml(node, path, showWhen);
  if (node.type === 'jev') return jevHtml(node, path, showWhen);
  return targetHtml(node, path, showWhen);
}

function renderTreeList() {
  $('#treeList').innerHTML = STATE.trees.map(tree => `<button class="tree-item ${STATE.current && tree.id === STATE.current.id ? 'on' : ''}" data-tree-id="${esc(tree.id)}"><b>${esc(tree.name || tree.id)}</b><small>${tree.enabled ? '● ' : '○ '}${esc(tree.id)}</small></button>`).join('');
}

function render() {
  renderTreeList();
  const tree = STATE.current;
  if (!tree) {
    $('#nodeEditor').innerHTML = `<div class="muted-block">${t('routing.empty')}</div>`;
    return;
  }
  $('#treeName').value = tree.name || '';
  $('#timezone').value = tree.timezone || 'Asia/Shanghai';
  $('#stickyEnabled').checked = !!tree.sticky?.enabled;
  $('#stickyTtl').value = tree.sticky?.ttl_seconds ?? 1800;
  $('#enableTree').textContent = tree.enabled ? t('routing.disable') : t('routing.enable');
  $('#enableTree').disabled = STATE.isNew;
  $('#copyTree').disabled = STATE.isNew;
  $('#nodeEditor').innerHTML = renderNode(tree.root, ['root'], false);
  $('#dirty').textContent = STATE.isNew ? t('routing.unsaved') : '';
}

function selectTree(id) {
  const tree = STATE.trees.find(item => item.id === id);
  if (!tree) return;
  STATE.current = clone(tree);
  STATE.isNew = false;
  render();
}

function updatePeriods() {
  $('#periods').innerHTML = STATE.periods.map((period, index) => `<div class="period-row"><input data-period-index="${index}" data-period-field="name" value="${esc(period.name)}"><input data-period-index="${index}" data-period-field="start" value="${esc(period.start)}" placeholder="09:00"><input data-period-index="${index}" data-period-field="end" value="${esc(period.end)}" placeholder="18:00"><button class="del" data-remove-period="${index}">✕</button></div><div class="period-days">${[0, 1, 2, 3, 4, 5, 6].map(day => `<label><input type="checkbox" data-period-index="${index}" data-period-day="${day}" ${period.days.includes(day) ? 'checked' : ''}>${day}</label>`).join('')}</div>`).join('');
}

$('#treeList').addEventListener('click', event => {
  const button = event.target.closest('[data-tree-id]');
  if (button) selectTree(button.dataset.treeId);
});
$('#newTree').addEventListener('click', () => { STATE.current = newTree(); STATE.isNew = true; render(); });
$('#treeName').addEventListener('input', event => { STATE.current.name = event.target.value; markDirty(); });
$('#timezone').addEventListener('input', event => { STATE.current.timezone = event.target.value; markDirty(); });
$('#stickyEnabled').addEventListener('change', event => { STATE.current.sticky.enabled = event.target.checked; markDirty(); });
$('#stickyTtl').addEventListener('input', event => { STATE.current.sticky.ttl_seconds = Number(event.target.value); markDirty(); });

$('#nodeEditor').addEventListener('change', event => {
  const input = event.target;
  if (!input.dataset.path) return;
  const path = JSON.parse(input.dataset.path);
  const node = at(path);
  if (input.dataset.changeType !== undefined) {
    const replacement = nodeOf(input.value, node.id);
    if (node.when) replacement.when = node.when;
    if (path.length === 1) STATE.current.root = replacement;
    else {
      const parent = at(path.slice(0, -1));
      parent[path[path.length - 1]] = replacement;
    }
    markDirty(); render(); return;
  }
  if (input.dataset.period) {
    const periods = new Set(node.when?.periods || []);
    if (input.checked) periods.add(input.dataset.period); else periods.delete(input.dataset.period);
    node.when = periods.size ? { periods: Array.from(periods) } : null;
  } else if (input.dataset.field) {
    let value = input.value;
    if (['min_prob', 'min_margin'].includes(input.dataset.field)) value = Number(value);
    if (input.dataset.field === 'reasoning') value = value || null;
    node[input.dataset.field] = value;
  }
  markDirty();
});

$('#nodeEditor').addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const path = JSON.parse(button.dataset.path);
  const node = at(path);
  const action = button.dataset.action;
  if (action === 'add-model') node.models.push({ model: STATE.models[0]?.id || '', reasoning: null });
  if (action === 'remove-model') {
    const [parentPath, , index] = [path.slice(0, -1), 'models', path[path.length - 1]];
    at(parentPath).splice(index, 1);
  }
  if (action === 'add-option') node.options.push({ key: uid('option'), label: '新候选', target: modelTarget() });
  if (action === 'remove-option') {
    const parent = at(path.slice(0, -1));
    parent.splice(path[path.length - 1], 1);
  }
  if (action === 'add-child') node.children.push(modelTarget());
  if (action === 'remove-child') {
    const parent = at(path.slice(0, -1));
    parent.splice(path[path.length - 1], 1);
  }
  if (action === 'add-otherwise') node.otherwise = modelTarget();
  if (action === 'remove-otherwise') node.otherwise = null;
  markDirty(); render();
});

function treePayload() {
  return { id: STATE.current.id, name: $('#treeName').value, timezone: $('#timezone').value, sticky: STATE.current.sticky, root: STATE.current.root };
}

$('#saveTree').addEventListener('click', async () => {
  try {
    const response = STATE.isNew ? await api.post('/admin/trees', treePayload()) : await api.put(`/admin/trees/${STATE.current.id}`, treePayload());
    STATE.current = response.tree;
    STATE.isNew = false;
    STATE.snapshotRevision = response.snapshot_revision || STATE.snapshotRevision;
    const list = await api.get('/admin/trees');
    STATE.trees = list.trees || [];
    selectTree(STATE.current.id);
    toast(t('common.saved'));
  } catch (error) {
    const detail = Array.isArray(error.detail) ? error.detail.map(issue => issue.zh || issue.en).join('; ') : apiErrText(error);
    $('#issues').textContent = detail;
  }
});

$('#enableTree').addEventListener('click', async () => {
  try {
    const action = STATE.current.enabled ? 'disable' : 'enable';
    const response = await api.post(`/admin/trees/${STATE.current.id}/${action}`);
    STATE.trees = response.trees || [];
    STATE.snapshotRevision = response.snapshot_revision || '';
    selectTree(STATE.current.id);
  } catch (error) { $('#issues').textContent = Array.isArray(error.detail) ? error.detail.map(issue => issue.zh || issue.en).join('; ') : apiErrText(error); }
});

$('#copyTree').addEventListener('click', async () => {
  try {
    const response = await api.post(`/admin/trees/${STATE.current.id}/copy`, { name: `${STATE.current.name} copy` });
    STATE.trees.push(response.tree);
    selectTree(response.tree.id);
  } catch (error) { toast(apiErrText(error)); }
});

$('#exportTree').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify({ kind: 'sr-jev-routing-tree', version: 1, tree: treePayload() }, null, 2)], { type: 'application/json' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${STATE.current.id}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});
$('#importTree').addEventListener('click', () => $('#importFile').click());
$('#importFile').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    if (data.kind !== 'sr-jev-routing-tree' || data.version !== 1 || !data.tree) throw new Error(t('routing.importBad'));
    STATE.current = data.tree;
    STATE.current.enabled = false;
    STATE.isNew = true;
    render();
  } catch (error) { $('#issues').textContent = error.message; }
  event.target.value = '';
});

$('#addPeriod').addEventListener('click', () => { STATE.periods.push({ name: uid('时段'), days: [0, 1, 2, 3, 4, 5, 6], start: '00:00', end: '00:00' }); updatePeriods(); });
$('#periods').addEventListener('change', event => {
  const input = event.target;
  const index = Number(input.dataset.periodIndex);
  if (input.dataset.periodField) STATE.periods[index][input.dataset.periodField] = input.value;
  if (input.dataset.periodDay !== undefined) {
    const day = Number(input.dataset.periodDay);
    const days = new Set(STATE.periods[index].days);
    if (input.checked) days.add(day); else days.delete(day);
    STATE.periods[index].days = Array.from(days).sort();
  }
});
$('#periods').addEventListener('click', event => {
  const button = event.target.closest('[data-remove-period]');
  if (!button) return;
  STATE.periods.splice(Number(button.dataset.removePeriod), 1);
  updatePeriods();
});
$('#savePeriods').addEventListener('click', async () => {
  try {
    const response = await api.put('/admin/time-periods', { periods: STATE.periods });
    STATE.periods = response.periods || [];
    updatePeriods(); render(); toast(t('common.saved'));
  } catch (error) { toast(apiErrText(error)); }
});

$('#tryRun').addEventListener('click', async () => {
  $('#tryOut').textContent = t('common.loading');
  try {
    const body = { tree_id: STATE.current.id, text: $('#tryText').value, session_id: $('#trySession').value };
    if ($('#tryNow').value) body.now = $('#tryNow').value;
    const result = await api.post('/admin/try', body);
    SRTrace.renderTry($('#tryOut'), result);
  } catch (error) { $('#tryOut').textContent = apiErrText(error); }
});

async function boot() {
  const [trees, models, periods] = await Promise.all([api.get('/admin/trees'), api.get('/admin/models'), api.get('/admin/time-periods')]);
  STATE.trees = trees.trees || [];
  STATE.models = models.models || [];
  STATE.periods = periods.periods || [];
  STATE.snapshotRevision = trees.snapshot_revision || '';
  setServerOffset(periods.now);
  startServerClockTick(() => { $('#serverClockPill').textContent = fmtServerTime(serverNow()); });
  $('#snapshotPill').textContent = STATE.snapshotRevision.slice(0, 12) || '—';
  updatePeriods();
  if (STATE.trees.length) selectTree(STATE.trees[0].id);
  else { STATE.current = newTree(); STATE.isNew = true; render(); }
}

window.onLangChange = () => { renderTreeList(); render(); updatePeriods(); };
boot().catch(error => { $('#issues').textContent = apiErrText(error); });
