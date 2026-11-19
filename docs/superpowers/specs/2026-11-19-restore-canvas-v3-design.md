# 恢复 v3 画布 + canvas key 格式设计

日期：2026-11-19
状态：已确认设计，未实施

## 1. 背景与决策

Routing Policy v4 完整替换（f22d0ec）移除了拖拽式画布，用户裁决：画布是该项目的核心交互，
v4 表单式编辑器难用，方向反转——恢复之前的 v3 画布方案与画布 JSON，只保留前后端分离的改进；
序列化上把画布坐标从节点对象挪到文档顶层独立的 `canvas` key（阅读时折叠即得纯逻辑视图）。

本仓库是开发项目：解析**不做双格式兼容**，只认新格式；所有旧格式文件一次性重写。

- 保留 v4 实验为 git 历史（符合本仓库保留实验证据的习惯），用 `git revert -m 1 f22d0ec` 撤销合并。
- 用户隔夜合入 main 的 11 个 chat 提交与 revert 无关，自然保留。
- 76 生产实例随之回退并更新。

## 2. 目标与非目标

### 2.1 目标

1. 恢复 v3 拖拽式画布：拖节点、拉连线、条件浮层、检查器、图例等全部交互照旧。
2. 画布 JSON 的坐标集中到顶层 `canvas` key，节点对象只含逻辑字段。
3. 行为哈希/快照/校验/评估与坐标彻底无关（前后端分离语义）。
4. 旧格式文件（仓库 configs、76 存量图、用户用例）一次性重写为新格式。
5. 76 恢复画布服务并升级到新格式。

### 2.2 非目标

- 不恢复 v4 策略模型、规则语义或表单编辑器（整体随 revert 撤销）。
- 不做双格式解析、迁移工具、兼容开关。
- 不改 JEV wire、调度、熔断、粘性、账本等与图序列化无关的模块。
- 不处理 76 routing-policy 目录的数据（v4 切换产物，直接删除）。

## 3. 文档格式（唯一实质变更）

```json
{
  "kind": "sr-routing-graph",
  "version": 3,
  "name": "默认决策组",
  "timezone": "Asia/Shanghai",
  "sticky": {"enabled": true, "ttl_seconds": 1800},
  "root": {
    "id": "root",
    "type": "graph",
    "error_fallback": "safe-target",
    "nodes": [
      {"id": "start", "type": "start", "title": "开始"},
      {"id": "q1", "type": "question", "question_type": "choice", "instructions": "…", "title": "主问题", "options": []},
      {"id": "t1", "type": "target", "models": [], "priority": 1, "title": "轻量"}
    ],
    "edges": []
  },
  "canvas": {
    "start": {"x": 40, "y": 200},
    "q1": {"x": 260, "y": 200},
    "t1": {"x": 560, "y": 200}
  }
}
```

约束：

- 顶层字段表变为 `{kind, version, name, timezone, sticky, root, canvas}`；`canvas` 可选。
- `canvas` 为对象：`node_id → {"x": <有限数值>, "y": <有限数值>}`；只接受 `x/y` 键。
- 节点对象出现 `position` 字段 → `graph_parse_error`（严格单格式；version 保持 3）。
- `canvas` 引用不存在的节点 id → 拒绝。
- 节点缺 canvas 条目 → 合法，前端自动分层布局补位。
- 前端导出时收集全部节点位置统一写入 `canvas`。
- 除坐标集中外，envelope、nodes、edges、语义字段与 base 7a25c3b 的 v3 格式逐字段一致。

## 4. 后端改动（仅两个序列化边界）

- `router/routing/document.py`：`FIELDS` 增加 `canvas`；`parse_graph_document` 校验 canvas
  形状/数值/引用；`graph_document` 输出顶层 `canvas`。
- `router/routing/graph/model.py`：节点解析不再读 `node.position`（出现即未知字段拒绝）；
  位置从顶层 canvas 映射注入；`graph_to_dict` 输出无 position 节点 + 顶层 canvas。
  domain 的 `GraphNode.position: Position | None` 保持为内部工作状态（解析时从 canvas 填充），
  `tree_behavior_dict`/`tree_revision_of` 不变（行为哈希本就排除坐标）。
- 其余零改动：快照、启用校验、GraphEvaluator、调度、粘性、reloader、API、账本、控制面。

## 5. 前端改动（仅导入导出边界）

- `router/console/routing/graph.js`：`IMPORT_FIELDS` 加 `canvas`；`NODE_FIELDS` 各类型删
  `position`；`ENVELOPE_FIELDS` 不变（服务端资源 envelope 与画布文档分离）。导入时把 canvas
  分发回内部 `node.position`；导出/下载/示例把 `node.position` 收集进顶层 `canvas`。
- 拖拽/连线/条件浮层/检查器/渲染/布局逻辑零改动；无 canvas 条目的节点走既有自动布局。
- 拖拽坐标只触发 DraftStore 既有 `layout` 变更类别（不产生 behavior dirty），与 v3 行为一致。

## 6. 旧文件一次性重写

- 仓库内：`configs/default_routing_tree.json`、`configs/production_routing_tree.json`、
  `router/console/routing/example.json`，以及用户未跟踪的 `configs/cases/*.json`（先备份到
  /tmp 再改写）。
- 76 上 `/opt/smart-router-v3/routing-graph/graphs/` 的 11 个文件（部署时重写，目录先备份）。
- 用内联一次性脚本完成，不向仓库提交永久迁移工具。

## 7. 部署顺序（两阶段）

- **Phase 0（revert 后立即部署）**：`git revert -m 1 f22d0ec` → 同步代码到 76 → 停服 →
  DB 恢复切换前备份 `/opt/router-v4-cutover-backup-20261119-154714.db`（schema v4、188 行
  账本；丢弃 v4 期间约 4 条测试行）→ unit 环境变量换回 `ROUTER_GRAPH_DIR/ROUTER_GRAPH_WRITER`
  → 删除 `routing-policy/` → 重启。此时旧格式代码 + 原样旧图 = 画布服务恢复，
  `/admin/trees` 200，default-jev 继续生效。
- **Phase 1（delta 完成后部署）**：重写 11 个图文件 + 仓库 configs → 同步 delta 代码 → 重启
  → 验证严格单格式（带 position 的旧文件被拒）。
- 回滚链：DB 双备份（切换前 + v4 期间）、routing-graph 目录备份、unit 文件备份。

## 8. 测试

- 后端：旧格式（节点含 position）→ `graph_parse_error`；canvas 非对象/非有限数值/未知键/
  引用不存在节点 → 拒绝；缺 canvas 合法；canvas 增删改不影响 `tree_revision`；往返序列化
  字节稳定；document bytes 与前端导出格式一致。
- 前端：导入 canvas→内部 position、导出收集→canvas 的往返一致；缺条目自动布局；拖拽不改变
  behavior canonical（dirty 判定不受影响）。
- 端到端：从 git 历史恢复的画布 QA 模块（canvas/inspector）重注册运行；76 实测
  （画布编辑→保存→重载布局保留；拖拽后 tree_revision 不变）。

## 9. 明确假设

- v4 分支不推送远端、不需要在远端撤销。
- 76 的 v4 策略文件（routing-policy/）无保留价值，直接删除。
- 用户未跟踪文件（configs/cases、PDF、PPT 等）除 configs/cases 按第 6 节重写外均不动。
- revert 提交信息明确记录方向反转原因，v4 设计/计划文档保留为历史证据。
