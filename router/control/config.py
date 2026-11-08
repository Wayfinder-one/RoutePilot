"""控制面配置写用例：写入、校验、审计与候选快照构造共享一个事务。

破坏一致性约束的任何一步失败都回滚数据库；发布（内存替换 app.state.snapshot）
只发生在数据库提交之后。引用扫描、审计与发布由服务负责；API 只做 DTO 解析
与异常映射。

服务层异常 → HTTP 的映射关系（routes_* 统一处理）：
- ``ValidationFailed`` → 422（Issue 数组）
- ``ValueError`` → 422（detail 字符串）
- ``MissingError`` → 404（detail 字符串）
- ``AlreadyExists`` / ``ReferencedByError`` → 409（detail 字符串 / dict）
"""
from __future__ import annotations

from dataclasses import dataclass

from router.control.validation import active_tree_issues
from router.jev.config import JevSettings
from router.jev.window import MAX_TOKENS, MIN_CONTEXT_TOKENS, MIN_INPUT_TOKENS
from router.store.jev_config import JevConfigRepo
from router.store.repositories import (
    ChangeRepo,
    ModelRepo,
    ProviderRepo,
    SettingsRepo,
    TimePeriodRepo,
    model_ids_using_provider,
    model_references,
    now_iso,
    write_transaction,
)
from router.store.routing_trees import GraphChangedDuringLoad


class ValidationFailed(ValueError):
    def __init__(self, issues):
        super().__init__("configuration validation failed")
        self.issues = list(issues)


class MissingError(Exception):
    """写用例的目标资源不存在（服务抛出，API 映射 404）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AlreadyExists(Exception):
    """写用例的目标资源已存在（服务抛出，API 映射 409）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ReferencedByError(Exception):
    """写用例的目标仍被引用（服务抛出，API 映射 409，detail 直接透传）。"""

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "referenced"))
        self.detail = detail


@dataclass(frozen=True)
class JevWindow:
    """JEV 输入窗口的服务层数值：范围与关系校验一体完成（路由只做折算）。"""

    input_tokens: int
    context_tokens: int

    def __post_init__(self):
        if isinstance(self.context_tokens, bool) or not isinstance(self.context_tokens, int):
            raise ValueError("context_tokens 必须是整数")
        if isinstance(self.input_tokens, bool) or not isinstance(self.input_tokens, int):
            raise ValueError("input_tokens 必须是整数")
        if not MIN_CONTEXT_TOKENS <= self.context_tokens <= MAX_TOKENS:
            raise ValueError(
                f"context_tokens 必须在 {MIN_CONTEXT_TOKENS}..{MAX_TOKENS} 之间")
        if not MIN_INPUT_TOKENS <= self.input_tokens <= MAX_TOKENS:
            raise ValueError(
                f"input_tokens 必须在 {MIN_INPUT_TOKENS}..{MAX_TOKENS} 之间")
        if self.input_tokens > self.context_tokens:
            raise ValueError("input_tokens 不能大于 context_tokens")


class ConfigurationService:
    def __init__(self, conn, trees, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    # ── 共享骨架：publisher.locked() → 写事务（写入/校验/审计/prepare/复核指纹）→
    #    COMMIT → publish（纯内存替换）。复核在事务内、提交之前进行：指纹仍不稳定
    #    时在 COMMIT 前抛冲突，DB、审计与候选快照一并回滚，且不发布。publish 不进
    #    数据库事务。─────────
    def _write_and_publish(self, write):
        with self.publisher.locked():
            with write_transaction(self.conn):
                prepared = write(self.conn)
                prepared = self._recheck_fingerprint(prepared)
            self.publisher.publish(prepared)

    def _recheck_fingerprint(self, prepared):
        """COMMIT 前在同一事务连接上复核磁盘指纹（等价旧 SnapshotPublisher.refresh 的复核语义）。

        同一连接能看到本事务未提交的写入，重建候选快照读到的是事务内配置。若图在
        候选快照构造后变了，用新指纹重建一次；仍不稳定就在提交前抛与 GraphService
        相同的冲突并回滚，绝不把过期指纹打给已发布快照。
        """
        for _attempt in range(2):
            fingerprint = self.trees.fingerprint()
            if fingerprint == prepared.fingerprint:
                return prepared
            prepared = self.publisher.prepare(
                self.conn, self.trees.active(), fingerprint)
        raise GraphChangedDuringLoad(
            "graph changed between document write and snapshot publish")

    def _require_valid_active_graph(self) -> None:
        issues = active_tree_issues(self.conn, self.trees)
        if issues:
            raise ValidationFailed(issues)

    def _prepared_snapshot(self):
        # 必须先取指纹再读树：两次读取之间图若被改写，_recheck_fingerprint 会看到
        # 不匹配并重建一次。反过来（先读树、后取指纹）会产出（过期树, 当前指纹），
        # 复核只看指纹标签，于是把过期树当最新快照发布出去。
        fingerprint = self.trees.fingerprint()
        return self.publisher.prepare(self.conn, self.trees.active(), fingerprint)

    def save_jev(self, settings: JevSettings) -> None:
        self._write_and_publish(lambda conn: self._save_jev_in_tx(settings))

    def _save_jev_in_tx(self, settings: JevSettings):
        JevConfigRepo(self.conn).save(settings)
        self._require_valid_active_graph()
        ChangeRepo(self.conn).record(
            now_iso(), "jev", {"action": "save", "model": settings.model}
        )
        return self._prepared_snapshot()

    def save_window(self, window: JevWindow) -> None:
        self._write_and_publish(lambda conn: self._save_window_in_tx(window))

    def _save_window_in_tx(self, window: JevWindow):
        repo = SettingsRepo(self.conn)
        repo.set("jev_context_tokens", str(window.context_tokens))
        repo.set("jev_input_tokens", str(window.input_tokens))
        self._require_valid_active_graph()
        ChangeRepo(self.conn).record(now_iso(), "jev_window", {
            "action": "window",
            "context_tokens": window.context_tokens,
            "input_tokens": window.input_tokens,
        })
        return self._prepared_snapshot()

    def save_periods(self, periods: list) -> None:
        """时段替换写用例：TimePeriodRepo.save 的 ValueError（结构/重名）原样上抛。"""
        self._write_and_publish(lambda conn: self._save_periods_in_tx(periods))

    def _save_periods_in_tx(self, periods: list):
        TimePeriodRepo(self.conn).save(periods)
        self._require_valid_active_graph()
        ChangeRepo(self.conn).record(
            now_iso(), "time-periods", {"periods": len(periods)}
        )
        return self._prepared_snapshot()

    # ── 模型与提供方 ────────────────────────────────────────────────────
    # audit_* 参数仅为审计行保留请求原始值（与历史 /admin/changes 明细一致）；
    # 其余参数是已归一化的写值。
    def create_model(self, *, model_id: str, upstream: str, provider: str,
                     profile_text: str | None, audit_profile) -> None:
        def write(conn):
            if not ModelRepo(conn).create(model_id, upstream, provider, profile_text or "{}"):
                raise AlreadyExists(f"model {model_id} 已存在")
            self._require_valid_active_graph()
            ChangeRepo(conn).record(now_iso(), "model", {
                "action": "create", "id": model_id,
                "upstream": upstream, "provider": provider,
                "profile": audit_profile,
            })
            return self._prepared_snapshot()
        self._write_and_publish(write)

    def update_model(self, *, model_id: str, upstream: str | None,
                     provider: str | None, profile_text: str | None,
                     audit_upstream, audit_provider, audit_profile) -> None:
        def write(conn):
            if not ModelRepo(conn).update(
                    model_id, upstream=upstream, provider=provider, profile=profile_text):
                raise MissingError(f"unknown model {model_id}")
            self._require_valid_active_graph()
            ChangeRepo(conn).record(now_iso(), "model", {
                "action": "update", "id": model_id,
                "upstream": audit_upstream, "provider": audit_provider,
                "profile": audit_profile,
            })
            return self._prepared_snapshot()
        self._write_and_publish(write)

    def delete_model(self, model_id: str) -> None:
        def write(conn):
            if ModelRepo(conn).get(model_id) is None:
                raise MissingError(f"unknown model {model_id}")
            refs = model_references(self.trees.list(), model_id)
            if refs:
                raise ReferencedByError({
                    "message": f"model {model_id} 被路由树引用", "references": refs})
            if not ModelRepo(conn).delete(model_id):
                raise MissingError(f"unknown model {model_id}")
            ChangeRepo(conn).record(
                now_iso(), "model", {"action": "delete", "id": model_id})
            return self._prepared_snapshot()
        self._write_and_publish(write)

    def upsert_provider(self, *, name: str, base_url: str, key_env: str | None,
                        reasoning_format: str | None, api_key: str | None) -> None:
        def write(conn):
            existing = ProviderRepo(conn).get(name)
            ProviderRepo(conn).upsert(
                name, base_url, key_env=key_env,
                reasoning_format=reasoning_format, api_key=api_key)
            self._require_valid_active_graph()
            ChangeRepo(conn).record(now_iso(), "provider", {
                "action": "upsert", "name": name, "base_url": base_url,
                "key_env": key_env if key_env is not None else (existing or {}).get("key_env", ""),
                "reasoning_format": reasoning_format or (existing or {}).get("reasoning_format", "openai"),
                "api_key": "unchanged" if api_key is None else "cleared" if api_key == "" else "set",
            })
            return self._prepared_snapshot()
        self._write_and_publish(write)

    def delete_provider(self, name: str) -> None:
        def write(conn):
            if ProviderRepo(conn).get(name) is None:
                raise MissingError(f"unknown provider {name}")
            used_by = model_ids_using_provider(conn, name)
            if used_by:
                raise ReferencedByError({"message": "provider 被模型引用", "models": used_by})
            if not ProviderRepo(conn).delete(name):
                raise MissingError(f"unknown provider {name}")
            ChangeRepo(conn).record(
                now_iso(), "provider", {"action": "delete", "name": name})
            return self._prepared_snapshot()
        self._write_and_publish(write)
