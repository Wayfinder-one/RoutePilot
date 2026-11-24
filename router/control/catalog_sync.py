"""确认后的目录写与候选画布原子同步（模型 / provider / JEV 共用用例）。

职责边界：
- API 层负责 DTO、资源存在性、影响预览（``router.control.impacts``）与候选
  画布构造；本服务只负责提交：一个写锁 + 一个数据库事务里执行
  ``recheck(conn)``（锁内重算最新引用/影响，可选）、``db_write(conn)``、严格
  校验每份候选画布、预检并提交画布图批次、按完整启用集合构造并复核候选快照、
  记录审计，提交成功后才发布内存快照。
- ``candidates`` 是保留原始 ``document_sha256`` 的新 ``RoutingTree``；该哈希
  就是 ``RoutingTreeRepo.stage_updates()`` 的基线。哈希已被外部改写时由
  ``GraphConflictError`` 在事务内抛出，数据库与候选文件一起回滚。
- 失败语义：任一候选校验失败 → ``ValidationFailed``（422）；图文件替换失败 →
  ``GraphStorageError``（503），已替换的文件在数据库回滚后恢复；审计随数据库
  事务一起回滚。没有候选时只写数据库与审计，不构造、也不发布快照。候选快照与
  指纹复核始终覆盖完整启用集合；只有候选改写了至少一张启用图时才发布——只改
  草稿（或只改非启用图）不触发与目录写无关的路由集替换。

复用 ``ConfigurationService`` 的共享骨架（``_commit_locked`` +
``_recheck_fingerprint`` + ``_rollback_graph_batch``），不另建多文件写入协议，
也不嵌套裸 ``BEGIN IMMEDIATE``。
"""
from __future__ import annotations

from router.control.config import ConfigurationService

__all__ = ["CatalogSyncService"]


class CatalogSyncService:
    def __init__(self, conn, trees, publisher):
        self.conn = conn
        self.trees = trees
        self.publisher = publisher

    def commit(self, *, db_write, candidates, audit, recheck=None) -> list[str]:
        """原子提交目录写 + 候选画布；返回同步的画布 id 列表。

        ``recheck(conn)``：可选。在写锁内、事务内、``db_write`` 之前执行，用于
        重算最新引用/影响清单（把「路由层扫描之后、加锁之前」新出现的引用变成
        409，而不是漏同步）。
        ``db_write(conn)``：在事务内写数据库目录行（失败视为用例失败）。
        ``audit(conn)``：在事务内记录审计行，应包含 ``synced_trees``。
        """
        return ConfigurationService(
            self.conn, self.trees, self.publisher
        ).commit_catalog_write(
            db_write=db_write, candidates=candidates, audit=audit,
            recheck=recheck)
