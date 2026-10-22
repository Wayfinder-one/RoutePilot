/* Signal configuration page: edit the JEV question config
   (/admin/signal-questions) — question wording, true/false meanings, choice
   candidate descriptions, PII type names. Save validates on the server and
   hot-applies to the running JevTagger. Also exposes the raw JSON for bulk
   edits / import / export. */
const $ = s => document.querySelector(s);

let cfg = null;          // the editable config object
let meta = {};
let selected = 'domain';
const KW = '__keyword_lexicon__';   // pseudo entry in the signal list
const TW = '__time_rules__';        // pseudo entry: time period lexicon
let kwGroups = {};
let timePeriods = [];
let dirty = false;
let rawOpen = false;

const kindLabel = k => cfg.signals[k].kind === 'noul' ? t('sig.kind.noul') : t('sig.kind.choice');

async function boot() {
  const [qBody, sigBody, kwBody, twBody] = await Promise.all([
    api.get('/admin/signal-questions'),
    api.get('/admin/signals'),
    api.get('/admin/keyword-groups'),
    api.get('/admin/time-periods'),
  ]);
  timePeriods = twBody.periods || [];
  setServerOffset(twBody.now);
  startServerClock();
  cfg = qBody.config;
  meta = qBody.meta || {};
  setSignalRegistry(sigBody.signals);
  kwGroups = kwBody.groups || {};
  renderAll();
}

function renderHeader() {
  const temp = meta.temperature != null ? Number(meta.temperature).toFixed(2) : '—';
  $('#dirtyPill').className = 'pill ' + (dirty ? 'warn' : 'ok');
  $('#dirtyPill').textContent = dirty ? t('sig.changed') : t('pipe.clean');
  $('#sigCount').textContent = Object.keys(cfg.signals).length;
  const parts = [];
  parts.push(`${t('sig.temperature')} <b class="mono">${temp}</b>`);
  if (meta.max_length != null) parts.push(`${t('sig.maxlen')} <b class="mono">${meta.max_length}</b>`);
  if (meta.questions_path) parts.push(`<span class="mono" title="${esc(meta.questions_path)}">${esc(String(meta.questions_path).split('/').pop())}</span>`);
  $('#metaInfo').innerHTML = parts.join(' · ');
}

function renderList() {
  const items = Object.keys(cfg.signals).map(k => {
    const s = cfg.signals[k];
    return `<div class="sig-item ${k === selected ? 'on' : ''}" data-sig="${esc(k)}">
      <span class="k" style="background:${sigColor((SIGNAL_REGISTRY[k] || {}).group)}"></span>
      <span class="n">${esc(sigName(k))}</span>
      <span class="b">${s.kind === 'noul' ? 'y/n' : 'choice'}</span>
    </div>`;
  });
  items.push(`<div class="sig-item ${selected === KW ? 'on' : ''}" data-sig="${KW}">
      <span class="k" style="background:#8a8a84"></span>
      <span class="n">${esc(t('sig.kw.short'))}</span>
      <span class="b">rule</span>
    </div>`);
  items.push(`<div class="sig-item ${selected === TW ? 'on' : ''}" data-sig="${TW}">
      <span class="k" style="background:#4a7d6b"></span>
      <span class="n">${esc(t('sig.tw.short'))}</span>
      <span class="b">rule</span>
    </div>`);
  $('#sigList').innerHTML = items.join('');
}

function instrHtml(k, s) {
  // Only the first wording is rendered at runtime; the others are kept in the
  // file as documentation (see the JEV module docstring).
  const list = Array.isArray(s.instructions) ? s.instructions : [s.instructions || ''];
  return `<div class="subcard"><div class="hd">${t('sig.instructions')}</div>
    <textarea data-instr="${esc(k)}">${esc(list[0] || '')}</textarea>
  </div>`;
}

function noulHtml(k, s) {
  return `<div class="subcard"><div class="hd">${t('sig.true')} / ${t('sig.false')}</div>
    <label class="field">${t('sig.true')}<textarea data-noul="${esc(k)}|true">${esc(s.true || '')}</textarea></label>
    <label class="field" style="margin-top:6px">${t('sig.false')}<textarea data-noul="${esc(k)}|false">${esc(s.false || '')}</textarea></label>
  </div>`;
}

function choiceHtml(k, s) {
  const cands = s.candidates || {};
  return `<div class="subcard"><div class="hd">${t('sig.candidates')}</div>
    <table class="cand-table"><thead><tr><th>${t('sig.candKey')}</th><th>${t('sig.candZh')}</th><th>${t('sig.candEn')}</th></tr></thead><tbody>
    ${Object.entries(cands).map(([key, val]) => {
      const v = typeof val === 'object' && val !== null ? val : { zh: val, en: val };
      return `<tr><td><span class="mono">${esc(key)}</span></td>
        <td><input data-cand="${esc(k)}|${esc(key)}|zh" value="${esc(v.zh || '')}"></td>
        <td><input data-cand="${esc(k)}|${esc(key)}|en" value="${esc(v.en || '')}"></td></tr>`;
    }).join('')}</tbody></table></div>`;
}

function piiHtml(s) {
  const types = s.types || {};
  return `<div class="subcard"><div class="hd">${t('sig.piiTemplate')}</div>
    <textarea style="width:100%;min-height:42px" data-pii="instructions">${esc(s.instructions || '')}</textarea>
    <label class="field" style="margin-top:6px">${t('sig.piiTrue')}<textarea data-pii="true">${esc(s.true || '')}</textarea></label>
    <label class="field" style="margin-top:6px">${t('sig.piiFalse')}<textarea data-pii="false">${esc(s.false || '')}</textarea></label>
  </div>
  <div class="subcard"><div class="hd">${t('sig.piiTypes')}</div>
    <table class="cand-table"><thead><tr><th>${t('sig.candKey')}</th><th>${t('sig.typeFa')}</th></tr></thead><tbody>
    ${Object.entries(types).map(([key, val]) => `<tr>
      <td><span class="mono">${esc(key)}</span></td>
      <td><input data-pii-type="${esc(key)}" value="${esc((val || {}).zh || '')}"></td></tr>`).join('')}
    </tbody></table></div>`;
}

function previewHtml(k, s) {
  const line = k === 'pii'
    ? String(s.instructions || '').replace('{zh}', '身份证号')
    : (Array.isArray(s.instructions) ? s.instructions[0] : s.instructions) || '';
  if (k === 'pii') {
    return `<div class="footnote">${t('sig.preview')}: ${esc(line)} · true=${esc(String(s.true || '').replace('{zh}', '身份证号'))}</div>`;
  }
  if (s.kind === 'noul') {
    return `<div class="footnote">${t('sig.preview')}: ${esc(line)}<br>Yes: ${esc(s.true || '')}<br>No: ${esc(s.false || '')}</div>`;
  }
  const cands = Object.keys(s.candidates || {});
  return `<div class="footnote">${t('sig.preview')}: ${esc(line)} (${cands.length} candidates: ${cands.slice(0, 6).map(esc).join(', ')}${cands.length > 6 ? '…' : ''})</div>`;
}

function rawHtml() {
  if (!rawOpen) return '';
  return `<div class="subcard" style="margin-top:12px">
    <div class="hd">${t('sig.raw')} <span class="mut" style="font-weight:400">${t('sig.raw.hint')}</span></div>
    <textarea class="json-editor" id="rawJson">${esc(JSON.stringify(cfg, null, 2))}</textarea>
    <div class="insp-row" style="margin-top:6px">
      <button class="btn sm" id="rawApply">${t('sig.raw.apply')}</button>
      <button class="btn sm" id="rawClose">${t('common.cancel')}</button>
    </div>
  </div>`;
}

function renderEditor() {
  if (selected === KW) { renderKwEditor(); return; }
  if (selected === TW) { renderTwEditor(); return; }
  const k = selected;
  const s = cfg.signals[k];
  const reg = SIGNAL_REGISTRY[k] || {};
  const head = `<div class="card">
    <div class="insp-h">
      <span class="type ${s.kind === 'noul' ? 'gate' : 'score'}">${kindLabel(k)}</span>
      <span class="t">${esc(sigName(k))}</span>
      <span class="mut">${esc(reg.en || '')}</span>
      <span class="stat" style="margin-left:auto">${t('sig.values')}: ${s.kind === 'noul' ? 2 : Object.keys(s.candidates || {}).length}</span>
    </div>
    ${k === 'pii' ? piiHtml(s) : instrHtml(k, s)}
    ${s.kind === 'noul' && k !== 'pii' ? noulHtml(k, s) : ''}
    ${s.kind === 'choice' ? choiceHtml(k, s) : ''}
    ${previewHtml(k, s)}
    <div class="footnote" style="margin-top:6px">${esc(JSON.stringify({ version: cfg.version, signals: Object.keys(cfg.signals).length }).replace(/"/g, ''))} ·
      <button class="btn ghost sm" id="rawToggle">${t('sig.raw')} ${rawOpen ? '▲' : '▼'}</button></div>
    ${rawHtml()}
  </div>`;
  $('#editor').innerHTML = head;
  bindEditor();
}

function bindEditor() {
  document.querySelectorAll('[data-instr]').forEach(el => el.addEventListener('input', () => {
    cfg.signals[el.dataset.instr].instructions = [el.value];
    markDirty();
  }));
  document.querySelectorAll('[data-noul]').forEach(el => el.addEventListener('input', () => {
    const [k, f] = el.dataset.noul.split('|');
    cfg.signals[k][f] = el.value;
    markDirty();
  }));
  document.querySelectorAll('[data-cand]').forEach(el => el.addEventListener('input', () => {
    const [k, key, f] = el.dataset.cand.split('|');
    const cands = cfg.signals[k].candidates;
    if (typeof cands[key] !== 'object' || cands[key] === null) cands[key] = { zh: cands[key], en: cands[key] };
    cands[key][f] = el.value;
    markDirty();
  }));
  document.querySelectorAll('[data-pii]').forEach(el => el.addEventListener('input', () => {
    cfg.signals.pii[el.dataset.pii] = el.value;
    markDirty();
  }));
  document.querySelectorAll('[data-pii-type]').forEach(el => el.addEventListener('input', () => {
    const t2 = cfg.signals.pii.types[el.dataset.piiType];
    if (typeof t2 !== 'object' || t2 === null) cfg.signals.pii.types[el.dataset.piiType] = { zh: el.value };
    else t2.zh = el.value;
    markDirty();
  }));
  const tog = $('#rawToggle');
  if (tog) tog.addEventListener('click', () => { rawOpen = !rawOpen; renderEditor(); });
  const apply = $('#rawApply');
  if (apply) apply.addEventListener('click', () => applyRaw());
  const close = $('#rawClose');
  if (close) close.addEventListener('click', () => { rawOpen = false; renderEditor(); });
}

function markDirty() { dirty = true; renderHeader(); }

function applyRaw() {
  try {
    const parsed = JSON.parse($('#rawJson').value);
    if (!parsed || typeof parsed !== 'object' || !parsed.signals) throw new Error('missing signals');
    cfg = parsed;
    rawOpen = false;
    dirty = true;
    renderAll();
    toast(t('common.saved'));
  } catch (err) {
    toast(tf('sig.raw.invalid', { err: err.message }));
  }
}

async function save() {
  try {
    const out = await api.put('/admin/signal-questions', { config: cfg });
    dirty = false;
    if (out.meta) meta = out.meta;
    renderHeader();
    toast(out.applied ? t('sig.savedLive') : t('sig.savedReload'));
  } catch (e) {
    toast((Array.isArray(e.detail) ? JSON.stringify(e.detail) : (e.detail || e.message)));
  }
}

function exportJson() {
  const blob = new Blob([JSON.stringify(cfg, null, 2) + '\n'], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'signal_questions.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function renderAll() {
  renderHeader(); renderList(); renderEditor();
}

$('#sigList').addEventListener('click', e => {
  const item = e.target.closest('[data-sig]');
  if (!item) return;
  selected = item.dataset.sig;
  renderList(); renderEditor();
});
$('#saveBtn').addEventListener('click', save);
$('#exportBtn').addEventListener('click', exportJson);
$('#importBtn').addEventListener('click', () => $('#importFile').click());
$('#importFile').addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(reader.result);
      if (!parsed || !parsed.signals) throw new Error('missing signals');
      cfg = parsed; dirty = true; renderAll();
      toast(t('sig.import'));
    } catch (err) {
      toast(tf('sig.raw.invalid', { err: err.message }));
    }
  };
  reader.readAsText(file);
  e.target.value = '';
});
window.onLangChange = () => renderAll();
boot();

/* ── keyword lexicon (rule signal "keyword" vocabulary) ──────────────── */
function renderKwEditor() {
  const rows = Object.entries(kwGroups).map(([name, words]) => `
    <div class="kw-group">
      <input class="kw-name" value="${esc(name)}" style="max-width:120px">
      <input class="kw-words" value="${esc((words || []).join(', '))}" placeholder="${esc(t('sig.kw.placeholder'))}">
      <button class="del" data-del-group title="${t('common.delete')}">✕</button>
    </div>`).join('');
  $('#editor').innerHTML = `<div class="card">
    <div class="insp-h">
      <span class="type gate">${esc(t('sig.kw.ruletype'))}</span>
      <span class="t">${esc(t('sig.kw.short'))}</span>
      <span class="mut">keyword</span>
      <span class="stat" style="margin-left:auto">${Object.keys(kwGroups).length} ${esc(t('sig.kw.groupsUnit'))}</span>
    </div>
    <div class="subcard">
      <div class="hd">${esc(t('sig.kw.groupsTitle'))}</div>
      <div class="footnote" style="margin:-2px 0 9px">${esc(t('sig.kw.hint'))}</div>
      <div id="kwGroups">${rows}</div>
      <div class="addrow" style="margin-top:8px">
        <button class="btn sm" id="addGroup">${esc(t('sig.addgroup'))}</button>
        <button class="btn pri sm" id="saveKw">${esc(t('sig.savekw'))}</button>
      </div>
    </div>
  </div>`;
  $('#editor').querySelectorAll('[data-del-group]').forEach(b =>
    b.addEventListener('click', () => b.closest('.kw-group').remove()));
  $('#addGroup').addEventListener('click', () => {
    const row = document.createElement('div');
    row.className = 'kw-group';
    row.innerHTML = `<input class="kw-name" value="" style="max-width:120px">
      <input class="kw-words" value="" placeholder="${esc(t('sig.kw.placeholder'))}">
      <button class="del" data-del-group title="${t('common.delete')}">✕</button>`;
    row.querySelector('[data-del-group]').addEventListener('click', () => row.remove());
    $('#kwGroups').appendChild(row);
    row.querySelector('.kw-name').focus();
  });
  $('#saveKw').addEventListener('click', async () => {
    const next = {};
    document.querySelectorAll('#kwGroups .kw-group').forEach(g => {
      const name = g.querySelector('.kw-name').value.trim();
      if (name) next[name] = g.querySelector('.kw-words').value
        .split(',').map(w => w.trim()).filter(Boolean);
    });
    try {
      await api.put('/admin/keyword-groups', { groups: next });
      kwGroups = next;
      renderKwEditor();
      toast(t('common.saved'));
    } catch (e) {
      toast((LANG === 'zh' ? '保存失败: ' : 'save failed: ') + (e.detail || e.message));
    }
  });
}


/* ── time period lexicon (rule signal "time_window") ─────────────────── */
function renderTwEditor() {
  const dows = t('sig.tw.dows').split(',');
  const rows = timePeriods.map((tp, i) => `
    <div class="tp-row" data-tp-row="${i}">
      <input class="tp-name" data-tp-name value="${esc(tp.name)}" placeholder="${esc(t('sig.tw.name'))}" style="max-width:110px">
      <span class="tp-days">${dows.map((d, di) =>
        `<button type="button" class="tp-day${tp.days.includes(di) ? ' on' : ''}" aria-pressed="${tp.days.includes(di)}"
                 data-tp-day title="${esc(d)}">${esc(d)}</button>`).join('')}</span>
      <input type="time" data-tp-start value="${esc(tp.start)}">
      <span class="mut">–</span>
      <input type="time" data-tp-end value="${esc(tp.end)}">
      <button class="del" data-del-tp title="${t('common.delete')}">✕</button>
    </div>`).join('');
  $('#editor').innerHTML = `<div class="card">
    <div class="insp-h">
      <span class="type gate">${esc(t('sig.kw.ruletype'))}</span>
      <span class="t">${esc(t('sig.tw.short'))}</span>
      <span class="mut">time_window</span>
      <span class="stat" style="margin-left:auto">${timePeriods.length} ${esc(t('sig.kw.groupsUnit'))}</span>
    </div>
    <div class="subcard">
      <div class="hd">${esc(t('sig.tw.groupsTitle'))}</div>
      <div class="footnote" style="margin:-2px 0 9px">${esc(t('sig.tw.hint'))}</div>
      <div id="tpRows">${rows}</div>
      <div class="addrow" style="margin-top:8px">
        <button class="btn sm" id="addTp">${esc(t('sig.tw.add'))}</button>
        <button class="btn pri sm" id="saveTw">${esc(t('sig.tw.save'))}</button>
      </div>
      <div class="footnote" id="twCurrent" style="margin-top:8px"></div>
    </div>
  </div>`;
  const root = $('#editor');
  root.querySelectorAll('.tp-row').forEach(row => {
    row.querySelectorAll('.tp-day').forEach(btn =>
      btn.addEventListener('click', () => {
        btn.classList.toggle('on');
        btn.setAttribute('aria-pressed', String(btn.classList.contains('on')));
      }));
    row.querySelector('[data-del-tp]').addEventListener('click', () => row.remove());
  });
  $('#addTp').addEventListener('click', () => {
    const row = document.createElement('div');
    row.className = 'tp-row';
    row.innerHTML = `
      <input class="tp-name" data-tp-name value="" placeholder="${esc(t('sig.tw.name'))}" style="max-width:110px">
      <span class="tp-days">${dows.map((d, di) =>
        `<button type="button" class="tp-day${di <= 4 ? ' on' : ''}" aria-pressed="${di <= 4}"
                 data-tp-day title="${esc(d)}">${esc(d)}</button>`).join('')}</span>
      <input type="time" data-tp-start value="09:00">
      <span class="mut">–</span>
      <input type="time" data-tp-end value="18:00">
      <button class="del" data-del-tp title="${t('common.delete')}">✕</button>`;
    row.querySelectorAll('.tp-day').forEach(btn =>
      btn.addEventListener('click', () => {
        btn.classList.toggle('on');
        btn.setAttribute('aria-pressed', String(btn.classList.contains('on')));
      }));
    row.querySelector('[data-del-tp]').addEventListener('click', () => row.remove());
    $('#tpRows').appendChild(row);
    row.querySelector('.tp-name').focus();
  });
  $('#saveTw').addEventListener('click', async () => {
    const periods = [];
    let bad = false;
    document.querySelectorAll('#tpRows .tp-row').forEach(row => {
      const name = row.querySelector('[data-tp-name]').value.trim();
      const days = [...row.querySelectorAll('.tp-day.on')]
        .map(b => Number(b.dataset.tpDay));
      const start = row.querySelector('[data-tp-start]').value;
      const end = row.querySelector('[data-tp-end]').value;
      if (!name || !days.length || !start || !end) { bad = true; return; }
      periods.push({ name, days: [...new Set(days)].sort((a, b) => a - b), start, end });
    });
    if (bad || !periods.length) { toast(t('sig.tw.invalid')); return; }
    try {
      const out = await api.put('/admin/time-periods', { periods });
      timePeriods = out.periods || periods;
      renderTwEditor();
      toast(t('common.saved'));
    } catch (e) {
      toast((LANG === 'zh' ? '保存失败: ' : 'save failed: ') + (e.detail || e.message));
    }
  });
}

/* ── server clock (the time_window rule judges by server time) ───────── */
/* The time_window signal's value IS the list of covering period names (the
   page hint says so), so the indicator shows the configured names — not a
   hardcoded weekday/weekend guess. */
function twPeriod(d) {
  const wd = (d.getDay() + 6) % 7;                 // Python weekday: Mon=0
  const hit = timePeriods.filter(p => p.days.includes(wd) && periodCovers(p, d));
  return hit.length ? [hit[0].name, hit.map(p => p.name).join(', ')] : null;
}
function periodCovers(p, d) {
  const minutes = d.getHours() * 60 + d.getMinutes();
  const hm = s => { const [h, m] = String(s).split(':').map(Number); return h * 60 + (m || 0); };
  const a = hm(p.start), b = hm(p.end);
  if (a === b) return true;
  if (a < b) return a <= minutes && minutes < b;
  return minutes >= a || minutes < b;
}
function startServerClock() {
  startServerClockTick(() => {
    const el = $('#serverClock');
    if (!el) return;
    const d = serverNow();
    el.textContent = `${t('sig.clock')} ${fmtServerTime(d)}`;
    const cur = $('#twCurrent');
    if (cur) {
      const hit = twPeriod(d);
      cur.textContent = hit
        ? `${t('sig.tw.current')}${hit[1]} (${hit[0]})`
        : `${t('sig.tw.current')}—`;
    }
  });
}
