# RoutePilot 部门路由、独立内核与 APISIX 集成设计

日期：2026-11-24
状态：设计已确认，待书面设计评审

## 1. 背景与最终方向

RoutePilot 当前已经具备完整的智能路由后端：自包含图 JSON、图校验与编译、多波次 JEV、模型选择、会话粘性、熔断、推理改写、上游 JSON/SSE 代理、控制台、SQLite 管理目录、请求账本和多副本图重载。

本次不拆除这些能力，也不维护两套内核或单独的功能分支。最终目标是：

- `main` 保持单一代码线，所有提交只推送 Gitee。
- 同一个镜像既能独立运行，也能部署在 APISIX 后面。
- 独立运行与网关嵌入共用同一个图执行器、快照装配和上游代理，不增加 decision-only 的第二套实现。
- 图支持部门配置、多图同时启用和唯一默认图。
- 当前 newapi 或后续 token-factory 负责 API Key 校验和部门来源；RoutePilot 不需要按 Key 内省部门。
- JEV 使用 sandlake-admin 提供的原生 `/v1/systemone`，不经过 token-factory。
- 不新增 token-factory 的部门内省或模型目录接口。

工作拆成两个阶段：第一阶段先完成独立项目改造；第二阶段才接入 APISIX 并在 163 验证。

## 2. 范围

### 2.1 第一阶段：独立项目

- 图文档破坏性升级为 v4，不保留 v3 或 `current` 指针兼容逻辑。
- 每份图保存 `departments`、`enabled`、`default`。
- 支持多图启用、默认图、部门设置 API、默认图切换事务和完整快照集。
- main 控制台同步支持部门编辑、多图启停、默认图和 v4 导入导出。
- 公开 `/v1` 独立运行时只使用默认图；`/admin/try` 和管理端测试能力用于验证部门选图。
- 保留现有模型代理、JEV、SQLite、控制台、管理 API 和请求账本。
- 更新示例图、测试契约、README 与图规范。

第一阶段不修改 163、APISIX、newapi 或 token-factory，不开放内部网关数据面。

### 2.2 第二阶段：APISIX 与 163

- 增加内部数据面、网关凭据和可信部门 Header。
- 在当前 newapi 的鉴权后增加 Auto 拦截；未来 token-factory 复用同样的 Auto 拦截行为。
- 配置当前 newapi / token-factory 模型出口和 sandlake-admin 原生 JEV。
- 在 163 分别完成当前 newapi 和替换后 token-factory 的端到端验收。

第二阶段在第一阶段验收后另写独立实施计划。

## 3. 总体架构

### 3.1 独立运行模式

```text
客户端
  → RoutePilot POST /v1/chat/completions
  → 默认图
  → 图执行器与 JEV
  → 改写请求体 model
  → 配置的模型网关
  → JSON / SSE 返回
```

公开 `/v1` 不信任客户端部门 Header。管理端检查路由和测试功能可以显式提供部门，用于验证实际选图规则。

### 3.2 APISIX 嵌入模式

```text
客户端
  → APISIX
  → 当前 newapi / 后续 token-factory 校验 API Key
  → Auto 中间件读取 token.Department
  → 覆盖式注入可信 X-SR-Department
  → RoutePilot 内部数据面
  → 部门精确选图，未命中使用默认图
  → 图执行器与原生 /v1/systemone JEV
  → 改写请求体 model
  → 使用内部服务 Key 请求当前 newapi / token-factory
  → JSON / SSE 原路返回
```

只有 `model: "auto"` 进入智能路由。固定模型请求保持原直连链路，不经过 RoutePilot，避免再次进入路由形成循环。

RoutePilot 继续负责请求体改写和上游代理，APISIX 不需要 Lua 改写插件。外部用户 Authorization 不转发给模型上游；RoutePilot 使用图配置对应的内部服务 Key。

未来 newapi 替换为 token-factory 时，内核不变，只需要复用 Auto 拦截行为并切换图内的网关地址和服务 Key。Auto 拦截是转发行为改造，不新增 token-factory API。

## 4. v4 图文档

### 4.1 格式

以下为一份可保存的部门图草稿：

```json
{
  "kind": "sr-routing-graph",
  "version": 4,
  "name": "部门路由",
  "departments": ["财务部", "法务部"],
  "enabled": false,
  "default": false,
  "timezone": "Asia/Shanghai",
  "sticky": {"enabled": true, "ttl_seconds": 1800},
  "root": {
    "id": "root",
    "type": "graph",
    "nodes": [
      {"id": "start", "type": "start", "title": "开始"},
      {
        "id": "safe",
        "type": "target",
        "priority": 1,
        "models": [{"model": "fast", "reasoning": null}]
      }
    ],
    "edges": [
      {
        "id": "e-start-safe",
        "source": "start",
        "source_port": "out",
        "target": "safe",
        "target_port": "in_1",
        "condition": null
      }
    ],
    "error_fallback": "safe"
  },
  "backends": {
    "providers": [
      {
        "name": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "key_env": "",
        "reasoning_format": "openai"
      }
    ],
    "models": [
      {"id": "fast", "upstream": "fast", "provider": "local", "profile": {}}
    ]
  },
  "jev": null,
  "jev_window": {"input_tokens": 1024, "context_tokens": 4096},
  "time_periods": []
}
```

`departments`、`enabled`、`default` 均为必填字段。`canvas` 仍按现有规则可选，四个运行字段仍必须成组出现。既有节点、边、运行配置和密钥规则保持不变。

旧示例图和部署图直接修改为 v4；运行时不增加旧格式检测、自动迁移或兼容分支。`current` 文件不再读取或写入。

### 4.2 部门规则

- `departments` 是字符串数组；每项去空白后非空、最长 64 字符、不得含控制字符，数组内不得重复。
- 部门匹配采用解码后的精确、大小写敏感比较，不做子串或模糊匹配。
- 同一部门只能归属一张已启用图；未启用图之间允许重复，以便并行编辑草稿。
- 默认图也可以声明部门：先精确匹配，未命中再使用默认图。
- 非默认图且 `departments: []` 是合法的备用图，之后可以通过“设为默认”接管流量。
- 请求没有部门、部门为空或部门未命中任何启用图，都使用默认图。

`departments` 参与 `tree_revision`，因为它影响图选择行为。`enabled/default` 是生命周期状态，不参与单图 `tree_revision`，但参与文件指纹和路由集 `snapshot_revision`。

### 4.3 生命周期规则

- `default: true` 必须同时 `enabled: true`。
- 有启用图时必须恰好有一张默认图；零启用图是合法空状态。
- 新建、复制和导入图强制 `enabled: false、default: false`，不会直接接管流量。
- 第一张启用图自动成为默认图；后续启用不改变默认图。
- 停用最后一张启用图后进入空路由集。
- 默认图之外还有其他启用图时，禁止直接停用默认图，必须先切换默认图。
- `enabled/default` 只能通过专用生命周期 API 修改，普通图保存不能绕过。

## 5. 快照集与选图

启动和定时重载时扫描全部图文件，为所有启用图构造不可变快照集：

- `default_snapshot`：唯一默认图快照，可以为空。
- `department_snapshots`：部门到图快照的一对一映射。
- `snapshot_revision`：由默认图、部门映射和各图版本共同计算的路由集版本。

请求先按部门精确查找，未命中使用 `default_snapshot`。原图执行器、JEV 波次、目标选择、会话粘性、随机模型选择、熔断、推理改写和代理逻辑继续消费选中的单图快照。

一致性规则：

- 任一启用图解析、启用校验或编译失败，整套候选快照不发布。
- 重载失败继续沿用上一份完整快照，并报告 `stale`，不运行半套路由配置。
- 无启用图返回 `503 no_active_tree`。
- 启用集合缺少默认图返回 `503 no_default_tree`。
- 没有可用初始快照返回 `503 no_valid_snapshot`。
- 响应增加 `x-sr-tree-id` 和 `x-sr-graph-selection: department|default`。
- 请求账本继续记录实际选中的 `tree_id/tree_revision/snapshot_revision`；不同图的粘性和后端状态不得串用。
- 熔断与 inflight 的内部运行键按 `tree_scope(tree_id) = tree:<tree_id>`、`runtime_model_key(scope, model) = <scope>:<model>（scope 非空）或 <model>（固定直连）` 构造（详见 `docs/决策图规范.md` §8.3）：只作用于运行状态，不改变图文档、`tree_revision`、响应模型名与账本模型名；同一 scope 跨副本稳定，另一图及固定直连不受故障影响。

## 6. 管理 API

### 6.1 部门设置

```http
PUT /admin/trees/{id}/departments
Content-Type: application/json

{
  "departments": ["财务部", "法务部"],
  "base_document_sha256": "<current-document-sha256>"
}
```

该接口与普通图保存共用校验、写入和审计路径，返回更新后的 `tree`、路由集 `snapshot_revision` 和 `graph_status`。普通图保存也可以修改 `departments`，但不能修改生命周期状态。

错误包括：

- `422 graph_parse_error`：部门字段类型、长度、空值或图内重复非法。
- `422 department_conflict`：与其他已启用图冲突，并指出冲突部门和图 ID。
- `409 graph_conflict`：基线文档哈希过期。

### 6.2 生命周期

- `POST /admin/trees/{id}/enable`：只启用当前图；第一张启用图同时成为默认图。
- `POST /admin/trees/{id}/disable`：只停用当前图；停用规则见第 4.3 节。
- `POST /admin/trees/{id}/default`：目标图设为默认并启用；原默认图保持启用但改为非默认。

禁止直接停用仍需保留其他启用图的默认图时，返回 `409 default_graph_required`。

`GET /admin/trees` 和单图响应包含 `departments/enabled/default`。`/readyz` 与管理列表的 `graph_status` 增加默认图 ID、启用图 ID 集合和路由集版本。

### 6.3 检查路由与测试对话

`POST /admin/try` 增加可选 `department`：

- 指定 `tree_id`：测试该图的草稿或已存版本。
- 不指定 `tree_id` 且没有草稿 `root`：按给定部门执行实际选图；未提供部门时使用默认图。
- 提供草稿 `root`：只测试草稿，不读取路由集。

管理端测试对话默认使用默认图，允许管理员显式指定部门。固定模型测试仍绕过图和 JEV。

## 7. 默认图切换与多文件一致性

默认图切换是一个多文件写用例：

1. 获取全局单写锁，读取当前路由集并校验目标图和部门唯一性。
2. 暂存原默认图与新默认图的完整 JSON。
3. 构造完整候选快照集，并复核相关文件指纹。
4. 使用 `GraphBatch` 提交；失败时恢复已替换文件。
5. 审计提交成功后，一次性替换内存快照集。

普通图保存、目录影响同步和生命周期写也改为针对整个启用快照集构造候选，不再只准备唯一活动图。

文件与 SQLite 不声称具有跨系统 ACID 事务。一致性依赖现有单写者、暂存、回滚、指纹复核和“提交前不发布”的规则。进程在部分文件替换后崩溃时，内存仍保留旧快照；后续非法或不完整路由集不得发布，必须恢复文件后再重载。

## 8. 控制台

main 控制台与后端同步升级：

- 图设置增加多部门编辑、格式校验和跨图冲突提示。
- 决策组列表和当前图状态同时展示“已启用”和“默认”。
- 各图独立启停；启用一张图不再停用其他图。
- 增加“设为默认”操作，以及默认图停用限制和切换提示。
- 检查路由支持输入部门，并展示实际选中的图与选择原因。
- 管理端测试对话默认显示默认图，可输入部门测试其他图。
- 导入导出、前端严格解析、行为 canonical、图契约 fixture、审计与影响确认全部升级为 v4。

## 9. 第二阶段接入契约

### 9.1 内部数据面

第二阶段增加 `POST /internal/v1/chat/completions`，与公开 `/v1` 共用执行器，但接受可信部门并执行完整部门选图。

安全要求：

- 内部数据面仅通过 ClusterIP 暴露给 APISIX，不提供公网入口。
- 使用 `ROUTER_GATEWAY_TOKEN` 和 `X-SR-Gateway-Token` 做独立网关凭据校验，采用常量时间比较，凭据不写日志。
- APISIX / Auto 中间件必须删除客户端提供的 `X-SR-Department`、`X-SR-Gateway-Token` 及其他内部 Header，再注入可信值。
- 公开 `/v1` 收到 `X-SR-Department` 时返回 `400 department_header_not_allowed`，不静默信任或忽略。
- `X-SR-Session-Id` 和请求 ID 按现有规则转发；原始用户 Authorization 不进入 RoutePilot 内部数据面或模型上游。

### 9.2 部门 Header 编码

`token.Department` 是部门名称，可能包含中文。Auto 中间件使用 Go `url.PathEscape` 生成 `X-SR-Department`；RoutePilot 使用严格 UTF-8 百分号解码并校验最长 64 字符及控制字符。

- 缺失或空部门：使用默认图。
- 合法但未匹配的部门：使用默认图。
- 编码非法、UTF-8 非法或长度非法：返回 `400 invalid_department`，不进入图执行。

### 9.3 Auto 中间件位置

当前 newapi 在 `TokenAuth` 之后、`Distribute` 之前拦截 `model: "auto"`：

- 复用已经校验的 `token.Department`，不增加按 Key 查询部门的网络调用。
- 保留 JSON/SSE 转发，不缓冲完整流式响应。
- 固定模型继续走原 `Distribute` 和模型 relay。
- 未来 token-factory 复用同一行为，但不新增部门内省、路由配置目录或模型目录接口。

## 10. 部门、模型与 JEV 接口证据

### 10.1 部门

源码确认：

- `sandlake-api/model/token.go` 的 `Token.Department` 是运行时事实源，`TokenAuth` 已取得完整令牌。
- `GET /api/v1/api-gateway/token/` 和 `/api/v1/api-gateway/token/search` 的令牌结果包含 `department`，支持分页与部门过滤；普通用户看自己的 Key，管理员可看全部。
- `GET /api/v1/api-gateway/user/search` 允许管理员取得用户的 `dept_name`。
- token-factory 已有对应 token 管理接口和 `department` 字段，无需新增接口。

163 的 APISIX 未携带身份访问上述管理接口返回 401，说明不能把未鉴权访问当成前端前提。带身份的浏览器请求和响应字段在第二阶段使用测试账号验收。

没有公开的独立部门目录，也没有按 API Key 内省部门的接口。图里的部门值必须与 token 中的 `department` 完全一致。sandlake-admin 的 `sys_dept` 是另一套组织数据，不能直接当作令牌部门事实源。

### 10.2 模型

`GET /apiuser/v1/models`：

- 使用 Bearer API Key，返回模型 ID、提供方和支持端点。
- 163 实测无 Key 返回 401；直接服务 CORS 预检返回 204，允许 Authorization 请求头。
- token-factory 的同名接口也存在，替换后需复验权限和响应。

`GET /apiuser/api/v1/api-gateway/models/search`：

- 163 已真实返回模型记录，包括 `model_name`、说明、标签、端点、分类、上下文长度等字段；实测目录中含 `d1-2b`。
- token-factory 另有 `/api/v1/api-gateway/models/search` 和 `/models/available`，替换后需要复验权限与响应形状。

当前 rich 模型响应还包含 `channel` 连接信息。前端只能提取 `model_name`、说明、标签、端点、分类、上下文长度等白名单字段，禁止把整个响应或 `channel` 写入图 JSON、日志或浏览器持久化状态。

管理页面请求优先走同源 APISIX，不依赖“跨域 Cookie + 通配 Origin”的组合。

### 10.3 JEV

sandlake-admin 的 `GET /api/v1/models` 可提供模型名称、说明、标签、上下文长度等非密钥元数据。运行时 JEV 使用其提供的原生 `/v1/systemone` 服务，真实地址与鉴权方式在第二阶段通过实际请求验收。

图 JSON 的 `jev.base_url` 不包含 `/v1/systemone` 后缀，现有 `JevClient` 自动拼接。图只保存模型、地址、提示词、输入窗口和 `key_env`；真实 JEV Key 仍由服务端 SQLite 或环境变量保存。newapi 替换为 token-factory 不影响 JEV 链路。

## 11. 错误与安全边界

- 部门冲突在写入或启用前拒绝，不允许运行时依赖启用顺序决定归属。
- 默认图缺失、重复或处于停用状态时，候选路由集非法。
- 公开 `/v1` 不信任部门 Header；内部数据面同时依赖网络隔离和独立网关凭据。
- 所有 Key 继续只存在服务端受控配置中，不进入图 JSON、API 出参、浏览器持久化状态或日志。
- 外部 Authorization、内部网关凭据、模型服务 Key 和 JEV Key 是四个不同凭据，不得互相透传或复用。
- 图文件仍不保存密钥，密钥派生状态字段按现有递归规则拒绝。
- JSON 和 SSE 保持现有 10 MiB 请求上限、错误映射、断连和租约释放语义。
- 固定模型始终绕过智能路由，防止 Auto 循环，也避免不必要的 JEV 调用。

## 12. 验证策略

### 12.1 第一阶段自动化

运行项目标准门禁：

```bash
.venv/bin/python -m pytest -q
npm run check:console
.venv/bin/python scripts/i18n_dead_keys.py
```

重点覆盖：

- v4 全字段解析、未知字段、非法部门和非法生命周期。
- 多图启用、第一张图自动默认、独立停用和显式切换默认图。
- 部门精确选图、空部门与未知部门回退默认图。
- 跨图部门冲突、默认图停用限制和默认切换文件回滚。
- 启用图更新、目录影响同步、只读副本重载和 `stale`。
- 不同图的 JEV、模型、推理配置、粘性、熔断和请求日志隔离。
- 前端部门编辑、默认状态、冲突提示、导入导出和中英文文案。

### 12.2 第一阶段真实冒烟

使用同一镜像和模拟 JEV / 模型上游运行真实应用：

1. 创建默认图与两张部门图并同时启用。
2. 公开 `/v1` 使用默认图，验证 JSON 与 SSE、模型改写和响应头。
3. 通过 `/admin/try` 分别验证两个部门、未知部门和空部门的选图结果。
4. 切换默认图并复测；验证默认图停用限制和最后一张图停用。
5. 验证部门设置 API 的哈希冲突和部门冲突。
6. 重启副本，确认路由集恢复；制造非法候选文件，确认旧快照保留并报告 `stale`。

第一阶段通过前，不开始 APISIX 接入。

### 12.3 第二阶段 163 验收

分别在当前 newapi 和替换后的 token-factory 上执行：

- 使用测试身份验证部门接口与模型接口的鉴权、分页和字段白名单。
- 验证原生 `/v1/systemone` 的地址、模型、鉴权、超时和非法响应兜底。
- 至少两个部门命中各自图，未知部门与空部门使用默认图。
- 固定模型不经过 RoutePilot；Auto 的 `model` 被替换为图内上游模型。
- JSON、SSE、断连、上游 4xx/5xx、JEV 失败和网关失败符合契约。
- 伪造部门与网关 Header 被拒绝，直接访问内部数据面被拒绝。
- 原始 Authorization 不到达模型上游，内部服务 Key 不出现在日志。
- 回滚 APISIX Auto Route 或应用镜像后，固定模型与默认链路可恢复。

token-factory 尚未替换前，不以 newapi 的通过结果推断 token-factory 已通过。

## 13. 提交与实施约束

- 不创建功能分支；所有改动最终进入 `main`，只执行 `git push origin main` 或等价的 `HEAD:main`，不推送 GitHub。
- 设计文档单独提交，不夹带其他文件。
- 第一阶段代码基于最新 `main` 实施并直接提交 `main`；提交前检查工作区，不纳入与本功能无关的未跟踪产物，也不覆盖他人的并行修改。
- 第一阶段先写详细实施计划；第二阶段在第一阶段验收后另写计划。
- 默认不添加新依赖，沿用现有 FastAPI、httpx、SQLite、原生 ES Modules 和 Node 测试工具链。
- 修改内核时同步更新对应控制台、契约 fixture、README、图规范和相关注释，不保留描述旧单活动图行为的文档。

## 14. 已确认的取舍

- 选择图内保存 `departments/enabled/default`，不采用单独路由集指针或额外启用文件。
- 允许破坏性升级，不维护旧图、旧 `current` 或旧控制台契约的兼容代码。
- 同一镜像通过部署方式支持独立与嵌入，不通过代码分支区分两种产品。
- 第一阶段只做独立项目，第二阶段才做 APISIX 和 163。
- 部门直接取自已校验令牌，不调用 token-factory 内省接口。
- 不新增 token-factory 接口；未来仅复用 Auto 转发行为。
- JEV 使用原生 `/v1/systemone`，不通过 token-factory。
- 固定模型旁路，Auto 才进入智能路由。
