/* RoutingController · Policy v4 管理面编排（Task 8 重写）
   ------------------------------------------------------------------
   唯一职责：/admin/policies 资源的保存 / 启停 / 切换 / 删除 / 载入示例
   这些管理面 API 的编排。
   · 唯一 saving 状态：controller 写 store.setSaving()，Store 据此拒绝编辑；
   · 请求发出前记录 targetDraft / targetId，回包时用 identity 检查丢弃过期
     响应——旧回包绝不能把别的策略元数据盖到当前草稿上；
   · 已有资源保存永远带 If-Match（保存基线的 document_sha256）；新建走
     POST {id, policy}；成功后用服务端完整 resource 整体替换基线，不逐字
     段合并；409 冲突不覆盖本地草稿；
   · 启用 dirty 草稿先保存（保存失败绝不再发 enable）；
   · 布局保存只 PUT /layout，不带策略哈希，也不更新策略元数据；
   · 不直接改 DOM：渲染/提示通过 hooks 回调交给 app.js。
   ------------------------------------------------------------------ */
import { api, apiErrText } from '../api.js';
import { t, tf } from '../i18n.js';
import { clone, createPolicy } from './policy.js';

function serverIssues(error) {
  const detail = error && error.detail;
  if (!Array.isArray(detail)) return null;
  const issues = detail
    .filter(item => item && typeof item === 'object' && !Array.isArray(item))
    .map(item => Object.assign({ level: 'hard' }, item));
  return issues.length ? issues : null;
}

function newPolicyId() {
  let raw = '';
  try {
    raw = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : '';
  } catch (error) { raw = ''; }
  if (!raw) raw = Date.now().toString(16) + Math.random().toString(16).slice(2);
  return raw.replace(/[^a-z0-9]/gi, '').slice(0, 12);
}

export class RoutingController {
  /* hooks:
       toast(message, tone)
       askDiscard(reason) -> Promise<boolean>
       askDelete(policyId) -> Promise<boolean>
       render() / renderIssues() / revealIssues() / refreshDirty() / syncSaving()
       setIssues(issues)
       renderPolicySelect()
       getModels() -> []                                        */
  constructor(options) {
    const opts = options || {};
    this.store = opts.store;
    this.hooks = opts.hooks || {};
    this.policies = [];
    this.policyId = '';
    this.snapshotRevision = '';
    /* 单调递增的装载序号：并发 selectPolicy 只允许最后一次请求写进 store。 */
    this.loadSeq = 0;
  }

  get saved() {
    return this.store.saved;
  }

  upsertPolicy(resource) {
    if (!resource) return;
    const list = this.policies || [];
    const index = list.findIndex(item => item && item.id === resource.id);
    if (index >= 0) list[index] = clone(resource);
    else list.push(clone(resource));
    this.policies = list;
  }

  defaultModelId() {
    const models = (this.hooks.getModels && this.hooks.getModels()) || [];
    if (models.length) return models[0].id;
    return 'qwen3-8b';
  }

  /* 新建与启动兜底共用：前端 UUID 切片 id，保存时 POST 成新资源。 */
  newDraftResource() {
    return {
      id: newPolicyId(), enabled: false,
      policy_revision: '', updated_at: '', document_sha256: '',
      policy: createPolicy(this.defaultModelId()),
    };
  }

  /* 装载一个服务端 resource（或本地新资源）；saved=true 时同时落基线。 */
  loadPolicy(resource, layout, saved) {
    /* 真正换策略（新建/启动兜底/selectPolicy 成功）时让在途请求过期。 */
    this.loadSeq += 1;
    this.policyId = resource && resource.id ? resource.id : newPolicyId();
    this.store.load(resource, layout, { saved: !!saved });
    this.hooks.renderPolicySelect();
    return this.store.policy;
  }

  /* 本地新草稿（不经确认）：envelope 元数据全部为空，保存即 POST。 */
  loadNewDraft() {
    return this.loadPolicy(this.newDraftResource(), {}, false);
  }

  /* UI 入口「新建」：dirty 时先确认丢弃。 */
  async newPolicy() {
    if (this.store.dirtyKind !== 'clean') {
      const discard = await this.hooks.askDiscard('new');
      if (!discard) return false;
    }
    this.loadNewDraft();
    this.hooks.toast(t('pv.toastNewPolicy'));
    return true;
  }

  /* 保存成功（POST/PUT）的唯一出口：服务端完整 resource 整体替换基线。 */
  applySavedResource(resource) {
    if (!resource || !resource.policy) return null;
    this.policyId = resource.id || this.policyId;
    this.store.markSaved(resource);
    this.upsertPolicy(resource);
    this.hooks.renderPolicySelect();
    return resource;
  }

  /* 保存 payload：新建 POST {id, policy}；已有资源 PUT 直接是完整 v4 文档
     （无信封），If-Match 携带保存基线的 document_sha256。 */
  async savePolicy() {
    const store = this.store;
    if (!store.policy || store.saving || store.tryRunning) return false;
    store.setSaving(true);
    this.hooks.syncSaving();
    /* 请求发出前记住目标草稿：若期间草稿被切走，回包必须整份丢弃。 */
    const targetDraft = store.draft;
    const targetId = this.policyId;
    const isNew = !store.saved;
    try {
      let resource = null;
      if (isNew) {
        const result = await api.post('/admin/policies', {
          id: this.policyId,
          policy: clone(store.policy),
        });
        resource = result && result.policy_resource;
      } else {
        const result = await api.put(
          '/admin/policies/' + encodeURIComponent(this.policyId),
          clone(this.store.policy),
          { headers: { 'If-Match': this.store.saved.resource.document_sha256 } }
        );
        resource = result && result.policy_resource;
      }
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      if (!resource || !resource.policy) throw new Error('invalid policy resource payload');
      this.applySavedResource(resource);
      /* 在编辑器里还有未提交输入时不做整页重绘。 */
      if (this.hooks.isEditorEditing && this.hooks.isEditorEditing()) this.hooks.renderIssues();
      else this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t('pv.toastSaved'));
      return true;
    } catch (error) {
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      /* 409：本地草稿与基线都不动——绝不拿服务端哈希覆盖未保存编辑。
         If-Match 冲突用专门文案；重复 id 等其它 409 仍展示服务端文本。 */
      if (error && error.status === 409) {
        if (error.detail && error.detail.code === 'policy_conflict') {
          this.hooks.toast(t('pv.toastSaveConflict'), 'error');
        } else {
          this.hooks.toast(tf('pv.toastSaveFail', { err: apiErrText(error) }), 'error');
        }
        return false;
      }
      const issues = serverIssues(error);
      if (issues) {
        this.hooks.setIssues(issues);
        this.hooks.revealIssues();
        this.hooks.toast(tf('pv.toastSaveIssues', { n: issues.length }), 'error');
      } else {
        this.hooks.toast(tf('pv.toastSaveFail', { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  /* 布局独立保存：只 PUT /layout，不发策略哈希，也不更新策略元数据。 */
  async saveLayout() {
    const store = this.store;
    if (!store.layout || !store.saved || store.saving || store.tryRunning) return false;
    store.setSaving(true);
    this.hooks.syncSaving();
    const targetDraft = store.draft;
    const targetId = this.policyId;
    try {
      const savedLayout = await api.put(
        '/admin/policies/' + encodeURIComponent(this.policyId) + '/layout',
        clone(store.layout)
      );
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      store.markLayoutSaved(savedLayout && typeof savedLayout === 'object' ? savedLayout : store.layout);
      this.hooks.refreshDirty();
      this.hooks.toast(t('pv.toastLayoutSaved'));
      return true;
    } catch (error) {
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      const issues = serverIssues(error);
      if (issues) {
        this.hooks.setIssues(issues);
        this.hooks.revealIssues();
        this.hooks.toast(tf('pv.toastLayoutIssues', { n: issues.length }), 'error');
      } else {
        this.hooks.toast(tf('pv.toastSaveFail', { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  async enable() {
    const store = this.store;
    if (!store.policy || store.tryRunning || store.saving) return false;
    /* 启用前先落盘：保存失败（含 422 issue 数组）绝不发 enable。
       已启用的策略若有 dirty 改动，也要先保存（点击启用 = “保存并启用”）。 */
    if (store.dirtyKind !== 'clean' || !store.saved) {
      const saved = await this.savePolicy();
      if (!saved) return false;
    }
    if (store.resource && store.resource.enabled) {
      this.hooks.toast(t('pv.toastAlreadyEnabled'), 'warn');
      return true;
    }
    return this.setEnabled(true);
  }

  async disable() {
    const store = this.store;
    if (!store.policy || store.tryRunning || store.saving) return false;
    if (!(store.resource && store.resource.enabled)) return true;
    return this.setEnabled(false);
  }

  async setEnabled(enabled) {
    const store = this.store;
    store.setSaving(true);
    this.hooks.syncSaving();
    const targetDraft = store.draft;
    const targetId = this.policyId;
    const action = enabled ? 'enable' : 'disable';
    try {
      const result = await api.post(
        '/admin/policies/' + encodeURIComponent(this.policyId) + '/' + action);
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      if (result && Array.isArray(result.policies)) this.policies = result.policies;
      if (result && result.snapshot_revision != null) this.snapshotRevision = result.snapshot_revision;
      /* 启停只改元数据视图：草稿与基线的 enabled 同步置位，不走历史。 */
      if (store.draft) store.draft.resource.enabled = !!enabled;
      if (store.saved && store.saved.resource) store.saved.resource.enabled = !!enabled;
      this.hooks.renderPolicySelect();
      /* 启用成功后清掉保存时的 422 列表：Issue 条不能停在旧状态。 */
      this.hooks.setIssues([]);
      if (this.hooks.isEditorEditing && this.hooks.isEditorEditing()) this.hooks.renderIssues();
      else this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t(enabled ? 'pv.toastEnabled' : 'pv.toastDisabled'));
      return true;
    } catch (error) {
      if (store.draft !== targetDraft || this.policyId !== targetId) return false;
      const issues = serverIssues(error);
      if (issues) {
        this.hooks.setIssues(issues);
        this.hooks.revealIssues();
        this.hooks.toast(tf('pv.toastEnableIssues', { n: issues.length }), 'error');
      } else {
        this.hooks.toast(tf('pv.toastEnableFail', { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  /* 切换策略：dirty 先确认丢弃；晚到的旧响应绝不能覆盖更新的选择。 */
  async selectPolicy(id) {
    if (!id || id === this.policyId || this.store.tryRunning) return false;
    /* 保存回包还没落地就把草稿切走，会让“旧回包写新策略”的竞态变得可达。 */
    if (this.store.saving) {
      this.hooks.toast(t('pv.toastSavingBlocked'), 'warn');
      return false;
    }
    if (this.store.dirtyKind !== 'clean') {
      const discard = await this.hooks.askDiscard('switch');
      if (!discard) return false;
    }
    const seq = ++this.loadSeq;
    try {
      const result = await api.get('/admin/policies/' + encodeURIComponent(id));
      if (seq !== this.loadSeq) return false;
      if (this.store.saving) {
        this.hooks.toast(t('pv.toastSavingBlocked'), 'warn');
        return false;
      }
      const resource = result && result.policy_resource;
      if (!resource || !resource.policy) throw new Error('invalid policy payload');
      const layoutResult = await api.get('/admin/policies/' + encodeURIComponent(id) + '/layout');
      if (seq !== this.loadSeq) return false;
      this.loadPolicy(resource, layoutResult, true);
      return true;
    } catch (error) {
      /* 旧请求失败时已有更新的选择胜出：静默丢弃。 */
      if (seq !== this.loadSeq) return false;
      this.hooks.toast(tf('pv.toastLoadFail', { err: apiErrText(error) }), 'error');
      return false;
    }
  }

  /* 删除策略：确认后 DELETE；删除的是当前策略时切换到剩余策略或新草稿。 */
  async deletePolicy(id) {
    const store = this.store;
    if (store.saving || store.tryRunning) return false;
    const target = id || this.policyId;
    if (!target) return false;
    const confirmed = await this.hooks.askDelete(target);
    if (!confirmed) return false;
    store.setSaving(true);
    this.hooks.syncSaving();
    try {
      const result = await api.del('/admin/policies/' + encodeURIComponent(target));
      if (result && Array.isArray(result.policies)) this.policies = result.policies;
      if (result && result.snapshot_revision != null) this.snapshotRevision = result.snapshot_revision;
      if (target === this.policyId) {
        const next = this.policies.find(item => item.enabled) || this.policies[0];
        if (next) {
          const loaded = await api.get('/admin/policies/' + encodeURIComponent(next.id));
          const layout = await api.get(
            '/admin/policies/' + encodeURIComponent(next.id) + '/layout');
          this.loadPolicy(loaded.policy_resource, layout, true);
        } else {
          this.loadNewDraft();
        }
      }
      this.hooks.setIssues([]);
      this.hooks.renderPolicySelect();
      this.hooks.render();
      this.hooks.refreshDirty();
      this.hooks.toast(t('pv.toastPolicyDeleted'));
      return true;
    } catch (error) {
      if (error && error.status === 409) {
        this.hooks.toast(t('pv.toastDeleteActive'), 'error');
      } else {
        this.hooks.toast(tf('pv.toastDeleteFail', { err: apiErrText(error) }), 'error');
      }
      return false;
    } finally {
      store.setSaving(false);
      this.hooks.syncSaving();
    }
  }

  /* 载入示例：example.json 与 spec §4.1 同构；一次历史操作替换整个文档。 */
  async loadExample() {
    const confirmed = await this.hooks.askDiscard('example');
    if (!confirmed) return false;
    try {
      const response = await fetch('routing/example.json', { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const example = await response.json();
      if (!example || example.kind !== 'sr-routing-policy' || example.version !== 4) {
        throw new Error('invalid example policy');
      }
      if (this.hooks.change('behavior', draft => {
        draft.policy = clone(example);
      }) === false) return false;
      this.hooks.toast(t('pv.toastExampleLoaded'));
      return true;
    } catch (error) {
      this.hooks.toast(tf('pv.toastLoadFail', {
        err: String(error && error.message != null ? error.message : error),
      }), 'error');
      return false;
    }
  }
}
