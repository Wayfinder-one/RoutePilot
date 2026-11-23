/* RoutePilot console · 共享影响确认（模型 / provider / JEV 目录写操作）
   ------------------------------------------------------------------
   目录写操作的 409 两阶段协议（model / provider / JEV 更新与删除）：
   - 第一次写（不带 impact_confirmation）命中已存画布引用 → 409
     `detail.code = "impact_confirmation_required"`，`detail` 携带
     `resource` / `affected_trees[{id,name,enabled,document_sha256,usage}]` /
     `replacements[]`（删除时可选替代项，非密钥投影）。
   - 用户在共享确认框里确认后才重试；重试体携带
     `impact_confirmation = confirmationFrom(detail)`（**精确哈希**）与
     操作所需的替代字段。通用 `replacement_id` 只在本模块与调用方之间传递，
     调用方负责映射成后端字段（`replacement_model_id` / `replacement_jev_id`），
     绝不直接发 `replacement_id`。
   - 哈希过期（并发改画布）→ 第二次 409 `detail.code = "graph_conflict"`
     `actual_document_sha256`：显示“重新加载画布 / 重新读取影响清单”，
     不盲目重试。
   - provider 删除的 409 `provider_referenced` **不可绕过**：只展示引用清单，
     没有确认写入按钮。

   纪律：
   - 取消不发出任何请求；确认按钮在需要替代项而未选择时禁用。
   - 全部动态文本用 textContent 构建（画布名 / id / 地址都是不可信输入），
     不拼未转义的 innerHTML。
   - 焦点管理沿用既有 modal 约定：打开时聚焦首个可操作项，关闭时还原焦点，
     Esc / 遮罩点击 = 取消。
   - zh / en 文案全部来自 i18n；语言切换时重绘当前打开的确认框。

   该模块是纯页面模块，可在 Node 直测（node --test）里只 import 纯函数：
   顶层不触碰 document / window。
   ------------------------------------------------------------------ */
import { apiErrText } from './api.js';
import { onLangChange, t, tf } from './i18n.js';

const IMPACT_CODE = 'impact_confirmation_required';
const CONFLICT_CODE = 'graph_conflict';
const BLOCKED_CODE = 'provider_referenced';

/* ── 纯逻辑（Node 直测） ─────────────────────────────────────────────── */

/** 409 影响确认预览：只有 status 409 且 payload code 匹配才算。 */
export function isImpactError(error) {
  return !!error && error.status === 409 &&
    !!error.detail && error.detail.code === IMPACT_CODE;
}

/** 409 陈旧哈希：确认重试时画布已被并发修改。 */
export function isGraphConflict(error) {
  return !!error && error.status === 409 &&
    !!error.detail && error.detail.code === CONFLICT_CODE;
}

/** 409 不可绕过的 provider 删除阻塞。 */
export function isProviderReferenced(error) {
  return !!error && error.status === 409 &&
    !!error.detail && error.detail.code === BLOCKED_CODE;
}

/**
 * 从 409 detail 生成确认载荷：受影响画布的 id + **精确** document_sha256。
 * 只投影这两个字段，服务端协议不接受的字段绝不带上。
 */
export function confirmationFrom(detail) {
  const trees = detail && Array.isArray(detail.affected_trees) ? detail.affected_trees : [];
  return {
    trees: trees.map(tree => ({
      id: tree && tree.id,
      document_sha256: tree && tree.document_sha256,
    })),
  };
}

/**
 * 这次操作是否必须选择替代项（后端语义）：
 * - 模型删除：有 target 槽位引用时必须给 `replacement_model_id`；
 *   只有运行目录引用（runtime_catalog）时不需要。
 * - JEV 删除：只要还有画布引用该服务，就必须给 `replacement_jev_id`
 *   （同步会把 runtime.jev 与窗口整体替换成替代服务）。
 * - 更新 / provider 操作：永不需要替代项。
 */
export function requiresReplacement(detail, operation) {
  if (!detail || operation !== 'delete') return false;
  const resource = detail.resource && typeof detail.resource === 'object' ? detail.resource : {};
  const trees = Array.isArray(detail.affected_trees) ? detail.affected_trees : [];
  if (resource.kind === 'model') {
    return trees.some(tree => {
      const targets = tree && tree.usage && tree.usage.targets;
      return Array.isArray(targets) && targets.length > 0;
    });
  }
  if (resource.kind === 'jev') return trees.length > 0;
  return false;
}

/** 替代项在下拉框里的标签：id + 非密钥目录事实。 */
export function replacementLabel(item) {
  if (!item || typeof item !== 'object') return '';
  const id = item.id == null ? '' : String(item.id);
  const detail = item.upstream || item.model || item.base_url || '';
  return detail ? id + ' · ' + String(detail) : id;
}

/* ── dialog state ────────────────────────────────────────────────────── */
let dialog = null;      // 复用的 DOM 节点
let current = null;     // 当前打开的确认上下文
let langHooked = false;

function ensureLangHook() {
  if (langHooked) return;
  langHooked = true;
  onLangChange(() => {
    if (!current || !dialog || dialog.root.hidden) return;
    render();
  });
}

/* ── tiny DOM helpers (textContent only) ─────────────────────────────── */
function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = String(text);
  return el;
}
function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}
function shortHash(value) {
  const text = value == null ? '' : String(value);
  return text.length > 12 ? text.slice(0, 12) + '…' : text;
}

function ensureDialog() {
  if (dialog) return dialog;
  ensureLangHook();
  const root = node('div', 'modal impact-modal');
  root.id = 'impactModal';
  root.hidden = true;
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-labelledby', 'impactTitle');
  const card = node('div', 'modal-card impact-card');
  const head = node('div', 'modal-h');
  const title = node('b');
  title.id = 'impactTitle';
  const close = node('button', 'modal-x', '✕');
  close.type = 'button';
  close.setAttribute('data-impact-cancel', '');
  close.id = 'impactX';
  head.append(title, close);
  const body = node('div', 'modal-body');
  body.id = 'impactBody';
  const foot = node('div', 'modal-foot');
  const msg = node('span', 'modal-msg');
  msg.id = 'impactMsg';
  msg.setAttribute('role', 'status');
  msg.setAttribute('aria-live', 'polite');
  const cancel = node('button', 'btn');
  cancel.type = 'button';
  cancel.id = 'impactCancel';
  cancel.setAttribute('data-impact-cancel', '');
  const confirm = node('button', 'btn pri');
  confirm.type = 'button';
  confirm.id = 'impactConfirm';
  confirm.setAttribute('data-impact-confirm', '');
  foot.append(msg, cancel, confirm);
  card.append(head, body, foot);
  root.append(card);
  document.body.appendChild(root);
  root.addEventListener('click', ev => {
    if (ev.target === root) cancelDialog();
  });
  /* Escape 用 document 捕获阶段处理：必须抢在页面级 closeTopModal（bubble）
     之前结算 impact Promise，否则上层保存/删除会永远 pending。 */
  document.addEventListener('keydown', ev => {
    if (ev.key !== 'Escape') return;
    if (!current || !dialog || dialog.root.hidden) return;
    ev.preventDefault();
    ev.stopPropagation();
    cancelDialog();
  }, true);
  close.addEventListener('click', () => cancelDialog());
  cancel.addEventListener('click', () => cancelDialog());
  confirm.addEventListener('click', () => { confirmDialog(); });
  dialog = { root, title, body, msg, cancel, confirm, previousFocus: null };
  return dialog;
}

/* ── rendering ───────────────────────────────────────────────────────── */
function detailOf() {
  return current && current.detail ? current.detail : {};
}
function resourceOf() {
  const detail = detailOf();
  return (current && current.resource) || (detail.resource) || {};
}
function titleKey() {
  if (current && current.blocked) return 'impact.blockedTitle';
  const kind = resourceOf().kind;
  if (kind === 'model') return 'impact.titleModel';
  if (kind === 'provider') return 'impact.titleProvider';
  if (kind === 'jev') return 'impact.titleJev';
  return 'impact.title';
}
function conflictActual() {
  if (!current || !current.conflict) return '';
  if (typeof current.conflict === 'string') return current.conflict;
  const detail = detailOf();
  return detail.actual_document_sha256 || '';
}

function buildUsage(tree, usage) {
  const wrap = node('div', 'impact-usage');
  const push = text => {
    if (!text) return;
    if (wrap.childNodes.length) wrap.append(document.createTextNode(' · '));
    wrap.append(node('span', null, text));
  };
  if (usage && Array.isArray(usage.targets) && usage.targets.length) {
    push(tf('impact.usageTargets', { nodes: usage.targets.map(String).join(', ') }));
  }
  if (usage && usage.runtime_catalog) push(t('impact.usageRuntime'));
  if (usage && usage.jev != null) push(tf('impact.usageJev', { id: String(usage.jev) }));
  if (usage && usage.models && Array.isArray(usage.models.use) && usage.models.use.length) {
    push(tf('impact.usageModels', { models: usage.models.use.map(String).join(', ') }));
  }
  if (!wrap.childNodes.length) push(t('impact.usageUnknown'));
  return wrap;
}

function buildTreeList(trees) {
  const list = node('ul', 'impact-list');
  list.id = 'impactTrees';
  trees.forEach(tree => {
    const item = node('li', 'impact-tree');
    item.dataset.treeId = tree && tree.id != null ? String(tree.id) : '';
    const head = node('div', 'impact-tree-head');
    const name = node('b', 'impact-name', (tree && (tree.name || tree.id)) || '—');
    const id = node('span', 'mono mut impact-id', tree && tree.id != null ? tree.id : '—');
    const enabled = node('span', 'pill',
      tree && tree.enabled ? t('impact.enabled') : t('impact.disabled'));
    if (tree && tree.enabled) enabled.classList.add('ok');
    head.append(name, id, enabled);
    const sha = node('code', 'impact-sha', shortHash(tree && tree.document_sha256));
    sha.title = tree && tree.document_sha256 ? String(tree.document_sha256) : '';
    item.append(head, buildUsage(tree, tree && tree.usage), sha);
    list.append(item);
  });
  return list;
}

function selectedReplacement() {
  if (!current) return null;
  const list = Array.isArray(current.replacements) ? current.replacements : [];
  return list.find(item => item && String(item.id) === String(current.selectedId)) || null;
}
function replacementRequired() {
  return !!(current && !current.blocked && !current.conflict &&
    requiresReplacement(detailOf(), current.operation));
}
function confirmDisabled() {
  if (!current || current.busy || current.blocked || current.conflict) return true;
  if (replacementRequired()) return !selectedReplacement();
  return false;
}

function render() {
  if (!current || !dialog) return;
  const detail = detailOf();
  dialog.title.textContent = t(titleKey());
  clear(dialog.body);

  if (current.conflict) {
    const notice = node('div', 'notice bad impact-conflict');
    notice.id = 'impactConflict';
    notice.textContent = tf('impact.stale', { actual: shortHash(conflictActual()) || '—' });
    dialog.body.append(notice);
  } else {
    const hint = node('p', 'hint');
    hint.textContent = current.blocked ? t('impact.blockedHint') : t('impact.subtitle');
    dialog.body.append(hint);
  }

  /* 操作对象自身（资源类型 + ID）：用户必须能确认自己正在改哪个资源，
     不能只看到受影响画布清单。provider 的资源标识字段是 name。 */
  const resource = resourceOf();
  const resourceId = resource.id != null ? resource.id : resource.name;
  const resourceLine = node('p', 'mut impact-resource');
  resourceLine.id = 'impactResource';
  resourceLine.textContent = tf('impact.resourceLine', {
    kind: resource.kind || '—',
    id: resourceId == null ? '—' : String(resourceId),
  });
  dialog.body.append(resourceLine);

  if (current.blocked) {
    const blocked = node('div', 'notice warn');
    blocked.id = 'impactBlocked';
    blocked.textContent = t('impact.providerBlocked');
    dialog.body.append(blocked);
    const used = Array.isArray(detail.models) ? detail.models.filter(Boolean) : [];
    if (used.length) {
      const line = node('p', 'impact-blocked-models');
      line.textContent = tf('impact.blockedModels', { models: used.map(String).join(', ') });
      dialog.body.append(line);
    }
  }

  const trees = Array.isArray(detail.affected_trees) ? detail.affected_trees : [];
  if (trees.length) dialog.body.append(buildTreeList(trees));
  else {
    const empty = node('p', 'hint');
    empty.textContent = t('impact.noTrees');
    dialog.body.append(empty);
  }

  const needReplacement = replacementRequired();
  if (needReplacement) {
    const box = node('div', 'impact-replacement');
    const label = node('label', 'field');
    label.append(node('span', null, t('impact.replacement')));
    const select = document.createElement('select');
    select.id = 'impactReplacement';
    const prompt = node('option', null, t('impact.replacementPick'));
    prompt.value = '';
    select.append(prompt);
    current.replacements.forEach(item => {
      const option = node('option', null, replacementLabel(item));
      option.value = String(item.id);
      select.append(option);
    });
    select.value = current.selectedId ? String(current.selectedId) : '';
    select.addEventListener('change', () => {
      current.selectedId = select.value;
      paintFoot();
    });
    label.append(select);
    box.append(label);
    if (!current.replacements.length) {
      const none = node('div', 'notice bad');
      none.id = 'impactNoReplacement';
      none.textContent = t('impact.noReplacement');
      box.append(none);
    } else {
      const hint = node('p', 'hint');
      hint.textContent = t('impact.replacementHint');
      box.append(hint);
    }
    dialog.body.append(box);
  }

  dialog.confirm.hidden = !!current.blocked || !!current.conflict;
  dialog.msg.textContent = current.errorText || '';
  dialog.msg.classList.toggle('bad', !!current.errorText);
  paintFoot();
}

function paintFoot() {
  if (!current || !dialog) return;
  dialog.cancel.textContent = current.conflict || current.blocked ? t('impact.close') : t('impact.cancel');
  dialog.confirm.textContent = current.busy ? t('impact.working') : t('impact.confirm');
  dialog.confirm.disabled = confirmDisabled();
  dialog.cancel.disabled = !!current.busy;
}

/* ── open / close / actions ──────────────────────────────────────────── */
function openDialog() {
  const d = ensureDialog();
  d.previousFocus = typeof document.activeElement === 'object' ? document.activeElement : null;
  d.root.hidden = false;
  document.body.classList.add('modal-open');
  const target = !d.confirm.hidden && !d.confirm.disabled ? d.confirm
    : (document.getElementById('impactReplacement') || d.cancel);
  if (target && typeof target.focus === 'function') {
    try { target.focus({ preventScroll: true }); } catch (error) { /* ignore */ }
  }
}

function closeDialog(confirmed) {
  const d = dialog;
  if (!d) return;
  d.root.hidden = true;
  if (!Array.from(document.querySelectorAll('.modal')).some(el => !el.hidden)) {
    document.body.classList.remove('modal-open');
  }
  if (d.previousFocus && d.previousFocus.isConnected && typeof d.previousFocus.focus === 'function') {
    try { d.previousFocus.focus({ preventScroll: true }); } catch (error) { /* ignore */ }
  }
  d.previousFocus = null;
  const settle = d.settle;
  d.settle = null;
  current = null;
  if (typeof settle === 'function') settle(!!confirmed);
}

function cancelDialog() {
  if (!current || current.busy) return;
  closeDialog(false);
}

async function confirmDialog() {
  if (!current || current.busy || current.blocked || current.conflict) return;
  if (confirmDisabled()) return;
  const replacement = replacementRequired() ? selectedReplacement() : null;
  if (replacementRequired() && !replacement) return;
  current.busy = true;
  current.errorText = '';
  render();
  try {
    if (typeof current.onConfirm === 'function') await current.onConfirm(replacement);
  } catch (error) {
    if (!current) return;
    current.busy = false;
    if (isGraphConflict(error)) {
      current.conflict = (error.detail && error.detail.actual_document_sha256) || true;
      render();
      return;
    }
    current.errorText = tf('impact.failed', { err: apiErrText(error) });
    render();
    return;
  }
  if (current) closeDialog(true);
}

/**
 * 打开共享影响确认框。
 *
 * @param {object} options
 * @param {object} options.detail      409 detail（impact_confirmation_required /
 *                                     graph_conflict / provider_referenced）
 * @param {object} [options.resource]  资源标识（默认取 detail.resource）
 * @param {Array}  [options.replacements] 替代项（默认取 detail.replacements）
 * @param {'update'|'delete'} [options.operation] 决定是否需要替代项（默认 update）
 * @param {string} [options.preselectReplacementId] 预选替代项 id
 * @param {boolean} [options.blocked]  强制“仅告知”模式（provider_referenced）
 * @param {boolean} [options.conflict] 强制“陈旧哈希”模式
 * @param {(replacement: object|null) => Promise<void>} [options.onConfirm]
 * @returns {Promise<boolean>} 确认成功 true；取消 / 失败 / 陈旧 false
 */
export function openImpact(options) {
  const opts = options || {};
  const detail = opts.detail || {};
  ensureDialog();
  const conflict = !!opts.conflict || detail.code === CONFLICT_CODE;
  const blocked = !conflict && (!!opts.blocked || detail.code === BLOCKED_CODE ||
    typeof opts.onConfirm !== 'function');
  const replacements = Array.isArray(opts.replacements) ? opts.replacements
    : (Array.isArray(detail.replacements) ? detail.replacements : []);
  const preselected = opts.preselectReplacementId == null ? '' : String(opts.preselectReplacementId);
  return new Promise(resolve => {
    dialog.settle = resolve;
    current = {
      resource: opts.resource || detail.resource || {},
      detail,
      replacements,
      operation: opts.operation === 'delete' ? 'delete' : 'update',
      onConfirm: typeof opts.onConfirm === 'function' ? opts.onConfirm : null,
      selectedId: preselected,
      blocked,
      conflict,
      errorText: '',
      busy: false,
    };
    render();
    openDialog();
    if (preselected && !blocked && !conflict) paintFoot();
  });
}

/* ── 两阶段写：第一次原样请求，409 确认后带精确哈希重试 ───────────────── */
function normalizeOptions(options) {
  if (typeof options === 'string') return { replacementId: options };
  if (options && typeof options === 'object') return options;
  return {};
}

/**
 * 共享两阶段写助手。
 *
 * `requestFn(confirmation)`：
 * - 第一次以 `undefined` 调用（原样写请求）；
 * - 命中 409 影响预览并确认后，以
 *   `{ impact_confirmation: {trees:[{id,document_sha256}]}, replacement_id }`
 *   重试。`replacement_id` 是**通用**字段，调用方必须映射成
 *   `replacement_model_id` / `replacement_jev_id` 再发送；绝不原样发给后端。
 *
 * 返回契约（每个 key 始终存在，调用方不必猜缺省值）：
 * - 成功：`{ok:true, result}`；
 * - 用户取消确认框：`{ok:false, canceled:true, conflict:false, blocked:false, error:null}`；
 * - 不可绕过的 provider 引用：`{ok:false, canceled:false, conflict:false, blocked:true, error}`
 *   （`error` 是第一次写返回的 409）；
 * - 确认重试时哈希过期：`{ok:false, canceled:false, conflict:true, blocked:false, error}`
 *   （`error` 是重试时的 `graph_conflict`）；
 * - 第一次写就命中 `graph_conflict`：同上 `conflict:true`；
 * - 其他错误原样抛出（调用方的 catch 负责展示）。
 *
 * @param {(confirmation?: object) => Promise<any>} requestFn
 * @param {string|object} [options] 替代项预选 id，或
 *        `{ operation:'delete', preselectReplacementId, replacements }`
 * @returns {Promise<{ok:true, result:any} |
 *   {ok:false, canceled:boolean, conflict:boolean, blocked:boolean, error:(Error|null)}>}
 */
export async function withImpactConfirmation(requestFn, options) {
  const opts = normalizeOptions(options);
  let first;
  try {
    first = await requestFn(undefined);
  } catch (error) {
    if (isProviderReferenced(error)) {
      await openImpact({ detail: error.detail, replacements: [], blocked: true });
      return { ok: false, canceled: false, conflict: false, blocked: true, error };
    }
    if (isGraphConflict(error)) {
      await openImpact({ detail: error.detail, replacements: [], conflict: true });
      return { ok: false, canceled: false, conflict: true, blocked: false, error };
    }
    if (!isImpactError(error)) throw error;
    const detail = error.detail;
    let conflictError = null;
    let result;
    const accepted = await openImpact({
      resource: detail.resource,
      detail,
      replacements: Array.isArray(opts.replacements) ? opts.replacements : detail.replacements,
      operation: opts.operation,
      preselectReplacementId: opts.preselectReplacementId,
      onConfirm: async replacement => {
        try {
          result = await requestFn({
            impact_confirmation: confirmationFrom(detail),
            replacement_id: replacement && replacement.id != null ? String(replacement.id) : undefined,
          });
        } catch (retryError) {
          if (isGraphConflict(retryError)) conflictError = retryError;
          throw retryError;
        }
      },
    });
    if (accepted) return { ok: true, result };
    if (conflictError) {
      return { ok: false, canceled: false, conflict: true, blocked: false, error: conflictError };
    }
    return { ok: false, canceled: true, conflict: false, blocked: false, error: null };
  }
  return { ok: true, result: first };
}
