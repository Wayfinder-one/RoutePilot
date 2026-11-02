# RoutePilot

> Sandlake 出品 · 版权归 Sandlake 所有

[English](README.md) | 简体中文

RoutePilot 是 OpenAI 兼容的模型路由代理。路由策略由一张通用决策图画布配置：问题、判断门、请求条件、模型目标都是节点，比较条件直接写在判断门的输入槽位上。

| | |
|---|---|
| 项目状态 | graph v3 运行时（M1 完成，2026-11-02）；画布、`/v1` 真链路、`/admin/try` 试算与请求日志均已按 graph v3 工作 |
| 设计文档 | `docs/superpowers/specs/2026-11-02-generic-decision-tree-design.md`（仅本地保留，不随仓库发布） |
| 控制台原型 | `docs/od-design/routing-canvas-2026-11-02/index.html`（方向 A · 画布优先；仅本地保留，不随仓库发布） |
| 配置格式 | `sr-routing-graph` v3；旧版路由树不自动转换，见[从旧数据库升级](#从旧数据库升级) |

## 决策画布

![RoutePilot 决策画布](assets/routepilot/canvas.png)

### 节点

| 类型 | 作用 | 输入 | 输出 |
|---|---|---|---|
| `start` | 请求入口，恒为 true | — | boolean |
| `question` | 调用决策服务的一步提问；`choice` / `noul` / `score` | 布尔槽位 `in_1..in_n`（OR） | choice：`prob.*` / `selected.*` / `p_top` / `margin`；noul：`yes` / `no`；score：`value` |
| `logic` | 判断门，`and` / `or` / `not` | 布尔槽位 `in_1..in_n`，每槽位一条入边 + 条件 | boolean（三值） |
| `condition` | 请求条件：时间段 / 文本关键词 | — | boolean |
| `target` | 模型目标（模型、推理档位、priority、least_inflight） | 布尔槽位 `in_1..in_n`（OR） | — |

### 输入条件

所有输入端口都是布尔输入槽位。条件随边保存：

```json
{
  "id": "e-cap-strong-and-power",
  "source": "capability",
  "source_port": "prob.strong",
  "target": "and-power",
  "target_port": "in_1",
  "condition": { "op": "gt", "value": 0.65 }
}
```

| 上游输出 | 条件 | 求值 |
|---|---|---|
| number | 必填：`gt` / `gte` / `lt` / `lte` / `eq` / `neq` + 有限阈值 | 上游值与阈值比较；unknown 传播为 unknown |
| boolean | 可选：`null`（等价 `= true`）或 `{ "op": "is", "value": true\|false }` | 布尔期望值 |

- 一个输出可连多个槽位，每个槽位各自写条件；同一 AND 门的两个槽位写 `> 0.3` / `< 0.8` 即区间判断。
- 每个输入槽位最多一条入边；槽位号 `in_1..in_N` 不重排，删除连线后空槽位可复用。
- NOT 恰好 1 个槽位；AND / OR 至少 2 个；question / target 的多个槽位为 OR。

### 执行

1. 读取不可变 RoutingSnapshot，计算请求条件，截断一次请求文本。
2. 开始节点激活第一批问题；同一波问题合并为一次 `/v1/systemone` 调用。
3. 波次返回后更新数值端口与 `selected`，求值所有下游槽位条件与判断门。
4. true 信号激活后续问题与 target；重复直到没有新问题。
5. 多个 target 同时激活时取 priority 最小者；无候选用 `otherwise_target`，决策失败用 `error_fallback`。
6. 到达 target 后应用会话粘性；target 内按 `least_inflight` 选择后端。

unknown 不触发任何问题或 target；比较条件遇 unknown 结果为 unknown。启用限制：节点 ≤ 200、边 ≤ 400、问题 ≤ 32、最长问题链 ≤ 8 波。

### 试算回放

![试算回放](assets/routepilot/replay.png)

## 条件编辑

![条件编辑](assets/routepilot/condition.png)

- 数值线拖入任意布尔槽位：自动生成默认条件 `> 0.5` 并打开条件编辑。
- 点槽位条件 chip：修改算子与阈值（数值线）或 `= true / = false`（布尔线）。
- 条件随边保存；修改条件计入行为 revision，移动节点不计入。
- 选中边或槽位，右侧检查器同步显示来源、目标、线型与条件。

## 控制台原型

方向 A「画布优先」已确认。交互包括：

- 节点库拖入 / 点击创建；画布平移缩放；节点拖动；端口连线与类型校验；
- 快速创建：输出端口拖到空白处按来源类型创建兼容节点；
- 判断门 / 问题 / 目标的输入槽位行：来源、条件、试算实际值；
- 试算 5 个样例（电网抢修 / 翻译 / 工单 / 低置信度追问 / 服务超时），按波次回放；
- 校验错误定位、删除确认、撤销重做、自动整理、导入导出 v3。

原型入口（本地，file:// 直接打开）：

- 画廊：`docs/od-design/routing-canvas-2026-11-02/index.html`
- 方向 A：`docs/od-design/routing-canvas-2026-11-02/drafts/index-a.html`
- 方向 B / C：同目录 `drafts/index-b.html`、`drafts/index-c.html`

校验错误态：

![校验错误](assets/routepilot/errors.png)

## 运行时职责

| 组件 | 职责 |
|---|---|
| Graph Executor | 按波次执行图：问题、条件、判断门、target 激活；维护三值状态与 trace |
| DecisionService 客户端 | 调用 NeoHorse `/v1/systemone`，返回概率/数值并做严格校验 |
| SessionAffinity | 到达 target 后按 tree revision + target 复用模型；可 TTL/LRU 配置 |
| LoadBalancer | target 内按 `least_inflight` 选择后端，计数覆盖完整上游生命周期 |
| CircuitBreaker | 剔除熔断后端；全部不可用返回 503 `no_available_backend` |

dry-run 使用同一执行器，不写粘性、不增加在途、不调用上游生成模型。当前运行模式为单 uvicorn worker，粘性与在途计数为进程内状态。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

ROUTER_DB=/tmp/routepilot.db .venv/bin/python -m router
```

默认监听 `127.0.0.1:8400`，控制台 `http://127.0.0.1:8400/console/`。生产环境首次启动向空 users 表写入默认管理员账号；上线后立即修改密码。本地开发可设 `ROUTER_USER_AUTH=0` 关闭 `/admin` 鉴权。

## 从旧数据库升级

graph 运行时把完整决策图持久化在 `routing_trees.root`。如果库里的路由树仍是旧版载荷（例如 `root.type = "jev"`），新版**不会自动转换**：启动会拒绝并停留在 v1，同时报出：

```
legacy routing trees require explicit reset; ... DELETE FROM routing_trees ...: <tree ids>
```

1. 停止旧服务。
2. 备份数据库：

   ```bash
   sqlite3 router.db ".backup 'router.db-pre-graph-v3'"
   ```

3. 启动一次新版：新版会在修改 schema 之前退出，并列出旧树 id。
4. 仅在管理员确认旧路由配置已不再需要后，删除旧树：

   ```bash
   sqlite3 router.db "DELETE FROM routing_trees"
   ```

5. 再次启动新版：迁移新增 `request_logs.waves`、写入 schema 版本 2；已有请求日志保留，`waves` 为 NULL。

该重置只删除旧的路由配置；模型、提供方、用户、会话、决策服务配置和请求日志都会保留。旧树不会被自动转换，仍需要的策略请在画布上重建后重新启用。

## 决策服务

决策服务提供 `POST /v1/systemone`。配置方式：控制台「JEV 模型」页，或首次启动时通过环境变量播种。

```bash
ROUTER_DB=/tmp/routepilot.db \
ROUTER_JEV_URL=http://127.0.0.1:30104 \
ROUTER_JEV_MODEL=d1-2B \
ROUTER_JEV_API_KEY= \
.venv/bin/python -m router
```

- `ROUTER_JEV_URL` 与 `ROUTER_JEV_MODEL` 必须同时提供；数据库已有配置时不被环境变量覆盖。
- 输入窗口：服务上下文预算（`-c / --parallel`）与输入截断上限可配置；超限按首尾保留截断，不返回 4xx/5xx。
- 连接测试会真实调用决策服务，但不改变分流状态。

## 接口

| 路径 | 用途 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容路由入口（非流式 / SSE） |
| `GET/POST/PUT/DELETE /admin/trees` | 决策组 CRUD、启用、复制 |
| `POST /admin/try` | dry-run 试算（同一执行器） |
| `GET/POST /admin/models` | 模型目录与提供方 |
| `GET/PUT /admin/jev`、`/admin/jev/window` | 决策服务配置与上下文窗口 |
| `GET /admin/time-periods` | 命名时间段（请求条件节点引用） |

错误语义：

- 无启用树：503 `no_active_tree`；
- target 后端全不可用：503 `no_available_backend`；
- 决策失败 / 超时 / 非法响应：走 `error_fallback`；
- 上游 4xx：状态码与错误体透传；上游 5xx / 网络错误：502 并计熔断失败；
- 流式终态：`completed / upstream_error / client_disconnected / cancelled / incomplete`。

## 环境变量

| 变量 | 默认 | 用途 |
|---|---|---|
| `ROUTER_DB` | `router.db` | SQLite 路径 |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8400` | 监听地址 |
| `ROUTER_JEV_URL` / `ROUTER_JEV_MODEL` | 空 | 首次启动播种决策服务 |
| `ROUTER_JEV_API_KEY` | 空 | 可选决策服务密钥 |
| `ROUTER_JEV_TIMEOUT` | `10` | 决策服务请求超时（秒） |
| `ROUTER_SESSION_MAX_ENTRIES` | `10000` | 粘性 LRU 上限 |
| `ROUTER_USER_AUTH` | 生产 `1` | 控制台账号鉴权 |
| `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` | 首次启动 seed | 管理员账号 |
| `ROUTER_SESSION_TTL_HOURS` | `168` | 控制台登录会话 TTL |

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

原型 QA（本地）：

```bash
cd docs/od-design/routing-canvas-2026-11-02 && node qa.js
```

## 目录

```
router/                  服务端（FastAPI、路由执行、管理接口）
router/console/          控制台静态页
configs/                 示例决策组
scripts/                 本地运维脚本（按约定不入库）
tests/                   测试
docs/                    设计文档、原型（按约定不入库）
assets/routepilot/       README 截图
```

---

© 2026 Sandlake. All rights reserved. 版权归 Sandlake 所有。
