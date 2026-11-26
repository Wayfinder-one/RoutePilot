"""One password policy shared by every credential entry point.

历史问题：启动守卫（``router.api.app._guard_admin_credentials``）自己手写
「空 / 纯空白 / 过短 / 公开默认」判断，而控制台 DTO 只声明
``Field(min_length=8)``。于是 8 个空格这种「长度达标、信息为零」的口令可以：
启动时通过 env 守卫、旧库扫描也认不出它（只查默认口令与空串）、API 还接受它。

这个模块是唯一的判断来源，三类调用方都必须走它：

* 启动时的 ``ROUTER_ADMIN_PASSWORD`` 守卫（``password_policy_error`` +
  公开默认口令检查）；
* 所有写口令的 DTO：创建用户 / 自助改密 / 管理员重置
  （``password_policy_error`` → FastAPI 422）；
* 旧库 stale 账号扫描（``is_weak_stored_password``）。

「公开 / 无信息口令」的定义：内置默认口令、空串、纯空白串。纯空白枚举
**有界**（1..16 个空格）：它覆盖「空白口令被旧 bug 持久化」的现实形态
（当前最小长度 8，再加一点余量），又不让每次启动扫描变成无界搜索。更长的
或其它花样的纯空白历史口令不在自动识别范围（见 README「已知限制」），
运维应主动轮换它们——它们不像空/默认口令那样广为人知。

注意模块导入方向：``router.config`` 从本模块 re-export
``MIN_ADMIN_PASSWORD_LENGTH``，而本模块从 ``router.config`` 读取
``DEFAULT_ADMIN_PASSWORD``。两边都在交叉导入之前定义好对方需要的属性，
所以 ``import router.config`` 与 ``import router.password_policy`` 任一
先后顺序都能完成初始化（``tests/test_users.py`` 有子进程级回归）。
"""
from __future__ import annotations

from collections.abc import Callable

from router import auth

#: 口令最小长度（唯一来源）。``router.config`` 把它 re-export 成既有的
#: ``MIN_ADMIN_PASSWORD_LENGTH``；启动守卫、DTO 与旧库扫描共用这个数字。
#: 必须在下面导入 ``router.config`` 之前定义：config 为 re-export 会反过来
#: 导入本模块，交叉导入时它读的正是此刻已存在的属性。
MIN_PASSWORD_LENGTH = 8

#: 纯空白候选的长度上界（含）。有界枚举的理由见模块 docstring。
_MAX_WHITESPACE_LENGTH = 16

# ``router.config`` 已先定义 DEFAULT_ADMIN_EMAIL/DEFAULT_ADMIN_PASSWORD 再
# 导入本模块（或本模块先定义 MIN_PASSWORD_LENGTH 再导入 config），因此这个
# 交叉导入在任一入口顺序下都安全。
from router.config import DEFAULT_ADMIN_PASSWORD  # noqa: E402

#: 扫描顺序：默认口令与空串最先命中（绝大多数 stale 旧库是这两种），
#: 然后才是 1..16 个空白。顺序只影响性能，不影响结果。
_PUBLIC_PASSWORD_CANDIDATES: tuple[str, ...] = (
    DEFAULT_ADMIN_PASSWORD,
    "",
    *(" " * length for length in range(1, _MAX_WHITESPACE_LENGTH + 1)),
)

#: 全部「公开已知 / 信息为零」的口令（只读集合，供调用方枚举/判断）。
PUBLIC_PASSWORDS = frozenset(_PUBLIC_PASSWORD_CANDIDATES)


def password_policy_error(password: object) -> str | None:
    """Return a human-readable error when ``password`` is unusable, else ``None``.

    只检查「形状」：非字符串、空、纯空白（``.strip()`` 为空）、短于
    :data:`MIN_PASSWORD_LENGTH` 都不可用。内置默认口令是合法形状（长度够、
    非空白），它属于「公开已知」而不是「形状非法」，由启动守卫单独拒绝。
    """
    if not isinstance(password, str):
        return "must be a string"
    if not password.strip():
        return "is empty or whitespace-only"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"is shorter than {MIN_PASSWORD_LENGTH} characters"
    return None


def is_public_password(value: object, stored_hash: str | None = None) -> bool:
    """Whether ``value`` is a publicly-known or information-free password.

    ``stored_hash`` 可选：给了它就额外要求该哈希确实接受 ``value``（用于问
    「这个公开值是不是这一行的口令」）；只传 ``value`` 时就是纯粹的集合判断。
    一个强哈希不会仅因为世界上存在某个公开口令就被判成弱口令。
    """
    if not isinstance(value, str) or value not in PUBLIC_PASSWORDS:
        return False
    return stored_hash is None or auth.verify_password(value, stored_hash)


def is_weak_stored_password(
    stored_hash: object,
    verify: Callable[[str, str], bool] = auth.verify_password,
) -> bool:
    """True when ``stored_hash`` accepts any publicly-known/blank password.

    ``verify`` 是 ``(password, stored_hash) -> bool`` 的注入点（默认
    :func:`router.auth.verify_password`），批量扫描时便于测试替身；生产调用
    一律用真实 scrypt 校验。损坏/非字串哈希只是返回 False（绝不抛异常）。
    """
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    return any(verify(candidate, stored_hash)
               for candidate in _PUBLIC_PASSWORD_CANDIDATES)
