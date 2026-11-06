/* RoutingController · 决策组管理面编排（Task 8，规格 Step 4）
   ------------------------------------------------------------------
   唯一职责：保存 / 启用 / 停用 / 切换 / 导入导出这些管理面 API 的编排。
   · 唯一 saving 状态：controller 写 store.setSaving()，Store 据此拒绝编辑；
   · 保存请求发出前记录 targetTree / targetId，回包时用 tree identity 拒绝过期响应；
   · 启用 dirty 草稿时先保存（保存失败绝不再发 enable）；
   · 不直接改 DOM：渲染/提示通过 hooks 回调交给 app.js。
   ------------------------------------------------------------------ */
import { api, apiErrText } from '../api.js';
import { t, tf } from '../i18n.js';
import * as RTGraph from './graph.js';

function serverIssues(error) {
  const detail = error && error.detail;
  if (!Array.isArray(detail)) return null;
  const issues = detail
    .filter(item => item && typeof item === 'object' && !Array.isArray(item))
    .map(item => Object.assign({ level: 'hard' }, item));
  return issues.length ? issues : null;
}

function newTreeId() {
  let raw = '';
  try {
    raw = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : '';
  } catch (error) { raw = ''; }
  if (!raw) raw = Date.now().toString(16) + Math.random().toString(16).slice(2);
  return raw.replace(/[^a-z0-9]/gi, '').slice(0, 12);
}

/* 服务端树 / 本地新草稿统一补齐 envelope 字段与缺失坐标。 */
function normaliseTree(raw) {
  const tree = RTGraph.clone(raw || {});
  tree.root = tree.root || { id: 'root', type: 'graph', nodes: [], edges: [] };
  if (!tree.name) tree.name = t('cv.default.name');
  if (!tree.timezone) tree.timezone = 'Asia/Shanghai';
  if (!tree.sticky) tree.sticky = { enabled: false, ttl_seconds: 1800 };
  if (!Array.isArray(tree.root.nodes)) tree.root.nodes = [];
  if (!Array.isArray(tree.root.edges)) tree.root.edges = [];
  const positions = RTGraph.layoutPositions('dag', tree.root);
  tree.root.nodes.forEach((node, index) => {
    const position = node && node.position;
    const valid = position && typeof position.x === 'number' && Number.isFinite(position.x) &&
      typeof position.y === 'number' && Number.isFinite(position.y);
    if (!valid && node) {
      node.position = positions[node.id] || { x: 30 + index * 30, y: 30 + index * 30 };
    }
  });
  return tree;
}

export class RoutingController {
  /* hooks:
       toast(message, tone)
       askDiscard(reason, copy) -> Promise<boolean>
       render() / renderIssues() / revealIssues() / refreshDirty() / syncSaving()
       setIssues(issues)
       isInspectorEditing() -> boolean
       fitAfterReplace()
       renderTreeSelect()
       getModels() -> []                                        */
  constructor(options) {
    const opts = options || {};
    this.store = opts.store;
    this.hooks = opts.hooks || {};
    this.trees = [];
    this.treeId = '';
    this.snapshotRevision = '';
    /* 单调递增的装载序号：并发 selectTree 只允许最后一次请求把树写进 store。 */
    this.loadSeq = 0;
  }

  get saved() {
    return this.store.saved;
  }

  upsertTree(tree) {
    if (!tree) return;
    const list = this.trees || [];
    const index = list.findIndex(item => item && item.id === tree.id);
    if (index >= 0) list[index] = RTGraph.clone(tree);
    else list.push(RTGraph.clone(tree));
    this.trees = list;
  }

  draftHints() {
    if (!this.store.tree) return [];
    return RTGraph.validate(this.store.tree.root).filter(issue => issue.level === 'hint');
  }

  /* 服务端 422 只回错误数组，提示仍来自当前草稿的 RTGraph.validate。 */
  mergeIssues(issues) {
    const hard = (Array.isArray(issues) ? issues : []).filter(issue => issue && issue.level !== 'hint');
    return hard.concat(this.draftHints());
  }

  /* 导入被拒后不能保留被拒文件的 Issue：重新校验当前草稿。 */
  restoreDraftIssues() {
    this.hooks.setIssues(this.store.tree ? RTGraph.validate(this.store.tree.root) : []);
    this.hooks.renderIssues();
  }

  defaultModelId() {
    const models = (this.hooks.getModels && this.hooks.getModels()) || [];
    if (models.length) return models[0].id;
    return 'qwen3-8b';
  }

  /* 新建与启动兜底共用：前端 UUID 切片 id，保存时 POST 成新组。 */
  newDraftTree() {
    const tree = RTGraph.createMinimalTree(this.defaultModelId());
    tree.id = newTreeId();
    tree.name = t('cv.default.name');
    return tree;
  }

  loadTree(raw, saved) {
    /* 真正换树（新建/启动兜底/selectTree 成功）时让在途的 selectTree 回包过期。
       示例与导入不走这里，它们在应用前各自 bump（见 loadExample / applyImport）。 */
    this.loadSeq += 1;
    const tree = normaliseTree(raw);
    this.treeId = raw && raw.id ? raw.id : newTreeId();
    tree.id = this.treeId;
    /* store.load 会同步通知装配层（attachGraph + renderAll），此时 treeId 已就绪。 */
    this.store.load(tree, { saved: !!saved });
    this.hooks.renderTreeSelect();
    return tree;
  }

  /* 保存成功的唯一基线：store.saved = 服务端回包的完整 envelope（§2.8）。 */
  applySavedResponse(result) {
    const savedTree = result && result.tree;
    if (!savedTree) return null;
    const tree = this.store.tree;
    if (savedTree.name != null) tree.name = savedTree.name; /* 服务端会 trim 名称 */
    tree.id = savedTree.id || this.treeId;
    tree.enabled = !!savedTree.enabled;
    tree.tree_revision = savedTree.tree_revision || '';
    tree.updated_at = savedTree.updated_at || '';
    tree.document_sha256 = savedTree.document_sha256 || '';
    this.treeId = tree.id;
    this.store.markSaved(savedTree);
    this.snapshotRevision = result.snapshot_revision != null
      ? result.snapshot_revision
      : this.snapshotRevision;
    this.upsertTree(savedTree);
    this.hooks.renderTreeSelect();
    return savedTree;
  }

  /* 保存 payload 只含完整 envelope 字段：不发送 enabled / tree_revision / updated_at。 */
  async save() {
    const store = this.store;
    if (!store.tree || store.saving || store.tryRunning) return false;
    store.setSaving(true);
    this.hooks.syncSaving();
    /* 请求发出前记住目标草稿：若期间草稿被切走，回包必须整份丢弃，
       不能把旧组的 id/name/revision/enabled 盖到另一张图上。 */
    const targetTree = store.tree;
    const targetId = this.treeId;
    const isNew = !store.saved;
    const payload = {
      id: this.treeId,
      name: String(store.tree.name == null ? '' : store.tree.name).trim(),
      timezone: store.tree.timezone || 'Asia/Shanghai',
      sticky: RTGraph.clone(store.tree.sticky || { enabled: false, ttl_seconds: 1800 }),
      root: RTGraph.clone(store.tree.root),
    };
    if (!isNew) payload.base_document_sha256 = store.saved.document_sha256 || '';
    try {
      const result = isNew
        ? await api.post('/admin/trees', payload)
        : await api.put('/admin/trees/' + encodeURIComponent(this.treeId), payload);
      if (store.tree !== targetTree || this.treeId !== targetId) return false;
      this.applySavedResponse(result);
      this.hooks.setIssues(RTGraph.validate(store.tree.root));
      /* 在检查器里还有未提交输入时不做整页重绘。 */
      if (this.hooks.isInspectorEditing()) this.hooks.renderIssues();
      else this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t('cv.toastSaved'));
      return true;
    } catch (error) {
      if (store.tree !== targetTree || this.treeId !== targetId) return false;
      if (error && error.status === 409 && error.detail && error.detail.code === 'graph_conflict') {
        this.hooks.toast(t('cv.toastSaveConflict'), 'error');
        return false;
      }
      const issues = serverIssues(error);
      if (issues) {
        this.hooks.setIssues(this.mergeIssues(issues));
        this.hooks.revealIssues();
        this.hooks.toast(tf('cv.toastSaveIssues', { n: issues.length }), 'error');
      } else {
        this.hooks.toast(tf('cv.toastSaveFail', { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  async enable() {
    const store = this.store;
    if (!store.tree || store.tryRunning || store.saving) return false;
    /* 启用前先落盘：保存失败（含 422 issue 数组）绝不发 enable。
       已启用的组若有 dirty 改动，也要先保存（点击启用 = “保存并启用”）。 */
    if (store.dirtyKind !== 'clean' || !store.saved) {
      const saved = await this.save();
      if (!saved) return false;
    }
    if (store.tree.enabled) {
      this.hooks.toast(t('cv.toastAlreadyEnabled'), 'warn');
      return true;
    }
    return this.setEnabled(true);
  }

  async disable() {
    const store = this.store;
    if (!store.tree || store.tryRunning || store.saving) return false;
    if (!store.tree.enabled) return true;
    return this.setEnabled(false);
  }

  async setEnabled(enabled) {
    const store = this.store;
    store.setSaving(true);
    this.hooks.syncSaving();
    const targetTree = store.tree;
    const targetId = this.treeId;
    const action = enabled ? 'enable' : 'disable';
    try {
      const result = await api.post('/admin/trees/' + encodeURIComponent(this.treeId) + '/' + action);
      if (store.tree !== targetTree || this.treeId !== targetId) return false;
      if (result && Array.isArray(result.trees)) this.trees = result.trees;
      if (result && result.snapshot_revision != null) this.snapshotRevision = result.snapshot_revision;
      store.tree.enabled = !!enabled;
      if (store.saved) store.saved.enabled = !!enabled;
      this.hooks.renderTreeSelect();
      /* 启用成功后重新校验当前草稿：服务端只回新 revision，Issue 条不能停在旧状态。 */
      this.hooks.setIssues(RTGraph.validate(store.tree.root));
      if (this.hooks.isInspectorEditing()) this.hooks.renderIssues();
      else this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t(enabled ? 'cv.toastEnabled' : 'cv.toastDisabled'));
      return true;
    } catch (error) {
      if (store.tree !== targetTree || this.treeId !== targetId) return false;
      const issues = serverIssues(error);
      if (issues) {
        this.hooks.setIssues(this.mergeIssues(issues));
        this.hooks.revealIssues();
        this.hooks.toast(tf(enabled ? 'cv.toastEnableIssues' : 'cv.toastDisableIssues',
          { n: issues.length }), 'error');
      } else {
        this.hooks.toast(tf(enabled ? 'cv.toastEnableFail' : 'cv.toastDisableFail',
          { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  async selectTree(id) {
    if (!id || id === this.treeId || this.store.tryRunning) return false;
    /* 保存回包还没落地就把草稿切走，会让“旧回包写新图”的竞态变得可达。 */
    if (this.store.saving) {
      this.hooks.toast(t('cv.toastSavingBlocked'), 'warn');
      return false;
    }
    if (this.store.dirtyKind !== 'clean') {
      const discard = await this.hooks.askDiscard('switch');
      if (!discard) return false;
    }
    const seq = ++this.loadSeq;
    try {
      const result = await api.get('/admin/trees/' + encodeURIComponent(id));
      /* 晚到的旧响应绝不能覆盖更新的选择；期间若已开始保存，也不换草稿，
         否则服务端已提交的回包会被保存的 identity 检查丢弃。 */
      if (seq !== this.loadSeq) return false;
      if (this.store.saving) {
        this.hooks.toast(t('cv.toastSavingBlocked'), 'warn');
        return false;
      }
      const tree = result && result.tree;
      if (!tree || !tree.root) throw new Error('invalid tree payload');
      this.loadTree(tree, true);
      return true;
    } catch (error) {
      /* 旧请求失败时已有更新的选择胜出：静默丢弃，不能再弹加载失败提示。 */
      if (seq !== this.loadSeq) return false;
      this.hooks.toast(tf('cv.toastLoadFail', { err: apiErrText(error) }), 'error');
      return false;
    }
  }

  async newTree() {
    if (this.store.dirtyKind !== 'clean') {
      const discard = await this.hooks.askDiscard('new');
      if (!discard) return false;
    }
    /* 确认通过才换树：取消“新建”不能把在途的 switch 判成过期（loadTree 内部 bump）。 */
    this.loadTree(this.newDraftTree(), false);
    this.hooks.toast(t('cv.toastNewTree'));
    return true;
  }

  async loadExample() {
    /* 载入示例总是确认：clean 草稿用“替换整张图”文案，dirty 才提示丢弃改动。 */
    const confirmed = await this.hooks.askDiscard('example',
      this.store.dirtyKind === 'clean' ? CONFIRM_LOAD_EXAMPLE : null);
    if (!confirmed) return false;
    try {
      const response = await fetch('routing/example.json', { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const example = await response.json();
      if (!example || example.type !== 'graph' ||
          !Array.isArray(example.nodes) || !Array.isArray(example.edges)) {
        throw new Error('invalid example graph');
      }
      /* 示例是整图替换（不走 loadTree）：应用前让在途的 switch 过期，
         否则旧回包会在示例落地后把草稿盖回去。 */
      this.loadSeq += 1;
      /* 一次历史操作替换整张图；envelope 的 id/enabled/name/timezone/sticky 保持不变。 */
      if (this.hooks.change('behavior', tree => { tree.root = RTGraph.clone(example); }) === false) {
        return false;
      }
      this.hooks.fitAfterReplace();
      this.hooks.toast(t('cv.toastExampleLoaded'));
      return true;
    } catch (error) {
      this.hooks.toast(tf('cv.toastLoadFail', {
        err: String(error && error.message != null ? error.message : error),
      }), 'error');
      return false;
    }
  }

  exportTree() {
    const tree = this.store.tree;
    if (!tree) return false;
    try {
      const text = RTGraph.serialize(tree);
      const blob = new Blob([text], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = String(this.treeId || tree.id || 'tree') + '.sr-routing-graph-v3.json';
      link.rel = 'noopener';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => {
        try { URL.revokeObjectURL(url); } catch (error) { /* ignore */ }
      }, 2000);
      this.hooks.toast(t('cv.toastExported'));
      return true;
    } catch (error) {
      this.hooks.toast(tf('cv.toastExportFail', {
        err: String(error && error.message != null ? error.message : error),
      }), 'error');
      return false;
    }
  }

  /* 导入只接受 v3：先校验 kind/version（固定拒绝文案），再做结构校验；
     任何失败都保留当前草稿，合法导入作为一次历史操作替换完整 envelope。 */
  applyImport(text) {
    const result = RTGraph.parseImport(text);
    if (!result) {
      this.restoreDraftIssues();
      this.hooks.toast(tf('cv.toastImportRejected', { err: 'invalid import' }), 'error');
      return false;
    }
    const issues = Array.isArray(result.issues) ? result.issues : [];
    const hard = issues.filter(issue => issue.level !== 'hint');
    if (!result.ok) {
      this.restoreDraftIssues();
      if (hard.length) {
        this.hooks.toast(tf('cv.toastImportIssues', { n: hard.length }), 'error');
      } else {
        const reason = result.error || 'invalid import';
        this.hooks.toast(tf('cv.toastImportRejected', { err: reason }), 'error');
      }
      return false;
    }
    if (hard.length) {
      this.restoreDraftIssues();
      this.hooks.toast(tf('cv.toastImportIssues', { n: hard.length }), 'error');
      return false;
    }
    /* 导入同样整图替换（不走 loadTree）：应用前让在途的 switch 过期。 */
    this.loadSeq += 1;
    if (this.hooks.change('behavior', tree => {
      tree.name = result.tree.name;
      tree.timezone = result.tree.timezone;
      tree.sticky = RTGraph.clone(result.tree.sticky);
      tree.root = RTGraph.clone(result.tree.root);
    }) === false) return false;
    this.hooks.fitAfterReplace();
    this.hooks.toast(t('cv.toastImported'));
    return true;
  }

  async openImport() {
    if (this.store.dirtyKind !== 'clean') {
      const discard = await this.hooks.askDiscard('import');
      if (!discard) return false;
    }
    const file = document.getElementById('cvImportFile');
    if (!file) return false;
    file.value = '';
    file.click();
    return true;
  }

  async onImportFile(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    let text = '';
    try {
      text = await file.text();
    } catch (error) {
      this.hooks.toast(tf('cv.toastImportRejected', {
        err: String(error && error.message != null ? error.message : error),
      }), 'error');
      return;
    }
    this.applyImport(text);
  }
}

/* 载入示例总是确认；clean 草稿没有“丢弃”语义，用只描述替换整张图的文案。 */
const CONFIRM_LOAD_EXAMPLE = {
  title: 'cv.confirmLoadExample',
  body: 'cv.confirmLoadExampleClean',
  action: 'cv.confirmLoad',
};
