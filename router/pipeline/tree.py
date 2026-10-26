"""Unified routing tree: frozen node model and full-out-edge JSON serde.

One ``RoutingTree`` is an embedded, finite tree (no cross references) of four
node types:

  route      -> terminal: route to a model (reasoning, weights, min_score)
  block      -> terminal: block the request (weights, min_score)
  branch     -> ordered ``children`` (each with its own ``when``) + ``otherwise``
  nl_choice  -> natural-language multi-way choice + ``otherwise``

``when`` only decides whether a node is entered; ``otherwise`` is a normal,
visible out-edge of an already-entered ``branch``/``nl_choice``.  The root has a
single entry, so the tree type falls out of the node composition instead of a
``type=natural_language|signal`` flag (design doc §3–§4).

This module is the configuration trust boundary: :func:`node_from_dict` /
:func:`tree_from_dict` raise ``ValueError`` for malformed JSON so the API layer
turns it into 4xx and nothing downstream re-validates shape.  Semantic checks
that need runtime context (signal arity, model catalog, required fallback,
natural-language thresholds of a *complete* tree) belong to the validation layer;
a disabled draft tree may still be saved with empty ``children``/``options``.

The JSON form is canonical: serialising the parsed result twice is identical and
candidate order is never sorted.  Direct ``model`` options are pure config sugar;
in memory they stay ``NLOption`` with ``text=''`` and an auto-built
``Route(id=f"{nl_id}/options/{key}")`` target.  The compiler fills the text with
``profile.capability`` later; it is not stored in the tree JSON.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass

#: Defaults from the core contracts (single source for dataclasses + parsing).
DEFAULT_MIN_PROB = 0.45
DEFAULT_MIN_MARGIN = 0.10
DEFAULT_WEIGHT = 0.50

#: ``when.op`` values: ``eq`` for single-value signals, ``contains`` for multi.
WHEN_OPS = ("eq", "contains")
#: Wire ``type`` of every node, in the order the editor palette shows them.
NODE_TYPES = ("route", "block", "branch", "nl_choice")


@dataclass(frozen=True)
class When:
    signal: str
    value: str
    op: str = "eq"
    conf_min: float | None = None


@dataclass(frozen=True)
class Weight:
    signal: str
    value: str
    weight: float = DEFAULT_WEIGHT


@dataclass(frozen=True)
class Route:
    id: str
    model: str
    reasoning: str | None = None
    when: When | None = None
    weights: tuple[Weight, ...] = ()
    min_score: float | None = None


@dataclass(frozen=True)
class Block:
    id: str
    when: When | None = None
    weights: tuple[Weight, ...] = ()
    min_score: float | None = None


@dataclass(frozen=True)
class NLOption:
    key: str
    text: str
    target: Node
    capability_model: str | None = None


@dataclass(frozen=True)
class Branch:
    id: str
    children: tuple[Node, ...] = ()
    otherwise: Node | None = None
    when: When | None = None


@dataclass(frozen=True)
class NLChoice:
    id: str
    instructions: str
    options: tuple[NLOption, ...] = ()
    min_prob: float = DEFAULT_MIN_PROB
    min_margin: float = DEFAULT_MIN_MARGIN
    otherwise: Node | None = None
    when: When | None = None


Node = Route | Block | Branch | NLChoice


@dataclass(frozen=True)
class RoutingTree:
    id: str
    name: str
    enabled: bool
    root: Node
    tree_revision: str = ""
    updated_at: str = ""


# Fields that belong to another node type.  They cannot be represented by the
# frozen dataclass, so they must be rejected here instead of silently dropped.
_FOREIGN_FIELDS: dict[str, tuple[str, ...]] = {
    "route": ("children", "otherwise", "instructions", "options",
              "min_prob", "min_margin"),
    "block": ("model", "reasoning", "children", "otherwise", "instructions",
              "options", "min_prob", "min_margin"),
    "branch": ("model", "reasoning", "instructions", "options", "min_prob",
               "min_margin", "min_score", "weights"),
    "nl_choice": ("model", "reasoning", "children", "min_score", "weights"),
}


def _unit(raw, name: str) -> float:
    """A probability-like number in [0, 1] (bools, NaN and inf rejected)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a number in [0,1], got {raw!r}")
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a number in [0,1], got {raw!r}")
    return value


def _optional_unit(raw, name: str) -> float | None:
    return None if raw is None else _unit(raw, name)


def _node_id(raw: dict) -> str:
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError(f"node needs a nonempty string id: {raw!r}")
    return node_id


def _when_from_dict(raw) -> When:
    if not isinstance(raw, dict):
        raise ValueError(f"when must be an object: {raw!r}")
    signal = raw.get("signal")
    if not isinstance(signal, str) or not signal:
        raise ValueError(f"when needs a nonempty string signal: {raw!r}")
    value = raw.get("value", "")
    if not isinstance(value, str):
        raise ValueError(f"when value must be a string: {raw!r}")
    op = raw.get("op") or "eq"
    if op not in WHEN_OPS:
        raise ValueError(f"when op must be one of {WHEN_OPS}: {raw!r}")
    return When(signal=signal, value=value, op=op,
                conf_min=_optional_unit(raw.get("conf_min"), "conf_min"))


def _weight_from_dict(raw) -> Weight:
    if not isinstance(raw, dict):
        raise ValueError(f"weight must be an object: {raw!r}")
    signal = raw.get("signal")
    if not isinstance(signal, str) or not signal:
        raise ValueError(f"weight needs a nonempty string signal: {raw!r}")
    value = raw.get("value", "")
    if not isinstance(value, str):
        raise ValueError(f"weight value must be a string: {raw!r}")
    raw_weight = raw.get("weight")
    return Weight(signal=signal, value=value,
                  weight=DEFAULT_WEIGHT if raw_weight is None
                  else _unit(raw_weight, "weight"))


def _weights_from_dict(raw) -> tuple[Weight, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"weights must be a list: {raw!r}")
    return tuple(_weight_from_dict(item) for item in raw)


def _children_from_dict(raw) -> tuple[Node, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"children must be a list: {raw!r}")
    return tuple(node_from_dict(child) for child in raw)


def _options_list(raw, parent_id: str) -> tuple[NLOption, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"nl options must be a list: {raw!r}")
    return tuple(_option_from_dict(option, parent_id) for option in raw)


def _reject_foreign_fields(node_type: str, node_id: str, raw: dict) -> None:
    for key in _FOREIGN_FIELDS[node_type]:
        if key in raw and raw[key] is not None:
            raise ValueError(
                f"{node_type} node {node_id} cannot carry {key!r}")


def _option_from_dict(raw: dict, parent_id: str) -> NLOption:
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"]:
        raise ValueError("nl option needs a nonempty string key")
    has_model = "model" in raw
    has_child = "child" in raw
    if has_model == has_child:
        raise ValueError("nl option needs exactly one of model or child")
    if has_model:
        if not isinstance(raw["model"], str) or not raw["model"]:
            raise ValueError("nl model option needs a nonempty model")
        target = Route(
            id=f"{parent_id}/options/{raw['key']}",
            model=raw["model"], reasoning=raw.get("reasoning"))
        return NLOption(key=raw["key"], text="", target=target,
                        capability_model=raw["model"])
    if not isinstance(raw.get("description"), str) or not raw["description"].strip():
        raise ValueError("nl child option needs a nonempty description")
    return NLOption(
        key=raw["key"], text=raw["description"].strip(),
        target=node_from_dict(raw["child"]), capability_model=None)


def node_from_dict(raw) -> Node:
    """Parse one node (terminal or container) from its JSON object form."""
    if not isinstance(raw, dict):
        raise ValueError(f"node must be an object: {raw!r}")
    node_id = _node_id(raw)
    node_type = raw.get("type")
    if node_type not in NODE_TYPES:
        raise ValueError(f"node type must be one of {NODE_TYPES}: {raw!r}")
    _reject_foreign_fields(node_type, node_id, raw)
    when = None if raw.get("when") is None else _when_from_dict(raw["when"])

    if node_type == "route":
        model = raw.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError(f"route node {node_id} needs a nonempty model")
        reasoning = raw.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError(
                f"route node {node_id} reasoning must be a string or null")
        return Route(id=node_id, model=model, reasoning=reasoning, when=when,
                     weights=_weights_from_dict(raw.get("weights")),
                     min_score=_optional_unit(raw.get("min_score"), "min_score"))

    if node_type == "block":
        return Block(id=node_id, when=when,
                     weights=_weights_from_dict(raw.get("weights")),
                     min_score=_optional_unit(raw.get("min_score"), "min_score"))

    otherwise = None if raw.get("otherwise") is None \
        else node_from_dict(raw["otherwise"])
    if node_type == "branch":
        return Branch(id=node_id, children=_children_from_dict(raw.get("children")),
                      otherwise=otherwise, when=when)

    instructions = raw.get("instructions")
    if instructions is None:
        instructions = ""
    elif not isinstance(instructions, str):
        raise ValueError(
            f"nl_choice node {node_id} instructions must be a string")
    min_prob = raw.get("min_prob")
    min_margin = raw.get("min_margin")
    return NLChoice(
        id=node_id, instructions=instructions,
        options=_options_list(raw.get("options"), node_id),
        min_prob=DEFAULT_MIN_PROB if min_prob is None
        else _unit(min_prob, "min_prob"),
        min_margin=DEFAULT_MIN_MARGIN if min_margin is None
        else _unit(min_margin, "min_margin"),
        otherwise=otherwise, when=when)


def _when_to_dict(when: When) -> dict:
    encoded = {"signal": when.signal, "value": when.value, "op": when.op}
    if when.conf_min is not None:
        encoded["conf_min"] = when.conf_min
    return encoded


def _weight_to_dict(weight: Weight) -> dict:
    return {"signal": weight.signal, "value": weight.value,
            "weight": weight.weight}


def _node_head(node: Node, node_type: str) -> dict:
    encoded = {"id": node.id, "type": node_type}
    if node.when is not None:
        encoded["when"] = _when_to_dict(node.when)
    return encoded


def _option_to_dict(option: NLOption) -> dict:
    if option.capability_model is not None:
        if not isinstance(option.target, Route):
            raise ValueError(
                f"direct model option {option.key!r} must target a route")
        return {"key": option.key, "model": option.target.model,
                "reasoning": option.target.reasoning}
    return {"key": option.key, "description": option.text,
            "child": node_to_dict(option.target)}


def node_to_dict(node: Node) -> dict:
    """Encode one node back to the canonical JSON object form."""
    if isinstance(node, Route):
        encoded = _node_head(node, "route")
        encoded["model"] = node.model
        encoded["reasoning"] = node.reasoning
        encoded["weights"] = [_weight_to_dict(w) for w in node.weights]
        if node.min_score is not None:
            encoded["min_score"] = node.min_score
        return encoded
    if isinstance(node, Block):
        encoded = _node_head(node, "block")
        encoded["weights"] = [_weight_to_dict(w) for w in node.weights]
        if node.min_score is not None:
            encoded["min_score"] = node.min_score
        return encoded
    if isinstance(node, Branch):
        encoded = _node_head(node, "branch")
        encoded["children"] = [node_to_dict(child) for child in node.children]
        if node.otherwise is not None:
            encoded["otherwise"] = node_to_dict(node.otherwise)
        return encoded
    if isinstance(node, NLChoice):
        encoded = _node_head(node, "nl_choice")
        encoded["instructions"] = node.instructions
        encoded["min_prob"] = node.min_prob
        encoded["min_margin"] = node.min_margin
        encoded["options"] = [_option_to_dict(option) for option in node.options]
        if node.otherwise is not None:
            encoded["otherwise"] = node_to_dict(node.otherwise)
        return encoded
    raise TypeError(f"not a routing tree node: {node!r}")


def walk_nodes(root: Node) -> Iterator[Node]:
    """Yield ``root`` and every reachable node, in configuration order.

    Every out-edge is followed exactly once: ``Branch.children`` in order,
    ``Branch.otherwise``, every ``NLChoice.options[].target`` in order (which
    includes the auto-built route of a direct ``model`` option) and
    ``NLChoice.otherwise``.  ``otherwise`` is emitted after all ordinary edges.
    """
    if not isinstance(root, (Route, Block, Branch, NLChoice)):
        raise TypeError(f"not a routing tree node: {root!r}")
    yield root
    if isinstance(root, Branch):
        for child in root.children:
            yield from walk_nodes(child)
        if root.otherwise is not None:
            yield from walk_nodes(root.otherwise)
    elif isinstance(root, NLChoice):
        for option in root.options:
            yield from walk_nodes(option.target)
        if root.otherwise is not None:
            yield from walk_nodes(root.otherwise)


def _tree_text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"tree {key} must be a string")
    return value


def tree_from_dict(raw) -> RoutingTree:
    """Parse a saved tree envelope (``{id,name,enabled,root,...}``)."""
    if not isinstance(raw, dict):
        raise ValueError(f"tree must be an object: {raw!r}")
    tree_id = raw.get("id")
    if not isinstance(tree_id, str) or not tree_id:
        raise ValueError(f"tree needs a nonempty string id: {raw!r}")
    if raw.get("root") is None:
        raise ValueError(f"tree {tree_id} needs a root node")
    name = raw.get("name")
    if name is None:
        name = ""
    elif not isinstance(name, str):
        raise ValueError(f"tree {tree_id} name must be a string")
    enabled = raw.get("enabled")
    if enabled is None:
        enabled = False
    elif isinstance(enabled, bool):
        pass
    elif isinstance(enabled, int) and enabled in (0, 1):
        # SQLite stores the v19 column as INTEGER 0/1; accept its rows as input.
        enabled = bool(enabled)
    else:
        raise ValueError(f"tree {tree_id} enabled must be a boolean")
    return RoutingTree(
        id=tree_id, name=name, enabled=enabled, root=node_from_dict(raw["root"]),
        tree_revision=_tree_text(raw, "tree_revision"),
        updated_at=_tree_text(raw, "updated_at"))


def tree_to_dict(tree: RoutingTree) -> dict:
    """Encode a tree envelope; the root carries no server-only fields."""
    return {"id": tree.id, "name": tree.name, "enabled": tree.enabled,
            "root": node_to_dict(tree.root),
            "tree_revision": tree.tree_revision,
            "updated_at": tree.updated_at}


def tree_revision_of(root: Node) -> str:
    """Canonical content revision of a tree root (design doc §8.2).

    ``tree_revision`` covers the tree structure and every behaviour field
    (``when``, instructions, candidate keys/order/thresholds, model references,
    branch descriptions, reasoning, weights, ``otherwise``) and NOT the
    envelope (id/name/enabled/updated_at). The canonical text is UTF-8 JSON
    with ``ensure_ascii=False``, sorted keys and compact separators; arrays
    keep their configuration order — candidate order is behaviour and must
    never be re-sorted by compilation, import/export or logging.

    ``node_to_dict`` is canonical (serialising the parsed tree twice is
    identical), so this text is also what the store persists as
    ``config_revisions(kind='tree')`` content: the revision can be re-hashed
    offline from the stored content.
    """
    content = json.dumps(node_to_dict(root), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
