"""策略（policy）写用例：CRUD/启停的校验、候选快照与发布次序（brief Step 4）。

- `create`：结构校验 →（锁内）文档写入 → 审计；不发布快照（新策略为草稿态）。
- `update`：锁内读取 → If-Match 形状检查 → 启用与否分档校验（已启用走
  `policy_issues`，草稿走 `validate_policy`）→ 已启用策略 prepare 候选快照 →
  `base_document_sha256` 冲突检查后文件成功写入 → 指纹复核（等价于旧
  refresh() 的提交前复核，不匹配抛 `PolicyChangedDuringLoad`）→ publish → 审计。
- `enable`：启用校验通过后 prepare → current 指针写入 → 指纹复核 → publish →
  审计；任何失败不改变数据库、审计、文件指针或现有快照。
- `disable`：当前策略关闭时发布空快照；关闭非当前策略不发布。
- `delete`：仅非 active（active 抛 `ActivePolicyError`，API 映射 409）；成功后
  仓库一并移除策略与布局文件，重建同 ID 不继承旧 UI。

发布是纯内存替换（`SnapshotPublisher.publish`），绝不改文件或数据库；服务抛出
的异常由 API 层映射（ValidationFailed→422、PolicyConflictError→409、
MissingError→404、AlreadyExists→409、ActivePolicyError→409、
PolicyReadOnlyError/PolicyStorageError→503）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import replace as dataclass_replace

from router.control.errors import AlreadyExists, MissingError, ValidationFailed
from router.control.validation import policy_issues
from router.routing.document import policy_document_bytes
from router.routing.policy.model import Policy, PolicyResource, policy_revision_of
from router.routing.policy.validate import validate_policy
from router.store.policies import (
    PolicyAlreadyExistsError,
    PolicyChangedDuringLoad,
    PolicyConflictError,
    PolicyRepo,
)
from router.store.repositories import ChangeRepo, new_id, now_iso

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class PolicyService:
    def __init__(self, conn: sqlite3.Connection, policies: PolicyRepo, publisher):
        self.conn = conn
        self.policies = policies
        self.publisher = publisher

    # ── CRUD ────────────────────────────────────────────────────────────
    def create(self, policy_id: str | None, policy: Policy) -> PolicyResource:
        issues = validate_policy(policy)
        if issues:
            raise ValidationFailed(issues)
        resource_id = policy_id or new_id()
        with self.publisher.locked():
            try:
                created = self.policies.create(resource_id, policy)
            except PolicyAlreadyExistsError:
                raise AlreadyExists(f"policy {resource_id} already exists") from None
            self._audit("create", {"id": resource_id})
            return created

    def update(self, policy_id: str, policy: Policy, *,
               base_document_sha256: str) -> PolicyResource:
        with self.publisher.locked():
            current = self.policies.get(policy_id)
            if current is None:
                raise MissingError(f"unknown policy {policy_id}")
            if not isinstance(base_document_sha256, str) or not _SHA256_RE.fullmatch(base_document_sha256):
                raise PolicyConflictError(current.document_sha256)
            issues = policy_issues(self.conn, policy) if current.enabled else validate_policy(policy)
            if issues:
                raise ValidationFailed(issues)
            candidate = None
            if current.enabled:
                resource = PolicyResource(
                    id=policy_id, policy=policy, enabled=True,
                    policy_revision=policy_revision_of(policy),
                )
                fingerprint = (policy_id, hashlib.sha256(policy_document_bytes(resource)).hexdigest())
                candidate = self.publisher.prepare(self.conn, resource, fingerprint)
            updated = self.policies.update(
                policy_id, policy, base_document_sha256=base_document_sha256,
            )
            if candidate is not None:
                self._require_fingerprint(candidate.fingerprint)
                self.publisher.publish(candidate)
            self._audit("update", {"id": policy_id, "policy_revision": updated.policy_revision})
            return updated

    def delete(self, policy_id: str) -> None:
        with self.publisher.locked():
            try:
                self.policies.delete(policy_id)
            except KeyError:
                raise MissingError(f"unknown policy {policy_id}") from None
            self._audit("delete", {"id": policy_id})

    # ── 启停 ────────────────────────────────────────────────────────────
    def enable(self, policy_id: str) -> None:
        with self.publisher.locked():
            current = self.policies.get(policy_id)
            if current is None:
                raise MissingError(f"unknown policy {policy_id}")
            issues = policy_issues(self.conn, current.policy)
            if issues:
                raise ValidationFailed(issues)
            enabled_resource = dataclass_replace(current, enabled=True)
            candidate = self.publisher.prepare(
                self.conn, enabled_resource, (policy_id, current.document_sha256))
            try:
                self.policies.enable(policy_id)
            except KeyError:
                raise MissingError(f"unknown policy {policy_id}") from None
            self._require_fingerprint(candidate.fingerprint)
            self.publisher.publish(candidate)
            self._audit("enable", {"id": policy_id})

    def disable(self, policy_id: str) -> None:
        with self.publisher.locked():
            candidate = (
                self.publisher.prepare(self.conn, None, (None, ""))
                if self.policies.active_id() == policy_id else None
            )
            if not self.policies.disable(policy_id):
                raise MissingError(f"unknown policy {policy_id}")
            if candidate is not None:
                self._require_fingerprint(candidate.fingerprint)
                self.publisher.publish(candidate)
            self._audit("disable", {"id": policy_id})

    # ── 内部 ────────────────────────────────────────────────────────────
    def _require_fingerprint(self, fingerprint: tuple[str | None, str]) -> None:
        """发布前复核：内存候选指纹必须与磁盘一致（等价旧 refresh() 的复核语义）。"""
        if self.policies.fingerprint() != fingerprint:
            raise PolicyChangedDuringLoad(
                "policy changed between document write and snapshot publish")

    def _audit(self, action: str, detail: dict) -> None:
        try:
            with self.conn:
                ChangeRepo(self.conn).record(now_iso(), "policy", {"action": action, **detail})
        except sqlite3.Error:
            logger.exception("policy change committed but audit write failed: %s", action)
