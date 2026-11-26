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

扫描缓存（``stored_credentials_fingerprint``）：非 loopback 启动时旧库扫描是
每个 active 账号最多 18 次 scrypt 校验，因此 ``router.api.app`` 把「这批
active 用户的 ``(id, password_hash)`` 已证实干净」这一结论的指纹写进 ``settings``
表（``admin_weak_scan_state``），下次启动指纹逐字节一致就跳过扫描。指纹前缀
绑定候选集合的摘要，所以增删/改动候选会让所有旧缓存自动失效；任何账号增删、
状态变化或口令哈希改动也会改变指纹。它只短路「输入哈希与上次完全相同」的
扫描，不是口令黑名单的替代，也不缓存 stale 结论（只有 clean 结果才落库）。

注意模块导入方向：``router.config`` 从本模块 re-export
``MIN_ADMIN_PASSWORD_LENGTH``，而本模块从 ``router.config`` 读取
``DEFAULT_ADMIN_PASSWORD``。两边都在交叉导入之前定义好对方需要的属性，
所以 ``import router.config`` 与 ``import router.password_policy`` 任一
先后顺序都能完成初始化（``tests/test_users.py`` 有子进程级回归）。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping

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

#: 扫描缓存指纹的策略前缀：候选集合本身的 SHA-256。候选集合是
#: :func:`is_weak_stored_password` 的完整判定输入之一，改动它（包括
#: ``DEFAULT_ADMIN_PASSWORD``）就必须让所有旧缓存失效，否则新策略会被旧结论
#: 静默跳过。
_POLICY_DIGEST = hashlib.sha256(
    b"\x00".join(candidate.encode("utf-8")
                 for candidate in _PUBLIC_PASSWORD_CANDIDATES)
).hexdigest()


def stored_credentials_fingerprint(
        users: Iterable[Mapping[str, object]], *,
        exempt_password: str | None = None) -> str:
    """Fingerprint of the stored credentials a weak-password scan depends on.

    ``users`` 必须恰好是「status 为 active」的用户行（至少含 ``id`` 与
    ``password_hash``）：它们是 :func:`is_weak_stored_password` 的唯一输入。
    ``exempt_password`` 是本次部署显式配置的管理员口令（见
    :func:`is_weak_stored_password`）：它同样参与指纹，否则「默认口令部署」
    留下的 clean 结论会在换成自定义口令后被误用。输出对排序后的
    ``(id, password_hash)`` 对做 SHA-256，因此与行顺序无关；任一账号增删、
    状态变化、哈希改动或豁免口令变化都会改变指纹。
    """
    pairs = sorted(
        (str(user.get("id") or ""), str(user.get("password_hash") or ""))
        for user in users
    )
    digest = hashlib.sha256(
        b"router.admin-weak-scan.v2\x00" + _POLICY_DIGEST.encode("ascii"))
    if exempt_password is None:
        digest.update(b"\x00exempt:none")
    else:
        encoded = exempt_password.encode("utf-8")
        digest.update(b"\x00exempt:" + str(len(encoded)).encode("ascii")
                      + b":" + encoded)
    for user_id, password_hash in pairs:
        digest.update(b"\x00" + user_id.encode("utf-8"))
        digest.update(b"\x00" + password_hash.encode("utf-8"))
    return digest.hexdigest()


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
    *,
    exempt_password: str | None = None,
) -> bool:
    """True when ``stored_hash`` accepts a publicly-known/blank password.

    ``exempt_password`` 是本次部署**显式配置**的管理员口令：内置默认凭据
    （``admin@test.com / 1qaz@WSX``）在首次部署时就是期望口令，因此当它等于
    部署配置时不算 stale；换过自定义口令的部署仍会把旧默认口令判为 stale，
    空/纯空白也永远判为 stale。

    ``verify`` 是 ``(password, stored_hash) -> bool`` 的注入点（默认
    :func:`router.auth.verify_password`），批量扫描时便于测试替身；生产调用
    一律用真实 scrypt 校验。损坏/非字串哈希只是返回 False（绝不抛异常）。
    """
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    candidates = _PUBLIC_PASSWORD_CANDIDATES
    if exempt_password is not None:
        candidates = tuple(candidate for candidate in candidates
                           if candidate != exempt_password)
    return any(verify(candidate, stored_hash)
               for candidate in candidates)
