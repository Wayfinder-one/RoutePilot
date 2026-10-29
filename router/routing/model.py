from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class When:
    periods: tuple[str, ...]


@dataclass(frozen=True)
class ModelTarget:
    model: str
    reasoning: str | None = None


@dataclass(frozen=True)
class Target:
    id: str
    models: tuple[ModelTarget, ...] = ()
    strategy: str = "least_inflight"
    capability: str = ""
    when: When | None = None


@dataclass(frozen=True)
class JevOption:
    key: str
    label: str
    target: Target


@dataclass(frozen=True)
class Jev:
    id: str
    instructions: str
    options: tuple[JevOption, ...] = ()
    fallback: Target | None = None
    min_prob: float = 0.0
    min_margin: float = 0.0
    when: When | None = None


@dataclass(frozen=True)
class Branch:
    id: str
    children: tuple[Node, ...] = ()
    otherwise: Node | None = None
    when: When | None = None


Node = Branch | Jev | Target


@dataclass(frozen=True)
class Sticky:
    enabled: bool = False
    ttl_seconds: int = 1800


@dataclass(frozen=True)
class RoutingTree:
    id: str
    name: str
    enabled: bool
    root: Node
    sticky: Sticky = Sticky()
    timezone: str = "Asia/Shanghai"
    tree_revision: str = ""
    updated_at: str = ""


def _unknown(kind: str, raw: dict, allowed: set[str]) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{kind} has unknown fields: {', '.join(unknown)}")


def _text(value, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{field} must be a {'string' if empty else 'nonempty string'}")
    return value


def _unit(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number in [0,1]")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be a number in [0,1]")
    return number


def _node_id(raw: dict) -> str:
    return _text(raw.get("id"), "node id")


def _when_from_dict(raw) -> When | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("when must be an object")
    _unknown("when", raw, {"periods"})
    periods = raw.get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("when.periods must be a nonempty list")
    return When(tuple(_text(period, "period name") for period in periods))


def _model_target_from_dict(raw) -> ModelTarget:
    if not isinstance(raw, dict):
        raise ValueError("target model must be an object")
    _unknown("target model", raw, {"model", "reasoning"})
    reasoning = raw.get("reasoning")
    if reasoning is not None:
        reasoning = _text(reasoning, "reasoning")
    return ModelTarget(_text(raw.get("model"), "model"), reasoning)


def _target_from_dict(raw: dict, when: When | None) -> Target:
    _unknown("target", raw, {"id", "type", "models", "strategy", "capability", "when"})
    models = raw.get("models", [])
    if not isinstance(models, list):
        raise ValueError("target.models must be a list")
    strategy = raw.get("strategy", "least_inflight")
    if strategy != "least_inflight":
        raise ValueError("target.strategy must be least_inflight")
    return Target(
        id=_node_id(raw),
        models=tuple(_model_target_from_dict(model) for model in models),
        strategy=strategy,
        capability=_text(raw.get("capability", ""), "capability", empty=True),
        when=when,
    )


def _option_from_dict(raw) -> JevOption:
    if not isinstance(raw, dict):
        raise ValueError("jev option must be an object")
    _unknown("jev option", raw, {"key", "label", "target"})
    target = node_from_dict(raw.get("target"))
    if not isinstance(target, Target):
        raise ValueError("jev option target must be a target node")
    return JevOption(
        key=_text(raw.get("key"), "option key"),
        label=_text(raw.get("label"), "option label"),
        target=target,
    )


def node_from_dict(raw) -> Node:
    if not isinstance(raw, dict):
        raise ValueError("node must be an object")
    node_type = raw.get("type")
    when = _when_from_dict(raw.get("when"))
    if node_type == "target":
        return _target_from_dict(raw, when)
    if node_type == "jev":
        _unknown("jev", raw, {"id", "type", "instructions", "options", "fallback", "min_prob", "min_margin", "when"})
        options = raw.get("options", [])
        if not isinstance(options, list):
            raise ValueError("jev.options must be a list")
        fallback = raw.get("fallback")
        parsed_fallback = node_from_dict(fallback) if fallback is not None else None
        if parsed_fallback is not None and not isinstance(parsed_fallback, Target):
            raise ValueError("jev fallback must be a target node")
        return Jev(
            id=_node_id(raw),
            instructions=_text(raw.get("instructions"), "instructions"),
            options=tuple(_option_from_dict(option) for option in options),
            fallback=parsed_fallback,
            min_prob=_unit(raw.get("min_prob", 0.0), "min_prob"),
            min_margin=_unit(raw.get("min_margin", 0.0), "min_margin"),
            when=when,
        )
    if node_type == "branch":
        _unknown("branch", raw, {"id", "type", "children", "otherwise", "when"})
        children = raw.get("children", [])
        if not isinstance(children, list):
            raise ValueError("branch.children must be a list")
        otherwise = raw.get("otherwise")
        return Branch(
            id=_node_id(raw),
            children=tuple(node_from_dict(child) for child in children),
            otherwise=node_from_dict(otherwise) if otherwise is not None else None,
            when=when,
        )
    raise ValueError(f"unknown node type {node_type!r}")


def _when_to_dict(when: When | None) -> dict | None:
    return None if when is None else {"periods": list(when.periods)}


def node_to_dict(node: Node) -> dict:
    result: dict = {"id": node.id, "type": node.__class__.__name__.lower()}
    if node.when is not None:
        result["when"] = _when_to_dict(node.when)
    if isinstance(node, Target):
        result.update({
            "strategy": node.strategy,
            "capability": node.capability,
            "models": [{"model": item.model, "reasoning": item.reasoning} for item in node.models],
        })
    elif isinstance(node, Jev):
        result.update({
            "instructions": node.instructions,
            "min_prob": node.min_prob,
            "min_margin": node.min_margin,
            "options": [{"key": option.key, "label": option.label, "target": node_to_dict(option.target)} for option in node.options],
            "fallback": node_to_dict(node.fallback) if node.fallback is not None else None,
        })
    elif isinstance(node, Branch):
        result.update({
            "children": [node_to_dict(child) for child in node.children],
            "otherwise": node_to_dict(node.otherwise) if node.otherwise is not None else None,
        })
    else:
        raise TypeError(f"unsupported node {node!r}")
    return result


def _sticky_from_dict(raw) -> Sticky:
    if raw is None:
        return Sticky()
    if not isinstance(raw, dict):
        raise ValueError("sticky must be an object")
    _unknown("sticky", raw, {"enabled", "ttl_seconds"})
    enabled = raw.get("enabled", False)
    ttl = raw.get("ttl_seconds", 1800)
    if not isinstance(enabled, bool):
        raise ValueError("sticky.enabled must be a boolean")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 31536000:
        raise ValueError("sticky.ttl_seconds must be an integer in [1,31536000]")
    return Sticky(enabled, ttl)


def tree_from_dict(raw) -> RoutingTree:
    if not isinstance(raw, dict):
        raise ValueError("tree must be an object")
    _unknown("tree", raw, {"id", "name", "enabled", "timezone", "sticky", "root", "tree_revision", "updated_at"})
    root = node_from_dict(raw.get("root"))
    if root.when is not None:
        raise ValueError("root cannot have a when condition")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    return RoutingTree(
        id=_text(raw.get("id"), "tree id"),
        name=_text(raw.get("name", ""), "tree name", empty=True),
        enabled=enabled,
        root=root,
        sticky=_sticky_from_dict(raw.get("sticky")),
        timezone=_text(raw.get("timezone", "Asia/Shanghai"), "timezone"),
        tree_revision=_text(raw.get("tree_revision", ""), "tree_revision", empty=True),
        updated_at=_text(raw.get("updated_at", ""), "updated_at", empty=True),
    )


def tree_behavior_dict(tree: RoutingTree) -> dict:
    return {
        "timezone": tree.timezone,
        "sticky": {"enabled": tree.sticky.enabled, "ttl_seconds": tree.sticky.ttl_seconds},
        "root": node_to_dict(tree.root),
    }


def tree_to_dict(tree: RoutingTree) -> dict:
    return {
        "id": tree.id,
        "name": tree.name,
        "enabled": tree.enabled,
        **tree_behavior_dict(tree),
        "tree_revision": tree.tree_revision,
        "updated_at": tree.updated_at,
    }


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tree_revision_of(tree: RoutingTree) -> str:
    return hashlib.sha256(canonical_json(tree_behavior_dict(tree)).encode("utf-8")).hexdigest()


def walk_nodes(root: Node) -> Iterator[Node]:
    yield root
    if isinstance(root, Branch):
        for child in root.children:
            yield from walk_nodes(child)
        if root.otherwise is not None:
            yield from walk_nodes(root.otherwise)
    elif isinstance(root, Jev):
        for option in root.options:
            yield from walk_nodes(option.target)
        if root.fallback is not None:
            yield from walk_nodes(root.fallback)
