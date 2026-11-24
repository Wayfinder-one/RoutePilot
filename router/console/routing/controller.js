/* RoutingController · 决策组管理面编排
   ------------------------------------------------------------------
   唯一职责：保存 / 启用 / 停用 / 切换 / 导入导出这些管理面 API 的编排。
   · 唯一 saving 状态：controller 写 store.setSaving()，Store 据此拒绝编辑；
   · 保存请求发出前记录 targetTree / targetId，回包时用 tree identity 拒绝过期响应；
   · 启用 dirty 草稿时先保存（保存失败绝不再发 enable）；
   · 不直接改 DOM：渲染/提示通过 hooks 回调交给 app.js。
   ------------------------------------------------------------------ */
import { api, apiErrText } from '../api.js';
import { LANG, t, tf } from '../i18n.js';
import * as RTGraph from './graph.js';
import { createRuntime } from './runtime.js';

function serverIssues(error) {
  const detail = error && error.detail;
  if (!Array.isArray(detail)) return null;
  const issues = detail
    .filter(item => item && typeof item === 'object' && !Array.isArray(item))
    .map(item => Object.assign({ level: 'hard' }, item));
  return issues.length ? issues : null;
}

/* 导入失败的具体原因：解析/运行配置契约错误没有节点位置，toast 只报
   「N 个校验问题」会把真正的原因（缺字段、runtime:null、未知字段…）藏起来。
   优先取首条 graph_parse_error（或首条硬 Issue）的 zh/en，其次退回 parse 结果
   的 error 文案；都为空时返回空串，由调用方兜底。 */
function importFailureReason(result, hard) {
  const localized = issue => {
    if (!issue) return '';
    if (LANG === 'en') return issue.en || issue.zh || '';
    return issue.zh || issue.en || '';
  };
  const parseIssue = hard.find(issue => issue && issue.code === 'graph_parse_error');
  const first = parseIssue || hard[0] || null;
  const reason = localized(first) || (result && result.error) || '';
  return String(reason).trim();
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

/* 服务端树 / 本地新草稿统一补齐 envelope 字段与缺失坐标。
   严格契约：envelope 必须携带完整四字段运行快照（API envelope 里是扁平四字段），
   缺失即拒绝加载，不再有「无 runtime 草稿」分支。 */
function normaliseTree(raw) {
  const source = raw || {};
  /* API envelope 的坐标在顶层 canvas：先分发回内部 node.position 再做布局补位。 */
  const tree = RTGraph.dispatchCanvas(RTGraph.clone(source));
  delete tree.canvas;
  /* 运行配置在内部树里统一成嵌套 runtime：Python tree_to_dict 的四字段是扁平的，
     先投影成契约形状再剔除顶层键。 */
  const runtime = RTGraph.runtimeDocumentView(source);
  if (!runtime) throw new Error('tree envelope is missing the four runtime fields');
  RTGraph.RUNTIME_FIELDS.forEach(field => { delete tree[field]; });
  tree.runtime = runtime;
  /* v4 envelope：部门规范化（重复/空部门/控制字符在这里就拒绝），并保留服务端
     返回的 default（生命周期由启停端点维护，普通保存不写）。 */
  tree.departments = RTGraph.normalizeDepartments(tree.departments);
  tree.default = source.default === true;
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
       getCatalog() -> {providers, models, services, periods}    */
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

  /* 新建与启动兜底共用：前端 UUID 切片 id，保存时 POST 成新组。
     新画布的初始运行快照必须从当前数据库目录（createRuntime）生成；目录为空或
     缺少 provider 时抛明确错误，由 app.js 提示并阻止新建（不再造空 runtime）。 */
  catalogNow() {
    return (this.hooks.getCatalog && this.hooks.getCatalog()) || null;
  }

  newDraftTree() {
    const catalog = this.catalogNow();
    const models = catalog && Array.isArray(catalog.models) ? catalog.models : [];
    if (!models.length) {
      throw new Error(t('cv.toastCatalogEmpty'));
    }
    const services = catalog && Array.isArray(catalog.services) ? catalog.services : [];
    const runtime = createRuntime(catalog, {
      modelId: models[0].id,
      jevId: services.length ? services[0].id : null
    });
    const tree = RTGraph.createMinimalTree(runtime.backends.models[0].id, runtime);
    tree.id = newTreeId();
    tree.name = t('cv.default.name');
    return tree;
  }

  loadTree(raw, saved) {
    /* 真正换树（新建/启动兜底/selectTree 成功）时让在途的 selectTree 回包过期。
       示例已移除；导入不走这里，它在应用前自己 bump loadSeq（见 applyImport）。 */
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
    tree.default = !!savedTree.default;
    tree.departments = RTGraph.normalizeDepartments(savedTree.departments);
    tree.tree_revision = savedTree.tree_revision || '';
    tree.updated_at = savedTree.updated_at || '';
    tree.document_sha256 = savedTree.document_sha256 || '';
    /* 服务端回包的完整运行快照就是新基线：不同步回当前草稿的话，
       保存后的草稿会立刻被判成 behavior dirty。 */
    tree.runtime = RTGraph.runtimeDocumentView(savedTree);
    this.treeId = tree.id;
    /* 回包是保存基线：坐标同样在顶层 canvas，先分发回内部 position 再入基线，
       否则 clean 草稿会被判成 layout dirty。 */
    this.store.markSaved(RTGraph.dispatchCanvas(RTGraph.clone(savedTree)));
    this.snapshotRevision = result.snapshot_revision != null
      ? result.snapshot_revision
      : this.snapshotRevision;
    this.upsertTree(savedTree);
    this.hooks.renderTreeSelect();
    return savedTree;
  }

  /* 生命周期回包刷新当前草稿与保存基线（Task 8）：v4 的 enabled/default 不参与
     behavior canonical，启停/设默认也不会改变 tree_revision；它们只改写目标文档
     字节，因此 document_sha256 与 updated_at 会变。不同步回保存基线，下一次
     普通保存会拿旧 base_document_sha256 被 409 graph_conflict 拒绝。
     多图语义必须逐字段取服务端值：第一张启用图自动默认，之后启用的图是
     enabled && !default —— 绝不能写 `default = enabled`。

     departments 例外（Task 9）：启停端点从不修改部门，服务端回包里的 departments
     只是「上一次保存的基线」。草稿有未保存的部门编辑时（dirtyKind !== 'clean'）
     把它同步回草稿会静默丢弃用户的部门输入。因此这里只在草稿干净时同步
     departments；脏草稿保留本地编辑，saved 基线也保持原值，下一次保存会带着
     本地部门一起提交（enable/setDefault 已经先保存脏草稿，走到这里时通常是 clean）。 */
  syncLifecycleBaseline(updated) {
    const tree = this.store.tree;
    if (!tree || !updated) return;
    const keepDirtyDepartments = this.store.dirtyKind !== 'clean';
    for (const field of ['enabled', 'default', 'tree_revision', 'updated_at',
      'document_sha256', 'departments']) {
      if (field === 'departments' && keepDirtyDepartments) continue;
      const value = field === 'enabled' || field === 'default'
        ? updated[field] === true
        : field === 'departments'
          ? RTGraph.normalizeDepartments(updated.departments)
          : (updated[field] || '');
      tree[field] = RTGraph.clone(value);
      if (this.store.saved) this.store.saved[field] = RTGraph.clone(value);
    }
  }

  /* 保存 payload 只含完整 envelope 字段：不发送 enabled / tree_revision / updated_at。 */
  async save() {
    const store = this.store;
    if (!store.tree || store.saving || store.tryRunning) return false;
    /* 保存前本地硬校验运行配置：四字段缺失/非法一律拒绝（服务端同样 422），
       不再发送 runtime:null 让服务端从数据库补齐。 */
    const runtimeIssues = RTGraph.validateRuntime(store.tree)
      .filter(issue => issue.level !== 'hint');
    if (runtimeIssues.length) {
      this.hooks.setIssues(this.mergeIssues(runtimeIssues));
      this.hooks.revealIssues();
      this.hooks.toast(tf('cv.toastSaveIssues', { n: runtimeIssues.length }), 'error');
      return false;
    }
    store.setSaving(true);
    this.hooks.syncSaving();
    /* 请求发出前记住目标草稿：若期间草稿被切走，回包必须整份丢弃，
       不能把旧组的 id/name/revision/enabled 盖到另一张图上。 */
    const targetTree = store.tree;
    const targetId = this.treeId;
    const isNew = !store.saved;
    /* 线上 payload 与文件文档同一格式：坐标在顶层 canvas，节点对象不带 position；
       自包含运行配置按四字段无条件平铺（jev 可为 null，但字段恒在）。 */
    const payload = RTGraph.collectCanvas(RTGraph.clone({
      id: this.treeId,
      name: String(store.tree.name == null ? '' : store.tree.name).trim(),
      departments: RTGraph.normalizeDepartments(store.tree.departments),
      timezone: store.tree.timezone || 'Asia/Shanghai',
      sticky: RTGraph.clone(store.tree.sticky || { enabled: false, ttl_seconds: 1800 }),
      root: store.tree.root,
    }));
    Object.assign(payload, RTGraph.runtimeDocumentView(store.tree));
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

  /* 默认图停用保护（设计 §6.2/§8）：默认图仍需保留其它启用图时必须先切换默认图。
     this.trees 来自 GET /admin/trees 与生命周期回包；不在前端猜测，而是拦下一次
     注定被服务端以 409 default_graph_required 拒绝的写请求。 */
  hasOtherEnabledTree(treeId) {
    const target = treeId || this.treeId;
    return (this.trees || []).some(tree =>
      !!tree && tree.enabled === true && tree.id !== target);
  }

  async disable() {
    const store = this.store;
    if (!store.tree || store.tryRunning || store.saving) return false;
    if (!store.tree.enabled) return true;
    /* 停用默认图但仍有其它启用图：服务端会 409，先提示切换默认图，不发请求。 */
    if (store.tree.default && this.hasOtherEnabledTree(this.treeId)) {
      this.hooks.toast(t('cv.toastDisableDefaultBlocked'), 'warn');
      return false;
    }
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
      /* v4：启停只维护目标图的 enabled/default（多图不互斥，第一张启用图自动
         默认）。生命周期以服务端回包为准 —— 后续启用的图是 enabled && !default，
         写 `default = enabled` 会让控制台把自己误标成默认图。 */
      const updated = (Array.isArray(result && result.trees) ? result.trees : [])
        .find(item => item && item.id === targetId);
      if (!updated) throw new Error('lifecycle response is missing the current tree');
      this.syncLifecycleBaseline(updated);
      if (result && result.snapshot_revision != null) this.snapshotRevision = result.snapshot_revision;
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
      /* 服务端竞态：页面里的启用图列表可能已过期（另一会话刚启用/切换了其它图），
         停用默认图仍会被 409 default_graph_required 拒绝。此时服务器状态未变，
         给与前端拦截相同的「先切换默认图」提示，绝不把结构化 detail 当原始 JSON
         塞进泛化失败文案。 */
      if (!enabled && error && error.detail && error.detail.code === 'default_graph_required') {
        this.hooks.toast(t('cv.toastDisableDefaultBlocked'), 'warn');
        return false;
      }
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

  /* 设为默认图（Task 8）：保存脏草稿 → POST /default → 用回包刷新完整 envelope。
     生命周期写改了文档字节，必须刷新保存基线，否则紧接着的普通保存会 409
     graph_conflict（brief 的真实浏览器场景会立即再保存一次）。 */
  async setDefault() {
    const store = this.store;
    if (!store.tree || store.saving || store.tryRunning) return false;
    if (store.dirtyKind !== 'clean' || !store.saved) {
      const saved = await this.save();
      if (!saved) return false;
    }
    store.setSaving(true);
    this.hooks.syncSaving();
    const targetTree = store.tree;
    const targetId = this.treeId;
    try {
      const result = await api.post(
        '/admin/trees/' + encodeURIComponent(targetId) + '/default');
      if (store.tree !== targetTree || this.treeId !== targetId) return false;
      if (Array.isArray(result.trees)) this.trees = result.trees;
      const current = this.trees.find(item => item.id === targetId);
      if (!current) throw new Error('default response is missing the current tree');
      this.applySavedResponse({
        tree: current,
        snapshot_revision: result.snapshot_revision,
      });
      this.hooks.renderTreeSelect();
      this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t('cv.toastDefault'));
      return true;
    } catch (error) {
      this.hooks.toast(tf('cv.toastDefaultFail', { err: apiErrText(error) }), 'error');
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
    let tree;
    try {
      tree = this.newDraftTree();
    } catch (error) {
      this.hooks.toast(String(error && error.message ? error.message : error), 'error');
      return false;
    }
    this.loadTree(tree, false);
    this.hooks.toast(t('cv.toastNewTree'));
    return true;
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
      link.download = String(this.treeId || tree.id || 'tree') + '.sr-routing-graph-v4.json';
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

  /* 导入只接受 v4：先校验 kind/version（固定拒绝文案），再做结构校验；
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
      /* 解析级失败（无节点位置）必须把具体原因交给用户：只报问题数量会让人
         无从下手，且被拒文件的 Issue 已被恢复成当前草稿的 Issue。 */
      const reason = importFailureReason(result, hard) || 'invalid import';
      this.hooks.toast(tf('cv.toastImportRejected', { err: reason }), 'error');
      return false;
    }
    if (hard.length) {
      this.restoreDraftIssues();
      this.hooks.toast(tf('cv.toastImportIssues', { n: hard.length }), 'error');
      return false;
    }
    /* 导入同样整图替换（不走 loadTree）：应用前让在途的 switch 过期。
       运行配置也在这一次历史里整体替换 —— 导入文档必须自带完整四字段快照，
       撤销能恢复载入前的 runtime。 */
    this.loadSeq += 1;
    if (this.hooks.change('behavior', tree => {
      tree.name = result.tree.name;
      tree.departments = RTGraph.clone(result.tree.departments);
      tree.timezone = result.tree.timezone;
      tree.sticky = RTGraph.clone(result.tree.sticky);
      tree.root = RTGraph.clone(result.tree.root);
      tree.runtime = RTGraph.clone(result.tree.runtime);
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
