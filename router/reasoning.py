"""推理（thinking / reasoning）能力声明与上行协议派发。

契约见 ``docs/模型目录与推理配置-2026-10-21.md`` §4。职责：

1. 校验 ``models.profile.reasoning``（档位表 + 协议拼写），把模型能力归一化成
   热路径/控制台使用的形状；
2. 按 ``format`` 把选中的档位写进 OpenAI 兼容请求体（``reasoning_effort`` /
   ``thinking`` / ``enable_thinking`` / ``reasoning.effort`` ...），并先剥离客户端
   自带的异构推理字段（路由配置胜出）。

本文件由 backend 任务实现函数体；下面的常量/元数据是 catalog 与 console 的公共契约，
不要改名。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

#: 合法档位 id（顺序 = UI 的固定展示顺序；具体模型只展示自己声明的档位）。
#: ``off`` = 关闭思考；``on`` = 只有开关两态的提供方（Qwen 等）的「开启」档。
LEVELS: tuple[str, ...] = (
    "off", "on", "minimal", "low", "medium", "high", "xhigh", "max",
)

#: 支持的推理协议方言（与 pi-ai 的 ``thinkingFormat`` 对齐；chat-template 任意透传不支持）。
FORMAT_IDS: tuple[str, ...] = (
    "openai", "deepseek", "qwen", "qwen-chat-template", "openrouter", "zai",
    "together", "string-thinking", "ant-ling",
)


@dataclass(frozen=True)
class FormatSpec:
    """一种推理协议方言的元数据。"""

    id: str
    #: 非 off 档位是否必须声明非空 wire 值（「档位→协议拼写」）。
    #: False 表示该方言靠开关语义工作，wire 可为 null（如 deepseek/qwen）。
    needs_wire: bool
    #: 该方言是否使用 ``supports_reasoning_effort`` 这个兼容开关。
    supports_sre: bool
    #: 控制台里的中文 / 英文说明（一句话，讲清注入什么字段）。
    zh: str
    en: str


#: format -> 元数据。``needs_wire`` / ``supports_sre`` 是校验与 UI 的契约。
FORMATS: dict[str, FormatSpec] = {
    "openai": FormatSpec("openai", True, True,
                         "OpenAI 风格：reasoning_effort", "OpenAI style: reasoning_effort"),
    "deepseek": FormatSpec("deepseek", False, True,
                           "DeepSeek 风格：thinking.type + reasoning_effort",
                           "DeepSeek style: thinking.type + reasoning_effort"),
    "qwen": FormatSpec("qwen", False, False,
                       "Qwen 风格：enable_thinking 布尔开关",
                       "Qwen style: enable_thinking boolean"),
    "qwen-chat-template": FormatSpec("qwen-chat-template", False, False,
                                     "vLLM/Qwen 模板：chat_template_kwargs.enable_thinking",
                                     "vLLM/Qwen template: chat_template_kwargs.enable_thinking"),
    "openrouter": FormatSpec("openrouter", True, False,
                             "OpenRouter：reasoning.effort",
                             "OpenRouter: reasoning.effort"),
    "zai": FormatSpec("zai", False, True,
                      "Z.ai / 智谱：thinking.type + reasoning_effort",
                      "Z.ai: thinking.type + reasoning_effort"),
    "together": FormatSpec("together", False, True,
                           "Together：reasoning.enabled + reasoning_effort",
                           "Together: reasoning.enabled + reasoning_effort"),
    "string-thinking": FormatSpec("string-thinking", True, False,
                                  "字符串 thinking 字段", "string thinking field"),
    "ant-ling": FormatSpec("ant-ling", True, False,
                           "Ant Ling：reasoning.effort", "Ant Ling: reasoning.effort"),
}

#: reasoning 对象里允许出现的键（未知键 → 422，避免拼写错误静默失效）。
_REASONING_KEYS = frozenset({"format", "efforts", "default",
                             "supports_reasoning_effort"})


class ReasoningError(ValueError):
    """非法的推理能力声明 / 档位请求；message 直接展示给控制台。"""


def level_label(level: str) -> str:
    """档位 id -> 展示名（中文；控制台用自己的 i18n，这个给服务端日志/错误用）。"""
    return {"off": "关闭", "on": "开启", "minimal": "极低", "low": "低",
            "medium": "中", "high": "高", "xhigh": "极高", "max": "最高"}.get(
                level, level)


def _effective_format(declared: dict, provider_format: str) -> str:
    """模型级 format 胜出；两者都非法 raise。"""
    fmt = declared.get("format", provider_format)
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise ReasoningError(
            f"非法推理协议 format {fmt!r}，合法值：{', '.join(FORMAT_IDS)}")
    return fmt


def _validate_efforts(efforts, spec: FormatSpec) -> dict:
    """校验「档位 → 协议拼写」有序 map，返回规范化副本。"""
    if not isinstance(efforts, dict) or not efforts:
        raise ReasoningError("reasoning.efforts 必须是非空对象（档位 → 协议拼写）")
    out: dict[str, str | None] = {}
    for level, wire in efforts.items():
        if level not in LEVELS:
            raise ReasoningError(
                f"非法推理档位 {level!r}，合法值：{', '.join(LEVELS)}")
        if wire is not None:
            if not isinstance(wire, str):
                raise ReasoningError(
                    f"档位 {level!r} 的协议拼写必须是字符串或 null，"
                    f"收到 {type(wire).__name__}")
            if not wire.strip():
                raise ReasoningError(
                    f"档位 {level!r} 的协议拼写不能为空字符串（不发值请用 null）")
        if level != "off" and spec.needs_wire and wire is None:
            raise ReasoningError(
                f"format {spec.id!r} 的档位 {level!r} 必须声明协议拼写（needs_wire）")
        out[level] = wire
    if not any(level != "off" for level in out):
        raise ReasoningError(
            "reasoning.efforts 至少要有一个非 off 档位；只支持关闭请用 reasoning: false")
    return out


def validate_model_profile(raw: dict, provider_format: str = "openai") -> dict:
    """校验并规整 ``models.profile``，非法 raise ``ReasoningError``。

    顶层键（domains/tier/...）原样保留；``reasoning`` 按 §3.2 校验并规整成
    ``{format?, efforts, default?, supports_reasoning_effort?}``，其中 ``format``
    只保留显式声明值（缺省时热路径用提供方的 ``reasoning_format``）。
    """
    if not isinstance(raw, dict):
        raise ReasoningError("profile 必须是 JSON 对象")
    out = dict(raw)
    if "reasoning" not in raw:
        return out
    declared = raw["reasoning"]
    if declared is None:                    # 显式 null 视同未声明
        out.pop("reasoning")
        return out
    if declared is False:
        out["reasoning"] = False
        return out
    if declared is True or not isinstance(declared, dict):
        raise ReasoningError("profile.reasoning 必须是对象或 false（false = 明确不支持）")

    unknown = set(declared) - _REASONING_KEYS
    if unknown:
        raise ReasoningError(
            f"profile.reasoning 含未知键：{', '.join(sorted(map(str, unknown)))}"
            f"（允许：{', '.join(sorted(_REASONING_KEYS))}）")

    spec = FORMATS[_effective_format(declared, provider_format)]
    supports_sre = True
    if "supports_reasoning_effort" in declared:
        value = declared["supports_reasoning_effort"]
        if not isinstance(value, bool):
            raise ReasoningError("reasoning.supports_reasoning_effort 必须是布尔值")
        if not spec.supports_sre:
            raise ReasoningError(
                f"format {spec.id!r} 不支持 supports_reasoning_effort 声明")
        supports_sre = value

    normalized: dict = {"efforts": _validate_efforts(declared.get("efforts"), spec)}
    if "format" in declared:
        normalized["format"] = spec.id
    if declared.get("default") is not None:
        default = declared["default"]
        if not isinstance(default, str) or default not in normalized["efforts"]:
            raise ReasoningError(
                f"reasoning.default {default!r} 必须是 efforts 里的档位之一："
                f"{', '.join(normalized['efforts'])}")
        normalized["default"] = default
    if "supports_reasoning_effort" in declared:
        normalized["supports_reasoning_effort"] = supports_sre
    out["reasoning"] = normalized
    return out


def _undeclared(provider_format: str, *, error: str | None = None) -> dict:
    """§3.2 的「未声明」归一化形状（console 依赖固定键）。"""
    out = {"declared": False, "supported": None,
           "format": provider_format if provider_format in FORMATS else "openai",
           "efforts": [], "default": None}
    if error is not None:
        out["error"] = error
    return out


def normalize_reasoning(profile: dict | None,
                        provider_format: str = "openai") -> dict | None:
    """把 ``profile`` 归一化成控制台/热路径形状（§3.2）；绝不抛异常。

    返回 ``None`` 表示 **未声明**（profile 里没有 reasoning 键），调用方负责
    套上 ``_undeclared`` 形状展示；``reasoning: false`` 返回 ``declared: true,
    supported: false``；非法声明返回 ``declared: false, supported: null, error``
    （老库兼容：热路径按未声明透传，服务照常启动）。
    """
    if not isinstance(profile, dict):
        return None
    if "reasoning" not in profile or profile["reasoning"] is None:
        return None
    declared = profile["reasoning"]
    fmt = provider_format if provider_format in FORMATS else "openai"
    if declared is False:
        return {"declared": True, "supported": False, "format": fmt,
                "efforts": [], "default": None}
    try:
        validated = validate_model_profile({"reasoning": declared},
                                          provider_format)
    except ReasoningError as exc:
        return _undeclared(fmt, error=str(exc))
    spec = FORMATS[validated["reasoning"].get("format", fmt)]
    efforts = [{"id": level, "wire": wire}
               for level, wire in validated["reasoning"]["efforts"].items()]
    return {"declared": True, "supported": True, "format": spec.id,
            "efforts": efforts,
            "default": validated["reasoning"].get("default"),
            "supports_reasoning_effort": bool(
                validated["reasoning"].get("supports_reasoning_effort",
                                           spec.supports_sre))}


def _strip_client_fields(body: dict) -> dict:
    """剥离客户端自带的异构推理字段（只保留路由配置的注入值）。"""
    body.pop("reasoning_effort", None)
    body.pop("reasoning", None)
    body.pop("thinking", None)
    body.pop("enable_thinking", None)
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict):
        ctk.pop("enable_thinking", None)
        if not ctk:
            body.pop("chat_template_kwargs", None)
    return body


def apply_reasoning(body: dict, capability: dict, level: str) -> dict:
    """按能力声明把档位写进请求体（先剥离客户端异构字段）；返回新 dict。

    非法 ``level`` / 能力缺失 / 档位未被该模型声明 → ``ReasoningError``；
    热路径捕获后降级透传（网关不能因为配置竞态把用户请求打挂）。
    """
    if level not in LEVELS:
        raise ReasoningError(f"非法推理档位 {level!r}，合法值：{', '.join(LEVELS)}")
    if not isinstance(capability, dict) or not capability.get("declared") \
            or not capability.get("supported"):
        raise ReasoningError("模型未声明/不支持推理")
    fmt = capability.get("format")
    if fmt not in FORMATS:
        raise ReasoningError(f"未知推理协议 format {fmt!r}")
    spec = FORMATS[fmt]
    efforts = {e.get("id"): e.get("wire")
               for e in capability.get("efforts") or []
               if isinstance(e, dict)}
    if level not in efforts:
        raise ReasoningError(
            f"模型未声明档位 {level_label(level)}（{level}）；"
            f"已声明：{', '.join(str(k) for k in efforts)}")
    wire = efforts[level]
    if level != "off" and spec.needs_wire and not (
            isinstance(wire, str) and wire.strip()):
        raise ReasoningError(
            f"format {fmt!r} 的档位 {level} 缺少协议拼写（needs_wire）")
    sre = spec.supports_sre and bool(
        capability.get("supports_reasoning_effort", True))

    out = _strip_client_fields(copy.deepcopy(body))
    on = level != "off"
    if fmt == "openai":
        # off：wire 是字符串才发送（例如 "none"），null 则只剥离不注入
        if on:
            out["reasoning_effort"] = wire
        elif isinstance(wire, str) and wire:
            out["reasoning_effort"] = wire
    elif fmt == "deepseek":
        if on:
            out["thinking"] = {"type": "enabled"}
            if sre:
                out["reasoning_effort"] = wire if wire is not None else level
        else:
            out["thinking"] = {"type": "disabled"}
    elif fmt == "qwen":
        out["enable_thinking"] = on
    elif fmt == "qwen-chat-template":
        ctk = out.get("chat_template_kwargs")
        if not isinstance(ctk, dict):
            ctk = {}
        ctk["enable_thinking"] = on
        out["chat_template_kwargs"] = ctk
    elif fmt == "openrouter":
        out["reasoning"] = {"effort": (wire if wire is not None else level)
                            if on else (wire if isinstance(wire, str) and wire
                                        else "none")}
    elif fmt == "zai":
        if on:
            out["thinking"] = {"type": "enabled", "clear_thinking": False}
            if sre:
                out["reasoning_effort"] = wire if wire is not None else level
        else:
            out["thinking"] = {"type": "disabled"}
    elif fmt == "together":
        if on:
            out["reasoning"] = {"enabled": True}
            if sre:
                out["reasoning_effort"] = wire if wire is not None else level
        else:
            out["reasoning"] = {"enabled": False}
    elif fmt == "string-thinking":
        out["thinking"] = wire if on else (
            wire if isinstance(wire, str) and wire else "none")
    elif fmt == "ant-ling":
        if on:                              # off 只剥离不发送
            out["reasoning"] = {"effort": wire}
    return out
