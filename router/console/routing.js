/* Smart Router console — routing config (direction D1, 2026-10-29).

   One page = one JEV routing tree. Only two things are edited:
     1. root.instructions        — "how to choose"
     2. option.target.capability — "what each model is good at"
   A candidate with an empty target.capability inherits the model catalog
   capability (single-model target only), exactly like the engine does.
   There are no time periods, branches, weights or nested trees here; a
   tree whose root is not a JEV node is shown read-only with a banner.

   API used (unchanged): GET /admin/trees, GET /admin/models, GET /admin/jev,
   POST/PUT /admin/trees, POST /admin/trees/{id}/enable|disable|copy,
   POST /admin/try. Saving never ships `enabled` (that is its own endpoint). */
(function () {
  'use strict';

  /* ── tiny helpers ─────────────────────────────────────────────── */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const clone = value => JSON.parse(JSON.stringify(value));
  const lang = () => (typeof LANG === 'string' ? LANG : 'zh');
  const pad2 = n => (n < 10 ? '0' : '') + n;
  const ic = (name, cls) => `<svg class="rt2-ic${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const snippet = (text, n) => {
    const s = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : (s || '—');
  };
  const unit = v => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0;
  };
  const ttlOf = v => {
    const n = Math.round(Number(v));
    return Number.isFinite(n) && n >= 1 ? Math.min(31536000, n) : 1800;
  };
  /* Write a number into an input without fighting the user while typing. */
  const setNumber = (input, value) => {
    if (!input || document.activeElement === input) return;
    if (input.value === '' || Number(input.value) !== value) input.value = String(value);
  };

  /* ── state ────────────────────────────────────────────────────── */
  const S = {
    trees: [],
    models: [],
    modelMap: {},
    jev: null,
    tree: null,
    base: null,
    isNew: false,
    rev: '',
    editing: null,
    draftLabel: '',
    draftCap: '',
    issues: [],
    tab: 'preview',
    tryText: '',
    tryState: 'idle',
    tryResult: null,
    rawOpen: false,
    popOpen: false,
    dragKey: null,
    booted: false,
  };

  /* ── model / option helpers ───────────────────────────────────── */
  function modelOf(id) { return S.modelMap[id] || null; }
  function catalogCapability(id) {
    const model = modelOf(id);
    return model && typeof model.capability === 'string' ? model.capability.trim() : '';
  }
  function rootJev() {
    const root = S.tree && S.tree.root;
    return root && root.type === 'jev' ? root : null;
  }
  function isBranch() {
    return !!(S.tree && S.tree.root && S.tree.root.type !== 'jev');
  }
  function options() {
    const root = rootJev();
    return root && Array.isArray(root.options) ? root.options : [];
  }
  function optionByKey(key) {
    return options().find(option => option.key === key) || null;
  }
  function modelIds(option) {
    const list = option && option.target && option.target.models;
    return Array.isArray(list) ? list.map(item => item.model).filter(Boolean) : [];
  }
  function effectiveCapability(option) {
    const own = String((option && option.target && option.target.capability) || '').trim();
    if (own) return { text: own, inherited: false };
    const ids = modelIds(option);
    const inherited = ids.length === 1 ? catalogCapability(ids[0]) : '';
    return { text: inherited, inherited: !!inherited };
  }
  function previewCapability(option) {
    if (S.editing && option.key === S.editing) {
      const draft = String(S.draftCap || '').trim();
      if (draft) return draft;
    }
    return effectiveCapability(option).text;
  }
  function previewLabel(option) {
    const draft = S.editing && option.key === S.editing ? String(S.draftLabel || '').trim() : '';
    return draft || option.label || modelIds(option)[0] || '—';
  }
  function fallbackModel() {
    const root = rootJev();
    const fb = root && root.fallback;
    return fb && Array.isArray(fb.models) && fb.models[0] ? fb.models[0].model : '';
  }
  function fallbackTarget() {
    const root = rootJev();
    if (!root) return null;
    if (!root.fallback) {
      root.fallback = { id: 't-fallback', type: 'target', strategy: 'least_inflight', capability: '', models: [] };
    }
    return root.fallback;
  }
  function jevModelName() {
    return (S.jev && S.jev.config && S.jev.config.model) || '';
  }
  function stateTemplate() {
    return (S.jev && S.jev.config && S.jev.config.state_template) ||
      (S.jev && S.jev.defaults && S.jev.defaults.state_template) || '';
  }
  function defaultInstructions() {
    return (S.jev && S.jev.defaults && S.jev.defaults.instructions) || '';
  }
  function nextTargetId() {
    const ids = new Set();
    const root = rootJev();
    if (root) {
      if (root.id) ids.add(root.id);
      (root.options || []).forEach(option => {
        if (option.target && option.target.id) ids.add(option.target.id);
      });
      if (root.fallback && root.fallback.id) ids.add(root.fallback.id);
    }
    let n = 1;
    while (ids.has('t-opt-' + n)) n += 1;
    return 't-opt-' + n;
  }

  /* ── dirty tracking (screen values vs last saved tree) ────────── */
  function projection(tree) {
    const root = tree && tree.root;
    const jev = root && root.type === 'jev' ? root : null;
    if (!jev) return { branch: (root && root.type) || '', name: (tree && tree.name) || '' };
    const sticky = (tree && tree.sticky) || {};
    return {
      name: (tree && tree.name) || '',
      timezone: (tree && tree.timezone) || 'Asia/Shanghai',
      sticky: { enabled: !!sticky.enabled, ttl: Number(sticky.ttl_seconds) || 1800 },
      instructions: jev.instructions || '',
      min_prob: Number(jev.min_prob) || 0,
      min_margin: Number(jev.min_margin) || 0,
      options: (jev.options || []).map(option => ({
        key: option.key || '',
        label: option.label || '',
        cap: String((option.target && option.target.capability) || ''),
        models: (option.target && option.target.models || [])
          .map(item => item.model + ':' + (item.reasoning == null ? '' : item.reasoning)),
      })),
      fallback: (jev.fallback && jev.fallback.models || []).map(item => item.model).join(','),
    };
  }

  function dirtyItems() {
    if (!S.tree) return [];
    if (S.isNew) return [t('routing.tree')];
    if (isBranch()) return [];
    const base = projection(S.base || {});
    const now = projection(S.tree);
    const out = [];
    if (base.instructions !== now.instructions) out.push(t('routing.instructions'));
    if (base.name !== now.name) out.push(t('routing.treeName'));
    if (base.timezone !== now.timezone) out.push(t('routing.timezone'));
    if (base.sticky.enabled !== now.sticky.enabled || base.sticky.ttl !== now.sticky.ttl) out.push(t('routing.sticky'));
    if (base.min_prob !== now.min_prob) out.push(t('routing.minProb'));
    if (base.min_margin !== now.min_margin) out.push(t('routing.minMargin'));
    if (base.fallback !== now.fallback) out.push(t('routing.fallback'));

    const baseMap = new Map(base.options.map(option => [option.key, option]));
    const baseKeys = base.options.map(option => option.key);
    const nowKeys = now.options.map(option => option.key);
    const structural = baseKeys.length !== nowKeys.length ||
      baseKeys.some((key, index) => key !== nowKeys[index]) ||
      nowKeys.some(key => !baseMap.has(key));
    if (structural) {
      out.push(t('routing.candidates'));
    } else {
      now.options.forEach(option => {
        const prev = baseMap.get(option.key);
        if (prev.label !== option.label || prev.cap !== option.cap || prev.models.join('|') !== option.models.join('|')) {
          out.push(option.label || option.key);
        }
      });
    }

    if (S.editing) {
      const option = optionByKey(S.editing);
      if (option) {
        const labelChanged = String(S.draftLabel || '').trim() &&
          String(S.draftLabel || '').trim() !== (option.label || '');
        const runCap = String((option.target && option.target.capability) || '');
        const eff = effectiveCapability(option).text;
        const capChanged = S.draftCap !== runCap && S.draftCap !== eff;
        if ((labelChanged || capChanged) && out.indexOf(option.label) < 0) {
          out.push(option.label || t('routing.capability'));
        }
      }
    }
    return out;
  }

  /* ── page head ────────────────────────────────────────────────── */
  function renderHeader() {
    const model = jevModelName();
    $('#jevModel').textContent = model || t('routing.jevUnconfigured');
    $('#jevPill').classList.toggle('rt2-warn', !model);
    $('#jevPill').title = (S.jev && S.jev.config && S.jev.config.base_url) || '';

    $('#revCode').textContent = (S.rev || '').slice(0, 8) || '—';
    $('#revPill').title = S.rev || '';

    const enabled = !!(S.tree && S.tree.enabled);
    const statePill = $('#statePill');
    statePill.className = 'rt2-pill ' + (enabled ? 'rt2-on' : 'rt2-off');
    statePill.innerHTML = `<span class="rt2-ldot"></span>${esc(enabled ? t('routing.enabled') : t('routing.disabled'))}`;

    const dirty = dirtyItems();
    const savePill = $('#savePill');
    if (dirty.length) {
      savePill.className = 'rt2-pill rt2-dirty';
      savePill.title = dirty.join(' · ');
      savePill.innerHTML = `<span class="rt2-ldot"></span>${esc(tf('routing.unsaved', { n: dirty.length }))}`;
    } else {
      savePill.className = 'rt2-pill rt2-saved';
      savePill.title = t('routing.saved');
      savePill.innerHTML = ic('check') + esc(t('routing.saved'));
    }

    const enableBtn = $('#enableTree');
    $('#enableLabel').textContent = enabled ? t('routing.disable') : t('routing.enable');
    enableBtn.classList.toggle('on', enabled);
    enableBtn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    enableBtn.disabled = !S.tree || S.isNew || isBranch();
    $('#saveTree').disabled = !S.tree || isBranch();
    $('#headerTry').disabled = !S.tree || isBranch();
    $('#copyTree').disabled = !S.tree || S.isNew;
    $('#restoreDefault').disabled = isBranch();
  }

  /* ── issues ───────────────────────────────────────────────────── */
  function issueText(issue) {
    const first = lang() === 'en' ? issue.en : issue.zh;
    const second = lang() === 'en' ? issue.zh : issue.en;
    return String(first || second || issue.msg || issue.code || '');
  }

  function issueTarget(issue) {
    const root = rootJev();
    if (!root || !issue || !issue.node_id) return null;
    const nodeId = issue.node_id;
    const code = String(issue.code || '');
    if (nodeId === root.id) {
      if (/option|duplicate/.test(code)) return $('#candidateCard');
      if (/fallback/.test(code)) return $('#fallbackCard');
      return $('#promptCard');
    }
    if (root.fallback && root.fallback.id === nodeId) return $('#fallbackCard');
    const rows = $$('#candidateList .rt2-mrow');
    for (let i = 0; i < rows.length; i += 1) {
      const option = optionByKey(rows[i].dataset.key);
      if (option && option.target && option.target.id === nodeId) return rows[i];
    }
    return null;
  }

  function applyFlags() {
    $$('#promptCard, #candidateCard, #fallbackCard, #candidateList .rt2-mrow')
      .forEach(el => el.classList.remove('rt2-flagged'));
    const flagged = new Set();
    S.issues.forEach(issue => {
      const el = issueTarget(issue);
      if (el && !flagged.has(el)) {
        flagged.add(el);
        el.classList.add('rt2-flagged');
      }
    });
  }

  function renderIssues() {
    const box = $('#issues');
    if (!S.issues.length) {
      box.hidden = true;
      box.innerHTML = '';
      applyFlags();
      return;
    }
    box.hidden = false;
    box.innerHTML = S.issues.map((issue, index) => {
      const locatable = !!issueTarget(issue);
      const tag = locatable ? 'button' : 'div';
      const attrs = locatable ? ' type="button"' : '';
      const cls = 'rt2-issue' + (locatable ? ' rt2-locate' : '');
      return `<${tag} class="${cls}" data-issue="${index}"${attrs}>${ic('alert')}<b>${esc(issueText(issue))}</b></${tag}>`;
    }).join('') + `<button class="rt2-issues-x" type="button" data-close="issues" aria-label="${esc(t('common.close'))}">×</button>`;
    applyFlags();
  }

  function setIssues(list) {
    S.issues = Array.isArray(list) ? list : [];
    renderIssues();
  }

  function apiErrorIssues(error, prefixKey) {
    if (error && error.status === 422 && Array.isArray(error.detail)) return error.detail;
    const text = prefixKey ? tf(prefixKey, { err: apiErrText(error) }) : apiErrText(error);
    return [{ code: 'api', zh: text, en: text }];
  }

  function reportError(error, prefixKey) {
    const list = apiErrorIssues(error, prefixKey);
    setIssues(list);
    toast(issueText(list[0]));
  }

  /* ── branch banner / section visibility ───────────────────────── */
  function renderBanner() {
    const branch = isBranch();
    $('#branchBanner').hidden = !branch;
    ['#promptCard', '#candidateCard', '#fallbackCard'].forEach(selector => {
      $(selector).hidden = branch;
    });
  }

  /* ── prompt ───────────────────────────────────────────────────── */
  function renderPrompt() {
    const root = rootJev();
    const value = root ? root.instructions || '' : '';
    const textarea = $('#instructions');
    if (textarea.value !== value) textarea.value = value;
    textarea.placeholder = t('routing.instructionsPlaceholder');
    textarea.setAttribute('aria-label', t('routing.instructions'));
    textarea.disabled = isBranch();
    $('#charCount').innerHTML = tf('routing.chars', { n: `<b>${value.length}</b>` });
  }

  /* ── candidate rows ───────────────────────────────────────────── */
  function rowHtml(option, index) {
    const editing = S.editing === option.key;
    const eff = effectiveCapability(option);
    const ids = modelIds(option);
    const filled = !!eff.text;
    const label = option.label || ids[0] || '—';
    const modelCell = ids.length === 0
      ? '<span class="rt2-mmid">—</span>'
      : ids.length === 1
        ? `<code class="rt2-mid">${esc(ids[0])}</code>`
        : `<span class="rt2-mmid">${esc(tf('routing.multiModels', { n: ids.length }))}</span>`;

    const tags = [];
    if (eff.inherited) tags.push(`<span class="rt2-tag rt2-inherit">${esc(t('routing.inherit'))}</span>`);
    if (!filled) tags.push(`<span class="rt2-tag rt2-warn">${esc(t('routing.unconfigured'))}</span>`);
    if (ids.length === 0 || (ids.length === 1 && !modelOf(ids[0]))) {
      tags.push(`<span class="rt2-tag rt2-warn">${esc(t('routing.unknownModel'))}</span>`);
    }

    const capHtml = editing ? '' : `<p class="rt2-cap${filled ? '' : ' rt2-blank'}" title="${esc(eff.text || '')}">${esc(filled ? eff.text : t('routing.capabilityHint'))}</p>`;

    const editor = editing ? `<div class="rt2-edit">
        <label class="rt2-editf">
          <span class="rt2-editlbl">${esc(t('routing.candidateName'))}</span>
          <input class="rt2-editname" data-edit-name value="${esc(S.draftLabel)}" title="${esc(t('routing.candidateNameHint'))}" autocomplete="off">
        </label>
        <label class="rt2-editf">
          <span class="rt2-editlbl">${esc(t('routing.capability'))}</span>
          <textarea class="rt2-editta" data-edit-cap spellcheck="false" title="${esc(t('routing.capabilityHint'))}"
            placeholder="${esc(t('routing.capabilityHint'))}">${esc(S.draftCap)}</textarea>
        </label>
        <div class="rt2-editfoot">
          <span class="rt2-count">${tf('routing.chars', { n: `<b data-editcount>${S.draftCap.length}</b>` })}</span>
          <span class="rt2-editnote">→ criteria["${esc(S.draftLabel || label)}"]</span>
          <span class="rt2-editacts">
            <button class="btn sm ghost" type="button" data-act="cancel">${esc(t('routing.cancel'))}</button>
            <button class="btn sm pri" type="button" data-act="commit">${esc(t('routing.saveCapability'))}</button>
          </span>
        </div>
      </div>` : '';

    return `<div class="rt2-mrow${editing ? ' editing' : ''}" data-key="${esc(option.key)}" tabindex="${editing ? '-1' : '0'}" aria-expanded="${editing}"${editing ? ` aria-label="${esc(t('routing.editTitle'))}"` : ''}>
      <button class="rt2-grip" type="button" data-grip tabindex="-1" aria-label="${esc(t('routing.drag'))}" title="${esc(t('routing.drag'))}">${ic('grip')}</button>
      <span class="rt2-idx">${pad2(index + 1)}</span>
      <div class="rt2-mbody">
        <div class="rt2-mhead">
          <span class="rt2-mdot${filled ? '' : ' rt2-blank'}" title="${esc(filled ? t('routing.configured') : t('routing.unconfigured'))}"></span>
          <span class="rt2-mname">${esc(label)}</span>
          ${modelCell}${tags.join('')}
        </div>
        ${capHtml}${editor}
      </div>
      <div class="rt2-ops">
        <button class="rt2-op" type="button" data-act="edit">${esc(t('routing.editCapability'))}</button>
        <button class="rt2-op rt2-del" type="button" data-act="del">${esc(t('routing.delete'))}</button>
      </div>
    </div>`;
  }

  function renderCandidates() {
    const box = $('#candidateList');
    const list = options();
    let html = '';
    if (!list.length) {
      html = `<div class="rt2-empty">
        <b>${esc(t('routing.empty'))}</b>
        <span>${esc(t('routing.emptyHint'))}</span>
        <button class="btn sm" type="button" data-act="add-empty">${esc(t('routing.addModel'))}</button>
      </div>`;
    } else {
      html = list.map(rowHtml).join('');
    }
    box.innerHTML = html;
    $('#candidateCount').textContent = String(list.length);
    applyFlags();
  }

  /* ── fallback ─────────────────────────────────────────────────── */
  function renderFallback() {
    const select = $('#fallbackSelect');
    const root = rootJev();
    if (!root) {
      select.innerHTML = '<option value="">—</option>';
      select.disabled = true;
      return;
    }
    select.disabled = isBranch();
    select.setAttribute('aria-label', t('routing.fallback'));
    const ids = [];
    options().forEach(option => {
      const id = modelIds(option)[0];
      if (id && ids.indexOf(id) < 0) ids.push(id);
    });
    const current = fallbackModel();
    if (current && ids.indexOf(current) < 0) ids.push(current);
    const emptyOption = current ? '' : '<option value="">—</option>';
    select.innerHTML = emptyOption + (ids.map(id => {
      const option = options().find(item => modelIds(item)[0] === id);
      const label = option && option.label && option.label !== id ? option.label + ' (' + id + ')' : id;
      return `<option value="${esc(id)}"${id === current ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('') || '<option value="">—</option>');
    select.value = current || '';
  }

  /* ── advanced ─────────────────────────────────────────────────── */
  function renderAdvanced() {
    const current = S.isNew ? '' : (S.tree && S.tree.id) || '';
    const select = $('#treeSelect');
    let items = S.trees.map(tree =>
      `<option value="${esc(tree.id)}">${esc(tree.name || tree.id)}${tree.enabled ? ' ●' : ''}</option>`);
    if (S.isNew) items.unshift(`<option value="">${esc(t('routing.newTree'))}</option>`);
    select.innerHTML = items.join('') || '<option value="">—</option>';
    select.value = current;
    if (select.value !== current) select.value = items.length ? select.options[0].value : '';

    const name = S.tree ? S.tree.name || '' : '';
    if ($('#treeName').value !== name) $('#treeName').value = name;
    const timezone = (S.tree && S.tree.timezone) || 'Asia/Shanghai';
    if ($('#timezone').value !== timezone) $('#timezone').value = timezone;

    const sticky = (S.tree && S.tree.sticky) || {};
    $('#stickyEnabled').checked = !!sticky.enabled;
    setNumber($('#stickyTtl'), ttlOf(sticky.ttl_seconds));

    const root = rootJev();
    setNumber($('#minProb'), root ? Number(root.min_prob) || 0 : 0);
    setNumber($('#minMargin'), root ? Number(root.min_margin) || 0 : 0);

    $('#treeName').disabled = !S.tree;
    $('#timezone').disabled = !S.tree;
    $('#stickyEnabled').disabled = !S.tree || isBranch();
    $('#stickyTtl').disabled = !S.tree || isBranch();
    $('#minProb').disabled = !S.tree || isBranch();
    $('#minMargin').disabled = !S.tree || isBranch();
    $('#exportTree').disabled = !S.tree;
  }

  /* ── request preview ──────────────────────────────────────────── */
  function renderState() {
    const template = String(stateTemplate() || '');
    $('#stateCode').innerHTML = esc(template).split('{request}').join('<em class="rt2-ph">{request}</em>');
  }

  function renderCriteria() {
    const list = options();
    if (!list.length) {
      $('#crits').innerHTML = `<div class="rt2-crow"><div class="rt2-cv rt2-blank">${esc(t('routing.empty'))}</div></div>`;
    } else {
      $('#crits').innerHTML = list.map(option => {
        const cap = previewCapability(option);
        const first = cap ? cap.split('\n')[0] : '';
        const ids = modelIds(option);
        return `<div class="rt2-crow">
          <div class="rt2-ck"><b>${esc(previewLabel(option))}</b><code>${esc(ids[0] || '')}</code>${cap ? '' : `<span class="rt2-cmiss">${esc(t('routing.unconfigured'))}</span>`}</div>
          <div class="rt2-cv${cap ? '' : ' rt2-blank'}" title="${esc(cap)}">${esc(cap ? first : t('routing.capabilityHint'))}</div>
        </div>`;
      }).join('');
    }
    $('#critNote').textContent = tf('routing.previewCriteriaHint', { n: list.length });
  }

  function renderPreview() {
    const root = rootJev();
    const instructions = root ? root.instructions || '' : '';
    const out = $('#instrOut');
    out.className = 'rt2-instr' + (instructions.trim() ? '' : ' rt2-mut');
    out.textContent = instructions.trim() ? instructions : '—';

    const model = jevModelName();
    $('#previewModel').textContent = model || t('routing.jevUnconfigured');
    $('#fixedModel').textContent = 'model=' + (model || '—');
    renderState();
    renderCriteria();
  }

  /* ── try pane ─────────────────────────────────────────────────── */
  const sample = n => t('routing.trySample' + n);

  function usageTokens(usage) {
    if (!usage || typeof usage !== 'object') return 0;
    if (usage.total_tokens != null) return Number(usage.total_tokens) || 0;
    const input = Number(usage.input_tokens != null ? usage.input_tokens : usage.prompt_tokens) || 0;
    const output = Number(usage.output_tokens != null ? usage.output_tokens : usage.completion_tokens) || 0;
    return input + output;
  }

  function emptyTryHtml() {
    return `<div class="rt2-outempty">${ic('play')}
      <div class="rt2-outempty-t">${esc(t('routing.tryRun'))}</div>
      <div class="rt2-outempty-s">${esc(t('routing.tryNoLog'))}</div>
    </div>`;
  }

  function resultHtml(result) {
    const outcome = result.outcome || {};
    const jev = result.jev || null;
    const fallback = outcome.selection_reason === 'fallback' || !!outcome.fallback_reason || !jev;
    const selectedKey = jev && jev.selected_key;
    const selected = selectedKey ? optionByKey(selectedKey) : null;
    const modelId = outcome.model || (selected ? modelIds(selected)[0] : '') || '';
    const byModel = fallback && modelId
      ? options().find(option => modelIds(option).indexOf(modelId) >= 0)
      : null;
    const title = selected
      ? selected.label || selectedKey || modelId
      : byModel ? byModel.label : modelId || '—';

    let html = `<div class="rt2-final${fallback ? ' warn' : ''}">
      <span class="rt2-finalic">${ic(fallback ? 'alert' : 'check')}</span>
      <div class="rt2-finalmain">
        <div class="rt2-final-m">${esc(title)}</div>
        <div class="rt2-final-v">${esc(modelId)}</div>
      </div>
      <span class="rt2-final-tag">${esc(t(fallback ? 'routing.tryFallback' : 'routing.tryHit'))}</span>
    </div>`;

    if (fallback) {
      html += `<div class="rt2-fbnote">${ic('alert')}<div><b>${esc(t('routing.tryFallbackReason'))}</b>
        <code>${esc(outcome.fallback_reason || outcome.selection_reason || '—')}</code></div></div>`;
    }

    if (jev && Array.isArray(jev.ranking) && jev.ranking.length) {
      html += `<div class="rt2-probhdr"><b>${esc(t('routing.tryProb'))}</b><span>${esc(t('routing.tryRanking'))}</span></div>
        <div class="rt2-probs">` + jev.ranking.map(key => {
        const option = optionByKey(key);
        const p = Number((jev.probabilities || {})[key]) || 0;
        const label = option ? option.label || key : key;
        return `<div class="rt2-prow${key === selectedKey ? ' sel' : ''}" title="${esc(option ? modelIds(option)[0] || '' : '')}">
          <span class="rt2-pn">${esc(label)}</span>
          <span class="rt2-bar"><i style="width:${Math.max(1.5, p * 100).toFixed(2)}%"></i></span>
          <span class="rt2-pv">${p.toFixed(4)}</span>
        </div>`;
      }).join('') + '</div>';
    }

    const tokens = jev ? usageTokens(jev.usage) : 0;
    html += `<div class="rt2-meta">`;
    if (jev) {
      html += `<span><em>p_top</em><b>${Number(jev.p_top).toFixed(2)}</b></span>
        <span><em>margin</em><b>${Number(jev.margin).toFixed(2)}</b></span>
        <span><b>${Math.round(Number(jev.duration_ms) || 0)} ms</b></span>`;
      if (tokens) html += `<span><b>${tokens} tok</b></span>`;
    }
    html += `<span class="rt2-metatag${fallback ? ' warn' : ''}">${esc(t('routing.trySticky'))} · ${esc(outcome.sticky_hit ? t('routing.tryHit') : t('routing.tryMiss'))}</span>
    </div>`;

    if (outcome.policy_path) {
      html += `<div class="rt2-policy">policy_path: ${esc(outcome.policy_path)}</div>`;
    }

    html += `<div class="rt2-outfoot">
      <span>rev ${esc(String(result.snapshot_revision || S.rev || '').slice(0, 8) || '—')} · dry_run</span>
      <button class="rt2-rawbtn" type="button" data-act="raw">${esc(t(S.rawOpen ? 'routing.rawHide' : 'routing.rawResponse'))}</button>
    </div>`;
    if (S.rawOpen) {
      html += `<div class="rt2-raw"><pre>${esc(JSON.stringify(result, null, 2))}</pre></div>`;
    }
    return html;
  }

  function renderTry() {
    const button = $('#tryRun');
    const out = $('#tryOut');
    if (S.tryState === 'running') {
      button.disabled = true;
      button.innerHTML = `<span class="rt2-spin"></span><span>${esc(t('routing.tryRunning'))}</span>`;
      out.innerHTML = `<div class="rt2-outload"><span class="spinner"></span>${esc(t('routing.tryRunning'))}</div>`;
    } else {
      button.disabled = !S.tree || isBranch();
      button.innerHTML = ic('play') + `<span>${esc(t('routing.tryRun'))}</span>`;
      out.innerHTML = S.tryState === 'done' && S.tryResult ? resultHtml(S.tryResult) : emptyTryHtml();
    }
    $('#tryDot').hidden = S.tryState !== 'done';
  }

  function renderTryInputs() {
    const textarea = $('#tryText');
    if (textarea.value !== S.tryText) textarea.value = S.tryText;
    textarea.placeholder = t('routing.tryPlaceholder');
    textarea.setAttribute('aria-label', t('routing.try'));
    $('#tryCount').innerHTML = tf('routing.chars', { n: `<b>${S.tryText.length}</b>` });
    $$('#tryChips .rt2-chip').forEach(chip => {
      const value = sample(chip.dataset.sample);
      chip.textContent = value;
      chip.classList.toggle('on', S.tryText.trim() === String(value).trim());
    });
  }

  /* ── full render ──────────────────────────────────────────────── */
  function renderAll() {
    renderHeader();
    renderBanner();
    renderPrompt();
    renderCandidates();
    renderFallback();
    renderAdvanced();
    renderPreview();
    renderTryInputs();
    renderTry();
    renderIssues();
  }

  /* ── tree selection / defaults ────────────────────────────────── */
  function applyTree(tree) {
    S.tree = clone(tree);
    S.base = clone(tree);
    S.isNew = false;
    S.editing = null;
    S.draftLabel = '';
    S.draftCap = '';
    S.tryState = 'idle';
    S.tryResult = null;
    S.rawOpen = false;
    renderAll();
  }

  function selectTree(id) {
    const tree = S.trees.find(item => item.id === id);
    if (!tree) return;
    setIssues([]);
    applyTree(tree);
  }

  function defaultTree() {
    const pool = S.models.filter(model => String(model.capability || '').trim());
    const picks = pool.slice(0, 4);
    const takenKeys = new Set();
    const takenLabels = new Set();
    const optionList = picks.map((model, index) => {
      let key = 'opt-' + (index + 1);
      let n = 2;
      while (takenKeys.has(key)) key = 'opt-' + (index + 1) + '-' + n++;
      takenKeys.add(key);
      let label = model.id;
      let ln = 2;
      while (takenLabels.has(label)) label = model.id + ' (' + ln++ + ')';
      takenLabels.add(label);
      return {
        key,
        label,
        target: {
          id: 't-opt-' + (index + 1),
          type: 'target',
          strategy: 'least_inflight',
          capability: '',
          models: [{ model: model.id, reasoning: null }],
        },
      };
    });
    const fallbackId = picks.length ? picks[0].id : (S.models[0] && S.models[0].id) || '';
    return {
      id: '',
      name: t('routing.title'),
      enabled: false,
      timezone: 'Asia/Shanghai',
      sticky: { enabled: true, ttl_seconds: 1800 },
      root: {
        id: 'root',
        type: 'jev',
        instructions: defaultInstructions() || t('routing.instructionsPlaceholder'),
        min_prob: 0,
        min_margin: 0,
        options: optionList,
        fallback: {
          id: 't-fallback',
          type: 'target',
          strategy: 'least_inflight',
          capability: '',
          models: fallbackId ? [{ model: fallbackId, reasoning: null }] : [],
        },
      },
    };
  }

  function startNewTree() {
    setIssues([]);
    S.tree = defaultTree();
    S.base = null;
    S.isNew = true;
    S.editing = null;
    S.draftLabel = '';
    S.draftCap = '';
    S.tryState = 'idle';
    S.tryResult = null;
    S.rawOpen = false;
    renderAll();
  }

  /* Banner action for a non-JEV tree: immediately create a simplified JEV
     tree (default prompt + up to 4 catalog models with a capability) and
     switch to it. The catalog capability is inherited, so target.capability
     stays empty and the new tree is enable-ready. */
  async function createSimplifiedTree() {
    const draft = defaultTree();
    try {
      const response = await api.post('/admin/trees', {
        name: draft.name,
        timezone: draft.timezone,
        sticky: draft.sticky,
        root: draft.root,
      });
      const listed = await api.get('/admin/trees');
      S.trees = listed.trees || S.trees;
      S.rev = listed.snapshot_revision || S.rev;
      setIssues([]);
      selectTree(response.tree.id);
      toast(t('routing.saveOk'));
    } catch (error) {
      reportError(error, 'routing.saveFail');
    }
  }

  function pickInitialTree() {
    return S.trees.find(tree => tree.enabled && tree.root && tree.root.type === 'jev') ||
      S.trees.find(tree => tree.root && tree.root.type === 'jev') ||
      S.trees[0] || null;
  }

  /* ── editing a capability ─────────────────────────────────────── */
  function openEditor(key) {
    const option = optionByKey(key);
    if (!option || isBranch()) return;
    const eff = effectiveCapability(option);
    S.editing = key;
    S.draftLabel = option.label || '';
    S.draftCap = String((option.target && option.target.capability) || eff.text || '');
    renderCandidates();
    renderPreview();
    renderHeader();
    // Focus synchronously: the previous row node is replaced by innerHTML, so
    // waiting a frame would drop keyboard input on <body>.
    const textarea = $('#candidateList .rt2-editta');
    if (textarea) {
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    }
  }

  function focusRow(key) {
    const row = $$('#candidateList .rt2-mrow').find(el => el.dataset.key === key);
    if (row) row.focus();
  }

  function cancelEditor() {
    if (!S.editing) return;
    const key = S.editing;
    S.editing = null;
    S.draftLabel = '';
    S.draftCap = '';
    renderCandidates();
    renderPreview();
    renderHeader();
    focusRow(key);
  }

  function commitEditor() {
    if (!S.editing) return false;
    const key = S.editing;
    const option = optionByKey(key);
    const draftLabel = String(S.draftLabel || '');
    const draftCap = String(S.draftCap || '');
    S.editing = null;
    S.draftLabel = '';
    S.draftCap = '';
    if (!option) {
      renderCandidates();
      renderPreview();
      renderHeader();
      return false;
    }

    const label = draftLabel.trim();
    if (label) option.label = label;
    const ids = modelIds(option);
    const wasInherited = !String((option.target && option.target.capability) || '').trim();
    const inherited = ids.length === 1 ? catalogCapability(ids[0]) : '';
    option.target = option.target || { id: nextTargetId(), type: 'target', strategy: 'least_inflight', capability: '', models: [] };
    // Keep "inherit from catalog" sticky: a row that had no own capability stays
    // inherited while the text equals the catalog text; an explicit description
    // stays explicit even when it happens to match the catalog.
    option.target.capability = wasInherited && inherited && draftCap.trim() === inherited ? '' : draftCap;

    renderCandidates();
    renderPreview();
    renderHeader();
    focusRow(key);
    return true;
  }

  function removeCandidate(key) {
    const root = rootJev();
    const option = optionByKey(key);
    if (!root || !option) return;
    root.options = (root.options || []).filter(item => item.key !== key);
    if (S.editing === key) {
      S.editing = null;
      S.draftLabel = '';
      S.draftCap = '';
    }
    setIssues([]);
    renderAll();
    if (options().length < 2) toast(t('routing.needTwo'));
  }

  /* ── validation + payload ─────────────────────────────────────── */
  function validateLocal() {
    const root = rootJev();
    const issues = [];
    if (!root) return issues;
    if (!String(root.instructions || '').trim()) {
      issues.push({ code: 'instructions', zh: t('routing.needInstructions'), en: t('routing.needInstructions'), node_id: root.id });
    }
    const list = root.options || [];
    if (list.length < 2) {
      issues.push({ code: 'option_count', zh: t('routing.needTwo'), en: t('routing.needTwo'), node_id: root.id });
    }
    list.forEach(option => {
      const ids = modelIds(option);
      if (!ids.length) {
        issues.push({
          code: 'target_models_required',
          zh: (option.label || '—') + ': ' + t('routing.unknownModel'),
          en: (option.label || '—') + ': ' + t('routing.unknownModel'),
          node_id: option.target && option.target.id,
        });
        return;
      }
      if (!effectiveCapability(option).text) {
        issues.push({
          code: 'capability_required',
          zh: (option.label || ids[0]) + ': ' + t('routing.needCapability'),
          en: (option.label || ids[0]) + ': ' + t('routing.needCapability'),
          node_id: option.target && option.target.id,
        });
      }
    });
    if (!fallbackModel()) {
      issues.push({ code: 'fallback_required', zh: t('routing.needFallback'), en: t('routing.needFallback'), node_id: root.id });
    }
    return issues;
  }

  function treePayload() {
    const tree = S.tree;
    const root = rootJev();
    const sticky = (tree && tree.sticky) || {};
    const fb = root && root.fallback;
    const fbId = fallbackModel();
    return {
      name: String((tree && tree.name) || '').trim() || t('routing.title'),
      timezone: String((tree && tree.timezone) || '').trim() || 'Asia/Shanghai',
      sticky: { enabled: !!sticky.enabled, ttl_seconds: ttlOf(sticky.ttl_seconds) },
      root: {
        id: (root && root.id) || 'root',
        type: 'jev',
        instructions: root ? root.instructions || '' : '',
        min_prob: unit(root && root.min_prob),
        min_margin: unit(root && root.min_margin),
        options: ((root && root.options) || []).map(option => ({
          key: option.key,
          label: option.label,
          target: {
            id: (option.target && option.target.id) || nextTargetId(),
            type: 'target',
            strategy: 'least_inflight',
            capability: (option.target && option.target.capability) || '',
            models: ((option.target && option.target.models) || []).map(item => ({
              model: item.model,
              reasoning: item.reasoning == null ? null : item.reasoning,
            })),
          },
        })),
        fallback: {
          id: (fb && fb.id) || 't-fallback',
          type: 'target',
          strategy: 'least_inflight',
          capability: '',
          models: fbId ? [{
            model: fbId,
            reasoning: fb && fb.models && fb.models[0] && fb.models[0].reasoning != null ? fb.models[0].reasoning : null,
          }] : [],
        },
      },
    };
  }

  /* ── save / enable ────────────────────────────────────────────── */
  async function saveTree(opts) {
    if (!S.tree || isBranch()) return false;
    if (S.editing) commitEditor();
    const local = validateLocal();
    if (local.length) {
      setIssues(local);
      toast(issueText(local[0]));
      return false;
    }
    setIssues([]);
    const button = $('#saveTree');
    button.disabled = true;
    try {
      const payload = treePayload();
      const response = S.isNew
        ? await api.post('/admin/trees', payload)
        : await api.put('/admin/trees/' + encodeURIComponent(S.tree.id), payload);
      const saved = response.tree;
      S.rev = response.snapshot_revision || S.rev;
      const listed = await api.get('/admin/trees');
      S.trees = listed.trees || S.trees;
      S.rev = listed.snapshot_revision || S.rev;
      const fresh = S.trees.find(tree => tree.id === saved.id);
      if (fresh) applyTree(fresh);
      else applyTree(saved);
      toast(opts && opts.auto ? t('routing.tryAutoSaved') : t('routing.saveOk'));
      return true;
    } catch (error) {
      reportError(error, 'routing.saveFail');
      return false;
    } finally {
      renderHeader();
    }
  }

  async function toggleEnable() {
    if (!S.tree || isBranch()) return;
    if (S.isNew || dirtyItems().length) {
      const ok = await saveTree();
      if (!ok) return;
    }
    const action = S.tree.enabled ? 'disable' : 'enable';
    try {
      const response = await api.post('/admin/trees/' + encodeURIComponent(S.tree.id) + '/' + action);
      S.trees = response.trees || S.trees;
      S.rev = response.snapshot_revision || S.rev;
      setIssues([]);
      if (S.trees.find(tree => tree.id === S.tree.id)) selectTree(S.tree.id);
      else renderAll();
      toast(action === 'enable' ? t('routing.enabled') : t('routing.disabled'));
    } catch (error) {
      reportError(error, 'routing.enableFail');
    }
  }

  /* ── try ──────────────────────────────────────────────────────── */
  async function runTry() {
    if (!S.tree || isBranch() || S.tryState === 'running') return;
    const text = String($('#tryText').value || '');
    if (!text.trim()) {
      toast(t('routing.tryNeedText'));
      $('#tryText').focus();
      return;
    }
    if (dirtyItems().length) {
      const ok = await saveTree({ auto: true });
      if (!ok) return;
    }
    S.tryText = text;
    S.tryState = 'running';
    S.rawOpen = false;
    renderTry();
    try {
      const result = await api.post('/admin/try', { tree_id: S.tree.id, text: text, session_id: '' });
      S.tryResult = result;
      S.tryState = 'done';
    } catch (error) {
      S.tryResult = null;
      S.tryState = 'idle';
      const message = tf('routing.tryError', { err: apiErrText(error) });
      setIssues([{ code: 'try', zh: message, en: message }]);
      toast(message);
    }
    renderTry();
  }

  /* ── tabs ─────────────────────────────────────────────────────── */
  function setTab(tab, opts) {
    S.tab = tab;
    $$('.rt2-tab').forEach(button => {
      const on = button.dataset.tab === tab;
      button.classList.toggle('on', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('.rt2-pane').forEach(pane => { pane.hidden = pane.dataset.pane !== tab; });
    if (tab === 'try' && !(opts && opts.quiet)) {
      const textarea = $('#tryText');
      if (textarea) textarea.focus();
    }
  }

  /* ── add-model popover ────────────────────────────────────────── */
  function popHtml() {
    const added = new Set(options().map(option => modelIds(option)[0]).filter(Boolean));
    const items = S.models.map(model => {
      const isAdded = added.has(model.id);
      const capability = String(model.capability || '').trim();
      return `<button class="rt2-pitem" type="button" data-model="${esc(model.id)}"
          data-search="${esc((model.id + ' ' + capability).toLowerCase())}"${isAdded ? ' disabled' : ''}>
        <span class="rt2-pico">${esc(String(model.id || '?').slice(0, 2).toUpperCase())}</span>
        <span class="rt2-pt">
          <span class="rt2-pn2"><code>${esc(model.id)}</code></span>
          <span class="rt2-ps">${esc(snippet(capability, 46))}</span>
        </span>
        <span class="rt2-padd${isAdded ? ' on' : ''}">${esc(isAdded ? t('routing.addAlready') : '＋')}</span>
      </button>`;
    }).join('');
    return `<div class="rt2-pop-h"><b>${esc(t('routing.addTitle'))}</b><span>${options().length} / ${S.models.length}</span></div>
      <label class="rt2-pop-search">${ic('search')}<input id="popSearch" autocomplete="off" spellcheck="false" placeholder="${esc(t('routing.addSearch'))}"></label>
      <div class="rt2-pop-list" id="popList">
        <div class="rt2-pop-custom" id="popCustom" hidden>
          <span class="rt2-pico">${ic('plus')}</span>
          <input id="popCustomId" autocomplete="off" spellcheck="false" placeholder="model-id">
          <button class="btn sm pri" type="button" data-act="custom-add">${esc(t('common.add'))}</button>
          <p class="rt2-pop-err" id="popErr" hidden>${esc(t('routing.addCustomMissing'))}</p>
        </div>
        <button class="rt2-pitem" type="button" data-act="custom-open">
          <span class="rt2-pico">${ic('plus')}</span>
          <span class="rt2-pt"><span class="rt2-pn2">${esc(t('routing.addCustom'))}</span><span class="rt2-ps">${esc(t('routing.addCustomHint'))}</span></span>
        </button>
        ${items}
        <div class="rt2-pop-empty" id="popEmpty" hidden>${esc(t('routing.addEmpty'))}</div>
      </div>
      <div class="rt2-pop-foot"><a href="models.html">${esc(t('routing.goModels'))}</a></div>`;
  }

  function openAddPop() {
    if (isBranch() || !S.tree) return;
    const pop = $('#addModelPop');
    if (!pop.hidden) { closeAddPop(); return; }
    pop.innerHTML = popHtml();
    pop.hidden = false;
    S.popOpen = true;
    const rect = $('#addModelBtn').getBoundingClientRect();
    const width = 356;
    const left = Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12));
    pop.style.left = Math.round(left) + 'px';
    pop.style.top = Math.round(rect.bottom + 7) + 'px';
    const input = $('#popSearch');
    if (input) setTimeout(() => input.focus(), 10);
  }

  function closeAddPop() {
    const pop = $('#addModelPop');
    if (pop.hidden) return;
    pop.hidden = true;
    pop.innerHTML = '';
    S.popOpen = false;
  }

  function addCandidate(modelId) {
    const root = rootJev();
    const model = modelOf(modelId);
    if (!root || !model) return false;
    const keys = new Set((root.options || []).map(option => option.key));
    let baseKey = String(modelId).replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 40) || 'option';
    let key = baseKey;
    let n = 2;
    while (keys.has(key)) key = baseKey + '-' + n++;
    const labels = new Set((root.options || []).map(option => option.label));
    let label = modelId;
    let ln = 2;
    while (labels.has(label)) label = modelId + ' (' + ln++ + ')';

    root.options = root.options || [];
    root.options.push({
      key,
      label,
      target: {
        id: nextTargetId(),
        type: 'target',
        strategy: 'least_inflight',
        capability: '',
        models: [{ model: modelId, reasoning: null }],
      },
    });
    if (!fallbackModel()) {
      const target = fallbackTarget();
      target.models = [{ model: modelId, reasoning: null }];
    }
    return true;
  }

  function submitCustom() {
    const input = $('#popCustomId');
    if (!input) return;
    const id = String(input.value || '').trim();
    if (!id) return;
    if (!modelOf(id)) {
      const error = $('#popErr');
      if (error) error.hidden = false;
      input.focus();
      return;
    }
    if (addCandidate(id)) {
      closeAddPop();
      setIssues([]);
      renderAll();
      const row = $$('#candidateList .rt2-mrow').find(el => {
        const option = optionByKey(el.dataset.key);
        return option && modelIds(option)[0] === id;
      });
      if (row) {
        row.scrollIntoView({ block: 'nearest' });
        row.focus();
      }
    }
  }

  /* ── clipboard / file import / export ─────────────────────────── */
  function copyText(text, okMessage) {
    const fallback = () => {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.top = '-1000px';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      area.remove();
      return ok;
    };
    const done = ok => toast(ok ? okMessage || t('routing.copied') : t('routing.copied'));
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => done(true), () => done(fallback()));
    } else {
      done(fallback());
    }
  }

  function requestJson() {
    const root = rootJev();
    const criteria = {};
    options().forEach(option => {
      criteria[previewLabel(option)] = previewCapability(option);
    });
    return {
      model: jevModelName(),
      state: stateTemplate(),
      questions: {
        route: {
          type: 'choice',
          instructions: root ? root.instructions || '' : '',
          criteria,
        },
      },
    };
  }

  function exportTree() {
    if (!S.tree) return;
    const payload = { kind: 'sr-jev-routing-tree', version: 1, tree: treePayload() };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = ((S.tree && S.tree.id) || 'jev-tree') + '.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  }

  async function importTree(file) {
    try {
      const data = JSON.parse(await file.text());
      if (!data || data.kind !== 'sr-jev-routing-tree' || data.version !== 1 || !data.tree) {
        throw new Error(t('routing.importBad'));
      }
      const tree = clone(data.tree);
      if (!tree.root || tree.root.type !== 'jev') throw new Error(t('routing.importBad'));
      delete tree.id;
      delete tree.enabled;
      delete tree.tree_revision;
      delete tree.updated_at;
      setIssues([]);
      S.tree = tree;
      S.base = null;
      S.isNew = true;
      S.editing = null;
      S.draftLabel = '';
      S.draftCap = '';
      S.tryState = 'idle';
      S.tryResult = null;
      renderAll();
    } catch (error) {
      setIssues([{ code: 'import', zh: error.message, en: error.message }]);
    }
  }

  /* ── events ───────────────────────────────────────────────────── */
  function bind() {
    /* prompt */
    $('#instructions').addEventListener('input', event => {
      const root = rootJev();
      if (!root) return;
      root.instructions = event.target.value;
      $('#charCount').innerHTML = tf('routing.chars', { n: `<b>${event.target.value.length}</b>` });
      renderPreview();
      renderHeader();
    });
    $('#restoreDefault').addEventListener('click', () => {
      const root = rootJev();
      if (!root) return;
      root.instructions = defaultInstructions() || t('routing.instructionsPlaceholder');
      renderPrompt();
      renderPreview();
      renderHeader();
      $('#instructions').focus();
    });

    /* candidate list: click / input / keyboard / drag */
    const list = $('#candidateList');
    list.addEventListener('click', event => {
      if (event.target.closest('[data-grip]')) return;
      const action = event.target.closest('[data-act]');
      if (action) {
        const act = action.dataset.act;
        const row = action.closest('.rt2-mrow');
        const key = row && row.dataset.key;
        if (act === 'add-empty') openAddPop();
        else if (act === 'edit' && key) openEditor(key);
        else if (act === 'del' && key) removeCandidate(key);
        else if (act === 'cancel') cancelEditor();
        else if (act === 'commit') commitEditor();
        return;
      }
      if (event.target.closest('input, textarea, select, button')) return;
      const row = event.target.closest('.rt2-mrow');
      if (row && !row.classList.contains('editing')) openEditor(row.dataset.key);
    });

    list.addEventListener('input', event => {
      const name = event.target.closest('[data-edit-name]');
      if (name) {
        S.draftLabel = name.value;
        const note = $('#candidateList .rt2-editnote');
        if (note) note.textContent = '→ criteria["' + (S.draftLabel || '') + '"]';
        renderPreview();
        renderHeader();
        return;
      }
      const cap = event.target.closest('[data-edit-cap]');
      if (cap) {
        S.draftCap = cap.value;
        const count = $('#candidateList [data-editcount]');
        if (count) count.textContent = String(cap.value.length);
        renderPreview();
        renderHeader();
      }
    });

    list.addEventListener('keydown', event => {
      const row = event.target.closest('.rt2-mrow');
      if (!row) return;
      const editing = row.classList.contains('editing');
      const modified = event.metaKey || event.ctrlKey;
      if ((event.key === 'Enter' || event.key === ' ') && !modified) {
        if (!editing && event.target === row) {
          event.preventDefault();
          openEditor(row.dataset.key);
        }
        return;
      }
      if (!editing) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        cancelEditor();
      } else if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        commitEditor();
      }
    });

    list.addEventListener('mousedown', event => {
      const grip = event.target.closest('[data-grip]');
      if (grip) grip.closest('.rt2-mrow').setAttribute('draggable', 'true');
    });

    const clearDrop = () => {
      $$('#candidateList .rt2-mrow').forEach(row => {
        row.classList.remove('dragging', 'dragover', 'over-top', 'over-bot');
        row.removeAttribute('draggable');
      });
    };
    list.addEventListener('dragstart', event => {
      const row = event.target.closest('.rt2-mrow');
      if (!row || row.getAttribute('draggable') !== 'true') { event.preventDefault(); return; }
      S.dragKey = row.dataset.key;
      row.classList.add('dragging');
      try {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', S.dragKey);
      } catch (error) { /* ignore */ }
    });
    list.addEventListener('dragover', event => {
      if (!S.dragKey) return;
      event.preventDefault();
      const row = event.target.closest('.rt2-mrow');
      $$('#candidateList .rt2-mrow').forEach(item => item.classList.remove('dragover', 'over-top', 'over-bot'));
      if (row && row.dataset.key !== S.dragKey) {
        const box = row.getBoundingClientRect();
        row.classList.add('dragover', event.clientY - box.top < box.height / 2 ? 'over-top' : 'over-bot');
      }
    });
    list.addEventListener('drop', event => {
      if (!S.dragKey) return;
      event.preventDefault();
      const row = event.target.closest('.rt2-mrow');
      const root = rootJev();
      if (!row || !root) { S.dragKey = null; clearDrop(); return; }
      const from = (root.options || []).findIndex(option => option.key === S.dragKey);
      const toKey = row.dataset.key;
      if (from < 0 || toKey === S.dragKey) { S.dragKey = null; clearDrop(); return; }
      const box = row.getBoundingClientRect();
      const before = event.clientY - box.top < box.height / 2;
      const moved = root.options.splice(from, 1)[0];
      let to = root.options.findIndex(option => option.key === toKey);
      if (!before) to += 1;
      root.options.splice(to, 0, moved);
      S.dragKey = null;
      clearDrop();
      setIssues([]);
      renderAll();
    });
    list.addEventListener('dragend', () => { S.dragKey = null; clearDrop(); });

    /* add-model popover */
    $('#addModelBtn').addEventListener('click', event => {
      event.stopPropagation();
      openAddPop();
    });
    const pop = $('#addModelPop');
    pop.addEventListener('click', event => {
      if (event.target.closest('[data-act="custom-open"]')) {
        const box = $('#popCustom');
        if (box) {
          box.hidden = false;
          const input = $('#popCustomId');
          if (input) input.focus();
        }
        return;
      }
      if (event.target.closest('[data-act="custom-add"]')) { submitCustom(); return; }
      const item = event.target.closest('[data-model]');
      if (!item || item.disabled) return;
      if (addCandidate(item.dataset.model)) {
        closeAddPop();
        setIssues([]);
        renderAll();
      }
    });
    pop.addEventListener('input', event => {
      if (event.target.id !== 'popSearch') return;
      const query = String(event.target.value || '').trim().toLowerCase();
      let visible = 0;
      $$('#popList .rt2-pitem[data-model]').forEach(item => {
        const hit = !query || (item.dataset.search || '').indexOf(query) >= 0;
        item.hidden = !hit;
        if (hit) visible += 1;
      });
      const empty = $('#popEmpty');
      if (empty) empty.hidden = visible > 0 || !query;
    });
    pop.addEventListener('keydown', event => {
      if (event.key === 'Enter' && event.target.id === 'popCustomId') {
        event.preventDefault();
        submitCustom();
      }
    });
    document.addEventListener('click', event => {
      if ($('#addModelPop').hidden) return;
      if (event.target.closest('#addModelPop') || event.target.closest('#addModelBtn')) return;
      // the empty candidate list opens the same popover from inside the list
      if (event.target.closest('#candidateList [data-act="add-empty"]')) return;
      closeAddPop();
    });
    window.addEventListener('resize', closeAddPop);
    window.addEventListener('scroll', closeAddPop, true);

    /* fallback */
    $('#fallbackSelect').addEventListener('change', event => {
      const modelId = event.target.value;
      const root = rootJev();
      if (!root || !modelId) return;
      const target = fallbackTarget();
      target.type = 'target';
      target.strategy = 'least_inflight';
      target.capability = '';
      target.models = [{ model: modelId, reasoning: null }];
      setIssues([]);
      renderFallback();
      renderHeader();
    });

    /* advanced */
    $('#advancedToggle').addEventListener('click', () => {
      const panel = $('#advanced');
      const open = !panel.classList.contains('open');
      panel.classList.toggle('open', open);
      $('#advancedBody').hidden = !open;
      $('#advancedToggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    $('#treeSelect').addEventListener('change', event => {
      const value = event.target.value;
      if (!value) { startNewTree(); return; }
      if (S.tree && S.tree.id === value && !S.isNew) return;
      selectTree(value);
    });
    $('#newTree').addEventListener('click', startNewTree);
    $('#branchNew').addEventListener('click', createSimplifiedTree);
    $('#copyTree').addEventListener('click', async () => {
      if (!S.tree || S.isNew) return;
      try {
        const name = ((S.tree.name || S.tree.id) + ' · ' + t('routing.copyTree')).trim();
        const response = await api.post('/admin/trees/' + encodeURIComponent(S.tree.id) + '/copy', { name });
        const listed = await api.get('/admin/trees');
        S.trees = listed.trees || S.trees;
        S.rev = listed.snapshot_revision || S.rev;
        selectTree(response.tree.id);
      } catch (error) {
        reportError(error, 'routing.saveFail');
      }
    });
    $('#treeName').addEventListener('input', event => {
      if (!S.tree) return;
      S.tree.name = event.target.value;
      renderAdvanced();
      renderHeader();
    });
    $('#timezone').addEventListener('input', event => {
      if (!S.tree) return;
      S.tree.timezone = event.target.value;
      renderHeader();
    });
    $('#stickyEnabled').addEventListener('change', event => {
      if (!S.tree) return;
      S.tree.sticky = S.tree.sticky || { enabled: false, ttl_seconds: 1800 };
      S.tree.sticky.enabled = event.target.checked;
      renderHeader();
    });
    $('#stickyTtl').addEventListener('input', event => {
      if (!S.tree) return;
      S.tree.sticky = S.tree.sticky || { enabled: false, ttl_seconds: 1800 };
      S.tree.sticky.ttl_seconds = ttlOf(event.target.value);
      renderHeader();
    });
    $('#minProb').addEventListener('input', event => {
      const root = rootJev();
      if (!root) return;
      root.min_prob = unit(event.target.value === '' ? 0 : event.target.value);
      renderHeader();
    });
    $('#minMargin').addEventListener('input', event => {
      const root = rootJev();
      if (!root) return;
      root.min_margin = unit(event.target.value === '' ? 0 : event.target.value);
      renderHeader();
    });
    $('#importTree').addEventListener('click', () => $('#importFile').click());
    $('#importFile').addEventListener('change', async event => {
      const file = event.target.files && event.target.files[0];
      if (file) await importTree(file);
      event.target.value = '';
    });
    $('#exportTree').addEventListener('click', exportTree);

    /* head / dock */
    $('#saveTree').addEventListener('click', () => saveTree());
    $('#enableTree').addEventListener('click', toggleEnable);
    $('#headerTry').addEventListener('click', () => {
      setTab('try');
      const textarea = $('#tryText');
      if (textarea) textarea.focus();
    });
    $('#copyJson').addEventListener('click', () => {
      copyText(JSON.stringify(requestJson(), null, 2), t('routing.copied'));
    });
    $('#issues').addEventListener('click', event => {
      if (event.target.closest('[data-close]')) {
        setIssues([]);
        return;
      }
      const row = event.target.closest('[data-issue]');
      if (!row) return;
      const issue = S.issues[Number(row.dataset.issue)];
      if (!issue) return;
      const target = issueTarget(issue);
      if (!target) return;
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
      target.classList.remove('rt2-flash');
      void target.offsetWidth;
      target.classList.add('rt2-flash');
    });

    $$('.rt2-tab').forEach(button => {
      button.addEventListener('click', () => setTab(button.dataset.tab));
    });

    /* try pane */
    $('#tryText').addEventListener('input', event => {
      S.tryText = event.target.value;
      renderTryInputs();
    });
    $('#tryChips').addEventListener('click', event => {
      const chip = event.target.closest('[data-sample]');
      if (!chip) return;
      S.tryText = sample(chip.dataset.sample);
      renderTryInputs();
      $('#tryText').focus();
    });
    $('#tryRun').addEventListener('click', runTry);
    $('#tryOut').addEventListener('click', event => {
      if (!event.target.closest('[data-act="raw"]')) return;
      S.rawOpen = !S.rawOpen;
      renderTry();
    });

    /* keyboard */
    document.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && String(event.key).toLowerCase() === 's') {
        event.preventDefault();
        saveTree();
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        // While a capability is being edited the shortcut saves that draft;
        // running a try is the shortcut for the rest of the page.
        if (S.editing) { commitEditor(); return; }
        setTab('try', { quiet: true });
        runTry();
        return;
      }
      if (event.key === 'Escape') {
        if (!$('#addModelPop').hidden) closeAddPop();
        else if (S.editing) cancelEditor();
      }
    });
  }

  /* ── boot ─────────────────────────────────────────────────────── */
  async function boot() {
    bind();
    S.tryText = sample('1');
    try {
      const [trees, models, jev] = await Promise.all([
        api.get('/admin/trees'),
        api.get('/admin/models'),
        api.get('/admin/jev'),
      ]);
      S.trees = trees.trees || [];
      S.rev = trees.snapshot_revision || '';
      S.models = models.models || [];
      S.models.forEach(model => { S.modelMap[model.id] = model; });
      S.jev = jev || null;
      const initial = pickInitialTree();
      if (initial) {
        S.tree = clone(initial);
        S.base = clone(initial);
        S.isNew = false;
      } else {
        S.tree = defaultTree();
        S.isNew = true;
      }
    } catch (error) {
      const message = tf('routing.loadFail', { err: apiErrText(error) });
      setIssues([{ code: 'load', zh: message, en: message }]);
    }
    S.booted = true;
    renderAll();
    setTab('preview', { quiet: true });
  }

  onLangChange(() => { if (S.booted) renderAll(); });
  boot();
})();
