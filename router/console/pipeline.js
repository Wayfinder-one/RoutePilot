/* Routing page: signal palette (drag) + gate/score node editor + save.
   Ported from docs/prototypes/score-routing-demo.html (renderPalette /
   renderNodes / rowHtml / branchHtml / bindNodeEvents + drag). Changed:
   state comes from the server, row/branch field names match the API
   (conf_min / weight / min_score), free-form signals render a text input,
   and save PUTs the tree, surfacing health issues. */
const $ = s => document.querySelector(s);
let SIG = {};              // key -> {zh, en, group, kind, multi, values:[[v,zh,en],...]}
let GROUP_COLOR = { safety: 'var(--g-safety)', domain: 'var(--g-domain)',
  difficulty: 'var(--g-cx)', context: 'var(--g-ctx)', session: 'var(--g-sess)',
  rule: 'var(--g-rule)' };
let MODELS = [];
let KEYWORD_GROUPS = [];
let nodes = [];

let _uid = 0;
const uid = () => 'r' + (++_uid);

async function boot() {
  const [sigBody, pipeBody, modelBody] = await Promise.all([
    api.get('/admin/signals'), api.get('/admin/pipeline'),
    api.get('/admin/models')]);
  sigBody.signals.forEach(s => { SIG[s.key] = s; });
  KEYWORD_GROUPS = sigBody.keyword_groups;   // options for the keyword signal
  MODELS = modelBody.models.map(m => m.id);
  nodes = pipeBody.nodes.map(n => ({
    ...n,
    branches: (n.branches || []).map(b => ({
      ...b, rows: (b.rows || []).map(r => ({ ...r, id: uid() })),
    })),
  }));
  $('#version').textContent = 'v' + pipeBody.version;
  renderPalette(); renderNodes();
}

/* ── labels ──────────────────────────────────────────────────────────── */
const sigName = k => { const d = SIG[k]; return d ? (LANG === 'zh' ? d.zh : d.en) : k; };
const groupName = g => t('group.' + g);
function valLabel(key, value) {
  const d = SIG[key] || { values: [] };
  const o = (d.values || []).find(p => String(p[0]) === String(value));
  return o ? (LANG === 'zh' ? o[1] : (o[2] || o[1])) : value;
}
/* Free-form signals (empty registry values, not keyword) render a text input. */
const isFreeSig = k => { const d = SIG[k] || {}; return !(d.values || []).length && k !== 'keyword'; };

function valueOptions(sigKey, current) {
  const d = SIG[sigKey] || {};
  const pairs = (d.values && d.values.length) ? d.values
    : (sigKey === 'keyword' ? KEYWORD_GROUPS.map(g => [g, g]) : []);
  return pairs.map(p => {
    const value = p[0];
    const label = LANG === 'zh' ? p[1] : (p[2] || p[1]);
    return `<option value="${esc(value)}" ${String(value) === String(current) ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');
}

function firstValue(sigKey) {
  const d = SIG[sigKey] || {};
  if ((d.values || []).length) return d.values[0][0];
  if (sigKey === 'keyword') return KEYWORD_GROUPS[0] || '';
  return '';
}

function newRow(nodeType, sigKey) {
  const row = { id: uid(), signal: sigKey, value: String(firstValue(sigKey)) };
  if (nodeType === 'gate') row.conf_min = 0.6; else row.weight = 0.5;
  return row;
}

/* ── render: palette ─────────────────────────────────────────────────── */
function renderPalette() {
  const groups = {};
  Object.entries(SIG).forEach(([k, d]) => { (groups[d.group] ||= []).push([k, d]); });
  $('#palGroups').innerHTML = Object.entries(groups).map(([g, items]) => `
    <div class="pal-group">
      <div class="gname"><span class="gdot" style="background:${GROUP_COLOR[g] || 'var(--g-rule)'}"></span>${groupName(g)}</div>
      ${items.map(([k, d]) => `<button class="chip" draggable="true" data-sig="${k}">${esc(sigName(k))}<small>${d.kind === 'rule' ? '规则 · c=1' : 'JEV'}</small></button>`).join('')}
    </div>`).join('');
  document.querySelectorAll('.chip').forEach(ch => {
    ch.addEventListener('dragstart', e => e.dataTransfer.setData('text/sig', ch.dataset.sig));
  });
}

/* ── render: nodes ───────────────────────────────────────────────────── */
function rowHtml(node, branch, row) {
  const d = SIG[row.signal] || { group: 'rule' };
  const valCell = isFreeSig(row.signal)
    ? `<input type="text" data-row="${row.id}" data-f="value" value="${esc(row.value)}" style="width:130px">`
    : `<select data-row="${row.id}" data-f="value">${valueOptions(row.signal, row.value)}</select>`;
  const slider = node.type === 'gate'
    ? `<span class="wlab">${t('pipe.confmin')}</span><input type="range" min="0" max="1" step="0.05" value="${row.conf_min ?? 0.5}" data-row="${row.id}" data-f="conf_min"><span class="val" id="rv${row.id}">${Number(row.conf_min ?? 0.5).toFixed(2)}</span>`
    : `<span class="wlab">${t('pipe.weight')}</span><input type="range" min="0" max="1" step="0.05" value="${row.weight ?? 0.5}" data-row="${row.id}" data-f="weight"><span class="val" id="rv${row.id}">${Number(row.weight ?? 0.5).toFixed(2)}</span>`;
  return `<div class="wrow">
    <span class="sig" style="color:${GROUP_COLOR[d.group] || 'inherit'}">${esc(sigName(row.signal))}</span><span class="eq">${d.multi ? '含' : '='}</span>
    ${valCell}
    ${slider}
    <button class="iconbtn" data-delrow="${row.id}" title="${t('common.delete')}">×</button>
  </div>`;
}

const modelSel = branch =>
  `<select data-br="${branch.id}" data-f="model">${MODELS.map(m =>
    `<option value="${esc(m)}" ${branch.model === m ? 'selected' : ''}>${esc(m)}</option>`).join('')}</select>`;

function branchHtml(node, branch) {
  const rows = branch.rows.map(r => rowHtml(node, branch, r)).join('') ||
    `<div class="drop-ph">${node.type === 'gate' ? t('pipe.ph.gate') : t('pipe.ph.score')}</div>`;
  const foot = node.type === 'gate'
    ? `<select data-br="${branch.id}" data-f="action">
         <option value="block" ${branch.action === 'block' ? 'selected' : ''}>${t('pipe.block')}</option>
         <option value="route" ${branch.action === 'route' ? 'selected' : ''}>${t('pipe.route')}</option></select>
       ${branch.action === 'route' ? modelSel(branch) : `<span class="mut">${t('pipe.reject')}</span>`}`
    : `${modelSel(branch)}
       <span class="wlab">${t('pipe.minscore')}</span><input type="range" min="0" max="1" step="0.05" value="${branch.min_score ?? 0.5}" data-br="${branch.id}" data-f="min_score"><span class="val" id="bv${branch.id}">${Number(branch.min_score ?? 0.5).toFixed(2)}</span>`;
  return `<div class="branch" data-branch="${branch.id}">
    <div class="branch-head">
      <input class="branch-name" value="${esc(branch.name)}" data-br="${branch.id}" data-f="name" placeholder="${t('pipe.branchname')}">
      <button class="iconbtn" data-delbr="${branch.id}" title="${t('common.delete')}">×</button>
    </div>
    <div class="wrows" data-drop="${branch.id}">${rows}</div>
    <div class="branch-foot">${foot}</div>
  </div>`;
}

function renderNodes() {
  $('#nodes').innerHTML = nodes.map((n, i) => `
    <div class="node ${n.type}">
      <div class="node-head">
        <span class="badge ${n.type}">${n.type === 'gate' ? t('pipe.gate') : t('pipe.score')}</span>
        <input class="node-name" value="${esc(n.name)}" data-node="${n.id}" data-f="name">
        <button class="iconbtn" data-move="${n.id}|-1" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="iconbtn" data-move="${n.id}|1" ${i === nodes.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="iconbtn" data-delnode="${n.id}" title="${t('common.delete')}">×</button>
      </div>
      <div class="node-desc">${n.type === 'gate' ? t('pipe.desc.gate') : t('pipe.desc.score')}</div>
      ${n.branches.map(b => branchHtml(n, b)).join('')}
      <button class="btn dashed sm" data-addbr="${n.id}">${t('pipe.branch')}</button>
    </div>`).join('');
  bindNodeEvents();
}

/* ── interaction ─────────────────────────────────────────────────────── */
function findRow(rid) {
  for (const n of nodes) for (const b of n.branches) {
    const r = b.rows.find(r => r.id === rid); if (r) return { n, b, r };
  } return {};
}
function findBr(bid) {
  for (const n of nodes) { const b = n.branches.find(b => b.id === bid); if (b) return { n, b }; } return {};
}

function bindNodeEvents() {
  document.querySelectorAll('[data-drop]').forEach(z => {
    z.addEventListener('dragover', e => { e.preventDefault(); z.classList.add('over'); });
    z.addEventListener('dragleave', () => z.classList.remove('over'));
    z.addEventListener('drop', e => {
      e.preventDefault(); z.classList.remove('over');
      const sig = e.dataTransfer.getData('text/sig'); if (!sig) return;
      const { n, b } = findBr(z.dataset.drop);
      b.rows.push(newRow(n.type, sig)); renderNodes();
    });
  });
  document.querySelectorAll('input[type=range][data-row]').forEach(el => el.addEventListener('input', () => {
    const { r } = findRow(el.dataset.row); r[el.dataset.f] = +el.value;
    document.getElementById('rv' + r.id).textContent = (+el.value).toFixed(2);
  }));
  document.querySelectorAll('input[type=range][data-br]').forEach(el => el.addEventListener('input', () => {
    const { b } = findBr(el.dataset.br); b.min_score = +el.value;
    document.getElementById('bv' + b.id).textContent = (+el.value).toFixed(2);
  }));
  document.querySelectorAll('select[data-row]').forEach(el => el.addEventListener('change', () => {
    const { r } = findRow(el.dataset.row); r.value = el.value;
  }));
  document.querySelectorAll('input[type=text][data-row]').forEach(el => el.addEventListener('input', () => {
    const { r } = findRow(el.dataset.row); r.value = el.value;
  }));
  document.querySelectorAll('select[data-br]').forEach(el => el.addEventListener('change', () => {
    const { b } = findBr(el.dataset.br);
    if (el.dataset.f === 'action') { b.action = el.value; if (b.action === 'route' && !b.model) b.model = MODELS[0]; renderNodes(); }
    else b.model = el.value;
  }));
  document.querySelectorAll('input[data-f=name]').forEach(el => el.addEventListener('change', () => {
    if (el.dataset.node) { nodes.find(n => n.id === el.dataset.node).name = el.value; }
    else { findBr(el.dataset.br).b.name = el.value; }
  }));
  document.querySelectorAll('[data-delrow]').forEach(el => el.addEventListener('click', () => {
    const { b, r } = findRow(el.dataset.delrow); b.rows = b.rows.filter(x => x !== r); renderNodes();
  }));
  document.querySelectorAll('[data-delbr]').forEach(el => el.addEventListener('click', () => {
    const { n, b } = findBr(el.dataset.delbr); n.branches = n.branches.filter(x => x !== b); renderNodes();
  }));
  document.querySelectorAll('[data-delnode]').forEach(el => el.addEventListener('click', () => {
    nodes = nodes.filter(n => n.id !== el.dataset.delnode); renderNodes();
  }));
  document.querySelectorAll('[data-addbr]').forEach(el => el.addEventListener('click', () => {
    const n = nodes.find(n => n.id === el.dataset.addbr);
    const b = { id: uid(), name: '', rows: [] };
    if (n.type === 'gate') { b.action = 'route'; b.model = MODELS[0]; } else { b.model = MODELS[0]; b.min_score = 0.6; }
    n.branches.push(b); renderNodes();
  }));
  document.querySelectorAll('[data-move]').forEach(el => el.addEventListener('click', () => {
    const [id, d] = el.dataset.move.split('|');
    const i = nodes.findIndex(n => n.id === id), j = i + (+d);
    if (j < 0 || j >= nodes.length) return;
    [nodes[i], nodes[j]] = [nodes[j], nodes[i]]; renderNodes();
  }));
}

$('#addGate').addEventListener('click', () => {
  nodes.push({ id: uid(), type: 'gate', name: t('pipe.gate') + ' ' + (nodes.length + 1),
    branches: [{ id: uid(), name: '', rows: [], action: 'route', model: MODELS[0] }] });
  renderNodes();
});
$('#addScore').addEventListener('click', () => {
  nodes.push({ id: uid(), type: 'score', name: t('pipe.score') + ' ' + (nodes.length + 1),
    branches: [{ id: uid(), name: '', rows: [], model: MODELS[0], min_score: 0.6 }] });
  renderNodes();
});

/* ── save ────────────────────────────────────────────────────────────── */
function cleanNode(n) {
  return { id: n.id, type: n.type, name: n.name, branches: (n.branches || []).map(b => ({
    id: b.id, name: b.name, action: b.action || 'route', model: b.model ?? null,
    min_score: b.min_score ?? null,
    rows: (b.rows || []).map(r => ({ signal: r.signal, value: String(r.value ?? ''),
      weight: r.weight ?? null, conf_min: r.conf_min ?? null })),
  })) };
}

function showIssues(issues) {
  $('#issues').innerHTML = issues.map(i =>
    `<div>• ${esc(LANG === 'zh' ? (i.zh || i.code || '') : (i.en || i.code || ''))}</div>`).join('');
}

async function save() {
  try {
    await api.put('/admin/pipeline', { nodes: nodes.map(cleanNode) });
    $('#issues').innerHTML = '';
    toast(t('common.saved'));
  } catch (e) {
    const issues = Array.isArray(e.detail) ? e.detail : [{ zh: String(e.detail), en: String(e.detail) }];
    showIssues(issues);
  }
}
$('#savePipeline').addEventListener('click', save);

window.onLangChange = () => { renderPalette(); renderNodes(); };
boot();
