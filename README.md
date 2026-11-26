# RoutePilot

**Natural-language-driven intelligent routing for LLM traffic.**

English | [简体中文](README.zh-CN.md)

RoutePilot is an intelligent routing system built from one repository and shipped as **two deployable services**:

- **`routepilot-core` (decision kernel)** — owns the versioned decision graphs, makes every routing decision (graph execution, decision service/JEV, reasonings, session affinity) and emits one self-contained decision event per real decision. It never calls a model upstream and holds no SQLite database, console or people data. Start it with `python -m router core`.
- **`routepilot-console` (peripheral)** — the optional user-facing service: console, login/people, SQLite configuration catalog, graph-management proxy to the kernel, the standalone OpenAI-compatible `/v1/chat/completions` data plane and the volatile 20-event decision log. Start it with `python -m router peripheral`.

Deploy both for a complete standalone service, or deploy only the kernel as the decision component behind APISIX + newapi (the 163 form). Graphs are edited, validated, dry-run, saved and enabled in the console; every graph request is authenticated by the peripheral and executed by the kernel.

![RoutePilot decision canvas](assets/routepilot/en/routing-canvas.png)

## Features

| Area | Capability |
|---|---|
| Services | Kernel (`python -m router core`) and optional peripheral (`python -m router peripheral`) from one image; the kernel owns the graph JSON and all decisions, the peripheral owns console/SQLite/execution |
| Entry point | Peripheral `POST /v1/chat/completions` (standalone form) and kernel `POST /internal/v1/route-decision` (APISIX form); JSON and SSE; `x-sr-model` / `x-sr-selection` / `x-sr-tree-id` / `x-sr-graph-selection` / `x-sr-reasoning*` response headers |
| Routing policy | Visual decision graph: conditions, decision-service questions, logic gates, model targets |
| Route set | Several decision graphs can be enabled at once; exactly one default graph serves public `/v1`, and department mappings let admin checks target a specific enabled graph |
| Decision service | `choice` / `noul` / `score` questions, batched per wave, sent to a `systemone`-compatible service; the canvas snapshot fixes which service profile a graph runs with |
| Target selection | Priority competition among activated targets; one fallback target `error_fallback` |
| Backend selection | Session affinity; random pick among a multi-model target's available models; Auto isolates circuit breaking and inflight per graph+model; fixed-model direct stays independent |
| Portable canvases | Every saved v4 canvas embeds its lifecycle markers, departments and complete non-secret run configuration, so one JSON file restores it on an empty database |
| Check routing | `/admin/try` executes the same graph and trace format without touching affinity, inflight counters or upstream models; it runs either the current draft or the active route set (optionally selected by department), and database drift is reported as hints only |
| Operations | Volatile 20-event decision log with eviction/replay, confidence distribution, model/provider catalog, non-secret multi-service JEV catalog, time periods, audit history, role-based users |
| Console | Bilingual (zh/en) native ES Modules, no build step |

## Request chains

**Standalone (kernel + peripheral).** The peripheral answers the OpenAI-compatible endpoint and asks the kernel for exactly one decision:

```text
client
  → peripheral POST /v1/chat/completions
  → kernel   POST /internal/v1/route-decision   (no user Authorization)
  → peripheral rewrites the request model to the kernel's real model
  → peripheral resolves the Provider Key from its own SQLite catalog
  → model upstream
  → peripheral passes JSON / SSE through

kernel decision event → peripheral POST /internal/v1/decision-logs → in-memory deque(maxlen=20)
```

**Kernel behind APISIX + newapi (163 form).** APISIX keeps the original newapi chain and only asks the kernel which model to run:

```text
client
  → APISIX Auto Route (model == "auto")
  → forward-auth: original Authorization is validated by newapi, which injects the trusted department
  → sr-auto-router asks kernel POST /internal/v1/route-decision WITHOUT Authorization
  → APISIX rewrites only the JSON body `model` (and Content-Length)
  → the original newapi upstream/plugins/Authorization execute that model
  → APISIX passes JSON / SSE through
```

Fixed-model requests bypass the kernel entirely. If no valid decision is produced, APISIX returns 503 and never sends `auto` to newapi. The kernel never becomes a model upstream.

Steps inside every decision (both forms, executed by the kernel):

1. Pick the graph: an enabled graph by trusted department, else the default graph.
2. Load that graph's compiled snapshot and evaluate request conditions (time period, text keywords).
3. Activate the first question wave; all questions in one wave are merged into one decision-service call.
4. Write numeric and `selected` ports, then re-evaluate downstream slot conditions and logic gates.
5. Repeat wave by wave until no further question is activated.
6. Select the highest-priority activated target; otherwise use `error_fallback`.
7. Apply session affinity; on a miss, pick one of the target's available models at random.
8. Emit one self-contained decision event (best effort) with the full graph and trace.

`unknown` never activates a question or target. A numeric comparison against an unknown value stays unknown.

## Architecture

| Layer | Path | Responsibility |
|---|---|---|
| Kernel app | `router/api/core_app.py`, `router/api/routes_decision.py` | `create_core_app()`: graph management `/internal/v1/trees*`, decision `/internal/v1/route-decision`, atomic batches `/internal/v1/graph-batches`, `/healthz`, `/readyz`; no SQLite/console/upstream |
| Peripheral app | `router/api/app.py`, `router/api/routes_core_proxy.py` | `create_app()`: console, login/people, catalog APIs, graph-management proxy to the kernel, standalone `/v1` data plane, decision-event receiver |
| Cross-service clients | `router/core_client.py`, `router/api/routes_v1.py`, `router/upstream/execution.py` | Kernel decision/management HTTP client; peripheral execution catalog join (decision model + Provider Key) |
| Decision events | `router/decision_events.py`, `router/decision_log_sink.py`, `router/decision_log_store.py` | Self-contained event schema/validation, kernel best-effort async sink, peripheral 20-event memory log and replay projections |
| API | `router/api/` | DTO parsing, exception-to-HTTP mapping, presenters |
| Control plane | `router/control/` | Graph write use cases (kernel side), catalog writes with cross-service atomic graph batches and compensating rollback, impact lists, enable validation |
| Routing domain | `router/routing/` | Graph model, validation, compiled topology, evaluator, scheduler, immutable route-set snapshots |
| Decision service | `router/jev/` | `systemone` client, service settings (non-secret), input window |
| Upstream | `router/upstream/` | Fixed-model catalog (`model_id -> Backend`), peripheral proxy transport and breaker, execution-catalog resolution |
| Storage | `router/store/` | Peripheral SQLite schema/repositories, kernel graph-file repository |
| Console | `router/console/` | Bilingual native ES Module UI (peripheral-only) |
| Runtime entry | `router/__main__.py` | `python -m router core|peripheral` |

Boundaries:

- The kernel is the only writer of the graph JSON directory (global design constraint); the peripheral never mounts or writes it. The peripheral is the only writer of its own SQLite database.
- `router/routing/` and `router/upstream/` import neither FastAPI nor SQLite. `router/control/` and `router/store/` do not import `router.api`; the kernel app imports no `router.store.db`.
- The kernel app overrides the `get_conn` dependency with `None`, so the same graph routes run without any SQLite connection; audit rows and catalog drift hints are peripheral-only concerns injected through the proxy.
- The peripheral's API layer parses DTOs and maps exceptions; it does not write graph state. Account and session routes (`/admin/login`, `/admin/logout`, `/admin/users`, `/admin/me`) call `UserRepo`/`SessionRepo`/`ChangeRepo` directly.
- `GraphService` (kernel) validates, prepares the candidate route set, writes lifecycle documents under the single-writer lock, re-checks the route fingerprint, publishes the complete enabled set, then records the audit entry. A stale draft returns 409.
- A catalog write that can touch graphs commits in two phases: the peripheral re-checks the impact list, asks the kernel to atomically replace every confirmed canvas in one conditional batch (`POST /internal/v1/graph-batches`: baseline check → strict validation → route-set validation → one commit/publish), then commits its own SQLite rows. If the SQLite commit fails after the kernel committed, the peripheral compensates with a reverse batch built on the new base hashes; a failed compensation is a hard 503 `catalog_sync_diverged` (never a silent partial update). A write that synchronizes at least one enabled graph republishes the whole enabled set; a draft-only sync publishes nothing; a stale confirmation returns 409 `graph_conflict` before any write.
- A catalog write that nothing references writes rows and audit only. It never rewrites canvases and never republishes the snapshot.
- `compile_graph()` runs once per enabled graph in a published route set. The kernel request path reads `snapshot.require_compiled()` and never rebuilds indexes or opens SQLite.
- Each real kernel decision builds one self-contained event and hands it to a bounded async queue (`Queue(1000)`): best-effort HTTP delivery with a short timeout, drop counters for observability, no disk retry and no effect on the decision response. `logging.type=disabled` makes the sink a no-op.
- Session affinity and kernel scheduling/inflight counters are process-local; the peripheral has its own execution-side circuit breaker for upstream failures.

## Console

![Test](assets/routepilot/en/test.png)

One `<script type="module">` entry per page, no application globals, no CommonJS. The console is served by the **peripheral** only (`/console/`); the kernel process has no static mount.

| Page | Content |
|---|---|
| Test | Admin-only model test chat: Auto-only smart routing through the active route set (default graph, or a trusted department header), SSE streaming, in-memory multi-turn history, image/text/audio/file attachments, safe multimodal rendering |
| Decision groups | Canvas editor, multi-graph lifecycle (enable/disable, set as default, default marker), departments in graph settings, layout and runtime settings (timezone/sticky/fallback, JEV selection, refresh runtime from the database catalog), inspector, validation, routing checks, import/export |
| Calc Log | Recent decisions with model, target, candidates, waves, JEV probabilities, reasoning, decision-only latency, replayable trace |
| Decision service | Multi-service JEV catalog: service list, create/edit/delete, per-service connection test, prompts, timeout and the per-service context/input window |
| Models | Model registry and provider catalog; Connect models onboards a catalog provider and its models in one save. Edits and deletes that affect saved canvases preview the impact list and synchronize only after confirmation. The preset dialog's Advanced holds only connection details and slot IDs; reasoning declarations, tiers, domains and provider protocol belong to the custom form |
| Users | Admin/viewer accounts, passwords, roles, status |

### Common paths

- The decision-group toolbar shows every editing action directly: undo/redo, auto-layout, **Save**, New, Import, Export and (for an enabled group) **Disable**, next to **Check routing**, the primary button and the issue counter. Saving is the standalone **Save** button; the primary button only expresses enablement — **Enable** for a new or saved disabled group, **Enabled** (disabled) for an enabled group, **Saving…** while a save or enable is in flight. The core version operations are **Save** and **Enable**: saving an enabled graph takes effect for later requests immediately, saving a disabled draft leaves the running route set untouched, and enabling a graph with unsaved edits saves it first. **Disable** and **Set as default** are separate lifecycle controls. Several groups can be enabled at once and the selector marks the default graph; an enabled non-default group also offers **Set as default**. **Disable** is blocked for the default graph while another enabled group remains — switch the default first. Departments are edited in graph settings. The inspector's **Apply** only updates the local draft and never writes to the server.
- Graph settings (inspector) select the JEV service from the database catalog and apply **JEV + its window** in one history step; **Refresh runtime config from database** rebuilds the draft's `backends`/`jev`/`jev_window`/`time_periods` from the catalog. Neither action writes the server.
- On the Models page, **Connect models** is the default entry and a preset catalog onboards the provider and its models in one save. Model IDs are generated automatically. The preset dialog's Advanced holds only connection details and slot IDs; manual IDs, reasoning declarations, tiers, domains and provider protocol settings live in the custom form's Advanced.
- The **Decision service** page is a catalog: the left side lists services, the right side edits the selected one (address, model, timeout, instructions, state template, input/context window) and tests it; the page has **no key input** — the JEV key lives only in the kernel environment. The window belongs to the service, so a canvas always replaces JEV and window together. The catalog stores no key material: the **Test** button proxies the non-secret service description to the kernel, which probes the JEV endpoint with its own `ROUTER_JEV_API_KEY` key. The environment can create a `default` catalog entry from `ROUTER_JEV_URL` / `ROUTER_JEV_MODEL` (no key), but only once: it is seeded on a start where the catalog is empty, no environment seed has ever run, and the URL and model are both set. A first boot without them followed by a boot with them still seeds once; deleting every service and restarting never re-creates it.
- Editing or deleting a model, provider or JEV service that a canvas references raises a two-step confirmation; a resource that no canvas references is written immediately. The first write returns 409 with every affected canvas (name, id, enabled, current `document_sha256`, usage); confirming sends those exact hashes back and the server synchronizes only the affected canvases. Model/JEV deletes that are still referenced need a replacement; a provider that a database model or a canvas model still references cannot be deleted at all. After a confirmed sync, reload every already-open canvas (or use **Refresh runtime config from database**) — open drafts are not updated in place.

### Model test chat

The home page (`/console/index.html`) is an admin-only model test chat. Every request is Auto-routed to `/admin/test/chat/completions`, and the message metadata names the upstream model that actually served it (`Auto → <model>`) using the `x-sr-model` response header.

- **Auto smart routing** — evaluates the active route set exactly like `/v1/chat/completions`: the default graph, or an enabled graph selected by the optional **Department** field (`x-sr-department`, percent-encoded). The decision graph, JEV waves, sticky and random backend selection all apply. The page offers no fixed-model bypass, so test traffic always takes the real routing path; fixed-model direct calls remain available only through `POST /admin/test/chat/completions` (`x-sr-selection: direct`, resolved through the database catalog, no graph headers). The reply metadata rendered by the page comes from the `x-sr-model` header; the response headers also carry `x-sr-tree-id` / `x-sr-graph-selection` for the graph that served an Auto reply, but the page does not render those two headers.
- **Streaming** — answers stream over SSE; Stop aborts the stream and keeps the partial answer, and Copy / Retry / Regenerate act on the message. A stream counts as complete only when the upstream ends it cleanly — an early EOF is marked incomplete and a provider error is shown inline in the conversation, never silently passed off as a complete answer.
- **Multi-turn history** — the in-memory transcript is resent as wire-format `messages`, replayed losslessly from the raw assistant message (message-level media renders but never re-enters history); New Chat aborts any in-flight reply, clears the conversation and rotates the session id.
- **Attachments** — text files are read as text and sent as content parts; images, audio and other files are read to data URLs. A file whose estimated size would break the 10 MiB request limit is rejected inline before it is read, and the exact whole-body size (history + Base64) is checked again before sending.
- **Multimodal output** — text renders as safe Markdown; images, audio, video and files use native players or download links; unknown parts stay folded as inspectable JSON. Model-provided HTML is shown as text and active URL schemes are dropped, so nothing executes.
- **Memory only** — conversations and attachments live in the page; a refresh clears them, and the server stores neither chat history nor uploaded files.

### Editor and execution zone

The routing page is split into two zones.

**Editor zone** — canvas, node library, inspector, validation:

- Drag nodes from the library and pan/zoom; auto-layout and undo/redo sit directly in the toolbar.
- Typed wires; a numeric output dropped into an input slot creates a default condition.
- Each input slot is an OR input with its own edge condition.
- Dropping an output onto empty canvas opens a picker of compatible node types; choosing one creates the node there and connects it automatically (Esc cancels).
- The core version operations are **Save** and **Enable**. **Save** writes the draft without changing enablement: saving an enabled graph makes the saved version serve later requests immediately, saving a disabled draft does not touch the running route set, and enabling a graph with unsaved edits saves it first. The primary button only expresses enablement — **Enable** for a new or saved disabled group, **Enabled** (disabled) for an enabled group, **Saving…** while a save or enable is in flight. The lifecycle badge has exactly four states — Running, Unsaved changes, Disabled, Unsaved draft. A saved but disabled group reads Disabled even with local edits (disabled wins), and Unsaved draft is reserved for a group that was never saved. The graph selector marks the default graph; an enabled non-default group shows **Set as default**, and **Disable** is refused for the default graph while other enabled graphs remain.
- Graph settings hold the **Departments** mapping (comma or newline separated). A department may belong to only one enabled graph; disabled drafts may overlap, and enabling a conflicting graph is rejected with a `department_conflict` issue. The inspector's **Apply** commits to the local draft only. Everything that writes to the server lives in the toolbar: **Save** writes the draft (an enabled graph's save republishes the complete enabled route set), **Enable** switches the graph into the active route set after saving pending edits, **Disable** flips the enabled state, and **Set as default** switches the default graph atomically. There is no separate user-facing "publish" or "save and update" action.
- Target cards show display name, model list, fallback role and a random-pick hint for multi-model targets.

**Execution zone** — docked below the canvas:

- Input field, sample prompts, run button, and a **Check routing** scope selector: the current draft, or the active route set with an optional department. A route-set check renders only the selected graph's result and raw node/target IDs, and never replays, paints or lays out the current draft; a draft check keeps the canvas replay.
- Result: final target, model, candidates with priorities, selection reason, waves and call count. Route-set checks also report the selected `tree_id` and whether it was chosen by department or as the default.
- Execution trace: one card per wave with hit conditions, plus the full request timeline.

A draft routing check highlights the winning path wave by wave and can arrange the nodes by waves. A route-set check leaves the canvas untouched because its result describes the active route set, not the current draft — the result panel is the only evidence. Selecting a trace row pans the canvas to the corresponding edge or node; editing entry points are the canvas and inspector.

![Routing-check replay](assets/routepilot/en/try-run.png)

### Decision log

![Calculation log](assets/routepilot/en/logs.png)

The decision log is the peripheral's **volatile in-memory ring**: the kernel emits one complete `routepilot.decision` event per real decision (best effort), the peripheral validates it and keeps only the newest 20 events (`deque(maxlen=20)`). Nothing is written to SQLite, nothing survives a restart, and evicting or restarting is final — the 21st event drops the oldest one and its replay then returns 404. There is no graph-snapshot table and no ledger writer any more.

Each event is self-contained: `schema_version` (`1`), `event_type` (`routepilot.decision`), `id` / `ts`, the request facts (text, requested model, trusted department, session hash), the **complete graph document actually used** (non-secret run configuration included), the decision values (status, logical/real model, target, graph/path selection reasons, fallback, stickiness, policy path, reasoning, timings), the JEV facts and answers, the full replay trace and node labels. It carries no Provider key, no JEV key, no `Authorization` and no graph-snapshot IDs; `key_env` names are allowed because they are not secrets. Only decisions are recorded — never newapi channel/billing, model-upstream state or token usage.

Expanded rows show the calculation stages in order: input preparation, initial computation, per-wave JEV, candidates and final pick, model scheduling. Latency is decision time only — entering the kernel to selecting the model — and never includes upstream generation; upstream status and end-to-end totals stay in the gateway data plane and out of the routing console. A `routing_error` event keeps the partial process up to the failure point and still embeds the selected graph; requests rejected before graph selection (bad parameters) or with no current graph produce no replayable event.

Replay pages keep working exactly as before: `routing.html?request=<decision id>` loads the event's own `graph` and `trace` through the existing `controller.loadTree()` / `rebuildTryRun()` path. Because the graph is embedded in the event, editing or deleting the current graph later does not change a retained replay. The page sends the list request to `GET /admin/requests/recent` (summaries only — a 20-event list never ships 20 full graphs) and the single replay to `GET /admin/requests/{id}/replay`.

### Decision events and logging configuration

The kernel decides the sink through its `config.json`:

- `logging.type = "routepilot"` (default for the standalone form): send events asynchronously to `logging.url`, e.g. `http://console:8400/internal/v1/decision-logs`, with `timeout_ms` (default 1000). Delivery is best effort: bounded queue, short timeout, drop counters for observability, failures never affect the decision response.
- `logging.type = "disabled"`: explicitly produce no event output (the kernel-only 163 deployment uses this). Decisions keep working and no event copies are kept inside the kernel.
- VictoriaLogs is **not implemented** in this period; the event schema is fixed so a future `logging.type=victorialogs` adapter can serialize the same objects as JSON Lines.

## Decision graph

### Nodes

| Type | Purpose | Inputs | Outputs |
|---|---|---|---|
| `start` | Request entry, always true | — | boolean |
| `question` | Decision-service question: `choice`, `noul`, `score` | Slots `in_1..in_n` (OR: any true slot activates; the type comes from the incoming edge's source port) | choice: `prob.*`, `selected.*`, `p_top`, `margin`; noul: `yes`, `no`; score: `value` |
| `logic` | Three-valued gate: `and`, `or` | Slots `in_1..in_n`, one edge condition per slot, at least two | boolean |
| `condition` | Request condition: time period or text keywords | — | boolean |
| `target` | Model target: models, reasoning tiers, priority (random across multiple models) | Slots `in_1..in_n` (OR) | — |

### Conditions

Conditions live on edges:

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
| number | `gt` / `gte` / `lt` / `lte` / `eq` / `neq` + finite threshold | Value comparison; unknown propagates as unknown |
| boolean | `null` (equivalent to `= true`) or `{ "op": "is", "value": true \| false }` | Boolean expectation |

- Slots do not declare a type: validity depends only on the source port (number vs boolean). A numeric output can feed any slot as long as the edge carries a comparison.
- One output can feed multiple slots; every slot keeps its own condition.
- Two slots of an AND gate can express a range, e.g. `> 0.3` and `< 0.8`.
- Each input slot accepts one edge. Slot numbers are stable; released slots are reusable.
- `and` / `or` require at least two slots. Question and target slots are OR (true activates; unknown does not).
- Use boolean-output negation or the complementary numeric comparison for the opposite test; there is no standalone NOT node.

### Execution rules

- Questions execute in waves; one wave is one decision-service call. A question executes at most once per request.
- A failed wave writes no ports and discards **every** candidate accumulated by earlier waves, then falls back.
- Target priorities are unique; the larger number wins. Ties in candidate ordering break on target id.
- `error_fallback` handles a normal run with no activated target, and a decision call that fails, times out or returns an invalid answer.
- The fallback reference must point to an existing target.

### Limits

| Item | Limit |
|---|---:|
| Nodes | 200 |
| Edges | 400 |
| Question nodes | 32 |
| Question chain depth | 8 waves |
| Options per choice question | 255 |
| Score criteria | 2–10 finite values |

### Graph document

A v4 document is a complete, portable canvas: its lifecycle markers (`enabled`/`default`), its department mapping (`departments`), the graph and its four required run fields. Lifecycle is persisted inside every document — there is no `current` pointer any more. New and copied documents are always saved with `enabled: false, default: false`, and importing only replaces the draft (it never changes lifecycle): saved as a new group an imported document is stored closed, while saving it into an existing group inherits that group's lifecycle (an enabled group stays enabled). The first graph enabled through the lifecycle API automatically becomes the default graph.

```json
{
  "kind": "sr-routing-graph",
  "version": 4,
  "name": "Example canvas",
  "departments": [],
  "enabled": false,
  "default": false,
  "timezone": "Asia/Shanghai",
  "sticky": { "enabled": true, "ttl_seconds": 1800 },
  "root": {
    "id": "root",
    "type": "graph",
    "error_fallback": "safe-target",
    "nodes": [
      { "id": "start", "type": "start", "title": "Start" },
      { "id": "safe-target", "type": "target", "priority": 1,
        "models": [ { "model": "qwen3-8b", "reasoning": null } ] }
    ],
    "edges": [
      { "id": "e-start-safe", "source": "start", "source_port": "out",
        "target": "safe-target", "target_port": "in_1", "condition": null }
    ]
  },
  "backends": {
    "providers": [
      { "name": "local", "base_url": "http://127.0.0.1:8000/v1",
        "key_env": "LOCAL_API_KEY", "reasoning_format": "openai" }
    ],
    "models": [
      { "id": "qwen3-8b", "upstream": "qwen3-8b", "provider": "local", "profile": {} }
    ]
  },
  "jev": null,
  "jev_window": { "input_tokens": 1024, "context_tokens": 4096 },
  "time_periods": []
}
```

Only `sr-routing-graph` v4 is accepted. Version-3 and version-2 documents, non-v4 `kind` values, unknown node types and unknown envelope/node/edge fields are rejected before they replace a draft or a published snapshot; so is a document that carries only some (or none) of the four run fields, and so is `default: true` without `enabled: true`. `departments` is a hard string array: each entry is trimmed, 1–64 code points, free of control characters, unique within the document, and the server stores it sorted; a department may belong to only one **enabled** graph, while disabled drafts may overlap. Parse failures (unknown fields, wrong types, missing keys, invalid departments) surface as `graph_parse_error`; structural problems such as start count, ports/slots, cycles, reachability, priorities and the fallback carry their own Issue codes instead of being folded into a parse error. The console runs the same validation before applying an import. `jev: null` is always structurally legal; a canvas with question nodes is rejected at enable time with `decision_service_unavailable`.

Lifecycle rules live in [Multi-graph route sets](#multi-graph-route-sets): exactly one default graph while any graph is enabled, zero enabled graphs is a legal empty route set, disabling the default graph while others stay enabled returns `409 default_graph_required`, and departments are unique across enabled graphs only.

See the [decision-graph specification](docs/决策图规范.md) for the full field tables, issue codes and execution details (Chinese).

This experimental version does not accept legacy NOT graphs. Any `logic/operator=not` is rejected without migration, and historical NOT snapshots are not supported. Rewrite the condition manually: `> 0.65 → NOT` becomes `<= 0.65`, and `>= 0.5 → NOT` becomes `< 0.5`.

The four run fields are described in [Self-contained canvas JSON](#self-contained-canvas-json).

`tree_revision` is the SHA-256 of canonical JSON over `{departments, timezone, sticky, root}` plus the four run fields, with nodes sorted by id, edges by `(target, slot, source, source_port)` and departments by code point. Python and the console produce the same canonical form (departments are ordered by Unicode code point on both sides and numbers render as Python's float repr); the console uses it to classify edits, and `tree_revision` is computed by the server. Display-only changes and lifecycle changes do not affect it: node titles, positions, node/edge array order, edge ids, `id`, `name`, `enabled` and `default` are all excluded. Not every list is order-insensitive: departments are sorted and order-insensitive, but the order of question options, score criteria, `target.models`, catalog providers/models and time periods **does** participate. Question-option `capability` (choice questions) and the model-profile capability and declared reasoning also participate, because they change runtime behaviour.

## Multi-graph route sets

Every decision group is one v4 JSON file carrying `departments`, `enabled` and `default`; files on disk are the only lifecycle source (there is no `current` pointer). The **enabled** graphs form the published route set:

- **Snapshot set.** All enabled graphs are validated and compiled together on load. Any invalid enabled graph skips the whole publish: a previously published route set keeps serving and `/readyz` reports `stale: true` with `last_load_error`; if no publish ever succeeded, `/readyz` is not ready (503).
- **Unique default.** While any graph is enabled there is exactly one default graph. The first graph enabled through the API automatically becomes the default; later enables keep it. `POST /admin/trees/{id}/default` switches the default atomically (the old default stays enabled but stops being default). Disabling the default while another enabled graph remains returns `409 {"detail":{"code":"default_graph_required","tree_id":"…"}}`; disable the last enabled graph and the route set becomes empty — `/readyz` is still ready, and `/v1` answers `503 no_active_tree`.
- **Departments.** `departments` is a normalized, sorted string array per graph (trimmed, 1–64 code points, no control characters, unique within the file). A department may belong to only **one enabled** graph; disabled drafts may overlap and are only rejected when they would both be enabled (`422 department_conflict` with `department` and `tree_ids`). Department matching is exact and case-sensitive; a request whose department matches no enabled graph uses the default graph.
- **Default-only public data plane.** `POST /v1/chat/completions` always uses the default graph and rejects any `x-sr-department` header with `400 department_header_not_allowed`. Department selection is an admin verification surface: the test chat sends a trusted percent-encoded `x-sr-department` header, and the routing check has a **Published route set** scope with an optional department. Auto routing reports the chosen graph through `x-sr-tree-id` and `x-sr-graph-selection` (`department` / `default`); fixed-model direct calls keep `x-sr-selection: direct` and emit no graph headers.
- **Lifecycle is server state.** New and copied documents are always stored as `enabled: false, default: false`. Importing only replaces the draft: saving it into a new group stores it closed, while saving it into an existing group inherits that group's lifecycle. Only the dedicated endpoints change lifecycle; an ordinary save may change `departments` but can never write `enabled`/`default`.
- **Route-set revision.** `snapshot_revision` is the SHA-256 of the canonical `{default_tree_id, department_tree_ids, per-graph snapshot revisions}`; `route_fingerprint` covers the enabled ids, their default flags and their document hashes, so draft-only edits never reload anything. Both are exposed with `default_tree_id` and `enabled_tree_ids` in `graph_status` (the `/readyz` object and every tree-list response).

## Models and reasoning

![Models](assets/routepilot/en/models.png)

A model slot used by the graph maps to an upstream model id and a provider. A target can hold several slots; when more than one is available, one is picked at random. The **catalog** (peripheral SQLite) is the management source for the console dropdowns and for fixed-model direct requests; each **enabled graph's embedded canvas run configuration** is the only source for the kernel decision. In the APISIX form the kernel only returns the real model name; newapi executes it. In the standalone form the peripheral joins the kernel decision with its own catalog and Provider Key through `router/upstream/execution.py` — a model missing from the execution catalog, or a catalog provider/base URL that drifted from the kernel decision, returns 503 `execution_catalog_mismatch` without calling any upstream.

Providers define the OpenAI-compatible base URL, API-key environment variable and reasoning dialect. Model profiles declare:

- reasoning support and default level;
- level-to-wire mapping for `off`, `on`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`;
- capability notes and catalog metadata.

Reasoning dialects: OpenAI, DeepSeek, Qwen, Qwen chat template, OpenRouter, Z.ai, Together, string-thinking, Ant Ling. An unsupported requested level is forwarded without reasoning injection and the response carries `x-sr-reasoning-warning`.

`GET /admin/models` returns `profile` as a JSON object; the database stores it as JSON text. The console and the admin catalog endpoints always read SQLite — they never project an enabled graph's canvas, and a catalog write never rewrites a canvas on its own. Saving a canvas **selects** models: the chosen model and its provider are added to the snapshot; a model with the same ID already in the snapshot is not overwritten by normal target editing (drift is reported by the check hints, and replaced only by an explicit refresh or a confirmed catalog sync).

Legacy database profiles are still read leniently as a sanitized projection: `api_key`, `authorization`, `key_present`, `key_source` and their case/separator variants are removed recursively from `profile`. This is read-only — legal metadata (`tier`, `domains`, `capability`, …) is preserved and the database row is never rewritten. Graph files and management writes still reject those fields rather than cleaning them.

Non-JSON-compatible numbers are the one exception: management writes reject `NaN` / `Infinity` with 422, and a legacy row that already stores one is **not** tolerated by the catalog response — `GET /admin/models` and every other model echo return 500, and a confirmed catalog sync that has to re-project that row returns 422. The `/admin/try` canvas-vs-catalog drift check is JSON-safe: it normalizes non-finite values inside its hint parameters and keeps answering, so routing checks are unaffected. Fix such rows before upgrading — see [Legacy profiles with non-finite numbers](#legacy-profiles-with-non-finite-numbers).

Deleting or updating a model or provider that a saved canvas references is a two-step operation: the first write returns 409 `impact_confirmation_required` with every affected canvas and its current `document_sha256`; the confirmed retry synchronizes only those canvases. Deleting a model still referenced by a target requires a replacement model; deleting a provider that a database model or a canvas model still references is always rejected (`provider_referenced`) — release the references first. See [Catalog impact confirmation](#catalog-impact-confirmation).

A provider API key never appears in a response: the projection exposes `key_present` and `key_source` (`stored` / `env` / `none`) only.

## Decision service (JEV)

![Decision service settings (JEV)](assets/routepilot/en/jev.png)

The decision service exposes `POST /v1/systemone`; typical deployments use NeoHorse or vLLM. The kernel calls the service configured by the selected graph; the console page manages a **non-secret catalog of services**, not a single global configuration:

- The list shows every service with its ID, model and endpoint. Create, edit and delete are per service; the window (`input_tokens` / `context_tokens`) belongs to the service together with its prompts and timeout. The catalog stores **no key material** — `api_key` is an unknown field (422) and the old `key_present`/`key_source` states were removed with the v7 migration.
- **The JEV key belongs to the kernel.** The kernel reads it only from the environment variable named by `config.json` `jev.api_key_env` (default `ROUTER_JEV_API_KEY`) and passes it directly to the JEV endpoint. The peripheral and its SQLite never see it, and there is no key input in the console.
- **Test** calls `POST /admin/jev/{id}/test` with the catalog service configuration: the peripheral proxies the **non-secret** description to the kernel (`POST /internal/v1/jev-test`), and the kernel performs the probe with its own key. A split deployment can therefore test connectivity from the console, and the key never reaches the peripheral process or its SQLite.
- A canvas selects one service (`jev.id` + `key_env`) and embeds its complete non-secret profile plus its window; `key_env` is kept as a schema-compatible environment-variable name and the catalog projection fixes it to `ROUTER_JEV_API_KEY`. JEV and window are replaced together whenever a canvas selects, refreshes or receives a server-side sync — mixing two services is not representable.
- Deleting a service that a canvas references requires a replacement service, and synchronizes the affected canvases (JEV profile + window) after confirmation. Updating a referenced service is also confirmed and synchronized the same way.

## Access and operations

- Email/password login, scrypt hashing, opaque bearer sessions — all in the peripheral. The kernel exposes no login, people, session or `/admin/*` API.
- `admin` can edit graphs (through the kernel proxy), models, providers, JEV services and users; `viewer` is read-only.
- The last active admin cannot be demoted, disabled or deleted.
- User and configuration changes are recorded in the peripheral audit log.
- Graph files are content-addressed by SHA-256 for save conflicts and reload checks; the graph store keeps no version history — replacing the in-memory route set is not a persisted release. Historical replay instead uses the graph embedded in each retained decision event.
- Peripheral control-plane state is SQLite (WAL): people, sessions, providers, model catalog, non-secret JEV catalog, time periods and change records only. Routing graphs are JSON files owned by the kernel.
- Session affinity and kernel scheduling/inflight counters are process-local; the peripheral execution breaker is process-local too.

## Graph storage (kernel-owned)

Graphs are stored **only** as JSON files, never in SQLite, and only the kernel writes them. Each decision group is one v4 document at `<graphs.directory>/graphs/{tree_id}.json`; lifecycle comes from the document's own `enabled`/`default` fields — the old `current` pointer is neither read nor written, and there is no database fallback. Every document **must** carry its own run configuration (`backends`, `jev`, `jev_window`, `time_periods`), because a saved canvas is the only runtime source for a decision. The directory comes from `config.json` `graphs.directory` (`/data/graphs` in the standalone compose form, `/data/graph` on the 163 PVC — the existing persisted path is kept).

- **Single writer: the kernel.** The peripheral never mounts or writes the directory; every graph change is proxied over HTTP to `/internal/v1/trees*`. A process manager must run exactly one kernel writer against one graph directory (the 163 Deployment uses `Recreate` and retires the old single-service workload before starting the kernel).
- The kernel validates and compiles **every** enabled graph when it loads or publishes the route set; any invalid enabled graph skips the publish, a previously published set keeps serving, and `/readyz` reports `stale: true` with `last_load_error` (not ready if no publish ever succeeded).
- Enable/disable, default switches and catalog-driven multi-graph updates are atomic: a conditional batch stages all candidate files, validates the resulting enabled set, then commits and republishes the complete enabled set; a failure rolls back the batch.
- Writes are atomic through temporary files with mode `0600`; the kernel process (uid 10001 in the image) must own the directory.
- Kubernetes: one ReadWriteOnce/ReadWriteMany PVC for the kernel graph directory (`/data/graph` on 163), `Recreate` strategy, no console deployment.
- Docker standalone: named volume `core-graphs` for the kernel, separate `console-data` volume for peripheral SQLite.
- A stale save returns 409 `graph_conflict`; imports only change the draft.
- Kernel `/healthz` is liveness; `/readyz` reports route-set readiness and exposes `default_tree_id`, `enabled_tree_ids`, `snapshot_revision` and `route_fingerprint`; 503 means not ready, and a later reload failure keeps the existing route set with `"stale": true`. Peripheral `/readyz` reports only its own SQLite state — an unreachable kernel is a data-plane error, not a readiness failure, so operators can still open the console.
- The peripheral SQLite schema upgrades itself (see [Upgrading from a pre-split installation](#upgrading-from-a-pre-split-installation)); there is no SQLite-to-graph export script any more — a canvas is moved by exporting/importing its JSON file.

## Upgrading from a pre-split installation

The core/peripheral split changes what each process owns. Do this in order:

1. **Back up everything first.** Copy the SQLite database (`router.db` with `router.db-wal` / `router.db-shm`) and the kernel graph directory (`graphs.directory`, holding `graphs/*.json`). Graph JSON stays the kernel fact source; switching deployment means mounting it into the kernel and making the kernel the only writer.
2. **The SQLite schema migrates automatically on the peripheral's first start.** Any supported historical schema (v1 through v6) upgrades sequentially to v7 in one start, one transaction per step; the final v6→v7 step is destructive: it **drops** `request_logs`, `graph_snapshots`, `request_snapshots` and their indexes (decision logs/snapshots and the stored JEV key are discarded, design §9) and rebuilds `jev_services` from its explicit non-key columns (the JEV `api_key` column is gone). Historical decision logs are **not** migrated to the memory ring or VictoriaLogs — old records can no longer be replayed. Only a too-new schema, or a database whose contents are unknown/junk, is refused; there is no path back to the single-service layout. The v7 step destroys data, so the backup in step 1 is still required.
3. **Move the JEV key out of SQLite.** The kernel reads it only from its environment (`ROUTER_JEV_API_KEY`, or the name in `config.json` `jev.api_key_env`). Set it in the kernel container/process; existing stored keys are not migrated and the old `ROUTER_JEV_*` seed variables do not create a key.
4. **Deploy the two services** (or the kernel only behind APISIX): the kernel needs `config.json` + the graph directory + the JEV key; the peripheral needs `ROUTER_CORE_URL`, its SQLite path and the usual console settings. See [Quick start](#quick-start) and [Configuration](#configuration).
5. **Re-publish graphs through the kernel** if the kernel cannot load the existing directory: startup logs `initial graph snapshot is unavailable`, kernel `/readyz` reports not ready (or `stale`), and `/internal/v1/trees` answers `graph_storage_error` until the file is fixed. Every stored canvas must already be a full v4 document (four run fields + `departments` + `enabled`/`default`); v3 files are rejected, never adopted or migrated implicitly.
6. **Retire the old single-service workload** before the kernel starts, so only one process writes the graph directory. The retired shared gateway credential and static RoutePilot upstream key are gone from code, deployment env and docs.

### Legacy profiles with non-finite numbers

One pre-upgrade data check is separate from the migration itself. Python's JSON reader accepts `NaN`, `Infinity` and `-Infinity`, so an old row can store them inside `models.profile`; they are not valid JSON and the strict catalog response cannot serialize them.

Symptoms:

- `GET /admin/models` (and other responses that echo the model row) fail with **500** — the serializer raises `Out of range float values are not JSON compliant`;
- a **confirmed** catalog sync that has to re-project that row (for example a model update that leaves `profile` unchanged) answers **422** and names the offending path;
- `POST /admin/try` is unaffected: the canvas-vs-catalog drift check normalizes non-finite values in its hint `params`, so checks still answer 200 with `runtime_mismatch` hints.

Locate the candidate rows in SQLite:

```bash
sqlite3 router.db "SELECT id, profile FROM models WHERE json_valid(profile) = 0;"
```

`json_valid(profile) = 0` covers both rows whose text is malformed (read as `{}`, harmless but still worth rewriting) and rows holding a bare `NaN` / `Infinity` token (the breaking case). To spot the latter precisely, decode each hit with Python's `json.loads` (it accepts these tokens by default) and reject values where `math.isfinite` is false; strict JSON parsers such as `jq` reject them outright.

Repair the rows:

- preferred: `PUT /admin/models/{id}` with a corrected finite `profile` and, when a canvas references it, the confirmed 409 impact payload. The console model list may itself show an error until this row is fixed, so do not rely on it for the repair;
- last resort: back up `router.db`, then rewrite the JSON text directly, e.g. `UPDATE models SET profile = '{}' WHERE id = '<model>';`;
- verify that every row is valid again: `SELECT COUNT(*) FROM models WHERE json_valid(profile) = 0;` must return 0.

No restart is needed: the catalog is read from SQLite on every request.

## Self-contained canvas JSON

Every saved canvas is a portable, immutable run snapshot: one file restores a decision group on an empty database instance. A v4 file also carries its lifecycle markers and departments; four top-level fields are **required and must appear together**:

| Field | Content |
|---|---|
| `backends` | `providers` (name, base URL, API-key environment variable, reasoning dialect) and `models` (id, upstream id, provider, profile) |
| `jev` | the selected decision-service profile: `id`, `base_url`, `model`, `key_env`, timeout, instructions and state template; `null` only for a canvas without question nodes |
| `jev_window` | `input_tokens` / `context_tokens` **of that same service** |
| `time_periods` | Time period definitions referenced by `periods` conditions |

```json
{
  "kind": "sr-routing-graph",
  "version": 4,
  "name": "Example canvas",
  "departments": [],
  "enabled": false,
  "default": false,
  "timezone": "Asia/Shanghai",
  "sticky": { "enabled": true, "ttl_seconds": 1800 },
  "root": {
    "id": "root",
    "type": "graph",
    "error_fallback": "qwen3-target",
    "nodes": [
      { "id": "start", "type": "start", "title": "Start" },
      { "id": "qwen3-target", "type": "target", "priority": 1,
        "models": [ { "model": "qwen3-8b", "reasoning": null } ] }
    ],
    "edges": [
      { "id": "e-start-qwen3", "source": "start", "source_port": "out",
        "target": "qwen3-target", "target_port": "in_1", "condition": null }
    ]
  },
  "backends": {
    "providers": [
      {
        "name": "local",
        "base_url": "http://127.0.0.1:8000/v1",
        "key_env": "LOCAL_API_KEY",
        "reasoning_format": "openai"
      }
    ],
    "models": [
      {
        "id": "qwen3-8b",
        "upstream": "qwen3-8b",
        "provider": "local",
        "profile": {
          "capability": "Fast small model for simple requests.",
          "reasoning": { "format": "qwen-chat-template", "efforts": { "off": null, "on": null }, "default": "on" }
        }
      }
    ]
  },
  "jev": {
    "id": "default",
    "base_url": "http://127.0.0.1:8100",
    "model": "d1-2b",
    "key_env": "ROUTER_JEV_API_KEY",
    "timeout_seconds": 10.0,
    "instructions": "You are a model router.",
    "state_template": "【待判断的用户请求】\n{request}"
  },
  "jev_window": { "input_tokens": 1024, "context_tokens": 4096 },
  "time_periods": [
    { "name": "day", "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00" }
  ]
}
```

Rules:

- **All four fields, always.** A document carrying only some of them (or none) is rejected with `graph_parse_error` when parsing, importing, saving or checking; there is no database fallback that fills the missing fields and no `runtime:null` control field / materialization step. Old documents are rejected, not upgraded in place.
- **JEV and window are one unit.** `jev.id` selects the catalog service; `key_env` is the kernel environment-variable name the key is read from and is fixed to `ROUTER_JEV_API_KEY`; `jev_window` belongs to that service. Selecting or synchronizing a JEV service replaces `jev` and `jev_window` together. When a raw `PUT /admin/trees/{id}` patch changes `jev.id`, it must also include the new service's `jev_window`; the request fails with 422 instead of inheriting the previous service's window.
- **No secrets, at any depth.** A provider carries `key_env` (an environment variable name) instead of a key, and `jev` never carries `api_key`. Secret and derived-key fields (`api_key`, `authorization`, `key_present`, `key_source`) are rejected wherever they appear inside the four fields, including nested objects and arrays below `backends.models[*].profile`. The match ignores case, `-` and `_` (`API-KEY` is the same as `api_key`) but only covers that fixed set, so ordinary metadata such as `monkey` or `keychain` is preserved. `profile` stays a free-form JSON metadata object otherwise: `capability`, `domains`, `tier` and later metadata are stored unchanged. A local server that needs no authentication may leave `key_env` empty.
- **Validated at the file boundary.** Provider and model ids, provider references, profiles, reasoning declarations, the JEV service profile, the window and the periods are all validated while parsing.
- **Lifecycle is required but not ordinary-save writable.** `enabled`/`default` are required document fields, but new/copied documents are always saved disabled and non-default, and importing only replaces the draft (a new group is stored closed, an existing group keeps its stored lifecycle); enable/disable/set-default go through the dedicated lifecycle endpoints, and an ordinary save can change `departments` but never lifecycle. `default: true` requires `enabled: true`.
- **Part of the behaviour version.** The four run fields plus `departments` join `tree_revision` (and the console dirty/undo baseline); `enabled`/`default` join the route-set fingerprint and `snapshot_revision` instead. Canvas coordinates still do not affect behaviour.

### Run-configuration snapshot sources

"Snapshot" here means a canvas's embedded **run configuration** — the concept the graph editor has always had and keeps. It is unrelated to the removed decision-log `graph_snapshots` table.

Only these actions create or change a canvas run snapshot:

1. **New canvas** — the initial snapshot is built from the current database catalog.
2. **Import a complete JSON document** — the imported snapshot wins.
3. **Select a model or JEV in the canvas** — the selected non-secret catalog entry is added/replaced in the draft; a JEV selection replaces profile and window atomically.
4. **Refresh runtime from database** in graph settings.
5. **Confirm a catalog impact list** — the peripheral asks the kernel to synchronize the affected canvases after the administrator confirms.

A catalog write by itself never rewrites a canvas and never republishes a route set. Editing a target with a model ID already present in the snapshot does not overwrite its snapshot entry; drift is reported by the check hints and replaced only by an explicit refresh or a confirmed sync. Copying a group keeps the source group's snapshot and departments but forces it closed, and `POST /admin/try` inherits a stored canvas's snapshot when it is given `tree_id` (a full draft must carry all four fields itself; without `tree_id` and `root` the check runs against the published route set instead).

### Keys

JSON never stores keys, and each secret has exactly one owner:

| Secret | Owner and resolution order |
|---|---|
| Provider API key | **Peripheral only.** The execution catalog resolves it for the standalone `/v1` data plane: the SQLite provider's stored key **only when `name` and the normalized `base_url` both match** the kernel decision, otherwise none; then `os.environ[key_env]` of the peripheral process. It never enters the kernel request, the decision event or any graph response. |
| Decision-service (JEV) key | **Kernel only.** Read from the environment variable named by `config.json` `jev.api_key_env` (default `ROUTER_JEV_API_KEY`) and sent only to the JEV endpoint. There is no SQLite storage, no console input and no peripheral copy. |

An endpoint change therefore never forwards an old key to the new address; keys are never written to SQLite, the JSON files or any API envelope; derived authorization headers exist only in the process that owns the key; and the decision event can contain at most a `key_env` name, never a key value. The APISIX form needs no Provider key at all — newapi executes the routed model with the user's original credential.

### Catalog impact confirmation

Updating or deleting a model, provider or JEV service first scans every canvas. If anything references the resource, the first write returns **409** with `detail.code = "impact_confirmation_required"`:

- `resource`: the kind and id/name being changed;
- `affected_trees`: for every affected canvas its `id`, `name`, `enabled`, current `document_sha256` and `usage` (target node ids, runtime catalog, JEV id, provider models);
- `replacements` for deletes (non-secret candidate models or JEV services).

Nothing is written in that state — database, files, audit and snapshot are untouched. The console shows the list; confirming retries the same request with `impact_confirmation = {"trees":[{"id","document_sha256"}, …]}` built from exactly those hashes. If a canvas changed in between, the server answers 409 `graph_conflict` with the **current** hash, and the console asks for a reload instead of applying a stale confirmation.

After confirmation, the write spans two stores. The peripheral re-checks the impact list, then asks the kernel to replace every confirmed canvas in **one conditional batch** (`POST /internal/v1/graph-batches`): verify all base `document_sha256` hashes, strictly validate every candidate, validate the resulting enabled set, commit one batch and publish. Only then does the peripheral commit its SQLite rows and audit. If that SQLite commit fails, the peripheral compensates with a reverse batch built on the new base hashes; a failed compensation returns 503 `catalog_sync_diverged` and logs a severe error instead of leaving a silent partial state. The route set is republished only when at least one synchronized canvas is an enabled graph; a draft-only sync publishes nothing.

Update/delete semantics:

- **Model update** — replaces the model entry (and its provider entry) in every canvas that declares it; target model IDs do not change.
- **Model delete** — a catalog-only entry is removed; a model referenced by a target requires a replacement model, which replaces the target references, de-duplicates model slots and embeds the replacement (and its provider) in the snapshot.
- **Provider update** — replaces the provider entry in every canvas that declares it; model references do not change. A model profile that becomes invalid under the new reasoning dialect rejects the whole write (422, nothing written).
- **JEV update** — synchronizes `jev` + `jev_window` in every canvas whose `runtime.jev.id` matches.
- **JEV delete** — any canvas referencing the service requires a replacement service; JEV and window are replaced together.
- **Provider delete** — never bypassable: it is rejected (`409 provider_referenced`) while any database model or canvas model still references the provider, and there is no implicit replacement. Release the references first.

### Canvas-vs-catalog drift

`POST /admin/try` compares the resources a canvas declares (providers, models, the JEV service and its window) with the database catalog and reports every difference or missing row as a hint (`code = "runtime_mismatch"`, `level = "hint"`, both canvas and database values in `params`). Hints never block the check and never change the execution configuration: the run still uses the canvas snapshot. Database resources the canvas does not declare are not conflicts and produce no hint. The console renders these hints under the routing-check result. The kernel runs the dry run (it has no SQLite); the peripheral re-attaches these catalog hints to the kernel's answer — the only place the comparison can run after the split.

### Restoring from one file

1. Start the two services with an empty peripheral database and an empty kernel graph directory (or the kernel only, for the APISIX form).
2. Import the JSON into a decision group (Decision groups page, or `POST /admin/trees`). Importing only replaces the draft, and saving it as a new group stores it closed; enable it from the toolbar or `POST /admin/trees/{id}/enable` — the first enabled graph becomes the default graph. (Importing into an existing enabled group and saving keeps it enabled.) Add departments in graph settings if this graph should serve a department.
3. Run a routing check (`POST /admin/try`) against the draft to verify the graph, embedded model catalog, decision service, window and time periods. Run it with scope **Published route set** (and optionally a department) to verify which enabled graph is selected; `tree_id` / `graph_selection` report the evidence.
4. Call `POST /v1/chat/completions` for Auto routing through the default graph, or open the model test chat (Auto-only, with an optional Department field) to verify an end-to-end reply.
5. Provide keys through environment variables only: the kernel's JEV key (`ROUTER_JEV_API_KEY`) and, for standalone execution, each provider's `key_env` in the peripheral environment (or its stored SQLite key).

`configs/default_routing_tree.json` is a minimal runnable example: one `local` provider, two models, the `default` decision-service profile and three time periods. It is a v4 document with `enabled: false, default: false, departments: []`: apply it through the admin API (for example `scripts/apply_default_routing_tree.py`), then enable it. The admin write path never accepts lifecycle from the payload: new and copied documents are always persisted as `enabled: false, default: false`, and importing only replaces the draft (saving into an existing group inherits that group's lifecycle). `configs/production_routing_tree.json` has the same shape with three models; replace the example base URLs and endpoints before deploying.

The five demo cases in `configs/cases/case-*.json` are complete v4 documents with `kind`/`version`, no external `id`, `enabled: false, default: false, departments: []`, provider `demo-tap`, JEV service `demo-jev` (window `2048 / 4096`) and every model their targets reference. `scripts/demo/apply_cases.py` is their deployment entry point: it merges the catalogs from the case files, seeds the multi-service JEV catalog and time periods, deploys the five canvases (without enabling them) and runs the dry-runs. It never bypasses impact confirmation — a 409 prints the affected canvases and stops by default; `--confirm-sync` explicitly retries the same requests with the previewed baseline hashes. A `graph_conflict` means a canvas changed after the preview: re-run the script.

## Behind APISIX + newapi (internal distribution)

The APISIX + newapi deployment form — the `auto` route plugin, its newapi
forward-auth integration, the kernel-only cluster manifests and the shadow
acceptance drills — is maintained in the internal distribution and is not
published in this repository. This repository ships the standalone form: the
decision kernel plus the optional console, with the generic container files in
`scripts/docker/`.

## Known limitations (split)

- **Cross-service catalog commit crash window.** A catalog write commits the kernel graph batch first and the peripheral SQLite rows second. A SIGKILL between the two commits leaves the kernel graphs ahead of SQLite until the next catalog write; kernel graphs are the base of that next batch, and standalone execution fails closed with 503 `execution_catalog_mismatch` until both sides agree. There is no crash recovery in this iteration — a deliberate non-goal.
- **Decision-log receiver is network-boundary-only, now on its own listener.** `POST /internal/v1/decision-logs` has no shared token, but it is served only by the peripheral's separate internal listener (`ROUTER_INTERNAL_HOST`/`ROUTER_INTERNAL_PORT`, default `127.0.0.1:8402`; container port 8400 in compose, never published) while the public console port answers 404 for it. Firewall that internal port in non-compose deployments. The receiver accepts at most 10 MiB per event and retains 20 events in memory (the 21st evicts the oldest, and a restart clears the log).
- **The JEV "Test connection" runs in the kernel.** The console proxies the non-secret service description to the kernel's `POST /internal/v1/jev-test`; the kernel probes the endpoint with the key from its own environment. The peripheral process and its SQLite never hold a JEV key (I6).
- **Kernel scheduling breaker no longer sees upstream execution.** It observes decisions only: there is no execution write-back by design. The standalone peripheral keeps its own execution-side breaker; the APISIX form lets newapi own retries and channel health.
- **VictoriaLogs output adapter is not implemented.** `logging.type` supports only `routepilot` and `disabled`; the event schema is fixed so a future `logging.type=victorialogs` adapter can serialize the same objects.
- **Expired console sessions are no longer purged automatically.** The old `MaintenanceWorker` is no longer wired into the peripheral app (plan Task 8), so expired `user_sessions` rows accumulate in SQLite until manual cleanup.
- **Legacy short non-whitespace admin passwords are not auto-flagged.** The startup gate (and `ROUTER_ADMIN_FORCE_RESET=1`) only treats publicly-known, blank and whitespace-only passwords as compromised: the built-in default, the empty string, and **space runs of 1–16 spaces** (space characters only — a legacy password made of tabs or other whitespace, for example eight tabs, is not flagged by this bounded scan). Any whitespace-only password (spaces, tabs or any other whitespace) is rejected by the current API policy for new and reset passwords going forward. A legacy non-whitespace password shorter than the current 8-character minimum is **not** guessable the way a blank/default password is, so it does not block startup; operators should rotate such accounts deliberately. Longer or exotic whitespace-only legacies are likewise out of the automatic scan's scope.
- **The APISIX + newapi deployment form is not part of this repository.** Its plugin contract, cluster manifests and acceptance drills are maintained in the internal distribution; this public tree ships only the standalone kernel + console form.

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Standalone: kernel + peripheral.** The kernel reads `config.json` through `ROUTER_CONFIG` (start from `configs/core.example.json`, but point `graphs.directory` at a writable local directory and use `logging.type=disabled` unless a peripheral receiver is reachable); the peripheral needs `ROUTER_CORE_URL` and its SQLite path:

```bash
# Terminal 1 — decision kernel (default port 8400)
cat > /tmp/routepilot-core.json <<'EOF'
{"server": {"host": "127.0.0.1", "port": 8400},
 "graphs": {"directory": "/tmp/routepilot-graphs"},
 "jev": {"api_key_env": "ROUTER_JEV_API_KEY"},
 "logging": {"type": "disabled"}}
EOF
ROUTER_CONFIG=/tmp/routepilot-core.json \
ROUTER_JEV_API_KEY='<jev-key>' \
.venv/bin/python -m router core

# Terminal 2 — peripheral: public console 8401 + internal event listener 8402
ROUTER_CORE_URL=http://127.0.0.1:8400 \
ROUTER_DB=/tmp/routepilot.db \
ROUTER_HOST=127.0.0.1 ROUTER_PORT=8401 \
ROUTER_INTERNAL_HOST=127.0.0.1 ROUTER_INTERNAL_PORT=8402 \
ROUTER_ADMIN_EMAIL='admin@example.com' ROUTER_ADMIN_PASSWORD='<strong-password>' \
.venv/bin/python -m router peripheral
```

Console: `http://127.0.0.1:8401/console/`. The complete standalone form is composed in `scripts/docker/compose.yml` (kernel 8400 internal, console public port published only on `127.0.0.1:8401`, separate volumes).

The peripheral process runs **two listeners**: the public console/`/admin`/`/v1` listener (`ROUTER_HOST`/`ROUTER_PORT`) and a separate internal listener (`ROUTER_INTERNAL_HOST`/`ROUTER_INTERNAL_PORT`, `127.0.0.1:8402` by default) that serves only `POST /internal/v1/decision-logs` and `/healthz` for the kernel event feed. In compose the internal listener is container port 8400 and is **not published**; point the kernel at `http://console:8400/internal/v1/decision-logs`. For the local layout above set the kernel `logging` block to `{"type": "routepilot", "url": "http://127.0.0.1:8402/internal/v1/decision-logs", "timeout_ms": 1000}`. In a non-compose deployment, firewall the internal port so only the kernel host can reach it.

**Admin credentials are required and have no deployment defaults.** Compose refuses to start unless `ROUTER_ADMIN_EMAIL` and `ROUTER_ADMIN_PASSWORD` are exported explicitly. On a non-loopback bind the peripheral also refuses to start when `ROUTER_ADMIN_PASSWORD` is empty, whitespace-only, shorter than 8 characters or the built-in seed password (the same policy the user API enforces — both use `router/password_policy.py`); the built-in seed values exist only for loopback-only local runs like the one above, so always set explicit credentials. An existing database that still accepts the built-in default admin password, an empty one, or one made only of spaces refuses to start as well: the operator must either start once with `ROUTER_HOST=127.0.0.1` and change those passwords through the console/user API, or explicitly authorize `ROUTER_ADMIN_FORCE_RESET=1`, which resets all such accounts to `ROUTER_ADMIN_PASSWORD` (in one batch transaction) and revokes all their sessions. This stale-account check is a strictly read-only preflight that runs **before** the destructive v6→v7 migration, so a refused start leaves the old database and its decision logs intact. Only publicly-known, blank and whitespace-only credentials are treated as public; see [Known limitations](#known-limitations-split).

**Kernel only (behind APISIX + newapi).** This is the 163 form: no SQLite, no console, no model upstream, no decision-log receiver. Use a `config.json` with `logging.type=disabled` and the graph directory on a persistent volume:

```bash
ROUTER_CONFIG=/etc/routepilot/config.json \
ROUTER_JEV_API_KEY='<jev-key>' \
.venv/bin/python -m router core
```

The first peripheral start against an empty users table seeds the admin account. An empty graph directory is a legal empty route set: the kernel `/readyz` is ready, but decisions answer 503 `no_default_tree`/`no_active_tree` until a graph is enabled. Create or import a canvas through the console, then enable it; the first enabled graph becomes the default graph. Existing installations: read [Upgrading from a pre-split installation](#upgrading-from-a-pre-split-installation) first.

## Configuration

The **kernel** is configured by `config.json` (path via `ROUTER_CONFIG`, default `config.json`). It contains no secret values; the JEV key is named by `jev.api_key_env` and supplied as an environment variable:

```json
{
  "server": {"host": "0.0.0.0", "port": 8400},
  "graphs": {"directory": "/data/graphs"},
  "jev": {"api_key_env": "ROUTER_JEV_API_KEY"},
  "logging": {
    "type": "routepilot",
    "url": "http://console:8400/internal/v1/decision-logs",
    "timeout_ms": 1000
  }
}
```

The sample URL targets the compose peripheral service's **internal** listener (`console:8400`), matching `configs/core.example.json` and `scripts/docker/core-config.json`; that container port is never published. In the local Quick start layout the console's internal listener defaults to `127.0.0.1:8402`, so use `http://127.0.0.1:8402/internal/v1/decision-logs`; the public 8401 console port answers 404 for the receiver. When no receiver is reachable keep `logging.type=disabled`.

| Kernel `config.json` field | Default | Purpose |
|---|---|---|
| `server.host` / `server.port` | `0.0.0.0` / `8400` | Kernel listener |
| `graphs.directory` | `./routing-graph` | The **only** graph JSON directory; the kernel is its single writer (`/data/graphs` in compose, `/data/graph` on 163) |
| `jev.api_key_env` | `ROUTER_JEV_API_KEY` | Environment-variable **name** holding the JEV key; never a key value |
| `logging.type` | `disabled` | `routepilot` (send events to the peripheral receiver) or `disabled`; `victorialogs` is not implemented this period |
| `logging.url` | — | Event receiver URL, required for `logging.type=routepilot` (http/https) |
| `logging.timeout_ms` | `1000` | Per-event HTTP timeout, 1–10000 |

Kernel environment: `ROUTER_CONFIG` (config path) and the JEV key variable named by `jev.api_key_env`. The kernel honours no `ROUTER_HOST/PORT/DB/GRAPH_DIR` variables and opens no SQLite.

The **peripheral** uses environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `ROUTER_CORE_URL` | `http://127.0.0.1:8400` | Kernel base URL for graph management and decisions |
| `ROUTER_CORE_TIMEOUT_SECONDS` | `60` | Kernel HTTP timeout |
| `ROUTER_DB` | `router.db` | Peripheral SQLite: people, sessions, providers, model catalog, non-secret JEV catalog, time periods, change records |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8400` | Public peripheral listener: console, `/admin`, `/v1` (compose publishes only `127.0.0.1:8401`) |
| `ROUTER_INTERNAL_HOST` / `ROUTER_INTERNAL_PORT` | `127.0.0.1` / `8402` | Internal kernel event-receiver listener: only `/internal/v1/decision-logs` + `/healthz`, never published (compose: container port 8400) |
| `ROUTER_JEV_TIMEOUT` | `10` | Decision-service timeout used when seeding the `default` non-secret JEV catalog entry |
| `ROUTER_JEV_URL` / `ROUTER_JEV_MODEL` | empty | One-time seed of the `default` non-secret JEV catalog entry; used together, only while the catalog is empty and never seeded before |
| `ROUTER_SESSION_MAX_ENTRIES` | `10000` | Session-affinity LRU capacity (peripheral process) |
| `ROUTER_USER_AUTH` | `true` | Console authentication |
| `ROUTER_ADMIN_EMAIL` / `ROUTER_ADMIN_PASSWORD` | seed values (loopback only) | First-run admin account; **required explicitly in the compose deployment (no defaults); on a non-loopback bind the password must be at least 8 characters and must not be the built-in default** |
| `ROUTER_ADMIN_FORCE_RESET` | `false` | Explicitly authorizes the one-shot, batch-atomic reset of existing active accounts whose stored hash still accepts the built-in default, an empty password or a password made only of spaces (to `ROUTER_ADMIN_PASSWORD`, revoking their sessions); non-loopback bind only |
| `ROUTER_SESSION_TTL_HOURS` | `168` | Console session lifetime |

The peripheral process **never reads** `ROUTER_JEV_API_KEY`: that variable belongs to the kernel process only (see the kernel configuration); the console "Test connection" is executed by the kernel with its own key (I6).

## API

Full field tables, request/response examples and the impact-confirmation flow live in the [backend API reference](docs/后端接口说明.md) (Chinese).

- **Kernel:** `POST /internal/v1/route-decision`, `/internal/v1/trees*`, `/internal/v1/try`, `POST /internal/v1/graph-batches`, `/healthz`, `/readyz`. No auth middleware and no console; protect it with the network boundary. It is the only caller-facing component in the 163 form.
- **Peripheral:** the console and `/admin/*` APIs, `POST /internal/v1/decision-logs` (kernel event receiver, no admin login), `GET /admin/requests/recent`, `GET /admin/requests/{id}/replay`, the standalone `POST /v1/chat/completions` and `/admin/test/chat/completions`.
- `/v1/*`, `/healthz`, `/readyz` and `/console/*` need no auth; every other `/admin/*` route needs `Authorization: Bearer <token>`.
- `POST /admin/login` is the only public admin endpoint: email and password exchange for a token. Sessions default to 168 hours; logout, a password change or deleting the user revokes them.
- `viewer` can read only (`/admin/users*` is off-limits too); writes and user management need `admin`. With `ROUTER_USER_AUTH=0` everything is open and requests run as the service account.
- Data-plane errors are `{"error":{"message":"<machine code>"}}`; admin errors are `{"detail": ...}`, where graph/config validation failures carry an Issue array.
- `POST /v1/chat/completions` asks the kernel for a decision and always routes through the default graph of the kernel's enabled route set; the `x-sr-session-id` header drives stickiness, and an `x-sr-department` header is rejected with `400 department_header_not_allowed`. `x-sr-model` / `x-sr-selection` / `x-sr-tree-id` / `x-sr-graph-selection` / `x-sr-reasoning*` report the kernel decision. A kernel decision whose model/provider the peripheral catalog cannot execute returns 503 `execution_catalog_mismatch` without calling any upstream.
- `POST /admin/trees` must carry `backends`, `jev`, `jev_window` and `time_periods` together and accepts `departments`; new groups are always closed. `PUT` is a patch and must carry the current `base_document_sha256`, otherwise 409. Lifecycle is written only by `POST /admin/trees/{id}/enable`, `/disable` and `/default`; `departments` can also be written by `PUT /admin/trees/{id}/departments`. All of these are proxied to the kernel by the peripheral.

| Method and path | Service | Auth | Purpose |
|---|---|---|---|
| `POST /internal/v1/route-decision` | kernel | network boundary | One routing decision: real model, logical model, non-secret provider facts, tree/reasoning metadata |
| `POST /internal/v1/graph-batches` | kernel | network boundary | Atomic conditional multi-graph replacement used by peripheral catalog writes |
| `/internal/v1/trees*`, `/internal/v1/try` | kernel | network boundary | Graph management and dry-run (the peripheral proxies its `/admin/trees*`, `/admin/try` here) |
| `GET /healthz` / `GET /readyz` | both | public | Liveness / readiness (kernel: route set + `stale`; peripheral: SQLite) |
| `POST /v1/chat/completions` | peripheral | public | Standalone OpenAI-compatible data plane: kernel decision → peripheral Provider key → model upstream; JSON or SSE |
| `POST /internal/v1/decision-logs` | peripheral | network boundary | Kernel decision-event receiver (validated, in-memory, max 20) |
| `GET /admin/requests/recent`, `/requests/{id}/replay` | peripheral | viewer | Memory decision log list and single-event replay |
| `POST /admin/login` / `POST /admin/logout` | peripheral | public / signed in | Console session |
| `GET /admin/me`, `POST /admin/me/password` | peripheral | signed in | Current user, password change |
| `GET/POST /admin/trees`, `GET/PUT /admin/trees/{id}` | peripheral proxy | read / admin | Decision groups; create is always closed |
| `PUT /admin/trees/{id}/departments` | peripheral proxy | admin | Department mapping for one group |
| `POST /admin/trees/{id}/copy`, `/enable`, `/disable`, `/default` | peripheral proxy | admin | Copy and multi-graph lifecycle (first enable becomes default; default switch) |
| `POST /admin/try` | peripheral proxy | admin | Routing check against a draft graph or the active route set (optional department) |
| `POST /admin/test/chat/completions` | peripheral | admin | Test chat; `model=auto` asks the kernel, anything else is a fixed direct call through the peripheral catalog |
| `GET/POST /admin/models`, `GET/PUT/DELETE /admin/models/{id}` | peripheral | read / admin | Model registry; update/delete preview a 409 impact list and sync confirmed canvases |
| `GET /admin/model-catalog`, `POST /admin/fetch-models` | peripheral | read / admin | Provider presets, upstream model discovery |
| `GET /admin/providers`, `PUT/DELETE /admin/providers/{name}` | peripheral | read / admin | Providers; update previews a 409 impact list, delete is blocked while referenced |
| `GET/POST /admin/jev`, `GET/PUT/DELETE /admin/jev/{id}` | peripheral | read / admin | Non-secret multi-service JEV catalog (window belongs to each service) |
| `POST /admin/jev/{id}/test` | peripheral | admin | Test one catalog service (proxied; the kernel runs it with its own key) |
| `GET/PUT /admin/time-periods` | peripheral | read / admin | Time periods |
| `GET /admin/requests/stats`, `GET /admin/overview`, `GET /admin/changes` | peripheral | viewer | Memory-log stats/overview, audit history |
| `GET/POST /admin/users`, `PUT/DELETE /admin/users/{id}`, `POST /admin/users/{id}/password` | peripheral | admin | Users |

### Errors

Data-plane (peripheral `/v1`):

| Status | Error `message` | Meaning |
|---|---|---|
| 400 | `invalid_json` / `body_must_be_object` / `messages_must_be_list` / `invalid_session_id` | Malformed body or session header |
| 400 | `department_header_not_allowed` | Public `/v1` request carried `x-sr-department` |
| 413 | `request_too_large` | Body exceeds 10 MiB |
| 502 | `upstream_error` | Upstream 5xx or network failure |
| 503 | `execution_catalog_mismatch` | Kernel decision names a model/provider the peripheral catalog cannot execute; no upstream call |
| 503 | `no_valid_snapshot` | Kernel has never assembled a route set |
| 503 | `no_active_tree` | Empty route set: no enabled decision group |
| 503 | `no_default_tree` | Enabled graphs exist but none is the default graph |
| 503 | `no_available_backend` | All backends of the selected target unavailable |
| 503 | `routing_failed` / `routing_invariant_violation` | Internal routing failure (kernel) |
| 503 | `core_unavailable` | The peripheral cannot reach the kernel |

Admin:

| Status | Error | Meaning |
|---|---|---|
| 401 / 403 | `unauthorized…` / `forbidden…` | Not signed in / viewer write |
| 404 | `unknown request` | Decision event evicted from the 20-event memory log or lost after a restart |
| 404 | `unknown tree <id>` | Graph id does not exist in the kernel |
| 409 | `default_graph_required` | Disabling the default graph while other graphs stay enabled |
| 409 | `graph_conflict` | Baseline or impact-list document hash is stale |
| 409 | `impact_confirmation_required` | Model/provider/JEV write affects canvases; confirm the listed document hashes |
| 409 | `provider_referenced` | Provider still used by a database model or a canvas model |
| 409 | string `tree <id> already exists` | Duplicate graph id (create/copy); plain string detail, no `code` |
| 422 | Issue array | Graph/config validation failed |
| 503 | `graph_storage_error` | Kernel graph storage unreadable/write failed, or the loaded route set violates invariants |
| 503 | `core_unavailable` | Kernel unreachable while proxying graph management |
| 503 | `catalog_sync_diverged` | Kernel graph batch committed but the peripheral SQLite commit **and** its compensating batch failed; manual reconciliation required |
| 502 | string | JEV test or upstream model discovery failed |

Upstream 4xx responses pass through with their status and body; `/admin/test/chat/completions` additionally returns `404 unknown_model`, `422 invalid_model` and `400 invalid_department` (malformed trusted department header).

Configuration endpoints: `/admin/models`, `/admin/providers`, `/admin/jev`, `/admin/time-periods`. There is no generic `/admin/settings` API and no standalone JEV-window endpoint: the window lives on each JEV service.

## Tests

```bash
.venv/bin/python -m pytest -q
node --test scripts/console_chat_test.mjs
node --test scripts/console_graph_test.mjs scripts/console_draft_test.mjs \
    scripts/console_runtime_test.mjs scripts/graph_contract_test.mjs \
    scripts/console_impact_test.mjs
npm install
npm run check:console
.venv/bin/python scripts/i18n_dead_keys.py
.venv/bin/python -m pytest -q tests/test_service_split_e2e.py -v
ROUTER_ADMIN_EMAIL=x ROUTER_ADMIN_PASSWORD=y \
  docker compose -f scripts/docker/compose.yml config
```

- `pytest` collects `tests/` (`testpaths` in `pyproject.toml`), runs the unit and service-split suites, starts no browser and does not collect `experiments/`. `tests/test_service_split_e2e.py` is the real two-app acceptance run: in-process kernel + peripheral over real HTTP, graph creation/enable through the peripheral proxy, `auto` decision, peripheral model execution, JSON/SSE, kernel event → peripheral memory log, recent/replay, 21st-event eviction, current-graph edits not affecting replays, restart clearing the log, and a log-sink failure not blocking decisions.
- `node --test` runs the ES-module suites: graph/canonical behavior, draft state, the runtime helpers used by graph settings and impact confirmation, and the cross-runtime contract fixtures (`tests/fixtures/graph_contracts.json`).
- `npm run check:console` runs all Node test suites and the Playwright acceptance matrix against a temporary database, mock decision service and mock upstream. Screenshots are off by default; `QA_SCREENSHOTS=1` regenerates them. `QA_ONLY=<regex>` selects cases; a page module runs standalone, e.g. `node scripts/console_qa/canvas.mjs`. Set `CHROME=/path/to/chrome` when Chromium is not in the default cache.
- `.venv/bin/python scripts/i18n_dead_keys.py` lists console-dictionary keys that no page references (exit code 1 when any exist) so dead translations can be deleted; it is a CI lint and is not part of the Playwright matrix.
- `docker compose -f scripts/docker/compose.yml config` statically validates the two-service standalone deployment (two services, separate volumes, healthchecks, no secret values). It requires `ROUTER_ADMIN_EMAIL`/`ROUTER_ADMIN_PASSWORD` in the environment: without them compose refuses to render, which is the intended default-credential guard.
- `scripts/standalone_v4_smoke.py` is a **legacy pre-split** acceptance run (single-service writer/replica assumptions); it is kept for history and must not be used to validate the split. The split gate is `tests/test_service_split_e2e.py`.

Experiments are standalone and excluded from the default run:

```bash
.venv/bin/python -m pytest -q experiments/jev-batch/test_spike_jev_batch.py
.venv/bin/python experiments/jev-batch/spike_jev_batch.py --samples /path/to/samples.jsonl --scenario all --dry-run
```

## Project layout

```
router/
  api/                 FastAPI apps (core_app/create_app), routes, DTOs, exception mapping, proxy wiring, presenters
  control/             kernel graph writes, cross-service catalog commits/compensation, impact lists, enable validation
  routing/
    graph/             graph model, validation, conditions, compiler, evaluator
    service.py         evaluate → schedule orchestration
    scheduling.py      sticky selection, lease acquisition, affinity writes
    affinity.py        session-affinity LRU
    scheduler.py       random model picker and leases
    snapshot.py        immutable route-set snapshot assembly and compiled topology
    route_set.py       route-set invariants: the default graph and department index
    tree.py            tree envelope codec and revision
    codec.py           strict field parsing helpers
  decision_events.py   self-contained decision event schema, validation and projections
  decision_log_sink.py kernel best-effort async event sink (routepilot / disabled)
  decision_log_store.py peripheral deque(maxlen=20) event store
  core_client.py       peripheral → kernel HTTP client (decision and management)
  core_config.py       kernel config.json loader
  jev/                 decision-service client, non-secret service catalog DTO, input window
  upstream/            execution catalog join, proxy transport, breaker; provider catalog
  store/               peripheral SQLite schema/repositories, kernel graph-file repository
  console/             bilingual native ES Module console (peripheral-only)
configs/               example decision groups + core.example.json; configs/cases/ holds the five portable demo cases
docs/                  design and operations notes; 后端接口说明.md and 决策图规范.md are the reference docs
tests/                 pytest suite and contract fixtures
scripts/               Node contract tests, Playwright QA, deployment helpers
experiments/jev-batch/ standalone JEV batch spike
assets/routepilot/     README screenshots
```
