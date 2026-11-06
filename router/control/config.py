"""控制面配置写用例：写入、校验、审计与候选快照构造共享一个事务。

破坏一致性约束的任何一步失败都回滚数据库；发布（内存替换 app.state.snapshot）
只发生在数据库提交之后。
"""
from __future__ import annotations

from router.control.validation import active_tree_issues
from router.jev.config import JevConfigRepo, JevSettings
from router.store.repositories import ChangeRepo, now_iso, write_transaction


class ValidationFailed(ValueError):
    def __init__(self, issues):
        super().__init__("configuration validation failed")
        self.issues = list(issues)


class ConfigurationService:
    def __init__(self, conn, trees, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    def save_jev(self, settings: JevSettings) -> None:
        with self.publisher.locked():
            with write_transaction(self.conn):
                JevConfigRepo(self.conn).save(settings)
                issues = active_tree_issues(self.conn, self.trees)
                if issues:
                    raise ValidationFailed(issues)
                ChangeRepo(self.conn).record(
                    now_iso(), "jev", {"action": "save", "model": settings.model}
                )
                tree = self.trees.active()
                prepared = self.publisher.prepare(
                    self.conn, tree, self.trees.fingerprint()
                )
            self.publisher.publish(prepared)
