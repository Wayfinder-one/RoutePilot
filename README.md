# Smart Router v2

LLM 请求的**信号计算 + 统一路由树 + 控制台**。请求进入后端之前，router 按树里的静态引用批量算出
一包路由信号（15 个：7 个模型信号 + 8 个规则信号），再由一棵可编辑的**统一路由树**决定走哪个模型、
或直接拦截；树里的自然语言节点（`nl_choice`）还会让 JEV 决策模型按「模型能力说明 / 分支说明」
在有序候选里做多级选择（详见下文「统一路由树」）。

默认推荐 **JEV 版主模型** 作为信号引擎：**Qwen3.5-0.8B 底座 + JEV 标量决策头**（LoRA 合并
导出），在 router 进程内按 JEV 标准（context + typed questions + candidates → 候选概率）输出
全部 7 个固定模型信号；问题/候选/真值定义 JSON 可配。RoutePilot checkpoint（多头）作为可回退引擎
保留。PII 信号另有确定性正则补充（`router/signals/pii_rules.py`）。

## 架构

```
用户请求 ──▶ FastAPI (/v1/chat/completions)
              │
              ├─ Tagger（二选一）
              │   ├─ checkpoint: RoutePilot-0.8B 一次前向 → 7 个模型信号
              │   └─ jev:       Qwen3.5-0.8B JEV 头按问题作答 → 候选概率（推荐）
              │   + 8 个确定性规则信号（signals/rules.py）
              │   （普通信号按树引用批量算一次：纯自然语言树跳过打标、纯规则树不触发模型前向）
              │
              ├─ RoutingRuntime（异步统一树求值：branch 顺序/加权；route/block 终止；
              │                  nl_choice 按实际路径调用 JEV 决策模型；不可变快照 + Hit/Miss/trace）
              ├─ Upstream（OpenAI 兼容推理后端 + 熔断；按 route 节点声明的推理档位
              │            注入各家协议字段：reasoning_effort / thinking / enable_thinking…）
              └─ RequestLog（决策摘要 → SQLite requests slim 行 + request_ledger
                             动作—结果账本（outcome_state / tree_id / tree_revision /
                             snapshot_revision）；信号/trace 只在内存 ring）
控制台（/console）：概览 / 统一路由树（多棵树：列表 + 四类型节点编辑 + 试算侧滑）/
                    计算日志 / 信号配置 / 模型与设置（模型目录 + 提供方 + 推理声明 + 能力说明列）/
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

### 保存与启用两级校验

`router/pipeline/validation.py` 把校验拆成两级：

- **保存（结构）**：节点/候选 id 唯一、节点类型与字段组合合法、根节点不能带 `when`、
  `otherwise` 不能带 `when`、内嵌树有界无循环；不合法返回 422。**关闭的草稿树允许不完整**
  （缺能力说明、缺最终兜底也能保存）。
- **启用（运行就绪）**：结构校验 + 信号/取值/操作符与置信门槛、`route` 模型存在、直接模型候选
  有非空能力说明、推理档位合法、树含 `nl_choice` 时 DecisionRunner 可用、NL 候选 2–255、
  **必须存在明确的最终兜底**（根是无权重 `route/block`，或从根沿 `otherwise` 链到无权重
  `route/block`）；不满足返回 422 并定位节点。
- **启动**：只对**当前启用树**跑完整启用检查（硬错误拒绝启动）；关闭树可以是残缺草稿；
  没有任何启用树时正常启动，`/v1` 返回 503 `no_active_tree`。新建树默认关闭，
  新库不隐式创建或启用默认树。

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
  api/            FastAPI 应用、/v1、/admin、认证、上游与熔断、/admin/trees 管理 API
  pipeline/       统一路由树（tree 模型、validation、compiler、不可变 snapshot、
                  evaluator、runtime、scope/matching/scoring 纯函数）
  decision/       自然语言决策边界（choice 纯函数、DecisionRunner 协议与 JEV 适配、
                  模型能力目录）
  signals/        信号注册表、规则、checkpoint / jev / prompt tagger
  reasoning.py    推理能力声明校验 + 9 种协议方言的上行派发
  presets.py      内置提供方 / 预置模型目录（纯数据）
  store/          SQLite schema、repositories（含 routing_trees）、迁移
  console/        静态控制台（HTML/CSS/JS，中英双语）
configs/          signal_questions.json（JEV 问题配置）、default_tree.json（出厂统一树）等
scripts/          运维与联调脚本（部署、上游配置、统一树金标、控制台冒烟）
tests/            pytest（信号纯函数、统一树、admin API、tagger 单测）
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

> **标度说明（2026-10-21 审核 P2 4.4）**：上表准确率是 **argmax 正确率**，与统一树
> `when` 的置信门槛（`conf_min`，模型信号默认 0.70）不在同一标度。置信度 `1 - H(p)/log K`
> 在 K 大时很陡：domain（K=14）要过 0.70 需 p_top ≥ 0.854（折合 T=3.45 下原始 logit gap ≈ 14.9），
> 二类信号需 p ≥ 0.947。README 不提供「过门槛后的条件命中率」，选型评估时请勿把
> argmax 准确率当作门槛通过率使用。`nl_choice` 不叠加 `conf_min`，只用 `min_prob` /
> `min_margin` 两个概率门槛。

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

`/v1` 按当前启用树的**静态信号引用**（所有 `when` 与权重）只计算会被读取的模型信号问题
（`ROUTER_JEV_TRIM=0` 一键关闭）。纯自然语言树没有普通信号引用时跳过打标；纯规则树不触发模型
前向；`/admin/try` 用被试树自己的快照做同样的裁剪（与线上同一 compiler + runtime）。

## 决策快照、账本终态与 config revision

- 每个请求只读一份不可变 `RoutingSnapshot`（编译后的统一树 / 信号 scope / questions / backends /
  DecisionRunner 配置 / `tree_revision` / `snapshot_revision`）；打标与决策期间的配置热更新不会让
  路由与账本错配（进行中的请求用旧快照跑完）。
- 树内容与有效快照分别以 canonical content + sha256 写入 `config_revisions`
  （`kind="tree"` / `kind="snapshot"`，append-only，旧 revision 可回放）。账本记录
  `tree_id / terminal_node_id / tree_revision / snapshot_revision` 与 `TagResult` 的实际评分配置指纹；
  回放用当时快照内容恢复**编译树与候选文本**，不重新读今天的模型目录。
- 流式请求终态 `outcome_state ∈ completed / upstream_error / client_disconnected / cancelled /
  incomplete`，结果更新恰好一次；`GET /admin/requests/stats` 暴露 ring/队列与
  dropped_rows / dropped_updates / partial_rows / ledger_write_failures。
- 不再有 `app.state.pipeline / backends` 兼容别名：hot path 只读 `app.state.snapshot`。

## `/v1` 入口约定

- 没有启用树 → **503** `{"error":{"message":"no_active_tree"}}`（在打标之前返回）。
- 信号准备失败（必需信号缺失 / 结构损坏 / tagger 异常）→ **503**
  `{"error":{"message":"signal_preparation_failed"}}`，绝不静默落到默认模型。
- 启用的健康树仍未命中（不应发生）→ **503** `{"error":{"message":"routing_invariant_violation"}}`，
  并记录完整 trace 到计算日志。
- 请求体必须是 JSON 对象、`messages` 必须是数组：畸形请求回 **400**（不是 500）；
  请求体超过 **10 MB** 回 **413**。
- 上游返回 **4xx** 时按原状态码与错误体透传给客户端（流式与非流式一致），
  不再包装成 200 SSE；只有 5xx / 网络错误才计入熔断失败。
- 流式请求的读超时单独放开（`httpx.Timeout(60.0, read=None)`）：思考型模型首个 token
  可能超过 60s，不应被全局超时截断。

## 模型目录与推理配置

「模型与设置」页是后端模型（统一树 `route` 节点的目标）的唯一配置面，参考
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
- **route 节点选择推理**：`reasoning` 为 `null`（默认）= 完全透传客户端请求体；`off` = 强制
  关闭；档位 id = 强制该档。保存树时按目标模型的能力硬校验（422，启用时也会复检）；运行期若配置
  竞态导致档位不再可用，则降级透传并回 `x-sr-reasoning-warning: unsupported`，不打断用户请求。
  成功注入时响应头带 `x-sr-reasoning: <level>`，计算日志也会记录。

## 统一路由树

一棵树就是一份路由配置：**信号条件**（`when`）、**自然语言多级选择**（`nl_choice`）、
**路由**（`route`）、**拦截**（`block`）和**显式兜底**（`otherwise`）都在同一棵树里表达，
不再有「信号树 / 自然语言树」两种模式。设计文档：
`docs/superpowers/specs/2026-10-26-unified-routing-tree-design.md`（按仓库约定 `docs/` 不入库）。

### 节点类型

| 类型 | 作用 | 关键字段 |
|---|---|---|
| `branch` | 普通分支：按顺序求值子节点（子节点用 `when` 决定是否进入），直接命中优先、加权命中按分数竞争 | `children[]`、可选 `otherwise` |
| `nl_choice` | 自然语言多分支选择：按请求文本 + 本节点问题 + 有序候选选一个候选目标 | `instructions`、`options[2..255]`、`min_prob`(0.45)、`min_margin`(0.10)、可选 `otherwise` |
| `route` | 终止并路由到模型（可带推理档位、可选加权条件） | `model`、`reasoning`、可选 `weights[]`/`min_score` |
| `block` | 终止并拦截 | 可选 `weights[]`/`min_score` |

- 树只有一个 `root`；除根节点外都可以带 `when`（信号条件）；`branch`/`nl_choice` 可带一个
  `otherwise`（自身不能带 `when`）。终止节点不能带 `children`/`otherwise`。
- `branch` 的求值规则：`Miss` 继续下一个子节点；直接 `Hit` 立即返回（丢弃此前的加权候选）；
  加权 `Hit` 暂存、最后取最高分（同分保留较早）；全部无命中才走本节点 `otherwise`。
- `nl_choice` 的门槛只看最高概率 `p_top ≥ min_prob` 与前两名差 `p_top-p_second ≥ min_margin`；
  未过门槛/调用失败/结果非法 = `Miss` → 本节点 `otherwise`（没有则向父节点传播）。
  **选中子树再 Miss 不会尝试第二候选、也不会重调当前节点**——所有备用路径都必须用显式
  `otherwise` 或其他分支表达（避免额外模型调用与语义冲突）。
- 自然语言节点只在**执行路径实际到达时**调用；未到达的节点 0 次模型调用。

### 自然语言候选：model 与 child

```json
{
  "id": "root", "type": "nl_choice",
  "instructions": "根据请求内容选择处理方式。",
  "options": [
    {"key": "fast", "model": "qwen3-8b"},
    {"key": "complex", "description": "需要多步推理、复杂代码或架构分析的任务",
     "child": {"id": "complex", "type": "nl_choice", "instructions": "进一步选择模型。",
               "options": [{"key": "code", "model": "qwen3-32b"},
                           {"key": "reason", "model": "deepseek-v3"}]}}
  ],
  "otherwise": {"id": "safe", "type": "route", "model": "qwen3-8b"}
}
```

- **model 候选**：编译时从模型目录的 `profile.capability` 展开候选文本（`"<model>: <能力说明>"`），
  树配置里不复制能力说明；模型必须存在且能力说明非空（启用时校验）。
- **child 候选**：用 `description` 说明什么情景进入该分支，`child` 可以是任意合法的非根子树
  （包括另一个 `nl_choice`），支持多级自然语言决策。
- 候选顺序是行为的一部分，会进入版本哈希，编译/序列化/日志都不得重排。

### 管理、启停与试算

- `GET/POST /admin/trees`、`GET/PUT /admin/trees/{id}`、`POST /admin/trees/{id}/copy`、
  `POST /admin/trees/{id}/enable|disable`；通用 `PUT` 不接受 `enabled`（422），启停走独立端点。
- 同一时刻**最多一棵树启用**（数据库部分唯一索引强制）；启用一棵会自动关闭其他树；
  **允许全部关闭**（此时 `/v1` 返回 503 `no_active_tree`）。进行中的请求继续用旧快照跑完。
- **试算** `POST /admin/try {tree_id,text,headers?,n_messages?}`：可以试算任意已保存的树
  （不要求启用，也不改变线上快照），返回命中/未命中、信号、逐节点 trace、两级耗时、
  `nl_calls` 与 `tree_id/tree_revision/snapshot_revision`。试算与线上共用同一个编译器与
  `RoutingRuntime`，不维护第二套路由逻辑。
- 控制台「统一路由树」页：左侧信号库 + 树列表（新建/复制/启停/试算），右侧单根画布；
  四类型节点、三类出边（普通子节点 / 模型或分支候选 / `otherwise`）、拖拽与 Alt+↑↓ 排序；
  启用前会提示缺失能力说明、无效门槛、缺失最终兜底等定位错误。

### 配置版本与回放

启用树或修改当前启用树时构建新的不可变快照，`tree_revision` 是树内容的 canonical hash
（不含名称/启用状态/更新时间），`snapshot_revision` 覆盖编译树（含展开后的候选文本）、
信号问题版本、DecisionRunner 配置/模型版本与后端路由相关版本（不含密钥）。修改关闭树不影响
线上快照；修改当前树未引用的模型能力说明不改变有效快照。

### 离线回归

`configs/capability_golden.jsonl`（30 条金标）+ `scripts/unified_tree_eval.py`：通过
`POST /admin/eval {mode:"labels"}` 用当前启用树逐条跑真实 JEV 决策，输出每条请求的
`tree_revision / snapshot_revision / 选中模型 / mismatch` 报告（不实现第二套打分逻辑）：

```bash
ROUTER_ADMIN_EMAIL=... ROUTER_ADMIN_PASSWORD=... \
.venv/bin/python scripts/unified_tree_eval.py \
  --base-url http://127.0.0.1:8000 \
  --golden configs/capability_golden.jsonl \
  --out /tmp/unified-tree-eval.json
```

### 迁移说明（不兼容演进）

旧的 `capability_route` 特殊信号、全局 `capability_*` 策略、Station 执行层级与
`/admin/pipeline` 接口已移除；旧路由配置表不再作为运行输入（历史请求行保留）。
`GET/PUT /admin/pipeline`、`/admin/capability` 不再存在：树配置统一走 `/admin/trees`。

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

新库**没有任何启用树**（`/v1` 会返回 503 `no_active_tree`）：首次上线用控制台
「统一路由树」页新建并启用，或跑
`SR_BASE=http://127.0.0.1:8400 scripts/apply_default_pipeline.py --enable`
（不带 `--enable` 只创建一棵关闭的默认树，绝不隐式恢复线上路由）。出厂统一树数据见
`configs/default_tree.json`。

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

- 单元/集成：`pytest`（1100 passed, 7 skipped；skip 均为缺 GPU/模型/可选依赖的用例）。
- 统一树验收脚本：`scripts/unified_tree_eval.py`（真实 JEV 30 条金标，见「离线回归」）。
- 离线 JEV 评估：`scripts/jev_dump_scores.py`（官方 test 集 raw 分布 dump，需模型）、
  `scripts/jev_calibrate.py`（coverage-risk / AURC / ECE / 各门槛 block recall 报告，纯 CPU）、
  `scripts/eval_jev_window_ab.py`（输入窗口 head/tail/head+tail A/B，需 GPU）、
  `scripts/cheap_sufficient_mvp.py`（GSM8K 上便宜档 C vs 强档 S 的 cheap_sufficient 离线实验，需两个模型）。
- i18n 死键 lint：`python scripts/i18n_dead_keys.py`（有死键时退出码非 0，可挂 CI；
  `scripts/i18n_prune_i18n.py` 负责批量删除）。
- 控制台冒烟：`BASE=http://<host>:<port> LOGIN_EMAIL=<admin> LOGIN_PASSWORD=<pass> node scripts/console_check.js`
  （七个页面含登录页/用户管理页；统一路由树多树列表、四类型节点编辑、候选/`otherwise` 出边、
  保存不带 enabled、导入导出 round-trip、试算与日志共享 trace、启停与全关闭 503；脚本先用
  账号登录换会话 token 再做检查）。
