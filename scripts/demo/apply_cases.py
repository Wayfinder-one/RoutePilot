#!/usr/bin/env python3
"""把 5 个演示决策组幂等部署到 RoutePilot（sr-routing-graph v4）并逐条 dry-run 验证。

案例 JSON 是唯一事实源：本脚本不再维护任何硬编码的 provider / 模型 / JEV /
时段目录，而是读取 ``configs/cases/case-*.json``（每份都是可直接导入的完整 v4
文档）合并出数据库下拉框目录，再按 ``manifest.json`` 的案例清单部署画布。

做的事（全部走管理 API，不落本地状态）：

1. 登录 /admin/login 拿 Bearer token；
2. 从五份案例文档合并运行目录（同名条目必须逐字一致，否则拒绝部署）：
   providers → models → JEV 服务与各自窗口 → 时段；
3. 用合并结果幂等写入目录（已存在则 PUT，缺则 POST），并回读校验；
4. 按 manifest 逐个案例 POST/PUT 决策组画布：剔除 ``kind``/``version``/
   ``id``/``enabled``/``default``（生命周期与文件信封不进管理写请求），携带
   ``departments`` 与 ``backends``/``jev``/``jev_window``/``time_periods``
   四个运行字段；**不 enable**（试算用 /admin/try tree_id）；
5. 对每个演示 prompt 调 POST /admin/try，比较 outcome.target_id / model /
   reasoning / candidates 与期望是否一致，并检查 enable 级 warnings 为空；
6. 部署前记录当前 enabled 的决策组，部署后确认这份集合原样保持（全新实例部署前
   为空时，部署后也必须为空），并确认 5 棵演示树全部未启用、运行字段齐全。

影响确认（409 ``impact_confirmation_required``）默认行为：打印受影响画布清单
（含当前 ``document_sha256`` 与用途）后停止，不写入任何后续内容；确认同步后
显式加 ``--confirm-sync``，脚本会用 409 响应里的基线哈希重试同一请求。画布在
确认前已被改动（409 ``graph_conflict``）时打印真实哈希并停止，绝不自动绕过。

用法：
    python3 scripts/demo/apply_cases.py
    python3 scripts/demo/apply_cases.py --only case-ha --verbose
    python3 scripts/demo/apply_cases.py --confirm-sync

可用环境变量覆盖：SR_BASE_URL / SR_ADMIN_EMAIL / SR_ADMIN_PASSWORD。
退出码：0 = 全部成功；1 = 任一目录/画布写入、影响确认或 dry-run 期望不匹配。

注意（案例五）：熔断状态是进程级 + DB 持久（同一模型连续 3 次 5xx 打开 300s 半开窗口）。
如果刚跑过案例五的实时故障注入，先等待熔断半开窗口（或重启服务）再执行本脚本，
否则 ha-fail 熔断期间 dry-run 会选到 ha-general / 返回 503。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "configs" / "cases"
MANIFEST_FILE = CASES_DIR / "manifest.json"

DEFAULT_BASE_URL = os.environ.get("SR_BASE_URL", "http://192.168.95.76:8400")
DEFAULT_EMAIL = os.environ.get("SR_ADMIN_EMAIL", "admin@test.com")
DEFAULT_PASSWORD = os.environ.get("SR_ADMIN_PASSWORD", "1qaz@WSX")

#: 画布文档的四个运行字段：必须成组出现在部署请求里。
RUNTIME_FIELDS = ("backends", "jev", "jev_window", "time_periods")
#: 管理 DTO 允许、但由 manifest + 文件名决定的信封字段：部署前必须剔除。
#: 生命周期（enabled/default）只能由管理端启停端点写入；departments 保留为普通字段。
ENVELOPE_FIELDS = ("kind", "version", "id", "enabled", "default")

#: JEV 服务的完整非密钥字段（``id`` 决定 POST / PUT 路径）。
JEV_FIELDS = (
    "id", "base_url", "model", "key_env", "timeout_seconds",
    "instructions", "state_template", "input_tokens", "context_tokens",
)

OK = "✓"
BAD = "✗"


# --------------------------------------------------------------------------- #
# 案例文档 → 运行目录（纯函数，测试直接导入）
# --------------------------------------------------------------------------- #
def load_case_documents(cases: list[dict]) -> dict[str, dict]:
    """按 manifest 的 ``tree_file`` 读取案例文档（键 = manifest 案例 id）。"""
    documents: dict[str, dict] = {}
    for case in cases:
        tree_file = case.get("tree_file")
        if not isinstance(tree_file, str) or not tree_file:
            raise ValueError(f"案例 {case.get('id')!r} 缺少 tree_file")
        path = CASES_DIR / tree_file
        if path.parent != CASES_DIR or not path.is_file():
            raise ValueError(f"案例 {case.get('id')!r} 的 tree_file 不在 {CASES_DIR}：{tree_file}")
        documents[case["id"]] = json.loads(path.read_text(encoding="utf-8"))
    return documents


def case_runtime(case_documents) -> dict:
    """把多份自包含案例文档合并成数据库目录（同名条目必须完全一致）。

    合并键：provider ``name`` / model ``id`` / JEV 服务 ``id`` / 时段 ``name``。
    同一键在两个案例里配置不同 → ``ValueError``（点名冲突键），绝不后写覆盖先写。
    """
    groups: dict[str, dict] = {
        "providers": {},
        "models": {},
        "jev_services": {},
        "time_periods": {},
    }

    def merge(group: str, key: str, value) -> None:
        if key in groups[group] and groups[group][key] != value:
            raise ValueError(f"conflicting {group} entry: {key}")
        groups[group][key] = value

    for document in case_documents:
        for provider in document["backends"]["providers"]:
            merge("providers", provider["name"], provider)
        for model in document["backends"]["models"]:
            merge("models", model["id"], model)
        jev = document.get("jev")
        if jev is not None:
            # 窗口属于 JEV 服务：合并成一条目录项（与 PUT /admin/jev/{id} 的 DTO 一致）。
            merge("jev_services", jev["id"], {**jev, **document["jev_window"]})
        for period in document["time_periods"]:
            merge("time_periods", period["name"], period)
    return {name: list(values.values()) for name, values in groups.items()}


# --------------------------------------------------------------------------- #
# 影响确认（409 impact_confirmation_required / graph_conflict）纯函数
# --------------------------------------------------------------------------- #
def _detail_of(payload):
    return payload.get("detail", payload) if isinstance(payload, dict) else payload


def impact_detail(payload):
    """取出 409 ``impact_confirmation_required`` 详情；其它响应返回 ``None``。"""
    detail = _detail_of(payload)
    if isinstance(detail, dict) and detail.get("code") == "impact_confirmation_required":
        return detail
    return None


def graph_conflict_detail(payload):
    """取出 409 ``graph_conflict`` 详情（画布基线哈希已过期）。"""
    detail = _detail_of(payload)
    if isinstance(detail, dict) and detail.get("code") == "graph_conflict":
        return detail
    return None


def confirmation_for(detail: dict) -> dict:
    """用 409 响应里的当前基线哈希构造 ``impact_confirmation``。"""
    return {
        "trees": [
            {"id": item["id"], "document_sha256": item.get("document_sha256", "")}
            for item in detail.get("affected_trees") or ()
            if isinstance(item, dict) and item.get("id")
        ],
    }


def impact_report_lines(detail: dict) -> list[str]:
    """把影响确认详情整理成人读的行（默认停止时打印）。"""
    resource = detail.get("resource") or {}
    lines = [
        f"{BAD} 需要影响确认（409 impact_confirmation_required）："
        f"{_brief(resource, 200)}",
    ]
    trees = [item for item in detail.get("affected_trees") or () if isinstance(item, dict)]
    lines.append(f"受影响画布 {len(trees)} 个（基线哈希 = document_sha256）：")
    for item in trees:
        usage = item.get("usage")
        suffix = f"；用途={_brief(usage, 160)}" if usage else ""
        lines.append(
            f"  - {item.get('id')}（{item.get('name') or '-'}）"
            f"enabled={item.get('enabled')} sha256={item.get('document_sha256')}{suffix}")
    replacements = detail.get("replacements") or []
    if replacements:
        lines.append(f"可替代目录 {len(replacements)} 个：{_brief(replacements, 400)}")
    lines.append("默认行为：不写入任何内容并停止。确认同步后请加 --confirm-sync 重新运行本脚本。")
    return lines


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail):
        self.method, self.path, self.status, self.detail = method, path, status, detail
        super().__init__(f"{method} {path} -> HTTP {status}: {_brief(detail)}")


class ImpactConfirmationRequired(RuntimeError):
    """409 影响确认：默认停止；``--confirm-sync`` 时由调用方带基线哈希重试。"""

    def __init__(self, method: str, path: str, detail: dict):
        self.method, self.path, self.detail = method, path, detail
        super().__init__(f"{method} {path} -> impact_confirmation_required")


class GraphConflictError(RuntimeError):
    """409 画布基线哈希过期（确认与当前影响清单不一致）。"""

    def __init__(self, method: str, path: str, detail: dict):
        self.method, self.path, self.detail = method, path, detail
        super().__init__(f"{method} {path} -> graph_conflict: {_brief(detail)}")


def _brief(value, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _issue_lines(detail) -> list[str]:
    """把 422 的 Issue 数组整理成人读的多行文本。"""
    if not isinstance(detail, list):
        return [_brief(detail)]
    lines = []
    for item in detail:
        if isinstance(item, dict):
            code = item.get("code", "?")
            zh = item.get("zh") or item.get("en") or ""
            where = item.get("node_id") or item.get("edge_id") or item.get("port") or ""
            lines.append(f"  [{code}] {zh}" + (f"（{where}）" if where else ""))
        else:
            lines.append(f"  {_brief(item)}")
    return lines


class Api:
    def __init__(self, base_url: str, email: str = "", password: str = "", timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.timeout = timeout
        self.token = ""
        #: 影响确认默认拒绝；只有显式 --confirm-sync 才置 True。
        self.confirm_sync = False

    # ---------------------------------------------------------------- transport
    def _raw(self, method: str, path: str, body=None, *, auth: bool = True):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw) if raw else {}
            except ValueError:
                detail = raw.decode("utf-8", "replace")
            return exc.code, detail
        except urllib.error.URLError as exc:
            raise SystemExit(f"无法连接 {self.base_url}：{exc.reason}") from exc

    def request(self, method: str, path: str, body=None, *, ok=(200, 201)):
        """管理写/读请求：409 影响确认默认抛出，``confirm_sync`` 时带基线哈希重试一次。"""
        status, payload = self._raw(method, path, body)
        if status in ok:
            return payload
        impact = impact_detail(payload)
        if impact is not None:
            if not self.confirm_sync:
                raise ImpactConfirmationRequired(method, path, impact)
            # 显式确认：只用 409 响应里的当前哈希，绝不虚构或复用旧哈希。
            retry_body = dict(body or {})
            retry_body["impact_confirmation"] = confirmation_for(impact)
            status, payload = self._raw(method, path, retry_body)
            if status in ok:
                return payload
        # 首次（或重试后）就拿到 409 graph_conflict 时同样必须走停止路径：
        # 画布在 upsert_tree 的 GET 与 PUT 之间被改了，基线哈希已经过期。
        conflict = graph_conflict_detail(payload)
        if conflict is not None:
            raise GraphConflictError(method, path, conflict)
        raise ApiError(method, path, status, _detail_of(payload))

    # ---------------------------------------------------------------- endpoints
    def login(self) -> None:
        data = self.request(
            "POST", "/admin/login",
            {"email": self.email, "password": self.password},
            ok=(200,),
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise ApiError("POST", "/admin/login", 200, "响应里没有 token")
        self.token = token

    def providers(self) -> dict:
        return {row["name"]: row for row in self.request("GET", "/admin/providers")["providers"]}

    def models(self) -> dict:
        return {row["id"]: row for row in self.request("GET", "/admin/models")["models"]}

    def jev_services(self) -> dict:
        return {row["id"]: row for row in self.request("GET", "/admin/jev")["services"]}

    def time_periods(self) -> list:
        return self.request("GET", "/admin/time-periods")["periods"]

    def trees(self) -> dict:
        return {row["id"]: row for row in self.request("GET", "/admin/trees")["trees"]}

    def upsert_provider(self, provider: dict) -> str:
        body = {field: provider.get(field, "" if field == "key_env" else None)
                for field in ("base_url", "key_env", "reasoning_format")}
        self.request("PUT", f"/admin/providers/{provider['name']}", body)
        return "updated"

    def upsert_model(self, model: dict) -> str:
        body = {
            "upstream": model["upstream"],
            "provider": model["provider"],
            "profile": model["profile"],
        }
        if model["id"] in self.models():
            self.request("PUT", f"/admin/models/{model['id']}", body)
            return "updated"
        self.request("POST", "/admin/models", {"id": model["id"], **body})
        return "created"

    def upsert_jev_service(self, service: dict) -> str:
        body = {field: service[field] for field in JEV_FIELDS if field in service}
        if service["id"] in self.jev_services():
            self.request("PUT", f"/admin/jev/{service['id']}", body)
            return "updated"
        self.request("POST", "/admin/jev", body)
        return "created"

    def save_time_periods(self, periods: list) -> None:
        self.request("PUT", "/admin/time-periods", {"periods": periods})

    def upsert_tree(self, tree_id: str, document: dict) -> str:
        """部署一份案例文档：剔除信封字段，携带四个运行字段。"""
        body = {
            "name": document.get("name", ""),
            "root": document["root"],
            "departments": list(document.get("departments") or []),
            "sticky": document.get("sticky") or {"enabled": False, "ttl_seconds": 1800},
            "timezone": document.get("timezone") or "Asia/Shanghai",
        }
        if "canvas" in document:
            body["canvas"] = document["canvas"]
        body.update({field: document[field] for field in RUNTIME_FIELDS})
        for field in ENVELOPE_FIELDS:
            body.pop(field, None)
        rows = self.trees()
        if tree_id in rows:
            # 画布写用例要求显式基线哈希（乐观并发）；缺失会被严格拒绝为 graph_conflict。
            body["base_document_sha256"] = rows[tree_id].get("document_sha256")
            self.request("PUT", f"/admin/trees/{tree_id}", body)
            return "updated"
        self.request("POST", "/admin/trees", {"id": tree_id, **body})
        return "created"

    def try_tree(self, tree_id: str, text: str, now: str | None = None) -> tuple[int, dict]:
        body: dict = {"tree_id": tree_id, "text": text}
        if now:
            body["now"] = now
        return self._raw("POST", "/admin/try", body)


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def _jev_evidence(answers: dict) -> str:
    parts = []
    for node_id in sorted(answers):
        answer = answers[node_id]
        if "probabilities" in answer:
            parts.append(f"{node_id}:{answer.get('ranking', ['?'])[0]} p_top={answer.get('p_top', 0):.3f}")
        elif "yes" in answer:
            parts.append(f"{node_id}:yes={answer['yes']:.3f}")
        else:
            parts.append(f"{node_id}:{answer.get('value')}")
    return " | ".join(parts)


def _display_width(text: str) -> int:
    """终端显示宽度：CJK / 全角字符按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _clip(text: str, limit: int) -> str:
    if _display_width(text) <= limit:
        return text
    out = ""
    for char in text:
        if _display_width(out + char) > limit - 1:
            return out.rstrip() + "…"
        out += char
    return out


def _expected_summary(prompt: dict) -> str:
    expect = prompt.get("expect", {})
    model = expect.get("model")
    model = "/".join(model) if isinstance(model, list) else (model or "-")
    text = f"{expect.get('target_id', '-')} → {model}"
    if expect.get("reasoning") is not None:
        text += f" · {expect['reasoning']}"
    return text


def _compare(prompt: dict, status: int, data: dict) -> tuple[bool, list[str], dict]:
    """返回 (是否通过, 失败原因列表, 实际值摘要 dict)。"""
    expect = prompt.get("expect", {})
    failures: list[str] = []
    if status != 200:
        detail = data.get("detail", data) if isinstance(data, dict) else data
        if status == 503:
            failures.append(f"HTTP 503（{_brief(detail, 160)}）；若刚做过熔断注入，请等待半开窗口或重启服务后重试")
        else:
            failures.append(f"HTTP {status}: {_brief(detail, 300)}")
        return False, failures, {"target": "-", "model": "-", "candidates": [], "text": f"HTTP {status}"}

    outcome = data.get("outcome", {})
    candidates = [item.get("target_id") for item in outcome.get("candidates", [])]
    actual = {
        "target": outcome.get("target_id") or "-",
        "model": outcome.get("model") or "-",
        "reasoning": outcome.get("reasoning"),
        "candidates": candidates,
        "text": (f"target={outcome.get('target_id')} model={outcome.get('model')} "
                 f"reasoning={outcome.get('reasoning')} candidates={candidates}"),
    }
    target = expect.get("target_id")
    if target is not None and outcome.get("target_id") != target:
        failures.append(f"target 期望 {target}，实际 {outcome.get('target_id')}")
    if "model" in expect:
        model_ok = expect["model"] if isinstance(expect["model"], list) else [expect["model"]]
        if outcome.get("model") not in model_ok:
            failures.append(f"model 期望 {model_ok}，实际 {outcome.get('model')}")
    if "reasoning" in expect and outcome.get("reasoning") != expect["reasoning"]:
        failures.append(f"reasoning 期望 {expect['reasoning']!r}，实际 {outcome.get('reasoning')!r}")
    if "candidates" in expect:
        if sorted(candidates) != sorted(expect["candidates"]):
            failures.append(f"candidates 期望 {sorted(expect['candidates'])}，实际 {sorted(candidates)}")
    warnings = data.get("warnings") or []
    if warnings:
        codes = [(item.get("code"), item.get("zh") or item.get("en")) for item in warnings]
        failures.append(f"enable 级校验不通过（warnings）：{codes}")
    return not failures, failures, actual


# --------------------------------------------------------------------------- #
# 部署步骤（目录 → 画布 → dry-run）
# --------------------------------------------------------------------------- #
def _reasoning_expectations(models: list[dict], formats: dict[str, str]):
    """从内嵌 profile 派生推理声明期望（不再维护独立的硬编码档位表）。"""
    for model in models:
        declared = (model.get("profile") or {}).get("reasoning")
        if not isinstance(declared, dict):
            continue
        fmt = declared.get("format") or formats.get(model.get("provider"))
        yield (
            model["id"],
            fmt,
            tuple((declared.get("efforts") or {}).keys()),
            declared.get("default"),
        )


def step_providers(api: Api, runtime: dict, verbose: bool) -> None:
    for provider in runtime["providers"]:
        action = api.upsert_provider(provider)
        if verbose:
            print(f"  provider {provider['name']}: {action}（base_url={provider['base_url']}）")
    rows = api.providers()
    for provider in runtime["providers"]:
        row = rows.get(provider["name"])
        if row is None:
            raise ApiError("GET", "/admin/providers", 200, f"provider {provider['name']} 写入后不可见")
        for field in ("base_url", "key_env", "reasoning_format"):
            expected = provider.get(field) or ""
            actual = row.get(field) or ""
            if field == "base_url":
                expected, actual = expected.rstrip("/"), actual.rstrip("/")
            if expected != actual:
                raise ApiError(
                    "GET", "/admin/providers", 200,
                    f"{provider['name']}.{field} 期望 {expected!r}，实际 {actual!r}")
    print(f"provider: {', '.join(provider['name'] for provider in runtime['providers'])} 就绪")


def step_models(api: Api, runtime: dict, verbose: bool) -> None:
    formats = {provider["name"]: provider["reasoning_format"] for provider in runtime["providers"]}
    for model in runtime["models"]:
        action = api.upsert_model(model)
        if verbose:
            print(f"  model {model['id']}: {action}（upstream={model['upstream']}）")
    rows = api.models()
    for model_id, fmt, required_efforts, default in _reasoning_expectations(runtime["models"], formats):
        row = rows.get(model_id)
        if row is None:
            raise ApiError("GET", "/admin/models", 200, f"模型 {model_id} 不存在")
        declared = row.get("reasoning") or {}
        efforts = {item.get("id") for item in declared.get("efforts", []) if isinstance(item, dict)}
        problems = []
        if not declared.get("declared") or not declared.get("supported"):
            problems.append("reasoning 未声明或标记为不支持")
        if declared.get("format") != fmt:
            problems.append(f"format 期望 {fmt} 实际 {declared.get('format')}")
        missing = set(required_efforts) - efforts
        if missing:
            problems.append(f"缺少档位 {sorted(missing)}")
        if default is not None and declared.get("default") != default:
            problems.append(f"default 期望 {default} 实际 {declared.get('default')}")
        if declared.get("declared") and default is not None and default not in efforts:
            problems.append(f"default {default} 不在 efforts 里")
        if problems:
            raise ApiError("GET", "/admin/models", 200, f"{model_id}: " + "；".join(problems))
    for model in runtime["models"]:
        row = rows.get(model["id"])
        expected = (model.get("profile") or {}).get("capability")
        if row is None:
            raise ApiError("GET", "/admin/models", 200, f"模型 {model['id']} 不存在")
        if expected and (row.get("capability") or "").strip() != expected:
            raise ApiError("GET", "/admin/models", 200,
                           f"{model['id']}: profile.capability 期望 {expected!r}，实际 {row.get('capability')!r}")
    print(f"models: {len(runtime['models'])} 个演示模型就绪"
          f"（provider={', '.join(sorted(provider['name'] for provider in runtime['providers']))}），"
          f"推理声明与能力说明校验通过")


def step_jev_services(api: Api, runtime: dict, verbose: bool) -> None:
    for service in runtime["jev_services"]:
        action = api.upsert_jev_service(service)
        if verbose:
            print(f"  jev {service['id']}: {action}（base_url={service['base_url']}，"
                  f"window={service['input_tokens']}/{service['context_tokens']}）")
    rows = api.jev_services()
    for service in runtime["jev_services"]:
        row = rows.get(service["id"])
        if row is None:
            raise ApiError("GET", "/admin/jev", 200, f"JEV 服务 {service['id']} 写入后不可见")
        for field in ("base_url", "model", "input_tokens", "context_tokens"):
            expected, actual = service.get(field), row.get(field)
            if field == "base_url":
                expected, actual = (expected or "").rstrip("/"), (actual or "").rstrip("/")
            if expected != actual:
                raise ApiError(
                    "GET", "/admin/jev", 200,
                    f"{service['id']}.{field} 期望 {expected!r}，实际 {actual!r}")
    print("jev: " + "；".join(
        f"{service['id']} @ {service['base_url']} "
        f"window={service['input_tokens']}/{service['context_tokens']}"
        for service in runtime["jev_services"]) + " 就绪")


def step_time_periods(api: Api, runtime: dict, verbose: bool) -> None:
    periods = runtime["time_periods"]
    current = api.time_periods()
    if current == periods:
        print(f"time_periods: {len(periods)} 个演示时段已就绪（{', '.join(p['name'] for p in periods)}）")
        return
    # 时段是整表覆盖写入：先打印新增/删除的名字，让破坏性变更可见。
    before_names = [item.get("name") for item in current if isinstance(item, dict)]
    after_names = [period["name"] for period in periods]
    added = [name for name in after_names if name not in before_names]
    removed = [name for name in before_names if name not in after_names]
    print(f"time_periods: 覆盖写入 {len(before_names)} → {len(after_names)} 个时段"
          f"（新增：{added or '无'}；删除：{removed or '无'}）")
    api.save_time_periods(periods)
    rows = api.time_periods()
    if rows != periods:
        raise ApiError("GET", "/admin/time-periods", 200,
                       f"时段写入后不一致：期望 {_brief(periods, 300)}，实际 {_brief(rows, 300)}")
    if verbose:
        print(f"  time_periods: {len(periods)} 个时段已写入")
    print(f"time_periods: {len(periods)} 个演示时段就绪（{', '.join(p['name'] for p in periods)}）")


def step_trees(api: Api, cases: list[dict], documents: dict[str, dict], verbose: bool) -> None:
    for case in cases:
        action = api.upsert_tree(case["id"], documents[case["id"]])
        print(f"tree {case['id']}: {action}（{case['name']}）")
    rows = api.trees()
    for case in cases:
        if case["id"] not in rows:
            raise ApiError("GET", "/admin/trees", 200, f"决策组 {case['id']} 写入后不可见")
        if rows[case["id"]].get("enabled"):
            raise ApiError("GET", "/admin/trees", 200, f"决策组 {case['id']} 处于 enabled，本脚本不应启用任何新树")
    for case in cases:
        tree = rows[case["id"]]
        if not tree.get("document_sha256"):
            raise ApiError("GET", "/admin/trees", 200, f"决策组 {case['id']} 缺少 document_sha256")
        for field in RUNTIME_FIELDS:
            if field not in tree:
                raise ApiError("GET", "/admin/trees", 200,
                               f"决策组 {case['id']} 的存储文档缺少运行字段 {field}")


def step_dry_run(api: Api, cases: list[dict], verbose: bool) -> int:
    failures = 0
    print()
    print("dry-run 验证（POST /admin/try，不落库、不 enable）")
    widths = (16, 24, 38, 38, 32, 4)
    header = (
        _pad("案例", widths[0]) + _pad("演示 prompt", widths[1])
        + _pad("期望 target → model", widths[2]) + _pad("实际 target → model", widths[3])
        + _pad("JEV 证据", widths[4]) + "结果"
    )
    print(header)
    print("-" * _display_width(header))
    for case in cases:
        for prompt in case["prompts"]:
            status, data = api.try_tree(case["id"], prompt["text"], prompt.get("now"))
            passed, reasons, actual = _compare(prompt, status, data)
            answers = data.get("answers") if isinstance(data, dict) else None
            evidence = _jev_evidence(answers) if isinstance(answers, dict) else "-"
            print(
                _pad(case["id"], widths[0])
                + _pad(_clip(prompt["text"], widths[1] - 2), widths[1])
                + _pad(_clip(_expected_summary(prompt), widths[2] - 2), widths[2])
                + _pad(_clip(f"{actual['target']} → {actual['model']}", widths[3] - 2), widths[3])
                + _pad(_clip(evidence, widths[4] - 2), widths[4])
                + (OK if passed else BAD)
            )
            if verbose or not passed:
                print(f"  {actual['text']}")
                if prompt.get("note"):
                    print(f"  说明：{prompt['note']}")
            for reason in reasons:
                print(f"  {BAD} {reason}")
            if not passed:
                failures += 1
    return failures


def enabled_tree_ids(api: Api) -> list[str]:
    """当前 enabled 的决策组 id（排序）。部署前后各取一次作为最终状态基线。"""
    rows = api.trees()
    return sorted(tree_id for tree_id, row in rows.items() if row.get("enabled"))


def step_final_state(api: Api, cases: list[dict], enabled_before: list[str]) -> list[str]:
    """最终状态校验：以部署前的 enabled 集合为基线，而不是写死 ``default-jev``。

    - 全新实例（部署前没有 enabled 决策组）部署后仍为空是正确结果：脚本只部署、
      从不启用任何树，启用是单独步骤；
    - 既有实例（例如 76 上的 ``default-jev``）部署后必须原样保持 enabled；
    - 5 棵演示树必须全部未启用且带 ``document_sha256``。

    ``enabled_before`` 由调用方在部署前用 :func:`enabled_tree_ids` 记录；影响确认
    同步会更新受影响画布的文档，但不会改变 enabled 状态。
    """
    problems: list[str] = []
    rows = api.trees()
    enabled = sorted(tree_id for tree_id, row in rows.items() if row.get("enabled"))
    expected = sorted(enabled_before)
    if enabled != expected:
        if expected:
            problems.append(
                f"enabled 决策组应保持部署前的 {expected}，实际 {enabled}；"
                f"请勿启用演示决策组，并恢复既有 enabled 状态")
        else:
            problems.append(
                f"部署前实例没有 enabled 决策组，部署后不应新增，实际 {enabled}；"
                f"请勿启用演示决策组")
    elif expected:
        print(f"最终状态：部署前 enabled 的决策组 {expected} 保持不变"
              f"（{len(cases)} 棵演示树保持未启用）")
    else:
        print(f"最终状态：实例部署前没有 enabled 决策组，部署后仍为空"
              f"（{len(cases)} 棵演示树保持未启用；启用是单独步骤）")
    for tree_id in expected:
        row = rows.get(tree_id)
        if row is None:
            problems.append(f"部署前 enabled 的决策组 {tree_id} 不见了")
        elif not row.get("document_sha256"):
            problems.append(f"部署前 enabled 的决策组 {tree_id} 缺少 document_sha256")
    for case in cases:
        if case["id"] not in rows:
            problems.append(f"缺少决策组 {case['id']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署并 dry-run 验证 5 个 RoutePilot 演示决策组")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"服务地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--only", action="append", default=None, help="只处理指定案例 id（可重复）")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每个 prompt 的实际命中与说明")
    parser.add_argument(
        "--confirm-sync", action="store_true",
        help="对 409 impact_confirmation_required 用响应里的基线哈希显式确认并重试；默认停止")
    args = parser.parse_args(argv)

    manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    all_cases = manifest["cases"]
    try:
        documents = load_case_documents(all_cases)
        runtime = case_runtime(list(documents.values()))
    except (KeyError, TypeError, ValueError) as exc:
        print(f"{BAD} 案例文档无法合并为运行目录：{exc}")
        return 1

    cases = all_cases
    if args.only:
        wanted = set(args.only)
        cases = [case for case in all_cases if case["id"] in wanted]
        unknown = wanted - {case["id"] for case in cases}
        if unknown:
            parser.error(f"未知案例 id：{sorted(unknown)}")

    api = Api(args.base_url, args.email, args.password)
    api.confirm_sync = args.confirm_sync
    api.login()
    print(f"已登录 {api.base_url}（email={args.email}）")
    try:
        # 先记下部署前的 enabled 集合：全新实例为空是合法基线，不能假定 default-jev 存在。
        enabled_before = enabled_tree_ids(api)
        step_providers(api, runtime, args.verbose)
        step_models(api, runtime, args.verbose)
        step_jev_services(api, runtime, args.verbose)
        step_time_periods(api, runtime, args.verbose)
        step_trees(api, cases, documents, args.verbose)
        failures = step_dry_run(api, cases, args.verbose)
        problems = step_final_state(api, cases, enabled_before)
    except ImpactConfirmationRequired as exc:
        for line in impact_report_lines(exc.detail):
            print(line)
        print(f"{BAD} 已停止：未写入任何后续内容（--confirm-sync 才会显式同步受影响画布）")
        return 1
    except GraphConflictError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        actual = detail.get("actual_document_sha256") or "-"
        print(f"{BAD} 画布已变化（409 graph_conflict）：实际 document_sha256={actual}")
        print(f"{BAD} 画布已变化，请重新运行本脚本以读取最新的影响清单；"
              f"已停止，未写入任何后续内容")
        return 1
    except ApiError as exc:
        print(f"{BAD} 管理请求失败（{exc.method} {exc.path} → HTTP {exc.status}）："
              f"{_brief(exc.detail, 400)}")
        print(f"{BAD} 已停止，未写入任何后续内容")
        return 1
    except KeyError as exc:
        print(f"{BAD} 案例文档缺少字段 {exc}（数据错误，请修正 configs/cases/*.json）")
        print(f"{BAD} 已停止，未写入任何后续内容")
        return 1

    for problem in problems:
        print(f"{BAD} {problem}")
    total = sum(len(case["prompts"]) for case in cases)
    print()
    if failures or problems:
        print(f"{BAD} 失败：{failures} 条 dry-run 不匹配，{len(problems)} 项最终状态问题")
        return 1
    print(f"{OK} 全部通过：{len(cases)} 个决策组、{total} 条 dry-run 均命中期望，"
          f"部署前的 enabled 状态未变（{enabled_before or '无'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
