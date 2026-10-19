# Smart Router v2

LLM 请求的**信号计算 + 分流决策 + 控制台**。请求进入后端之前，router 先算出一包路由信号
（19 个：规则 + 学习型 + 会话），再按可编辑的分流树（pipeline）决定走哪个模型、或直接拦截。

当前线上（192.168.95.76:8400）使用 **JEV 版主模型 r4b3** 作为信号引擎：**Qwen3.5-0.8B
底座 + JEV 标量决策头**（LoRA 合并导出，1.73GB），在 router 进程内按 JEV 标准（context +
typed questions + candidates → 候选概率）输出全部 7 个模型信号；问题/候选/真值定义 JSON 可配，
但**运行期只渲染每条信号的第 1 条标准问法**（r4b3 训练覆盖 3 种措辞，自由改写会漂移，见下）。
RoutePilot checkpoint（多头）作为可回退引擎保留，早前的 Open-Jev-2B 版本仅作对照（未部署）。
PII 信号另有确定性正则补充（`router/signals/pii_rules.py`）。

## 架构

```
用户请求 ──▶ FastAPI (/v1/chat/completions, /v1/embeddings)
              │
              ├─ Tagger（二选一）
              │   ├─ checkpoint: RoutePilot-0.8B 一次前向 → 7 信号 + embedding
              │   └─ jev:       Qwen3.5-0.8B JEV 头按问题作答 → 候选概率（默认部署）
              │   + 9 个确定性规则信号（signals/rules.py）
              │
              ├─ Pipeline（分流树：gate / score / split（树形由分支推导，SPEC-TREE-4），
              │            存 SQLite，可热更新）
              ├─ Upstream（OpenAI 兼容推理后端 + 熔断）
              └─ RequestLog（信号/决策/trace/tree_path → SQLite；Litestream 复制）
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
ROUTER_JEV_MODEL=/opt/smart-router-v2/models/jev_merged_08b_r3 \
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
scripts/          deploy-76.sh、console_check.js、configure_upstream.py、systemd 模板；
                  209 真实后端联调：vllm_backends_209.sh（起停两个 Qwen3-0.6B vLLM 模拟后端，
                  一强一弱）+ configure_backends_209.py（把分流树模型位接到 209:9101/9102）
tests/            pytest（信号纯函数、pipeline、admin API、tagger 单测）
docs/             设计/实施/审核/部署记录（按约定不入库，见文末）
```

## 两个信号引擎

| | RoutePilot checkpoint | JEV 主模型 |
|---|---|---|
| 模型 | Qwen3.5-0.8B 全参微调 + 8 头 | Qwen3.5-0.8B + JEV 决策头（LoRA 合并导出，1.73GB；r4b3） |
| 输入形态 | 用户请求，一次前向 | context + typed questions + candidates |
| 可配性 | 固定 19 信号（权重内） | 问题/候选/真值定义 JSON 可配（运行期只用第 1 条标准问法） |
| 信号 | 7 学习型 + embedding（similarity/reask）+ 规则 9 | 7 学习型（无 embedding）+ 规则 9 |
| 延迟 | ~7 ms/text（batch） | 全量 54 候选 p50 **113 ms**；线上树裁剪到 4 信号/17 候选后 p50 **64 ms**（`_tag_row`，76 GPU，r4b2 bench 口径）；r4b3 上线后线上 decision 实测 **64–73 ms**；r4b3 训练口径全量 36 rec/54 cand p50 **359 ms** / p95 413 ms（与 r4b2 的 358/422 持平） |
| 准确率（test） | domain .83/.82、jailbreak .85/.82、complexity .96、fact_check .95、pii macro-F1 .17 | domain .810、jailbreak .877、complexity .948、fact_check .948、user_feedback .802、PII micro-F1 .889（r4b3；官方 JEV test .879 / ECE .021） |
| 部署位置 | 76 主机 checkpoint 目录 | `/opt/smart-router-v2/models/jev_merged_08b_r4b3`（T=3.0，进程内，2.7GB GPU；r4b2/r3 目录保留可一行回滚） |

**JEV 版 vs semantic-router（test，en）**

| 信号 | JEV 版（r4b3） | semantic-router |
|---|---|---|
| domain | 0.810 | 0.9423 |
| jailbreak | 0.877 | 0.7267 |
| fact_check | 0.948 | 0.8188 |
| user_feedback | 0.802 | 0.1749 |

**r4b3 使用注意（标准问法，2026-10-19）**：运行期只渲染 `configs/signal_questions.json`
里每条信号的**第 1 条问法**，必须保持训练侧标准问法（与训练仓 `configs/jev/r4_questions.json`
的 variant 0 逐字一致）。r4b3 训练覆盖每题 3 种措辞变体，未见措辞整体漂移 ≤~5pt macro，
但 `user_feedback` 对措辞最敏感（标准问法 .802、改写 .945）；候选顺序不敏感；输入上限
1024 token（超长由 `_shrink_to_fit` 收缩上下文、最后才 token 截断，不报错）；
`softmax(logits / 3.0)` 由模型目录 `temperature.json` 自动加载。控制台「信号配置」页已加
改写警告，`tests/test_jev_canonical.py` 钉住标准问法——改问法前先在训练仓跑 eval 复测。

**JEV 线上热路径裁剪（2026-10-18，r4b3 沿用）**：`/v1` 按当前 pipeline 树只计算会被读取的问题
（当前树 = jailbreak + pii.cn_id_card + domain + complexity，4 records / 17 candidates），
`/admin/try` 与 eval 入口保持全量；`ROUTER_JEV_TRIM=0` 一键回退。r4b3 沿用 T=3.0，
线上 `jailbreak` 门限仍为 **0.15**：r4b3 在 en+zh test（n=1074，逐请求打分）复测 gate
**P .894 / R .832 / F1 .862**（r4b2 同阈值 P .908 / R .774 / F1 .836）；r4b3 的置信度
首次具备区分度（conf-AUC .638 vs r4b2 .474），F1 最优点在 **0.10**（P .890 / R .847）——
先保持 0.15，若后续更看重拦截召回再下调。PII 门限 .30 由确定性规则兜底，pipeline 无需改动。

训练/数据/蒸馏代码与完整指标在子仓库 **RoutePilot-train**：
`results/jev_r4/README.md`（r4/r4b/r4b2/r4b3 全过程）、`results/bilingual/README.md`、
`results/jev_distill/README.md`、`docs/*/jev-distill.md`。

## 部署

```bash
# 76 上切模型（r4b3 merged 由训练仓产出，保留旧目录即可一行回滚）
rsync -a /root/r4-runs/jev_r4b3_merged/ \
  /opt/smart-router-v2/models/jev_merged_08b_r4b3/
sed -i 's#^Environment=ROUTER_JEV_MODEL=.*#Environment=ROUTER_JEV_MODEL=/opt/smart-router-v2/models/jev_merged_08b_r4b3#' \
  /etc/systemd/system/smart-router-v2.service
systemctl daemon-reload && systemctl restart smart-router-v2

# 新机器部署（带 admin token / litestream / 模型目录）
SR_TAGGER=jev SR_JEV_MODEL=/opt/smart-router-v2/models/jev_merged_08b_r4b3 \
SR_SIGNAL_QUESTIONS=/opt/smart-router-v2/data/signal_questions.json \
SR_ADMIN_TOKEN=... SR_PYTHON=/root/jev-2b/venv/bin/python \
SR_DEVICE=cuda:0 bash scripts/deploy-76.sh
```

部署、验证、回滚细节见 `docs/部署到76.md`；JEV 接入记录见
`docs/JEV-接入与部署记录-2026-10-17.md`。

## 文档（本地 `docs/`，按约定不入库）

- `docs/部署到76.md` — 76 部署/验证/回滚（含 2026-10-17 JEV 上线、2026-10-18 r3+PII 安全网、
  2026-10-19 r4b3 切换记录）
- `docs/JEV-接入与部署记录-2026-10-17.md` — JEV 主模型接入的完整记录
- `docs/Smart-Router-v2-实现与部署记录.md` — 实现、偏差清单、测试与部署证据
- `docs/Smart-Router-v2-设计方案.md` / `实施计划` — 设计与计划基线

## 测试与验证

- 本地：`pytest` **172 passed, 6 skipped**。
- 控制台冒烟：`BASE=http://<host>:8400 ADMIN_TOKEN=<token> node scripts/console_check.js`。
- 76 线上：`/healthz`、`/admin/try` 信号探针、`/v1` 拦截/放行、console smoke 均已通过
  （2026-10-17 JEV 上线验证、2026-10-18 r4b2、2026-10-19 r4b3，见
  `docs/JEV-接入与部署记录-2026-10-17.md`）。
