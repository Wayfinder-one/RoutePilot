/* Models & settings page: model registry, providers, runtime settings and
   the remote JEV service summary. Model profile.capability is the text source
   for single-model JEV targets in routing.html.

   Modal #modelModal has two add paths:
     - preset: pick a catalog provider, optional API key, tick preset models,
       optionally rename their model slots (default = upstream id)
     - custom: define a model slot by hand, wire a provider (existing or inline
       new), declare its reasoning capability (profile.reasoning) and profile
   Modal #provModal edits one provider (catalog prefill, api_key three-state).

   Contract: docs/模型目录与推理配置-2026-10-21.md §5/§7. Old backends without
   /admin/model-catalog or without model.reasoning degrade gracefully (catalog
   notice, "未声明" badges) instead of throwing. */
const $ = s => document.querySelector(s);
const $$ = (s, root = document) => Array.from((root || document).querySelectorAll(s));

/* ── static protocol knowledge ───────────────────────────────────────── */
const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const LEVELS = ['off', 'on', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
/* The protocol table (formats, needs_wire, supports_sre) comes from the
   server: GET /admin/model-catalog -> formats[]. The console never mirrors
   router/reasoning.py, so a new backend dialect shows up here by itself. */

/* Upstream preset ids may contain '/' (e.g. 'openai/gpt-5.6-luna') but model
   slot ids may not; the preset tab normalizes them and dedupes against the
   registry + the other ticked models (user can still edit the slot). */
function sanitizeSlot(raw) {
  let s = String(raw == null ? '' : raw).trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[._-]+/, '');
  if (!s) s = 'model';
  return s.slice(0, 64);
}
function uniqueSlot(base, taken) {
  let slot = base, i = 2;
  while (taken.has(slot)) { slot = base.slice(0, 60) + '-' + i; i += 1; }
  return slot;
}

/* ── state ───────────────────────────────────────────────────────────── */
const STATE = {
  models: [], providers: [], settings: {}, jev: {},
  catalog: null,            // GET /admin/model-catalog, null when unavailable
  formats: [],              // [{id, zh, en}]
};

let modal = null;           // model modal state
let provModal = null;       // provider modal state

/* ── small helpers ───────────────────────────────────────────────────── */
function errText(e) {
  if (!e) return '';
  const d = e.detail;
  const pick = x => {
    if (x == null) return '';
    if (typeof x === 'string') return x;
    if (Array.isArray(x)) return x.map(pick).filter(Boolean).join('; ');
    if (typeof x === 'object') {
      if (x.zh || x.en) return LANG === 'zh' ? (x.zh || x.en) : (x.en || x.zh);
      return JSON.stringify(x);
    }
    return String(x);
  };
  const out = pick(d) || e.message || String(e);
  return out === '[object Object]' ? JSON.stringify(d || e) : out;
}
const reasonLabel = id => t('reason.' + id);
const fmtLabel = id => id;
function reasonDesc(id) {
  const f = STATE.formats.find(x => x.id === id);
  if (f) return LANG === 'zh' ? (f.zh || '') : (f.en || f.zh || '');
  return t('reason.format.' + id) === 'reason.format.' + id ? '' : t('reason.format.' + id);
}
function formatIds() {
  return STATE.formats.map(f => f.id);
}
function formatSpec(id) {
  return STATE.formats.find(f => f.id === id) || null;
}
/* does this dialect need a protocol spelling for every non-off level? */
function formatNeedsWire(id) {
  const spec = formatSpec(id);
  return !!(spec && spec.needs_wire);
}
/* does this dialect use the supports_reasoning_effort switch? */
function formatSupportsSre(id) {
  const spec = formatSpec(id);
  return !!(spec && spec.supports_sre);
}
function providerFormat(name) {
  const p = STATE.providers.find(x => x.name === name);
  return (p && p.reasoning_format) || 'openai';
}
function normReason(ri) {
  if (!ri || typeof ri !== 'object') return { ...FALLBACK_REASON };
  return ri;
}
function profileOf(model) {
  const raw = model && model.profile;
  if (!raw) return {};
  if (typeof raw === 'object') return { ...raw };
  // The server stores models.profile as a JSON *text* column, so it arrives
  // as a string. TODO(remove after the server returns an object).
  try { return JSON.parse(raw) || {}; } catch (e) { return {}; }
}
/* Natural-language capability description. GET /admin/models also returns the
   validated text as a top-level ``capability`` field; fall back to the raw
   profile for older backends so the list never renders empty by accident. */
function capabilityOf(model) {
  if (model && typeof model.capability === 'string') return model.capability;
  const cap = profileOf(model).capability;
  return typeof cap === 'string' ? cap : '';
}
function modelIds() { return new Set(STATE.models.map(m => m.id)); }
function providerNames() { return new Set(STATE.providers.map(p => p.name)); }
function providerOf(name) { return STATE.providers.find(p => p.name === name) || null; }

/* reasoning badge strips: 未声明 / 不支持 / 声明的档位（默认档位加圆点） */
function reasonChips(ri, opts) {
  const r = normReason(ri);
  if (r.error) {
    return `<span class="rbadge unset" title="${esc(tf('models.reason.error', { err: r.error }))}">${esc(t('models.reason.unset'))}</span>`;
  }
  if (!r.declared) return `<span class="rbadge unset">${esc(t('models.reason.unset'))}</span>`;
  if (r.supported === false) return `<span class="rbadge off">${esc(t('models.reason.unsupported'))}</span>`;
  const efforts = Array.isArray(r.efforts) ? r.efforts : [];
  if (!efforts.length) return `<span class="rbadge unset">${esc(t('models.reason.unset'))}</span>`;
  const title = fmtLabel(r.format || '') || '';
  const chips = efforts.map(e => {
    const def = e.id === r.default;
    return `<span class="rbadge on${def ? ' def' : ''}"${def ? ` title="${esc(t('models.reason.defaultMark'))}"` : ''}>` +
      `${def ? '<i class="rdot"></i>' : ''}${esc(reasonLabel(e.id))}</span>`;
  }).join('');
  return `<span class="rbadges" title="${esc(title)}">${chips}</span>` +
    (opts && opts.withFormat && title ? `<span class="mut" style="font-size:10.5px">${esc(title)}</span>` : '');
}

function formatOptions(selected, includeFollow) {
  const ids = formatIds();
  let html = '';
  if (includeFollow) {
    html += `<option value=""${!selected ? ' selected' : ''}>${esc(tf('models.reason.formatFollow', { format: includeFollow }))}</option>`;
  }
  html += ids.map(id => {
    const desc = reasonDesc(id);
    return `<option value="${esc(id)}"${id === selected ? ' selected' : ''} title="${esc(desc)}">${esc(id)}</option>`;
  }).join('');
  return html;
}

/* ── modal plumbing ──────────────────────────────────────────────────── */
function openModal(id) {
  const el = $('#' + id);
  if (!el) return;
  el.hidden = false;
  document.body.classList.add('modal-open');
}
function closeModal(id) {
  const el = $('#' + id);
  if (!el) return;
  el.hidden = true;
  if ($$('.modal').every(m => m.hidden)) document.body.classList.remove('modal-open');
  if (id === 'modelModal') modal = null;
  if (id === 'provModal') provModal = null;
}
function closeTopModal() {
  const open = $$('.modal').filter(m => !m.hidden);
  if (!open.length) return false;
  closeModal(open[open.length - 1].id);
  return true;
}
function modalMsg(which, text, bad) {
  const el = $('#' + (which === 'prov' ? 'provModalMsg' : 'modelModalMsg'));
  el.textContent = text || '';
  el.classList.toggle('bad', !!bad);
  el.classList.toggle('ok', !!text && !bad);
}

/* Two-step inline delete: 删除 -> 确认删除 (3s) -> delete. */
function armDelete(btn) {
  if (btn.dataset.armed === '1') return true;
  btn.dataset.armed = '1';
  btn.dataset.label = btn.textContent;
  btn.textContent = t('models.confirmDelete');
  btn.classList.add('armed');
  setTimeout(() => {
    if (!btn.isConnected) return;
    btn.dataset.armed = '0';
    btn.textContent = btn.dataset.label || t('models.delete');
    btn.classList.remove('armed');
  }, 3000);
  return false;
}

function renderEngine(config) {
  config = config || {};
  const pill = $('#enginePill');
  if (pill) pill.innerHTML = `<span class="dot" style="color:var(--indigo)"></span>JEV <b>${esc(config.model || '—')}</b>`;
  const card = $('#engineCard');
  if (!card) return;
  card.innerHTML = `<div class="col-h">JEV<span class="pill indigo">${esc(config.model || '—')}</span></div>
    <div class="bar-row"><span class="bl">${esc(config.base_url || '—')}</span></div>
    <div class="footnote"><a href="jev.html">${t('nav.jev')} →</a></div>`;
}

/* ── model registry ──────────────────────────────────────────────────── */
function renderModels() {
  const rows = STATE.models;
  const count = $('#modelCount');
  if (count) count.textContent = rows.length ? `· ${rows.length}` : '';
  const table = $('#modelTable');
  if (!table) return;
  const head = `<tr><th>${t('models.col.model')}</th><th>${t('models.col.upstream')}</th>
      <th>${t('models.col.provider')}</th><th>${t('models.col.reasoning')}</th>
      <th>${t('models.col.capability')}</th>
      <th class="th-actions">${t('models.col.actions')}</th></tr>`;
  if (!rows.length) {
    table.innerHTML = head + `<tr><td colspan="6"><div class="empty-cell">${esc(t('models.empty'))}</div></td></tr>`;
    return;
  }
  table.innerHTML = head + rows.map(m => {
    const capability = capabilityOf(m);
    const capCell = capability
      ? `<td class="cap-cell"><span class="cap-text" title="${esc(capability)}">${esc(capability)}</span></td>`
      : `<td class="cap-cell"><span class="mut" title="${esc(t('models.capability.unsetHint'))}">${esc(t('models.capability.unset'))}</span></td>`;
    return `<tr data-mrow="${esc(m.id)}">
      <td class="mono" style="font-size:11.5px">${esc(m.id)}</td>
      <td class="mono mut" style="font-size:11px">${esc(m.upstream || '—')}</td>
      <td>${esc(m.provider || '—')}</td>
      <td>${reasonChips(m.reasoning)}</td>
      ${capCell}
      <td class="row-actions">
        <button class="btn sm" data-editmodel="${esc(m.id)}">${esc(t('models.edit'))}</button>
        <button class="btn sm danger" data-delmodel="${esc(m.id)}">${esc(t('models.delete'))}</button>
      </td></tr>`;
  }).join('');
}

async function deleteModel(id) {
  try {
    await api.req('DELETE', `/admin/models/${encodeURIComponent(id)}`);
    toast(t('models.deleted') + ' · ' + id);
    await boot();
  } catch (e) {
    toast(t('models.deleteRef') + ' · ' + errText(e), 3600);
  }
}

/* ── providers ───────────────────────────────────────────────────────── */
function keyBadge(p) {
  // The server always sends key_source ('stored' | 'env' | 'none').
  const source = p.key_source || 'none';
  if (source === 'stored') return `<span class="keybadge ok">${esc(t('models.key.stored'))}</span>`;
  if (source === 'env') return `<span class="keybadge env">${esc(t('models.key.env'))}</span>`;
  return `<span class="keybadge none">${esc(t('models.key.none'))}</span>`;
}
function renderProviders() {
  const wrap = $('#providers');
  if (!wrap) return;
  const count = $('#provCount');
  if (count) count.textContent = STATE.providers.length ? `· ${STATE.providers.length}` : '';
  if (!STATE.providers.length) {
    wrap.innerHTML = `<div class="empty">${esc(t('models.prov.none'))}</div>`;
    return;
  }
  wrap.innerHTML = STATE.providers.map(p => `<div class="prov-card" data-prov="${esc(p.name)}">
      <div class="pc-head">
        <b>${esc(p.name)}</b>
        <span class="rbadge fmt">${esc(p.reasoning_format || 'openai')}</span>
      </div>
      <div class="pc-base mono" title="${esc(p.base_url || '')}">${esc(p.base_url || '—')}</div>
      <div class="pc-meta">${keyBadge(p)}
        <span class="mut">${esc(p.key_env || '—')}</span></div>
      <div class="pc-actions">
        <button class="btn sm" data-editprov="${esc(p.name)}">${esc(t('models.edit'))}</button>
        <button class="btn sm danger" data-delprov="${esc(p.name)}">${esc(t('models.delete'))}</button>
      </div>
    </div>`).join('');
}

async function deleteProvider(name) {
  try {
    await api.req('DELETE', `/admin/providers/${encodeURIComponent(name)}`);
    toast(t('models.deleted') + ' · ' + name);
    await boot();
  } catch (e) {
    toast(t('models.prov.ref') + ' · ' + errText(e), 3600);
  }
}

/* ── runtime settings (/admin/settings; pipeline_version is read-only) ─ */
function renderSettings(settings) {
  const rows = Object.entries(settings || {})
    .filter(([k]) => k !== 'pipeline_version')
    .map(([k, v]) => `<div class="set-row">
        <input class="s-key" value="${esc(k)}" style="max-width:190px">
        <input class="s-val" value="${esc(v)}">
        <button class="del" data-del-setting title="${t('common.delete')}">✕</button>
      </div>`).join('');
  const ver = settings && settings.pipeline_version
    ? `<div class="footnote">${t('models.pipelineversion')}：<b class="mono">${esc(settings.pipeline_version)}</b></div>` : '';
  $('#settingsRows').innerHTML = rows + ver;
  $$('[data-del-setting]').forEach(b =>
    b.addEventListener('click', () => b.closest('.set-row').remove()));
}
function readSettings() {
  const out = {};
  $$('#settingsRows .set-row').forEach(row => {
    const k = row.querySelector('.s-key').value.trim();
    if (k) out[k] = row.querySelector('.s-val').value;
  });
  return out;
}

/* ══ model modal: preset tab ══════════════════════════════════════════ */
function catalogProviders() {
  return (STATE.catalog && Array.isArray(STATE.catalog.providers)) ? STATE.catalog.providers : [];
}
function ensurePresetSlots() {
  const taken = new Set(STATE.models.map(m => m.id));
  Object.keys(modal.slots).forEach(up => {
    if (modal.checked.has(up) && modal.slots[up]) taken.add(modal.slots[up]);
  });
  [...modal.checked].forEach(up => {
    const cur = modal.slots[up];
    if (cur != null && cur !== '') return;      // keep explicit/user-edited slots
    const slot = uniqueSlot(sanitizeSlot(up), taken);
    taken.add(slot);
    modal.slots[up] = slot;
  });
}
function presetBody() {
  ensurePresetSlots();
  if (!STATE.catalog) {
    return `<div class="notice warn">${esc(t('models.preset.catalogMissing'))}</div>
      <div class="hint">${esc(t('models.tab.custom'))} →</div>`;
  }
  const list = catalogProviders();
  if (!list.length) return `<div class="notice warn">${esc(t('models.preset.catalogMissing'))}</div>`;
  const pid = modal.catalogProvider && list.some(p => p.id === modal.catalogProvider)
    ? modal.catalogProvider : list[0].id;
  modal.catalogProvider = pid;
  const prov = list.find(p => p.id === pid) || list[0];
  const existing = providerOf(pid);
  const env = prov.key_env || '';
  const envLine = existing
    ? `<span class="keyslot">${keyBadge(existing)}</span><span class="mut">${esc(env || '—')}</span>`
    : `<span class="keyslot">${prov.key_env_present
        ? `<span class="keybadge ok">${esc(tf('models.preset.envPresent', { env }))}</span>`
        : `<span class="keybadge none">${esc(tf('models.preset.envMissing', { env: env || '—' }))}</span>`}</span>`;
  const modelRows = (prov.models || []).map(m => {
    const checked = modal.checked.has(m.id);
    const slot = modal.slots[m.id] != null ? modal.slots[m.id] : sanitizeSlot(m.id);
    const ctx = m.context_window ? `<span class="mut">${esc(tf('models.preset.ctx', { n: m.context_window }))}</span>` : '';
    return `<div class="preset-model">
        <label class="pm-l">
          <input type="checkbox" class="pm-check" data-mid="${esc(m.id)}"${checked ? ' checked' : ''}>
          <span class="pm-name">${esc(m.name || m.id)}</span>
          <span class="pm-up mono">${esc(m.id)}</span>${ctx}
          ${reasonChips(m.reasoning || (m.profile && m.profile.reasoning))}
        </label>
        <div class="pm-idslot" data-slot="${esc(m.id)}"${checked ? '' : ' hidden'}>
          <label class="field"><span>${esc(t('models.preset.slot'))}</span>
            <input class="pm-id" data-mid="${esc(m.id)}" value="${esc(slot)}"></label>
          <span class="mut pm-slot-hint">${esc(t('models.preset.slotHint'))}</span>
          <span class="pm-conflict" data-conflict="${esc(m.id)}" hidden>${esc(t('models.preset.slotConflict'))}</span>
        </div>
      </div>`;
  }).join('');
  return `
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.preset.catalog'))}</span>
        <select id="pmProvider">${list.map(p =>
          `<option value="${esc(p.id)}"${p.id === pid ? ' selected' : ''}>${esc(p.name)} · ${esc(p.base_url)}</option>`).join('')}</select></label>
      <label class="field"><span>${esc(t('models.preset.baseurl'))}</span>
        <input id="pmBase" value="${esc(prov.base_url || '')}" readonly></label>
    </div>
    <div class="keyline">${envLine}</div>
    <label class="field"><span>${esc(t('models.preset.key'))}</span>
      <input id="pmKey" type="password" autocomplete="off" value="${esc(modal.keyValue || '')}"
             placeholder="${esc(tf('models.preset.keyPlaceholder', { env: env || '—' }))}"></label>
    <div class="col-h sm">${esc(t('models.preset.pickModels'))}</div>
    <div class="preset-models" id="pmModels">${modelRows || `<div class="empty">${esc(t('common.none'))}</div>`}</div>`;
}
function refreshPresetSlots() {
  const checked = [...modal.checked];
  const values = checked.map(up => {
    const input = $(`#pmModels .pm-id[data-mid="${CSS.escape(up)}"]`);
    return input ? input.value.trim() : (modal.slots[up] || '');
  });
  const dup = new Set(values.filter((v, i) => v && values.indexOf(v) !== i));
  $$('#pmModels .preset-model').forEach(row => {
    const chk = row.querySelector('.pm-check');
    const slot = row.querySelector('.pm-idslot');
    if (slot) slot.hidden = !chk.checked;
    const input = row.querySelector('.pm-id');
    const warn = row.querySelector('.pm-conflict');
    if (!input || !warn) return;
    const id = input.value.trim();
    const invalid = !id || !MODEL_ID_RE.test(id);
    if (invalid || modelIds().has(id) || dup.has(id)) {
      warn.hidden = false;
      warn.textContent = dup.has(id) ? t('models.preset.slotDup') : t('models.preset.slotConflict');
    } else {
      warn.hidden = true;
    }
  });
}
function presetSlotIds() {
  return [...modal.checked].map(up => {
    const input = $(`#pmModels .pm-id[data-mid="${CSS.escape(up)}"]`);
    const raw = input ? input.value.trim() : '';
    return { upstream: up, slot: raw || up };
  });
}
async function savePreset() {
  const list = catalogProviders();
  const pid = modal.catalogProvider;
  const prov = list.find(p => p.id === pid);
  if (!prov) { modalMsg('model', t('models.preset.noneSelected'), true); return; }
  if (!modal.checked.size) { modalMsg('model', t('models.preset.noneSelected'), true); return; }
  const pairs = presetSlotIds();
  const seen = new Set();
  for (const { upstream, slot } of pairs) {
    if (!MODEL_ID_RE.test(slot)) {
      modalMsg('model', t('models.custom.idInvalid') + ' · ' + slot, true); return;
    }
    if (modelIds().has(slot)) {
      modalMsg('model', t('models.preset.slotConflict') + ' · ' + slot, true); return;
    }
    if (seen.has(slot)) { modalMsg('model', t('models.preset.slotDup') + ' · ' + slot, true); return; }
    seen.add(slot);
    if (!(prov.models || []).some(m => m.id === upstream)) {
      modalMsg('model', t('models.preset.noneSelected'), true); return;
    }
  }
  const btn = $('#modelModalSave');
  btn.disabled = true;
  try {
    const existing = providerOf(pid);
    const key = (modal.keyValue || '').trim();
    if (!existing) {
      const body = { base_url: prov.base_url, key_env: prov.key_env || '',
                     reasoning_format: prov.reasoning_format || 'openai' };
      if (key) body.api_key = key;
      await api.put(`/admin/providers/${encodeURIComponent(pid)}`, body);
    } else if (key) {
      await api.put(`/admin/providers/${encodeURIComponent(pid)}`, {
        base_url: existing.base_url,
        key_env: existing.key_env || '',
        reasoning_format: existing.reasoning_format || 'openai',
        api_key: key,
      });
    }
    for (const { upstream, slot } of pairs) {
      const preset = (prov.models || []).find(m => m.id === upstream) || {};
      const profile = preset.profile && typeof preset.profile === 'object'
        ? preset.profile
        : { domains: preset.domains || [], tier: preset.tier || '' };
      await api.post('/admin/models', { id: slot, upstream, provider: pid, profile });
    }
    toast(tf('models.preset.added', { n: pairs.length }));
    closeModal('modelModal');
    await boot();
  } catch (e) {
    modalMsg('model', errText(e), true);
  } finally {
    btn.disabled = false;
  }
}

/* ══ model modal: custom tab ══════════════════════════════════════════ */
function customDraft() {
  return modal.custom || (modal.custom = {});
}
function customBody() {
  const d = customDraft();
  const editing = modal.mode === 'edit';
  const model = editing ? STATE.models.find(m => m.id === modal.editId) : null;
  const providers = STATE.providers;
  const pid = d.provider != null ? d.provider : (model && model.provider) || (providers[0] && providers[0].name) || '';
  const pidKnown = providers.some(p => p.name === pid);
  const slot = d.id != null ? d.id : (model && model.id) || '';
  const upstream = d.upstream != null ? d.upstream : (model && model.upstream) || '';
  const p = model ? profileOf(model) : {};
  const tier = d.tier != null ? d.tier : (p.tier || '');
  const domains = d.domains != null ? d.domains : (Array.isArray(p.domains) ? p.domains.join(', ') : '');
  const capability = d.capability != null ? d.capability : (p.capability || '');
  const fetched = modal.fetched || [];
  const upstreamOpts = [...new Set([...fetched, ...catalogModelIds()])];
  const provOpts = providers.map(x =>
    `<option value="${esc(x.name)}"${x.name === pid ? ' selected' : ''}>${esc(x.name)}${x.base_url ? ' · ' + esc(x.base_url) : ''}</option>`).join('');
  return `
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.custom.slot'))}</span>
        <input id="cmId" value="${esc(slot)}"${editing ? ' readonly' : ''} placeholder="my-model-slot">
        <small>${esc(t('models.custom.slotHint'))}</small></label>
      <label class="field"><span>${esc(t('models.custom.upstream'))}</span>
        <input id="cmUpstream" list="cmUpstreamOpts" value="${esc(upstream)}" placeholder="gpt-5.2">
        <datalist id="cmUpstreamOpts">${upstreamOpts.map(x => `<option value="${esc(x)}">`).join('')}</datalist>
        <small class="fetch-line">
          <button type="button" class="btn sm" id="fetchModels">${esc(t('models.custom.fetch'))}</button>
          <span class="mut" id="fetchMsg"></span>
        </small></label>
    </div>
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.custom.provider'))}</span>
        <select id="cmProvider">${pidKnown ? provOpts : `<option value="" selected>${esc(t('common.none'))}</option>${provOpts}`}</select>
        <small><button type="button" class="linkbtn" id="cmNewProvider">${esc(t('models.custom.newProvider'))}</button></small></label>
      <div class="field"><span>&nbsp;</span>
        <span class="mut" style="font-size:11px">${esc(t('models.custom.newProviderHint'))}</span></div>
    </div>
    <div class="inline-form" id="cmProvInline"${d.provOpen ? '' : ' hidden'}>
      <div class="col-h sm">${esc(t('models.custom.newProvider'))}</div>
      <div class="form-grid">
        <label class="field"><span>${esc(t('models.prov.name'))}</span><input id="dprovName" value="${esc((d.provDraft && d.provDraft.name) || '')}"></label>
        <label class="field"><span>${esc(t('models.prov.baseurl'))}</span><input id="dprovBase" value="${esc((d.provDraft && d.provDraft.base_url) || '')}" placeholder="https://api.example.com/v1"></label>
      </div>
      <div class="form-grid">
        <label class="field"><span>${esc(t('models.prov.keyenv'))}</span><input id="dprovKeyEnv" value="${esc((d.provDraft && d.provDraft.key_env) || '')}" placeholder="EXAMPLE_API_KEY"></label>
        <label class="field"><span>${esc(t('models.prov.format'))}</span>
          <select id="dprovFormat">${formatOptions((d.provDraft && d.provDraft.reasoning_format) || 'openai')}</select></label>
      </div>
      <label class="field"><span>${esc(t('models.prov.apiKey'))}</span>
        <input id="dprovKey" type="password" autocomplete="off" value="${esc((d.provDraft && d.provDraft.api_key) || '')}"
               placeholder="${esc(t('models.prov.keyEmptyHint'))}"></label>
    </div>
    <label class="field"><span>${esc(t('models.custom.capability'))}</span>
      <textarea id="cmCapability" rows="3" placeholder="${esc(t('models.custom.capabilityHint'))}">${esc(capability)}</textarea>
      <small>${esc(t('models.custom.capabilityHint'))}</small></label>
    <div id="reasonEditor">${reasonEditorBody()}</div>
    <details class="adv"${d.advOpen ? ' open' : ''}>
      <summary>${esc(t('models.custom.advanced'))}</summary>
      <div class="form-grid">
        <label class="field"><span>${esc(t('models.custom.tier'))}</span><input id="cmTier" value="${esc(tier)}" placeholder="large / small"></label>
        <label class="field"><span>${esc(t('models.custom.domains'))}</span><input id="cmDomains" value="${esc(domains)}" placeholder="health, computer_science"></label>
      </div>
    </details>`;
}
function catalogModelIds() {
  const out = [];
  catalogProviders().forEach(p => (p.models || []).forEach(m => out.push(m.id)));
  return out;
}

/* reasoning declaration editor (profile.reasoning) */
function rmState() {
  if (!modal.rm) {
    modal.rm = { mode: 'omit', format: '', checked: new Set(), wires: {}, def: '', sre: true };
  }
  return modal.rm;
}
function initReasonFromModel(model) {
  const ri = normReason(model && model.reasoning);
  const rm = rmState();
  rm.mode = !ri.declared ? 'omit' : (ri.supported === false ? 'no' : 'yes');
  rm.format = (ri.declared && ri.format) || '';
  rm.checked = new Set((ri.efforts || []).map(e => e.id).filter(id => LEVELS.includes(id)));
  rm.wires = {};
  (ri.efforts || []).forEach(e => { rm.wires[e.id] = e.wire == null ? '' : String(e.wire); });
  rm.def = ri.default || '';
  rm.sre = ri.supports_reasoning_effort !== false;
  rm.error = ri.error || '';
  rm.provider = model ? model.provider : '';
}
function effectiveRmFormat() {
  const rm = rmState();
  if (rm.format) return rm.format;
  return providerFormat(currentCustomProvider());
}
function currentCustomProvider() {
  const d = customDraft();
  const edited = modal.mode === 'edit' ? STATE.models.find(m => m.id === modal.editId) : null;
  return d.provider != null ? d.provider : (edited && edited.provider) || (STATE.providers[0] && STATE.providers[0].name) || '';
}
function reasonEditorBody() {
  const rm = rmState();
  const followFmt = providerFormat(currentCustomProvider());
  const eff = effectiveRmFormat();
  const needsWire = formatNeedsWire(eff);
  const levelRows = LEVELS.map(id => {
    const on = rm.checked.has(id);
    const wire = rm.wires[id] != null ? rm.wires[id] : '';
    const required = needsWire && id !== 'off';
    return `<div class="rl-row${on ? ' on' : ''}" data-level="${esc(id)}">
        <label class="rl-check"><input type="checkbox" data-rl="${esc(id)}"${on ? ' checked' : ''}>
          <span class="rl-name">${esc(reasonLabel(id))}</span><span class="mono mut">${esc(id)}</span></label>
        <input class="rl-wire mono" data-wire="${esc(id)}" value="${esc(wire)}"
               placeholder="${esc(required ? t('models.reason.wireRequired') : t('models.reason.wirePh'))}"${on ? '' : ' disabled'}>
      </div>`;
  }).join('');
  const defOpts = [`<option value="">${esc(t('models.reason.noDefault'))}</option>`].concat(
    LEVELS.filter(id => rm.checked.has(id)).map(id =>
      `<option value="${esc(id)}"${id === rm.def ? ' selected' : ''}>${esc(reasonLabel(id))}</option>`)).join('');
  const err = rm.error
    ? `<div class="notice bad">${esc(tf('models.reason.error', { err: rm.error }))}</div>` : '';
  return `<div class="reason-editor">
    <div class="col-h sm">${esc(t('models.reason.title'))}</div>
    <p class="hint">${esc(t('models.reason.note'))}</p>
    ${err}
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.reason.mode'))}</span>
        <select id="rmMode">
          <option value="omit"${rm.mode === 'omit' ? ' selected' : ''}>${esc(t('models.reason.omit'))}</option>
          <option value="no"${rm.mode === 'no' ? ' selected' : ''}>${esc(t('models.reason.no'))}</option>
          <option value="yes"${rm.mode === 'yes' ? ' selected' : ''}>${esc(t('models.reason.yes'))}</option>
        </select></label>
      <label class="field"><span>${esc(t('models.reason.format'))}</span>
        <select id="rmFormat"${rm.mode === 'yes' ? '' : ' disabled'}>${formatOptions(rm.format, followFmt)}</select>
        <small class="mut" id="rmFormatDesc">${esc(reasonDesc(eff))}</small></label>
      <label class="field"><span>${esc(t('models.reason.default'))}</span>
        <select id="rmDefault"${rm.mode === 'yes' ? '' : ' disabled'}>${defOpts}</select></label>
    </div>
    <div class="reason-detail"${rm.mode === 'yes' ? '' : ' hidden'}>
      <div class="col-h sm">${esc(t('models.reason.levels'))}</div>
      <div class="rl-table" id="rmLevels">${levelRows}</div>
      <label class="checkline" id="rmSreLine"${formatSupportsSre(eff) ? '' : ' hidden'}>
        <input type="checkbox" id="rmSre"${rm.sre ? ' checked' : ''}> ${esc(t('models.reason.sre'))}</label>
    </div>
  </div>`;
}

/* keep the small reason UI bits in sync after mode/format/level changes */
function syncReasonEditor() {
  const rm = rmState();
  const eff = effectiveRmFormat();
  const needsWire = formatNeedsWire(eff);
  const mode = $('#rmMode');
  if (mode) rm.mode = mode.value;
  const fmt = $('#rmFormat');
  if (fmt) { rm.format = fmt.value; }
  const desc = $('#rmFormatDesc');
  if (desc) desc.textContent = reasonDesc(eff);
  const detail = $('#reasonEditor .reason-detail');
  if (detail) detail.hidden = rm.mode !== 'yes';
  ['#rmFormat', '#rmDefault'].forEach(sel => {
    const el = $(sel);
    if (el) el.disabled = rm.mode !== 'yes';
  });
  const sreLine = $('#rmSreLine');
  if (sreLine) sreLine.hidden = !formatSupportsSre(eff);
  $$('#rmLevels .rl-row').forEach(row => {
    const id = row.dataset.level;
    row.classList.toggle('on', rm.checked.has(id));
    const wire = row.querySelector('[data-wire]');
    if (wire) {
      wire.disabled = !rm.checked.has(id);
      const required = needsWire && id !== 'off';
      wire.placeholder = rm.checked.has(id)
        ? (required ? t('models.reason.wireRequired') : t('models.reason.wirePh')) : '';
      wire.classList.toggle('req', required && !wire.value.trim());
    }
  });
  // rebuild the default dropdown to only offer checked levels
  const def = $('#rmDefault');
  if (def) {
    if (!rm.checked.has(rm.def)) rm.def = '';
    def.innerHTML = [`<option value="">${esc(t('models.reason.noDefault'))}</option>`].concat(
      LEVELS.filter(id => rm.checked.has(id)).map(id =>
        `<option value="${esc(id)}"${id === rm.def ? ' selected' : ''}>${esc(reasonLabel(id))}</option>`)).join('');
  }
}
function reasonFromEditor() {
  const rm = rmState();
  const eff = effectiveRmFormat();
  const needsWire = formatNeedsWire(eff);
  if (rm.mode === 'omit') return { kind: 'omit' };
  if (rm.mode === 'no') return { kind: 'false' };
  const efforts = {};
  let nonOff = 0;
  LEVELS.filter(id => rm.checked.has(id)).forEach(id => {
    const wire = (rm.wires[id] || '').trim();
    if (needsWire && id !== 'off' && !wire) {
      throw new Error(tf('models.reason.wireMissing', { level: reasonLabel(id) }));
    }
    if (id !== 'off') nonOff += 1;
    efforts[id] = wire || null;
  });
  if (!nonOff) throw new Error(t('models.reason.needLevel'));
  const reasoning = { efforts };
  if (rm.format) reasoning.format = rm.format;
  if (rm.def && rm.checked.has(rm.def)) reasoning.default = rm.def;
  if (formatSupportsSre(eff)) reasoning.supports_reasoning_effort = !!rm.sre;
  return { kind: 'object', value: reasoning };
}

/* capture / restore the custom tab across tab switches & language changes */
function captureCustom() {
  const d = customDraft();
  const g = sel => { const el = $(sel); return el ? el.value : undefined; };
  if ($('#cmId')) d.id = g('#cmId');
  if ($('#cmUpstream')) d.upstream = g('#cmUpstream');
  if ($('#cmProvider')) d.provider = g('#cmProvider');
  if ($('#cmTier')) d.tier = g('#cmTier');
  if ($('#cmDomains')) d.domains = g('#cmDomains');
  if ($('#cmCapability')) d.capability = g('#cmCapability');
  d.provOpen = $('#cmProvInline') ? !$('#cmProvInline').hidden : false;
  if (d.provOpen) {
    d.provDraft = {
      name: g('#dprovName') || '', base_url: g('#dprovBase') || '',
      key_env: g('#dprovKeyEnv') || '', reasoning_format: g('#dprovFormat') || 'openai',
      api_key: g('#dprovKey') || '',
    };
  }
  d.advOpen = $('details.adv') ? $('details.adv').open : false;
  const sre = $('#rmSre');
  if (sre) rmState().sre = sre.checked;
}
async function saveCustom() {
  const editing = modal.mode === 'edit';
  const id = ($('#cmId') ? $('#cmId').value.trim() : '');
  const upstream = ($('#cmUpstream') ? $('#cmUpstream').value.trim() : '');
  let provider = ($('#cmProvider') ? $('#cmProvider').value.trim() : '');
  const tier = ($('#cmTier') ? $('#cmTier').value.trim() : '');
  const domains = ($('#cmDomains') ? $('#cmDomains').value : '')
    .split(/[,，]/).map(s => s.trim()).filter(Boolean);
  if (!editing && !MODEL_ID_RE.test(id)) { modalMsg('model', t('models.custom.idInvalid'), true); return; }
  if (!upstream || !provider) { modalMsg('model', t('models.custom.required'), true); return; }
  const btn = $('#modelModalSave');
  btn.disabled = true;
  try {
    // inline "new provider" form wins when open and named
    const inline = $('#cmProvInline');
    let providerReady = providerNames().has(provider);
    if (inline && !inline.hidden) {
      const pname = $('#dprovName').value.trim();
      const pbase = $('#dprovBase').value.trim();
      if (!pname || !pbase) { modalMsg('model', t('models.prov.required'), true); return; }
      const body = {
        base_url: pbase,
        key_env: ($('#dprovKeyEnv').value || '').trim(),
        reasoning_format: $('#dprovFormat').value || 'openai',
      };
      const key = ($('#dprovKey').value || '').trim();
      if (key) body.api_key = key;
      await api.put(`/admin/providers/${encodeURIComponent(pname)}`, body);
      provider = pname;
      providerReady = true;
    }
    if (!providerReady) { modalMsg('model', t('models.custom.required'), true); return; }
    // build the profile: keep unknown keys, refresh domains/tier/reasoning
    const base = editing
      ? profileOf(STATE.models.find(m => m.id === modal.editId))
      : {};
    const profile = { ...base };
    delete profile.reasoning;
    profile.domains = domains;
    if (tier) profile.tier = tier; else delete profile.tier;
    const capability = ($('#cmCapability') ? $('#cmCapability').value.trim() : '');
    if (capability) profile.capability = capability; else delete profile.capability;
    const reason = reasonFromEditor();
    if (reason.kind === 'false') profile.reasoning = false;
    else if (reason.kind === 'object') profile.reasoning = reason.value;
    if (editing) {
      await api.put(`/admin/models/${encodeURIComponent(modal.editId)}`,
        { upstream, provider, profile });
    } else {
      await api.post('/admin/models', { id, upstream, provider, profile });
    }
    toast(t('common.saved'));
    closeModal('modelModal');
    await boot();
  } catch (e) {
    modalMsg('model', errText(e), true);
  } finally {
    btn.disabled = false;
  }
}

/* ── model modal: open / render ─────────────────────────────────────── */
function openModelModal(kind, modelId) {
  modal = {
    mode: kind, tab: kind === 'edit' ? 'custom' : 'preset',
    editId: modelId || null,
    catalogProvider: null, checked: new Set(), slots: {}, keyValue: '',
    fetched: [], custom: null, rm: null,
  };
  const model = kind === 'edit' ? STATE.models.find(m => m.id === modelId) : null;
  if (kind === 'edit' && !model) return;
  if (model) {
    initReasonFromModel(model);
    const p = profileOf(model);
    modal.custom = {
      id: model.id, upstream: model.upstream || '', provider: model.provider || '',
      tier: p.tier || '', domains: Array.isArray(p.domains) ? p.domains.join(', ') : '',
      capability: p.capability || '',
      provOpen: false, advOpen: false,
    };
  } else {
    rmState();
    initReasonFromModel({ reasoning: null, provider: '' });
  }
  $('#modelModalTitle').textContent = kind === 'edit' ? t('models.editTitle') : t('models.addTitle');
  $('#modelTabs').hidden = kind === 'edit';
  $$('#modelTabs .mtab').forEach(b => b.classList.toggle('on', b.dataset.tab === modal.tab));
  $('#modelModalSave').textContent = t('models.save');
  modalMsg('model', '');
  renderModelModal();
  openModal('modelModal');
}
function renderModelModal() {
  if (!modal) return;
  const tabs = $('#modelTabs');
  if (tabs) tabs.hidden = modal.mode === 'edit';
  $$('#modelTabs .mtab').forEach(b => b.classList.toggle('on', b.dataset.tab === modal.tab));
  $('#modelModalTitle').textContent = modal.mode === 'edit' ? t('models.editTitle') : t('models.addTitle');
  const save = $('#modelModalSave');
  if (save) save.textContent = t('models.save');
  $('#modelModalBody').innerHTML = modal.tab === 'custom' ? customBody() : presetBody();
}

/* ── provider modal ─────────────────────────────────────────────────── */
function openProvModal(name) {
  const existing = name ? providerOf(name) : null;
  provModal = { name: name || null, mode: name ? 'edit' : 'add',
    keyValue: '', clearKey: false, presetId: existing ? existing.name : '' };
  $('#provModalTitle').textContent = name ? t('models.prov.editTitle') : t('models.prov.addTitle');
  modalMsg('prov', '');
  renderProvModal();
  openModal('provModal');
}
function provModalBody() {
  const p = provModal || {};
  const existing = p.name ? providerOf(p.name) : null;
  const v = k => {
    if (p.draft && p.draft[k] != null) return p.draft[k];
    if (existing) {
      if (k === 'name') return existing.name;
      if (k === 'base_url') return existing.base_url || '';
      if (k === 'key_env') return existing.key_env || '';
      if (k === 'reasoning_format') return existing.reasoning_format || 'openai';
    }
    return k === 'reasoning_format' ? 'openai' : '';
  };
  const cps = catalogProviders();
  const presetOpts = [`<option value="">${esc(t('models.prov.presetManual'))}</option>`].concat(
    cps.map(c => `<option value="${esc(c.id)}"${p.presetId === c.id ? ' selected' : ''}>${esc(c.name)} · ${esc(c.base_url)}</option>`)).join('');
  const status = existing ? `<div class="keyline">${keyBadge(existing)}<span class="mono mut">${esc(existing.key_env || '—')}</span></div>` : '';
  return `
    <label class="field"><span>${esc(t('models.prov.preset'))}</span>
      <select id="pvPreset"${cps.length ? '' : ' disabled'}>${presetOpts}</select></label>
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.prov.name'))}</span>
        <input id="pvName" value="${esc(v('name'))}"${existing ? ' readonly' : ''} placeholder="openai"></label>
      <label class="field"><span>${esc(t('models.prov.baseurl'))}</span>
        <input id="pvBase" value="${esc(v('base_url'))}" placeholder="https://api.example.com/v1"></label>
    </div>
    <div class="form-grid">
      <label class="field"><span>${esc(t('models.prov.keyenv'))}</span>
        <input id="pvKeyEnv" value="${esc(v('key_env'))}" placeholder="EXAMPLE_API_KEY"></label>
      <label class="field"><span>${esc(t('models.prov.format'))}</span>
        <select id="pvFormat">${formatOptions(v('reasoning_format'))}</select>
        <small class="mut">${esc(reasonDesc(v('reasoning_format')))}</small></label>
    </div>
    ${status}
    <label class="field"><span>${esc(t('models.prov.apiKey'))}</span>
      <input id="pvKey" type="password" autocomplete="off" value="${esc(p.keyValue || '')}"
             placeholder="${esc(t('models.prov.keyKeep'))}"></label>
    <div class="keyline">
      <label class="checkline"><input type="checkbox" id="pvClearKey"${p.clearKey ? ' checked' : ''}>
        ${esc(t('models.prov.clearKey'))}</label>
      <span class="mut">${esc(t('models.prov.keyEmptyHint'))}</span>
    </div>
    ${existing ? `<div class="pc-actions" style="margin-top:10px">
        <button class="btn sm danger" id="pvDelete">${esc(t('models.delete'))}</button></div>` : ''}`;
}
function renderProvModal() {
  if (!provModal) return;
  $('#provModalTitle').textContent = provModal.mode === 'edit' ? t('models.prov.editTitle') : t('models.prov.addTitle');
  $('#provModalBody').innerHTML = provModalBody();
}
function captureProvModal() {
  if (!provModal) return;
  const g = sel => { const el = $(sel); return el ? el.value : undefined; };
  provModal.draft = {
    name: g('#pvName'), base_url: g('#pvBase'), key_env: g('#pvKeyEnv'),
    reasoning_format: g('#pvFormat'),
  };
  provModal.keyValue = g('#pvKey') || '';
  provModal.clearKey = $('#pvClearKey') ? $('#pvClearKey').checked : false;
  provModal.presetId = g('#pvPreset') || '';
}
async function saveProviderModal() {
  captureProvModal();
  const name = ($('#pvName').value || '').trim();
  const base = ($('#pvBase').value || '').trim();
  if (!MODEL_ID_RE.test(name)) { modalMsg('prov', t('models.prov.required'), true); return; }
  if (!base) { modalMsg('prov', t('models.prov.required'), true); return; }
  const body = {
    base_url: base,
    key_env: ($('#pvKeyEnv').value || '').trim(),
    reasoning_format: $('#pvFormat').value || 'openai',
  };
  const key = ($('#pvKey').value || '').trim();
  const clear = $('#pvClearKey').checked;
  if (clear) body.api_key = '';
  else if (key) body.api_key = key;
  const btn = $('#provModalSave');
  btn.disabled = true;
  try {
    await api.put(`/admin/providers/${encodeURIComponent(name)}`, body);
    toast(t('common.saved'));
    closeModal('provModal');
    await boot();
  } catch (e) {
    modalMsg('prov', errText(e), true);
  } finally {
    btn.disabled = false;
  }
}
async function deleteProvInModal() {
  if (!provModal || !provModal.name) return;
  const btn = $('#pvDelete');
  if (!(await armDelete(btn))) return;
  try {
    await api.req('DELETE', `/admin/providers/${encodeURIComponent(provModal.name)}`);
    toast(t('models.deleted'));
    closeModal('provModal');
    await boot();
  } catch (e) {
    modalMsg('prov', t('models.prov.ref') + ' · ' + errText(e), true);
    btn.dataset.armed = '0';
    btn.textContent = t('models.delete');
  }
}

/* ══ fetch available models (POST /admin/fetch-models) ════════════════ */
async function fetchAvailableModels() {
  const btn = $('#fetchModels');
  const msg = $('#fetchMsg');
  if (!btn || !msg) return;
  const inline = $('#cmProvInline');
  const draftOpen = inline && !inline.hidden;
  let body;
  if (draftOpen) {
    const base = ($('#dprovBase') ? $('#dprovBase').value.trim() : '');
    if (!base) { msg.textContent = t('models.prov.required'); msg.classList.add('bad'); return; }
    body = { base_url: base };
    const keyEnv = ($('#dprovKeyEnv') ? $('#dprovKeyEnv').value.trim() : '');
    if (keyEnv) body.key_env = keyEnv;
    const key = ($('#dprovKey') ? $('#dprovKey').value.trim() : '');
    if (key) body.api_key = key;
  } else {
    const provider = $('#cmProvider') ? $('#cmProvider').value : '';
    if (!provider) { msg.textContent = t('models.custom.required'); msg.classList.add('bad'); return; }
    body = { provider };
  }
  btn.disabled = true;
  msg.classList.remove('bad', 'ok');
  msg.textContent = t('common.loading');
  try {
    const out = await api.post('/admin/fetch-models', body);
    modal.fetched = (out.models || []).map(m => m.id).filter(Boolean);
    msg.textContent = tf('models.custom.fetched', { n: modal.fetched.length });
    msg.classList.add('ok');
    const list = $('#cmUpstreamOpts');
    if (list) {
      const all = [...new Set([...modal.fetched, ...catalogModelIds()])];
      const inp = $('#cmUpstream');
      if (inp) inp.removeAttribute('list');
      list.innerHTML = all.map(x => `<option value="${esc(x)}">`).join('');
      if (inp) inp.setAttribute('list', 'cmUpstreamOpts');
    }
  } catch (e) {
    msg.textContent = tf('models.custom.fetchFail', { err: errText(e) });
    msg.classList.add('bad');
  } finally {
    btn.disabled = false;
  }
}

/* ── boot ────────────────────────────────────────────────────────────── */
async function boot() {
  let m = { models: [] }, prov = { providers: [] }, set = { settings: {} };
  let jevBody = { config: null }, catalog = null;
  try {
    [m, prov, set, jevBody, catalog] = await Promise.all([
      api.get('/admin/models'),
      api.get('/admin/providers'),
      api.get('/admin/settings'),
      api.get('/admin/jev').catch(() => ({ config: null })),
      api.get('/admin/model-catalog').catch(() => null),
    ]);
  } catch (e) {
    toast((LANG === 'zh' ? '加载失败: ' : 'load failed: ') + errText(e), 4000);
  }
  STATE.models = m.models || [];
  STATE.providers = prov.providers || [];
  STATE.settings = set.settings || {};
  STATE.jev = jevBody.config || {};
  STATE.catalog = catalog && Array.isArray(catalog.providers) ? catalog : null;
  STATE.formats = catalog && Array.isArray(catalog.formats) ? catalog.formats : [];
  renderEngine(STATE.jev);
  renderModels();
  renderProviders();
  renderSettings(STATE.settings);
}

/* ── wiring (delegated; rendered children are replaced often) ────────── */
$('#addModel').addEventListener('click', () => openModelModal('add'));

$('#modelTabs').addEventListener('click', e => {
  const b = e.target.closest('[data-tab]');
  if (!b || !modal) return;
  if (b.dataset.tab === modal.tab) return;
  if (modal.tab === 'custom') captureCustom();
  modal.tab = b.dataset.tab;
  renderModelModal();
});

$('#modelModalBody').addEventListener('change', e => {
  if (!modal) return;
  const el = e.target;
  if (el.id === 'pmProvider') {
    modal.catalogProvider = el.value;
    modal.slots = {};
    modal.checked = new Set();
    modal.keyValue = ($('#pmKey') || {}).value || '';
    renderModelModal();
    return;
  }
  if (el.id === 'rmMode') { syncReasonEditor(); return; }
  if (el.id === 'rmFormat') { syncReasonEditor(); return; }
  if (el.id === 'rmDefault') { rmState().def = el.value; return; }
  if (el.id === 'rmSre') { rmState().sre = el.checked; return; }
  if (el.classList.contains('pm-check')) {
    const mid = el.dataset.mid;
    if (el.checked) {
      modal.checked.add(mid);
      ensurePresetSlots();
      const slotInput = $(`#pmModels .pm-id[data-mid="${CSS.escape(mid)}"]`);
      if (slotInput && modal.slots[mid] != null) slotInput.value = modal.slots[mid];
    } else {
      modal.checked.delete(mid);
    }
    refreshPresetSlots();
    return;
  }
  if (el.classList.contains('rl-check') || el.matches('[data-rl]')) {
    const id = el.dataset.rl;
    if (el.checked) rmState().checked.add(id); else rmState().checked.delete(id);
    syncReasonEditor();
    return;
  }
  if (el.id === 'cmProvider') {
    customDraft().provider = el.value;
    const fmt = $('#rmFormat');
    if (fmt) {
      const follow = providerFormat(el.value);
      const followOpt = fmt.querySelector('option[value=""]');
      if (followOpt) followOpt.textContent = tf('models.reason.formatFollow', { format: follow });
    }
    return;
  }
});
$('#modelModalBody').addEventListener('input', e => {
  if (!modal) return;
  const el = e.target;
  if (el.id === 'pmKey') { modal.keyValue = el.value; return; }
  if (el.classList.contains('pm-id')) {
    const mid = el.dataset.mid;
    modal.slots[mid] = el.value;
    refreshPresetSlots();
    return;
  }
  if (el.dataset.wire) { rmState().wires[el.dataset.wire] = el.value; return; }
  // Custom tab: write every keystroke back into the draft, so a tab or
  // language switch can rebuild the DOM without losing half-typed input.
  if (modal.tab === 'custom') captureCustom();
});
$('#modelModalBody').addEventListener('click', e => {
  const tgt = e.target;
  if (tgt.closest('#fetchModels')) { fetchAvailableModels(); return; }
  if (tgt.closest('#cmNewProvider')) {
    captureCustom();
    const d = customDraft();
    d.provOpen = true;
    renderModelModal();
    return;
  }
});
$('#modelModalSave').addEventListener('click', () => {
  if (!modal) return;
  if (modal.mode === 'add' && modal.tab === 'preset') savePreset();
  else saveCustom();
});

$('#addProvider').addEventListener('click', () => openProvModal(null));
$('#provModalBody').addEventListener('change', e => {
  const el = e.target;
  if (!provModal) return;
  if (el.id === 'pvPreset') {
    captureProvModal();
    const cp = catalogProviders().find(x => x.id === el.value);
    provModal.draft = cp ? {
      name: cp.id, base_url: cp.base_url || '', key_env: cp.key_env || '',
      reasoning_format: cp.reasoning_format || 'openai',
    } : { name: '', base_url: '', key_env: '', reasoning_format: 'openai' };
    renderProvModal();
    return;
  }
  if (el.id === 'pvFormat') {
    captureProvModal();
    renderProvModal();
    return;
  }
  if (el.id === 'pvClearKey') { captureProvModal(); return; }
});
$('#provModalBody').addEventListener('input', e => {
  if (!provModal) return;
  // Keep the provider draft in sync with every keystroke: the language switch
  // rebuilds this modal, so unsaved input must live in the state, not the DOM.
  captureProvModal();
});
$('#provModalBody').addEventListener('click', e => {
  if (e.target.closest('#pvDelete')) deleteProvInModal();
});
$('#provModalSave').addEventListener('click', saveProviderModal);

/* registry / provider card actions */
$('#modelTable').addEventListener('click', e => {
  const edit = e.target.closest('[data-editmodel]');
  if (edit) { openModelModal('edit', edit.dataset.editmodel); return; }
  const del = e.target.closest('[data-delmodel]');
  if (del) { if (armDelete(del)) deleteModel(del.dataset.delmodel); return; }
});
$('#providers').addEventListener('click', e => {
  const edit = e.target.closest('[data-editprov]');
  if (edit) { openProvModal(edit.dataset.editprov); return; }
  const del = e.target.closest('[data-delprov]');
  if (del) { if (armDelete(del)) deleteProvider(del.dataset.delprov); return; }
});

/* modal chrome: backdrop, ✕ / 取消, Esc */
$$('.modal').forEach(m => {
  m.addEventListener('click', ev => {
    if (ev.target === m) closeModal(m.id);
  });
});
document.addEventListener('click', e => {
  const c = e.target.closest('[data-close]');
  if (c) closeModal(c.dataset.close);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && closeTopModal()) e.preventDefault();
});

/* ── runtime settings ────────────────────────────────────────────────── */
$('#addSetting').addEventListener('click', () => {
  const wrap = document.createElement('div');
  wrap.className = 'set-row';
  wrap.innerHTML = `<input class="s-key" placeholder="key" style="max-width:190px">
    <input class="s-val" placeholder="value">
    <button class="del" data-del-setting>✕</button>`;
  wrap.querySelector('[data-del-setting]').addEventListener('click', () => wrap.remove());
  $('#settingsRows').prepend(wrap);
});
$('#saveSettings').addEventListener('click', async () => {
  try {
    await api.put('/admin/settings', readSettings());
    const body = await api.get('/admin/settings');
    STATE.settings = body.settings || {};
    renderSettings(STATE.settings);
    toast(t('common.saved'));
  } catch (e) {
    toast((LANG === 'zh' ? '保存失败: ' : 'save failed: ') + errText(e));
  }
});
window.onLangChange = () => {
  // Capture the open modals BEFORE re-rendering: the language switch rebuilds
  // their DOM, so unsaved input would be lost (the capture/restore patches
  // keep the draft, not the DOM).
  if (modal && modal.tab === 'custom') captureCustom();
  if (provModal) captureProvModal();
  renderEngine(STATE.jev);
  renderModels();
  renderProviders();
  renderSettings(STATE.settings);
  if (modal) renderModelModal();
  if (provModal) renderProvModal();
};
boot();
