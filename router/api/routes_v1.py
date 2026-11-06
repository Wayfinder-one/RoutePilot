from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router.api.trace_presenter import present_trace
# 与 /admin/try 共用答案序列化契约：完整 answers 只进内存 ring / 试算响应，不进 SQLite。
from router.jev.client import ChoiceAnswer
from router.jev.serialization import answer_to_dict
from router.reasoning import ReasoningError, apply_reasoning
from router.routing.graph.model import ConditionNode, LogicNode, QuestionNode, StartNode, TargetNode
from router.routing.types import NoActiveTreeError, NoAvailableBackend, RoutingMiss, RoutingRequest, session_digest

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_SSE_LINE_BYTES = 1024 * 1024
OUTCOME_COMPLETED = "completed"
OUTCOME_UPSTREAM_ERROR = "upstream_error"
OUTCOME_CLIENT_DISCONNECTED = "client_disconnected"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_ROUTING_ERROR = "routing_error"
OUTCOME_STATES = (OUTCOME_COMPLETED, OUTCOME_UPSTREAM_ERROR, OUTCOME_CLIENT_DISCONNECTED, OUTCOME_CANCELLED, OUTCOME_INCOMPLETE, OUTCOME_ROUTING_ERROR)

OUTCOME_DISPLAY = {
    OUTCOME_COMPLETED: {"zh": "已完成", "en": "Completed"},
    OUTCOME_UPSTREAM_ERROR: {"zh": "上游错误", "en": "Upstream error"},
    OUTCOME_CLIENT_DISCONNECTED: {"zh": "客户端断开", "en": "Client disconnected"},
    OUTCOME_CANCELLED: {"zh": "已取消", "en": "Cancelled"},
    OUTCOME_INCOMPLETE: {"zh": "进行中", "en": "In progress"},
    OUTCOME_ROUTING_ERROR: {"zh": "路由失败", "en": "Routing failed"},
}


def _label(zh: str, en: str) -> dict[str, str]:
    return {"zh": zh, "en": en}


def _shown(value):
    return "—" if value is None or value == "" else value


def _milliseconds(value) -> str:
    return "—" if value is None else f"{round(value)}ms"


def _route_label(record: dict) -> dict[str, str]:
    if record.get("outcome_state") == OUTCOME_ROUTING_ERROR:
        return _label("路由中止", "Routing stopped")
    if record.get("selection_reason") == "sticky" or record.get("sticky_hit"):
        return _label("会话粘性直达", "Session affinity")
    final = next((event for event in reversed(record.get("trace") or [])
                  if isinstance(event, dict) and event.get("event") == "final"), None)
    kind = final.get("kind") if final else None
    if kind == "otherwise":
        return _label("默认目标兜底", "Default target fallback")
    if kind == "error_fallback" or record.get("selection_reason") == "error_fallback":
        return _label("错误兜底目标", "Error fallback target")
    return _label("目标命中", "Target selected")


def _display_fields(record: dict) -> None:
    """Ordered UI facts stay in the in-memory record, outside the SQLite ledger."""
    record["route_label"] = _route_label(record)
    record["metrics"] = [
        [_label("上游", "Upstream"), _shown(record.get("upstream_status"))],
        [_label("路由", "Routing"), _milliseconds(record.get("routing_ms"))],
        [_label("总耗时", "Total"), _milliseconds(record.get("total_ms"))],
        [_label("轮次", "Waves"), _shown(record.get("waves"))],
    ]
    record["fields"] = [
        [_label("请求 ID", "Request ID"), _shown(record.get("id"))],
        [_label("决策组", "Decision group"), _shown(record.get("tree_id"))],
        [_label("决策组版本", "Tree revision"), _shown(record.get("tree_revision"))],
        [_label("快照", "Snapshot"), _shown(record.get("snapshot_revision"))],
        [_label("策略路径", "Policy path"), _shown(record.get("policy_path"))],
        [_label("会话哈希", "Session hash"), _shown(record.get("session_hash"))],
        [_label("JEV 模型", "JEV model"), _shown(record.get("jev_model"))],
        [_label("推理等级", "Reasoning level"), _shown(record.get("reasoning"))],
        [_label("推理回退", "Reasoning fallback"), _shown(record.get("reasoning_warning"))],
        [_label("首 token 耗时", "Time to first token"), _milliseconds(record.get("ttft_ms"))],
        [_label("上游模型", "Upstream model"), _shown(record.get("upstream_model"))],
        [_label("Token 总数", "Total tokens"), _shown(record.get("total_tokens"))],
    ]


def _result_event(record: dict) -> dict:
    state = record["outcome_state"]
    status = OUTCOME_DISPLAY[state]
    tone = ("bad" if state == OUTCOME_UPSTREAM_ERROR else
            "warn" if state in (OUTCOME_CANCELLED, OUTCOME_CLIENT_DISCONNECTED) else
            "warn" if record.get("error_class") == "client_4xx" else "hit")
    return {
        "event": "request_outcome", "phase": "outcome",
        "label": _label("执行结果", "Execution result"),
        "cell": status, "summary": status,
        "facts": [
            [_label("模型", "Model"), _shown(record.get("model"))],
            [_label("上游状态", "Upstream status"), _shown(record.get("upstream_status"))],
            [_label("总耗时", "Total time"), _milliseconds(record.get("total_ms"))],
        ],
        "tone": tone, "outcome_state": state,
    }


def _last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


_NODE_TYPES = (
    (StartNode, "start"),
    (QuestionNode, "question"),
    (ConditionNode, "condition"),
    (LogicNode, "logic"),
    (TargetNode, "target"),
)


def _node_labels(snapshot) -> dict:
    if snapshot is None or snapshot.tree is None:
        return {}
    labels = {}
    for node in snapshot.tree.root.nodes:
        node_type = next(
            (name for node_class, name in _NODE_TYPES if isinstance(node, node_class)),
            "unknown",
        )
        labels[node.id] = {"title": node.title, "type": node_type}
    return labels


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def prepare_upstream_request(body: dict, backend, reasoning_level):
    upstream_body = {**body, "model": backend.upstream_name}
    if reasoning_level is None:
        return upstream_body, None
    try:
        return apply_reasoning(upstream_body, backend.reasoning, reasoning_level), None
    except ReasoningError as exc:
        logger.warning("reasoning fallback model=%s level=%s: %s", backend.upstream_name, reasoning_level, exc)
        return upstream_body, "unsupported"


def _usage_fields(usage) -> dict:
    fields = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    if isinstance(usage, dict):
        for key in fields:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                fields[key] = value
    return fields


async def _is_disconnected(request) -> bool:
    if request is None:
        return False
    try:
        return bool(await request.is_disconnected())
    except BaseException:
        return False


class _OutcomeRecorder:
    def __init__(self, requestlog, record: dict, started: float):
        self.requestlog = requestlog
        self.record = record
        self.started = started
        self._done = False

    def finish(self, *, outcome_state: str, error_class: str | None = None,
               upstream_model: str | None = None, upstream_status: int | None = None,
               upstream_ms: float | None = None, ttft_ms: float | None = None,
               usage: dict | None = None) -> bool:
        if outcome_state not in OUTCOME_STATES:
            raise ValueError(f"unknown outcome_state {outcome_state!r}")
        if self._done:
            return False
        self._done = True
        self.record.update({
            "upstream_model": upstream_model,
            "upstream_status": upstream_status,
            "upstream_ms": upstream_ms,
            "ttft_ms": ttft_ms,
            "total_ms": (time.monotonic() - self.started) * 1000.0,
            "error_class": error_class,
            "outcome_state": outcome_state,
            **_usage_fields(usage),
        })
        self.record["trace"].append(_result_event(self.record))
        _display_fields(self.record)
        self.requestlog.enqueue_result(self.record)
        return True


class _SseUsage:
    def __init__(self):
        self._buf = b""
        self.usage: dict | None = None

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            newline = self._buf.find(b"\n")
            if newline < 0:
                break
            line, self._buf = self._buf[:newline], self._buf[newline + 1:]
            self._scan(line)
        if len(self._buf) > MAX_SSE_LINE_BYTES:
            self._buf = b""

    def finish(self) -> None:
        if self._buf:
            line, self._buf = self._buf, b""
            self._scan(line)

    def _scan(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            value = json.loads(payload)
        except (ValueError, TypeError):
            return
        if isinstance(value, dict) and isinstance(value.get("usage"), dict):
            self.usage = value["usage"]


def _first_wave_choice(decision):
    """第一波按 node_id 排序后的首个 ChoiceAnswer（日志兼容列，spec §3.8）。"""
    first_wave = decision.waves[0] if decision.waves else None
    if first_wave is None:
        return None
    for node_id in sorted(first_wave.questions):
        answer = decision.answers.get(node_id)
        if isinstance(answer, ChoiceAnswer):
            return answer
    return None


def _decision_record(request: Request, decision, snapshot, body, text: str,
                     reasoning_warning, routing_ms: float, started: float) -> dict:
    first_wave = decision.waves[0] if decision.waves else None
    answer = _first_wave_choice(decision)
    record = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
        "decision": "route",
        "model": decision.model,
        "target_id": decision.target_id,
        "tree_id": snapshot.tree_id,
        "tree_revision": snapshot.tree_revision,
        "snapshot_revision": snapshot.snapshot_revision,
        "policy_path": decision.policy_path,
        "selection_reason": decision.selection_reason,
        "fallback_reason": decision.fallback_reason,
        "sticky_hit": 1 if decision.sticky_hit else 0,
        "session_hash": decision.session_hash,
        "jev_model": snapshot.jev.model if first_wave is not None and snapshot.jev else None,
        "jev_choice": answer.selected_key if answer else None,
        "jev_top": answer.p_top if answer else None,
        "jev_margin": answer.margin if answer else None,
        "jev_ms": first_wave.duration_ms if first_wave is not None else None,
        "jev_probabilities": dict(answer.probabilities) if answer else None,
        "waves": len(decision.waves),
        "answers": {node_id: answer_to_dict(value) for node_id, value in decision.answers.items()},
        "reasoning": decision.reasoning,
        "reasoning_warning": reasoning_warning,
        "routing_ms": routing_ms,
        "latency_ms": (time.monotonic() - started) * 1000.0,
        "trace": list(decision.trace),
        "node_labels": _node_labels(snapshot),
        "upstream_model": None,
        "upstream_status": None,
        "upstream_ms": None,
        "ttft_ms": None,
        "total_ms": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "error_class": None,
        "outcome_state": OUTCOME_INCOMPLETE,
    }
    _display_fields(record)
    return record


def _routing_failure_record(snapshot, text, session_id, trace, *, error_class,
                            routing_ms, total_ms):
    # 调度失败 ≠ 图没有决策：恢复 trace 中最后一个 final 的目标元数据；
    # 无 final（图前失败）时保持 None，不从 sticky/schedule 推断成功结论。
    final = next(
        (
            event for event in reversed(trace)
            if isinstance(event, dict) and event.get("event") == "final"
        ),
        None,
    )
    target_id = final.get("target_id") if final is not None else None
    final_kind = final.get("kind") if final is not None else None
    if final_kind == "target":
        selection_reason = "target"
        fallback_reason = None
    elif final_kind in {"otherwise", "error_fallback"}:
        selection_reason = "error_fallback"
        fallback_reason = "error_fallback"
    else:
        selection_reason = None
        fallback_reason = None
    record = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
        "decision": "route",
        "model": None,
        "target_id": target_id,
        "tree_id": getattr(snapshot, "tree_id", None),
        "tree_revision": getattr(snapshot, "tree_revision", None),
        "snapshot_revision": getattr(snapshot, "snapshot_revision", None),
        "policy_path": target_id,
        "selection_reason": selection_reason,
        "fallback_reason": fallback_reason,
        "sticky_hit": 0,
        "session_hash": session_digest(session_id),
        "jev_model": snapshot.jev.model if snapshot is not None and snapshot.jev else None,
        "waves": sum(event.get("event") == "wave" for event in trace),
        "answers": {},
        "routing_ms": routing_ms,
        "latency_ms": total_ms,
        "total_ms": total_ms,
        "trace": trace,
        "node_labels": _node_labels(snapshot),
        "outcome_state": OUTCOME_ROUTING_ERROR,
        "error_class": error_class,
    }
    _display_fields(record)
    return record


@router.post("/chat/completions")
async def chat_completions(request: Request):
    started = time.monotonic()
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return _error("request_too_large", 413)
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > MAX_BODY_BYTES:
            return _error("request_too_large", 413)
    try:
        body = json.loads(bytes(buffer))
    except Exception:
        return _error("invalid_json", 400)
    if not isinstance(body, dict):
        return _error("body_must_be_object", 400)
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return _error("messages_must_be_list", 400)
    session_id = request.headers.get("x-sr-session-id", "").strip()
    if len(session_id) > 256:
        return _error("invalid_session_id", 400)

    text = _last_user_text(messages)
    requestlog = request.app.state.requestlog
    routing_started = time.monotonic()
    trace: list[dict] = []

    def fail(error_class, status_code, stage, message):
        routing_ms = (time.monotonic() - routing_started) * 1000.0
        total_ms = (time.monotonic() - started) * 1000.0
        trace.append({
            "event": "routing_error",
            "stage": stage,
            "error_class": error_class,
            "message": message,
            "phase": "routing_error",
            "label": _label("路由中止", "Routing stopped"),
            "cell": OUTCOME_DISPLAY[OUTCOME_ROUTING_ERROR],
            "summary": OUTCOME_DISPLAY[OUTCOME_ROUTING_ERROR],
            "facts": [[_label("原因", "Reason"), error_class]],
            "tone": "bad",
        })
        trace[:] = present_trace(trace)
        requestlog.enqueue(_routing_failure_record(
            snapshot, text, session_id, trace,
            error_class=error_class, routing_ms=routing_ms, total_ms=total_ms,
        ))
        return _error(error_class, status_code)

    snapshot = request.app.state.snapshot
    if snapshot is None:
        return fail("no_valid_snapshot", 503, "snapshot", "no valid snapshot")
    try:
        decision = await request.app.state.routing_service.route(
            snapshot,
            RoutingRequest(text=text, session_id=session_id),
            trace_sink=trace,
        )
    except NoActiveTreeError as exc:
        return fail("no_active_tree", 503, "graph", str(exc))
    except NoAvailableBackend as exc:
        return fail("no_available_backend", 503, "schedule", str(exc))
    except RoutingMiss as exc:
        return fail("routing_invariant_violation", 503, "graph", str(exc))
    except Exception:
        logger.exception("routing failed")
        return fail("routing_failed", 503, "graph", "routing failed")
    routing_ms = (time.monotonic() - routing_started) * 1000.0
    backend = snapshot.backends.get(decision.model)
    # 防御性不变式：正常引擎不可达，decision.model 必来自同一快照的 backends；真到达时无请求记录。
    if backend is None:
        decision.release()
        return _error("no_available_backend", 503)
    upstream_body, reasoning_warning = prepare_upstream_request(body, backend, decision.reasoning)
    trace[:] = present_trace(trace)
    record = _decision_record(request, decision, snapshot, body, text, reasoning_warning, routing_ms, started)
    requestlog.enqueue(record)
    outcome = _OutcomeRecorder(requestlog, record, started)
    breaker = request.app.state.breaker
    client: httpx.AsyncClient = request.app.state.http
    headers = {"x-sr-model": decision.model, "x-sr-selection": decision.selection_reason}
    if reasoning_warning:
        headers["x-sr-reasoning-warning"] = reasoning_warning
    elif decision.reasoning is not None:
        headers["x-sr-reasoning"] = decision.reasoning
    upstream_started = time.monotonic()
    release_in_handler = True
    try:
        if body.get("stream"):
            response = await _stream(client, backend, upstream_body, headers, breaker,
                                     decision.model, outcome=outcome, request=request,
                                     on_release=decision.release)
            if isinstance(response, StreamingResponse):
                release_in_handler = False
            return response
        response = await client.post(backend.url, json=upstream_body, headers=backend.headers)
        upstream_ms = (time.monotonic() - upstream_started) * 1000.0
        if response.status_code >= 500:
            breaker.failure(decision.model)
            outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                           upstream_model=backend.upstream_name, upstream_status=response.status_code,
                           upstream_ms=upstream_ms)
            return JSONResponse(status_code=502, content={"error": {"message": "upstream_error"}}, headers=headers)
        if response.status_code < 400:
            breaker.success(decision.model)
        try:
            content = response.json()
        except json.JSONDecodeError:
            content = {"raw": response.text}
        outcome.finish(outcome_state=OUTCOME_COMPLETED,
                       error_class=None if response.status_code < 400 else "client_4xx",
                       upstream_model=backend.upstream_name, upstream_status=response.status_code,
                       upstream_ms=upstream_ms,
                       usage=content.get("usage") if isinstance(content, dict) else None)
        return JSONResponse(status_code=response.status_code, content=content, headers=headers)
    except asyncio.CancelledError:
        outcome.finish(outcome_state=OUTCOME_CANCELLED, error_class="cancelled",
                       upstream_model=backend.upstream_name,
                       upstream_ms=(time.monotonic() - upstream_started) * 1000.0)
        raise
    except httpx.HTTPError:
        breaker.failure(decision.model)
        outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                       upstream_model=backend.upstream_name,
                       upstream_ms=(time.monotonic() - upstream_started) * 1000.0)
        return JSONResponse(status_code=502, content={"error": {"message": "upstream_error"}}, headers=headers)
    finally:
        if release_in_handler:
            decision.release()


def _relay(resp, *, outcome, request, backend, upstream_started: float,
           upstream_ms: float, on_release):
    async def generator():
        usage_sniffer = _SseUsage()
        first_chunk_at: float | None = None
        state = OUTCOME_INCOMPLETE
        try:
            if await _is_disconnected(request):
                state = OUTCOME_CLIENT_DISCONNECTED
                return
            async for chunk in resp.aiter_raw():
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                usage_sniffer.feed(chunk)
                yield chunk
            state = OUTCOME_COMPLETED
        except GeneratorExit:
            state = OUTCOME_CLIENT_DISCONNECTED
            raise
        except asyncio.CancelledError:
            state = OUTCOME_CLIENT_DISCONNECTED if await _is_disconnected(request) else OUTCOME_CANCELLED
            raise
        except Exception:
            state = OUTCOME_UPSTREAM_ERROR
            raise
        finally:
            try:
                await resp.aclose()
            except Exception:
                logger.warning("upstream stream close failed", exc_info=True)
            ttft_ms = None if first_chunk_at is None else (first_chunk_at - upstream_started) * 1000.0
            usage_sniffer.finish()
            outcome.finish(
                outcome_state=state,
                error_class="upstream_error" if state == OUTCOME_UPSTREAM_ERROR else None,
                upstream_model=backend.upstream_name,
                upstream_status=resp.status_code,
                upstream_ms=upstream_ms,
                ttft_ms=ttft_ms,
                usage=usage_sniffer.usage,
            )
            on_release()
    return generator()


async def _stream(client, backend, body, headers, breaker, model, *,
                  outcome, request, on_release):
    upstream_started = time.monotonic()
    upstream_request = client.build_request(
        "POST", backend.url, json=body, headers=backend.headers,
        timeout=httpx.Timeout(60.0, read=None),
    )
    response = await client.send(upstream_request, stream=True)
    upstream_ms = (time.monotonic() - upstream_started) * 1000.0
    if response.status_code >= 400:
        raw = await response.aread()
        await response.aclose()
        if response.status_code >= 500:
            breaker.failure(model)
            outcome.finish(outcome_state=OUTCOME_UPSTREAM_ERROR, error_class="upstream_error",
                           upstream_model=backend.upstream_name, upstream_status=response.status_code,
                           upstream_ms=upstream_ms)
            return JSONResponse(status_code=502, content={"error": {"message": "upstream_error"}}, headers=headers)
        try:
            content = json.loads(raw)
        except (ValueError, TypeError):
            content = {"raw": raw.decode("utf-8", "replace")}
        outcome.finish(outcome_state=OUTCOME_COMPLETED, error_class="client_4xx",
                       upstream_model=backend.upstream_name, upstream_status=response.status_code,
                       upstream_ms=upstream_ms)
        return JSONResponse(status_code=response.status_code, content=content, headers=headers)
    breaker.success(model)
    return StreamingResponse(
        _relay(response, outcome=outcome, request=request, backend=backend,
               upstream_started=upstream_started, upstream_ms=upstream_ms,
               on_release=on_release),
        media_type="text/event-stream",
        headers=headers,
    )
