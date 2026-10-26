"""Two-level validation for the unified routing tree.

Saving a tree (``POST/PUT /admin/trees``) runs only the *structural* checks: node
ids and natural-language candidate keys unique, root/``otherwise`` boundary
conditions, per-type field combinations and a bounded embedded tree.  Runtime
context (which models exist, which capabilities and reasoning levels they
declare, whether the DecisionRunner is available) is deliberately absent there,
so an incomplete **disabled** draft tree can still be stored (design §7.2).

Enabling a tree runs the same structural checks first and then the full
runtime-readiness check: signal references and values, route models, direct-model
capabilities, reasoning levels, DecisionRunner availability, the 2..255
candidate range for every ``nl_choice`` and a conservative guaranteed fallback.

The API layer maps ``level == 'hard'`` issues to a 422 response and must not
duplicate any of these rules.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from router.pipeline.tree import (
    Block,
    Branch,
    NLChoice,
    NLOption,
    Node,
    Route,
    RoutingTree,
    walk_nodes,
)
from router.reasoning import level_label
from router.signals.registry import SIGNALS

#: The tree is one embedded JSON document evaluated recursively; a runaway
#: nested/large tree is rejected at save time instead of blowing the evaluator
#: stack at request time (design §7.2: "树有界且无循环").
MAX_TREE_DEPTH = 128
MAX_TREE_NODES = 2048

#: Design §4.4: a natural-language node has 2..255 ordered candidates.
MIN_NL_OPTIONS = 2
MAX_NL_OPTIONS = 255

HARD = "hard"
WARN = "warn"

#: Structural problems that make the rest of the enable checks meaningless (and
#: unsafe to traverse): report them and stop instead of walking a broken tree.
_FATAL_STRUCTURE = frozenset({"missing_root", "invalid_node", "tree_too_deep",
                              "tree_too_large"})


@dataclass(frozen=True)
class Issue:
    """One validation finding; ``zh``/``en`` are shown by the console."""

    code: str
    zh: str
    en: str
    node_id: str = ""
    level: str = HARD


@dataclass(frozen=True)
class EnableContext:
    """Everything the enable-time checks need that is not in the tree itself."""

    known_models: frozenset[str] | set[str] = field(default_factory=frozenset)
    #: model_id -> capability text (``profile.capability``); empty text = missing.
    capabilities: Mapping[str, str] = field(default_factory=dict)
    #: model_id -> normalized reasoning object (``normalize_reasoning`` output).
    model_reasoning: Mapping[str, dict | None] = field(default_factory=dict)
    decision_runner_available: bool = False


def _issue(code: str, node_id: str, zh: str, en: str, *, level: str = HARD) -> Issue:
    return Issue(code=code, zh=zh, en=en, node_id=node_id, level=level)


def _is_unit(value) -> bool:
    """A probability-like number in [0, 1] (bools, NaN and inf rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and 0.0 <= number <= 1.0


# --------------------------------------------------------------------------
# Structure (save) validation
# --------------------------------------------------------------------------

def _structure_options(node: NLChoice, issues: list[Issue]) -> None:
    keys: set[str] = set()
    for option in node.options:
        if not isinstance(option, NLOption):
            issues.append(_issue("invalid_option", node.id,
                                 "自然语言候选不是合法选项",
                                 "A natural-language candidate is not a valid option"))
            continue
        if not isinstance(option.key, str) or not option.key:
            issues.append(_issue(
                "invalid_option", node.id,
                f"自然语言候选需要非空 key：{option.key!r}",
                f"An NL candidate needs a nonempty key, got {option.key!r}"))
        elif option.key in keys:
            issues.append(_issue(
                "duplicate_option_key", node.id,
                f"自然语言候选 key 重复：{option.key}",
                f"Duplicate NL candidate key: {option.key}"))
        else:
            keys.add(option.key)
        if option.capability_model is not None:
            if not isinstance(option.capability_model, str) \
                    or not option.capability_model:
                issues.append(_issue(
                    "invalid_option", node.id,
                    "直接模型候选的模型引用必须是非空字符串",
                    "A direct-model candidate needs a nonempty model reference"))
            if not isinstance(option.target, Route):
                issues.append(_issue(
                    "invalid_option", node.id,
                    f"直接模型候选 {option.key!r} 必须编译为 route 终止节点",
                    f"Direct-model candidate {option.key!r} must target a route"))


def validate_tree_structure(tree: RoutingTree) -> list[Issue]:
    """Save-time structural validation (no runtime context consulted)."""
    root = getattr(tree, "root", None)
    if root is None:
        return [Issue("missing_root", "树没有根节点", "The tree has no root node")]
    issues: list[Issue] = []
    seen_ids: set[str] = set()
    visited = 0
    too_large = False
    too_deep = False

    def visit(node, *, is_root: bool, is_otherwise: bool, depth: int) -> None:
        nonlocal visited, too_large, too_deep
        if depth > MAX_TREE_DEPTH:
            if not too_deep:
                too_deep = True
                issues.append(_issue(
                    "tree_too_deep", "",
                    f"树嵌套深度超过上限 {MAX_TREE_DEPTH}",
                    f"Tree nesting depth exceeds {MAX_TREE_DEPTH}"))
            return
        visited += 1
        if visited > MAX_TREE_NODES:
            if not too_large:
                too_large = True
                issues.append(_issue(
                    "tree_too_large", "",
                    f"树节点数超过上限 {MAX_TREE_NODES}",
                    f"Tree node count exceeds {MAX_TREE_NODES}"))
            return
        if not isinstance(node, (Route, Block, Branch, NLChoice)):
            issues.append(_issue("invalid_node", "",
                                 "节点不是合法的 route/block/branch/nl_choice",
                                 "Node is not one of route/block/branch/nl_choice"))
            return
        if node.id in seen_ids:
            issues.append(_issue("duplicate_node_id", node.id,
                                 f"节点 id 重复：{node.id}",
                                 f"Duplicate node id: {node.id}"))
        else:
            seen_ids.add(node.id)
        if is_root and node.when is not None:
            issues.append(_issue(
                "root_when", node.id,
                "根节点不能配置 when 条件（每个请求必须从同一入口开始）",
                "The root node cannot carry a when condition"))
        if is_otherwise and node.when is not None:
            issues.append(_issue(
                "otherwise_when", node.id,
                "otherwise 节点不能配置 when 条件",
                "An otherwise node cannot carry a when condition"))
        if isinstance(node, Route):
            if not isinstance(node.model, str) or not node.model:
                issues.append(_issue("missing_model", node.id,
                                     "路由节点没有配置模型",
                                     "A route node has no target model"))
        elif isinstance(node, Block):
            pass
        elif isinstance(node, Branch):
            for child in node.children:
                visit(child, is_root=False, is_otherwise=False, depth=depth + 1)
            if node.otherwise is not None:
                visit(node.otherwise, is_root=False, is_otherwise=True,
                      depth=depth + 1)
        else:
            _structure_options(node, issues)
            for option in node.options:
                if isinstance(option, NLOption):
                    visit(option.target, is_root=False, is_otherwise=False,
                          depth=depth + 1)
            if node.otherwise is not None:
                visit(node.otherwise, is_root=False, is_otherwise=True,
                      depth=depth + 1)

    visit(root, is_root=True, is_otherwise=False, depth=0)
    return issues


# --------------------------------------------------------------------------
# Enable-time validation
# --------------------------------------------------------------------------

def has_guaranteed_fallback(node: Node, *, is_root: bool = True) -> bool:
    """Conservative "clear final fallback" rule (design §7.2).

    Only a weightless terminal at the root, or a weightless terminal reachable
    through an unbroken ``otherwise`` chain from the root, guarantees a result.
    Ordinary children, NL candidates and weighted terminals never do.
    """
    if isinstance(node, (Route, Block)):
        return is_root and not node.weights
    if not isinstance(node, (Branch, NLChoice)):
        return False
    fallback = node.otherwise
    while isinstance(fallback, (Branch, NLChoice)):
        fallback = fallback.otherwise
    return isinstance(fallback, (Route, Block)) and not fallback.weights


def _check_condition(signal, value, op, conf_min, node_id: str,
                     issues: list[Issue]) -> None:
    spec = SIGNALS.get(signal) if isinstance(signal, str) else None
    if spec is None:
        issues.append(_issue("unknown_signal", node_id,
                             f"未知信号 {signal}",
                             f"Unknown signal {signal}"))
        return
    if not isinstance(value, str) or not value:
        issues.append(_issue("missing_value", node_id,
                             f"条件 {signal} 缺少取值",
                             f"Condition {signal} has no value"))
        value = ""
    if spec.multi:
        if op != "contains":
            issues.append(_issue(
                "invalid_op", node_id,
                f"多值信号 {signal} 必须使用 op='contains'",
                f"Multi-value signal {signal} must use op='contains'"))
    elif op != "eq":
        issues.append(_issue(
            "invalid_op", node_id,
            f"单值信号 {signal} 必须使用 op='eq'",
            f"Single-value signal {signal} must use op='eq'"))
    if value and spec.values:
        allowed = {entry[0] for entry in spec.values}
        if value not in allowed:
            issues.append(_issue(
                "unknown_signal_value", node_id,
                f"信号 {signal} 的取值 {value!r} 不在可选范围内",
                f"Value {value!r} is not valid for signal {signal}"))
    if conf_min is not None:
        if not _is_unit(conf_min):
            issues.append(_issue(
                "invalid_threshold", node_id,
                "置信门槛 conf_min 必须是 [0,1] 内的数值",
                "The confidence floor conf_min must be a number in [0,1]"))
        elif spec.kind == "rule":
            issues.append(_issue(
                "rule_signal_floor", node_id,
                f"规则信号 {signal} 是确定性的，置信门槛不会生效",
                f"Rule signal {signal} is deterministic; the confidence floor "
                f"never applies", level=WARN))


def _check_weight(signal, value, weight, node_id: str,
                  issues: list[Issue]) -> None:
    spec = SIGNALS.get(signal) if isinstance(signal, str) else None
    if spec is None:
        issues.append(_issue("unknown_signal", node_id,
                             f"未知信号 {signal}",
                             f"Unknown signal {signal}"))
        return
    if not isinstance(value, str) or not value:
        issues.append(_issue("missing_value", node_id,
                             f"加权条件 {signal} 缺少取值",
                             f"Weighted condition {signal} has no value"))
    elif spec.values and value not in {entry[0] for entry in spec.values}:
        issues.append(_issue(
            "unknown_signal_value", node_id,
            f"信号 {signal} 的取值 {value!r} 不在可选范围内",
            f"Value {value!r} is not valid for signal {signal}"))
    if not _is_unit(weight):
        issues.append(_issue(
            "invalid_threshold", node_id,
            "加权条件的权重必须是 [0,1] 内的数值",
            "A weight must be a number in [0,1]"))


def _check_terminal(node: Route | Block, issues: list[Issue]) -> None:
    for weight in node.weights:
        _check_weight(weight.signal, weight.value, weight.weight, node.id,
                      issues)
    if node.min_score is not None and not _is_unit(node.min_score):
        issues.append(_issue(
            "invalid_threshold", node.id,
            "达成率门槛 min_score 必须是 [0,1] 内的数值",
            "The attainment floor min_score must be a number in [0,1]"))


def _declared_levels(capability: dict | None) -> list[str]:
    if not isinstance(capability, dict) \
            or not capability.get("declared") \
            or not capability.get("supported"):
        return []
    return [effort["id"] for effort in capability.get("efforts") or []
            if isinstance(effort, dict) and "id" in effort]


def _check_reasoning(model: str, reasoning, node_id: str,
                     context: EnableContext, issues: list[Issue],
                     *, option_key: str | None = None) -> None:
    levels = _declared_levels(context.model_reasoning.get(model))
    subject = f"候选 {option_key} 的模型 {model}" if option_key \
        else f"模型 {model}"
    english_subject = f"Candidate {option_key} model {model}" \
        if option_key else f"Model {model}"
    if not levels:
        issues.append(_issue(
            "reasoning_unsupported", node_id,
            f"{subject} 未声明/不支持推理，不能选择推理档位",
            f"{english_subject} does not declare reasoning support"))
    elif reasoning not in levels:
        issues.append(_issue(
            "reasoning_unsupported", node_id,
            f"{subject} 未声明推理档位 {level_label(reasoning)}"
            f"（{reasoning}）；可选：{', '.join(levels)}",
            f"{english_subject} does not declare reasoning level "
            f"{reasoning!r}; declared: {', '.join(levels)}"))


def _check_route(node: Route, context: EnableContext,
                 issues: list[Issue]) -> None:
    _check_terminal(node, issues)
    if not isinstance(node.model, str) or not node.model:
        return                                  # reported by the structure pass
    if node.model not in context.known_models:
        issues.append(_issue("unknown_model", node.id,
                             f"未知模型 {node.model}",
                             f"Unknown model {node.model}"))
    elif node.reasoning is not None:
        _check_reasoning(node.model, node.reasoning, node.id, context, issues)


def _check_option(option: NLOption, node: NLChoice, context: EnableContext,
                  issues: list[Issue]) -> None:
    """Direct-model candidates carry the model rules; child candidates the text rule."""
    if option.capability_model is None:
        if not isinstance(option.text, str) or not option.text.strip():
            issues.append(_issue(
                "missing_option_description", node.id,
                f"候选 {option.key} 缺少分支说明 description",
                f"Candidate {option.key} has no branch description"))
        return
    model = option.capability_model
    if not isinstance(model, str) or not model:
        return                                  # reported by the structure pass
    if model not in context.known_models:
        issues.append(_issue(
            "unknown_model", node.id,
            f"候选 {option.key} 引用了未知模型 {model}",
            f"Candidate {option.key} references unknown model {model}"))
        return
    capability = context.capabilities.get(model)
    if not isinstance(capability, str) or not capability.strip():
        issues.append(_issue(
            "missing_capability", node.id,
            f"候选 {option.key} 的模型 {model} 缺少能力说明",
            f"Candidate {option.key} model {model} has no capability text"))
    target = option.target
    if isinstance(target, Route) and target.reasoning is not None:
        # Task 2 handed the direct-model candidate `reasoning` check to this
        # layer: the auto-built target route must be judged like a real route.
        _check_reasoning(model, target.reasoning, node.id, context, issues,
                         option_key=option.key)


def _check_nl_choice(node: NLChoice, context: EnableContext,
                     issues: list[Issue]) -> None:
    if not isinstance(node.instructions, str) or not node.instructions.strip():
        issues.append(_issue("missing_instructions", node.id,
                             "自然语言节点缺少问题说明 instructions",
                             "An nl_choice node has no instructions"))
    count = len(node.options)
    if count == 0:
        issues.append(_issue("missing_options", node.id,
                             "自然语言节点没有候选",
                             "An nl_choice node has no candidates"))
    elif count < MIN_NL_OPTIONS:
        issues.append(_issue(
            "too_few_options", node.id,
            f"自然语言节点至少需要 {MIN_NL_OPTIONS} 个候选",
            f"An nl_choice node needs at least {MIN_NL_OPTIONS} candidates"))
    elif count > MAX_NL_OPTIONS:
        issues.append(_issue(
            "too_many_options", node.id,
            f"自然语言节点最多 {MAX_NL_OPTIONS} 个候选",
            f"An nl_choice node has at most {MAX_NL_OPTIONS} candidates"))
    if not _is_unit(node.min_prob):
        issues.append(_issue("invalid_threshold", node.id,
                             "最高候选概率门槛 min_prob 必须是 [0,1] 内的数值",
                             "min_prob must be a number in [0,1]"))
    if not _is_unit(node.min_margin):
        issues.append(_issue("invalid_threshold", node.id,
                             "前两名概率差门槛 min_margin 必须是 [0,1] 内的数值",
                             "min_margin must be a number in [0,1]"))
    for option in node.options:
        if isinstance(option, NLOption):
            _check_option(option, node, context, issues)


def validate_tree_for_enable(tree: RoutingTree,
                            context: EnableContext) -> list[Issue]:
    """Full runtime-readiness validation; structure issues are included first."""
    issues = validate_tree_structure(tree)
    root = getattr(tree, "root", None)
    if root is None or any(issue.code in _FATAL_STRUCTURE for issue in issues):
        return issues
    try:
        nodes = list(walk_nodes(root))
    except TypeError:
        # A malformed node type was already reported by the structure pass; the
        # remaining runtime checks can only run on a well-formed embedded tree.
        return issues

    # Direct-model candidates are compiled into auto-built terminal routes; their
    # model/capability/reasoning errors are reported on the *visible* nl_choice
    # node (the auto id does not exist in the editor), so skip those terminals
    # in the generic route pass.
    compiled_option_targets = {
        id(option.target)
        for node in nodes if isinstance(node, NLChoice)
        for option in node.options
        if isinstance(option, NLOption) and option.capability_model is not None}

    has_nl = False
    for node in nodes:
        if node.when is not None:
            _check_condition(node.when.signal, node.when.value, node.when.op,
                             node.when.conf_min, node.id, issues)
        if isinstance(node, Route):
            if id(node) not in compiled_option_targets:
                _check_route(node, context, issues)
        elif isinstance(node, Block):
            _check_terminal(node, issues)
        elif isinstance(node, NLChoice):
            has_nl = True
            _check_nl_choice(node, context, issues)

    if has_nl and not context.decision_runner_available:
        issues.append(_issue(
            "decision_runner_unavailable", "",
            "树包含自然语言节点，但 DecisionRunner 不可用",
            "The tree contains nl_choice nodes but no DecisionRunner is available"))
    if not has_guaranteed_fallback(root, is_root=True):
        issues.append(_issue(
            "missing_fallback", root.id,
            "树必须存在明确的最终兜底（无权重的根 route/block，"
            "或从根 otherwise 链到达的无权重 route/block）",
            "The tree needs a guaranteed final fallback (a weightless root "
            "route/block or one reachable through the otherwise chain)"))
    return issues
