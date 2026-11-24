#!/usr/bin/env python3
"""RoutePilot v4 department-aware standalone routing smoke test.

Usage
-----
    python scripts/standalone_v4_smoke.py          # same entry as the docs
    .venv/bin/python scripts/standalone_v4_smoke.py
    .venv/bin/python scripts/standalone_v4_smoke.py --keep   # keep temp workspace

This is the final Task 11 acceptance run.  It is a *real* end-to-end smoke: the
script starts mock upstream HTTP servers and real ``python -m router`` processes
against a throwaway temp workspace, then exercises the running data plane over
HTTP.  Nothing is asserted by importing the application in-process.

What runs
---------
* mock model upstream on ``127.0.0.1:18080``: handles ``POST /v1/chat/completions``
  (OpenAI ``choices`` JSON, and chunked SSE frames + ``data: [DONE]`` with a
  deliberate delay between frames so full-response buffering is detectable);
  records path/model/Authorization/stream flag for every request;
* mock JEV service on ``127.0.0.1:18082``: returns a valid ``answers`` shape
  (the smoke graphs have no question nodes, so it must record zero calls);
* writer app ``python -m router`` on ``127.0.0.1:18400`` with a temp
  ``ROUTER_DB`` / ``ROUTER_GRAPH_DIR``, ``ROUTER_USER_AUTH=0``;
* read-only replica on ``127.0.0.1:18081`` with ``ROUTER_GRAPH_WRITER=0``
  sharing the same DB and graph directory.

Checks
------
1. mock model upstream + JEV servers start after the four ports are preflighted;
2. ``/readyz`` polling until ready (never a blind sleep);
3. main (default) + finance (财务部) + legal (法务部) v4 graphs created/enabled;
4. public ``/v1`` JSON -> default graph ``main``/``fast`` (+ response headers);
5. public ``/v1`` SSE -> live streaming, ``[DONE]``, frames measurably spaced;
6. forged ``X-SR-Department`` -> 400 ``department_header_not_allowed``;
7. ``/admin/try`` department selection matrix (finance/legal/main/main);
8. cross-graph same-model isolation: three ``main``/``fast`` upstream 500s -> 502
   open only main's circuit, a ``twin`` graph (镜像部, same logical model
   ``fast``, different tree id) still succeeds and its success does not reset
   main's circuit;
9. default switch to ``legal`` and public data-plane follow-up;
10. disabling the default graph while others stay enabled -> 409
    ``default_graph_required``;
11. empty enabled set: ``/readyz`` still ready, public ``/v1`` 503
    ``no_active_tree``;
12. corrupt one enabled graph -> ``stale: true`` and old route set keeps serving;
    restore the bytes -> ``stale: false``;
13. read-only replica matches the writer's snapshot revision/fingerprint and
    tree lifecycle, including catching up after the writer enables a graph;
14. SIGTERM clean shutdown of both processes, mock shutdown, temp cleanup.

Any failure prints the failed check, process log tails and mock evidence, then
exits non-zero.  ``--keep`` retains the temp workspace for debugging.

The run is interrupt-safe: SIGINT/SIGTERM handlers terminate the spawned router
processes and stop the mock servers before exiting non-zero (130/143).  The temp
workspace is still removed unless ``--keep`` was passed.

Ports 18080/18081/18082/18400 must be free; the script fails fast otherwise.
No third-party imports are used here (the project venv is only used to run the
real application process).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent

HOST = "127.0.0.1"
UPSTREAM_PORT = 18080
REPLICA_PORT = 18081
JEV_PORT = 18082
APP_PORT = 18400
APP_BASE = f"http://{HOST}:{APP_PORT}"
REPLICA_BASE = f"http://{HOST}:{REPLICA_PORT}"
UPSTREAM_BASE = f"http://{HOST}:{UPSTREAM_PORT}"
JEV_BASE = f"http://{HOST}:{JEV_PORT}"

READY_TIMEOUT = 30.0
STALE_TIMEOUT = 20.0
REPLICA_CATCHUP_TIMEOUT = 20.0
PROCESS_STOP_TIMEOUT = 15.0

#: Mock SSE frames: content plus the delay before the *next* frame, so a buffered
#: response collapses the first-to-last gap and the streaming assertion fails.
STREAM_FRAME_DELAY = 0.15
STREAM_CHUNKS = ("你", "好", "！")
MOCK_ANSWER = "mock upstream answer"

MOCK_CHAT_PATH = "/v1/chat/completions"


class SmokeFailure(RuntimeError):
    """A smoke check failed (message is printed verbatim before exit 1)."""


class TransportError(RuntimeError):
    """The app was not reachable yet (used by /readyz polling)."""


class SmokeInterrupted(BaseException):
    """Raised from the SIGINT/SIGTERM handler so the cleanup path always runs."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"interrupted by {_signal_name(signum)}")


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover - unknown/real-time signal number
        return f"signal {signum}"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: Any
    raw: bytes


def _brief(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def require(condition: Any, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def expect(response: Response, status: int, what: str) -> Response:
    if response.status != status:
        raise SmokeFailure(
            f"{what}: expected HTTP {status}, got {response.status}: {_brief(response.body)}")
    return response


def call(base: str, method: str, path: str, body: Any = None, *,
         headers: dict[str, str] | None = None,
         timeout: float = 15.0) -> Response:
    """JSON HTTP call; returns the response for any HTTP status (4xx/5xx included)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json"} if data is not None else {}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        base + path, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_headers = {
                key.lower(): value for key, value in response.headers.items()}
            raw = response.read()
    except urllib.error.HTTPError as exc:  # subclass of URLError: keep first
        status = exc.code
        response_headers = {
            key.lower(): value for key, value in exc.headers.items()}
        raw = exc.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise TransportError(f"{method} {path}: {exc}") from exc
    try:
        decoded: Any = json.loads(raw) if raw else {}
    except ValueError:
        decoded = raw.decode("utf-8", "replace")
    return Response(status=status, headers=response_headers, body=decoded, raw=raw)


# ---------------------------------------------------------------------------
# Graphs (adapted verbatim from the task brief, plus the four runtime fields)
# ---------------------------------------------------------------------------
def graph_doc(tree_id: str, model: str, *, departments=(), enabled=False,
              default=False) -> dict:
    return {
        "id": tree_id,
        "name": tree_id,
        "departments": sorted(departments),
        "enabled": enabled,
        "default": default,
        "timezone": "Asia/Shanghai",
        "sticky": {"enabled": False, "ttl_seconds": 1800},
        "root": {
            "id": "root", "type": "graph",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "target", "type": "target", "priority": 1,
                 "models": [{"model": model, "reasoning": None}]},
            ],
            "edges": [{
                "id": "e-start-target", "source": "start", "source_port": "out",
                "target": "target", "target_port": "in_1", "condition": None,
            }],
            "error_fallback": "target",
        },
        "backends": {
            "providers": [{
                "name": "mock", "base_url": f"{UPSTREAM_BASE}/v1",
                "key_env": "", "reasoning_format": "openai",
            }],
            "models": [{
                "id": model, "upstream": model, "provider": "mock", "profile": {},
            }],
        },
        "jev": None,
        "jev_window": {"input_tokens": 1024, "context_tokens": 4096},
        "time_periods": [],
    }


def create_and_enable(base: str, document: dict) -> None:
    """POST create (lifecycle omitted) -> enable -> departments -> default."""
    payload = {key: value for key, value in document.items()
               if key not in {"enabled", "default"}}
    expect(call(base, "POST", "/admin/trees", payload), 201,
           f"create {document['id']}")
    expect(call(base, "POST", f"/admin/trees/{document['id']}/enable"), 200,
           f"enable {document['id']}")
    if document["departments"]:
        current = expect(call(base, "GET", f"/admin/trees/{document['id']}"), 200,
                         f"get {document['id']}")
        expect(call(base, "PUT", f"/admin/trees/{document['id']}/departments", {
            "departments": document["departments"],
            "base_document_sha256": current.body["tree"]["document_sha256"],
        }), 200, f"departments {document['id']}")
    if document["default"]:
        expect(call(base, "POST", f"/admin/trees/{document['id']}/default"), 200,
               f"default {document['id']}")


def python_executable() -> str:
    """The project venv interpreter (the real app process must run in it)."""
    candidate = REPO_ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError as exc:
            raise SmokeFailure(
                f"port {port} is not free ({exc}); another smoke/server is running, "
                "stop it and rerun") from exc


def _tail_file(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = text.splitlines()[-lines:]
    return "\n".join(tail)


# ---------------------------------------------------------------------------
# Mock upstream / JEV HTTP servers
# ---------------------------------------------------------------------------
class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, records: list, lock: threading.Lock):
        super().__init__(address, handler)
        self.records = records
        self.records_lock = lock
        #: Controllable failure queue: each entry is the HTTP status the next
        #: request must answer with instead of the normal mock response. The
        #: smoke uses it to open exactly one graph's circuit on demand.
        self._failures: list[int] = []
        self._failures_lock = threading.Lock()

    def record(self, entry: dict) -> None:
        with self.records_lock:
            self.records.append(entry)

    def snapshot(self) -> list[dict]:
        with self.records_lock:
            return list(self.records)

    def fail_next(self, count: int, status: int = 500) -> None:
        with self._failures_lock:
            self._failures.extend([status] * count)

    def take_failure(self) -> int | None:
        with self._failures_lock:
            return self._failures.pop(0) if self._failures else None

    def pending_failures(self) -> int:
        with self._failures_lock:
            return len(self._failures)

    def clear_failures(self) -> None:
        with self._failures_lock:
            self._failures.clear()


class MockUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _RecordingServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        started = time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        entry = {
            "path": self.path,
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "authorization": self.headers.get("Authorization"),
            "messages": body.get("messages"),
            "started_at": started,
        }
        self.server.record(entry)
        failure_status = self.server.take_failure()
        if failure_status is not None:
            entry["failure_status"] = failure_status
        try:
            if failure_status is not None:
                self._send_failure(failure_status)
            elif entry["stream"]:
                self._send_stream()
            else:
                self._send_json(entry["model"])
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            entry["finished_at"] = time.monotonic()

    def _send_failure(self, status: int) -> None:
        payload = json.dumps({
            "error": {
                "message": f"mock upstream failure {status}",
                "type": "upstream_error",
            },
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _send_json(self, model) -> None:
        payload = json.dumps({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or "mock",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": MOCK_ANSWER},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _send_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for index, text in enumerate(STREAM_CHUNKS):
            if index:
                time.sleep(STREAM_FRAME_DELAY)
            frame = json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "mock",
                "choices": [{
                    "index": 0, "delta": {"content": text}, "finish_reason": None,
                }],
            }, ensure_ascii=False).encode("utf-8")
            self._write_chunk(b"data: " + frame + b"\n\n")
        time.sleep(STREAM_FRAME_DELAY)
        self._write_chunk(b"data: [DONE]\n\n")
        self._write_chunk(b"")

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, fmt: str, *args: Any) -> None:  # silence request logs
        return


class MockJevHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _RecordingServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        self.server.record({
            "path": self.path,
            "model": body.get("model"),
            "authorization": self.headers.get("Authorization"),
            "questions": body.get("questions"),
        })
        try:
            payload = json.dumps(
                {"answers": self._answers(body), "usage": {"input_tokens": 1,
                                                           "output_tokens": 0}},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _answers(body: dict) -> dict:
        answers = {}
        questions = body.get("questions")
        if isinstance(questions, dict):
            for key, question in questions.items():
                kind = question.get("type") if isinstance(question, dict) else None
                criteria = question.get("criteria") if isinstance(question, dict) else None
                if kind == "choice":
                    labels = list(criteria) if isinstance(criteria, dict) else []
                    labels = labels or ["候选 A", "候选 B"]
                    share = 1.0 / len(labels)
                    answers[key] = {
                        "type": "choice",
                        "probabilities": {label: share for label in labels},
                    }
                elif kind == "score":
                    levels = criteria if isinstance(criteria, list) and criteria else [0.0, 1.0]
                    answers[key] = {
                        "type": "score", "score": 0,
                        "legend": {"0": levels[0]},
                    }
                else:
                    answers[key] = {"type": "noul", "noul": 0.5}
        return answers

    def log_message(self, fmt: str, *args: Any) -> None:  # silence request logs
        return


# ---------------------------------------------------------------------------
# Smoke runner
# ---------------------------------------------------------------------------
class Smoke:
    def __init__(self, *, keep: bool = False):
        self.keep = keep
        self.workspace = ""
        self.db_path = ""
        self.graph_dir = ""
        self.log_dir = ""
        self.upstream: _RecordingServer | None = None
        self.jev: _RecordingServer | None = None
        self._mock_threads: list[tuple[_RecordingServer, threading.Thread]] = []
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_paths: dict[str, dict[str, Path]] = {}
        self._log_handles: list[Any] = []
        self._results: list[tuple[str, float, str]] = []
        self._current_check = ""
        self._shutdown_done = False
        self._shutdown_errors: list[str] = []
        self._shutdown_detail = ""
        self._previous_signal_handlers: dict[int, Any] = {}
        self._interrupted: int | None = None
        self._cleanup_started = False
        self._started = time.monotonic()

    # -- workspace / checks -------------------------------------------------
    def _make_workspace(self) -> None:
        self.workspace = tempfile.mkdtemp(prefix="routepilot-v4-smoke-")
        self.db_path = os.path.join(self.workspace, "router.db")
        self.graph_dir = os.path.join(self.workspace, "routing-graph")
        self.log_dir = os.path.join(self.workspace, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

    def _run_check(self, name: str, action: Callable[[], str | None]) -> None:
        self._current_check = name
        started = time.monotonic()
        try:
            detail = action() or ""
        except SmokeFailure:
            raise
        except Exception as exc:  # unexpected error still names the check
            raise SmokeFailure(f"{type(exc).__name__}: {exc}") from exc
        elapsed = time.monotonic() - started
        self._record_pass(name, elapsed, detail)

    def _record_pass(self, name: str, elapsed: float, detail: str) -> None:
        self._results.append((name, elapsed, detail))
        suffix = f" — {detail}" if detail else ""
        print(f"[PASS] {name}{suffix} ({elapsed:.2f}s)", flush=True)

    # -- process helpers ----------------------------------------------------
    def _start_app(self, label: str, port: int, *, graph_writer: bool) -> subprocess.Popen:
        env = os.environ.copy()
        env.update({
            "ROUTER_DB": self.db_path,
            "ROUTER_GRAPH_DIR": self.graph_dir,
            "ROUTER_GRAPH_WRITER": "1" if graph_writer else "0",
            "ROUTER_HOST": HOST,
            "ROUTER_PORT": str(port),
            "ROUTER_USER_AUTH": "0",
            "ROUTER_JEV_URL": JEV_BASE,
            # ROUTER_JEV_URL and ROUTER_JEV_MODEL must be configured together;
            # the smoke graphs carry `jev: null` so this catalog seed is never used.
            "ROUTER_JEV_MODEL": "smoke-jev",
            "ROUTER_JEV_API_KEY": "",
            "PYTHONUNBUFFERED": "1",
        })
        stdout_path = Path(self.log_dir) / f"{label}.stdout.log"
        stderr_path = Path(self.log_dir) / f"{label}.stderr.log"
        stdout = open(stdout_path, "wb")
        stderr = open(stderr_path, "wb")
        self._log_handles.extend([stdout, stderr])
        self._log_paths[label] = {"stdout": stdout_path, "stderr": stderr_path}
        process = subprocess.Popen(
            [python_executable(), "-m", "router"],
            cwd=str(REPO_ROOT), env=env, stdout=stdout, stderr=stderr,
        )
        self._processes[label] = process
        return process

    def _wait_ready(self, process: subprocess.Popen, base: str, *,
                    timeout: float, label: str) -> tuple[Response, float]:
        started = time.monotonic()
        last_problem = "no /readyz response"
        while time.monotonic() - started < timeout:
            if process.poll() is not None:
                raise SmokeFailure(
                    f"{label} exited before becoming ready (code {process.returncode})")
            try:
                response = call(base, "GET", "/readyz", timeout=2.0)
            except TransportError as exc:
                last_problem = str(exc)
                time.sleep(0.1)
                continue
            if response.status == 200:
                return response, time.monotonic() - started
            last_problem = f"HTTP {response.status}: {_brief(response.body)}"
            time.sleep(0.1)
        raise SmokeFailure(
            f"{label} /readyz not ready within {timeout:.0f}s (last: {last_problem})")

    def _wait_status(self, predicate: Callable[[dict], bool], *,
                     timeout: float, label: str) -> float:
        started = time.monotonic()
        last_body: dict = {}
        while time.monotonic() - started < timeout:
            response = call(APP_BASE, "GET", "/readyz", timeout=5.0)
            if response.status == 200 and isinstance(response.body, dict):
                last_body = response.body
                if predicate(last_body):
                    return time.monotonic() - started
            time.sleep(0.1)
        raise SmokeFailure(
            f"{label}: /readyz predicate not satisfied within {timeout:.0f}s "
            f"(last: {_brief(last_body)})")

    def _readyz(self, base: str = APP_BASE) -> dict:
        response = expect(call(base, "GET", "/readyz"), 200, f"GET {base}/readyz")
        return response.body if isinstance(response.body, dict) else {}

    def _trees(self, base: str = APP_BASE) -> dict[str, dict]:
        payload = expect(call(base, "GET", "/admin/trees"), 200, "GET /admin/trees")
        trees = payload.body.get("trees") if isinstance(payload.body, dict) else None
        require(isinstance(trees, list), f"/admin/trees: unexpected body {_brief(payload.body)}")
        return {tree["id"]: tree for tree in trees}

    @staticmethod
    def _enabled_ids(tree_map: dict[str, dict]) -> list[str]:
        return sorted(tree_id for tree_id, tree in tree_map.items() if tree["enabled"])

    # -- SSE client ---------------------------------------------------------
    def _stream_sse(self, payload: dict) -> dict:
        connection = http.client.HTTPConnection(HOST, APP_PORT, timeout=30)
        try:
            connection.request(
                "POST", MOCK_CHAT_PATH, body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Accept": "text/event-stream"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if response.status != 200:
                return {"status": response.status, "headers": headers,
                        "body": response.read().decode("utf-8", "replace")}
            content_frames: list[tuple[float, str]] = []
            done_at: float | None = None
            while True:
                line = response.readline()
                if not line:
                    break
                now = time.monotonic()
                stripped = line.strip()
                if not stripped.startswith(b"data:"):
                    continue
                chunk = stripped[5:].strip()
                if chunk == b"[DONE]":
                    done_at = now
                    break
                content_frames.append((now, chunk.decode("utf-8", "replace")))
            return {
                "status": response.status,
                "headers": headers,
                "frames": content_frames,
                "done_at": done_at,
            }
        finally:
            connection.close()

    @staticmethod
    def _frame_text(frame: str) -> str:
        try:
            data = json.loads(frame)
        except ValueError:
            return ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return ""
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        return content if isinstance(content, str) else ""

    # -- checks -------------------------------------------------------------
    def start_mocks(self) -> str:
        for port in (UPSTREAM_PORT, JEV_PORT, APP_PORT, REPLICA_PORT):
            require_free_port(port)
        upstream_records: list[dict] = []
        jev_records: list[dict] = []
        self.upstream = _RecordingServer(
            (HOST, UPSTREAM_PORT), MockUpstreamHandler,
            upstream_records, threading.Lock())
        self.jev = _RecordingServer(
            (HOST, JEV_PORT), MockJevHandler, jev_records, threading.Lock())
        for server, label in ((self.upstream, "upstream"), (self.jev, "jev")):
            thread = threading.Thread(
                target=server.serve_forever, name=f"mock-{label}", daemon=True)
            thread.start()
            self._mock_threads.append((server, thread))
        return (f"model upstream {HOST}:{UPSTREAM_PORT}, "
                f"JEV {HOST}:{JEV_PORT}")

    def start_app(self) -> str:
        self._start_app("app", APP_PORT, graph_writer=True)
        response, elapsed = self._wait_ready(
            self._processes["app"], APP_BASE, timeout=READY_TIMEOUT, label="writer app")
        status = response.body if isinstance(response.body, dict) else {}
        require(status.get("ready") is True, f"/readyz not ready: {_brief(status)}")
        require(status.get("stale") is False, f"/readyz stale on boot: {_brief(status)}")
        require(status.get("enabled_tree_ids") == [],
                f"/readyz should start with no enabled graphs: {_brief(status)}")
        require(status.get("default_tree_id") is None,
                f"/readyz default tree should be absent: {_brief(status)}")
        return f"ready in {elapsed:.2f}s on {APP_BASE} (pid {self._processes['app'].pid})"

    def prepare_graphs(self) -> str:
        documents = (
            graph_doc("main", "fast", default=True),
            graph_doc("finance", "strong", departments=["财务部"], enabled=True),
            graph_doc("legal", "legal", departments=["法务部"], enabled=True),
        )
        for document in documents:
            create_and_enable(APP_BASE, document)
        trees = self._trees()
        require(set(trees) == {"main", "finance", "legal"},
                f"unexpected tree ids: {sorted(trees)}")
        require(trees["main"]["enabled"] and trees["main"]["default"],
                "main must be enabled and the default graph")
        require(trees["finance"]["enabled"] and not trees["finance"]["default"],
                "finance must be enabled and non-default")
        require(trees["legal"]["enabled"] and not trees["legal"]["default"],
                "legal must be enabled and non-default")
        require(trees["finance"]["departments"] == ["财务部"],
                f"finance departments: {trees['finance']['departments']}")
        require(trees["legal"]["departments"] == ["法务部"],
                f"legal departments: {trees['legal']['departments']}")
        require(trees["main"]["departments"] == [],
                f"main should not own departments: {trees['main']['departments']}")
        ready = self._readyz()
        require(ready.get("default_tree_id") == "main",
                f"/readyz default tree: {_brief(ready)}")
        require(ready.get("enabled_tree_ids") == ["finance", "legal", "main"],
                f"/readyz enabled trees: {_brief(ready)}")
        return ("main(default,fast) / finance(enabled,strong,财务部) / "
                "legal(enabled,legal,法务部)")

    def check_public_json(self) -> str:
        before = len(self.upstream.snapshot()) if self.upstream else 0
        response = expect(
            call(APP_BASE, "POST", MOCK_CHAT_PATH, {
                "model": "auto",
                "messages": [{"role": "user", "content": "用一句话回答：1+1=?"}],
            }), 200, "public /v1 JSON")
        body = response.body if isinstance(response.body, dict) else {}
        choices = body.get("choices")
        require(isinstance(choices, list) and choices,
                f"public /v1 JSON: missing choices: {_brief(body)}")
        content = choices[0].get("message", {}).get("content")
        require(content == MOCK_ANSWER,
                f"public /v1 JSON: unexpected answer {content!r}")
        require(response.headers.get("x-sr-tree-id") == "main",
                f"x-sr-tree-id: {response.headers.get('x-sr-tree-id')!r}")
        require(response.headers.get("x-sr-graph-selection") == "default",
                f"x-sr-graph-selection: {response.headers.get('x-sr-graph-selection')!r}")
        require(response.headers.get("x-sr-model") == "fast",
                f"x-sr-model: {response.headers.get('x-sr-model')!r}")
        records = (self.upstream.snapshot() if self.upstream else [])[before:]
        require(len(records) == 1, f"upstream requests: expected 1, got {len(records)}")
        record = records[0]
        require(record["path"] == MOCK_CHAT_PATH,
                f"upstream path: {record['path']!r}")
        require(record["model"] == "fast", f"upstream model: {record['model']!r}")
        require(record["stream"] is False, "upstream stream flag should be false")
        require(record["authorization"] is None,
                f"upstream Authorization unexpectedly sent: {record['authorization']!r}")
        return ("200 JSON; headers x-sr-tree-id=main x-sr-graph-selection=default; "
                "upstream model=fast, no Authorization header")

    def check_public_sse(self) -> str:
        before = len(self.upstream.snapshot()) if self.upstream else 0
        result = self._stream_sse({
            "model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "逐字回答：你好"}],
        })
        require(result.get("status") == 200,
                f"public /v1 SSE: expected 200, got {result.get('status')}: "
                f"{_brief(result.get('body'))}")
        headers = result["headers"]
        require(str(headers.get("content-type", "")).startswith("text/event-stream"),
                f"SSE content-type: {headers.get('content-type')!r}")
        require(headers.get("x-sr-tree-id") == "main",
                f"SSE x-sr-tree-id: {headers.get('x-sr-tree-id')!r}")
        require(headers.get("x-sr-graph-selection") == "default",
                f"SSE x-sr-graph-selection: {headers.get('x-sr-graph-selection')!r}")
        frames = result.get("frames") or []
        require(len(frames) == len(STREAM_CHUNKS),
                f"SSE frames: expected {len(STREAM_CHUNKS)}, got {len(frames)}")
        require(result.get("done_at") is not None,
                "SSE stream ended without a data: [DONE] terminator")
        assembled = "".join(self._frame_text(frame) for _at, frame in frames)
        require(assembled == "".join(STREAM_CHUNKS),
                f"SSE assembled content: {assembled!r}")
        first_at = frames[0][0]
        last_at = result["done_at"]
        gap = last_at - first_at
        require(gap >= 0.30,
                f"SSE looks fully buffered: first-to-last {gap * 1000:.0f}ms "
                f"< 300ms (frame delay {STREAM_FRAME_DELAY * 1000:.0f}ms)")
        records = (self.upstream.snapshot() if self.upstream else [])[before:]
        require(len(records) == 1, f"upstream requests: expected 1, got {len(records)}")
        record = records[0]
        require(record["stream"] is True, "upstream stream flag should be true")
        require(record["model"] == "fast", f"upstream model: {record['model']!r}")
        require(record["authorization"] is None,
                f"upstream Authorization unexpectedly sent: {record['authorization']!r}")
        return (f"3 live SSE frames + [DONE]; first-to-last {gap * 1000:.0f}ms "
                f"(frame delay {STREAM_FRAME_DELAY * 1000:.0f}ms); upstream stream=true")

    def check_department_header(self) -> str:
        before = len(self.upstream.snapshot()) if self.upstream else 0
        response = call(
            APP_BASE, "POST", MOCK_CHAT_PATH,
            {"model": "auto", "messages": [{"role": "user", "content": "伪造部门"}]},
            headers={"X-SR-Department": "%E8%B4%A2%E5%8A%A1%E9%83%A8"})
        expect(response, 400, "forged X-SR-Department")
        message = response.body.get("error", {}).get("message") \
            if isinstance(response.body, dict) else None
        require(message == "department_header_not_allowed",
                f"forged department error: {_brief(response.body)}")
        after = len(self.upstream.snapshot()) if self.upstream else 0
        require(after == before,
                "rejected department request must not reach the upstream")
        return "400 department_header_not_allowed; no upstream call"

    def check_try_departments(self) -> str:
        upstream_before = len(self.upstream.snapshot()) if self.upstream else 0
        cases = (
            ("财务部", "finance", "department", "strong"),
            ("法务部", "legal", "department", "legal"),
            ("未知部门", "main", "default", "fast"),
            ("", "main", "default", "fast"),
        )
        observed = []
        for department, tree_id, selection, model in cases:
            response = expect(
                call(APP_BASE, "POST", "/admin/try",
                     {"department": department, "text": "处理请求"}),
                200, f"/admin/try department={department!r}")
            body = response.body if isinstance(response.body, dict) else {}
            outcome = body.get("outcome") if isinstance(body.get("outcome"), dict) else {}
            require(body.get("tree_id") == tree_id,
                    f"try department={department!r}: tree_id {body.get('tree_id')!r} "
                    f"!= {tree_id!r}")
            require(body.get("graph_selection") == selection,
                    f"try department={department!r}: graph_selection "
                    f"{body.get('graph_selection')!r} != {selection!r}")
            require(outcome.get("model") == model,
                    f"try department={department!r}: model "
                    f"{outcome.get('model')!r} != {model!r}")
            observed.append(f"{department or '<empty>'}->{tree_id}/{selection}/{model}")
        jev_calls = len(self.jev.snapshot()) if self.jev else 0
        require(jev_calls == 0,
                f"mock JEV received {jev_calls} calls but the graphs have no question nodes")
        upstream_after = len(self.upstream.snapshot()) if self.upstream else 0
        require(upstream_after == upstream_before,
                "dry-run /admin/try must not call the model upstream")
        return "; ".join(observed) + "; JEV mock 0 calls"

    def check_cross_graph_same_model(self) -> str:
        """Two graphs sharing one logical model must not share circuit state.

        The controllable upstream failure queue answers the next three
        ``main``/``fast`` requests with 500: only main's circuit opens (public
        ``/v1`` then fails 503 ``no_available_backend`` before the upstream).
        The ``twin`` graph (department 镜像部) routes the same logical model
        ``fast`` under its own tree scope, so its admin test chat still
        succeeds, reaches the upstream exactly once and must not reset main's
        failure count. Everything is asserted through real HTTP behaviour and
        upstream request counts; the router's ``app.state`` is never touched.
        """
        twin = graph_doc("twin", "fast", departments=["镜像部"], enabled=True)
        body = {"model": "auto",
                "messages": [{"role": "user", "content": "跨图同模型隔离"}]}
        upstream = self.upstream
        require(upstream is not None, "mock upstream is not running")
        created = False
        try:
            create_and_enable(APP_BASE, twin)
            created = True
            trees = self._trees()
            require("twin" in trees and trees["twin"]["enabled"],
                    f"twin graph not enabled: {sorted(trees)}")
            require(trees["twin"]["departments"] == ["镜像部"],
                    f"twin departments: {trees['twin']['departments']}")
            require(self._enabled_ids(trees) == ["finance", "legal", "main", "twin"],
                    f"enabled trees with twin: {self._enabled_ids(trees)}")

            before_failures = len(upstream.snapshot())
            upstream.fail_next(3)
            for attempt in range(3):
                expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, body), 502,
                       f"main/fast upstream failure #{attempt + 1}")
            require(upstream.pending_failures() == 0,
                    f"failure queue still has {upstream.pending_failures()} entries")
            failures = upstream.snapshot()[before_failures:]
            require(len(failures) == 3,
                    f"each main/fast failure must reach the upstream: got {len(failures)}")
            # Self-verify the injected failures through the recording the mock
            # upstream already keeps: each one reached it as logical model
            # ``fast`` and was answered with the injected 500 -- not another
            # model, and not a short-circuited/cached response.
            for index, record in enumerate(failures):
                require(record.get("model") == "fast",
                        f"failure #{index + 1} upstream model: "
                        f"{record.get('model')!r}")
                require(record.get("failure_status") == 500,
                        f"failure #{index + 1} did not receive the injected 500: "
                        f"{record.get('failure_status')!r}")

            before = len(upstream.snapshot())
            unavailable = expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, body), 503,
                                 "public /v1 while main circuit is open")
            message = unavailable.body.get("error", {}).get("message") \
                if isinstance(unavailable.body, dict) else None
            require(message == "no_available_backend",
                    f"open circuit error: {_brief(unavailable.body)}")
            require(len(upstream.snapshot()) == before,
                    "an open main circuit must block before the upstream")

            response = expect(call(
                APP_BASE, "POST", "/admin/test/chat/completions", body,
                headers={"x-sr-department": quote("镜像部", safe="")}),
                200, "twin/fast has an independent circuit")
            require(response.headers.get("x-sr-tree-id") == "twin",
                    f"twin tree header: {response.headers}")
            require(response.headers.get("x-sr-graph-selection") == "department",
                    f"twin graph selection: {response.headers}")
            require(len(upstream.snapshot()) == before + 1,
                    "twin/fast did not reach the upstream")

            # Deliberately leave tree:main:fast's circuit open for the rest of
            # this smoke process: later steps route legal/finance (and only
            # re-enable main), so none of them may expect a 200 from
            # ``main/fast`` or depend on this circuit closing again.
            before = len(upstream.snapshot())
            expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, body), 503,
                   "twin success must not reset main circuit")
            require(len(upstream.snapshot()) == before,
                    "main upstream should remain blocked after the twin success")
        finally:
            # Restore a successful upstream and remove the temporary graph so
            # the later empty-route-set / replica / cleanup checks are unaffected.
            upstream.clear_failures()
            if created:
                expect(call(APP_BASE, "POST", "/admin/trees/twin/disable"), 200,
                       "disable twin")
                self._wait_status(
                    lambda status: status.get("enabled_tree_ids")
                    == ["finance", "legal", "main"],
                    timeout=STALE_TIMEOUT, label="twin disabled back to main/finance/legal")
                require(self._enabled_ids(self._trees()) == ["finance", "legal", "main"],
                        "enabled trees after disabling twin")
        return ("3x main/fast 502 -> main circuit open (next /v1 503 "
                "no_available_backend, no upstream call); twin/fast 200 via "
                "department 镜像部 (exactly 1 upstream call); main stayed open "
                "after the twin success; twin disabled, enabled set restored to "
                "main/finance/legal")

    def check_switch_default(self) -> str:
        before = len(self.upstream.snapshot()) if self.upstream else 0
        expect(call(APP_BASE, "POST", "/admin/trees/legal/default"), 200,
               "POST /admin/trees/legal/default")
        trees = self._trees()
        require(trees["legal"]["default"] and trees["legal"]["enabled"],
                "legal should be enabled and default")
        require(trees["main"]["enabled"] and not trees["main"]["default"],
                "main should stay enabled and non-default")
        require(trees["finance"]["enabled"] and not trees["finance"]["default"],
                "finance should stay enabled and non-default")
        response = expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, {
            "model": "auto",
            "messages": [{"role": "user", "content": "默认图切换后"}],
        }), 200, "public /v1 after default switch")
        require(response.headers.get("x-sr-tree-id") == "legal",
                f"x-sr-tree-id: {response.headers.get('x-sr-tree-id')!r}")
        require(response.headers.get("x-sr-graph-selection") == "default",
                f"x-sr-graph-selection: {response.headers.get('x-sr-graph-selection')!r}")
        records = (self.upstream.snapshot() if self.upstream else [])[before:]
        require(len(records) == 1,
                f"upstream requests after switch: expected 1, got {len(records)}")
        require(records[0]["model"] == "legal",
                f"upstream model after switch: {records[0]['model']!r}")
        ready = self._readyz()
        require(ready.get("default_tree_id") == "legal",
                f"/readyz default after switch: {_brief(ready)}")
        return "legal default (main/finance still enabled); /v1 -> x-sr-tree-id=legal model=legal"

    def check_disable_guard(self) -> str:
        expect(call(APP_BASE, "POST", "/admin/trees/main/disable"), 200,
               "disable main")
        trees = self._trees()
        require(not trees["main"]["enabled"] and not trees["main"]["default"],
                "main should be disabled")
        require(trees["legal"]["default"] and trees["legal"]["enabled"],
                "legal should still be the enabled default graph")
        require(trees["finance"]["enabled"], "finance should still be enabled")
        response = expect(call(APP_BASE, "POST", "/admin/trees/legal/disable"), 409,
                          "disable default legal while finance is enabled")
        detail = response.body.get("detail") if isinstance(response.body, dict) else None
        require(isinstance(detail, dict) and detail.get("code") == "default_graph_required",
                f"disable default error: {_brief(response.body)}")
        require(detail.get("tree_id") == "legal",
                f"default_graph_required tree_id: {detail.get('tree_id')!r}")
        ready = self._readyz()
        require(ready.get("enabled_tree_ids") == ["finance", "legal"],
                f"/readyz enabled after failed disable: {_brief(ready)}")
        return "main disabled; disabling default legal -> 409 default_graph_required (finance enabled)"

    def check_empty_route_set(self) -> str:
        expect(call(APP_BASE, "POST", "/admin/trees/finance/disable"), 200,
               "disable finance")
        response = expect(call(APP_BASE, "POST", "/admin/trees/legal/disable"), 200,
                          "disable last enabled graph legal")
        status = response.body.get("graph_status") if isinstance(response.body, dict) else {}
        require(status.get("enabled_tree_ids") == [],
                f"enabled_tree_ids after last disable: {_brief(status)}")
        require(status.get("default_tree_id") is None,
                f"default_tree_id after last disable: {_brief(status)}")
        ready = self._readyz()
        require(ready.get("ready") is True and ready.get("stale") is False,
                f"/readyz should stay ready on an empty route set: {_brief(ready)}")
        require(ready.get("enabled_tree_ids") == [],
                f"/readyz enabled trees: {_brief(ready)}")
        require(ready.get("default_tree_id") is None,
                f"/readyz default tree: {_brief(ready)}")
        before = len(self.upstream.snapshot()) if self.upstream else 0
        unavailable = call(APP_BASE, "POST", MOCK_CHAT_PATH, {
            "model": "auto", "messages": [{"role": "user", "content": "空路由集"}],
        })
        expect(unavailable, 503, "public /v1 with no enabled graph")
        message = unavailable.body.get("error", {}).get("message") \
            if isinstance(unavailable.body, dict) else None
        require(message == "no_active_tree",
                f"empty route set error: {_brief(unavailable.body)}")
        after = len(self.upstream.snapshot()) if self.upstream else 0
        require(after == before, "empty route set must not call the upstream")
        return "/readyz ready; enabled_tree_ids=[], default_tree_id=null; /v1 503 no_active_tree"

    def check_stale_recovery(self) -> str:
        expect(call(APP_BASE, "POST", "/admin/trees/finance/enable"), 200,
               "re-enable finance")
        trees = self._trees()
        require(trees["finance"]["enabled"] and trees["finance"]["default"],
                "re-enabled finance should become the default graph")
        graph_path = Path(self.graph_dir) / "graphs" / "finance.json"
        original = graph_path.read_bytes()
        try:
            graph_path.write_bytes(b'{"broken": ')
            stale_after = self._wait_status(
                lambda body: body.get("stale") is True,
                timeout=STALE_TIMEOUT, label="stale after corrupt graph")
            ready = self._readyz()
            require(ready.get("ready") is True,
                    f"/readyz must stay ready while stale: {_brief(ready)}")
            require(ready.get("enabled_tree_ids") == ["finance"],
                    f"/readyz keeps the previous route set: {_brief(ready)}")
            require(ready.get("last_load_error"),
                    f"/readyz should report a load error while stale: {_brief(ready)}")
            response = expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, {
                "model": "auto", "messages": [{"role": "user", "content": "stale 期间旧集合"}],
            }), 200, "public /v1 while stale")
            require(response.headers.get("x-sr-tree-id") == "finance",
                    f"stale serving tree: {response.headers.get('x-sr-tree-id')!r}")
            records = self.upstream.snapshot() if self.upstream else []
            require(records and records[-1]["model"] == "strong",
                    f"stale serving upstream model: "
                    f"{records[-1]['model'] if records else None!r}")
        finally:
            graph_path.write_bytes(original)
        recovered_after = self._wait_status(
            lambda body: body.get("stale") is False,
            timeout=STALE_TIMEOUT, label="recovered after restore")
        ready = self._readyz()
        require(ready.get("last_load_error") == "",
                f"/readyz last_load_error after recovery: {_brief(ready)}")
        require(ready.get("enabled_tree_ids") == ["finance"],
                f"/readyz enabled after recovery: {_brief(ready)}")
        response = expect(call(APP_BASE, "POST", MOCK_CHAT_PATH, {
            "model": "auto", "messages": [{"role": "user", "content": "恢复后"}],
        }), 200, "public /v1 after recovery")
        require(response.headers.get("x-sr-tree-id") == "finance",
                f"recovered serving tree: {response.headers.get('x-sr-tree-id')!r}")
        return (f"stale after {stale_after:.2f}s (old set served, model=strong); "
                f"stale cleared {recovered_after:.2f}s after restore; /v1 200")

    def _assert_replica_matches(self, *, context: str) -> None:
        writer_ready = self._readyz(APP_BASE)
        replica_ready = self._readyz(REPLICA_BASE)
        for field in ("snapshot_revision", "route_fingerprint",
                      "default_tree_id", "enabled_tree_ids"):
            require(writer_ready.get(field) == replica_ready.get(field),
                    f"{context}: replica {field} mismatch: "
                    f"writer={writer_ready.get(field)!r} replica={replica_ready.get(field)!r}")
        require(bool(writer_ready.get("snapshot_revision")),
                f"{context}: writer snapshot_revision is empty")
        writer_trees = self._trees(APP_BASE)
        replica_trees = self._trees(REPLICA_BASE)
        require(set(writer_trees) == set(replica_trees),
                f"{context}: tree ids mismatch: writer={sorted(writer_trees)} "
                f"replica={sorted(replica_trees)}")
        for tree_id in sorted(writer_trees):
            for field in ("enabled", "default", "departments"):
                require(writer_trees[tree_id][field] == replica_trees[tree_id][field],
                        f"{context}: tree {tree_id} {field} mismatch: "
                        f"writer={writer_trees[tree_id][field]!r} "
                        f"replica={replica_trees[tree_id][field]!r}")
        require(self._enabled_ids(replica_trees) == writer_ready.get("enabled_tree_ids"),
                f"{context}: replica tree enabled flags do not match /readyz")

    def check_replica(self) -> str:
        self._start_app("replica", REPLICA_PORT, graph_writer=False)
        response, elapsed = self._wait_ready(
            self._processes["replica"], REPLICA_BASE,
            timeout=READY_TIMEOUT, label="read-only replica")
        status = response.body if isinstance(response.body, dict) else {}
        require(status.get("ready") is True,
                f"replica /readyz not ready: {_brief(status)}")
        self._assert_replica_matches(context="initial")
        initial = self._readyz(REPLICA_BASE)

        # Writer enables a second graph; the replica must reload on its own.
        expect(call(APP_BASE, "POST", "/admin/trees/main/enable"), 200,
               "re-enable main on writer")
        writer_ready = self._readyz(APP_BASE)
        require(writer_ready.get("enabled_tree_ids") == ["finance", "main"],
                f"writer enabled trees after re-enable main: {_brief(writer_ready)}")
        target_revision = writer_ready.get("snapshot_revision")
        catchup_started = time.monotonic()
        while time.monotonic() - catchup_started < REPLICA_CATCHUP_TIMEOUT:
            replica_ready = self._readyz(REPLICA_BASE)
            if replica_ready.get("snapshot_revision") == target_revision:
                break
            time.sleep(0.1)
        else:
            raise SmokeFailure(
                "read-only replica did not reload the writer's route set within "
                f"{REPLICA_CATCHUP_TIMEOUT:.0f}s (writer revision {target_revision!r})")
        catchup = time.monotonic() - catchup_started
        self._assert_replica_matches(context="after enable main")
        replica_trees = self._trees(REPLICA_BASE)
        require(replica_trees["main"]["enabled"] and not replica_trees["main"]["default"],
                "replica should see main enabled and non-default")
        require(replica_trees["finance"]["default"],
                "replica should see finance as the default graph")
        return (f"startup ready in {elapsed:.2f}s; snapshot_revision="
                f"{initial.get('snapshot_revision', '')[:12]}… and "
                f"route_fingerprint={initial.get('route_fingerprint', '')[:12]}… matched; "
                f"enable main -> replica caught up in {catchup:.2f}s; trees matched")

    # -- signal handling / interrupt safety ---------------------------------
    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return  # signals are only deliverable to the main thread
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_signal_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (ValueError, OSError):  # pragma: no cover - hostile environment
                self._previous_signal_handlers.pop(signum, None)

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._previous_signal_handlers.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):  # pragma: no cover
                pass
        self._previous_signal_handlers.clear()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        name = _signal_name(signum)
        if self._cleanup_started:
            # Cleanup is already running/completed; a repeated Ctrl-C must not
            # abort it and orphan the spawned router processes.
            print(f"\n{name} received during cleanup; finishing cleanup",
                  file=sys.stderr, flush=True)
            return
        if self._interrupted is not None:
            return
        self._interrupted = signum
        print(f"\n{name} received during {self._current_check or 'startup'}; "
              f"cleaning up", file=sys.stderr, flush=True)
        raise SmokeInterrupted(signum)

    # -- shutdown / cleanup -------------------------------------------------
    def _stop_process(self, label: str, process: subprocess.Popen) -> list[str]:
        errors: list[str] = []
        if process.poll() is not None:
            return errors
        process.terminate()  # SIGTERM
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=5)
            errors.append(
                f"{label} did not exit within {PROCESS_STOP_TIMEOUT:.0f}s of SIGTERM; "
                f"killed (code {code})")
        return errors

    def _shutdown(self) -> tuple[list[str], str]:
        if self._shutdown_done:
            return self._shutdown_errors, self._shutdown_detail
        self._shutdown_done = True
        errors: list[str] = []
        exit_codes: dict[str, int] = {}
        for label in ("app", "replica"):
            process = self._processes.get(label)
            if process is None:
                continue
            errors.extend(self._stop_process(label, process))
            if process.returncode is not None:
                exit_codes[label] = process.returncode
        # Graceful uvicorn shutdown writes this line to the captured logs.
        graceful = {}
        for label, paths in self._log_paths.items():
            if label not in self._processes:
                continue
            text = ""
            for name in ("stdout", "stderr"):
                try:
                    text += paths[name].read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            graceful[label] = "Application shutdown complete" in text
            if label in exit_codes and not graceful[label]:
                errors.append(f"{label} did not log a graceful application shutdown")
        for server, thread in self._mock_threads:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if thread.is_alive():
                errors.append(f"mock server {thread.name} did not stop")
        for handle in self._log_handles:
            try:
                handle.close()
            except OSError:
                pass
        cleanup_note = ""
        if self.keep:
            cleanup_note = f"workspace retained at {self.workspace}"
        elif self.workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)
            if os.path.exists(self.workspace):
                errors.append(f"temp workspace not removed: {self.workspace}")
            else:
                cleanup_note = "temp workspace removed"
        codes = ", ".join(f"{label}={code}" for label, code in exit_codes.items()) or "no processes"
        detail = (f"SIGTERM exit codes [{codes}], graceful shutdown logged "
                  f"{ {label: graceful[label] for label in graceful} }, "
                  f"mocks stopped, {cleanup_note}")
        self._shutdown_errors = errors
        self._shutdown_detail = detail
        return errors, detail

    def _finalize(self) -> str:
        errors, detail = self._shutdown()
        if errors:
            raise SmokeFailure("shutdown/cleanup: " + "; ".join(errors))
        return detail

    # -- failure evidence / summary ----------------------------------------
    def _failure_evidence(self) -> str:
        sections: list[str] = []
        for label, paths in self._log_paths.items():
            for stream in ("stdout", "stderr"):
                tail = _tail_file(paths[stream], lines=40)
                if tail:
                    sections.append(f"--- {label} {stream} tail ---\n{tail}")
        upstream = self.upstream.snapshot() if self.upstream else []
        sections.append(f"--- mock upstream requests ({len(upstream)}) ---")
        for record in upstream[-5:]:
            sections.append(
                f"  {record.get('path')} model={record.get('model')!r} "
                f"stream={record.get('stream')} "
                f"authorization={record.get('authorization')!r}")
        jev = self.jev.snapshot() if self.jev else []
        sections.append(f"--- mock JEV calls ({len(jev)}) ---")
        if self.workspace:
            sections.append(f"workspace: {self.workspace}")
        return "\n".join(sections)

    def _print_summary(self) -> None:
        total = time.monotonic() - self._started
        print(f"\n=== RoutePilot standalone v4 smoke summary ({len(self._results)} checks) ===")
        for name, elapsed, detail in self._results:
            suffix = f" — {detail}" if detail else ""
            print(f"  PASS  {name}{suffix} ({elapsed:.2f}s)")
        print(f"PASS: {len(self._results)}/{len(self._results)} checks in {total:.2f}s")

    def _print_failure(self, evidence: str) -> None:
        failure = self.failure
        assert failure is not None
        print("\n" + "=" * 78)
        print(f"FAIL: {self._current_check or 'startup'}")
        print(f"      {failure}")
        print("=" * 78)
        if evidence:
            print(evidence)
        if self._shutdown_errors:
            print("--- shutdown/cleanup problems ---")
            for error in self._shutdown_errors:
                print(f"  - {error}")
        if self.keep and self.workspace:
            print(f"workspace retained at {self.workspace}")

    def _print_interrupted(self, evidence: str, shutdown_errors: list[str],
                           shutdown_detail: str) -> None:
        assert self._interrupted is not None
        name = _signal_name(self._interrupted)
        print("\n" + "=" * 78)
        print(f"INTERRUPTED: {name} during {self._current_check or 'startup'}")
        print(f"           {shutdown_detail}")
        print("=" * 78)
        if evidence:
            print(evidence)
        for error in shutdown_errors:
            print(f"  - cleanup problem: {error}")
        if self.keep and self.workspace:
            print(f"workspace retained at {self.workspace}")
        print(f"exiting with code {128 + self._interrupted}", flush=True)

    # -- main ----------------------------------------------------------------
    def run(self) -> int:
        checks: list[tuple[str, Callable[[], str | None]]] = [
            ("mock upstream/JEV servers started", self.start_mocks),
            ("real writer app ready (/readyz 200)", self.start_app),
            ("main/finance/legal v4 graphs created and enabled", self.prepare_graphs),
            ("public /v1 JSON uses default graph main/fast", self.check_public_json),
            ("public /v1 SSE streams live and terminates with [DONE]", self.check_public_sse),
            ("public /v1 rejects forged X-SR-Department", self.check_department_header),
            ("/admin/try selects graphs by department", self.check_try_departments),
            ("cross-graph same-model isolation keeps circuits independent",
             self.check_cross_graph_same_model),
            ("legal becomes default and /v1 follows", self.check_switch_default),
            ("default graph cannot be disabled while others stay enabled",
             self.check_disable_guard),
            ("empty enabled set: /readyz ready, /v1 503 no_active_tree",
             self.check_empty_route_set),
            ("corrupt enabled graph -> stale, old set serves; restore recovers",
             self.check_stale_recovery),
            ("read-only replica matches writer route set", self.check_replica),
        ]
        self.failure: SmokeFailure | None = None
        self._install_signal_handlers()
        evidence = ""
        shutdown_errors: list[str] = []
        shutdown_detail = ""
        shutdown_elapsed = 0.0
        try:
            try:
                self._make_workspace()
                for name, action in checks:
                    self._run_check(name, action)
            except SmokeFailure as exc:
                self.failure = exc
            except Exception as exc:  # defensive: never hide an unexpected traceback
                self.failure = SmokeFailure(f"{type(exc).__name__}: {exc}")
            except KeyboardInterrupt:  # defensive: SIGINT that bypassed the handler
                self._interrupted = signal.SIGINT
            finally:
                # Unconditional cleanup: neither a failed check nor a signal may
                # leave router processes or a temp workspace behind.
                self._cleanup_started = True
                if self.failure is not None or self._interrupted is not None:
                    evidence = self._failure_evidence()
                started = time.monotonic()
                shutdown_errors, shutdown_detail = self._shutdown()
                shutdown_elapsed = time.monotonic() - started
        except SmokeInterrupted as exc:
            # A signal raced with the cleanup entry; ``_shutdown()`` is idempotent.
            self._cleanup_started = True
            if self._interrupted is None:
                self._interrupted = exc.signum
            shutdown_errors, shutdown_detail = self._shutdown()
        finally:
            self._restore_signal_handlers()
        if self._interrupted is not None:
            self._print_interrupted(evidence, shutdown_errors, shutdown_detail)
            return 128 + self._interrupted
        if self.failure is None and shutdown_errors:
            self._current_check = "clean shutdown and temp workspace cleanup"
            self.failure = SmokeFailure("; ".join(shutdown_errors))
        if self.failure is None:
            self._record_pass(
                "clean shutdown and temp workspace cleanup",
                shutdown_elapsed, shutdown_detail)
            self._print_summary()
            return 0
        self._print_failure(evidence)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RoutePilot v4 department-aware standalone routing smoke test")
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the temp database/graph/log workspace for debugging")
    args = parser.parse_args(argv)
    return Smoke(keep=args.keep).run()


if __name__ == "__main__":
    sys.exit(main())
