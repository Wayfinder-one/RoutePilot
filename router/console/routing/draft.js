/* DraftStore · Policy v4 草稿与撤销历史的唯一所有者（Task 8 重写）
   ------------------------------------------------------------------
   纯内存状态：不读 DOM、不发请求、不碰 localStorage。草稿是
   {resource, policy, layout} 三联：
     · resource 只保存 id/enabled/policy_revision/updated_at/document_sha256
       这类服务端元数据视图（policy 单独持有，不重复存一份）；
     · policy 是可编辑的完整 v4 文档；
     · layout 是纯 UI 数据（折叠状态、面板宽度等），绝不写回 policy。
   所有写入都走 change(kind, mutate)：先克隆 before，再执行 mutate，
   再克隆 after 入栈；历史上限 100，任何新编辑清空 redo。undo/redo 在
   运行锁 / 保存锁下拒绝，且不回滚服务端元数据。
   双基线：保存策略 markSaved(resource) 只替换 policy+resource 基线，
   保存布局 markLayoutSaved(layout) 只替换 layout 基线，互不清除 dirty。
   dirty 分类：behavior（policy 行为差异，behaviorCanonical 键序无关、
   数组有序）/ layout（UI 数据差异）/ clean；没有 display 类——name 不进
   行为 canonical，属于文档内可保存字段但不是行为差异。
   ------------------------------------------------------------------ */
import { behaviorCanonical, clone } from './policy.js';

export const HISTORY_LIMIT = 100;

/* layout 是前端私有的任意 JSON：比较用稳定键序序列化（键序无关）。 */
function stableJson(value) {
  if (Array.isArray(value)) return '[' + value.map(stableJson).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort()
      .map(key => JSON.stringify(key) + ':' + stableJson(value[key]))
      .join(',') + '}';
  }
  return JSON.stringify(value === undefined ? null : value);
}

function layoutChanged(a, b) {
  return stableJson(a || {}) !== stableJson(b || {});
}

/* 服务端 resource → 草稿内持有的元数据视图（不复制 policy 本体）。 */
function resourceMeta(resource) {
  const value = resource || {};
  return {
    id: value.id == null ? '' : value.id,
    enabled: value.enabled === true,
    policy_revision: value.policy_revision == null ? '' : value.policy_revision,
    updated_at: value.updated_at == null ? '' : value.updated_at,
    document_sha256: value.document_sha256 == null ? '' : value.document_sha256,
  };
}

export class DraftStore {
  constructor() {
    this.draft = null; /* {resource, policy, layout} */
    this.saved = null; /* {resource, policy, layout} 深拷贝基线 */
    this.tryRunning = false;
    this.saving = false;
    this.undoStack = [];
    this.redoStack = [];
    this._dirtyKind = 'clean';
    this._listeners = new Set();
  }

  get policy() { return this.draft ? this.draft.policy : null; }
  get layout() { return this.draft ? this.draft.layout : null; }
  get resource() { return this.draft ? this.draft.resource : null; }

  get dirtyKind() {
    return this._dirtyKind;
  }

  get canUndo() {
    return this.undoStack.length > 0;
  }

  get canRedo() {
    return this.redoStack.length > 0;
  }

  /* 订阅状态变化；返回取消订阅函数。 */
  subscribe(listener) {
    if (typeof listener !== 'function') return () => {};
    this._listeners.add(listener);
    return () => { this._listeners.delete(listener); };
  }

  _refreshDirty() {
    if (!this.draft) this._dirtyKind = 'clean';
    else if (!this.saved || this.saved.policy == null) this._dirtyKind = 'behavior';
    else if (behaviorCanonical(this.draft.policy) !== behaviorCanonical(this.saved.policy)) {
      this._dirtyKind = 'behavior';
    } else if (layoutChanged(this.draft.layout, this.saved.layout)) this._dirtyKind = 'layout';
    else this._dirtyKind = 'clean';
    return this._dirtyKind;
  }

  notify(event) {
    this._refreshDirty();
    const payload = Object.assign({ dirtyKind: this._dirtyKind }, event || {});
    this._listeners.forEach(listener => {
      try { listener(payload); } catch (error) { /* 一个订阅者失败不能中断其他渲染 */ }
    });
  }

  /* 整体装载草稿；saved=true 时同时把当前内容作为保存基线。 */
  load(resource, layout, options) {
    const opts = options || {};
    const policy = resource && resource.policy ? clone(resource.policy) : null;
    this.draft = {
      resource: resourceMeta(resource),
      policy: policy,
      layout: clone(layout || {}),
    };
    this.saved = opts.saved && policy ? {
      resource: clone(resource),
      policy: clone(policy),
      layout: clone(layout || {}),
    } : null;
    this.undoStack.length = 0;
    this.redoStack.length = 0;
    this.notify({ type: 'load' });
    return this.draft;
  }

  /* 唯一写入路径。返回 false 表示被锁 / 没有草稿 / 调用方应放弃本次编辑。 */
  change(kind, mutate) {
    if (!this.draft || this.tryRunning || this.saving) return false;
    const before = clone(this.draft);
    mutate(this.draft);
    this.undoStack.push({ before: before, after: clone(this.draft), kind: kind || 'behavior' });
    if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
    this.redoStack.length = 0;
    this.notify({ type: 'change', kind: kind || 'behavior' });
    return true;
  }

  undo() {
    if (!this.draft || this.tryRunning || this.saving) return false;
    const entry = this.undoStack.pop();
    if (!entry) return false;
    this.redoStack.push(entry);
    this.draft = clone(entry.before);
    this._syncResourceMeta();
    this.notify({ type: 'undo', kind: entry.kind });
    return true;
  }

  redo() {
    if (!this.draft || this.tryRunning || this.saving) return false;
    const entry = this.redoStack.pop();
    if (!entry) return false;
    this.undoStack.push(entry);
    this.draft = clone(entry.after);
    this._syncResourceMeta();
    this.notify({ type: 'redo', kind: entry.kind });
    return true;
  }

  /* 服务端元数据不属于编辑历史：undo/redo 之后从基线重新对齐。 */
  _syncResourceMeta() {
    if (this.draft && this.saved && this.saved.resource) {
      this.draft.resource = resourceMeta(this.saved.resource);
    }
  }

  /* 保存策略成功的唯一基线：服务端完整 resource 整体替换 policy+resource
     基线（不逐字段合并）；layout 基线原样保留，不清除布局 dirty。 */
  markSaved(serverResource) {
    if (!serverResource || !serverResource.policy) return false;
    const layoutBaseline = this.saved ? this.saved.layout
      : clone(this.draft ? this.draft.layout : {});
    this.saved = {
      resource: clone(serverResource),
      policy: clone(serverResource.policy),
      layout: layoutBaseline,
    };
    if (this.draft) this.draft.resource = resourceMeta(serverResource);
    this.notify({ type: 'saved' });
    return true;
  }

  /* 布局保存成功：只替换 layout 基线，绝不触碰策略哈希/revision，
     也不清除行为 dirty。 */
  markLayoutSaved(serverLayout) {
    if (!serverLayout || typeof serverLayout !== 'object') return false;
    const base = this.saved;
    this.saved = {
      resource: base ? base.resource : null,
      policy: base ? base.policy : null,
      layout: clone(serverLayout),
    };
    this.notify({ type: 'layoutSaved' });
    return true;
  }

  setTryRunning(value) {
    this.tryRunning = !!value;
  }

  setSaving(value) {
    this.saving = !!value;
  }
}
