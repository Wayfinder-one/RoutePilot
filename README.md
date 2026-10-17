# Smart Router v2

LLM 请求的**信号计算 + 分流决策 + 控制台**。请求进入后端之前，router 先算出一包路由信号
（19 个：规则 + 学习型 + 会话），再按可编辑的分流树（pipeline）决定走哪个模型、或直接拦截。

当前线上（192.168.95.76:8400）使用 **JEV 版主模型**作为信号引擎：**你训练的 Qwen3.5-0.8B
RoutePilot checkpoint 的文本塔 + 新建 JEV 决策头**，在 router 进程内按 JEV 标准（context +
typed questions + candidates → 候选概率）输出全部 7 个模型信号，提示词/候选可配；
RoutePilot checkpoint（多头）作为可回退引擎保留，早前的 Open-Jev-2B 版本仅作对照（未部署）。

## 架构

```
用户请求 ──▶ FastAPI (/v1/chat/completions, /v1/embeddings)
              │
              ├─ Tagger（二选一）
              │   ├─ checkpoint: RoutePilot-0.8B 一次前向 → 7 信号 + embedding
              │   └─ jev:       Qwen3.5-0.8B JEV 头按问题作答 → 候选概率（默认部署）
              │   + 9 个确定性规则信号（signals/rules.py）
              │
              ├─ Pipeline（分流树：gate / score / route / block，存 SQLite，可热更新）
              ├─ Upstream（OpenAI 兼容推理后端 + 熔断）
              └─ RequestLog（信号/决策/trace → SQLite；Litestream 复制）
控制台（/console）：概览 / 分流树 / 试算 / 模型与设置
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
ROUTER_JEV_MODEL=/opt/smart-router-v2/models/jev_merged_08b \
ROUTER_SIGNAL_QUESTIONS=configs/signal_questions.json \
.venv/bin/python -m router
```

### 关键环境变量

| 变量 | 作用 |
|---|---|
| `ROUTER_TAGGER` | `heuristic` / `checkpoint` / `jev` / `prompt` |
| `ROUTER_CHECKPOINT` | checkpoint 模式权重目录（回退用） |
| `ROUTER_JEV_MODEL` | merged Open-Jev 单模型目录 |
| `ROUTER_SIGNAL_QUESTIONS` | JEV 问题配置 JSON（问题措辞/候选/真值定义，可编辑） |
| `ROUTER_JEV_MAX_LENGTH` | JEV 输入上限（默认取 model.json） |
| `ROUTER_DEVICE` | 模型设备（如 `cuda:0`） |
| `ROUTER_ADMIN_TOKEN` | `/admin*` 的 Bearer token（控制台会提示输入） |
| `ROUTER_DB` / `ROUTER_HOST` / `ROUTER_PORT` / `ROUTER_SESSIONS` | DB 路径 / 监听地址 / 端口 / 会话信号 |

## 目录

```
router/
  api/            FastAPI 应用、/v1、/admin、认证、上游与熔断
  pipeline/       分流树引擎（gate/score/route/block, trace）
  signals/        信号注册表、规则、checkpoint / jev / prompt tagger
  store/          SQLite schema、repositories、迁移
  console/        静态控制台（HTML/CSS/JS，中英双语）
configs/          signal_questions.json（JEV 问题配置）、关键词表等
scripts/          deploy-76.sh、console_check.js、configure_upstream.py、systemd 模板
tests/            pytest（信号纯函数、pipeline、admin API、tagger 单测）
docs/             设计/实施/审核/部署记录（按约定不入库，见文末）
```

## 两个信号引擎

| | RoutePilot checkpoint | JEV 主模型 |
|---|---|---|
| 模型 | Qwen3.5-0.8B 全参微调 + 8 头 | 你的 Qwen3.5-0.8B checkpoint 文本塔 + JEV 决策头（LoRA 合并导出，1.73GB） |
| 输入形态 | 用户请求，一次前向 | context + typed questions + candidates |
| 可配性 | 固定 19 信号（权重内） | 问题/候选/真值定义 JSON 可配 |
| 信号 | 7 学习型 + embedding（similarity/reask）+ 规则 9 | 7 学习型（无 embedding）+ 规则 9 |
| 延迟 | ~7 ms/text（batch） | ~0.30–0.42 s/请求（全部信号 54 候选 prompt） |
| 准确率（test） | domain .83/.82、jailbreak .85/.82、complexity .96、fact_check .95、pii 弱 | domain .840/.806、jailbreak .857/.835、complexity .951/.952、fact_check .913、pii exact .445（fact_check -4pt、PII 弱，见 docs） |
| 部署位置 | 76 主机 checkpoint 目录 | `/opt/smart-router-v2/models/jev_merged_08b`（进程内，2.3GB GPU） |

训练/数据/蒸馏代码与完整指标在子仓库 **RoutePilot-train**：
`results/bilingual/README.md`、`results/jev_distill/README.md`、`docs/*/jev-distill.md`。

## 部署

```bash
# 76 上（已带 admin token / litestream / 模型目录）
SR_TAGGER=jev SR_JEV_MODEL=/opt/smart-router-v2/models/jev_merged_08b \
SR_SIGNAL_QUESTIONS=/opt/smart-router-v2/configs/signal_questions.json \
SR_ADMIN_TOKEN=... SR_PYTHON=/root/jev-2b/venv/bin/python \
SR_DEVICE=cuda:0 bash scripts/deploy-76.sh
```

部署、验证、回滚细节见 `docs/部署到76.md`；JEV 接入记录见
`docs/JEV-接入与部署记录-2026-10-17.md`。

## 文档（本地 `docs/`，按约定不入库）

- `docs/部署到76.md` — 76 部署/验证/回滚（含 2026-10-17 JEV 第四轮上线记录）
- `docs/JEV-接入与部署记录-2026-10-17.md` — JEV 主模型接入的完整记录
- `docs/Smart-Router-v2-实现与部署记录.md` — 实现、偏差清单、测试与部署证据
- `docs/Smart-Router-v2-设计方案.md` / `实施计划` — 设计与计划基线

## 测试与验证

- 本地：`pytest` **135 passed, 6 skipped**。
- 控制台冒烟：`BASE=http://<host>:8400 ADMIN_TOKEN=<token> node scripts/console_check.js`。
- 76 线上：`/healthz`、`/admin/try` 信号探针、`/v1` 拦截/放行、console smoke 均已通过
  （2026-10-17 JEV 上线验证，见 `docs/JEV-接入与部署记录-2026-10-17.md`）。
