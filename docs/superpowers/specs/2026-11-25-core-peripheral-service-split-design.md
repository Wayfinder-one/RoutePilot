# RoutePilot 内核与外围服务拆分设计

日期：2026-11-25
状态：已实施（分支 feat/core-peripheral-split，2026-11-26）；历史实施细节见计划与 .superpowers/sdd/core-split-2026-11-25/

## 1. 背景与目标

RoutePilot 当前在同一进程中混合承担决策图管理、图执行、JEV 调用、模型上游代理、SSE 转发、控制台、人员管理、SQLite 和请求日志。现有 APISIX 集成又把 RoutePilot 当作模型上游，导致 RoutePilot 回调 APISIX，并引入 `ROUTER_GATEWAY_TOKEN` 和 `ROUTE_PILOT_UPSTREAM_API_KEY`。

本次拆分形成两个可独立部署的服务：

- **内核服务**：管理当前决策图，执行决策并输出决策事件，不调用模型上游。
- **外围服务**：提供控制台、人员管理、SQLite、Provider/JEV 配置、standalone 模型代理和最近 20 条内存日志。

支持两种形态：

1. 内核 + 外围组成完整独立智能路由服务。
2. 只部署内核，作为 APISIX + newapi 的决策组件。

## 2. 已确认的原则

1. 同一仓库、共享图引擎和协议代码、两个应用入口、两个容器镜像。
2. 图 JSON 文件由内核独占管理，是唯一图事实源；外围不保存图文档副本。
3. 内核没有 SQLite、前端、人员体系、Provider Key、模型上游代理或日志存储。
4. 内核只产生决策日志，不提供执行结果回写接口，也不提供日志查询接口。
5. 每条决策事件都内嵌本次实际使用的完整当前图和决策值，不再存在“决策图快照”概念。
6. 外围接收决策事件，只在内存保留最近 20 条；独立模式不持久化决策日志。
7. 回放沿用当前页面，图和 trace 均直接来自同一条日志事件。
8. JEV Key 配置在内核环境变量中；外围不提供 JEV 代理。
9. APISIX 调内核得到真实模型 ID，改写原请求体 `model` 后继续原 newapi 链路；内核不转发模型请求。
10. 决策日志彻底离开 SQLite，长期留存以后由 VictoriaLogs 输出适配器承担。

## 3. 服务职责

### 3.1 内核服务 `routepilot-core`

- 独立端口和容器，默认监听 `0.0.0.0:8400`。
- 新建、更新、复制、启停决策图，设置默认图和部门。
- 原子写入图 JSON，校验、编译并发布当前启用图集合。
- 根据请求文本、可信部门、会话和 JEV 结果选择模型。
- 从内核环境变量读取 JEV Key，直接调用图中配置的 JEV 服务。
- 返回逻辑模型、真实模型 ID、Provider 非密钥信息和决策元数据。
- 每次实际路由决策生成一条完整事件，异步写入配置的外部日志接口。
- 提供健康与就绪接口；日志输出故障不使决策就绪状态失败。

明确不包含：SQLite、控制台静态资源、登录和人员管理、Provider Key 解析、模型上游调用、SSE 代理、最近日志存储和日志查询。

### 3.2 外围服务

- 独立端口和容器，可不部署。
- 提供现有控制台、人员管理、登录鉴权和管理 API。
- SQLite 只保存外围配置：人员、会话、Provider、模型目录、JEV 非密钥目录、时段目录和配置变更记录。
- 图管理请求完成人员鉴权后代理到内核；所有图写操作最终由内核执行。
- 提供 standalone `/v1/chat/completions`：调用内核决策、改写 `model`、解析 Provider Key、调用模型上游并透传 JSON/SSE。
- 提供决策日志接收接口，以 `deque(maxlen=20)` 保存完整事件。
- 提供最近日志和单条回放 API；不写 SQLite，重启即清空。

明确不包含：图文档事实源、独立决策引擎、JEV Key、长期决策日志或图快照数据库。

## 4. 两种调用链

### 4.1 完整独立服务

```text
客户端
  → 外围 /v1/chat/completions
  → 内核 /internal/v1/route-decision
  → 外围按决策改写 model
  → 外围从 SQLite/自身环境解析 Provider Key
  → 模型上游
  → 外围透传 JSON / SSE

内核决策事件 → 外围 /internal/v1/decision-logs → 内存最近 20 条
```

### 4.2 内核融入 APISIX + newapi

```text
客户端
  → APISIX Auto Route（model=auto）
  → forward-auth 使用原 Authorization 调用 newapi 鉴权并注入可信部门
  → APISIX 调内核 /internal/v1/route-decision（不发送 Authorization）
  → APISIX 只改写原请求体 model 并更新 Content-Length
  → 原 newapi upstream、插件和 Authorization
  → newapi 渠道选择、计费、重试和实际模型调用
  → APISIX 直接透传 JSON / SSE
```

固定模型请求继续绕过内核。决策失败时返回 503，不允许把 `auto` 发送给 newapi。

## 5. 全局 `config.json`

内核默认读取 `config.json`，可用 `ROUTER_CONFIG` 指定路径。配置只含非密钥信息：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8400
  },
  "graphs": {
    "directory": "/data/graphs"
  },
  "jev": {
    "api_key_env": "ROUTER_JEV_API_KEY"
  },
  "logging": {
    "type": "routepilot",
    "url": "http://routepilot-console:8401/internal/v1/decision-logs",
    "timeout_ms": 1000
  }
}
```

- `graphs.directory` 是内核唯一图持久化目录。
- `jev.api_key_env` 是环境变量名称，不是密钥值。
- 本期实现 `logging.type=routepilot`；`{"logging": {"type": "disabled"}}` 表示显式关闭日志输出。
- 后续新增 `logging.type=victorialogs`，复用同一事件对象并序列化为 JSON Lines；本期不声称 VictoriaLogs 适配器已经完成。
- 日志关闭时，内核继续决策，不保存事件副本。

日志输出采用 best-effort：异步发送、短超时、有界内存排队、失败不影响决策响应、不做磁盘重试。进程错误和丢弃计数只用于运行观测，不构成内核日志系统。

## 6. 内核 API

### 6.1 决策请求

```http
POST /internal/v1/route-decision
Content-Type: application/json
X-SR-Department: <可信部门，可为空>
X-SR-Session-Id: <可选>
X-Request-ID: <可选>
```

请求体沿用 Chat Completions JSON。不得向内核转发用户 `Authorization`。

成功响应：

```json
{
  "decision_id": "01JZK4M7F3N8Q2W5V6D9E4R0TA",
  "model": "qwen3-32b-real",
  "logical_model": "strong",
  "provider": {
    "name": "gateway",
    "base_url": "http://apisix-gateway.sandlake.svc.cluster.local/apiuser/v1",
    "reasoning_format": "openai"
  },
  "reasoning": null,
  "reasoning_warning": null,
  "tree_id": "finance",
  "tree_revision": "revision-7",
  "target_id": "target",
  "selection_reason": "target",
  "graph_selection": "department"
}
```

- `model` 是 newapi 或 standalone Provider 接受的真实模型名。
- `logical_model` 是图中逻辑模型 ID。
- APISIX 只读取决策和展示所需字段；外围使用 Provider 信息解析执行凭据。
- `reasoning` 保留当前推理档位决策语义。
- `tree_revision` 是图编辑修订号，不表示持久化快照。

错误语义：

- 非法 JSON、消息、部门或会话参数：400/422。
- 无当前启用图、无默认图或没有可用模型：503。
- JEV 故障：按图内 `error_fallback` 继续；只有无法形成合法决策时才返回 503。
- 任一返回路径都必须释放调度租约。
- 内核在任何成功或失败路径都不调用模型上游。
- 外围执行目录缺少所选逻辑模型，或目录 Provider/base_url 与内核决策不一致时，返回 503 `execution_catalog_mismatch`，不得调用上游或用其他 Provider 凭据代替。

### 6.2 图管理与试算

- 内核承接现有图 DTO、乐观并发校验和生命周期语义，提供完整图管理 API。
- 外围保持 `/admin/trees` 和 `/admin/try` 前端契约并代理到内核。
- Provider、模型和 JEV 目录变更需要同步图时，外围通过内核图 API 完成，不直接写图目录。
- 跨服务目录写不再声称拥有本地单事务：外围在内核用一次条件批次原子更新全部候选图，随后提交 SQLite；SQLite 提交失败时按原图和新基线哈希调用反向批次补偿。任一补偿失败都返回 503 `catalog_sync_diverged` 并记录严重错误，绝不能返回成功或静默留下部分更新。
- 内核直接管理的图必须在缺少外围 SQLite 的情况下通过自包含运行配置校验。
- 管理面试算继续不写请求决策日志；真实 standalone 测试和 APISIX 决策正常写事件。
- 外围停止不影响最后一次成功发布的当前图集合。

## 7. 决策事件与回放

### 7.1 单条事件结构

每次实际路由决策生成一个自包含 JSON 对象：

| 字段 | 内容 |
|---|---|
| `schema_version` | 事件结构版本，本期为 `1` |
| `event_type` | 固定为 `routepilot.decision` |
| `id` / `ts` | 决策 ID / ISO 8601 时间 |
| `request` | 决策文本、请求模型、部门、会话哈希 |
| `graph` | 本次实际使用的完整当前图文档和非密钥运行配置 |
| `decision` | 状态、逻辑/真实模型、目标、选图与选路原因、兜底、粘性、策略路径、推理和耗时 |
| `jev` | JEV 模型、选择、置信度、边际、耗时和概率 |
| `answers` | 完整 JEV 答案 |
| `trace` | 当前回放页面使用的完整计算事件 |
| `node_labels` | 节点显示名称和类型 |

事件约束：

- `graph` 包含当前图的 ID、修订号、部门、时区、粘性、根节点、画布、backends、JEV 非密钥配置、窗口和时段。
- 不生成 `graph_snapshot_id`、`snapshot_revision`，不按内容去重，不拆成独立图存储表。
- 不包含 Provider Key、JEV Key、`Authorization` 或其他密钥值；`key_env` 名称可以保留。
- 只记录决策，不记录 newapi 渠道、计费、模型上游状态或 Token 用量。
- 已选中当前图后，无论决策成功还是路由失败，事件都带该图。
- 请求在选图之前因参数非法被拒绝，或根本没有当前图时，不生成可回放事件。

### 7.2 外围接收与读取

```http
POST /internal/v1/decision-logs
GET  /admin/requests/recent
GET  /admin/requests/{id}/replay
```

- 接收端校验事件结构、必需字段和请求大小，完整事件直接进入 `deque(maxlen=20)`。
- 最近列表返回既有页面所需的摘要和计算字段，避免列表响应重复携带 20 份完整图。
- 单条回放从事件读取 `graph`、`trace`、`request`、`answers` 和其他决策值。
- 删除旧 `/snapshot` 数据接口、SQLite 回退和快照 ID 依赖。
- 第 21 条淘汰最旧事件；淘汰或重启后的回放返回 404，不尝试恢复。

### 7.3 页面兼容

- 入口继续为 `routing.html?request=<决策 ID>`。
- 继续调用 `controller.loadTree()` 和 `rebuildTryRun()`，沿用当前画布及计算过程页面。
- 页面加载的图改为该条日志的 `graph`，trace 改为同条日志的 `trace`，不读取当前图或其他表。
- 当前图后来修改或删除不影响仍在内存中的历史回放。
- “查看图快照”等用户可见文案改为“决策回放”；内部变量和注释同步移除决策日志快照语义。
- 长期 SQLite 统计和 14 天决策日志统计移除；内存统计只覆盖当前保留的事件。

## 8. APISIX 与部署改造

- Auto Route 使用原 newapi upstream，不再使用 RoutePilot upstream。
- `sr-auto-router` 在 newapi forward-auth 后调用内核决策接口，解析响应并只改写请求体 `model`。
- 决策子请求只发送原始 JSON、Content-Type、可信部门、会话和 Request-ID，不发送 Authorization。
- Auto Route 复用固定链路的 URI 改写和既有插件；原始 Authorization 留给 newapi 原链路。
- 删除 `ROUTER_GATEWAY_TOKEN`、`X-SR-Gateway-Token` 和 `ROUTE_PILOT_UPSTREAM_API_KEY` 的代码、部署环境、Secret 和文档依赖。
- 163 图配置不再引用 `ROUTE_PILOT_UPSTREAM_API_KEY`。
- 内核和外围分别构建镜像；内核图目录挂载持久卷，外围 SQLite 使用自己的可选数据卷。
- NetworkPolicy 只允许受信任调用方访问内核管理和决策端口，以及内核访问外围日志接收端口；不以共享 Token 代替网络边界。

## 9. SQLite 与密钥迁移

- 新增外围 SQLite 迁移，删除 `request_logs`、`graph_snapshots`、`request_snapshots` 及相关索引。
- 历史决策日志不迁移到内存或 VictoriaLogs，升级后不再提供旧记录回放。
- 删除请求日志 SQLite writer、hydrate、队列落库和图快照序列化逻辑。
- 外围 JEV 页面只管理非密钥配置；移除 JEV Key 输入和 SQLite 密钥使用。
- 已有 JEV Key 不自动迁移，部署时显式设置内核 `ROUTER_JEV_API_KEY`。
- Provider Key 继续由外围 SQLite 或外围环境变量管理，仅用于 standalone 模型调用。
- 图 JSON 保持为内核事实源；切换部署时把图目录挂载给内核，并确保只有内核一个写者。

## 10. 验证与验收

### 10.1 内核

- 在无 SQLite、无外围容器的条件下完成图 CRUD、启停、部门选图和决策。
- 普通、JEV、兜底、粘性和异常路径均不调用模型上游。
- 日志成功、超时、不可达和禁用均符合 best-effort 语义。
- 每个可回放事件都含完整当前图和决策值，密钥扫描通过。

### 10.2 双容器 standalone

- 外围图管理经真实 HTTP 写入内核，随后决策立即使用新当前图。
- `/v1/chat/completions` 经内核决策后由外围调用 Provider，JSON 和 SSE 均正常。
- Provider Key 只出现在外围上游请求，不出现在内核请求、事件或页面。
- 修改/删除当前图后，内存中的历史回放仍使用日志内的图。
- 第 21 条正确淘汰；外围重启后最近列表为空、旧回放为 404。

### 10.3 APISIX + newapi

- 原用户 Key 由 newapi 校验，内核收不到 Authorization。
- newapi 最终收到选中后的真实 `model`，而不是 `auto`。
- 模型请求不经过内核，固定模型仍绕过内核。
- 无有效决策返回 503；JSON/SSE、部门选图、伪造部门头防护和回滚验证通过。

### 10.4 标准检查

- 运行完整 `pytest`。
- 运行 `npm run check:console`，覆盖现有回放页面和控制台回归。
- 分别构建两个容器并执行真实双服务 HTTP 联调。
- 更新所有仍描述旧代理、快照数据库或两个已删除环境变量的文档和注释。

## 11. 非目标

- 不重写图执行、JEV、条件、粘性或调度算法。
- 不实现执行结果回写、长期内存日志或外围日志数据库。
- 不在本期实现 VictoriaLogs 适配器，只固定兼容的事件结构。
- 不在本期部署或修改共享 163/APISIX 现网；所有远端变更需要后续明确授权和维护窗口。
- 不把图编辑中的“运行配置随目录更新”误删为日志快照清理的一部分；两者是不同概念。
