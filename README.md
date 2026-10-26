# Smart Router v2

LLM 请求的**信号计算 + 分流决策 + 控制台**。请求进入后端之前，router 先算出一包路由信号
（16 个：8 个模型信号 + 8 个规则信号），再按可编辑的分流树（pipeline）决定走哪个模型、或直接拦截。

其中第 8 个模型信号 `capability_route`（**自然语言能力路由**，2026-10-26）不是固定问题：
它让模型按「每个候选模型的一段自然语言能力说明」选择本次请求最适合的后端模型
（详见下文「自然语言能力路由」）。

默认推荐 **JEV 版主模型** 作为信号引擎：**Qwen3.5-0.8B 底座 + JEV 标量决策头**（LoRA 合并
导出），在 router 进程内按 JEV 标准（context + typed questions + candidates → 候选概率）输出
全部 7 个固定模型信号；问题/候选/真值定义 JSON 可配。RoutePilot checkpoint（多头）作为可回退引擎
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
              └─ RequestLog（决策摘要 → SQLite requests slim 行 + request_ledger
                             动作—结果账本（outcome_state/revisions）；信号/trace 只在内存 ring）
控制台（/console）：概览 / 分流树（信号库 + 树形编辑 + 试算侧滑）/ 计算日志 /
                    信号配置 / 能力路由（自然语言说明 → 模型选择，策略 + 候选 + 缺失）/
                    模型与设置（模型目录 + 提供方 + 推理声明 + 能力说明列）/
                    用户管理（登录账号：角色 admin/viewer，见「账号与鉴权」）
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
| `ROUTER_JEV_CHUNK_SEGMENTS` | 长文 PII 分块（0=off 默认；4/8 为实验值）；配合 `ROUTER_JEV_CHUNK_SIZE`(512)、`ROUTER_JEV_CHUNK_AGG`(noisy_or)、`ROUTER_JEV_CHUNK_CALIBRATION`（默认全关，行为不变） |
| `ROUTER_JEV_WINDOW` | JEV 输入窗口 `<head>:<tail>`（token 数，默认未配置 = 旧行为 `<max_length>:0` 纯 head；如 `256:768` / `0:1024` / `512:512`；短文本逐字不变） |
| `ROUTER_JEV_TRIM` | 设为 `0` 关闭 /v1 按树裁剪问题的热路径优化 |
| `ROUTER_DEVICE` | 模型设备（如 `cuda:0`） |
| `ROUTER_USER_AUTH` | 控制台账号体系（默认 `1`）。`0` = 退回旧行为：`/admin` 完全开放（仅本地 dev） |
| `ROUTER_ADMIN_EMAIL` | 首次启动 seed 的管理员邮箱（默认 `admin@test.com`；users 表非空不覆盖） |
| `ROUTER_ADMIN_PASSWORD` | 首次启动 seed 的管理员密码（默认 `1qaz@WSX`；**上线后请立刻在「用户管理」改掉**） |
| `ROUTER_SESSION_TTL_HOURS` | 登录会话有效期（默认 `168` 小时） |
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
- **输入窗口 = 最后一条 user 消息的 tokenizer 窗口**：默认 `1024:0`（`ROUTER_JEV_WINDOW`
  未配置时 = `max_length:0`，即旧的纯 head 截断，行为逐字不变）；超长请求只裁一次，
  不再做 8 轮上下文收缩/左截断（200K 输入：截断 0.67s、全量打标 2.0s；旧 stock shrink 46–48s）。
  窗口可配成 `256:768` / `512:512`（head+tail，用 `\n\n[...]\n\n` 连接，应对"长文末尾才是真正指令"）
  或 `0:1024`（纯 tail）；短文本（≤ max_length token）任何配置下都逐字不裁剪。
  问题模板另占 47–120 token，prompt 上限相应放宽为 `max_length + 160`，标准问法不会被截。
  规则信号（`pii_rules` 等确定性正则）仍扫**全文**，是长请求的兜底；窗口事实进计算日志
  （`input_tokens` / `input_visible_tokens` / `input_truncated` / `input_window`，见
  `/admin/requests/recent`）与动作—结果账本（`request_ledger.jev_window`）。
- 采样温度由模型目录的 `temperature.json` 自动加载（`softmax(logits / T)`）。

## 热路径裁剪

`/v1` 会按当前 pipeline 树只计算会被读取的信号问题（`ROUTER_JEV_TRIM=0` 一键关闭）；
`/admin/try` 与 eval 入口始终全量打标，便于试算与回归。

## 决策快照、账本终态与 config revision（2026-10-24 P1）

- 每个请求只读一份不可变 `RoutingSnapshot`（pipeline/backends/版本/revision）；打标期间的热更新
  不会让路由与账本错配。pipeline/questions 以 canonical content + sha256 写入 `config_revisions`
  （append-only，旧 revision 可回放）；账本记录 `pipeline_revision / questions_revision /
  backends_revision` 与 `TagResult` 的实际评分配置指纹。
- 流式请求终态 `outcome_state ∈ completed / upstream_error / client_disconnected / cancelled /
  incomplete`，结果更新恰好一次；`GET /admin/requests/stats` 暴露 ring/队列与
  dropped_rows / dropped_updates / partial_rows / ledger_write_failures。
- `app.state.pipeline / backends` 仅作为管理端兼容别名；hot path 只读 snapshot。

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

## 自然语言能力路由（2026-10-26，方案 A）

让「决策模型按自然语言能力说明选后端模型」成为分流树里的一个普通信号
`capability_route`（decision 组，模型信号）：

- **能力说明存在模型目录**：`models.profile.capability`（每个模型一段自然语言描述，
  ≤2000 字符；空 = 不参与）。控制台「模型与设置 → 编辑模型 → 能力说明」维护，
  模型注册表**直接展示**这段说明（「能力说明」列，在操作列之前；未配置显示「未配置」）。
- **全局策略**：`settings.capability_instructions` / `capability_min_prob` /
  `capability_min_margin` / `capability_fallback_model`，控制台独立的「能力路由」页编辑
  （位于「模型与设置」之前），或 `PUT /admin/capability`。请求里会把候选说明渲染成一个 JEV
  choice 问题，与 7 个信号**同一批前向**打分（树不引用该信号时不算）。
- **信号输出**：`value` = 选中的模型位 ID（可直接路由），`confidence` = 候选分布的熵置信度，
  `prob_by_value` / `p_top` / `margin` / `top_model` / `fallback_used` / `reason` /
  `fingerprint` 为 record-only 证据（计算日志可见）。`min_prob` / `min_margin` 不满足时
  直接落到 `fallback_model`（fail-open），不会让请求失败。
- **分流树写法**：在兜底站点前加一个站点，每个候选一个叶子：
  `capability_route == qwen3-32b, conf ≥ 0.2 → route qwen3-32b`（置信度是熵标度，
  不要沿用旧节点的 0.7）。`GET /admin/signals` 会把当前能力候选注入为该信号的取值下拉。
- **健康检查**：树引用了 `capability_route` 但取值不是能力候选 / 没有任何模型配置说明 /
  tagger 不是 JEV 引擎 → 启动或保存时拒绝（422 / 拒绝启动），不会静默落到兜底。
- **配置版本**：能力包随 `RoutingSnapshot` 发布，内容写入 `config_revisions(kind="capability")`，
  账本新增 `capability_revision`（migration v18），可回放；模型写入会预检并阻止删掉被树引用的候选
  （409 带 `via="capability"`）。
- **离线回归**：`configs/capability_golden.jsonl`（30 条）+ `scripts/capability_ab_eval.py`
  （复制 DB 跑「现有树 vs 能力信号」A/B，不碰生产库）。首轮实测：能力信号 26/30、默认树 10/30
  （默认树把 30 条全落 `qwen3-8b` 兜底）；只改能力说明文本两轮后 29/30。
  设计、证据与上线/回滚步骤见 `docs/能力路由-自然语言能力分配-2026-10-26.md`。

## 部署

任意一台有模型目录的机器上，用 systemd 或进程管理器拉起：

```bash
ROUTER_TAGGER=jev \
ROUTER_JEV_MODEL=/path/to/jev_merged_model \
ROUTER_SIGNAL_QUESTIONS=/path/to/signal_questions.json \
ROUTER_DEVICE=cuda:0 \
ROUTER_HOST=0.0.0.0 \
python -m router
```

首次启动（users 表为空）会 seed 一个默认管理员：`admin@test.com` / `1qaz@WSX`
（`ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` 可覆盖）。控制台入口
`http://<host>:<port>/console/login.html`；`/admin*` 需要登录会话。
**静态 `ROUTER_ADMIN_TOKEN` 已于 2026-10-26 移除**——脚本改为用
`POST /admin/login` 换会话 token（`scripts/configure_upstream.py` 等已内置）。

`scripts/` 内附部署与上游配置脚本模板，可按目标环境改写。

## 账号与鉴权

- **登录**：控制台打开即跳 `login.html`，邮箱 + 密码登录；会话 token 存浏览器
  localStorage（`sr_admin_token`），TTL 默认 168 小时，过期或服务端吊销后自动跳回
  登录页。`?token=<session token>` 可用于把已有会话一次性注入浏览器（演示/自动化）。
- **角色**：`admin` = 全部 `/admin` 读写 + 用户管理；`viewer` = `/admin` 只读
  （写操作 403、用户管理页 403）。角色在用户表里维护，每次请求现查（改角色立即生效）。
- **用户管理**（`/console/users.html`，仅 admin）：新建/改邮箱/改角色/停用/
  重置密码/删除；不能删除自己，不能删除或降级最后一个 active admin。
  所有写操作进 `changes` 审计（只有 metadata，绝不含密码或哈希）。
- **脚本/CI 怎么认证**：先去 `POST /admin/login`（默认管理员或其他 admin 账号）拿
  `token`，后续请求带 `Authorization: Bearer <token>`；`deploy-76.sh`、`configure_upstream.py`、
  `apply_default_pipeline.py` 已按这个方式改造（账号用 `SR_ADMIN_EMAIL` /
  `SR_ADMIN_PASSWORD`，默认是 seed 的 admin@test.com）。**改过默认密码后要在部署 env 里
  同步，否则脚本登录会失败。**
- **密码存储**：标准库 `hashlib.scrypt`（n=2^14, r=8, p=1，16 字节随机盐）；
  会话 token 只存 sha256，DB 泄露不等于会话泄露。登录失败统一 401（不泄露
  账号是否存在），失败写 logger（邮箱 + 来源 IP）。
- **关掉账号体系**：`ROUTER_USER_AUTH=0` → `/admin` 完全开放（无鉴权），仅限本地 dev。

## 测试与验证

- 单元/集成：`pytest`（873 passed, 7 skipped；skip 均为缺 GPU/模型/可选依赖的用例）。
- 离线 JEV 评估：`scripts/jev_dump_scores.py`（官方 test 集 raw 分布 dump，需模型）、
  `scripts/jev_calibrate.py`（coverage-risk / AURC / ECE / 各门槛 block recall 报告，纯 CPU）、
  `scripts/eval_jev_window_ab.py`（输入窗口 head/tail/head+tail A/B，需 GPU）、
  `scripts/cheap_sufficient_mvp.py`（GSM8K 上便宜档 C vs 强档 S 的 cheap_sufficient 离线实验，需两个模型）。
- i18n 死键 lint：`python scripts/i18n_dead_keys.py`（有死键时退出码非 0，可挂 CI；
  `scripts/i18n_prune_i18n.py` 负责批量删除）。
- 控制台冒烟：`BASE=http://<host>:<port> LOGIN_EMAIL=<admin> LOGIN_PASSWORD=<pass> node scripts/console_check.js`
  （覆盖七个页面（含登录页/用户管理页/能力路由页）、模型目录/添加模型弹窗/推理声明编辑器、
  能力说明列与能力策略保存、分流树叶推理下拉与日志徽章；脚本先用账号登录换会话 token 再做检查）。
