# Smart Router v2

LLM 请求的**信号计算 + 分流决策 + 控制台**。请求进入后端之前，router 先算出一包路由信号
（15 个：7 个模型信号 + 8 个规则信号），再按可编辑的分流树（pipeline）决定走哪个模型、或直接拦截。

默认推荐 **JEV 版主模型** 作为信号引擎：**Qwen3.5-0.8B 底座 + JEV 标量决策头**（LoRA 合并
导出），在 router 进程内按 JEV 标准（context + typed questions + candidates → 候选概率）输出
全部 7 个模型信号；问题/候选/真值定义 JSON 可配。RoutePilot checkpoint（多头）作为可回退引擎
保留。PII 信号另有确定性正则补充（`router/signals/pii_rules.py`）。

## 架构

```
用户请求 ──▶ FastAPI (/v1/chat/completions)
              │
              ├─ Tagger（二选一）
              │   ├─ checkpoint: RoutePilot-0.8B 一次前向 → 7 信号
              │   └─ jev:       Qwen3.5-0.8B JEV 头按问题作答 → 候选概率（推荐）
              │   + 8 个确定性规则信号（signals/rules.py）
              │
              ├─ Pipeline（分流树：每个站点一棵独立显式树，节点可增删、叶子可加权；
              │            存 SQLite JSON，可热更新）
              ├─ Upstream（OpenAI 兼容推理后端 + 熔断；按路由叶选择的推理档位
              │            注入各家协议字段：reasoning_effort / thinking / enable_thinking…）
              └─ RequestLog（决策摘要 → SQLite；信号/trace 只在内存 ring）
控制台（/console）：概览 / 分流树（信号库 + 树形编辑 + 试算侧滑）/ 计算日志 /
                    信号配置 / 模型与设置（内置模型目录 + 提供方 + 推理声明）
```

## 快速开始

```bash
# 本地开发（不加载模型）
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m router            # ROUTER_TAGGER=heuristic

# 测试
.venv/bin/python -m pytest tests/ -q

# JEV 模式（需要 torch/transformers/peft 与 merged 模型目录）
ROUTER_TAGGER=jev \
ROUTER_JEV_MODEL=/path/to/jev_merged_model \
ROUTER_SIGNAL_QUESTIONS=configs/signal_questions.json \
.venv/bin/python -m router
```

### 启动即体检

服务启动时会用库里的分流树跑一次健康检查（`router/pipeline/health.py`）：

- **硬错误**（未知信号/未知模型、节点或站点 id 重复、缺兜底…）→ **拒绝启动**并打印问题，
  避免「树跑不起来」变成线上 502；
- **警告**（配了但不会生效的字段：无权重叶的 `min_score`、fork 的 `model/action/min_score`、
  无条件节点的 `conf_min`、拦截叶的 `model`）→ 记录日志，控制台保存时也会回显。

### 关键环境变量

| 变量 | 作用 |
|---|---|
| `ROUTER_TAGGER` | `heuristic` / `checkpoint` / `jev` / `prompt` |
| `ROUTER_CHECKPOINT` | checkpoint 模式权重目录（回退用） |
| `ROUTER_JEV_MODEL` | merged JEV 单模型目录 |
| `ROUTER_SIGNAL_QUESTIONS` | JEV 问题配置 JSON（问题措辞/候选/真值定义，可编辑） |
| `ROUTER_JEV_MAX_LENGTH` | JEV 输入上限（默认取 model.json） |
| `ROUTER_JEV_TRIM` | 设为 `0` 关闭 /v1 按树裁剪问题的热路径优化 |
| `ROUTER_DEVICE` | 模型设备（如 `cuda:0`） |
| `ROUTER_ADMIN_TOKEN` | `/admin*` 的 Bearer token（控制台会提示输入） |
| `ROUTER_DB` / `ROUTER_HOST` / `ROUTER_PORT` | DB 路径 / 监听地址 / 端口 |

## 目录

```
router/
  api/            FastAPI 应用、/v1、/admin、认证、上游与熔断
  pipeline/       分流树引擎（显式树、加权叶子、trace）
  signals/        信号注册表、规则、checkpoint / jev / prompt tagger
  reasoning.py    推理能力声明校验 + 9 种协议方言的上行派发
  presets.py      内置提供方 / 预置模型目录（纯数据）
  store/          SQLite schema、repositories、迁移
  console/        静态控制台（HTML/CSS/JS，中英双语）
configs/          signal_questions.json（JEV 问题配置）等
scripts/          运维与联调脚本（部署、上游配置、控制台冒烟）
tests/            pytest（信号纯函数、pipeline、admin API、tagger 单测）
```

## 两个信号引擎

| | RoutePilot checkpoint | JEV 主模型 |
|---|---|---|
| 模型 | Qwen3.5-0.8B 全参微调 + 8 头 | Qwen3.5-0.8B + JEV 决策头（LoRA 合并导出） |
| 输入形态 | 用户请求，一次前向 | context + typed questions + candidates |
| 可配性 | 固定 7 模型信号（权重内）+ 规则 8 | 问题/候选/真值定义 JSON 可配 |
| 信号 | 7 学习型 + 规则 8 | 7 学习型 + 规则 8 |
| 延迟 | ~7 ms/text（batch） | 全量 54 候选 p50 113 ms；按树裁剪后 p50 64 ms；decision 实测 64–73 ms |
| 准确率（test） | domain .83/.82、jailbreak .85/.82、complexity .96、fact_check .95、pii macro-F1 .17 | domain .810、jailbreak .877、complexity .948、fact_check .948、user_feedback .802、PII micro-F1 .889 |

**JEV 版 vs semantic-router（test，en）**

| 信号 | JEV 版 | semantic-router |
|---|---|---|
| domain | 0.810 | 0.9423 |
| jailbreak | 0.877 | 0.7267 |
| fact_check | 0.948 | 0.8188 |
| user_feedback | 0.802 | 0.1749 |

> **标度说明（2026-10-21 审核 P2 4.4）**：上表准确率是 **argmax 正确率**，与分流树
> 置信门槛（默认 `conf_min = 0.70`）不在同一标度。置信度 `1 - H(p)/log K` 在 K 大时
> 很陡：domain（K=14）要过 0.70 需 p_top ≥ 0.854（折合 T=3.45 下原始 logit gap ≈ 14.9），
> 二类信号需 p ≥ 0.947。README 不提供「过门槛后的条件命中率」，选型评估时请勿把
> argmax 准确率当作门槛通过率使用。

## JEV 模型使用注意

- 运行期只渲染 `configs/signal_questions.json` 里每条信号的**第 1 条问法**；模型按标准问法
  校准，改写措辞前请先复评（`user_feedback` 对措辞最敏感）。
- 候选顺序不敏感。
- **输入窗口 = 最后一条 user 消息的前 1024 token**（`max_length`，按 tokenizer 精确头截断）：
  超长请求只裁一次并保留开头，不再做 8 轮上下文收缩/左截断（200K 输入：截断 0.67s、
  全量打标 2.0s；旧 stock shrink 46–48s）。问题模板另占 47–120 token，prompt 上限相应放宽为
  `max_length + 160`，标准问法不会被截。规则信号（`pii_rules` 等确定性正则）仍扫**全文**，
  是长请求的兜底；裁剪事实进计算日志（`input_tokens` / `input_visible_tokens` /
  `input_truncated`，见 `/admin/requests/recent`）。
- 采样温度由模型目录的 `temperature.json` 自动加载（`softmax(logits / T)`）。

## 热路径裁剪

`/v1` 会按当前 pipeline 树只计算会被读取的信号问题（`ROUTER_JEV_TRIM=0` 一键关闭）；
`/admin/try` 与 eval 入口始终全量打标，便于试算与回归。

## `/v1` 入口约定

- 请求体必须是 JSON 对象、`messages` 必须是数组：畸形请求回 **400**（不是 500）；
  请求体超过 **10 MB** 回 **413**。
- 上游返回 **4xx** 时按原状态码与错误体透传给客户端（流式与非流式一致），
  不再包装成 200 SSE；只有 5xx / 网络错误才计入熔断失败。
- 流式请求的读超时单独放开（`httpx.Timeout(60.0, read=None)`）：思考型模型首个 token
  可能超过 60s，不应被全局超时截断。

## 模型目录与推理配置

「模型与设置」页是后端模型（分流树的叶子目标）的唯一配置面，参考
[deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的声明式提供方目录与
「每模型推理档位」设计（详细设计与实现记录见本地工作记录
`docs/模型目录与推理配置-2026-10-21.md`，按仓库约定 `docs/` 不入库）。

- **预置模型目录**：内置 14 个 OpenAI 兼容提供方 / 54 个预置模型（OpenAI、Anthropic、
  DeepSeek、Kimi、Qwen、Z.ai、OpenRouter、Gemini、xAI、Together、Groq、Mistral、MiniMax、
  本地 vLLM）。添加模型走「预置模型」（提供方 → 密钥 → 多选模型 → 模型位 ID 可改）或
  「自定义模型」（任意上游 id + 推理声明 + 高级画像）。
- **提供方密钥**：可在弹窗内填写（存 SQLite，`/admin` 有 Bearer 鉴权；GET 接口只回
  `key_present`/`key_source`，永不回显明文），也可留空使用环境变量（`key_env`）。
  支持「获取可用模型」（服务端问提供方 `GET {base_url}/models`，不落库）。
- **推理能力是模型属性**：`models.profile.reasoning` 声明「可选的档位 → 该档在协议里发送的
  拼写」，例如 `{"efforts": {"off": null, "high": "high", "max": "max"}, "default": "high"}`；
  `reasoning: false` = 明确不支持，缺省 = 未声明（只透传）。协议方言 9 种：
  `openai`(reasoning_effort) / `deepseek`(thinking.type) / `qwen`(enable_thinking) /
  `qwen-chat-template` / `openrouter`(reasoning.effort) / `zai` / `together` /
  `string-thinking` / `ant-ling`，提供方级 `reasoning_format` 做默认、模型级 `format` 覆盖。
- **分流树叶子选择推理**：`reasoning` 为 `null`（默认）= 完全透传客户端请求体；`off` = 强制
  关闭；档位 id = 强制该档。保存分流树时按目标模型的能力硬校验（422）；运行期若配置竞态导致
  档位不再可用，则降级透传并回 `x-sr-reasoning-warning: unsupported`，不打断用户请求。
  成功注入时响应头带 `x-sr-reasoning: <level>`，计算日志也会记录。

## 部署

任意一台有模型目录的机器上，用 systemd 或进程管理器拉起：

```bash
ROUTER_TAGGER=jev \
ROUTER_JEV_MODEL=/path/to/jev_merged_model \
ROUTER_SIGNAL_QUESTIONS=/path/to/signal_questions.json \
ROUTER_ADMIN_TOKEN=<random-token> \
ROUTER_DEVICE=cuda:0 \
ROUTER_BIND=0.0.0.0 \
python -m router
```

`scripts/` 内附部署与上游配置脚本模板，可按目标环境改写。

## 测试与验证

- 单元/集成：`pytest`（491 passed, 6 skipped）。
- i18n 死键 lint：`python scripts/i18n_dead_keys.py`（有死键时退出码非 0，可挂 CI；
  `scripts/i18n_prune_i18n.py` 负责批量删除）。
- 控制台冒烟：`BASE=http://<host>:<port> ADMIN_TOKEN=<token> node scripts/console_check.js`
  （覆盖五个页面、模型目录/添加模型弹窗/推理声明编辑器、分流树叶推理下拉与日志徽章）。
