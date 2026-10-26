"""Console accounts: login/logout/me + user management (design doc §4).

鉴权本身在 ``router.api.app._install_admin_auth``（登录会话）里完成；这里的
路由只负责登录/登出/改密与用户 CRUD，并保证：

* 任何响应都不含 ``password_hash``/salt（spec §4「响应不含 password_hash」）；
* 所有用户写操作（创建/更新/重置密码/删除）追加 ``changes`` 审计，
  detail 里只有 metadata（spec §4 最后一条）；
* 最后 active admin 保护在**一个 BEGIN IMMEDIATE 写事务**里重新计数
  （spec §7「并发最后 admin 保护」），不依赖前端。

角色门（viewer 的 403）由中间件统一做，路由不再重复判断。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from router import auth
from router.api.deps import get_conn
from router.store.repositories import (ChangeRepo, EmailExists, SessionRepo,
                                       UserRepo, write_transaction)

router = APIRouter()

logger = logging.getLogger(__name__)

#: 没有中间件时（user_auth=False 的旧开放模式）的身份：按服务账号 super admin
#: 处理（spec §7 最后一条）。
SERVICE_USER = {"id": None, "email": None, "role": "admin",
                "service": True, "expires_at": None}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _current_user(request: Request) -> dict:
    """中间件挂在 request.state 上的身份；缺省 = 旧开放模式的服务账号。"""
    user = getattr(request.state, "user", None)
    return user if user else dict(SERVICE_USER)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "bearer "
    if header[:len(prefix)].lower() != prefix:
        return ""
    return header[len(prefix):].strip()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


def _audit(conn: sqlite3.Connection, actor: dict, action: str,
           user: dict, **extra) -> None:
    """Append one ``changes`` row with metadata only (never secrets).

    口令/哈希/salt 一律不进审计：审计是给人看的历史，不是凭据副本。
    """
    detail = {
        "action": action,
        "actor": {"id": actor.get("id"), "email": actor.get("email"),
                  "role": actor.get("role"),
                  "service": bool(actor.get("service"))},
        "user": {key: user.get(key)
                 for key in ("id", "email", "role", "status")},
        **extra,
    }
    ChangeRepo(conn).record(_now(), "user", detail)


def _normalize_validated_email(value: str) -> str:
    email = auth.normalize_email(value)
    if not auth.is_valid_email(email):
        raise ValueError("invalid email")
    return email


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    # 故意不加格式/长度约束：登录失败统一 401，不额外泄露参数是否合法。
    email: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


class UserCreateIn(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: Literal["admin", "viewer"] = "viewer"

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalize_validated_email(value)


class UserUpdateIn(BaseModel):
    """Patch body: omitted (or explicit null) = keep the current value."""

    email: str | None = None
    role: Literal["admin", "viewer"] | None = None
    status: Literal["active", "disabled"] | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_validated_email(value)


class UserPasswordIn(BaseModel):
    new_password: str = Field(min_length=8)


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
@router.post("/login")
def login(payload: LoginIn, request: Request,
          conn: sqlite3.Connection = Depends(get_conn)):
    settings = request.app.state.settings
    if not settings.user_auth:
        # 用户体系关闭时登录接口形同虚设，按登录失败处理（旧行为里 /admin
        # 由静态 token 或完全开放控制；这里不签发永远用不上的会话）。
        raise HTTPException(status_code=401,
                            detail="invalid email or password")
    email = auth.normalize_email(payload.email)
    repo = UserRepo(conn)
    user = repo.get_by_email(email)
    # 未注册 / 密码错 / disabled 统一 401：不泄露账号是否存在（spec §4）。
    if (user is None or user["status"] != "active"
            or not auth.verify_password(payload.password,
                                        user["password_hash"])):
        logger.warning("admin login failed: email=%r ip=%s",
                       email, _client_ip(request))
        raise HTTPException(status_code=401,
                            detail="invalid email or password")
    token, expires_at = SessionRepo(conn).create(
        user["id"], settings.session_ttl_hours)
    repo.record_login(user["id"])
    return {"token": token, "expires_at": expires_at,
            "user": {"id": user["id"], "email": user["email"],
                     "role": user["role"]}}


@router.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Revoke the current session token. 旧开放模式没有会话行，直接 no-op。"""
    user = _current_user(request)
    token = _bearer(request)
    if token and not user.get("service"):
        SessionRepo(conn).delete(token)
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = _current_user(request)
    if user.get("service"):
        return dict(SERVICE_USER)
    return {"id": user["id"], "email": user["email"], "role": user["role"],
            "expires_at": user.get("expires_at")}


@router.post("/me/password")
def change_own_password(payload: PasswordChangeIn, request: Request,
                        conn: sqlite3.Connection = Depends(get_conn)):
    actor = _current_user(request)
    if actor.get("service"):
        raise HTTPException(status_code=400,
                            detail="service token cannot change password")
    repo = UserRepo(conn)
    user = repo.get(actor["id"])
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if not auth.verify_password(payload.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="invalid old password")
    # 改密 + 吊销该用户全部会话（含当前）必须原子：半途失败不能留下
    # 「密码已换、旧会话还能用」的状态。
    with write_transaction(conn):
        repo.set_password(user["id"], payload.new_password)
        SessionRepo(conn).delete_for_user(user["id"])
    _audit(conn, actor, "change_password", user)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 用户管理（仅 admin；viewer 在中间件就被 403）
# --------------------------------------------------------------------------- #
@router.get("/users")
def list_users(conn: sqlite3.Connection = Depends(get_conn)):
    repo = UserRepo(conn)
    return {"users": [repo.public(user) for user in repo.list()]}


@router.post("/users", status_code=201)
def create_user(payload: UserCreateIn, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    repo = UserRepo(conn)
    try:
        user = repo.create(payload.email, payload.password, role=payload.role)
    except EmailExists:
        raise HTTPException(status_code=409,
                            detail="email already exists") from None
    _audit(conn, _current_user(request), "create", user)
    return {"user": repo.public(user)}


@router.put("/users/{user_id}")
def update_user(user_id: str, payload: UserUpdateIn, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    repo = UserRepo(conn)
    changes = payload.model_dump(exclude_none=True)
    with write_transaction(conn):
        # BEGIN IMMEDIATE 之后读到的计数才是权威：另一个写者可能在本次请求
        # 读档与写入之间降级了同僚（spec §7 并发保护）。
        user = repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        new_role = changes.get("role", user["role"])
        new_status = changes.get("status", user["status"])
        loses_admin = (user["role"] == "admin" and user["status"] == "active"
                       and (new_role != "admin" or new_status != "active"))
        if loses_admin and repo.count_active_admins() <= 1:
            raise HTTPException(
                status_code=400,
                detail="cannot demote or disable the last active admin")
        try:
            updated = repo.update(user_id, **changes)
        except EmailExists:
            raise HTTPException(status_code=409,
                                detail="email already exists") from None
        if updated is None:                       # 并发删除：仍按 404 处理
            raise HTTPException(status_code=404, detail="user not found")
    _audit(conn, _current_user(request), "update", updated,
           changes=sorted(changes))
    return {"user": repo.public(updated)}


@router.post("/users/{user_id}/password")
def reset_password(user_id: str, payload: UserPasswordIn, request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
    actor = _current_user(request)
    repo = UserRepo(conn)
    with write_transaction(conn):
        user = repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        repo.set_password(user_id, payload.new_password)
        SessionRepo(conn).delete_for_user(user_id)
    _audit(conn, actor, "reset_password", user)
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: str, request: Request,
                conn: sqlite3.Connection = Depends(get_conn)):
    actor = _current_user(request)
    if actor.get("id") is not None and actor["id"] == user_id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    repo = UserRepo(conn)
    with write_transaction(conn):
        user = repo.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if (user["role"] == "admin" and user["status"] == "active"
                and repo.count_active_admins() <= 1):
            raise HTTPException(status_code=400,
                                detail="cannot delete the last active admin")
        repo.delete(user_id)
        SessionRepo(conn).delete_for_user(user_id)   # 删用户同时删会话
    _audit(conn, actor, "delete", user)
    return {"ok": True}
