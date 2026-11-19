/* DraftStore · 决策组草稿与撤销历史的唯一所有者
   ------------------------------------------------------------------
   纯内存状态：不读 DOM、不发请求、不碰 localStorage。所有写入都走
   change(kind, mutate)：先克隆 before，再执行 mutate，再克隆 after 入栈；
   历史上限 100，任何新编辑清空 redo。undo/redo 在运行锁 / 保存锁下拒绝。
   dirty 分类复用 graph.js 的 changeKind（behavior / display / layout / clean）。
   ------------------------------------------------------------------ */
import { changeKind, clone } from './graph.js';

export const HISTORY_LIMIT = 100;

export class DraftStore {
  constructor() {
    this.tree = null;
    this.saved = null;
    this.tryRunning = false;
    this.saving = false;
    this.undoStack = [];
    this.redoStack = [];
    this._dirtyKind = 'clean';
    this._listeners = new Set();
  }

  get graph() {
    return this.tree ? this.tree.root : null;
  }

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
    if (!this.tree) this._dirtyKind = 'clean';
    else if (!this.saved) this._dirtyKind = 'behavior';
    else this._dirtyKind = changeKind(this.saved, this.tree);
    return this._dirtyKind;
  }

  notify(event) {
    this._refreshDirty();
    const payload = Object.assign({ dirtyKind: this._dirtyKind }, event || {});
    this._listeners.forEach(listener => {
      try { listener(payload); } catch (error) { /* 一个订阅者失败不能中断其他渲染 */ }
    });
  }

  /* 整体装载草稿；saved=true 时同时把当前草稿作为保存基线。 */
  load(tree, options) {
    const opts = options || {};
    this.tree = tree || null;
    this.saved = opts.saved && tree ? clone(tree) : null;
    this.undoStack.length = 0;
    this.redoStack.length = 0;
    this.notify({ type: 'load' });
    return this.tree;
  }

  /* 唯一写入路径。返回 false 表示被锁 / 没有草稿 / 调用方应放弃本次编辑。 */
  change(kind, mutate) {
    if (!this.tree || this.tryRunning || this.saving) return false;
    const before = clone(this.tree);
    mutate(this.tree);
    this.undoStack.push({ before: before, after: clone(this.tree), kind: kind || 'behavior' });
    if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
    this.redoStack.length = 0;
    this.notify({ type: 'change', kind: kind || 'behavior' });
    return true;
  }

  undo() {
    if (!this.tree || this.tryRunning || this.saving) return false;
    const entry = this.undoStack.pop();
    if (!entry) return false;
    this.redoStack.push(entry);
    this.tree = clone(entry.before);
    this.notify({ type: 'undo', kind: entry.kind });
    return true;
  }

  redo() {
    if (!this.tree || this.tryRunning || this.saving) return false;
    const entry = this.redoStack.pop();
    if (!entry) return false;
    this.undoStack.push(entry);
    this.tree = clone(entry.after);
    this.notify({ type: 'redo', kind: entry.kind });
    return true;
  }

  /* 服务端回包是唯一保存基线：整体替换，不逐字段合并。 */
  markSaved(serverTree) {
    if (!serverTree) return false;
    this.saved = clone(serverTree);
    this.notify({ type: 'saved' });
    return true;
  }

  setTryRunning(value) {
    this.tryRunning = !!value;
  }

  setSaving(value) {
    this.saving = !!value;
  }
}
