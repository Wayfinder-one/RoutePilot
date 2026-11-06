"""内置的模型提供方 / 预置模型目录（纯数据模块）。

契约见 ``docs/模型目录与推理配置-2026-10-21.md`` §8。这里**只放数据**，不 import
``router.reasoning``（避免循环依赖）；结构的合法性由 ``tests/test_presets.py`` 对照
``router.reasoning.FORMAT_IDS`` 校验。

每个提供方：

    {id, name, base_url, key_env, reasoning_format, models: (model, ...)}

每个模型：

    {id, name, context_window?, max_tokens?, tier?, domains?, profile?}

``profile`` 与落库的 ``models.profile`` 同构（可含 ``reasoning`` 声明）；
``base_url`` 是 OpenAI 兼容前缀（router 会在其后拼 ``/chat/completions`` 与 ``/models``），
因此**不能**带尾斜杠、``/chat/completions`` 或 ``/models``。

数据来源（2026-10-21 核对）：

- 模型 id / 名称 / 上下文 / 输出上限：DSH 依赖的 pi-ai 0.82.1 目录 JSON
  （``~/work/deepseek-harness/node_modules/.pnpm/@earendil-works+pi-ai@0.82.1*/``
  ``dist/providers/data/*.json``）+ 各家官方文档；
  比目录更新的当季模型（GPT-6 Astra、Grok 4.7、GLM-5.3、Claude *-5-5、MiniMax-M3.1-Flash、
  DeepSeek V4.1 Flash 等）按官方文档 2026-10 版本补齐。
- 推理声明只写能确证协议的（见 §4 的 format 表）：OpenAI/DeepSeek/Qwen/Z.ai/OpenRouter/
  Together/Kimi/Gemini 的「档位→协议拼写」均已核对官方文档或 pi-ai 的 ``thinkingLevelMap``；
  拿不准的模型**不写** ``reasoning``（UI 显示「未声明」，只允许透传，用户可自定义）。
  明确不具备推理能力的模型写 ``reasoning: false``。
- 下文每个提供方前的注释记录了该家的关键事实与降级点。

几处刻意的取舍：

- ``anthropic`` 用的是 Anthropic 官方的 OpenAI 兼容入口（``/v1/chat/completions``）；该兼容层
  不暴露 thinking 控制（``reasoning_effort`` 不被支持/被忽略），所以 4 个 Claude 模型都
  **不声明**推理能力，而不是编造档位。
- ``moonshot`` 的 kimi-k2.7-code 系列始终思考、传 ``thinking.type=disabled`` 会报错，
  因此不声明档位（声明 off 会让用户选中后 400）；kimi-k3 用顶层 ``reasoning_effort``
  （openai 方言），所以是模型级 ``format`` 覆盖。
- ``minimax`` 官方模型列表（2026-10）只剩两个可用 LLM（MiniMax-M3.1-Flash / MiniMax-M3），
  已退役的 M2.x 不收录，所以该提供方只有 2 个预置模型（tests 里显式放宽下限）。
- ``local`` 的 base 按契约固定为 ``http://127.0.0.1:8000/v1``、无 key；模型 id 是 vLLM
  ``--served-model-name`` 的常见取值，实际部署按需改名。Qwen 系用 ``qwen-chat-template``
  （vLLM 的 ``chat_template_kwargs.enable_thinking``）。
- 带 ``/`` 的上游 id（OpenRouter / Together / Groq）是各家 API 的真实 id；控制台把它作为
  「模型位」默认值时需要做一次字符规整（§5.2 的模型位 id 不允许 ``/``）。
"""

from __future__ import annotations

PROVIDER_PRESETS: tuple[dict, ...] = (
    # ------------------------------------------------------------------ OpenAI
    # /v1/chat/completions + reasoning_effort。GPT-6 系不支持 "none"（官方 Using GPT-6 指南），
    # 所以 gpt-6-astra 不声明 off；GPT-5.6 / 5.5 / 5.4 的档位映射取自 pi-ai thinkingLevelMap
    # （minimal 不开放，xhigh 是显式值，max 只有 5.6 有）。
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "gpt-6-astra",
                "name": "GPT-6 Astra",
                "context_window": 1050000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gpt-5.6-luna",
                "name": "GPT-5.6 Luna",
                "context_window": 272000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": "none", "low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh", "max": "max"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gpt-5.6-sol",
                "name": "GPT-5.6 Sol",
                "context_window": 272000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": "none", "low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh", "max": "max"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gpt-5.5",
                "name": "GPT-5.5",
                "context_window": 272000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": "none", "low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gpt-5.4-mini",
                "name": "GPT-5.4 mini",
                "context_window": 400000,
                "max_tokens": 128000,
                "tier": "small",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "small",
                    "reasoning": {
                        "efforts": {"off": "none", "low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gpt-4.1",
                "name": "GPT-4.1",
                "context_window": 1047576,
                "max_tokens": 32768,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large", "reasoning": False},
            },
        ),
    },
    # --------------------------------------------------------------- Anthropic
    # 官方 OpenAI 兼容入口：https://api.anthropic.com/v1，router 拼 /chat/completions。
    # 兼容层只做消息/工具格式转换，不暴露 thinking 控制（reasoning_effort 不被支持），
    # 所以所有模型都不声明推理能力（未声明 = 只允许透传）。
    {
        "id": "anthropic",
        "name": "Anthropic (OpenAI 兼容入口)",
        "base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "claude-fable-5",
                "name": "Claude Fable 5",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large"},
            },
            {
                "id": "claude-opus-5-5",
                "name": "Claude Opus 5.5",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large"},
            },
            {
                "id": "claude-sonnet-5-5",
                "name": "Claude Sonnet 5.5",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large"},
            },
            {
                "id": "claude-haiku-4-5",
                "name": "Claude Haiku 4.5",
                "context_window": 200000,
                "max_tokens": 64000,
                "tier": "small",
                "domains": [],
                "profile": {"domains": [], "tier": "small"},
            },
        ),
    },
    # ---------------------------------------------------------------- DeepSeek
    # base 用 https://api.deepseek.com/v1（与 https://api.deepseek.com 等价，/v1 与模型版本无关）。
    # thinking.type 开关 + reasoning_effort（high/max）；1M 上下文、384K 输出。
    # deepseek-v4-flash 是官方文档标注「仍可调用」的兼容别名（实际由 V4.1-Flash 服务）。
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "reasoning_format": "deepseek",
        "models": (
            {
                "id": "deepseek-v4-pro",
                "name": "DeepSeek V4 Pro",
                "context_window": 1000000,
                "max_tokens": 384000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "high": "high", "max": "max"},
                        "default": "high",
                        "supports_reasoning_effort": True,
                    },
                },
            },
            {
                "id": "deepseek-flash",
                "name": "DeepSeek V4.1 Flash",
                "context_window": 1000000,
                "max_tokens": 384000,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"off": None, "high": "high", "max": "max"},
                        "default": "high",
                        "supports_reasoning_effort": True,
                    },
                },
            },
            {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash（兼容别名）",
                "context_window": 1000000,
                "max_tokens": 384000,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"off": None, "high": "high", "max": "max"},
                        "default": "high",
                        "supports_reasoning_effort": True,
                    },
                },
            },
        ),
    },
    # ---------------------------------------------------------- Moonshot / Kimi
    # OpenAI 兼容 base https://api.moonshot.cn/v1，K2.x 用 thinking.type（deepseek 方言），
    # kimi-k3 用顶层 reasoning_effort（openai 方言，low/high/max，默认 max）→ 模型级 format 覆盖。
    # kimi-k2.7-code 系列始终思考、传 disabled 会报错，故不声明档位。
    {
        "id": "moonshot",
        "name": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "reasoning_format": "deepseek",
        "models": (
            {
                "id": "kimi-k3",
                "name": "Kimi K3",
                "context_window": 1048576,
                "max_tokens": 131072,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "format": "openai",
                        "efforts": {"low": "low", "high": "high", "max": "max"},
                        "default": "max",
                    },
                },
            },
            {
                "id": "kimi-k2.6",
                "name": "Kimi K2.6",
                "context_window": 262144,
                "max_tokens": 262144,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                        "supports_reasoning_effort": False,
                    },
                },
            },
            {
                "id": "kimi-k2.7-code",
                "name": "Kimi K2.7 Code",
                "context_window": 262144,
                "max_tokens": 262144,
                "tier": "large",
                "domains": ["computer_science"],
                "profile": {"domains": ["computer_science"], "tier": "large"},
            },
            {
                "id": "kimi-k2.7-code-highspeed",
                "name": "Kimi K2.7 Code HighSpeed",
                "context_window": 262144,
                "max_tokens": 262144,
                "tier": "medium",
                "domains": ["computer_science"],
                "profile": {"domains": ["computer_science"], "tier": "medium"},
            },
        ),
    },
    # ----------------------------------------------------------- Qwen / 千问AI
    # 原 DashScope 兼容入口已迁到千问AI 平台的 maas 域名（官方文档 2026-10），
    # base_url = https://maas.qianwenaiapi.com/compatible-mode/v1（国际站
    # https://maas.qwencloudapi.com/compatible-mode/v1），key 仍是 DASHSCOPE_API_KEY。
    # 扩展参数 enable_thinking（官方明确 reasoning_effort 被静默忽略）→ qwen 方言只有开关两态。
    {
        "id": "qwen",
        "name": "Qwen / DashScope（千问AI平台）",
        "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "reasoning_format": "qwen",
        "models": (
            {
                "id": "qwen3.8-max",
                "name": "Qwen3.8 Max",
                "context_window": 1000000,
                "max_tokens": 131072,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
            {
                "id": "qwen3.7-max",
                "name": "Qwen3.7 Max",
                "context_window": 1000000,
                "max_tokens": 131072,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
            {
                "id": "qwen3.7-plus",
                "name": "Qwen3.7 Plus",
                "context_window": 1000000,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
            {
                "id": "qwen3.6-flash",
                "name": "Qwen3.6 Flash",
                "context_window": 1000000,
                "max_tokens": 65536,
                "tier": "small",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "small",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
        ),
    },
    # ----------------------------------------------------------- Z.ai / 智谱
    # 通用 API（非 Coding Plan）：https://api.z.ai/api/paas/v4，zai 方言：
    # thinking.type（enabled/disabled）+ reasoning_effort（high/max）。
    # GLM-5.3 / GLM-5.3-Flash 始终推理、不支持 disabled（传 disabled 会 400），故不声明 off；
    # GLM-5.2 支持 disabled，off 用 thinking.type=disabled 表达。
    {
        "id": "zai",
        "name": "Z.ai / 智谱",
        "base_url": "https://api.z.ai/api/paas/v4",
        "key_env": "ZAI_API_KEY",
        "reasoning_format": "zai",
        "models": (
            {
                "id": "glm-5.3",
                "name": "GLM-5.3",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "high": "high", "max": "max"},
                        "supports_reasoning_effort": True,
                    },
                },
            },
            {
                "id": "glm-5.3-flash",
                "name": "GLM-5.3 Flash",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"low": "low", "high": "high", "max": "max"},
                        "supports_reasoning_effort": True,
                    },
                },
            },
            {
                "id": "glm-5.2",
                "name": "GLM-5.2",
                "context_window": 1000000,
                "max_tokens": 131072,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "high": "high", "max": "max"},
                        "default": "high",
                        "supports_reasoning_effort": True,
                    },
                },
            },
        ),
    },
    # -------------------------------------------------------------- OpenRouter
    # openrouter 方言：reasoning.effort（非 off 档必须给 wire）。OpenRouter 把 effort
    # 翻译成上游模型的档位；"none" 关闭推理（Claude 系拒绝 none，故 Claude 不声明 off，
    # Gemini 3 的 thinking 是 mandatory，也不声明 off）。
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "reasoning_format": "openrouter",
        "models": (
            {
                "id": "openai/gpt-5.6-luna",
                "name": "OpenAI: GPT-5.6 Luna (via OpenRouter)",
                "context_window": 1050000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": "none", "low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "anthropic/claude-opus-5",
                "name": "Anthropic: Claude Opus 5 (via OpenRouter)",
                "context_window": 1000000,
                "max_tokens": 128000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "high",
                    },
                },
            },
            {
                "id": "deepseek/deepseek-v4-pro",
                "name": "DeepSeek: DeepSeek V4 Pro (via OpenRouter)",
                "context_window": 1048576,
                "max_tokens": 384000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": "none", "high": "high", "max": "max"},
                        "default": "high",
                    },
                },
            },
            {
                "id": "google/gemini-3.6-flash",
                "name": "Google: Gemini 3.6 Flash (via OpenRouter)",
                "context_window": 1048576,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "qwen/qwen3.7-max",
                "name": "Qwen: Qwen3.7 Max (via OpenRouter)",
                "context_window": 1000000,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
        ),
    },
    # -------------------------------------------------- Google Gemini（兼容入口）
    # 官方 OpenAI 兼容 base：https://generativelanguage.googleapis.com/v1beta/openai，
    # router 拼 /chat/completions。兼容层把 reasoning_effort 映射到 Gemini 的 thinkingLevel
    # （minimal/low/medium/high 同一套词表）；Gemini 3 Pro 只开放 low/high，
    # 3.1 Pro / Gemini 4 还有 medium；Flash 系列的思考档位不可控（不声明）。
    {
        "id": "gemini",
        "name": "Google Gemini (OpenAI 兼容入口)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "gemini-4-argon",
                "name": "Gemini 4 Argon",
                "context_window": 1048576,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "gemini-3.6-flash",
                "name": "Gemini 3.6 Flash",
                "context_window": 1048576,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium"},
            },
            {
                "id": "gemini-3.5-flash",
                "name": "Gemini 3.5 Flash",
                "context_window": 1048576,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium"},
            },
            {
                "id": "gemini-3.1-pro-preview",
                "name": "Gemini 3.1 Pro Preview",
                "context_window": 1048576,
                "max_tokens": 65536,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "high",
                    },
                },
            },
            {
                "id": "gemini-3-pro-preview",
                "name": "Gemini 3 Pro Preview",
                "context_window": 1048576,
                "max_tokens": 65536,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "high": "high"},
                        "default": "high",
                    },
                },
            },
        ),
    },
    # -------------------------------------------------------------------- xAI
    # https://api.x.ai/v1，OpenAI 风格 reasoning_effort（low/medium/high/xhigh，默认 high；
    # 官方 2026-10 reasoning 文档覆盖 grok-4.7/4.6/4.5）。grok-4.3 的说明里没有该参数，
    # 不声明档位（未声明 = 透传）。
    {
        "id": "xai",
        "name": "xAI",
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "grok-4.7",
                "name": "Grok 4.7",
                "context_window": 500000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "high",
                    },
                },
            },
            {
                "id": "grok-4.5",
                "name": "Grok 4.5",
                "context_window": 500000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium",
                                    "high": "high", "xhigh": "xhigh"},
                        "default": "high",
                    },
                },
            },
            {
                "id": "grok-4.3",
                "name": "Grok 4.3",
                "context_window": 1000000,
                "max_tokens": 30000,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large"},
            },
        ),
    },
    # ----------------------------------------------------------------- Together
    # https://api.together.ai/v1，together 方言：reasoning.enabled 开关
    # （DeepSeek-V4-Pro 还接受 reasoning_effort high/max）；GPT-OSS 走 OpenAI 风格
    # reasoning_effort，故用模型级 format=openai。
    {
        "id": "together",
        "name": "Together AI",
        "base_url": "https://api.together.ai/v1",
        "key_env": "TOGETHER_API_KEY",
        "reasoning_format": "together",
        "models": (
            {
                "id": "deepseek-ai/DeepSeek-V4-Pro",
                "name": "DeepSeek V4 Pro (via Together)",
                "context_window": 512000,
                "max_tokens": 384000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "high": "high", "max": "max"},
                        "default": "high",
                        "supports_reasoning_effort": True,
                    },
                },
            },
            {
                "id": "openai/gpt-oss-120b",
                "name": "GPT-OSS 120B (via Together)",
                "context_window": 131072,
                "max_tokens": 131072,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "format": "openai",
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "moonshotai/Kimi-K2.6",
                "name": "Kimi K2.6 (via Together)",
                "context_window": 262144,
                "max_tokens": 131000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                        "supports_reasoning_effort": False,
                    },
                },
            },
            {
                "id": "MiniMaxAI/MiniMax-M3",
                "name": "MiniMax M3 (via Together)",
                "context_window": 524288,
                "max_tokens": 250000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"off": None, "on": None},
                        "supports_reasoning_effort": False,
                    },
                },
            },
        ),
    },
    # --------------------------------------------------------------------- Groq
    # https://api.groq.com/openai/v1，GPT-OSS 支持 OpenAI 风格 reasoning_effort（low/medium/high）。
    # qwen3-32b 在 Groq 上只有 reasoning_effort none/default 两态，没映射到本目录档位，不声明。
    {
        "id": "groq",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "openai/gpt-oss-120b",
                "name": "GPT-OSS 120B (Groq)",
                "context_window": 131072,
                "max_tokens": 65536,
                "tier": "medium",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "medium",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "openai/gpt-oss-20b",
                "name": "GPT-OSS 20B (Groq)",
                "context_window": 131072,
                "max_tokens": 65536,
                "tier": "small",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "small",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high"},
                        "default": "medium",
                    },
                },
            },
            {
                "id": "llama-3.3-70b-versatile",
                "name": "Llama 3.3 70B Versatile",
                "context_window": 131072,
                "max_tokens": 32768,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium", "reasoning": False},
            },
            {
                "id": "qwen/qwen3-32b",
                "name": "Qwen3 32B (Groq)",
                "context_window": 131072,
                "max_tokens": 40960,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium"},
            },
        ),
    },
    # ------------------------------------------------------------------ Mistral
    # https://api.mistral.ai/v1 是 Mistral 的 OpenAI 兼容入口。新版 Medium/Small 的
    # reasoning 控制只在 Mistral 自己的 Conversations API 里有明确文档，这里不声明档位；
    # Large / Codestral 明确是非推理模型 → reasoning: false。
    {
        "id": "mistral",
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "mistral-large-latest",
                "name": "Mistral Large（latest）",
                "context_window": 262144,
                "max_tokens": 262144,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large", "reasoning": False},
            },
            {
                "id": "mistral-medium-latest",
                "name": "Mistral Medium（latest）",
                "context_window": 262144,
                "max_tokens": 262144,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium"},
            },
            {
                "id": "mistral-small-latest",
                "name": "Mistral Small（latest）",
                "context_window": 256000,
                "max_tokens": 256000,
                "tier": "small",
                "domains": [],
                "profile": {"domains": [], "tier": "small"},
            },
            {
                "id": "codestral-latest",
                "name": "Codestral（latest）",
                "context_window": 256000,
                "max_tokens": 4096,
                "tier": "medium",
                "domains": ["computer_science"],
                "profile": {"domains": ["computer_science"], "tier": "medium",
                            "reasoning": False},
            },
        ),
    },
    # ------------------------------------------------------------------ MiniMax
    # 官方 OpenAI 兼容入口：https://api.minimax.cn/v1（国际站 https://api.minimax.io/v1）。
    # MiniMax-M3.1-Flash 支持 OpenAI 风格 reasoning_effort（low/medium/high/xhigh/max，
    # 默认 max，不支持 none）；MiniMax-M3 无档位控制，不声明。
    # 注意：2026-10 官方模型列表只剩这两个可用 LLM（M2.x 已退役），所以本提供方只有 2 个
    # 预置模型（契约 §8 的 3~6 下限在此按实际模型列表放宽）。
    {
        "id": "minimax",
        "name": "MiniMax",
        "base_url": "https://api.minimax.cn/v1",
        "key_env": "MINIMAX_API_KEY",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "MiniMax-M3.1-Flash",
                "name": "MiniMax M3.1 Flash",
                "context_window": 1000000,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "efforts": {"low": "low", "medium": "medium", "high": "high",
                                    "xhigh": "xhigh", "max": "max"},
                        "default": "max",
                    },
                },
            },
            {
                "id": "MiniMax-M3",
                "name": "MiniMax M3",
                "context_window": 1000000,
                "tier": "large",
                "domains": [],
                "profile": {"domains": [], "tier": "large"},
            },
        ),
    },
    # ------------------------------------------------------- 本地 OpenAI 兼容服务
    # 契约 §8 固定：base http://127.0.0.1:8000/v1、无 key（key_env 为空串）。
    # 模型 id 取 vLLM 常见的 --served-model-name 默认值，实际部署按需改名。
    # Qwen3 在 vLLM 上的思考开关是 chat_template_kwargs.enable_thinking →
    # 模型级 format=qwen-chat-template（off/on 两态）；GPT-OSS 的注入方式取决于
    # 启动模板，未声明（透传）。
    {
        "id": "local",
        "name": "本地 OpenAI 兼容服务 (vLLM / Ollama)",
        "base_url": "http://127.0.0.1:8000/v1",
        "key_env": "",
        "reasoning_format": "openai",
        "models": (
            {
                "id": "qwen3-8b",
                "name": "Qwen3 8B（本地 vLLM）",
                "context_window": 131072,
                "max_tokens": 8192,
                "tier": "small",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "small",
                    "reasoning": {
                        "format": "qwen-chat-template",
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
            {
                "id": "qwen3-32b",
                "name": "Qwen3 32B（本地 vLLM）",
                "context_window": 40960,
                "max_tokens": 16384,
                "tier": "large",
                "domains": [],
                "profile": {
                    "domains": [],
                    "tier": "large",
                    "reasoning": {
                        "format": "qwen-chat-template",
                        "efforts": {"off": None, "on": None},
                        "default": "on",
                    },
                },
            },
            {
                "id": "gpt-oss-20b",
                "name": "GPT-OSS 20B（本地 vLLM）",
                "context_window": 131072,
                "max_tokens": 131072,
                "tier": "medium",
                "domains": [],
                "profile": {"domains": [], "tier": "medium"},
            },
        ),
    },
)


#: id -> 预置（同一份数据的索引；播种与控制台读的是同一处定义）。
PRESET_BY_ID: dict[str, dict] = {p["id"]: p for p in PROVIDER_PRESETS}
