# RoutePilot

> Built and maintained by Sandlake · Copyright © 2026 Sandlake. All rights reserved.

English | [简体中文](README.zh-CN.md)

RoutePilot is an OpenAI-compatible model routing proxy. Routing policy is configured on a generic decision graph: questions, logic gates, request conditions and model targets are nodes; comparison conditions live on the input slots of logic gates.

| | |
|---|---|
| Status | Graph v3 runtime (M1 complete, 2026-11-02). The canvas, real `/v1` routing chain, `/admin/try` dry-run and request logging all run on graph v3. |
| Design spec | `docs/superpowers/specs/2026-11-02-generic-decision-tree-design.md` (local only, not published) |
| Console prototype | `docs/od-design/routing-canvas-2026-11-02/index.html` (Direction A · Canvas-first; local only, not published) |
| Config format | `sr-routing-graph` v3. Legacy routing trees are not converted; see [Upgrading a legacy database](#upgrading-a-legacy-database). |

## Decision canvas

![RoutePilot decision canvas](assets/routepilot/canvas.png)

### Nodes

| Type | Purpose | Inputs | Outputs |
|---|---|---|---|
| `start` | Request entry; always true | — | boolean |
| `question` | One decision-service question; `choice` / `noul` / `score` | Boolean slots `in_1..in_n` (OR) | choice: `prob.*`, `selected.*`, `p_top`, `margin`; noul: `yes`, `no`; score: `value` |
| `logic` | Logic gate; `and` / `or` / `not` | Boolean slots `in_1..in_n`; one edge + condition per slot | boolean (three-valued) |
| `condition` | Request condition: time period / text keyword | — | boolean |
| `target` | Model target (model, reasoning tier, priority, least_inflight) | Boolean slots `in_1..in_n` (OR) | — |

### Input conditions

Every input port is a boolean input slot. Conditions are stored on the edge:

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

| Upstream output | Condition | Evaluation |
|---|---|---|
| number | Required: `gt` / `gte` / `lt` / `lte` / `eq` / `neq` + finite threshold | Compare upstream value with threshold; unknown propagates as unknown |
| boolean | Optional: `null` (equivalent to `= true`) or `{ "op": "is", "value": true\|false }` | Boolean expectation |

- One output may feed multiple slots; each slot has its own condition. Two slots of the same AND gate with `> 0.3` / `< 0.8` implement a range condition.
- Each input slot takes at most one edge. Slot IDs `in_1..in_N` are stable; deleted edges free their slot for reuse.
- NOT has exactly 1 slot; AND / OR require at least 2. Multiple slots on question / target are OR.

### Execution

1. Read an immutable RoutingSnapshot, evaluate request conditions, truncate the request text once.
2. The start node activates the first wave of questions; questions in one wave are merged into a single `/v1/systemone` call.
3. After each wave, update numeric ports and `selected`, then evaluate all downstream slot conditions and logic gates.
4. True signals activate further questions and targets; repeat until no new question is activated.
5. When multiple targets are active, pick the smallest priority; fall back to `otherwise_target` when nothing activates, and to `error_fallback` on decision failure.
6. After reaching the target, apply session affinity; select a backend inside the target with `least_inflight`.

unknown never activates a question or target; a slot condition on unknown evaluates to unknown. Enable limits: ≤ 200 nodes, ≤ 400 edges, ≤ 32 questions, ≤ 8 question waves.

### Dry-run replay

![Dry-run replay](assets/routepilot/replay.png)

## Condition editing

![Condition editing](assets/routepilot/condition.png)

- Drag a numeric wire into any boolean slot: a default `> 0.5` condition is created and the editor opens.
- Click a slot condition chip: edit operator and threshold (numeric) or `= true / = false` (boolean).
- Conditions are stored on the edge. Condition edits change the behavior revision; moving nodes does not.
- Selecting an edge or slot shows source, target, line type and condition in the inspector.

## Console prototype

Direction A “Canvas-first” is the confirmed direction. Implemented interactions:

- Drag / click to create nodes; pan and zoom; drag nodes; draw edges with type validation.
- Quick create: drop an output wire on empty canvas to create a compatible node by source type.
- Input slot rows on logic gates / questions / targets: source, condition, live replay value.
- Dry-run 5 samples (grid repair / translation / work order / low-confidence follow-up / timeout) with wave-by-wave replay.
- Validation error navigation, delete confirmation, undo/redo, auto layout, v3 import/export.

Prototype entry points (local, open directly via file://):

- Gallery: `docs/od-design/routing-canvas-2026-11-02/index.html`
- Direction A: `docs/od-design/routing-canvas-2026-11-02/drafts/index-a.html`
- Directions B / C: `drafts/index-b.html`, `drafts/index-c.html`

Validation error state:

![Validation errors](assets/routepilot/errors.png)

## Runtime responsibilities

| Component | Responsibility |
|---|---|
| Graph Executor | Wave-based graph execution: questions, conditions, logic gates, target activation; three-valued state and trace |
| DecisionService client | Calls NeoHorse `/v1/systemone`; strict response validation |
| SessionAffinity | After reaching a target, reuse the model for the same tree revision + target; TTL/LRU configurable |
| LoadBalancer | `least_inflight` backend selection inside a target; count covers the full upstream lifecycle |
| CircuitBreaker | Remove tripped backends; return 503 `no_available_backend` when none is available |

Dry-run uses the same executor without writing affinity, increasing in-flight counts or calling upstream generation models. Current runtime mode is a single uvicorn worker; affinity and in-flight state are in-process.

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

ROUTER_DB=/tmp/routepilot.db .venv/bin/python -m router
```

Default listener: `127.0.0.1:8400`; console: `http://127.0.0.1:8400/console/`. On first production start, a default admin account is seeded into an empty users table; change the password immediately. Set `ROUTER_USER_AUTH=0` for local development to disable `/admin` authentication.

## Upgrading a legacy database

The graph runtime persists the complete decision graph in `routing_trees.root`. A database whose routing trees still contain the pre-graph payload (for example `root.type = "jev"`) is **not converted automatically**. The new version refuses to start, keeps the schema at v1 and reports:

```
legacy routing trees require explicit reset; ... DELETE FROM routing_trees ...: <tree ids>
```

1. Stop the old service.
2. Back up the database:

   ```bash
   sqlite3 router.db ".backup 'router.db-pre-graph-v3'"
   ```

3. Start the new version once. It stops before changing the schema and names the legacy tree ids.
4. Only after confirming that the old routing configurations are no longer needed, delete them:

   ```bash
   sqlite3 router.db "DELETE FROM routing_trees"
   ```

5. Start the new version again. The migration adds `request_logs.waves`, records schema version 2 and keeps existing request-log rows with `waves = NULL`.

The reset deletes the old routing configuration only. Models, providers, users, sessions, decision-service settings and request logs are kept. There is no automatic conversion of legacy trees, so rebuild any policy you still need on the canvas and enable it again.

- The production graph config shipped with this release (`configs/production_routing_tree.json`) replaces the old production policy and retires its `power` and `image` options (both were `qwen3-32b` placeholders). Operators who still need those categories must re-add them as question options and targets on the canvas after upgrading.

## Decision service

The decision service exposes `POST /v1/systemone`. Configure it on the console “JEV Models” page, or seed it via environment variables on first start.

```bash
ROUTER_DB=/tmp/routepilot.db \
ROUTER_JEV_URL=http://127.0.0.1:30104 \
ROUTER_JEV_MODEL=d1-2B \
ROUTER_JEV_API_KEY= \
.venv/bin/python -m router
```

- `ROUTER_JEV_URL` and `ROUTER_JEV_MODEL` must be provided together. Existing database configuration is not overwritten by environment variables.
- Input window: service context budget (`-c / --parallel`) and input truncation limit are configurable. Oversized input is truncated head/tail; no 4xx/5xx is returned.
- Connection test performs a real call to the decision service without changing routing state.

## API

| Path | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible routing entry (non-streaming / SSE) |
| `GET/POST/PUT/DELETE /admin/trees` | Decision group CRUD, enable, copy |
| `POST /admin/try` | Dry-run trial (same executor) |
| `GET/POST /admin/models` | Model catalog and providers |
| `GET/PUT /admin/jev`, `/admin/jev/window` | Decision service config and context window |
| `GET /admin/time-periods` | Named time periods (referenced by request-condition nodes) |

Errors:

- No active tree: 503 `no_active_tree`.
- All target backends unavailable: 503 `no_available_backend`.
- Decision failure / timeout / invalid response: `error_fallback`.
- Upstream 4xx: status and body passthrough; upstream 5xx / network error: 502 and circuit-breaker failure.
- Streaming terminal states: `completed / upstream_error / client_disconnected / cancelled / incomplete`.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ROUTER_DB` | `router.db` | SQLite path |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8400` | Listener |
| `ROUTER_JEV_URL` / `ROUTER_JEV_MODEL` | empty | Seed decision service on first start |
| `ROUTER_JEV_API_KEY` | empty | Optional decision service key |
| `ROUTER_JEV_TIMEOUT` | `10` | Decision service request timeout (s) |
| `ROUTER_SESSION_MAX_ENTRIES` | `10000` | Affinity LRU capacity |
| `ROUTER_USER_AUTH` | `1` in production | Console authentication |
| `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` | seeded on first start | Admin account |
| `ROUTER_SESSION_TTL_HOURS` | `168` | Console session TTL |

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Prototype QA (local):

```bash
cd docs/od-design/routing-canvas-2026-11-02 && node qa.js
```

## Layout

```
router/                  server (FastAPI, routing engine, admin API)
router/console/          console static pages
configs/                 example decision groups
scripts/                 local ops scripts (not committed)
tests/                   tests
docs/                    design docs and prototypes (not committed by convention)
assets/routepilot/       README screenshots
```

---

© 2026 Sandlake. All rights reserved.
