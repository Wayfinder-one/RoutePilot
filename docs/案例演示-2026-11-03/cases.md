# 案例演示决策组设计说明（RoutePilot graph v3，2026-11-03）

> 本文是演示案例的交付说明：在 76 的 RoutePilot v3（`http://192.168.95.76:8400`）上新增的
> **5 个演示决策组 + 15 个演示模型**的设计、画布结构、dry-run 基线结果与复现方式。
> 相邻文档：`infra.md`（76 演示后端：VL 服务与 demo-tap 映射/日志）、`README.md` 与 `evidence/`
> （端到端实时取证，由 evidence/task-4 产出）。
>
> **2026-11-23 更新**：画布改为严格自包含运行快照（四个必填运行字段），JEV 改为多服务目录
> （`demo-jev`，窗口属于服务），目录修改需确认影响后同步。§二.4、§八、§九 已按最终行为重写；
> 五棵画布的图结构、演示 prompt 与 22 条 dry-run 基线不变。

## 一、交付概览

### 1.1 一句话

用管理 API 在 76 上新建 provider `demo-tap`、15 个模型位与 5 棵 graph v3 决策组，分别演示
**行业模型分流 / 多模态分流 / 推理等级 / 请求条件与逻辑门 / 调度与容错**；全部经
`POST /admin/try` 逐条 dry-run 验证（22/22 命中期望），保持**未启用**，不触碰既有
`default-jev` 与既有模型/provider。

### 1.2 环境与入口

| 项 | 值 |
|---|---|
| RoutePilot 服务 | `http://192.168.95.76:8400`（systemd `smart-router-v3.service`，DB `/opt/smart-router-v3/router-v3.db`） |
| 管理面登录 | `POST /admin/login`（`admin@test.com`，Bearer token） |
| 图格式 | `sr-routing-graph v3`（`root.type="graph"`），引擎语义见 `router/routing/graph/engine.py`。每份文件都是完整 v3 文档：`kind` / `version` + 四个必填运行字段 `backends` / `jev` / `jev_window` / `time_periods`；文件内不含外部 `id` / `enabled` |
| 决策服务 | 多服务 JEV 目录中的 `demo-jev` = llama.cpp `d1-2B`（`127.0.0.1:30104`），每服务独立窗口 `2048 / 4096`，单次请求多问题合并打分 |
| 演示上游 | provider `demo-tap` → `http://127.0.0.1:9300/v1`（tap 代理，映射见 `infra.md` §2.2） |
| 部署/校验脚本 | `python3 scripts/demo/apply_cases.py [--confirm-sync]`（案例 JSON 是唯一事实源；遇到影响确认默认打印清单并停止） |
| 图文件 | `configs/cases/case-*.json`（5 份可直接导入的完整 v3 文档，导入路径见 §8.4）+ `configs/cases/manifest.json`（决策组 id/名称、演示 prompt 与期望） |

### 1.3 5 棵决策组清单

| id | 中文名 | 节点/连线/问题 | sticky | 演示 prompt | dry-run |
|---|---|---|---|---|---|
| `case-industry` | 案例一 · 行业模型分流 | 8 / 7 / 1 | 关（1800s） | 6 | 6/6 ✓ |
| `case-multimodal` | 案例二 · 多模态分流 | 4 / 3 / 1 | 关（1800s） | 4 | 4/4 ✓ |
| `case-reasoning` | 案例三 · 推理等级 | 6 / 5 / 1 | 关（1800s） | 3 | 3/3 ✓ |
| `case-logic` | 案例四 · 请求条件与逻辑门 | 16 / 15 / 2 | 关（1800s） | 6 | 6/6 ✓ |
| `case-ha` | 案例五 · 调度与容错 | 7 / 5 / 1 | **开（1800s）** | 3 | 3/3 ✓ |

- 5 份案例 JSON 各自内嵌 provider `demo-tap`、JEV 服务 `demo-jev`（含 `id` / `key_env` 与
  `2048 / 4096` 窗口）以及各 target 实际引用的模型与时段；每份文件都能直接在空数据库实例上
  导入并运行，不依赖目标实例的数据库目录。
- 5 棵树保存时通过结构校验；`POST /admin/try` 返回的 enable 级 `warnings` 均为空
  （画布内嵌模型/档位/时段齐全、JEV 已声明），即具备直接 `POST /admin/trees/{id}/enable` 的条件
  （画布声明与数据库目录不一致只会产生 hint 级 `runtime_mismatch` 提示，不阻塞试算）。
- **本任务不启用任何新树**：试算用 `POST /admin/try {tree_id}` 即可。实时取证期间由
  evidence 按需 enable/disable，取证结束恢复 `default-jev` 为 enabled。
- 画布坐标均为手工分层布点（`position`），已按真实控制台渲染尺寸逐棵检查**无节点重叠**；
  画布截图由 task-4 统一产出到 `shots/routing-<tree id>.png`。

## 二、演示资源：provider 与模型目录

### 2.1 provider

| name | base_url | key_env | reasoning_format | 说明 |
|---|---|---|---|---|
| `demo-tap` | `http://127.0.0.1:9300/v1` | （空） | `openai` | 只监听 76 本机；router 与 tap 同机直连。映射键 → 真实 vLLM/kthena 后端见 `infra.md` §2.2，每请求写入 `/opt/sr-demo/tap.jsonl` |

### 2.2 15 个模型位

15 个模型都写入了 `models.profile.capability`（一句话中文能力说明）：控制台「模型设置」页的
**「能力说明」列**直接展示该字段。模型的唯一事实源是五份案例 JSON 的 `backends.models`；
`apply_cases.py` 从案例文件合并出目录（同键不同值直接报错，绝不 last-wins），幂等重跑会保持
该字段，并在写完后回读校验 15 条 capability 均非空且与文件一致。

| model id | upstream（tap 映射键） | domains | tier | 推理声明（归一化后） | 能力说明（profile.capability） |
|---|---|---|---|---|---|
| `industry-medical` | `industry-medical` | `["health"]` | small | 未声明 | 医疗健康行业模型：症状与用药咨询、检查报告解读、病历与临床指南问答 |
| `industry-legal` | `industry-legal` | `["legal"]` | small | 未声明 | 法律合规行业模型：合同审查、劳动争议、诉讼流程与合规风险问答 |
| `industry-finance` | `industry-finance` | `["finance"]` | small | 未声明 | 金融财务行业模型：投资理财、行情分析、信贷风控、财务报表与税务 |
| `industry-energy` | `industry-energy` | `["energy"]` | large | 未声明 | 电力能源行业模型：电网调度、抢修与设备缺陷、安规两票、电价计量 |
| `industry-code` | `industry-code` | `["computer_science"]` | large | 未声明 | 编程代码模型：代码编写、调试排错、代码审查、算法与数据结构 |
| `industry-general` | `industry-general` | `[]` | large | 未声明 | 通用对话模型：闲聊、常识问答、写作润色、翻译、摘要 |
| `vl-qwen35-4b` | `vl-model` | `["multimodal"]` | large | 未声明 | 视觉多模态模型：截图/照片/图表理解，支持图像输入与看图问答 |
| `text-general` | `general-backend` | `[]` | large | 未声明 | 通用文本模型：写作、总结、翻译、信息提取等纯文本请求 |
| `reason-qwen` | `reason-base` | `[]` | large | `qwen-chat-template`，efforts `off/on`，default `off` | 推理等级模型（qwen-chat-template 方言）：off/on 思考开关，默认关闭思考 |
| `reason-openai` | `reason-base` | `[]` | large | `openai`，efforts `low/medium/high`，default `medium` | 推理等级模型（openai 方言）：reasoning_effort 低/中/高三档，默认中等 |
| `reason-deepseek` | `reason-base` | `[]` | large | `deepseek`，efforts `high/max`，default `high` | 推理等级模型（deepseek 方言）：thinking + reasoning_effort，支持高/最高档 |
| `ha-slow-a` | `slow-a` | `[]` | large | 未声明 | 调度演示后端（慢响应 A）：模拟带 1.2s 延迟的正常后端，用于并发分摊演示 |
| `ha-slow-b` | `slow-b` | `[]` | large | 未声明 | 调度演示后端（慢响应 B）：与 A 同构的慢后端，用于 least_inflight 分摊与粘滞演示 |
| `ha-fail` | `fail-upstream` | `[]` | large | 未声明 | 故障模拟后端：立即返回 503，用于演示熔断打开与请求转移 |
| `ha-general` | `general-backend` | `[]` | large | 未声明 | 通用兜底后端：正常返回 200，用于熔断转移与 otherwise 兜底场景 |

### 2.3 三种推理协议（案例三用）

| 模型 | format | 声明档位 | 默认 | 注入到上游请求体的字段（`apply_reasoning`） |
|---|---|---|---|---|
| `reason-qwen` | `qwen-chat-template` | off(on) | off | `chat_template_kwargs.enable_thinking=false`（off）/ `true`（on） |
| `reason-openai` | `openai` | low/medium/high | medium | `reasoning_effort="medium"` |
| `reason-deepseek` | `deepseek` | high/max | high | `thinking={"type":"enabled"}` + `reasoning_effort="max"` |

- 归一化校验：三者在 `GET /admin/models` 中均为 `declared=true, supported=true`，
  `efforts` 含目标所引用的档位（`max`/`medium`/`off`），所以启用校验无 `reasoning_unsupported`。
- 实时链路证据：`/v1/chat/completions` 响应头 `x-sr-reasoning: <档位>`，且 tap 日志
  `/opt/sr-demo/tap.jsonl` 的 `reasoning_effort` / `thinking` / `chat_template_kwargs` 字段可见注入值。
- dry-run（`/admin/try`）本身不调用上游，故试算只验证「选了哪个模型/档位」，注入证据需实时请求。

### 2.4 JEV 服务与窗口（多服务目录）

五份案例统一声明同一个服务 `demo-jev`：

| 字段 | 值 | 说明 |
|---|---|---|
| `id` | `demo-jev` | 目录主键；画布快照按它引用服务 |
| `base_url` | `http://127.0.0.1:30104` | llama.cpp `d1-2B` |
| `model` | `d1-2B` | 决策服务模型名 |
| `key_env` | （空） | 本机 JEV 无需鉴权；真实密钥永不进 JSON |
| `timeout_seconds` | `10.0` | |
| `input_tokens` / `context_tokens` | `2048 / 4096` | **窗口属于服务**，画布选择/同步 JEV 时与档案一起替换 |

- 数据库改为多服务目录 `jev_services`；`apply_cases.py` 用 `POST /admin/jev`（不存在时）或
  `PUT /admin/jev/demo-jev`（已存在时）写入，并在 `GET /admin/jev` 回读 `demo-jev` 的窗口 2048/4096。
- 旧单 JEV 端点（`PUT /admin/jev`、`POST /admin/jev/test`、`GET/PUT /admin/jev/window`）已删除，
  文档/脚本不再调用；窗口请读 `GET /admin/jev` 的 `services[].input_tokens/context_tokens`。
- 窗口 2048/4096 是案例的固定事实：`case-logic` 的演示 prompt 依赖输入截断行为；改动窗口需重跑
  全部 dry-run 基线。

## 三、案例一 · 行业模型分流（`case-industry`）

### 3.1 展示的功能点

1. **行业模型目录**：6 个行业专用模型位（医疗/法律/金融/电力/编程/通用）带 `domains/tier` 能力画像；
2. **JEV 单问题多候选**：一个 choice 问题把 6 个候选的「能力说明」一次性交给 d1-2B 打分，按 `p_top` 选择；
3. **target priority 6..1**：全图 priority 唯一，数值越大越优先；
4. **否则/异常兜底**：`otherwise_target` 与 `error_fallback` 都收敛到 `industry-general-target`，请求永不落空。

### 3.2 树定义

| 项 | 值 |
|---|---|
| id / 名称 | `case-industry` / 案例一 · 行业模型分流 |
| 时区 / sticky | `Asia/Shanghai` / `{enabled:false, ttl_seconds:1800}` |
| otherwise_target / error_fallback | `industry-general-target` / 同 |

### 3.3 画布结构

节点（`x,y` 为画布坐标，按 3 列分层：start → 问题 → 目标列）：

| 节点 id | 类型 | 标题 | 关键配置 | position |
|---|---|---|---|---|
| `start` | start | 开始 | — | (40, 360) |
| `industry` | question/choice | 行业领域 | 6 个选项（见下），instructions 要求按行业能力说明选择 | (320, 360) |
| `industry-medical-target` | target | 医疗健康模型 | priority 6，models `[industry-medical]` | (660, -60) |
| `industry-legal-target` | target | 法律合规模型 | priority 5，models `[industry-legal]` | (660, 100) |
| `industry-finance-target` | target | 金融财务模型 | priority 4，models `[industry-finance]` | (660, 260) |
| `industry-energy-target` | target | 电力能源模型 | priority 3，models `[industry-energy]` | (660, 420) |
| `industry-code-target` | target | 编程代码模型 | priority 2，models `[industry-code]` | (660, 580) |
| `industry-general-target` | target | 通用兜底模型 | priority 1，models `[industry-general]` | (660, 740) |

choice 选项（key / label / capability 摘要）：

| key | label | capability 摘要 |
|---|---|---|
| `medical` | 医疗健康模型 | 疾病症状与用药、检查检验报告、病历与诊断文书、健康管理、医学术语 |
| `legal` | 法律合规模型 | 合同审查、劳动争议与赔偿、诉讼流程与管辖、侵权责任、知识产权、合规风险 |
| `finance` | 金融财务模型 | 投资理财与资产配置、股票基金行情、信贷风控、财务报表与税务、保险与宏观数据 |
| `energy` | 电力能源模型 | 电网调度、运维抢修与设备缺陷、安规两票、输变电与继保、电价计量、新能源并网 |
| `code` | 编程代码模型 | 编码补全、调试排错、代码审查重构、算法数据结构、框架选型、SQL/脚本 |
| `general` | 通用对话模型 | 闲聊、常识问答、写作润色、翻译、摘要、生活建议等无行业归属请求 |

边（7 条，全部为布尔「选中」端口，`condition:null`）：

| 边 id | 源端口 | 目标槽位 | 条件 |
|---|---|---|---|
| `e-start-industry` | `start.out` | `industry.in_1` | null |
| `e-selected-medical` | `industry.selected.medical` | `industry-medical-target.in_1` | null |
| `e-selected-legal` | `industry.selected.legal` | `industry-legal-target.in_1` | null |
| `e-selected-finance` | `industry.selected.finance` | `industry-finance-target.in_1` | null |
| `e-selected-energy` | `industry.selected.energy` | `industry-energy-target.in_1` | null |
| `e-selected-code` | `industry.selected.code` | `industry-code-target.in_1` | null |
| `e-selected-general` | `industry.selected.general` | `industry-general-target.in_1` | null |

### 3.4 演示 prompt 与 dry-run 结果

（`POST /admin/try {tree_id:"case-industry", text:"…"}`，无 `now`；下表全部来自线上实测）

| # | 演示 prompt | 期望 target → model | 实际 target → model | JEV 证据（p_top / margin / ranking） | 结论 |
|---|---|---|---|---|---|
| 1 | 最近体检查出甲状腺结节，需要怎么复查和用药？ | `industry-medical-target` → `industry-medical` | 同 | `medical` p_top **0.978** / margin 0.966；ranking `[medical, general, finance, legal, code, energy]` | ✓ |
| 2 | 公司要和我解除劳动合同，经济补偿金应该怎么算？ | `industry-legal-target` → `industry-legal` | 同 | `legal` p_top **0.908** / margin 0.850；ranking `[legal, finance, general, code, medical, energy]` | ✓ |
| 3 | 现在手里有30万闲置资金，想配置一部分债券基金，有什么建议？ | `industry-finance-target` → `industry-finance` | 同 | `finance` p_top **0.974** / margin 0.954；ranking `[finance, general, legal, energy, medical, code]` | ✓ |
| 4 | 10kV配电线路发生单相接地故障，应该怎么排查和隔离？ | `industry-energy-target` → `industry-energy` | 同 | `energy` p_top **0.969** / margin 0.943；ranking `[energy, general, finance, legal, code, medical]` | ✓ |
| 5 | 帮我用Python写一个读取CSV并聚合统计的脚本。 | `industry-code-target` → `industry-code` | 同 | `code` p_top **0.922** / margin 0.859；ranking `[code, general, finance, legal, energy, medical]` | ✓ |
| 6 | 周末适合带孩子去哪些地方玩？ | `industry-general-target` → `industry-general` | 同 | `general` p_top **0.988** / margin 0.985；ranking `[general, finance, code, medical, energy, legal]` | ✓ |

### 3.5 讲解要点

- 6 个 `selected.*` 端口同一波只有一个为 true，因此每请求恰好一个 target 候选 → 无 priority 竞争；
  priority 6..1 用来表达「行业越专属越优先」的展示语义，同时满足全图 priority 唯一约束。
- 若 JEV 选到 `general`（或选项概率集合对不上导致整波作废），仍由 `otherwise` / `error_fallback`
  兜底到通用模型；把兜底 target 放在目标列最下方便于画布讲解。

## 四、案例二 · 多模态分流（`case-multimodal`）

### 4.1 展示的功能点

1. **输入模态判断**：一个 choice 问题区分「纯文本」与「带图」请求；
2. **视觉模型承接**：`vl-qwen35-4b`（upstream `vl-model` → qwen4b-vl）处理截图/照片/图表；
3. **多模态请求透传**：router 只做路由与模型替换，图像 content 原样透传给上游（tap 日志 `has_image=true` 佐证）；
4. **兜底回文本模型**：`otherwise_target`/`error_fallback` 都指向 `text-target`。

### 4.2 树定义与画布结构

| 项 | 值 |
|---|---|
| id / 名称 | `case-multimodal` / 案例二 · 多模态分流 |
| 时区 / sticky | `Asia/Shanghai` / 关（1800s） |
| otherwise_target / error_fallback | `text-target` / 同 |

| 节点 id | 类型 | 标题 | 关键配置 | position |
|---|---|---|---|---|
| `start` | start | 开始 | — | (40, 220) |
| `modality` | question/choice | 输入模态 | 2 选项：`text`（文本对话模型）/ `image`（视觉多模态模型） | (330, 220) |
| `text-target` | target | 文本模型（text-general） | priority 1，models `[text-general]` | (680, 110) |
| `image-target` | target | 视觉模型（vl-qwen35-4b） | priority 2，models `[vl-qwen35-4b]` | (680, 330) |

| 边 id | 源端口 | 目标槽位 | 条件 |
|---|---|---|---|
| `e-start-modality` | `start.out` | `modality.in_1` | null |
| `e-selected-text` | `modality.selected.text` | `text-target.in_1` | null |
| `e-selected-image` | `modality.selected.image` | `image-target.in_1` | null |

选项 capability 摘要：

- `text`：纯文本模型，接收文字输入，擅长写作/总结/翻译/代码/常识问答/信息提取；
- `image`：视觉多模态模型，接收文字与图片，擅长识别截图、照片、图表、表格与页面内容，请求中带有图片或要求看图。

### 4.3 演示 prompt 与 dry-run 结果

| # | 演示 prompt | 期望 target → model | 实际 | JEV 证据（p_top / margin / ranking） | 结论 |
|---|---|---|---|---|---|
| 1 | 我发了一张系统报错截图，请帮我看一下这个错误是什么原因？ | `image-target` → `vl-qwen35-4b` | 同 | `image` p_top **0.997** / margin 0.994；ranking `[image, text]` | ✓ |
| 2 | 这张照片里的菜单是法文的，帮我翻译成中文。 | `image-target` → `vl-qwen35-4b` | 同 | `image` p_top **0.954** / margin 0.907；ranking `[image, text]` | ✓ |
| 3 | 帮我写一段产品发布会的开场白，控制在200字以内。 | `text-target` → `text-general` | 同 | `text` p_top **0.998** / margin 0.996；ranking `[text, image]` | ✓ |
| 4 | 请根据下面这段会议记录，整理出三条待办事项。 | `text-target` → `text-general` | 同 | `text` p_top **0.912** / margin 0.823；ranking `[text, image]` | ✓ |

### 4.4 讲解要点

- 真实多模态请求由调用方在 `messages` 里放 `image_url`（data URI 或 URL）；router 不解析图像，
  只按**文本语义**判断模态，因此 prompt 里需要出现「截图/照片/看图」等线索。
- 实时联调时注意带上 `max_tokens`（VL 真图建议 160~260），见 `infra.md` §5。

## 五、案例三 · 推理等级（`case-reasoning`）

### 5.1 展示的功能点

1. **score 数值问题**：JEV 在 `criteria [1,3,6,8,10]` 上给「推理深度」分值（离散档）；
2. **数值区间条件的两种写法**：
   - 单边条件直连：`value >= 8` → 深度推理 target；
   - AND 门组合区间：`value > 4` AND `value < 8` → 标准推理 target（命中 6 档）；
3. **否则兜底 = 轻量直答**（score 1/3 档不浪费推理预算）；
4. **三种推理协议注入**：qwen-chat-template(off) / openai(medium) / deepseek(max)，
   实时链路可由响应头 `x-sr-reasoning` 与 tap 日志取证。

### 5.2 树定义与画布结构

| 项 | 值 |
|---|---|
| id / 名称 | `case-reasoning` / 案例三 · 推理等级 |
| 时区 / sticky | `Asia/Shanghai` / 关（1800s） |
| otherwise_target / error_fallback | `light-target` / 同 |

| 节点 id | 类型 | 标题 | 关键配置 | position |
|---|---|---|---|---|
| `start` | start | 开始 | — | (40, 280) |
| `depth` | question/score | 推理深度 | criteria `[1,3,6,8,10]`；instructions 要求按「简单直接 ↔ 复杂多步推理」打分 | (330, 280) |
| `and-mid` | logic/and | 与门 · 中等推理（4 < 分 < 8） | 两个输入槽都是数值区间边 | (620, 350) |
| `deep-target` | target | 深度推理（reason-deepseek:max） | priority 3，models `[{reason-deepseek, max}]` | (930, 100) |
| `standard-target` | target | 标准推理（reason-openai:medium） | priority 2，models `[{reason-openai, medium}]` | (930, 320) |
| `light-target` | target | 轻量直答（reason-qwen:off） | priority 1，models `[{reason-qwen, off}]` | (930, 540) |

| 边 id | 源端口 | 目标槽位 | 条件 |
|---|---|---|---|
| `e-start-depth` | `start.out` | `depth.in_1` | null |
| `e-depth-deep` | `depth.value` | `deep-target.in_1` | `{op:"gte", value:8}` |
| `e-depth-mid-gt` | `depth.value` | `and-mid.in_1` | `{op:"gt", value:4}` |
| `e-depth-mid-lt` | `depth.value` | `and-mid.in_2` | `{op:"lt", value:8}` |
| `e-mid-standard` | `and-mid.out` | `standard-target.in_1` | null |

判定真值表（score 只能取 criteria 之一）：

| score | `>=8` 边 | `>4` 边 | `<8` 边 | AND 门 | 最终 target |
|---|---|---|---|---|---|
| 10 / 8 | true | true | false | false | `deep-target` |
| 6 | false | true | true | **true** | `standard-target`（AND 输出） |
| 3 / 1 | false | false | true | false | `otherwise_target` → `light-target` |

### 5.3 演示 prompt 与 dry-run 结果

| # | 演示 prompt | 期望 target → model（reasoning） | 实际 | JEV 证据 | 结论 |
|---|---|---|---|---|---|
| 1 | 请结合公司近三年的财务报表、行业竞争格局和宏观政策，设计一套多维度的经营风险评估框架，并说明各指标权重如何确定。 | `deep-target` → `reason-deepseek`（`max`） | 同 | score = **8** | ✓ |
| 2 | 帮我写一封邮件，向客户说明项目延期一周，并给出新的交付计划。 | `standard-target` → `reason-openai`（`medium`） | 同 | score = **6**（落 4~8 区间） | ✓ |
| 3 | 今天星期几？ | `light-target` → `reason-qwen`（`off`） | 同 | score = **3** → 无候选，走 `otherwise`（`selection_reason=otherwise`） | ✓ |

### 5.4 讲解要点

- score 的取值由 criteria 决定（不是连续值），因此区间条件要按 criteria 的离散点配合阈值：
  这里 `>4 且 <8` 恰好只命中 6 档。
- 深推理 / 标准档 / 轻量档分别声明三种不同推理协议，用来证明「同一张图里可以混用不同上游
  推理方言」；实时请求的 `x-sr-reasoning` 响应头会回显实际注入档位。
- `off` 档对 `qwen-chat-template` 是 `enable_thinking=false`（显式关思考），不是「不发字段」。

## 六、案例四 · 请求条件与逻辑门（`case-logic`）

### 6.1 展示的功能点

1. **text 关键词 condition**：两级关键词（告警/抢修/故障/紧急；故障/抢修）；
2. **periods 时段 condition**：`白天` / `夜间`（可配 `now` 模拟）；
3. **noul 问题「是否紧急」**：`yes >= 0.5` 视为紧急（noul 输出的是 yes/no 概率，数值端口必须带比较条件）；
4. **score 问题「严重度」**：`[1,3,6,8,10]`；
5. **AND / OR 两型逻辑门 + 数值反向条件**：
   - AND：关键词 **且** 紧急 → 紧急处置；
   - OR：白天 **或** 故障 → 值班响应；
   - 数值反向条件：`q-urgent.yes < 0.5`（非紧急）→ 常规处理；
   - AND：夜间 **且** 严重度 >= 8 → 自动升级紧急；
   - AND：严重度 4 < 分 < 8 → 排期复审（数值区间条件）；
6. **otherwise / error_fallback 分离**：无候选 → 常规处理；决策波失败 → 独立兜底 target。

### 6.2 树定义与画布结构

| 项 | 值 |
|---|---|
| id / 名称 | `case-logic` / 案例四 · 请求条件与逻辑门 |
| 时区 / sticky | `Asia/Shanghai` / 关（1800s） |
| otherwise_target / error_fallback | `t-normal` / `t-fallback` |

节点（左侧条件列 / 中间逻辑门列 / 右侧目标列）：

| 节点 id | 类型 | 标题 | 关键配置 | position |
|---|---|---|---|---|
| `start` | start | 开始 | — | (40, 380) |
| `c-keyword` | condition/text | 关键词 · 告警/抢修/故障/紧急 | `contains_any:["告警","抢修","故障","紧急"]` | (330, -40) |
| `c-fault` | condition/text | 关键词 · 故障/抢修 | `contains_any:["故障","抢修"]` | (330, 100) |
| `c-day` | condition/periods | 时段 · 白天 | `periods:["白天"]` | (330, 240) |
| `c-night` | condition/periods | 时段 · 夜间 | `periods:["夜间"]` | (330, 380) |
| `q-urgent` | question/noul | 是否紧急 | instructions：告警/故障是否紧急、是否需立即处置 | (330, 540) |
| `q-severity` | question/score | 严重度 | criteria `[1,3,6,8,10]` | (330, 700) |
| `g-and-urgent` | logic/and | 与门 · 关键词且紧急 | 2 输入 | (640, 40) |
| `g-or-dayfault` | logic/or | 或门 · 白天或故障 | 2 输入 | (640, 180) |
| `g-and-night` | logic/and | 与门 · 夜间且高严重度 | 2 输入 | (640, 460) |
| `g-and-severity` | logic/and | 与门 · 严重度中段（4–8） | 2 输入（数值区间） | (640, 620) |
| `t-emergency` | target | 紧急处置（industry-energy） | priority 5，models `[industry-energy]`，2 个输入槽 | (970, 80) |
| `t-oncall` | target | 值班响应（ha-general） | priority 4，models `[ha-general]` | (970, 240) |
| `t-review` | target | 排期复审（ha-general） | priority 3，models `[ha-general]` | (970, 400) |
| `t-normal` | target | 常规处理（ha-general） | priority 2，models `[ha-general]`，也是 `otherwise_target` | (970, 560) |
| `t-fallback` | target | 异常兜底（ha-general） | priority 1，models `[ha-general]`，`error_fallback` | (970, 720) |

边（15 条）：

| 边 id | 源端口 | 目标槽位 | 条件 |
|---|---|---|---|
| `e-start-urgent` | `start.out` | `q-urgent.in_1` | null |
| `e-start-severity` | `start.out` | `q-severity.in_1` | null |
| `e-kw-and` | `c-keyword.out` | `g-and-urgent.in_1` | null |
| `e-urgent-and` | `q-urgent.yes` | `g-and-urgent.in_2` | `{op:"gte", value:0.5}` |
| `e-and-emergency` | `g-and-urgent.out` | `t-emergency.in_1` | null |
| `e-day-or` | `c-day.out` | `g-or-dayfault.in_1` | null |
| `e-fault-or` | `c-fault.out` | `g-or-dayfault.in_2` | null |
| `e-or-oncall` | `g-or-dayfault.out` | `t-oncall.in_1` | null |
| `e-nonurgent-normal` | `q-urgent.yes` | `t-normal.in_1` | `{op:"lt", value:0.5}` |
| `e-night-and` | `c-night.out` | `g-and-night.in_1` | null |
| `e-sev-night` | `q-severity.value` | `g-and-night.in_2` | `{op:"gte", value:8}` |
| `e-andnight-emergency` | `g-and-night.out` | `t-emergency.in_2` | null |
| `e-sev-andgt` | `q-severity.value` | `g-and-severity.in_1` | `{op:"gt", value:4}` |
| `e-sev-andlt` | `q-severity.value` | `g-and-severity.in_2` | `{op:"lt", value:8}` |
| `e-andsev-review` | `g-and-severity.out` | `t-review.in_1` | null |

判定语义小结：

- 关键词条件与 noul 概率都在**第 0/1 波**就绪：conditions 在 phase 0 求值，两个问题在第 1 波合并
  调用 JEV，同一波返回后逻辑门做三值定点求解；
- 多候选同时激活时按 target priority 决胜（`t-emergency` > `t-oncall` > `t-review` > `t-normal`），
  trace 的 `final.candidates` 可看到全部候选；
- `t-normal` 既可由 `yes < 0.5` 的数值反向条件激活，也承担 `otherwise` 兜底；`t-fallback` 只接 `error_fallback`。

### 6.3 演示 prompt 与 dry-run 结果

| # | 演示 prompt | now | 期望 target → model | 实际 target | 候选集 | JEV 证据 | 结论 |
|---|---|---|---|---|---|---|---|
| 1 | 10kV线路出现接地告警，抢修班组已经到场，需要立即处置，设备严重过载。 | （默认=白天） | `t-emergency` → `industry-energy` | 同 | `[t-emergency, t-oncall]` | 严重度 **10**；紧急 **yes=0.994** | ✓ |
| 2 | 夜深了，想问问明天营业厅几点开门。 | `2026-11-03T23:30:00` | `t-normal` → `ha-general` | 同 | `[t-normal]` | 严重度 **3**；yes=0.014（`yes < 0.5` 命中） | ✓ |
| 3 | 白天收到一条设备告警短信，影响不大，想确认一下常规巡检流程。 | `2026-11-03T10:30:00` | `t-oncall` → `ha-general` | 同 | `[t-oncall, t-normal]` | 严重度 3；yes=0.011；OR 门白天分支为真 | ✓ |
| 4 | 白天收到一条设备告警短信，影响不大，想确认一下常规巡检流程。 | `2026-11-03T23:30:00` | `t-normal` → `ha-general` | 同 | `[t-normal]` | 同一句话改到夜间 → OR 门假 | ✓ |
| 5 | 某小区计划停电检修，涉及300户居民，需要制定风险控制方案和通知计划。 | `2026-11-03T23:30:00` | `t-review` → `ha-general` | 同 | `[t-review, t-normal]` | 严重度 **6** 落 4~8 区间 | ✓ |
| 6 | 夜里发现主变油温异常升高，冷却器全停，情况非常危急。 | `2026-11-03T22:30:00` | `t-emergency` → `industry-energy` | 同 | `[t-emergency]` | 严重度 10 + 夜间 → AND 门升级 | ✓ |

> 3/4 是同一句话在白天与夜间的对照：时段条件直接改变 OR 门结果，用来演示 periods 条件的作用。

### 6.4 讲解要点

- `now` 只在 `/admin/try` 生效（模拟时段）；实时 `/v1` 请求按服务器当前时间求值，无法注入。
- `case-logic` 自己内嵌两个时段：白天 = 周一~周五 09:00-18:00；夜间 = 每天 18:00-09:00。
  `apply_cases.py` 部署后的数据库目录（`GET /admin/time-periods`）只有这两个；既有 `default-jev`
  画布自带「周末」定义，但不影响本案例（案例不使用周末）。
- 因此实时演示时（白天时段）任何非紧急请求都会经 OR 门命中 `t-oncall`，这是设计使然；
  「夜间/常规」对照建议放在试算里演示。

## 七、案例五 · 调度与容错（`case-ha`）

### 7.1 展示的功能点

1. **least_inflight 双后端资源池**：`ha-balanced` 的 models `[ha-slow-a, ha-slow-b]`，
   按各模型在途请求数取最小者，并发时自动分摊；
2. **同 session 粘滞**：树级 `sticky.enabled=true / ttl_seconds=1800`，同一 `x-sr-session-id`
   在 30 分钟内固定命中同一后端（`x-sr-selection: sticky`）；
3. **熔断转移**：`ha-complex` 的 models `[ha-fail, ha-general]`，`ha-fail` 连续 3 次 5xx 后熔断，
   第 4 次请求自动落到 `ha-general` 返回 200；
4. **单点熔断 503**：`ha-drill` 只有 `ha-fail`，熔断打开后再请求返回
   `503 {"error":{"message":"no_available_backend"}}`；
5. **优先级演示**：同一个 `scene.selected.balanced` 端口同时连接 `ha-balanced`（priority 5）与
   `ha-balanced-backup`（priority 4），两候选同时激活时高优先级胜出，`final.candidates` 可见两者。

### 7.2 树定义与画布结构

| 项 | 值 |
|---|---|
| id / 名称 | `case-ha` / 案例五 · 调度与容错 |
| 时区 / sticky | `Asia/Shanghai` / **`{enabled:true, ttl_seconds:1800}`** |
| otherwise_target / error_fallback | `ha-general-target` / 同 |

| 节点 id | 类型 | 标题 | 关键配置 | position |
|---|---|---|---|---|
| `start` | start | 开始 | — | (40, 320) |
| `scene` | question/choice | 调度场景 | 3 选项：`balanced`/`complex`/`drill` | (330, 320) |
| `ha-balanced` | target | 均衡调度（ha-slow-a / ha-slow-b · least_inflight） | priority 5，models `[ha-slow-a, ha-slow-b]` | (720, 20) |
| `ha-balanced-backup` | target | 均衡调度 · 低优先备份（ha-general） | priority 4，models `[ha-general]` | (720, 200) |
| `ha-complex` | target | 复杂推理（ha-fail → ha-general 熔断转移） | priority 3，models `[ha-fail, ha-general]` | (720, 380) |
| `ha-drill` | target | 故障演练（ha-fail 单点熔断） | priority 2，models `[ha-fail]` | (720, 560) |
| `ha-general-target` | target | 通用兜底（ha-general） | priority 1，models `[ha-general]`，兜底 | (720, 740) |

| 边 id | 源端口 | 目标槽位 | 条件 |
|---|---|---|---|
| `e-start-scene` | `start.out` | `scene.in_1` | null |
| `e-selected-balanced` | `scene.selected.balanced` | `ha-balanced.in_1` | null |
| `e-selected-balanced-backup` | `scene.selected.balanced` | `ha-balanced-backup.in_1` | null |
| `e-selected-complex` | `scene.selected.complex` | `ha-complex.in_1` | null |
| `e-selected-drill` | `scene.selected.drill` | `ha-drill.in_1` | null |

场景选项 capability 摘要：

- `balanced`：两个同构慢速后端组成资源池，按最少在途请求分摊并发流量，同一会话保持粘滞；
- `complex`：主后端与通用备用后端组合，主后端连续失败触发熔断后由通用后端接管；
- `drill`：只指向故障后端，连续失败触发熔断，用于观察无可用后端时的 503 兜底。

### 7.3 演示 prompt 与 dry-run 结果

| # | 演示 prompt | 期望 target → model | 实际 | 候选集（priority 演示） | JEV 证据 | 结论 |
|---|---|---|---|---|---|---|
| 1 | 请把一批请求均匀分给两个后端，演示并发流量分摊和会话粘滞。 | `ha-balanced` → `ha-slow-a` 或 `ha-slow-b` | `ha-balanced` → `ha-slow-a`（least_inflight 平局取声明顺序第一个） | `[ha-balanced(5), ha-balanced-backup(4)]` → 5 胜出 | `balanced` p_top **0.997** / margin 0.995 | ✓ |
| 2 | 这次请求很复杂，需要多步推理和长链路分析。 | `ha-complex` → `ha-fail` | 同 | `[ha-complex]` | `complex` p_top **0.931** / margin 0.894 | ✓ |
| 3 | 模拟一次上游故障演练，把请求打到故障后端观察熔断效果。 | `ha-drill` → `ha-fail` | 同 | `[ha-drill]` | `drill` p_top **0.925** / margin 0.853 | ✓ |

> dry-run 是 `dry_run=true`：不写粘滞、不计 inflight、不打开熔断，所以 2/3 恒选 `ha-fail`。
> 粘滞/分摊/熔断必须在实时 `/v1` 链路验证，剧本见 §7.4。

### 7.4 实时取证剧本（给 evidence / 复跑用）

前置：先 `POST /admin/trees/case-ha/enable`（会自动停用其它树），脚本结束后 `POST /admin/trees/default-jev/enable` 恢复。
**所有 `/v1` 请求必须显式带 `max_tokens`**（否则 qwen4b 长思考导致 17~83s 长尾，见 `infra.md` §5）。

#### 场景 A：均衡分摊 + 同 session 粘滞

```bash
# A1 并发分摊：6 个并发请求（无 session），观察 x-sr-model 分布
for i in 1 2 3 4 5 6; do
  curl -s -o /dev/null -D - -X POST http://192.168.95.76:8400/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"auto","max_tokens":16,"messages":[{"role":"user","content":"请把一批请求均匀分给两个后端，演示并发流量分摊和会话粘滞。"}]}' \
    | grep -i '^x-sr-model' &
done; wait

# A2 粘滞：同一 session 连续 3 次，第 2/3 次应 x-sr-selection: sticky 且 x-sr-model 相同
for i in 1 2 3; do
  curl -s -o /dev/null -D - -X POST http://192.168.95.76:8400/v1/chat/completions \
    -H 'Content-Type: application/json' -H 'x-sr-session-id: demo-session-001' \
    -d '{"model":"auto","max_tokens":16,"messages":[{"role":"user","content":"请把一批请求均匀分给两个后端，演示并发流量分摊和会话粘滞。"}]}' \
    | grep -iE '^x-sr-(model|selection)'
done
```

预期：A1 的 6 个 `x-sr-model` 在 `ha-slow-a` / `ha-slow-b` 间分摊（在途计数最小者优先，
并发窗口内出现交替；单发请求会一直取声明顺序第一个）；A2 第 1 次 `x-sr-selection: target`，
第 2/3 次 `x-sr-selection: sticky` 且 model 与第 1 次一致。

#### 场景 B：复杂推理 → 熔断转移（3×502 → 第 4 次 200）

```bash
for i in 1 2 3 4; do
  curl -s -o /tmp/ha-$i.json -D /tmp/ha-$i.h -w "req$i: %{http_code}\n" \
    -X POST http://192.168.95.76:8400/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"auto","max_tokens":16,"messages":[{"role":"user","content":"这次请求很复杂，需要多步推理和长链路分析。"}]}'
  grep -i '^x-sr-model' /tmp/ha-$i.h
done
```

| 请求 | 期望 HTTP | `x-sr-model` | 说明 |
|---|---|---|---|
| 1 | 502 | `ha-fail` | tap `fail-upstream` 立即 503 → router 转 502 `upstream_error` |
| 2 | 502 | `ha-fail` | 连续失败计数 2 |
| 3 | 502 | `ha-fail` | 连续失败计数 3 → **熔断打开** |
| 4 | 200 | `ha-general` | `ha-fail` 不在 available 列表，least_inflight 落到备用后端 |

#### 场景 C：故障演练 → 503 no_available_backend

```bash
curl -s -o /tmp/drill.json -w "%{http_code}\n" -X POST http://192.168.95.76:8400/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","max_tokens":16,"messages":[{"role":"user","content":"模拟一次上游故障演练，把请求打到故障后端观察熔断效果。"}]}'
cat /tmp/drill.json
```

- **顺序 B（一次连续跑，推荐）**：做完场景 B 后 `ha-fail` 已熔断，场景 C 的**首次请求即 503**
  `{"error":{"message":"no_available_backend"}}`（无 `x-sr-model` 头，因为未选出后端）。
- **顺序 A（独立验证「3 次 502 再 503」）**：把场景 C 放在最前面：请求 1-3 → 502（`x-sr-model: ha-fail`），
  请求 4+ → 503。此时场景 B 需要在 300s 半开窗口之后或清熔断状态后再跑。

#### 熔断状态说明（重要）

- 阈值：同一模型**连续 3 次** 5xx 打开；半开窗口 `half_open_after=300s`；
- 状态是**进程级 + DB 持久**（`/opt/smart-router-v3/router-v3.db` 的 `breaker` 表），
  重启服务不会立刻清除（若 DB 行仍在 300s 内仍判定为开）；
- 需要重置时（76 上没装 sqlite3 CLI，用服务自带 venv）：

```bash
ssh root@192.168.95.76 '/opt/smart-router-v3/.venv/bin/python - <<PY
import sqlite3
c = sqlite3.connect("/opt/smart-router-v3/router-v3.db")
with c: c.execute("DELETE FROM breaker")
print("breaker rows:", c.execute("select count(*) from breaker").fetchone()[0])
PY'
```

#### 观察点

- 响应头 `x-sr-model` / `x-sr-selection`（`target|sticky`）/ `x-sr-reasoning`；
- `GET /admin/requests/recent`：`model`、`target_id`、`selection_reason`、`sticky_hit`、`fallback_reason`；
- tap 日志 `/opt/sr-demo/tap.jsonl`：`model`、`status`、`duration_ms`、`max_tokens`，确认 `ha-fail` 的 503 与
  `ha-general` 的 200 上游记录。

## 八、复现、影响确认与升级

### 8.1 一键部署 + 校验

```bash
cd /tmp/smart-router-strict-canvas          # 或部署仓库根目录
python3 scripts/demo/apply_cases.py                     # 首次：目录 + 5 树 + 22 条 dry-run
python3 scripts/demo/apply_cases.py --only case-ha -v    # 单案例 + 打印每个 prompt 的说明
python3 scripts/demo/apply_cases.py --confirm-sync       # 重跑：显式确认目录影响并同步 5 棵画布
```

- 案例 JSON 是唯一事实源：脚本不维护任何硬编码 provider/模型/JEV/窗口/时段，而是读取五份
  `case-*.json` 合并出目录（`providers → models → JEV 服务与各自窗口 → 时段`），再按
  `manifest.json` 部署画布；脚本只用标准库（urllib）。
- **首次部署**（目录与画布都不存在）不会触发影响确认，目录写入 200/201，五棵树 `created`。
- **重跑**时目录项已被五棵树引用：provider 写第一次返回 `409 impact_confirmation_required`，
  脚本打印受影响画布清单（`id` / `name` / `enabled` / `document_sha256` / `usage`）并停止，
  退出码 1，**不写任何后续内容**。
- 只有显式 `--confirm-sync` 才用 409 响应里的基线哈希重试同一 method/path；重试仍 409 且
  `code == graph_conflict` 时打印真实的 `actual_document_sha256` 并停止（画布在预览后变化 →
  重新运行脚本以读取最新清单）。脚本绝不自动绕过确认。
- 幂等语义：provider `PUT`；模型已存在 `PUT`、否则 `POST`；JEV 服务已存在 `PUT`、否则 `POST`；
  时段整表 `PUT`（会打印旧数量 → 新数量）；树已存在 `PUT`（先从 `GET /admin/trees` 取当前
  `document_sha256` 作为 `base_document_sha256`）、否则 `POST`；**从不调用 enable/disable**。
- 每条 prompt 断言：HTTP 200、`outcome.target_id`、`outcome.model`、`outcome.reasoning`（案例三）、
  `candidates` 集合，以及 `warnings` 为空（启用级校验）。
- 最终状态检查以**部署前**从 `GET /admin/trees` 读到的 enabled 集合为基线：5 棵演示树必须全部
  未启用，该集合必须原样保持（影响确认同步只更新受影响画布的文档，不改 enabled 状态）。
  全新实例部署前没有 enabled 组，部署后仍为空即正确——脚本不会凭空启用任何演示树；启用某棵树
  是单独的演示动作（`POST /admin/trees/{id}/enable`）。
- 退出码：0 = 全部通过；1 = 任一目录/画布写入失败、影响确认停止、dry-run 期望不匹配或最终状态不符。
- 可用环境变量覆盖连接参数：`SR_BASE_URL` / `SR_ADMIN_EMAIL` / `SR_ADMIN_PASSWORD`。

### 8.2 实测结果

2026-11-03 首次部署（严格快照前基线，dry-run 命中结果；当时 76 实例部署前已有 `default-jev`
处于 enabled）：

```
provider demo-tap: base_url=http://127.0.0.1:9300/v1 format=openai
models: 15 个演示模型就绪（provider=demo-tap），推理声明与能力说明校验通过
tree case-industry: created / tree case-multimodal: created / tree case-reasoning: created
tree case-logic: created / tree case-ha: created
dry-run: 22/22 命中 ✓  最终状态：default-jev 仍是唯一 enabled 的决策组
```

2026-11-23 严格自包含快照复跑（同一套案例文件；下列为真实组合根 smoke 的输出摘要，非逐字粘贴）：

```
provider: demo-tap 就绪
models: 15 个演示模型就绪（provider=demo-tap），推理声明与能力说明校验通过
jev: demo-jev @ http://127.0.0.1:30104 window=2048/4096 就绪
time_periods: 2 个演示时段就绪（白天, 夜间）
tree case-industry: created …（5 棵树全部 created，未 enable）
首次部署 OK；最终状态检查：部署前无 enabled 组，部署后 enabled 仍为空 []
二次运行：在 provider demo-tap 处 409 impact_confirmation_required 停止，affected: 5
加 --confirm-sync 重跑：provider/model/JEV/时段目录 409 → 确认哈希后同步成功
tree case-industry: updated …（5 棵树 updated）→ dry-run 22/22 命中 ✓
```

- 上面的 `enabled: []` 是**全新实例**（部署前没有任何 enabled 决策组）的正确终态：脚本只部署
  5 棵案例画布且保持 `enabled=false`，启用是后续单独的演示动作；最终状态检查以部署前集合为
  基线，所以不会再误报缺少 `default-jev`（旧实现写死要求 `['default-jev']`，在全新实例上会
  假失败并以退出码 1 结束）。
- 在 76 这类既有实例上，部署前 `default-jev` 已 enabled：最终状态检查要求它部署后仍然
  enabled；若演示树被误启用（集合新增成员）则报错退出 1。
- 即：未确认时目录、五份画布字节与审计零改动；确认后只同步受影响的画布，画布文件仍是严格
  自包含文档（四运行字段齐全，`GET /admin/trees` 可见四字段与 `document_sha256`）。

### 8.3 注意

- 若刚跑过案例五的实时熔断注入（`ha-fail` 处于熔断期），重跑 `apply_cases.py` 时案例五的
  dry-run 会选到 `ha-general` 或直接 503 —— 这是熔断状态的正常副作用，不是配置错误；
  等 300s 半开窗口或按 §7.4 清 `breaker` 表后重跑即恢复。
- 实时取证会 enable/disable 各树。`apply_cases.py` 的最终状态检查以**本次运行部署前**的
  enabled 集合为基线：部署前 `default-jev` enabled → 部署后必须仍 enabled；部署前没有
  enabled 组（全新实例）→ 部署后为空即正确。若演示树在运行前仍处于 enabled（取证中断未
  恢复），`step_trees` 会明确报错停止——脚本从不主动 enable/disable 任何树，恢复
  `default-jev` 仍由取证脚本负责（task-3/4）。
- 首次部署后如需再次执行，必须按 §8.1 的两种结果处理：默认会在第一个受影响目录写处停止，
  这是有意的安全行为，不是失败；加 `--confirm-sync` 才是「确认同步」。
- `run_cases.py` 的「默认 409 停止」只保证**不写主证据 JSON**：平台快照阶段（阶段 1）早于
  后续管理写，若在更晚的 409 处停止，它可能已经创建 `evidence/` 目录并写入平台阶段的
  `evidence/trees-export.json`——这不代表取证跑完，也不代表主证据已生成。主证据
  （`case-*.json` / `probes.json` / `platform.json` / `summary.json`）只在完整跑完后写入；
  判断结果请以退出码（2 = 中止）和停止日志为准，不要把半成品目录当成完整证据。

### 8.4 严格的运行快照与升级注意

- 每份案例 JSON 都是完整 v3 文档：`kind: "sr-routing-graph"`、`version: 3`、四个必填运行字段
  `backends` / `jev` / `jev_window` / `time_periods`；**没有**外部 `id` / `enabled`（决策组 id
  由 `manifest.json` 的 `tree_file`/文件名关联）。
- 「可直接导入」指**控制台/文件导入**路径：导入接口解析完整 v3 文档，`kind` / `version` 信封
  字段是允许且必需的。`apply_cases.py` 走管理 API（`POST /admin/trees` / `PUT /admin/trees/{id}`），
  该 DTO 以 `extra="forbid"` 禁止信封字段，因此脚本在提交前显式剔除 `kind` / `version` / `id` /
  `enabled`（见上游 `test_upsert_tree_strips_envelope_and_carries_runtime`）。
- 通过管理 API 部署画布时，请求体携带 `name / root / sticky / timezone` + 四个运行字段；
  业务操作（`/admin/try`）同理：带 `tree_id` 时继承已存快照，带完整草稿时必须四字段齐全。
- 严格拒绝：缺任一个运行字段、只带部分字段、`runtime: null`、旧文档（无运行字段）都会以
  `graph_parse_error` 422 拒绝；不存在从数据库目录补齐运行配置的回退，也没有物化步骤。
  `jev: null` 仅用于没有问题节点的画布。
- **升级注意（重要）**：从旧版本升级时，已存画布若没有四运行字段，严格解析会失败。
  升级前必须在旧版本上对每个决策组保存一次（把当时的有效运行配置写入文件），或手工补齐
  每个 `ROUTER_GRAPH_DIR/graphs/*.json`；否则升级后启动会记录
  `initial graph snapshot is unavailable`，`/readyz` 未就绪，`GET /admin/trees` 503
  `graph_storage_error`，直到文件修好。SQLite schema v5→v6 会自动迁移（旧单行 JEV 配置变成
  `default` 服务，密钥保留），升级前请备份 `router.db` 与图目录。

### 8.5 关键 API 形状与验证命令

多服务 JEV 目录（旧单配置端点已删除）：

```bash
# 服务列表：窗口在服务条目内，响应不含真实密钥
curl -s -H "Authorization: Bearer $TOKEN" $BASE/admin/jev
# → {"services":[{"id":"demo-jev","base_url":"http://127.0.0.1:30104","model":"d1-2B",
#                 "key_env":"","timeout_seconds":10.0,"instructions":"…","state_template":"…",
#                 "input_tokens":2048,"context_tokens":4096,
#                 "key_present":false,"key_source":"none"}]}
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"other-jev","base_url":"http://127.0.0.1:30105","model":"d1-2B",
       "input_tokens":1024,"context_tokens":4096}' $BASE/admin/jev
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"固定测试文本"}' $BASE/admin/jev/demo-jev/test
```

画布与目录的两阶段写（模型/provider/JEV 更新或删除共用）：

```bash
# 1) 未确认：409 + 受影响画布清单（每幅含当前 document_sha256 与 usage）
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"upstream":"new-upstream"}' $BASE/admin/models/ha-general
# → {"detail":{"code":"impact_confirmation_required","resource":{...},
#              "affected_trees":[{"id":"case-logic","name":"…","enabled":false,
#                                 "document_sha256":"<sha>","usage":{...}}],
#              "replacements":[...]}}

# 2) 带确认哈希重试：只同步受影响画布，响应带 synced_trees
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"upstream":"new-upstream","impact_confirmation":{"trees":[
        {"id":"case-logic","document_sha256":"<上面返回的 sha>"}]}}' \
  $BASE/admin/models/ha-general
# → {"ok":true,"model":{...},"synced_trees":["case-logic"]}
```

- 删除被 target 引用的模型必须带 `replacement_model_id`；删除被引用的 JEV 服务必须带
  `replacement_jev_id`（确认后缺失 → 422，未知 → 404，自身 → 422）；仍被数据库模型或画布模型
  引用的 provider 不可删除（409 `provider_referenced`，带确认也不放行）。
- 画布 `PUT /admin/trees/{id}` 需要 `base_document_sha256`（乐观并发门，缺失/过期 → 409
  `graph_conflict` + 当前哈希）。
- 验证命令（部署后）：

```bash
curl -s -H "Authorization: Bearer $TOKEN" $BASE/admin/jev | python3 -m json.tool
curl -s -H "Authorization: Bearer $TOKEN" $BASE/admin/time-periods   # 白天 / 夜间（2 个）
curl -s -H "Authorization: Bearer $TOKEN" $BASE/admin/trees | python3 -m json.tool
#   每棵树应含 backends/jev/jev_window/time_periods 与 document_sha256
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tree_id":"case-industry","text":"最近体检查出甲状腺结节，需要怎么复查和用药？"}' \
  $BASE/admin/try | python3 -m json.tool
#   断言 outcome.target_id/model 命中期望，warnings 为空或仅 runtime_mismatch(hint)
grep -R "api_key\|authorization\|key_present\|key_source" $GRAPH_DIR/graphs/ ; # 不应有输出
```

## 九、已知边界与注意事项

1. **不启用**：交付的 5 棵树保持 `enabled=false`；试算/演示走 `/admin/try` 或由取证脚本临时 enable。
2. **上游依赖**：dry-run 不依赖 tap 存活；实时请求依赖 76 本机的 tap（`127.0.0.1:9300`）与
   其后端（详见 `infra.md`）。实时请求**必须带 `max_tokens`**，否则 qwen4b 长思考会有 17~83s 长尾。
3. **JEV 判定与窗口**：`demo-jev` 的 d1-2B 单模型打分，choice 的 p_top 实测集中在 0.91~0.998；
   score 为 criteria 离散档位。窗口 2048/4096 属于 `demo-jev` 服务本身（多服务目录中每个服务
   各自持有窗口），改窗口或换服务必须同时更新画布 `jev` + `jev_window`。若换 prompt 后判定不稳，
   优先调整选项 capability/instructions 文案，不要放宽期望。
4. **时段自包含**：`case-logic` 把白天/夜间两个时段内嵌在案例 JSON 里，不依赖数据库已有定义；
   `apply_cases.py` 部署后的目录只有这两个时段（`default-jev` 画布自带的「周末」不属于本案例）。
5. **粘滞/熔断只在实时链路可验**：`/admin/try` 是 `dry_run=true`，不写粘滞、不计 inflight、不打开熔断。
6. **画布布局**：5 棵树按分层手工布点，已用真实控制台渲染尺寸检查无节点重叠；
   用户仍可在画布上用「自动整理」重新布局（会改动 position，属正常编辑）。
7. **不改既有数据**：`default-jev`、既有 6 个模型位与 4 个 provider 未被修改；本任务只新增
   `demo-tap` + 15 个新模型 + `demo-jev` + 5 棵新树。目录改名/删除需要走影响确认（见 §8.5）。

## 附录 A：22 条 dry-run 全量证据（2026-11-03 14:18）

| # | 树 | 演示 prompt | now | 实际 target | 实际 model | JEV 证据 | 结论 |
|---|---|---|---|---|---|---|---|
| 1 | case-industry | 最近体检查出甲状腺结节，需要怎么复查和用药？ | — | `industry-medical-target` | `industry-medical` | medical p_top 0.978 / margin 0.966 | ✓ |
| 2 | case-industry | 公司要和我解除劳动合同，经济补偿金应该怎么算？ | — | `industry-legal-target` | `industry-legal` | legal p_top 0.908 / margin 0.850 | ✓ |
| 3 | case-industry | 现在手里有30万闲置资金，想配置一部分债券基金，有什么建议？ | — | `industry-finance-target` | `industry-finance` | finance p_top 0.974 / margin 0.954 | ✓ |
| 4 | case-industry | 10kV配电线路发生单相接地故障，应该怎么排查和隔离？ | — | `industry-energy-target` | `industry-energy` | energy p_top 0.969 / margin 0.943 | ✓ |
| 5 | case-industry | 帮我用Python写一个读取CSV并聚合统计的脚本。 | — | `industry-code-target` | `industry-code` | code p_top 0.922 / margin 0.859 | ✓ |
| 6 | case-industry | 周末适合带孩子去哪些地方玩？ | — | `industry-general-target` | `industry-general` | general p_top 0.988 / margin 0.985 | ✓ |
| 7 | case-multimodal | 我发了一张系统报错截图，请帮我看一下这个错误是什么原因？ | — | `image-target` | `vl-qwen35-4b` | image p_top 0.997 / margin 0.994 | ✓ |
| 8 | case-multimodal | 这张照片里的菜单是法文的，帮我翻译成中文。 | — | `image-target` | `vl-qwen35-4b` | image p_top 0.954 / margin 0.907 | ✓ |
| 9 | case-multimodal | 帮我写一段产品发布会的开场白，控制在200字以内。 | — | `text-target` | `text-general` | text p_top 0.998 / margin 0.996 | ✓ |
| 10 | case-multimodal | 请根据下面这段会议记录，整理出三条待办事项。 | — | `text-target` | `text-general` | text p_top 0.912 / margin 0.823 | ✓ |
| 11 | case-reasoning | 请结合公司近三年的财务报表、行业竞争格局和宏观政策，设计一套多维度的经营风险评估框架，并说明各指标权重如何确定。 | — | `deep-target` | `reason-deepseek`（max） | score 8 | ✓ |
| 12 | case-reasoning | 帮我写一封邮件，向客户说明项目延期一周，并给出新的交付计划。 | — | `standard-target` | `reason-openai`（medium） | score 6 | ✓ |
| 13 | case-reasoning | 今天星期几？ | — | `light-target` | `reason-qwen`（off） | score 3 → otherwise | ✓ |
| 14 | case-logic | 10kV线路出现接地告警，抢修班组已经到场，需要立即处置，设备严重过载。 | 默认(白天) | `t-emergency` | `industry-energy` | 严重度 10；yes 0.994；候选 `[emergency, oncall]` | ✓ |
| 15 | case-logic | 夜深了，想问问明天营业厅几点开门。 | 23:30 | `t-normal` | `ha-general` | 严重度 3；yes 0.014 | ✓ |
| 16 | case-logic | 白天收到一条设备告警短信，影响不大，想确认一下常规巡检流程。 | 10:30 | `t-oncall` | `ha-general` | 严重度 3；yes 0.011；候选 `[oncall, normal]` | ✓ |
| 17 | case-logic | 白天收到一条设备告警短信，影响不大，想确认一下常规巡检流程。 | 23:30 | `t-normal` | `ha-general` | 同上，时段不匹配 → OR 假 | ✓ |
| 18 | case-logic | 某小区计划停电检修，涉及300户居民，需要制定风险控制方案和通知计划。 | 23:30 | `t-review` | `ha-general` | 严重度 6；区间 AND；候选 `[review, normal]` | ✓ |
| 19 | case-logic | 夜里发现主变油温异常升高，冷却器全停，情况非常危急。 | 22:30 | `t-emergency` | `industry-energy` | 严重度 10 + 夜间 → AND 升级 | ✓ |
| 20 | case-ha | 请把一批请求均匀分给两个后端，演示并发流量分摊和会话粘滞。 | — | `ha-balanced` | `ha-slow-a` | balanced p_top 0.997；候选 `[balanced(5), backup(4)]` | ✓ |
| 21 | case-ha | 这次请求很复杂，需要多步推理和长链路分析。 | — | `ha-complex` | `ha-fail` | complex p_top 0.931 | ✓ |
| 22 | case-ha | 模拟一次上游故障演练，把请求打到故障后端观察熔断效果。 | — | `ha-drill` | `ha-fail` | drill p_top 0.925 | ✓ |
