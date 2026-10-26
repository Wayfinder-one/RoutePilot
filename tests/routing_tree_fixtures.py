"""Shared dataclass builders for unified-routing-tree tests.

JSON-shaped fixtures and dataclass builders stay separate on purpose:
``tests/test_unified_tree_model.py::mixed_root`` exercises the parser, while
this module builds the frozen dataclasses that the engine, runtime, validation
and API tests evaluate.  Keep it dataclass-only — do not add ``*_from_dict``
helpers here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from router.pipeline.tree import (
    Branch,
    NLChoice,
    NLOption,
    Node,
    Route,
    RoutingTree,
    Weight,
    When,
)


def route(node_id: str, model: str, when: When | None = None) -> Route:
    """A terminal ``route`` node, optionally gated by ``when``."""
    return Route(id=node_id, model=model, when=when)


def nl(node_id: str, options: Iterable[NLOption],
       otherwise: Node | None = None) -> NLChoice:
    """A natural-language choice with a nonempty default question."""
    return NLChoice(id=node_id, instructions=f"choose the path for {node_id}",
                    options=tuple(options), otherwise=otherwise)


def _model_option(owner_id: str, key: str, model: str) -> NLOption:
    """A direct ``model`` option exactly as the parser builds it in memory."""
    return NLOption(key=key, text="",
                    target=Route(id=f"{owner_id}/options/{key}", model=model),
                    capability_model=model)


def mixed_tree() -> RoutingTree:
    """信号 + 自然语言混合树: PII/time gated NL and routes, weighted leaf, fallback.

    Covers every out-edge kind (branch children, NL direct-model and child
    options, branch/NL ``otherwise``) plus the signal kinds scope tests need
    (model ``pii``/``complexity``, rule ``time_window``).
    """
    deep = nl("deep-nl", (
        _model_option("deep-nl", "code", "qwen3-32b"),
        _model_option("deep-nl", "reasoning", "deepseek-v3"),
    ), otherwise=route("deep-fallback", "qwen3-8b"))
    pii = replace(
        nl("pii-nl", (
            _model_option("pii-nl", "fast", "qwen3-8b"),
            NLOption(key="deep", text="需要多步推理、复杂代码或架构分析的任务",
                     target=deep),
        ), otherwise=route("pii-fallback", "qwen3-8b")),
        when=When(signal="pii", op="contains", value="email_address"))
    office_hours = route("office-hours", "qwen3-8b",
                         when=When(signal="time_window", op="contains",
                                   value="work_hours"))
    weighted = Route(
        id="complex-route", model="qwen3-32b",
        weights=(Weight(signal="complexity", value="hard", weight=0.8),),
        min_score=0.6)
    return RoutingTree(
        id="mixed-tree", name="Mixed tree", enabled=True,
        root=Branch(id="root", children=(pii, office_hours, weighted),
                    otherwise=route("fallback", "qwen3-8b")))
