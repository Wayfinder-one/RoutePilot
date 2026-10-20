/* Routing-tree console — explicit-tree editor (2026-10-19 rewrite).

   Each station is an independent ordered tree:
     Station {id, type: gate|score|split, name, root}
     TreeNode {id, signal?, value, conf_min, kind: leaf|fork,
               action, model, min_score, weights[], children[]}

   A LEAF is terminal: direct hit (命中路由) or weighted scoring (weight chips +
   达成率门槛). A FORK holds ordered children; the first child whose condition
   matches is followed. Children are independent branches.

   Editing model:
   - a station is created by picking a type and a root condition (or an
     unconditional entry); the root's condition value is not editable in place
   - [+] on any node opens the same signal→value picker and adds a CHILD node
   - [−] deletes a node and its subtree (undo toast); the root's [−] deletes
     the station (the last station is the locked fallback)
   - dragging a signal onto a LEAF adds a weighted condition; dragging a signal
     the leaf already carries forks the leaf on that signal (children for the
     weighted values + one more value)
   - changing a condition's value = delete the node and rebuild it
*/
const $ = (s, r = document) => (r || document).querySelector(s);
const $$ = (s, r = document) => Array.from((r || document).querySelectorAll(s));

/* ── state ───────────────────────────────────────────────────────────── */
let SIG = {};                  // key -> {key,zh,en,group,kind,multi,free,values:[[k,zh,en]]}
let GROUPS = [];
let KEYWORD_GROUPS = [];
let MODELS = [];
let stations = [];             // API shape: [{id,type,name,root}]
let version = '0';
let tagger = {};
let lastTry = null;
let dragSig = null;
let dragNodeId = null;
let nodeDrop = null;
let popTarget = null;
let uid = 0;
let savedJson = null;
const nid = p => (p || 'x') + Date.now().toString(36) + (++uid).toString(36);

/* ── i18n / signal helpers ───────────────────────────────────────────── */
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
const condText = n => n.signal
  ? `${sigName(n.signal)} = ${valLabel(n.signal, n.value) || n.value}`
  : t('p6.entry');

/* ── tree helpers ────────────────────────────────────────────────────── */
const findStation = id => stations.find(s => s.id === id);
function walkNodes(node, cb, parent = null) {
  cb(node, parent);
  (node.children || []).forEach(child => walkNodes(child, cb, node));
}
function findNodeRef(nodeId) {
  for (const station of stations) {
    let found = null;
    walkNodes(station.root, (node, parent) => {
      if (node.id === nodeId) found = { station, node, parent };
    });
    if (found) return found;
  }
  return null;
}
function usedSignals() {
  const used = new Set();
  stations.forEach(st => st.root && walkNodes(st.root, n => {
    if (n.signal) used.add(n.signal);
    (n.weights || []).forEach(w => used.add(w.signal));
  }));
  return used;
}
const isFallback = st => stations.length && st.id === stations[stations.length - 1].id;

/* ── header / dirty ──────────────────────────────────────────────────── */
function isDirty() { return savedJson == null || JSON.stringify(stations) !== savedJson; }
function renderDirty() {
  const btn = $('#savePipeline');
  if (!btn) return;
  btn.classList.toggle('has-dirty', isDirty());
  btn.title = isDirty() ? t('pipe.dirty') : t('pipe.clean');
}
function renderHeader() {
  $('#exportPipeline').title = t('p4.exportTitle');
  $('#importPipeline').title = t('p4.importTitle');
  renderDirty();
}
function markDirty() { renderDirty(); }

/* ── palette ─────────────────────────────────────────────────────────── */
function renderPalette() {
  $('#palBody').innerHTML = GROUPS.map(g => {
    const items = Object.values(SIG).filter(s => s.group === g);
    if (!items.length) return '';
    return `<section class="grp">
      <h3>${esc(groupName(g))}<span class="rl"></span></h3>
      <div class="chips">${items.map(s =>
        `<button class="sig" data-sig="${esc(s.key)}" draggable="true"
                 title="${esc(t('p4.sigTitle'))}">
           <svg class="grip" width="7" height="11" viewBox="0 0 7 11" fill="currentColor" aria-hidden="true">
             <circle cx="1.5" cy="1.5" r="1.2"/><circle cx="5.5" cy="1.5" r="1.2"/>
             <circle cx="1.5" cy="5.5" r="1.2"/><circle cx="5.5" cy="5.5" r="1.2"/>
             <circle cx="1.5" cy="9.5" r="1.2"/><circle cx="5.5" cy="9.5" r="1.2"/>
           </svg>${esc(sigName(s.key))}</button>`).join('')}</div>
    </section>`;
  }).join('');
  $('#palCount').textContent = Object.keys(SIG).length + (LANG === 'zh' ? ' 个' : '');
}
function syncPalFade() {
  const body = $('#palBody');
  body.classList.toggle('more',
    body.scrollHeight - body.scrollTop - body.clientHeight > 2);
}

/* ── tree rendering ──────────────────────────────────────────────────── */
function conditionChip(n) {
  const conf = n.signal
    ? (isRuleSig(n.signal)
        ? `<button class="tn-conf det" data-cond="${esc(n.id)}">${esc(t('p4.deterministic'))}</button>`
        : `<button class="tn-conf" data-cond="${esc(n.id)}">${esc(t('p4.confFloor'))}${(n.conf_min != null ? n.conf_min : 0.7).toFixed(2)}</button>`)
    : '';
  return `<button class="tn-cond${n.signal ? '' : ' entry'}" data-cond="${esc(n.id)}">${esc(condText(n))}</button>${conf}`;
}

function outcomeChip(n) {
  if (n.action === 'block') {
    return `<button class="tn-out block" data-out="${esc(n.id)}">${esc(t('pipe.block'))}</button>`;
  }
  const text = n.model
    ? esc(t('pipe.route')) + '<code>' + esc(n.model) + '</code>'
    : esc(t('p4.noModel'));
  return `<button class="tn-out ${n.model ? 'route' : 'todo'}" data-out="${esc(n.id)}">${text}</button>`;
}

function weightsHTML(n) {
  if (!n.weights || !n.weights.length) {
    return `<span class="tn-hit">${esc(t('p4.hitRoute'))}</span>`;
  }
  const chips = n.weights.map((w, i) =>
    `<button class="tn-weight" data-weight="${esc(n.id)}:${i}"
             title="${esc(t('p6.weightTitle'))}">${esc(sigName(w.signal))}
       <span class="sp">·</span>${esc(valLabel(w.signal, w.value) || w.value)}
       <span class="w">×${(+w.weight.toFixed(2))}</span></button>`).join('');
  const floor = n.min_score != null
    ? `<span class="tn-min">${esc(t('p4.minScore'))} ${n.min_score.toFixed(2)}</span>` : '';
  return `<span class="tn-weights">${chips}${floor}</span>`;
}

function nodeHTML(n, depth, station) {
  const isLeaf = n.kind === 'leaf';
  const rootLocked = depth === 0 && isFallback(station);   // fallback station: locked
  const hasWeights = isLeaf && (n.weights || []).length > 0;
  const tone = n.action === 'block' ? 'var(--t4-red)'
    : (rootLocked ? 'var(--t4-slate)' : 'var(--t4-accent)');
  const children = isLeaf ? '' : `<div class="tnode-children">${
    (n.children || []).map(c => nodeHTML(c, depth + 1, station)).join('') ||
    `<div class="tn-empty">${esc(t('p6.noChildren'))}</div>`}</div>`;
  const right = `<span class="tn-right">
      ${isLeaf ? outcomeChip(n) : ''}
      <span class="tn-actions">
        ${hasWeights ? '' : `<button class="bact add" data-nodeadd="${esc(n.id)}"
          title="${esc(t('p6.addChild'))}" aria-label="${esc(tf('p6.addChildNamed', { name: condText(n) }))}">+</button>`}
        ${rootLocked ? '' : `<button class="bact del" data-nodedel="${esc(n.id)}"
          title="${esc(depth === 0 ? t('p6.delStation') : t('p6.delNode'))}"
          aria-label="${esc(tf(depth === 0 ? 'p6.delStationNamed' : 'p6.delNodeNamed', { name: condText(n) }))}">−</button>`}
      </span>
    </span>`;
  return `<div class="tnode ${isLeaf ? 'leaf' : 'fork'}" data-node="${esc(n.id)}"${isLeaf ? ` style="--leaf-tone:${tone}"` : ''}>
    <div class="tnode-row">
      ${conditionChip(n)}
      ${isLeaf ? weightsHTML(n) : `<span class="tn-count">${(n.children || []).length} ${esc(t('p6.children'))}</span>`}
      ${right}
    </div>
    ${children}
  </div>`;
}

function stationHTML(st, i, last) {
  return `<div class="station${last ? ' last' : ''}" role="listitem" data-station="${esc(st.id)}" style="--i:${i}">
      <button class="seq" type="button"${last ? '' : ' draggable="true" title="' + esc(t('p4.dragNode')) + '"'}
              aria-label="${esc(tf('p4.stepN', { n: i + 1, name: st.name }))}">${String(i + 1).padStart(2, '0')}</button>
      <div class="st-body">
        <div class="st-head">
          <h2>${esc(st.name)}</h2>
          ${last ? '' : `<button class="st-del" data-delstation="${esc(st.id)}" title="${esc(t('p6.delStation'))}"
                aria-label="${esc(tf('p6.delStationNamed', { name: st.name }))}">
              <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M2 2l7 7M9 2l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>`}
        </div>
        <div class="tree-box" role="group" aria-label="${esc(tf('p4.stackOf', { name: st.name }))}">
          ${st.root ? nodeHTML(st.root, 0, st) : `<div class="tn-empty">${esc(t('p6.emptyStation'))}</div>`}
        </div>
      </div>
    </div>`;
}

const newstationRow = () =>
  `<div class="newnode-row"><button class="newnode" type="button" data-newstation>＋ ${esc(t('p6.newStation'))}</button></div>`;
const passRow = () =>
  `<div class="pass" role="presentation">
      <span class="chev" aria-hidden="true">
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none"><path d="M5.5 1.5v8M2 6l3.5 3.5L9 6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </span>
      <span class="pt">${esc(t('p4.passOn'))}</span>
    </div>`;

/* whole-tree innerHTML: once at boot and on language change */
function renderTree() {
  const parts = [];
  stations.forEach((st, i) => {
    const last = i === stations.length - 1;
    parts.push(stationHTML(st, i, last));
    if (!last) parts.push(passRow());
  });
  parts.push(newstationRow());
  $('#flow').innerHTML = parts.join('');
}

function renderStation(stationId) {
  const st = findStation(stationId);
  const el = $('#flow').querySelector(`[data-station="${CSS.escape(stationId)}"]`);
  if (!st || !el) return;
  const tmp = document.createElement('div');
  tmp.innerHTML = stationHTML(st, stations.indexOf(st), isFallback(st));
  el.replaceWith(tmp.firstElementChild);
}

function syncFlowChrome() {
  const flow = $('#flow');
  const stationEls = $$('.station', flow);
  stationEls.forEach((el, i) => {
    const last = i === stationEls.length - 1;
    el.classList.toggle('last', last);
    const seq = el.querySelector('.seq');
    if (seq) {
      seq.textContent = String(i + 1).padStart(2, '0');
      const st = findStation(el.dataset.station);
      seq.setAttribute('aria-label', tf('p4.stepN', { n: i + 1, name: st ? st.name : '' }));
      if (last) { seq.removeAttribute('draggable'); seq.removeAttribute('title'); }
      else { seq.setAttribute('draggable', 'true'); seq.title = t('p4.dragNode'); }
    }
  });
  $$('.pass', flow).forEach(p => p.remove());
  stationEls.slice(0, -1).forEach(el => el.insertAdjacentHTML('afterend', passRow()));
  const nn = flow.querySelector('.newnode-row');
  if (nn && stationEls.length) stationEls[stationEls.length - 1].after(nn);
}

function insertStationEl(st, idx) {
  const flow = $('#flow');
  const list = $$('.station', flow);
  const html = stationHTML(st, idx, false);
  if (idx >= 0 && idx < list.length) list[idx].insertAdjacentHTML('beforebegin', html);
  else {
    const nn = flow.querySelector('.newnode-row');
    if (nn) nn.insertAdjacentHTML('beforebegin', html);
    else flow.insertAdjacentHTML('beforeend', html);
  }
  syncFlowChrome();
}
function removeStationEl(id) {
  const el = $('#flow').querySelector(`[data-station="${CSS.escape(id)}"]`);
  if (el) el.remove();
  syncFlowChrome();
}
function reorderFlowDOM() {
  const flow = $('#flow');
  const nn = flow.querySelector('.newnode-row');
  stations.forEach(st => {
    const el = flow.querySelector(`[data-station="${CSS.escape(st.id)}"]`);
    if (el) flow.insertBefore(el, nn);
  });
  syncFlowChrome();
}
function flashNode(id) {
  const el = $('#flow').querySelector(`[data-node="${CSS.escape(id)}"]`);
  if (!el) return;
  el.classList.add('is-new');
  setTimeout(() => el.classList.remove('is-new'), 1000);
}

/* ── toast ───────────────────────────────────────────────────────────── */
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

/* ── node mutations ──────────────────────────────────────────────────── */
function newNode(station, signal, value) {
  return {
    id: nid('n'), signal, value: value == null ? '' : String(value),
    conf_min: signal && !isRuleSig(signal) ? 0.7 : null,
    kind: 'leaf', action: 'route',
    model: MODELS[0] || '',
    min_score: null, weights: [], children: [],
  };
}

function addChild(nodeId, signal, value) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  const snapshot = JSON.parse(JSON.stringify(node));
  const becameFork = node.kind === 'leaf';
  if (becameFork) {
    node.kind = 'fork';
    node.action = 'route';
    node.model = null;
    node.min_score = null;
    node.weights = [];
    node.children = [newNode(station, signal, value)];
  } else {
    node.children.push(newNode(station, signal, value));
  }
  markDirty();
  renderStation(station.id);
  flashNode(node.children[node.children.length - 1].id);
  toast(becameFork
    ? tf('p6.leafBecameFork', { name: condText(node) })
    : tf('p6.toastChildAdded', { name: sigName(signal) }), () => {
    const back = findNodeRef(nodeId);
    if (!back) return;
    Object.assign(back.node, snapshot);
    markDirty();
    renderStation(back.station.id);
  });
}

function deleteNode(nodeId) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node, parent } = ref;
  if (!parent) { deleteStation(station.id); return; }
  const index = parent.children.findIndex(c => c.id === nodeId);
  if (index < 0) return;
  const snapshot = parent.children.splice(index, 1)[0];
  markDirty();
  renderStation(station.id);
  toast(tf('p6.toastNodeDeleted', { name: condText(snapshot) }), () => {
    const back = findNodeRef(parent.id);
    if (!back) return;
    back.node.children.splice(index, 0, snapshot);
    markDirty();
    renderStation(back.station.id);
  });
}

function deleteStation(stationId) {
  const idx = stations.findIndex(s => s.id === stationId);
  if (idx < 0) return;
  if (idx === stations.length - 1) {
    toast(t('p6.toastNoDeleteFallback'));
    return;
  }
  const [st] = stations.splice(idx, 1);
  markDirty();
  removeStationEl(stationId);
  toast(tf('p6.toastStationDeleted', { name: st.name }), () => {
    stations.splice(idx, 0, st);
    markDirty();
    insertStationEl(st, idx);
  });
}

function addStation(signal, value) {
  const body = { id: nid('st'), type: 'split',
                 name: `${t('p6.stationName')} ${stations.length}`, root: null };
  if (signal === null) {
    body.root = { id: nid('n'), signal: null, value: '', conf_min: null,
                  kind: 'fork', action: 'route', model: null, min_score: null,
                  weights: [], children: [] };
  } else {
    body.root = newNode(body, signal, value);
  }
  stations.splice(Math.max(0, stations.length - 1), 0, body);
  markDirty();
  insertStationEl(body, stations.length - 2);
  toast(tf('p6.toastStationAdded', { name: body.name }), () => {
    const i = stations.findIndex(s => s.id === body.id);
    if (i >= 0) { stations.splice(i, 1); markDirty(); removeStationEl(body.id); }
  });
}

/* ── weights ─────────────────────────────────────────────────────────── */
function addWeight(nodeId, signal, value) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  if (node.kind !== 'leaf') { toast(t('p6.forkNoWeights')); return; }
  if (node.signal === signal) { toast(t('p6.sameAsCondition')); return; }
  if ((node.weights || []).some(w => w.signal === signal && w.value === value)) {
    toast(t('p6.duplicateWeight'));
    return;
  }
  const added = { signal, value, weight: 0.5 };
  node.weights.push(added);
  if (node.min_score == null) node.min_score = 0.5;
  markDirty();
  renderStation(station.id);
  toast(tf('p6.toastWeightAdded', { name: sigName(signal) }), () => {
    const back = findNodeRef(nodeId);
    if (!back) return;
    back.node.weights = back.node.weights.filter(w => w !== added);
    markDirty();
    renderStation(back.station.id);
  });
}

/* a repeated signal forks the leaf on it: weighted values become child
   conditions, plus one more unused value as a sibling seed. */
function forkOnSignal(nodeId, signal) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  if (node.signal === signal) { toast(t('p6.sameAsCondition')); return; }
  const spec = sigSpec(signal);
  const used = [...new Set((node.weights || [])
    .filter(w => w.signal === signal).map(w => w.value))];
  if (!used.length) { openValuePicker(signal, (key, v) => addWeight(nodeId, key, v)); return; }
  const rest = (spec.values || []).map(([v]) => v).filter(v => !used.includes(v));
  const otherWeights = (node.weights || []).filter(w => w.signal !== signal);
  const makeChild = value => ({
    id: nid('n'), signal, value: value == null ? '' : String(value),
    conf_min: isRuleSig(signal) ? null : 0.7, kind: 'leaf',
    action: node.action, model: node.model, min_score: node.min_score,
    weights: otherWeights.map(w => ({ ...w })), children: [],
  });
  const children = used.map(makeChild);
  if (rest.length) children.push(makeChild(rest[0]));
  const snapshot = JSON.parse(JSON.stringify(node));
  node.kind = 'fork';
  node.weights = [];
  node.children = children;
  node.action = 'route';
  node.model = null;
  node.min_score = null;
  markDirty();
  renderStation(station.id);
  children.forEach(c => flashNode(c.id));
  toast(tf('p6.toastForked', { name: sigName(signal), n: children.length }), () => {
    const back = findNodeRef(nodeId);
    if (!back) return;
    Object.assign(back.node, snapshot);
    markDirty();
    renderStation(back.station.id);
  });
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
  if (!num || !rng) return;
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

function popHead(title, sub) {
  return `<div class="pop-head">
      <b>${esc(title)}</b>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}
      <button class="pop-x" data-popclose aria-label="${esc(t('common.close'))}">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>`;
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
const anchorOf = sel => $('#flow').querySelector(sel);

/* condition editor: value read-only, confidence adjustable */
function openConditionEditor(nodeId) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  popTarget = { kind: 'cond', nodeId };
  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `${popHead(node.signal ? sigName(node.signal) : t('p6.entry'), station.name)}
    <p class="pop-label">${esc(t('p4.value'))}</p>
    <p class="pop-note" style="margin:0 0 10px">${esc(condText(node))} · ${esc(t('p6.valueReadonly'))}</p>
    ${node.signal && isRuleSig(node.signal)
      ? `<p class="pop-note">${esc(t('p4.ruleNote'))}</p>`
      : node.signal
        ? rangeBlock('nc', t('p4.confMin'), node.conf_min != null ? node.conf_min : 0.7, 0.01, t('p4.confHint'))
        : `<p class="pop-note">${esc(t('p6.entryNote'))}</p>`}
    <div class="pop-foot">
      <span class="pop-note">${esc(t('p6.changeByRebuild'))}</span>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop(anchorOf(`[data-cond="${CSS.escape(nodeId)}"]`));
  if (node.signal && !isRuleSig(node.signal)) wireRange('nc', {
    onInput(v) {
      node.conf_min = v;
      const el = anchorOf(`[data-cond="${CSS.escape(nodeId)}"]`);
      if (el) el.textContent = t('p4.confFloor') + v.toFixed(2);
      const conf = el && el.parentElement.querySelector('.tn-conf');
      if (conf) conf.textContent = t('p4.confFloor') + v.toFixed(2);
    },
    onCommit() { markDirty(); renderStation(station.id); placePop(anchorOf(`[data-cond="${CSS.escape(nodeId)}"]`)); }
  });
}

/* leaf editor: action / model / min_score / weights */
function openLeafEditor(nodeId) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  if (node.kind !== 'leaf') { openConditionEditor(nodeId); return; }
  popTarget = { kind: 'leaf', nodeId };
  const pop = $('#pop');
  pop.hidden = false;
  const weightRows = (node.weights || []).map((w, i) => `
    <div class="wrow">
      <span class="wname">${esc(sigName(w.signal))} · ${esc(valLabel(w.signal, w.value) || w.value)}</span>
      <input class="pr-num" type="number" min="0" max="1" step="0.05"
             data-weightnum="${i}" value="${(+w.weight).toFixed(2)}">
      <button class="wdel" data-weightdel="${i}" aria-label="${esc(t('p4.delCond'))}">✕</button>
    </div>`).join('');
  pop.innerHTML = `${popHead(t('p6.leafTitle'), station.name)}
    <p class="pop-label">${esc(t('p4.afterHit'))}</p>
    <div class="pop-vals" id="lfAction">
      <button data-act="route" class="${node.action !== 'block' ? 'on' : ''}">${esc(t('p4.routeModel'))}</button>
      <button data-act="block" class="${node.action === 'block' ? 'on' : ''}">${esc(t('p4.blockNow'))}</button>
    </div>
    ${node.action !== 'block' ? `
      <p class="pop-label">${esc(t('p4.model'))}</p>
      <input class="pop-text" id="lfModel" list="lfModels" value="${esc(node.model || '')}" placeholder="qwen3-8b">
      <datalist id="lfModels">${MODELS.map(m => `<option value="${esc(m)}">`).join('')}</datalist>` : ''}
    ${(node.weights && node.weights.length) ? `
      <p class="pop-label">${esc(t('p6.weights'))}</p>
      <div class="wlist" id="lfWeights">${weightRows}</div>
      ${rangeBlock('lfMin', t('p4.minScore'), node.min_score != null ? node.min_score : 0.5, 0.05, t('p4.minScoreHint'))}
    ` : `<p class="pop-note">${esc(t('p6.noWeightsNote'))}</p>`}
    <div class="pop-foot">
      <button class="pop-del" data-popdelnode="${esc(nodeId)}">${esc(t('p6.delNode'))}</button>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop(anchorOf(`[data-out="${CSS.escape(nodeId)}"]`));
  pop.querySelector('#lfAction').addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    node.action = b.dataset.act;
    if (node.action === 'route' && !node.model) node.model = MODELS[0] || '';
    markDirty();
    renderStation(station.id);
    openLeafEditor(nodeId);
  });
  const modelInput = $('#lfModel');
  if (modelInput) modelInput.addEventListener('change', e => {
    node.model = e.target.value.trim() || null;
    markDirty();
    renderStation(station.id);
  });
  if (node.weights && node.weights.length) {
    wireRange('lfMin', {
      onInput(v) { node.min_score = v; },
      onCommit() { markDirty(); renderStation(station.id); }
    });
    pop.querySelectorAll('[data-weightnum]').forEach(input => {
      input.addEventListener('change', () => {
        const i = Number(input.dataset.weightnum);
        const v = Math.min(1, Math.max(0, parseFloat(input.value)));
        if (Number.isNaN(v)) return;
        node.weights[i].weight = v;
        markDirty();
        renderStation(station.id);
        openLeafEditor(nodeId);
      });
    });
    pop.querySelectorAll('[data-weightdel]').forEach(btn => {
      btn.addEventListener('click', () => {
        node.weights.splice(Number(btn.dataset.weightdel), 1);
        markDirty();
        renderStation(station.id);
        openLeafEditor(nodeId);
      });
    });
  }
  pop.querySelector('[data-popdelnode]').addEventListener('click', () => {
    closePop();
    deleteNode(nodeId);
  });
}

/* weight editor (click a weight chip) */
function openWeightEditor(nodeId, index) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const { station, node } = ref;
  const w = node.weights[index];
  if (!w) return;
  popTarget = { kind: 'weight', nodeId };
  const pop = $('#pop');
  pop.hidden = false;
  pop.innerHTML = `${popHead(sigName(w.signal), station.name)}
    <p class="pop-label">${esc(t('p4.value'))}</p>
    <p class="pop-note" style="margin:0 0 10px">${esc(valLabel(w.signal, w.value) || w.value)}</p>
    ${rangeBlock('wt', t('p4.weight'), w.weight, 0.05, t('p4.weightHint'))}
    <div class="pop-foot">
      <button class="pop-del" data-weightdel="${esc(nodeId)}:${index}">${esc(t('p4.delCond'))}</button>
      <button class="pop-ok" data-popclose>${esc(t('common.close'))}</button>
    </div>`;
  placePop(anchorOf(`[data-weight="${CSS.escape(nodeId + ':' + index)}"]`));
  wireRange('wt', {
    onInput(v) { w.weight = v; },
    onCommit() {
      markDirty();
      renderStation(station.id);
      placePop(anchorOf(`[data-weight="${CSS.escape(nodeId + ':' + index)}"]`));
    }
  });
  pop.querySelector('[data-weightdel]').addEventListener('click', () => {
    node.weights.splice(index, 1);
    closePop();
    markDirty();
    renderStation(station.id);
  });
}

/* ── shared picker: signal -> value ──────────────────────────────────── */
function openPicker({ title, sub, anchor, allowFree, note, onPick, disabledValues,
                      preselect }) {
  const pop = $('#pop');
  const anchorEl = () => (typeof anchor === 'function' ? anchor() : anchor);
  popTarget = { kind: 'picker', anchor: anchorEl };

  function place() { placePop(anchorEl(), true); }

  function renderSignals() {
    pop.hidden = false;
    pop.innerHTML = `${popHead(title, sub)}
      <p class="pop-label">${esc(t('p6.pickSignal'))}</p>
      <div class="pop-vals" id="pkSigs">
        ${Object.values(SIG).filter(x => allowFree || !isFreeSig(x.key)).map(x =>
          `<button data-pk="${esc(x.key)}">${esc(sigName(x.key))}</button>`).join('')}
      </div>
      <div class="pop-foot"><span class="pop-note">${esc(note || '')}</span></div>`;
    place();
    $('#pkSigs').addEventListener('click', e => {
      const b = e.target.closest('[data-pk]');
      if (b) renderValues(b.dataset.pk);
    });
  }

  function renderValues(key) {
    pop.hidden = false;
    const spec = sigSpec(key);
    const back = `<button class="pop-del" id="pkBack" style="color:var(--t4-ink-3)">← ${esc(t('p4.back'))}</button>`;
    if (isFreeSig(key)) {
      const list = key === 'keyword' && KEYWORD_GROUPS.length
        ? `<datalist id="pkKw">${KEYWORD_GROUPS.map(g => `<option value="${esc(g)}">`).join('')}</datalist>` : '';
      pop.innerHTML = `${popHead(tf('p4.pickRootValue', { sig: sigName(key) }), sub)}
        <p class="pop-label">${esc(t('p4.value'))}</p>
        <input class="pop-text" id="pkText" value="" list="${key === 'keyword' && KEYWORD_GROUPS.length ? 'pkKw' : ''}"
               placeholder="${esc(t('p4.freePlaceholder'))}">${list}
        <div class="pop-foot">${back}
          <button class="pop-ok" id="pkOk">${esc(t('common.add'))}</button></div>`;
      place();
      const input = $('#pkText');
      input.focus();
      const submit = () => {
        const v = input.value.trim();
        if (!v) { input.focus(); return; }
        closePop();
        onPick(key, v);
      };
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      $('#pkOk').addEventListener('click', submit);
    } else {
      const hidden = disabledValues ? disabledValues(key) : new Set();
      const allowed = (spec.values || []).filter(([v]) => !hidden.has(v));
      const body = allowed.length
        ? `<div class="pop-vals" id="pkVals">
             ${allowed.map(([v, zh, en]) =>
               `<button data-pv="${esc(v)}">${esc(LANG === 'zh' ? zh : (en || zh))}</button>`).join('')}
           </div>`
        : `<p class="pop-note" style="margin:2px 0 10px">${esc(tf('p4.noFreeValue', { sig: sigName(key) }))}</p>`;
      pop.innerHTML = `${popHead(tf('p4.pickRootValue', { sig: sigName(key) }), sub)}
        ${body}
        <div class="pop-foot">${back}<span class="pop-note">${esc(note || '')}</span></div>`;
      place();
      if (allowed.length) $('#pkVals').addEventListener('click', e => {
        const b = e.target.closest('[data-pv]');
        if (!b) return;
        closePop();
        onPick(key, b.dataset.pv);
      });
    }
    $('#pkBack').addEventListener('click', renderSignals);
  }

  if (preselect) renderValues(preselect);
  else renderSignals();
}

function openChildPicker(nodeId) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  openPicker({
    title: t('p6.addChild'),
    sub: condText(ref.node),
    anchor: () => anchorOf(`[data-nodeadd="${CSS.escape(nodeId)}"]`),
    allowFree: true,
    note: t('p6.childNote'),
    onPick: (signal, value) => addChild(nodeId, signal, value),
  });
}

function openValuePicker(signal, onPick, anchor) {
  openPicker({
    title: t('p6.addWeight'),
    sub: sigName(signal),
    anchor: anchor || (() => $('#palBody .sig[data-sig="' + CSS.escape(signal) + '"]')),
    allowFree: true,
    preselect: signal,
    note: t('p6.weightNote'),
    onPick,
  });
}

/* drop handler shared by pointer drag and click-to-pick */
function handleSignalDrop(nodeId, signal) {
  const ref = findNodeRef(nodeId);
  if (!ref) return;
  const node = ref.node;
  if (node.kind !== 'leaf') { toast(t('p6.forkNoWeights')); return; }
  if (node.signal === signal) { toast(t('p6.sameAsCondition')); return; }
  if ((node.weights || []).some(w => w.signal === signal)) {
    forkOnSignal(nodeId, signal);
    return;
  }
  openValuePicker(signal, (key, value) => addWeight(nodeId, key, value),
                  () => $(`[data-node="${CSS.escape(nodeId)}"]`));
}

/* station creation: pick the root condition (or an unconditional entry);
   stations are no longer classified by type (2026-10-20) */
function openStationPicker() {
  popTarget = { kind: 'picker' };
  const pop = $('#pop');
  function renderRoots() {
    pop.hidden = false;
    pop.innerHTML = `${popHead(t('p6.newStation'), t('p6.stationNote'))}
      <p class="pop-label">${esc(t('p6.rootCondition'))}</p>
      <div class="pop-vals" id="stRoots">
        <button data-uncond="1">${esc(t('p6.unconditional'))}</button>
        ${Object.values(SIG).map(x =>
          `<button data-pk="${esc(x.key)}">${esc(sigName(x.key))}</button>`).join('')}
      </div>
      <div class="pop-foot"><span class="pop-note">${esc(t('p6.stationNote'))}</span></div>`;
    placePop(anchorOf('[data-newstation]'), true);
    $('#stRoots').addEventListener('click', e => {
      if (e.target.closest('[data-uncond]')) { closePop(); addStation(null, ''); return; }
      const b = e.target.closest('[data-pk]');
      if (!b) return;
      const key = b.dataset.pk;
      const spec = sigSpec(key);
      if (isFreeSig(key)) {
        pop.innerHTML = `${popHead(tf('p4.pickRootValue', { sig: sigName(key) }), '')}
          <p class="pop-label">${esc(t('p4.value'))}</p>
          <input class="pop-text" id="stText" value="">
          <div class="pop-foot"><button class="pop-ok" id="stOk">${esc(t('common.add'))}</button></div>`;
        placePop(anchorOf('[data-newstation]'), true);
        const submit = () => {
          const v = $('#stText').value.trim();
          if (!v) { $('#stText').focus(); return; }
          closePop();
          addStation(key, v);
        };
        $('#stText').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
        $('#stOk').addEventListener('click', submit);
        return;
      }
      pop.innerHTML = `${popHead(tf('p4.pickRootValue', { sig: sigName(key) }), '')}
        <div class="pop-vals" id="stVals">
          ${(spec.values || []).map(([v, zh, en]) =>
            `<button data-pv="${esc(v)}">${esc(LANG === 'zh' ? zh : (en || zh))}</button>`).join('')}
        </div>`;
      placePop(anchorOf('[data-newstation]'), true);
      $('#stVals').addEventListener('click', e => {
        const v = e.target.closest('[data-pv]');
        if (!v) return;
        closePop();
        addStation(key, v.dataset.pv);
      });
    });
  }
  renderRoots();
}

/* ── pointer interactions (no native HTML5 DnD) ─────────────────────── */
let pdrag = null;        // {kind:'signal'|'station', ...}
let suppressClick = false;

function clearHot() { $$('.tnode.hot').forEach(el => el.classList.remove('hot')); }
function clearDropIndicators() {
  $$('.station.drop-before, .station.drop-after')
    .forEach(el => el.classList.remove('drop-before', 'drop-after'));
}
function elementAt(x, y) {
  const el = document.elementFromPoint(x, y);
  return el && el.closest ? el : null;
}

function onPointerMove(ev) {
  if (!pdrag) return;
  const dx = ev.clientX - pdrag.startX, dy = ev.clientY - pdrag.startY;
  if (!pdrag.active) {
    if (Math.hypot(dx, dy) < 6) return;
    pdrag.active = true;
    suppressClick = true;
    document.body.classList.add('dragging');
    if (pdrag.kind === 'signal') {
      const ghost = document.createElement('div');
      ghost.className = 'drag-ghost';
      ghost.textContent = sigName(pdrag.sig);
      ghost.style.cssText = 'position:fixed;pointer-events:none;z-index:80;';
      // append inside .t4 so the drag-ghost styles and CSS variables apply
      ($('.t4') || document.body).appendChild(ghost);
      pdrag.ghost = ghost;
      dragSig = pdrag.sig;
    } else {
      const el = $(`[data-station="${CSS.escape(pdrag.stationId)}"]`);
      if (el) el.classList.add('moving');
    }
  }
  if (pdrag.kind === 'signal') {
    if (pdrag.ghost) {
      pdrag.ghost.style.left = (ev.clientX + 14) + 'px';
      pdrag.ghost.style.top = (ev.clientY + 10) + 'px';
    }
    clearHot();
    const tn = elementAt(ev.clientX, ev.clientY)?.closest('.tnode');
    if (tn) {
      const ref = findNodeRef(tn.dataset.node);
      if (ref && ref.node.kind === 'leaf') tn.classList.add('hot');
    }
    return;
  }
  clearDropIndicators();
  const stEl = elementAt(ev.clientX, ev.clientY)?.closest('.station');
  if (!stEl) return;
  const r = stEl.getBoundingClientRect();
  const before = ev.clientY < r.top + r.height / 2;
  stEl.classList.add(before ? 'drop-before' : 'drop-after');
  pdrag.target = { id: stEl.dataset.station, before };
}

function onPointerUp(ev) {
  if (!pdrag) return;
  const drag = pdrag;
  pdrag = null;
  document.body.classList.remove('dragging');
  if (drag.ghost) drag.ghost.remove();
  dragSig = null;
  clearHot();
  clearDropIndicators();
  if (drag.kind === 'station') {
    $$('.station.moving').forEach(el => el.classList.remove('moving'));
    if (drag.active && drag.target) {
      const idx = stations.findIndex(s => s.id === drag.target.id);
      if (idx >= 0) moveStation(drag.stationId,
                                drag.target.before ? idx : idx + 1);
    }
    return;
  }
  if (!drag.active) return;
  const tn = elementAt(ev.clientX, ev.clientY)?.closest('.tnode');
  if (tn) handleSignalDrop(tn.dataset.node, drag.sig);
}

function bindPalette() {
  const palBody = $('#palBody');
  palBody.addEventListener('mousedown', e => {
    const sig = e.target.closest('.sig');
    if (!sig || e.button !== 0) return;
    e.preventDefault();                       // avoid text selection while dragging
    pdrag = { kind: 'signal', sig: sig.dataset.sig,
              startX: e.clientX, startY: e.clientY, active: false, ghost: null };
  });
  palBody.addEventListener('scroll', syncPalFade, { passive: true });
  $('#palToggle').addEventListener('click', () =>
    setPalOpen($('#palette').classList.contains('closed')));
}
function setPalOpen(open) {
  $('#palette').classList.toggle('closed', !open);
  $('#palToggle').setAttribute('aria-expanded', String(open));
}

function bindFlow() {
  const flow = $('#flow');

  flow.addEventListener('click', e => {
    const add = e.target.closest('[data-nodeadd]');
    if (add) { openChildPicker(add.dataset.nodeadd); return; }
    const del = e.target.closest('[data-nodedel]');
    if (del) { deleteNode(del.dataset.nodedel); return; }
    const cond = e.target.closest('[data-cond]');
    if (cond) { openConditionEditor(cond.dataset.cond); return; }
    const out = e.target.closest('[data-out]');
    if (out) { openLeafEditor(out.dataset.out); return; }
    const weight = e.target.closest('[data-weight]');
    if (weight) {
      const [nid_, idx] = weight.dataset.weight.split(':');
      openWeightEditor(nid_, Number(idx));
      return;
    }
    const delStation = e.target.closest('[data-delstation]');
    if (delStation) { deleteStation(delStation.dataset.delstation); return; }
    const newStation = e.target.closest('[data-newstation]');
    if (newStation) { openStationPicker(); return; }
  });

  /* station reorder by dragging the number badge (fallback locked last) */
  flow.addEventListener('mousedown', e => {
    const seq = e.target.closest('.seq');
    if (!seq || e.button !== 0) return;
    const id = seq.closest('.station').dataset.station;
    if (id === stations[stations.length - 1].id) return;
    e.preventDefault();
    pdrag = { kind: 'station', stationId: id,
              startX: e.clientX, startY: e.clientY, active: false };
  });

  flow.addEventListener('keydown', e => {
    const seq = e.target.closest('.seq');
    if (seq && e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      const id = seq.closest('.station').dataset.station;
      const idx = stations.findIndex(s => s.id === id);
      if (idx < 0 || idx === stations.length - 1) return;
      e.preventDefault();
      moveStation(id, e.key === 'ArrowUp' ? idx - 1 : idx + 2);
      const again = flow.querySelector(`[data-station="${CSS.escape(id)}"] .seq`);
      if (again) again.focus();
    }
  });
}

document.addEventListener('mousemove', onPointerMove);
document.addEventListener('mouseup', onPointerUp);

function moveStation(stationId, beforeIdx) {
  const from = stations.findIndex(s => s.id === stationId);
  if (from < 0 || from === stations.length - 1) return;
  const snapshot = stations.map(s => s.id);
  const st = stations[from];
  let to = beforeIdx > from ? beforeIdx - 1 : beforeIdx;
  to = Math.max(0, Math.min(to, stations.length - 2));
  if (to === from) return;
  stations.splice(from, 1);
  stations.splice(to, 0, st);
  markDirty();
  reorderFlowDOM();
  toast(tf('p4.toastMoved', { name: st.name, n: to + 1 }), () => {
    const byId = new Map(stations.map(s => [s.id, s]));
    stations.length = 0;
    snapshot.forEach(id => stations.push(byId.get(id)));
    markDirty();
    reorderFlowDOM();
  });
}

/* popover external-click close */
function bindPop() {
  const pop = $('#pop');
  pop.addEventListener('click', e => {
    if (e.target.closest('[data-popclose]')) closePop();
  });
  document.addEventListener('mousedown', e => {
    if (pop.hidden) return;
    if (pop.contains(e.target)) return;
    if (e.target.closest('[data-cond],[data-out],[data-weight],[data-nodeadd],[data-newstation]')) return;
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
    $('#simOut').innerHTML = dirtyWarning() +
      `<b>${esc(t('p4.simEmptyTitle'))}</b>${esc(t('p4.simEmptyBody'))}`;
  }
}
function stepText(s) {
  const conf = (s.conf === 1.0 && s.conf_min == null)
    ? t('p4.deterministic')
    : pct(s.conf) + (s.conf_min != null ? ' ' + t('p4.confFloor') + Number(s.conf_min).toFixed(2) : '');
  const why = s.pass ? ''
    : (s.m === 0 ? ' · ' + t('p6.notMatched') : ' · ' + t('p4.cut'));
  return `${sigName(s.signal)} = ${esc(valLabel(s.signal, s.value) || s.value)} · ${esc(conf)}${why}`;
}
function dirtyWarning() {
  return isDirty()
    ? `<div class="sim-warn">⚠ ${esc(t('p6.dirtyTry'))}</div>` : '';
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
  const station = d.station_id ? findStation(d.station_id) : null;
  let html = dirtyWarning() + `<div class="final ${cls}">
      <div><div class="m">${esc(label)}</div>
      <div class="via">${esc(station ? station.name : '')}${d.score != null ? ' · score ' + Number(d.score).toFixed(2) : ''}</div></div>
      <span class="badge">${esc(badge)}</span></div>`;
  html += `<div class="col-h">${esc(t('pipe.try.trace'))}</div>`;
  (lastTry.trace || []).forEach((r, i) => {
    const hit = r.result === 'hit';
    let body = '';
    if ((r.path || []).length) {
      body += `<div class="pathrow">${r.path.map(s =>
        `<div class="pstep${s.pass ? '' : ' cut'}">${esc(stepText(s))}</div>`).join('')}</div>`;
    }
    if ((r.board || []).length) {
      body += `<div class="board" style="margin-top:5px">${r.board.map(b =>
        `<div class="brow${b.winner ? ' win' : ''}">${b.winner ? '★ ' : ''}
           <span class="mono">score ${Number(b.score).toFixed(2)} ≥ ${Number(b.min_score).toFixed(2)}</span>
           <span class="mono">${b.passed ? t('p6.passed') : t('p6.failed')}</span></div>`).join('')}</div>`;
    }
    if (!body) {
      body = hit
        ? `<div class="pstep">${esc(t('p6.traceUncondHit'))}</div>`
        : `<div class="pstep cut">${esc(t('p6.traceNoMatch'))}</div>`;
    }
    html += `<div class="pstep" style="align-items:flex-start;flex-direction:column">
        <div style="display:flex;gap:7px;align-items:center;width:100%">
          <span class="mono">${i + 1}</span><b>${esc(r.name || r.station_id)}</b>
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
  out.innerHTML = html;
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

/* ── export / import ─────────────────────────────────────────────────── */
function exportPipeline() {
  const body = { version, exported_at: new Date().toISOString(), stations };
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

function normalizeImported(raw) {
  const list = Array.isArray(raw) ? raw
    : (raw && Array.isArray(raw.stations) ? raw.stations : null);
  if (!list) throw new Error(t('p4.importNoNodes'));
  function normNode(n, where) {
    if (!n || typeof n !== 'object') throw new Error(where);
    const kind = n.kind === 'fork' ? 'fork' : 'leaf';
    const node = {
      id: String(n.id || nid('n')), signal: n.signal || null,
      value: n.value == null ? '' : String(n.value),
      conf_min: n.conf_min != null ? Number(n.conf_min) : null,
      kind, action: n.action === 'block' ? 'block' : 'route',
      model: n.model || null,
      min_score: n.min_score != null ? Number(n.min_score) : null,
      weights: (n.weights || []).map(w => ({
        signal: String(w.signal), value: String(w.value ?? ''),
        weight: w.weight != null ? Number(w.weight) : 0.5,
      })),
      children: [],
    };
    if (kind === 'fork') {
      if (!Array.isArray(n.children) || !n.children.length)
        throw new Error(where + ': fork children');
      node.children = n.children.map((c, i) =>
        normNode(c, where + '/' + (i + 1)));
    }
    return node;
  }
  return list.map((st, i) => {
    const where = t('p4.importRow') + ' #' + (i + 1);
    if (!st || typeof st !== 'object') throw new Error(where);
    if (!['gate', 'score', 'split'].includes(st.type))
      throw new Error(where + ': type=' + st.type);
    return {
      id: String(st.id || nid('st')),
      type: st.type, name: String(st.name || ''),
      root: st.root ? normNode(st.root, where + '/root') : null,
    };
  });
}

async function importPipeline(file) {
  try {
    const raw = JSON.parse(await file.text());
    const imported = normalizeImported(raw);
    if (!imported.length) throw new Error(t('p4.importEmpty'));
    stations = imported;
    markDirty();
    renderAll();
    toast(tf('p4.importOk', { n: imported.length }));
  } catch (e) {
    toast(tf('p4.importBad', { err: e.message }));
  } finally {
    $('#importFile').value = '';
  }
}

/* ── save ────────────────────────────────────────────────────────────── */
function showIssues(list, boxId) {
  const box = $(boxId);
  if (!list || !list.length) { box.innerHTML = ''; return; }
  box.innerHTML = list.map(i => {
    const msg = LANG === 'zh' ? (i.zh || i.code || JSON.stringify(i)) : (i.en || i.code || JSON.stringify(i));
    return `<div class="iv">• ${esc(msg)}</div>`;
  }).join('');
}

async function save() {
  try {
    const out = await api.put('/admin/pipeline', { stations });
    showIssues([], '#issues');
    showIssues(out.warnings || [], '#warnings');
    version = String(out.version != null ? out.version : version);
    savedJson = JSON.stringify(stations);
    renderHeader();
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
    stations = pipeBody.stations || [];
    version = String(pipeBody.version || '0');
    tagger = tagBody.tagger || {};
    savedJson = JSON.stringify(stations);
    renderAll();
  } catch (e) {
    showIssues([{ zh: '加载失败: ' + (e.detail || e.message),
                  en: 'load failed: ' + (e.detail || e.message) }], '#issues');
  }
}

function renderAll() {
  renderHeader();
  renderPalette();
  renderTree();
  syncFlowChrome();
  syncPalFade();
}

/* ── wiring ──────────────────────────────────────────────────────────── */
$('#savePipeline').addEventListener('click', save);
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
$('#exportPipeline').addEventListener('click', exportPipeline);
$('#importPipeline').addEventListener('click', () => $('#importFile').click());
$('#importFile').addEventListener('change', e => {
  const file = e.target.files && e.target.files[0];
  if (file) importPipeline(file);
});
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
  else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); openSim(); runTry(); }
  else if (e.key === 'Escape') {
    if (!$('#pop').hidden) { closePop(); return; }
    if (document.body.classList.contains('sim-open')) { closeSim(); return; }
  }
});
window.addEventListener('resize', () => {
  if (!$('#pop').hidden && popTarget && popTarget.kind === 'picker' && popTarget.anchor) {
    placePop(popTarget.anchor(), true);
  }
});
window.onLangChange = () => {
  renderAll();
  renderSimText();
  if (lastTry) renderTry();
};
bindFlow();
bindPalette();
bindPop();
renderHeader();
boot();
