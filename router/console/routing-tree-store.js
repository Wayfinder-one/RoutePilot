/* Unified routing-tree store (console rewrite, 2026-10-26).

   One tree = one draft + one saved snapshot + one dirty flag, so switching
   trees never throws away an unsaved edit and editing one tree cannot dirty
   another. The state shape is fixed (task-12 brief Step 4):

     {
       trees: [],              // GET /admin/trees rows, server order preserved
       selectedTreeId: null,
       draftsById: Map(),      // tree id -> draft root node (canonical shape)
       savedById: Map(),       // tree id -> last server-confirmed root node
       dirtyById: Map(),       // tree id -> bool (draft differs from saved)
       modelsById: Map(),      // model id -> GET /admin/models row (capability)
       lastTryByTree: Map(),   // tree id -> last POST /admin/try response
     }

   Backend contract (Task 9, router/api/routes_trees.py):

     GET  /admin/trees
     POST /admin/trees                    {id?,name,root}     new tree (disabled)
     GET  /admin/trees/{tree_id}
     PUT  /admin/trees/{tree_id}          {name,root}         save (never `enabled`)
     POST /admin/trees/{tree_id}/copy     {id,name}          disabled copy
     POST /admin/trees/{tree_id}/enable
     POST /admin/trees/{tree_id}/disable
     POST /admin/try                      {tree_id,text,headers,n_messages}

   The server is always the source of truth for `enabled`: enable/disable
   re-read GET /admin/trees instead of guessing the mutual exclusion locally
   (spec §7.1). `snapshot_revision` is a single publication pointer, not a
   per-tree value, so it is kept outside the seven fixed maps.

   Browser: loads after api.js and routing-tree-model.js and reads both
   globals lazily. Node self-tests inject them with
   SRTreeStore.configure({api, model}). Nothing here touches the DOM.
*/
"use strict";

const SRTreeStore = (function () {
  const state = {
    trees: [],
    selectedTreeId: null,
    draftsById: new Map(),
    savedById: new Map(),
    dirtyById: new Map(),
    modelsById: new Map(),
    lastTryByTree: new Map(),
  };

  /* Active snapshot publication pointer (spec §8.2) — not per tree. */
  let snapshotRevision = "";

  /* Injected dependencies (tests); empty in the browser. */
  let deps = {api: null, model: null};

  function apiClient() {
    const client = deps.api || (typeof api !== "undefined" ? api : null);
    if (!client) throw new Error("SRTreeStore: api client not loaded");
    return client;
  }

  function modelNs() {
    const model = deps.model || (typeof SRTreeModel !== "undefined" ? SRTreeModel : null);
    if (!model) throw new Error("SRTreeStore: SRTreeModel not loaded");
    return model;
  }

  /* ── record helpers ──────────────────────────────────────────────────── */

  function treePath(treeId) {
    return "/admin/trees/" + encodeURIComponent(treeId);
  }

  function metaOf(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new Error("GET /admin/trees: tree row must be an object");
    }
    const id = typeof raw.id === "string" ? raw.id.trim() : "";
    if (!id) throw new Error("GET /admin/trees: tree row needs a nonempty string id");
    return {
      id: id,
      name: raw.name == null ? "" : String(raw.name),
      enabled: !!raw.enabled,
      tree_revision: raw.tree_revision == null ? "" : String(raw.tree_revision),
      updated_at: raw.updated_at == null ? "" : String(raw.updated_at),
    };
  }

  function mergeMeta(meta, raw) {
    if (!meta || !raw || typeof raw !== "object" || Array.isArray(raw)) return meta;
    if (raw.name != null) meta.name = String(raw.name);
    if (raw.enabled != null) meta.enabled = !!raw.enabled;
    if (raw.tree_revision != null) meta.tree_revision = String(raw.tree_revision);
    if (raw.updated_at != null) meta.updated_at = String(raw.updated_at);
    return meta;
  }

  /* Accept both `{tree: {...}}` wrappers and a bare tree envelope. */
  function unwrapTree(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    if (body.tree && typeof body.tree === "object" && !Array.isArray(body.tree)) return body.tree;
    return body;
  }

  function pickList(body) {
    if (Array.isArray(body)) return body;
    if (body && Array.isArray(body.trees)) return body.trees;
    throw new Error("GET /admin/trees: response carries no tree list");
  }

  function absorbRevision(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return snapshotRevision;
    const raw = body.snapshot_revision != null ? body.snapshot_revision
      : (body.snapshotRevision != null ? body.snapshotRevision : null);
    if (raw != null) snapshotRevision = String(raw);
    return snapshotRevision;
  }

  /* ── per-tree content ────────────────────────────────────────────────── */

  function normalizeRoot(raw, treeId) {
    if (!raw || typeof raw !== "object") throw new Error(`tree ${treeId}: root node missing`);
    return modelNs().normalizeNode(raw, {root: true, otherwise: false});
  }

  /* Trust-boundary check before a write: duplicate node ids (including the
     auto-built `<nl_id>/options/<key>` ids) are rejected here as well as by
     the backend health check, so the editor reports them with the same id. */
  function validateRoot(root, treeId) {
    try {
      modelNs().walk(root, () => {});
    } catch (err) {
      throw new Error(`tree ${treeId}: ${err.message}`);
    }
    return root;
  }

  function installRoot(meta, rawRoot) {
    const root = normalizeRoot(rawRoot, meta.id);
    state.savedById.set(meta.id, root);
    state.draftsById.set(meta.id, modelNs().clone(root));
    state.dirtyById.set(meta.id, false);
    return root;
  }

  function canonical(value) {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map(key =>
        JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
    }
    return JSON.stringify(value === undefined ? null : value);
  }

  function computeDirty(treeId) {
    const draft = state.draftsById.get(treeId);
    const saved = state.savedById.get(treeId);
    if (draft === undefined || saved === undefined) return false;
    return canonical(draft) !== canonical(saved);
  }

  /* ── selection ───────────────────────────────────────────────────────── */

  function findTree(treeId) {
    return state.trees.find(tree => tree.id === treeId) || null;
  }

  function defaultSelectedId() {
    const enabled = state.trees.find(tree => tree.enabled);
    if (enabled) return enabled.id;
    return state.trees.length ? state.trees[0].id : null;
  }

  function ensureSelection() {
    const ids = new Set(state.trees.map(tree => tree.id));
    if (state.selectedTreeId && ids.has(state.selectedTreeId)) return;
    state.selectedTreeId = defaultSelectedId();
  }

  function requireTree(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    if (!id) throw new Error("SRTreeStore: no tree selected");
    const meta = findTree(id);
    if (!meta) throw new Error(`SRTreeStore: unknown tree ${id}`);
    return meta;
  }

  function select(treeId) {
    if (treeId === null || treeId === undefined) {
      state.selectedTreeId = null;
      return null;
    }
    const meta = findTree(treeId);
    if (!meta) throw new Error(`SRTreeStore: unknown tree ${treeId}`);
    state.selectedTreeId = meta.id;
    return meta;
  }

  /* ── loading ─────────────────────────────────────────────────────────── */

  function idSet() {
    return new Set(state.trees.map(tree => tree.id));
  }

  async function readTreeList(reset) {
    const body = await apiClient().get("/admin/trees");
    absorbRevision(body);
    const rows = pickList(body);
    const metas = rows.map(metaOf);
    state.trees = metas;
    if (reset) {
      state.draftsById.clear();
      state.savedById.clear();
      state.dirtyById.clear();
      state.lastTryByTree.clear();
    }
    /* `refresh()` only needs metadata: trees that already have content keep
       their draft/saved entry (an enable/disable or a create elsewhere must
       not throw away a locally edited draft). */
    const missing = [];
    metas.forEach((meta, index) => {
      if (!reset && state.savedById.has(meta.id)) return;
      const row = rows[index];
      const embedded = row && typeof row === "object" && row.root != null ? row.root : null;
      missing.push({meta: meta, embedded: embedded});
    });
    const roots = await Promise.all(missing.map(async item => {
      if (item.embedded != null) return item.embedded;
      const detail = unwrapTree(await apiClient().get(treePath(item.meta.id)));
      if (!detail || detail.root == null) {
        throw new Error(`tree ${item.meta.id}: GET ${treePath(item.meta.id)} returned no root`);
      }
      mergeMeta(item.meta, detail);
      return detail.root;
    }));
    missing.forEach((item, index) => installRoot(item.meta, roots[index]));
    ensureSelection();
    return state.trees;
  }

  async function loadModels() {
    const body = await apiClient().get("/admin/models");
    const rows = body && Array.isArray(body.models) ? body.models : null;
    if (!rows) throw new Error("GET /admin/models: response carries no model list");
    state.modelsById.clear();
    rows.forEach(row => {
      if (row && typeof row.id === "string" && row.id) state.modelsById.set(row.id, row);
    });
    return [...state.modelsById.values()];
  }

  /* Full boot load: tree list + roots + model catalog, resetting all drafts. */
  async function load() {
    await Promise.all([readTreeList(true), loadModels()]);
    return {trees: state.trees, selectedTreeId: state.selectedTreeId};
  }

  /* Metadata-only reload; drafts and dirty flags survive untouched. Used by
     enable/disable and after any server-side tree creation. */
  async function refresh() {
    return readTreeList(false);
  }

  /* ── drafts ──────────────────────────────────────────────────────────── */

  function draftRoot(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    return id ? state.draftsById.get(id) || null : null;
  }

  function savedRoot(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    return id ? state.savedById.get(id) || null : null;
  }

  function markDirty(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    if (!id || !findTree(id)) return false;
    const dirty = computeDirty(id);
    state.dirtyById.set(id, dirty);
    return dirty;
  }

  function isDirty(treeId) {
    return markDirty(treeId);
  }

  /* Replace one tree's draft after an editor mutation (updateNode/import).
     The root is canonicalised on the way in, so dirty comparison never trips
     over key order. Only the given tree's state changes. */
  function setDraft(treeId, root) {
    const meta = requireTree(treeId);
    const next = normalizeRoot(root, meta.id);
    /* duplicate node ids / candidate keys must fail BEFORE the draft is
       replaced, so a bad import cannot clear the current draft (Minor 2). */
    validateRoot(next, meta.id);
    state.draftsById.set(meta.id, next);
    if (!state.savedById.has(meta.id)) state.savedById.set(meta.id, modelNs().clone(next));
    markDirty(meta.id);
    return next;
  }

  /* ── save / create / copy ────────────────────────────────────────────── */

  async function save(treeId) {
    const meta = requireTree(treeId);
    if (!state.draftsById.has(meta.id)) throw new Error(`tree ${meta.id}: no draft loaded`);
    const root = validateRoot(normalizeRoot(state.draftsById.get(meta.id), meta.id), meta.id);
    state.draftsById.set(meta.id, root);
    /* PUT only ever carries content: `enabled` is managed by enable/disable
       (spec §7.1) and the backend rejects it on the generic update. */
    const body = {name: meta.name, root: root};
    const res = await apiClient().put(treePath(meta.id), body);
    /* A save of the ACTIVE tree republishes the online snapshot: absorb the
       response revision so the pill/store stop showing the stale one (P1). */
    absorbRevision(res);
    const savedTree = unwrapTree(res);
    const savedRoot = savedTree && savedTree.root != null
      ? normalizeRoot(savedTree.root, meta.id) : root;
    state.savedById.set(meta.id, savedRoot);
    state.draftsById.set(meta.id, modelNs().clone(savedRoot));
    state.dirtyById.set(meta.id, false);
    mergeMeta(meta, savedTree);
    return {
      tree: meta,
      root: state.savedById.get(meta.id),
      warnings: res && Array.isArray(res.warnings) ? res.warnings : [],
      snapshot_revision: snapshotRevision,
      raw: res,
    };
  }

  function resolveNewTreeId(responseTree, beforeIds, name) {
    if (responseTree && typeof responseTree.id === "string" && responseTree.id) {
      const hit = findTree(responseTree.id);
      if (hit) return hit.id;
    }
    const added = state.trees.filter(tree => !beforeIds.has(tree.id));
    if (added.length === 1) return added[0].id;
    const named = added.filter(tree => tree.name === name);
    return named.length === 1 ? named[0].id : null;
  }

  async function create(name, root, options) {
    const cleanName = typeof name === "string" ? name.trim() : "";
    if (!cleanName) throw new Error("tree name required");
    if (!root || typeof root !== "object") throw new Error("tree root required");
    const normalizedRoot = validateRoot(normalizeRoot(root, "new tree"), "new tree");
    const body = {name: cleanName, root: normalizedRoot};
    const opts = options && typeof options === "object" ? options : {};
    if (typeof opts.id === "string" && opts.id.trim()) body.id = opts.id.trim();
    const before = idSet();
    const res = await apiClient().post("/admin/trees", body);
    const created = unwrapTree(res);
    await refresh();
    const newId = resolveNewTreeId(created, before, cleanName);
    if (!newId) throw new Error("POST /admin/trees: created tree not found in GET /admin/trees");
    select(newId);
    return findTree(newId);
  }

  async function copy(treeId, spec) {
    const meta = requireTree(treeId);
    const source = spec && typeof spec === "object" ? spec : {};
    const newId = typeof source.id === "string" ? source.id.trim() : "";
    const newName = typeof source.name === "string" ? source.name.trim() : "";
    if (!newId) throw new Error("tree copy: id required");
    if (!newName) throw new Error("tree copy: name required");
    /* The copy request carries only id/name: `enabled` and `updated_at` are
       server-managed and a copy always starts disabled (spec §7.1). */
    const before = idSet();
    const res = await apiClient().post(treePath(meta.id) + "/copy",
                                        {id: newId, name: newName});
    const created = unwrapTree(res);
    await refresh();
    const createdId = (created && typeof created.id === "string" && created.id) || newId;
    const resolved = findTree(createdId) ? createdId
      : resolveNewTreeId(created, before, newName);
    if (!resolved) throw new Error(`POST /admin/trees/${meta.id}/copy: copied tree not found`);
    select(resolved);
    return findTree(resolved);
  }

  /* ── enable / disable (server decides the mutual exclusion) ──────────── */

  async function setEnabled(treeId, action) {
    const meta = requireTree(treeId);
    const body = await apiClient().post(treePath(meta.id) + "/" + action);
    absorbRevision(body);
    /* Never guess: the refetched list is the authoritative enabled state for
       *all* trees (enabling B disables A in the same server transaction). */
    await refresh();
    return findTree(meta.id);
  }

  function enable(treeId) {
    return setEnabled(treeId, "enable");
  }

  function disable(treeId) {
    return setEnabled(treeId, "disable");
  }

  /* ── per-tree try (disabled trees are still tryable, spec §9) ────────── */

  async function tryTree(treeId, payload) {
    const meta = requireTree(treeId);
    const input = payload && typeof payload === "object" ? payload : {};
    const text = typeof input.text === "string" ? input.text : "";
    if (!text.trim()) throw new Error("try.text required");
    const body = {
      tree_id: meta.id,
      text: text,
      headers: input.headers && typeof input.headers === "object" ? input.headers : {},
      n_messages: input.n_messages == null ? 1 : input.n_messages,
    };
    const res = await apiClient().post("/admin/try", body);
    state.lastTryByTree.set(meta.id, res);
    return res;
  }

  function lastTry(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    return id ? state.lastTryByTree.get(id) || null : null;
  }

  /* ── read helpers (state is otherwise read-only for callers) ─────────── */

  function treeList() {
    return state.trees;
  }

  function selectedTreeId() {
    return state.selectedTreeId;
  }

  function selectedTree() {
    return findTree(state.selectedTreeId);
  }

  function treeRevision(treeId) {
    const id = treeId === undefined || treeId === null ? state.selectedTreeId : treeId;
    const meta = id ? findTree(id) : null;
    return meta ? meta.tree_revision : "";
  }

  function modelRow(modelId) {
    return state.modelsById.get(modelId) || null;
  }

  function modelCapability(modelId) {
    const row = modelRow(modelId);
    return row && typeof row.capability === "string" ? row.capability : "";
  }

  function modelIds() {
    return [...state.modelsById.keys()];
  }

  function currentSnapshotRevision() {
    return snapshotRevision;
  }

  function configure(next) {
    if (next && typeof next === "object") deps = Object.assign({}, deps, next);
    return store;
  }

  const store = {
    state,
    configure,
    /* loading */
    load, refresh, loadModels,
    /* selection */
    select, selectedTreeId, selectedTree, tree: findTree, trees: treeList,
    /* drafts */
    draft: draftRoot, saved: savedRoot, setDraft, markDirty, isDirty,
    /* writes */
    save, create, copy, enable, disable,
    /* try */
    tryTree, lastTry,
    /* model catalog + revisions */
    model: modelRow, modelCapability, modelIds,
    treeRevision, snapshotRevision: currentSnapshotRevision,
  };
  return store;
})();

if (typeof module === "object" && module.exports) module.exports = SRTreeStore;
