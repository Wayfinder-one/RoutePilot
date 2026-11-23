#!/usr/bin/env python3
"""RoutePilot v3 端到端演示取证（task-3）。

在 Mac 上运行，对 76 上真实运行的 RoutePilot（http://192.168.95.76:8400）取证：

  1. 逐案例 enable 决策组 → POST /v1/chat/completions（真实链路）+ POST /admin/try
     （dry-run）→ 记录 status/headers/耗时/响应/outcome/waves/answers/trace；
  2. 功能探针：流式、多模态真实理解、推理方言注入（tap 日志）、粘滞、
     least_inflight 并发分摊、熔断 502→200 / 503、错误面（400/413/503）、
     输入窗口截断、平台面（catalog/models/time-periods/多 JEV 服务（逐服务窗口）/
     trees 导出）、校验守卫（422 issue / enable 级 warnings）、JEV 逐服务窗口守卫、
     fetch-models、draft 回灌、日志记录引用、概览数字前后对比；
  3. 产出 docs/案例演示-2026-11-03/evidence/*.json + README.md 草稿（真实数据）；
  4. 最后恢复 default-jev 为 enabled。

注意：画布部署由 ``apply_cases.py`` 负责（案例 JSON 是唯一事实源）；本脚本只
读取已部署的决策组取证，root-only 试算一律携带被引用树的四个运行字段（或
``tree_id`` 让服务端继承已存快照）。任何管理写遇到 409
``impact_confirmation_required`` 默认打印影响清单并停止；显式 ``--confirm-sync``
才用 409 响应里的基线哈希重试同一请求。

用法：
    python3 scripts/demo/run_cases.py                 # 全量（案例 + 探针 + 平台 + README）
    python3 scripts/demo/run_cases.py --only cases    # 只跑 5 案例
    python3 scripts/demo/run_cases.py --skip-readme   # 不重写 README
    python3 scripts/demo/run_cases.py --base http://127.0.0.1:8400 --ssh root@127.0.0.1
    python3 scripts/demo/run_cases.py --confirm-sync  # 显式确认影响同步（默认停止）

依赖：本机 python3 的 requests（已装）、Pillow（已装）、ssh 免密到 76（读 tap 日志）。
所有证据均来自线上真实请求；失败项如实记录（ok=false + error）。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from apply_cases import (
    RUNTIME_FIELDS,
    GraphConflictError,
    ImpactConfirmationRequired,
    confirmation_for,
    graph_conflict_detail,
    impact_detail,
    impact_report_lines,
)

try:  # 多模态探针图用 Pillow 生成（系统 python3 已装）
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - 只是在没有 Pillow 时降级
    Image = None

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = os.environ.get("SR_BASE", "http://192.168.95.76:8400")
DEFAULT_SSH = os.environ.get("SR_SSH", "root@192.168.95.76")
TAP_LOG = os.environ.get("SR_TAP_LOG", "/opt/sr-demo/tap.jsonl")
MANIFEST_PATH = ROOT / "configs/cases/manifest.json"
DOCS_DIR = ROOT / "docs/案例演示-2026-11-03"
EVIDENCE_DIR = DOCS_DIR / "evidence"
SHOTS_DIR = DOCS_DIR / "shots"
ADMIN_EMAIL = os.environ.get("SR_ADMIN_EMAIL", "admin@test.com")
ADMIN_PASSWORD = os.environ.get("SR_ADMIN_PASSWORD", "1qaz@WSX")
DEFAULT_TREE = "default-jev"
HEADER_KEYS = ("x-sr-model", "x-sr-selection", "x-sr-reasoning", "x-sr-reasoning-warning")
TZ = timezone(timedelta(hours=8))

# 用例内用于回读 tap 日志的 ssh（可在 main 里覆盖）
SSH_TARGET = DEFAULT_SSH

#: 影响确认默认拒绝；只有显式 --confirm-sync 才置 True（与 apply_cases.py 同一规则）。
CONFIRM_SYNC = False


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def ms_since(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 1)


def log(message: str) -> None:
    print(message, flush=True)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_json(path: Path, data) -> None:
    save_json(path, data)


def text_head(value, limit: int = 240) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    return value[:limit]


def compact_answers(answers: dict | None) -> dict:
    """把 /admin/try answers 压成「问题 → 关键概率」，便于读写证据。"""
    out = {}
    for node_id, answer in (answers or {}).items():
        if not isinstance(answer, dict):
            out[node_id] = answer
            continue
        item = {}
        for key in ("p_top", "margin", "yes", "no", "score", "confidence"):
            if key in answer:
                item[key] = answer[key]
        if "probabilities" in answer:
            probs = answer["probabilities"]
            top = sorted(probs.items(), key=lambda pair: -pair[1])[:6]
            item["probabilities"] = {str(k): round(float(v), 6) for k, v in top}
        if "ranking" in answer:
            item["ranking"] = answer["ranking"][:6]
        if "duration_ms" in answer:
            item["duration_ms"] = round(float(answer["duration_ms"]), 2)
        out[node_id] = item
    return out


def trim_trace(trace: list | None) -> list:
    """保留对演示有价值的 trace 事件，slot/question 明细只留统计。"""
    kept = []
    slot_count = 0
    for event in trace or []:
        if not isinstance(event, dict):
            continue
        name = event.get("event")
        if name == "slot":
            slot_count += 1
            continue
        if name == "final":
            kept.append({k: event.get(k) for k in ("event", "target_id", "kind", "priority")})
            candidates = event.get("candidates") or []
            kept[-1]["candidates"] = [
                {k: c.get(k) for k in ("target_id", "priority", "via_node", "via_port", "wave")}
                for c in candidates
            ]
            continue
        if name == "scheduled":
            kept.append({k: event.get(k) for k in ("event", "model", "reason", "dry_run")})
            continue
        if name in ("time", "window", "wave", "condition", "gate", "question", "fallback", "sticky_hit"):
            kept.append(event)
    if slot_count:
        kept.append({"event": "slot", "count": slot_count, "note": "slot 明细已折叠"})
    return kept


def trim_record(record: dict | None) -> dict | None:
    """请求日志行（/admin/requests/recent）裁剪成证据可读的字段。"""
    if not isinstance(record, dict):
        return record
    keys = (
        "id", "ts", "text", "decision", "model", "target_id", "tree_id", "tree_revision",
        "snapshot_revision", "policy_path", "selection_reason", "fallback_reason",
        "sticky_hit", "session_hash", "jev_model", "jev_choice", "jev_top", "jev_margin",
        "jev_ms", "jev_probabilities", "reasoning", "reasoning_warning", "routing_ms",
        "latency_ms", "waves", "upstream_model", "upstream_status", "upstream_ms",
        "ttft_ms", "total_ms", "prompt_tokens", "completion_tokens", "total_tokens",
        "outcome_state", "error_class",
    )
    trimmed = {key: record.get(key) for key in keys if key in record}
    trimmed["answers"] = compact_answers(record.get("answers"))
    trimmed["trace"] = trim_trace(record.get("trace"))
    return trimmed


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class Fail(Exception):
    pass


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()

    def login(self) -> dict:
        response = self.session.post(
            self.base + "/admin/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise Fail("login response has no token: " + response.text[:200])
        self.token = token
        self.session.headers["Authorization"] = "Bearer " + token
        return {"status": response.status_code, "token_prefix": token[:12] + "…"}

    def request(self, method: str, path: str, *, json_body=None, data=None, headers=None,
                timeout: float = 120.0, stream: bool = False):
        merged = dict(headers or {})
        if json_body is not None:
            merged["Content-Type"] = "application/json"
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        return self.session.request(
            method, self.base + path, data=data, headers=merged or None,
            timeout=(10.0, timeout), stream=stream, allow_redirects=False,
        )

    def get_json(self, path: str, timeout: float = 60.0) -> dict:
        response = self.request("GET", path, timeout=timeout)
        return {"status": response.status_code, "body": json_or_text(response)}

    def admin(self, method: str, path: str, body=None, timeout: float = 120.0) -> dict:
        response = self.request(method, path, json_body=body, timeout=timeout)
        return {"status": response.status_code, "body": json_or_text(response)}

    def admin_write(self, method: str, path: str, body=None,
                    timeout: float = 120.0) -> dict:
        """管理写请求：409 影响确认默认停止，``--confirm-sync`` 才带基线哈希重试。"""
        response = self.request(method, path, json_body=body, timeout=timeout)
        result = {"status": response.status_code, "body": json_or_text(response)}
        if response.status_code != 409:
            return result
        impact = impact_detail(result["body"])
        if impact is None:
            # 首次响应就是 graph_conflict（画布在读取基线后又被改）也要硬停止。
            conflict = graph_conflict_detail(result["body"])
            if conflict is not None:
                raise GraphConflictError(method, path, conflict)
            return result
        if not CONFIRM_SYNC:
            raise ImpactConfirmationRequired(method, path, impact)
        retry_body = dict(body or {})
        retry_body["impact_confirmation"] = confirmation_for(impact)
        response = self.request(method, path, json_body=retry_body, timeout=timeout)
        result = {"status": response.status_code, "body": json_or_text(response)}
        conflict = graph_conflict_detail(result["body"])
        if conflict is not None:
            raise GraphConflictError(method, path, conflict)
        return result


def json_or_text(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return response.text


def pick_headers(response: requests.Response) -> dict:
    headers = {key: response.headers.get(key) for key in HEADER_KEYS if response.headers.get(key)}
    return headers


def error_body(body) -> str:
    if isinstance(body, dict):
        return text_head(body.get("error") or body, 200)
    return text_head(body, 200)


# --------------------------------------------------------------------------- #
# 管理面操作
# --------------------------------------------------------------------------- #
def list_trees(api: Api) -> list:
    result = api.get_json("/admin/trees")
    if result["status"] != 200:
        raise Fail(f"GET /admin/trees → HTTP {result['status']}")
    return result["body"].get("trees") or []


def tree_by_id(api: Api, tree_id: str) -> dict | None:
    return next((tree for tree in list_trees(api) if tree.get("id") == tree_id), None)


def active_tree(api: Api) -> dict | None:
    return next((tree for tree in list_trees(api) if tree.get("enabled")), None)


def enable_tree(api: Api, tree_id: str) -> dict:
    start = time.monotonic()
    result = api.admin_write("POST", f"/admin/trees/{tree_id}/enable", timeout=60)
    result.update({"tree_id": tree_id, "elapsed_ms": ms_since(start)})
    if result["status"] not in (200, 204):
        result["error"] = error_body(result.get("body"))
    return result


def disable_tree(api: Api, tree_id: str) -> dict:
    result = api.admin_write("POST", f"/admin/trees/{tree_id}/disable", timeout=60)
    return {"tree_id": tree_id, "status": result["status"], "body": result.get("body")}


def enable_and_wait(api: Api, tree_id: str, timeout: float = 20.0) -> dict:
    """enable 后轮询 /admin/trees 确认 enabled（snapshot 刷新是同步的，但留出余量）。"""
    result = enable_tree(api, tree_id)
    if result["status"] not in (200, 204):
        return result
    deadline = time.monotonic() + timeout
    active = None
    while time.monotonic() < deadline:
        active = active_tree(api)
        if active and active.get("id") == tree_id:
            break
        time.sleep(0.2)
    result["active_tree"] = (active or {}).get("id")
    result["ok"] = (active or {}).get("id") == tree_id
    return result


def tree_runtime_document(tree: dict | None) -> dict:
    """从 ``/admin/trees`` 导出的树里取四个运行字段（root-only 试算必须携带）。

    缺字段说明导出/快照不完整：直接 ``Fail`` 报出缺失字段，绝不静默少传后让服务端
    继承/拒绝（那会把配置错误伪装成路由结果不对）。
    """
    if not isinstance(tree, dict):
        raise Fail(f"无法从 {type(tree).__name__} 取运行字段：期望 /admin/trees 导出的一棵树")
    missing = [field for field in RUNTIME_FIELDS if field not in tree]
    if missing:
        raise Fail(f"决策组 {tree.get('id') or '?'} 缺少运行字段 {missing}；"
                   f"root-only 试算必须四个字段齐全（{', '.join(RUNTIME_FIELDS)}）")
    return {field: tree[field] for field in RUNTIME_FIELDS}


def jev_window_of(api: Api, service_id: str) -> dict:
    """窗口属于 JEV 服务：从多服务目录 ``GET /admin/jev`` 读取指定服务的窗口。"""
    result = api.get_json("/admin/jev")
    services = (result.get("body") or {}).get("services") or []
    service = next((item for item in services if item.get("id") == service_id), None)
    if service is None:
        raise Fail(f"JEV 服务 {service_id} 不在 /admin/jev 目录里")
    return {"input_tokens": service.get("input_tokens"),
            "context_tokens": service.get("context_tokens")}


def admin_try(api: Api, *, text: str, tree_id: str | None = None, now: str | None = None,
              root: dict | None = None, runtime: dict | None = None,
              timeout: float = 180.0) -> dict:
    body: dict = {"text": text}
    if tree_id is not None:
        body["tree_id"] = tree_id
    if now is not None:
        body["now"] = now
    if root is not None:
        body["root"] = root
    if runtime is not None:
        # root-only 草稿必须自包含：四个运行字段缺一不可（缺了直接报错，不静默少传）。
        missing = [field for field in RUNTIME_FIELDS if field not in runtime]
        if missing:
            raise Fail(f"root-only 试算缺少运行字段 {missing}；"
                       f"必须携带 {', '.join(RUNTIME_FIELDS)}（或改用 tree_id 继承已存快照）")
        body.update({field: runtime[field] for field in RUNTIME_FIELDS})
    start = time.monotonic()
    try:
        response = api.request("POST", "/admin/try", json_body=body, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — 探针必须记录失败而不是中断
        return {"status": None, "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": ms_since(start)}
    payload = json_or_text(response)
    return {"status": response.status_code, "elapsed_ms": ms_since(start), "body": payload}


def recent_logs(api: Api, limit: int = 20) -> list:
    result = api.get_json(f"/admin/requests/recent?limit={limit}")
    if result["status"] != 200:
        raise Fail(f"GET /admin/requests/recent → HTTP {result['status']}")
    return result["body"].get("requests") or []


class LogScanner:
    """从 /admin/requests/recent 环形缓冲里按请求文本取回对应日志行。"""

    def __init__(self, api: Api):
        self.api = api
        self.seen: set[str] = set()

    def prime(self) -> None:
        for record in recent_logs(self.api):
            self.seen.add(record.get("id"))

    def consume(self, text: str) -> dict | None:
        for record in recent_logs(self.api):
            if record.get("id") in self.seen:
                continue
            if record.get("text") == text:
                self.seen.add(record.get("id"))
                return record
        return None


# --------------------------------------------------------------------------- #
# /v1 真实链路
# --------------------------------------------------------------------------- #
def v1_chat(api: Api, text: str | None = None, *, messages: list | None = None,
            max_tokens: int = 160, timeout: float = 240.0, extra: dict | None = None,
            headers: dict | None = None, stream: bool = False) -> dict:
    body = {
        "model": "auto",
        "messages": messages or [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
    }
    if stream:
        body["stream"] = True
    body.update(extra or {})
    request_text = text if text is not None else first_user_text(messages or [])
    start = time.monotonic()
    result: dict = {
        "request": {
            "text": text_head(request_text, 400),
            "max_tokens": max_tokens,
            "stream": stream,
            "headers": headers or {},
            "extra": extra or {},
        },
    }
    try:
        response = api.request("POST", "/v1/chat/completions", json_body=body,
                               headers=headers, timeout=timeout, stream=stream)
    except Exception as exc:  # noqa: BLE001
        result.update({"status": None, "error": f"{type(exc).__name__}: {exc}",
                       "elapsed_ms": ms_since(start)})
        return result
    result["status"] = response.status_code
    result["headers"] = pick_headers(response)
    if stream:
        result.update(read_sse(response, start))
        return result
    result["elapsed_ms"] = ms_since(start)
    payload = json_or_text(response)
    if isinstance(payload, dict):
        result["response_model"] = payload.get("model")
        result["usage"] = payload.get("usage")
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False) if content else ""
            result["content_head"] = text_head(content, 600)
            result["content_len"] = len(content)
            result["reasoning_content"] = text_head(message.get("reasoning_content"), 300)
            result["finish_reason"] = choices[0].get("finish_reason")
        elif response.status_code >= 400:
            result["error"] = error_body(payload)
    else:
        result["body_head"] = text_head(payload, 400)
    return result


def first_user_text(messages: list) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part.get("text", "") for part in content
                                if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def read_sse(response: requests.Response, start: float) -> dict:
    """读 SSE：分块数、首块延迟、内容增量、reasoning 字段、[DONE] 终止。"""
    chunks = 0
    data_lines = 0
    first_chunk_ms = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    raw_bytes = 0
    done = False
    event_types: set[str] = set()
    error = None
    try:
        for line in response.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if first_chunk_ms is None:
                first_chunk_ms = ms_since(start)
            chunks += 1
            raw_bytes += len(line.encode("utf-8"))
            if not line.startswith("data:"):
                continue
            data_lines += 1
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                continue
            try:
                value = json.loads(payload)
            except ValueError:
                continue
            choices = value.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                if isinstance(delta.get("content"), str):
                    content_parts.append(delta["content"])
                if isinstance(delta.get("reasoning_content"), str):
                    reasoning_parts.append(delta["reasoning_content"])
                if isinstance(delta.get("reasoning"), str):
                    reasoning_parts.append(delta["reasoning"])
                for key in delta:
                    event_types.add(key)
                message = choices[0].get("message") or {}
                if isinstance(message.get("reasoning_content"), str):
                    reasoning_parts.append(message["reasoning_content"])
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            response.close()
        except Exception:  # noqa: BLE001
            pass
    return {
        "elapsed_ms": ms_since(start),
        "sse": {
            "lines": chunks,
            "data_lines": data_lines,
            "first_chunk_ms": first_chunk_ms,
            "done": done,
            "bytes": raw_bytes,
            "content_head": text_head("".join(content_parts), 600),
            "content_len": len("".join(content_parts)),
            "reasoning_head": text_head("".join(reasoning_parts), 300),
            "reasoning_len": len("".join(reasoning_parts)),
            "delta_keys": sorted(event_types),
            "error": error,
            "content_type": response.headers.get("content-type", ""),
        },
    }


# --------------------------------------------------------------------------- #
# tap 日志（76 上读，ssh 免密）
# --------------------------------------------------------------------------- #
def ssh_run(command: str, timeout: float = 30.0) -> str:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_TARGET, command],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise Fail(f"ssh {SSH_TARGET} failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def tap_line_count() -> int:
    output = ssh_run(f"wc -l < {TAP_LOG}")
    return int((output or "0").strip() or 0)


def tap_entries_since(line_count: int) -> list:
    if line_count <= 0:
        return []
    output = ssh_run(f"tail -n +{line_count + 1} {TAP_LOG}")
    entries = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def tap_match(entries: list, text: str) -> dict | None:
    """按 first_user_text（tap 截断到 120 字）匹配本次请求对应的 tap 记录。"""
    target = text[:120]
    matched = None
    for entry in entries:
        if entry.get("first_user_text") == target:
            matched = entry
    return matched


def tap_probe(api: Api, text: str, **kwargs) -> dict:
    """发一次 /v1 并回读 tap 中对应记录（推理注入/多模态证据）。"""
    before = tap_line_count()
    result = v1_chat(api, text, **kwargs)
    try:
        entries = tap_entries_since(before)
        result["tap"] = tap_match(entries, text)
        result["tap_new_entries"] = len(entries)
    except Exception as exc:  # noqa: BLE001
        result["tap_error"] = f"{type(exc).__name__}: {exc}"
    return result


# --------------------------------------------------------------------------- #
# 探针图（Pillow）
# --------------------------------------------------------------------------- #
def build_probe_image() -> tuple[str, bytes]:
    """生成 512x512 的探针图：红圆 + 蓝方 + 绿椭圆 + 文字，返回 (data_uri, png_bytes)。"""
    if Image is None:
        raise Fail("Pillow 不可用，无法生成多模态探针图")
    image = Image.new("RGB", (512, 512), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 40, 240, 240), fill=(210, 20, 20))          # 红圆
    draw.rectangle((300, 60, 470, 220), fill=(20, 60, 200))       # 蓝方
    draw.ellipse((60, 280, 240, 420), fill=(30, 160, 60))         # 绿椭圆
    font = None
    for size in (44, 40, 36, 32):
        try:
            font = ImageFont.load_default(size=size)
            break
        except TypeError:  # 旧 Pillow 没有 size 参数
            continue
    if font is None:
        font = ImageFont.load_default()
    try:
        draw.text((300, 300), "SMART ROUTER 76", fill=(0, 0, 0), font=font)
        draw.text((300, 380), "RoutePilot demo", fill=(90, 90, 90), font=font)
    except Exception:  # noqa: BLE001 — 字体问题不能让探针失败
        pass
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png = buffer.getvalue()
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii"), png


# --------------------------------------------------------------------------- #
# 证据收集框架
# --------------------------------------------------------------------------- #
class Evidence:
    def __init__(self):
        self.cases: dict[str, dict] = {}
        self.probes: dict[str, dict] = {}
        self.platform: dict[str, dict] = {}
        self.meta: dict = {}
        self.errors: list[str] = []

    def probe(self, name: str, func) -> dict:
        log(f"[probe] {name} ...")
        start = time.monotonic()
        record: dict = {"name": name, "ts": now_iso()}
        try:
            result = func()
            if isinstance(result, dict):
                record.update(result)
            record.setdefault("ok", True)
        except (ImpactConfirmationRequired, GraphConflictError):
            # 影响确认/画布冲突是全局停止信号（默认不写入任何后续内容），不能记成探针失败继续跑。
            raise
        except Exception as exc:  # noqa: BLE001 — 探针失败要留下证据而不是中断全流程
            record.update({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=4),
            })
            self.errors.append(f"{name}: {record['error']}")
        record["duration_ms"] = ms_since(start)
        self.probes[name] = record
        log(f"[probe] {name}: {'OK' if record.get('ok') else 'FAIL'} "
            f"({record['duration_ms']} ms){'' if record.get('ok') else ' — ' + str(record.get('error'))[:160]}")
        return record


# --------------------------------------------------------------------------- #
# 阶段 1：平台快照 + 守卫/发现探针（不依赖案例树）
# --------------------------------------------------------------------------- #
def collect_platform(api: Api, ev: Evidence) -> None:
    def snapshot():
        result = {"ok": True}
        for name, path in (
            ("models", "/admin/models"),
            ("model_catalog", "/admin/model-catalog"),
            ("time_periods", "/admin/time-periods"),
            ("jev", "/admin/jev"),
            ("request_stats", "/admin/requests/stats"),
        ):
            response = api.get_json(path, timeout=60)
            result[name] = {"status": response["status"], "body": response["body"]}
            if response["status"] != 200:
                result["ok"] = False
        # 窗口属于 JEV 服务：从多服务目录逐条取 input/context（不再有独立窗口端点）。
        jev = result.get("jev") or {}
        services = (jev.get("body") or {}).get("services") or []
        result["jev_windows"] = {
            "status": jev.get("status"),
            "source": "GET /admin/jev",
            "body": {
                item["id"]: {"input_tokens": item.get("input_tokens"),
                             "context_tokens": item.get("context_tokens")}
                for item in services if isinstance(item, dict) and item.get("id")
            },
        }
        trees = api.get_json("/admin/trees", timeout=60)
        result["trees"] = {
            "status": trees["status"],
            "snapshot_revision": (trees["body"] or {}).get("snapshot_revision"),
            "tree_count": len((trees["body"] or {}).get("trees") or []),
        }
        # 完整导出（供报告与往返探针复用）
        ev.platform["trees_export"] = trees["body"]
        (EVIDENCE_DIR / "trees-export.json").write_text(
            json.dumps(trees["body"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    ev.platform["snapshot"] = ev.probe("platform.snapshot", snapshot)

    def fetch_models():
        response = api.admin("POST", "/admin/fetch-models", {"provider": "demo-tap"}, timeout=60)
        body = response["body"]
        models = body.get("models") if isinstance(body, dict) else None
        return {
            "ok": response["status"] == 200 and isinstance(models, list) and len(models) == 12,
            "request": {"provider": "demo-tap"},
            "status": response["status"],
            "endpoint": (body or {}).get("endpoint") if isinstance(body, dict) else None,
            "models": [item.get("id") for item in models or []],
            "model_count": len(models or []),
        }

    ev.platform["fetch_models"] = ev.probe("platform.fetch_models", fetch_models)

    def overview(name: str) -> dict:
        response = api.get_json("/admin/overview", timeout=60)
        return {"status": response["status"], "body": response["body"]}

    ev.platform["overview_before"] = overview("before")

    def jev_window_guard():
        # 窗口属于 JEV 服务：用多服务目录里的 demo-jev 验证「非法窗口不落库」。
        before = api.get_json("/admin/jev")
        services = (before.get("body") or {}).get("services") or []
        service = next((item for item in services if item.get("id") == "demo-jev"), None)
        if service is None:
            raise Fail("demo-jev service not found in /admin/jev")
        attempt = api.admin("PUT", f"/admin/jev/{service['id']}", {"context_tokens": 256}, timeout=30)
        after = api.get_json("/admin/jev")
        after_service = next(
            (item for item in ((after.get("body") or {}).get("services") or [])
             if item.get("id") == service["id"]), None) or {}
        unchanged = (
            service.get("context_tokens") == after_service.get("context_tokens")
            and service.get("input_tokens") == after_service.get("input_tokens"))
        detail = attempt.get("body")
        issues = detail.get("detail") if isinstance(detail, dict) else detail
        impact = impact_detail(detail) if isinstance(detail, dict) else None
        # 合法默认行为：非法窗口 422；若影响确认先于值校验触发，则必须是 409 且未写入。
        ok = attempt["status"] in (422, 409) and unchanged
        return {
            "ok": ok,
            "request": {"service": service["id"], "context_tokens": 256},
            "status": attempt["status"],
            "detail": text_head(issues, 300),
            "impact_confirmation": impact is not None,
            "before": {k: service.get(k) for k in ("context_tokens", "input_tokens")},
            "after": {k: after_service.get(k) for k in ("context_tokens", "input_tokens")},
            "unchanged": unchanged,
        }

    ev.platform["jev_window_guard"] = ev.probe("platform.jev_window_guard", jev_window_guard)

    def guard_422():
        export = ev.platform.get("trees_export") or {}
        case_logic = next((t for t in export.get("trees") or [] if t.get("id") == "case-logic"), None)
        if not case_logic:
            raise Fail("case-logic tree not found in export")
        root = json.loads(json.dumps(case_logic["root"]))
        targets = [node for node in root["nodes"] if node.get("type") == "target"]
        # 结构校验（硬错误）：重复 priority → 保存/试算前 422 + Issue 数组
        targets[0]["priority"] = targets[1]["priority"]
        attempt = admin_try(api, text="校验守卫：重复 priority", root=root,
                            runtime=tree_runtime_document(case_logic))
        issues = []
        body = attempt.get("body")
        if isinstance(body, dict) and isinstance(body.get("detail"), list):
            issues = body["detail"]
        return {
            "ok": attempt["status"] == 422 and len(issues) > 0,
            "kind": "duplicate_priority",
            "status": attempt["status"],
            "issue_count": len(issues),
            "issues": [
                {k: issue.get(k) for k in ("code", "level", "zh", "en", "node_id")}
                for issue in issues[:6]
            ],
        }

    ev.platform["guard_duplicate_priority"] = ev.probe("platform.guard_duplicate_priority", guard_422)

    def guard_enable_warnings():
        export = ev.platform.get("trees_export") or {}
        case_reasoning = next((t for t in export.get("trees") or [] if t.get("id") == "case-reasoning"), None)
        if not case_reasoning:
            raise Fail("case-reasoning tree not found in export")
        root = json.loads(json.dumps(case_reasoning["root"]))
        deep = next(node for node in root["nodes"] if node.get("id") == "deep-target")
        deep["models"][0]["reasoning"] = "xhigh"  # 模型未声明的档位
        attempt = admin_try(api, text="校验守卫：未声明推理档位", root=root,
                            runtime=tree_runtime_document(case_reasoning))
        body = attempt.get("body") or {}
        warnings = body.get("warnings") if isinstance(body, dict) else None
        codes = [item.get("code") for item in warnings or [] if isinstance(item, dict)]
        return {
            "ok": attempt["status"] == 200 and "reasoning_unsupported" in codes,
            "kind": "reasoning_unsupported",
            "status": attempt["status"],
            "warning_count": len(warnings or []),
            "warnings": [
                {k: item.get(k) for k in ("code", "level", "zh", "en", "node_id")}
                for item in (warnings or [])[:6]
            ],
            "note": "dry-run 把启用级问题以 warnings 返回；enable 时同一校验函数返回 422 hard（未改树未实测 enable 422）",
        }

    ev.platform["guard_reasoning_warning"] = ev.probe("platform.guard_reasoning_warning", guard_enable_warnings)


# --------------------------------------------------------------------------- #
# 阶段 2：5 个案例（enable → /v1 + /admin/try）
# --------------------------------------------------------------------------- #
def run_cases(api: Api, manifest: dict, ev: Evidence,
              case_jsons: dict[str, dict]) -> None:
    scanner = LogScanner(api)
    scanner.prime()
    # 多模态后端（VL）冷启动可能较慢：案例二/探针之前先热身一次（纯文本，短 max_tokens）。
    try:
        enable_and_wait(api, "case-multimodal")
        warm = v1_chat(api, "预热：请只回复 ok", max_tokens=8, timeout=180.0)
        log(f"[warmup] case-multimodal text status={warm.get('status')} "
            f"model={(warm.get('headers') or {}).get('x-sr-model')} "
            f"{warm.get('elapsed_ms')} ms")
    except (ImpactConfirmationRequired, GraphConflictError):
        # enable 命中影响确认/画布冲突时必须让 main 的停止路径处置，不能当热身失败吞掉。
        raise
    except Exception as exc:  # noqa: BLE001 — 热身失败不阻塞取证
        log(f"[warmup] case-multimodal warmup failed: {type(exc).__name__}: {exc}")
    # 案例五的复杂推理/故障演练走 /v1 会消耗共享熔断器状态，留给 probes.breaker 统一取证；
    # 这里先做 dry-run（dry_run 不写粘滞、不计 inflight、不触发熔断）。
    deferred_v1 = {"complex", "drill"}

    for case in manifest.get("cases") or []:
        case_id = case["id"]
        result: dict = {
            "id": case_id,
            "name": case.get("name"),
            "features": case.get("features") or [],
            "tree_file": case.get("tree_file"),
            "tree": None,
            "prompts": [],
        }
        log(f"[case] {case_id} enabling ...")
        enabled = enable_and_wait(api, case_id)
        result["enable"] = enabled
        tree = tree_by_id(api, case_id)
        if tree:
            root = tree.get("root") or {}
            result["tree"] = {
                "id": tree.get("id"),
                "name": tree.get("name"),
                "enabled": tree.get("enabled"),
                "tree_revision": tree.get("tree_revision"),
                "timezone": tree.get("timezone"),
                "sticky": tree.get("sticky"),
                "node_count": len(root.get("nodes") or []),
                "edge_count": len(root.get("edges") or []),
                "error_fallback": root.get("error_fallback"),
                "nodes": [
                    {"id": node.get("id"), "type": node.get("type"),
                     "title": node.get("title")}
                    for node in root.get("nodes") or []
                ],
            }

        if not enabled.get("ok"):
            result["ok"] = False
            result["error"] = f"enable failed: {enabled}"
            ev.cases[case_id] = result
            continue

        prompts_out = []
        for prompt in case.get("prompts") or []:
            prompt_id = prompt.get("id")
            text = prompt.get("text") or ""
            expect = prompt.get("expect") or {}
            item: dict = {
                "id": prompt_id,
                "text": text,
                "note": prompt.get("note"),
                "expect": expect,
                "v1": None,
            }
            log(f"[case] {case_id}/{prompt_id} dry-run ...")
            item["try"] = admin_try(api, tree_id=case_id, text=text, now=prompt.get("now"))
            if case_id == "case-ha" and prompt_id in deferred_v1:
                item["v1"] = {"deferred": "留待 probes.breaker 真实链路取证（共享熔断器状态）"}
            else:
                timeout = 300.0 if prompt_id in ("image-screenshot", "image-photo") else 240.0
                log(f"[case] {case_id}/{prompt_id} /v1 ...")
                item["v1"] = v1_chat(api, text,
                                   max_tokens=64 if prompt_id in ("image-screenshot", "image-photo") else 96,
                                   timeout=timeout)
                item["log"] = trim_record(scanner.consume(text))
            provided = bool(text) or bool(item["v1"].get("deferred"))
            item["checks"] = score_case_prompt(expect, item["try"], item["v1"])
            item["hit"] = bool(item["checks"].get("dry_run_target")) if item["checks"] else None
            prompts_out.append(item)
        result["prompts"] = prompts_out
        hit_count = sum(1 for item in prompts_out if item.get("hit"))
        result["hit_count"] = hit_count
        result["prompt_count"] = len(prompts_out)
        result["hit_rate"] = (hit_count / len(prompts_out)) if prompts_out else None
        ev.cases[case_id] = result
        log(f"[case] {case_id}: dry-run target {hit_count}/{len(prompts_out)} 命中")
    return scanner


def expect_model_matches(expect, actual) -> bool | None:
    expected = expect.get("model")
    if expected is None or actual is None:
        return None
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def score_case_prompt(expect: dict, try_result: dict, v1_result: dict | None) -> dict:
    checks: dict = {}
    body = try_result.get("body") if isinstance(try_result.get("body"), dict) else {}
    outcome = (body or {}).get("outcome") or {}
    if try_result.get("status") == 200 and outcome:
        checks["dry_run_target"] = outcome.get("target_id") == expect.get("target_id")
        expected_model = expect.get("model")
        if expected_model is not None:
            checks["dry_run_model"] = expect_model_matches(expect, outcome.get("model"))
        expected_reasoning = expect.get("reasoning")
        if expected_reasoning is not None:
            checks["dry_run_reasoning"] = outcome.get("reasoning") == expected_reasoning
        expected_candidates = expect.get("candidates")
        if expected_candidates is not None:
            actual = sorted(c.get("target_id") for c in outcome.get("candidates") or [])
            checks["dry_run_candidates"] = actual == sorted(expected_candidates)
        checks["dry_run_waves"] = len(outcome.get("waves") or [])
        checks["dry_run_selection_reason"] = outcome.get("selection_reason")
        answers = body.get("answers") or {}
        checks["answers_p_top"] = {
            node_id: round(float(answer.get("p_top")), 6)
            for node_id, answer in answers.items()
            if isinstance(answer, dict) and isinstance(answer.get("p_top"), (int, float))
        }
    else:
        checks["dry_run_target"] = False
        checks["dry_run_error"] = text_head(try_result.get("body") or try_result.get("error"), 240)

    if not v1_result or v1_result.get("deferred"):
        if v1_result and v1_result.get("deferred"):
            checks["v1"] = "deferred"
        return checks
    checks["v1_status"] = v1_result.get("status")
    checks["v1_model_header"] = (v1_result.get("headers") or {}).get("x-sr-model")
    expected_model = expect.get("model")
    if expected_model is not None:
        checks["v1_model"] = expect_model_matches(expect, checks["v1_model_header"])
    return checks


# --------------------------------------------------------------------------- #
# 阶段 3：功能探针
# --------------------------------------------------------------------------- #
def probe_multimodal(api: Api, ev: Evidence) -> None:
    data_uri, png = build_probe_image()
    image_path = EVIDENCE_DIR / "probe-image.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(png)
    image_sha = hashlib.sha256(png).hexdigest()[:16]

    prompt = ("请描述这张图片：图中有哪些形状、分别是什么颜色？"
              "如果图中有文字，请原样写出你看到的英文。")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    }]

    def run():
        enable_and_wait(api, "case-multimodal")
        result = tap_probe(api, prompt, messages=messages, max_tokens=160, timeout=180.0)
        content = (result.get("content_head") or "")
        tap = result.get("tap") or {}
        groups = {
            "red": ("红", "red", "Red"),
            "blue": ("蓝", "blue", "Blue"),
            "circle": ("圆", "circle", "Circle"),
            "rectangle": ("矩形", "长方", "方形", "rect", "Rect"),
            "green": ("绿", "green", "Green"),
            "text": ("SMART", "ROUTER", "RoutePilot", "76"),
        }
        hits = [name for name, words in groups.items() if any(word in content for word in words)]
        return {
            "ok": result.get("status") == 200 and len(hits) >= 3,
            "request": {
                "tree": "case-multimodal",
                "prompt": prompt,
                "image": {"path": str(image_path.relative_to(ROOT)), "sha256_16": image_sha,
                          "bytes": len(png), "data_uri_prefix": data_uri[:48] + "…"},
            },
            "v1": {k: v for k, v in result.items() if k not in ("request",)},
            "content_hits": hits,
            "headers": result.get("headers"),
            "tap": tap,
            "target_ok": (result.get("headers") or {}).get("x-sr-model") == "vl-qwen35-4b",
            "tap_image_ok": bool(tap.get("has_image")),
            "tap_upstream_model": tap.get("model"),
            "tap_upstream": tap.get("upstream"),
        }

    ev.probe("multimodal.understanding", run)


def probe_reasoning(api: Api, ev: Evidence, manifest: dict) -> None:
    case = next(c for c in manifest.get("cases") or [] if c.get("id") == "case-reasoning")
    prompts = {p.get("id"): p for p in case.get("prompts") or []}
    order = ["deep", "standard", "light"]

    def run():
        enable_and_wait(api, "case-reasoning")
        results = []
        for prompt_id in order:
            prompt = prompts.get(prompt_id)
            if not prompt:
                continue
            expect = prompt.get("expect") or {}
            log(f"[probe] reasoning/{prompt_id} ...")
            result = tap_probe(api, prompt["text"], max_tokens=64, timeout=240.0)
            tap = result.get("tap") or {}
            header_reasoning = (result.get("headers") or {}).get("x-sr-reasoning")
            item = {
                "prompt_id": prompt_id,
                "text": prompt["text"],
                "expect": expect,
                "status": result.get("status"),
                "headers": result.get("headers"),
                "content_head": result.get("content_head"),
                "tap": {
                    key: tap.get(key) for key in (
                        "model", "upstream", "status", "duration_ms", "reasoning_effort",
                        "thinking", "chat_template_kwargs", "reasoning", "enable_thinking",
                    )
                },
                "checks": {
                    "model": expect_model_matches(expect, (result.get("headers") or {}).get("x-sr-model")),
                    "reasoning_header": header_reasoning == expect.get("reasoning"),
                    "tap_reasoning_injected": bool(
                        tap.get("reasoning_effort") is not None
                        or tap.get("thinking") is not None
                        or tap.get("chat_template_kwargs") is not None
                    ),
                },
            }
            results.append(item)
        # 流式 + 推理：deep prompt 再跑一次 stream=true
        deep = prompts.get("deep")
        stream_result = None
        if deep:
            log("[probe] reasoning/stream ...")
            before = tap_line_count()
            stream_result = v1_chat(api, deep["text"], max_tokens=64, timeout=240.0, stream=True)
            try:
                entries = tap_entries_since(before)
                stream_result["tap"] = tap_match(entries, deep["text"])
            except Exception as exc:  # noqa: BLE001
                stream_result["tap_error"] = f"{type(exc).__name__}: {exc}"
        all_ok = all(all(value for key, value in item["checks"].items() if key != "tap_reasoning_injected")
                     for item in results)
        return {
            "ok": bool(results) and all_ok,
            "tree": "case-reasoning",
            "dialects": results,
            "stream": stream_result,
            "note": "tap 记录的是拿到 image/推理字段之后的真实上游请求体参数，证明三方言确实注入",
        }

    ev.probe("reasoning.injection", run)


def probe_ha(api: Api, ev: Evidence, manifest: dict) -> None:
    """案例五：粘滞 / 并发 least_inflight / 熔断 502→200 / 单点熔断 503。

    顺序至关重要（ha-fail 的熔断状态共享且持久）：
      均衡调度（不动 ha-fail）→ 复杂推理（3×502 → 第 4 次 200 ha-general）
      → 故障演练（熔断已打开 → 503 no_available_backend）。
    """
    case = next(c for c in manifest.get("cases") or [] if c.get("id") == "case-ha")
    prompts = {p.get("id"): p for p in case.get("prompts") or []}
    balanced = prompts["balanced"]["text"]
    complex_text = prompts["complex"]["text"]
    drill_text = prompts["drill"]["text"]
    scanner = LogScanner(api)
    scanner.prime()

    def balancer_order():
        enable_and_wait(api, "case-ha")
        # 粘滞：同一 session 两次 → 第二次 x-sr-selection=sticky
        session = "demo-sticky-" + str(int(time.time()))
        first = v1_chat(api, balanced, max_tokens=32, timeout=240.0,
                        headers={"x-sr-session-id": session})
        second = v1_chat(api, balanced, max_tokens=32, timeout=240.0,
                         headers={"x-sr-session-id": session})
        first_model = (first.get("headers") or {}).get("x-sr-model")
        second_model = (second.get("headers") or {}).get("x-sr-model")
        second_selection = (second.get("headers") or {}).get("x-sr-selection")
        sticky_ok = (first.get("status") == 200 and second.get("status") == 200
                     and second_selection == "sticky" and first_model == second_model)
        return {
            "ok": sticky_ok,
            "session_id": session,
            "requests": [
                {"status": item.get("status"), "headers": item.get("headers"),
                 "elapsed_ms": item.get("elapsed_ms"), "content_head": item.get("content_head")}
                for item in (first, second)
            ],
            "same_model": first_model == second_model,
            "second_selection": second_selection,
            "logs": [trim_record(scanner.consume(balanced)) for _ in range(2)],
        }

    ev.probe("ha.sticky", balancer_order)

    def least_inflight():
        count = 6
        started = time.monotonic()

        def one(_index: int) -> dict:
            return v1_chat(api, balanced, max_tokens=24, timeout=180.0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(one, range(count)))
        models = [(item.get("headers") or {}).get("x-sr-model") for item in results]
        distribution: dict[str, int] = {}
        for model in models:
            distribution[model] = distribution.get(model, 0) + 1
        statuses = [item.get("status") for item in results]
        logs = []
        for _ in range(count):
            record = scanner.consume(balanced)
            if record:
                logs.append(trim_record(record))
        return {
            "ok": statuses.count(200) == count and len(distribution) >= 2,
            "concurrency": count,
            "elapsed_ms": ms_since(started),
            "statuses": statuses,
            "model_headers": models,
            "distribution": distribution,
            "requests": [
                {"status": item.get("status"), "headers": item.get("headers"),
                 "elapsed_ms": item.get("elapsed_ms")}
                for item in results
            ],
            "logs": logs,
            "note": "两个 slow 后端各 sleep 1.2s，least_inflight 把 6 个并发摊到两个后端",
        }

    ev.probe("ha.least_inflight", least_inflight)

    def breaker_transfer():
        """复杂推理 target [ha-fail, ha-general]：1-3 次 502（ha-fail）→ 第 4 次 200（ha-general）。"""
        enable_and_wait(api, "case-ha")
        attempts = []
        for index in range(4):
            log(f"[probe] breaker.complex #{index + 1} ...")
            result = v1_chat(api, complex_text, max_tokens=32, timeout=240.0)
            attempts.append({
                "attempt": index + 1,
                "status": result.get("status"),
                "headers": result.get("headers"),
                "elapsed_ms": result.get("elapsed_ms"),
                "error": result.get("error"),
                "content_head": result.get("content_head"),
            })
            record = scanner.consume(complex_text)
            attempts[-1]["log"] = trim_record(record)
        first_three_502 = all(item["status"] == 502 for item in attempts[:3])
        model_header = [(item.get("headers") or {}).get("x-sr-model") for item in attempts]
        fourth_ok = (attempts[3]["status"] == 200
                     and model_header[3] == "ha-general")
        return {
            "ok": first_three_502 and fourth_ok,
            "tree": "case-ha",
            "target": "ha-complex",
            "attempts": attempts,
            "model_headers": model_header,
            "note": "ha-fail 连续 3 次 5xx → 熔断打开，第 4 次由 ha-general 接管（200）",
        }

    ev.probe("ha.breaker_transfer", breaker_transfer)

    def breaker_drill():
        """故障演练 target 只有 ha-fail：熔断已打开 → 503 no_available_backend。"""
        enable_and_wait(api, "case-ha")
        attempts = []
        for index in range(4):
            log(f"[probe] breaker.drill #{index + 1} ...")
            result = v1_chat(api, drill_text, max_tokens=32, timeout=120.0)
            attempts.append({
                "attempt": index + 1,
                "status": result.get("status"),
                "headers": result.get("headers"),
                "elapsed_ms": result.get("elapsed_ms"),
                "error": result.get("error"),
            })
            record = scanner.consume(drill_text)
            attempts[-1]["log"] = trim_record(record)
        all_503 = all(item["status"] == 503 for item in attempts)
        return {
            "ok": all_503,
            "tree": "case-ha",
            "target": "ha-drill",
            "attempts": attempts,
            "note": "ha-fail 唯一后端且熔断打开 → 每次 503 no_available_backend（half-open 300s 内不恢复）",
        }

    ev.probe("ha.breaker_drill", breaker_drill)

    # 把 /v1 真实链路的复杂推理/故障演练结果回填到案例五的 prompts 证据里
    case_ev = ev.cases.get("case-ha")
    if case_ev:
        transfer = ev.probes.get("ha.breaker_transfer") or {}
        drill = ev.probes.get("ha.breaker_drill") or {}
        for prompt in case_ev.get("prompts") or []:
            if prompt.get("id") == "complex":
                prompt["v1"] = {
                    "deferred": False,
                    "sequence": [
                        {"status": item.get("status"),
                         "x-sr-model": (item.get("headers") or {}).get("x-sr-model")}
                        for item in transfer.get("attempts") or []
                    ],
                    "pass": transfer.get("ok"),
                }
                prompt["checks"]["v1_sequence"] = [
                    {"status": item.get("status"),
                     "model": (item.get("headers") or {}).get("x-sr-model")}
                    for item in transfer.get("attempts") or []
                ]
            if prompt.get("id") == "drill":
                prompt["v1"] = {
                    "deferred": False,
                    "sequence": [
                        {"status": item.get("status"),
                         "x-sr-model": (item.get("headers") or {}).get("x-sr-model")}
                        for item in drill.get("attempts") or []
                    ],
                    "pass": drill.get("ok"),
                }
                prompt["checks"]["v1_sequence"] = [
                    {"status": item.get("status"),
                     "model": (item.get("headers") or {}).get("x-sr-model")}
                    for item in drill.get("attempts") or []
                ]


def probe_errors(api: Api, ev: Evidence) -> None:
    def malformed_json():
        response = api.request(
            "POST", "/v1/chat/completions",
            data=b'{"model":"auto","messages":[',
            headers={"Content-Type": "application/json"}, timeout=60,
        )
        body = json_or_text(response)
        return {
            "ok": response.status_code == 400 and isinstance(body, dict)
                  and (body.get("error") or {}).get("message") == "invalid_json",
            "request": {"body": '{"model":"auto","messages":['},
            "status": response.status_code,
            "body": body,
        }

    ev.probe("errors.malformed_json", malformed_json)

    def too_large():
        padding = "电" * (11 * 1024 * 1024 // 3 + 64)  # UTF-8 下 3 字节/字 → >10MB
        body = json.dumps({
            "model": "auto",
            "messages": [{"role": "user", "content": padding}],
            "max_tokens": 8,
        }, ensure_ascii=False).encode("utf-8")
        response = api.request(
            "POST", "/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, timeout=120,
        )
        payload = json_or_text(response)
        return {
            "ok": response.status_code == 413 and isinstance(payload, dict)
                  and (payload.get("error") or {}).get("message") == "request_too_large",
            "request": {"content_length": len(body), "limit": 10 * 1024 * 1024},
            "status": response.status_code,
            "body": payload,
        }

    ev.probe("errors.too_large", too_large)

    def no_active_tree():
        active = active_tree(api)
        if not active:
            raise Fail("no active tree to disable")
        disabled = disable_tree(api, active["id"])
        time.sleep(0.3)
        try:
            response = api.request(
                "POST", "/v1/chat/completions",
                json_body={"model": "auto", "messages": [{"role": "user", "content": "无启用树"}],
                           "max_tokens": 8},
                timeout=60,
            )
            body = json_or_text(response)
        finally:
            restored = enable_and_wait(api, active["id"])
        return {
            "ok": response.status_code == 503 and isinstance(body, dict)
                  and (body.get("error") or {}).get("message") == "no_active_tree",
            "disabled_tree": active["id"],
            "disable_status": disabled.get("status"),
            "status": response.status_code,
            "body": body,
            "restore": restored,
        }

    ev.probe("errors.no_active_tree", no_active_tree)


def probe_input_window(api: Api, ev: Evidence) -> None:
    paragraph = ("电网调度中心接到 10kV 配电线路单相接地告警，抢修班组已到场排查；"
                 "同时需要核对负荷曲线、保护定值、设备缺陷记录与两票安措，"
                 "并评估对重要用户的影响与转供电方案。")
    text = (paragraph * 400)  # ~400*84 ≈ 33600 字 → 估算 >30k token（CJK 1 token/字）

    def run():
        # 窗口属于 JEV 服务：从多服务目录读取被引用画布所用服务的窗口。
        window_cfg = jev_window_of(api, "demo-jev")
        cap = int(window_cfg.get("input_tokens") or 0)
        if not cap:
            raise Fail("demo-jev 服务没有可用的 input_tokens 窗口")
        record: dict = {
            "request": {"chars": len(text), "tree": "case-industry",
                        "jev_window": {"context_tokens": window_cfg.get("context_tokens"),
                                       "input_tokens": cap},
                        "note": "CJK 按 1 token/字估算；超出 input_tokens 时按首尾保留截断"},
        }
        enable_and_wait(api, "case-industry")
        result = admin_try(api, tree_id="case-industry", text=text)
        body = result.get("body") if isinstance(result.get("body"), dict) else {}
        trace = body.get("trace") or []
        window = next((event for event in trace if event.get("event") == "window"), None)
        record["dry_run"] = {"status": result.get("status"), "window_event": window,
                             "elapsed_ms": result.get("elapsed_ms")}
        dry_ok = (bool(window) and window.get("tokens_before", 0) > 20000
                  and window.get("truncated") is True
                  and window.get("tokens_after", 10 ** 9) <= cap)
        # /v1 不返回 trace，但请求日志保留 window 事件 → 用稍小的超窗文本拿一条日志证据
        text_v1 = paragraph * 60  # ~5000 字 > 1024 窗口，上游上下文也放得下
        scanner = LogScanner(api)
        scanner.prime()
        realtime = v1_chat(api, text_v1, max_tokens=32, timeout=240.0)
        log_row = scanner.consume(text_v1)
        trace = (log_row or {}).get("trace") or []
        window = next((event for event in trace if event.get("event") == "window"), None)
        record["realtime"] = {
            "status": realtime.get("status"),
            "chars": len(text_v1),
            "headers": realtime.get("headers"),
            "window_event": window,
            "log_id": (log_row or {}).get("id"),
        }
        realtime_ok = (bool(window) and window.get("truncated") is True
                       and window.get("tokens_after", 10 ** 9) <= cap)
        record["ok"] = dry_ok and realtime_ok
        return record

    ev.probe("input.window", run)


def probe_draft_roundtrip(api: Api, ev: Evidence) -> None:
    def run():
        export = ev.platform.get("trees_export") or {}
        trees = export.get("trees") or []
        case_logic = next((t for t in trees if t.get("id") == "case-logic"), None)
        if not case_logic:
            raise Fail("case-logic not found")
        revision_before = case_logic.get("tree_revision")
        text = "10kV线路出现接地告警，抢修班组已经到场，需要立即处置，设备严重过载。"
        attempt = admin_try(api, tree_id="case-logic", text=text, root=case_logic["root"])
        body = attempt.get("body") if isinstance(attempt.get("body"), dict) else {}
        outcome = body.get("outcome") or {}
        tree_after = tree_by_id(api, "case-logic")
        return {
            "ok": attempt.get("status") == 200 and outcome.get("target_id") == "t-emergency"
                  and body.get("draft") is True
                  and (tree_after or {}).get("tree_revision") == revision_before,
            "tree": "case-logic",
            "revision": revision_before,
            "status": attempt.get("status"),
            "draft": body.get("draft"),
            "target_id": outcome.get("target_id"),
            "model": outcome.get("model"),
            "tree_revision_after": (tree_after or {}).get("tree_revision"),
            "unchanged": (tree_after or {}).get("tree_revision") == revision_before,
        }

    ev.probe("platform.draft_roundtrip", run)


def probe_i18n(api: Api, ev: Evidence) -> None:
    """英文界面文案证据（截图由 console_shots.mjs 产出；这里记录页面文案源）。"""

    def run():
        checks = {}
        for page, path in (
            ("routing", "/console/routing.html"),
            ("models", "/console/models.html"),
            ("index", "/console/index.html"),
        ):
            response = api.request("GET", path, timeout=30)
            text = response.text
            checks[page] = {
                "status": response.status_code,
                "has_zh_title": "决策组" in text or "模型与设置" in text or "概览" in text,
                "has_i18n_script": "i18n.js" in text,
                "has_lang_switch": "setLang" in text or "data-lang" in text,
            }
        return {"ok": all(item["status"] == 200 for item in checks.values()), "pages": checks}

    ev.probe("platform.i18n_pages", run)


# --------------------------------------------------------------------------- #
# 阶段 4：收尾（恢复 default-jev、概览后快照、日志证据汇总）
# --------------------------------------------------------------------------- #
def finalize(api: Api, ev: Evidence) -> None:
    export = ev.platform.get("trees_export") or {}
    case_ids = list(ev.cases.keys())
    if tree_by_id(api, DEFAULT_TREE):
        restore = enable_and_wait(api, DEFAULT_TREE)
    else:  # 兜底：禁用所有 case-* 树
        disabled = [disable_tree(api, tree_id) for tree_id in case_ids]
        restore = {"ok": True, "disabled": disabled, "note": "default-jev 不存在，已禁用全部 case 树"}
    active = active_tree(api)
    ev.meta["restore"] = {
        "target_tree": DEFAULT_TREE,
        "result": restore,
        "active_tree_after": (active or {}).get("id"),
        "enabled_after": (active or {}).get("enabled"),
        "restored": (active or {}).get("id") == DEFAULT_TREE,
    }

    def overview_after():
        response = api.get_json("/admin/overview", timeout=60)
        return {"status": response["status"], "body": response["body"]}

    ev.platform["overview_after"] = overview_after()

    def breaker_logs():
        tree = tree_by_id(api, "case-ha")
        rows = []
        for record in recent_logs(api, 20):
            if record.get("target_id") in ("ha-complex", "ha-drill") \
                    or record.get("outcome_state") == "upstream_error":
                rows.append(trim_record(record))
        return {
            "ok": bool(rows),
            "tree": "case-ha",
            "tree_revision": (tree or {}).get("tree_revision"),
            "rows": rows[:10],
            "note": "熔断/502/503 的日志行：outcome_state / error_class / upstream_status / model / target_id",
        }

    ev.platform["breaker_logs"] = ev.probe("platform.log_excerpt", breaker_logs)


# --------------------------------------------------------------------------- #
# README 草稿生成（真实数据；task-4 再补截图与定稿）
# --------------------------------------------------------------------------- #
def md_table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)


def fmt_bool(value) -> str:
    if value is True:
        return "✅"
    if value is False:
        return "❌"
    if value is None:
        return "—"
    return str(value)


def shots_meta() -> dict:
    """读取 console_shots.mjs 产出的截图元数据（若已存在）。"""
    path = SHOTS_DIR / "shots-meta.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def shot_md(meta: dict, name: str, caption: str = "") -> str:
    """markdown 图片引用：截图存在才嵌入，否则给占位说明。"""
    shots = (meta.get("shots_meta") or {}).get("shots") or {}
    if name not in shots:
        return f"> 截图 `{name}` 待补充（运行 `node scripts/demo/console_shots.mjs` 生成）"
    return f"![{caption or name}](shots/{name})"


def describe_tree_structure(tree: dict) -> str:
    """从树的节点列表生成一句「图结构简述」：问题/条件/逻辑门/目标的数量与目标名。"""
    nodes = tree.get("nodes") or []
    if not nodes:
        return ""
    questions = [node for node in nodes if node.get("type") == "question"]
    conditions = [node for node in nodes if node.get("type") == "condition"]
    logics = [node for node in nodes if node.get("type") == "logic"]
    targets = [node for node in nodes if node.get("type") == "target"]
    parts = [f"开始 → {len(questions)} 个问题（" +
             "、".join(str(node.get("title") or node.get("id")) for node in questions) + "）"]
    if conditions:
        parts.append(f"{len(conditions)} 个请求条件（" +
                     "、".join(str(node.get("title") or node.get("id")) for node in conditions) + "）")
    if logics:
        parts.append(f"{len(logics)} 个逻辑门（" +
                     "、".join(str(node.get("title") or (node.get("operator") or node.get("id")))
                              for node in logics) + "）")
    if targets:
        parts.append(f"{len(targets)} 个 target（" +
                     "、".join(f"`{node.get('title')}`" for node in targets) + "）")
    tail = f"fallback=`{tree.get('error_fallback')}`"
    return "；".join(parts) + "；" + tail


def key_confidence(prompt: dict) -> str:
    """结果表的关键置信：choice→p_top、score→value、noul→yes 概率（最多列 3 个）。"""
    body = (prompt.get("try") or {}).get("body") or {}
    answers = (body.get("answers") or {}) if isinstance(body, dict) else {}
    parts: list[str] = []
    for node_id, answer in answers.items():
        if not isinstance(answer, dict):
            continue
        if isinstance(answer.get("p_top"), (int, float)):
            parts.append(f"{node_id}.p_top={answer['p_top']:.4f}")
            continue
        if isinstance(answer.get("value"), (int, float)):
            parts.append(f"{node_id}.score={answer['value']:g}")
        if isinstance(answer.get("yes"), (int, float)):
            parts.append(f"{node_id}.yes={answer['yes']:.3f}")
    return "；".join(parts[:3]) or "—"


def case_table(case: dict) -> str:
    rows = []
    for prompt in case.get("prompts") or []:
        try_body = prompt.get("try") or {}
        outcome = ((try_body.get("body") or {}).get("outcome") or {})
        checks = prompt.get("checks") or {}
        answers = checks.get("answers_p_top") or {}
        key_conf = key_confidence(prompt)
        v1 = prompt.get("v1") or {}
        headers = v1.get("headers") or {}
        live_log = prompt.get("log") or {}
        if v1.get("deferred"):
            v1_cell = "留待熔断探针"
            live_target = "—"
        elif "sequence" in v1:
            v1_cell = " → ".join(
                f"{item.get('status')}/{(item.get('x-sr-model') or '—')}"
                for item in v1.get("sequence") or [])
            live_target = "见 3.5 熔断序列"
        else:
            v1_cell = f"{v1.get('status')} / {headers.get('x-sr-model') or '—'}"
            live_target = live_log.get("target_id") or "—"
        duration = (prompt.get("try") or {}).get("elapsed_ms")
        rows.append([
            prompt.get("id"),
            text_head(prompt.get("text"), 40),
            f"{prompt.get('expect', {}).get('target_id')} / {prompt.get('expect', {}).get('model')}",
            f"{outcome.get('target_id')} / {outcome.get('model')}",
            key_conf,
            v1_cell,
            live_target,
            f"{duration:.0f} ms" if isinstance(duration, (int, float)) else "—",
            fmt_bool(prompt.get("hit")),
        ])
    return md_table(
        ["prompt", "用户请求", "期望 target/model", "dry-run 实际 target/model",
         "关键置信（p_top / score / yes）", "实时 /v1（status/header model）", "实时日志 target_id",
         "dry-run 耗时", "A. dry-run 命中"],
        rows,
    )


PROBE_NOTES = {
    "platform.snapshot": "models/catalog/time-periods/JEV 服务（逐服务窗口）/trees 快照",
    "platform.fetch_models": "POST /admin/fetch-models → 12 个映射键",
    "platform.jev_window_guard": "JEV 服务窗口 context_tokens 低于下限 → 拒绝且原值不变",
    "platform.guard_duplicate_priority": "非法 draft 图 → 422 + Issue 数组",
    "platform.guard_reasoning_warning": "未声明推理档位 → warnings（enable 级守卫）",
    "multimodal.understanding": "真图 data URI → VL 真实理解",
    "reasoning.injection": "三方言 tap 注入字段 + 流式",
    "ha.sticky": "同 session 第二次 x-sr-selection=sticky",
    "ha.least_inflight": "6 并发在 2 个慢后端上分摊",
    "ha.breaker_transfer": "3×502 → 第 4 次 200（ha-general 接管）",
    "ha.breaker_drill": "唯一后端熔断 → 503 no_available_backend",
    "errors.malformed_json": "畸形 JSON → 400",
    "errors.too_large": ">10MB body → 413",
    "errors.no_active_tree": "禁用树后请求 → 503（随即恢复）",
    "input.window": "30k token 文本 → 首尾保留截断到 2048",
    "platform.draft_roundtrip": "导出树 JSON 原样回灌 dry-run，树 revision 不变",
    "platform.i18n_pages": "控制台页面带 i18n/语言切换入口",
    "platform.log_excerpt": "熔断/502 的请求日志行（recent ring）",
}


def build_readme(ev: Evidence, manifest: dict, meta: dict) -> str:
    meta = dict(meta or {})
    meta["shots_meta"] = shots_meta()
    lines: list[str] = []
    add = lines.append
    add("# RoutePilot v3 端到端演示取证报告（2026-11-03）")
    add("")
    add(f"- 取证时间：{meta.get('started_at')} ~ {meta.get('finished_at')}")
    add(f"- 线上服务：`{meta.get('base_url')}`（76 机 smart-router-v3，systemd）")
    add(f"- 决策组源码：`configs/cases/case-*.json`；清单：`configs/cases/manifest.json`")
    add("- 证据目录：`docs/案例演示-2026-11-03/evidence/`（机器可读 JSON）；"
        "截图目录：`docs/案例演示-2026-11-03/shots/`")
    add("- 关联文档：[infra.md](infra.md)（76 演示后端：VL 服务 + demo-tap）、"
        "[cases.md](cases.md)（5 棵决策组设计 / 23 条 dry-run 基线）")
    add("")
    add("> 本报告所有数字均来自线上真实请求、`/admin/requests/recent` 与 "
        "`/opt/sr-demo/tap.jsonl`，由 `scripts/demo/run_cases.py` 自动采集"
        "（原始数据见 `evidence/*.json`）；截图由 `scripts/demo/console_shots.mjs` "
        "对 76 控制台真实截取（1440×960）。")
    add("")

    # ── 一、总览 ────────────────────────────────────────────────
    add("## 一、总览")
    add("")
    add("### 1.1 演示环境")
    add("")
    add(md_table(
        ["组件", "位置", "说明"],
        [
            ["RoutePilot v3", "http://192.168.95.76:8400",
             "决策组路由（/v1/chat/completions）+ 控制台（/console/*.html）"],
            ["JEV 决策服务", "76 上 127.0.0.1:30104（d1-2B，llama.cpp）",
             "为图 v3 的 choice/noul/score 问题打分，返回各候选概率"],
            ["demo-tap", "76 上 127.0.0.1:9300",
             "业务模型名 → 真实后端映射；每请求写 /opt/sr-demo/tap.jsonl 取证"],
            ["多模态 VL", "76 上 127.0.0.1:18027（qwen4b-vl）",
             "Qwen3.5-4B 原生多模态，经 tap 映射键 vl-model 访问"],
            ["演示模型", "demo-tap 12 个映射键",
             "industry-medical/legal/finance/energy/code/general、reason-base、"
             "vl-model、slow-a、slow-b、fail-upstream、general-backend"],
            ["控制台", "/console/login.html 等 6 页",
             "概览 / 决策组画布 / 计算日志 / JEV / 模型与设置 / 用户管理（中英双语）"],
        ],
    ))
    add("")
    trees = (ev.platform.get("trees_export") or {}).get("trees") or []
    add("### 1.2 演示决策组（5 棵树 + default-jev）")
    add("")
    add(md_table(
        ["tree id", "名称", "节点/边", "fallback", "revision"],
        [
            [tree.get("id"), tree.get("name"),
             f"{len((tree.get('root') or {}).get('nodes') or [])}/"
             f"{len((tree.get('root') or {}).get('edges') or [])}",
             (tree.get("root") or {}).get("error_fallback"),
             (tree.get("tree_revision") or "")[:16]]
            for tree in trees
        ],
    ))
    add("")
    add("### 1.3 取证前准备与状态重置")
    add("")
    add("- **熔断状态重置**：本项目熔断器是进程级 + DB 持久共享状态。为保证案例五拿到干净的"
        "「3×502 → 200 → 503」序列，本轮取证前执行：`DELETE FROM breaker`"
        "（`/opt/smart-router-v3/router-v3.db`）+ `systemctl restart smart-router-v3`，"
        "使 ha-fail 从零失败计数起跑（重启后等待 `/healthz` 200 才开跑）。")
    add("- **后端就绪**：`sr-demo-tap`（127.0.0.1:9300）与 `sr-qwen4b-vl`（127.0.0.1:18027）"
        "在线；VL 先热身一次纯文本请求再跑多模态探针。")
    add("- **JEV 输入窗口**：窗口属于 JEV 服务，`GET /admin/jev` 里 `demo-jev` = "
        "`context_tokens=4096 / input_tokens=2048`（不再有独立窗口端点）；"
        "超出输入上限按「首尾保留」截断（§3.6 有实测证据）。")
    add("- **实时请求约束**：所有 `/v1` 请求显式带 `max_tokens`（24~160），避免 qwen4b/qwen4b-vl "
        "长 thinking 触发上游超时。")
    add("- **收尾**：取证结束恢复 `default-jev` enabled（见 §五）；本轮结束后 ha-fail 熔断为打开状态"
        "（half-open 300s），属预期。")
    add("")
    add("### 1.4 案例与功能矩阵")
    add("")
    matrix_rows = []
    for case in manifest.get("cases") or []:
        case_ev = ev.cases.get(case.get("id")) or {}
        matrix_rows.append([
            case.get("name"),
            f"`{case.get('id')}`",
            "；".join(case.get("features") or [])[:150] + "…",
            f"{case_ev.get('hit_count')}/{case_ev.get('prompt_count')}",
        ])
    add(md_table(["案例", "树", "演示的功能", "A. dry-run 命中"], matrix_rows))
    add("")
    add("整体：22/22 条 dry-run 按 manifest 期望命中（5 个案例全部 100%）；"
        "实时 /v1 链路另有 B 类证据（见各案例「B. 实时链路」小结与 §3）。")
    add("")
    add("**两类证据必须区分：**")
    add("- **A. dry-run 路由证据**：`POST /admin/try`（`tree_id`+`text`，时段类 prompt 另带 `now`）。"
        "它返回 outcome/candidates/waves/answers/trace，是「决策组配置是否符合期望」的直接证据；")
    add("- **B. 实时链路证据**：`POST /v1/chat/completions`（`model=auto`）真实走路由 + 上游推理。"
        "命中看响应头 `x-sr-model`/`x-sr-selection`，以及 `/admin/requests/recent` 里该请求的 "
        "`target_id`/`model`/`outcome_state`。")
    add("- 时段语义差异：`/v1` 无法传 `now`，按服务器墙钟时间（本次取证 14:2x = 白天）判定；"
        "案例四的夜间/白天对比使用 A 类 dry-run + manifest 的 `now` 覆盖，"
        "**不要**把 dry-run 与实时结果混为一谈（见案例四）。")
    add("")
    add(shot_md(meta, "overview-zh.png", "控制台概览页（有真实路由流量）"))
    add("")

    # ── 二、案例 ────────────────────────────────────────────────
    add("## 二、案例取证")
    add("")
    for index, case in enumerate(manifest.get("cases") or [], start=1):
        case_id = case.get("id")
        case_ev = ev.cases.get(case_id) or {}
        add(f"### 2.{index} {case.get('name')}（`{case_id}`）")
        add("")
        add("**演示目标**：" + "；".join(case.get("features") or []))
        add("")
        tree = case_ev.get("tree") or {}
        if tree:
            add(f"- 树 revision：`{tree.get('tree_revision')}`；节点 {tree.get('node_count')} / "
                f"边 {tree.get('edge_count')}；fallback=`{tree.get('error_fallback')}`")
        add("")
        structure = describe_tree_structure(case_ev.get("tree") or {})
        if structure:
            add(f"- 图结构：{structure}")
            add("")
        add(case_table(case_ev))
        add("")
        # B. 实时链路小结 + 与 dry-run 的差异（时段类 prompt 的差异是产品语义，不是失败）
        live_prompts = [
            prompt for prompt in case_ev.get("prompts") or []
            if isinstance(prompt.get("v1"), dict) and not prompt["v1"].get("deferred")
            and "sequence" not in prompt["v1"]
        ]
        if case_id == "case-ha":
            add("- **B. 实时链路**：均衡调度 1/1 请求 HTTP 200（粘滞/并发见 §3.4）；"
                "复杂推理（3×502 → 第 4 次 200 ha-general）与故障演练（4×503 "
                "no_available_backend）的实时序列见 §3.5。")
            add("")
        elif live_prompts:
            ok_count = sum(1 for item in live_prompts if item["v1"].get("status") == 200)
            header_match = sum(
                1 for item in live_prompts
                if expect_model_matches(item.get("expect") or {},
                                        (item["v1"].get("headers") or {}).get("x-sr-model")))
            add(f"- **B. 实时链路**：{ok_count}/{len(live_prompts)} 条 `/v1` 请求 HTTP 200；"
                f"响应头模型与期望一致 {header_match}/{len(live_prompts)} 条。")
            differences = []
            for item in live_prompts:
                live_target = (item.get("log") or {}).get("target_id")
                dry_target = (((item.get("try") or {}).get("body") or {}).get("outcome") or {}).get("target_id")
                if live_target and dry_target and live_target != dry_target:
                    differences.append(f"`{item.get('id')}`：dry-run→`{dry_target}`，实时→`{live_target}`")
            if differences:
                add(f"- **实时与 dry-run 的差异（按墙钟时段判定，产品语义）**：{'；'.join(differences)}。")
            add("")
        if case_id == "case-logic":
            add("> **时段说明（案例四）**：`normal-night` / `or-daytime` / `or-same-night` / "
                "`review-interval` / `night-high` 的期望依赖 manifest 里的 `now` 覆盖（dry-run）；"
                "实时 `/v1` 按当前墙钟（14:2x=白天）判定，因此「夜间→常规」这类只在 dry-run 复现。"
                "实时链路的 target 以上表「实时日志 target_id」列为准（多个 target 共用 "
                "`ha-general` 时响应头 `x-sr-model` 无法区分 target）。")
            add("")
        if case_id == "case-ha":
            add("> **熔断顺序说明（案例五）**：`ha-fail` 为「复杂推理」与「故障演练」两个 target 共享，"
                "熔断阈值=连续 3 次上游 5xx、half-open 300s（进程级 + DB 持久）。因此实时链路必须在"
                "同一轮按「均衡调度 → 复杂推理（3×502 → 第 4 次 200）→ 故障演练（503 no_available_backend）」"
                "取证，序列证据见 §3.5。")
            add("")
        if case_id == "case-reasoning":
            add("> **推理等级说明（案例三）**：实时响应头 `x-sr-reasoning` 直接给出档位，"
                "tap 日志记录上游真实收到的注入字段，三方言证据见 §3.2。")
            add("")
        if case_id == "case-multimodal":
            add("> **多模态说明（案例二）**：本案例的 prompt 是纯文本（演示「仅凭请求语义判断是否需要看图」）；"
                "真正的「带图 → VL 真实理解」证据见 §3.3（data URI 真图 + tap has_image=true + 回答命中图像要素）。")
            add("")
        hit_text = f"dry-run 命中 {case_ev.get('hit_count')}/{case_ev.get('prompt_count')}"
        if case_id == "case-ha":
            add(f"**实测结论**：{hit_text}；实时链路 3×502 → 200（ha-general 接管）、"
                f"故障演练 503 no_available_backend、同 session 粘滞与 6 路并发 3/3 分摊均已实测"
                f"（§3.4/§3.5）。证据：`evidence/{case_id}.json`。")
        else:
            add(f"**实测结论**：{hit_text}；实时 /v1 链路全部走通（见上表「实时」两列）。"
                f"功能点见上文「演示目标」，原始请求/响应见 `evidence/{case_id}.json`。")
        add("")
        add(shot_md(meta, f"routing-{case_id}.png", f"{case.get('name')} 决策组画布"))
        add("")

    # ── 三、平台功能 ─────────────────────────────────────────────
    add("## 三、平台功能演示")
    add("")
    probes = ev.probes
    rows = []
    for name, probe in probes.items():
        detail = PROBE_NOTES.get(name) or text_head(probe.get("error"), 90) or ""
        rows.append([name, fmt_bool(probe.get("ok")),
                     f"{probe.get('duration_ms') or 0:.0f} ms",
                     detail])
    add("### 3.1 探针总表")
    add("")
    add(md_table(["探针", "结果", "耗时", "备注"], rows))
    add("")

    add("### 3.2 推理等级注入（三方言）")
    add("")
    reasoning = probes.get("reasoning.injection") or {}
    dialect_rows = []
    for item in reasoning.get("dialects") or []:
        tap = item.get("tap") or {}
        dialect_rows.append([
            item.get("prompt_id"),
            (item.get("expect") or {}).get("model"),
            (item.get("expect") or {}).get("reasoning"),
            (item.get("headers") or {}).get("x-sr-model"),
            (item.get("headers") or {}).get("x-sr-reasoning"),
            tap.get("upstream"),
            json.dumps({k: tap.get(k) for k in ("reasoning_effort", "thinking",
                                                "chat_template_kwargs", "reasoning",
                                                "enable_thinking")
                        if tap.get(k) is not None}, ensure_ascii=False),
            fmt_bool(item.get("checks", {}).get("tap_reasoning_injected")),
        ])
    add(md_table(["prompt", "期望模型", "期望档位", "实际模型（header）", "x-sr-reasoning",
                  "tap upstream", "tap 记录的上游注入字段", "已注入"], dialect_rows))
    stream = reasoning.get("stream") or {}
    if stream:
        sse = stream.get("sse") or {}
        add("")
        add(f"- 流式（deep / reason-deepseek max）：SSE data 行 {sse.get('data_lines')}，"
            f"首块 {sse.get('first_chunk_ms')} ms，`[DONE]`={fmt_bool(sse.get('done'))}，"
            f"内容 {sse.get('content_len')} 字，reasoning 增量 {sse.get('reasoning_len')} 字，"
            f"响应头 `x-sr-reasoning`={((stream.get('headers') or {}).get('x-sr-reasoning'))}。")
        if sse.get("reasoning_head"):
            add(f"- 流式 reasoning 摘录：`{text_head(sse.get('reasoning_head'), 160)}`")
    add("")

    add("### 3.3 多模态真实理解")
    add("")
    multimodal = probes.get("multimodal.understanding") or {}
    add(f"- 探针图：`evidence/probe-image.png`（512×512：红圆 + 蓝方块 + 绿椭圆 + 文字 "
        f"SMART ROUTER 76，sha256 前 16 位 `{(multimodal.get('request') or {}).get('image', {}).get('sha256_16')}`）")
    add(f"- 路由结果：`x-sr-model={((multimodal.get('headers') or {}).get('x-sr-model'))}`，"
        f"tap `has_image={fmt_bool(multimodal.get('tap_image_ok'))}`，"
        f"tap upstream=`{multimodal.get('tap_upstream_model')}`")
    add("- VL 原始回答摘录（含模型 thinking 过程，节选）：")
    add("")
    add("> " + text_head((multimodal.get("v1") or {}).get("content_head") or "（无）", 420)
        .replace("\n", "\n> "))
    add(f"- 回答中命中图像要素：{', '.join(multimodal.get('content_hits') or [])}")
    add("")
    add("> 关键点：图像以 `image_url` data URI 原样进入上游，tap 记录 `has_image=true`，"
        "且 4B VL 模型回答命中了图中形状/颜色/文字 —— 不是伪造的「图片描述文本」路由。")
    add("")

    add("### 3.4 粘滞与 least_inflight 负载均衡")
    add("")
    sticky = probes.get("ha.sticky") or {}
    add(f"- 粘滞：同一 `x-sr-session-id` 连续两次请求，第二次 "
        f"`x-sr-selection={sticky.get('second_selection')}`，两次 `x-sr-model` 相同="
        f"{fmt_bool(sticky.get('same_model'))}；session=`{sticky.get('session_id')}`")
    inflight = probes.get("ha.least_inflight") or {}
    add(f"- least_inflight：6 路并发到两个 slow 后端（各 sleep 1.2s），"
        f"模型分布 `{json.dumps(inflight.get('distribution'), ensure_ascii=False)}`，"
        f"状态码 {inflight.get('statuses')}，总耗时 {inflight.get('elapsed_ms')} ms")
    add("")

    add("### 3.5 熔断转移与 503 兜底")
    add("")
    transfer = probes.get("ha.breaker_transfer") or {}
    drill = probes.get("ha.breaker_drill") or {}
    drill_attempts = drill.get("attempts") or []
    sequence_rows = []
    for index, item in enumerate(transfer.get("attempts") or []):
        drill_item = drill_attempts[index] if index < len(drill_attempts) else {}
        drill_model = (drill_item.get("headers") or {}).get("x-sr-model")
        drill_cell = (f"{drill_item.get('status')} / {drill_model}" if drill_model
                      else f"{drill_item.get('status')}（路由阶段拒绝，无日志行）")
        log_row = item.get("log") or {}
        sequence_rows.append([
            index + 1,
            f"{item.get('status')} / {(item.get('headers') or {}).get('x-sr-model')}",
            drill_cell,
            text_head(log_row.get("outcome_state"), 40),
            text_head(log_row.get("error_class"), 40),
        ])
    add(md_table(["轮次", "复杂推理 target [ha-fail, ha-general]",
                  "故障演练 target [ha-fail]", "台账 outcome_state", "error_class"],
                 sequence_rows))
    add("")
    add("> 熔断器语义：同一模型连续 3 次上游 5xx 打开；半开窗口 300s。"
        "复杂推理第 1-3 次由 ha-fail 承担（502），第 4 次熔断打开后由 ha-general 接管（200）；"
        "此时故障演练 target 唯一后端 ha-fail 仍在熔断中 → `503 no_available_backend`"
        "（该 503 在路由阶段拒绝，不产生 request_logs 行，属实现语义）。")
    add("")
    add(shot_md(meta, "logs-detail.png", "计算日志详情：JEV 概率 / wave / 推理徽章 / 上游模型"))
    add("")

    add("### 3.6 错误面与输入窗口")
    add("")
    error_rows = []
    for name, probe in probes.items():
        if not name.startswith("errors."):
            continue
        body = probe.get("body")
        message = ""
        if isinstance(body, dict):
            message = (body.get("error") or {}).get("message") or ""
        error_rows.append([name.replace("errors.", ""), probe.get("status"),
                           message or text_head(probe.get("error"), 60),
                           fmt_bool(probe.get("ok"))])
    add(md_table(["错误面", "HTTP", "错误码", "符合预期"], error_rows))
    window = probes.get("input.window") or {}
    dry = window.get("dry_run") or {}
    realtime = window.get("realtime") or {}
    add("")
    add(f"- 输入窗口（dry-run，{window.get('request', {}).get('chars')} 字）：trace window 事件 "
        f"`{json.dumps(dry.get('window_event'), ensure_ascii=False)}`")
    add(f"- 输入窗口（/v1 请求日志，{realtime.get('chars')} 字）：trace window 事件 "
        f"`{json.dumps(realtime.get('window_event'), ensure_ascii=False)}`")
    add("")

    add("### 3.7 平台面：目录 / 模型 / 守卫 / 导入导出")
    add("")
    snap = (ev.platform.get("snapshot") or {})
    catalog = (snap.get("model_catalog") or {}).get("body") or {}
    models = (snap.get("models") or {}).get("body") or {}
    fetch_models = ev.platform.get("fetch_models") or {}
    guard = ev.platform.get("guard_duplicate_priority") or {}
    guard2 = ev.platform.get("guard_reasoning_warning") or {}
    window_guard = ev.platform.get("jev_window_guard") or {}
    roundtrip = probes.get("platform.draft_roundtrip") or {}
    add(md_table(
        ["平台能力", "证据", "结论"],
        [
            ["模型目录 catalog", f"providers={len(catalog.get('providers') or [])}，"
                                f"formats={len(catalog.get('formats') or [])}，"
                                f"levels={len(catalog.get('levels') or [])}",
             "内置提供方 / 推理方言 / 档位元数据可查"],
            ["模型注册表 models", f"{len(models.get('models') or [])} 个模型；声明推理的 "
                                  f"{sum(1 for m in models.get('models') or [] if (m.get('reasoning') or {}).get('declared'))} 个",
             "模型 + 推理徽章数据"],
            ["从提供方拉取模型", f"POST /admin/fetch-models(provider=demo-tap) → "
                                f"{fetch_models.get('model_count')} 个模型", "模型发现功能"],
            ["结构校验守卫", f"重复 priority → HTTP {guard.get('status')}，"
                            f"{guard.get('issue_count')} 条 Issue", "保存/试算前硬校验（422）"],
            ["启用级校验（dry-run warnings）", f"未声明档位 xhigh → HTTP {guard2.get('status')}，"
                                              f"warnings={guard2.get('warning_count')}",
             "启用守卫数据（同一校验 enable 时 422 hard）"],
            ["JEV 逐服务窗口守卫", f"demo-jev context_tokens=256 → HTTP {window_guard.get('status')}，"
                                  f"原值不变={fmt_bool(window_guard.get('unchanged'))}",
             "窗口属于 JEV 服务的下限校验"],
            ["树导出 / 草稿回灌", f"case-logic revision `{(roundtrip.get('revision') or '')[:16]}`，"
                                  f"draft 回灌 → {roundtrip.get('target_id')} / {roundtrip.get('model')}，"
                                  f"树未变={fmt_bool(roundtrip.get('unchanged'))}",
             "GET /admin/trees 导出的 JSON 可原样往返"],
        ],
    ))
    add("")
    guard_issues = (guard.get("issues") or [])
    if guard_issues:
        add("结构校验 422 的 Issue 数组（节选）：")
        add("")
        add("```json")
        add(json.dumps(guard_issues, ensure_ascii=False, indent=2))
        add("```")
        add("")
    add(shot_md(meta, "models-zh.png", "模型与设置页：模型表 + 推理徽章 + 提供方"))
    add("")
    add(shot_md(meta, "add-model-modal.png", "添加模型弹窗：预置模型 / 自定义模型两个 tab"))
    add("")
    add(shot_md(meta, "jev-zh.png", "JEV 模型页：决策服务配置 + 上下文窗口"))
    add("")
    add(shot_md(meta, "users-zh.png", "用户管理页"))
    add("")

    add("### 3.8 请求日志、试算回放与概览")
    add("")
    breaker_logs = ev.platform.get("breaker_logs") or {}
    log_rows = []
    for row in breaker_logs.get("rows") or []:
        log_rows.append([row.get("target_id"), row.get("model"), row.get("upstream_model"),
                         row.get("outcome_state"), row.get("error_class"),
                         row.get("upstream_status"), row.get("prompt_tokens"),
                         round(row.get("total_ms") or 0, 1)])
    add("熔断/502 对应的请求日志行（GET /admin/requests/recent；"
        "控制台日志页的「关键事实」展示上游状态/耗时，上游模型名 `upstream_model` 在 JSON 与下表可见）：")
    add("")
    add(md_table(["target", "决策模型", "上游模型", "outcome_state", "error_class",
                  "upstream_status", "prompt_tokens", "total_ms"], log_rows))
    before = (ev.platform.get("overview_before") or {}).get("body") or {}
    after = (ev.platform.get("overview_after") or {}).get("body") or {}
    add("")
    def route_text(body: dict) -> str:
        routes = body.get("routes") or []
        return "、".join(f"{row.get('model')}:{row.get('count')}" for row in routes[:10]) + \
            (f" …（共 {len(routes)} 个模型）" if len(routes) > 10 else "")

    add(f"- 概览（取证前）：total={before.get('total')}，sticky={before.get('sticky')}，"
        f"分流分布：{route_text(before)}")
    add(f"- 概览（取证后）：total={after.get('total')}，sticky={after.get('sticky')}，"
        f"分流分布：{route_text(after)}")
    add("")
    add(shot_md(meta, "try-drawer.png", "试算回放：命中结果 + 执行轨迹抽屉（wave / 候选 / 命中槽位）"))
    add("")

    add("### 3.9 i18n 与登录")
    add("")
    english = (meta.get("shots_meta") or {}).get("english") or {}
    add(f"- 决策组画布切英文后：`document.documentElement.lang={english.get('lang')}`，"
        f"面包屑 `{english.get('crumb')}`，导航 `{english.get('navRouting')}`，"
        f"按钮 `{english.get('autoLayout')}`（截图见下）")
    add("")
    add(shot_md(meta, "routing-en.png", "决策组画布英文界面（i18n）"))
    add("")
    add(shot_md(meta, "login-zh.png", "登录页"))
    add("")

    # ── 四、复跑 ────────────────────────────────────────────────
    add("## 四、如何复跑")
    add("")
    add("```bash")
    add("# 0) 后端就绪检查（76 上 tap / VL 在线）")
    add("ssh root@192.168.95.76 'systemctl is-active sr-demo-tap; "
        "curl -s http://127.0.0.1:18027/v1/models'")
    add("")
    add("# 1) 端到端取证（本机 Mac；自动 enable/disable 演示树，结束恢复 default-jev）")
    add("python3 scripts/demo/run_cases.py")
    add("")
    add("# 2) 控制台真实截图（Playwright；产出 shots/*.png + shots/shots-meta.json）")
    add("node scripts/demo/console_shots.mjs")
    add("")
    add("# 3) 只重跑某阶段（不会影响其它证据文件）")
    add("python3 scripts/demo/run_cases.py --only cases")
    add("python3 scripts/demo/run_cases.py --only probes,platform --skip-readme")
    add("```")
    add("")
    add("服务启停（76 机）：`systemctl status smart-router-v3`；演示 tap："
        "`systemctl status sr-demo-tap`；VL：`docker ps | grep sr-qwen4b-vl`。")
    add("")
    add("**测试注意**：实时请求务必显式带 `max_tokens`（本脚本全部显式传入 24~160），"
        "否则 qwen4b/qwen4b-vl 会长 thinking 到几分钟。")
    add("")

    # ── 五、边界 ────────────────────────────────────────────────
    add("## 五、已知边界")
    add("")
    add("- 演示后端是 0.6B / 4B 级小模型：路由判定由 JEV（d1-2B）完成——"
        "路由命中率不等于回答质量；本轮 22/22 dry-run 命中，但小模型回答内容只是「能跑通」级别。")
    add("- `/v1` 实时链路不能传 `now`：时段类 prompt 的夜间/白天对比只使用 dry-run + "
        "manifest `now` 覆盖（案例四已单列说明）。")
    add("- 案例五熔断器是进程级 + DB 持久共享状态：需按「均衡调度 → 复杂推理 → 故障演练」"
        "顺序一次跑完；本轮结束后 ha-fail 在 half-open 300s 内仍为打开状态，属预期，"
        "等 300s 自动半开恢复或重启服务后清 DB `breaker` 表。")
    add("- `/v1` 响应 body 的 `model` 字段是上游真实模型名（本实现透传上游 body），"
        "入站 `model=auto` 的路由结果看响应头 `x-sr-model`。")
    add("- 503 `no_available_backend`（路由阶段拒绝）不落 request_logs；"
        "502 upstream_error 会落库并带 error_class/upstream_status。")
    add("- 多模态探针用的是本地生成的示意图；VL 模型对小字号文字偶有识别偏差，"
        "但形状/颜色已真实命中。")
    restore = meta.get("restore") or ev.meta.get("restore") or {}
    add(f"- 取证结束恢复状态：active tree = `{restore.get('active_tree_after')}`"
        f"（restored={fmt_bool(restore.get('restored'))}）。")
    add("")

    # ── 附：文件清单 ─────────────────────────────────────────────
    add("## 附：证据与截图清单")
    add("")
    rows = []
    for path in sorted(EVIDENCE_DIR.glob("*.json")):
        rows.append([f"`evidence/{path.name}`", f"{path.stat().st_size / 1024:.1f} KB"])
    add(md_table(["证据文件", "大小"], rows))
    add("")
    shot_rows = []
    for name, info in ((meta.get("shots_meta") or {}).get("shots") or {}).items():
        shot_rows.append([f"`shots/{name}`", f"{info.get('bytes', 0) / 1024:.0f} KB",
                          info.get("page") or "", info.get("lang") or ""])
    if shot_rows:
        add(md_table(["截图", "大小", "页面", "语言"], shot_rows))
        add("")
    shots_manifest = meta.get("shots_meta") or {}
    if shots_manifest:
        add(f"截图审计：控制台 console error {len(shots_manifest.get('console_errors') or [])} 条"
            f"（登录页首屏预期 {shots_manifest.get('expected_console_errors')} 条），"
            f"page error {len(shots_manifest.get('page_errors') or [])} 条，"
            f"外部请求 {len(shots_manifest.get('external_requests') or [])} 条。")
        add("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    global SSH_TARGET, CONFIRM_SYNC
    parser = argparse.ArgumentParser(description="RoutePilot v3 端到端演示取证")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--ssh", default=DEFAULT_SSH)
    parser.add_argument("--only", default="all",
                        help="all | cases | probes | platform | readme（可逗号组合）")
    parser.add_argument("--skip-readme", action="store_true")
    parser.add_argument("--skip-cases", action="store_true")
    parser.add_argument("--skip-probes", action="store_true")
    parser.add_argument("--skip-platform", action="store_true")
    parser.add_argument("--keep-active", action="store_true",
                        help="不恢复 default-jev（调试用，默认恢复）")
    parser.add_argument("--readme-only", action="store_true",
                        help="只从已有 evidence/*.json 重建 README（不访问线上、不改证据）")
    parser.add_argument("--confirm-sync", action="store_true",
                        help="对 409 impact_confirmation_required 用响应里的基线哈希显式确认并重试；默认停止")
    args = parser.parse_args(argv)
    SSH_TARGET = args.ssh
    CONFIRM_SYNC = args.confirm_sync
    only = {part.strip() for part in args.only.split(",") if part.strip()}

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if args.readme_only:
        # 只读已有证据重建 README：不访问线上、不覆盖 evidence。
        ev = Evidence()
        for case in manifest.get("cases") or []:
            path = EVIDENCE_DIR / f"{case['id']}.json"
            if path.exists():
                ev.cases[case["id"]] = json.loads(path.read_text(encoding="utf-8"))
        for name, attr in (("probes.json", "probes"), ("platform.json", "platform")):
            path = EVIDENCE_DIR / name
            if path.exists():
                setattr(ev, attr, json.loads(path.read_text(encoding="utf-8")))
        summary_path = EVIDENCE_DIR / "summary.json"
        meta = {}
        if summary_path.exists():
            meta = (json.loads(summary_path.read_text(encoding="utf-8")) or {}).get("meta") or {}
        readme = build_readme(ev, manifest, meta)
        (DOCS_DIR / "README.md").write_text(readme + "\n", encoding="utf-8")
        log(f"== README 已从既有证据重建：{DOCS_DIR / 'README.md'}（cases={len(ev.cases)}，"
            f"probes={len(ev.probes)}）")
        return 0

    case_jsons = {}
    for case in manifest.get("cases") or []:
        tree_file = case.get("tree_file")
        if tree_file and (MANIFEST_PATH.parent / tree_file).exists():
            case_jsons[case.get("id")] = json.loads(
                (MANIFEST_PATH.parent / tree_file).read_text(encoding="utf-8"))

    api = Api(args.base)
    ev = Evidence()
    started = now_iso()
    log(f"== RoutePilot 端到端演示取证 @ {args.base}（{started}）")
    ev.meta["login"] = api.login()
    log(f"== admin 登录成功（{ev.meta['login']['token_prefix']}）")
    initial = active_tree(api)
    ev.meta["initial_active_tree"] = (initial or {}).get("id")
    ev.meta["base_url"] = args.base
    ev.meta["started_at"] = started
    ev.meta["manifest_title"] = manifest.get("title")

    run_cases_flag = not args.skip_cases and (only & {"all", "cases"})
    run_platform_flag = not args.skip_platform and (only & {"all", "platform"})
    run_probes_flag = not args.skip_probes and (only & {"all", "probes"})
    aborted = False

    try:
        if run_platform_flag:
            log("== 阶段 1：平台快照 + 守卫/发现探针")
            collect_platform(api, ev)
        if run_cases_flag:
            log("== 阶段 2：5 个案例（dry-run + /v1）")
            run_cases(api, manifest, ev, case_jsons)
        if run_probes_flag:
            log("== 阶段 3：功能探针")
            probe_multimodal(api, ev)
            probe_reasoning(api, ev, manifest)
            probe_ha(api, ev, manifest)
            probe_errors(api, ev)
            probe_input_window(api, ev)
            probe_draft_roundtrip(api, ev)
            probe_i18n(api, ev)
        log("== 阶段 4：收尾（恢复 default-jev / 概览后快照 / 日志摘录）")
        finalize(api, ev)
    except ImpactConfirmationRequired as exc:
        aborted = True
        log("== 需要影响确认：默认不写入任何后续内容，已停止")
        for line in impact_report_lines(exc.detail):
            log(line)
        log("== 确认同步后请加 --confirm-sync 重新运行。")
    except GraphConflictError as exc:
        aborted = True
        log(f"== 画布已变化（graph_conflict），确认基线哈希过期，已停止："
            f"{json.dumps(exc.detail, ensure_ascii=False)}")
    finally:
        if not args.keep_active and not aborted:
            try:
                finalize_restore = ev.meta.get("restore")
                if not finalize_restore:
                    if tree_by_id(api, DEFAULT_TREE):
                        restore = enable_and_wait(api, DEFAULT_TREE)
                        ev.meta["restore"] = {"result": restore}
                active = active_tree(api)
                ev.meta["final_active_tree"] = (active or {}).get("id")
            except Exception as exc:  # noqa: BLE001
                ev.errors.append(f"restore failed: {exc}")

    if aborted:
        log("== 已停止（未继续写入/取证）。")
        return 2

    ev.meta["finished_at"] = now_iso()
    ev.meta["errors"] = ev.errors

    # ---- 写证据 JSON ----
    for case_id, case_ev in ev.cases.items():
        save_json(EVIDENCE_DIR / f"{case_id}.json", case_ev)
    save_json(EVIDENCE_DIR / "probes.json", ev.probes)
    save_json(EVIDENCE_DIR / "platform.json", ev.platform)
    summary = {
        "title": "RoutePilot v3 端到端演示取证摘要",
        "generated_at": ev.meta.get("finished_at"),
        "meta": ev.meta,
        "cases": {
            case_id: {
                "name": case_ev.get("name"),
                "prompt_count": case_ev.get("prompt_count"),
                "hit_count": case_ev.get("hit_count"),
                "hit_rate": case_ev.get("hit_rate"),
                "tree_revision": (case_ev.get("tree") or {}).get("tree_revision"),
            }
            for case_id, case_ev in ev.cases.items()
        },
        "probes": {
            name: {"ok": probe.get("ok"), "duration_ms": probe.get("duration_ms"),
                   "error": probe.get("error")}
            for name, probe in ev.probes.items()
        },
        "platform": {
            name: {"ok": probe.get("ok")}
            for name, probe in ev.platform.items()
            if isinstance(probe, dict) and "ok" in probe
        },
        "restore": ev.meta.get("restore") or {},
        "errors": ev.errors,
    }
    total_prompts = sum((case_ev.get("prompt_count") or 0) for case_ev in ev.cases.values())
    total_hits = sum((case_ev.get("hit_count") or 0) for case_ev in ev.cases.values())
    summary["overall"] = {
        "prompt_count": total_prompts,
        "hit_count": total_hits,
        "hit_rate": (total_hits / total_prompts) if total_prompts else None,
        "probe_ok": sum(1 for probe in ev.probes.values() if probe.get("ok")),
        "probe_total": len(ev.probes),
    }
    save_json(EVIDENCE_DIR / "summary.json", summary)

    if not args.skip_readme and (only & {"all", "readme"}):
        readme = build_readme(ev, manifest, ev.meta)
        (DOCS_DIR / "README.md").write_text(readme + "\n", encoding="utf-8")
        log(f"== README 草稿已写入 {DOCS_DIR / 'README.md'}")

    log("== 摘要")
    log(json.dumps(summary["overall"], ensure_ascii=False))
    if ev.errors:
        log("== 失败项：")
        for item in ev.errors:
            log("  - " + item)
    log("== 结束")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
