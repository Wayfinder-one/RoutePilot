# 计算日志详情：数值与判断过程展示（设计）

- 日期：2026-11-20
- 状态：已评审
- 关联：`docs/design/calc-log-v2/`（日志页信息架构）、`docs/superpowers/plans/2026-11-20-log-graph-snapshot.md`

## 背景

日志详情当前只显示“阶段 / 摘要 / 依据”三类摘要，看不出计算过程。以线上一条真实请求为例：

- 「初始计算 · 13 事件」只显示最后一条事件的摘要“节点 question-noul-1 已激活”；
- 「JEV 第 1 轮 · 15 事件」只显示“节点 safe-target 已激活”；
- JEV 返回的概率、槽位比较（`0.84 > 0.65`）、门运算结果、候选优先级全部不可见。

根因有两层：

1. `router/console/logs.js` 的 `stages()` 把同一 `phase` 的所有事件压成一行，`cell/summary/facts`
   只取最后一条非空值，其余事件被丢弃。
2. `router/api/trace_presenter.py` 只输出节点名/连接名/状态等 2–3 条 `facts`；机器事件里已有的
   `raw`、`condition`、`ports`、`inputs`、`candidates` 等数值没有进入展示字段。

图回放（`/console/routing.html?request=<id>`）已经能直观看到命中链路；本设计只解决日志页
“用文字一眼看清数值如何参与计算、最终优先级如何决出”的问题。

## 目标

打开一条日志后，无需再次点击，直接看到：

- 每个 JEV 问题返回的值：choice 的概率、选中项、`p_top`、`margin`；noul 的 yes/no；score 的 value；
- 每个值参与的判断：`capability.selected.strong 0.84 > 0.65 → true`、布尔直连、invalid/unknown；
- 每个逻辑门的运算：`and-power：true AND true → true`，以及逐输入的依据；
- 激活结果：哪个节点、通过哪个入口；
- 最终优先级判断：候选目标、优先级、来源、轮次，以及胜者（优先级降序、同优先级按 `target_id` 升序）；
- 模型调度的模型、原因、策略与可用模型。

数值格式约定：

| 类型 | 格式 | 示例 |
|---|---|---|
| 概率 / `p_top` / `margin` | 整数百分比，四舍五入（≥ .5 进位） | `84%`、`72%` |
| 槽位原值与阈值 | 四舍五入到 3 位小数，去掉尾随 0 | `0.84 > 0.65` |
| 布尔 | 小写 `true` / `false` | `true` |
| 缺失值 | `—` | `—` |
| 耗时 | 四舍五入到整数 ms；≥1000ms 用秒（2 位小数） | `9 ms`、`1.88 s` |
| Token 窗口 | `前 → 后` | `18 → 18` |

## 非目标

- 不新增 API、不改数据库、不改快照存储；
- 不做通用 `detail` 契约、不做阶段折叠开关（默认全展开）；
- 不改图回放、不改试算页、不复活 `router/console/trace.js`（legacy 渲染器保持原样）；
- 不为重启后无 `trace` 的日志做懒加载（保持现有空状态）；
- 不承诺展示机器字段的原始 JSON。

## 设计

### 1. 后端展示契约（`router/api/trace_presenter.py`）

每个展示事件在现有 `phase/label/cell/summary/facts/tone` 之外新增：

```python
"kind_label": {"zh": "槽位", "en": "Slot"}
```

`kind_label` 只描述事件类型，供日志页事件行的类型小标签使用；`label` 仍是阶段名。所有事件类型都有值，
未知类型回退 `{"zh": "事件", "en": "Event"}`。`request_presenter.present_result_event()` 返回的
`request_outcome` 同样补 `kind_label={"zh": "结果", "en": "Outcome"}`。

`present_trace_event()` 继续不修改机器字段，`present_trace()` 的幂等行为不变。

#### 各事件类型的 summary / facts

| 事件 | kind_label (zh/en) | summary (zh) | facts |
|---|---|---|---|
| `time` | 时段 / Time | `Asia/Shanghai，生效 peak、normal`；无生效时段 `Asia/Shanghai，未命中` | `[时区, TZ]`、`[生效时段, "peak, normal" 或 —]` |
| `window` | 窗口 / Window | `输入窗口：18 → 18 tok`；截断时加 `（已截断）` | `[Token, "18 → 18"]`、`[截断, 是/否]` |
| `condition` | 条件 / Condition | `cond-text：命中，匹配 10kV、跳闸`；未命中不带“匹配” | `[节点, cond-text]`、`[匹配, "10kV, 跳闸" 或 —]`、`[结果, true/false]` |
| `wave` | 轮次 / Wave | `第 1 轮 JEV：2 个问题，耗时 9 ms`；失败 `第 1 轮 JEV 失败：<error>` | `[轮次, 1]`、`[问题数, 2]`、`[耗时, 9 ms]` |
| `question`（choice） | 问题 / Question | `capability：strong（p_top 84%，margin 72%）` | `[概率, "strong 84% · easy 16%"]`、`[耗时, 9 ms]` |
| `question`（noul） | 问题 / Question | `domain：P(yes) 92%，P(no) 8%` | `[概率, "yes 92% · no 8%"]`、`[耗时, …]` |
| `question`（score） | 问题 / Question | `q-score：value 0.7` | `[值, 0.7]`、`[耗时, …]` |
| `gate` | 门 / Gate | `and-power：true AND true → true`；一元 NOT 门 `not-x：NOT true → false`（运算符大写） | 每个输入一条：`[in_1, "0.84 > 0.65 → true"]`；无输入时 `[运算, AND]` |
| `slot` | 槽位 / Slot | `capability.selected.strong 0.84 > 0.65 → true` | `[目标, power-target.in_1]` |
| `activate` | 激活 / Activation | `power-target（via in_1）` | `[节点, power-target]`、`[入口, in_1]` |
| `fallback` | 兜底 / Fallback | `JEV 失败，启用错误兜底 safe-target（timeout）` | `[目标, …]`、`[原因, …]` |
| `final` | 候选 / Candidates | 有候选：`power-target p5、strong-target p3 → power-target 胜出`；无候选：`无候选，使用兜底目标 safe-target` | 每个候选一条：`[power-target, "p5 · via and-power.in_1 · 第 1 轮 ✓"]`；无候选时 `[目标, …]`、`[优先级, …]` |
| `schedule` | 调度 / Schedule | `目标 power-target：2 个可用模型，策略 least_inflight` | `[可用模型, "qwen3-8b, qwen3-32b"]`、`[配置模型, …]`、`[在途, "qwen3-8b: 1"]`（仅非空）、`[会话粘性, "miss → qwen3-8b"]`（仅检查过时） |
| `sticky_hit` | 粘性 / Affinity | `命中会话粘性：复用 qwen3-8b` | `[模型, qwen3-8b]` |
| `scheduled` | 调度 / Schedule | `最终调度 qwen3-8b（target）` | `[模型, …]`、`[原因, …]` |
| `routing_error` | 错误 / Error | `路由失败（schedule）：no_available_backend` | `[阶段, schedule]`、`[原因, no_available_backend]`、`[消息, …]` |

补充规则：

- 事件行已有 `kind_label` 徽标，`summary` 不再重复类型词（与 §2 线框图一致）。

- 选择类问题的 `p_top/margin` 优先取事件顶层字段，缺失时回退 `ports.p_top` / `ports.margin`；
  `selected` 缺失时从 `ports` 中为 true 的 `selected.<key>` 推导；`ports` 整体缺失时对应值显示 `—`。
- 逻辑门运算符统一大写（`and` → `AND`、`or` → `OR`、`not` → `NOT`）。
- 门的每个输入格式为 `<port> <raw><条件> → <status>`：数值线 `<raw> <符号> <阈值>`；
  布尔线无条件显示 `<raw> → <status>`，有 `is false` 显示 `<raw> = false → <status>`。
- 槽位条件符号映射：`gt >`、`gte ≥`、`lt <`、`lte ≤`、`eq =`、`neq ≠`、`is =`；
  数值线没有条件时摘要显示 `<raw> （未配置阈值） → invalid`（英文 `<raw> (no threshold) → invalid`）。
- `status` 原样使用 `true/false/invalid/unknown`，`invalid` 不折叠成 false。
- 最终候选顺序沿用引擎的 `(-priority, target_id)` 排序；摘要把排序后的第一个标为胜者，
  同优先级时由 `target_id` 升序决定，不在摘要里额外解释。
- 文案中的节点/连接 ID 原样使用，不做标题替换（日志页没有图快照的节点标题）。

新增纯函数辅助：`_number(value)`、`_percent(value)`、`_ms(value)`、`_condition(value)`、
`_slot_status(status)`。全部无副作用，便于单测。

### 2. 前端渲染（`router/console/logs.js`、`shared.css`、`i18n.js`）

`stages()`：

- 仍按 `phase` 分组，保留现有 `cell/summary/facts/tone` 供折叠行链路使用（取最后一条，不变）；
- 新增 `events: []`，按 trace 顺序收集该阶段所有已展示事件（含 `kind_label/summary/facts/tone`）；
- `count` 仍为该阶段事件数。

`stageHtml(stage, index)` 改为“阶段头 + 逐事件行”，全部直接渲染，没有开合按钮：

```
[pin] ② JEV 第 1 轮 · 15 事件                              15
      问题   capability：strong（p_top 84%，margin 72%）    概率 strong 84% · easy 16% · 耗时 9 ms
      问题   domain：P(yes) 92%，P(no) 8%                   概率 yes 92% · no 8% · 耗时 7 ms
      门     and-power：true AND true → true                in_1 0.84 > 0.65 → true · in_2 0.92 > 0.70 → true
      槽位   capability.selected.strong 0.84 > 0.65 → true  目标 power-target.in_1
      激活   power-target（via in_1）                       节点 power-target · 入口 in_1
```

- 阶段头仍用 `.lg-stage-name` 与 `.lg-stage-name small`（保留现有 QA 的
  `phase-order` / `.lg-stage` 计数断言），`small` 文案仍为 `<phase> · N 事件`；
- 事件行复用现有 5 列网格：第 1 列只画竖向轨道线，第 2 列留空（阶段列），第 3 列以
  `kind_label` 小标签开头接 `summary`，第 4 列 `facts`（沿用 `pairsHtml`），第 5 列留空；
- 事件行 tone：`warn/bad` 用对应颜色；`slot` 的 `false`、`unknown` 行降低对比度；
  `true` / 激活 / 命中正常显示；
- 表达式与依据允许换行（`overflow-wrap:anywhere`），不允许横向滚动；
- 表头第二列文案由 `logs.summary`（摘要）改为新增 `logs.computation`（计算过程 / Computation），
  其余表头不变；
- `detailHtml` 的结构、快照链接、原始字段 `<details class="lg-raw">` 均保持不变；
- 无 `trace` 的记录仍显示 `logs.traceEmpty` 空状态。

CSS：

- `.lg-stage` 由单行 grid 改为块级容器；阶段头 `.lg-stage-h` 保持 5 列 grid；
- 竖向轨道线从逐行 `.lg-rail-cell::before` 改为 `.lg-stage` 上的连续线，pin 仍只在阶段头；
- 新增 `.lg-event`（5 列 grid）、`.lg-event-kind`（行内 mono 小标签/弱色）、
  `.lg-event.is-muted`（false/unknown 行降低对比度）、`.lg-event-summary`（允许换行）。

### 3. 测试

后端 `tests/test_trace_presenter.py`：

- 每个事件类型至少一条断言：`kind_label` 双语存在；`summary` 含数值表达式；
  `facts` 含预期键值（概率、阈值、候选优先级等）；
- 幂等性与“不丢机器字段”的既有断言保持不变。

前端 `scripts/console_qa/logs.mjs`：

- 现有阶段顺序、7 个 `.lg-stage`、旧记录空状态、移动端无横滚用例保持不变；
- 新增/扩展真实请求用例：展开首行后**不点击任何阶段**即可看到
  `0.84 > 0.65 → true`、`p_top`、`84%`、`AND`、`power-target p5`、`strong-target p3`
  与胜者 `power-target`；
- 断言事件行数量大于阶段数，且 `false` 槽位也在文本中。

命令：

```bash
.venv/bin/python -m pytest tests/test_trace_presenter.py -q
npm run check:console
.venv/bin/python -m pytest -q
```

### 4. 验收标准

- 打开一条日志即可看到每个阶段的逐事件计算过程，无需二次展开；
- 任一 JEV 轮次都完整呈现“JEV 返回值 → 槽位比较 → 门运算 → 激活”链路；
- 目标判定行能看到全部候选的优先级、来源、轮次与胜者；
- 概率用百分比、阈值比较用原值，数值与结果能逐行对上；
- 旧记录（无 trace）行为不变；图回放与快照功能不受影响；
- `pytest` 与 `npm run check:console` 全绿。

## 风险与取舍

- **详情变长**：一条请求展开后约 30–50 行。这是“一眼看全”的代价；日志列表仍可滚动，
  移动端只要求不横向溢出。若后续嫌长，再引入阶段折叠，不属于本次范围。
- **展示文案在后端**：延续现有契约（事件自描述、前端纯投影），代价是文案改动需要后端发布；
  但数值格式化、i18n 与事件语义集中在一处，避免前端重新认识 `raw/inputs/ports`。
- **概率百分比 vs 原值**：概率用百分比便于阅读，阈值比较用原值保证与图回放/配置一致；
  同一行内不混用两种尺度。
- **无快照标题**：日志页不加载图快照，只显示节点/连接 ID，与现有依据列一致。
