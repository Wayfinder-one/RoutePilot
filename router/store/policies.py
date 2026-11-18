"""Routing Policy v4 文件仓库：纯文件存储，绝不触碰数据库或 HTTP。

目录布局（固定）::

    {root}/policies/{id}.json   完整 v4 行为文档（唯一写入内容）
    {root}/layouts/{id}.json     前端私有布局 JSON（设计 §6.3）
    {root}/current               当前启用策略指针（尾随换行或空）

策略文件内容仅为 v4 文档；资源 id、enabled、updated_at、document_sha256 与
policy_revision 全部由路径 + current 指针派生（设计 §4.7），绝不写回文档。
布局是独立的前端文档：后端只负责独立存取与 1 MiB 上限，不解析为行为模型，
不参与策略校验、启用、行为版本或 If-Match。

原子写复用 RoutingTreeRepo 的 fsync + ``os.replace`` 实现，作为模块私有
``_atomic_write`` 整体搬入（不留旧类包装）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from router.routing.document import parse_policy_document, policy_document_bytes
from router.routing.policy.model import Policy, PolicyResource, policy_revision_of

#: 资源 ID 唯一合法形状：允许点号（比问题 ID 宽），首字符字母数字，最长 64。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 布局文件编码后的字节上限（设计 §6.3：后端只做独立存取与基本大小限制）。
MAX_LAYOUT_BYTES = 1024 * 1024


class PolicyStorageError(RuntimeError):
    pass


class PolicyReadOnlyError(PolicyStorageError):
    pass


class PolicyAlreadyExistsError(PolicyStorageError):
    pass


class PolicyChangedDuringLoad(PolicyStorageError):
    pass


class PolicyConflictError(PolicyStorageError):
    def __init__(self, actual_document_sha256: str | None):
        super().__init__("policy_conflict")
        self.actual_document_sha256 = actual_document_sha256


class ActivePolicyError(PolicyStorageError):
    """删除当前启用策略被拒绝（服务抛出，API 映射 409）。"""

    def __init__(self, policy_id: str):
        super().__init__("policy_is_active")
        self.policy_id = policy_id


class PolicyRepo:
    def __init__(self, root: str | Path, *, writer: bool = True):
        self.root = Path(root)
        self.policies = self.root / "policies"
        self.layouts = self.root / "layouts"
        self.current = self.root / "current"
        self.writer = writer

    def initialize(self) -> None:
        self._require_writer()
        self.policies.mkdir(parents=True, exist_ok=True)
        self.layouts.mkdir(parents=True, exist_ok=True)
        if not self.current.exists():
            _atomic_write(self.current, b"")

    def active_id(self) -> str | None:
        try:
            raw = self.current.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PolicyStorageError("current pointer is unavailable") from exc
        value = raw[:-1] if raw.endswith("\n") else raw
        if not value:
            return None
        if not _ID_RE.fullmatch(value):
            raise PolicyStorageError("current pointer contains an invalid policy id")
        return value

    def fingerprint(self) -> tuple[str | None, str]:
        active_id = self.active_id()
        if active_id is None:
            return None, ""
        try:
            content = (self.policies / f"{active_id}.json").read_bytes()
        except OSError as exc:
            raise PolicyStorageError(f"active policy is unavailable: {active_id}") from exc
        return active_id, hashlib.sha256(content).hexdigest()

    def active(self) -> PolicyResource | None:
        active_id = self.active_id()
        if active_id is None:
            return None
        resource = self.get(active_id)
        if resource is None:
            raise PolicyStorageError(f"active policy is missing: {active_id}")
        return resource

    def get(self, policy_id: str) -> PolicyResource | None:
        self._validate_id(policy_id)
        path = self.policies / f"{policy_id}.json"
        if not path.exists():
            return None
        return self._read(path, policy_id, self.active_id())

    def list(self) -> list[PolicyResource]:
        active_id = self.active_id()
        resources = []
        for path in self.policies.glob("*.json"):
            policy_id = path.stem
            self._validate_id(policy_id)
            resources.append(self._read(path, policy_id, active_id))
        return sorted(resources, key=lambda resource: (resource.updated_at, resource.id))

    def create(self, policy_id: str, policy: Policy) -> PolicyResource:
        self._require_writer()
        self._validate_id(policy_id)
        path = self.policies / f"{policy_id}.json"
        if path.exists():
            raise PolicyAlreadyExistsError(policy_id)
        resource = PolicyResource(
            id=policy_id, policy=policy, enabled=False,
            policy_revision=policy_revision_of(policy),
        )
        _atomic_write(path, policy_document_bytes(resource))
        return self._read(path, policy_id, self.active_id())

    def update(self, policy_id: str, policy: Policy, *,
               base_document_sha256: str) -> PolicyResource:
        self._require_writer()
        current = self.get(policy_id)
        if current is None:
            raise KeyError(policy_id)
        actual = hashlib.sha256(
            (self.policies / f"{policy_id}.json").read_bytes()
        ).hexdigest()
        if not base_document_sha256 or actual != base_document_sha256:
            raise PolicyConflictError(actual)
        resource = PolicyResource(
            id=policy_id, policy=policy, enabled=current.enabled,
            policy_revision=policy_revision_of(policy),
        )
        _atomic_write(self.policies / f"{policy_id}.json", policy_document_bytes(resource))
        return self._read(self.policies / f"{policy_id}.json", policy_id, self.active_id())

    def delete(self, policy_id: str) -> bool:
        self._require_writer()
        self._validate_id(policy_id)
        path = self.policies / f"{policy_id}.json"
        if not path.exists():
            raise KeyError(policy_id)
        if self.active_id() == policy_id:
            raise ActivePolicyError(policy_id)
        path.unlink()
        # 布局随策略一并清除：同 ID 重建不继承旧 UI（brief Step 4）。
        (self.layouts / f"{policy_id}.json").unlink(missing_ok=True)
        return True

    def enable(self, policy_id: str) -> PolicyResource:
        self._require_writer()
        resource = self.get(policy_id)
        if resource is None:
            raise KeyError(policy_id)
        _atomic_write(self.current, (policy_id + "\n").encode("utf-8"))
        return self._read(self.policies / f"{policy_id}.json", policy_id, policy_id)

    def disable(self, policy_id: str) -> bool:
        self._require_writer()
        if self.get(policy_id) is None:
            return False
        if self.active_id() == policy_id:
            _atomic_write(self.current, b"")
        return True

    # ── 布局：前端私有 JSON，独立存取（设计 §6.3）─────────────────────
    def get_layout(self, policy_id: str) -> dict:
        self._validate_id(policy_id)
        if self.get(policy_id) is None:
            raise KeyError(policy_id)
        path = self.layouts / f"{policy_id}.json"
        if not path.exists():
            return {}
        try:
            layout = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PolicyStorageError(f"invalid layout document: {policy_id}") from exc
        if not isinstance(layout, dict):
            raise PolicyStorageError(f"invalid layout document: {policy_id}")
        return layout

    def put_layout(self, policy_id: str, layout: dict) -> dict:
        self._require_writer()
        self._validate_id(policy_id)
        if self.get(policy_id) is None:
            raise KeyError(policy_id)
        if not isinstance(layout, dict):
            raise ValueError("layout must be a JSON object")
        content = (
            json.dumps(layout, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(content) > MAX_LAYOUT_BYTES:
            raise ValueError(
                f"layout document exceeds the {MAX_LAYOUT_BYTES} byte limit")
        self.layouts.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.layouts / f"{policy_id}.json", content)
        return self.get_layout(policy_id)

    # ── 内部 ────────────────────────────────────────────────────────────
    def _read(self, path: Path, policy_id: str, active_id: str | None) -> PolicyResource:
        try:
            content = path.read_bytes()
            modified = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            return parse_policy_document(
                content,
                policy_id=policy_id,
                enabled=active_id == policy_id,
                updated_at=modified,
                document_sha256=hashlib.sha256(content).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise PolicyStorageError(f"invalid policy document: {policy_id}") from exc

    def _require_writer(self) -> None:
        if not self.writer:
            raise PolicyReadOnlyError("policy_read_only")

    @staticmethod
    def _validate_id(policy_id: str) -> None:
        if not _ID_RE.fullmatch(policy_id):
            raise PolicyStorageError(f"invalid policy id: {policy_id!r}")


def _atomic_write(path: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
