/* Unified routing-tree data model (console rewrite, 2026-10-26).

   A tree is a single `root` node — there are no Stations any more. The four
   node types follow docs §3.2 / task-12 brief Step 3 and the canonical wire
   form of router/pipeline/tree.py:

     branch     {id,type,when?,children[],otherwise?}
     nl_choice  {id,type,when?,instructions,min_prob,min_margin,options[],otherwise?}
     route      {id,type,when?,model,reasoning,weights[],min_score?}
     block      {id,type,when?,weights[],min_score?}

   `when` may sit on every non-root, non-otherwise node. `otherwise` is a
   normal out-edge of an *entered* branch/nl_choice; terminal nodes (route/
   block) carry neither children nor otherwise. An `nl_choice` candidate is an
   out-edge: a direct `model` candidate is exposed to walk() as an auto-built
   `route` terminal (`<nl_id>/options/<key>`, same id as the backend compiler)
   so the editor and the engine see the same graph — the capability text never
   enters the tree JSON (it lives in the model profile, docs §4.4).

   Everything here is a pure function of its input: normalisers return new
   objects, updateNode/removeNode/moveNode return a NEW root and never touch
   the input, and candidate order is only ever swapped by position — never
   sorted — because order is behaviour (docs §4.5).

   Loaded as a classic script before routing-tree-store.js / pipeline.js; the
   module also exports itself for `require()` so the pure functions can be
   self-tested from node (no DOM, no API).

   Note on canonical shape: normaliseNode keeps `otherwise: null` on
   branch/nl_choice while router/pipeline/tree.py omits an absent `otherwise`.
   Both directions are lossless (`node_from_dict` treats a null `otherwise` as
   absent), so JSON.stringify(normalizeNode(x)) is a stable round-trip value.

   Two backend-parity guards are deliberately stricter than the brief's code
   (ruled by the controller on 2026-10-26): `when.op` must be "eq"/"contains"
   and every weight needs a non-empty `signal`. Foreign/unexpected keys are
   still ignored on purpose, so a future wire field cannot break import. */
"use strict";

const SRTreeModel = (function () {
  /* ── normalisation (wire / imported JSON -> canonical draft nodes) ───── */

  function unit(value, name) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
      throw new Error(`${name} must be in [0,1]`);
    }
    return value;
  }

  function normalizeWhen(raw) {
    if (raw == null) return null;
    if (typeof raw.signal !== "string" || !raw.signal) throw new Error("when.signal required");
    // `op` must match the signal arity (backend WHEN_OPS, validation.py):
    // multi-value signals use "contains", single-value signals use "eq".
    const op = raw.op || "eq";
    if (op !== "eq" && op !== "contains") throw new Error("when.op must be eq or contains");
    const result = {signal: raw.signal, value: String(raw.value ?? ""), op: op};
    if (raw.conf_min != null) result.conf_min = unit(raw.conf_min, "conf_min");
    return result;
  }

  function normalizeOption(raw) {
    if (!raw || typeof raw.key !== "string" || !raw.key) throw new Error("option.key required");
    if ((raw.model == null) === (raw.child == null)) throw new Error("option needs model or child");
    if (raw.model != null) {
      if (typeof raw.model !== "string" || !raw.model) throw new Error("option.model required");
      return {key: raw.key, model: raw.model, reasoning: raw.reasoning ?? null};
    }
    if (typeof raw.description !== "string" || !raw.description.trim()) {
      throw new Error("option.description required");
    }
    return {key: raw.key, description: raw.description.trim(),
            child: normalizeNode(raw.child, {root: false, otherwise: false})};
  }

  function normalizeNode(raw, context = {root: true, otherwise: false}) {
    if (!raw || typeof raw.id !== "string" || !raw.id) throw new Error("node.id required");
    if ((context.root || context.otherwise) && raw.when != null) {
      throw new Error("root/otherwise cannot have when");
    }
    const node = {id: raw.id, type: raw.type};
    if (raw.when != null) node.when = normalizeWhen(raw.when);
    if (raw.type === "route" || raw.type === "block") {
      if (raw.otherwise != null || raw.children != null) throw new Error("terminal cannot have children");
      if (raw.type === "route") {
        if (typeof raw.model !== "string" || !raw.model) throw new Error("route.model required");
        node.model = raw.model;
        node.reasoning = raw.reasoning ?? null;
      }
      node.weights = (raw.weights || []).map(weight => {
        // Backend parity (`_weight_from_dict`): a weight needs its signal.
        if (!weight || typeof weight.signal !== "string" || !weight.signal) {
          throw new Error("weight.signal required");
        }
        return {
          signal: weight.signal, value: String(weight.value ?? ""),
          weight: unit(weight.weight ?? 0.5, "weight")
        };
      });
      if (raw.min_score != null) node.min_score = unit(raw.min_score, "min_score");
      return node;
    }
    if (raw.type === "branch") {
      node.children = (raw.children || []).map(
        child => normalizeNode(child, {root: false, otherwise: false}));
    } else if (raw.type === "nl_choice") {
      if (typeof raw.instructions !== "string" && raw.instructions != null) {
        throw new Error("instructions must be a string");
      }
      node.instructions = raw.instructions || "";
      node.min_prob = unit(raw.min_prob ?? 0.45, "min_prob");
      node.min_margin = unit(raw.min_margin ?? 0.10, "min_margin");
      node.options = (raw.options || []).map(normalizeOption);
    } else {
      throw new Error(`unknown node type ${raw.type}`);
    }
    node.otherwise = raw.otherwise == null ? null : normalizeNode(
      raw.otherwise, {root: false, otherwise: true});
    return node;
  }

  /* ── out-edges ───────────────────────────────────────────────────────── */

  /* Every child edge of `node`, in configuration order: branch.children,
     nl_choice options (direct model candidates included as their auto-built
     route terminal) and finally `otherwise`. */
  function childRefs(node) {
    const refs = [];
    if (node.type === "branch") {
      node.children.forEach((child, index) => refs.push(
        {node: child, parent: node, edgeKind: "children", index, optionKey: null}));
    }
    if (node.type === "nl_choice") {
      node.options.forEach((option, index) => {
        const child = option.child || {
          id: `${node.id}/options/${option.key}`, type: "route",
          model: option.model, reasoning: option.reasoning ?? null
        };
        refs.push({node: child, parent: node, edgeKind: "optionChild", index, optionKey: option.key});
      });
    }
    if ((node.type === "branch" || node.type === "nl_choice") && node.otherwise) {
      refs.push({node: node.otherwise, parent: node, edgeKind: "otherwise", index: 0, optionKey: null});
    }
    return refs;
  }

  function walk(root, visit) {
    const seen = new Set();
    function inner(node, ref) {
      if (seen.has(node.id)) throw new Error(`duplicate node id ${node.id}`);
      seen.add(node.id);
      visit(node, ref);
      childRefs(node).forEach(child => inner(child.node, child));
    }
    inner(root, {node: root, parent: null, edgeKind: "root", index: 0, optionKey: null});
  }

  function findNodeRef(root, id) {
    let found = null;
    walk(root, (node, ref) => { if (node.id === id) found = {...ref, node}; });
    return found;
  }

  /* ── pure mutations: always return a NEW root, never mutate input ────── */

  function updateNode(node, id, updater) {
    if (node.id === id) return updater(node);
    const copy = {...node};
    if (node.type === "branch") {
      copy.children = node.children.map(child => updateNode(child, id, updater));
    }
    if (node.type === "nl_choice") {
      copy.options = node.options.map(option => {
        if (option.child) {
          return {...option, child: updateNode(option.child, id, updater)};
        }
        /* Direct `model` candidates have no stored node: walk exposes the
           auto-built route `<nl_id>/options/<key>`. An update targeting that
           synthetic id maps back onto the candidate's model/reasoning instead
           of silently no-oping (review Minor 1). */
        const syntheticId = `${node.id}/options/${option.key}`;
        if (syntheticId !== id) return {...option};
        const updated = updater({
          id: syntheticId, type: "route", model: option.model,
          reasoning: option.reasoning ?? null, weights: [],
        });
        if (updated != null && (updated.type != null && updated.type !== "route")) {
          throw new Error("a direct model candidate must stay a route node");
        }
        const next = {...option};
        if (updated) {
          if (updated.model != null) next.model = updated.model;
          next.reasoning = updated.reasoning ?? null;
        }
        return next;
      });
    }
    if (node.otherwise) copy.otherwise = updateNode(node.otherwise, id, updater);
    return copy;
  }

  function removeNode(root, id) {
    const ref = findNodeRef(root, id);
    if (!ref || !ref.parent) throw new Error("cannot remove root or missing node");
    return updateNode(root, ref.parent.id, parent => {
      if (ref.edgeKind === "children") {
        return {...parent, children: parent.children.filter((_, index) => index !== ref.index)};
      }
      if (ref.edgeKind === "optionChild") {
        return {...parent, options: parent.options.filter((_, index) => index !== ref.index)};
      }
      return {...parent, otherwise: null};
    });
  }

  function moveNode(root, id, direction) {
    const ref = findNodeRef(root, id);
    if (!ref || ref.edgeKind === "root" || ref.edgeKind === "otherwise") return root;
    return updateNode(root, ref.parent.id, parent => {
      const field = ref.edgeKind === "children" ? "children" : "options";
      const items = [...parent[field]];
      const target = ref.index + direction;
      if (target < 0 || target >= items.length) return parent;
      [items[ref.index], items[target]] = [items[target], items[ref.index]];
      return {...parent, [field]: items};
    });
  }

  /* ── helpers used by the store / editor ──────────────────────────────── */

  /* Deep copy of a canonical node (or any JSON-ish value). The store keeps a
     draft and a saved snapshot independent, so a draft edit can never leak
     into the saved copy. */
  function clone(value) {
    if (Array.isArray(value)) return value.map(clone);
    if (value && typeof value === "object") {
      const out = {};
      Object.keys(value).forEach(key => { out[key] = clone(value[key]); });
      return out;
    }
    return value;
  }

  return {
    unit, normalizeWhen, normalizeOption, normalizeNode,
    childRefs, walk, findNodeRef, updateNode, removeNode, moveNode, clone,
  };
})();

if (typeof module === "object" && module.exports) module.exports = SRTreeModel;
