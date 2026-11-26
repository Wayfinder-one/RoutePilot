"""Peripheral application factory: console, SQLite configuration and execution.

The peripheral owns everything user-facing and execution-related:

* the console static mount, personnel/session login and the ``/admin`` APIs that
  read or write peripheral SQLite (people, providers, model catalog, non-secret
  JEV catalog, time periods, changes);
* the graph-management **proxy** (``routes_core_proxy``): every graph request is
  authenticated here and executed by the kernel — the peripheral has no graph
  repository, publisher or routing service of its own;
* the standalone data plane (``/v1/chat/completions``): ask the kernel for a
  decision, resolve the Provider Key from peripheral SQLite and execute the
  model request; fixed-model admin test chat resolves the catalog directly;
* the volatile 20-event memory log (``/admin/requests/*``).  The kernel's
  decision-event receiver is NOT mounted here: it lives on the separate internal
  listener (``router.api.internal_app``) so external console clients can never
  inject or poison decision logs (I5).

Deliberately absent (kernel-owned): graph JSON, routing/JEV execution, decision
events production and model upstream calls during a decision.  The peripheral
also keeps no request-log database and no graph snapshots any more: the only
decision log is the volatile in-memory event ring.  The only kernel boundary is
``app.state.core`` (CoreClient).

Readiness reflects peripheral state only (SQLite migrated).  An unreachable
kernel is a data-plane/management error, never a readiness failure: the
peripheral stays healthy so operators can still open the console and inspect the
configuration while the kernel is being restarted.

Startup hardening (C1/C2) lives below: ``create_app`` refuses a weak admin
credential before migrating.  The C2 stale-account scan costs up to 18 scrypt
verifications per active user, so a non-loopback start (a) reuses the read-only
preflight result in the post-migration remediation instead of scanning twice,
and (b) memoizes a **clean** result in the ``settings`` row
``admin_weak_scan_state`` as a fingerprint of the active users'
``(id, password_hash)`` pairs.  A later start skips the scrypt scan only when
that fingerprint is byte-identical; any password, row, status or policy change
invalidates it (no stale/refusal result is ever cached).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from router import password_policy
from router.api.issue_presenter import issues_payload
from router.api.routes_core_proxy import router as core_proxy_router
from router.config import (
    DEFAULT_ADMIN_PASSWORD,
    LOOPBACK_HOSTS,
    Settings,
)
from router.control.errors import (
    AlreadyExists,
    DefaultGraphRequired,
    MissingError,
    ReferencedByError,
    ValidationFailed,
)
from router.control.remote_catalog_sync import (
    CatalogSyncDiverged,
    CatalogWriteNotApplied,
    RemoteCatalogSyncService,
)
from router.core_client import CoreClient, CoreGraphError
from router.decision_log_store import DecisionLogStore
from router.jev.service import JevService
from router.store.db import connect, migrate
from router.store.jev_services import JevServiceRepo
from router.store.repositories import (
    SessionRepo,
    SettingsRepo,
    UserRepo,
    seed_default,
    transaction,
)
from router.store.routing_trees import (
    GraphAlreadyExistsError,
    GraphConflictError,
    GraphStorageError,
)
from router.upstream.breaker import CircuitBreaker

CONSOLE_DIR = Path(__file__).parent.parent / "console"
logger = logging.getLogger(__name__)

#: ``settings`` 键：非 loopback 启动时旧库弱口令扫描的结论缓存。值是对 active
#: 用户 ``(id, password_hash)`` 排序对的 SHA-256（见
#: :func:`router.password_policy.stored_credentials_fingerprint`）。**只有 clean
#: 结果才写入**：读到相同指纹就跳过每账号最多 18 次 scrypt 的扫描。
_WEAK_SCAN_SETTING = "admin_weak_scan_state"


def _active_users(rows: list[dict]) -> list[dict]:
    """Active-only filter; a users table without ``status`` has no disabled rows.

    手工/极旧库的 ``users`` 表可能没有 ``status`` 列：迁移链不补这个列，此时
    所有行都按 active 处理（与 preflight 的历史行为一致），不能让一条记录把
    启动变成 KeyError。
    """
    active = []
    for user in rows:
        user.setdefault("status", "active")
        if user["status"] == "active":
            active.append(user)
    return active


def _read_weak_scan_cache(conn) -> str | None:
    """Clean-scan fingerprint stored in ``settings``, ``None`` when unavailable.

    任何读取失败（fresh DB/旧库没有 ``settings`` 表、连接异常）都当作「没有
    缓存」：调用方继续做真实扫描，行为与没有缓存时完全一致。
    """
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (_WEAK_SCAN_SETTING,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def _active_credential_rows(conn) -> list[dict] | None:
    """One cheap SELECT: ``id``/``password_hash`` of the active users.

    Returns ``None`` when the table has no ``id``/``password_hash`` column and
    therefore cannot be fingerprinted (no cache, scan as today).
    """
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    except sqlite3.Error:
        return None
    if not {"id", "password_hash"} <= columns:
        return None
    selected = [column for column in ("id", "password_hash", "status")
                if column in columns]
    rows = [dict(row) for row in conn.execute(
        f"SELECT {', '.join(selected)} FROM users")]
    return _active_users(rows)


def _remember_clean_scan(conn) -> None:
    """Persist the fingerprint of a scan that proved the stored hashes clean.

    只在 clean 分支调用：stale 分支只会拒绝启动或强制重置，绝不写入「干净」
    结论。缓存只是性能优化，写入失败不影响启动（下一次启动重新扫描）。
    """
    rows = _active_credential_rows(conn)
    if rows is None:
        return
    try:
        with transaction(conn):
            SettingsRepo(conn).set(
                _WEAK_SCAN_SETTING,
                password_policy.stored_credentials_fingerprint(rows))
    except sqlite3.Error:
        logger.warning("could not store the admin credential scan cache; the "
                       "next start will rescan", exc_info=True)


def _catalog_sync_factory(client: CoreClient):
    """Bind the cross-service catalog write use-case to a request's SQLite conn."""

    def build(conn) -> RemoteCatalogSyncService:
        return RemoteCatalogSyncService(conn, client)

    return build


def _is_loopback_bind(settings: Settings) -> bool:
    return settings.host.strip().lower() in LOOPBACK_HOSTS


def _guard_admin_credentials(settings: Settings) -> None:
    """C1: refuse weak or publicly-known admin passwords on a non-loopback bind.

    The seed values exist only so ``python -m router`` on this machine works out
    of the box.  A deployment (compose binds ``0.0.0.0``) must provide an
    explicit ``ROUTER_ADMIN_PASSWORD``: silently seeding ``1qaz@WSX`` — or an
    empty/whitespace-only/short password — on a reachable listener is a critical
    security defect.  The checks key on the **password** only: changing the email
    while keeping the public password must not pass.  Shape checks come from the
    shared :mod:`router.password_policy` (the same rule the user API enforces),
    so a deployment cannot start with a password the console would reject.
    """
    if not settings.user_auth:
        return
    if _is_loopback_bind(settings):
        return
    password = settings.admin_password
    reason = password_policy.password_policy_error(password)
    if reason is None and password == DEFAULT_ADMIN_PASSWORD:
        reason = "is the publicly-known default password"
    if reason is None:
        return
    raise RuntimeError(
        "refusing to start the peripheral console on non-loopback host "
        f"{settings.host!r}: ROUTER_ADMIN_PASSWORD {reason}; it must be an "
        f"explicit value of at least {password_policy.MIN_PASSWORD_LENGTH} "
        "characters that is not the publicly-known default "
        "(ROUTER_ADMIN_EMAIL may also be overridden; or bind ROUTER_HOST to "
        "127.0.0.1 for local-only use)"
    )


def _preflight_stale_admin_users(db_path: str) -> list[dict]:
    """C2 preflight: strictly read-only stale-account scan *before* migrating.

    The v6→v7 migration is destructive (it drops the SQLite decision logs and
    the stored JEV key), so an unauthorized start must decide whether to refuse
    **before** ``migrate()`` runs; otherwise a rejected start has already lost
    data.  This helper therefore opens the database with ``mode=ro`` and never
    creates, migrates or writes anything:

    * the database file does not exist yet (fresh install) → ``[]``;
    * the file has no ``users`` table (pre-user-management build) → ``[]``;
    * very old ``users`` tables may lack the ``status`` column: that build had
      no disabled accounts, so every row counts as active.  Only the columns
      that actually exist are selected.

    Scan cache: when a ``settings`` table exists, ``admin_weak_scan_state`` is
    read first.  If it equals the fingerprint of the current active users'
    ``(id, password_hash)`` pairs, a previous start already verified exactly
    these hashes as clean, so the 18-scrypt-per-user scan is skipped entirely
    (``[]``).  This connection is read-only (nothing may be written before
    migrating), so the cache is only read here; the writable post-migration
    connection refreshes it via :func:`_remember_clean_scan`.  A missing table,
    key or ``id``/``password_hash`` column means "no cache", i.e. scan as today.

    Nothing is migrated/seeded/written through this connection.  SQLite may
    still create its empty WAL sidecar files (``-wal``/``-shm``) when the
    database is in WAL mode; row/page content is not modified.
    """
    path = os.fspath(db_path)
    if not Path(path).is_file():
        return []
    uri = f"file:{quote(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "users" not in tables:
            return []
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        selected = [column for column in
                    ("id", "email", "password_hash", "status")
                    if column in columns]
        if not selected:
            return []
        rows = [dict(row) for row in conn.execute(
            f"SELECT {', '.join(selected)} FROM users ORDER BY rowid")]
        active = _active_users(rows)
        if {"id", "password_hash"} <= columns:
            cached = _read_weak_scan_cache(conn)
            fingerprint = password_policy.stored_credentials_fingerprint(active)
            if cached == fingerprint:
                return []
    finally:
        conn.close()
    return [
        user for user in active
        if password_policy.is_weak_stored_password(user.get("password_hash"))
    ]


def _stale_admin_runtime_error(
        settings: Settings, stale: list[dict]) -> RuntimeError:
    """The operator-facing refusal shared by the preflight and the post-scan."""
    emails = ", ".join(str(user.get("email")) for user in stale)
    return RuntimeError(
        "refusing to start the peripheral console on non-loopback host "
        f"{settings.host!r}: the existing database still accepts a "
        "publicly-known, blank or whitespace-only admin password for: "
        f"{emails}. Remediate by either (a) starting once with "
        "ROUTER_HOST=127.0.0.1 and changing the affected password through "
        "the console or the user API, or (b) setting "
        "ROUTER_ADMIN_FORCE_RESET=1 and restarting: the listed accounts' "
        "passwords are reset to ROUTER_ADMIN_PASSWORD and all their "
        "sessions are revoked"
    )


def _guard_stale_admin_credentials(settings: Settings) -> list[dict] | None:
    """C2 preflight: refuse before the destructive v6→v7 migration runs.

    Only decides "refuse before touching the database": with
    ``ROUTER_ADMIN_FORCE_RESET=1`` (or no stale account) the caller proceeds to
    migrate/seed/remediate exactly as before, and the post-migration remediation
    remains the authority for the authorized reset path.

    Returns the stale users the preflight found (``[]`` also covers a clean
    scan-cache hit, i.e. "already verified clean") so :func:`create_app` can
    hand the result to :func:`_remediate_stale_admin_credentials` instead of
    scanning every active user's hash twice per start.  Returns ``None`` when no
    scan was performed at all (loopback bind / ``user_auth`` off): callers must
    not treat ``None`` as "clean".
    """
    if not settings.user_auth:
        return None
    if _is_loopback_bind(settings):
        return None
    stale = _preflight_stale_admin_users(settings.db_path)
    if stale and not settings.admin_force_reset:
        raise _stale_admin_runtime_error(settings, stale)
    return stale


def _stale_admin_users(conn) -> list[dict]:
    """Active users whose stored hash accepts a public/blank/whitespace password.

    ``status`` missing (hand-made pre-status schema) means active: the migration
    chain never adds that column, so the defensive ``.get`` keeps such a database
    from crashing the scan with a KeyError.
    """
    return [
        user for user in _active_users(UserRepo(conn).list())
        if password_policy.is_weak_stored_password(user.get("password_hash"))
    ]


def _remediate_stale_admin_credentials(
        conn, settings: Settings, *, stale: list[dict] | None = None) -> None:
    """C2: an old database must not keep a public (default/blank/space) password.

    ``UserRepo.seed_admin()`` only seeds an empty ``users`` table, so an existing
    volume keeps whatever hash it stored — a database seeded by an old version
    keeps ``admin@test.com`` / ``1qaz@WSX`` (or, after the C1 hazard, an empty or
    whitespace-only password) forever.  On a non-loopback bind the only
    acceptable end state is that no active user accepts a publicly-known
    password: without explicit authorization refuse to start (operator changes
    the password locally); with ``ROUTER_ADMIN_FORCE_RESET=1`` rotate those
    accounts to the validated ``admin_password`` and revoke all their sessions.

    ``stale`` is the preflight result computed seconds earlier by
    :func:`_guard_stale_admin_credentials`: the migration never touches
    ``users``, and ``seed_admin`` only writes the already-validated env
    password, so within one start that list cannot go stale — passing it skips
    the second 18-scrypt-per-user scan.  ``None`` means "no preflight result"
    (callers other than :func:`create_app`); the scan then happens here, still
    cache-aware.  An empty list means "the preflight already proved these hashes
    clean", in which case only the clean-scan cache is refreshed.
    """
    if not settings.user_auth:
        return
    if _is_loopback_bind(settings):
        return
    if stale is None:
        credentials = _active_credential_rows(conn)
        if credentials is not None and _read_weak_scan_cache(conn) == \
                password_policy.stored_credentials_fingerprint(credentials):
            return
        stale = _stale_admin_users(conn)
        if not stale:
            _remember_clean_scan(conn)
            return
    elif not stale:
        # The preflight holds a read-only connection (no write before migrating),
        # so persist the clean fingerprint here, after migration.
        _remember_clean_scan(conn)
        return
    emails = [user.get("email") for user in stale]
    if not settings.admin_force_reset:
        raise _stale_admin_runtime_error(settings, stale)
    # C2 整批原子：所有账号的改密 + 会话吊销在同一个 BEGIN IMMEDIATE 事务里。
    # 逐账号事务会在第 N 个失败时留下「前 N-1 个已修好」的半状态；下次启动不再
    # 把它们判为 stale，旧 token 也可能已经失效而账号状态不一致。
    UserRepo(conn).reset_passwords_and_sessions(
        [(user["id"], settings.admin_password) for user in stale])
    # 重置后的哈希就是已通过 env 守卫的 ``admin_password``：无需再跑一次扫描。
    _remember_clean_scan(conn)
    logger.critical(
        "ROUTER_ADMIN_FORCE_RESET=1: reset the password of %s and revoked all "
        "their sessions (the stored hash still accepted a publicly-known, "
        "blank or whitespace-only password)", ", ".join(str(email) for email in emails))


def _admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


_ADMIN_PUBLIC_PATHS = {"/admin/login"}
_VIEWER_WRITE_WHITELIST = {("POST", "/admin/logout"), ("POST", "/admin/me/password")}


def _bearer(header: str) -> str:
    prefix = "bearer "
    return header[len(prefix):].strip() if header[:len(prefix)].lower() == prefix else ""


def _viewer_forbidden(method: str, path: str) -> bool:
    if path.startswith("/admin/users"):
        return True
    if method in ("GET", "HEAD"):
        return False
    return (method, path) not in _VIEWER_WRITE_WHITELIST


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": "unauthorized: /admin requires login"}, headers={"WWW-Authenticate": "Bearer"})


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "forbidden: admin role required"})


def _install_admin_auth(app: FastAPI, settings: Settings) -> None:
    if not settings.user_auth:
        return
    db_path = settings.db_path

    @app.middleware("http")
    async def admin_auth(request: Request, call_next):
        path = request.url.path
        if not _admin_path(path):
            return await call_next(request)
        normalized = path.rstrip("/") or "/"
        if normalized in _ADMIN_PUBLIC_PATHS:
            return await call_next(request)
        token = _bearer(request.headers.get("authorization", ""))
        if token:
            conn = connect(db_path)
            try:
                user = SessionRepo(conn).user_for_token(token)
            finally:
                conn.close()
            if user is not None:
                request.state.user = {
                    "id": user["id"], "email": user["email"], "role": user["role"],
                    "service": False, "expires_at": user["expires_at"],
                }
                if user["role"] != "admin" and _viewer_forbidden(request.method, normalized):
                    return _forbidden()
                return await call_next(request)
        return _unauthorized()


#: ``settings`` 表里的「JEV 目录已用环境种子初始化过」标记。
#: 与 ``seed_default`` 使用的 ``catalog_seeded``（provider/model 种子）分开：
#: 该标记只在**确实写入**环境 JEV 时设置，用于保证环境种子只发生一次。
JEV_SEEDED_SETTING = "jev_catalog_seeded"


def _seed_jev(conn, settings: Settings) -> None:
    """boot 种子：环境配置只在整个生命周期内创建一次 ``default`` 服务。

    种子只写非密钥目录字段：JEV 密钥由内核从 ``ROUTER_JEV_API_KEY`` 读取，
    v7 起不再写入 SQLite；外围进程也没有任何 JEV 密钥或密钥通道（I6）。

    - 目录里已有任何服务就完全跳过（数据库目录是事实源，不覆盖管理结果）。
    - 只有从未用环境种子初始化过（``settings`` 表的 ``jev_catalog_seeded``
      标记缺失）且目录为空时才播种：因此「首启无环境、之后补环境」仍会播种一次，而「全部删除
      后重启」不会把 ``default`` 复活。
    - 没有配置环境 URL/model 时不写标记，留给之后带环境启动的机会。
    - 不读取任何画布、不触发任何图同步：启动路由集只由启用图 JSON 决定。
    - 环境只配置 URL/model 之一时仍是配置错误（保持原启动失败语义）。
    """
    repo = JevServiceRepo(conn)
    if repo.list():
        return
    if SettingsRepo(conn).get(JEV_SEEDED_SETTING) is not None:
        return
    if not settings.jev_url and not settings.jev_model:
        return
    if not settings.jev_url or not settings.jev_model:
        raise RuntimeError("ROUTER_JEV_URL and ROUTER_JEV_MODEL must be configured together")
    service = JevService(
        id="default",
        base_url=settings.jev_url,
        model=settings.jev_model,
        timeout_seconds=settings.jev_timeout,
    )
    with transaction(conn):
        repo.save(service)
        SettingsRepo(conn).set(JEV_SEEDED_SETTING, "1")


def create_app(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    core_transport: httpx.AsyncBaseTransport | None = None,
    decision_logs: DecisionLogStore | None = None,
) -> FastAPI:
    """Build the peripheral service: no graph ownership, no local routing.

    ``decision_logs`` lets :func:`router.api.internal_app.create_service_apps`
    share one memory store between the public console app and the internal
    receiver app; standalone callers (and tests) may omit it and get a fresh
    store.
    """
    # C1 first: a start must never accept a weak/default admin password, and a
    # forced stale-account reset below must target a validated password.
    _guard_admin_credentials(settings)
    # C2 preflight BEFORE any write: the v6→v7 migration is destructive, so an
    # unauthorized start must be refused while the old database is still intact.
    # The scan is strictly read-only, cache-aware and a no-op for loopback /
    # user_auth=False; its result is reused below so no hash is scanned twice.
    stale_admin_users = _guard_stale_admin_credentials(settings)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        seed_default(conn)
        if settings.user_auth:
            UserRepo(conn).seed_admin(settings.admin_email, settings.admin_password)
        # C2: seed_admin is a no-op on a non-empty users table, so an old volume
        # can still hold a publicly-known password; inspect the actual rows.
        # The preflight list is authoritative for this start (migration does not
        # touch users; seed_admin only writes the validated env password).
        _remediate_stale_admin_credentials(
            conn, settings, stale=stale_admin_users)
        _seed_jev(conn, settings)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # The kernel boundary first, then the model-upstream client: no
            # background worker or SQLite connection is owned by this app.
            await app.state.core.aclose()
            await app.state.http.aclose()

    app = FastAPI(title="RoutePilot", lifespan=lifespan)
    app.state.settings = settings
    #: SQLite migrated at construction: readiness does not depend on the kernel.
    app.state.db_ready = True
    app.state.http = httpx.AsyncClient(timeout=60.0, transport=transport)
    # 内核客户端自备 AsyncClient：浏览器/上游 transport 不复用到内核管理面。
    app.state.core = CoreClient(
        settings.core_url,
        http=httpx.AsyncClient(
            timeout=settings.core_timeout_seconds, transport=core_transport),
        timeout_seconds=settings.core_timeout_seconds,
    )
    # 目录写（模型 / provider / JEV / 时段）的跨服务事务：SQLite 单写事务 +
    # 内核条件图批次 + 反向补偿。路由只做 DTO 与影响预览。
    app.state.catalog_sync_factory = _catalog_sync_factory(app.state.core)
    # 外围执行层的上游熔断（只记录本进程的上游执行结果，不参与内核调度）。
    # JEV 调用（含连接测试）只属于内核：外围没有 JEV 客户端，也没有密钥。
    app.state.breaker = CircuitBreaker()
    # 决策日志只保存在内存：完整事件来自内核事件接收口，重启即清空（Global Constraint 5）。
    # 生产进程通过 create_service_apps 传入与内部监听共享的 store；独立使用时自建。
    app.state.decision_logs = (
        decision_logs if decision_logs is not None else DecisionLogStore())

    @app.exception_handler(GraphConflictError)
    async def graph_conflict_handler(request: Request, exc: GraphConflictError):
        return JSONResponse(status_code=409, content={"detail": {
            "code": "graph_conflict",
            "actual_document_sha256": exc.actual_document_sha256,
        }})

    @app.exception_handler(GraphAlreadyExistsError)
    async def graph_exists_handler(request: Request, exc: GraphAlreadyExistsError):
        return JSONResponse(status_code=409, content={"detail": {
            "code": "graph_already_exists",
            "tree_id": str(exc),
        }})

    @app.exception_handler(GraphStorageError)
    async def graph_storage_handler(request: Request, exc: GraphStorageError):
        return JSONResponse(status_code=503, content={"detail": {
            "code": "graph_storage_error",
            "message": str(exc),
        }})

    # 服务层异常 → HTTP 的统一映射：ValidationFailed/引用冲突/缺失
    # 由服务抛出，这里构成唯一映射点；路由层只做 DTO 解析与最小 try 包装。
    @app.exception_handler(ValidationFailed)
    async def validation_failed_handler(request: Request, exc: ValidationFailed):
        return JSONResponse(status_code=422, content={"detail": issues_payload(exc.issues)})

    @app.exception_handler(MissingError)
    async def missing_error_handler(request: Request, exc: MissingError):
        return JSONResponse(status_code=404, content={"detail": exc.message})

    @app.exception_handler(AlreadyExists)
    async def already_exists_handler(request: Request, exc: AlreadyExists):
        return JSONResponse(status_code=409, content={"detail": exc.message})

    @app.exception_handler(ReferencedByError)
    async def referenced_by_error_handler(request: Request, exc: ReferencedByError):
        return JSONResponse(status_code=409, content={"detail": exc.detail})

    @app.exception_handler(DefaultGraphRequired)
    async def default_graph_required_handler(
            request: Request, exc: DefaultGraphRequired):
        """默认图之外仍有启用图时禁止直接停用默认图（结构化 409）。"""
        return JSONResponse(status_code=409, content={"detail": {
            "code": "default_graph_required",
            "tree_id": exc.tree_id,
        }})

    @app.exception_handler(CoreGraphError)
    async def core_graph_error_handler(request: Request, exc: CoreGraphError):
        """内核图管理面不可用：保留内核的结构化 detail（503 graph_* 码）。"""
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return JSONResponse(status_code=503, content={"detail": detail})
        return JSONResponse(
            status_code=503, content={"detail": {"code": "core_unavailable"}})

    @app.exception_handler(CatalogSyncDiverged)
    async def catalog_sync_diverged_handler(
            request: Request, exc: CatalogSyncDiverged):
        """SQLite 已回滚但内核补偿失败：显式 503，绝不返回成功。"""
        return JSONResponse(status_code=503, content={
            "detail": {"code": "catalog_sync_diverged"}})

    @app.exception_handler(CatalogWriteNotApplied)
    async def catalog_write_not_applied_handler(
            request: Request, exc: CatalogWriteNotApplied):
        """探测证明内核未应用本批：503 目录写失败，可安全重试。

        ``graph_batch_committed`` 只对「已提交但未确认」诚实；这里的事实相反
        （每张画布仍是原图，SQLite 已回滚），因此不能复用那个 code。
        """
        return JSONResponse(status_code=503, content={"detail": {
            "code": "catalog_write_failed",
            "message": (
                "the kernel did not apply the catalog's graph batch: the "
                "peripheral SQLite write was rolled back, safe to retry"),
        }})

    _install_admin_auth(app, settings)

    @app.get("/")
    def root():
        return RedirectResponse("/console/index.html")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        """Peripheral readiness: SQLite migrated. The kernel is not probed."""
        ready = bool(app.state.db_ready)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "database": "ok" if ready else "unavailable"},
        )

    @app.middleware("http")
    async def console_nocache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/console/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    if CONSOLE_DIR.is_dir():
        app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")

    from router.api.routes_jev import router as jev_router
    from router.api.routes_logs import router as logs_router
    from router.api.routes_models import router as models_router
    from router.api.routes_test import router as test_router
    from router.api.routes_time import router as time_router
    from router.api.routes_users import router as users_router
    from router.api.routes_v1 import router as v1_router

    app.include_router(models_router, prefix="/admin")
    app.include_router(time_router, prefix="/admin")
    # 图管理经内核执行：外围只保留 /admin/trees、/admin/try 的鉴权与代理契约。
    app.include_router(core_proxy_router, prefix="/admin")
    app.include_router(jev_router, prefix="/admin")
    app.include_router(logs_router, prefix="/admin")
    app.include_router(users_router, prefix="/admin")
    app.include_router(test_router, prefix="/admin")
    # I5：内核事件接收口刻意不挂在这里。它只存在于独立内部监听
    # （router.api.internal_app），公开控制台监听上该路径是 404。
    # 决策端点（/internal/v1/route-decision）只属于内核：外围不挂载它；
    # 外围与内核的 HTTP 边界是 app.state.core（CoreClient）。
    app.include_router(v1_router, prefix="/v1")
    return app
