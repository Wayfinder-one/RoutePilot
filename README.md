# Smart Router v2

LLM 请求的**信号计算 + 分流决策 + 控制台**。请求进入后端之前，router 先算出一包路由信号
（19 个：规则 + 学习型 + 会话），再按可编辑的分流树（pipeline）决定走哪个模型、或直接拦截。

默认推荐 **JEV 版主模型** 作为信号引擎：**Qwen3.5-0.8B 底座 + JEV 标量决策头**（LoRA 合并
导出），在 router 进程内按 JEV 标准（context + typed questions + candidates → 候选概率）输出
全部 7 个模型信号；问题/候选/真值定义 JSON 可配。RoutePilot checkpoint（多头）作为可回退引擎
保留。PII 信号另有确定性正则补充（`router/signals/pii_rules.py`）。

## 架构

```
用户请求 ──▶ FastAPI (/v1/chat/completions, /v1/embeddings)
              │
              ├─ Tagger（二选一）
              │   ├─ checkpoint: RoutePilot-0.8B 一次前向 → 7 信号 + embedding
              │   └─ jev:       Qwen3.5-0.8B JEV 头按问题作答 → 候选概率（推荐）
              │   + 9 个确定性规则信号（signals/rules.py）
              │
              ├─ Pipeline（分流树：每个站点一棵独立显式树，节点可增删、叶子可加权；
              │            存 SQLite JSON，可热更新）
              ├─ Upstream（OpenAI 兼容推理后端 + 熔断）
              └─ RequestLog（信号/决策/trace/tree_path → SQLite）
控制台（/console）：概览 / 分流树（信号库 + 树形编辑 + 试算侧滑）/ 计算日志 / 信号配置 / 模型与设置
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
| `ROUTER_DB` / `ROUTER_HOST` / `ROUTER_PORT` / `ROUTER_SESSIONS` | DB 路径 / 监听地址 / 端口 / 会话信号 |

## 目录

```
router/
  api/            FastAPI 应用、/v1、/admin、认证、上游与熔断
  pipeline/       分流树引擎（显式树、加权叶子、trace）
  signals/        信号注册表、规则、checkpoint / jev / prompt tagger
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
| 可配性 | 固定 19 信号（权重内） | 问题/候选/真值定义 JSON 可配 |
| 信号 | 7 学习型 + embedding（similarity/reask）+ 规则 9 | 7 学习型（无 embedding）+ 规则 9 |
| 延迟 | ~7 ms/text（batch） | 全量 54 候选 p50 113 ms；按树裁剪后 p50 64 ms；decision 实测 64–73 ms |
| 准确率（test） | domain .83/.82、jailbreak .85/.82、complexity .96、fact_check .95、pii macro-F1 .17 | domain .810、jailbreak .877、complexity .948、fact_check .948、user_feedback .802、PII micro-F1 .889 |

**JEV 版 vs semantic-router（test，en）**

| 信号 | JEV 版 | semantic-router |
|---|---|---|
| domain | 0.810 | 0.9423 |
| jailbreak | 0.877 | 0.7267 |
| fact_check | 0.948 | 0.8188 |
| user_feedback | 0.802 | 0.1749 |

## JEV 模型使用注意

- 运行期只渲染 `configs/signal_questions.json` 里每条信号的**第 1 条问法**；模型按标准问法
  校准，改写措辞前请先复评（`user_feedback` 对措辞最敏感）。
- 候选顺序不敏感。
- 输入上限 1024 token：超长先收缩上下文、最后才 token 截断，不报错。
- 采样温度由模型目录的 `temperature.json` 自动加载（`softmax(logits / T)`）。

## 热路径裁剪

`/v1` 会按当前 pipeline 树只计算会被读取的信号问题（`ROUTER_JEV_TRIM=0` 一键关闭）；
`/admin/try` 与 eval 入口始终全量打标，便于试算与回归。

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

- 单元/集成：`pytest`（177 passed, 6 skipped）。
- 控制台冒烟：`BASE=http://<host>:<port> ADMIN_TOKEN=<token> node scripts/console_check.js`。
