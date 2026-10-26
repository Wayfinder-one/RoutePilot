/* 能力路由 page (split out of 模型与设置 on 2026-10-26).

   Natural-language capability routing has two halves:
     - per-model capability description, stored as models.profile.capability,
       edited in the model modal on models.html and shown in its registry list;
     - global policy (instructions / min_prob / min_margin / fallback_model),
       stored as settings.capability_* and edited HERE.
   This page owns the policy plus the candidate / missing-description summary
   (GET/PUT /admin/capability). A backend older than the capability release
   answers 404: show a notice instead of an empty, unsaveable form. */
const $ = s => document.querySelector(s);

const STATE = { cap: null, unavailable: '' };

/* ── render ──────────────────────────────────────────────────────────── */
function renderCapability(cap) {
  cap = cap || {};
  STATE.cap = cap;
  STATE.unavailable = '';
  const engine = cap.enabled_engine || '';
  const badge = $('#capabilityBadge');
  if (badge) {
    const n = (cap.candidates || []).length;
    badge.className = 'pill ' + (cap.configured ? 'ok' : 'warn');
    badge.textContent = cap.configured
      ? (LANG === 'zh' ? n + ' 个候选' : n + ' candidate' + (n === 1 ? '' : 's'))
      : t('cap.badge.none');
  }
  const pill = $('#capEngine');
  if (pill) {
    pill.classList.remove('hidden');
    pill.innerHTML = `<span class="dot" style="color:var(--indigo)"></span>` +
      (LANG === 'zh' ? '引擎 <b>' + esc(engine || '?') + '</b>'
                     : 'engine <b>' + esc(engine || '?') + '</b>');
  }
  const notice = $('#capNotice');
  if (notice) {
    notice.innerHTML = engine && engine !== 'jev'
      ? `<div class="warnbox">${esc(tf('cap.engineHint', { engine }))}</div>` : '';
  }
  const meta = $('#capMeta');
  if (meta) meta.textContent = cap.revision
    ? 'revision ' + String(cap.revision).slice(0, 12) : '';
  renderPolicy(cap);
  renderCandidates(cap);
}

function renderPolicy(cap) {
  const wrap = $('#capPolicy');
  if (!wrap) return;
  const models = cap.models || [];
  const fallback = cap.fallback_model || '';
  const fallbackOpts = `<option value="">${esc(t('common.none'))}</option>` +
    models.map(m => `<option value="${esc(m.id)}"${m.id === fallback ? ' selected' : ''}>` +
      `${esc(m.id)}${m.upstream && m.upstream !== m.id ? ' · ' + esc(m.upstream) : ''}</option>`).join('');
  wrap.innerHTML = `
    <label class="field"><span>${esc(t('cap.instructions'))}</span>
      <textarea id="capInstructions" rows="3">${esc(cap.instructions || '')}</textarea></label>
    <div class="cap-policy-grid">
      <label class="field"><span>${esc(t('cap.fallback'))}</span>
        <select id="capFallback">${fallbackOpts}</select>
        <small class="mut">${esc(t('cap.fallbackHint'))}</small></label>
      <label class="field"><span>${esc(t('cap.minprob'))}</span>
        <input id="capMinProb" type="number" step="0.01" min="0" max="1"
               value="${cap.min_prob != null ? esc(cap.min_prob) : ''}"></label>
      <label class="field"><span>${esc(t('cap.minmargin'))}</span>
        <input id="capMinMargin" type="number" step="0.01" min="0" max="1"
               value="${cap.min_margin != null ? esc(cap.min_margin) : ''}"></label>
    </div>
    <div class="addrow" style="margin-top:10px">
      <button class="btn pri sm" id="capSave">${esc(t('cap.save'))}</button>
      <span class="mut" id="capSaveMsg"></span>
    </div>`;
}

function renderCandidates(cap) {
  const wrap = $('#capCandidates');
  if (!wrap) return;
  const candidates = cap.candidates || [];
  const missing = (cap.missing || []).map(m => m.id);
  const count = $('#capCandidateCount');
  if (count) count.textContent = candidates.length ? `· ${candidates.length}` : '';
  const rows = candidates.map(c => `<tr>
      <td class="mono" style="font-size:11.5px">${esc(c.id)}</td>
      <td class="mono mut" style="font-size:11px">${esc(c.upstream || '—')}</td>
      <td class="cap-desc">${esc(c.capability)}</td>
    </tr>`).join('');
  const table = candidates.length ? `
    <table class="cap-table">
      <thead><tr>
        <th>${esc(t('models.col.model'))}</th>
        <th>${esc(t('models.col.upstream'))}</th>
        <th>${esc(t('models.col.capability'))}</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>` : `<div class="empty">${esc(t('cap.none'))}</div>`;
  const missingHtml = missing.length
    ? `<div class="footnote">${esc(t('cap.missing'))}:
         <span class="mono">${missing.map(esc).join(', ')}</span></div>` : '';
  wrap.innerHTML = table +
    `<div class="footnote">${esc(t('cap.editHint'))}</div>` + missingHtml;
}

/* ── save ────────────────────────────────────────────────────────────── */
async function saveCapability() {
  const num = v => (v == null || v === '' ? null : Number(v));
  const val = id => { const el = $('#' + id); return el ? el.value : ''; };
  const payload = {
    instructions: val('capInstructions'),
    min_prob: num(val('capMinProb')),
    min_margin: num(val('capMinMargin')),
    fallback_model: val('capFallback'),
  };
  const btn = $('#capSave');
  const msg = $('#capSaveMsg');
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = t('common.loading');
  try {
    const out = await api.put('/admin/capability', payload);
    renderCapability(out);
    toast(t('cap.saved'));
  } catch (e) {
    if (msg) msg.textContent = apiErrText(e);
    toast(apiErrText(e), 4000);
  } finally {
    const again = $('#capSave');
    if (again) again.disabled = false;
  }
}

/* Language toggle rebuilds this page's DOM: keep unsaved policy edits. */
function captureDraft() {
  const cap = { ...(STATE.cap || {}) };
  const num = v => (v == null || v === '' ? null : Number(v));
  const value = id => { const el = $('#' + id); return el ? el.value : undefined; };
  if (value('capInstructions') !== undefined) cap.instructions = value('capInstructions');
  if (value('capFallback') !== undefined) cap.fallback_model = value('capFallback');
  if (value('capMinProb') !== undefined) cap.min_prob = num(value('capMinProb'));
  if (value('capMinMargin') !== undefined) cap.min_margin = num(value('capMinMargin'));
  return cap;
}
/* Entry point when the backend cannot answer GET /admin/capability at all:
   404 = the code predates capability routing, anything else = unreachable /
   failing backend. Either way the page must stay honest: no half-filled,
   unsaveable policy form, and a persistent notice that survives language
   switches (STATE.unavailable is re-rendered by onLangChange). */
function renderUnavailable(key) {
  STATE.unavailable = key;
  const badge = $('#capabilityBadge');
  if (badge) {
    badge.className = 'pill bad';
    badge.textContent = t('cap.unavailable');
  }
  ['#capEngine', '#capMeta', '#capCandidateCount'].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.textContent = '';
    el.classList.add('hidden');
  });
  const empty = `<div class="empty">${esc(t(key))}</div>`;
  const policy = $('#capPolicy');
  if (policy) policy.innerHTML = empty;
  const cands = $('#capCandidates');
  if (cands) cands.innerHTML = empty;
}

window.onLangChange = () => {
  if (STATE.unavailable) { renderUnavailable(STATE.unavailable); return; }
  renderCapability(captureDraft());
};

/* ── boot ────────────────────────────────────────────────────────────── */
async function bootCapability() {
  try {
    renderCapability(await api.get('/admin/capability'));
  } catch (e) {
    if (e && e.status === 404) { renderUnavailable('cap.unsupported'); return; }
    renderUnavailable('cap.unreachable');
    toast((LANG === 'zh' ? '加载失败: ' : 'load failed: ') + apiErrText(e), 4000);
  }
}

$('#capPolicy').addEventListener('click', e => {
  if (e.target.closest('#capSave')) saveCapability();
});

bootCapability();
