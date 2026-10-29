# Smart Router v3

Smart Router 是一个 OpenAI 兼容的模型路由代理：**NeoHorse-JEV 负责按模型能力选择目标，会话粘性和最小在途请求负责选择实际上游**。控制台提供模型管理、JEV 服务配置、分流树、时间段和计算日志。

本版是开发中的不兼容重构：旧信号体系、旧路由树和旧数据库 schema 均已移除，没有兼容适配层，也不会在 router 进程内加载 torch/transformers。

## 架构

```text
POST /v1/chat/completions
  │
  ├─ 读取一份不可变 RoutingSnapshot
  ├─ 按命名时间段选择确定性 branch
  ├─ 检查当前策略仍允许的会话粘性
  │   └─ 命中：跳过 JEV
  ├─ 未命中：调用一次 NeoHorse /v1/systemone
  │   ├─ 通过概率/差距门槛：进入选中的 target
  │   └─ 失败或不确定：进入 fallback target
  ├─ 在 target 内选择最小在途的健康后端，计数 +1
  ├─ 转发 OpenAI 非流式或 SSE 请求
  └─ 完成、错误、断连或取消时释放计数并写计算日志
```

职责分离：

- **JEV**：判断“哪类能力/哪个模型目标合适”，不承担负载均衡；
- **SessionAffinity**：判断“能否复用当前策略仍允许的模型”；
- **LoadBalancer**：判断“target 内哪个后端在途最少”；
- **CircuitBreaker**：剔除当前熔断的后端。

JEV 客户端与训练仓库 `/Users/yuansu/work/smart-router-train` 的 NeoHorse-JEV-4B 原生 vLLM 0.32 插件对齐。训练仓库既有实验采用 400 条主集和 40 条对抗集；其准确率与延迟结果属于该模型/部署评测，不是本项目的重构验收结果。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 全新开发数据库；不要把旧版 router.db 直接交给 v3
ROUTER_DB=/tmp/smart-router-v3.db .venv/bin/python -m router
```

默认监听 `127.0.0.1:8400`，控制台为 `http://127.0.0.1:8400/console/`。生产环境默认启用控制台账号，首次启动向空 users 表写入：

- `admin@test.com`
- `1qaz@WSX`

上线后请立即修改密码。本地开发可设 `ROUTER_USER_AUTH=0` 关闭 `/admin` 鉴权。

标准测试：

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/i18n_dead_keys.py
npm install
npm run check:console   # 需要已启动服务和可用的 Chromium
```

## 配置 NeoHorse JEV

先在训练仓库部署原生 vLLM 0.32 服务，确保其提供：

```text
POST http://<jev-host>:30000/v1/systemone
```

然后通过控制台「JEV 模型」填写：

- 服务地址，例如 `http://127.0.0.1:30000`；不要重复追加 `/v1/systemone`；
- served model name，例如 `NeoHorse-Jev-4B`；
- 可选 API 密钥、超时、默认 instructions 和 state 模板；
- 「连接测试」会真实调用该服务，但不改变分流状态。

也可在首次启动时通过环境变量播种配置：

```bash
ROUTER_DB=/tmp/smart-router-v3.db \
ROUTER_JEV_URL=http://127.0.0.1:30000 \
ROUTER_JEV_MODEL=NeoHorse-Jev-4B \
ROUTER_JEV_API_KEY= \
.venv/bin/python -m router
```

`ROUTER_JEV_URL` 与 `ROUTER_JEV_MODEL` 必须同时提供。数据库已有配置时不会被环境变量覆盖。密钥 GET 永不回显；控制台可清除数据库密钥并改用 `ROUTER_JEV_API_KEY`。

请求使用训练仓库验证过的格式：

```text
【待判断的用户请求】
<最后一条 user 文本>

【任务】请选择最合适处理这条请求的模型。
```

一个 `choice` question 包含有序 `criteria`。客户端根据完整 `probabilities` 排序，不直接信任 `choice`，也不把原生 `confidence` 当作跨候选数可比较的校准值。

## 模型与提供方

「模型与设置」保留预置目录、自定义模型、提供方密钥、获取可用模型、能力说明和推理档位：

- 模型位 ID 是分流树中的稳定引用；upstream 是实际发给 OpenAI 后端的模型名；
- 单模型 target 默认读取 `profile.capability` 作为 JEV 候选说明；
- 多模型 target 必须直接在 target 上填写整组能力说明；
- target 中每个模型位可单独设置 reasoning；`null` 表示完全透传客户端字段；
- 保存模型或提供方时会重新校验启用树，避免删除正在使用的能力说明或推理档位；
- 被任一保存树引用的模型不能删除。

## 新分流树

只支持三种节点：

| 节点 | 用途 |
|---|---|
| `branch` | 有序时间条件分支和 `otherwise` 兜底 |
| `jev` | 一次 NeoHorse choice，2–255 个有序候选及必需 fallback |
| `target` | 一个或多个模型位；单模型直接路由，多模型最小在途 |

不再有信号条件、权重评分、拦截节点或嵌套 JEV。`min_prob` 和 `min_margin` 默认均为 0，即采用 JEV argmax；需要保守策略时显式提高门槛。JEV 调用失败、响应非法或门槛未通过都走该节点的 fallback，不重试第二候选。

简例：

```json
{
  "id": "main",
  "name": "主分流树",
  "timezone": "Asia/Shanghai",
  "sticky": {"enabled": true, "ttl_seconds": 1800},
  "root": {
    "id": "root", "type": "branch",
    "children": [{
      "id": "night-choice", "type": "jev",
      "when": {"periods": ["夜间"]},
      "instructions": "根据请求选择最合适的模型。",
      "min_prob": 0, "min_margin": 0,
      "options": [
        {"key": "fast", "label": "轻量模型", "target": {
          "id": "fast-target", "type": "target", "strategy": "least_inflight",
          "models": [{"model": "qwen3-8b", "reasoning": null}]
        }},
        {"key": "strong", "label": "增强模型", "target": {
          "id": "strong-target", "type": "target", "strategy": "least_inflight",
          "capability": "复杂代码、长文档与深度推理。",
          "models": [{"model": "strong-a", "reasoning": "high"},
                     {"model": "strong-b", "reasoning": "high"}]
        }}
      ],
      "fallback": {"id": "safe", "type": "target", "strategy": "least_inflight",
                   "models": [{"model": "qwen3-8b", "reasoning": null}]}
    }],
    "otherwise": {"id": "default", "type": "target", "strategy": "least_inflight",
                  "models": [{"model": "qwen3-8b", "reasoning": null}]}
  }
}
```

树支持草稿、复制、启停、导入导出和 dry-run 试算。启用时要求模型、时间段、能力说明、推理档位和最终 fallback 全部有效；新库不隐式创建或启用树。没有启用树时 `/v1` 返回 503 `no_active_tree`。

出厂示例见 `configs/default_routing_tree.json`，可通过以下脚本应用：

```bash
SR_BASE=http://127.0.0.1:8400 .venv/bin/python scripts/apply_default_routing_tree.py --enable
```

## 时间段

「分流树」页保留命名时间段：`name/days/start/end`。

- 周一为 0，周日为 6；
- `start > end` 表示跨午夜；
- `start == end` 表示全天；
- 结束时刻不包含；
- 树级 IANA timezone 默认 `Asia/Shanghai`。

时间是树的原生确定性条件，不是模型信号。时间段在粘性和 JEV 之前求值，因此旧会话不能借粘性绕过当前时段策略。

## 会话粘性

在 OpenAI 请求中显式传入：

```http
x-sr-session-id: <your-conversation-id>
```

没有该 header 时不使用粘性。命中条件包括：同一 `tree_revision + policy_path`、模型仍在允许集合、后端存在、熔断未开、TTL 未到期。命中后跳过 JEV；未命中则正常决策并更新缓存。

- 默认滑动 TTL 为 1800 秒，可按树调整；
- LRU 上限默认 10000，可用 `ROUTER_SESSION_MAX_ENTRIES` 调整；
- 缓存只存 SHA-256 后的会话键，不存原始会话 ID；
- 缓存为单进程内存，重启清空；dry-run 不读入新条目也不写缓存。

## 最小在途与熔断

多模型 target 使用 `least_inflight`。选择和计数递增在同一把锁内完成，同分时保持 target 的模型配置顺序。粘性优先于最小在途；熔断模型不参与选择。

计数覆盖完整上游生命周期，包括 SSE、正常响应、5xx、客户端断连和任务取消。全部后端不可用时返回 503 `no_available_backend`，不会静默落到未配置模型。

**当前运行模式是单 uvicorn worker。** 粘性、在途计数和计算日志详情 ring 都是进程内状态，不声称支持多副本全局调度。

## 计算日志与版本

控制台「计算日志」保留最近 20 条请求的内存详情：请求文本、时间路径、JEV 概率、fallback、粘性、最小在途、trace 和最终状态。

SQLite 使用单张 `request_logs` 保存决策与结果元数据：tree/snapshot revision、policy path、JEV choice/top/margin/耗时、sticky、selection reason、延迟、usage、上游状态和终态。请求文本、完整概率、原始会话 ID 和密钥不会持久化。

管理写操作在 publisher 锁内更新数据库、记录内容版本并一次赋值发布新快照。进行中的请求继续使用开始时的快照；热路径不查询配置数据库。

旧版数据库不会自动迁移或删除。启动时发现旧版本或不明内容会要求使用新数据库；请显式设置一个新的 `ROUTER_DB`。

## API 错误与上游行为

- 无启用树：503 `no_active_tree`；
- 所有目标后端不可用：503 `no_available_backend`；
- JEV 失败：使用树内 fallback；
- 上游 4xx：状态码和错误体透传，不计熔断失败；
- 上游 5xx/网络错误：返回 502，记录真实上游状态并计熔断失败；
- 流式终态：`completed / upstream_error / client_disconnected / cancelled / incomplete`。

`POST /admin/try` 与线上共用同一个 `RoutingEngine`，但使用 `dry_run`：不写粘性、不增加在途、不调用上游，也不替换线上快照。

## 后续部门/用户控制

部门或用户必须来自可信网关/身份系统，不能让 JEV 从提示词猜测。扩展点位于确定性 `when` 条件：在时间/部门条件得到允许集合后，才检查粘性和调用 JEV。

这样可以按部门限制高级模型候选、fallback 或 target，而不修改 JEV 客户端和负载均衡器；粘性也不能跨 `policy_path` 复用高级模型。

## 运维

`scripts/deploy-76.sh` 与 `scripts/smart-router-v3.service` 是 v3 systemd 部署模板。JEV 是独立 HTTP 服务，部署时只需提供 `SR_JEV_URL` 和 `SR_JEV_MODEL`，不再配置 checkpoint、tagger 或本地模型目录。

主要环境变量：

| 变量 | 默认 | 用途 |
|---|---|---|
| `ROUTER_DB` | `router.db` | 当前 v3 SQLite；生产请显式指定新路径 |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8400` | 监听地址 |
| `ROUTER_JEV_URL` / `ROUTER_JEV_MODEL` | 空 | 首次启动播种远程 JEV 配置 |
| `ROUTER_JEV_API_KEY` | 空 | 可选 JEV 密钥 |
| `ROUTER_JEV_TIMEOUT` | `10` | JEV 请求超时秒数 |
| `ROUTER_SESSION_MAX_ENTRIES` | `10000` | 粘性 LRU 上限 |
| `ROUTER_USER_AUTH` | 生产 `1` | 控制台账号鉴权 |
| `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` | 见上文 | 空 users 表首次 seed |
| `ROUTER_SESSION_TTL_HOURS` | `168` | 控制台登录会话 TTL |
