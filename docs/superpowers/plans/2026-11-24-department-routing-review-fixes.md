# RoutePilot 部门路由审核发现修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复跨图熔断/在途状态串用，以及路由集试算把所选图轨迹错误投影到当前草稿画布的问题。

**Architecture:** 后端在图路由路径统一使用 `tree:<tree_id>` 作用域，逻辑模型 ID 仍用于图执行、响应和账本，作用域键只用于熔断与 inflight；固定模型直连继续使用原裸模型键。前端路由集试算改为不触碰当前画布的结果模式，草稿试算保持原有完整回放。

**Tech Stack:** Python 3.11、FastAPI、httpx、SQLite、原生 ES Modules、Node test runner、Playwright、pytest。

## Global Constraints

- 审核事实源：`0291f99..78b88a2` 的两项 Important 和一项 Minor；本计划只做这些修复，不重写 v4 路由集架构。
- 只修改 `main`，只推送 Gitee：`git push origin main`；不创建功能分支，不推送 GitHub。
- 不接入 APISIX、163、newapi、token-factory，不新增第二阶段内部端点。
- 图内逻辑模型 ID、响应 `x-sr-model`、账本 `model`、调度 trace 的模型名保持不变。只有熔断器与 inflight 的内部键增加作用域。
- Auto 图路由使用 `tree_scope(tree_id) == "tree:<tree_id>"`；`runtime_model_key(scope, model)` 在 scope 非空时返回 `<scope>:<model>`，scope 为空时返回 `<model>`。
- 固定模型直连继续使用 `scope=""`，不能与任何 Auto 图共享熔断或 inflight。
- `RoutingSnapshot.tree_id` 是跨副本稳定作用域；不得使用进程 ID、快照对象 ID、随机数或 `tree_revision`，避免重启/副本间漂移及普通图保存后状态重置。
- 路由集试算不得高亮、重排、修改或 dirty 当前草稿；草稿试算行为逐字保留。
- 不新增依赖，不改变管理 API/数据面 HTTP 契约，不降低现有断言来通过测试。
- 每个任务先运行新增测试并确认因目标缺陷失败，再写最小实现并提交。最终运行完整 Python、Node/Playwright、i18n 和 standalone 13 项冒烟。

## File Map

- `router/upstream/runtime_keys.py`（新增）：唯一的作用域与内部模型键构造函数，无 I/O。
- `router/routing/scheduler.py`：Lease 保存内部键；LoadBalancer 的可用性、选择和 inflight 支持 scope。
- `router/routing/scheduling.py`：从快照 tree ID 推导 scope，并传入每次 selection/acquire。
- `router/upstream/proxy.py`：breaker 成功/失败使用显式 `breaker_key`，与展示模型名分离。
- `router/api/routes_v1.py`：Auto 计算图作用域 breaker key；固定直连传裸模型键。
- `router/upstream/breaker_store.py`、`breaker_worker.py`：不需要结构修改，验证其 TEXT 键能原样同步作用域键。
- `router/console/routing/tryrun.js`：路由集结果模式、原样 ID 展示、画布写操作禁用。
- `router/console/routing/controller.js`：修正生命周期 revision 注释，无行为修改。
- `tests/test_v1.py`、`tests/test_random_scheduling.py`、`tests/test_routing_service.py`、`tests/test_upstream.py`：跨图、固定直连、代理和副本同步回归。
- `scripts/console_qa/canvas.mjs`：不同节点/边 ID 的真实浏览器回归。
- `README.md`、`README.zh-CN.md`、`docs/决策图规范.md`：更新隔离口径。
- `scripts/standalone_v4_smoke.py`：只增加已有冒烟中的作用域键证据，不重写 13 步流程。

## Required Interfaces

```python
def tree_scope(tree_id: str) -> str:
    return f"tree:{tree_id}"


def runtime_model_key(scope: str, model: str) -> str:
    return f"{scope}:{model}" if scope else model


class LoadBalancer:
    def available(self, models: list[str], *, scope: str = "") -> list[str]: ...
    def selection(self, models: list[str], *, scope: str = "") -> dict: ...
    def acquire(self, models: list[str], *, preferred: str | None = None,
                dry_run: bool = False, scope: str = "") -> Lease: ...
    def inflight(self, model: str, *, scope: str = "") -> int: ...


class UpstreamProxy:
    async def send(self, *, backend, body: dict, headers: dict,
                   model: str, breaker_key: str, outcome, release) -> ProxyResult: ...
    async def stream(self, *, backend, body: dict, headers: dict,
                     model: str, breaker_key: str, outcome, release,
                     is_disconnected=None) -> ProxyResult | ProxyStream: ...
```

`Lease` 增加 `runtime_key: str`。`Lease.model` 继续保存逻辑模型 ID，`Lease.release()` 调用 `balancer._release(self.runtime_key)`。

---

### Task 1: 按图隔离熔断与 inflight，并贯通上游代理

**Files:**
- Create: `router/upstream/runtime_keys.py`
- Modify: `router/routing/scheduler.py`
- Modify: `router/routing/scheduling.py`
- Modify: `router/upstream/proxy.py`
- Modify: `router/api/routes_v1.py`
- Test: `tests/test_random_scheduling.py`
- Test: `tests/test_routing_service.py`
- Test: `tests/test_v1.py`
- Test: `tests/test_graph_evaluator.py`
- Test: `tests/test_upstream.py`

**Interfaces:**
- Consumes: 既有 `snapshot.tree_id`、`decision.model`、`Backend` 和裸字符串键的 `CircuitBreaker/BreakerStore`。
- Produces: Required Interfaces 中的 `tree_scope/runtime_model_key`、带 scope 的 LoadBalancer、显式 `breaker_key` 的 UpstreamProxy。

- [ ] **Step 1: 写 LoadBalancer 跨图失败测试**

在 `tests/test_random_scheduling.py` 增加：

```python
from router.upstream.runtime_keys import runtime_model_key, tree_scope


def test_same_model_has_independent_breaker_and_inflight_per_tree():
    breaker = CircuitBreaker(threshold=1)
    balancer = LoadBalancer(breaker)
    main = tree_scope('main')
    finance = tree_scope('finance')

    first = balancer.acquire(['shared'], scope=main)
    second = balancer.acquire(['shared'], scope=finance)
    assert balancer.inflight('shared', scope=main) == 1
    assert balancer.inflight('shared', scope=finance) == 1
    first.release()
    assert balancer.inflight('shared', scope=main) == 0
    assert balancer.inflight('shared', scope=finance) == 1
    second.release()

    breaker.failure(runtime_model_key(main, 'shared'))
    assert balancer.available(['shared'], scope=main) == []
    assert balancer.available(['shared'], scope=finance) == ['shared']
    assert balancer.available(['shared']) == ['shared'], 'fixed direct must stay independent'
```

- [ ] **Step 2: 运行测试，确认缺少作用域接口**

Run: `.venv/bin/python -m pytest -q tests/test_random_scheduling.py -k same_model`
Expected: FAIL；`router.upstream.runtime_keys` 不存在，随后是 `acquire()/available()` 不接受 `scope`。不得删除 `scope` 参数让测试退化为全局状态。

- [ ] **Step 3: 实现唯一键构造与 LoadBalancer 作用域**

新建 `router/upstream/runtime_keys.py`，完整实现：

```python
def tree_scope(tree_id: str) -> str:
    if not isinstance(tree_id, str) or not tree_id:
        raise ValueError('tree_id must be nonempty')
    return f"tree:{tree_id}"


def runtime_model_key(scope: str, model: str) -> str:
    return f"{scope}:{model}" if scope else model
```

`LoadBalancer` 的 `available/selection/acquire/inflight` 增加 keyword-only `scope=''`。内部字典和 breaker 查询使用 `runtime_model_key(scope, model)`，返回的 `available_models/configured_models/registered_models` 仍是逻辑模型 ID。

`Lease` 构造改为：

```python
class Lease:
    def __init__(self, balancer, model, *, runtime_key, dry_run=False, selection=None):
        self.balancer = balancer
        self.model = model
        self.runtime_key = runtime_key
        self.dry_run = dry_run
        self.selection = selection or {}
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        if not self.dry_run:
            self.balancer._release(self.runtime_key)
```

`acquire()` 只在真正取得租约后递增同一个 `runtime_key`；`_release()` 接收已经构造好的内部键，不再二次拼接。inflight trace 仍按逻辑模型名投影：读取内部计数后写回 `{model: count}`。

- [ ] **Step 4: 把 scope 传入 ModelScheduler 的所有分支**

`ModelScheduler.schedule()` 开头计算一次：

```python
from router.upstream.runtime_keys import tree_scope
scope = tree_scope(snapshot.tree.id)
```

以下四处必须全部传入同一个 `scope`：`initial_selection = balancer.selection(...)`、正常 `acquire()`、sticky 失效后的 retry `acquire()`，以及测试/其他调用对 `available()` 的使用。不得只修正常 acquire 而漏掉 initial selection 或 retry。

更新 `RecordingBalancer` 等测试替身签名：

```python
def acquire(self, models, *, preferred=None, dry_run=False, scope=''):
    # 保留原记录逻辑，并把 scope 原样传给 super()
    return super().acquire(
        models, preferred=preferred, dry_run=dry_run, scope=scope)
```

- [ ] **Step 5: 写上游代理与端到端跨图失败测试**

先把测试辅助函数扩展为 `make_admin_chat_app(..., root=None)`，并把它收到的 `root` 传给 `make_app()`。随后增加：

```python
def test_same_model_failure_in_main_does_not_block_finance(tmp_path):
    async def scenario():
        state = {'fail': True, 'calls': []}

        def handler(request):
            if request.url.host.endswith('.test') and request.url.path.endswith('/chat/completions'):
                state['calls'].append(request.url.host)
                if state['fail']:
                    return httpx.Response(500)
                return upstream_ok(request)
            return httpx.Response(200, json={'answers': {}})

        app = make_admin_chat_app(tmp_path, handler, root=direct_graph())
        base = app.state.snapshot_set.default_snapshot
        # 部门图与默认图使用同一直连结构、同一 safe 逻辑模型和同一运行目录。
        tree = tree_from_dict(tree_dict(
            'finance', root=direct_graph(), departments=['财务部'],
            enabled=True, default=False))
        finance = build_snapshot(
            tree=tree, backends=base.backends,
            time_periods=[], jev=JEV_SETTINGS, capabilities=CAPABILITIES,
            compiled=compile_graph(tree.root))
        app.state.snapshot_set = assemble_snapshot_set(
            [base, finance], fingerprint='same-model')

        # 公开默认图持续失败，直到 main:safe 对应的电路打开。
        for _ in range(app.state.breaker.threshold):
            response = await post(app, payload())
            assert response.status_code == 502

        state['fail'] = False
        before = len(state['calls'])
        response = await post(
            app, {**payload(), 'model': 'auto'},
            headers={'x-sr-department': encode_department('财务部')},
            path='/admin/test/chat/completions')
        assert response.status_code == 200, response.text
        assert response.headers['x-sr-tree-id'] == 'finance'
        assert len(state['calls']) == before + 1, 'finance was blocked before its upstream call'

        # 财务图成功不能重置 main 的电路；默认图再次请求仍应在调度前阻断。
        before = len(state['calls'])
        response = await post(app, payload())
        assert response.status_code == 503
        assert response.json()['error']['message'] == 'no_available_backend'
        assert len(state['calls']) == before, 'main circuit should remain open'

    asyncio.run(scenario())
```

公开 `/v1` 只用于默认图；财务部门请求只走已有管理端测试对话并显式 `model:"auto"`。不要给公开 `/v1` 放开部门 Header。

- [ ] **Step 6: 让 UpstreamProxy 显式使用 breaker_key**

`UpstreamProxy.send/stream/_relay` 增加必传 `breaker_key`，所有 `breaker.success/failure` 只使用它；`model` 保留给既有日志/测试语义。`routes_v1.py` 在调用代理前计算：

```python
from router.upstream.runtime_keys import runtime_model_key, tree_scope
scope = '' if fixed_model is not None else tree_scope(snapshot.tree_id)
breaker_key = runtime_model_key(scope, decision.model)
```

Auto 的 `breaker_key` 必须与 ModelScheduler 的 scope 完全一致；固定直连传 `runtime_model_key('', fixed_model) == fixed_model`。把 `breaker_key` 传入非流式、流式及 `_relay` 的成功、4xx/5xx、网络错误和中途断流分支。

- [ ] **Step 7: 更新旧测试的键并验证副本同步**

Auto 测试不能再通过 `breaker.failure('safe')` 打开图路由电路，改为：

```python
key = runtime_model_key(tree_scope('main'), 'safe')
app.state.breaker.failure(key)
assert app.state.breaker.is_open(key) is True
assert app.state.breaker.is_open('safe') is False
```

Auto inflight 断言改为 `balancer.inflight('safe', scope=tree_scope('main'))`；固定模型直连测试保留裸键。不要全局机械替换固定直连用例。

在 `tests/test_upstream.py` 的副本同步测试中使用 `runtime_model_key(tree_scope('main'), 'shared')` 作为故障键，验证另一副本导入后该键打开，而 `tree_scope('finance')` 和裸 `shared` 仍关闭。`BreakerStore/BreakerWorker` 无需修改，测试要证明 TEXT 键原样持久化。

- [ ] **Step 8: 运行后端聚焦测试**

Run:
```bash
.venv/bin/python -m pytest -q \
  tests/test_random_scheduling.py tests/test_routing_service.py \
  tests/test_v1.py tests/test_graph_evaluator.py tests/test_upstream.py
```
Expected: PASS；跨图同模型、sticky retry、fixed direct、SSE 断流和副本同步全部覆盖，旧测试没有通过绕开 scope 而变绿。

- [ ] **Step 9: 提交**

```bash
git add router/upstream/runtime_keys.py router/routing/scheduler.py \
  router/routing/scheduling.py router/upstream/proxy.py router/api/routes_v1.py \
  tests/test_random_scheduling.py tests/test_routing_service.py \
  tests/test_v1.py tests/test_graph_evaluator.py tests/test_upstream.py
git commit -m "fix: isolate model runtime state by graph"
```

---

### Task 2: 路由集试算不得投影或修改当前草稿

**Files:**
- Modify: `router/console/routing/tryrun.js`
- Modify: `router/console/routing/controller.js`
- Test: `scripts/console_qa/canvas.mjs`

**Interfaces:**
- Consumes: 既有 `/admin/try` 的 `tree_id/graph_selection/trace/outcome`，以及当前 `cvTryScope`。
- Produces: `replay.scope === 'draft' | 'route_set'`；路由集结果仅显示原始图/节点 ID，不调用画布 paint 或 wave layout。

- [ ] **Step 1: 把浏览器回归改为不同节点和边 ID**

在 `scripts/console_qa/canvas.mjs` 的 `Task 9 · departments` 用例中，让 `directDocument()` 根据图 ID 生成不同结构：

```javascript
const directDocument = (id, model, departments) => {
  const start = id + '-start';
  const target = id + '-target';
  const edge = id + '-edge';
  return {
    id: id,
    name: id === MAIN ? '默认图' : '财务图',
    departments: departments.slice(),
    timezone: 'Asia/Shanghai',
    sticky: { enabled: false, ttl_seconds: 1800 },
    root: {
      id: 'root', type: 'graph',
      nodes: [
        { id: start, type: 'start', title: id + ' 起始节点' },
        { id: target, type: 'target', title: id + ' 独立目标', priority: 1,
          models: [{ model: model, reasoning: null }] }
      ],
      edges: [{
        id: edge, source: start, source_port: 'out',
        target: target, target_port: 'in_1', condition: null
      }],
      error_fallback: target
    },
    backends: {
      providers: [{
        name: 'local', base_url: mockBase + '/v1', key_env: '', reasoning_format: 'openai'
      }],
      models: [{ id: model, upstream: model, provider: 'local', profile: {} }]
    },
    jev: null,
    jev_window: { input_tokens: 1024, context_tokens: 4096 },
    time_periods: []
  };
};
```

路由集运行前后采集：

```javascript
const canvasState = () => page.evaluate(async () => {
  const { app } = await import('/console/routing/app.js');
  const RTGraph = await import('/console/routing/graph.js');
  return {
    canonical: RTGraph.editableCanonical(app.tree, false),
    dirty: app.changeKind,
    replayNodes: document.querySelectorAll('#cvNodes .is-replay').length,
    finalEdges: document.querySelectorAll('#cvEdges .is-final-edge').length,
  };
});
```

财务图处于当前画布时，未知部门会选择 `main`。断言结果含 `main-target`，不含 `finance-target`；运行前后 `canonical/dirty` 完全不变，`replayNodes == 0 && finalEdges == 0`，且 `#cvTryWavesBtn` 不可见。切回 `draft` 后，当前图画布正常高亮并可按波次整理。

- [ ] **Step 2: 运行浏览器回归，确认错投影与错误布局入口**

Run: `QA_ONLY='Task 9.*departments' node scripts/console_qa/canvas.mjs`
Expected: FAIL；当前实现把 main 的轨迹涂到 finance 画布，结果标签来自 finance，且仍显示按波次整理按钮。

- [ ] **Step 3: 标记 replay scope，并用空图重建路由集结果**

在 `tryrun.js` 增加：

```javascript
function currentScope() {
  const select = byId('cvTryScope');
  return select && select.value === 'route_set' ? 'route_set' : 'draft';
}

function resultNodeName(replay, id) {
  return replay && replay.scope === 'route_set' ? String(id) : nodeName(id);
}
```

`run()` 在发请求前保存 `const requestScope = currentScope()`。收到响应后：

```javascript
const replayGraph = requestScope === 'route_set'
  ? { nodes: [], edges: [] }
  : graphNow();
const rebuilt = rebuild(response.trace || [], replayGraph);
rebuilt.scope = requestScope;
```

空图仍由 `final` trace 事件生成 target/candidate 状态，`response.outcome` 继续提供模型和调度结果。`candidateChips()`、`candidateNote()`、`renderResult()` 中的最终目标和候选名称统一改为 `resultNodeName(replay, id)`，路由集结果不得读取当前图标题。

- [ ] **Step 4: 跳过画布 paint，并禁止按波次整理**

`apply()` 改为只在草稿范围取当前图：

```javascript
const graph = replay.scope === 'route_set' ? null : graphNow();
```

现有 `if (graph) { ...paint... }` 保持不变，因此路由集结果只渲染结论、模型、tree ID、graph selection、波次和 warnings，不修改当前画布。

`renderResult()` 中 `#cvTryWavesBtn` 仅在 `!isReadOnly() && replay.scope !== 'route_set'` 时输出；rerun/clear 保留。`layoutByWaves()` 再加防御：

```javascript
if (state && state.scope === 'route_set') {
  toastNow(t('cv.tryRouteSetLayoutBlocked'), 'warn');
  return false;
}
```

在中英文 i18n 增加 `cv.tryRouteSetLayoutBlocked`，并在 `reset()/clear()/removeReplayClasses()` 后确认不会遗留前一次草稿高亮。

- [ ] **Step 5: 修正 Minor 注释**

`router/console/routing/controller.js:202-205` 改为：启停/设默认不参与 behavior canonical，不改变 `tree_revision`；它们只改变文档字节，因此会改变 `document_sha256` 和 `updated_at`，仍需刷新保存基线。不要修改该函数行为。

- [ ] **Step 6: 运行前端聚焦测试**

Run:
```bash
QA_ONLY='Task 9.*departments' node scripts/console_qa/canvas.mjs
node --test scripts/console_graph_test.mjs scripts/console_draft_test.mjs
```
Expected: PASS；不同节点/边 ID 下结果仍正确，当前草稿 canonical 和 dirty 不变、无跨图高亮、无路由集布局按钮，草稿回放原能力保留。

- [ ] **Step 7: 提交**

```bash
git add router/console/routing/tryrun.js router/console/routing/controller.js \
  router/console/i18n.js scripts/console_qa/canvas.mjs
git commit -m "fix: keep route-set replay off the current canvas"
```

---

### Task 3: 更新隔离文档并执行完整验收

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/决策图规范.md`
- Modify: `docs/superpowers/specs/2026-11-24-department-graph-standalone-design.md`
- Modify: `scripts/standalone_v4_smoke.py`
- Test: `tests/test_v1.py`
- Test: `scripts/console_qa/canvas.mjs`

**Interfaces:**
- Consumes: Task 1/2 的最终行为。
- Produces: 与实现一致的作用域键文档、完整测试和真实进程证据。

- [ ] **Step 1: 更新文档口径**

README 双语把“按模型熔断”改为“Auto 按图与模型隔离熔断和 inflight；固定模型直连独立”。`docs/决策图规范.md` 增加：

```text
tree_scope(tree_id) = tree:<tree_id>
runtime_model_key(scope, model) = <scope>:<model>（scope 非空）或 <model>（固定直连）
```

说明该键只用于 CircuitBreaker/BreakerStore 和 inflight，不改变图文档、tree_revision、响应模型名或账本模型名；同一 scope 在副本间稳定，另一图及固定直连不受故障影响。

在设计文档的“后端状态不得串用”处引用同一规则。控制台文档补充：路由集试算只显示所选图结果和原始 ID，不在当前草稿上回放或布局；草稿模式保留画布回放。

- [ ] **Step 2: 在 standalone 冒烟增加跨图同模型证据**

复用现有 13 步冒烟，不重写流程。在 `main/finance/legal` 之外创建 `twin`：部门为 `镜像部`，逻辑模型与 `main` 同为 `fast`，但 tree ID 不同。使用 mock 上游的可控故障队列让接下来三次 `main/fast` 请求返回 500，随后恢复成功。只通过真实 HTTP 行为和上游调用计数断言：

```python
from urllib.parse import quote

body = {'model': 'auto', 'messages': [{'role': 'user', 'content': '跨图同模型隔离'}]}
for _ in range(3):
    expect(call(APP_BASE, 'POST', '/v1/chat/completions', body),
           502, 'main/fast failure opens only main circuit')

before = len(self.upstream.snapshot())
expect(call(APP_BASE, 'POST', '/v1/chat/completions', body),
       503, 'main circuit remains open')
require(len(self.upstream.snapshot()) == before,
        'open main circuit must block before the upstream')

response = expect(call(
    APP_BASE, 'POST', '/admin/test/chat/completions', body,
    headers={'x-sr-department': quote('镜像部', safe='')}),
    200, 'twin/fast has an independent circuit')
require(response.headers.get('x-sr-tree-id') == 'twin',
        f"twin tree header: {response.headers}")
require(len(self.upstream.snapshot()) == before + 1,
        'twin/fast did not reach the upstream')

before = len(self.upstream.snapshot())
expect(call(APP_BASE, 'POST', '/v1/chat/completions', body),
       503, 'twin success must not reset main circuit')
require(len(self.upstream.snapshot()) == before,
        'main upstream should remain blocked')
```

不要访问被测路由进程的 `app.state`，不要读取或打印任何真实密钥。新增检查放在原有“部门试算通过”之后、“legal 成为默认图”之前；`twin` 在该检查内创建，并在 `finally` 中调用 `POST /admin/trees/twin/disable`，确认后续启用图列表恢复为 `main/finance/legal`，不能影响原空路由集断言。测试完成后保持上游成功并继续原有 stale/replica/cleanup 步骤。

- [ ] **Step 3: 运行完整标准门禁**

Run:
```bash
git diff --check
.venv/bin/python -m pytest -q
npm run check:console
.venv/bin/python scripts/i18n_dead_keys.py
.venv/bin/python scripts/standalone_v4_smoke.py
```
Expected: 全部 PASS；pytest 无失败，Playwright `ALL PASS`，i18n 0 dead keys，standalone 原 13 步及新增跨图同模型检查全部通过。

- [ ] **Step 4: 复核缺陷没有以替代形式残留**

Run:
```bash
git grep -n 'breaker\.\(failure\|success\|is_open\)(model)' -- router
git grep -n 'rebuild(response.trace.*graphNow' -- router/console/routing/tryrun.js
```
Expected: 第一条无 Auto 裸键调用；固定直连显式使用 `breaker_key=fixed_model`。第二条无匹配；草稿分支可以在单独变量中使用 `graphNow()`，路由集分支必须使用空图并跳过 paint。

- [ ] **Step 5: 清理、提交并只推送 Gitee**

删除测试/冒烟新产生的临时数据库、图目录、日志和截图，不删除已有未跟踪文件。

```bash
git status --short --branch
git diff --check
git add README.md README.zh-CN.md docs router tests scripts
git commit -m "test: verify graph isolation review fixes"
git push origin main
```

Expected: `HEAD == origin/main`，没有推送 GitHub，没有临时产物或无关 tracked 修改。

## Completion Checklist

- [ ] 两张图使用同一模型 ID 时，一图熔断和 inflight 不影响另一图。
- [ ] Auto 与固定模型直连不共享内部运行状态键。
- [ ] 调度初次选择、sticky retry、JSON、SSE、中途断流和副本同步使用一致 scope。
- [ ] 路由集试算在不同节点/边 ID 下不涂色、不重排、不 dirty 当前草稿。
- [ ] 草稿试算的画布回放和按波次整理保持原能力。
- [ ] 逻辑模型名、响应头、账本和 trace 的公开词汇没有变成内部作用域键。
- [ ] 全量 pytest、Node/Playwright、i18n、standalone 冒烟及 `git diff --check` 全绿。
- [ ] 只提交并推送 Gitee `main`，未实现任何第二阶段集成。
