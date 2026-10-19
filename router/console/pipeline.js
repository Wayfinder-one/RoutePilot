/* Routing-tree console v4 (SPEC-TREE-4 §7-8).
   Ported from the approved prototype docs/od-design/pipeline-tree-redesign/
   index-c.html and wired to the admin API. The tree of a split node is always
   DERIVED (never stored) — the JS derive below mirrors router/pipeline/tree.py
   exactly; both sides must stay in sync.

   Hard rules carried over from the spec:
   - whole-tree innerHTML rebuild happens ONCE at boot; every edit only redraws
     its station (renderStation) and station elements are never recreated;
   - the signal palette list is never rebuilt either — used/selection state is
     class/aria toggles so scroll and focus survive;
   - every add/delete/move shows a toast with undo (6 s);
   - the fallback node is locked last: not draggable, not deletable. */
const $ = (s, r = document) => (r || document).querySelector(s);
const $$ = (s, r = document) => Array.from((r || document).querySelectorAll(s));

/* ── state ───────────────────────────────────────────────────────────── */
let SIG = {};                  // key -> {key,zh,en,group,kind,multi,free,values:[[k,zh,en]]}
let GROUPS = [];               // group keys in registry order
let KEYWORD_GROUPS = [];
let MODELS = [];
let nodes = [];                // API shape: {id,type,name,floor,split_signal,branches:[…]}
let version = '0';
let tagger = {};
let dirty = false;
let lastTry = null;            // /admin/try response

let mode = null;               // null | 'pick' | 'drag'
let pick = { signal: null, node: null };
let dragSig = null;
let dragNodeId = null;
let nodeDrop = null;
let popTarget = null;          // {kind:'row'|'branch'|'level'|'new', …}

let uid = 0;
const nid = p => (p || 'x') + Date.now().toString(36) + (++uid).toString(36);

/* ── i18n helpers ────────────────────────────────────────────────────── */
const sigSpec = k => SIG[k] || { key: k, zh: k, en: k, group: 'rule', kind: 'model', values: [] };
const sigName = k => (LANG === 'zh' ? sigSpec(k).zh : (sigSpec(k).en || sigSpec(k).zh)) || k;
const isRuleSig = k => (sigSpec(k).kind || 'model') === 'rule';
const isFreeSig = k => sigSpec(k).free !== undefined
  ? !!sigSpec(k).free
  : !(sigSpec(k).values || []).length;
const valLabel = (k, v) => {
  if (v == null || v === '') return '';
  const hit = (sigSpec(k).values || []).find(x => String(x[0]) === String(v));
  if (!hit) return String(v);
  return LANG === 'zh' ? hit[1] : (hit[2] || hit[1]);
};
const groupName = g => t('group.' + g);
const nodeName = n => n.name || (LANG === 'zh' ? '节点 ' + n.id : 'node ' + n.id);
const branchName = b => b.name || (LANG === 'zh' ? '分支 ' + b.id : 'branch ' + b.id);

const findNode = id => nodes.find(n => n.id === id);
const findBranch = id => {
  for (const n of nodes) { const b = (n.branches || []).find(b => b.id === id); if (b) return { node: n, branch: b }; }
  return null;
};
const isLastNode = n => nodes.length && n.id === nodes[nodes.length - 1].id;

/* ── derivation — mirrors router/pipeline/tree.py ────────────────────── */
function signalOrder(node) {
  const seen = [];
  for (const b of node.branches) for (const r of b.rows)
    if (r.value && !seen.includes(r.signal)) seen.push(r.signal);
  return seen;
}
function nextForkKey(node, branches, used) {
  if (branches.length < 2) return null;              // one branch: no layer
  for (const sig of signalOrder(node)) {
    if (used.has(sig)) continue;
    // every branch carries the signal → it is this layer's key, even when
    // all values are equal (the trunk shows first)
    if (branches.every(b => b.rows.some(r => r.signal === sig))) return sig;
  }
  return null;
}
function rootKey(node, branches, used) {
  // manual override applies to the ROOT level only (SPEC-TREE-4 §3.3)
  if (!used.size && node.type === 'split' && node.split_signal) {
    if (branches.length && branches.every(b => b.rows.some(r => r.signal === node.split_signal)))
      return node.split_signal;
  }
  return nextForkKey(node, branches, used);
}
function derive(node, branches, used) {
  if (!branches.length) return { leaves: [] };
  const key = rootKey(node, branches, used);
  if (!key) return { leaves: branches.slice() };
  const map = new Map();
  branches.forEach(b => {
    const r = b.rows.find(x => x.signal === key);
    const v = (r ? (r.value || '') : '') || null;    // 「待选值」 → pending group
    if (!map.has(v)) map.set(v, []);
    map.get(v).push(b);
  });
  const next = new Set(used); next.add(key);
  return { key, groups: [...map.entries()].map(([value, bs]) => ({ value, child: derive(node, bs, next) })) };
}
const countLeaves = level => level.leaves ? level.leaves.length
  : level.groups.reduce((a, g) => a + countLeaves(g.child), 0);
const hasFork = node => node.type === 'split' && !!derive(node, node.branches, new Set()).groups;
const firstPath = node => {
  let lvl = derive(node, node.branches, new Set());
  const path = [];
  while (lvl.groups && lvl.groups.length) {
    path.push({ signal: lvl.key, value: lvl.groups[0].value });
    lvl = lvl.groups[0].child;
  }
  return path;
};
const rowsUsed = () => {
  const used = new Set();
  nodes.forEach(n => (n.branches || []).forEach(b => b.rows.forEach(r => used.add(r.signal))));
  return used;
};

/* ── header ──────────────────────────────────────────────────────────── */
/* The page head is deliberately bare (no engine/sync/version pills, no
   config-count strip): unsaved edits surface as a dot on the save button. */
function renderDirty() {
  const btn = $('#savePipeline');
  if (!btn) return;
  btn.classList.toggle('has-dirty', !!dirty);
  btn.title = dirty ? t('pipe.dirty') : t('pipe.clean');
}
function renderHeader() {
  $('#exportPipeline').title = t('p4.exportTitle');
  $('#importPipeline').title = t('p4.importTitle');
  renderDirty();
}

/* ── palette (rendered once; state is toggled, never rebuilt) ────────── */
function renderPalette() {
  const used = rowsUsed();
  $('#palBody').innerHTML = GROUPS.map(g => {
    const items = Object.values(SIG).filter(s => s.group === g);
    if (!items.length) return '';
    return `<section class="grp">
      <h3>${esc(groupName(g))}<span class="rl"></span></h3>
      ${items.map(s => `
        <button class="sig${used.has(s.key) ? ' used' : ''}" data-sig="${esc(s.key)}" draggable="true"
                aria-pressed="false" title="${esc(t('p4.sigTitle'))}">
          <i class="dot"></i><span class="n">${esc(sigName(s.key))}</span>
          <svg class="grip" width="9" height="9" viewBox="0 0 9 9" fill="currentColor" aria-hidden="true">
            <circle cx="3" cy="2" r="1"/><circle cx="6" cy="2" r="1"/>
            <circle cx="3" cy="4.5" r="1"/><circle cx="6" cy="4.5" r="1"/>
            <circle cx="3" cy="7" r="1"/><circle cx="6" cy="7" r="1"/>
          </svg>
        </button>`).join('')}
    </section>`;
  }).join('');
  $('#palCount').textContent = Object.keys(SIG).length + (LANG === 'zh' ? ' 个' : '');
  syncPalHint();
}
function syncPalHint() {
  const el = $('#palHint');
  if (mode === 'pick' && pick.signal) {
    el.classList.add('active');
    el.textContent = tf('p4.hintPicked', { name: sigName(pick.signal) });
  } else if (mode === 'pick' && pick.node) {
    el.classList.add('active');
    const n = findNode(pick.node) || {};
    const path = n.type === 'split' ? firstPath(n) : [];
    el.textContent = path.length
      ? tf('p4.hintNewBranchAt', {
          name: nodeName(n),
          path: path.map(p => valLabel(p.signal, p.value) || t('p4.pending')).join(' → '),
        })
      : tf('p4.hintNewBranch', { name: nodeName(n) });
  } else if (mode === 'drag' && dragSig) {
    el.classList.add('active');
    el.textContent = tf('p4.hintDragging', { name: sigName(dragSig) });
  } else {
    el.classList.remove('active');
    el.innerHTML = esc(t('p4.hintDrag')) + '<br>' + esc(t('p4.hintPick'));
  }
}
function syncPalFade() {
  const body = $('#palBody');
  body.classList.toggle('more',
    body.scrollHeight - body.scrollTop - body.clientHeight > 2);
}
function syncPaletteUsed() {
  const used = rowsUsed();
  $$('#palBody .sig').forEach(el => el.classList.toggle('used', used.has(el.dataset.sig)));
}
function syncPaletteSelection() {
  $$('#palBody .sig').forEach(el =>
    el.setAttribute('aria-pressed', String(pick.signal === el.dataset.sig)));
}

/* ── tree rendering ──────────────────────────────────────────────────── */
function rowChip(node, branch, row, idx, showWeight) {
  const key = branch.id + ':' + idx;
  if (!row.value) {
    return `<button class="row tbd" data-row="${key}" title="${esc(t('p4.chooseValue'))}">${esc(sigName(row.signal))}<span class="sp">·</span>${isFreeSig(row.signal) ? t('p4.tbdFree') : t('p4.tbdValue')}</button>`;
  }
  let k = '';
  if (row.weight != null) k = showWeight ? '×' + (+row.weight.toFixed(2)) : '';
  else if (row.conf_min != null) k = isRuleSig(row.signal) ? t('p4.deterministic') : t('p4.confFloor') + row.conf_min.toFixed(2);
  return `<button class="row" data-row="${key}" title="${esc(t('p4.rowTitle'))}">
      <b>${esc(sigName(row.signal))}</b><span class="sp">·</span><span>${esc(valLabel(row.signal, row.value))}</span>
      ${k ? `<span class="k">${esc(k)}</span>` : ''}
    </button>`;
}

function branchRow(node, b, opts = {}) {
  const fall = isLastNode(node) && node.type === 'gate';
  const tone = b.action === 'block' ? 'var(--t4-red)' : (fall ? 'var(--t4-slate)' : 'var(--t4-accent)');
  const outCls = b.action === 'block' ? 'block' : (fall ? 'fall' : 'route');
  const outTxt = b.action === 'block'
    ? esc(t('pipe.block'))
    : (b.model ? esc(fall ? t('p4.fallback') : t('pipe.route')) + '<code>' + esc(b.model) + '</code>' : esc(t('p4.noModel')));
  // grouped signals live on the group head; leaves show only their own rest
  const shown = opts.omit ? b.rows.filter(r => !opts.omit.has(r.signal)) : b.rows;
  const showWeight = opts.omit ? shown.length > 1 : true;
  const rows = shown.map(r => rowChip(node, b, r, b.rows.indexOf(r), showWeight))
    .join('<span class="plus" aria-hidden="true">＋</span>');
  const threshold = node.type !== 'gate' && b.min_score != null && shown.length
    ? `<span class="min-s">${t('p4.minScore')} ${b.min_score.toFixed(2)}</span>` : '';
  const empty = shown.length ? rows
    : (opts.omit ? `<span class="no-cond">${t('p4.hitRoute')}</span>`
                 : `<span class="no-cond">${t('p4.noCond')}</span>`);
  // Tree editing model (2026-10-19): a split row becomes editable (+ / −) only
  // once it already carries a child layer (>= 2 conditions, deepest one set),
  // so the root node row starts bare. Flat gate/score rows always offer them.
  const showActions = node.type === 'split'
    ? (b.rows.length >= 2 && !!b.rows[b.rows.length - 1].value)
    : true;
  return `<div class="branch" data-branch="${esc(b.id)}" style="--tone:${tone}">
      <span class="bname" title="${esc(branchName(b))}">${esc(branchName(b))}</span>
      <div class="conds">
        ${empty}
        ${threshold}
        <span class="drop-hint">${esc(t('p4.dropHere'))}</span>
      </div>
      <div class="outcell">
        <button class="out ${b.model || b.action === 'block' ? outCls : 'todo'}" data-branchcfg="${esc(b.id)}"
                title="${esc(t('p4.branchCfg'))}">${outTxt}</button>
        ${showActions && b.rows.length ? `<button class="bact add" data-addbranchfrom="${esc(b.id)}"
                title="${esc(tf('p4.addBranchFromTitle', { sig: sigName(b.rows[b.rows.length - 1].signal) }))}"
                aria-label="${esc(tf('p4.addBranchFromNamed', { name: branchName(b), sig: sigName(b.rows[b.rows.length - 1].signal) }))}">
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M5.5 2v7M2 5.5h7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>` : ''}
        ${showActions && !(isLastNode(node) && node.branches.length === 1) ? `<button class="bact del" data-delbranch="${esc(b.id)}" title="${esc(t('p4.delBranch'))}"
                aria-label="${esc(tf('p4.delBranchNamed', { name: branchName(b) }))}">
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M2 5.5h7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>` : ''}
      </div>
    </div>`;
}

function renderLevel(node, level, path, used) {
  if (level.leaves) {
    return level.leaves.map(b =>
      `<div class="tnode leaf">${branchRow(node, b, { omit: used })}</div>`).join('');
  }
  const key = level.key;
  const nextUsed = new Set(used); nextUsed.add(key);
  return level.groups.map(g => {
    const p2 = [...path, { signal: key, value: g.value }];
    const gdata = esc(JSON.stringify({ node: node.id, path: p2 }));
    return `<div class="tnode grp">
        <div class="ghead" data-group="${gdata}">
          <span class="gk">${esc(sigName(key))}</span><span class="gk">=</span>
          <span class="gv${g.value == null ? ' tbd' : ''}">${g.value == null ? esc(t('p4.pending')) : esc(valLabel(key, g.value))}</span>
          ${isRuleSig(key)
            ? `<button class="gdet" data-level="${esc(node.id)}" title="${esc(t('p4.groupCfg'))}">${esc(t('p4.deterministic'))}</button>`
            : `<button class="floor" data-level="${esc(node.id)}" title="${esc(t('p4.floorTitle'))}">${esc(t('p4.confFloor'))}${(node.floor != null ? node.floor : 0.70).toFixed(2)}</button>`}
          <span class="gc">${countLeaves(g.child)} ${esc(t('p4.leafUnit'))}</span>
        </div>
        <div class="gbody">${renderLevel(node, g.child, p2, nextUsed)}</div>
      </div>`;
  }).join('');
}

function splitBody(n) {
  if (!n.branches.length) {
    return `<div class="stack-empty">
        ${esc(t('p4.emptySplit'))}<br>
        <button data-delnode="${esc(n.id)}">${esc(t('p4.deleteNode'))}</button>
      </div>`;
  }
  const level = derive(n, n.branches, new Set());
  if (!level.groups) {
    return `<div class="stack-note">
        <span>${esc(t('p4.noFork'))}</span>
        <button data-level="${esc(n.id)}">${esc(t('p4.setGroupKey'))}</button>
      </div>` + n.branches.map(b => branchRow(n, b)).join('');
  }
  return `<div class="gbody groot">${renderLevel(n, level, [], new Set())}</div>`;
}

function nodeDesc(n) {
  if (n.type === 'split') {
    if (!n.branches.length) return t('p4.descEmpty');
    return hasFork(n) ? t('p4.descSplit') : t('p4.descNoFork');
  }
  return n.type === 'gate' ? t('pipe.desc.gate') : t('pipe.desc.score');
}

function delBtnHTML(n) {
  return `<button class="st-del" data-delnode="${esc(n.id)}" title="${esc(t('p4.deleteNode'))}"
            aria-label="${esc(tf('p4.delNodeNamed', { name: nodeName(n) }))}">
      <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M2 2l7 7M9 2l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
    </button>`;
}

function bodyHTML(n) {
  const last = isLastNode(n);
  const typeLabel = n.type === 'split' ? t('p4.typeSplit') : (n.type === 'score' ? t('pipe.score') : t('pipe.gate'));
  // a name that already contains the type word does not get the badge again
  const showType = !(LANG === 'zh' ? (n.name || '').includes(typeLabel) : (n.name || '').toLowerCase().includes(typeLabel.toLowerCase()));
  return `<div class="st-head">
      <h2>${esc(nodeName(n))}</h2>
      ${showType ? `<span class="ty">${esc(typeLabel)}</span>` : ''}
      <p class="st-desc">${esc(nodeDesc(n))}</p>
      <button class="st-add" data-addbranch="${esc(n.id)}">＋ ${esc(t('p4.newBranch'))}</button>
      ${last ? '' : delBtnHTML(n)}
    </div>
    <div class="stack" role="group" aria-label="${esc(tf('p4.stackOf', { name: nodeName(n) }))}">
      ${n.type === 'split' ? splitBody(n) : n.branches.map(b => branchRow(n, b)).join('')}
    </div>`;
}

function stationHTML(n, i, last) {
  return `<div class="station${last ? ' last' : ''}" role="listitem" data-node="${esc(n.id)}" style="--i:${i}">
      <button class="seq" type="button"${last ? '' : ' draggable="true" title="' + esc(t('p4.dragNode')) + '"'}
              aria-label="${esc(tf('p4.stepN', { n: i + 1, name: nodeName(n) }))}">${String(i + 1).padStart(2, '0')}</button>
      <div class="st-body">${bodyHTML(n)}</div>
    </div>`;
}

const newnodeRow = () =>
  `<div class="newnode-row"><button class="newnode" type="button" data-newnode>＋ ${esc(t('p4.newSplitNode'))}</button></div>`;
const passRow = () =>
  `<div class="pass" role="presentation">
      <span class="chev" aria-hidden="true">
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none"><path d="M5.5 1.5v8M2 6l3.5 3.5L9 6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <span class="pt">${esc(t('p4.passOn'))}</span>
    </div>`;

/* whole-tree innerHTML: ONCE at boot (and language switch) only */
function renderTree() {
  const flow = $('#flow');
  const parts = [];
  nodes.forEach((n, i) => {
    const last = i === nodes.length - 1;
    if (last) parts.push(newnodeRow());
    parts.push(stationHTML(n, i, last));
    if (!last) parts.push(passRow());
  });
  flow.innerHTML = parts.join('');
}

/* partial: one station only — add/delete conditions, value edits, … */
function renderStation(nodeId) {
  const n = findNode(nodeId);
  const el = $('#flow').querySelector(`[data-node="${CSS.escape(nodeId)}"]`);
  if (!n || !el) return;
  const body = el.querySelector('.st-body');
  if (body) body.innerHTML = bodyHTML(n);
}

/* after reorder/insert/remove: renumber, fix last markers, pass rows */
function syncFlowChrome() {
  const flow = $('#flow');
  const stations = $$('.station', flow);
  stations.forEach((el, i) => {
    const last = i === stations.length - 1;
    const n = findNode(el.dataset.node);
    el.classList.toggle('last', last);
    const seq = el.querySelector('.seq');
    if (seq) {
      seq.textContent = String(i + 1).padStart(2, '0');
      seq.setAttribute('aria-label', tf('p4.stepN', { n: i + 1, name: n ? nodeName(n) : '' }));
      if (last) { seq.removeAttribute('draggable'); seq.removeAttribute('title'); }
      else { seq.setAttribute('draggable', 'true'); seq.title = t('p4.dragNode'); }
    }
    const head = el.querySelector('.st-head');
    const del = head && head.querySelector('.st-del');
    if (last && del) del.remove();
    if (!last && !del && n) head.insertAdjacentHTML('beforeend', delBtnHTML(n));
  });
  $$('.pass', flow).forEach(p => p.remove());
  stations.slice(0, -1).forEach(el => el.insertAdjacentHTML('afterend', passRow()));
  const nn = flow.querySelector('.newnode-row');
  const lastEl = stations[stations.length - 1];
  if (nn && lastEl) lastEl.before(nn);
}

function insertStationEl(node, idx) {
  const flow = $('#flow');
  const ref = flow.querySelector('.station.last');
  if (ref) ref.insertAdjacentHTML('beforebegin', stationHTML(node, idx, false));
  const el = flow.querySelector(`[data-node="${CSS.escape(node.id)}"]`);
  if (el) el.style.setProperty('--i', 0);        // enter at once, no queue
  syncFlowChrome();
}
function removeStationEl(id) {
  const el = $('#flow').querySelector(`[data-node="${CSS.escape(id)}"]`);
  if (el) el.remove();
  syncFlowChrome();
}
/* move existing elements into tree order (no rebuild, no animation replay) */
function reorderFlowDOM() {
  const flow = $('#flow');
  const nn = flow.querySelector('.newnode-row');
  nodes.forEach(n => {
    const el = flow.querySelector(`[data-node="${CSS.escape(n.id)}"]`);
    if (el && nn) flow.insertBefore(el, nn);
  });
  syncFlowChrome();
}

function flashBranch(id) {
  const el = $('#flow').querySelector(`[data-branch="${CSS.escape(id)}"]`);
  if (!el) return;
  el.classList.add('is-new');
  setTimeout(() => el.classList.remove('is-new'), 1000);
}

/* ── toast with undo (spec §8.5: 6 s) ────────────────────────────────── */
let toastTimer = null;
function toast(msg, undo) {
  const el = $('#t4toast');
  el.innerHTML = `<span>${esc(msg)}</span>${undo ? '<button type="button">' + esc(t('p4.undo')) + '</button>' : ''}`;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), undo ? 6000 : 3600);
  if (undo) {
    el.querySelector('button').addEventListener('click', () => {
      undo();
      el.classList.remove('show');
    }, { once: true });
  }
}

/* ── mutations (undoable; dirty is content-based) ────────────────────── */
let savedJson = null;            // server state as of boot / last save

const currentJson = () => JSON.stringify(nodes.map(cleanNode));
function markDirty() {
  // undo often restores the saved content exactly — compare, don't assume
  dirty = savedJson == null || currentJson() !== savedJson;
  renderDirty();
}

function addNode(key, value) {
  const spec = sigSpec(key);
  // One root branch with the explicitly picked value: the node starts as a
  // single path (no +/− until a child layer is dragged in).
  const label = valLabel(key, value) || String(value);
  const branches = [{
    id: nid('b'),
    name: LANG === 'zh' ? label + ' ' + t('p4.branchWord')
                        : label + ' ' + t('p4.branchWord'),
    action: 'route', model: MODELS[0] || '', min_score: 0.5,
    rows: [{ signal: key, value }],
  }];
  const node = {
    id: nid('n'), type: 'split',
    name: (LANG === 'zh' ? spec.zh : (spec.en || spec.zh)) + ' ' + t('p4.typeSplit'),
    floor: 0.70, split_signal: key, branches,
  };
  nodes.splice(nodes.length - 1, 0, node);
  markDirty();
  insertStationEl(node, nodes.length - 2);
  syncPaletteUsed();
  const el = $('#flow').querySelector(`[data-node="${CSS.escape(node.id)}"]`);
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  toast(tf('p4.toastNodeAdded', { name: nodeName(node) }), () => {
    const i = nodes.findIndex(n => n.id === node.id);
    if (i >= 0) { nodes.splice(i, 1); markDirty(); removeStationEl(node.id); syncPaletteUsed(); }
  });
}

function moveNode(nodeId, beforeIdx) {
  const from = nodes.findIndex(n => n.id === nodeId);
  if (from < 0 || from === nodes.length - 1) return;
  const snapshot = nodes.map(n => n.id);
  const node = nodes[from];
  let to = beforeIdx > from ? beforeIdx - 1 : beforeIdx;
  to = Math.max(0, Math.min(to, nodes.length - 2));
  if (to === from) return;
  nodes.splice(from, 1);
  nodes.splice(to, 0, node);
  markDirty();
  reorderFlowDOM();
  toast(tf('p4.toastMoved', { name: nodeName(node), n: to + 1 }), () => {
    const byId = new Map(nodes.map(n => [n.id, n]));
    nodes.length = 0;
    snapshot.forEach(id => nodes.push(byId.get(id)));
    markDirty();
    reorderFlowDOM();
  });
}

function newEmptyRow(nodeType, signalKey) {
  const spec = sigSpec(signalKey);
  return nodeType === 'gate'
    ? { signal: signalKey, value: spec.free ? '' : null, conf_min: 0.5 }
    : { signal: signalKey, value: spec.free ? '' : null, weight: 0.5 };
}

/* drop a signal onto a leaf (spec §8.1): same signal on a single-valued
   signal clones the branch with a different value → next layer grows */
function addRow(branchId, signalKey) {
  const found = findBranch(branchId);
  if (!found) return;
  const { node, branch } = found;
  const spec = sigSpec(signalKey);
  if (branch.rows.some(r => r.signal === signalKey) && !spec.multi) {
    cloneBranch(node, branch, signalKey);
    setMode(null);
    return;
  }
  const row = newEmptyRow(node.type, signalKey);
  branch.rows.push(row);
  setMode(null);
  markDirty();
  renderStation(node.id);
  syncPaletteUsed();
  flashBranch(branchId);
  openRowEditor(branchId + ':' + (branch.rows.length - 1));
  // On a split node a signal only becomes a layer when every branch of the
  // group carries it; otherwise it stays a leaf condition. Say so, once.
  const siblingsMissing = node.type === 'split' && !spec.multi
    && node.branches.some(b => b.id !== branch.id
         && !b.rows.some(r => r.signal === signalKey));
  const msg = siblingsMissing
    ? tf('p4.toastRowAddedNoFork', { name: branchName(branch), sig: sigName(signalKey) })
    : tf('p4.toastRowAdded', { name: branchName(branch) });
  toast(msg, () => {
    branch.rows = branch.rows.filter(r => r !== row);
    markDirty();
    renderStation(node.id); syncPaletteUsed(); closePop();
  });
}

/* The group (leaf bucket) a branch lives in, plus the ancestor path. Used to
   scope "a value no sibling occupies" (SPEC-TREE-4 §8.1) to the group the
   clone actually lands in — node-wide scoping would block the legitimate
   single-branch-group case that grows the next layer. */
function branchGroup(node, branch) {
  let level = derive(node, node.branches, new Set());
  const used = new Set();
  while (level.groups) {
    const key = level.key;
    const row = branch.rows.find(r => r.signal === key);
    const value = row ? ((row.value || '') || null) : null;
    const g = level.groups.find(x => x.value === value) || level.groups[0];
    used.add(key);
    level = g.child;
  }
  return { leaves: level.leaves || [], used };
}

/* Leaf paths + remaining conditions, for the duplicate guard below. */
function leafEntries(node) {
  const out = [];
  const walk = (level, path, used) => {
    if (!level.groups) {
      level.leaves.forEach(b => out.push({ branch: b, path, used }));
      return;
    }
    const nextUsed = new Set(used); nextUsed.add(level.key);
    for (const g of level.groups)
      walk(g.child, path.concat([{ signal: level.key, value: g.value }]), nextUsed);
  };
  walk(derive(node, node.branches, new Set()), [], new Set());
  return out;
}

function wouldDuplicate(node, clone) {
  const entries = leafEntries(node);
  const mine = entries.find(e => e.branch === clone);
  if (!mine) return false;
  const sig = e => JSON.stringify([
    e.path,
    e.branch.rows.filter(r => !e.used.has(r.signal))
      .map(r => [r.signal, r.value, r.weight]),
    e.branch.min_score,
  ]);
  const mineSig = sig(mine);
  return entries.some(e => e.branch !== clone && sig(e) === mineSig);
}

/* Clone a branch for another value of one signal.
   explicitValue: undefined = pick the first free value automatically;
   null = leave the value 待选 (free-form signals, editor opens).
   Used by palette drops (same signal) and by the row-level [+] button. */
function cloneBranch(node, branch, signalKey, explicitValue) {
  const spec = sigSpec(signalKey);
  const cur = branch.rows.find(r => r.signal === signalKey);
  // siblings = branches in the group this branch belongs to (the clone keeps
  // that parent path, only this signal's value changes)
  const group = branchGroup(node, branch);
  const taken = new Set(group.leaves
    .filter(b => b.id !== branch.id)
    .map(b => (b.rows.find(r => r.signal === signalKey) || {}).value)
    .filter(Boolean));
  const vals = (spec.values || []).filter(([v]) => v !== cur.value && !taken.has(v));
  // Pick the first value whose clone would not be a dead duplicate under the
  // landing path (health's split_dead_branch); none left -> refuse and say why.
  let pickVal = explicitValue === null ? null : undefined;
  if (explicitValue === undefined) {
    for (const v of vals) {
      const probe = {
        id: '__probe__', name: '', action: branch.action, model: branch.model,
        min_score: branch.min_score,
        rows: branch.rows.map(r => r.signal === signalKey ? { ...r, value: v[0] } : { ...r }),
      };
      const trial = { ...node, branches: node.branches.concat([probe]) };
      if (!wouldDuplicate(trial, probe)) { pickVal = v; break; }
    }
  }
  if (pickVal === undefined || (pickVal === null && explicitValue === undefined)) {
    toast(tf('p4.cloneNoValue', { name: sigName(signalKey) }));
    return;
  }
  const copy = {
    id: nid('b'), name: LANG === 'zh'
      ? (pickVal ? pickVal[1] : spec.zh) + ' ' + t('p4.branchWord')
      : (pickVal ? (pickVal[2] || pickVal[1]) : spec.en) + ' ' + t('p4.branchWord'),
    action: branch.action, model: branch.model, min_score: branch.min_score,
    rows: branch.rows.map(r => r.signal === signalKey
      ? { ...r, value: pickVal ? pickVal[0] : '' }
      : { ...r }),
  };
  const at = node.branches.findIndex(b => b.id === branch.id);
  node.branches.splice(at + 1, 0, copy);
  markDirty();
  renderStation(node.id);
  syncPaletteUsed();
  flashBranch(copy.id);
  openRowEditor(copy.id + ':' + copy.rows.findIndex(r => r.signal === signalKey));
  toast(tf('p4.toastCloned', { name: sigName(signalKey), value: pickVal ? valLabel(signalKey, pickVal[0]) : t('p4.tbdValue') }),
    () => {
      node.branches = node.branches.filter(b => b.id !== copy.id);
      markDirty();
      renderStation(node.id); syncPaletteUsed(); closePop();
    });
}

/* Row-level [+]: derive a new branch from this row's FIRST condition signal
   (SPEC-TREE-4 §8.4 extension requested on 2026-10-19). The clone keeps the
   row's other conditions and only changes that signal's value, so the new
   branch appears at the same tree level the row lives on. */
function addBranchFrom(branchId) {
  const found = findBranch(branchId);
  if (!found) return;
  const { node, branch } = found;
  // the DEEPEST condition is the child node being edited: duplicating it and
  // changing its value adds a sibling under the same parent (tree editing).
  const last = branch.rows[branch.rows.length - 1];
  if (!last || !last.signal) {
    toast(t('p4.addBranchNoSignal'));
    return;
  }
  if (isFreeSig(last.signal)) cloneBranch(node, branch, last.signal, null);
  else cloneBranch(node, branch, last.signal);
}

function createBranch(nodeId, signalKey) {
  const node = findNode(nodeId);
  if (!node) return;
  const spec = sigSpec(signalKey);
  const rows = [];
  if (node.type === 'split') {
    // new branches land at the end of the first path's last group
    firstPath(node).forEach(p => rows.push({ signal: p.signal, value: p.value }));
  }
  // the picked signal may already be inherited from the path: re-target that
  // row instead of appending a duplicate signal (keeps one row per signal)
  const inherited = rows.find(r => r.signal === signalKey);
  if (inherited) inherited.value = '';
  else rows.push(newEmptyRow(node.type, signalKey));
  const b = {
    id: nid('b'),
    name: (LANG === 'zh' ? spec.zh : (spec.en || spec.zh)) + ' ' + t('p4.branchWord'),
    action: 'route', model: MODELS[0] || '', rows,
  };
  if (node.type !== 'gate') b.min_score = 0.5;
  node.branches.push(b);
  setMode(null);
  markDirty();
  renderStation(node.id);
  syncPaletteUsed();
  flashBranch(b.id);
  openRowEditor(b.id + ':' + rows.findIndex(r => r.signal === signalKey));
  toast(tf('p4.toastBranchAdded', { name: branchName(b) }), () => {
    node.branches = node.branches.filter(x => x.id !== b.id);
    markDirty();
    renderStation(node.id); syncPaletteUsed(); closePop();
  });
}

/* drop onto a group head: new branch inside that group, full ancestor path */
function addBranchToGroup(groupJson, signalKey) {
  let parsed;
  try { parsed = JSON.parse(groupJson); } catch (e) { return; }
  const node = findNode(parsed.node);
  if (!node) return;
  const path = parsed.path || [];
  const spec = sigSpec(signalKey);
  if (path.some(p => p.signal === signalKey)) {
    toast(tf('p4.toastGroupHas', { name: sigName(signalKey) }));
    return;
  }
  const rows = path.map(p => ({ signal: p.signal, value: p.value }));
  rows.push(newEmptyRow(node.type, signalKey));
  const b = {
    id: nid('b'),
    name: (LANG === 'zh' ? spec.zh : (spec.en || spec.zh)) + ' ' + t('p4.branchWord'),
    action: 'route', model: MODELS[0] || '', rows,
  };
  if (node.type !== 'gate') b.min_score = 0.5;
  node.branches.push(b);
  markDirty();
  renderStation(node.id);
  syncPaletteUsed();
  flashBranch(b.id);
  openRowEditor(b.id + ':' + (rows.length - 1));
  toast(tf('p4.toastBranchInGroup', { path: path.map(p => valLabel(p.signal, p.value)).join(' / ') }), () => {
    node.branches = node.branches.filter(x => x.id !== b.id);
    markDirty();
    renderStation(node.id); syncPaletteUsed(); closePop();
  });
}

function deleteNode(nodeId) {
  const idx = nodes.findIndex(n => n.id === nodeId);
  if (idx < 0) return;
  if (idx === nodes.length - 1) {
    toast(t('p4.toastNoDeleteLast'));
    return;
  }
  const [node] = nodes.splice(idx, 1);
  closePop();
  markDirty();
  removeStationEl(nodeId);
  syncPaletteUsed();
  toast(tf('p4.toastNodeDeleted', { name: nodeName(node) }), () => {
    nodes.splice(idx, 0, node);
    markDirty();
    insertStationEl(node, idx);
    syncPaletteUsed();
  });
}

function deleteBranch(bid) {
  const found = findBranch(bid);
  if (!found) return;
  const { node, branch } = found;
  if (isLastNode(node) && node.branches.length === 1) {
    toast(t('p4.toastNoDeleteLastBranch'));
    return;
  }
  const at = node.branches.findIndex(b => b.id === bid);
  node.branches.splice(at, 1);
  closePop();
  markDirty();
  renderStation(node.id);
  syncPaletteUsed();
  toast(tf('p4.toastBranchDeleted', { name: branchName(branch) }), () => {
    node.branches.splice(at, 0, branch);
    markDirty();
    renderStation(node.id); syncPaletteUsed();
  });
}

/* ── pick / drag state machine ───────────────────────────────────────── */
function syncPickA11y() {
  $$('#flow .branch').forEach(br => {
    if (mode === 'pick') {
      br.tabIndex = 0;
      const name = br.querySelector('.bname');
      br.setAttribute('aria-label', tf('p4.a11yDropOn', { name: name ? name.textContent : '' }));
    } else {
      br.removeAttribute('tabindex');
      br.removeAttribute('aria-label');
    }
  });
}
function setMode(next) {
  mode = next;
  const flow = $('#flow');
  flow.classList.toggle('dropping', next === 'drag');
  flow.classList.toggle('picking', next === 'pick');
  if (!next) {
    pick = { signal: null, node: null };
  }
  syncPickA11y();
  syncPaletteSelection();
  syncPalHint();
}
function setPickSignal(key) {
  pick.signal = key;
  mode = 'pick';
  $('#flow').classList.add('picking');
  syncPickA11y();
  syncPaletteSelection();
  syncPalHint();
}
function setPickNode(nodeId) {
  pick.node = nodeId;
  mode = 'pick';
  $('#flow').classList.add('picking');
  setPalOpen(true);
  syncPickA11y();
  syncPaletteSelection();
  syncPalHint();
  const node = findNode(nodeId);
  const path = node && node.type === 'split' ? firstPath(node) : [];
  toast(path.length
    ? tf('p4.toastPickSignalAt', { path: path.map(p => valLabel(p.signal, p.value) || t('p4.pending')).join(' → ') })
    : t('p4.toastPickSignal'));
}

/* ── popovers ────────────────────────────────────────────────────────── */
function rangeBlock(id, label, value, step, hint) {
  return `<div class="pop-range">
      <div class="pr-head">
        <label for="${id}Num">${esc(label)}</label>
        <input class="pr-num" id="${id}Num" type="number" inputmode="decimal"
               min="0" max="1" step="${step}" value="${Number(value).toFixed(2)}">
      </div>
      <input class="pr-track" id="${id}Range" type="range" min="0" max="1" step="${step}"
             value="${Number(value)}" aria-label="${esc(label)}" style="--p:${Number(value) * 100}%">
      <div class="pr-scale" aria-hidden="true"><span>0</span><span>1</span></div>
      ${hint ? `<p class="pr-hint">${esc(hint)}</p>` : ''}
    </div>`;
}

function wireRange(id, { onInput, onCommit }) {
  const num = $('#' + id + 'Num'), rng = $('#' + id + 'Range');
  const paint = v => rng.style.setProperty('--p', (v * 100) + '%');
  const clamp = v => Math.min(1, Math.max(0, v));
  let last = parseFloat(rng.value);
  paint(last);
  const set = (v, src) => {
    v = clamp(v);
    last = v;
    rng.value = v;
    paint(v);
    if (src !== 'num') num.value = v.toFixed(2);
    if (src === 'commit') onCommit(v); else onInput(v);
  };
  rng.addEventListener('input', e => set(parseFloat(e.target.value), 'range'));
  rng.addEventListener('change', e => set(parseFloat(e.target.value), 'commit'));
  num.addEventListener('input', e => {
    const v = parseFloat(e.target.value);
    if (!Number.isNaN(v)) set(v, 'num');
  });
  num.addEventListener('blur', () => {
    const v = parseFloat(num.value);
    set(Number.isNaN(v) ? last : v, 'commit');
  });
  num.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); num.blur(); }
  });
}

function openRowEditor(key) {
  const [bid, idx] = key.split(':');
  const found = findBranch(bid);
  if (!found) return;
  const { node, branch } = found;
  const row = branch.rows[Number(idx)];
  if (!row) return;
  const spec = sigSpec(row.signal);
  const isScore = node.type !== 'gate';      // split leaves weight like score
  popTarget = { kind: 'row', bid, idx: Number(idx) };

  const datalist = row.signal === 'keyword' && KEYWORD_GROUPS.length
    ? `<datalist id="kwVals">${KEYWORD_GROUPS.map(g => `<option value="${esc(g)}">`).join('')}</datalist>` : '';
  const valBlock = isFreeSig(row.signal)
    ? `<p class="pop-label">${esc(t('p4.value'))}</p>
       <input class="pop-text" id="popValText" value="${esc(row.value || '')}" list="${row.signal === 'keyword' && KEYWORD_GROUPS.length ? 'kwVals' : ''}"
              placeholder="${esc(t('p4.freePlaceholder'))}">${datalist}`
    : `<p class="pop-label">${esc(t('p4.value'))}</p>
       <div class="pop-vals" id="popVals">
         ${(spec.values || []).map(([k, zh, en]) =>
            `<button data-val="${esc(k)}" class="${row.value === k ? 'on' : ''}">${esc(LANG === 'zh' ? zh : (en || zh))}</button>`).join('')}
       </div>`;
  const numLabel = isScore ? t('p4.weight') : t('p4.confMin');
  const numVal = isScore ? (row.weight != null ? row.weight : 0.5) : (row.conf_min != null ? row.conf_min : 0.5);
  const numStep = isScore ? 0.05 : 0.01;
  const chipText = v => isScore ? '×' + (+v.toFixed(2)) : t('p4.confFloor') + v.toFixed(2);

  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `
    <div class="pop-head">
      <b>${esc(sigName(row.signal))}</b><span class="sub">${esc(branchName(branch))}</span>
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
    ${valBlock}
    ${isRuleSig(row.signal) && !isScore
      ? `<p class="pop-note">${esc(t('p4.ruleNote'))}</p>`
      : rangeBlock('popVal', numLabel, numVal, numStep,
          isScore ? t('p4.weightHint') : t('p4.confHint'))}
    <div class="pop-foot">
      <button class="pop-del" data-popdel="${esc(key)}">${esc(t('p4.delCond'))}</button>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop($('#flow').querySelector(`[data-row="${CSS.escape(key)}"]`));

  if (isFreeSig(row.signal)) {
    $('#popValText').addEventListener('input', e => {
      row.value = e.target.value;
      markDirty();
      renderTreeSoft(bid);
    });
  }
  if (!(isRuleSig(row.signal) && !isScore)) {
    wireRange('popVal', {
      onInput(v) {
        if (isScore) row.weight = v; else row.conf_min = v;
        const el = $('#flow').querySelector(`[data-row="${CSS.escape(key)}"] .k`);
        if (el) el.textContent = chipText(v);
      },
      onCommit() { markDirty(); renderTreeSoft(); }
    });
  }
}

function openBranchEditor(bid) {
  const found = findBranch(bid);
  if (!found) return;
  const { node, branch } = found;
  const hasMinScore = node.type !== 'gate';
  popTarget = { kind: 'branch', bid };
  const modelOptions = (MODELS.includes(branch.model) || !branch.model
    ? MODELS : [branch.model, ...MODELS]).map(m =>
      `<option value="${esc(m)}" ${m === (branch.model || '') ? 'selected' : ''}>${esc(m)}</option>`).join('');
  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `
    <div class="pop-head">
      <b>${esc(t('p4.branchCfg'))}</b><span class="sub">${esc(nodeName(node))}</span>
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
    <p class="pop-label">${esc(t('p4.name'))}</p>
    <input class="pop-text" id="bName" value="${esc(branch.name || '')}">
    <p class="pop-label">${esc(t('p4.afterHit'))}</p>
    <div class="pop-vals" id="bAction">
      <button data-act="route" class="${branch.action !== 'block' ? 'on' : ''}">${esc(t('p4.routeModel'))}</button>
      <button data-act="block" class="${branch.action === 'block' ? 'on' : ''}">${esc(t('p4.blockNow'))}</button>
    </div>
    ${branch.action !== 'block' ? `
      <p class="pop-label">${esc(t('p4.model'))}</p>
      <input class="pop-text" id="bModel" list="modelList" value="${esc(branch.model || '')}" placeholder="qwen3-8b-medical">
      <datalist id="modelList">${MODELS.map(m => `<option value="${esc(m)}">`).join('')}</datalist>` : ''}
    ${hasMinScore ? rangeBlock('bMin', t('p4.minScore'), branch.min_score != null ? branch.min_score : 0.5, 0.05, t('p4.minScoreHint')) : ''}
    <div class="pop-foot">
      <button class="pop-del" data-bdel="${esc(bid)}">${esc(t('p4.delBranch'))}</button>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop($('#flow').querySelector(`[data-branch="${CSS.escape(bid)}"] .out`));

  $('#bName').addEventListener('input', e => {
    branch.name = e.target.value;
    markDirty();
    renderTreeSoft();
  });
  const bModel = $('#bModel');
  if (bModel) bModel.addEventListener('input', e => {
    branch.model = e.target.value.trim();
    markDirty();
    renderTreeSoft();
  });
  if (hasMinScore) wireRange('bMin', {
    onInput(v) {
      branch.min_score = v;
      const el = $('#flow').querySelector(`[data-branch="${CSS.escape(bid)}"] .min-s`);
      if (el) el.textContent = t('p4.minScore') + ' ' + v.toFixed(2);
    },
    onCommit() { markDirty(); renderTreeSoft(); }
  });
  $('#bAction').addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    branch.action = b.dataset.act;
    if (branch.action === 'route' && !branch.model) branch.model = MODELS[0] || '';
    markDirty();
    renderTreeSoft();
    openBranchEditor(bid);   // action switch changes the form shape
  });
}

/* group settings: fork key + entry floor; opens ABOVE the target (§7.4) */
function openLevelEditor(nodeId) {
  const node = findNode(nodeId);
  if (!node) return;
  const level = derive(node, node.branches, new Set());
  const key = level.key || (node.split_signal || null);
  const present = [...new Set(node.branches.flatMap(b => b.rows.map(r => r.signal)))]
    .filter(k => !isFreeSig(k));
  popTarget = { kind: 'level', nid: nodeId };
  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `
    <div class="pop-head">
      <b>${esc(t('p4.groupCfg'))}</b><span class="sub">${esc(nodeName(node))}</span>
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
    <p class="pop-label">${esc(t('p4.groupKey'))}</p>
    <div class="pop-vals" id="lvSigs">
      ${present.map(k => `<button data-lv="${esc(k)}" class="${k === key ? 'on' : ''}">${esc(sigName(k))}</button>`).join('')}
    </div>
    ${key && isRuleSig(key)
      ? `<p class="pop-note">${esc(tf('p4.ruleKeyNote', { name: sigName(key) }))}</p>`
      : rangeBlock('lv', t('p4.floor'), node.floor != null ? node.floor : 0.70, 0.05, t('p4.floorHint'))}
    <div class="pop-foot">
      <span class="pop-note">${esc(t('p4.groupNote'))}</span>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop(popAnchor(), true);   // above, so the reshaping tree stays visible

  $('#lvSigs').addEventListener('click', e => {
    const b = e.target.closest('[data-lv]');
    if (!b) return;
    node.split_signal = b.dataset.lv;
    markDirty();
    renderTreeSoft();
    openLevelEditor(nodeId);
  });
  if (!(key && isRuleSig(key))) wireRange('lv', {
    onInput(v) {
      node.floor = v;
      $$(`[data-node="${CSS.escape(nodeId)}"] .floor`).forEach(el => {
        el.textContent = t('p4.confFloor') + v.toFixed(2);
      });
    },
    onCommit() { markDirty(); renderTreeSoft(); }
  });
}

/* new split node: every enumerable signal is a candidate (§8.4) */
function openNewNode() {
  popTarget = { kind: 'new' };
  renderNewNodeSignals();
}

/* Step 1: which signal roots this node. */
function renderNewNodeSignals() {
  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `
    <div class="pop-head">
      <b>${esc(t('p4.newSplitNode'))}</b><span class="sub">${esc(t('p4.beforeFallback'))}</span>
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
    <p class="pop-label">${esc(t('p4.forkBy'))}</p>
    <div class="pop-vals" id="nnSigs">
      ${Object.values(SIG).filter(s => !isFreeSig(s.key)).map(s =>
        `<button data-nn="${esc(s.key)}">${esc(sigName(s.key))}</button>`).join('')}
    </div>
    <div class="pop-foot"><span class="pop-note">${esc(t('p4.newNodeNote'))}</span></div>`;
  placePop($('#flow').querySelector('[data-newnode]'), true);
  $('#nnSigs').addEventListener('click', e => {
    const b = e.target.closest('[data-nn]');
    if (!b) return;
    renderNewNodeValues(b.dataset.nn);
  });
}

/* Step 2: pick the root value explicitly (e.g. 学科 = 医疗). */
function renderNewNodeValues(key) {
  const spec = sigSpec(key);
  const pop = $('#pop');
  pop.innerHTML = `
    <div class="pop-head">
      <b>${esc(tf('p4.pickRootValue', { sig: sigName(key) }))}</b>
      <span class="sub">${esc(t('p4.newSplitNode'))}</span>
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
    <div class="pop-vals" id="nnVals">
      ${(spec.values || []).map(([v, zh, en]) =>
        `<button data-nv="${esc(v)}">${esc(LANG === 'zh' ? zh : (en || zh))}</button>`).join('')}
    </div>
    <div class="pop-foot">
      <button class="pop-del" id="nnBack" style="color:var(--t4-ink-3)">← ${esc(t('p4.back'))}</button>
      <span class="pop-note">${esc(t('p4.newNodeNote'))}</span>
    </div>`;
  placePop($('#flow').querySelector('[data-newnode]'), true);
  $('#nnVals').addEventListener('click', e => {
    const b = e.target.closest('[data-nv]');
    if (!b) return;
    closePop();
    addNode(key, b.dataset.nv);
  });
  $('#nnBack').addEventListener('click', renderNewNodeSignals);
}

function popAnchor() {
  if (!popTarget) return null;
  if (popTarget.kind === 'row') return $('#flow').querySelector(`[data-row="${CSS.escape(popTarget.bid + ':' + popTarget.idx)}"]`);
  if (popTarget.kind === 'level') {
    const el = $('#flow').querySelector(`[data-node="${CSS.escape(popTarget.nid)}"] .ghead .floor, [data-node="${CSS.escape(popTarget.nid)}"] .ghead .gdet`);
    return el || $('#flow').querySelector(`[data-node="${CSS.escape(popTarget.nid)}"] .ghead`);
  }
  if (popTarget.kind === 'new') return $('#flow').querySelector('[data-newnode]');
  return $('#flow').querySelector(`[data-branch="${CSS.escape(popTarget.bid)}"] .out`);
}

/* soft redraw: swap one station, keep the popover + its focus */
function renderTreeSoft() {
  if (!popTarget) return;
  let nid = null;
  if (popTarget.kind === 'row' || popTarget.kind === 'branch') {
    const f = findBranch(popTarget.bid);
    nid = f ? f.node.id : null;
  } else if (popTarget.kind === 'level') {
    nid = popTarget.nid;
  }
  if (nid) renderStation(nid);
  const anchor = popAnchor();
  if (anchor) placePop(anchor, popTarget.kind === 'level'); else closePop();
}

function placePop(anchor, preferTop) {
  const pop = $('#pop');
  if (!anchor) return;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
  let top = r.bottom + 8;
  if (preferTop) top = Math.max(12, r.top - h - 8);
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 8);
  if (top < 12) top = Math.min(r.bottom + 8, window.innerHeight - h - 12);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}
function closePop() { const pop = $('#pop'); pop.hidden = true; pop.innerHTML = ''; popTarget = null; }

/* ── flow event delegation ───────────────────────────────────────────── */
function bindFlow() {
  const flow = $('#flow');

  flow.addEventListener('click', e => {
    const stAdd = e.target.closest('[data-addbranch]');
    if (stAdd) { setPickNode(stAdd.dataset.addbranch); return; }
    const delNode = e.target.closest('[data-delnode]');
    if (delNode) { deleteNode(delNode.dataset.delnode); return; }
    const addFrom = e.target.closest('[data-addbranchfrom]');
    if (addFrom) { addBranchFrom(addFrom.dataset.addbranchfrom); return; }
    const del = e.target.closest('[data-delbranch]');
    if (del) { deleteBranch(del.dataset.delbranch); return; }
    const nn = e.target.closest('[data-newnode]');
    if (nn) { openNewNode(); return; }
    const lvl = e.target.closest('[data-level]');
    if (lvl) { openLevelEditor(lvl.dataset.level); return; }
    const cfg = e.target.closest('[data-branchcfg]');
    if (cfg) { openBranchEditor(cfg.dataset.branchcfg); return; }
    const rowEl = e.target.closest('[data-row]');
    if (rowEl) { openRowEditor(rowEl.dataset.row); return; }
    const br = e.target.closest('.branch');
    if (br && mode === 'pick' && pick.signal) { addRow(br.dataset.branch, pick.signal); return; }
    if (br && mode !== 'pick') closePop();
  });

  flow.addEventListener('keydown', e => {
    if (mode === 'pick' && pick.signal) {
      const br = e.target.closest('.branch');
      if (br && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        addRow(br.dataset.branch, pick.signal);
      }
    }
    const seq = e.target.closest('.seq');
    if (seq && e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      const id = seq.closest('.station').dataset.node;
      const idx = nodes.findIndex(n => n.id === id);
      if (idx < 0 || idx === nodes.length - 1) return;
      e.preventDefault();
      moveNode(id, e.key === 'ArrowUp' ? idx - 1 : idx + 2);
      const again = $('#flow').querySelector(`[data-node="${CSS.escape(id)}"] .seq`);
      if (again) again.focus();
    }
  });

  /* palette drag → branch / group head; seq drag → station reorder */
  flow.addEventListener('dragstart', e => {
    const seq = e.target.closest('.seq');
    if (seq) {
      const st = seq.closest('.station');
      const id = st.dataset.node;
      if (id === nodes[nodes.length - 1].id) { e.preventDefault(); return; }
      dragNodeId = id;
      st.classList.add('moving');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', 'node:' + id);
      return;
    }
  });
  flow.addEventListener('dragend', () => {
    if (!dragNodeId) return;
    dragNodeId = null;
    clearNodeDrop();
    $$('.station.moving', flow).forEach(el => el.classList.remove('moving'));
  });

  const clearHot = () =>
    $$('.branch.hot, .ghead.hot', flow).forEach(el => el.classList.remove('hot'));

  flow.addEventListener('dragover', e => {
    if (dragNodeId) {
      const st = e.target.closest('.station');
      if (!st) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const r = st.getBoundingClientRect();
      const before = e.clientY < r.top + r.height / 2;
      if (!nodeDrop || nodeDrop.id !== st.dataset.node || nodeDrop.before !== before) {
        clearNodeDrop();
        st.classList.add(before ? 'drop-before' : 'drop-after');
        nodeDrop = { id: st.dataset.node, before };
      }
      return;
    }
    if (!dragSig) return;
    const gh = e.target.closest('[data-group]');
    if (gh) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      clearHot();
      gh.classList.add('hot');
      return;
    }
    const br = e.target.closest('.branch');
    if (!br) return;                    // only branch rows + group heads accept
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    clearHot();
    br.classList.add('hot');
  });

  flow.addEventListener('dragleave', e => {
    if (!flow.contains(e.relatedTarget)) clearHot();
  });

  flow.addEventListener('drop', e => {
    if (dragNodeId) {
      e.preventDefault();
      const id = dragNodeId, target = nodeDrop;
      dragNodeId = null;
      clearNodeDrop();
      $$('.station.moving', flow).forEach(el => el.classList.remove('moving'));
      if (target) {
        const idx = nodes.findIndex(n => n.id === target.id);
        if (idx >= 0) moveNode(id, target.before ? idx : idx + 1);
      }
      return;
    }
    if (!dragSig) return;
    e.preventDefault();
    const gh = e.target.closest('[data-group]');
    if (gh) {
      const s0 = dragSig;
      dragSig = null;
      clearHot();
      setMode(null);
      addBranchToGroup(gh.dataset.group, s0);
      return;
    }
    const br = e.target.closest('.branch');
    const sig = dragSig;
    dragSig = null;
    clearHot();
    if (br) addRow(br.dataset.branch, sig);
    else setMode(null);
  });

  function clearNodeDrop() {
    $$('.station.drop-before, .station.drop-after', flow)
      .forEach(el => el.classList.remove('drop-before', 'drop-after'));
    nodeDrop = null;
  }
}

function bindPalette() {
  const palBody = $('#palBody');
  palBody.addEventListener('dragstart', e => {
    const sig = e.target.closest('.sig');
    if (!sig) return;
    dragSig = sig.dataset.sig;
    sig.classList.add('dragging');
    mode = 'drag';
    $('#flow').classList.add('dropping');
    syncPalHint();
    e.dataTransfer.effectAllowed = 'copy';
    e.dataTransfer.setData('text/plain', dragSig);
    const ghost = document.createElement('div');
    ghost.className = 'drag-ghost';
    ghost.textContent = sigName(dragSig);
    ghost.style.cssText += 'position:fixed;top:-100px;left:-100px;';
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, 24, 16);
    setTimeout(() => ghost.remove(), 0);
  });
  palBody.addEventListener('dragend', () => {
    const s = palBody.querySelector('.sig.dragging');
    if (s) s.classList.remove('dragging');
    dragSig = null;
    if (mode === 'drag') setMode(null);
    $$('.branch.hot, .ghead.hot').forEach(el => el.classList.remove('hot'));
  });
  palBody.addEventListener('click', e => {
    const sig = e.target.closest('.sig');
    if (!sig) return;
    const key = sig.dataset.sig;
    if (pick.node) { createBranch(pick.node, key); return; }
    if (pick.signal === key) { setMode(null); return; }
    setPickSignal(key);
  });
  palBody.addEventListener('scroll', syncPalFade, { passive: true });

  const palToggle = $('#palToggle');
  palToggle.addEventListener('click', () =>
    setPalOpen($('#palette').classList.contains('closed')));
}
function setPalOpen(open) {
  $('#palette').classList.toggle('closed', !open);
  $('#palToggle').setAttribute('aria-expanded', String(open));
}

/* popover-internal clicks (values, deletes) */
function bindPop() {
  const pop = $('#pop');
  pop.addEventListener('click', e => {
    if (e.target.closest('[data-popclose]')) { closePop(); return; }
    const val = e.target.closest('[data-val]');
    if (val && popTarget && popTarget.kind === 'row') {
      const found = findBranch(popTarget.bid);
      if (found) {
        const row = found.branch.rows[popTarget.idx];
        row.value = val.dataset.val;
        markDirty();
        $$('#popVals button').forEach(x => x.classList.toggle('on', x === val));
        renderTreeSoft();
      }
      return;
    }
    const del = e.target.closest('[data-popdel]');
    if (del) {
      const [bid, idx] = del.dataset.popdel.split(':');
      const found = findBranch(bid);
      if (!found) return;
      const removed = found.branch.rows.splice(Number(idx), 1)[0];
      closePop();
      markDirty();
      renderStation(found.node.id);
      syncPaletteUsed();
      toast(t('p4.toastCondDeleted'), () => {
        found.branch.rows.splice(Number(idx), 0, removed);
        markDirty();
        renderStation(found.node.id); syncPaletteUsed();
      });
      return;
    }
    const bdel = e.target.closest('[data-bdel]');
    if (bdel) deleteBranch(bdel.dataset.bdel);
  });

  document.addEventListener('mousedown', e => {
    if (pop.hidden) return;
    if (pop.contains(e.target)) return;
    if (e.target.closest('[data-row]') || e.target.closest('[data-branchcfg]')) return;
    closePop();
  });
}

/* ── try drawer ──────────────────────────────────────────────────────── */
let tryBusy = false;
function openSim() {
  document.body.classList.add('sim-open');
  $('#sim').setAttribute('aria-hidden', 'false');
  renderSimText();
  $('#simText').focus();
}
function closeSim() {
  document.body.classList.remove('sim-open');
  $('#sim').setAttribute('aria-hidden', 'true');
}
function renderSimText() {
  $('#simText').placeholder = t('p4.simPlaceholder');
  $('#simRun').textContent = t('p4.simRun');
  $('#simEx').innerHTML = [
    t('p4.exMedical'), t('p4.exCode'), t('p4.exJail'), t('p4.exFact'),
  ].map(x => `<button type="button">${esc(x)}</button>`).join('');
  if (!lastTry) {
    $('#simOut').innerHTML = `<b>${esc(t('p4.simEmptyTitle'))}</b>${esc(t('p4.simEmptyBody'))}`;
  }
}
function stepText(s) {
  const conf = s.conf === 1.0 && s.floor == null
    ? t('p4.deterministic')
    : pct(s.conf) + (s.floor != null ? ' ' + t('p4.confFloor') + s.floor.toFixed(2) : '');
  return `${sigName(s.signal)} = ${esc(valLabel(s.signal, s.value))} · ${conf}${s.pass ? '' : ' · ' + t('p4.cut')}`;
}
function renderTry() {
  const out = $('#simOut');
  if (!lastTry) { renderSimText(); return; }
  const d = lastTry.decision || {};
  const cls = d.kind === 'block' ? 'block' : (d.kind === 'route' ? 'route' : 'none');
  const badge = d.kind === 'route' ? t('pipe.try.finalhit')
    : d.kind === 'block' ? t('pipe.try.finalblock') : t('pipe.try.finalnone');
  const label = d.kind === 'block' ? '✕ ' + t('pipe.try.finalblock')
    : d.kind === 'route' ? '→ ' + (d.model || '—')
      : t('pipe.try.finalnone');
  const node = d.node_id ? findNode(d.node_id) : null;
  const branch = d.branch_id && node
    ? (node.branches || []).find(b => b.id === d.branch_id) : null;
  let html = `<div class="final ${cls}">
      <div><div class="m">${esc(label)}</div>
      <div class="via">${esc(node ? nodeName(node) : '')}${branch ? ' · ' + esc(branchName(branch)) : ''}${d.score != null ? ' · score ' + d.score.toFixed(2) : ''}</div></div>
      <span class="badge">${esc(badge)}</span></div>`;
  html += `<div class="col-h">${esc(t('pipe.try.trace'))}</div>`;
  (lastTry.trace || []).forEach((r, i) => {
    const hit = r.result === 'hit';
    let body = '';
    if (r.type === 'split') {
      const steps = (r.path || []).map(s =>
        `<div class="pstep${s.pass ? '' : ' cut'}">${esc(stepText(s))}</div>`).join('');
      body = `<div class="pathrow">${steps || `<div class="pstep cut">${esc(t('p4.noGroupMatch'))}</div>`}</div>`;
      const win = (r.board || []).find(b => b.winner);
      if (r.path && r.path.length && (r.board || []).length) {
        body += `<div class="board" style="margin-top:5px">${r.board.map(b =>
          `<div class="brow${b.winner ? ' win' : ''}">${b.winner ? '★ ' : ''}${esc(b.name || b.branch_id)}
             <span class="mono">${b.sum_w > 0 ? 'score ' + b.score.toFixed(2) + ' ≥ ' + (b.min_score != null ? b.min_score.toFixed(2) : '0') : t('p4.hitRoute')}</span></div>`).join('')}</div>`;
      } else if (hit && !r.board.length) {
        body += `<div class="brow win" style="margin-top:5px">${esc(t('p4.hitRoute'))}</div>`;
      }
    } else if (r.type === 'gate') {
      body = r.branch
        ? esc(tf('p4.gateHit', { name: r.branch.name || '', to: r.branch.action === 'block' ? t('pipe.block') : (r.branch.model || '') }))
        : esc(t('p4.gateMiss'));
    } else if (Array.isArray(r.board)) {
      const w = r.board.find(b => b.winner);
      body = w
        ? esc(tf('p4.scoreWin', { name: w.name || '', score: fmt(w.score) }))
        : esc(t('p4.scoreNone'));
    }
    html += `<div class="pstep" style="align-items:flex-start;flex-direction:column">
        <div style="display:flex;gap:7px;align-items:center;width:100%">
          <span class="mono">${i + 1}</span><b>${esc(r.name || r.node_id)}</b>
          <span class="mono" style="color:${hit ? 'var(--t4-green)' : 'var(--t4-ink-3)'}">${hit ? t('pipe.hit') : t('pipe.miss')}</span>
        </div>
        <div style="width:100%;margin-top:4px">${body}</div>
      </div>`;
  });
  const pack = lastTry.signals || {};
  const chips = Object.entries(pack).filter(([, s]) => !s.is_rule).slice(0, 12).map(([k, s]) => {
    let v = s.value;
    if (Array.isArray(v)) v = v.length ? v.join(',') : '—';
    else if (v == null) v = '—';
    const conf = s.confidence != null ? ' ' + pct(s.confidence) : '';
    return `<span>${esc(sigName(k))} ${esc(String(v))}${esc(conf)}</span>`;
  }).join('');
  html += `<div class="col-h" style="margin-top:4px">${esc(t('p4.signals'))}</div>
    <div class="sigchips">${chips}</div>`;
  $('#simOut').innerHTML = html;
}

async function runTry() {
  const text = $('#simText').value.trim();
  if (!text) { $('#simText').focus(); return; }
  if (tryBusy) return;
  tryBusy = true;
  $('#simRun').disabled = true;
  try {
    lastTry = await api.post('/admin/try', { text });
    renderTry();
  } catch (e) {
    $('#simOut').innerHTML = `<b>${esc(t('p4.tryFail'))}</b>${esc(Array.isArray(e.detail) ? JSON.stringify(e.detail) : (e.detail || e.message))}`;
  }
  tryBusy = false;
  $('#simRun').disabled = false;
}

/* ── save ────────────────────────────────────────────────────────────── */
function cleanNode(n) {
  return {
    id: n.id, type: n.type, name: n.name || '',
    floor: n.type === 'split' ? (n.floor != null ? n.floor : null) : null,
    split_signal: n.type === 'split' ? (n.split_signal || null) : null,
    branches: (n.branches || []).map(b => ({
      id: b.id, name: b.name || '', action: b.action || 'route',
      model: b.model || null, min_score: b.min_score != null ? b.min_score : null,
      rows: (b.rows || []).map(r => ({
        signal: r.signal, value: String(r.value == null ? '' : r.value),
        weight: r.weight != null ? r.weight : null,
        conf_min: r.conf_min != null ? r.conf_min : null,
      })),
    })),
  };
}

function showIssues(list, boxId) {
  const box = $(boxId);
  if (!list || !list.length) { box.innerHTML = ''; return; }
  box.innerHTML = list.map(i => {
    const msg = LANG === 'zh' ? (i.zh || i.code || JSON.stringify(i)) : (i.en || i.code || JSON.stringify(i));
    return `<div class="iv">• ${esc(msg)}</div>`;
  }).join('');
}

/* ── export / import (config as code; SQLite stays the store of record) ── */
function exportPipeline() {
  const body = {
    version,
    exported_at: new Date().toISOString(),
    nodes: nodes.map(cleanNode),
  };
  const blob = new Blob([JSON.stringify(body, null, 2) + '\n'],
                        { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `pipeline-v${version}.json`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  toast(tf('p4.exportOk', { name: a.download }));
}

/* Accept either {"nodes":[…]} (what export writes / GET returns) or a bare
   node array. Shape errors name the offending node/branch so a hand-edited
   file is easy to fix. The server still runs the full health check on save. */
function normalizeImported(raw) {
  const list = Array.isArray(raw) ? raw
    : (raw && Array.isArray(raw.nodes) ? raw.nodes : null);
  if (!list) throw new Error(t('p4.importNoNodes'));
  const nodeIds = new Set();
  const branchIds = new Set();
  return list.map((n, i) => {
    const where = t('p4.importRow') + ' #' + (i + 1);
    if (!n || typeof n !== 'object') throw new Error(where);
    if (!['gate', 'score', 'split'].includes(n.type))
      throw new Error(where + ': type=' + n.type);
    if (!Array.isArray(n.branches)) throw new Error(where + ': branches');
    let id = String(n.id || nid('n'));
    if (nodeIds.has(id)) throw new Error(where + ': duplicate id ' + id);
    nodeIds.add(id);
    return {
      id, type: n.type, name: String(n.name || ''),
      floor: n.type === 'split' && n.floor != null ? Number(n.floor) : null,
      split_signal: n.type === 'split' ? (n.split_signal || null) : null,
      branches: n.branches.map((b, j) => {
        const bwhere = where + ' / ' + t('p4.importBranch') + ' #' + (j + 1);
        if (!b || typeof b !== 'object') throw new Error(bwhere);
        if (!Array.isArray(b.rows)) throw new Error(bwhere + ': rows');
        let bid = String(b.id || nid('b'));
        if (branchIds.has(bid)) throw new Error(bwhere + ': duplicate id ' + bid);
        branchIds.add(bid);
        return {
          id: bid, name: String(b.name || ''),
          action: b.action === 'block' ? 'block' : 'route',
          model: b.model || null,
          min_score: b.min_score != null ? Number(b.min_score) : null,
          rows: b.rows.map((r, k) => {
            if (!r || typeof r.signal !== 'string')
              throw new Error(bwhere + ' / ' + t('p4.importCond') + ' #' + (k + 1));
            return {
              signal: r.signal,
              value: r.value == null ? '' : String(r.value),
              weight: r.weight != null ? Number(r.weight) : null,
              conf_min: r.conf_min != null ? Number(r.conf_min) : null,
            };
          }),
        };
      }),
    };
  });
}

async function importPipeline(file) {
  try {
    const raw = JSON.parse(await file.text());
    const imported = normalizeImported(raw);
    if (!imported.length) throw new Error(t('p4.importEmpty'));
    nodes.length = 0;
    imported.forEach(n => nodes.push(n));
    markDirty();               // baseline unchanged: save is still required
    renderAll();               // whole-document replace is what import IS
    toast(tf('p4.importOk', { n: imported.length }));
  } catch (e) {
    toast(tf('p4.importBad', { err: e.message }));
  } finally {
    $('#importFile').value = '';
  }
}

async function save() {
  try {
    const out = await api.put('/admin/pipeline', { nodes: nodes.map(cleanNode) });
    showIssues([], '#issues');
    showIssues(out.warnings || [], '#warnings');
    version = String(out.version != null ? out.version : version);
    savedJson = currentJson();      // baseline moves with the saved content
    dirty = false;
    renderDirty();
    toast(t('common.saved'));
  } catch (e) {
    if (Array.isArray(e.detail)) showIssues(e.detail, '#issues');
    else toast((LANG === 'zh' ? '保存失败: ' : 'save failed: ') + (e.detail || e.message));
  }
}

/* ── boot ────────────────────────────────────────────────────────────── */
async function boot() {
  try {
    const [sigBody, pipeBody, modelBody, tagBody] = await Promise.all([
      api.get('/admin/signals'), api.get('/admin/pipeline'),
      api.get('/admin/models'),
      api.get('/admin/tagger').catch(() => ({ tagger: { name: '?' } })),
    ]);
    GROUPS = sigBody.groups && sigBody.groups.length
      ? sigBody.groups
      : ['safety', 'domain', 'difficulty', 'context', 'session', 'rule'];
    (sigBody.signals || []).forEach(s => {
      SIG[s.key] = { ...s, values: (s.values || []).map(v =>
        Array.isArray(v) ? v : [v.key, v.zh, v.en]) };
    });
    KEYWORD_GROUPS = sigBody.keyword_groups || [];
    MODELS = (modelBody.models || []).map(m => m.id);
    nodes = (pipeBody.nodes || []).map(n => ({
      ...n,
      branches: (n.branches || []).map(b => ({ ...b, rows: (b.rows || []).map(r => ({ ...r })) })),
    }));
    version = String(pipeBody.version || '0');
    tagger = tagBody.tagger || {};
    savedJson = currentJson();      // clean baseline: undo can return here
    dirty = false;
    renderAll();
  } catch (e) {
    showIssues([{ zh: '加载失败: ' + (e.detail || e.message), en: 'load failed: ' + (e.detail || e.message) }], '#issues');
  }
}

function renderAll() {
  renderHeader();
  renderPalette();
  renderTree();
  syncFlowChrome();
  syncPalFade();
  syncPaletteUsed();
}

/* ── wiring ──────────────────────────────────────────────────────────── */
$('#savePipeline').addEventListener('click', save);
$('#exportPipeline').addEventListener('click', exportPipeline);
$('#importPipeline').addEventListener('click', () => $('#importFile').click());
$('#importFile').addEventListener('change', e => {
  const file = e.target.files && e.target.files[0];
  if (file) importPipeline(file);
});
$('#openSim').addEventListener('click', openSim);
$('#simClose').addEventListener('click', closeSim);
$('#simMask').addEventListener('click', closeSim);
$('#simRun').addEventListener('click', runTry);
$('#simText').addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runTry(); }
});
$('#simEx').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b) { $('#simText').value = b.textContent; $('#simText').focus(); }
});
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
  else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); openSim(); runTry(); }
  else if (e.key === 'Escape') {
    if (!$('#pop').hidden) { closePop(); return; }
    if (document.body.classList.contains('sim-open')) { closeSim(); return; }
    if (mode) setMode(null);
  }
});
window.addEventListener('resize', () => {
  const pop = $('#pop');
  if (pop.hidden || !popTarget) return;
  const anchor = popAnchor();
  if (anchor) placePop(anchor, popTarget.kind === 'level');
});
window.onLangChange = () => {
  renderAll();
  renderSimText();
  if (lastTry) renderTry();
};
bindFlow();
bindPalette();
bindPop();
boot();
