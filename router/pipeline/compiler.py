"""Effective-tree compiler: expand direct model candidates into input text.

The stored ``RoutingTree`` keeps ``model`` candidates as configuration sugar (a
key + model reference + optional reasoning).  The DecisionRunner never sees a
model id — it chooses among ordered candidate *texts* — so a snapshot must run
the **effective** tree produced here:

* direct ``model`` candidate -> ``text = "<model>: <capability>"`` from the
  already-validated model catalog mapping (the capability text is never copied
  into the stored tree);
* child candidate -> recurse into its target, stored description unchanged;
* terminals are shared (immutable), only ``Branch``/``NLChoice`` containers are
  copied with ``dataclasses.replace``.

The step is pure: the input tree is never mutated and candidate order is never
sorted (order is behaviour).  ``effective_node_to_dict`` is the matching
snapshot serde — the exact canonical shape of :func:`tree.node_to_dict` (null
keys omitted) plus the compiled candidate ``text``, so the snapshot revision
covers the real model input and a replay restores it faithfully.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from router.pipeline.scope import (
    SignalScope,
    compute_tree_scope,
    required_signal_keys,
)
from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Node,
    Route,
    RoutingTree,
    node_from_dict,
    node_to_dict,
)

#: Extra field the effective serde adds to a direct-model candidate.
EFFECTIVE_TEXT_FIELD = "text"


def compile_tree(tree: RoutingTree,
                 capabilities: Mapping[str, str]) -> RoutingTree:
    """Return the effective tree with every direct candidate text expanded.

    ``capabilities`` maps model id -> validated ``profile.capability`` text; a
    referenced model without one is a configuration error (enable validation
    rejects it, this is the defensive compile boundary).
    """
    return replace(tree, root=_compile_node(tree.root, capabilities))


def _compile_node(node: Node, capabilities: Mapping[str, str]) -> Node:
    if isinstance(node, (Route, Block)):
        return node
    if isinstance(node, Branch):
        return replace(
            node,
            children=tuple(_compile_node(child, capabilities)
                           for child in node.children),
            otherwise=None if node.otherwise is None
            else _compile_node(node.otherwise, capabilities))
    return replace(
        node,
        options=tuple(_compile_option(option, capabilities)
                      for option in node.options),
        otherwise=None if node.otherwise is None
        else _compile_node(node.otherwise, capabilities))


def _compile_option(option: NLOption,
                    capabilities: Mapping[str, str]) -> NLOption:
    if option.capability_model is not None:
        model = option.capability_model
        capability = capabilities.get(model)
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError(
                f"model {model!r} has no capability description for the "
                f"direct NL candidate {option.key!r}")
        return replace(option, text=f"{model}: {capability.strip()}")
    return replace(option, target=_compile_node(option.target, capabilities))


def effective_node_to_dict(node: Node) -> dict:
    """Canonical effective serde: ``node_to_dict`` + compiled option text.

    ``node_to_dict`` is reused verbatim as the base so the two serializers can
    never drift; the direct-model candidate dict then gains
    ``EFFECTIVE_TEXT_FIELD`` (child candidates already carry their description).
    """
    encoded = node_to_dict(node)
    _add_compiled_text(node, encoded)
    return encoded


def _add_compiled_text(node: Node, encoded: dict) -> None:
    if isinstance(node, Branch):
        for child, child_encoded in zip(node.children, encoded["children"]):
            _add_compiled_text(child, child_encoded)
        if node.otherwise is not None:
            _add_compiled_text(node.otherwise, encoded["otherwise"])
    elif isinstance(node, NLChoice):
        for option, option_encoded in zip(node.options, encoded["options"]):
            if option.capability_model is not None:
                option_encoded[EFFECTIVE_TEXT_FIELD] = option.text
            else:
                _add_compiled_text(option.target, option_encoded["child"])


def effective_node_from_dict(raw: dict) -> Node:
    """Parse the effective serde back, keeping compiled direct-candidate text.

    Companion of :func:`effective_node_to_dict` used by ``snapshot_from_content``
    for decision replay.  The stored-tree parser is strict (unknown keys are
    rejected), so the effective-only ``text`` field is first stripped in a
    recursive copy — ``raw`` itself is never mutated — then the texts are
    re-attached structurally.
    """
    return _restore_compiled_text(raw, node_from_dict(_without_compiled_text(raw)))


def _without_compiled_text(raw):
    """Deep copy of an effective node dict with direct-candidate ``text`` removed."""
    if not isinstance(raw, dict):
        return raw
    node = dict(raw)
    children = node.get("children")
    if isinstance(children, list):
        node["children"] = [_without_compiled_text(child) for child in children]
    otherwise = node.get("otherwise")
    if isinstance(otherwise, dict):
        node["otherwise"] = _without_compiled_text(otherwise)
    options = node.get("options")
    if isinstance(options, list):
        stripped = []
        for option in options:
            if isinstance(option, dict) and "model" in option:
                option = {key: value for key, value in option.items()
                          if key != EFFECTIVE_TEXT_FIELD}
            elif isinstance(option, dict) \
                    and isinstance(option.get("child"), dict):
                option = dict(option)
                option["child"] = _without_compiled_text(option["child"])
            stripped.append(option)
        node["options"] = stripped
    return node


def _restore_compiled_text(encoded: dict, node: Node) -> Node:
    if isinstance(node, Branch):
        children_encoded = encoded.get("children")
        if not isinstance(children_encoded, list) \
                or len(children_encoded) != len(node.children):
            raise ValueError("effective branch children do not match the tree")
        otherwise = node.otherwise
        if otherwise is not None:
            otherwise = _restore_compiled_text(encoded["otherwise"], otherwise)
        return replace(node, children=tuple(
            _restore_compiled_text(child_encoded, child)
            for child_encoded, child in zip(children_encoded, node.children)),
            otherwise=otherwise)
    if isinstance(node, NLChoice):
        options_encoded = encoded.get("options")
        if not isinstance(options_encoded, list) \
                or len(options_encoded) != len(node.options):
            raise ValueError("effective nl options do not match the tree")
        options = []
        for option_encoded, option in zip(options_encoded, node.options):
            if option.capability_model is not None:
                text = option_encoded.get(EFFECTIVE_TEXT_FIELD, option.text)
                if not isinstance(text, str):
                    raise ValueError(
                        f"effective option {option.key!r} text must be a string")
                options.append(replace(option, text=text))
            else:
                options.append(replace(
                    option, target=_restore_compiled_text(
                        option_encoded["child"], option.target)))
        otherwise = node.otherwise
        if otherwise is not None:
            otherwise = _restore_compiled_text(encoded["otherwise"], otherwise)
        return replace(node, options=tuple(options), otherwise=otherwise)
    return node


def tree_signal_requirements(root: Node) -> tuple[frozenset[str], SignalScope]:
    """Static signal requirements, computed once per snapshot build.

    Both values are derived from the effective tree and never scanned per
    request: ``required_signals`` is what signal preparation must compute and
    ``scope`` is the model-question subset the tagger may trim to.
    """
    return required_signal_keys(root), compute_tree_scope(root)
